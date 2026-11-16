"""Packing and hardened unpacking of the archives mes sends and receives (deploys, backups).

An archive is a gzip-compressed **plain ustar** tar file. mes writes it itself, so the reader
accepts only what mes writes and refuses everything else, before anything is written:

- only regular files and folders (no symbolic or hard links, devices, FIFOs, sparse files, pax
  or GNU extension headers);
- every path relative, made of plain parts (no ``/`` at the start, no ``..`` or ``.``, no empty
  parts, no backslashes, no control characters), at most ``MAX_PATH_BYTES`` bytes and
  ``MAX_DEPTH`` levels, and never the same path twice (also not a file and a folder);
- at most ``MAX_FILES`` entries, ``MAX_FILE_BYTES`` per file and ``MAX_TOTAL_BYTES`` in all,
  counted while decompressing, so a small archive cannot expand into a huge one;
- header checksums are checked; owners, times and special mode bits are ignored (files get
  ``0600``, or ``0700`` when the archive marks them executable; folders ``0700``).

Files are created with ``O_EXCL | O_NOFOLLOW`` inside a destination folder that must not exist
yet, so nothing outside it can be touched. On any problem the partly written folder is removed.

Public API
----------
``ArchiveError`` (``Refused``, code ``archive_refused``).
``Limits(max_files, max_file_bytes, max_total_bytes)``; ``DEFAULT_LIMITS``.
``check_member_path(name) -> str``      the normalised path or ``ArchiveError``.
``pack(entries, out_path) -> dict``     ``entries``: iterable of ``(archive_path, source_path |
    bytes)``; writes ``out_path`` (gzip ustar, owner 0, fixed times). Returns counts and SHA-256.
``read_members(path, limits) -> Iterator[(name, kind, mode, size, chunks)]``  the streaming reader.
``extract(path, dest, *, limits=DEFAULT_LIMITS, allowed_top=None) -> dict``
    Unpacks into ``dest`` (created, must not exist). ``allowed_top``: the only names allowed at
    the top level. Returns ``{"files", "folders", "bytes"}``.
``sha256_file(path) -> str``
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import shutil
import stat
import tarfile
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mes.errors import Refused

BLOCK = 512
MAX_PATH_BYTES = 255
MAX_DEPTH = 32
CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Limits:
    max_files: int = 20_000
    max_file_bytes: int = 200 * 1024**2
    max_total_bytes: int = 1024**3


DEFAULT_LIMITS = Limits()


class ArchiveError(Refused):
    default_code = "archive_refused"


def _refuse(problem: str) -> ArchiveError:
    return ArchiveError(
        f"The archive was refused before anything was unpacked: {problem}. mes only accepts archives in the "
        "simple form it writes itself, so a damaged or hostile archive cannot write outside its own folder.",
        hint="Make the archive again with mes (mes deploy or mes backup). If you did not send this archive, "
        "someone else may have tried to.",
    )


def check_member_path(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise _refuse("an entry has no name")
    if len(name.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES:
        raise _refuse(f"the path of an entry is longer than {MAX_PATH_BYTES} bytes")
    if any(ord(c) < 32 or ord(c) == 127 for c in name) or "\\" in name:
        raise _refuse("an entry's path contains control characters or a backslash")
    if name.startswith("/"):
        raise _refuse("an entry has an absolute path (starting with /)")
    text = name[:-1] if name.endswith("/") else name
    parts = text.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise _refuse("an entry's path contains '..', '.' or an empty part")
    if len(parts) > MAX_DEPTH:
        raise _refuse(f"an entry is more than {MAX_DEPTH} folders deep")
    if unicodedata.normalize("NFC", text) != text:
        raise _refuse("an entry's path is not in the normal Unicode form")
    return text


# --------------------------------------------------------------------------- packing


def _tarinfo(name: str, size: int, mode: int, kind: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = mode
    info.type = kind
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def pack(entries: Iterable[tuple[str, str | os.PathLike[str] | bytes]], out_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Write a gzip ustar archive. Sources must be regular files (never links)."""
    out = Path(out_path)
    count = total = 0
    seen: set[str] = set()
    folders: set[str] = set()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, source in entries:
            name = check_member_path(name)
            if name in seen or name in folders:
                raise ValueError(f"{name} is in the archive twice")
            seen.add(name)
            parts = name.split("/")
            for i in range(1, len(parts)):
                folder = "/".join(parts[:i])
                if folder in seen:
                    raise ValueError(f"{folder} is both a file and a folder")
                if folder not in folders:
                    folders.add(folder)
                    tar.addfile(_tarinfo(folder, 0, 0o755, tarfile.DIRTYPE))
            if isinstance(source, bytes):
                tar.addfile(_tarinfo(name, len(source), 0o644, tarfile.REGTYPE), io.BytesIO(source))
                size = len(source)
            else:
                src = Path(source)
                st = os.lstat(src)
                if not stat.S_ISREG(st.st_mode):
                    raise ValueError(f"{src} is not a regular file")
                mode = 0o755 if st.st_mode & 0o111 else 0o644
                with open(src, "rb") as f:
                    tar.addfile(_tarinfo(name, st.st_size, mode, tarfile.REGTYPE), f)
                size = st.st_size
            count += 1
            total += size
    return {"files": count, "bytes": total, "sha256": sha256_file(out), "archive_bytes": out.stat().st_size}


