"""Tests for mes backup / mes restore (mes.backup)."""

from __future__ import annotations

import gzip
import io
import json
import os
import stat
import sys
import tarfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from mes import archive, backup, cli, paths
from mes.errors import NotFound, Refused

PROJECT_TOML = '[project]\nname = "keeper"\n'
STORE_TOML = PROJECT_TOML + '\n[storage]\nbucket = "archive"\n'

FAKE_MYBUCKET = """\
#!{python}
import json, shutil, sys
args = sys.argv[1:]
store = args[args.index("--store") + 1]
if "snapshot" in args:
    dest = args[args.index("snapshot") + 1]
    shutil.copytree(store, dest)
    print(json.dumps({{"snapshot": dest}}))
elif "verify" in args:
    print(json.dumps({{"ok": True, "damaged": []}}))
else:
    print(json.dumps({{"error": {{"code": "usage", "message": "unknown", "hint": None}}}}))
    sys.exit(2)
"""


@pytest.fixture(autouse=True)
def with_backup_commands(monkeypatch):
    if "mes.backup" not in cli.COMMAND_MODULES:
        monkeypatch.setattr(cli, "COMMAND_MODULES", cli.COMMAND_MODULES + ("mes.backup",))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "keeper"
    root.mkdir()
    (root / "project.toml").write_text(PROJECT_TOML)
    data = paths.project_data_dir("keeper", create=True)
    state = paths.project_state_dir("keeper", create=True)
    (data / "raw").mkdir()
    (data / "raw" / "notes.jsonl").write_text('{"text": "hello"}\n')
    (data / "keeper.sqlite").write_bytes(os.urandom(5000))
    (state / "runs.jsonl").write_text("{}\n")
    return root


