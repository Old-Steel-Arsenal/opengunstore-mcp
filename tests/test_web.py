"""The WordPress abilities bridge (GUNSTORE_MCP_MODE=web), end to end against two
stub HTTP servers: one plays the WordPress site (core's Abilities REST API), the
other the POS (connector_identity + log_connector_call). Nothing is mocked between
the MCP request and those two servers."""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qsl, unquote, urlparse

from mcp.server.fastmcp import FastMCP

from gunstore_mcp import audit, config, web

PASSWORD = "abcd EFGH 1234 ijkl MNOP 5678"
USER = "bridge-bot"
BASIC = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
SECRETS = (PASSWORD, PASSWORD.replace(" ", ""), BASIC)

ORDERS = "woocommerce/orders-query"
PRODUCTS = "woocommerce/products-query"
UPDATE = "woocommerce/order-update-status"  # a write: must never be bridged
API = "/wp-json/wp-abilities/v1/abilities/"
IDENTITY = "/api/method/ffl_core.api.connector.connector_identity"
LOG = "/api/method/ffl_core.api.connector.log_connector_call"


def _ability(name, readonly=True, schema=None, **extra):
	meta = {"show_in_rest": True, "annotations": {"readonly": readonly, "destructive": False,
		"idempotent": True}}
	if readonly == "absent":
		meta = {"show_in_rest": True}
	return {"name": name, "label": name.split("/")[1].replace("-", " ").title(),
		"description": f"Does {name}.", "category": "test", "meta": meta, **extra,
		"input_schema": schema if schema is not None else {"type": "object", "properties": {
			"page": {"type": "integer", "default": 1}, "per_page": {"type": "integer"},
			"include_line_items": {"type": "boolean"},
			"status": {"type": "array", "items": {"type": "string"}}}, "default": {}},
		"output_schema": {"type": "object"}}


class _Server:
	"""A stub HTTP server on a free loopback port, answering through `handle`."""

	def __init__(self, handle):
		self.handle = handle
		self.seen = []
		outer = self

		class H(BaseHTTPRequestHandler):
			def _do(self):
				n = int(self.headers.get("Content-Length") or 0)
				body = self.rfile.read(n).decode() if n else ""
				outer.seen.append((self.command, self.path, dict(self.headers), body))
				status, payload, delay = outer.handle(self.command, self.path, dict(self.headers), body)
				if delay:
					time.sleep(delay)
				data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
				try:
					self.send_response(status)
					self.send_header("Content-Type", "application/json")
					self.send_header("Content-Length", str(len(data)))
					self.end_headers()
					self.wfile.write(data)
				except (BrokenPipeError, ConnectionResetError):
					pass

			do_GET = do_POST = do_PUT = do_DELETE = _do

			def log_message(self, *a):
				pass

		self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
		self.srv.daemon_threads = True
		self.url = f"http://127.0.0.1:{self.srv.server_port}"
		threading.Thread(target=self.srv.serve_forever, args=(0.05,), daemon=True).start()

	def close(self):
		self.srv.shutdown()
		self.srv.server_close()


class FakeWP:
	def __init__(self, abilities):
		self.abilities = {a["name"]: a for a in abilities}
		self.down = False
		self.run_reply = None  # (status, payload, delay) overriding the default echo
		self.server = _Server(self.handle)

	def handle(self, method, path, headers, body):
		if self.down:
			return 503, b"<html>maintenance</html>", 0
		if method != "GET":
			return 405, {"code": "stub", "message": "the bridge sent a non-GET request"}, 0
		parts = urlparse(path)
		if not parts.path.startswith(API):
			return 404, {"code": "rest_no_route", "message": "No route"}, 0
		rest = unquote(parts.path[len(API):])
		if headers.get("Authorization") != f"Basic {BASIC}":
			return 401, {"code": "incorrect_password", "message": "bad"}, 0
		if rest.endswith("/run"):
			name = rest[:-4]
			if name not in self.abilities:
				return 404, {"code": "rest_ability_not_found", "message": "Ability not found."}, 0
			if self.run_reply:
				return self.run_reply
			return 200, {"ran": name, "query": parse_qsl(parts.query, keep_blank_values=True)}, 0
		if rest not in self.abilities:
			return 404, {"code": "rest_ability_not_found", "message": "Ability not found."}, 0
		return 200, self.abilities[rest], 0

	def runs(self):
		return [p for m, p, h, b in self.server.seen if "/run" in p]


