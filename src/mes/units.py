"""Rendering the systemd **system** units of a project on a server.

Every unit that runs project code runs under the project's own account (``User=mes-<project>``),
never as the owner or as root, so the sandbox settings are really enforced. ``mes server plan``
puts these files into the root script a person reads and runs once with sudo; mes itself never
installs them.

Unit names use dots, which project, app and job names cannot contain, so two projects can never
produce the same name (``mes-fit.job.fit-a.sync`` and ``mes-fit-a.job.sync...`` differ). A job is
named by its id, ``<app>.<job>``:

- ``mes-<p>.job.<app>.<job>.service``  one run of a job (``Type=oneshot``, sandboxed). A job that
  may write data first runs ``mes db sync`` (``ExecStartPre``, as the project's account, with an
  empty environment and Python's isolated mode) so the shared database matches ``project.toml``.
- ``mes-<p>.job.<app>.<job>.timer``    for a job with ``schedule``.
- ``mes-<p>.job.<app>.<job>.path``     for a job with ``webhook = true``: starts it when the
  webhook receiver touches ``webhook/triggers/<app>.<job>``.
- ``mes-<p>.net.<app>.<job>.service``  for a job that lists network hosts: runs just before each
  start of the job, as root, looks the hosts up again, writes the job's own ``/etc/hosts``
  (``/run/mes-<p>-net/<app>.<job>/hosts``, bound over ``/etc/hosts`` in the job's view) and sets the
  job's ``IPAddressAllow=`` to exactly those addresses (the job itself has ``IPAddressDeny=any``
  and no access to the system's name service, so it needs no DNS server at all). It runs as
  root because only root may change another unit's settings; it takes nothing from the
  project's files (the hosts are written into this file) and is sandboxed too.
- ``mes-<p>.record.<app>.<job>.service``  records each run of the job in the run log, as root, in a
  unit of its own (see "Root's commands" below).
- ``mes-<p>.ui-refresh.service`` rebuilds the dashboard data (``easy-ui data build``) after the
  jobs listed in ``[ui] refresh_after`` succeed.
- ``mes-<p>.webhook.socket`` / ``.service``  the signed webhook receiver (see ``mes.webhook``).
- ``mes-<p>.gate-<level>.socket`` / ``mes-<p>.gate-<level>@.service`` for ``read``, ``write`` and
  ``deploy``: the gate hands each request to a fresh instance (``Accept=yes``), so every request
  runs in its own sandbox under the project's account. The sockets live in
  ``/run/mes-<p>-gate/``, a folder no job, dashboard refresh, webhook receiver or gate request
  can see (``InaccessiblePaths=``); each instance also checks that the connecting process is an
  SSH session of the project's account, never a job (see ``mes.gate``).
- ``mes-<p>.selftest.path`` / ``.service``  ``mes server selftest``: a probe that runs with
  exactly a job's sandbox and reports what it could and could not reach. Its run is recorded by
  ``mes-<p>.record.selftest.service`` and starts ``mes-<p>.selftest-after.service``, a probe with
  exactly the sandbox of a job that waits for another, so the self-test proves both for real.
- ``mes-<p>-web.socket`` / ``.service``  the dashboard server (easy-ui), rendered here from mes's
  own template (``render_web``); easy-ui only supplies checked parameters.

A job with ``after = "<upstream>"`` is started by the upstream service through ``OnSuccess=``.

Root's commands
---------------
Root runs three commands for a project, each with only names as arguments and a clean
environment (``/usr/bin/env -i PATH=/usr/bin:/bin``, Python in isolated mode ``-I``), so nothing
the project can write (its ``HOME``, its working folder, ``PYTHON*`` settings) reaches root:

- ``mes jobs record``: in the job's own record unit, started by the job unit's ``OnSuccess=`` and
  ``OnFailure=`` once a run has ended, whatever its result. It reads how the run ended from systemd
  (``systemctl show``) and appends it to ``log/runs.jsonl``, a folder only root can change, so a job
  cannot rewrite its own history. Its result is its own: a failing record shows as a failed record
  unit and never turns a job that worked into a failed one.
- ``mes jobs allow-network``: the network helper (``mes-<p>.net.<app>.<job>.service``).
- ``mes jobs should-run``: ``ExecCondition=+`` of a job that waits for another (and of the dashboard
  refresh). A condition has to run in the job's unit, so it is the one root command inside a unit
  that runs project code.

systemd applies a unit's mount namespace (``TemporaryFileSystem=``, ``BindPaths=``,
``BindReadOnlyPaths=``) to ``+`` commands too: ``+`` lifts only ``User=``, the capability bounds and
the access-restricting options (``ProtectSystem=``, ``InaccessiblePaths=``, ``ReadWritePaths=``, the
system call and address family filters). Inside a job's view ``/run`` is empty, so root's
``systemctl`` there could not reach systemd, and ``log/`` is not there at all. That is why the
record and the network helper are units of their own, and why the units with ``should-run`` get
exactly what systemctl needs: systemd's control socket (``CONTROL_SOCKET``), bound in read-only,
and an empty read-only folder at ``SYSTEMD_BOOTED``, where systemctl looks for the sign that
systemd runs the computer before it tries the socket. ``ROOT_NEEDS`` lists
what each of the three needs; ``tests/test_units.py`` checks every rendered root command against
the view its unit really gives it.

What each unit sees of ``/var/lib/mes``: ``TemporaryFileSystem=/var/lib/mes:ro`` hides every
project, then only the folders the unit needs are bound back (read-only unless it writes them).
Other projects' folders, the sign-in folder (``auth``), the gate's own folder and the webhook
receiver's folder are therefore not there at all for project code.

Safety of the generated text
----------------------------
Every value written into a unit is checked or escaped here, so a project cannot add directives
of its own: names must match their strict patterns; no value may contain control characters
(a newline would start a new directive); ``ExecStart`` words are double-quoted with ``\\``,
``"``, ``%`` and ``$`` escaped; paths must be absolute, normalised and made only of
``[A-Za-z0-9._/@+=,~-]``; schedules, memory sizes, timeouts, host names and web addresses are
checked against tight patterns. Anything that does not pass raises ``mes.errors.Refused`` (exit 5).

Public API
----------
``UNIT_PREFIX``, ``SLICE``, ``RANDOM_DELAY_SEC``, ``JOB_ALLOW_LEVEL`` (``"write"``),
``MARKER_PROJECT``, ``MARKER_JOB``, ``MARKER_KIND``, ``HEADER``, ``UNIT_NAME_RE``,
``JOB_UNIT_RE``, ``RUN_LOG_FILE``, ``DEFAULT_MES_PYTHON``, ``ROOT_ENV``, ``DEPLOY_DENY`` (the address
filter of the install step and of ``run-as --network any``), ``NETWORK_NOT_COVERED`` (what that
filter cannot block, in plain words), ``RESOLVE_SOCKET``, ``CONTROL_SOCKET``, ``SYSTEMD_BOOTED``,
``INSTALL_UV_DIR``,
``ROOT_NEEDS``, ``root_needs(argv, layout) -> dict``  what one root command needs from its view.
``job_unit(project, job_id, suffix="service")``, ``net_unit(project, job_id)``,
``record_unit(project, job_id)``, ``selftest_record_unit(project)``, ``selftest_after_unit(project)``,
``ui_refresh_unit(project)``, ``webhook_units(project) -> (socket, service)``,
``webhook_socket_path(project) -> str``, ``gate_units(project, level) -> (socket, template)``,
``gate_socket_dir(project) -> str``, ``gate_socket_path(project, level) -> str``,
``net_run_dir(project)``, ``net_hosts_file(project, job_id)``, ``selftest_units(project)``,
``web_unit_names(project)``, ``web_socket_path(project)``.
``quote_exec_word(value)``, ``exec_line(argv)``, ``env_assignment(key, value)``,
``sandbox_path(path)``, ``line_path(path)``, ``check_tool_path(path, what)``,
``root_command(mes_python, *args) -> list[str]``  how root runs mes (clean environment, ``-I``).
``account_command(mes_python, env, *args) -> list[str]``  the same for a step that runs as the
    project's account (the ``db sync`` before a job): only the listed variables, ``HOME`` nowhere.
``render_job_service(project, job, *, layout, mes, mes_python) -> str``
``render_job_timer(project, job) -> str``, ``render_job_path(project, job, *, layout) -> str``
``render_net_service(project, job, *, mes_python) -> str``
``render_record(project, job, *, layout, mes_python) -> str``  (``job=None``: the self-test probe's)
``render_ui_refresh(project, *, layout, easy_ui) -> str``
``render_webhook(project, *, layout, mes) -> dict[str, str]``
``render_gate(project, *, layout, mes, allow_builds=False) -> dict[str, str]``  the gate's per-request
    sandboxes; with ``allow_builds`` the install step gets uv folders of the release being
    installed (``install_uv_folders``) and none of the shared ones.
``render_selftest(project, *, layout, mes) -> dict[str, str]``
``check_web_params(answer, *, layout, easy_ui) -> dict``  what easy-ui says, checked.
``check_origin(value) -> bool``, ``web_exec_start(easy_ui, ui, auth, origins) -> list[str]``.
``render_web(project, *, layout, params) -> dict[str, str]``  the dashboard server's units.
``render_project(project, *, layout, mes, mes_python=DEFAULT_MES_PYTHON, easy_ui=None, allow_builds=False) -> dict[str, str]``
    File name -> text for every mes unit of the project (not the dashboard server's units,
    which ``render_web`` makes once easy-ui's parameters are known).
``interactive_command(project, layout, argv, *, network="none", addresses=()) -> list[str]``  the
    ``systemd-run`` command of ``mes server run-as`` (``network``: none, hosts or any).
``read_markers(text) -> (project | None, job | None)``
``SANDBOX_LINES``  the sandbox settings shared by every unit that runs project code.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any

from mes.errors import Refused
from mes.manifest import HOST_RE, JOB_ID_RE, PROJECT_NAME_RE, Job, Project, is_host_name

UNIT_PREFIX = "mes"
SLICE = "mes.slice"
RANDOM_DELAY_SEC = 120
TIMEOUT_GRACE_SEC = 30
JOB_ALLOW_LEVEL = "write"
RUN_LOG_FILE = "runs.jsonl"
MARKER_PROJECT = "# mes-project: "
MARKER_JOB = "# mes-job: "
MARKER_KIND = "# mes-unit: "
HEADER = ("# Made by my-easy-software (mes). Do not edit: run 'mes server plan' again and the script it "
          "writes instead.")
_JOB_ID = r"[a-z][a-z0-9-]{1,30}\.[a-z][a-z0-9-]{0,30}"
UNIT_NAME_RE = re.compile(rf"^mes-[a-z][a-z0-9-]{{1,30}}\.(?:job\.{_JOB_ID}\.(?:service|timer|path)|"
                          rf"(?:net|record)\.(?:{_JOB_ID}|selftest)\.service|ui-refresh\.service|"
                          r"webhook\.(?:socket|service)|selftest(?:-net)?\.(?:path|service)|selftest-after\.service|"
                          r"gate-(?:read|write|deploy|publish)(?:\.socket|@\.service))\Z")
# The levels an SSH key can have.
GATE_LEVELS = ("read", "write", "deploy")
# The request sandboxes the gate hands work to: the three key levels, plus "publish", which only
# the gate's own deploy steps use (no key has it).
SANDBOX_LEVELS = ("read", "write", "deploy", "publish")
# The host-list self-test probe may reach only this name, which its network helper maps to an
# address of the documentation range (RFC 5737): allowed by the filter, but routed nowhere.
SELFTEST_HOST = "selftest.invalid"
SELFTEST_ADDRESS = "192.0.2.1"
JOB_UNIT_RE = re.compile(r"^mes-(?P<project>[a-z][a-z0-9-]{1,30})\.job\.(?P<app>[a-z][a-z0-9-]{1,30})\."
                         r"(?P<job>[a-z][a-z0-9-]{0,30})\.service\Z")
# Where root's own copy of mes runs from: the Python of the root-owned, locked install
# (/opt/mes/my-easy-software, see: mes guide server). 'mes server plan' reads it from the first
# line of the mes program; --mes-python changes it.
DEFAULT_MES_PYTHON = "/opt/mes/my-easy-software/.venv/bin/python"
# Everything root runs for a project starts with an empty environment: nothing the project's
# account can write (HOME, PYTHONPATH, XDG folders, the working folder) reaches root.
ROOT_ENV = ("/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8")
# The install step (and 'mes server run-as --network any') needs the internet, but nothing on this
# computer or the local network that can be named when the unit is written: systemd's own sets for
# this computer (localhost), link-local and multicast addresses, plus the private ranges, the
# shared range Tailscale uses (100.64.0.0/10), benchmarking, reserved and IPv6 unique-local and
# local NAT64 ranges. Not covered (NETWORK_NOT_COVERED): this computer's own public addresses and
# the global IPv6 addresses of other machines on the local network, which differ per network and
# can change while the server runs.
DEPLOY_DENY = ("localhost", "link-local", "multicast", "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12",
               "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "240.0.0.0/4", "::/128", "64:ff9b:1::/48",
               "fc00::/7", "fec0::/10")
NETWORK_NOT_COVERED = ("this server's own public addresses and the global IPv6 addresses of other machines on "
                       "the local network are not blocked (they cannot be known when the unit is written)")
NETWORK_NOTE_LINES = (
    "# uv needs the package index, but nothing on this computer or the local network: this computer,",
    "# link-local, multicast, private, Tailscale (100.64.0.0/10) and IPv6 unique-local addresses are blocked.",
    "# Not covered: this server's own public addresses and the global IPv6 addresses of other machines on",
    "# the local network (they cannot be known when this file is written).",
)
# The one socket of the host the install step (and 'mes server run-as' with network) may see:
# systemd-resolved's, for name lookups.
RESOLVE_SOCKET = "/run/systemd/resolve/io.systemd.Resolve"
# systemd's own control socket: how root's systemctl reaches systemd (without D-Bus). It belongs
# to root and only root may connect (owner-only permissions), so a unit's account that can see it
# gains nothing.
CONTROL_SOCKET = "/run/systemd/private"
# Before it tries that socket, systemctl checks that systemd runs this computer by looking for
# this folder (sd_booted()); without it, it stops with "System has not been booted with systemd".
SYSTEMD_BOOTED = "/run/systemd/system"
# With server builds, uv's cache and Python of the install step live in this folder of the release.
INSTALL_UV_DIR = ".uv"
WEB_SOCKET_DIR = "/run/mes"
WEB_MEMORY_DEFAULT = "256M"
MAX_ORIGINS = 8

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\u0085  ]")
_SAFE_PATH_RE = re.compile(r"/[A-Za-z0-9._/@+=,~-]*")
_SCHEDULE_RE = re.compile(r"[A-Za-z0-9*:,./~+_@-]+(?: [A-Za-z0-9*:,./~+_@-]+)*")
_MEMORY_RE = re.compile(r"[1-9][0-9]{0,6}[KMG]")
_TIMEOUT_RE = re.compile(r"([1-9][0-9]{0,6})([smh])")
_ENV_KEY_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_ORIGIN_RE = re.compile(r"https://(?P<host>[a-z0-9.-]{1,253})(?::(?P<port>[1-9][0-9]{0,4}))?")
_SECONDS = {"s": 1, "m": 60, "h": 3600}
# Folders a tool run by root or by a project account must never be in: other accounts can write
# there, or the units hide them (ProtectHome=yes).
UNSAFE_TOOL_ROOTS = ("/tmp", "/var/tmp", "/dev/shm", "/run/shm", "/home", "/root", "/run/user")

# The sandbox shared by every unit that runs the project's own code or a tool on the project's
# files (jobs, the dashboard data refresh, the webhook receiver, the gate's requests, the
# dashboard server, the self-test probes). Network settings and folders are added per unit; only
# UMask=, SystemCallFilter= and TasksMax= may differ (see HARDENING_OVERRIDABLE).
SANDBOX_LINES = (
    "UMask=0077",
    "NoNewPrivileges=yes",
    "CapabilityBoundingSet=",
    "AmbientCapabilities=",
    "ProtectSystem=strict",
    "ProtectHome=yes",
    "PrivateTmp=yes",
    "PrivateDevices=yes",
    "DevicePolicy=closed",
    "PrivateIPC=yes",
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes",
    "ProtectClock=yes",
    "ProtectHostname=yes",
    "ProtectProc=invisible",
    "ProcSubset=pid",
    "KeyringMode=private",
    "RestrictSUIDSGID=yes",
    "LockPersonality=yes",
    "RestrictRealtime=yes",
    "RestrictNamespaces=yes",
    "RemoveIPC=yes",
    "SystemCallArchitectures=native",
    "SystemCallFilter=@system-service",
    "SystemCallFilter=~@privileged @resources",
    "SystemCallErrorNumber=EPERM",
    "LimitCORE=0",
    "TasksMax=256",
)
# Secrets of the system and of other services in /etc. What Python, TLS roots, names, accounts
# and time zones need (resolv.conf, hosts, nsswitch.conf, passwd, group, localtime, ssl/certs,
# ca-certificates) stays readable.
HIDDEN_ETC = (
    "/etc/shadow", "/etc/shadow-", "/etc/gshadow", "/etc/gshadow-", "/etc/sudoers", "/etc/sudoers.d",
    "/etc/ssh", "/etc/ssl/private", "/etc/pki/tls/private", "/etc/letsencrypt", "/etc/credstore",
    "/etc/credstore.encrypted", "/etc/NetworkManager", "/etc/wireguard", "/etc/systemd/network", "/etc/netctl",
    "/etc/wpa_supplicant", "/etc/iwd", "/etc/openvpn", "/etc/ppp", "/etc/ipsec.d", "/etc/ipsec.secrets",
    "/etc/krb5.keytab", "/etc/docker", "/etc/containers", "/etc/tailscale", "/etc/libvirt", "/etc/samba",
    "/etc/cups", "/etc/polkit-1", "/etc/security", "/etc/crypttab",
)
# What the rest of the filesystem looks like inside: /var (other services' data, and every other
# project under /var/lib/mes), /run (every service's sockets: systemd, D-Bus, resolved, userdb,
# the journal, Tailscale, Docker, containerd, Podman, nscd, CUPS, Avahi, the gate's own sockets),
# /opt and /usr/local are empty read-only folders; only /opt/mes (the tools) and the program
# folders of /usr/local come back, read-only; /srv, /mnt, /media, /boot and /efi are not there at
# all. The project's own folders are bound back per unit (_view), and a job that lists hosts gets
# its own hosts file. Output goes to the journal through the stream systemd connects before the
# sandbox is set up, so it needs no socket in /run; account lookups read /etc/passwd.
FS_LINES = (
    "TemporaryFileSystem=/var:ro /run:ro /opt:ro /usr/local:ro",
    "BindReadOnlyPaths=-/opt/mes -/usr/local/bin -/usr/local/lib -/usr/local/lib64",
    "InaccessiblePaths=-/srv -/mnt -/media -/boot -/efi",
    "InaccessiblePaths=" + " ".join(f"-{p}" for p in HIDDEN_ETC),
)
# Where the tools (mes, its Python, uv, easy-ui) may be: everything else is hidden from the units.
TOOL_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt/mes")
HARDENING_OVERRIDABLE = ("UMask=", "SystemCallFilter=", "TasksMax=")
# The install step runs uv: it may set ownership of its own files (unpacking Python) and raise
# its own open-file limit, so these two groups come back; nothing else is relaxed. To be
# confirmed on a real Linux server (mes server selftest reports it).
DEPLOY_SYSCALL_LINES = (
    "SystemCallFilter=@system-service",
    "SystemCallFilter=~@privileged",
    "SystemCallFilter=@chown",
)


def hardening(*, umask: str = "0077", syscalls: Sequence[str] | None = None, tasks: int = 256) -> list[str]:
    """The one hardening block of every unit that runs project code (see SANDBOX_LINES, FS_LINES)."""
    if not re.fullmatch(r"0[0-7]{3}", umask):
        raise ValueError(umask)
    out: list[str] = []
    first_filter = True
    for line in SANDBOX_LINES:
        if line.startswith("UMask="):
            out.append(f"UMask={umask}")
        elif line.startswith("SystemCallFilter="):
            if syscalls is None:
                out.append(line)
            elif first_filter:
                out += list(syscalls)
            first_filter = False
        elif line.startswith("TasksMax="):
            out.append(f"TasksMax={int(tasks)}")
        else:
            out.append(line)
    return out + list(FS_LINES)


def _refuse(what: str, value: object, why: str, hint: str) -> Refused:
    return Refused(
        f"mes will not write {what} {str(value)[:200]!r} into a system service file, because {why} "
        "Such a value could change how the project is protected, so nothing was rendered.",
        hint=hint,
        code="unsafe_unit_value",
    )


def _no_control(what: str, value: object) -> str:
    if not isinstance(value, str):
        raise _refuse(what, value, "it is not text.", "Use plain text for this setting.")
    if _CONTROL_RE.search(value):
        raise _refuse(
            what,
            value,
            "it contains a line break or another invisible control character.",
            "Remove line breaks and control characters from this value.",
        )
    return value


def _check_project(name: str) -> str:
    if not isinstance(name, str) or not PROJECT_NAME_RE.fullmatch(name):
        raise _refuse("the project name", name, "it is not a simple name.",
                      "Project names use 2 to 31 lowercase letters, digits and dashes, starting with a letter.")
    return name


def _check_job(job: str) -> str:
    if not isinstance(job, str) or not JOB_ID_RE.fullmatch(job):
        raise _refuse("the job id", job, "it is not <app>.<job> with simple names.",
                      "App and job names use lowercase letters, digits and dashes, starting with a letter.")
    return job


def job_unit(project: str, job: str, suffix: str = "service") -> str:
    if suffix not in ("service", "timer", "path"):
        raise ValueError("suffix must be 'service', 'timer' or 'path'")
    return f"{UNIT_PREFIX}-{_check_project(project)}.job.{_check_job(job)}.{suffix}"


def net_unit(project: str, job: str) -> str:
    return f"{UNIT_PREFIX}-{_check_project(project)}.net.{_check_job(job)}.service"


def record_unit(project: str, job: str) -> str:
    """The unit that records each run of a job (as root, after the run, in a unit of its own)."""
    return f"{UNIT_PREFIX}-{_check_project(project)}.record.{_check_job(job)}.service"


def selftest_record_unit(project: str) -> str:
    return f"{UNIT_PREFIX}-{_check_project(project)}.record.selftest.service"


def selftest_after_unit(project: str) -> str:
    """The self-test probe with the sandbox of a job that waits for another."""
    return f"{UNIT_PREFIX}-{_check_project(project)}.selftest-after.service"


def ui_refresh_unit(project: str) -> str:
    return f"{UNIT_PREFIX}-{_check_project(project)}.ui-refresh.service"


def webhook_units(project: str) -> tuple[str, str]:
    p = _check_project(project)
    return f"{UNIT_PREFIX}-{p}.webhook.socket", f"{UNIT_PREFIX}-{p}.webhook.service"


def selftest_units(project: str) -> tuple[str, str]:
    p = _check_project(project)
    return f"{UNIT_PREFIX}-{p}.selftest.path", f"{UNIT_PREFIX}-{p}.selftest.service"


def webhook_socket_path(project: str) -> str:
    return f"/run/mes-{_check_project(project)}-webhook.sock"


def gate_units(project: str, level: str) -> tuple[str, str]:
    if level not in SANDBOX_LEVELS:
        raise ValueError("level must be read, write, deploy or publish")
    p = _check_project(project)
    return f"{UNIT_PREFIX}-{p}.gate-{level}.socket", f"{UNIT_PREFIX}-{p}.gate-{level}@.service"


def gate_socket_dir(project: str) -> str:
    """The folder of the gate's sockets: hidden from every unit that runs project code."""
    return f"/run/mes-{_check_project(project)}-gate"


