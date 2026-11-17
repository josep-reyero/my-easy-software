"""Tests for mes.jobs (mes jobs ...): running jobs on this computer, the run log, and the two
helpers the server's system units call (record, allow-network).

The rendering of the system units themselves is tested in test_units.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from mes import cli, jobs, paths, units
from mes.errors import MesError, TemporaryFailure, UsageError

import projects as helpers


def loads(old_toml: str, tmp: Path | None = None):
    """The composed project of an old-form test project.toml."""
    import tempfile

    from mes import manifest

    with tempfile.TemporaryDirectory() as d:
        return manifest.load(helpers.write_old(Path(d), old_toml))

PROJECT_TOML = """
[project]
name = "demo"
title = "Demo project"
version = "0.1.0"

[apps.demo]
cli = "demo"

[jobs.sync]
run = "demo ok sync --days 3"
schedule = "*-*-* 06:00:00"
network = ["api.example.com"]

[jobs.build]
run = "demo touch build"
after = "sync"
writes = ["data"]

[jobs.report]
run = "demo touch report"
after = "build"

[jobs.export]
run = "demo ok export"
manual = true

[jobs.broken]
run = "demo fail"
manual = true

[jobs.slow]
run = "demo sleep"
manual = true
timeout = "1s"

[jobs.flaky]
run = "demo temp"
manual = true

[jobs.after-broken]
run = "demo touch after-broken"
after = "broken"
"""

FAKE_APP = """\
#!{python}
import json, os, sys, time
cmd = [a for a in sys.argv[1:] if a != "--json"]
KINDS = {{"ok": "read", "fail": "write", "sleep": "read", "temp": "write", "touch": "write",
         "reset": "admin", "describe": "read", "crash": "write"}}
if cmd[0] == "describe":
    print(json.dumps({{"app": os.environ.get("FAKE_APP_NAME", "demo-app"),
                      "commands": [{{"name": n, "kind": k}} for n, k in KINDS.items()]}}))
elif cmd[0] == "crash":
    print(json.dumps({{"error": {{"code": "not_found", "message": "The notes-per-day table does not exist yet.",
                                 "hint": "Run the rebuild job first."}}}}))
    sys.exit(3)
elif cmd[0] == "ok":
    print(json.dumps({{"argv": cmd, "allow": os.environ.get("MES_ALLOW"),
                      "job": os.environ.get("MES_JOB"), "cwd": os.getcwd(),
                      "data_dir": os.environ.get("MES_DATA_DIR"), "project": os.environ.get("MES_PROJECT")}}))
elif cmd[0] == "fail":
    print("the source refused the request", file=sys.stderr)
    sys.exit(3)
elif cmd[0] == "sleep":
    time.sleep(30)
elif cmd[0] == "temp":
    sys.exit(75)
elif cmd[0] == "touch":
    d = os.path.join(os.environ["MES_DATA_HOME"], "demo")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "order.txt"), "a") as f:
        f.write(cmd[1] + "\\n")
