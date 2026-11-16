"""The agent protocol: what commands return and how it is printed.

A command (in mes itself or in an app) returns plain data. This module builds
the special results that ask for a person, builds checklists, maps any result
to its exit code and prints it, as JSON with ``--json`` or as readable text
without.

Payload builders (they validate their input and raise ``ValueError`` on a
programming mistake)
-----------------------------------------------------------------------------
``option(value, label, meaning) -> dict``
    One choice for ``needs_input``.

``needs_input(id, question, why, options, recommended, rerun) -> dict``
    ``{"status": "needs_input", "id", "question", "why", "options", "recommended",
    "rerun"}``. ``recommended`` must be one of the option values. Exit code 6.

``needs_human(id, action, why, rerun, url=None) -> dict``
    ``{"status": "needs_human", "id", "action", "why", "url", "rerun"}``. Exit code 6.

``item(id, title, status, why, fix=None, needs_human=False, detail=None) -> dict``
    One checklist entry; ``status`` is one of ``STATUSES``
    (``"done" | "todo" | "blocked" | "skipped"``).

``checklist(items) -> dict``
    ``{"ok": bool, "items": [...]}``; ``ok`` is true when every item is
    ``done`` or ``skipped``.

Inspecting results
------------------
``is_needs(result) -> bool``      a needs_input / needs_human payload.
``is_checklist(result) -> bool``  a checklist.
``exit_code_for(result) -> int``
    6 for needs payloads; for a checklist 0 when ok, 6 when an unfinished item
    needs the human, otherwise 1; 0 for anything else.

Printing
--------
``pop_json_flag(argv) -> (argv_without_json, json_mode)``
    Removes every ``--json`` before a ``--`` separator, so ``--json`` works
    before or after the command name.
``to_json(data) -> str``      JSON text; Paths, dates, sets and dataclasses are converted.
``format_text(data) -> str``  readable text: tables for lists of rows,
    ``key: value`` lines for dicts, special layouts for protocol payloads.
``emit(result, *, json_mode, out=None) -> int``
    Prints a result (stdout) and returns its exit code.
``emit_error(err, *, json_mode, out=None, err_out=None) -> int``
    Prints a ``MesError``: JSON on stdout with ``--json``, plain text on stderr
    otherwise. Returns its exit code.
``run_and_emit(fn, *, json_mode) -> int``
    Calls ``fn()`` and emits its result; turns ``MesError`` into an error
    output, ``KeyboardInterrupt`` into exit 130 and any other exception into a
    plain "unexpected problem" error (see ``internal_error``).
``internal_error(exc, message) -> MesError``
    The exception text is only attached when ``MES_DEBUG`` is set and the
    request did not come through the gate.
``safe_text(text) -> str``
    Terminal control characters (other than tab and newline) shown as ``\\xNN``.
    Every text-mode output goes through it.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import re
import sys
import traceback
from collections.abc import Callable, Iterable, Sequence
from pathlib import PurePath
from typing import Any, TextIO

from mes.errors import ExitCode, MesError, NeedsPerson

STATUSES = ("done", "todo", "blocked", "skipped")
NEEDS_STATUSES = ("needs_input", "needs_human")


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


# --------------------------------------------------------------------------- builders


def option(value: str, label: str, meaning: str) -> dict[str, str]:
    return {
        "value": _text("value", value),
        "label": _text("label", label),
        "meaning": _text("meaning", meaning),
    }


def needs_input(
    id: str,
    question: str,
    why: str,
    options: Sequence[dict[str, str]],
    recommended: str,
    rerun: str,
) -> dict[str, Any]:
    opts = [dict(o) for o in options]
    if not opts:
        raise ValueError("needs_input needs at least one option")
    for o in opts:
        option(o.get("value"), o.get("label"), o.get("meaning"))  # type: ignore[arg-type]
    if recommended not in {o["value"] for o in opts}:
        raise ValueError("recommended must be one of the option values")
    return {
        "status": "needs_input",
        "id": _text("id", id),
        "question": _text("question", question),
        "why": _text("why", why),
        "options": opts,
        "recommended": recommended,
        "rerun": _text("rerun", rerun),
    }


def needs_human(id: str, action: str, why: str, rerun: str, url: str | None = None) -> dict[str, Any]:
    if url is not None:
        _text("url", url)
    return {
        "status": "needs_human",
        "id": _text("id", id),
        "action": _text("action", action),
        "why": _text("why", why),
        "url": url,
        "rerun": _text("rerun", rerun),
    }


def item(
    id: str,
    title: str,
    status: str,
    why: str,
    fix: str | None = None,
    needs_human: bool = False,
    detail: str | None = None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    return {
        "id": _text("id", id),
        "title": _text("title", title),
        "status": status,
        "why": _text("why", why),
        "fix": fix,
        "needs_human": bool(needs_human),
        "detail": detail,
    }


def checklist(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    entries = list(items)
    for e in entries:
        if e.get("status") not in STATUSES:
            raise ValueError("every checklist item needs a valid status")
    return {"ok": all(e["status"] in ("done", "skipped") for e in entries), "items": entries}


# --------------------------------------------------------------------------- inspecting


def is_needs(result: Any) -> bool:
    return isinstance(result, dict) and result.get("status") in NEEDS_STATUSES


def is_checklist(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and isinstance(result.get("ok"), bool)
        and isinstance(result.get("items"), list)
        and all(isinstance(i, dict) and "status" in i for i in result["items"])
    )


def exit_code_for(result: Any) -> int:
    if is_needs(result):
        return ExitCode.NEEDS_HUMAN
    if is_checklist(result):
        if result["ok"]:
            return ExitCode.OK
        unfinished = [i for i in result["items"] if i.get("status") not in ("done", "skipped")]
        if any(i.get("needs_human") for i in unfinished):
            return ExitCode.NEEDS_HUMAN
        return ExitCode.ERROR
    return ExitCode.OK


# --------------------------------------------------------------------------- printing


def pop_json_flag(argv: Sequence[str]) -> tuple[list[str], bool]:
    out: list[str] = []
    found = False
    args = list(argv)
    for i, a in enumerate(args):
        if a == "--":
            out.extend(args[i:])
            break
        if a == "--json":
            found = True
            continue
        out.append(a)
    return out, found


def _json_default(o: Any) -> Any:
    if isinstance(o, PurePath):
        return str(o)
    if isinstance(o, (_dt.datetime, _dt.date, _dt.time)):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    if isinstance(o, tuple):
        return list(o)
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if hasattr(o, "to_dict"):
        return o.to_dict()
    raise TypeError(f"cannot turn {type(o).__name__} into JSON")


# Characters that are invisible or change how text around them is shown (C1 controls, bidi
# overrides and isolates, line and paragraph separators): always written as \uXXXX escapes.
_JSON_ESCAPE = re.compile("[\u0080-\u009f\u061c\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")


def to_json(data: Any) -> str:
    text = json.dumps(data, default=_json_default, ensure_ascii=False, indent=2)
    return _JSON_ESCAPE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def _scalar(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=_json_default, ensure_ascii=False)
    return str(v)


def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, (str, int, float, bool, PurePath, _dt.date))


def _table(rows: list[dict[str, Any]], max_width: int = 40) -> str:
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)

    def cell(v: Any) -> str:
        s = _scalar(v).replace("\n", " ")
        return s if len(s) <= max_width else s[: max_width - 3] + "..."

    cells = [[cell(r.get(c)) for c in cols] for r in rows]
    widths = [max([len(c)] + [len(row[i]) for row in cells]) for i, c in enumerate(cols)]
    line = lambda vals: "  ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()  # noqa: E731
    out = [line(cols), line(["-" * w for w in widths])]
    out += [line(row) for row in cells]
    return "\n".join(out)


def _render_needs(r: dict[str, Any]) -> str:
    if r["status"] == "needs_input":
        lines = [f"A decision is needed: {r.get('question')}", f"Why: {r.get('why')}", "Options:"]
        for o in r.get("options") or []:
            mark = "  (recommended)" if o.get("value") == r.get("recommended") else ""
            lines.append(f"  - {o.get('label')} [{o.get('value')}]: {o.get('meaning')}{mark}")
        lines.append(f"To continue with your answer, run: {r.get('rerun')}")
        return "\n".join(lines)
    lines = [f"Something only you can do: {r.get('action')}", f"Why: {r.get('why')}"]
    if r.get("url"):
        lines.append(f"Open: {r['url']}")
    lines.append(f"When you have done it, run: {r.get('rerun')}")
    return "\n".join(lines)


def _render_checklist(r: dict[str, Any]) -> str:
    lines = []
    skipped = False
    for i in r["items"]:
        status = i.get("status", "?")
        line = f"[{status}] {i.get('title') or i.get('id')}"
        if status == "skipped":
            skipped = True
            # Nothing that affects safety is skipped silently: always say why and what is lost.
            if i.get("why"):
                line += f": {i['why']}"
        elif status != "done":
            if i.get("why"):
                line += f": {i['why']}"
            if i.get("fix"):
                line += f" Your assistant can do this for you after you say yes (command: {i['fix']})."
            if i.get("needs_human"):
                line += " (this step needs you)"
        lines.append(line)
    if not r["ok"]:
        lines.append("Not ready yet: see the items above that are not done.")
    elif skipped:
        lines.append("Ready, except for the steps marked [skipped] above: read what each of them means "
                     "for you.")
    else:
        lines.append("Everything is ready.")
    return "\n".join(lines)


def _render_dict(d: dict[str, Any], indent: int = 0) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    for k, v in d.items():
        if isinstance(v, dict) and v:
            lines.append(f"{pad}{k}:")
            lines.extend(_render_dict(v, indent + 1))
        elif isinstance(v, list) and v and not all(_is_scalar(x) for x in v):
            lines.append(f"{pad}{k}:")
            if all(isinstance(x, dict) and all(_is_scalar(y) for y in x.values()) for x in v):
                lines.extend(pad + "  " + ln for ln in _table(v).splitlines())
            else:
                lines.extend(pad + "  - " + _scalar(x) for x in v)
        elif isinstance(v, list):
            lines.append(f"{pad}{k}: {', '.join(_scalar(x) for x in v) if v else '-'}")
        else:
            lines.append(f"{pad}{k}: {_scalar(v)}")
    return lines


def format_text(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if is_needs(data):
        return _render_needs(data)
    if is_checklist(data):
        return _render_checklist(data)
    if isinstance(data, (list, tuple)):
        if not data:
            return "Nothing to show yet."
        rows = list(data)
        if all(isinstance(r, dict) and all(_is_scalar(v) for v in r.values()) for r in rows):
            return _table(rows)
        if all(isinstance(r, dict) for r in rows):
            return "\n\n".join("\n".join(_render_dict(r)) for r in rows)
        return "\n".join(_scalar(r) for r in rows)
    if isinstance(data, dict):
        return "\n".join(_render_dict(data)) if data else "Nothing to show yet."
    return _scalar(data)


# C0 and C1 control characters except tab and newline. App data shown on a terminal must not be
# able to move the cursor, clear the screen, change the window title or hide text.
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")


def safe_text(text: str) -> str:
    """``text`` with terminal control characters shown as ``\\xNN`` instead of acted on."""
    return _CONTROL_CHARS.sub(lambda m: f"\\x{ord(m.group()):02x}" if ord(m.group()) < 0x100
                              else f"\\u{ord(m.group()):04x}", text)


def emit(result: Any, *, json_mode: bool, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    if json_mode:
        out.write(to_json(result) + "\n")
    else:
        text = safe_text(format_text(result))
        if text:
            out.write(text.rstrip("\n") + "\n")
    out.flush()
    return int(exit_code_for(result))


def emit_error(
    err: MesError,
    *,
    json_mode: bool,
    out: TextIO | None = None,
    err_out: TextIO | None = None,
) -> int:
    if isinstance(err, NeedsPerson):
        emit(err.payload, json_mode=json_mode, out=out)
        return int(err.exit_code)
    if json_mode:
        out = out or sys.stdout
        out.write(to_json(err.to_dict()) + "\n")
        out.flush()
    else:
        err_out = err_out or sys.stderr
        err_out.write(safe_text(err.message.rstrip()) + "\n")
        if err.detail and err.detail_label:
            err_out.write(f"{err.detail_label}\n{safe_text(err.detail.rstrip())}\n")
        if err.hint:
            err_out.write(f"What to do: {safe_text(err.hint)}\n")
        if err.detail and not err.detail_label and os.environ.get("MES_DEBUG"):
            err_out.write(f"Technical detail: {safe_text(err.detail)}\n")
        err_out.flush()
    return int(err.exit_code)


def run_and_emit(fn: Callable[[], Any], *, json_mode: bool) -> int:
    try:
        result = fn()
    except MesError as e:
        return emit_error(e, json_mode=json_mode)
    except KeyboardInterrupt:
        sys.stderr.write("Stopped because you pressed Ctrl+C. Nothing else was done.\n")
        return 130
    except Exception as e:  # noqa: BLE001 - last line of defence, reported in plain words
        return emit_error(internal_error(e, "Something went wrong inside the command, so it stopped "
                                         "before finishing."), json_mode=json_mode)
    return emit(result, json_mode=json_mode)


def internal_error(e: BaseException, message: str) -> MesError:
    """A plain error for an unexpected exception.

    The exception's own text can hold file paths, addresses or other private values, so it is
    only included (as ``detail``, plus a traceback on stderr) when ``MES_DEBUG`` is set. Through
    the gate (``MES_VIA_GATE``) it is never included: the caller is not the owner.
    """
    via_gate = bool(os.environ.get("MES_VIA_GATE"))
    debug = bool(os.environ.get("MES_DEBUG")) and not via_gate
    if debug:
        traceback.print_exc()
    hint = (
        "Try again. If it keeps happening, ask the owner of this computer to look at it."
        if via_gate else
        "Run it again with MES_DEBUG=1 to see the technical details, and share them with whoever "
        "maintains this app."
    )
    return MesError(message, hint=hint, code="internal",
                    detail=f"{type(e).__name__}: {e}" if debug else None)
