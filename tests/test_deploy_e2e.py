"""End-to-end deploy on this computer: "the server" is a temporary folder driven through the gate.

- A throwaway Ed25519 signing key is made with ssh-keygen for each test session.
- ``ssh`` is a fake on PATH: it does what sshd does with a forced command, setting
  SSH_ORIGINAL_COMMAND to the request and running ``mes gate --project demo --allow <level>
  --server-root <tmp>`` with the archive on stdin.
- ``uv`` is a fake. On this computer ``uv build --wheel`` writes a small real wheel named after
  the folder's pyproject.toml (its metadata and console scripts, no code). On "the server" ``uv
  sync --frozen --no-build --no-dev --no-install-project ...`` checks that the release has no
  environment yet and makes a fresh one (a link to the base Python and a .pth that stands in for
  the third-party packages); mes then unpacks the archive's own wheels itself (``mes.wheels``),
  which writes ``.venv/bin/demo`` and ``.venv/bin/demo-app``. ``uv pip`` must never be called.
- The test user plays both root (it owns the project's top folder and the signers file) and
  mes-demo (it owns code/), which is the only difference from a real server.

Linux-only parts (the root script, system units, the per-request sandbox sockets) are not used
here; they are tested in test_server.py and test_units.py.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import types
from pathlib import Path

import pytest

import mes
from mes import deploy, paths
from mes.errors import MesError, NotFound, Refused

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None or shutil.which("git") is None,
                                reason="needs ssh-keygen and git")

MES_SRC = str(Path(mes.__file__).resolve().parent.parent)

CLI = '''
from mes import App
app = App("demo-app")


@app.command("hello", kind="read")
def hello(ctx):
    """Say hello."""
    return {"hello": "world", "version": VERSION}


@app.command("rebuild", kind="write")
def rebuild(ctx):
    """Rebuild."""
    return {"ok": True}


def main():
    app.main()
'''

PROJECT_TOML = '''
[project]
name = "demo"
title = "Demo"
version = "{version}"

apps = ["demo_app"]
'''

APP_TOML = '''
[app]
name = "demo-app"
cli = "demo_app.cli:app"

[jobs.rebuild]
run = "rebuild"
manual = true
'''

FAKE_UV = '''#!{python}
import base64, hashlib, json, os, sys, tomllib, zipfile
args = sys.argv[1:]
log = {log!r}
if log:
    with open(log, "a") as f:
        f.write(json.dumps({{"args": args, "cwd": os.getcwd(), "home": os.environ.get("HOME"),
                             "no_config": os.environ.get("UV_NO_CONFIG")}}) + "\\n")
if args[:2] == ["build", "--wheel"]:
    out, folder = args[args.index("--out-dir") + 1], args[-1]
    meta = tomllib.load(open(os.path.join(folder, "pyproject.toml"), "rb"))["project"]
    assert not os.path.exists(os.path.join(folder, "UNTRACKED")), "built from the working folder"
    name, version = meta["name"].replace("-", "_"), meta["version"]
    dist = "%s-%s.dist-info" % (name, version)
    files = {{dist + "/METADATA": ("Metadata-Version: 2.1\\nName: %s\\nVersion: %s\\n" % (meta["name"], version)).encode(),
             dist + "/WHEEL": b"Wheel-Version: 1.0\\nGenerator: fake\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"}}
    scripts = meta.get("scripts") or {{}}
    if scripts:
        files[dist + "/entry_points.txt"] = ("[console_scripts]\\n" + "".join(
            "%s = %s\\n" % kv for kv in scripts.items())).encode()
    def h(b):
        return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(b).digest()).rstrip(b"=").decode()
    files[dist + "/RECORD"] = ("".join("%s,%s,%d\\n" % (k, h(v), len(v)) for k, v in files.items())
                               + dist + "/RECORD,,\\n").encode()
    with zipfile.ZipFile(os.path.join(out, "%s-%s-py3-none-any.whl" % (name, version)), "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    sys.exit(0)
assert args[0] != "pip", "the server must never ask uv to install into the release's environment"
assert args[:6] == ["sync", "--frozen", "--no-progress", "--no-build", "--no-dev", "--no-install-project"], args
assert not any("/.venv/" in a or a.endswith("/.venv") for a in args), args
here = os.getcwd()
assert not os.path.lexists(os.path.join(here, ".venv")), "uv sync must start from a fresh environment"
if os.path.exists(os.path.join(here, "FAIL_SYNC")):
    sys.stderr.write("could not resolve packages\\n")
    sys.exit(1)
# A fresh environment, as uv makes it: its Python is a link to the base one, and (standing in for
# the locked third-party packages) a .pth that puts the project's apps and mes on the path.
venv = os.path.join(here, ".venv")
site = os.path.join(venv, "lib", "python%d.%d" % sys.version_info[:2], "site-packages")
os.makedirs(site)
os.makedirs(os.path.join(venv, "bin"))
base = os.path.realpath(getattr(sys, "_base_executable", sys.executable))
os.symlink(base, os.path.join(venv, "bin", "python"))
with open(os.path.join(venv, "pyvenv.cfg"), "w") as f:
    f.write("home = %s\\ninclude-system-site-packages = false\\nversion_info = %d.%d\\n"
            % ((os.path.dirname(base),) + tuple(sys.version_info[:2])))
with open(os.path.join(site, "_e2e_paths.pth"), "w") as f:
    f.write(os.path.join(here, "apps") + "\\n" + {mes_src!r} + "\\n")
'''

FAKE_SSH = '''#!{python}
import os, subprocess, sys
args = sys.argv[1:]
assert "BatchMode=yes" in args and "ForwardAgent=no" in args and "--" in args, args
dest, command = args[args.index("--") + 1:]
assert dest == "mes-demo@my-server", dest
env = dict(os.environ, SSH_ORIGINAL_COMMAND=command)
level = os.environ.get("FAKE_SSH_LEVEL", "deploy")
boot = ("import sys, mes.units, mes.gate; mes.units.UNSAFE_TOOL_ROOTS = (); mes.gate.REQUIRE_SANDBOX_ON_SERVER = False; "
        "from mes.cli import main; sys.argv[0] = 'mes'; main()")
sys.exit(subprocess.call([sys.executable, "-c", boot, "gate", "--project", "demo", "--allow", level,
                          "--server-root", {root!r}], env=env))
'''


def _script(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o700)
    return path


@pytest.fixture(scope="session")
def signing_key(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    for name in ("deploy", "stranger"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(d / name)], check=True)
    return d


def _git(folder: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(folder), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


class World:
    def __init__(self, tmp: Path, keys: Path):
        self.tmp = tmp
        self.keys = keys
        self.root = tmp / "srv"
        self.base = self.root / "demo"
        self.project = tmp / "work" / "demo"

    def write_project(self, version: str) -> None:
        p = self.project
        (p / "apps" / "demo_app").mkdir(parents=True, exist_ok=True)
        (p / "project.toml").write_text(PROJECT_TOML.format(version=version))
        (p / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "%s"\n'
                                          '[project.scripts]\ndemo = "mes.project:main"\n'
                                          'demo-app = "demo_app.cli:main"\n' % version)
        (p / "uv.lock").write_text("version = 1\n")
        (p / "apps" / "demo_app" / "__init__.py").write_text("")
        (p / "apps" / "demo_app" / "app.toml").write_text(APP_TOML)
        (p / "apps" / "demo_app" / "cli.py").write_text(f"VERSION = {version!r}\n" + CLI)
        _git(p, "add", "-A")
        _git(p, "commit", "-q", "-m", f"version {version}")


@pytest.fixture
def world(tmp_path, signing_key, monkeypatch):
    w = World(tmp_path, signing_key)
    w.project.mkdir(parents=True)
    _git(w.project, "init", "-q")
    w.write_project("0.1.0")
    tools = tmp_path / "tools"
    tools.mkdir()
    uv = _script(tools / "uv", FAKE_UV.format(python=sys.executable, mes_src=MES_SRC,
                                              log=str(tmp_path / "uv-log.jsonl")))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _script(bindir / "ssh", FAKE_SSH.format(python=sys.executable, root=str(w.root)))
    shutil.copy(uv, bindir / "uv")  # the client builds the wheels with uv too
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    # The server's layout, as the root script of 'mes server plan' leaves it.
    for sub, mode in (("", 0o711), ("code", 0o700), ("data", 0o700), ("state", 0o700), ("store", 0o700),
                      ("gate", 0o700), ("webhook", 0o700), ("ui", 0o750), ("etc", 0o750), ("log", 0o750)):
        (w.base / sub).mkdir(mode=mode, parents=True, exist_ok=True)
        (w.base / sub).chmod(mode)
    pub = (signing_key / "deploy.pub").read_text().split()
    (w.base / "etc" / "allowed_signers").write_text(f'demo namespaces="mes-deploy,mes-rollback" {pub[0]} {pub[1]}\n')
    (w.base / "etc" / "server.toml").write_text(
        f'[server]\nproject = "demo"\nroot = "{w.root}"\nmes = "/usr/local/bin/mes"\nuv = "{uv}"\n')
    for f in ("allowed_signers", "server.toml"):
        (w.base / "etc" / f).chmod(0o640)
    return w


def _deploy(w: World, **kw):
    args = dict(project_dir=str(w.project), key=str(w.keys / "deploy"), dry_run=False, rollback=False, to=None,
                releases=False, timeout=120)
    args.update(kw)
    return deploy.run_deploy("my-server", **args)


def _signed(w: World, key: str = "deploy", name: str = "a.tgz") -> tuple[Path, str]:
    out = w.tmp / name
    project = deploy.manifest_mod.load(w.project)
    deploy.build_archive(w.project, project, out)
    return out, deploy.sign(out, w.keys / key)


def _releases(w: World) -> list[str]:
    return sorted(p.name for p in (w.base / "code" / "releases").iterdir())


def _ask(w: World, request: list[str], level: str = "read") -> subprocess.CompletedProcess[str]:
    return subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ForwardAgent=no", "--", "mes-demo@my-server",
                           json.dumps(request)], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env={**os.environ, "FAKE_SSH_LEVEL": level})


def test_deploy_runs_the_new_release_through_the_gate(world):
    w = world
    dry = _deploy(w, dry_run=True)
    assert dry["dry_run"] and not (w.base / "code" / "releases").exists()
    first = _deploy(w)
    assert first["deployed"] and first["release"] == 1 and first["previous"] is None, first
    assert first["signer"].startswith("SHA256:")
    assert os.readlink(w.base / "code" / "current") == "releases/1"
    assert not list((w.base / "code" / "incoming").iterdir())
    ran = _ask(w, ["demo", "hello", "--json"])
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout) == {"hello": "world", "version": "0.1.0"}

    w.write_project("0.2.0")
    second = _deploy(w)
    assert second["release"] == 2 and second["previous"] == 1
    assert json.loads(_ask(w, ["demo", "hello", "--json"]).stdout)["version"] == "0.2.0"
    listed = _deploy(w, releases=True)
    assert [r["release"] for r in listed["releases"]] == [2, 1] and listed["current"] == 2

    back = _deploy(w, rollback=True)
    assert back["rolled_back"] and back["release"] == 1 and back["from"] == 2
    assert json.loads(_ask(w, ["demo", "hello", "--json"]).stdout)["version"] == "0.1.0"
    again = _deploy(w, rollback=True, to=2)
    assert again["release"] == 2
    log = [json.loads(ln) for ln in (w.base / "gate" / deploy.DEPLOY_LOG).read_text().splitlines()]
    assert [e["event"] for e in log] == ["deployed", "deployed", "rolled_back", "rolled_back"]
    gate_log = (w.base / "gate" / "gate-log.jsonl").read_text()
    assert '"deploy"' in gate_log
    assert not (w.base / "state" / "gate").exists() and not (w.base / "code" / deploy.DEPLOY_LOG).exists()
    # The server installed only ready-made wheels, with uv's own settings and caches out of reach.
    calls = [json.loads(ln) for ln in (w.tmp / "uv-log.jsonl").read_text().splitlines()]
    server_calls = [c for c in calls if c["args"][0] in ("sync", "pip")]
    assert server_calls and all(c["home"] == "/nonexistent" and c["no_config"] == "1" for c in server_calls)
    assert all("--no-build" in c["args"] and "--no-dev" in c["args"] for c in server_calls)
    # uv is never asked to install into (and so never starts) the release's own Python: mes
    # unpacked the project's wheel itself.
    assert not [c for c in calls if c["args"][0] == "pip"]
    site = next((w.base / "code" / "releases" / "1" / "project" / ".venv" / "lib").glob("python*/site-packages"))
    assert (site / "demo-0.1.0.dist-info" / "INSTALLER").read_text() == "mes\n"
    assert not os.path.lexists(w.base / "code" / "installing")


def test_rollback_is_signed_and_cannot_be_replayed(world):
    """Finding 5: '["mes", "rollback"]' needed only the deploy login key."""
    w = world
    _deploy(w)
    w.write_project("0.2.0")
    _deploy(w)
    with pytest.raises(MesError) as e:  # unsigned
        deploy._send("mes-demo@my-server", ["mes", "rollback", "--to", "1"], None, 60)
    assert e.value.exit_code == 2 and "signed with the signing key" in e.value.message
    import time as _time

    when, nonce = int(_time.time()), "ab" * 16
    msg = w.tmp / "rb.txt"
    msg.write_bytes(deploy.rollback_message("demo", 1, when, nonce))
    sig = deploy.sign(msg, w.keys / "deploy", deploy.ROLLBACK_NAMESPACE)
    req = ["mes", "rollback", "--to", "1", "--time", str(when), "--nonce", nonce, "--signature", sig]
    assert deploy._send("mes-demo@my-server", req, None, 60)["release"] == 1
    with pytest.raises(Refused) as e:
        deploy._send("mes-demo@my-server", req, None, 60)
    assert e.value.code == "replay"
    # A deploy signature is not a rollback signature (namespaces differ), and a stale one is refused.
    old = when - deploy.ROLLBACK_WINDOW - 10
    msg.write_bytes(deploy.rollback_message("demo", 2, old, "cd" * 16))
    sig = deploy.sign(msg, w.keys / "deploy", deploy.ROLLBACK_NAMESPACE)
    with pytest.raises(Refused) as e:
        deploy._send("mes-demo@my-server", ["mes", "rollback", "--to", "2", "--time", str(old), "--nonce", "cd" * 16,
                                            "--signature", sig], None, 60)
    assert e.value.code == "stale_request"
    msg.write_bytes(deploy.rollback_message("demo", 2, when, "ef" * 16))
    sig = deploy.sign(msg, w.keys / "deploy")  # mes-deploy namespace
    with pytest.raises(Refused) as e:
        deploy._send("mes-demo@my-server", ["mes", "rollback", "--to", "2", "--time", str(when), "--nonce", "ef" * 16,
                                            "--signature", sig], None, 60)
    assert e.value.code == "bad_signature"
    assert os.readlink(w.base / "code" / "current") == "releases/1"


def test_only_the_last_commit_is_packed(world):
    """Audit F10: the packer read the working folder (a link put in place of a folder was followed,
    and untracked files under the app's folder ended up in wheels)."""
    w = world
    (w.project / "apps" / "demo_app" / "UNTRACKED").write_text("secret")
    (w.project / "apps" / "demo_app" / "cli.py").write_text("uncommitted change")
    out, _ = _signed(w)
    with tarfile.open(out) as t:
        names = t.getnames()
        cli = t.extractfile("project/apps/demo_app/cli.py").read().decode()
    assert "project/apps/demo_app/UNTRACKED" not in names and "uncommitted change" not in cli
    assert any(n.startswith("wheels/demo-0.1.0") for n in names)
    elsewhere = w.tmp / "elsewhere"
    shutil.copytree(w.project / "apps", elsewhere)
    (elsewhere / "demo_app" / "cli.py").write_text("from a linked folder")
    (elsewhere / "demo_app" / "UNTRACKED").unlink()
    shutil.rmtree(w.project / "apps")
    (w.project / "apps").symlink_to(elsewhere)
    out2, _ = _signed(w, name="b.tgz")
    with tarfile.open(out2) as t:
        cli = t.extractfile("project/apps/demo_app/cli.py").read().decode()
    assert "from a linked folder" not in cli and "VERSION" in cli


def test_an_interrupted_release_does_not_block_sending_it_again(world):
    """Finding 14: a release unpacked but never completed blocked the same archive forever."""
    w = world
    out, sig = _signed(w)
    (w.base / "code" / "releases" / "1").mkdir(parents=True)
    (w.base / "code" / "releases" / "1" / "half").write_text("x")
    assert deploy._send("mes-demo@my-server", ["mes", "deploy", "--signature", sig], out, 60)["release"] >= 1
    assert (w.base / "gate" / "releases").is_dir()


def test_only_the_deploy_level_may_deploy(world):
    w = world
    for level in ("read", "write"):
        os.environ["FAKE_SSH_LEVEL"] = level
        try:
            with pytest.raises(MesError) as e:
                _deploy(w)
        finally:
            del os.environ["FAKE_SSH_LEVEL"]
        assert e.value.exit_code == 4, e.value.message
    assert not (w.base / "code" / "releases").exists() or not _releases(w)


def test_a_replayed_or_older_signed_archive_is_refused(world):
    w = world
    old, old_sig = _signed(w, name="old.tgz")
    w.write_project("0.2.0")
    new, new_sig = _signed(w, name="new.tgz")
    assert deploy._send("mes-demo@my-server", ["mes", "deploy", "--signature", new_sig], new, 60)["release"] == 1
    with pytest.raises(Refused) as e:
        deploy._send("mes-demo@my-server", ["mes", "deploy", "--signature", new_sig], new, 60)
    assert e.value.code == "already_installed"
    with pytest.raises(Refused) as e:
        deploy._send("mes-demo@my-server", ["mes", "deploy", "--signature", old_sig], old, 60)
    assert e.value.code == "older_release"
    assert _releases(w) == ["1"]


def test_a_changed_archive_or_an_unknown_key_is_refused_before_unpacking(world):
    w = world
    out, sig = _signed(w)
    raw = bytearray(out.read_bytes())
    raw[-30] ^= 0x01
    out.write_bytes(bytes(raw))
    with pytest.raises(Refused) as e:
        deploy._send("mes-demo@my-server", ["mes", "deploy", "--signature", sig], out, 60)
    assert e.value.code == "bad_signature"
    other, other_sig = _signed(w, key="stranger", name="b.tgz")
    with pytest.raises(Refused) as e:
        deploy._send("mes-demo@my-server", ["mes", "deploy", "--signature", other_sig], other, 60)
    assert e.value.code == "bad_signature"
    with pytest.raises(Refused):
        deploy._send("mes-demo@my-server", ["mes", "deploy", "--signature", "not a signature"], other, 60)
    assert _releases(w) == []
    assert not list((w.base / "code" / "incoming").iterdir())


def _hostile_archive(w: World, build) -> Path:
    out = w.tmp / "hostile.tgz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.USTAR_FORMAT) as t:
        build(t)
    out.write_bytes(buf.getvalue())
    return out


def _add(t, name, data=b"x", **kw):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    for k, v in kw.items():
        setattr(info, k, v)
    t.addfile(info, io.BytesIO(data))


@pytest.mark.parametrize("build, code", [
    (lambda t: _add(t, "project/x", type=tarfile.SYMTYPE, linkname="/etc", size=0), "archive_refused"),
    (lambda t: _add(t, "../../escape"), "archive_refused"),
    (lambda t: _add(t, "MES-RELEASE.json", json.dumps({"format": 1, "project": "other", "created_ns": 1,
                                                       "deps": []}).encode()), "wrong_project"),
    (lambda t: (_add(t, "MES-RELEASE.json", json.dumps({"format": 1, "project": "demo", "created_ns": 1,
                                                        "deps": []}).encode()),
                _add(t, "project/project.toml", PROJECT_TOML.format(version="1").encode()),
                _add(t, "project/uv.lock", b""), _add(t, "extra/x")), "bad_release"),
])
def test_signed_but_hostile_archives_are_refused_and_leave_nothing(world, build, code):
    w = world
    out = _hostile_archive(w, build)
    sig = deploy.sign(out, w.keys / "deploy")
    with pytest.raises(Refused) as e:
        deploy._send("mes-demo@my-server", ["mes", "deploy", "--signature", sig], out, 60)
    assert e.value.code == code
    assert _releases(w) == []
    assert not (w.tmp / "escape").exists()


def test_a_failed_install_keeps_the_running_release(world):
    w = world
    assert _deploy(w)["release"] == 1
    (w.project / "FAIL_SYNC").write_text("")
    _git(w.project, "add", "FAIL_SYNC")
    _git(w.project, "commit", "-q", "-m", "break")
    with pytest.raises(MesError) as e:
        _deploy(w)
    assert e.value.code == "sync_failed"
    assert os.readlink(w.base / "code" / "current") == "releases/1"
    assert _releases(w) == ["1"]


def test_signers_that_others_could_change_are_not_trusted(world):
    w = world
    (w.base / "etc" / "allowed_signers").chmod(0o666)
    with pytest.raises(Refused) as e:
        _deploy(w)
    assert e.value.code == "untrusted_file"
    (w.base / "etc" / "allowed_signers").chmod(0o640)
    rsa = w.tmp / "rsa"
    subprocess.run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "", "-f", str(rsa)], check=True)
    kind, b64 = rsa.with_name("rsa.pub").read_text().split()[:2]
    (w.base / "etc" / "allowed_signers").write_text(f'demo namespaces="mes-deploy,mes-rollback" {kind} {b64}\n')
    with pytest.raises(Refused) as e:
        _deploy(w)
    assert e.value.code == "key_type"


