"""``mes server plan|check|remove <project>``: set a project up on a Linux server, safely.

mes never runs sudo and never installs system software. ``plan`` renders **one readable root
script** that a person reads and runs once per project with ``sudo bash <file>``:

- two accounts without a password and without sudo: ``mes-<project>`` (owns code, data and
  state; runs the jobs and the SSH gate) and ``mes-<project>-web`` (runs only the dashboard
  server; can read ``ui/`` and nothing else of the project; no network of its own);
- ``/var/lib/mes/<project>/`` and its folders, each with owner-only permissions;
- the SSH keys that may connect, each forced into ``mes gate --project <p> --allow <level>``
  with ``restrict`` (no shell, pty, forwarding or agent), and a login shell that runs nothing
  but the gate;
- the keys allowed to sign deploys (``etc/allowed_signers``, Ed25519 only);
- the project's system units (jobs, timers, the network helper of jobs that list hosts, the
  dashboard data refresh, the gate's per-request sandboxes, webhooks) plus the dashboard
  server's socket and service, rendered by ``easy-ui units`` when easy-ui is installed.

The script is idempotent (running it again brings the server back to the plan), uses
``set -euo pipefail``, and touches nothing outside ``/var/lib/mes/<project>``, the two accounts
and the project's own unit files. Every value written into it is validated first (names
against strict patterns, paths against a safe character set, keys by their exact format) and
single-quoted as well.

``check`` verifies the result as a checklist without root. ``remove`` renders the matching
removal script (data is kept unless ``--delete-data``).

Public API
----------
``MAX_SERVER_NAME`` (24: ``mes-<name>-web`` must fit the 32-character account limit),
``DEFAULT_TOOLS``, ``UNITS_DIR``.
``check_server_name(name) -> str``
``parse_public_key(text, *, what) -> (kind, base64)``
``read_key_file(path, *, what) -> (kind, base64)``
``PlanInput``  everything a plan needs (project, layout, tools, keys, signers, web units).
``build_plan(...) -> PlanInput``; ``render_script(plan) -> str``; ``render_remove(...) -> str``
``gate_command(plan, level) -> str``  the forced command of one key level.
``per_app(project) -> list[dict]``  each app's jobs with their network hosts, memory and time limits.
``run_check(project_name, *, server_root, units_dir, project_dir, probe) -> checklist``
``register(subparsers)``
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import grp
import hashlib
import os
import pwd
import re
import secrets
import shlex
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mes import __version__, paths, protocol, siblings, units
from mes import manifest as manifest_mod
from mes.app import check_allowed
from mes.errors import MesError, NotFound, Refused, UsageError

MAX_SERVER_NAME = 24
UNITS_DIR = "/etc/systemd/system"
DEFAULT_TOOLS = {"mes": "/usr/local/bin/mes", "uv": "/usr/local/bin/uv", "easy_ui": "/usr/local/bin/easy-ui"}
KEY_LEVELS = ("read", "write", "deploy")
SIGNER_TYPES = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com")
LOGIN_KEY_TYPES = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com")
_B64_RE = re.compile(r"^[A-Za-z0-9+/]{60,1000}={0,3}\Z")
_HOST_RE = re.compile(r"^(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z")
MAX_WEB_UNIT_BYTES = 64 * 1024


def check_server_name(name: Any) -> str:
    paths.check_name(name)
    if len(name) > MAX_SERVER_NAME:
        raise UsageError(
            f'The project name "{name}" is {len(name)} characters long, but on a server it may have at most '
            f"{MAX_SERVER_NAME}: the account mes-{name}-web must fit the 32-character limit of Linux account names.",
            hint="Choose a shorter project name (in project.toml) for projects that run on a server.",
            code="name_too_long",
        )
    if name.endswith("-web"):
        raise UsageError(
            f'The project name "{name}" ends with "-web", which a server cannot use: the dashboard account of a '
            f'project called "{name[:-4]}" is already named mes-{name}.',
            hint="Choose a project name that does not end with -web.",
            code="name_reserved",
        )
    return name


def web_unit_names(project: str) -> tuple[str, str]:
    """The names easy-ui gives the dashboard server's units (``easy-ui units``)."""
    return f"mes-{project}-web.socket", f"mes-{project}-web.service"


# --------------------------------------------------------------------------- keys


def parse_public_key(text: Any, *, what: str, allowed: tuple[str, ...] = LOGIN_KEY_TYPES) -> tuple[str, str]:
    bad = Refused(
        f"{what} is not an OpenSSH public key mes accepts, so it was not used.",
        hint="Give the .pub file of an Ed25519 key (it starts with ssh-ed25519 or sk-ssh-ed25519@openssh.com). "
        "Create one with: ssh-keygen -t ed25519",
        code="bad_key",
    )
    if not isinstance(text, str) or "PRIVATE KEY" in text:
        if isinstance(text, str):
            raise Refused(f"{what} is a private key. Only the public half (the .pub file) is ever given to a server.",
                          hint="Give the matching .pub file instead, and keep the private key to yourself.",
                          code="private_key")
        raise bad
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if len(lines) != 1:
        raise bad
    words = lines[0].split()
    if len(words) < 2 or words[0] not in allowed or not _B64_RE.match(words[1]):
        raise bad
    try:
        blob = base64.b64decode(words[1], validate=True)
    except ValueError:
        raise bad from None
    n = int.from_bytes(blob[:4], "big") if len(blob) >= 4 else 0
    if blob[4:4 + n].decode("ascii", "replace") != words[0]:
        raise bad
    return words[0], words[1]


def read_key_file(path: str | os.PathLike[str], *, what: str, allowed: tuple[str, ...] = LOGIN_KEY_TYPES) -> tuple[str, str]:
    p = Path(path).expanduser()
    try:
        st = p.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > 16 * 1024:
            raise OSError
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise NotFound(f"{what} ({p}) could not be read as a small text file.",
                       hint="Give the path of the key's .pub file.", code="key_missing") from None
    return parse_public_key(text, what=f"{what} ({p.name})", allowed=allowed)


# --------------------------------------------------------------------------- the plan


@dataclass
class PlanInput:
    project: manifest_mod.Project
    layout: paths.ServerLayout
    tools: dict[str, str | None]
    keys: list[tuple[str, str, str]] = field(default_factory=list)      # (level, kind, base64)
    signers: list[tuple[str, str]] = field(default_factory=list)        # (kind, base64)
    units: dict[str, str] = field(default_factory=dict)
    web_units: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    units_dir: str = UNITS_DIR
    fingerprint: str = ""


