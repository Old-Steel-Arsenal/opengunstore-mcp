"""Environment-driven configuration (loaded once, lazily)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# Schema / permission doctypes blocked from writes by default. Changing these is
# a code/migration concern, not something to do ad-hoc against production.
DEFAULT_WRITE_DENYLIST = (
    "DocType", "Custom Field", "Property Setter", "Server Script",
    "Client Script", "Role", "DocPerm", "Custom DocPerm", "User",
)


STDIO = "stdio"
HTTP = "http"
_VALID_TRANSPORTS = (STDIO, HTTP)


def get_transport() -> str:
    """GUNSTORE_MCP_TRANSPORT: stdio (default — the local, API-key server) or
    http (the remote connector: OAuth bearer tokens, no API key). An unknown
    value refuses to start rather than guessing which auth model was meant."""
    _load_env()
    raw = (os.environ.get("GUNSTORE_MCP_TRANSPORT") or STDIO).strip().lower()
    if raw not in _VALID_TRANSPORTS:
        raise RuntimeError(
            f"GUNSTORE_MCP_TRANSPORT must be one of {_VALID_TRANSPORTS} (got {raw!r})."
        )
    return raw


def store_host() -> str:
    """The POS this server points at (the host of FRAPPE_BASE_URL), "" when unset.

    Every store runs its own server with the same tool names, so with several
    connected this host is what tells them apart: it goes into the server's
    instructions and into each confirm refusal."""
    _load_env()
    return urlparse((os.environ.get("FRAPPE_BASE_URL") or "").strip()).netloc


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    api_secret: str
    timeout: int
    write_denylist: frozenset[str]
    transport: str = STDIO
    # http transport only: the public resource URL a connector is given
    # (https://pos.example.com/connector/cpa/mcp), the listen address and port
    # behind the proxy, and where POS calls actually go.
    public_url: str = ""
    port: int = 0
    host: str = "127.0.0.1"
    # FRAPPE_INTERNAL_URL: the connector runs on the POS host, so calls can go
    # straight to the local frontend (http://127.0.0.1:8080) with the site's Host
    # header instead of out and back in through the public name. The public
    # FRAPPE_BASE_URL stays the OAuth issuer either way.
    backend_url: str = ""
    backend_host: str = ""


_config: Config | None = None


def secure_url(url: str) -> bool:
    """A URL credentials may travel to: https, or plain http only for a local dev stack."""
    return url.startswith(("https://", "http://localhost:", "http://127.0.0.1:",
                           "http://dev.localhost:"))


def _load_env() -> None:
    # .env sits at the repo root, two levels up from gunstore_mcp/config.py.
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()  # fall back to process env / cwd .env


def get_config() -> Config:
    global _config
    if _config is not None:
        return _config

    _load_env()
    transport = get_transport()
    base_url = (os.environ.get("FRAPPE_BASE_URL") or "").rstrip("/")
    public_url = port = None
    if transport == HTTP:
        # The remote server holds NO key: every call carries the signed-in
        # user's own OAuth token. A key left in the env is ignored, not used.
        api_key = api_secret = ""
        public_url = (os.environ.get("GUNSTORE_MCP_PUBLIC_URL") or "").rstrip("/")
        raw_port = os.environ.get("GUNSTORE_MCP_PORT") or ""
        if not (secure_url(base_url) and secure_url(public_url)
                and public_url.endswith("/mcp") and "//" not in urlparse(public_url).path
                and raw_port.isdigit()):
            raise RuntimeError(
                "http transport needs FRAPPE_BASE_URL (https://…), GUNSTORE_MCP_PUBLIC_URL "
                "(https://…/mcp) — plain http only for a local stack — and GUNSTORE_MCP_PORT."
            )
        port = int(raw_port)
        internal = (os.environ.get("FRAPPE_INTERNAL_URL") or "").rstrip("/")
        if internal and not internal.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise RuntimeError("FRAPPE_INTERNAL_URL must be a loopback URL (http://127.0.0.1:<port>).")
        host = (os.environ.get("GUNSTORE_MCP_HOST") or "127.0.0.1").strip()
        if host not in ("127.0.0.1", "localhost", "::1"):
            # Plain HTTP carrying users' tokens: only the local proxy may reach it.
            raise RuntimeError("GUNSTORE_MCP_HOST must be a loopback address (run with host networking).")
    else:
        api_key = os.environ.get("FRAPPE_API_KEY") or ""
        api_secret = os.environ.get("FRAPPE_API_SECRET") or ""
        if not (base_url and api_key and api_secret):
            raise RuntimeError(
                "Missing Frappe credentials. Set FRAPPE_BASE_URL, FRAPPE_API_KEY and "
                "FRAPPE_API_SECRET in mcp/.env (see mcp/.env.example)."
            )

    raw = os.environ.get("FRAPPE_WRITE_DENYLIST")
    denylist = (
        frozenset(d.strip() for d in raw.split(",") if d.strip())
        if raw is not None
        else frozenset(DEFAULT_WRITE_DENYLIST)
    )

    raw_timeout = os.environ.get("FRAPPE_TIMEOUT") or "30"
    try:
        timeout = int(raw_timeout)
    except ValueError:
        raise RuntimeError(f"FRAPPE_TIMEOUT must be an integer (got {raw_timeout!r}).")

    _config = Config(
        base_url=base_url,
        api_key=api_key,
        api_secret=api_secret,
        timeout=timeout,
        write_denylist=denylist,
        transport=transport,
        public_url=public_url or "",
        port=port or 0,
        host=host if transport == HTTP else "127.0.0.1",
        backend_url=(internal or base_url) if transport == HTTP else base_url,
        backend_host=urlparse(base_url).netloc if transport == HTTP and internal else "",
    )
    return _config
