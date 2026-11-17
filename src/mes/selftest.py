"""``mes server selftest <project>``: prove, on the real server, what the sandboxes promise.

Most of the isolation mes sets up is enforced by systemd and the kernel, which no test on a
personal computer can show. The self-test asks the real units, after setup, and reports one
plain pass or fail per claim:

- From the account that runs ``mes server selftest`` (any account, no root): it cannot open the
  project's data, state, code, sign-in or gate folders, nor connect to the gate's sockets.
- Inside each request sandbox (read, write, deploy, publish), reached through the gate with the
  deploy login key like a deploy (``["mes", "selftest"]``), and inside two job sandboxes: a job
  without network (``mes-<p>.selftest.service``) and a job that lists one host
  (``mes-<p>.selftest-net.service``, rendered exactly like a real job with ``network = [...]``,
  whose one host is mapped to a documentation address that leads nowhere), each started by its
  ``.path`` unit when the gate touches ``gate/selftest-request`` or ``gate/selftest-net-request``.
  A probe tries what must fail and what must work: other projects' folders, the sign-in folder,
  the gate's folder, the webhook secret, writing code/ (only the new release, from the deploy
  sandbox), writing data (not from a read request), the network (none, except the package index
  for the install step, never this computer or the local network; for the host-list job only its
  one address, with names only from its own hosts file), the host's service sockets (systemd,
  D-Bus, resolved, Tailscale, Docker, ...), the gate's deploy socket, privileges, other accounts'
  processes, and the system calls uv needs in the deploy sandbox.
- From the gate's side: that it refuses to run anything without the request sandboxes
  (``REQUIRE_SANDBOX_ON_SERVER`` is on), and what systemd says the host-list probe's address
  filter is (``IPAddressAllow``/``IPAddressDeny``).

A claim that cannot be checked from where the probe runs is reported as ``skipped`` with the
reason, never as passed.

Public API
----------
``probe(layout, context) -> dict``     one sandbox's checklist (``context``: job, read, write, deploy).
``run_inside(where, run_child) -> dict``  the gate's side: every sandbox, plus the job probe.
``outside(layout) -> list[dict]``      the checks from the calling account.
``run(project, *, server_root, host, login_key) -> dict``  the whole checklist (``mes server selftest``).
``write_job_result(layout)``            what ``mes-<p>.selftest.service`` runs.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from mes import paths, protocol, units

JOB_RESULT = "job.json"
NET_RESULT = "net.json"
REQUEST_FILE = "selftest-request"
NET_REQUEST_FILE = "selftest-net-request"
# Service sockets and name-service paths of the host that no sandbox may see.
HOST_SOCKETS = ("/run/systemd/resolve", "/run/dbus", "/run/systemd/private", "/run/systemd/userdb",
                "/run/systemd/journal/stdout", "/run/systemd/journal/socket", "/run/systemd/notify",
                "/run/tailscale", "/var/run/tailscale", "/run/docker.sock", "/var/run/docker.sock",
                "/run/containerd", "/run/podman", "/run/user", "/run/nscd", "/var/run/nscd", "/run/cups",
                "/run/avahi-daemon", "/run/mes")
_LOCAL = (("127.0.0.1", 22), ("10.0.0.1", 80), ("172.16.0.1", 80), ("192.168.1.1", 80), ("169.254.169.254", 80),
          ("::1", 22))
JOB_WAIT_SEC = 90.0
CONNECT_TIMEOUT = 3.0
_PUBLIC = (("1.1.1.1", 443), ("8.8.8.8", 53))


def _item(id: str, title: str, ok: bool | None, why_ok: str, why_not: str, detail: str | None = None) -> dict[str, Any]:
    if ok is None:
        return protocol.item(id, title, "skipped", why_not, detail=detail)
    return protocol.item(id, title, "done" if ok else "todo", why_ok if ok else why_not, detail=detail)


def _fails(action) -> tuple[bool, str]:
    """(True, what happened) when ``action`` raised an OSError, which is what a check wants."""
    try:
        action()
    except OSError as e:
        return True, errno.errorcode.get(e.errno or 0, type(e).__name__)
    return False, "it worked"


def _connect(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.settimeout(CONNECT_TIMEOUT)
        s.connect((host, port))


def _connect_unix(path: str) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(CONNECT_TIMEOUT)
        s.connect(path)


def _try_write(folder: Path) -> None:
    target = folder / f".mes-selftest-{os.getpid()}"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    os.unlink(target)


def _status_field(name: str) -> str | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(name + ":"):
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def probe(layout: paths.ServerLayout, context: str) -> dict[str, Any]:
    """What this sandbox can and cannot do. ``context`` is where the probe runs."""
    if context not in ("job", "net", "read", "write", "deploy", "publish"):
        raise ValueError(context)
    where = {"job": "a job", "net": "a job that lists hosts", "read": "a read request", "write": "a write request",
             "deploy": "the install step", "publish": "the publish step"}[context]
    p = layout.project
    items: list[dict[str, Any]] = []
    pre = f"{context}-"

    try:
        seen = sorted(os.listdir(layout.root))
    except OSError as e:
        seen = [f"(cannot list: {errno.errorcode.get(e.errno or 0, '?')})"]
    items.append(_item(pre + "projects", f"{where} sees no other project", seen in ([p], []) or seen[0].startswith("(cannot"),
                       f"Only its own project is in {layout.root}.",
                       f"It sees other folders in {layout.root}: {', '.join(seen[:6])}.", detail=", ".join(seen[:10])))
    ok, what = _fails(lambda: os.listdir(layout.auth))
    items.append(_item(pre + "auth", f"{where} cannot read the dashboard sign-in", ok,
                       f"The sign-in folder is not there for it ({what}).", "It can list the sign-in folder."))
    ok, what = _fails(lambda: _try_write(layout.gate))
    items.append(_item(pre + "gate", f"{where} cannot change the gate's log and records", ok,
                       f"The gate's folder cannot be written ({what}).", "It could write into the gate's folder."))
    ok, what = _fails(lambda: open(layout.webhook_key, "rb").close())
    items.append(_item(pre + "webhook-key", f"{where} cannot read the webhook secret", ok,
                       f"The secret is not readable ({what}).", "It can read the webhook secret."))
    ok, what = _fails(lambda: _try_write(layout.code))
    if context == "deploy":
        items.append(_item(pre + "code", "the install step cannot change code/ itself", ok,
                           f"code/ is read-only for it ({what}).", "It can write code/, so it could change other releases "
                           "or the 'current' link."))
        items += _install_target(layout)
    else:
        items.append(_item(pre + "code", f"{where} cannot change the installed code", ok,
                           f"code/ is read-only for it ({what}).", "It can write into code/, so it could change what runs."))
    if context == "read":
        ok, what = _fails(lambda: _try_write(layout.data))
        items.append(_item(pre + "data", "a read request cannot change the data", ok,
                           f"data/ is read-only for it ({what}).", "It can write into data/."))
    ok, what = _fails(lambda: _connect_unix(units.gate_socket_path(p, "deploy")))
    items.append(_item(pre + "deploy-socket", f"{where} cannot reach the gate's deploy socket", ok,
                       f"The socket cannot be reached ({what}).", "It can connect to the deploy socket."))
    if context in ("deploy", "publish", "job", "net"):
        for name, folder in (("data", layout.data), ("state", layout.state)):
            if context in ("deploy", "publish"):
                ok, what = _fails(lambda f=folder: os.listdir(f))
                items.append(_item(pre + f"no-{name}", f"{where} cannot see the {name} folder", ok,
                                   f"{name}/ is not there for it ({what}).", f"It can list {name}/."))
    hidden = [p for p in HOST_SOCKETS if os.path.lexists(p)]
    items.append(_item(pre + "host-sockets", f"{where} cannot see the server's service sockets", not hidden,
                       "None of systemd's, D-Bus's, the name service's, Tailscale's or the container tools' sockets "
                       "is there.", "It can see: " + ", ".join(hidden[:8]) + "."))
    if context == "deploy":
        ok, what = _fails(lambda: _connect("127.0.0.1", 22))
        items.append(_item(pre + "loopback", "the install step cannot reach this computer's own services", ok,
                           f"127.0.0.1 is blocked ({what}).", "It can connect to 127.0.0.1."))
        ok, what = _fails(lambda: _connect("192.168.1.1", 80))
        items.append(_item(pre + "lan", "the install step cannot reach the local network", ok,
                           f"192.168.0.0/16 is blocked ({what}).", "It can connect to 192.168.1.1."))
        items.append(_uv_syscalls(layout))
    elif context == "net":
        items += _net_checks(pre)
    else:
        results = [_fails(lambda h=h, port=port: _connect(h, port)) for h, port in _PUBLIC]
        ok = all(r[0] for r in results)
        items.append(_item(pre + "network", f"{where} has no network", ok,
                           f"Nothing on the internet can be reached ({', '.join(r[1] for r in results)}).",
                           "It can reach the internet."))
    euid = os.geteuid()
    nnp = _status_field("NoNewPrivs")
    cap = _status_field("CapEff")
    items.append(_item(pre + "privileges", f"{where} runs without privileges", euid != 0 and nnp in ("1", None)
                       and (cap is None or int(cap, 16) == 0),
                       f"Not root (uid {euid}), no new privileges, no capabilities.",
                       f"uid {euid}, NoNewPrivs {nnp}, capabilities {cap}."))
    items.append(_item(pre + "processes", f"{where} cannot see other accounts' processes",
                       None if not os.path.isdir("/proc/self") else not os.path.exists("/proc/1"),
                       "The system's first process (root's) is invisible.", "skipped: /proc is not there."
                       if not os.path.isdir("/proc/self") else "It can see root's processes."))
    return {"context": context, "items": items}


def _install_target(layout: paths.ServerLayout) -> list[dict[str, Any]]:
    """The install step may write only the release the gate points code/installing at (for the
    self-test, an empty folder the gate made), and uv's cache."""
    items = []
    ok, what = _fails(lambda: _try_write(layout.installing))
    items.append(_item("deploy-new-release", "the install step can write the new release", not ok,
                       "It can write the release that code/installing points at, as installing needs.",
                       f"It cannot write it ({what}), so deploys fail. (systemd must follow the link code/installing "
                       "when it sets up the sandbox.)"))
    ok, what = _fails(lambda: _try_write(layout.releases))
    items.append(_item("deploy-other-releases", "the install step cannot change the other releases",
                       ok if layout.releases.is_dir() else None,
                       f"code/releases/ is read-only for it ({what}).", "It can write code/releases/."
                       if layout.releases.is_dir() else "skipped: code/releases/ is not there."))
    ok, what = _fails(lambda: _try_write(layout.uv_cache))
    items.append(_item("deploy-uv-cache", "the install step can use uv's cache", not ok,
                       "It can write uv's cache.", f"It cannot write uv's cache ({what})."))
    return items


