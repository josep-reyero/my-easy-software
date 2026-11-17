"""The root script's own checks of the tools it hands to systemd (finding 13): every folder and
link on the way, the program behind a script (also through uv's long-path trampoline), and for
mes an isolated virtual environment of the same version.

The prelude really runs under bash. ``stat`` is replaced by a stand-in that reports the real
mode and treats files of the account running the tests as root's, inside a test folder only
(everything outside it counts as root's 0755), so no root is needed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mes import server

UNSAFE = ("/tmp", "/var/tmp", "/dev/shm", "/run/shm", "/home", "/root", "/run/user")

FAKE_STAT = r'''
stat() {
  local fmt="$2" path="${@: -1}"
  python3 - "$fmt" "$path" "$TESTROOT" <<'PY'
import os, sys
fmt, path, root = sys.argv[1:]
inside = os.path.abspath(path) == root or os.path.abspath(path).startswith(root + "/")
if not inside:
    print({"%u": "0", "%a": "755"}[fmt])
else:
    st = os.lstat(path)
    uid = 0 if st.st_uid == os.getuid() else st.st_uid
    print({"%u": str(uid), "%a": format(st.st_mode & 0o7777, "o")}[fmt])
PY
}
'''


@pytest.fixture
def root(tmp_path):
    real = os.path.realpath(tmp_path)
    if any(real == u or real.startswith(u + "/") for u in UNSAFE):
        pytest.skip("the test folder is itself in a folder the script refuses (such as /tmp)")
    os.chmod(real, 0o755)  # pytest makes it owner-only; a real tools folder lets others pass
    return Path(real)


def _program(path: Path, text: str, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


BUILD = "0123456789abcdef"


def _mes_tree(root: Path, *, version: str = server.__version__, isolated: bool = True, build: str = BUILD,
              editable: bool = False) -> tuple[Path, Path]:
    venv = root / "opt" / "mes" / "venv"
    lib = (root / "opt" / "mes" / "src" / "mes") if editable else venv / "lib" / "mes"
    lib.mkdir(parents=True)
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (lib / "__init__.py").write_text("")
    (venv / "pyvenv.cfg").write_text(f"home = /usr/bin\ninclude-system-site-packages = {'false' if isolated else 'true'}\n")
    py = _program(venv / "bin" / "python", "#!/bin/sh\n"
                  f'if [ "$*" = "-I -m mes --version" ]; then echo "mes {version}"; exit 0; fi\n'
                  f'case "$3" in *buildinfo*) echo "{build}"; exit 0 ;; esac\n'
                  f'if [ "$1" = -I ] && [ "$2" = -c ]; then echo "{lib}"; exit 0; fi\n'
                  "exit 1\n")
    mes = _program(venv / "bin" / "mes", f"#!{py}\nimport mes\n")
    return mes, py


def _check(root: Path, call: str) -> subprocess.CompletedProcess[str]:
    prelude = server._PRELUDE.replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", "")
    # The tools of these tests live in <test folder>/opt, which stands in for /opt/mes.
    prelude = prelude.replace("for r in /usr /bin /sbin /lib /lib64 /opt/mes; do",
                              f"for r in /usr /bin /sbin /lib /lib64 /opt/mes {root / 'opt'}; do")
    script = (FAKE_STAT + prelude
              + f"\nMES_ROOT={root / 'srv'}\n{call}\necho CHECKED\n")
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "TESTROOT": str(root)})


def test_a_well_installed_mes_passes(root):
    mes, py = _mes_tree(root)
    res = _check(root, f"check_mes {mes} {py} {server.__version__} {BUILD}")
    assert res.returncode == 0 and "CHECKED" in res.stdout, res.stderr


def test_uv_long_path_trampoline_is_followed_to_the_real_python(root):
    mes, py = _mes_tree(root)
    _program(mes, f"#!/bin/sh\n'''exec' '{py}' \"$0\" \"$@\"\n' '''\nimport mes\n")
    res = _check(root, f"check_mes {mes} {py} {server.__version__} {BUILD}")
    assert res.returncode == 0, res.stderr
    other = _program(root / "opt" / "other" / "python", "#!/bin/sh\nexit 0\n")
    _program(mes, f"#!/bin/sh\n'''exec' '{other}' \"$0\" \"$@\"\n' '''\n")
    res = _check(root, f"check_mes {mes} {py} {server.__version__} {BUILD}")
    assert res.returncode == 1 and "the plan expects" in res.stderr


def test_a_folder_others_can_change_on_the_way_is_refused(root):
    mes, py = _mes_tree(root)
    (root / "opt" / "mes").chmod(0o777)
    res = _check(root, f"check_mes {mes} {py} {server.__version__} {BUILD}")
    assert res.returncode == 1 and "can be changed by other accounts" in res.stderr


def test_a_link_into_a_refused_folder_is_refused(root):
    mes, py = _mes_tree(root)
    (root / "srv").mkdir()
    hidden = _program(root / "srv" / "uv", "#!/bin/sh\nexit 0\n")
    link = root / "opt" / "uv"
    link.symlink_to(hidden)
    res = _check(root, f"check_tool uv {link}")
    assert res.returncode == 1 and f"really in {hidden}" in res.stderr


def test_a_script_started_through_env_is_refused(root):
    tool = _program(root / "opt" / "bin" / "uv", "#!/usr/bin/env python3\n")
    res = _check(root, f"check_tool uv {tool}")
    assert res.returncode == 1 and "through env" in res.stderr


def test_a_tool_the_project_cannot_run_or_read_is_refused(root):
    tool = _program(root / "opt" / "bin" / "uv", "#!/bin/sh\nexit 0\n", mode=0o750)
    res = _check(root, f"check_tool uv {tool}")
    assert res.returncode == 1 and "cannot be run by the project's accounts" in res.stderr
    tool.chmod(0o711)
    res = _check(root, f"check_tool uv {tool}")
    assert res.returncode == 1 and "cannot read" in res.stderr


def test_a_python_with_system_packages_is_refused(root):
    mes, py = _mes_tree(root, isolated=False)
    res = _check(root, f"check_mes {mes} {py} {server.__version__} {BUILD}")
    assert res.returncode == 1 and "not an isolated virtual environment" in res.stderr


def test_a_different_mes_version_is_refused(root):
    mes, py = _mes_tree(root, version="0.0.1")
    res = _check(root, f"check_mes {mes} {py} {server.__version__} {BUILD}")
    assert res.returncode == 1 and "runs mes 0.0.1" in res.stderr


def test_the_interpreter_of_a_script_is_checked_too(root):
    mes, py = _mes_tree(root)
    py.parent.chmod(0o777)
    res = _check(root, f"check_tool mes {mes}")
    assert res.returncode == 1 and "can be changed by other accounts" in res.stderr


def test_another_build_of_the_same_version_is_refused(root):
    """Item I: __version__ is the same for every commit, so the plan also names the build."""
    mes, py = _mes_tree(root, build="fedcba9876543210")
    res = _check(root, f"check_mes {mes} {py} {server.__version__} {BUILD}")
    assert res.returncode == 1 and "runs build fedcba9876543210" in res.stderr


def test_an_editable_mes_is_refused(root):
    mes, py = _mes_tree(root, editable=True)
    res = _check(root, f"check_mes {mes} {py} {server.__version__} {BUILD}")
    assert res.returncode == 1 and "installed as editable" in res.stderr


def test_a_tool_the_services_cannot_see_is_refused(root):
    """Finding C: the units hide everything but /usr, /bin, /lib and /opt/mes."""
    tool = _program(root / "elsewhere" / "uv", "#!/bin/sh\nexit 0\n")
    res = _check(root, f"check_tool uv {tool}")
    assert res.returncode == 1 and "only see programs in /usr" in res.stderr


def test_the_locked_install_layout_passes(root):
    """Item I: a clone at /opt/mes/my-easy-software with 'uv sync --locked --no-editable' and a link
    in /usr/local/bin (here under the test folder): mes's code sits in the environment's lib/."""
    venv = root / "opt" / "my-easy-software" / ".venv"
    lib = venv / "lib" / "python3.13" / "site-packages" / "mes"
    lib.mkdir(parents=True)
    (lib / "__init__.py").write_text("")
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\ninclude-system-site-packages = false\n")
    py = _program(venv / "bin" / "python", "#!/bin/sh\n"
                  f'if [ "$*" = "-I -m mes --version" ]; then echo "mes {server.__version__}"; exit 0; fi\n'
                  f'case "$3" in *buildinfo*) echo "{BUILD}"; exit 0 ;; esac\n'
                  f'if [ "$1" = -I ] && [ "$2" = -c ]; then echo "{lib}"; exit 0; fi\nexit 1\n')
    real = _program(venv / "bin" / "mes", f"#!{py}\nimport mes\n")
    link = root / "opt" / "bin" / "mes"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    res = _check(root, f"check_mes {link} {py} {server.__version__} {BUILD}")
    assert res.returncode == 0 and "CHECKED" in res.stdout, res.stderr
