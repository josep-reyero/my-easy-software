"""Rendering the systemd **system** units of a project on a server.

Every unit that runs project code runs under the project's own account (``User=mes-<project>``),
never as the owner or as root, so the sandbox settings are really enforced. ``mes server plan``
puts these files into the root script a person reads and runs once with sudo; mes itself never
installs them.

Unit names use dots, which project and job names cannot contain, so two projects can never
produce the same name (``mes-fit.job.data-sync`` and ``mes-fit-data.job.sync`` differ):

- ``mes-<p>.job.<job>.service``  one run of a job (``Type=oneshot``, sandboxed).
- ``mes-<p>.job.<job>.timer``    for a job with ``schedule``.
- ``mes-<p>.job.<job>.path``     for a job with ``webhook = true``: starts it when the webhook
  receiver touches ``webhook/triggers/<job>``.
- ``mes-<p>.net.<job>.service``  for a job that lists network hosts: runs just before each
  start of the job, as root, looks the hosts up again, writes the job's own ``/etc/hosts``
  (``/run/mes-<p>-net/<job>/hosts``, bound over ``/etc/hosts`` in the job's view) and sets the
  job's ``IPAddressAllow=`` to exactly those addresses (the job itself has ``IPAddressDeny=any``
  and no access to the system's name service, so it needs no DNS server at all). It runs as
  root because only root may change another unit's settings; it takes nothing from the
  project's files (the hosts are written into this file) and is sandboxed too.
- ``mes-<p>.ui-refresh.service`` rebuilds the dashboard data (``easy-ui data build``) after the
  jobs listed in ``[ui] refresh_after`` succeed.
- ``mes-<p>.webhook.socket`` / ``.service``  the signed webhook receiver (see ``mes.webhook``).
- ``mes-<p>.gate-<level>.socket`` / ``mes-<p>.gate-<level>@.service`` for ``read``, ``write`` and
  ``deploy``: the gate hands each request to a fresh instance (``Accept=yes``), so every request
  runs in its own sandbox under the project's account. The sockets live in
  ``/run/mes-<p>-gate/``, a folder no job, dashboard refresh, webhook receiver or gate request
  can see (``InaccessiblePaths=``); each instance also checks that the connecting process is an
  SSH session of the project's account, never a job (see ``mes.gate``).
- ``mes-<p>.selftest.path`` / ``.service``  ``mes server selftest``: a probe that runs with
  exactly a job's sandbox and reports what it could and could not reach.
- ``mes-<p>-web.socket`` / ``.service``  the dashboard server (easy-ui), rendered here from mes's
  own template (``render_web``); easy-ui only supplies checked parameters.

A job with ``after = "<upstream>"`` is started by the upstream service through ``OnSuccess=``.
After every run, ``ExecStopPost=+`` runs ``mes jobs record`` as root, outside the sandbox, with
only the project and job names as arguments and a clean environment
(``/usr/bin/env -i PATH=/usr/bin:/bin``, Python in isolated mode ``-I``), so nothing the project
can write (its ``HOME``, its working folder, ``PYTHON*`` settings) reaches root. It appends the
run to ``log/runs.jsonl``, a folder that only root can change, so a job cannot rewrite its own
history. The network helper runs the same way.

What each unit sees of ``/var/lib/mes``: ``TemporaryFileSystem=/var/lib/mes:ro`` hides every
project, then only the folders the unit needs are bound back (read-only unless it writes them).
Other projects' folders, the sign-in folder (``auth``), the gate's own folder and the webhook
receiver's folder are therefore not there at all for project code.

Safety of the generated text
----------------------------
Every value written into a unit is checked or escaped here, so a project cannot add directives
of its own: names must match their strict patterns; no value may contain control characters
(a newline would start a new directive); ``ExecStart`` words are double-quoted with ``\\``,
``"``, ``%`` and ``$`` escaped; paths must be absolute, normalised and made only of
``[A-Za-z0-9._/@+=,~-]``; schedules, memory sizes, timeouts, host names and web addresses are
checked against tight patterns. Anything that does not pass raises ``mes.errors.Refused`` (exit 5).

Public API
----------
``UNIT_PREFIX``, ``SLICE``, ``RANDOM_DELAY_SEC``, ``JOB_ALLOW_LEVEL`` (``"write"``),
``MARKER_PROJECT``, ``MARKER_JOB``, ``MARKER_KIND``, ``HEADER``, ``UNIT_NAME_RE``,
``JOB_UNIT_RE``, ``RUN_LOG_FILE``, ``DEFAULT_MES_PYTHON``, ``ROOT_ENV``, ``DEPLOY_DENY``.
``job_unit(project, job, suffix="service")``, ``net_unit(project, job)``,
``ui_refresh_unit(project)``, ``webhook_units(project) -> (socket, service)``,
``webhook_socket_path(project) -> str``, ``gate_units(project, level) -> (socket, template)``,
``gate_socket_dir(project) -> str``, ``gate_socket_path(project, level) -> str``,
``net_run_dir(project)``, ``net_hosts_file(project, job)``, ``selftest_units(project)``,
``web_unit_names(project)``, ``web_socket_path(project)``.
``quote_exec_word(value)``, ``exec_line(argv)``, ``env_assignment(key, value)``,
``sandbox_path(path)``, ``line_path(path)``, ``check_tool_path(path, what)``,
``root_command(mes_python, *args) -> list[str]``  how root runs mes (clean environment, ``-I``).
``render_job_service(project, job, *, layout, mes, mes_python) -> str``
``render_job_timer(project, job) -> str``, ``render_job_path(project, job, *, layout) -> str``
``render_net_service(project, job, *, mes_python) -> str``
``render_ui_refresh(project, *, layout, easy_ui) -> str``
``render_webhook(project, *, layout, mes) -> dict[str, str]``
``render_gate(project, *, layout, mes) -> dict[str, str]``  the gate's per-request sandboxes.
``render_selftest(project, *, layout, mes) -> dict[str, str]``
``check_web_params(answer, *, layout, easy_ui) -> dict``  what easy-ui says, checked.
``check_origin(value) -> bool``, ``web_exec_start(easy_ui, ui, auth, origins) -> list[str]``.
``render_web(project, *, layout, params) -> dict[str, str]``  the dashboard server's units.
``render_project(project, *, layout, mes, mes_python=DEFAULT_MES_PYTHON, easy_ui=None) -> dict[str, str]``
    File name -> text for every mes unit of the project (not the dashboard server's units,
    which ``render_web`` makes once easy-ui's parameters are known).
``read_markers(text) -> (project | None, job | None)``
``SANDBOX_LINES``  the sandbox settings shared by every unit that runs project code.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any

from mes.errors import Refused
from mes.manifest import HOST_RE, JOB_NAME_RE, PROJECT_NAME_RE, Job, Project, is_host_name

UNIT_PREFIX = "mes"
SLICE = "mes.slice"
RANDOM_DELAY_SEC = 120
TIMEOUT_GRACE_SEC = 30
JOB_ALLOW_LEVEL = "write"
RUN_LOG_FILE = "runs.jsonl"
MARKER_PROJECT = "# mes-project: "
MARKER_JOB = "# mes-job: "
MARKER_KIND = "# mes-unit: "
HEADER = ("# Made by my-easy-software (mes). Do not edit: run 'mes server plan' again and the script it "
          "writes instead.")
UNIT_NAME_RE = re.compile(r"^mes-[a-z][a-z0-9-]{1,30}\.(?:job\.[a-z][a-z0-9-]{0,30}\.(?:service|timer|path)|"
                          r"net\.[a-z][a-z0-9-]{0,30}\.service|ui-refresh\.service|webhook\.(?:socket|service)|"
                          r"selftest\.(?:path|service)|gate-(?:read|write|deploy)(?:\.socket|@\.service))\Z")
GATE_LEVELS = ("read", "write", "deploy")
JOB_UNIT_RE = re.compile(r"^mes-(?P<project>[a-z][a-z0-9-]{1,30})\.job\.(?P<job>[a-z][a-z0-9-]{0,30})\.service\Z")
# Where root's own copy of mes runs from: the Python of the root-owned tool install
# (UV_TOOL_DIR=/opt/mes/tools, see: mes guide server). 'mes server plan --mes-python' changes it.
DEFAULT_MES_PYTHON = "/opt/mes/tools/my-easy-software/bin/python"
# Everything root runs for a project starts with an empty environment: nothing the project's
# account can write (HOME, PYTHONPATH, XDG folders, the working folder) reaches root.
ROOT_ENV = ("/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8")
# The deploy sandbox needs the package index, but nothing on this computer or the local network.
DEPLOY_DENY = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
               "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4", "::/128", "::1/128", "fc00::/7", "fe80::/10",
               "ff00::/8")
WEB_SOCKET_DIR = "/run/mes"
WEB_MEMORY_DEFAULT = "256M"
MAX_ORIGINS = 8

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\u0085  ]")
_SAFE_PATH_RE = re.compile(r"/[A-Za-z0-9._/@+=,~-]*")
_SCHEDULE_RE = re.compile(r"[A-Za-z0-9*:,./~+_@-]+(?: [A-Za-z0-9*:,./~+_@-]+)*")
_MEMORY_RE = re.compile(r"[1-9][0-9]{0,6}[KMG]")
_TIMEOUT_RE = re.compile(r"([1-9][0-9]{0,6})([smh])")
_ENV_KEY_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_ORIGIN_RE = re.compile(r"https://(?P<host>[a-z0-9.-]{1,253})(?::(?P<port>[1-9][0-9]{0,4}))?")
_SECONDS = {"s": 1, "m": 60, "h": 3600}
# Folders a tool run by root or by a project account must never be in: other accounts can write
# there, or the units hide them (ProtectHome=yes).
UNSAFE_TOOL_ROOTS = ("/tmp", "/var/tmp", "/dev/shm", "/run/shm", "/home", "/root", "/run/user")

# The sandbox shared by every unit that runs the project's own code (jobs, the dashboard data
# refresh, the webhook receiver, the gate's requests). Network settings and folders are added
# per unit.
SANDBOX_LINES = (
    "UMask=0077",
    "NoNewPrivileges=yes",
    "CapabilityBoundingSet=",
    "AmbientCapabilities=",
    "ProtectSystem=strict",
    "ProtectHome=yes",
    "PrivateTmp=yes",
    "PrivateDevices=yes",
    "DevicePolicy=closed",
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes",
    "ProtectClock=yes",
    "ProtectHostname=yes",
    "ProtectProc=invisible",
    "ProcSubset=pid",
    "KeyringMode=private",
    "RestrictSUIDSGID=yes",
    "LockPersonality=yes",
    "RestrictRealtime=yes",
    "RestrictNamespaces=yes",
    "RemoveIPC=yes",
    "SystemCallArchitectures=native",
    "SystemCallFilter=@system-service",
    "SystemCallFilter=~@privileged @resources",
    "SystemCallErrorNumber=EPERM",
    "LimitCORE=0",
    "TasksMax=256",
)
# The deploy sandbox runs uv: it may set ownership of its own files (unpacking Python) and raise
# its own open-file limit, so these two groups come back; nothing else is relaxed. To be
# confirmed on a real Linux server (mes server selftest reports it).
DEPLOY_SYSCALL_LINES = (
    "SystemCallFilter=@system-service",
    "SystemCallFilter=~@privileged",
    "SystemCallFilter=@chown",
)


def _refuse(what: str, value: object, why: str, hint: str) -> Refused:
    return Refused(
        f"mes will not write {what} {str(value)[:200]!r} into a system service file, because {why} "
        "Such a value could change how the project is protected, so nothing was rendered.",
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
            "Remove line breaks and control characters from this value.",
        )
    return value


def _check_project(name: str) -> str:
    if not isinstance(name, str) or not PROJECT_NAME_RE.fullmatch(name):
        raise _refuse("the project name", name, "it is not a simple name.",
                      "Project names use 2 to 31 lowercase letters, digits and dashes, starting with a letter.")
    return name


def _check_job(job: str) -> str:
    if not isinstance(job, str) or not JOB_NAME_RE.fullmatch(job):
        raise _refuse("the job name", job, "it is not a simple name.",
                      "Job names use lowercase letters, digits and dashes, starting with a letter.")
    return job


def job_unit(project: str, job: str, suffix: str = "service") -> str:
    if suffix not in ("service", "timer", "path"):
        raise ValueError("suffix must be 'service', 'timer' or 'path'")
    return f"{UNIT_PREFIX}-{_check_project(project)}.job.{_check_job(job)}.{suffix}"


def net_unit(project: str, job: str) -> str:
    return f"{UNIT_PREFIX}-{_check_project(project)}.net.{_check_job(job)}.service"


def ui_refresh_unit(project: str) -> str:
    return f"{UNIT_PREFIX}-{_check_project(project)}.ui-refresh.service"


def webhook_units(project: str) -> tuple[str, str]:
    p = _check_project(project)
    return f"{UNIT_PREFIX}-{p}.webhook.socket", f"{UNIT_PREFIX}-{p}.webhook.service"


def selftest_units(project: str) -> tuple[str, str]:
    p = _check_project(project)
    return f"{UNIT_PREFIX}-{p}.selftest.path", f"{UNIT_PREFIX}-{p}.selftest.service"


def webhook_socket_path(project: str) -> str:
    return f"/run/mes-{_check_project(project)}-webhook.sock"


def gate_units(project: str, level: str) -> tuple[str, str]:
    if level not in GATE_LEVELS:
        raise ValueError("level must be read, write or deploy")
    p = _check_project(project)
    return f"{UNIT_PREFIX}-{p}.gate-{level}.socket", f"{UNIT_PREFIX}-{p}.gate-{level}@.service"


def gate_socket_dir(project: str) -> str:
    """The folder of the gate's sockets: hidden from every unit that runs project code."""
    return f"/run/mes-{_check_project(project)}-gate"


