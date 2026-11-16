"""``mes backup`` and ``mes restore``: a copy of a project's data and state, checked twice.

A backup is one archive (gzip ustar, written by ``mes.archive``) holding::

    MES-BACKUP.json   project, time, and every file with its size and SHA-256
    data/...          the project's data folder (without the store, when the store is inside it)
    state/...         the project's state folder
    store/...         a consistent snapshot of the project's my-bucket-storage store, if it has one

Only regular files are copied; links and special files are skipped and listed. The store is
never copied file by file while it may be in use: ``mybucket snapshot`` makes a consistent copy,
``mybucket verify`` checks it, and only then is it packed. Without mybucket, a project with a
store is not backed up (unless ``--no-store``), and the message says why.

Verify step: after writing, the archive is read back and every file's size and SHA-256 are
compared with ``MES-BACKUP.json``. ``mes restore`` repeats that check after unpacking (with the
hardened extractor, into a temporary folder) and before anything is put in place.

Where backups go: ``--to DIR``, or by default ``<state home>/../mes-backups/<project>/``
(normally ``~/.local/state/mes-backups/<project>/``); on a server (``--server-root``) the default
is ``<project>/state/backups/``, which is left out of later backups. The folder is private
(0700) and each archive is ``<project>-<UTC time>.tar.gz``, mode 0600. mes refuses to write a
backup into the project's own data, store or code folders.

Restoring never deletes anything: when a folder already has contents, ``--replace`` moves them
into ``<folder>/.before-restore-<time>/`` first; without it the restore is refused. Without
``--yes`` it answers ``needs_input`` and says exactly what would be put in place and moved aside.

Public API
----------
``BACKUP_INFO`` (``"MES-BACKUP.json"``), ``RESTORE_LIMITS``.
``Target``: ``name``, ``data``, ``state``, ``store``, ``project`` (Project | None), ``root``
    (project folder | None), ``server`` (bool), ``default_dest``.
``resolve(project=None, *, project_dir=None, server_root=None) -> Target``
``backup(target, *, dest=None, include_store=True) -> dict``
``verify_archive(path, *, limits=RESTORE_LIMITS) -> dict``  re-reads and checks every hash.
``restore(target, archive, *, yes=False, replace=False, json_mode=False) -> dict``
``register(subparsers)``  adds ``mes backup`` and ``mes restore``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shlex
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mes import archive, paths, protocol, siblings
from mes import manifest as manifest_mod
from mes.app import check_allowed
from mes.errors import MesError, NotFound, Refused, TemporaryFailure, UsageError

BACKUP_INFO = "MES-BACKUP.json"
PARTS = ("data", "state", "store")
RESTORE_LIMITS = archive.Limits(max_files=500_000, max_file_bytes=8 * 1024**3 - 1, max_total_bytes=512 * 1024**3)
ASIDE_PREFIX = ".before-restore-"
TEMP_PREFIX = ".mes-restore-"
CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Target:
    name: str
    data: Path
    state: Path
    store: Path
    project: manifest_mod.ProjectFile | None
    root: Path | None
    server: bool
    default_dest: Path

    def folder(self, part: str) -> Path:
        return {"data": self.data, "state": self.state, "store": self.store}[part]


def _now_stamp() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _load_manifest(folder: Path) -> manifest_mod.ProjectFile | None:
    try:
        return manifest_mod.load_file(folder) if (folder / manifest_mod.MANIFEST_FILE).is_file() else None
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
                      None, True, layout.state / "backups")
    found: Path | None
    if project_dir is not None:
        found = Path(project_dir).expanduser().resolve() / manifest_mod.MANIFEST_FILE
        if not found.is_file():
            raise NotFound(f"There is no project.toml in {found.parent}.",
                           hint="Point --project-dir at the project's folder (the one holding project.toml).")
    else:
        found = manifest_mod.find()
    manifest = manifest_mod.load_file(found) if found is not None else None
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


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _walk(folder: Path, prefix: str, skip: set[Path], skipped: list[dict[str, str]]) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    if not folder.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            p = here / d
            if p in skip or d.startswith(TEMP_PREFIX):
                continue
            if p.is_symlink():
                skipped.append({"path": f"{prefix}/{p.relative_to(folder).as_posix()}", "why": "a link to somewhere else"})
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            p = here / f
            rel = f"{prefix}/{p.relative_to(folder).as_posix()}"
            st = os.lstat(p)
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
            out.append((rel, p))
    return out


def _check_dest(target: Target, dest: Path) -> Path:
    dest = dest.expanduser()
    if not dest.is_absolute():
        dest = Path.cwd() / dest
    dest = Path(os.path.abspath(dest))
    forbidden = [target.data, target.store]
    if target.root is not None:
        forbidden.append(target.root)
    if not (target.server and dest == target.default_dest):
        forbidden.append(target.state)
    for f in forbidden:
        if paths.is_inside(dest, f):
            raise Refused(
                "The backup would be written inside the project's own folders, where it would be lost together "
                "with them (and copied into the next backup), so nothing was written.",
                hint="Choose another folder with --to, for example on another disk.",
                code="unsafe_folder",
            )
    if dest.exists():
        st = os.lstat(dest)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise Refused(
                f"The backup folder {dest} can be opened by other accounts (or is not a plain folder of yours), so "
                "a backup of private data will not be written there.",
                hint=f"Use a private folder (chmod 700 {dest}), or a new folder: mes creates it privately.",
                code="unsafe_folder",
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


def _snapshot_db(source: Path, target: Path) -> None:
    """A consistent copy of the project's shared database (SQLite's own backup, which waits for a
    writer to finish), so a backup never holds half of a transaction."""
    import sqlite3

    src = sqlite3.connect(source, timeout=30)
    try:
        src.execute("PRAGMA busy_timeout = 30000")
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as e:
        raise TemporaryFailure("The project's database could not be copied consistently (it may be busy), so no "
                               "backup was written.", hint="Run the backup again in a moment.", code="db_busy",
                               detail=str(e)) from None
    finally:
        src.close()
    os.chmod(target, 0o600)


def _has_contents(folder: Path) -> bool:
    try:
        return folder.is_dir() and any(folder.iterdir())
    except OSError:
        return False


# --------------------------------------------------------------------------- backup


def backup(target: Target, *, dest: str | os.PathLike[str] | None = None, include_store: bool = True) -> dict[str, Any]:
    out_dir = _check_dest(target, Path(dest) if dest is not None else target.default_dest)
    notes: list[str] = []
    skipped: list[dict[str, str]] = []
    store_in_data = paths.is_inside(target.store, target.data) and target.store != target.data
    has_store = _has_contents(target.store)
    work = Path(tempfile.mkdtemp(prefix=".mes-backup-", dir=out_dir))
    try:
        skip = {out_dir, work}
        if store_in_data:
            skip.add(target.store)
        entries = _walk(target.data, "data", skip, skipped)
        db_file = target.data / f"{target.name}.sqlite"
        if any(p == db_file for _, p in entries):
            snap_db = work / db_file.name
            _snapshot_db(db_file, snap_db)
            journal = target.data / f"{target.name}.sqlite-journal"
            entries = [(rel, snap_db if p == db_file else p) for rel, p in entries if p != journal]
        entries += _walk(target.state, "state", skip, skipped)
        store_used = False
        if has_store and include_store:
            snap = _snapshot_store(target.store, work)
            entries += _walk(snap, "store", set(), skipped)
            store_used = True
        elif has_store:
            notes.append("The storage bucket was left out (--no-store), so this backup does not hold it.")
        elif target.project is not None and target.project.storage is not None:
            notes.append("The project has [storage], but its store is empty or missing, so there was nothing to copy.")
        if not entries:
            raise NotFound(f'The project "{target.name}" has no data or state on this computer yet, so there is '
                           "nothing to back up.", hint="Use the project first (for example run its jobs).",
                           code="nothing_to_back_up")
        files = []
        for rel, p in entries:
            files.append({"path": rel, "size": os.lstat(p).st_size, "sha256": _sha256(p)})
        info = {"format": 1, "project": target.name, "created": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
                "server": target.server, "store": store_used, "files": files}
        info_bytes = json.dumps(info, indent=1, ensure_ascii=False).encode("utf-8")
        stamp = _now_stamp()
        path = out_dir / f"{target.name}-{stamp}.tar.gz"
        n = 2
        while path.exists():
            path = out_dir / f"{target.name}-{stamp}-{n}.tar.gz"
            n += 1
        packed = archive.pack([(BACKUP_INFO, info_bytes), *entries], path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    try:
        checked = verify_archive(path)
    except MesError:
        path.unlink(missing_ok=True)
        raise MesError(
            "A file changed while the backup was being written, so the backup did not match the files and was "
            "removed. Nothing else was changed.",
            hint="Run the backup again, ideally while no job of the project is running.",
            code="backup_changed",
        ) from None
    if skipped:
        notes.append(f"{len(skipped)} item(s) were not copied because they are links or special files: see "
                     "'skipped'.")
    return {
        "project": target.name,
        "archive": str(path),
        "files": checked["files"],
        "bytes": checked["bytes"],
        "archive_bytes": packed["archive_bytes"],
        "sha256": packed["sha256"],
        "store": store_used,
        "verified": True,
        "skipped": skipped,
        "notes": notes,
        "message": f'The data and state of "{target.name}" are backed up in {path}, and the backup was read back '
        "and checked file by file. Keep a copy on another disk or computer: a backup on the same disk does not "
        "survive the disk.",
    }


def verify_archive(path: str | os.PathLike[str], *, limits: archive.Limits = RESTORE_LIMITS) -> dict[str, Any]:
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
    return {"ok": True, "project": info["project"], "created": info.get("created"), "files": len(found),
            "bytes": sum(s for s, _ in found.values()), "store": bool(info.get("store"))}


def _broken(problem: str) -> Refused:
    return Refused(
        f"The backup does not pass its check ({problem}), so nothing was restored. It may be damaged or changed.",
        hint="Use another backup, or make a new one.",
        code="backup_broken",
    )


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
    seen = set()
    for f in info["files"]:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not isinstance(f.get("size"), int) \
                or not isinstance(f.get("sha256"), str) or f["path"] in seen \
                or f["path"].split("/", 1)[0] not in PARTS:
            raise _broken(f"its {BACKUP_INFO} lists a file in a way mes does not write")
        seen.add(f["path"])
    return info


# --------------------------------------------------------------------------- restore


def _entries(folder: Path, target: Target, part: str) -> list[Path]:
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.iterdir()):
        if p.name.startswith((ASIDE_PREFIX, TEMP_PREFIX)):
            continue
        if part == "data" and p == target.store:
            continue  # the store has its own part
        if part == "state" and target.server and p == target.default_dest:
            continue
        out.append(p)
    return out


def _move(src: Path, dst: Path) -> None:
    try:
        os.rename(src, dst)
    except OSError:
        shutil.move(str(src), str(dst))


def restore(target: Target, archive_path: str | os.PathLike[str], *, yes: bool = False, replace: bool = False,
            json_mode: bool = False) -> dict[str, Any]:
    src = Path(archive_path).expanduser()
    if not src.is_file():
        raise NotFound(f"There is no backup file at {src}.", hint="Give the path of a .tar.gz file made by mes backup.")
    tmp_parent = target.state
    tmp_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=tmp_parent))
    try:
        unpacked = work / "x"
        archive.extract(src, unpacked, limits=RESTORE_LIMITS, allowed_top={BACKUP_INFO, *PARTS})
        info = _parse_info((unpacked / BACKUP_INFO).read_bytes())
        if info["project"] != target.name:
            raise Refused(
                f'This backup belongs to the project "{str(info["project"])[:40]}", not "{target.name}", so it was '
                "not restored. Projects never get each other's data.",
                hint=f"Restore it into its own project: mes restore {str(info['project'])[:40]} <archive>",
                code="wrong_project",
            )
        _check_unpacked(unpacked, info)
        parts = [p for p in PARTS if (unpacked / p).is_dir()]
        busy = {p: _entries(target.folder(p), target, p) for p in parts}
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
        counts = {p: sum(1 for f in info["files"] if f["path"].startswith(p + "/")) for p in parts}
        plan = [f"{p}: {counts[p]} file(s) from the backup go into {target.folder(p)}" +
                (f"; its current {len(busy[p])} item(s) are moved into {ASIDE_PREFIX}{stamp}" if busy[p] else "")
                for p in parts]
        if not yes:
            prefix = "mes --json" if json_mode else "mes"
            rerun = f"{prefix} restore {target.name} {shlex.quote(str(src))} --yes" + (" --replace" if replace else "")
            if target.server:
                rerun += f" --server-root {shlex.quote(str(target.data.parent.parent))}"
            return protocol.needs_input(
                id="restore",
                question=f'Restore the backup of "{target.name}" from {info.get("created")}? ' + " ".join(
                    s + "." for s in plan),
                why="Restoring changes the project's data. The backup passed its check. Nothing is deleted: current "
                "contents are moved aside.",
                options=[protocol.option("yes", "Restore it", "The backup is put in place as described."),
                         protocol.option("no", "Do not restore", "Nothing changes.")],
                recommended="yes",
                rerun=rerun,
            )
        moved: dict[str, str] = {}
        for p in parts:
            folder = target.folder(p)
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            if busy[p]:
                aside = folder / f"{ASIDE_PREFIX}{stamp}"
                aside.mkdir(mode=0o700)
                for entry in busy[p]:
                    _move(entry, aside / entry.name)
                moved[p] = str(aside)
            for entry in sorted((unpacked / p).iterdir()):
                _move(entry, folder / entry.name)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {
        "project": target.name,
        "restored": parts,
        "files": len(info["files"]),
        "moved_aside": moved,
        "verified": True,
        "message": f'The backup of "{target.name}" from {info.get("created")} is back in place, after its check '
        "passed." + (" The previous contents were moved aside, not deleted: " + ", ".join(moved.values()) + "."
                     if moved else ""),
    }


def _check_unpacked(unpacked: Path, info: dict[str, Any]) -> None:
    expected = {f["path"]: (f["size"], f["sha256"]) for f in info["files"]}
    found: dict[str, tuple[int, str]] = {}
    for p in PARTS:
        base = unpacked / p
        if not base.exists():
            continue
        for dirpath, _dirs, filenames in os.walk(base):
            for f in filenames:
                fp = Path(dirpath) / f
                found[fp.relative_to(unpacked).as_posix()] = (fp.stat().st_size, _sha256(fp))
    if found != expected:
        raise _broken("its files do not match its recorded list and fingerprints")


# --------------------------------------------------------------------------- commands


def _text_backup(r: dict[str, Any]) -> str:
    lines = [r["message"], f"Files: {r['files']}, {r['bytes']} bytes (archive: {r['archive_bytes']} bytes)."]
    for n in r["notes"]:
        lines.append(f"Please note: {n}")
    for s in r["skipped"][:20]:
        lines.append(f"  not copied: {s['path']} ({s['why']})")
    return "\n".join(lines)


def _cmd_backup(args: argparse.Namespace) -> Any:
    check_allowed("write")  # it writes a file outside the project
    target = resolve(args.project, project_dir=args.project_dir, server_root=args.server_root)
    result = backup(target, dest=args.to, include_store=not args.no_store)
    return result if args.json else _text_backup(result)


def _cmd_restore(args: argparse.Namespace) -> Any:
    check_allowed("admin")  # it replaces the project's data
    if args.archive is None:
        project, archive_path = None, args.project
    else:
        project, archive_path = args.project, args.archive
    target = resolve(project, project_dir=args.project_dir, server_root=args.server_root)
    return restore(target, archive_path, yes=args.yes, replace=args.replace, json_mode=args.json)


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
    r.set_defaults(func=_cmd_restore)
