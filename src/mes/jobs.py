"""``mes jobs``: list, run, log, install, uninstall and check an app's jobs.

Jobs are declared in the app's ``app.toml`` (see ``mes guide jobs``). A job
always runs one of the app's own commands, as an argv list, never through a
shell.

Commands (all accept ``--app-dir DIR``; default: the app in the current folder)
--------------------------------------------------------------------------------
``mes jobs list``                  the jobs, their triggers and limits (read).
``mes jobs run <job> [--no-chain]`` run a job now, then the jobs set to run
                                   after it (write).
``mes jobs log [--job J] [--limit N] [--verify]``
                                   recent runs; ``--verify`` checks the hash chain (read).
``mes jobs status``                last run of each job, and whether it is
                                   installed on this computer (read).
``mes jobs install [--render-only [--out DIR]]``
                                   write and start the systemd user units (admin;
                                   Linux only, ``--render-only`` works everywhere).
``mes jobs uninstall [--app NAME]`` stop and remove them (admin). Data is kept.
``mes jobs record <job>``          used by systemd after each scheduled run to
                                   add it to the run log (write).

Run log
-------
``<state_dir>/runs.jsonl``: one JSON object per line, per run::

    {"time", "app", "job", "via", "argv", "exit_code", "result", "duration_s", "prev"}

``result`` uses systemd's words: ``success``, ``exit-code``, ``timeout``,
``signal``, ``start-failed`` (and whatever else systemd reports). ``prev`` is
the SHA-256 (hex) of the previous line's exact bytes, without its newline;
the first record uses ``GENESIS`` (64 zeros). Changing or removing any line
other than the last breaks the chain, and ``mes jobs log --verify`` says where.

Public API (besides ``register``)
---------------------------------
``GENESIS``, ``RUN_LOG_FILE``
``load_app(app_dir=None) -> (root, manifest)``
``app_command(root, manifest) -> list[str]``   absolute argv prefix of the app CLI
``mes_command() -> list[str]``                 absolute argv prefix of mes itself
``run_job(root, manifest, job_name, *, chain=True) -> list[dict]``
``append_record(log_path, record) -> dict``     adds ``prev`` and writes one line
``read_log(log_path) -> list[dict]``
``verify_log(log_path) -> dict``               raises ``MesError`` (code ``run_log_broken``)
``systemd_user_dir() -> Path``
``render_units(root, manifest) -> dict[str, str]``
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from mes import manifest as manifest_mod
from mes import paths, protocol, units
from mes.app import check_allowed
from mes.errors import MesError, NotFound, Refused, TemporaryFailure, UsageError
from mes.manifest import Job, Manifest

GENESIS = "0" * 64
RUN_LOG_FILE = units.RUN_LOG_FILE
OUTPUT_TAIL = 2000
KILL_GRACE_SEC = 5

# --------------------------------------------------------------------------- finding the app


def load_app(app_dir: str | os.PathLike[str] | None = None) -> tuple[Path, Manifest]:
    if app_dir is not None:
        root = Path(app_dir).expanduser().resolve()
        if not (root / manifest_mod.MANIFEST_FILE).is_file():
            raise NotFound(
                f"There is no app.toml in {root}, so mes cannot tell which jobs this app has.",
                hint="Point --app-dir at the app's folder (the one holding app.toml).",
            )
        return root, manifest_mod.load(root)
    found = manifest_mod.find()
    if found is None:
        raise NotFound(
            "There is no app in this folder (no app.toml was found here or above), so mes does not "
            "know whose jobs to use.",
            hint="Run the command from inside the app's folder, or add --app-dir <folder>.",
        )
    return found.parent.resolve(), manifest_mod.load(found)


# Programs that run arbitrary code or install software. An app's command can never be one of them:
# a job must run the app's own command-line tool, nothing else from its .venv/bin.
_NOT_APP_PROGRAMS = re.compile(
    r"^(?:python[0-9.]*|pythonw|pip[0-9.]*|uv|uvx|pydoc[0-9.]*|ipython[0-9]*|pytest|py\.test|activate.*|"
    r"sh|bash|zsh|fish|dash|env|deactivate)\Z", re.IGNORECASE)
_SCRIPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}\Z")


def _script_name(root: Path, manifest: Manifest) -> str:
    """The name of the app's program in .venv/bin: the app's name, or else the only script."""
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            scripts = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("scripts", {})
        except (tomllib.TOMLDecodeError, AttributeError, UnicodeDecodeError):
            scripts = {}
        if isinstance(scripts, dict) and scripts and manifest.name not in scripts and len(scripts) == 1:
            only = next(iter(scripts))
            if isinstance(only, str) and _SCRIPT_RE.match(only):
                if _NOT_APP_PROGRAMS.match(only):
                    raise Refused(
                        f'The app\'s pyproject.toml names its command "{only}", which is the name of a '
                        "program that runs any code (or installs software), not of the app itself. Jobs "
                        "only ever run the app's own command, so mes will not run it.",
                        hint=f'Name the command after the app in [project.scripts], for example '
                        f'{manifest.name} = "<module>.cli:main".',
                        code="unsafe_app_program",
                    )
                return only
    return manifest.name


def _expected_command(root: Path, manifest: Manifest) -> Path:
    return root / ".venv" / "bin" / _script_name(root, manifest)


def app_command(root: Path, manifest: Manifest) -> list[str]:
    exe = _expected_command(root, manifest)
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        raise NotFound(
            f'The program of the app "{manifest.name}" is not installed in its folder yet '
            f"(expected {exe}), so its jobs cannot start.",
            hint=f"Install the app's program first: uv sync --project {root}",
            code="app_not_installed",
        )
    return [str(exe)]


def _job_env(root: Path, allow: str, job: Job | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "MES_ALLOW": allow,
        "MES_DATA_HOME": str(paths.data_home()),
        "MES_STATE_HOME": str(paths.state_home()),
    })
    if job is not None:
        env["MES_JOB"] = job.name
    else:
        env.pop("MES_JOB", None)
    return env


def app_commands(root: Path, manifest: Manifest, exe: list[str]) -> dict[str, str]:
    """Ask the installed app which commands it has: ``{command: kind}``."""
    broken = f'The program of the app "{manifest.name}" did not say which commands it has, so mes cannot check its jobs and runs none of them.'
    try:
        res = subprocess.run([*exe, "--json", "describe"], cwd=root, env=_job_env(root, "read"),
                             stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MesError(broken, hint=f"Reinstall the app's program with: uv sync --project {root}",
                       code="app_broken", detail=f"{type(e).__name__}: {e}") from None
    try:
        info = json.loads(res.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        info = None
    if isinstance(info, dict) and isinstance(info.get("error"), dict) and res.returncode != 0:
        said = info["error"]
        raise MesError(broken, hint=str(said.get("hint") or f"Run it by hand to see more: uv run {exe[0]} describe"),
                       code="app_broken", detail=str(said.get("message") or ""), detail_label="What the app said:")
    if res.returncode != 0 or not isinstance(info, dict) or not isinstance(info.get("commands"), list):
        raise MesError(broken, hint=f"Reinstall the app's program with: uv sync --project {root}",
                       code="app_broken",
                       detail=_tail(res.stderr or res.stdout).strip() or None, detail_label="What the app said:")
    if info.get("app") != manifest.name:
        raise Refused(
            f'The program in the app\'s folder says it is the app {str(info.get("app"))[:40]!r}, but app.toml '
            f'says "{manifest.name}". Running its jobs could change the wrong app\'s data, so mes refuses.',
            hint='Use the same name in app.toml [app] name and in App("...") in the app\'s code.',
            code="app_mismatch",
        )
    return {c["name"]: c["kind"] for c in info["commands"]
            if isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("kind"), str)}


def job_problems(manifest: Manifest, commands: dict[str, str], jobs: list[Job] | None = None) -> list[str]:
    """Plain sentences for jobs whose command the app does not have, or may not run as a job."""
    out = []
    names = ", ".join(sorted(c for c in commands if c not in ("describe", "guide"))) or "none"
    for job in jobs if jobs is not None else manifest.jobs.values():
        cmd = job.argv[0]
        kind = commands.get(cmd)
        if kind is None:
            out.append(f'Job "{job.name}" runs "{cmd}", but {manifest.name} has no such command '
                       f"(its commands are: {names}).")
        elif kind not in ("read", "write"):
            out.append(f'Job "{job.name}" runs "{cmd}", which is an {kind} command. Jobs run with '
                       f'"write" access, so it would always be refused: jobs may only run read and '
                       "write commands.")
    return out


def _refuse_bad_jobs(problems: list[str]) -> None:
    if problems:
        raise MesError(
            " ".join(problems) + " Nothing was run or installed.",
            hint="Change the job's run line in app.toml to one of the app's commands (see: mes jobs list).",
            code="job_invalid",
        )


def mes_command() -> list[str]:
    return [str(Path(sys.executable).absolute()), "-m", "mes.cli"]


def _state_dir(manifest: Manifest, create: bool = False) -> Path:
    return paths.app_state_dir(manifest.name, create=create)


def _log_path(manifest: Manifest) -> Path:
    return _state_dir(manifest) / RUN_LOG_FILE


def _size_words(size: str) -> str:
    return f"{size[:-1]} {size[-1]}B"


def _duration_words(text: str) -> str:
    n, unit = int(text[:-1]), text[-1]
    word = {"s": "second", "m": "minute", "h": "hour"}[unit]
    return f"{n} {word}{'' if n == 1 else 's'}"


def _network_words(job: Job) -> str:
    if not job.network:
        return "has no internet access"
    return (f"has full internet access (it is meant to reach {', '.join(job.network)}, but this "
            "version of mes cannot limit it to those hosts yet)")


def _when(job: Job) -> str:
    if job.trigger == "schedule":
        return f"on schedule: {job.schedule}"
    if job.trigger == "after":
        return f'after "{job.after}" succeeds'
    return f"only when asked (mes jobs run {job.name})"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- run log


def _line_hash(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def _last_line(f: Any) -> bytes | None:
    """The last complete line of an open binary file (without newline), or None when empty."""
    f.seek(0, os.SEEK_END)
    end = f.tell()
    if end == 0:
        return None
    pos = end
    chunk = b""
    while pos > 0:
        step = min(4096, pos)
        pos -= step
        f.seek(pos)
        chunk = f.read(step) + chunk
        body = chunk[:-1] if chunk.endswith(b"\n") else chunk
        if b"\n" in body:
            return body.rsplit(b"\n", 1)[1]
    return chunk[:-1] if chunk.endswith(b"\n") else chunk


def append_record(log_path: Path, record: dict[str, Any]) -> dict[str, Any]:
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(log_path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "r+b") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            last = _last_line(f)
            f.seek(0, os.SEEK_END)
            if last is not None and f.tell() > 0:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    f.write(b"\n")  # a half-written line stays, and --verify will point at it
            full = {**record, "prev": GENESIS if last is None else _line_hash(last)}
            line = json.dumps(full, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            f.write(line + b"\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return full


def _log_lines(log_path: Path) -> list[bytes]:
    if not log_path.is_file():
        return []
    data = log_path.read_bytes()
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def read_log(log_path: Path) -> list[dict[str, Any]]:
    out = []
    for line in _log_lines(log_path):
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _broken(log_path: Path, number: int, problem: str) -> MesError:
    return MesError(
        f"The run log ({log_path}) has been changed: {problem}. The log is a chain "
        "where every line includes a fingerprint of the line before, so this means a past record "
        "was edited, removed or damaged, and the history of job runs can no longer be trusted from "
        "that point on.",
        hint="Do not delete the file: keep it as evidence. If you did not edit it yourself, treat "
        "this computer as possibly tampered with and ask someone you trust to look at it.",
        code="run_log_broken",
        detail=f"line={number}",
    )


def verify_log(log_path: Path) -> dict[str, Any]:
    prev = GENESIS
    first = last = None
    lines = _log_lines(log_path)
    for i, line in enumerate(lines, start=1):
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise _broken(log_path, i, f"line {i} is not a valid record") from None
        if not isinstance(rec, dict) or not isinstance(rec.get("prev"), str):
            raise _broken(log_path, i, f"line {i} has no link to the line before it")
        if rec["prev"] != prev:
            changed = "the first line was changed" if i == 1 else (
                f"line {i - 1} or an earlier line was changed, or a line right after it was removed")
            raise _broken(log_path, i, changed)
        prev = _line_hash(line)
        first = first or rec.get("time")
        last = rec.get("time")
    return {
        "ok": True,
        "records": len(lines),
        "first": first,
        "last": last,
        "head": prev if lines else None,
        "log": str(log_path),
        "message": (
            f"The run log is intact: all {len(lines)} records link up correctly."
            if lines else "The run log is empty: no job has run yet on this computer."
        ),
        "note": "The check proves no earlier record was changed or removed; it cannot tell whether "
        "the newest records were cut off.",
    }


# --------------------------------------------------------------------------- running


@contextlib.contextmanager
def _job_lock(manifest: Manifest, job: Job) -> Iterator[None]:
    lock_dir = _state_dir(manifest, create=True) / "locks"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(lock_dir / f"{job.name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TemporaryFailure(
                f'The job "{job.name}" is already running, so it was not started a second time '
                "(two copies at once could damage its data).",
                hint="Wait for it to finish, then try again.",
                code="job_running",
            ) from None
        yield
    finally:
        os.close(fd)


def _tail(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return text[-OUTPUT_TAIL:]


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            proc.wait(timeout=KILL_GRACE_SEC)
            return
        except subprocess.TimeoutExpired:
            continue


def _parse_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _run_one(root: Path, manifest: Manifest, job: Job, exe: list[str]) -> dict[str, Any]:
    # --json goes before the command, so it never mixes with the job's own arguments.
    argv = [*exe, "--json", *job.argv]
    env = _job_env(root, units.JOB_ALLOW_LEVEL, job)
    started = _now()
    t0 = time.monotonic()
    out = err = b""
    exit_code: int | None = None
    result = "interrupted"  # what the log says if mes itself is stopped while the job runs
    record: dict[str, Any] = {}
    with _job_lock(manifest, job):
        try:
            try:
                proc = subprocess.Popen(
                    argv, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, start_new_session=True,
                )
            except OSError as e:
                result, err = "start-failed", str(e).encode()
            else:
                try:
                    out, err = proc.communicate(timeout=job.timeout_seconds)
                    exit_code = proc.returncode
                    if exit_code == 0:
                        result = "success"
                    elif exit_code < 0:
                        result, exit_code = "signal", None
                    else:
                        result = "exit-code"
                except subprocess.TimeoutExpired:
                    _kill_group(proc)
                    more_out, more_err = proc.communicate()
                    out, err = out + (more_out or b""), err + (more_err or b"")
                    result = "timeout"
                except BaseException:
                    _kill_group(proc)
                    raise
        finally:
            # Once the job was started (or failed to start), there is always a line in the log.
            duration = round(time.monotonic() - t0, 3)
            record = append_record(_log_path(manifest), {
                "time": started,
                "app": manifest.name,
                "job": job.name,
                "via": "mes jobs run",
                "argv": argv,
                "exit_code": exit_code,
                "result": result,
                "duration_s": duration,
            })
    return {
        "job": job.name,
        "result": result,
        "exit_code": exit_code,
        "duration_s": duration,
        "time": started,
        "data": _parse_json(out) if result == "success" else None,
        "output": _tail(out),
        "errors": _tail(err),
        "_app_error": _app_error(out),
        "_record": record,
    }


def _app_error(out: bytes) -> dict[str, Any] | None:
    data = _parse_json(out)
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return data["error"]
    return None


def _failure(run: dict[str, Any], job: Job, done: list[str], *, manifest: Manifest, chain: bool,
             script: str) -> MesError:
    already = f" Jobs that finished before it: {', '.join(done)}." if done else ""
    waiting = [j.name for j in manifest.dependents(job.name)] if chain else []
    held = f" The jobs that wait for it ({', '.join(waiting)}) were not started." if waiting else ""
    app_error = run.get("_app_error")
    if app_error:
        said = str(app_error.get("message") or "").strip()
        if app_error.get("hint"):
            said += f"\nIts advice: {app_error['hint']}"
    else:
        said = (run.get("errors") or run.get("output") or "").strip()
    detail = said or None
    label = "What the app said:" if detail else None
    by_hand = f"To see it yourself, run this in the app's folder: uv run {script} {job.run}"
    if run["result"] == "timeout":
        return MesError(
            f'The job "{job.name}" took longer than its time limit ({_duration_words(job.timeout)}) '
            f"and was stopped, so its work may be unfinished.{already}{held}",
            hint=f"If it really needs more time, raise timeout for [jobs.{job.name}] in app.toml "
            "(your assistant can do this).",
            code="job_timeout", detail=detail, detail_label=label,
        )
    if run["result"] == "start-failed":
        return MesError(
            f'The job "{job.name}" could not be started, so nothing was done.{already}{held}',
            hint="Reinstall the app's program with: uv sync",
            code="job_start_failed", detail=detail, detail_label=label,
        )
    if run["exit_code"] == 75:
        return TemporaryFailure(
            f'The job "{job.name}" hit a temporary problem (for example the network was down) and '
            f"stopped.{already}{held}",
            hint=f"Try again later: mes jobs run {job.name}",
            code="job_temporary", detail=detail, detail_label=label,
        )
    how = "stopped with an error" if run["exit_code"] is not None else "was stopped by the system before it finished"
    return MesError(
        f'The job "{job.name}" {how}, so its work is not done.{already}{held}',
        hint=by_hand,
        code="job_failed", detail=detail, detail_label=label,
    )


def _chain(manifest: Manifest, first: Job, chain: bool) -> list[Job]:
    out, queue = [], [first]
    while queue:
        job = queue.pop(0)
        out.append(job)
        if chain:
            queue.extend(manifest.dependents(job.name))
    return out


def run_job(root: Path, manifest: Manifest, job_name: str, *, chain: bool = True) -> list[dict[str, Any]]:
    first = manifest.job(job_name)
    exe = app_command(root, manifest)
    script = _script_name(root, manifest)
    planned = _chain(manifest, first, chain)
    _refuse_bad_jobs(job_problems(manifest, app_commands(root, manifest, exe), planned))
    runs: list[dict[str, Any]] = []
    for job in planned:
        run = _run_one(root, manifest, job, exe)
        run.pop("_record", None)
        app_error = run.pop("_app_error", None)
        if run["result"] != "success":
            raise _failure({**run, "_app_error": app_error}, job, [r["job"] for r in runs],
                           manifest=manifest, chain=chain, script=script)
        runs.append(run)
    return runs


# --------------------------------------------------------------------------- systemd


def systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".config") / "systemd" / "user"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, timeout=60, check=False
    )


def _has_systemd() -> bool:
    return shutil.which("systemctl") is not None


def _needs_linux(app: str) -> MesError:
    return MesError(
        "Scheduled jobs need a Linux server: they are run by systemd, the part of Linux that starts "
        "programs on a timetable. This computer is not Linux, so nothing was installed and nothing "
        "will run on its own here.",
        hint="You can still run any job by hand now with: mes jobs run <job>. Installing apps on a "
        "server you own will come with 'mes deploy' (not available yet). To look at the files that "
        "would be installed, run: mes jobs install --render-only",
        code="needs_linux",
    )


def _needs_systemd() -> MesError:
    return MesError(
        "This Linux computer does not have systemd (the program that starts jobs on a timetable), "
        "so scheduled jobs cannot be installed here.",
        hint="Use a Linux server that runs systemd (most do), or run jobs by hand with: mes jobs run <job>",
        code="needs_systemd",
    )


def _check_schedules(manifest: Manifest) -> None:
    tool = shutil.which("systemd-analyze")
    if tool is None:
        return
    for job in manifest.jobs.values():
        if job.trigger != "schedule":
            continue
        try:
            res = subprocess.run([tool, "calendar", "--", job.schedule or ""], capture_output=True,
                                 text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return
        if res.returncode != 0:
            raise MesError(
                f'The schedule of job "{job.name}" ("{job.schedule}") is not a time systemd '
                "understands, so the job would never start. Nothing was installed.",
                hint='Use a calendar expression such as "daily", "hourly" or "*-*-* 06:00:00" '
                f"for [jobs.{job.name}] in app.toml.",
                code="manifest_invalid",
                detail=(res.stderr or res.stdout).strip() or None,
            )


def render_units(root: Path, manifest: Manifest, *, exe: list[str] | None = None) -> dict[str, str]:
    return units.render_app(
        manifest,
        app_command=exe if exe is not None else [str(_expected_command(root, manifest))],
        mes_command=mes_command(),
        root=root,
        data_home=paths.data_home().absolute(),
        state_home=paths.state_home().absolute(),
    )


def _installed_units(app: str) -> dict[str, Path]:
    """Unit files in the systemd user folder that mes made for this app."""
    d = systemd_user_dir()
    found: dict[str, Path] = {}
    if not d.is_dir():
        return found
    for p in sorted(d.glob(f"{units.UNIT_PREFIX}-{app}-*")):
        if p.suffix not in (".service", ".timer") or not p.is_file():
            continue
        try:
            owner, _ = units.read_markers(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if owner == app:
            found[p.name] = p
    return found


def _write_file(path: Path, text: str, mode: int = 0o644) -> None:
    """Write ``path`` all at once. The temporary file gets a fresh random name (created with
    O_EXCL, never following a planted symlink) and its mode is set on the open file itself."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _check_private_folder(folder: Path) -> None:
    """Refuse a folder that other users of this computer could write into."""
    st = folder.stat()
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or st.st_uid not in (os.getuid(), 0):
        raise Refused(
            f"The folder {folder} can be changed by other users of this computer, so mes will not "
            "write service files into it (someone could swap them before they are used).",
            hint="Choose a folder of your own, for example a new folder in your home folder.",
            code="unsafe_folder",
        )


