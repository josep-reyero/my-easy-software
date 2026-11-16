"""``mes server plan|check|remove|selftest|analyze <project>``: set a project up on a Linux server, safely.

mes never runs sudo and never installs system software. ``plan`` renders **one readable root
script** that a person reads and runs once per project with ``sudo bash <file>``:

- two accounts without a password and without sudo: ``mes-<project>`` (owns code, data and
  state; runs the jobs and the SSH gate) and ``mes-<project>-web`` (runs only the dashboard
  server; can read ``ui/`` and its own ``auth/`` and nothing else; no network of its own);
- ``/var/lib/mes/<project>/`` and its folders, each protecting itself by its own owner and mode
  (the project folder itself is root's, and others may only pass through it);
- the SSH keys that may connect, each forced into ``mes gate --project <p> --allow <level>``
  with ``restrict`` (no shell, pty, forwarding or agent), and a login shell that runs nothing
  but the gate (written before the account is made);
- the keys allowed to sign deploys and rollbacks (``etc/allowed_signers``, Ed25519 only), which
  must differ from the deploy login key;
- the project's system units (jobs, timers, the network helper of jobs that list hosts, the
  dashboard data refresh, the gate's per-request sandboxes, webhooks, the self-test probe) and
  the dashboard server's socket and service. mes renders all of them from its own templates;
  from easy-ui it takes only checked parameters (``easy-ui units --json``), never unit text.

Before anything, the script checks the tools: mes, its Python, uv and easy-ui, every folder and
link on the way to them, and the interpreter behind each script must belong to root and not be
changeable by others, must not be in a home or temporary folder, and must be usable by the
project's accounts; mes's Python must be an isolated virtual environment
(``include-system-site-packages = false``) with mes's own code root-owned too.

The script is idempotent (running it again brings the server back to the plan), uses
``set -euo pipefail``, and touches nothing outside ``/var/lib/mes/<project>``, the two accounts
and the project's own unit files. Every value written into it is validated first (names
against strict patterns, paths against a safe character set, keys by their exact format) and
single-quoted as well. Its header names the mes version that made it and a fingerprint (SHA-256)
of everything below the header; the script checks that fingerprint when it starts.

``plan`` refuses to run from a copy of mes (or easy-ui) inside the project's own folder or its
virtual environment: the plan is root's, so it must come from the system-installed mes.

``check`` verifies the result as a checklist without root. ``remove`` renders the matching
removal script (data is kept unless ``--delete-data``). ``selftest`` asks the real units what
they can reach (see ``mes.selftest``). ``analyze`` renders a script of ``systemd-analyze verify``
and ``systemd-analyze security`` calls for every unit.

Public API
----------
``MAX_SERVER_NAME`` (24: ``mes-<name>-web`` must fit the 32-character account limit),
``DEFAULT_TOOLS``, ``UNITS_DIR``, ``PLAN_MARKER``.
``check_server_name(name) -> str``
``parse_public_key(text, *, what) -> (kind, base64)``
``read_key_file(path, *, what) -> (kind, base64)``
``PlanInput``  everything a plan needs (project, layout, tools, keys, signers, web units).
``build_plan(...) -> PlanInput``; ``render_script(plan) -> str``; ``render_remove(...) -> str``
``render_analyze(plan) -> str``
``gate_command(plan, level) -> str``  the forced command of one key level.
``plan_fingerprint(script) -> str``   the SHA-256 the header names.
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
import shutil
import stat
import subprocess
import sys
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
DEFAULT_TOOLS = {"mes": "/usr/local/bin/mes", "mes_python": None, "uv": "/usr/local/bin/uv",
                 "easy_ui": "/usr/local/bin/easy-ui"}
KEY_LEVELS = ("read", "write", "deploy")
SIGNER_TYPES = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com")
LOGIN_KEY_TYPES = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com")
PLAN_MARKER = "# ---- the plan starts here (the fingerprint above covers every line below) ----"
_B64_RE = re.compile(r"^[A-Za-z0-9+/]{60,1000}={0,3}\Z")
_HOST_RE = re.compile(r"^(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z")


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
    allow_builds: bool = False


def gate_command(plan: PlanInput, level: str) -> str:
    if level not in KEY_LEVELS:
        raise ValueError(level)
    return (f"{plan.tools['mes']} gate --project {plan.project.name} --allow {level} "
            f"--server-root {units.sandbox_path(plan.layout.root)}")


def _inside(path: str | os.PathLike[str] | None, folder: Path) -> bool:
    if not path:
        return False
    try:
        return paths.is_inside(path, folder)
    except (OSError, RuntimeError):
        return False


def refuse_project_tools(project_root: Path, easy_ui: str | None = None) -> None:
    """The plan is root's: it must come from the system-installed mes, never from a copy inside
    the project's folder or its virtual environment (which the project's own code can change)."""
    root = Path(project_root).resolve()
    found = {
        "mes (the program making this plan)": [sys.prefix, sys.executable],
        "the mes found first on PATH": [shutil.which("mes")],
        "easy-ui": [easy_ui, shutil.which("easy-ui")],
    }
    for what, candidates in found.items():
        for c in candidates:
            if c and (_inside(c, root) or _inside(os.path.realpath(c), root)):
                raise Refused(
                    f"{what[0].upper() + what[1:]} is inside the project's own folder ({c}), so it was not used to make "
                    "a root script: the project's code could have changed it.",
                    hint="Run the system-installed mes instead (not 'uv run mes', and with no project environment "
                    "activated), for example: /usr/local/bin/mes server plan <project> --project-dir <folder>",
                    code="tool_in_project",
                )


def _shebang_python(path: str) -> str | None:
    """The Python a console script names on its first line (also uv's long-path trampoline)."""
    try:
        with open(path, "rb") as f:
            head = f.read(2048).decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    if not head or not head[0].startswith("#!"):
        return None
    interp = head[0][2:].strip().split()[0] if head[0][2:].strip() else ""
    if os.path.basename(interp) in ("sh", "bash", "dash") and len(head) > 1:
        m = re.match(r"^'''exec' '([^']+)' ", head[1])
        interp = m.group(1) if m else interp
    return interp if interp.startswith("/") else None


def mes_python_for(mes: str, given: str | None) -> str:
    if given:
        return units.check_tool_path(given, "the path of mes's Python (--mes-python)")
    found = _shebang_python(mes)
    if found:
        try:
            return units.check_tool_path(found, "the Python of mes")
        except Refused:
            pass
    return units.DEFAULT_MES_PYTHON


def _web_units(project: manifest_mod.Project, project_dir: Path, layout: paths.ServerLayout,
               easy_ui: str, origins: list[str]) -> tuple[dict[str, str], list[str]]:
    """Ask easy-ui for the dashboard server's parameters (never its unit text), check each one and
    render the units from mes's own template."""
    if siblings.tool_path("easy-ui") is None:
        return {}, ["The dashboard server's units are not in this plan, because easy-ui (my-easy-ui) is not "
                    "installed on this computer. Install it system-wide and run 'mes server plan' again."]
    if not origins:
        return {}, ["The dashboard server's units are not in this plan, because no web address was given. Add the "
                    "server's private name: --allow-host <server>.<tailnet>.ts.net (or --origin https://NAME:PORT)."]
    argv = siblings.easy_ui_argv(project_dir, "units", "--render-only", "--exec", easy_ui,
                                 "--root", units.sandbox_path(layout.root))
    for o in origins:
        argv += ["--origin", o]
    try:
        answer = siblings.run_json(argv, what="Asking easy-ui for the dashboard server's settings (easy-ui units)",
                                   timeout=60)
    except MesError as e:
        return {}, ["The dashboard server's units are not in this plan, because easy-ui could not give its settings: "
                    f"{(e.detail or e.message)[:500]}"]
    params = units.check_web_params(answer, layout=layout, easy_ui=easy_ui, origins=origins)
    return units.render_web(project, layout=layout, params=params), []


def build_plan(project_dir: str | os.PathLike[str] | None, name: str, *, server_root: str = str(paths.SERVER_ROOT),
               tools: dict[str, str | None] | None = None, key_files: list[str] = (),
               signer_files: list[str] = (), units_dir: str = UNITS_DIR, allow_hosts: list[str] = (),
               origins: list[str] = (), allow_builds: bool = False, refuse_tools_in_project: bool = True) -> PlanInput:
    from mes.jobs import load_project

    _, project = load_project(project_dir)
    root_dir = project.root
    if project.name != name:
        raise UsageError(f'The project in this folder is "{project.name}", not "{name}", so no plan was made.',
                         hint=f"Run: mes server plan {project.name}")
    check_server_name(project.name)
    web_origins: list[str] = []
    for host in allow_hosts:
        if not isinstance(host, str) or not _HOST_RE.match(host):
            raise UsageError(f"--allow-host {str(host)[:80]!r} is not a host name.",
                             hint="For example: --allow-host my-server.example-tailnet.ts.net")
        web_origins.append(f"https://{host}")
    for o in origins:
        if not units.check_origin(o):
            raise UsageError(f"--origin {str(o)[:80]!r} is not a web address of the form https://NAME or https://NAME:PORT.",
                             hint="For example: --origin https://my-server.example-tailnet.ts.net")
        web_origins.append(o)
    web_origins = list(dict.fromkeys(web_origins))
    layout = paths.server_layout(project.name, units.sandbox_path(server_root))
    chosen = {**DEFAULT_TOOLS, **{k: v for k, v in (tools or {}).items() if v}}
    if project.ui is None and not (tools or {}).get("easy_ui"):
        chosen["easy_ui"] = None
    checked: dict[str, str | None] = {}
    for key, value in chosen.items():
        if key == "mes_python":
            continue
        checked[key] = units.check_tool_path(value, f"the path of {key.replace('_', '-')}") if value else None
    checked["mes_python"] = mes_python_for(checked["mes"], chosen.get("mes_python"))
    if refuse_tools_in_project:
        refuse_project_tools(root_dir, siblings.tool_path("easy-ui"))
    plan = PlanInput(project=project, layout=layout, tools=checked, units_dir=units.sandbox_path(units_dir),
                     allow_builds=bool(allow_builds))
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
    signer_blobs = {b64 for _, b64 in plan.signers}
    for level, _, b64 in plan.keys:
        if b64 in signer_blobs:
            raise Refused(
                f"The {level} key given with --key is also a signing key (--signer). One stolen key would then both "
                "reach the server and sign what it installs, so no plan was made.",
                hint="Use two keys: one to log in (--key deploy=~/.ssh/mes-login.pub) and one only for signing, best "
                "on a hardware security key (--signer ~/.ssh/mes-sign.pub, made with: ssh-keygen -t ed25519-sk).",
                code="same_key",
            )
    plan.units = units.render_project(project, layout=layout, mes=checked["mes"], mes_python=checked["mes_python"],
                                      easy_ui=checked["easy_ui"])
    from mes.deploy import units_fingerprint

    plan.fingerprint = units_fingerprint(project, layout, checked["mes"], checked["easy_ui"], checked["mes_python"])
    if project.ui is not None and checked["easy_ui"]:
        plan.web_units, notes = _web_units(project, root_dir, layout, checked["easy_ui"], web_origins)
        plan.notes += notes
    if not plan.keys:
        plan.notes.append("No SSH keys were given, so nobody can reach this project over SSH yet (not even to "
                          "deploy). Add them with --key deploy=<your login key.pub> (and read=/write= for agents).")
    if not plan.signers:
        plan.notes.append("No signing key was given, so the server will refuse every deploy until one is added "
                          "with --signer <signing key.pub>.")
    elif not any(kind.startswith("sk-") for kind, _ in plan.signers):
        plan.notes.append("The signing key is not on a hardware security key. That works, but a hardware key "
                          "(ssh-keygen -t ed25519-sk) cannot be copied off your computer, so it protects deploys best.")
    if not any(level == "deploy" for level, _, _ in plan.keys):
        plan.notes.append("No key may deploy (--key deploy=...), so 'mes deploy' cannot reach this project yet.")
    if plan.allow_builds:
        plan.notes.append("--allow-server-builds: packages that exist only as source code are built on the server, "
                          "in the deploy sandbox with the internet. Without it, the server installs only ready-made "
                          "wheels.")
    return plan


# --------------------------------------------------------------------------- root script


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
        f'mes_python = "{t["mes_python"]}"',
        f'uv = "{t["uv"]}"',
    ]
    if t.get("easy_ui"):
        lines.append(f'easy_ui = "{t["easy_ui"]}"')
    lines += [f"allow_builds = {'true' if plan.allow_builds else 'false'}",
              f'units_sha256 = "{plan.fingerprint}"', f'planned_with = "mes {__version__}"', ""]
    return "\n".join(lines)


