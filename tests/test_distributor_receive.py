"""Distributor receiving tools: distributor_incoming / distributor_receive /
distributor_ensure_item.

Pins the dotted method names and payload keys (checked against the POS
`ffl_integrations.distributor.receive_bridge` and `distributor.api` source), the
confirm gates, and the argument checks that must fire before any network call.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import distributor

_RB = "ffl_integrations.distributor.receive_bridge."

_INCOMING = {"groups": [
    {"label": "WEB-1", "customer_name": "A Buyer", "orders": [
        {"do_name": "DO-1", "lines": [{"line_name": "L1", "outstanding": 1}]}]},
    {"label": "Restock", "customer_name": "", "orders": [
        {"do_name": "DO-2", "lines": []},
        {"do_name": "", "invoice_name": "INV-9", "lines": []}]},
]}


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeClient:
    def __init__(self, reply=None):
        self.calls = []
        self.reply = {"ok": True} if reply is None else reply

    def call_method(self, method, args=None, timeout=None):
        self.calls.append((method, args or {}, timeout))
        return self.reply


def _register() -> dict:
    """Default surface: the action gate closed, as it is in production."""
    env = {k: v for k, v in os.environ.items() if k != distributor.ACTIONS_ENV}
    mcp = FakeMCP()
    with patch.dict(os.environ, env, clear=True), \
            patch.object(distributor, "_load_env", lambda: None):
        distributor.register(mcp)
    return mcp.tools


class _Base(unittest.TestCase):
    reply = None

    def setUp(self):
        self.tools = _register()
        self.client = FakeClient(self.reply)
        p = patch.object(distributor, "get_client", return_value=self.client)
        p.start()
        self.addCleanup(p.stop)


class Surface(_Base):
    def test_receiving_tools_are_registered_with_the_gate_closed(self):
        # They place nothing with the distributor, so they are not behind the switch.
        for name in ("distributor_incoming", "distributor_receive",
                     "distributor_ensure_item"):
            self.assertIn(name, self.tools)


class Incoming(_Base):
    reply = _INCOMING

    def test_full_list(self):
        out = self.tools["distributor_incoming"]()
        self.assertEqual(self.client.calls, [(_RB + "list_incoming", {}, None)])
        self.assertEqual(out, _INCOMING)

    def test_one_order(self):
        out = self.tools["distributor_incoming"](do_name="DO-1")
        self.assertEqual(self.client.calls[0][0], _RB + "list_incoming")
        self.assertEqual(out["order"]["do_name"], "DO-1")
        self.assertEqual(out["label"], "WEB-1")

    def test_an_order_with_nothing_outstanding_says_so(self):
        out = self.tools["distributor_incoming"](do_name="DO-404")
        self.assertIsNone(out["order"])
        self.assertIn("note", out)

    def test_item_code_matches_against_waiting_orders(self):
        self.client.reply = {"item_code": "UPC1", "candidates": [
            {"do_name": "DO-1"}, {"do_name": "DO-2"}]}
        out = self.tools["distributor_incoming"](item_code=" UPC1 ")
        self.assertEqual(self.client.calls,
                         [(_RB + "match_incoming", {"item_code": "UPC1"}, None)])
        self.assertEqual(len(out["candidates"]), 2)

    def test_item_code_and_do_name_narrow_the_candidates(self):
        self.client.reply = {"item_code": "UPC1", "candidates": [
            {"do_name": "DO-1"}, {"do_name": "DO-2"}]}
        out = self.tools["distributor_incoming"](item_code="UPC1", do_name="DO-2")
        self.assertEqual(out["candidates"], [{"do_name": "DO-2"}])


class Receive(_Base):
    reply = _INCOMING

    # ---- argument checks (before any call) -------------------------------

    def test_needs_exactly_one_target(self):
        for kw in ({}, {"do_name": "DO-1", "invoice_name": "INV-9"}):
            with self.assertRaises(ValueError):
                self.tools["distributor_receive"](**kw)
        self.assertEqual(self.client.calls, [])

    def test_bad_items_are_refused_before_the_call(self):
        bad = ([], [{"qty": 1}], [{"line_name": " ", "qty": 1}],
               [{"line_name": "L1", "qty": 0}], [{"line_name": "L1", "qty": "1"}],
               [{"line_name": "L1", "qty": True}], ["L1"])
        for items in bad:
            with self.assertRaises(ValueError, msg=repr(items)):
                self.tools["distributor_receive"](do_name="DO-1", items=items,
                                                  confirm=True)
        self.assertEqual(self.client.calls, [])

    def test_booking_refuses_without_confirm(self):
        with self.assertRaises(WriteRefused):
            self.tools["distributor_receive"](
                do_name="DO-1", items=[{"line_name": "L1", "qty": 1, "serials": ["S1"]}])
        with self.assertRaises(WriteRefused):
            self.tools["distributor_receive"](
                invoice_name="INV-9", items=[{"line_name": "IL1", "qty": 1}])
        self.assertEqual(self.client.calls, [])

    # ---- prepare (no confirm) --------------------------------------------

    def test_prepare_an_order_reads_its_outstanding_lines(self):
        out = self.tools["distributor_receive"](do_name="DO-1")
        self.assertEqual(self.client.calls, [(_RB + "list_incoming", {}, None)])
        self.assertEqual(out["order"]["lines"], [{"line_name": "L1", "outstanding": 1}])

    def test_prepare_an_invoice_stages_it(self):
        self.tools["distributor_receive"](invoice_name="INV-9")
        self.assertEqual(self.client.calls,
                         [(_RB + "stage_invoice", {"invoice_name": "INV-9"}, 130)])

    # ---- booking: exact payloads -----------------------------------------

    def test_receive_order_lines(self):
        items = [{"line_name": "L1", "qty": 1, "serials": ["S1"], "rate": 450.0}]
        self.tools["distributor_receive"](do_name="DO-1", items=items, confirm=True)
        self.assertEqual(self.client.calls,
                         [(_RB + "receive_lines", {"do_name": "DO-1", "items": items}, 130)])

    def test_receive_invoice_lines_with_warehouse(self):
        items = [{"line_name": "IL1", "qty": 2, "rate": 0, "zero_cost_confirmed": 1,
                  "zero_cost_reason": "Supplier gift"}]
        self.tools["distributor_receive"](invoice_name="INV-9", items=items,
                                          warehouse="Back Room - S", confirm=True)
        self.assertEqual(self.client.calls, [(
            _RB + "receive_invoice_lines",
            {"invoice_name": "INV-9", "items": items, "warehouse": "Back Room - S"},
            130)])


class EnsureItem(_Base):
    def test_requires_a_sku(self):
        with self.assertRaises(ValueError):
            self.tools["distributor_ensure_item"](sku="  ", confirm=True)
        self.assertEqual(self.client.calls, [])

    def test_refuses_without_confirm(self):
        with self.assertRaises(WriteRefused):
            self.tools["distributor_ensure_item"](sku="ABC123")
        self.assertEqual(self.client.calls, [])

    def test_restock_item_sends_claim_zero(self):
        # The POS defaults claim to 1: omitting it would mark the Item drop-shipped.
        self.tools["distributor_ensure_item"](sku=" ABC123 ", confirm=True)
        self.assertEqual(self.client.calls, [(
            "ffl_integrations.distributor.api.ensure_dropship_item",
            {"sku": "ABC123", "distributor": None, "claim": 0}, None)])

    def test_dropship_item_sends_claim_one(self):
        self.tools["distributor_ensure_item"](sku="ABC123", distributor="RSR",
                                              dropship=True, confirm=True)
        self.assertEqual(self.client.calls[0][1],
                         {"sku": "ABC123", "distributor": "RSR", "claim": 1})


if __name__ == "__main__":
    unittest.main()
