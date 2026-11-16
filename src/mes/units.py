"""Rendering the systemd **system** units of a project on a server.

Every unit runs under the project's own account (``User=mes-<project>``), never as the owner or
as root, so the sandbox settings are really enforced. ``mes server plan`` puts these files into
the root script a person reads and runs once with sudo; mes itself never installs them.

Unit names use dots, which project and job names cannot contain, so two projects can never
produce the same name (``mes-fit.job.data-sync`` and ``mes-fit-data.job.sync`` differ):

- ``mes-<p>.job.<job>.service``  one run of a job (``Type=oneshot``, sandboxed).
- ``mes-<p>.job.<job>.timer``    for a job with ``schedule``.
- ``mes-<p>.job.<job>.path``     for a job with ``webhook = true``: starts it when the webhook
  receiver touches ``state/triggers/<job>``.
- ``mes-<p>.net.<job>.service``  for a job that lists network hosts: runs just before each
  start of the job, looks the hosts up again and sets the job's ``IPAddressAllow=`` (the job
  itself has ``IPAddressDeny=any``). It runs as root because only root may change another
  unit's settings; it takes nothing from the project's files (the hosts are written into this
  file) and is sandboxed too.
- ``mes-<p>.ui-refresh.service`` rebuilds the dashboard data (``easy-ui data build``) after the
  jobs listed in ``[ui] refresh_after`` succeed.
- ``mes-<p>.webhook.socket`` / ``.service``  the signed webhook receiver (see ``mes.webhook``).
- ``mes-<p>.gate-<level>.socket`` / ``mes-<p>.gate-<level>@.service`` for ``read``, ``write`` and
  ``deploy``: the gate hands each request to a fresh instance (``Accept=yes``), so every request
  runs in its own sandbox under the project's account.

A job with ``after = "<upstream>"`` is started by the upstream service through ``OnSuccess=``.
After every run, ``ExecStopPost=+`` runs ``mes jobs record`` as root, outside the sandbox, with
only the project and job names as arguments; it appends the run to ``log/runs.jsonl``, a folder
that only root can change, so a job cannot rewrite its own history.

Safety of the generated text
----------------------------
Every value written into a unit is checked or escaped here, so a project cannot add directives
of its own: names must match their strict patterns; no value may contain control characters
(a newline would start a new directive); ``ExecStart`` words are double-quoted with ``\\``,
``"``, ``%`` and ``$`` escaped; paths must be absolute, normalised and made only of
``[A-Za-z0-9._/@+=,~-]``; schedules, memory sizes, timeouts and host names are checked against
tight patterns. Anything that does not pass raises ``mes.errors.Refused`` (exit 5).

Public API
----------
``UNIT_PREFIX``, ``SLICE``, ``RANDOM_DELAY_SEC``, ``JOB_ALLOW_LEVEL`` (``"write"``),
``MARKER_PROJECT``, ``MARKER_JOB``, ``MARKER_KIND``, ``HEADER``, ``UNIT_NAME_RE``,
``JOB_UNIT_RE``, ``RUN_LOG_FILE``.
``job_unit(project, job, suffix="service")``, ``net_unit(project, job)``,
``ui_refresh_unit(project)``, ``webhook_units(project) -> (socket, service)``,
``webhook_socket_path(project) -> str``, ``gate_units(project, level) -> (socket, template)``,
``gate_socket_path(project, level) -> str``.
``quote_exec_word(value)``, ``exec_line(argv)``, ``env_assignment(key, value)``,
``sandbox_path(path)``, ``line_path(path)``, ``check_tool_path(path, what)``.
``render_job_service(project, job, *, layout, mes) -> str``
``render_job_timer(project, job) -> str``, ``render_job_path(project, job, *, layout) -> str``
``render_net_service(project, job, *, mes) -> str``
``render_ui_refresh(project, *, layout, easy_ui) -> str``
``render_webhook(project, *, layout, mes) -> dict[str, str]``
``render_gate(project, *, layout, mes) -> dict[str, str]``  the gate's per-request sandboxes.
``render_project(project, *, layout, mes, easy_ui=None) -> dict[str, str]``
    File name -> text for every mes unit of the project (not the dashboard server's own units,
    which easy-ui renders).
``read_markers(text) -> (project | None, job | None)``
``SANDBOX_LINES``  the sandbox settings shared by every unit that runs project code.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import PurePosixPath

from mes.errors import Refused
from mes.manifest import HOST_RE, JOB_NAME_RE, PROJECT_NAME_RE, Job, Project

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
UNIT_NAME_RE = re.compile(r"^mes-[a-z][a-z0-9-]{1,30}\.(?:job\.[a-z][a-z0-9-]{0,30}\.(?:service|timer|path)|"
                          r"net\.[a-z][a-z0-9-]{0,30}\.service|ui-refresh\.service|webhook\.(?:socket|service)|"
                          r"gate-(?:read|write|deploy)(?:\.socket|@\.service))\Z")
GATE_LEVELS = ("read", "write", "deploy")
JOB_UNIT_RE = re.compile(r"^mes-(?P<project>[a-z][a-z0-9-]{1,30})\.job\.(?P<job>[a-z][a-z0-9-]{0,30})\.service\Z")

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\u0085  ]")
_SAFE_PATH_RE = re.compile(r"/[A-Za-z0-9._/@+=,~-]*")
_SCHEDULE_RE = re.compile(r"[A-Za-z0-9*:,./~+_@-]+(?: [A-Za-z0-9*:,./~+_@-]+)*")
_MEMORY_RE = re.compile(r"[1-9][0-9]{0,6}[KMG]")
_TIMEOUT_RE = re.compile(r"([1-9][0-9]{0,6})([smh])")
_ENV_KEY_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_SECONDS = {"s": 1, "m": 60, "h": 3600}
# Folders a tool run by root or by a project account must never be in: other accounts can write
# there, or the units hide them (ProtectHome=yes).
UNSAFE_TOOL_ROOTS = ("/tmp", "/var/tmp", "/dev/shm", "/run/shm", "/home", "/root", "/run/user")

# The sandbox shared by every unit that runs the project's own code (jobs, the dashboard data
# refresh, the webhook receiver). Network settings and writable folders are added per unit.
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
    "SystemCallArchitectures=native",
    "SystemCallFilter=@system-service",
    "SystemCallFilter=~@privileged @resources",
    "SystemCallErrorNumber=EPERM",
    "LimitCORE=0",
    "TasksMax=256",
)


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
    if not isinstance(job, str) or not JOB_NAME_RE.fullmatch(job):
        raise _refuse("the job name", job, "it is not a simple name.",
                      "Job names use lowercase letters, digits and dashes, starting with a letter.")
    return job


def job_unit(project: str, job: str, suffix: str = "service") -> str:
    if suffix not in ("service", "timer", "path"):
        raise ValueError("suffix must be 'service', 'timer' or 'path'")
    return f"{UNIT_PREFIX}-{_check_project(project)}.job.{_check_job(job)}.{suffix}"


def net_unit(project: str, job: str) -> str:
    return f"{UNIT_PREFIX}-{_check_project(project)}.net.{_check_job(job)}.service"


def ui_refresh_unit(project: str) -> str:
    return f"{UNIT_PREFIX}-{_check_project(project)}.ui-refresh.service"


def webhook_units(project: str) -> tuple[str, str]:
    p = _check_project(project)
    return f"{UNIT_PREFIX}-{p}.webhook.socket", f"{UNIT_PREFIX}-{p}.webhook.service"


def webhook_socket_path(project: str) -> str:
    return f"/run/mes-{_check_project(project)}-webhook.sock"


def gate_units(project: str, level: str) -> tuple[str, str]:
    if level not in GATE_LEVELS:
        raise ValueError("level must be read, write or deploy")
    p = _check_project(project)
    return f"{UNIT_PREFIX}-{p}.gate-{level}.socket", f"{UNIT_PREFIX}-{p}.gate-{level}@.service"


def gate_socket_path(project: str, level: str) -> str:
    if level not in GATE_LEVELS:
        raise ValueError("level must be read, write or deploy")
    return f"/run/mes-{_check_project(project)}-gate-{level}.sock"


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
    """The full path of mes, uv or easy-ui on the server: safe characters, outside home and
    shared temporary folders (the units hide homes, and anyone can write to /tmp)."""
    p = sandbox_path(path)
    if any(p == r or p.startswith(r + "/") for r in UNSAFE_TOOL_ROOTS):
        raise _refuse(
            what, p,
            "it is inside a home folder or a shared temporary folder: the services cannot see home folders, "
            "and other accounts can change files in temporary folders.",
            "Install the tool where only the administrator can change it, for example /usr/local/bin or /opt.",
        )
    return p


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
        if not isinstance(h, str) or not HOST_RE.fullmatch(h):
            raise _refuse("the network host", h, "it is not a plain host name.",
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


def _project_env(project: Project, layout) -> list[str]:
    data = sandbox_path(layout.data)
    state = sandbox_path(layout.state)
    return [
        f"Environment={env_assignment('MES_PROJECT', _check_project(project.name))}",
        f"Environment={env_assignment('MES_DATA_DIR', data)}",
        f"Environment={env_assignment('MES_STATE_DIR', state)}",
        f"Environment={env_assignment('MES_STORE_DIR', sandbox_path(layout.store))}",
        f"Environment={env_assignment('HOME', state)}",
        f"Environment={env_assignment('LANG', 'C.UTF-8')}",
        f"Environment={env_assignment('PYTHONDONTWRITEBYTECODE', '1')}",
    ]


def _program(project: Project, layout, app: str) -> str:
    spec = project.app(app)
    return sandbox_path(f"{sandbox_path(layout.current_project)}/.venv/bin/{spec.cli}")


def render_job_service(project: Project, job: Job, *, layout, mes: str) -> str:
    p = _check_project(project.name)
    name = _check_job(job.name)
    hosts = check_hosts(job.network)
    writes = _writes(job)
    timeout = _timeout_seconds(job)
    mes = check_tool_path(mes, "the path of mes")
    root = sandbox_path(layout.root)
    start = exec_line([_program(project, layout, job.app), "--json", *job.argv])
    record = exec_line([mes, "jobs", "record", "--project", p, "--server-root", root, name])
    after_success = [job_unit(p, d.name) for d in project.dependents(job.name)]
    if project.ui is not None and job.name in project.ui.refresh_after:
        after_success.append(ui_refresh_unit(p))

    net_comment = (f"network: only {', '.join(hosts)} (their addresses are looked up again before each run)"
                   if hosts else "network: none")
    lines = _header(p, "job", name, [net_comment])
    lines += ["", "[Unit]", f"Description=mes job {name} of project {p}"]
    if hosts:
        lines += [f"Requires={net_unit(p, name)}", f"After={net_unit(p, name)}"]
    if after_success:
        lines.append("OnSuccess=" + " ".join(after_success))
    lines += [
        "",
        "[Service]",
        "Type=oneshot",
        f"User={layout.account}",
        f"Group={layout.account}",
        f"ExecStart={start}",
        f"ExecStopPost=+{record}",
        f"WorkingDirectory={line_path(layout.current_project)}",
        *_project_env(project, layout),
        f"Environment={env_assignment('MES_ALLOW', JOB_ALLOW_LEVEL)}",
        f"Environment={env_assignment('MES_JOB', name)}",
        f"Slice={SLICE}",
        f"MemoryMax={_memory(job)}",
        "MemorySwapMax=0",
        f"TimeoutStartSec={timeout}",
        *SANDBOX_LINES,
    ]
    writable = []
    for w in writes:
        writable += [sandbox_path(layout.data), sandbox_path(layout.store)] if w == "data" else [sandbox_path(layout.state)]
    if writable:
        lines.append("ReadWritePaths=" + " ".join(w.replace("%", "%%") for w in writable))
    lines.append(f"InaccessiblePaths=-{line_path(layout.webhook_key)}")
    if hosts:
        lines += ["RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", "IPAddressDeny=any"]
    else:
        lines += ["PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX", "IPAddressDeny=any"]
    lines.append("")
    return "\n".join(lines)


def render_job_timer(project: Project, job: Job) -> str:
    p = _check_project(project.name)
    name = _check_job(job.name)
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
    name = _check_job(job.name)
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


def render_net_service(project: Project, job: Job, *, mes: str) -> str:
    p = _check_project(project.name)
    name = _check_job(job.name)
    hosts = check_hosts(job.network)
    if not hosts:
        raise ValueError(f"job {name!r} lists no hosts")
    mes = check_tool_path(mes, "the path of mes")
    start = exec_line([mes, "jobs", "allow-network", "--unit", job_unit(p, name), "--", *hosts])
    lines = _header(p, "net", name, [
        "Runs as root just before each start of the job: it looks up the job's hosts and lets the",
        "job reach only those addresses (and the DNS servers). It reads nothing from the project.",
    ]) + [
        "",
        "[Unit]",
        f"Description=Allow the network hosts of mes job {name} of project {p}",
        f"Before={job_unit(p, name)}",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={start}",
        "WorkingDirectory=/",
        f"Environment={env_assignment('LANG', 'C.UTF-8')}",
        "TimeoutStartSec=60",
        "UMask=0077",
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
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service",
        "",
    ]
    return "\n".join(lines)


def render_ui_refresh(project: Project, *, layout, easy_ui: str) -> str:
    p = _check_project(project.name)
    easy_ui = check_tool_path(easy_ui, "the path of easy-ui")
    start = exec_line([easy_ui, "--json", "--project-dir", sandbox_path(layout.current_project), "data", "build"])
    lines = _header(p, "ui-refresh") + [
        "",
        "[Unit]",
        f"Description=Rebuild the dashboard data of project {p}",
        "",
        "[Service]",
        "Type=oneshot",
        f"User={layout.account}",
        f"Group={layout.account}",
        f"ExecStart={start}",
        f"WorkingDirectory={line_path(layout.current_project)}",
        *_project_env(project, layout),
        # easy-ui finds <root>/<project>/ui and /data through MES_ROOT (the server layout).
        f"Environment={env_assignment('MES_ROOT', sandbox_path(layout.root))}",
        f"Environment={env_assignment('MES_ALLOW', 'read')}",
        f"Slice={SLICE}",
        "MemoryMax=512M",
        "TimeoutStartSec=600",
        *[line for line in SANDBOX_LINES if not line.startswith("UMask=")],
        "# The dashboard server's account reads ui/ through its group, so new files are group-readable.",
        "UMask=0027",
        f"ReadWritePaths={line_path(layout.ui)}",
        f"InaccessiblePaths=-{line_path(layout.webhook_key)}",
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
    service = _header(p, "webhook") + [
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
        *SANDBOX_LINES,
        f"ReadWritePaths={line_path(layout.triggers)}",
        f"InaccessiblePaths=-{line_path(layout.data)} -{line_path(layout.store)} -{line_path(layout.code)}",
        "# No network of its own: requests arrive only through the socket.",
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "IPAddressDeny=any",
        "",
    ]
    return {socket_name: "\n".join(socket), service_name: "\n".join(service)}


def render_gate(project: Project, *, layout, mes: str, uv: str | None = None,
                easy_ui: str | None = None) -> dict[str, str]:
    """Per-request sandboxes for the gate: one socket per level (only the project's account may
    connect) and a template service that systemd starts once per connection."""
    p = _check_project(project.name)
    mes = check_tool_path(mes, "the path of mes")
    root = sandbox_path(layout.root)
    hidden = (f"InaccessiblePaths=-{line_path(f'{sandbox_path(layout.state)}/gate')} "
              f"-{line_path(layout.webhook_key)} -{line_path(layout.incoming)}")
    files: dict[str, str] = {}
    for level in GATE_LEVELS:
        socket_name, service_name = gate_units(p, level)
        socket = _header(p, f"gate-{level}", None, [
            f"Each connection gets its own sandboxed instance of {service_name}.",
            f"Only the account {layout.account} (the gate) can connect.",
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
            "Accept=yes",
            f"MaxConnections={1 if level == 'deploy' else 4}",
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
            *_project_env(project, layout),
            f"Slice={SLICE}",
            "MemoryMax=2G",
            "MemorySwapMax=0",
            f"RuntimeMaxSec={'1800' if level == 'deploy' else '3700'}",
        ]
        if level == "deploy":
            lines += [
                f"Environment={env_assignment('UV_CACHE_DIR', sandbox_path(layout.state) + '/uv-cache')}",
                f"Environment={env_assignment('UV_PYTHON_INSTALL_DIR', sandbox_path(layout.state) + '/uv-python')}",
                f"Environment={env_assignment('MES_ROOT', sandbox_path(layout.root))}",
                *[line for line in SANDBOX_LINES if not line.startswith("UMask=")],
                "# The dashboard server's account reads ui/ through its group, so new files are group-readable.",
                "UMask=0027",
                f"ReadWritePaths={line_path(layout.code)} {line_path(layout.state)} {line_path(layout.ui)}",
                hidden,
                "# Installing the project's packages (uv sync) needs the internet.",
                "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
                "",
            ]
        else:
            lines += list(SANDBOX_LINES)
            if level == "write":
                lines.append(f"ReadWritePaths={line_path(layout.data)} {line_path(layout.state)} "
                             f"{line_path(layout.store)}")
            else:
                lines.append("# Read requests see the data and state folders read-only.")
            lines += [hidden, "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX", "IPAddressDeny=any", ""]
        files[socket_name] = "\n".join(socket)
        files[service_name] = "\n".join(lines)
    return files


def render_project(project: Project, *, layout, mes: str, easy_ui: str | None = None) -> dict[str, str]:
    files: dict[str, str] = {}
    files.update(render_gate(project, layout=layout, mes=mes))
    for job in project.jobs.values():
        files[job_unit(project.name, job.name)] = render_job_service(project, job, layout=layout, mes=mes)
        if job.trigger == "schedule":
            files[job_unit(project.name, job.name, "timer")] = render_job_timer(project, job)
        if job.trigger == "webhook":
            files[job_unit(project.name, job.name, "path")] = render_job_path(project, job, layout=layout)
        if job.network:
            files[net_unit(project.name, job.name)] = render_net_service(project, job, mes=mes)
    if project.ui is not None and project.ui.refresh_after and easy_ui is not None:
        files[ui_refresh_unit(project.name)] = render_ui_refresh(project, layout=layout, easy_ui=easy_ui)
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
