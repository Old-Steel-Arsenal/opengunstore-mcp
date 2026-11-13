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
		self.forbidden = set()  # abilities whose metadata answers 403
		self.run_reply = None  # (status, payload, delay) overriding the default echo
		self.server = _Server(self.handle)

	def handle(self, method, path, headers, body):
		if self.down:
			return 503, b"<html>maintenance</html>", 0
		parts = urlparse(path)
		if method != "GET" and not parts.path.endswith("/run"):
			return 405, {"code": "stub", "message": "metadata is read with GET"}, 0
		if not parts.path.startswith(API):
			return 404, {"code": "rest_no_route", "message": "No route"}, 0
		rest = unquote(parts.path[len(API):])
		if headers.get("Authorization") != f"Basic {BASIC}":
			return 401, {"code": "incorrect_password", "message": "bad"}, 0
		if rest.endswith("/run"):
			name = rest[:-4]
			if name not in self.abilities:
				return 404, {"code": "rest_ability_not_found", "message": "Ability not found."}, 0
			# WordPress 7.0's run controller: the method must match the annotations
			# (validate_request_method), and a POST's input is the JSON body's "input".
			ann = self.abilities[name]["meta"].get("annotations") or {}
			want = "GET" if ann.get("readonly") else "DELETE" if (ann.get("destructive")
				and ann.get("idempotent")) else "POST"
			if method != want:
				return 405, {"code": "rest_ability_invalid_method", "message": f"{want} required."}, 0
			if self.run_reply:
				return self.run_reply
			if method == "POST":
				return 200, {"ran": name, "method": method, "body": json.loads(body or "null")}, 0
			query = parse_qsl(parts.query, keep_blank_values=True)
			schema = self.abilities[name]["input_schema"]
			# WordPress 7.0's rule for a run without input (GET reads only ?input[...]):
			# WP_Ability::normalize_input (abilities-api/class-wp-ability.php) falls back
			# to the schema's top-level default only; validate_input then checks null
			# against the schema, and rest_validate_object_value_from_schema
			# (rest-api.php) refuses null for type object. The run controller answers 400.
			if (not any(k == "input" or k.startswith("input[") for k, _ in query)
					and schema and schema.get("type") == "object" and "default" not in schema):
				return 400, {"code": "ability_invalid_input", "message": f'Ability "{name}" has '
					"invalid input. Reason: input is not of type object.", "data": {"status": 400}}, 0
			out = {"ran": name, "query": parse_qsl(parts.query, keep_blank_values=True)}
			return 200, ({**out, "method": method} if method != "GET" else out), 0
		if rest not in self.abilities:
			return 404, {"code": "rest_ability_not_found", "message": "Ability not found."}, 0
		if rest in self.forbidden:
			return 403, {"code": "rest_forbidden", "message": "Sorry, you are not allowed."}, 0
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


def _env(pos, wp_url, plain=(ORDERS,), restricted=(), write=None, **extra):
	env = {k: v for k, v in os.environ.items() if not k.startswith(("GUNSTORE_MCP_", "FRAPPE_", "WEB_"))}
	env.update({
		"GUNSTORE_MCP_TRANSPORT": "http", "GUNSTORE_MCP_MODE": "web",
		"FRAPPE_BASE_URL": pos.server.url if pos else "https://pos.example.com",
		"GUNSTORE_MCP_PUBLIC_URL": "http://localhost:8790/mcp", "GUNSTORE_MCP_PORT": "8790",
		"WEB_SITE_URL": wp_url, "WEB_SITE_USER": USER, "WEB_SITE_APP_PASSWORD": PASSWORD,
		"WEB_ABILITIES": ",".join(plain), "WEB_ABILITIES_RESTRICTED": ",".join(restricted)})
	if write is not None:
		env["WEB_ABILITIES_WRITE"] = ",".join(write)
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
		self.assertIn("Website Connector Manager", products.description)
		self.assertNotIn("Manager", tools["woocommerce_orders_query"].description)
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


