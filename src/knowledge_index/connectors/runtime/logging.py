"""Contextual logger for connector runs.

Connectors log at debug/warning constantly. Two properties matter for an on-prem legal
appliance:

1. Every line carries which source and which sync run produced it, so an operator can
   answer "why did the Munich office's OneDrive stall" from one log stream.
2. Nothing sensitive is logged. Connector code interpolates file names and URLs into
   messages, and those URLs can carry access tokens in query strings (Google download
   links, SharePoint pre-authenticated URLs), while token endpoints answer in JSON.

Redaction therefore covers all three ways text reaches a log line — the message, the
``%s`` arguments the handler substitutes in later, and the traceback the handler renders
from ``exc_info`` — because a credential that reaches a log file outlives the request
and is held far less carefully than the documents it unlocks.
"""

from __future__ import annotations

import logging
import re
import sys
import traceback

# Query parameters that carry credentials in the sources we support.
_SENSITIVE_QUERY_KEYS = (
    "access_token",
    "refresh_token",
    "code",
    "client_secret",
    "id_token",
    "sig",
    "signature",
    "tempauth",
    "authkey",
    "guestaccesstoken",
    "se",
    "sv",
)
# Keys that carry a credential in a JSON body. Token endpoints answer in JSON, so a
# connector logging a failed refresh response is one step from writing the firm's new
# refresh token — a durable credential — into the log file.
_SENSITIVE_JSON_KEYS = (
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "token",
    "secret",
    "password",
    "api_key",
)
_REDACTION_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(_SENSITIVE_QUERY_KEYS) + r")=([^&\s\"'>]+)"
)
_JSON_PATTERN = re.compile(
    r"(?i)(\"(?:" + "|".join(_SENSITIVE_JSON_KEYS) + r")\"\s*:\s*\")([^\"]+)(\")"
)
# The character class has to include +, / and =: a base64 token that stops matching
# part-way leaves its own tail in the log.
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-+/=~]{8,}")
_BASIC_PATTERN = re.compile(r"(?i)\b(basic\s+)[A-Za-z0-9+/=]{8,}")
# Credentials that are recognisable on their own, with no key next to them: Notion
# integration tokens, Slack tokens, Dropbox and GitHub tokens.
_STATIC_TOKEN_PATTERN = re.compile(
    r"\b(?:secret_ntn_|ntn_|xox[abeoprs]-|xapp-|sl\.|ghp_|gho_|ghu_|ghs_|github_pat_)"
    r"[A-Za-z0-9._\-]{6,}"
)


def redact(message: str) -> str:
    """Strip credential-shaped values out of a log message."""
    redacted = _REDACTION_PATTERN.sub(lambda m: f"{m.group(1)}=<redacted>", message)
    redacted = _JSON_PATTERN.sub(lambda m: f"{m.group(1)}<redacted>{m.group(3)}", redacted)
    redacted = _BEARER_PATTERN.sub(lambda m: f"{m.group(1)}<redacted>", redacted)
    redacted = _BASIC_PATTERN.sub(lambda m: f"{m.group(1)}<redacted>", redacted)
    return _STATIC_TOKEN_PATTERN.sub("<redacted>", redacted)


class _RedactedArg:
    """Defers redaction of a ``%s`` argument until logging interpolates it.

    ``redact`` runs on the message, and in ``logger.warning("failed for %s", url)`` the
    URL is not part of the message — it is substituted in later, by the handler.
    """

    __slots__ = ("_value",)

    def __init__(self, value: object) -> None:
        self._value = value

    def __str__(self) -> str:
        return redact(str(self._value))

    def __repr__(self) -> str:
        return redact(repr(self._value))

    def __format__(self, spec: str) -> str:
        return redact(format(self._value, spec))


def _redact_args(args: tuple) -> tuple:
    """Wrap arguments that could carry a credential, leaving numerics alone."""
    redacted = []
    for arg in args:
        if isinstance(arg, str):
            redacted.append(redact(arg))
        elif isinstance(arg, (int, float, complex, bool)) or arg is None:
            redacted.append(arg)
        else:
            redacted.append(_RedactedArg(arg))
    return tuple(redacted)


def _format_exception(exc_info: object) -> str:
    """Render an exception the way logging would, so it can be redacted first."""
    if exc_info is True:
        exc_info = sys.exc_info()
    if isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if not isinstance(exc_info, tuple) or len(exc_info) != 3 or exc_info[0] is None:
        return ""
    return "".join(traceback.format_exception(*exc_info)).rstrip()


class ContextualLogger:
    """Logger façade that prefixes source/run context and redacts secrets."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        source: str = "",
        run_id: str = "",
    ) -> None:
        self._logger = logger or logging.getLogger("knowledge_index.connectors")
        self.source = source
        self.run_id = run_id

    def bind(self, **context: str) -> ContextualLogger:
        """Return a logger with additional/overridden context."""
        return ContextualLogger(
            self._logger,
            source=context.get("source", self.source),
            run_id=context.get("run_id", self.run_id),
        )

    def _format(self, message: object) -> str:
        parts = [part for part in (self.source, self.run_id) if part]
        prefix = f"[{' '.join(parts)}] " if parts else ""
        return prefix + redact(str(message))

    def _emit(self, level: int, message: object, args: tuple, kwargs: dict) -> None:
        text = self._format(message)
        # A traceback is rendered by the handler, long after redact() has run, and an
        # httpx error carries the full request URL in its message — query string
        # included. Render it here so it can be redacted, and hand logging plain text.
        exc_info = kwargs.pop("exc_info", None)
        if exc_info:
            formatted = _format_exception(exc_info)
            if formatted:
                text = f"{text}\n{redact(formatted)}"
        self._logger.log(level, text, *_redact_args(args), **kwargs)

    def debug(self, message: object, *args, **kwargs) -> None:
        self._emit(logging.DEBUG, message, args, kwargs)

    def info(self, message: object, *args, **kwargs) -> None:
        self._emit(logging.INFO, message, args, kwargs)

    def warning(self, message: object, *args, **kwargs) -> None:
        self._emit(logging.WARNING, message, args, kwargs)

    def error(self, message: object, *args, **kwargs) -> None:
        self._emit(logging.ERROR, message, args, kwargs)

    def exception(self, message: object, *args, **kwargs) -> None:
        kwargs.setdefault("exc_info", True)
        self._emit(logging.ERROR, message, args, kwargs)

    def critical(self, message: object, *args, **kwargs) -> None:
        self._emit(logging.CRITICAL, message, args, kwargs)


# Module-level default, for helpers that log without an injected logger.
logger = ContextualLogger()
