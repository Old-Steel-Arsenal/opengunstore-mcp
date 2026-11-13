"""WordPress abilities bridge (GUNSTORE_MCP_MODE=web): a WordPress site's
abilities as a remote connector that people sign in to with their POS account.

Users sign in exactly as on the POS connectors (auth.FrappeTokenVerifier, surface
"web_ro"), every call is recorded in the POS's Connector Audit Log before it runs
(audit.py), and the site itself is called with ONE service account's application
password (HTTP Basic) through WordPress core's Abilities REST API:

  GET  /wp-json/wp-abilities/v1/abilities/<name>       metadata, schema, annotations
  GET  /wp-json/wp-abilities/v1/abilities/<name>/run   run a read; input as ?input[...]=
  POST /wp-json/wp-abilities/v1/abilities/<name>/run   run a write; input as {"input": ...}

Reads, twice over and failing closed: an ability in WEB_ABILITIES or
WEB_ABILITIES_RESTRICTED is bridged only when the site's own metadata also marks it
readonly, and it is called with GET, which WordPress itself refuses for anything else.

Writes are opt-in per deployment: exactly the abilities listed in WEB_ABILITIES_WRITE
(empty = the connector stays read-only), whatever the site's annotations say; those
only pick the HTTP method WordPress expects. Restricted reads and every write run only
for a user whose POS role gives website "manager" access; a write's audit record keeps
what it targets, not what it writes.

Which abilities a store exposes is that store's deployment configuration, not code.
"""
from __future__ import annotations

import base64
import functools
import html
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from http.cookiejar import DefaultCookiePolicy
from typing import Any
from urllib.parse import quote, urlparse

import anyio
import jsonschema
import requests
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools import Tool
from mcp.server.fastmcp.utilities.func_metadata import func_metadata
from mcp.types import TextContent, ToolAnnotations

from .config import _load_env, secure_url
from .modes import WEB_SURFACE

logger = logging.getLogger(__name__)

SITE_URL = "WEB_SITE_URL"
SITE_USER = "WEB_SITE_USER"
SITE_PASSWORD = "WEB_SITE_APP_PASSWORD"
ABILITIES = "WEB_ABILITIES"
ABILITIES_RESTRICTED = "WEB_ABILITIES_RESTRICTED"
ABILITIES_WRITE = "WEB_ABILITIES_WRITE"
# What a listed ability is: a read anyone with website access may run, a read only a
# manager may run, or a write (manager only).
READ, RESTRICTED, WRITE = "read", "restricted", "write"
# The POS roles that give website access (the POS decides; these name them to users).
VIEWER_ROLE = "Website Connector Viewer"
MANAGER_ROLE = "Website Connector Manager"

API = "/wp-json/wp-abilities/v1/abilities/"
SITE_TIMEOUT = 30
RESULT_MAX_CHARS = 150_000
# The catalog is fetched at start; a site that is down then is retried in the
# background (first after RETRY_FIRST seconds, doubling up to RETRY_MAX) instead of
# the process exiting and restarting in a loop.
RETRY_FIRST = 15
RETRY_MAX = 300
# lazy: no per-user rate limit; every call is audited in the POS and the site's own
# limits apply. Add a token bucket per POS user here if a client ever floods the site.

# Writes that delete, send, bulk-change or move an order's status: marked destructive
# (as is any the site itself marks destructive) so clients ask before each one. A hint
# for the client's confirmation prompt, not a gate.
_DESTRUCTIVE_WORDS = ("delete", "update-status", "change-campaign-status", "send", "bulk")
# One sentence a write tool's description adds, where its own parameters do not say
# what the change sets off (WC_Order::add_order_note fires woocommerce_new_customer_note
# for a customer note, which sends WooCommerce's "Customer note" email).
WRITE_NOTES = {
    "woocommerce/order-add-note": "With customer_note true the note is also emailed to the "
                                  "customer; without it (default false) the note is private.",
}

# WordPress's own rule for an ability name (WP_Abilities_Registry::register).
_NAME = re.compile(r"^[a-z0-9-]+/[a-z0-9-]+$")
_PLACEHOLDER_META = func_metadata(lambda: None, structured_output=False)


@dataclass(frozen=True)
class WebConfig:
    site_url: str
    user: str
    password: str
    # ability name -> READ, RESTRICTED or WRITE
    abilities: dict[str, str]


