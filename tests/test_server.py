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

from mes import cli, server, units

import projects as helpers
from mes.errors import Refused, UsageError

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
        f"ensure_dir {q(base + '/code')} mes-demo mes-demo 0700",
        f"ensure_dir {q(base + '/data')} mes-demo mes-demo 0700",
        f"ensure_dir {q(base + '/state')} mes-demo mes-demo 0700",
        f"ensure_dir {q(base + '/ui')} mes-demo mes-demo-web 2750",
        f"ensure_sub {q(base)} ui/auth mes-demo-web mes-demo-web 0700",
        f"ensure_sub {q(base)} ui/data mes-demo mes-demo-web 2750",
        f"ensure_dir {q(base + '/log')} root mes-demo 0750",
        "usermod -p '*' -- mes-demo",
        "usermod -p '!' -- mes-demo-web",
        f"ensure_account mes-demo {q(base + '/home')} {q(base + '/etc/gate-shell')} 'mes project demo'",
    ]:
        assert line in script, line
    code_lines = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
    assert not any(re.match(r"\s*sudo\b", ln) for ln in code_lines)
    assert 'command="/bin/sh gate --project demo --allow read --server-root ' in script
    assert 'command="/bin/sh gate --project demo --allow deploy --server-root ' in script
    assert "someone@example.invalid" not in script  # key comments are never copied
    assert 'demo namespaces="mes-deploy" ssh-ed25519 ' in script
    for name in plan.units:
        assert f"put_file {q(f'{tmp_path}/units/{name}')} 0644 root:root <<'MES_END_" in script
    assert "mes-demo.job.demo-app.sync.timer" in script and "systemctl enable --now -- " in script
    assert "webhook.key" in script and "od -An -tx1 -N32 /dev/urandom" in script


def test_script_only_touches_its_own_places(proj, keys, tmp_path):
    script = server.render_script(_plan(proj, keys, tmp_path)).replace(server._PRELUDE, "")
    srv, udir = f"{tmp_path}/srv", f"{tmp_path}/units"
    allowed_prefixes = (srv, f"{udir}/mes-demo.", "/dev/null", "/dev/urandom", "/nonexistent", "/usr/sbin/nologin",
                        "/bin/sh", "/usr/sbin:/usr/bin:/sbin:/bin", "/usr/bin/env bash")
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
            if path in ("/", "/*", "/bin/false"):
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
systemctl() { echo "systemctl $*" >> "$LOG"; }
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


def _run_script(script: str, fake: Path) -> subprocess.CompletedProcess[str]:
    fake.mkdir(exist_ok=True)
    (fake / "fakes.sh").write_text(FAKES)
    (fake / "plan.sh").write_text(script)
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
    first = _run_script(script, tmp_path / "fake")
    assert first.returncode == 0, first.stderr
    assert "Done. The project demo is set up." in first.stdout
    base = tmp_path / "srv" / "demo"
    for sub, mode in (("code", 0o700), ("data", 0o700), ("state", 0o700), ("store", 0o700), ("ui/auth", 0o700),
                      ("ui", 0o2750), ("ui/data", 0o2750), ("ui/specs", 0o2750),
                      ("log", 0o750), ("etc", 0o755), ("home/.ssh", 0o755)):
        assert stat.S_IMODE((base / sub).stat().st_mode) == mode, sub
    assert stat.S_IMODE(base.stat().st_mode) == 0o711
    ak = (base / "home/.ssh/authorized_keys").read_text()
    assert ak.count("restrict,command=") == 2 and stat.S_IMODE((base / "home/.ssh/authorized_keys").stat().st_mode) == 0o644
    assert (base / "etc/allowed_signers").read_text().count('namespaces="mes-deploy"') == 1
    assert stat.S_IMODE((base / "etc/gate-shell").stat().st_mode) == 0o755
    key = (base / "etc/webhook.key").read_text()
    assert re.fullmatch(r"[0-9a-f]{64}", key.strip()), key
    assert stat.S_IMODE((base / "etc/webhook.key").stat().st_mode) == 0o640
    assert stat.S_IMODE((base / "state/triggers").stat().st_mode) == 0o700
    for name, text in plan.units.items():
        assert (tmp_path / "units" / name).read_text() == text.rstrip("\n") + "\n"
    assert not stale.exists() and foreign.exists()
    log = (tmp_path / "fake" / "log").read_text()
    assert log.count("useradd") == 2 and "systemctl daemon-reload" in log
    assert "systemctl disable --now -- mes-demo.job.old.service" in log
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
    (tmp_path / "srv" / "demo" / "ui" / "auth").symlink_to(tmp_path / "elsewhere")
    res = _run_script(script, fake)
    assert res.returncode == 1 and "is not a real folder" in res.stderr
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
    exe.write_text(f"#!{sys.executable}\nimport json, sys\n"
                   f"(open({str(tmp_path / 'easy-ui-argv.json')!r}, 'w').write(json.dumps(sys.argv[1:])))\n"
                   f"print(json.dumps({json.dumps(units_answer)}))\n")
    exe.chmod(0o755)
    return exe


GOOD_SERVICE = "[Service]\nUser=mes-demo-web\nExecStart=/bin/sh serve\n"