def gate_socket_path(project: str, level: str) -> str:
    if level not in GATE_LEVELS:
        raise ValueError("level must be read, write or deploy")
    return f"{gate_socket_dir(project)}/{level}.sock"


def net_run_dir(project: str) -> str:
    return f"/run/mes-{_check_project(project)}-net"


def net_hosts_file(project: str, job: str) -> str:
    return f"{net_run_dir(project)}/{_check_job(job)}/hosts"


def web_unit_names(project: str) -> tuple[str, str]:
    p = _check_project(project)
    return f"mes-{p}-web.socket", f"mes-{p}-web.service"


def web_socket_path(project: str) -> str:
    return f"{WEB_SOCKET_DIR}/{_check_project(project)}-web.sock"


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
                      "Use the full path of the program.")
    return " ".join(quote_exec_word(w) for w in words)


def env_assignment(key: str, value: str) -> str:
    if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key):
        raise _refuse("the setting name", key, "it is not a simple upper-case name.", "Use A-Z, 0-9 and _.")
    _no_control(f"the value of {key}", value)
    escaped = f"{key}={value}".replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
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
    """A path made only of safe characters, so it needs no quoting in any directive."""
    norm = _norm_abs("the folder", path)
    if not _SAFE_PATH_RE.fullmatch(norm):
        raise _refuse(
            "the folder",
            norm,
            "it contains spaces, quotes, backslashes or other characters that the sandbox settings "
            "cannot hold safely.",
            "Use paths made of letters, digits, '.', '_', '-' and '/'.",
        )
    return norm


