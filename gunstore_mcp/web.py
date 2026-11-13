"""WordPress abilities bridge (GUNSTORE_MCP_MODE=web): a WordPress site's
read-only abilities as a remote connector that people sign in to with their POS
account.

Users sign in exactly as on the POS connectors (auth.FrappeTokenVerifier, surface
"web_ro"), every call is recorded in the POS's Connector Audit Log before it runs
(audit.py), and the site itself is called with ONE service account's application
password (HTTP Basic) through WordPress core's Abilities REST API:

  GET /wp-json/wp-abilities/v1/abilities/<name>       metadata, schema, annotations
  GET /wp-json/wp-abilities/v1/abilities/<name>/run   run it; input as ?input[...]=

Read-only, twice over, failing closed: an ability is bridged only when it is in this
deployment's list (WEB_ABILITIES / WEB_ABILITIES_RESTRICTED) AND the site's own
metadata marks it readonly. Every call is a GET, which WordPress itself refuses for
an ability that is not read-only. Restricted abilities run only for a user whose POS
role gives website "manager" access.

No ability name is written here: which abilities a store exposes is that store's
deployment configuration.
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

# WordPress's own rule for an ability name (WP_Abilities_Registry::register).
_NAME = re.compile(r"^[a-z0-9-]+/[a-z0-9-]+$")
_PLACEHOLDER_META = func_metadata(lambda: None, structured_output=False)


@dataclass(frozen=True)
class WebConfig:
    site_url: str
    user: str
    password: str
    # ability name -> restricted (manager only)
    abilities: dict[str, bool]


def tool_name(ability: str) -> str:
    """woocommerce/orders-query -> woocommerce_orders_query"""
    return re.sub(r"[/-]", "_", ability)


def _list(var: str) -> list[str]:
    return [n.strip() for n in (os.environ.get(var) or "").split(",") if n.strip()]


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
    plain, restricted = _list(ABILITIES), _list(ABILITIES_RESTRICTED)
    if not plain and not restricted:
        raise RuntimeError(f"GUNSTORE_MCP_MODE=web needs at least one ability in {ABILITIES} "
                           f"or {ABILITIES_RESTRICTED} (comma-separated, e.g. woocommerce/orders-query).")
    bad = [n for n in plain + restricted if not _NAME.match(n)]
    if bad:
        raise RuntimeError(f"Not ability names (namespace/name): {', '.join(bad)}.")
    both = sorted(set(plain) & set(restricted))
    if both:
        raise RuntimeError(f"Listed in both {ABILITIES} and {ABILITIES_RESTRICTED}: {', '.join(both)}.")
    abilities = {n: False for n in plain} | {n: True for n in restricted}
    clash: dict[str, list[str]] = {}
    for n in abilities:
        clash.setdefault(tool_name(n), []).append(n)
    clash = {k: v for k, v in clash.items() if len(v) > 1}
    if clash:
        raise RuntimeError(f"These abilities would share a tool name: {sorted(clash.values())}.")
    return WebConfig(url, os.environ[SITE_USER].strip(), os.environ[SITE_PASSWORD], abilities)


class SiteError(RuntimeError):
    """A call to the site failed; the message is safe to show the user."""


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


class Site:
    """The site, as the bridge's service account. GET only."""

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

    def _get(self, ability: str, suffix: str = "", params: list | None = None) -> requests.Response:
        try:
            # No retries: a slow query repeated would only load the site more.
            return self.session.get(self.base + quote(ability, safe="/") + suffix, params=params,
                                    auth=self._auth, timeout=SITE_TIMEOUT,
                                    headers={"Accept": "application/json"})
        except requests.Timeout:
            raise SiteError(f"The website did not answer within {SITE_TIMEOUT} s. Try a narrower "
                            "query, or try again later.") from None
        except requests.RequestException as e:
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
            return SiteError(f"The website no longer marks {ability} read-only, so this connector "
                             "will not run it.")
        if status >= 500:
            return SiteError(f"The website failed while running {ability} (HTTP {status}).{said} "
                             "Try again later.")
        return SiteError(f"The website did not accept the request for {ability} (HTTP {status})."
                         + (said or ""))

    def _json(self, ability: str, resp: requests.Response) -> Any:
        if resp.status_code != 200:
            raise self._error(ability, resp)
        try:
            return resp.json()
        except ValueError:
            raise SiteError("The website answered with something that is not JSON (a maintenance "
                            "or security page?). Try again later.") from None

    def describe(self, ability: str) -> dict | None:
        """The ability's metadata; None when the site definitively does not have it."""
        resp = self._get(ability)
        if resp.status_code == 404:
            return None
        meta = self._json(ability, resp)
        return meta if isinstance(meta, dict) else None

    def run(self, ability: str, value: Any) -> str:
        resp = self._get(ability, "/run", _query("input", value, []))
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


