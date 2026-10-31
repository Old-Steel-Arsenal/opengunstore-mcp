"""OpenGunstore admin MCP server.

Run: `gunstore-mcp` (console script) or `python -m gunstore_mcp.server`.

Transports (GUNSTORE_MCP_TRANSPORT): "stdio" (default) = the local server, one
API key from mcp/.env (see .env.example); "http" = the remote connector — an
OAuth resource server whose authorization server is the POS itself. It holds no
key: every request carries the signed-in user's bearer token, verified against
the POS (auth.FrappeTokenVerifier) and forwarded on every call.

Modes (GUNSTORE_MCP_MODE): "full" (default) = the whole surface; "cpa" = the
read-only accountant surface (exactly the tools in modes.CPA_TOOL_NAMES; the
write tools are never registered).

GUNSTORE_MCP_DISTRIBUTOR_ACTIONS=1 additionally opts into the 4 distributor queue
actions, which are NOT registered otherwise. Sizes are asserted in tests rather
than restated here — this docstring is where the last stale count lived."""
from __future__ import annotations

import functools
import inspect
from urllib.parse import urlparse

import anyio

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__, audit
from .auth import FrappeTokenVerifier
from .config import HTTP, get_config, get_transport
from .modes import CPA_MODE, CPA_TOOL_NAMES, FULL_MODE, FilteredMCP, get_mode
from .tools import (compliance, curated, customers, distributor, generic, hr, orders, range_board,
                    reports, services, shopfloor)


def _audited(fn, name: str, surface: str, args: tuple, kwargs: dict):
    """Run one tool call inside its audit record (see audit.py)."""
    log = audit.start(name, surface, kwargs)
    try:
        result = fn(*args, **kwargs)
    except Exception as e:
        audit.finish(log, name, surface, error=f"{type(e).__name__}: {e}")
        raise
    audit.finish(log, name, surface)
    return result


class ThreadedMCP:
    """Remote registration wrapper: every sync tool runs in a worker thread,
    inside an audit record.

    The tools block on requests; FastMCP calls a sync tool on the event loop, so
    one slow report would stall every other user of the process. anyio copies the
    request's contextvars into the worker, so get_access_token() still sees this
    request's token there — for the tool's own POS calls and for the audit trail."""

    def __init__(self, mcp, surface: str = FULL_MODE) -> None:
        self._mcp = mcp
        self._surface = surface

    def tool(self, *args, **kwargs):
        real = self._mcp.tool(*args, **kwargs)

        def deco(fn):
            if inspect.iscoroutinefunction(fn):
                # Would skip the audit record; wrap it here before allowing one.
                raise RuntimeError(f"async tool {fn.__name__} has no audited remote path.")

            @functools.wraps(fn)
            async def threaded(*a, **kw):
                return await anyio.to_thread.run_sync(functools.partial(
                    _audited, fn, fn.__name__, self._surface, a, kw))

            # FastMCP builds the schema from the signature and resolves string
            # annotations against the function's globals; hand it the resolved
            # signature so the wrapper's (server.py) globals never matter.
            threaded.__signature__ = inspect.signature(fn, eval_str=True)
            real(threaded)
            return fn

        return deco


def register_tools(mcp, mode: str | None = None) -> None:
    """Register the tool surface for `mode` (default: env GUNSTORE_MCP_MODE).

    cpa mode routes every registration through FilteredMCP so only the
    allowlisted tools ever reach tools/list."""
    mode = mode or get_mode()
    if mode not in (FULL_MODE, CPA_MODE):
        raise RuntimeError(f"Unknown server mode {mode!r}.")
    target = mcp if mode == FULL_MODE else FilteredMCP(mcp, CPA_TOOL_NAMES)
    generic.register(target)
    curated.register(target)
    distributor.register(target)
    reports.register(target)
    shopfloor.register(target)
    range_board.register(target)
    orders.register(target)
    customers.register(target)
    services.register(target)
    compliance.register(target)
    hr.register(target)