"""


def make_project(root: Path, *, toml: str = PROJECT_TOML, installed: bool = True) -> Path:
    helpers.write_old(root, toml)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n\n[project.scripts]\ndemo-app = "demo_app.cli:main"\n'
    )
    if installed:
        bin_dir = root / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        exe = bin_dir / "demo-app"
        exe.write_text(FAKE_APP.format(python=sys.executable))
        exe.chmod(0o755)
    return root


@pytest.fixture
def project_root(tmp_path):
    return make_project(tmp_path / "projects" / "demo")


def mes(capsys, *argv):
    code = cli.run(["--json", *argv])
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else None)


def order_file(mes_home) -> Path:
    return mes_home["data"] / "demo" / "order.txt"


def log_file(mes_home) -> Path:
    return mes_home["state"] / "demo" / "runs.jsonl"


# =========================================================================== list


def test_list_json(project_root, capsys):
    code, data = mes(capsys, "jobs", "list", "--project-dir", str(project_root))
    assert code == 0
    assert data["project"] == "demo"
    by = {j["id"]: j for j in data["jobs"]}
    assert by["demo-app.sync"]["trigger"] == "schedule" and by["demo-app.sync"]["app"] == "demo-app"
    assert by["demo-app.sync"]["argv"] == ["ok", "sync", "--days", "3"]
    assert by["demo-app.sync"]["network"] == ["api.example.com"]
    assert by["demo-app.sync"]["network_enforced"] == {"server": True, "this_computer": False}
    assert by["demo-app.export"]["network_enforced"] == {"server": True, "this_computer": True}
    assert by["demo-app.build"]["when"] == 'after "demo-app.sync" succeeds'
    assert by["demo-app.export"]["network"] is False
    assert by["demo-app.slow"]["timeout"] == "1s"
    assert data["warnings"] == []


def test_list_text_from_current_folder(project_root, capsys, monkeypatch):
    monkeypatch.chdir(project_root)
    assert cli.run(["jobs", "list"]) == 0
    out = capsys.readouterr().out
    assert "sync" in out and "on schedule: *-*-* 06:00:00" in out
    assert "only api.example.com (on a server)" in out


def test_list_without_jobs(tmp_path, capsys):
    root = make_project(tmp_path / "p", toml='[project]\nname = "demo"\n')
    assert cli.run(["jobs", "list", "--project-dir", str(root)]) == 0
    assert "has no jobs yet" in capsys.readouterr().out


def test_no_project_here(tmp_path, capsys, monkeypatch):
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.chdir(tmp_path / "empty")
    code, data = mes(capsys, "jobs", "list")
    assert code == 3
    assert data["error"]["code"] == "not_found"
    code, data = mes(capsys, "jobs", "list", "--project-dir", str(tmp_path / "empty"))
    assert code == 3 and "project.toml" in data["error"]["message"]


def test_group_without_action_is_usage_error(capsys):
    code, data = mes(capsys, "jobs")
    assert code == 2


# =========================================================================== run


def test_run_success_logs_and_sets_env(project_root, capsys, mes_home):
    code, data = mes(capsys, "jobs", "run", "export", "--project-dir", str(project_root))
    assert code == 0, data
    assert data["project"] == "demo"
    assert [r["job"] for r in data["runs"]] == ["demo-app.export"]
    out = data["runs"][0]["data"]  # the app runs with --json, so its data comes back parsed
    assert out == json.loads(data["runs"][0]["output"])
    assert out["argv"] == ["ok", "export"]
    assert out["allow"] == "write"
    assert out["job"] == "demo-app.export"
    assert Path(out["cwd"]).resolve() == project_root.resolve()
    # The project's folders, as the units and the gate give them: a plain program (no mes import)
    # then writes the same <data folder>/<project>.sqlite that mes db and easy-ui read.
    assert out["project"] == "demo" and out["data_dir"] == str(mes_home["data"] / "demo")

    records = jobs.read_log(log_file(mes_home))
    assert len(records) == 1
    rec = records[0]
    assert rec["job"] == "demo-app.export" and rec["exit_code"] == 0 and rec["result"] == "success"
    assert rec["project"] == "demo" and rec["app"] == "demo-app" and rec["via"] == "mes jobs run"
    assert rec["argv"] == [str(project_root.resolve() / ".venv" / "bin" / "demo-app"), "--json", "ok", "export"]
    assert rec["prev"] == jobs.GENESIS
    assert set(rec) >= {"time", "job", "argv", "exit_code", "duration_s", "prev"}
    assert oct(log_file(mes_home).stat().st_mode & 0o777) == oct(0o600)


def test_run_text_mode(project_root, capsys):
    assert cli.run(["jobs", "run", "export", "--project-dir", str(project_root)]) == 0
    out = capsys.readouterr().out
    assert 'Job "demo-app.export" finished successfully' in out


def test_run_follows_after_chain(project_root, capsys, mes_home):
    code, data = mes(capsys, "jobs", "run", "sync", "--project-dir", str(project_root))
    assert code == 0, data
    assert [r["job"] for r in data["runs"]] == ["demo-app.sync", "demo-app.build", "demo-app.report"]
    assert order_file(mes_home).read_text().split() == ["build", "report"]
    assert [r["job"] for r in jobs.read_log(log_file(mes_home))] == ["demo-app.sync", "demo-app.build", "demo-app.report"]


def test_run_no_chain(project_root, capsys, mes_home):
    code, data = mes(capsys, "jobs", "run", "sync", "--no-chain", "--project-dir", str(project_root))
    assert code == 0
    assert [r["job"] for r in data["runs"]] == ["demo-app.sync"]
    assert not order_file(mes_home).exists()


def test_run_failure_stops_chain(project_root, capsys, mes_home):
    code, data = mes(capsys, "jobs", "run", "broken", "--project-dir", str(project_root))
    assert code == 1
    assert data["error"]["code"] == "job_failed"
    assert 'The job "demo-app.broken" stopped with an error' in data["error"]["message"]
    assert "(demo-app.after-broken) were not started" in data["error"]["message"]
    assert "the source refused the request" in data["error"]["detail"]
    assert "uv run demo-app fail" in data["error"]["hint"] and "'detail'" not in data["error"]["hint"]
    assert not order_file(mes_home).exists()  # after-broken did not run
    rec = jobs.read_log(log_file(mes_home))[-1]
    assert rec["result"] == "exit-code" and rec["exit_code"] == 3


def test_run_failure_text_goes_to_stderr(project_root, capsys):
    assert cli.run(["jobs", "run", "broken", "--project-dir", str(project_root)]) == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert 'The job "demo-app.broken" stopped with an error' in cap.err and "What to do:" in cap.err
    assert "What the app said:\nthe source refused the request" in cap.err


def test_run_timeout_is_stopped(project_root, capsys, mes_home):
    code, data = mes(capsys, "jobs", "run", "slow", "--project-dir", str(project_root))
    assert code == 1
    assert data["error"]["code"] == "job_timeout"
    rec = jobs.read_log(log_file(mes_home))[-1]
    assert rec["result"] == "timeout" and rec["exit_code"] is None
    assert rec["duration_s"] < 15


def test_run_temporary_failure_exit_75(project_root, capsys, mes_home):
    code, data = mes(capsys, "jobs", "run", "flaky", "--project-dir", str(project_root))
    assert code == 75
    assert data["error"]["code"] == "job_temporary"
    # Finding H: recorded as skipped (a temporary problem), not as a failure.
    assert jobs.read_log(log_file(mes_home))[-1]["result"] == "skipped"


def test_run_unknown_job(project_root, capsys):
    code, data = mes(capsys, "jobs", "run", "nope", "--project-dir", str(project_root))
    assert code == 3
    assert "export" in data["error"]["hint"]


def test_run_needs_write_level(project_root, capsys, monkeypatch, mes_home):
    monkeypatch.setenv("MES_ALLOW", "read")
    code, data = mes(capsys, "jobs", "run", "export", "--project-dir", str(project_root))
    assert code == 4
    assert not log_file(mes_home).exists()
    code, _ = mes(capsys, "jobs", "list", "--project-dir", str(project_root))
    assert code == 0


def test_run_app_not_installed(tmp_path, capsys):
    root = make_project(tmp_path / "bare", installed=False)
    code, data = mes(capsys, "jobs", "run", "export", "--project-dir", str(root))
    assert code == 3
    assert data["error"]["code"] == "app_not_installed"
    assert "uv sync" in data["error"]["hint"]
    # Showing the jobs still works without the program.
    code, data = mes(capsys, "jobs", "list", "--project-dir", str(root))
    assert code == 0 and data["warnings"] == []


def test_run_twice_at_once_is_refused(project_root, capsys, mes_home):
    _, project = jobs.load_project(project_root)
    job = project.job("export")
    with jobs._job_lock(project, job):
        code, data = mes(capsys, "jobs", "run", "export", "--project-dir", str(project_root))
    assert code == 75
    assert data["error"]["code"] == "job_running"


def test_run_never_uses_a_shell(tmp_path, capsys, mes_home):
    toml = '[project]\nname = "demo"\n[jobs.x]\nrun = "demo touch \'a b;c\'"\nmanual = true\n'
    root = make_project(tmp_path / "shell", toml=toml)
    code, _ = mes(capsys, "jobs", "run", "x", "--project-dir", str(root))
    assert code == 0
    assert order_file(mes_home).read_text() == "a b;c\n"


def test_two_apps_in_one_project(tmp_path, capsys, mes_home):
    toml = ('[project]\nname = "demo"\n[apps.demo]\ncli = "demo"\n[apps.tool]\ncli = "tool"\n'
            '[jobs.a]\nrun = "demo ok a"\nmanual = true\n[jobs.b]\nrun = "tool ok b"\nafter = "a"\n')
    root = make_project(tmp_path / "two", toml=toml)
    tool = root / ".venv" / "bin" / "demo-tool"
    tool.write_text(FAKE_APP.format(python=sys.executable).replace(
        'os.environ.get("FAKE_APP_NAME", "demo-app")', '"demo-tool"'))
    tool.chmod(0o755)
    code, data = mes(capsys, "jobs", "run", "a", "--project-dir", str(root))
    assert code == 0, data
    # b belongs to another app and waits for a: after = "demo-app.a" in its app.toml.
    assert [(r["job"], r["data"]["argv"]) for r in data["runs"]] == [("demo-app.a", ["ok", "a"]),
                                                                      ("demo-tool.b", ["ok", "b"])]
    assert [r["app"] for r in jobs.read_log(log_file(mes_home))] == ["demo-app", "demo-tool"]


def test_run_line_starts_with_a_command_of_the_app(tmp_path, capsys):
    toml = '[project]\nname = "demo"\n[jobs.x]\nrun = "demo -c 1"\nmanual = true\n'
    root = make_project(tmp_path / "bad", toml=toml)
    code, data = mes(capsys, "jobs", "list", "--project-dir", str(root))
    assert code != 0 and "not the name of one of the app's commands" in data["error"]["message"]


def test_interpreter_is_never_an_app_program(tmp_path):
    root = make_project(tmp_path / "py", toml='[project]\nname = "demo"\n')
    (root / "apps" / "demo_app" / "app.toml").write_text('[app]\nname = "demo-app"\ncommand = "python3"\n')
    with pytest.raises(MesError):
        jobs.load_project(root)


# =========================================================================== run log


def test_log_chain_and_verify(tmp_path):
    log = tmp_path / "s" / "runs.jsonl"
    a = jobs.append_record(log, {"time": "t1", "job": "a"})
    b = jobs.append_record(log, {"time": "t2", "job": "b"})
    jobs.append_record(log, {"time": "t3", "job": "c"})
    lines = log.read_bytes().splitlines()
    assert a["prev"] == jobs.GENESIS
    assert b["prev"] == hashlib.sha256(lines[0]).hexdigest()
    assert json.loads(lines[2])["prev"] == hashlib.sha256(lines[1]).hexdigest()
    res = jobs.verify_log(log)
    assert res["ok"] and res["records"] == 3
    assert res["head"] == hashlib.sha256(lines[2]).hexdigest()


def test_verify_empty_log(tmp_path):
    res = jobs.verify_log(tmp_path / "missing.jsonl")
    assert res["ok"] and res["records"] == 0


def test_verify_detects_edit(tmp_path):
    log = tmp_path / "runs.jsonl"
    for i in range(3):
        jobs.append_record(log, {"time": f"t{i}", "job": "a", "exit_code": 1})
    lines = log.read_text().splitlines()
    lines[0] = lines[0].replace('"exit_code":1', '"exit_code":0')
    log.write_text("\n".join(lines) + "\n")
    with pytest.raises(MesError) as e:
        jobs.verify_log(log)
    assert e.value.code == "run_log_broken"
    assert "line 1 or an earlier line was changed" in e.value.message


def test_verify_detects_removed_line(tmp_path):
    log = tmp_path / "runs.jsonl"
    for i in range(3):
        jobs.append_record(log, {"time": f"t{i}", "job": "a"})
    lines = log.read_text().splitlines()
    log.write_text(lines[0] + "\n" + lines[2] + "\n")
    with pytest.raises(MesError) as e:
        jobs.verify_log(log)
    assert "a line right after it was removed" in e.value.message


def test_verify_detects_garbage_line(tmp_path):
    log = tmp_path / "runs.jsonl"
    jobs.append_record(log, {"time": "t", "job": "a"})
    with log.open("a") as f:
        f.write("not json")  # half-written line, no newline
    jobs.append_record(log, {"time": "t2", "job": "a"})
    with pytest.raises(MesError) as e:
        jobs.verify_log(log)
    assert "line 2" in e.value.message


def test_log_command(project_root, capsys, mes_home):
    mes(capsys, "jobs", "run", "export", "--project-dir", str(project_root))
    mes(capsys, "jobs", "run", "sync", "--project-dir", str(project_root))
    code, data = mes(capsys, "jobs", "log", "--project-dir", str(project_root))
    assert code == 0
    assert [r["job"] for r in data["runs"]] == ["demo-app.export", "demo-app.sync", "demo-app.build", "demo-app.report"]
    code, data = mes(capsys, "jobs", "log", "--job", "build", "--limit", "5", "--project-dir", str(project_root))
    assert [r["job"] for r in data["runs"]] == ["demo-app.build"]
    code, data = mes(capsys, "jobs", "log", "--verify", "--project-dir", str(project_root))
    assert code == 0 and data["ok"] and data["records"] == 4
    code, data = mes(capsys, "jobs", "log", "--limit", "0", "--project-dir", str(project_root))
    assert code == 2

    text = log_file(mes_home).read_text().replace('"job":"demo-app.export"', '"job":"demo-app.exporT"')
    log_file(mes_home).write_text(text)
    code, data = mes(capsys, "jobs", "log", "--verify", "--project-dir", str(project_root))
    assert code == 1 and data["error"]["code"] == "run_log_broken"


def test_log_of_the_server(tmp_path, capsys):
    layout = paths.server_layout("demo", tmp_path / "srv")
    jobs.append_record(layout.run_log, {"time": "t", "project": "demo", "job": "demo-app.sync", "via": "systemd"})
    code, data = mes(capsys, "jobs", "log", "--project", "demo", "--server-root", str(tmp_path / "srv"))
    assert code == 0 and [r["job"] for r in data["runs"]] == ["demo-app.sync"] and data["project"] == "demo"
    code, data = mes(capsys, "jobs", "log", "--verify", "--project", "demo", "--server-root", str(tmp_path / "srv"))
    assert code == 0 and data["records"] == 1
    code, data = mes(capsys, "jobs", "log", "--server-root", str(tmp_path / "srv"))
    assert code == 2
    code, data = mes(capsys, "jobs", "log", "--project", "../etc", "--server-root", str(tmp_path / "srv"))
    assert code == 2


def test_status_shows_last_run(project_root, capsys):
    mes(capsys, "jobs", "run", "export", "--project-dir", str(project_root))
    code, data = mes(capsys, "jobs", "status", "--project-dir", str(project_root))
    assert code == 0
    by = {r["job"]: r for r in data["jobs"]}
    assert by["demo-app.export"]["last_result"] == "success" and by["demo-app.export"]["last_exit_code"] == 0
    assert by["demo-app.sync"]["last_run"] is None
    assert "mes server check" in data["note"]


# =========================================================================== record (root, after each run)

INVOCATION = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def server_root(tmp_path):
    root = tmp_path / "srv"
    layout = paths.server_layout("demo", root)
    layout.log.mkdir(parents=True, mode=0o750)
    return root


def _systemd_env(monkeypatch, *, status="2", result="exit-code", code="1", state="deactivating"):
    """What systemd shows for the unit while its ExecStopPost runs. The record step gets an empty
    environment (env -i), so everything comes from these properties."""
    monkeypatch.setattr(jobs, "_is_linux", lambda: True)
    for k in ("INVOCATION_ID", "SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS"):
        monkeypatch.delenv(k, raising=False)
    asked = []

    def show(unit, *props):
        asked.append(unit)
        return {"InvocationID": INVOCATION, "ExecMainStartTimestampMonotonic": "1000000",
                "ExecMainExitTimestampMonotonic": "3500000", "Result": result, "ExecMainCode": code,
                "ExecMainStatus": status, "ActiveState": state,
                "ExecStart": '{ path=/x/.venv/bin/demo ; argv[]=/x/.venv/bin/demo --json sync ; ignore_errors=no }'}

    monkeypatch.setattr(jobs, "_show", show)
    return asked


def test_record_from_systemd(server_root, capsys, monkeypatch):
    asked = _systemd_env(monkeypatch)
    code, data = mes(capsys, "jobs", "record", "--project", "demo", "--server-root", str(server_root), "demo-app.sync")
    assert code == 0, data
    assert asked == ["mes-demo.job.demo-app.sync.service"]
    log = paths.server_layout("demo", server_root).run_log
    rec = jobs.read_log(log)[-1]
    assert rec["via"] == "systemd" and rec["exit_code"] == 2 and rec["result"] == "exit-code"
    assert rec["duration_s"] == 2.5 and rec["unit"] == "mes-demo.job.demo-app.sync.service"
    assert rec["project"] == "demo" and rec["job"] == "demo-app.sync" and rec["prev"] == jobs.GENESIS
    assert rec["invocation"] == INVOCATION and rec["exec"] == "/x/.venv/bin/demo --json sync"
    assert "argv" not in rec  # root reads nothing from the project's files
    code, data = mes(capsys, "jobs", "log", "--verify", "--project", "demo", "--server-root", str(server_root))
    assert code == 0 and data["records"] == 1 and data["anchored"] is True
    # The same run cannot be recorded twice.
    code, data = mes(capsys, "jobs", "record", "--project", "demo", "--server-root", str(server_root), "demo-app.sync")
    assert code == 5 and data["error"]["code"] == "already_recorded"


def test_record_counts_exit_75_and_a_skipped_condition_as_skipped(server_root, capsys, monkeypatch):
    """Finding H: with SuccessExitStatus=75 systemd says "success" for exit 75; the log says skipped."""
    _systemd_env(monkeypatch, status="75", result="success", code="1")
    code, data = mes(capsys, "jobs", "record", "--project", "demo", "--server-root", str(server_root), "demo-app.sync")
    assert code == 0, data
    log = paths.server_layout("demo", server_root).run_log
    assert jobs.read_log(log)[-1]["result"] == "skipped"


def test_should_run_follows_the_job_that_finished_last():
    def show_with(states):
        return lambda unit, *props: states[unit]

    ok = {"Result": "success", "ExecMainCode": "1", "ExecMainStatus": "0"}
    temp = {"Result": "success", "ExecMainCode": "1", "ExecMainStatus": "75"}
    unit = units.job_unit
    states = {unit("demo", "app-a.one"): {**ok, "InactiveEnterTimestampMonotonic": "100"},
              unit("demo", "app-a.two"): {**temp, "InactiveEnterTimestampMonotonic": "200"}}
    with pytest.raises(MesError) as e:  # a.two finished last, with exit code 75
        jobs.should_run("demo", ["app-a.one", "app-a.two"], server_root="/var/lib/mes", show=show_with(states))
    assert e.value.code == "upstream_skipped" and e.value.exit_code == 1  # 1: systemd skips, not fails
    states[unit("demo", "app-a.one")]["InactiveEnterTimestampMonotonic"] = "300"
    assert jobs.should_run("demo", ["app-a.one", "app-a.two"], server_root="/var/lib/mes",
                           show=show_with(states)) == {"run": True, "after": "app-a.one"}
    states[unit("demo", "app-a.one")]["Result"] = "exec-condition"  # it was skipped itself
    with pytest.raises(MesError):
        jobs.should_run("demo", ["app-a.one", "app-a.two"], server_root="/var/lib/mes", show=show_with(states))
    with pytest.raises(jobs.ShouldRunBroken):
        jobs.should_run("demo", ["../x"], server_root="/var/lib/mes", show=show_with(states))


def test_should_run_exits_0_to_run_1_to_skip_and_255_when_it_cannot_tell(capsys, monkeypatch):
    """In ExecCondition= systemd skips the run on exit 1 to 254, so a broken check (exit 1 to 5)
    quietly skipped every dashboard refresh. Now: 0 run, 1 upstream did not succeed, 255 broken
    (systemd fails the unit), with a message of its own."""
    ok = {"Result": "success", "ExecMainCode": "1", "ExecMainStatus": "0", "InactiveEnterTimestampMonotonic": "5"}
    state = {"props": ok}
    monkeypatch.setattr(jobs, "_is_linux", lambda: True)
    monkeypatch.setattr(jobs, "_show", lambda unit, *props: state["props"])
    argv = ["jobs", "should-run", "--project", "demo", "--server-root", "/var/lib/mes", "--", "demo-app.sync"]
    assert mes(capsys, *argv) == (0, {"run": True, "after": "demo-app.sync"})
    state["props"] = {**ok, "Result": "exit-code"}
    code, data = mes(capsys, *argv)
    assert code == 1 and data["error"]["code"] == "upstream_skipped"
    broken = []
    state["props"] = {}  # systemctl could not be asked
    broken.append(mes(capsys, *argv))
    state["props"] = ok
    monkeypatch.setattr(jobs, "_is_linux", lambda: False)
    broken.append(mes(capsys, *argv))  # not a systemd server (said "only records a run" before)
    monkeypatch.setattr(jobs, "_is_linux", lambda: True)

    def explode(*a):
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs, "_show", explode)
    broken.append(mes(capsys, *argv))  # an error inside mes
    broken.append(mes(capsys, "jobs", "should-run", "--project", "demo", "--server-root", "/var/lib/mes",
                      "--bogus", "--", "demo-app.sync"))  # a command line it does not understand
    broken.append(mes(capsys, "jobs", "should-run", "--project", "demo", "--server-root", "/var/lib/mes",
                      "--", "../x"))
    for code, data in broken:
        assert code == 255 == jobs.SHOULD_RUN_BROKEN, (code, data)
        assert data["error"]["code"] == "should_run_broken"
        assert data["error"]["message"].startswith("mes could not tell whether the job this one waits for succeeded")
        assert "run log" not in data["error"]["message"]


def test_allow_network_for_the_selftest_allows_only_its_test_address(tmp_path):
    """Finding G: the probe gets exactly one documentation address, with no DNS lookup."""
    calls = []
    written = {}
    run = FakeRun()
    real = jobs._write_hosts_file
    jobs._write_hosts_file = lambda path, unit, found, **kw: written.update(path=path, found=found, **kw)
    try:
        res = jobs.allow_network("mes-demo.selftest-net.service", [units.SELFTEST_HOST], selftest=True,
                                 hosts_file=units.selftest_hosts_file("demo"),
                                 resolver=lambda *a: calls.append(a), run=run)
    finally:
        jobs._write_hosts_file = real
    assert written["found"] == {units.SELFTEST_HOST: [units.SELFTEST_ADDRESS]}
    assert calls == []  # the resolver was never asked
    assert run.calls[0][-1] == f"IPAddressAllow={units.SELFTEST_ADDRESS}", run.calls
    assert units.SELFTEST_ADDRESS in json.dumps(res)
    for unit, hosts in (("mes-demo.job.demo-app.sync.service", [units.SELFTEST_HOST]),
                        ("mes-demo.selftest-net.service", ["example.com"])):
        with pytest.raises(UsageError):
            jobs.allow_network(unit, hosts, selftest=True, run=lambda argv, **kw: None)


def test_record_ignores_a_project_writable_environment(server_root, capsys, monkeypatch):
    """Finding 4: root used to read SERVICE_RESULT and friends from its environment, which came
    with the job's settings. Now only systemd's properties count."""
    _systemd_env(monkeypatch, status="0", result="success")
    monkeypatch.setenv("SERVICE_RESULT", "exit-code")
    monkeypatch.setenv("EXIT_STATUS", "99")
    code, data = mes(capsys, "jobs", "record", "--project", "demo", "--server-root", str(server_root), "demo-app.sync")
    assert code == 0 and data["result"] == "success" and data["exit_code"] == 0


def test_cut_or_emptied_server_log_is_caught_by_its_anchor(server_root, capsys, monkeypatch):
    """Audit F5: the unkeyed chain still verified after the log was cut or emptied."""
    _systemd_env(monkeypatch)
    log = paths.server_layout("demo", server_root).run_log
    for inv in ("a" * 32, "b" * 32):
        monkeypatch.setattr(jobs, "_show", lambda unit, *p, inv=inv: {
            "InvocationID": inv, "Result": "success", "ExecMainCode": "1", "ExecMainStatus": "0",
            "ActiveState": "deactivating"})
        assert mes(capsys, "jobs", "record", "--project", "demo", "--server-root", str(server_root), "demo-app.sync")[0] == 0
    lines = log.read_bytes().splitlines(keepends=True)
    log.write_bytes(lines[0])  # cut the newest record off: the chain alone still verifies
    assert jobs.verify_log(log)["records"] == 1
    code, data = mes(capsys, "jobs", "log", "--verify", "--project", "demo", "--server-root", str(server_root))
    assert code == 1 and data["error"]["code"] == "run_log_broken"
    log.write_bytes(b"")
    code, data = mes(capsys, "jobs", "log", "--verify", "--project", "demo", "--server-root", str(server_root))
    assert code == 1


def test_record_killed_job_has_no_exit_code(server_root, capsys, monkeypatch):
    _systemd_env(monkeypatch, result="timeout", code="2", status="15")
    code, data = mes(capsys, "jobs", "record", "--project", "demo", "--server-root", str(server_root), "demo-app.sync")
    assert code == 0 and data["exit_code"] is None and data["result"] == "timeout"


def test_forged_systemd_record_is_refused(server_root, capsys, monkeypatch):
    argv = ("jobs", "record", "--project", "demo", "--server-root", str(server_root), "demo-app.sync")
    monkeypatch.setattr(jobs, "_is_linux", lambda: False)
    code, data = mes(capsys, *argv)
    assert code == 5 and data["error"]["code"] == "not_from_systemd"
    _systemd_env(monkeypatch, state="inactive")  # nobody is just finishing a run
    code, data = mes(capsys, *argv)
    assert code == 5
    monkeypatch.setattr(jobs, "_show", lambda unit, *p: {"InvocationID": "not-hex", "ActiveState": "deactivating"})
    code, data = mes(capsys, *argv)
    assert code == 5
    assert not paths.server_layout("demo", server_root).run_log.exists()


@pytest.mark.parametrize("project,job", [
    ("demo", "x\nExecStart=/bin/sh"), ("demo", "../x"), ("demo", "Sync"),
    ("../etc", "sync"), ("demo\n", "sync"), ("Demo", "sync"),
])
def test_record_refuses_hostile_names(server_root, capsys, monkeypatch, project, job):
    _systemd_env(monkeypatch)
    code, data = mes(capsys, "jobs", "record", "--project", project, "--server-root", str(server_root), job)
    assert code in (2, 5)
    assert not paths.server_layout("demo", server_root).run_log.exists()


def test_record_refuses_missing_or_linked_log_folder(tmp_path, capsys, monkeypatch):
    _systemd_env(monkeypatch)
    root = tmp_path / "srv"
    argv = ("jobs", "record", "--project", "demo", "--server-root", str(root), "demo-app.sync")
    code, data = mes(capsys, *argv)  # the server was never set up: a plain refusal, not a crash
    assert code == 5 and data["error"]["code"] == "unsafe_folder"
    (root / "demo").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "demo" / "log").symlink_to(elsewhere)
    code, data = mes(capsys, *argv)
    assert code == 5 and data["error"]["code"] == "unsafe_folder"
    assert list(elsewhere.iterdir()) == []


def test_record_refuses_log_folder_of_another_account(server_root, capsys, monkeypatch):
    _systemd_env(monkeypatch)
    real = os.getuid()
    monkeypatch.setattr(jobs.os, "getuid", lambda: real + 4242)
    code, data = mes(capsys, "jobs", "record", "--project", "demo", "--server-root", str(server_root), "demo-app.sync")
    assert code == 5 and data["error"]["code"] == "unsafe_folder"


def test_record_needs_write(server_root, capsys, monkeypatch):
    _systemd_env(monkeypatch)
    monkeypatch.setenv("MES_ALLOW", "read")
    code, _ = mes(capsys, "jobs", "record", "--project", "demo", "--server-root", str(server_root), "demo-app.sync")
    assert code == 4


# =========================================================================== allow-network (root, before a run)


def _resolver(table):
    def look(host, port, family, kind):
        if host not in table:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, kind, 6, "", (a, port)) for a in table[host]]
    return look


class FakeRun:
    def __init__(self, code=0):
        self.code = code
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.code, "", "Unit not loaded." if self.code else "")


def test_allow_network_sets_only_the_hosts_and_no_nameservers(tmp_path):
    """Finding 9: name servers were opened on every port; resolved addresses on this computer or the
    local network were allowed. Now: internet addresses only, written to the job's own hosts file."""
    run = FakeRun()
    hosts_file = "/run/mes-demo-net/demo-app.sync/hosts"
    written = {}
    real_write = jobs._write_hosts_file

    def capture(path, unit, found, **_):
        written.update(path=path, found=found)

    jobs._write_hosts_file = capture
    try:
        res = jobs.allow_network(
            "mes-demo.job.demo-app.sync.service", ["api.example.com", "cdn.example.com"],
            resolver=_resolver({"api.example.com": ["93.184.216.34", "93.184.216.34", "2606:2800:220:1::1", "0.0.0.0",
                                                    "224.0.0.1", "127.0.0.1", "10.1.2.3", "169.254.169.254",
                                                    "::ffff:127.0.0.1"],
                                "cdn.example.com": ["151.101.1.1"]}),
            hosts_file=hosts_file, run=run,
        )
    finally:
        jobs._write_hosts_file = real_write
    assert run.calls == [["systemctl", "set-property", "--runtime", "mes-demo.job.demo-app.sync.service", "IPAddressAllow=",
                          "IPAddressAllow=93.184.216.34 2606:2800:220:1::1 151.101.1.1"]]
    assert res["allowed"] == ["93.184.216.34", "2606:2800:220:1::1", "151.101.1.1"]
    assert written["found"] == {"api.example.com": ["93.184.216.34", "2606:2800:220:1::1"],
                                "cdn.example.com": ["151.101.1.1"]}


