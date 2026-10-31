"""Service tools: gunsmith work orders and memberships (tools/services.py).

Fully mocked, like the rest of the suite: they pin the dotted method path and the exact
kwargs of every tool, the confirm gate on every write and the argument validation (refused
before any call). Signatures were checked against the POS source when written
(ffl_core/gunsmith.py, ffl_core/membership.py); ffl_core.gunsmith.open_deposit is a new POS
endpoint coded against its agreed signature (name, amount).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from gunstore_mcp import modes
from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import services

GS = "ffl_core.gunsmith."
MB = "ffl_core.membership."
WO = "Gunsmith Work Order"

# tool -> list of (kwargs for a valid call, the methods it calls once confirmed)
WRITES = {
    "gunsmith_intake": [({"customer": "C", "manufacturer": "Colt", "model": "1911",
                          "serial_number": "SN1", "work_requested": "Clean"},
                         [GS + "create_work_order"])],
    "gunsmith_job_action": [
        ({"work_order": "WO-1", "action": "start"}, [GS + "start_work"]),
        ({"work_order": "WO-1", "action": "ready"}, [GS + "mark_ready"]),
        ({"work_order": "WO-1", "action": "notify", "via": "Phone"}, [GS + "notify_customer"]),
        ({"work_order": "WO-1", "action": "take_payment", "amount": 50},
         [GS + "open_deposit"]),
        ({"work_order": "WO-1", "action": "pickup"}, [GS + "open_pickup"]),
        ({"work_order": "WO-1", "action": "cancel", "reason": "no parts"},
         [GS + "cancel_work_order"]),
        ({"work_order": "WO-1", "action": "log_bound_book"}, [GS + "log_in_bound_book"]),
        ({"work_order": "WO-1", "action": "refund_deposit"}, [GS + "refund_deposit"]),
    ],
    "membership_action": [
        ({"action": "start", "customer": "C", "plan": "Gold"}, [MB + "start_membership"]),
        ({"action": "invoice", "membership": "M-1"}, [MB + "invoice_membership"]),
        ({"action": "invoice_renewal", "membership": "M-1"}, [MB + "invoice_renewal"]),
        ({"action": "retry_card", "membership": "M-1"}, [MB + "retry_renewal"]),
        ({"action": "set_auto_renew", "membership": "M-1", "auto_renew": False},
         [MB + "set_auto_renew"]),
        ({"action": "change_card", "membership": "M-1", "saved_card": "SC-1"},
         [MB + "change_card"]),
        ({"action": "cancel", "membership": "M-1", "reason": "moved"},
         [MB + "cancel_membership"]),
        ({"action": "drop", "membership": "M-1", "reason": "never paid"},
         [MB + "discard_membership"]),
    ],
}


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
        self.doc = {"name": "WO-1", "customer": "C", "status": "In Progress",
                    "work_performed": "", "id_number": "D123", "id_document": "/private/x.jpg",
                    "lines": [{"item_code": "LABOR", "qty": 1, "rate": 80, "description": None}]}
        self.state = {"labor_item": "LABOR", "deposit_held": 20, "charges": 80,
                      "authorization": None,
                      "drafts": {"deposit": None, "pickup": None, "refund": None}}
        self.prices = {"PART": 12.5}

    def call_method(self, method, kwargs=None):
        self.calls.append(("call_method", method, kwargs or {}))
        if method == GS + "get_state":
            return self.state
        if method == GS + "charge_details":
            return {"item_name": "x", "rate": self.prices.get(kwargs["item_code"], 0)}
        return {"ok": True}

    def get_document(self, doctype, name):
        self.calls.append(("get_document", doctype, name))
        return dict(self.doc)

    def update_document(self, doctype, name, values):
        self.calls.append(("update_document", doctype, name, values))
        return {"name": name}


class Base(unittest.TestCase):
    def setUp(self):
        mcp = FakeMCP()
        services.register(mcp)
        self.tools = mcp.tools
        self.client = FakeClient()
        p = patch.object(services, "get_client", return_value=self.client)
        p.start()
        self.addCleanup(p.stop)

    def tool(self, name):
        return self.tools[name]


class Surface(Base):
    def test_the_five_tools(self):
        self.assertEqual(set(self.tools), {
            "gunsmith_jobs", "gunsmith_intake", "gunsmith_job_action", "memberships",
            "membership_action"})

    def test_none_is_on_the_cpa_surface(self):
        mcp = FakeMCP()
        services.register(modes.FilteredMCP(mcp, modes.CPA_TOOL_NAMES))
        self.assertEqual(mcp.tools, {})

    def test_no_wrapped_method_is_cpa_callable(self):
        for name in ("register_work_orders", "get_state", "create_work_order", "open_deposit"):
            self.assertNotIn(GS + name, modes.CPA_METHOD_ALLOWLIST)
        for name in ("get_memberships", "page_overview", "start_membership"):
            self.assertNotIn(MB + name, modes.CPA_METHOD_ALLOWLIST)


class ConfirmGate(Base):
    def test_every_write_refuses_without_confirm_and_never_calls_the_pos(self):
        for name, cases in WRITES.items():
            for kwargs, _methods in cases:
                with self.subTest(tool=name, kwargs=kwargs):
                    with self.assertRaises(WriteRefused):
                        self.tool(name)(**kwargs)
                    with self.assertRaises(WriteRefused):
                        self.tool(name)(**kwargs, confirm=False)
        self.assertEqual(self.client.calls, [])

    def test_every_write_reaches_its_method_once_confirmed(self):
        for name, cases in WRITES.items():
            for kwargs, methods in cases:
                with self.subTest(tool=name, kwargs=kwargs):
                    self.client.calls.clear()
                    self.tool(name)(**kwargs, confirm=True)
                    self.assertEqual([c[1] for c in self.client.calls], methods)

    def test_reads_need_no_confirm(self):
        self.tool("gunsmith_jobs")()
        self.tool("memberships")()
        self.assertEqual(len(self.client.calls), 2)


class GunsmithJobs(Base):
    def test_list(self):
        self.tool("gunsmith_jobs")(search="colt")
        self.tool("gunsmith_jobs")()
        self.assertEqual(self.client.calls, [
            ("call_method", GS + "register_work_orders", {"search_term": "colt"}),
            ("call_method", GS + "register_work_orders", {"search_term": None})])

    def test_detail_strips_the_id_and_merges_the_state(self):
        out = self.tool("gunsmith_jobs")(work_order="WO-1")
        self.assertEqual(self.client.calls, [
            ("get_document", WO, "WO-1"),
            ("call_method", GS + "get_state", {"name": "WO-1"})])
        for hidden in ("id_number", "id_document", "scan_id"):
            self.assertNotIn(hidden, out)
        self.assertNotIn("D123", repr(out))
        self.assertTrue(out["id_on_file"])
        self.assertEqual(out["drafts"], self.client.state["drafts"])
        self.assertEqual(out["deposit_held"], 20)
        self.assertEqual(out["customer"], "C")

    def test_search_and_work_order_together_is_refused(self):
        with self.assertRaises(ValueError):
            self.tool("gunsmith_jobs")(search="x", work_order="WO-1")
        self.assertEqual(self.client.calls, [])


class GunsmithIntake(Base):
    BASE = {"customer": " C ", "manufacturer": "Colt", "model": "1911",
            "serial_number": "SN1", "work_requested": "Clean"}

    def test_minimal_payload_never_sends_pay_now(self):
        self.tool("gunsmith_intake")(**self.BASE, confirm=True)
        self.assertEqual(self.client.calls, [("call_method", GS + "create_work_order", {
            "payload": {"customer": "C", "manufacturer": "Colt", "model": "1911",
                        "serial_number": "SN1", "work_requested": "Clean",
                        "log_in_bound_book": 0}})])

    def test_full_payload_with_bound_book(self):
        self.tool("gunsmith_intake")(
            **self.BASE, caliber=".45 ACP", firearm_type="Pistol", accessories="case",
            condition="worn", estimated_cost=0, estimated_completion="2026-11-05",
            log_in_bound_book=True, id_type="Driver License", id_number="D1", confirm=True)
        payload = self.client.calls[0][2]["payload"]
        self.assertEqual(payload, {
            "customer": "C", "manufacturer": "Colt", "model": "1911", "serial_number": "SN1",
            "work_requested": "Clean", "caliber": ".45 ACP", "firearm_type": "Pistol",
            "accessories": "case", "condition": "worn", "estimated_cost": 0,
            "estimated_completion": "2026-11-05", "id_type": "Driver License",
            "id_number": "D1", "log_in_bound_book": 1})
        self.assertNotIn("pay_now", payload)

    def test_bad_shapes_are_refused_before_any_call(self):
        f = self.tool("gunsmith_intake")
        bad = [
            {**self.BASE, "serial_number": "  "},
            {**self.BASE, "work_requested": ""},
            {**self.BASE, "log_in_bound_book": True},
            {**self.BASE, "log_in_bound_book": True, "caliber": "9mm", "firearm_type": "Pistol",
             "id_type": "Passport"},  # no id_number
            {**self.BASE, "estimated_completion": "11/05/2026"},
            {**self.BASE, "estimated_cost": -1},
        ]
        for kw in bad:
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    f(**kw, confirm=True)
                with self.assertRaises(ValueError):  # validated before the confirm gate
                    f(**kw)
        self.assertEqual(self.client.calls, [])


class GunsmithJobAction(Base):
    def act(self, **kw):
        return self.tool("gunsmith_job_action")(work_order="WO-1", confirm=True, **kw)

    def test_simple_actions_payloads(self):
        self.act(action="start")
        self.act(action="notify", via="Email")
        self.act(action="take_payment", amount=75.5)
        self.act(action="cancel", reason="  customer changed mind ")
        self.act(action="log_bound_book", id_type="State ID", id_number=" X9 ")
        self.act(action="log_bound_book")
        self.act(action="refund_deposit")
        self.act(action="pickup")
        self.act(action="ready")
        self.assertEqual([c[1:] for c in self.client.calls], [
            (GS + "start_work", {"name": "WO-1"}),
            (GS + "notify_customer", {"name": "WO-1", "via": "Email"}),
            (GS + "open_deposit", {"name": "WO-1", "amount": 75.5}),
            (GS + "cancel_work_order", {"name": "WO-1", "reason": "customer changed mind"}),
            (GS + "log_in_bound_book", {"name": "WO-1", "id_type": "State ID", "id_number": "X9"}),
            (GS + "log_in_bound_book", {"name": "WO-1", "id_type": None, "id_number": None}),
            (GS + "refund_deposit", {"name": "WO-1"}),
            (GS + "open_pickup", {"name": "WO-1"}),
            (GS + "mark_ready", {"name": "WO-1"}),
        ])

    def test_ready_saves_work_and_priced_lines_then_marks_ready(self):
        out = self.act(action="ready", work_performed=" new spring ",
                       lines=[{"item_code": "LABOR", "rate": 60},
                              {"item_code": "PART", "qty": 2, "description": "spring"}])
        lines = [{"item_code": "LABOR", "qty": 1, "rate": 60.0},
                 {"item_code": "PART", "qty": 2.0, "rate": 12.5, "description": "spring"}]
        self.assertEqual(self.client.calls, [
            ("get_document", WO, "WO-1"),
            ("call_method", GS + "charge_details", {"item_code": "PART", "customer": "C"}),
            ("call_method", GS + "get_state", {"name": "WO-1"}),
            ("update_document", WO, "WO-1", {"work_performed": "new spring", "lines": lines}),
            ("call_method", GS + "mark_ready", {"name": "WO-1"}),
        ])
        self.assertEqual(out["updated"], ["lines", "work_performed"])

    def test_ready_on_a_ready_job_only_updates(self):
        self.client.doc["status"] = "Ready"
        out = self.act(action="ready", work_performed="done")
        self.assertEqual(self.client.calls[-1],
                         ("update_document", WO, "WO-1", {"work_performed": "done"}))
        self.assertEqual(out, {"status": "Ready", "updated": ["work_performed"]})

    def test_ready_refuses_to_change_charges_under_an_open_ticket(self):
        self.client.state["drafts"]["pickup"] = "POS-INV-1"
        with self.assertRaises(ValueError):
            self.act(action="ready", lines=[{"item_code": "LABOR", "rate": 1}])
        self.assertFalse([c for c in self.client.calls if c[0] == "update_document"])

    def test_pickup_with_charges_completes_the_work(self):
        self.act(action="pickup", work_performed="cleaned", lines=[{"item_code": "PART"}])
        self.assertEqual(self.client.calls[-1], ("call_method", GS + "complete_work", {
            "name": "WO-1", "work_performed": "cleaned",
            "lines": [{"item_code": "PART", "qty": 1, "rate": 12.5}]}))

    def test_pickup_with_only_work_performed_keeps_the_current_charges(self):
        # complete_work replaces the charges, so omitted lines must not clear the bill.
        self.act(action="pickup", work_performed="cleaned")
        self.assertEqual(self.client.calls[-1][2]["lines"], [
            {"item_code": "LABOR", "qty": 1, "rate": 80, "description": None}])

    def test_pickup_with_lines_and_no_work_performed_anywhere_is_refused(self):
        with self.assertRaises(ValueError):
            self.act(action="pickup", lines=[{"item_code": "LABOR", "rate": 5}])
        self.assertFalse([c for c in self.client.calls if c[1] == GS + "complete_work"])

    def test_bad_shapes_are_refused_before_any_call(self):
        f = self.tool("gunsmith_job_action")
        bad = [
            {"action": "explode"},
            {"action": "start", "reason": "x"},                       # stray
            {"action": "cancel"},                                     # no reason
            {"action": "cancel", "reason": "   "},
            {"action": "notify"},                                     # no via
            {"action": "notify", "via": "Fax"},
            {"action": "take_payment"},                               # no amount
            {"action": "take_payment", "amount": 0},
            {"action": "take_payment", "amount": 5, "via": "Email"},  # stray
            {"action": "refund_deposit", "amount": 5},                # stray
            {"action": "log_bound_book", "lines": []},                # stray
            {"action": "ready", "lines": "LABOR"},                    # not a list
            {"action": "ready", "lines": [{"rate": 5}]},              # no item_code
            {"action": "ready", "lines": [{"item_code": "P", "qty": 0}]},
            {"action": "ready", "lines": [{"item_code": "P", "rate": -1}]},
            {"action": "ready", "lines": [{"item_code": "P", "price": 1}]},  # unknown key
        ]
        for kw in bad:
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    f(work_order="WO-1", **kw, confirm=True)
                with self.assertRaises(ValueError):  # validated before the confirm gate
                    f(work_order="WO-1", **kw)
        self.assertEqual(self.client.calls, [])


class Memberships(Base):
    def test_views(self):
        f = self.tool("memberships")
        f()
        f("attention")
        f("customer", customer=" C ")
        f("members")
        f("members", plan="Gold", status="Past Due", search="smi", start=50)
        self.assertEqual([c[1:] for c in self.client.calls], [
            (MB + "page_overview", {}),
            (MB + "needs_attention", {}),
            (MB + "get_memberships", {"customer": "C"}),
            (MB + "page_members", {"plan": None, "status": None, "search": None, "start": 0}),
            (MB + "page_members", {"plan": "Gold", "status": "Past Due", "search": "smi",
                                   "start": 50}),
        ])

    def test_bad_shapes_are_refused_before_any_call(self):
        f = self.tool("memberships")
        bad = [
            {"view": "everything"},
            {"view": "customer"},                      # no customer
            {"view": "overview", "customer": "C"},     # stray
            {"view": "customer", "customer": "C", "plan": "Gold"},
            {"view": "attention", "start": 50},
            {"view": "members", "customer": "C"},
            {"view": "members", "start": -1},
        ]
        for kw in bad:
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    f(**kw)
        self.assertEqual(self.client.calls, [])

    def test_docstring_warns_the_customer_view_writes(self):
        self.assertIn("NOT purely a read", self.tool("memberships").__doc__)


class MembershipAction(Base):
    def act(self, **kw):
        return self.tool("membership_action")(confirm=True, **kw)

    def test_payloads(self):
        self.act(action="start", customer=" C ", plan="Gold")
        self.act(action="start", customer="C", plan="Gold", auto_renew=False)
        self.act(action="invoice", membership="M-1")
        self.act(action="invoice_renewal", membership="M-1")
        self.act(action="retry_card", membership="M-1")
        self.act(action="set_auto_renew", membership="M-1", auto_renew=True)
        self.act(action="set_auto_renew", membership="M-1", auto_renew=False)
        self.act(action="change_card", membership="M-1", saved_card="SC-2")
        self.act(action="cancel", membership="M-1", reason=" moved ")
        self.act(action="cancel", membership="M-1", reason="fraud", end_now=True)
        self.act(action="drop", membership="M-1", reason="no show")
        self.assertEqual([c[1:] for c in self.client.calls], [
            (MB + "start_membership", {"customer": "C", "plan": "Gold", "auto_renew": None}),
            (MB + "start_membership", {"customer": "C", "plan": "Gold", "auto_renew": 0}),
            (MB + "invoice_membership", {"membership": "M-1"}),
            (MB + "invoice_renewal", {"membership": "M-1"}),
            (MB + "retry_renewal", {"membership": "M-1"}),
            (MB + "set_auto_renew", {"membership": "M-1", "enabled": 1}),
            (MB + "set_auto_renew", {"membership": "M-1", "enabled": 0}),
            (MB + "change_card", {"membership": "M-1", "saved_card": "SC-2"}),
            (MB + "cancel_membership", {"membership": "M-1", "reason": "moved", "end_now": 0}),
            (MB + "cancel_membership", {"membership": "M-1", "reason": "fraud", "end_now": 1}),
            (MB + "discard_membership", {"membership": "M-1", "reason": "no show"}),
        ])

    def test_bad_shapes_are_refused_before_any_call(self):
        f = self.tool("membership_action")
        bad = [
            {"action": "renew", "membership": "M-1"},
            {"action": "start", "customer": "C"},                         # no plan
            {"action": "start", "customer": "C", "plan": "G", "membership": "M-1"},
            {"action": "invoice"},                                         # no membership
            {"action": "invoice", "membership": "M-1", "reason": "x"},     # stray
            {"action": "set_auto_renew", "membership": "M-1"},             # no auto_renew
            {"action": "change_card", "membership": "M-1"},                # no card
            {"action": "cancel", "membership": "M-1"},                     # no reason
            {"action": "cancel", "membership": "M-1", "reason": "  "},
            {"action": "drop", "membership": "M-1", "reason": "x", "end_now": True},
            {"action": "retry_card", "membership": "M-1", "auto_renew": False},
        ]
        for kw in bad:
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    f(**kw, confirm=True)
                with self.assertRaises(ValueError):
                    f(**kw)
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