def gate_socket_path(project: str, level: str) -> str:
    if level not in SANDBOX_LEVELS:
        raise ValueError("level must be read, write, deploy or publish")
    return f"{gate_socket_dir(project)}/{level}.sock"


def net_run_dir(project: str) -> str:
    return f"/run/mes-{_check_project(project)}-net"


def net_hosts_file(project: str, job: str) -> str:
    return f"{net_run_dir(project)}/{_check_job(job)}/hosts"


def web_unit_names(project: str) -> tuple[str, str]:
    p = _check_project(project)
    return f"mes-{p}-web.socket", f"mes-{p}-web.service"


def web_socket_path(project: str) -> str:
    return f"{WEB_SOCKET_DIR}/{_check_project(project)}-web.sock"


# --------------------------------------------------------------------------- escaping


def quote_exec_word(value: str) -> str:
    """One ExecStart word: double-quoted, with backslash, quote, % and $ escaped."""
    _no_control("the command word", value)
    if value == ";":
        raise _refuse("the command word", value, "a lone ';' separates commands in a service file.",
                      "Remove the ';' from the job's run line.")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def exec_line(argv: Sequence[str]) -> str:
    words = list(argv)
    if not words:
        raise ValueError("argv must not be empty")
    exe = _no_control("the program path", words[0])
    if not exe.startswith("/"):
        raise _refuse("the program path", exe, "the program must be given as a full path from '/'.",
                      "Use the full path of the program.")
    return " ".join(quote_exec_word(w) for w in words)


