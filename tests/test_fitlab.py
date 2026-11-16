"""The proof of the app model on examples/fitlab (docs/apps-contract.md, 9.7), steps 1 to 4, 8 and 9.

A copy of the example is installed with ``uv sync --offline`` (skipped when uv or its cache cannot
do that), then driven as real programs with throwaway per-machine folders. Steps 5 to 7 are
my-easy-ui's (dashboards) and are tested there.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

FRAMEWORK = Path(__file__).resolve().parents[1]
EXAMPLE = FRAMEWORK / "examples" / "fitlab"
uv = shutil.which("uv")


@pytest.fixture(scope="module")
def fitlab(tmp_path_factory):
    if uv is None:
        pytest.skip("uv is not installed")
    base = tmp_path_factory.mktemp("fitlab")
    root = base / "fitlab"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns(".venv", "apps.lock.json", "uv.lock", "__pycache__"))
    py = root / "pyproject.toml"
    py.write_text(py.read_text().replace('path = "../.."', f'path = "{FRAMEWORK.as_posix()}"'))
    homes = {"MES_DATA_HOME": base / "data", "MES_STATE_HOME": base / "state", "MES_CONFIG": base / "config.toml",
             "EASYUI_STATE_HOME": base / "easy-ui"}
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "MES_ALLOW",
                                                             "MES_PROJECT", "MES_PROJECT_DIR")}
    env.update({k: str(v) for k, v in homes.items()})
    proc = subprocess.run([uv, "sync", "--offline", "--quiet"], cwd=root, env=env, capture_output=True, text=True,
                          timeout=300)
    if proc.returncode != 0:
        pytest.skip(f"uv sync --offline did not work here: {proc.stderr[-300:]}")
    bin_dir = root / ".venv" / "bin"

    def run(*argv, allow=None, stdin=None):
        e = dict(env)
        if allow:
            e["MES_ALLOW"] = allow
        exe = str(bin_dir / argv[0])
        return subprocess.run([exe, *argv[1:]], cwd=root, env=e, capture_output=True, text=True, timeout=120,
                              input=stdin)

    return root, run, base


def test_the_proof(fitlab):
    root, run, base = fitlab
    # 1. The lock lists walks (in the project) then rides (installed).
    p = run("mes", "apps", "sync", "--project-dir", str(root))
    assert p.returncode == 0, p.stdout + p.stderr
    lock = json.loads((root / "apps.lock.json").read_text())
    assert [(a["package"], a["in_project"]) for a in lock["apps"]] == [("fitlab_walks", True), ("fitlab_rides", False)]

    # 2. Data goes in through both apps.
    assert run("fitlab", "walks", "add", "2026-01-05", "4.2", "50", "--weather", "sunny").returncode == 0
    p = run("fitlab", "rides", "add", "2026-01-05", "21.5", "60", "--climb", "240", "--weather", "cloudy")
    assert p.returncode == 0, p.stderr

    # 3. A cross-app chain, and the project's setting for the rides job.
    p = run("mes", "--json", "jobs", "run", "fitlab-walks.rebuild", "--project-dir", str(root))
    assert p.returncode == 0, p.stdout + p.stderr
    assert [r["job"] for r in json.loads(p.stdout)["runs"]] == ["fitlab-walks.rebuild", "fitlab-rides.rebuild"]
    p = run("mes", "--json", "jobs", "list", "--project-dir", str(root))
    by = {j["id"]: j for j in json.loads(p.stdout)["jobs"]}
    assert by["fitlab-rides.rebuild"]["timeout"] == "5m"

    # 4. The shared tables, read through their views.
    dbf = base / "data" / "fitlab" / "fitlab.sqlite"
    conn = sqlite3.connect(dbf)
    try:
        acts = conn.execute("SELECT id, source FROM activities ORDER BY id").fetchall()
        days = conn.execute("SELECT date, walk_km, ride_km, weather, source FROM days").fetchall()
        weekly = conn.execute("SELECT count(*) FROM weekly").fetchone()[0]
    finally:
        conn.close()
    assert acts == [("ride-1", "fitlab-rides"), ("walk-1", "fitlab-walks")]
    assert days == [("2026-01-05", 4.2, 21.5, "cloudy", "fitlab-rides")]
    assert weekly == 2
    assert run("mes", "jobs", "run", "fitlab-walks.rebuild", "--no-chain", "--project-dir", str(root)).returncode == 0
    conn = sqlite3.connect(dbf)
    try:
        assert conn.execute("SELECT count(*) FROM days__rows WHERE source = 'fitlab-rides'").fetchone() == (1,)
    finally:
        conn.close()
    p = run("mes", "--json", "apps", "check", "--project-dir", str(root))
    assert p.returncode == 0, p.stdout

    # 8. The project command.
    p = run("fitlab", "--json", "describe")
    assert p.returncode == 0 and [a["name"] for a in json.loads(p.stdout)["apps"]] == ["fitlab-walks", "fitlab-rides"]
    p = run("fitlab", "list")
    assert p.returncode == 2 and "walks" in p.stderr and "rides" in p.stderr
    assert run("fitlab", "walks", "add", "2026-01-06", "1", "10", allow="read").returncode == 4

    # 9. The gate, on a personal machine.
    assert run("mes", "projects", "add", str(root)).returncode == 0

    def gate(request):
        e = {"SSH_ORIGINAL_COMMAND": json.dumps(request)}
        return subprocess.run([str(root / ".venv" / "bin" / "mes"), "gate", "--project", "fitlab", "--allow", "read"],
                              cwd=root, env={**os.environ, **{k: str(v) for k, v in {
                                  "MES_DATA_HOME": base / "data", "MES_STATE_HOME": base / "state",
                                  "MES_CONFIG": base / "config.toml"}.items()}, **e},
                              capture_output=True, text=True, timeout=120)

    p = gate(["fitlab", "rides", "list", "--json"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads(p.stdout)[0]["km"] == 21.5
    assert gate(["fitlab-rides", "add", "2026-01-06", "10", "30", "--json"]).returncode == 4
    p = gate(["fitlab", "describe", "--json"])
    assert p.returncode == 0 and json.loads(p.stdout)["project"] == "fitlab"