def gate_command(plan: PlanInput, level: str) -> str:
    if level not in KEY_LEVELS:
        raise ValueError(level)
    return (f"{plan.tools['mes']} gate --project {plan.project.name} --allow {level} "
            f"--server-root {units.sandbox_path(plan.layout.root)}")


def _web_units(project: manifest_mod.Project, project_dir: Path, layout: paths.ServerLayout,
               easy_ui: str, allow_hosts: list[str] = ()) -> tuple[dict[str, str], list[str]]:
    """Ask easy-ui for the dashboard server's units, and check every line that matters."""
    if siblings.tool_path("easy-ui") is None:
        return {}, ["The dashboard server's units are not in this plan, because easy-ui (my-easy-ui) is not "
                    "installed on this computer. Install it here and run 'mes server plan' again to add them."]
    argv = siblings.easy_ui_argv(project_dir, "units", "--render-only", "--exec", easy_ui,
                                 "--root", units.sandbox_path(layout.root))
    for host in allow_hosts:
        argv += ["--allow-host", host]
    try:
        answer = siblings.run_json(argv, what="Rendering the dashboard server's units (easy-ui units)", timeout=60)
    except MesError as e:
        return {}, ["The dashboard server's units are not in this plan, because easy-ui could not render them: "
                    f"{(e.detail or e.message)[:500]}"]
    found = answer.get("units")
    if not isinstance(found, dict) or not found:
        return {}, ["The dashboard server's units are not in this plan: easy-ui gave no units."]
    out: dict[str, str] = {}
    for name, text in found.items():
        problem = None
        if name not in web_unit_names(project.name):
            problem = f"a unit name mes does not accept ({str(name)[:80]!r})"
        elif not isinstance(text, str) or "\x00" in text or len(text.encode("utf-8")) > MAX_WEB_UNIT_BYTES:
            problem = f"{name} is not plain text of a sensible size"
        elif name.endswith(".socket"):
            # Commands in a socket unit run as root, and Service= could start another unit.
            if re.search(r"^[ \t]*(?:Exec\w*|Service|User|Group|SupplementaryGroups)[ \t]*=", text, re.MULTILINE):
                problem = f"{name} runs commands or names another service"
        elif name.endswith(".service"):
            users = re.findall(r"^[ \t]*User[ \t]*=[ \t]*(.*?)[ \t]*$", text, re.MULTILINE)
            groups = re.findall(r"^[ \t]*Group[ \t]*=[ \t]*(.*?)[ \t]*$", text, re.MULTILINE)
            if users != [layout.web_account] or any(g != layout.web_account for g in groups):
                problem = f"{name} does not run as {layout.web_account}"
            elif re.search(r"^[ \t]*(?:PermissionsStartOnly|SupplementaryGroups|DynamicUser|PAMName"
                           r"|AmbientCapabilities[ \t]*=[ \t]*\S)", text, re.MULTILINE) or \
                    re.search(r"^[ \t]*Exec\w*[ \t]*=[ \t]*[-@:|]*[+!]", text, re.MULTILINE):
                problem = f"{name} asks for privileges"
        if problem:
            raise Refused(
                f"easy-ui rendered dashboard units that mes will not put into a root script: {problem}.",
                hint="Update easy-ui, or report this to its maintainers. Nothing was written.",
                code="unsafe_web_unit",
            )
        out[name] = text if text.endswith("\n") else text + "\n"
    return out, _folder_notes(answer.get("folders"), layout)


def planned_ui_folders(layout: paths.ServerLayout) -> dict[str, tuple[str, str, str]]:
    """The folders the root script makes for the dashboard server: path -> (owner, group, mode)."""
    base = units.sandbox_path(layout.base)
    p, w = layout.account, layout.web_account
    return {f"{base}/ui": (p, w, "2750"), f"{base}/ui/data": (p, w, "2750"),
            f"{base}/ui/specs": (p, w, "2750"), f"{base}/ui/auth": (w, w, "0700")}


def _folder_notes(folders: Any, layout: paths.ServerLayout) -> list[str]:
    """Compare the folders easy-ui says its units need with the ones this script makes."""
    if not isinstance(folders, list):
        return []
    base = units.sandbox_path(layout.base)
    planned = planned_ui_folders(layout)
    notes = []
    for f in folders[:50]:
        if not isinstance(f, dict):
            continue
        path, want = f.get("path"), (f.get("owner"), f.get("group"), str(f.get("mode")))
        if path == base:
            continue  # deliberately root:root 0711 (see the script's comments); the web account can pass through
        if planned.get(path) != want:
            notes.append(f"easy-ui expects {str(path)[:200]} as {':'.join(map(str, want[:2]))} {want[2]}, which this "
                         "plan does not make that way, so the dashboard server may not start. Update mes or easy-ui.")
    return notes