def env_assignment(key: str, value: str) -> str:
    if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key):
        raise _refuse("the setting name", key, "it is not a simple upper-case name.", "Use A-Z, 0-9 and _.")
    _no_control(f"the value of {key}", value)
    escaped = f"{key}={value}".replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def _norm_abs(what: str, path: str | os.PathLike[str]) -> str:
    text = _no_control(what, os.fspath(path))
    if not text.startswith("/"):
        raise _refuse(what, text, "it is not a full path from '/'.", "Use a full folder path.")
    norm = str(PurePosixPath(os.path.normpath(text)))
    if norm != text.rstrip("/") and norm != text:
        raise _refuse(what, text, "it contains '..', '.' or repeated slashes.", "Use a plain full folder path.")
    return norm


def sandbox_path(path: str | os.PathLike[str]) -> str:
    """A path made only of safe characters, so it needs no quoting in any directive."""
    norm = _norm_abs("the folder", path)
    if not _SAFE_PATH_RE.fullmatch(norm):
        raise _refuse(
            "the folder",
            norm,
            "it contains spaces, quotes, backslashes or other characters that the sandbox settings "
            "cannot hold safely.",
            "Use paths made of letters, digits, '.', '_', '-' and '/'.",
        )
    return norm


def line_path(path: str | os.PathLike[str]) -> str:
    return sandbox_path(path).replace("%", "%%")


def check_tool_path(path: str | os.PathLike[str], what: str) -> str:
    """The full path of mes, its Python, uv or easy-ui on the server: safe characters, outside
    home and shared temporary folders (the units hide homes, and anyone can write to /tmp)."""
    p = sandbox_path(path)
    if any(p == r or p.startswith(r + "/") for r in UNSAFE_TOOL_ROOTS):
        raise _refuse(
            what, p,
            "it is inside a home folder or a shared temporary folder: the services cannot see home folders, "
            "and other accounts can change files in temporary folders.",
            "Install the tool where only the administrator can change it, for example /usr/local/bin or /opt "
            "(see: mes guide server).",
        )
    return p


def root_command(mes_python: str, *args: str) -> list[str]:
    """How root runs mes for a project: an empty environment and Python's isolated mode, so no
    HOME, PYTHON* setting, user site-packages or working folder of the project is used."""
    py = check_tool_path(mes_python, "the Python of mes")
    return [*ROOT_ENV, py, "-I", "-m", "mes", *args]


def account_command(mes_python: str, env: Sequence[tuple[str, str]], *args: str) -> list[str]:
    """How a step of mes itself runs as the project's account inside a unit (the ``db sync``
    before a job): the same empty environment and isolated mode as root's steps, with only the
    variables listed here. ``HOME`` and ``MES_CONFIG`` point nowhere, so nothing in a folder the
    project can write (its state folder is the job's ``HOME``) changes how mes runs."""
    py = check_tool_path(mes_python, "the Python of mes")
    words = [*ROOT_ENV, "HOME=/nonexistent", "MES_CONFIG=/nonexistent", "PYTHONDONTWRITEBYTECODE=1"]
    for key, value in env:
        if not _ENV_KEY_RE.fullmatch(key):
            raise _refuse("the environment name", key, "it is not a plain name.", "Use capital letters and _.")
        words.append(f"{key}={_no_control('an environment value', value)}")
    return [*words, py, "-I", "-m", "mes", *args]