def _sc_or_fail(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        res = _systemctl(*args)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MesError(
            "mes could not talk to systemd (the program that starts jobs), so the jobs may not be "
            "switched on.",
            hint="Check that you are logged in to this computer normally, then run 'mes jobs install' again.",
            code="systemd_failed",
            detail=str(e),
        ) from None
    if res.returncode != 0:
        raise MesError(
            f"systemd refused to {args[0]} the jobs, so they may not be switched on.",
            hint="Run 'mes doctor' to check this computer, then run 'mes jobs install' again.",
            code="systemd_failed",
            detail=(res.stderr or res.stdout).strip() or None,
        )
    return res


def _linger_warning() -> str | None:
    try:
        res = subprocess.run(["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"],
                             capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode == 0 and res.stdout.strip() == "yes":
        return None
    return ("Scheduled jobs only run while you are logged in to this computer. To let them run at "
            "any time, switch on 'lingering' with: loginctl enable-linger")


def _describe_job(manifest: Manifest, job: Job) -> str:
    return (f'Job "{job.name}" runs "{manifest.name} {job.run}" {_when(job)}; it {_network_words(job)}, '
            f"may use up to {_size_words(job.memory)} of memory and is stopped after "
            f"{_duration_words(job.timeout)}.")


def _schedule_note(manifest: Manifest) -> str | None:
    if any(j.trigger == "schedule" for j in manifest.jobs.values()) and shutil.which("systemd-analyze") is None:
        return ("The schedules were checked by mes's own simple check, because this computer has no "
                "systemd. systemd checks them again before they are switched on, on the Linux server.")
    return None


def _app_check_warnings(root: Path, manifest: Manifest) -> list[str]:
    """Warnings (not errors) about jobs, for commands that only show things."""
    exe = _expected_command(root, manifest)
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        return []
    try:
        return job_problems(manifest, app_commands(root, manifest, [str(exe)]))
    except MesError as e:
        return [e.message]


def install(root: Path, manifest: Manifest) -> dict[str, Any]:
    if not _is_linux():
        raise _needs_linux(manifest.name)
    if not _has_systemd():
        raise _needs_systemd()
    exe = app_command(root, manifest)
    _refuse_bad_jobs(job_problems(manifest, app_commands(root, manifest, exe)))
    _check_schedules(manifest)
    files = render_units(root, manifest, exe=exe)

    unit_dir = systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name in files:
        existing = unit_dir / name
        if existing.exists():
            owner, _ = units.read_markers(existing.read_text(encoding="utf-8", errors="replace"))
            if owner != manifest.name:
                raise Refused(
                    f"A service file called {name} already exists and was not made by mes for the app "
                    f'"{manifest.name}", so mes will not overwrite it.',
                    hint="Rename the job in app.toml, or move that file away if you know it is not needed.",
                    code="unit_conflict",
                )

    old = _installed_units(manifest.name)
    stale = [n for n in old if n not in files]
    for n in stale:
        if n.endswith(".timer"):
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                _systemctl("disable", "--now", n)
        old[n].unlink(missing_ok=True)

    # The sandbox needs these to exist; the run log is read-only inside the job.
    paths.app_data_dir(manifest.name, create=True)
    state = _state_dir(manifest, create=True)
    log = state / RUN_LOG_FILE
    os.close(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))

    slice_path = unit_dir / units.SLICE
    if not slice_path.exists():
        _write_file(slice_path, units.render_slice())
    for name, text in files.items():
        _write_file(unit_dir / name, text)

    _sc_or_fail("daemon-reload")
    timers = [n for n in files if n.endswith(".timer")]
    for t in timers:
        _sc_or_fail("enable", "--now", t)

    warnings = [w for w in (_linger_warning(),) if w]
    return {
        "app": manifest.name,
        "installed": sorted(files),
        "removed": sorted(stale),
        "timers_started": timers,
        "unit_dir": str(unit_dir),
        "what_happens": [_describe_job(manifest, j) for j in manifest.jobs.values()],
        "warnings": warnings,
        "message": f'The jobs of "{manifest.name}" are installed on this computer and their schedules '
        "are switched on. Each run is recorded in the run log (mes jobs log).",
    }


def render_only(root: Path, manifest: Manifest, out: str | None) -> dict[str, Any]:
    warnings = []
    exe = _expected_command(root, manifest)
    if not exe.is_file():
        warnings.append(f"The app's program is not installed yet (expected {exe}); run 'uv sync' in "
                        "the app's folder before installing for real.")
    warnings += _app_check_warnings(root, manifest)
    note = _schedule_note(manifest)
    if note:
        warnings.append(note)
    files = render_units(root, manifest)
    files_with_slice = {**files, units.SLICE: units.render_slice()}
    result: dict[str, Any] = {
        "app": manifest.name,
        "what_happens": [_describe_job(manifest, j) for j in manifest.jobs.values()],
        "warnings": warnings,
        "message": "Nothing was installed: these are only the files that 'mes jobs install' would write.",
    }
    if out is None:
        result["units"] = files_with_slice
        return result
    out_dir = Path(out).expanduser().absolute()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = out_dir.resolve()
    if out_dir == systemd_user_dir().resolve():
        raise Refused(
            "--out points at the folder systemd reads its jobs from, so the files would take effect "
            "without the checks 'mes jobs install' does. Nothing was written.",
            hint="To switch the jobs on, run: mes jobs install. To only look at the files, use "
            "--render-only without --out, or --out with another folder.",
            code="unsafe_folder",
        )
    _check_private_folder(out_dir)
    for name in files_with_slice:
        existing = out_dir / name
        if existing.is_symlink() or (existing.exists() and not existing.is_file()):
            raise Refused(f"{existing} is not a plain file, so mes will not write over it.",
                          hint="Move it away, or choose another folder with --out.", code="unit_conflict")
        if existing.exists():
            text = existing.read_text(encoding="utf-8", errors="replace")
            owner, _ = units.read_markers(text)
            ours = owner == manifest.name or (name == units.SLICE and text.startswith(units.HEADER))
            if not ours:
                raise Refused(
                    f"{existing} already exists and was not made by mes for the app \"{manifest.name}\", "
                    "so mes will not overwrite it. Nothing was written.",
                    hint="Move that file away, or choose another folder with --out.",
                    code="unit_conflict",
                )
    for name, text in files_with_slice.items():
        _write_file(out_dir / name, text)
    result["out"] = str(out_dir)
    result["files"] = sorted(files_with_slice)
    return result


def uninstall(app: str) -> dict[str, Any]:
    paths.app_state_dir(app)  # checks the name
    if not _is_linux() or not _has_systemd():
        return {
            "app": app,
            "removed": [],
            "message": "Nothing to remove: scheduled jobs are only installed on Linux servers, and "
            "this computer has none. Your data was not touched.",
        }
    found = _installed_units(app)
    for name in found:
        if name.endswith(".timer"):
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                _systemctl("disable", "--now", name)
    for name in found:
        if name.endswith(".service"):
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                _systemctl("stop", name)
    for p in found.values():
        p.unlink(missing_ok=True)
    if found:
        _sc_or_fail("daemon-reload")
    return {
        "app": app,
        "removed": sorted(found),
        "message": (
            f'The scheduled jobs of "{app}" are switched off and removed from this computer. '
            "The app's data and run log were kept."
            if found else f'No jobs of "{app}" were installed on this computer, so nothing changed.'
        ),
    }


def _show(unit: str, *props: str) -> dict[str, str]:
    try:
        res = _systemctl("show", unit, *(f"--property={p}" for p in props))
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if res.returncode != 0:
        return {}
    out = {}
    for line in res.stdout.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k] = v
    return out


