"""``mes new <name>``: create a new project with one app; ``mes new-app <name>``: add an app.

A new project has these files (``<module>`` is the name with ``-`` replaced by ``_``; the app is
``<name>-main`` in the package ``<module>_main`` unless ``--app`` names another short name)::

    project.toml                       the project: its apps, its shared table "daily", its dashboards
    pyproject.toml                     how uv installs it: the project command and the app's program
    AGENTS.md                          how an AI agent uses the project (``<name> guide`` adds each app's)
    .gitignore                         keeps .venv/ and apps.lock.json (this computer's paths) out of git
    apps/<package>/__init__.py
    apps/<package>/app.toml            the app: its tables, its rebuild, its job
    apps/<package>/cli.py              notes (read), add (write), rebuild (write)
    apps/<package>/AGENTS.md           the app's commands and tables
    ui/dashboards/overview.yaml        reads the shared view "daily"; apps may add sections to it
    ui/fixtures/sample.sqlite          made-up data made with the schema script

``mes new-app <name>`` (inside a project) adds ``apps/<package>/`` with ``__init__.py``,
``app.toml``, ``cli.py``, ``AGENTS.md`` and ``dashboards/<short>.yaml``, and edits two lines: the
package is appended to a one-line ``apps = [...]`` in ``project.toml``, and the app's script and
folder go into ``[project.scripts]`` and a one-line ``packages = [...]`` of ``pyproject.toml``.
When a line is not in that simple form, that file is left as it is and the exact line to add is
given instead.

Nothing is ever written through a symlink: an existing folder on the way to a new file must be a
real folder inside the project's folder. The texts live in ``src/mes/templates/project/`` and
``src/mes/templates/app/`` and use ``{{placeholder}}`` markers.

my-easy-software is not published yet, so the generated ``pyproject.toml`` gets a
``[tool.uv.sources]`` entry pointing at a folder holding the framework: the ``--framework-path``
given, or else the source checkout mes itself runs from.

Public API
----------
``FILES``, ``APP_FILES``  ``(template name, path)`` pairs; ``{package}`` / ``{short}`` are replaced.
``FIXTURE``  the path of the sample data file.  ``FRAMEWORK_NAME``  ``"my-easy-software"``.
``DEFAULT_SHORT``  ``"main"``.
``validate_name(name) -> str``  checks the project name and returns the Python module name.
``app_names(project, short) -> (app, package, prefix)``
``default_framework_path() -> Path | None``, ``check_framework_path(path) -> Path``
``render(name, *, title=None, framework_path=None, app_dir=None, short="main") -> dict[str, str]``
``sample_data(project) -> bytes``  the SQLite file of made-up data for a composed project.
``create(name, *, directory=None, title=None, framework_path=None, cwd=None, short="main") -> dict``
``new_app(name, *, project_dir=None, title=None) -> dict``
``format_result(result) -> str``, ``register(subparsers)``
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import random
import sqlite3
import tempfile
import keyword
import os
import re
import shlex
import sys
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

from mes import __version__
from mes.app import check_allowed
from mes.errors import MesError, NotFound, Refused, UsageError
from mes import manifest as manifest_mod
from mes.manifest import APP_NAME_RE, NOT_APP_PROGRAMS, PACKAGE_RE, RESERVED_APP_NAMES, RESERVED_SHORT_NAMES

FRAMEWORK_NAME = "my-easy-software"
DEFAULT_SHORT = "main"

FILES: tuple[tuple[str, str], ...] = (
    ("project.toml.tmpl", "project.toml"),
    ("pyproject.toml.tmpl", "pyproject.toml"),
    ("AGENTS.md.tmpl", "AGENTS.md"),
    ("gitignore.tmpl", ".gitignore"),
    ("", "apps/{package}/__init__.py"),
    ("app.toml.tmpl", "apps/{package}/app.toml"),
    ("cli.py.tmpl", "apps/{package}/cli.py"),
    ("app-AGENTS.md.tmpl", "apps/{package}/AGENTS.md"),
    ("overview.yaml.tmpl", "ui/dashboards/overview.yaml"),
)
APP_FILES: tuple[tuple[str, str], ...] = (
    ("", "apps/{package}/__init__.py"),
    ("app.toml.tmpl", "apps/{package}/app.toml"),
    ("cli.py.tmpl", "apps/{package}/cli.py"),
    ("AGENTS.md.tmpl", "apps/{package}/AGENTS.md"),
    ("dashboard.yaml.tmpl", "apps/{package}/dashboards/{short}.yaml"),
)
FIXTURE = "ui/fixtures/sample.sqlite"

PURPOSES = {
    "project.toml": "what the project is: its apps, its shared table and its dashboards",
    "pyproject.toml": "how uv installs the project: the project command and each app's program",
    "AGENTS.md": "how an AI agent should use the project",
    "__init__.py": "makes the app's folder a Python package",
    "app.toml": "what the app is: its commands' program, its tables, its job",
    "cli.py": "the app's commands (data in the project's shared database)",
    "ui/dashboards/overview.yaml": "the overview dashboard (apps can add sections to it)",
    FIXTURE: "made-up data, so the dashboard shows something before there is real data",
    ".gitignore": "keeps this computer's private files (such as .venv/ and apps.lock.json) out of the project's code",
}

# Names that would clash with Python itself or with the framework.
_RESERVED_MODULES = {"mes", "my_easy_software", "src", "test", "tests"}
_RESERVED_NAMES = {FRAMEWORK_NAME, "my-easy-ui", "my-bucket-storage"}
_TITLE_MAX = 80


# --------------------------------------------------------------------------- checks


def validate_name(name: Any) -> str:
    """Return the module name for ``name`` or raise ``UsageError``."""
    if not isinstance(name, str) or not APP_NAME_RE.match(name) or name in RESERVED_APP_NAMES \
            or NOT_APP_PROGRAMS.match(name):
        raise UsageError(
            f'"{name}" cannot be used as a project name. The name becomes a folder, a command, accounts '
            "and services, so it must be simple.",
            hint="Use 2 to 31 characters: lowercase letters, digits and dashes, starting with a "
            'letter, for example "notes" or "my-notes".',
        )
    module = name.replace("-", "_")
    if (
        name in _RESERVED_NAMES
        or module in _RESERVED_MODULES
        or keyword.iskeyword(module)
        or keyword.issoftkeyword(module)
        or module in sys.stdlib_module_names
    ):
        raise UsageError(
            f'"{name}" is already used by Python or by my-easy-software itself, so a project with that '
            "name would not work.",
            hint=f'Pick another name, for example "my-{name}".',
        )
    return module


def app_names(project: str, short: str) -> tuple[str, str, str]:
    """``(app name, package, table prefix)`` for the app ``short`` of ``project``."""
    module = project.replace("-", "_")
    if short.startswith(project + "-"):
        short = short[len(project) + 1:]
    app = f"{project}-{short}"
    package = f"{module}_{short.replace('-', '_')}"
    ok = (isinstance(short, str) and manifest_mod.JOB_NAME_RE.match(short) and APP_NAME_RE.match(app)
          and PACKAGE_RE.match(package) and short not in RESERVED_SHORT_NAMES and not NOT_APP_PROGRAMS.match(app)
          and package not in sys.stdlib_module_names and not keyword.iskeyword(package))
    if not ok:
        raise UsageError(
            f'"{short}" cannot be used as the name of an app of "{project}". The app would be called "{app}" (in the '
            f'package {package}), and that name must be simple and free.',
            hint="Use a short word of lowercase letters, digits and dashes, starting with a letter, such as "
            '"notes" or "rides" (not describe, guide, apps or help), so that the whole app name has at most 31 '
            "characters.",
        )
    return app, package, manifest_mod.table_prefix(short)


def _check_title(title: str) -> str:
    title = title.strip()
    if not title or len(title) > _TITLE_MAX or any(ord(c) < 32 or ord(c) == 127 for c in title):
        raise UsageError(
            "The title must be one line of text, 1 to 80 characters long.",
            hint='Leave --title out to use the name, or give a short title such as --title "My notes".',
        )
    return title


def _default_title(name: str) -> str:
    return name.replace("-", " ").capitalize()


def _is_framework(folder: Path) -> bool:
    try:
        data = tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    return data.get("project", {}).get("name") == FRAMEWORK_NAME


def default_framework_path() -> Path | None:
    """The source checkout the running mes comes from (``<root>/src/mes``), if any."""
    import mes

    here = Path(mes.__file__).resolve().parent
    if here.parent.name != "src":
        return None
    root = here.parent.parent
    return root if _is_framework(root) else None


def check_framework_path(path: str | Path) -> Path:
    folder = Path(path).expanduser().resolve()
    if not _is_framework(folder):
        raise NotFound(
            f"{folder} is not a my-easy-software folder (it has no pyproject.toml for "
            f'"{FRAMEWORK_NAME}"), so the new project could not be connected to the framework.',
            hint="Give the folder where my-easy-software was downloaded (the one holding its "
            "pyproject.toml) with --framework-path, or leave the option out.",
        )
    return folder


# --------------------------------------------------------------------------- rendering


def _toml_str(text: str) -> str:
    # JSON string escapes are valid TOML basic-string escapes for one-line text.
    return json.dumps(text, ensure_ascii=False)


def _yaml_str(text: str) -> str:
    # A JSON string is a valid YAML double-quoted scalar.
    return json.dumps(text, ensure_ascii=True)


def _source_path(framework: Path, app_dir: Path | None) -> str:
    """Relative to the app folder when they share a folder below the top of the disk."""
    framework = framework.resolve()
    if app_dir is None:
        return framework.as_posix()
    app_dir = app_dir.resolve()
    try:
        common = Path(os.path.commonpath([framework, app_dir]))
    except ValueError:  # different drives on Windows
        return framework.as_posix()
    if common == Path(common.anchor):
        return framework.as_posix()
    return Path(os.path.relpath(framework, app_dir)).as_posix()


def _uv_sources(source: str | None) -> str:
    if source is None:
        return ""
    return (
        "\n# my-easy-software is not published yet, so uv takes it from a folder on this computer.\n"
        "# This line only works where that folder exists; remove it once the framework is published.\n"
        "[tool.uv.sources]\n"
        f"{FRAMEWORK_NAME} = {{ path = {_toml_str(source)}, editable = true }}\n"
    )


def _template(name: str, kind: str = "project") -> str:
    if not name:
        return ""
    return (resources.files("mes") / "templates" / kind / name).read_text(encoding="utf-8")


def _fill(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def sample_data(project: manifest_mod.Project, today: _dt.date | None = None) -> bytes:
    """A small SQLite file made with the project's own schema script: the shared table "daily"
    filled with made-up numbers for the last 60 days, so the dashboard preview shows something
    before there is real data."""
    from mes import db

    today = today or _dt.datetime.now(_dt.UTC).date()
    rnd = random.Random(7)
    spec = next(iter(project.apps.values()))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.sqlite"
        conn = sqlite3.connect(path)
        try:
            conn.executescript(db.schema_sql(project))
            rows = [(spec.name, (today - _dt.timedelta(days=i)).isoformat(), rnd.randint(0, 6)) for i in range(60)]
            conn.executemany('INSERT INTO "daily__rows" ("source", "day", "notes") VALUES (?, ?, ?)', rows)
            notes = f"{spec.prefix}notes"
            conn.execute(f'CREATE TABLE "{notes}" (id INTEGER PRIMARY KEY, time TEXT NOT NULL, text TEXT NOT NULL)')
            conn.execute(f'INSERT INTO "{notes}" (time, text) VALUES (?, ?)', (f"{today.isoformat()}T08:00:00+00:00",
                                                                             "A made-up note"))
            conn.commit()
        finally:
            conn.close()
        return path.read_bytes()


def render(
    name: str,
    *,
    title: str | None = None,
    framework_path: str | Path | None = None,
    app_dir: str | Path | None = None,
    short: str = DEFAULT_SHORT,
) -> dict[str, str]:
    """The text files as ``{relative path: text}``. Nothing is written."""
    module = validate_name(name)
    title = _check_title(title) if title is not None else _default_title(name)
    app, package, prefix = app_names(name, short)
    short = manifest_mod.short_name(app, name)
    source = None
    if framework_path is not None:
        source = _source_path(Path(framework_path), Path(app_dir) if app_dir is not None else None)
    app_title = short.replace("-", " ").capitalize() if short != DEFAULT_SHORT else title
    values = {
        "name": name,
        "module": module,
        "title": title,
        "title_toml": _toml_str(title),
        "title_yaml": _yaml_str(title),
        "app": app,
        "package": package,
        "prefix": prefix,
        "short": short,
        "app_title": app_title,
        "app_title_toml": _toml_str(app_title),
        "mes_version": __version__,
        "uv_sources": _uv_sources(source),
    }
    out: dict[str, str] = {}
    for tmpl, target in FILES:
        text = _fill(_template(tmpl), values)
        out[target.format(package=package)] = text.rstrip("\n") + "\n" if text else ""
    return out


# --------------------------------------------------------------------------- creating


def _home_name_in(path_text: str) -> bool:
    try:
        home = Path.home()
    except RuntimeError:
        return False
    parts = Path(path_text).parts
    return bool(home.name) and home.name in parts and str(home) != "/"


def _compose_texts(texts: dict[str, str]) -> manifest_mod.Project:
    """The project the rendered files describe (written to a temporary folder and read back)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in texts.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        return manifest_mod.load(root)


