"""``mes deploy <host>``: install a project on a server you own, signed, with rollback.

On this computer (the client)
-----------------------------
1. Exports the last commit (``HEAD``) of the project straight from git's objects (never the
   working folder, so untracked files, uncommitted changes and links put in place of folders
   cannot slip in) under ``project/``; plus the last commit of each local path dependency named
   in ``[tool.uv.sources]`` (only sibling folders, ``../<name>``, such as
   ``../my-easy-software``), under ``<name>/``, so the same relative paths work on the server
   (a path dependency inside the project, such as ``vendor/<app>``, is already part of the
   project's export). ``apps.lock.json`` holds this computer's paths and is never sent: the
   server writes its own. It builds a wheel of the project and of each of those dependencies
   (the ones inside the project too) here, from that clean export (``uv build --wheel``), under
   ``wheels/``, so the server installs them without building anything. ``MES-RELEASE.json``
   records the project, version, time, commit and whether there were uncommitted (so not
   deployed) changes. Links, missing ``uv.lock`` and folders outside the project are refused.
2. Signs the archive with ``ssh-keygen -Y sign -n mes-deploy`` with the **signing key**
   (Ed25519, best hardware-backed: ``--key``, ``[deploy] key`` in the config file, or
   ``~/.ssh/mes-sign``).
3. Sends it through the server's gate at the ``deploy`` level, logging in with the separate
   **login key** (``--login-key``, ``[deploy] login_key``, or whatever SSH offers):
   ``ssh -T -o BatchMode=yes mes-<project>@<host> '["mes", "deploy", "--signature", "..."]' < archive``.
``--dry-run`` shows every step and sends nothing. ``--releases`` lists what is installed.
``--rollback [--to N]`` signs a short rollback request (namespace ``mes-rollback``: the project,
the release, the time and a random nonce) with the signing key, so a stolen login key alone can
neither install nor switch anything.

On the server (inside the gate, as the project's account)
---------------------------------------------------------
1. Reads the archive from the connection into a file in ``code/incoming/`` that is removed from
   the folder at once; everything after reads that one open file (at most ``MAX_ARCHIVE_BYTES``).
2. Verifies the signature **before unpacking**: ``ssh-keygen -Y verify -n mes-deploy -I <project>
   -f etc/allowed_signers``. The signer list must belong to whoever owns the project's top folder
   (root on a real server) and nobody else may change it; it may only hold Ed25519 keys.
3. Unpacks the same open file with the hardened extractor of ``mes.archive`` into a new
   ``code/releases/<n>/``.
4. Refuses an archive for another project, one older than the newest complete release, or one
   already installed completely. A release that was interrupted (never completed) is removed and
   never blocks sending the same archive again.
5. In the deploy sandbox: refuses a stray ``pyproject.toml``/``uv.toml``/``.python-version`` in
   ``code/`` or ``code/releases/``, then runs ``uv sync --frozen --no-build`` for the third-party
   packages and installs the archive's own wheels with ``uv pip install --no-index --no-build``
   (building on the server only if the administrator allowed it with ``mes server plan
   --allow-server-builds``). In a read sandbox (which cannot change any release): finds every
   app of the release (as ``mes apps sync`` does) and asks each app's program to describe
   itself. The gate itself (no project code) then checks what the sandbox found, writes the
   release's own ``apps.lock.json`` and checks the jobs' commands.
6. Records the release as complete in ``gate/releases/<n>.json`` (a folder only the gate
   writes), switches ``code/current`` to it atomically, publishes the dashboards
   (``easy-ui publish``), and switches back if publishing fails.
7. Keeps the newest ``KEEP_RELEASES`` releases (never the current or the previous one).
8. Says when the jobs or units changed, so the person runs ``mes server plan`` again.

Rollback: ``["mes", "rollback", "--to", N, "--time", T, "--nonce", X, "--signature", S]``. The
signature must be over ``rollback_message(project, N, T, X)`` in the namespace ``mes-rollback``
by an allowed signer, ``T`` within ``ROLLBACK_WINDOW`` seconds of the server's clock, and ``X``
never seen before (kept in ``gate/rollback-seen.json``). Only complete releases are targets.

Public API
----------
``SIGN_NAMESPACE`` (``"mes-deploy"``), ``ROLLBACK_NAMESPACE`` (``"mes-rollback"``),
``SIGNER_NAMESPACES``, ``ROLLBACK_WINDOW``, ``RELEASE_FILE``, ``KEEP_RELEASES``,
``MAX_ARCHIVE_BYTES``, ``DEPLOY_LIMITS``, ``SIGNER_KEY_TYPES``, ``STRAY_CONFIG``.
Client: ``export_release(root, export) -> dict``, ``collect(exported, project, wheels=None) -> (entries, info)``,
``build_wheels(export, deps, out, inside=()) -> list[Path]``, ``build_archive(root, project, out) -> dict``,
``sign(file, key, namespace=SIGN_NAMESPACE) -> str``, ``rollback_message(project, release, when, nonce) -> bytes``,
``destination(host, project) -> str``, ``ssh_argv(dest, request, login_key=None) -> list[str]``, ``run_deploy(...)``.
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
ROLLBACK_NAMESPACE = "mes-rollback"
SIGNER_NAMESPACES = f"{SIGN_NAMESPACE},{ROLLBACK_NAMESPACE}"
ROLLBACK_WINDOW = 300
RELEASE_FILE = "MES-RELEASE.json"
WHEELS_DIR = "wheels"
STRAY_CONFIG = ("pyproject.toml", "uv.toml", ".python-version", "uv.lock")
DEPLOY_LOG = "deploy-log.jsonl"
KEEP_RELEASES = 5
MAX_ARCHIVE_BYTES = 200 * 1024**2
DEPLOY_LIMITS = archive.Limits(max_files=20_000, max_file_bytes=100 * 1024**2, max_total_bytes=500 * 1024**2)
SIGNER_KEY_TYPES = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com")
DEFAULT_KEY = "~/.ssh/mes-sign"
SYNC_TIMEOUT = 900.0
_DEP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}\Z")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}\Z")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\Z")
_SIG_RE = re.compile(r"-----BEGIN SSH SIGNATURE-----\n[A-Za-z0-9+/=\n]{40,3000}-----END SSH SIGNATURE-----\n?")
_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,3}\Z")
_WHEEL_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,200}\.whl\Z")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}\Z")
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


def _export(root: Path, label: str, dest: Path) -> tuple[list[tuple[str, Path]], list[str], bool, str]:
    """Write the files of the last commit (``HEAD``) under ``root`` into the new folder ``dest``,
    straight from git's objects: (files, notes, dirty, commit). Nothing in the working folder is
    read, so an untracked file, an uncommitted change or a link put in place of a folder can
    never end up in the release."""
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        listed = _git(root, "rev-parse", "--is-inside-work-tree")
        raise MesError(
            f"{label} is not in a git repository with a commit, so mes cannot tell which of its files belong to it. "
            "Only committed files are deployed, so nothing private that happens to sit in the folder is sent.",
            hint=(f"Commit it first: cd {root} && git add -A && git commit -m 'First version'" if listed.returncode == 0
                  else f"Put it in git first: cd {root} && git init && git add -A && git commit -m 'First version'"),
            code="not_in_git",
        )
    commit = head.stdout.decode().strip()
    res = _git(root, "ls-tree", "-r", "-z", commit, "--", ".")
    if res.returncode != 0:
        raise MesError(f"git could not list the files of {label}.", hint="Run 'git status' there to see why.",
                       code="git_failed", detail=res.stderr.decode("utf-8", "replace")[-500:] or None)
    wanted: list[tuple[str, str, str]] = []
    notes: list[str] = []
    for item in res.stdout.split(b"\x00"):
        if not item:
            continue
        meta, _, raw_name = item.partition(b"\t")
        mode, kind, sha = meta.decode("ascii", "replace").split(" ")
        name = raw_name.decode("utf-8", "surrogateescape")
        if mode == "120000":
            raise Refused(f"{label}: {name} is a link. Links are never deployed (on the server they could point "
                          "anywhere), so nothing was sent.",
                          hint="Replace the link with the real file, commit, and deploy again.", code="link_in_project")
        if kind == "commit":
            notes.append(f"{label}: {name} is another git repository (a submodule), so it is not sent.")
            continue
        if kind != "blob" or mode not in ("100644", "100755"):
            raise Refused(f"{label}: {name} is not a normal file, so nothing was sent.",
                          hint="Remove it from git.", code="special_file")
        try:
            archive.check_member_path(name)
        except ValueError:
            raise Refused(f"{label}: the file name {name[:80]!r} cannot be deployed, so nothing was sent.",
                          hint="Rename it.", code="bad_name") from None
        if _SECRET_NAMES.search(name):
            notes.append(f"{label}: {name} looks like a file with a password or key, and it is committed to git, "
                         "so it will be sent to the server. If it holds a secret, remove it from git.")
        wanted.append((name, sha, mode))
    files: list[tuple[str, Path]] = []
    dest.mkdir(mode=0o700, parents=True)
    batch = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"], input=b"".join(
        sha.encode() + b"\n" for _, sha, _ in wanted), capture_output=True, timeout=300, check=False)
    if batch.returncode != 0:
        raise MesError(f"git could not read the files of {label}.", hint="Run 'git fsck' there.", code="git_failed")
    data, pos = batch.stdout, 0
    for name, sha, mode in wanted:
        nl = data.index(b"\n", pos)
        got_sha, kind, size = data[pos:nl].decode("ascii").split(" ")
        if got_sha != sha or kind != "blob":
            raise MesError(f"git gave something unexpected for {name}.", hint="Run 'git fsck' there.",
                           code="git_failed")
        start, end = nl + 1, nl + 1 + int(size)
        content, pos = data[start:end], end + 1
        target = dest / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o755 if mode == "100755" else 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        files.append((name, target))
    dirty = bool(_git(root, "status", "--porcelain", "--", ".").stdout.strip())
    if dirty:
        notes.append(f"{label} has changes that are not committed. Only the last commit ({commit[:12]}) is deployed; "
                     "commit the changes to deploy them.")
    return files, notes, dirty, commit


def _inside_path_dependencies(root: Path) -> list[str]:
    """Path dependencies that are folders of the project itself (for example ``vendor/<app>``), as
    paths relative to ``root``. They are part of the project's export, and get their own wheel."""
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    out: list[str] = []
    if not isinstance(sources, dict):
        return out
    for spec in sources.values():
        for s in spec if isinstance(spec, list) else [spec]:
            if not isinstance(s, dict) or "path" not in s:
                continue
            raw = str(s["path"])
            parts = Path(raw).parts
            if os.path.isabs(raw) or not parts or ".." in parts or not (root / raw).resolve().is_relative_to(
                    root.resolve()) or (root / raw).resolve() == root.resolve():
                continue
            if (root / raw / "pyproject.toml").is_file():
                out.append(Path(raw).as_posix())
    return list(dict.fromkeys(out))


