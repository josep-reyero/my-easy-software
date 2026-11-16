"""``mes deploy <host>``: install a project on a server you own, signed, with rollback.

On this computer (the client)
-----------------------------
1. Packs the project: only files tracked by git (``git ls-files``), as they are in the folder,
   under ``project/``; plus the git-tracked files of each local path dependency named in
   ``[tool.uv.sources]`` (only sibling folders, ``../<name>``, such as ``../my-easy-software``),
   under ``<name>/``, so the same relative paths work on the server. ``MES-RELEASE.json``
   records the project, version, time, commit and whether there were uncommitted changes.
   Links, missing ``uv.lock`` and folders outside the project are refused.
2. Signs the archive with ``ssh-keygen -Y sign -n mes-deploy`` (an OpenSSH signature, Ed25519
   key; ``--key``, ``[deploy] key`` in the config file, or ``~/.ssh/mes-deploy``).
3. Sends it through the server's gate at the ``deploy`` level:
   ``ssh -T -o BatchMode=yes mes-<project>@<host> '["mes", "deploy", "--signature", "..."]' < archive``.
``--dry-run`` shows every step and sends nothing. ``--rollback [--to N]`` and ``--releases`` ask
the server to switch back or to list what is installed.

On the server (inside the gate, as the project's account)
---------------------------------------------------------
1. Reads the archive from the connection into ``code/incoming/`` (at most ``MAX_ARCHIVE_BYTES``).
2. Verifies the signature **before unpacking**: ``ssh-keygen -Y verify -n mes-deploy -I <project>
   -f etc/allowed_signers``. The signer list must belong to whoever owns the project's top folder
   (root on a real server) and nobody else may change it; it may only hold Ed25519 keys.
3. Unpacks with the hardened extractor of ``mes.archive`` into a new ``code/releases/<n>/``.
4. Refuses an archive for another project, one older than the newest release, or one already
   installed.
5. Runs ``uv sync --frozen`` in its own sandbox, then asks every app to describe itself and
   checks the jobs' commands.
6. Switches ``code/current`` to the new release atomically, publishes the dashboards
   (``easy-ui publish``), and switches back if publishing fails.
7. Keeps the newest ``KEEP_RELEASES`` releases (never the current or the previous one).
8. Says when the jobs or units changed, so the person runs ``mes server plan`` again.

Public API
----------
``SIGN_NAMESPACE`` (``"mes-deploy"``), ``RELEASE_FILE``, ``KEEP_RELEASES``, ``MAX_ARCHIVE_BYTES``,
``DEPLOY_LIMITS``, ``SIGNER_KEY_TYPES``.
Client: ``collect(root, project) -> (entries, info)``, ``build_archive(root, project, out) -> dict``,
``sign(archive, key) -> str``, ``destination(host, project) -> str``,
``ssh_argv(dest, request) -> list[str]``, ``run_deploy(...)``.
Server: ``operation(where, command, args, *, stdin, timeout, run_child) -> dict``,
``receive(...)``, ``rollback(...)``, ``list_releases(layout) -> dict``, ``child_step(where, req, *, timeout) -> dict``,
``check_signers(path, project, base) -> list[str]``, ``read_server_config(layout) -> dict``,
``units_fingerprint(project, layout, mes, easy_ui) -> str``.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any, BinaryIO, Callable

from mes import archive, paths, protocol, units
from mes import manifest as manifest_mod
from mes.app import check_allowed
from mes.errors import MesError, NeedsPerson, NotAllowed, NotFound, Refused, TemporaryFailure, UsageError

SIGN_NAMESPACE = "mes-deploy"
RELEASE_FILE = "MES-RELEASE.json"
DEPLOY_LOG = "deploy-log.jsonl"
KEEP_RELEASES = 5
MAX_ARCHIVE_BYTES = 200 * 1024**2
DEPLOY_LIMITS = archive.Limits(max_files=20_000, max_file_bytes=100 * 1024**2, max_total_bytes=500 * 1024**2)
SIGNER_KEY_TYPES = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com")
DEFAULT_KEY = "~/.ssh/mes-deploy"
SYNC_TIMEOUT = 900.0
_DEP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}\Z")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}\Z")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\Z")
_SIG_RE = re.compile(r"-----BEGIN SSH SIGNATURE-----\n[A-Za-z0-9+/=\n]{40,3000}-----END SSH SIGNATURE-----\n?")
_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,3}\Z")
_SECRET_NAMES = re.compile(r"(?:^|/)(?:\.env(?:\..*)?|id_[a-z0-9]+|.*\.pem|.*\.key|credentials.*|.*token.*)\Z",
                           re.IGNORECASE)


# =========================================================================== client


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=60, check=False,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise NotFound("Deploying needs git (to know which files belong to the project), and git is not "
                       "installed on this computer.", hint="Install git, then try again.", code="git_missing") from None


def _tracked(root: Path, label: str) -> tuple[list[tuple[str, Path]], list[str], bool, str | None]:
    """(files, notes, dirty, commit) for the git-tracked files under ``root``."""
    res = _git(root, "ls-files", "-z", "--cached", "--")
    if res.returncode != 0:
        raise MesError(
            f"{label} is not in a git repository, so mes cannot tell which of its files belong to it. Only files "
            "tracked by git are deployed, so nothing private that happens to sit in the folder is sent.",
            hint=f"Put it in git first: cd {root} && git init && git add -A && git commit -m 'First version'",
            code="not_in_git",
        )
    names = [n.decode("utf-8", "surrogateescape") for n in res.stdout.split(b"\x00") if n]
    files: list[tuple[str, Path]] = []
    notes: list[str] = []
    for name in sorted(set(names)):
        full = root / name
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            notes.append(f"{label}: {name} is tracked by git but missing from the folder, so it is not sent.")
            continue
        if stat.S_ISLNK(st.st_mode):
            raise Refused(f"{label}: {name} is a link. Links are never deployed (on the server they could point "
                          "anywhere), so nothing was sent.",
                          hint="Replace the link with the real file, or remove it from git.", code="link_in_project")
        if stat.S_ISDIR(st.st_mode):
            notes.append(f"{label}: {name} is another git repository (a submodule), so it is not sent.")
            continue
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"{label}: {name} is not a normal file, so nothing was sent.",
                          hint="Remove it from git.", code="special_file")
        if _SECRET_NAMES.search(name):
            notes.append(f"{label}: {name} looks like a file with a password or key, and it is tracked by git, so "
                         "it will be sent to the server. If it holds a secret, remove it from git.")
        files.append((name, full))
    dirty = bool(_git(root, "status", "--porcelain", "--", ".").stdout.strip())
    head = _git(root, "rev-parse", "HEAD")
    commit = head.stdout.decode().strip() if head.returncode == 0 else None
    return files, notes, dirty, commit


def _path_dependencies(root: Path) -> list[tuple[str, Path]]:
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    out = []
    if not isinstance(sources, dict):
        return out
    for package, spec in sources.items():
        specs = spec if isinstance(spec, list) else [spec]
        for s in specs:
            if not isinstance(s, dict) or "path" not in s:
                continue
            raw = str(s["path"])
            parts = Path(raw).parts
            if os.path.isabs(raw) or len(parts) != 2 or parts[0] != ".." or not _DEP_NAME_RE.match(parts[1]) \
                    or parts[1] in ("project", RELEASE_FILE):
                raise Refused(
                    f'pyproject.toml takes "{package}" from the folder {raw!r}. mes can only send local dependencies '
                    "that sit right next to the project (../<folder>), so the same path works on the server.",
                    hint="Move that folder next to the project folder (and use path = \"../<folder>\"), or depend on "
                    "a published version instead.",
                    code="dependency_path",
                )
            folder = (root / raw).resolve()
            if not (folder / "pyproject.toml").is_file():
                raise NotFound(f'The dependency "{package}" should be in {folder}, but that folder has no '
                               "pyproject.toml, so nothing was sent.", hint="Check the path in pyproject.toml.")
            out.append((parts[1], folder))
    return out


def collect(root: Path, project: manifest_mod.Project) -> tuple[list[tuple[str, Path | bytes]], dict[str, Any]]:
    files, notes, dirty, commit = _tracked(root, "The project")
    names = {n for n, _ in files}
    for needed in (manifest_mod.MANIFEST_FILE, "pyproject.toml", "uv.lock"):
        if needed not in names:
            raise MesError(
                f"{needed} is not tracked by git, so the server could not install the project the same way as here.",
                hint=f"Add it: git add {needed}" + (" (create it first with: uv lock)" if needed == "uv.lock" else ""),
                code="missing_file",
            )
    entries: list[tuple[str, Path | bytes]] = [(f"{paths.RELEASE_PROJECT_DIR}/{n}", p) for n, p in files]
    deps = []
    for name, folder in _path_dependencies(root):
        dep_files, dep_notes, dep_dirty, _ = _tracked(folder, f"The dependency {name}")
        notes += dep_notes
        dirty = dirty or dep_dirty
        entries += [(f"{name}/{n}", p) for n, p in dep_files]
        deps.append(name)
    now = time.time()
    info = {
        "format": 1,
        "project": project.name,
        "version": project.version,
        "created": _dt.datetime.fromtimestamp(now, _dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "created_ns": time.time_ns(),
        "commit": commit,
        "dirty": dirty,
        "files": len(entries),
        "deps": deps,
    }
    if dirty:
        notes.append("Some files have changes that are not committed to git yet. They are deployed as they are now; "
                     "commit them so you can always tell which version is on the server.")
    entries.insert(0, (RELEASE_FILE, json.dumps(info, indent=2).encode("utf-8") + b"\n"))
    total = sum(p.stat().st_size if isinstance(p, Path) else len(p) for _, p in entries)
    if total > DEPLOY_LIMITS.max_total_bytes or len(entries) > DEPLOY_LIMITS.max_files:
        raise Refused("The project is larger than a deploy may be (500 MB or 20,000 files), so nothing was sent.",
                      hint="Keep data out of the project's git repository: it belongs in the data folder.",
                      code="too_large")
    info["notes"] = notes
    info["bytes"] = total
    return entries, info


def build_archive(root: Path, project: manifest_mod.Project, out: Path) -> dict[str, Any]:
    entries, info = collect(root, project)
    packed = archive.pack(entries, out)
    if packed["archive_bytes"] > MAX_ARCHIVE_BYTES:
        raise Refused("The packed project is larger than 200 MB, so nothing was sent.",
                      hint="Keep data out of the project's git repository.", code="too_large")
    return {**info, **packed}


def _key_path(given: str | None) -> Path:
    if given:
        return Path(given).expanduser()
    cfg = paths.load_config().get("deploy", {})
    if isinstance(cfg, dict) and isinstance(cfg.get("key"), str):
        return Path(cfg["key"]).expanduser()
    return Path(DEFAULT_KEY).expanduser()


def _check_key(key: Path) -> None:
    pub = key.with_name(key.name + ".pub") if not key.name.endswith(".pub") else key
    if not key.exists() and not pub.exists():
        raise NeedsPerson(protocol.needs_human(
            "deploy-key",
            f"create a signing key for deploys (you choose a passphrase): ssh-keygen -t ed25519 -f {key} -C mes-deploy",
            "Every deploy is signed with your own key, and the server only installs code signed by a key it "
            "knows. Only you can create it, because only you should know its passphrase. Then add the public "
            f"half ({pub}) to the server with: mes server plan <project> --signer {pub}",
            "mes deploy <host>",
        ))
    try:
        kind = pub.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError, UnicodeDecodeError):
        kind = ""
    if kind not in SIGNER_KEY_TYPES:
        raise Refused(f"The signing key {key} is not an Ed25519 key, and deploys are only signed with Ed25519.",
                      hint=f"Create one with: ssh-keygen -t ed25519 -f {DEFAULT_KEY} -C mes-deploy", code="key_type")


def sign(archive_path: Path, key: Path) -> str:
    _check_key(key)
    sig = archive_path.with_name(archive_path.name + ".sig")
    sig.unlink(missing_ok=True)
    try:
        res = subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n", SIGN_NAMESPACE, str(archive_path)],
                             capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL,
                             start_new_session=True)  # no terminal: never waits for a passphrase
    except FileNotFoundError:
        raise NotFound("Signing needs ssh-keygen (part of OpenSSH), which is not installed here.",
                       hint="Install OpenSSH, then try again.", code="ssh_keygen_missing") from None
    except subprocess.TimeoutExpired:
        raise TemporaryFailure("Signing took too long, so it was stopped.", hint="Try again.") from None
    if res.returncode != 0 or not sig.is_file():
        raise NeedsPerson(protocol.needs_human(
            "unlock-deploy-key",
            f"unlock your signing key for this session: ssh-add {key}",
            "The deploy is signed with your key, and the key is protected by a passphrase that only you know. "
            "Once it is in your SSH agent, mes can sign without asking.",
            "mes deploy <host>",
        ))
    text = sig.read_text(encoding="ascii")
    if not _SIG_RE.fullmatch(text):
        raise MesError("ssh-keygen wrote a signature mes does not recognise, so nothing was sent.",
                       hint="Check that OpenSSH is version 8.1 or newer.", code="bad_signature")
    return text


def destination(host: str, project: str) -> str:
    user, at, name = host.rpartition("@")
    if not at:
        user, name = f"mes-{project}", host
    if not _HOST_RE.match(name) or not _USER_RE.match(user):
        raise UsageError(f"{host[:80]!r} is not a server name mes can use.",
                         hint="Give the server's name, for example: mes deploy my-server")
    return f"{user}@{name}"


def ssh_argv(dest: str, request: list[str]) -> list[str]:
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
            "-e", "none", "--", dest, json.dumps(request, ensure_ascii=True)]


def _send(dest: str, request: list[str], archive_path: Path | None, timeout: float) -> dict[str, Any]:
    # "--json": the gate then answers with JSON even when it refuses before reading the request.
    argv = ssh_argv(dest, request if "--json" in request else [*request, "--json"])
    try:
        with (open(archive_path, "rb") if archive_path else open(os.devnull, "rb")) as f:
            res = subprocess.run(argv, stdin=f, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise NotFound("Deploying needs ssh, which is not installed on this computer.",
                       hint="Install OpenSSH, then try again.", code="ssh_missing") from None
    except subprocess.TimeoutExpired:
        raise TemporaryFailure(f"The server did not finish within {timeout:g} seconds, so mes stopped waiting.",
                               hint="Check on the server with: mes deploy <host> --releases") from None
    try:
        data = json.loads(res.stdout.decode("utf-8")) if res.stdout.strip() else None
    except (UnicodeDecodeError, ValueError):
        data = None
    if isinstance(data, dict) and "error" in data and isinstance(data["error"], dict):
        said = data["error"]
        cls = {2: UsageError, 3: NotFound, 4: NotAllowed, 5: Refused, 75: TemporaryFailure}.get(res.returncode, MesError)
        raise cls(f"The server refused the request: {str(said.get('message') or '')[:2000]}",
                  hint=str(said.get("hint") or "")[:500] or None, code=str(said.get("code") or "server_refused")[:40])
    if res.returncode != 0 or not isinstance(data, dict):
        detail = res.stderr.decode("utf-8", "replace").strip()[-2000:]
        raise MesError(
            "mes could not reach the server's gate, so nothing changed there.",
            hint="Check that the server is on (and on your private network), that its script from 'mes server plan' "
            "was run, and that your deploy key is in your SSH agent.",
            code="ssh_failed", detail=detail or f"ssh exit code {res.returncode}", detail_label="What ssh said:",
        )
    return data


def run_deploy(host: str, *, project_dir: str | None, key: str | None, dry_run: bool, rollback: bool,
               to: int | None, releases: bool, timeout: float) -> dict[str, Any]:
    from mes.jobs import load_project

    root, project = load_project(project_dir)
    dest = destination(host, project.name)
    if releases or rollback:
        request = ["mes", "releases"] if releases else (["mes", "rollback"] + (["--to", str(to)] if to else []))
        if dry_run:
            return {"dry_run": True, "steps": [{"step": "ask the server", "command": " ".join(ssh_argv(dest, request))}]}
        return _send(dest, request, None, timeout)
    with tempfile.TemporaryDirectory(prefix="mes-deploy-") as tmp:
        out = Path(tmp) / f"{project.name}.tar.gz"
        info = build_archive(root, project, out)
        key_path = _key_path(key)
        sign_cmd = f"ssh-keygen -Y sign -f {key_path} -n {SIGN_NAMESPACE} {out.name}"
        steps = [
            {"step": "pack", "what": f"{info['files']} files ({info['bytes']} bytes) tracked by git"
             + (f", with the local dependencies {', '.join(info['deps'])}" if info["deps"] else ""),
             "archive_sha256": info["sha256"]},
            {"step": "sign", "command": sign_cmd, "why": "the server installs only code signed by a key it knows"},
            {"step": "send", "command": " ".join(ssh_argv(dest, ["mes", "deploy", "--signature", "<signature>"]))
             + " < archive"},
            {"step": "on the server", "what": [
                "verify the signature against the project's allowed signers, before unpacking anything",
                "unpack with the hardened extractor into a new release folder",
                "uv sync --frozen in a sandbox, then check that every app describes itself and the jobs' commands",
                "switch 'current' to the new release in one step",
                "publish the dashboards (switch back if that fails)",
                f"keep the newest {KEEP_RELEASES} releases",
            ]},
        ]
        if dry_run:
            return {"dry_run": True, "project": project.name, "to": dest, "steps": steps, "notes": info["notes"],
                    "message": "Nothing was signed or sent: these are the steps 'mes deploy' would take."}
        signature = sign(out, key_path)
        answer = _send(dest, ["mes", "deploy", "--signature", signature], out, timeout)
    return {**answer, "notes": info["notes"] + list(answer.get("notes") or [])}


def _deploy_text(result: dict[str, Any]) -> str:
    if result.get("dry_run"):
        lines = [result.get("message") or "Dry run: nothing was sent."]
        for s in result["steps"]:
            what = s.get("command") or s.get("what")
            if isinstance(what, list):
                lines.append(f"- {s['step']}:")
                lines += [f"    {w}" for w in what]
            else:
                lines.append(f"- {s['step']}: {what}")
    else:
        lines = [str(result.get("message") or protocol.format_text(result))]
    for n in result.get("notes") or []:
        lines.append(f"Please note: {n}")
    return "\n".join(lines)


def _cmd_deploy(args: argparse.Namespace) -> Any:
    check_allowed("admin")  # it changes what runs on the server
    if args.to is not None and not args.rollback:
        raise UsageError("--to only works together with --rollback.", hint="For example: mes deploy my-server --rollback --to 3")
    result = run_deploy(args.host, project_dir=args.project_dir, key=args.key, dry_run=args.dry_run,
                        rollback=args.rollback, to=args.to, releases=args.releases, timeout=args.timeout)
    return result if args.json else _deploy_text(result)


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "deploy",
        help="Install the project on a server you own (signed), roll back, or list releases.",
        description="Pack the project's git-tracked files, sign them with your Ed25519 key and send them through "
        "the server's gate. The server verifies the signature before unpacking. See: mes guide deploy",
    )
    p.add_argument("host", help="the server (an SSH name such as my-server; the account mes-<project> is used)")
    p.add_argument("--project-dir", default=None, metavar="DIR", help="the project's folder (default: here)")
    p.add_argument("--key", default=None, metavar="PATH", help=f"the signing key (default: {DEFAULT_KEY})")
    p.add_argument("--dry-run", action="store_true", help="show every step and send nothing")
    p.add_argument("--rollback", action="store_true", help="switch the server back to the previous release")
    p.add_argument("--to", type=int, default=None, metavar="N", help="with --rollback: the release to switch to")
    p.add_argument("--releases", action="store_true", help="list the releases installed on the server")
    p.add_argument("--timeout", type=float, default=1800.0, help="seconds to wait for the server (default 1800)")
    p.set_defaults(func=_cmd_deploy)


# =========================================================================== server


def _owner_of(path: Path) -> int:
    return os.lstat(path).st_uid


def _check_root_file(path: Path, base: Path, what: str) -> None:
    """A file the project's account must not be able to change: it belongs to whoever owns the
    project's top folder (root on a server) and nobody else can write to it."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise NotFound(f"The server has no {what} for this project, so nothing was installed.",
                       hint="Run 'mes server plan <project> --signer <key.pub>' and its script on the server.",
                       code="not_set_up") from None
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"The {what} is not a normal file, so the server refuses to trust it.",
                      hint="Run 'mes server plan' and its script again.", code="untrusted_file")
    owner = _owner_of(base)
    if st.st_uid != owner or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise Refused(f"The {what} could be changed by someone other than the server's administrator, so the "
                      "server refuses to trust it.",
                      hint="Run 'mes server plan' and its script again to repair it.", code="untrusted_file")
    for folder in path.parents:
        if folder == base.parent:
            break
        fst = os.lstat(folder)
        if fst.st_uid not in (owner, 0) or fst.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not fst.st_mode & stat.S_ISVTX:
            raise Refused(f"A folder above the {what} could be changed by someone else, so the server refuses to "
                          "trust it.", hint="Run 'mes server plan' and its script again.", code="untrusted_file")