def _write_files(folder: Path, files: dict[str, str | bytes], *, where: str) -> list[Path]:
    """Write new files only (never overwrite, never through a link); undo everything on failure."""
    existing = sorted(rel for rel in files if (folder / rel).exists() or (folder / rel).is_symlink())
    linked = _symlinked_parts(folder, files)
    if linked:
        raise Refused(
            f"{linked} is a link to another place, so files could end up outside the project's folder. Nothing was "
            "changed.",
            hint=f"Choose an empty or new folder{' with ' + where if where else ''}, or replace the link with a real "
            "folder.",
        )
    if existing:
        raise Refused(
            f"{folder} already has {', '.join(existing)}. Nothing was changed, so no work is lost.",
            hint=f"Choose an empty or new folder with {where}, or another name." if where else "Choose another name.",
        )
    written: list[Path] = []
    created_dirs: list[Path] = []
    try:
        for rel, text in files.items():
            target = folder / rel
            missing = [d for d in (target.parent, *target.parent.parents) if not d.exists()]
            target.parent.mkdir(parents=True, exist_ok=True)
            created_dirs.extend(reversed(missing))
            if isinstance(text, bytes):
                with target.open("xb") as fb:  # "x": never overwrite
                    fb.write(text)
            else:
                with target.open("x", encoding="utf-8") as f:
                    f.write(text)
            written.append(target)
    except FileExistsError:
        _undo(written, created_dirs)
        raise Refused(
            f"A file appeared in {folder} while the files were being created, so creation stopped and "
            "what was already written was removed.",
            hint="Run the command again.",
        ) from None
    except OSError as e:
        _undo(written, created_dirs)
        raise MesError(
            f"The files could not be written to {folder}, so nothing was left behind.",
            hint="Check that the folder is on a disk with free space that you are allowed to write to"
            + (f", or choose another place with {where}." if where else "."),
            code="write_failed",
            detail=f"{type(e).__name__}: {e}",
        ) from None

    return written


