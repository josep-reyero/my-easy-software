"""Tests for mes.server: the root script of ``mes server plan``, ``check`` and ``remove``.

The root script is checked three ways, none of which needs Linux or root:
- golden checks on its text (what it creates, with which owners and modes, and nothing else);
- ``bash -n`` (and ``sh -n`` for the gate's login shell);
- a run under a fake-command harness: ``id``, ``useradd``, ``install``, ``chown``, ``systemctl`` ...
  are bash functions that record what they were asked to do, so the script really runs, twice
  (it must be idempotent), against a temporary server root and units folder.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from mes.buildinfo import build_id
from mes import cli, server, units

import projects as helpers
from mes.errors import MesError, Refused, UsageError

PROJECT_TOML = """
[project]
name = "demo"
title = "Demo"
version = "0.1.0"

[apps.demo]
cli = "demo"

[jobs.sync]
run = "demo sync --days 3"
schedule = "*:0/10"
network = ["api.example.com"]

[jobs.build]
run = "demo build"
after = "sync"

[jobs.hook]
run = "demo sync"
webhook = true

[ui]
data = "demo.sqlite"
tables = ["daily"]
refresh_after = ["build"]
"""

TOOLS = {"mes": "/bin/sh", "uv": "/bin/sh", "easy_ui": "/bin/sh"}


def _keygen(path: Path, comment: str = "someone@example.invalid") -> Path:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)], check=True)
    return path.with_name(path.name + ".pub")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    d = tmp_path / "demo"
    d.mkdir()
    helpers.write_old(d, PROJECT_TOML)
    monkeypatch.setenv("MES_EASY_UI", str(tmp_path / "no-easy-ui"))  # not installed unless a test adds it
    return d


@pytest.fixture
def keys(tmp_path):
    k = tmp_path / "keys"
    k.mkdir()
    return {"read": _keygen(k / "read"), "deploy": _keygen(k / "deploy"), "signer": _keygen(k / "signer")}


def _plan(proj, keys, tmp_path, **kw):
    return server.build_plan(proj, "demo", server_root=str(tmp_path / "srv"), tools=dict(TOOLS),
                             key_files=[f"read={keys['read']}", f"deploy={keys['deploy']}"],
                             signer_files=[str(keys["signer"])], units_dir=str(tmp_path / "units"), **kw)


def test_script_text_golden(proj, keys, tmp_path):
    plan = _plan(proj, keys, tmp_path)
    script = server.render_script(plan)
    srv = tmp_path / "srv"
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "\nset -euo pipefail\n" in script and "\numask 077\n" in script
    assert "What it does, in plain words:" in script
    base = f"{srv}/demo"
    q = shlex.quote
    for line in [
        f"ensure_top {q(str(srv))}",
        f"ensure_dir {q(base)} root root 0711",
        f"ensure_dir {q(base + '/etc')} root mes-demo 0750",
        f"ensure_dir {q(base + '/home')} root mes-demo 0750",
        f"ensure_dir {q(base + '/home/.ssh')} root mes-demo 0750",
        f"ensure_dir {q(base + '/code')} mes-demo mes-demo 0700",
        f"ensure_dir {q(base + '/data')} mes-demo mes-demo 0700",
        f"ensure_dir {q(base + '/state')} mes-demo mes-demo 0700",
        f"ensure_dir {q(base + '/gate')} mes-demo mes-demo 0700",
        f"ensure_dir {q(base + '/webhook')} mes-demo mes-demo 0700",
        f"ensure_dir {q(base + '/ui')} mes-demo mes-demo-web 2750",
        f"ensure_dir {q(base + '/auth')} mes-demo-web mes-demo-web 0700",
        f"ensure_sub {q(base)} ui/data mes-demo mes-demo-web 2750",
        f"ensure_sub {q(base)} webhook/triggers mes-demo mes-demo 0700",
        f"ensure_dir {q(base + '/log')} root mes-demo 0750",
        "usermod -p '*' -- mes-demo",
        "usermod -p '!' -- mes-demo-web",
        f"ensure_account mes-demo {q(base + '/home')} {q(base + '/etc/gate-shell')} 'mes project demo'",
        f"check_mes /bin/sh {q(units.DEFAULT_MES_PYTHON)} {q(server.__version__)} {q(build_id())}",
        "check_tool uv /bin/sh",
    ]:
        assert line in script, line
    assert "ui/auth" not in script  # finding 7: the sign-in folder is beside ui/, not inside it
    code_lines = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
    assert not any(re.match(r"\s*sudo\b", ln) for ln in code_lines)
    assert 'command="/bin/sh gate --project demo --allow read --server-root ' in script
    assert 'command="/bin/sh gate --project demo --allow deploy --server-root ' in script
    assert "someone@example.invalid" not in script  # key comments are never copied
    assert 'demo namespaces="mes-deploy,mes-rollback" ssh-ed25519 ' in script
    for name in plan.units:
        assert f"put_file {q(f'{tmp_path}/units/{name}')} 0644 root:root <<'MES_END_" in script
    for rel in ("etc/server.toml", "etc/allowed_signers", "etc/webhook-jobs", "home/.ssh/authorized_keys"):
        assert f"put_file {q(f'{base}/{rel}')} 0640 root:mes-demo <<'MES_END_" in script, rel
    assert f"put_file {q(f'{base}/etc/gate-shell')} 0750 root:mes-demo <<'MES_END_" in script
    # The gate's login shell is written before the account that uses it.
    assert script.index(f"put_file {q(f'{base}/etc/gate-shell')}") < script.index("ensure_account mes-demo ")
    assert "mes-demo.job.demo-app.sync.timer" in script and "systemctl enable --now -- " in script
    assert "mes-demo.selftest.path" in script.split("systemctl enable --now -- ")[1]
    assert "webhook.key" in script and "od -An -tx1 -N32 /dev/urandom" in script


def test_script_header_names_the_version_and_a_fingerprint(proj, keys, tmp_path):
    """Finding 1: nothing in the script showed which mes made it or whether it was changed."""
    script = server.render_script(_plan(proj, keys, tmp_path))
    head = script.split(server.PLAN_MARKER)[0]
    assert f"(mes {server.__version__}, build {build_id()})" in head  # item I: the version alone is not enough
    m = re.search(r"^# Plan fingerprint \(SHA-256\): ([0-9a-f]{64})$", head, re.M)
    assert m and m.group(1) == server.plan_fingerprint(script)


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs sha256sum")
def test_a_changed_script_stops_before_doing_anything(proj, keys, tmp_path):
    script = server.render_script(_plan(proj, keys, tmp_path))
    evil = script.replace("ensure_group mes-demo\n", "ensure_group mes-demo\necho pwned > /dev/null\n", 1)
    res = _run_script(evil, tmp_path / "fake", harness=False)
    assert res.returncode == 1 and "fingerprint does not match" in res.stderr
    assert not (tmp_path / "fake" / "log").exists()


def test_script_only_touches_its_own_places(proj, keys, tmp_path):
    script = server.render_script(_plan(proj, keys, tmp_path)).replace(server._PRELUDE, "")
    srv, udir = f"{tmp_path}/srv", f"{tmp_path}/units"
    allowed_prefixes = (srv, f"{udir}/mes-demo.", "/dev/null", "/dev/urandom", "/nonexistent", "/usr/sbin/nologin",
                        "/bin/sh", "/usr/sbin:/usr/bin:/sbin:/bin", "/usr/bin/env bash", units.DEFAULT_MES_PYTHON)
    code = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
    in_heredoc = None
    for ln in code:
        if in_heredoc:
            if ln == in_heredoc:
                in_heredoc = None
            continue
        m = re.search(r"<<'(MES_END_[0-9a-f]+)'", ln)
        if m:
            in_heredoc = m.group(1)
        for path in re.findall(r"(?<![\w$])(/[A-Za-z0-9._/:@+=,~-]+)", ln):
            if path in ("/", "/*", "/bin/false", "//p"):
                continue
            assert path.startswith(allowed_prefixes) or path == udir, (path, ln)
    assert re.search(r"rm -f -- \"\$f\"", script)  # only units with the project's marker
    assert "chown -R" not in script and "rm -rf" not in script


def test_bash_syntax_of_script_and_gate_shell(proj, keys, tmp_path):
    plan = _plan(proj, keys, tmp_path)
    f = tmp_path / "plan.sh"
    f.write_text(server.render_script(plan))
    subprocess.run(["bash", "-n", str(f)], check=True)
    g = tmp_path / "gate-shell"
    g.write_text(server._gate_shell(plan))
    subprocess.run(["sh", "-n", str(g)], check=True)
    r = tmp_path / "remove.sh"
    r.write_text(server.render_remove("demo", server_root=str(tmp_path / "srv"), units_dir=str(tmp_path / "units")))
    subprocess.run(["bash", "-n", str(r)], check=True)
    r.write_text(server.render_remove("demo", delete_data=True))
    subprocess.run(["bash", "-n", str(r)], check=True)


def test_gate_shell_runs_only_the_gate(proj, keys, tmp_path):
    # With mes = /bin/echo the shell's exec is observable; anything else is refused.
    plan = server.build_plan(proj, "demo", server_root="/var/lib/mes", tools={"mes": "/bin/echo", "uv": "/bin/sh"},
                             key_files=[f"read={keys['read']}"])
    g = tmp_path / "gate-shell"
    g.write_text(server._gate_shell(plan))
    ok = subprocess.run(["sh", str(g), "-c", server.gate_command(plan, "read")], capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.strip() == "gate --project demo --allow read --server-root /var/lib/mes"
    for argv in (["-c", "bash"], ["-c", server.gate_command(plan, "read") + " ; id"],
                 ["-c", server.gate_command(plan, "read").replace("read", "admin")], [], ["-i"],
                 ["-c", server.gate_command(plan, "read"), "extra"]):
        bad = subprocess.run(["sh", str(g), *argv], capture_output=True, text=True)
        assert bad.returncode == 1 and "only runs the mes gate" in bad.stderr and not bad.stdout


# --------------------------------------------------------------------------- the script, really run

FAKES = r'''
LOG="$FAKE/log"
id() { if [ "${1:-}" = -u ]; then echo 0; else command id "$@"; fi; }
uname() { echo Linux; }
systemctl() {
  if [ "${1:-}" = --version ]; then echo "systemd ${FAKE_SYSTEMD:-256} (${FAKE_SYSTEMD:-256}.1-1-arch)"; return 0; fi
  echo "systemctl $*" >> "$LOG"
}
chown() {
  echo "chown $*" >> "$LOG"
  local a=("$@"); [ "${a[0]}" = -- ] && a=("${a[@]:1}")
  printf '%s\t%s\n' "${a[1]}" "${a[0]}" >> "$FAKE/owners"
}
stat() {
  case "$2" in
    %u) echo 0 ;;
    %a) echo 755 ;;
    %U:%G:%a)
      local who mode
      who="$(awk -F'\t' -v p="$4" '$1 == p { o = $2 } END { print o }' "$FAKE/owners")"
      mode="$(python3 -c 'import os, sys; print(format(os.stat(sys.argv[1]).st_mode & 0o7777, "o"))' "$4")"
      echo "$who:$mode" ;;
    *) command stat "$@" ;;
  esac
}
mv() {
  local a=() x
  for x in "$@"; do case "$x" in -f|-T|--) ;; *) a+=("$x") ;; esac; done
  command mv -f "${a[0]}" "${a[1]}"
  [ -f "$FAKE/owners" ] && awk -F'\t' -v s="${a[0]}" -v d="${a[1]}" '$1 == s { print d "\t" $2 }' "$FAKE/owners" >> "$FAKE/owners"
  return 0
}
getent() {
  local db="$1" name="$2"
  [ -f "$FAKE/$db" ] || return 2
  grep -E "^$name:" "$FAKE/$db" || return 2
}
groupadd() { echo "groupadd $*" >> "$LOG"; echo "${@: -1}:x:1000:" >> "$FAKE/group"; }
useradd() {
  echo "useradd $*" >> "$LOG"
  local comment="" home="" shell="" a
  while [ "$#" -gt 1 ]; do
    case "$1" in --comment) comment="$2"; shift ;; --home-dir) home="$2"; shift ;; --shell) shell="$2"; shift ;; esac
    shift
  done
  echo "$1:x:1000:1000:$comment:$home:$shell" >> "$FAKE/passwd"
}
usermod() { echo "usermod $*" >> "$LOG"; }
install() {
  echo "install $*" >> "$LOG"
  if [ "$1" = -d ]; then
    local mode path
    mode="$7"; path="$9"
    mkdir -p -- "$path"; chmod "$mode" "$path"
  else
    local mode="$2" target="${@: -1}"
    : > "$target"; chmod "$mode" "$target"
  fi
}
'''


def _harness(script: str) -> str:
    """The tool checks need root-owned system tools; they have their own tests below. Here the
    calls are switched off, and the fingerprint is made again so the script still accepts itself."""
    body = script.split(server.PLAN_MARKER + "\n", 1)[1]
    new_body = re.sub(r"^(check_mes|check_tool) ", r": \1 ", body, flags=re.M)
    head = script.split(server.PLAN_MARKER + "\n", 1)[0]
    head = re.sub(r"(# Plan fingerprint \(SHA-256\): )[0-9a-f]{64}",
                  lambda m: m.group(1) + server.hashlib.sha256(new_body.encode()).hexdigest(), head)
    return head + server.PLAN_MARKER + "\n" + new_body


def _run_script(script: str, fake: Path, harness: bool = True) -> subprocess.CompletedProcess[str]:
    fake.mkdir(exist_ok=True)
    (fake / "fakes.sh").write_text(FAKES)
    (fake / "plan.sh").write_text(_harness(script) if harness else script)
    return subprocess.run(["bash", "-c", f'FAKE="{fake}"; source "{fake}/fakes.sh"; source "{fake}/plan.sh"'],
                          capture_output=True, text=True, env={"PATH": os.environ["PATH"], "HOME": str(fake)})


def test_script_really_runs_and_is_idempotent(proj, keys, tmp_path):
    plan = _plan(proj, keys, tmp_path)
    script = server.render_script(plan)
    (tmp_path / "units").mkdir()
    stale = tmp_path / "units" / "mes-demo.job.old.service"
    stale.write_text(units.HEADER + "\n# mes-project: demo\n")
    foreign = tmp_path / "units" / "mes-demo.job.keep.service"
    foreign.write_text("# not made by mes\n")
    # The record unit of a job that is gone goes with it; the plan's own record units stay.
    stale_record = tmp_path / "units" / "mes-demo.record.old-app.x.service"
    stale_record.write_text(units.HEADER + "\n# mes-project: demo\n")
    records = [n for n in plan.units if n.startswith("mes-demo.record.")]
    assert "mes-demo.record.selftest.service" in records and len(records) >= 2
    assert "mes-demo.selftest-after.service" in plan.units
    first = _run_script(script, tmp_path / "fake")
    assert first.returncode == 0, first.stderr
    assert "Done. The project demo is set up." in first.stdout
    base = tmp_path / "srv" / "demo"
    for sub, mode in (("code", 0o700), ("data", 0o700), ("state", 0o700), ("store", 0o700), ("auth", 0o700),
                      ("gate", 0o700), ("webhook", 0o700), ("webhook/triggers", 0o700), ("backups", 0o700),
                      ("ui", 0o2750), ("ui/data", 0o2750), ("ui/specs", 0o2750),
                      ("log", 0o750), ("etc", 0o750), ("home", 0o750), ("home/.ssh", 0o750)):
        assert stat.S_IMODE((base / sub).stat().st_mode) == mode, sub
    assert stat.S_IMODE(base.stat().st_mode) == 0o711
    assert not (base / "ui" / "auth").exists() and not (base / "state" / "triggers").exists()
    owners = {}
    for line in (tmp_path / "fake" / "owners").read_text().splitlines():
        path, owner = line.split("\t")
        owners[path] = owner
    assert owners[str(base / "auth")] == "mes-demo-web:mes-demo-web"
    assert owners[str(base / "etc")] == "root:mes-demo" and owners[str(base / "home/.ssh")] == "root:mes-demo"
    assert owners[str(base / "etc/allowed_signers")] == "root:mes-demo"
    ak = (base / "home/.ssh/authorized_keys").read_text()
    assert ak.count("restrict,command=") == 2 and stat.S_IMODE((base / "home/.ssh/authorized_keys").stat().st_mode) == 0o640
    assert (base / "etc/allowed_signers").read_text().count('namespaces="mes-deploy,mes-rollback"') == 1
    assert (base / "etc/webhook-jobs").read_text().splitlines()[-1] == "demo-app.hook"  # job ids
    assert stat.S_IMODE((base / "etc/gate-shell").stat().st_mode) == 0o750
    for rel in ("etc/server.toml", "etc/allowed_signers", "etc/webhook-jobs"):
        assert stat.S_IMODE((base / rel).stat().st_mode) == 0o640, rel
    key = (base / "etc/webhook.key").read_text()
    assert re.fullmatch(r"[0-9a-f]{64}", key.strip()), key
    assert stat.S_IMODE((base / "etc/webhook.key").stat().st_mode) == 0o640
    for name, text in plan.units.items():
        assert (tmp_path / "units" / name).read_text() == text.rstrip("\n") + "\n"
    assert not stale.exists() and foreign.exists() and not stale_record.exists()
    log = (tmp_path / "fake" / "log").read_text()
    assert log.count("useradd") == 2 and "systemctl daemon-reload" in log
    assert "systemctl disable --now -- mes-demo.job.old.service" in log
    assert not any(f"disable --now -- {n}" in log for n in records)
    assert not any(n in line for n in records for line in log.splitlines() if "enable --now" in line)
    # Second run: same files, the accounts are not created again, the secret is kept.
    second = _run_script(script, tmp_path / "fake")
    assert second.returncode == 0, second.stderr
    log2 = (tmp_path / "fake" / "log").read_text()[len(log):]
    assert "useradd" not in log2 and "groupadd" not in log2
    assert (base / "etc/webhook.key").read_text() == key
    for name, text in plan.units.items():
        assert (tmp_path / "units" / name).read_text() == text.rstrip("\n") + "\n"
    assert not list(base.rglob("*.mes-new.*")) and not list((tmp_path / "units").glob("*.mes-new.*"))


def test_script_stops_for_a_foreign_account_and_a_planted_link(proj, keys, tmp_path):
    script = server.render_script(_plan(proj, keys, tmp_path))
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "passwd").write_text("mes-demo:x:1000:1000:Someone Else:/home/x:/bin/bash\n")
    (fake / "group").write_text("mes-demo:x:1000:\n")
    res = _run_script(script, fake)
    assert res.returncode == 1 and "already exists and was not made by mes" in res.stderr
    (fake / "passwd").write_text("")
    (tmp_path / "srv").mkdir(exist_ok=True)
    shutil.rmtree(tmp_path / "srv" / "demo", ignore_errors=True)
    (tmp_path / "srv" / "demo").symlink_to(tmp_path)
    res = _run_script(script, fake)
    assert res.returncode == 1 and "is a link" in res.stderr
    # A link planted inside ui/ (which the project's account owns) is never followed.
    (tmp_path / "srv" / "demo").unlink()
    (tmp_path / "srv" / "demo" / "ui").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "srv" / "demo" / "ui" / "data").symlink_to(tmp_path / "elsewhere")
    res = _run_script(script, fake)
    assert res.returncode == 1 and "is not a real folder" in res.stderr
    assert not list((tmp_path / "elsewhere").iterdir())
    # Nor a link in place of the sign-in folder.
    (tmp_path / "srv" / "demo" / "ui" / "data").unlink()
    (tmp_path / "srv" / "demo" / "auth").symlink_to(tmp_path / "elsewhere")
    res = _run_script(script, fake)
    assert res.returncode == 1 and "is a link" in res.stderr
    assert not list((tmp_path / "elsewhere").iterdir())


def test_script_refuses_to_run_without_root(proj, keys, tmp_path):
    f = tmp_path / "plan.sh"
    f.write_text(server.render_script(_plan(proj, keys, tmp_path)))
    res = subprocess.run(["bash", str(f)], capture_output=True, text=True)
    assert res.returncode == 1 and "must be run by the administrator" in res.stderr


# --------------------------------------------------------------------------- hostile input


@pytest.mark.parametrize("path", ["/usr/local/bin/mes;rm -rf /", "/usr/local/bin/m es", "/usr/local/bin/$(id)",
                                  "/usr/local/bin/'mes'", "relative/mes", "/home/me/.local/bin/mes", "/tmp/mes",
                                  "/usr/local/../tmp/mes", "/usr/local/bin/mes\n"])
def test_hostile_tool_paths_are_refused(proj, keys, tmp_path, path):
    with pytest.raises(Refused):
        server.build_plan(proj, "demo", tools={"mes": path, "uv": "/bin/sh"})


def test_long_project_names_are_refused_for_servers():
    with pytest.raises(UsageError) as e:
        server.check_server_name("a" * 25)
    assert "32-character limit" in e.value.message
    assert server.check_server_name("a" * 24) == "a" * 24
    for bad in ("../x", "a b", "A", "x\n", "-x", "mes-$(id)", "demo-web"):
        with pytest.raises(UsageError):
            server.check_server_name(bad)


def test_project_name_must_match(proj):
    with pytest.raises(UsageError):
        server.build_plan(proj, "other", tools=dict(TOOLS))


def test_hostile_keys_are_refused(tmp_path, keys):
    good = keys["read"].read_text()
    kind, b64 = server.parse_public_key(good, what="k")
    assert kind == "ssh-ed25519"
    cases = [
        f'command="/bin/sh" {good}',
        good.replace(" ", "\n", 1),
        good + "\n" + good,
        f"ssh-rsa {b64}",
        f"ssh-ed25519 {b64[:-4]}!!!!",
        "ssh-ed25519 AAAA",
        f"ssh-ed25519 {b64}\x00",
        "",
    ]
    for text in cases:
        with pytest.raises(Refused):
            server.parse_public_key(text, what="k")
    with pytest.raises(Refused) as e:
        server.parse_public_key((tmp_path / "keys" / "read").read_text(), what="k")
    assert "private key" in e.value.message
    # A real key blob of another type under an ed25519 label is refused too.
    rsa = tmp_path / "rsa"
    subprocess.run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "", "-f", str(rsa)], check=True)
    rsa_b64 = rsa.with_name("rsa.pub").read_text().split()[1]
    with pytest.raises(Refused):
        server.parse_public_key(f"ssh-ed25519 {rsa_b64}", what="k")


def test_key_options_and_levels(proj, keys, tmp_path):
    with pytest.raises(UsageError):
        server.build_plan(proj, "demo", tools=dict(TOOLS), key_files=[f"admin={keys['read']}"])
    with pytest.raises(UsageError):
        server.build_plan(proj, "demo", tools=dict(TOOLS), key_files=[str(keys["read"])])
    plan = server.build_plan(proj, "demo", tools=dict(TOOLS))
    assert any("nobody can reach this project" in n for n in plan.notes)
    assert any("refuse every deploy" in n for n in plan.notes)


def test_heredoc_delimiter_never_appears_in_content(proj, keys, tmp_path, monkeypatch):
    plan = _plan(proj, keys, tmp_path)
    name = next(iter(plan.units))
    plan.units[name] += "# MES_END_aaaaaaaaaaaa\n"
    values = iter(["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
    monkeypatch.setattr(server.secrets, "token_hex", lambda n: next(values))
    script = server.render_script(plan)
    assert "<<'MES_END_bbbbbbbbbbbb'" in script and "<<'MES_END_aaaaaaaaaaaa'" not in script


# --------------------------------------------------------------------------- dashboard units from easy-ui


def _fake_easy_ui(tmp_path: Path, units_answer: dict) -> Path:
    exe = tmp_path / "easy-ui"
    (tmp_path / "easy-ui-answer.json").write_text(json.dumps(units_answer))
    exe.write_text(f"#!{sys.executable}\nimport json, sys\n"
                   f"(open({str(tmp_path / 'easy-ui-argv.json')!r}, 'w').write(json.dumps(sys.argv[1:])))\n"
                   f"print(open({str(tmp_path / 'easy-ui-answer.json')!r}).read())\n")
    exe.chmod(0o755)
    return exe


ORIGIN = "https://box.example.ts.net"


def _web_answer(tmp_path: Path, **change) -> dict:
    """What easy-ui units --render-only --json answers (my-easy-ui 1e1d33f)."""
    root = str(tmp_path / "srv")
    ui, auth = f"{root}/demo/ui", f"{root}/demo/auth"
    answer = {
        "app": "demo", "project": "demo", "accounts": {"project": "mes-demo", "web": "mes-demo-web"},
        "unit_names": {"socket": "mes-demo-web.socket", "service": "mes-demo-web.service"},
        "exec": "/bin/sh", "root": root, "ui_dir": ui, "auth_dir": auth, "socket": "/run/mes/demo-web.sock",
        "socket_owner": "root", "origins": [ORIGIN], "memory_max": "256M",
        "exec_start": ["/bin/sh", "serve", "--ui-dir", ui, "--auth-dir", auth, "--origin", ORIGIN],
        "folders": [], "enable": ["mes-demo-web.socket"], "written": [],
        "units": {"mes-demo-web.service": "[Service]\nUser=root\nExecStart=+/bin/sh -c id\n"},
    }
    answer.update(change)
    return answer


def test_dashboard_units_are_rendered_by_mes_from_checked_parameters(proj, keys, tmp_path, monkeypatch):
    """Finding 1: the plan copied easy-ui's unit text into a root script. Now mes renders the units
    itself and ignores easy-ui's text (the answer here carries a hostile one)."""
    monkeypatch.setenv("MES_EASY_UI", str(_fake_easy_ui(tmp_path, _web_answer(tmp_path))))
    plan = _plan(proj, keys, tmp_path, allow_hosts=["box.example.ts.net"])
    argv = json.loads((tmp_path / "easy-ui-argv.json").read_text())
    assert argv[:3] == ["--json", "--project-dir", str(proj.resolve())]
    assert argv[3:] == ["units", "--render-only", "--exec", "/bin/sh", "--root", str(tmp_path / "srv"),
                        "--origin", ORIGIN]
    assert set(plan.web_units) == {"mes-demo-web.socket", "mes-demo-web.service"}
    svc = plan.web_units["mes-demo-web.service"]
    assert "User=root" not in svc and "+/bin/sh" not in svc and "User=mes-demo-web" in svc
    lines = svc.splitlines()
    exec_start = next(ln for ln in lines if ln.startswith("ExecStart="))
    assert shlex.split(exec_start[len("ExecStart="):]) == [
        "/bin/sh", "serve", "--ui-dir", f"{tmp_path}/srv/demo/ui", "--auth-dir", f"{tmp_path}/srv/demo/auth",
        "--origin", ORIGIN]
    assert f"BindPaths={tmp_path}/srv/demo/auth" in lines and f"BindReadOnlyPaths={tmp_path}/srv/demo/ui" in lines
    assert "TemporaryFileSystem=/var:ro /run:ro /opt:ro /usr/local:ro" in lines
    env = " ".join(ln for ln in lines if ln.startswith("Environment="))
    assert "HOME=/nonexistent" in env and "MES_CONFIG=/nonexistent" in env
    sock = plan.web_units["mes-demo-web.socket"]
    assert "ListenStream=/run/mes/demo-web.sock" in sock and "SocketUser=root" in sock and "SocketMode=0600" in sock
    script = server.render_script(plan)
    assert "mes-demo-web.socket" in script.split("systemctl enable --now -- ")[1]


