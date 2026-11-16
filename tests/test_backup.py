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

PROJECT_TOML = '[project]\nname = "keeper"\napps = ["keeper_app"]\n'
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
def own_home(tmp_path, monkeypatch):
    """No real ~/.ssh/mes-sign may take part: HOME is a fresh folder."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


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
    (state / "cursor.json").write_text('{"page": 3}\n')
    return root


def run(*argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.run(["--json", *argv])
    return code, json.loads(out.getvalue())


def _tree(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*"))
            if p.is_file() and not any(part.startswith(".before-restore-") for part in p.relative_to(folder).parts)
            and p.name not in ("runs.jsonl", "runs.jsonl.head")}


def test_backup_and_restore_round_trip(project, tmp_path):
    data, state = paths.project_data_dir("keeper"), paths.project_state_dir("keeper")
    before = (_tree(data), _tree(state))
    dest = tmp_path / "backups"
    code, out = run("backup", "--project-dir", str(project), "--to", str(dest))
    assert code == 0, out
    assert out["verified"] is True and out["files"] == 3 and out["store"] is False
    assert out["signed"] is False and any("integrity" in n and out["sha256"] in n for n in out["notes"])
    names = {n for n, *_ in archive.read_members(out["archive"])}
    assert "state/runs.jsonl" not in names  # the run log is never part of a backup
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
    assert ask["recommended"] == "no"
    assert "NOT signed" in ask["why"] and out["sha256"] in ask["why"]
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
    (root / "project.toml").write_text('[project]\nname = "nada"\napps = ["nada_app"]\n')
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
    for d in (layout.data, layout.state, layout.current_project, layout.base / "backups"):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    (layout.current_project / "project.toml").write_text(PROJECT_TOML)
    (layout.data / "x.txt").write_text("server data")
    code, out = run("backup", "keeper", "--server-root", str(root))
    assert code == 0, out
    # Audit F4: never in state/, which the project's jobs can change.
    assert Path(out["archive"]).parent == layout.base / "backups"
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


def test_the_shared_database_is_copied_with_sqlite_and_its_journal_left_out(project, tmp_path):
    import sqlite3

    data = paths.project_data_dir("keeper")
    db_file = data / "keeper.sqlite"
    db_file.unlink()
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    conn.commit()
    conn.close()
    (data / "keeper.sqlite-journal").write_bytes(b"left over")
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    assert code == 0, out
    with tarfile.open(out["archive"]) as t:
        names = t.getnames()
        assert "data/keeper.sqlite" in names and "data/keeper.sqlite-journal" not in names
        copy = tmp_path / "copy.sqlite"
        copy.write_bytes(t.extractfile("data/keeper.sqlite").read())
    assert sqlite3.connect(copy).execute("SELECT count(*) FROM t").fetchone() == (100,)


def test_a_damaged_database_file_is_kept_as_it_is(project, tmp_path):
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    assert code == 0, out
    assert any("not a readable SQLite database" in n for n in out["notes"])


# --------------------------------------------------------------------------- audit F3: origin


needs_keygen = pytest.mark.skipif(__import__("shutil").which("ssh-keygen") is None, reason="needs ssh-keygen")


def _key(tmp_path, name="sign"):
    import subprocess

    k = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(k)], check=True)
    return k


def _wipe():
    import shutil

    shutil.rmtree(paths.project_data_dir("keeper"), ignore_errors=True)
    shutil.rmtree(paths.project_state_dir("keeper"), ignore_errors=True)


@needs_keygen
def test_signed_backup_round_trip(project, tmp_path):
    key = _key(tmp_path)
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"), "--key", str(key))
    assert code == 0, out
    assert out["signed"] is True and Path(out["signature"]).is_file()
    assert stat.S_IMODE(Path(out["signature"]).stat().st_mode) == 0o600
    _wipe()
    code, ask = run("restore", out["archive"], "--project-dir", str(project), "--signer", f"{key}.pub")
    assert code == 6 and "signed by SHA256:" in ask["why"] and ask["recommended"] == "no"
    code, res = run("restore", out["archive"], "--project-dir", str(project), "--signer", f"{key}.pub", "--yes")
    assert code == 0, res
    assert res["signed"] is True


@needs_keygen
def test_a_backup_signed_by_someone_else_or_changed_is_refused(project, tmp_path):
    """Audit F3: anyone could make a backup that restores as genuine."""
    mine, stranger = _key(tmp_path, "mine"), _key(tmp_path, "stranger")
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"), "--key", str(stranger))
    assert code == 0, out
    _wipe()
    code, res = run("restore", out["archive"], "--project-dir", str(project), "--signer", f"{mine}.pub", "--yes")
    assert code == 5 and res["error"]["code"] == "bad_signature"
    paths.project_data_dir("keeper", create=True).joinpath("x.txt").write_text("again")
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "c"), "--key", str(mine))
    assert code == 0, out
    _wipe()
    raw = bytearray(Path(out["archive"]).read_bytes())
    raw[-40] ^= 1
    Path(out["archive"]).write_bytes(bytes(raw))
    code, res = run("restore", out["archive"], "--project-dir", str(project), "--signer", f"{mine}.pub", "--yes")
    assert code == 5 and res["error"]["code"] == "bad_signature"
    assert not paths.project_data_dir("keeper").exists() or not any(paths.project_data_dir("keeper").iterdir())


@needs_keygen
def test_an_unsigned_backup_is_refused_when_a_signing_key_is_set_up(project, tmp_path, own_home):
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    assert code == 0 and out["signed"] is False
    (own_home / ".ssh").mkdir()
    _key(own_home / ".ssh", "mes-sign")  # the default signing key now exists
    _wipe()
    code, res = run("restore", out["archive"], "--project-dir", str(project), "--yes")
    assert code == 5 and res["error"]["code"] == "unsigned"
    code, res = run("restore", out["archive"], "--project-dir", str(project), "--yes", "--allow-unsigned")
    assert code == 0 and res["signed"] is False and "NOT signed" in res["message"]
    # And new backups are signed with it without asking.
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "c"))
    assert code == 0 and out["signed"] is True


@needs_keygen
def test_a_signed_backup_without_a_key_to_check_it_is_refused(project, tmp_path):
    key = _key(tmp_path)
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"), "--key", str(key))
    _wipe()
    code, res = run("restore", out["archive"], "--project-dir", str(project), "--yes")
    assert code == 5 and res["error"]["code"] == "no_signer_key"


def _rewrite(good: Path, bad: Path, change_info=None, extra=()):
    members = []
    for name, kind, mode, size, chunks in archive.read_members(good):
        blob = b"".join(chunks)
        if kind == "file":
            members.append((name, blob))
    info = json.loads(dict(members)[backup.BACKUP_INFO])
    for name, blob in extra:
        members.append((name, blob))
        import hashlib

        info["files"].append({"path": name, "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})
    if change_info:
        change_info(info)
    new = json.dumps(info).encode()
    entries = [(_info(n, new if n == backup.BACKUP_INFO else b), new if n == backup.BACKUP_INFO else b)
               for n, b in members]
    _evil_archive(bad, entries)


@pytest.mark.parametrize("created", ["yesterday", "2026-01-01T00:00:00", "2026-01-01T00:00:00+00:00\nRun: rm -rf ~",
                                     12345, None])
def test_created_must_be_a_real_timestamp(project, tmp_path, created):
    """Audit F3: 'created' went into the question shown to the person as it was."""
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    bad = tmp_path / "bad.tar.gz"
    _rewrite(Path(out["archive"]), bad, change_info=lambda i: i.update(created=created))
    _wipe()
    code, res = run("restore", str(bad), "--project-dir", str(project))
    assert code == 5 and res["error"]["code"] == "backup_broken"


def test_the_run_log_and_gate_log_are_never_restored(project, tmp_path):
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    bad = tmp_path / "old.tar.gz"
    _rewrite(Path(out["archive"]), bad, extra=[("state/runs.jsonl", b'{"forged": true}\n'),
                                              ("state/gate/gate-log.jsonl", b'{"forged": true}\n')])
    state = paths.project_state_dir("keeper")
    log_before = (state / "runs.jsonl").read_text()
    code, ask = run("restore", str(bad), "--project-dir", str(project), "--replace")
    assert code == 6 and "forged" not in json.dumps(ask)
    code, res = run("restore", str(bad), "--project-dir", str(project), "--replace", "--yes")
    assert code == 0, res
    assert (state / "runs.jsonl").read_text() == log_before  # neither replaced nor moved aside
    assert not (state / "gate").exists()
    assert set(res["not_restored"]) == {"state/runs.jsonl", "state/gate/gate-log.jsonl"}


# --------------------------------------------------------------------------- audit F4: who and where


def test_backup_and_restore_refuse_root(project, tmp_path, monkeypatch):
    monkeypatch.setattr(backup.os, "geteuid", lambda: 0)
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    assert code == 5 and out["error"]["code"] == "as_root"
    code, out = run("restore", str(tmp_path / "x.tar.gz"), "--project-dir", str(project), "--yes")
    assert code == 5 and out["error"]["code"] == "as_root"


def test_folders_of_another_account_are_refused(project, tmp_path, monkeypatch):
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    me = os.geteuid()
    dest = tmp_path / "other-dest"
    monkeypatch.setattr(backup.os, "geteuid", lambda: me + 4242)
    code, res = run("restore", out["archive"], "--project-dir", str(project), "--replace", "--yes")
    assert code == 5 and res["error"]["code"] == "not_owner"
    code, res = run("backup", "--project-dir", str(project), "--to", str(dest))
    assert code == 5 and res["error"]["code"] in ("not_owner", "unsafe_folder")


def test_a_data_folder_that_is_a_link_is_refused(project, tmp_path):
    import shutil

    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    data = paths.project_data_dir("keeper")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(data)
    data.symlink_to(elsewhere)
    code, res = run("restore", out["archive"], "--project-dir", str(project), "--replace", "--yes")
    assert code == 5 and res["error"]["code"] == "unsafe_folder"
    assert list(elsewhere.iterdir()) == []
    code, res = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "c"))
    assert code == 5 and res["error"]["code"] == "unsafe_folder"


def test_restore_never_writes_through_a_link_planted_in_a_skipped_name(tmp_path):
    """Audit F4: a name restore leaves alone (state/backups on a server) could be a link, and the
    files from the archive were then moved into wherever it pointed."""
    root = tmp_path / "srv"
    layout = paths.server_layout("keeper", root)
    for d in (layout.data, layout.state, layout.current_project, layout.base / "backups"):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    (layout.current_project / "project.toml").write_text(PROJECT_TOML)
    (layout.data / "x.txt").write_text("server data")
    code, out = run("backup", "keeper", "--server-root", str(root))
    assert code == 0, out
    bad = tmp_path / "bad.tar.gz"
    _rewrite(Path(out["archive"]), bad, extra=[("state/backups/planted.txt", b"evil"),
                                              ("data/.before-restore-x/planted.txt", b"evil")])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (layout.state / "backups").symlink_to(elsewhere)
    code, res = run("restore", "keeper", str(bad), "--server-root", str(root), "--replace", "--yes")
    assert code == 0, res
    assert list(elsewhere.iterdir()) == []
    assert (layout.state / "backups").is_symlink()  # left alone, not followed
    assert not (layout.data / ".before-restore-x").exists()


def test_backup_reads_files_without_following_links(project, tmp_path):
    """Audit F4: a link planted as a file in a live folder is skipped, never read."""
    secret = tmp_path / "owners-secret"
    secret.write_text("private")
    data = paths.project_data_dir("keeper")
    (data / "raw" / "innocent.txt").symlink_to(secret)
    code, out = run("backup", "--project-dir", str(project), "--to", str(tmp_path / "b"))
    assert code == 0
    assert {"path": "data/raw/innocent.txt", "why": "a link to somewhere else"} in out["skipped"]
    for name, kind, mode, size, chunks in archive.read_members(out["archive"]):
        assert b"private" not in b"".join(chunks)


def test_rename_never_replaces(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f").write_text("new")
    (b / "f").write_text("old")
    fa, fb = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    try:
        with pytest.raises(Refused):
            backup._rename_noreplace(fa, "f", fb, "f")
        assert (b / "f").read_text() == "old"
        backup._rename_noreplace(fa, "f", fb, "g")
        assert (b / "g").read_text() == "new"
    finally:
        os.close(fa)
        os.close(fb)
