"""``mes gate``: the single entry point for AI agents and deploys that connect over SSH.

On a server, every key in ``/var/lib/mes/<project>/home/.ssh/authorized_keys`` is tied to a fixed
command, so it can never open a shell (``mes server plan`` writes these lines)::

    restrict,command="/usr/local/bin/mes gate --project myproject --allow read --server-root /var/lib/mes" ssh-ed25519 AAAA...

On a personal machine the project comes from the owner's list (``mes projects add``) and
``--server-root`` is left out.

The client sends one JSON array of strings as its SSH command::

    ssh mes-myproject@my-server '["myapp", "summary", "--days", "7", "--json"]'

SSH puts that text in ``SSH_ORIGINAL_COMMAND``. The gate:

1. Accepts only a JSON array of strings: ``[app, command, *args]`` for an app of the project,
   ``[project, short-or-app-name, command, *args]`` (rewritten to the first form),
   ``[project, "describe" | "guide" | "apps"]`` (the project command's read built-ins, only
   ``--json`` may follow), ``[project, word, ...]`` for a project with one app (passed to it), or
   ``["mes", <operation>, ...]`` for the gate's own deploy operations. No shell, no shell
   parsing; at most 64 items, each at most 4096 bytes, no NUL bytes. Anything else: exit 2.
2. Finds the project in the server layout (``--server-root``; the gate must run as the account
   that owns the project's ``code`` folder) or in the owner's list, never in the request. The
   app must be one of the project's installed apps; its program is
   ``<project>/.venv/bin/<program>``. ``project.toml``, ``apps.lock.json``, every ``app.toml``
   and the program must be changeable only by the gate's account (or root). An app command
   always runs the app's own program, never the project command.
3. Asks the installed app for its commands and their kinds, fresh on every request.
4. Levels: ``read`` runs read commands; ``write`` also write commands; ``deploy`` also the
   ``mes`` operations (``deploy``, ``rollback``, ``releases``, ``status``; see ``mes.deploy``).
   ``admin`` commands never run through the gate, and ``--allow admin`` is refused.
5. Runs each request in its own sandbox: on a server set up by ``mes server plan`` the request
   is handed to a fresh, socket-activated system service instance
   (``mes-<p>.gate-<level>@.service``, see ``mes.units.render_gate``) that runs as the project's
   account with the full sandbox (no new privileges, read-only system, no home folders, private
   /tmp and /dev, no capabilities, ``@system-service`` system calls, no network, memory and task
   limits; read requests see data and state read-only). That instance checks the request again
   and runs the program. Where those sockets do not exist (a personal machine, a Mac, a server
   not set up yet), the gate runs the program itself with the limits of v0.1 only: time,
   output, CPU time, file size, no core files, address space (Linux), and says so in its log.
   If a server's socket exists but cannot be used, the request is refused (exit 75), never run
   with less protection.
6. Every request runs with a small fixed environment (fixed ``PATH``, ``MES_ALLOW``, the
   project's folders, no SSH agent, no tokens, no ``PYTHON*`` from outside), no input (except
   the deploy archive), a time limit and an output limit; at most ``MAX_PARALLEL`` run at once
   (exit 75 when busy).
7. Writes one JSON line per request to ``<state>/gate/gate-log.jsonl`` before running anything
   (and one when it finishes). If that log cannot be written, nothing is run.

Any unexpected problem refuses the request (fails closed). Errors are JSON on stdout when the
request or the gate command contains ``--json`` (always for ``mes`` operations); otherwise
plain text on stderr.

Public API
----------
``MAX_ITEMS``, ``MAX_ARG_BYTES``, ``MAX_REQUEST_BYTES``, ``DEFAULT_TIMEOUT``,
``DEFAULT_MAX_OUTPUT``, ``SAFE_PATH``, ``GATE_LEVELS`` (``read``, ``write``, ``deploy``).
``Outcome(exit_code, stdout, stderr)``, ``Finished(...)``.
``GateProject``  where the request's project lives (``locate(project, server_root)``).
``parse_request(text) -> list[str]``
``resolve_request(where, request) -> (request, project, program, builtin)``
``clean_env(allow, where=None) -> dict``
``run_limited(argv, *, env, timeout, max_output, max_stderr, stdin=None, cwd="/") -> Finished``
``ask_commands(program, app, *, timeout, env=None) -> dict[str, str]``
``handle(raw, allow, *, project, server_root=None, json_mode=False, timeout=..., max_output=...,
  source="option", stdin=None) -> Outcome``  the whole gate; never raises.
``child_main(project, server_root, level, stdin, stdout) -> int``  the per-request instance.
``register(subparsers)``  adds ``mes gate`` (and ``mes projects`` from ``mes.registry``).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as _dt
import fcntl
import io
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

from mes import manifest as manifest_mod
from mes import paths, protocol, registry, units
from mes.app import KINDS
from mes.errors import (
    MesError,
    NotAllowed,
    NotFound,
    Refused,
    TemporaryFailure,
    UsageError,
)

MAX_ITEMS = 64
MAX_ARG_BYTES = 4096
MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 3600.0
DEFAULT_MAX_OUTPUT = 8 * 1024 * 1024
MAX_STDERR = 64 * 1024
DESCRIBE_TIMEOUT = 20.0
DESCRIBE_MAX_OUTPUT = 1024 * 1024
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
GATE_LEVELS = ("read", "write", "deploy")
GATE_DIR = "gate"
LOG_FILE = "gate-log.jsonl"
MEMORY_LIMIT = 2 * 1024**3
FILE_SIZE_LIMIT = 1024**3
CPU_GRACE = 5
MAX_PARALLEL = 4
SLOTS_DIR = "slots"
MES_WORD = "mes"
MES_OPERATIONS = ("deploy", "rollback", "releases", "status")
PROJECT_BUILTINS = ("describe", "guide", "apps")

_COMMAND_RE = re.compile(r"[a-z][a-z0-9-]{0,40}")
_KEEP_ENV_PERSONAL = (
    "HOME", "USER", "LOGNAME", "TZ",
    "MES_DATA_HOME", "MES_STATE_HOME", "MES_CONFIG",
    "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
)
_KEEP_ENV_SERVER = ("USER", "LOGNAME", "TZ")


@dataclasses.dataclass
class Outcome:
    exit_code: int
    stdout: bytes = b""
    stderr: bytes = b""


@dataclasses.dataclass
class Finished:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    too_big: bool = False
    isolation: str = "limits"


# --------------------------------------------------------------------------- where the project is


@dataclasses.dataclass(frozen=True)
class GateProject:
    name: str
    folder: Path            # the project's own files (project.toml, .venv)
    data: Path
    state: Path
    store: Path
    layout: paths.ServerLayout | None = None

    @property
    def server(self) -> bool:
        return self.layout is not None

    @property
    def gate_dir(self) -> Path:
        return self.state / GATE_DIR

    def env(self) -> dict[str, str]:
        out = {
            "MES_PROJECT": self.name,
            "MES_PROJECT_DIR": str(self.folder),
            "MES_DATA_DIR": str(self.data),
            "MES_STATE_DIR": str(self.state),
            "MES_STORE_DIR": str(self.store),
        }
        if self.server:
            out["HOME"] = str(self.state)
        return out


def locate(project: str, server_root: str | os.PathLike[str] | None) -> GateProject:
    if not registry.valid_name(project):
        raise UsageError(f"{str(project)[:40]!r} is not a project name, so the gate cannot start.",
                         hint="Set up the key with: mes gate --project <name> --allow read")
    if server_root is None:
        folder = registry.lookup(project, public=True)
        return GateProject(project, folder, paths.project_data_dir(project), paths.project_state_dir(project),
                           paths.project_store_dir(project))
    layout = paths.server_layout(project, server_root)
    try:
        st = os.lstat(layout.code)
    except OSError:
        raise NotFound(f'The project "{project}" is not set up on this server, so nothing was run.',
                       hint="Ask the owner to run 'mes server plan' and its script for this project.") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise Refused(
            f'The gate for "{project}" is not running as the project\'s own account, so it refuses to run anything '
            "(it could otherwise reach another project's data).",
            hint="Ask the owner to check the key setup with: mes server check " + project,
            code="wrong_account",
        )
    return GateProject(project, layout.current_project, layout.data, layout.state, layout.store, layout)


def _load_checked(where: GateProject) -> manifest_mod.Project:
    if where.server and not (where.folder / manifest_mod.MANIFEST_FILE).is_file():
        raise NotFound(f'Nothing has been deployed for "{where.name}" yet, so there is nothing to run.',
                       hint="Deploy it first with: mes deploy <server>")
    registry.check_project_file(where.folder, public=True)
    registry.check_lock_file(where.folder, public=True)
    project = manifest_mod.load(where.folder)
    if project.name != where.name:
        raise Refused(f'The folder set up for "{where.name}" holds a different project, so nothing was run.',
                      hint="Ask the owner to check the project's setup.", code="wrong_project")
    registry.check_app_files(project, public=True)
    return project


def _project_and_program(where: GateProject, app: str) -> tuple[manifest_mod.Project, Path]:
    project = _load_checked(where)
    if app not in project.apps:
        raise NotFound(f'The project "{where.name}" has no app called "{app}", so nothing was run.',
                       hint=f"Its apps are: {', '.join(project.apps)}.")
    program = registry.check_program(registry.program_path(where.folder, project.apps[app].program), app=app)
    return project, program


def resolve_request(where: GateProject, request: list[str]) -> tuple[list[str], manifest_mod.Project, Path, bool]:
    """``(request, project, program, builtin)``. A request that starts with the project's name is
    rewritten to the app form; ``[project, describe|guide|apps]`` runs the project command."""
    if request[0] != where.name:
        project, program = _project_and_program(where, request[0])
        return request, project, program, False
    project = _load_checked(where)
    word = request[1]
    if word in PROJECT_BUILTINS:
        if any(a != "--json" for a in request[2:]):
            raise _bad_request(f'"{where.name} {word}" takes nothing else through the gate (only --json).',
                               hint=f'Send ["{where.name}", "{word}", "--json"].')
        program = registry.check_program(registry.program_path(where.folder, where.name), app=where.name)
        return request, project, program, True
    spec = project.app_for_word(word)
    if spec is not None:
        rewritten = [spec.name, *request[2:]]
    elif len(project.apps) == 1:
        rewritten = [next(iter(project.apps)), *request[1:]]
    else:
        shorts = [a.short for a in project.apps.values()]
        raise UsageError(
            f'{where.name} has several apps, so "{_short(word)}" does not say which one is meant.',
            hint=f"Start with the app's short name: {', '.join(shorts)}. For example: "
            f'["{where.name}", "{shorts[0]}", "{_short(word)}"].',
            code="bad_request",
        )
    rewritten = parse_request(json.dumps(rewritten))
    program = registry.check_program(registry.program_path(where.folder, project.apps[rewritten[0]].program),
                                     app=rewritten[0])
    return rewritten, project, program, False


# --------------------------------------------------------------------------- request


def _bad_request(message: str, hint: str | None = None) -> UsageError:
    return UsageError(
        message,
        hint=hint or ("Send the command as a JSON list of words: the app's name, then its command, then "
                      'options, for example ["myapp", "describe", "--json"] (where myapp is the app\'s name). '
                      "See: mes guide gate"),
        code="bad_request",
    )


def parse_request(text: Any) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        raise _bad_request(
            "No command was given, so the gate has nothing to run. This connection can only run app "
            "commands; it never opens a shell."
        )
    if len(text.encode("utf-8", "surrogatepass")) > MAX_REQUEST_BYTES:
        raise _bad_request("The request is too large, so it was refused without being read.")
    if "\x00" in text:
        raise _bad_request("The request contains a NUL byte, which is never allowed, so it was refused.")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise _bad_request(
            "The request is not a JSON list, so it was refused. The gate never reads shell command "
            "lines, so quoting tricks and shell symbols cannot work."
        ) from None
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise _bad_request("The request must be a JSON list of text items only, so it was refused.")
    moved = False
    while len(data) > 1 and data[1] == "--json":
        del data[1]
        moved = True
    if moved:
        data.append("--json")
    if len(data) < 2:
        raise _bad_request(
            "The request must name an app and one of its commands, so it was refused.",
            hint='For example: ["myapp", "describe", "--json"], where myapp is the app\'s name.',
        )
    if len(data) > MAX_ITEMS:
        raise _bad_request(f"The request has {len(data)} items; at most {MAX_ITEMS} are allowed, so it was refused.")
    for i, item in enumerate(data):
        if "\x00" in item:
            raise _bad_request(f"Item {i + 1} contains a NUL byte, which is never allowed, so it was refused.")
        try:
            size = len(item.encode("utf-8"))
        except UnicodeEncodeError:
            raise _bad_request(f"Item {i + 1} is not valid text, so the request was refused.") from None
        if size > MAX_ARG_BYTES:
            raise _bad_request(
                f"Item {i + 1} is {size} bytes long; at most {MAX_ARG_BYTES} are allowed, so it was refused."
            )
    app, command = data[0], data[1]
    if app != MES_WORD and not registry.valid_name(app):
        raise _bad_request(
            f"{_short(app)!r} is not an app name, so the request was refused.",
            hint='App names are lowercase letters, digits and dashes, like "myapp".',
        )
    if not _COMMAND_RE.fullmatch(command):
        raise _bad_request(
            f"{_short(command)!r} is not a command name, so the request was refused. The second item must "
            "be the app command itself (options come after it).",
            hint=f'Ask the app for its commands with: ["{app}", "describe", "--json"]',
        )
    return list(data)


def _short(value: Any, limit: int = 64) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    return value


# --------------------------------------------------------------------------- running


def clean_env(allow: str, where: GateProject | None = None) -> dict[str, str]:
    keep = _KEEP_ENV_SERVER if where is not None and where.server else _KEEP_ENV_PERSONAL
    env = {k: os.environ[k] for k in keep if os.environ.get(k)}
    env.update({
        "PATH": SAFE_PATH,
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MES_ALLOW": "write" if allow == "deploy" else allow,
        "MES_VIA_GATE": "1",
    })
    if where is not None:
        env.update(where.env())
    return env


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _limits(timeout: float) -> list[tuple[int, int]]:
    import resource

    cpu = int(timeout) + CPU_GRACE
    out = [(resource.RLIMIT_CPU, cpu), (resource.RLIMIT_FSIZE, FILE_SIZE_LIMIT), (resource.RLIMIT_CORE, 0)]
    if sys.platform.startswith("linux"):
        out.append((resource.RLIMIT_AS, MEMORY_LIMIT))
    return out


def _apply_limits(limits: list[tuple[int, int]]) -> Any:
    def preexec() -> None:  # runs in the child, between fork and exec
        import resource

        for which, value in limits:
            try:
                _, hard = resource.getrlimit(which)
                new = value if hard == resource.RLIM_INFINITY else min(value, hard)
                resource.setrlimit(which, (new, new))
            except (ValueError, OSError):
                pass

    return preexec


@contextlib.contextmanager
def _slot(folder: Path) -> Iterator[None]:
    """One of MAX_PARALLEL places; none free means the computer is busy (exit 75)."""
    for i in range(MAX_PARALLEL):
        try:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(folder / f"slot-{i}.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            raise MesError(
                "The gate could not use its own folder (to count and record requests), and it never "
                "runs anything it cannot record, so nothing was run.",
                hint="Ask the owner to check that the project's state folder exists and can be written.",
                code="log_unavailable",
            ) from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        try:
            yield
        finally:
            os.close(fd)
        return
    raise TemporaryFailure(
        f"This computer is already answering {MAX_PARALLEL} requests, so this one was "
        "not started (too many at once could slow it down for everyone).",
        hint="Try again in a moment.",
        code="busy",
    )


def run_limited(
    argv: list[str],
    *,
    env: dict[str, str],
    timeout: float,
    max_output: int,
    max_stderr: int = MAX_STDERR,
    cwd: str = "/",
) -> Finished:
    """Run ``argv`` (no shell, no input), keeping at most ``max_output`` bytes of stdout.

    Going over the stdout limit or the time limit stops the program and its children. Stderr
    beyond ``max_stderr`` is dropped.
    """
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            close_fds=True,
            start_new_session=True,
            shell=False,
            preexec_fn=_apply_limits(_limits(timeout)),
        )
    except (OSError, subprocess.SubprocessError):
        raise MesError(
            "The program could not be started, so nothing was run.",
            hint="Ask the owner to check that the project is installed correctly.",
            code="app_unavailable",
        ) from None

    state = {"too_big": False}
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []

    def pump(stream: Any, chunks: list[bytes], cap: int, is_stdout: bool) -> None:
        total = 0
        while True:
            block = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not block:
                break
            if total + len(block) > cap:
                chunks.append(block[: max(cap - total, 0)])
                total = cap
                if is_stdout:
                    state["too_big"] = True
                    _kill(proc)
                    break
                continue  # stderr: keep draining, drop the rest
            chunks.append(block)
            total += len(block)
        stream.close()

    readers = [
        threading.Thread(target=pump, args=(proc.stdout, out_chunks, max_output, True), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, err_chunks, max_stderr, False), daemon=True),
    ]
    for t in readers:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(proc)
        proc.wait()
    except BaseException:
        _kill(proc)
        proc.wait()
        raise
    _kill(proc)  # children that kept the pipes open are stopped too
    for t in readers:
        t.join(timeout=5)
    return Finished(proc.returncode, b"".join(out_chunks), b"".join(err_chunks), timed_out, state["too_big"])


def ask_commands(program: str | Path, app: str, *, timeout: float = DESCRIBE_TIMEOUT,
                 env: dict[str, str] | None = None) -> dict[str, str]:
    """Ask the installed program which commands it has; returns command -> kind."""
    broken = MesError(
        f'The app "{app}" did not describe itself correctly, so the gate cannot tell which commands are '
        "safe and refuses to run anything from it.",
        hint=f"Ask the owner to check the app: {app} --json describe",
        code="app_unavailable",
    )
    done = run_limited([str(program), "--json", "describe"], env=env or clean_env("read"),
                       timeout=timeout, max_output=DESCRIBE_MAX_OUTPUT, max_stderr=4096)
    if done.timed_out:
        raise TemporaryFailure(
            f'The app "{app}" took too long to describe itself, so nothing was run.',
            hint="Try again in a minute. If it keeps happening, ask the owner to check the app.",
        )
    if done.too_big or done.returncode != 0:
        broken.detail = f"describe exit code {done.returncode}" + (", output too large" if done.too_big else "")
        raise broken
    try:
        info = json.loads(done.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        broken.detail = "describe did not print JSON"
        raise broken from None
    if not isinstance(info, dict) or not isinstance(info.get("commands"), list):
        broken.detail = "describe JSON has no command list"
        raise broken
    if info.get("app") != app:
        wrong = Refused(
            f'The program registered for "{app}" says it is {_short(info.get("app"))!r}. Running it could touch '
            "the wrong app's data, so the gate refuses.",
            hint="Ask the owner to check the app's app.toml and run: mes apps sync",
            code="wrong_app",
        )
        wrong.actual_app = info.get("app")  # type: ignore[attr-defined]
        raise wrong
    kinds: dict[str, str] = {}
    for c in info["commands"]:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or c.get("kind") not in KINDS:
            broken.detail = f"describe lists a command without a valid kind: {_short(str(c), 200)}"
            raise broken
        kinds[c["name"]] = c["kind"]
    return kinds


def _check_kind(command: str, kind: str, allow: str) -> None:
    if kind == "admin":
        raise NotAllowed(
            f'"{command}" is an admin command (it changes how the app is set up). Admin commands are never '
            "run through the gate, so it was not started. This keeps the app's setup in the owner's hands.",
            hint="Ask the owner to run it on the machine itself.",
        )
    level = "write" if allow == "deploy" else allow
    if KINDS.index(kind) > KINDS.index(level):
        raise NotAllowed(
            f'"{command}" changes data (kind "{kind}"), but this key may only run "{allow}" commands, so it '
            "was not started. This protects the data from changes nobody allowed.",
            hint="Ask the owner to run it, or to give you a key that allows write commands.",
        )


# --------------------------------------------------------------------------- per-request instance


def _socket_path(where: GateProject, level: str) -> str | None:
    if not where.server:
        return None
    return units.gate_socket_path(where.name, level)


def _app_level(allow: str) -> str:
    """The sandbox an app command runs in: a deploy key's app commands run in the write sandbox
    (the deploy sandbox has the internet and the code folder, which app commands never need)."""
    return "write" if allow == "deploy" else allow


def _send_frame(sock_file: BinaryIO, header: dict[str, Any], *payloads: bytes) -> None:
    sock_file.write(json.dumps(header, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")
    for p in payloads:
        sock_file.write(p)
    sock_file.flush()


def _read_exact(stream: BinaryIO, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        block = stream.read(n - len(out))
        if not block:
            raise TemporaryFailure("The connection to the request's sandbox ended early, so the answer is incomplete.",
                                   hint="Try again in a moment.", code="sandbox_failed")
        out += block
    return bytes(out)


def read_answer(stream: BinaryIO, max_output: int) -> tuple[dict[str, Any], bytes, bytes]:
    line = stream.readline(64 * 1024)
    try:
        head = json.loads(line.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise TemporaryFailure("The request's sandbox did not answer correctly, so nothing was passed on.",
                               hint="Try again; if it keeps happening, ask the owner to run mes server check.",
                               code="sandbox_failed") from None
    if not isinstance(head, dict):
        raise TemporaryFailure("The request's sandbox did not answer correctly.", hint="Try again.", code="sandbox_failed")
    n_out, n_err = head.get("stdout_bytes"), head.get("stderr_bytes")
    if not isinstance(n_out, int) or not isinstance(n_err, int) or not 0 <= n_out <= max_output + 1 \
            or not 0 <= n_err <= MAX_STDERR:
        raise TemporaryFailure("The request's sandbox gave sizes that do not fit, so nothing was passed on.",
                               hint="Try again.", code="sandbox_failed")
    return head, _read_exact(stream, n_out), _read_exact(stream, n_err)


def via_instance(sock_path: str, request: dict[str, Any], *, timeout: float, max_output: int) -> Finished:
    """Hand a request to its own sandboxed system service instance and read the answer."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout + 30)
        s.connect(sock_path)
    except OSError:
        raise TemporaryFailure(
            "The sandbox for requests is set up on this server but could not be reached, so nothing was run "
            "(the gate never runs a request with less protection than the server was set up for).",
            hint="Try again in a moment. If it keeps happening, ask the owner to run: mes server check",
            code="sandbox_unavailable",
        ) from None
    with s, s.makefile("rwb") as f:
        try:
            _send_frame(f, request)
            head, out, err = read_answer(f, max_output)
        except (OSError, socket.timeout):
            raise TemporaryFailure("The request's sandbox stopped answering, so nothing was passed on.",
                                   hint="Try again later.", code="sandbox_failed") from None
    return Finished(int(head.get("exit_code", 1)), out, err, bool(head.get("timed_out")), bool(head.get("too_big")),
                    "systemd")