def status(root: Path, manifest: Manifest, *, json_mode: bool) -> Any:
    log = read_log(_log_path(manifest))
    last: dict[str, dict[str, Any]] = {}
    for rec in log:
        if isinstance(rec.get("job"), str):
            last[rec["job"]] = rec
    on_linux = _is_linux() and _has_systemd()
    installed = _installed_units(manifest.name) if on_linux else {}
    rows = []
    for job in manifest.jobs.values():
        rec = last.get(job.name, {})
        service = units.unit_name(manifest.name, job.name)
        timer = units.unit_name(manifest.name, job.name, "timer")
        if not on_linux:
            inst, inst_note = False, "Scheduled jobs need a Linux server; on this computer run them by hand."
        else:
            inst, inst_note = service in installed, None
        next_run = None
        if on_linux and job.trigger == "schedule" and timer in installed:
            next_run = _show(timer, "NextElapseUSecRealtime").get("NextElapseUSecRealtime") or None
        rows.append({
            "job": job.name,
            "when": _when(job),
            "installed": inst,
            "installed_note": inst_note,
            "last_run": rec.get("time"),
            "last_result": rec.get("result"),
            "last_exit_code": rec.get("exit_code"),
            "last_duration_s": rec.get("duration_s"),
            "next_run": next_run,
        })
    if json_mode:
        return {"app": manifest.name, "systemd": on_linux, "jobs": rows}
    text_rows = [{
        "job": r["job"], "when": r["when"],
        "switched on here": "yes" if r["installed"] else "no",
        "last run": r["last_run"] or "never",
        "last result": _RESULT_WORDS.get(r["last_result"], r["last_result"]) if r["last_result"] else "-",
        "next run": r["next_run"] or "-",
    } for r in rows]
    text = protocol.format_text(text_rows)
    if not on_linux:
        text += "\n\nScheduled jobs need a Linux server, so none are switched on here. Run them by hand with: mes jobs run <job>"
    return text