def build_plan(project_dir: str | os.PathLike[str] | None, name: str, *, server_root: str = str(paths.SERVER_ROOT),
               tools: dict[str, str | None] | None = None, key_files: list[str] = (),
               signer_files: list[str] = (), units_dir: str = UNITS_DIR, allow_hosts: list[str] = ()) -> PlanInput:
    from mes.jobs import load_project

    _, project = load_project(project_dir)
    root_dir = project.root
    if project.name != name:
        raise UsageError(f'The project in this folder is "{project.name}", not "{name}", so no plan was made.',
                         hint=f"Run: mes server plan {project.name}")
    check_server_name(project.name)
    for host in allow_hosts:
        if not isinstance(host, str) or not _HOST_RE.match(host):
            raise UsageError(f"--allow-host {str(host)[:80]!r} is not a host name.",
                             hint="For example: --allow-host my-server.example-tailnet.ts.net")
    layout = paths.server_layout(project.name, units.sandbox_path(server_root))
    chosen = {**DEFAULT_TOOLS, **{k: v for k, v in (tools or {}).items() if v}}
    if not project.has_dashboards and not (tools or {}).get("easy_ui"):
        chosen["easy_ui"] = None
    checked: dict[str, str | None] = {}
    for key, value in chosen.items():
        checked[key] = units.check_tool_path(value, f"the path of {key.replace('_', '-')}") if value else None
    plan = PlanInput(project=project, layout=layout, tools=checked, units_dir=units.sandbox_path(units_dir))
    for spec in key_files:
        level, sep, path = spec.partition("=")
        if not sep or level not in KEY_LEVELS or not path:
            raise UsageError(f"--key {spec!r} is not in the form level=file.",
                             hint="For example: --key read=~/.ssh/agent-read.pub (levels: read, write, deploy)")
        kind, b64 = read_key_file(path, what=f"The {level} key")
        plan.keys.append((level, kind, b64))
    for path in signer_files:
        kind, b64 = read_key_file(path, what="The signing key", allowed=SIGNER_TYPES)
        plan.signers.append((kind, b64))
    plan.units = units.render_project(project, layout=layout, mes=checked["mes"], easy_ui=checked["easy_ui"])
    from mes.deploy import units_fingerprint

    plan.fingerprint = units_fingerprint(project, layout, checked["mes"], checked["easy_ui"])
    if project.has_dashboards and checked["easy_ui"]:
        plan.web_units, notes = _web_units(project, root_dir, layout, checked["easy_ui"], list(allow_hosts))
        plan.notes += notes
    if not plan.keys:
        plan.notes.append("No SSH keys were given, so nobody can reach this project over SSH yet (not even to "
                          "deploy). Add them with --key deploy=<your key.pub> (and read=/write= for agents).")
    if not plan.signers:
        plan.notes.append("No signing key was given, so the server will refuse every deploy until one is added "
                          "with --signer <signing key.pub>.")
    if not any(level == "deploy" for level, _, _ in plan.keys):
        plan.notes.append("No key may deploy (--key deploy=...), so 'mes deploy' cannot reach this project yet.")
    return plan


# --------------------------------------------------------------------------- root script


def per_app(project: manifest_mod.Project) -> list[dict[str, Any]]:
    """What each installed app is allowed: its jobs' network hosts, memory and time limits."""
    out = []
    for spec in project.apps.values():
        jobs = [j for j in project.jobs.values() if j.app == spec.name]
        hosts = list(dict.fromkeys(h for j in jobs for h in j.network))
        out.append({"app": spec.name, "title": spec.title, "location": spec.location,
                    "network": hosts, "jobs": [{"id": j.id, "network": list(j.network), "memory": j.memory,
                                                "timeout": j.timeout, "writes": list(j.writes)} for j in jobs]})
    return out


def _per_app_lines(project: manifest_mod.Project) -> list[str]:
    lines = []
    for a in per_app(project):
        net = ", ".join(a["network"]) if a["network"] else "no internet"
        lines.append(f"{a['app']} ({a['location']}): {net}")
        for j in a["jobs"]:
            lines.append(f"  {j['id']}: memory {j['memory']}, time limit {j['timeout']}, internet: "
                         + (", ".join(j["network"]) if j["network"] else "none"))
        if not a["jobs"]:
            lines.append("  (no jobs)")
    return lines


def _q(value: str) -> str:
    return shlex.quote(value)


def _delimiter(texts: list[str]) -> str:
    while True:
        d = f"MES_END_{secrets.token_hex(6)}"
        if not any(d in t for t in texts):
            return d


def _put(target: str, mode: str, owner: str, text: str, delim: str) -> list[str]:
    return [f"put_file {_q(target)} {mode} {_q(owner)} <<'{delim}'", text.rstrip("\n"), delim]


def _gate_shell(plan: PlanInput) -> str:
    lines = [
        "#!/bin/sh",
        f"# Login shell of {plan.layout.account}, written by 'mes server plan'. It only ever starts the mes gate,",
        "# with one of the levels set up for this project's keys, and refuses everything else.",
        'if [ "$#" -eq 2 ] && [ "$1" = "-c" ]; then',
        '  case "$2" in',
    ]
    for level in KEY_LEVELS:
        cmd = gate_command(plan, level)
        lines.append(f"    {_q(cmd)}) exec {cmd} ;;")
    lines += ["  esac", "fi", 'echo "This account only runs the mes gate." >&2', "exit 1", ""]
    return "\n".join(lines)


def _server_toml(plan: PlanInput) -> str:
    t = plan.tools
    lines = [
        "# Written by the root script of 'mes server plan'. Do not edit: run the plan again instead.",
        "[server]",
        f'project = "{plan.project.name}"',
        f'root = "{units.sandbox_path(plan.layout.root)}"',
        f'mes = "{t["mes"]}"',
        f'uv = "{t["uv"]}"',
    ]
    if t.get("easy_ui"):
        lines.append(f'easy_ui = "{t["easy_ui"]}"')
    lines += [f'units_sha256 = "{plan.fingerprint}"', f'planned_with = "mes {__version__}"', ""]
    return "\n".join(lines)


def _authorized_keys(plan: PlanInput) -> str:
    lines = [f"# Keys that may connect to {plan.layout.account}. Each one can only run the mes gate at its level.",
             "# Written by 'mes server plan'; run it again to change them."]
    for level, kind, b64 in plan.keys:
        lines.append(f'restrict,command="{gate_command(plan, level)}" {kind} {b64} mes-{level}')
    return "\n".join(lines) + "\n"


def _allowed_signers(plan: PlanInput) -> str:
    lines = [f"# Keys allowed to sign deploys of {plan.project.name} (namespace mes-deploy). Written by 'mes server plan'."]
    for kind, b64 in plan.signers:
        lines.append(f'{plan.project.name} namespaces="mes-deploy" {kind} {b64}')
    return "\n".join(lines) + "\n"


