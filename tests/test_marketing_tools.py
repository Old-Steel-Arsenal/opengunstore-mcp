"""Marketing tools (tools/marketing.py). Fully mocked, like the rest of the suite: the POS endpoints each tool
calls and with which verb, the confirm gate on every write, argument checks before any call, and that none
is on the read-only surface."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from gunstore_mcp import modes
from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import marketing

API = "ffl_core.api.marketing."


class _MCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class MarketingTools(unittest.TestCase):
    def setUp(self):
        mcp = _MCP()
        marketing.register(mcp)
        self.t = mcp.tools
        self.client = MagicMock()
        patcher = patch.object(marketing, "get_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_five_tools_and_none_on_the_read_only_surface(self):
        self.assertEqual(set(self.t), {"marketing_find", "marketing_contact", "marketing_tags", "marketing_consent",
                                       "marketing_requests"})
        self.assertFalse(set(self.t) & set(modes.CPA_TOOL_NAMES))

    def test_every_write_needs_confirm(self):
        for tool, kwargs in (("marketing_tags", {"contact": "a@b.com", "add": ["vip"]}),
                             ("marketing_consent", {"contact": "a@b.com", "action": "subscribe", "channel": "phone"}),
                             ("marketing_consent", {"contact": "a@b.com", "action": "unsubscribe"}),
                             ("marketing_requests", {"action": "close", "request": "MR-1"})):
            with self.assertRaises(WriteRefused, msg=tool):
                self.t[tool](**kwargs)
        self.assertFalse(self.client.mock_calls)

    def test_reads_are_get_calls_with_capped_limits(self):
        self.t["marketing_find"](tag="new-arrivals", status="Subscribed", limit=5000, start=-3)
        self.client.get_method.assert_called_with(API + "find_contacts", {
            "email": None, "name": None, "tag": "new-arrivals", "status": "Subscribed", "source": None,
            "limit": 200, "start": 0})
        self.t["marketing_contact"]("a@b.com", events=0)
        self.client.get_method.assert_called_with(API + "contact_card", {"contact": "a@b.com", "events": 1})
        self.t["marketing_requests"](contact="a@b.com", status="all")
        self.client.get_method.assert_called_with(API + "list_requests", {
            "status": "all", "kind": None, "contact": "a@b.com", "item": None, "limit": 50, "start": 0})
        self.assertFalse(self.client.call_method.called)

    def test_writes_go_to_the_rule_keeping_endpoints(self):
        self.t["marketing_tags"]("a@b.com", add=[" vip ", ""], remove=["old"], confirm=True)
        self.client.call_method.assert_called_with(API + "tag_contact",
                                                   {"contact": "a@b.com", "add": ["vip"], "remove": ["old"]})
        self.t["marketing_consent"]("a@b.com", "subscribe", channel="in store", note="clipboard", confirm=True)
        self.client.call_method.assert_called_with(API + "record_consent",
                                                   {"contact": "a@b.com", "channel": "in store", "note": "clipboard"})
        self.t["marketing_consent"]("a@b.com", "unsubscribe", note="asked", confirm=True)
        self.client.call_method.assert_called_with(API + "unsubscribe", {"contact": "a@b.com", "reason": "asked"})
        self.t["marketing_requests"]("close", request="MR-1", confirm=True)
        self.client.call_method.assert_called_with(API + "close_request", {"request": "MR-1"})
        self.assertFalse(self.client.update_document.called, "no raw field writes")

    def test_bad_arguments_are_refused_before_any_call(self):
        for tool, kwargs in (("marketing_tags", {"contact": "a@b.com", "confirm": True}),
                             ("marketing_tags", {"contact": "a@b.com", "add": ["x"], "remove": ["x"], "confirm": True}),
                             ("marketing_consent", {"contact": "a@b.com", "action": "subscribe", "confirm": True}),
                             ("marketing_consent", {"contact": "a@b.com", "action": "unsubscribe", "channel": "x",
                                                    "confirm": True}),
                             ("marketing_requests", {"action": "close", "confirm": True}),
                             ("marketing_requests", {"action": "list", "request": "MR-1"})):
            with self.assertRaises(ValueError, msg=f"{tool} {kwargs}"):
                self.t[tool](**kwargs)
        self.assertFalse(self.client.mock_calls)


if __name__ == "__main__":
    unittest.main()
