"""Remote-connector audit trail: every tool call leaves a row in the POS's
permanent Connector Audit Log (ffl_core.api.connector.log_connector_call),
recorded as the calling user, through the connector they authorized. Reads included.

Written BEFORE the tool runs; if that fails the tool does not run, so nothing
happens unrecorded. Closed after the call (Success / Failed). A failure to close
leaves the row at "Started" (outcome unknown) and never turns a finished call into
an error the user would retry.

Infrastructure, not a tool: it posts with the user's own token through the shared
remote pool and is not subject to the cpa method allowlist (which governs what
TOOLS may call).
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .config import get_config
from .frappe_client import RemoteAuthMissing, backend_headers, remote_session
from .safety import _CREDENTIAL_NAME

LOG = "/api/method/ffl_core.api.connector.log_connector_call"
logger = logging.getLogger(__name__)


# Closing a record never holds the user's result back; it happens here.
_closer = ThreadPoolExecutor(max_workers=4, thread_name_prefix="audit-close")


def _submit(fn, *args) -> None:
    _closer.submit(fn, *args)


_ASSIGNED_SECRET = re.compile(r"(" + _CREDENTIAL_NAME.pattern + r")(\W{0,3}[=:]\W{0,3})\S+", re.I)
# Personal data a tool legitimately writes (hr_onboard) but the audit trail must not keep.
# Kept apart from _CREDENTIAL_NAME: that rule also strips fields from writes.
_PERSONAL_NAME = re.compile(r"date_of_birth", re.I)


def _mask(value: Any) -> Any:
    """Secrets masked before anything leaves this process (the POS masks again):
    by key (the write guards' credential rule, plus personal fields), in
    [field, op, value] filter triples, and inside JSON-string arguments."""
    if isinstance(value, dict):
        return {k: ("***" if _CREDENTIAL_NAME.search(str(k)) or _PERSONAL_NAME.search(str(k))
                    else _mask(v)) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) >= 3 and isinstance(value[0], str) and _CREDENTIAL_NAME.search(value[0]):
            return [value[0], value[1], "***", *value[3:]]
        return [_mask(v) for v in value]
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return json.dumps(_mask(json.loads(value)))
        except ValueError:
            return value
    return value


def _mask_text(text: str) -> str:
    """'api_key=sk_live…' style secrets in free text (error messages)."""
    return _ASSIGNED_SECRET.sub(lambda m: m.group(1) + m.group(m.lastindex) + "***", text)


class AuditUnavailable(RuntimeError):
    """The call could not be recorded, so it was not run."""


def _bearer() -> str:
    from mcp.server.auth.middleware.auth_context import get_access_token

    tok = get_access_token()
    if not tok:
        raise RemoteAuthMissing("No OAuth token on this request — reconnect the connector.")
    return tok.token


def _pos_reason(resp) -> str:
    try:
        body = resp.json() or {}
        msgs = [json.loads(m).get("message", "") for m in json.loads(body.get("_server_messages") or "[]")]
    except (ValueError, TypeError, AttributeError):
        return ""
    return re.sub(r"<[^>]+>", "", " ".join(m for m in msgs if m)).strip()


def _post(payload: dict, bearer: str) -> Any:
    cfg = get_config()
    resp = remote_session().post(cfg.backend_url + LOG, data=json.dumps(payload),
                                 headers=backend_headers(bearer), timeout=cfg.timeout)
    if resp.status_code >= 400:
        # Pass the POS's own reason through (e.g. "The full MCP server is switched
        # off in MCP Settings.") — the user needs it, not a bare status code.
        raise AuditUnavailable(_pos_reason(resp) or f"POS refused the call ({resp.status_code}).")
    return ((resp.json() or {}).get("message") or {}).get("log")


def start(tool: str, surface: str, arguments: dict) -> str:
    try:
        log = _post({"tool": tool, "surface": surface, "phase": "started",
                     "arguments": json.dumps(_mask(arguments), default=str)}, _bearer())
    except AuditUnavailable:
        raise
    except Exception as e:  # network, bad JSON
        raise AuditUnavailable(f"Could not record the call ({type(e).__name__}); not run.") from None
    if not log:
        raise AuditUnavailable("The POS returned no audit record; not run.")
    return log


def finish(log: str, tool: str, surface: str, error: str | None = None) -> None:
    """Close the record in the background (the token is read now, in the
    request's context — the worker thread has none)."""
    bearer = _bearer()

    def close():
        try:
            _post({"tool": tool, "surface": surface, "log": log,
                   "phase": "failed" if error else "ok",
                   "error": _mask_text(error or "")[:1000] or None}, bearer)
        except Exception:
            logger.warning("audit: could not close %s for %s", log, tool)

    _submit(close)