def line_path(path: str | os.PathLike[str]) -> str:
    return sandbox_path(path).replace("%", "%%")


def check_tool_path(path: str | os.PathLike[str], what: str) -> str:
    """The full path of mes, its Python, uv or easy-ui on the server: safe characters, outside
    home and shared temporary folders (the units hide homes, and anyone can write to /tmp)."""
    p = sandbox_path(path)
    if any(p == r or p.startswith(r + "/") for r in UNSAFE_TOOL_ROOTS):
        raise _refuse(
            what, p,
            "it is inside a home folder or a shared temporary folder: the services cannot see home folders, "
            "and other accounts can change files in temporary folders.",
            "Install the tool where only the administrator can change it, for example /usr/local/bin or /opt "
            "(see: mes guide server).",
        )
    return p


def root_command(mes_python: str, *args: str) -> list[str]:
    """How root runs mes for a project: an empty environment and Python's isolated mode, so no
    HOME, PYTHON* setting, user site-packages or working folder of the project is used."""
    py = check_tool_path(mes_python, "the Python of mes")
    return [*ROOT_ENV, py, "-I", "-m", "mes", *args]


def _schedule(job: Job) -> str:
    value = _no_control("the schedule", job.schedule)
    if len(value) > 200 or not _SCHEDULE_RE.fullmatch(value):
        raise _refuse("the schedule", value, "it is not a plain calendar expression.",
                      'Use a systemd calendar expression such as "daily" or "*-*-* 06:00:00".')
    return value


