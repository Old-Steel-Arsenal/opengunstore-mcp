"""CPA mode tests: the three defence layers.

Layer 1 — registration: GUNSTORE_MCP_MODE=cpa registers EXACTLY the 27-name
allowlist (set equality, per spec acceptance #1 — not merely "no write tools").
Layer 2 — client: mutating client methods + non-allowlisted dotted methods
raise CpaModeRefused before any HTTP.
Layer 3 — the 9 integration Settings doctypes refuse get/list reads in cpa mode.
Full mode must behave exactly as before (no refusals); its size is pinned below and
in tests/test_doc_counts.py rather than restated here in prose, because a number
written into a docstring is exactly the kind that goes stale unnoticed.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from gunstore_mcp import frappe_client as fc_mod
from gunstore_mcp import server
from gunstore_mcp.config import Config
from gunstore_mcp.modes import (
	CPA_METHOD_ALLOWLIST,
	CPA_SETTINGS_READ_BLOCKLIST,
	CpaModeRefused,
	get_mode,
)
from gunstore_mcp.tools import curated, distributor


_GATES = (distributor.ACTIONS_ENV, curated.GUNBROKER_ACTIONS_ENV)


def _actions(value, gb=None):
	"""Context manager controlling BOTH registration gates (None = absent):
	GUNSTORE_MCP_DISTRIBUTOR_ACTIONS and GUNSTORE_MCP_GUNBROKER_ACTIONS.

	Both are cleared before either is set, and _load_env is stubbed on both
	modules, so a developer's own shell or mcp/.env cannot flip these
	assertions — the surface under test has to be decided here, not inherited."""
	env = {k: v for k, v in os.environ.items() if k not in _GATES}
	if value is not None:
		env[distributor.ACTIONS_ENV] = value
	if gb is not None:
		env[curated.GUNBROKER_ACTIONS_ENV] = gb
	return _Both(patch.dict(os.environ, env, clear=True),
		patch.object(distributor, "_load_env", lambda: None),
		patch.object(curated, "_load_env", lambda: None))


class _Both:
	def __init__(self, *ctxs):
		self._ctxs = ctxs

	def __enter__(self):
		for c in self._ctxs:
			c.__enter__()
		return self

	def __exit__(self, *exc):
		for c in reversed(self._ctxs):
			c.__exit__(*exc)
		return False

# Spelled out independently of modes.py so a drift in EITHER place fails the
# set-equality assertion (the constant can't vouch for itself).
EXPECTED_CPA_TOOLS = {
	# generic (4)
	"frappe_list_documents", "frappe_get_document", "frappe_describe_doctype",
	"frappe_run_report",
	# curated read-only (10)
	"find_item", "item_stock", "firearms_in_stock",
	"pending_orders", "pending_web_orders",
	"consignment_queue", "consignment_dealers", "consignment_serials",
	"consignment_dealer_orders",
	"boundbook_mismatches",
	# cash drawer + stocktake reads (6) — shop-floor WRITES never appear here
	"cash_drawer_closes", "cash_drawer_entries", "cash_drawer_log", "cash_drawer_weekly",
	"inventory_counts", "inventory_count_variance",
	# CPA reports (7)
	"sales_report", "inventory_receipts", "gl_entries", "financial_statement",
	"tax_liability", "ar_ap_summary", "payroc_transactions",
}

EXPECTED_METHOD_ALLOWLIST = {
	"frappe.desk.query_report.run",
	"ffl_core.api.receive_goods.search_items",
	"ffl_core.api.item_admin.get_item_stock",
	"ffl_core.api.manual_order.list_pending_dispositions",
	"ffl_woo_sync.woocommerce.fulfillment.list_pending_web_orders",
	"osa_consignment.api.consignment_out.list_consignment_queue",
	"osa_consignment.api.consignment_out.list_consignment_dealers",
	"osa_consignment.api.consignment_out.available_serials_for_consignment",
	"osa_consignment.api.dealer_orders.list_dealer_orders",
	"ffl_integrations.payroc.ledger.payroc_transactions",
	"ffl_core.api.cash_drawer.get_log",
	"ffl_core.api.inventory_count.get_counts",
	"ffl_core.api.inventory_count.variance",
	"ffl_integrations.fastbound.inventory_sync.boundbook_mismatches",
}

SETTINGS_DOCTYPES = {
	"FFL Settings", "FastBound Settings", "RSR Settings", "Payroc Settings",
	"WooCommerce Settings", "ShipStation Settings",
	# PR-4a: holds the GunBroker DevKey + seller password (Password fields), and
	# has no accounting purpose — same reasoning as every other row here.
	"GunBroker Settings",
	# Sports South ordering credentials (account + API password), no accounting purpose.
	"Sports South Settings",
	"Lipseys Settings",
	# Data-service HMAC client id + secret, no accounting purpose.
	"Data Service Settings",
}


class FakeMCP:
	def __init__(self):
		self.tools = {}

	def tool(self, *a, **k):
		def deco(fn):
			self.tools[fn.__name__] = fn
			return fn
		return deco


class RegistrationLayer(unittest.TestCase):
	def test_cpa_mode_registers_exactly_the_27_allowlisted_tools(self):
		mcp = FakeMCP()
		server.register_tools(mcp, mode="cpa")
		self.assertEqual(set(mcp.tools), EXPECTED_CPA_TOOLS)
		self.assertEqual(len(mcp.tools), 27)

	def test_default_full_mode_holds_both_opt_in_sets_back(self):
		"""Default full mode is 110, not 117: the 4 distributor queue actions and the
		3 GunBroker write actions each require an explicit opt-in. Pinned separately
		from the full surface so that turning either gate into a no-op would break a
		test rather than quietly restore the wider surface."""
		mcp = FakeMCP()
		with _actions(None):
			server.register_tools(mcp, mode="full")
		self.assertEqual(len(mcp.tools), 114)
		for name in ("distributor_confirm_order", "distributor_cancel_order",
				"distributor_reroute", "distributor_update_order_ffl",
				"gb_push_serial", "gb_end_listing", "gb_pull_orders"):
			self.assertNotIn(name, mcp.tools)
		# the read halves are never gated
		for name in ("gb_test_connection", "gb_listing_status"):
			self.assertIn(name, mcp.tools)

	def test_the_two_gates_are_independent(self):
		"""Opting into one must not turn on the other."""
		mcp = FakeMCP()
		with _actions("1", gb=None):
			server.register_tools(mcp, mode="full")
		self.assertIn("distributor_confirm_order", mcp.tools)
		self.assertNotIn("gb_push_serial", mcp.tools)

		mcp = FakeMCP()
		with _actions(None, gb="1"):
			server.register_tools(mcp, mode="full")
		self.assertNotIn("distributor_confirm_order", mcp.tools)
		self.assertIn("gb_push_serial", mcp.tools)

	def test_the_actions_flags_cannot_widen_cpa_mode(self):
		"""cpa is the strictest switch: opting into EITHER action set must not add a
		single tool to the accountant surface. In particular gb_push_serial must not
		appear just because somebody set the GunBroker flag on a cpa instance."""
		mcp = FakeMCP()
		with _actions("1", gb="1"):
			server.register_tools(mcp, mode="cpa")
		self.assertEqual(set(mcp.tools), EXPECTED_CPA_TOOLS)
		self.assertEqual(len(mcp.tools), 27)
		for name in ("gb_push_serial", "gb_end_listing", "gb_pull_orders",
				"gb_test_connection", "gb_listing_status"):
			self.assertNotIn(name, mcp.tools)

	def test_full_mode_registers_the_whole_surface_including_the_cpa_surface(self):
		mcp = FakeMCP()
		with _actions("1", gb="1"):
			server.register_tools(mcp, mode="full")
		self.assertEqual(len(mcp.tools), 121)
		self.assertTrue(EXPECTED_CPA_TOOLS <= set(mcp.tools))
		# regression: none of the write faces leaked out of full mode
		for name in ("frappe_run_method", "dispose_order", "receive_goods",
				"cancel_order", "update_settings", "upload_attachment"):
			self.assertIn(name, mcp.tools)

	def test_cpa_mode_excludes_the_write_and_misleading_faces(self):
		mcp = FakeMCP()
		server.register_tools(mcp, mode="cpa")
		for name in (
			"frappe_run_method", "frappe_create_document", "frappe_update_document",
			"frappe_delete_document", "frappe_submit_document", "frappe_cancel_document",
			"upload_attachment", "get_settings", "update_settings",
			"available_serials",  # cpa-review OQ-3: 盘点语境默认剔除,误导
			"rsr_catalog_search",
			"distributor_test_connection", "fastbound_test_connection",
			"woo_test_connection", "shipstation_test_connection",
			# PR-4a/4b: the GunBroker channel is a sales surface, not an accounting
			# one; three of these write (two to a live marketplace, one starts the
			# order poll that creates POS documents and reserves guns).
			"gb_test_connection", "gb_push_serial", "gb_end_listing", "gb_listing_status",
			"gb_pull_orders",
			"dispose_order", "receive_goods", "cancel_order",
			"ship_consignment_out", "create_consignment_out",
			# shop-floor: only the five reads are on the accountant surface
			"cash_drawer_today", "cash_drawer_preview_close", "cash_drawer_close_day",
			"cash_drawer_record_entry", "cash_drawer_undo", "cash_drawer_record_payout",
			"inventory_count_state", "inventory_count_create", "inventory_count_scan",
			"inventory_count_set_qty", "inventory_count_toggle_serial",
			"inventory_count_undo", "inventory_count_cancel", "inventory_count_finalize",
			"storage_map", "storage_location", "storage_where", "storage_unassigned",
			"storage_create_zone", "storage_add_positions", "storage_set_disabled",
			"storage_scan_move", "storage_undo_move", "storage_confirm_taken",
		):
			self.assertNotIn(name, mcp.tools)

	def test_method_allowlist_is_exactly_the_fourteen_names(self):
		self.assertEqual(set(CPA_METHOD_ALLOWLIST), EXPECTED_METHOD_ALLOWLIST)

	def test_cpa_mode_has_the_dry_run_boundbook_tool_but_not_the_writable_one(self):
		"""boundbook_reconcile can apply (removes guns from stock), so only the
		dry-run-only boundbook_mismatches rides cpa — and the writable POS method
		stays off the client allowlist."""
		self.assertIn("boundbook_mismatches", EXPECTED_CPA_TOOLS)
		self.assertNotIn("boundbook_reconcile", EXPECTED_CPA_TOOLS)
		self.assertNotIn(
			"ffl_integrations.fastbound.inventory_sync.sync_in_stock_from_boundbook",
			CPA_METHOD_ALLOWLIST)

	def test_settings_blocklist_is_exactly_the_ten_doctypes(self):
		self.assertEqual(set(CPA_SETTINGS_READ_BLOCKLIST), SETTINGS_DOCTYPES)


class ModeEnv(unittest.TestCase):
	def test_default_is_full(self):
		with patch.dict(os.environ, {}, clear=False):
			os.environ.pop("GUNSTORE_MCP_MODE", None)
			self.assertEqual(get_mode(), "full")

	def test_cpa_env(self):
		with patch.dict(os.environ, {"GUNSTORE_MCP_MODE": "CPA"}):
			self.assertEqual(get_mode(), "cpa")

	def test_unknown_mode_fails_closed(self):
		with patch.dict(os.environ, {"GUNSTORE_MCP_MODE": "rw"}):
			with self.assertRaises(RuntimeError):
				get_mode()


def _client(mode: str) -> fc_mod.FrappeClient:
	cfg = Config(base_url="http://test.invalid", api_key="k", api_secret="s",
		timeout=5, write_denylist=frozenset())
	with patch.object(fc_mod, "get_config", return_value=cfg), \
			patch.dict(os.environ, {"GUNSTORE_MCP_MODE": mode}):
		return fc_mod.FrappeClient()


class Recorder:
	def __init__(self):
		self.calls = []

	def __call__(self, method, path, **kw):
		self.calls.append((method, path, kw))
		return {"ok": True}


class ClientLayerCpa(unittest.TestCase):
	def setUp(self):
		self.client = _client("cpa")
		self.rec = Recorder()
		self.client._request = self.rec  # any HTTP that happens is recorded

	def test_document_writes_refused_before_any_http(self):
		with self.assertRaises(CpaModeRefused):
			self.client.create_document("Item", {"item_code": "X"})
		with self.assertRaises(CpaModeRefused):
			self.client.update_document("Item", "X", {"disabled": 1})
		with self.assertRaises(CpaModeRefused):
			self.client.delete_document("Item", "X")
		with self.assertRaises(CpaModeRefused):
			self.client.submit_document("Sales Invoice", "SI-1")
		with self.assertRaises(CpaModeRefused):
			self.client.cancel_document("Sales Invoice", "SI-1")
		with self.assertRaises(CpaModeRefused):
			self.client.upload_file("/tmp/x.jpg")
		self.assertEqual(self.rec.calls, [])

	def test_non_allowlisted_methods_refused_even_reads(self):
		for method in (
			"ffl_core.api.manual_order.dispose_order",       # write
			"ffl_core.api.manual_order.record_payment",      # write
			"ffl_core.firearm.available_serials_with_prices",  # read, 未列名
			"ffl_integrations.rsr.search.search",            # read, 未列名
			"frappe.client.get_list",                        # framework read, 未列名
		):
			with self.assertRaises(CpaModeRefused, msg=method):
				self.client.call_method(method, {})
		self.assertEqual(self.rec.calls, [])

	def test_every_allowlisted_method_passes(self):
		for method in sorted(EXPECTED_METHOD_ALLOWLIST):
			self.client.call_method(method, {})
		self.assertEqual(len(self.rec.calls), len(EXPECTED_METHOD_ALLOWLIST))

	def test_run_report_allowed(self):
		self.client.run_report("Sales Report", {"from_date": "2026-07-01"})
		self.assertEqual(len(self.rec.calls), 1)

	def test_settings_reads_blocked_third_layer(self):
		for dt in sorted(SETTINGS_DOCTYPES):
			with self.assertRaises(CpaModeRefused, msg=dt):
				self.client.get_document(dt, dt)
			with self.assertRaises(CpaModeRefused, msg=dt):
				self.client.list_documents(dt)
		self.assertEqual(self.rec.calls, [])

	def test_ordinary_reads_still_work(self):
		self.client.list_documents("Sales Invoice", limit=5)
		self.client.get_document("GL Entry", "GL-0001")
		self.client.get_document("Global Defaults", "Global Defaults")
		self.assertEqual(len(self.rec.calls), 3)


class ClientLayerFull(unittest.TestCase):
	def test_full_mode_unchanged(self):
		client = _client("full")
		rec = Recorder()
		client._request = rec
		client.create_document("Item", {"item_code": "X"})
		client.call_method("ffl_core.api.manual_order.dispose_order", {})
		client.get_document("RSR Settings", "RSR Settings")
		self.assertEqual(len(rec.calls), 3)


class PerCallTimeout(unittest.TestCase):
	def test_call_method_timeout_reaches_the_http_request(self):
		client = _client("cpa")
		seen = []

		class Resp:
			status_code, content = 200, b'{"message": 1}'

			def json(self):
				return {"message": 1}

		client.session.request = lambda *a, **kw: seen.append(kw["timeout"]) or Resp()
		client.call_method("ffl_integrations.payroc.ledger.payroc_transactions", {}, timeout=130)
		client.call_method("ffl_integrations.payroc.ledger.payroc_transactions", {})
		self.assertEqual(seen, [130, 5])


if __name__ == "__main__":
	unittest.main()
