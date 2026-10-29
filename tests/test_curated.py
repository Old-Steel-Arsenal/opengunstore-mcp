"""Unit tests for the curated MCP tools added for groups A–D.

The tools are thin wrappers over FrappeClient.call_method / run_report. We register
them against a fake MCP, patch the client to a recorder, and assert each tool calls
the correct whitelisted method path + kwargs and enforces its confirm guard. No
network / Frappe needed.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import curated


class FakeClient:
	def __init__(self):
		self.calls = []

	def call_method(self, method, kwargs=None, timeout=None):
		# timeout recorded only when passed, so the other tests' call shapes stay 3-tuples
		self.calls.append(("call_method", method, kwargs or {}) + ((timeout,) if timeout else ()))
		return {"ok": True}

	def run_report(self, name, filters=None):
		self.calls.append(("run_report", name, filters))
		return {"ok": True}

	def update_document(self, doctype, name, values):
		self.calls.append(("update_document", doctype, name, values))
		return {"ok": True}

	def get_document(self, doctype, name):
		self.calls.append(("get_document", doctype, name))
		return {"ok": True}

	def upload_file(self, file_path, *, doctype=None, docname=None, fieldname=None,
			is_private=True, filename=None):
		self.calls.append(("upload_file", file_path, doctype, docname, fieldname,
			is_private, filename))
		return {"ok": True}


class FakeMCP:
	"""Captures @mcp.tool()-decorated functions by name."""
	def __init__(self):
		self.tools = {}

	def tool(self, *a, **k):
		def deco(fn):
			self.tools[fn.__name__] = fn
			return fn
		return deco


def _register(mcp, *, gb_actions: str | None):
	"""Register curated with the GunBroker action gate set explicitly.

	Never inherited from the ambient environment: a GUNSTORE_MCP_GUNBROKER_ACTIONS
	sitting in a developer's shell or mcp/.env would otherwise decide whether the
	write tools exist, and these assertions would pass or fail per machine."""
	env = {k: v for k, v in os.environ.items() if k != curated.GUNBROKER_ACTIONS_ENV}
	if gb_actions is not None:
		env[curated.GUNBROKER_ACTIONS_ENV] = gb_actions
	with patch.dict(os.environ, env, clear=True), \
			patch.object(curated, "_load_env", lambda: None):
		curated.register(mcp)
	return mcp


class CuratedTools(unittest.TestCase):
	def setUp(self):
		mcp = FakeMCP()
		# Actions ON: this class asserts the shape of every tool, writes included.
		# Whether they are registered by DEFAULT is pinned in GunBrokerActionGate.
		_register(mcp, gb_actions="1")
		self.tools = mcp.tools
		self.client = FakeClient()
		self._patch = patch.object(curated, "get_client", return_value=self.client)
		self._patch.start()
		self.addCleanup(self._patch.stop)

	def _last(self):
		return self.client.calls[-1]

	# ---------------------------------------------------------- A: read-only

	def test_item_stock(self):
		self.tools["item_stock"](["A", "B"])
		self.assertEqual(self._last(), (
			"call_method", "ffl_core.api.item_admin.get_item_stock",
			{"item_codes": ["A", "B"]},
		))

	def test_available_serials_excludes_unavailable_by_default(self):
		# Mirrors the server default (firearm.py available_serials_with_prices
		# exclude_unavailable=True): held / consigned-out guns are dropped.
		self.tools["available_serials"]("A")
		self.assertEqual(self._last(), (
			"call_method", "ffl_core.firearm.available_serials_with_prices",
			{"item_codes": "A", "exclude_unavailable": 1},
		))

	def test_available_serials_can_include_unavailable(self):
		# Inventory-audit callers need the RAW list (consigned/held included).
		self.tools["available_serials"]("A", exclude_unavailable=False)
		self.assertEqual(self._last()[2],
			{"item_codes": "A", "exclude_unavailable": 0})

	def test_rsr_catalog_search(self):
		self.tools["rsr_catalog_search"]("glock", 5)
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.rsr.search.search",
			{"query": "glock", "limit": 5},
		))

	def test_boundbook_reconcile_defaults_to_dry_run(self):
		self.tools["boundbook_reconcile"]()
		self.assertEqual(self._last(), (
			"call_method",
			"ffl_integrations.fastbound.inventory_sync.sync_in_stock_from_boundbook",
			{"dry_run": 1, "item_ids": None},
			130,
		))

	def test_boundbook_reconcile_apply_requires_confirm(self):
		with self.assertRaises(WriteRefused):
			self.tools["boundbook_reconcile"](apply=True)
		self.assertEqual(self.client.calls, [])  # refused before any call
		self.tools["boundbook_reconcile"](apply=True, confirm=True)
		self.assertEqual(self._last(), (
			"call_method",
			"ffl_integrations.fastbound.inventory_sync.sync_in_stock_from_boundbook",
			{"dry_run": 0, "item_ids": None},
			130,
		))

	def test_boundbook_mismatches_calls_the_dry_run_only_method(self):
		"""Read-only twin of boundbook_reconcile: no apply/confirm, and it must never
		route to the writable sync method (it rides cpa mode)."""
		self.tools["boundbook_mismatches"]()
		self.assertEqual(self._last(), (
			"call_method",
			"ffl_integrations.fastbound.inventory_sync.boundbook_mismatches",
			{},
			130,
		))
		with self.assertRaises(TypeError):
			self.tools["boundbook_mismatches"](item_ids=["FB1"])

	def test_set_serial_title(self):
		"""Per-gun Woo title write goes to Serial No.item_name (no confirm gate)."""
		self.tools["set_serial_title"]("SN1", "Colt King Cobra .357 Magnum")
		self.assertEqual(self._last(), (
			"update_document", "Serial No", "SN1",
			{"item_name": "Colt King Cobra .357 Magnum"},
		))

	# ------------------------------------------------- B: writes (confirm)

	def test_receive_goods_requires_confirm(self):
		payload = {"supplier": "X", "items": []}
		with self.assertRaises(WriteRefused):
			self.tools["receive_goods"](payload)
		self.tools["receive_goods"](payload, confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_core.api.receive_goods.create_receive",
			{"payload": payload},
		))

	def test_receive_goods_passes_the_seller_payment_through(self):
		"""A paid purchase from a private seller says how the seller was paid; the POS books it from these two keys,
		so the tool must hand them over untouched (it never builds or filters the payload)."""
		payload = {
			"acquisition_source": "Individual", "acquisition_type": "Individual", "source": "Individual",
			"seller_payment_method": "Check", "seller_payment_reference": "1042",
			"items": [{"item_code": "AMMO", "qty": 2, "rate": 10}],
		}
		self.tools["receive_goods"](dict(payload), confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_core.api.receive_goods.create_receive", {"payload": payload},
		))
		doc = self.tools["receive_goods"].__doc__ or ""
		for needed in ("seller_payment_method", "seller_payment_reference", "Cash", "Zelle", "Check", "ACH"):
			self.assertIn(needed, doc)

	def test_add_stock(self):
		with self.assertRaises(WriteRefused):
			self.tools["add_stock"]("ITEM", 5)
		self.tools["add_stock"]("ITEM", 5, warehouse="WH", rate=10, remarks="r", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_core.api.item_admin.add_stock",
			{"item_code": "ITEM", "qty": 5, "warehouse": "WH", "rate": 10, "remarks": "r"},
		))

	def test_set_stock(self):
		with self.assertRaises(WriteRefused):
			self.tools["set_stock"]("ITEM", 3)
		self.tools["set_stock"]("ITEM", 3, warehouse="WH", reason="count", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_core.api.item_admin.set_stock",
			{"item_code": "ITEM", "new_qty": 3, "warehouse": "WH", "reason": "count"},
		))

	def test_toggle_service_need_maps_bool_to_int(self):
		with self.assertRaises(WriteRefused):
			self.tools["toggle_service_need"]("SN1", True)
		self.tools["toggle_service_need"]("SN1", True, confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_core.firearm.toggle_serial_service_need",
			{"serial": "SN1", "value": 1},
		))
		self.tools["toggle_service_need"]("SN1", False, confirm=True)
		self.assertEqual(self._last()[2]["value"], 0)

	# ------------------------------------ C: compliance / FastBound (confirm)

	def test_push_serial_to_fastbound(self):
		with self.assertRaises(WriteRefused):
			self.tools["push_serial_to_fastbound"]("SN1")
		self.tools["push_serial_to_fastbound"]("SN1", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.fastbound.tasks.push_serial_to_fastbound",
			{"serial": "SN1"},
		))

	def test_verify_supplier_ffl(self):
		with self.assertRaises(WriteRefused):
			self.tools["verify_supplier_ffl"]("SUP")
		self.tools["verify_supplier_ffl"]("SUP", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.atf.ez_check_api.verify_supplier_ffl",
			{"supplier_name": "SUP"},
		))

	def test_reverify_all_ffls(self):
		with self.assertRaises(WriteRefused):
			self.tools["reverify_all_ffls"]()
		self.tools["reverify_all_ffls"](confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.atf.ez_check.re_verify_all", {},
		))

	# ------------------------------------------------- D: RSR promote (confirm)

	def test_promote_to_item(self):
		payload = {"rsr_stock_number": "GLK19"}
		with self.assertRaises(WriteRefused):
			self.tools["promote_to_item"](payload)
		self.tools["promote_to_item"](payload, confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.rsr.promote.promote_to_item",
			{"payload": payload},
		))

	def test_backfill_from_rsr(self):
		with self.assertRaises(WriteRefused):
			self.tools["backfill_from_rsr"]()
		self.tools["backfill_from_rsr"](["A"], confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.rsr.promote.backfill_from_rsr",
			{"item_codes": ["A"]},
		))

	# ------------------------------------------ E: settings map additions

	def test_get_settings_gunbroker(self):
		self.tools["get_settings"]("gunbroker")
		self.assertEqual(self._last(), (
			"get_document", "GunBroker Settings", "GunBroker Settings"))

	def test_update_settings_gunbroker_refuses_the_environment_switch(self):
		"""Without this the chain is: flip sandbox_mode with no confirm at all,
		then one confirm=true push, and a real gun is on the real marketplace —
		while three places in this repo claim the environment can't be chosen here.
		Refused outright, and nothing reaches the client."""
		for values in ({"sandbox_mode": 0}, {"enabled": 1},
				{"base_url_override": "https://api.gunbroker.com/v1"},
				{"dev_key": "leaked"}, {"password": "leaked"}, {"username": "someone"},
				{"end_strategy": "Manual Only"}, {"card_checkout_enabled": 1},
				{"check_deposit_account": "Some Bank - OSA"},
				{"page_size": 50, "sandbox_mode": 0}):
			with self.subTest(values=values):
				before = len(self.client.calls)
				with self.assertRaises(WriteRefused):
					self.tools["update_settings"]("gunbroker", values)
				self.assertEqual(len(self.client.calls), before,
					"refused write still reached the client")

	def test_update_settings_gunbroker_allows_the_ordinary_fields(self):
		self.tools["update_settings"]("gunbroker", {"page_size": 50, "max_pictures": 8})
		self.assertEqual(self._last(), (
			"update_document", "GunBroker Settings", "GunBroker Settings",
			{"page_size": 50, "max_pictures": 8},
		))

	def test_update_settings_other_doctypes_are_not_narrowed(self):
		"""The never-writes rule is GunBroker-only on purpose; widening it to the
		other seven Settings is a separate decision, not a side effect."""
		self.tools["update_settings"]("woocommerce", {"enabled": 1})
		self.assertEqual(self._last()[:3],
			("update_document", "WooCommerce Settings", "WooCommerce Settings"))

	def test_get_settings_shipstation_and_no_dealer_area(self):
		self.tools["get_settings"]("shipstation")
		self.assertEqual(self._last(), (
			"get_document", "ShipStation Settings", "ShipStation Settings"))
		n = len(self.client.calls)
		for tool, args in (("get_settings", ("dealer",)), ("update_settings", ("dealer", {"enabled": 1}))):
			with self.assertRaises(ValueError):
				self.tools[tool](*args)                    # the dealer web shop is gone (POS 1.11.0)
		self.assertEqual(len(self.client.calls), n)        # nothing was sent

	# ---------------------------------------------------- F: Woo multi-site

	def test_woo_push_item_defaults_to_retail(self):
		with self.assertRaises(WriteRefused):
			self.tools["woo_push_item"]("ITEM")
		self.tools["woo_push_item"]("ITEM", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_woo_sync.woocommerce.client_api.push_item_now",
			{"item_code": "ITEM", "site": "retail"},
		))

	def test_woo_tools_offer_the_retail_site_only(self):
		"""The six woo_* tools advertise site as the single value "retail" — the
		schema FastMCP derives from the signature — so a client cannot pick "dealer"."""
		import inspect
		from typing import Literal, get_args, get_type_hints
		names = ("woo_test_connection", "woo_push_item", "woo_delist_item",
			"woo_reconcile", "woo_push_serial", "woo_delist_serial")
		for name in names:
			hint = get_type_hints(self.tools[name])["site"]
			self.assertIs(hint.__origin__, Literal, name)
			self.assertEqual(get_args(hint), ("retail",), name)
			self.assertEqual(inspect.signature(self.tools[name]).parameters["site"].default, "retail")

	def test_woo_delist_item_site(self):
		self.tools["woo_delist_item"]("ITEM", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_woo_sync.woocommerce.client_api.delist_item_now",
			{"item_code": "ITEM", "site": "retail"},
		))

	def test_woo_reconcile_site(self):
		self.tools["woo_reconcile"]("ITEM", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_woo_sync.woocommerce.client_api.reconcile_now",
			{"item_code": "ITEM", "site": "retail"},
		))

	def test_woo_test_connection_site(self):
		self.tools["woo_test_connection"]()
		self.assertEqual(self._last(), (
			"call_method", "ffl_woo_sync.woocommerce.client_api.test_connection",
			{"site": "retail"},
		))

	def test_woo_push_serial(self):
		with self.assertRaises(WriteRefused):
			self.tools["woo_push_serial"]("SN1")
		self.tools["woo_push_serial"]("SN1", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_woo_sync.woocommerce.client_api.push_serial_now",
			{"serial_no": "SN1", "site": "retail"},
		))

	def test_woo_delist_serial(self):
		with self.assertRaises(WriteRefused):
			self.tools["woo_delist_serial"]("SN1")
		self.tools["woo_delist_serial"]("SN1", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_woo_sync.woocommerce.client_api.delist_serial_now",
			{"serial_no": "SN1", "site": "retail"},
		))

	# ------------------------------------------------- F2: GunBroker channel

	def test_gb_test_connection_read_only(self):
		self.tools["gb_test_connection"]()
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.gunbroker.client_api.test_connection", {},
		))

	def test_gb_push_serial(self):
		with self.assertRaises(WriteRefused):
			self.tools["gb_push_serial"]("SN1")
		self.tools["gb_push_serial"]("SN1", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.gunbroker.client_api.push_serial_now",
			{"serial_no": "SN1"},
		))

	def test_gb_push_serial_never_sends_relist(self):
		"""push_serial_now takes a `relist` flag that lifts the manually-ended
		sentinel. The MCP must not expose it: re-listing a gun somebody ended by
		hand is a decision for the Serial No form, where the person can see why it
		was ended. Sending nothing leaves the server on its safe default (0)."""
		self.tools["gb_push_serial"]("SN1", confirm=True)
		self.assertNotIn("relist", self._last()[2])

	def test_gb_end_listing(self):
		with self.assertRaises(WriteRefused):
			self.tools["gb_end_listing"]("SN1")
		self.tools["gb_end_listing"]("SN1", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.gunbroker.client_api.end_listing_now",
			{"serial_no": "SN1", "reason": None},
		))
		self.tools["gb_end_listing"]("SN1", confirm=True, reason="sold at the counter")
		self.assertEqual(self._last()[2],
			{"serial_no": "SN1", "reason": "sold at the counter"})

	def test_gb_listing_status_read_only(self):
		self.tools["gb_listing_status"]("SN1")
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.gunbroker.client_api.listing_status",
			{"serial_no": "SN1"},
		))

	def test_gb_pull_orders(self):
		with self.assertRaises(WriteRefused):
			self.tools["gb_pull_orders"]()
		self.tools["gb_pull_orders"](confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.gunbroker.client_api.sync_orders_now", {},
		))

	def test_gb_pull_orders_sends_no_arguments(self):
		"""sync_orders_now() takes none, and the watermark is deliberately not
		reachable from here: GunBroker Settings.orders_since_override is what
		rewinds the poll window, and rewinding it re-imports old orders and
		re-reserves the guns they name. That is a Desk decision."""
		self.tools["gb_pull_orders"](confirm=True)
		self.assertEqual(self._last()[2], {})

	def test_gb_pull_orders_says_the_answer_is_only_a_queue_receipt(self):
		"""{"queued": true} is the entire server answer — it carries no counts,
		because the poll has not run yet. An agent reading that key alone will
		report "orders pulled"; the note is what stops it, and it must not invent
		numbers the server did not send."""
		with patch.object(curated, "get_client") as gc:
			gc.return_value.call_method.return_value = {"queued": True}
			out = self.tools["gb_pull_orders"](confirm=True)
		self.assertEqual(out["queued"], True)
		self.assertIn("note", out)
		self.assertIn("queued", out["note"].lower())
		# nothing invented: the only key added is the note
		self.assertEqual(set(out) - {"note"}, {"queued"})
		self.assertNotRegex(out["note"], r"\b\d+ (orders?|alerts?)\b")

	def test_gb_pull_orders_passes_any_other_shape_through_verbatim(self):
		"""The note is attached to ONE recognised shape. If the server ever answers
		something else — an error dict, a future body that does carry counts — the
		wrapper must hand it over untouched rather than narrate a shape it has not
		been taught. Guessing here is how a wrapper starts lying."""
		for payload in (
			{"ok": False, "error": "GunBroker is not configured"},
			{"queued": False, "reason": "polling disabled"},
			{"queued": True, "orders": 3, "created": 1},  # a shape we do not know
			[{"queued": True}],
			None,
		):
			with self.subTest(payload=payload):
				with patch.object(curated, "get_client") as gc:
					gc.return_value.call_method.return_value = payload
					self.assertEqual(self.tools["gb_pull_orders"](confirm=True), payload)

	def test_gb_tools_pass_the_server_answer_through_verbatim(self):
		"""The backend answers guard refusals with a skip dict and end_listing with
		a confirmed/pending_manual shape. Both carry operational meaning ("the gun
		can still be bought on GunBroker until somebody ends it by hand"), so the
		wrapper must not reshape, summarise or drop keys."""
		skip = {"ok": False, "skipped": "channel_reserved", "message": "held for GB-ORD-1"}
		pending = {"ok": True, "strategy": "delete_endpoint", "confirmed": False,
			"pending_manual": True, "gb_url": "https://gunbroker.test/item/1",
			"message": "still buyable until ended by hand"}
		for tool, args, kwargs, payload in (
			("gb_push_serial", ("SN1",), {"confirm": True}, skip),
			("gb_end_listing", ("SN1",), {"confirm": True}, pending),
			("gb_listing_status", ("SN1",), {}, {"ok": True, "state": "sold_unknown"}),
			("gb_test_connection", (), {}, {"ok": True, "sandbox": True}),
		):
			with patch.object(curated, "get_client") as gc:
				gc.return_value.call_method.return_value = payload
				self.assertEqual(self.tools[tool](*args, **kwargs), payload)

	# ------------------------------------------- G: order / fulfillment queue

	def test_pending_orders_read_only(self):
		self.tools["pending_orders"]()
		self.assertEqual(self._last(), (
			"call_method", "ffl_core.api.manual_order.list_pending_dispositions", {},
		))

	def test_pending_orders_docstring_names_consignment_queue(self):
		# #213 split the consignment fulfillment into its own queue; the server
		# list explicitly EXCLUDES consignment SIs. The docstring must route the
		# operator to consignment_queue or they will think consignments vanished.
		self.assertIn("consignment_queue", self.tools["pending_orders"].__doc__)

	def test_pending_web_orders_read_only(self):
		self.tools["pending_web_orders"]()
		self.assertEqual(self._last(), (
			"call_method",
			"ffl_woo_sync.woocommerce.fulfillment.list_pending_web_orders", {},
		))

	def test_dispose_order_requires_confirm(self):
		with self.assertRaises(WriteRefused):
			self.tools["dispose_order"]("SI-1")
		self.assertEqual(self.client.calls, [])
		self.tools["dispose_order"]("SI-1", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_core.api.manual_order.dispose_order",
			{"sales_invoice": "SI-1"},
		))

	def test_dispose_web_order_requires_confirm(self):
		with self.assertRaises(WriteRefused):
			self.tools["dispose_web_order"]("WOO-1")
		self.tools["dispose_web_order"]("WOO-1", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_woo_sync.woocommerce.fulfillment.dispose_web_order",
			{"woo_online_order": "WOO-1"},
		))

	def test_record_payment(self):
		with self.assertRaises(WriteRefused):
			self.tools["record_payment"]("SI-1", "Zelle")
		self.tools["record_payment"]("SI-1", "Zelle", amount=100.0,
			transaction_number="Z123", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_core.api.manual_order.record_payment",
			{"sales_invoice": "SI-1", "mode_of_payment": "Zelle",
			 "amount": 100.0, "transaction_number": "Z123"},
		))
		doc = self.tools["record_payment"].__doc__ or ""
		for mode in ("Cash", "Credit Card", "Zelle", "ACH", "Check/Money Order"):
			self.assertIn(mode, doc)

	def test_card_void_and_refund(self):
		self.tools["card_charges"]("SI-1")
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.payments.si_reversal.card_charges", {"sales_invoice": "SI-1"}))
		for name, args in (("void_card_charge", ("TP-1", "wrong card")),
				("refund_card_charge", ("TP-1", 10.0, "Return", "returned")),
				("resolve_card_reversal", ("TP-1", True, "portal shows it", "P-1"))):
			with self.assertRaises(WriteRefused):
				self.tools[name](*args)
		self.tools["void_card_charge"]("TP-1", "wrong card", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.payments.si_reversal.void_sales_invoice_charge",
			{"terminal_payment": "TP-1", "reason": "wrong card"}))
		self.tools["refund_card_charge"]("TP-1", 10.0, "Return", "returned", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.payments.si_reversal.refund_sales_invoice_charge",
			{"terminal_payment": "TP-1", "amount": 10.0, "refund_type": "Return", "reason": "returned"}))
		self.tools["resolve_card_reversal"]("TP-1", False, "not in portal", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.payments.si_reversal.resolve_card_reversal",
			{"terminal_payment": "TP-1", "went_through": 0, "note": "not in portal", "portal_reference": None}))

	def test_push_shipment_requires_confirm(self):
		with self.assertRaises(WriteRefused):
			self.tools["push_shipment"]("SI-1")
		self.tools["push_shipment"]("SI-1", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.shipstation.api.push_shipment",
			{"sales_invoice": "SI-1"},
		))

	def test_mark_shipped_manually_requires_confirm(self):
		with self.assertRaises(WriteRefused):
			self.tools["mark_shipped_manually"]("SI-1")
		self.tools["mark_shipped_manually"]("SI-1", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_core.api.manual_order.mark_shipped_manually",
			{"sales_invoice": "SI-1"},
		))

	def test_shipstation_test_connection_read_only(self):
		self.tools["shipstation_test_connection"]()
		self.assertEqual(self._last(), (
			"call_method", "ffl_integrations.shipstation.api.test_connection", {},
		))

	# --------------------------------------------------------- H: POS 4473

	def test_start_4473(self):
		mapping = {"row1": "SN1"}
		with self.assertRaises(WriteRefused):
			self.tools["start_4473"]("POSINV-1", mapping)
		self.tools["start_4473"]("POSINV-1", mapping, confirm=True)
		self.assertEqual(self._last(), (
			"call_method",
			"ffl_integrations.fastbound.pos_4473.start_4473_for_pos_invoice",
			{"invoice_name": "POSINV-1", "serial_by_item_row": mapping},
		))

	def test_manager_override_4473(self):
		with self.assertRaises(WriteRefused):
			self.tools["manager_override_4473"]("POSINV-1", "webhook lost")
		self.tools["manager_override_4473"]("POSINV-1", "webhook lost", confirm=True)
		self.assertEqual(self._last(), (
			"call_method",
			"ffl_integrations.fastbound.pos_4473.manager_override_4473",
			{"invoice_name": "POSINV-1", "reason": "webhook lost"},
		))

	def test_start_transfer_4473(self):
		with self.assertRaises(WriteRefused):
			self.tools["start_transfer_4473"](["SN1"], "TRANSFER-FEE", 25.0, "CUST-1")
		self.tools["start_transfer_4473"](["SN1", "SN2"], "TRANSFER-FEE", 25.0,
			"CUST-1", pos_profile="Main", confirm=True)
		self.assertEqual(self._last(), (
			"call_method",
			"ffl_integrations.fastbound.pos_4473.start_transfer_for_pos_invoice",
			{"serials": ["SN1", "SN2"], "fee_item": "TRANSFER-FEE",
			 "fee_amount": 25.0, "customer": "CUST-1", "pos_profile": "Main",
			 "invoice_name": None},
		))

	# ------------------------------------------------------- I: file upload

	def test_upload_attachment(self):
		self.tools["upload_attachment"]("/tmp/x.jpg", doctype="Serial No",
			name="SN1", is_private=False)
		self.assertEqual(self._last(), (
			"upload_file", "/tmp/x.jpg", "Serial No", "SN1", None, False, None))

	# ------------------------------------------ J: consignment out (寄售) reads

	def test_consignment_queue_read_only(self):
		self.tools["consignment_queue"]()
		self.assertEqual(self._last(), (
			"call_method", "osa_consignment.api.consignment_out.list_consignment_queue",
			{"include_closed": 0},
		))
		self.tools["consignment_queue"](include_closed=True)
		self.assertEqual(self._last()[2], {"include_closed": 1})

	def test_consignment_dealers_read_only(self):
		self.tools["consignment_dealers"]()
		self.assertEqual(self._last(), (
			"call_method",
			"osa_consignment.api.consignment_out.list_consignment_dealers", {},
		))

	def test_consignment_serials_read_only(self):
		self.tools["consignment_serials"](item_code="GLK19", search="G19", limit=10)
		self.assertEqual(self._last(), (
			"call_method",
			"osa_consignment.api.consignment_out.available_serials_for_consignment",
			{"item_code": "GLK19", "search": "G19", "limit": 10},
		))

	def test_consignment_dealer_orders_read_only(self):
		self.tools["consignment_dealer_orders"]()
		self.assertEqual(self._last(), (
			"call_method",
			"osa_consignment.api.dealer_orders.list_dealer_orders", {},
		))

	# ----------------------------------------- K: consignment out (寄售) writes

	def test_create_consignment_out_requires_confirm(self):
		payload = {"dealer": "AAA", "lines": [
			{"item_code": "I", "serial": "SN1", "cost": 400, "msrp": 599}]}
		with self.assertRaises(WriteRefused):
			self.tools["create_consignment_out"](payload)
		self.assertEqual(self.client.calls, [])
		self.tools["create_consignment_out"](payload, confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "osa_consignment.api.consignment_out.create_consignment_out",
			{"payload": payload},
		))

	def test_ship_consignment_out_requires_confirm(self):
		with self.assertRaises(WriteRefused):
			self.tools["ship_consignment_out"]("CONO-0001")
		self.assertEqual(self.client.calls, [])
		self.tools["ship_consignment_out"]("CONO-0001", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "osa_consignment.api.consignment_out.ship_consignment_out",
			{"consignment_out": "CONO-0001"},
		))

	def test_push_consignment_shipment_requires_confirm(self):
		with self.assertRaises(WriteRefused):
			self.tools["push_consignment_shipment"]("CONO-0001")
		self.assertEqual(self.client.calls, [])
		self.tools["push_consignment_shipment"]("CONO-0001", confirm=True)
		self.assertEqual(self._last(), (
			"call_method",
			"osa_consignment.shipping.push_consignment_shipment",
			{"consignment_out": "CONO-0001"},
		))

	def test_mark_consignment_shipped_requires_confirm(self):
		with self.assertRaises(WriteRefused):
			self.tools["mark_consignment_shipped"]("CONO-0001")
		self.assertEqual(self.client.calls, [])
		self.tools["mark_consignment_shipped"]("CONO-0001",
			tracking_number="1Z999", carrier="UPS", confirm=True)
		self.assertEqual(self._last(), (
			"call_method",
			"osa_consignment.api.consignment_out.mark_consignment_shipped_manually",
			{"consignment_out": "CONO-0001", "tracking_number": "1Z999",
			 "carrier": "UPS"},
		))

	def test_retry_consignment_invoice_requires_confirm(self):
		with self.assertRaises(WriteRefused):
			self.tools["retry_consignment_invoice"]("LINE-1")
		self.assertEqual(self.client.calls, [])
		self.tools["retry_consignment_invoice"]("LINE-1", confirm=True)
		self.assertEqual(self._last(), (
			"call_method",
			"osa_consignment.api.dealer_orders.create_consignment_invoice_now",
			{"line": "LINE-1"},
		))

	def test_return_consignment_lines_requires_confirm(self):
		with self.assertRaises(WriteRefused):
			self.tools["return_consignment_lines"]("CONO-0001", ["L1", "L2"])
		self.assertEqual(self.client.calls, [])
		self.tools["return_consignment_lines"]("CONO-0001", ["L1", "L2"],
			to_warehouse="Main - OSA", confirm=True)
		self.assertEqual(self._last(), (
			"call_method",
			"osa_consignment.api.consignment_out.return_consignment_lines",
			{"consignment_out": "CONO-0001", "lines": ["L1", "L2"],
			 "to_warehouse": "Main - OSA"},
		))

	def test_cancel_consignment_whole_doc(self):
		with self.assertRaises(WriteRefused):
			self.tools["cancel_consignment"]("CONO-0001", "built by mistake")
		self.assertEqual(self.client.calls, [])
		self.tools["cancel_consignment"]("CONO-0001", "built by mistake",
			confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "osa_consignment.api.consignment_out.cancel_consignment_out",
			{"consignment_out": "CONO-0001", "reason": "built by mistake"},
		))

	def test_cancel_consignment_single_line(self):
		self.tools["cancel_consignment"]("CONO-0001", "wrong gun", line="L1",
			confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "osa_consignment.api.consignment_out.cancel_consignment_line",
			{"consignment_out": "CONO-0001", "line": "L1", "reason": "wrong gun"},
		))

	def test_cancel_consignment_blank_reason_rejected(self):
		with self.assertRaises(ValueError):
			self.tools["cancel_consignment"]("CONO-0001", "  ", confirm=True)
		self.assertEqual(self.client.calls, [])

	# --------------------------------------------------- L: counter order cancel

	def test_cancel_order_requires_confirm_and_reason(self):
		with self.assertRaises(WriteRefused):
			self.tools["cancel_order"]("SI-1", "customer backed out")
		with self.assertRaises(ValueError):
			self.tools["cancel_order"]("SI-1", "", confirm=True)
		self.assertEqual(self.client.calls, [])
		self.tools["cancel_order"]("SI-1", "customer backed out",
			refund_mode="Cash", refund_reference="R1", confirm=True)
		self.assertEqual(self._last(), (
			"call_method", "ffl_core.api.manual_order.cancel_order",
			{"sales_invoice": "SI-1", "reason": "customer backed out",
			 "refund_mode": "Cash", "refund_reference": "R1"},
		))

	def test_update_consignment_prices_requires_confirm(self):
		with self.assertRaises(WriteRefused):
			self.tools["update_consignment_prices"](
				"CONO-0007", {"LINE-1": {"cost": 500}})
		self.assertEqual(self.client.calls, [])
		self.tools["update_consignment_prices"](
			"CONO-0007", {"LINE-1": {"cost": 500, "msrp": 650}}, confirm=True)
		self.assertEqual(self._last(), (
			"call_method",
			"osa_consignment.api.consignment_out.update_consignment_line_prices",
			{"consignment_out": "CONO-0007",
			 "prices": {"LINE-1": {"cost": 500, "msrp": 650}}},
		))

	# ------------------------------------------------------------- coverage

	def test_all_new_tools_registered(self):
		for name in (
			"item_stock", "available_serials", "rsr_catalog_search", "boundbook_reconcile", "boundbook_mismatches",
			"receive_goods", "add_stock", "set_stock", "toggle_service_need",
			"push_serial_to_fastbound", "verify_supplier_ffl", "reverify_all_ffls",
			"promote_to_item", "backfill_from_rsr", "set_serial_title",
			"woo_push_serial", "woo_delist_serial",
			# PR-4a: GunBroker listing-side channel
			"gb_test_connection", "gb_push_serial", "gb_end_listing", "gb_listing_status",
			# PR-4b: GunBroker order side
			"gb_pull_orders",
			"pending_orders", "pending_web_orders",
			"dispose_order", "dispose_web_order", "record_payment",
			"card_charges", "void_card_charge", "refund_card_charge", "resolve_card_reversal",
			"push_shipment", "mark_shipped_manually", "shipstation_test_connection",
			"start_4473", "manager_override_4473", "start_transfer_4473",
			"upload_attachment",
			# consignment out lifecycle + counter-order cancel (P1 补齐)
			"consignment_queue", "consignment_dealers", "consignment_serials",
			"consignment_dealer_orders", "create_consignment_out",
			"ship_consignment_out", "push_consignment_shipment",
			"mark_consignment_shipped", "retry_consignment_invoice",
			"return_consignment_lines", "cancel_consignment", "cancel_order",
			# PR #224 increment: At-dealer price snapshot edit
			"update_consignment_prices",
		):
			self.assertIn(name, self.tools)

	def test_curated_tool_count_pinned(self):
		# This pins the CURATED bucket with the GunBroker actions ON. Total =
		# 57 curated + 10 generic + 12 distributor + 7 reports + 30 shop-floor = 116, pinned in
		# test_modes.py::test_full_mode_registers_the_whole_surface_including_the_cpa_surface.
		# The DEFAULT surface is 3 lower here and 7 lower overall (both gates off).
		# Moving any of these means moving README.md, CLAUDE.md and TOOLS.md.
		self.assertEqual(len(self.tools), 61)


class GunBrokerActionGate(unittest.TestCase):
	"""gb_push_serial / gb_end_listing / gb_pull_orders are not registered unless
	asked for.

	confirm= catches a misfire. It does not catch an agent that has reasoned its
	way into believing it ought to confirm — and the user-level instance really
	does point at prod. An absent tool cannot be reasoned about
	(tools/distributor.py:169-177, same stance).
	"""
	# name -> the positional args that make a valid call (gb_pull_orders takes none)
	WRITE_ARGS = {"gb_push_serial": ("SN1",), "gb_end_listing": ("SN1",),
		"gb_pull_orders": ()}
	WRITES = tuple(WRITE_ARGS)
	READS = ("gb_test_connection", "gb_listing_status")

	def _tools(self, gb_actions):
		return _register(FakeMCP(), gb_actions=gb_actions).tools

	def test_writes_are_absent_by_default(self):
		tools = self._tools(None)
		for name in self.WRITES:
			self.assertNotIn(name, tools)

	def test_reads_are_always_registered(self):
		"""Only the irreversible half is gated. Looking at a listing, and asking
		which GunBroker you are pointed at, must not need an opt-in — that is the
		call you want someone to make BEFORE they reach for a write."""
		for gb_actions in (None, "1"):
			with self.subTest(gb_actions=gb_actions):
				tools = self._tools(gb_actions)
				for name in self.READS:
					self.assertIn(name, tools)

	def test_the_flag_registers_them(self):
		tools = self._tools("1")
		for name in self.WRITES:
			self.assertIn(name, tools)

	def test_truthy_spellings_accepted_anything_else_is_off(self):
		"""Fail-closed by construction: unlike GUNSTORE_MCP_MODE this does not
		refuse to boot on a typo, because anything unrecognised simply leaves the
		writes off. Mirrors distributor.actions_enabled exactly."""
		for value in ("1", "true", "TRUE", "yes", "on", " on "):
			with self.subTest(on=value):
				self.assertIn("gb_push_serial", self._tools(value))
		for value in ("", "0", "no", "off", "false", "maybe", "2"):
			with self.subTest(off=value):
				self.assertNotIn("gb_push_serial", self._tools(value))

	def test_gating_does_not_drop_the_tools_defined_after_it(self):
		"""The gb block sits in the middle of register(), so an early `return`
		here — the shape distributor.py can afford, since its actions are last —
		would silently take out everything below it."""
		off, on = self._tools(None), self._tools("1")
		self.assertEqual(len(on) - len(off), len(self.WRITES))
		for name in ("atf_verify_ffl", "firearms_in_stock", "upload_attachment",
				"cancel_order", "update_consignment_prices"):
			self.assertIn(name, off)

	def test_writes_still_need_confirm_when_registered(self):
		"""The gate replaces nothing: both layers apply."""
		tools = self._tools("1")
		client = FakeClient()
		with patch.object(curated, "get_client", return_value=client):
			for name, args in self.WRITE_ARGS.items():
				with self.subTest(name), self.assertRaises(WriteRefused):
					tools[name](*args)
			self.assertEqual(client.calls, [])


if __name__ == "__main__":
	unittest.main()