def tool_name(ability: str) -> str:
    """woocommerce/orders-query -> woocommerce_orders_query"""
    return re.sub(r"[/-]", "_", ability)


def _list(var: str) -> list[str]:
    return [n.strip() for n in (os.environ.get(var) or "").split(",") if n.strip()]


def writes_listed() -> bool:
    """Whether this deployment lists any write ability (the connector is read-only otherwise)."""
    return bool(_list(ABILITIES_WRITE))


def load_config() -> WebConfig:
    """Everything the bridge needs, or a refusal to start that names what is wrong."""
    _load_env()
    missing = [v for v in (SITE_URL, SITE_USER, SITE_PASSWORD) if not (os.environ.get(v) or "").strip()]
    if missing:
        raise RuntimeError(f"GUNSTORE_MCP_MODE=web needs {', '.join(missing)}.")
    url = os.environ[SITE_URL].strip().rstrip("/")
    parts = urlparse(url)
    if not secure_url(url) or parts.username or parts.password or parts.query or parts.fragment:
        # The application password travels to this URL on every call.
        raise RuntimeError(f"{SITE_URL} must be the site's https:// URL (plain http only for "
                           "localhost), without a query or credentials in it.")
    lists = {ABILITIES: (READ, _list(ABILITIES)), ABILITIES_RESTRICTED: (RESTRICTED, _list(ABILITIES_RESTRICTED)),
             ABILITIES_WRITE: (WRITE, _list(ABILITIES_WRITE))}
    named = [n for _, names in lists.values() for n in names]
    bad = [n for n in named if not _NAME.match(n)]
    if bad:
        raise RuntimeError(f"Not ability names (namespace/name): {', '.join(bad)}.")
    seen: dict[str, list[str]] = {}
    for var, (_, names) in lists.items():
        for n in dict.fromkeys(names):
            seen.setdefault(n, []).append(var)
    both = {n: v for n, v in seen.items() if len(v) > 1}
    if both:
        raise RuntimeError("Each ability belongs in one list; listed twice: " + "; ".join(
            f"{n} ({' and '.join(v)})" for n, v in sorted(both.items())) + ".")
    abilities = {n: kind for kind, names in lists.values() for n in names}
    if not abilities:
        raise RuntimeError(f"GUNSTORE_MCP_MODE=web needs at least one ability in {ABILITIES}, "
                           f"{ABILITIES_RESTRICTED} or {ABILITIES_WRITE} (comma-separated, e.g. "
                           "woocommerce/orders-query).")
    clash: dict[str, list[str]] = {}
    for n in abilities:
        clash.setdefault(tool_name(n), []).append(n)
    clash = {k: v for k, v in clash.items() if len(v) > 1}
    if clash:
        raise RuntimeError(f"These abilities would share a tool name: {sorted(clash.values())}.")
    return WebConfig(url, os.environ[SITE_USER].strip(), os.environ[SITE_PASSWORD], abilities)


class SiteError(RuntimeError):
    """A call to the site failed; the message is safe to show the user. `status`,
    `code` and `said` are the site's HTTP status, WordPress error code and message."""

    def __init__(self, message: str, status: int = 0, code: str = "", said: str = "") -> None:
        super().__init__(message)
        self.status, self.code, self.said = status, code, said


class Refused(RuntimeError):
    """The signed-in user may not run this ability."""