def test_dashboard_units_come_from_easy_ui(proj, keys, tmp_path, monkeypatch):
    exe = _fake_easy_ui(tmp_path, {"units": {"mes-demo-web.socket": "[Socket]\nListenStream=/run/easy-ui/demo.sock\n",
                                             "mes-demo-web.service": GOOD_SERVICE}})
    monkeypatch.setenv("MES_EASY_UI", str(exe))
    plan = _plan(proj, keys, tmp_path)
    argv = json.loads((tmp_path / "easy-ui-argv.json").read_text())
    assert argv[:3] == ["--json", "--project-dir", str(proj.resolve())]
    assert argv[3:] == ["units", "--render-only", "--exec", "/bin/sh", "--root", str(tmp_path / "srv")]
    assert set(plan.web_units) == {"mes-demo-web.socket", "mes-demo-web.service"}
    script = server.render_script(plan)
    assert "mes-demo-web.socket" in script.split("systemctl enable --now -- ")[1]


@pytest.mark.parametrize("answer", [
    {"units": {"mes-demo-web.service": "[Service]\nUser=root\n"}},
    {"units": {"mes-demo-web.service": "[Service]\nExecStart=/bin/sh\n"}},
    {"units": {"mes-demo-web.service": GOOD_SERVICE + "User=mes-demo\n"}},
    {"units": {"mes-demo-web.service": GOOD_SERVICE + "ExecStartPre=+/bin/sh -c id\n"}},
    {"units": {"mes-demo-web.service": GOOD_SERVICE + "AmbientCapabilities=CAP_NET_ADMIN\n"}},
    {"units": {"mes-demo-web.service": GOOD_SERVICE + "Group=root\n"}},
    {"units": {"mes-demo-web.service": GOOD_SERVICE + "SupplementaryGroups=mes-demo\n"}},
    {"units": {"mes-demo-web.service": GOOD_SERVICE + "ExecStartPost=-+/bin/id\n"}},
    {"units": {"mes-demo-web.socket": "[Socket]\nListenStream=/run/x.sock\nExecStartPre=/bin/id\n"}},
    {"units": {"mes-demo-web.socket": "[Socket]\nListenStream=/run/x.sock\nService=sshd.service\n"}},
    {"units": {"sshd.service": GOOD_SERVICE}},
    {"units": {"mes-demo2-web.service": GOOD_SERVICE}},
    {"units": {"mes-other-web.service": GOOD_SERVICE}},
    {"units": {"mes-demo.job.demo-app.sync.service": GOOD_SERVICE}},
    {"units": {"../../etc/x-demo.service": GOOD_SERVICE}},
    {"units": {"mes-demo-web.service": GOOD_SERVICE + "\x00"}},
])
def test_unsafe_dashboard_units_are_refused(proj, keys, tmp_path, monkeypatch, answer):
    monkeypatch.setenv("MES_EASY_UI", str(_fake_easy_ui(tmp_path, answer)))
    with pytest.raises(Refused) as e:
        _plan(proj, keys, tmp_path)
    assert e.value.code == "unsafe_web_unit"


def test_easy_ui_folders_are_compared_with_the_plan(proj, keys, tmp_path, monkeypatch):
    base = str(tmp_path / "srv" / "demo")
    good = [{"path": base, "owner": "mes-demo", "group": "mes-demo-web", "mode": "0710"},
            {"path": base + "/ui", "owner": "mes-demo", "group": "mes-demo-web", "mode": "2750"},
            {"path": base + "/ui/auth", "owner": "mes-demo-web", "group": "mes-demo-web", "mode": "0700"}]
    answer = {"units": {"mes-demo-web.service": GOOD_SERVICE}, "folders": good}
    monkeypatch.setenv("MES_EASY_UI", str(_fake_easy_ui(tmp_path, answer)))
    assert not any("easy-ui expects" in n for n in _plan(proj, keys, tmp_path).notes)
    answer["folders"] = good + [{"path": base + "/ui/new", "owner": "mes-demo", "group": "mes-demo", "mode": "0700"}]
    monkeypatch.setenv("MES_EASY_UI", str(_fake_easy_ui(tmp_path, answer)))
    assert any("ui/new" in n for n in _plan(proj, keys, tmp_path).notes)


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
    d = stat.S_IFDIR
    files = {
        base: _st(d | 0o711), f"{base}/etc": _st(d | 0o755), f"{base}/home": _st(d | 0o755),
        f"{base}/home/.ssh": _st(d | 0o755), f"{base}/code": _st(d | 0o700, 1001, 1001),
        f"{base}/data": _st(d | 0o700, 1001, 1001), f"{base}/state": _st(d | 0o700, 1001, 1001),
        f"{base}/store": _st(d | 0o700, 1001, 1001), f"{base}/ui": _st(d | 0o2750, 1001, 1002),
        f"{base}/ui/data": _st(d | 0o2750, 1001, 1002), f"{base}/ui/specs": _st(d | 0o2750, 1001, 1002),
        f"{base}/ui/auth": _st(d | 0o700, 1002, 1002), f"{base}/log": _st(d | 0o750, 0, 1001),
        f"{base}/etc/allowed_signers": _st(stat.S_IFREG | 0o644), f"{base}/etc/gate-shell": _st(stat.S_IFREG | 0o755),
        f"{base}/etc/server.toml": _st(stat.S_IFREG | 0o644),
        f"{base}/home/.ssh/authorized_keys": _st(stat.S_IFREG | 0o644),
        "/bin/sh": _st(stat.S_IFREG | 0o755), os.path.realpath("/bin/sh"): _st(stat.S_IFREG | 0o755),
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


def test_empty_capability_lines_are_fine(proj, keys, tmp_path, monkeypatch):
    service = GOOD_SERVICE + "AmbientCapabilities=\nCapabilityBoundingSet=\n"
    monkeypatch.setenv("MES_EASY_UI", str(_fake_easy_ui(tmp_path, {"units": {"mes-demo-web.service": service}})))
    assert "mes-demo-web.service" in _plan(proj, keys, tmp_path).web_units


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