_INVOCATION_RE = re.compile(r"^[0-9a-f]{32}\Z")


def _not_from_systemd() -> Refused:
    return Refused(
        "This command only records a run that systemd itself has just finished, and this one was "
        "not started by systemd, so nothing was added to the run log. (Otherwise anyone could add "
        "made-up runs to the history.)",
        hint="To run a job yourself, use: mes jobs run <job>",
        code="not_from_systemd",
    )


def record_systemd_run(root: Path, manifest: Manifest, job_name: str) -> dict[str, Any]:
    """Called by systemd (ExecStopPost=+) after a scheduled run, outside the sandbox."""
    job = manifest.job(job_name)
    # systemd gives every process of a unit run, ExecStopPost= included, the run's INVOCATION_ID.
    invocation = os.environ.get("INVOCATION_ID", "")
    if not _is_linux() or not _INVOCATION_RE.match(invocation) or not os.environ.get("SERVICE_RESULT"):
        raise _not_from_systemd()
    current = _show(units.unit_name(manifest.name, job.name), "InvocationID").get("InvocationID")
    if current != invocation:
        raise _not_from_systemd()
    result = os.environ.get("SERVICE_RESULT") or "unknown"
    exit_kind = os.environ.get("EXIT_CODE")
    exit_status = os.environ.get("EXIT_STATUS")
    exit_code = int(exit_status) if exit_kind == "exited" and (exit_status or "").isdigit() else None
    duration = None
    props = _show(units.unit_name(manifest.name, job.name),
                  "ExecMainStartTimestampMonotonic", "ExecMainExitTimestampMonotonic")
    try:
        start = int(props.get("ExecMainStartTimestampMonotonic", ""))
        end = int(props.get("ExecMainExitTimestampMonotonic", ""))
        if start > 0 and end >= start:
            duration = round((end - start) / 1_000_000, 3)
    except ValueError:
        pass
    exe = [str(_expected_command(root, manifest))]
    rec = append_record(_log_path(manifest), {
        "time": _now(),
        "app": manifest.name,
        "job": job.name,
        "via": "systemd",
        "argv": [*exe, *job.argv],
        "exit_code": exit_code,
        "result": result,
        "duration_s": duration,
    })
    return {k: rec[k] for k in ("time", "job", "result", "exit_code", "duration_s")}


