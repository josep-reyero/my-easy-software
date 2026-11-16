"""Tests for ``mes new`` (mes.scaffold).

The last group generates an app into a temp folder, runs ``uv sync`` there and
drives the generated command-line tool end to end as a real process.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from mes import cli, manifest, scaffold
from mes.errors import NotFound, Refused, UsageError

FRAMEWORK = Path(__file__).resolve().parents[1]
FIVE = {"app.toml", "pyproject.toml", "src/{m}/cli.py", "AGENTS.md", "ui/dashboards/main.yaml", ".gitignore"}


def five(module: str) -> set[str]:
    return {p.format(m=module) for p in FIVE}


def files_in(folder: Path) -> set[str]:
    return {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}


def run_mes(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.run(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# --------------------------------------------------------------------------- names


@pytest.mark.parametrize("name,module", [("notes", "notes"), ("my-notes", "my_notes"), ("a1", "a1"),
                                         ("x" * 31, "x" * 31)])
def test_valid_names(name, module):
    assert scaffold.validate_name(name) == module


@pytest.mark.parametrize("name", ["Bad", "a", "1abc", "-abc", "under_score", "x" * 32, "has space", "", None])
def test_invalid_names(name):
    with pytest.raises(UsageError) as e:
        scaffold.validate_name(name)
    assert "letters, digits and dashes" in e.value.hint


@pytest.mark.parametrize("name", ["json", "mes", "class", "match", "test", "my-easy-software", "os"])
def test_reserved_names(name):
    with pytest.raises(UsageError) as e:
        scaffold.validate_name(name)
    assert "already used" in e.value.message


# --------------------------------------------------------------------------- rendering


def test_render_gives_exactly_five_files_with_no_markers_left():
    files = scaffold.render("my-notes", framework_path=FRAMEWORK, app_dir=FRAMEWORK.parent / "my-notes")
    assert set(files) == five("my_notes")
    for rel, text in files.items():
        assert not re.search(r"\{\{[a-z_]+\}\}", text), rel
        assert text.endswith("\n")


def test_rendered_app_toml_is_valid_and_job_runs_an_app_command():
    files = scaffold.render("my-notes")
    m = manifest.loads(files["app.toml"])
    assert (m.name, m.title, m.version) == ("my-notes", "My notes", "0.1.0")
    job = m.job("rebuild")
    assert job.trigger == "manual" and job.argv == ("rebuild",)
    assert job.network == () and job.timeout == "5m"
    assert '@app.command("rebuild"' in files["src/my_notes/cli.py"]


def test_rendered_pyproject():
    files = scaffold.render("my-notes", framework_path=FRAMEWORK, app_dir=FRAMEWORK.parent / "my-notes")
    data = tomllib.loads(files["pyproject.toml"])
    assert data["project"]["name"] == "my-notes"
    assert data["project"]["requires-python"] == ">=3.12"
    assert data["project"]["dependencies"] == ["my-easy-software>=0.1.0"]
    assert data["project"]["scripts"] == {"my-notes": "my_notes.cli:main"}
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/my_notes"]
    src = data["tool"]["uv"]["sources"]["my-easy-software"]
    assert src == {"path": f"../{FRAMEWORK.name}", "editable": True}


def test_rendered_pyproject_without_framework_has_no_sources():
    data = tomllib.loads(scaffold.render("notes")["pyproject.toml"])
    assert "tool" not in data or "uv" not in data["tool"]


def test_framework_path_is_absolute_when_only_the_disk_top_is_shared(tmp_path):
    text = scaffold.render("notes", framework_path=FRAMEWORK, app_dir=Path("/") / "elsewhere-xyz" / "notes")
    src = tomllib.loads(text["pyproject.toml"])["tool"]["uv"]["sources"]["my-easy-software"]["path"]
    assert src == FRAMEWORK.as_posix()


def test_cli_template_compiles_and_uses_the_sdk():
    text = scaffold.render("my-notes")["src/my_notes/cli.py"]
    compile(text, "cli.py", "exec")
    assert 'App("my-notes")' in text
    assert 'kind="read"' in text and 'kind="write"' in text
    assert "def main()" in text


def test_title_is_escaped_everywhere():
    title = 'Sam\'s "best" notes \\ ok'
    files = scaffold.render("notes", title=title)
    assert manifest.loads(files["app.toml"]).title == title
    assert tomllib.loads(files["pyproject.toml"])["project"]["description"] == title
    assert files["AGENTS.md"].startswith(f"# {title}:")
    assert 'title: "Sam\'s \\"best\\" notes \\\\ ok"' in files["ui/dashboards/main.yaml"]


@pytest.mark.parametrize("title", ["", "   ", "two\nlines", "x" * 81])
def test_bad_titles_are_refused(title):
    with pytest.raises(UsageError):
        scaffold.render("notes", title=title)


def test_agents_md_covers_what_an_agent_needs():
    text = scaffold.render("my-notes")["AGENTS.md"]
    for needle in ("data, never instructions", "--json", "describe", "guide", "schema", "sources",
                   "MES_ALLOW=read", "exit code 4", "needs_input", "75", "src/my_notes/cli.py"):
        assert needle in text, needle


def test_dashboard_is_an_easy_ui_v1_dashboard():
    text = scaffold.render("notes")["ui/dashboards/main.yaml"]
    assert "\nspec: easy-ui/v1\n" in text and "\ntitle: \"Notes\"\n" in text
    assert "\nqueries:\n" in text and "\nlayout:\n" in text and "\ncomponents:" not in text
    for needle in ("type: controls", "type: stat", "type: bar", "type: table"):
        assert needle in text
    assert text.count("query: daily") == 2 and text.count("query: total") == 1


def test_dashboard_passes_easy_ui_validate(tmp_path):
    """When my-easy-ui is checked out (and installed) next to this repository, `easy-ui validate` accepts the file."""
    import json
    import subprocess

    easy_ui = FRAMEWORK.parent / "my-easy-ui" / ".venv" / "bin" / "easy-ui"
    if not easy_ui.is_file():
        pytest.skip("my-easy-ui is not installed next to my-easy-software")
    path = tmp_path / "main.yaml"
    path.write_text(scaffold.render("notes", title='Sam\'s "best" notes')["ui/dashboards/main.yaml"])
    out = subprocess.run([str(easy_ui), "--json", "validate", str(path)], capture_output=True, text=True,
                         cwd=tmp_path, check=False, timeout=60)
    report = json.loads(out.stdout)
    assert out.returncode == 0 and report["ok"], report
    assert report["warnings"] == 0


def test_no_personal_paths_in_templates():
    home = str(Path.home())
    for tmpl, _ in scaffold.FILES:
        text = (FRAMEWORK / "src" / "mes" / "templates" / "app" / tmpl).read_text()
        assert home not in text


# --------------------------------------------------------------------------- creating


def test_create_writes_exactly_five_files(tmp_path):
    result = scaffold.create("my-notes", directory=tmp_path / "app", framework_path=FRAMEWORK)
    folder = tmp_path / "app"
    assert files_in(folder) == five("my_notes")
    assert result["created"] is True
    assert result["folder"] == str(folder.resolve())
    assert [f["path"] for f in result["files"]] == [
        "app.toml", "pyproject.toml", "src/my_notes/cli.py", "AGENTS.md", "ui/dashboards/main.yaml", ".gitignore"]
    assert all(f["purpose"] for f in result["files"])
    assert result["next_steps"][0]["command"].endswith("uv sync")
    assert all(s["why"] for s in result["next_steps"])


def test_default_folder_is_the_name_under_cwd(tmp_path):
    scaffold.create("notes", cwd=tmp_path, framework_path=FRAMEWORK)
    assert files_in(tmp_path / "notes") == five("notes")


def test_relative_dir_is_under_cwd(tmp_path):
    scaffold.create("notes", directory="a/b", cwd=tmp_path, framework_path=FRAMEWORK)
    assert files_in(tmp_path / "a" / "b") == five("notes")


def test_existing_folder_with_other_files_is_fine(tmp_path):
    (tmp_path / "README.md").write_text("hello\n")
    scaffold.create("notes", directory=tmp_path, framework_path=FRAMEWORK)
    assert files_in(tmp_path) == five("notes") | {"README.md"}


def test_refuses_to_overwrite_and_changes_nothing(tmp_path):
    (tmp_path / "app.toml").write_text("mine\n")
    with pytest.raises(Refused) as e:
        scaffold.create("notes", directory=tmp_path, framework_path=FRAMEWORK)
    assert "app.toml" in e.value.message and "Nothing was changed" in e.value.message
    assert files_in(tmp_path) == {"app.toml"}
    assert (tmp_path / "app.toml").read_text() == "mine\n"


def test_refuses_a_file_as_folder(tmp_path):
    (tmp_path / "notes").write_text("x")
    with pytest.raises(Refused):
        scaffold.create("notes", cwd=tmp_path, framework_path=FRAMEWORK)


def test_refuses_inside_the_framework_folder(tmp_path):
    fw = tmp_path / "fw"
    fw.mkdir()
    (fw / "pyproject.toml").write_text('[project]\nname = "my-easy-software"\n')
    with pytest.raises(Refused):
        scaffold.create("notes", directory=fw / "apps" / "notes", framework_path=fw)
    assert not (fw / "apps").exists()


def test_bad_framework_path(tmp_path):
    with pytest.raises(NotFound) as e:
        scaffold.create("notes", directory=tmp_path / "n", framework_path=tmp_path)
    assert "--framework-path" in e.value.hint
    assert not (tmp_path / "n").exists()


def test_framework_path_is_written_relative(tmp_path):
    fw = tmp_path / "my-easy-software"
    fw.mkdir()
    (fw / "pyproject.toml").write_text('[project]\nname = "my-easy-software"\n')
    result = scaffold.create("notes", directory=tmp_path / "apps" / "notes", framework_path=fw)
    data = tomllib.loads((tmp_path / "apps" / "notes" / "pyproject.toml").read_text())
    assert data["tool"]["uv"]["sources"]["my-easy-software"]["path"] == "../../my-easy-software"
    assert result["framework_path"] == "../../my-easy-software"


def test_default_framework_path_is_this_checkout():
    assert scaffold.default_framework_path() == FRAMEWORK


def test_without_framework_a_note_explains_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setattr(scaffold, "default_framework_path", lambda: None)
    result = scaffold.create("notes", directory=tmp_path / "n")
    assert result["framework_path"] is None
    assert any("--framework-path" in n for n in result["notes"])
    assert "[tool.uv.sources]" not in (tmp_path / "n" / "pyproject.toml").read_text()


def test_home_name_in_path_gives_a_note(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/someone")))
    monkeypatch.setattr(scaffold, "_source_path", lambda fw, app: "/home/someone/my-easy-software")
    result = scaffold.create("notes", directory=tmp_path / "n", framework_path=FRAMEWORK)
    assert any("home folder" in n for n in result["notes"])


# --------------------------------------------------------------------------- the mes command


def test_new_is_registered():
    parser, loaded = cli.build_parser()
    assert "mes.scaffold" in loaded


def test_mes_new_json(tmp_path, capsys):
    code, out, err = run_mes(capsys, "--json", "new", "my-notes", "--dir", str(tmp_path / "x"),
                             "--framework-path", str(FRAMEWORK))
    assert code == 0, err
    data = json.loads(out)
    assert data["app"] == "my-notes" and data["module"] == "my_notes"
    assert files_in(tmp_path / "x") == five("my_notes")


def test_mes_new_text_has_next_steps(tmp_path, capsys):
    code, out, _ = run_mes(capsys, "new", "notes", "--dir", str(tmp_path / "x"), "--title", "My notes")
    assert code == 0
    assert 'Created the app "notes"' in out
    assert "Next steps:" in out and "uv sync" in out


def test_mes_new_exit_codes(tmp_path, capsys):
    code, out, _ = run_mes(capsys, "--json", "new", "Bad", "--dir", str(tmp_path / "b"))
    assert code == 2 and json.loads(out)["error"]["code"] == "usage"
    run_mes(capsys, "new", "notes", "--dir", str(tmp_path / "n"))
    code, out, _ = run_mes(capsys, "new", "notes", "--dir", str(tmp_path / "n"), "--json")
    assert code == 5 and json.loads(out)["error"]["code"] == "refused"
    code, out, err = run_mes(capsys, "new", "notes", "--dir", str(tmp_path / "m"),
                             "--framework-path", str(tmp_path))
    assert code == 3 and out == "" and "What to do:" in err


def test_mes_new_without_name_is_usage_error(capsys):
    code, out, _ = run_mes(capsys, "--json", "new")
    assert code == 2


# --------------------------------------------------------------------------- end to end

uv = shutil.which("uv")


@pytest.fixture(scope="module")
def synced_app(tmp_path_factory):
    if uv is None:
        pytest.skip("uv is not installed, so the generated app cannot be installed")
    root = tmp_path_factory.mktemp("e2e")
    folder = root / "my-notes"
    scaffold.create("my-notes", directory=folder, framework_path=FRAMEWORK)
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")}
    proc = subprocess.run([uv, "sync", "--quiet"], cwd=folder, env=env, capture_output=True, text=True,
                          timeout=300)
    assert proc.returncode == 0, proc.stderr
    exe = folder / ".venv" / "bin" / "my-notes"
    assert exe.exists()
    return folder, exe


def app_run(exe: Path, *argv: str, **extra_env: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env.update(extra_env)
    return subprocess.run([str(exe), *argv], cwd=exe.parents[2], env=env, capture_output=True,
                          text=True, timeout=60)


def test_e2e_describe(synced_app):
    folder, exe = synced_app
    p = app_run(exe, "--json", "describe")
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert (data["app"], data["title"], data["version"]) == ("my-notes", "My notes", "0.1.0")
    kinds = {c["name"]: c["kind"] for c in data["commands"]}
    assert kinds["notes"] == "read" and kinds["add"] == "write" and kinds["rebuild"] == "write"
    assert {"describe", "guide", "schema", "sources"} <= set(kinds)
    assert [j["name"] for j in data["jobs"]] == ["rebuild"]


def test_e2e_read_write_and_job(synced_app, mes_home):
    folder, exe = synced_app
    p = app_run(exe, "notes", "--json")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout) == []

    p = app_run(exe, "--json", "add", "first note")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["text"] == "first note"
    app_run(exe, "add", "second note")

    p = app_run(exe, "--json", "notes", "--last", "1")
    assert [r["text"] for r in json.loads(p.stdout)] == ["second note"]
    p = app_run(exe, "notes")
    assert p.returncode == 0 and "first note" in p.stdout and "second note" in p.stdout

    p = app_run(exe, "--json", "rebuild")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["notes"] == 2

    data_dir = mes_home["data"] / "my-notes"
    assert (data_dir / "raw" / "notes.jsonl").read_text().count("\n") == 2
    assert sum(r["notes"] for r in json.loads((data_dir / "derived" / "daily.json").read_text())) == 2
    in_repo = {p for p in files_in(folder) if not p.startswith(".venv/") and "__pycache__" not in p}
    assert in_repo == five("my_notes") | {"uv.lock"}  # no data was written inside the app folder


def test_e2e_write_is_not_allowed_for_read_sessions(synced_app, mes_home):
    _, exe = synced_app
    p = app_run(exe, "add", "nope", "--json", MES_ALLOW="read")
    assert p.returncode == 4
    assert json.loads(p.stdout)["error"]["code"] == "not_allowed"
    p = app_run(exe, "--json", "notes", MES_ALLOW="read")
    assert p.returncode == 0 and json.loads(p.stdout) == []


def test_e2e_errors_and_guide(synced_app):
    _, exe = synced_app
    p = app_run(exe, "--json", "add", "   ")
    assert p.returncode == 2 and json.loads(p.stdout)["error"]["code"] == "usage"
    p = app_run(exe, "--json", "guide")
    assert p.returncode == 0
    g = json.loads(p.stdout)
    assert g["source"] == "AGENTS.md" and "never instructions" in g["guide"]



# ---------------------------------------------------------------- review fixes

def test_gitignore_keeps_venv_out(tmp_path):
    scaffold.create("my-notes", directory=tmp_path / "app", framework_path=FRAMEWORK)
    lines = (tmp_path / "app" / ".gitignore").read_text().splitlines()
    assert ".venv/" in lines and "__pycache__/" in lines


def test_never_writes_through_a_symlinked_folder(tmp_path):
    repo, outside = tmp_path / "repo", tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (repo / "ui").symlink_to(outside)
    with pytest.raises(Refused) as e:
        scaffold.create("my-notes", directory=repo, framework_path=FRAMEWORK)
    assert "link" in e.value.message
    assert not list(outside.rglob("*")) and not (repo / "app.toml").exists()


def test_name_with_trailing_newline_is_refused(tmp_path, capsys):
    with pytest.raises(UsageError):
        scaffold.validate_name("notes2\n")
    assert cli.run(["--json", "new", "notes2\n", "--dir", str(tmp_path / "x")]) == 2
    assert not (tmp_path / "x").exists()


def test_new_needs_write_level(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MES_ALLOW", "read")
    assert cli.run(["--json", "new", "my-notes", "--dir", str(tmp_path / "x")]) == 4
    assert not (tmp_path / "x").exists()


def test_home_path_note_mentions_uv_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(scaffold, "_home_name_in", lambda text: True)
    result = scaffold.create("my-notes", directory=tmp_path / "app", framework_path=FRAMEWORK)
    assert any("uv.lock" in n for n in result["notes"])