_PRELUDE = r'''set -euo pipefail
umask 077
export LC_ALL=C
PATH=/usr/sbin:/usr/bin:/sbin:/bin

die() { printf 'Stopped: %s\n' "$1" >&2; exit 1; }
say() { printf '%s\n' "$1"; }

# put_file <target> <mode> <owner:group>, with the content on stdin. The file is written next to
# its target and moved into place in one step. Targets are only ever in folders that belong to root.
put_file() {
  local target="$1" mode="$2" owner="$3" tmp
  tmp="$(mktemp "${target}.mes-new.XXXXXX")"
  cat > "$tmp"
  chmod "$mode" "$tmp"
  chown "$owner" "$tmp"
  mv -f -T "$tmp" "$target"
}

# check_tool <name> <path>: the program exists, and it and every folder above it belong to root
# and cannot be changed by other accounts (so no other account can swap the program).
check_tool() {
  local name="$1" path="$2" real dir perm interp
  [ -x "$path" ] || die "$name is not installed at $path. Install it there first (see: mes guide server)."
  real="$(readlink -f -- "$path")"
  dir="$real"
  while :; do
    [ "$(stat -c %u -- "$dir")" = 0 ] || die "$name ($real) or a folder above it does not belong to root, so another account could change it."
    perm="$(stat -c %a -- "$dir")"
    if (( 8#$perm & 8#022 )); then die "$name ($real) or a folder above it can be changed by other accounts."; fi
    [ "$dir" = / ] && break
    dir="$(dirname -- "$dir")"
  done
  if [ "$(head -c 2 -- "$real")" = '#!' ]; then
    interp="$(head -n 1 -- "$real" | sed -e 's/^#![[:space:]]*//' -e 's/[[:space:]].*$//')"
    case "$interp" in
      /*) check_tool "the Python that runs $name" "$interp" ;;
      *) die "$name starts a program that is not given with a full path." ;;
    esac
  fi
}

# ensure_dir <path> <owner> <group> <mode>: a real folder (never a link) with exactly these settings.
ensure_dir() {
  local path="$1" owner="$2" group="$3" mode="$4"
  [ -L "$path" ] && die "$path is a link; it must be a real folder. Nothing more was changed."
  if [ -e "$path" ] && [ ! -d "$path" ]; then die "$path exists but is not a folder."; fi
  install -d -o "$owner" -g "$group" -m "$mode" -- "$path"
  chown -- "$owner:$group" "$path"
  chmod -- "$mode" "$path"
}

# ensure_top <path>: the folder of all projects. Made if missing; otherwise only checked (it is
# shared with other projects, so this script never changes it).
ensure_top() {
  local path="$1" perm
  if [ ! -e "$path" ] && [ ! -L "$path" ]; then
    install -d -o root -g root -m 0755 -- "$path"
    return 0
  fi
  [ -d "$path" ] && [ ! -L "$path" ] || die "$path is not a real folder."
  [ "$(stat -c %u -- "$path")" = 0 ] || die "$path does not belong to root."
  perm="$(stat -c %a -- "$path")"
  if (( 8#$perm & 8#022 )); then die "$path can be changed by other accounts."; fi
}

# ensure_sub <base> <sub> <owner> <group> <mode>: a folder inside a folder that belongs to the
# project's account. A missing one is made in <base> (root's) and renamed into place; an existing
# one must already be a real folder with these settings (nothing inside it is changed).
ensure_sub() {
  local base="$1" sub="$2" owner="$3" group="$4" mode="$5" path tmp
  path="$base/$sub"
  if [ -e "$path" ] || [ -L "$path" ]; then
    [ -d "$path" ] && [ ! -L "$path" ] || die "$path is not a real folder. Move it away and run this script again."
    [ "$(stat -c %U:%G:%a -- "$path")" = "$owner:$group:${mode#0}" ] || die "$path does not have the expected owner and permissions ($owner:$group $mode). Move it away and run this script again."
    return 0
  fi
  tmp="$(mktemp -d "$base/.mes-new.XXXXXX")"
  chown -- "$owner:$group" "$tmp"
  chmod -- "$mode" "$tmp"
  mv -T -- "$tmp" "$path" || { rmdir -- "$tmp"; die "$path could not be created."; }
}

# ensure_account <name> <home> <shell> <comment>: a system account in its own group, with no
# other groups (so no sudo). An existing account is only changed if mes made it.
ensure_account() {
  local name="$1" home="$2" shell="$3" comment="$4" existing
  getent group "$name" >/dev/null || groupadd --system -- "$name"
  if existing="$(getent passwd "$name")"; then
    [ "$(printf '%s' "$existing" | cut -d: -f5)" = "$comment" ] || die "an account called $name already exists and was not made by mes. Nothing was changed for it."
  else
    useradd --system --gid "$name" --home-dir "$home" --no-create-home --shell "$shell" --comment "$comment" -- "$name"
  fi
  usermod --gid "$name" --home "$home" --shell "$shell" -G '' -- "$name"
}
'''