def _execute(where: GateProject, level: str, argv_request: list[str], *, timeout: float, max_output: int,
             program: Path, env: dict[str, str]) -> Finished:
    sock = _socket_path(where, _app_level(level))
    if sock is not None and os.path.exists(sock):
        return via_instance(sock, {"kind": "app", "request": argv_request, "timeout": timeout,
                                   "max_output": max_output}, timeout=timeout, max_output=max_output)
    done = run_limited([str(program), *argv_request[1:]], env=env, timeout=timeout, max_output=max_output,
                       cwd=str(where.folder) if where.server else "/")
    done.isolation = "limits"
    return done


def child_main(project: str, server_root: str, level: str, stdin: BinaryIO, stdout: BinaryIO) -> int:
    """One request inside its own system service instance (stdin/stdout are the connection).

    Everything is checked again here: the request, the project, the program and the kind."""
    def answer(done: Finished) -> int:
        _send_frame(stdout, {"exit_code": done.returncode, "stdout_bytes": len(done.stdout),
                             "stderr_bytes": len(done.stderr), "timed_out": done.timed_out,
                             "too_big": done.too_big}, done.stdout, done.stderr)
        return 0

    def refuse(e: MesError) -> int:
        out, err = _render_error(e, True)
        return answer(Finished(int(e.exit_code), out, err))

    try:
        if level not in GATE_LEVELS:
            raise UsageError("Unknown level.", hint="Use read, write or deploy.")
        line = stdin.readline(MAX_REQUEST_BYTES + 1)
        try:
            req = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _bad_request("The sandbox got a request it could not read.") from None
        if not isinstance(req, dict):
            raise _bad_request("The sandbox got a request it could not read.")
        timeout = float(req.get("timeout", DEFAULT_TIMEOUT))
        max_output = int(req.get("max_output", DEFAULT_MAX_OUTPUT))
        if not (0 < timeout <= MAX_TIMEOUT) or not (0 < max_output <= 64 * 1024 * 1024):
            raise _bad_request("The sandbox got limits it does not accept.")
        where = locate(project, server_root)
        if req.get("kind") == "app" and level in ("read", "write"):
            request = parse_request(json.dumps(req.get("request")))
            if request[0] == MES_WORD:
                raise _bad_request("mes operations are not run here.")
            request, _, program, builtin = resolve_request(where, request)
            env = clean_env(level, where)
            if not builtin:
                kinds = ask_commands(program, request[0], env=clean_env("read", where))
                if request[1] not in kinds:
                    raise NotFound(f'The app "{request[0]}" has no command called "{request[1]}", so nothing was run.',
                                   hint=f'See its commands with: ["{request[0]}", "describe", "--json"]')
                _check_kind(request[1], kinds[request[1]], level)
            return answer(run_limited([str(program), *request[1:]], env=env, timeout=timeout,
                                      max_output=max_output, cwd=str(where.folder)))
        if req.get("kind") in ("sync", "publish") and level == "deploy":
            from mes import deploy

            result = deploy.child_step(where, req, timeout=timeout)
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            return answer(Finished(0, body, b""))
        raise _bad_request("The sandbox does not run this kind of request at this level.")
    except MesError as e:
        return refuse(e)
    except Exception as e:  # noqa: BLE001 - fail closed
        return refuse(_internal(e))


