"""The proof of the app model on examples/fitlab (docs/apps-contract.md, 9.7 and 11).

A copy of the example is installed with ``uv sync --offline`` (skipped when uv or its cache cannot
do that), then driven as real programs with throwaway per-machine folders: steps 1 to 4, 8 and 9
always; steps 5 to 7 (dashboards: compose, data build, validate, publish, export, overrides, the
project's theme and stylesheet) when easy-ui is installed (``MES_EASY_UI``, ``easy-ui`` on PATH, or
a my-easy-ui checkout next to this one), otherwise that test is skipped saying why. The journey
ends by removing an app from the project: its jobs, its dashboard sections and its rows go away.
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


def _easy_ui() -> str | None:
    """easy-ui as mes itself finds it, else the program of a my-easy-ui checkout next to this one."""
    from mes import siblings

    found = siblings.tool_path("easy-ui")
    if found:
        return found
    sibling = FRAMEWORK.parent / "my-easy-ui" / ".venv" / "bin" / "easy-ui"
    return str(sibling) if sibling.is_file() and os.access(sibling, os.X_OK) else None


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

    run.env = env  # type: ignore[attr-defined]
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
        assert conn.execute("SELECT count(*) FROM days__rows WHERE source = 'fitlab-rides'").fetchone()[0] >= 1
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


def test_dashboards_overrides_and_removing_an_app(fitlab):
    """Steps 5 to 7 with easy-ui, the project's look and override, then removing the rides app."""
    root, run, base = fitlab
    easy_ui = _easy_ui()
    if easy_ui is None:
        pytest.skip("easy-ui (my-easy-ui) is not installed, so the dashboard steps were not run "
                    "(set MES_EASY_UI to its program)")
    assert run("mes", "apps", "sync", "--project-dir", str(root)).returncode == 0
    for argv in (("fitlab", "walks", "add", "2026-01-05", "4.2", "50", "--weather", "sunny"),
                 ("fitlab", "rides", "add", "2026-01-12", "21.5", "60", "--climb", "240")):
        assert run(*argv).returncode == 0
    p = run("mes", "--json", "jobs", "run", "fitlab-walks.rebuild", "--project-dir", str(root))
    assert p.returncode == 0, p.stdout + p.stderr

    def ui(*argv, ok=True):
        p = subprocess.run([easy_ui, "--json", "--project-dir", str(root), *argv], cwd=root, env=run.env,
                           capture_output=True, text=True, timeout=180)
        if ok:
            assert p.returncode == 0, p.stdout + p.stderr
        return json.loads(p.stdout) if p.stdout.strip() else {}

    # 5. Composition: the project's override replaces the walks section, the rides section goes last.
    out = ui("compose")
    assert out["order"] == ["overview", "walks"]
    over = next(d for d in out["dashboards"] if d["slug"] == "overview")
    assert [(s["app"], s["id"], s["placed"], s["file"]) for s in over["sections"]] == [
        ("fitlab-walks", "walks", "after:activity", "ui/sections/fitlab-walks.overview.yaml"),
        ("fitlab-rides", "rides", "end", over["sections"][1]["file"])]
    # 6. The data copy holds exactly the allowlist; validate (with the look) and publish succeed.
    built = ui("data", "build")
    assert sorted(built["tables"]) == ["activities", "days", "walks_log", "weekly"]
    v = ui("validate")
    assert v["ok"] is True and not v.get("errors")
    pub = ui("publish")
    assert pub.get("styles"), pub  # the theme and the stylesheet are part of the version
    exported = ui("export", "--out", str(base / "export"))
    assert (base / "export" / "ui" / "styles" / "10-fitlab.css").is_file() and exported["dashboards"]
    # Both apps' rows reach one chart and one table of the overview.
    bundle = sqlite3.connect(built["path"])
    try:
        assert {r[0] for r in bundle.execute("SELECT source FROM activities")} == {"fitlab-walks", "fitlab-rides"}
    finally:
        bundle.close()
    # 7. sections: [] removes the rides section; a project file replaces the walks dashboard.
    (root / "ui" / "sections" / "fitlab-rides.overview.yaml").write_text("spec: easy-ui/sections-v1\nsections: []\n")
    (root / "ui" / "dashboards" / "walks.yaml").write_text(
        (root / "apps" / "fitlab_walks" / "dashboards" / "walks.yaml").read_text().replace("title: Walks",
                                                                                         "title: My walks"))
    out = ui("compose")
    over = next(d for d in out["dashboards"] if d["slug"] == "overview")
    assert [s["id"] for s in over["sections"]] == ["walks"]
    walks = next(d for d in out["dashboards"] if d["slug"] == "walks")
    assert walks["from"] == "project" and walks["replaces"] == "fitlab-walks"
    (root / "ui" / "sections" / "fitlab-rides.overview.yaml").unlink()
    (root / "ui" / "dashboards" / "walks.yaml").unlink()

    # Removing the rides app: its jobs, its section and its rows go; adding it back brings them back.
    text = (root / "project.toml").read_text()
    smaller = (text.replace('apps = ["fitlab_walks", "fitlab_rides"]', 'apps = ["fitlab_walks"]')
               .replace('prefer = ["fitlab-rides"]\n', "")
               .replace('[settings.fitlab-rides]\njobs.rebuild.timeout = "5m"\n', "")
               .replace('refresh_after = ["fitlab-rides.rebuild"]', 'refresh_after = ["fitlab-walks.rebuild"]'))
    (root / "project.toml").write_text(text.replace('apps = ["fitlab_walks", "fitlab_rides"]', 'apps = ["fitlab_walks"]'))
    p = run("mes", "apps", "sync", "--project-dir", str(root))
    assert p.returncode == 1 and "[settings.fitlab-rides]" in p.stderr and "remove these lines too" in p.stderr
    (root / "project.toml").write_text(smaller)
    assert run("mes", "apps", "sync", "--project-dir", str(root)).returncode == 0
    jobs = json.loads(run("mes", "--json", "jobs", "list", "--project-dir", str(root)).stdout)["jobs"]
    assert [j["id"] for j in jobs] == ["fitlab-walks.rebuild"]
    out = ui("compose")
    assert [s["app"] for s in next(d for d in out["dashboards"] if d["slug"] == "overview")["sections"]] == [
        "fitlab-walks"]
    built = ui("data", "build")
    bundle = sqlite3.connect(built["path"])
    try:
        assert {r[0] for r in bundle.execute("SELECT source FROM activities")} == {"fitlab-walks"}
    finally:
        bundle.close()
    dbf = base / "data" / "fitlab" / "fitlab.sqlite"
    conn = sqlite3.connect(dbf)
    try:  # the rows stay in the file until mes db check --prune
        assert conn.execute("SELECT count(*) FROM activities__rows WHERE source = 'fitlab-rides'").fetchone()[0] >= 1
    finally:
        conn.close()
    (root / "project.toml").write_text(text)
    assert run("mes", "apps", "sync", "--project-dir", str(root)).returncode == 0
    built = ui("data", "build")
    bundle = sqlite3.connect(built["path"])
    try:
        assert {r[0] for r in bundle.execute("SELECT source FROM activities")} == {"fitlab-walks", "fitlab-rides"}
    finally:
        bundle.close()
