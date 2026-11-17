"""The checks behind ``mes setup`` and ``mes doctor``.

Each check looks at this computer and returns one checklist item (see
``mes.protocol.item``). Checks only look: they never install anything, never
use sudo and never change the system. When something is missing, the item's
``fix`` is the command for the agent (or the person) to run after the person
agrees.

Every external command goes through ``Probe.run`` (by default
``run_command``), and every other look at the machine (``which``, ``exists``,
the system name, ``/etc/os-release``, the Python version, the user name) is a
field of ``Probe``, so tests replace them all with fakes.

Public API
----------
``CommandResult(argv, returncode, stdout, stderr)``  with ``.ok``.
``run_command(argv, timeout=10.0) -> CommandResult``
    Runs a program without a shell. A missing program gives returncode 127, a
    timeout gives 124 (with whatever output arrived before it).
``Probe(run, which, exists, system, os_release, python, user)``
    How the checks see the machine. ``Probe()`` uses the real machine.
``Platform``, ``detect_platform(probe) -> Platform``
    ``kind`` is ``"macos"``, ``"linux"`` or ``"other"``; ``distro`` is
    ``"arch"``, ``"debian"``, ``"ubuntu"``, ``"fedora"`` or ``None``.
``TailscaleState``, ``tailscale_state(probe, plat) -> TailscaleState``
    Parses ``tailscale status --json``.
``tailscale_install_command(probe, plat) -> str | None``
    The install command from official package sources (``None`` when only a
    manual download works).
``tailscale_start_command(ts, plat) -> str``, ``tailscale_up_command(plat) -> str``
``uv_install_command(probe, plat) -> str | None``
    Runs ``tailscale up`` with a time limit to get the sign-in link. Only
    ``mes setup`` calls it, never ``mes doctor``.
``MODES``  ``("tailscale", "local-only")``: the network modes kept in the config.
``ConfigState``, ``config_state(path=None)``, ``network_mode(config) -> str | None``
``gather(probe, config_file=None) -> facts``
    ``{"platform", "tailscale", "config", "mode"}``, looked at once.
``build_items(probe, facts, mode) -> list``  the checklist items in order.
``run_checks(probe=None, *, mode=None, config_file=None) -> (items, facts)``
    ``gather`` then ``build_items``.
``DashboardServe(project, socket, port)``, ``dashboard_socket(project) -> str``,
``check_socket_path(path) -> str``, ``serve_commands(serve, ts, plat) -> dict``,
``check_tailscale_serve(plat, ts, mode, serve) -> item``
    The "Serve the dashboard on your private network" item: the exact ``tailscale serve``
    commands for the server (``--bg --https=<port> unix:<socket>``), the host name the dashboard
    server must accept (``--allow-host``), how to check and how to stop. mes never runs them.
"""

from __future__ import annotations

import getpass
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mes import paths
from mes.messages import (
    APPROVE_URL,
    LOCAL_ONLY_NOT_NEEDED,
    LOCAL_ONLY_SKIPPED_WHY,
    T,
)
from mes.protocol import item

MIN_PYTHON = (3, 12)
MODES = ("tailscale", "local-only")
MAC_APP = "/Applications/Tailscale.app"
MAC_APP_CLI = MAC_APP + "/Contents/MacOS/Tailscale"
OS_RELEASE = "/etc/os-release"
_URL_RE = re.compile(r"https://[^\s\"'<>]+")


# --------------------------------------------------------------------------- the runner


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_command(argv: Sequence[str], timeout: float = 10.0) -> CommandResult:
    """Run one program (never through a shell) and capture its output."""
    args = tuple(str(a) for a in argv)
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError:
        return CommandResult(args, 127, "", f"{args[0]}: not found")
    except PermissionError as e:
        return CommandResult(args, 126, "", str(e))
    except subprocess.TimeoutExpired as e:
        return CommandResult(args, 124, _text(e.stdout), _text(e.stderr) or "timed out")
    except OSError as e:
        return CommandResult(args, 126, "", str(e))
    return CommandResult(args, p.returncode, p.stdout or "", p.stderr or "")