def test_reviewers_hostile_easy_ui_is_refused(proj, keys, tmp_path, monkeypatch):
    attack = Path("/tmp/mesreview/attack/fake-easy-ui")
    if not attack.exists():
        pytest.skip("the reviewer's file is not on this computer")
    exe = tmp_path / "fake-easy-ui"
    exe.write_text(f"#!{sys.executable}\n" + attack.read_text().split("\n", 1)[1])
    exe.chmod(0o755)
    monkeypatch.setenv("MES_EASY_UI", str(exe))
    with pytest.raises(Refused) as e:
        _plan(proj, keys, tmp_path, allow_hosts=["box.example.ts.net"])
    assert e.value.code == "unsafe_web_unit"


def test_dashboard_needs_a_web_address(proj, keys, tmp_path, monkeypatch):
    monkeypatch.setenv("MES_EASY_UI", str(_fake_easy_ui(tmp_path, _web_answer(tmp_path))))
    plan = _plan(proj, keys, tmp_path)
    assert not plan.web_units and any("--allow-host" in n for n in plan.notes)
    with pytest.raises(UsageError):
        _plan(proj, keys, tmp_path, origins=["http://box.example.ts.net"])


@pytest.mark.parametrize("change", [
    {"accounts": {"project": "mes-demo", "web": "root"}},
    {"accounts": {"project": "mes-demo", "web": "mes-demo"}},
    {"ui_dir": "/var/lib/mes/other/ui"},
    {"auth_dir": "/tmp/auth"},
    {"auth_dir": None},
    {"socket": "/run/x.sock"},
    {"socket_owner": "mes-demo-web"},
    {"exec": "/usr/bin/python3"},
    {"memory_max": "256M\nUser=root"},
    {"memory_max": "infinity"},
    {"origins": []},
    {"origins": ["http://box.example.ts.net"]},
    {"origins": [ORIGIN, "https://evil.example.com"]},
    {"origins": ["https://box.example.ts.net\nExecStartPre=+/bin/id"]},
    {"exec_start": ["/bin/sh", "-c", "id"]},
    {"unit_names": {"socket": "sshd.socket", "service": "sshd.service"}},
    {"root": "/"},
])
def test_unsafe_dashboard_parameters_are_refused(proj, keys, tmp_path, monkeypatch, change):
    monkeypatch.setenv("MES_EASY_UI", str(_fake_easy_ui(tmp_path, _web_answer(tmp_path, **change))))
    with pytest.raises(Refused) as e:
        _plan(proj, keys, tmp_path, allow_hosts=["box.example.ts.net"])
    assert e.value.code == "unsafe_web_unit"


