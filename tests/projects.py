"""Helpers for tests: write a project folder with in-project apps.

``write(root, project_toml, apps)``  writes ``project.toml`` and ``apps/<package>/app.toml`` (plus an
empty ``__init__.py``) for each ``{package: app_toml}``; returns ``root``.
``load(root_or_tmp, project_toml, apps)``  writes them and returns the composed ``Project``.
``app_toml(name, *, command=None, jobs="", extra="")``  a small app.toml text.
``spec(name, program=None, project="demo")``  an ``AppSpec`` for building projects by hand.
"""

from __future__ import annotations

from pathlib import Path

from mes import manifest
from mes.manifest import AppData, AppSpec, AppUI


def write(root: Path, project_toml: str, apps: dict[str, str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.toml").write_text(project_toml)
    for package, text in (apps or {}).items():
        folder = root / "apps" / package
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "app.toml").write_text(text)
        (folder / "__init__.py").write_text("")
    return root


def load(root: Path, project_toml: str, apps: dict[str, str] | None = None) -> manifest.Project:
    return manifest.load(write(root, project_toml, apps))


def app_toml(name: str, *, command: str | None = None, cli: str | None = None, jobs: str = "",
             extra: str = "") -> str:
    head = f'[app]\nname = "{name}"\n'
    if cli is not None:
        head += f'cli = "{cli}"\n'
    else:
        head += f'command = "{command or name}"\n'
    return head + extra + ("\n" + jobs if jobs else "")


def spec(name: str, program: str | None = None, project: str = "demo", package: str | None = None) -> AppSpec:
    short = manifest.short_name(name, project)
    return AppSpec(package=package or name.replace("-", "_"), name=name, title=name, version="0.0.0", short=short,
                   prefix=manifest.table_prefix(short), program=program or name, cli=None, command=program or name,
                   folder=None, in_project=True, data=AppData(folder=short), ui=AppUI())
