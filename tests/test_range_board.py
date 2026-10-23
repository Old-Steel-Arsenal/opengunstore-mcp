"""Range tools (tools/range_board.py): registered only behind GUNSTORE_MCP_RANGE, the confirm gate
on every write, the dotted methods and arguments each tool sends, the date/time checks, and that
the board's writes are gated for the bare frappe_run_method path too.

Mocked like the rest of the suite; the endpoints were checked against osa-range main (0.2.1)
when written: get_board / find_free_lanes / find_bookings answer GET only, the writes POST.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import generic, range_board

B = "osa_range.api.board."
READS = {"range_board", "range_free_lanes", "range_find_bookings"}
WRITES = {
    "range_book": ({"kind": "Block", "date": "2030-01-07", "start": "10:00", "end": "12:00",
                    "lanes": ["Bay 1"], "block_reason": "Maintenance"}, B + "create_booking"),
    "range_update": ({"booking": "RNG-1", "start": "11:00"}, B + "update_booking"),
    "range_cancel": ({"booking": "RNG-1", "reason": "Called off"}, B + "cancel_booking"),
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
    def __init__(self, rows=None):
        self.calls = []
        self.rows = rows

    def get_method(self, method, params=None):
        self.calls.append(("GET", method, params or {}))
        if method.endswith("find_bookings"):
            return self.rows if self.rows is not None else [{"name": "RNG-1", "date": "2030-01-07",
                                                             "starts_at": "2030-01-07 10:00:00", "modified": "m"}]
        return {"bookings": [{"name": "RNG-1", "modified": "m", "title": "x", "lanes": ["Bay 1"]}]}

    def call_method(self, method, kwargs=None):
        self.calls.append(("POST", method, kwargs or {}))
        return {"ok": True}

    def get_document(self, doctype, name):
        self.calls.append(("DOC", doctype, name))
        return {"name": name, "starts_at": "2030-01-07 10:00:00", "modified": "2030-01-01 09:00:00.123"}


def _tools(on: str | None):
    mcp = FakeMCP()
    env = {k: v for k, v in os.environ.items() if k != range_board.RANGE_ENV}
    if on is not None:
        env[range_board.RANGE_ENV] = on
    with patch.dict(os.environ, env, clear=True), patch.object(range_board, "_load_env", lambda: None):
        range_board.register(mcp)
    return mcp.tools


class Gate(unittest.TestCase):
    def test_off_by_default_and_on_only_when_truthy(self):
        self.assertEqual(_tools(None), {})
        self.assertEqual(_tools("0"), {})
        self.assertEqual(_tools("maybe"), {})
        self.assertEqual(set(_tools("1")), READS | set(WRITES))


class Base(unittest.TestCase):
    def setUp(self):
        self.tools = _tools("1")
        self.client = FakeClient()
        p = patch.object(range_board, "get_client", return_value=self.client)
        p.start()
        self.addCleanup(p.stop)


class Writes(Base):
    def test_every_write_refuses_without_confirm_and_never_calls_the_pos(self):
        for name, (kwargs, _m) in WRITES.items():
            with self.subTest(tool=name), self.assertRaises(WriteRefused):
                self.tools[name](**kwargs)
        self.assertEqual(self.client.calls, [])

    def test_every_write_reaches_its_method_once_confirmed(self):
        for name, (kwargs, method) in WRITES.items():
            with self.subTest(tool=name):
                self.client.calls.clear()
                self.tools[name](**kwargs, confirm=True)
                self.assertEqual([c[1] for c in self.client.calls if c[0] == "POST"], [method])

    def test_the_board_writes_are_gated_for_frappe_run_method_too(self):
        self.assertEqual(range_board.WRITE_METHODS, {m for _k, m in WRITES.values()})
        self.assertTrue(range_board.WRITE_METHODS <= generic._ALWAYS_CONFIRM_METHODS)

    def test_book_sends_the_board_payload_and_never_moves_anyone(self):
        self.tools["range_book"](kind="Reservation", date="2030-01-07", start="09:30", end="11:30",
                                 lanes=["Bay 1", "Bay 2"], customer="CUST-1", service="Private Bay",
                                 party_size=3, confirm=True)
        (_v, method, kwargs), = self.client.calls
        p = json.loads(kwargs["payload"])
        self.assertEqual(method, B + "create_booking")
        self.assertEqual((p["starts_at"], p["ends_at"], p["lanes"]),
                         ("2030-01-07 09:30:00", "2030-01-07 11:30:00", ["Bay 1", "Bay 2"]))
        self.assertEqual((p["moves"], p["keep"], p["source"], p["party_size"]), ([], [], "Phone", 3))
        self.assertEqual(len(p["client_token"]), 32)
        self.assertNotIn("custom_price", p)            # unset fields are not sent

    def test_a_block_is_booked_by_staff(self):
        self.tools["range_book"](**WRITES["range_book"][0], source="Phone", confirm=True)
        self.assertEqual(json.loads(self.client.calls[-1][2]["payload"])["source"], "Staff")

    def test_update_and_cancel_read_the_booking_first(self):
        self.tools["range_update"](booking="RNG-1", start="11:00", end="13:00", lanes=["Bay 3"], note="",
                                   confirm=True)
        doc, (_v, _m, args) = self.client.calls
        self.assertEqual(doc, ("DOC", "Range Booking", "RNG-1"))
        self.assertEqual(args, {"name": "RNG-1", "modified": "2030-01-01 09:00:00.123",
                                "starts_at": "2030-01-07 11:00:00", "ends_at": "2030-01-07 13:00:00",
                                "lanes": json.dumps(["Bay 3"]), "note": ""})
        self.client.calls.clear()
        self.tools["range_cancel"](booking="RNG-1", reason="Called off", confirm=True)
        self.assertEqual(self.client.calls[-1][2], {"name": "RNG-1", "modified": "2030-01-01 09:00:00.123",
                                                    "reason": "Called off"})

    def test_a_cancel_needs_a_reason(self):
        with self.assertRaises(ValueError):
            self.tools["range_cancel"](booking="RNG-1", reason="  ", confirm=True)


class Reads(Base):
    def test_reads_are_gets_with_the_endpoint_arguments(self):
        self.tools["range_board"](date="2030-01-07")
        self.tools["range_free_lanes"](date="2030-01-07", start="10:00", end="12:00", service="Private Bay",
                                       exclude_lanes=["Bay 1"])
        self.tools["range_find_bookings"](customer="Raj", include_cancelled=True)
        self.assertEqual(self.client.calls, [
            ("GET", B + "get_board", {"date": "2030-01-07"}),
            ("GET", B + "find_free_lanes", {"service": "Private Bay", "starts_at": "2030-01-07 10:00:00",
                                            "ends_at": "2030-01-07 12:00:00", "exclude_lanes": '["Bay 1"]'}),
            ("GET", B + "find_bookings", {"customer": "Raj", "date_from": None, "date_to": None,
                                          "include_cancelled": 1}),
        ])

    def test_the_board_drops_bookkeeping_fields(self):
        out = self.tools["range_board"]()
        self.assertEqual(out["bookings"], [{"name": "RNG-1", "lanes": ["Bay 1"]}])

    def test_find_says_when_the_pos_stopped_at_its_limit(self):
        self.assertFalse(self.tools["range_find_bookings"]()["truncated"])
        self.client.rows = [{"name": f"RNG-{i}"} for i in range(range_board.FIND_LIMIT)]
        self.assertTrue(self.tools["range_find_bookings"]()["truncated"])

    def test_dates_and_times_must_be_written_out(self):
        for call in (lambda: self.tools["range_board"](date="next friday"),
                     lambda: self.tools["range_free_lanes"](date="2030-01-07", start="9am", end="12:00"),
                     lambda: self.tools["range_find_bookings"](date_from="01/07/2030")):
            with self.subTest(), self.assertRaises(ValueError):
                call()
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