def sha256_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- reading


class _Limited:
    """Reads decompressed bytes and stops once more than ``cap`` bytes came out."""

    def __init__(self, stream: Any, cap: int) -> None:
        self.stream = stream
        self.cap = cap
        self.used = 0

    def read(self, n: int) -> bytes:
        data = self.stream.read(n)
        self.used += len(data)
        if self.used > self.cap:
            raise _refuse("it expands to more data than is allowed")
        return data

    def exact(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            block = self.read(n - len(out))
            if not block:
                raise _refuse("it ends in the middle of an entry")
            out += block
        return bytes(out)


def _octal(field: bytes, what: str) -> int:
    text = field.replace(b"\x00", b" ").strip()
    if not text:
        return 0
    if not all(48 <= c <= 55 for c in text):
        raise _refuse(f"an entry's {what} is not written as a number")
    return int(text, 8)


def _text(field: bytes) -> str:
    raw = field.split(b"\x00", 1)[0]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _refuse("an entry's name is not valid UTF-8 text") from None


def read_members(path: str | os.PathLike[str], limits: Limits = DEFAULT_LIMITS) -> Iterator[tuple[str, str, int, int, Iterator[bytes]]]:
    """Yield ``(name, kind, mode, size, chunks)`` for each entry; ``kind`` is ``"file"`` or
    ``"folder"``. ``chunks`` must be consumed before the next entry is asked for."""
    cap = limits.max_total_bytes + (limits.max_files * 2 + 4) * BLOCK
    with open(path, "rb") as raw:
        try:
            gz = gzip.GzipFile(fileobj=raw, mode="rb")
            src = _Limited(gz, cap)
            count = 0
            total = 0
            zero_blocks = 0
            while True:
                try:
                    header = src.read(BLOCK)
                except (OSError, EOFError):
                    raise _refuse("it is not a gzip archive, or it is damaged") from None
                if not header:
                    if zero_blocks == 0 and count == 0:
                        raise _refuse("it is empty")
                    return
                if len(header) < BLOCK:
                    raise _refuse("it ends in the middle of a header")
                if header == b"\x00" * BLOCK:
                    zero_blocks += 1
                    if zero_blocks >= 2:
                        return
                    continue
                if zero_blocks:
                    raise _refuse("it has entries after its end marker")
                stored = _octal(header[148:156], "checksum")
                computed = sum(header[:148]) + 32 * 8 + sum(header[156:])
                if stored != computed:
                    raise _refuse("an entry's header checksum is wrong")
                if header[257:263] not in (b"ustar\x00", b"ustar "):
                    raise _refuse("it is not a plain ustar archive")
                typeflag = header[156:157]
                name = _text(header[0:100])
                prefix = _text(header[345:500])
                full = f"{prefix}/{name}" if prefix else name
                size = _octal(header[124:136], "size")
                mode = _octal(header[100:108], "mode")
                if typeflag in (b"0", b"\x00"):
                    kind = "file"
                elif typeflag == b"5":
                    kind = "folder"
                    if size:
                        raise _refuse("a folder entry carries data")
                else:
                    kinds = {b"1": "a hard link", b"2": "a symbolic link", b"3": "a device", b"4": "a device",
                             b"6": "a FIFO", b"x": "an extended header", b"g": "an extended header",
                             b"L": "a long-name header", b"K": "a long-link header", b"S": "a sparse file"}
                    raise _refuse(f"it contains {kinds.get(typeflag, 'an entry of an unknown type')}")
                count += 1
                if count > limits.max_files:
                    raise _refuse(f"it has more than {limits.max_files} entries")
                if size > limits.max_file_bytes:
                    raise _refuse(f"a file is larger than {limits.max_file_bytes} bytes")
                total += size
                if total > limits.max_total_bytes:
                    raise _refuse(f"its files add up to more than {limits.max_total_bytes} bytes")
                clean = check_member_path(full)

                def chunks(remaining: int = size) -> Iterator[bytes]:
                    left = remaining
                    while left:
                        block = src.exact(min(CHUNK, left))
                        left -= len(block)
                        yield block
                    pad = (-remaining) % BLOCK
                    if pad:
                        src.exact(pad)

                gen = chunks()
                yield clean, kind, mode, size, gen
                for _ in gen:  # skip what the caller did not read
                    pass
        except (OSError, EOFError, gzip.BadGzipFile):
            raise _refuse("it is not a gzip archive, or it is damaged") from None


def extract(path: str | os.PathLike[str], dest: str | os.PathLike[str], *, limits: Limits = DEFAULT_LIMITS,
            allowed_top: Iterable[str] | None = None) -> dict[str, int]:
    target = Path(dest)
    allowed = set(allowed_top) if allowed_top is not None else None
    os.mkdir(target, 0o700)  # must not exist yet
    files = folders = written = 0
    seen: dict[str, str] = {}
    try:
        dirfd = os.open(target, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            for name, kind, mode, size, chunks in read_members(path, limits):
                parts = name.split("/")
                if allowed is not None and parts[0] not in allowed:
                    raise _refuse(f"it has an unexpected entry at the top: {parts[0][:60]!r}")
                if name in seen:
                    raise _refuse("it has the same path twice")
                for i in range(1, len(parts)):
                    parent = "/".join(parts[:i])
                    if seen.get(parent) == "file":
                        raise _refuse("a path is used both as a file and as a folder")
                    if parent not in seen:
                        _mkdir_at(dirfd, parts[:i])
                        seen[parent] = "folder"
                        folders += 1
                if kind == "folder":
                    _mkdir_at(dirfd, parts)
                    seen[name] = "folder"
                    folders += 1
                    continue
                seen[name] = "file"
                file_mode = 0o700 if mode & 0o111 else 0o600
                fd = _open_at(dirfd, parts, file_mode)
                got = 0
                with os.fdopen(fd, "wb") as f:
                    for block in chunks:
                        f.write(block)
                        got += len(block)
                if got != size:
                    raise _refuse("an entry is shorter than its header says")
                files += 1
                written += size
        finally:
            os.close(dirfd)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {"files": files, "folders": folders, "bytes": written}


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


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


def _mkdir_at(dirfd: int, parts: list[str]) -> None:
    parent = _walk_to(dirfd, parts[:-1])
    try:
        try:
            os.mkdir(parts[-1], 0o700, dir_fd=parent)
        except FileExistsError:
            st = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode):
                raise _refuse("a path is used both as a file and as a folder") from None
    finally:
        os.close(parent)


def _open_at(dirfd: int, parts: list[str], mode: int) -> int:
    parent = _walk_to(dirfd, parts[:-1])
    try:
        return os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, mode, dir_fd=parent)
    except FileExistsError:
        raise _refuse("it has the same path twice") from None
    finally:
        os.close(parent)
