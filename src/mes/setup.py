"""``mes setup`` and ``mes doctor``: get this computer ready, and check it later.

``mes doctor [--json]`` only looks: it runs every check in ``mes.checks`` and
returns the checklist. It never changes anything.

``mes setup [--json] [--answer id=value ...]`` runs the same checks and walks
the person through the next step, one at a time:

1. Tailscale missing and no choice made yet: returns ``needs_input`` (exit 6)
   with the "Keeping your apps private" question and the options ``install``
   (recommended) and ``local-only``. The agent asks the person and re-runs
   ``mes setup --answer tailscale=<value>``.
2. The choice is saved in the per-machine config file (``[network] mode =
   "tailscale" | "local-only"``), so later runs do not ask again.
3. Tailscale installed but not signed in: setup asks Tailscale for a sign-in
   link (``tailscale up`` with a time limit) and returns ``needs_human`` (exit 6)
   with that link.
4. Otherwise: the checklist (exit 0 when everything is ready, 1 when something
   is left for the agent to fix, 6 when what is left needs the person). Missing
   tools come with the install command from official package sources; mes
   itself never installs anything and never uses sudo.

Public API
----------
``ANSWERS``  ``{question_id: {value: saved_mode}}``: the answers setup accepts.
``parse_answers(raw) -> dict``  ``["tailscale=install"] -> {"tailscale": "install"}``.
``run_setup(answers=None, *, probe=None, json_mode=False) -> dict``
``run_doctor(*, probe=None) -> dict``
``toml_value(v) -> str``, ``toml_table(name, values) -> str``
    A tiny TOML writer for flat tables of strings, booleans and integers.
``set_config_table(name, values, path=None)``
    Replaces one ``[name]`` table in the config file and keeps everything else
    (other tables and comments). It checks the result and refuses to write
    when it cannot update the file safely.
``register(subparsers)``  adds ``setup`` and ``doctor`` to ``mes``.
"""

from __future__ import annotations

import argparse
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mes import checks, messages, paths, protocol
from mes.app import check_allowed
from mes.errors import ConfigError, UsageError

ANSWERS: dict[str, dict[str, str]] = {
    "tailscale": {"install": "tailscale", "local-only": "local-only"},
}

CONFIG_HEADER = (
    "# Settings for mes on this computer, written by \"mes setup\".\n"
    "# You do not need to edit this file: run \"mes setup\" to change your choices.\n"
)

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+\Z")
_HEADER = re.compile(r"^\s*\[")


# --------------------------------------------------------------------------- answers