def _schedule(job: Job) -> str:
    value = _no_control("the schedule", job.schedule)
    if len(value) > 200 or not _SCHEDULE_RE.fullmatch(value):
        raise _refuse("the schedule", value, "it is not a plain calendar expression.",
                      'Use a systemd calendar expression such as "daily" or "*-*-* 06:00:00".')
    return value


def _memory(job: Job) -> str:
    if not isinstance(job.memory, str) or not _MEMORY_RE.fullmatch(job.memory):
        raise _refuse("the memory limit", job.memory, "it is not a size such as 256M.",
                      'Write memory = "256M" in project.toml.')
    return job.memory


def _timeout_seconds(job: Job) -> int:
    m = _TIMEOUT_RE.fullmatch(job.timeout) if isinstance(job.timeout, str) else None
    if not m:
        raise _refuse("the time limit", job.timeout, "it is not a duration such as 15m.",
                      'Write timeout = "15m" in project.toml.')
    return int(m.group(1)) * _SECONDS[m.group(2)]


def check_hosts(hosts: Sequence[str]) -> list[str]:
    out = []
    for h in hosts:
        if not isinstance(h, str) or not HOST_RE.fullmatch(h) or not is_host_name(h):
            raise _refuse("the network host", h, "it is not a plain host name (numbers in any spelling are not names).",
                          'List only host names, for example network = ["api.example.com"].')
        out.append(h)
    return out


def _writes(job: Job) -> list[str]:
    out = []
    for w in job.writes:
        if w not in ("data", "state"):
            raise _refuse("the writable folder", w, 'only "data" and "state" exist.',
                          'Use writes = ["data", "state"] or a part of it.')
        out.append(w)
    return out


# --------------------------------------------------------------------------- rendering


def _header(project: str, kind: str, job: str | None = None, extra: Sequence[str] = ()) -> list[str]:
    lines = [HEADER, MARKER_PROJECT + _check_project(project), MARKER_KIND + kind]
    if job is not None:
        lines.append(MARKER_JOB + _check_job(job))
    lines += [f"# {_no_control('a comment', e)}" for e in extra]
    return lines


def _project_env(project: Project, layout, *, home: str | None = None) -> list[str]:
    data = sandbox_path(layout.data)
    state = sandbox_path(layout.state)
    return [
        f"Environment={env_assignment('MES_PROJECT', _check_project(project.name))}",
        f"Environment={env_assignment('MES_DATA_DIR', data)}",
        f"Environment={env_assignment('MES_STATE_DIR', state)}",
        f"Environment={env_assignment('MES_STORE_DIR', sandbox_path(layout.store))}",
        f"Environment={env_assignment('HOME', sandbox_path(home) if home else state)}",
        f"Environment={env_assignment('LANG', 'C.UTF-8')}",
        f"Environment={env_assignment('PYTHONDONTWRITEBYTECODE', '1')}",
    ]


def _view(layout, *, ro: Sequence[Any] = (), rw: Sequence[Any] = ()) -> list[str]:
    """What the unit sees of the server folder: /var is an empty read-only folder (FS_LINES), and
    only these folders of its own project are bound back, read-only unless listed as writable."""
    seen = {str(p) for p in (*ro, *rw)}
    absent = "other projects and the gate's own folder are not" if str(layout.auth) in seen else \
        "other projects, the sign-in folder and the gate's own folder are not"
    lines = ["# Of all the projects' folders, only these of this project are there (read-only unless listed",
             f"# as writable); {absent}."]
    if ro:
        lines.append("BindReadOnlyPaths=" + " ".join(line_path(p) for p in ro))
    if rw:
        lines.append("BindPaths=" + " ".join(line_path(p) for p in rw))
        lines.append("ReadWritePaths=" + " ".join(line_path(p) for p in rw))
    return lines


def _hidden(project: Project, layout, *extra: Any, webhook_key: bool = True, uv_cache: bool = True) -> str:
    """Inside the folders a unit sees: the webhook secret, the incoming archives and uv's cache."""
    paths_ = ([layout.webhook_key] if webhook_key else []) + [layout.incoming] + \
        ([layout.uv_cache] if uv_cache else []) + list(extra)
    return "InaccessiblePaths=" + " ".join(f"-{line_path(p)}" for p in paths_)


def install_uv_folders(layout, *, allow_builds: bool) -> tuple[str, str]:
    """uv's cache and Python folders for the install step. Without server builds they are the
    shared ``code/uv-cache`` and ``code/uv-python`` (no package code runs there); with them, folders
    of the release being installed (reached through ``code/installing``), so nothing a package's
    build code writes is used by another deploy."""
    if allow_builds:
        own = f"{sandbox_path(layout.installing)}/{INSTALL_UV_DIR}"
        return sandbox_path(f"{own}/cache"), sandbox_path(f"{own}/python")
    return sandbox_path(layout.uv_cache), sandbox_path(layout.uv_python)


def _program(project: Project, layout, app: str) -> str:
    spec = project.app(app)
    return sandbox_path(f"{sandbox_path(layout.current_project)}/.venv/bin/{spec.program}")


def selftest_net_unit(project: str) -> str:
    """The network helper of the host-list self-test probe."""
    return f"{UNIT_PREFIX}-{_check_project(project)}.net.selftest.service"


def selftest_net_units(project: str) -> tuple[str, str]:
    p = _check_project(project)
    return f"{UNIT_PREFIX}-{p}.selftest-net.path", f"{UNIT_PREFIX}-{p}.selftest-net.service"


def selftest_hosts_file(project: str) -> str:
    return f"{net_run_dir(project)}/selftest/hosts"


def should_run_line(project: str, layout, mes_python: str, upstream: Sequence[str] = (), *,
                    selftest: bool = False) -> str:
    """``ExecCondition=+``: root asks systemd how the job that started this one ended, and skips
    this run when that one only stopped for a temporary problem (exit 75, recorded as skipped).
    ``selftest``: the self-test's dependent probe, which waits for the job probe."""
    p = _check_project(project)
    base = ["jobs", "should-run", "--project", p, "--server-root", sandbox_path(layout.root)]
    if selftest:
        return exec_line(root_command(mes_python, *base, "--selftest"))
    ids = [_check_job(u) for u in upstream]
    return exec_line(root_command(mes_python, *base, "--", *ids))


def control_socket_lines() -> list[str]:
    """What a unit with ``ExecCondition=+ ... should-run`` adds to its view, for root's systemctl:
    systemd's control socket (read-only) and an empty read-only folder where systemctl looks for
    the sign that systemd runs this computer (``SYSTEMD_BOOTED``). The unit's own account cannot
    use the socket (only root may connect) and the folder is empty, so it gains nothing."""
    return ["# Root's check before the run asks systemd (systemctl) through its control socket, which the",
            f"# empty /run would hide; systemctl first looks for {SYSTEMD_BOOTED}, for which an empty",
            "# read-only folder stands in. Only root may connect to the socket, so this unit's account gains",
            "# nothing from either.",
            f"BindReadOnlyPaths={CONTROL_SOCKET}",
            f"TemporaryFileSystem={SYSTEMD_BOOTED}:ro"]


# What each command root runs for a project needs from the view systemd gives it (see "Root's
# commands" above): systemd's control socket, the folders it reads and writes, the capabilities it
# keeps in a root unit with a bounded set, and whether it needs the network. The Python of mes is
# always read as well. tests/test_units.py checks every rendered root command against this.
# systemctl needs both: the sign that systemd runs this computer, and the control socket.
_SYSTEMCTL = {"exists": (SYSTEMD_BOOTED,), "sockets": (CONTROL_SOCKET,)}
ROOT_NEEDS: dict[str, dict[str, tuple[str, ...] | bool]] = {
    # How the run ended (systemctl show), which release ran (the link code/current, in the project's
    # folder), the run log and its anchor (log/, root's; the anchor gets the log's group).
    "record": {**_SYSTEMCTL, "read": ("{current}",), "write": ("{log}",),
               "caps": ("CAP_DAC_READ_SEARCH", "CAP_CHOWN"), "network": False},
    # How the job before it ended (systemctl show); nothing else.
    "should-run": {**_SYSTEMCTL, "read": (), "write": (), "caps": (), "network": False},
    # Name lookups, the job's own hosts file and the job unit's address list (systemctl set-property).
    "allow-network": {**_SYSTEMCTL, "read": ("/etc/resolv.conf", "/etc/nsswitch.conf"),
                      "write": ("{hosts_dir}",), "caps": ("CAP_SYS_ADMIN",), "network": True},
}


def root_needs(argv: Sequence[str], layout) -> dict[str, Any]:
    """What one root command (a whole ``root_command`` argv) needs from its unit's view."""
    words = list(argv)
    i = words.index("-m")
    if words[i - 1] != "-I" or words[i + 1:i + 3] != ["mes", "jobs"] or words[i + 3] not in ROOT_NEEDS:
        raise ValueError(f"not a root command of mes: {words[i + 1:i + 4]}")
    need = ROOT_NEEDS[words[i + 3]]
    hosts = words[words.index("--hosts-file") + 1] if "--hosts-file" in words else ""
    fill = {"current": str(layout.current), "log": str(layout.log),
            "hosts_dir": str(PurePosixPath(hosts).parent) if hosts else ""}
    return {"command": words[i + 3], "python": words[i - 2],
            **{k: tuple(v.format(**fill) for v in need[k]) for k in ("exists", "sockets", "read", "write")},
            "caps": tuple(need["caps"]), "network": need["network"]}


