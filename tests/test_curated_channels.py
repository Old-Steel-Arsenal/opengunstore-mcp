"""Channel tools in tools/curated.py (register_channels): web-order repairs, Woo store
sweeps, the GunBroker order lane and the card processor probe.

Fully mocked like the rest of the suite: they pin the dotted method path and the exact
kwargs each tool sends, the confirm gate on every write, and the argument validation
that refuses before any call. Signatures were checked against gunstore-pos develop
(merge 6e1c546); order_audit.shipped_undisposed_orders is a planned POS endpoint.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from gunstore_mcp.frappe_client import FrappeAPIError
from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import curated

WOO = "ffl_woo_sync.woocommerce."
GB = "ffl_integrations.gunbroker."
PAY = "ffl_integrations.payments."


class FakeClient:
	def __init__(self, answers=None):
		self.calls = []
		self.answers = answers or {}

	def call_method(self, method, kwargs=None, timeout=None):
		self.calls.append(("call_method", method, kwargs or {}) + ((timeout,) if timeout else ()))
		ans = self.answers.get(method, {"ok": True})
		if isinstance(ans, Exception):
			raise ans
		return ans


class FakeMCP:
	def __init__(self):
		self.tools = {}

	def tool(self, *a, **k):
		def deco(fn):
			self.tools[fn.__name__] = fn
			return fn
		return deco


def _register(gb_actions: str | None):
	env = {k: v for k, v in os.environ.items() if k != curated.GUNBROKER_ACTIONS_ENV}
	if gb_actions is not None:
		env[curated.GUNBROKER_ACTIONS_ENV] = gb_actions
	mcp = FakeMCP()
	with patch.dict(os.environ, env, clear=True), \
			patch.object(curated, "_load_env", lambda: None):
		curated.register_channels(mcp)
	return mcp.tools


DEFAULT_TOOLS = {"web_order_action", "web_order_exceptions", "woo_store_maintenance",
	"gb_pending_orders", "gb_record_payment", "gb_order_action", "gb_dispose_order",
	"card_processor_test_connection"}


class Registration(unittest.TestCase):
	def test_default_surface(self):
		self.assertEqual(set(_register(None)), DEFAULT_TOOLS)

	def test_gb_update_listing_only_behind_the_switch(self):
		self.assertNotIn("gb_update_listing", _register("0"))
		self.assertEqual(set(_register("1")), DEFAULT_TOOLS | {"gb_update_listing"})


class Base(unittest.TestCase):
	answers: dict = {}

	def setUp(self):
		self.tools = _register("1")
		self.client = FakeClient(dict(self.answers))
		p = patch.object(curated, "get_client", return_value=self.client)
		p.start()
		self.addCleanup(p.stop)

	def _last(self):
		return self.client.calls[-1]

	def refused(self, exc, name, **kw):
		with self.assertRaises(exc):
			self.tools[name](**kw)
		self.assertEqual(self.client.calls, [])


class WebOrderAction(Base):
	def test_per_order_actions(self):
		for action, method in [("pickup", "pickup.pickup_web_order"),
				("book_invoice", "revenue.create_web_invoice_now"),
				("reconcile_refund", "refunds.reconcile_web_order_refund"),
				("retire_antique", "antique.retire_antique_web_order_now")]:
			self.tools["web_order_action"](action, woo_online_order="WOO-1", confirm=True)
			self.assertEqual(self._last(),
				("call_method", WOO + method, {"woo_online_order": "WOO-1"}))

	def test_store_wide_actions_send_nothing_and_wait_longer(self):
		for action, method in [("run_backstop", "order_audit.run_web_revenue_backstop_now"),
				("retry_serials", "tasks.retry_stuck_serial_reservations")]:
			self.tools["web_order_action"](action, confirm=True)
			self.assertEqual(self._last(), ("call_method", WOO + method, {}, 130))

	def test_confirm_required(self):
		self.refused(WriteRefused, "web_order_action", action="pickup", woo_online_order="W")
		self.refused(WriteRefused, "web_order_action", action="run_backstop")

	def test_argument_validation(self):
		self.refused(ValueError, "web_order_action", action="pickup", confirm=True)
		self.refused(ValueError, "web_order_action", action="run_backstop",
			woo_online_order="W", confirm=True)
		self.refused(ValueError, "web_order_action", action="dispose",
			woo_online_order="W", confirm=True)


class WebOrderExceptions(Base):
	answers = {
		WOO + "order_audit.web_stock_legs_outstanding": {"invoices": ["SI-1"], "count": 1},
		WOO + "order_audit.shipped_undisposed_orders": [{"name": "WOO-2"}],
	}

	def test_both_reads_and_next_steps(self):
		out = self.tools["web_order_exceptions"]("2026-09-01", "2026-09-30")
		self.assertEqual(self.client.calls, [
			("call_method", WOO + "order_audit.web_stock_legs_outstanding",
				{"from_date": "2026-09-01", "to_date": "2026-09-30"}),
			("call_method", WOO + "order_audit.shipped_undisposed_orders", {}),
		])
		self.assertEqual(out["stock_legs_outstanding"]["count"], 1)
		self.assertEqual(out["shipped_not_disposed"], [{"name": "WOO-2"}])
		self.assertEqual(len(out["next_steps"]), 2)

	def test_dates_default_to_none(self):
		self.tools["web_order_exceptions"]()
		self.assertEqual(self.client.calls[0][2], {"from_date": None, "to_date": None})

	def test_ambiguous_date_refused(self):
		self.refused(ValueError, "web_order_exceptions", from_date="09/01/2026")

	def test_older_pos_without_the_new_endpoint_still_answers_the_other_half(self):
		self.client.answers[WOO + "order_audit.shipped_undisposed_orders"] = \
			FrappeAPIError(404, "DoesNotExistError", ["no such method"])
		out = self.tools["web_order_exceptions"]()
		self.assertEqual(out["stock_legs_outstanding"]["count"], 1)
		self.assertIn("error", out["shipped_not_disposed"])


class WooStoreMaintenance(Base):
	def test_reports_by_default_without_confirm(self):
		t = self.tools["woo_store_maintenance"]
		t("backfill_attributes", item_codes=["I1"])
		self.assertEqual(self._last(), ("call_method",
			WOO + "condition_backfill.backfill_condition_attributes",
			{"serials": None, "item_codes": ["I1"], "dry_run": 1}, 130))
		t("recategorize", under=["rifles"])
		self.assertEqual(self._last(), ("call_method", WOO + "categories.recategorize_store",
			{"site": "retail", "dry_run": 1, "under": ["rifles"]}, 130))
		t("audit_dangling_ids")
		self.assertEqual(self._last(), ("call_method", WOO + "dangling.woo_audit_dangling_ids",
			{"site": "retail", "fix": 0, "batch_size": 50}, 130))

	def test_apply_needs_confirm_and_flips_the_flag(self):
		t = self.tools["woo_store_maintenance"]
		self.refused(WriteRefused, "woo_store_maintenance", action="recategorize", apply=True)
		t("recategorize", apply=True, confirm=True)
		self.assertEqual(self._last()[2]["dry_run"], 0)
		t("audit_dangling_ids", apply=True, batch_size=100, confirm=True)
		self.assertEqual(self._last()[2], {"site": "retail", "fix": 1, "batch_size": 100})
		t("backfill_attributes", apply=True, serials=["SN1"], confirm=True)
		self.assertEqual(self._last()[2], {"serials": ["SN1"], "item_codes": None, "dry_run": 0})

	def test_argument_validation(self):
		self.refused(ValueError, "woo_store_maintenance", action="backfill_attributes",
			apply=True, confirm=True)  # whole-store push refused
		self.refused(ValueError, "woo_store_maintenance", action="recategorize",
			serials=["SN1"])
		self.refused(ValueError, "woo_store_maintenance", action="audit_dangling_ids",
			under=["x"])
		self.refused(ValueError, "woo_store_maintenance", action="backfill_attributes",
			batch_size=10, item_codes=["I"])
		self.refused(ValueError, "woo_store_maintenance", action="purge")


class GbPendingOrders(Base):
	answers = {GB + "fulfillment.list_pending_gb_orders": [
		{"gunbroker_order": "GBO-1", "stock_units": []},
		{"gunbroker_order": "GBO-2", "stock_units": [{"item_code": "AMMO", "qty": 2}]},
	], GB + "fulfillment.get_dispose_stock_lines": [{"line": "123"}]}

	def test_whole_list(self):
		out = self.tools["gb_pending_orders"]()
		self.assertEqual(len(out), 2)
		self.assertEqual(self.client.calls,
			[("call_method", GB + "fulfillment.list_pending_gb_orders", {})])

	def test_one_card_without_loose_stock(self):
		out = self.tools["gb_pending_orders"]("GBO-1")
		self.assertEqual(out["gunbroker_order"], "GBO-1")
		self.assertNotIn("stock_lines", out)
		self.assertIs(out["banned_customer"], False)
		self.assertEqual(self._last(), ("call_method",
			"ffl_core.customer_flags.banned_order_rows", {"gb_orders": ["GBO-1"]}))
		self.assertEqual(len(self.client.calls), 2)

	def test_banned_buyer_flagged(self):
		self.client.answers["ffl_core.customer_flags.banned_order_rows"] = {"gb": ["GBO-1"]}
		self.assertIs(self.tools["gb_pending_orders"]("GBO-1")["banned_customer"], True)

	def test_banned_lookup_refused_is_reported_not_hidden(self):
		self.client.answers["ffl_core.customer_flags.banned_order_rows"] = FrappeAPIError(
			403, "PermissionError", ["no Customer read"])
		self.assertIn("error", self.tools["gb_pending_orders"]("GBO-1")["banned_customer"])

	def test_one_card_with_loose_stock_adds_stock_lines(self):
		out = self.tools["gb_pending_orders"]("GBO-2")
		self.assertEqual(self._last(), ("call_method",
			GB + "fulfillment.get_dispose_stock_lines", {"gunbroker_order": "GBO-2"}))
		self.assertEqual(out["stock_lines"], [{"line": "123"}])

	def test_not_open(self):
		self.assertEqual(self.tools["gb_pending_orders"]("GBO-9")["found"], False)


class GbRecordPayment(Base):
	def test_payload(self):
		self.tools["gb_record_payment"]("GBO-1", "GunBroker Check", 512.5,
			reference_no=" 1044 ", received_on="2026-10-28", confirm=True)
		self.assertEqual(self._last(), ("call_method", GB + "fulfillment.mark_payment_received",
			{"gunbroker_order": "GBO-1", "mode_of_payment": "GunBroker Check",
			 "amount": 512.5, "reference_no": "1044", "received_on": "2026-10-28",
			 "replace_posted_receipt": 0}))

	def test_card_with_replace(self):
		self.tools["gb_record_payment"]("GBO-1", "GunBroker Card", 100,
			replace_posted_receipt=True, confirm=True)
		self.assertEqual(self._last()[2], {"gunbroker_order": "GBO-1",
			"mode_of_payment": "GunBroker Card", "amount": 100, "reference_no": None,
			"received_on": None, "replace_posted_receipt": 1})

	def test_confirm_required(self):
		self.refused(WriteRefused, "gb_record_payment", gunbroker_order="GBO-1",
			mode_of_payment="GunBroker Card", amount=10)

	def test_argument_validation(self):
		base = {"gunbroker_order": "GBO-1", "confirm": True}
		self.refused(ValueError, "gb_record_payment", **base,
			mode_of_payment="GunBroker Check", amount=10)  # no cheque number
		self.refused(ValueError, "gb_record_payment", **base,
			mode_of_payment="Cash", amount=10)
		self.refused(ValueError, "gb_record_payment", **base,
			mode_of_payment="GunBroker Card", amount=0)
		self.refused(ValueError, "gb_record_payment", **base,
			mode_of_payment="GunBroker Card", amount=10, received_on="10/28/2026")


class GbOrderAction(Base):
	def test_unit_actions(self):
		t = self.tools["gb_order_action"]
		t("GBO-1", "assign_unit", row_name="9001:1", serial_no="SN1", confirm=True)
		self.assertEqual(self._last(), ("call_method", GB + "fulfillment.assign_gb_unit_serial",
			{"gunbroker_order": "GBO-1", "row_name": "9001:1", "serial_no": "SN1"}))
		t("GBO-1", "unassign_unit", row_name="9001:1", confirm=True)
		self.assertEqual(self._last(), ("call_method", GB + "fulfillment.assign_gb_unit_serial",
			{"gunbroker_order": "GBO-1", "row_name": "9001:1", "serial_no": ""}))
		t("GBO-1", "mark_unit_refunded", row_name="9001:2", confirm=True)
		self.assertEqual(self._last(), ("call_method", GB + "fulfillment.mark_gb_unit_refunded",
			{"gunbroker_order": "GBO-1", "row_name": "9001:2", "refunded": 1}))
		t("GBO-1", "undo_unit_refunded", row_name="9001:2", confirm=True)
		self.assertEqual(self._last()[2]["refunded"], 0)

	def test_order_actions(self):
		for action, method in [("push_shipment", "fulfillment.push_gb_shipment"),
				("push_tracking", "fulfillment.push_tracking_now"),
				("retry_invoice", "revenue.create_gb_invoice_now")]:
			self.tools["gb_order_action"]("GBO-1", action, confirm=True)
			self.assertEqual(self._last(),
				("call_method", GB + method, {"gunbroker_order": "GBO-1"}))

	def test_confirm_required(self):
		self.refused(WriteRefused, "gb_order_action", gunbroker_order="GBO-1",
			action="push_tracking")

	def test_argument_validation(self):
		base = {"gunbroker_order": "GBO-1", "confirm": True}
		self.refused(ValueError, "gb_order_action", **base, action="assign_unit",
			serial_no="SN1")  # no row
		self.refused(ValueError, "gb_order_action", **base, action="assign_unit",
			row_name="9001:1")  # no serial: that would unassign
		self.refused(ValueError, "gb_order_action", **base, action="unassign_unit",
			row_name="9001:1", serial_no="SN1")
		self.refused(ValueError, "gb_order_action", **base, action="push_shipment",
			row_name="9001:1")
		self.refused(ValueError, "gb_order_action", **base, action="refund")


class GbDisposeOrder(Base):
	def test_payload(self):
		cr = {"license_number": "1-23-456-03-7C-89012", "license_expiry": "2027-01-01",
			"contact_name": "A B", "premise_street": "1 Main", "premise_city": "X",
			"premise_state": "TX", "premise_zip": "75001"}
		self.tools["gb_dispose_order"]("GBO-1", locations={"123": "LOC-A"},
			cr_details=cr, confirm=True)
		self.assertEqual(self._last(), ("call_method", GB + "fulfillment.dispose_gb_order",
			{"gunbroker_order": "GBO-1", "cr_details": cr, "locations": {"123": "LOC-A"}}))

	def test_plain_order(self):
		self.tools["gb_dispose_order"]("GBO-1", confirm=True)
		self.assertEqual(self._last()[2],
			{"gunbroker_order": "GBO-1", "cr_details": None, "locations": None})

	def test_confirm_required(self):
		self.refused(WriteRefused, "gb_dispose_order", gunbroker_order="GBO-1")

	def test_unknown_cr_key_refused(self):
		self.refused(ValueError, "gb_dispose_order", gunbroker_order="GBO-1",
			cr_details={"licence_number": "x"}, confirm=True)


class CardProcessorProbe(Base):
	answers = {PAY + "hub.overview": {"processors": [{"code": "payroc"}, {"code": "other"}]},
		PAY + "hub.test_connection": {"checks": []}}

	def test_probes_every_processor(self):
		out = self.tools["card_processor_test_connection"]()
		self.assertEqual(self.client.calls, [
			("call_method", PAY + "hub.overview", {}),
			("call_method", PAY + "hub.test_connection", {"processor": "payroc"}),
			("call_method", PAY + "hub.test_connection", {"processor": "other"}),
		])
		self.assertEqual(set(out["probes"]), {"payroc", "other"})
		self.assertNotIn("terminals", out)

	def test_one_processor_and_terminals(self):
		self.tools["card_processor_test_connection"]("payroc", list_terminals=True)
		self.assertEqual(self.client.calls[1:], [
			("call_method", PAY + "hub.test_connection", {"processor": "payroc"}),
			("call_method", PAY + "api.list_terminals", {}),
		])

	def test_permission_error_is_that_parts_answer(self):
		self.client.answers[PAY + "hub.overview"] = FrappeAPIError(
			403, "PermissionError", ["Not permitted"])
		out = self.tools["card_processor_test_connection"](list_terminals=True)
		self.assertEqual(out["overview"]["status"], 403)
		self.assertEqual(out["probes"], {})
		self.assertEqual(out["terminals"], {"ok": True})


class GbUpdateListing(Base):
	def test_payloads(self):
		t = self.tools["gb_update_listing"]
		cases = [
			(("reprice",), {"serial_no": "SN1"}, "reprice_serial_now", {"serial_no": "SN1"}),
			(("reprice",), {"item_code": "I1"}, "reprice_item_now", {"item_code": "I1"}),
			(("replace_pictures",), {"serial_no": "SN1"}, "replace_pictures_now",
				{"serial_no": "SN1"}),
			(("replace_pictures",), {"item_code": "I1"}, "replace_pictures_now",
				{"item_code": "I1"}),
			(("sync_quantity",), {"item_code": "I1"}, "sync_item_quantity_now",
				{"item_code": "I1"}),
			(("clear_sold_unknown",), {"serial_no": "SN1", "note": " checked GB "},
				"clear_sold_unknown_now", {"serial_no": "SN1", "note": "checked GB"}),
		]
		for args, kw, method, sent in cases:
			t(*args, confirm=True, **kw)
			self.assertEqual(self._last(), ("call_method", GB + "client_api." + method, sent))

	def test_confirm_required(self):
		self.refused(WriteRefused, "gb_update_listing", action="reprice", serial_no="SN1")

	def test_argument_validation(self):
		c = {"confirm": True}
		self.refused(ValueError, "gb_update_listing", action="reprice", **c)
		self.refused(ValueError, "gb_update_listing", action="reprice",
			serial_no="SN1", item_code="I1", **c)
		self.refused(ValueError, "gb_update_listing", action="sync_quantity",
			serial_no="SN1", **c)
		self.refused(ValueError, "gb_update_listing", action="clear_sold_unknown",
			item_code="I1", note="x", **c)
		self.refused(ValueError, "gb_update_listing", action="clear_sold_unknown",
			serial_no="SN1", note="  ", **c)
		self.refused(ValueError, "gb_update_listing", action="reprice",
			serial_no="SN1", note="x", **c)


if __name__ == "__main__":
	unittest.main()
