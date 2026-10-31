"""Customer and agreement tools (tools/customers.py).

Fully mocked, like the rest of the suite: they pin the dotted method path and the exact
kwargs of every call, the confirm gate on every write, and the argument validation.
Signatures were checked against gunstore-pos develop (ffl_core customer_flags,
customer_profile, agreements, signing_terminal) when written; drift there would not show
up here.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import customers

CF = "ffl_core.customer_flags."
CP = "ffl_core.customer_profile."
AG = "ffl_core.agreements."

# tool -> (kwargs that make a valid call, the dotted method / REST op it ends with)
WRITES = {
    "customer_manage": ({"action": "merge", "customer": "C-DUP", "into": "C-KEEP"},
                        CP + "merge_customer"),
    "agreement_request": ({"customer": "C1", "template": "Range Waiver"}, AG + "request_agreement"),
    "agreement_manage": ({"action": "void", "agreement": "AGR-1", "reason": "wrong signer"},
                         AG + "void_agreement"),
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
        self.answers = {}   # method -> return value
        self.docs = {}      # (doctype, name) -> doc
        self.serials = []   # rows list_documents("Serial No") returns

    def call_method(self, method, kwargs=None):
        self.calls.append(("call_method", method, kwargs or {}))
        return self.answers.get(method, {"ok": True})

    def get_document(self, doctype, name):
        self.calls.append(("get_document", doctype, name))
        return self.docs.get((doctype, name), {})

    def update_document(self, doctype, name, values):
        self.calls.append(("update_document", doctype, name, values))
        return {"name": name}

    def list_documents(self, doctype, **kw):
        self.calls.append(("list_documents", doctype, kw))
        return self.serials


class Base(unittest.TestCase):
    def setUp(self):
        mcp = FakeMCP()
        customers.register(mcp)
        self.tools = mcp.tools
        self.client = FakeClient()
        p = patch.object(customers, "get_client", return_value=self.client)
        p.start()
        self.addCleanup(p.stop)

    def tool(self, name):
        return self.tools[name]

    def methods(self):
        return [c[1] for c in self.client.calls if c[0] == "call_method"]


class Surface(Base):
    def test_registered_names(self):
        self.assertEqual(set(self.tools), {
            "customer_overview", "customer_find", "customer_manage", "agreement_request",
            "agreement_manage"})

    def test_no_registration_gate(self):
        self.assertFalse([n for n in dir(customers) if n.endswith("_ENV")])


class ConfirmGate(Base):
    def test_every_write_refuses_without_confirm_and_never_calls_the_pos(self):
        for name, (kwargs, _m) in WRITES.items():
            with self.subTest(tool=name):
                with self.assertRaises(WriteRefused):
                    self.tool(name)(**kwargs)
                with self.assertRaises(WriteRefused):
                    self.tool(name)(**kwargs, confirm=False)
        self.assertEqual(self.client.calls, [])

    def test_every_customer_manage_action_refuses_without_confirm(self):
        for kwargs in ({"action": "override_ban", "reason": "ok'd"},
                       {"action": "set_status", "status": "Inactive"},
                       {"action": "add_alert", "message": "ask for ID"}):
            with self.subTest(**kwargs), self.assertRaises(WriteRefused):
                self.tool("customer_manage")(customer="C1", **kwargs)
        self.assertEqual(self.client.calls, [])

    def test_record_paper_and_tablet_refuse_without_confirm(self):
        with self.assertRaises(WriteRefused):
            self.tool("agreement_manage")("record_paper", "AGR-1", file_url="/private/files/s.pdf",
                                          signer_name="J", paper_signed_on="2026-10-01")
        with self.assertRaises(WriteRefused):
            # the tablet lookup is a POS read: it must not happen before confirm either
            self.tool("agreement_request")("C1", "Range Waiver", deliver="tablet")
        self.assertEqual(self.client.calls, [])

    def test_every_write_reaches_its_method_once_confirmed(self):
        for name, (kwargs, method) in WRITES.items():
            with self.subTest(tool=name):
                self.client.calls.clear()
                self.tool(name)(**kwargs, confirm=True)
                self.assertEqual(self.methods(), [method])


class Overview(Base):
    def test_plain_overview_reads_three_methods(self):
        out = self.tool("customer_overview")(" C1 ")
        self.assertEqual(self.client.calls, [
            ("call_method", CF + "get_customer_flags", {"customer": "C1"}),
            ("call_method", CP + "get_family", {"customer": "C1"}),
            ("call_method", AG + "get_customer_agreements", {"customer": "C1"}),
        ])
        self.assertEqual(set(out), {"flags", "family", "agreements"})

    def test_cart_fills_item_code_from_serial(self):
        self.client.serials = [{"name": "SN1", "item_code": "GLOCK19"}]
        self.tool("customer_overview")("C1", cart=[
            {"serial_no": "SN1", "rate": 500}, {"item_code": "AMMO9", "rate": 20}])
        lst = self.client.calls[3]
        self.assertEqual(lst[:2], ("list_documents", "Serial No"))
        self.assertEqual(lst[2]["filters"], [["name", "in", ["SN1"]]])
        self.assertEqual(self.client.calls[4], ("call_method", AG + "agreements_for_sale", {
            "customer": "C1", "items": [
                {"item_code": "GLOCK19", "serial_no": "SN1", "rate": 500},
                {"item_code": "AMMO9", "rate": 20}]}))

    def test_cart_with_item_codes_skips_the_lookup(self):
        self.tool("customer_overview")("C1", cart=[{"item_code": "I", "serial_no": "SN1"}])
        self.assertNotIn("list_documents", [c[0] for c in self.client.calls])

    def test_cart_unknown_serial_is_refused(self):
        self.client.serials = []
        with self.assertRaises(ValueError):
            self.tool("customer_overview")("C1", cart=[{"serial_no": "NOPE"}])
        self.assertNotIn(AG + "agreements_for_sale", self.methods())

    def test_cart_row_needs_serial_or_item(self):
        with self.assertRaises(ValueError):
            self.tool("customer_overview")("C1", cart=[{"rate": 5}])

    def test_sale_lines_become_the_cart(self):
        self.client.docs[("POS Invoice", "PINV-1")] = {"customer": "C1", "items": [
            {"item_code": "I", "serial_no": "SN1", "rate": 400, "qty": 1}]}
        out = self.tool("customer_overview")("C1", sale="PINV-1")
        self.assertEqual(self.client.calls[-1], ("call_method", AG + "agreements_for_sale", {
            "customer": "C1", "sale": "PINV-1",
            "items": [{"item_code": "I", "serial_no": "SN1", "rate": 400}]}))
        self.assertIn("sale_agreements", out)

    def test_sale_of_another_customer_is_refused(self):
        self.client.docs[("POS Invoice", "PINV-1")] = {"customer": "C2", "items": []}
        with self.assertRaises(ValueError):
            self.tool("customer_overview")("C1", sale="PINV-1")

    def test_cart_and_sale_together_refused(self):
        with self.assertRaises(ValueError):
            self.tool("customer_overview")("C1", cart=[{"item_code": "I"}], sale="PINV-1")
        self.assertEqual(self.client.calls, [])


class Find(Base):
    def test_validation(self):
        for kwargs in ({}, {"first_name": "A"}, {"last_name": "B"}, {"email": "  "}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                self.tool("customer_find")(**kwargs)
        self.assertEqual(self.client.calls, [])

    def test_contact_lookup_payload(self):
        self.client.answers[CF + "find_possible_duplicates"] = [
            {"name": "C1", "customer_name": "A", "customer_status": "Active", "matched": ["email"]}]
        out = self.tool("customer_find")(email=" a@x.test ", phone="555-0100")
        self.assertEqual(self.client.calls, [("call_method", CF + "find_possible_duplicates",
            {"email": "a@x.test", "phone": "555-0100", "id_number": None})])
        self.assertEqual(out[0]["matched"], ["email"])

    def test_name_only_uses_scan_lookup_and_marks_name(self):
        self.client.answers[CF + "find_customers_by_scan"] = [
            {"name": "C2", "customer_name": "Ann B", "customer_status": "Active",
             "address": "1 Main St", "by_id": False}]
        out = self.tool("customer_find")(first_name="Ann", last_name="B")
        self.assertEqual(self.client.calls, [
            ("call_method", CF + "find_customers_by_scan", {"first": "Ann", "last": "B"})])
        self.assertEqual(out, [{"name": "C2", "customer_name": "Ann B", "customer_status": "Active",
                                "matched": ["name"], "address": "1 Main St"}])

    def test_merges_rows_and_caps_at_ten(self):
        self.client.answers[CF + "find_possible_duplicates"] = [
            {"name": f"C{i}", "matched": ["phone"]} for i in range(5)]
        self.client.answers[CF + "find_customers_by_scan"] = [
            {"name": f"C{i}"} for i in range(3, 12)]
        out = self.tool("customer_find")(phone="5550100", first_name="A", last_name="B")
        self.assertEqual(len(out), 10)
        self.assertEqual(out[3]["matched"], ["phone", "name"])


class Manage(Base):
    def test_merge_payload(self):
        self.tool("customer_manage")("merge", "C-DUP", into="C-KEEP", confirm=True)
        self.assertEqual(self.client.calls, [
            ("call_method", CP + "merge_customer", {"source": "C-DUP", "target": "C-KEEP"})])

    def test_override_ban_payload(self):
        self.tool("customer_manage")("override_ban", "C1", reason=" paid off ", confirm=True)
        self.assertEqual(self.client.calls, [
            ("call_method", CF + "override_customer_ban", {"customer": "C1", "reason": "paid off"})])

    def test_set_status_payload(self):
        self.tool("customer_manage")("set_status", "C1", status="Banned", reason="threats",
                                     confirm=True)
        self.tool("customer_manage")("set_status", "C1", status="Active", confirm=True)
        self.assertEqual(self.client.calls, [
            ("update_document", "Customer", "C1",
             {"customer_status": "Banned", "status_reason": "threats"}),
            ("update_document", "Customer", "C1", {"customer_status": "Active"})])

    def test_saved_customer_answer_drops_dob_and_ids(self):
        self.client.update_document = lambda dt, n, v: {
            "name": n, "customer_status": "Active", "date_of_birth": "1970-01-01",
            "customer_ids": [{"id_number": "D123"}]}
        out = self.tool("customer_manage")("set_status", "C1", status="Active", confirm=True)
        self.assertEqual(out["customer_status"], "Active")
        self.assertNotIn("date_of_birth", out)
        self.assertNotIn("customer_ids", out)

    def test_add_alert_keeps_existing_rows_and_sends_modified(self):
        self.client.docs[("Customer", "C1")] = {"modified": "2026-10-01 10:00:00", "customer_alerts": [
            {"name": "a1", "message": "old", "active": 1, "created_by": "u@x.test",
             "created_on": "2026-01-01", "parent": "C1", "idx": 1}]}
        self.tool("customer_manage")("add_alert", "C1", message="check ID", confirm=True)
        self.assertEqual(self.client.calls[-1], ("update_document", "Customer", "C1", {
            "modified": "2026-10-01 10:00:00",
            "customer_alerts": [
                {"name": "a1", "message": "old", "active": 1, "created_by": "u@x.test",
                 "created_on": "2026-01-01"},
                {"message": "check ID", "active": 1}]}))

    def test_validation(self):
        bad = [
            {"action": "merge"},                                  # no into
            {"action": "merge", "into": "C1"},                    # into itself
            {"action": "merge", "into": "C2", "reason": "x"},     # stray
            {"action": "override_ban"},                           # no reason
            {"action": "override_ban", "reason": "x", "message": "m"},
            {"action": "set_status"},                             # no status
            {"action": "set_status", "status": "Banned"},         # Banned without reason
            {"action": "set_status", "status": "Gone"},
            {"action": "add_alert"},
            {"action": "add_alert", "message": "m", "status": "Active"},
            {"action": "delete", "into": "C2"},
        ]
        for kwargs in bad:
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                self.tool("customer_manage")(customer="C1", confirm=True, **kwargs)
        self.assertEqual(self.client.calls, [])


class Request(Base):
    def test_email_default_payload(self):
        self.tool("agreement_request")("C1", "Range Waiver", confirm=True)
        self.assertEqual(self.client.calls, [("call_method", AG + "request_agreement", {
            "customer": "C1", "template": "Range Waiver", "deliver": "email", "replace": 0})])

    def test_one_serial_and_price(self):
        self.tool("agreement_request")("C1", "Used Gun", serial_nos=["SN1"], prices=[350.0],
                                       deliver="none", replace=True, confirm=True)
        self.assertEqual(self.client.calls[0][2], {
            "customer": "C1", "template": "Used Gun", "deliver": "none", "replace": 1,
            "serial_no": "SN1", "price": 350.0})

    def test_several_serials_become_firearms(self):
        self.tool("agreement_request")("C1", "Used Gun", serial_nos=["SN1", "SN2"],
                                       prices=[1.0, 2.0], confirm=True)
        self.assertEqual(self.client.calls[0][2]["firearms"], [
            {"serial_no": "SN1", "price": 1.0}, {"serial_no": "SN2", "price": 2.0}])
        self.assertNotIn("serial_no", self.client.calls[0][2])
        self.client.calls.clear()
        self.tool("agreement_request")("C1", "Used Gun", serial_nos=["SN1", "SN2"], confirm=True)
        self.assertEqual(self.client.calls[0][2]["firearms"], [
            {"serial_no": "SN1", "price": None}, {"serial_no": "SN2", "price": None}])

    def test_reference_payload(self):
        self.tool("agreement_request")("C1", "Gunsmith Authorization",
                                       reference_doctype="Gunsmith Work Order",
                                       reference_name="GWO-1", confirm=True)
        kw = self.client.calls[0][2]
        self.assertEqual((kw["reference_doctype"], kw["reference_name"]),
                         ("Gunsmith Work Order", "GWO-1"))

    def test_tablet_picks_the_only_paired_one(self):
        self.client.answers["ffl_core.signing_terminal.list_terminals"] = [
            {"name": "Tab-A", "paired": True}, {"name": "Tab-B", "paired": False}]
        self.tool("agreement_request")("C1", "Range Waiver", deliver="tablet", confirm=True)
        self.assertEqual(self.methods(), [
            "ffl_core.signing_terminal.list_terminals", AG + "request_agreement"])
        self.assertEqual(self.client.calls[1][2]["terminal"], "Tab-A")

    def test_tablet_ambiguous_or_none_is_refused_with_the_list(self):
        for tablets in ([], [{"name": "Tab-A", "paired": True}, {"name": "Tab-B", "paired": True}]):
            self.client.answers["ffl_core.signing_terminal.list_terminals"] = tablets
            with self.subTest(n=len(tablets)), self.assertRaises(ValueError) as e:
                self.tool("agreement_request")("C1", "Range Waiver", deliver="tablet", confirm=True)
            if tablets:
                self.assertIn("Tab-B", str(e.exception))
        self.assertNotIn(AG + "request_agreement", self.methods())

    def test_named_tablet_skips_the_lookup(self):
        self.tool("agreement_request")("C1", "W", deliver="tablet", terminal="Tab-B", confirm=True)
        self.assertEqual(self.methods(), [AG + "request_agreement"])

    def test_link_and_qr_are_stripped_and_open_link_noted(self):
        self.client.answers[AG + "request_agreement"] = {
            "name": "AGR-1", "deliver": "qr", "sign_url": "https://x/agreement/sign#t=abc",
            "qr_svg": "data:image/svg+xml;base64,..."}
        out = self.tool("agreement_request")("C1", "W", confirm=True)
        self.assertEqual(out, {"name": "AGR-1", "deliver": "qr"})
        self.client.answers[AG + "request_agreement"] = {
            "name": "AGR-1", "link_open": True, "token_expires": "2026-11-01 10:00:00"}
        out = self.tool("agreement_request")("C1", "W", confirm=True)
        self.assertIn("replace=true", out["note"])

    def test_qr_is_never_offered(self):
        with self.assertRaises(ValueError):
            self.tool("agreement_request")("C1", "W", deliver="qr", confirm=True)
        self.assertEqual(self.client.calls, [])

    def test_validation(self):
        bad = [
            {"customer": "", "template": "W"},
            {"customer": "C1", "template": "W", "terminal": "Tab-A"},          # not tablet
            {"customer": "C1", "template": "W", "prices": [1.0]},             # no serials
            {"customer": "C1", "template": "W", "serial_nos": ["A", "B"], "prices": [1.0]},
            {"customer": "C1", "template": "W", "serial_nos": [" "]},
            {"customer": "C1", "template": "W", "reference_doctype": "Membership"},
            {"customer": "C1", "template": "W", "serial_nos": ["A"],
             "reference_doctype": "Membership", "reference_name": "M-1"},
        ]
        for kwargs in bad:
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                self.tool("agreement_request")(confirm=True, **kwargs)
        self.assertEqual(self.client.calls, [])


class Manage_Agreement(Base):
    def test_void_payload(self):
        self.tool("agreement_manage")("void", "AGR-1", reason=" sent by mistake ", confirm=True)
        self.assertEqual(self.client.calls, [
            ("call_method", AG + "void_agreement", {"name": "AGR-1", "reason": "sent by mistake"})])

    def test_record_paper_payload(self):
        self.tool("agreement_manage")(
            "record_paper", "AGR-1", file_url="/private/files/scan.pdf", signer_name="Pat Doe",
            paper_signed_on="2026-10-01", participant_is_minor=True,
            signer_relationship="Parent", confirm=True)
        self.assertEqual(self.client.calls, [("call_method", AG + "record_paper_agreement", {
            "name": "AGR-1", "file_url": "/private/files/scan.pdf", "signer_name": "Pat Doe",
            "paper_signed_on": "2026-10-01", "participant_is_minor": 1,
            "signer_relationship": "Parent"})])

    def test_validation(self):
        paper = {"file_url": "/private/files/s.pdf", "signer_name": "P",
                 "paper_signed_on": "2026-10-01"}
        bad = [
            {"action": "void"},                                          # no reason
            {"action": "void", "reason": "x", "file_url": "/f"},         # stray
            {"action": "void", "reason": "x", "participant_is_minor": True},
            {"action": "record_paper"},                                  # missing all
            {"action": "record_paper", **paper, "reason": "x"},          # stray
            {"action": "record_paper", **{**paper, "paper_signed_on": "10/01/2026"}},
            {"action": "record_paper", **paper, "participant_is_minor": True},  # minor + Self
            {"action": "record_paper", **paper, "signer_relationship": "Uncle"},
            {"action": "delete", **paper},
        ]
        for kwargs in bad:
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                self.tool("agreement_manage")(agreement="AGR-1", confirm=True, **kwargs)
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