def _path_dependencies(root: Path, *, real_root: Path | None = None) -> list[tuple[str, Path]]:
    """Sibling path dependencies named in ``root/pyproject.toml``; their folders are looked up next
    to ``real_root`` (the project's own folder, when ``root`` is an export of it)."""
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
            inside = not os.path.isabs(raw) and parts and ".." not in parts and \
                (root / raw).resolve().is_relative_to(root.resolve())
            if inside:
                continue  # a folder of the project itself (for example vendor/<app>): already packed
            if os.path.isabs(raw) or len(parts) != 2 or parts[0] != ".." or not _DEP_NAME_RE.match(parts[1]) \
                    or parts[1] in ("project", RELEASE_FILE):
                raise Refused(
                    f'pyproject.toml takes "{package}" from the folder {raw!r}. mes can only send local dependencies '
                    "that sit inside the project or right next to it (../<folder>), so the same path works on the "
                    "server.",
                    hint="Move that folder next to the project folder (and use path = \"../<folder>\"), or depend on "
                    "a published version instead.",
                    code="dependency_path",
                )
            folder = (Path(real_root or root) / raw).resolve()
            if not (folder / "pyproject.toml").is_file():
                raise NotFound(f'The dependency "{package}" should be in {folder}, but that folder has no '
                               "pyproject.toml, so nothing was sent.", hint="Check the path in pyproject.toml.")
            out.append((parts[1], folder))
    return out


