import datetime as dt
import json
from pathlib import Path
from typing import Annotated, Literal

import pytest

from mes import App, protocol
from mes.app import allowed_level, check_allowed
from mes.errors import NotAllowed, NotFound, Refused

import projects as helpers


def make_app(root):
    app = App("myapp", root=root)

    @app.command("summary", kind="read", help="Key numbers for the last days.")
    def summary(ctx, days: int = 7):
        return {"days": days, "json": ctx.json}

    @app.command(kind="read")
    def day(ctx, date: dt.date, source: Literal["sleep", "steps"] = "steps"):
        """One day of data."""
        if date.year < 2000:
            raise NotFound("There is no data for that day.", hint="Pick a day after 2000.")
        return [{"date": date, "source": source, "value": 1}]

    @app.command("add-note", kind="write", help="Add a note.")
    def add_note(ctx, text: str, tags: list[str] | None = None, pinned: bool = False):
        path = ctx.data_dir / "notes.txt"
        path.write_text(text)
        return {"saved": path, "tags": tags, "pinned": pinned}

    @app.command("wipe", kind="admin", help="Remove everything.")
    def wipe(ctx, *, confirm: Annotated[str, "type the app name"]):
        raise Refused("Wiping is not allowed in tests.")

    @app.command("ask", kind="read")
    def ask(ctx):
        return protocol.needs_human("login", "Sign in", "Only you can.", "myapp ask")

    @app.command("crash", kind="read")
    def crash(ctx):
        raise RuntimeError("boom")

    @app.schema
    def schema(ctx):
        return {"tables": {"days": ["date", "value"]}}

    return app


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