class _App(_Fresh):
	"""A stub site and POS, and the real ASGI app between them."""

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

class Bridge(_App, unittest.TestCase):
	"""Through the real ASGI app, as a client would call it."""

	def test_server_identity_and_tools(self):
		from gunstore_mcp import server
		with _env(self.pos, self.wp.server.url, restricted=[PRODUCTS]):
			mcp = server.build()
		self.assertEqual(mcp.name, "gunstore-web")
		c = self._client()
		tools = self._rpc(c, "T-VIEW", "tools/list")["result"]["tools"]
		self.assertEqual({t["name"] for t in tools}, {"woocommerce_orders_query", "woocommerce_products_query"})
		self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
		r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
		self.assertEqual(r.status_code, 401, "no token, no tools")
		self.assertIn("/.well-known/oauth-protected-resource/mcp", r.headers["www-authenticate"])

	def test_the_instructions_name_the_website_and_say_read_only(self):
		from gunstore_mcp import server
		with _env(self.pos, self.wp.server.url):
			text = server.build().instructions
		self.assertIn(f"ONE store's website: {urlparse(self.wp.server.url).netloc}.", text)
		self.assertIn("read-only", text)
		self.assertNotIn(urlparse(self.pos.server.url).netloc, text, "names the site, not the POS")
		self.assertNotIn("reads and writes", text)

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

	def test_no_arguments_without_a_top_level_default_sends_the_property_defaults(self):
		# WordPress refuses a missing input for an object schema without a top-level
		# default, and a query string cannot carry {}: the declared defaults stand in.
		schema = {"type": "object", "properties": {"include_counts": {"type": "boolean", "default": False},
			"per_page": {"type": "integer", "default": 20}, "search": {"type": "string"}}}
		self.wp.abilities[PRODUCTS] = _ability(PRODUCTS, schema=schema)
		bare = "example/bare-object"
		self.wp.abilities[bare] = _ability(bare, schema={"type": "object", "properties": {
			"search": {"type": "string"}}})
		c = self._client(restricted=(), plain=(ORDERS, PRODUCTS, bare))
		err, text = self._call(c, "T-VIEW", "woocommerce_products_query")
		self.assertFalse(err, text)
		self.assertEqual(json.loads(text)["query"], [["input[include_counts]", "0"], ["input[per_page]", "20"]])
		err, text = self._call(c, "T-VIEW", "woocommerce_products_query", {"search": "x"})
		self.assertEqual(json.loads(text)["query"], [["input[search]", "x"]], "given arguments go as given")
		err, text = self._call(c, "T-VIEW", "example_bare_object")
		self.assertTrue(err, "no defaults to send: the site refuses the empty call")
		self.assertEqual(self.wp.runs()[-1].split("?")[1:], [], "and nothing was invented to send")

	def test_an_empty_call_the_site_refuses_names_the_parameters_to_pass(self):
		schema = {"type": "object", "properties": {"search": {"type": "string"},
			"limit": {"type": "integer"}, "category": {"type": "string", "required": True}}}
		self.wp.abilities[PRODUCTS] = _ability(PRODUCTS, schema=schema)
		c = self._client(restricted=(), plain=(ORDERS, PRODUCTS))
		err, text = self._call(c, "T-VIEW", "woocommerce_products_query")
		self.assertTrue(err)
		expected = ("woocommerce_products_query cannot be called without arguments. Call it again "
			"with at least one of its parameters: search, limit, category (required). The website "
			'said: Ability "woocommerce/products-query" has invalid input. Reason: input is not '
			"of type object.")
		self.assertIn(expected, text)
		self.assertEqual([e["phase"] for _, e in self.pos.audits[-2:]], ["started", "failed"])
		self.assertIn(expected, self.pos.audits[-1][1]["error"])
		err, text = self._call(c, "T-VIEW", "woocommerce_products_query", {"category": "x"})
		self.assertFalse(err, text)

	def test_an_empty_call_to_a_tool_without_parameters_says_the_site_will_not_take_it(self):
		bare = "fluent-crm/get-contact-filter-schema"
		self.wp.abilities[bare] = _ability(bare, schema={"type": "object", "properties": {}})
		c = self._client(restricted=(), plain=(ORDERS, bare))
		err, text = self._call(c, "T-VIEW", "fluent_crm_get_contact_filter_schema")
		self.assertTrue(err)
		self.assertIn("The website does not accept an empty call to fluent_crm_get_contact_filter_schema "
			"over this connector, and the tool has no parameters to pass instead. Ask an administrator "
			"to give the ability's input a default on the website.", text)
		self.assertEqual(self.pos.audits[-1][1]["phase"], "failed")

	def test_a_call_with_arguments_keeps_the_plain_mapping_for_invalid_input(self):
		self.wp.run_reply = (400, {"code": "ability_invalid_input", "message": "Ability \"x\" has "
			"invalid input. Reason: input[per_page] is not of type integer."}, 0)
		c = self._client()
		err, text = self._call(c, "T-VIEW", "woocommerce_orders_query", {"per_page": "many"})
		self.assertTrue(err)
		self.assertIn("did not accept the request", text)
		self.assertNotIn("cannot be called without arguments", text)

	def test_restricted_abilities_need_manager(self):
		c = self._client()
		err, text = self._call(c, "T-VIEW", "woocommerce_products_query")
		self.assertTrue(err)
		# The POS role's real name: Frappe's own "Website Manager" gives no access.
		self.assertIn("needs the Website Connector Manager role", text)
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
			# With arguments: an empty call's 400 has its own refusal (tested below).
			err, text = self._call(c, "T-VIEW", "woocommerce_orders_query", {"per_page": 5})
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


	def test_one_ability_the_site_refuses_does_not_hold_back_the_others(self):
		wp = FakeWP([_ability(ORDERS), _ability(PRODUCTS), _ability("woocommerce/customers-query")])
		self.addCleanup(wp.server.close)
		wp.forbidden = {ORDERS}  # listed first
		mcp = FastMCP("t")
		with _env(None, wp.server.url, plain=[ORDERS, PRODUCTS, "woocommerce/customers-query"]), \
				self.assertLogs("gunstore_mcp.web", "WARNING") as logs:
			catalog = web.Catalog(mcp, web.Site(web.load_config()), web.load_config().abilities)
			catalog.load_once()
		self.assertEqual({t.name for t in mcp._tool_manager.list_tools()},
			{"woocommerce_products_query", "woocommerce_customers_query"})
		self.assertEqual(list(catalog.pending), [ORDERS], "kept pending: it may recover")
		out = "\n".join(logs.output)
		self.assertIn(f"could not load {ORDERS} (HTTP 403", out)
		self.assertIn("ready, %d tool(s)" % 2, out)  # not a literal: the doc-count guard reads prose
		self.assertIn(f"1 ability(ies) still pending: {ORDERS}", out)
		wp.forbidden = set()
		with self.assertLogs("gunstore_mcp.web", "WARNING"):
			catalog.load_once()
		self.assertEqual(catalog.pending, {})
		self.assertEqual(len(mcp._tool_manager.list_tools()), 3)

	def test_a_site_that_does_not_answer_stops_the_pass(self):
		mcp = FastMCP("t")
		with _env(None, "http://127.0.0.1:9", plain=[ORDERS, PRODUCTS]), \
				self.assertLogs("gunstore_mcp.web", "WARNING") as logs:
			catalog = web.Catalog(mcp, web.Site(web.load_config()), web.load_config().abilities)
			calls = []
			real = catalog.site.describe
			catalog.site.describe = lambda a: (calls.append(a), real(a))[1]
			catalog.load_once()
		self.assertEqual(calls, [ORDERS], "the pass stops at the first unanswered call")
		self.assertEqual(set(catalog.pending), {ORDERS, PRODUCTS})
		out = "\n".join(logs.output)
		self.assertIn("will retry", out)
		self.assertNotIn("ready", out)