def read_os_release(path: str = OS_RELEASE) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _user() -> str | None:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name is fine, the fix uses $USER
        return None


@dataclass
class Probe:
    """How the checks see this computer. Tests pass fakes for every field."""

    run: Callable[..., CommandResult] = run_command
    which: Callable[[str], str | None] = shutil.which
    exists: Callable[[str], bool] = os.path.exists
    system: str = field(default_factory=_platform.system)
    os_release: dict[str, str] = field(default_factory=read_os_release)
    python: tuple[int, ...] = field(default_factory=lambda: tuple(sys.version_info[:3]))
    user: str | None = field(default_factory=_user)


# --------------------------------------------------------------------------- facts


@dataclass(frozen=True)
class Platform:
    kind: str  # "macos" | "linux" | "other"
    name: str
    distro: str | None = None  # "arch" | "debian" | "ubuntu" | "fedora" | None
    codename: str | None = None
    version_id: str | None = None

    @property
    def supported(self) -> bool:
        return self.kind in ("macos", "linux")


def detect_platform(probe: Probe) -> Platform:
    system = (probe.system or "").lower()
    if system == "darwin":
        return Platform("macos", "macOS")
    if system != "linux":
        return Platform("other", probe.system or "unknown")
    rel = probe.os_release or {}
    ident = rel.get("ID", "").lower()
    like = rel.get("ID_LIKE", "").lower().split()
    name = rel.get("PRETTY_NAME") or rel.get("NAME") or "Linux"
    codename = rel.get("VERSION_CODENAME") or None
    distro: str | None = None
    if ident == "arch" or "arch" in like:
        distro = "arch"
    elif ident in ("ubuntu", "debian"):
        distro = ident
    elif "ubuntu" in like and rel.get("UBUNTU_CODENAME"):
        distro, codename = "ubuntu", rel["UBUNTU_CODENAME"]
    elif ident == "fedora":
        distro = "fedora"
    return Platform("linux", name, distro, codename, rel.get("VERSION_ID") or None)


@dataclass(frozen=True)
class TailscaleState:
    installed: bool
    cli: str | None = None
    app: bool = False
    daemon: bool = False  # tailscaled answered
    backend: str | None = None  # BackendState: Running, NeedsLogin, Stopped, ...
    auth_url: str | None = None
    dns_name: str | None = None
    tailnet: str | None = None
    error: str | None = None

    @property
    def connected(self) -> bool:
        return self.backend == "Running"


def _tailscale_cli(probe: Probe, plat: Platform) -> tuple[str | None, bool]:
    app = plat.kind == "macos" and probe.exists(MAC_APP)
    cli = probe.which("tailscale")
    if not cli and app and probe.exists(MAC_APP_CLI):
        cli = MAC_APP_CLI
    return cli, app


def tailscale_state(probe: Probe, plat: Platform) -> TailscaleState:
    cli, app = _tailscale_cli(probe, plat)
    if not cli and not app:
        return TailscaleState(installed=False)
    if not cli:
        return TailscaleState(installed=True, app=True, error="the Tailscale app has no command-line part")
    res = probe.run([cli, "status", "--json"], timeout=10)
    try:
        data = json.loads(res.stdout) if res.stdout.strip() else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or "BackendState" not in data:
        error = (res.stderr or res.stdout).strip() or f"exit code {res.returncode}"
        return TailscaleState(installed=True, cli=cli, app=app, error=error[:300])
    backend = str(data.get("BackendState") or "")
    me = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    net = data.get("CurrentTailnet") if isinstance(data.get("CurrentTailnet"), dict) else {}
    dns = str(me.get("DNSName") or "").rstrip(".") or None
    return TailscaleState(
        installed=True,
        cli=cli,
        app=app,
        daemon=backend not in ("", "NoState"),
        backend=backend or None,
        auth_url=str(data.get("AuthURL") or "") or None,
        dns_name=dns,
        tailnet=str(net.get("Name") or "") or None,
    )


# --------------------------------------------------------------------------- fix commands


