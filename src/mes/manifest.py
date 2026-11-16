"""Reading and checking a project's ``project.toml``.

A **project** is one folder (usually one git repository) with a ``project.toml`` at its root. It
owns one or more apps (command-line tools), its jobs, its dashboards, its data and, on a server,
its own operating-system accounts. Example::

    [project]
    name = "fitdata"          # ^[a-z][a-z0-9-]{1,30}\\Z
    title = "Fitness"
    version = "0.1.0"

    [apps.fitdata]            # optional; default: one app named like the project
    cli = "fitdata"           # console script name inside the project's environment

    [jobs.sync]
    run = "fitdata sync --days 3"   # first word: an app of this project; then its command
    schedule = "*:0/10"             # or after = "<job>", manual = true, webhook = true
    network = ["connect.example.com"]

    [ui]                            # what the dashboards (my-easy-ui) may show
    data = "fitdata.sqlite"         # the project's SQLite file, inside its data folder
    tables = ["days", "activities"]
    dashboards = ["today", "sleep"]
    refresh_after = ["sync"]        # rebuild the dashboard data after these jobs succeed

    [storage]                       # optional
    bucket = "archive"              # a my-bucket-storage bucket inside the project's store

Every job has exactly one trigger: ``schedule``, ``after``, ``manual = true`` or ``webhook = true``.
Unknown keys are an error (a typo must never silently drop a safety limit). ``[ui]`` is read by
my-easy-ui too; mes checks it with the same rules so a mistake shows up in ``mes`` as well.

Public API
----------
Constants: ``MANIFEST_FILE`` (``"project.toml"``), ``PROJECT_NAME_RE`` (also ``APP_NAME_RE``),
``JOB_NAME_RE``, ``CLI_NAME_RE``, ``BUCKET_NAME_RE``, ``HOST_RE``, ``TOP_KEYS``,
``PROJECT_KEYS``, ``APP_KEYS``, ``JOB_KEYS``, ``TRIGGER_KEYS``, ``UI_KEYS``, ``STORAGE_KEYS``,
``DEFAULT_MEMORY`` (``"256M"``), ``DEFAULT_TIMEOUT`` (``"15m"``), ``DEFAULT_WRITES``,
``WRITE_TARGETS``, ``RESERVED_APP_NAMES`` (``mes``: the gate uses it for its own requests),
``NOT_APP_PROGRAMS``, ``NETWORK_NOTE_LOCAL``.

``validate_project_name(name) -> str``, ``validate_app_name(name) -> str``
    Return the name or raise ``ManifestError``.
``parse(data, *, source="project.toml", path=None) -> Project``
``loads(text, *, source="project.toml", path=None) -> Project``
``load(path) -> Project``   a ``project.toml`` file or a folder holding one; ``NotFound`` when absent.
``find(start=None) -> Path | None``  the nearest ``project.toml`` at or above ``start``; stops
    at a folder holding ``pyproject.toml`` without one.
``calendar_problem(text) -> str | None``  mes's own check of a systemd calendar expression.

``AppSpec`` (frozen): ``name``, ``cli``.
``Job`` (frozen): ``name``, ``run`` (text), ``app`` (the app it runs), ``argv`` (the words after
    the app name: ``argv[0]`` is the app's command, also ``job.command``), ``trigger``
    (``"schedule" | "after" | "manual" | "webhook"``), ``schedule``, ``after``, ``memory``,
    ``timeout``, ``network`` (tuple of host names; empty means no network), ``local_network``
    (host names on the person's own network, such as ``nas.home.arpa``: only these may resolve to
    private addresses; loopback and link-local addresses are never allowed), ``writes``.
    Properties ``timeout_seconds``, ``memory_bytes``, ``network_allowed``, ``hosts`` (both lists).
    ``to_dict()``.
``UI`` (frozen): ``data``, ``tables``, ``accent``, ``dashboards``, ``refresh_after``. ``to_dict()``.
``Storage`` (frozen): ``bucket``.
``Project`` (frozen): ``name``, ``title``, ``version``, ``apps`` (dict name -> AppSpec),
    ``jobs`` (dict, file order), ``ui`` (``UI | None``), ``storage`` (``Storage | None``),
    ``path`` (of project.toml), ``root`` (its folder). Methods ``job(name)``, ``app(name)``,
    ``dependents(name)``, ``to_dict()``. ``Manifest`` is another name for ``Project``.
"""