def test_local_network_hosts_may_be_private_but_never_this_computer():
    look = _resolver({"nas.home.arpa": ["192.168.1.20", "127.0.0.1", "169.254.1.1", "fd00::5"],
                      "api.example.com": ["10.0.0.9", "93.184.216.34"]})
    found = jobs.resolve_hosts(["nas.home.arpa", "api.example.com"], resolver=look, local=["nas.home.arpa"])
    assert found == {"nas.home.arpa": ["192.168.1.20", "fd00::5"], "api.example.com": ["93.184.216.34"]}


def test_hosts_file_is_only_the_jobs_own(tmp_path, monkeypatch):
    with pytest.raises(UsageError):
        jobs._write_hosts_file("/etc/hosts", "mes-demo.job.demo-app.sync.service", {})
    with pytest.raises(UsageError):
        jobs._write_hosts_file("/run/mes-other-net/demo-app.sync/hosts", "mes-demo.job.demo-app.sync.service", {})
    with pytest.raises(UsageError):
        jobs._write_hosts_file("/run/mes-demo-net/../x/hosts", "mes-demo.job.demo-app.sync.service", {})
    import re

    target = tmp_path / "mes-demo-net" / "demo-app.sync" / "hosts"
    monkeypatch.setattr(jobs, "_HOSTS_FILE_RE", re.compile(
        re.escape(str(tmp_path)) + r"/mes-(?P<project>[a-z]+)-net/(?P<app>[a-z-]+)\.(?P<job>[a-z]+)/hosts\Z"))
    jobs._write_hosts_file(str(target), "mes-demo.job.demo-app.sync.service", {"api.example.com": ["93.184.216.34"]})
    lines = target.read_text().splitlines()
    assert lines[1:] == ["127.0.0.1 localhost", "::1 localhost", "93.184.216.34 api.example.com"]
    assert oct(target.stat().st_mode & 0o777) == "0o644"


