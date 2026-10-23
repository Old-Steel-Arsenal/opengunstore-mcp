"""Thin HTTP client over the Frappe v16 REST API.

Mirrors the session/retry/error pattern of the in-app integration clients
(apps/ffl_integrations/.../fastbound/client.py, payroc/client.py). One `_request`
chokepoint unwraps Frappe's `data`/`message` envelope and turns error responses
into a clean FrappeAPIError carrying exc_type + the user-facing _server_messages.
"""
from __future__ import annotations

import json
import os
from http.cookiejar import DefaultCookiePolicy
from typing import Any
from urllib.parse import quote, urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import HTTP, get_config
from .modes import (
    CPA_METHOD_ALLOWLIST,
    CPA_MODE,
    CPA_SETTINGS_READ_BLOCKLIST,
    CpaModeRefused,
    get_mode,
)


_remote_session: requests.Session | None = None


def remote_session() -> requests.Session:
    """One connection pool for the whole remote process (headers stay per call)."""
    global _remote_session
    if _remote_session is None:
        s = _build_session()
        # Shared by every user: a Set-Cookie (a login, a session) must never be
        # replayed on somebody else's request.
        s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        _remote_session = s
    return _remote_session


def backend_headers(bearer: str) -> dict:
    """Per-request headers for a POS call on the user's behalf. With
    FRAPPE_INTERNAL_URL the call goes to the local frontend, which picks the site
    by Host — so the public site name is sent as Host."""
    cfg = get_config()
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json",
               "Content-Type": "application/json"}
    if cfg.backend_host:
        headers["Host"] = cfg.backend_host
    return headers


class RemoteAuthMissing(RuntimeError):
    """The remote (http) server was asked to act without the user's token, or
    to do something only the local server may do."""


class FrappeAPIError(RuntimeError):
    def __init__(self, status: int, exc_type: str | None, messages: list[str], raw: str = "") -> None:
        self.status = status
        self.exc_type = exc_type
        self.messages = messages
        detail = "; ".join(m for m in messages if m) or exc_type or raw or "request failed"
        super().__init__(f"[{status}] {detail}")


def _build_session() -> requests.Session:
    s = requests.Session()
    # Retry idempotent methods only. POST (create / run_method / submit / cancel)
    # is excluded — those writes aren't idempotent and Frappe has no dedup key,
    # so a blind retry could double-apply.
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "PUT", "DELETE", "HEAD", "OPTIONS"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _normalize_fields(meta: Any) -> list[dict]:
    if isinstance(meta, dict):
        if isinstance(meta.get("fields"), list):
            return meta["fields"]
        docs = meta.get("docs")
        if isinstance(docs, list) and docs and isinstance(docs[0], dict):
            return docs[0].get("fields") or []
    return []


