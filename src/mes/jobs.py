"""``mes jobs``: list, run and log a project's jobs; and the two small helpers the server's
system units call.

Jobs are declared by the apps, each in its own ``app.toml`` (see ``mes guide jobs``), and the
project can change them in ``[settings.<app>]``. A job's id is ``<app>.<job>``. A job always runs
a command of its own app's program, as an argv list, never through a shell.

Commands (``--project-dir DIR``; default: the project in the current folder)
---------------------------------------------------------------------------
``mes jobs list``                   the jobs, their triggers and limits (read).
``mes jobs run <job> [--no-chain]`` run a job now on this computer, then the jobs set to run
                                    after it (write). ``<job>`` is a job id, or a bare job name
                                    when exactly one app has a job of that name.
``mes jobs log [--job J] [--limit N] [--verify]``  recent runs; ``--verify`` checks the hash chain.
``mes jobs status``                 last run of each job on this computer (read).
``mes jobs install``                only says where scheduling moved: ``mes server plan``.

Used by the system units on a server (admin; they run as root, take only names as arguments
and read nothing from the project's files):

``mes jobs record --project P --server-root R <job id>``
    after each run (``ExecStopPost=+``): appends the run to ``<R>/<P>/log/runs.jsonl``.
``mes jobs allow-network --unit U -- <host>...``
    before each run of a job that lists hosts: looks the hosts up and sets the job unit's
    ``IPAddressAllow=`` (the job unit itself has ``IPAddressDeny=any``).

Run log
-------
One JSON object per line, per run::

    {"time", "project", "app", "job", "via", "argv", "exit_code", "result", "duration_s", "prev"}

``app`` is the app's name and ``job`` the job id.

``result`` uses systemd's words: ``success``, ``exit-code``, ``timeout``, ``signal``,
``start-failed`` (and whatever else systemd reports). ``prev`` is the SHA-256 (hex) of the
previous line's exact bytes, without its newline; the first record uses ``GENESIS`` (64 zeros).
Changing or removing any line other than the last breaks the chain, and ``mes jobs log
--verify`` says where.

Public API (besides ``register``)
---------------------------------
``GENESIS``, ``RUN_LOG_FILE``
``project_root(project_dir=None) -> Path``  the project's folder (nothing else is read)
``load_project(project_dir=None) -> (root, project)``
``program(root, project, app) -> Path``      ``<root>/.venv/bin/<program>``
``app_command(root, project, app) -> list[str]``  that program, checked to exist
``app_commands(root, project, app, exe) -> dict[str, str]``  command -> kind, from the app itself
``job_problems(project, commands_by_app, jobs=None) -> list[str]``
``run_job(root, project, job, *, chain=True) -> list[dict]``
``rebuild_apps(root, project, *, app=None) -> list[dict]``  ``mes db rebuild``
``append_record(log_path, record) -> dict``     adds ``prev`` and writes one line
``read_log(log_path) -> list[dict]``, ``verify_log(log_path) -> dict``
``resolve_hosts(hosts, *, resolver=None) -> list[str]``  addresses for ``allow-network``
``nameservers(path="/etc/resolv.conf") -> list[str]``
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from mes import manifest as manifest_mod
from mes import paths, protocol, units
from mes.app import check_allowed
from mes.errors import MesError, NotFound, Refused, TemporaryFailure, UsageError
from mes.manifest import Job, Project

GENESIS = "0" * 64
RUN_LOG_FILE = units.RUN_LOG_FILE
OUTPUT_TAIL = 2000
KILL_GRACE_SEC = 5

# --------------------------------------------------------------------------- finding the project


def project_root(project_dir: str | os.PathLike[str] | None = None) -> Path:
    if project_dir is not None:
        root = Path(project_dir).expanduser().resolve()
        if not (root / manifest_mod.MANIFEST_FILE).is_file():
            raise NotFound(
                f"There is no project.toml in {root}, so mes cannot tell which project this is.",
                hint="Point --project-dir at the project's folder (the one holding project.toml).",
            )
        return root
    found = manifest_mod.find()
    if found is None:
        raise NotFound(
            "There is no project in this folder (no project.toml was found here or above), so mes does not "
            "know which project to use.",
            hint="Run the command from inside the project's folder, or add --project-dir <folder>.",
        )
    return found.parent.resolve()


def load_project(project_dir: str | os.PathLike[str] | None = None) -> tuple[Path, Project]:
    root = project_root(project_dir)
    return root, manifest_mod.load(root)


def program(root: Path, project: Project, app: str) -> Path:
    return root / ".venv" / "bin" / project.app(app).program


def app_command(root: Path, project: Project, app: str) -> list[str]:
    exe = program(root, project, app)
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        raise NotFound(
            f'The program of the app "{app}" is not installed in the project\'s folder yet '
            f"(expected {exe}), so its jobs cannot start.",
            hint=f"Install the project's programs first: uv sync --project {root}",
            code="app_not_installed",
        )
    return [str(exe)]


def _job_env(project: Project, allow: str, job: Job | None = None, root: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "MES_ALLOW": allow,
        "MES_DATA_HOME": str(paths.data_home()),
        "MES_STATE_HOME": str(paths.state_home()),
    })
    if root is not None or project.root is not None:
        env["MES_PROJECT_DIR"] = str(root or project.root)
    if job is not None:
        env["MES_JOB"] = job.id
    else:
        env.pop("MES_JOB", None)
    return env


def app_commands(root: Path, project: Project, app: str, exe: list[str]) -> dict[str, str]:
    """Ask the installed app which commands it has: ``{command: kind}``."""
    broken = (f'The program of the app "{app}" did not say which commands it has, so mes cannot check its jobs '
              "and runs none of them.")
    try:
        res = subprocess.run([*exe, "--json", "describe"], cwd=root, env=_job_env(project, "read", root=root),
                             stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MesError(broken, hint=f"Reinstall the project's programs with: uv sync --project {root}",
                       code="app_broken", detail=f"{type(e).__name__}: {e}") from None
    try:
        info = json.loads(res.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        info = None
    if isinstance(info, dict) and isinstance(info.get("error"), dict) and res.returncode != 0:
        said = info["error"]
        raise MesError(broken, hint=str(said.get("hint") or f"Run it by hand to see more: {exe[0]} describe"),
                       code="app_broken", detail=str(said.get("message") or ""), detail_label="What the app said:")
    if res.returncode != 0 or not isinstance(info, dict) or not isinstance(info.get("commands"), list):
        raise MesError(broken, hint=f"Reinstall the project's programs with: uv sync --project {root}",
                       code="app_broken",
                       detail=_tail(res.stderr or res.stdout).strip() or None, detail_label="What the app said:")
    if info.get("app") != app:
        raise Refused(
            f'The program of the app "{app}" says it is the app {str(info.get("app"))[:40]!r}. Running its jobs '
            "could change the wrong data, so mes refuses.",
            hint=f'Use App("{app}") in the app\'s code, the same name as [app] name in its app.toml.',
            code="app_mismatch",
        )
    return {c["name"]: c["kind"] for c in info["commands"]
            if isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("kind"), str)}


def job_problems(project: Project, commands: dict[str, dict[str, str]], jobs: Sequence[Job] | None = None) -> list[str]:
    """Plain sentences for jobs whose command the app does not have, or may not run as a job.

    ``commands`` maps each app name to ``{command: kind}``."""
    out = []
    for job in jobs if jobs is not None else project.jobs.values():
        known = commands.get(job.app, {})
        names = ", ".join(sorted(c for c in known if c not in ("describe", "guide"))) or "none"
        kind = known.get(job.command)
        if kind is None:
            out.append(f'Job "{job.id}" runs "{job.command}" of {job.app}, but {job.app} has no such command '
                       f"(its commands are: {names}).")
        elif kind not in ("read", "write"):
            out.append(f'Job "{job.id}" runs "{job.command}" of {job.app}, which is an {kind} command. Jobs run '
                       'with "write" access, so it would always be refused: jobs may only run read and write '
                       "commands.")
    return out


def _refuse_bad_jobs(problems: list[str]) -> None:
    if problems:
        raise MesError(
            " ".join(problems) + " Nothing was run.",
            hint="Change the job's run line in the app's app.toml to one of its commands (see: mes jobs list).",
            code="job_invalid",
        )


def _state_dir(project: Project, create: bool = False) -> Path:
    return paths.project_state_dir(project.name, create=create)


def _log_path(project: Project) -> Path:
    return _state_dir(project) / RUN_LOG_FILE


def _size_words(size: str) -> str:
    return f"{size[:-1]} {size[-1]}B"


def _duration_words(text: str) -> str:
    n, unit = int(text[:-1]), text[-1]
    word = {"s": "second", "m": "minute", "h": "hour"}[unit]
    return f"{n} {word}{'' if n == 1 else 's'}"


def _network_words(job: Job) -> str:
    if not job.network:
        return "has no internet access"
    return (f"may reach only {', '.join(job.network)} on a server set up with mes server plan (on this computer, "
            "when run by hand, it has full internet access)")


def _when(job: Job) -> str:
    if job.trigger == "schedule":
        return f"on schedule: {job.schedule}"
    if job.trigger == "after":
        return f'after "{job.after}" succeeds'
    if job.trigger == "webhook":
        return "when a signed webhook arrives (on a server), or when asked"
    return f"only when asked (mes jobs run {job.id})"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def describe_job(job: Job) -> str:
    return (f'Job "{job.id}" runs "{job.run}" {_when(job)}; it {_network_words(job)}, '
            f"may use up to {_size_words(job.memory)} of memory and is stopped after "
            f"{_duration_words(job.timeout)}.")


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
def _job_lock(project: Project, job: Job) -> Iterator[None]:
    lock_dir = _state_dir(project, create=True) / "locks"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(lock_dir / f"{job.id}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TemporaryFailure(
                f'The job "{job.id}" is already running, so it was not started a second time '
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


def _run_one(root: Path, project: Project, job: Job, exe: list[str], via: str = "mes jobs run") -> dict[str, Any]:
    # --json goes before the command, so it never mixes with the job's own arguments.
    argv = [*exe, "--json", *job.argv]
    env = _job_env(project, units.JOB_ALLOW_LEVEL, job, root)
    started = _now()
    t0 = time.monotonic()
    out = err = b""
    exit_code: int | None = None
    result = "interrupted"  # what the log says if mes itself is stopped while the job runs
    record: dict[str, Any] = {}
    with _job_lock(project, job):
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
            record = append_record(_log_path(project), {
                "time": started,
                "project": project.name,
                "app": job.app,
                "job": job.id,
                "via": via,
                "argv": argv,
                "exit_code": exit_code,
                "result": result,
                "duration_s": duration,
            })
    return {
        "job": job.id,
        "app": job.app,
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


def _failure(run: dict[str, Any], job: Job, done: list[str], *, project: Project, chain: bool) -> MesError:
    already = f" Jobs that finished before it: {', '.join(done)}." if done else ""
    waiting = [j.id for j in project.dependents(job.id)] if chain else []
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
    by_hand = (f"To see it yourself, run this in the project's folder: uv run "
               f"{project.apps[job.app].program if job.app in project.apps else job.app} {job.run}")
    if run["result"] == "timeout":
        return MesError(
            f'The job "{job.id}" took longer than its time limit ({_duration_words(job.timeout)}) '
            f"and was stopped, so its work may be unfinished.{already}{held}",
            hint=f"If it really needs more time, raise it in project.toml with [settings.{job.app}] "
            f'jobs.{job.name}.timeout = "..." (your assistant can do this).',
            code="job_timeout", detail=detail, detail_label=label,
        )
    if run["result"] == "start-failed":
        return MesError(
            f'The job "{job.id}" could not be started, so nothing was done.{already}{held}',
            hint="Reinstall the app's program with: uv sync",
            code="job_start_failed", detail=detail, detail_label=label,
        )
    if run["exit_code"] == 75:
        return TemporaryFailure(
            f'The job "{job.id}" hit a temporary problem (for example the network was down) and '
            f"stopped.{already}{held}",
            hint=f"Try again later: mes jobs run {job.id}",
            code="job_temporary", detail=detail, detail_label=label,
        )
    how = "stopped with an error" if run["exit_code"] is not None else "was stopped by the system before it finished"
    return MesError(
        f'The job "{job.id}" {how}, so its work is not done.{already}{held}',
        hint=by_hand,
        code="job_failed", detail=detail, detail_label=label,
    )


def _chain(project: Project, first: Job, chain: bool) -> list[Job]:
    out, queue = [], [first]
    while queue:
        job = queue.pop(0)
        out.append(job)
        if chain:
            queue.extend(project.dependents(job.id))
    return out


def _sync_schema(project: Project) -> None:
    """Bring the shared database in line with project.toml before a job that writes data (the
    server does the same with ExecStartPre=mes db sync)."""
    from mes import db

    db.sync(project)


def rebuild_apps(root: Path, project: Project, *, app: str | None = None) -> list[dict[str, Any]]:
    """``mes db rebuild``: each app's ``[data] rebuild`` command, in apps order, like a job."""
    specs = list(project.apps.values())
    if app is not None:
        spec = project.app_for_word(app)
        if spec is None:
            raise NotFound(f'The project "{project.name}" has no app called "{app}".',
                           hint=f"Its apps are: {', '.join(project.apps)}.")
        specs = [spec]
    chosen = [s for s in specs if s.data.rebuild]
    if app is not None and not chosen:
        raise NotFound(f"The app {specs[0].name} has no [data] rebuild command, so there is nothing to rebuild.",
                       hint='Add rebuild = "<write command>" under [data] in its app.toml.', code="no_rebuild")
    exes = {s.name: app_command(root, project, s.name) for s in chosen}
    runs = []
    for spec in chosen:
        kinds = app_commands(root, project, spec.name, exes[spec.name])
        kind = kinds.get(spec.data.rebuild or "")
        if kind != "write":
            raise MesError(
                f'The [data] rebuild command "{spec.data.rebuild}" of {spec.name} '
                + ("does not exist." if kind is None else f"is a {kind} command, not a write command.")
                + " Nothing more was rebuilt.",
                hint="Fix [data] rebuild in the app's app.toml.", code="job_invalid")
        existing = project.jobs.get(f"{spec.name}.{spec.data.rebuild}")
        job = Job(id=f"{spec.name}.{spec.data.rebuild}", name=str(spec.data.rebuild), app=spec.name,
                  run=str(spec.data.rebuild), argv=(str(spec.data.rebuild),), trigger="manual",
                  timeout=existing.timeout if existing else manifest_mod.DEFAULT_TIMEOUT)
        if project.tables:
            _sync_schema(project)
        run = _run_one(root, project, job, exes[spec.name], via="mes db rebuild")
        run.pop("_record", None)
        app_error = run.pop("_app_error", None)
        if run["result"] != "success":
            raise _failure({**run, "_app_error": app_error}, job, [r["job"] for r in runs], project=project, chain=False)
        runs.append({**run, "command": spec.data.rebuild})
    return runs


