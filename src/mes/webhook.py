"""``mes webhook``: signed webhooks as a job trigger (``webhook = true`` in project.toml).

How it fits together on a server (all rendered by ``mes server plan``, see ``mes.units``):

- ``mes-<p>.webhook.socket`` listens on ``/run/mes-<p>-webhook.sock`` (owned by root, mode 0600),
  so only a local proxy running as root (for example ``tailscale serve`` or ``funnel``, set up by
  the person) can pass requests to it. mes never runs that proxy.
- The first request starts ``mes webhook serve --project <p> --server-root <root>`` as
  ``mes-<p>``, with no network of its own and only ``state/triggers`` writable. It checks the
  signature and, when it is right, rewrites ``state/triggers/<job id>``. It stops by itself after
  ``IDLE_EXIT_SEC`` seconds without requests.
- ``mes-<p>.job.<app>.<job>.path`` notices the change and starts the job's own sandboxed unit. The
  receiver therefore never runs project code and never needs any privilege.

The request a sender makes::

    POST /hooks/<job id> HTTP/1.1
    Content-Length: <n>
    X-Mes-Timestamp: <unix seconds>
    X-Mes-Signature: sha256=<hex of HMAC-SHA256(secret, "<timestamp>." + body)>

Checks, in order: method and path, header sizes, ``Content-Length`` (required, at most
``MAX_BODY`` bytes; no chunked bodies), the body read within ``READ_TIMEOUT_SEC``, the timestamp
within ``WINDOW_SEC`` seconds of now (either way), the signature (constant-time compare), not
seen before in the window (an exact replay is refused), and the job: it must exist in the
deployed project.toml with ``webhook = true``. Answers are short JSON bodies with no internal
detail: 202 accepted, 400, 401, 404, 405, 408, 411, 413, 503.

The secret is ``<root>/<p>/etc/webhook.key``: written by the root script (owned by root,
readable by the project's account), hex or base64 text of at least ``MIN_SECRET_BYTES`` random
bytes. The receiver refuses to start when it is missing, too short, a link, or writable by
anyone but its owner (root or the project's account).

Public API
----------
``WINDOW_SEC``, ``MAX_BODY``, ``MAX_HEADER_BYTES``, ``READ_TIMEOUT_SEC``, ``IDLE_EXIT_SEC``,
``MAX_CONNECTIONS``, ``MIN_SECRET_BYTES``, ``SEEN_FILE``.
``sign(secret, timestamp, body) -> str``               ``"sha256=<hex>"``.
``verify(secret, timestamp, signature, body, now, seen) -> str | None``
    ``None`` when the request may pass, else a problem code: ``bad_timestamp``, ``stale``,
    ``too_large``, ``bad_signature_header``, ``bad_signature``, ``replay``. Pure: ``seen`` (a
    mapping signature -> time) is only read.
``job_problem(project, job) -> str | None``            ``bad_job``, ``unknown_job`` or ``not_webhook``.
``parse_secret(text) -> bytes``, ``load_secret(path, *, owner_only=False) -> bytes``
``Receiver(layout, secret)``                           ``handle(conn)`` serves one connection.
``serve(layout, *, listener=None, socket_path=None, idle=IDLE_EXIT_SEC) -> dict``
``register(subparsers)``                               adds ``mes webhook serve|sign|secret-check``.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import os
import re
import socket
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mes import manifest as manifest_mod
from mes import paths
from mes.app import check_allowed
from mes.errors import MesError, NotFound, Refused, UsageError

WINDOW_SEC = 300
MAX_BODY = 64 * 1024
MAX_HEADER_BYTES = 8 * 1024
MAX_HEADERS = 40
READ_TIMEOUT_SEC = 10.0
IDLE_EXIT_SEC = 60.0
MAX_CONNECTIONS = 4
MIN_SECRET_BYTES = 32
MAX_SECRET_FILE = 4096
SEEN_FILE = ".seen"
SD_LISTEN_FDS_START = 3

_SIG_RE = re.compile(r"^sha256=([0-9a-f]{64})\Z")
_TS_RE = re.compile(r"^[0-9]{1,12}\Z")
_PATH_RE = re.compile(r"^/hooks/([^/?#]{1,64})\Z")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+\Z")
_B64_RE = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}\Z")

_REASONS = {202: "Accepted", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
            405: "Method Not Allowed", 408: "Request Timeout", 411: "Length Required",
            413: "Payload Too Large", 503: "Service Unavailable"}


# --------------------------------------------------------------------------- pure checks


def sign(secret: bytes, timestamp: int | str, body: bytes) -> str:
    mac = hmac.new(secret, f"{timestamp}.".encode("ascii") + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: bytes, timestamp: Any, signature: Any, body: bytes, now: float,
           seen: Mapping[str, float]) -> str | None:
    if not isinstance(timestamp, str) or not _TS_RE.fullmatch(timestamp):
        return "bad_timestamp"
    if abs(now - int(timestamp)) > WINDOW_SEC:
        return "stale"
    if len(body) > MAX_BODY:
        return "too_large"
    if not isinstance(signature, str) or not _SIG_RE.fullmatch(signature):
        return "bad_signature_header"
    expected = sign(secret, timestamp, body)
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii")):
        return "bad_signature"
    if signature in seen:
        return "replay"
    return None


def job_problem(project: manifest_mod.Project, job: Any) -> str | None:
    if not isinstance(job, str) or not manifest_mod.JOB_ID_RE.fullmatch(job):
        return "bad_job"
    if job not in project.jobs:
        return "unknown_job"
    if project.jobs[job].trigger != "webhook":
        return "not_webhook"
    return None


def parse_secret(text: str) -> bytes:
    value = text.strip()
    raw: bytes | None = None
    if value and _HEX_RE.fullmatch(value) and len(value) % 2 == 0:
        raw = bytes.fromhex(value)
    elif value and _B64_RE.fullmatch(value):
        try:
            padded = value + "=" * (-len(value) % 4)
            raw = base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True)
        except (binascii.Error, ValueError):
            raw = None
    if raw is None or len(raw) < MIN_SECRET_BYTES:
        raise Refused(
            f"The webhook secret is not at least {MIN_SECRET_BYTES} random bytes written as hex or base64, so it "
            "could be guessed and webhooks stay switched off.",
            hint="Make a new one, for example: head -c 32 /dev/urandom | base64 (the root script from "
            "'mes server plan' does this for you).",
            code="weak_secret",
        )
    return raw


def load_secret(path: str | os.PathLike[str], *, owner_only: bool = False) -> bytes:
    """Read a secret file that only its owner (root or this account) can change.

    ``owner_only=True`` (for ``mes webhook sign`` on a personal computer) also refuses a file
    that others can read."""
    p = Path(path)
    unsafe = Refused(
        "The webhook secret file could be read or changed by someone else, or is not a plain file, so it was "
        "not used.",
        hint=f"Make it a plain file only you can read and change (chmod 600 {p})." if owner_only else
        "Run 'mes server plan' and its script again: it writes the secret with safe permissions.",
        code="untrusted_file",
    )
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        raise NotFound(
            "There is no webhook secret yet, so webhooks cannot be checked and stay switched off.",
            hint="Run 'mes server plan' for a project with a webhook job, and run its script with sudo.",
            code="no_secret",
        ) from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise unsafe
    if st.st_uid not in (0, os.getuid()) or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise unsafe
    if owner_only and (st.st_uid != os.getuid() or st.st_mode & 0o077):
        raise unsafe
    if st.st_size > MAX_SECRET_FILE:
        raise unsafe
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        data = f.read(MAX_SECRET_FILE + 1)
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        text = ""  # parse_secret refuses it with the plain "weak secret" message
    return parse_secret(text)


# --------------------------------------------------------------------------- the receiver


class _Problem(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


_STATUS_FOR = {"bad_timestamp": 401, "stale": 401, "bad_signature_header": 401, "bad_signature": 401,
               "replay": 401, "too_large": 413, "bad_job": 404, "unknown_job": 404, "not_webhook": 404}


class Receiver:
    """Serves one connection at a time per thread; at most ``MAX_CONNECTIONS`` together."""

    def __init__(self, layout: paths.ServerLayout, secret: bytes, *, clock: Any = time.time) -> None:
        self.layout = layout
        self.secret = secret
        self.clock = clock
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.accepted = 0
        self.refused = 0
        self.seen: dict[str, float] = {}
        self._triggers = self._triggers_dir()
        self._load_seen()

    # -- state

    def _triggers_dir(self) -> Path:
        d = self.layout.triggers
        try:
            d.mkdir(mode=0o700, exist_ok=True)
        except FileNotFoundError:
            raise NotFound("The project's state folder does not exist, so webhooks cannot be recorded.",
                           hint="Run 'mes server plan' and its script again.") from None
        st = os.lstat(d)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() \
                or st.st_mode & 0o077:
            raise Refused("The webhook trigger folder is not a private folder of this account, so nothing is "
                          "written into it.", hint="Remove state/triggers and let mes create it again.",
                          code="unsafe_folder")
        return d

    def _prune(self, now: float) -> None:
        for sig, t in list(self.seen.items()):
            if abs(now - t) > 2 * WINDOW_SEC:
                del self.seen[sig]

    def _load_seen(self) -> None:
        p = self._triggers / SEEN_FILE
        try:
            fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return
        with os.fdopen(fd, "rb") as f:
            raw = f.read(1024 * 1024)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if isinstance(data, dict):
            for sig, t in data.items():
                if isinstance(sig, str) and _SIG_RE.fullmatch(sig) and isinstance(t, (int, float)):
                    self.seen[sig] = float(t)
        self._prune(self.clock())

    def _write_atomic(self, name: str, payload: bytes) -> None:
        fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=self._triggers)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._triggers / name)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def _remember(self, signature: str, now: float) -> None:
        self.seen[signature] = now
        self._prune(now)
        self._write_atomic(SEEN_FILE, json.dumps(self.seen, separators=(",", ":")).encode("ascii"))

    def _project(self) -> manifest_mod.Project:
        return manifest_mod.load(self.layout.current_project)

    # -- one request

    def process(self, method: str, target: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        """Decide on one parsed request. Returns (status, JSON body)."""
        if method != "POST":
            raise _Problem(405, "method_not_allowed")
        m = _PATH_RE.fullmatch(target)
        if not m:
            raise _Problem(404, "not_found")
        job = m.group(1)
        now = self.clock()
        with self.lock:
            problem = verify(self.secret, headers.get("x-mes-timestamp"), headers.get("x-mes-signature"),
                             body, now, self.seen)
            if problem:
                raise _Problem(_STATUS_FOR[problem], problem if problem != "bad_signature_header" else "bad_signature")
            try:
                project = self._project()
            except MesError:
                raise _Problem(404, "unknown_job") from None
            jp = job_problem(project, job)
            if jp:
                raise _Problem(404, "unknown_job")
            signature = headers["x-mes-signature"]
            self._remember(signature, now)
            record = {"time": int(now), "body_sha256": hashlib.sha256(body).hexdigest()}
            self._write_atomic(job, json.dumps(record, separators=(",", ":")).encode("ascii"))
            self.accepted += 1
        return 202, {"accepted": True, "job": job}

    def handle(self, conn: socket.socket) -> None:
        conn.settimeout(READ_TIMEOUT_SEC)
        status, body = 400, {"error": "bad_request"}
        deadline = time.monotonic() + READ_TIMEOUT_SEC
        try:
            method, target, headers, rest = _read_head(conn, deadline)
            if "transfer-encoding" in headers:
                raise _Problem(411, "length_required")
            length_text = headers.get("content-length")
            if length_text is None:
                raise _Problem(411, "length_required")
            if not re.fullmatch(r"[0-9]{1,9}", length_text):
                raise _Problem(400, "bad_request")
            length = int(length_text)
            if length > MAX_BODY:
                raise _Problem(413, "too_large")
            payload = _read_body(conn, rest, length, deadline)
            status, body = self.process(method, target, headers, payload)
        except _Problem as p:
            status, body = p.status, {"error": p.code}
        except (TimeoutError, socket.timeout):
            status, body = 408, {"error": "timeout"}
        except (OSError, ValueError, UnicodeError):
            status, body = 400, {"error": "bad_request"}
        except Exception:  # noqa: BLE001 - never leak detail; refuse
            status, body = 400, {"error": "bad_request"}
        if status != 202:
            self.refused += 1
        with contextlib.suppress(OSError):
            _respond(conn, status, body)


def _read_head(conn: socket.socket, deadline: float) -> tuple[str, str, dict[str, str], bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        if time.monotonic() > deadline:
            raise TimeoutError
        block = conn.recv(4096)
        if not block:
            raise _Problem(400, "bad_request")
        data += block
        if len(data) > MAX_HEADER_BYTES and b"\r\n\r\n" not in data[:MAX_HEADER_BYTES + 4]:
            raise _Problem(400, "headers_too_large")
    head, _, rest = data.partition(b"\r\n\r\n")
    if len(head) > MAX_HEADER_BYTES:
        raise _Problem(400, "headers_too_large")
    lines = head.decode("ascii").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[2] not in ("HTTP/1.1", "HTTP/1.0"):
        raise _Problem(400, "bad_request")
    headers: dict[str, str] = {}
    if len(lines) - 1 > MAX_HEADERS:
        raise _Problem(400, "headers_too_large")
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip() or any(c in name for c in " \t"):
            raise _Problem(400, "bad_request")
        key = name.lower()
        if key in headers and key in ("content-length", "x-mes-timestamp", "x-mes-signature"):
            raise _Problem(400, "bad_request")  # a duplicate of a header that matters is never guessed at
        headers[key] = value.strip()
    return parts[0], parts[1], headers, rest


def _read_body(conn: socket.socket, rest: bytes, length: int, deadline: float) -> bytes:
    body = rest[:length + 1]
    while len(body) < length:
        if time.monotonic() > deadline:
            raise TimeoutError
        block = conn.recv(min(65536, length - len(body)))
        if not block:
            raise _Problem(400, "bad_request")
        body += block
    if len(body) != length:
        raise _Problem(400, "bad_request")
    return body


def _respond(conn: socket.socket, status: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body, separators=(",", ":")).encode("ascii")
    head = (f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode("ascii")
    conn.sendall(head + payload)


# --------------------------------------------------------------------------- serving


def _systemd_listener() -> socket.socket:
    not_activated = Refused(
        "mes webhook serve only runs when systemd starts it for its socket, and it was started some other way, "
        "so it did not listen anywhere.",
        hint="It is started by the mes-<project>.webhook.socket unit that 'mes server plan' sets up.",
        code="not_socket_activated",
    )
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise not_activated
    try:
        sock = socket.socket(fileno=SD_LISTEN_FDS_START)
    except OSError:
        raise not_activated from None
    if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM:
        sock.detach()
        raise not_activated
    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(name, None)
    return sock


def _test_listener(path: str) -> socket.socket:
    p = Path(path)
    folder = p.parent
    st = os.lstat(folder)
    if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise Refused("--socket must be in a folder of your own that nobody else can write to.",
                      hint="Use a new private folder.", code="unsafe_folder")
    if os.path.lexists(p):
        raise Refused("--socket must not exist yet.", hint="Use a new socket path.", code="unsafe_folder")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(str(p))
    finally:
        os.umask(old)
    sock.listen(16)
    return sock


def serve(layout: paths.ServerLayout, *, listener: socket.socket | None = None, socket_path: str | None = None,
          idle: float = IDLE_EXIT_SEC) -> dict[str, Any]:
    secret = load_secret(layout.webhook_key)
    receiver = Receiver(layout, secret)
    sock = listener or (_test_listener(socket_path) if socket_path else _systemd_listener())
    sock.settimeout(idle)
    threads: list[threading.Thread] = []

    def run(conn: socket.socket) -> None:
        try:
            receiver.handle(conn)
        finally:
            with contextlib.suppress(OSError):
                conn.close()
            receiver.slots.release()

    try:
        while True:
            try:
                conn, _ = sock.accept()
            except (TimeoutError, socket.timeout):
                break  # nobody used it for a while: stop, systemd starts it again on the next request
            if not receiver.slots.acquire(blocking=False):
                with contextlib.suppress(OSError):
                    conn.settimeout(2)
                    _respond(conn, 503, {"error": "busy"})
                    conn.close()
                receiver.refused += 1
                continue
            t = threading.Thread(target=run, args=(conn,), daemon=True)
            t.start()
            threads = [x for x in threads if x.is_alive()] + [t]
    finally:
        for t in threads:
            t.join(timeout=READ_TIMEOUT_SEC + 1)
        sock.close()
        if socket_path:
            with contextlib.suppress(OSError):
                os.unlink(socket_path)
    return {"project": layout.project, "accepted": receiver.accepted, "refused": receiver.refused,
            "message": f"Stopped after {idle:g} seconds without requests ({receiver.accepted} accepted, "
            f"{receiver.refused} refused)."}


# --------------------------------------------------------------------------- commands


def _cmd_serve(args: argparse.Namespace) -> Any:
    check_allowed("write")
    layout = paths.server_layout(args.project, args.server_root)
    return serve(layout, socket_path=args.socket, idle=args.idle)


def _read_stdin_body() -> bytes:
    data = sys.stdin.buffer.read(MAX_BODY + 1) if not sys.stdin.isatty() else b""
    if len(data) > MAX_BODY:
        raise UsageError(f"The body is larger than {MAX_BODY} bytes, so the receiver would refuse it.",
                         hint="Send a smaller body.")
    return data


def _cmd_sign(args: argparse.Namespace) -> Any:
    check_allowed("read")
    secret = load_secret(args.secret_file, owner_only=True)
    ts = str(int(time.time()) if args.timestamp is None else args.timestamp)
    if not _TS_RE.fullmatch(ts):
        raise UsageError("--timestamp must be a whole number of seconds.", hint="Leave it out to use now.")
    body = _read_stdin_body()
    signature = sign(secret, ts, body)
    headers = [f"X-Mes-Timestamp: {ts}", f"X-Mes-Signature: {signature}"]
    if args.json:
        return {"timestamp": ts, "signature": signature, "headers": headers, "body_bytes": len(body)}
    return "\n".join(headers)


def _cmd_secret_check(args: argparse.Namespace) -> Any:
    check_allowed("read")
    layout = paths.server_layout(args.project, args.server_root)
    secret = load_secret(layout.webhook_key)
    return {"project": layout.project, "ok": True, "secret_bytes": len(secret),
            "message": "The webhook secret is there, strong enough, and only its owner can change it."}


def _seconds(text: str) -> float:
    value = float(text)
    if not 0 < value <= 3600:
        raise ValueError(text)
    return value


_seconds.__name__ = "number of seconds"


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "webhook",
        help="Signed webhooks that start a project's jobs on a server (serve, sign, secret-check).",
        description="Signed webhooks start jobs marked webhook = true. See: mes guide webhooks",
    )
    sub = p.add_subparsers(dest="webhook_command", metavar="<serve|sign|secret-check>")
    s = sub.add_parser("serve", help="(Started by systemd for the webhook socket.) Check signed webhooks.")
    s.add_argument("--project", required=True, help="the project's name")
    s.add_argument("--server-root", default=str(paths.SERVER_ROOT), metavar="DIR",
                   help="the server folder (default /var/lib/mes)")
    s.add_argument("--socket", default=None, help=argparse.SUPPRESS)
    s.add_argument("--idle", type=_seconds, default=IDLE_EXIT_SEC, help=argparse.SUPPRESS)
    s.set_defaults(func=_cmd_serve)
    g = sub.add_parser("sign", help="Print the signature headers for a body read from standard input (for testing).")
    g.add_argument("--secret-file", required=True, metavar="FILE",
                   help="a file only you can read, holding the webhook secret")
    g.add_argument("--timestamp", type=int, default=None, help="unix seconds (default: now)")
    g.set_defaults(func=_cmd_sign)
    c = sub.add_parser("secret-check", help="Check that the project's webhook secret on the server is usable.")
    c.add_argument("--project", required=True, help="the project's name")
    c.add_argument("--server-root", default=str(paths.SERVER_ROOT), metavar="DIR",
                   help="the server folder (default /var/lib/mes)")
    c.set_defaults(func=_cmd_secret_check)
