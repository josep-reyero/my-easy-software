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


# --------------------------------------------------------------------------- a compact form for tests

def _toml_value(v) -> str:
    import json as _json

    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return _json.dumps(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(v)


def new_app_name(project: str, old: str) -> str:
    """The app name a test's old ``[apps.<old>]`` becomes: ``<project>-app`` for the project's own
    name, else ``<project>-<old>``."""
    if old == project:
        return f"{project}-app"
    return old if old.startswith(project + "-") else f"{project}-{old}"


def convert(old_toml: str) -> tuple[str, dict[str, str]]:
    """Turn a test's old project.toml (``[apps.X]``, ``[jobs.J] run = "<app> cmd"``, ``[ui] data``)
    into the new ``project.toml`` and ``{package: app.toml}``. Every app's program is its app name."""
    import tomllib

    data = tomllib.loads(old_toml)
    proj = data["project"]
    name = proj["name"]
    old_apps = data.get("apps") or {name: {"cli": name}}
    by_cli = {}
    apps: dict[str, dict] = {}
    for old, conf in old_apps.items():
        app = new_app_name(name, old)
        apps[app] = {"jobs": {}}
        by_cli[conf.get("cli", old)] = app
        by_cli[old] = app
    ids: dict[str, str] = {}
    for job, conf in (data.get("jobs") or {}).items():
        word, _, rest = conf["run"].partition(" ")
        app = by_cli.get(word, next(iter(apps)))
        ids[job] = f"{app}.{job}"
        apps[app]["jobs"][job] = {**conf, "run": rest or word}
    for app in apps.values():
        for conf in app["jobs"].values():
            if "after" in conf and conf["after"] in ids:
                conf["after"] = ids[conf["after"]]
    lines = ["[project]"] + [f"{k} = {_toml_value(v)}" for k, v in proj.items()]
    packages = [a.replace("-", "_") for a in apps]
    lines.append(f"apps = {_toml_value(packages)}")
    ui = dict(data.get("ui") or {})
    ui.pop("data", None)
    if "refresh_after" in ui:
        ui["refresh_after"] = [ids.get(j, j) for j in ui["refresh_after"]]
    ui.pop("tables", None)  # old free table names: the new allowlist comes from [tables] and the apps
    if data.get("ui") is not None:
        lines += ["", "[ui]"] + [f"{k} = {_toml_value(v)}" for k, v in ui.items()]
    if data.get("storage"):
        lines += ["", "[storage]"] + [f"{k} = {_toml_value(v)}" for k, v in data["storage"].items()]
    out: dict[str, str] = {}
    for app, conf in apps.items():
        text = [f'[app]\nname = "{app}"\ncommand = "{app}"']
        for job, jconf in conf["jobs"].items():
            text.append(f"[jobs.{job}]\n" + "\n".join(f"{k} = {_toml_value(v)}" for k, v in jconf.items()))
        out[app.replace("-", "_")] = "\n\n".join(text) + "\n"
    return "\n".join(lines) + "\n", out


def write_old(root: Path, old_toml: str) -> Path:
    project, apps = convert(old_toml)
    return write(root, project, apps)