def parse_answers(raw: Sequence[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in raw or []:
        key, sep, value = entry.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key or not value:
            raise UsageError(
                f'The answer "{entry}" is not in the form question=answer, so mes cannot use it.',
                hint="Write it like this: mes setup --answer tailscale=install",
            )
        if key not in ANSWERS:
            known = ", ".join(sorted(ANSWERS))
            raise UsageError(
                f'mes setup does not ask a question called "{key}", so this answer cannot be used.',
                hint=f"The questions it can ask are: {known}. Run mes setup without answers to see "
                "what it needs.",
            )
        if value not in ANSWERS[key]:
            allowed = " or ".join(ANSWERS[key])
            raise UsageError(
                f'"{value}" is not one of the answers to the {key} question.',
                hint=f"Answer with {allowed}, for example: mes setup --answer {key}="
                f"{next(iter(ANSWERS[key]))}",
            )
        out[key] = value
    return out


# --------------------------------------------------------------------------- tiny TOML writer


def _toml_string(s: str) -> str:
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return _toml_string(v)
    raise ValueError(f"the settings writer only stores text, yes/no and whole numbers, not {type(v).__name__}")


def toml_table(name: str, values: Mapping[str, Any]) -> str:
    if not _BARE_KEY.match(name):
        raise ValueError(f"table name {name!r} must use letters, digits, - and _ only")
    lines = [f"[{name}]"]
    for key, value in values.items():
        if not _BARE_KEY.match(key):
            raise ValueError(f"key {key!r} must use letters, digits, - and _ only")
        lines.append(f"{key} = {toml_value(value)}")
    return "\n".join(lines) + "\n"


def _replace_table(text: str, name: str, table: str) -> str:
    header = re.compile(rf"^\s*\[\s*{re.escape(name)}\s*\]\s*(#.*)?$")
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        if header.match(line):
            skipping = True
            continue
        if skipping and _HEADER.match(line):
            skipping = False
        if not skipping:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    body = "\n".join(kept)
    return (body + "\n\n" if body else "") + table


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _cannot_update(path: Path, why: str) -> ConfigError:
    return ConfigError(
        f"mes could not safely save your choice in the settings file for this computer ({path}), "
        "so it left the file exactly as it was.",
        hint="Move the file aside and run mes setup again to start fresh, or repair the file.",
        detail=why,
    )


def set_config_table(name: str, values: Mapping[str, Any], path: Path | None = None) -> Path:
    p = Path(path) if path else paths.config_path()
    exists = p.is_file()
    text = p.read_text(encoding="utf-8") if exists else CONFIG_HEADER
    try:
        old = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise _cannot_update(p, f"the file is not valid TOML: {e}") from None
    try:
        table = toml_table(name, values)
    except ValueError as e:
        raise _cannot_update(p, str(e)) from None
    new_text = _replace_table(text, name, table)
    try:
        new = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise _cannot_update(p, f"the updated file would not be valid TOML: {e}") from None
    expected = dict(old)
    expected[name] = dict(values)
    if new != expected:
        raise _cannot_update(p, f"the [{name}] settings are written in a way mes cannot update")
    if not exists or new_text != text:
        _write_private(p, new_text)
    return p


def _ensure_config(cfg: checks.ConfigState, mode: str | None) -> None:
    """Create the config file when missing, and save the network mode when it changed."""
    if mode is not None:
        current = checks.network_mode(cfg.data)
        if current != mode or not cfg.exists:
            existing = (cfg.data or {}).get("network")
            values = dict(existing) if isinstance(existing, dict) else {}
            values["mode"] = mode
            set_config_table("network", values, cfg.path)
    elif not cfg.exists:
        _write_private(cfg.path, CONFIG_HEADER)


# --------------------------------------------------------------------------- setup and doctor


def _tailscale_question(prefix: str) -> dict[str, Any]:
    options = [
        protocol.option(value, label, meaning)
        for value, (label, meaning) in messages.TAILSCALE_OPTIONS.items()
    ]
    return protocol.needs_input(
        id="tailscale",
        question=messages.TAILSCALE_OFFER,
        why=messages.TAILSCALE_OFFER_WHY,
        options=options,
        recommended="install",
        rerun=f"{prefix} setup --answer tailscale=<value>",
    )


def _sign_in(probe: checks.Probe, facts: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    ts: checks.TailscaleState = facts["tailscale"]
    plat: checks.Platform = facts["platform"]
    rerun = f"{prefix} setup"
    if ts.backend == "NeedsMachineAuth":
        return protocol.needs_human(
            "tailscale-approve", messages.APPROVE_ACTION, messages.APPROVE_WHY, rerun,
            url=messages.APPROVE_URL,
        )
    if ts.backend != "NeedsLogin":
        return None
    url = ts.auth_url or checks.start_sign_in(probe, ts, plat)
    if url:
        why = messages.SIGN_IN_WHY + messages.SIGN_IN_WHY_LINK
    elif plat.kind == "macos":
        why = messages.SIGN_IN_WHY + messages.SIGN_IN_WHY_NO_LINK_MAC
    else:
        why = messages.SIGN_IN_WHY + messages.SIGN_IN_WHY_NO_LINK_LINUX
    return protocol.needs_human("tailscale-sign-in", messages.SIGN_IN_ACTION, why, rerun, url=url)


def run_setup(
    answers: Mapping[str, str] | None = None,
    *,
    probe: checks.Probe | None = None,
    json_mode: bool = False,
) -> dict[str, Any]:
    probe = probe or checks.Probe()
    prefix = "mes --json" if json_mode else "mes"
    facts = checks.gather(probe)
    cfg: checks.ConfigState = facts["config"]
    if cfg.data is None:
        raise ConfigError(
            f"The settings file for this computer ({cfg.path}) is damaged, so mes setup cannot "
            "read or save your choices.",
            hint="Repair the line named below, or move the file aside and run mes setup again to "
            "start fresh.",
            detail=cfg.error,
        )
    plat: checks.Platform = facts["platform"]
    ts: checks.TailscaleState = facts["tailscale"]

    mode = facts["mode"]
    if answers and "tailscale" in answers:
        mode = ANSWERS["tailscale"][answers["tailscale"]]

    if plat.supported:
        if mode is None and not ts.installed:
            return _tailscale_question(prefix)
        if mode is None:
            mode = "tailscale"  # already installed: keep using it
    _ensure_config(cfg, mode)
    facts["config"] = checks.config_state(cfg.path)

    if plat.supported and mode == "tailscale" and ts.installed:
        person = _sign_in(probe, facts, prefix)
        if person:
            return person
    return protocol.checklist(checks.build_items(probe, facts, mode))


def run_doctor(*, probe: checks.Probe | None = None) -> dict[str, Any]:
    items, _ = checks.run_checks(probe or checks.Probe())
    return protocol.checklist(items)


# --------------------------------------------------------------------------- command line


def _handle_setup(args: argparse.Namespace) -> Any:
    check_allowed("admin")  # it saves choices in this computer's settings file
    return run_setup(parse_answers(args.answer), json_mode=bool(getattr(args, "json", False)))


def _handle_doctor(args: argparse.Namespace) -> Any:
    check_allowed("read")
    return run_doctor()


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "setup",
        help="Get this computer ready for mes apps, one step at a time.",
        description="Check this computer and walk through the next setup step. It never installs "
        "anything itself: it says which command to run, and asks when a decision or the person "
        "is needed (exit code 6).",
    )
    p.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="ID=VALUE",
        help="answer a question setup asked, for example tailscale=install or tailscale=local-only",
    )
    p.set_defaults(func=_handle_setup)

    d = subparsers.add_parser(
        "doctor",
        help="Check that this computer is still ready for mes apps (changes nothing).",
        description="Run every check and list what is done, what is left and how to fix it. "
        "Nothing is changed.",
    )
    d.set_defaults(func=_handle_doctor)