def _memory(job: Job) -> str:
    if not isinstance(job.memory, str) or not _MEMORY_RE.fullmatch(job.memory):
        raise _refuse("the memory limit", job.memory, "it is not a size such as 256M.",
                      'Write memory = "256M" in project.toml.')
    return job.memory


def _timeout_seconds(job: Job) -> int:
    m = _TIMEOUT_RE.fullmatch(job.timeout) if isinstance(job.timeout, str) else None
    if not m:
        raise _refuse("the time limit", job.timeout, "it is not a duration such as 15m.",
                      'Write timeout = "15m" in project.toml.')
    return int(m.group(1)) * _SECONDS[m.group(2)]


def check_hosts(hosts: Sequence[str]) -> list[str]:
    out = []
    for h in hosts:
        if not isinstance(h, str) or not HOST_RE.fullmatch(h) or not is_host_name(h):
            raise _refuse("the network host", h, "it is not a plain host name (numbers in any spelling are not names).",
                          'List only host names, for example network = ["api.example.com"].')
        out.append(h)
    return out


def _writes(job: Job) -> list[str]:
    out = []
    for w in job.writes:
        if w not in ("data", "state"):
            raise _refuse("the writable folder", w, 'only "data" and "state" exist.',
                          'Use writes = ["data", "state"] or a part of it.')
        out.append(w)
    return out


# --------------------------------------------------------------------------- rendering


def _header(project: str, kind: str, job: str | None = None, extra: Sequence[str] = ()) -> list[str]:
    lines = [HEADER, MARKER_PROJECT + _check_project(project), MARKER_KIND + kind]
    if job is not None:
        lines.append(MARKER_JOB + _check_job(job))
    lines += [f"# {_no_control('a comment', e)}" for e in extra]
    return lines


def _project_env(project: Project, layout, *, home: str | None = None) -> list[str]:
    data = sandbox_path(layout.data)
    state = sandbox_path(layout.state)
    return [
        f"Environment={env_assignment('MES_PROJECT', _check_project(project.name))}",
        f"Environment={env_assignment('MES_DATA_DIR', data)}",
        f"Environment={env_assignment('MES_STATE_DIR', state)}",
        f"Environment={env_assignment('MES_STORE_DIR', sandbox_path(layout.store))}",
        f"Environment={env_assignment('HOME', sandbox_path(home) if home else state)}",
        f"Environment={env_assignment('LANG', 'C.UTF-8')}",
        f"Environment={env_assignment('PYTHONDONTWRITEBYTECODE', '1')}",
    ]


def _view(layout, *, ro: Sequence[Any] = (), rw: Sequence[Any] = ()) -> list[str]:
    """What the unit sees of the server folder: nothing but these folders of its own project."""
    lines = ["# Of all the projects' folders, only these of this project are there (read-only unless listed",
             "# as writable); other projects, the sign-in folder and the gate's own folder are not.",
             f"TemporaryFileSystem={line_path(layout.root)}:ro"]
    if ro:
        lines.append("BindReadOnlyPaths=" + " ".join(line_path(p) for p in ro))
    if rw:
        lines.append("BindPaths=" + " ".join(line_path(p) for p in rw))
        lines.append("ReadWritePaths=" + " ".join(line_path(p) for p in rw))
    return lines


def _hidden(project: Project, layout, *extra: Any) -> str:
    paths_ = [gate_socket_dir(project.name), layout.webhook_key, *extra]
    return "InaccessiblePaths=" + " ".join(f"-{line_path(p)}" for p in paths_)


def _program(project: Project, layout, app: str) -> str:
    spec = project.app(app)
    return sandbox_path(f"{sandbox_path(layout.current_project)}/.venv/bin/{spec.cli}")


def render_job_service(project: Project, job: Job, *, layout, mes: str,
                       mes_python: str = DEFAULT_MES_PYTHON, selftest: bool = False) -> str:
    p = _check_project(project.name)
    name = _check_job(job.name)
    hosts = check_hosts(job.network)
    local = check_hosts(getattr(job, "local_network", ()))
    all_hosts = list(dict.fromkeys(hosts + local))
    writes = _writes(job)
    timeout = _timeout_seconds(job)
    mes = check_tool_path(mes, "the path of mes")
    root = sandbox_path(layout.root)
    if selftest:
        start = exec_line([mes, "server", "selftest-probe", p, "--context", "job", "--server-root", root])
        record = None
        kind = "selftest"
    else:
        start = exec_line([_program(project, layout, job.app), "--json", *job.argv])
        record = exec_line(root_command(mes_python, "jobs", "record", "--project", p, "--server-root", root, name))
        kind = "job"
    after_success = [] if selftest else [job_unit(p, d.name) for d in project.dependents(job.name)]
    if not selftest and project.ui is not None and job.name in project.ui.refresh_after:
        after_success.append(ui_refresh_unit(p))

    if all_hosts:
        net_comment = (f"network: only {', '.join(all_hosts)} (their addresses are looked up again before each run"
                       + (f"; {', '.join(local)} may be on the local network" if local else "") + ")")
    else:
        net_comment = "network: none"
    lines = _header(p, kind, None if selftest else name, [net_comment])
    lines += ["", "[Unit]",
              f"Description=mes self-test probe of project {p} (a job's sandbox)" if selftest
              else f"Description=mes job {name} of project {p}"]
    if all_hosts:
        lines += [f"Requires={net_unit(p, name)}", f"After={net_unit(p, name)}"]
    if after_success:
        lines.append("OnSuccess=" + " ".join(after_success))
    lines += [
        "",
        "[Service]",
        "Type=oneshot",
        f"User={layout.account}",
        f"Group={layout.account}",
        f"ExecStart={start}",
    ]
    if record:
        lines += ["# Root records the run with an empty environment and Python's isolated mode: nothing the",
                  "# job can write reaches it.",
                  f"ExecStopPost=+{record}"]
    lines += [
        f"WorkingDirectory={line_path(layout.current_project)}",
        *_project_env(project, layout),
        f"Environment={env_assignment('MES_ALLOW', JOB_ALLOW_LEVEL)}",
        f"Environment={env_assignment('MES_JOB', name)}",
        f"Slice={SLICE}",
        f"MemoryMax={_memory(job)}",
        "MemorySwapMax=0",
        f"TimeoutStartSec={timeout}",
        *SANDBOX_LINES,
    ]
    folders = {"data": [layout.data, layout.store], "state": [layout.state]}
    rw = [f for w in writes for f in folders[w]]
    ro = [layout.code] + [f for w in ("data", "state") if w not in writes for f in folders[w]]
    lines += _view(layout, ro=ro, rw=rw)
    if all_hosts:
        lines += [
            "# The job's own /etc/hosts, written just before the run: the only names it can look up. It has",
            "# no DNS server and no access to the system's name service.",
            f"BindReadOnlyPaths={line_path(net_hosts_file(p, name))}:/etc/hosts",
            _hidden(project, layout, "/run/systemd/resolve", "/run/dbus"),
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "IPAddressDeny=any",
        ]
    else:
        lines += [_hidden(project, layout), "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX",
                  "IPAddressDeny=any"]
    lines.append("")
    return "\n".join(lines)


