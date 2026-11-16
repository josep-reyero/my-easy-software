"""Tests for ``mes gate`` and the per-machine app registry (``mes apps``).

A tiny fake app built on ``mes.App`` is written into a temp folder and run as
a real program, exactly as the gate would run an installed app.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from mes import cli, gate, registry

FAKE_APP = '''\
#!{python}
import os, sys, time
from mes import App

app = App("fakeapp", root={root!r})

@app.command("summary", kind="read", help="Numbers.")
def summary(ctx, days: int = 7):
    return {{"days": days}}

@app.command("echo", kind="read")
def echo(ctx, words: list[str]):
    return {{"words": words}}

@app.command("env", kind="read")
def env(ctx):
    return dict(os.environ)

@app.command("where", kind="read")
def where(ctx):
    return {{"cwd": os.getcwd(), "argv": sys.argv[1:], "stdin": sys.stdin.read()}}

@app.command("big", kind="read")
def big(ctx, size: int = 1000):
    return {{"blob": "x" * size}}

@app.command("slow", kind="read")
def slow(ctx, seconds: float = 30.0):
    time.sleep(seconds)
    return {{"slept": seconds}}

@app.command("fail", kind="read")
def fail(ctx):
    sys.stderr.write("plain failure\\n")
    sys.exit(9)

@app.command("crash", kind="read")
def crash(ctx):
    open("/nonexistent-owner-secret-path/token?key=SECRET123")

@app.command("limits", kind="read")
def limits(ctx):
    import resource
    return {{"cpu": resource.getrlimit(resource.RLIMIT_CPU)[0],
             "fsize": resource.getrlimit(resource.RLIMIT_FSIZE)[0],
             "core": resource.getrlimit(resource.RLIMIT_CORE)[0]}}

@app.command("add", kind="write")
def add(ctx, value: str):
    (ctx.data_dir / "added.txt").write_text(value)
    return {{"added": value}}

@app.command("reset", kind="admin")
def reset(ctx):
    (ctx.data_dir / "reset.txt").write_text("reset")
    return {{"reset": True}}

if __name__ == "__main__":
    app.main()
'''


def _write_program(folder: Path, name: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text(body)
    p.chmod(0o755)
    return p


@pytest.fixture
def fake_app(tmp_path) -> Path:
    root = tmp_path / "fakeapp"
    return _write_program(root, "fakeapp", FAKE_APP.format(python=sys.executable, root=str(root)))


@pytest.fixture
def registered(fake_app) -> Path:
    registry.add("fakeapp", fake_app)
    return fake_app


def _run(request, allow="read", **kw) -> gate.Outcome:
    raw = request if isinstance(request, str) else json.dumps(request)
    return gate.handle(raw, allow, **kw)


def _json(outcome: gate.Outcome) -> dict:
    return json.loads(outcome.stdout.decode())


def _log_lines() -> list[dict]:
    p = gate.log_path()
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines()]


# --------------------------------------------------------------------------- registry


def test_registry_add_list_remove(fake_app, mes_home):
    result = registry.add("fakeapp", fake_app)
    assert result["status"] == "added"
    assert "summary" in result["commands"]["read"]
    assert result["commands"]["admin"] == ["reset"]
    path = registry.registry_path()
    assert path == mes_home["config"].parent / "apps.toml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert registry.load() == {"fakeapp": str(fake_app)}
    assert registry.list_apps() == [
        {"app": "fakeapp", "program": str(fake_app), "usable": True, "problem": None}
    ]
    assert registry.add("fakeapp", fake_app)["status"] == "updated"
    assert registry.remove("fakeapp")["status"] == "removed"
    assert registry.load() == {}
    with pytest.raises(registry.NotFound):
        registry.remove("fakeapp")


def test_registry_add_refuses_wrong_or_broken_program(tmp_path, fake_app):
    with pytest.raises(registry.Refused):  # the program says it is "fakeapp"
        registry.add("otherapp", fake_app)
    not_mes = _write_program(tmp_path / "bin", "notmes", "#!/bin/sh\necho hello\n")
    with pytest.raises(registry.MesError) as e:
        registry.add("notmes", not_mes)
    assert e.value.code == "not_an_app" and "is not an app made with mes" in e.value.message
    with pytest.raises(registry.MesError):
        registry.add("missing", tmp_path / "does-not-exist")
    for bad in ("mes", "../x", "Fake", "a", "fake\n", "fake app"):
        with pytest.raises(registry.UsageError):
            registry.add(bad, fake_app)
    assert registry.load() == {}


def test_registry_refuses_writable_by_others(registered):
    path = registry.registry_path()
    path.chmod(0o666)
    with pytest.raises(registry.Refused):
        registry.load()
    path.chmod(0o600)
    registered.chmod(0o777)
    with pytest.raises(registry.Refused):
        registry.check_program(registered)


def test_registry_broken_file_fails_closed(mes_home):
    path = registry.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in ("not toml [", "[other]\nx = 1\n", "[apps]\nfakeapp = \"relative/path\"\n",
                 "[apps]\nmes = \"/bin/sh\"\n", "[apps]\n\"../x\" = \"/bin/sh\"\n", "apps = 3\n"):
        path.write_text(text)
        path.chmod(0o600)
        with pytest.raises(registry.ConfigError):
            registry.load()
    out = _run(["fakeapp", "summary"])
    assert out.exit_code == 1
    assert b"list of installed apps" in out.stderr


def test_mes_apps_commands(fake_app, capsys):
    assert cli.run(["apps", "add", "fakeapp", str(fake_app), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "added"
    assert cli.run(["--json", "apps", "list"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [a["app"] for a in data["apps"]] == ["fakeapp"]
    assert cli.run(["apps", "remove", "fakeapp"]) == 0
    capsys.readouterr()
    assert cli.run(["apps", "remove", "fakeapp", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"
    assert cli.run(["apps"]) == 2


# --------------------------------------------------------------------------- allowed requests


def test_read_command_passes_through_stdout_and_exit_code(registered):
    out = _run(["fakeapp", "summary", "--days", "3", "--json"])
    assert out.exit_code == 0
    assert _json(out) == {"days": 3}
    text = _run(["fakeapp", "summary"])
    assert text.exit_code == 0 and b"days: 7" in text.stdout
    failed = _run(["fakeapp", "fail"])
    assert failed.exit_code == 9
    assert failed.stderr == b"plain failure\n"


def test_app_usage_errors_pass_through(registered):
    out = _run(["fakeapp", "summary", "--days", "many", "--json"])
    assert out.exit_code == 2
    assert _json(out)["error"]["code"] == "usage"


def test_write_needs_write_key(registered, mes_home):
    out = _run(["fakeapp", "add", "hello", "--json"], allow="read")
    assert out.exit_code == 4
    assert _json(out)["error"]["code"] == "not_allowed"
    assert not (mes_home["data"] / "fakeapp" / "added.txt").exists()
    ok = _run(["fakeapp", "add", "hello", "--json"], allow="write")
    assert ok.exit_code == 0 and _json(ok) == {"added": "hello"}
    assert (mes_home["data"] / "fakeapp" / "added.txt").read_text() == "hello"


def test_admin_never_runs_through_gate(registered, mes_home):
    for allow in ("read", "write"):
        out = _run(["fakeapp", "reset", "--json"], allow=allow)
        assert out.exit_code == 4
        assert "admin" in _json(out)["error"]["message"]
    refused = _run(["fakeapp", "summary", "--json"], allow="admin")
    assert refused.exit_code == 5
    for bad in ("", "root", "READ", None):
        assert _run(["fakeapp", "summary"], allow=bad).exit_code == 2
    assert not (mes_home["data"] / "fakeapp" / "reset.txt").exists()


def test_builtin_commands_are_read(registered):
    out = _run(["fakeapp", "describe", "--json"])
    assert out.exit_code == 0
    assert _json(out)["allowed"] == "read"
    write = _run(["fakeapp", "describe", "--json"], allow="write")
    assert _json(write)["allowed"] == "write"
    assert _run(["fakeapp", "guide"]).exit_code == 0


def test_clean_environment(registered, monkeypatch):
    for name in ("SSH_AUTH_SOCK", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "PYTHONPATH",
                 "PYTHONSTARTUP", "MES_DEBUG", "LD_PRELOAD", "TERM", "LC_ALL"):
        monkeypatch.setenv(name, "secret-value")
    monkeypatch.setenv("MES_ALLOW", "admin")
    monkeypatch.setenv("PATH", "/tmp/evil:" + os.environ["PATH"])
    env = _json(_run(["fakeapp", "env", "--json"]))
    assert env["PATH"] == gate.SAFE_PATH
    assert env["MES_ALLOW"] == "read"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert "secret-value" not in json.dumps(env)
    assert env["MES_DATA_HOME"] == os.environ["MES_DATA_HOME"]


def test_runs_without_shell_stdin_or_cwd(registered):
    hostile = ["$(touch /tmp/mes-gate-pwned)", "; rm -rf /", "`id`", "a|b", "&& echo x", "'\"", "*", "~"]
    out = _run(["fakeapp", "echo", *hostile, "--json"])
    assert out.exit_code == 0
    assert _json(out)["words"] == hostile
    assert not Path("/tmp/mes-gate-pwned").exists()
    where = _json(_run(["fakeapp", "where", "--json"]))
    assert where["cwd"] == "/"
    assert where["stdin"] == ""
    assert where["argv"] == ["where", "--json"]  # exactly the request, nothing added


# --------------------------------------------------------------------------- hostile requests


@pytest.mark.parametrize(
    "raw",
    [
        "fakeapp summary",                       # a shell command line, not JSON
        "fakeapp summary; rm -rf /",
        "$(id)",
        "",
        "   ",
        '{"app": "fakeapp", "command": "summary"}',
        '"fakeapp summary"',
        '["fakeapp"]',
        "[]",
        '["fakeapp", 1]',
        '["fakeapp", ["summary"]]',
        '["fakeapp", "summary", null]',
        '["fakeapp", "summary"',
        '["fakeapp", "summary\\u0000"]',
        '["fakeapp", "summary", "a\\u0000b"]',
        '["fakeapp", "summary", "\\ud800"]',     # not valid text
        '["../../bin/sh", "summary"]',
        '["/bin/sh", "-c"]',
        '["fakeapp/../x", "summary"]',
        '["FakeApp", "summary"]',
        '["fakeapp\\n", "summary"]',
        '["fakeapp", "--help"]',
        '["fakeapp", "--json"]',
        '["fakeapp", "-c"]',
        '["fakeapp", "../summary"]',
        '["fakeapp", "summary\\n"]',
        '["fakeapp", "Summary"]',
        '["fakeapp", ""]',
    ],
)
def test_malformed_requests_are_refused(registered, raw):
    out = _run(raw)
    assert out.exit_code == 2, (raw, out)
    if '"--json"' in raw:
        assert _json(out)["error"]["code"] == "bad_request"
    else:
        assert out.stdout == b""
        assert out.stderr  # a plain message


def test_malformed_request_json_error(registered):
    out = _run("not json", json_mode=True)
    assert out.exit_code == 2
    err = _json(out)["error"]
    assert err["code"] == "bad_request" and err["message"] and err["hint"]


def test_size_limits(registered):
    assert _run(["fakeapp", "echo", "x" * gate.MAX_ARG_BYTES]).exit_code == 0
    assert _run(["fakeapp", "echo", "x" * (gate.MAX_ARG_BYTES + 1)]).exit_code == 2
    assert _run(["fakeapp", "echo", "é" * (gate.MAX_ARG_BYTES // 2 + 1)]).exit_code == 2  # bytes, not chars
    assert _run(["fakeapp", "echo", *["w"] * (gate.MAX_ITEMS - 2)]).exit_code == 0
    assert _run(["fakeapp", "echo", *["w"] * (gate.MAX_ITEMS - 1)]).exit_code == 2
    assert _run(" " * (gate.MAX_REQUEST_BYTES + 1)).exit_code == 2
    assert _run("[" * 100_000 + "]" * 100_000).exit_code == 2


def test_unknown_app_or_command(registered):
    out = _run(["nosuchapp", "summary", "--json"])
    assert out.exit_code == 3 and _json(out)["error"]["code"] == "not_found"
    out = _run(["fakeapp", "nosuch", "--json"])
    assert out.exit_code == 3 and _json(out)["error"]["code"] == "not_found"


def test_option_injection_stays_with_the_app_command(registered):
    for args in (["--config=/etc/x"], ["--allow", "admin"], ["-c", "import os"], ["--json=1"],
                 ["@/etc/passwd"], ["-m", "http.server"]):
        out = _run(["fakeapp", "summary", *args, "--json"])
        assert out.exit_code == 2, args  # the app's own parser refuses the unknown option
        assert _json(out)["error"]["code"] == "usage"
    # values are values, even when they look like options of mes or of Python
    out = _run(["fakeapp", "echo", "--json", "--", "--config=/etc/x", "-c", "--allow=admin"])
    assert out.exit_code == 0
    assert _json(out)["words"] == ["--config=/etc/x", "-c", "--allow=admin"]


def test_program_writable_by_others_is_refused(registered):
    registered.chmod(0o777)
    out = _run(["fakeapp", "summary", "--json"])
    assert out.exit_code == 5


def test_registry_pointing_at_other_program(registered, tmp_path):
    impostor = _write_program(tmp_path / "imp", "impostor", "#!/bin/sh\necho '{\"app\": \"other\", \"commands\": []}'\n")
    registry._save({"fakeapp": str(impostor)})
    assert _run(["fakeapp", "summary"]).exit_code == 5
    liar = _write_program(tmp_path / "imp", "liar",
                          "#!/bin/sh\necho '{\"app\": \"fakeapp\", \"commands\": [{\"name\": \"x\", \"kind\": \"root\"}]}'\n")
    registry._save({"fakeapp": str(liar)})
    assert _run(["fakeapp", "x"]).exit_code == 1
    shell = _write_program(tmp_path / "imp", "notjson", "#!/bin/sh\necho hello\n")
    registry._save({"fakeapp": str(shell)})
    assert _run(["fakeapp", "summary"]).exit_code == 1
    registry._save({"fakeapp": str(tmp_path / "gone")})
    assert _run(["fakeapp", "summary"]).exit_code == 1


def test_timeout_and_output_cap(registered):
    out = _run(["fakeapp", "slow", "--seconds", "30", "--json"], timeout=1.5)
    assert out.exit_code == 75
    assert _json(out)["error"]["code"] == "temporary"
    big = _run(["fakeapp", "big", "--size", "100000", "--json"], max_output=10_000)
    assert big.exit_code == 5
    assert b"xxxx" not in big.stdout
    small = _run(["fakeapp", "big", "--size", "100", "--json"], max_output=10_000)
    assert small.exit_code == 0


# --------------------------------------------------------------------------- log


def test_every_request_is_logged(registered):
    _run(["fakeapp", "summary", "--days", "2", "--json"])
    _run(["fakeapp", "add", "secret-word"], allow="read")
    _run("rm -rf /")
    lines = _log_lines()
    assert [(r["event"], r["decision"]) for r in lines] == [
        ("request", "allowed"), ("finished", "done"), ("request", "refused"), ("request", "refused"),
    ]
    first, done, refused, garbage = lines
    assert first["app"] == "fakeapp" and first["command"] == "summary" and first["kind"] == "read"
    assert first["allow"] == "read" and first["time"].endswith("Z") and first["args"] == 3
    assert done["exit_code"] == 0 and "duration_ms" in done
    assert refused["kind"] == "write" and refused["reason"] == "not_allowed"
    assert garbage["app"] is None and garbage["reason"] == "bad_request"
    assert "secret-word" not in gate.log_path().read_text()  # arguments are not logged
    assert stat.S_IMODE(gate.log_path().stat().st_mode) == 0o600


def test_unwritable_log_runs_nothing(registered, mes_home, monkeypatch):
    blocker = mes_home["root"] / "blocked"
    blocker.write_text("a file, not a folder")
    monkeypatch.setenv("MES_STATE_HOME", str(blocker / "state"))
    out = _run(["fakeapp", "add", "x", "--json"], allow="write")
    assert out.exit_code == 1
    assert _json(out)["error"]["code"] == "log_unavailable"
    assert not (mes_home["data"] / "fakeapp" / "added.txt").exists()


# --------------------------------------------------------------------------- mes gate command


def _mes(argv, env_command=None, monkeypatch=None):
    if env_command is not None:
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", env_command)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), pytest.raises(SystemExit) as e:
        cli.run(argv)
    return e.value.code, out.getvalue(), err.getvalue()


def test_mes_gate_reads_ssh_original_command(registered, monkeypatch):
    code, out, _ = _mes(["gate", "--allow", "read"], '["fakeapp", "summary", "--json"]', monkeypatch)
    assert code == 0 and json.loads(out) == {"days": 7}
    assert _log_lines()[0]["source"] == "ssh"


def test_mes_gate_command_option_and_conflict(registered, monkeypatch):
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    code, out, _ = _mes(["gate", "--allow", "write", '--command=["fakeapp", "add", "v", "--json"]'])
    assert code == 0 and json.loads(out) == {"added": "v"}
    code, _, err = _mes(["gate", "--allow", "read"])
    assert code == 2 and "No command was given" in err
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", '["fakeapp", "summary"]')
    code, _, err = _mes(["gate", "--allow", "read", '--command=["fakeapp", "summary"]'])
    assert code == 2 and "both" in err


def test_mes_gate_requires_allow(capsys):
    assert cli.run(["gate", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


def test_mes_gate_as_real_ssh_forced_command(registered, monkeypatch):
    """The whole path as sshd would run it: a separate process with SSH_ORIGINAL_COMMAND set."""
    import subprocess

    env = dict(os.environ, SSH_ORIGINAL_COMMAND='["fakeapp", "summary", "--days", "4", "--json"]')
    done = subprocess.run([sys.executable, "-m", "mes.cli", "gate", "--allow", "read"],
                          capture_output=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"days": 4}
    env["SSH_ORIGINAL_COMMAND"] = '["fakeapp", "reset"]'
    done = subprocess.run([sys.executable, "-m", "mes.cli", "gate", "--allow", "write"],
                          capture_output=True, env=env, timeout=60)
    assert done.returncode == 4 and done.stdout == b""
    assert b"admin" in done.stderr


def test_parse_request_unit():
    assert gate.parse_request('["fakeapp", "summary", "--days", "3"]') == ["fakeapp", "summary", "--days", "3"]
    with pytest.raises(gate.UsageError):
        gate.parse_request(None)



# --------------------------------------------------------------------------- review fixes


def test_apps_add_and_remove_need_admin(fake_app, capsys, monkeypatch):
    monkeypatch.setenv("MES_ALLOW", "read")
    assert cli.run(["--json", "apps", "add", "fakeapp", str(fake_app)]) == 4
    assert registry.load() == {}
    monkeypatch.setenv("MES_ALLOW", "admin")
    assert cli.run(["--json", "apps", "add", "fakeapp", str(fake_app)]) == 0
    monkeypatch.setenv("MES_ALLOW", "write")
    assert cli.run(["--json", "apps", "remove", "fakeapp"]) == 4
    monkeypatch.setenv("MES_ALLOW", "read")
    assert cli.run(["--json", "apps", "list"]) == 0
    capsys.readouterr()
    assert "fakeapp" in registry.load()


def test_writable_folders_are_not_trusted(registered, mes_home):
    folder = registered.parent
    folder.chmod(0o777)
    try:
        with pytest.raises(registry.Refused) as e:
            registry.check_program(registered)
        assert "folder" in e.value.message
        out = _run(["fakeapp", "summary", "--json"])
        assert out.exit_code == 5 and str(folder) not in out.stdout.decode()
    finally:
        folder.chmod(0o755)
    reg_folder = registry.registry_path().parent
    reg_folder.chmod(0o777)
    try:
        with pytest.raises(registry.Refused):
            registry.load()
        out = _run(["fakeapp", "summary", "--json"])
        assert out.exit_code == 5 and str(reg_folder) not in out.stdout.decode()
    finally:
        reg_folder.chmod(0o700)
    assert _run(["fakeapp", "summary", "--json"]).exit_code == 0


def test_sticky_shared_folder_is_fine(tmp_path, fake_app):
    shared = tmp_path / "sticky"
    shared.mkdir()
    shared.chmod(0o1777)
    prog = shared / "fakeapp"
    prog.write_text(fake_app.read_text())
    prog.chmod(0o755)
    assert registry.trust_problem(prog) is None


def test_files_of_another_account_are_not_trusted(registered, monkeypatch):
    monkeypatch.setattr(registry.os, "getuid", lambda: 4242)
    registry._private_group.cache_clear()
    try:
        assert "another account" in registry.trust_problem(registered)
        with pytest.raises(registry.Refused):
            registry.check_program(registered)
    finally:
        registry._private_group.cache_clear()


def test_json_before_the_command_is_accepted(registered):
    out = _run(["fakeapp", "--json", "summary", "--days", "3"])
    assert out.exit_code == 0 and _json(out) == {"days": 3}
    assert gate.parse_request('["fakeapp", "--json", "summary"]') == ["fakeapp", "summary", "--json"]
    with pytest.raises(gate.UsageError):
        gate.parse_request('["fakeapp", "--json"]')


def test_gate_errors_show_no_paths_or_exception_text(registered, tmp_path):
    out = _run(["fakeapp", "crash", "--json"])
    text = out.stdout.decode() + out.stderr.decode()
    assert out.exit_code == 1 and "SECRET123" not in text and "nonexistent" not in text
    assert "MES_DEBUG" not in text
    moved = tmp_path / "moved-away"
    registered.rename(moved)
    out = _run(["fakeapp", "summary", "--json"])
    text = out.stdout.decode() + out.stderr.decode()
    assert out.exit_code != 0 and str(registered) not in text
    assert _json(out)["error"]["message"].startswith('The program of the app "fakeapp" does not exist')


def test_gate_sets_resource_limits(registered):
    out = _run(["fakeapp", "limits", "--json"])
    assert out.exit_code == 0
    data = _json(out)
    assert data["cpu"] == int(gate.DEFAULT_TIMEOUT) + gate.CPU_GRACE
    assert data["fsize"] == gate.FILE_SIZE_LIMIT and data["core"] == 0


def test_gate_caps_parallel_requests(registered, monkeypatch):
    monkeypatch.setattr(gate, "MAX_PARALLEL", 1)
    with gate._slot():
        out = _run(["fakeapp", "summary", "--json"])
    assert out.exit_code == 75 and _json(out)["error"]["code"] == "busy"
    assert _run(["fakeapp", "summary", "--json"]).exit_code == 0  # free again afterwards


def test_apps_add_plain_messages(fake_app, tmp_path):
    with pytest.raises(registry.Refused) as e:
        registry.add("otherapp", fake_app)
    assert 'This program is the app "fakeapp", not "otherapp". Nothing was added.' in e.value.message
    assert e.value.hint.startswith("Register it under its own name: mes apps add fakeapp ")


def test_gate_examples_use_a_neutral_name(registered):
    out = _run(["Bad Name", "x"])
    text = out.stdout.decode() + out.stderr.decode()
    assert "fitness" not in text and "mes guide gate" in gate._bad_request("x").hint
