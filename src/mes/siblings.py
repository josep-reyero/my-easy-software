"""Calling the sibling tools, my-easy-ui (``easy-ui``) and my-bucket-storage (``mybucket``).

mes never imports them: it runs their command-line tools as subprocesses with argument lists
(never a shell) and reads their ``--json`` output. When a tool is not installed, mes says so in
plain words and goes on with what it can do.

Where the programs are: ``MES_EASY_UI`` / ``MES_MYBUCKET`` (full paths) when set, otherwise the
first ``easy-ui`` / ``mybucket`` on ``PATH``.

What mes assumes about their command lines (kept here, in one place):

- ``easy-ui --json --project-dir DIR <command> ...`` for every command; ``--json`` prints one
  JSON document on stdout, errors as ``{"error": {"code", "message", "hint"}}`` with the usual
  exit codes (0 ok, 2 usage, 3 not found, 5 refused, 6 needs a person, 75 retry).
- ``easy-ui ... units --render-only --exec PATH --root ROOT [--allow-host NAME ...]`` answers
  ``{"project", "accounts", "socket", "ui_dir", "folders": [{path, owner, group, mode, why}],
  "enable": ["mes-<p>-web.socket"], "written", "units": {"mes-<p>-web.socket": ..., "mes-<p>-web.service": ...}}``.
  The service runs as ``mes-<p>-web``; the socket defaults to ``/run/mes/<p>-web.sock``. easy-ui
  lists ``<root>/<p>`` as ``mes-<p>:mes-<p>-web 0710``; mes keeps it ``root:root 0711`` on purpose
  and makes ``ui``, ``ui/data``, ``ui/specs`` (``mes-<p>:mes-<p>-web 2750``) and ``auth``
  (beside ``ui``, ``mes-<p>-web 0700``) itself.
- ``easy-ui ... preview [--port N]`` prints a ``http://127.0.0.1:<port>/?token=...`` link and runs
  until stopped.
- ``easy-ui ... export --out DIR [--dashboard NAME ...] [--encrypt] [--working]`` creates DIR and
  answers ``{"files", "encrypted", "fragment" ("#k=<key>" in link mode, else null), "dashboards",
  "visible": [{slug, title, values, queries: [{name, columns, rows, truncated}]}], "bytes"}``. mes
  asks the person about ``visible`` and never sends ``fragment`` to a provider.
- ``easy-ui ... data build`` and ``easy-ui ... publish``: on a server with ``MES_ROOT=<root>``
  they read ``<root>/<p>/data`` and write ``<root>/<p>/ui``; on a personal computer they use the
  project's mes data folder (``[projects.<name>] data_dir`` overrides it).
- easy-ui has no webhook endpoint; webhooks are mes's own receiver (``mes webhook serve``).
- ``mybucket --json --store DIR snapshot DEST`` and ``mybucket --json --store DIR verify``.

Public API
----------
``tool_path(name) -> str | None``       ``name`` is ``"easy-ui"`` or ``"mybucket"``.
``missing(name, why) -> NotFound``       the plain "not installed" error.
``require(name, why) -> str``            the path, or raises ``missing``.
``run_json(argv, *, cwd=None, env=None, timeout=120, what) -> dict``
    Runs a tool with ``--json`` already in ``argv``; returns its JSON object, or raises a
    ``MesError`` with the tool's own message as detail.
``easy_ui_argv(project_dir, *args) -> list[str]``
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mes.errors import MesError, NeedsPerson, NotFound, Refused, TemporaryFailure, UsageError

TOOLS = {
    "easy-ui": ("MES_EASY_UI", "my-easy-ui", "dashboards"),
    "mybucket": ("MES_MYBUCKET", "my-bucket-storage", "storage buckets"),
}
MAX_OUTPUT = 32 * 1024 * 1024


def tool_path(name: str) -> str | None:
    env_name, _, _ = TOOLS[name]
    value = os.environ.get(env_name)
    if value:
        p = Path(value).expanduser()
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    return shutil.which(name)


def missing(name: str, why: str) -> NotFound:
    _, package, _ = TOOLS[name]
    return NotFound(
        f"{why} needs {name} (from {package}), which is not installed on this computer, so it was not done.",
        hint=f"Install {package} on this computer (with your yes, your assistant can do it), then run the "
        "command again. Everything else in mes keeps working without it.",
        code=f"{name.replace('-', '_')}_missing",
    )


def require(name: str, why: str) -> str:
    path = tool_path(name)
    if path is None:
        raise missing(name, why)
    return path


def easy_ui_argv(project_dir: str | os.PathLike[str], *args: str) -> list[str]:
    return [require("easy-ui", "This step"), "--json", "--project-dir", str(project_dir), *args]


def run_json(argv: Sequence[str], *, cwd: str | os.PathLike[str] | None = None,
             env: Mapping[str, str] | None = None, timeout: float = 120, what: str) -> dict[str, Any]:
    try:
        res = subprocess.run(list(argv), cwd=cwd, env=dict(env) if env is not None else None,
                             stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise MesError(f"{what} could not start the program it needs, so it was not done.",
                       hint="Check that the program is installed.", code="tool_failed") from None
    except subprocess.TimeoutExpired:
        raise TemporaryFailure(f"{what} took longer than {timeout:g} seconds, so it was stopped.",
                               hint="Try again later.") from None
    except OSError as e:
        raise MesError(f"{what} could not run the program it needs, so it was not done.",
                       hint="Check that the program is installed.", code="tool_failed", detail=str(e)) from None
    out = res.stdout[:MAX_OUTPUT]
    try:
        data = json.loads(out.decode("utf-8")) if out.strip() else None
    except (UnicodeDecodeError, ValueError):
        data = None
    if res.returncode == 6 and isinstance(data, dict) and data.get("status") in ("needs_input", "needs_human"):
        raise NeedsPerson(data)
    if res.returncode != 0 or not isinstance(data, dict) or "error" in data:
        said = data.get("error") if isinstance(data, dict) and isinstance(data.get("error"), dict) else {}
        message = str(said.get("message") or "").strip()
        hint = str(said.get("hint") or "").strip()
        detail = message + (f"\nIts advice: {hint}" if hint else "")
        if not detail:
            detail = res.stderr.decode("utf-8", "replace").strip()[-2000:] or f"exit code {res.returncode}"
        cls = {2: UsageError, 3: NotFound, 5: Refused, 75: TemporaryFailure}.get(res.returncode, MesError)
        err = cls(f"{what} did not work.", hint="See what the tool said below, fix that, and try again.",
                  code="tool_failed", detail=detail, detail_label="What it said:")
        err.tool_code = str(said.get("code") or "")[:64]  # the tool's own error code, for a better hint
        raise err
    return data