def render_script(plan: PlanInput) -> str:
    p = plan.project.name
    lay = plan.layout
    root = units.sandbox_path(lay.root)
    base = units.sandbox_path(lay.base)
    t = plan.tools
    gate_shell = _gate_shell(plan)
    files: list[tuple[str, str, str, str]] = [
        (f"{base}/etc/server.toml", "0644", "root:root", _server_toml(plan)),
        (f"{base}/etc/allowed_signers", "0644", "root:root", _allowed_signers(plan)),
        (f"{base}/etc/gate-shell", "0755", "root:root", gate_shell),
        (f"{base}/home/.ssh/authorized_keys", "0644", "root:root", _authorized_keys(plan)),
    ]
    all_units = {**plan.units, **plan.web_units}
    texts = [f[3] for f in files] + list(all_units.values())
    delim = _delimiter(texts)
    enable = [n for n in all_units if n.endswith((".timer", ".path", ".socket"))]
    keep = " ".join(sorted(plan.units))
    when = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    has_webhook = any(j.trigger == "webhook" for j in plan.project.jobs.values())

    out = [
        "#!/usr/bin/env bash",
        f'# Root script for the mes project "{p}", made by \'mes server plan\' (mes {__version__}) on {when}.',
        "#",
        "# What it does, in plain words:",
        "#  1. Checks that it runs as the administrator on Linux with systemd, and that mes, uv"
        + (" and easy-ui are" if t.get("easy_ui") else " are"),
        "#     installed where only the administrator can change them.",
        "#  2. Creates two accounts that have no password and cannot use sudo:",
        f"#       {lay.account:<22} owns the project's code, data and state; runs its jobs and the SSH gate",
        f"#       {lay.web_account:<22} runs only the dashboard server; it can read ui/ and nothing else",
        f"#  3. Creates {base}/ and its folders, each readable only by the account that needs it.",
        f"#  4. Writes the keys that may connect over SSH ({len(plan.keys)}; each one can only run the mes gate),",
        f"#     and the keys that may sign deploys ({len(plan.signers)}).",
        f"#  5. Writes the project's {len(all_units)} system units into {plan.units_dir} and switches on its",
        "#     schedules and sockets.",
        "#",
        "# What each app of the project may do in its jobs (network, memory, time):",
        *[f"#   {line}" for line in _per_app_lines(plan.project)],
        "#",
        "# It touches nothing else on this server. Running it again is safe: it brings everything back",
        "# to this plan. Undo it with the script from: mes server remove " + p,
        "#",
        "# Read it, then run:   sudo bash <this file>",
        "# Afterwards check:    mes server check " + p,
        "",
        _PRELUDE,
        "say 'Checking this server...'",
        '[ "$(id -u)" -eq 0 ] || die "this script must be run by the administrator: sudo bash $0"',
        '[ "$(uname -s)" = Linux ] || die "this script is for a Linux server."',
        'command -v systemctl >/dev/null || die "this server has no systemd, which runs the jobs."',
        "for tool in useradd usermod groupadd getent install stat readlink mktemp head sed cut; do",
        '  command -v "$tool" >/dev/null || die "the program $tool is missing."',
        "done",
        f"check_tool mes {_q(t['mes'])}",
        f"check_tool uv {_q(t['uv'])}",
    ]
    if t.get("easy_ui"):
        out.append(f"check_tool easy-ui {_q(t['easy_ui'])}")
    out += [
        "NOLOGIN=\"$(command -v nologin || true)\"",
        '[ -n "$NOLOGIN" ] || NOLOGIN=/usr/sbin/nologin',
        "",
        "say 'Creating the folders and the accounts...'",
        f"ensure_top {_q(root)}",
        "# The project's own folder belongs to root, so the project's account cannot replace etc/, home/",
        "# or log/. Others may pass through it (0711) but not list it; every folder inside decides who",
        "# may open it.",
        f"ensure_dir {_q(base)} root root 0711",
        f"ensure_dir {_q(base + '/etc')} root root 0755",
        f"ensure_dir {_q(base + '/home')} root root 0755",
        f"ensure_dir {_q(base + '/home/.ssh')} root root 0755",
        f"ensure_account {_q(lay.account)} {_q(base + '/home')} {_q(base + '/etc/gate-shell')} "
        f"{_q('mes project ' + p)}",
        f"ensure_account {_q(lay.web_account)} /nonexistent \"$NOLOGIN\" {_q('mes dashboards of ' + p)}",
        "# No password at all: '*' matches no password, so nobody can log in with one, while the SSH",
        "# keys below still work for the gate. The dashboard account cannot log in in any way.",
        f"usermod -p '*' -- {_q(lay.account)}",
        f"usermod -p '!' -- {_q(lay.web_account)}",
        f"ensure_dir {_q(base + '/code')} {_q(lay.account)} {_q(lay.account)} 0700",
        f"ensure_dir {_q(base + '/data')} {_q(lay.account)} {_q(lay.account)} 0700",
        f"ensure_dir {_q(base + '/state')} {_q(lay.account)} {_q(lay.account)} 0700",
        f"ensure_dir {_q(base + '/store')} {_q(lay.account)} {_q(lay.account)} 0700",
        "# ui/: published dashboards and data bundles. The project's account writes them; the dashboard",
        "# account reads them through its group (new files inherit the group).",
        f"ensure_dir {_q(base + '/ui')} {_q(lay.account)} {_q(lay.web_account)} 2750",
        "# Folders inside ui/ (which the project's account owns) are made in a folder only root can change",
        "# and then moved in with one rename, so no link planted in ui/ can redirect them. Existing ones",
        "# are only checked.",
        f"ensure_sub {_q(base)} ui/data {_q(lay.account)} {_q(lay.web_account)} 2750",
        f"ensure_sub {_q(base)} ui/specs {_q(lay.account)} {_q(lay.web_account)} 2750",
        "# ui/auth: the dashboard sign-in (password hash, passkeys, sessions); only the dashboard account.",
        f"ensure_sub {_q(base)} ui/auth {_q(lay.web_account)} {_q(lay.web_account)} 0700",
        "# log/: the tamper-evident run log. Only root writes it; the project's account can read it.",
        f"ensure_dir {_q(base + '/log')} root {_q(lay.account)} 0750",
        f"[ -e {_q(base + '/log/runs.jsonl')} ] || install -m 0640 -o root -g {_q(lay.account)} /dev/null "
        f"{_q(base + '/log/runs.jsonl')}",
        "",
        "say 'Writing the keys, the gate and the settings...'",
    ]
    for target, mode, owner, text in files:
        out += _put(target, mode, owner, text, delim)
    if has_webhook:
        key = base + "/etc/webhook.key"
        out += [
            "# state/triggers: where the webhook receiver leaves a note that starts a job (the receiver may",
            "# write only there). Made like the folders in ui/, inside the project's own state folder.",
            f"ensure_sub {_q(base)} state/triggers {_q(lay.account)} {_q(lay.account)} 0700",
        ]
        out += [
            "# The secret that webhook senders sign with. It is made once on this server; read it with",
            f"#   sudo cat {key}",
            f"if [ ! -s {_q(key)} ]; then",
            f"  od -An -tx1 -N32 /dev/urandom | tr -d ' \\n' | put_file {_q(key)} 0640 {_q('root:' + lay.account)}",
            "fi",
        ]
    out += ["", "say 'Writing the system units...'"]
    for name, text in all_units.items():
        out += _put(f"{plan.units_dir}/{name}", "0644", "root:root", text, delim)
    out += [
        "# Units of this project that are no longer in the plan are switched off and removed; this includes",
        "# units in the old form (mes-<project>.job.<job>.*, mes-<project>.net.<job>.service) from before jobs",
        "# were named <app>.<job>.",
        f"KEEP={_q(keep)}",
        f"for f in {_q(plan.units_dir)}/mes-{p}.*; do",
        '  [ -e "$f" ] || continue',
        '  name="$(basename -- "$f")"',
        '  case " $KEEP " in *" $name "*) continue ;; esac',
        f"  grep -qxF {_q(units.MARKER_PROJECT + p)} -- \"$f\" || continue",
        '  systemctl disable --now -- "$name" >/dev/null 2>&1 || true',
        '  rm -f -- "$f"',
        '  say "Removed $name (no longer in the plan)."',
        "done",
        "systemctl daemon-reload",
    ]
    if enable:
        out.append("systemctl enable --now -- " + " ".join(_q(n) for n in enable))
    out += [
        "",
        f"say {_q(f'Done. The project {p} is set up. Check it (no sudo needed) with: mes server check {p}')}",
        "",
    ]
    return "\n".join(out)