def test_deploy_needs_git_tracked_lock_and_refuses_links(world):
    w = world
    (w.project / "secret-link").symlink_to("/etc/hosts")
    _git(w.project, "add", "secret-link")
    _git(w.project, "commit", "-q", "-m", "link")
    with pytest.raises(Refused) as e:
        _deploy(w, dry_run=True)
    assert e.value.code == "link_in_project"
    _git(w.project, "rm", "-q", "--cached", "secret-link")
    _git(w.project, "rm", "-q", "--cached", "uv.lock")
    _git(w.project, "commit", "-q", "-m", "no lock")
    with pytest.raises(MesError) as e:
        _deploy(w, dry_run=True)
    assert e.value.code == "missing_file"


def test_a_missing_signing_key_asks_the_person(world, tmp_path):
    with pytest.raises(MesError) as e:
        _deploy(world, key=str(tmp_path / "nope"))
    assert e.value.exit_code == 6 and "ssh-keygen -t ed25519" in (e.value.message + str(e.value.hint))


def test_sibling_path_dependency_travels_with_the_release(world):
    w = world
    dep = w.project.parent / "helper"
    dep.mkdir()
    (dep / "pyproject.toml").write_text('[project]\nname = "helper"\nversion = "1"\n')
    _git(dep, "init", "-q")
    _git(dep, "add", "-A")
    _git(dep, "commit", "-q", "-m", "one")
    with open(w.project / "pyproject.toml", "a") as f:
        f.write('\n[tool.uv.sources]\nhelper = { path = "../helper" }\n')
    _git(w.project, "commit", "-q", "-am", "dep")
    result = _deploy(w)
    assert result["deployed"]
    assert (w.base / "code" / "releases" / "1" / "helper" / "pyproject.toml").is_file()
    with open(w.project / "pyproject.toml", "a") as f:
        f.write('other = { path = "/opt/other" }\n')
    _git(w.project, "commit", "-q", "-am", "abs")
    with pytest.raises(Refused) as e:
        _deploy(w, dry_run=True)
    assert e.value.code == "dependency_path"


