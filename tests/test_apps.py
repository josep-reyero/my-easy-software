"""``mes apps list|sync|check`` (mes.apps) and the project command (mes.project).

Programs are small fake executables in ``.venv/bin``: mes never imports an app's code, it only runs
its program and reads ``--json describe``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

import projects as helpers
from mes import apps, cli, manifest, project as project_cmd
from mes.errors import ManifestError, MesError

PROJECT = '[project]\nname = "pp"\ntitle = "PP"\napps = ["pp_a", "pp_b"]\n'
A = '[app]\nname = "pp-a"\ncommand = "pp-a"\n[jobs.sync]\nrun = "sync"\nmanual = true\n'
B = '[app]\nname = "pp-b"\ncommand = "pp-b"\n[jobs.tidy]\nrun = "tidy"\nafter = "pp-a.sync"\n'

FAKE = """#!{python}
import json, os, sys
args = [a for a in sys.argv[1:] if a != "--json"]
NAME = {name!r}
KINDS = {kinds!r}
if args[:1] == ["describe"]:
    print(json.dumps({{"app": NAME, "commands": [{{"name": n, "kind": k}} for n, k in KINDS.items()]}}))
elif args[:1] == ["guide"]:
    print(json.dumps({{"app": NAME, "guide": "Guide of " + NAME}}))
else:
    print(json.dumps({{"app": NAME, "argv": sys.argv[1:], "allow": os.environ.get("MES_ALLOW")}}))
