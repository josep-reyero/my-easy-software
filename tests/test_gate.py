"""Tests for ``mes gate`` and the per-machine list of projects (``mes projects``).

A tiny fake app built on ``mes.App`` is written into a project folder and run as a real program,
exactly as the gate would run an installed app. Two places are tested:

- a personal machine: the project comes from the owner's list (``mes projects add``);
- a server: the project lives in a temporary server folder laid out like ``/var/lib/mes`` (with
  ``code/current -> releases/1``), and the per-request sandboxes are stood in for by a real unix
  socket served by ``gate.child_main`` (what systemd's ``Accept=yes`` instance runs).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from mes import cli, gate, paths, registry, units

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

PROJECT_TOML = '[project]\nname = "fakeproj"\n\n[apps.fakeapp]\ncli = "fakeapp"\n'
# The program "fakeapp" is installed for the app "otherapp" (the app "fakeapp" exists, under another program).
WRONG_TOML = '[project]\nname = "wrong"\n[apps.otherapp]\ncli = "fakeapp"\n[apps.fakeapp]\ncli = "fake-other"\n'


def _write_program(folder: Path, name: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text(body)
    p.chmod(0o755)
    return p


def make_project(folder: Path, toml: str = PROJECT_TOML) -> Path:
    """A project folder with project.toml and its installed program; returns the program."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "project.toml").write_text(toml)
    return _write_program(folder / ".venv" / "bin", "fakeapp", FAKE_APP.format(python=sys.executable, root=str(folder)))


@pytest.fixture
def fake_app(tmp_path) -> Path:
    return make_project(tmp_path / "fakeproj")


@pytest.fixture
def project_dir(fake_app) -> Path:
    return fake_app.parent.parent.parent


@pytest.fixture
def registered(fake_app, project_dir) -> Path:
    registry.add(project_dir)
    return fake_app


def _run(request, allow="read", **kw) -> gate.Outcome:
    raw = request if isinstance(request, str) else json.dumps(request)
    kw.setdefault("project", "fakeproj")
    return gate.handle(raw, allow, **kw)


def _json(outcome: gate.Outcome) -> dict:
    return json.loads(outcome.stdout.decode())


def _log_lines(server_root=None) -> list[dict]:
    p = gate.log_path(gate.locate("fakeproj", server_root))
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines()]


def _data(mes_home) -> Path:
    return mes_home["data"] / "fakeproj"


# --------------------------------------------------------------------------- the list of projects


def test_registry_add_list_remove(project_dir, fake_app, mes_home):
    result = registry.add(project_dir)
    assert result["status"] == "added" and result["project"] == "fakeproj"
    assert "summary" in result["apps"]["fakeapp"]["read"]
    assert result["apps"]["fakeapp"]["admin"] == ["reset"]
    path = registry.registry_path()
    assert path == mes_home["config"].parent / "projects.toml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert registry.load() == {"fakeproj": str(project_dir)}
    assert registry.list_projects() == [
        {"project": "fakeproj", "folder": str(project_dir), "usable": True, "problem": None}
    ]
    assert registry.add(project_dir)["status"] == "updated"
    assert registry.remove("fakeproj")["status"] == "removed"
    assert registry.load() == {}
    with pytest.raises(registry.NotFound):
        registry.remove("fakeproj")


def test_registry_add_refuses_wrong_or_broken_program(tmp_path):
    wrong = tmp_path / "wrong"
    make_project(wrong, WRONG_TOML)
    with pytest.raises(registry.Refused) as e:  # the program says it is "fakeapp"
        registry.add(wrong)
    assert e.value.code == "wrong_app"
    notmes = tmp_path / "notmes"
    make_project(notmes, '[project]\nname = "notmes"\n[apps.notmes]\ncli = "notmes"\n')
    _write_program(notmes / ".venv" / "bin", "notmes", "#!/bin/sh\necho hello\n")
    with pytest.raises(registry.MesError) as e:
        registry.add(notmes)
    assert e.value.code == "not_an_app" and "is not an app made with mes" in e.value.message
    with pytest.raises(registry.MesError):
        registry.add(tmp_path / "does-not-exist")
    uninstalled = tmp_path / "uninstalled"
    uninstalled.mkdir()
    (uninstalled / "project.toml").write_text('[project]\nname = "uninstalled"\n')
    with pytest.raises(registry.MesError):
        registry.add(uninstalled)
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