# --------------------------------------------------------------------------- command handlers


def _cmd_list(args: argparse.Namespace) -> Any:
    check_allowed("read")
    root, manifest = load_app(args.app_dir)
    warnings = _app_check_warnings(root, manifest)
    note = _schedule_note(manifest)
    if note:
        warnings.append(note)
    if args.json:
        return {"app": manifest.name, "jobs": [{**j.to_dict(), "when": _when(j)} for j in manifest.jobs.values()],
                "warnings": warnings}
    if not manifest.jobs:
        return f'The app "{manifest.name}" has no jobs yet. Add one to app.toml (see: mes guide jobs).'
    rows = [
        {
            "job": j.name,
            "when": _when(j),
            "runs": f"{manifest.name} {j.run}",
            "memory": _size_words(j.memory),
            "time limit": _duration_words(j.timeout),
            "internet": ("full (meant for " + ", ".join(j.network) + ")") if j.network else "none",
            "may change": " and ".join(j.writes) if j.writes else "nothing",
        }
        for j in manifest.jobs.values()
    ]
    text = protocol.format_text(rows)
    if any(j.network for j in manifest.jobs.values()):
        text += "\n\n" + manifest_mod.NETWORK_NOTE
    if warnings:
        text += "\n\nPlease note:\n" + "\n".join(f"- {w}" for w in warnings)
    return text


