"""``mes dev``: run a project on this computer and show its dashboards, with one link.

1. Runs ``mes apps sync`` (finds every app and writes ``apps.lock.json``, which the dashboards
   read), then checks the project; it needs dashboards (``[ui]``, ``ui/dashboards/`` or an app's).
2. When the project already has its shared database (``<project>.sqlite``), builds the dashboard
   data (``easy-ui data build``); otherwise the preview uses the made-up data in ``ui/fixtures/``.
3. Starts ``easy-ui preview`` (only reachable from this computer, no password, nothing
   published) and prints the one link it gives.
4. Watches the files: a change to ``project.toml`` or an ``app.toml`` runs ``mes apps sync`` again
   and reports the result; a change to the shared database rebuilds the dashboard data; dashboard
   and sections files are reloaded by the preview itself. Stops with Ctrl-C (or after ``--stop-after`` seconds) and stops the preview too.

With ``--json``, stdout holds exactly one JSON object, printed when the link is ready:
``{"project", "url", "data", "watching"}``. Later events go to stderr as plain lines.

Public API
----------
``URL_RE``  the local link form looked for in the preview's output.
``run_dev(project_dir=None, *, port=None, stop_after=None, json_mode=False, out=None, err=None) -> int``
``register(subparsers)``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, TextIO

from mes import manifest as manifest_mod
from mes import apps, paths, protocol, siblings
from mes.app import check_allowed
from mes.errors import MesError, NotFound, TemporaryFailure, UsageError

URL_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?/[^\s\"'<>]*")
URL_WAIT = 60.0
POLL = 1.0


def _mtimes(files: list[Path]) -> dict[Path, float]:
    out = {}
    for f in files:
        try:
            out[f] = f.stat().st_mtime_ns
        except OSError:
            out[f] = -1
    return out


def _watched(root: Path, project: manifest_mod.Project | None = None) -> list[Path]:
    files = [root / manifest_mod.MANIFEST_FILE]
    for sub in ("dashboards", "sections"):
        folder = root / "ui" / sub
        if folder.is_dir():
            files += sorted(folder.glob("*.yaml")) + sorted(folder.glob("*.yml"))
    for spec in (project.apps.values() if project else ()):
        if spec.folder is None:
            continue
        files.append(spec.folder / manifest_mod.APP_FILE)
        folder = spec.folder / "dashboards"
        if folder.is_dir():
            files += sorted(folder.glob("*.yaml"))
    return list(dict.fromkeys(files))


def _rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def _say(err: TextIO, text: str) -> None:
    err.write(protocol.safe_text(text.rstrip()) + "\n")
    err.flush()


def _build_data(root: Path) -> str | None:
    try:
        siblings.run_json(siblings.easy_ui_argv(root, "data", "build"), what="Building the dashboard data",
                          timeout=300)
    except MesError as e:
        return (e.detail or e.message).strip()
    return None


def run_dev(project_dir: str | None = None, *, port: int | None = None, stop_after: float | None = None,
            json_mode: bool = False, out: TextIO | None = None, err: TextIO | None = None) -> int:
    from mes.jobs import project_root

    out = out or sys.stdout
    err = err or sys.stderr
    root = project_root(project_dir)
    apps.sync(root)
    project = manifest_mod.load(root)
    if not project.has_dashboards:
        raise UsageError(
            f'The project "{project.name}" has no dashboards: no [ui] section in project.toml, nothing in '
            "ui/dashboards/ and no app ships one.",
            hint='Add for example:\n[ui]\ndashboards = ["overview"]\nand a dashboard in ui/dashboards/overview.yaml '
            "(see: mes guide dashboards).",
        )
    easy_ui = siblings.require("easy-ui", "Showing the dashboards")
    data_file: Path | None = paths.project_data_dir(project.name) / project.db_file
    has_data = bool(data_file and data_file.is_file())
    if has_data:
        problem = _build_data(root)
        if problem:
            _say(err, f"The dashboard data could not be built, so the preview uses the sample data for now: {problem}")
    argv = [easy_ui, "--project-dir", str(root), "preview"] + (["--port", str(port)] if port else [])
    try:
        proc = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                start_new_session=True)
    except OSError as e:
        raise MesError("The dashboard preview (easy-ui preview) could not be started.",
                       hint="Check that easy-ui is installed correctly.", code="preview_failed", detail=str(e)) from None

    found: dict[str, str] = {}
    tail: list[str] = []
    ready = threading.Event()

    def reader(stream: Any) -> None:
        for line in stream:
            if len(tail) < 200:
                tail.append(line.rstrip())
            m = URL_RE.search(line)
            if m and "url" not in found:
                found["url"] = m.group(0).rstrip(".,)")
                ready.set()
        stream.close()

    threads = [threading.Thread(target=reader, args=(s,), daemon=True) for s in (proc.stdout, proc.stderr)]
    for t in threads:
        t.start()
    stop = threading.Event()

    def on_signal(signum: int, frame: Any) -> None:
        stop.set()

    try:
        old_term = signal.signal(signal.SIGTERM, on_signal)
    except ValueError:  # not the main thread: Ctrl-C and stop_after still work
        old_term = None
    try:
        deadline = time.monotonic() + URL_WAIT
        while not ready.is_set():
            if proc.poll() is not None or time.monotonic() > deadline:
                for t in threads:
                    t.join(timeout=2)
                detail = "\n".join(tail[-20:]) or None
                if proc.poll() is None:
                    raise TemporaryFailure("The dashboard preview did not give a link in time, so mes stopped it.",
                                           hint="Try again; if it keeps happening, run: easy-ui preview",
                                           code="preview_failed")
                raise MesError("The dashboard preview stopped before it gave a link.",
                               hint="See what it said below. 'easy-ui validate' explains mistakes in dashboards.",
                               code="preview_failed", detail=detail, detail_label="What easy-ui said:")
            ready.wait(0.2)
        url = found["url"]
        info = {"project": project.name, "url": url,
                "data": "the project's own data" if has_data else "the made-up sample data in ui/fixtures/",
                "watching": [_rel(p, root) for p in _watched(root, project)]}
        if json_mode:
            out.write(json.dumps(info) + "\n")
        else:
            out.write(f"Open this link in a browser on this computer: {url}\n"
                      f"It shows {info['data']} and updates when you save a file. Stop with Ctrl-C.\n")
        out.flush()
        seen = _mtimes(_watched(root, project))
        data_seen = _mtimes([data_file]) if data_file else {}
        started = time.monotonic()
        while not stop.is_set():
            if stop_after is not None and time.monotonic() - started >= stop_after:
                break
            if proc.poll() is not None:
                _say(err, "The dashboard preview stopped by itself.")
                return proc.returncode or 1
            stop.wait(POLL)
            now = _mtimes(_watched(root, project))
            for f, t in now.items():
                if seen.get(f) == t:
                    continue
                name = _rel(f, root)
                if f.name in (manifest_mod.MANIFEST_FILE, manifest_mod.APP_FILE):
                    try:
                        apps.sync(root)
                        project = manifest_mod.load(root)
                        _say(err, f"{name} changed and is still correct (the list of apps was written again). If you "
                             "changed [ui] or the apps, stop and start mes dev again to use the new settings.")
                    except MesError as e:
                        _say(err, f"{name} changed and now has a problem: {e.message} {e.hint or ''}")
                else:
                    _say(err, f"Saved {name}; the open page updates by itself (or lists what to fix).")
            seen = now
            if data_file:
                current = _mtimes([data_file])
                if current != data_seen:
                    data_seen = current
                    if data_file.is_file():
                        problem = _build_data(root)
                        _say(err, "The project's data changed, so the dashboard data was rebuilt." if not problem
                             else f"The project's data changed, but the dashboard data could not be rebuilt: {problem}")
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait()


def _cmd_dev(args: argparse.Namespace) -> Any:
    check_allowed("write")  # it builds the dashboard data copy
    if args.stop_after is not None and args.stop_after <= 0:
        raise UsageError("--stop-after must be a positive number of seconds.", hint="For example: --stop-after 600")
    code = run_dev(args.project_dir, port=args.port, stop_after=args.stop_after, json_mode=args.json)
    raise SystemExit(code)


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "dev",
        help="Show the project's dashboards on this computer, with one link, updating as you edit.",
        description="Run the dashboard preview (easy-ui preview) for the project in this folder, print one link "
        "and watch the files. Only this computer can open the link. Stop with Ctrl-C.",
    )
    p.add_argument("--project-dir", default=None, metavar="DIR", help="the project's folder (default: here)")
    p.add_argument("--port", type=int, default=None, help="the port on 127.0.0.1 (default: a free one)")
    p.add_argument("--stop-after", type=float, default=None, metavar="SECONDS",
                   help="stop by itself after this many seconds")
    p.set_defaults(func=_cmd_dev)


__all__ = ["run_dev", "register", "URL_RE", "NotFound"]