def check_signers(path: Path, project: str, base: Path) -> list[str]:
    _check_root_file(path, base, "list of allowed signers")
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines()[:100]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        words = line.split()
        if len(words) < 4 or words[0] != project or words[1] != f'namespaces="{SIGN_NAMESPACE}"':
            raise Refused("The list of allowed signers has a line that is not exactly '<project> "
                          f'namespaces="{SIGN_NAMESPACE}" <key>\' for this project, so the server refuses to use it.',
                          hint="Run 'mes server plan' and its script again.", code="untrusted_file")
        if words[2] not in SIGNER_KEY_TYPES or len(words) > 5:
            raise Refused("The list of allowed signers has a key that is not a plain Ed25519 key, so the server "
                          "refuses to use it.", hint="Use Ed25519 keys only.", code="key_type")
        lines.append(line)
    if not lines:
        raise NotFound("No signing key is allowed for this project yet, so nothing can be installed.",
                       hint="Run 'mes server plan <project> --signer <key.pub>' and its script on the server.",
                       code="no_signers")
    return lines


def read_server_config(layout: paths.ServerLayout) -> dict[str, Any]:
    _check_root_file(layout.server_toml, layout.base, "server settings file")
    try:
        data = tomllib.loads(layout.server_toml.read_text(encoding="utf-8")).get("server", {})
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise MesError("The server settings file of this project is damaged.", hint="Run 'mes server plan' again.",
                       code="config_invalid") from None
    if not isinstance(data, dict) or data.get("project") != layout.project:
        raise Refused("The server settings file is for another project.", hint="Run 'mes server plan' again.",
                      code="untrusted_file")
    for key in ("mes", "uv", "easy_ui"):
        if key in data and data[key] is not None:
            data[key] = units.check_tool_path(str(data[key]), f"the path of {key}")
    return data