def test_wrong_destination_names_are_refused():
    for host in ("-oProxyCommand=x", "a b", "user@-x", "x;y", ""):
        with pytest.raises(MesError):
            deploy.destination(host, "demo")
    assert deploy.destination("my-server", "demo") == "mes-demo@my-server"
    assert deploy.ssh_argv("mes-demo@my-server", ["mes", "releases"])[-2] == "mes-demo@my-server"


def test_releases_when_nothing_is_set_up(tmp_path, monkeypatch, world):
    shutil.rmtree(world.base / "code")
    with pytest.raises(NotFound):
        _deploy(world, releases=True)


@pytest.mark.parametrize("line", [
    'demo cert-authority,namespaces="mes-deploy,mes-rollback" {kind} {b64}',
    'demo,other namespaces="mes-deploy,mes-rollback" {kind} {b64}',
    '* namespaces="mes-deploy,mes-rollback" {kind} {b64}',
    'demo namespaces="mes-deploy,git" {kind} {b64}',
    'demo namespaces="mes-deploy" {kind} {b64}',
    'demo {kind} {b64}',
    'demo namespaces="mes-deploy,mes-rollback" {kind} {b64}\n' * 100 + '* cert-authority {kind} {b64}',
])
def test_signer_lines_with_extra_powers_are_not_trusted(world, line):
    w = world
    kind, b64 = (w.keys / "deploy.pub").read_text().split()[:2]
    (w.base / "etc" / "allowed_signers").write_text(line.format(kind=kind, b64=b64) + "\n")
    with pytest.raises(Refused) as e:
        _deploy(w)
    assert e.value.code == "untrusted_file"
    assert not (w.base / "code" / "releases").exists() or _releases(w) == []