def _http_settings() -> dict:
    """FastMCP kwargs for the remote connector.

    The POS host's proxy maps https://pos.<domain>/connector/<mode>/mcp to this
    process's /mcp and forwards /.well-known/oauth-protected-resource/connector/
    <mode>/mcp unchanged (the metadata route follows the public URL, RFC 9728)."""
    cfg = get_config()
    if distributor.actions_enabled():
        # A developer .env can carry this; the remote surface never opens it.
        raise RuntimeError("The distributor action gate stays off on the remote connector — "
            "unset GUNSTORE_MCP_DISTRIBUTOR_ACTIONS.")
    if curated.gunbroker_actions_enabled() and get_mode() != FULL_MODE:
        raise RuntimeError("GUNSTORE_MCP_GUNBROKER_ACTIONS opens only the full connector.")
    # The GunBroker writes may open on the full connector: the POS deploy sets the
    # gate from that store's own GunBroker switch, because a store that can list a
    # gun must be able to end the listing (an end GunBroker does not confirm).
    public = urlparse(cfg.public_url)
    return {
        "host": cfg.host,
        "port": cfg.port,
        "streamable_http_path": "/mcp",
        # Stateless: every request runs the server in ITS OWN context, so tools see
        # that request's token (a refreshed one included), nothing is kept between
        # requests, and a session id is not a credential anybody could replay.
        "stateless_http": True,
        "token_verifier": FrappeTokenVerifier(cfg.backend_url, cfg.backend_host, cfg.timeout,
                                              surface=get_mode()),
        "auth": AuthSettings(issuer_url=cfg.base_url, resource_server_url=cfg.public_url),
        # Bound to loopback, the SDK would otherwise allow only localhost Host
        # headers and refuse every request the proxy forwards.
        "transport_security": TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public.netloc, "127.0.0.1:*", "localhost:*"],
            # Remote MCP clients connect server-to-server (no Origin); these cover
            # the browser-side ones (Claude, ChatGPT).
            allowed_origins=[f"{public.scheme}://{public.netloc}", "https://claude.ai",
                             "https://chatgpt.com", "https://chat.openai.com"],
        ),
    }


def build() -> FastMCP:
    mode = get_mode()
    remote = get_transport() == HTTP
    kwargs = _http_settings() if remote else {}
    mcp = FastMCP("gunstore-pos" if mode == FULL_MODE else "gunstore-pos-cpa", **kwargs)
    # FastMCP takes no version; without this serverInfo reports the mcp SDK's
    # version, so clients can't tell which build of ours they're talking to.
    mcp._mcp_server.version = __version__
    register_tools(ThreadedMCP(mcp, mode) if remote else mcp, mode)
    return mcp


def http_app(server: FastMCP):
    """The remote ASGI app, with the protected-resource metadata served by us.

    The SDK's own metadata writes the issuer through pydantic, which appends a
    "/" (https://pos.example.com/), while the POS reports its issuer without one;
    RFC 8414 §3.3 wants them identical and a strict client stops there. Our route
    is inserted first, so it answers instead."""
    cfg = get_config()
    app = server.streamable_http_app()
    path = "/.well-known/oauth-protected-resource" + urlparse(cfg.public_url).path
    # scopes_supported: the one scope a dynamically registered POS client holds
    # ("all"); clients (ChatGPT included) request what is advertised here, and
    # the POS refuses any scope the client was not registered with.
    body = {"resource": cfg.public_url, "authorization_servers": [cfg.base_url],
            "scopes_supported": ["all"], "bearer_methods_supported": ["header"]}
    app.router.routes.insert(0, Route(path, lambda request: JSONResponse(body),
                                      methods=["GET"]))
    return app


mcp = build()


def main() -> None:
    if get_transport() != HTTP:
        mcp.run("stdio")
        return
    import uvicorn

    cfg = get_config()
    uvicorn.run(http_app(mcp), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
