"""Tests for mes.wheels: the release's own wheels are unpacked without starting any Python.

Finding A: the install step ran ``uv pip install --python <release>/.venv/bin/python``, which
starts the release's interpreter, and that runs every ``.pth`` startup file of the packages
already installed there. The stand-in here is a harmless ``.pth`` file that writes a marker file
when Python starts; it must never be written.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from mes import wheels
from mes.errors import Refused


def _hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _wheel(folder: Path, files: dict[str, bytes], *, name: str = "demo", version: str = "0.1.0",
           record: bool = True, extra_record: str = "", links: dict[str, str] | None = None) -> Path:
    di = f"{name}-{version}.dist-info"
    files = {**files,
             f"{di}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
             f"{di}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    path = folder / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as z:
        lines = []
        for n, data in files.items():
            z.writestr(n, data)
            lines.append(f"{n},{_hash(data)},{len(data)}")
        for n, target in (links or {}).items():
            info = zipfile.ZipInfo(n)
            info.external_attr = (0o120777 << 16)
            z.writestr(info, target)
            lines.append(f"{n},{_hash(target.encode())},{len(target)}")
        if record:
            z.writestr(f"{di}/RECORD", "\n".join(lines) + f"\n{di}/RECORD,,\n" + extra_record)
    return path


def _venv(folder: Path) -> Path:
    venv = folder / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {Path(sys._base_executable).parent}\ninclude-system-site-packages = false\n")
    os.symlink(os.path.realpath(sys._base_executable), venv / "bin" / "python")
    return venv


def test_a_startup_file_never_runs_while_installing(tmp_path):
    marker = tmp_path / "marker"
    venv = _venv(tmp_path)
    site = wheels.site_packages(venv)
    # A package already in the environment with a startup file (harmless stand-in: it writes a marker).
    (site / "zz_marker.pth").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n")
    whl = _wheel(tmp_path, {"demo/__init__.py": b"def main():\n    print('demo ran')\n",
                            "demo-0.1.0.dist-info/entry_points.txt": b"[console_scripts]\ndemo = demo:main\n"})
    out = wheels.install(whl, venv)
    assert not marker.exists()  # nothing started the environment's Python
    assert out["name"] == "demo" and out["scripts"] == ["demo"]
    assert (site / "demo-0.1.0.dist-info" / "INSTALLER").read_text() == "mes\n"
    script = venv / "bin" / "demo"
    assert str(venv / "bin" / "python") in "".join(script.read_text().splitlines(True)[:2])
    # The script works (and only now, run on purpose, does the startup file run).
    res = subprocess.run([str(script)], capture_output=True, text=True)
    assert res.stdout.strip() == "demo ran" and marker.exists()


@pytest.mark.parametrize("files,links,why", [
    ({"../evil.py": b"x"}, None, "name mes does not accept"),
    ({"/abs.py": b"x"}, None, "name mes does not accept"),
    ({"demo/a\\b.py": b"x"}, None, "name mes does not accept"),
    ({"demo/__init__.py": b""}, {"demo/link.py": "/etc/passwd"}, ""),
])
def test_unsafe_members_are_refused(tmp_path, files, links, why):
    venv = _venv(tmp_path)
    whl = _wheel(tmp_path, files, links=links)
    with pytest.raises(Refused) as e:
        wheels.install(whl, venv)
    assert e.value.code == "bad_wheel" and why in e.value.message
    assert not any(wheels.site_packages(venv).iterdir())  # nothing half-installed


def test_a_wrong_or_missing_record_is_refused(tmp_path):
    venv = _venv(tmp_path)
    with pytest.raises(Refused):
        wheels.install(_wheel(tmp_path, {"demo/__init__.py": b""}, record=False), venv)
    (tmp_path / "w2").mkdir()
    bad = _wheel(tmp_path / "w2", {"demo/__init__.py": b""},
                 extra_record=f"demo/missing.py,{_hash(b'x')},1\n")
    with pytest.raises(Refused):
        wheels.install(bad, venv)
    # A file whose content does not match its RECORD line.
    (tmp_path / "w3").mkdir()
    whl = _wheel(tmp_path / "w3", {"demo/__init__.py": b"ok"})
    tampered = tmp_path / "w3" / "t" / whl.name
    tampered.parent.mkdir()
    with zipfile.ZipFile(whl) as src, zipfile.ZipFile(tampered, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, b"changed" if item.filename == "demo/__init__.py" else src.read(item))
    with pytest.raises(Refused):
        wheels.install(tampered, venv)
    assert not (wheels.site_packages(venv) / "demo").exists()


def test_only_pure_python_wheels_and_no_overwrites(tmp_path):
    venv = _venv(tmp_path)
    whl = _wheel(tmp_path, {"demo/__init__.py": b""})
    native = whl.with_name("demo-0.1.0-cp313-cp313-linux_x86_64.whl")
    native.write_bytes(whl.read_bytes())
    with pytest.raises(Refused):
        wheels.install(native, venv)
    wheels.install(whl, venv)
    with pytest.raises(Refused) as e:
        wheels.install(whl, venv)  # the same files again: never written over
    assert e.value.code == "wheel_conflict"


def test_a_link_in_the_way_is_not_followed(tmp_path):
    venv = _venv(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, wheels.site_packages(venv) / "demo")
    with pytest.raises(Refused):
        wheels.install(_wheel(tmp_path, {"demo/__init__.py": b"x"}), venv)
    assert not any(outside.iterdir())


def test_long_paths_get_the_shell_trampoline(tmp_path):
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    venv = _venv(deep)
    whl = _wheel(tmp_path, {"demo/__init__.py": b"def main():\n    print('deep ran')\n",
                            "demo-0.1.0.dist-info/entry_points.txt": b"[console_scripts]\ndemo = demo:main\n"})
    wheels.install(whl, venv)
    script = venv / "bin" / "demo"
    assert script.read_text().startswith("#!/bin/sh\n")
    assert subprocess.run([str(script)], capture_output=True, text=True).stdout.strip() == "deep ran"