def test_the_release_lock_is_written_only_from_a_checked_describe(tmp_path):
    """The read sandbox finds the release's apps (running project code); the gate itself writes
    apps.lock.json only after checking every path stays inside that release."""
    import projects as helpers

    folder = helpers.write(tmp_path / "rel" / "project", PROJECT_TOML.format(version="0.1.0"),
                           {"demo_app": APP_TOML}).resolve()
    good = {"apps": [{"package": "demo_app", "name": "demo-app", "path": str(folder / "apps" / "demo_app"),
                      "in_project": True, "program": "demo-app"}]}
    elsewhere = tmp_path / "elsewhere" / "demo_app"
    elsewhere.mkdir(parents=True)
    (elsewhere / "app.toml").write_text(APP_TOML)
    for bad in (
        {"apps": []},
        {"apps": [{**good["apps"][0], "path": str(elsewhere.resolve()), "in_project": False}]},
        {"apps": [{**good["apps"][0], "path": str(folder / "apps" / ".." / "apps" / "demo_app")}]},
        {"apps": [{**good["apps"][0], "name": "other-app"}]},
        {"apps": [{**good["apps"][0], "program": "sh"}]},
    ):
        with pytest.raises(deploy.MesError):
            deploy.write_release_lock(folder, bad)
        assert not (folder / "apps.lock.json").exists()
    project = deploy.write_release_lock(folder, good)
    assert list(project.apps) == ["demo-app"]
    assert json.loads((folder / "apps.lock.json").read_text())["apps"][0]["path"] == str(folder / "apps" / "demo_app")


