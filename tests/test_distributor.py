"""Distributor (RSR Direct Connect) tool surface.

Two things these tests are actually for:

1. **the dotted method names and argument keys** — the MCP is a thin wrapper and its
   client is mocked, so a wrong method path or arg name cannot fail here the way it
   would against a live POS. Pinning the exact call payload is the only local signal.
   (Every path below was checked against gunstore-pos `origin/develop` when written.)
2. **the confirm gates** — `confirm_order` is the moment a real, uncancellable
   purchase from RSR becomes inevitable, so "it refuses without confirm" is a
   safety assertion, not a formality.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import distributor

_ACTION_TOOLS = (
    "distributor_confirm_order",
    "distributor_cancel_order",
    "distributor_reroute",
    "distributor_update_order_ffl",
)


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeClient:
    def __init__(self):
        self.calls = []

    def call_method(self, method, args=None):
        self.calls.append((method, args or {}))
        return {"ok": True}


def _register(actions: str | None) -> dict:
    """Register the surface with GUNSTORE_MCP_DISTRIBUTOR_ACTIONS set to `actions`
    (None = variable absent), isolated from the developer's real environment."""
    env = {k: v for k, v in os.environ.items() if k != distributor.ACTIONS_ENV}
    if actions is not None:
        env[distributor.ACTIONS_ENV] = actions
    mcp = FakeMCP()
    # _load_env is stubbed out so a developer's own mcp/.env cannot decide whether
    # these tests see the gate open — the test controls the environment, nothing else.
    with patch.dict(os.environ, env, clear=True), \
            patch.object(distributor, "_load_env", lambda: None):
        distributor.register(mcp)
    return mcp.tools


class ActionGate(unittest.TestCase):
    """The 4 queue actions are OFF unless explicitly switched on.

    SEMANTICS PINNED HERE: they are **not registered**, not "registered but
    refusing" — the same stance cpa mode takes (modes.py layer 1: "physically
    absent, not merely guarded"). The threat is an agent that decides it ought to
    confirm; a tool that exists and refuses still advertises the affordance and
    invites a retry, and the user-level `gunstore-pos` instance really does point at
    prod. `require_confirm` stays on top of this — the gate replaces nothing."""

    def test_the_four_actions_are_absent_by_default(self):
        tools = _register(None)
        for name in _ACTION_TOOLS:
            self.assertNotIn(name, tools, f"{name} must not exist unless opted in")

    def test_the_read_surface_is_untouched_by_the_gate(self):
        self.assertEqual(len(_register(None)), 12)

    def test_the_draft_order_tool_is_outside_the_gate(self):
        # A draft places nothing, so it is on every surface, the remote one included.
        self.assertIn("distributor_create_draft_order", _register(None))

    def test_explicit_opt_in_registers_them(self):
        tools = _register("1")
        for name in _ACTION_TOOLS:
            self.assertIn(name, tools)
        self.assertEqual(len(tools), 16)

    def test_only_explicitly_truthy_values_open_the_gate(self):
        for on in ("1", "true", "TRUE", "yes", "on", " 1 "):
            self.assertEqual(len(_register(on)), 16, f"{on!r} should enable")
        for off in ("", "  ", "0", "false", "off", "no"):
            self.assertEqual(len(_register(off)), 12, f"{off!r} must stay closed")

    def test_an_unrecognised_value_fails_closed_instead_of_refusing_to_boot(self):
        """GUNSTORE_MCP_MODE refuses to start on a typo because a typo'd 'cpa' would
        silently yield a WRITABLE server — the dangerous direction. Here the opposite
        is true: anything not explicitly truthy leaves the actions off, so this is
        fail-closed by construction and refusing to boot would cost availability for
        no safety."""
        self.assertEqual(len(_register("ture")), 12)
        self.assertEqual(len(_register("enabled")), 12)