def _authorized_keys(plan: PlanInput) -> str:
    lines = [f"# Keys that may connect to {plan.layout.account}. Each one can only run the mes gate at its level.",
             "# Written by 'mes server plan'; run it again to change them."]
    for level, kind, b64 in plan.keys:
        lines.append(f'restrict,command="{gate_command(plan, level)}" {kind} {b64} mes-{level}')
    return "\n".join(lines) + "\n"


def _allowed_signers(plan: PlanInput) -> str:
    from mes.deploy import SIGNER_NAMESPACES

    lines = [f"# Keys allowed to sign deploys and rollbacks of {plan.project.name}. Written by 'mes server plan'."]
    for kind, b64 in plan.signers:
        lines.append(f'{plan.project.name} namespaces="{SIGNER_NAMESPACES}" {kind} {b64}')
    return "\n".join(lines) + "\n"


def _webhook_jobs(plan: PlanInput) -> str:
    names = [j.name for j in plan.project.jobs.values() if j.trigger == "webhook"]
    return ("# Jobs a signed webhook may start (from project.toml when the plan was made). Written by "
            "'mes server plan'.\n" + "".join(f"{units._check_job(n)}\n" for n in names))


_PRELUDE = r"""set -euo pipefail
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

# owned_by_root <name> <path>: it belongs to root and no other account can change it; a folder
# must also let the project's accounts pass through (they run the tools).
owned_by_root() {
  local name="$1" path="$2" perm
  [ "$(stat -c %u -- "$path")" = 0 ] || die "$name: $path does not belong to root, so another account could change it."
  [ -L "$path" ] && return 0
  perm="$(stat -c %a -- "$path")"
  if (( 8#$perm & 8#022 )); then die "$name: $path can be changed by other accounts."; fi
  if [ -d "$path" ] && ! (( 8#$perm & 8#001 )); then die "$name: the project's accounts cannot pass through $path."; fi
  return 0
}

# follow <name> <path>: walks the path one part at a time, checking every folder and every link
# on the way (and where each link leads), like the system does when it opens the file. The real
# end of the path is left in REAL.
follow() {
  local name="$1" path="$2" cur=/ rest part next link hops=0
  case "$path" in /*) ;; *) die "$name is not given with a full path ($path)." ;; esac
  owned_by_root "$name" /
  rest="${path#/}"
  while [ -n "$rest" ]; do
    part="${rest%%/*}"
    if [ "$part" = "$rest" ]; then rest=""; else rest="${rest#*/}"; fi
    case "$part" in
      ''|.) continue ;;
      ..) cur="$(dirname -- "$cur")"; continue ;;
    esac
    next="${cur%/}/$part"
    if [ -L "$next" ]; then
      hops=$((hops + 1))
      [ "$hops" -le 40 ] || die "$name: $path goes through too many links."
      owned_by_root "$name" "$next"
      link="$(readlink -- "$next")"
      case "$link" in /*) cur=/ ;; esac
      rest="${link#/}${rest:+/$rest}"
      continue
    fi
    [ -e "$next" ] || die "$name: $next does not exist (on the way to $path)."
    owned_by_root "$name" "$next"
    cur="$next"
  done
  REAL="$cur"
}

# not_in_unsafe <name> <real path>: never in a home folder, a temporary folder or the projects' folder.
not_in_unsafe() {
  local name="$1" real="$2" r
  for r in /tmp /var/tmp /dev/shm /run/shm /home /root /run/user "$MES_ROOT"; do
    case "$real" in "$r"|"$r"/*) die "$name is really in $real, inside $r, where it could be changed or is hidden from the services. Install it as described in: mes guide server" ;; esac
  done
  return 0
}

# check_tool <name> <path>: the program, every folder and link on the way, and the program that
# runs it (a script's first line, also through uv's long-path trampoline) belong to root, cannot
# be changed by others, are outside home and temporary folders, and the project's accounts can
# run them. TOOL_INTERP is the script's interpreter as written (empty for a compiled program).
check_tool() {
  local name="$1" path="$2" depth="${3:-0}" real perm first interp tramp
  [ -x "$path" ] || die "$name is not installed at $path. Install it there first (see: mes guide server)."
  follow "$name" "$path"
  real="$REAL"
  not_in_unsafe "$name" "$real"
  [ -f "$real" ] && [ -x "$real" ] || die "$name ($real) is not a program."
  perm="$(stat -c %a -- "$real")"
  (( 8#$perm & 8#001 )) || die "$name ($real) cannot be run by the project's accounts."
  TOOL_INTERP=""
  if [ "$(head -c 2 -- "$real")" = '#!' ]; then
    (( 8#$perm & 8#004 )) || die "$name ($real) is a script the project's accounts cannot read."
    first="$(head -n 1 -- "$real" | sed -e 's/^#![[:space:]]*//')"
    interp="${first%%[[:space:]]*}"
    case "$(basename -- "$interp")" in
      env) die "$name starts its program through env, which looks it up in PATH. Reinstall it so its first line names its Python with a full path." ;;
      sh|bash|dash)
        tramp="$(sed -n -e "2s/^'''exec' '\([^']*\)' .*/\1/p" -- "$real")"
        [ -z "$tramp" ] || interp="$tramp" ;;
    esac
    case "$interp" in /*) ;; *) die "$name starts a program that is not given with a full path." ;; esac
    [ "$depth" -lt 3 ] || die "$name starts a chain of programs that is too long."
    check_tool "the program that runs $name" "$interp" $((depth + 1))
    TOOL_INTERP="$interp"
  fi
  return 0
}

# check_mes <mes> <python> <version>: mes is the script of that Python, which is an isolated
# virtual environment (no system packages), and mes's own code belongs to root as well. Root
# runs mes for the project's units only as: env -i PATH=/usr/bin:/bin <python> -I -m mes ...
check_mes() {
  local mes="$1" py="$2" version="$3" script_py venv got where
  check_tool mes "$mes"
  [ -n "$TOOL_INTERP" ] || die "$mes is not the mes program (it should be a Python script)."
  script_py="$TOOL_INTERP"
  [ "$script_py" = "$py" ] || die "$mes runs $script_py, but the plan expects $py. Make the plan again with: --mes-python $script_py"
  check_tool "the Python of mes" "$py"
  venv="$(dirname -- "$(dirname -- "$py")")"
  follow "the settings of mes's Python" "$venv/pyvenv.cfg"
  grep -Eq -e '^[[:space:]]*include-system-site-packages[[:space:]]*=[[:space:]]*false[[:space:]]*$' -- "$venv/pyvenv.cfg" \
    || die "mes's Python ($py) is not an isolated virtual environment (include-system-site-packages must be false). Install mes as described in: mes guide server"
  got="$(cd / && /usr/bin/env -i PATH=/usr/bin:/bin "$py" -I -m mes --version 2>/dev/null || true)"
  [ "$got" = "mes $version" ] || die "the plan was made by mes $version, but $py runs ${got:-no mes at all}. Make the plan with the mes installed on this server."
  where="$(cd / && /usr/bin/env -i PATH=/usr/bin:/bin "$py" -I -c 'import mes, os; print(os.path.dirname(os.path.realpath(mes.__file__)))')"
  follow "mes's own code" "$where/__init__.py"
  not_in_unsafe "mes's own code" "$REAL"
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

# ensure_group <name>: a system group of its own.
ensure_group() {
  getent group "$1" >/dev/null || groupadd --system -- "$1"
}

# ensure_account <name> <home> <shell> <comment>: a system account in its own group, with no
# other groups (so no sudo). An existing account is only changed if mes made it.
ensure_account() {
  local name="$1" home="$2" shell="$3" comment="$4" existing
  ensure_group "$name"
  if existing="$(getent passwd "$name")"; then
    [ "$(printf '%s' "$existing" | cut -d: -f5)" = "$comment" ] || die "an account called $name already exists and was not made by mes. Nothing was changed for it."
  else
    useradd --system --gid "$name" --home-dir "$home" --no-create-home --shell "$shell" --comment "$comment" -- "$name"
  fi
  usermod --gid "$name" --home "$home" --shell "$shell" -G '' -- "$name"
}
"""