def test_an_installed_app_inside_the_project_gets_its_own_wheel():
    """The server installs only the archive's wheels (no building there), so an app installed from
    a folder of the project (vendor/<app>) needs a wheel too; its package is never built there."""
    fitlab = Path(__file__).resolve().parents[1] / "examples" / "fitlab"
    assert deploy._inside_path_dependencies(fitlab) == ["vendor/fitlab-rides"]
    assert "fitlab-rides" in deploy._path_packages(fitlab)


# --------------------------------------------------------------------------- replay record, fingerprints, first data

def test_replay_is_refused_even_after_the_releases_are_gone(world):
    """Finding A4: replay protection rested on the release folders and their records; once those
    were pruned (or removed by hand), an old signed archive installed again. The gate now keeps
    the newest accepted time and the accepted archives in gate/accepted.json."""
    w = world
    old, old_sig = _signed(w, name="old.tgz")
    w.write_project("0.2.0")
    new, new_sig = _signed(w, name="new.tgz")
    assert deploy._send("mes-demo@my-server", ["mes", "deploy", "--signature", new_sig], new, 60)["release"] == 1
    shutil.rmtree(w.base / "code" / "releases")
    os.unlink(w.base / "code" / "current")
    shutil.rmtree(w.base / "gate" / "releases", ignore_errors=True)
    for archive, sig, code in ((new, new_sig, "already_installed"), (old, old_sig, "older_release")):
        with pytest.raises(Refused) as e:
            deploy._send("mes-demo@my-server", ["mes", "deploy", "--signature", sig], archive, 60)
        assert e.value.code == code
    accepted = json.loads((w.base / "gate" / "accepted.json").read_text())
    assert len(accepted["archives"]) == 1 and accepted["newest_created_ns"] > 0


