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
                             ("crm_want", {"action": "close", "name": "WANT-00001"})):
            with self.assertRaises(WriteRefused, msg=tool):
                self.t[tool](**kwargs)
        self.assertFalse(self.client.mock_calls)

    def test_find_leaves_other_mail_out_of_leads(self):
        self.t["crm_find"](kind="lead", email=" A@B.com ")
        filters = self.client.list_documents.call_args.kwargs["filters"]
        self.assertIn([crm.LEAD, "email", "=", "a@b.com"], filters)
        self.assertIn([crm.LEAD, "status", "!=", crm.OTHER_MAIL], filters)
        self.t["crm_find"](kind="lead", other_mail=True)
        self.assertIn([crm.LEAD, "status", "=", crm.OTHER_MAIL], self.client.list_documents.call_args.kwargs["filters"])

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
