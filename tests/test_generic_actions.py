"""list_pos_actions: the payload sent to the POS action catalog, argument validation, the
confirm/blocked annotations (same predicate as frappe_run_method), and the old-POS message."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from gunstore_mcp.frappe_client import FrappeAPIError
from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import generic

CATALOG = "ffl_core.api.action_catalog.list_actions"


class FakeClient:
	def __init__(self, result=None, error=None):
		self.calls = []
		self.result = result if result is not None else []
		self.error = error

	def call_method(self, method, kwargs=None):
		self.calls.append(("call_method", method, kwargs or {}))
		if self.error:
			raise self.error
		return self.result


class FakeMCP:
	def __init__(self):
		self.tools = {}

	def tool(self, *a, **k):
		def deco(fn):
			self.tools[fn.__name__] = fn
			return fn
		return deco


def _row(method, **extra):
	return {"method": method, "summary": "s", "args": [], "http": ["POST"], **extra}


class RunMethodVerb(unittest.TestCase):
	"""frappe_run_method sends GET-only endpoints as GET, per the action catalog."""

	class Client:
		def __init__(self, http=None, catalog_error=None):
			self.calls = []
			self.http = http
			self.catalog_error = catalog_error

		def call_method(self, method, kwargs=None):
			self.calls.append(("POST", method, kwargs or {}))
			if method == CATALOG:
				if self.catalog_error:
					raise self.catalog_error
				return [_row(kwargs["method"], http=self.http)] if self.http else []
			return {"via": "POST"}

		def get_method(self, method, params=None):
			self.calls.append(("GET", method, params or {}))
			return {"via": "GET"}

	def setUp(self):
		mcp = FakeMCP()
		generic.register(mcp)
		self.run = mcp.tools["frappe_run_method"]
		generic._VERB_CACHE.clear()
		self.addCleanup(generic._VERB_CACHE.clear)

	def _with(self, client):
		p = patch.object(generic, "get_client", return_value=client)
		p.start()
		self.addCleanup(p.stop)
		return client

	def test_get_only_endpoint_goes_as_get(self):
		c = self._with(self.Client(http=["GET"]))
		out = self.run("app.mod.get_board", {"date": "2026-10-30", "lanes": [1, 2], "all": True})
		self.assertEqual(out, {"via": "GET"})
		self.assertEqual(c.calls, [
			("POST", CATALOG, {"method": "app.mod.get_board"}),
			("GET", "app.mod.get_board", {"date": "2026-10-30", "lanes": "[1, 2]", "all": 1})])

	def test_post_endpoint_goes_as_post(self):
		for http in (["POST"], ["GET", "POST"], "POST"):
			with self.subTest(http=http):
				generic._VERB_CACHE.clear()
				c = self._with(self.Client(http=http))
				self.assertEqual(self.run("app.mod.read_x", {"a": 1}), {"via": "POST"})
				self.assertEqual(c.calls[-1], ("POST", "app.mod.read_x", {"a": 1}))

	def test_verb_cached_for_ten_minutes(self):
		c = self._with(self.Client(http=["GET"]))
		with patch.object(generic.time, "monotonic", return_value=1000.0):
			self.run("app.mod.get_board")
			self.run("app.mod.get_board")
		self.assertEqual([k for k in c.calls if k[1] == CATALOG], [c.calls[0]])
		with patch.object(generic.time, "monotonic", return_value=1000.0 + 601):
			self.run("app.mod.get_board")
		self.assertEqual(sum(1 for k in c.calls if k[1] == CATALOG), 2)

	def test_old_pos_without_catalog_keeps_post(self):
		err = FrappeAPIError(404, "ModuleNotFoundError", ["No module named action_catalog"])
		c = self._with(self.Client(catalog_error=err))
		self.assertEqual(self.run("app.mod.get_board"), {"via": "POST"})
		self.run("app.mod.get_board")
		# the missing catalog is cached too: one lookup, two POSTs
		self.assertEqual([k[0] + " " + k[1] for k in c.calls], [
			"POST " + CATALOG, "POST app.mod.get_board", "POST app.mod.get_board"])

	def test_transient_catalog_error_posts_without_caching(self):
		c = self._with(self.Client(catalog_error=FrappeAPIError(500, "TimeoutError", ["boom"])))
		self.assertEqual(self.run("app.mod.get_board"), {"via": "POST"})
		self.assertNotIn("app.mod.get_board", generic._VERB_CACHE)

	def test_unlisted_method_keeps_post(self):
		c = self._with(self.Client(http=None))
		self.assertEqual(self.run("app.mod.get_board"), {"via": "POST"})


class ListPosActions(unittest.TestCase):
	def setUp(self):
		self.mcp = FakeMCP()
		generic.register(self.mcp)
		self.tool = self.mcp.tools["list_pos_actions"]

	def _use(self, client):
		p = patch.object(generic, "get_client", return_value=client)
		p.start()
		self.addCleanup(p.stop)
		return client

	def test_registered_with_frappe_run_method(self):
		self.assertIn("frappe_run_method", self.mcp.tools)
		self.assertIn("list_pos_actions", self.mcp.tools)

	def test_search_payload(self):
		c = self._use(FakeClient())
		self.tool(query=" gunbroker reprice ", module="ffl_integrations.gunbroker.")
		self.assertEqual(c.calls, [("call_method", CATALOG, {
			"query": "gunbroker reprice", "module": "ffl_integrations.gunbroker", "limit": 100})])

	def test_bare_payload_and_trim_to_limit(self):
		c = self._use(FakeClient([_row(f"app.mod.read_{i}") for i in range(30)]))
		out = self.tool(limit=5)
		self.assertEqual(c.calls[-1][2], {"limit": 100})
		self.assertEqual(len(out["actions"]), 5)
		self.assertEqual(out["count"], 5)
		self.assertTrue(out["truncated"])

	def test_method_detail_payload(self):
		c = self._use(FakeClient([_row("ffl_core.api.manual_order.dispose_order", returns="r")]))
		out = self.tool(method="ffl_core.api.manual_order.dispose_order")
		self.assertEqual(c.calls[-1][2], {"method": "ffl_core.api.manual_order.dispose_order"})
		self.assertEqual(out["action"]["returns"], "r")
		self.assertTrue(out["action"]["confirm"])

	def test_method_detail_dict_and_missing(self):
		self._use(FakeClient({"method": "app.mod.get_x"}))
		self.assertFalse(self.tool(method="app.mod.get_x")["action"]["confirm"])
		self._use(FakeClient([]))
		self.assertIsNone(self.tool(method="app.mod.nope")["action"])

	def test_argument_validation_sends_nothing(self):
		c = self._use(FakeClient())
		for kw in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": "5"},
				   {"method": "a.b.c", "query": "x"}, {"method": "a.b.c", "module": "a"}):
			with self.assertRaises(ValueError, msg=kw):
				self.tool(**kw)
		self.assertEqual(c.calls, [])

	def test_confirm_matches_frappe_run_method(self):
		methods = [
			"ffl_core.api.manual_order.list_pending_dispositions",
			"ffl_core.api.manual_order.record_payment",  # exact-name set
			"ffl_integrations.payroc.api_v2.charge_pos_invoice",  # verb regex
			"ffl_core.disposal.list_rows",  # verb only in module segment
		]
		self._use(FakeClient([_row(m) for m in methods]))
		rows = {r["method"]: r["confirm"] for r in self.tool()["actions"]}
		run = self.mcp.tools["frappe_run_method"]
		for m in methods:
			try:
				run(m)
				refused = False
			except WriteRefused:
				refused = True
			self.assertEqual(rows[m], refused, m)

	def test_blocked_rows_hidden_unless_asked(self):
		rows = [
			_row("app.mod.get_x"),
			_row("frappe.client.set_value"),
			_row("ffl_core.staff_switch.set_pin"),
			_row("ffl_integrations.gunbroker.webhooks.order_paid"),
			_row("ffl_integrations.payroc.api.discover_terminals"),
			_row("some_ext.hooks.inbound", guest=1),
		]
		self._use(FakeClient(rows))
		self.assertEqual([r["method"] for r in self.tool()["actions"]], ["app.mod.get_x"])
		shown = {r["method"]: r for r in self.tool(include_blocked=True)["actions"]}
		self.assertEqual(len(shown), 6)
		self.assertNotIn("blocked", shown["app.mod.get_x"])
		self.assertIn("payments.api.discover_terminals",
					  shown["ffl_integrations.payroc.api.discover_terminals"]["blocked"])
		self.assertNotIn("guest", shown["some_ext.hooks.inbound"])
		self.assertIn("blocked", shown["some_ext.hooks.inbound"])

	def test_annotation_maps(self):
		self._use(FakeClient([_row("app.mod.do_thing")]))
		with patch.dict(generic.METHOD_TO_TOOL, {"app.mod.do_thing": "thing_tool"}), \
				patch.dict(generic.METHOD_NOTES, {"app.mod.do_thing": "needs ids"}), \
				patch.dict(generic.METHOD_SWITCH, {"app.mod.do_thing": "GUNSTORE_MCP_X"}):
			row = self.tool()["actions"][0]
		self.assertEqual((row["use_tool"], row["note"], row["switch"]),
						 ("thing_tool", "needs ids", "GUNSTORE_MCP_X"))

	def test_old_pos_says_upgrade(self):
		self._use(FakeClient(error=FrappeAPIError(500, "ModuleNotFoundError",
			["No module named 'ffl_core.api.action_catalog'"])))
		with self.assertRaisesRegex(RuntimeError, "upgrade"):
			self.tool(query="x")

	def test_other_errors_pass_through(self):
		err = FrappeAPIError(403, "PermissionError", ["Not permitted"])
		self._use(FakeClient(error=err))
		with self.assertRaises(FrappeAPIError):
			self.tool(query="x")


if __name__ == "__main__":
	unittest.main()