def plan_fingerprint(script: str) -> str:
    """SHA-256 of every line below ``PLAN_MARKER`` (what the header of the script names)."""
    _, sep, body = script.partition(PLAN_MARKER + "\n")
    if not sep:
        raise ValueError("not a plan")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def render_script(plan: PlanInput) -> str:
    p = plan.project.name
    lay = plan.layout
    root = units.sandbox_path(lay.root)
    base = units.sandbox_path(lay.base)
    t = plan.tools
    acct, web = lay.account, lay.web_account
    has_webhook = any(j.trigger == "webhook" for j in plan.project.jobs.values())
    group_files = f"root:{acct}"
    files: list[tuple[str, str, str, str]] = [
        (f"{base}/etc/server.toml", "0640", group_files, _server_toml(plan)),
        (f"{base}/etc/allowed_signers", "0640", group_files, _allowed_signers(plan)),
        (f"{base}/etc/webhook-jobs", "0640", group_files, _webhook_jobs(plan)),
        (f"{base}/home/.ssh/authorized_keys", "0640", group_files, _authorized_keys(plan)),
    ]
    gate_shell = (f"{base}/etc/gate-shell", "0750", group_files, _gate_shell(plan))
    all_units = {**plan.units, **plan.web_units}
    texts = [f[3] for f in files] + [gate_shell[3]] + list(all_units.values())
    delim = _delimiter(texts)
    enable = [n for n in all_units if n.endswith((".timer", ".path", ".socket"))]
    keep = " ".join(sorted(plan.units))
    when = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d %H:%M UTC")

    body = [
        _PRELUDE,
        f"MES_ROOT={_q(root)}",
        "# The header names a fingerprint of everything below it; a changed copy stops here.",
        'SELF="${BASH_SOURCE[0]:-}"',
        'case "$SELF" in /*|"") ;; *) SELF="./$SELF" ;; esac',
        'if [ -n "$SELF" ] && [ -f "$SELF" ] && command -v sha256sum >/dev/null; then',
        "  want=\"$(sed -n -e 's/^# Plan fingerprint (SHA-256): //p' -- \"$SELF\" | head -n 1)\"",
        f"  got=\"$(awk -v m={_q(PLAN_MARKER)} 'f {{ print }} $0 == m {{ f = 1 }}' \"$SELF\" | sha256sum | cut -d' ' -f1)\"",
        '  [ "$want" = "$got" ] || die "this script was changed after mes wrote it (its fingerprint does not match). Make it again with mes server plan."',
        '  say "Plan fingerprint: $got"',
        "fi",
        f"say {_q(f'Plan made by mes {__version__} for the project {p}.')}",
        "say 'Checking this server...'",
        '[ "$(id -u)" -eq 0 ] || die "this script must be run by the administrator: sudo bash $0"',
        '[ "$(uname -s)" = Linux ] || die "this script is for a Linux server."',
        'command -v systemctl >/dev/null || die "this server has no systemd, which runs the jobs."',
        "for tool in useradd usermod groupadd getent install stat readlink mktemp head sed cut grep awk dirname basename; do",
        '  command -v "$tool" >/dev/null || die "the program $tool is missing."',
        "done",
        f"check_mes {_q(t['mes'])} {_q(t['mes_python'])} {_q(__version__)}",
        f"check_tool uv {_q(t['uv'])}",
    ]
    if t.get("easy_ui"):
        body.append(f"check_tool easy-ui {_q(t['easy_ui'])}")
    body += [
        "NOLOGIN=\"$(command -v nologin || true)\"",
        '[ -n "$NOLOGIN" ] || NOLOGIN=/usr/sbin/nologin',
        "",
        "say 'Creating the folders and the accounts...'",
        f"ensure_group {_q(acct)}",
        f"ensure_group {_q(web)}",
        f"ensure_top {_q(root)}",
        "# The project's own folder belongs to root, so the project's account cannot replace etc/, home/,",
        "# auth/, gate/ or log/. Others may pass through it (0711) but not list it; every folder inside",
        "# protects itself by its own owner and mode.",
        f"ensure_dir {_q(base)} root root 0711",
        "# etc/ and home/ belong to root; only the project's account may look inside (group, read-only).",
        f"ensure_dir {_q(base + '/etc')} root {_q(acct)} 0750",
        f"ensure_dir {_q(base + '/home')} root {_q(acct)} 0750",
        f"ensure_dir {_q(base + '/home/.ssh')} root {_q(acct)} 0750",
        "# The gate's login shell exists before the account that uses it.",
        *_put(*gate_shell, delim),
        f"ensure_account {_q(acct)} {_q(base + '/home')} {_q(base + '/etc/gate-shell')} {_q('mes project ' + p)}",
        f"ensure_account {_q(web)} /nonexistent \"$NOLOGIN\" {_q('mes dashboards of ' + p)}",
        "# No password at all: '*' matches no password, so nobody can log in with one, while the SSH",
        "# keys below still work for the gate. The dashboard account cannot log in in any way.",
        f"usermod -p '*' -- {_q(acct)}",
        f"usermod -p '!' -- {_q(web)}",
        f"ensure_dir {_q(base + '/code')} {_q(acct)} {_q(acct)} 0700",
        f"ensure_dir {_q(base + '/data')} {_q(acct)} {_q(acct)} 0700",
        f"ensure_dir {_q(base + '/state')} {_q(acct)} {_q(acct)} 0700",
        f"ensure_dir {_q(base + '/store')} {_q(acct)} {_q(acct)} 0700",
        "# gate/: the gate's log, locks and deploy records; webhook/: the webhook receiver's replay record",
        "# and trigger notes. Both belong to the project's account, but no job or sandboxed request has",
        "# them writable (or even sees them).",
        f"ensure_dir {_q(base + '/gate')} {_q(acct)} {_q(acct)} 0700",
        f"ensure_dir {_q(base + '/webhook')} {_q(acct)} {_q(acct)} 0700",
        "# backups/: where 'mes backup' keeps the project's backups on this server, out of every job's reach.",
        f"ensure_dir {_q(base + '/backups')} {_q(acct)} {_q(acct)} 0700",
        "# ui/: published dashboards and data bundles. The project's account writes them; the dashboard",
        "# account reads them through its group (new files inherit the group).",
        f"ensure_dir {_q(base + '/ui')} {_q(acct)} {_q(web)} 2750",
        "# Folders inside ui/ (which the project's account owns) are made in a folder only root can change",
        "# and then moved in with one rename, so no link planted in ui/ can redirect them. Existing ones",
        "# are only checked.",
        f"ensure_sub {_q(base)} ui/data {_q(acct)} {_q(web)} 2750",
        f"ensure_sub {_q(base)} ui/specs {_q(acct)} {_q(web)} 2750",
        "# auth/: the dashboard sign-in (password hash, passkeys, recovery codes, sessions). Only the",
        "# dashboard account; beside ui/, never inside it, in root's folder, so the project cannot reach it.",
        f"ensure_dir {_q(base + '/auth')} {_q(web)} {_q(web)} 0700",
        "# log/: the tamper-evident run log. Only root writes it; the project's account can read it.",
        f"ensure_dir {_q(base + '/log')} root {_q(acct)} 0750",
        f"[ -e {_q(base + '/log/runs.jsonl')} ] || install -m 0640 -o root -g {_q(acct)} /dev/null "
        f"{_q(base + '/log/runs.jsonl')}",
        "",
        "say 'Writing the keys, the gate and the settings...'",
    ]
    for target, mode, owner, text in files:
        body += _put(target, mode, owner, text, delim)
    if has_webhook:
        key = base + "/etc/webhook.key"
        body += [
            "# webhook/triggers: where the webhook receiver leaves a note that starts a job.",
            f"ensure_sub {_q(base)} webhook/triggers {_q(acct)} {_q(acct)} 0700",
            "# The secret that webhook senders sign with. It is made once on this server; read it with",
            f"#   sudo cat {key}",
            f"if [ ! -s {_q(key)} ]; then",
            f"  od -An -tx1 -N32 /dev/urandom | tr -d ' \\n' | put_file {_q(key)} 0640 {_q('root:' + acct)}",
            "fi",
        ]
    body += ["", "say 'Writing the system units...'"]
    for name, text in all_units.items():
        body += _put(f"{plan.units_dir}/{name}", "0644", "root:root", text, delim)
    body += [
        "# Units of this project that are no longer in the plan are switched off and removed.",
        f"KEEP={_q(keep)}",
        f"for f in {_q(plan.units_dir)}/mes-{p}.*; do",
        '  [ -e "$f" ] || continue',
        '  name="$(basename -- "$f")"',
        '  case " $KEEP " in *" $name "*) continue ;; esac',
        f"  grep -qxF -e {_q(units.MARKER_PROJECT + p)} -- \"$f\" || continue",
        '  systemctl disable --now -- "$name" >/dev/null 2>&1 || true',
        '  rm -f -- "$f"',
        '  say "Removed $name (no longer in the plan)."',
        "done",
        "systemctl daemon-reload",
    ]
    if enable:
        body.append("systemctl enable --now -- " + " ".join(_q(n) for n in enable))
    body += [
        "",
        f"say {_q(f'Done. The project {p} is set up. Check it (no sudo needed) with: mes server check {p}')}",
        f"say {_q(f'Then prove the sandboxes from your computer: mes server selftest {p} --host <server>')}",
        "",
    ]
    body_text = "\n".join(body)
    fingerprint = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    header = [
        "#!/usr/bin/env bash",
        f'# Root script for the mes project "{p}", made by \'mes server plan\' (mes {__version__}) on {when}.',
        f"# Plan fingerprint (SHA-256): {fingerprint}",
        "#",
        "# What it does, in plain words:",
        "#  1. Checks that it runs as the administrator on Linux with systemd, and that mes, its Python, uv"
        + (" and easy-ui" if t.get("easy_ui") else ""),
        "#     are installed where only the administrator can change them (every folder and link on the way).",
        "#  2. Creates two accounts that have no password and cannot use sudo:",
        f"#       {acct:<22} owns the project's code, data and state; runs its jobs and the SSH gate",
        f"#       {web:<22} runs only the dashboard server; it can read ui/ and its own auth/, nothing else",
        f"#  3. Creates {base}/ and its folders, each readable only by the account that needs it.",
        f"#  4. Writes the keys that may connect over SSH ({len(plan.keys)}; each one can only run the mes gate),",
        f"#     and the keys that may sign deploys and rollbacks ({len(plan.signers)}).",
        f"#  5. Writes the project's {len(all_units)} system units into {plan.units_dir} and switches on its",
        "#     schedules and sockets.",
        "# It touches nothing else on this server. Running it again is safe: it brings everything back",
        "# to this plan. Undo it with the script from: mes server remove " + p,
        "#",
        "# Read it, then run:   sudo bash <this file>",
        "# Afterwards check:    mes server check " + p,
        PLAN_MARKER,
    ]
    return "\n".join(header) + "\n" + body_text