def render_job_timer(project: Project, job: Job) -> str:
    p = _check_project(project.name)
    name = _check_job(job.name)
    if job.trigger != "schedule" or job.schedule is None:
        raise ValueError(f"job {name!r} has no schedule")
    lines = _header(p, "timer", name) + [
        "",
        "[Unit]",
        f"Description=Schedule of mes job {name} of project {p}",
        "",
        "[Timer]",
        f"OnCalendar={_schedule(job)}",
        "Persistent=true",
        f"RandomizedDelaySec={RANDOM_DELAY_SEC}",
        f"Unit={job_unit(p, name)}",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ]
    return "\n".join(lines)


def render_job_path(project: Project, job: Job, *, layout) -> str:
    p = _check_project(project.name)
    name = _check_job(job.name)
    trigger = sandbox_path(f"{sandbox_path(layout.triggers)}/{name}")
    lines = _header(p, "webhook-trigger", name) + [
        "",
        "[Unit]",
        f"Description=Start mes job {name} of project {p} when its signed webhook arrives",
        "",
        "[Path]",
        f"PathModified={trigger.replace('%', '%%')}",
        f"Unit={job_unit(p, name)}",
        "",
        "[Install]",
        "WantedBy=paths.target",
        "",
    ]
    return "\n".join(lines)


def render_net_service(project: Project, job: Job, *, mes_python: str = DEFAULT_MES_PYTHON, layout=None) -> str:
    p = _check_project(project.name)
    name = _check_job(job.name)
    hosts = check_hosts(job.network)
    local = check_hosts(getattr(job, "local_network", ()))
    if not hosts and not local:
        raise ValueError(f"job {name!r} lists no hosts")
    argv = ["jobs", "allow-network", "--unit", job_unit(p, name), "--hosts-file", net_hosts_file(p, name)]
    for h in local:
        argv += ["--local", h]
    start = exec_line(root_command(mes_python, *argv, "--", *dict.fromkeys(hosts + local)))
    lines = _header(p, "net", name, [
        "Runs as root just before each start of the job, with an empty environment: it looks up the",
        "job's hosts, writes the job's own hosts file and lets the job reach only those addresses.",
        "Addresses on this computer or the local network are dropped, except for hosts listed in",
        "local_network. It reads nothing from the project.",
    ]) + [
        "",
        "[Unit]",
        f"Description=Allow the network hosts of mes job {name} of project {p}",
        f"Before={job_unit(p, name)}",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={start}",
        "WorkingDirectory=/",
        f"RuntimeDirectory=mes-{p}-net/{name}",
        "RuntimeDirectoryMode=0755",
        "RuntimeDirectoryPreserve=yes",
        "TimeoutStartSec=60",
        "UMask=0022",
        "NoNewPrivileges=yes",
        "# Only the right to change unit settings (systemd asks for CAP_SYS_ADMIN for that).",
        "CapabilityBoundingSet=CAP_SYS_ADMIN",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "RestrictNamespaces=yes",
        "# It needs nothing of the projects' folders.",
        f"InaccessiblePaths=-{line_path(layout.root)}" if layout is not None else "InaccessiblePaths=-/var/lib/mes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service",
        "",
    ]
    return "\n".join(lines)


def render_ui_refresh(project: Project, *, layout, easy_ui: str) -> str:
    p = _check_project(project.name)
    easy_ui = check_tool_path(easy_ui, "the path of easy-ui")
    start = exec_line([easy_ui, "--json", "--project-dir", sandbox_path(layout.current_project), "data", "build"])
    lines = _header(p, "ui-refresh") + [
        "",
        "[Unit]",
        f"Description=Rebuild the dashboard data of project {p}",
        "",
        "[Service]",
        "Type=oneshot",
        f"User={layout.account}",
        f"Group={layout.account}",
        f"ExecStart={start}",
        f"WorkingDirectory={line_path(layout.current_project)}",
        *_project_env(project, layout),
        # easy-ui finds <root>/<project>/ui and /data through MES_ROOT (the server layout).
        f"Environment={env_assignment('MES_ROOT', sandbox_path(layout.root))}",
        f"Environment={env_assignment('MES_ALLOW', 'read')}",
        f"Slice={SLICE}",
        "MemoryMax=512M",
        "TimeoutStartSec=600",
        *[line for line in SANDBOX_LINES if not line.startswith("UMask=")],
        "# The dashboard server's account reads ui/ through its group, so new files are group-readable.",
        "UMask=0027",
        *_view(layout, ro=[layout.code, layout.data, layout.state], rw=[layout.ui]),
        _hidden(project, layout),
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "IPAddressDeny=any",
        "",
    ]
    return "\n".join(lines)


