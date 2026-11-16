"""The per-machine list of projects that ``mes gate`` may run on a personal machine.

On a server, the gate finds its project in the server layout (``mes gate --server-root``) and
needs no list. On a personal machine the list lives next to the per-machine config file, in
``projects.toml`` (default ``~/.config/mes/projects.toml``; it follows ``MES_CONFIG`` /
``XDG_CONFIG_HOME``). It maps a project name to the full path of the project's folder::

    [projects]
    myproject = "/home/me/code/myproject"

Only the owner changes it, with ``mes projects add|remove``. ``mes gate --project <name>`` reads it
on every request and never takes a path from the caller. The apps the gate may run are the
project's ``[apps]``; each one's program is ``<folder>/.venv/bin/<cli>``.

Public API
----------
``RESERVED_NAMES``                 names that can never be an app (``mes``).
``registry_path() -> Path``        where the list is kept (may not exist yet).
``valid_name(name) -> bool``
``load(path=None, *, public=False) -> dict[str, str]``  name -> project folder. ``{}`` when the
    file does not exist; ``ConfigError`` when it is broken; ``Refused`` when other users could
    change it.
``lookup(name, *, public=False) -> Path``  the folder of one project (``NotFound`` when absent).
``program_path(folder, cli) -> Path``  ``<folder>/.venv/bin/<cli>``.
``check_program(path, *, app=None) -> Path``  the path must be absolute, a regular
    file, executable, and pass ``trust_problem`` (as must the interpreter on its ``#!``
    line, with group-writable folders tolerated). ``app`` given: messages show no paths.
``trust_problem(path, *, strict=True, public=False) -> str | None``  why another account could
    change a file (owner, mode bits, and every folder above it), like OpenSSH StrictModes.
``add(folder, *, timeout=20.0) -> dict``  check every app of the project really is that app (it
    is asked ``--json describe``) and save the project.
``remove(name) -> dict``           take a project off the list (``NotFound`` when absent).
``list_projects() -> list[dict]``  every registered project with whether its apps are usable.
``register(subparsers)``           adds ``mes projects add|remove|list``.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from mes import manifest as manifest_mod
from mes import paths
from mes.app import check_allowed
from mes.errors import ConfigError, MesError, NotFound, Refused, UsageError
from mes.manifest import APP_NAME_RE

REGISTRY_FILE = "projects.toml"
RESERVED_NAMES = manifest_mod.RESERVED_APP_NAMES

_HEADER = (
    "# Projects whose apps this computer may run for AI agents through 'mes gate'.\n"
    "# Each line maps a project name to the full path of the project's folder.\n"
    "# Change it with 'mes projects add <folder>' and 'mes projects remove <name>'.\n"
)


def registry_path() -> Path:
    return paths.config_path().parent / REGISTRY_FILE


def valid_name(name: Any) -> bool:
    return isinstance(name, str) and APP_NAME_RE.fullmatch(name) is not None


@functools.lru_cache(maxsize=None)
def _private_group(gid: int) -> bool:
    """True when ``gid`` is this account's own group and no other account is in it."""
    if gid != os.getgid():
        return False
    try:
        import grp
        import pwd

        me = pwd.getpwuid(os.getuid()).pw_name
        if any(m != me for m in grp.getgrgid(gid).gr_mem):
            return False
        return not any(u.pw_gid == gid and u.pw_uid != os.getuid() for u in pwd.getpwall())
    except (ImportError, KeyError, OSError):
        return False


def _others_can_write(st: os.stat_result, *, group_ok: bool = False) -> bool:
    if st.st_mode & stat.S_IWOTH:
        return True
    if st.st_mode & stat.S_IWGRP and not group_ok and not _private_group(st.st_gid):
        return True
    return False