# A write ability as the site describes it: never readonly (WordPress then wants POST,
# or DELETE when destructive and idempotent).
NOTE = "woocommerce/order-add-note"
SET_META = "example/set-meta"
DELETE = "woocommerce/product-delete"
UPSERT = "fluent-crm/upsert-contact"
NOTE_SCHEMA = {"type": "object", "properties": {"id": {"type": "integer", "minimum": 1},
	"note": {"type": "string", "minLength": 1}, "customer_note": {"type": "boolean", "default": False,
	"description": "Whether the note is visible to the customer."}},
	"required": ["id", "note"], "additionalProperties": False}


def _write(name, schema=None, destructive=False, idempotent=False, readonly=False):
	a = _ability(name, schema=schema or {"type": "object", "properties": {"id": {"type": "integer"},
		"fields": {"type": "object"}}, "required": ["id", "fields"]})
	a["meta"]["annotations"] = {"readonly": readonly, "destructive": destructive, "idempotent": idempotent}
	return a


# What 0.15.0 (read-only) listed for plain=[ORDERS], restricted=[PRODUCTS]: with no
# write list the tool list must not change by one byte.
READ_ONLY_TOOLS = [
	{"name": "woocommerce_orders_query", "title": "Orders Query", "description": "Does woocommerce/orders-query.",
		"inputSchema": {"type": "object", "default": {}, "properties": {
			"page": {"type": "integer", "default": 1}, "per_page": {"type": "integer"},
			"include_line_items": {"type": "boolean"}, "status": {"type": "array", "items": {"type": "string"}}}},
		"annotations": {"title": "Orders Query", "readOnlyHint": True, "destructiveHint": False,
			"idempotentHint": True}},
	{"name": "woocommerce_products_query", "title": "Products Query",
		"description": "Does woocommerce/products-query.\n\nNeeds the Website Connector Manager role in the POS.",
		"inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
		"annotations": {"title": "Products Query", "readOnlyHint": True, "destructiveHint": False,
			"idempotentHint": True}},
]
READ_ONLY_INSTRUCTIONS = ("This server reads ONE store's website: {host}. It is read-only: it cannot "
	"change the site, and it has no POS tools. Every other store has its own server with the same tool "
	"names. Before calling a tool here, check the task is for this website; when reporting what you "
	"found, name the site.")


