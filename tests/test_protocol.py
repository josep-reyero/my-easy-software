"""Tests for mes.protocol, mes.errors, mes.paths, mes.guide and the mes CLI shell."""

import datetime as dt
import io
import json
from pathlib import Path

import pytest

from mes import cli, guide, paths, protocol
from mes.errors import (
    ConfigError,
    ExitCode,
    MesError,
    NeedsPerson,
    NotAllowed,
    NotFound,
    Refused,
    TemporaryFailure,
    UsageError,
)


def _question():
    return protocol.needs_input(
        id="tailscale",
        question="Install Tailscale now?",
        why="It keeps your apps private to your own devices.",
        options=[
            protocol.option("yes", "Install it", "Your phone can reach your apps."),
            protocol.option("no", "Not now", "Apps work only on this computer."),
        ],
        recommended="yes",
        rerun="mes setup --answer tailscale=<value>",
    )


# --------------------------------------------------------------------------- builders


def test_needs_input_shape_and_exit_code():
    q = _question()
    assert q["status"] == "needs_input"
    assert set(q) == {"status", "id", "question", "why", "options", "recommended", "rerun"}
    assert q["options"][0] == {"value": "yes", "label": "Install it", "meaning": "Your phone can reach your apps."}
    assert protocol.is_needs(q)
    assert protocol.exit_code_for(q) == ExitCode.NEEDS_HUMAN == 6


def test_needs_input_rejects_bad_recommended():
    with pytest.raises(ValueError):
        protocol.needs_input("x", "q?", "why", [protocol.option("a", "A", "means a")], "b", "rerun")
    with pytest.raises(ValueError):
        protocol.needs_input("x", "q?", "why", [], "a", "rerun")


def test_needs_human_shape():
    h = protocol.needs_human("login", "Sign in to Tailscale", "Only you can sign in.", "mes setup", url="https://example.com")
    assert h == {
        "status": "needs_human",
        "id": "login",
        "action": "Sign in to Tailscale",
        "why": "Only you can sign in.",
        "url": "https://example.com",
        "rerun": "mes setup",
    }
    assert protocol.needs_human("a", "b", "c", "d")["url"] is None
    assert protocol.exit_code_for(h) == 6


def test_checklist_and_exit_codes():
    done = protocol.item("uv", "uv is installed", "done", "mes needs uv to run apps.")
    todo = protocol.item("dirs", "Folders exist", "todo", "Apps keep data there.", fix="mes setup --fix dirs")
    human = protocol.item("account", "Tailscale account", "todo", "Only you can create it.", needs_human=True)
    skipped = protocol.item("share", "Sharing", "skipped", "Not available yet.")
    assert set(done) == {"id", "title", "status", "why", "fix", "needs_human", "detail"}
    ok = protocol.checklist([done, skipped])
    assert ok["ok"] is True and protocol.is_checklist(ok) and protocol.exit_code_for(ok) == 0
    assert protocol.exit_code_for(protocol.checklist([done, todo])) == 1
    assert protocol.exit_code_for(protocol.checklist([done, todo, human])) == 6
    with pytest.raises(ValueError):
        protocol.item("x", "x", "maybe", "why")


def test_plain_data_exit_code_zero():
    assert protocol.exit_code_for({"a": 1}) == 0
    assert protocol.exit_code_for([1, 2]) == 0
    assert protocol.exit_code_for(None) == 0


# --------------------------------------------------------------------------- output


def test_pop_json_flag():
    assert protocol.pop_json_flag(["--json", "x", "--json"]) == (["x"], True)
    assert protocol.pop_json_flag(["x", "--", "--json"]) == (["x", "--", "--json"], False)
    assert protocol.pop_json_flag([]) == ([], False)


def test_to_json_converts_common_types():
    data = {"p": Path("/tmp/x"), "d": dt.date(2026, 1, 2), "s": {"b", "a"}, "t": (1, 2)}
    assert json.loads(protocol.to_json(data)) == {"p": "/tmp/x", "d": "2026-01-02", "s": ["a", "b"], "t": [1, 2]}


def test_format_text_table_and_dict():
    table = protocol.format_text([{"day": "2026-01-01", "steps": 100}, {"day": "2026-01-02", "steps": None}])
    lines = table.splitlines()
    assert lines[0].split() == ["day", "steps"]
    assert lines[3].split() == ["2026-01-02", "-"]
    text = protocol.format_text({"name": "x", "ok": True, "nested": {"a": 1}, "tags": ["a", "b"]})
    assert "name: x" in text and "ok: yes" in text and "nested:" in text and "  a: 1" in text
    assert "tags: a, b" in text
    assert protocol.format_text([]) == "Nothing to show yet."
    assert protocol.format_text(None) == ""
    assert protocol.format_text("hello") == "hello"