def tailscale_install_command(probe: Probe, plat: Platform) -> str | None:
    """Official package sources only. mes never runs these itself."""
    if plat.kind == "macos":
        return "brew install --cask tailscale" if probe.which("brew") else None
    if plat.kind != "linux":
        return None
    enable = "sudo systemctl enable --now tailscaled"
    if plat.distro == "arch":
        return f"sudo pacman -S tailscale && {enable}"
    if plat.distro in ("debian", "ubuntu") and plat.codename:
        base = f"https://pkgs.tailscale.com/stable/{plat.distro}/{plat.codename}"
        return (
            f"curl -fsSL {base}.noarmor.gpg | sudo tee "
            "/usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null && "
            f"curl -fsSL {base}.tailscale-keyring.list | sudo tee "
            "/etc/apt/sources.list.d/tailscale.list && "
            "sudo apt-get update && sudo apt-get install -y tailscale"
        )
    if plat.distro == "fedora":
        repo = "https://pkgs.tailscale.com/stable/fedora/tailscale.repo"
        try:
            dnf5 = int((plat.version_id or "0").split(".")[0]) >= 41
        except ValueError:
            dnf5 = True
        add = (
            f"sudo dnf config-manager addrepo --from-repofile={repo}"
            if dnf5
            else f"sudo dnf config-manager --add-repo {repo}"
        )
        return f"{add} && sudo dnf install -y tailscale && {enable}"
    # Any other Linux: no command. Piping a script from the internet into a shell is never
    # suggested; the person follows the official instructions for their system instead.
    return None


def tailscale_start_command(ts: TailscaleState, plat: Platform) -> str:
    if plat.kind == "macos":
        return "open -a Tailscale" if ts.app else "sudo tailscaled install-system-daemon"
    return "sudo systemctl enable --now tailscaled"


def tailscale_up_command(plat: Platform) -> str:
    return "tailscale up" if plat.kind == "macos" else "sudo tailscale up"


UV_DOCS = "https://docs.astral.sh/uv/getting-started/installation/"
TAILSCALE_LINUX_DOCS = "https://tailscale.com/download/linux"


def uv_install_command(probe: Probe, plat: Platform) -> str | None:
    """A package manager's command, or None (then the person follows uv's own instructions)."""
    if plat.kind == "macos" and probe.which("brew"):
        return "brew install uv"
    if plat.distro == "arch":
        return "sudo pacman -S uv"
    if plat.distro == "fedora":
        return "sudo dnf install -y uv"
    return None


# --------------------------------------------------------------------------- checks


def _t(check: str, key: str) -> str:
    return T[check][key]


def check_platform(plat: Platform) -> dict[str, Any]:
    if plat.kind == "macos":
        return item("platform", _t("platform", "title"), "done", _t("platform", "why_mac"), detail="macOS")
    if plat.kind == "linux":
        return item("platform", _t("platform", "title"), "done", _t("platform", "why_done"), detail=plat.name)
    return item("platform", _t("platform", "title"), "blocked", _t("platform", "why_other"), detail=plat.name)


def check_python(probe: Probe) -> dict[str, Any]:
    version = ".".join(str(p) for p in probe.python)
    if tuple(probe.python[:2]) >= MIN_PYTHON:
        return item("python", _t("python", "title"), "done", _t("python", "why_done"), detail=f"Python {version}")
    need = ".".join(str(p) for p in MIN_PYTHON)
    return item(
        "python",
        _t("python", "title"),
        "todo",
        _t("python", "why_todo"),
        fix="uv python install 3.13",
        detail=f"Python {version} found; {need} or newer is needed",
    )


def check_uv(probe: Probe, plat: Platform) -> dict[str, Any]:
    title = _t("uv", "title")
    path = probe.which("uv")
    fix = uv_install_command(probe, plat)
    if not path:
        if fix is None:
            return item("uv", title, "todo", _t("uv", "why_manual"), needs_human=True, detail=UV_DOCS)
        return item("uv", title, "todo", _t("uv", "why_todo"), fix=fix)
    res = probe.run([path, "--version"], timeout=10)
    m = re.search(r"uv\s+(\d+(?:\.\d+)+)", res.stdout)
    if not res.ok or not m:
        return item(
            "uv", title, "todo", _t("uv", "why_broken") + ("" if fix else " " + _t("uv", "why_manual")),
            fix=fix, needs_human=fix is None,
            detail=(res.stderr or res.stdout).strip()[:300] or None,
        )
    return item("uv", title, "done", _t("uv", "why_done"), detail=f"uv {m.group(1)}")


