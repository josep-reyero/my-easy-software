"""Rendering systemd user units for an app's jobs.

Each job becomes ``mes-<app>-<job>.service`` (``Type=oneshot``, sandboxed). A
job with a ``schedule`` also gets ``mes-<app>-<job>.timer``. A job with
``after = "<upstream>"`` is started by the upstream service through
``OnSuccess=``. Manual jobs only get the service.

The service runs the app's own command-line tool directly (``ExecStart`` is an
absolute argv, never a shell line). After every run, ``ExecStopPost=+`` runs
``mes jobs record`` outside the sandbox, which appends the run to the
tamper-evident run log; the log file itself is read-only inside the sandbox,
so the job cannot rewrite it.

Safety of the generated text
----------------------------
Every value written into a unit is checked or escaped here, so a manifest
cannot add directives of its own:

- App and job names must match their strict patterns (letters, digits, dashes).
- No value may contain control characters (newlines, carriage returns, tabs,
  NUL...). A newline would start a new directive, so it is always refused.
- ``ExecStart`` / ``ExecStopPost`` words are double-quoted with ``\\``, ``"``,
  ``%`` (specifiers) and ``$`` (variable expansion) escaped; the executable
  must be an absolute path; a bare ``;`` word is refused.
- ``Environment=`` values are double-quoted with ``\\``, ``"`` and ``%`` escaped.
- Folders given to ``ReadWritePaths=`` / ``ReadOnlyPaths=`` must be absolute,
  normalised, and made only of safe characters (no spaces, quotes or
  backslashes); ``WorkingDirectory=`` must be absolute with no backslash or
  edge spaces, and ``%`` is doubled.
- ``OnCalendar=``, ``MemoryMax=`` and the timeout are checked against tight
  patterns, and host names against the host-name pattern.

Anything that does not pass raises ``mes.errors.Refused`` (exit 5) with a
plain-language message.

Public API
----------
``UNIT_PREFIX`` (``"mes"``), ``SLICE`` (``"mes.slice"``), ``RANDOM_DELAY_SEC``,
``TIMEOUT_GRACE_SEC``, ``JOB_ALLOW_LEVEL`` (``"write"``), ``MARKER_APP``, ``MARKER_JOB``.

``unit_name(app, job, suffix="service") -> str``
    ``mes-<app>-<job>.<suffix>`` (names are checked first).
``quote_exec_word(value) -> str``, ``exec_line(argv) -> str``
    Quote one ``ExecStart`` word / a whole argv (first word must be absolute).
``env_assignment(key, value) -> str``
    A quoted ``Environment=`` value.
``sandbox_path(path) -> str``
    A path checked for ``ReadWritePaths=`` / ``ReadOnlyPaths=``.
``line_path(path) -> str``
    A path checked and escaped for ``WorkingDirectory=``.
``render_service(manifest, job, *, app_command, mes_command, root, data_home, state_home) -> str``
``render_timer(manifest, job) -> str``
``render_slice() -> str``
``render_app(manifest, *, app_command, mes_command, root, data_home, state_home) -> dict[str, str]``
    File name -> file text for every unit of the app (not the slice).
``read_markers(text) -> tuple[str | None, str | None]``
    The app and job a mes-made unit file belongs to, from its header.
``run_log_path(state_home, app) -> Path``
    Where the run log of an app lives (``<state_home>/<app>/runs.jsonl``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from mes.errors import Refused
from mes.manifest import APP_NAME_RE, JOB_NAME_RE, Job, Manifest

UNIT_PREFIX = "mes"
SLICE = "mes.slice"
RANDOM_DELAY_SEC = 120
TIMEOUT_GRACE_SEC = 30
JOB_ALLOW_LEVEL = "write"
RUN_LOG_FILE = "runs.jsonl"
MARKER_APP = "# mes-app: "
MARKER_JOB = "# mes-job: "
HEADER = "# Made by my-easy-software (mes). Do not edit: run 'mes jobs install' again instead."

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\u0085  ]")
_SAFE_PATH_RE = re.compile(r"/[A-Za-z0-9._/@+=,~-]*")
_SCHEDULE_RE = re.compile(r"[A-Za-z0-9*:,./~+_@-]+(?: [A-Za-z0-9*:,./~+_@-]+)*")
_MEMORY_RE = re.compile(r"[1-9][0-9]{0,6}[KMG]")
_TIMEOUT_RE = re.compile(r"([1-9][0-9]{0,6})([smh])")
_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST_RE = re.compile(rf"(?=.{{1,253}}$){_HOST_LABEL}(?:\.{_HOST_LABEL})*")
_ENV_KEY_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_SECONDS = {"s": 1, "m": 60, "h": 3600}


def _refuse(what: str, value: object, why: str, hint: str) -> Refused:
    return Refused(
        f"mes will not write {what} {value!r} into a system service file, because {why} "
        "Such a value could change how the job is protected, so nothing was installed.",
        hint=hint,
        code="unsafe_unit_value",
    )


def _no_control(what: str, value: object) -> str:
    if not isinstance(value, str):
        raise _refuse(what, value, "it is not text.", "Use plain text for this setting.")
    if _CONTROL_RE.search(value):
        raise _refuse(
            what,
            value,
            "it contains a line break or another invisible control character.",
            "Remove line breaks and control characters from this value (for folders: use a folder "
            "name without them).",
        )
    return value


def _check_app(app: str) -> str:
    if not isinstance(app, str) or not APP_NAME_RE.fullmatch(app):
        raise _refuse("the app name", app, "it is not a simple name.",
                      "App names use 2 to 31 lowercase letters, digits and dashes, starting with a letter.")
    return app


def _check_job(job: str) -> str:
    if not isinstance(job, str) or not JOB_NAME_RE.fullmatch(job):
        raise _refuse("the job name", job, "it is not a simple name.",
                      "Job names use lowercase letters, digits and dashes, starting with a letter.")
    return job


def unit_name(app: str, job: str, suffix: str = "service") -> str:
    if suffix not in ("service", "timer"):
        raise ValueError("suffix must be 'service' or 'timer'")
    return f"{UNIT_PREFIX}-{_check_app(app)}-{_check_job(job)}.{suffix}"


# --------------------------------------------------------------------------- escaping


def quote_exec_word(value: str) -> str:
    """One ExecStart word: double-quoted, with backslash, quote, % and $ escaped."""
    _no_control("the command word", value)
    if value == ";":
        raise _refuse("the command word", value, "a lone ';' separates commands in a service file.",
                      "Remove the ';' from the job's run line.")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def exec_line(argv: Sequence[str]) -> str:
    words = list(argv)
    if not words:
        raise ValueError("argv must not be empty")
    exe = _no_control("the program path", words[0])
    if not exe.startswith("/"):
        raise _refuse("the program path", exe, "the program must be given as a full path from '/'.",
                      "Install the app with 'uv sync' so its program has a full path.")
    return " ".join(quote_exec_word(w) for w in words)


def env_assignment(key: str, value: str) -> str:
    if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key):
        raise _refuse("the setting name", key, "it is not a simple upper-case name.", "Use A-Z, 0-9 and _.")
    _no_control(f"the value of {key}", value)
    escaped = f"{key}={value}".replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _norm_abs(what: str, path: str | os.PathLike[str]) -> str:
    text = _no_control(what, os.fspath(path))
    if not text.startswith("/"):
        raise _refuse(what, text, "it is not a full path from '/'.", "Use a full folder path.")
    norm = str(PurePosixPath(os.path.normpath(text)))
    if norm != text.rstrip("/") and norm != text:
        raise _refuse(what, text, "it contains '..', '.' or repeated slashes.", "Use a plain full folder path.")
    return norm


def sandbox_path(path: str | os.PathLike[str]) -> str:
    """A folder for ReadWritePaths=/ReadOnlyPaths=: only safe characters, no quoting needed."""
    norm = _norm_abs("the folder", path)
    if not _SAFE_PATH_RE.fullmatch(norm):
        raise _refuse(
            "the folder",
            norm,
            "it contains spaces, quotes, backslashes or other characters that the sandbox settings "
            "cannot hold safely.",
            "Keep the app's data and state folders at paths made of letters, digits, '.', '_', '-' "
            "and '/' (set MES_DATA_HOME / MES_STATE_HOME to such a folder).",
        )
    return norm


def line_path(path: str | os.PathLike[str]) -> str:
    """A folder for single-path directives such as WorkingDirectory= (rest of line)."""
    norm = _norm_abs("the folder", path)
    if "\\" in norm or norm != norm.strip():
        raise _refuse("the folder", norm, "it contains a backslash or starts or ends with a space.",
                      "Move the app to a folder whose path has no backslash and no edge spaces.")
    return norm.replace("%", "%%")


def _schedule(job: Job) -> str:
    value = _no_control("the schedule", job.schedule)
    if len(value) > 200 or not _SCHEDULE_RE.fullmatch(value):
        raise _refuse("the schedule", value, "it is not a plain calendar expression.",
                      'Use a systemd calendar expression such as "daily" or "*-*-* 06:00:00".')
    return value


def _memory(job: Job) -> str:
    if not isinstance(job.memory, str) or not _MEMORY_RE.fullmatch(job.memory):
        raise _refuse("the memory limit", job.memory, "it is not a size such as 256M.",
                      'Write memory = "256M" in app.toml.')
    return job.memory


def _timeout_seconds(job: Job) -> int:
    m = _TIMEOUT_RE.fullmatch(job.timeout) if isinstance(job.timeout, str) else None
    if not m:
        raise _refuse("the time limit", job.timeout, "it is not a duration such as 15m.",
                      'Write timeout = "15m" in app.toml.')
    return int(m.group(1)) * _SECONDS[m.group(2)]


def _hosts(job: Job) -> list[str]:
    hosts = []
    for h in job.network:
        if not isinstance(h, str) or not _HOST_RE.fullmatch(h):
            raise _refuse("the network host", h, "it is not a plain host name.",
                          'List only host names, for example network = ["api.example.com"].')
        hosts.append(h)
    return hosts


def _writes(job: Job) -> list[str]:
    out = []
    for w in job.writes:
        if w not in ("data", "state"):
            raise _refuse("the writable folder", w, 'only "data" and "state" exist.',
                          'Use writes = ["data", "state"] or a part of it.')
        out.append(w)
    return out


def run_log_path(state_home: str | os.PathLike[str], app: str) -> Path:
    return Path(state_home) / _check_app(app) / RUN_LOG_FILE


# --------------------------------------------------------------------------- rendering


def _header(app: str, job: str | None) -> list[str]:
    lines = [HEADER, MARKER_APP + _check_app(app)]
    if job is not None:
        lines.append(MARKER_JOB + _check_job(job))
    return lines


def render_service(
    manifest: Manifest,
    job: Job,
    *,
    app_command: Sequence[str],
    mes_command: Sequence[str],
    root: str | os.PathLike[str],
    data_home: str | os.PathLike[str],
    state_home: str | os.PathLike[str],
) -> str:
    app = _check_app(manifest.name)
    name = _check_job(job.name)
    hosts = _hosts(job)
    writes = _writes(job)
    timeout = _timeout_seconds(job)
    data_home_s = _norm_abs("the data folder", data_home)
    state_home_s = _norm_abs("the state folder", state_home)
    data_dir = sandbox_path(f"{data_home_s}/{app}")
    state_dir = sandbox_path(f"{state_home_s}/{app}")
    run_log = sandbox_path(f"{state_dir}/{RUN_LOG_FILE}")
    root_s = line_path(root)

    start = exec_line([*app_command, *job.argv])
    record = exec_line([*mes_command, "jobs", "record", "--app-dir", os.fspath(root), name])
    dependents = [unit_name(app, d.name) for d in manifest.dependents(job.name)]

    lines = _header(app, name)
    lines.append("# network: " + (
        f"full internet access (meant for {', '.join(hosts)}; mes cannot limit it to those hosts yet)"
        if hosts else "none"))
    lines += [
        "",
        "[Unit]",
        f"Description=mes job {name} of app {app}",
    ]
    if dependents:
        lines.append("OnSuccess=" + " ".join(dependents))
    lines += [
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={start}",
        f"ExecStopPost=+{record}",
        f"WorkingDirectory={root_s}",
        f"Environment={env_assignment('MES_ALLOW', JOB_ALLOW_LEVEL)}",
        f"Environment={env_assignment('MES_JOB', name)}",
        f"Environment={env_assignment('MES_DATA_HOME', data_home_s)}",
        f"Environment={env_assignment('MES_STATE_HOME', state_home_s)}",
        f"Environment={env_assignment('PYTHONDONTWRITEBYTECODE', '1')}",
        f"Slice={SLICE}",
        f"MemoryMax={_memory(job)}",
        "MemorySwapMax=0",
        f"TimeoutStartSec={timeout}",
        f"RuntimeMaxSec={timeout + TIMEOUT_GRACE_SEC}",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "PrivateUsers=yes",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
    ]
    if "data" in writes:
        lines.append(f"ReadWritePaths={data_dir.replace('%', '%%')}")
    if "state" in writes:
        lines.append(f"ReadWritePaths={state_dir.replace('%', '%%')}")
    lines.append(f"ReadOnlyPaths={run_log.replace('%', '%%')}")
    if hosts:
        lines.append("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6")
    else:
        lines.append("PrivateNetwork=yes")
        lines.append("RestrictAddressFamilies=AF_UNIX")
    lines += [
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "RestrictNamespaces=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service",
        "",
    ]
    return "\n".join(lines)


def render_timer(manifest: Manifest, job: Job) -> str:
    app = _check_app(manifest.name)
    name = _check_job(job.name)
    if job.trigger != "schedule" or job.schedule is None:
        raise ValueError(f"job {name!r} has no schedule")
    lines = _header(app, name) + [
        "",
        "[Unit]",
        f"Description=Schedule of mes job {name} of app {app}",
        "",
        "[Timer]",
        f"OnCalendar={_schedule(job)}",
        "Persistent=true",
        f"RandomizedDelaySec={RANDOM_DELAY_SEC}",
        f"Unit={unit_name(app, name)}",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ]
    return "\n".join(lines)


def render_slice() -> str:
    return "\n".join([
        HEADER,
        "",
        "[Unit]",
        "Description=Jobs of apps made with my-easy-software",
        "",
        "[Slice]",
        "",
    ])


def render_app(
    manifest: Manifest,
    *,
    app_command: Sequence[str],
    mes_command: Sequence[str],
    root: str | os.PathLike[str],
    data_home: str | os.PathLike[str],
    state_home: str | os.PathLike[str],
) -> dict[str, str]:
    files: dict[str, str] = {}
    for job in manifest.jobs.values():
        files[unit_name(manifest.name, job.name)] = render_service(
            manifest, job, app_command=app_command, mes_command=mes_command, root=root,
            data_home=data_home, state_home=state_home,
        )
        if job.trigger == "schedule":
            files[unit_name(manifest.name, job.name, "timer")] = render_timer(manifest, job)
    return files


def read_markers(text: str) -> tuple[str | None, str | None]:
    app = job = None
    for line in text.splitlines()[:6]:
        if line.startswith(MARKER_APP):
            app = line[len(MARKER_APP):].strip()
        elif line.startswith(MARKER_JOB):
            job = line[len(MARKER_JOB):].strip()
    return app, job