def trust_problem(path: str | Path, *, strict: bool = True, public: bool = False) -> str | None:
    """Why another account could change ``path`` (a file), or ``None`` when only this account
    (or the system administrator) can.

    Like OpenSSH's StrictModes: the file and every folder above it must belong to this account
    or to root, and must not be writable by other accounts (a folder with the sticky bit, such
    as /tmp, is fine: others cannot rename or replace files in it). With ``strict=False``,
    group-writable folders are accepted (used for the Python interpreter, which on a Mac often
    lives in Homebrew's group-writable folders).
    """
    real = Path(os.path.realpath(path))
    me = os.getuid()
    try:
        st = real.stat()
    except OSError:
        return "it does not exist"
    if st.st_uid not in (me, 0):
        return "it belongs to another account on this computer"
    if _others_can_write(st, group_ok=not strict):
        return "other accounts on this computer can change it"
    for folder in real.parents:
        try:
            fst = folder.stat()
        except OSError:
            return "one of its folders cannot be checked"
        if fst.st_uid not in (me, 0):
            return ("one of its folders" if public else f"the folder {folder}") + " belongs to another account on this computer"
        if _others_can_write(fst, group_ok=not strict) and not fst.st_mode & stat.S_ISVTX:
            return "other accounts on this computer can change " + ("one of its folders" if public else f"the folder {folder}")
    return None


def _interpreter(program: Path) -> Path | None:
    """The program named on a script's first line (#!...), when it is a full path."""
    try:
        with open(program, "rb") as f:
            first = f.readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    words = first[2:].strip().split()
    if not words or not words[0].startswith(b"/"):
        return None
    return Path(os.fsdecode(words[0]))


def load(path: str | Path | None = None, *, public: bool = False) -> dict[str, str]:
    """``public=True`` (used by the gate) leaves file paths out of the messages."""
    p = Path(path) if path else registry_path()
    where = "" if public else f" ({p})"
    try:
        st = p.stat()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigError(
            f"The list of projects{where} could not be read, so nothing can be run through the gate.",
            hint="Check that the file belongs to this account and can be read.",
            detail=str(e),
        ) from None
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError(
            f"The list of projects{where} is not a normal file, so mes will not trust it.",
            hint="Move it away and add the projects again with: mes projects add <folder>",
        )
    problem = trust_problem(p, public=public)
    if problem:
        raise Refused(
            f"The list of projects{where} could be changed by someone else: {problem}. Someone "
            "could then make the gate run a program of their choice, so mes refuses to use it.",
            hint="Ask the owner to check it with: mes projects list" if public else
            f"Make the file and its folders private to this account (for example: chmod 600 {p} and "
            f"chmod go-w on its folders), then try again.",
            code="untrusted_file",
        )
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(
            f"The list of projects{where} is not valid TOML ({e}), so nothing can be run through the gate.",
            hint="Fix the line mentioned, or move the file away and add the projects again with: "
            "mes projects add <folder>",
        ) from None
    extra = sorted(set(data) - {"projects"})
    if extra:
        raise ConfigError(
            f"The list of projects{where} has an unexpected section {extra[0]!r}, so mes will not "
            "trust it. Only a [projects] table is allowed.",
            hint="Remove that section, or move the file away and add the projects again.",
        )
    projects = data.get("projects", {})
    if not isinstance(projects, dict):
        raise ConfigError(
            f"In the list of projects{where}, 'projects' must be a table of name = \"folder\".",
            hint="Move the file away and add the projects again with: mes projects add <folder>",
        )
    out: dict[str, str] = {}
    for name, folder in projects.items():
        if not valid_name(name) or not isinstance(folder, str) or not os.path.isabs(folder):
            raise ConfigError(
                f"In the list of projects{where}, the entry {str(name)[:40]!r} is not a valid project name with "
                "the full path of its folder, so mes will not trust the list.",
                hint=f"Remove that entry with: mes projects remove {name}" if valid_name(name)
                else "Remove that line from the file.",
            )
        out[name] = folder
    return out