def render_remove(project: str, *, server_root: str = str(paths.SERVER_ROOT), units_dir: str = UNITS_DIR,
                  delete_data: bool = False) -> str:
    p = check_server_name(project)
    lay = paths.server_layout(p, units.sandbox_path(server_root))
    base = units.sandbox_path(lay.base)
    udir = units.sandbox_path(units_dir)
    web_units = web_unit_names(p)
    out = [
        "#!/usr/bin/env bash",
        f'# Removal script for the mes project "{p}", made by \'mes server remove\' (mes {__version__}).',
        "#",
        "# What it does, in plain words:",
        "#  1. Switches off and removes the project's system units (only files marked as made by mes for",
        f"#     this project, and the dashboard server's {web_units[0]} and {web_units[1]}).",
        f"#  2. Removes the accounts {lay.account} and {lay.web_account}.",
    ]
    if delete_data:
        out.append(f"#  3. DELETES EVERYTHING in {base}, including the project's data. This cannot be undone.")
    else:
        out += [f"#  3. Removes the code, the keys and the settings in {base}, and KEEPS the data, state and",
                "#     store folders (handed to root, so no new account can inherit them)."]
    out += [
        "# It touches nothing else. Because it removes things, it only runs with --confirm:",
        "#   sudo bash <this file> --confirm",
        "",
        _PRELUDE,
        '[ "${1:-}" = --confirm ] || die "read this script first, then run it with --confirm."',
        '[ "$(id -u)" -eq 0 ] || die "this script must be run by the administrator: sudo bash $0 --confirm"',
        f"for f in {_q(udir)}/mes-{p}.*; do",
        '  [ -e "$f" ] || continue',
        f"  grep -qxF {_q(units.MARKER_PROJECT + p)} -- \"$f\" || continue",
        '  name="$(basename -- "$f")"',
        '  systemctl disable --now -- "$name" >/dev/null 2>&1 || true',
        '  rm -f -- "$f"',
        "done",
    ]
    for name in web_units:
        out += [f"systemctl disable --now -- {_q(name)} >/dev/null 2>&1 || true", f"rm -f -- {_q(udir + '/' + name)}"]
    out += ["systemctl daemon-reload"]
    for account in (lay.account, lay.web_account):
        comment = f"mes project {p}" if account == lay.account else f"mes dashboards of {p}"
        out += [
            f"if existing=\"$(getent passwd {_q(account)})\"; then",
            f"  [ \"$(printf '%s' \"$existing\" | cut -d: -f5)\" = {_q(comment)} ] || die {_q(f'the account {account} was not made by mes; it was left alone.')}",
            f"  userdel -- {_q(account)}",
            "fi",
            f"getent group {_q(account)} >/dev/null && groupdel -- {_q(account)} || true",
        ]
    out.append(f"if [ -L {_q(base)} ]; then die {_q(base + ' is a link; nothing was deleted there.')}; fi")
    if delete_data:
        out += [f"rm -rf --one-file-system -- {_q(base)}", f"say {_q(f'Removed {p} and all of its data.')}"]
    else:
        for sub in ("code", "ui", "etc", "home", "log"):
            out.append(f"rm -rf --one-file-system -- {_q(base + '/' + sub)}")
        for sub in ("data", "state", "store"):
            out += [f"if [ -d {_q(base + '/' + sub)} ] && [ ! -L {_q(base + '/' + sub)} ]; then",
                    f"  chown -R -P root:root -- {_q(base + '/' + sub)}",
                    f"  chmod 0700 -- {_q(base + '/' + sub)}",
                    "fi"]
        out.append(f"say {_q(f'Removed {p}. Its data is kept in {base} (data, state, store), owned by root.')}")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- check


@dataclass
class ServerProbe:
    """How ``mes server check`` looks at the server (tests pass fakes)."""

    getpwnam: Callable[[str], Any] = pwd.getpwnam
    getgrall: Callable[[], list[Any]] = grp.getgrall
    lstat: Callable[[str], os.stat_result] = os.lstat
    read: Callable[[str], str] = lambda p: Path(p).read_text(encoding="utf-8")
    run: Callable[[list[str]], tuple[int, str]] = lambda argv: _run(argv)
    exists: Callable[[str], bool] = os.path.exists


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)
    return res.returncode, (res.stdout or res.stderr).strip()


def _item(id: str, title: str, ok: bool | None, why_ok: str, why_not: str, fix: str | None = None,
          detail: str | None = None) -> dict[str, Any]:
    if ok is None:
        return protocol.item(id, title, "skipped", why_not, detail=detail)
    return protocol.item(id, title, "done" if ok else "todo", why_ok if ok else why_not,
                         fix=None if ok else fix, detail=detail)