def test_rollback_refuses_a_release_whose_files_changed(world):
    """Finding A4: a rollback switched to whatever was in releases/<n>; now the gate checks the
    fingerprint of its files recorded when it was installed."""
    w = world
    _deploy(w)
    w.write_project("0.2.0")
    _deploy(w)
    record = json.loads((w.base / "gate" / "releases" / "1.json").read_text())
    assert len(record["tree_sha256"]) == 64
    cli = w.base / "code" / "releases" / "1" / "project" / "apps" / "demo_app" / "cli.py"
    cli.write_text(cli.read_text() + "\n# changed after installing\n")
    with pytest.raises(Refused) as e:
        _deploy(w, rollback=True, to=1)
    assert e.value.code == "release_changed"
    assert os.readlink(w.base / "code" / "current") == "releases/2"


def test_a_first_deploy_before_any_data_is_waiting_not_failed(world):
    """Finding E: easy-ui cannot publish before the first data; the deploy says so and succeeds."""
    w = world
    fake = _script(w.tmp / "tools" / "easy-ui", f"#!{sys.executable}\n"
                   "import json, sys\n"
                   "if sys.argv[-1] == 'publish':\n"
                   "    print(json.dumps({'error': {'code': 'no_data', 'message': 'There is no data yet.'}}))\n"
                   "    sys.exit(3)\n"
                   "print(json.dumps({}))\n")
    with open(w.base / "etc" / "server.toml", "a") as f:
        f.write(f'easy_ui = "{fake}"\n')
    (w.project / "ui" / "dashboards").mkdir(parents=True)
    (w.project / "ui" / "dashboards" / "main.yaml").write_text("title: Main\n")
    _git(w.project, "add", "-A")
    _git(w.project, "commit", "-q", "-m", "dashboards")
    out = _deploy(w)
    assert out["deployed"] and out["release"] == 1
    assert out["dashboards"]["waiting"] is True
    assert "waiting for first data" in out["message"]
    assert os.readlink(w.base / "code" / "current") == "releases/1"