def test_allow_network_with_some_hosts_unknown(tmp_path):
    run = FakeRun()
    res = jobs.allow_network("mes-demo.job.demo-app.sync.service", ["api.example.com", "gone.example.com"],
                             resolver=_resolver({"api.example.com": ["93.184.216.34"]}), run=run)
    assert res["allowed"] == ["93.184.216.34"] and res["unreachable"] == ["gone.example.com"]


def test_allow_network_dns_failure_is_temporary(tmp_path):
    run = FakeRun()
    with pytest.raises(TemporaryFailure) as e:
        jobs.allow_network("mes-demo.job.demo-app.sync.service", ["api.example.com"], resolver=_resolver({}), run=run)
    assert e.value.code == "dns_failed" and e.value.exit_code == 75
    assert run.calls == []  # nothing was allowed
    with pytest.raises(TemporaryFailure):  # only loopback: nothing it may reach
        jobs.allow_network("mes-demo.job.demo-app.sync.service", ["api.example.com"],
                           resolver=_resolver({"api.example.com": ["127.0.0.1"]}), run=run)


def test_allow_network_systemd_failure(tmp_path):
    with pytest.raises(MesError) as e:
        jobs.allow_network("mes-demo.job.demo-app.sync.service", ["api.example.com"],
                           resolver=_resolver({"api.example.com": ["93.184.216.34"]}), run=FakeRun(1))
    assert e.value.code == "systemd_failed" and "Unit not loaded." in e.value.detail