def render_webhook(project: Project, *, layout, mes: str) -> dict[str, str]:
    p = _check_project(project.name)
    mes = check_tool_path(mes, "the path of mes")
    socket_name, service_name = webhook_units(p)
    socket = _header(p, "webhook", None, [
        "The socket belongs to root (mode 0600): only a local proxy running as root, such as",
        "'tailscale serve', can pass requests to it. See: mes guide webhooks",
    ]) + [
        "",
        "[Unit]",
        f"Description=Signed webhooks of project {p} (socket)",
        "",
        "[Socket]",
        f"ListenStream={webhook_socket_path(p)}",
        "SocketUser=root",
        "SocketGroup=root",
        "SocketMode=0600",
        "Accept=no",
        f"Service={service_name}",
        "",
        "[Install]",
        "WantedBy=sockets.target",
        "",
    ]
    start = exec_line([mes, "webhook", "serve", "--project", p, "--server-root", sandbox_path(layout.root)])
    service = _header(p, "webhook", None, [
        "It sees only etc/ (the secret and the list of webhook jobs, both written by root) and its own",
        "webhook/ folder (the replay record and the trigger notes), which no job can write.",
    ]) + [
        "",
        "[Unit]",
        f"Description=Signed webhooks of project {p}",
        f"Requires={socket_name}",
        f"After={socket_name}",
        "",
        "[Service]",
        "Type=simple",
        f"User={layout.account}",
        f"Group={layout.account}",
        f"ExecStart={start}",
        "WorkingDirectory=/",
        *_project_env(project, layout),
        f"Slice={SLICE}",
        "MemoryMax=128M",
        "RuntimeMaxSec=3600",
        *SANDBOX_LINES,
        *_view(layout, ro=[layout.etc], rw=[layout.webhook]),
        f"InaccessiblePaths=-{line_path(gate_socket_dir(p))}",
        "# No network of its own: requests arrive only through the socket.",
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "IPAddressDeny=any",
        "",
    ]
    return {socket_name: "\n".join(socket), service_name: "\n".join(service)}


def render_gate(project: Project, *, layout, mes: str, uv: str | None = None,
                easy_ui: str | None = None) -> dict[str, str]:
    """Per-request sandboxes for the gate: one socket per level (only the project's account may
    connect, from an SSH session) and a template service that systemd starts once per connection."""
    p = _check_project(project.name)
    mes = check_tool_path(mes, "the path of mes")
    root = sandbox_path(layout.root)
    files: dict[str, str] = {}
    for level in GATE_LEVELS:
        socket_name, service_name = gate_units(p, level)
        socket = _header(p, f"gate-{level}", None, [
            f"Each connection gets its own sandboxed instance of {service_name}.",
            f"Only the account {layout.account} can connect, and each instance accepts only an SSH session of",
            "that account (never a job). No unit that runs project code can see this socket's folder.",
        ]) + [
            "",
            "[Unit]",
            f"Description=Sandboxed {level} requests of the mes gate for project {p} (socket)",
            "",
            "[Socket]",
            f"ListenStream={gate_socket_path(p, level)}",
            f"SocketUser={layout.account}",
            f"SocketGroup={layout.account}",
            "SocketMode=0600",
            "DirectoryMode=0755",
            "Accept=yes",
            f"MaxConnections={1 if level == 'deploy' else 4}",
            "",
            "[Install]",
            "WantedBy=sockets.target",
            "",
        ]
        start = exec_line([mes, "gate", "child", "--project", p, "--server-root", root, "--level", level])
        lines = _header(p, f"gate-{level}") + [
            "",
            "[Unit]",
            f"Description=One {level} request of the mes gate for project {p}",
            "",
            "[Service]",
            "Type=simple",
            f"User={layout.account}",
            f"Group={layout.account}",
            f"ExecStart={start}",
            "StandardInput=socket",
            "StandardOutput=socket",
            "StandardError=journal",
            "WorkingDirectory=/",
            *_project_env(project, layout, home="/nonexistent" if level == "deploy" else None),
            f"Slice={SLICE}",
            "MemoryMax=2G",
            "MemorySwapMax=0",
            f"RuntimeMaxSec={'1800' if level == 'deploy' else '3700'}",
        ]
        if level == "deploy":
            lines += [
                "# The deploy tools' own folders are in code/, which jobs and the write gate see read-only.",
                f"Environment={env_assignment('UV_CACHE_DIR', sandbox_path(layout.uv_cache))}",
                f"Environment={env_assignment('UV_PYTHON_INSTALL_DIR', sandbox_path(layout.uv_python))}",
                f"Environment={env_assignment('UV_NO_CONFIG', '1')}",
                f"Environment={env_assignment('UV_LINK_MODE', 'copy')}",
                f"Environment={env_assignment('PYTHONNOUSERSITE', '1')}",
                f"Environment={env_assignment('MES_ROOT', sandbox_path(layout.root))}",
                *[line for line in SANDBOX_LINES if not line.startswith(("UMask=", "SystemCallFilter="))],
                *DEPLOY_SYSCALL_LINES,
                "# The dashboard server's account reads ui/ through its group, so new files are group-readable.",
                "UMask=0027",
                *_view(layout, ro=[layout.etc, layout.data], rw=[layout.code, layout.ui]),
                _hidden(project, layout, layout.incoming),
                "# uv needs the package index, but nothing on this computer or the local network.",
                "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
                "IPAddressDeny=" + " ".join(DEPLOY_DENY),
                "",
            ]
        else:
            lines += list(SANDBOX_LINES)
            if level == "write":
                lines += _view(layout, ro=[layout.code], rw=[layout.data, layout.state, layout.store])
            else:
                lines.append("# Read requests see the data and state folders read-only.")
                lines += _view(layout, ro=[layout.code, layout.data, layout.state, layout.store])
            lines += [_hidden(project, layout, layout.incoming), "PrivateNetwork=yes",
                      "RestrictAddressFamilies=AF_UNIX", "IPAddressDeny=any", ""]
        files[socket_name] = "\n".join(socket)
        files[service_name] = "\n".join(lines)
    return files


