"""Range tools: the shooting-range day board of the osa_range extension (lanes, reservations,
blocked time, custom slots).

osa_range is a paid extension on the stores we host, so these tools are registered only where
the store has it: ``GUNSTORE_MCP_RANGE=1`` (the POS deploy's ``MCP_RANGE``, default off — OSA
and CGA never see them). The switch is a convenience, not the boundary: every call is the
counter's own endpoint (``osa_range.api.board``), which checks that Range is on and that the
caller holds a counter role, and applies every booking rule (opening hours, time step, lane
capacity, service areas, cleaning time, no lane booked twice).

Reads: ``range_board``, ``range_free_lanes``, ``range_find_bookings``. Writes need
``confirm=true``: ``range_book``, ``range_update``, ``range_cancel``. ``range_book`` never moves
another booking out of the way (the board's ``moves``); a clash is refused with the POS's
message, which names the booking in the way.

Times are the store's day as ``HH:MM`` (24-hour); a day is ``YYYY-MM-DD``.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Literal

from ..config import _load_env
from ..frappe_client import get_client
from ..safety import require_confirm

RANGE_ENV = "GUNSTORE_MCP_RANGE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_B = "osa_range.api.board."
# The board's writes, also gated for the bare frappe_run_method path (generic._ALWAYS_CONFIRM_METHODS).
WRITE_METHODS = frozenset({_B + "create_booking", _B + "update_booking", _B + "cancel_booking"})
FIND_LIMIT = 200  # osa_range booking.MAX_FOUND

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_BOOKING_KEYS = ("name", "booking_type", "status", "source", "customer", "customer_name", "party_size",
                 "service", "custom_title", "custom_price", "block_reason", "note", "moved_note", "starts_at",
                 "ends_at", "lanes")


def range_enabled() -> bool:
    """Whether the Range tools are registered at all (default: NO). Same shape as the other
    registration gates: anything not explicitly truthy leaves them off."""
    _load_env()
    return (os.environ.get(RANGE_ENV) or "").strip().lower() in _TRUTHY


def _day(value: str, what: str = "date") -> str:
    if not _DAY_RE.match(value or ""):
        raise ValueError(f"Write the {what} as YYYY-MM-DD (got {value!r}).")
    return value


def _stamp(day: str, hhmm: str, what: str) -> str:
    if not _TIME_RE.match(hhmm or ""):
        raise ValueError(f"Write the {what} time as HH:MM, 24-hour (got {hhmm!r}).")
    return f"{day} {hhmm}:00"


def _booking(row: dict) -> dict:
    return {k: row.get(k) for k in _BOOKING_KEYS if k in row}


def register(mcp: Any) -> None:
    if not range_enabled():
        return

    # ===================================================== reads

    @mcp.tool()
    def range_board(date: str | None = None) -> Any:
        """One day of the range board: opening hours (minutes after midnight; closed + reason on
        a closed day), the lanes by area (name, max_people, out_of_service), the services
        (name, service_name, default_minutes, active, areas — empty areas = any area), every
        booking that day (name, booking_type Reservation|Block|Custom Slot, customer_name,
        party_size, service, block_reason, note, moved_note — a booking moved to another lane
        whose customer must be told, starts_at, ends_at, lanes), the block reasons and the
        "booked by" sources. date: YYYY-MM-DD, empty = today. Read-only."""
        out = get_client().get_method(_B + "get_board", {"date": _day(date) if date else None})
        if isinstance(out, dict) and isinstance(out.get("bookings"), list):
            out = {**out, "bookings": [_booking(b) for b in out["bookings"]]}
        return out

    @mcp.tool()
    def range_free_lanes(date: str, start: str, end: str, service: str | None = None,
                         exclude_lanes: list[str] | None = None) -> Any:
        """Lanes free from start to end that day (cleaning time included), in board order:
        [{lane, lane_name, area}]. With a service, only lanes in its areas. exclude_lanes:
        lane names not to offer. Read-only — use it before range_book."""
        day = _day(date)
        return get_client().get_method(_B + "find_free_lanes", {
            "service": service, "starts_at": _stamp(day, start, "start"), "ends_at": _stamp(day, end, "end"),
            "exclude_lanes": None if not exclude_lanes else json.dumps(exclude_lanes)})

    @mcp.tool()
    def range_find_bookings(customer: str | None = None, date_from: str | None = None,
                            date_to: str | None = None, include_cancelled: bool = False) -> Any:
        """Bookings over a stretch of days, earliest first: {bookings: [...same fields as
        range_board, plus date and (cancelled) cancel_reason], truncated}. customer: a Customer
        id or part of a customer's name; empty = everyone. date_from empty = today; date_to
        empty = 30 days after date_from (at most a year). truncated=true means the POS stopped
        at its limit — narrow the dates or the customer. Read-only."""
        rows = get_client().get_method(_B + "find_bookings", {
            "customer": customer, "date_from": _day(date_from, "first day") if date_from else None,
            "date_to": _day(date_to, "last day") if date_to else None,
            "include_cancelled": 1 if include_cancelled else 0}) or []
        return {"bookings": [{**_booking(r), "date": r.get("date"), "cancel_reason": r.get("cancel_reason")}
                             for r in rows],
                "truncated": len(rows) >= FIND_LIMIT}

    # ===================================================== writes

    @mcp.tool()
    def range_book(
        kind: Literal["Reservation", "Block", "Custom Slot"], date: str, start: str, end: str,
        lanes: list[str], customer: str | None = None, service: str | None = None,
        party_size: int | None = None, custom_title: str | None = None, custom_price: float | None = None,
        block_reason: str | None = None, note: str | None = None,
        source: Literal["Phone", "Walk-in", "Staff"] = "Phone", confirm: bool = False,
    ) -> Any:
        """Put a booking on the board. lanes: lane names from range_board / range_free_lanes
        (several = one booking holding them all). Reservation: customer (Customer id), service,
        party_size. Custom Slot (a paid booking for something not on the service list): customer,
        custom_title (name on the ticket), custom_price. Block (no customer, no charge):
        block_reason (one of the board's reasons, or a new one, which is added to the list).
        The POS refuses a time that is taken (naming the booking in the way), outside opening
        hours, off the time step, on an out-of-service lane, or for more people than the lanes
        take — nothing is moved to make room. Returns {bookings: [...], moved: [], repeat}.
        Creates a booking — confirm=true, after the staff member agreed to it."""
        day = _day(date)
        require_confirm(f"range_book {kind} {day} {start}-{end} {','.join(lanes)}", confirm)
        payload = {
            "booking_type": kind, "starts_at": _stamp(day, start, "start"), "ends_at": _stamp(day, end, "end"),
            "lanes": lanes, "customer": customer, "service": service, "party_size": party_size,
            "custom_title": custom_title, "custom_price": custom_price, "block_reason": block_reason,
            "note": note or "", "source": source, "moves": [], "keep": [], "client_token": uuid.uuid4().hex,
        }
        return get_client().call_method(_B + "create_booking", {
            "payload": json.dumps({k: v for k, v in payload.items() if v is not None})})

    @mcp.tool()
    def range_update(booking: str, start: str | None = None, end: str | None = None,
                     lanes: list[str] | None = None, note: str | None = None, party_size: int | None = None,
                     confirm: bool = False) -> Any:
        """Change a booking (RNG-…): its start/end on the same day (HH:MM), the lanes it holds,
        the note shown on the board (empty string clears it) or the number of people. Anything
        not given stays. A booking moved to other lanes is marked "tell the customer". The POS
        applies every rule and refuses a booking someone changed meanwhile (read it again).
        Changes a booking — confirm=true, after the staff member agreed to it."""
        require_confirm(f"range_update {booking}", confirm)
        client = get_client()
        doc = client.get_document("Range Booking", booking)
        day = str(doc.get("starts_at") or "")[:10]
        args = {"name": booking, "modified": doc.get("modified")}
        if start:
            args["starts_at"] = _stamp(day, start, "start")
        if end:
            args["ends_at"] = _stamp(day, end, "end")
        if lanes:
            args["lanes"] = json.dumps(lanes)
        if note is not None:
            args["note"] = note
        if party_size is not None:
            args["party_size"] = party_size
        return client.call_method(_B + "update_booking", args)

    @mcp.tool()
    def range_cancel(booking: str, reason: str, confirm: bool = False) -> Any:
        """Cancel a booking (RNG-…) with the reason (required). It leaves the board and stays in
        the bookings list. Cancelling cannot be undone from here (make a new booking instead).
        confirm=true, after the staff member agreed to it."""
        require_confirm(f"range_cancel {booking}", confirm)
        if not (reason or "").strip():
            raise ValueError("Say why the booking is cancelled.")
        client = get_client()
        doc = client.get_document("Range Booking", booking)
        return client.call_method(_B + "cancel_booking",
                                  {"name": booking, "modified": doc.get("modified"), "reason": reason})
