"""Tests for mes.selftest and 'mes server analyze'. The real proof needs the server (the claims
are enforced by systemd and the kernel); here the checklist logic is tested: a claim that holds
is reported as passed, one that does not as failed, and one that cannot be checked as skipped,
never as passed."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from mes import paths, selftest, server, units

import projects as helpers

PROJECT_TOML = """
[project]
name = "demo"
apps = ["demo_app"]
"""

APP_TOML = """
[app]
name = "demo-app"
command = "demo-app"

[jobs.sync]
run = "sync"
manual = true
network = ["api.example.com"]
"""


@pytest.fixture
def layout(tmp_path):
    lay = paths.server_layout("demo", tmp_path / "srv")
    for d in (lay.code, lay.data, lay.state, lay.gate, lay.etc):
        d.mkdir(parents=True)
    return lay


def _server_toml(layout, *, builds):
    (layout.etc / "server.toml").write_text(f'[server]\nproject = "demo"\nallow_builds = {str(builds).lower()}\n')
    os.chmod(layout.etc / "server.toml", 0o644)


def _by_id(result):
    return {i["id"]: i for i in result["items"]}


def test_probe_reports_what_is_reachable_as_failed(layout):
    """Outside a sandbox everything is reachable: every isolation claim must come out failed (or
    skipped), never passed."""
    (layout.root / "other").mkdir()
    layout.auth.mkdir()
    layout.webhook_key.write_text("x")
    items = _by_id(selftest.probe(layout, "job"))
    for claim in ("job-projects", "job-auth", "job-gate", "job-webhook-key", "job-code"):
        assert items[claim]["status"] == "todo", claim
    assert items["job-deploy-socket"]["status"] == "done"  # no socket here at all
    with pytest.raises(ValueError):
        selftest.probe(layout, "admin")


def test_install_probe_expects_only_the_new_release_to_be_writable(layout):
    """Finding A: the install step may write the release code/installing points at and uv's
    cache, never code/ itself or the other releases."""
    items = _by_id(selftest.probe(layout, "deploy"))
    assert items["deploy-code"]["status"] == "todo"  # outside a sandbox code/ is writable: not a pass
    assert items["deploy-other-releases"]["status"] == "skipped"  # no releases/ yet: not a pass
    assert items["deploy-new-release"]["status"] == "todo"  # no code/installing here
    target = layout.releases / ".selftest-install"
    target.mkdir(parents=True)
    os.symlink("releases/.selftest-install", layout.installing)
    layout.uv_cache.mkdir()
    assert _by_id(selftest.probe(layout, "deploy"))["deploy-uv-cache"]["status"] == "skipped"  # no settings here
    _server_toml(layout, builds=False)
    items = _by_id(selftest.probe(layout, "deploy"))
    assert items["deploy-new-release"]["status"] == "done" and items["deploy-uv-cache"]["status"] == "done"
    # With server builds the shared cache must NOT be writable (build code gets the release's own).
    _server_toml(layout, builds=True)
    assert _by_id(selftest.probe(layout, "deploy"))["deploy-uv-cache"]["status"] == "todo"
    assert items["deploy-other-releases"]["status"] == "todo"  # writable outside a sandbox
    assert items["deploy-no-data"]["status"] == "todo"  # data/ is visible here
    assert "deploy-syscalls" in items and "deploy-loopback" in items and "deploy-network" not in items


def test_net_probe_never_passes_the_filter_when_it_cannot_tell(layout, monkeypatch):
    """Finding G: on a computer without the filter, unlisted addresses either connect (fail) or
    fail for another reason (todo with a reason), never pass."""
    def refuse(host, port):
        raise OSError(101, "Network is unreachable")  # ENETUNREACH for everything: cannot tell

    monkeypatch.setattr(selftest, "_connect", refuse)
    items = _by_id(selftest.probe(layout, "net"))
    assert items["net-filter"]["status"] == "todo" and "cannot tell" in items["net-filter"]["why"]
    assert items["net-names"]["status"] == "todo"  # this computer's /etc/hosts is not the probe's own

    def filtered(host, port):
        raise OSError(1 if host != units.SELFTEST_ADDRESS else 110, "x")  # EPERM unless listed

    monkeypatch.setattr(selftest, "_connect", filtered)
    items = _by_id(selftest.probe(layout, "net"))
    assert items["net-filter"]["status"] == "done" and items["net-public"]["status"] == "done"
    assert items["net-local"]["status"] == "done"
    monkeypatch.setattr(selftest, "_connect", lambda host, port: None)  # everything connects
    items = _by_id(selftest.probe(layout, "net"))
    assert items["net-filter"]["status"] == "todo" and items["net-public"]["status"] == "todo"


def test_host_sockets_seen_are_reported(layout, monkeypatch):
    monkeypatch.setattr(selftest, "HOST_SOCKETS", (str(layout.code),))
    assert _by_id(selftest.probe(layout, "job"))["job-host-sockets"]["status"] == "todo"
    monkeypatch.setattr(selftest, "HOST_SOCKETS", (str(layout.code / "nothing-here"),))
    assert _by_id(selftest.probe(layout, "job"))["job-host-sockets"]["status"] == "done"


def test_filter_settings_and_sandbox_switch_are_checked(layout, monkeypatch):
    good = {"IPAddressAllow": f"{units.SELFTEST_ADDRESS}/32", "IPAddressDeny": "0.0.0.0/0 ::/0"}
    assert selftest._filter_settings(layout, show=lambda *a: good)["status"] == "done"
    assert selftest._filter_settings(layout, show=lambda *a: {**good, "IPAddressDeny": ""})["status"] == "todo"
    assert selftest._filter_settings(layout, show=lambda *a: {})["status"] == "skipped"
    from mes import gate

    assert selftest._sandbox_required()["status"] == "done"  # the default, as installed
    monkeypatch.setattr(gate, "REQUIRE_SANDBOX_ON_SERVER", False)
    assert selftest._sandbox_required()["status"] == "todo"


def test_job_result_is_left_where_no_job_can_write(layout):
    """A job could otherwise write a forged 'all passed' answer into state/."""
    selftest.write_job_result(layout)
    assert (layout.gate / "selftest" / "job.json").is_file()
    assert not (layout.state / "selftest").exists()
    project = helpers.load(layout.root.parent / "proj", PROJECT_TOML, {"demo_app": APP_TOML})
    svc = units.render_selftest(project, layout=layout, mes="/usr/local/bin/mes")
    text = svc[units.selftest_units("demo")[1]]
    assert f"BindPaths=-{layout.gate}/selftest" in text


def test_stale_or_missing_job_answer_is_skipped_not_passed(layout, monkeypatch):
    monkeypatch.setattr(selftest, "JOB_WAIT_SEC", 0.2)
    out = layout.gate / "selftest" / "job.json"
    out.parent.mkdir()
    out.write_text(json.dumps({"time": 1, "items": [{"id": "x", "status": "done"}]}))  # from long ago
    items = selftest._job_probe(layout)
    assert items[0]["status"] == "skipped" and "did not answer" in items[0]["why"]
    assert (layout.gate / selftest.REQUEST_FILE).is_file()


def test_run_does_not_pass_what_it_could_not_check(tmp_path, monkeypatch):
    from mes import deploy
    from mes.errors import MesError

    def unreachable(*a, **k):
        raise MesError("no server", hint="x", code="ssh_failed")

    monkeypatch.setattr(deploy, "_send", unreachable)
    result = selftest.run("demo", server_root=str(tmp_path / "nothing"), host="my-server", login_key=None)
    assert result["ok"] is False and set(result["unproved"]) == {"outside", "inside"} and result["failed"] == []

    def answered(*a, **k):
        return {"items": [{"id": "read-auth", "title": "t", "status": "done", "why": "w"},
                          {"id": "job-network", "title": "t", "status": "todo", "why": "w"}]}

    monkeypatch.setattr(deploy, "_send", answered)
    result = selftest.run("demo", server_root=str(tmp_path / "nothing"), host="my-server", login_key=None)
    assert result["ok"] is False and result["failed"] == ["job-network"]


def test_outside_checks_from_the_owners_account(layout):
    if os.getuid() == 0:
        pytest.skip("run as a normal account")
    items = {i["id"]: i for i in selftest.outside(layout)}
    assert items["outside-data"]["status"] == "todo"  # the test account owns these folders
    assert items["outside-socket-read"]["status"] == "done"


def test_selftest_is_a_deploy_level_operation():
    from mes import gate

    assert "selftest" in gate.MES_OPERATIONS


def test_analyze_script_names_every_unit(tmp_path):
    proj = helpers.write(tmp_path / "demo", PROJECT_TOML, {"demo_app": APP_TOML})
    plan = server.build_plan(proj, "demo", tools={"mes": "/bin/sh", "uv": "/bin/sh"})
    script = server.render_analyze(plan)
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    for name in plan.units:
        assert f"{plan.units_dir}/{name}" in script
    assert "systemd-analyze security --no-pager -- mes-demo.job.demo-app.sync.service" in script
    assert "systemd-analyze security --no-pager -- mes-demo.gate-read@selftest.service" in script
    assert "systemd-analyze verify --man=no" in script
    assert "sudo" not in script.replace("needs no sudo", "")


def test_selftest_command_line(tmp_path, monkeypatch, capsys):
    from mes import cli, deploy

    monkeypatch.setattr(deploy, "_send", lambda *a, **k: {"items": []})
    code = cli.run(["--json", "server", "selftest", "demo", "--host", "my-server",
                    "--server-root", str(tmp_path / "srv")])
    data = json.loads(capsys.readouterr().out)
    assert code in (0, 1) and data["ok"] is False and "outside" in data["unproved"]


def test_selftest_probe_command_writes_the_job_answer(layout, capsys):
    from mes import cli

    code = cli.run(["--json", "server", "selftest-probe", "demo", "--context", "job",
                    "--server-root", str(layout.root)])
    assert code == 0
    assert json.loads((layout.gate / "selftest" / "job.json").read_text())["context"] == "job"
    capsys.readouterr()


def test_plan_output_mentions_the_selftest(tmp_path):
    proj = helpers.write(tmp_path / "demo", PROJECT_TOML, {"demo_app": APP_TOML})
    script = server.render_script(server.build_plan(proj, "demo", tools={"mes": "/bin/sh", "uv": "/bin/sh"}))
    assert "mes server selftest demo" in script


def _rendered_run_binds(layout, level_or_unit):
    """What the real rendered unit binds under /run (the sandbox's /run is otherwise empty)."""
    project = helpers.load(layout.root.parent / "proj-binds", PROJECT_TOML, {"demo_app": APP_TOML})
    files = units.render_project(project, layout=layout, mes="/usr/local/bin/mes")
    text = files[level_or_unit]
    binds = []
    for line in text.splitlines():
        if line.startswith(("BindPaths=", "BindReadOnlyPaths=")):
            binds += [w.lstrip("-").split(":")[-1] for w in line.split("=", 1)[1].split()]
    return [b for b in binds if b.startswith("/run/")]


def _sandbox_fs(binds):
    """The paths a sandbox shows under /run: each bound path and the folders systemd makes for it."""
    shown = set()
    for b in binds:
        parts = b.split("/")
        shown |= {"/".join(parts[:i]) for i in range(3, len(parts) + 1)}
    return shown


def test_the_install_steps_resolver_socket_is_allowed_and_nothing_else(layout):
    """The real HOST_SOCKETS list held /run/systemd/resolve, which the install unit binds on
    purpose (its resolver socket), so deploy-host-sockets always failed. Checked here with the
    real list and the real rendered unit's binds."""
    binds = _rendered_run_binds(layout, "mes-demo.gate-deploy@.service")
    assert binds == [units.RESOLVE_SOCKET]
    shown = _sandbox_fs(binds)
    exists = shown.__contains__
    item = selftest._host_sockets("deploy-", "the install step", "deploy", exists=exists,
                                  under_run=lambda: sorted(shown))
    assert item["status"] == "done", item
    # Anything else under /run fails: a host socket of the list, or any other entry.
    for extra in ("/run/dbus/system_bus_socket", "/run/systemd/resolve/other", "/run/whatever"):
        more = shown | _sandbox_fs([extra])
        item = selftest._host_sockets("deploy-", "the install step", "deploy", exists=more.__contains__,
                                      under_run=lambda m=more: sorted(m))
        assert item["status"] == "todo" and extra in item["why"], extra
    # A job binds nothing under /run, and the same resolver socket there is a failure.
    assert _rendered_run_binds(layout, "mes-demo.job.demo-app.sync.service") == []
    assert selftest._host_sockets("job-", "a job", "job", exists=exists)["status"] == "todo"
    assert selftest._host_sockets("job-", "a job", "job", exists=lambda p: False)["status"] == "done"


@pytest.mark.parametrize("err,status", [(1, "done"), (13, "done"), (111, "todo"), (110, "todo"), (None, "todo")])
def test_install_step_blocks_count_only_the_filters_own_refusal(layout, monkeypatch, err, status):
    """A refused connection or a timeout happens without any filter: only EPERM/EACCES at connect
    proves the address filter blocks this computer and the local network."""
    def connect(host, port):
        if err is not None:
            raise OSError(err, "x")

    monkeypatch.setattr(selftest, "_connect", connect)
    items = _by_id(selftest.probe(layout, "deploy"))
    for claim in ("deploy-loopback", "deploy-lan"):
        assert items[claim]["status"] == status, (claim, items[claim])
    if err in (111, 110):
        assert "cannot tell" in items["deploy-loopback"]["why"]


def test_privileges_are_not_passed_when_proc_cannot_be_read():
    assert selftest._privileges("job-", "a job", status=lambda name: None)["status"] == "todo"
    good = {"NoNewPrivs": "1", "CapEff": "0000000000000000"}
    if os.geteuid() != 0:
        assert selftest._privileges("job-", "a job", status=good.get)["status"] == "done"
    assert selftest._privileges("job-", "a job", status={**good, "NoNewPrivs": "0"}.get)["status"] == "todo"
    assert selftest._privileges("job-", "a job", status={**good, "CapEff": "00000000a80425fb"}.get)["status"] == "todo"