def test_plan_refuses_a_mes_inside_the_project(proj, keys, tmp_path, monkeypatch):
    """Finding 1: the guides said 'uv run mes server plan', i.e. the root script came from the mes
    in the project's own virtual environment, which the project's code can change."""
    venv = proj / ".venv"
    (venv / "bin").mkdir(parents=True)
    monkeypatch.setattr(server.sys, "prefix", str(venv))
    with pytest.raises(Refused) as e:
        _plan(proj, keys, tmp_path)
    assert e.value.code == "tool_in_project" and "/usr/local/bin/mes" in e.value.hint


def test_plan_refuses_a_login_key_that_also_signs(proj, keys, tmp_path):
    """Finding 5: with one key for both, a stolen key deploys anything."""
    with pytest.raises(Refused) as e:
        server.build_plan(proj, "demo", server_root=str(tmp_path / "srv"), tools=dict(TOOLS),
                          key_files=[f"deploy={keys['signer']}"], signer_files=[str(keys["signer"])])
    assert e.value.code == "same_key"


def test_a_missing_web_address_is_explained_with_mes_options(proj, keys, tmp_path, monkeypatch):
    exe = tmp_path / "easy-ui"
    exe.write_text(f"#!{sys.executable}\nimport json, sys\n"
                   "print(json.dumps({'error': {'code': 'origin_needed', 'message': 'needs the web address', "
                   "'hint': 'Add it, for example: easy-ui units --render-only --origin https://x'}}))\n"
                   "sys.exit(2)\n")
    exe.chmod(0o755)
    monkeypatch.setenv("MES_EASY_UI", str(exe))
    # No web address given: mes says so itself, with its own options (never easy-ui's).
    notes = " ".join(_plan(proj, keys, tmp_path).notes)
    assert "no web address was given" in notes and "--allow-host" in notes
    assert "easy-ui units --render-only" not in notes
    # Given, but easy-ui still asks for one: the same advice in mes's words.
    notes = " ".join(_plan(proj, keys, tmp_path, allow_hosts=["my-server.example.ts.net"]).notes)
    assert "mes server plan again with --allow-host" in notes and "easy-ui units --render-only" not in notes