"""


def _program(root: Path, name: str, app: str | None = None, kinds: dict | None = None) -> Path:
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    p = bin_dir / name
    p.write_text(FAKE.format(python=sys.executable, name=app or name,
                             kinds=kinds or {"sync": "write", "tidy": "write", "list": "read", "add": "write",
                                             "describe": "read", "guide": "read"}))
    p.chmod(0o755)
    return p


@pytest.fixture
def root(tmp_path):
    r = helpers.write(tmp_path / "pp", PROJECT, {"pp_a": A, "pp_b": B})
    _program(r, "pp-a")
    _program(r, "pp-b")
    _program(r, "pp")  # stands in for the project command (mes.project:main)
    return r


# --------------------------------------------------------------------------- sync


def test_sync_writes_the_lock(root):
    out = apps.sync(root)
    lock = json.loads((root / manifest.LOCK_FILE).read_text())
    assert lock["v"] == 1 and lock["project"] == "pp" and [a["package"] for a in lock["apps"]] == ["pp_a", "pp_b"]
    assert all(a["in_project"] for a in lock["apps"]) and lock["apps"][0]["program"] == "pp-a"
    assert stat.S_IMODE((root / manifest.LOCK_FILE).stat().st_mode) == 0o644
    assert out["problems"] == [] and out["warnings"] == []
    assert manifest.load(root).lock_used


def test_sync_refuses_a_job_that_runs_no_command(root):
    _program(root, "pp-b", kinds={"describe": "read"})
    with pytest.raises(MesError) as e:
        apps.sync(root)
    assert "tidy" in e.value.message
    assert not (root / manifest.LOCK_FILE).exists()
    out = apps.sync(root, strict=False)  # deploys: a note, not a refusal
    assert out["problems"] and (root / manifest.LOCK_FILE).exists()


def test_sync_refuses_a_program_that_is_another_app(root):
    _program(root, "pp-b", app="pp-a")
    with pytest.raises(MesError) as e:
        apps.sync(root)
    assert e.value.code == "app_mismatch"


def test_sync_needs_the_project_command_and_each_program(root):
    (root / ".venv" / "bin" / "pp").unlink()
    with pytest.raises(MesError) as e:
        apps.sync(root)
    assert e.value.code == "project_command_missing" and "mes.project:main" in e.value.hint
    _program(root, "pp")
    (root / ".venv" / "bin" / "pp-b").unlink()
    with pytest.raises(MesError) as e:
        apps.sync(root)
    assert e.value.code == "app_not_installed" and "uv sync" in e.value.hint


def test_sync_with_an_installed_app_uses_find_spec(root, tmp_path):
    site = tmp_path / "site" / "pp_c"
    site.mkdir(parents=True)
    (site / "app.toml").write_text('[app]\nname = "pp-c"\ncli = "pp_c.cli:app"\n')
    (root / "project.toml").write_text(PROJECT.replace('"pp_b"]', '"pp_b", "pp_c"]'))
    _program(root, "pp-c")
    (root / ".venv" / "bin" / "python").symlink_to(sys.executable)
    seen = []

    def runner(argv, *, env, timeout, cwd):
        if "-c" in argv:
            seen.append(argv)
            assert argv[1] == "-I" and env.get("MES_ALLOW") == "read"
            return 0, json.dumps({"pp_c": str(site)}).encode(), b""
        from mes.apps import _default_runner
        return _default_runner(argv, env=env, timeout=timeout, cwd=cwd)

    apps.sync(root, runner=runner)
    assert seen and seen[0][-1:] == ["pp_c"]
    p = manifest.load(root)
    assert p.apps["pp-c"].location == "installed" and p.apps["pp-c"].folder == site.resolve()


def test_check_lists_what_is_done_and_what_to_do(root):
    out = apps.check(root)
    by = {i["id"]: i for i in out["items"]}
    assert by["project"]["status"] == "done"
    (root / "project.toml").write_text(PROJECT + "\n")
    apps.sync(root)
    (root / "project.toml").write_text(PROJECT)  # changed after the sync: the lock is out of date
    by = {i["id"]: i for i in apps.check(root)["items"]}
    assert by["lock"]["status"] == "todo" and "mes apps sync" in json.dumps(by["lock"])
    apps.sync(root)
    out = apps.check(root)
    by = {i["id"]: i for i in out["items"]}
    assert by["lock"]["status"] == "done" and by["programs"]["status"] == "done"


def test_apps_commands(root, capsys):
    assert cli.run(["--json", "apps", "sync", "--project-dir", str(root)]) == 0
    capsys.readouterr()
    assert cli.run(["--json", "apps", "list", "--project-dir", str(root)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [a["name"] for a in data["apps"]] == ["pp-a", "pp-b"]
    assert cli.run(["apps", "list", "--project-dir", str(root)]) == 0
    assert "pp-a" in capsys.readouterr().out
    assert cli.run(["--json", "apps", "check", "--project-dir", str(root)]) == 0


# --------------------------------------------------------------------------- the project command


class Exec:
    def __init__(self):
        self.calls = []

    def __call__(self, path, argv):
        self.calls.append(argv)


@pytest.fixture
def run_project(root, monkeypatch):
    monkeypatch.setenv("MES_PROJECT_DIR", str(root))
    monkeypatch.delenv("MES_PROJECT", raising=False)

    def go(*argv):
        ex = Exec()
        code = project_cmd.run(list(argv), bin_dir=root / ".venv" / "bin", execv=ex)
        return code, ex.calls

    return go


def test_an_app_is_reached_by_its_short_or_full_name(run_project, root):
    bin_dir = root / ".venv" / "bin"
    assert run_project("a", "list", "--json") == (0, [[str(bin_dir / "pp-a"), "list", "--json"]])
    assert run_project("--json", "pp-b", "tidy") == (0, [[str(bin_dir / "pp-b"), "--json", "tidy"]])


def test_with_several_apps_a_command_alone_is_a_usage_error(run_project, capsys):
    code, calls = run_project("list")
    err = capsys.readouterr().err
    assert code == 2 and calls == [] and "a, b" in err and "pp a list" in err


def test_with_one_app_a_command_is_passed_on_with_a_note(run_project, root, capsys):
    (root / "project.toml").write_text('[project]\nname = "pp"\napps = ["pp_a"]\n')
    code, calls = run_project("list")
    assert code == 0 and calls[0][1:] == ["list"]
    assert capsys.readouterr().err == ('Note: "pp list" was passed to pp-a, the only app of this project. '
                                       'Write "pp a list" so it keeps working when the project has more apps.\n')
    code, calls = run_project("list", "--json")
    assert code == 0 and capsys.readouterr().err == ""  # no note for machines


def test_a_read_session_cannot_start_a_write_command(run_project, monkeypatch, capsys):
    monkeypatch.setenv("MES_ALLOW", "read")
    code, calls = run_project("a", "sync", "--json")
    assert code == 4 and calls == []
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_allowed"
    assert run_project("a", "list")[0] == 0


def test_builtins(run_project, capsys):
    assert run_project("--json", "describe")[0] == 0
    data = json.loads(capsys.readouterr().out)
    assert [a["name"] for a in data["apps"]] == ["pp-a", "pp-b"] and data["apps"][0]["describe"]["app"] == "pp-a"
    assert [j["id"] for j in data["jobs"]] == ["pp-a.sync", "pp-b.tidy"]
    assert run_project("apps", "--json")[0] == 0
    assert [a["short"] for a in json.loads(capsys.readouterr().out)["apps"]] == ["a", "b"]
    assert run_project("guide")[0] == 0
    text = capsys.readouterr().out
    assert "Guide of pp-a" in text and "Guide of pp-b" in text
    assert run_project()[0] == 0 and "pp a ..." in capsys.readouterr().out
    assert run_project("describe", "extra")[0] == 2


def test_the_project_command_refuses_another_projects_session(run_project, monkeypatch, capsys):
    monkeypatch.setenv("MES_PROJECT", "other")
    code, calls = run_project("a", "list", "--json")
    assert code != 0 and calls == [] and json.loads(capsys.readouterr().out)["error"]["code"] == "wrong_project"


def test_a_missing_program_says_uv_sync(run_project, root, capsys):
    (root / ".venv" / "bin" / "pp-b").unlink()
    code, calls = run_project("b", "tidy", "--json")
    err = json.loads(capsys.readouterr().out)["error"]
    assert code == 3 and "uv sync" in err["hint"]


# --------------------------------------------------------------------------- the gate's new request shape


def test_gate_rewrites_project_requests(root):
    from mes import gate, registry

    apps.sync(root)
    registry.add(root)
    where = gate.locate("pp", None)
    bin_dir = (root / ".venv" / "bin").resolve()
    request, project, program, builtin = gate.resolve_request(where, ["pp", "b", "tidy", "--json"])
    assert request == ["pp-b", "tidy", "--json"] and builtin is False and program.resolve() == bin_dir / "pp-b"
    request, project, program, builtin = gate.resolve_request(where, ["pp", "describe", "--json"])
    assert builtin is True and program.resolve() == bin_dir / "pp"
    request, project, program, builtin = gate.resolve_request(where, ["pp-a", "sync"])
    assert request == ["pp-a", "sync"] and program.resolve() == bin_dir / "pp-a"
    with pytest.raises(MesError) as e:
        gate.resolve_request(where, ["pp", "tidy"])
    assert e.value.exit_code == 2 and "a, b" in e.value.hint
    with pytest.raises(MesError):
        gate.resolve_request(where, ["pp", "describe", "more"])