def check_systemd(probe: Probe, plat: Platform) -> list[dict[str, Any]]:
    """On a Linux server, jobs run as system units of the project's own account (made by the root
    script of ``mes server plan``), so only the system's own systemd matters here."""
    title = _t("systemd", "title")
    if plat.kind == "macos":
        return [item("systemd", title, "skipped", _t("systemd", "why_mac"))]
    if plat.kind != "linux":
        return [item("systemd", title, "skipped", _t("tailscale-installed", "why_unsupported"))]
    if not probe.which("systemctl"):
        return [item("systemd", title, "blocked", _t("systemd", "why_missing"))]
    res = probe.run(["systemctl", "is-system-running"], timeout=10)
    state = res.stdout.strip().splitlines()[0] if res.stdout.strip() else ""
    if state in ("running", "degraded", "starting", "initializing"):
        detail = "systemd: " + state
        if state == "degraded":
            detail += " (some other services failed, which does not stop mes jobs)"
        return [item("systemd", title, "done", _t("systemd", "why_done"), detail=detail)]
    return [item("systemd", title, "blocked", _t("systemd", "why_not_running"),
                 detail=(res.stderr or res.stdout).strip()[:300] or None)]


def check_tailscale(
    probe: Probe, plat: Platform, ts: TailscaleState, mode: str | None
) -> list[dict[str, Any]]:
    ids = ("tailscale-installed", "tailscale-running", "tailscale-signed-in")
    titles = [_t(i, "title") for i in ids]
    if not plat.supported:
        why = _t("tailscale-installed", "why_unsupported")
        return [item(i, t, "skipped", why) for i, t in zip(ids, titles)]

    # installed
    if ts.installed:
        where = ts.cli or MAC_APP
        installed = item(ids[0], titles[0], "done", _t(ids[0], "why_done"), detail=f"found: {where}")
    else:
        fix = tailscale_install_command(probe, plat)
        if fix is None and plat.kind == "macos":
            installed = item(ids[0], titles[0], "todo", _t(ids[0], "why_manual_mac"), needs_human=True,
                             detail="https://tailscale.com/download/mac")
        elif fix is None:
            installed = item(ids[0], titles[0], "todo", _t(ids[0], "why_manual_linux"), needs_human=True,
                             detail=TAILSCALE_LINUX_DOCS)
        else:
            installed = item(ids[0], titles[0], "todo", _t(ids[0], "why_todo"), fix=fix)

    # running
    if not ts.installed:
        running = item(ids[1], titles[1], "blocked", _t(ids[1], "why_blocked"))
    elif ts.backend == "Running" or ts.backend in ("NeedsLogin", "NeedsMachineAuth"):
        running = item(ids[1], titles[1], "done", _t(ids[1], "why_done"))
    elif ts.backend == "Stopped":
        running = item(ids[1], titles[1], "todo", _t(ids[1], "why_stopped"), fix=tailscale_up_command(plat))
    elif ts.backend == "Starting":
        running = item(ids[1], titles[1], "todo", _t(ids[1], "why_starting"))
    else:
        why = _t(ids[1], "why_todo_mac" if plat.kind == "macos" else "why_todo")
        running = item(ids[1], titles[1], "todo", why, fix=tailscale_start_command(ts, plat), detail=ts.error)

    # signed in
    if ts.backend in ("Running", "Stopped"):
        # The full MagicDNS name ends with the network's own name, which can be the owner's chosen
        # name or come from their account, so only the device's own part (the first label) is shown.
        device = ts.dns_name.split(".", 1)[0] if ts.dns_name else None
        detail = f"this computer's private name: {device}" if device else None
        signed = item(ids[2], titles[2], "done", _t(ids[2], "why_done"), detail=detail)
    elif ts.backend == "NeedsLogin":
        signed = item(ids[2], titles[2], "todo", _t(ids[2], "why_todo"), needs_human=True, detail=ts.auth_url)
    elif ts.backend == "NeedsMachineAuth":
        signed = item(ids[2], titles[2], "todo", _t(ids[2], "why_approve"), needs_human=True, detail=APPROVE_URL)
    else:
        signed = item(ids[2], titles[2], "blocked", _t(ids[2], "why_blocked"))

    items = [installed, running, signed]
    if mode == "local-only":
        first = True
        for n, it in enumerate(items):
            if it["status"] != "done":
                why = LOCAL_ONLY_SKIPPED_WHY if first else LOCAL_ONLY_NOT_NEEDED
                first = False
                items[n] = item(it["id"], it["title"], "skipped", why, detail="local-only mode")
    return items