def run_check(name: str, *, server_root: str = str(paths.SERVER_ROOT), units_dir: str = UNITS_DIR,
              project_dir: str | None = None, probe: ServerProbe | None = None) -> dict[str, Any]:
    pr = probe or ServerProbe()
    p = check_server_name(name)
    lay = paths.server_layout(p, server_root)
    base = str(lay.base)
    fix = f"mes server plan {p} --out plan.sh, then: sudo bash plan.sh"
    items: list[dict[str, Any]] = []

    def stat_of(path: str) -> os.stat_result | None:
        try:
            return pr.lstat(path)
        except OSError:
            return None

    accounts = {}
    for acct, shell_ok in ((lay.account, lambda s: s == f"{base}/etc/gate-shell"),
                           (lay.web_account, lambda s: s.endswith("nologin") or s == "/bin/false")):
        try:
            pw = pr.getpwnam(acct)
        except KeyError:
            pw = None
        accounts[acct] = pw
        if pw is None:
            items.append(_item(f"account-{acct}", f"The account {acct} exists", False, "",
                               "The account is missing, so the project cannot run under its own name.", fix))
            continue
        extra = [g.gr_name for g in pr.getgrall() if acct in getattr(g, "gr_mem", [])]
        good = shell_ok(pw.pw_shell) and not extra and pw.pw_uid != 0
        items.append(_item(f"account-{acct}", f"The account {acct} exists and is locked down", good,
                           "It exists, has no other groups (so no sudo) and can only start what it should.",
                           "It exists but its login shell or groups are not what the plan sets"
                           + (f" (it is also in: {', '.join(extra)})" if extra else "") + ".", fix,
                           detail=f"shell {pw.pw_shell}"))

    def owner_ok(path: str, owner: str | None, group: str | None, mode: int) -> tuple[bool | None, str]:
        st = stat_of(path)
        if st is None:
            return False, "missing"
        if stat.S_ISLNK(st.st_mode):
            return False, "a link"
        want_uid = 0 if owner == "root" else getattr(accounts.get(owner or ""), "pw_uid", None)
        good = (want_uid is None or st.st_uid == want_uid) and stat.S_IMODE(st.st_mode) == mode
        if group is not None and group != "root":
            gid = getattr(accounts.get(group), "pw_gid", None)
            good = good and (gid is None or st.st_gid == gid)
        return good, f"mode {stat.S_IMODE(st.st_mode):04o}"

    folders = [
        (base, "root", None, 0o711), (f"{base}/etc", "root", None, 0o755), (f"{base}/home", "root", None, 0o755),
        (f"{base}/home/.ssh", "root", None, 0o755),
        (f"{base}/code", lay.account, lay.account, 0o700), (f"{base}/data", lay.account, lay.account, 0o700),
        (f"{base}/state", lay.account, lay.account, 0o700), (f"{base}/store", lay.account, lay.account, 0o700),
        (f"{base}/ui", lay.account, lay.web_account, 0o2750), (f"{base}/ui/data", lay.account, lay.web_account, 0o2750),
        (f"{base}/ui/specs", lay.account, lay.web_account, 0o2750),
        (f"{base}/ui/auth", lay.web_account, lay.web_account, 0o700),
        (f"{base}/log", "root", lay.account, 0o750),
    ]
    bad = []
    for path, owner, group, mode in folders:
        good, what = owner_ok(path, owner, group, mode)
        if not good:
            bad.append(f"{path} ({what})")
    items.append(_item("folders", "The project's folders exist with owner-only permissions", not bad,
                       "Each folder belongs to the account that needs it, and nobody else can open it.",
                       "Some folders are missing or have the wrong owner or permissions: " + "; ".join(bad[:6]), fix))

    for rel, title in (("etc/allowed_signers", "Deploy signing keys"), ("etc/gate-shell", "The gate's login shell"),
                       ("etc/server.toml", "The server settings"), ("home/.ssh/authorized_keys", "The SSH keys")):
        good, what = owner_ok(f"{base}/{rel}", "root", None, 0o755 if rel.endswith("gate-shell") else 0o644)
        items.append(_item(f"file-{rel.replace('/', '-').replace('.', '')}", f"{title} belong to root", good,
                           "Only the administrator can change it.", f"It is {what} or not owned by root.", fix))

    try:
        keys_text = pr.read(f"{base}/home/.ssh/authorized_keys")
    except OSError:
        keys_text = None
    if keys_text is not None:
        lines = [ln for ln in keys_text.splitlines() if ln.strip() and not ln.startswith("#")]
        forced = all(ln.startswith('restrict,command="') and " gate --project " + p + " --allow " in ln for ln in lines)
        items.append(_item("keys-forced", "Every SSH key can only run the mes gate", forced and bool(lines),
                           f"{len(lines)} key(s), each tied to the gate with no shell, forwarding or terminal.",
                           "No keys are set up yet." if not lines else "A key line is not tied to the gate.", fix))
    try:
        signers = [ln for ln in pr.read(f"{base}/etc/allowed_signers").splitlines() if ln and not ln.startswith("#")]
    except OSError:
        signers = []
    items.append(_item("signers", "At least one key may sign deploys", bool(signers),
                       f"{len(signers)} signing key(s).", "No signing key yet, so every deploy is refused.",
                       f"mes server plan {p} --signer <key.pub> --out plan.sh, then: sudo bash plan.sh"))

    try:
        import tomllib

        cfg = tomllib.loads(pr.read(f"{base}/etc/server.toml")).get("server", {})
    except (OSError, ValueError):
        cfg = {}
    for tool in ("mes", "uv", "easy_ui"):
        path = cfg.get(tool)
        if not path:
            if tool == "easy_ui":
                continue
            items.append(_item(f"tool-{tool}", f"{tool} is installed for the server", False, "",
                               "The server settings do not name it.", fix))
            continue
        st = stat_of(str(path))
        real = os.path.realpath(str(path)) if st is not None else None
        rst = stat_of(real) if real else None
        good = rst is not None and rst.st_uid == 0 and not rst.st_mode & 0o022
        items.append(_item(f"tool-{tool}", f"{tool.replace('_', '-')} is installed where only root can change it",
                           good, f"{path} belongs to root.",
                           f"{path} is missing or could be changed by another account.",
                           "Install it system-wide (see: mes guide server).", detail=str(path)))

    expected: dict[str, str] | None = None
    if project_dir is not None or manifest_mod.find() is not None:
        try:
            plan = build_plan(project_dir, p, server_root=server_root,
                              tools={k: cfg.get(k) for k in ("mes", "uv", "easy_ui")}, units_dir=units_dir)
            expected = plan.units
        except MesError as e:
            items.append(_item("units-plan", "The units can be compared with the plan", None, "",
                               f"skipped: {e.message}"))
    installed = []
    for unit_name in sorted(expected or {}):
        path = f"{units_dir}/{unit_name}"
        try:
            text = pr.read(path)
        except OSError:
            text = None
        installed.append((unit_name, text))
    if expected is not None:
        missing = [n for n, t in installed if t is None]
        differ = [n for n, t in installed if t is not None and t != expected[n]]
        items.append(_item("units", "The project's system units match the plan", not missing and not differ,
                           f"All {len(expected)} units are installed as planned.",
                           ("Missing: " + ", ".join(missing[:5]) + ". " if missing else "")
                           + ("Different from the plan (the project changed since): " + ", ".join(differ[:5])
                              if differ else ""), fix))
        active = []
        for unit_name in expected:
            if unit_name.endswith((".timer", ".socket", ".path")):
                code, out = pr.run(["systemctl", "is-active", unit_name])
                if code != 0:
                    active.append(f"{unit_name} ({out or 'inactive'})")
        items.append(_item("units-on", "Its schedules and sockets are switched on", not active,
                           "systemd runs them.", "Not running: " + ", ".join(active[:5]), fix))
    else:
        items.append(_item("units", "The project's system units match the plan", None, "",
                           "skipped: run this in the project's folder (or with --project-dir) to compare the units "
                           "with the plan."))
    st = stat_of(f"{base}/code")
    items.append(_item("deployed", "A release is deployed", None, "",
                       "skipped: only the project's account can look inside its code folder. 'mes deploy <host> "
                       "--releases' shows what is installed." if st is not None else
                       "skipped: the project is not set up yet."))
    return protocol.checklist(items)


