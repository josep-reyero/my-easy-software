"""The guides, the agent skills and the Claude Code plugin manifest stay consistent."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mes import guide
from mes.errors import NotFound

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "mes"
TOPICS = {t["name"] for t in guide.topics()}

NEEDED = {"setup", "new-project", "commands", "jobs", "dashboards", "server", "deploy", "gate", "share",
          "webhooks", "backup", "security", "troubleshoot", "apps", "add-app", "share-data", "contribute-panel"}
SKILLS = {"mes-setup", "mes-new-project", "mes-add-command", "mes-add-job", "mes-dashboard", "mes-deploy",
          "mes-share", "mes-troubleshoot", "mes-add-app", "mes-share-data", "mes-contribute-panel"}


def test_every_needed_guide_exists_with_a_summary():
    assert NEEDED <= TOPICS
    for t in guide.topics():
        assert t["summary"] and len(t["summary"]) < 200, t


def test_every_guide_mentioned_anywhere_exists():
    files = list(SRC.rglob("*.py")) + list(SRC.rglob("*.md")) + list(SRC.rglob("*.tmpl")) + \
        list((REPO / "skills").rglob("*.md")) + [REPO / "README.md"]
    for f in files:
        for topic in re.findall(r"mes guide ([a-z][a-z0-9-]*)", f.read_text(encoding="utf-8")):
            if topic in ("topic",):
                continue
            assert topic in TOPICS, f"{f.relative_to(REPO)} mentions 'mes guide {topic}'"


def test_unknown_topic():
    with pytest.raises(NotFound):
        guide.read_topic("../setup")
    with pytest.raises(NotFound):
        guide.read_topic("nope")


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "no frontmatter"
    out = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def test_skills_are_thin_and_delegate_to_guides():
    found = {p.parent.name for p in (REPO / "skills").glob("*/SKILL.md")}
    assert found == SKILLS
    for name in SKILLS:
        text = (REPO / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        meta = _frontmatter(text)
        assert meta["name"] == name and re.fullmatch(r"[a-z0-9-]{1,64}", name)
        assert 80 < len(meta["description"]) <= 1024 and "Use when" in meta["description"]
        assert re.search(r"`mes guide [a-z-]+`", text)
        assert len(text) < 1500, f"{name} is not thin"


def test_plugin_manifest():
    data = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert data["name"] == "my-easy-software" and data["skills"] == "./skills/"
    assert re.fullmatch(r"\d+\.\d+\.\d+", data["version"])
    text = json.dumps(data)
    assert "@" not in text and "/Users/" not in text and "/home/" not in text