def render_remove(project: str, *, server_root: str = str(paths.SERVER_ROOT), units_dir: str = UNITS_DIR,
                  delete_data: bool = False) -> str:
    p = check_server_name(project)
    lay = paths.server_layout(p, units.sandbox_path(server_root))
    base = units.sandbox_path(lay.base)
    udir = units.sandbox_path(units_dir)
    web_units = units.web_unit_names(p)
    accounts = (lay.account, lay.web_account)
    out = [
        "#!/usr/bin/env bash",
        f'# Removal script for the mes project "{p}", made by \'mes server remove\' (mes {__version__}).',
        "#",
        "# What it does, in plain words:",
        "#  1. Stops every unit of the project (also running gate requests and jobs) and then every process",
        f"#     still running as {lay.account} or {lay.web_account}.",
        "#  2. Switches off and removes the project's system units (only files marked as made by mes for",
        f"#     this project, and the dashboard server's {web_units[0]} and {web_units[1]}).",
        f"#  3. Removes the accounts {lay.account} and {lay.web_account}.",
    ]
    if delete_data:
        out.append(f"#  4. DELETES EVERYTHING in {base}, including the project's data. This cannot be undone.")
    else:
        out += ["#  4. Removes the code, the keys, the settings, the dashboard sign-in and the gate's records in",
                f"#     {base}, and KEEPS the data, state, store and backups folders (handed to root, so no new account",
                "#     can inherit them)."]
    out += [
        "# It touches nothing else. Because it removes things, it only runs with --confirm:",
        "#   sudo bash <this file> --confirm",
        "",
        _PRELUDE,
        '[ "${1:-}" = --confirm ] || die "read this script first, then run it with --confirm."',
        '[ "$(id -u)" -eq 0 ] || die "this script must be run by the administrator: sudo bash $0 --confirm"',
        "# Every unit of the project, including the instances of the gate's requests, stops first.",
        f"systemctl stop -- {_q(f'mes-{p}.*')} {_q(f'mes-{p}-web.*')} >/dev/null 2>&1 || true",
        f"for f in {_q(udir)}/mes-{p}.*; do",
        '  [ -e "$f" ] || continue',
        f"  grep -qxF -e {_q(units.MARKER_PROJECT + p)} -- \"$f\" || continue",
        '  name="$(basename -- "$f")"',
        '  systemctl disable --now -- "$name" >/dev/null 2>&1 || true',
        '  rm -f -- "$f"',
        "done",
    ]
    for name in web_units:
        out += [f"systemctl disable --now -- {_q(name)} >/dev/null 2>&1 || true", f"rm -f -- {_q(udir + '/' + name)}"]
    out += ["systemctl daemon-reload"]
    out += [
        "# Then every process still running as one of the two accounts (an SSH session, a leftover child).",
        f"for acct in {' '.join(_q(a) for a in accounts)}; do",
        '  getent passwd "$acct" >/dev/null || continue',
        '  loginctl terminate-user "$acct" >/dev/null 2>&1 || true',
        '  pkill -KILL -u "$acct" >/dev/null 2>&1 || true',
        "  for _ in 1 2 3 4 5 6 7 8 9 10; do",
        '    pgrep -u "$acct" >/dev/null 2>&1 || break',
        "    sleep 1",
        "  done",
        '  if pgrep -u "$acct" >/dev/null 2>&1; then die "processes of $acct are still running, so the account was not removed. Run this script again."; fi',
        "done",
    ]
    for account in accounts:
        comment = f"mes project {p}" if account == lay.account else f"mes dashboards of {p}"
        out += [
            f"if existing=\"$(getent passwd {_q(account)})\"; then",
            f"  [ \"$(printf '%s' \"$existing\" | cut -d: -f5)\" = {_q(comment)} ] || die {_q(f'the account {account} was not made by mes; it was left alone.')}",
            f"  userdel -- {_q(account)}",
            "fi",
            f"getent group {_q(account)} >/dev/null && groupdel -- {_q(account)} || true",
        ]
    out.append(f"rm -rf --one-file-system -- {_q(units.net_run_dir(p))}")
    out.append(f"if [ -L {_q(base)} ]; then die {_q(base + ' is a link; nothing was deleted there.')}; fi")
    if delete_data:
        out += [f"rm -rf --one-file-system -- {_q(base)}", f"say {_q(f'Removed {p} and all of its data.')}"]
    else:
        for sub in ("code", "ui", "auth", "gate", "webhook", "etc", "home", "log"):
            out.append(f"rm -rf --one-file-system -- {_q(base + '/' + sub)}")
        for sub in ("data", "state", "store", "backups"):
            out += [f"if [ -d {_q(base + '/' + sub)} ] && [ ! -L {_q(base + '/' + sub)} ]; then",
                    f"  chown -R -P root:root -- {_q(base + '/' + sub)}",
                    f"  chmod 0700 -- {_q(base + '/' + sub)}",
                    "fi"]
        out.append(f"say {_q(f'Removed {p}. Its data is kept in {base} (data, state, store, backups), owned by root.')}")
    out.append("")
    return "\n".join(out)