def render_selftest(project: Project, *, layout, mes: str) -> dict[str, str]:
    """``mes server selftest``: the gate asks for a run by touching ``gate/selftest-request``
    (a folder only the gate itself can write); the probe runs with a job's exact sandbox (no
    network, data and state writable) and leaves its answer in ``gate/selftest/job.json``, the only
    part of gate/ it sees."""
    p = _check_project(project.name)
    path_name, service_name = selftest_units(p)
    probe = Job(name="selftest", run="selftest", app=next(iter(project.apps), p), argv=("selftest",),
                trigger="manual", memory="128M", timeout="2m", network=(), writes=("data", "state"))
    service = render_job_service(project, probe, layout=layout, mes=mes, selftest=True)
    # Its answer goes to gate/selftest/, which no job can see, so no job can forge a pass.
    answer = sandbox_path(f"{sandbox_path(layout.gate)}/selftest")
    service = service.replace("\nInaccessiblePaths=", f"\nBindPaths=-{answer}\nInaccessiblePaths=", 1)
    request = sandbox_path(f"{sandbox_path(layout.gate)}/selftest-request")
    path = _header(p, "selftest", None, ["Started by 'mes server selftest' through the gate."]) + [
        "",
        "[Unit]",
        f"Description=Start the mes self-test probe of project {p}",
        "",
        "[Path]",
        f"PathModified={request.replace('%', '%%')}",
        f"Unit={service_name}",
        "",
        "[Install]",
        "WantedBy=paths.target",
        "",
    ]
    return {path_name: "\n".join(path), service_name: service}


# --------------------------------------------------------------------------- the dashboard server


def _bad_web(what: str, value: Any) -> Refused:
    return Refused(
        f"easy-ui gave a value for the dashboard server that mes does not accept: {what} "
        f"({str(value)[:120]!r}). mes renders the dashboard server's units itself and only takes plain, "
        "checked values from easy-ui, so nothing was written.",
        hint="Update easy-ui (and mes), then run 'mes server plan' again.",
        code="unsafe_web_unit",
    )


def check_web_params(answer: Any, *, layout, easy_ui: str, origins: Sequence[str] | None = None) -> dict[str, Any]:
    """The parameters ``easy-ui units --json`` gives for the dashboard server, each checked
    against a strict pattern and against what mes itself expects for this project. Nothing else
    of easy-ui's answer (in particular no unit text) is ever used."""
    if not isinstance(answer, dict):
        raise _bad_web("the answer", type(answer).__name__)
    p = _check_project(layout.project)
    root = sandbox_path(layout.root)
    accounts = answer.get("accounts")
    if accounts != {"project": layout.account, "web": layout.web_account}:
        raise _bad_web("the accounts", accounts)
    for key, want in (("ui_dir", f"{root}/{p}/ui"), ("auth_dir", f"{root}/{p}/auth"),
                      ("socket", web_socket_path(p))):
        if answer.get(key) != want:
            raise _bad_web(key, answer.get(key))
    if answer.get("root", root) != root:
        raise _bad_web("the folder of all projects", answer.get("root"))
    if answer.get("unit_names", {"socket": web_unit_names(p)[0], "service": web_unit_names(p)[1]}) != \
            {"socket": web_unit_names(p)[0], "service": web_unit_names(p)[1]}:
        raise _bad_web("the unit names", answer.get("unit_names"))
    if answer.get("socket_owner", "root") != "root":
        raise _bad_web("the socket's owner", answer.get("socket_owner"))
    exe = answer.get("exec", easy_ui)
    if exe != easy_ui:
        raise _bad_web("the program", exe)
    memory = answer.get("memory_max", WEB_MEMORY_DEFAULT)
    if not isinstance(memory, str) or not _MEMORY_RE.fullmatch(memory):
        raise _bad_web("the memory limit", memory)
    requested = list(origins) if origins is not None else None
    origins = answer.get("origins")
    if not isinstance(origins, list) or not 1 <= len(origins) <= MAX_ORIGINS:
        raise _bad_web("the web addresses", origins)
    for o in origins:
        if not check_origin(o):
            raise _bad_web("a web address", o)
    origins = list(dict.fromkeys(origins))
    if requested is not None and origins != list(dict.fromkeys(requested)):
        raise _bad_web("the web addresses (not the ones asked for)", origins)
    if "exec_start" in answer and answer["exec_start"] != web_exec_start(easy_ui, f"{root}/{p}/ui",
                                                                          f"{root}/{p}/auth", origins):
        raise _bad_web("the command line", answer.get("exec_start"))
    return {"exec": check_tool_path(easy_ui, "the path of easy-ui"), "ui_dir": f"{root}/{p}/ui",
            "auth_dir": f"{root}/{p}/auth", "socket": web_socket_path(p), "memory_max": memory,
            "origins": origins}