def _cmd_run(args: argparse.Namespace) -> Any:
    check_allowed("write")
    root, manifest = load_app(args.app_dir)
    runs = run_job(root, manifest, args.job, chain=not args.no_chain)
    if args.json:
        return {"app": manifest.name, "ok": True, "runs": runs}
    lines = []
    for r in runs:
        lines.append(f'Job "{r["job"]}" finished successfully in {r["duration_s"]} seconds.')
        shown = protocol.format_text(r["data"]) if r.get("data") is not None else r["output"]
        if shown.strip():
            lines.append("What it reported:\n" + shown.rstrip())
    return "\n".join(lines)


def _cmd_log(args: argparse.Namespace) -> Any:
    check_allowed("read")
    _, manifest = load_app(args.app_dir)
    path = _log_path(manifest)
    if args.verify:
        result = verify_log(path)
        if args.json:
            return result
        return f"{result['message']}\n{result['note']}"
    if args.limit < 1:
        raise UsageError("--limit must be 1 or more.", hint="For example: mes jobs log --limit 20")
    recs = [r for r in read_log(path) if args.job is None or r.get("job") == args.job][-args.limit:]
    if args.json:
        return {"app": manifest.name, "log": str(path), "runs": recs}
    if not recs:
        return "No job has run yet on this computer."
    return [
        {
            "time": r.get("time"),
            "job": r.get("job"),
            "result": _RESULT_WORDS.get(r.get("result"), r.get("result")),
            "took": f"{r['duration_s']} s" if isinstance(r.get("duration_s"), (int, float)) else "-",
            "how it was started": _VIA_WORDS.get(r.get("via"), r.get("via")),
        }
        for r in recs
    ]