def render_analyze(plan: PlanInput) -> str:
    """A script (any account, no changes) that asks systemd itself about every unit of the plan."""
    names = sorted({**plan.units, **plan.web_units})
    services = []
    for n in names:
        if n.endswith("@.service"):
            services.append(n.replace("@.service", "@selftest.service"))
        elif n.endswith(".service"):
            services.append(n)
    udir = plan.units_dir
    lines = [
        "#!/usr/bin/env bash",
        f'# systemd\'s own checks of the units of "{plan.project.name}" (made by mes {__version__}).',
        "# It changes nothing and needs no sudo. Run it on the server after the root script:",
        "#   bash <this file>",
        "# 'verify' must print nothing; 'security' prints a score per service (lower is safer) and the",
        "# settings it would still add. Send both to whoever looks after mes if anything looks wrong.",
        "set -u",
        "status=0",
        f"cd {_q(udir)} || exit 1",
        "echo '== systemd-analyze verify'",
        "systemd-analyze verify --man=no -- " + " ".join(_q(f"{udir}/{n}") for n in names) + " || status=1",
    ]
    for s in services:
        lines += [f"echo {_q('== systemd-analyze security ' + s)}",
                  f"systemd-analyze security --no-pager -- {_q(s)} || status=1"]
    lines += ['exit "$status"', ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- probe


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


# --------------------------------------------------------------------------- check


def run_check(name: str, *, server_root: str = str(paths.SERVER_ROOT), units_dir: str = UNITS_DIR,
              project_dir: str | None = None, probe: ServerProbe | None = None,
              describe: bool = True) -> dict[str, Any]:
    pr = probe or ServerProbe()
    p = check_server_name(name)
    lay = paths.server_layout(p, server_root)
    base = str(lay.base)
    fix = f"mes server plan {p} --out plan.sh, then: sudo bash plan.sh"
    items: list[dict[str, Any]] = []

    def stat_of(path: str) -> tuple[os.stat_result | None, str | None]:
        try:
            return pr.lstat(path), None
        except PermissionError:
            return None, "hidden"
        except OSError:
            return None, "missing"

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
        st, why = stat_of(path)
        if st is None:
            return (None if why == "hidden" else False), why or "missing"
        if stat.S_ISLNK(st.st_mode):
            return False, "a link"
        want_uid = 0 if owner == "root" else getattr(accounts.get(owner or ""), "pw_uid", None)
        good = (want_uid is None or st.st_uid == want_uid) and stat.S_IMODE(st.st_mode) == mode
        if group is not None and group != "root":
            gid = getattr(accounts.get(group), "pw_gid", None)
            good = good and (gid is None or st.st_gid == gid)
        return good, f"mode {stat.S_IMODE(st.st_mode):04o}"

    a, w = lay.account, lay.web_account
    folders = [
        (base, "root", None, 0o711), (f"{base}/etc", "root", a, 0o750), (f"{base}/home", "root", a, 0o750),
        (f"{base}/code", a, a, 0o700), (f"{base}/data", a, a, 0o700),
        (f"{base}/state", a, a, 0o700), (f"{base}/store", a, a, 0o700),
        (f"{base}/gate", a, a, 0o700), (f"{base}/webhook", a, a, 0o700), (f"{base}/backups", a, a, 0o700),
        (f"{base}/ui", a, w, 0o2750), (f"{base}/ui/data", a, w, 0o2750), (f"{base}/ui/specs", a, w, 0o2750),
        (f"{base}/auth", w, w, 0o700),
        (f"{base}/log", "root", a, 0o750),
    ]
    bad, hidden = [], []
    for path, owner, group, mode in folders:
        good, what = owner_ok(path, owner, group, mode)
        if good is None:
            hidden.append(path)
        elif not good:
            bad.append(f"{path} ({what})")
    items.append(_item("folders", "The project's folders exist with owner-only permissions", not bad,
                       "Each folder belongs to the account that needs it, and nobody else can open it."
                       + (f" ({len(hidden)} inside folders that only the project can open were not checked.)"
                          if hidden else ""),
                       "Some folders are missing or have the wrong owner or permissions: " + "; ".join(bad[:6]), fix))
    st, _ = stat_of(f"{base}/ui/auth")
    if st is not None:
        if True:
            items.append(_item("old-auth", "No sign-in folder is left inside ui/", False, "",
                               f"{base}/ui/auth is from an older plan. The sign-in now lives in {base}/auth; the old "
                               "folder is not moved over (sign in again and set a new password).",
                               f"sudo rm -rf {base}/ui/auth"))

    for rel, title, mode in (("etc/allowed_signers", "Deploy signing keys", 0o640),
                             ("etc/gate-shell", "The gate's login shell", 0o750),
                             ("etc/server.toml", "The server settings", 0o640),
                             ("home/.ssh/authorized_keys", "The SSH keys", 0o640)):
        good, what = owner_ok(f"{base}/{rel}", "root", a, mode)
        items.append(_item(f"file-{rel.replace('/', '-').replace('.', '')}", f"{title} belong to root", good,
                           "Only the administrator can change it; only the project's account can read it.",
                           f"skipped: only root and {a} can look inside etc/ and home/ (on purpose). Run the check as "
                           "the administrator to see it." if good is None else f"It is {what} or not owned by root.",
                           fix))

    try:
        keys_text = pr.read(f"{base}/home/.ssh/authorized_keys")
    except OSError:
        keys_text = None
    if keys_text is not None:
        lines = [ln for ln in keys_text.splitlines() if ln.strip() and not ln.startswith("#")]
        forced = bool(lines) and all(_forced_key_line_ok(ln, p) for ln in lines)
        items.append(_item("keys-forced", "Every SSH key can only run the mes gate", forced,
                           f"{len(lines)} key(s), each tied to the gate with no shell, forwarding or terminal.",
                           "No keys are set up yet." if not lines else "A key line is not tied to the gate.", fix))
    try:
        signers = [ln for ln in pr.read(f"{base}/etc/allowed_signers").splitlines() if ln and not ln.startswith("#")]
        readable = True
    except OSError:
        signers, readable = [], False
    if readable:
        items.append(_item("signers", "At least one key may sign deploys", bool(signers),
                           f"{len(signers)} signing key(s).", "No signing key yet, so every deploy is refused.",
                           f"mes server plan {p} --signer <key.pub> --out plan.sh, then: sudo bash plan.sh"))

    try:
        import tomllib

        cfg = tomllib.loads(pr.read(f"{base}/etc/server.toml")).get("server", {})
    except (OSError, ValueError):
        cfg = {}
    for tool in ("mes", "mes_python", "uv", "easy_ui"):
        path = cfg.get(tool)
        if not path:
            if tool in ("easy_ui", "mes_python") or not cfg:
                continue
            items.append(_item(f"tool-{tool}", f"{tool} is installed for the server", False, "",
                               "The server settings do not name it.", fix))
            continue
        problem = _tool_chain_problem(str(path), stat_of)
        items.append(_item(f"tool-{tool}", f"{tool.replace('_', '-')} is installed where only root can change it",
                           problem is None, f"{path} and every folder and link on the way belong to root.",
                           f"{path}: {problem}.", "Install it system-wide (see: mes guide server).", detail=str(path)))

    expected: dict[str, str] | None = None
    if not describe:
        items.append(_item("units-plan", "The units can be compared with the plan", None, "",
                           "skipped: --no-describe."))
    elif os.geteuid() == 0:
        items.append(_item("units-plan", "The units can be compared with the plan", None, "",
                           "skipped: run 'mes server check' as your own account, not as root (it reads the project's "
                           "files and asks easy-ui, which root never does for a project)."))
    elif project_dir is not None or manifest_mod.find() is not None:
        try:
            plan = build_plan(project_dir, p, server_root=server_root,
                              tools={k: cfg.get(k) for k in ("mes", "mes_python", "uv", "easy_ui")},
                              units_dir=units_dir)
            expected = {**plan.units, **plan.web_units}
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
    items.append(_run_log_item(lay, p, server_root))
    st, _ = stat_of(f"{base}/code")
    items.append(_item("deployed", "A release is deployed", None, "",
                       "skipped: only the project's account can look inside its code folder. 'mes deploy <host> "
                       "--releases' shows what is installed." if st is not None else
                       "skipped: the project is not set up yet."))
    items.append(_item("selftest", "The sandboxes do what they promise", None, "",
                       f"skipped: 'mes server check' looks at files only. Prove the sandboxes with: mes server selftest {p} "
                       "--host <server>"))
    return protocol.checklist(items)


def _run_log_item(lay: paths.ServerLayout, p: str, server_root: str) -> dict[str, Any]:
    """The run log against its anchor (count and newest hash, written by root after every run)."""
    from mes import jobs

    title = "The run log is complete and unchanged"
    how = f"sudo -u {lay.account} {DEFAULT_TOOLS['mes']} jobs log --verify --project {p} --server-root {server_root}"
    try:
        result = jobs.check_head(lay.run_log, jobs.verify_log(lay.run_log))
    except PermissionError:
        return _item("run-log", title, None, "", f"skipped: only root and {lay.account} can read the run log. "
                     f"Check it with: {how}")
    except FileNotFoundError:
        return _item("run-log", title, None, "", "skipped: no job has run yet.")
    except MesError as e:
        return _item("run-log", title, False, "", e.message, "Keep the files as they are and look at this server.")
    if not result.get("anchored"):
        return _item("run-log", title, None, "", "skipped: the log has no anchor yet (no job has run since this "
                     "version of mes was set up).")
    return _item("run-log", title, True, f"{result['records']} records; the log matches its anchor.", "")


def _forced_key_line_ok(line: str, project: str) -> bool:
    """``restrict,command="<mes> gate --project <p> --allow <level> --server-root <dir>" <type> <key> <comment>``."""
    m = re.fullmatch(r'restrict,command="([^"]+)" (\S+) (\S+)(?: \S+)?', line.strip())
    if not m or m.group(2) not in LOGIN_KEY_TYPES or not _B64_RE.match(m.group(3)):
        return False
    words = m.group(1).split()
    return (len(words) == 8 and words[0].startswith("/") and words[1:4] == ["gate", "--project", project]
            and words[4] == "--allow" and words[5] in KEY_LEVELS and words[6] == "--server-root"
            and words[7].startswith("/"))


def _tool_chain_problem(path: str, stat_of) -> str | None:
    """Every folder on the way, every link, and the end belong to root and are not writable by others."""
    cur = "/"
    parts = [x for x in path.split("/") if x]
    hops = 0
    while parts:
        part = parts.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            cur = os.path.dirname(cur) or "/"
            continue
        nxt = os.path.join(cur, part)
        st, why = stat_of(nxt)
        if st is None:
            return f"{nxt} is {why or 'missing'}"
        if st.st_uid != 0:
            return f"{nxt} does not belong to root"
        if stat.S_ISLNK(st.st_mode):
            hops += 1
            if hops > 40:
                return "too many links"
            try:
                target = os.readlink(nxt)
            except OSError:
                return f"{nxt} cannot be followed"
            if target.startswith("/"):
                cur = "/"
            parts = [x for x in target.split("/") if x] + parts
            continue
        if st.st_mode & 0o022:
            return f"{nxt} can be changed by other accounts"
        cur = nxt
    return None


# --------------------------------------------------------------------------- command line


def _plan_text(plan: PlanInput, script: str, out: str | None) -> dict[str, Any]:
    p = plan.project.name
    runs = f"sudo bash {out}" if out else "save the script to a file, read it, then: sudo bash <file>"
    return {
        "project": p,
        "accounts": [plan.layout.account, plan.layout.web_account],
        "folder": str(plan.layout.base),
        "units": sorted({**plan.units, **plan.web_units}),
        "keys": [{"level": lvl, "type": kind} for lvl, kind, _ in plan.keys],
        "signers": len(plan.signers),
        "tools": plan.tools,
        "notes": plan.notes,
        "written_to": out,
        "mes_version": __version__,
        "fingerprint": plan_fingerprint(script),
        "next_steps": [
            {"step": "Read the script", "why": "It says at the top, in plain words, everything it will do."},
            {"step": "Run it once on the server, as the administrator", "command": runs,
             "why": "Only this step needs the administrator. mes itself never uses sudo."},
            {"step": "Check the result (no sudo needed)", "command": f"mes server check {p}"},
            {"step": "Deploy from your computer", "command": "mes deploy <server>"},
            {"step": "Prove the sandboxes", "command": f"mes server selftest {p} --host <server>"},
        ],
        **({} if out else {"script": script}),
    }


def _write_new(path: str, text: str) -> None:
    target = Path(path).expanduser()
    if target.exists() or target.is_symlink():
        raise Refused(f"{target} already exists, so it was not overwritten.",
                      hint="Choose another file name, or move the old one away.", code="exists")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _build_from_args(args: argparse.Namespace) -> PlanInput:
    return build_plan(args.project_dir, args.project, server_root=args.server_root,
                      tools={"mes": args.mes, "mes_python": args.mes_python, "uv": args.uv, "easy_ui": args.easy_ui},
                      key_files=args.key, signer_files=args.signer, units_dir=args.units_dir,
                      allow_hosts=args.allow_host, origins=args.origin, allow_builds=args.allow_server_builds)


def _cmd_plan(args: argparse.Namespace) -> Any:
    check_allowed("read" if args.out is None else "write")
    plan = _build_from_args(args)
    script = render_script(plan)
    if args.out:
        _write_new(args.out, script)
    result = _plan_text(plan, script, args.out)
    if args.json:
        return {**result, "sha256": hashlib.sha256(script.encode()).hexdigest()}
    if not args.out:
        return script
    lines = [f'The root script for "{plan.project.name}" is in {args.out}. Nothing was changed on any computer.',
             f"It was made by mes {__version__}; its fingerprint is {result['fingerprint']} (the script checks it)."]
    lines += [f"{i}. {s['step']}" + (f": {s['command']}" if s.get("command") else "") for i, s in
              enumerate(result["next_steps"], 1)]
    lines += [f"Please note: {n}" for n in plan.notes]
    return "\n".join(lines)


def _cmd_analyze(args: argparse.Namespace) -> Any:
    check_allowed("read" if args.out is None else "write")
    plan = _build_from_args(args)
    script = render_analyze(plan)
    if args.out:
        _write_new(args.out, script)
        msg = f"The systemd-analyze script is in {args.out}. Run it on the server (no sudo needed): bash {args.out}"
        return {"written_to": args.out, "message": msg} if args.json else msg
    return {"script": script} if args.json else script


def _cmd_check(args: argparse.Namespace) -> Any:
    check_allowed("read")
    return run_check(args.project, server_root=args.server_root, units_dir=args.units_dir,
                     project_dir=args.project_dir, describe=not args.no_describe)


def _cmd_remove(args: argparse.Namespace) -> Any:
    check_allowed("read" if args.out is None else "write")
    script = render_remove(args.project, server_root=args.server_root, units_dir=args.units_dir,
                           delete_data=args.delete_data)
    if args.out:
        _write_new(args.out, script)
        msg = (f"The removal script is in {args.out}. Read it, then run: sudo bash {args.out} --confirm. "
               + ("It DELETES the project's data." if args.delete_data else "It keeps the project's data."))
        return {"written_to": args.out, "delete_data": args.delete_data, "message": msg} if args.json else msg
    return {"script": script, "delete_data": args.delete_data} if args.json else script


def _cmd_selftest(args: argparse.Namespace) -> Any:
    check_allowed("read")
    from mes import selftest

    check_server_name(args.project)
    return selftest.run(args.project, server_root=args.server_root, host=args.host, login_key=args.login_key)


def _cmd_selftest_probe(args: argparse.Namespace) -> Any:
    from mes import selftest

    if args.context != "job":
        raise UsageError("Only the job probe runs this way.", hint="Use: mes server selftest <project>")
    layout = paths.server_layout(check_server_name(args.project), args.server_root)
    return selftest.write_job_result(layout)


def register(subparsers: Any) -> None:
    s = subparsers.add_parser(
        "server",
        help="Set a project up on a Linux server (plan a root script, check it, prove it, remove it).",
        description="mes never uses sudo: 'plan' writes one readable root script that you run once with sudo, "
        "'check' verifies the result without root, 'selftest' proves the sandboxes on the real server, "
        "'analyze' writes systemd's own checks, 'remove' writes the matching removal script. See: mes guide server",
    )
    sub = s.add_subparsers(dest="server_command", metavar="<plan|check|selftest|analyze|remove>")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("project", help="the project's name (as in project.toml)")
        p.add_argument("--server-root", default=str(paths.SERVER_ROOT), metavar="DIR",
                       help="where projects live on the server (default /var/lib/mes)")
        p.add_argument("--units-dir", default=UNITS_DIR, metavar="DIR",
                       help=f"where system units go (default {UNITS_DIR})")

    def planning(p: argparse.ArgumentParser) -> None:
        p.add_argument("--project-dir", default=None, metavar="DIR", help="the project's folder (default: here)")
        p.add_argument("--key", action="append", default=[], metavar="LEVEL=FILE",
                       help="an SSH public key that may connect: read=, write= or deploy= (repeatable); never a "
                       "signing key")
        p.add_argument("--signer", action="append", default=[], metavar="FILE",
                       help="an Ed25519 public key that may sign deploys and rollbacks (repeatable), best "
                       "hardware-backed (ed25519-sk)")
        p.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                       help="a name the dashboards are opened under, such as the server's Tailscale name; short "
                       "for --origin https://NAME (repeatable)")
        p.add_argument("--origin", action="append", default=[], metavar="URL",
                       help="the exact web address the dashboards are opened with, https://NAME[:PORT] (repeatable)")
        p.add_argument("--allow-server-builds", action="store_true",
                       help="let deploys build packages that exist only as source code on the server (off: only "
                       "ready-made wheels are installed)")
        p.add_argument("--mes", default=None, metavar="PATH", help=f"mes on the server (default {DEFAULT_TOOLS['mes']})")
        p.add_argument("--mes-python", default=None, metavar="PATH",
                       help="the Python of mes on the server (default: the one --mes names, or "
                       f"{units.DEFAULT_MES_PYTHON})")
        p.add_argument("--uv", default=None, metavar="PATH", help=f"uv on the server (default {DEFAULT_TOOLS['uv']})")
        p.add_argument("--easy-ui", dest="easy_ui", default=None, metavar="PATH",
                       help=f"easy-ui on the server (default {DEFAULT_TOOLS['easy_ui']})")

    p = sub.add_parser("plan", help="Write the root script that sets the project up on a server.")
    common(p)
    planning(p)
    p.add_argument("--out", default=None, metavar="FILE", help="write the script to FILE (owner-only) instead of printing it")
    p.set_defaults(func=_cmd_plan)

    a = sub.add_parser("analyze", help="Write a script of systemd-analyze verify/security calls for every unit.")
    common(a)
    planning(a)
    a.add_argument("--out", default=None, metavar="FILE", help="write the script to FILE instead of printing it")
    a.set_defaults(func=_cmd_analyze)

    c = sub.add_parser("check", help="Check, without root, that the server matches the plan (a checklist).")
    common(c)
    c.add_argument("--project-dir", default=None, metavar="DIR", help="the project's folder, to compare the units")
    c.add_argument("--no-describe", action="store_true",
                   help="do not compare the units (that step reads the project's files and asks easy-ui)")
    c.set_defaults(func=_cmd_check)

    t = sub.add_parser("selftest", help="Prove on the real server what each sandbox can and cannot reach.")
    common(t)
    t.add_argument("--host", default=None, metavar="NAME",
                   help="the server, to reach its gate with your deploy login key (default: localhost)")
    t.add_argument("--login-key", default=None, metavar="PATH", help="the SSH key that logs in at the deploy level")
    t.set_defaults(func=_cmd_selftest)

    pr = sub.add_parser("selftest-probe", help="(Run by the server's self-test unit.) Probe a job's sandbox.")
    pr.add_argument("project")
    pr.add_argument("--context", required=True, choices=("job",))
    pr.add_argument("--server-root", required=True, metavar="DIR")
    pr.set_defaults(func=_cmd_selftest_probe)

    r = sub.add_parser("remove", help="Write the script that removes the project from a server.")
    common(r)
    r.add_argument("--out", default=None, metavar="FILE", help="write the script to FILE instead of printing it")
    r.add_argument("--delete-data", action="store_true", help="also delete the project's data (cannot be undone)")
    r.set_defaults(func=_cmd_remove)