@pytest.mark.parametrize("unit", [
    "mes-demo.net.demo-app.sync.service", "sshd.service", "mes-demo.job.demo-app.sync.service\nx", "mes-demo.job.demo-app.sync.timer",
    "mes-demo.job.demo-app.sync.service; reboot", "-mes-demo.job.demo-app.sync.service", "",
])
def test_allow_network_only_for_job_units(unit, tmp_path):
    run = FakeRun()
    with pytest.raises(UsageError):
        jobs.allow_network(unit, ["api.example.com"], resolver=_resolver({"api.example.com": ["93.184.216.34"]}),
                           run=run)
    assert run.calls == []


@pytest.mark.parametrize("host", ["api.example.com\nIPAddressAllow=any", "evil host", "1.2.3.4/0", "*", "-x", "",
                                  "0x7f.0.0.1", "0x7f.1", "2130706433", "127.1"])
def test_allow_network_refuses_hostile_hosts(host, tmp_path):
    run = FakeRun()
    with pytest.raises(MesError) as e:
        jobs.allow_network("mes-demo.job.demo-app.sync.service", [host], resolver=_resolver({host: ["93.184.216.34"]}), run=run)
    assert e.value.code == "unsafe_unit_value"
    assert run.calls == []


def test_resolver_answers_are_validated():
    def liar(host, port, family, kind):
        return [(socket.AF_INET, kind, 6, "", ("any",)), (socket.AF_INET, kind, 6, "", ("10.0.0.1 0.0.0.0/0",)),
                (socket.AF_INET, kind, 6, "", ()), (socket.AF_INET, kind, 6, "", ("93.184.216.34", 443))]
    assert jobs.resolve_hosts(["api.example.com"], resolver=liar) == {"api.example.com": ["93.184.216.34"]}