def test_project_file_writable_by_others_is_refused(registered, project_dir):
    (project_dir / "project.toml").chmod(0o666)
    out = _run(["fakeapp", "summary", "--json"])
    assert out.exit_code == 5 and _json(out)["error"]["code"] == "untrusted_file"
    assert str(project_dir) not in out.stdout.decode()


def test_registry_broken_file_fails_closed(mes_home):
    path = registry.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in ("not toml [", "[other]\nx = 1\n", "[projects]\nfakeproj = \"relative/path\"\n",
                 "[projects]\n\"../x\" = \"/bin\"\n", "projects = 3\n", "[projects]\nfakeproj = 3\n"):
        path.write_text(text)
        path.chmod(0o600)
        with pytest.raises(registry.ConfigError):
            registry.load()
    out = _run(["fakeapp", "summary"])
    assert out.exit_code == 1
    assert b"list of projects" in out.stderr


def test_mes_projects_commands(project_dir, fake_app, capsys):
    assert cli.run(["projects", "add", str(project_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "added"
    assert cli.run(["--json", "projects", "list"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [p["project"] for p in data["projects"]] == ["fakeproj"]
    assert cli.run(["projects", "remove", "fakeproj"]) == 0
    capsys.readouterr()
    assert cli.run(["projects", "remove", "fakeproj", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"
    assert cli.run(["projects"]) == 2


def test_projects_add_and_remove_need_admin(project_dir, fake_app, capsys, monkeypatch):
    monkeypatch.setenv("MES_ALLOW", "read")
    assert cli.run(["--json", "projects", "add", str(project_dir)]) == 4
    assert registry.load() == {}
    monkeypatch.setenv("MES_ALLOW", "admin")
    assert cli.run(["--json", "projects", "add", str(project_dir)]) == 0
    monkeypatch.setenv("MES_ALLOW", "write")
    assert cli.run(["--json", "projects", "remove", "fakeproj"]) == 4
    monkeypatch.setenv("MES_ALLOW", "read")
    assert cli.run(["--json", "projects", "list"]) == 0
    capsys.readouterr()
    assert "fakeproj" in registry.load()


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
    assert not (_data(mes_home) / "added.txt").exists()
    ok = _run(["fakeapp", "add", "hello", "--json"], allow="write")
    assert ok.exit_code == 0 and _json(ok) == {"added": "hello"}
    assert (_data(mes_home) / "added.txt").read_text() == "hello"


def test_admin_never_runs_through_gate(registered, mes_home):
    for allow in ("read", "write", "deploy"):
        out = _run(["fakeapp", "reset", "--json"], allow=allow)
        assert out.exit_code == 4
        assert _json(out)["error"]["code"] == "not_allowed"
        if allow != "deploy":
            assert "admin" in _json(out)["error"]["message"]
    refused = _run(["fakeapp", "summary", "--json"], allow="admin")
    assert refused.exit_code == 5
    for bad in ("", "root", "READ", None, "deploy\n"):
        assert _run(["fakeapp", "summary"], allow=bad).exit_code == 2
    assert not (_data(mes_home) / "reset.txt").exists()


def test_builtin_commands_are_read(registered):
    out = _run(["fakeapp", "describe", "--json"])
    assert out.exit_code == 0
    assert _json(out)["allowed"] == "read" and _json(out)["project"] == "fakeproj"
    write = _run(["fakeapp", "describe", "--json"], allow="write")
    assert _json(write)["allowed"] == "write"
    assert _run(["fakeapp", "guide"]).exit_code == 0


def test_clean_environment(registered, monkeypatch, mes_home):
    for name in ("SSH_AUTH_SOCK", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "PYTHONPATH",
                 "PYTHONSTARTUP", "MES_DEBUG", "LD_PRELOAD", "TERM", "LC_ALL", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.setenv(name, "secret-value")
    monkeypatch.setenv("MES_ALLOW", "admin")
    monkeypatch.setenv("PATH", "/tmp/evil:" + os.environ["PATH"])
    env = _json(_run(["fakeapp", "env", "--json"]))
    assert env["PATH"] == gate.SAFE_PATH
    assert env["MES_ALLOW"] == "read"
    assert env["MES_VIA_GATE"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert "secret-value" not in json.dumps(env)
    assert env["MES_PROJECT"] == "fakeproj"
    assert env["MES_DATA_DIR"] == str(_data(mes_home))
    assert env["MES_DATA_HOME"] == os.environ["MES_DATA_HOME"]
    # A gate started inside another project's session never reaches this project's folders.
    monkeypatch.setenv("MES_PROJECT", "otherproj")
    out = _run(["fakeapp", "env", "--json"])
    assert out.exit_code == 2 and _json(out)["error"]["code"] == "wrong_project"


def test_runs_without_shell_stdin_or_cwd(registered):
    hostile = ["$(touch /tmp/mes-gate-pwned)", "; rm -rf /", "`id`", "a|b", "&& echo x", "'\"", "*", "~"]
    out = _run(["fakeapp", "echo", *hostile, "--json"])
    assert out.exit_code == 0
    assert _json(out)["words"] == hostile
    assert not Path("/tmp/mes-gate-pwned").exists()
    where = _json(_run(["fakeapp", "where", "--json"], stdin=io.BytesIO(b"sneaky input")))
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
        '["mes"]',
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
        '["mes", "--deploy"]',
        '["mes", "Deploy"]',
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


def test_unknown_project_app_or_command(registered):
    out = _run(["nosuchapp", "summary", "--json"])
    assert out.exit_code == 3 and _json(out)["error"]["code"] == "not_found"
    out = _run(["fakeapp", "nosuch", "--json"])
    assert out.exit_code == 3 and _json(out)["error"]["code"] == "not_found"
    out = _run(["fakeapp", "summary", "--json"], project="otherproj")
    assert out.exit_code == 3
    for bad in ("../x", "Fake", "x\n", None):
        assert _run(["fakeapp", "summary", "--json"], project=bad).exit_code == 2


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


def test_installed_program_that_is_another_app(registered, tmp_path):
    bin_dir = registered.parent
    _write_program(bin_dir, "fakeapp", "#!/bin/sh\necho '{\"app\": \"other\", \"commands\": []}'\n")
    assert _run(["fakeapp", "summary"]).exit_code == 5
    _write_program(bin_dir, "fakeapp",
                   "#!/bin/sh\necho '{\"app\": \"fakeapp\", \"commands\": [{\"name\": \"x\", \"kind\": \"root\"}]}'\n")
    assert _run(["fakeapp", "x"]).exit_code == 1
    _write_program(bin_dir, "fakeapp", "#!/bin/sh\necho hello\n")
    assert _run(["fakeapp", "summary"]).exit_code == 1
    registered.unlink()
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
    assert first["project"] == "fakeproj"
    assert first["isolation"] == "limits" and done["isolation"] == "limits"  # no sandbox sockets here
    assert done["exit_code"] == 0 and "duration_ms" in done
    assert refused["kind"] == "write" and refused["reason"] == "not_allowed"
    assert garbage["app"] is None and garbage["reason"] == "bad_request"
    log = gate.log_path(gate.locate("fakeproj", None))
    assert "secret-word" not in log.read_text()  # arguments are not logged
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_unwritable_log_runs_nothing(registered, mes_home, monkeypatch):
    blocker = mes_home["root"] / "blocked"
    blocker.write_text("a file, not a folder")
    monkeypatch.setenv("MES_STATE_HOME", str(blocker / "state"))
    out = _run(["fakeapp", "add", "x", "--json"], allow="write")
    assert out.exit_code == 1
    assert _json(out)["error"]["code"] == "log_unavailable"
    assert not (_data(mes_home) / "added.txt").exists()


# --------------------------------------------------------------------------- mes gate command


def _mes(argv, env_command=None, monkeypatch=None):
    if env_command is not None:
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", env_command)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), pytest.raises(SystemExit) as e:
        cli.run(argv)
    return e.value.code, out.getvalue(), err.getvalue()


def test_mes_gate_reads_ssh_original_command(registered, monkeypatch):
    code, out, _ = _mes(["gate", "--project", "fakeproj", "--allow", "read"],
                        '["fakeapp", "summary", "--json"]', monkeypatch)
    assert code == 0 and json.loads(out) == {"days": 7}
    assert _log_lines()[0]["source"] == "ssh"


def test_mes_gate_command_option_and_conflict(registered, monkeypatch):
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    code, out, _ = _mes(["gate", "--project", "fakeproj", "--allow", "write",
                         '--command=["fakeapp", "add", "v", "--json"]'])
    assert code == 0 and json.loads(out) == {"added": "v"}
    code, _, err = _mes(["gate", "--project", "fakeproj", "--allow", "read"])
    assert code == 2 and "No command was given" in err
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", '["fakeapp", "summary"]')
    code, _, err = _mes(["gate", "--project", "fakeproj", "--allow", "read", '--command=["fakeapp", "summary"]'])
    assert code == 2 and "both" in err


def test_mes_gate_requires_project_and_allow(capsys):
    assert cli.run(["gate", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"
    assert cli.run(["gate", "--json", "--allow", "read"]) == 2
    capsys.readouterr()
    assert cli.run(["gate", "--json", "--project", "fakeproj"]) == 2


def test_mes_gate_as_real_ssh_forced_command(registered, monkeypatch):
    """The whole path as sshd would run it: a separate process with SSH_ORIGINAL_COMMAND set."""
    env = dict(os.environ, SSH_ORIGINAL_COMMAND='["fakeapp", "summary", "--days", "4", "--json"]')
    argv = [sys.executable, "-m", "mes.cli", "gate", "--project", "fakeproj"]
    done = subprocess.run([*argv, "--allow", "read"], capture_output=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"days": 4}
    env["SSH_ORIGINAL_COMMAND"] = '["fakeapp", "reset"]'
    done = subprocess.run([*argv, "--allow", "write"], capture_output=True, env=env, timeout=60)
    assert done.returncode == 4 and done.stdout == b""
    assert b"admin" in done.stderr


def test_parse_request_unit():
    assert gate.parse_request('["fakeapp", "summary", "--days", "3"]') == ["fakeapp", "summary", "--days", "3"]
    assert gate.parse_request('["mes", "status"]') == ["mes", "status"]
    with pytest.raises(gate.UsageError):
        gate.parse_request(None)


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
    with gate._slot(gate.locate("fakeproj", None).gate_dir / gate.SLOTS_DIR):
        out = _run(["fakeapp", "summary", "--json"])
    assert out.exit_code == 75 and _json(out)["error"]["code"] == "busy"
    assert _run(["fakeapp", "summary", "--json"]).exit_code == 0  # free again afterwards


def test_projects_add_plain_messages(tmp_path):
    wrong = tmp_path / "wrong"
    make_project(wrong, WRONG_TOML)
    with pytest.raises(registry.Refused) as e:
        registry.add(wrong)
    assert 'does not say it is the app "otherapp", so the project was not added. Nothing changed.' in e.value.message
    assert 'App("otherapp")' in e.value.hint


def test_gate_examples_use_a_neutral_name(registered):
    out = _run(["Bad Name", "x"])
    text = out.stdout.decode() + out.stderr.decode()
    assert "fitness" not in text and "mes guide gate" in gate._bad_request("x").hint


# --------------------------------------------------------------------------- deploy level


def test_mes_operations_need_a_deploy_key(registered):
    for allow in ("read", "write"):
        out = _run(["mes", "deploy"], allow=allow)
        assert out.exit_code == 4, allow
        err = _json(out)["error"]  # mes operations always answer in JSON
        assert err["code"] == "not_allowed" and "--allow deploy" in err["hint"]
    lines = _log_lines()
    assert [(r["kind"], r["reason"]) for r in lines] == [(None, "not_allowed")] * 2


def test_deploy_key_on_a_personal_machine_installs_nothing(registered):
    out = _run(["mes", "status"], allow="deploy")
    assert out.exit_code == 5 and _json(out)["error"]["code"] == "not_a_server"
    out = _run(["mes", "shell"], allow="deploy")
    assert out.exit_code == 3 and "deploy, rollback, releases, status" in _json(out)["error"]["hint"]


def test_deploy_key_cannot_run_app_commands(registered, mes_home):
    """Finding 5: the deploy key ran app commands as write, so a stolen deploy key could change data
    without the signing key. Now it only runs the mes operations."""
    for request in (["fakeapp", "env", "--json"], ["fakeapp", "add", "v", "--json"]):
        out = _run(request, allow="deploy")
        assert out.exit_code == 4 and _json(out)["error"]["code"] == "not_allowed"
    assert _run(["fakeapp", "summary"], allow="deploy").exit_code == 4
    assert not (_data(mes_home) / "added.txt").exists()


# --------------------------------------------------------------------------- on a server


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A server folder like /var/lib/mes with fakeproj deployed as release 1. A real server always
    has the request sockets; these tests run without them, so the fail-closed switch is lifted
    (test_a_server_without_sandbox_sockets_runs_nothing keeps it on)."""
    monkeypatch.setattr(gate, "REQUIRE_SANDBOX_ON_SERVER", False)
    root = tmp_path / "srv"
    layout = paths.server_layout("fakeproj", root)
    release = layout.releases / "1"
    make_project(release / paths.RELEASE_PROJECT_DIR)
    os.symlink("releases/1", layout.code / "current")
    for d in (layout.data, layout.state, layout.store):
        d.mkdir(parents=True, mode=0o700)
    return root


def test_server_runs_the_current_release(server):
    layout = paths.server_layout("fakeproj", server)
    out = _run(["fakeapp", "where", "--json"], server_root=server)
    assert out.exit_code == 0, out
    assert Path(_json(out)["cwd"]).resolve() == (layout.releases / "1" / "project").resolve()
    assert _log_lines(server)[0]["isolation"] == "limits"  # no sandbox sockets in this folder
    # Finding 11: the gate's log and locks are in gate/, which no job or write request can change.
    assert gate.log_path(gate.locate("fakeproj", server)) == layout.gate / "gate-log.jsonl"
    assert not (layout.state / "gate").exists()


def test_a_server_without_sandbox_sockets_runs_nothing(server, monkeypatch):
    """Finding 2: on a server without the request sockets the gate used to fall back to running the
    app (and its describe) directly as the SSH account, with no sandbox at all."""
    monkeypatch.setattr(gate, "REQUIRE_SANDBOX_ON_SERVER", True)
    layout = paths.server_layout("fakeproj", server)
    for allow, request in (("read", ["fakeapp", "summary", "--json"]), ("write", ["fakeapp", "add", "x", "--json"])):
        out = _run(request, allow=allow, server_root=server)
        assert out.exit_code == 75 and _json(out)["error"]["code"] == "sandbox_unavailable"
    assert not (layout.data / "added.txt").exists()


def test_server_environment_has_only_the_project_folders(server, monkeypatch):
    monkeypatch.setenv("HOME", "/home/owner")
    layout = paths.server_layout("fakeproj", server)
    env = _json(_run(["fakeapp", "env", "--json"], server_root=server))
    assert env["HOME"] == str(layout.state)
    assert env["MES_DATA_DIR"] == str(layout.data) and env["MES_STORE_DIR"] == str(layout.store)
    assert env["MES_STATE_DIR"] == str(layout.state) and env["MES_PROJECT"] == "fakeproj"
    for k in ("MES_DATA_HOME", "MES_STATE_HOME", "MES_CONFIG", "XDG_CONFIG_HOME"):
        assert k not in env
    ok = _run(["fakeapp", "add", "srv", "--json"], allow="write", server_root=server)
    assert ok.exit_code == 0 and (layout.data / "added.txt").read_text() == "srv"


def test_server_without_a_release_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "REQUIRE_SANDBOX_ON_SERVER", False)
    root = tmp_path / "srv"
    layout = paths.server_layout("fakeproj", root)
    out = _run(["fakeapp", "summary", "--json"], server_root=root)
    assert out.exit_code == 3  # not set up at all
    layout.code.mkdir(parents=True)
    layout.state.mkdir(parents=True)
    out = _run(["fakeapp", "summary", "--json"], server_root=root)
    assert out.exit_code == 3 and "Nothing has been deployed" in _json(out)["error"]["message"]


def test_server_refuses_another_project_or_account(server, monkeypatch):
    layout = paths.server_layout("fakeproj", server)
    (layout.current_project / "project.toml").write_text('[project]\nname = "other"\n[apps.fakeapp]\ncli = "fakeapp"\n')
    out = _run(["fakeapp", "summary", "--json"], server_root=server)
    assert out.exit_code == 5 and _json(out)["error"]["code"] == "wrong_project"
    real = os.getuid()
    monkeypatch.setattr(gate.os, "getuid", lambda: real + 4242)
    out = _run(["fakeapp", "summary", "--json"], server_root=server)
    assert out.exit_code == 5 and _json(out)["error"]["code"] == "wrong_account"


def test_server_deploy_operations_go_to_mes_deploy(server, monkeypatch):
    from mes import deploy

    seen = {}

    def fake_operation(where, command, args, *, stdin, timeout, run_child):
        seen.update(where=where, command=command, args=args, stdin=stdin)
        return {"ok": True, "operation": command}

    monkeypatch.setattr(deploy, "operation", fake_operation)
    body = io.BytesIO(b"archive bytes")
    out = _run(["mes", "releases", "--limit", "3"], allow="deploy", server_root=server, stdin=body)
    assert out.exit_code == 0 and _json(out) == {"ok": True, "operation": "releases"}
    assert seen["command"] == "releases" and seen["args"] == ["--limit", "3"] and seen["stdin"] is body
    assert seen["where"].server and seen["where"].name == "fakeproj"
    lines = _log_lines(server)
    assert [(r["event"], r["kind"], r["decision"]) for r in lines] == [
        ("request", "deploy", "allowed"), ("finished", "deploy", "done")]
    out = _run(["mes", "deploy"], allow="write", server_root=server, stdin=io.BytesIO(b"x"))
    assert out.exit_code == 4 and seen["command"] == "releases"  # not called again


def test_server_deploy_operation_errors_are_json(server, monkeypatch):
    from mes import deploy
    from mes.errors import Refused

    def refusing(*a, **k):
        raise Refused("The archive's signature is not valid, so nothing was installed.", hint="Sign it.",
                      code="bad_signature")

    monkeypatch.setattr(deploy, "operation", refusing)
    out = _run(["mes", "deploy"], allow="deploy", server_root=server, stdin=io.BytesIO(b"x"))
    assert out.exit_code == 5 and _json(out)["error"]["code"] == "bad_signature"
    assert _log_lines(server)[-1]["reason"] == "bad_signature"


# --------------------------------------------------------------------------- per-request sandboxes


@pytest.fixture
def short_dir():
    d = tempfile.mkdtemp(prefix="mesg", dir="/tmp")  # unix socket paths must be short
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


class FakeInstances:
    """Stands in for systemd's socket-activated instances: every connection to a level's socket
    is answered by gate.child_main for that level, as ``mes gate child`` would."""

    def __init__(self, folder: Path, server_root: Path, levels=("read", "write", "deploy")):
        self.server_root = server_root
        self.socks = {}
        self.connections = {lvl: 0 for lvl in levels}
        for level in levels:
            path = str(folder / f"{level}.sock")
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(path)
            s.listen(4)
            self.socks[level] = (path, s)
            threading.Thread(target=self._serve, args=(level, s), daemon=True).start()

    def path(self, project, level):
        assert project == "fakeproj"
        return self.socks[level][0]

    def _serve(self, level, s):
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            self.connections[level] += 1
            with conn, conn.makefile("rb") as rf, conn.makefile("wb") as wf:
                gate.child_main("fakeproj", str(self.server_root), level, rf, wf)

    def close(self):
        for _, s in self.socks.values():
            s.close()


@pytest.fixture
def instances(server, short_dir, monkeypatch):
    fake = FakeInstances(short_dir, server)
    monkeypatch.setattr(units, "gate_socket_path", fake.path)
    yield fake
    fake.close()


def test_requests_go_to_their_own_sandbox_instance(server, instances):
    layout = paths.server_layout("fakeproj", server)
    out = _run(["fakeapp", "summary", "--days", "5", "--json"], server_root=server)
    assert out.exit_code == 0 and _json(out) == {"days": 5}
    assert instances.connections["read"] == 1
    out = _run(["fakeapp", "add", "boxed", "--json"], allow="write", server_root=server)
    assert out.exit_code == 0 and (layout.data / "added.txt").read_text() == "boxed"
    assert instances.connections["write"] == 1
    failed = _run(["fakeapp", "fail"], server_root=server)
    assert failed.exit_code == 9 and failed.stderr == b"plain failure\n"
    lines = _log_lines(server)
    assert {r["isolation"] for r in lines if r["decision"] in ("allowed", "done")} == {"systemd"}


def test_deploy_key_app_commands_reach_no_sandbox(server, instances):
    out = _run(["fakeapp", "add", "x", "--json"], allow="deploy", server_root=server)
    assert out.exit_code == 4
    assert instances.connections == {"read": 0, "write": 0, "deploy": 0}


def test_the_sandbox_checks_the_kind(server, instances):
    layout = paths.server_layout("fakeproj", server)
    out = _run(["fakeapp", "reset", "--json"], allow="write", server_root=server)
    assert out.exit_code == 4 and _json(out)["error"]["code"] == "not_allowed"
    assert instances.connections["write"] == 1  # the sandbox, not the SSH side, asked the app
    assert not (layout.data / "reset.txt").exists()
    assert _log_lines(server)[-1]["decision"] == "refused"


def test_the_ssh_side_runs_no_project_code_when_sandboxes_exist(server, instances, monkeypatch):
    """Finding 2: the SSH-side gate ran the release's app (describe) as mes-<p> outside every sandbox."""
    callers = []
    real = gate.ask_commands

    def spy(*a, **k):
        callers.append(threading.current_thread() is threading.main_thread())
        return real(*a, **k)

    monkeypatch.setattr(gate, "ask_commands", spy)
    real_limited = gate.run_limited

    def spy_run(*a, **k):
        callers.append(threading.current_thread() is threading.main_thread())
        return real_limited(*a, **k)

    monkeypatch.setattr(gate, "run_limited", spy_run)
    out = _run(["fakeapp", "summary", "--json"], server_root=server)
    assert out.exit_code == 0
    assert callers and not any(callers)  # every app start happened in the sandbox (its thread here)


def test_sandbox_limits_apply_inside_the_instance(server, instances):
    out = _run(["fakeapp", "slow", "--seconds", "30", "--json"], server_root=server, timeout=1.5)
    assert out.exit_code == 75
    big = _run(["fakeapp", "big", "--size", "100000", "--json"], server_root=server, max_output=10_000)
    assert big.exit_code == 5 and b"xxxx" not in big.stdout


def test_socket_that_exists_but_does_not_answer_refuses(server, short_dir, monkeypatch):
    path = str(short_dir / "dead.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)  # the file exists, but nothing listens
    s.close()
    monkeypatch.setattr(units, "gate_socket_path", lambda project, level: path)
    layout = paths.server_layout("fakeproj", server)
    out = _run(["fakeapp", "add", "x", "--json"], allow="write", server_root=server)
    assert out.exit_code == 75 and _json(out)["error"]["code"] == "sandbox_unavailable"
    assert not (layout.data / "added.txt").exists()  # never run with less protection
    assert _log_lines(server)[-1]["reason"] == "sandbox_unavailable"


def test_sandbox_that_answers_garbage_is_refused(server, short_dir, monkeypatch):
    path = str(short_dir / "liar.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.listen(1)

    def liar():
        conn, _ = s.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(b'{"exit_code": 0, "stdout_bytes": 999999999999, "stderr_bytes": 0}\n')

    threading.Thread(target=liar, daemon=True).start()
    monkeypatch.setattr(units, "gate_socket_path", lambda project, level: path)
    out = _run(["fakeapp", "summary", "--json"], server_root=server)
    s.close()
    assert out.exit_code == 75 and _json(out)["error"]["code"] == "sandbox_failed"


def _child(server, level, request) -> tuple[dict, bytes, bytes]:
    line = request if isinstance(request, bytes) else json.dumps(request).encode() + b"\n"
    stdout = io.BytesIO()
    assert gate.child_main("fakeproj", str(server), level, io.BytesIO(line), stdout) == 0
    stdout.seek(0)
    return gate.read_answer(stdout, gate.DEFAULT_MAX_OUTPUT)


def _child_error(answer) -> dict:
    head, out, _ = answer
    return {"exit_code": head["exit_code"], **json.loads(out)["error"]}


def test_child_runs_one_checked_request(server):
    head, out, err = _child(server, "read", {"kind": "app", "request": ["fakeapp", "summary", "--json"],
                                             "timeout": 30, "max_output": 10_000})
    assert head["exit_code"] == 0 and json.loads(out) == {"days": 7} and err == b""


@pytest.mark.parametrize("level,request_", [
    ("read", {"kind": "app", "request": ["fakeapp", "add", "x", "--json"]}),           # write at read
    ("write", {"kind": "app", "request": ["fakeapp", "reset", "--json"]}),             # admin never
    ("deploy", {"kind": "app", "request": ["fakeapp", "summary", "--json"]}),          # no app commands
    ("write", {"kind": "app", "request": ["mes", "deploy", "--json"]}),                # no mes operations
    ("read", {"kind": "app", "request": "fakeapp summary"}),                           # not a list
    ("read", {"kind": "app", "request": ["../bin/sh", "-c", "--json"]}),
    ("read", {"kind": "shell", "request": ["fakeapp", "summary", "--json"]}),
    ("write", {"kind": "sync"}),                                                       # deploy steps: deploy only
    ("read", {"kind": "app", "request": ["fakeapp", "summary"], "timeout": 99999}),
    ("read", {"kind": "app", "request": ["fakeapp", "summary"], "timeout": -1}),
    ("read", {"kind": "app", "request": ["fakeapp", "summary"], "timeout": "nan"}),
    ("read", {"kind": "app", "request": ["fakeapp", "summary"], "max_output": 10**12}),
    ("read", {"kind": "app", "request": ["nosuchapp", "summary", "--json"]}),
    ("admin", {"kind": "app", "request": ["fakeapp", "summary", "--json"]}),
    ("read", b"not json\n"),
    ("read", b"[1, 2]\n"),
    ("read", b"\xff\xfe\n"),
])
def test_child_refuses(server, level, request_):
    data_file = paths.server_layout("fakeproj", server).data / "added.txt"
    head, out, err = _child(server, level, request_)
    assert head["exit_code"] != 0
    assert out or err
    assert not data_file.exists()


def test_child_refusals_are_plain_errors(server):
    e = _child_error(_child(server, "read", {"kind": "app", "request": ["fakeapp", "add", "x", "--json"]}))
    assert e["exit_code"] == 4 and e["code"] == "not_allowed"
    e = _child_error(_child(server, "deploy", {"kind": "app", "request": ["fakeapp", "summary", "--json"]}))
    assert e["exit_code"] == 2 and e["code"] == "bad_request"


def test_child_through_the_real_command(server):
    """``mes gate child`` as systemd runs it: the connection is stdin and stdout. Here stdin is a
    pipe, not a unix socket from an SSH session, so the peer check refuses (finding 3)."""
    line = json.dumps({"kind": "app", "request": ["fakeapp", "summary", "--days", "2", "--json"]}).encode() + b"\n"
    done = subprocess.run([sys.executable, "-m", "mes.cli", "gate", "child", "--project", "fakeproj",
                           "--server-root", str(server), "--level", "read"],
                          input=line, capture_output=True, timeout=60)
    head, out, _ = gate.read_answer(io.BytesIO(done.stdout), gate.DEFAULT_MAX_OUTPUT)
    assert head["exit_code"] == 5 and json.loads(out)["error"]["code"] == "sandbox_peer"
    assert not (paths.server_layout("fakeproj", server).data / "added.txt").exists()


@pytest.mark.parametrize("uid, cgroup, expected", [
    (1000, "0::/user.slice/user-1000.slice/session-4.scope\n", None),
    (1000, "0::/system.slice/sshd.service\n", None),
    (1000, "0::/system.slice/ssh.service\n", None),
    (1000, "0::/system.slice/system-sshd.slice/sshd@3-10.0.0.1:22-10.0.0.2:5000.service\n", None),
    (1001, "0::/user.slice/user-1001.slice/session-4.scope\n", "another_account"),
    (1000, None, "no_cgroup"),
    (1000, "0::/mes.slice/mes-demo.slice/mes-demo.job.sync.service\n", "from_a_job"),
    (1000, "0::/system.slice/mes-demo.job.sync.service\n", "not_an_ssh_session"),
    (1000, "0::/user.slice/user-1000.slice/user@1000.service/app.slice/x.service\n", "not_an_ssh_session"),
    (1000, "0::/user.slice/user-2000.slice/session-4.scope\n", "not_an_ssh_session"),
    (1000, "0::/system.slice/mes-demo.webhook.service\n", "not_an_ssh_session"),
])
def test_peer_must_be_an_ssh_session_of_the_project(uid, cgroup, expected):
    """Finding 3: any process of mes-<p> (a job, the webhook receiver) could connect to the request
    sockets and run write requests. The child now checks who is on the other end."""
    assert gate.peer_problem(uid, cgroup, my_uid=1000) == expected


@pytest.mark.parametrize("answer", [
    b"",
    b"not json\n",
    b"[1]\n",
    b'{"exit_code": 0}\n',
    b'{"exit_code": 0, "stdout_bytes": -1, "stderr_bytes": 0}\n',
    b'{"exit_code": 0, "stdout_bytes": 5, "stderr_bytes": 0}\nabc',              # ends early
    b'{"exit_code": 0, "stdout_bytes": 0, "stderr_bytes": 99999999}\n',
    b'{"exit_code": 0, "stdout_bytes": "5", "stderr_bytes": 0}\nabcde',
])
def test_read_answer_refuses_broken_answers(answer):
    with pytest.raises(gate.TemporaryFailure):
        gate.read_answer(io.BytesIO(answer), 1000)
