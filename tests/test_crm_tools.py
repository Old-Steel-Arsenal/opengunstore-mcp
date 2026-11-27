"""CRM tools (tools/crm.py). Fully mocked, like the rest of the suite: the dotted paths, the confirm gate
on every write, argument checks before any call, and what each read leaves out."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from gunstore_mcp import modes
from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import crm


class _MCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class CRMTools(unittest.TestCase):
    def setUp(self):
        mcp = _MCP()
        crm.register(mcp)
        self.t = mcp.tools
        self.client = MagicMock()
        patcher = patch.object(crm, "get_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_none_is_on_the_read_only_surface(self):
        self.assertFalse(set(self.t) & set(modes.CPA_TOOL_NAMES))

    def test_every_write_needs_confirm(self):
        for tool, kwargs in (("crm_lead_action", {"action": "convert", "name": "L"}),
                             ("crm_note_task", {"what": "note", "kind": "deal", "name": "D", "title": "T"}),
                             ("crm_open_order", {"deal": "D"}),
                             ("crm_want", {"action": "add", "deal": "D", "keywords": "P228"}),
                             ("crm_want", {"action": "close", "name": "WANT-00001"}),
                             ("crm_sell_request", {"deal": "D", "action": "offer", "amount": 500}),
                             ("crm_sell_request", {"deal": "D", "action": "accepted"}),
                             ("crm_deal_hold", {"action": "hold", "deal": "D", "serials": ["S1"]}),
                             ("crm_deal_hold", {"action": "extend", "hold": "H"}),
                             ("crm_deal_hold", {"action": "release", "hold": "H"})):
            with self.assertRaises(WriteRefused, msg=tool):
                self.t[tool](**kwargs)
        self.assertFalse(self.client.mock_calls)

    def test_find_leaves_other_mail_out_of_leads(self):
        self.t["crm_find"](kind="lead", email=" A@B.com ")
        filters = self.client.list_documents.call_args.kwargs["filters"]
        self.assertIn([crm.LEAD, "email", "=", "a@b.com"], filters)
        self.assertIn([crm.LEAD, "status", "!=", crm.OTHER_MAIL], filters)
        self.t["crm_find"](kind="lead", email="a@b.com", other_mail=True)
        self.assertEqual(self.client.list_documents.call_args.kwargs["filters"], [[crm.LEAD, "email", "=", "a@b.com"]])
        self.t["crm_find"](kind="lead", status=crm.OTHER_MAIL)
        self.assertEqual(self.client.list_documents.call_args.kwargs["filters"],
                         [[crm.LEAD, "status", "=", crm.OTHER_MAIL]])

    def test_find_waiting_deals(self):
        self.t["crm_find"](waiting_for_reply=True, owner="u@x.com")
        self.assertEqual(self.client.list_documents.call_args.kwargs["filters"],
                         [[crm.DEAL, "deal_owner", "=", "u@x.com"], [crm.DEAL, "communication_status", "=", "Open"]])

    def test_deal_drops_the_card_html(self):
        self.client.call_method.return_value = {"html": "<div>", "customer": "C"}
        self.assertEqual(self.t["crm_deal"]("D")["customer"], {"customer": "C"})

    def test_lead_actions(self):
        self.t["crm_lead_action"](action="convert", name="L", confirm=True)
        self.client.call_method.assert_called_with(crm._CONVERT, {"lead": "L"})
        self.t["crm_lead_action"](action="set_status", name="L", status="New", confirm=True)
        self.client.update_document.assert_called_with(crm.LEAD, "L", {"status": "New"})
        self.t["crm_lead_action"](action="set_owner", kind="deal", name="D", owner="u", confirm=True)
        self.client.update_document.assert_called_with(crm.DEAL, "D", {"deal_owner": "u"})

    def test_lead_action_checks_come_first(self):
        for kwargs in ({"action": "convert", "kind": "deal", "name": "D"}, {"action": "set_status", "name": "L"},
                       {"action": "set_owner", "name": "L"}):
            with self.assertRaises(ValueError):
                self.t["crm_lead_action"](**kwargs)

    def test_note_and_task(self):
        self.t["crm_note_task"](what="note", kind="deal", name="D", title="T", content="C", confirm=True)
        self.client.create_document.assert_called_with(crm.NOTE, {"title": "T", "content": "C",
                                                                  "reference_doctype": crm.DEAL, "reference_docname": "D"})
        self.t["crm_note_task"](what="task", kind="lead", name="L", title="T", assigned_to="u", due_date="2026-12-01", confirm=True)
        values = self.client.create_document.call_args.args[1]
        self.assertEqual((values["assigned_to"], values["due_date"], values["status"]), ("u", "2026-12-01", "Todo"))
        with self.assertRaises(ValueError):
            self.t["crm_note_task"](what="note", kind="deal", name="D", title="T", assigned_to="u")

    def test_open_order_stops_at_the_matches(self):
        self.client.call_method.return_value = {"customer": None, "matches": []}
        self.assertEqual(self.t["crm_open_order"]("D", confirm=True), {"customer": None, "matches": []})
        self.assertEqual(self.client.call_method.call_count, 1)

    def test_open_order_saves_the_draft(self):
        self.client.call_method.side_effect = [{"customer": "C"}, {"draft": "X"}]
        self.assertEqual(self.t["crm_open_order"]("D", customer="C", confirm=True), {"draft": "X"})
        self.client.call_method.assert_called_with(crm._API + "open_deal_order", {"deal": "D"})

    def test_open_order_takes_one_or_the_other(self):
        with self.assertRaises(ValueError):
            self.t["crm_open_order"]("D", customer="C", create_customer=True)

    def test_want_list_is_a_read_that_leaves_closed_wants_out(self):
        self.t["crm_want"](deal="D", owner="a@b.c")
        doctype, = self.client.list_documents.call_args.args
        filters = self.client.list_documents.call_args.kwargs["filters"]
        self.assertEqual(doctype, "CRM Want")
        self.assertEqual(filters, [["CRM Want", "status", "!=", "Closed"], ["CRM Want", "deal", "=", "D"],
                                   ["CRM Want", "want_owner", "=", "a@b.c"]])
        self.t["crm_want"](include_closed=True)
        self.assertEqual(self.client.list_documents.call_args.kwargs["filters"], [])
        self.client.call_method.assert_not_called()
        self.client.update_document.assert_not_called()

    def test_want_add_goes_through_the_pos_endpoint(self):
        self.t["crm_want"](action="add", deal="D", keywords="Mosin 91/30", budget=600, confirm=True)
        self.client.call_method.assert_called_once_with("ffl_core.api.crm.add_want", {
            "deal": "D", "keywords": "Mosin 91/30", "item": None, "budget": 600, "notes": None})

    def test_want_add_and_close_check_their_arguments_before_any_call(self):
        for kwargs in ({"action": "add", "keywords": "P228"}, {"action": "add", "deal": "D", "keywords": "  "},
                       {"action": "close"}):
            with self.assertRaises(ValueError, msg=kwargs):
                self.t["crm_want"](confirm=True, **kwargs)
        self.assertFalse(self.client.mock_calls)

    def test_want_close_only_sets_the_status(self):
        self.t["crm_want"](action="close", name="WANT-00001", confirm=True)
        self.client.update_document.assert_called_once_with("CRM Want", "WANT-00001", {"status": "Closed"})

    def test_find_sell_requests_shows_where_each_stands(self):
        self.client.list_documents.return_value = [
            {"name": "A", "ffl_sell_offer": 0},
            {"name": "B", "ffl_sell_offer": 450.0},
            {"name": "C", "ffl_sell_offer": 450.0, "ffl_sell_accepted_on": "2026-11-01"},
            {"name": "D", "ffl_sell_offer": 450.0, "ffl_sell_accepted_on": "2026-11-01", "ffl_sell_acquisition": "ACQ"},
            {"name": "E", "ffl_sell_offer": None, "ffl_sell_receipt": "PR"}]
        rows = self.t["crm_find"](sell_requests=True, waiting_for_reply=True)
        self.assertEqual([r["sell_state"] for r in rows],
                         ["no offer", "offered", "waiting for the gun", "received", "received"])
        kwargs = self.client.list_documents.call_args.kwargs
        self.assertEqual(kwargs["filters"], [[crm.DEAL, "ffl_sell_request", "=", 1],
                                             [crm.DEAL, "communication_status", "=", "Open"]])
        self.assertTrue({"ffl_sell_offer", "ffl_sell_accepted_on", "ffl_sell_acquisition"} <= set(kwargs["fields"]))

    def test_find_leaves_sell_fields_out_of_ordinary_searches(self):
        self.client.list_documents.return_value = [{"name": "A"}]
        self.assertEqual(self.t["crm_find"](), [{"name": "A"}])
        self.assertEqual(self.client.list_documents.call_args.kwargs["fields"], crm._FIELDS[crm.DEAL])
        with self.assertRaises(ValueError):
            self.t["crm_find"](kind="lead", sell_requests=True)

    def test_sell_card_carries_the_gun_and_photos_not_the_html(self):
        self.client.call_method.return_value = {"html": "<div>", "offer": 0, "accepted_on": None, "received": False}
        self.client.get_document.return_value = {
            "ffl_sell_manufacturer": "Colt", "ffl_sell_model": "1911", "ffl_sell_serial": "S1", "ffl_sell_type": "",
            "ffl_sell_photos": '[{"url": "https://w/1.jpg", "file": "/private/files/1.jpg"}, {"url": "https://w/2.jpg"}, "x"]'}
        card = self.t["crm_sell_request"]("D")
        self.client.call_method.assert_called_once_with(crm._API + "sell_request_card", {"deal": "D"})
        self.assertEqual(card, {"deal": "D", "gun": {"manufacturer": "Colt", "model": "1911", "serial": "S1"},
                                "photos": ["/private/files/1.jpg", "https://w/2.jpg"],
                                "offer": 0, "accepted_on": None, "received": False})
        self.client.get_document.return_value = {"ffl_sell_photos": "not json"}
        self.assertEqual(self.t["crm_sell_request"]("D")["photos"], [])

    def test_sell_offer_accepted_and_intake_call_only_their_endpoint(self):
        self.client.call_method.return_value = {"html": "<div>", "offer": 500.0}
        self.assertEqual(self.t["crm_sell_request"]("D", action="offer", amount=500, confirm=True), {"offer": 500.0})
        self.client.call_method.assert_called_with(crm._API + "make_sell_offer", {"deal": "D", "amount": 500})
        self.t["crm_sell_request"]("D", action="accepted", confirm=True)
        self.client.call_method.assert_called_with(crm._API + "sell_offer_accepted", {"deal": "D"})
        self.t["crm_sell_request"]("D", action="intake")
        self.client.call_method.assert_called_with(crm._API + "sell_gun_intake", {"deal": "D"})
        # Nothing but those three POS methods: no document writes, no trade-in, no stock.
        self.assertEqual(self.client.call_method.call_count, 3)
        for write in ("create_document", "update_document", "submit_document", "get_document"):
            getattr(self.client, write).assert_not_called()

    def test_sell_request_checks_come_first(self):
        for kwargs in ({"action": "offer"}, {"action": "offer", "amount": 0}, {"action": "offer", "amount": -5},
                       {"action": "accepted", "amount": 10}):
            with self.assertRaises(ValueError, msg=kwargs):
                self.t["crm_sell_request"]("D", confirm=True, **kwargs)
        self.assertFalse(self.client.mock_calls)

    def test_deal_hold_actions(self):
        self.t["crm_deal_hold"](deal="D")
        self.client.call_method.assert_called_with(crm._API + "deal_holds", {"deal": "D"})
        self.t["crm_deal_hold"](action="hold", deal="D", serials=[" S1 ", "", "S2"], confirm=True)
        self.client.call_method.assert_called_with(crm._API + "hold_guns", {"deal": "D", "serials": ["S1", "S2"]})
        self.t["crm_deal_hold"](action="extend", hold="H", confirm=True)
        self.client.call_method.assert_called_with(crm._API + "extend_hold", {"hold": "H"})
        self.t["crm_deal_hold"](action="release", hold="H", reason="Changed mind", confirm=True)
        self.client.call_method.assert_called_with(crm._API + "release_hold", {"hold": "H", "reason": "Changed mind"})
        # Never a manager's PIN or password through this server.
        for call in self.client.call_method.call_args_list:
            self.assertFalse({"pin", "password", "approver"} & set(call.args[1]))

    def test_deal_hold_checks_come_first(self):
        for kwargs in ({"action": "list"}, {"action": "hold", "serials": ["S1"]}, {"action": "hold", "deal": "D"},
                       {"action": "hold", "deal": "D", "serials": ["  "]}, {"action": "extend"}, {"action": "release"}):
            with self.assertRaises(ValueError, msg=kwargs):
                self.t["crm_deal_hold"](confirm=True, **kwargs)
        self.assertFalse(self.client.mock_calls)