# --------------------------------------------------------------------------- serving a dashboard

DASHBOARD_SOCKET_DIR = "/run/mes"
DEFAULT_SERVE_PORT = 443
PLACEHOLDER_HOST = "<this-server>.<your-tailnet>.ts.net"
_SOCKET_PATH_RE = re.compile(r"^(?:/[A-Za-z0-9._-]+)+\Z")
_DNS_NAME_RE = re.compile(r"^(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")


@dataclass(frozen=True)
class DashboardServe:
    """Which project's dashboard to serve, from which local socket, on which HTTPS port."""

    project: str
    socket: str
    port: int = DEFAULT_SERVE_PORT


def dashboard_socket(project: str) -> str:
    """Where the dashboard server (easy-ui, units mes-<project>-web.socket/.service) of a project
    listens on a server by default."""
    return f"{DASHBOARD_SOCKET_DIR}/{paths.check_name(project)}-web.sock"


def check_socket_path(path: str) -> str:
    """A plain absolute path (letters, digits, ``._-`` and ``/``; no ``..``), or ``ValueError``.

    The path ends up in a command the person pastes into a root shell, so it is kept boring."""
    if (not isinstance(path, str) or len(path) > 200 or not _SOCKET_PATH_RE.match(path)
            or any(part in (".", "..") for part in path.split("/"))):
        raise ValueError("the socket must be a plain absolute path such as /run/mes/notes-web.sock")
    return path


def _server_host(ts: TailscaleState) -> tuple[str, bool]:
    name = (ts.dns_name or "").lower()
    if name and _DNS_NAME_RE.match(name):
        return name, True
    return PLACEHOLDER_HOST, False


def serve_commands(serve: DashboardServe, ts: TailscaleState, plat: Platform) -> dict[str, Any]:
    """The commands for the person to run on the server; mes never runs any of them."""
    sudo = "" if plat.kind == "macos" else "sudo "
    socket = check_socket_path(serve.socket)
    port = int(serve.port)
    host, known = _server_host(ts)
    https = f"--https={port}"
    url = f"https://{host}" + ("" if port == 443 else f":{port}") + "/"
    return {
        "serve": f"{sudo}tailscale serve --bg {https} unix:{socket}",
        "status": "tailscale serve status",
        "stop": f"{sudo}tailscale serve {https} off",
        "allow_host": f"--allow-host {host}" if port == 443 else f"--origin https://{host}:{port}",
        "plan": f"/usr/local/bin/mes server plan {serve.project} "
        + (f"--allow-host {host}" if port == 443 else f"--origin https://{host}:{port}"),
        "host": host,
        "host_known": known,
        "url": url,
    }


