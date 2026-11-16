"""``mes gate``: the single entry point for AI agents that connect over SSH.

Each agent key in ``authorized_keys`` is tied to a fixed command, for example::

    command="/path/to/mes gate --allow read",restrict ssh-ed25519 AAAA... agent-read

The agent then asks for one app command by sending a JSON array of strings as
its SSH command::

    ssh my-server '["myapp", "summary", "--days", "7", "--json"]'

SSH puts that text in ``SSH_ORIGINAL_COMMAND``. The gate:

1. Accepts only a JSON array of strings ``[app, command, *args]``. There is no
   shell and no shell parsing; anything else is refused (exit 2). Arguments may
   not contain NUL bytes, may not be longer than 4096 bytes, and there may be at
   most 64 items in the array.
2. Finds the app in the per-machine registry (``mes.registry``), never in the
   request. Unknown app: exit 3.
3. Asks the installed app for its commands and their kinds (``<program> --json
   describe``), fresh on every request. Unknown command: exit 3.
4. Refuses a command whose kind is above ``--allow`` (exit 4). ``admin``
   commands are never run through the gate, and ``--allow admin`` is refused.
5. Runs the app's program directly with an argument list (never through mes,
   a shell or an interpreter command line), with a small fixed environment
   (fixed ``PATH``, ``MES_ALLOW`` set to the allowed level, no SSH agent, no
   tokens, no ``PYTHON*`` variables from outside), no input, a time limit, an
   output size limit and resource limits (CPU time, file size, no core files,
   and address space on Linux). At most ``MAX_PARALLEL`` requests run at once;
   more get exit 75. Its stdout, stderr and exit code are passed through.
   v0.1 does not isolate the app's files or network from the gate's account
   (there is no full sandbox yet); see ``mes guide gate``.
6. Writes one JSON line per request to ``<state home>/gate-log.jsonl`` (and a
   second line when an allowed command finishes). If that log cannot be
   written, nothing is run.

Any unexpected problem refuses the request (fails closed).

The gate's own errors are JSON on stdout when the request or the gate command
contains ``--json``; otherwise they are plain text on stderr.

Public API
----------
``MAX_ITEMS`` (64), ``MAX_ARG_BYTES`` (4096), ``MAX_REQUEST_BYTES``,
``DEFAULT_TIMEOUT`` (seconds), ``DEFAULT_MAX_OUTPUT`` (bytes), ``SAFE_PATH``.
``Outcome(exit_code, stdout, stderr)``  what the gate prints and its exit code.
``parse_request(text) -> list[str]``  check and decode a request (``UsageError``).
``clean_env(allow) -> dict``          the environment an app is run with.
``run_limited(argv, *, env, timeout, max_output, max_stderr) -> Finished``
``ask_commands(program, app, *, timeout) -> dict[str, str]``  command -> kind,
    from the installed app itself.
``handle(raw, allow, *, json_mode=False, timeout=..., max_output=..., source="option") -> Outcome``
    the whole gate, without touching sys.stdout; never raises.
``log_path() -> Path``
``register(subparsers)``  adds ``mes gate`` (and ``mes apps`` from ``mes.registry``).
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
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from mes import paths, protocol, registry
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
DEFAULT_MAX_OUTPUT = 8 * 1024 * 1024
MAX_STDERR = 64 * 1024
DESCRIBE_TIMEOUT = 20.0
DESCRIBE_MAX_OUTPUT = 1024 * 1024
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
GATE_LEVELS = ("read", "write")
LOG_FILE = "gate-log.jsonl"
# Limits for every program the gate starts (set with setrlimit in the child, before it runs).
# They stop one request from using up the machine; they are not a full sandbox (see
# ``mes guide gate``).
MEMORY_LIMIT = 2 * 1024**3        # address space, bytes (enforced on Linux)
FILE_SIZE_LIMIT = 1024**3         # largest file a request may write, bytes
CPU_GRACE = 5                     # CPU seconds allowed beyond the time limit
MAX_PARALLEL = 4                  # requests the gate runs at the same time
SLOTS_DIR = "gate-slots"

_COMMAND_RE = re.compile(r"[a-z][a-z0-9-]{0,40}")
# Copied from the gate's own environment (set by the account, not by the SSH client):
# they say whose data folders to use. Everything else is dropped.
_KEEP_ENV = (
    "HOME", "USER", "LOGNAME", "TZ",
    "MES_DATA_HOME", "MES_STATE_HOME", "MES_CONFIG",
    "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
)


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
    # "--json" right after the app name is accepted and moved after the command, as apps allow
    # it in either place ("--json" is a fixed, known flag, so this changes nothing else).
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
    if not registry.valid_name(app):
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


def clean_env(allow: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in _KEEP_ENV if os.environ.get(k)}
    env.update({
        "PATH": SAFE_PATH,
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MES_ALLOW": allow,
        "MES_VIA_GATE": "1",
    })
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
def _slot() -> Iterator[None]:
    """One of MAX_PARALLEL places; none free means the computer is busy (exit 75)."""
    folder = paths.state_home() / SLOTS_DIR
    for i in range(MAX_PARALLEL):
        try:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(folder / f"slot-{i}.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            raise MesError(
                "The gate could not use its own folder (to count and record requests), and it never "
                "runs anything it cannot record, so nothing was run.",
                hint="Ask the owner to check that the mes state folder exists and can be written.",
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
        f"This computer is already answering {MAX_PARALLEL} requests from agents, so this one was "
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
) -> Finished:
    """Run ``argv`` (no shell), keeping at most ``max_output`` bytes of stdout.

    Going over the stdout limit or the time limit stops the program and its
    children. Stderr beyond ``max_stderr`` is dropped.
    """
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd="/",
            close_fds=True,
            start_new_session=True,
            shell=False,
            preexec_fn=_apply_limits(_limits(timeout)),
        )
    except (OSError, subprocess.SubprocessError):
        raise MesError(
            "The app's program could not be started, so nothing was run.",
            hint="Ask the owner to check that the app is installed correctly (mes apps list).",
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
    # The program has ended; children that kept the pipes open are stopped too.
    _kill(proc)
    for t in readers:
        t.join(timeout=5)
    return Finished(
        returncode=proc.returncode,
        stdout=b"".join(out_chunks),
        stderr=b"".join(err_chunks),
        timed_out=timed_out,
        too_big=state["too_big"],
    )


def ask_commands(program: str | Path, app: str, *, timeout: float = DESCRIBE_TIMEOUT) -> dict[str, str]:
    """Ask the installed program which commands it has; returns command -> kind."""
    broken = MesError(
        f'The app "{app}" did not describe itself correctly, so the gate cannot tell which commands are '
        "safe and refuses to run anything from it.",
        hint=f"Ask the owner to check the app: {app} --json describe",
        code="app_unavailable",
    )
    done = run_limited([str(program), "--json", "describe"], env=clean_env("read"),
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
            hint=f"Ask the owner to register the right program with: mes apps add {app} <program>",
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


# --------------------------------------------------------------------------- log


def log_path() -> Path:
    return paths.state_home() / LOG_FILE


def _log(record: dict[str, Any]) -> None:
    p = log_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("ascii"))
    finally:
        os.close(fd)


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
            hint="Use '--allow read' or '--allow write' for agent keys; the owner runs admin commands "
            "directly on the machine.",
        )
    if allow not in GATE_LEVELS:
        raise UsageError(
            f"The gate's allowed level is {_short(str(allow))!r}, which it does not know, so nothing was run.",
            hint="Set up the key with 'mes gate --allow read' or 'mes gate --allow write'.",
        )
    return allow


def handle(
    raw: str | None,
    allow: str,
    *,
    json_mode: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    max_output: int = DEFAULT_MAX_OUTPUT,
    source: str = "option",
) -> Outcome:
    """Run one gate request. Never raises; every problem becomes a refusal."""
    held: list[Any] = []
    try:
        return _handle_request(raw, allow, json_mode=json_mode, timeout=timeout, max_output=max_output,
                               source=source, held=held)
    finally:
        for slot in held:
            slot.__exit__(None, None, None)


def _handle_request(raw: str | None, allow: str, *, json_mode: bool, timeout: float, max_output: int,
                    source: str, held: list[Any]) -> Outcome:
    started = time.monotonic()
    record: dict[str, Any] = {
        "time": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "event": "request",
        "source": source,
        "client": _client(),
        "allow": _short(allow) if isinstance(allow, str) else None,
        "app": None,
        "command": None,
        "kind": None,
        "args": None,
        "request_bytes": len(raw.encode("utf-8", "surrogatepass")) if isinstance(raw, str) else None,
        "decision": None,
        "reason": None,
    }
    json_mode = json_mode or (isinstance(raw, str) and '"--json"' in raw)
    try:
        allow = _check_allow(allow)
        request = parse_request(raw)
        app, command, args = request[0], request[1], request[2:]
        json_mode = json_mode or "--json" in args
        record.update(app=app, command=command, args=len(args))
        slot = _slot()
        slot.__enter__()  # held until the request has finished (released by handle())
        held.append(slot)
        program = registry.check_program(registry.lookup(app, public=True), app=app)
        kinds = ask_commands(program, app)
        if command not in kinds:
            raise NotFound(
                f'The app "{app}" has no command called "{command}", so nothing was run.',
                hint=f'See its commands with: ["{app}", "describe", "--json"]',
            )
        kind = kinds[command]
        record["kind"] = kind
        if kind == "admin":
            raise NotAllowed(
                f'"{command}" is an admin command (it changes how the app is set up). Admin commands are never '
                "run through the gate, so it was not started. This keeps the app's setup in the owner's hands.",
                hint="Ask the owner to run it on the machine itself.",
            )
        if KINDS.index(kind) > KINDS.index(allow):
            raise NotAllowed(
                f'"{command}" changes data (kind "{kind}"), but this key may only run "{allow}" commands, so it '
                "was not started. This protects the data from changes nobody allowed.",
                hint="Ask the owner to run it, or to give you a key that allows write commands.",
            )
    except MesError as e:
        record.update(decision="refused", reason=e.code)
        try:
            _log(record)
        except OSError:
            pass  # already refusing
        out, err = _render_error(e, json_mode)
        return Outcome(int(e.exit_code), out, err)
    except Exception as e:  # noqa: BLE001 - fail closed
        record.update(decision="refused", reason="internal")
        try:
            _log(record)
        except OSError:
            pass
        out, err = _render_error(_internal(e), json_mode)
        return Outcome(1, out, err)

    record.update(decision="allowed")
    try:
        _log(record)
    except OSError as e:
        err_ = MesError(
            "The gate could not write its log, and it never runs anything it cannot record, so nothing was run.",
            hint="Ask the owner to check that the mes state folder exists and can be written.",
            code="log_unavailable",
        )
        out, err = _render_error(err_, json_mode)
        return Outcome(1, out, err)

    result: Outcome
    finished: dict[str, Any] = {"event": "finished"}
    try:
        done = run_limited([str(program), command, *args], env=clean_env(allow),
                           timeout=timeout, max_output=max_output)
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
    try:
        _log(record)
    except OSError:
        pass  # the request line was written before running
    return result


def _internal(e: BaseException) -> MesError:
    return MesError(
        "Something went wrong inside the gate, so it refused the request and ran nothing.",
        hint="Try again. If it keeps happening, ask the owner to look at the gate log.",
        code="internal",
    )


# --------------------------------------------------------------------------- mes gate


def _positive(text: str) -> float:
    value = float(text)
    if not (0 < value <= 24 * 3600):
        raise ValueError(text)
    return value


_positive.__name__ = "number of seconds"


def _size(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise ValueError(text)
    return value


_size.__name__ = "number of bytes"


def _handle(args: argparse.Namespace) -> Any:
    ssh_command = os.environ.get("SSH_ORIGINAL_COMMAND")
    if args.request is not None and ssh_command is not None:
        outcome = Outcome(*_conflict(args.json))
    else:
        raw, source = (args.request, "option") if args.request is not None else (ssh_command, "ssh")
        outcome = handle(raw, args.allow, json_mode=args.json, timeout=args.timeout,
                         max_output=args.max_output, source=source)
    # The gate passes the app's own output through unchanged, so it writes the bytes itself
    # instead of returning data for mes to format.
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
    raise SystemExit(outcome.exit_code)


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
        help="The one SSH entry point for AI agents: runs a single app command safely.",
        description="Run one app command for an AI agent that connected over SSH. The command is read "
        "from SSH_ORIGINAL_COMMAND as a JSON list: [app, command, args...]. Only commands of the allowed "
        "kind run; admin commands never do.",
    )
    p.add_argument("--allow", required=True, metavar="read|write",
                   help="the most powerful kind of command this key may run")
    p.add_argument("--command", dest="request", default=None, metavar="JSON",
                   help="the request as a JSON list, for testing on this machine (instead of SSH)")
    p.add_argument("--timeout", type=_positive, default=DEFAULT_TIMEOUT,
                   help=f"seconds a command may run (default {DEFAULT_TIMEOUT:g})")
    p.add_argument("--max-output", type=_size, default=DEFAULT_MAX_OUTPUT,
                   help=f"most bytes a command may print (default {DEFAULT_MAX_OUTPUT})")
    p.set_defaults(func=_handle)
    registry.register(subparsers)