_RESULT_WORDS = {
    "success": "worked",
    "exit-code": "failed",
    "timeout": "stopped: took too long",
    "signal": "stopped by the system",
    "start-failed": "could not start",
    "interrupted": "interrupted",
    "core-dump": "crashed",
    "watchdog": "stopped: stuck",
    "resources": "could not start: not enough resources",
}
_VIA_WORDS = {"mes jobs run": "by hand", "systemd": "on schedule"}


def _cmd_status(args: argparse.Namespace) -> Any:
    check_allowed("read")
    root, manifest = load_app(args.app_dir)
    return status(root, manifest, json_mode=args.json)


def _install_text(result: dict[str, Any]) -> str:
    lines = [result["message"]]
    if result.get("what_happens"):
        lines += ["", "What the jobs do:"] + [f"- {s}" for s in result["what_happens"]]
    if result.get("removed"):
        lines += ["", "Removed (no longer in app.toml): " + ", ".join(result["removed"])]
    if result.get("warnings"):
        lines += ["", "Please note:"] + [f"- {w}" for w in result["warnings"]]
    if result.get("out"):
        lines += ["", f"Files written to {result['out']}: " + ", ".join(result["files"])]
    for name, text in (result.get("units") or {}).items():
        lines += ["", f"===== {name} =====", text.rstrip()]
    return "\n".join(lines)


