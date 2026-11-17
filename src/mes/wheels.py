"""Unpacking a release's own wheels into its environment, without starting any Python.

On a server, ``mes deploy`` installs the wheels it built from the project (and from its local
path dependencies) into the new release's ``.venv``. Asking uv to do that would make uv start
the release's interpreter to learn about it, and starting that interpreter runs every ``.pth``
startup file in its site-packages, that is, code from any package already installed there. So
mes unpacks these wheels itself, with the standard library only:

- only pure-Python wheels (``*-none-any.whl``) whose ``WHEEL`` says ``Wheel-Version: 1.x``;
- every member is a plain file with a safe relative name (no absolute paths, ``..``, backslashes,
  control characters or links), listed in ``RECORD`` with the right SHA-256 and size;
- the file name follows the wheel format: a normalised distribution name (letters and digits in
  runs joined by single ``_``, so never ``.`` or ``..``), a version that starts with a digit, and
  the ``.dist-info`` folder has exactly that name and version;
- files go into the environment's single ``lib/python3.*/site-packages`` folder, never over an
  existing file, through real folders only (``O_NOFOLLOW``, ``O_EXCL``). Of the ``.data/``
  parts, ``purelib`` goes into site-packages too and ``scripts`` into the environment's ``bin/``
  (one plain file name each, a ``#!python`` first line replaced by the environment's Python);
  ``platlib``, ``headers`` and ``data`` are refused with a plain error (a pure-Python release
  has no use for them, and they would write outside site-packages and ``bin/``);
- console scripts from ``entry_points.txt`` are written the way installers write them, with the
  environment's own Python on the first line (uv's long-path form when that line would be too
  long); ``RECORD`` is rewritten with what was installed, and ``INSTALLER`` says ``mes``.

Public API
----------
``WheelError`` (a ``Refused``), ``MAX_FILES``, ``MAX_BYTES``
``site_packages(venv) -> Path``
``install(wheel, venv) -> dict``  ``{"name", "version", "files", "scripts"}``.
"""

from __future__ import annotations

import base64
import configparser
import csv
import hashlib
import io
import os
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from mes.errors import Refused

MAX_FILES = 20_000
MAX_BYTES = 200 * 1024**2
MAX_SHEBANG = 127
# The wheel format's file name: {distribution}-{version}(-{build})?-{python}-{abi}-{platform}.whl, the
# distribution name normalised (runs of "-", "_" and "." become one "_"), so it is never "." or "..".
_DIST_NAME = r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*"
_VERSION = r"[0-9][A-Za-z0-9!+]*(?:[._][A-Za-z0-9!+]+)*"
_WHEEL_NAME_RE = re.compile(rf"^(?P<name>{_DIST_NAME})-(?P<version>{_VERSION})(?:-[0-9][A-Za-z0-9_]*)?"
                            r"-(?P<py>[A-Za-z0-9_.]+)-none-any\.whl\Z")
_DIST_INFO_RE = re.compile(rf"^(?P<name>{_DIST_NAME})-(?P<version>{_VERSION})\.dist-info\Z")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.+@=,~-]+\Z")
_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\Z")
_ENTRY_RE = re.compile(r"^(?P<module>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
                       r":(?P<attr>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*(?:\[[^\]]*\])?\Z")
# The .data/ parts mes installs; platlib, headers and data are refused (see the module's text).
_SCHEMES = ("purelib", "scripts")
_REFUSED_SCHEMES = ("platlib", "headers", "data")


class WheelError(Refused):
    pass


def _bad(wheel: str, why: str) -> WheelError:
    return WheelError(f"The release's wheel {wheel} {why}, so the release was not installed.",
                      hint="Deploy again with 'mes deploy', which builds the wheels on your computer.",
                      code="bad_wheel")


def site_packages(venv: Path) -> Path:
    """The one ``lib/python3.*/site-packages`` folder of an environment (never through a link)."""
    lib = venv / "lib"
    try:
        if not stat.S_ISDIR(os.lstat(venv).st_mode) or not stat.S_ISDIR(os.lstat(lib).st_mode):
            raise OSError
        found = [p / "site-packages" for p in sorted(lib.iterdir())
                 if re.fullmatch(r"python3\.[0-9]{1,3}t?", p.name) and stat.S_ISDIR(os.lstat(p).st_mode)
                 and os.path.lexists(p / "site-packages") and stat.S_ISDIR(os.lstat(p / "site-packages").st_mode)]
    except OSError:
        found = []
    if len(found) != 1 or not (venv / "pyvenv.cfg").is_file():
        raise Refused("The new release has no usable environment (.venv with one site-packages folder), so its "
                      "own wheels could not be installed.", hint="Deploy again; if it keeps happening, check uv on "
                      "the server.", code="no_environment")
    return found[0]


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def _check_name(wheel: str, name: str) -> PurePosixPath:
    if not name or "\\" in name or name.startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise _bad(wheel, f"holds a file with a name mes does not accept ({name[:80]!r})")
    path = PurePosixPath(name)
    if any(part in ("", ".", "..") for part in name.split("/")) or not all(_SEGMENT_RE.match(p) for p in path.parts):
        raise _bad(wheel, f"holds a file with a name mes does not accept ({name[:80]!r})")
    return path


