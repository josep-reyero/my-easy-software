"""Reading and checking an app's ``app.toml``.

Example::

    [app]
    name = "myapp"
    title = "My app"
    version = "0.1.0"

    [jobs.sync]
    run = "sync --days 3"            # the app's own CLI subcommand and args
    schedule = "*-*-* 06:00:00"      # systemd OnCalendar expression
    network = ["api.example.com"]    # default false: no network

    [jobs.build]
    run = "build"
    after = "sync"                   # runs when sync has finished

Every job has exactly one trigger: ``schedule``, ``after`` or ``manual = true``.
Unknown keys are an error (a typo must never silently drop a safety limit).

Public API
----------
Constants: ``MANIFEST_FILE`` (``"app.toml"``), ``APP_NAME_RE``, ``JOB_NAME_RE``,
``TOP_KEYS``, ``APP_KEYS``, ``JOB_KEYS``, ``DEFAULT_MEMORY`` (``"256M"``),
``DEFAULT_TIMEOUT`` (``"15m"``), ``DEFAULT_WRITES`` (``("data", "state")``),
``WRITE_TARGETS``.

``validate_app_name(name) -> str``
    Returns the name or raises ``ManifestError`` (pattern ``^[a-z][a-z0-9-]{1,30}$``).

``parse(data: dict, *, source="app.toml", path=None) -> Manifest``
    Checks an already-parsed TOML table.
``loads(text, *, source="app.toml", path=None) -> Manifest``
    Parses TOML text.
``load(path) -> Manifest``
    ``path`` is an ``app.toml`` file or a directory containing one. Missing file
    raises ``NotFound``; invalid content raises ``ManifestError``.
``find(start=None) -> Path | None``
    Walks up from ``start`` (default: current directory) to the first
    ``app.toml``; stops at a directory holding ``pyproject.toml`` without one.

``Job`` (frozen dataclass)
    ``name``, ``run`` (text), ``argv`` (tuple of args for the app CLI, from
    ``shlex.split(run)``), ``trigger`` (``"schedule" | "after" | "manual"``),
    ``schedule`` (str | None), ``after`` (str | None), ``memory`` (e.g.
    ``"256M"``), ``timeout`` (e.g. ``"15m"``), ``network`` (tuple of allowed host
    names; empty tuple means no network; v0.1 cannot limit a job to those hosts, so a
    non-empty list means full internet access, see ``network_enforced``), ``writes`` (tuple, subset of
    ``("data", "state")``). Properties: ``timeout_seconds``, ``memory_bytes``,
    ``network_allowed``. ``to_dict()`` gives the JSON form (``network`` is
    ``false`` or a list).

``Manifest`` (frozen dataclass)
    ``name``, ``title``, ``version``, ``jobs`` (dict name -> Job, file order),
    ``path`` (Path | None). Methods: ``job(name)`` (raises ``NotFound``),
    ``dependents(name)`` (jobs with ``after = name``), ``to_dict()``.
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

MANIFEST_FILE = "app.toml"
APP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")
JOB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}\Z")
# The first word of a job's run line: one of the app's own command names.
COMMAND_WORD_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}\Z")
TOP_KEYS = ("app", "jobs")
APP_KEYS = ("name", "title", "version")
JOB_KEYS = ("run", "schedule", "after", "manual", "memory", "timeout", "network", "writes")
TRIGGER_KEYS = ("schedule", "after", "manual")
DEFAULT_MEMORY = "256M"
DEFAULT_TIMEOUT = "15m"
DEFAULT_WRITES = ("data", "state")
WRITE_TARGETS = ("data", "state")
DEFAULT_VERSION = "0.0.0"
NETWORK_NOTE = (
    "This version of mes cannot yet limit a job to the hosts it lists, so the job has full internet "
    "access. The list says which hosts it is meant to reach."
)

_MEMORY_RE = re.compile(r"^([1-9][0-9]{0,6})([KMG])\Z")
_TIMEOUT_RE = re.compile(r"^([1-9][0-9]{0,5})([smh])\Z")
_SCHEDULE_RE = re.compile(r"^[A-Za-z0-9*:,./~ +_@-]+\Z")
_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST_RE = re.compile(rf"^(?=.{{1,253}}\Z){_HOST_LABEL}(?:\.{_HOST_LABEL})*\Z")
_SHELL_TOKENS = {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "2>", "2>&1"}
_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3}
_SECONDS = {"s": 1, "m": 60, "h": 3600}


@dataclass(frozen=True)
class Job:
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
            "name": self.name,
            "run": self.run,
            "argv": list(self.argv),
            "trigger": self.trigger,
            "schedule": self.schedule,
            "after": self.after,
            "memory": self.memory,
            "timeout": self.timeout,
            "network": list(self.network) if self.network else False,
            # mes v0.1 cannot limit a job to its listed hosts: a list means full internet access.
            "network_enforced": not self.network,
            "network_note": NETWORK_NOTE if self.network else None,
            "writes": list(self.writes),
        }


@dataclass(frozen=True)
class Manifest:
    name: str
    title: str
    version: str
    jobs: dict[str, Job] = field(default_factory=dict)
    path: Path | None = None

    def job(self, name: str) -> Job:
        try:
            return self.jobs[name]
        except KeyError:
            known = ", ".join(self.jobs) or "none"
            raise NotFound(
                f'The app "{self.name}" has no job called "{name}".',
                hint=f"Jobs in app.toml: {known}. Check the spelling, or add the job to app.toml.",
            ) from None

    def dependents(self, name: str) -> list[Job]:
        return [j for j in self.jobs.values() if j.after == name]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "path": str(self.path) if self.path else None,
            "jobs": [j.to_dict() for j in self.jobs.values()],
        }


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
    On Linux, systemd checks the expression again before anything is installed.
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
        f"There is a problem in {place}: {problem} The app cannot use this file until it is fixed.",
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


def validate_app_name(name: Any, *, source: str = MANIFEST_FILE) -> str:
    if not isinstance(name, str) or not APP_NAME_RE.match(name):
        raise _fail(
            source,
            "[app]",
            f"the app name {name!r} is not allowed. Names are used for folders and services, "
            "so they must be simple.",
            "Use 2 to 31 characters: lowercase letters, digits and dashes, starting with a letter "
            '(for example "fitness" or "my-notes").',
        )
    return name


def _parse_run(value: Any, source: str, where: str) -> tuple[str, tuple[str, ...]]:
    text = _string(value, source, where, "run", "sync --days 3")
    try:
        argv = tuple(shlex.split(text))
    except ValueError as e:
        raise _fail(source, where, f'"run" could not be split into words ({e}).', "Check that every quote is closed.") from None
    if not argv:
        raise _fail(source, where, '"run" is empty.', 'Write the app command to run, for example run = "sync".')
    if not COMMAND_WORD_RE.match(argv[0]):
        raise _fail(
            source,
            where,
            f'"run" starts with {argv[0]!r}, which is not the name of one of the app\'s commands. A job '
            "always runs one of your app's own commands, so the first word must be that command's name "
            "(options come after it).",
            'Start "run" with the command, for example run = "sync --days 3".',
        )
    bad = [t for t in argv if t in _SHELL_TOKENS or "$(" in t or "`" in t]
    if bad:
        raise _fail(
            source,
            where,
            f'"run" contains {bad[0]!r}. A job is not a shell command line: it always runs one of '
            "your app's own commands, so pipes, ';', '&&' and redirections cannot be used.",
            "Put the steps in one app command, or split them into jobs chained with after = \"<job>\".",
        )
    return text, argv


def _parse_job(name: str, table: Any, source: str) -> Job:
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
        raise _fail(source, where, 'the job has no "run" setting, so mes does not know what to start.', 'Add run = "<app command>", for example run = "sync".')
    run, argv = _parse_run(table["run"], source, where)

    triggers = [k for k in TRIGGER_KEYS if k in table]
    if len(triggers) != 1:
        problem = "the job has no trigger, so it would never run." if not triggers else (
            f"the job has more than one trigger ({', '.join(triggers)}), so it is unclear when it should run."
        )
        raise _fail(
            source,
            where,
            problem,
            'Give it exactly one of: schedule = "<when>", after = "<other job>", or manual = true.',
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
    elif table["manual"] is not True:
        raise _fail(source, where, '"manual" can only be true.', "Write manual = true, or use schedule or after instead.")

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
    elif isinstance(network_value, list) and all(isinstance(h, str) for h in network_value):
        for host in network_value:
            if "://" in host or "/" in host or not _HOST_RE.match(host):
                raise _fail(
                    source,
                    where,
                    f'"network" contains {host!r}, which is not a plain host name.',
                    'List only host names, without https:// or paths, for example network = ["api.example.com"].',
                )
        network = tuple(dict.fromkeys(network_value))
    else:
        raise _fail(
            source,
            where,
            '"network" must be false (no internet) or a list of the host names the job needs.',
            'Write network = false, or list the hosts, for example network = ["api.example.com"].',
        )

    writes_value = table.get("writes", list(DEFAULT_WRITES))
    if not isinstance(writes_value, list) or any(w not in WRITE_TARGETS for w in writes_value):
        raise _fail(
            source,
            where,
            f'"writes" = {writes_value!r} is not allowed. It says which of the app\'s folders the job may change.',
            'Use a list with "data" and/or "state", for example writes = ["data", "state"], or [] for none.',
        )
    writes = tuple(dict.fromkeys(writes_value))

    return Job(
        name=name,
        run=run,
        argv=argv,
        trigger=trigger,
        schedule=schedule,
        after=after,
        memory=memory,
        timeout=timeout,
        network=network,
        writes=writes,
    )


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


# --------------------------------------------------------------------------- public


def parse(data: dict[str, Any], *, source: str = MANIFEST_FILE, path: Path | None = None) -> Manifest:
    if not isinstance(data, dict):
        raise _fail(source, "", "it is not a table of settings.", "Start the file with an [app] section.")
    _check_keys(data, TOP_KEYS, source, "")
    app = data.get("app")
    if not isinstance(app, dict):
        raise _fail(
            source,
            "",
            "it has no [app] section, so mes does not know which app this is.",
            'Add:\n[app]\nname = "myapp"\ntitle = "My app"\nversion = "0.1.0"',
        )
    _check_keys(app, APP_KEYS, source, "[app]")
    if "name" not in app:
        raise _fail(source, "[app]", "the app has no name.", 'Add name = "myapp" under [app].')
    name = validate_app_name(app["name"], source=source)
    title = _string(app["title"], source, "[app]", "title", "My app") if "title" in app else name
    version = _string(app["version"], source, "[app]", "version", "0.1.0") if "version" in app else DEFAULT_VERSION

    jobs_table = data.get("jobs", {})
    if not isinstance(jobs_table, dict):
        raise _fail(source, "[jobs]", "jobs must be written as [jobs.<name>] sections.", 'Write for example [jobs.sync] followed by run = "sync".')
    jobs = {jname: _parse_job(jname, jtable, source) for jname, jtable in jobs_table.items()}
    _check_after(jobs, source)
    return Manifest(name=name, title=title, version=version, jobs=jobs, path=path)


def loads(text: str, *, source: str = MANIFEST_FILE, path: Path | None = None) -> Manifest:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(
            f"{source} has a typing mistake{_toml_where(e)}, so the app's settings cannot be read. "
            "The app cannot be used until that line is fixed.",
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


def load(path: str | Path) -> Manifest:
    p = Path(path)
    if p.is_dir():
        p = p / MANIFEST_FILE
    if not p.is_file():
        raise NotFound(
            f"There is no {MANIFEST_FILE} at {p}. This file describes the app, so mes cannot work with it yet.",
            hint="Run the command from inside the app's folder, or create the app with: mes new <name>",
        )
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