def test_allow_network_command_needs_admin_and_hosts(capsys, monkeypatch):
    monkeypatch.setenv("MES_ALLOW", "write")
    code, _ = mes(capsys, "jobs", "allow-network", "--unit", "mes-demo.job.demo-app.sync.service", "--", "api.example.com")
    assert code == 4
    monkeypatch.setenv("MES_ALLOW", "admin")
    code, _ = mes(capsys, "jobs", "allow-network", "--unit", "mes-demo.job.demo-app.sync.service")
    assert code == 2


# =========================================================================== checks of the jobs themselves


def test_job_run_must_be_an_app_command(tmp_path, capsys):
    toml = PROJECT_TOML + ('\n[jobs.bad]\nrun = "demo nosuchcmd --x"\nmanual = true\n\n'
                           '[jobs.boss]\nrun = "demo reset"\nmanual = true\n')
    root = make_project(tmp_path / "x", toml=toml)
    code, data = mes(capsys, "jobs", "run", "bad", "--project-dir", str(root))
    assert code == 1 and data["error"]["code"] == "job_invalid"
    assert 'Job "demo-app.bad" runs "nosuchcmd" of demo-app, but demo-app has no such command' in data["error"]["message"]
    assert "Nothing was run." in data["error"]["message"]
    code, data = mes(capsys, "jobs", "run", "boss", "--project-dir", str(root))
    assert code == 1 and "admin command" in data["error"]["message"]
    code, data = mes(capsys, "jobs", "list", "--project-dir", str(root))
    assert code == 0 and any("nosuchcmd" in w for w in data["warnings"])


