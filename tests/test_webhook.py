"""Tests for signed webhooks (mes.webhook): the pure checks, the receiver over a real unix
socket, and the command line."""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from mes import cli, paths, webhook
from mes.errors import NotFound, Refused

SECRET = bytes(range(32))
NOW = 1_800_000_000

PROJECT_TOML = """
[project]
name = "hooky"

[jobs.build]
run = "hooky build"
webhook = true

[jobs.nightly]
run = "hooky build"
schedule = "daily"
"""


@pytest.fixture
def cli_with_webhook(monkeypatch):
    if "mes.webhook" not in cli.COMMAND_MODULES:
        monkeypatch.setattr(cli, "COMMAND_MODULES", cli.COMMAND_MODULES + ("mes.webhook",))


def run_cli(*argv, stdin: bytes = b""):
    out = io.StringIO()

    class FakeStdin:
        buffer = io.BytesIO(stdin)

        def isatty(self):
            return False

    old = sys.stdin
    sys.stdin = FakeStdin()
    try:
        with redirect_stdout(out):
            code = cli.run(list(argv))
    finally:
        sys.stdin = old
    return code, out.getvalue()


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "srv"
    layout = paths.server_layout("hooky", root)
    for d in (layout.base, layout.etc, layout.state, layout.current_project):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(layout.state, 0o700)
    (layout.current_project / "project.toml").write_text(PROJECT_TOML)
    layout.webhook_key.write_text(SECRET.hex() + "\n")
    os.chmod(layout.webhook_key, 0o640)
    return layout


# --------------------------------------------------------------------------- pure checks


def _sig(body=b"{}", ts=NOW, secret=SECRET):
    return webhook.sign(secret, ts, body)


def test_valid_signature_passes():
    assert webhook.verify(SECRET, str(NOW), _sig(), b"{}", NOW, {}) is None
    assert webhook.verify(SECRET, str(NOW), _sig(), b"{}", NOW + 299, {}) is None
    assert webhook.verify(SECRET, str(NOW), _sig(), b"{}", NOW - 299, {}) is None


@pytest.mark.parametrize("case, expected", [
    ("wrong_secret", "bad_signature"),
    ("tampered_body", "bad_signature"),
    ("old", "stale"),
    ("future", "stale"),
    ("bad_ts", "bad_timestamp"),
    ("negative_ts", "bad_timestamp"),
    ("malformed_sig", "bad_signature_header"),
    ("uppercase_sig", "bad_signature_header"),
    ("no_prefix", "bad_signature_header"),
    ("replay", "replay"),
    ("oversize", "too_large"),
    ("timestamp_swapped", "bad_signature"),
])
def test_refusals(case, expected):
    body, ts, sig, now, seen = b"{}", str(NOW), _sig(), NOW, {}
    if case == "wrong_secret":
        sig = _sig(secret=b"x" * 32)
    elif case == "tampered_body":
        body = b'{"a":1}'
    elif case == "old":
        now = NOW + 301
    elif case == "future":
        now = NOW - 301
    elif case == "bad_ts":
        ts = "12.5"
    elif case == "negative_ts":
        ts = "-5"
    elif case == "malformed_sig":
        sig = "sha256=zz"
    elif case == "uppercase_sig":
        sig = sig.upper()
    elif case == "no_prefix":
        sig = sig.split("=", 1)[1]
    elif case == "replay":
        seen = {sig: NOW}
    elif case == "oversize":
        body = b"x" * (webhook.MAX_BODY + 1)
        sig = _sig(body)
    elif case == "timestamp_swapped":
        ts = str(NOW + 1)
    assert webhook.verify(SECRET, ts, sig, body, now, seen) == expected


def test_missing_headers_are_refused():
    assert webhook.verify(SECRET, None, _sig(), b"{}", NOW, {}) == "bad_timestamp"
    assert webhook.verify(SECRET, str(NOW), None, b"{}", NOW, {}) == "bad_signature_header"


def test_job_problem():
    from mes import manifest

    project = manifest.loads(PROJECT_TOML)
    assert webhook.job_problem(project, "build") is None
    assert webhook.job_problem(project, "nightly") == "not_webhook"
    assert webhook.job_problem(project, "missing") == "unknown_job"
    for bad in ("../build", "build/../x", "Build", "", "a" * 40, "build\n"):
        assert webhook.job_problem(project, bad) == "bad_job"