class WriteConfig(_Fresh, unittest.TestCase):
	def test_an_ability_belongs_in_one_list(self):
		for plain, restricted, write in (([NOTE], [], [NOTE]), ([], [NOTE], [NOTE])):
			with _env(None, "https://shop.example.com", plain=plain, restricted=restricted, write=write):
				with self.assertRaisesRegex(RuntimeError, "WEB_ABILITIES_WRITE"):
					web.load_config()

	def test_a_write_list_alone_is_enough_and_names_are_checked(self):
		with _env(None, "https://shop.example.com", plain=(), write=[NOTE, DELETE]):
			self.assertEqual(web.load_config().abilities, {NOTE: web.WRITE, DELETE: web.WRITE})
		with _env(None, "https://shop.example.com", write=["Not A Name"]):
			with self.assertRaises(RuntimeError):
				web.load_config()

	def test_whatever_a_deployment_lists_is_bridged(self):
		# No ability is refused by name: the deployment decides.
		with _env(None, "https://shop.example.com", write=[DELETE, "woocommerce/order-update-status",
				"fluent-crm/send-email-to-contact"]):
			self.assertEqual(len([k for k, v in web.load_config().abilities.items() if v == web.WRITE]), 3)

	def test_the_http_method_is_wordpress_own_rule(self):
		self.assertEqual(web.http_method({"readonly": True, "destructive": True}), "GET")
		self.assertEqual(web.http_method({"readonly": False, "destructive": True, "idempotent": True}), "DELETE")
		self.assertEqual(web.http_method({"readonly": None, "destructive": True, "idempotent": None}), "POST")
		self.assertEqual(web.http_method({"readonly": False, "destructive": False, "idempotent": True}), "POST")
		self.assertEqual(web.http_method(None), "POST")


