"""Remote-connector auth — this server is an OAuth *resource server* for the POS.

The POS (Frappe) is the authorization server: the connector registers itself
there, the user signs in to the POS and approves, and every request then arrives
with that user's bearer token. This module only answers "is this a live token of
a POS user, issued to a connector?" — by asking the POS itself
(ffl_core.api.connector.connector_identity) — so no key, secret or user database
lives here. Tokens issued to OAuth apps created in Desk are refused: only a
client made by dynamic registration (a connector) may drive the MCP. The same
token is then forwarded on every Frappe call (frappe_client.get_client), so each
call runs with that user's own roles.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict

import requests
from mcp.server.auth.provider import AccessToken

from .frappe_client import remote_session

IDENTITY = "/api/method/ffl_core.api.connector.connector_identity"
# How long a positive answer is reused. Short on purpose: a token revoked in the
# POS stops working here within this window.
CACHE_SECONDS = 60
# A token the POS rejected is remembered briefly, so repeating one bad token
# costs one POS round-trip per window.
REJECT_SECONDS = 30
# lazy: a plain LRU bound; a flood of DISTINCT bogus tokens still costs one POS
# call each — rate-limit at the proxy if that ever shows up.
CACHE_MAX = 5000


# connector_identity's web_access: the website role of the token's user ("" = none).
# Only the web surface reads it; anything unexpected counts as no role.
WEB_ACCESS = ("viewer", "manager")


class ConnectorToken(AccessToken):
    """The SDK's AccessToken plus the user's website role, so a web tool can
    tell a viewer from a manager without another POS round-trip."""
    web_access: str = ""


class AuthUnavailable(RuntimeError):
    """The POS could not answer (restarting, 5xx, rate-limited, not JSON). Not
    a verdict on the token: nothing is cached and the request fails with a
    server error, so clients retry instead of throwing their token away."""


def _key(token: str) -> str:
    # The cache never holds the token itself.
    return hashlib.sha256(token.encode()).hexdigest()


class FrappeTokenVerifier:
    """mcp TokenVerifier backed by the POS."""

    def __init__(self, backend_url: str, host_header: str = "", timeout: int = 10,
                 surface: str = "full") -> None:
        self.backend_url = backend_url.rstrip("/")
        # Which server is asking. A surface switched off in the POS's MCP Settings
        # still verifies the token (a refusal would look like a bad token and send
        # clients into a sign-in loop); its calls are refused with the reason when
        # they try to start (audit.start).
        self.surface = surface
        self.host_header = host_header
        self.timeout = timeout
        self._cache: OrderedDict[str, tuple[float, str, int | None, str]] = OrderedDict()

    def _identity(self, token: str) -> tuple[str, int | None, str]:
        """(the token's user, its expiry as a Unix time, its web_access); user ""
        when the POS refuses it. Raises AuthUnavailable when the POS gives no verdict."""
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if self.host_header:
            headers["Host"] = self.host_header
        try:
            resp = remote_session().get(self.backend_url + IDENTITY, headers=headers,
                                        params={"surface": self.surface}, timeout=self.timeout)
        except requests.RequestException as e:
            raise AuthUnavailable(f"POS unreachable: {type(e).__name__}") from None
        if resp.status_code in (401, 403, 417):
            return "", None, ""
        if resp.status_code != 200:
            raise AuthUnavailable(f"POS answered {resp.status_code}")
        try:
            me = (resp.json() or {}).get("message") or {}
        except ValueError:
            raise AuthUnavailable("POS answered non-JSON") from None
        user = me.get("user") if isinstance(me, dict) else None
        if not user or user == "Guest" or not me.get("connector"):
            return "", None, ""
        expires = me.get("expires_at")
        web = me.get("web_access")
        return (user, int(expires) if isinstance(expires, (int, float)) else None,
                web if web in WEB_ACCESS else "")

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        now = time.monotonic()
        key = _key(token)
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            self._cache.move_to_end(key)
            _, user, expires, web = hit
        else:
            # The role is optional in the answer: none means no website access.
            user, expires, *role = await asyncio.to_thread(self._identity, token)
            web = role[0] if role else ""
            ttl = CACHE_SECONDS if user else REJECT_SECONDS
            if expires:
                # Never trust a token past its own expiry: past it the SDK answers 401
                # at the HTTP layer and the client refreshes, instead of tools failing.
                ttl = max(0.0, min(ttl, expires - time.time()))
            self._cache[key] = (now + ttl, user, expires, web)
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_MAX:
                self._cache.popitem(last=False)
        if not user:
            return None
        return ConnectorToken(token=token, client_id=user, scopes=[], expires_at=expires,
                              web_access=web)