class DistributorTools(unittest.TestCase):
    def setUp(self):
        # Actions explicitly enabled: this class exercises them. The default-off
        # behaviour is ActionGate's subject.
        self.tools = _register("1")
        self.client = FakeClient()
        p = patch.object(distributor, "get_client", return_value=self.client)
        p.start()
        self.addCleanup(p.stop)

    # ---- surface ---------------------------------------------------------

    def test_distributor_tool_count_pinned(self):
        # 8 read + 1 draft + 3 receiving + 4 queue actions = 16. TOOLS.md / CLAUDE.md / README quote the
        # TOTAL (114) — if this moves, move all of them too.
        self.assertEqual(len(self.tools), 16)

    def test_every_tool_is_namespaced(self):
        for name in self.tools:
            self.assertTrue(name.startswith("distributor_"),
                            f"{name} breaks the distributor_* namespace")

    def test_placement_and_settings_are_not_exposed(self):
        # The console surface must not grow a direct-place or settings door.
        for forbidden in ("place", "settings", "delete"):
            self.assertFalse([n for n in self.tools if forbidden in n],
                             f"unexpected {forbidden!r} tool in the distributor surface")

    # ---- reads: exact method paths + arg keys ----------------------------

    def test_orders_call(self):
        self.tools["distributor_orders"](status="Draft", limit=5)
        method, args = self.client.calls[0]
        self.assertEqual(method, "ffl_integrations.distributor.api.list_orders")
        self.assertEqual(args, {"status": "Draft", "distributor": None, "limit": 5})

    def test_route_queue_call(self):
        self.tools["distributor_route_queue"](limit=7)
        self.assertEqual(self.client.calls[0],
                         ("osa_dropship.web_router.route_queue", {"limit": 7}))

    def test_catalog_search_call(self):
        self.tools["distributor_catalog_search"](query="glock", limit=3)
        method, args = self.client.calls[0]
        self.assertEqual(method, "ffl_integrations.distributor.api.search")
        self.assertEqual(args, {"query": "glock", "limit": 3, "distributor": None})

    def test_quote_call(self):
        self.tools["distributor_quote"](item_code="ACC-1")
        method, args = self.client.calls[0]
        self.assertEqual(method, "ffl_integrations.distributor.api.quote")
        self.assertEqual(args, {"item_code": "ACC-1", "sku": None, "distributor": None})

    def test_check_availability_call(self):
        self.tools["distributor_check_availability"](lines=[{"sku": "A", "qty": 1}])
        method, args = self.client.calls[0]
        self.assertEqual(method, "ffl_integrations.distributor.api.check_availability")
        self.assertEqual(args["lines"], [{"sku": "A", "qty": 1}])

    def test_precheck_fds_call(self):
        self.tools["distributor_precheck_fds"](do_name="DO-1")
        self.assertEqual(self.client.calls[0],
                         ("ffl_integrations.distributor.router.precheck_fds",
                          {"do_name": "DO-1"}))

    def test_fulfillment_options_call(self):
        self.tools["distributor_fulfillment_options"](woo_online_order="WOO-1")
        self.assertEqual(
            self.client.calls[0],
            ("osa_dropship.options.fulfillment_options_for_order",
             {"woo_online_order": "WOO-1"}))

    def test_fulfillment_options_lives_in_the_options_module_not_api(self):
        """It is the ONLY tool served from `distributor.options`. Pinning that here
        because a plausible-looking `...api.fulfillment_options_for_order` would pass
        every other test in this file and only fail against a live POS."""
        self.tools["distributor_fulfillment_options"](woo_online_order="WOO-1")
        method, _ = self.client.calls[0]
        self.assertTrue(method.startswith("osa_dropship.options."))

    def test_reads_need_no_confirm(self):
        self.tools["distributor_orders"]()
        self.tools["distributor_route_queue"]()
        self.tools["distributor_fulfillment_options"](woo_online_order="WOO-1")
        self.assertEqual(len(self.client.calls), 3)

    # ---- queue actions: gates fire BEFORE any network call ---------------

    def test_confirm_order_refuses_without_confirm(self):
        with self.assertRaises(WriteRefused):
            self.tools["distributor_confirm_order"](do_name="DO-1")
        self.assertEqual(self.client.calls, [], "refusal must precede the call")

    def test_confirm_order_proceeds_with_confirm(self):
        self.tools["distributor_confirm_order"](do_name="DO-1", confirm=True)
        self.assertEqual(self.client.calls[0],
                         ("ffl_integrations.distributor.api.confirm_order",
                          {"do_name": "DO-1"}))

    def test_cancel_requires_a_reason_and_confirm(self):
        with self.assertRaises(Exception):
            self.tools["distributor_cancel_order"](do_name="DO-1", reason="",
                                                   confirm=True)
        with self.assertRaises(WriteRefused):
            self.tools["distributor_cancel_order"](do_name="DO-1", reason="dup order")
        self.assertEqual(self.client.calls, [])

    def test_cancel_passes_the_stripped_reason(self):
        self.tools["distributor_cancel_order"](do_name="DO-1", reason="  dup  ",
                                               confirm=True)
        method, args = self.client.calls[0]
        self.assertEqual(method, "ffl_integrations.distributor.api.cancel_order")
        self.assertEqual(args, {"do_name": "DO-1", "reason": "dup", "confirmed": 0})

    def test_cancel_passes_the_distributor_confirmation(self):
        self.tools["distributor_cancel_order"](do_name="DO-1", reason="house cancelled",
                                               distributor_confirmed=True, confirm=True)
        _method, args = self.client.calls[0]
        self.assertEqual(args["confirmed"], 1)

    def test_create_draft_order_refuses_without_confirm(self):
        with self.assertRaises(WriteRefused):
            self.tools["distributor_create_draft_order"](
                distributor="RSR", lines=[{"sku": "GLUX4350203FRMOS", "qty": 1}])
        self.assertEqual(self.client.calls, [])

    def test_create_draft_order_ships_to_the_store(self):
        lines = [{"sku": "GLUX4350203FRMOS", "qty": 1, "upc": "764503072864"}]
        self.tools["distributor_create_draft_order"](distributor="RSR", lines=lines,
                                                     confirm=True)
        self.assertEqual(self.client.calls[0],
                         ("ffl_integrations.distributor.api.create_orders",
                          {"distributor": "RSR", "lines": lines,
                           "ship_context": {"mode": "stocking"}}))

    def test_reroute_refuses_without_confirm(self):
        with self.assertRaises(WriteRefused):
            self.tools["distributor_reroute"](woo_online_order="WOO-1")
        self.assertEqual(self.client.calls, [])

    def test_reroute_proceeds_with_confirm(self):
        self.tools["distributor_reroute"](woo_online_order="WOO-1", confirm=True)
        self.assertEqual(self.client.calls[0],
                         ("osa_dropship.web_router.reroute",
                          {"woo_online_order": "WOO-1"}))

    def test_update_order_ffl_refuses_without_confirm(self):
        with self.assertRaises(WriteRefused):
            self.tools["distributor_update_order_ffl"](do_name="DO-1",
                                                       ffl_license="1-23")
        self.assertEqual(self.client.calls, [])

    def test_update_order_ffl_call(self):
        self.tools["distributor_update_order_ffl"](do_name="DO-1", ffl_license="1-23",
                                                   confirm=True)
        method, args = self.client.calls[0]
        self.assertEqual(method, "ffl_integrations.distributor.router.update_order_ffl")
        self.assertEqual(args, {"do_name": "DO-1", "ffl_license": "1-23"})


class CpaModeUnchanged(unittest.TestCase):
    """The CPA read-only surface is a security boundary — adding a whole new tool
    module must not widen it by accident."""

    def test_cpa_allowlist_gains_nothing(self):
        from gunstore_mcp import modes

        self.assertEqual(len(modes.CPA_TOOL_NAMES), 30)
        self.assertFalse([n for n in modes.CPA_TOOL_NAMES if n.startswith("distributor_")])


if __name__ == "__main__":
    unittest.main()