def create(
    name: str,
    *,
    directory: str | Path | None = None,
    title: str | None = None,
    framework_path: str | Path | None = None,
    cwd: str | Path | None = None,
    short: str = DEFAULT_SHORT,
) -> dict[str, Any]:
    """Write a new project. ``directory`` is its own folder (default ``<cwd>/<name>``)."""
    module = validate_name(name)
    app, package, _ = app_names(name, short)
    short = manifest_mod.short_name(app, name)
    base = Path(cwd) if cwd is not None else Path.cwd()
    folder = (base / (Path(directory).expanduser() if directory is not None else Path(name))).resolve()

    if framework_path is not None:
        framework = check_framework_path(framework_path)
    else:
        framework = default_framework_path()

    if folder.exists() and not folder.is_dir():
        raise Refused(
            f"{folder} already exists and is a file, not a folder, so the project was not created there.",
            hint="Choose another place with --dir, or another name.",
        )
    if framework is not None and (folder == framework or framework in folder.parents):
        raise Refused(
            "The new project would be created inside the my-easy-software folder itself, which would "
            "mix the project's code with the framework's.",
            hint="Create it somewhere else, for example next to it: --dir ../" + name,
        )

    texts = render(name, title=title, framework_path=framework, app_dir=folder, short=short)
    files: dict[str, str | bytes] = {**texts, FIXTURE: sample_data(_compose_texts(texts))}
    _write_files(folder, files, where="--dir")

    source_line = None
    notes: list[str] = []
    if framework is None:
        notes.append(
            "my-easy-software is not published yet, and this mes is not running from a downloaded "
            "copy of it, so 'uv sync' in the new project will not find it. Run mes new again with "
            "--framework-path <folder of my-easy-software> to connect them."
        )
    else:
        source_line = _source_path(framework, folder)
        if _home_name_in(source_line):
            notes.append(
                "pyproject.toml points to my-easy-software with a path that contains the name of your "
                f"home folder ({source_line}). After 'uv sync', uv.lock holds the same path. Before you "
                "publish this app's code, keep the app next to the my-easy-software folder (so the path "
                "becomes short, like ../my-easy-software) or remove the [tool.uv.sources] section once "
                "the framework is published, then run 'uv sync' again. (mes deploy only accepts a framework "
                "folder right next to the project.)"
            )

    q = shlex.quote(str(folder))
    steps = [
        {
            "step": "Install what the project needs",
            "command": f"cd {q} && uv sync",
            "why": "This prepares a private Python setup inside the project folder. Nothing is installed "
            "for the rest of the computer.",
        },
        {
            "step": "Write the list of the project's apps",
            "command": f"cd {q} && uv run mes apps sync",
            "why": "It finds every app of the project, checks them, and writes apps.lock.json, which the "
            "dashboards and the project command use to find the apps.",
        },
        {
            "step": "Save a first note and read it back",
            "command": f'cd {q} && uv run {name} {short} add "My first note" && uv run {name} {short} notes',
            "why": "This checks that the app works and can store data on this computer (never inside "
            "the project folder).",
        },
        {
            "step": "Run the job that builds the dashboard's table",
            "command": f"cd {q} && uv run mes jobs run {app}.rebuild",
            "why": "Jobs keep the project's tables up to date on their own. This one counts notes per day.",
        },
        {
            "step": "See the dashboard",
            "command": f"cd {q} && uv run mes dev",
            "why": "It opens a preview of the dashboard on this computer only and prints one link. It "
            "shows made-up sample data until the project has real data, and updates when files change. "
            "It needs easy-ui (my-easy-ui).",
        },
        {
            "step": "Make it yours",
            "command": None,
            "why": f"Change the commands in apps/{package}/cli.py and the dashboard in ui/dashboards/overview.yaml, "
            "add apps with 'mes new-app <name>', and keep AGENTS.md up to date so AI agents know how to use the "
            "project. To put it on a server later, keep it in git (git init) and see: mes guide deploy",
        },
    ]
    return {
        "created": True,
        "project": name,
        "title": title.strip() if title else _default_title(name),
        "module": module,
        "app": app,
        "package": package,
        "folder": str(folder),
        "files": [
            {"path": rel, "purpose": PURPOSES.get(rel, PURPOSES.get(Path(rel).name, ""))}
            for rel in files
        ],
        "short": short,
        "framework_path": source_line,
        "next_steps": steps,
        "notes": notes,
    }


