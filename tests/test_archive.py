"""Tests for mes.archive: the packer and the hardened extractor used by deploys and backups."""

from __future__ import annotations

import gzip
import io
import os
import tarfile
from pathlib import Path

import pytest

from mes import archive
from mes.archive import ArchiveError, Limits


def _src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "bin").mkdir(parents=True)
    (src / "a.txt").write_text("hello")
    (src / "bin" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    os.chmod(src / "bin" / "run.sh", 0o755)
    return src


def test_round_trip_keeps_content_and_only_the_exec_bit(tmp_path):
    src = _src(tmp_path)
    out = tmp_path / "x.tar.gz"
    info = archive.pack([("project/a.txt", src / "a.txt"), ("project/bin/run.sh", src / "bin" / "run.sh"),
                         ("MES-RELEASE.json", b"{}")], out)
    assert info["files"] == 3 and info["sha256"] == archive.sha256_file(out)
    got = archive.extract(out, tmp_path / "dest", allowed_top={"project", "MES-RELEASE.json"})
    assert got == {"files": 3, "folders": 2, "bytes": 5 + len("#!/bin/sh\necho hi\n") + 2}
    dest = tmp_path / "dest"
    assert (dest / "project" / "a.txt").read_text() == "hello"
    assert (dest / "project" / "bin" / "run.sh").stat().st_mode & 0o777 == 0o700
    assert (dest / "project" / "a.txt").stat().st_mode & 0o777 == 0o600
    assert (dest / "project").stat().st_mode & 0o777 == 0o700


def test_pack_refuses_links_duplicates_and_bad_names(tmp_path):
    src = _src(tmp_path)
    (src / "link").symlink_to(src / "a.txt")
    with pytest.raises(ValueError):
        archive.pack([("project/link", src / "link")], tmp_path / "1.tgz")
    with pytest.raises(ValueError):
        archive.pack([("a", b"x"), ("a", b"y")], tmp_path / "2.tgz")
    with pytest.raises(ValueError):
        archive.pack([("a", b"x"), ("a/b", b"y")], tmp_path / "3.tgz")
    with pytest.raises(ArchiveError):
        archive.pack([("../evil", b"x")], tmp_path / "4.tgz")


def test_extract_needs_a_new_folder(tmp_path):
    out = tmp_path / "x.tgz"
    archive.pack([("a", b"x")], out)
    (tmp_path / "dest").mkdir()
    with pytest.raises(FileExistsError):
        archive.extract(out, tmp_path / "dest")


def _tar(tmp_path: Path, build, *, name: str = "bad.tgz", fmt=tarfile.USTAR_FORMAT) -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=fmt) as t:
        build(t)
    p = tmp_path / name
    p.write_bytes(buf.getvalue())
    return p


def _file(t, name, data=b"x", **kw):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    for k, v in kw.items():
        setattr(info, k, v)
    t.addfile(info, io.BytesIO(data))


@pytest.mark.parametrize("build, needle", [
    (lambda t: _file(t, "/etc/passwd"), "absolute path"),
    (lambda t: _file(t, "project/../../etc/x"), "'..'"),
    (lambda t: _file(t, "project/./x"), "'..', '.'"),
    (lambda t: _file(t, "project//x"), "empty part"),
    (lambda t: _file(t, "project\\x"), "backslash"),
    (lambda t: _file(t, "project/a\nb"), "control characters"),
    (lambda t: _file(t, "project/x", type=tarfile.SYMTYPE, linkname="/etc/passwd", size=0), "symbolic link"),
    (lambda t: _file(t, "project/x", type=tarfile.LNKTYPE, linkname="project/y", size=0), "hard link"),
    (lambda t: _file(t, "project/x", type=tarfile.CHRTYPE, size=0), "a device"),
    (lambda t: _file(t, "project/x", type=tarfile.FIFOTYPE, size=0), "FIFO"),
    (lambda t: (_file(t, "project/x"), _file(t, "project/x")), "same path twice"),
    (lambda t: (_file(t, "project/x"), _file(t, "project/x/y")), "both as a file and as a folder"),
])
def test_hostile_entries_are_refused_and_nothing_is_left(tmp_path, build, needle):
    bad = _tar(tmp_path, build)
    with pytest.raises(ArchiveError) as e:
        archive.extract(bad, tmp_path / "dest")
    assert needle in e.value.message
    assert e.value.exit_code == 5
    assert not (tmp_path / "dest").exists()