def _cmd_install(args: argparse.Namespace) -> Any:
    if args.out is not None and not args.render_only:
        raise UsageError("--out only works together with --render-only.",
                         hint="Run: mes jobs install --render-only --out <folder>")
    if args.render_only:
        # Only showing the files is "read"; writing them into a folder is a setup change.
        check_allowed("read" if args.out is None else "admin")
        root, manifest = load_app(args.app_dir)
        result = render_only(root, manifest, args.out)
    else:
        check_allowed("admin")
        root, manifest = load_app(args.app_dir)
        result = install(root, manifest)
    return result if args.json else _install_text(result)


def _cmd_uninstall(args: argparse.Namespace) -> Any:
    check_allowed("admin")
    if args.app is not None:
        name = args.app
        if not manifest_mod.APP_NAME_RE.fullmatch(name):
            raise UsageError(f"{name!r} is not a valid app name.",
                             hint="App names use lowercase letters, digits and dashes, starting with a letter.")
    else:
        _, manifest = load_app(args.app_dir)
        name = manifest.name
    return uninstall(name)


def _cmd_record(args: argparse.Namespace) -> Any:
    check_allowed("write")
    root, manifest = load_app(args.app_dir)
    return record_systemd_run(root, manifest, args.job)


def _app_dir_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--app-dir", metavar="DIR", default=None,
                   help="the app's folder (default: the app in the current folder)")


def register(subparsers: Any) -> None:
    jobs = subparsers.add_parser(
        "jobs",
        help="List, run and schedule an app's jobs.",
        description="Jobs are short runs of an app's own commands, declared in app.toml. "
        "See: mes guide jobs",
    )
    sub = jobs.add_subparsers(dest="jobs_command", metavar="<action>")

    p = sub.add_parser("list", help="The app's jobs, when they run and their limits.")
    _app_dir_arg(p)
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("run", help="Run a job now (then the jobs set to run after it).")
    p.add_argument("job", help="the job's name in app.toml")
    p.add_argument("--no-chain", action="store_true", help="do not start the jobs that run after this one")
    _app_dir_arg(p)
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("log", help="Recent job runs; --verify checks that the log was not changed.")
    p.add_argument("--job", default=None, help="only this job")
    p.add_argument("--limit", type=int, default=20, help="how many runs to show (default 20)")
    p.add_argument("--verify", action="store_true", help="check the whole log's hash chain")
    _app_dir_arg(p)
    p.set_defaults(func=_cmd_log)

    p = sub.add_parser("status", help="When each job last ran, whether it worked, and if it is installed.")
    _app_dir_arg(p)
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("install", help="Switch on the job schedules on this Linux computer (systemd).")
    p.add_argument("--render-only", action="store_true",
                   help="only show the service files that would be written (works on any computer)")
    p.add_argument("--out", metavar="DIR", default=None, help="with --render-only: write the files to DIR")
    _app_dir_arg(p)
    p.set_defaults(func=_cmd_install)

    p = sub.add_parser("uninstall", help="Switch off and remove the app's job schedules. Data is kept.")
    p.add_argument("--app", metavar="NAME", default=None, help="the app's name (default: the app in this folder)")
    _app_dir_arg(p)
    p.set_defaults(func=_cmd_uninstall)

    p = sub.add_parser("record", help="(Used by systemd after each scheduled run.) Add the run to the log.")
    p.add_argument("job", help="the job's name in app.toml")
    _app_dir_arg(p)
    p.set_defaults(func=_cmd_record)