def _symlinked_parts(folder: Path, files: dict[str, str]) -> str | None:
    """The first existing folder between ``folder`` and a new file that is a symlink (or that
    resolves outside ``folder``), or None."""
    for rel in files:
        cur = folder
        for part in Path(rel).parts[:-1]:
            cur = cur / part
            if cur.is_symlink():
                return str(cur)
            if cur.exists() and not (cur.resolve() == folder or folder in cur.resolve().parents):
                return str(cur)
    return None


def _undo(written: list[Path], created_dirs: list[Path]) -> None:
    for p in reversed(written):
        try:
            p.unlink()
        except OSError:
            pass
    for d in reversed(created_dirs):
        try:
            d.rmdir()
        except OSError:
            pass


# --------------------------------------------------------------------------- mes new-app

_APPS_LINE = re.compile(r"^(?P<indent>[ \t]*)apps[ \t]*=[ \t]*\[(?P<items>[^\]\n#]*)\](?P<rest>[ \t]*(?:#.*)?)$", re.M)
_PACKAGES_LINE = re.compile(r"^(?P<indent>[ \t]*)packages[ \t]*=[ \t]*\[(?P<items>[^\]\n#]*)\](?P<rest>[ \t]*(?:#.*)?)$",
                            re.M)
_ITEM = re.compile(r'^\s*"([^"\\]*)"\s*$')