# --------------------------------------------------------------------------- command line


def _plan_text(plan: PlanInput, script: str, out: str | None) -> dict[str, Any]:
    p = plan.project.name
    runs = f"sudo bash {out}" if out else "save the script to a file, read it, then: sudo bash <file>"
    return {
        "project": p,
        "accounts": [plan.layout.account, plan.layout.web_account],
        "folder": str(plan.layout.base),
        "units": sorted({**plan.units, **plan.web_units}),
        "apps": per_app(plan.project),
        "keys": [{"level": lvl, "type": kind} for lvl, kind, _ in plan.keys],
        "signers": len(plan.signers),
        "tools": plan.tools,
        "notes": plan.notes,
        "written_to": out,
        "next_steps": [
            {"step": "Read the script", "why": "It says at the top, in plain words, everything it will do."},
            {"step": "Run it once on the server, as the administrator", "command": runs,
             "why": "Only this step needs the administrator. mes itself never uses sudo."},
            {"step": "Check the result (no sudo needed)", "command": f"mes server check {p}"},
            {"step": "Deploy from your computer", "command": "mes deploy <server>"},
        ],
        **({} if out else {"script": script}),
    }


def _cmd_plan(args: argparse.Namespace) -> Any:
    check_allowed("read" if args.out is None else "write")
    plan = build_plan(args.project_dir, args.project, server_root=args.server_root,
                      tools={"mes": args.mes, "uv": args.uv, "easy_ui": args.easy_ui},
                      key_files=args.key, signer_files=args.signer, units_dir=args.units_dir,
                      allow_hosts=args.allow_host)
    script = render_script(plan)
    if args.out:
        target = Path(args.out).expanduser()
        if target.exists() or target.is_symlink():
            raise Refused(f"{target} already exists, so it was not overwritten.",
                          hint="Choose another file name, or move the old one away.", code="exists")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
    result = _plan_text(plan, script, args.out)
    if args.json:
        return {**result, "sha256": hashlib.sha256(script.encode()).hexdigest()}
    if not args.out:
        return script
    lines = [f'The root script for "{plan.project.name}" is in {args.out}. Nothing was changed on any computer.',
             "What each app may do in its jobs:", *[f"  {line}" for line in _per_app_lines(plan.project)]]
    lines += [f"{i}. {s['step']}" + (f": {s['command']}" if s.get("command") else "") for i, s in
              enumerate(result["next_steps"], 1)]
    lines += [f"Please note: {n}" for n in plan.notes]
    return "\n".join(lines)


def _cmd_check(args: argparse.Namespace) -> Any:
    check_allowed("read")
    return run_check(args.project, server_root=args.server_root, units_dir=args.units_dir,
                     project_dir=args.project_dir)


def _cmd_remove(args: argparse.Namespace) -> Any:
    check_allowed("read" if args.out is None else "write")
    script = render_remove(args.project, server_root=args.server_root, units_dir=args.units_dir,
                           delete_data=args.delete_data)
    if args.out:
        target = Path(args.out).expanduser()
        if target.exists() or target.is_symlink():
            raise Refused(f"{target} already exists, so it was not overwritten.", hint="Choose another file name.",
                          code="exists")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        msg = (f"The removal script is in {args.out}. Read it, then run: sudo bash {args.out} --confirm. "
               + ("It DELETES the project's data." if args.delete_data else "It keeps the project's data."))
        return {"written_to": args.out, "delete_data": args.delete_data, "message": msg} if args.json else msg
    return {"script": script, "delete_data": args.delete_data} if args.json else script


def register(subparsers: Any) -> None:
    s = subparsers.add_parser(
        "server",
        help="Set a project up on a Linux server (plan a root script, check it, remove it).",
        description="mes never uses sudo: 'plan' writes one readable root script that you run once with sudo, "
        "'check' verifies the result without root, 'remove' writes the matching removal script. "
        "See: mes guide server",
    )
    sub = s.add_subparsers(dest="server_command", metavar="<plan|check|remove>")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("project", help="the project's name (as in project.toml)")
        p.add_argument("--server-root", default=str(paths.SERVER_ROOT), metavar="DIR",
                       help="where projects live on the server (default /var/lib/mes)")
        p.add_argument("--units-dir", default=UNITS_DIR, metavar="DIR",
                       help=f"where system units go (default {UNITS_DIR})")

    p = sub.add_parser("plan", help="Write the root script that sets the project up on a server.")
    common(p)
    p.add_argument("--project-dir", default=None, metavar="DIR", help="the project's folder (default: here)")
    p.add_argument("--out", default=None, metavar="FILE", help="write the script to FILE (owner-only) instead of printing it")
    p.add_argument("--key", action="append", default=[], metavar="LEVEL=FILE",
                   help="an SSH public key that may connect: read=, write= or deploy= (repeatable)")
    p.add_argument("--signer", action="append", default=[], metavar="FILE",
                   help="an Ed25519 public key that may sign deploys (repeatable)")
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="a name the dashboards are opened under, such as the server's Tailscale name (repeatable)")
    p.add_argument("--mes", default=None, metavar="PATH", help=f"mes on the server (default {DEFAULT_TOOLS['mes']})")
    p.add_argument("--uv", default=None, metavar="PATH", help=f"uv on the server (default {DEFAULT_TOOLS['uv']})")
    p.add_argument("--easy-ui", dest="easy_ui", default=None, metavar="PATH",
                   help=f"easy-ui on the server (default {DEFAULT_TOOLS['easy_ui']})")
    p.set_defaults(func=_cmd_plan)

    c = sub.add_parser("check", help="Check, without root, that the server matches the plan (a checklist).")
    common(c)
    c.add_argument("--project-dir", default=None, metavar="DIR", help="the project's folder, to compare the units")
    c.set_defaults(func=_cmd_check)

    r = sub.add_parser("remove", help="Write the script that removes the project from a server.")
    common(r)
    r.add_argument("--out", default=None, metavar="FILE", help="write the script to FILE instead of printing it")
    r.add_argument("--delete-data", action="store_true", help="also delete the project's data (cannot be undone)")
    r.set_defaults(func=_cmd_remove)
