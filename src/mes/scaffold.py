"""``mes new <name>``: create the folder of a new app.

A new app has five files, plus a ``.gitignore``::

    app.toml                   what the app is, and its jobs
    pyproject.toml             how uv installs it (depends on my-easy-software)
    src/<module>/cli.py        its command-line tool: one read command (notes),
                               one write command (add) and the job command (rebuild)
    AGENTS.md                  how an AI agent uses the app
    ui/dashboards/main.yaml    a placeholder dashboard
    .gitignore                 keeps .venv/ (full of this computer's paths) out of git

Nothing is ever written through a symlink: an existing folder on the way to a
new file must be a real folder inside the app's folder.

``<module>`` is the app name with ``-`` replaced by ``_``. The texts live in
``src/mes/templates/app/*.tmpl`` and use ``{{placeholder}}`` markers.

my-easy-software is not published yet, so the generated ``pyproject.toml`` gets
a ``[tool.uv.sources]`` entry pointing at a folder holding the framework: the
``--framework-path`` given, or else the source checkout mes itself runs from.
The path is written relative to the app folder when possible.

Public API
----------
``FILES``  ``(template name, path in the new app)`` pairs; ``{module}`` in a path
    is replaced by the module name.
``FRAMEWORK_NAME``  ``"my-easy-software"``.
``validate_name(name) -> str``  checks the app name and returns the Python module
    name; raises ``UsageError`` (exit 2) in plain words.
``default_framework_path() -> Path | None``  the framework source checkout that
    the running mes comes from, or ``None`` when mes is installed normally.
``check_framework_path(path) -> Path``  raises ``NotFound`` unless ``path`` is a
    my-easy-software source folder.
``render(name, *, title=None, framework_path=None, app_dir=None) -> dict[str, str]``
    the five files as ``{relative path: text}``; nothing is written.
``create(name, *, directory=None, title=None, framework_path=None, cwd=None) -> dict``
    writes the app and returns what was created plus the next steps. Raises
    ``Refused`` (exit 5) instead of overwriting any existing file.
``format_result(result) -> str``  the plain-text form of ``create``'s result.
``register(subparsers)``  adds ``new`` to the ``mes`` command line.
"""

from __future__ import annotations

import argparse
import json
import keyword
import os
import shlex
import sys
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

from mes import __version__
from mes.app import check_allowed
from mes.errors import MesError, NotFound, Refused, UsageError
from mes.manifest import APP_NAME_RE

FRAMEWORK_NAME = "my-easy-software"

FILES: tuple[tuple[str, str], ...] = (
    ("app.toml.tmpl", "app.toml"),
    ("pyproject.toml.tmpl", "pyproject.toml"),
    ("cli.py.tmpl", "src/{module}/cli.py"),
    ("AGENTS.md.tmpl", "AGENTS.md"),
    ("main.yaml.tmpl", "ui/dashboards/main.yaml"),
    ("gitignore.tmpl", ".gitignore"),
)

PURPOSES = {
    "app.toml": "what the app is and its jobs",
    "pyproject.toml": "how uv installs the app and what it needs",
    "cli.py": "the app's command-line tool, with its commands",
    "AGENTS.md": "how an AI agent should use the app",
    "ui/dashboards/main.yaml": "the main dashboard (a placeholder for now)",
    ".gitignore": "keeps this computer's private files (such as .venv/) out of the app's code",
}

# Names that would clash with Python itself or with the framework.
_RESERVED_MODULES = {"mes", "my_easy_software", "src", "test", "tests"}
_RESERVED_NAMES = {FRAMEWORK_NAME, "my-easy-ui", "my-bucket-storage"}
_TITLE_MAX = 80


# --------------------------------------------------------------------------- checks


def validate_name(name: Any) -> str:
    """Return the module name for ``name`` or raise ``UsageError``."""
    if not isinstance(name, str) or not APP_NAME_RE.match(name):
        raise UsageError(
            f'"{name}" cannot be used as an app name. The name becomes a folder, a command and a '
            "background service, so it must be simple.",
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
            f'"{name}" is already used by Python or by my-easy-software itself, so an app with that '
            "name would not work.",
            hint=f'Pick another name, for example "my-{name}".',
        )
    return module


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
            f'"{FRAMEWORK_NAME}"), so the new app could not be connected to the framework.',
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


def _template(name: str) -> str:
    return (resources.files("mes") / "templates" / "app" / name).read_text(encoding="utf-8")


def render(
    name: str,
    *,
    title: str | None = None,
    framework_path: str | Path | None = None,
    app_dir: str | Path | None = None,
) -> dict[str, str]:
    """The five files as ``{relative path: text}``. Nothing is written."""
    module = validate_name(name)
    title = _check_title(title) if title is not None else _default_title(name)
    source = None
    if framework_path is not None:
        source = _source_path(Path(framework_path), Path(app_dir) if app_dir is not None else None)
    values = {
        "name": name,
        "module": module,
        "title": title,
        "title_toml": _toml_str(title),
        "title_yaml": _yaml_str(title),
        "mes_version": __version__,
        "uv_sources": _uv_sources(source),
    }
    out: dict[str, str] = {}
    for tmpl, target in FILES:
        text = _template(tmpl)
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        out[target.format(module=module)] = text.rstrip("\n") + "\n"
    return out


# --------------------------------------------------------------------------- creating


def _home_name_in(path_text: str) -> bool:
    try:
        home = Path.home()
    except RuntimeError:
        return False
    parts = Path(path_text).parts
    return bool(home.name) and home.name in parts and str(home) != "/"