def test_without_easy_ui_the_plan_says_so(proj, keys, tmp_path):
    plan = _plan(proj, keys, tmp_path)
    assert not plan.web_units and any("easy-ui (my-easy-ui) is not installed" in n for n in plan.notes)


# --------------------------------------------------------------------------- command line


def _mes(*argv, cwd=None):
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    old = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.run(list(argv))
    finally:
        os.chdir(old)
    return code, out.getvalue(), err.getvalue()


def test_plan_command_writes_an_owner_only_file_once(proj, keys, tmp_path):
    out = tmp_path / "plan.sh"
    code, stdout, _ = _mes("--json", "server", "plan", "demo", "--project-dir", str(proj), "--out", str(out),
                           "--mes", "/bin/sh", "--uv", "/bin/sh", "--easy-ui", "/bin/sh",
                           "--key", f"deploy={keys['deploy']}", "--signer", str(keys["signer"]))
    assert code == 0, stdout
    data = json.loads(stdout)
    assert data["written_to"] == str(out) and "script" not in data
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert data["accounts"] == ["mes-demo", "mes-demo-web"]
    code, stdout, _ = _mes("--json", "server", "plan", "demo", "--project-dir", str(proj), "--out", str(out))
    assert code == 5 and json.loads(stdout)["error"]["code"] == "exists"