@contextlib.contextmanager
def _deploy_lock(where: Any):
    folder = where.gate_dir
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(folder / "deploy.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TemporaryFailure("Another deploy of this project is running, so this one was not started.",
                                   hint="Wait for it to finish, then try again.", code="deploy_running") from None
        yield
    finally:
        os.close(fd)


def _release_numbers(layout: paths.ServerLayout) -> list[int]:
    if not layout.releases.is_dir():
        return []
    out = []
    for p in layout.releases.iterdir():
        if p.name.isdigit() and p.is_dir() and not p.is_symlink():
            out.append(int(p.name))
    return sorted(out)


def _current_number(layout: paths.ServerLayout) -> int | None:
    try:
        target = os.readlink(layout.current)
    except OSError:
        return None
    m = re.fullmatch(r"releases/([0-9]+)", target)
    return int(m.group(1)) if m else None


def _switch(layout: paths.ServerLayout, number: int) -> None:
    tmp = layout.code / f".current-{secrets.token_hex(4)}"
    os.symlink(f"releases/{number}", tmp)
    os.replace(tmp, layout.current)


def _release_info(layout: paths.ServerLayout, number: int) -> dict[str, Any]:
    p = layout.releases / str(number) / RELEASE_FILE
    try:
        if p.stat().st_size > 64 * 1024:
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _record(layout: paths.ServerLayout, entry: dict[str, Any]) -> None:
    line = json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n"
    fd = os.open(layout.code / DEPLOY_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, line.encode("ascii"))
    finally:
        os.close(fd)


