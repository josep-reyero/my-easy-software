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


def test_deploy_probe_expects_code_to_be_writable(layout):
    items = _by_id(selftest.probe(layout, "deploy"))
    assert items["deploy-code"]["status"] == "done"
    assert "deploy-syscalls" in items and "deploy-loopback" in items and "deploy-network" not in items


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