def _query(prefix: str, value: Any, out: list) -> list:
    """The ability input as PHP-style query pairs (input[a][0]=x), the encoding
    WordPress's own abilities client uses for GET. Booleans go as 1/0: PHP reads the
    string "false" as true. None, and an empty list or object (a query string cannot
    hold one), are left out, so the site applies the schema's default."""
    if value is None:
        return out
    if isinstance(value, bool):
        out.append((prefix, "1" if value else "0"))
    elif isinstance(value, dict):
        for k, v in value.items():
            _query(f"{prefix}[{k}]", v, out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _query(f"{prefix}[{i}]", v, out)
    else:
        out.append((prefix, str(value)))
    return out


def http_method(annotations: Any) -> str:
    """The method WordPress requires for an ability with these annotations
    (WP_REST_Abilities_V1_Run_Controller::validate_request_method): GET for a
    read-only one, DELETE for one both destructive and idempotent, POST otherwise.
    Who may run it is decided by the configured lists, never by these."""
    a = annotations if isinstance(annotations, dict) else {}
    if a.get("readonly"):
        return "GET"
    if a.get("destructive") and a.get("idempotent"):
        return "DELETE"
    return "POST"


class Site:
    """The site, as the bridge's service account."""

    def __init__(self, cfg: WebConfig) -> None:
        self.base = cfg.site_url + API
        self._auth = (cfg.user, cfg.password)
        basic = base64.b64encode(f"{cfg.user}:{cfg.password}".encode()).decode()
        # WordPress accepts an application password with or without its spaces.
        self._secrets = [s for s in {cfg.password, cfg.password.replace(" ", ""), basic} if s]
        self.session = requests.Session()
        # Shared by every user's calls: never keep a cookie the site sets.
        self.session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))

    def scrub(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    def _send(self, ability: str, suffix: str = "", params: list | None = None,
              method: str = "GET", body: dict | None = None) -> requests.Response:
        try:
            # No retries: a slow query repeated would only load the site more, and a
            # write repeated could be made twice.
            return self.session.request(method, self.base + quote(ability, safe="/") + suffix,
                                        params=params, json=body, auth=self._auth,
                                        timeout=SITE_TIMEOUT, headers={"Accept": "application/json"})
        except requests.RequestException as e:
            if method != "GET":
                raise SiteError(f"No answer from the website for {ability} ({type(e).__name__}). "
                                "The change may or may not have been made: check on the website "
                                "before trying again.") from None
            if isinstance(e, requests.Timeout):
                raise SiteError(f"The website did not answer within {SITE_TIMEOUT} s. Try a narrower "
                                "query, or try again later.") from None
            raise SiteError(f"Could not reach the website ({type(e).__name__}). "
                            "Try again later.") from None

    def _message(self, resp: requests.Response) -> tuple[str, str]:
        """(WordPress error code, its message), cleaned for the user."""
        try:
            body = resp.json()
        except ValueError:
            return "", ""
        if not isinstance(body, dict):
            return "", ""
        msg = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(body.get("message") or ""))))
        return str(body.get("code") or ""), self.scrub(msg.strip())[:500]

    def _error(self, ability: str, resp: requests.Response) -> SiteError:
        error = self._describe_error(ability, resp, resp.request.method if resp.request else "GET")
        error.status = resp.status_code
        error.code, error.said = self._message(resp)
        return error

    def _describe_error(self, ability: str, resp: requests.Response, method: str = "GET") -> SiteError:
        status = resp.status_code
        code, msg = self._message(resp)
        said = f" The website said: {msg}" if msg else ""
        if status == 401:
            return SiteError("The website refused this connector's own sign-in (its service account "
                             "or application password). Ask an administrator to check the "
                             "connector's website credentials.")
        if status == 403:
            return SiteError(f"The connector's website account is not allowed to run {ability}. "
                             f"Ask an administrator to give that account the role it needs.{said}")
        if status == 404 and code in ("rest_ability_not_found", "rest_no_route", ""):
            return SiteError(f"The website does not offer {ability} (its plugin may be off, or the "
                             "ability was renamed). Ask an administrator.")
        if status == 405:
            if method == "GET":
                return SiteError(f"The website no longer marks {ability} read-only, so this connector "
                                 "will not run it.")
            return SiteError(f"The website refused a {method} request for {ability} (its annotations "
                             "may have changed). Nothing was changed. Ask an administrator.")
        if status >= 500 and method != "GET":
            return SiteError(f"The website failed while running {ability} (HTTP {status}).{said} "
                             "The change may or may not have been made: check on the website "
                             "before trying again.")
        if status >= 500:
            return SiteError(f"The website failed while running {ability} (HTTP {status}).{said} "
                             "Try again later.")
        return SiteError(f"The website did not accept the request for {ability} (HTTP {status})."
                         + (said or ""))

    def _json(self, ability: str, resp: requests.Response) -> Any:
        if not 200 <= resp.status_code < 300:
            raise self._error(ability, resp)
        try:
            return resp.json()
        except ValueError:
            raise SiteError("The website answered with something that is not JSON (a maintenance "
                            "or security page?). Try again later.") from None

    def describe(self, ability: str) -> dict | None:
        """The ability's metadata; None when the site definitively does not have it."""
        resp = self._send(ability)
        if resp.status_code == 404:
            return None
        meta = self._json(ability, resp)
        return meta if isinstance(meta, dict) else None

    def run(self, ability: str, value: Any, method: str = "GET") -> str:
        if method == "POST":
            # The run controller reads a POST's input from the JSON body.
            resp = self._send(ability, "/run", method=method,
                              body={} if value is None else {"input": value})
        else:
            resp = self._send(ability, "/run", _query("input", value, []), method=method)
        text = self.scrub(json.dumps(self._json(ability, resp), ensure_ascii=False))
        if len(text) > RESULT_MAX_CHARS:
            text = (text[:RESULT_MAX_CHARS] + f"\n\n[Truncated: the result is {len(text)} characters "
                    f"and this connector returns at most {RESULT_MAX_CHARS}. Ask for less: use the "
                    "ability's paging or filter parameters (for example page / per_page).]")
        return text