def run_job(root: Path, project: Project, job_name: str, *, chain: bool = True) -> list[dict[str, Any]]:
    first = project.job(job_name)
    planned = _chain(project, first, chain)
    exes = {name: app_command(root, project, name) for name in dict.fromkeys(j.app for j in planned)}
    commands = {name: app_commands(root, project, name, exe) for name, exe in exes.items()}
    _refuse_bad_jobs(job_problems(project, commands, planned))
    runs: list[dict[str, Any]] = []
    for job in planned:
        if "data" in job.writes and project.tables:
            _sync_schema(project)
        run = _run_one(root, project, job, exes[job.app])
        run.pop("_record", None)
        app_error = run.pop("_app_error", None)
        if run["result"] != "success":
            raise _failure({**run, "_app_error": app_error}, job, [r["job"] for r in runs],
                           project=project, chain=chain)
        runs.append(run)
    return runs


# --------------------------------------------------------------------------- server helpers (root)


_INVOCATION_RE = re.compile(r"^[0-9a-f]{32}\Z")


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=60, check=False,
                          stdin=subprocess.DEVNULL)


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


def _not_from_systemd() -> Refused:
    return Refused(
        "This command only records a run that systemd itself has just finished, and this one was "
        "not started by systemd, so nothing was added to the run log. (Otherwise anyone could add "
        "made-up runs to the history.)",
        hint="To run a job yourself, use: mes jobs run <job>",
        code="not_from_systemd",
    )


