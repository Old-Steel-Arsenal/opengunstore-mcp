"""Distributor (RSR Direct Connect / Sports South) tools — the drop-ship read
surface plus the human confirmation queue.

Every method below was checked against gunstore-pos `origin/develop` before being
wrapped, per this repo's standing rule: the MCP is a thin wrapper and its tests are
fully mocked, so a signature drift would NOT surface here — it would surface as a
runtime error against a live POS.

Split by consequence, because "queue operations" spans a wide range:

* **reads** — catalog/quote/queue/order lists. Two of them (`check_availability`,
  `precheck_fds`) reach the distributor over HTTP; they change nothing, but they are
  not free, so they say so.
* **draft orders** — `create_draft_order` makes Draft orders only. It places
  nothing, so it is NOT behind the action gate and is on the remote connector too.
* **queue actions** — these move a Distributor Order through the v1 human-confirmed
  gate. They are NOT REGISTERED unless `GUNSTORE_MCP_DISTRIBUTOR_ACTIONS` is
  explicitly truthy (default: off), and on top of that ``confirm`` is required on
  every one of them — it is not ceremony:
  `confirm_order` transitions Draft → Queued **and enqueues the placement worker**,
  i.e. it is the moment a real purchase from the distributor becomes inevitable.
  Cancelling here never reaches the house, so there is no undo after that.

Deliberately NOT exposed: direct placement, settings writes, and anything outside the
metabox's clientele.

`fulfillment_options_for_order` (gunstore-pos #258, merged) IS wrapped. Its payload is
three-valued in two places, which is the one thing a consumer here is likely to get
wrong: a null means "not determined yet", never "fine". See that tool's docstring —
an agent that reads it as falsy will report an unchecked destination as shippable,
which is the same fail-open the POS side was just fixed for.
"""
from __future__ import annotations

import os
from typing import Any

from ..config import _load_env
from ..frappe_client import get_client
from ..safety import require_confirm, require_reason

_API = "ffl_integrations.distributor.api."
_ROUTER = "ffl_integrations.distributor.router."
# Web-order routing and the fulfilment options live in the POS drop-ship extension
# (osa_dropship, POS 1.5+); the rest of the distributor surface is core.
_WEB_ROUTER = "osa_dropship.web_router."
_OPTIONS = "osa_dropship.options."
_HUB = "ffl_integrations.distributor.hub."
_RECEIVE = "ffl_integrations.distributor.receive_bridge."

# A receive books a Purchase Receipt and pushes each firearm to the A&D book; the
# gunicorn cap is 120 s, so a 30 s client timeout would report a failure while the
# server goes on booking.
_RECEIVE_TIMEOUT = 130


def _only_order(incoming: Any, do_name: str) -> Any:
    """Narrow list_incoming's {groups: [{orders: [...]}]} to one order (the POS
    endpoint has no filter). Unrecognised shapes pass through untouched."""
    if not isinstance(incoming, dict):
        return incoming
    for group in incoming.get("groups") or []:
        for order in group.get("orders") or []:
            if order.get("do_name") == do_name:
                return {"order": order, "customer_name": group.get("customer_name"),
                        "label": group.get("label")}
    return {"order": None,
            "note": f"{do_name} has nothing outstanding (not store-bound, not placed yet, "
                    "or already received in full)."}


def _check_receive_items(items: list) -> None:
    """Shape check before any network call: every row names a line and a positive
    whole quantity. Everything else (serial count, outstanding qty, $0 reason) is
    the POS's to judge."""
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list of {line_name, qty, serials?, …}.")
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError(f"items[{i}] must be an object.")
        if not str(it.get("line_name") or "").strip():
            raise ValueError(f"items[{i}] needs line_name (from distributor_incoming or "
                             "the staged invoice).")
        qty = it.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise ValueError(f"items[{i}].qty must be a whole number >= 1, got {qty!r}.")