# --------------------------------------------------------------------------- log


def _log(where: GateProject | None, record: dict[str, Any]) -> None:
    folder = where.gate_dir if where is not None else paths.state_home() / GATE_DIR
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
    fd = os.open(folder / LOG_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, line.encode("ascii"))
    finally:
        os.close(fd)


def log_path(where: GateProject) -> Path:
    return where.gate_dir / LOG_FILE


def _client() -> str | None:
    value = os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT") or ""
    first = value.split(" ", 1)[0]
    return first[:64] or None


# --------------------------------------------------------------------------- the gate


def _render_error(err: MesError, json_mode: bool) -> tuple[bytes, bytes]:
    out, err_out = io.StringIO(), io.StringIO()
    protocol.emit_error(err, json_mode=json_mode, out=out, err_out=err_out)
    return out.getvalue().encode("utf-8"), err_out.getvalue().encode("utf-8")


def _check_allow(allow: Any) -> str:
    if allow == "admin":
        raise Refused(
            "The gate never runs admin commands, so a key set up with '--allow admin' is refused. Admin "
            "commands change how an app is set up and stay with the owner.",
            hint="Use '--allow read', '--allow write' or '--allow deploy' for keys; the owner runs admin "
            "commands directly on the machine.",
        )
    if allow not in GATE_LEVELS:
        raise UsageError(
            f"The gate's allowed level is {_short(str(allow))!r}, which it does not know, so nothing was run.",
            hint="Set up the key with '--allow read', '--allow write' or '--allow deploy'.",
        )
    return allow