def record_systemd_run(project_name: str, job_name: str, server_root: str | Path) -> dict[str, Any]:
    """Called as root by systemd (ExecStopPost=+) after each run. Only names come in."""
    layout = paths.server_layout(project_name, server_root)
    if not manifest_mod.JOB_ID_RE.fullmatch(job_name or ""):
        raise UsageError(f"{str(job_name)[:70]!r} is not a job id.", hint="Use the job's id: <app>.<job>.")
    unit = units.job_unit(project_name, job_name)
    invocation = os.environ.get("INVOCATION_ID", "")
    if not _is_linux() or not _INVOCATION_RE.match(invocation) or not os.environ.get("SERVICE_RESULT"):
        raise _not_from_systemd()
    if _show(unit, "InvocationID").get("InvocationID") != invocation:
        raise _not_from_systemd()
    result = os.environ.get("SERVICE_RESULT") or "unknown"
    exit_kind = os.environ.get("EXIT_CODE")
    exit_status = os.environ.get("EXIT_STATUS")
    exit_code = int(exit_status) if exit_kind == "exited" and (exit_status or "").isdigit() else None
    duration = None
    props = _show(unit, "ExecMainStartTimestampMonotonic", "ExecMainExitTimestampMonotonic")
    try:
        start = int(props.get("ExecMainStartTimestampMonotonic", ""))
        end = int(props.get("ExecMainExitTimestampMonotonic", ""))
        if start > 0 and end >= start:
            duration = round((end - start) / 1_000_000, 3)
    except ValueError:
        pass
    log = layout.run_log
    # The log folder belongs to root, so the project's account cannot put a link in its place.
    try:
        st = os.lstat(log.parent)
    except OSError:
        st = None
    if st is None or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise Refused("The run log folder does not belong to the account writing the log, so nothing was written.",
                      hint="Run 'mes server plan' and its script again to repair the folders.", code="unsafe_folder")
    rec = append_record(log, {
        "time": _now(),
        "project": project_name,
        "job": job_name,
        "via": "systemd",
        "unit": unit,
        "exit_code": exit_code,
        "result": result,
        "duration_s": duration,
    })
    return {k: rec[k] for k in ("time", "job", "result", "exit_code", "duration_s")}


