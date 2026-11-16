"""The project's shared database: ``<data folder>/<project>.sqlite``.

Every shared table ``T`` of ``project.toml`` is stored as ``T__rows`` (primary key ``source`` plus
the table's key; ``source`` is the app that wrote the row) and read through the view ``T``.
Without ``prefer`` the view is the union of the installed apps' rows; with ``prefer`` it is one
row per key, each column taken from the highest-ranked app that has a value for it. ``[views]``
are plain read-only SELECTs on top. App tables (with the app's prefix) belong to their app.

The database uses SQLite's rollback journal (not WAL), so read-only openers never create
``-shm`` files. Every writer sets a busy timeout of 30 s and writes in one ``BEGIN IMMEDIATE``
transaction. The exact schema script is in ``docs/apps-contract.md`` section 4.3.

Public API
----------
``BUSY_TIMEOUT_MS`` (30000).
``schema_sql(project) -> str``     the exact schema script (``mes db schema --sql``).
``schema_info(project) -> dict``   the derived schema as data (``mes db schema``).
``ensure_schema(conn, project) -> dict``  bring the file in line with the schema: create what is
    missing, add missing columns, recreate views whose SQL differs; refuse anything else
    (``SchemaError``, hint ``mes db sync --reset <name>``). Runs inside the caller's transaction.
``check_views(conn, project) -> list[str]``  problems with ``[views]`` (read-only authorizer).
``db_path(project) -> Path``       ``<data folder>/<project>.sqlite`` on this computer / in this session.
``connect(path, *, write=True) -> sqlite3.Connection``
``sync(project, *, reset=(), path=None) -> dict``  ``ensure_schema`` in one transaction.
``check(project, *, prune=False, path=None) -> dict``
``AppDB``  what ``ctx.db()`` yields: ``.conn``, ``replace_rows``, ``upsert_rows``, ``delete_rows``.
``open_app_db(project, app, *, write=True, path=None)``  context manager behind ``ctx.db()``.
``register(subparsers)``  adds ``mes db schema|sync|rebuild|check``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from mes import paths
from mes.app import check_allowed
from mes.errors import MesError, NotFound, Refused, UsageError
from mes.manifest import SHARED_NAME_RE, AppSpec, Project, SharedTable

BUSY_TIMEOUT_MS = 30_000
RESET_HINT = "If its rows can be rebuilt from the apps' files, run: mes db sync --reset {name}"


class SchemaError(MesError):
    default_code = "schema_conflict"


# --------------------------------------------------------------------------- the script


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _apps_list(project: Project) -> str:
    return ", ".join(_lit(a) for a in project.apps)


def _rank(project: Project, table: SharedTable) -> str:
    order = list(table.prefer) + [a for a in project.apps if a not in table.prefer]
    whens = " ".join(f"WHEN {_lit(a)} THEN {i}" for i, a in enumerate(order))
    return f'CASE r."source" {whens} END'


def _create_table(t: SharedTable) -> str:
    lines = [f"CREATE TABLE IF NOT EXISTS {_q(t.rows_table)} (", '  "source" TEXT NOT NULL,']
    for k in t.key:
        lines.append(f"  {_q(k)} {t.column(k).sql_type} NOT NULL,")  # type: ignore[union-attr]
    for c in t.other_columns:
        lines.append(f"  {_q(c.name)} {c.sql_type},".replace(" ,", ","))  # "number" has no declared type
    lines.append(f'  PRIMARY KEY ("source", {", ".join(_q(k) for k in t.key)})')
    lines.append(");")
    return "\n".join(lines)


def _create_index(t: SharedTable) -> str:
    return f"CREATE INDEX IF NOT EXISTS {_q(t.index_name)} ON {_q(t.rows_table)} ({', '.join(_q(k) for k in t.key)});"


def view_body(project: Project, t: SharedTable) -> str:
    """``CREATE VIEW "<T>" AS ...`` without the trailing ``;`` (as SQLite stores it)."""
    apps = _apps_list(project)
    cols = ["source", *t.key, *(c.name for c in t.other_columns)]
    if not t.prefer:
        return (f"CREATE VIEW {_q(t.name)} AS\nSELECT {', '.join(_q(c) for c in cols)}\n"
                f"FROM {_q(t.rows_table)}\nWHERE \"source\" IN ({apps})")
    match = " AND ".join(f"r.{_q(k)} = k.{_q(k)}" for k in t.key)
    rank = _rank(project, t)
    lines = []
    for c in cols:
        if c in t.key:
            lines.append(f"  k.{_q(c)} AS {_q(c)}")
            continue
        not_null = "" if c == "source" else f" AND r.{_q(c)} IS NOT NULL"
        lines.append(f"  (SELECT r.{_q(c)} FROM {_q(t.rows_table)} r WHERE {match} AND r.\"source\" IN ({apps})"
                     f"{not_null} ORDER BY {rank} LIMIT 1) AS {_q(c)}")
    keys = ", ".join(_q(k) for k in t.key)
    return (f"CREATE VIEW {_q(t.name)} AS\nSELECT\n" + ",\n".join(lines)
            + f"\nFROM (SELECT DISTINCT {keys} FROM {_q(t.rows_table)} WHERE \"source\" IN ({apps})) k")


def _view_text(name: str, sql: str) -> str:
    return f"CREATE VIEW {_q(name)} AS\n{sql.strip()}"


def schema_sql(project: Project) -> str:
    """The exact schema script of contract section 4.3."""
    out: list[str] = []
    for t in project.tables.values():
        out += [_create_table(t), _create_index(t)]
    for t in project.tables.values():
        out += [f"DROP VIEW IF EXISTS {_q(t.name)};", view_body(project, t) + ";"]
    for v in project.views.values():
        out += [f"DROP VIEW IF EXISTS {_q(v.name)};", _view_text(v.name, v.sql) + ";"]
    return "\n".join(out) + "\n" if out else ""


def schema_info(project: Project) -> dict[str, Any]:
    return {
        "project": project.name,
        "file": project.db_file,
        "apps": list(project.apps),
        "tables": [t.to_dict() for t in project.tables.values()],
        "views": [v.to_dict() for v in project.views.values()],
        "app_tables": {a.name: list(a.data.tables) for a in project.apps.values()},
        "allowlist": project.allowlist(),
        "journal": "rollback (not WAL)",
        "busy_timeout_ms": BUSY_TIMEOUT_MS,
    }


# --------------------------------------------------------------------------- connecting


def db_path(project: Project | str, *, create_folder: bool = False) -> Path:
    name = project if isinstance(project, str) else project.name
    return paths.project_data_dir(name, create=create_folder) / f"{name}.sqlite"


def connect(path: Path, *, write: bool = True) -> sqlite3.Connection:
    if write:
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    else:
        if not path.is_file():
            raise NotFound(f"The project's database ({path.name}) does not exist yet, so there is nothing to read.",
                           hint="Run the app's rebuild (mes db rebuild) or a job that writes data first.",
                           code="no_database")
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000,
                               isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if write:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() == "wal":
            conn.execute("PRAGMA journal_mode = DELETE")
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- ensure_schema


def _objects(conn: sqlite3.Connection) -> dict[str, tuple[str, str | None]]:
    """name -> (type, sql) for every table, view, index and trigger."""
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def _conflict(name: str, problem: str) -> SchemaError:
    return SchemaError(
        f"The project's database cannot be brought in line with project.toml: {problem} mes only makes changes "
        "that keep every row (new tables, new columns, new views), so it changed nothing.",
        hint=RESET_HINT.format(name=name),
    )


def _expected_columns(t: SharedTable) -> list[tuple[str, str, int, int]]:
    """(name, declared type, notnull, pk position) as PRAGMA table_info reports them."""
    out = [("source", "TEXT", 1, 1)]
    for i, k in enumerate(t.key, 2):
        out.append((k, t.column(k).sql_type, 1, i))  # type: ignore[union-attr]
    for c in t.other_columns:
        out.append((c.name, c.sql_type, 0, 0))
    return out


def ensure_schema(conn: sqlite3.Connection, project: Project) -> dict[str, Any]:
    """Make the file match the schema. Call inside a write transaction."""
    objects = _objects(conn)
    done: dict[str, list[str]] = {"created": [], "columns_added": [], "views_recreated": [], "indexes_recreated": []}
    for t in project.tables.values():
        kind = objects.get(t.rows_table, (None, None))[0]
        if kind is None:
            conn.execute(_create_table(t).rstrip(";"))
            done["created"].append(t.rows_table)
        elif kind != "table":
            raise _conflict(t.name, f'"{t.rows_table}" is a {kind}, but it must be the table holding the rows of {t.name}.')
        else:
            have = [(r[1], str(r[2]).upper(), int(r[3]), int(r[5])) for r in conn.execute(
                f"PRAGMA table_info({_q(t.rows_table)})")]
            want = _expected_columns(t)
            have_by = {h[0]: h for h in have}
            want_by = {w[0]: w for w in want}
            removed = [h for h in have_by if h not in want_by]
            if removed:
                raise _conflict(t.name, f'the shared table {t.name} has the column "{removed[0]}", which project.toml '
                                "no longer lists (removing a column would lose its values).")
            keys_have = sorted((h[3], h[0]) for h in have if h[3])
            keys_want = sorted((w[3], w[0]) for w in want if w[3])
            if keys_have != keys_want:
                raise _conflict(t.name, f"the key of the shared table {t.name} changed (it was "
                                f"{', '.join(n for _, n in keys_have)}).")
            for name, w in want_by.items():
                h = have_by.get(name)
                if h is None:
                    conn.execute(f"ALTER TABLE {_q(t.rows_table)} ADD COLUMN {_q(name)} {w[1]}")
                    done["columns_added"].append(f"{t.name}.{name}")
                elif h[1] != w[1] or h[2] != w[2]:
                    raise _conflict(t.name, f'the column "{name}" of {t.name} changed its type (it was {h[1]}).')
        idx = objects.get(t.index_name)
        want_idx = [k for k in t.key]
        if idx is not None and idx[0] != "index":
            raise _conflict(t.name, f'"{t.index_name}" is a {idx[0]}, not the index of {t.name}.')
        if idx is not None:
            cols = [r[2] for r in conn.execute(f"PRAGMA index_info({_q(t.index_name)})")]
            if cols != want_idx:
                conn.execute(f"DROP INDEX {_q(t.index_name)}")
                idx = None
                done["indexes_recreated"].append(t.index_name)
        if idx is None:
            conn.execute(_create_index(t).rstrip(";"))
    views: list[tuple[str, str]] = [(t.name, view_body(project, t)) for t in project.tables.values()]
    views += [(v.name, _view_text(v.name, v.sql)) for v in project.views.values()]
    objects = _objects(conn)
    for name, _ in views:
        kind = objects.get(name, (None, None))[0]
        if kind not in (None, "view"):
            raise _conflict(name, f'"{name}" is a {kind} in the database, but project.toml makes it a view.')
    if any(objects.get(name, (None, None))[1] != body for name, body in views):
        for name, _ in reversed(views):
            if name in objects:
                conn.execute(f"DROP VIEW IF EXISTS {_q(name)}")
        for name, body in views:
            conn.execute(body)
            if objects.get(name, (None, None))[1] != body:
                done["views_recreated" if name in objects else "created"].append(name)
    return done


# --------------------------------------------------------------------------- views


_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


def check_views(conn: sqlite3.Connection, project: Project) -> list[str]:
    """Each [views] SQL may only read: it is prepared under an authorizer that allows nothing else.
    Returns plain problems (a view reading a table that does not exist yet is not one)."""
    problems: list[str] = []
    for v in project.views.values():
        refused: list[int] = []

        def auth(action: int, *_: Any) -> int:
            if action in _ALLOWED_ACTIONS:
                return sqlite3.SQLITE_OK
            refused.append(action)
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(auth)
        try:
            conn.execute(f"EXPLAIN {v.sql}").fetchall()
        except sqlite3.DatabaseError as e:
            text = str(e)
            if refused or "not authorized" in text:
                problems.append(f'The view "{v.name}" in project.toml does more than read data, so it is not used. '
                                "A view may only be one SELECT.")
            elif "no such table" in text or "no such column" in text:
                continue  # an app table that the app has not created yet
            else:
                problems.append(f'The SQL of the view "{v.name}" in project.toml is not valid: {text}.')
        finally:
            conn.set_authorizer(None)
    return problems


# --------------------------------------------------------------------------- sync / check


def _begin(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def sync(project: Project, *, reset: Sequence[str] = (), path: Path | None = None) -> dict[str, Any]:
    target = path or db_path(project, create_folder=True)
    declared_app_tables = {t for a in project.apps.values() for t in a.data.tables}
    for name in reset:
        if not isinstance(name, str) or not SHARED_NAME_RE.match(name) or name.startswith("sqlite_"):
            raise UsageError(f"{str(name)[:60]!r} is not the name of a shared table or view.",
                             hint="Use the name from project.toml, for example: mes db sync --reset activities")
        if name in declared_app_tables:
            raise Refused(f'"{name}" is a table of an app, and its app manages it, so mes does not reset it.',
                          hint="Ask the app to rebuild it, or remove it from the app's [data] tables first.",
                          code="app_table")
    conn = connect(target, write=True)
    try:
        _begin(conn)
        try:
            dropped = []
            objects = _objects(conn)
            for name in reset:
                for obj in (f"{name}__rows", name):
                    kind = objects.get(obj, (None, None))[0]
                    if kind in ("table", "view"):
                        conn.execute(f"DROP {kind.upper()} {_q(obj)}")
                        dropped.append(obj)
            done = ensure_schema(conn, project)
            problems = check_views(conn, project)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    return {"project": project.name, "file": str(target), "dropped": dropped, **done, "problems": problems}


def check(project: Project, *, prune: bool = False, path: Path | None = None) -> dict[str, Any]:
    target = path or db_path(project)
    if not target.is_file():
        return {"project": project.name, "file": str(target), "exists": False, "ok": True, "missing": [],
                "differ": [], "undeclared": [], "orphan_rows": {}, "pruned": 0,
                "message": "The project's database does not exist yet; it is made the first time an app writes data."}
    conn = connect(target, write=prune)
    try:
        objects = _objects(conn)
        missing, differ = [], []
        expected_views = {t.name: view_body(project, t) for t in project.tables.values()}
        expected_views.update({v.name: _view_text(v.name, v.sql) for v in project.views.values()})
        for t in project.tables.values():
            if objects.get(t.rows_table, (None,))[0] != "table":
                missing.append(t.rows_table)
                continue
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({_q(t.rows_table)})")}
            for c in ("source", *t.column_names):
                if c not in have:
                    missing.append(f"{t.rows_table}.{c}")
        for name, body in expected_views.items():
            if name not in objects:
                missing.append(name)
            elif objects[name][1] != body:
                differ.append(name)
        known = set(expected_views) | {t.rows_table for t in project.tables.values()} \
            | {t.index_name for t in project.tables.values()}
        prefixes = tuple(a.prefix for a in project.apps.values())
        undeclared = []
        for name, (kind, _sql) in objects.items():
            if name in known or kind == "index" and name.startswith("sqlite_autoindex"):
                continue
            if any(name.startswith(p) for p in prefixes):
                continue  # an app's own table (or its index)
            undeclared.append(f"{name} ({kind})")
        installed = list(project.apps)
        orphans: dict[str, int] = {}
        for t in project.tables.values():
            if objects.get(t.rows_table, (None,))[0] != "table":
                continue
            marks = ", ".join("?" for _ in installed) or "NULL"
            n = conn.execute(f'SELECT count(*) FROM {_q(t.rows_table)} WHERE "source" NOT IN ({marks})',
                             installed).fetchone()[0]
            if n:
                orphans[t.name] = int(n)
        pruned = 0
        if prune and orphans:
            _begin(conn)
            try:
                for t in project.tables.values():
                    if t.name in orphans:
                        marks = ", ".join("?" for _ in installed) or "NULL"
                        pruned += conn.execute(f'DELETE FROM {_q(t.rows_table)} WHERE "source" NOT IN ({marks})',
                                               installed).rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        problems = check_views(conn, project)
    finally:
        conn.close()
    ok = not missing and not differ and not problems
    parts = []
    if missing:
        parts.append(f"missing: {', '.join(missing[:8])}")
    if differ:
        parts.append(f"views that differ from project.toml: {', '.join(differ[:8])}")
    if undeclared:
        parts.append(f"objects nobody declares: {', '.join(undeclared[:8])}")
    if orphans:
        parts.append("rows of apps that are no longer installed: "
                     + ", ".join(f"{n} in {t}" for t, n in orphans.items()))
    message = ("The database matches project.toml." if ok and not undeclared and not orphans else
               "The database differs from project.toml (" + "; ".join(parts) + ").")
    if (missing or differ) and not prune:
        message += " Run: mes db sync"
    if orphans and not prune:
        message += " Rows of apps that are no longer installed are not shown anywhere; remove them with: mes db check --prune"
    return {"project": project.name, "file": str(target), "exists": True, "ok": ok, "missing": missing,
            "differ": differ, "undeclared": undeclared, "orphan_rows": orphans, "pruned": pruned,
            "problems": problems, "message": message}


# --------------------------------------------------------------------------- the helper for apps


def _bad_rows(table: str, problem: str, hint: str) -> MesError:
    return MesError(f"The rows for the shared table {table} were not written: {problem} Nothing was changed.",
                    hint=hint, code="bad_rows")


def _cell(table: SharedTable, column: str, value: Any) -> Any:
    col = table.column(column)
    assert col is not None
    if col.type == "json":
        if value is None:
            return None
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            raise _bad_rows(table.name, f'the value for "{column}" cannot be written as JSON.',
                            "Give plain lists, dicts, text and numbers.") from None
    if value is None:
        return None
    if isinstance(value, bool):
        if col.type in ("boolean", "integer"):
            return int(value)
        raise _bad_rows(table.name, f'"{column}" is {col.type}, but it was given true or false.',
                        f"Give a {col.type} value for {column}.")
    if not isinstance(value, (int, float, str)):
        raise _bad_rows(table.name, f'the value for "{column}" is a {type(value).__name__}, which a database column '
                        "cannot hold.", "Give text, numbers, true/false or nothing (None).")
    return value


class AppDB:
    """The project's database, as one app sees it (what ``ctx.db()`` gives)."""

    def __init__(self, conn: sqlite3.Connection, project: Project, app: AppSpec, *, write: bool) -> None:
        self.conn = conn
        self.project = project
        self.app = app
        self.write = write

    def _table(self, name: str) -> SharedTable:
        if not self.write:
            raise UsageError("This database was opened for reading only, so nothing can be written.",
                             hint="Use ctx.db() (write=True) to write rows.")
        if name not in self.app.data.writes or name not in self.project.tables:
            raise _bad_rows(name, f"the app {self.app.name} does not write this table (it is not in its [data] writes).",
                            f'Add "{name}" to [data] writes in the app\'s app.toml (and [tables.{name}] to project.toml).')
        return self.project.tables[name]

    def _prepare(self, table: SharedTable, rows: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
        names = table.column_names
        out = []
        for n, row in enumerate(rows, 1):
            if not isinstance(row, Mapping):
                raise _bad_rows(table.name, f"row {n} is not a dict of column values.", "Give each row as a dict.")
            for key in row:
                if key == "source":
                    if row[key] != self.app.name:
                        raise _bad_rows(table.name, f'row {n} says it comes from "{str(row[key])[:40]}", but only rows '
                                        f"of {self.app.name} can be written here.",
                                        "Leave source out: mes fills it in.")
                    continue
                if key not in names:
                    raise _bad_rows(table.name, f'row {n} has the column "{str(key)[:60]}", which the table does not have.',
                                    f"Its columns are: {', '.join(names)}.")
            for k in table.key:
                if row.get(k) is None:
                    raise _bad_rows(table.name, f'row {n} has no value for the key column "{k}".',
                                    f"Every row needs {', '.join(table.key)}.")
            out.append((self.app.name, *(_cell(table, c, row.get(c)) for c in names)))
        return out

    def _insert(self, table: SharedTable, values: list[tuple[Any, ...]], verb: str) -> int:
        cols = ", ".join(_q(c) for c in ("source", *table.column_names))
        marks = ", ".join("?" for _ in range(len(table.columns) + 1))
        self.conn.executemany(f"{verb} INTO {_q(table.rows_table)} ({cols}) VALUES ({marks})", values)
        return len(values)

    def replace_rows(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        """Delete this app's rows of ``table`` and insert ``rows``; returns how many were written."""
        t = self._table(table)
        values = self._prepare(t, rows)
        self.conn.execute(f'DELETE FROM {_q(t.rows_table)} WHERE "source" = ?', (self.app.name,))
        try:
            return self._insert(t, values, "INSERT")
        except sqlite3.IntegrityError:
            raise _bad_rows(table, "two rows have the same key.", f"Give each row its own {', '.join(t.key)}.") from None

    def upsert_rows(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        t = self._table(table)
        return self._insert(t, self._prepare(t, rows), "INSERT OR REPLACE")

    def delete_rows(self, table: str, where: Mapping[str, Any] | None = None) -> int:
        t = self._table(table)
        sql = f'DELETE FROM {_q(t.rows_table)} WHERE "source" = ?'
        args: list[Any] = [self.app.name]
        for k, v in (where or {}).items():
            if k not in t.column_names:
                raise _bad_rows(table, f'"{str(k)[:60]}" is not one of its columns.', f"Its columns are: {', '.join(t.column_names)}.")
            if v is None:
                sql += f" AND {_q(k)} IS NULL"
            else:
                sql += f" AND {_q(k)} = ?"
                args.append(_cell(t, k, v))
        return self.conn.execute(sql, args).rowcount


@contextlib.contextmanager
def open_app_db(project: Project, app: AppSpec, *, write: bool = True, path: Path | None = None) -> Iterator[AppDB]:
    """Connect, ensure the schema and ``BEGIN IMMEDIATE`` (write), or open read-only. Commits on
    success, rolls back on any exception, always closes."""
    target = path or db_path(project, create_folder=write)
    conn = connect(target, write=write)
    try:
        if write:
            _begin(conn)
            try:
                ensure_schema(conn, project)
                yield AppDB(conn, project, app, write=True)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        else:
            yield AppDB(conn, project, app, write=False)
    finally:
        conn.close()
    if write:
        with contextlib.suppress(OSError):
            os.chmod(target, 0o600)


# --------------------------------------------------------------------------- mes db


def _load(args: argparse.Namespace) -> tuple[Path, Project]:
    from mes.jobs import load_project

    return load_project(args.project_dir)


def _cmd_schema(args: argparse.Namespace) -> Any:
    check_allowed("read")
    _, project = _load(args)
    if args.sql:
        text = schema_sql(project)
        return {"project": project.name, "sql": text} if args.json else _RawText(text)
    return schema_info(project)


class _RawText(str):
    """Printed exactly as it is (the schema script must stay byte-exact)."""


def _cmd_sync(args: argparse.Namespace) -> Any:
    check_allowed("write")
    _, project = _load(args)
    result = sync(project, reset=args.reset)
    if args.json:
        return result
    changed = result["created"] + result["columns_added"] + result["views_recreated"] + result["dropped"]
    lines = []
    if not args.quiet or changed or result["problems"]:
        lines.append("The project's database matches project.toml now." if not changed else
                     "The project's database was brought in line with project.toml: " + ", ".join(changed[:12]) + ".")
    lines += [f"Please note: {p}" for p in result["problems"]]
    return "\n".join(lines) if lines else None


def _cmd_rebuild(args: argparse.Namespace) -> Any:
    check_allowed("write")
    from mes import jobs

    root, project = _load(args)
    runs = jobs.rebuild_apps(root, project, app=args.app)
    if args.json:
        return {"project": project.name, "runs": runs}
    if not runs:
        return "No app of this project has a [data] rebuild command, so there was nothing to rebuild."
    return "\n".join(f'{r["app"]}: "{r["command"]}" finished in {r["duration_s"]} seconds.' for r in runs)


def _cmd_check(args: argparse.Namespace) -> Any:
    check_allowed("write" if args.prune else "read")
    _, project = _load(args)
    result = check(project, prune=args.prune)
    return result if args.json else result["message"] + (f" Removed {result['pruned']} rows." if result["pruned"] else "")


def register(subparsers: Any) -> None:
    d = subparsers.add_parser(
        "db",
        help="The project's shared database: its schema, bringing it up to date, rebuilding and checking it.",
        description="Every project has one shared SQLite database in its data folder. Its shared tables and views "
        "come from project.toml. See: mes guide share-data",
    )
    sub = d.add_subparsers(dest="db_command", metavar="<schema|sync|rebuild|check>")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--project-dir", default=None, metavar="DIR", help="the project's folder (default: here)")

    p = sub.add_parser("schema", help="The shared tables and views derived from project.toml (--sql: the exact script).")
    p.add_argument("--sql", action="store_true", help="print the exact schema script")
    common(p)
    p.set_defaults(func=_cmd_schema)

    p = sub.add_parser("sync", help="Bring the database in line with project.toml (only changes that keep every row).")
    p.add_argument("--reset", action="append", default=[], metavar="NAME",
                   help="first drop this shared table's rows (or a view or stray object of that name); repeatable")
    p.add_argument("--quiet", action="store_true", help="say nothing when nothing changed")
    common(p)
    p.set_defaults(func=_cmd_sync)

    p = sub.add_parser("rebuild", help="Run each app's [data] rebuild command, in the project's app order.")
    p.add_argument("--app", default=None, help="only this app (its name or short name)")
    common(p)
    p.set_defaults(func=_cmd_rebuild)

    p = sub.add_parser("check", help="Compare the database with project.toml; --prune removes rows of removed apps.")
    p.add_argument("--prune", action="store_true", help="delete rows whose app is no longer installed")
    common(p)
    p.set_defaults(func=_cmd_check)