def handle(
    raw: str | None,
    allow: str,
    *,
    project: str,
    server_root: str | os.PathLike[str] | None = None,
    json_mode: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    max_output: int = DEFAULT_MAX_OUTPUT,
    source: str = "option",
    stdin: BinaryIO | None = None,
) -> Outcome:
    """Run one gate request. Never raises; every problem becomes a refusal."""
    held: list[Any] = []
    try:
        return _handle_request(raw, allow, project=project, server_root=server_root, json_mode=json_mode,
                               timeout=timeout, max_output=max_output, source=source, held=held, stdin=stdin)
    finally:
        for slot in held:
            slot.__exit__(None, None, None)


def _handle_request(raw: str | None, allow: str, *, project: str, server_root: Any, json_mode: bool,
                    timeout: float, max_output: int, source: str, held: list[Any],
                    stdin: BinaryIO | None) -> Outcome:
    started = time.monotonic()
    record: dict[str, Any] = {
        "time": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "event": "request",
        "source": source,
        "client": _client(),
        "allow": _short(allow) if isinstance(allow, str) else None,
        "project": _short(project) if isinstance(project, str) else None,
        "app": None,
        "command": None,
        "kind": None,
        "args": None,
        "request_bytes": len(raw.encode("utf-8", "surrogatepass")) if isinstance(raw, str) else None,
        "decision": None,
        "reason": None,
    }
    json_mode = json_mode or (isinstance(raw, str) and '"--json"' in raw)
    where: GateProject | None = None
    try:
        allow = _check_allow(allow)
        where = locate(project, server_root)
        request = parse_request(raw)
        app, command, args = request[0], request[1], request[2:]
        json_mode = json_mode or "--json" in args or app == MES_WORD
        record.update(app=app, command=command, args=len(args))
        slot = _slot(where.gate_dir / SLOTS_DIR)
        slot.__enter__()  # held until the request has finished (released by handle())
        held.append(slot)
        if app == MES_WORD:
            if allow != "deploy":
                raise NotAllowed(
                    f'"mes {command}" changes what is installed on the server, but this key may only run "{allow}" '
                    "commands, so it was refused.",
                    hint="Deploys use the key that was set up with '--allow deploy'.",
                )
            if command not in MES_OPERATIONS:
                raise NotFound(f'The gate has no operation called "{command}".',
                               hint=f"Its operations are: {', '.join(MES_OPERATIONS)}.")
            if not where.server:
                raise Refused("Deploys go to a server set up with 'mes server plan'; this gate is on a personal "
                              "computer, so nothing was installed.",
                              hint="Run the project here with uv run, or deploy to your server.", code="not_a_server")
            record["kind"] = "deploy"
            _log(where, {**record, "decision": "allowed"})
            from mes import deploy

            result = deploy.operation(where, command, args, stdin=stdin, timeout=timeout,
                                      run_child=lambda req: _deploy_child(where, req, timeout))
            record.update(event="finished", decision="done", exit_code=0,
                          duration_ms=int((time.monotonic() - started) * 1000))
            with contextlib.suppress(OSError):
                _log(where, record)
            return Outcome(0, (protocol.to_json(result) + "\n").encode("utf-8"), b"")
        request, _, program, builtin = resolve_request(where, request)
        if request[0] != app:
            record["requested"] = _short(app)
        app, command = request[0], request[1]
        record.update(app=app, command=command, args=len(request) - 2)
        if builtin:
            record["kind"] = "read"  # the project command's built-ins only read
        else:
            kinds = ask_commands(program, app, env=clean_env("read", where))
            if command not in kinds:
                raise NotFound(
                    f'The app "{app}" has no command called "{command}", so nothing was run.',
                    hint=f'See its commands with: ["{app}", "describe", "--json"]',
                )
            record["kind"] = kinds[command]
            _check_kind(command, kinds[command], allow)
    except MesError as e:
        record.update(decision="refused", reason=e.code)
        with contextlib.suppress(OSError):
            _log(where, record)
        out, err = _render_error(e, json_mode)
        return Outcome(int(e.exit_code), out, err)
    except Exception as e:  # noqa: BLE001 - fail closed
        record.update(decision="refused", reason="internal")
        with contextlib.suppress(OSError):
            _log(where, record)
        out, err = _render_error(_internal(e), json_mode)
        return Outcome(1, out, err)

    sock = _socket_path(where, _app_level(allow))
    record.update(decision="allowed", isolation="systemd" if sock and os.path.exists(sock) else "limits")
    try:
        _log(where, record)
    except OSError:
        err_ = MesError(
            "The gate could not write its log, and it never runs anything it cannot record, so nothing was run.",
            hint="Ask the owner to check that the project's state folder exists and can be written.",
            code="log_unavailable",
        )
        out, err = _render_error(err_, json_mode)
        return Outcome(1, out, err)

    result: Outcome
    finished: dict[str, Any] = {"event": "finished"}
    try:
        done = _execute(where, allow, request, timeout=timeout, max_output=max_output, program=program,
                        env=clean_env(allow, where))
        finished["isolation"] = done.isolation
        if done.timed_out:
            stop: MesError = TemporaryFailure(
                f'"{command}" took longer than {timeout:g} seconds, so the gate stopped it. Its output was '
                "not passed on, because it may be incomplete.",
                hint="Try again later, or ask for less data at once.",
            )
            finished.update(decision="stopped", reason="timeout")
            out, err = _render_error(stop, json_mode)
            result = Outcome(int(stop.exit_code), out, err)
        elif done.too_big:
            stop = Refused(
                f'"{command}" printed more than {max_output} bytes, so the gate stopped it and passed nothing on.',
                hint="Ask for less data at once (for example a shorter date range).",
            )
            finished.update(decision="stopped", reason="output_too_large")
            out, err = _render_error(stop, json_mode)
            result = Outcome(int(stop.exit_code), out, err)
        else:
            code = done.returncode if 0 <= done.returncode <= 255 else 1
            finished.update(decision="done", reason=None)
            result = Outcome(code, done.stdout, done.stderr)
    except MesError as e:
        finished.update(decision="failed", reason=e.code)
        out, err = _render_error(e, json_mode)
        result = Outcome(int(e.exit_code), out, err)
    except Exception as e:  # noqa: BLE001 - fail closed
        finished.update(decision="failed", reason="internal")
        out, err = _render_error(_internal(e), json_mode)
        result = Outcome(1, out, err)

    record.update(finished, exit_code=result.exit_code,
                  duration_ms=int((time.monotonic() - started) * 1000),
                  stdout_bytes=len(result.stdout))
    with contextlib.suppress(OSError):
        _log(where, record)
    return result