class SiteTool(Tool):
    """One site ability as one MCP tool. `fn(input)` calls the site; run() adds the
    role check and the audit record around it, in a worker thread."""

    ability: str
    restricted: bool
    input_mode: str

    def _invoke(self, arguments: dict) -> str:
        from mcp.server.auth.middleware.auth_context import get_access_token

        role = getattr(get_access_token(), "web_access", "")
        if not role:
            raise Refused("Your POS account has no website access role (Website Viewer or "
                          "Website Manager). Ask a manager to add one.")
        if self.restricted and role != "manager":
            raise Refused(f"{self.name} needs the Website Manager role in the POS; your account "
                          "has Website Viewer. Ask a manager.")
        if self.input_mode == NO_INPUT:
            value = None
        elif self.input_mode == WRAPPED_INPUT:
            value = arguments.get("input")
        else:
            value = arguments or None
        return self.fn(self.ability, value)

    async def run(self, arguments: dict[str, Any], context=None, convert_result: bool = False) -> Any:
        from .server import _audited

        call = functools.partial(_audited, lambda **kw: self._invoke(kw), self.name, WEB_SURFACE,
                                 (), arguments)
        try:
            text = await anyio.to_thread.run_sync(call)
        except Exception as e:
            raise ToolError(f"Error executing tool {self.name}: {e}") from None
        return [TextContent(type="text", text=text)] if convert_result else text


def make_tool(site: Site, ability: str, meta: dict, restricted: bool) -> SiteTool | None:
    """The tool for one listed ability, or None (logged) when the site does not
    mark it read-only. Only `readonly: true` passes; missing, null or anything else
    is refused."""
    block = meta.get("meta")
    annotations = block.get("annotations") if isinstance(block, dict) else None
    if meta.get("name") != ability or not isinstance(annotations, dict) or annotations.get("readonly") is not True:
        logger.warning("web bridge: SKIPPED %s: the website does not mark it read-only.", ability)
        return None
    schema, mode = _input_schema(ability, meta.get("input_schema"))
    label = str(meta.get("label") or "") or None
    description = str(meta.get("description") or label or ability)
    if restricted:
        description += "\n\nNeeds the Website Manager role in the POS."
    return SiteTool(
        fn=site.run, name=tool_name(ability), title=label,
        description=description, parameters=schema, fn_metadata=_PLACEHOLDER_META,
        is_async=True, context_kwarg=None,
        annotations=ToolAnnotations(title=label, readOnlyHint=True,
                                    destructiveHint=False,
                                    idempotentHint=annotations.get("idempotent") is True),
        ability=ability, restricted=restricted, input_mode=mode)


class Catalog:
    """Loads the listed abilities' metadata from the site and registers the tools
    that pass the read-only gate. An ability the site answers for (or definitively
    lacks) is settled; one it could not answer for stays pending and is retried."""

    def __init__(self, mcp, site: Site, abilities: dict[str, bool]) -> None:
        self.mcp, self.site = mcp, site
        self.pending = dict(abilities)

    def _add(self, tool: SiteTool) -> None:
        manager = self.mcp._tool_manager
        # A new dict swapped in whole: requests listing tools on the event loop
        # never see one being changed under them.
        manager._tools = {**manager._tools, tool.name: tool}

    def load_once(self) -> None:
        for ability, restricted in list(self.pending.items()):
            try:
                meta = self.site.describe(ability)
            except SiteError as e:
                logger.warning("web bridge: could not load the website's abilities (%s); will retry.", e)
                return  # the site is likely down: leave the rest for the next attempt
            del self.pending[ability]
            if meta is None:
                logger.warning("web bridge: SKIPPED %s: the website does not offer it.", ability)
                continue
            tool = make_tool(self.site, ability, meta, restricted)
            if tool:
                self._add(tool)
        if not self.pending:
            names = sorted(t.name for t in self.mcp._tool_manager.list_tools())
            logger.warning("web bridge: ready, %d tool(s): %s", len(names), ", ".join(names) or "none")

    def retry_until_loaded(self) -> None:
        delay = RETRY_FIRST
        while self.pending:
            time.sleep(delay)
            self.load_once()
            delay = min(delay * 2, RETRY_MAX)


def register(mcp) -> Catalog:
    """Register the configured site's read-only abilities on `mcp`."""
    cfg = load_config()
    catalog = Catalog(mcp, Site(cfg), cfg.abilities)
    catalog.load_once()
    if catalog.pending:
        threading.Thread(target=catalog.retry_until_loaded, name="web-catalog", daemon=True).start()
    return catalog
