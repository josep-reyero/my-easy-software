"""my-easy-software: the framework behind small personal apps, and the ``mes`` command.

Public API (re-exported here for apps)
--------------------------------------
``__version__``           the framework version (``"0.1.0"``).
``App``, ``Context``      the App SDK (see ``mes.app``).
``protocol``              needs_input / needs_human / checklist builders (see ``mes.protocol``).
Errors (see ``mes.errors``): ``MesError``, ``UsageError``, ``NotFound``, ``NotAllowed``,
``Refused``, ``NeedsPerson``, ``TemporaryFailure``, ``ExitCode``.

Modules: ``mes.app`` (App SDK), ``mes.protocol`` (agent protocol and output),
``mes.manifest`` (app.toml), ``mes.paths`` (per-machine folders and config),
``mes.errors`` (errors and exit codes), ``mes.cli`` (the ``mes`` command),
``mes.guide`` (``mes guide``).
"""

__version__ = "0.1.0"

from mes import protocol  # noqa: E402
from mes.app import App, Context  # noqa: E402
from mes.errors import (  # noqa: E402
    ExitCode,
    MesError,
    NeedsPerson,
    NotAllowed,
    NotFound,
    Refused,
    TemporaryFailure,
    UsageError,
)

__all__ = [
    "__version__",
    "App",
    "Context",
    "protocol",
    "ExitCode",
    "MesError",
    "NeedsPerson",
    "NotAllowed",
    "NotFound",
    "Refused",
    "TemporaryFailure",
    "UsageError",
]