def run(*argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.run(["--json", *argv])
    return code, json.loads(out.getvalue())


def _tree(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*"))
            if p.is_file() and not any(part.startswith(".before-restore-") for part in p.relative_to(folder).parts)}


def test_backup_and_restore_round_trip(project, tmp_path):
    data, state = paths.project_data_dir("keeper"), paths.project_state_dir("keeper")
    before = (_tree(data), _tree(state))
    dest = tmp_path / "backups"
    code, out = run("backup", "--project-dir", str(project), "--to", str(dest))
    assert code == 0, out
    assert out["verified"] is True and out["files"] == 3 and out["store"] is False
    arc = Path(out["archive"])
    assert arc.name.startswith("keeper-") and arc.name.endswith(".tar.gz")
    assert stat.S_IMODE(arc.stat().st_mode) == 0o600 and stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert backup.verify_archive(arc)["ok"]

    # Lose the data, then restore it (nothing there: no --replace needed).
    import shutil

    shutil.rmtree(data)
    shutil.rmtree(state)
    code, ask = run("restore", "keeper", str(arc), "--project-dir", str(project))
    assert code == 6 and ask["status"] == "needs_input" and "--yes" in ask["rerun"]
    assert not data.exists() or not any(data.iterdir())
    code, out = run("restore", "keeper", str(arc), "--project-dir", str(project), "--yes")
    assert code == 0, out
    assert (_tree(data), _tree(state)) == before


def test_restore_refuses_non_empty_and_replace_keeps_old(project, tmp_path):
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    arc = out["archive"]
    data = paths.project_data_dir("keeper")
    (data / "raw" / "notes.jsonl").write_text("changed\n")
    code, out = run("restore", arc, "--project-dir", str(project), "--yes")
    assert code == 5 and out["error"]["code"] == "not_empty"
    assert (data / "raw" / "notes.jsonl").read_text() == "changed\n"
    code, ask = run("restore", arc, "--project-dir", str(project), "--replace")
    assert code == 6 and "moved into .before-restore-" in ask["question"]
    code, out = run("restore", arc, "--project-dir", str(project), "--yes", "--replace")
    assert code == 0, out
    assert (data / "raw" / "notes.jsonl").read_text() == '{"text": "hello"}\n'
    aside = Path(out["moved_aside"]["data"])
    assert (aside / "raw" / "notes.jsonl").read_text() == "changed\n"


def _evil_archive(path: Path, members) -> None:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as t:
        for info, data in members:
            t.addfile(info, io.BytesIO(data) if data is not None else None)
    path.write_bytes(gzip.compress(raw.getvalue()))


def _info(name, data=b"", kind=tarfile.REGTYPE, link=""):
    i = tarfile.TarInfo(name)
    i.type = kind
    i.size = len(data) if kind == tarfile.REGTYPE else 0
    i.linkname = link
    return i


@pytest.mark.parametrize("how", ["flip_byte", "symlink", "dotdot", "absolute", "extra_file", "other_project"])
def test_tampered_archives_are_refused_and_nothing_written(project, tmp_path, how):
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    good = Path(out["archive"])
    bad = tmp_path / "bad.tar.gz"
    members = []
    for name, kind, mode, size, chunks in archive.read_members(good):
        blob = b"".join(chunks)
        if kind == "file":
            members.append((name, blob))
    if how == "flip_byte":
        name, blob = next(m for m in members if m[0].endswith("notes.jsonl"))
        members = [(n, (blob[:-2] + b"X\n") if n == name else b) for n, b in members]
        entries = [(_info(n, b), b) for n, b in members]
    elif how == "symlink":
        entries = [(_info(n, b), b) for n, b in members] + [(_info("data/evil", kind=tarfile.SYMTYPE, link="/etc/passwd"), None)]
    elif how == "dotdot":
        entries = [(_info(n, b), b) for n, b in members] + [(_info("data/../../escape.txt", b"x"), b"x")]
    elif how == "absolute":
        entries = [(_info(n, b), b) for n, b in members] + [(_info("/tmp/escape.txt", b"x"), b"x")]
    elif how == "extra_file":
        entries = [(_info(n, b), b) for n, b in members] + [(_info("data/sneaky.py", b"x"), b"x")]
    else:
        info = json.loads(dict(members)[backup.BACKUP_INFO])
        info["project"] = "someone-else"
        new = json.dumps(info).encode()
        entries = [(_info(n, new if n == backup.BACKUP_INFO else b), new if n == backup.BACKUP_INFO else b)
                   for n, b in members]
    _evil_archive(bad, entries)
    data = paths.project_data_dir("keeper")
    import shutil

    shutil.rmtree(data)
    shutil.rmtree(paths.project_state_dir("keeper"))
    code, out = run("restore", str(bad), "--project-dir", str(project), "--yes")
    assert code == 5, out
    assert out["error"]["code"] in ("archive_refused", "backup_broken", "wrong_project")
    if how == "other_project":
        assert out["error"]["code"] == "wrong_project"
    assert not data.exists() or not any(data.iterdir())
    assert not (tmp_path / "escape.txt").exists()
    state = paths.project_state_dir("keeper")
    assert not state.exists() or not any(p.name.startswith(".mes-restore-") for p in state.iterdir())


def test_backup_refuses_own_folders_and_open_folders(project, tmp_path):
    data = paths.project_data_dir("keeper")
    code, out = run("backup", "--project-dir", str(project), "--to", str(data / "b"))
    assert code == 5 and out["error"]["code"] == "unsafe_folder"
    code, out = run("backup", "--project-dir", str(project), "--to", str(project / "b"))
    assert code == 5
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    code, out = run("backup", "--project-dir", str(project), "--to", str(shared))
    assert code == 5 and out["error"]["code"] == "unsafe_folder"


def test_backup_default_destination_and_skips_links(project, tmp_path):
    data = paths.project_data_dir("keeper")
    (data / "link").symlink_to(tmp_path)
    (data / "filelink").symlink_to(data / "keeper.sqlite")
    code, out = run("backup", "--project-dir", str(project))
    assert code == 0, out
    assert Path(out["archive"]).parent == paths.state_home().parent / "mes-backups" / "keeper"
    assert {s["path"] for s in out["skipped"]} == {"data/link", "data/filelink"}


def test_nothing_to_back_up(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    (root / "project.toml").write_text('[project]\nname = "nada"\n')
    code, out = run("backup", "--project-dir", str(root), "--to", str(tmp_path / "b"))
    assert code == 3 and out["error"]["code"] == "nothing_to_back_up"


def test_name_mismatch(project, tmp_path):
    code, out = run("backup", "other", "--project-dir", str(project))
    assert code == 2


def _store_project(tmp_path, project):
    (project / "project.toml").write_text(STORE_TOML)
    store = paths.project_store_dir("keeper", create=True)
    (store / "buckets").mkdir()
    (store / "buckets" / "archive.sqlite").write_bytes(b"bucket bytes")
    return store


def test_store_uses_mybucket_snapshot(project, tmp_path, monkeypatch):
    store = _store_project(tmp_path, project)
    fake = tmp_path / "mybucket"
    fake.write_text(FAKE_MYBUCKET.format(python=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("MES_MYBUCKET", str(fake))
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    assert code == 0, out
    assert out["store"] is True
    names = {n for n, *_ in archive.read_members(out["archive"]) if True}
    assert "store/buckets/archive.sqlite" in names
    assert not any(n.startswith("data/store/") for n in names)  # the store is not copied twice
    import shutil

    shutil.rmtree(paths.project_data_dir("keeper"))
    shutil.rmtree(paths.project_state_dir("keeper"))
    code, res = run("restore", out["archive"], "--project-dir", str(project), "--yes")
    assert code == 0, res
    assert (store / "buckets" / "archive.sqlite").read_bytes() == b"bucket bytes"


def test_store_without_mybucket_is_refused(project, tmp_path, monkeypatch):
    _store_project(tmp_path, project)
    monkeypatch.setenv("MES_MYBUCKET", str(tmp_path / "not-there"))
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    assert code == 3 and out["error"]["code"] == "mybucket_missing"
    assert "mybucket" in out["error"]["message"]
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"), "--no-store")
    assert code == 0 and out["store"] is False and any("left out" in n for n in out["notes"])


def test_kinds(project, tmp_path, monkeypatch):
    monkeypatch.setenv("MES_ALLOW", "read")
    code, _ = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    assert code == 4
    monkeypatch.setenv("MES_ALLOW", "write")
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    assert code == 0
    code, _ = run("restore", out["archive"], "--project-dir", str(project), "--yes")
    assert code == 4


def test_server_layout(tmp_path):
    root = tmp_path / "srv"
    layout = paths.server_layout("keeper", root)
    for d in (layout.data, layout.state, layout.current_project):
        d.mkdir(parents=True)
        os.chmod(d, 0o700)
    (layout.current_project / "project.toml").write_text(PROJECT_TOML)
    (layout.data / "x.txt").write_text("server data")
    code, out = run("backup", "keeper", "--server-root", str(root))
    assert code == 0, out
    assert Path(out["archive"]).parent == layout.state / "backups"
    code, out2 = run("backup", "keeper", "--server-root", str(root))
    names = {n for n, *_ in archive.read_members(out2["archive"])}
    assert not any(n.startswith("state/backups") for n in names)
    (layout.data / "x.txt").write_text("changed")
    code, ask = run("restore", "keeper", out["archive"], "--server-root", str(root), "--replace")
    assert code == 6 and "--server-root" in ask["rerun"]
    code, res = run("restore", "keeper", out["archive"], "--server-root", str(root), "--replace", "--yes")
    assert code == 0, res
    assert (layout.data / "x.txt").read_text() == "server data"


def test_verify_archive_refuses_missing_info(tmp_path):
    arc = tmp_path / "a.tar.gz"
    archive.pack([("data/x", b"1")], arc)
    with pytest.raises(Refused):
        backup.verify_archive(arc)


def test_restore_missing_archive(project, tmp_path):
    code, out = run("restore", str(tmp_path / "nope.tar.gz"), "--project-dir", str(project), "--yes")
    assert code == 3