def test_pax_and_gnu_headers_are_refused(tmp_path):
    long = "project/" + "a" * 120 + "/" + "b" * 120 + "/c"
    pax = _tar(tmp_path, lambda t: _file(t, "project/" + "x" * 300), fmt=tarfile.PAX_FORMAT, name="pax.tgz")
    with pytest.raises(ArchiveError) as e:
        archive.extract(pax, tmp_path / "d1")
    assert "extended header" in e.value.message
    gnu = _tar(tmp_path, lambda t: _file(t, long), fmt=tarfile.GNU_FORMAT, name="gnu.tgz")
    with pytest.raises(ArchiveError):
        archive.extract(gnu, tmp_path / "d2")


def test_limits_on_count_size_and_total(tmp_path):
    many = tmp_path / "many.tgz"
    archive.pack([(f"f{i}", b"x") for i in range(5)], many)
    with pytest.raises(ArchiveError) as e:
        archive.extract(many, tmp_path / "d1", limits=Limits(max_files=3, max_file_bytes=10, max_total_bytes=100))
    assert "more than 3 entries" in e.value.message
    big = tmp_path / "big.tgz"
    archive.pack([("f", b"x" * 100)], big)
    with pytest.raises(ArchiveError) as e:
        archive.extract(big, tmp_path / "d2", limits=Limits(max_files=10, max_file_bytes=50, max_total_bytes=1000))
    assert "larger than 50 bytes" in e.value.message
    with pytest.raises(ArchiveError) as e:
        archive.extract(big, tmp_path / "d3", limits=Limits(max_files=10, max_file_bytes=1000, max_total_bytes=60))
    assert "add up to more than" in e.value.message


def test_a_lying_size_header_cannot_expand_without_limit(tmp_path):
    # A header announcing more bytes than allowed is refused before any byte of it is read.
    def build(t):
        info = tarfile.TarInfo("f")
        info.size = 10_000_000
        t.addfile(info, io.BytesIO(b"\0" * 10_000_000))
    bomb = _tar(tmp_path, build)
    assert bomb.stat().st_size < 100_000
    with pytest.raises(ArchiveError):
        archive.extract(bomb, tmp_path / "d", limits=Limits(max_files=10, max_file_bytes=1000, max_total_bytes=1000))
    assert not (tmp_path / "d").exists()


def test_damaged_and_non_gzip_input(tmp_path):
    good = tmp_path / "g.tgz"
    archive.pack([("a", b"hello" * 1000)], good)
    raw = bytearray(good.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    broken = tmp_path / "b.tgz"
    broken.write_bytes(bytes(raw))
    with pytest.raises(ArchiveError):
        archive.extract(broken, tmp_path / "d1")
    plain = tmp_path / "p.tgz"
    plain.write_bytes(b"not an archive at all")
    with pytest.raises(ArchiveError):
        archive.extract(plain, tmp_path / "d2")
    empty = tmp_path / "e.tgz"
    empty.write_bytes(gzip.compress(b""))
    with pytest.raises(ArchiveError) as e:
        archive.extract(empty, tmp_path / "d3")
    assert "empty" in e.value.message


def test_checksum_is_checked(tmp_path):
    out = tmp_path / "x.tgz"
    archive.pack([("abc", b"x")], out)
    raw = bytearray(gzip.decompress(out.read_bytes()))
    raw[0:3] = b"xyz"  # change the name, keep the stored checksum
    bad = tmp_path / "bad.tgz"
    bad.write_bytes(gzip.compress(bytes(raw)))
    with pytest.raises(ArchiveError) as e:
        archive.extract(bad, tmp_path / "d")
    assert "checksum" in e.value.message


def test_allowed_top_level(tmp_path):
    out = tmp_path / "x.tgz"
    archive.pack([("project/a", b"x"), ("other/b", b"y")], out)
    with pytest.raises(ArchiveError) as e:
        archive.extract(out, tmp_path / "d", allowed_top={"project"})
    assert "unexpected entry at the top" in e.value.message


@pytest.mark.parametrize("name", ["", "/x", "a/../b", "a/./b", "a//b", "a\\b", "a\x00b", "x" * 300,
                                  "/".join(["a"] * 40), "é"])
def test_check_member_path(name):
    with pytest.raises(ArchiveError):
        archive.check_member_path(name)


def test_check_member_path_accepts_plain_names():
    assert archive.check_member_path("project/src/app/cli.py") == "project/src/app/cli.py"
    assert archive.check_member_path("project/") == "project"
    assert archive.check_member_path("data/café.json") == "data/café.json"