def _mkdirs(base: Path, rel: PurePosixPath) -> None:
    """Make the folders of ``rel`` under ``base``, refusing any part that is a link."""
    cur = base
    for part in rel.parts:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            os.mkdir(cur, 0o755)
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise Refused(f"{cur} is in the way of a wheel's files (it is not a folder), so the release was not "
                          "installed.", hint="Deploy again.", code="bad_wheel")


def _write_new(path: Path, data: bytes, mode: int) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    except FileExistsError:
        raise Refused(f"Two packages of the release want to write the same file ({path.name}), so the release was "
                      "not installed.", hint="Check that no package of the project is also installed from the "
                      "package index.", code="wheel_conflict") from None
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, mode)


def _shebang(python: Path) -> str:
    text = str(python)
    if len(text) + 2 <= MAX_SHEBANG and " " not in text:
        return f"#!{text}\n"
    # uv's form for long paths (also read by 'mes server plan's tool check).
    return f"#!/bin/sh\n'''exec' '{text}' \"$0\" \"$@\"\n' '''\n"


def _script(python: Path, module: str, attr: str) -> bytes:
    head = attr.split(".", 1)[0]
    return (_shebang(python) + "# -*- coding: utf-8 -*-\nimport sys\n"
            f"from {module} import {head}\n"
            "if __name__ == \"__main__\":\n"
            "    if sys.argv[0].endswith(\"-script.pyw\"):\n"
            "        sys.argv[0] = sys.argv[0][:-11]\n"
            "    elif sys.argv[0].endswith(\".exe\"):\n"
            "        sys.argv[0] = sys.argv[0][:-4]\n"
            f"    sys.exit({attr}())\n").encode("utf-8")