def _read_stdin(stream: BinaryIO | None, target: Path) -> tuple[int, str]:
    if stream is None:
        raise UsageError("No archive came with the deploy request.", hint="Use: mes deploy <host>")
    h = hashlib.sha256()
    total = 0
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_ARCHIVE_BYTES:
                raise Refused("The archive is larger than 200 MB, so it was refused before being checked.",
                              hint="Keep data out of the project's git repository.", code="too_large")
            h.update(block)
            f.write(block)
    if total == 0:
        raise UsageError("The deploy request came without an archive.", hint="Use: mes deploy <host>")
    return total, h.hexdigest()


def _ssh_keygen() -> str:
    for candidate in ("/usr/bin/ssh-keygen", "/usr/local/bin/ssh-keygen", "/bin/ssh-keygen"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("ssh-keygen", path="/usr/local/bin:/usr/bin:/bin")
    if not found:
        raise NotFound("The server has no ssh-keygen, so it cannot check signatures and installs nothing.",
                       hint="Install OpenSSH on the server.", code="ssh_keygen_missing")
    return found


def verify_signature(archive_path: Path, signature: str, signers: Path, project: str) -> str:
    """``ssh-keygen -Y verify``; returns the signing key's fingerprint. Raises ``Refused``."""
    if not isinstance(signature, str) or not _SIG_RE.fullmatch(signature):
        raise Refused("The deploy's signature is not an OpenSSH signature, so nothing was installed.",
                      hint="Deploy with 'mes deploy', which signs with ssh-keygen.", code="bad_signature")
    sig = archive_path.with_name(archive_path.name + ".sig")
    fd = os.open(sig, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(signature)
    try:
        with open(archive_path, "rb") as data:
            res = subprocess.run([_ssh_keygen(), "-Y", "verify", "-f", str(signers), "-I", project,
                                  "-n", SIGN_NAMESPACE, "-s", str(sig)], stdin=data, capture_output=True,
                                 timeout=120, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C"})
    finally:
        sig.unlink(missing_ok=True)
    out = res.stdout.decode("utf-8", "replace")
    if res.returncode != 0 or not out.startswith(f'Good "{SIGN_NAMESPACE}" signature for {project} with '):
        raise Refused(
            "The deploy is not signed by a key this server trusts for the project, so nothing was unpacked or "
            "installed.",
            hint="Sign with the key whose public half was given to 'mes server plan --signer', or add your key "
            "there and run its script again.",
            code="bad_signature",
        )
    m = re.search(r"key (SHA256:[A-Za-z0-9+/=]+)", out)
    return m.group(1) if m else "unknown"


def _check_release(layout: paths.ServerLayout, folder: Path, sha: str) -> dict[str, Any]:
    info_path = folder / RELEASE_FILE
    try:
        if info_path.stat().st_size > 64 * 1024:
            raise ValueError
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        raise Refused("The archive has no readable release description, so it was not installed.",
                      hint="Deploy with 'mes deploy'.", code="bad_release") from None
    if not isinstance(info, dict) or info.get("format") != 1 or info.get("project") != layout.project:
        raise Refused(f"The archive is not a release of the project \"{layout.project}\", so it was not installed.",
                      hint="Deploy the right project (each project has its own account on the server).",
                      code="wrong_project")
    deps = info.get("deps", [])
    if not isinstance(deps, list) or not all(isinstance(d, str) and _DEP_NAME_RE.match(d) and d != "project"
                                             for d in deps):
        raise Refused("The release description lists dependencies mes does not accept.", hint="Deploy with 'mes deploy'.",
                      code="bad_release")
    top = {p.name for p in folder.iterdir()}
    expected = {RELEASE_FILE, paths.RELEASE_PROJECT_DIR, *deps}
    if top != expected:
        raise Refused("The archive holds other things than the project and the dependencies it lists, so it was "
                      "not installed.", hint="Deploy with 'mes deploy'.", code="bad_release")
    created = info.get("created_ns")
    if not isinstance(created, int):
        raise Refused("The release description has no creation time.", hint="Deploy with 'mes deploy'.",
                      code="bad_release")
    project = manifest_mod.load(folder / paths.RELEASE_PROJECT_DIR)
    if project.name != layout.project:
        raise Refused(f"The archive's project.toml is for \"{project.name}\", not \"{layout.project}\".",
                      hint="Deploy the right project.", code="wrong_project")
    if not (folder / paths.RELEASE_PROJECT_DIR / "uv.lock").is_file():
        raise Refused("The archive has no uv.lock, so the server cannot install exactly the same packages.",
                      hint="Run 'uv lock' and commit uv.lock.", code="bad_release")
    for n in _release_numbers(layout):
        other = _release_info(layout, n)
        if other.get("archive_sha256") == sha:
            raise Refused(f"This exact archive is already installed as release {n}, so nothing changed.",
                          hint=f"To switch to it, use: mes deploy <host> --rollback --to {n}", code="already_installed")
        if isinstance(other.get("created_ns"), int) and other["created_ns"] >= created:
            raise Refused(
                f"This archive was made before release {n}, which is already installed, so it was refused (an old "
                "signed archive must not quietly replace a newer version).",
                hint="To go back to an older version on purpose, use: mes deploy <host> --rollback", code="older_release")
    return {**info, "archive_sha256": sha}


def _units_note(layout: paths.ServerLayout, project: manifest_mod.Project, config: dict[str, Any]) -> str | None:
    planned = config.get("units_sha256")
    if not isinstance(planned, str):
        return None
    try:
        now = units_fingerprint(project, layout, config["mes"], config.get("easy_ui"))
    except (MesError, KeyError):
        return None
    if now == planned:
        return None
    return ("The jobs, schedules or dashboards of this version differ from what the server was set up for, so the "
            "server keeps running the old schedules. Run 'mes server plan "
            f"{layout.project}' again and run its script with sudo on the server.")


def units_fingerprint(project: manifest_mod.Project, layout: paths.ServerLayout, mes: str, easy_ui: str | None) -> str:
    files = units.render_project(project, layout=layout, mes=mes, easy_ui=easy_ui)
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\0" + files[name].encode("utf-8") + b"\0")
    return h.hexdigest()


def _prune(layout: paths.ServerLayout, keep: set[int]) -> list[int]:
    numbers = _release_numbers(layout)
    newest = set(numbers[-KEEP_RELEASES:])
    removed = []
    for n in numbers:
        if n in newest or n in keep:
            continue
        shutil.rmtree(layout.releases / str(n), ignore_errors=True)
        removed.append(n)
    return removed


def receive(where: Any, signature: str, stdin: BinaryIO | None, *,
            run_child: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    layout: paths.ServerLayout = where.layout
    signers = layout.allowed_signers
    check_signers(signers, layout.project, layout.base)
    config = read_server_config(layout)
    for folder in (layout.incoming, layout.releases):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f"{int(time.time())}-{secrets.token_hex(6)}.tar.gz"
    incoming = layout.incoming / name
    partial: Path | None = None
    try:
        size, sha = _read_stdin(stdin, incoming)
        fingerprint = verify_signature(incoming, signature, signers, layout.project)
        numbers = _release_numbers(layout)
        number = (numbers[-1] + 1) if numbers else 1
        partial = layout.releases / f".partial-{number}-{secrets.token_hex(4)}"
        counts = archive.extract(incoming, partial, limits=DEPLOY_LIMITS)
        info = _check_release(layout, partial, sha)
        info.update(signer=fingerprint, received=_now(), archive_bytes=size)
        with open(partial / RELEASE_FILE, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
        final = layout.releases / str(number)
        os.rename(partial, final)
        partial = None
    except BaseException:
        if partial is not None:
            shutil.rmtree(partial, ignore_errors=True)
        raise
    finally:
        incoming.unlink(missing_ok=True)
    try:
        synced = run_child({"kind": "sync", "release": number})
    except MesError:
        shutil.rmtree(final, ignore_errors=True)
        raise
    previous = _current_number(layout)
    _switch(layout, number)
    project = manifest_mod.load(layout.current_project)
    published = _publish(layout, project, run_child)
    if published.get("failed"):
        if previous is not None:
            _switch(layout, previous)
        _record(layout, {"time": _now(), "event": "publish_failed", "release": number, "back_to": previous})
        raise MesError(
            f"Release {number} was installed, but its dashboards could not be published, so the server switched back "
            f"to release {previous}." if previous else
            f"Release {number} was installed, but its dashboards could not be published.",
            hint="Fix the dashboards (easy-ui validate) and deploy again.", code="publish_failed",
            detail=str(published.get("detail") or "")[:2000] or None, detail_label="What easy-ui said:",
        )
    removed = _prune(layout, {number, previous} if previous else {number})
    _record(layout, {"time": _now(), "event": "deployed", "release": number, "previous": previous,
                     "sha256": sha, "signer": fingerprint, "commit": info.get("commit"), "version": info.get("version")})
    notes = [n for n in (_units_note(layout, project, config),) if n]
    notes += list(synced.get("problems") or [])
    return {
        "deployed": True,
        "project": layout.project,
        "release": number,
        "previous": previous,
        "version": info.get("version"),
        "commit": info.get("commit"),
        "files": counts["files"],
        "signer": fingerprint,
        "dashboards": published,
        "removed_releases": removed,
        "needs_plan": any("mes server plan" in n for n in notes),
        "notes": notes,
        "message": f'Release {number} of "{layout.project}" is installed and running.'
        + (f" Release {previous} is kept, so you can switch back with: mes deploy <host> --rollback" if previous else ""),
    }


def _publish(layout: paths.ServerLayout, project: manifest_mod.Project, run_child) -> dict[str, Any]:
    if project.ui is None:
        return {"published": False, "why": "the project has no dashboards"}
    try:
        return run_child({"kind": "publish"})
    except MesError as e:
        return {"failed": True, "detail": e.detail or e.message}


def rollback(where: Any, to: int | None, *, run_child) -> dict[str, Any]:
    layout: paths.ServerLayout = where.layout
    numbers = _release_numbers(layout)
    current = _current_number(layout)
    if to is None:
        older = [n for n in numbers if current is None or n < current]
        if not older:
            raise NotFound("There is no earlier release to switch back to.",
                           hint="See what is installed with: mes deploy <host> --releases", code="no_release")
        to = older[-1]
    if to not in numbers:
        raise NotFound(f"There is no release {to} on the server.", hint="See: mes deploy <host> --releases",
                       code="no_release")
    if to == current:
        return {"rolled_back": False, "release": to, "message": f"Release {to} is already the one running."}
    target = layout.releases / str(to) / paths.RELEASE_PROJECT_DIR
    if not (target / ".venv").is_dir():
        raise Refused(f"Release {to} was never installed completely, so the server will not switch to it.",
                      hint="Pick another release.", code="incomplete_release")
    _switch(layout, to)
    project = manifest_mod.load(layout.current_project)
    published = _publish(layout, project, run_child)
    if published.get("failed") and current is not None:
        _switch(layout, current)
        raise MesError(f"Switching to release {to} failed because its dashboards could not be published, so the "
                       f"server stayed on release {current}.", hint="Pick another release.", code="publish_failed")
    _record(layout, {"time": _now(), "event": "rolled_back", "release": to, "from": current})
    config = read_server_config(layout)
    notes = [n for n in (_units_note(layout, project, config),) if n]
    return {"rolled_back": True, "release": to, "from": current, "dashboards": published, "notes": notes,
            "message": f'"{layout.project}" now runs release {to} again (it ran release {current}).'}


def list_releases(layout: paths.ServerLayout) -> dict[str, Any]:
    current = _current_number(layout)
    rows = []
    for n in reversed(_release_numbers(layout)):
        info = _release_info(layout, n)
        rows.append({"release": n, "current": n == current, "version": info.get("version"),
                     "created": info.get("created"), "commit": info.get("commit"), "dirty": info.get("dirty"),
                     "signer": info.get("signer")})
    return {"project": layout.project, "current": current, "releases": rows}


def _status(where: Any) -> dict[str, Any]:
    layout: paths.ServerLayout = where.layout
    sockets = {level: os.path.exists(units.gate_socket_path(layout.project, level))
               for level in units.GATE_LEVELS}
    return {"project": layout.project, "current": _current_number(layout),
            "request_sandbox": sockets,
            "message": "Every request runs in its own system sandbox." if all(sockets.values()) else
            "Some request sandboxes are not set up, so those requests run with time and size limits only. Run "
            "'mes server plan' and its script again."}


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def operation(where: Any, command: str, args: list[str], *, stdin: BinaryIO | None, timeout: float,
              run_child: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    args = [a for a in args if a != "--json"]
    if command == "deploy":
        if len(args) != 2 or args[0] != "--signature":
            raise UsageError("A deploy request is [\"mes\", \"deploy\", \"--signature\", \"<signature>\"] with the archive "
                             "as input.", hint="Use: mes deploy <host>", code="bad_request")
        with _deploy_lock(where):
            return receive(where, args[1], stdin, run_child=run_child)
    if command == "rollback":
        to = None
        if args:
            if len(args) != 2 or args[0] != "--to" or not args[1].isdigit() or len(args[1]) > 9:
                raise UsageError("A rollback request is [\"mes\", \"rollback\"] or [\"mes\", \"rollback\", \"--to\", \"N\"].",
                                 hint="Use: mes deploy <host> --rollback [--to N]", code="bad_request")
            to = int(args[1])
        with _deploy_lock(where):
            return rollback(where, to, run_child=run_child)
    if args:
        raise UsageError(f'"mes {command}" takes no options.', hint="Use: mes deploy <host> --releases", code="bad_request")
    if command == "releases":
        return list_releases(where.layout)
    return _status(where)


# --------------------------------------------------------------------------- steps in the sandbox


def _tool_env(where: Any) -> dict[str, str]:
    layout: paths.ServerLayout = where.layout
    from mes.gate import clean_env

    env = clean_env("write", where)
    env.update({
        "UV_CACHE_DIR": str(layout.state / "uv-cache"),
        "UV_PYTHON_INSTALL_DIR": str(layout.state / "uv-python"),
        "UV_NO_PROGRESS": "1",
        "MES_ROOT": str(layout.root),  # easy-ui then uses <root>/<project>/ui and .../data
    })
    return env


def child_step(where: Any, req: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    """A deploy step that runs project code or downloads packages: called inside the deploy
    sandbox (``mes gate child --level deploy``), or by the gate itself where there is none."""
    from mes import jobs
    from mes.gate import ask_commands, clean_env, run_limited

    layout: paths.ServerLayout = where.layout
    config = read_server_config(layout)
    kind = req.get("kind")
    if kind == "sync":
        number = req.get("release")
        if not isinstance(number, int) or number < 1:
            raise UsageError("Bad release number.", hint="Use: mes deploy <host>", code="bad_request")
        folder = layout.releases / str(number) / paths.RELEASE_PROJECT_DIR
        if folder.is_symlink() or not folder.is_dir():
            raise NotFound(f"Release {number} is not on the server.", hint="Deploy again.", code="no_release")
        uv = config.get("uv")
        if not uv:
            raise NotFound("The server settings do not say where uv is, so the project cannot be installed.",
                           hint="Run 'mes server plan' with --uv <path> and its script again.", code="uv_missing")
        done = run_limited([uv, "sync", "--frozen", "--no-progress"], env=_tool_env(where),
                           timeout=min(timeout, SYNC_TIMEOUT), max_output=1024 * 1024, cwd=str(folder))
        if done.timed_out or done.returncode != 0:
            raise MesError("Installing the project's packages on the server (uv sync --frozen) did not work, so the "
                           "new release was not switched on.",
                           hint="Check that uv.lock is up to date (uv lock) and that the server can reach the "
                           "package index.", code="sync_failed",
                           detail=done.stderr.decode("utf-8", "replace")[-2000:] or None, detail_label="What uv said:")
        project = manifest_mod.load(folder)
        commands: dict[str, dict[str, str]] = {}
        for spec in project.apps.values():
            program = folder / ".venv" / "bin" / spec.cli
            if not program.is_file():
                raise MesError(f'The app "{spec.name}" has no program "{spec.cli}" after installing, so the release '
                               "was not switched on.", hint=f'Name the script "{spec.cli}" in [project.scripts] of '
                               "pyproject.toml.", code="app_missing")
            commands[spec.name] = ask_commands(program, spec.name, env=clean_env("read", where))
        return {"synced": True, "release": number, "apps": sorted(commands),
                "problems": jobs.job_problems(project, commands)}
    if kind == "publish":
        easy_ui = config.get("easy_ui")
        if not easy_ui:
            return {"published": False, "why": "easy-ui is not set up on this server (mes server plan --easy-ui)"}
        done = run_limited([easy_ui, "--json", "--project-dir", str(layout.current_project), "publish"],
                           env=_tool_env(where), timeout=min(timeout, 300.0), max_output=1024 * 1024,
                           cwd=str(layout.current_project))
        if done.timed_out or done.returncode != 0:
            raise MesError("The dashboards could not be published.", hint="Check them with: easy-ui validate",
                           code="publish_failed",
                           detail=(done.stdout or done.stderr).decode("utf-8", "replace")[-2000:] or None)
        try:
            said = json.loads(done.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            said = None
        return {"published": True, "easy_ui": said if isinstance(said, dict) else None}
    raise UsageError("Unknown deploy step.", hint="Use: mes deploy <host>", code="bad_request")
