"""``mes guide [topic]``: short instructions for AI agents, shipped with mes.

The texts live in ``src/mes/guides/<topic>.md`` so they always match the
installed version. Skills stay thin and call ``mes guide <topic>``.

Public API
----------
``topics() -> list[dict]``
    ``[{"name": "setup", "summary": "..."}, ...]`` sorted by name. The summary is
    the first line of the file that is not a heading.
``read_topic(name) -> str``
    The text of one topic; raises ``NotFound`` (exit 3) for an unknown topic.
``register(subparsers)``
    Adds ``guide`` to the ``mes`` command line.
"""

from __future__ import annotations

import argparse
import re
from importlib import resources
from typing import Any

from mes.errors import NotFound

_TOPIC_RE = re.compile(r"^[a-z][a-z0-9-]*\Z")


def _dir():
    return resources.files("mes") / "guides"


def _summary(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s
    return ""


def topics() -> list[dict[str, str]]:
    out = []
    for entry in _dir().iterdir():
        if entry.name.endswith(".md"):
            out.append({"name": entry.name[:-3], "summary": _summary(entry.read_text(encoding="utf-8"))})
    return sorted(out, key=lambda t: t["name"])


def read_topic(name: str) -> str:
    names = [t["name"] for t in topics()]
    if not _TOPIC_RE.match(name or "") or name not in names:
        raise NotFound(
            f'There is no guide called "{name}".',
            hint=f"Available guides: {', '.join(names)}. Run: mes guide <topic>",
        )
    return (_dir() / f"{name}.md").read_text(encoding="utf-8")


def _handle(args: argparse.Namespace) -> Any:
    if not args.topic:
        items = topics()
        if args.json:
            return {"topics": items}
        lines = ["Guides for AI agents. Run: mes guide <topic>", ""]
        width = max((len(t["name"]) for t in items), default=0)
        lines += [f"  {t['name'].ljust(width)}  {t['summary']}" for t in items]
        return "\n".join(lines)
    text = read_topic(args.topic)
    if args.json:
        return {"topic": args.topic, "text": text}
    return text


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "guide",
        help="Short instructions for AI agents (topics: setup, new-app, jobs, gate, security).",
        description="Print instructions for AI agents. Without a topic, list the topics.",
    )
    p.add_argument("topic", nargs="?", help="the topic to show")
    p.set_defaults(func=_handle)