def install(wheel: str | os.PathLike[str], venv: str | os.PathLike[str]) -> dict[str, Any]:
    wheel_path, venv = Path(wheel), Path(venv)
    label = wheel_path.name
    m = _WHEEL_NAME_RE.match(label)
    if not m and label.endswith("-none-any.whl"):
        raise _bad(label, "has a file name that does not follow the wheel format (<name>-<version>-py3-none-any.whl, "
                   "the name made only of letters, digits and single '_')")
    if not m or not (m["py"].startswith("py3") or m["py"].startswith("py2.py3")):
        raise _bad(label, "is not a pure-Python wheel (its name must end in -none-any.whl)")
    site = site_packages(venv)
    python = venv / "bin" / "python"
    try:
        zf = zipfile.ZipFile(wheel_path)
    except (OSError, zipfile.BadZipFile):
        raise _bad(label, "is not a readable wheel") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_BYTES:
            raise _bad(label, "is larger than a release's wheel may be")
        names: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            if info.is_dir():
                continue
            kind = (info.external_attr >> 16) & 0o170000
            if kind not in (0, stat.S_IFREG):
                raise _bad(label, f"holds something that is not a plain file ({info.filename[:80]!r})")
            _check_name(label, info.filename)
            if info.filename in names:
                raise _bad(label, f"holds {info.filename[:80]!r} twice")
            names[info.filename] = info
        dist_infos = {n.split("/", 1)[0] for n in names if n.split("/", 1)[0].endswith(".dist-info")}
        if len(dist_infos) != 1:
            raise _bad(label, "does not have exactly one .dist-info folder")
        dist_info = dist_infos.pop()
        dm = _DIST_INFO_RE.match(dist_info)
        if not dm or dm["name"].lower() != m["name"].lower() or dm["version"].lower() != m["version"].lower():
            raise _bad(label, "has a .dist-info folder of another package or version")
        data_dir = dist_info[: -len(".dist-info")] + ".data"
        try:
            wheel_meta = zf.read(f"{dist_info}/WHEEL").decode("utf-8")
            record_text = zf.read(f"{dist_info}/RECORD").decode("utf-8")
        except (KeyError, UnicodeDecodeError):
            raise _bad(label, "has no readable WHEEL or RECORD file") from None
        if not re.search(r"^Wheel-Version:\s*1\.[0-9]+\s*$", wheel_meta, re.M):
            raise _bad(label, "has a wheel format mes does not know")
        record: dict[str, tuple[str, str]] = {}
        for row in csv.reader(io.StringIO(record_text)):
            if not row:
                continue
            if len(row) != 3:
                raise _bad(label, "has a RECORD line mes does not understand")
            record[row[0]] = (row[1], row[2])
        signatures = {f"{dist_info}/RECORD", f"{dist_info}/RECORD.jws", f"{dist_info}/RECORD.p7s"}
        for name in names:
            if name in signatures:
                continue
            if name not in record:
                raise _bad(label, f"holds {name[:80]!r}, which its RECORD does not list")
        for name in record:
            if name not in names and name not in signatures:
                raise _bad(label, f"lists {name[:80]!r} in its RECORD but does not hold it")

        # Where each member goes.
        plan: list[tuple[str, Path, PurePosixPath, bool]] = []  # (member, base, relative path, is a script)
        for name in sorted(names):
            if name in signatures:
                continue
            first, _, rest = name.partition("/")
            if first == data_dir:
                scheme, _, inner = rest.partition("/")
                if scheme in _REFUSED_SCHEMES:
                    raise _bad(label, f"holds {name[:80]!r}, a {scheme} file; mes installs only the Python code "
                               "(purelib) and programs (scripts) of a release's own wheels")
                if scheme not in _SCHEMES or not inner:
                    raise _bad(label, f"holds {name[:80]!r} in a part of the wheel mes does not install")
                rel = PurePosixPath(inner)
                if scheme == "purelib":
                    plan.append((name, site, rel, False))
                else:
                    if len(rel.parts) != 1 or not _SCRIPT_NAME_RE.match(rel.name):
                        raise _bad(label, f"has a script with a name mes does not accept ({inner[:80]!r})")
                    plan.append((name, venv / "bin", rel, True))
            else:
                plan.append((name, site, PurePosixPath(name), False))

        written: list[tuple[str, str, int]] = []
        for name, base, rel, is_script in plan:
            data = zf.read(names[name])
            want_hash, want_size = record[name]
            if want_hash != _record_hash(data) or (want_size and want_size != str(len(data))):
                raise _bad(label, f"has a file whose content does not match its RECORD ({name[:80]!r})")
            if name == f"{dist_info}/RECORD":
                continue
            if is_script and data.startswith(b"#!python"):
                data = _shebang(python).encode() + data.split(b"\n", 1)[1] if b"\n" in data else _shebang(python).encode()
            _mkdirs(base, rel.parent)
            target = base / rel
            mode = 0o755 if is_script else 0o644
            _write_new(target, data, mode)
            written.append((os.path.relpath(target, site), _record_hash(data), len(data)))

        scripts: list[str] = []
        try:
            entry_text = zf.read(f"{dist_info}/entry_points.txt").decode("utf-8")
        except KeyError:
            entry_text = ""
        except UnicodeDecodeError:
            raise _bad(label, "has an entry_points.txt that is not text") from None
        if entry_text:
            cp = configparser.ConfigParser(interpolation=None, delimiters=("=",))
            cp.optionxform = str  # type: ignore[assignment]
            try:
                cp.read_string(entry_text)
            except configparser.Error:
                raise _bad(label, "has an entry_points.txt mes cannot read") from None
            for section in ("console_scripts", "gui_scripts"):
                if not cp.has_section(section):
                    continue
                for script, value in cp.items(section):
                    em = _ENTRY_RE.match(value.strip())
                    if not _SCRIPT_NAME_RE.match(script) or not em:
                        raise _bad(label, f"names a program mes does not accept ({script[:40]!r})")
                    body = _script(python, em["module"], em["attr"])
                    target = venv / "bin" / script
                    _write_new(target, body, 0o755)
                    written.append((os.path.relpath(target, site), _record_hash(body), len(body)))
                    scripts.append(script)

    dist = site / dist_info
    _write_new(dist / "INSTALLER", b"mes\n", 0o644)
    written.append((f"{dist_info}/INSTALLER", _record_hash(b"mes\n"), 4))
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    for row in sorted(written):
        w.writerow(row)
    w.writerow((f"{dist_info}/RECORD", "", ""))
    _write_new(dist / "RECORD", out.getvalue().encode("utf-8"), 0o644)
    return {"name": m["name"], "version": m["version"], "files": len(written) + 1, "scripts": sorted(scripts)}