def export_release(root: Path, export: Path) -> dict[str, Any]:
    """The last commit of the project (``export/project``) and of each sibling path dependency
    (``export/<name>``), from git's objects. Wheels are built from this export, never from the
    working folder."""
    files, notes, dirty, commit = _export(root, "The project", export / paths.RELEASE_PROJECT_DIR)
    if any(n == manifest_mod.LOCK_FILE for n, _ in files):
        files = [(n, p) for n, p in files if n != manifest_mod.LOCK_FILE]
        (export / paths.RELEASE_PROJECT_DIR / manifest_mod.LOCK_FILE).unlink(missing_ok=True)
        notes.append(f"{manifest_mod.LOCK_FILE} is committed to git, but it holds this computer's paths, so it was not "
                     "sent (the server writes its own). Remove it from git and add it to .gitignore.")
    names = {n for n, _ in files}
    for needed in (manifest_mod.MANIFEST_FILE, "pyproject.toml", "uv.lock"):
        if needed not in names:
            raise MesError(
                f"{needed} is not committed to git, so the server could not install the project the same way as here.",
                hint=f"Add and commit it: git add {needed} && git commit"
                + (" (create it first with: uv lock)" if needed == "uv.lock" else ""),
                code="missing_file",
            )
    entries: list[tuple[str, Path]] = [(f"{paths.RELEASE_PROJECT_DIR}/{n}", f) for n, f in files]
    deps = []
    for name, folder in _path_dependencies(export / paths.RELEASE_PROJECT_DIR, real_root=root):
        dep_files, dep_notes, dep_dirty, _ = _export(folder, f"The dependency {name}", export / name)
        notes += dep_notes
        dirty = dirty or dep_dirty
        entries += [(f"{name}/{n}", f) for n, f in dep_files]
        deps.append(name)
    inside = _inside_path_dependencies(export / paths.RELEASE_PROJECT_DIR)
    return {"entries": entries, "notes": notes, "dirty": dirty, "commit": commit, "deps": deps, "inside": inside}


def collect(exported: dict[str, Any], project: manifest_mod.Project,
            wheels: list[Path] | None = None) -> tuple[list[tuple[str, Path | bytes]], dict[str, Any]]:
    entries: list[tuple[str, Path | bytes]] = list(exported["entries"])
    notes = list(exported["notes"])
    now = time.time()
    info = {
        "format": 1,
        "project": project.name,
        "version": project.version,
        "created": _dt.datetime.fromtimestamp(now, _dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "created_ns": time.time_ns(),
        "commit": exported["commit"],
        "dirty": exported["dirty"],
        "files": len(entries),
        "deps": exported["deps"],
        "wheels": [w.name for w in wheels or []],
    }
    for w in wheels or []:
        if not _WHEEL_RE.match(w.name):
            raise Refused(f"uv built a wheel with a name mes does not accept ({w.name[:80]!r}), so nothing was sent.",
                          hint="Check the project's name and version in pyproject.toml.", code="bad_wheel")
        entries.append((f"{WHEELS_DIR}/{w.name}", w))
    entries.insert(0, (RELEASE_FILE, json.dumps(info, indent=2).encode("utf-8") + b"\n"))
    total = sum(p.stat().st_size if isinstance(p, Path) else len(p) for _, p in entries)
    if total > DEPLOY_LIMITS.max_total_bytes or len(entries) > DEPLOY_LIMITS.max_files:
        raise Refused("The project is larger than a deploy may be (500 MB or 20,000 files), so nothing was sent.",
                      hint="Keep data out of the project's git repository: it belongs in the data folder.",
                      code="too_large")
    info["notes"] = notes
    info["bytes"] = total
    return entries, info


def _uv_client() -> str:
    found = shutil.which("uv")
    if not found:
        raise NotFound("Deploying builds the project's wheels with uv, which is not installed on this computer.",
                       hint="Install uv (https://docs.astral.sh/uv/getting-started/installation/), then try again.",
                       code="uv_missing")
    return found


def build_wheels(export: Path, deps: list[str], out: Path, inside: list[str] = ()) -> list[Path]:
    """Wheels of the project, of its sibling path dependencies and of the path dependencies
    inside it (such as an installed app in ``vendor/<app>``), built here from the clean export of
    the last commit, so the server never has to build (and run the build code of) anything, and
    no untracked file can slip into a wheel."""
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    uv = _uv_client()
    folders = [("the project", export / paths.RELEASE_PROJECT_DIR), *((f"the dependency {n}", export / n) for n in deps),
               *((f"the dependency {r}", export / paths.RELEASE_PROJECT_DIR / r) for r in inside)]
    for label, folder in folders:
        try:
            res = subprocess.run([uv, "build", "--wheel", "--no-progress", "--out-dir", str(out), str(folder)],
                                 capture_output=True, timeout=600, check=False, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise TemporaryFailure(f"Building {label} took too long, so nothing was sent.", hint="Try again.") from None
        if res.returncode != 0:
            raise MesError(f"Building a wheel of {label} (uv build) did not work, so nothing was sent.",
                           hint="Run 'uv build --wheel' in that folder to see why.", code="build_failed",
                           detail=res.stderr.decode("utf-8", "replace")[-2000:] or None, detail_label="What uv said:")
    return sorted(p for p in out.iterdir() if p.suffix == ".whl")


def build_archive(root: Path, project: manifest_mod.Project, out: Path, *, wheels: bool = True) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mes-export-") as tmp:
        export = Path(tmp) / "export"
        exported = export_release(Path(root), export)
        built = build_wheels(export, exported["deps"], Path(tmp) / "wheels", exported.get("inside", [])) \
            if wheels else None
        entries, info = collect(exported, project, built)
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


def _login_key(given: str | None) -> Path | None:
    if given:
        return Path(given).expanduser()
    cfg = paths.load_config().get("deploy", {})
    if isinstance(cfg, dict) and isinstance(cfg.get("login_key"), str):
        return Path(cfg["login_key"]).expanduser()
    return None


def _check_key(key: Path) -> None:
    pub = key.with_name(key.name + ".pub") if not key.name.endswith(".pub") else key
    if not key.exists() and not pub.exists():
        raise NeedsPerson(protocol.needs_human(
            "deploy-key",
            "create a signing key for deploys, best on a hardware security key (you touch it for every deploy): "
            f"ssh-keygen -t ed25519-sk -f {key} -C mes-sign (without a hardware key: ssh-keygen -t ed25519 -f {key} "
            "-C mes-sign, with a passphrase you choose)",
            "Every deploy and rollback is signed with your own key, and the server only installs code signed by a "
            "key it knows. Only you can create it. It is not your login key: the server is reached with a separate "
            f"key, so a stolen login key alone cannot install anything. Then add the public half ({pub}) to the "
            f"server with: mes server plan <project> --signer {pub}",
            "mes deploy <host>",
        ))
    try:
        kind = pub.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError, UnicodeDecodeError):
        kind = ""
    if kind not in SIGNER_KEY_TYPES:
        raise Refused(f"The signing key {key} is not an Ed25519 key, and deploys are only signed with Ed25519.",
                      hint=f"Create one with: ssh-keygen -t ed25519-sk -f {DEFAULT_KEY} -C mes-sign", code="key_type")


def sign(archive_path: Path, key: Path, namespace: str = SIGN_NAMESPACE) -> str:
    _check_key(key)
    sig = archive_path.with_name(archive_path.name + ".sig")
    sig.unlink(missing_ok=True)
    try:
        res = subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n", namespace, str(archive_path)],
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
            f"unlock your signing key for this session: ssh-add {key} (a hardware key also asks for a touch)",
            "The deploy is signed with your key, which is protected by a passphrase or a hardware key that only "
            "you have. Once it is in your SSH agent, mes can sign without asking.",
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


def ssh_argv(dest: str, request: list[str], login_key: Path | None = None) -> list[str]:
    key = ["-i", str(login_key), "-o", "IdentitiesOnly=yes"] if login_key else []
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
            *key, "-e", "none", "--", dest, json.dumps(request, ensure_ascii=True)]