def test_plan_without_out_prints_the_script(proj):
    code, stdout, _ = _mes("server", "plan", "demo", "--project-dir", str(proj))
    assert code == 0 and stdout.startswith("#!/usr/bin/env bash")


def test_remove_script(tmp_path):
    keep = server.render_remove("demo", server_root=str(tmp_path / "srv"), units_dir=str(tmp_path / "u"))
    assert "--confirm" in keep and "KEEPS the data" in keep and "chown -R -P root:root" in keep
    for sub in ("auth", "gate", "webhook", "code", "etc", "home"):
        assert f"rm -rf --one-file-system -- {shlex.quote(f'{tmp_path}/srv/demo/{sub}')}" in keep, sub
    # Finding 15: running instances and leftover processes are stopped before the accounts go.
    lines = keep.splitlines()
    first_userdel = next(i for i, ln in enumerate(lines) if "userdel" in ln)
    for needle in ("systemctl stop -- 'mes-demo.*' 'mes-demo-web.*'", 'pkill -KILL -u "$acct"',
                   'loginctl terminate-user "$acct"', 'pgrep -u "$acct"'):
        i = next(i for i, ln in enumerate(lines) if needle in ln)
        assert i < first_userdel, needle
    assert f"rm -rf --one-file-system -- {shlex.quote(f'{tmp_path}/srv/demo/data')}" not in keep
    assert "mes-demo-web.socket" in keep and "mes-demo-web.service" in keep
    gone = server.render_remove("demo", delete_data=True)
    assert "DELETES EVERYTHING" in gone and "rm -rf --one-file-system -- /var/lib/mes/demo" in gone
    f = tmp_path / "r.sh"
    f.write_text(keep)
    res = subprocess.run(["bash", str(f)], capture_output=True, text=True)
    assert res.returncode == 1 and "run it with --confirm" in res.stderr