def test_bad_job_in_chain_stops_before_anything_runs(tmp_path, capsys, mes_home):
    toml = PROJECT_TOML + '\n[jobs.late]\nrun = "demo nosuchcmd"\nafter = "report"\n'
    root = make_project(tmp_path / "x", toml=toml)
    code, data = mes(capsys, "jobs", "run", "sync", "--project-dir", str(root))
    assert code == 1 and data["error"]["code"] == "job_invalid"
    assert not log_file(mes_home).exists() and not order_file(mes_home).exists()


def test_job_problems_unit():
    project = loads(PROJECT_TOML)
    assert jobs.job_problems(project, {"demo-app": {"ok": "read", "fail": "write", "sleep": "read", "temp": "write",
                                                "touch": "write"}}) == []
    problems = jobs.job_problems(project, {"demo-app": {"ok": "admin"}}, [project.job("export")])
    assert problems and "admin command" in problems[0]
    problems = jobs.job_problems(project, {}, [project.job("export")])
    assert "has no such command" in problems[0] and "(its commands are: none)" in problems[0]


def test_job_program_of_another_app_is_refused(project_root, capsys, monkeypatch):
    exe = project_root / ".venv" / "bin" / "demo-app"
    exe.write_text(exe.read_text().replace('os.environ.get("FAKE_APP_NAME", "demo-app")', '"other"'))
    code, data = mes(capsys, "jobs", "run", "export", "--project-dir", str(project_root))
    assert code == 5 and data["error"]["code"] == "app_mismatch"


