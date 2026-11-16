"""Per-machine folders and the per-machine config file.

App data never lives inside the app's repository. Each machine keeps it here:

- data:   ``$MES_DATA_HOME/<app>/``  (default ``$XDG_DATA_HOME/mes`` or ``~/.local/share/mes``)
- state:  ``$MES_STATE_HOME/<app>/`` (default ``$XDG_STATE_HOME/mes`` or ``~/.local/state/mes``)
- config: ``$MES_CONFIG``            (default ``$XDG_CONFIG_HOME/mes/config.toml`` or
  ``~/.config/mes/config.toml``)

``MES_DATA_HOME`` / ``MES_STATE_HOME`` replace the ``mes`` base folder (the app
name is still appended). ``MES_CONFIG`` is the path of the config file itself.

Public API
----------
``data_home() -> Path``, ``state_home() -> Path``
    Base folders (not created).
``config_path() -> Path``
    The config file path (may not exist).
``app_data_dir(app, create=False) -> Path``, ``app_state_dir(app, create=False) -> Path``
    ``<base>/<app>``; the app name is validated so it cannot escape the base.
    ``create=True`` makes the folder (mode 0700).
``load_config(path=None) -> dict``
    The parsed config file, ``{}`` when it does not exist. Raises ``ConfigError``
    when it is not valid TOML.
``app_config(app) -> dict``
    The ``[apps.<app>]`` table of the config file, ``{}`` when absent.
``project_root(start=None) -> Path | None``
    Nearest folder at or above ``start`` (default: current directory) holding
    ``app.toml`` or ``pyproject.toml``.
``is_inside(path, folder) -> bool``
    True when ``path`` is ``folder`` or inside it (after resolving).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from mes.errors import ConfigError, UsageError
from mes.manifest import APP_NAME_RE, MANIFEST_FILE


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def _xdg(name: str, default: str) -> Path:
    return _env_path(name) or Path.home() / default


def data_home() -> Path:
    return _env_path("MES_DATA_HOME") or _xdg("XDG_DATA_HOME", ".local/share") / "mes"


def state_home() -> Path:
    return _env_path("MES_STATE_HOME") or _xdg("XDG_STATE_HOME", ".local/state") / "mes"


def config_path() -> Path:
    return _env_path("MES_CONFIG") or _xdg("XDG_CONFIG_HOME", ".config") / "mes" / "config.toml"


def _app_dir(base: Path, app: str, create: bool) -> Path:
    if not isinstance(app, str) or not APP_NAME_RE.match(app):
        raise UsageError(
            f"{app!r} is not a valid app name, so mes will not use it as a folder name.",
            hint="App names use 2 to 31 lowercase letters, digits and dashes, starting with a letter.",
        )
    d = base / app
    if create:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def app_data_dir(app: str, create: bool = False) -> Path:
    return _app_dir(data_home(), app, create)


def app_state_dir(app: str, create: bool = False) -> Path:
    return _app_dir(state_home(), app, create)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else config_path()
    if not p.is_file():
        return {}
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        from mes.manifest import _toml_where

        raise ConfigError(
            f"The settings file for this computer ({p}) has a typing mistake{_toml_where(e)}, "
            "so mes cannot read its settings.",
            hint="Your assistant can fix that line, or move the file away and run mes setup to start "
            "again with default settings.",
            detail=str(e),
        ) from None


def app_config(app: str) -> dict[str, Any]:
    apps = load_config().get("apps", {})
    section = apps.get(app, {}) if isinstance(apps, dict) else {}
    return section if isinstance(section, dict) else {}


def project_root(start: str | Path | None = None) -> Path | None:
    cur = Path(start or Path.cwd()).resolve()
    if cur.is_file():
        cur = cur.parent
    for d in (cur, *cur.parents):
        if (d / MANIFEST_FILE).is_file() or (d / "pyproject.toml").is_file():
            return d
    return None


def is_inside(path: str | Path, folder: str | Path) -> bool:
    p, f = Path(path).resolve(), Path(folder).resolve()
    return p == f or f in p.parents