def check_tailscale_serve(plat: Platform, ts: TailscaleState, mode: str | None,
                          serve: DashboardServe) -> dict[str, Any]:
    texts = T["tailscale-serve"]
    title = texts["title"]
    if not plat.supported:
        return item("tailscale-serve", title, "skipped", texts["why_unsupported"])
    if mode == "local-only":
        return item("tailscale-serve", title, "skipped", texts["why_local_only"], detail="local-only mode")
    if not (ts.installed and ts.backend == "Running"):
        return item("tailscale-serve", title, "blocked", texts["why_blocked"])
    cmd = serve_commands(serve, ts, plat)
    if cmd["host_known"]:
        host_note = f"The dashboard then opens at {cmd['url']} on your own devices."
    else:
        host_note = (f"Replace {PLACEHOLDER_HOST} with the server's private name (shown by `tailscale status` "
                     f"on the server). The dashboard then opens at {cmd['url']} on your own devices.")
    why = " ".join([
        texts["why_todo"].format(project=f'"{serve.project}"'),
        f"First look at what the server already serves with `{cmd['status']}`: serving on port {serve.port} "
        "replaces whatever is served there now; if that port is taken, use another one (--port).",
        f"Then run this once on the server: `{cmd['serve']}`.",
        f"The dashboard server only answers to names it knows, so make its units (mes-{serve.project}-web) "
        f"with that private name: `{cmd['plan']}` (it passes `{cmd['allow_host']}` to `easy-ui units`), "
        "then run the script it prints as before.",
        host_note,
        f"Check what is served with `{cmd['status']}`; stop serving it with `{cmd['stop']}`.",
        texts["never_funnel"],
    ])
    detail = "\n".join([
        f"serve: {cmd['serve']}",
        f"dashboard units: {cmd['plan']}",
        f"check: {cmd['status']}",
        f"stop: {cmd['stop']}",
        f"opens at: {cmd['url']}",
    ])
    return item("tailscale-serve", title, "todo", why, needs_human=True, detail=detail)


@dataclass(frozen=True)
class ConfigState:
    path: Path
    exists: bool
    data: dict[str, Any] | None  # None when the file is damaged
    error: str | None = None


def config_state(path: Path | None = None) -> ConfigState:
    p = Path(path) if path else paths.config_path()
    if not p.is_file():
        return ConfigState(p, False, {})
    try:
        return ConfigState(p, True, tomllib.loads(p.read_text(encoding="utf-8")))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as e:
        return ConfigState(p, True, None, str(e))


def network_mode(config: dict[str, Any] | None) -> str | None:
    net = (config or {}).get("network")
    mode = net.get("mode") if isinstance(net, dict) else None
    return mode if mode in MODES else None


def check_config(cfg: ConfigState) -> dict[str, Any]:
    title = _t("config", "title")
    if not cfg.exists:
        return item("config", title, "todo", _t("config", "why_todo"), fix="mes setup", detail=str(cfg.path))
    if cfg.data is None:
        return item("config", title, "todo", _t("config", "why_broken"), detail=f"{cfg.path}: {cfg.error}")
    mode = network_mode(cfg.data)
    detail = f"{cfg.path} (network: {mode})" if mode else str(cfg.path)
    return item("config", title, "done", _t("config", "why_done"), detail=detail)


def gather(probe: Probe, config_file: Path | None = None) -> dict[str, Any]:
    """Look at the machine once: ``{"platform", "tailscale", "config", "mode"}``."""
    cfg = config_state(config_file)
    plat = detect_platform(probe)
    ts = tailscale_state(probe, plat) if plat.supported else TailscaleState(installed=False)
    return {"platform": plat, "tailscale": ts, "config": cfg, "mode": network_mode(cfg.data)}


def build_items(probe: Probe, facts: dict[str, Any], mode: str | None,
                serve: DashboardServe | None = None) -> list[dict[str, Any]]:
    """The checklist items, in order, from facts already gathered. With ``serve``, the last item
    is how to serve that project's dashboard on the private network."""
    plat: Platform = facts["platform"]
    items = [check_platform(plat), check_python(probe), check_uv(probe, plat)]
    items += check_systemd(probe, plat)
    items += check_tailscale(probe, plat, facts["tailscale"], mode)
    items.append(check_config(facts["config"]))
    if serve is not None:
        items.append(check_tailscale_serve(plat, facts["tailscale"], mode, serve))
    return items


def run_checks(
    probe: Probe | None = None,
    *,
    mode: str | None = None,
    config_file: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run every check. ``mode`` overrides the network mode read from the config."""
    probe = probe or Probe()
    facts = gather(probe, config_file)
    mode = mode or facts["mode"]
    return build_items(probe, facts, mode), facts