def rollback_message(project: str, release: int, when: int, nonce: str) -> bytes:
    """The exact text a rollback signature is over: project, target release, time and nonce."""
    return f"mes-rollback 1\nproject {project}\nrelease {int(release)}\ntime {int(when)}\nnonce {nonce}\n".encode("ascii")


def _send(dest: str, request: list[str], archive_path: Path | None, timeout: float,
          login_key: Path | None = None) -> dict[str, Any]:
    # "--json": the gate then answers with JSON even when it refuses before reading the request.
    argv = ssh_argv(dest, request if "--json" in request else [*request, "--json"], login_key)
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


def _rollback_target(dest: str, to: int | None, timeout: float, login_key: Path | None) -> int:
    if to is not None:
        return to
    listed = _send(dest, ["mes", "releases"], None, timeout, login_key)
    current = listed.get("current")
    older = [r.get("release") for r in listed.get("releases") or []
             if isinstance(r.get("release"), int) and (current is None or r["release"] < current)]
    if not older:
        raise NotFound("There is no earlier release to switch back to.",
                       hint="See what is installed with: mes deploy <host> --releases", code="no_release")
    return max(older)


def run_deploy(host: str, *, project_dir: str | None, key: str | None, dry_run: bool, rollback: bool,
               to: int | None, releases: bool, timeout: float, login_key: str | None = None) -> dict[str, Any]:
    from mes.jobs import load_project

    root, project = load_project(project_dir)
    dest = destination(host, project.name)
    login = _login_key(login_key)
    if releases:
        request = ["mes", "releases"]
        if dry_run:
            return {"dry_run": True, "steps": [{"step": "ask the server", "command": " ".join(ssh_argv(dest, request, login))}]}
        return _send(dest, request, None, timeout, login)
    key_path = _key_path(key)
    if rollback:
        if dry_run:
            return {"dry_run": True, "steps": [
                {"step": "find the release", "what": f"release {to}" if to else "ask the server for the one before"},
                {"step": "sign", "what": f"a rollback request (project, release, time, nonce) with {key_path}, "
                 f"namespace {ROLLBACK_NAMESPACE}"},
                {"step": "send", "command": " ".join(ssh_argv(dest, ["mes", "rollback", "--to", "<N>", "--time", "<T>",
                                                                     "--nonce", "<nonce>", "--signature", "<signature>"],
                                                              login))}],
                "message": "Nothing was signed or sent: these are the steps 'mes deploy --rollback' would take."}
        target = _rollback_target(dest, to, timeout, login)
        when, nonce = int(time.time()), secrets.token_hex(16)
        with tempfile.TemporaryDirectory(prefix="mes-rollback-") as tmp:
            msg = Path(tmp) / "rollback.txt"
            msg.write_bytes(rollback_message(project.name, target, when, nonce))
            signature = sign(msg, key_path, ROLLBACK_NAMESPACE)
        return _send(dest, ["mes", "rollback", "--to", str(target), "--time", str(when), "--nonce", nonce,
                            "--signature", signature], None, timeout, login)
    with tempfile.TemporaryDirectory(prefix="mes-deploy-") as tmp:
        out = Path(tmp) / f"{project.name}.tar.gz"
        info = build_archive(root, project, out)
        sign_cmd = f"ssh-keygen -Y sign -f {key_path} -n {SIGN_NAMESPACE} {out.name}"
        steps = [
            {"step": "pack", "what": f"{info['files']} files ({info['bytes']} bytes) of the last commit"
             + (f", with the local dependencies {', '.join(info['deps'])}" if info["deps"] else "")
             + (f", and the wheels {', '.join(info['wheels'])} built here" if info["wheels"] else ""),
             "archive_sha256": info["sha256"]},
            {"step": "sign", "command": sign_cmd, "why": "the server installs only code signed by a key it knows"},
            {"step": "send", "command": " ".join(ssh_argv(dest, ["mes", "deploy", "--signature", "<signature>"], login))
             + " < archive"},
            {"step": "on the server", "what": [
                "verify the signature against the project's allowed signers, before unpacking anything",
                "unpack the same open file with the hardened extractor into a new release folder",
                "uv sync --frozen --no-build in the deploy sandbox, and install the wheels built here",
                "find every app and ask each to describe itself in a read sandbox; then write the release's own "
                "apps.lock.json and check the jobs' commands",
                "switch 'current' to the new release in one step",
                "publish the dashboards (switch back if that fails)",
                f"keep the newest {KEEP_RELEASES} releases",
            ]},
        ]
        if dry_run:
            return {"dry_run": True, "project": project.name, "to": dest, "steps": steps, "notes": info["notes"],
                    "message": "Nothing was signed or sent: these are the steps 'mes deploy' would take."}
        signature = sign(out, key_path)
        answer = _send(dest, ["mes", "deploy", "--signature", signature], out, timeout, login)
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
                        rollback=args.rollback, to=args.to, releases=args.releases, timeout=args.timeout,
                        login_key=args.login_key)
    return result if args.json else _deploy_text(result)


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "deploy",
        help="Install the project on a server you own (signed), roll back, or list releases.",
        description="Pack the project's git-tracked files and wheels, sign them with your signing key and send them "
        "through the server's gate with your login key. The server verifies the signature before unpacking. "
        "See: mes guide deploy",
    )
    p.add_argument("host", help="the server (an SSH name such as my-server; the account mes-<project> is used)")
    p.add_argument("--project-dir", default=None, metavar="DIR", help="the project's folder (default: here)")
    p.add_argument("--key", default=None, metavar="PATH",
                   help=f"the signing key, best hardware-backed (default: {DEFAULT_KEY})")
    p.add_argument("--login-key", default=None, metavar="PATH",
                   help="the SSH key that logs in to mes-<project> (default: [deploy] login_key, or what ssh offers); "
                   "never the signing key")
    p.add_argument("--dry-run", action="store_true", help="show every step and send nothing")
    p.add_argument("--rollback", action="store_true", help="switch the server back to the previous release (signed)")
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


