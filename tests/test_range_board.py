"""Range tools (tools/range_board.py): registered only behind GUNSTORE_MCP_RANGE, the confirm gate
on every write, the dotted methods and arguments each tool sends, the date/time checks, and that
the board's writes are gated for the bare frappe_run_method path too.

Mocked like the rest of the suite; the endpoints were checked against osa-range main (0.2.1)
when written: get_board / find_free_lanes / find_bookings answer GET only, the writes POST.
The guest rows and checkout_preview answers below are shaped as osa-range 0.4.0 builds them
(booking.board_row / guest_row / _booking_rows, api.board._annotate_guests, checkout.preview).
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import generic, range_board

B = "osa_range.api.board."
READS = {"range_board", "range_free_lanes", "range_find_bookings", "range_guests"}
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


# osa_range booking.guest_row (GUEST_FIELDS), as get_board annotates it (waiver, consolidated_invoice).
GUEST = {"customer": "CUST-1", "customer_name": "Pat Doe", "service": "Lane hour", "discount": "MIL",
         "discount_note": "Veteran", "discount_by": "clerk@example.com", "firearm_pistol": 1,
         "firearm_rifle": 0, "firearm_shotgun": 0, "checked_in_at": "2030-01-07 09:55:00",
         "invoice": "ACC-PSINV-0001", "paid": 1, "split_ways": 0, "waiver": "signed",
         "consolidated_invoice": "ACC-SINV-0001"}
GUEST_UNPAID = {**GUEST, "customer": "CUST-2", "customer_name": "Lee Roe", "discount": None,
                "discount_note": "", "discount_by": None, "checked_in_at": None, "invoice": None,
                "paid": 0, "waiver": "missing", "consolidated_invoice": None}
# osa_range booking.board_row
BOARD_ROW = {"name": "RNG-1", "booking_type": "Reservation", "status": "Checked In", "source": "Phone",
             "customer": "CUST-1", "customer_name": "Pat Doe", "party_size": 2, "service": "Lane hour",
             "custom_title": None, "custom_price": 0.0, "block_reason": None, "note": "", "moved_note": "",
             "title": "Pat Doe ×2", "starts_at": "2030-01-07 10:00:00", "ends_at": "2030-01-07 11:00:00",
             "lanes": ["Bay 1"], "modified": "2030-01-07 09:55:00.123", "guests": [GUEST, GUEST_UNPAID]}
# osa_range checkout.preview (lines from _guest_lines)
LINE = {"customer": "CUST-2", "customer_name": "Lee Roe", "service": "Lane hour", "service_name": "Lane hour",
        "discount": None, "discount_name": "", "note": "", "qty": 1, "list_rate": 25.0, "rate": 25.0,
        "amount": 25.0, "item_code": "RANGE-LANE-HOUR", "nothing_to_pay": False, "invoice": None, "paid": 0,
        "split_ways": 0, "waiver": "missing"}
PREVIEW = {"booking": "RNG-1", "modified": "2030-01-07 09:55:00.123", "mode": "split", "payer": "CUST-2",
           "ways": 3, "guests": [LINE], "selected": ["CUST-2"],
           "tickets": [{"customer": "CUST-2", "customer_name": "Lee Roe", "guests": ["CUST-2"], "total": 25.0,
                        "ways": 3, "shares": [8.34, 8.33, 8.33]}],
           "total": 25.0, "waiver_needed": True, "waiver_missing": ["Lee Roe"], "on_ticket": [],
           "nothing_to_pay": []}


class FakeClient:
    def __init__(self, rows=None):
        self.calls = []
        self.rows = rows
        self.board = None

    def get_method(self, method, params=None):
        self.calls.append(("GET", method, params or {}))
        if method.endswith("checkout_preview"):
            return PREVIEW
        if method.endswith("get_board") and self.board is not None:
            return self.board
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
        # Every POST endpoint of osa_range 0.4.0 api/board.py, wrapped or not.
        for verb in ("set_guests", "check_in", "checkout", "set_status", "email_range_ticket"):
            with self.subTest(verb=verb):
                self.assertTrue(generic._needs_confirm(B + verb))
        for read in ("get_board", "checkout_preview", "find_bookings", "find_free_lanes"):
            with self.subTest(read=read):
                self.assertFalse(generic._needs_confirm(B + read))

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

    def test_board_shows_each_guest_and_who_paid_but_only_listed_fields(self):
        extra = {**GUEST, "date_of_birth": "1990-01-01", "id_number": "D1234567"}   # never on a guest row today
        self.client.board = {"date": "2030-01-07", "bookings": [{**BOARD_ROW, "guests": [extra, GUEST_UNPAID]}]}
        (b,) = self.tools["range_board"](date="2030-01-07")["bookings"]
        self.assertNotIn("modified", b)
        self.assertNotIn("title", b)
        self.assertEqual(b["status"], "Checked In")
        self.assertEqual(b["guests"], [GUEST, GUEST_UNPAID])               # every guest field, nothing more
        self.assertEqual([(g["customer_name"], g["paid"], g["invoice"]) for g in b["guests"]],
                         [("Pat Doe", 1, "ACC-PSINV-0001"), ("Lee Roe", 0, None)])

    def test_find_carries_guests_without_the_board_only_fields(self):
        find_guest = {k: v for k, v in GUEST.items() if k not in ("waiver", "consolidated_invoice")}
        self.client.rows = [{**BOARD_ROW, "guests": [find_guest], "date": "2030-01-07", "cancel_reason": None,
                             "no_show_reason": None}]
        (b,) = self.tools["range_find_bookings"]()["bookings"]
        self.assertEqual(b["guests"], [find_guest])
        self.assertNotIn("modified", b)
        self.assertEqual((b["date"], b["no_show_reason"]), ("2030-01-07", None))

    def test_a_booking_without_guests_has_no_guests_key(self):
        out = self.tools["range_board"]()
        self.assertNotIn("guests", out["bookings"][0])

    def test_guests_is_the_checkout_preview_read_and_drops_bookkeeping(self):
        out = self.tools["range_guests"](booking="RNG-1", mode="split", payer="CUST-2", ways=3)
        self.assertEqual(self.client.calls, [("GET", B + "checkout_preview",
                                              {"name": "RNG-1", "mode": "split", "payer": "CUST-2", "ways": 3})])
        self.assertNotIn("modified", out)
        (line,) = out["guests"]
        self.assertNotIn("item_code", line)
        self.assertEqual({**line, "item_code": LINE["item_code"]}, LINE)
        self.assertEqual(out["tickets"], PREVIEW["tickets"])
        self.assertEqual((out["total"], out["waiver_missing"], out["selected"]), (25.0, ["Lee Roe"], ["CUST-2"]))

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