from __future__ import annotations

import difflib
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mes.errors import ManifestError, NotFound

MANIFEST_FILE = "project.toml"
PROJECT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")
APP_NAME_RE = PROJECT_NAME_RE
JOB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}\Z")
# The first word after the app name in a job's run line: one of the app's own command names.
COMMAND_WORD_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}\Z")
CLI_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}\Z")
BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")
RESERVED_APP_NAMES = ("mes",)
TOP_KEYS = ("project", "apps", "jobs", "ui", "storage")
PROJECT_KEYS = ("name", "title", "version")
APP_KEYS = ("cli",)
JOB_KEYS = ("run", "schedule", "after", "manual", "webhook", "memory", "timeout", "network", "local_network",
            "writes")
TRIGGER_KEYS = ("schedule", "after", "manual", "webhook")
UI_KEYS = ("data", "tables", "accent", "dashboards", "refresh_after")
STORAGE_KEYS = ("bucket",)
UI_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
UI_DASHBOARD_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\Z")
UI_ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}\Z")
UI_MAX_ITEMS = 100
DEFAULT_MEMORY = "256M"
DEFAULT_TIMEOUT = "15m"
DEFAULT_WRITES = ("data", "state")
WRITE_TARGETS = ("data", "state")
DEFAULT_VERSION = "0.0.0"
MAX_HOSTS = 32
MAX_MANIFEST_BYTES = 256 * 1024
# Programs that run any code or install software: an app's program can never be one of them.
NOT_APP_PROGRAMS = re.compile(
    r"^(?:python[0-9.]*|pythonw|pip[0-9.]*|uv|uvx|pydoc[0-9.]*|ipython[0-9]*|pytest|py\.test|activate.*|"
    r"sh|bash|zsh|fish|dash|env|deactivate|mes|easy-ui|mybucket)\Z", re.IGNORECASE)
NETWORK_NOTE_LOCAL = (
    "On this computer (mes jobs run) mes cannot limit a job to the hosts it lists, so the job has "
    "full internet access here. On a Linux server set up with 'mes server plan', the job can only "
    "reach the listed hosts."
)

_MEMORY_RE = re.compile(r"^([1-9][0-9]{0,6})([KMG])\Z")
_TIMEOUT_RE = re.compile(r"^([1-9][0-9]{0,5})([smh])\Z")
_SCHEDULE_RE = re.compile(r"^[A-Za-z0-9*:,./~ +_@-]+\Z")
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST_RE = re.compile(rf"^(?=.{{1,253}}\Z){_HOST_LABEL}(?:\.{_HOST_LABEL})*\Z")
_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})\Z")


def is_host_name(h: str) -> bool:
    """A real host name: plain labels, a final label made of letters (so no number in any
    spelling, such as 0x7f.1 or 2130706433, can pass for a name), no label starting with 0x."""
    if not isinstance(h, str) or not HOST_RE.match(h) or "." not in h:
        return False
    labels = h.split(".")
    return bool(_TLD_RE.match(labels[-1])) and not any(label.startswith("0x") for label in labels)


_SHELL_TOKENS = {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "2>", "2>&1"}
_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3}
_SECONDS = {"s": 1, "m": 60, "h": 3600}


@dataclass(frozen=True)
class AppSpec:
    name: str
    cli: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "cli": self.cli}


@dataclass(frozen=True)
class Job:
    name: str
    run: str
    app: str
    argv: tuple[str, ...]
    trigger: str
    schedule: str | None = None
    after: str | None = None
    memory: str = DEFAULT_MEMORY
    timeout: str = DEFAULT_TIMEOUT
    network: tuple[str, ...] = ()
    writes: tuple[str, ...] = DEFAULT_WRITES
    local_network: tuple[str, ...] = ()

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
        return bool(self.network or self.local_network)

    @property
    def hosts(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.network + self.local_network))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "run": self.run,
            "app": self.app,
            "argv": list(self.argv),
            "trigger": self.trigger,
            "schedule": self.schedule,
            "after": self.after,
            "memory": self.memory,
            "timeout": self.timeout,
            "network": list(self.network) if self.network else False,
            # A host list is enforced by the system units of a server, not by 'mes jobs run'.
            "network_enforced": {"server": True, "this_computer": not self.network_allowed},
            "network_note": NETWORK_NOTE_LOCAL if self.network_allowed else None,
            "local_network": list(self.local_network),
            "writes": list(self.writes),
        }