# --------------------------------------------------------------------------- check


def _probe(tmp_path: Path, accounts: dict, files: dict, active: set[str] = frozenset()):
    def getpwnam(name):
        if name not in accounts:
            raise KeyError(name)
        return accounts[name]

    def lstat(path):
        info = files.get(path)
        if info is None:
            raise FileNotFoundError(path)
        return info

    def read(path):
        if path in texts:
            return texts[path]
        raise OSError(path)

    texts: dict[str, str] = {}
    probe = server.ServerProbe(getpwnam=getpwnam, getgrall=lambda: [], lstat=lstat, read=read,
                               run=lambda argv: (0, "active") if argv[-1] in active else (3, "inactive"))
    return probe, texts


def _st(mode: int, uid: int = 0, gid: int = 0):
    return os.stat_result((mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


def test_check_reports_everything_missing_without_root(tmp_path):
    probe, _ = _probe(tmp_path, {}, {})
    result = server.run_check("demo", server_root="/var/lib/mes", probe=probe)
    assert result["ok"] is False
    ids = {i["id"]: i for i in result["items"]}
    assert ids["account-mes-demo"]["status"] == "todo" and "mes server plan demo" in ids["account-mes-demo"]["fix"]
    assert ids["folders"]["status"] == "todo"
    assert ids["units"]["status"] == "skipped"


def test_check_passes_on_a_server_that_matches_the_plan(proj, keys, tmp_path):
    plan = server.build_plan(proj, "demo", tools=dict(TOOLS), key_files=[f"deploy={keys['deploy']}"],
                             signer_files=[str(keys["signer"])])
    base = "/var/lib/mes/demo"
    pw = types.SimpleNamespace
    accounts = {"mes-demo": pw(pw_uid=1001, pw_gid=1001, pw_shell=f"{base}/etc/gate-shell"),
                "mes-demo-web": pw(pw_uid=1002, pw_gid=1002, pw_shell="/usr/sbin/nologin")}
    d, f = stat.S_IFDIR, stat.S_IFREG
    files = {
        "/": _st(d | 0o755), "/bin": _st(d | 0o755),
        base: _st(d | 0o711), f"{base}/etc": _st(d | 0o750, 0, 1001), f"{base}/home": _st(d | 0o750, 0, 1001),
        f"{base}/home/.ssh": _st(d | 0o750, 0, 1001), f"{base}/code": _st(d | 0o700, 1001, 1001),
        f"{base}/data": _st(d | 0o700, 1001, 1001), f"{base}/state": _st(d | 0o700, 1001, 1001),
        f"{base}/store": _st(d | 0o700, 1001, 1001), f"{base}/gate": _st(d | 0o700, 1001, 1001),
        f"{base}/webhook": _st(d | 0o700, 1001, 1001), f"{base}/backups": _st(d | 0o700, 1001, 1001),
        f"{base}/ui": _st(d | 0o2750, 1001, 1002),
        f"{base}/ui/data": _st(d | 0o2750, 1001, 1002), f"{base}/ui/specs": _st(d | 0o2750, 1001, 1002),
        f"{base}/auth": _st(d | 0o700, 1002, 1002), f"{base}/log": _st(d | 0o750, 0, 1001),
        f"{base}/etc/allowed_signers": _st(f | 0o640, 0, 1001), f"{base}/etc/gate-shell": _st(f | 0o750, 0, 1001),
        f"{base}/etc/server.toml": _st(f | 0o640, 0, 1001),
        f"{base}/home/.ssh/authorized_keys": _st(f | 0o640, 0, 1001),
        "/bin/sh": _st(f | 0o755), "/opt": _st(d | 0o755), "/opt/mes": _st(d | 0o755),
        "/opt/mes/my-easy-software": _st(d | 0o755), "/opt/mes/my-easy-software/.venv": _st(d | 0o755),
        "/opt/mes/my-easy-software/.venv/bin": _st(d | 0o755), units.DEFAULT_MES_PYTHON: _st(f | 0o755),
    }
    active = {n for n in plan.units if n.endswith((".timer", ".socket", ".path"))}
    probe, texts = _probe(tmp_path, accounts, files, active)
    texts[f"{base}/home/.ssh/authorized_keys"] = server._authorized_keys(plan)
    texts[f"{base}/etc/allowed_signers"] = server._allowed_signers(plan)
    texts[f"{base}/etc/server.toml"] = server._server_toml(plan)
    for name, text in plan.units.items():
        texts[f"/etc/systemd/system/{name}"] = text
    result = server.run_check("demo", project_dir=str(proj), probe=probe)
    bad = [i for i in result["items"] if i["status"] not in ("done", "skipped")]
    assert not bad, bad
    # A changed unit (for example after editing project.toml) is reported.
    first = sorted(plan.units)[0]
    texts[f"/etc/systemd/system/{first}"] += "# changed\n"
    result = server.run_check("demo", project_dir=str(proj), probe=probe)
    units_item = next(i for i in result["items"] if i["id"] == "units")
    assert units_item["status"] == "todo" and first in units_item["why"]
    # --no-describe never reads the project or asks easy-ui.
    result = server.run_check("demo", project_dir=str(proj), probe=probe, describe=False)
    assert next(i for i in result["items"] if i["id"] == "units-plan")["status"] == "skipped"
    # Finding 12: etc/ readable by everyone is reported.
    files[f"{base}/etc"] = _st(d | 0o755, 0, 1001)
    result = server.run_check("demo", project_dir=str(proj), probe=probe)
    assert next(i for i in result["items"] if i["id"] == "folders")["status"] == "todo"
    files[f"{base}/etc"] = _st(d | 0o750, 0, 1001)
    # A key line that only mentions the gate somewhere is not a forced command.
    texts[f"{base}/home/.ssh/authorized_keys"] = ("ssh-ed25519 " + "A" * 68 + ' x restrict,command="/bin/sh gate '
                                                  '--project demo --allow read --server-root /var/lib/mes"\n')
    result = server.run_check("demo", project_dir=str(proj), probe=probe)
    assert next(i for i in result["items"] if i["id"] == "keys-forced")["status"] == "todo"
    # A tool in a folder others can change is reported (every folder on the way is checked).
    files["/opt/mes/my-easy-software"] = _st(d | 0o777)
    result = server.run_check("demo", project_dir=str(proj), probe=probe)
    assert next(i for i in result["items"] if i["id"] == "tool-mes_python")["status"] == "todo"


def test_check_says_what_it_cannot_see_without_root(tmp_path):
    base = "/var/lib/mes/demo"
    pw = types.SimpleNamespace
    accounts = {"mes-demo": pw(pw_uid=1001, pw_gid=1001, pw_shell=f"{base}/etc/gate-shell")}
    d = stat.S_IFDIR
    files = {base: _st(d | 0o711), f"{base}/etc": _st(d | 0o750, 0, 1001)}

    def lstat(path):
        if path.startswith(f"{base}/etc/"):
            raise PermissionError(path)
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    probe = server.ServerProbe(getpwnam=lambda n: accounts[n] if n in accounts else (_ for _ in ()).throw(KeyError(n)),
                               getgrall=lambda: [], lstat=lstat, read=lambda p: (_ for _ in ()).throw(OSError(p)),
                               run=lambda argv: (3, "inactive"))
    result = server.run_check("demo", probe=probe)
    item = next(i for i in result["items"] if i["id"] == "file-etc-allowed_signers")
    assert item["status"] == "skipped" and "only root and mes-demo" in item["why"]


# --------------------------------------------------------------------------- per app


def test_plan_shows_network_and_limits_per_app(proj, keys, tmp_path):
    plan = _plan(proj, keys, tmp_path)
    apps = server.per_app(plan.project)
    assert [a["app"] for a in apps] == ["demo-app"] and apps[0]["network"] == ["api.example.com"]
    assert [j["id"] for j in apps[0]["jobs"]] == ["demo-app.sync", "demo-app.build", "demo-app.hook"]
    sync = apps[0]["jobs"][0]
    assert sync["network"] == ["api.example.com"] and sync["memory"] and sync["timeout"]
    script = server.render_script(plan)
    assert "demo-app (in-project): api.example.com" in script
    assert "demo-app.build: memory" in script and "internet: none" in script


# --------------------------------------------------------------------------- --no-start, systemd, first publish, run-as

def test_no_start_switches_on_nothing_that_runs_jobs(proj, keys, tmp_path):
    """Item D: a first install next to an existing archive must not start a second schedule."""
    plan = _plan(proj, keys, tmp_path, start=False)
    script = server.render_script(plan)
    enable = [ln for ln in script.splitlines() if "systemctl enable --now -- " in ln and not ln.lstrip().startswith(
        ("#", "say", "echo"))]
    assert enable and not any(".job." in ln or ".webhook." in ln for ln in enable), enable
    assert "made with --no-start" in script
    (tmp_path / "units").mkdir()
    res = _run_script(script, tmp_path / "fake")
    assert res.returncode == 0, res.stderr
    log = (tmp_path / "fake" / "log").read_text()
    started = [ln for ln in log.splitlines() if ln.startswith("systemctl enable --now")]
    assert started and not any(".job." in ln for ln in started), started
    assert "sudo systemctl enable --now -- mes-demo.job.demo-app.sync.timer" in res.stdout
    assert "Nothing that runs jobs was switched on" in res.stdout
    # Without --no-start the timers are switched on, and the last lines say which run.
    again = server.build_plan(proj, "demo", server_root=str(tmp_path / "srv2"), tools=dict(TOOLS),
                              key_files=[f"deploy={keys['deploy']}"], signer_files=[str(keys["signer"])],
                              units_dir=str(tmp_path / "units"))
    res = _run_script(server.render_script(again), tmp_path / "fake2")
    assert res.returncode == 0, res.stderr
    assert any(".job.demo-app.sync.timer" in ln for ln in (tmp_path / "fake2" / "log").read_text().splitlines()
               if ln.startswith("systemctl enable --now"))


def test_an_old_systemd_is_refused(proj, keys, tmp_path, monkeypatch):
    script = server.render_script(_plan(proj, keys, tmp_path))
    (tmp_path / "units").mkdir()
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "fakes.sh").write_text(FAKES)
    (fake / "plan.sh").write_text(_harness(script))
    res = subprocess.run(["bash", "-c", f'FAKE="{fake}"; FAKE_SYSTEMD=245; source "{fake}/fakes.sh"; '
                          f'source "{fake}/plan.sh"'], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "HOME": str(fake)})
    assert res.returncode == 1 and "need 249 or newer" in res.stderr


