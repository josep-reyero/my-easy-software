"""The project's shared database (mes.db): the schema script, keeping it in line with
project.toml, the helper apps write through (ctx.db()), and ``mes db check``."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import projects as helpers
from mes import cli, db, manifest
from mes.errors import MesError, UsageError

FITLAB = Path(__file__).resolve().parents[1] / "examples" / "fitlab"
GOLDEN = Path(__file__).parent / "golden" / "fitlab-schema.sql"

PROJECT = """
[project]
name = "pp"
apps = ["pp_a", "pp_b"]

[tables.days]
key = ["date"]
columns = { date = "text", steps = "integer", ok = "boolean", extra = "json", km = "real" }

[tables.things]
key = ["id"]
prefer = ["pp-b"]
columns = { id = "text", a = "text", b = "text" }

[views.busy]
sql = "SELECT date FROM days WHERE steps > 1000"
"""

A = '[app]\nname = "pp-a"\ncommand = "pp-a"\n[data]\nwrites = ["days", "things"]\ntables = ["a_own"]\n'
B = '[app]\nname = "pp-b"\ncommand = "pp-b"\n[data]\nwrites = ["things"]\n'


@pytest.fixture
def proj(tmp_path):
    return helpers.load(tmp_path / "pp", PROJECT, {"pp_a": A, "pp_b": B})


def _rows(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- the schema script


def test_fitlab_schema_is_the_golden_text(tmp_path):
    """examples/fitlab with a hand-written lock (rides is 'installed' from vendor/), so no uv is needed."""
    import hashlib
    import shutil

    root = tmp_path / "fitlab"
    shutil.copytree(FITLAB, root, ignore=shutil.ignore_patterns(".venv", "apps.lock.json", "uv.lock"))
    rides = (root / "vendor" / "fitlab-rides" / "src" / "fitlab_rides").resolve()
    lock = {"v": 1, "project": "fitlab",
            "project_toml_sha256": hashlib.sha256((root / "project.toml").read_bytes()).hexdigest(),
            "apps": [{"package": "fitlab_walks", "name": "fitlab-walks", "path": str((root / "apps" / "fitlab_walks").resolve()),
                      "in_project": True, "program": "fitlab-walks"},
                     {"package": "fitlab_rides", "name": "fitlab-rides", "path": str(rides), "in_project": False,
                      "program": "fitlab-rides"}]}
    (root / "apps.lock.json").write_text(json.dumps(lock))
    project = manifest.load(root)
    assert db.schema_sql(project) == GOLDEN.read_text()
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.schema_sql(project))  # the script runs as it is
    assert project.allowlist() == ["activities", "days", "weekly", "walks_log"]


def test_schema_sql_runs_and_views_read_the_rows(proj, tmp_path):
    script = db.schema_sql(proj)
    assert 'CREATE TABLE IF NOT EXISTS "days__rows"' in script and 'CREATE VIEW "days"' in script
    info = db.schema_info(proj)
    assert info["allowlist"] == ["days", "things", "busy"]


# --------------------------------------------------------------------------- writing rows


def test_replace_rows_and_the_union_view(proj, tmp_path):
    path = tmp_path / "pp.sqlite"
    with db.open_app_db(proj, proj.apps["pp-a"], path=path) as d:
        assert d.replace_rows("days", [{"date": "2026-01-01", "steps": 2000, "ok": True, "extra": {"x": [1]},
                                        "km": 1.5}, {"date": "2026-01-02", "steps": 10}]) == 2
    assert _rows(path, "SELECT source, date, steps, ok, extra, km FROM days ORDER BY date") == [
        ("pp-a", "2026-01-01", 2000, 1, '{"x":[1]}', 1.5), ("pp-a", "2026-01-02", 10, None, None, None)]
    assert _rows(path, "SELECT * FROM busy") == [("2026-01-01",)]
    with db.open_app_db(proj, proj.apps["pp-a"], path=path) as d:
        d.replace_rows("days", [{"date": "2026-01-03"}])
    assert _rows(path, "SELECT date FROM days") == [("2026-01-03",)]
    with db.open_app_db(proj, proj.apps["pp-a"], path=path) as d:
        assert d.upsert_rows("days", [{"date": "2026-01-03", "steps": 5}, {"date": "2026-01-04"}]) == 2
        assert d.delete_rows("days", {"date": "2026-01-04"}) == 1
    assert _rows(path, "SELECT date, steps FROM days") == [("2026-01-03", 5)]
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_merged_view_prefers_and_keeps_other_apps_rows(proj, tmp_path):
    path = tmp_path / "pp.sqlite"
    with db.open_app_db(proj, proj.apps["pp-a"], path=path) as d:
        d.replace_rows("things", [{"id": "1", "a": "from a", "b": "a's b"}, {"id": "2", "a": "only a"}])
    with db.open_app_db(proj, proj.apps["pp-b"], path=path) as d:
        d.replace_rows("things", [{"id": "1", "b": "b's b"}])
    # One row per key; pp-b is preferred, and a's values fill what b leaves empty.
    assert _rows(path, "SELECT id, a, b, source FROM things ORDER BY id") == [
        ("1", "from a", "b's b", "pp-b"), ("2", "only a", None, "pp-a")]
    # Rewriting a's rows leaves b's rows as they are.
    with db.open_app_db(proj, proj.apps["pp-a"], path=path) as d:
        d.replace_rows("things", [])
    assert _rows(path, "SELECT source, id FROM things__rows") == [("pp-b", "1")]


@pytest.mark.parametrize("table, rows, needle", [
    ("days", [{"date": "d", "nope": 1}], 'the column "nope"'),
    ("days", [{"steps": 1}], 'no value for the key column "date"'),
    ("days", [{"date": "d", "source": "pp-b"}], "only rows of pp-a"),
    ("days", [{"date": "d", "steps": [1]}], "cannot hold"),
    ("days", [{"date": "d", "extra": {1, 2}}], "as JSON"),
    ("days", [{"date": "d", "km": True}], "true or false"),
    ("days", [{"date": "d"}, {"date": "d"}], "same key"),
    ("days", ["not a dict"], "not a dict"),
    ("a_own", [{"x": 1}], "does not write this table"),
])
def test_bad_rows_change_nothing(proj, tmp_path, table, rows, needle):
    path = tmp_path / "pp.sqlite"
    with db.open_app_db(proj, proj.apps["pp-a"], path=path) as d:
        d.replace_rows("days", [{"date": "keep"}])
    with pytest.raises(MesError) as e:
        with db.open_app_db(proj, proj.apps["pp-a"], path=path) as d:
            d.replace_rows(table, rows)
    assert e.value.code == "bad_rows" and needle in e.value.message and "Nothing was changed" in e.value.message
    assert _rows(path, "SELECT date FROM days") == [("keep",)]


def test_another_apps_table_cannot_be_written(proj, tmp_path):
    with pytest.raises(MesError) as e:
        with db.open_app_db(proj, proj.apps["pp-b"], path=tmp_path / "pp.sqlite") as d:
            d.replace_rows("days", [{"date": "x"}])
    assert e.value.code == "bad_rows"


def test_read_only_helper_refuses_writes(proj, tmp_path):
    path = tmp_path / "pp.sqlite"
    with db.open_app_db(proj, proj.apps["pp-a"], path=path) as d:
        d.replace_rows("days", [{"date": "x"}])
    with db.open_app_db(proj, proj.apps["pp-a"], path=path, write=False) as d:
        with pytest.raises(UsageError):
            d.replace_rows("days", [])
        with pytest.raises(sqlite3.OperationalError):
            d.conn.execute("DELETE FROM days__rows")


# --------------------------------------------------------------------------- keeping the schema in line


def test_sync_adds_columns_and_recreates_changed_views(proj, tmp_path):
    path = tmp_path / "pp.sqlite"
    db.sync(proj, path=path)
    root = tmp_path / "pp"
    text = PROJECT.replace('km = "real" }', 'km = "real", note = "text" }').replace("> 1000", "> 5")
    (root / "project.toml").write_text(text)
    changed = manifest.load(root)
    out = db.sync(changed, path=path)
    assert out["ok"] if "ok" in out else True
    cols = [r[1] for r in _rows(path, "PRAGMA table_info(days__rows)")]
    assert cols[-1] == "note"
    sql = _rows(path, "SELECT sql FROM sqlite_master WHERE name = 'busy'")[0][0]
    assert "> 5" in sql
    assert db.check(changed, path=path)["ok"]


@pytest.mark.parametrize("change, needle", [
    ('km = "real" }', "removed"),
    ('steps = "integer"', "type"),
])
def test_sync_refuses_changes_that_would_lose_data(proj, tmp_path, change, needle):
    path = tmp_path / "pp.sqlite"
    db.sync(proj, path=path)
    root = tmp_path / "pp"
    if needle == "removed":
        text = PROJECT.replace(', km = "real" }', " }")
    else:
        text = PROJECT.replace('steps = "integer"', 'steps = "text"')
    (root / "project.toml").write_text(text)
    with pytest.raises(db.SchemaError) as e:
        db.sync(manifest.load(root), path=path)
    assert e.value.code == "schema_conflict" and "mes db sync --reset days" in e.value.hint


def test_reset_rebuilds_a_shared_table_but_never_an_apps_table(proj, tmp_path):
    path = tmp_path / "pp.sqlite"
    with db.open_app_db(proj, proj.apps["pp-a"], path=path) as d:
        d.replace_rows("days", [{"date": "x"}])
    root = tmp_path / "pp"
    (root / "project.toml").write_text(PROJECT.replace('steps = "integer"', 'steps = "text"'))
    changed = manifest.load(root)
    db.sync(changed, reset=["days"], path=path)
    assert _rows(path, "SELECT count(*) FROM days") == [(0,)]
    with pytest.raises(MesError) as e:
        db.sync(changed, reset=["a_own"], path=path)
    assert e.value.code == "app_table"


def test_a_table_where_a_view_belongs_is_refused(proj, tmp_path):
    path = tmp_path / "pp.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE days (date TEXT)")  # the old one-file form
    conn.commit()
    conn.close()
    with pytest.raises(db.SchemaError) as e:
        db.sync(proj, path=path)
    assert "days" in e.value.message


def test_check_reports_undeclared_objects_and_orphan_rows(proj, tmp_path):
    path = tmp_path / "pp.sqlite"
    with db.open_app_db(proj, proj.apps["pp-a"], path=path) as d:
        d.replace_rows("days", [{"date": "x"}])
        d.conn.execute("CREATE TABLE a_own (x)")
        d.conn.execute("CREATE TABLE stray (x)")
        d.conn.execute("INSERT INTO days__rows (source, date) VALUES ('gone-app', 'y')")
    out = db.check(proj, path=path)
    assert out["ok"] and out["undeclared"] == ["stray (table)"] and out["orphan_rows"] == {"days": 1}
    assert "mes db check --prune" in out["message"]
    out = db.check(proj, prune=True, path=path)
    assert out["pruned"] == 1 and _rows(path, "SELECT source FROM days__rows") == [("pp-a",)]
    assert db.check(proj, path=tmp_path / "none.sqlite")["exists"] is False


def test_the_database_uses_a_rollback_journal(proj, tmp_path):
    path = tmp_path / "pp.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.close()
    db.sync(proj, path=path)
    assert _rows(path, "PRAGMA journal_mode") == [("delete",)]


# --------------------------------------------------------------------------- the commands


def test_mes_db_commands(proj, tmp_path, capsys, mes_home):
    root = str(tmp_path / "pp")
    assert cli.run(["--json", "db", "schema", "--project-dir", root]) == 0
    assert json.loads(capsys.readouterr().out)["allowlist"] == ["days", "things", "busy"]
    assert cli.run(["db", "schema", "--sql", "--project-dir", root]) == 0
    assert capsys.readouterr().out.startswith(db.schema_sql(proj).splitlines()[0])
    assert cli.run(["--json", "db", "sync", "--project-dir", root]) == 0
    capsys.readouterr()
    assert (mes_home["data"] / "pp" / "pp.sqlite").is_file()
    assert cli.run(["--json", "db", "check", "--project-dir", root]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli.run(["db", "sync", "--quiet", "--project-dir", root]) == 0
    assert capsys.readouterr().out == ""


def test_a_number_column_keeps_the_form_each_value_was_written_in(tmp_path):
    """real turns 144 into 144.0; number keeps 144 and 144.5 as they were written."""
    import sqlite3

    from mes import db as mesdb
    from mes import manifest as m

    pf = m.loads('[project]\nname = "pp"\napps = ["pp_a"]\n[tables.t]\nkey = ["k"]\n'
                 'columns = { k = "text", n = "number", r = "real" }\n')
    tables = pf.tables
    sql = mesdb._create_table(tables["t"])
    assert '"n",' in sql and '"r" REAL,' in sql
    conn = sqlite3.connect(":memory:")
    conn.execute(sql)
    conn.executemany('INSERT INTO "t__rows" ("source", "k", "n", "r") VALUES (?, ?, ?, ?)',
                     [("pp-a", "a", 144, 144), ("pp-a", "b", 144.5, 144.5), ("pp-a", "c", 144.0, 144.0)])
    rows = conn.execute('SELECT n, r FROM "t__rows" ORDER BY k').fetchall()
    assert [type(v) for v in rows[0]] == [int, float]
    assert rows == [(144, 144.0), (144.5, 144.5), (144.0, 144.0)] and type(rows[2][0]) is float


def test_a_type_change_says_both_types_once(proj, tmp_path):
    path = tmp_path / "pp.sqlite"
    db.sync(proj, path=path)
    root = tmp_path / "pp"
    (root / "project.toml").write_text(PROJECT.replace('steps = "integer"', 'steps = "text"'))
    with pytest.raises(db.SchemaError) as e:
        db.sync(manifest.load(root), path=path)
    assert e.value.message.startswith("days.steps changed from integer (or boolean) to text in project.toml")
    assert e.value.message.count("brought in line") == 0


def test_sync_changes_are_said_in_plain_words():
    assert db.describe_changes({"created": ["outings__rows", "outings"], "views_recreated": ["daily"]}) == (
        "Made the shared table outings; remade the view daily so it shows the rows of exactly the listed apps.")
    assert db.describe_changes({"created": [], "views_recreated": []}) is None


def test_schema_text_is_readable(proj, capsys):
    from mes import cli

    assert cli.run(["db", "schema", "--project-dir", str(proj.root)]) == 0
    out = capsys.readouterr().out
    assert "- days: key date; columns date (text), steps (integer)" in out and "{" not in out
    assert "- things: key id;" in out and "first of pp-b" in out and "Dashboards may read: days" in out


def test_check_notes_an_apps_table_it_does_not_list(proj, tmp_path):
    path = tmp_path / "pp.sqlite"
    db.sync(proj, path=path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE a_own (x)")
    conn.execute("CREATE TABLE a_extra (x)")
    conn.commit()
    conn.close()
    res = db.check(proj, path=path)
    assert res["unlisted"] == ["a_extra (pp-a)"] and res["ok"]
    assert "a_extra (pp-a) belongs to an app by name" in res["message"]
