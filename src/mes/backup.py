"""``mes backup`` and ``mes restore``: a copy of a project's data and state, checked twice.

A backup is one archive (gzip ustar) holding::

    data/...          the project's data folder (without the store, when the store is inside it)
    state/...         the project's state folder (without its run log and the gate's log)
    store/...         a consistent snapshot of the project's my-bucket-storage store, if it has one
    MES-BACKUP.json   project, time, and every file with its size and SHA-256

and, next to it, ``<archive>.sig``: an ``ssh-keygen -Y sign -n mes-backup`` signature made with
the person's signing key (the same key as for deploys: ``--key``, ``[deploy] key`` in the config
file, or ``~/.ssh/mes-sign``) whenever one is set up (``--no-sign`` leaves it out).

Only regular files are copied; links and special files are skipped and listed. Every file is
opened without following links, one folder at a time from the project's own folder, and hashed
from the same open file that is packed. The store is never copied file by file while it may be
in use: ``mybucket snapshot`` makes a consistent copy, ``mybucket verify`` checks it, and only
then is it packed. Without mybucket, a project with a store is not backed up (unless
``--no-store``), and the message says why.

Who may run it: only the account that owns the project's folders, never root (a root backup or
restore would follow whatever the project's own code left in those folders).

Verify step: after writing, the archive is read back and every file's size and SHA-256 are
compared with ``MES-BACKUP.json``. That proves the archive is complete and unchanged (integrity);
only the signature proves who made it (origin). ``mes restore`` checks the signature first (a
signed backup is refused unless it verifies against a trusted key: ``--signer``, the public half
of the person's signing key, and on a server the project's deploy signers), then the list of
files, while unpacking into a private folder inside each destination folder, before anything is
put in place. An unsigned backup is refused when a signing key is known here, unless
``--allow-unsigned``; otherwise the question says plainly that only its integrity was checked,
and gives its SHA-256.

Where backups go: ``--to DIR``, or by default ``<state home>/../mes-backups/<project>/``
(normally ``~/.local/state/mes-backups/<project>/``); on a server (``--server-root``)
``/var/lib/mes/<project>/backups/``, a folder the root script makes for the project's account
that no job or gate request can see (never ``state/``, which jobs can change). The folder is
private (0700) and each archive is ``<project>-<UTC time>.tar.gz``, mode 0600. mes refuses to
write a backup into the project's own data, state, store or code folders.

Restoring never deletes anything: when a folder already has contents, ``--replace`` moves them
into ``<folder>/.before-restore-<time>/`` first; without it the restore is refused. Every move is
a rename relative to an open folder, and never over an existing name. The run log
(``state/runs.jsonl``), the gate's log (``state/gate/``) and mes's own working names are never
restored, never moved aside and never written into. Without ``--yes`` it answers
``needs_input`` (recommended answer: no) and says exactly what would be put in place and moved
aside, in mes's own words only.

Public API
----------
``BACKUP_INFO`` (``"MES-BACKUP.json"``), ``RESTORE_LIMITS``, ``BACKUP_NAMESPACE`` (``"mes-backup"``).
``Target``: ``name``, ``data``, ``state``, ``store``, ``project`` (Project | None), ``root``
    (project folder | None), ``server`` (bool), ``default_dest``, ``base`` (server folder | None).
``resolve(project=None, *, project_dir=None, server_root=None) -> Target``
``backup(target, *, dest=None, include_store=True, key=None, sign=True) -> dict``
``verify_archive(path_or_file, *, limits=RESTORE_LIMITS) -> dict``  re-reads and checks every hash.
``restore(target, archive, *, yes=False, replace=False, json_mode=False, signature=None, signers=(),
    allow_unsigned=False) -> dict``
``register(subparsers)``  adds ``mes backup`` and ``mes restore``.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import errno
import gzip
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from mes import archive, paths, protocol, siblings
from mes import manifest as manifest_mod
from mes.app import check_allowed
from mes.errors import MesError, NotFound, Refused, UsageError

BACKUP_INFO = "MES-BACKUP.json"
BACKUP_NAMESPACE = "mes-backup"
PARTS = ("data", "state", "store")
RESTORE_LIMITS = archive.Limits(max_files=500_000, max_file_bytes=8 * 1024**3 - 1, max_total_bytes=512 * 1024**3)
ASIDE_PREFIX = ".before-restore-"
TEMP_PREFIX = ".mes-restore-"
WORK_PREFIX = ".mes-backup-"
CHUNK = 1024 * 1024
MAX_SIGNATURE_BYTES = 16 * 1024
# Never copied into a backup, never restored, never moved aside, never written into: the run
# log (and its anchor), the gate's log and locks, and the old place of server backups.
STATE_RESERVED = frozenset({"runs.jsonl", "runs.jsonl.head", "gate", "backups"})
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_KEY_B64_RE = re.compile(r"^[A-Za-z0-9+/]{60,400}={0,3}\Z")
_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/=]{20,80}\Z")


@dataclass(frozen=True)
class Target:
    name: str
    data: Path
    state: Path
    store: Path
    project: manifest_mod.Project | None
    root: Path | None
    server: bool
    default_dest: Path
    base: Path | None = None

    def folder(self, part: str) -> Path:
        return {"data": self.data, "state": self.state, "store": self.store}[part]

    def reserved(self, part: str, name: str) -> bool:
        """Names inside a part folder that a backup never holds and a restore never touches."""
        if name.startswith((ASIDE_PREFIX, TEMP_PREFIX, WORK_PREFIX)):
            return True
        if part == "state" and name in STATE_RESERVED:
            return True
        return part == "data" and self.store.parent == self.data and name == self.store.name


def _now_stamp() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _load_manifest(folder: Path) -> manifest_mod.Project | None:
    try:
        return manifest_mod.load(folder) if (folder / manifest_mod.MANIFEST_FILE).is_file() else None
    except MesError:
        return None


def resolve(project: str | None = None, *, project_dir: str | os.PathLike[str] | None = None,
            server_root: str | os.PathLike[str] | None = None) -> Target:
    if server_root is not None:
        if not project:
            raise UsageError("With --server-root, say which project: mes backup <project> --server-root DIR",
                             hint="For example: mes backup myproject --server-root /var/lib/mes")
        layout = paths.server_layout(project, server_root)
        return Target(project, layout.data, layout.state, layout.store, _load_manifest(layout.current_project),
                      None, True, layout.base / "backups", layout.base)
    found: Path | None
    if project_dir is not None:
        found = Path(project_dir).expanduser().resolve() / manifest_mod.MANIFEST_FILE
        if not found.is_file():
            raise NotFound(f"There is no project.toml in {found.parent}.",
                           hint="Point --project-dir at the project's folder (the one holding project.toml).")
    else:
        found = manifest_mod.find()
    manifest = manifest_mod.load(found) if found is not None else None
    if manifest is not None and project and project != manifest.name:
        raise UsageError(
            f'The project in this folder is "{manifest.name}", not "{project}", so mes stopped rather than guess.',
            hint=f"Run it from the folder of {project}, or leave the name out.",
        )
    name = project or (manifest.name if manifest else None)
    if not name:
        raise NotFound("There is no project here (no project.toml), and no project name was given.",
                       hint="Run it inside the project's folder, or name it: mes backup <project>")
    paths.check_name(name)
    return Target(name, paths.project_data_dir(name), paths.project_state_dir(name), paths.project_store_dir(name),
                  manifest, manifest.root if manifest else None, False,
                  paths.state_home().parent / "mes-backups" / name)


# --------------------------------------------------------------------------- who runs it


def _check_runner() -> None:
    if os.geteuid() == 0:
        raise Refused(
            "Backups and restores are never run as root: root would follow whatever the project's own code left "
            "in its folders (links to other places), so nothing was done.",
            hint="Run it as the account that owns the project's folders (on a server: sudo -u mes-<project> mes ...).",
            code="as_root",
        )


def _open_own(path: Path, *, create: bool = False) -> int | None:
    """An open folder that is a real folder (not a link) of the account running mes. None when
    it does not exist (and ``create`` is false)."""
    if create and not os.path.lexists(path):
        path.mkdir(mode=0o700, parents=True)
    try:
        fd = os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise Refused(f"{path} is not a real folder (it may be a link to somewhere else), so nothing was done.",
                      hint="Move it away, or check the project's folder settings.", code="unsafe_folder") from None
    st = os.fstat(fd)
    if st.st_uid != os.geteuid():
        os.close(fd)
        raise Refused(
            f"{path} belongs to another account, so mes does not back it up or restore into it from this one.",
            hint="Run it as the account that owns the project's folders.",
            code="not_owner",
        )
    return fd


def _walk_to(dirfd: int, parts: list[str]) -> int:
    """An fd for the folder ``parts`` below ``dirfd``, never following a link."""
    fd = os.dup(dirfd)
    try:
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


# --------------------------------------------------------------------------- walking


def _ustar_ok(name: str) -> bool:
    raw = name.encode("utf-8")
    if len(raw) <= 100:
        return True
    if len(raw) > 255:
        return False
    for i in range(len(raw) - 1, 0, -1):
        if raw[i:i + 1] == b"/" and len(raw[:i]) <= 155 and len(raw[i + 1:]) <= 100:
            return True
    return False


def _walk(root_fd: int, prefix: str, target: Target | None, skipped: list[dict[str, str]]) -> list[list[str]]:
    """The plain files below the open folder ``root_fd``, as path parts, read with ``os.fwalk``
    (one open folder at a time, links never followed)."""
    out: list[list[str]] = []
    for dirpath, dirnames, filenames, dirfd in os.fwalk(".", dir_fd=root_fd, follow_symlinks=False):
        here = [x for x in dirpath.split("/") if x not in ("", ".")]
        keep = []
        for d in sorted(dirnames):
            if (not here and target is not None and target.reserved(prefix, d)) or d.startswith(WORK_PREFIX):
                continue
            if stat.S_ISLNK(os.stat(d, dir_fd=dirfd, follow_symlinks=False).st_mode):
                skipped.append({"path": "/".join([prefix, *here, d]), "why": "a link to somewhere else"})
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            parts = [*here, f]
            if not here and target is not None and target.reserved(prefix, f):
                continue
            rel = "/".join([prefix, *parts])
            st = os.stat(f, dir_fd=dirfd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                why = "a link to somewhere else" if stat.S_ISLNK(st.st_mode) else "not a plain file"
                skipped.append({"path": rel, "why": why})
                continue
            try:
                archive.check_member_path(rel)
                ok = _ustar_ok(rel)
            except archive.ArchiveError:
                ok = False
            if not ok:
                skipped.append({"path": rel, "why": "its name is too long or has characters a backup cannot hold"})
                continue
            out.append(parts)
    return out


def _check_dest(target: Target, dest: Path) -> Path:
    dest = dest.expanduser()
    if not dest.is_absolute():
        dest = Path.cwd() / dest
    dest = Path(os.path.abspath(dest))
    forbidden = [target.data, target.store, target.state]
    if target.root is not None:
        forbidden.append(target.root)
    if target.base is not None:
        forbidden += [target.base / "code", target.base / "ui"]
    for f in forbidden:
        if paths.is_inside(dest, f):
            raise Refused(
                "The backup would be written inside the project's own folders, where it would be lost together "
                "with them (or where the project's own code could change it), so nothing was written.",
                hint="Choose another folder with --to, for example on another disk.",
                code="unsafe_folder",
            )
    if dest.exists() or dest.is_symlink():
        st = os.lstat(dest)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise Refused(
                f"The backup folder {dest} can be opened by other accounts (or is not a plain folder of yours), so "
                "a backup of private data will not be written there.",
                hint=f"Use a private folder (chmod 700 {dest}), or a new folder: mes creates it privately.",
                code="unsafe_folder",
            )
    elif target.server and dest == target.default_dest:
        raise NotFound(
            f"The server's backup folder {dest} does not exist yet. It is made by the root script, so jobs and gate "
            "requests can never see or change the backups.",
            hint="Make the plan again and run its script (mes server plan <project>), or give a folder with --to.",
            code="not_set_up",
        )
    else:
        dest.mkdir(mode=0o700, parents=True)
        os.chmod(dest, 0o700)
    return dest


def _snapshot_store(store: Path, workdir: Path) -> Path:
    mybucket = siblings.require("mybucket", "Backing up the project's storage bucket")
    snap = workdir / "store"
    siblings.run_json([mybucket, "--json", "--store", str(store), "snapshot", str(snap)],
                      timeout=3600, what="Making a consistent copy of the store (mybucket snapshot)")
    siblings.run_json([mybucket, "--json", "--store", str(snap), "verify"],
                      timeout=3600, what="Checking the copy of the store (mybucket verify)")
    return snap


def _has_contents(folder: Path) -> bool:
    try:
        return folder.is_dir() and not folder.is_symlink() and any(folder.iterdir())
    except OSError:
        return False


# --------------------------------------------------------------------------- packing


class _Hashing:
    """Reads exactly ``size`` bytes from an open file, hashing them on the way."""

    def __init__(self, f: BinaryIO, size: int):
        self.f, self.left, self.h, self.got = f, size, hashlib.sha256(), 0

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        block = self.f.read(self.left if n is None or n < 0 else min(n, self.left))
        self.left -= len(block)
        self.got += len(block)
        self.h.update(block)
        return block


class _Changed(Exception):
    pass


def _pack(out: Path, sources: list[tuple[str, int, list[str]]], info: dict[str, Any]) -> dict[str, Any]:
    """Write the archive: each file opened below its part's open folder without following links,
    checked to be a plain file on the open file itself, and hashed from the bytes that are packed.
    ``MES-BACKUP.json`` comes last, with those hashes."""
    files: list[dict[str, Any]] = []
    folders: set[str] = set()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for prefix, root_fd, parts in sources:
            name = "/".join([prefix, *parts])
            names = name.split("/")
            for i in range(1, len(names)):
                folder = "/".join(names[:i])
                if folder not in folders:
                    folders.add(folder)
                    tar.addfile(archive._tarinfo(folder, 0, 0o755, tarfile.DIRTYPE))
            parent = _walk_to(root_fd, parts[:-1])
            try:
                try:
                    sfd = os.open(parts[-1], os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=parent)
                except OSError:
                    raise _Changed(name) from None
            finally:
                os.close(parent)
            with os.fdopen(sfd, "rb") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode):
                    raise _Changed(name)
                reader = _Hashing(f, st.st_size)
                mode = 0o755 if st.st_mode & 0o111 else 0o644
                try:
                    tar.addfile(archive._tarinfo(name, st.st_size, mode, tarfile.REGTYPE), reader)
                except OSError:
                    raise _Changed(name) from None
                after = os.fstat(f.fileno())
                if reader.got != st.st_size or after.st_size != st.st_size or after.st_mtime_ns != st.st_mtime_ns:
                    raise _Changed(name)
            files.append({"path": name, "size": st.st_size, "sha256": reader.h.hexdigest()})
        info = {**info, "files": files}
        blob = json.dumps(info, indent=1, ensure_ascii=False).encode("utf-8")
        tar.addfile(archive._tarinfo(BACKUP_INFO, len(blob), 0o644, tarfile.REGTYPE), io.BytesIO(blob))
    return {"files": len(files), "bytes": sum(f["size"] for f in files), "sha256": archive.sha256_file(out),
            "archive_bytes": out.stat().st_size}


# --------------------------------------------------------------------------- signing


def _signing_key(given: str | None) -> Path | None:
    """The person's signing key: the one given, or the deploy key setting, or ~/.ssh/mes-sign
    when it exists. None when no key is set up (the backup is then not signed, and says so)."""
    from mes import deploy

    if given:
        return Path(given).expanduser()
    key = deploy._key_path(None)
    pub = key.with_name(key.name + ".pub")
    return key if key.exists() or pub.exists() else None


def _read_public_key(path: Path, what: str) -> tuple[str, str]:
    from mes import deploy

    try:
        words = path.expanduser().read_text(encoding="utf-8").split()
    except (OSError, UnicodeDecodeError):
        raise NotFound(f"{what} ({path}) cannot be read.", hint="Give the public key file (.pub).",
                       code="no_key") from None
    if len(words) < 2 or words[0] not in deploy.SIGNER_KEY_TYPES or not _KEY_B64_RE.match(words[1]):
        raise Refused(f"{what} ({path}) is not an Ed25519 public key.", hint="Give the .pub file of an Ed25519 key.",
                      code="key_type")
    return words[0], words[1]


def _trusted_signers(target: Target, signer_files: list[str]) -> list[str]:
    """The keys a backup of this project may be signed with, as checked allowed-signers lines
    (namespace mes-backup only)."""
    from mes import deploy

    keys: list[tuple[str, str]] = [_read_public_key(Path(f), "The signer key") for f in signer_files]
    own = deploy._key_path(None)
    own_pub = own if own.name.endswith(".pub") else own.with_name(own.name + ".pub")
    if own_pub.is_file():
        keys.append(_read_public_key(own_pub, "Your signing key"))
    if target.server and target.base is not None:
        layout = paths.server_layout(target.name, target.base.parent)
        try:
            for line in deploy.check_signers(layout.allowed_signers, target.name, layout.base):
                kind, b64 = line.split()[2:4]
                keys.append((kind, b64))
        except MesError:
            pass
    return list(dict.fromkeys(f'{target.name} namespaces="{BACKUP_NAMESPACE}" {k} {b}' for k, b in keys))


# --------------------------------------------------------------------------- backup


def backup(target: Target, *, dest: str | os.PathLike[str] | None = None, include_store: bool = True,
           key: str | None = None, sign: bool = True) -> dict[str, Any]:
    from mes import deploy

    _check_runner()
    key_path = _signing_key(key) if sign else None
    if key_path is not None:
        deploy._check_key(key_path)  # asks the person to make one when --key names a missing key
    out_dir = _check_dest(target, Path(dest) if dest is not None else target.default_dest)
    notes: list[str] = []
    skipped: list[dict[str, str]] = []
    store_in_data = target.store.parent == target.data
    has_store = _has_contents(target.store)
    opened: list[int] = []
    work = Path(tempfile.mkdtemp(prefix=WORK_PREFIX, dir=out_dir))
    path: Path | None = None
    try:
        sources: list[tuple[str, int, list[str]]] = []
        for part, folder in (("data", target.data), ("state", target.state)):
            fd = _open_own(folder)
            if fd is None:
                continue
            opened.append(fd)
            sources += [(part, fd, parts) for parts in _walk(fd, part, target, skipped)]
        store_used = False
        if has_store and include_store:
            if not store_in_data and _open_own(target.store) is None:
                raise NotFound("The project's store folder disappeared during the backup.", hint="Run it again.")
            snap = _snapshot_store(target.store, work)
            fd = _open_own(snap)
            assert fd is not None
            opened.append(fd)
            sources += [("store", fd, parts) for parts in _walk(fd, "store", None, skipped)]
            store_used = True
        elif has_store:
            notes.append("The storage bucket was left out (--no-store), so this backup does not hold it.")
        elif target.project is not None and target.project.storage is not None:
            notes.append("The project has [storage], but its store is empty or missing, so there was nothing to copy.")
        if not sources:
            raise NotFound(f'The project "{target.name}" has no data or state on this computer yet, so there is '
                           "nothing to back up.", hint="Use the project first (for example run its jobs).",
                           code="nothing_to_back_up")
        info = {"format": 1, "project": target.name, "created": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
                "server": target.server, "store": store_used}
        stamp = _now_stamp()
        path = out_dir / f"{target.name}-{stamp}.tar.gz"
        n = 2
        while path.exists():
            path = out_dir / f"{target.name}-{stamp}-{n}.tar.gz"
            n += 1
        try:
            packed = _pack(path, sources, info)
        except _Changed:
            raise MesError(
                "A file changed or was replaced while the backup was being written, so the backup was removed. "
                "Nothing else was changed.",
                hint="Run the backup again, ideally while no job of the project is running.",
                code="backup_changed",
            ) from None
    except BaseException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    finally:
        for fd in opened:
            os.close(fd)
        shutil.rmtree(work, ignore_errors=True)
    try:
        checked = verify_archive(path)
    except MesError:
        path.unlink(missing_ok=True)
        raise MesError(
            "The backup did not match the files when it was read back, so it was removed. Nothing else was changed.",
            hint="Run the backup again, ideally while no job of the project is running.",
            code="backup_changed",
        ) from None
    signature = None
    if key_path is not None:
        try:
            deploy.sign(path, key_path, BACKUP_NAMESPACE)
        except MesError:
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".sig").unlink(missing_ok=True)
            raise MesError(
                f"The backup could not be signed with {key_path}, so it was removed (an unsigned backup cannot be told "
                "apart from one someone else made).",
                hint=f"Unlock the key for this session (ssh-add {key_path}; a hardware key also asks for a touch), or "
                "make an unsigned backup with --no-sign.",
                code="sign_failed",
            ) from None
        signature = path.with_name(path.name + ".sig")
        os.chmod(signature, 0o600)
    if skipped:
        notes.append(f"{len(skipped)} item(s) were not copied because they are links or special files: see "
                     "'skipped'.")
    if signature is None:
        notes.append("This backup is not signed (no signing key is set up here, or --no-sign). Its check proves it is "
                     "complete and unchanged (integrity), not who made it (origin). Write down its SHA-256 and compare "
                     "it before restoring: " + packed["sha256"])
    return {
        "project": target.name,
        "archive": str(path),
        "signature": str(signature) if signature else None,
        "signed": signature is not None,
        "files": checked["files"],
        "bytes": checked["bytes"],
        "archive_bytes": packed["archive_bytes"],
        "sha256": packed["sha256"],
        "store": store_used,
        "verified": True,
        "skipped": skipped,
        "notes": notes,
        "message": f'The data and state of "{target.name}" are backed up in {path}, and the backup was read back '
        "and checked file by file" + (f", and signed ({signature.name})" if signature else "") + ". Keep a copy on "
        "another disk or computer: a backup on the same disk does not survive the disk.",
    }


def verify_archive(path: Any, *, limits: archive.Limits = RESTORE_LIMITS) -> dict[str, Any]:
    info_raw: bytes | None = None
    found: dict[str, tuple[int, str]] = {}
    for name, kind, _mode, size, chunks in archive.read_members(path, limits):
        if kind != "file":
            for _ in chunks:
                pass
            continue
        h = hashlib.sha256()
        data = bytearray() if name == BACKUP_INFO else None
        got = 0
        for block in chunks:
            h.update(block)
            got += len(block)
            if data is not None:
                if len(data) + len(block) > 64 * 1024 * 1024:
                    raise _broken("its list of files is too large")
                data += block
        if data is not None:
            info_raw = bytes(data)
        else:
            found[name] = (got, h.hexdigest())
    info = _parse_info(info_raw)
    expected = {f["path"]: (f["size"], f["sha256"]) for f in info["files"]}
    if expected != found:
        missing = sorted(set(expected) - set(found))
        extra = sorted(set(found) - set(expected))
        if missing or extra:
            raise _broken("files are missing from it or were added to it")
        raise _broken("the contents of a file do not match its recorded fingerprint")
    return {"ok": True, "project": info["project"], "created": info["created"], "files": len(found),
            "bytes": sum(s for s, _ in found.values()), "store": bool(info.get("store")), "info": info}


def _broken(problem: str) -> Refused:
    return Refused(
        f"The backup does not pass its check ({problem}), so nothing was restored. It may be damaged or changed.",
        hint="Use another backup, or make a new one.",
        code="backup_broken",
    )


_SHA_RE = re.compile(r"^[0-9a-f]{64}\Z")


def _created(value: Any) -> _dt.datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise _broken("its time of making is not a date and time")
    try:
        when = _dt.datetime.fromisoformat(value)
    except ValueError:
        raise _broken("its time of making is not a date and time") from None
    if when.tzinfo is None:
        raise _broken("its time of making has no time zone")
    return when


def _parse_info(raw: bytes | None) -> dict[str, Any]:
    if raw is None:
        raise _broken(f"it has no {BACKUP_INFO}")
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _broken(f"its {BACKUP_INFO} cannot be read") from None
    if not isinstance(info, dict) or info.get("format") != 1 or not isinstance(info.get("files"), list) \
            or not isinstance(info.get("project"), str):
        raise _broken(f"its {BACKUP_INFO} is not in the form mes writes")
    _created(info.get("created"))
    seen = set()
    for f in info["files"]:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not isinstance(f.get("size"), int) \
                or isinstance(f.get("size"), bool) or f["size"] < 0 \
                or not isinstance(f.get("sha256"), str) or not _SHA_RE.match(f["sha256"]) or f["path"] in seen \
                or f["path"].split("/", 1)[0] not in PARTS or "/" not in f["path"]:
            raise _broken(f"its {BACKUP_INFO} lists a file in a way mes does not write")
        seen.add(f["path"])
    return info


# --------------------------------------------------------------------------- restore


def _rename_noreplace(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    """Rename relative to two open folders, never over an existing name."""
    if _RENAME_EXCL is not None:
        func, flag = _RENAME_EXCL
        if func(src_fd, src.encode(), dst_fd, dst.encode(), flag) == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise _conflict(dst)
        if err not in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise OSError(err, os.strerror(err), dst)
    try:
        os.stat(dst, dir_fd=dst_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        return
    raise _conflict(dst)


def _conflict(name: str) -> Refused:
    return Refused(f"Something called {name[:80]!r} appeared in the folder while restoring, so it was not replaced.",
                   hint="Stop the project's jobs, then restore again with --replace.", code="restore_conflict")


def _find_rename_excl() -> tuple[Any, int] | None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    for name, flag in (("renameat2", 1), ("renameatx_np", 0x4)):  # RENAME_NOREPLACE / RENAME_EXCL
        func = getattr(libc, name, None)
        if func is not None:
            func.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            func.restype = ctypes.c_int
            return func, flag
    return None


_RENAME_EXCL = _find_rename_excl() if sys.platform in ("linux", "darwin") else None


def _names(fd: int, target: Target, part: str) -> list[str]:
    return sorted(n for n in os.listdir(fd) if not target.reserved(part, n))


def _stage(held: BinaryIO, part: str, folder_fd: int, staging: str, target: Target,
           expected: dict[str, tuple[int, str]]) -> tuple[int, list[str]]:
    """Unpack the files of one part into ``staging`` (a new folder inside the destination, so
    moving them in is a rename), with mes's hardened reader, hashing every file. Names mes never
    restores are left out and listed."""
    os.mkdir(staging, 0o700, dir_fd=folder_fd)
    stage_fd = os.open(staging, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=folder_fd)
    found: dict[str, tuple[int, str]] = {}
    left_out: list[str] = []
    made: set[tuple[str, ...]] = set()
    try:
        for name, kind, mode, size, chunks in archive.read_members(held, RESTORE_LIMITS):
            parts = name.split("/")
            if parts[0] != part or len(parts) < 2:
                for _ in chunks:
                    pass
                continue
            rel = parts[1:]
            if target.reserved(part, rel[0]):
                for _ in chunks:
                    pass
                if kind == "file":
                    left_out.append(name)
                    expected.pop(name, None)
                continue
            dirs = rel if kind == "folder" else rel[:-1]
            for i in range(1, len(dirs) + 1):
                key = tuple(dirs[:i])
                if key not in made:
                    parent = _walk_to(stage_fd, list(dirs[:i - 1]))
                    try:
                        try:
                            os.mkdir(dirs[i - 1], 0o700, dir_fd=parent)
                        except FileExistsError:
                            st = os.stat(dirs[i - 1], dir_fd=parent, follow_symlinks=False)
                            if not stat.S_ISDIR(st.st_mode):
                                raise _broken("a path is used both as a file and as a folder") from None
                    finally:
                        os.close(parent)
                    made.add(key)
            if kind == "folder":
                continue
            parent = _walk_to(stage_fd, rel[:-1])
            try:
                try:
                    fd = os.open(rel[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                                 0o700 if mode & 0o111 else 0o600, dir_fd=parent)
                except FileExistsError:
                    raise _broken("it has the same path twice") from None
            finally:
                os.close(parent)
            h = hashlib.sha256()
            got = 0
            with os.fdopen(fd, "wb") as f:
                for block in chunks:
                    f.write(block)
                    h.update(block)
                    got += len(block)
            found[name] = (got, h.hexdigest())
    finally:
        os.close(stage_fd)
    wanted = {k: v for k, v in expected.items() if k.split("/", 1)[0] == part}
    if found != wanted:
        raise _broken("its files do not match its recorded list and fingerprints")
    return len(found), left_out


def _read_signature(src: Path, given: str | None) -> str | None:
    path = Path(given).expanduser() if given else src.with_name(src.name + ".sig")
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except FileNotFoundError:
        if given:
            raise NotFound(f"There is no signature file at {path}.", hint="Give the .sig file made by mes backup.",
                           code="no_signature") from None
        return None
    except OSError:
        raise Refused(f"The signature file {path} is not a plain file, so nothing was restored.",
                      hint="Use the .sig file made by mes backup.", code="bad_signature") from None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise Refused(f"The signature file {path} is not a plain file, so nothing was restored.",
                          hint="Use the .sig file made by mes backup.", code="bad_signature")
        raw = f.read(MAX_SIGNATURE_BYTES + 1)
    if len(raw) > MAX_SIGNATURE_BYTES:
        raise Refused("The signature file is too large to be a signature, so nothing was restored.",
                      hint="Use the .sig file made by mes backup.", code="bad_signature")
    return raw.decode("ascii", "replace")


def restore(target: Target, archive_path: str | os.PathLike[str], *, yes: bool = False, replace: bool = False,
            json_mode: bool = False, signature: str | None = None, signers: list[str] | tuple[str, ...] = (),
            allow_unsigned: bool = False) -> dict[str, Any]:
    from mes import deploy

    _check_runner()
    src = Path(archive_path).expanduser()
    try:
        afd = os.open(src, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except FileNotFoundError:
        raise NotFound(f"There is no backup file at {src}.",
                       hint="Give the path of a .tar.gz file made by mes backup.") from None
    except OSError:
        raise Refused(f"{src} is not a plain file (it may be a link), so nothing was restored.",
                      hint="Give the path of the .tar.gz file itself.", code="unsafe_file") from None
    held = os.fdopen(afd, "rb")
    folder_fds: dict[str, int] = {}
    staged: dict[str, str] = {}
    try:
        if not stat.S_ISREG(os.fstat(held.fileno()).st_mode):
            raise Refused(f"{src} is not a plain file, so nothing was restored.",
                          hint="Give the path of the .tar.gz file itself.", code="unsafe_file")
        sha = hashlib.sha256()
        for block in iter(lambda: held.read(CHUNK), b""):
            sha.update(block)
        archive_sha = sha.hexdigest()
        sig_text = _read_signature(src, signature)
        trusted = _trusted_signers(target, list(signers))
        signed_by: str | None = None
        if sig_text is not None:
            if not trusted:
                raise Refused(
                    "This backup is signed, but no key to check the signature with is set up here, so it was not "
                    "restored.", hint="Give the public half of the key that signed it: --signer <key.pub>",
                    code="no_signer_key")
            with tempfile.TemporaryDirectory(prefix=".mes-verify-") as vdir:
                try:
                    signed_by = deploy.verify_signature(held, sig_text, trusted, target.name,
                                                        namespace=BACKUP_NAMESPACE, sig_dir=Path(vdir))
                except Refused:
                    raise Refused(
                        "The backup's signature does not match it, or it was signed by a key that is not trusted "
                        "here, so nothing was unpacked or restored. It may have been changed.",
                        hint="Use another backup, or give the right key with --signer <key.pub>.",
                        code="bad_signature") from None
            if not _FINGERPRINT_RE.match(signed_by or ""):
                signed_by = "a trusted key"
        elif trusted and not allow_unsigned:
            raise Refused(
                "This backup is not signed, but a signing key is set up here, so it was not restored: anyone could "
                "have made an unsigned backup.",
                hint="Restore a signed backup, or (if you are sure where this one came from) add --allow-unsigned.",
                code="unsigned")
        checked = verify_archive(held)
        info = checked["info"]
        if info["project"] != target.name:
            raise Refused(
                f"This backup belongs to another project ({str(info['project'])[:40]!r}), not \"{target.name}\", so it "
                "was not restored. Projects never get each other's data.",
                hint="Restore it into its own project.",
                code="wrong_project",
            )
        when = _created(info["created"]).astimezone(_dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
        expected = {f["path"]: (f["size"], f["sha256"]) for f in info["files"]}
        parts = [p for p in PARTS if any(k.startswith(p + "/") for k in expected)]
        for p in parts:
            fd = _open_own(target.folder(p), create=True)
            assert fd is not None
            folder_fds[p] = fd
        busy = {p: _names(folder_fds[p], target, p) for p in parts}
        stamp = _now_stamp()
        full = [p for p in parts if busy[p]]
        if full and not replace:
            names = ", ".join(f"{p} ({target.folder(p)})" for p in full)
            raise Refused(
                f"These folders of the project already have contents: {names}. Restoring over them would mix old and "
                "new files, so nothing was changed.",
                hint=f"Run it again with --replace: their current contents are then moved aside into "
                f"{ASIDE_PREFIX}<time> inside each folder, never deleted.",
                code="not_empty",
            )
        never = [k for k in expected if target.reserved(k.split("/")[0], k.split("/")[1])]
        counts = {p: sum(1 for f in expected if f.startswith(p + "/") and f not in never) for p in parts}
        plan = [f"{p}: {counts[p]} file(s) from the backup go into {target.folder(p)}" +
                (f"; its current {len(busy[p])} item(s) are moved into {ASIDE_PREFIX}{stamp}" if busy[p] else "")
                for p in parts]
        origin = (f"It is signed by {signed_by}, so it was made with a key you trust." if signed_by else
                  "It is NOT signed: its check proves it is complete and unchanged since it was written, not who "
                  f"made it. Its SHA-256 is {archive_sha}; compare it with the one mes backup printed.")
        if not yes:
            prefix = "mes --json" if json_mode else "mes"
            rerun = f"{prefix} restore {target.name} {shlex.quote(str(src))} --yes" + (" --replace" if replace else "")
            if signature:
                rerun += f" --signature {shlex.quote(str(signature))}"
            for f in signers:
                rerun += f" --signer {shlex.quote(str(f))}"
            if allow_unsigned:
                rerun += " --allow-unsigned"
            if target.server and target.base is not None:
                rerun += f" --server-root {shlex.quote(str(target.base.parent))}"
            return protocol.needs_input(
                id="restore",
                question=f'Restore the backup of "{target.name}" made at {when}? ' + " ".join(s + "." for s in plan),
                why="Restoring changes the project's data. " + origin + " Nothing is deleted: current contents are "
                "moved aside. The run log and the gate's log are never restored.",
                options=[protocol.option("no", "Do not restore", "Nothing changes."),
                         protocol.option("yes", "Restore it", "The backup is put in place as described.")],
                recommended="no",
                rerun=rerun,
            )
        left_out: list[str] = []
        for p in parts:
            staged[p] = f"{TEMP_PREFIX}{secrets.token_hex(6)}"
            _, skipped_names = _stage(held, p, folder_fds[p], staged[p], target, expected)
            left_out += skipped_names
        moved: dict[str, str] = {}
        for p in parts:
            ffd = folder_fds[p]
            if busy[p]:
                aside = f"{ASIDE_PREFIX}{stamp}"
                os.mkdir(aside, 0o700, dir_fd=ffd)
                aside_fd = os.open(aside, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=ffd)
                try:
                    for name in busy[p]:
                        _rename_noreplace(ffd, name, aside_fd, name)
                finally:
                    os.close(aside_fd)
                moved[p] = str(target.folder(p) / aside)
            stage_fd = os.open(staged[p], os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=ffd)
            try:
                for name in sorted(os.listdir(stage_fd)):
                    _rename_noreplace(stage_fd, name, ffd, name)
            finally:
                os.close(stage_fd)
            os.rmdir(staged.pop(p), dir_fd=ffd)
    finally:
        for p, name in staged.items():
            with _suppress():
                shutil.rmtree(target.folder(p) / name)
        for fd in folder_fds.values():
            os.close(fd)
        held.close()
    return {
        "project": target.name,
        "restored": parts,
        "files": sum(counts.values()),
        "moved_aside": moved,
        "not_restored": left_out,
        "verified": True,
        "signed": signed_by is not None,
        "signed_by": signed_by,
        "sha256": archive_sha,
        "message": f'The backup of "{target.name}" made at {when} is back in place, after its check passed. '
        + origin + (" The previous contents were moved aside, not deleted: " + ", ".join(moved.values()) + "."
                    if moved else ""),
    }


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return True


# --------------------------------------------------------------------------- commands


def _text_backup(r: dict[str, Any]) -> str:
    lines = [r["message"], f"Files: {r['files']}, {r['bytes']} bytes (archive: {r['archive_bytes']} bytes).",
             f"SHA-256 of the archive: {r['sha256']}"]
    for n in r["notes"]:
        lines.append(f"Please note: {n}")
    for s in r["skipped"][:20]:
        lines.append(f"  not copied: {s['path']} ({s['why']})")
    return "\n".join(lines)


def _cmd_backup(args: argparse.Namespace) -> Any:
    check_allowed("write")  # it writes a file outside the project
    target = resolve(args.project, project_dir=args.project_dir, server_root=args.server_root)
    result = backup(target, dest=args.to, include_store=not args.no_store, key=args.key, sign=not args.no_sign)
    return result if args.json else _text_backup(result)


def _cmd_restore(args: argparse.Namespace) -> Any:
    check_allowed("admin")  # it replaces the project's data
    if args.archive is None:
        project, archive_path = None, args.project
    else:
        project, archive_path = args.project, args.archive
    target = resolve(project, project_dir=args.project_dir, server_root=args.server_root)
    return restore(target, archive_path, yes=args.yes, replace=args.replace, json_mode=args.json,
                   signature=args.signature, signers=args.signer, allow_unsigned=args.allow_unsigned)


def register(subparsers: Any) -> None:
    b = subparsers.add_parser(
        "backup",
        help="Back up a project's data and state into one checked archive.",
        description="Copy the project's data and state (and a consistent snapshot of its store) into one archive, "
        "then read it back and check every file. See: mes guide backup",
    )
    b.add_argument("project", nargs="?", default=None, help="the project's name (default: the project in this folder)")
    b.add_argument("--project-dir", metavar="DIR", default=None, help="the project's folder")
    b.add_argument("--server-root", metavar="DIR", default=None,
                   help="back up the server layout (for example /var/lib/mes), as the project's account")
    b.add_argument("--to", metavar="DIR", default=None, help="the folder for the archive (default: see mes guide backup)")
    b.add_argument("--no-store", action="store_true", help="leave the storage bucket out")
    b.add_argument("--key", metavar="FILE", default=None,
                   help="the signing key (default: [deploy] key in the config file, or ~/.ssh/mes-sign if it exists)")
    b.add_argument("--no-sign", action="store_true", help="do not sign the backup (it can then only be checked "
                   "for damage, not for who made it)")
    b.set_defaults(func=_cmd_backup)
    r = subparsers.add_parser(
        "restore",
        help="Put a backup made by mes backup back in place (after checking it).",
        description="Check a backup, then put its data and state back. Current contents are moved aside, never "
        "deleted. See: mes guide backup",
    )
    r.add_argument("project", help="the project's name, or the archive when the name is left out")
    r.add_argument("archive", nargs="?", default=None, help="the .tar.gz file made by mes backup")
    r.add_argument("--project-dir", metavar="DIR", default=None, help="the project's folder")
    r.add_argument("--server-root", metavar="DIR", default=None, help="restore into the server layout")
    r.add_argument("--yes", action="store_true", help="restore without asking (the person already said yes)")
    r.add_argument("--replace", action="store_true", help="move the current contents aside and restore over them")
    r.add_argument("--signature", metavar="FILE", default=None, help="the signature (default: <archive>.sig)")
    r.add_argument("--signer", action="append", default=[], metavar="FILE",
                   help="a public key (.pub) whose backups are trusted (repeatable; your own signing key and, on a "
                   "server, the project's deploy signers are trusted already)")
    r.add_argument("--allow-unsigned", action="store_true",
                   help="restore an unsigned backup even though a signing key is set up here")
    r.set_defaults(func=_cmd_restore)
