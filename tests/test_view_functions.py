"""[views] may call only SQLite's own side-effect-free built-ins (an explicit list), never random
values, files, extensions or functions registered on a connection."""

from __future__ import annotations

import sqlite3

import pytest

import projects as helpers
from mes import db

APP = '[app]\nname = "vv-a"\ncommand = "vv-a"\n[data]\nwrites = ["days"]\n'


def _project(tmp_path, sql: str):
    text = ('[project]\nname = "vv"\napps = ["vv_a"]\n[tables.days]\nkey = ["date"]\n'
            'columns = { date = "text", steps = "integer", extra = "json" }\n'
            f'[views.v]\nsql = "{sql}"\n')
    return helpers.load(tmp_path / "vv", text, {"vv_a": APP})


def _problems(project, *, extra=None) -> list[str]:
    conn = sqlite3.connect(":memory:")
    try:
        if extra:
            conn.create_function(extra, 0, lambda: 1)
        conn.executescript(db.schema_sql(project))
        return db.check_views(conn, project)
    finally:
        conn.close()


@pytest.mark.parametrize("sql", [
    "SELECT date, abs(steps), round(steps / 1000.0, 1), coalesce(steps, 0) FROM days",
    "SELECT strftime('%Y-%W', date) AS week, count(*), sum(steps), group_concat(date) FROM days GROUP BY 1",
    "SELECT date, json_extract(extra, '$.a'), row_number() OVER (ORDER BY date) FROM days",
    "SELECT date, lag(steps) OVER (ORDER BY date), sqrt(abs(steps)) FROM days",
])
def test_ordinary_functions_are_allowed(tmp_path, sql):
    assert _problems(_project(tmp_path, sql)) == []


@pytest.mark.parametrize("sql, name", [
    ("SELECT random() AS r FROM days", "random"),
    ("SELECT randomblob(4) FROM days", "randomblob"),
    ("SELECT sqlite_compileoption_used('THREADSAFE') FROM days", "sqlite_compileoption_used"),
    ("SELECT last_insert_rowid() FROM days", "last_insert_rowid"),
])
def test_other_functions_are_refused_by_name(tmp_path, sql, name):
    problems = _problems(_project(tmp_path, sql))
    assert len(problems) == 1 and '"v"' in problems[0] and f"{name}()" in problems[0]


def test_a_function_registered_on_the_connection_is_refused(tmp_path):
    problems = _problems(_project(tmp_path, "SELECT stand_in() FROM days"), extra="stand_in")
    assert problems and "stand_in()" in problems[0]
