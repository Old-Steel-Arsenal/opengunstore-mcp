"""Unit tests for the generic backbone guards: the destructive-verb confirm
gate on frappe_run_method (incl. the domain verbs dispose/push/charge/
consolidate) and the blocked generic Frappe mutators."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import generic


class FakeClient:
	def __init__(self):
		self.calls = []

	def call_method(self, method, kwargs=None):
		self.calls.append(("call_method", method, kwargs or {}))
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


class RunMethodGuards(unittest.TestCase):
	def setUp(self):
		mcp = FakeMCP()
		generic.register(mcp)
		self.run_method = mcp.tools["frappe_run_method"]
		self.client = FakeClient()
		self._patch = patch.object(generic, "get_client", return_value=self.client)
		self._patch.start()
		self.addCleanup(self._patch.stop)

	def test_benign_method_needs_no_confirm(self):
		self.run_method("ffl_core.api.manual_order.list_pending_dispositions")
		self.assertEqual(self.client.calls[-1][1],
			"ffl_core.api.manual_order.list_pending_dispositions")

	def test_always_confirm_methods_gated_by_exact_name(self):
		# review q2 (run 2026-07-16-mcp-cpa-mode): high-consequence writes whose
		# last path segment carries no destructive verb — the regex can't see
		# them, so they are gated by EXACT dotted name.
		for method in (
			"ffl_core.api.manual_order.update_order",
			"osa_consignment.api.consignment_out.update_consignment_line_prices",
			"osa_consignment.api.consignment_out.create_consignment_out",
			"ffl_core.api.manual_order.record_payment",
			"osa_consignment.api.dealer_orders.create_consignment_invoice_now",
			"ffl_core.api.item_admin.add_stock",
			"ffl_core.api.item_admin.set_stock",
			"ffl_core.api.manual_order.set_customer_tax_exempt",
			"ffl_core.api.manual_order.release_order_legs",
		):
			with self.assertRaises(WriteRefused, msg=method):
				self.run_method(method)
			self.run_method(method, confirm=True)
			self.assertEqual(self.client.calls[-1][1], method)

	def test_domain_destructive_verbs_gated(self):
		for method in (
			"ffl_core.api.manual_order.dispose_order",
			"ffl_woo_sync.woocommerce.fulfillment.dispose_web_order",
			"ffl_integrations.shipstation.api.push_shipment",
			"ffl_woo_sync.woocommerce.client_api.push_serial_now",
			# the legacy payroc.api alias is refused outright now (test_catalog.py)
			"ffl_integrations.payments.api.charge_pos_invoice",
			"ffl_core.api.pos_consolidate.consolidate_pos_invoice_now",
		):
			with self.assertRaises(WriteRefused, msg=method):
				self.run_method(method)
			self.run_method(method, confirm=True)
			self.assertEqual(self.client.calls[-1][1], method)

	def test_consignment_and_lifecycle_verbs_gated(self):
		# audit 附注 a: the consignment / onboarding whitelisted writes carry
		# verbs (ship / mark_*_shipped / mark_sold / mark_received / return /
		# onboard / receive) that the original regex missed — a bare
		# frappe_run_method call could dispose stock or mint invoices unconfirmed.
		for method in (
			"osa_consignment.api.consignment_out.ship_consignment_out",
			"osa_consignment.api.consignment_out.mark_consignment_shipped_manually",
			"osa_consignment.api.consignment_out.return_consignment_lines",
			"osa_consignment.api.consignment_out.cancel_consignment_out",
			"osa_consignment.api.portal.mark_sold",
			"osa_consignment.api.portal.mark_received",
			"osa_consignment.api.dealer_onboarding.onboard_dealer",
			"ffl_core.api.receive_goods.create_receive",
			"ffl_core.api.manual_order.mark_shipped_manually",
		):
			with self.assertRaises(WriteRefused, msg=method):
				self.run_method(method)
			self.run_method(method, confirm=True)
			self.assertEqual(self.client.calls[-1][1], method)

	def test_consignment_reads_stay_ungated(self):
		for method in (
			"osa_consignment.api.consignment_out.list_consignment_queue",
			"osa_consignment.api.consignment_out.list_consignment_dealers",
			"osa_consignment.api.consignment_out.available_serials_for_consignment",
			"osa_consignment.api.dealer_orders.list_dealer_orders",
		):
			self.run_method(method)
			self.assertEqual(self.client.calls[-1][1], method)

	def test_verb_in_module_segment_does_not_gate(self):
		# Gating matches the FINAL path segment only — a module named after a
		# verb must not gate an innocuous method inside it.
		self.run_method("ffl_core.disposal.list_rows")
		self.assertEqual(self.client.calls[-1][1], "ffl_core.disposal.list_rows")

	def test_blocked_generic_mutators_refused_even_with_confirm(self):
		with self.assertRaises(WriteRefused):
			self.run_method("frappe.client.set_value", confirm=True)
		self.assertEqual(self.client.calls, [])

	def test_generic_tool_count_pinned(self):
		# 11 generic (10 Frappe CRUD/method + list_pos_actions) + 52 curated + 5 reports = 67 total (full mode; cpa = 18,
		# pinned by set equality in test_modes). TOOLS.md / CLAUDE.md / README.md
		# quote 67 — move all three if this moves.
		mcp = FakeMCP()
		generic.register(mcp)
		self.assertEqual(len(mcp.tools), 11)


if __name__ == "__main__":
	unittest.main()