def nameservers(path: str = "/etc/resolv.conf") -> list[str]:
    out = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines()[:200]:
        words = line.split()
        if len(words) >= 2 and words[0] == "nameserver":
            try:
                out.append(str(ipaddress.ip_address(words[1].split("%", 1)[0])))
            except ValueError:
                continue
    return list(dict.fromkeys(out))


def resolve_hosts(hosts: Sequence[str], *, resolver: Callable[..., Any] | None = None) -> list[str]:
    """Every address the hosts have now (IPv4 and IPv6), validated as addresses."""
    look = resolver or socket.getaddrinfo
    found: list[str] = []
    for host in units.check_hosts(list(hosts)):
        try:
            infos = look(host, 443, 0, socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            continue
        for info in infos[:64]:
            try:
                addr = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
            except (ValueError, IndexError, TypeError):
                continue
            if addr.is_unspecified or addr.is_multicast:
                continue
            found.append(str(addr))
    return list(dict.fromkeys(found))


def allow_network(unit: str, hosts: Sequence[str], *, resolver: Callable[..., Any] | None = None,
                  resolv_conf: str = "/etc/resolv.conf", run: Callable[..., Any] | None = None) -> dict[str, Any]:
    if not isinstance(unit, str) or not units.JOB_UNIT_RE.fullmatch(unit):
        raise UsageError(f"{str(unit)[:80]!r} is not the unit of a mes job.", hint="Use mes-<project>.job.<app>.<job>.service")
    if not hosts:
        raise UsageError("No hosts were given, so nothing would be allowed.", hint="List the job's hosts after --.")
    addresses = resolve_hosts(hosts, resolver=resolver)
    if not addresses:
        raise TemporaryFailure(
            f"None of the job's hosts ({', '.join(hosts)}) could be looked up, so the job was not allowed to reach "
            "anything and will not start this time.",
            hint="Check the server's internet connection; the job runs again at its next time.",
            code="dns_failed",
        )
    allowed = addresses + [a for a in nameservers(resolv_conf) if a not in addresses]
    argv = ["systemctl", "set-property", "--runtime", unit, "IPAddressAllow=", "IPAddressAllow=" + " ".join(allowed)]
    runner = run or (lambda a: subprocess.run(a, capture_output=True, text=True, timeout=60, check=False,
                                              stdin=subprocess.DEVNULL))
    res = runner(argv)
    if getattr(res, "returncode", 1) != 0:
        raise MesError(
            "systemd did not accept the list of addresses the job may reach, so the job will not start this time.",
            hint="Run 'mes server check <project>' on the server.",
            code="systemd_failed",
            detail=(getattr(res, "stderr", "") or "").strip()[:500] or None,
        )
    return {"unit": unit, "hosts": list(hosts), "allowed": allowed}


# --------------------------------------------------------------------------- command handlers


def _cmd_list(args: argparse.Namespace) -> Any:
    check_allowed("read")
    root, project = load_project(args.project_dir)
    warnings = _check_warnings(root, project)
    if args.json:
        return {"project": project.name, "jobs": [{**j.to_dict(), "when": _when(j)} for j in project.jobs.values()],
                "disabled": list(project.disabled), "warnings": warnings}
    if not project.jobs:
        return f'The project "{project.name}" has no jobs yet. Add one to an app\'s app.toml (see: mes guide jobs).'
    rows = [
        {
            "job": j.id,
            "when": _when(j),
            "runs": j.run,
            "memory": _size_words(j.memory),
            "time limit": _duration_words(j.timeout),
            "internet": ("only " + ", ".join(j.network) + " (on a server)") if j.network else "none",
            "may change": " and ".join(j.writes) if j.writes else "nothing",
        }
        for j in project.jobs.values()
    ]
    text = protocol.format_text(rows)
    if any(j.network for j in project.jobs.values()):
        text += "\n\n" + manifest_mod.NETWORK_NOTE_LOCAL
    if project.disabled:
        text += "\n\nSwitched off in project.toml ([settings]): " + ", ".join(project.disabled)
    if warnings:
        text += "\n\nPlease note:\n" + "\n".join(f"- {w}" for w in warnings)
    return text


def _check_warnings(root: Path, project: Project) -> list[str]:
    """Warnings (not errors) about jobs, for commands that only show things."""
    out: list[str] = []
    commands: dict[str, dict[str, str]] = {}
    for name in dict.fromkeys(j.app for j in project.jobs.values()):
        exe = program(root, project, name)
        if not (exe.is_file() and os.access(exe, os.X_OK)):
            continue
        try:
            commands[name] = app_commands(root, project, name, [str(exe)])
        except MesError as e:
            out.append(e.message)
    jobs = [j for j in project.jobs.values() if j.app in commands]
    return out + job_problems(project, commands, jobs)


def _cmd_run(args: argparse.Namespace) -> Any:
    check_allowed("write")
    root, project = load_project(args.project_dir)
    runs = run_job(root, project, args.job, chain=not args.no_chain)
    if args.json:
        return {"project": project.name, "ok": True, "runs": runs}
    lines = []
    for r in runs:
        lines.append(f'Job "{r["job"]}" finished successfully in {r["duration_s"]} seconds.')
        shown = protocol.format_text(r["data"]) if r.get("data") is not None else r["output"]
        if shown.strip():
            lines.append("What it reported:\n" + shown.rstrip())
    return "\n".join(lines)


def _cmd_log(args: argparse.Namespace) -> Any:
    check_allowed("read")
    if args.server_root:
        if not args.project:
            raise UsageError("--server-root needs --project <name>.", hint="For example: mes jobs log --project myproject --server-root /var/lib/mes")
        path = paths.server_layout(args.project, args.server_root).run_log
        name = args.project
    else:
        _, project = load_project(args.project_dir)
        path, name = _log_path(project), project.name
    if args.verify:
        result = verify_log(path)
        if args.json:
            return result
        return f"{result['message']}\n{result['note']}"
    if args.limit < 1:
        raise UsageError("--limit must be 1 or more.", hint="For example: mes jobs log --limit 20")
    recs = [r for r in read_log(path) if args.job is None or r.get("job") == args.job
            or str(r.get("job", "")).endswith("." + args.job)][-args.limit:]
    if args.json:
        return {"project": name, "log": str(path), "runs": recs}
    if not recs:
        return "No job has run yet."
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
_VIA_WORDS = {"mes jobs run": "by hand", "systemd": "on the server", "mes db rebuild": "by hand (mes db rebuild)"}


def _cmd_status(args: argparse.Namespace) -> Any:
    check_allowed("read")
    _, project = load_project(args.project_dir)
    last: dict[str, dict[str, Any]] = {}
    for rec in read_log(_log_path(project)):
        if isinstance(rec.get("job"), str):
            last[rec["job"]] = rec
    rows = []
    for job in project.jobs.values():
        rec = last.get(job.id, {})
        rows.append({"job": job.id, "when": _when(job), "last_run": rec.get("time"),
                     "last_result": rec.get("result"), "last_exit_code": rec.get("exit_code"),
                     "last_duration_s": rec.get("duration_s")})
    if args.json:
        return {"project": project.name, "jobs": rows,
                "note": "Runs on this computer only. On a server: mes server check <project>, and mes jobs log "
                "--project <project> --server-root /var/lib/mes"}
    text = protocol.format_text([{
        "job": r["job"], "when": r["when"], "last run": r["last_run"] or "never",
        "last result": _RESULT_WORDS.get(r["last_result"], r["last_result"]) if r["last_result"] else "-",
    } for r in rows])
    return text + "\n\nThese are the runs on this computer. Scheduled runs happen on a server set up with: mes server plan"


def _cmd_install(args: argparse.Namespace) -> Any:
    raise MesError(
        "Scheduled jobs now run as system services under the project's own accounts on a Linux server, so "
        "'mes jobs install' no longer installs anything for your own account.",
        hint="Run 'mes server plan <project>' on the server: it writes one readable script that you run once "
        "with sudo, and 'mes server check <project>' confirms the result. See: mes guide server",
        code="moved",
    )


def _cmd_record(args: argparse.Namespace) -> Any:
    check_allowed("write")
    return record_systemd_run(args.project, args.job, args.server_root)


def _cmd_allow_network(args: argparse.Namespace) -> Any:
    check_allowed("admin")
    return allow_network(args.unit, args.hosts)


def _project_dir_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--project-dir", metavar="DIR", default=None,
                   help="the project's folder (default: the project in the current folder)")