def _section(text: str, header: str) -> tuple[int, int] | None:
    """Start and end offsets of a ``[header]`` section (up to the next ``[`` header)."""
    m = re.search(rf"^\[{re.escape(header)}\][ \t]*(?:#.*)?$", text, re.M)
    if not m:
        return None
    nxt = re.search(r"^\[", text[m.end():], re.M)
    return m.end(), (m.end() + nxt.start()) if nxt else len(text)


def _append_to_list(text: str, header: str, pattern: re.Pattern[str], value: str) -> str | None:
    """``text`` with ``value`` appended to the one-line list in ``[header]``, or None when the line
    is not in that simple form."""
    span = _section(text, header)
    if span is None:
        return None
    body = text[span[0]:span[1]]
    found = list(pattern.finditer(body))
    if len(found) != 1:
        return None
    m = found[0]
    raw = [x for x in m.group("items").split(",") if x.strip()]
    items = []
    for x in raw:
        im = _ITEM.match(x)
        if not im:
            return None
        items.append(im.group(1))
    if value in items:
        return text
    items.append(value)
    key = "apps" if pattern is _APPS_LINE else "packages"
    line = f'{m.group("indent")}{key} = [' + ", ".join(json.dumps(i) for i in items) + "]" + m.group("rest")
    start = span[0] + m.start()
    end = span[0] + m.end()
    return text[:start] + line + text[end:]


