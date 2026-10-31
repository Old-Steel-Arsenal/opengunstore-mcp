"""Centralized write guards used by every mutating tool.

Concerns kept in one place so generic and curated tools can't diverge:
  - strip_passwords: credentials never transit the MCP / the conversation.
  - require_confirm: destructive/consequential ops need an explicit confirm=true.
  - check_write_allowed: structural/permission doctypes are read-only here.
  - check_fields_writable: some fields are not writable through the MCP at all.
  - check_method_call_writable: the same rule, for the run_method escape hatch.
  - stripped_note: uniform "field X was not sent" response wrapper.
"""
from __future__ import annotations

import re
import time

from .config import get_config
from .frappe_client import get_client

# Name-based credential backstop — applied to EVERY key at every nesting level,
# even when doctype meta can't be fetched, so a secret can't slip through on a
# transient describe failure. Meta-based Password-type detection is primary.
#
# `[_-]key\b` is the general arm: it catches `consumer_key`, `dev_key`,
# `sandbox_dev_key` and anything else spelled <thing>_key, while leaving RSR's
# `key_dealer` / `ftp_dir_keydealer` alone (those are a dealer tier, not a
# secret — "key" leads there, and no word boundary follows it). A bare `key`
# would strip both and break legitimate writes. `dev[_-]?key` is spelled out
# separately so the unseparated `devkey` is covered too, the way `apikey` is.
#
# Verified against every Password field in the platform: before this arm,
# `consumer_key` (WooCommerce Settings) already
# escaped the backstop — GunBroker's DevKey was not the first gap, only the one
# that got noticed.
_CREDENTIAL_NAME = re.compile(
    r"(passwd|password|pwd|secret|api[_-]?key|token|webhook|private[_-]?key"
    r"|dev[_-]?key|[_-]key\b|passphrase|credential|\bpin\b)",
    re.I,
)

# Fields the MCP must never write, per doctype. Distinct from the credential
# backstop above in both mechanism and reason: credentials are stripped and
# reported, these REFUSE the whole call.
#
# Refusing rather than stripping is the point. A caller who asks to flip
# `sandbox_mode` and set `page_size` in one call believes both landed; strip the
# first and the write half-applies while the response says it succeeded in a
# note nobody reads. For a field that decides whether a firearm goes on the real
# GunBroker, "you may not do this here" has to be an error, not a footnote.
#
# These belong in the desk UI, done by a person who can see the whole settings
# form, not in a tool call an agent can talk itself into. The MCP instance
# genuinely points at production (see tools/distributor.py:169-177 for the same
# reasoning applied to registration).
MCP_NEVER_WRITES: dict[str, frozenset[str]] = {
    "GunBroker Settings": frozenset({
        # environment — LD-13: sandbox vs the live marketplace is decided on the
        # POS and nowhere else. base_url_override is here because it overrides
        # the base URL outright and so defeats sandbox_mode on its own.
        "enabled", "sandbox_mode", "base_url_override",
        # credentials — also caught by the backstop above; named here so the
        # refusal is explicit rather than a silent strip.
        "dev_key", "sandbox_dev_key", "username", "password",
        # money and irreversibility
        "end_strategy", "check_deposit_account", "card_checkout_enabled",
    }),
    # Switching it off erases every stored customer DOB and ID number.
    "FFL Settings": frozenset({"store_customer_id_numbers"}),
    # Booking a job into the A&D book: gunsmith_job_action(action='log_bound_book').
    "Gunsmith Work Order": frozenset({"log_in_bound_book"}),
    # Turning a card processor on or off goes through payments.hub.toggle, which
    # refuses a second active processor:
    # frappe_run_method('ffl_integrations.payments.hub.toggle', ..., confirm=true).
    "Payroc Settings": frozenset({"enabled"}),
}

_PW_TTL = 600.0  # re-describe a doctype's Password fields at most every 10 min
# Keyed by (user, doctype): on the remote server each user's describe runs with
# THEIR roles, and a partial answer for one user must never stand in for another.
_pw_cache: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}


def _cache_user() -> str:
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        tok = get_access_token()
    except Exception:
        tok = None
    return tok.client_id if tok else ""


class WriteRefused(RuntimeError):
    """Raised when a write is blocked by a guard (surfaced to the user as-is)."""


def _password_fields(doctype: str) -> frozenset[str]:
    now = time.monotonic()
    key = (_cache_user(), doctype)
    hit = _pw_cache.get(key)
    if hit and now - hit[0] < _PW_TTL:
        return hit[1]
    fields = get_client().describe_doctype(doctype)
    pw = frozenset(
        f.get("fieldname") for f in fields
        if f.get("fieldtype") == "Password" and f.get("fieldname")
    )
    if fields:  # never cache an empty result from a failed describe
        _pw_cache[key] = (now, pw)
    return pw


def _is_credential(key: str, meta_pw: frozenset[str]) -> bool:
    return key in meta_pw or bool(_CREDENTIAL_NAME.search(key))