@dataclass(frozen=True)
class UI:
    data: str | None = None
    tables: tuple[str, ...] = ()
    accent: str | None = None
    dashboards: tuple[str, ...] = ()
    refresh_after: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"data": self.data, "tables": list(self.tables), "accent": self.accent,
                "dashboards": list(self.dashboards), "refresh_after": list(self.refresh_after)}


@dataclass(frozen=True)
class Storage:
    bucket: str

    def to_dict(self) -> dict[str, Any]:
        return {"bucket": self.bucket}


@dataclass(frozen=True)
class Project:
    name: str
    title: str
    version: str
    apps: dict[str, AppSpec] = field(default_factory=dict)
    jobs: dict[str, Job] = field(default_factory=dict)
    path: Path | None = None
    ui: UI | None = None
    storage: Storage | None = None

    @property
    def root(self) -> Path | None:
        return self.path.parent if self.path else None

    def job(self, name: str) -> Job:
        try:
            return self.jobs[name]
        except KeyError:
            known = ", ".join(self.jobs) or "none"
            raise NotFound(
                f'The project "{self.name}" has no job called "{name}".',
                hint=f"Jobs in project.toml: {known}. Check the spelling, or add the job to project.toml.",
            ) from None

    def app(self, name: str) -> AppSpec:
        try:
            return self.apps[name]
        except KeyError:
            known = ", ".join(self.apps) or "none"
            raise NotFound(
                f'The project "{self.name}" has no app called "{name}".',
                hint=f"Its apps are: {known}.",
            ) from None

    def dependents(self, name: str) -> list[Job]:
        return [j for j in self.jobs.values() if j.after == name]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "path": str(self.path) if self.path else None,
            "apps": [a.to_dict() for a in self.apps.values()],
            "jobs": [j.to_dict() for j in self.jobs.values()],
            "ui": self.ui.to_dict() if self.ui else None,
            "storage": self.storage.to_dict() if self.storage else None,
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


