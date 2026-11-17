"""The ``mes`` command line.

``mes`` itself only builds the top-level parser. Each feature module adds its
own commands through a ``register(subparsers)`` function. The modules are
imported one by one; a module that is not installed is skipped, so ``mes``
keeps working with whatever parts are present.

Contract for a feature module (``mes.scaffold``, ``mes.dev``, ``mes.setup``, ``mes.apps``,
``mes.db``, ``mes.jobs``, ``mes.server``, ``mes.deploy``, ``mes.gate``, ``mes.expose``,
``mes.webhook``, ``mes.backup``, ``mes.guide``)
---------------------------------------------------------------------------
- Define ``register(subparsers)``. ``subparsers`` is the top-level
  ``add_subparsers(dest="command")`` action; call ``subparsers.add_parser(...)``.
  Its parsers are ``mes.app.ArgumentParser`` instances, so a usage error raises
  ``UsageError`` (exit 2) and respects ``--json``.
- Nested subcommands must use their own ``dest`` (e.g. ``dest="jobs_command"``),
  never ``"command"``.
- Every leaf parser sets ``set_defaults(func=handler)``. ``handler(args)``
  receives the ``argparse.Namespace`` with ``args.json`` (bool) already set,
  and returns the data: a dict, list, string, None, or a ``mes.protocol``
  payload (needs_input / needs_human -> exit 6, checklist -> 0 / 1 / 6). Raise
  a ``mes.errors.MesError`` subclass for errors. Never print the result
  yourself and never prompt on the terminal.
- ``--json`` is removed from the command line before parsing, so it works in
  any position; do not add your own ``--json`` argument.

Public API
----------
``COMMAND_MODULES``  the module names imported, in order.
``NOT_AVAILABLE``    the planned commands that only explain they are not available yet.
``build_parser() -> (parser, loaded_modules)``
``run(argv=None) -> int``  run one command line and return the exit code.
``main(argv=None)``         console entry point: ``sys.exit(run(argv))``.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence
from typing import Any, NoReturn

from mes import __version__, protocol
from mes.app import ArgumentParser, parse_json_aware
from mes.errors import MesError, NotAvailableYet, UsageError

COMMAND_MODULES = (
    "mes.scaffold", "mes.dev", "mes.setup", "mes.apps", "mes.db", "mes.jobs", "mes.server", "mes.deploy", "mes.gate",
    "mes.expose", "mes.webhook", "mes.backup", "mes.guide",
)

# Planned commands that only explain they are not available yet: (help, message, hint).
NOT_AVAILABLE: dict[str, tuple[str, str, str]] = {}


def _not_available(name: str):
    _, message, hint = NOT_AVAILABLE[name]

    def handler(args: argparse.Namespace) -> Any:
        raise NotAvailableYet(message, hint=hint or None)

    return handler


def _warn(text: str) -> None:
    sys.stderr.write(text.rstrip() + "\n")


def build_parser() -> tuple[ArgumentParser, list[str]]:
    parser = ArgumentParser(
        prog="mes",
        description="Create, run, deploy and share small personal projects that keep your data yours. "
        "Add --json (anywhere) to get pure JSON on stdout, for AI agents.",
        epilog="Start with 'mes guide' for instructions written for AI agents.",
    )
    parser.add_argument("--json", action="store_true", help="print only JSON data on stdout")
    parser.add_argument("--version", action="version", version=f"mes {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    loaded: list[str] = []
    for modname in COMMAND_MODULES:
        try:
            module = importlib.import_module(modname)
        except ModuleNotFoundError as e:
            if e.name == modname:
                continue  # that part of mes is not installed yet
            _warn(f"Note: part of mes ({modname}) could not be loaded, so its commands are missing. "
                  f"Reinstalling mes usually fixes this. Technical detail: {e}")
            continue
        except ImportError as e:
            _warn(f"Note: part of mes ({modname}) could not be loaded, so its commands are missing. "
                  f"Reinstalling mes usually fixes this. Technical detail: {e}")
            continue
        register = getattr(module, "register", None)
        if not callable(register):
            continue
        try:
            register(sub)
        except Exception as e:  # noqa: BLE001 - one broken part must not break the others
            _warn(f"Note: the commands from {modname} could not be added, so they are missing. "
                  f"Technical detail: {type(e).__name__}: {e}")
            continue
        loaded.append(modname)

    for name, (help_text, _, _) in NOT_AVAILABLE.items():
        if name in sub.choices:
            continue
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=_not_available(name), _accept_extra=True)
    return parser, loaded


def _command_list(sub: Any) -> list[dict[str, Any]]:
    return [{"name": a.dest, "help": a.help} for a in sub._choices_actions]


def _parse_error(args: Sequence[str], e: MesError) -> MesError:
    """A command line that cannot be read. For ``mes jobs should-run`` (systemd's
    ``ExecCondition=``) that must not look like "skip this run", so it exits 255 instead."""
    if list(args[:2]) == ["jobs", "should-run"]:
        from mes.jobs import _should_run_broken

        return _should_run_broken("its command line was not understood: " + e.message.rstrip("."))
    return e


def run(argv: Sequence[str] | None = None) -> int:
    args, json_mode = protocol.pop_json_flag(sys.argv[1:] if argv is None else argv)
    if json_mode and args[:1] == ["--version"]:
        return protocol.emit({"mes_version": __version__}, json_mode=True)
    parser, _ = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    try:
        ns, extra, finished = parse_json_aware(parser, args, json_mode=json_mode, known=True)
        if finished is not None:  # --help, --version
            return finished
        assert ns is not None
        if extra and not getattr(ns, "_accept_extra", False):
            parser.error(f"unrecognized arguments: {' '.join(extra)}")
    except MesError as e:
        return protocol.emit_error(_parse_error(args, e), json_mode=json_mode)

    ns.json = json_mode
    func = getattr(ns, "func", None)
    if func is None:
        if not ns.command:
            if json_mode:
                return protocol.emit(
                    {"mes_version": __version__, "commands": _command_list(sub)}, json_mode=True
                )
            parser.print_help()
            return 0
        err = UsageError(
            f"'mes {ns.command}' needs one more word saying what to do.",
            hint=f"Run 'mes {ns.command} --help' to see the choices.",
        )
        return protocol.emit_error(err, json_mode=json_mode)
    return protocol.run_and_emit(lambda: func(ns), json_mode=json_mode)


def main(argv: Sequence[str] | None = None) -> NoReturn:
    sys.exit(run(argv))


if __name__ == "__main__":
    main()