# How a tool's MCP arguments become the ability's input.
NO_INPUT, OBJECT_INPUT, WRAPPED_INPUT = "none", "object", "wrapped"


def _draft4_required(schema: Any) -> Any:
    """WordPress schemas may mark a property `required: true` (draft 3); MCP clients
    and the SDK's validator expect the draft-4+ list on the parent object."""
    if isinstance(schema, list):
        return [_draft4_required(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: _draft4_required(v) for k, v in schema.items() if not (k == "required" and isinstance(v, bool))}
    props = out.get("properties")
    if isinstance(props, dict):
        req = [k for k, v in (schema.get("properties") or {}).items()
               if isinstance(v, dict) and v.get("required") is True]
        if req:
            out["required"] = sorted(set(out.get("required") or []) | set(req))
    return out


def _input_schema(ability: str, raw: Any) -> tuple[dict, str]:
    """(the tool's inputSchema, how to pass arguments on). MCP tool arguments are
    always an object; an ability whose input is not one takes it as `input`."""
    if not raw:
        return {"type": "object", "properties": {}}, NO_INPUT
    schema = _draft4_required(raw)
    if isinstance(schema, dict) and schema.get("type") == "object":
        mode = OBJECT_INPUT
    else:
        schema, mode = {"type": "object", "properties": {"input": schema}, "required": ["input"]}, WRAPPED_INPUT
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except jsonschema.SchemaError as e:
        # The SDK would refuse every call against a schema it cannot read; the site
        # still validates the input itself.
        logger.warning("web bridge: %s has an input schema this server cannot use (%s); "
                       "its tool accepts any arguments.", ability, e.message)
        schema = {"type": "object"} if mode == OBJECT_INPUT else \
            {"type": "object", "properties": {"input": {}}, "required": ["input"]}
    return schema, mode


def _empty_input(raw: Any) -> dict | None:
    """The input for a call made without arguments. A query string cannot carry an
    empty object, and WordPress refuses a missing input for an object schema unless
    the schema has a top-level default. So without one, send the properties' own
    declared defaults (what the ability would assume anyway); with none of those,
    send nothing and let the site say which input it needs."""
    if not isinstance(raw, dict) or "default" in raw or not isinstance(raw.get("properties"), dict):
        return None
    defaults = {k: v["default"] for k, v in raw["properties"].items()
                if isinstance(v, dict) and "default" in v}
    return defaults or None


def _empty_call_refusal(tool: str, schema: dict, site_error: SiteError) -> SiteError:
    """The refusal for a call made without arguments that the site would not take
    (see SiteTool._invoke): name the parameters so the client retries with some."""
    props = schema.get("properties")
    names = list(props) if isinstance(props, dict) else []
    required = set(schema.get("required") or [])
    said = f" The website said: {site_error.said}" if site_error.said else ""
    if not names:
        return SiteError(f"The website does not accept an empty call to {tool} over this "
                         "connector, and the tool has no parameters to pass instead. Ask an "
                         "administrator to give the ability's input a default on the website."
                         + said)
    listed = ", ".join(f"{n} (required)" if n in required else n for n in names)
    return SiteError(f"{tool} cannot be called without arguments. Call it again with at least "
                     f"one of its parameters: {listed}.{said}")


class SiteTool(Tool):
    """One site ability as one MCP tool. `fn(ability, input, method)` calls the site;
    run() adds the role check and the audit record around it, in a worker thread."""

    ability: str
    # Manager only: a restricted read, or any write.
    restricted: bool
    write: bool = False
    method: str = "GET"
    input_mode: str
    # What a call without arguments sends (see _empty_input).
    empty_input: dict | None = None

    def _invoke(self, arguments: dict) -> str:
        from mcp.server.auth.middleware.auth_context import get_access_token

        role = getattr(get_access_token(), "web_access", "")
        if not role:
            raise Refused(f"Your POS account has no website access role ({VIEWER_ROLE} or "
                          f"{MANAGER_ROLE}). Ask a manager to add one.")
        if self.restricted and role != "manager":
            what = "changes the website and needs" if self.write else "needs"
            raise Refused(f"{self.name} {what} the {MANAGER_ROLE} role in the POS; your account "
                          f"has {VIEWER_ROLE}. Ask a manager.")
        if self.input_mode == NO_INPUT:
            value = None
        elif self.input_mode == WRAPPED_INPUT:
            value = arguments.get("input")
        elif self.method == "POST":
            # A JSON body carries an empty object as it is.
            value = arguments
        else:
            value = arguments or self.empty_input
        try:
            return self.fn(self.ability, value, self.method)
        except SiteError as e:
            # A query string cannot carry an empty object, so WordPress validates a
            # missing input against an object schema without a top-level default and
            # refuses it with 400 ability_invalid_input ("input is not of type object";
            # WP_Ability::normalize_input / validate_input). No encoding gets an empty
            # array to the ability over GET, so tell the client to pass arguments. The
            # code alone is matched: the message is translated.
            if (not arguments and self.input_mode == OBJECT_INPUT and self.method != "POST"
                    and e.status == 400 and e.code == "ability_invalid_input"):
                raise _empty_call_refusal(self.name, self.parameters, e) from None
            raise

    async def run(self, arguments: dict[str, Any], context=None, convert_result: bool = False) -> Any:
        from .server import _audited

        # A write's record names its target, not the text or personal data it writes.
        call = functools.partial(_audited, lambda **kw: self._invoke(kw), self.name, WEB_SURFACE,
                                 (), arguments, audit_record(arguments) if self.write else None)
        try:
            text = await anyio.to_thread.run_sync(call)
        except Exception as e:
            raise ToolError(f"Error executing tool {self.name}: {e}") from None
        return [TextContent(type="text", text=text)] if convert_result else text


def make_tool(site: Site, ability: str, meta: dict, kind: str = READ) -> SiteTool | None:
    """The tool for one listed ability, or None (logged). A read must be marked
    read-only by the site: only `readonly: true` passes; missing, null or anything
    else is refused. A write is listed as one on purpose; the site's annotations only
    choose the HTTP method WordPress expects."""
    block = meta.get("meta")
    annotations = block.get("annotations") if isinstance(block, dict) else None
    write = kind == WRITE
    if meta.get("name") != ability:
        logger.warning("web bridge: SKIPPED %s: the website answered for another ability.", ability)
        return None
    if not write and (not isinstance(annotations, dict) or annotations.get("readonly") is not True):
        logger.warning("web bridge: SKIPPED %s: the website does not mark it read-only.", ability)
        return None
    annotations = annotations if isinstance(annotations, dict) else {}
    schema, mode = _input_schema(ability, meta.get("input_schema"))
    label = str(meta.get("label") or "") or None
    description = str(meta.get("description") or label or ability)
    if write:
        if ability in WRITE_NOTES:
            description += f"\n\n{WRITE_NOTES[ability]}"
        description += f"\n\nChanges the website. Needs the {MANAGER_ROLE} role in the POS."
    elif kind == RESTRICTED:
        description += f"\n\nNeeds the {MANAGER_ROLE} role in the POS."
    method = http_method(annotations) if write else "GET"
    return SiteTool(
        fn=site.run, name=tool_name(ability), title=label,
        description=description, parameters=schema, fn_metadata=_PLACEHOLDER_META,
        is_async=True, context_kwarg=None,
        annotations=ToolAnnotations(title=label, readOnlyHint=not write,
                                    destructiveHint=write and (
                                        any(w in ability for w in _DESTRUCTIVE_WORDS)
                                        or annotations.get("destructive") is True),
                                    idempotentHint=annotations.get("idempotent") is True),
        ability=ability, restricted=kind != READ, write=write, method=method, input_mode=mode,
        empty_input=_empty_input(meta.get("input_schema")) if mode == OBJECT_INPUT and method != "POST"
        else None)


# A write's audit record: these keys are kept as given (what the change targets, and
# the segment or status change itself); an email is shortened; a map keeps its key
# names (which fields changed, not to what); any other text or list keeps its size only.
_AUDIT_KEEP = frozenset({"id", "ids", "contact_id", "contact_ids", "order_id", "post_id", "product_id",
                         "type", "status", "contact_type", "if_exists", "dry_run",
                         "add_tags", "remove_tags", "add_lists", "remove_lists"})
_AUDIT_EMAIL = frozenset({"email", "new_email"})
# lazy: 50 bulk items listed per record; the POS cuts a record over 4000 characters anyway.
_AUDIT_ITEMS = 50


def _audit_value(key: str, value: Any) -> Any:
    if key in _AUDIT_EMAIL and isinstance(value, str):
        local, at, domain = value.partition("@")
        return f"{local[:1]}***@{domain}" if at else "***"
    if isinstance(value, dict):
        return sorted(value)
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        out: list = [audit_record(v) for v in value[:_AUDIT_ITEMS]]
        return out + [f"... {len(value) - _AUDIT_ITEMS} more"] if len(value) > _AUDIT_ITEMS else out
    if key in _AUDIT_KEEP:
        return value
    if isinstance(value, list):
        return f"<{len(value)} items>"
    if isinstance(value, str):
        return f"<{len(value)} characters>"
    return value


def audit_record(arguments: Any) -> Any:
    """What a write's Connector Audit Log row keeps of its arguments: who and what it
    targets, without the text or personal data it writes."""
    if not isinstance(arguments, dict):
        return {"input": _audit_value("input", arguments)}
    return {k: _audit_value(str(k), v) for k, v in arguments.items()}


class Catalog:
    """Loads the listed abilities' metadata from the site and registers the tools
    that pass the read-only gate. An ability the site answers for (or definitively
    lacks) is settled; one it could not answer for stays pending and is retried."""

    def __init__(self, mcp, site: Site, abilities: dict[str, str]) -> None:
        self.mcp, self.site = mcp, site
        self.pending = dict(abilities)

    def _add(self, tool: SiteTool) -> None:
        manager = self.mcp._tool_manager
        # A new dict swapped in whole: requests listing tools on the event loop
        # never see one being changed under them.
        manager._tools = {**manager._tools, tool.name: tool}

    def load_once(self) -> None:
        for ability, kind in list(self.pending.items()):
            try:
                meta = self.site.describe(ability)
            except SiteError as e:
                if not e.status:
                    # No answer at all (down, timeout, not JSON): leave the rest for the next attempt.
                    logger.warning("web bridge: could not load the website's abilities (%s); "
                                   "will retry.", e)
                    return
                # The site answered, with an error for this one ability: it stays pending (it may
                # recover) without holding back the abilities listed after it.
                logger.warning("web bridge: could not load %s (HTTP %s: %s); will retry.",
                               ability, e.status, e)
                continue
            del self.pending[ability]
            if meta is None:
                logger.warning("web bridge: SKIPPED %s: the website does not offer it.", ability)
                continue
            tool = make_tool(self.site, ability, meta, kind)
            if tool:
                self._add(tool)
        names = sorted(t.name for t in self.mcp._tool_manager.list_tools())
        logger.warning("web bridge: ready, %d tool(s): %s%s", len(names), ", ".join(names) or "none",
                       f"; {len(self.pending)} ability(ies) still pending: "
                       f"{', '.join(sorted(self.pending))}" if self.pending else "")

    def retry_until_loaded(self) -> None:
        delay = RETRY_FIRST
        while self.pending:
            time.sleep(delay)
            self.load_once()
            delay = min(delay * 2, RETRY_MAX)


def register(mcp) -> Catalog:
    """Register the configured site's abilities on `mcp`."""
    cfg = load_config()
    catalog = Catalog(mcp, Site(cfg), cfg.abilities)
    catalog.load_once()
    if catalog.pending:
        threading.Thread(target=catalog.retry_until_loaded, name="web-catalog", daemon=True).start()
    return catalog