def _fail(source: str, where: str, problem: str, hint: str) -> ManifestError:
    place = f"{source}, {where}" if where else source
    return ManifestError(
        f"There is a problem in {place}: {problem} The project cannot use this file until it is fixed.",
        hint=hint,
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


def validate_project_name(name: Any, *, source: str = MANIFEST_FILE) -> str:
    if not isinstance(name, str) or not PROJECT_NAME_RE.match(name):
        raise _fail(
            source,
            "[project]",
            f"the project name {name!r} is not allowed. Names are used for folders, accounts and services, "
            "so they must be simple.",
            "Use 2 to 31 characters: lowercase letters, digits and dashes, starting with a letter "
            '(for example "fitness" or "my-notes").',
        )
    return name


def validate_app_name(name: Any, *, source: str = MANIFEST_FILE) -> str:
    if not isinstance(name, str) or not APP_NAME_RE.match(name) or name in RESERVED_APP_NAMES:
        raise _fail(
            source,
            "[apps]",
            f"the app name {name!r} is not allowed. App names are what agents type to reach an app, "
            "so they must be simple (and \"mes\" is kept for mes itself).",
            "Use 2 to 31 characters: lowercase letters, digits and dashes, starting with a letter.",
        )
    return name


def _parse_apps(table: Any, project_name: str, source: str) -> dict[str, AppSpec]:
    if table is None:
        if project_name in RESERVED_APP_NAMES:
            raise _fail(source, "[project]", f'a project called "{project_name}" needs an [apps] section, because '
                        f'"{project_name}" is kept for mes itself.', "Choose another project name.")
        return {project_name: AppSpec(project_name, project_name)}
    if not isinstance(table, dict) or not table:
        raise _fail(source, "[apps]", "apps must be written as [apps.<name>] sections, and there must be at least one.",
                    f'Write for example:\n[apps.{project_name}]\ncli = "{project_name}"')
    if len(table) > 16:
        raise _fail(source, "[apps]", "a project can have at most 16 apps.", "Split the project in two.")
    apps: dict[str, AppSpec] = {}
    for name, spec in table.items():
        validate_app_name(name, source=source)
        where = f"[apps.{name}]"
        if not isinstance(spec, dict):
            raise _fail(source, where, "an app must be a section of settings.", f'Write [apps.{name}] followed by cli = "{name}".')
        _check_keys(spec, APP_KEYS, source, where)
        cli = spec.get("cli", name)
        if not isinstance(cli, str) or not CLI_NAME_RE.match(cli) or NOT_APP_PROGRAMS.match(cli):
            raise _fail(
                source, where,
                f'"cli" = {cli!r} is not the name of the app\'s own command-line program (the name in '
                "[project.scripts] of pyproject.toml). Names of programs that run any code, such as python or "
                "bash, are never accepted.",
                f'Write cli = "{name}" and name the script the same in pyproject.toml.',
            )
        apps[name] = AppSpec(name, cli)
    return apps


def _parse_run(value: Any, apps: dict[str, AppSpec], source: str, where: str) -> tuple[str, str, tuple[str, ...]]:
    example = f"{next(iter(apps))} sync --days 3"
    text = _string(value, source, where, "run", example)
    try:
        words = tuple(shlex.split(text))
    except ValueError as e:
        raise _fail(source, where, f'"run" could not be split into words ({e}).', "Check that every quote is closed.") from None
    if not words:
        raise _fail(source, where, '"run" is empty.', f'Write the app and its command, for example run = "{example}".')
    bad = [t for t in words if t in _SHELL_TOKENS or "$(" in t or "`" in t]
    if bad:
        raise _fail(
            source,
            where,
            f'"run" contains {bad[0]!r}. A job is not a shell command line: it always runs one of '
            "your project's own app commands, so pipes, ';', '&&' and redirections cannot be used.",
            "Put the steps in one app command, or split them into jobs chained with after = \"<job>\".",
        )
    app = words[0]
    if app not in apps:
        known = ", ".join(apps)
        raise _fail(
            source,
            where,
            f'"run" starts with {app!r}, which is not an app of this project. A job always runs one of the '
            "project's own apps, so the first word must be that app's name, then its command.",
            f'Start "run" with one of: {known} (for example run = "{example}").',
        )
    argv = words[1:]
    if not argv or not COMMAND_WORD_RE.match(argv[0]):
        shown = repr(argv[0]) if argv else "nothing"
        raise _fail(
            source,
            where,
            f'after the app name "{app}", "run" has {shown}, which is not the name of one of the app\'s commands. '
            "The second word must be the command (options come after it).",
            f'Write the app, then its command, for example run = "{example}".',
        )
    if any(len(w.encode("utf-8")) > 4096 for w in argv) or len(argv) > 63:
        raise _fail(source, where, '"run" is too long.', "Keep the command and its options short.")
    return text, app, argv


def _parse_job(name: str, table: Any, apps: dict[str, AppSpec], source: str) -> Job:
    where = f"[jobs.{name}]"
    if not JOB_NAME_RE.match(name):
        raise _fail(
            source,
            where,
            f"the job name {name!r} is not allowed.",
            "Use lowercase letters, digits and dashes, starting with a letter (for example \"sync\").",
        )
    if not isinstance(table, dict):
        raise _fail(source, where, "a job must be a table of settings.", f'Write it as [jobs.{name}] followed by run = "...".')
    _check_keys(table, JOB_KEYS, source, where)
    if "run" not in table:
        raise _fail(source, where, 'the job has no "run" setting, so mes does not know what to start.',
                    'Add run = "<app> <command>", for example run = "myapp sync".')
    run, app, argv = _parse_run(table["run"], apps, source, where)

    triggers = [k for k in TRIGGER_KEYS if k in table]
    if len(triggers) != 1:
        problem = "the job has no trigger, so it would never run." if not triggers else (
            f"the job has more than one trigger ({', '.join(triggers)}), so it is unclear when it should run."
        )
        raise _fail(
            source,
            where,
            problem,
            'Give it exactly one of: schedule = "<when>", after = "<other job>", manual = true or webhook = true.',
        )
    trigger = triggers[0]
    schedule = after = None
    if trigger == "schedule":
        schedule = _string(table["schedule"], source, where, "schedule", "*-*-* 06:00:00")
        problem = None if _SCHEDULE_RE.match(schedule) and len(schedule) <= 200 else "it has characters a calendar expression cannot have"
        problem = problem or calendar_problem(schedule)
        if problem:
            raise _fail(
                source,
                where,
                f'"schedule" = {schedule!r} is not a time mes can use ({problem}), so the job would '
                "never start.",
                'Use a calendar expression such as "daily", "hourly", "Mon..Fri 08:00" or '
                '"*-*-* 06:00:00" (every day at 06:00).',
            )
    elif trigger == "after":
        after = _string(table["after"], source, where, "after", "sync")
    elif table[trigger] is not True:
        raise _fail(source, where, f'"{trigger}" can only be true.',
                    f"Write {trigger} = true, or use another trigger instead.")

    memory = table.get("memory", DEFAULT_MEMORY)
    if not isinstance(memory, str) or not _MEMORY_RE.match(memory):
        raise _fail(
            source,
            where,
            f'"memory" = {memory!r} is not a size mes understands. It caps how much memory the job may use.',
            'Write a whole number (at most 7 digits) followed by K, M or G, for example memory = "256M".',
        )
    timeout = table.get("timeout", DEFAULT_TIMEOUT)
    if not isinstance(timeout, str) or not _TIMEOUT_RE.match(timeout):
        raise _fail(
            source,
            where,
            f'"timeout" = {timeout!r} is not a duration mes understands. It stops a job that runs too long.',
            'Write a whole number (at most 6 digits) followed by s, m or h, for example timeout = "15m".',
        )

    network_value = table.get("network", False)
    if network_value is False:
        network: tuple[str, ...] = ()
    elif isinstance(network_value, list) and network_value and all(isinstance(h, str) for h in network_value):
        hosts = []
        for host in network_value:
            h = host.strip().lower().rstrip(".")
            if "://" in host or "/" in host or not HOST_RE.match(h):
                raise _fail(
                    source,
                    where,
                    f'"network" contains {host!r}, which is not a plain host name.',
                    'List only host names, without https:// or paths, for example network = ["api.example.com"].',
                )
            if "." not in h or h == "localhost" or h.endswith((".localhost", ".local", ".internal", ".lan")):
                raise _fail(
                    source,
                    where,
                    f'"network" contains {host!r}, which names this computer or its local network, not a '
                    "service on the internet. Jobs may only reach the internet hosts they list.",
                    'List full internet host names, for example network = ["api.example.com"].',
                )
            if not is_host_name(h):
                raise _fail(
                    source,
                    where,
                    f'"network" contains {host!r}, which is a number, not a host name (numbers in any spelling, '
                    "such as 0x7f.1, can point at this computer).",
                    'List full internet host names, for example network = ["api.example.com"].',
                )
            hosts.append(h)
        if len(hosts) > MAX_HOSTS:
            raise _fail(source, where, f'"network" lists more than {MAX_HOSTS} hosts.',
                        "List only the hosts the job really needs.")
        network = tuple(dict.fromkeys(hosts))
    else:
        raise _fail(
            source,
            where,
            '"network" must be false (no internet) or a list of the host names the job needs.',
            'Write network = false, or list the hosts, for example network = ["api.example.com"].',
        )

    local_value = table.get("local_network", [])
    local: list[str] = []
    if not isinstance(local_value, list) or not all(isinstance(h, str) for h in local_value):
        raise _fail(source, where, '"local_network" must be a list of host names on your own network.',
                    'For example local_network = ["nas.home.arpa"], or leave it out.')
    for host in local_value:
        h = host.strip().lower().rstrip(".")
        if "://" in host or "/" in host or not is_host_name(h) or h.endswith(".localhost"):
            raise _fail(
                source, where,
                f'"local_network" contains {host!r}, which is not the name of another device on your network.',
                'List device names only, for example local_network = ["nas.home.arpa"]. This computer itself '
                "(localhost) can never be listed.")
        local.append(h)
    if len(local) + len(network) > MAX_HOSTS:
        raise _fail(source, where, f'"network" and "local_network" list more than {MAX_HOSTS} hosts together.',
                    "List only the hosts the job really needs.")
    local_network = tuple(h for h in dict.fromkeys(local) if h not in network)

    writes_value = table.get("writes", list(DEFAULT_WRITES))
    if not isinstance(writes_value, list) or any(w not in WRITE_TARGETS for w in writes_value):
        raise _fail(
            source,
            where,
            f'"writes" = {writes_value!r} is not allowed. It says which of the project\'s folders the job may change.',
            'Use a list with "data" and/or "state", for example writes = ["data", "state"], or [] for none.',
        )
    writes = tuple(dict.fromkeys(writes_value))

    return Job(name=name, run=run, app=app, argv=argv, trigger=trigger, schedule=schedule, after=after,
               memory=memory, timeout=timeout, network=network, writes=writes, local_network=local_network)


def _check_after(jobs: dict[str, Job], source: str) -> None:
    for job in jobs.values():
        if job.after is None:
            continue
        where = f"[jobs.{job.name}]"
        if job.after == job.name:
            raise _fail(source, where, "the job is set to run after itself, so it would never start.", "Point after to a different job.")
        if job.after not in jobs:
            known = ", ".join(n for n in jobs if n != job.name) or "none"
            raise _fail(
                source,
                where,
                f'it runs after "{job.after}", but there is no job with that name.',
                f"Use one of the jobs in this file ({known}), or add the missing job.",
            )
    for job in jobs.values():
        seen = [job.name]
        cur = job
        while cur.after is not None:
            if cur.after in seen:
                loop = " -> ".join(seen + [cur.after])
                raise _fail(
                    source,
                    f"[jobs.{job.name}]",
                    f"these jobs wait for each other in a circle ({loop}), so none of them would ever start.",
                    "Give the first job in the chain a schedule or manual = true instead of after.",
                )
            seen.append(cur.after)
            cur = jobs[cur.after]


def _name_list(value: Any, pattern: re.Pattern[str], source: str, key: str, what: str, example: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _fail(source, "[ui]", f'"{key}" must be a list of {what}, for example {key} = {example}.',
                    f"Write {key} = {example}.")
    bad = [v for v in value if not pattern.match(v)]
    if bad:
        raise _fail(source, "[ui]", f'"{key}" has {bad[0]!r}, which is not one of {what}.', f"Write {key} = {example}.")
    if len(value) > UI_MAX_ITEMS or len(set(value)) != len(value):
        raise _fail(source, "[ui]", f'"{key}" lists a name more than once (or more than {UI_MAX_ITEMS} names).',
                    "List each one once.")
    return tuple(value)


def _parse_ui(table: Any, jobs: dict[str, Job], source: str) -> UI:
    if not isinstance(table, dict):
        raise _fail(source, "[ui]", "[ui] must be a section of settings.",
                    'Write:\n[ui]\ndata = "project.sqlite"\ntables = ["days"]')
    _check_keys(table, UI_KEYS, source, "[ui]")
    data = table.get("data")
    if data is not None:
        ok = isinstance(data, str) and data.strip() and not data.startswith(("/", "~")) and "\\" not in data \
            and ".." not in data.split("/") and not any(ord(c) < 32 for c in data)
        if not ok:
            raise _fail(source, "[ui]", '"data" must be the name of a file inside the project\'s data folder, '
                        'for example data = "project.sqlite".',
                        'Write data = "project.sqlite" (no "/" at the start, no "..").')
    tables = _name_list(table.get("tables", []), UI_TABLE_RE, source, "tables", "table names", '["days"]')
    accent = table.get("accent")
    if accent is not None and (not isinstance(accent, str) or not UI_ACCENT_RE.match(accent)):
        raise _fail(source, "[ui]", f'"accent" is {accent!r}, which is not a colour written as # and six hex digits.',
                    'Write for example accent = "#2f6fdf", or remove the line.')
    dashboards = _name_list(table.get("dashboards", []), UI_DASHBOARD_RE, source, "dashboards", "dashboard names",
                            '["overview"]')
    refresh = _name_list(table.get("refresh_after", []), JOB_NAME_RE, source, "refresh_after", "job names", '["sync"]')
    missing = [j for j in refresh if j not in jobs]
    if missing:
        known = ", ".join(jobs) or "none"
        raise _fail(source, "[ui]", f'"refresh_after" names "{missing[0]}", but there is no job with that name.',
                    f"Use jobs from this file ({known}).")
    return UI(data=data, tables=tables, accent=accent.lower() if accent else None, dashboards=dashboards,
              refresh_after=refresh)


def _parse_storage(table: Any, source: str) -> Storage:
    if not isinstance(table, dict):
        raise _fail(source, "[storage]", "[storage] must be a section of settings.", 'Write:\n[storage]\nbucket = "archive"')
    _check_keys(table, STORAGE_KEYS, source, "[storage]")
    bucket = table.get("bucket")
    if not isinstance(bucket, str) or not BUCKET_NAME_RE.match(bucket):
        raise _fail(source, "[storage]", f'"bucket" = {bucket!r} is not a bucket name.',
                    'Use lowercase letters, digits and dashes, for example bucket = "archive".')
    return Storage(bucket=bucket)


# --------------------------------------------------------------------------- public


def parse(data: dict[str, Any], *, source: str = MANIFEST_FILE, path: Path | None = None) -> Project:
    if not isinstance(data, dict):
        raise _fail(source, "", "it is not a table of settings.", "Start the file with a [project] section.")
    _check_keys(data, TOP_KEYS, source, "")
    proj = data.get("project")
    if not isinstance(proj, dict):
        raise _fail(
            source,
            "",
            "it has no [project] section, so mes does not know which project this is.",
            'Add:\n[project]\nname = "myproject"\ntitle = "My project"\nversion = "0.1.0"',
        )
    _check_keys(proj, PROJECT_KEYS, source, "[project]")
    if "name" not in proj:
        raise _fail(source, "[project]", "the project has no name.", 'Add name = "myproject" under [project].')
    name = validate_project_name(proj["name"], source=source)
    title = _string(proj["title"], source, "[project]", "title", "My project") if "title" in proj else name
    if len(title) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in title):
        raise _fail(source, "[project]", "the title must be one line of at most 80 characters.", 'Write title = "My project".')
    version = _string(proj["version"], source, "[project]", "version", "0.1.0") if "version" in proj else DEFAULT_VERSION
    if len(version) > 40 or not re.fullmatch(r"[A-Za-z0-9.+_-]+", version):
        raise _fail(source, "[project]", f"the version {version!r} is not a plain version number.", 'Write version = "0.1.0".')

    apps = _parse_apps(data.get("apps"), name, source)
    jobs_table = data.get("jobs", {})
    if not isinstance(jobs_table, dict):
        raise _fail(source, "[jobs]", "jobs must be written as [jobs.<name>] sections.",
                    f'Write for example [jobs.sync] followed by run = "{next(iter(apps))} sync".')
    jobs = {jname: _parse_job(jname, jtable, apps, source) for jname, jtable in jobs_table.items()}
    _check_after(jobs, source)
    ui = _parse_ui(data["ui"], jobs, source) if "ui" in data else None
    storage = _parse_storage(data["storage"], source) if "storage" in data else None
    return Project(name=name, title=title, version=version, apps=apps, jobs=jobs, path=path, ui=ui, storage=storage)


def loads(text: str, *, source: str = MANIFEST_FILE, path: Path | None = None) -> Project:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(
            f"{source} has a typing mistake{_toml_where(e)}, so the project's settings cannot be read. "
            "The project cannot be used until that line is fixed.",
            hint="Your assistant can fix that line (often a quote or a bracket that is not closed).",
            detail=str(e),
        ) from None
    return parse(data, source=source, path=path)


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


def load(path: str | Path) -> Project:
    p = Path(path)
    if p.is_dir():
        p = p / MANIFEST_FILE
    if not p.is_file():
        raise NotFound(
            f"There is no {MANIFEST_FILE} at {p}. This file describes the project, so mes cannot work with it yet.",
            hint="Run the command from inside the project's folder, or create one with: mes new <name>",
        )
    if p.stat().st_size > MAX_MANIFEST_BYTES:
        raise ManifestError(f"{p.name} is larger than {MAX_MANIFEST_BYTES // 1024} KB, so mes will not read it.",
                            hint="project.toml only holds settings; keep it short.")
    return loads(p.read_text(encoding="utf-8"), source=p.name, path=p.resolve())


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