def render_job_service(project: Project, job: Job, *, layout, mes: str,
                       mes_python: str = DEFAULT_MES_PYTHON, selftest: str | bool | None = None) -> str:
    """A job's unit. ``selftest``: one of the self-test probes, rendered with exactly the sandbox
    of a job (``job``), of a job that lists hosts (``net``) or of a job that waits for another
    (``after``, started by the ``job`` probe)."""
    p = _check_project(project.name)
    name = _check_job(job.id)
    if selftest is True:
        selftest = "job"
    if selftest not in (None, False, "job", "net", "after"):
        raise ValueError("selftest must be 'job', 'net' or 'after'")
    hosts = check_hosts(job.network)
    local = check_hosts(getattr(job, "local_network", ()))
    all_hosts = list(dict.fromkeys(hosts + local))
    writes = _writes(job)
    timeout = _timeout_seconds(job)
    mes = check_tool_path(mes, "the path of mes")
    root = sandbox_path(layout.root)
    current = sandbox_path(layout.current_project)
    pre = None
    condition = None
    upstream: list[str] = []
    record = None
    after_success: list[str] = []
    if selftest:
        start = exec_line([mes, "server", "selftest-probe", p, "--context", selftest, "--server-root", root])
        kind = "selftest"
        helper = selftest_net_unit(p)
        hosts_file = selftest_hosts_file(p)
        if selftest == "job":
            record = selftest_record_unit(p)
            after_success = [record, selftest_after_unit(p)]
        elif selftest == "after":
            condition = should_run_line(p, layout, mes_python, selftest=True)
            upstream = [selftest_units(p)[1]]
    else:
        start = exec_line([_program(project, layout, job.app), "--json", *job.argv])
        record = record_unit(p, name)
        kind = "job"
        helper = net_unit(p, name)
        hosts_file = net_hosts_file(p, name)
        if job.after:
            condition = should_run_line(p, layout, mes_python, [job.after])
            upstream = [job_unit(p, job.after)]
        if "data" in writes:
            pre = exec_line(account_command(mes_python, [
                ("MES_PROJECT", p), ("MES_DATA_DIR", sandbox_path(layout.data)),
                ("MES_STATE_DIR", sandbox_path(layout.state)), ("MES_STORE_DIR", sandbox_path(layout.store)),
                ("MES_PROJECT_DIR", current), ("MES_ALLOW", JOB_ALLOW_LEVEL),
            ], "db", "sync", "--quiet", "--project-dir", current))
        after_success = [record] + [job_unit(p, d.id) for d in project.dependents(job.id)]
        if project.ui is not None and job.id in project.ui.refresh_after:
            after_success.append(ui_refresh_unit(p))

    if all_hosts:
        net_comment = (f"network: only {', '.join(all_hosts)} (their addresses are looked up again before each run"
                       + (f"; {', '.join(local)} may be on the local network" if local else "") + ")")
    else:
        net_comment = "network: none"
    description = {"job": f"mes self-test probe of project {p} (a job's sandbox)",
                   "net": f"mes self-test probe of project {p} (the sandbox of a job that lists hosts)",
                   "after": f"mes self-test probe of project {p} (the sandbox of a job that waits for another)"}
    lines = _header(p, kind, None if selftest else name, [net_comment])
    lines += ["", "[Unit]", "Description=" + (description[selftest] if selftest else f"mes job {name} of project {p}")]
    if all_hosts:
        lines += [f"Requires={helper}", f"After={helper}"]
    if upstream:
        lines += ["# Ordering only: the job before it stays loaded, with how its run ended, until root's check",
                  "# below has read it.",
                  f"After={' '.join(upstream)}"]
    if record:
        lines += ["# Once a run has ended, whatever its result, root records it in a unit of its own: a failing",
                  "# record shows there and never changes this job's result. What waits for this job starts",
                  "# after a success."]
    if after_success:
        lines.append("OnSuccess=" + " ".join(after_success))
    if record:
        lines.append(f"OnFailure={record}")
    lines += [
        "",
        "[Service]",
        "Type=oneshot",
        f"User={layout.account}",
        f"Group={layout.account}",
    ]
    if not selftest:
        lines += ["# Exit code 75 means \"a temporary problem, try again later\": the run counts as skipped, not",
                  "# failed, and the jobs that wait for this one do not start.",
                  "SuccessExitStatus=75"]
    if condition:
        lines += ["# Root skips this run when the job before it only stopped for a temporary problem.",
                  f"ExecCondition=+{condition}"]
    if pre:
        lines += ["# First the shared database is brought in line with project.toml, by mes itself, as this",
                  "# account, with an empty environment and Python's isolated mode.",
                  f"ExecStartPre={pre}"]
    lines += [
        f"ExecStart={start}",
        f"WorkingDirectory={line_path(layout.current_project)}",
        *_project_env(project, layout),
        f"Environment={env_assignment('MES_PROJECT_DIR', current)}",
        f"Environment={env_assignment('MES_ALLOW', JOB_ALLOW_LEVEL)}",
        f"Environment={env_assignment('MES_JOB', name)}",
        f"Slice={SLICE}",
        f"MemoryMax={_memory(job)}",
        "MemorySwapMax=0",
        f"TimeoutStartSec={timeout}",
        *hardening(),
    ]
    folders = {"data": [layout.data, layout.store], "state": [layout.state]}
    rw = [f for w in writes for f in folders[w]]
    ro = [layout.code] + [f for w in ("data", "state") if w not in writes for f in folders[w]]
    lines += _view(layout, ro=ro, rw=rw)
    if selftest:
        # Its answer goes to gate/selftest/, which no job can see, so no job can forge a pass.
        answer = sandbox_path(f"{sandbox_path(layout.gate)}/selftest")
        lines += [f"BindPaths=-{line_path(answer)}"]
    if condition:
        lines += control_socket_lines()
    lines.append(_hidden(project, layout))
    if all_hosts:
        lines += [
            "# The job's own /etc/hosts, written just before the run: the only names it can look up. It has",
            "# no DNS server and no access to the system's name service.",
            f"BindReadOnlyPaths={line_path(hosts_file)}:/etc/hosts",
            "# It shares the server's network (where other programs' abstract unix sockets live), so it",
            "# cannot open unix sockets at all; socket pairs inside the job still work.",
            "RestrictAddressFamilies=AF_INET AF_INET6",
            "IPAddressDeny=any",
        ]
    else:
        lines += ["PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX", "IPAddressDeny=any"]
    lines.append("")
    return "\n".join(lines)


def render_job_timer(project: Project, job: Job) -> str:
    p = _check_project(project.name)
    name = _check_job(job.id)
    if job.trigger != "schedule" or job.schedule is None:
        raise ValueError(f"job {name!r} has no schedule")
    lines = _header(p, "timer", name) + [
        "",
        "[Unit]",
        f"Description=Schedule of mes job {name} of project {p}",
        "",
        "[Timer]",
        f"OnCalendar={_schedule(job)}",
        "Persistent=true",
        f"RandomizedDelaySec={RANDOM_DELAY_SEC}",
        f"Unit={job_unit(p, name)}",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ]
    return "\n".join(lines)


def render_job_path(project: Project, job: Job, *, layout) -> str:
    p = _check_project(project.name)
    name = _check_job(job.id)
    trigger = sandbox_path(f"{sandbox_path(layout.triggers)}/{name}")
    lines = _header(p, "webhook-trigger", name) + [
        "",
        "[Unit]",
        f"Description=Start mes job {name} of project {p} when its signed webhook arrives",
        "",
        "[Path]",
        f"PathModified={trigger.replace('%', '%%')}",
        f"Unit={job_unit(p, name)}",
        "",
        "[Install]",
        "WantedBy=paths.target",
        "",
    ]
    return "\n".join(lines)


def _net_lines(p: str, *, marker: str | None, description: str, before: str, runtime: str, start: str,
               layout) -> list[str]:
    lines = _header(p, "net", marker, [
        "Runs as root just before each start of the job, with an empty environment: it looks up the",
        "job's hosts, writes the job's own hosts file and lets the job reach only those addresses.",
        "Addresses on this computer or the local network are dropped, except for hosts listed in",
        "local_network. It reads nothing from the project.",
    ]) + [
        "",
        "[Unit]",
        f"Description={description}",
        f"Before={before}",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={start}",
        "WorkingDirectory=/",
        f"RuntimeDirectory={runtime}",
        "RuntimeDirectoryMode=0755",
        "RuntimeDirectoryPreserve=yes",
        "TimeoutStartSec=60",
        "UMask=0022",
        "NoNewPrivileges=yes",
        "# Only the right to change unit settings (systemd asks for CAP_SYS_ADMIN for that).",
        "CapabilityBoundingSet=CAP_SYS_ADMIN",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "RestrictNamespaces=yes",
        "# It needs nothing of the projects' folders.",
        f"InaccessiblePaths=-{line_path(layout.root)}" if layout is not None else "InaccessiblePaths=-/var/lib/mes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service",
        "",
    ]
    return lines


def render_net_service(project: Project, job: Job, *, mes_python: str = DEFAULT_MES_PYTHON, layout=None) -> str:
    p = _check_project(project.name)
    name = _check_job(job.id)
    hosts = check_hosts(job.network)
    local = check_hosts(getattr(job, "local_network", ()))
    if not hosts and not local:
        raise ValueError(f"job {name!r} lists no hosts")
    argv = ["jobs", "allow-network", "--unit", job_unit(p, name), "--hosts-file", net_hosts_file(p, name)]
    for h in local:
        argv += ["--local", h]
    start = exec_line(root_command(mes_python, *argv, "--", *dict.fromkeys(hosts + local)))
    return "\n".join(_net_lines(p, marker=name, description=f"Allow the network hosts of mes job {name} of project {p}",
                                before=job_unit(p, name), runtime=f"mes-{p}-net/{name}", start=start, layout=layout))


