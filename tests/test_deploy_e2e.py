"""End-to-end deploy on this computer: "the server" is a temporary folder driven through the gate.

- A throwaway Ed25519 signing key is made with ssh-keygen for each test session.
- ``ssh`` is a fake on PATH: it does what sshd does with a forced command, setting
  SSH_ORIGINAL_COMMAND to the request and running ``mes gate --project demo --allow <level>
  --server-root <tmp>`` with the archive on stdin.
- ``uv`` is a fake that "installs" the project by writing ``.venv/bin/demo``, a small program
  that runs the project's own cli.py.
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
from pathlib import Path

import pytest

import mes
from mes import deploy
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
import os, sys
assert sys.argv[1:] == ["sync", "--frozen", "--no-progress"], sys.argv
if os.path.exists(os.path.join(os.getcwd(), "FAIL_SYNC")):
    sys.stderr.write("could not resolve packages\\n")
    sys.exit(1)
here = os.getcwd()
os.makedirs(os.path.join(here, ".venv", "bin"), exist_ok=True)
for name, entry in (("demo-app", "from demo_app.cli import main"), ("demo", "from mes.project import main")):
    prog = os.path.join(here, ".venv", "bin", name)
    with open(prog, "w") as f:
        f.write("#!{python}\\nimport sys\\nsys.path[:0] = [%r, %r]\\n%s\\nmain()\\n"
                % (os.path.join(here, "apps"), {mes_src!r}, entry))
    os.chmod(prog, 0o700)
'''

FAKE_SSH = '''#!{python}
import os, subprocess, sys
args = sys.argv[1:]
assert "BatchMode=yes" in args and "ForwardAgent=no" in args and "--" in args, args
dest, command = args[args.index("--") + 1:]
assert dest == "mes-demo@my-server", dest
env = dict(os.environ, SSH_ORIGINAL_COMMAND=command)
level = os.environ.get("FAKE_SSH_LEVEL", "deploy")
boot = "import sys, mes.units; mes.units.UNSAFE_TOOL_ROOTS = (); from mes.cli import main; sys.argv[0] = 'mes'; main()"
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
    uv = _script(tools / "uv", FAKE_UV.format(python=sys.executable, mes_src=MES_SRC))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _script(bindir / "ssh", FAKE_SSH.format(python=sys.executable, root=str(w.root)))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    # The server's layout, as the root script of 'mes server plan' leaves it.
    for sub, mode in (("", 0o711), ("code", 0o700), ("data", 0o700), ("state", 0o700), ("store", 0o700),
                      ("ui", 0o750), ("etc", 0o755), ("log", 0o750)):
        (w.base / sub).mkdir(mode=mode, parents=True, exist_ok=True)
        (w.base / sub).chmod(mode)
    pub = (signing_key / "deploy.pub").read_text().split()
    (w.base / "etc" / "allowed_signers").write_text(f'demo namespaces="mes-deploy" {pub[0]} {pub[1]}\n')
    (w.base / "etc" / "server.toml").write_text(
        f'[server]\nproject = "demo"\nroot = "{w.root}"\nmes = "/usr/local/bin/mes"\nuv = "{uv}"\n')
    for f in ("allowed_signers", "server.toml"):
        (w.base / "etc" / f).chmod(0o644)
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
    log = [json.loads(ln) for ln in (w.base / "code" / deploy.DEPLOY_LOG).read_text().splitlines()]
    assert [e["event"] for e in log] == ["deployed", "deployed", "rolled_back", "rolled_back"]
    gate_log = (w.base / "state" / "gate" / "gate-log.jsonl").read_text()
    assert '"deploy"' in gate_log


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
    (w.base / "etc" / "allowed_signers").chmod(0o644)
    rsa = w.tmp / "rsa"
    subprocess.run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "", "-f", str(rsa)], check=True)
    kind, b64 = rsa.with_name("rsa.pub").read_text().split()[:2]
    (w.base / "etc" / "allowed_signers").write_text(f'demo namespaces="mes-deploy" {kind} {b64}\n')
    with pytest.raises(Refused) as e:
        _deploy(w)
    assert e.value.code == "key_type"


def test_deploy_needs_git_tracked_lock_and_refuses_links(world):
    w = world
    (w.project / "secret-link").symlink_to("/etc/hosts")
    _git(w.project, "add", "secret-link")
    with pytest.raises(Refused) as e:
        _deploy(w, dry_run=True)
    assert e.value.code == "link_in_project"
    _git(w.project, "rm", "-q", "--cached", "secret-link")
    _git(w.project, "rm", "-q", "--cached", "uv.lock")
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
    'demo cert-authority,namespaces="mes-deploy" {kind} {b64}',
    'demo,other namespaces="mes-deploy" {kind} {b64}',
    '* namespaces="mes-deploy" {kind} {b64}',
    'demo namespaces="mes-deploy,git" {kind} {b64}',
    'demo {kind} {b64}',
])
def test_signer_lines_with_extra_powers_are_not_trusted(world, line):
    w = world
    kind, b64 = (w.keys / "deploy.pub").read_text().split()[:2]
    (w.base / "etc" / "allowed_signers").write_text(line.format(kind=kind, b64=b64) + "\n")
    with pytest.raises(Refused) as e:
        _deploy(w)
    assert e.value.code == "untrusted_file"
    assert not (w.base / "code" / "releases").exists() or _releases(w) == []