class FrappeClient:
    def __init__(self, bearer: str | None = None) -> None:
        cfg = get_config()
        self.remote = cfg.transport == HTTP
        if self.remote and not bearer:
            # Never fall back to a key on the remote server — it has none, and a
            # call without the user's token must not run as anybody.
            raise RemoteAuthMissing("No OAuth token on this request — reconnect the connector.")
        self.base_url = cfg.backend_url if self.remote else cfg.base_url
        self.timeout = cfg.timeout
        self.mode = get_mode()
        # Remote clients are built per call but share one connection pool; the
        # per-user Authorization rides on each request, never on the shared session.
        self.session = remote_session() if self.remote else _build_session()
        self._headers = backend_headers(bearer) if self.remote else {}
        if not self.remote:
            self.session.headers.update({
                "Authorization": f"token {cfg.api_key}:{cfg.api_secret}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            })

    # ------------------------------------------- cpa read-only gate (layer 2/3)
    # The registration layer (modes.FilteredMCP) already removes the write
    # tools from tools/list; these guards make the CLIENT itself refuse, so a
    # future tool wired straight to the client cannot bypass the mode.
    def _refuse_cpa(self, what: str) -> None:
        raise CpaModeRefused(
            f"cpa mode is read-only: {what} is not available. "
            "Run the full-mode server for writes."
        )

    def _cpa_check_write(self, what: str) -> None:
        if self.mode == CPA_MODE:
            self._refuse_cpa(what)

    def _cpa_check_method(self, method: str) -> None:
        # Exact-name allowlist — no prefix wildcards, no HTTP-verb heuristics.
        if self.mode == CPA_MODE and method not in CPA_METHOD_ALLOWLIST:
            self._refuse_cpa(f"method '{method}' (not on the read-only allowlist)")

    def _cpa_check_read(self, doctype: str) -> None:
        if self.mode == CPA_MODE and doctype in CPA_SETTINGS_READ_BLOCKLIST:
            self._refuse_cpa(f"reading '{doctype}' (integration settings)")

    # -------------------------------------------------- HTTP plumbing
    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json_body: Any = None, timeout: int | None = None) -> Any:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        resp = self.session.request(
            method, url,
            params=params,
            data=json.dumps(json_body) if json_body is not None else None,
            headers=self._headers or None,
            timeout=timeout or self.timeout,
        )
        if resp.status_code >= 400:
            raise self._error(resp)
        if not resp.content:
            return None
        try:
            body = resp.json()
        except ValueError:
            return resp.text
        if isinstance(body, dict):
            if "data" in body:
                return body["data"]
            if "message" in body:
                return body["message"]
        return body

    @staticmethod
    def _error(resp: requests.Response) -> FrappeAPIError:
        try:
            body = resp.json()
        except ValueError:
            return FrappeAPIError(resp.status_code, None, [], resp.text[:500])
        messages: list[str] = []
        raw_msgs = body.get("_server_messages")
        if raw_msgs:
            try:
                for m in json.loads(raw_msgs):
                    try:
                        messages.append(json.loads(m).get("message", str(m)))
                    except (ValueError, TypeError, AttributeError):
                        messages.append(str(m))
            except (ValueError, TypeError):
                messages.append(str(raw_msgs))
        if not messages and body.get("exception"):
            messages.append(str(body["exception"]))
        return FrappeAPIError(resp.status_code, body.get("exc_type"), messages)

    @staticmethod
    def _res(doctype: str, name: str | None = None) -> str:
        base = f"/api/resource/{quote(doctype)}"
        return f"{base}/{quote(str(name), safe='')}" if name is not None else base

    # -------------------------------------------------- resource ops
    def list_documents(self, doctype: str, *, fields: list | None = None,
                       filters: Any = None, limit: int = 20, start: int = 0,
                       order_by: str | None = None) -> Any:
        self._cpa_check_read(doctype)
        params: dict[str, Any] = {"limit_page_length": limit, "limit_start": start}
        if fields:
            params["fields"] = json.dumps(fields)
        if filters:
            params["filters"] = json.dumps(filters)
        if order_by:
            params["order_by"] = order_by
        return self._request("GET", self._res(doctype), params=params)

    def get_document(self, doctype: str, name: str) -> Any:
        self._cpa_check_read(doctype)
        return self._request("GET", self._res(doctype, name))

    def create_document(self, doctype: str, values: dict) -> Any:
        self._cpa_check_write("create_document")
        return self._request("POST", self._res(doctype), json_body=values)

    def update_document(self, doctype: str, name: str, values: dict) -> Any:
        self._cpa_check_write("update_document")
        return self._request("PUT", self._res(doctype, name), json_body=values)

    def delete_document(self, doctype: str, name: str) -> Any:
        self._cpa_check_write("delete_document")
        return self._request("DELETE", self._res(doctype, name))

    # -------------------------------------------------- method / report ops
    def call_method(self, method: str, kwargs: dict | None = None, *,
                    timeout: int | None = None) -> Any:
        self._cpa_check_method(method)
        return self._request("POST", f"/api/method/{method}", json_body=kwargs or {},
                             timeout=timeout)

    def get_method(self, method: str, params: dict | None = None, *,
                   timeout: int | None = None) -> Any:
        """A whitelisted method that only answers GET (``methods=["GET"]``)."""
        self._cpa_check_method(method)
        return self._request("GET", f"/api/method/{method}",
                             params={k: v for k, v in (params or {}).items() if v is not None},
                             timeout=timeout)

    def submit_document(self, doctype: str, name: str) -> Any:
        self._cpa_check_write("submit_document")
        doc = self.get_document(doctype, name)
        if isinstance(doc, dict):
            # Frappe masks Password fields on GET (an all-"*" string). Never echo
            # those back on submit — re-sending the mask could corrupt or expose
            # the field. (Submittable docs in this domain have no Password fields.)
            doc = {k: v for k, v in doc.items()
                   if not (isinstance(v, str) and v and set(v) == {"*"})}
        return self._request("POST", "/api/method/frappe.client.submit", json_body={"doc": doc})

    def cancel_document(self, doctype: str, name: str) -> Any:
        self._cpa_check_write("cancel_document")
        return self._request("POST", "/api/method/frappe.client.cancel",
                             json_body={"doctype": doctype, "name": name})

    def upload_file(self, file_path: str, *, doctype: str | None = None,
                    docname: str | None = None, fieldname: str | None = None,
                    is_private: bool = True, filename: str | None = None) -> Any:
        """Multipart upload to Frappe's /api/method/upload_file — the one call
        that can't go through _request (JSON-only). Returns the created File doc."""
        self._cpa_check_write("upload_file")
        if self.remote:
            # file_path is a path on THIS server; remotely that would let a caller
            # upload the server's own files. The tool is not registered remotely
            # either — this is the second lock.
            raise RemoteAuthMissing("File upload from a server path is not available remotely.")
        url = urljoin(self.base_url + "/", "api/method/upload_file")
        data: dict[str, str] = {"is_private": "1" if is_private else "0"}
        if doctype:
            data["doctype"] = doctype
        if docname:
            data["docname"] = docname
        if fieldname:
            data["fieldname"] = fieldname
        with open(file_path, "rb") as fh:
            resp = self.session.post(
                url,
                data=data,
                files={"file": (filename or os.path.basename(file_path), fh)},
                # Drop the session's application/json so requests can set the
                # multipart boundary itself.
                headers={"Content-Type": None},
                timeout=self.timeout,
            )
        if resp.status_code >= 400:
            raise self._error(resp)
        body = resp.json()
        if isinstance(body, dict) and "message" in body:
            return body["message"]
        return body

    def run_report(self, report_name: str, filters: dict | None = None) -> Any:
        # Same chokepoint semantics as call_method — query_report.run is ON the
        # cpa allowlist, so this passes in both modes; listed for structure.
        self._cpa_check_method("frappe.desk.query_report.run")
        params: dict[str, Any] = {"report_name": report_name}
        if filters:
            params["filters"] = json.dumps(filters)
        return self._request("GET", "/api/method/frappe.desk.query_report.run", params=params)

    def describe_doctype(self, doctype: str) -> list[dict]:
        """Return the doctype's fields incl. custom fields. Prefer v2 meta (which
        merges custom fields); fall back to DocType.fields + Custom Field query."""
        try:
            fields = _normalize_fields(self._request("GET", f"/api/v2/doctype/{quote(doctype)}/meta"))
            if fields:
                return fields
        except FrappeAPIError:
            pass
        fields = []
        try:
            dt = self.get_document("DocType", doctype)
            if isinstance(dt, dict):
                fields = list(dt.get("fields") or [])
        except FrappeAPIError:
            pass
        try:
            customs = self.list_documents(
                "Custom Field", filters=[["dt", "=", doctype]],
                fields=["fieldname", "label", "fieldtype", "options", "reqd"], limit=0,
            )
            if customs:
                fields.extend(customs)
        except FrappeAPIError:
            pass
        return fields


_client: FrappeClient | None = None


def get_client() -> FrappeClient:
    """The local (stdio) server shares one API-key client. The remote (http)
    server builds one per call from the signed-in user's bearer token, so every
    Frappe call runs as that user."""
    if get_config().transport == HTTP:
        from mcp.server.auth.middleware.auth_context import get_access_token

        tok = get_access_token()
        # lazy: a fresh session per tool call; pool per token if call volume grows.
        return FrappeClient(bearer=tok.token if tok else None)
    global _client
    if _client is None:
        _client = FrappeClient()
    return _client