def _add_script(text: str, app: str, target: str) -> str | None:
    span = _section(text, "project.scripts")
    if span is None:
        return None
    body = text[span[0]:span[1]]
    if re.search(rf'^[ \t]*"?{re.escape(app)}"?[ \t]*=', body, re.M):
        return None  # already there: leave it to the person
    insert = f'{app} = "{target}"\n'
    head = body.rstrip("\n")
    trailing = body[len(head):]  # the newlines after the last line, kept (minus the one ending it)
    new_body = head + "\n" + insert + trailing[1:] if head.strip() else "\n" + insert + body.lstrip("\n")
    if not new_body.endswith("\n\n") and span[1] < len(text):
        new_body = new_body.rstrip("\n") + "\n\n"
    return text[:span[0]] + new_body + text[span[1]:]


def _safe_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise Refused(f"{path.name} is a link or not a plain file, so mes will not change it. Nothing was changed.",
                      hint=f"Replace {path} with the real file.", code="link")


def new_app(name: str, *, project_dir: str | Path | None = None, title: str | None = None) -> dict[str, Any]:
    """Add an app to the project in ``project_dir`` (default: the project of this folder)."""
    from mes.jobs import project_root

    root = project_root(project_dir)
    pf = manifest_mod.load_file(root)
    app, package, prefix = app_names(pf.name, name)
    short = manifest_mod.short_name(app, pf.name)
    if package in pf.packages:
        raise Refused(f'The project already lists the package {package}, so the app was not added again.',
                      hint="Choose another name.", code="exists")
    try:
        project = manifest_mod.load(root)
        taken = {w for a in project.apps.values() for w in (a.name, a.short)}
        prefixes = [a.prefix for a in project.apps.values()]
        folders = {a.data.folder for a in project.apps.values()}
    except MesError:
        located = [loc for loc in (_in_project(root, p) for p in pf.packages) if loc is not None]
        taken, prefixes, folders = set(), [], set()
        for loc in located:
            af = manifest_mod.load_app_file(loc / manifest_mod.APP_FILE)
            s = manifest_mod.short_name(af.name, pf.name)
            taken |= {af.name, s}
            prefixes.append(manifest_mod.table_prefix(s))
            folders.add(af.data.folder or s)
    clash = [w for w in (app, short) if w in taken]
    if clash or any(prefix.startswith(p) or p.startswith(prefix) for p in prefixes) or short in folders:
        raise Refused(f'The project already has an app that can be reached as "{clash[0] if clash else short}" (or '
                      "whose tables or folder would overlap), so the app was not added.",
                      hint="Choose another name.", code="exists")
    title_text = _check_title(title) if title is not None else short.replace("-", " ").capitalize()
    values = {"name": pf.name, "app": app, "package": package, "prefix": prefix, "short": short,
              "app_title": title_text, "app_title_toml": _toml_str(title_text), "app_title_yaml": _yaml_str(title_text)}
    files = {}
    for tmpl, target in APP_FILES:
        text = _fill(_template(tmpl, "app"), values)
        files[target.format(package=package, short=short)] = text.rstrip("\n") + "\n" if text else ""
    folder = root / "apps" / package
    if folder.exists() or folder.is_symlink():
        raise Refused(f"{folder} already exists, so the app was not created there. Nothing was changed.",
                      hint="Choose another name, or move that folder away.", code="exists")
    for rel in ("project.toml", "pyproject.toml"):
        if (root / rel).exists():
            _safe_file(root / rel)
    _write_files(root, files, where="")
    notes: list[str] = []
    changed: list[str] = []
    ptext = (root / "project.toml").read_text(encoding="utf-8")
    new_ptext = _append_to_list(ptext, "project", _APPS_LINE, package)
    if new_ptext is None:
        notes.append(f'project.toml was not changed, because its apps line is not a simple one-line list. Add the '
                     f'package yourself: apps = [..., "{package}"] under [project].')
    elif new_ptext != ptext:
        (root / "project.toml").write_text(new_ptext, encoding="utf-8")
        changed.append("project.toml")
    py = root / "pyproject.toml"
    if py.is_file():
        text = py.read_text(encoding="utf-8")
        with_script = _add_script(text, app, f"{package}.cli:main")
        if with_script is None:
            notes.append(f'pyproject.toml was not changed for the app\'s program. Add this line under '
                         f'[project.scripts]: {app} = "{package}.cli:main"')
            with_script = text
        with_pkg = _append_to_list(with_script, "tool.hatch.build.targets.wheel", _PACKAGES_LINE, f"apps/{package}")
        if with_pkg is None:
            notes.append(f'pyproject.toml has no simple one-line packages = [...] under [tool.hatch.build.targets.wheel]. '
                         f'Add the app\'s folder to it: "apps/{package}"')
            with_pkg = with_script
        if with_pkg != text:
            try:
                tomllib.loads(with_pkg)
            except tomllib.TOMLDecodeError:
                notes.append(f'pyproject.toml was not changed (the edit would not be valid). Add {app} = '
                             f'"{package}.cli:main" under [project.scripts] and "apps/{package}" to packages.')
            else:
                py.write_text(with_pkg, encoding="utf-8")
                changed.append("pyproject.toml")
        try:
            deps = tomllib.loads(py.read_text(encoding="utf-8")).get("project", {}).get("dependencies", [])
        except tomllib.TOMLDecodeError:
            deps = []
        if not any(isinstance(d, str) and re.match(rf"{re.escape(FRAMEWORK_NAME)}\b", d.strip()) for d in deps):
            notes.append(f"The new app's commands use my-easy-software (mes.App in cli.py), but this project does "
                         f"not depend on it, so the app's program would not start. Add {FRAMEWORK_NAME} to the "
                         "dependencies in pyproject.toml (for example: uv add my-easy-software), or write the app's "
                         "commands without it.")
    else:
        notes.append(f'There is no pyproject.toml, so the app\'s program is not set up. Add {app} = '
                     f'"{package}.cli:main" under [project.scripts].')
    q = shlex.quote(str(root))
    return {
        "created": True,
        "project": pf.name,
        "app": app,
        "short": short,
        "package": package,
        "prefix": prefix,
        "folder": str(folder),
        "files": [{"path": rel, "purpose": PURPOSES.get(Path(rel).name, "the app's dashboard")} for rel in files],
        "changed": changed,
        "notes": notes,
        "next_steps": [
            {"step": "Install the app's program", "command": f"cd {q} && uv sync",
             "why": "It installs the new app's command in the project's own environment."},
            {"step": "Write the list of apps again", "command": f"cd {q} && uv run mes apps sync",
             "why": "The project and its dashboards find the new app through it."},
            {"step": "Try it", "command": f'cd {q} && uv run {pf.name} {short} add "First item" && '
             f"uv run mes jobs run {app}.rebuild", "why": "It saves an item and builds the app's table."},
        ],
    }