MAX_SIGNER_LINES = 100


def check_signers(path: Path, project: str, base: Path) -> list[str]:
    """Every line of the file is checked (a longer file is refused as a whole), and only the
    checked lines, rebuilt from their parts, are ever handed to ssh-keygen."""
    _check_root_file(path, base, "list of allowed signers")
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    if len(raw_lines) > MAX_SIGNER_LINES:
        raise Refused(f"The list of allowed signers has more than {MAX_SIGNER_LINES} lines, so the server refuses "
                      "to use it.", hint="Run 'mes server plan' and its script again.", code="untrusted_file")
    lines = []
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        words = line.split()
        if len(words) < 4 or words[0] != project or words[1] != f'namespaces="{SIGNER_NAMESPACES}"':
            raise Refused("The list of allowed signers has a line that is not exactly '<project> "
                          f'namespaces="{SIGNER_NAMESPACES}" <key>\' for this project, so the server refuses to use it.',
                          hint="Run 'mes server plan' and its script again.", code="untrusted_file")
        if words[2] not in SIGNER_KEY_TYPES or len(words) > 5:
            raise Refused("The list of allowed signers has a key that is not a plain Ed25519 key, so the server "
                          "refuses to use it.", hint="Use Ed25519 keys only.", code="key_type")
        if not _B64_RE.match(words[3]) or not 60 <= len(words[3]) <= 400:
            raise Refused("The list of allowed signers has a key that is not written as a plain key, so the server "
                          "refuses to use it.", hint="Run 'mes server plan' and its script again.",
                          code="untrusted_file")
        lines.append(f'{project} namespaces="{SIGNER_NAMESPACES}" {words[2]} {words[3]}')
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
    for key in ("mes", "mes_python", "uv", "easy_ui"):
        if key in data and data[key] is not None:
            data[key] = units.check_tool_path(str(data[key]), f"the path of {key}")
    return data


@contextlib.contextmanager
def _deploy_lock(where: Any):
    folder = where.gate_dir  # on a server: gate/, which no job and no sandboxed request can write
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


# --------------------------------------------------------------------------- releases and their records


def _release_numbers(layout: paths.ServerLayout) -> list[int]:
    """Every release folder, complete or not."""
    if not layout.releases.is_dir():
        return []
    out = []
    for p in layout.releases.iterdir():
        if p.name.isdigit() and p.is_dir() and not p.is_symlink():
            out.append(int(p.name))
    return sorted(out)


def _record_path(layout: paths.ServerLayout, number: int) -> Path:
    return layout.release_records / f"{int(number)}.json"