def test_format_text_protocol_payloads():
    text = protocol.format_text(_question())
    assert "Install Tailscale now?" in text and "(recommended)" in text and "mes setup --answer" in text
    cl = protocol.checklist([protocol.item("a", "Thing A", "todo", "It matters.", fix="mes fix-a", needs_human=True)])
    text = protocol.format_text(cl)
    assert "[todo] Thing A" in text and "mes fix-a" in text and "needs you" in text and "Not ready" in text


def test_emit_json_and_text():
    out = io.StringIO()
    assert protocol.emit({"a": 1}, json_mode=True, out=out) == 0
    assert json.loads(out.getvalue()) == {"a": 1}
    out = io.StringIO()
    assert protocol.emit(_question(), json_mode=False, out=out) == 6
    assert "A decision is needed" in out.getvalue()


def test_emit_error_json_goes_to_stdout():
    out, err = io.StringIO(), io.StringIO()
    code = protocol.emit_error(NotFound("No such day.", hint="Pick another day."), json_mode=True, out=out, err_out=err)
    assert code == 3
    assert json.loads(out.getvalue()) == {"error": {"code": "not_found", "message": "No such day.", "hint": "Pick another day."}}
    assert err.getvalue() == ""


def test_emit_error_text_goes_to_stderr():
    out, err = io.StringIO(), io.StringIO()
    code = protocol.emit_error(Refused("Not safe."), json_mode=False, out=out, err_out=err)
    assert code == 5 and out.getvalue() == "" and "Not safe." in err.getvalue()


def test_run_and_emit_handles_all_outcomes(capsys):
    assert protocol.run_and_emit(lambda: {"x": 1}, json_mode=True) == 0
    assert json.loads(capsys.readouterr().out) == {"x": 1}

    def boom():
        raise RuntimeError("secret detail")

    assert protocol.run_and_emit(boom, json_mode=True) == 1
    body = json.loads(capsys.readouterr().out)["error"]
    assert body["code"] == "internal" and "detail" not in body  # no exception text without MES_DEBUG

    def needs():
        raise NeedsPerson(_question())

    assert protocol.run_and_emit(needs, json_mode=True) == 6
    assert json.loads(capsys.readouterr().out)["status"] == "needs_input"

    def retry():
        raise TemporaryFailure("The service is busy.", hint="Try again in a few minutes.")

    assert protocol.run_and_emit(retry, json_mode=True) == 75


def test_error_classes_exit_codes():
    assert [c("m").exit_code for c in (MesError, UsageError, NotFound, NotAllowed, Refused, TemporaryFailure)] == [
        1, 2, 3, 4, 5, 75,
    ]
    with pytest.raises(ValueError):
        NeedsPerson({"status": "other"})


# --------------------------------------------------------------------------- paths


def test_paths_follow_env(mes_home):
    assert paths.data_home() == mes_home["data"]
    assert paths.app_data_dir("myapp") == mes_home["data"] / "myapp"
    assert not (mes_home["data"] / "myapp").exists()
    d = paths.app_state_dir("myapp", create=True)
    assert d == mes_home["state"] / "myapp" and d.is_dir()
    assert paths.config_path() == mes_home["config"]