def test_a_tool_the_sandboxes_cannot_see_is_refused(proj, keys, tmp_path):
    """Finding C: units hide /home, /srv, the rest of /opt and /usr/local, so a tool there would
    fail on the server; the plan says so first."""
    tools = dict(TOOLS, uv="/srv/tools/uv")
    with pytest.raises(Refused) as e:
        server.build_plan(proj, "demo", server_root=str(tmp_path / "srv"), tools=tools,
                          key_files=[f"deploy={keys['deploy']}"], signer_files=[str(keys["signer"])],
                          units_dir=str(tmp_path / "units"))
    assert e.value.code == "tool_hidden"


def test_first_publish_publishes_only_when_nothing_is_published(tmp_path):
    """Item E: the refresh after the first data publishes; later refreshes never do."""
    calls = []

    def run_with(current, publish):
        def run(argv):
            calls.append(argv[4:])
            if argv[4:] == ["versions"]:
                return 0, {"app": "demo", "current": current, "versions": []}
            return publish
        return run

    out = server.first_publish("demo", server_root=str(tmp_path), easy_ui="/bin/sh", run=run_with(None, (0, {})))
    assert out == {"published": True, "first": True} and calls == [["versions"], ["publish"]]
    calls.clear()
    out = server.first_publish("demo", server_root=str(tmp_path), easy_ui="/bin/sh", run=run_with(3, (0, {})))
    assert out["published"] is False and calls == [["versions"]]
    out = server.first_publish("demo", server_root=str(tmp_path), easy_ui="/bin/sh",
                               run=run_with(None, (3, {"error": {"code": "no_data", "message": "x"}})))
    assert out["waiting"] is True and out["status"] == "waiting for first data"
    with pytest.raises(MesError) as e:
        server.first_publish("demo", server_root=str(tmp_path), easy_ui="/bin/sh",
                             run=run_with(None, (1, {"error": {"code": "invalid_spec", "message": "x"}})))
    assert e.value.code == "publish_failed"