def register(subparsers: Any) -> None:
    jobs = subparsers.add_parser(
        "jobs",
        help="List, run and log a project's jobs.",
        description="Jobs are short runs of an app's own commands, declared in the app's app.toml (ids "
        "<app>.<job>); the project can change them in [settings.<app>]. See: mes guide jobs",
    )
    sub = jobs.add_subparsers(dest="jobs_command", metavar="<action>")

    p = sub.add_parser("list", help="The project's jobs, when they run and their limits.")
    _project_dir_arg(p)
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("run", help="Run a job now on this computer (then the jobs set to run after it).")
    p.add_argument("job", help="the job's id (<app>.<job>), or its name when only one app has a job of that name")
    p.add_argument("--no-chain", action="store_true", help="do not start the jobs that run after this one")
    _project_dir_arg(p)
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("log", help="Recent job runs; --verify checks that the log was not changed.")
    p.add_argument("--job", default=None, help="only this job (its id or name)")
    p.add_argument("--limit", type=int, default=20, help="how many runs to show (default 20)")
    p.add_argument("--verify", action="store_true", help="check the whole log's hash chain")
    p.add_argument("--project", default=None, help="with --server-root: the project's name")
    p.add_argument("--server-root", default=None, metavar="DIR",
                   help="read the server's run log (for example /var/lib/mes) instead of this computer's")
    _project_dir_arg(p)
    p.set_defaults(func=_cmd_log)

    p = sub.add_parser("status", help="When each job last ran on this computer, and whether it worked.")
    _project_dir_arg(p)
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("install", help="(Moved) Scheduling now happens on a server: see 'mes server plan'.")
    p.set_defaults(func=_cmd_install, _accept_extra=True)

    p = sub.add_parser("record", help="(Used by the server's system units after each run.) Add the run to the log.")
    p.add_argument("--project", required=True, help="the project's name")
    p.add_argument("--server-root", required=True, metavar="DIR", help="the server folder, /var/lib/mes")
    p.add_argument("job", help="the job's id (<app>.<job>)")
    p.set_defaults(func=_cmd_record)

    p = sub.add_parser("allow-network", help="(Used by the server's system units before a run.) Let a job reach "
                       "only its hosts.")
    p.add_argument("--unit", required=True, help="the job's unit, mes-<project>.job.<app>.<job>.service")
    p.add_argument("hosts", nargs="+", help="the host names the job may reach")
    p.set_defaults(func=_cmd_allow_network)