def _in_project(root: Path, package: str) -> Path | None:
    folder = root / "apps" / package
    return folder if (folder / manifest_mod.APP_FILE).is_file() and not folder.is_symlink() else None


# --------------------------------------------------------------------------- cli


def format_result(result: dict[str, Any]) -> str:
    if "app" in result and "module" not in result:
        lines = [f'Added the app "{result["app"]}" (reach it as: {result["project"]} {result["short"]} ...) in '
                 f'{result["folder"]}', "", "Files:"]
    else:
        lines = [f'Created the project "{result["project"]}" in {result["folder"]}', "", "Files:"]
    width = max(len(f["path"]) for f in result["files"])
    lines += [f"  {f['path'].ljust(width)}  {f['purpose']}" for f in result["files"]]
    lines += ["", "Next steps:"]
    for i, s in enumerate(result["next_steps"], 1):
        lines.append(f"  {i}. {s['step']}: {s['why']}")
        if s["command"]:
            lines.append(f"     {s['command']}")
    if result.get("changed"):
        lines += ["", "Changed: " + ", ".join(result["changed"])]
    if result["notes"]:
        lines += ["", "Please note:"]
        lines += [f"  - {n}" for n in result["notes"]]
    return "\n".join(lines)


def _handle(args: argparse.Namespace) -> Any:
    check_allowed("write")  # it writes a new folder of files
    result = create(
        args.name,
        directory=args.target_dir,
        title=args.title,
        framework_path=args.framework_path,
        short=args.app,
    )
    return result if args.json else format_result(result)