def _strip(values: dict, meta_pw: frozenset[str], stripped: list[str], prefix: str = "") -> dict:
    clean: dict = {}
    for k, v in values.items():
        path = f"{prefix}{k}"
        if _is_credential(k, meta_pw):
            stripped.append(path)
            continue
        if isinstance(v, dict):
            clean[k] = _strip(v, meta_pw, stripped, f"{path}.")
        elif isinstance(v, list):
            clean[k] = [
                _strip(row, meta_pw, stripped, f"{path}[].") if isinstance(row, dict) else row
                for row in v
            ]
        else:
            clean[k] = v
    return clean


def strip_passwords(doctype: str, values: dict) -> tuple[dict, list[str]]:
    """Return (values with every credential field removed, list of stripped paths).

    Recurses into nested dicts and child-table rows. Credential detection is
    meta-based (Password fieldtype) for the top-level doctype, plus a name-regex
    backstop applied at every nesting level."""
    try:
        meta_pw = _password_fields(doctype)
    except Exception:
        # Fail safe: a describe hiccup must never block a legitimate write, and
        # the name-regex backstop still covers credential-named fields.
        meta_pw = frozenset()
    stripped: list[str] = []
    clean = _strip(values, meta_pw, stripped)
    return clean, stripped


def stripped_note(result, stripped: list[str]):
    """Wrap a write result with a note naming any credential fields not sent."""
    if not stripped:
        return result
    return {
        "result": result,
        "stripped_password_fields": stripped,
        "note": (
            f"credential field(s) {stripped} were NOT sent — set them directly in "
            "Desk; the MCP never transmits credentials."
        ),
    }


def _forbidden_hit(doctype: str, names) -> list[str]:
    forbidden = MCP_NEVER_WRITES.get(doctype)
    if not forbidden:
        return []
    return sorted(set(names) & forbidden)


def _refuse(doctype: str, hit: list[str]) -> None:
    raise WriteRefused(
        f"Refused: {hit} cannot be set through the MCP on '{doctype}'. "
        f"These pick the live environment, hold credentials, move money, erase "
        f"records or have a dedicated tool — a person changes them in Desk, or the "
        f"tool does, not a raw write. Not writable here: "
        f"{sorted(MCP_NEVER_WRITES[doctype])}."
    )


def check_fields_writable(doctype: str, values: dict) -> None:
    """Refuse the whole write if it touches a field the MCP may never set.

    **Call this BEFORE strip_passwords.** After it, the credential fields are
    already gone, so a refusal silently degrades into a strip — which is the
    exact behaviour MCP_NEVER_WRITES exists to rule out (see its comment).

    Top level only: the forbidden names are all scalars on the parent doctype,
    and a child row that happens to reuse one of these names (a `category_map`
    row with an `enabled` column, say) is a different field entirely."""
    hit = _forbidden_hit(doctype, values)
    if hit:
        _refuse(doctype, hit)


# Method names that write a field by name rather than through a document body.
# run_method is the escape hatch: it reaches any whitelisted dotted path, so the
# structured guards above never see the doctype or the fieldname.
_SETTER_METHOD = re.compile(
    r"(set_value|set_single_value|set_default|db_set|bulk_update|update_doc|save_doc)", re.I)


def _strings_and_keys(obj, out: set) -> set:
    """Every string and every dict key anywhere in the payload.

    Deliberately shape-agnostic. set_value takes (doctype, name, fieldname,
    value); set_single_value takes (doctype, fieldname, value); fieldname can
    also be a dict of several fields. Rather than model each signature — and be
    wrong about the next one — collect every name-ish token and match against
    that."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            _strings_and_keys(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _strings_and_keys(v, out)
    elif isinstance(obj, str):
        out.add(obj)
    return out


def check_method_call_writable(method: str, kwargs: dict | None) -> None:
    """Refuse a run_method call that would set a never-writable field.

    Only field-setter methods are inspected, so an ordinary whitelisted call
    that merely mentions a doctype in passing is unaffected. Frappe's own
    mutators are already refused wholesale by generic._BLOCKED_GENERIC_METHODS;
    this covers the ones that are not on that list — including app-side
    whitelisted helpers that end up writing Settings."""
    if not kwargs or not _SETTER_METHOD.search(method or ""):
        return
    tokens = _strings_and_keys(kwargs, set())
    for doctype in MCP_NEVER_WRITES:
        if doctype not in tokens:
            continue
        hit = _forbidden_hit(doctype, tokens)
        if hit:
            _refuse(doctype, hit)


def require_confirm(action: str, confirm: bool) -> None:
    if not confirm:
        raise WriteRefused(
            f"Refused: '{action}' is destructive/consequential. "
            "Re-call with confirm=true to proceed."
        )


def require_reason(action: str, reason: str | None) -> str:
    """Validate the mandatory reason on cancel-class ops; returns it stripped.

    The server enforces non-empty reasons independently — this is the UX
    front-gate so the refusal happens before any network call, and it lives
    here so curated tools can't diverge on the message."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError(
            f"reason is required for '{action}' — say why (it is recorded "
            "in the audit trail)."
        )
    return reason


def check_write_allowed(doctype: str) -> None:
    if doctype in get_config().write_denylist:
        raise WriteRefused(
            f"Refused: writes to '{doctype}' are blocked (schema/permission doctype). "
            "Change these in Desk or via code/migrations, not the MCP."
        )