def render_selftest_net(project: Project, *, layout, mes_python: str = DEFAULT_MES_PYTHON) -> str:
    """The network helper of the host-list self-test probe: exactly a job's helper, but its one
    host (SELFTEST_HOST) is mapped to SELFTEST_ADDRESS without a lookup."""
    p = _check_project(project.name)
    probe = selftest_net_units(p)[1]
    start = exec_line(root_command(mes_python, "jobs", "allow-network", "--selftest", "--unit", probe,
                                   "--hosts-file", selftest_hosts_file(p), "--", SELFTEST_HOST))
    return "\n".join(_net_lines(p, marker=None, description=f"Allow the one test address of the self-test probe of {p}",
                                before=probe, runtime=f"mes-{p}-net/selftest", start=start, layout=layout))


def render_record(project: Project, job: Job | None, *, layout, mes_python: str = DEFAULT_MES_PYTHON) -> str:
    """The unit that records each run of a job in the run log (``job=None``: of the self-test's
    job probe). The job unit starts it through ``OnSuccess=`` and ``OnFailure=`` once a run has
    ended, so it runs as root outside the job's view, and its result never changes the job's."""
    p = _check_project(project.name)
    if job is None:
        target, args, marker = selftest_units(p)[1], ["--selftest"], None
        what = f"the self-test probe of project {p}"
    else:
        name = _check_job(job.id)
        target, args, marker = job_unit(p, name), ["--", name], name
        what = f"mes job {name} of project {p}"
    start = exec_line(root_command(mes_python, "jobs", "record", "--project", p, "--server-root",
                                   sandbox_path(layout.root), *args))
    others = [layout.data, layout.state, layout.store, layout.ui, layout.auth, layout.gate, layout.webhook,
              layout.etc, layout.home, f"{sandbox_path(layout.base)}/backups"]
    lines = _header(p, "record", marker, [
        "Runs as root after each run of the job, with an empty environment: it reads how the run ended",
        "from systemd and appends it to the run log in log/, which only root can change. It is a unit of",
        "its own, so a failing record shows here and never changes the job's result.",
    ]) + [
        "",
        "[Unit]",
        f"Description=Record each run of {what} in its run log",
        "# Ordering only: the job's unit stays loaded, with how its run ended, until this has read it.",
        f"After={target}",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={start}",
        "WorkingDirectory=/",
        "TimeoutStartSec=60",
        "UMask=0027",
        "NoNewPrivileges=yes",
        "# Only the rights to look into code/ (the project's account owns it) for the release that ran,",
        "# and to give the log's anchor the log's group, so the project's account can check the log.",
        "CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_CHOWN",
        "ProtectSystem=strict",
        f"ReadWritePaths={line_path(layout.log)}",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "RestrictNamespaces=yes",
        "# Of the project's folders it needs only log/ and the link code/current.",
        "InaccessiblePaths=" + " ".join(f"-{line_path(x)}" for x in others),
        "# systemd's control socket is a unix socket in /run; it needs no network.",
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service",
        "",
    ]
    return "\n".join(lines)


def render_ui_refresh(project: Project, *, layout, easy_ui: str, mes_python: str = DEFAULT_MES_PYTHON) -> str:
    p = _check_project(project.name)
    easy_ui = check_tool_path(easy_ui, "the path of easy-ui")
    root = sandbox_path(layout.root)
    current = sandbox_path(layout.current_project)
    start = exec_line([easy_ui, "--json", "--project-dir", current, "data", "build"])
    first = exec_line(account_command(mes_python, [
        ("MES_PROJECT", p), ("MES_DATA_DIR", sandbox_path(layout.data)), ("MES_STATE_DIR", sandbox_path(layout.state)),
        ("MES_STORE_DIR", sandbox_path(layout.store)), ("MES_PROJECT_DIR", current), ("MES_ROOT", root),
        ("MES_ALLOW", "write"),
    ], "server", "first-publish", p, "--server-root", root, "--easy-ui", easy_ui))
    upstream = list(project.ui.refresh_after) if project.ui is not None else []
    lines = _header(p, "ui-refresh") + [
        "",
        "[Unit]",
        f"Description=Rebuild the dashboard data of project {p}",
        *(["# Ordering only: the jobs it waits for stay loaded, with how their runs ended, until root's",
           "# check below has read them.",
           "After=" + " ".join(job_unit(p, u) for u in upstream)] if upstream else []),
        "",
        "[Service]",
        "Type=oneshot",
        f"User={layout.account}",
        f"Group={layout.account}",
    ]
    if upstream:
        lines += ["# Root skips the refresh when the job that started it only stopped for a temporary problem.",
                  f"ExecCondition=+{should_run_line(p, layout, mes_python, upstream)}"]
    lines += [
        f"ExecStart={start}",
        "# The first time there is data, the dashboards are also published (a deploy before the first",
        "# data could not publish them). mes asks easy-ui and publishes only when nothing is published.",
        f"ExecStartPost={first}",
        f"WorkingDirectory={line_path(layout.current_project)}",
        *_project_env(project, layout),
        # easy-ui finds <root>/<project>/ui and /data through MES_ROOT (the server layout).
        f"Environment={env_assignment('MES_ROOT', root)}",
        f"Environment={env_assignment('MES_ALLOW', 'read')}",
        f"Slice={SLICE}",
        "MemoryMax=512M",
        "TimeoutStartSec=600",
        "# The dashboard server's account reads ui/ through its group, so new files are group-readable.",
        *hardening(umask="0027"),
        *_view(layout, ro=[layout.code, layout.data, layout.state], rw=[layout.ui]),
        *(control_socket_lines() if upstream else []),
        _hidden(project, layout),
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "IPAddressDeny=any",
        "",
    ]
    return "\n".join(lines)


def render_webhook(project: Project, *, layout, mes: str) -> dict[str, str]:
    p = _check_project(project.name)
    mes = check_tool_path(mes, "the path of mes")
    socket_name, service_name = webhook_units(p)
    socket = _header(p, "webhook", None, [
        "The socket belongs to root (mode 0600): only a local proxy running as root, such as",
        "'tailscale serve', can pass requests to it. See: mes guide webhooks",
    ]) + [
        "",
        "[Unit]",
        f"Description=Signed webhooks of project {p} (socket)",
        "",
        "[Socket]",
        f"ListenStream={webhook_socket_path(p)}",
        "SocketUser=root",
        "SocketGroup=root",
        "SocketMode=0600",
        "Accept=no",
        f"Service={service_name}",
        "",
        "[Install]",
        "WantedBy=sockets.target",
        "",
    ]
    start = exec_line([mes, "webhook", "serve", "--project", p, "--server-root", sandbox_path(layout.root)])
    service = _header(p, "webhook", None, [
        "It sees only etc/ (the secret and the list of webhook jobs, both written by root) and its own",
        "webhook/ folder (the replay record and the trigger notes), which no job can write.",
    ]) + [
        "",
        "[Unit]",
        f"Description=Signed webhooks of project {p}",
        f"Requires={socket_name}",
        f"After={socket_name}",
        "",
        "[Service]",
        "Type=simple",
        f"User={layout.account}",
        f"Group={layout.account}",
        f"ExecStart={start}",
        "WorkingDirectory=/",
        *_project_env(project, layout),
        f"Slice={SLICE}",
        "MemoryMax=128M",
        "RuntimeMaxSec=3600",
        *hardening(),
        *_view(layout, ro=[layout.etc], rw=[layout.webhook]),
        "# No network of its own: requests arrive only through the socket.",
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "IPAddressDeny=any",
        "",
    ]
    return {socket_name: "\n".join(socket), service_name: "\n".join(service)}