def run_json(app, capsys, *argv):
    code = app.run(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_option_defaults_and_values(app, capsys):
    assert run_json(app, capsys, "--json", "summary") == (0, {"days": 7, "json": True})
    assert run_json(app, capsys, "summary", "--days", "3", "--json") == (0, {"days": 3, "json": True})


def test_text_output_without_json(app, capsys):
    assert app.run(["summary"]) == 0
    out = capsys.readouterr().out
    assert "days: 7" in out and "json: no" in out
    assert app.run(["day", "2026-01-02"]) == 0
    assert "2026-01-02" in capsys.readouterr().out


def test_positional_types_and_choices(app, capsys):
    code, data = run_json(app, capsys, "--json", "day", "2026-01-02", "--source", "sleep")
    assert code == 0 and data == [{"date": "2026-01-02", "source": "sleep", "value": 1}]
    code, data = run_json(app, capsys, "--json", "day", "2026-01-02", "--source", "weight")
    assert code == 2 and data["error"]["code"] == "usage"
    code, data = run_json(app, capsys, "--json", "day", "not-a-date")
    assert code == 2 and "date" in data["error"]["message"]


def test_usage_error_text_goes_to_stderr(app, capsys):
    assert app.run(["summary", "--days", "many"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "--help" in captured.err
    assert '--days needs a whole number such as 5, but got "many".' in captured.err


def test_not_found_error_json(app, capsys):
    code, data = run_json(app, capsys, "--json", "day", "1990-01-01")
    assert code == 3
    assert data == {"error": {"code": "not_found", "message": "There is no data for that day.", "hint": "Pick a day after 2000."}}


def test_write_command_uses_data_dir(app, capsys, mes_home):
    code, data = run_json(app, capsys, "--json", "add-note", "hello", "--tags", "a", "b", "--pinned")
    assert code == 0
    assert data == {"saved": str(mes_home["data"] / "myapp" / "notes.txt"), "tags": ["a", "b"], "pinned": True}
    assert (mes_home["data"] / "myapp" / "notes.txt").read_text() == "hello"
    code, data = run_json(app, capsys, "--json", "add-note", "x", "--no-pinned")
    assert data["pinned"] is False and data["tags"] is None


def test_allowed_level_blocks_before_running(app, capsys, monkeypatch, mes_home):
    monkeypatch.setenv("MES_ALLOW", "read")
    code, data = run_json(app, capsys, "--json", "add-note", "hello")
    assert code == 4 and data["error"]["code"] == "not_allowed"
    assert not (mes_home["data"] / "myapp").exists()
    assert run_json(app, capsys, "--json", "summary")[0] == 0
    monkeypatch.setenv("MES_ALLOW", "write")
    assert run_json(app, capsys, "--json", "add-note", "hello")[0] == 0
    assert run_json(app, capsys, "--json", "wipe", "--confirm", "myapp")[0] == 4
    monkeypatch.setenv("MES_ALLOW", "root")
    assert run_json(app, capsys, "--json", "summary")[0] == 4


def test_allowed_helpers(monkeypatch):
    assert allowed_level() == "admin"
    check_allowed("admin")
    with pytest.raises(NotAllowed):
        check_allowed("write", "read")


def test_refused_and_required_keyword_option(app, capsys):
    code, data = run_json(app, capsys, "--json", "wipe")
    assert code == 2
    code, data = run_json(app, capsys, "--json", "wipe", "--confirm", "myapp")
    assert code == 5 and data["error"]["code"] == "refused"


def test_needs_human_exit_code(app, capsys):
    code, data = run_json(app, capsys, "--json", "ask")
    assert code == 6 and data["status"] == "needs_human"


def test_unexpected_exception_is_plain_error(app, capsys):
    code, data = run_json(app, capsys, "--json", "crash")
    assert code == 1 and data["error"]["code"] == "internal" and data["error"]["hint"]


def test_describe(app, capsys):
    code, data = run_json(app, capsys, "--json", "describe")
    assert code == 0
    assert data["app"] == "myapp" and data["version"] == "0.0.0" and data["allowed"] == "admin"
    cmds = {c["name"]: c for c in data["commands"]}
    assert {"describe", "guide", "schema", "summary", "day", "add-note", "wipe"} <= set(cmds)
    assert "sources" not in cmds
    assert cmds["add-note"]["kind"] == "write" and cmds["wipe"]["kind"] == "admin"
    assert cmds["day"]["help"] == "One day of data."
    args = {a["name"]: a for a in cmds["add-note"]["args"]}
    assert args["text"] == {
        "name": "text", "flag": None, "kind": "positional", "type": "str", "required": True,
        "default": None, "many": False, "choices": None, "help": None,
    }
    assert args["tags"]["many"] and args["tags"]["flag"] == "--tags"
    assert args["pinned"]["kind"] == "flag" and args["pinned"]["default"] is False
    day_args = {a["name"]: a for a in cmds["day"]["args"]}
    assert day_args["source"]["choices"] == ["sleep", "steps"] and day_args["date"]["type"] == "date"
    wipe_args = cmds["wipe"]["args"]
    assert wipe_args[0]["required"] and wipe_args[0]["help"] == "type the app name"


def test_no_command_json_is_describe(app, capsys):
    code, data = run_json(app, capsys, "--json")
    assert code == 0 and data["app"] == "myapp"
    assert app.run([]) == 0
    assert "summary" in capsys.readouterr().out


def test_schema_provider(app, capsys):
    assert run_json(app, capsys, "--json", "schema") == (0, {"tables": {"days": ["date", "value"]}})


def test_guide_generated_and_from_agents_md(app, tmp_path, capsys):
    code, data = run_json(app, capsys, "--json", "guide")
    assert code == 0 and data["source"] == "generated" and "`summary` (read)" in data["guide"]
    (tmp_path / "AGENTS.md").write_text("# Use myapp carefully\n")
    code, data = run_json(app, capsys, "--json", "guide")
    assert data == {"app": "myapp", "source": "AGENTS.md", "guide": "# Use myapp carefully\n"}
    assert app.run(["guide"]) == 0
    assert capsys.readouterr().out == "# Use myapp carefully\n"


def _in_project(tmp_path, project='[project]\nname = "myproj"\napps = ["myapp"]\n', app_toml=None, more=None):
    apps = {"myapp": app_toml or '[app]\nname = "myapp"\ncommand = "myapp"\n', **(more or {})}
    helpers.write(tmp_path, project, apps)
    return make_app(tmp_path)


def test_manifest_gives_title_version_and_jobs(tmp_path, capsys):
    app = _in_project(tmp_path, '[project]\nname = "myproj"\ntitle = "My project"\nversion = "1.2.3"\napps = ["myapp"]\n',
                      '[app]\nname = "myapp"\ntitle = "My App"\nversion = "2.0.0"\ncommand = "myapp"\n'
                      '[jobs.sync]\nrun = "summary --days 1"\nschedule = "daily"\n')
    code, data = run_json(app, capsys, "--json", "describe")
    assert code == 0, data
    assert data["title"] == "My App" and data["version"] == "2.0.0" and data["project"] == "myproj"
    assert data["short"] == "myapp"
    job = data["jobs"][0]
    assert job["id"] == "myapp.sync" and job["name"] == "sync" and job["app"] == "myapp"
    assert job["argv"] == ["summary", "--days", "1"]
    assert app.run(["describe"]) == 0
    assert "myapp.sync" in capsys.readouterr().out


def test_app_not_in_project_is_an_error(tmp_path, capsys):
    helpers.write(tmp_path, '[project]\nname = "other"\napps = ["other_x"]\n',
                  {"other_x": '[app]\nname = "other-x"\ncommand = "other-x"\n'})
    app = make_app(tmp_path)
    code, data = run_json(app, capsys, "--json", "describe")
    assert code == 1 and data["error"]["code"] == "manifest_invalid"
    assert "myapp" in data["error"]["message"]


def test_one_of_several_apps_sees_only_its_jobs(tmp_path, capsys, mes_home):
    app = _in_project(tmp_path, '[project]\nname = "fitness"\napps = ["myapp", "fitness_other"]\n',
                      '[app]\nname = "myapp"\ncommand = "myapp"\n[jobs.mine]\nrun = "summary"\nmanual = true\n',
                      {"fitness_other": '[app]\nname = "fitness-other"\ncommand = "fitness-other"\n'
                                        '[jobs.theirs]\nrun = "sync"\nmanual = true\n'})
    code, data = run_json(app, capsys, "--json", "describe")
    assert code == 0 and data["app"] == "myapp" and data["project"] == "fitness"
    assert [j["id"] for j in data["jobs"]] == ["myapp.mine"]
    # The data folder belongs to the project; each app also has its own folder inside it.
    code, data = run_json(app, capsys, "--json", "add-note", "hi")
    assert code == 0 and data["saved"] == str(mes_home["data"] / "fitness" / "notes.txt")


def test_app_data_dir_and_shared_database(tmp_path, capsys, mes_home):
    app = _in_project(tmp_path, '[project]\nname = "fitness"\napps = ["myapp"]\n[tables.days]\nkey = ["date"]\n'
                      'columns = { date = "text", n = "integer" }\n',
                      '[app]\nname = "myapp"\ncommand = "myapp"\n[data]\nwrites = ["days"]\n')

    @app.command("fill", kind="write")
    def fill(ctx):
        with ctx.db() as db:
            db.replace_rows("days", [{"date": "2026-01-01", "n": 1}])
        return {"folder": str(ctx.app_data_dir), "db": str(ctx.db_path)}

    code, data = run_json(app, capsys, "--json", "fill")
    assert code == 0, data
    assert data["folder"] == str(mes_home["data"] / "fitness" / "myapp")
    assert data["db"] == str(mes_home["data"] / "fitness" / "fitness.sqlite")
    import sqlite3

    rows = sqlite3.connect(data["db"]).execute("SELECT source, date, n FROM days").fetchall()
    assert rows == [("myapp", "2026-01-01", 1)]


def test_mes_project_mismatch_is_refused(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MES_PROJECT", "someone-else")
    app = _in_project(tmp_path)
    code, data = run_json(app, capsys, "--json", "describe")
    assert code != 0 and data["error"]["code"] == "wrong_project"


def test_session_dirs_only_for_own_project(tmp_path, capsys, monkeypatch, mes_home):
    own = tmp_path / "own-data"
    own.mkdir()
    monkeypatch.setenv("MES_PROJECT", "myproj")
    monkeypatch.setenv("MES_DATA_DIR", str(own))
    app = _in_project(tmp_path)
    code, data = run_json(app, capsys, "--json", "add-note", "x")
    assert code == 0 and data["saved"] == str(own / "notes.txt")


def test_data_dir_override_from_machine_config(tmp_path, capsys, mes_home, monkeypatch):
    from mes import paths
    monkeypatch.delenv("MES_PROJECT", raising=False)
    monkeypatch.delenv("MES_DATA_DIR", raising=False)
    elsewhere = tmp_path / "big-disk" / "myapp"
    mes_home["config"].parent.mkdir(parents=True)
    mes_home["config"].write_text(f'[projects.myapp]\ndata_dir = "{elsewhere}"\n')
    assert paths.project_data_dir("myapp") == elsewhere
    assert paths.project_store_dir("myapp") == elsewhere / "store"
    app = make_app(tmp_path)
    code, data = run_json(app, capsys, "--json", "add-note", "x")
    assert code == 0 and data["saved"] == str(elsewhere / "notes.txt")


def test_root_discovered_from_caller_file(monkeypatch):
    monkeypatch.delenv("MES_PROJECT_DIR", raising=False)
    app = App("myapp")  # tests/ is in no project and no app folder
    assert app.app_dir is None


def test_app_finds_its_folder_and_project_from_its_file(tmp_path, monkeypatch):
    monkeypatch.delenv("MES_PROJECT_DIR", raising=False)
    helpers.write(tmp_path, '[project]\nname = "myproj"\napps = ["myapp"]\n',
                  {"myapp": '[app]\nname = "myapp"\ncommand = "myapp"\n'})
    code = compile('from mes import App\napp = App("myapp")\n', str(tmp_path / "apps" / "myapp" / "cli.py"), "exec")
    ns = {"__file__": str(tmp_path / "apps" / "myapp" / "cli.py")}
    exec(code, ns)
    assert ns["app"].app_dir == (tmp_path / "apps" / "myapp").resolve() and ns["app"].root == tmp_path.resolve()


def test_registration_errors(tmp_path):
    app = App("myapp", root=tmp_path)
    with pytest.raises(ValueError):
        app.command("describe")(lambda ctx: None)
    with pytest.raises(ValueError):
        app.command("x", kind="root")
    app.command("dup")(lambda ctx: None)
    with pytest.raises(ValueError):
        app.command("dup")(lambda ctx: None)
    with pytest.raises(TypeError):
        app.command("bad")(lambda: None)
    with pytest.raises(TypeError):
        app.command("bad2")(lambda ctx, json=False: None)
    with pytest.raises(ValueError):
        App("Bad Name")


def test_bare_decorator_uses_function_name(tmp_path, capsys):
    app = App("myapp", root=tmp_path)

    @app.command
    def last_run(ctx):
        return "never"

    assert app.run(["last-run"]) == 0
    assert capsys.readouterr().out == "never\n"


def test_context_config(tmp_path, capsys, mes_home):
    mes_home["config"].parent.mkdir(parents=True)
    mes_home["config"].write_text('[projects.myapp]\nunits = "metric"\n')
    app = App("myapp", root=tmp_path)

    @app.command("units")
    def units(ctx):
        return ctx.config

    assert run_json(app, capsys, "--json", "units") == (0, {"units": "metric"})


def test_broken_manifest_only_blocks_commands_that_need_it(tmp_path, capsys):
    helpers.write(tmp_path, '[project]\nname = "myproj"\napps = ["myapp"]\ncolour = "red"\n',
                  {"myapp": '[app]\nname = "myapp"\ncommand = "myapp"\n'})
    app = make_app(tmp_path)
    assert run_json(app, capsys, "--json", "summary") == (0, {"days": 7, "json": True})
    code, data = run_json(app, capsys, "--json", "describe")
    assert code == 1 and '"colour"' in data["error"]["message"]


def test_subcommand_help(app, capsys):
    assert app.run(["add-note", "--help"]) == 0
    out = capsys.readouterr().out
    assert "--pinned" in out and "--no-pinned" in out and "--json" in out


# ---------------------------------------------------------------- review fixes

@pytest.mark.parametrize("value", ["", "  ", "\t"])
def test_blank_mes_allow_fails_closed(app, capsys, monkeypatch, value):
    monkeypatch.setenv("MES_ALLOW", value)
    with pytest.raises(NotAllowed):
        allowed_level()
    code, data = run_json(app, capsys, "--json", "summary")
    assert code == 4 and data["error"]["code"] == "not_allowed"


def test_missing_mes_allow_is_admin(monkeypatch):
    monkeypatch.delenv("MES_ALLOW", raising=False)
    assert allowed_level() == "admin"


def test_plain_usage_errors(app, capsys):
    code, data = run_json(app, capsys, "--json", "nope")
    assert code == 2
    msg = data["error"]["message"]
    assert msg.startswith('myapp has no command called "nope". Its commands are: ') and "summary" in msg
    code, data = run_json(app, capsys, "--json", "add-note")
    assert code == 2 and "needs more information that was not given: text" in data["error"]["message"]


def test_help_and_json_gives_json(app, capsys):
    for argv in (["--help", "--json"], ["summary", "--help", "--json"]):
        code = app.run(argv)
        out = capsys.readouterr().out
        assert code == 0 and "usage:" in json.loads(out)["help"]


def test_describe_text_is_a_table(app, capsys, monkeypatch):
    monkeypatch.setenv("MES_ALLOW", "read")
    assert app.run(["describe"]) == 0
    out = capsys.readouterr().out
    assert "This session may run only read commands" in out
    assert "command" in out and "kind" in out and "summary" in out and '{"name"' not in out


def test_command_name_with_newline_is_refused(app):
    with pytest.raises(ValueError):
        app.command("bad\n")(lambda ctx: None)
    with pytest.raises(ValueError):
        App("myapp\n", root=".")


def test_usage_hints_use_the_name_the_project_command_reached_the_app_by(app, capsys, monkeypatch):
    monkeypatch.setenv("MES_COMMAND_NAME", "myapp myapp")
    assert app.command_name == "myapp myapp"
    assert app.run(["summary", "--days", "many"]) == 2
    assert "Run 'myapp myapp summary --help'" in capsys.readouterr().err
    for other in ("other myapp", "myapp other", "myapp myapp; rm", "myapp"):
        monkeypatch.setenv("MES_COMMAND_NAME", other)
        assert app.command_name == "myapp"  # only a name that is this app's is used


# --------------------------------------------------------------------------- audit F11


def test_a_parameter_converter_never_runs_for_a_command_that_is_not_allowed(tmp_path, capsys, monkeypatch):
    """Audit F11: the converter of a write command's parameter ran before the kind check."""
    ran = []

    def spy(text):
        ran.append(text)
        return text

    app = App("myapp", root=tmp_path)

    @app.command("change", kind="write")
    def change(ctx, value: Annotated[spy, "a value"]):  # noqa: F821 - the converter is the type
        return {"value": value}

    monkeypatch.setenv("MES_ALLOW", "read")
    code, data = run_json(app, capsys, "--json", "change", "x")
    assert code == 4 and data["error"]["code"] == "not_allowed" and ran == []
    monkeypatch.setenv("MES_ALLOW", "write")
    code, data = run_json(app, capsys, "--json", "change", "x")
    assert code == 0 and ran == ["x"]


def test_without_project_toml_the_session_cannot_rename_the_project(tmp_path, capsys, monkeypatch, mes_home):
    """Audit F11: an app without project.toml took its project from MES_PROJECT, and with it
    another project's data folder."""
    other = tmp_path / "other-data"
    other.mkdir()
    monkeypatch.setenv("MES_PROJECT", "other")
    monkeypatch.setenv("MES_DATA_DIR", str(other))
    app = make_app(tmp_path)
    assert app.project == "myapp"
    code, data = run_json(app, capsys, "--json", "add-note", "x")
    assert code == 2 and data["error"]["code"] == "wrong_project"  # refused, not written into "other"
    assert list(other.iterdir()) == []


def test_help_text_cannot_hide_or_reorder_text(tmp_path, capsys):
    app = App("myapp", root=tmp_path)

    @app.command("trick", kind="read", help="safe‮ looking \x1b[2J text")
    def trick(ctx):
        return {"note": "a‮b\u0085"}

    code = app.run(["--json"])
    out = capsys.readouterr().out
    assert "‮" not in out and "\x1b" not in out and "\\u202e" in out
    code = app.run(["--help"])
    out = capsys.readouterr().out
    assert "‮" not in out and "\x1b" not in out
    code = app.run(["--json", "trick"])
    out = capsys.readouterr().out
    assert code == 0 and "‮" not in out and "\u0085" not in out and json.loads(out) == {"note": "a‮b\u0085"}
