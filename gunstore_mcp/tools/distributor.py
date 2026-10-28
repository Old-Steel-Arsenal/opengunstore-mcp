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

        Returns [{name, order_type, account, line_count}], one per draft."""
        require_confirm(f"create draft order(s) from {distributor}", confirm)
        return get_client().call_method(
            _API + "create_orders",
            {"distributor": distributor, "lines": lines,
             "ship_context": {"mode": "stocking"}},
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