ACTIONS_ENV = "GUNSTORE_MCP_DISTRIBUTOR_ACTIONS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def actions_enabled() -> bool:
    """Whether the 4 queue-action tools are registered at all (default: NO).

    Unlike GUNSTORE_MCP_MODE this does not refuse to start on an unrecognised value,
    and the asymmetry is deliberate: a typo'd mode could hand back a WRITABLE server,
    whereas anything not explicitly truthy here simply leaves the actions off. This is
    fail-closed by construction, so refusing to boot would cost availability and buy
    no safety.

    Read through _load_env so it is configurable in mcp/.env exactly like the mode and
    the credentials are — one place operators already know."""
    _load_env()
    return (os.environ.get(ACTIONS_ENV) or "").strip().lower() in _TRUTHY


def register(mcp: Any) -> None:
    # ------------------------------------------------------------------ reads

    @mcp.tool()
    def distributor_orders(
        status: str | None = None, distributor: str | None = None, limit: int = 50
    ) -> Any:
        """List Distributor Orders (drop-ship / ship-to-store / restock purchase
        orders to a distributor).
        status: Draft | Queued | Placed | Acknowledged | Hold | Partially Shipped |
        Complete | Cancelled | Error (empty = all). Read-only."""
        return get_client().call_method(
            _API + "list_orders",
            {"status": status, "distributor": distributor, "limit": limit},
        )

    @mcp.tool()
    def distributor_route_queue(limit: int = 50) -> Any:
        """The human confirmation queue: Draft Distributor Orders awaiting an
        operator, plus web orders PARKED before routing (unpaid, missing/expired
        buyer FFL, incomplete address) with the reason and the destination FFL's
        expiry. This is the list to read before confirming anything. Read-only."""
        return get_client().call_method(_WEB_ROUTER + "route_queue", {"limit": limit})

    @mcp.tool()
    def distributor_catalog_search(
        query: str, limit: int = 10, distributor: str | None = None
    ) -> Any:
        """Typeahead the distributor's catalog by keyword / stock # / UPC / MFG #.
        Reads the locally-synced catalog, not live stock — use
        distributor_check_availability before acting on a quantity. Read-only."""
        return get_client().call_method(
            _API + "search",
            {"query": query, "limit": limit, "distributor": distributor},
        )

    @mcp.tool()
    def distributor_quote(
        item_code: str | None = None, sku: str | None = None,
        distributor: str | None = None,
    ) -> Any:
        """Per-item economics: cost, MAP, MSRP, suggested sell price (the same
        pricing chain the storefront listing publishes), last-known catalog qty,
        restricted states, and the block flags. Pass item_code for a stocked item or
        sku for a raw catalog row. The quantity is the CACHED catalog figure — its
        freshness is not asserted here. Read-only, no HTTP to the distributor."""
        return get_client().call_method(
            _API + "quote",
            {"item_code": item_code, "sku": sku, "distributor": distributor},
        )

    @mcp.tool()
    def distributor_check_availability(
        lines: list[dict], distributor: str | None = None
    ) -> Any:
        """LIVE quantity/price re-confirm straight from the distributor for the given
        lines ([{sku, qty}, …]). Changes nothing, but unlike distributor_quote it
        does reach the distributor over HTTP — use it to second-check before confirming an order,
        not to browse."""
        return get_client().call_method(
            _API + "check_availability",
            {"lines": lines, "distributor": distributor},
        )

    @mcp.tool()
    def distributor_test_connection(distributor: str) -> Any:
        """Probe one distributor's live dependencies (read-only): the data-service
        catalog service plus the ordering API credentials (RSR Direct Connect
        accounts / Sports South orders+invoices services). distributor = the
        Distributor record name ('RSR', 'Sports South'). Returns
        {checks: [{label, ok: true|false|null, detail}]}; null = not
        configured / not applicable, not a failure."""
        return get_client().call_method(_HUB + "test_connection", {"distributor": distributor})

    @mcp.tool()
    def distributor_precheck_fds(do_name: str) -> Any:
        """Ask the distributor whether the transfer dealer named on this FDS order
        accepts drop-shipped firearms — the cheapest way to avoid an FDS Hold.
        Reaches the distributor over HTTP; changes nothing."""
        return get_client().call_method(_ROUTER + "precheck_fds", {"do_name": do_name})

    @mcp.tool()
    def distributor_fulfillment_options(woo_online_order: str) -> Any:
        """Everything needed to decide how to fill ONE web order: per line, the
        distributor candidates (cheapest first), a LOCAL stock comparison row, and a
        verdict. Read-only — it plans nothing and orders nothing.

        THREE-VALUED FIELDS. `if not x` is WRONG on both of these; null means "not
        determined yet", never "fine":

        * `verdict.fulfillable` — true / false / **null**. Null carries a `reason`:
          `unknown_destination` (the destination state is not captured yet — the
          NORMAL state while an order sits in Pending Route or before the buyer's FFL
          is on file) or `stale_feed` (the only quantity that could cover the line
          came off a feed past its refresh window).
        * `restricted_state` — true / false / **null**, same null meaning.

        Treat null as HOLD: report the line as undetermined and quote the `reason`.
        Never state that such a line can ship — a restricted item to an unresolved
        destination is exactly the case this must not wave through.

        CANDIDATE FLAGS: `not_carried` = that distributor has no catalog row for the
        item (listed rather than dropped, so it is not misread as out-of-stock);
        `blocked` = carried but unbuyable (distributor block / vendor approval);
        `stale` = quantity from an aged feed. Unbuyable rows sort to the BOTTOM, so
        the first candidate is the one MOST LIKELY to be actionable — but the row's
        own verdict and flags decide that, not its position. Ranking is not permission.

        MONEY: `unit_landed` is PER UNIT and goods-only. Freight does not exist until
        the order is placed, so `shipping` is null with `shipping_known` false. Do not
        present `unit_landed` as a landed cost, and do not multiply it by quantity and
        call the result final."""
        return get_client().call_method(
            _OPTIONS + "fulfillment_options_for_order",
            {"woo_online_order": woo_online_order},
        )

    # ------------------------------------------------------------ draft orders
    #
    # Not behind the action gate, so it is on the remote connector too: a draft
    # places nothing and cancelling one never reaches the house. The purchase point,
    # confirm_order, stays gated below.

    @mcp.tool()
    def distributor_create_draft_order(
        distributor: str, lines: list[dict], confirm: bool = False
    ) -> Any:
        """Create DRAFT restock order(s) from one distributor — the Distributor Orders
        page's "Create Draft Order(s)" button. `distributor` is the Distributor name
        (e.g. "RSR"); `lines` is [{sku, qty, item_code?, upc?}] with the distributor's
        SKU — find it with distributor_catalog_search. A line without an Item gets
        the shop's existing Item (by UPC or SKU), else one made from the catalog.

        Ships to the store (stocking). The cart may split into several drafts
        (firearms never share an order with accessories). Nothing is placed: a draft
        becomes a real purchase only when staff confirm it on the Distributor Orders
        page (or through distributor_confirm_order where that is enabled). Creates
        documents (and possibly Items), so it asks for confirm=true.

        NOT idempotent: calling it again makes a second set of drafts. If a call
        fails or times out, check distributor_orders (status Draft) before retrying.

        Returns [{name, order_type, account, line_count}], one per draft."""
        require_confirm(f"create draft order(s) from {distributor}", confirm)
        return get_client().call_method(
            _API + "create_orders",
            {"distributor": distributor, "lines": lines,
             "ship_context": {"mode": "stocking"}},
        )

    # ----------------------------------------------------------- receiving
    #
    # Not behind the action gate either: receiving books goods that physically
    # arrived and places nothing with the distributor.

    @mcp.tool()
    def distributor_incoming(item_code: str | None = None,
                             do_name: str | None = None) -> Any:
        """Distributor parcels on their way to the store and what is still
        outstanding on each, grouped by the customer order they are for (restock
        last; outside-POS store invoices are listed under restock with do_name
        blank and invoice_name set). Read-only.

        * neither argument: the full incoming list — {groups: [{label,
          customer_name, orders: [{do_name, invoice_name, status, tracking_numbers,
          outstanding, lines: [{line_name, item_code, sku, is_firearm, outstanding,
          unit_cost, serials}]}]}]}.
        * do_name: just that order's entry ({order: …}, or order null when nothing
          on it is outstanding).
        * item_code: which waiting order lines a unit of this item that just
          arrived could belong to, earliest order first ({candidates: [{do_name,
          line_name, customer_name, outstanding, unit_cost, serials, …}]}). With
          do_name as well, only that order's candidates.

        `serials` on a line is what the distributor ECHOED — a hint, never what
        gets booked. Receiving takes the serials read off the guns themselves.
        Feed line_name into distributor_receive."""
        client = get_client()
        if item_code and item_code.strip():
            result = client.call_method(_RECEIVE + "match_incoming",
                                        {"item_code": item_code.strip()})
            if do_name and isinstance(result, dict):
                result = {**result, "candidates": [
                    c for c in result.get("candidates") or []
                    if c.get("do_name") == do_name]}
            return result
        result = client.call_method(_RECEIVE + "list_incoming", {})
        return _only_order(result, do_name) if do_name else result

    @mcp.tool()
    def distributor_receive(
        do_name: str | None = None, invoice_name: str | None = None,
        items: list[dict] | None = None, warehouse: str | None = None,
        confirm: bool = False,
    ) -> Any:
        """Receive a distributor parcel against its Distributor Order (do_name) or,
        for a parcel ordered outside the POS, against its store invoice
        (invoice_name) — exactly one of the two. Books stock and the A&D
        acquisitions, marks the order line received, and hands the gun to the web
        or counter order waiting for it. Use this, NOT receive_goods, for anything
        a distributor shipped: receive_goods leaves the order open and the waiting
        order never gets its gun.

        Two steps:
        1. Without items — prepare (no confirm). do_name: that order's outstanding
           lines (same as distributor_incoming(do_name=…)). invoice_name: stages
           the invoice — finds or creates the local Item for each line and returns
           {lines: [{line_name, item_code, outstanding, unit_cost, serials, …}],
           unmatched: [{sku, reason}], received}; an invoice whose only remaining
           lines are $0 unmatched freebies is closed here (received=true).
        2. With items — book it (confirm=true). items: [{line_name, qty, serials,
           rate?, zero_cost_confirmed?, zero_cost_reason?, zero_cost_note?,
           manufacturer?, model_name?, firearm_type?, caliber?, importer?,
           sell_price?, is_antique?, year_made?, storage_location?, service_need?}].
           * serials: one per unit for a firearm, READ OFF THE GUN — not the
             distributor's echoed list. The POS refuses a count mismatch.
           * rate defaults to the invoiced cost; a $0 line must carry
             zero_cost_confirmed=1 and a zero_cost_reason.
           * qty above what is outstanding on the line is refused.
           warehouse: optional target warehouse.

        Returns the receipt ({ok, name, total}) plus do_name, or invoice_name and
        received (true once every invoice line is in).

        NOT idempotent and slow (up to ~2 minutes). After a timeout or error,
        re-read distributor_incoming before retrying — the receipt may have been
        booked."""
        if bool(do_name) == bool(invoice_name):
            raise ValueError("Pass exactly one of do_name (a Distributor Order) or "
                             "invoice_name (an outside-POS Distributor Invoice).")
        client = get_client()
        if items is None:
            if do_name:
                return _only_order(client.call_method(_RECEIVE + "list_incoming", {}),
                                   do_name)
            return client.call_method(_RECEIVE + "stage_invoice",
                                      {"invoice_name": invoice_name},
                                      timeout=_RECEIVE_TIMEOUT)
        _check_receive_items(items)
        target = do_name or invoice_name
        require_confirm(f"receive {len(items)} line(s) on {target} into stock and the "
                        "A&D book", confirm)
        if do_name:
            method, args = _RECEIVE + "receive_lines", {"do_name": do_name, "items": items}
        else:
            method, args = (_RECEIVE + "receive_invoice_lines",
                            {"invoice_name": invoice_name, "items": items})
        if warehouse:
            args["warehouse"] = warehouse
        return client.call_method(method, args, timeout=_RECEIVE_TIMEOUT)

    @mcp.tool()
    def distributor_ensure_item(sku: str, distributor: str | None = None,
                                dropship: bool = False, confirm: bool = False) -> Any:
        """Create (or find) the store Item for a distributor SKU, from any house
        (distributor = the Distributor name; empty = the primary). Idempotent: an
        existing Item (by UPC, barcode or SKU) is returned, not duplicated.

        Leave dropship=false for restock or ship-to-store items. dropship=true
        also marks the Item as drop-shipped by that house (unless another house or
        a web listing already owns it), which changes web listing and web-order
        routing. Not needed before distributor_create_draft_order, which finds or
        makes the Items for its own lines. Creates an Item, so it asks for
        confirm=true.

        Returns {item_code, item_name, is_firearm, claimed_by} — claimed_by is the
        house the Item's drop-ship mark names after the call (empty = none)."""
        sku = (sku or "").strip()
        if not sku:
            raise ValueError("sku is required (find it with distributor_catalog_search).")
        require_confirm(f"create/find the Item for {sku}"
                        + (" and mark it drop-shipped" if dropship else ""), confirm)
        return get_client().call_method(
            _API + "ensure_dropship_item",
            # claim defaults to 1 on the POS: always send it, or false would claim.
            {"sku": sku, "distributor": distributor, "claim": 1 if dropship else 0},
        )

    # --------------------------------------------------------- queue actions
    #
    # OFF unless GUNSTORE_MCP_DISTRIBUTOR_ACTIONS is explicitly truthy. They are not
    # registered at all rather than registered-and-refusing — the stance cpa mode
    # already takes (modes.py layer 1: "physically absent, not merely guarded").
    # A confirm= gate catches a misfire; it does NOT catch an agent that has reasoned
    # its way into believing it ought to confirm, and the user-level `gunstore-pos`
    # instance really does point at prod. An absent tool cannot be reasoned about.
    if not actions_enabled():
        return


    @mcp.tool()
    def distributor_confirm_order(do_name: str, confirm: bool = False) -> Any:
        """Confirm a DRAFT Distributor Order: Draft → Queued, then the placement
        worker runs.

        ⚠ This is the point of no return. It causes a REAL purchase from the
        distributor, and cancelling here never reaches the house — after placement,
        undoing it is a phone call and a return, not a status change. A
        counter-sourced DROP-SHIP order is also gated on its invoice being paid in
        full and will refuse otherwise; a ship-to-store order may be placed on a
        deposit. Read distributor_route_queue first. Requires confirm=true."""
        require_confirm(f"confirm distributor order {do_name} (places a real order)",
                        confirm)
        return get_client().call_method(_API + "confirm_order", {"do_name": do_name})

    @mcp.tool()
    def distributor_cancel_order(
        do_name: str, reason: str, distributor_confirmed: bool = False,
        confirm: bool = False,
    ) -> Any:
        """Cancel a Distributor Order here. Cancelling never reaches the house: safe
        BEFORE placement (Draft/Queued); a live order is still marked Cancelled but
        operations are alerted to arrange it with the distributor — unless
        distributor_confirmed=true records that the house has already confirmed the
        cancellation (phone / email), which skips that alert. Requires a reason and
        confirm=true."""
        reason = require_reason(f"cancel distributor order {do_name}", reason)
        require_confirm(f"cancel distributor order {do_name}", confirm)
        return get_client().call_method(
            _API + "cancel_order",
            {"do_name": do_name, "reason": reason,
             "confirmed": 1 if distributor_confirmed else 0},
        )

    @mcp.tool()
    def distributor_reroute(woo_online_order: str, confirm: bool = False) -> Any:
        """Re-run routing for a web order parked in the queue, after fixing whatever
        parked it (payment, the buyer's FFL, the shipping address). Creates DRAFT
        orders only — it never places anything — but it does create documents, so it
        asks for confirm=true. Idempotent: an order that already has live distributor
        orders gains no duplicates."""
        require_confirm(f"re-route {woo_online_order}", confirm)
        return get_client().call_method(
            _WEB_ROUTER + "reroute", {"woo_online_order": woo_online_order}
        )

    @mcp.tool()
    def distributor_update_order_ffl(
        do_name: str, ffl_license: str, confirm: bool = False
    ) -> Any:
        """Attach or replace the transfer FFL on a placed FDS order — the standard
        way to release an FDS Hold. The new licence is re-validated through the same
        canonical chain the counter uses (unexpired, complete premise address) BEFORE
        the distributor is called, so an expired licence is refused rather than
        released onto. Calls the distributor. Requires confirm=true."""
        require_confirm(f"update transfer FFL on {do_name} to {ffl_license}", confirm)
        return get_client().call_method(
            _ROUTER + "update_order_ffl",
            {"do_name": do_name, "ffl_license": ffl_license},
        )