def _deploy_child(where: GateProject, req: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Run a deploy step (uv sync, publish) in its own sandbox when the server has one."""
    sock = _socket_path(where, "deploy")
    if sock is not None and os.path.exists(sock):
        done = via_instance(sock, {**req, "timeout": timeout, "max_output": DESCRIBE_MAX_OUTPUT},
                            timeout=timeout, max_output=DESCRIBE_MAX_OUTPUT)
        try:
            data = json.loads(done.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise MesError("A deploy step did not answer correctly, so the deploy stopped.",
                           hint="Run mes server check on the server.", code="deploy_failed") from None
        if done.returncode != 0 or not isinstance(data, dict):
            said = data.get("error", {}) if isinstance(data, dict) else {}
            raise MesError(str(said.get("message") or "A deploy step failed, so the deploy stopped."),
                           hint=str(said.get("hint") or "Run mes server check on the server."),
                           code=str(said.get("code") or "deploy_failed"))
        return data
    from mes import deploy

    return deploy.child_step(where, req, timeout=timeout)


def _internal(e: BaseException) -> MesError:
    return MesError(
        "Something went wrong inside the gate, so it refused the request and ran nothing.",
        hint="Try again. If it keeps happening, ask the owner to look at the gate log.",
        code="internal",
    )


# --------------------------------------------------------------------------- mes gate


def _positive(text: str) -> float:
    value = float(text)
    if not (0 < value <= MAX_TIMEOUT):
        raise ValueError(text)
    return value


_positive.__name__ = "number of seconds"


def _size(text: str) -> int:
    value = int(text)
    if value <= 0 or value > 64 * 1024 * 1024:
        raise ValueError(text)
    return value


_size.__name__ = "number of bytes"


def _write_outcome(outcome: Outcome) -> None:
    # The gate passes the app's own output through unchanged, so it writes the bytes itself.
    for stream, data in ((sys.stdout, outcome.stdout), (sys.stderr, outcome.stderr)):
        if data:
            stream.flush()
            target = getattr(stream, "buffer", None)
            if target is not None:
                target.write(data)
                target.flush()
            else:
                stream.write(data.decode("utf-8", "replace"))
                stream.flush()


def _handle(args: argparse.Namespace) -> Any:
    ssh_command = os.environ.get("SSH_ORIGINAL_COMMAND")
    if args.request is not None and ssh_command is not None:
        outcome = Outcome(*_conflict(args.json))
    else:
        raw, source = (args.request, "option") if args.request is not None else (ssh_command, "ssh")
        outcome = handle(raw, args.allow, project=args.project, server_root=args.server_root, json_mode=args.json,
                         timeout=args.timeout, max_output=args.max_output, source=source,
                         stdin=getattr(sys.stdin, "buffer", None))
    _write_outcome(outcome)
    raise SystemExit(outcome.exit_code)


def _handle_child(args: argparse.Namespace) -> Any:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    raise SystemExit(child_main(args.project, args.server_root, args.level, stdin, stdout))


def _conflict(json_mode: bool) -> tuple[int, bytes, bytes]:
    e = _bad_request(
        "The gate was given a command both by SSH and by --command, so it cannot tell which one is meant "
        "and runs neither.",
        hint="--command is only for testing on the machine itself; remove it from the SSH key setup.",
    )
    out, err = _render_error(e, json_mode)
    return int(e.exit_code), out, err


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "gate",
        help="The one SSH entry point for AI agents and deploys: runs a single request safely.",
        description="Run one request that came over SSH. The request is read from SSH_ORIGINAL_COMMAND as a "
        "JSON list: [app, command, args...] or [\"mes\", operation, ...]. Only requests of the allowed level "
        "run; admin commands never do.",
    )
    sub = p.add_subparsers(dest="gate_command", metavar="[child]")
    p.add_argument("--project", required=False, default=None, help="the project whose apps this key may reach")
    p.add_argument("--allow", required=False, default=None, metavar="read|write|deploy",
                   help="the most powerful kind of request this key may make")
    p.add_argument("--server-root", default=None, metavar="DIR",
                   help="on a server: the folder holding the projects (/var/lib/mes)")
    p.add_argument("--command", dest="request", default=None, metavar="JSON",
                   help="the request as a JSON list, for testing on this machine (instead of SSH)")
    p.add_argument("--timeout", type=_positive, default=DEFAULT_TIMEOUT,
                   help=f"seconds a request may run (default {DEFAULT_TIMEOUT:g})")
    p.add_argument("--max-output", type=_size, default=DEFAULT_MAX_OUTPUT,
                   help=f"most bytes a request may print (default {DEFAULT_MAX_OUTPUT})")
    p.set_defaults(func=_handle_main)
    c = sub.add_parser("child", help="(Used by the server's sandboxed request instances.) Run one request.")
    c.add_argument("--project", required=True, dest="child_project")
    c.add_argument("--server-root", required=True, dest="child_server_root")
    c.add_argument("--level", required=True, choices=GATE_LEVELS)
    c.set_defaults(func=lambda a: _handle_child(argparse.Namespace(project=a.child_project,
                                                                   server_root=a.child_server_root,
                                                                   level=a.level)))
    registry.register(subparsers)


def _handle_main(args: argparse.Namespace) -> Any:
    if not args.project or not args.allow:
        raise UsageError("mes gate needs --project <name> and --allow read|write|deploy.",
                         hint="For example: mes gate --project myproject --allow read")
    return _handle(args)


__all__ = ["handle", "parse_request", "locate", "GateProject", "child_main", "register"]