def _save(projects: dict[str, str], path: Path | None = None) -> Path:
    p = path or registry_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lines = [_HEADER, "[projects]\n"]
    for name in sorted(projects):
        # A JSON string without control characters is also a valid TOML basic string.
        lines.append(f"{name} = {json.dumps(projects[name], ensure_ascii=False)}\n")
    fd, tmp = tempfile.mkstemp(prefix=".projects-", suffix=".toml", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(lines))
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return p


def program_path(folder: str | Path, cli: str) -> Path:
    return Path(folder) / ".venv" / "bin" / cli


def check_program(program: str | Path, *, app: str | None = None) -> Path:
    """``app`` given (by the gate): messages name the app instead of showing file paths."""
    p = Path(program)
    shown = f'The program of the app "{app}"' if app else f"The app's program ({p})"
    if not p.is_absolute():
        raise MesError(
            f"{shown} is not given as a full path, so mes cannot be sure which program would run.",
            hint="Give the full path, starting with /.",
            code="app_unavailable",
        )
    text = str(p)
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise UsageError(
            "The program path contains invisible control characters, so mes will not use it.",
            hint="Give the plain full path of the app's program.",
        )
    owner_hint = "Ask the owner of this computer to check the app (mes projects list)."
    try:
        st = p.stat()
    except OSError:
        raise MesError(
            f"{shown} does not exist on this computer, so it cannot be run.",
            hint=owner_hint if app else "Reinstall the app (uv sync in the project folder), then register it again with: mes projects add <folder>",
            code="app_unavailable",
        ) from None
    if not stat.S_ISREG(st.st_mode) or not os.access(p, os.X_OK):
        raise MesError(
            f"{shown} is not a program that can be run, so the app cannot be started.",
            hint=owner_hint if app else "Run uv sync in the project folder so the app's program is installed in .venv/bin.",
            code="app_unavailable",
        )
    problem = trust_problem(p, public=app is not None)
    interpreter = _interpreter(p)
    if problem is None and interpreter is not None:
        inner = trust_problem(interpreter, strict=False, public=app is not None)
        if inner:
            problem = f"the Python it starts{'' if app else f' ({interpreter})'} is not safe: {inner}"
    if problem:
        raise Refused(
            f"{shown} could be changed by someone else: {problem}. Someone could then make the gate "
            "run their own code, so mes refuses to run it.",
            hint=owner_hint if app else "Make the program and its folders private to this account "
            f"(for example: chmod go-w {p} and its folders), then register it again.",
            code="untrusted_file",
        )
    return p


def lookup(name: str, *, public: bool = False) -> Path:
    projects = load(public=public)
    if name not in projects:
        raise NotFound(
            f'There is no project called "{name}" on this computer, so nothing was run.',
            hint="Ask the owner which projects are set up (they can list them with: mes projects list).",
        )
    return Path(projects[name])


def check_project_file(folder: Path, *, public: bool) -> Path:
    """The project's ``project.toml`` must exist and be changeable only by this account."""
    p = folder / manifest_mod.MANIFEST_FILE
    if not p.is_file():
        raise MesError("The project's settings file is missing, so the gate cannot tell which apps it has.",
                       hint="Ask the owner to check the project (mes projects list).", code="app_unavailable")
    problem = trust_problem(p, public=public)
    if problem:
        raise Refused(
            f"The project's settings file could be changed by someone else: {problem}. Someone could then change "
            "which programs the gate runs, so mes refuses.",
            hint="Ask the owner to make the project folder private to their account.",
            code="untrusted_file",
        )
    return p


