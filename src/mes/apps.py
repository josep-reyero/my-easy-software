"""``mes apps list|sync|check``: the apps installed in a project, and ``apps.lock.json``.

A project lists its apps by package name (``[project] apps``). An app is either **in the
project** (``<project>/apps/<package>/app.toml``) or **installed** as a dependency (its package
folder in the project's environment, next to its ``__init__.py``). ``mes apps sync`` finds every
app, checks the whole project, asks every app's program to describe itself, and writes
``<project>/apps.lock.json``, which every reader (mes, the gate, easy-ui, a project command)
uses to find the apps. The lock holds this computer's paths, so it is never committed or
deployed: the server writes its own after ``uv sync``.

Installed apps are found by running the project's own interpreter,
``<project>/.venv/bin/python -I -c <FIND_SCRIPT> <package>...``: the script only calls
``importlib.util.find_spec`` (which never runs the package) and prints ``{package: folder}``.
mes never imports an app into its own process.

Public API
----------
``FIND_SCRIPT``  the fixed script given to the project's interpreter.
``Runner``       ``(argv, *, env, timeout, cwd) -> (returncode, stdout, stderr)``; tests pass fakes.
``find_installed(root, packages, *, runner=None) -> dict[str, Path]``
``resolve(root, project_file, *, runner=None) -> list[Located]``
``describe_app(root, spec, *, runner=None) -> dict``  the app's ``--json describe``, checked.
``sync(root, *, runner=None, strict=True, update_database=False) -> dict``
    writes the lock; returns a summary. With ``update_database`` (the ``mes apps sync`` command and
    ``mes dev``), an existing shared database is then brought in line with project.toml
    (``mes db sync``), so the views stop showing the rows of an app that was removed.
``write_lock(root, project_file, located) -> Path``
``check(root, *, runner=None) -> checklist``
``register(subparsers)``
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mes import manifest as manifest_mod
from mes import protocol
from mes.app import check_allowed
from mes.errors import ManifestError, MesError, NotFound, Refused
from mes.manifest import APP_FILE, APPS_DIR, LOCK_FILE, AppSpec, Located, Project, ProjectFile

Runner = Callable[..., tuple[int, bytes, bytes]]

FIND_SCRIPT = (
    "import importlib.util, json, sys\n"
    "out = {}\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        spec = importlib.util.find_spec(name)\n"
    "    except Exception:\n"
    "        spec = None\n"
    "    if spec is None:\n"
    "        out[name] = None\n"
    "    elif spec.submodule_search_locations is None:\n"
    "        out[name] = {'problem': 'module'}\n"
    "    elif spec.origin is None or len(list(spec.submodule_search_locations)) != 1:\n"
    "        out[name] = {'problem': 'namespace'}\n"
    "    else:\n"
    "        out[name] = list(spec.submodule_search_locations)[0]\n"
    "print(json.dumps(out))\n"
)
FIND_TIMEOUT = 60.0
DESCRIBE_TIMEOUT = 60.0
MAX_OUTPUT = 4 * 1024 * 1024
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"


def _default_runner(argv: list[str], *, env: dict[str, str], timeout: float, cwd: str) -> tuple[int, bytes, bytes]:
    try:
        res = subprocess.run(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                             timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, b"", b"timed out"
    except OSError as e:
        return 127, b"", str(e).encode()
    return res.returncode, res.stdout[:MAX_OUTPUT], res.stderr[-8192:]


def _env(root: Path, allow: str = "read") -> dict[str, str]:
    """A small environment for asking the project's own programs (no PYTHON* from outside)."""
    keep = ("HOME", "USER", "LOGNAME", "TZ", "MES_DATA_HOME", "MES_STATE_HOME", "MES_CONFIG", "MES_PROJECT",
            "MES_DATA_DIR", "MES_STATE_DIR", "MES_STORE_DIR", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME")
    env = {k: os.environ[k] for k in keep if os.environ.get(k)}
    env.update({"PATH": SAFE_PATH, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1", "MES_ALLOW": allow, "MES_PROJECT_DIR": str(root)})
    return env


def _bin(root: Path, name: str) -> Path:
    return root / ".venv" / "bin" / name


def _not_installed(what: str, root: Path) -> NotFound:
    return NotFound(
        f"{what} is not installed in the project's environment, so the apps cannot be listed safely.",
        hint=f"Run: uv sync (in {root}), then: mes apps sync",
        code="not_installed",
    )


# --------------------------------------------------------------------------- finding apps


def find_installed(root: Path, packages: list[str], *, runner: Runner | None = None) -> dict[str, Path]:
    if not packages:
        return {}
    python = _bin(root, "python")
    if not python.exists():
        raise _not_installed("Python", root)
    run = runner or _default_runner
    code, out, err = run([str(python), "-I", "-c", FIND_SCRIPT, *packages], env=_env(root), timeout=FIND_TIMEOUT,
                         cwd="/")
    try:
        found = json.loads(out.decode("utf-8")) if code == 0 else None
    except (UnicodeDecodeError, ValueError):
        found = None
    if not isinstance(found, dict):
        raise MesError("The project's own Python could not say where its installed apps are, so the list of apps was "
                       "not written.", hint=f"Run: uv sync (in {root}), then try again.", code="find_failed",
                       detail=err.decode("utf-8", "replace")[-1000:] or None)
    result: dict[str, Path] = {}
    for package in packages:
        value = found.get(package)
        if value is None:
            raise NotFound(
                f"The app {package} is listed in project.toml but is not installed in the project's environment, "
                "so its commands and jobs cannot be found.",
                hint=f"Add it to the dependencies in pyproject.toml, then run: uv sync (in {root}), and: mes apps sync",
                code="app_not_installed",
            )
        if not isinstance(value, str):
            kind = value.get("problem") if isinstance(value, dict) else None
            what = "a single module, not a package folder" if kind == "module" else "a package without one folder"
            raise ManifestError(
                f"The app {package} is {what}, so mes cannot find its app.toml.",
                hint="An app is a package: a folder with __init__.py and app.toml next to it. Fix the package, then "
                "run: uv sync and mes apps sync",
            )
        if not os.path.isabs(value) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise Refused(f"The project's Python gave a strange folder for {package}, so it was not used.",
                          hint="Run: uv sync, then try again.", code="bad_path")
        folder = Path(os.path.normpath(value))
        try:
            st, ast = os.lstat(folder), os.lstat(folder / APP_FILE)
        except OSError:
            raise ManifestError(
                f"The installed app {package} has no app.toml next to its __init__.py, so mes does not know its "
                "commands, jobs and tables.",
                hint="Add app.toml to the package (and to its build, so it is installed), then run: uv sync and "
                "mes apps sync",
            ) from None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or not stat.S_ISREG(ast.st_mode):
            raise Refused(f"The folder or the app.toml of {package} is a link, so mes will not use it.",
                          hint="Install the package as plain files, then run: mes apps sync", code="link")
        result[package] = folder
    return result


def resolve(root: Path, pf: ProjectFile, *, runner: Runner | None = None) -> list[Located]:
    in_project: dict[str, Path] = {}
    for package in pf.packages:
        folder = root / APPS_DIR / package
        try:
            ok = stat.S_ISREG(os.lstat(folder / APP_FILE).st_mode) and not os.path.islink(folder)
        except OSError:
            ok = False
        if ok:
            in_project[package] = folder
    installed = find_installed(root, [p for p in pf.packages if p not in in_project], runner=runner)
    return [Located(p, in_project[p], True) if p in in_project else Located(p, installed[p], False)
            for p in pf.packages]


def describe_app(root: Path, spec: AppSpec, *, runner: Runner | None = None) -> dict[str, Any]:
    exe = _bin(root, spec.program)
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        raise NotFound(
            f"The program of the app {spec.name} ({spec.program}) is not installed in the project's environment.",
            hint=f"Run: uv sync (in {root})" + (f'. For an app with cli = "...", pyproject.toml needs '
                                                f'{spec.name} = "{spec.package}.cli:main" under [project.scripts].'
                                                if spec.cli else ""),
            code="app_not_installed",
        )
    run = runner or _default_runner
    code, out, err = run([str(exe), "--json", "describe"], env=_env(root), timeout=DESCRIBE_TIMEOUT, cwd=str(root))
    try:
        info = json.loads(out.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        info = None
    if code != 0 or not isinstance(info, dict) or not isinstance(info.get("commands"), list):
        said = info.get("error", {}) if isinstance(info, dict) and isinstance(info.get("error"), dict) else {}
        raise MesError(
            f"The app {spec.name} did not say which commands it has, so the list of apps was not written.",
            hint=str(said.get("hint") or f"Run it by hand to see more: {spec.program} --json describe"),
            code="app_broken",
            detail=str(said.get("message") or err.decode("utf-8", "replace")[-1000:]) or None,
            detail_label="What the app said:",
        )
    if info.get("app") != spec.name:
        raise Refused(
            f"The program {spec.program} says it is the app {str(info.get('app'))[:40]!r}, but its app.toml says "
            f"{spec.name}. Running it could change the wrong data, so the list of apps was not written.",
            hint=f'Use App("{spec.name}") in the app\'s code (or answer "app": "{spec.name}" in its describe).',
            code="app_mismatch",
        )
    return info


def _kinds(info: dict[str, Any]) -> dict[str, str]:
    return {c["name"]: c["kind"] for c in info.get("commands", [])
            if isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("kind"), str)}


def _command_problems(project: Project, kinds: dict[str, dict[str, str]]) -> list[str]:
    from mes.jobs import job_problems

    out = job_problems(project, kinds, [j for j in project.jobs.values() if j.app in kinds])
    for spec in project.apps.values():
        r = spec.data.rebuild
        if r is None or spec.name not in kinds:
            continue
        kind = kinds[spec.name].get(r)
        if kind is None:
            out.append(f'The app {spec.name} names "{r}" as its [data] rebuild command, but it has no such command.')
        elif kind != "write":
            out.append(f'The [data] rebuild command "{r}" of {spec.name} is a {kind} command; it must be a write command.')
    return out


def write_lock(root: Path, pf: ProjectFile, located: list[Located], programs: dict[str, tuple[str, str]]) -> Path:
    """``programs``: package -> (app name, program)."""
    data = {
        "v": 1,
        "project": pf.name,
        "project_toml_sha256": pf.sha256,
        "apps": [{"package": loc.package, "name": programs[loc.package][0], "path": str(loc.folder),
                  "in_project": loc.in_project, "program": programs[loc.package][1]} for loc in located],
    }
    target = root / LOCK_FILE
    if target.is_symlink():
        raise Refused(f"{LOCK_FILE} is a link, so it was not overwritten.", hint=f"Remove the link {target}.",
                      code="link")
    fd, tmp = tempfile.mkstemp(prefix=".apps-lock-", suffix=".json", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def sync(root: str | Path, *, runner: Runner | None = None, strict: bool = True,
         update_database: bool = False) -> dict[str, Any]:
    """Find, check and lock the apps. ``strict=False`` (deploys) reports job problems instead of
    refusing, as ``mes deploy`` always has. ``update_database`` also runs ``mes db sync`` on an
    existing shared database (never creating one); a problem there becomes a warning, because the
    lock itself was written. Deploys leave it off: the release is not the current one yet, and the
    job units' ExecStartPre brings the database in line on the server."""
    root = Path(root).resolve()
    pf = manifest_mod.load_file(root)
    located = resolve(root, pf, runner=runner)
    project = manifest_mod.compose(pf, located)
    command = _bin(root, project.name)
    if not (command.is_file() and os.access(command, os.X_OK)):
        raise NotFound(
            f"The project's own command ({project.name}) is not installed in the project's environment, so agents "
            "could not reach the apps through it.",
            hint=f'Add {project.name} = "mes.project:main" under [project.scripts] in pyproject.toml, then run: uv sync',
            code="project_command_missing",
        )
    kinds: dict[str, dict[str, str]] = {}
    for spec in project.apps.values():
        kinds[spec.name] = _kinds(describe_app(root, spec, runner=runner))
    problems = _command_problems(project, kinds)
    if problems and strict:
        raise MesError(" ".join(problems) + " The list of apps was not written.",
                       hint="Fix the app.toml (or the app's commands), then run: mes apps sync", code="job_invalid")
    lock = write_lock(root, pf, located, {s.package: (s.name, s.program) for s in project.apps.values()})
    warnings = list(project.warnings)
    database = None
    if update_database and project.tables:
        from mes import db

        target = db.db_path(project)
        if target.is_file():
            try:
                done = db.sync(project, path=target)
                database = {"file": str(target), "views_recreated": done["views_recreated"],
                            "columns_added": done["columns_added"], "created": done["created"],
                            "problems": done["problems"]}
            except MesError as e:
                warnings.append(f"The list of apps was written, but the shared database was left as it was: "
                                f"{e.message} {e.hint or ''}".strip())
    return {
        "project": project.name,
        "lock": str(lock),
        "apps": [{"package": s.package, "name": s.name, "short": s.short, "location": s.location,
                  "path": str(s.folder), "program": s.program} for s in project.apps.values()],
        "jobs": list(project.jobs),
        "commands": kinds,
        "problems": problems,
        "warnings": warnings,
        "database": database,
        "message": f"The list of installed apps of {project.name} is up to date ({len(project.apps)} "
        f"app{'s' if len(project.apps) != 1 else ''}).",
    }


# --------------------------------------------------------------------------- check


def check(root: str | Path, *, runner: Runner | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    items: list[dict[str, Any]] = []

    def add(id: str, title: str, ok: bool | None, why_ok: str, why_not: str, fix: str | None = None) -> None:
        if ok is None:
            items.append(protocol.item(id, title, "skipped", why_not))
        else:
            items.append(protocol.item(id, title, "done" if ok else "todo", why_ok if ok else why_not,
                                       fix=None if ok else fix))

    try:
        pf = manifest_mod.load_file(root)
        located = resolve(root, pf, runner=runner)
        project = manifest_mod.compose(pf, located)
    except MesError as e:
        add("project", "project.toml and every app.toml are correct", False, "", e.message, e.hint)
        return protocol.checklist(items)
    add("project", "project.toml and every app.toml are correct", True,
        f"{len(project.apps)} app(s), {len(project.jobs)} job(s), {len(project.tables)} shared table(s).", "")
    try:
        manifest_mod.read_lock(root, pf)
        has_lock = (root / LOCK_FILE).exists()
        lock_ok: bool | None = True
        why = "apps.lock.json matches project.toml." if has_lock else \
            "There is no apps.lock.json; every app is inside the project, so it is not needed yet."
        if not has_lock and any(not loc.in_project for loc in located):
            lock_ok, why = False, "There is no apps.lock.json, and installed apps can only be found through it."
    except ManifestError as e:
        lock_ok, why = False, e.message
    add("lock", "The list of installed apps is up to date", lock_ok, why, why, "mes apps sync")
    owners: dict[str, str] = {}
    clash = []
    for spec in project.apps.values():
        for t in spec.data.tables:
            if t in owners:
                clash.append(f"{t} ({owners[t]} and {spec.name})")
            owners[t] = spec.name
    add("app-tables", "No two apps own the same app table", not clash, "Each app table has one owner.",
        "Tables owned twice: " + ", ".join(clash), "Rename one of the tables.")
    add("writes", "Every [data] writes names a shared table", True,
        "Each app writes only tables declared in project.toml.", "")
    kinds: dict[str, dict[str, str]] = {}
    broken = []
    for spec in project.apps.values():
        try:
            kinds[spec.name] = _kinds(describe_app(root, spec, runner=runner))
        except MesError as e:
            broken.append(e.message)
    add("programs", "Every app's program is installed and says it is that app", not broken,
        "Each app answers describe with its own name.", " ".join(broken), f"uv sync (in {root}), then: mes apps sync")
    problems = _command_problems(project, kinds)
    add("commands", "Jobs and rebuild commands are commands the apps have", not problems,
        "Every job runs a read or write command, and every rebuild is a write command.", " ".join(problems),
        "Change the run line in the app's app.toml (see: mes jobs list).")
    from mes import db

    try:
        result = db.check(project)
        db_ok = result["ok"] if result["exists"] else None
        add("database", "The shared database has the declared tables and columns", db_ok, result["message"],
            result["message"], "mes db sync")
        view_problems = result.get("problems") or []
    except MesError as e:
        add("database", "The shared database has the declared tables and columns", False, "", e.message, e.hint)
        view_problems = []
    if not view_problems:
        import sqlite3

        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.schema_sql(project))
            view_problems = db.check_views(conn, project)
        finally:
            conn.close()
    if project.views:
        add("views", "Every [views] entry only reads", not view_problems, "Each view is one read-only SELECT.",
            " ".join(view_problems), "Change the view's sql in project.toml.")
    missing_sections = []
    for spec in project.apps.values():
        for slug, rel in spec.ui.contributes:
            f = (spec.folder or root) / rel
            if not f.is_file() or f.is_symlink():
                missing_sections.append(f"{spec.name}: {rel}")
    known = {s for a in project.apps.values() for s in a.ui.dashboards}
    folder = root / "ui" / "dashboards"
    if folder.is_dir():
        known |= {p.stem for p in folder.glob("*.yaml")}
    missing_dash = [d for d in (project.ui.dashboards if project.ui else ()) if d not in known]
    add("dashboards", "The dashboards and contributed sections exist", not missing_sections and not missing_dash,
        "Every dashboard named in [ui] and every contributed sections file exists.",
        ("Missing sections files: " + ", ".join(missing_sections) + ". " if missing_sections else "")
        + ("[ui] dashboards names dashboards nobody has: " + ", ".join(missing_dash) + "." if missing_dash else ""),
        "Add the files, or remove the names (easy-ui validate explains more).")
    for i, w in enumerate(project.warnings):
        items.append(protocol.item(f"warning-{i + 1}", "Please note", "skipped", w))
    return protocol.checklist(items)


# --------------------------------------------------------------------------- command line


def _root(args: argparse.Namespace) -> Path:
    from mes.jobs import project_root

    return project_root(args.project_dir)


def _cmd_list(args: argparse.Namespace) -> Any:
    check_allowed("read")
    from mes.jobs import load_project

    root, project = load_project(args.project_dir)
    rows = [{**spec.to_dict(), "jobs": [j for j in project.jobs if project.jobs[j].app == spec.name]}
            for spec in project.apps.values()]
    if args.json:
        return {"project": project.name, "lock_used": project.lock_used, "apps": rows,
                "allowlist": project.allowlist(), "warnings": list(project.warnings)}
    table = protocol.format_text([{"app": r["name"], "short": r["short"], "package": r["package"],
                                   "where": r["location"], "jobs": len(r["jobs"]), "title": r["title"]} for r in rows])
    note = ("" if project.lock_used else
            "\n\nFound in the project's apps folder (no apps.lock.json yet). After installing apps, run: mes apps sync")
    warn = "".join(f"\nPlease note: {w}" for w in project.warnings)
    return table + note + warn


def _cmd_sync(args: argparse.Namespace) -> Any:
    check_allowed("write")
    result = sync(_root(args), update_database=True)
    if args.json:
        return result
    lines = [result["message"], f"Written to {result['lock']}."]
    lines += [f"- {a['name']} ({a['short']}): {a['location']}" for a in result["apps"]]
    from mes import db

    said = db.describe_changes(result["database"] or {})
    if said:
        lines.append(f"The shared database follows project.toml now. {said}")
    lines += [f"Please note: {w}" for w in result["warnings"]]
    return "\n".join(lines)


def _cmd_check(args: argparse.Namespace) -> Any:
    check_allowed("read")
    return check(_root(args))


def register(subparsers: Any) -> None:
    a = subparsers.add_parser(
        "apps",
        help="The apps of a project: list them, write the list of installed apps (sync), check them.",
        description="A project lists its apps (Python packages with an app.toml) under [project] apps. "
        "'mes apps sync' finds them and writes apps.lock.json. See: mes guide apps",
    )
    sub = a.add_subparsers(dest="apps_command", metavar="<list|sync|check>")
    for name, helptext, func in (
        ("list", "The project's apps, where each one is, and its jobs.", _cmd_list),
        ("sync", "Find every app, check them, and write apps.lock.json (run after uv sync).", _cmd_sync),
        ("check", "Check the apps, their programs, jobs, tables and dashboards (a checklist).", _cmd_check),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--project-dir", default=None, metavar="DIR", help="the project's folder (default: here)")
        p.set_defaults(func=func)


__all__ = ["sync", "check", "resolve", "find_installed", "describe_app", "write_lock", "register", "FIND_SCRIPT"]