class FakePOS:
	"""Users by token: (user, web_access). Records every audit post."""

	def __init__(self):
		self.users = {"T-VIEW": ("viewer@x.com", "viewer"), "T-MGR": ("mgr@x.com", "manager"),
			"T-NONE": ("clerk@x.com", "")}
		self.refuse = ""  # a reason: log_connector_call(started) then answers 403
		self.audits = []
		self.surfaces = []
		self.server = _Server(self.handle)

	def handle(self, method, path, headers, body):
		token = (headers.get("Authorization") or "").removeprefix("Bearer ")
		if path.startswith(IDENTITY):
			self.surfaces.append(dict(parse_qsl(urlparse(path).query)).get("surface"))
			if token not in self.users:
				return 401, {}, 0
			user, web_access = self.users[token]
			return 200, {"message": {"user": user, "connector": True, "web_access": web_access}}, 0
		if path == LOG:
			entry = json.loads(body)
			self.audits.append((self.users.get(token, ("?",))[0], entry))
			if self.refuse and entry["phase"] == "started":
				return 403, {"exc_type": "PermissionError", "_server_messages": json.dumps(
					[json.dumps({"message": self.refuse})])}, 0
			return 200, {"message": {"log": "CAL-1"}}, 0
		return 404, {}, 0


def _env(pos, wp_url, plain=(ORDERS,), restricted=(), **extra):
	env = {k: v for k, v in os.environ.items() if not k.startswith(("GUNSTORE_MCP_", "FRAPPE_", "WEB_"))}
	env.update({
		"GUNSTORE_MCP_TRANSPORT": "http", "GUNSTORE_MCP_MODE": "web",
		"FRAPPE_BASE_URL": pos.server.url if pos else "https://pos.example.com",
		"GUNSTORE_MCP_PUBLIC_URL": "http://localhost:8790/mcp", "GUNSTORE_MCP_PORT": "8790",
		"WEB_SITE_URL": wp_url, "WEB_SITE_USER": USER, "WEB_SITE_APP_PASSWORD": PASSWORD,
		"WEB_ABILITIES": ",".join(plain), "WEB_ABILITIES_RESTRICTED": ",".join(restricted)})
	env.update(extra)
	return patch.dict(os.environ, env, clear=True)


class _Fresh:
	"""Config is cached per process; each test resolves it anew, without anybody's .env."""

	def setUp(self):
		for p in (patch.object(config, "_config", None), patch.object(config, "_load_env", lambda: None),
				patch.object(web, "_load_env", lambda: None)):
			p.start()
			self.addCleanup(p.stop)


def _parse(r):
	assert r.status_code == 200, r.text
	for line in r.text.splitlines():
		if line.startswith("data: "):
			return json.loads(line[6:])
	return r.json()


class Config(_Fresh, unittest.TestCase):
	def test_both_lists_empty_refuse_to_start(self):
		with _env(None, "https://shop.example.com", plain=(), restricted=()):
			with self.assertRaisesRegex(RuntimeError, "WEB_ABILITIES"):
				web.load_config()

	def test_each_missing_setting_is_named(self):
		for var in (web.SITE_URL, web.SITE_USER, web.SITE_PASSWORD):
			with _env(None, "https://shop.example.com", **{var: ""}), patch.object(config, "_config", None):
				with self.assertRaises(RuntimeError) as ctx:
					web.load_config()
			self.assertIn(var, str(ctx.exception))
			self.assertNotIn(PASSWORD, str(ctx.exception))

	def test_the_site_url_is_https_or_local_and_carries_no_credentials(self):
		for bad in ("http://shop.example.com", "https://bot:pw@shop.example.com",
				"https://shop.example.com/?x=1", "ftp://shop.example.com"):
			with _env(None, bad):
				with self.assertRaises(RuntimeError, msg=bad):
					web.load_config()
		for good in ("https://shop.example.com/", "https://example.com/shop", "http://localhost:8090"):
			with _env(None, good):
				self.assertEqual(web.load_config().site_url, good.rstrip("/"))

	def test_bad_ambiguous_or_clashing_names_refuse_to_start(self):
		for plain, restricted in (([ORDERS, "Not A Name"], []), ([ORDERS], [ORDERS]),
				(["a/b-c"], ["a-b/c"])):
			with _env(None, "https://shop.example.com", plain=plain, restricted=restricted):
				with self.assertRaises(RuntimeError, msg=(plain, restricted)):
					web.load_config()

	def test_stdio_refuses_to_start(self):
		from gunstore_mcp import server
		with _env(None, "https://shop.example.com", GUNSTORE_MCP_TRANSPORT="stdio"):
			with self.assertRaisesRegex(RuntimeError, "remote connector"):
				server.build()

	def test_tool_names_are_mechanical(self):
		self.assertEqual(web.tool_name("woocommerce/orders-query"), "woocommerce_orders_query")