def test_the_install_step_never_names_the_new_environment(tmp_path):
    """Finding A1: 'uv pip install --python <release>/.venv/bin/python' started the release's
    Python. The one uv command now is a fresh 'uv sync' that names no interpreter."""
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    argv = deploy.install_argv("/usr/bin/uv", folder, wheels=True, allow_builds=False)
    assert argv[:2] == ["/usr/bin/uv", "sync"] and "--no-dev" in argv and "--no-build" in argv
    assert "--no-install-project" in argv and "pip" not in argv
    assert not any(a == "--python" or ".venv" in a for a in argv)
    assert "--no-build" not in deploy.install_argv("/usr/bin/uv", folder, wheels=True, allow_builds=True)


def test_the_install_step_refuses_a_release_it_is_not_pointed_at_or_with_an_environment(world):
    w = world
    _deploy(w)
    layout = paths.server_layout("demo", w.root)
    where = types.SimpleNamespace(layout=layout)
    with pytest.raises(Refused) as e:  # code/installing does not point at release 1
        deploy.child_step(where, {"kind": "sync", "release": 1}, timeout=60)
    assert e.value.code == "not_installing"
    os.symlink("releases/1", layout.installing)
    try:
        with pytest.raises(Refused) as e:  # release 1 has its .venv: uv would start its Python
            deploy.child_step(where, {"kind": "sync", "release": 1}, timeout=60)
        assert e.value.code == "stray_environment"
    finally:
        os.unlink(layout.installing)


