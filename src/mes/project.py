"""The project command: one console script named after the project that reaches every app.

A project made with mes has, in its ``pyproject.toml``::

    [project.scripts]
    fitlab = "mes.project:main"

so ``.venv/bin/fitlab`` exists next to every app's program. It never imports an app: it reads
``project.toml``, the lock (or the in-project apps) and each ``app.toml``, and runs the app's own
program with ``os.execv``, so the output, exit code and terminal are the app's own.

Dispatch of ``<project> [args]`` (contract section 5); ``w`` is the first argument that is not
``--json``:

1. no ``w``: with ``--json`` the same as ``describe``; otherwise a short plain help, exit 0;
2. ``-h``, ``--help`` or ``help``: that help;
3. ``describe``, ``guide``, ``apps``: the built-ins (read commands; ``MES_ALLOW`` is checked first);
4. an app's short name or full name: that app's program with the arguments minus that word;
5. any other word, with exactly one app: that app's program with the arguments unchanged, and
   (unless ``--json`` is given) a one-line note on stderr;
6. otherwise: a usage error (exit 2) listing the short names.

The project folder is ``MES_PROJECT_DIR`` (it must hold ``project.toml``), else the parent of
``sys.prefix``. App programs are taken only from ``Path(sys.executable).parent``, never ``PATH``.

``MES_ALLOW``: the environment is passed on unchanged and every app checks its own command kinds.
In addition, when ``MES_ALLOW`` is set to a level below ``admin``, the project command first asks
the app (``<program> --json describe``) for the command's kind and refuses (exit 4) before the
command runs when the level does not allow it; an unknown or empty ``MES_ALLOW`` is refused
before anything runs.

Public API
----------
``BUILTINS``  ``("describe", "guide", "apps")``.
``find_root() -> Path``
``run(argv=None, *, bin_dir=None, execv=os.execv) -> int``
``main(argv=None)``  the console entry point.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

from mes import manifest as manifest_mod
from mes import protocol
from mes.app import KINDS, allowed_level, check_allowed
from mes.errors import ExitCode, ManifestError, MesError, NotAllowed, NotFound, UsageError
from mes.manifest import AppSpec, Project

BUILTINS = ("describe", "guide", "apps")
HELP_WORDS = ("-h", "--help", "help")
DESCRIBE_TIMEOUT = 60.0
MAX_GUIDE_BYTES = 256 * 1024
EXIT_CODES = {"0": "ok", "1": "error", "2": "usage", "3": "not found", "4": "not allowed",
              "5": "refused for safety", "6": "needs a person", "75": "temporary failure, retry"}


def find_root() -> Path:
    env = os.environ.get("MES_PROJECT_DIR")
    if env:
        p = Path(env).expanduser()
        if not (p / manifest_mod.MANIFEST_FILE).is_file():
            raise NotFound(
                f"MES_PROJECT_DIR is set to {env[:200]!r}, but that folder has no project.toml, so the project "
                "command does not know which project it belongs to.",
                hint="Point MES_PROJECT_DIR at the project's folder, or remove it.",
                code="no_project",
            )
        return p.resolve()
    parent = Path(sys.prefix).parent
    if (parent / manifest_mod.MANIFEST_FILE).is_file():
        return parent.resolve()
    raise NotFound(
        "The project command could not find its project: its environment is not inside a project folder "
        "(no project.toml next to .venv).",
        hint="Run it from the project's own environment (uv run <project> ...), or set MES_PROJECT_DIR.",
        code="no_project",
    )


def _program(bin_dir: Path, spec: AppSpec) -> Path:
    exe = bin_dir / spec.program
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        raise NotFound(f"The program of the app {spec.name} is not installed in the project's environment.",
                       hint="Run: uv sync", code="app_not_installed")
    return exe


def _ask(exe: Path, *words: str) -> tuple[int, Any, bytes]:
    try:
        res = subprocess.run([str(exe), "--json", *words], stdin=subprocess.DEVNULL, capture_output=True,
                             timeout=DESCRIBE_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, None, str(e).encode()
    try:
        data = json.loads(res.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        data = None
    return res.returncode, data, res.stderr


def _describe_app(bin_dir: Path, spec: AppSpec) -> dict[str, Any]:
    code, data, err = _ask(_program(bin_dir, spec), "describe")
    if code != 0 or not isinstance(data, dict) or data.get("app") != spec.name:
        said = data.get("error", {}) if isinstance(data, dict) and isinstance(data.get("error"), dict) else {}
        raise MesError(
            f"The app {spec.name} did not describe itself correctly, so the project cannot be described.",
            hint=str(said.get("hint") or f"Run it by hand to see more: {spec.program} --json describe"),
            code="app_broken",
            detail=str(said.get("message") or err.decode("utf-8", "replace")[-1000:]) or None,
            detail_label="What the app said:",
        )
    return data


def _help(project: Project) -> str:
    lines = [f"{project.title} ({project.name}): the command of this project. Add --json for JSON data.", "",
             "Built-in commands:",
             f"  {project.name} describe   every app and its commands, with their kinds",
             f"  {project.name} guide      how an AI agent should use this project",
             f"  {project.name} apps       the apps of this project", "", "Apps:"]
    width = max((len(a.short) for a in project.apps.values()), default=0)
    for spec in project.apps.values():
        lines.append(f"  {project.name} {spec.short.ljust(width)} ...   {spec.title} ({spec.name})")
    lines += ["", f"For an app's commands: {project.name} <app> --help"]
    return "\n".join(lines)


def _describe(project: Project, bin_dir: Path, json_mode: bool) -> Any:
    apps = []
    for spec in project.apps.values():
        apps.append({"name": spec.name, "short": spec.short, "package": spec.package, "title": spec.title,
                     "program": spec.program, "describe": _describe_app(bin_dir, spec)})
    data = {"project": project.name, "title": project.title, "version": project.version, "command": project.name,
            "allowed": allowed_level(),
            "json": "Add --json (before or after the command) to get only JSON data on stdout.",
            "exit_codes": EXIT_CODES, "apps": apps,
            "jobs": [j.to_dict() for j in project.jobs.values()]}
    if json_mode:
        return data
    lines = [f"{project.title} ({project.name}), version {project.version}", ""]
    for a in apps:
        lines.append(f"{a['title']} ({project.name} {a['short']} ...)")
        rows = [{"command": c.get("name"), "kind": c.get("kind"),
                 "what it does": (c.get("help") or "").splitlines()[0] if c.get("help") else ""}
                for c in a["describe"].get("commands", []) if isinstance(c, dict)]
        lines += [protocol.format_text(rows), ""]
    if data["jobs"]:
        lines.append("Jobs (see: mes jobs list):")
        lines += [f"- {j['id']}: runs \"{j['run']}\"" for j in data["jobs"]]
    return "\n".join(lines).rstrip()


def _read_text(p: Path) -> str | None:
    try:
        st = os.lstat(p)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size > MAX_GUIDE_BYTES:
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def _guide(project: Project, root: Path, bin_dir: Path, json_mode: bool) -> Any:
    texts: list[str] = []
    parts: list[dict[str, str]] = []
    own = _read_text(root / "AGENTS.md")
    if own is not None:
        texts.append(own.rstrip())
        parts.append({"source": "project", "file": "AGENTS.md"})
    for spec in project.apps.values():
        body = _read_text(spec.folder / "AGENTS.md") if spec.folder else None
        source = "AGENTS.md"
        if body is None:
            code, data, _ = _ask(_program(bin_dir, spec), "guide")
            body = data.get("guide") if code == 0 and isinstance(data, dict) and isinstance(data.get("guide"), str) \
                else f"(The app {spec.name} has no guide.)"
            source = "generated"
        texts.append(f"## {spec.title} ({project.name} {spec.short} ...)\n\n{body.strip()}")
        parts.append({"source": spec.name, "file": source})
    text = "\n\n".join(texts) + "\n"
    if json_mode:
        return {"project": project.name, "guide": text, "parts": parts}
    return text


def _apps(project: Project, json_mode: bool) -> Any:
    rows = [{"name": a.name, "short": a.short, "package": a.package, "title": a.title, "program": a.program,
             "location": a.location} for a in project.apps.values()]
    if json_mode:
        return {"project": project.name, "apps": rows}
    return protocol.format_text([{"app": r["name"], "short": r["short"], "title": r["title"],
                                  "where": r["location"]} for r in rows])


def _command_word(args: Sequence[str]) -> str | None:
    return next((a for a in args if not a.startswith("-")), None)


def _precheck_kind(exe: Path, spec: AppSpec, args: Sequence[str]) -> None:
    """With MES_ALLOW below admin: refuse a command whose kind the level does not allow, before
    the command itself runs."""
    level = allowed_level()
    if level == "admin":
        return
    command = _command_word(args)
    if command is None or command in ("describe", "guide"):
        return
    code, data, err = _ask(exe, "describe")
    if code != 0 or not isinstance(data, dict) or not isinstance(data.get("commands"), list):
        raise MesError(f"The app {spec.name} did not say which commands it has, so nothing was run.",
                       hint=f"Run it by hand to see more: {spec.program} --json describe", code="app_broken",
                       detail=err.decode("utf-8", "replace")[-1000:] or None)
    kinds = {c.get("name"): c.get("kind") for c in data["commands"] if isinstance(c, dict)}
    kind = kinds.get(command)
    if kind in KINDS:
        check_allowed(kind, level)


def run(argv: Sequence[str] | None = None, *, bin_dir: Path | None = None,
        execv: Callable[[str, list[str]], Any] = os.execv) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in args
    try:
        root = find_root()
        project = manifest_mod.load(root)
        session = os.environ.get("MES_PROJECT")
        if session is not None and session != project.name:
            raise ManifestError(
                f'This session belongs to the project "{session[:40]}", not "{project.name}". Projects cannot read '
                "each other, so nothing was run.", hint="Run it in the project's own session or account.",
                code="wrong_project")
        bins = bin_dir or Path(sys.executable).parent
        words = [a for a in args if a != "--json"]
        w = words[0] if words else None
        if w is None or w in HELP_WORDS:
            if w is None and json_mode:
                check_allowed("read")
                return protocol.emit(_describe(project, bins, True), json_mode=True)
            return protocol.emit(_help(project), json_mode=False) if not json_mode else \
                protocol.emit({"help": _help(project)}, json_mode=True)
        if w in BUILTINS:
            check_allowed("read")
            if len(words) > 1:
                raise UsageError(f'"{project.name} {w}" takes nothing else (only --json).',
                                 hint=f"Run: {project.name} {w} --json")
            if w == "describe":
                result = _describe(project, bins, json_mode)
            elif w == "guide":
                result = _guide(project, root, bins, json_mode)
            else:
                result = _apps(project, json_mode)
            return protocol.emit(result, json_mode=json_mode)
        spec = project.app_for_word(w)
        if spec is not None:
            i = args.index(w)
            rest = args[:i] + args[i + 1:]
        elif len(project.apps) == 1:
            spec = next(iter(project.apps.values()))
            rest = args
            if not json_mode:
                sys.stderr.write(
                    f'Note: "{project.name} {w}" was passed to {spec.name}, the only app of this project. '
                    f'Write "{project.name} {spec.short} {w}" so it keeps working when the project has more apps.\n')
                sys.stderr.flush()
        else:
            shorts = [a.short for a in project.apps.values()]
            raise UsageError(
                f'{project.name} has several apps, so "{w}" does not say which one is meant.',
                hint=f"Start with the app's short name: {', '.join(shorts)}. For example: "
                f"{project.name} {shorts[0]} {w}.",
            )
        allowed_level()  # an empty or unknown MES_ALLOW is refused before anything runs
        exe = _program(bins, spec)
        _precheck_kind(exe, spec, rest)
        sys.stdout.flush()
        sys.stderr.flush()
        execv(str(exe), [str(exe), *rest])
        return 0  # only reached when execv is replaced (tests)
    except MesError as e:
        return protocol.emit_error(e, json_mode=json_mode)
    except Exception as e:  # noqa: BLE001 - last line of defence, in plain words
        return protocol.emit_error(protocol.internal_error(e, "Something went wrong inside the project command, so "
                                                           "it stopped before running anything."), json_mode=json_mode)


def main(argv: Sequence[str] | None = None) -> NoReturn:
    sys.exit(run(argv))


__all__ = ["BUILTINS", "find_root", "run", "main", "ExitCode", "NotAllowed"]