class ReadOnlyGate(_Fresh, unittest.TestCase):
	"""Gate 2: whatever the lists say, only abilities the site marks readonly=true
	become tools — and nothing else is ever run."""

	def setUp(self):
		super().setUp()
		self.wp = FakeWP([_ability(ORDERS), _ability(UPDATE, readonly=False),
			_ability("example/no-annotations", readonly="absent"),
			_ability("example/string-true", readonly="true"),
			_ability("example/null", readonly=None)])
		self.addCleanup(self.wp.server.close)

	def _names(self, plain, restricted):
		mcp = FastMCP("t")
		with _env(None, self.wp.server.url, plain=plain, restricted=restricted), \
				self.assertLogs("gunstore_mcp.web", "WARNING") as logs:
			web.register(mcp)
		return {t.name for t in mcp._tool_manager.list_tools()}, "\n".join(logs.output)

	def test_no_configuration_bridges_a_non_readonly_ability(self):
		others = [UPDATE, "example/no-annotations", "example/string-true", "example/null",
			"example/not-on-site"]
		for plain, restricted in (([ORDERS, *others], []), ([], [ORDERS, *others]),
				([ORDERS, UPDATE, "example/null"], ["example/no-annotations", "example/string-true"])):
			names, logs = self._names(plain, restricted)
			self.assertEqual(names, {"woocommerce_orders_query"}, (plain, restricted))
			for skipped in (set(plain) | set(restricted)) - {ORDERS}:
				self.assertIn(skipped, logs, f"{skipped} must be named in the startup log")
		self.assertEqual(self.wp.runs(), [], "the catalog never runs an ability")

	def test_tool_shape(self):
		schema = {"type": "object", "properties": {"id": {"type": "integer", "required": True},
			"email": {"type": "string"}}}
		self.wp.abilities[PRODUCTS] = _ability(PRODUCTS, schema=schema)
		mcp = FastMCP("t")
		with _env(None, self.wp.server.url, plain=[ORDERS], restricted=[PRODUCTS]):
			web.register(mcp)
		tools = {t.name: t for t in mcp._tool_manager.list_tools()}
		products = tools["woocommerce_products_query"]
		self.assertEqual(products.parameters, {"type": "object", "properties": {
			"id": {"type": "integer"}, "email": {"type": "string"}}, "required": ["id"]})
		self.assertTrue(products.annotations.readOnlyHint)
		self.assertFalse(products.annotations.destructiveHint)
		self.assertIn("Website Manager", products.description)
		self.assertNotIn("Website Manager", tools["woocommerce_orders_query"].description)
		self.assertEqual(tools["woocommerce_orders_query"].parameters["properties"]["page"],
			{"type": "integer", "default": 1})


class Encoding(unittest.TestCase):
	def test_input_is_written_the_way_wordpress_reads_get_input(self):
		self.assertEqual(web._query("input", {"a": True, "b": False, "c": [1, "x"],
			"d": {"e": None, "f": 2.5}, "g": [], "h": {}}, []), [
			("input[a]", "1"), ("input[b]", "0"), ("input[c][0]", "1"), ("input[c][1]", "x"),
			("input[d][f]", "2.5")])
		self.assertEqual(web._query("input", None, []), [])
		self.assertEqual(web._query("input", "abc", []), [("input", "abc")])