def test_parse_secret():
    assert webhook.parse_secret(SECRET.hex()) == SECRET
    import base64

    assert webhook.parse_secret(base64.b64encode(SECRET).decode()) == SECRET
    assert webhook.parse_secret(base64.urlsafe_b64encode(SECRET).decode().rstrip("=")) == SECRET
    for weak in ("", "abcd", "00" * 31, "not a secret!"):
        with pytest.raises(Refused):
            webhook.parse_secret(weak)


def test_load_secret_refuses_unsafe_files(tmp_path):
    key = tmp_path / "k"
    with pytest.raises(NotFound):
        webhook.load_secret(key)
    key.write_text(SECRET.hex())
    os.chmod(key, 0o600)
    assert webhook.load_secret(key, owner_only=True) == SECRET
    os.chmod(key, 0o640)
    assert webhook.load_secret(key) == SECRET          # group may read (the project's account)
    with pytest.raises(Refused):
        webhook.load_secret(key, owner_only=True)      # but not for a personal signing file
    os.chmod(key, 0o660)
    with pytest.raises(Refused):
        webhook.load_secret(key)
    os.chmod(key, 0o600)
    link = tmp_path / "link"
    link.symlink_to(key)
    with pytest.raises(Refused):
        webhook.load_secret(link)


# --------------------------------------------------------------------------- the receiver


class Running:
    def __init__(self, layout, idle=5.0):
        self.folder = tempfile.mkdtemp(prefix="mwh", dir="/tmp")  # short path: unix socket limit
        self.path = os.path.join(self.folder, "s")
        self.result = None
        self.error = None
        self.listener = webhook._test_listener(self.path)

        def go():
            try:
                self.result = webhook.serve(layout, listener=self.listener, idle=idle)
            except BaseException as e:  # noqa: BLE001
                self.error = e

        self.thread = threading.Thread(target=go, daemon=True)
        self.thread.start()

    def raw(self, data: bytes, timeout=5.0) -> tuple[int, dict]:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(timeout)
        c.connect(self.path)
        c.sendall(data)
        got = b""
        while True:
            block = c.recv(65536)
            if not block:
                break
            got += block
        c.close()
        head, _, body = got.partition(b"\r\n\r\n")
        return int(head.split(b" ")[1]), json.loads(body)

    def post(self, job="build", body=b'{"ref":"main"}', ts=None, sig=None, extra=b""):
        ts = str(int(time.time())) if ts is None else ts
        sig = webhook.sign(SECRET, ts, body) if sig is None else sig
        req = (f"POST /hooks/{job} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n"
               f"X-Mes-Timestamp: {ts}\r\nX-Mes-Signature: {sig}\r\n").encode() + extra + b"\r\n" + body
        return self.raw(req)

    def stop(self):
        self.thread.join(timeout=15)
        shutil.rmtree(self.folder, ignore_errors=True)


def test_receiver_accepts_and_writes_trigger(server):
    r = Running(server, idle=1.0)
    try:
        status, body = r.post()
        assert (status, body) == (202, {"accepted": True, "job": "build"})
        trigger = server.triggers / "build"
        record = json.loads(trigger.read_text())
        assert set(record) == {"time", "body_sha256"} and "main" not in trigger.read_text()
        assert oct(server.triggers.stat().st_mode & 0o777) == "0o700"
    finally:
        r.stop()
    assert r.error is None and r.result["accepted"] == 1


def test_receiver_refuses_replay_also_after_restart(server):
    ts = str(int(time.time()))
    body = b"{}"
    sig = webhook.sign(SECRET, ts, body)
    r = Running(server, idle=1.0)
    try:
        assert r.post(body=body, ts=ts, sig=sig)[0] == 202
        assert r.post(body=body, ts=ts, sig=sig) == (401, {"error": "replay"})
    finally:
        r.stop()
    r2 = Running(server, idle=1.0)
    try:
        assert r2.post(body=body, ts=ts, sig=sig) == (401, {"error": "replay"})
    finally:
        r2.stop()


@pytest.mark.parametrize("kwargs, expected", [
    ({"sig": "sha256=" + "0" * 64}, (401, {"error": "bad_signature"})),
    ({"ts": str(int(time.time()) - 1000)}, (401, {"error": "stale"})),
    ({"job": "nightly"}, (404, {"error": "unknown_job"})),
    ({"job": "missing"}, (404, {"error": "unknown_job"})),
    ({"job": "..%2fbuild"}, (404, {"error": "unknown_job"})),
    ({"job": "a/../build"}, (404, {"error": "not_found"})),
])
def test_receiver_refusals(server, kwargs, expected):
    r = Running(server, idle=1.0)
    try:
        assert r.post(**kwargs) == expected
        assert not (server.triggers / "build").exists()
    finally:
        r.stop()