def test_paths_defaults_without_env(monkeypatch, tmp_path):
    for var in ("MES_DATA_HOME", "MES_STATE_HOME", "MES_CONFIG", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.data_home() == tmp_path / ".local/share/mes"
    assert paths.state_home() == tmp_path / ".local/state/mes"
    assert paths.config_path() == tmp_path / ".config/mes/config.toml"


def test_paths_reject_bad_app_names():
    with pytest.raises(UsageError):
        paths.app_data_dir("../escape")


def test_config_loading(mes_home):
    assert paths.load_config() == {}
    mes_home["config"].parent.mkdir(parents=True)
    mes_home["config"].write_text('[apps.myapp]\nunits = "metric"\n')
    assert paths.app_config("myapp") == {"units": "metric"}
    assert paths.app_config("other") == {}
    mes_home["config"].write_text("[broken")
    with pytest.raises(ConfigError):
        paths.load_config()


def test_project_root_and_is_inside(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    assert paths.project_root(tmp_path / "src" / "pkg") == tmp_path.resolve()
    assert paths.is_inside(tmp_path / "src", tmp_path)
    assert not paths.is_inside(tmp_path, tmp_path / "src")


# --------------------------------------------------------------------------- guide + cli


def test_guide_topics():
    names = [t["name"] for t in guide.topics()]
    assert {"setup", "new-app", "jobs", "security"} <= set(names)
    assert all(t["summary"] for t in guide.topics())
    assert "checklist" in guide.read_topic("setup")
    with pytest.raises(NotFound):
        guide.read_topic("../setup")


def test_cli_help_and_version(capsys):
    assert cli.run(["--help"]) == 0
    assert "guide" in capsys.readouterr().out
    assert cli.run(["--version"]) == 0
    assert "mes 0.1.0" in capsys.readouterr().out


def test_cli_no_command_json_lists_commands(capsys):
    assert cli.run(["--json"]) == 0
    names = {c["name"] for c in json.loads(capsys.readouterr().out)["commands"]}
    assert {"guide", "dev", "deploy", "expose"} <= names


def test_cli_guide_json_after_command(capsys):
    assert cli.run(["guide", "jobs", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["topic"] == "jobs" and "[jobs.sync]" in data["text"]
    assert cli.run(["guide", "nope", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"


@pytest.mark.parametrize("name", ["dev", "deploy", "expose"])
def test_cli_not_available_yet(capsys, name):
    assert cli.run(["--json", name, "my-server", "--anything"]) == 1
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"] == "not_available_yet" and "not available yet" in err["message"] and err["hint"]


def test_cli_usage_error_json(capsys):
    assert cli.run(["--json", "nope"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"
    assert cli.run(["guide", "a", "b"]) == 2
    assert "does not know these words: b" in capsys.readouterr().err


def test_cli_skips_missing_modules(monkeypatch):
    monkeypatch.setattr(cli, "COMMAND_MODULES", ("mes.does_not_exist", "mes.guide"))
    parser, loaded = cli.build_parser()
    assert loaded == ["mes.guide"]


# ---------------------------------------------------------------- review fixes

def test_internal_error_detail_only_with_debug(capsys, monkeypatch):
    def boom():
        raise FileNotFoundError("/home/owner/secret-path/token?key=SECRET")

    monkeypatch.setenv("MES_DEBUG", "1")
    assert protocol.run_and_emit(boom, json_mode=True) == 1
    assert "SECRET" in json.loads(capsys.readouterr().out)["error"]["detail"]
    monkeypatch.setenv("MES_VIA_GATE", "1")  # through the gate: never, and no MES_DEBUG hint
    assert protocol.run_and_emit(boom, json_mode=True) == 1
    captured = capsys.readouterr()
    body = json.loads(captured.out)["error"]
    assert "detail" not in body and "SECRET" not in captured.out + captured.err
    assert "MES_DEBUG" not in body["hint"]


def test_text_output_neutralises_terminal_escapes(capsys):
    rows = [{"note": "hi\x1b]0;pwned\x07\x1b[2J", "day": "x"}]
    protocol.emit(rows, json_mode=False)
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "\\x1b" in out
    err = io.StringIO()
    protocol.emit_error(MesError("bad \x1b[2J thing", hint="x\x9b"), json_mode=False, err_out=err)
    assert "\x1b" not in err.getvalue() and "\x9b" not in err.getvalue()
    assert protocol.safe_text("a\tb\nc") == "a\tb\nc"


def test_skipped_items_say_why_and_ready_line_says_so():
    cl = protocol.checklist([
        protocol.item("a", "Thing A", "done", "fine"),
        protocol.item("b", "Private network", "skipped", "You chose local-only. Your phone cannot reach apps."),
    ])
    text = protocol.format_text(cl)
    assert "[skipped] Private network: You chose local-only. Your phone cannot reach apps." in text
    assert "Everything is ready." not in text and "except for the steps marked [skipped]" in text


def test_fix_is_offered_by_the_assistant_not_typed_by_the_person():
    cl = protocol.checklist([protocol.item("t", "Tool", "todo", "Missing.", fix="brew install x")])
    text = protocol.format_text(cl)
    assert "Your assistant can do this for you after you say yes (command: brew install x)." in text
    assert "To fix it, run" not in text


def test_detail_label_is_always_shown_in_text():
    err = io.StringIO()
    e = MesError("The job failed.", hint="Fix it.", detail="the app said this", detail_label="What the app said:")
    protocol.emit_error(e, json_mode=False, err_out=err)
    assert "What the app said:\nthe app said this" in err.getvalue()