def _release_record(layout: paths.ServerLayout, number: int) -> dict[str, Any] | None:
    """The gate's own record of a release that was installed completely (``None`` otherwise).
    Records live in gate/, which nothing that runs project code can write."""
    try:
        fd = os.open(_record_path(layout, number), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        raw = f.read(64 * 1024 + 1)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("release") == number else None


def _complete(layout: paths.ServerLayout) -> dict[int, dict[str, Any]]:
    out = {}
    for n in _release_numbers(layout):
        rec = _release_record(layout, n)
        if rec is not None:
            out[n] = rec
    return out


def _write_record(layout: paths.ServerLayout, number: int, record: dict[str, Any]) -> None:
    folder = layout.release_records
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = folder / f".{number}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({**record, "release": number}, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _record_path(layout, number))


def _drop_incomplete(layout: paths.ServerLayout) -> list[int]:
    """Releases that were never completed (an interrupted deploy): removed, so they never block
    sending the same archive again. The running one is never touched."""
    current = _current_number(layout)
    removed = []
    for n in _release_numbers(layout):
        if n != current and _release_record(layout, n) is None:
            shutil.rmtree(layout.releases / str(n), ignore_errors=True)
            removed.append(n)
    if layout.releases.is_dir():
        for p in layout.releases.iterdir():
            if p.name.startswith(".partial-"):
                shutil.rmtree(p, ignore_errors=True)
    return removed


def _current_number(layout: paths.ServerLayout) -> int | None:
    try:
        target = os.readlink(layout.current)
    except OSError:
        return None
    m = re.fullmatch(r"releases/([0-9]+)", target)
    return int(m.group(1)) if m else None


def _switch(layout: paths.ServerLayout, number: int) -> None:
    if _release_record(layout, number) is None:
        raise Refused(f"Release {number} was never installed completely, so the server will not switch to it.",
                      hint="Pick another release, or deploy again.", code="incomplete_release")
    tmp = layout.code / f".current-{secrets.token_hex(4)}"
    os.symlink(f"releases/{number}", tmp)
    os.replace(tmp, layout.current)


def _record(layout: paths.ServerLayout, entry: dict[str, Any]) -> None:
    line = json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n"
    folder = layout.gate
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(folder / DEPLOY_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, line.encode("ascii"))
    finally:
        os.close(fd)


def refuse_stray_config(layout: paths.ServerLayout) -> None:
    """uv looks for settings and workspaces in the folders above a project; none may be there."""
    for folder in (layout.code, layout.releases):
        for name in STRAY_CONFIG:
            if os.path.lexists(folder / name):
                raise Refused(
                    f"There is a {name} in {folder.name}/ on the server, which uv would read as part of the project's "
                    "settings, so nothing was installed. It does not belong there, and something put it there.",
                    hint=f"Ask the owner to look at it and remove it: {folder / name}", code="stray_config")


# --------------------------------------------------------------------------- receiving


def _read_stdin(stream: BinaryIO | None, f: BinaryIO) -> tuple[int, str]:
    if stream is None:
        raise UsageError("No archive came with the deploy request.", hint="Use: mes deploy <host>")
    h = hashlib.sha256()
    total = 0
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
    f.flush()
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


def verify_signature(data: Any, signature: str, signers: Path | list[str], project: str, *,
                     namespace: str = SIGN_NAMESPACE, sig_dir: Path | None = None) -> str:
    """``ssh-keygen -Y verify``; returns the signing key's fingerprint. Raises ``Refused``.

    ``data`` is an open binary file (read from its start) or a path. ``signers`` is the checked
    lines of ``check_signers`` (written to a private file for ssh-keygen) or a file."""
    if not isinstance(signature, str) or not _SIG_RE.fullmatch(signature):
        raise Refused("The signature is not an OpenSSH signature, so nothing was installed or changed.",
                      hint="Use 'mes deploy', which signs with ssh-keygen.", code="bad_signature")
    folder = sig_dir or (Path(data).parent if not hasattr(data, "read") else Path(tempfile.gettempdir()))
    sig = folder / f".sig-{secrets.token_hex(8)}"
    fd = os.open(sig, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(signature)
    checked: Path | None = None
    if isinstance(signers, list):
        checked = folder / f".signers-{secrets.token_hex(8)}"
        fd = os.open(checked, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write("".join(line + "\n" for line in signers))
        signers = checked
    try:
        ctx = contextlib.nullcontext(data) if hasattr(data, "read") else open(data, "rb")
        with ctx as stream:
            stream.seek(0)
            res = subprocess.run([_ssh_keygen(), "-Y", "verify", "-f", str(signers), "-I", project,
                                  "-n", namespace, "-s", str(sig)], stdin=stream, capture_output=True,
                                 timeout=120, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C"})
            stream.seek(0)
    finally:
        sig.unlink(missing_ok=True)
        if checked is not None:
            checked.unlink(missing_ok=True)
    out = res.stdout.decode("utf-8", "replace")
    if res.returncode != 0 or not out.startswith(f'Good "{namespace}" signature for {project} with '):
        raise Refused(
            "The request is not signed by a key this server trusts for the project, so nothing was unpacked, "
            "installed or changed.",
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
    if not isinstance(deps, list) or not all(isinstance(d, str) and _DEP_NAME_RE.match(d)
                                             and d not in ("project", WHEELS_DIR) for d in deps):
        raise Refused("The release description lists dependencies mes does not accept.", hint="Deploy with 'mes deploy'.",
                      code="bad_release")
    wheels = info.get("wheels", [])
    if not isinstance(wheels, list) or not all(isinstance(w, str) and _WHEEL_RE.match(w) for w in wheels):
        raise Refused("The release description lists wheels mes does not accept.", hint="Deploy with 'mes deploy'.",
                      code="bad_release")
    top = {p.name for p in folder.iterdir()}
    expected = {RELEASE_FILE, paths.RELEASE_PROJECT_DIR, *deps} | ({WHEELS_DIR} if wheels else set())
    if top != expected:
        raise Refused("The archive holds other things than the project and the dependencies it lists, so it was "
                      "not installed.", hint="Deploy with 'mes deploy'.", code="bad_release")
    if wheels and sorted(p.name for p in (folder / WHEELS_DIR).iterdir()) != sorted(wheels):
        raise Refused("The archive's wheels are not the ones its description lists, so it was not installed.",
                      hint="Deploy with 'mes deploy'.", code="bad_release")
    created = info.get("created_ns")
    if not isinstance(created, int):
        raise Refused("The release description has no creation time.", hint="Deploy with 'mes deploy'.",
                      code="bad_release")
    project = manifest_mod.load_file(folder / paths.RELEASE_PROJECT_DIR)
    if project.name != layout.project:
        raise Refused(f"The archive's project.toml is for \"{project.name}\", not \"{layout.project}\".",
                      hint="Deploy the right project.", code="wrong_project")
    if not (folder / paths.RELEASE_PROJECT_DIR / "uv.lock").is_file():
        raise Refused("The archive has no uv.lock, so the server cannot install exactly the same packages.",
                      hint="Run 'uv lock' and commit uv.lock.", code="bad_release")
    for n, other in _complete(layout).items():
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
        now = units_fingerprint(project, layout, config["mes"], config.get("easy_ui"),
                                config.get("mes_python") or units.DEFAULT_MES_PYTHON)
    except (MesError, KeyError):
        return None
    if now == planned:
        return None
    return ("The jobs, schedules or dashboards of this version differ from what the server was set up for, so the "
            "server keeps running the old schedules. Run 'mes server plan "
            f"{layout.project}' again and run its script with sudo on the server.")


def units_fingerprint(project: manifest_mod.Project, layout: paths.ServerLayout, mes: str, easy_ui: str | None,
                      mes_python: str = units.DEFAULT_MES_PYTHON) -> str:
    files = units.render_project(project, layout=layout, mes=mes, mes_python=mes_python, easy_ui=easy_ui)
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\0" + files[name].encode("utf-8") + b"\0")
    return h.hexdigest()


def _prune(layout: paths.ServerLayout, keep: set[int]) -> list[int]:
    numbers = sorted(_complete(layout))
    newest = set(numbers[-KEEP_RELEASES:])
    removed = []
    for n in numbers:
        if n in newest or n in keep:
            continue
        _record_path(layout, n).unlink(missing_ok=True)
        shutil.rmtree(layout.releases / str(n), ignore_errors=True)
        removed.append(n)
    return removed


def receive(where: Any, signature: str, stdin: BinaryIO | None, *,
            run_child: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    layout: paths.ServerLayout = where.layout
    signers = check_signers(layout.allowed_signers, layout.project, layout.base)
    config = read_server_config(layout)
    for folder in (layout.incoming, layout.releases):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    refuse_stray_config(layout)
    _drop_incomplete(layout)
    name = f"{int(time.time())}-{secrets.token_hex(6)}.tar.gz"
    incoming = layout.incoming / name
    partial: Path | None = None
    # One open file from the first byte to the last step: it leaves the folder at once, so nothing
    # can swap the archive between checking the signature and unpacking it.
    fd = os.open(incoming, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.unlink(incoming)
    with os.fdopen(fd, "w+b") as held:
        try:
            size, sha = _read_stdin(stdin, held)
            fingerprint = verify_signature(held, signature, signers, layout.project, sig_dir=layout.incoming)
            numbers = _release_numbers(layout)
            if layout.release_records.is_dir():
                numbers += [int(p.stem) for p in layout.release_records.glob("*.json") if p.stem.isdigit()]
            number = (max(numbers) + 1) if numbers else 1
            partial = layout.releases / f".partial-{number}-{secrets.token_hex(4)}"
            counts = archive.extract(held, partial, limits=DEPLOY_LIMITS)
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
    try:
        synced = run_child({"kind": "sync", "release": number})
        described = run_child({"kind": "describe", "release": number})
        from mes import apps

        commands = described.get("commands") if isinstance(described.get("commands"), dict) else {}
        new_project = write_release_lock(final / paths.RELEASE_PROJECT_DIR, described)
        problems = apps._command_problems(new_project, commands)
    except BaseException:
        shutil.rmtree(final, ignore_errors=True)
        raise
    _write_record(layout, number, {k: info.get(k) for k in (
        "project", "version", "created", "created_ns", "commit", "dirty", "archive_sha256", "signer", "received",
        "deps", "wheels")})
    previous = _current_number(layout)
    _switch(layout, number)
    project = manifest_mod.load(layout.current_project)
    published = _publish(layout, project, run_child)
    if published.get("failed"):
        if previous is not None and _release_record(layout, previous) is not None:
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
    notes += problems + list(synced.get("notes") or [])
    return {
        "deployed": True,
        "project": layout.project,
        "release": number,
        "previous": previous,
        "version": info.get("version"),
        "commit": info.get("commit"),
        "files": counts["files"],
        "signer": fingerprint,
        "built_on_server": bool(synced.get("built_on_server")),
        "dashboards": published,
        "removed_releases": removed,
        "needs_plan": any("mes server plan" in n for n in notes),
        "notes": notes,
        "message": f'Release {number} of "{layout.project}" is installed and running.'
        + (f" Release {previous} is kept, so you can switch back with: mes deploy <host> --rollback" if previous else ""),
    }


def write_release_lock(folder: Path, described: dict[str, Any]) -> manifest_mod.Project:
    """Write a new release's ``apps.lock.json`` from what the read sandbox found (``describe``).
    Runs in the gate itself, so it runs no project code: it only checks that every app is where
    an app of this release may be (an in-project app in ``apps/<package>``, an installed one in
    the release's own ``.venv``), reads each ``app.toml`` fresh and checks the names the sandbox
    gave against them. Returns the composed project."""
    from mes import apps

    pf = manifest_mod.load_file(folder)
    root = pf.root
    assert root is not None
    entries = described.get("apps")
    bad = MesError("The new release's apps were described in a way mes does not accept, so it was not switched on.",
                   hint="Deploy again with a current mes; if it keeps happening, run mes server check.",
                   code="bad_describe")
    if not isinstance(entries, list) or [e.get("package") if isinstance(e, dict) else None
                                         for e in entries] != list(pf.packages):
        raise bad
    venv = root / ".venv"
    located: list[manifest_mod.Located] = []
    for e in entries:
        path, in_project = e.get("path"), e.get("in_project")
        if not isinstance(path, str) or not isinstance(in_project, bool) or not os.path.isabs(path) \
                or os.path.normpath(path) != path or os.path.realpath(path) != path:
            raise bad
        folder_ = Path(path)
        if in_project and folder_ != root / manifest_mod.APPS_DIR / e["package"]:
            raise bad
        if not in_project and not folder_.is_relative_to(venv):
            raise bad
        located.append(manifest_mod.Located(e["package"], folder_, in_project))
    project = manifest_mod.compose(pf, located)
    for e in entries:
        spec = next(s for s in project.apps.values() if s.package == e["package"])
        if (e.get("name"), e.get("program")) != (spec.name, spec.program):
            raise bad
    apps.write_lock(root, pf, located, {s.package: (s.name, s.program) for s in project.apps.values()})
    return manifest_mod.load(root)


def _publish(layout: paths.ServerLayout, project: manifest_mod.Project, run_child) -> dict[str, Any]:
    if not project.has_dashboards:
        return {"published": False, "why": "the project has no dashboards"}
    try:
        return run_child({"kind": "publish"})
    except MesError as e:
        return {"failed": True, "detail": e.detail or e.message}


# --------------------------------------------------------------------------- rollback (signed)


def _parse_rollback(args: list[str]) -> tuple[int, int, str, str]:
    bad = UsageError('A rollback request is ["mes", "rollback", "--to", "N", "--time", "T", "--nonce", "X", '
                     '"--signature", "S"], signed with the signing key.',
                     hint="Use: mes deploy <host> --rollback [--to N]", code="bad_request")
    if len(args) != 8 or args[0::2] != ["--to", "--time", "--nonce", "--signature"]:
        raise bad
    to, when, nonce, signature = args[1], args[3], args[5], args[7]
    if not to.isdigit() or len(to) > 9 or not when.isdigit() or len(when) > 12 or not _NONCE_RE.match(nonce):
        raise bad
    return int(to), int(when), nonce, signature


def _seen_rollbacks(layout: paths.ServerLayout) -> dict[str, float]:
    try:
        fd = os.open(layout.gate / "rollback-seen.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return {}
    with os.fdopen(fd, "rb") as f:
        raw = f.read(1024 * 1024)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, (int, float))} \
        if isinstance(data, dict) else {}


def _remember_rollback(layout: paths.ServerLayout, nonce: str, now: float) -> None:
    seen = {k: v for k, v in _seen_rollbacks(layout).items() if abs(now - v) <= 2 * ROLLBACK_WINDOW}
    seen[nonce] = now
    layout.gate.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = layout.gate / f".rollback-seen.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        json.dump(seen, f)
    os.replace(tmp, layout.gate / "rollback-seen.json")


def check_rollback(layout: paths.ServerLayout, args: list[str], *, now: float | None = None) -> tuple[int, str]:
    """A signed rollback request: right project and release, fresh, never seen. Returns
    ``(release, signer fingerprint)``; raises ``Refused`` otherwise."""
    to, when, nonce, signature = _parse_rollback(args)
    now = time.time() if now is None else now
    if abs(now - when) > ROLLBACK_WINDOW:
        raise Refused("The rollback request is too old (or this server's clock is wrong), so nothing was switched.",
                      hint="Send it again: mes deploy <host> --rollback", code="stale_request")
    if nonce in _seen_rollbacks(layout):
        raise Refused("This signed rollback request was already used once, so it was refused.",
                      hint="Send a new one: mes deploy <host> --rollback", code="replay")
    signers = check_signers(layout.allowed_signers, layout.project, layout.base)
    layout.gate.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=layout.gate) as msg:
        msg.write(rollback_message(layout.project, to, when, nonce))
        msg.flush()
        fingerprint = verify_signature(msg, signature, signers, layout.project,
                                       namespace=ROLLBACK_NAMESPACE, sig_dir=layout.gate)
    _remember_rollback(layout, nonce, now)
    return to, fingerprint


def rollback(where: Any, args: list[str], *, run_child) -> dict[str, Any]:
    layout: paths.ServerLayout = where.layout
    to, fingerprint = check_rollback(layout, args)
    current = _current_number(layout)
    complete = _complete(layout)
    if to not in complete:
        if to in _release_numbers(layout):
            raise Refused(f"Release {to} was never installed completely, so the server will not switch to it.",
                          hint="Pick another release.", code="incomplete_release")
        raise NotFound(f"There is no release {to} on the server.", hint="See: mes deploy <host> --releases",
                       code="no_release")
    if to == current:
        return {"rolled_back": False, "release": to, "message": f"Release {to} is already the one running."}
    _switch(layout, to)
    project = manifest_mod.load(layout.current_project)
    published = _publish(layout, project, run_child)
    if published.get("failed") and current is not None and current in complete:
        _switch(layout, current)
        raise MesError(f"Switching to release {to} failed because its dashboards could not be published, so the "
                       f"server stayed on release {current}.", hint="Pick another release.", code="publish_failed")
    _record(layout, {"time": _now(), "event": "rolled_back", "release": to, "from": current, "signer": fingerprint})
    config = read_server_config(layout)
    notes = [n for n in (_units_note(layout, project, config),) if n]
    return {"rolled_back": True, "release": to, "from": current, "signer": fingerprint, "dashboards": published,
            "notes": notes, "message": f'"{layout.project}" now runs release {to} again (it ran release {current}).'}


def list_releases(layout: paths.ServerLayout) -> dict[str, Any]:
    current = _current_number(layout)
    rows = []
    for n, info in sorted(_complete(layout).items(), reverse=True):
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
            "Some request sandboxes are not set up, so those requests are refused. Run 'mes server plan' and its "
            "script again."}


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
        with _deploy_lock(where):
            return rollback(where, args, run_child=run_child)
    if args:
        raise UsageError(f'"mes {command}" takes no options.', hint="Use: mes deploy <host> --releases", code="bad_request")
    if command == "releases":
        return list_releases(where.layout)
    if command == "selftest":
        from mes import selftest

        return selftest.run_inside(where, run_child)
    return _status(where)


# --------------------------------------------------------------------------- steps in the sandbox


def _tool_env(where: Any) -> dict[str, str]:
    layout: paths.ServerLayout = where.layout
    from mes.gate import clean_env

    env = clean_env("write", where)
    env.update({
        # The tools' own folders are in code/, which jobs and the write gate see read-only, and uv
        # reads no settings file: nothing a job can write changes what the next deploy runs.
        "UV_CACHE_DIR": str(layout.uv_cache),
        "UV_PYTHON_INSTALL_DIR": str(layout.uv_python),
        "UV_NO_CONFIG": "1",
        "UV_LINK_MODE": "copy",
        "UV_NO_PROGRESS": "1",
        "HOME": "/nonexistent",
        "PYTHONNOUSERSITE": "1",
        "MES_ROOT": str(layout.root),  # easy-ui then uses <root>/<project>/ui and .../data
    })
    return env


def _path_packages(folder: Path) -> list[str]:
    """Names of the packages that come from local folders (the project's own and its siblings):
    they are installed from the archive's wheels, never built on the server."""
    try:
        data = tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    out = []
    if isinstance(sources, dict):
        for package, spec in sources.items():
            specs = spec if isinstance(spec, list) else [spec]
            if any(isinstance(s, dict) and "path" in s for s in specs) and _DEP_NAME_RE.match(str(package)):
                out.append(str(package))
    return out


def _uv_failed(done, *, built: bool) -> MesError:
    return MesError(
        "Installing the project's packages on the server did not work, so the new release was not switched on.",
        hint="Check that uv.lock is up to date (uv lock) and that the server can reach the package index."
        + ("" if built else " If a package only exists as source code, it would have to be built on the server, which "
           "is switched off: allow it with 'mes server plan <project> --allow-server-builds' and its script."),
        code="sync_failed",
        detail=done.stderr.decode("utf-8", "replace")[-2000:] or None, detail_label="What uv said:")


def child_step(where: Any, req: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    """A deploy step that downloads packages or publishes: called inside the deploy sandbox
    (``mes gate child --level deploy``), or by the gate itself where there is none (tests)."""
    from mes.gate import run_limited

    layout: paths.ServerLayout = where.layout
    config = read_server_config(layout)
    kind = req.get("kind")
    if kind == "sync":
        number = req.get("release")
        if not isinstance(number, int) or number < 1:
            raise UsageError("Bad release number.", hint="Use: mes deploy <host>", code="bad_request")
        release = layout.releases / str(number)
        folder = release / paths.RELEASE_PROJECT_DIR
        if folder.is_symlink() or not folder.is_dir():
            raise NotFound(f"Release {number} is not on the server.", hint="Deploy again.", code="no_release")
        refuse_stray_config(layout)
        uv = config.get("uv")
        if not uv:
            raise NotFound("The server settings do not say where uv is, so the project cannot be installed.",
                           hint="Run 'mes server plan' with --uv <path> and its script again.", code="uv_missing")
        allow_builds = config.get("allow_builds") is True
        wheels = sorted(str(p) for p in (release / WHEELS_DIR).glob("*.whl")) if (release / WHEELS_DIR).is_dir() else []
        env = _tool_env(where)
        limit = min(timeout, SYNC_TIMEOUT)
        notes: list[str] = []
        built = False
        if wheels:
            skip = ["--no-install-project"]
            for pkg in _path_packages(folder):
                skip += ["--no-install-package", pkg]
            done = run_limited([uv, "sync", "--frozen", "--no-progress", "--no-build", *skip], env=env, timeout=limit,
                               max_output=1024 * 1024, cwd=str(folder))
            if (done.timed_out or done.returncode != 0) and allow_builds:
                done = run_limited([uv, "sync", "--frozen", "--no-progress", *skip], env=env, timeout=limit,
                                   max_output=1024 * 1024, cwd=str(folder))
                built = True
                notes.append("Some packages had to be built on the server (allowed by its plan).")
            if done.timed_out or done.returncode != 0:
                raise _uv_failed(done, built=allow_builds)
            done = run_limited([uv, "pip", "install", "--no-progress", "--python", str(folder / ".venv" / "bin" / "python"),
                                "--no-deps", "--no-index", "--no-build", "--offline", *wheels], env=env, timeout=limit,
                               max_output=1024 * 1024, cwd=str(folder))
            if done.timed_out or done.returncode != 0:
                raise _uv_failed(done, built=True)
        elif allow_builds:
            done = run_limited([uv, "sync", "--frozen", "--no-progress"], env=env, timeout=limit,
                               max_output=1024 * 1024, cwd=str(folder))
            built = True
            if done.timed_out or done.returncode != 0:
                raise _uv_failed(done, built=True)
        else:
            raise Refused("The release has no wheels, and this server does not build packages, so it was not installed.",
                          hint="Deploy with a current 'mes deploy', which builds the wheels on your computer.",
                          code="no_wheels")
        return {"synced": True, "release": number, "built_on_server": built, "notes": notes}
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
