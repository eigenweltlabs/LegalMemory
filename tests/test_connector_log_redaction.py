"""Credentials must not reach the appliance's log files.

A connector's logs are full of URLs and API responses, and both carry tokens: Google and
SharePoint hand back pre-authenticated download URLs with the credential in the query
string, and a token endpoint answers in JSON. A refresh token written to a log is a
durable key to the firm's entire document estate, held somewhere with far weaker
handling than the documents themselves.

Three paths reach a log line and each is covered here: the message, the ``%s``
arguments the handler substitutes in later, and the traceback it renders from
``exc_info``.
"""

from __future__ import annotations

import io
import logging

import httpx
import pytest

from knowledge_index.connectors.runtime.logging import ContextualLogger, redact

SECRET = "ya29.A0ARrdaM_THE_ACTUAL_SECRET"


@pytest.mark.parametrize(
    "message",
    [
        f"GET https://www.googleapis.com/drive/v3/files/1?alt=media&access_token={SECRET}",
        f"POST /token?client_id=x&client_secret={SECRET}",
        f"headers={{'Authorization': 'Bearer {SECRET}'}}",
        # Base64 tokens contain +, / and =; the character class must not stop at them.
        "Authorization: Bearer abc+def/ghi=jklmnop",
        f'{{"access_token": "{SECRET}", "refresh_token": "1//04REFRESH", "expires_in": 3599}}',
        '{"token": "xoxb-1-SLACKSECRET"}',
        f"https://contoso.sharepoint.com/x.docx?tempauth={SECRET}&guestaccesstoken=GUESTSECRET",
        f"grant_type=refresh_token&refresh_token={SECRET}&client_secret=CSECRET",
        "using token secret_ntn_abcdefghijklmnop",
        "Authorization: Basic Y2xpZW50aWQ6Y2xpZW50c2VjcmV0",
    ],
)
def test_credential_shapes_are_redacted(message):
    cleaned = redact(message)
    for secret in (
        SECRET,
        "1//04REFRESH",
        "xoxb-1-SLACKSECRET",
        "GUESTSECRET",
        "CSECRET",
        "secret_ntn_abcdefghijklmnop",
        "Y2xpZW50aWQ6Y2xpZW50c2VjcmV0",
        "abc+def/ghi=jklmnop",
    ):
        assert secret not in cleaned, f"{secret!r} survived redaction of {message!r}"


def test_ordinary_messages_are_left_alone():
    message = "synced 41 items from Mandate/M-2026-0042 in 3.2s"
    assert redact(message) == message


@pytest.fixture()
def captured():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    base = logging.getLogger("test.connector.redaction")
    base.handlers = [handler]
    base.setLevel(logging.DEBUG)
    base.propagate = False
    yield ContextualLogger(base, source="google_drive", run_id="r1"), stream
    base.handlers = []


def test_percent_style_arguments_are_redacted(captured):
    """The argument is substituted in by the handler, after the message is redacted."""
    logger, stream = captured
    logger.warning("download failed for %s", f"https://x/f?access_token={SECRET}")
    assert SECRET not in stream.getvalue()
    assert "download failed for" in stream.getvalue()


def test_object_arguments_are_redacted_when_interpolated(captured):
    logger, stream = captured
    error = httpx.HTTPStatusError(
        f"401 for url 'https://x/f?access_token={SECRET}'",
        request=httpx.Request("GET", "https://x/f"),
        response=httpx.Response(401),
    )
    logger.warning("download failed: %s", error)
    assert SECRET not in stream.getvalue()


def test_the_traceback_is_redacted_too(captured):
    """An httpx error carries the full request URL, query string included."""
    logger, stream = captured
    request = httpx.Request(
        "GET", f"https://www.googleapis.com/drive/v3/files/1?alt=media&access_token={SECRET}"
    )
    try:
        httpx.Response(401, request=request).raise_for_status()
    except httpx.HTTPStatusError:
        logger.exception("drive download failed")
    emitted = stream.getvalue()
    assert SECRET not in emitted, "the token reached the log through the traceback"
    # The traceback is still there — redaction must not cost the operator the diagnosis.
    assert "HTTPStatusError" in emitted
    assert "Traceback" in emitted


def test_numeric_arguments_still_format(captured):
    logger, stream = captured
    logger.info("synced %d items in %.1fs", 41, 3.25)
    assert "synced 41 items in 3.2s" in stream.getvalue()
