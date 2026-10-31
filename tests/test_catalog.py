"""catalog.py's annotation maps point at real tools that really call the method.

list_pos_actions tells an agent "use_tool: X" for a method. A stale entry sends it to a
tool that does not exist, or that no longer wraps the method; this checks both."""
from __future__ import annotations

import inspect
import unittest

from gunstore_mcp.catalog import METHOD_NOTES, METHOD_SWITCH, METHOD_TO_TOOL
from gunstore_mcp.tools import generic

from tests.test_never_writes_surface import _surface


# Covered without a direct call: find_possible_duplicates runs it for customer_find.
_INDIRECT = {"ffl_core.customer_flags.find_customers_by_id_number"}


class CatalogMaps(unittest.TestCase):
    def setUp(self):
        self.tools = _surface()

    def test_every_use_tool_is_registered_and_calls_the_method(self):
        self.assertTrue(METHOD_TO_TOOL)
        for method, tool in METHOD_TO_TOOL.items():
            with self.subTest(method=method):
                self.assertIn(tool, self.tools)
                if method in _INDIRECT:
                    continue
                source = inspect.getsource(inspect.getmodule(self.tools[tool]))
                module_seg, name = method.rsplit(".", 2)[-2:]
                # Modules build paths from prefixes (_GB + "client_api.x"), so match
                # the last two segments rather than the full dotted string.
                self.assertTrue(f"{module_seg}.{name}" in source or
                                (name in source and module_seg in source),
                                f"{tool}'s module never names {method}")

    def test_the_generic_listing_uses_these_maps(self):
        self.assertIs(generic.METHOD_TO_TOOL, METHOD_TO_TOOL)
        self.assertIs(generic.METHOD_NOTES, METHOD_NOTES)
        self.assertIs(generic.METHOD_SWITCH, METHOD_SWITCH)

    def test_no_blocked_method_points_at_a_tool(self):
        for method in METHOD_TO_TOOL:
            with self.subTest(method=method):
                self.assertIsNone(generic._blocked_reason(method))


class RunMethodRefusals(unittest.TestCase):
    """The argument-dependent refusals frappe_run_method makes before any call."""

    def setUp(self):
        self.tools = _surface()

    def _refused(self, method, kwargs):
        from unittest.mock import patch

        from gunstore_mcp.safety import WriteRefused
        calls = []

        class Client:
            def call_method(self, *a, **k):
                calls.append(a)
                return {}

        with patch.object(generic, "get_client", return_value=Client()):
            with self.assertRaises(WriteRefused):
                self.tools["frappe_run_method"](method, kwargs, confirm=True)
        self.assertEqual(calls, [])

    def test_refusals(self):
        for method, kwargs in (
            ("ffl_integrations.distributor.receive_bridge.receive_from_do", {"do_name": "D"}),
            ("ffl_woo_sync.woocommerce.condition_backfill.backfill_condition_attributes",
             {"dry_run": 1}),
            ("ffl_core.agreements.request_agreement", {"customer": "C", "template": "T"}),
            ("ffl_core.agreements.request_agreement", {"deliver": "qr"}),
            ("ffl_integrations.fastbound.inventory_sync.sync_in_stock_from_boundbook",
             {"dry_run": 0}),
            ("ffl_integrations.fastbound.inventory_sync.sync_in_stock_from_boundbook",
             {"dry_run": "0", "item_ids": "[]"}),
            ("ffl_core.staff_switch.switch_with_pin", {}),
            ("ffl_core.price_approval.approve", {"approver": "m@x.test", "pin": "1234"}),
            ("ffl_integrations.gunbroker.webhooks.order_event", {}),
            ("ffl_integrations.payroc.api.charge_pos_invoice", {}),
        ):
            with self.subTest(method=method, kwargs=kwargs):
                self._refused(method, kwargs)

    def test_allowed_shapes_still_run(self):
        from unittest.mock import patch
        calls = []

        class Client:
            def call_method(self, method, kwargs=None, **k):
                if method != generic._ACTION_CATALOG:  # verb lookup
                    calls.append(method)
                return {}

        with patch.object(generic, "get_client", return_value=Client()):
            run = self.tools["frappe_run_method"]
            run("ffl_core.agreements.request_agreement",
                {"customer": "C", "template": "T", "deliver": "email"}, confirm=True)
            run("ffl_integrations.fastbound.inventory_sync.sync_in_stock_from_boundbook",
                {"dry_run": 1}, confirm=True)
            run("ffl_integrations.fastbound.inventory_sync.sync_in_stock_from_boundbook",
                {"item_ids": ["FB1"]}, confirm=True)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