class Writes(_App, unittest.TestCase):
	"""Through the real ASGI app, with a write list."""

	def setUp(self):
		super().setUp()
		for a in (_write(NOTE, schema=NOTE_SCHEMA), _write(SET_META, idempotent=True),
				_write(DELETE, destructive=True, idempotent=True,
					schema={"type": "object", "properties": {"id": {"type": "integer"},
						"force": {"type": "boolean"}}, "required": ["id"]}),
				_write(UPSERT, destructive=True, schema={"type": "object", "properties": {
					"contact_id": {"type": "integer"}, "email": {"type": "string"}, "first_name": {"type": "string"},
					"add_tags": {"type": "array"}, "custom_fields": {"type": "object"}}}),
				_write("example/bulk-set-meta"), _write("fluent-crm/send-email-to-contact"),
				_write("woocommerce/order-update-status")):
			self.wp.abilities[a["name"]] = a

	def _wclient(self, write=(NOTE, SET_META, DELETE, UPSERT)):
		from starlette.testclient import TestClient

		from gunstore_mcp import server
		self.env = _env(self.pos, self.wp.server.url, plain=(ORDERS,), restricted=(PRODUCTS,), write=write)
		self.env.start()
		self.addCleanup(self.env.stop)
		self.mcp = server.build()
		client = TestClient(server.http_app(self.mcp), base_url="http://localhost:8790")
		client.__enter__()
		self.addCleanup(client.__exit__, None, None, None)
		return client

	def test_an_empty_write_list_lists_exactly_what_the_read_only_connector_did(self):
		from starlette.testclient import TestClient

		from gunstore_mcp import server
		host = urlparse(self.wp.server.url).netloc
		self.wp.abilities[PRODUCTS] = _ability(PRODUCTS, schema={"type": "object", "properties": {
			"id": {"type": "integer", "required": True}}})
		for write in (None, (), ("",)):
			with _env(self.pos, self.wp.server.url, plain=(ORDERS,), restricted=(PRODUCTS,), write=write):
				mcp = server.build()
				with TestClient(server.http_app(mcp), base_url="http://localhost:8790") as c:
					tools = self._rpc(c, "T-VIEW", "tools/list")["result"]["tools"]
			self.assertEqual(tools, READ_ONLY_TOOLS, write)
			self.assertEqual(mcp.instructions, READ_ONLY_INSTRUCTIONS.format(host=host), write)

	def test_write_tools_and_their_hints(self):
		c = self._wclient(write=(NOTE, SET_META, DELETE, UPSERT, "example/bulk-set-meta",
			"fluent-crm/send-email-to-contact", "woocommerce/order-update-status"))
		tools = {t["name"]: t for t in self._rpc(c, "T-VIEW", "tools/list")["result"]["tools"]}
		self.assertTrue(tools["woocommerce_orders_query"]["annotations"]["readOnlyHint"])
		hints = {n: (t["annotations"]["readOnlyHint"], t["annotations"]["destructiveHint"])
			for n, t in tools.items() if n not in ("woocommerce_orders_query", "woocommerce_products_query")}
		self.assertEqual(hints, {
			"woocommerce_order_add_note": (False, False), "example_set_meta": (False, False),
			"woocommerce_product_delete": (False, True), "fluent_crm_upsert_contact": (False, True),
			"example_bulk_set_meta": (False, True), "fluent_crm_send_email_to_contact": (False, True),
			"woocommerce_order_update_status": (False, True)})
		note = tools["woocommerce_order_add_note"]
		self.assertIn("customer_note", note["inputSchema"]["properties"], "exposed as the site defines it")
		self.assertIn("With customer_note true the note is also emailed to the customer; without it "
			"(default false) the note is private.", note["description"])
		self.assertIn("Changes the website. Needs the Website Connector Manager role", note["description"])
		self.assertNotIn("emailed", tools["example_set_meta"]["description"])
		self.assertIn("work only for a Website Connector Manager", self.mcp.instructions)
		self.assertNotIn("read-only", self.mcp.instructions)
		self.assertEqual(self.wp.runs(), [], "the catalog never runs an ability")

	def test_a_viewer_is_refused_a_write_and_the_refusal_is_recorded(self):
		c = self._wclient()
		err, text = self._call(c, "T-VIEW", "woocommerce_order_add_note", {"id": 7, "note": "Called back"})
		self.assertTrue(err)
		self.assertIn("woocommerce_order_add_note changes the website and needs the Website Connector "
			"Manager role in the POS; your account has Website Connector Viewer.", text)
		self.assertEqual(self.wp.runs(), [], "a refused write never reaches the site")
		self.assertEqual([(u, e["phase"], e["tool"]) for u, e in self.pos.audits], [
			("viewer@x.com", "started", "woocommerce_order_add_note"),
			("viewer@x.com", "failed", "woocommerce_order_add_note")])
		self.assertIn("needs the Website Connector Manager role", self.pos.audits[1][1]["error"])
		err, text = self._call(c, "T-NONE", "example_set_meta", {"id": 1, "fields": {"t": "x"}})
		self.assertTrue(err)
		self.assertIn("no website access role", text)
		self.assertEqual(self.wp.runs(), [])

	def test_a_manager_writes_with_post_and_the_input_in_the_body(self):
		c = self._wclient()
		err, text = self._call(c, "T-MGR", "woocommerce_order_add_note",
			{"id": 7, "note": "Customer asked to hold the order.", "customer_note": True})
		self.assertFalse(err, text)
		self.assertEqual(json.loads(text), {"ran": NOTE, "method": "POST", "body": {"input": {
			"id": 7, "note": "Customer asked to hold the order.", "customer_note": True}}},
			"passed through as given")
		method, path, headers, body = [x for x in self.wp.server.seen if "/run" in x[1]][-1]
		self.assertEqual((method, urlparse(path).query), ("POST", ""))
		self.assertEqual(headers.get("Content-Type"), "application/json")
		self.assertEqual([(u, e["phase"]) for u, e in self.pos.audits], [("mgr@x.com", "started"),
			("mgr@x.com", "ok")])
		err, text = self._call(c, "T-MGR", "example_set_meta", {"id": 12, "fields": {"title": "New"}})
		self.assertFalse(err, text)
		self.assertEqual(json.loads(text)["body"], {"input": {"id": 12, "fields": {"title": "New"}}})
		# Without arguments a POST sends an empty input object (the site validates it).
		err, text = self._call(c, "T-MGR", "fluent_crm_upsert_contact")
		self.assertEqual(json.loads(text)["body"], {"input": {}})

	def test_a_destructive_idempotent_write_uses_delete_with_the_input_in_the_query(self):
		c = self._wclient()
		err, text = self._call(c, "T-MGR", "woocommerce_product_delete", {"id": 5, "force": False})
		self.assertFalse(err, text)
		self.assertEqual(json.loads(text), {"ran": DELETE, "method": "DELETE",
			"query": [["input[id]", "5"], ["input[force]", "0"]]})

	def test_a_write_audit_record_names_the_target_not_the_content(self):
		c = self._wclient()
		self._call(c, "T-MGR", "woocommerce_order_add_note", {"id": 7, "note": "Gate code 4411, cell 555-0100"})
		self._call(c, "T-MGR", "fluent_crm_upsert_contact", {"email": "jane.doe@example.com",
			"first_name": "Jane", "add_tags": ["VIP", 4], "custom_fields": {"dob": "1980-01-01"}})
		self._call(c, "T-MGR", "example_set_meta", {"id": 12, "fields": {"title": "Secret launch"}})
		started = [json.loads(e["arguments"]) for _, e in self.pos.audits if e["phase"] == "started"]
		self.assertEqual(started, [
			{"id": 7, "note": "<29 characters>"},
			{"email": "j***@example.com", "first_name": "<4 characters>", "add_tags": ["VIP", 4],
				"custom_fields": ["dob"]},
			{"id": 12, "fields": ["title"]}])
		everything = json.dumps(self.pos.audits)
		for private in ("4411", "555-0100", "jane.doe", "Jane", "1980", "Secret launch"):
			self.assertNotIn(private, everything)
		self.assertEqual([e["tool"] for _, e in self.pos.audits if e["phase"] == "started"],
			["woocommerce_order_add_note", "fluent_crm_upsert_contact", "example_set_meta"])
		# Reads keep their arguments as before.
		self._call(c, "T-MGR", "woocommerce_orders_query", {"per_page": 3})
		self.assertEqual(json.loads(self.pos.audits[-2][1]["arguments"]), {"per_page": 3})

	def test_bulk_items_keep_their_ids_and_field_names(self):
		self.assertEqual(web.audit_record({"items": [{"id": 1, "fields": {"a": "x"}}, {"id": 2, "fields": {}}]}),
			{"items": [{"id": 1, "fields": ["a"]}, {"id": 2, "fields": []}]})
		many = web.audit_record({"items": [{"id": i} for i in range(60)]})["items"]
		self.assertEqual((len(many), many[-1]), (51, "... 10 more"))
		self.assertEqual(web.audit_record({"email": "not-an-email", "contact_ids": [1, 2], "dry_run": True}),
			{"email": "***", "contact_ids": [1, 2], "dry_run": True})

	def test_write_errors_say_whether_anything_changed(self):
		c = self._wclient()
		args = {"id": 7, "note": "x"}
		cases = [
			((400, {"code": "rest_invalid_param", "message": "note is too short."}, 0),
				"did not accept the request for woocommerce/order-add-note (HTTP 400). The website said: "
				"note is too short.", False),
			((403, {"code": "rest_ability_cannot_execute", "message": "Sorry."}, 0), "not allowed to run", False),
			((404, {"code": "woocommerce_order_invalid_id", "message": "Invalid order ID."}, 0),
				"The website said: Invalid order ID.", False),
			((405, {"code": "rest_ability_invalid_method", "message": "x"}, 0),
				"refused a POST request for woocommerce/order-add-note", False),
			((500, {"code": "internal_server_error", "message": "Critical error."}, 0),
				"The change may or may not have been made", True),
		]
		for reply, expected, maybe in cases:
			self.wp.run_reply = reply
			err, text = self._call(c, "T-MGR", "woocommerce_order_add_note", args)
			self.assertTrue(err, reply)
			self.assertIn(expected, text, reply)
			self.assertEqual("may or may not" in text, maybe, reply)
			self.assertEqual(self.pos.audits[-1][1]["phase"], "failed")
			self.assertIn(expected, self.pos.audits[-1][1]["error"], reply)

	def test_a_write_that_times_out_is_not_retried_and_may_have_happened(self):
		self.wp.run_reply = (200, {}, 1.0)
		c = self._wclient()
		with patch.object(web, "SITE_TIMEOUT", 0.2):
			err, text = self._call(c, "T-MGR", "woocommerce_order_add_note", {"id": 7, "note": "x"})
		self.assertTrue(err)
		self.assertIn("The change may or may not have been made: check on the website before trying "
			"again.", text)
		self.assertEqual(len(self.wp.runs()), 1, "sent once")

	def test_credentials_never_appear_in_write_output(self):
		c = self._wclient()
		self.wp.run_reply = (500, {"code": "x", "message": f"auth was Basic {BASIC} pw {PASSWORD}"}, 0)
		self._call(c, "T-MGR", "woocommerce_order_add_note", {"id": 7, "note": "x"})
		everything = "\n".join(self.outputs) + json.dumps(self.pos.audits) + self.log.getvalue()
		for secret in SECRETS:
			self.assertNotIn(secret, everything)


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