def render_gate(project: Project, *, layout, mes: str, uv: str | None = None,
                easy_ui: str | None = None, allow_builds: bool = False) -> dict[str, str]:
    """Per-request sandboxes for the gate: one socket per level (only the project's account may
    connect, from an SSH session) and a template service that systemd starts once per connection.

    ``read`` and ``write`` run app commands (and ``read`` also asks a new release's apps to
    describe themselves); ``deploy`` only installs a new release (uv and mes's own wheel
    unpacker, no project code); ``publish`` only publishes the dashboards (no network).

    With ``allow_builds`` (``mes server plan --allow-server-builds``) package build code runs in
    the install step, so that step gets no shared cache or Python at all: uv's cache and Python
    are folders inside the release being installed, and the shared ``code/uv-cache`` and
    ``code/uv-python`` are hidden from it."""
    p = _check_project(project.name)
    mes = check_tool_path(mes, "the path of mes")
    root = sandbox_path(layout.root)
    files: dict[str, str] = {}
    for level in SANDBOX_LEVELS:
        socket_name, service_name = gate_units(p, level)
        who = (["Only the gate's own deploy steps use it (no SSH key has this level)."] if level == "publish" else [])
        socket = _header(p, f"gate-{level}", None, [
            f"Each connection gets its own sandboxed instance of {service_name}.", *who,
            f"Only the account {layout.account} can connect, and each instance accepts only an SSH",
            "session of that account (never a job). No unit that runs project code can see this socket's folder.",
        ]) + [
            "",
            "[Unit]",
            f"Description=Sandboxed {level} requests of the mes gate for project {p} (socket)",
            "",
            "[Socket]",
            f"ListenStream={gate_socket_path(p, level)}",
            f"SocketUser={layout.account}",
            f"SocketGroup={layout.account}",
            "SocketMode=0600",
            "DirectoryMode=0755",
            "Accept=yes",
            f"MaxConnections={1 if level in ('deploy', 'publish') else 4}",
            "",
            "[Install]",
            "WantedBy=sockets.target",
            "",
        ]
        start = exec_line([mes, "gate", "child", "--project", p, "--server-root", root, "--level", level])
        lines = _header(p, f"gate-{level}") + [
            "",
            "[Unit]",
            f"Description=One {level} request of the mes gate for project {p}",
            "",
            "[Service]",
            "Type=simple",
            f"User={layout.account}",
            f"Group={layout.account}",
            f"ExecStart={start}",
            "StandardInput=socket",
            "StandardOutput=socket",
            "StandardError=journal",
            "WorkingDirectory=/",
            *_project_env(project, layout, home="/nonexistent" if level in ("deploy", "publish") else None),
            f"Slice={SLICE}",
            "MemoryMax=2G",
            "MemorySwapMax=0",
            f"RuntimeMaxSec={'1800' if level == 'deploy' else '600' if level == 'publish' else '3700'}",
        ]
        if level == "deploy":
            cache, python = install_uv_folders(layout, allow_builds=allow_builds)
            if allow_builds:
                about = ["# store, gate or sign-in folder. Packages may be built here (--allow-server-builds), so their",
                         "# build code runs here: uv's cache and Python are folders of the release being installed,",
                         "# and the shared ones in code/ are not there for it."]
                writable = [layout.installing]
                hidden = _hidden(project, layout, layout.uv_python)
            else:
                about = ["# store, gate or sign-in folder. uv's cache and Python are in code/, which only this step writes."]
                writable = [layout.uv_cache, layout.uv_python, layout.installing]
                hidden = _hidden(project, layout, uv_cache=False)
            lines += [
                "# Installs a new release and nothing else: uv puts the locked third-party packages into the new",
                "# release's fresh environment, and mes unpacks the release's own wheels itself. No project code",
                "# runs here (uv never starts the new environment's Python), and it sees no data, state, ui,",
                *about,
                f"Environment={env_assignment('UV_CACHE_DIR', cache)}",
                f"Environment={env_assignment('UV_PYTHON_INSTALL_DIR', python)}",
                f"Environment={env_assignment('UV_NO_CONFIG', '1')}",
                f"Environment={env_assignment('UV_LINK_MODE', 'copy')}",
                f"Environment={env_assignment('PYTHONNOUSERSITE', '1')}",
                *hardening(syscalls=DEPLOY_SYSCALL_LINES),
                *_view(layout, ro=[layout.etc, layout.code]),
                ("# Writable: the new release only" if allow_builds else "# Writable: uv's cache and Python, and the new "
                 "release only") + ": code/installing is a link the gate",
                "# points at releases/<n> just before this step; every other release stays read-only.",
                "BindPaths=" + " ".join(f"-{line_path(x)}" for x in writable),
                hidden,
                "# Names are looked up through systemd-resolved's socket (its address 127.0.0.53 is blocked).",
                f"BindReadOnlyPaths=-{RESOLVE_SOCKET}",
                *NETWORK_NOTE_LINES,
                "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
                "IPAddressDeny=" + " ".join(DEPLOY_DENY),
                "",
            ]
        elif level == "publish":
            lines += [
                "# Publishes the dashboards of the running release: easy-ui reads the code (read-only) and",
                "# writes ui/. No network, no data, no state.",
                f"Environment={env_assignment('MES_ROOT', root)}",
                "# The dashboard server's account reads ui/ through its group, so new files are group-readable.",
                *hardening(umask="0027"),
                *_view(layout, ro=[layout.etc, layout.code], rw=[layout.ui]),
                _hidden(project, layout),
                "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX", "IPAddressDeny=any", "",
            ]
        else:
            lines += hardening()
            if level == "write":
                lines += _view(layout, ro=[layout.code], rw=[layout.data, layout.state, layout.store])
            else:
                lines.append("# Read requests see the data and state folders read-only.")
                lines += _view(layout, ro=[layout.code, layout.data, layout.state, layout.store])
            lines += [_hidden(project, layout), "PrivateNetwork=yes",
                      "RestrictAddressFamilies=AF_UNIX", "IPAddressDeny=any", ""]
        files[socket_name] = "\n".join(socket)
        files[service_name] = "\n".join(lines)
    return files


def _selftest_path(p: str, path_name: str, service_name: str, request: str, what: str) -> str:
    return "\n".join(_header(p, "selftest", None, ["Started by 'mes server selftest' through the gate."]) + [
        "",
        "[Unit]",
        f"Description=Start the mes self-test probe of project {p} ({what})",
        "",
        "[Path]",
        f"PathModified={request.replace('%', '%%')}",
        f"Unit={service_name}",
        "",
        "[Install]",
        "WantedBy=paths.target",
        "",
    ])


def render_selftest(project: Project, *, layout, mes: str, mes_python: str = DEFAULT_MES_PYTHON) -> dict[str, str]:
    """``mes server selftest``: the gate asks for a run by touching ``gate/selftest-request`` (or
    ``gate/selftest-net-request``), in a folder only the gate itself can write; each probe runs
    with a job's exact sandbox and leaves its answer in ``gate/selftest/``, the only part of gate/
    it sees. One probe is a job without network; the other is a job that lists one host
    (SELFTEST_HOST, mapped to a documentation address that leads nowhere), like real jobs with
    ``network = [...]``. After the first probe, root records its run (``record.selftest``) and
    starts a third probe with the sandbox of a job that waits for another (``selftest-after``),
    whose root check asks systemd how the first one ended, exactly like a real ``after`` job."""
    p = _check_project(project.name)
    path_name, service_name = selftest_units(p)
    net_path, net_service = selftest_net_units(p)
    app = next(iter(project.apps))
    probe = Job(id=f"{app}.selftest", name="selftest", app=app, run="selftest", argv=("selftest",),
                trigger="manual", memory="128M", timeout="2m", network=(), writes=("data", "state"))
    net_probe = Job(id=f"{app}.selftest", name="selftest", app=app, run="selftest", argv=("selftest",),
                    trigger="manual", memory="128M", timeout="2m", network=(SELFTEST_HOST,), writes=("data", "state"))
    after_probe = Job(id=f"{app}.selftest", name="selftest", app=app, run="selftest", argv=("selftest",),
                      trigger="after", after=f"{app}.selftest", memory="128M", timeout="2m", network=(),
                      writes=("data", "state"))
    gate = sandbox_path(layout.gate)
    return {
        path_name: _selftest_path(p, path_name, service_name, sandbox_path(f"{gate}/selftest-request"), "a job"),
        service_name: render_job_service(project, probe, layout=layout, mes=mes, mes_python=mes_python, selftest="job"),
        net_path: _selftest_path(p, net_path, net_service, sandbox_path(f"{gate}/selftest-net-request"),
                                 "a job that lists hosts"),
        net_service: render_job_service(project, net_probe, layout=layout, mes=mes, mes_python=mes_python,
                                        selftest="net"),
        selftest_net_unit(p): render_selftest_net(project, layout=layout, mes_python=mes_python),
        selftest_after_unit(p): render_job_service(project, after_probe, layout=layout, mes=mes, mes_python=mes_python,
                                                   selftest="after"),
        selftest_record_unit(p): render_record(project, None, layout=layout, mes_python=mes_python),
    }


def interactive_command(project: Project, layout, argv: Sequence[str], *, network: str = "none",
                        addresses: Sequence[str] = ()) -> list[str]:
    """``mes server run-as``: the ``systemd-run --pty`` command (for the owner to run with sudo)
    that runs one app command that needs a terminal, such as a login, as the project's account in
    a transient unit with the same hardening block as a job: data, state and store writable, the
    code read-only, nothing else of the server. mes only prints it; it never runs sudo.

    ``network``: ``"none"`` (the default) no network at all and no name lookups; ``"hosts"`` only
    ``addresses`` (internet addresses the caller looked up for the hosts it was given), names
    through systemd-resolved's socket; ``"any"`` the internet except what ``DEPLOY_DENY`` blocks
    (see ``NETWORK_NOT_COVERED`` for what that does not cover)."""
    import ipaddress

    p = _check_project(project.name)
    if not argv:
        raise ValueError("argv must not be empty")
    if network not in ("none", "hosts", "any"):
        raise ValueError(network)
    allowed: list[str] = []
    for a in addresses:
        addr = ipaddress.ip_address(str(a))
        if not addr.is_global or addr.is_multicast:
            raise _refuse("the address", a, "only internet addresses may be allowed.", "Name internet hosts.")
        allowed.append(str(addr))
    if (network == "hosts") != bool(allowed):
        raise ValueError("addresses go with network='hosts' only")
    program = _program(project, layout, next(a.name for a in project.apps.values() if a.program == argv[0]))
    for w in argv:
        _no_control("the command word", w)
    props = [
        f"User={layout.account}", f"Group={layout.account}",
        f"WorkingDirectory={sandbox_path(layout.current_project)}",
        f"Environment=MES_PROJECT={p}", f"Environment=MES_DATA_DIR={sandbox_path(layout.data)}",
        f"Environment=MES_STATE_DIR={sandbox_path(layout.state)}", f"Environment=MES_STORE_DIR={sandbox_path(layout.store)}",
        f"Environment=MES_PROJECT_DIR={sandbox_path(layout.current_project)}", f"Environment=HOME={sandbox_path(layout.state)}",
        "Environment=LANG=C.UTF-8", "Environment=PYTHONDONTWRITEBYTECODE=1",
        "MemoryMax=512M", "MemorySwapMax=0",
        *hardening(),
        *[ln for ln in _view(layout, ro=[layout.code], rw=[layout.data, layout.store, layout.state]) if not ln.startswith("#")],
        _hidden(project, layout),
    ]
    if network == "any":
        props += [f"BindReadOnlyPaths=-{RESOLVE_SOCKET}",
                  "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", "IPAddressDeny=" + " ".join(DEPLOY_DENY)]
    elif network == "hosts":
        props += [f"BindReadOnlyPaths=-{RESOLVE_SOCKET}", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
                  "IPAddressDeny=any", "IPAddressAllow=" + " ".join(dict.fromkeys(allowed))]
    else:
        # No network and no resolver socket, so not even names on the local network or the tailnet
        # can be looked up.
        props += ["PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX", "IPAddressDeny=any"]
    out = ["systemd-run", "--pty", "--wait", "--collect", "--quiet", f"--slice={SLICE}",
           f"--unit=mes-{p}.run-as-{os.getpid()}"]
    for prop in props:
        out += ["-p", prop]
    return [*out, "--", program, *argv[1:]]