def test_a_missing_accepted_record_with_releases_is_damage_not_a_fresh_start(world, capsys):
    """Deleting gate/accepted.json made the server forget every accepted archive, so an old
    signed archive installed again once the release folders were gone. A missing record while
    release records exist now stops deploys, and says how the owner re-creates it (a root command
    that reads the releases' records). A project with no releases at all still starts empty."""
    w = world
    assert deploy.read_accepted(paths.server_layout("demo", w.root)) == {"newest_created_ns": None, "archives": []}
    old, old_sig = _signed(w, name="old.tgz")
    w.write_project("0.2.0")
    new, new_sig = _signed(w, name="new.tgz")
    assert deploy._send("mes-demo@my-server", ["mes", "deploy", "--signature", new_sig], new, 60)["release"] == 1
    accepted = w.base / "gate" / "accepted.json"
    before = json.loads(accepted.read_text())
    accepted.unlink()
    shutil.rmtree(w.base / "code" / "releases" / "1")  # the release folder is gone too: only its record is left
    with pytest.raises(Refused) as e:
        deploy._send("mes-demo@my-server", ["mes", "deploy", "--signature", old_sig], old, 60)
    assert e.value.code == "missing_record"
    assert f"sudo /usr/local/bin/mes server rebuild-accepted demo --server-root {w.root}" in e.value.hint
    assert not accepted.exists()
    # The owner's repair: it must run as root (refused otherwise), and rebuilds from the records.
    from mes import cli

    if os.geteuid() != 0:
        code = cli.run(["--json", "server", "rebuild-accepted", "demo", "--server-root", str(w.root)])
        assert code == 5 and json.loads(capsys.readouterr().out)["error"]["code"] == "needs_root"
    out = deploy.rebuild_accepted(paths.server_layout("demo", w.root))
    assert out["releases"] == 1 and json.loads(accepted.read_text()) == before
    with pytest.raises(Refused) as e:
        deploy._send("mes-demo@my-server", ["mes", "deploy", "--signature", old_sig], old, 60)
    assert e.value.code == "older_release"
    with pytest.raises(Refused) as e:
        deploy.rebuild_accepted(paths.server_layout("demo", w.root))
    assert e.value.code == "record_fine"
