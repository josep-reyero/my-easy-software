"""Per-machine folders, the per-machine config file, and the server layout.

Project data never lives inside the project's repository.

On a personal machine (no accounts involved):

- data:   ``$MES_DATA_HOME/<project>/``  (default ``$XDG_DATA_HOME/mes`` or ``~/.local/share/mes``)
- state:  ``$MES_STATE_HOME/<project>/`` (default ``$XDG_STATE_HOME/mes`` or ``~/.local/state/mes``)
- config: ``$MES_CONFIG``                (default ``$XDG_CONFIG_HOME/mes/config.toml`` or
  ``~/.config/mes/config.toml``)

A project may keep its data elsewhere on one machine with ``[projects.<name>] data_dir = "..."``
in the config file (``state_dir`` works the same way). my-easy-ui honours the same setting.

On a server, everything for a project is under ``/var/lib/mes/<project>/`` (see
``ServerLayout``). The system units and the gate tell the project's programs where their folders
are with ``MES_PROJECT``, ``MES_DATA_DIR`` and ``MES_STATE_DIR``; those win over everything
else, and only for the project named in ``MES_PROJECT``.

Public API
----------
``data_home() -> Path``, ``state_home() -> Path``, ``config_path() -> Path``
``check_name(project) -> str``  raises ``UsageError`` for a name that is not a project name.
``project_data_dir(project, create=False) -> Path``, ``project_state_dir(project, create=False) -> Path``,
``project_store_dir(project, create=False) -> Path`` (the my-bucket-storage store)
    ``create=True`` makes the folder (mode 0700).
``load_config(path=None) -> dict``  ``{}`` when missing; ``ConfigError`` when not valid TOML.
``project_config(project) -> dict``  the ``[projects.<project>]`` table, ``{}`` when absent.
``project_root(start=None) -> Path | None``  nearest folder holding ``project.toml`` or
    ``pyproject.toml``.
``is_inside(path, folder) -> bool``
``SERVER_ROOT`` (``/var/lib/mes``), ``RELEASE_PROJECT_DIR``, ``ServerLayout``,
``server_layout(project, root=SERVER_ROOT)``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mes.errors import ConfigError, UsageError
from mes.manifest import MANIFEST_FILE, PROJECT_NAME_RE

SERVER_ROOT = Path("/var/lib/mes")
RELEASE_PROJECT_DIR = "project"  # inside each release: the project's own files


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


def check_name(project: Any) -> str:
    if not isinstance(project, str) or not PROJECT_NAME_RE.match(project):
        raise UsageError(
            f"{str(project)[:40]!r} is not a valid project name, so mes will not use it as a folder name.",
            hint="Project names use 2 to 31 lowercase letters, digits and dashes, starting with a letter.",
        )
    return project


def _session_dir(project: str, env_name: str) -> Path | None:
    """The folder the system unit or the gate gave this session, for its own project only."""
    session_project = os.environ.get("MES_PROJECT")
    if session_project is None:
        return None
    if session_project != project:
        raise UsageError(
            f'This session belongs to the project "{session_project[:40]}", so it cannot use the folders of '
            f'"{project}". Projects cannot read each other.',
            hint="Run the command in the project's own session or account.",
            code="wrong_project",
        )
    return _env_path(env_name)


def _configured_dir(project: str, key: str) -> Path | None:
    value = project_config(project).get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ConfigError(
            f"In the settings file for this computer, [projects.{project}] {key} must be the full path of a folder.",
            hint=f'Write for example {key} = "/srv/archive/{project}", or remove the line.',
        )
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise ConfigError(
            f"In the settings file for this computer, [projects.{project}] {key} = {value!r} is not a full path, "
            "so mes cannot be sure which folder is meant.",
            hint=f'Write the full path, for example {key} = "/srv/archive/{project}".',
        )
    return p


def _project_dir(project: str, key: str, env_name: str, base: Path, create: bool) -> Path:
    check_name(project)
    d = _session_dir(project, env_name) or _configured_dir(project, key) or base / project
    if create:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def project_data_dir(project: str, create: bool = False) -> Path:
    return _project_dir(project, "data_dir", "MES_DATA_DIR", data_home(), create)


def project_state_dir(project: str, create: bool = False) -> Path:
    return _project_dir(project, "state_dir", "MES_STATE_DIR", state_home(), create)


def project_store_dir(project: str, create: bool = False) -> Path:
    """The project's my-bucket-storage store: ``MES_STORE_DIR`` in a server session, else
    ``[projects.<name>] store_dir``, else ``<data folder>/store``."""
    check_name(project)
    d = _session_dir(project, "MES_STORE_DIR") or _configured_dir(project, "store_dir") \
        or project_data_dir(project) / "store"
    if create:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


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


def project_config(project: str) -> dict[str, Any]:
    projects = load_config().get("projects", {})
    section = projects.get(project, {}) if isinstance(projects, dict) else {}
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


# --------------------------------------------------------------------------- server layout


@dataclass(frozen=True)
class ServerLayout:
    """Where everything of one project lives on a server.

    ``base`` (``/var/lib/mes/<project>``) belongs to root and can only be entered, not listed.
    ``code``, ``data``, ``state`` and ``store`` belong to ``mes-<project>`` and nobody else can
    open them. ``code`` also holds the deploy tools' own folders (``uv-cache``, ``uv-python``), so
    jobs and the write gate, which see ``code`` read-only, cannot change what the next deploy runs.
    ``ui`` belongs to ``mes-<project>`` and is readable by ``mes-<project>-web``; ``auth``
    belongs to ``mes-<project>-web`` alone (the dashboard sign-in: password hash, passkeys,
    sessions), so not even the project's own code can read it. ``gate`` (the gate's log, locks and
    deploy records) and ``webhook`` (the webhook receiver's replay record and trigger notes) belong
    to ``mes-<project>`` but no job or sandboxed request has them writable. ``etc``, ``home`` and
    ``log`` belong to root (``etc`` and ``home`` with the group ``mes-<project>``, mode 0750, so
    only root and the project's account can look inside): ``etc`` holds the signer list, the
    gate's login shell, the paths of the tools and the webhook job list;
    ``home/.ssh/authorized_keys`` the keys that may connect; ``log`` the tamper-evident run log
    that only root writes.
    """

    project: str
    root: Path

    @property
    def base(self) -> Path:
        return self.root / self.project

    code = property(lambda self: self.base / "code")
    releases = property(lambda self: self.code / "releases")
    current = property(lambda self: self.code / "current")
    incoming = property(lambda self: self.code / "incoming")
    uv_cache = property(lambda self: self.code / "uv-cache")
    uv_python = property(lambda self: self.code / "uv-python")
    data = property(lambda self: self.base / "data")
    state = property(lambda self: self.base / "state")
    ui = property(lambda self: self.base / "ui")
    auth = property(lambda self: self.base / "auth")  # the dashboard sign-in, only for mes-<p>-web
    web = auth
    store = property(lambda self: self.base / "store")
    gate = property(lambda self: self.base / "gate")  # gate log, locks, deploy records: no job writes here
    webhook = property(lambda self: self.base / "webhook")
    etc = property(lambda self: self.base / "etc")
    home = property(lambda self: self.base / "home")
    log = property(lambda self: self.base / "log")
    ssh_dir = property(lambda self: self.home / ".ssh")
    authorized_keys = property(lambda self: self.ssh_dir / "authorized_keys")
    allowed_signers = property(lambda self: self.etc / "allowed_signers")
    server_toml = property(lambda self: self.etc / "server.toml")
    gate_shell = property(lambda self: self.etc / "gate-shell")
    webhook_key = property(lambda self: self.etc / "webhook.key")
    webhook_jobs = property(lambda self: self.etc / "webhook-jobs")
    run_log = property(lambda self: self.log / "runs.jsonl")
    triggers = property(lambda self: self.webhook / "triggers")
    release_records = property(lambda self: self.gate / "releases")

    @property
    def current_project(self) -> Path:
        return self.current / RELEASE_PROJECT_DIR

    @property
    def account(self) -> str:
        return f"mes-{self.project}"

    @property
    def web_account(self) -> str:
        return f"mes-{self.project}-web"


def server_layout(project: str, root: str | Path = SERVER_ROOT) -> ServerLayout:
    return ServerLayout(check_name(project), Path(root))