def _errno_of(action) -> str | None:
    try:
        action()
    except OSError as e:
        return errno.errorcode.get(e.errno or 0, "timeout" if isinstance(e, TimeoutError) else type(e).__name__)
    return None


def _net_checks(pre: str) -> list[dict[str, Any]]:
    """Inside the host-list probe: only its one (documentation) address may be reached, names come
    only from its own hosts file, and the address filter is really attached."""
    items = []
    public = [(h, port, _errno_of(lambda h=h, port=port: _connect(h, port))) for h, port in _PUBLIC]
    reached = [f"{h}:{port}" for h, port, e in public if e is None]
    denied = all(e == "EPERM" for _, _, e in public)
    items.append(_item(pre + "public", "a job that lists hosts cannot reach other internet addresses", not reached,
                       "Unlisted internet addresses are refused (" + ", ".join(f"{h}: {e}" for h, _, e in public) + ").",
                       "It can reach " + ", ".join(reached) + ", which it does not list."))
    local = [(h, port, _errno_of(lambda h=h, port=port: _connect(h, port))) for h, port in _LOCAL]
    open_ = [f"{h}:{port}" for h, port, e in local if e is None]
    items.append(_item(pre + "local", "a job that lists hosts cannot reach this computer or the local network",
                       not open_, "This computer, private ranges and the cloud metadata address are refused.",
                       "It can reach " + ", ".join(open_) + "."))
    listed = _errno_of(lambda: _connect(units.SELFTEST_ADDRESS, 443))
    if reached:
        attached: bool | None = False
        why = "Unlisted addresses can be reached, so the address filter is not working."
    elif denied and listed != "EPERM":
        attached = True
        why = (f"Unlisted addresses are refused by the filter (EPERM) while its one listed address is let through "
               f"(it leads nowhere: {listed}).")
    else:
        attached = None
        why = ("todo: mes cannot tell whether systemd's address filter is attached: unlisted addresses failed with "
               + ", ".join(sorted({str(e) for _, _, e in public})) + f" and the listed one with {listed}. Only a "
               "refusal with EPERM proves the filter (a server without internet fails the same way without it). "
               "Check the journal for 'BPF firewalling not supported' and that the kernel has cgroup BPF.")
    items.append(protocol.item(pre + "filter", "systemd's address filter is attached to a job that lists hosts",
                               "done" if attached else "todo", why))
    try:
        text = Path("/etc/hosts").read_text(encoding="utf-8")
    except OSError as e:
        text = f"(unreadable: {e})"
    lines = [ln.split() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    own = sorted(" ".join(w[:2]) for w in lines)
    want = sorted(["127.0.0.1 localhost", "::1 localhost", f"{units.SELFTEST_ADDRESS} {units.SELFTEST_HOST}"])
    try:
        got = sorted({str(i[4][0]) for i in socket.getaddrinfo(units.SELFTEST_HOST, 443, 0, socket.SOCK_STREAM)})
    except OSError:
        got = []
    try:
        socket.getaddrinfo("example.com", 443, 0, socket.SOCK_STREAM)
        other = True
    except OSError:
        other = False
    good = own == want and got == [units.SELFTEST_ADDRESS] and not other
    items.append(_item(pre + "names", "a job that lists hosts looks names up only in its own hosts file", good,
                       f"/etc/hosts is its own ({units.SELFTEST_HOST} only) and other names cannot be looked up.",
                       f"/etc/hosts holds {own[:6]}, {units.SELFTEST_HOST} gave {got}, other names "
                       + ("can" if other else "cannot") + " be looked up."))
    ok, what = _fails(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close())
    items.append(_item(pre + "unix", "a job that lists hosts cannot open unix sockets (it shares the server's "
                       "abstract unix socket namespace)", ok, f"Opening one is refused ({what}).",
                       "It can open unix sockets, so it could reach other programs' abstract sockets."))
    return items


def _uv_syscalls(layout: paths.ServerLayout) -> dict[str, Any]:
    problems = []
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    except (OSError, ValueError) as e:
        problems.append(f"raising the open-file limit: {e}")
    try:
        target = layout.installing / f".mes-selftest-chown-{os.getpid()}"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchown(fd, os.getuid(), os.getgid())
        finally:
            os.close(fd)
            target.unlink(missing_ok=True)
    except OSError as e:
        problems.append(f"setting a file's owner to itself: {e}")
    return _item("deploy-syscalls", "the deploy sandbox allows what uv needs", not problems,
                 "Raising the open-file limit and owning its own files work.", "; ".join(problems))


def write_job_result(layout: paths.ServerLayout, context: str = "job") -> dict[str, Any]:
    """Run by ``mes-<p>.selftest.service`` or ``mes-<p>.selftest-net.service`` (a job's sandbox):
    leave the answer in gate/selftest/, which those units alone have bound in (no job sees it, so
    no job can forge the answer)."""
    if context not in ("job", "net"):
        raise ValueError(context)
    result = {**probe(layout, context), "time": time.time()}
    out = layout.gate / "selftest" / (JOB_RESULT if context == "job" else NET_RESULT)
    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = out.with_name(f".{context}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(result), encoding="utf-8")
    os.replace(tmp, out)
    return result


def _job_probe(layout: paths.ServerLayout, context: str = "job") -> list[dict[str, Any]]:
    started = time.time()
    request = layout.gate / (REQUEST_FILE if context == "job" else NET_REQUEST_FILE)
    title = "a job's sandbox" if context == "job" else "the sandbox of a job that lists hosts"
    unit = units.selftest_units(layout.project)[1] if context == "job" else units.selftest_net_units(layout.project)[1]
    try:
        (layout.gate / "selftest").mkdir(mode=0o700, parents=True, exist_ok=True)
        request.write_text(f"{started}\n", encoding="ascii")
    except OSError as e:
        return [_item(context, title, None, "", f"skipped: the gate could not ask for the probe ({e}).")]
    result = layout.gate / "selftest" / (JOB_RESULT if context == "job" else NET_RESULT)
    deadline = time.monotonic() + JOB_WAIT_SEC
    while time.monotonic() < deadline:
        try:
            data = json.loads(result.read_text(encoding="utf-8"))
            if isinstance(data, dict) and float(data.get("time", 0)) >= started and isinstance(data.get("items"), list):
                return [i for i in data["items"] if isinstance(i, dict)]
        except (OSError, ValueError, TypeError):
            pass
        time.sleep(1)
    return [_item(context, title, None, "",
                  f"skipped: the probe unit did not answer within {JOB_WAIT_SEC:g} seconds. The person checks: "
                  f"systemctl status {unit}")]


def _filter_settings(layout: paths.ServerLayout, show=None) -> dict[str, Any]:
    """What systemd says the host-list probe may reach (read from outside, by the gate)."""
    from mes import jobs

    unit = units.selftest_net_units(layout.project)[1]
    props = (show or jobs._show)(unit, "IPAddressAllow", "IPAddressDeny")
    allow, deny = props.get("IPAddressAllow"), props.get("IPAddressDeny")
    if allow is None or deny is None:
        return _item("net-filter-settings", "systemd holds the host-list probe's address filter", None, "",
                     f"skipped: 'systemctl show {unit}' did not answer here.")
    deny_any = {"0.0.0.0/0", "::/0"} <= set(deny.split())
    good = deny_any and allow.split() == [f"{units.SELFTEST_ADDRESS}/32"]
    return _item("net-filter-settings", "systemd holds the host-list probe's address filter", good,
                 f"IPAddressDeny={deny}, IPAddressAllow={allow}: everything is refused except its one address.",
                 f"IPAddressDeny={deny or '(none)'}, IPAddressAllow={allow or '(none)'}, not what the plan sets.")


def _sandbox_required() -> dict[str, Any]:
    from mes import gate

    on = gate.REQUIRE_SANDBOX_ON_SERVER is True
    return _item("gate-requires-sandbox", "the gate refuses to run anything without its request sandboxes", on,
                 "REQUIRE_SANDBOX_ON_SERVER is on: a server whose sandbox sockets are missing runs nothing.",
                 "REQUIRE_SANDBOX_ON_SERVER is off in the mes on this server, so a missing sandbox would let requests "
                 "run with less protection. Reinstall mes as described in: mes guide server")


def run_inside(where: Any, run_child) -> dict[str, Any]:
    """The gate's side of ``["mes", "selftest"]`` (deploy key): every request sandbox, then a
    job's sandbox through the probe unit."""
    layout = where.layout
    items: list[dict[str, Any]] = [_sandbox_required()]
    probe_dir = layout.releases / ".selftest-install"
    for level in units.SANDBOX_LEVELS:
        try:
            if level == "deploy":
                # The install step may write only what code/installing points at: here an empty folder.
                # (Under the deploy lock, so a running deploy's link is never touched.)
                import shutil

                from mes import deploy

                with deploy._deploy_lock(where):
                    probe_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                    tmp = layout.code / f".installing-selftest-{os.getpid()}"
                    os.symlink(f"releases/{probe_dir.name}", tmp)
                    os.replace(tmp, layout.installing)
                    try:
                        data = run_child({"kind": "selftest", "level": level})
                    finally:
                        deploy._clear_installing(layout)
                        shutil.rmtree(probe_dir, ignore_errors=True)
            else:
                data = run_child({"kind": "selftest", "level": level})
            items += [i for i in data.get("items") or [] if isinstance(i, dict)]
        except Exception as e:  # noqa: BLE001 - report, never pass
            items.append(_item(f"{level}-sandbox", f"the {level} sandbox answers", False, "",
                               f"It did not answer: {getattr(e, 'message', type(e).__name__)}"))
    items += _job_probe(layout, "job")
    items += _job_probe(layout, "net")
    items.append(_filter_settings(layout))
    return protocol.checklist(items)


def outside(layout: paths.ServerLayout) -> list[dict[str, Any]]:
    """From the account running the command (the owner, normally)."""
    me = os.getuid()
    items = []
    if me == 0:
        return [_item("outside", "Checks from your own account", None, "",
                      "skipped: run 'mes server selftest' as your own account, not as root.")]
    for name, folder in (("data", layout.data), ("state", layout.state), ("code", layout.code),
                         ("auth", layout.auth), ("gate", layout.gate), ("etc", layout.etc)):
        ok, what = _fails(lambda f=folder: os.listdir(f))
        items.append(_item(f"outside-{name}", f"Other accounts cannot open {name}/", ok,
                           f"Your account cannot list it ({what}).", f"Your account can list {folder}."))
    for level in units.SANDBOX_LEVELS:
        ok, what = _fails(lambda lv=level: _connect_unix(units.gate_socket_path(layout.project, lv)))
        items.append(_item(f"outside-socket-{level}", f"Other accounts cannot use the {level} gate socket", ok,
                           f"Your account cannot connect ({what}).", "Your account can connect to it."))
    return items


def run(project: str, *, server_root: str, host: str | None, login_key: str | None,
        timeout: float = 300.0) -> dict[str, Any]:
    from mes import deploy

    layout = paths.server_layout(project, server_root)
    if os.path.isdir(layout.base):
        items = outside(layout)
    else:
        items = [_item("outside", "Checks from your own account on the server", None, "",
                       f"skipped: {layout.base} is not on this computer. Run 'mes server selftest {project}' on the "
                       "server itself (as your own account) to check that other accounts cannot open its folders.")]
    dest = deploy.destination(host or "localhost", project)
    try:
        answer = deploy._send(dest, ["mes", "selftest"], None, timeout, deploy._login_key(login_key))
        items += [i for i in answer.get("items") or [] if isinstance(i, dict)]
    except Exception as e:  # noqa: BLE001 - a self-test reports, it never passes what it could not check
        items.append(_item("inside", "Checks inside the sandboxes", None, "",
                           "skipped: the gate could not be reached with your deploy login key "
                           f"({getattr(e, 'message', type(e).__name__)}). Run it from your own computer with "
                           f"--host <server> (and --login-key), or check the key setup.",
                           detail=getattr(e, "detail", None)))
    result = protocol.checklist(items)
    # A self-test proves only what it checked: a skipped claim is not a pass.
    result["ok"] = all(i["status"] == "done" for i in items)
    result["unproved"] = [i["id"] for i in items if i["status"] == "skipped"]
    result["failed"] = [i["id"] for i in items if i["status"] == "todo"]
    return result