def test_receiver_protocol_limits(server):
    r = Running(server, idle=1.0)
    try:
        assert r.raw(b"GET /hooks/build HTTP/1.1\r\nContent-Length: 0\r\n\r\n")[0] == 405
        assert r.raw(b"POST /hooks/build HTTP/1.1\r\n\r\n") == (411, {"error": "length_required"})
        assert r.raw(b"POST /hooks/build HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n")[0] == 411
        big = webhook.MAX_BODY + 1
        assert r.raw(f"POST /hooks/build HTTP/1.1\r\nContent-Length: {big}\r\n\r\n".encode()) == \
            (413, {"error": "too_large"})
        assert r.raw(b"POST /hooks/build HTTP/1.1\r\nX: " + b"a" * 9000 + b"\r\n\r\n")[0] == 400
        assert r.raw(b"nonsense\r\n\r\n")[0] == 400
        dup = b"POST /hooks/build HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n{}"
        assert r.raw(dup)[0] == 400
        assert r.post(extra=b"X-Mes-Signature: sha256=" + b"1" * 64 + b"\r\n")[0] == 400
    finally:
        r.stop()


def test_receiver_times_out_slow_clients(server, monkeypatch):
    monkeypatch.setattr(webhook, "READ_TIMEOUT_SEC", 0.5)
    r = Running(server, idle=2.0)
    try:
        assert r.raw(b"POST /hooks/build HTTP/1.1\r\nContent-Length: 10\r\n\r\n{}") == (408, {"error": "timeout"})
    finally:
        r.stop()


def test_receiver_refuses_to_start_without_a_good_secret(server):
    server.webhook_key.write_text("short")
    with pytest.raises(Refused):
        webhook.serve(server, socket_path="/nonexistent/s")
    server.webhook_key.unlink()
    with pytest.raises(NotFound):
        webhook.serve(server, socket_path="/nonexistent/s")


def test_receiver_refuses_unsafe_trigger_folder(server, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    server.triggers.symlink_to(elsewhere)
    with pytest.raises(Refused):
        webhook.Receiver(server, SECRET)


def test_receiver_exits_when_idle(server):
    r = Running(server, idle=0.3)
    r.thread.join(timeout=5)
    assert not r.thread.is_alive() and r.result["accepted"] == 0
    r.stop()


# --------------------------------------------------------------------------- command line


def test_cli_sign(tmp_path, cli_with_webhook):
    key = tmp_path / "key"
    key.write_text(SECRET.hex())
    os.chmod(key, 0o600)
    code, out = run_cli("--json", "webhook", "sign", "--secret-file", str(key), "--timestamp", str(NOW), stdin=b"{}")
    data = json.loads(out)
    assert code == 0 and data["signature"] == webhook.sign(SECRET, NOW, b"{}")
    assert data["headers"] == [f"X-Mes-Timestamp: {NOW}", f"X-Mes-Signature: {data['signature']}"]
    code, out = run_cli("webhook", "sign", "--secret-file", str(key), "--timestamp", str(NOW), stdin=b"{}")
    assert code == 0 and out.splitlines()[0] == f"X-Mes-Timestamp: {NOW}"
    os.chmod(key, 0o644)
    code, out = run_cli("--json", "webhook", "sign", "--secret-file", str(key), stdin=b"{}")
    assert code == 5 and json.loads(out)["error"]["code"] == "untrusted_file"


def test_cli_serve_refuses_without_socket_activation(server, cli_with_webhook, monkeypatch):
    monkeypatch.delenv("LISTEN_FDS", raising=False)
    code, out = run_cli("--json", "webhook", "serve", "--project", "hooky", "--server-root", str(server.root))
    assert code == 5 and json.loads(out)["error"]["code"] == "not_socket_activated"


def test_cli_secret_check(server, cli_with_webhook):
    code, out = run_cli("--json", "webhook", "secret-check", "--project", "hooky", "--server-root", str(server.root))
    assert code == 0 and json.loads(out)["secret_bytes"] == 32


def test_serve_needs_write_level(server, cli_with_webhook, monkeypatch):
    monkeypatch.setenv("MES_ALLOW", "read")
    code, out = run_cli("--json", "webhook", "serve", "--project", "hooky", "--server-root", str(server.root))
    assert code == 4