def add(folder: str | Path, *, timeout: float = 20.0) -> dict[str, Any]:
    from mes.gate import ask_commands  # the gate's own safe way of asking an app what it is

    root = Path(os.path.abspath(Path(folder).expanduser()))
    project = manifest_mod.load(root)
    check_project_file(root, public=False)
    found: dict[str, dict[str, list[str]]] = {}
    for spec in project.apps.values():
        program = program_path(root, spec.cli)
        check_program(program)
        try:
            commands = ask_commands(program, spec.name, timeout=timeout)
        except Refused as e:
            if e.code != "wrong_app":
                raise
            raise Refused(
                f'The program {spec.cli} does not say it is the app "{spec.name}", so the project was not added. '
                "Nothing changed.",
                hint=f'Check App("...") in the app\'s code: it must be App("{spec.name}").',
                code="wrong_app",
            ) from None
        except MesError as e:
            if e.code != "app_unavailable":
                raise
            raise MesError(
                f'{program} is not an app made with mes (it did not answer "describe" the way mes apps do), so '
                "the project was not added. Nothing changed.",
                hint="Run 'uv sync' in the project's folder first, so its programs are installed.",
                code="not_an_app",
            ) from None
        found[spec.name] = {k: sorted(c for c, v in commands.items() if v == k) for k in ("read", "write", "admin")}
    projects = load()
    status = "updated" if project.name in projects else "added"
    projects[project.name] = str(root)
    where = _save(projects)
    return {
        "project": project.name,
        "folder": str(root),
        "status": status,
        "registry": str(where),
        "apps": found,
        "message": f'The apps of "{project.name}" can now be reached through the gate (mes gate --project '
        f"{project.name}). Agents with a read key can run their read commands, agents with a write key also "
        "their write commands, and admin commands stay with you. How agents connect: mes guide gate",
    }


def remove(name: str) -> dict[str, Any]:
    projects = load()
    if name not in projects:
        raise NotFound(
            f'There is no project called "{name}" in the list, so nothing was removed.',
            hint="See the list with: mes projects list",
        )
    del projects[name]
    _save(projects)
    return {
        "project": name,
        "status": "removed",
        "message": f'"{name}" can no longer be reached through the gate. Its data was not touched.',
    }


def list_projects() -> list[dict[str, Any]]:
    out = []
    for name, folder in sorted(load().items()):
        usable, problem = True, None
        try:
            project = manifest_mod.load(folder)
            check_project_file(Path(folder), public=False)
            for spec in project.apps.values():
                check_program(program_path(folder, spec.cli))
        except MesError as e:
            usable, problem = False, e.message
        out.append({"project": name, "folder": folder, "usable": usable, "problem": problem})
    return out


# --------------------------------------------------------------------------- mes projects


def _handle_add(args: argparse.Namespace) -> Any:
    check_allowed("admin")  # it changes which programs the gate may run
    return add(args.folder)


def _handle_remove(args: argparse.Namespace) -> Any:
    check_allowed("admin")
    return remove(args.name)


def _handle_list(args: argparse.Namespace) -> Any:
    check_allowed("read")
    items = list_projects()
    if args.json:
        return {"registry": str(registry_path()), "projects": items}
    if not items:
        return ("No projects are registered yet, so the gate cannot run anything on this computer. "
                "Add one with: mes projects add <folder>")
    return items


def register(subparsers: Any) -> None:
    if "projects" in getattr(subparsers, "choices", {}):
        return  # already added (mes.gate adds it too)
    p = subparsers.add_parser(
        "projects",
        help="The projects whose apps the gate may run on this computer (add, remove, list).",
        description="Manage the list of projects that AI agents can reach through 'mes gate' on a personal "
        "machine. (On a server, the gate finds the project in /var/lib/mes and needs no list.)",
    )
    sub = p.add_subparsers(dest="projects_command", metavar="<add|remove|list>")
    a = sub.add_parser("add", help="Let the gate run a project's apps.",
                       description="Register a project. mes first asks each app's program to describe "
                       "itself, to make sure it really is that app.")
    a.add_argument("folder", help="the project's folder (the one holding project.toml)")
    a.set_defaults(func=_handle_add)
    r = sub.add_parser("remove", help="Stop the gate from running a project's apps (its data is kept).")
    r.add_argument("name", help="the project's name")
    r.set_defaults(func=_handle_remove)
    ls = sub.add_parser("list", help="Show the registered projects.")
    ls.set_defaults(func=_handle_list)