# --------------------------------------------------------------------------- the dashboard server


def _bad_web(what: str, value: Any) -> Refused:
    return Refused(
        f"easy-ui gave a value for the dashboard server that mes does not accept: {what} "
        f"({str(value)[:120]!r}). mes renders the dashboard server's units itself and only takes plain, "
        "checked values from easy-ui, so nothing was written.",
        hint="Update easy-ui (and mes), then run 'mes server plan' again.",
        code="unsafe_web_unit",
    )


def check_web_params(answer: Any, *, layout, easy_ui: str, origins: Sequence[str] | None = None) -> dict[str, Any]:
    """The parameters ``easy-ui units --json`` gives for the dashboard server, each checked
    against a strict pattern and against what mes itself expects for this project. Nothing else
    of easy-ui's answer (in particular no unit text) is ever used."""
    if not isinstance(answer, dict):
        raise _bad_web("the answer", type(answer).__name__)
    p = _check_project(layout.project)
    root = sandbox_path(layout.root)
    accounts = answer.get("accounts")
    if accounts != {"project": layout.account, "web": layout.web_account}:
        raise _bad_web("the accounts", accounts)
    for key, want in (("ui_dir", f"{root}/{p}/ui"), ("auth_dir", f"{root}/{p}/auth"),
                      ("socket", web_socket_path(p))):
        if answer.get(key) != want:
            raise _bad_web(key, answer.get(key))
    if answer.get("root", root) != root:
        raise _bad_web("the folder of all projects", answer.get("root"))
    if answer.get("unit_names", {"socket": web_unit_names(p)[0], "service": web_unit_names(p)[1]}) != \
            {"socket": web_unit_names(p)[0], "service": web_unit_names(p)[1]}:
        raise _bad_web("the unit names", answer.get("unit_names"))
    if answer.get("socket_owner", "root") != "root":
        raise _bad_web("the socket's owner", answer.get("socket_owner"))
    exe = answer.get("exec", easy_ui)
    if exe != easy_ui:
        raise _bad_web("the program", exe)
    memory = answer.get("memory_max", WEB_MEMORY_DEFAULT)
    if not isinstance(memory, str) or not _MEMORY_RE.fullmatch(memory):
        raise _bad_web("the memory limit", memory)
    requested = list(origins) if origins is not None else None
    origins = answer.get("origins")
    if not isinstance(origins, list) or not 1 <= len(origins) <= MAX_ORIGINS:
        raise _bad_web("the web addresses", origins)
    for o in origins:
        if not check_origin(o):
            raise _bad_web("a web address", o)
    origins = list(dict.fromkeys(origins))
    if requested is not None and origins != list(dict.fromkeys(requested)):
        raise _bad_web("the web addresses (not the ones asked for)", origins)
    if "exec_start" in answer and answer["exec_start"] != web_exec_start(easy_ui, f"{root}/{p}/ui",
                                                                          f"{root}/{p}/auth", origins):
        raise _bad_web("the command line", answer.get("exec_start"))
    return {"exec": check_tool_path(easy_ui, "the path of easy-ui"), "ui_dir": f"{root}/{p}/ui",
            "auth_dir": f"{root}/{p}/auth", "socket": web_socket_path(p), "memory_max": memory,
            "origins": origins}


def check_origin(value: Any) -> bool:
    """``https://NAME[:PORT]``: exactly the address the dashboards are opened with."""
    m = _ORIGIN_RE.fullmatch(value) if isinstance(value, str) else None
    return bool(m and HOST_RE.fullmatch(m.group("host")) and (not m.group("port") or int(m.group("port")) <= 65535))


def web_exec_start(easy_ui: str, ui: str, auth: str, origins: Sequence[str]) -> list[str]:
    argv = [easy_ui, "serve", "--ui-dir", ui, "--auth-dir", auth]
    for o in origins:
        argv += ["--origin", o]
    return argv


def render_web(project: Project, *, layout, params: dict[str, Any]) -> dict[str, str]:
    """The dashboard server's socket and service, from mes's own template."""
    p = _check_project(project.name)
    socket_name, service_name = web_unit_names(p)
    exe = check_tool_path(params["exec"], "the path of easy-ui")
    ui, auth = sandbox_path(params["ui_dir"]), sandbox_path(params["auth_dir"])
    if ui != sandbox_path(layout.ui) or auth != sandbox_path(layout.auth) or params["socket"] != web_socket_path(p):
        raise _bad_web("the folders", params)
    if not _MEMORY_RE.fullmatch(params["memory_max"]):
        raise _bad_web("the memory limit", params["memory_max"])
    for o in params["origins"]:
        if not check_origin(o):
            raise _bad_web("a web address", o)
    argv = web_exec_start(exe, ui, auth, params["origins"])
    socket = _header(p, "web", None, [
        "The dashboard server's socket belongs to root (mode 0600): only a local proxy running as root,",
        "such as 'tailscale serve', can pass requests to it. See: mes guide setup",
    ]) + [
        "",
        "[Unit]",
        f"Description=Dashboards of project {p} (socket)",
        "",
        "[Socket]",
        f"ListenStream={web_socket_path(p)}",
        "SocketUser=root",
        "SocketGroup=root",
        "SocketMode=0600",
        "DirectoryMode=0711",
        "RemoveOnStop=yes",
        "Accept=no",
        "",
        "[Install]",
        "WantedBy=sockets.target",
        "",
    ]
    service = _header(p, "web", None, [
        f"Runs easy-ui as {layout.web_account}: it sees only ui/ (read-only) and auth/ (the sign-in),",
        "has no network of its own and no way to gain privileges. Rendered by mes from its own template.",
    ]) + [
        "",
        "[Unit]",
        f"Description=Dashboards of project {p}",
        f"Requires={socket_name}",
        f"After={socket_name}",
        "",
        "[Service]",
        "Type=simple",
        f"User={layout.web_account}",
        f"Group={layout.web_account}",
        f"ExecStart={exec_line(argv)}",
        "WorkingDirectory=/",
        f"Environment={env_assignment('HOME', '/nonexistent')}",
        f"Environment={env_assignment('MES_CONFIG', '/nonexistent')}",
        f"Environment={env_assignment('PYTHONNOUSERSITE', '1')}",
        "Restart=on-failure",
        "RestartSec=2",
        f"MemoryMax={params['memory_max']}",
        "MemoryDenyWriteExecute=yes",
        *hardening(tasks=64),
        "LimitNOFILE=1024",
        *_view(layout, ro=[layout.ui], rw=[layout.auth]),
        _hidden(project, layout),
        "# No network of its own: requests arrive only through the socket.",
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "IPAddressDeny=any",
        "",
    ]
    return {socket_name: "\n".join(socket), service_name: "\n".join(service)}


def render_project(project: Project, *, layout, mes: str, mes_python: str = DEFAULT_MES_PYTHON,
                   easy_ui: str | None = None, allow_builds: bool = False) -> dict[str, str]:
    check_tool_path(mes_python, "the Python of mes")
    files: dict[str, str] = {}
    files.update(render_gate(project, layout=layout, mes=mes, allow_builds=allow_builds))
    files.update(render_selftest(project, layout=layout, mes=mes, mes_python=mes_python))
    for job in project.jobs.values():
        files[job_unit(project.name, job.id)] = render_job_service(project, job, layout=layout, mes=mes,
                                                                   mes_python=mes_python)
        files[record_unit(project.name, job.id)] = render_record(project, job, layout=layout, mes_python=mes_python)
        if job.trigger == "schedule":
            files[job_unit(project.name, job.id, "timer")] = render_job_timer(project, job)
        if job.trigger == "webhook":
            files[job_unit(project.name, job.id, "path")] = render_job_path(project, job, layout=layout)
        if job.network or getattr(job, "local_network", ()):
            files[net_unit(project.name, job.id)] = render_net_service(project, job, mes_python=mes_python,
                                                                          layout=layout)
    if project.ui is not None and project.ui.refresh_after and easy_ui is not None:
        files[ui_refresh_unit(project.name)] = render_ui_refresh(project, layout=layout, easy_ui=easy_ui,
                                                                 mes_python=mes_python)
    if any(j.trigger == "webhook" for j in project.jobs.values()):
        files.update(render_webhook(project, layout=layout, mes=mes))
    for name in files:
        assert UNIT_NAME_RE.fullmatch(name), name
    return files


def read_markers(text: str) -> tuple[str | None, str | None]:
    project = job = None
    for line in text.splitlines()[:8]:
        if line.startswith(MARKER_PROJECT):
            project = line[len(MARKER_PROJECT):].strip()
        elif line.startswith(MARKER_JOB):
            job = line[len(MARKER_JOB):].strip()
    return project, job