def test_run_as_only_prints_the_command(proj, capsys):
    from mes.cli import run as main

    code = main(["--json", "server", "run-as", "demo", "--project-dir", str(proj), "--", "demo-app", "login"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0, data
    assert data["command"].startswith("sudo systemd-run --pty ") and "-p User=mes-demo" in data["command"]
    assert data["command"].endswith("/var/lib/mes/demo/code/current/project/.venv/bin/demo-app login")
    code = main(["--json", "server", "run-as", "demo", "--project-dir", str(proj), "--", "rm", "-rf"])
    assert code != 0


def test_run_as_has_no_network_unless_asked(proj, capsys, monkeypatch):
    """run-as had the internet (and the resolver, so local and tailnet names) by default. Now:
    nothing by default; --network HOST only that host's internet addresses; --network any the
    internet without what systemd can block locally, with a plain note of what it cannot."""
    from mes import jobs
    from mes.cli import run as main

    base = ["--json", "server", "run-as", "demo", "--project-dir", str(proj)]
    code = main([*base, "--", "demo-app", "login"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["network"] == "none" and "-p PrivateNetwork=yes" in data["command"]
    assert "resolve" not in data["command"] and "and no network." in data["message"]

    def look(host, *a, **k):
        table = {"login.example.com": ["93.184.216.34", "192.168.1.9"], "nas.home.arpa": ["192.168.1.20"]}
        return [(None, None, None, "", (addr, 443)) for addr in table.get(host, [])]

    monkeypatch.setattr(jobs.socket, "getaddrinfo", look)
    code = main([*base, "--network", "login.example.com", "--", "demo-app", "login"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0, data
    assert "-p IPAddressAllow=93.184.216.34" in data["command"] and "192.168.1.9" not in data["command"]
    assert "-p IPAddressDeny=any" in data["command"]
    code = main([*base, "--network", "nas.home.arpa", "--", "demo-app", "login"])
    assert code == 75 and json.loads(capsys.readouterr().out)["error"]["code"] == "dns_failed"
    code = main([*base, "--network", "any", "--", "demo-app", "login"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and "IPAddressDeny=localhost link-local multicast" in data["command"]
    assert data["notes"] and "this server's own public addresses" in data["notes"][0]
    assert "this server's own public addresses" in data["message"]
    code = main([*base, "--network", "any", "--network", "login.example.com", "--", "demo-app", "login"])
    assert code == 2
    capsys.readouterr()


def test_plan_header_says_what_the_install_steps_filter_does_not_cover(proj, keys, tmp_path):
    head = server.render_script(_plan(proj, keys, tmp_path)).split(server.PLAN_MARKER)[0]
    assert "# Not covered: this server's own public addresses and the global IPv6 addresses of other machines" in head
    assert "--allow-server-builds" not in head


def test_server_builds_change_the_install_unit_of_the_plan(proj, keys, tmp_path):
    """--allow-server-builds was only a flag in server.toml: the install unit still let package
    build code write the shared uv cache and Python. The plan now renders that unit for it."""
    plain = server.build_plan(proj, "demo", tools={"mes": "/bin/sh", "uv": "/bin/sh"})
    built = server.build_plan(proj, "demo", tools={"mes": "/bin/sh", "uv": "/bin/sh"}, allow_builds=True)
    unit = "mes-demo.gate-deploy@.service"
    assert "BindPaths=-/var/lib/mes/demo/code/uv-cache" in plain.units[unit]
    assert "BindPaths=-/var/lib/mes/demo/code/installing\n" in built.units[unit]
    assert plain.fingerprint != built.fingerprint
    assert "build code runs while" in server.render_script(built).split(server.PLAN_MARKER)[0]