def _handle_new_app(args: argparse.Namespace) -> Any:
    check_allowed("write")  # it writes new files and edits two lines
    result = new_app(args.name, project_dir=args.project_dir, title=args.title)
    return result if args.json else format_result(result)


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "new",
        help="Create a new project with one app: project.toml, apps/<app>/, AGENTS.md, a dashboard.",
        description="Create the folder of a new project with one app, whose data goes into the project's "
        "shared database and whose dashboard works right away. Existing files are never overwritten.",
    )
    p.add_argument("name", help="the project's name: 2 to 31 lowercase letters, digits and dashes")
    p.add_argument("--dir", dest="target_dir", metavar="PATH", default=None,
                   help="the folder for the project (default: a new folder named after it, here)")
    p.add_argument("--title", default=None, help="a short human title (default: from the name)")
    p.add_argument("--framework-path", dest="framework_path", metavar="PATH", default=None,
                   help="the folder of my-easy-software to use, while it is not published "
                   "(default: the copy this mes runs from, when it is one)")
    p.add_argument("--app", default=DEFAULT_SHORT, metavar="SHORT",
                   help=f'the short name of its first app (default "{DEFAULT_SHORT}": the app <name>-{DEFAULT_SHORT})')
    p.set_defaults(func=_handle)

    a = subparsers.add_parser(
        "new-app",
        help="Add an app to the project in this folder: apps/<package>/ with app.toml, cli.py and a dashboard.",
        description="Add a new app to a project. Its name is a short name (such as \"rides\") or a full name "
        "starting with the project's name. Existing files are never overwritten. See: mes guide add-app",
    )
    a.add_argument("name", help="the app's short name, for example rides (the app becomes <project>-rides)")
    a.add_argument("--title", default=None, help="a short human title")
    a.add_argument("--project-dir", default=None, metavar="DIR", help="the project's folder (default: here)")
    a.set_defaults(func=_handle_new_app)