def test_a_job_on_this_computer_gets_no_secrets_from_the_shell(project_root, capsys, monkeypatch):
    """Audit F12: 'mes jobs run' passed the whole environment (SSH agent, hosting tokens) to the job."""
    exe = project_root / ".venv" / "bin" / "demo-app"
    exe.write_text(exe.read_text().replace('os.environ.get("FAKE_APP_NAME", "demo-app")',
                                           '"demo-app" if not (os.environ.get("SSH_AUTH_SOCK") or '
                                           'os.environ.get("CLOUDFLARE_API_TOKEN")) else "leaked"'))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "secret")
    code, data = mes(capsys, "jobs", "run", "export", "--project-dir", str(project_root))
    assert code == 0, data


def test_jobs_list_can_skip_running_project_code(project_root, capsys, monkeypatch):
    exe = project_root / ".venv" / "bin" / "demo-app"
    exe.write_text(exe.read_text().replace("import json, os, sys, time\n", "import json, os, sys, time\n"
                                           "open(%r, 'w').close()\n" % str(project_root / "ran")))
    code, data = mes(capsys, "jobs", "list", "--no-describe", "--project-dir", str(project_root))
    assert code == 0 and not (project_root / "ran").exists()
    assert any("--no-describe" in w for w in data["warnings"])
    code, data = mes(capsys, "jobs", "list", "--project-dir", str(project_root))
    assert code == 0 and (project_root / "ran").exists()  # the check itself does run the app


def test_failed_job_text_shows_what_the_app_said(tmp_path, capsys):
    toml = PROJECT_TOML + '\n[jobs.stats]\nrun = "demo crash"\nmanual = true\n'
    root = make_project(tmp_path / "x", toml=toml)
    assert cli.run(["jobs", "run", "stats", "--no-chain", "--project-dir", str(root)]) == 1
    err = capsys.readouterr().err
    assert 'The job "demo-app.stats" stopped with an error' in err
    assert "What the app said:\nThe notes-per-day table does not exist yet." in err
    assert "Its advice: Run the rebuild job first." in err
    assert "uv run demo-app crash" in err and "'detail'" not in err
    assert "were not started" not in err  # nothing waits for it


def test_crash_inside_mes_still_logs_the_run(project_root, capsys, mes_home, monkeypatch):
    real = subprocess.Popen

    class Exploding(real):
        def communicate(self, *a, **k):
            raise OverflowError("timeout value is too large")

    monkeypatch.setattr(jobs, "app_commands", lambda *a: {"ok": "read"})
    monkeypatch.setattr(jobs.subprocess, "Popen", Exploding)
    code, data = mes(capsys, "jobs", "run", "export", "--project-dir", str(project_root))
    assert code == 1
    rec = jobs.read_log(log_file(mes_home))[-1]
    assert rec["job"] == "demo-app.export" and rec["result"] == "interrupted"


def test_plain_words_for_limits_network_and_log(project_root, capsys):
    project = loads(PROJECT_TOML)
    sync = jobs.describe_job(project.job("sync"))
    assert "256 MB" in sync and "15 minutes" in sync
    assert "may reach only api.example.com on a server" in sync and "full internet access" in sync
    slow = jobs.describe_job(project.job("slow"))
    assert "1 second." in slow and "has no internet access" in slow

    assert cli.run(["jobs", "list", "--project-dir", str(project_root)]) == 0
    out = capsys.readouterr().out
    assert "only api.example.com (on a server)" in out

    mes(capsys, "jobs", "run", "broken", "--project-dir", str(project_root))
    mes(capsys, "jobs", "run", "export", "--project-dir", str(project_root))
    assert cli.run(["jobs", "log", "--project-dir", str(project_root)]) == 0
    out = capsys.readouterr().out
    assert "failed" in out and "worked" in out and "by hand" in out and "exit-code" not in out
    assert cli.run(["jobs", "log", "--verify", "--project-dir", str(project_root)]) == 0
    out = capsys.readouterr().out
    assert "intact" in out and "head" not in out
    assert cli.run(["jobs", "status", "--project-dir", str(project_root)]) == 0
    out = capsys.readouterr().out
    assert "on this computer" in out and "mes server plan" in out
