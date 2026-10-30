"""Compliance repairs, briefing, receiving checks, galleries, extensions (tools/compliance.py).

Fully mocked, like the rest of the suite: they pin the dotted method path and the
exact payload of every tool, the confirm gate on every write, and the argument
validation. Signatures were checked against gunstore-pos origin/develop (merge
6e1c546) when written; a drift there would not show up here.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from gunstore_mcp.config import HTTP
from gunstore_mcp.frappe_client import FrappeAPIError
from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import compliance, generic

R4 = "ffl_integrations.fastbound.pos_4473_recovery."
P4 = "ffl_integrations.fastbound.pos_4473."
FBR = "ffl_integrations.fastbound.reconcile."
PH = "ffl_core.api.photos."
EXT = "ffl_core.api.extensions."
RG = "ffl_core.api.receive_goods."

DIFFS = [{"field": "model", "label": "Model", "pos_value": "M1", "fb_value": "M-1"},
         {"field": "caliber", "label": "Caliber", "pos_value": "9mm", "fb_value": "9 MM"}]

# tool -> (kwargs that make a valid call, the dotted method it reaches once confirmed)
WRITES = {
    "resolve_4473": ({"invoice_name": "INV-1", "action": "redo", "reason": "wrong serial"},
                     R4 + "manager_redo_4473"),
    "fastbound_serial_fix": ({"serial": "SN1", "action": "match"},
                             FBR + "match_serial_to_fastbound"),
    "boundbook_record_fix": ({"doctype": "FFL Disposition", "name": "D1", "action": "clear_mark"},
                             FBR + "clear_not_in_bound_book"),
    "verify_customer_ffl": ({"customer_name": "Dealer A"},
                            "ffl_integrations.atf.ez_check_api.verify_customer_ffl"),
    "correct_firearm": ({"serial": "SN1", "action": "cost", "reason": "typo", "new_cost": 300},
                        "ffl_core.api.cost_correction.correct_serial_cost"),
    "edit_photo_gallery": ({"doctype": "Serial No", "name": "SN1", "action": "remove",
                            "row_name": "r1"}, PH + "remove_gallery_image"),
    "manage_extension": ({"action": "install", "extension": "osa_x"}, EXT + "install_on_site"),
}
READS = {"refresh_4473_status", "fastbound_serial_diff", "ffl_directory_search",
         "firearm_costs", "store_overview", "receive_goods_preview", "photo_gallery",
         "extensions_status"}

# Every write method a compliance tool can reach. Each must also be gated for the bare
# frappe_run_method path (exact name or the verb regex) — install_upload instead sits on
# the never-via-MCP list the integration adds.
GATED_METHODS = [
    P4 + "attest_paper_4473", R4 + "manager_redo_4473", R4 + "manager_abandon_4473",
    FBR + "match_serial_to_fastbound", FBR + "apply_serial_fb_resolution",
    "ffl_integrations.fastbound.tasks.push_serial_to_fastbound",
    FBR + "retry_disposition_push", FBR + "reconcile_record_to_fastbound",
    FBR + "mark_not_in_bound_book", FBR + "clear_not_in_bound_book",
    "ffl_integrations.atf.ez_check_api.verify_customer_ffl",
    "ffl_core.api.cost_correction.correct_serial_cost",
    "ffl_core.api.serial_correction.correct_serial",
    PH + "add_gallery_images", PH + "remove_gallery_image",
    EXT + "set_enabled", EXT + "install_on_site", EXT + "fetch_update",
]


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
        self.replies = {}

    def _reply(self, method, kwargs):
        r = self.replies.get(method, {"ok": True})
        return r(kwargs) if callable(r) else r

    def call_method(self, method, kwargs=None, *, timeout=None):
        self.calls.append(("call_method", method, kwargs or {}, timeout))
        return self._reply(method, kwargs or {})

    def get_method(self, method, params=None, *, timeout=None):
        self.calls.append(("get_method", method, params or {}, timeout))
        return self._reply(method, params or {})

    def get_document(self, doctype, name):
        self.calls.append(("get_document", doctype, name, None))
        return self.replies.get(("doc", doctype), {})


class Base(unittest.TestCase):
    transport = HTTP

    def setUp(self):
        mcp = FakeMCP()
        compliance.register(mcp)
        self.tools = mcp.tools
        self.client = FakeClient()
        for target, value in (("get_client", self.client), ("get_transport", self.transport)):
            p = patch.object(compliance, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.tool = lambda name: self.tools[name]

    def methods(self):
        return [c[1] for c in self.client.calls]

    def payloads(self):
        return [c[2] for c in self.client.calls]


class Surface(Base):
    def test_tool_count_pinned(self):
        self.assertEqual(len(self.tools), 15)
        self.assertEqual(set(self.tools), READS | set(WRITES))

    def test_every_write_refuses_without_confirm_and_never_calls_the_pos(self):
        for name, (kwargs, _m) in WRITES.items():
            with self.subTest(tool=name):
                with self.assertRaises(WriteRefused):
                    self.tool(name)(**kwargs)
        self.assertEqual(self.client.calls, [])

    def test_every_write_reaches_its_method_once_confirmed(self):
        for name, (kwargs, method) in WRITES.items():
            with self.subTest(tool=name):
                self.client.calls.clear()
                self.tool(name)(**kwargs, confirm=True)
                self.assertEqual(self.methods(), [method])

    @unittest.skipUnless(P4 + "attest_paper_4473" in generic._ALWAYS_CONFIRM_METHODS,
                         "integration has not added the compliance methods to generic yet")
    def test_every_write_method_is_gated_for_the_bare_run_method_path_too(self):
        for method in GATED_METHODS:
            with self.subTest(method=method):
                gated = (method in generic._ALWAYS_CONFIRM_METHODS
                         or generic._DESTRUCTIVE_METHOD.search(method.rsplit(".", 1)[-1]))
                self.assertTrue(gated, f"{method} is reachable ungated via frappe_run_method")


class FourFourSevenThree(Base):
    def test_refresh_adds_next_step_per_state(self):
        for state, needle in (("fastbound_only", "manager_override_4473"),
                              ("no_serials", "start_4473"), ("completed", "pos_ticket"),
                              ("odd_new_state", "")):
            self.client.replies[R4 + "refresh_4473_status"] = {"state": state, "ok": True}
            out = self.tool("refresh_4473_status")("INV-1")
            self.assertEqual(out["state"], state)
            self.assertIn(needle, out["next_step"])
        self.assertEqual(self.client.calls[0][1:3], (R4 + "refresh_4473_status",
                                                     {"invoice_name": "INV-1"}))

    def test_every_documented_state_has_a_next_step(self):
        self.assertEqual(set(compliance._NEXT_4473), {
            "completed", "repaired_from_ledger", "fastbound_only", "not_ready",
            "fb_unreachable", "no_serials", "no_firearm"})

    def test_redo_and_abandon_payloads(self):
        self.tool("resolve_4473")("INV-1", "redo", reason=" wrong serial ", confirm=True)
        self.tool("resolve_4473")("INV-1", "abandon", reason="walked out", confirm=True)
        self.assertEqual([c[1:] for c in self.client.calls], [
            (R4 + "manager_redo_4473", {"invoice_name": "INV-1", "reason": "wrong serial"}, None),
            (R4 + "manager_abandon_4473", {"invoice_name": "INV-1", "reason": "walked out"}, 90),
        ])

    def test_redo_needs_a_reason_before_anything(self):
        with self.assertRaises(ValueError):
            self.tool("resolve_4473")("INV-1", "redo", reason="  ", confirm=True)
        self.assertEqual(self.client.calls, [])

    def test_attest_paper_on_remote(self):
        self.tool("resolve_4473")("INV-1", "attest_paper", confirm=True)
        self.assertEqual(self.client.calls[0][1:3], (P4 + "attest_paper_4473",
                                                     {"invoice_name": "INV-1"}))

    def test_attest_paper_takes_no_reason(self):
        with self.assertRaises(ValueError):
            self.tool("resolve_4473")("INV-1", "attest_paper", reason="x", confirm=True)
        self.assertEqual(self.client.calls, [])


class AttestOnStdio(Base):
    transport = "stdio"

    def test_attest_paper_refused_on_the_shop_key(self):
        with self.assertRaises(WriteRefused) as cm:
            self.tool("resolve_4473")("INV-1", "attest_paper", confirm=True)
        self.assertIn("API user", str(cm.exception))
        self.assertEqual(self.client.calls, [])


class FastBoundIdentity(Base):
    def test_diff_one_call_per_unique_serial_and_errors_stay_per_serial(self):
        def reply(kw):
            if kw["serial"] == "BAD":
                raise FrappeAPIError(403, "PermissionError", ["no read"])
            return {"status": "ok", "serial": kw["serial"], "diffs": []}
        self.client.replies[FBR + "compute_serial_fb_diff"] = reply
        out = self.tool("fastbound_serial_diff")([" SN1 ", "SN1", "BAD", ""])
        self.assertEqual(self.payloads(), [{"serial": "SN1"}, {"serial": "BAD"}])
        self.assertEqual(out["SN1"]["status"], "ok")
        self.assertEqual(out["BAD"]["status"], "error")

    def test_diff_bounds(self):
        for bad in ([], ["  "], [f"S{i}" for i in range(26)]):
            with self.assertRaises(ValueError):
                self.tool("fastbound_serial_diff")(bad)
        self.assertEqual(self.client.calls, [])

    def test_apply_builds_resolutions_from_the_values_seen(self):
        self.tool("fastbound_serial_fix")("SN1", "apply", choices={"model": "fb", "caliber": "pos"},
                                          seen_diffs=DIFFS, confirm=True)
        self.assertEqual(self.client.calls[0][1:3], (FBR + "apply_serial_fb_resolution", {
            "serial": "SN1", "resolutions": {
                "model": {"choice": "fb", "fb_value": "M-1"},
                "caliber": {"choice": "pos", "fb_value": "9 MM"}}}))

    def test_apply_refuses_a_field_it_did_not_see_and_bad_choices(self):
        cases = [
            {"choices": {"importer": "pos"}, "seen_diffs": DIFFS},
            {"choices": {"model": "both"}, "seen_diffs": DIFFS},
            {"choices": {"model": "pos"}},
            {"seen_diffs": DIFFS},
            {"choices": {}, "seen_diffs": DIFFS},
        ]
        for kw in cases:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.tool("fastbound_serial_fix")("SN1", "apply", confirm=True, **kw)
        self.assertEqual(self.client.calls, [])

    def test_match_and_push_take_no_choices(self):
        with self.assertRaises(ValueError):
            self.tool("fastbound_serial_fix")("SN1", "push", choices={"model": "pos"}, confirm=True)
        self.tool("fastbound_serial_fix")("SN1", "push", confirm=True)
        self.assertEqual(self.client.calls[0][1:3], (
            "ffl_integrations.fastbound.tasks.push_serial_to_fastbound", {"serial": "SN1"}))

    def test_apply_needs_confirm_even_when_valid(self):
        with self.assertRaises(WriteRefused):
            self.tool("fastbound_serial_fix")("SN1", "apply", choices={"model": "fb"},
                                              seen_diffs=DIFFS)
        self.assertEqual(self.client.calls, [])


class BoundBookRecord(Base):
    def test_payloads(self):
        t = self.tool("boundbook_record_fix")
        t("FFL Disposition", "D1", "retry_push", confirm=True)
        t("FFL Acquisition", "A1", "link_existing", fastbound_ref=" 01171 ", confirm=True)
        t("FFL Acquisition", "A1", "mark_not_in_book", reason=" antique ", confirm=True)
        t("FFL Disposition", "D1", "clear_mark", confirm=True)
        self.assertEqual([c[1:] for c in self.client.calls], [
            (FBR + "retry_disposition_push", {"name": "D1"}, 60),
            (FBR + "reconcile_record_to_fastbound",
             {"doctype": "FFL Acquisition", "name": "A1", "fastbound_ref": "01171"}, None),
            (FBR + "mark_not_in_bound_book",
             {"doctype": "FFL Acquisition", "name": "A1", "reason": "antique"}, None),
            (FBR + "clear_not_in_bound_book", {"doctype": "FFL Disposition", "name": "D1"}, None),
        ])

    def test_validation(self):
        t = self.tool("boundbook_record_fix")
        bad = [
            ("FFL Acquisition", "A1", "retry_push", {}),
            ("FFL Disposition", "D1", "link_existing", {}),
            ("FFL Disposition", "D1", "mark_not_in_book", {"reason": " "}),
            ("FFL Disposition", "D1", "clear_mark", {"reason": "x"}),
            ("FFL Disposition", "D1", "retry_push", {"fastbound_ref": "1"}),
        ]
        for doctype, name, action, kw in bad:
            with self.subTest(action=action, kw=kw), self.assertRaises(ValueError):
                t(doctype, name, action, confirm=True, **kw)
        self.assertEqual(self.client.calls, [])


class LicencesAndCorrections(Base):
    def test_reads(self):
        self.tool("ffl_directory_search")("Acme", limit=5)
        self.tool("firearm_costs")("ITEM-1")
        self.assertEqual([c[1:3] for c in self.client.calls], [
            ("ffl_integrations.atf.search.search", {"query": "Acme", "limit": 5}),
            ("ffl_core.api.cost_correction.list_item_serials_for_cost", {"item_code": "ITEM-1"}),
        ])

    def test_verify_customer_payload(self):
        self.tool("verify_customer_ffl")("Dealer A", confirm=True)
        self.assertEqual(self.payloads(), [{"customer_name": "Dealer A"}])

    def test_correct_payloads(self):
        t = self.tool("correct_firearm")
        t("SN1", "cost", " typo ", new_cost=300.5, payout_was_different=True, confirm=True)
        t("SN1", "cost", "typo", new_cost=300, confirm=True)
        t("SN1", "serial_number", "misread", new_serial=" SN-1 ", confirm=True)
        self.assertEqual([c[1:3] for c in self.client.calls], [
            ("ffl_core.api.cost_correction.correct_serial_cost",
             {"serial": "SN1", "new_cost": 300.5, "reason": "typo", "payout_was_different": 1}),
            ("ffl_core.api.cost_correction.correct_serial_cost",
             {"serial": "SN1", "new_cost": 300, "reason": "typo", "payout_was_different": 0}),
            ("ffl_core.api.serial_correction.correct_serial",
             {"serial": "SN1", "new_serial": "SN-1", "reason": "misread"}),
        ])

    def test_correct_validation(self):
        t = self.tool("correct_firearm")
        bad = [
            ("cost", "r", {}),
            ("cost", "r", {"new_cost": 0}),
            ("cost", "r", {"new_cost": 10, "new_serial": "X"}),
            ("cost", " ", {"new_cost": 10}),
            ("serial_number", "r", {}),
            ("serial_number", "r", {"new_serial": "X", "new_cost": 5}),
            ("serial_number", "r", {"new_serial": "X", "payout_was_different": False}),
        ]
        for action, reason, kw in bad:
            with self.subTest(action=action, kw=kw), self.assertRaises(ValueError):
                t("SN1", action, reason, confirm=True, **kw)
        self.assertEqual(self.client.calls, [])


class Briefing(Base):
    def test_store_overview_payloads_and_cards(self):
        def count(kw):
            if kw["filters"]["card"] == "to_receive":
                raise FrappeAPIError(403, "PermissionError", ["nope"])
            return {"value": 3, "fieldtype": "Int", "route": []}
        self.client.replies["ffl_core.api.store_overview.get_overview"] = {"today": "2026-10-30"}
        self.client.replies["ffl_core.api.gun_shop_cards.get_count"] = count
        out = self.tool("store_overview")(fresh_ad=True)
        self.assertEqual(self.client.calls[0][1:3], (
            "ffl_core.api.store_overview.get_overview", {"with_records": 0, "fresh_ad": 1}))
        self.assertEqual(self.payloads()[1:], [{"filters": {"card": k}} for k in compliance._CARDS])
        self.assertEqual(out["today"], "2026-10-30")
        self.assertEqual(out["cards"]["pending_4473"]["count"], 3)
        self.assertIsNone(out["cards"]["to_receive"]["count"])
        self.assertIn("list_with", out["cards"]["web_orders"])

    def test_receive_preview_without_payload(self):
        self.client.replies[RG + "get_receive_sources"] = ["Walk-in", "Distributor"]
        self.client.replies[RG + "seller_payment_preview"] = {"pays": False, "methods": ["Cash"]}
        out = self.tool("receive_goods_preview")()
        self.assertEqual(out, {"sources": ["Walk-in", "Distributor"], "payment_methods": ["Cash"]})
        self.assertEqual(self.payloads(), [{}, {"payload": {"items": []}}])

    def test_receive_preview_with_payload(self):
        self.client.replies[RG + "get_receive_sources"] = ["Walk-in"]
        payload = {"source": "Walk-in", "items": [
            {"item_code": "GUN", "qty": 2, "rate": 100, "serials": ["A1", " A2 ", "A1"]},
            {"item_code": "GUN", "qty": 1, "rate": 1, "serials": []},
            {"item_code": "AMMO", "qty": 5, "rate": 1}]}
        out = self.tool("receive_goods_preview")(payload)
        self.assertEqual([c[1:3] for c in self.client.calls], [
            (RG + "get_receive_sources", {}),
            (RG + "seller_payment_preview", {"payload": payload}),
            (RG + "lookup_item", {"scan": "GUN"}),
            (RG + "lookup_item", {"scan": "AMMO"}),
            ("ffl_core.api.firearm_intake.lookup_serial", {"serial": "A1"}),
            ("ffl_core.api.firearm_intake.lookup_serial", {"serial": "A2"}),
        ])
        self.assertTrue(out["source_valid"])
        self.assertEqual(set(out["items"]), {"GUN", "AMMO"})

    def test_receive_preview_caps(self):
        too_many = {"items": [{"item_code": f"I{i}"} for i in range(51)]}
        with self.assertRaises(ValueError):
            self.tool("receive_goods_preview")(too_many)
        many_serials = {"items": [{"item_code": "G", "serials": [f"S{i}" for i in range(101)]}]}
        with self.assertRaises(ValueError):
            self.tool("receive_goods_preview")(many_serials)
        # only the sources read ran; nothing per line
        self.assertEqual(self.methods(), [RG + "get_receive_sources"] * 2)


class Photos(Base):
    GALLERY = {"item_code": "GUN", "image": "/files/b.jpg", "image_gallery": [
        {"name": "r1", "image": "/files/a.jpg", "is_primary": 0, "idx": 1, "sort_order": 2},
        {"name": "r2", "image": "/files/b.jpg", "is_primary": 1, "idx": 2, "sort_order": 1,
         "caption": "left"}]}

    def test_serial_gallery(self):
        self.client.replies[("doc", "Serial No")] = self.GALLERY
        out = self.tool("photo_gallery")("GUN", serial_no="SN1")
        self.assertEqual(self.client.calls, [("get_document", "Serial No", "SN1", None)])
        self.assertEqual([r["name"] for r in out["gallery"]], ["r2", "r1"])
        self.assertEqual(out["gallery"][1]["caption"], "")

    def test_item_gallery_and_serial_counts(self):
        self.client.replies[("doc", "Item")] = self.GALLERY
        out = self.tool("photo_gallery")("GUN", in_stock_only=False, search="12")
        self.assertEqual([c[1:3] for c in self.client.calls], [
            ("Item", "GUN"),
            (PH + "serials_for_item", {"item_code": "GUN", "in_stock_only": 0, "search": "12"}),
        ])
        self.assertEqual(len(out["gallery"]), 2)

    def test_edit_payloads(self):
        t = self.tool("edit_photo_gallery")
        t("Item", "GUN", "add", file_urls=["/files/a.jpg", " "])
        t("Item", "GUN", "reorder", row_names=["r2", "r1"])
        t("Item", "GUN", "set_primary", row_name="r1")
        t("Item", "GUN", "set_caption", row_name="r1")
        t("Item", "GUN", "remove", row_name="r1", confirm=True)
        base = {"doctype": "Item", "name": "GUN"}
        self.assertEqual([c[1:3] for c in self.client.calls], [
            (PH + "add_gallery_images", {**base, "file_urls": ["/files/a.jpg"]}),
            (PH + "reorder_gallery", {**base, "row_names": ["r2", "r1"]}),
            (PH + "set_primary_image", {**base, "row_name": "r1"}),
            (PH + "set_image_caption", {**base, "row_name": "r1", "caption": ""}),
            (PH + "remove_gallery_image", {**base, "row_name": "r1"}),
        ])

    def test_private_file_needs_confirm(self):
        t = self.tool("edit_photo_gallery")
        with self.assertRaises(WriteRefused):
            t("Serial No", "SN1", "add", file_urls=["/files/a.jpg", "/private/files/id.jpg"])
        self.assertEqual(self.client.calls, [])
        t("Serial No", "SN1", "add", file_urls=["/private/files/id.jpg"], confirm=True)
        self.assertEqual(self.methods(), [PH + "add_gallery_images"])

    def test_edit_validation(self):
        t = self.tool("edit_photo_gallery")
        bad = [
            ("add", {}),
            ("add", {"file_urls": [f"/files/{i}.jpg" for i in range(51)]}),
            ("add", {"file_urls": ["/files/a.jpg"], "row_name": "r1"}),
            ("reorder", {}),
            ("reorder", {"row_names": ["r1", "r1"]}),
            ("set_primary", {}),
            ("set_primary", {"caption": "x", "row_name": "r1"}),
            ("remove", {}),
        ]
        for action, kw in bad:
            with self.subTest(action=action, kw=kw), self.assertRaises(ValueError):
                t("Item", "GUN", action, confirm=True, **kw)
        self.assertEqual(self.client.calls, [])


class Extensions(Base):
    def _offer(self, added=("network:api.example.com",), ok=True, allowed=True):
        self.client.replies[EXT + "fetch_update"] = {
            "ok": ok, "errors": 0, "sha256": "abc", "app_id": "osa_x",
            "high_risk": ["network:api.example.com"],
            "upgrade": {"from": "1.0.0", "to": "1.1.0", "allowed": allowed,
                        "reason": "" if allowed else "busy", "release_notes": "notes",
                        "permissions_added": list(added), "permissions_removed": []}}

    def test_status_uses_get_only_endpoints(self):
        self.tool("extensions_status")()
        self.tool("extensions_status")("osa_x")
        self.assertEqual([c[:3] for c in self.client.calls], [
            ("get_method", EXT + "list_extensions", {}),
            ("get_method", EXT + "list_extensions", {}),
            ("get_method", EXT + "release_notes", {"extension": "osa_x"}),
            ("get_method", EXT + "disable_notice", {"extension": "osa_x"}),
            ("get_method", EXT + "uninstall_preview", {"extension": "osa_x"}),
        ])

    def test_refresh_enable_disable_install_payloads(self):
        t = self.tool("manage_extension")
        t("refresh")
        t("enable", "osa_x", confirm=True)
        t("disable", "osa_x", confirm=True)
        t("install", "osa_x", confirm=True)
        self.assertEqual([c[1:3] for c in self.client.calls], [
            (EXT + "refresh", {}),
            (EXT + "set_enabled", {"extension": "osa_x", "enabled": 1}),
            (EXT + "set_enabled", {"extension": "osa_x", "enabled": 0}),
            (EXT + "install_on_site", {"extension": "osa_x"}),
        ])

    def test_enable_disable_need_confirm(self):
        for action in ("enable", "disable", "install"):
            with self.assertRaises(WriteRefused):
                self.tool("manage_extension")(action, "osa_x")
        self.assertEqual(self.client.calls, [])

    def test_update_preview_summarizes_and_installs_nothing(self):
        self._offer()
        out = self.tool("manage_extension")("update_preview", "osa_x")
        self.assertEqual(self.client.calls, [("call_method", EXT + "fetch_update",
                                              {"extension": "osa_x"}, 120)])
        self.assertEqual(out["to"], "1.1.0")
        self.assertEqual(out["high_risk_added"], ["network:api.example.com"])
        self.assertTrue(out["ok"] and out["allowed"])

    def test_update_installs_with_the_fetched_sha_when_permissions_match(self):
        self._offer()
        self.tool("manage_extension")("update", "osa_x",
                                      accept_permissions=["network:api.example.com"], confirm=True)
        self.assertEqual([c[1:3] for c in self.client.calls], [
            (EXT + "fetch_update", {"extension": "osa_x"}),
            (EXT + "install_upload", {"sha256": "abc", "confirm_id": "osa_x"}),
        ])

    def test_update_refusals(self):
        t = self.tool("manage_extension")
        self._offer()
        with self.assertRaises(WriteRefused):      # permission not accepted
            t("update", "osa_x", accept_permissions=[], confirm=True)
        with self.assertRaises(WriteRefused):      # no confirm
            t("update", "osa_x", accept_permissions=["network:api.example.com"])
        with self.assertRaises(ValueError):        # accept_permissions missing
            t("update", "osa_x", confirm=True)
        self._offer(added=(), allowed=False)
        with self.assertRaises(WriteRefused):      # upgrade not allowed
            t("update", "osa_x", accept_permissions=[], confirm=True)
        self._offer(added=(), ok=False)
        with self.assertRaises(WriteRefused):      # package fails checks
            t("update", "osa_x", accept_permissions=[], confirm=True)
        self.assertNotIn(EXT + "install_upload", self.methods())

    def test_argument_validation(self):
        t = self.tool("manage_extension")
        with self.assertRaises(ValueError):
            t("refresh", "osa_x")
        with self.assertRaises(ValueError):
            t("install", confirm=True)
        with self.assertRaises(ValueError):
            t("enable", "osa_x", accept_permissions=[], confirm=True)
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
