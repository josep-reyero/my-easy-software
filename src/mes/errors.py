"""Error types and exit codes shared by mes and every app built on it.

Every error a person or an agent can see is a ``MesError``. It carries three
plain-language parts, so the message always says what happened, why it matters
and what to do next:

- ``message``: what happened and why it matters.
- ``hint``: what to do next (may be ``None``).
- ``code``: a short stable machine word, for example ``"not_found"``.

Public API
----------
``ExitCode``
    ``IntEnum`` of the fixed exit codes: ``OK=0``, ``ERROR=1``, ``USAGE=2``,
    ``NOT_FOUND=3``, ``NOT_ALLOWED=4``, ``REFUSED=5``, ``NEEDS_HUMAN=6``,
    ``TEMPORARY=75``.

``MesError(message, *, hint=None, code=None, detail=None, detail_label=None)``
    Base class (exit code 1, code ``"error"``). ``err.exit_code``,
    ``err.code``, ``err.message``, ``err.hint``, ``err.detail`` (optional
    technical text), ``err.detail_label`` (when set, text output always shows the
    detail under that heading). ``err.to_dict()`` returns
    ``{"error": {"code", "message", "hint"[, "detail"]}}``.

Subclasses, each with its own exit code and default ``code``:

- ``UsageError``        exit 2,  ``"usage"``: the command line was not understood.
- ``NotFound``          exit 3,  ``"not_found"``: the thing asked for does not exist.
- ``NotAllowed``        exit 4,  ``"not_allowed"``: the caller's allowed level is too low.
- ``Refused``           exit 5,  ``"refused"``: refused for safety.
- ``NeedsPerson(payload)`` exit 6: the command needs a person. ``payload`` is a
  ``needs_input`` / ``needs_human`` dict from ``mes.protocol``; ``to_dict()``
  returns the payload itself, so raising it prints the same thing as returning
  the payload.
- ``TemporaryFailure``  exit 75, ``"temporary"``: try again later.
- ``ManifestError``     exit 1,  ``"manifest_invalid"``: ``app.toml`` is wrong.
- ``ConfigError``       exit 1,  ``"config_invalid"``: the per-machine config is wrong.
- ``NotAvailableYet``   exit 1,  ``"not_available_yet"``: the feature is planned
  but not part of this version.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2
    NOT_FOUND = 3
    NOT_ALLOWED = 4
    REFUSED = 5
    NEEDS_HUMAN = 6
    TEMPORARY = 75


class MesError(Exception):
    """An error with a plain-language message, a hint and a fixed exit code."""

    exit_code: int = ExitCode.ERROR
    default_code: str = "error"

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        code: str | None = None,
        detail: str | None = None,
        detail_label: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code or self.default_code
        self.detail = detail
        # When set, text output always shows the detail under this heading (for example the
        # app's own error output for a failed job). Otherwise it is shown only with MES_DEBUG.
        self.detail_label = detail_label

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message, "hint": self.hint}
        if self.detail:
            body["detail"] = self.detail
        return {"error": body}


class UsageError(MesError):
    exit_code = ExitCode.USAGE
    default_code = "usage"


class NotFound(MesError):
    exit_code = ExitCode.NOT_FOUND
    default_code = "not_found"


class NotAllowed(MesError):
    exit_code = ExitCode.NOT_ALLOWED
    default_code = "not_allowed"


class Refused(MesError):
    exit_code = ExitCode.REFUSED
    default_code = "refused"


class TemporaryFailure(MesError):
    exit_code = ExitCode.TEMPORARY
    default_code = "temporary"


class ManifestError(MesError):
    default_code = "manifest_invalid"


class ConfigError(MesError):
    default_code = "config_invalid"


class NotAvailableYet(MesError):
    default_code = "not_available_yet"


class NeedsPerson(MesError):
    """Raise with a ``needs_input`` or ``needs_human`` payload (exit 6)."""

    exit_code = ExitCode.NEEDS_HUMAN
    default_code = "needs_human"

    def __init__(self, payload: dict[str, Any]) -> None:
        status = payload.get("status")
        if status not in ("needs_input", "needs_human"):
            raise ValueError("NeedsPerson needs a needs_input or needs_human payload")
        text = payload.get("question") or payload.get("action") or status
        super().__init__(str(text), code=status)
        self.payload = payload

    def to_dict(self) -> dict[str, Any]:
        return self.payload
