"""Reading and checking a project's ``project.toml`` and the ``app.toml`` of each of its apps.

A **project** is one folder (usually one git repository) with a ``project.toml`` at its root. It is
what you deploy and the only security boundary (two accounts per project on a server). An **app**
is a Python package with an ``app.toml`` next to its ``__init__.py``: it brings its own commands,
jobs, tables and dashboards. The project lists the apps it uses, in order::

    [project]
    name = "fitlab"
    title = "Fit lab"
    version = "0.1.0"
    apps = ["fitlab_walks", "fitlab_rides"]      # package names; order matters

    [tables.activities]                          # a shared table any installed app may write
    key = ["id"]
    columns = { id = "text", date = "text", distance_m = "real" }

    [views.weekly]                               # one read-only SELECT over the shared data
    sql = "SELECT date, count(*) AS n FROM activities GROUP BY 1"

    [settings.fitlab-rides]                      # per-project changes, keyed by APP name
    jobs.rebuild.timeout = "5m"

    [ui]
    dashboards = ["overview"]
    refresh_after = ["fitlab-rides.rebuild"]     # job ids

``apps/<package>/app.toml`` (or ``site-packages/<package>/app.toml`` for an installed app)::

    [app]
    name = "fitlab-walks"
    cli = "fitlab_walks.cli:app"                 # a mes.App; or: command = "<console script>"
    [data]
    writes = ["activities"]
    tables = ["walks_log"]
    rebuild = "rebuild"
    [jobs.rebuild]
    run = "rebuild"                              # a command of THIS app
    manual = true

The full rules are in ``docs/apps-contract.md`` (sections 0 to 3). Unknown keys are errors
everywhere, because a typo must never silently drop a safety limit.

Where the apps are: ``apps.lock.json`` (written by ``mes apps sync``) when it exists, validated
against ``project.toml``; otherwise ``<project>/apps/<package>/app.toml`` for every package. Each
``app.toml`` is always read fresh from the app's folder. mes never imports an app.

Public API
----------
Constants: ``MANIFEST_FILE``, ``APP_FILE``, ``LOCK_FILE``, ``APPS_DIR``, ``PROJECT_NAME_RE``
(also ``APP_NAME_RE``), ``PACKAGE_RE``, ``JOB_NAME_RE``, ``JOB_ID_RE``, ``COMMAND_WORD_RE``,
``CLI_NAME_RE``, ``CLI_REF_RE``, ``SHARED_NAME_RE``, ``COLUMN_RE``, ``APP_TABLE_RE``,
``FOLDER_RE``, ``SECTIONS_PATH_RE``, ``UI_DASHBOARD_RE``, ``COLUMN_TYPES``, ``HOST_RE``,
``BUCKET_NAME_RE``, ``JOB_KEYS``, ``TRIGGER_KEYS``, ``RESERVED_APP_NAMES``, ``RESERVED_SHORT_NAMES``, ``RESERVED_TABLE_PREFIXES``,
``RESERVED_FOLDERS``, ``NOT_APP_PROGRAMS``, ``NETWORK_NOTE_LOCAL``, ``DEFAULT_MEMORY``,
``DEFAULT_TIMEOUT``, ``DEFAULT_WRITES``, ``WRITE_TARGETS``, ``MAX_MANIFEST_BYTES``,
``MAX_APP_TOML_BYTES``, ``MAX_LOCK_BYTES``, ``LOCK_STALE_MESSAGE``, ``LOCK_STALE_HINT``.

``validate_project_name(name)``, ``validate_app_name(name)``  return the name or raise.
``short_name(app, project) -> str``, ``table_prefix(short) -> str``
``parse(data, *, source, path=None) -> ProjectFile``; ``loads(text, ...) -> ProjectFile``
``load_file(path) -> ProjectFile``   project.toml only (no apps are read).
``parse_app(data, *, source, path=None, package=None) -> AppFile``; ``loads_app(text, ...)``
``load_app_file(path, *, source) -> AppFile``  one app.toml, with the file checks.
``read_lock(root, project_file) -> list[Located] | None``  ``None`` when there is no lock.
``locate_apps(root, project_file) -> (list[Located], bool)``  lock or fallback; bool = lock used.
``compose(project_file, located) -> Project``
``load(path) -> Project``  a ``project.toml`` file or a folder holding one, with its apps.
``find(start=None) -> Path | None``
``calendar_problem(text) -> str | None``

Dataclasses: ``Column``, ``SharedTable``, ``SharedView``, ``JobSetting``, ``UI``, ``Storage``,
``ProjectFile``, ``JobDecl``, ``AppData``, ``AppUI``, ``AppFile``, ``Located``, ``AppSpec``,
``Job``, ``Project`` (``Manifest`` is another name for it).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mes.errors import ManifestError, NotFound

MANIFEST_FILE = "project.toml"
APP_FILE = "app.toml"
LOCK_FILE = "apps.lock.json"
APPS_DIR = "apps"
PROJECT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")
APP_NAME_RE = PROJECT_NAME_RE
PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}\Z")
JOB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}\Z")
JOB_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}\.[a-z][a-z0-9-]{0,30}\Z")
# The first word of a job's run line: one of the app's own command names.
COMMAND_WORD_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}\Z")
CLI_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}\Z")
CLI_REF_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*\Z")
SHARED_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}\Z")
COLUMN_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}\Z")
APP_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}\Z")
FOLDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,40}\Z")
SECTIONS_PATH_RE = re.compile(r"^dashboards/[a-z0-9][a-z0-9-]{0,60}\.yaml\Z")
UI_DASHBOARD_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\Z")
UI_TABLE_RE = SHARED_NAME_RE
UI_ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}\Z")
BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")
COLUMN_TYPES = {"text": "TEXT", "integer": "INTEGER", "real": "REAL", "boolean": "INTEGER", "json": "TEXT"}
KEY_TYPES = ("text", "integer")
RESERVED_APP_NAMES = ("mes",)
RESERVED_SHORT_NAMES = ("describe", "guide", "apps", "help", "sqlite", "mes")
# Table names (and so app table prefixes) that belong to SQLite or to mes itself.
RESERVED_TABLE_PREFIXES = ("sqlite_", "mes_")
RESERVED_FOLDERS = ("store", "logs", "state", "ui", "backup", "tmp")
TOP_KEYS = ("project", "tables", "views", "settings", "ui", "storage")
PROJECT_KEYS = ("name", "title", "version", "apps")
TABLE_KEYS = ("columns", "key", "prefer")
VIEW_KEYS = ("sql",)
SETTINGS_KEYS = ("jobs",)
SETTINGS_JOB_KEYS = ("schedule", "enabled", "memory", "timeout", "add_network")
UI_KEYS = ("accent", "dashboards", "tables", "refresh_after")
STORAGE_KEYS = ("bucket",)
APP_TOP_KEYS = ("app", "data", "jobs", "ui")
APP_KEYS = ("name", "title", "version", "cli", "command")
DATA_KEYS = ("folder", "writes", "tables", "rebuild")
APP_UI_KEYS = ("dashboards", "tables", "contributes")
CONTRIBUTES_KEYS = ("sections",)
JOB_KEYS = ("run", "schedule", "after", "manual", "webhook", "memory", "timeout", "network", "writes")
TRIGGER_KEYS = ("schedule", "after", "manual", "webhook")
LOCK_KEYS = ("v", "project", "project_toml_sha256", "apps")
LOCK_APP_KEYS = ("package", "name", "path", "in_project", "program")
UI_MAX_ITEMS = 100
MAX_APPS = 16
MAX_JOBS_PER_APP = 32
MAX_JOBS = 64
MAX_TABLES = 32
MAX_COLUMNS = 200
MAX_KEY = 4
MAX_VIEWS = 32
MAX_VIEW_SQL = 10_000
DEFAULT_MEMORY = "256M"
DEFAULT_TIMEOUT = "15m"
DEFAULT_WRITES = ("data", "state")
WRITE_TARGETS = ("data", "state")
DEFAULT_VERSION = "0.0.0"
MAX_HOSTS = 32
MAX_MANIFEST_BYTES = 256 * 1024
MAX_APP_TOML_BYTES = 64 * 1024
MAX_LOCK_BYTES = 64 * 1024
# Programs that run any code or install software: an app's program can never be one of them.
NOT_APP_PROGRAMS = re.compile(
    r"^(?:python[0-9.]*|pythonw|pip[0-9.]*|uv|uvx|pydoc[0-9.]*|ipython[0-9]*|pytest|py\.test|activate.*|"
    r"sh|bash|zsh|fish|dash|env|deactivate|mes|easy-ui|mybucket)\Z", re.IGNORECASE)
NETWORK_NOTE_LOCAL = (
    "On this computer (mes jobs run) mes cannot limit a job to the hosts it lists, so the job has "
    "full internet access here. On a Linux server set up with 'mes server plan', the job can only "
    "reach the listed hosts."
)
LOCK_STALE_MESSAGE = "The list of installed apps (apps.lock.json) is out of date, so the apps cannot be found safely."
LOCK_STALE_HINT = "Run: mes apps sync"

_MEMORY_RE = re.compile(r"^([1-9][0-9]{0,6})([KMG])\Z")
_TIMEOUT_RE = re.compile(r"^([1-9][0-9]{0,5})([smh])\Z")
_SCHEDULE_RE = re.compile(r"^[A-Za-z0-9*:,./~ +_@-]+\Z")
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST_RE = re.compile(rf"^(?=.{{1,253}}\Z){_HOST_LABEL}(?:\.{_HOST_LABEL})*\Z")
_SHELL_TOKENS = {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "2>", "2>&1"}
_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3}
_SECONDS = {"s": 1, "m": 60, "h": 3600}
_VIEW_START_RE = re.compile(r"^\s*(?:SELECT|WITH)\b", re.IGNORECASE)
_VERSION_RE = re.compile(r"[A-Za-z0-9.+_-]+")


# --------------------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class Column:
    name: str
    type: str

    @property
    def sql_type(self) -> str:
        return COLUMN_TYPES[self.type]


@dataclass(frozen=True)
class SharedTable:
    name: str
    columns: tuple[Column, ...]
    key: tuple[str, ...]
    prefer: tuple[str, ...] = ()

    @property
    def rows_table(self) -> str:
        return f"{self.name}__rows"

    @property
    def index_name(self) -> str:
        return f"{self.name}__rows_key"

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    @property
    def other_columns(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.name not in self.key)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "rows_table": self.rows_table,
                "columns": {c.name: c.type for c in self.columns}, "key": list(self.key),
                "prefer": list(self.prefer), "view": "merged" if self.prefer else "union"}


@dataclass(frozen=True)
class SharedView:
    name: str
    sql: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "sql": self.sql}


@dataclass(frozen=True)
class JobSetting:
    schedule: str | None = None
    enabled: bool | None = None
    memory: str | None = None
    timeout: str | None = None
    add_network: tuple[str, ...] = ()


@dataclass(frozen=True)
class UI:
    accent: str | None = None
    dashboards: tuple[str, ...] = ()
    tables: tuple[str, ...] | None = None
    refresh_after: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"accent": self.accent, "dashboards": list(self.dashboards),
                "tables": list(self.tables) if self.tables is not None else None,
                "refresh_after": list(self.refresh_after)}


@dataclass(frozen=True)
class Storage:
    bucket: str

    def to_dict(self) -> dict[str, Any]:
        return {"bucket": self.bucket}


@dataclass(frozen=True)
class ProjectFile:
    """``project.toml`` on its own, before any app is read."""

    name: str
    title: str
    version: str
    packages: tuple[str, ...]
    tables: dict[str, SharedTable] = field(default_factory=dict)
    views: dict[str, SharedView] = field(default_factory=dict)
    settings: dict[str, dict[str, JobSetting]] = field(default_factory=dict)
    ui: UI | None = None
    storage: Storage | None = None
    path: Path | None = None
    sha256: str | None = None

    @property
    def root(self) -> Path | None:
        return self.path.parent if self.path else None


@dataclass(frozen=True)
class JobDecl:
    """One ``[jobs.<name>]`` of an app.toml, checked but not yet tied to the project."""

    name: str
    run: str
    argv: tuple[str, ...]
    trigger: str
    schedule: str | None = None
    after: str | None = None
    memory: str = DEFAULT_MEMORY
    timeout: str = DEFAULT_TIMEOUT
    network: tuple[str, ...] = ()
    writes: tuple[str, ...] = DEFAULT_WRITES


@dataclass(frozen=True)
class AppData:
    folder: str | None = None
    writes: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    rebuild: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"folder": self.folder, "writes": list(self.writes), "tables": list(self.tables),
                "rebuild": self.rebuild}


@dataclass(frozen=True)
class AppUI:
    dashboards: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    contributes: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"dashboards": list(self.dashboards), "tables": list(self.tables),
                "contributes": {slug: {"sections": p} for slug, p in self.contributes}}


@dataclass(frozen=True)
class AppFile:
    """One ``app.toml`` on its own."""

    name: str
    title: str
    version: str
    cli: str | None
    command: str | None
    data: AppData
    jobs: dict[str, JobDecl]
    ui: AppUI
    path: Path | None = None

    @property
    def program(self) -> str:
        return self.command if self.command else self.name


@dataclass(frozen=True)
class Located:
    package: str
    folder: Path
    in_project: bool
    name: str | None = None      # from the lock, checked against app.toml
    program: str | None = None   # from the lock, checked against app.toml


@dataclass(frozen=True)
class AppSpec:
    """One installed app of a project."""

    package: str
    name: str
    title: str
    version: str
    short: str
    prefix: str
    program: str
    cli: str | None
    command: str | None
    folder: Path | None
    in_project: bool
    data: AppData
    ui: AppUI

    @property
    def location(self) -> str:
        return "in-project" if self.in_project else "installed"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "short": self.short, "package": self.package, "title": self.title,
                "version": self.version, "program": self.program, "cli": self.cli, "command": self.command,
                "prefix": self.prefix, "location": self.location,
                "path": str(self.folder) if self.folder else None,
                "data": self.data.to_dict(), "ui": self.ui.to_dict()}


@dataclass(frozen=True)
class Job:
    id: str
    name: str
    app: str
    run: str
    argv: tuple[str, ...]
    trigger: str
    schedule: str | None = None
    after: str | None = None
    memory: str = DEFAULT_MEMORY
    timeout: str = DEFAULT_TIMEOUT
    network: tuple[str, ...] = ()
    writes: tuple[str, ...] = DEFAULT_WRITES

    @property
    def command(self) -> str:
        return self.argv[0]

    @property
    def timeout_seconds(self) -> int:
        m = _TIMEOUT_RE.match(self.timeout)
        assert m
        return int(m.group(1)) * _SECONDS[m.group(2)]

    @property
    def memory_bytes(self) -> int:
        m = _MEMORY_RE.match(self.memory)
        assert m
        return int(m.group(1)) * _UNITS[m.group(2)]

    @property
    def network_allowed(self) -> bool:
        return bool(self.network)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "app": self.app,
            "name": self.name,
            "run": self.run,
            "argv": list(self.argv),
            "trigger": self.trigger,
            "schedule": self.schedule,
            "after": self.after,
            "memory": self.memory,
            "timeout": self.timeout,
            "network": list(self.network) if self.network else False,
            # A host list is enforced by the system units of a server, not by 'mes jobs run'.
            "network_enforced": {"server": True, "this_computer": not self.network},
            "network_note": NETWORK_NOTE_LOCAL if self.network else None,
            "writes": list(self.writes),
        }


@dataclass(frozen=True)
class Project:
    """A project with its installed apps and their jobs ([settings] applied)."""

    name: str
    title: str
    version: str
    packages: tuple[str, ...] = ()
    apps: dict[str, AppSpec] = field(default_factory=dict)
    jobs: dict[str, Job] = field(default_factory=dict)
    disabled: tuple[str, ...] = ()
    tables: dict[str, SharedTable] = field(default_factory=dict)
    views: dict[str, SharedView] = field(default_factory=dict)
    ui: UI | None = None
    storage: Storage | None = None
    path: Path | None = None
    lock_used: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def root(self) -> Path | None:
        return self.path.parent if self.path else None

    @property
    def db_file(self) -> str:
        return f"{self.name}.sqlite"

    def job(self, name: str) -> Job:
        """A job by its id, or by its bare name when exactly one app has a job of that name."""
        if name in self.jobs:
            return self.jobs[name]
        if name in self.disabled:
            raise NotFound(
                f'The job "{name}" is switched off in project.toml ([settings] enabled = false), so it does not run.',
                hint="Remove the enabled = false line (or set it to true) to switch it on again.",
                code="job_disabled",
            )
        matches = [j for j in self.jobs.values() if j.name == name]
        if len(matches) == 1:
            return matches[0]
        known = ", ".join(self.jobs) or "none"
        if len(matches) > 1:
            raise NotFound(
                f'Several apps of "{self.name}" have a job called "{name}", so it is not clear which one is meant.',
                hint=f"Use the job's full id: {', '.join(j.id for j in matches)}.",
                code="job_ambiguous",
            )
        raise NotFound(
            f'The project "{self.name}" has no job called "{name}".',
            hint=f"Its jobs are: {known}. Check the spelling, or add the job to the app's app.toml.",
        )

    def app(self, name: str) -> AppSpec:
        try:
            return self.apps[name]
        except KeyError:
            known = ", ".join(self.apps) or "none"
            raise NotFound(
                f'The project "{self.name}" has no app called "{name}".',
                hint=f"Its apps are: {known}.",
            ) from None

    def app_for_word(self, word: str) -> AppSpec | None:
        """The app whose short name or full name is ``word``."""
        if word in self.apps:
            return self.apps[word]
        return next((a for a in self.apps.values() if a.short == word), None)

    def dependents(self, job_id: str) -> list[Job]:
        return [j for j in self.jobs.values() if j.after == job_id]

    def allowlist(self) -> list[str]:
        """The tables the dashboards may read (contract section 4.6)."""
        full = list(self.tables) + list(self.views)
        for spec in self.apps.values():
            full += [t for t in spec.ui.tables if t not in full]
        if self.ui is not None and self.ui.tables is not None:
            return list(self.ui.tables)
        return full

    @property
    def has_dashboards(self) -> bool:
        if self.ui is not None or any(a.ui.dashboards or a.ui.contributes for a in self.apps.values()):
            return True
        folder = self.root / "ui" / "dashboards" if self.root else None
        return bool(folder and folder.is_dir() and any(folder.glob("*.yaml")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "path": str(self.path) if self.path else None,
            "packages": list(self.packages),
            "apps": [a.to_dict() for a in self.apps.values()],
            "jobs": [j.to_dict() for j in self.jobs.values()],
            "disabled_jobs": list(self.disabled),
            "tables": [t.to_dict() for t in self.tables.values()],
            "views": [v.to_dict() for v in self.views.values()],
            "allowlist": self.allowlist(),
            "ui": self.ui.to_dict() if self.ui else None,
            "storage": self.storage.to_dict() if self.storage else None,
            "lock_used": self.lock_used,
            "warnings": list(self.warnings),
        }


Manifest = Project


# --------------------------------------------------------------------------- calendar check

_SHORTHANDS = {"minutely", "hourly", "daily", "weekly", "monthly", "yearly", "annually",
               "quarterly", "semiannually", "semi-annually"}
_DAY = r"(?:mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_WEEKDAYS_RE = re.compile(rf"^{_DAY}(?:\.\.{_DAY})?(?:,{_DAY}(?:\.\.{_DAY})?)*\Z", re.IGNORECASE)
_PART = r"[0-9*][0-9*.,/]*"
_DATE_RE = re.compile(rf"^{_PART}(?:[-~]{_PART}){{1,2}}\Z")
_TIME_RE = re.compile(rf"^{_PART}(?::{_PART}){{1,2}}\Z")
_ZONE_RE = re.compile(r"^(?:UTC|GMT|[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){1,2})\Z")
_EPOCH_RE = re.compile(r"^@[0-9]{1,12}\Z")


def calendar_problem(text: str) -> str | None:
    """A plain description of what is wrong with a systemd calendar expression, or None.

    This is mes's own check, so it works on computers without systemd (such as a Mac). It
    accepts the usual forms: a shorthand ("daily", "hourly", "weekly", ...), or
    "[weekdays] [date] [time] [time zone]" such as "Mon..Fri *-*-* 06:00:00" or "*:0/15".
    On Linux, systemd checks the expression again when the units are loaded.
    """
    words = text.split()
    if not words:
        return "it is empty"
    if _EPOCH_RE.match(words[0]) and len(words) == 1:
        return None
    if words[-1] != "UTC" and len(words) > 1 and _ZONE_RE.match(words[-1]) and "/" in words[-1]:
        words = words[:-1]
    elif words[-1] in ("UTC", "GMT") and len(words) > 1:
        words = words[:-1]
    if len(words) == 1 and words[0].lower() in _SHORTHANDS:
        return None
    rest = list(words)
    found = []
    if rest and _WEEKDAYS_RE.match(rest[0]):
        found.append(rest.pop(0))
    if rest and _DATE_RE.match(rest[0]):
        found.append(rest.pop(0))
    if rest and _TIME_RE.match(rest[0]):
        found.append(rest.pop(0))
    if rest:
        return f'mes does not understand the part "{rest[0]}"'
    if not found:
        return "it names no day, date or time"
    return None


# --------------------------------------------------------------------------- helpers


def _fail(source: str, where: str, problem: str, hint: str, code: str | None = None) -> ManifestError:
    place = f"{source}, {where}" if where else source
    return ManifestError(
        f"There is a problem in {place}: {problem} The project cannot use this file until it is fixed.",
        hint=hint, code=code,
    )


def _check_keys(table: dict[str, Any], allowed: tuple[str, ...], source: str, where: str) -> None:
    for key in table:
        if key not in allowed:
            close = difflib.get_close_matches(key, allowed, n=1)
            maybe = f' Did you mean "{close[0]}"?' if close else ""
            raise _fail(
                source,
                where,
                f'it has a setting this version of mes does not know: "{key}". '
                "Unknown settings are not ignored, because a typo could silently switch off a safety limit.",
                f"Allowed settings here: {', '.join(allowed)}.{maybe}",
            )


def _string(value: Any, source: str, where: str, key: str, example: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(source, where, f'"{key}" must be text, for example {key} = "{example}".', f'Write {key} = "{example}".')
    return value.strip()


def _table(value: Any, source: str, where: str, example: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(source, where, f"{where} must be a section of settings.", f"Write it as {example}.")
    return value


def _title(proj: dict[str, Any], default: str, source: str, where: str) -> str:
    title = _string(proj["title"], source, where, "title", "My project") if "title" in proj else default
    if len(title) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in title):
        raise _fail(source, where, "the title must be one line of at most 80 characters.", 'Write title = "My project".')
    return title


def _version(proj: dict[str, Any], source: str, where: str) -> str:
    version = _string(proj["version"], source, where, "version", "0.1.0") if "version" in proj else DEFAULT_VERSION
    if len(version) > 40 or not _VERSION_RE.fullmatch(version):
        raise _fail(source, where, f"the version {version!r} is not a plain version number.", 'Write version = "0.1.0".')
    return version


def _name_list(value: Any, pattern: re.Pattern[str], source: str, where: str, key: str, what: str,
               example: str, *, limit: int = UI_MAX_ITEMS) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _fail(source, where, f'"{key}" must be a list of {what}, for example {key} = {example}.',
                    f"Write {key} = {example}.")
    bad = [v for v in value if not pattern.match(v)]
    if bad:
        raise _fail(source, where, f'"{key}" has {bad[0][:80]!r}, which is not one of {what}.', f"Write {key} = {example}.")
    if len(value) > limit or len(set(value)) != len(value):
        raise _fail(source, where, f'"{key}" lists a name more than once (or more than {limit} names).',
                    "List each one once.")
    return tuple(value)


def validate_project_name(name: Any, *, source: str = MANIFEST_FILE) -> str:
    if not isinstance(name, str) or not PROJECT_NAME_RE.match(name) or name in RESERVED_APP_NAMES \
            or NOT_APP_PROGRAMS.match(name):
        raise _fail(
            source,
            "[project]",
            f"the project name {name!r} is not allowed. Names are used for folders, accounts, services and the "
            "project's own command, so they must be simple (and names of programs such as mes, python or uv are "
            "kept for those programs).",
            "Use 2 to 31 characters: lowercase letters, digits and dashes, starting with a letter "
            '(for example "fitness" or "my-notes").',
        )
    return name


def validate_app_name(name: Any, *, source: str = APP_FILE, project: str | None = None) -> str:
    if not isinstance(name, str) or not APP_NAME_RE.match(name) or name in RESERVED_APP_NAMES \
            or NOT_APP_PROGRAMS.match(name):
        raise _fail(
            source,
            "[app]",
            f"the app name {name!r} is not allowed. App names are what agents type to reach an app, "
            "so they must be simple (and \"mes\" and the names of programs such as python are kept for those programs).",
            "Use 2 to 31 characters: lowercase letters, digits and dashes, starting with a letter.",
        )
    if project is not None and name == project:
        raise _fail(
            source, "[app]",
            f'the app is called "{name}", the same as the project. The project has its own command of that name, '
            "so an app with the same name would make every request ambiguous.",
            f'Call the app "{project}-<something>", for example "{project}-main".',
        )
    return name


def short_name(app: str, project: str) -> str:
    """``fitdata-garmin`` in ``fitdata`` -> ``garmin``; otherwise the whole app name."""
    prefix = f"{project}-"
    return app[len(prefix):] if app.startswith(prefix) and len(app) > len(prefix) else app


def table_prefix(short: str) -> str:
    return short.replace("-", "_") + "_"


# --------------------------------------------------------------------------- project.toml


def _removed_forms(data: dict[str, Any], source: str) -> None:
    """The old [apps.*] / [jobs.*] / [ui] data forms are refused with a plain migration message."""
    old_apps = data.get("apps")
    if isinstance(old_apps, dict):
        name = next(iter(old_apps), "<name>")
        package = str(name).replace("-", "_") if isinstance(name, str) else "<package>"
        raise ManifestError(
            f"{source} still has the old [apps.{name}] section. Apps are now Python packages, each with its own "
            "app.toml, and the project lists them under [project] apps. The project cannot be used until this is "
            "changed.",
            hint=f"Move the app into apps/{package}/ with an app.toml (see: mes guide apps), write apps = "
            f'["{package}"] under [project], and remove [apps.{name}].',
            code="manifest_old_apps",
        )
    old_jobs = data.get("jobs")
    if isinstance(old_jobs, dict):
        name = next(iter(old_jobs), "<name>")
        raise ManifestError(
            f"{source} still has [jobs.{name}]. Jobs now belong to the app that runs them, in its app.toml, so they "
            "are no longer read here.",
            hint=f"Move the job to [jobs.{name}] in the app's app.toml and drop the app's name from its run line. "
            "To change a schedule from the project, use [settings.<app>].",
            code="manifest_old_jobs",
        )
    ui = data.get("ui")
    if isinstance(ui, dict) and "data" in ui:
        name = (data.get("project") or {}).get("name") if isinstance(data.get("project"), dict) else None
        raise ManifestError(
            f"[ui] data is no longer used: every project has one shared database, "
            f"{name if isinstance(name, str) else '<project>'}.sqlite, in its data folder.",
            hint="Remove the data line from [ui].",
            code="manifest_old_ui_data",
        )


def _parse_columns(value: Any, source: str, where: str) -> tuple[Column, ...]:
    example = '{ id = "text", date = "text" }'
    if not isinstance(value, dict) or not value:
        raise _fail(source, where, '"columns" must be a table of column names and types, for example '
                    f"columns = {example}.", f"Write columns = {example}.")
    if len(value) > MAX_COLUMNS:
        raise _fail(source, where, f"a shared table can have at most {MAX_COLUMNS} columns.", "Split it in two tables.")
    out = []
    for name, kind in value.items():
        if not COLUMN_RE.match(name) or name == "source":
            raise _fail(source, where, f'the column name {name[:80]!r} is not allowed ("source" is added by mes itself, '
                        "to say which app wrote each row).",
                        "Use lowercase letters, digits and _, starting with a letter, for example distance_m.")
        if kind not in COLUMN_TYPES:
            raise _fail(source, where, f'the column "{name}" has the type {str(kind)[:40]!r}, which mes does not know.',
                        f"Use one of: {', '.join(COLUMN_TYPES)}.")
        out.append(Column(name, kind))
    return tuple(out)


def _parse_table(name: str, value: Any, source: str) -> SharedTable:
    where = f"[tables.{name}]"
    if not SHARED_NAME_RE.match(name) or "__" in name or name.startswith(("sqlite_", "mes_")):
        raise _fail(source, where, f"the table name {name[:60]!r} is not allowed.",
                    'Use lowercase letters, digits and single _, starting with a letter, not starting with "sqlite_" '
                    'or "mes_" (for example "activities").')
    table = _table(value, source, where, f'[tables.{name}] with key = ["id"] and columns = {{ id = "text" }}')
    _check_keys(table, TABLE_KEYS, source, where)
    if "columns" not in table:
        raise _fail(source, where, "the table has no columns.", 'Add for example columns = { id = "text", date = "text" }.')
    columns = _parse_columns(table["columns"], source, where)
    if "key" not in table:
        raise _fail(source, where, 'the table has no "key", so mes cannot tell when two rows are the same thing.',
                    f'Add key = ["{columns[0].name}"] (1 to {MAX_KEY} of its columns).')
    key_value = table["key"]
    names = {c.name: c for c in columns}
    if not isinstance(key_value, list) or not 1 <= len(key_value) <= MAX_KEY \
            or not all(isinstance(k, str) for k in key_value) or len(set(key_value)) != len(key_value):
        raise _fail(source, where, f'"key" must list 1 to {MAX_KEY} of the table\'s columns, each once.',
                    f'Write for example key = ["{columns[0].name}"].')
    for k in key_value:
        if k not in names:
            raise _fail(source, where, f'"key" names "{k[:60]}", which is not one of the table\'s columns.',
                        f"Use columns from: {', '.join(names)}.")
        if names[k].type not in KEY_TYPES:
            raise _fail(source, where, f'the key column "{k}" has the type {names[k].type}; key columns must be text '
                        "or integer.", f'Change "{k}" to "text" or "integer", or use another column as the key.')
    prefer: tuple[str, ...] = ()
    if "prefer" in table:
        prefer = _name_list(table["prefer"], APP_NAME_RE, source, where, "prefer", "app names", '["myproject-main"]',
                            limit=MAX_APPS)
        if not prefer:
            raise _fail(source, where, '"prefer" must name at least one app.',
                        "Name the app whose values win, or remove the prefer line.")
    return SharedTable(name=name, columns=columns, key=tuple(key_value), prefer=prefer)


def _parse_view(name: str, value: Any, source: str) -> SharedView:
    where = f"[views.{name}]"
    if not SHARED_NAME_RE.match(name) or "__" in name or name.startswith(("sqlite_", "mes_")):
        raise _fail(source, where, f"the view name {name[:60]!r} is not allowed.",
                    'Use lowercase letters, digits and single _, starting with a letter (for example "weekly").')
    table = _table(value, source, where, f'[views.{name}] with sql = "SELECT ..."')
    _check_keys(table, VIEW_KEYS, source, where)
    sql = table.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise _fail(source, where, 'the view has no "sql".', 'Add sql = "SELECT ..." (one read-only SELECT).')
    if len(sql) > MAX_VIEW_SQL:
        raise _fail(source, where, f"the SQL is longer than {MAX_VIEW_SQL} characters.", "Keep the view shorter.")
    if any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in sql):
        raise _fail(source, where, "the SQL contains invisible control characters.", "Remove them.")
    if ";" in sql:
        raise _fail(source, where, 'the SQL contains ";". A view is exactly one SELECT, and ";" could start another '
                    "statement, so it is never accepted (not even inside text).",
                    'Remove the ";" (also at the end).')
    if "--" in sql or "/*" in sql:
        raise _fail(source, where, "the SQL contains a comment (-- or /*). Comments could hide the end of the "
                    "statement in the schema script, so they are not accepted in views.",
                    "Remove the comment.")
    if not _VIEW_START_RE.match(sql):
        raise _fail(source, where, "the SQL must start with SELECT or WITH: a view only reads.",
                    'Write sql = "SELECT ...".')
    return SharedView(name=name, sql=sql.strip())


def _parse_memory(value: Any, source: str, where: str) -> str:
    if not isinstance(value, str) or not _MEMORY_RE.match(value):
        raise _fail(
            source, where,
            f'"memory" = {value!r} is not a size mes understands. It caps how much memory the job may use.',
            'Write a whole number (at most 7 digits) followed by K, M or G, for example memory = "256M".',
        )
    return value


def _parse_timeout(value: Any, source: str, where: str) -> str:
    if not isinstance(value, str) or not _TIMEOUT_RE.match(value):
        raise _fail(
            source, where,
            f'"timeout" = {value!r} is not a duration mes understands. It stops a job that runs too long.',
            'Write a whole number (at most 6 digits) followed by s, m or h, for example timeout = "15m".',
        )
    return value


def _parse_schedule(value: Any, source: str, where: str) -> str:
    schedule = _string(value, source, where, "schedule", "*-*-* 06:00:00")
    problem = None if _SCHEDULE_RE.match(schedule) and len(schedule) <= 200 else "it has characters a calendar expression cannot have"
    problem = problem or calendar_problem(schedule)
    if problem:
        raise _fail(
            source, where,
            f'"schedule" = {schedule!r} is not a time mes can use ({problem}), so the job would never start.',
            'Use a calendar expression such as "daily", "hourly", "Mon..Fri 08:00" or '
            '"*-*-* 06:00:00" (every day at 06:00).',
        )
    return schedule


def _parse_hosts(value: Any, source: str, where: str, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(h, str) for h in value):
        raise _fail(
            source, where,
            f'"{key}" must be ' + ("false (no internet) or " if key == "network" else "") + "a list of the host names the job needs.",
            f'Write {key} = ["api.example.com"]' + (", or network = false." if key == "network" else "."),
        )
    hosts = []
    for host in value:
        h = host.strip().lower().rstrip(".")
        if "://" in host or "/" in host or not HOST_RE.match(h) or h.replace(".", "").isdigit():
            raise _fail(source, where, f'"{key}" contains {host[:80]!r}, which is not a plain host name.',
                        f'List only host names, without https:// or paths, for example {key} = ["api.example.com"].')
        if "." not in h or h == "localhost" or h.endswith((".localhost", ".local", ".internal", ".lan")):
            raise _fail(
                source, where,
                f'"{key}" contains {host[:80]!r}, which names this computer or its local network, not a service on '
                "the internet. Jobs may only reach the internet hosts they list.",
                f'List full internet host names, for example {key} = ["api.example.com"].',
            )
        hosts.append(h)
    if len(hosts) > MAX_HOSTS:
        raise _fail(source, where, f'"{key}" lists more than {MAX_HOSTS} hosts.', "List only the hosts the job really needs.")
    return tuple(dict.fromkeys(hosts))


def _parse_settings(value: Any, source: str) -> dict[str, dict[str, JobSetting]]:
    settings = _table(value, source, "[settings]", "[settings.<app name>]")
    out: dict[str, dict[str, JobSetting]] = {}
    for app, body in settings.items():
        where = f"[settings.{app}]"
        if not isinstance(app, str) or not APP_NAME_RE.match(app):
            hint = (f'Use the app\'s name (from its app.toml), not its package name: for example [settings.'
                    f'{str(app).replace("_", "-")}].' if isinstance(app, str) and PACKAGE_RE.match(app)
                    else "Use the app's name, as in its app.toml [app] name.")
            raise _fail(source, where, f"{str(app)[:60]!r} is not an app name.", hint)
        body = _table(body, source, where, f"[settings.{app}] with jobs.<job>.schedule = \"...\"")
        _check_keys(body, SETTINGS_KEYS, source, where)
        jobs = _table(body.get("jobs", {}), source, where + " jobs", f"[settings.{app}.jobs.<job>]")
        per: dict[str, JobSetting] = {}
        for job, keys in jobs.items():
            jwhere = f"[settings.{app}] jobs.{job}"
            if not isinstance(job, str) or not JOB_NAME_RE.match(job):
                raise _fail(source, jwhere, f"{str(job)[:40]!r} is not a job name.", "Use the job's name from the app's app.toml.")
            keys = _table(keys, source, jwhere, f'jobs.{job}.schedule = "daily"')
            _check_keys(keys, SETTINGS_JOB_KEYS, source, jwhere)
            enabled = keys.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                raise _fail(source, jwhere, '"enabled" can only be true or false.', f"Write jobs.{job}.enabled = false.")
            per[job] = JobSetting(
                schedule=_parse_schedule(keys["schedule"], source, jwhere) if "schedule" in keys else None,
                enabled=enabled,
                memory=_parse_memory(keys["memory"], source, jwhere) if "memory" in keys else None,
                timeout=_parse_timeout(keys["timeout"], source, jwhere) if "timeout" in keys else None,
                add_network=_parse_hosts(keys["add_network"], source, jwhere, "add_network") if "add_network" in keys else (),
            )
        out[app] = per
    return out


def _parse_ui(value: Any, source: str) -> UI:
    table = _table(value, source, "[ui]", '[ui] followed by dashboards = ["overview"]')
    _check_keys(table, UI_KEYS, source, "[ui]")
    accent = table.get("accent")
    if accent is not None and (not isinstance(accent, str) or not UI_ACCENT_RE.match(accent)):
        raise _fail(source, "[ui]", f'"accent" is {accent!r}, which is not a colour written as # and six hex digits.',
                    'Write for example accent = "#2f6fdf", or remove the line.')
    dashboards = _name_list(table.get("dashboards", []), UI_DASHBOARD_RE, source, "[ui]", "dashboards",
                            "dashboard names", '["overview"]')
    tables = None
    if "tables" in table:
        tables = _name_list(table["tables"], UI_TABLE_RE, source, "[ui]", "tables", "table names", '["days"]')
    refresh = _name_list(table.get("refresh_after", []), JOB_ID_RE, source, "[ui]", "refresh_after",
                         "job ids (<app>.<job>)", '["myproject-main.rebuild"]')
    return UI(accent=accent.lower() if accent else None, dashboards=dashboards, tables=tables, refresh_after=refresh)


def _parse_storage(table: Any, source: str) -> Storage:
    table = _table(table, source, "[storage]", '[storage] followed by bucket = "archive"')
    _check_keys(table, STORAGE_KEYS, source, "[storage]")
    bucket = table.get("bucket")
    if not isinstance(bucket, str) or not BUCKET_NAME_RE.match(bucket):
        raise _fail(source, "[storage]", f'"bucket" = {bucket!r} is not a bucket name.',
                    'Use lowercase letters, digits and dashes, for example bucket = "archive".')
    return Storage(bucket=bucket)


def parse(data: dict[str, Any], *, source: str = MANIFEST_FILE, path: Path | None = None,
          sha256: str | None = None) -> ProjectFile:
    """``project.toml`` (as a dict) -> ``ProjectFile``. No app is read."""
    if not isinstance(data, dict):
        raise _fail(source, "", "it is not a table of settings.", "Start the file with a [project] section.")
    _removed_forms(data, source)
    _check_keys(data, TOP_KEYS, source, "")
    proj = data.get("project")
    if not isinstance(proj, dict):
        raise _fail(
            source, "",
            "it has no [project] section, so mes does not know which project this is.",
            'Add:\n[project]\nname = "myproject"\ntitle = "My project"\nversion = "0.1.0"\napps = ["myproject_main"]',
        )
    _check_keys(proj, PROJECT_KEYS, source, "[project]")
    if "name" not in proj:
        raise _fail(source, "[project]", "the project has no name.", 'Add name = "myproject" under [project].')
    name = validate_project_name(proj["name"], source=source)
    title = _title(proj, name, source, "[project]")
    version = _version(proj, source, "[project]")
    if "apps" not in proj:
        raise ManifestError(
            f"{source} does not list its apps, so nothing knows which commands, jobs and dashboards the project has.",
            hint='Add apps = ["<package>"] under [project].',
            code="manifest_no_apps",
        )
    packages = _name_list(proj["apps"], PACKAGE_RE, source, "[project]", "apps", "Python package names",
                          f'["{name.replace("-", "_")}_main"]', limit=MAX_APPS)
    if not packages:
        raise _fail(source, "[project]", '"apps" lists no app, so the project would do nothing.',
                    f'List at least one package, for example apps = ["{name.replace("-", "_")}_main"].')
    if "mes" in packages:
        raise _fail(source, "[project]", '"mes" is the framework itself, not an app.', "Remove it from apps.")
    tables_raw = _table(data.get("tables", {}), source, "[tables]", "[tables.<name>]")
    if len(tables_raw) > MAX_TABLES:
        raise _fail(source, "[tables]", f"a project can have at most {MAX_TABLES} shared tables.", "Use fewer tables.")
    tables = {t: _parse_table(t, v, source) for t, v in tables_raw.items()}
    views_raw = _table(data.get("views", {}), source, "[views]", "[views.<name>]")
    if len(views_raw) > MAX_VIEWS:
        raise _fail(source, "[views]", f"a project can have at most {MAX_VIEWS} views.", "Use fewer views.")
    views = {v: _parse_view(v, body, source) for v, body in views_raw.items()}
    clash = sorted(set(tables) & set(views))
    if clash:
        raise _fail(source, f"[views.{clash[0]}]", f'"{clash[0]}" is both a shared table and a view.',
                    "Give the view another name.")
    settings = _parse_settings(data["settings"], source) if "settings" in data else {}
    ui = _parse_ui(data["ui"], source) if "ui" in data else None
    storage = _parse_storage(data["storage"], source) if "storage" in data else None
    return ProjectFile(name=name, title=title, version=version, packages=packages, tables=tables, views=views,
                       settings=settings, ui=ui, storage=storage, path=path, sha256=sha256)


def _toml(text: str, source: str, what: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(
            f"{source} has a typing mistake{_toml_where(e)}, so {what} cannot be read. "
            "The project cannot be used until that line is fixed.",
            hint="Your assistant can fix that line (often a quote or a bracket that is not closed).",
            detail=str(e),
        ) from None


def loads(text: str, *, source: str = MANIFEST_FILE, path: Path | None = None) -> ProjectFile:
    return parse(_toml(text, source, "the project's settings"), source=source, path=path,
                 sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


_TOML_AT = re.compile(r"\(at line (\d+), column (\d+)\)")
_TOML_PROBLEMS = (
    ("Expected ']'", "a bracket is not closed"),
    ("Unterminated string", "a quote is not closed"),
    ("Illegal character", "it has a character that is not allowed there"),
    ("Cannot overwrite a value", "a setting is written twice"),
    ("Cannot declare", "a section is written twice"),
    ("Invalid value", "a value is not written correctly (text needs quotes)"),
    ("Invalid statement", "a line is not written as name = value"),
    ("Expected '=' after a key", "a setting has no '=' after its name"),
)


def _toml_where(e: tomllib.TOMLDecodeError) -> str:
    """ " on line 23 (a bracket is not closed)" from a TOML error, as far as it can be told."""
    text = str(e)
    m = _TOML_AT.search(text)
    where = f" on line {m.group(1)}" if m else ""
    what = next((plain for key, plain in _TOML_PROBLEMS if key in text), None)
    return where + (f" ({what})" if what else "")


def _read_small(p: Path, limit: int, what: str, hint: str) -> bytes:
    """A regular file (never a link), at most ``limit`` bytes; larger files are refused unread."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        raise NotFound(f"There is no {p.name} at {p}.", hint=hint) from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ManifestError(f"{what} ({p.name}) is not a plain file (it may be a link), so mes will not read it.",
                            hint="Replace it with the real file.", code="manifest_not_a_file")
    if st.st_size > limit:
        raise ManifestError(f"{p.name} is larger than {limit // 1024} KB, so mes will not read it.",
                            hint=f"{p.name} only holds settings; keep it short.")
    with open(p, "rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ManifestError(f"{p.name} is larger than {limit // 1024} KB, so mes will not read it.",
                            hint=f"{p.name} only holds settings; keep it short.")
    return data


def _project_path(path: str | Path) -> Path:
    p = Path(path)
    if p.is_dir():
        p = p / MANIFEST_FILE
    if not p.is_file():
        raise NotFound(
            f"There is no {MANIFEST_FILE} at {p}. This file describes the project, so mes cannot work with it yet.",
            hint="Run the command from inside the project's folder, or create one with: mes new <name>",
        )
    return p


def load_file(path: str | Path) -> ProjectFile:
    """``project.toml`` alone (a file, or a folder holding one)."""
    p = _project_path(path)
    raw = _read_small(p, MAX_MANIFEST_BYTES, "The project's settings file",
                      "Run the command from inside the project's folder.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError(f"{p.name} is not plain UTF-8 text, so mes cannot read it.",
                            hint="Save it as UTF-8 text.") from None
    return parse(_toml(text, p.name, "the project's settings"), source=p.name, path=p.resolve(),
                 sha256=hashlib.sha256(raw).hexdigest())


# --------------------------------------------------------------------------- app.toml


def _parse_run(value: Any, source: str, where: str) -> tuple[str, tuple[str, ...]]:
    example = "sync --days 3"
    text = _string(value, source, where, "run", example)
    try:
        words = tuple(shlex.split(text))
    except ValueError as e:
        raise _fail(source, where, f'"run" could not be split into words ({e}).', "Check that every quote is closed.") from None
    if not words:
        raise _fail(source, where, '"run" is empty.', f'Write the app\'s command, for example run = "{example}".')
    bad = [t for t in words if t in _SHELL_TOKENS or "$(" in t or "`" in t]
    if bad:
        raise _fail(
            source, where,
            f'"run" contains {bad[0]!r}. A job is not a shell command line: it always runs one of '
            "this app's own commands, so pipes, ';', '&&' and redirections cannot be used.",
            "Put the steps in one app command, or split them into jobs chained with after = \"<job>\".",
        )
    if not COMMAND_WORD_RE.match(words[0]):
        raise _fail(
            source, where,
            f'"run" starts with {words[0][:60]!r}, which is not the name of one of the app\'s commands. A job runs a '
            "command of this app: the first word is that command (no app name), then its options.",
            f'Write the command, then its options, for example run = "{example}".',
        )
    if any(len(w.encode("utf-8")) > 4096 for w in words) or len(words) > 63:
        raise _fail(source, where, '"run" is too long.', "Keep the command and its options short.")
    return text, words


def _parse_job(name: str, table: Any, source: str) -> JobDecl:
    where = f"[jobs.{name}]"
    if not isinstance(name, str) or not JOB_NAME_RE.match(name):
        raise _fail(source, where, f"the job name {str(name)[:40]!r} is not allowed.",
                    "Use lowercase letters, digits and dashes, starting with a letter (for example \"sync\").")
    table = _table(table, source, where, f'[jobs.{name}] followed by run = "..."')
    _check_keys(table, JOB_KEYS, source, where)
    if "run" not in table:
        raise _fail(source, where, 'the job has no "run" setting, so mes does not know what to start.',
                    'Add run = "<command>", for example run = "sync".')
    run, argv = _parse_run(table["run"], source, where)
    triggers = [k for k in TRIGGER_KEYS if k in table]
    if len(triggers) != 1:
        problem = "the job has no trigger, so it would never run." if not triggers else (
            f"the job has more than one trigger ({', '.join(triggers)}), so it is unclear when it should run.")
        raise _fail(source, where, problem,
                    'Give it exactly one of: schedule = "<when>", after = "<other job>", manual = true or webhook = true.')
    trigger = triggers[0]
    schedule = after = None
    if trigger == "schedule":
        schedule = _parse_schedule(table["schedule"], source, where)
    elif trigger == "after":
        after = _string(table["after"], source, where, "after", "sync")
        if not (JOB_NAME_RE.match(after) or JOB_ID_RE.match(after)):
            raise _fail(source, where, f'"after" = {after[:80]!r} is not a job name or a job id.',
                        'Write after = "<job>" for a job of this app, or after = "<app>.<job>" for another app\'s job.')
    elif table[trigger] is not True:
        raise _fail(source, where, f'"{trigger}" can only be true.', f"Write {trigger} = true, or use another trigger instead.")
    memory = _parse_memory(table.get("memory", DEFAULT_MEMORY), source, where)
    timeout = _parse_timeout(table.get("timeout", DEFAULT_TIMEOUT), source, where)
    network_value = table.get("network", False)
    network = () if network_value is False else _parse_hosts(network_value, source, where, "network")
    writes_value = table.get("writes", list(DEFAULT_WRITES))
    if not isinstance(writes_value, list) or any(w not in WRITE_TARGETS for w in writes_value):
        raise _fail(
            source, where,
            f'"writes" = {writes_value!r} is not allowed. It says which of the project\'s folders the job may change.',
            'Use a list with "data" and/or "state", for example writes = ["data", "state"], or [] for none.',
        )
    return JobDecl(name=name, run=run, argv=argv, trigger=trigger, schedule=schedule, after=after, memory=memory,
                   timeout=timeout, network=network, writes=tuple(dict.fromkeys(writes_value)))


def _parse_app_ui(value: Any, source: str, data: AppData, app_folder: Path | None) -> AppUI:
    table = _table(value, source, "[ui]", '[ui] followed by dashboards = ["<slug>"]')
    _check_keys(table, APP_UI_KEYS, source, "[ui]")
    dashboards = _name_list(table.get("dashboards", []), UI_DASHBOARD_RE, source, "[ui]", "dashboards",
                            "dashboard names", '["walks"]')
    if app_folder is not None:
        for slug in dashboards:
            f = app_folder / "dashboards" / f"{slug}.yaml"
            try:
                st = os.lstat(f)
                ok = stat.S_ISREG(st.st_mode)
            except OSError:
                ok = False
            if not ok:
                raise _fail(source, "[ui]", f'"dashboards" names "{slug}", but the app has no file dashboards/{slug}.yaml.',
                            f"Add dashboards/{slug}.yaml to the app's folder, or remove \"{slug}\" from the list.")
    tables = _name_list(table.get("tables", []), APP_TABLE_RE, source, "[ui]", "tables", "table names", '["walks_log"]')
    extra = [t for t in tables if t not in data.tables]
    if extra:
        raise _fail(source, "[ui]", f'"tables" names "{extra[0]}", which is not one of the app\'s own tables '
                    "([data] tables).", f'Add "{extra[0]}" to [data] tables, or remove it from [ui] tables.')
    contributes_raw = _table(table.get("contributes", {}), source, "[ui.contributes]", "[ui.contributes.<dashboard>]")
    contributes = []
    own = {f"dashboards/{s}.yaml" for s in dashboards}
    for slug, body in contributes_raw.items():
        where = f"[ui.contributes.{slug}]"
        if not isinstance(slug, str) or not UI_DASHBOARD_RE.match(slug):
            raise _fail(source, where, f"{str(slug)[:60]!r} is not a dashboard name.", "Use the dashboard's file name without .yaml.")
        body = _table(body, source, where, f'[ui.contributes.{slug}] followed by sections = "dashboards/<file>.yaml"')
        _check_keys(body, CONTRIBUTES_KEYS, source, where)
        path = body.get("sections")
        if not isinstance(path, str) or not SECTIONS_PATH_RE.match(path):
            raise _fail(source, where, f'"sections" = {str(path)[:80]!r} is not a file in the app\'s dashboards folder.',
                        'Write sections = "dashboards/<file>.yaml".')
        if path in own:
            raise _fail(source, where, f'"sections" points at {path}, which is one of the app\'s own dashboards.',
                        "Put the contributed sections in a file of their own.")
        contributes.append((slug, path))
    return AppUI(dashboards=dashboards, tables=tables, contributes=tuple(contributes))


def parse_app(data: dict[str, Any], *, source: str = APP_FILE, path: Path | None = None,
              package: str | None = None) -> AppFile:
    """``app.toml`` (as a dict) -> ``AppFile``. ``package`` is the package it belongs to (for the
    check that ``cli`` lives inside it)."""
    if not isinstance(data, dict):
        raise _fail(source, "", "it is not a table of settings.", "Start the file with an [app] section.")
    _check_keys(data, APP_TOP_KEYS, source, "")
    app = data.get("app")
    if not isinstance(app, dict):
        raise _fail(source, "", "it has no [app] section, so mes does not know which app this is.",
                    'Add:\n[app]\nname = "myproject-main"\ncli = "myproject_main.cli:app"')
    _check_keys(app, APP_KEYS, source, "[app]")
    if "name" not in app:
        raise _fail(source, "[app]", "the app has no name.", 'Add name = "<project>-<app>" under [app].')
    name = validate_app_name(app["name"], source=source)
    title = _title(app, name, source, "[app]")
    version = _version(app, source, "[app]")
    cli = app.get("cli")
    command = app.get("command")
    if (cli is None) == (command is None):
        raise _fail(source, "[app]", 'the app must have exactly one of "cli" (a mes.App, as module:object) or '
                    '"command" (the name of its own console script).',
                    f'Write cli = "{(package or "mypackage")}.cli:app", or command = "{name}".')
    if cli is not None:
        if not isinstance(cli, str) or not CLI_REF_RE.match(cli):
            raise _fail(source, "[app]", f'"cli" = {str(cli)[:80]!r} is not written as module:object.',
                        f'Write for example cli = "{package or "mypackage"}.cli:app".')
        module = cli.split(":", 1)[0]
        if package is not None and module != package and not module.startswith(package + "."):
            raise _fail(source, "[app]", f'"cli" names the module {module}, which is not inside the app\'s package '
                        f"{package}.", f'Write cli = "{package}.cli:app".')
    if command is not None:
        if not isinstance(command, str) or not CLI_NAME_RE.match(command) or NOT_APP_PROGRAMS.match(command):
            raise _fail(
                source, "[app]",
                f'"command" = {str(command)[:80]!r} is not the name of the app\'s own command-line program (the name in '
                "[project.scripts] of pyproject.toml). Names of programs that run any code, such as python or bash, "
                "are never accepted.",
                f'Write command = "{name}" and name the script the same in pyproject.toml.',
            )
    data_t = _table(data.get("data", {}), source, "[data]", '[data] followed by writes = ["<shared table>"]')
    _check_keys(data_t, DATA_KEYS, source, "[data]")
    folder = data_t.get("folder")
    if folder is not None and (not isinstance(folder, str) or not FOLDER_RE.match(folder) or folder in RESERVED_FOLDERS):
        raise _fail(source, "[data]", f'"folder" = {str(folder)[:60]!r} cannot be the app\'s folder ('
                    f"{', '.join(RESERVED_FOLDERS)} are kept for the project).",
                    'Use lowercase letters, digits, "_" and "-", for example folder = "walks", or leave it out.')
    writes = _name_list(data_t.get("writes", []), SHARED_NAME_RE, source, "[data]", "writes", "shared table names",
                        '["activities"]', limit=MAX_TABLES)
    tables = _name_list(data_t.get("tables", []), APP_TABLE_RE, source, "[data]", "tables", "table names",
                        '["walks_log"]')
    for t in tables:
        if "__" in t:
            raise _fail(source, "[data]", f'the table name "{t}" has "__", which is kept for the shared tables.',
                        "Use single underscores.")
    rebuild = data_t.get("rebuild")
    if rebuild is not None and (not isinstance(rebuild, str) or not COMMAND_WORD_RE.match(rebuild)):
        raise _fail(source, "[data]", f'"rebuild" = {str(rebuild)[:60]!r} is not the name of one of the app\'s commands.',
                    'Write rebuild = "<command>" (a write command that rebuilds what the app wrote), or leave it out.')
    app_data = AppData(folder=folder, writes=writes, tables=tables, rebuild=rebuild)
    jobs_t = _table(data.get("jobs", {}), source, "[jobs]", '[jobs.<name>] followed by run = "<command>"')
    if len(jobs_t) > MAX_JOBS_PER_APP:
        raise _fail(source, "[jobs]", f"an app can have at most {MAX_JOBS_PER_APP} jobs.", "Use fewer jobs.")
    jobs = {jname: _parse_job(jname, jt, source) for jname, jt in jobs_t.items()}
    app_folder = path.parent if path is not None else None
    ui = _parse_app_ui(data["ui"], source, app_data, app_folder) if "ui" in data else AppUI()
    return AppFile(name=name, title=title, version=version, cli=cli, command=command, data=app_data, jobs=jobs,
                   ui=ui, path=path)


def loads_app(text: str, *, source: str = APP_FILE, path: Path | None = None, package: str | None = None) -> AppFile:
    return parse_app(_toml(text, source, "the app's settings"), source=source, path=path, package=package)


def load_app_file(path: str | Path, *, source: str | None = None, package: str | None = None) -> AppFile:
    p = Path(path)
    shown = source or str(p)
    raw = _read_small(p, MAX_APP_TOML_BYTES, f"The app's settings file {shown}",
                      "Every app has an app.toml next to its __init__.py; run: mes apps sync")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError(f"{shown} is not plain UTF-8 text, so mes cannot read it.", hint="Save it as UTF-8 text.") from None
    return parse_app(_toml(text, shown, "the app's settings"), source=shown, path=p, package=package)


# --------------------------------------------------------------------------- finding the apps


_LOCK_CHANGED = ("project.toml changed after the lock was written", "the lock lists other apps than project.toml")


def _stale(detail: str) -> ManifestError:
    if detail in _LOCK_CHANGED:
        return ManifestError(
            "project.toml changed after the list of apps (apps.lock.json) was written, so mes will not guess "
            "which apps to run.", hint="Run: mes apps sync (it takes a second), then run this command again.",
            code="apps_lock_stale", detail=detail)
    return ManifestError(f"{LOCK_STALE_MESSAGE} ({detail})", hint=LOCK_STALE_HINT, code="apps_lock_stale",
                         detail=detail)


def _real_dir_with_app_toml(folder: Path) -> bool:
    try:
        st = os.lstat(folder)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return False
        ast = os.lstat(folder / APP_FILE)
        return stat.S_ISREG(ast.st_mode)
    except OSError:
        return False


def read_lock(root: Path, pf: ProjectFile) -> list[Located] | None:
    """The apps as ``apps.lock.json`` lists them, checked; ``None`` when there is no lock."""
    lock = root / LOCK_FILE
    try:
        st = os.lstat(lock)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise _stale("the lock is not a plain file")
    if st.st_size > MAX_LOCK_BYTES:
        raise _stale("the lock is larger than 64 KB")
    try:
        with open(lock, "rb") as f:
            raw = f.read(MAX_LOCK_BYTES + 1)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise _stale("the lock is not valid JSON") from None
    if len(raw) > MAX_LOCK_BYTES:
        raise _stale("the lock is larger than 64 KB")
    if not isinstance(data, dict) or sorted(data) != sorted(LOCK_KEYS) or data.get("v") != 1:
        raise _stale("the lock is not version 1 of the format")
    if data["project"] != pf.name:
        raise _stale("the lock is for another project")
    if data["project_toml_sha256"] != pf.sha256:
        raise _stale("project.toml changed after the lock was written")
    apps = data["apps"]
    if not isinstance(apps, list) or [a.get("package") if isinstance(a, dict) else None for a in apps] != list(pf.packages):
        raise _stale("the lock lists other apps than project.toml")
    out = []
    for a in apps:
        if sorted(a) != sorted(LOCK_APP_KEYS):
            raise _stale(f"the entry of {a.get('package')} has other keys than expected")
        path, name, program, in_project = a["path"], a["name"], a["program"], a["in_project"]
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path \
                or any(ord(c) < 32 or ord(c) == 127 for c in path):
            raise _stale(f"the path of {a['package']} is not a plain full path")
        if not isinstance(name, str) or not APP_NAME_RE.match(name) or not isinstance(in_project, bool) \
                or not isinstance(program, str) or not CLI_NAME_RE.match(program) or NOT_APP_PROGRAMS.match(program):
            raise _stale(f"the entry of {a['package']} is damaged")
        folder = Path(path)
        if in_project and folder != root / APPS_DIR / a["package"]:
            raise _stale(f"the in-project app {a['package']} is not in this project's apps folder")
        if not _real_dir_with_app_toml(folder):
            raise _stale(f"the folder of {a['package']} has no app.toml (or is a link)")
        out.append(Located(a["package"], folder, in_project, name, program))
    return out


def locate_apps(root: Path, pf: ProjectFile) -> tuple[list[Located], bool]:
    """``(apps, lock_used)``: from the lock when there is one, else ``<root>/apps/<package>``."""
    try:
        from_lock = read_lock(root, pf)
    except ManifestError as e:
        # A lock that is only out of date because project.toml changed (a comment, a new table, a
        # reordered apps list) tells nothing a reader needs when every app is in apps/: it is then
        # read exactly as if there were no lock. A damaged lock, or one that points outside apps/,
        # is still refused.
        if e.code != "apps_lock_stale" or e.detail not in _LOCK_CHANGED or not all(
                _real_dir_with_app_toml(root / APPS_DIR / p) for p in pf.packages):
            raise
        from_lock = None
    if from_lock is not None:
        return from_lock, True
    out = []
    for package in pf.packages:
        folder = root / APPS_DIR / package
        if not _real_dir_with_app_toml(folder):
            raise ManifestError(
                f"The app {package} is not inside this project's apps folder and the list of installed apps has not "
                "been written yet.",
                hint=LOCK_STALE_HINT,
                code="apps_lock_missing",
            )
        out.append(Located(package, folder, True))
    return out, False


# --------------------------------------------------------------------------- composing


def _pfail(pf: ProjectFile, where: str, problem: str, hint: str, code: str | None = None) -> ManifestError:
    return _fail(pf.path.name if pf.path else MANIFEST_FILE, where, problem, hint, code)


def _removed_app_references(pf: ProjectFile, apps: dict[str, Any]) -> None:
    """One message for every place project.toml still names an app that is not installed, when it
    names it in more than one place (typically after removing the app from [project] apps), so the
    person fixes them all at once instead of one error at a time."""
    packages = {a.package for a in apps.values()}
    places: dict[str, list[str]] = {}
    for t in pf.tables.values():
        for p in t.prefer:
            if p not in apps:
                places.setdefault(p, []).append(f"[tables.{t.name}] prefer")
    for key in pf.settings:
        if key not in apps and key not in packages:
            places.setdefault(key, []).append(f"[settings.{key}]")
    if pf.ui is not None:
        for job_id in pf.ui.refresh_after:
            app_name = job_id.split(".", 1)[0]
            if app_name not in apps:
                places.setdefault(app_name, []).append(f'[ui] refresh_after "{job_id}"')
    for name, where in places.items():
        if len(where) > 1:
            raise _pfail(pf, ", ".join(where), f'project.toml still names the app "{name}", which is not in '
                         "[project] apps, in these places.",
                         f"If {name} was removed from the project, remove these lines too. If not, add its package "
                         f"to [project] apps. Installed apps: {', '.join(apps) or 'none'}.")


def compose(pf: ProjectFile, located: list[Located], *, lock_used: bool = False) -> Project:
    """Read each app's app.toml and tie everything together (contract sections 0 to 3)."""
    root = pf.root
    files: list[tuple[Located, AppFile]] = []
    for loc in located:
        source = f"{APPS_DIR}/{loc.package}/{APP_FILE}" if loc.in_project else f"the app.toml of {loc.package}"
        af = load_app_file(loc.folder / APP_FILE, source=source, package=loc.package)
        validate_app_name(af.name, source=source, project=pf.name)
        if af.program == pf.name:
            raise _fail(source, "[app]", f'the app\'s program is called "{af.program}", like the project\'s own command.',
                        "Give the app's console script another name.")
        if loc.name is not None and (loc.name != af.name or loc.program != af.program):
            raise _stale(f"the app.toml of {loc.package} now says another name or program")
        files.append((loc, af))

    apps: dict[str, AppSpec] = {}
    words: dict[str, str] = {}
    folders: dict[str, str] = {}
    programs: dict[str, str] = {}
    for loc, af in files:
        source = f"{APPS_DIR}/{loc.package}/{APP_FILE}" if loc.in_project else f"the app.toml of {loc.package}"
        short = short_name(af.name, pf.name)
        if short in RESERVED_SHORT_NAMES:
            raise _fail(source, "[app]", f'the app\'s short name would be "{short}", which the project command or the '
                        "database uses for itself.", f'Call the app "{pf.name}-<something else>".')
        if table_prefix(short).startswith(RESERVED_TABLE_PREFIXES):
            raise _fail(source, "[app]", f'the app\'s tables would start with "{table_prefix(short)}", and names that '
                        "start with sqlite_ or mes_ belong to the database itself.",
                        f'Call the app "{pf.name}-<something else>".')
        if af.program in programs:
            raise _pfail(pf, "[project] apps", f'the apps {programs[af.program]} and {af.name} both name the program '
                         f'"{af.program}", so the jobs of one would run the other\'s program.',
                         "Give each app its own program ([app] cli or command in its app.toml).")
        programs[af.program] = af.name
        for w in dict.fromkeys((af.name, short)):
            if w in words:
                raise _pfail(pf, "[project] apps", f'the apps {words[w]} and {af.name} can both be reached as "{w}", '
                             "so a request could go to the wrong app.", "Rename one of the apps.")
            words[w] = af.name
        prefix = table_prefix(short)
        for other in apps.values():
            if prefix.startswith(other.prefix) or other.prefix.startswith(prefix):
                raise _pfail(pf, "[project] apps", f'the table prefixes of {other.name} ({other.prefix}) and {af.name} '
                             f"({prefix}) overlap, so one app could own the other's tables.",
                             "Rename one of the apps.")
        folder = af.data.folder or short
        if not FOLDER_RE.match(folder) or folder in RESERVED_FOLDERS:
            raise _fail(source, "[data]", f'the app\'s data folder would be "{folder}", which is kept for the project.',
                        'Set [data] folder = "<another name>".')
        if folder in folders:
            raise _pfail(pf, "[project] apps", f'the apps {folders[folder]} and {af.name} both keep their files in the '
                         f'folder "{folder}".', "Give one of them another [data] folder.")
        folders[folder] = af.name
        for t in af.data.tables:
            if not t.startswith(prefix):
                raise _fail(source, "[data]", f'the app table "{t}" does not start with the app\'s prefix "{prefix}", '
                            "so it could clash with another app's tables.", f'Rename it to "{prefix}{t}".')
        for t in af.data.writes:
            if t not in pf.tables:
                raise ManifestError(
                    f"The app {af.name} writes the shared table {t}, but project.toml has no [tables.{t}]. Add it, "
                    "with its columns, so every app writes the same columns.",
                    hint=f"Add [tables.{t}] with key and columns to project.toml, or remove \"{t}\" from the app's "
                    "[data] writes.", code="manifest_invalid",
                )
        apps[af.name] = AppSpec(
            package=loc.package, name=af.name, title=af.title, version=af.version, short=short, prefix=prefix,
            program=af.program, cli=af.cli, command=af.command, folder=loc.folder, in_project=loc.in_project,
            data=AppData(folder=folder, writes=af.data.writes, tables=af.data.tables, rebuild=af.data.rebuild),
            ui=af.ui,
        )

    _removed_app_references(pf, apps)
    warnings: list[str] = []
    for t in pf.tables.values():
        for spec in apps.values():
            if t.name.startswith(spec.prefix):
                raise _pfail(pf, f"[tables.{t.name}]", f'the shared table name starts with "{spec.prefix}", the table '
                             f"prefix of the app {spec.name}.", "Give the shared table another name.")
        writers = [a.name for a in apps.values() if t.name in a.data.writes]
        if not writers:
            warnings.append(f'No installed app writes the shared table "{t.name}", so it stays empty.')
        for p in t.prefer:
            if p not in apps:
                hint = ""
                pkg = next((a.name for a in apps.values() if a.package == p.replace("-", "_")), None)
                if pkg:
                    hint = f' Did you mean "{pkg}"?'
                raise _pfail(pf, f"[tables.{t.name}]", f'"prefer" names "{p}", which is not an installed app.',
                             f"Use app names from: {', '.join(apps)}.{hint}")
            if p not in writers:
                raise _pfail(pf, f"[tables.{t.name}]", f'"prefer" names {p}, which does not write this table.',
                             f'Add "{t.name}" to the [data] writes of {p}, or remove it from prefer.')
    for v in pf.views.values():
        for spec in apps.values():
            if v.name.startswith(spec.prefix):
                raise _pfail(pf, f"[views.{v.name}]", f'the view name starts with "{spec.prefix}", the table prefix of '
                             f"the app {spec.name}.", "Give the view another name.")

    # settings, keyed by app name
    for key, per in pf.settings.items():
        if key not in apps:
            by_package = next((a.name for a in apps.values() if a.package == key), None)
            by_short = next((a.name for a in apps.values() if a.short == key), None)
            if by_short:
                raise _pfail(pf, f"[settings.{key}]", f'"{key}" is the short name of the app {by_short}, but settings '
                             "use the app's full name.", f"Did you mean [settings.{by_short}]?")
            maybe = f" Did you mean [settings.{by_package}]?" if by_package else ""
            raise _pfail(pf, f"[settings.{key}]", f'"{key}" is not the name of one of this project\'s apps.',
                         f"Settings are keyed by app name: {', '.join(apps)}.{maybe}")
        known = files[[af.name for _, af in files].index(key)][1].jobs
        for job in per:
            if job not in known:
                raise _pfail(pf, f"[settings.{key}] jobs.{job}", f'the app {key} has no job called "{job}".',
                             f"Its jobs are: {', '.join(known) or 'none'}.")

    jobs: dict[str, Job] = {}
    disabled: list[str] = []
    all_ids: dict[str, Job] = {}
    for loc, af in files:
        source = f"{APPS_DIR}/{loc.package}/{APP_FILE}" if loc.in_project else f"the app.toml of {loc.package}"
        per = pf.settings.get(af.name, {})
        for decl in af.jobs.values():
            s = per.get(decl.name, JobSetting())
            trigger, schedule, after = decl.trigger, decl.schedule, decl.after
            if s.schedule is not None:
                trigger, schedule, after = "schedule", s.schedule, None
            if after is not None:
                target = after if "." in after else f"{af.name}.{after}"
                after = target
            network = decl.network
            if s.add_network:
                network = tuple(dict.fromkeys(network + s.add_network))
                if len(network) > MAX_HOSTS:
                    raise _pfail(pf, f"[settings.{af.name}] jobs.{decl.name}",
                                 f"with add_network the job would list more than {MAX_HOSTS} hosts.",
                                 "List fewer hosts.")
            job = Job(id=f"{af.name}.{decl.name}", name=decl.name, app=af.name, run=decl.run, argv=decl.argv,
                      trigger=trigger, schedule=schedule, after=after, memory=s.memory or decl.memory,
                      timeout=s.timeout or decl.timeout, network=network, writes=decl.writes)
            all_ids[job.id] = job
            if s.enabled is False:
                disabled.append(job.id)
            else:
                jobs[job.id] = job
    if len(jobs) > MAX_JOBS:
        raise _pfail(pf, "[project] apps", f"the apps have more than {MAX_JOBS} jobs together.", "Switch some off.")
    for job in all_ids.values():
        if job.after is None or job.id in disabled:
            continue
        where = f"[jobs.{job.name}] of {job.app}"
        if job.after == job.id:
            raise _pfail(pf, where, "the job is set to run after itself, so it would never start.",
                         "Point after to a different job.")
        if job.after not in all_ids:
            known = ", ".join(i for i in all_ids if i != job.id) or "none"
            raise _pfail(pf, where, f'it runs after "{job.after}", but there is no job with that id.',
                         f"Use one of: {known}.")
        if job.after in disabled:
            raise _pfail(pf, f"[settings.{all_ids[job.after].app}] jobs.{all_ids[job.after].name}",
                         f'the job {job.after} is switched off, but {job.id} waits for it, so {job.id} would never run.',
                         f"Switch {job.id} off too, or keep {job.after} on.")
    for job in jobs.values():
        seen = [job.id]
        cur = job
        while cur.after is not None:
            if cur.after in seen:
                loop = " -> ".join(seen + [cur.after])
                raise _pfail(pf, f"[jobs.{job.name}] of {job.app}",
                             f"these jobs wait for each other in a circle ({loop}), so none of them would ever start.",
                             "Give the first job in the chain a schedule or manual = true instead of after.")
            seen.append(cur.after)
            cur = jobs[cur.after]

    if pf.ui is not None:
        missing = [j for j in pf.ui.refresh_after if j not in jobs]
        if missing:
            raise _pfail(pf, "[ui]", f'"refresh_after" names "{missing[0]}", but there is no enabled job with that id.',
                         f"Use job ids from: {', '.join(jobs) or 'none'}.")
    project = Project(name=pf.name, title=pf.title, version=pf.version, packages=pf.packages, apps=apps, jobs=jobs,
                      disabled=tuple(disabled), tables=pf.tables, views=pf.views, ui=pf.ui, storage=pf.storage,
                      path=pf.path, lock_used=lock_used, warnings=tuple(warnings))
    if pf.ui is not None and pf.ui.tables is not None:
        full = Project(**{**project.__dict__, "ui": UI()}).allowlist()
        extra = [t for t in pf.ui.tables if t not in full]
        if extra:
            raise _pfail(pf, "[ui]", f'"tables" names "{extra[0]}", which is not a shared table, a view or a table an '
                         "app lets dashboards read.", f"Use names from: {', '.join(full) or 'none'}.")
    return project


def load(path: str | Path) -> Project:
    """A project with its apps: ``project.toml`` (a file or the folder holding it), the lock or
    the in-project apps, and every app's ``app.toml``."""
    pf = load_file(path)
    assert pf.root is not None
    located, lock_used = locate_apps(pf.root, pf)
    return compose(pf, located, lock_used=lock_used)


def find(start: str | Path | None = None) -> Path | None:
    cur = Path(start or Path.cwd()).resolve()
    if cur.is_file():
        cur = cur.parent
    for d in (cur, *cur.parents):
        candidate = d / MANIFEST_FILE
        if candidate.is_file():
            return candidate
        if (d / "pyproject.toml").is_file():
            return None
    return None
