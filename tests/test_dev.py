"""Tests for ``mes dev`` (mes.dev), with a fake easy-ui program standing in for my-easy-ui.

The fake records every command line it gets (one JSON list per line) and its process id, answers
``data build`` with a JSON object and, for ``preview``, prints a local link and then sleeps until
it is stopped.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from mes import cli, dev, siblings
from mes.errors import ManifestError, NotFound, UsageError

FAKE = """#!{python}
import json, os, sys, time
args = sys.argv[1:]
with open({log!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
if "preview" in args:
    with open({pid!r}, "w") as f:
        f.write(str(os.getpid()))
    print("easy-ui preview: starting", flush=True)
    print("Open http://127.0.0.1:{port}/dashboards/main in a browser.", flush=True)
    time.sleep(120)
elif args[-2:] == ["data", "build"]:
    print(json.dumps({{"bundle": "b1", "tables": {{"daily": {{"rows": 1}}}}}}))
else:
    print(json.dumps({{"ok": True}}))
"""

PROJECT = """[project]
name = "demo"

[jobs.rebuild]
run = "demo rebuild"
manual = true

[ui]
data = "demo.sqlite"
tables = ["daily"]
dashboards = ["main"]
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    log, pid = tmp_path / "fake-calls.jsonl", tmp_path / "fake.pid"
    exe = tmp_path / "bin" / "easy-ui"
    exe.parent.mkdir()
    exe.write_text(FAKE.format(python=sys.executable, log=str(log), pid=str(pid), port=43117))
    exe.chmod(0o755)
    monkeypatch.setenv("MES_EASY_UI", str(exe))
    monkeypatch.setattr(dev, "POLL", 0.05)
    for var in ("MES_PROJECT", "MES_DATA_DIR", "MES_STATE_DIR", "MES_STORE_DIR"):
        monkeypatch.delenv(var, raising=False)

    class Fake:
        path = exe

        @staticmethod
        def calls() -> list[list[str]]:
            return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

        @staticmethod
        def pid() -> int:
            return int(pid.read_text())

    return Fake


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "demo"
    (folder / "ui" / "dashboards").mkdir(parents=True)
    (folder / "project.toml").write_text(PROJECT)
    (folder / "ui" / "dashboards" / "main.yaml").write_text("spec: easy-ui/v1\ntitle: Demo\n")
    return folder


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


URL = "http://127.0.0.1:43117/dashboards/main"


def test_text_mode_prints_one_link_and_stops_the_preview(fake, project):
    out, err = io.StringIO(), io.StringIO()
    started = time.monotonic()
    code = dev.run_dev(str(project), stop_after=0.3, out=out, err=err)
    assert code == 0 and time.monotonic() - started < 30
    text = out.getvalue()
    lines = [line for line in text.splitlines() if "http" in line]
    assert lines == [f"Open this link in a browser on this computer: {URL}"]
    assert "sample data" in text and "Ctrl-C" in text
    assert _gone(fake.pid())
    calls = fake.calls()
    assert calls == [["--project-dir", str(project.resolve()), "preview"]]  # no data yet: no data build


def test_json_mode_prints_exactly_one_object(fake, project):
    out, err = io.StringIO(), io.StringIO()
    assert dev.run_dev(str(project), stop_after=0.2, json_mode=True, out=out, err=err) == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    info = json.loads(lines[0])
    assert info["project"] == "demo" and info["url"] == URL
    assert "sample data" in info["data"]
    assert info["watching"] == ["project.toml", "ui/dashboards/main.yaml"]
    assert _gone(fake.pid())


def test_port_is_passed_on(fake, project):
    dev.run_dev(str(project), port=8123, stop_after=0.1, out=io.StringIO(), err=io.StringIO())
    assert fake.calls()[-1][-3:] == ["preview", "--port", "8123"]


def test_data_build_runs_when_the_project_has_data(fake, project, mes_home):
    data = mes_home["data"] / "demo"
    data.mkdir(parents=True)
    (data / "demo.sqlite").write_bytes(b"")
    out = io.StringIO()
    assert dev.run_dev(str(project), stop_after=0.1, json_mode=True, out=out, err=io.StringIO()) == 0
    assert json.loads(out.getvalue())["data"] == "the project's own data"
    root = str(project.resolve())
    assert fake.calls()[0] == ["--json", "--project-dir", root, "data", "build"]
    assert fake.calls()[1] == ["--project-dir", root, "preview"]


def _edit_when_ready(out: io.StringIO, action) -> threading.Thread:
    def worker():
        deadline = time.monotonic() + 20
        while "http" not in out.getvalue() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)
        action()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def test_project_toml_changes_are_reported_on_stderr(fake, project):
    out, err = io.StringIO(), io.StringIO()
    manifest_file = project / "project.toml"

    def break_it():
        manifest_file.write_text(PROJECT + "\ncolour = 1\n")
        bump = time.time() + 5
        os.utime(manifest_file, (bump, bump))

    t = _edit_when_ready(out, break_it)
    assert dev.run_dev(str(project), stop_after=1.5, out=out, err=err) == 0
    t.join()
    said = err.getvalue()
    assert "project.toml changed and now has a problem" in said and '"colour"' in said
    assert out.getvalue().count("http") == 1  # the link is printed once, events go to stderr


def test_correct_change_and_dashboard_save_are_reported(fake, project):
    out, err = io.StringIO(), io.StringIO()

    def touch_both():
        bump = time.time() + 5
        os.utime(project / "project.toml", (bump, bump))
        os.utime(project / "ui" / "dashboards" / "main.yaml", (bump, bump))

    t = _edit_when_ready(out, touch_both)
    dev.run_dev(str(project), stop_after=1.5, out=out, err=err)
    t.join()
    said = err.getvalue()
    assert "project.toml changed and is still correct" in said
    assert "Saved ui/dashboards/main.yaml" in said


def test_data_change_rebuilds_the_dashboard_data(fake, project, mes_home):
    data = mes_home["data"] / "demo"
    data.mkdir(parents=True)
    db = data / "demo.sqlite"
    out, err = io.StringIO(), io.StringIO()
    t = _edit_when_ready(out, lambda: db.write_bytes(b"x"))
    dev.run_dev(str(project), stop_after=1.5, out=out, err=err)
    t.join()
    builds = [c for c in fake.calls() if c[-2:] == ["data", "build"]]
    assert len(builds) == 1  # none at the start (no data yet), one after the file appeared
    assert "the dashboard data was rebuilt" in err.getvalue()


def test_no_ui_section_is_a_usage_error(fake, project):
    (project / "project.toml").write_text('[project]\nname = "demo"\n')
    with pytest.raises(UsageError) as e:
        dev.run_dev(str(project), stop_after=0.1, out=io.StringIO(), err=io.StringIO())
    assert "[ui]" in e.value.message and "[ui]" in e.value.hint
    assert fake.calls() == []


def test_easy_ui_missing_is_not_found(project, monkeypatch, tmp_path):
    monkeypatch.setenv("MES_EASY_UI", str(tmp_path / "nowhere" / "easy-ui"))
    with pytest.raises(siblings.NotFound) as e:
        dev.run_dev(str(project), stop_after=0.1, out=io.StringIO(), err=io.StringIO())
    assert e.value.code == "easy_ui_missing" and "my-easy-ui" in e.value.message
    assert isinstance(e.value, NotFound)


def test_broken_project_toml_is_reported_before_starting(fake, project):
    (project / "project.toml").write_text(PROJECT + "colour = 1\n")
    with pytest.raises(ManifestError):
        dev.run_dev(str(project), stop_after=0.1, out=io.StringIO(), err=io.StringIO())
    assert fake.calls() == []


def test_preview_that_stops_without_a_link_is_an_error(fake, project, tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "broken-easy-ui"
    exe.write_text(f"#!{sys.executable}\nimport sys\nprint('Dashboard main.yaml has a mistake', file=sys.stderr)\n"
                   "sys.exit(1)\n")
    exe.chmod(0o755)
    monkeypatch.setenv("MES_EASY_UI", str(exe))
    with pytest.raises(dev.MesError) as e:
        dev.run_dev(str(project), stop_after=0.1, out=io.StringIO(), err=io.StringIO())
    assert e.value.code == "preview_failed" and "has a mistake" in (e.value.detail or "")


def test_mes_dev_command(fake, project, capsys):
    with pytest.raises(SystemExit) as e:
        cli.run(["--json", "dev", "--project-dir", str(project), "--stop-after", "0.2"])
    assert e.value.code == 0
    assert json.loads(capsys.readouterr().out)["url"] == URL


def test_mes_dev_needs_a_positive_stop_after_and_write_level(fake, project, capsys, monkeypatch):
    assert cli.run(["--json", "dev", "--project-dir", str(project), "--stop-after", "0"]) == 2
    capsys.readouterr()
    monkeypatch.setenv("MES_ALLOW", "read")
    assert cli.run(["--json", "dev", "--project-dir", str(project), "--stop-after", "0.2"]) == 4
    assert fake.calls() == []


def test_run_dev_works_from_another_thread(fake, project):
    result = {}

    def worker():
        out = io.StringIO()
        result["code"] = dev.run_dev(str(project), stop_after=0.2, json_mode=True, out=out, err=io.StringIO())
        result["out"] = out.getvalue()

    t = threading.Thread(target=worker)
    t.start()
    t.join(30)
    assert result["code"] == 0 and json.loads(result["out"])["url"] == URL
    assert _gone(fake.pid())