class Bridge(_Fresh, unittest.TestCase):
	"""Through the real ASGI app, as a client would call it."""

	def setUp(self):
		super().setUp()
		self.pos = FakePOS()
		self.wp = FakeWP([_ability(ORDERS), _ability(PRODUCTS)])
		self.addCleanup(self.pos.server.close)
		self.addCleanup(self.wp.server.close)
		p = patch.object(audit, "_submit", lambda fn, *a: fn(*a))
		p.start()
		self.addCleanup(p.stop)
		self.outputs = []
		self.log = io.StringIO()
		handler = logging.StreamHandler(self.log)
		root = logging.getLogger()
		old = root.level
		root.addHandler(handler)
		root.setLevel(logging.DEBUG)
		self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(old)))

	def _client(self, restricted=(PRODUCTS,), plain=(ORDERS,)):
		from starlette.testclient import TestClient

		from gunstore_mcp import server
		self.env = _env(self.pos, self.wp.server.url, plain=plain, restricted=restricted)
		self.env.start()
		self.addCleanup(self.env.stop)
		app = server.http_app(server.build())
		client = TestClient(app, base_url="http://localhost:8790")
		client.__enter__()
		self.addCleanup(client.__exit__, None, None, None)
		return client

	def _rpc(self, c, token, method, params=None):
		r = c.post("/mcp", headers={"Authorization": f"Bearer {token}",
			"Accept": "application/json, text/event-stream"},
			json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
		self.outputs.append(r.text)
		return _parse(r)

	def _call(self, c, token, tool, args=None):
		res = self._rpc(c, token, "tools/call", {"name": tool, "arguments": args or {}})["result"]
		return res["isError"], res["content"][0]["text"]

	def test_server_identity_and_tools(self):
		from gunstore_mcp import server
		with _env(self.pos, self.wp.server.url, restricted=[PRODUCTS]):
			mcp = server.build()
		self.assertEqual(mcp.name, "gunstore-web-ro")
		c = self._client()
		tools = self._rpc(c, "T-VIEW", "tools/list")["result"]["tools"]
		self.assertEqual({t["name"] for t in tools}, {"woocommerce_orders_query", "woocommerce_products_query"})
		self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
		r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
		self.assertEqual(r.status_code, 401, "no token, no tools")
		self.assertIn("/.well-known/oauth-protected-resource/mcp", r.headers["www-authenticate"])

	def test_a_viewer_reads_and_the_call_is_audited_first(self):
		c = self._client()
		err, text = self._call(c, "T-VIEW", "woocommerce_orders_query",
			{"per_page": 5, "include_line_items": False, "status": ["processing", "on-hold"]})
		self.assertFalse(err, text)
		body = json.loads(text)
		self.assertEqual(body["ran"], ORDERS)
		self.assertEqual(body["query"], [["input[per_page]", "5"], ["input[include_line_items]", "0"],
			["input[status][0]", "processing"], ["input[status][1]", "on-hold"]])
		self.assertEqual(set(self.pos.surfaces), {"web_ro"})
		self.assertEqual([(u, e["phase"], e["tool"], e["surface"]) for u, e in self.pos.audits], [
			("viewer@x.com", "started", "woocommerce_orders_query", "web_ro"),
			("viewer@x.com", "ok", "woocommerce_orders_query", "web_ro")])
		self.assertEqual(json.loads(self.pos.audits[0][1]["arguments"])["per_page"], 5)
		self.assertTrue(all(m == "GET" for m, *_ in self.wp.server.seen), "the site only ever sees GET")

	def test_no_arguments_sends_no_input(self):
		c = self._client()
		err, text = self._call(c, "T-VIEW", "woocommerce_orders_query")
		self.assertFalse(err, text)
		self.assertEqual(json.loads(text)["query"], [])

	def test_restricted_abilities_need_manager(self):
		c = self._client()
		err, text = self._call(c, "T-VIEW", "woocommerce_products_query")
		self.assertTrue(err)
		self.assertIn("Website Manager", text)
		self.assertEqual(self.wp.runs(), [], "a refused call never reaches the site")
		self.assertEqual([e["phase"] for _, e in self.pos.audits], ["started", "failed"])
		err, text = self._call(c, "T-MGR", "woocommerce_products_query")
		self.assertFalse(err, text)
		self.assertEqual(len(self.wp.runs()), 1)

	def test_a_user_without_a_website_role_is_refused_even_if_the_pos_lets_it_start(self):
		c = self._client()
		err, text = self._call(c, "T-NONE", "woocommerce_orders_query")
		self.assertTrue(err)
		self.assertIn("no website access role", text)
		self.assertEqual(self.wp.runs(), [])

	def test_a_call_the_pos_will_not_record_does_not_run(self):
		self.pos.refuse = "The web_ro MCP server is switched off in MCP Settings."
		c = self._client()
		err, text = self._call(c, "T-MGR", "woocommerce_orders_query")
		self.assertTrue(err)
		self.assertIn("switched off in MCP Settings", text)
		self.assertEqual(self.wp.runs(), [], "an unrecorded call must not reach the site")

	def test_site_errors_become_plain_sentences_and_fail_the_audit(self):
		cases = [
			((401, {"code": "incorrect_password", "message": "x"}, 0), "own sign-in"),
			((403, {"code": "rest_ability_cannot_execute", "message": "Sorry."}, 0), "not allowed to run"),
			((404, {"code": "rest_ability_not_found", "message": "Ability not found."}, 0), "does not offer"),
			((404, {"code": "woocommerce_order_invalid_id", "message": "Invalid <b>order</b> ID."}, 0),
				"The website said: Invalid order ID."),
			((400, {"code": "ability_invalid_input", "message": "per_page is not of type integer."}, 0),
				"did not accept the request"),
			((405, {"code": "rest_ability_invalid_method", "message": "x"}, 0), "no longer marks"),
			((500, {"code": "internal_server_error", "message": "Critical error."}, 0), "HTTP 500"),
			((200, b"<html>blocked</html>", 0), "not JSON"),
		]
		c = self._client()
		for reply, expected in cases:
			self.wp.run_reply = reply
			err, text = self._call(c, "T-VIEW", "woocommerce_orders_query")
			self.assertTrue(err, reply)
			self.assertIn(expected, text, reply)
			self.assertEqual(self.pos.audits[-1][1]["phase"], "failed")
			self.assertIn(expected, self.pos.audits[-1][1]["error"], reply)

	def test_timeout(self):
		self.wp.run_reply = (200, {}, 1.0)
		c = self._client()
		with patch.object(web, "SITE_TIMEOUT", 0.2):
			err, text = self._call(c, "T-VIEW", "woocommerce_orders_query")
		self.assertTrue(err)
		self.assertIn("did not answer within", text)

	def test_long_results_are_truncated_with_a_hint(self):
		self.wp.run_reply = (200, {"orders": ["x" * 1000] * 200}, 0)
		c = self._client()
		err, text = self._call(c, "T-VIEW", "woocommerce_orders_query")
		self.assertFalse(err)
		self.assertLess(len(text), web.RESULT_MAX_CHARS + 500)
		self.assertIn("Truncated", text)
		self.assertIn("per_page", text)

	def test_credentials_never_appear_in_any_output(self):
		c = self._client()
		self._call(c, "T-VIEW", "woocommerce_orders_query", {"per_page": 1})
		# A site that echoes the credential back in an error or a result.
		for reply in ((500, {"code": "x", "message": f"auth was Basic {BASIC} pw {PASSWORD}"}, 0),
				(200, {"debug": PASSWORD.replace(" ", "")}, 0)):
			self.wp.run_reply = reply
			self._call(c, "T-VIEW", "woocommerce_orders_query")
		self._call(c, "T-VIEW", "woocommerce_products_query")
		self.wp.server.close()  # unreachable
		self._call(c, "T-VIEW", "woocommerce_orders_query")
		everything = "\n".join(self.outputs) + json.dumps(self.pos.audits) + self.log.getvalue()
		for secret in SECRETS:
			self.assertNotIn(secret, everything)
		self.assertIn("***", "\n".join(self.outputs))
		self.assertIn("Could not reach the website", "\n".join(self.outputs))


class Startup(_Fresh, unittest.TestCase):
	def test_an_unreachable_site_does_not_stop_the_server(self):
		from gunstore_mcp import server
		with _env(None, "http://127.0.0.1:9"), self.assertLogs("gunstore_mcp.web", "WARNING") as logs:
			mcp = server.build()
		self.assertEqual(mcp._tool_manager.list_tools(), [])
		self.assertIn("will retry", "\n".join(logs.output))

	def test_tools_appear_once_the_site_is_back(self):
		wp = FakeWP([_ability(ORDERS), _ability(PRODUCTS)])
		self.addCleanup(wp.server.close)
		wp.down = True
		mcp = FastMCP("t")
		with _env(None, wp.server.url, plain=[ORDERS, PRODUCTS]), patch.object(web, "RETRY_FIRST", 0.05), \
				patch.object(web, "RETRY_MAX", 0.05), self.assertLogs("gunstore_mcp.web", "WARNING"):
			catalog = web.register(mcp)
			self.assertEqual(mcp._tool_manager.list_tools(), [])
			wp.down = False
			deadline = time.monotonic() + 5
			while catalog.pending and time.monotonic() < deadline:
				time.sleep(0.02)
		self.assertEqual({t.name for t in mcp._tool_manager.list_tools()},
			{"woocommerce_orders_query", "woocommerce_products_query"})


class Verifier(unittest.TestCase):
	def test_web_access_rides_on_the_token(self):
		import asyncio

		from gunstore_mcp import auth
		pos = FakePOS()
		self.addCleanup(pos.server.close)
		pos.users["T-ODD"] = ("odd@x.com", "superuser")
		v = auth.FrappeTokenVerifier(pos.server.url, surface="web_ro")
		got = {t: asyncio.run(v.verify_token(t)).web_access for t in ("T-VIEW", "T-MGR", "T-NONE", "T-ODD")}
		self.assertEqual(got, {"T-VIEW": "viewer", "T-MGR": "manager", "T-NONE": "", "T-ODD": ""})
		self.assertEqual(set(pos.surfaces), {"web_ro"})


if __name__ == "__main__":
	unittest.main()