def create(
    name: str,
    *,
    directory: str | Path | None = None,
    title: str | None = None,
    framework_path: str | Path | None = None,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Write a new app. ``directory`` is the app's own folder (default ``<cwd>/<name>``)."""
    module = validate_name(name)
    base = Path(cwd) if cwd is not None else Path.cwd()
    folder = (base / (Path(directory).expanduser() if directory is not None else Path(name))).resolve()

    if framework_path is not None:
        framework = check_framework_path(framework_path)
    else:
        framework = default_framework_path()

    if folder.exists() and not folder.is_dir():
        raise Refused(
            f"{folder} already exists and is a file, not a folder, so the app was not created there.",
            hint="Choose another place with --dir, or another name.",
        )
    if framework is not None and (folder == framework or framework in folder.parents):
        raise Refused(
            "The new app would be created inside the my-easy-software folder itself, which would "
            "mix the app's code with the framework's.",
            hint="Create it somewhere else, for example next to it: --dir ../" + name,
        )

    files = render(name, title=title, framework_path=framework, app_dir=folder)
    existing = sorted(rel for rel in files if (folder / rel).exists() or (folder / rel).is_symlink())
    linked = _symlinked_parts(folder, files)
    if linked:
        raise Refused(
            f"{linked} is a link to another place, so files for the app could end up outside its "
            "folder. Nothing was changed.",
            hint="Choose an empty or new folder with --dir, or replace the link with a real folder.",
        )
    if existing:
        raise Refused(
            f"{folder} already has {', '.join(existing)}. Nothing was changed, so no work is lost.",
            hint="Choose an empty or new folder with --dir, or another name.",
        )

    written: list[Path] = []
    created_dirs: list[Path] = []
    try:
        for rel, text in files.items():
            target = folder / rel
            missing = [d for d in (target.parent, *target.parent.parents) if not d.exists()]
            target.parent.mkdir(parents=True, exist_ok=True)
            created_dirs.extend(reversed(missing))
            with target.open("x", encoding="utf-8") as f:  # "x": never overwrite
                f.write(text)
            written.append(target)
    except FileExistsError:
        _undo(written, created_dirs)
        raise Refused(
            f"A file appeared in {folder} while the app was being created, so creation stopped and "
            "what was already written was removed.",
            hint="Run the command again in an empty folder.",
        ) from None
    except OSError as e:
        _undo(written, created_dirs)
        raise MesError(
            f"The app could not be written to {folder}, so nothing was left behind.",
            hint="Check that the folder is on a disk with free space that you are allowed to write to, "
            "or choose another place with --dir.",
            code="write_failed",
            detail=f"{type(e).__name__}: {e}",
        ) from None

    source_line = None
    notes: list[str] = []
    if framework is None:
        notes.append(
            "my-easy-software is not published yet, and this mes is not running from a downloaded "
            "copy of it, so 'uv sync' in the new app will not find it. Run mes new again with "
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
                "the framework is published, then run 'uv sync' again."
            )

    q = shlex.quote(str(folder))
    steps = [
        {
            "step": "Install what the app needs",
            "command": f"cd {q} && uv sync",
            "why": "This prepares a private Python setup inside the app folder. Nothing is installed "
            "for the rest of the computer.",
        },
        {
            "step": "See what the app can do",
            "command": f"cd {q} && uv run {name} --json describe",
            "why": "It lists the app's commands, whether each one only reads or also changes data, "
            "and its jobs.",
        },
        {
            "step": "Save a first note and read it back",
            "command": f'cd {q} && uv run {name} add "My first note" && uv run {name} notes',
            "why": "This checks that the app works and can store data on this computer (never inside "
            "the app folder).",
        },
        {
            "step": "Run the example job",
            "command": f"cd {q} && uv run mes jobs run rebuild",
            "why": "Jobs keep the app's tables up to date on their own. This one counts notes per day.",
        },
        {
            "step": "Make it yours",
            "command": None,
            "why": f"Change the commands in src/{module}/cli.py and keep AGENTS.md up to date, so AI "
            "agents know how to use the app. The dashboard in ui/dashboards/main.yaml cannot be shown "
            "yet, because the dashboard server is not available yet.",
        },
    ]
    return {
        "created": True,
        "app": name,
        "title": title.strip() if title else _default_title(name),
        "module": module,
        "folder": str(folder),
        "files": [
            {"path": rel, "purpose": PURPOSES.get(rel, PURPOSES.get(Path(rel).name, ""))}
            for rel in files
        ],
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


# --------------------------------------------------------------------------- cli


def format_result(result: dict[str, Any]) -> str:
    lines = [f'Created the app "{result["app"]}" in {result["folder"]}', "", "Files:"]
    width = max(len(f["path"]) for f in result["files"])
    lines += [f"  {f['path'].ljust(width)}  {f['purpose']}" for f in result["files"]]
    lines += ["", "Next steps:"]
    for i, s in enumerate(result["next_steps"], 1):
        lines.append(f"  {i}. {s['step']}: {s['why']}")
        if s["command"]:
            lines.append(f"     {s['command']}")
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
    )
    return result if args.json else format_result(result)


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "new",
        help="Create a new app: app.toml, a command-line tool, AGENTS.md, a dashboard and pyproject.toml.",
        description="Create the folder of a new app with five files (and a .gitignore). Existing "
        "files are never overwritten.",
    )
    p.add_argument("name", help="the app's name: 2 to 31 lowercase letters, digits and dashes")
    p.add_argument("--dir", dest="target_dir", metavar="PATH", default=None,
                   help="the folder for the app (default: a new folder named after the app, here)")
    p.add_argument("--title", default=None, help="a short human title (default: from the name)")
    p.add_argument("--framework-path", dest="framework_path", metavar="PATH", default=None,
                   help="the folder of my-easy-software to use, while it is not published "
                   "(default: the copy this mes runs from, when it is one)")
    p.set_defaults(func=_handle)