def check_origin(value: Any) -> bool:
    """``https://NAME[:PORT]``: exactly the address the dashboards are opened with."""
    m = _ORIGIN_RE.fullmatch(value) if isinstance(value, str) else None
    return bool(m and HOST_RE.fullmatch(m.group("host")) and (not m.group("port") or int(m.group("port")) <= 65535))


def web_exec_start(easy_ui: str, ui: str, auth: str, origins: Sequence[str]) -> list[str]:
    argv = [easy_ui, "serve", "--ui-dir", ui, "--auth-dir", auth]
    for o in origins:
        argv += ["--origin", o]
    return argv


def render_web(project: Project, *, layout, params: dict[str, Any]) -> dict[str, str]:
    """The dashboard server's socket and service, from mes's own template."""
    p = _check_project(project.name)
    socket_name, service_name = web_unit_names(p)
    exe = check_tool_path(params["exec"], "the path of easy-ui")
    ui, auth = sandbox_path(params["ui_dir"]), sandbox_path(params["auth_dir"])
    if ui != sandbox_path(layout.ui) or auth != sandbox_path(layout.auth) or params["socket"] != web_socket_path(p):
        raise _bad_web("the folders", params)
    if not _MEMORY_RE.fullmatch(params["memory_max"]):
        raise _bad_web("the memory limit", params["memory_max"])
    for o in params["origins"]:
        if not check_origin(o):
            raise _bad_web("a web address", o)
    argv = web_exec_start(exe, ui, auth, params["origins"])
    socket = _header(p, "web", None, [
        "The dashboard server's socket belongs to root (mode 0600): only a local proxy running as root,",
        "such as 'tailscale serve', can pass requests to it. See: mes guide setup",
    ]) + [
        "",
        "[Unit]",
        f"Description=Dashboards of project {p} (socket)",
        "",
        "[Socket]",
        f"ListenStream={web_socket_path(p)}",
        "SocketUser=root",
        "SocketGroup=root",
        "SocketMode=0600",
        "DirectoryMode=0711",
        "RemoveOnStop=yes",
        "Accept=no",
        "",
        "[Install]",
        "WantedBy=sockets.target",
        "",
    ]
    service = _header(p, "web", None, [
        f"Runs easy-ui as {layout.web_account}: it sees only ui/ (read-only) and auth/ (the sign-in),",
        "has no network of its own and no way to gain privileges. Rendered by mes from its own template.",
    ]) + [
        "",
        "[Unit]",
        f"Description=Dashboards of project {p}",
        f"Requires={socket_name}",
        f"After={socket_name}",
        "",
        "[Service]",
        "Type=simple",
        f"User={layout.web_account}",
        f"Group={layout.web_account}",
        f"ExecStart={exec_line(argv)}",
        "WorkingDirectory=/",
        f"Environment={env_assignment('HOME', '/nonexistent')}",
        f"Environment={env_assignment('MES_CONFIG', '/nonexistent')}",
        f"Environment={env_assignment('PYTHONNOUSERSITE', '1')}",
        "Restart=on-failure",
        "RestartSec=2",
        f"MemoryMax={params['memory_max']}",
        "MemoryDenyWriteExecute=yes",
        *[line for line in SANDBOX_LINES if not line.startswith("TasksMax=")],
        "TasksMax=64",
        "LimitNOFILE=1024",
        *_view(layout, ro=[layout.ui], rw=[layout.auth]),
        _hidden(project, layout),
        "# No network of its own: requests arrive only through the socket.",
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "IPAddressDeny=any",
        "",
    ]
    return {socket_name: "\n".join(socket), service_name: "\n".join(service)}


def render_project(project: Project, *, layout, mes: str, mes_python: str = DEFAULT_MES_PYTHON,
                   easy_ui: str | None = None) -> dict[str, str]:
    check_tool_path(mes_python, "the Python of mes")
    files: dict[str, str] = {}
    files.update(render_gate(project, layout=layout, mes=mes))
    files.update(render_selftest(project, layout=layout, mes=mes))
    for job in project.jobs.values():
        files[job_unit(project.name, job.name)] = render_job_service(project, job, layout=layout, mes=mes,
                                                                     mes_python=mes_python)
        if job.trigger == "schedule":
            files[job_unit(project.name, job.name, "timer")] = render_job_timer(project, job)
        if job.trigger == "webhook":
            files[job_unit(project.name, job.name, "path")] = render_job_path(project, job, layout=layout)
        if job.network or getattr(job, "local_network", ()):
            files[net_unit(project.name, job.name)] = render_net_service(project, job, mes_python=mes_python,
                                                                            layout=layout)
    if project.ui is not None and project.ui.refresh_after and easy_ui is not None:
        files[ui_refresh_unit(project.name)] = render_ui_refresh(project, layout=layout, easy_ui=easy_ui)
    if any(j.trigger == "webhook" for j in project.jobs.values()):
        files.update(render_webhook(project, layout=layout, mes=mes))
    for name in files:
        assert UNIT_NAME_RE.fullmatch(name), name
    return files


def read_markers(text: str) -> tuple[str | None, str | None]:
    project = job = None
    for line in text.splitlines()[:8]:
        if line.startswith(MARKER_PROJECT):
            project = line[len(MARKER_PROJECT):].strip()
        elif line.startswith(MARKER_JOB):
            job = line[len(MARKER_JOB):].strip()
    return project, job
