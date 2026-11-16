"""The per-machine list of installed apps that ``mes gate`` may run.

The list lives next to the per-machine config file, in ``apps.toml``
(default ``~/.config/mes/apps.toml``; it follows ``MES_CONFIG`` /
``XDG_CONFIG_HOME``). It maps an app name to the absolute path of that app's
command-line program::

    [apps]
    myapp = "/opt/apps/myapp/.venv/bin/myapp"

Only the owner changes it, with ``mes apps add|remove``. ``mes gate`` reads it
on every request and never takes an app path from the caller.

Public API
----------
``RESERVED_NAMES``                 names that can never be registered (``mes``).
``registry_path() -> Path``        where the list is kept (may not exist yet).
``load(path=None, *, public=False) -> dict[str, str]``  name -> program path. ``{}`` when the file
    does not exist; ``ConfigError`` when it is broken; ``Refused`` when other
    users could change it.
``lookup(name) -> Path``           the program for one app (``NotFound`` when absent).
``check_program(path, *, app=None) -> Path``  the path must be absolute, a regular
    file, executable, and pass ``trust_problem`` (as must the interpreter on its ``#!``
    line, with group-writable folders tolerated). ``app`` given: messages show no paths.
``trust_problem(path, *, strict=True) -> str | None``  why another account could change
    a file (owner, mode bits, and every folder above it), like OpenSSH StrictModes.
``add(name, program, *, timeout=20.0) -> dict``  check the program really is that
    app (it is asked ``--json describe``) and save it.
``remove(name) -> dict``           take an app off the list (``NotFound`` when absent).
``list_apps() -> list[dict]``      every registered app with whether its program is usable.
``register(subparsers)``           adds ``mes apps add|remove|list``.
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

from mes import paths
from mes.app import check_allowed
from mes.errors import ConfigError, MesError, NotFound, Refused, UsageError
from mes.manifest import APP_NAME_RE

REGISTRY_FILE = "apps.toml"
RESERVED_NAMES = ("mes",)

_HEADER = (
    "# Apps that this computer may run for AI agents through 'mes gate'.\n"
    "# Each line maps an app name to the full path of the app's command-line program.\n"
    "# Change it with 'mes apps add <name> <program>' and 'mes apps remove <name>'.\n"
)


def registry_path() -> Path:
    return paths.config_path().parent / REGISTRY_FILE


def valid_name(name: Any) -> bool:
    return isinstance(name, str) and APP_NAME_RE.fullmatch(name) is not None


def _check_name(name: str) -> None:
    if not valid_name(name):
        raise UsageError(
            f"{name!r} is not a valid app name, so it cannot be added to the list of apps.",
            hint="App names use 2 to 31 lowercase letters, digits and dashes, starting with a letter.",
        )
    if name in RESERVED_NAMES:
        raise UsageError(
            f'"{name}" is the name of mes itself, so it cannot be used for an app. Allowing it would let '
            "agents reach mes's own commands through the gate.",
            hint="Use the app's own name, as written in its app.toml.",
        )


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
            f"The list of installed apps{where} could not be read, so no app can be run through the gate.",
            hint="Check that the file belongs to this account and can be read.",
            detail=str(e),
        ) from None
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError(
            f"The list of installed apps{where} is not a normal file, so mes will not trust it.",
            hint="Move it away and add the apps again with: mes apps add <name> <program>",
        )
    problem = trust_problem(p, public=public)
    if problem:
        raise Refused(
            f"The list of installed apps{where} could be changed by someone else: {problem}. Someone "
            "could then make the gate run a program of their choice, so mes refuses to use it.",
            hint="Ask the owner to check it with: mes apps list" if public else
            f"Make the file and its folders private to this account (for example: chmod 600 {p} and "
            f"chmod go-w on its folders), then try again.",
            code="untrusted_file",
        )
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(
            f"The list of installed apps{where} is not valid TOML ({e}), so no app can be run through the gate.",
            hint="Fix the line mentioned, or move the file away and add the apps again with: "
            "mes apps add <name> <program>",
        ) from None
    extra = sorted(set(data) - {"apps"})
    if extra:
        raise ConfigError(
            f"The list of installed apps{where} has an unexpected section {extra[0]!r}, so mes will not "
            "trust it. Only an [apps] table is allowed.",
            hint="Remove that section, or move the file away and add the apps again.",
        )
    apps = data.get("apps", {})
    if not isinstance(apps, dict):
        raise ConfigError(
            f"In the list of installed apps{where}, 'apps' must be a table of name = \"program path\".",
            hint="Move the file away and add the apps again with: mes apps add <name> <program>",
        )
    out: dict[str, str] = {}
    for name, program in apps.items():
        if not valid_name(name) or name in RESERVED_NAMES or not isinstance(program, str) \
                or not os.path.isabs(program):
            raise ConfigError(
                f"In the list of installed apps{where}, the entry {name!r} is not a valid app name with the "
                "full path of its program, so mes will not trust the list.",
                hint=f"Remove that entry with: mes apps remove {name}" if valid_name(name)
                else "Remove that line from the file.",
            )
        out[name] = program
    return out


def _save(apps: dict[str, str], path: Path | None = None) -> Path:
    p = path or registry_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lines = [_HEADER, "[apps]\n"]
    for name in sorted(apps):
        # A JSON string without control characters is also a valid TOML basic string.
        lines.append(f"{name} = {json.dumps(apps[name], ensure_ascii=False)}\n")
    fd, tmp = tempfile.mkstemp(prefix=".apps-", suffix=".toml", dir=p.parent)
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
    owner_hint = "Ask the owner of this computer to check the app (mes apps list)."
    try:
        st = p.stat()
    except OSError:
        raise MesError(
            f"{shown} does not exist on this computer, so it cannot be run.",
            hint=owner_hint if app else "Reinstall the app, then register it again with: mes apps add <name> <program>",
            code="app_unavailable",
        ) from None
    if not stat.S_ISREG(st.st_mode) or not os.access(p, os.X_OK):
        raise MesError(
            f"{shown} is not a program that can be run, so the app cannot be started.",
            hint=owner_hint if app else "Point to the app's command-line program (usually in the app's .venv/bin folder).",
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
    apps = load(public=public)
    if name not in apps:
        raise NotFound(
            f'There is no app called "{name}" on this computer, so nothing was run.',
            hint="Ask the owner which apps are installed (they can list them with: mes apps list).",
        )
    return Path(apps[name])


def add(name: str, program: str | Path, *, timeout: float = 20.0) -> dict[str, Any]:
    from mes.gate import ask_commands  # the gate's own safe way of asking an app what it is

    _check_name(name)
    p = Path(os.path.abspath(Path(program).expanduser()))
    check_program(p)
    try:
        commands = ask_commands(p, name, timeout=timeout)
    except Refused as e:
        if e.code != "wrong_app":
            raise
        actual = getattr(e, "actual_app", None)
        if isinstance(actual, str) and valid_name(actual):
            raise Refused(
                f'This program is the app "{actual}", not "{name}". Nothing was added.',
                hint=f"Register it under its own name: mes apps add {actual} {p}",
                code="wrong_app",
            ) from None
        raise Refused(f'This program does not say it is the app "{name}", so it was not added. Nothing changed.',
                      hint="Check that you picked the program of the right app.", code="wrong_app") from None
    except MesError as e:
        if e.code != "app_unavailable":
            raise
        raise MesError(
            f'{p} is not an app made with mes (it did not answer "describe" the way mes apps do), so it '
            "was not added. Nothing changed.",
            hint="Point to the app's own program, usually in the app's .venv/bin folder (run 'uv sync' "
            "in the app's folder first).",
            code="not_an_app",
        ) from None
    apps = load()
    status = "updated" if name in apps else "added"
    apps[name] = str(p)
    where = _save(apps)
    kinds = {k: sorted(c for c, v in commands.items() if v == k) for k in ("read", "write", "admin")}
    return {
        "app": name,
        "program": str(p),
        "status": status,
        "registry": str(where),
        "commands": kinds,
        "message": f'"{name}" can now be reached through the gate. Agents with a read key can run its '
        "read commands, agents with a write key also its write commands, and admin commands stay "
        "with you. How agents connect and how keys are set up: mes guide gate",
    }


def remove(name: str) -> dict[str, Any]:
    apps = load()
    if name not in apps:
        raise NotFound(
            f'There is no app called "{name}" in the list of installed apps, so nothing was removed.',
            hint="See the list with: mes apps list",
        )
    del apps[name]
    _save(apps)
    return {
        "app": name,
        "status": "removed",
        "message": f'"{name}" can no longer be reached through the gate. Its data was not touched.',
    }


def list_apps() -> list[dict[str, Any]]:
    out = []
    for name, program in sorted(load().items()):
        try:
            check_program(program)
            usable, problem = True, None
        except MesError as e:
            usable, problem = False, e.message
        out.append({"app": name, "program": program, "usable": usable, "problem": problem})
    return out


# --------------------------------------------------------------------------- mes apps


def _handle_add(args: argparse.Namespace) -> Any:
    check_allowed("admin")  # it changes which programs the gate may run
    return add(args.name, args.program)


def _handle_remove(args: argparse.Namespace) -> Any:
    check_allowed("admin")
    return remove(args.name)


def _handle_list(args: argparse.Namespace) -> Any:
    check_allowed("read")
    items = list_apps()
    if args.json:
        return {"registry": str(registry_path()), "apps": items}
    if not items:
        return ("No apps are registered yet, so the gate cannot run anything. "
                "Add one with: mes apps add <name> <program>")
    return items


def register(subparsers: Any) -> None:
    if "apps" in getattr(subparsers, "choices", {}):
        return  # already added (mes.gate adds it too)
    p = subparsers.add_parser(
        "apps",
        help="The apps the gate may run on this computer (add, remove, list).",
        description="Manage the list of installed apps that AI agents can reach through 'mes gate'.",
    )
    sub = p.add_subparsers(dest="apps_command", metavar="<add|remove|list>")
    a = sub.add_parser("add", help="Let the gate run an installed app.",
                       description="Register an installed app. mes first asks the program to describe "
                       "itself, to make sure it really is that app.")
    a.add_argument("name", help="the app's name, as in its app.toml")
    a.add_argument("program", help="full path of the app's command-line program")
    a.set_defaults(func=_handle_add)
    r = sub.add_parser("remove", help="Stop the gate from running an app (its data is kept).")
    r.add_argument("name", help="the app's name")
    r.set_defaults(func=_handle_remove)
    ls = sub.add_parser("list", help="Show the registered apps.")
    ls.set_defaults(func=_handle_list)
