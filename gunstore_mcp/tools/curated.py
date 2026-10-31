"""Curated convenience tools — one call for the frequent settings / sync / report
ops. Thin wrappers over the generic backbone + the apps' whitelisted methods."""
from __future__ import annotations

import datetime as _dt
import os
from typing import Any, Literal

from ..config import HTTP, _load_env, get_transport
from ..frappe_client import FrappeAPIError, get_client
from ..safety import (check_fields_writable, require_confirm, require_reason,
                      strip_passwords, stripped_note)

_SETTINGS = {
    "ffl": "FFL Settings",
    "fastbound": "FastBound Settings",
    "rsr": "RSR Settings",
    "payroc": "Payroc Settings",
    "woocommerce": "WooCommerce Settings",
    "shipstation": "ShipStation Settings",
    "gunbroker": "GunBroker Settings",
    "sports_south": "Sports South Settings",
    "lipseys": "Lipseys Settings",
    "data_service": "Data Service Settings",
}


GUNBROKER_ACTIONS_ENV = "GUNSTORE_MCP_GUNBROKER_ACTIONS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def gunbroker_actions_enabled() -> bool:
    """Whether the GunBroker WRITE tools are registered at all (default: NO).

    Which tools those are is the `if gunbroker_actions_enabled():` block in
    register() — deliberately not listed here. This docstring named two of them
    while the block held three, which is the same failure the block itself
    exists to talk about: the description of a guard drifting wider or narrower
    than the guard. A pointer cannot go stale; a list has now tried twice.

    Same shape and same reasoning as tools/distributor.py's ACTIONS_ENV: anything
    not explicitly truthy leaves them off, so this is fail-closed by construction
    and refusing to boot on a typo would cost availability and buy no safety.
    Read through _load_env so it is set in mcp/.env like the mode and the
    credentials, in the one place operators already look.

    Independent of GUNSTORE_MCP_MODE, and turning it on does NOT add anything to
    the cpa surface — the gb tools are not in CPA_TOOL_NAMES either way."""
    _load_env()
    return (os.environ.get(GUNBROKER_ACTIONS_ENV) or "").strip().lower() in _TRUTHY


# The whole answer sync_orders_now() gives. Recognised by exact key set, not by
# "does it have a queued key": the note below asserts the reply carries no counts,
# and that would become a lie the moment the server started sending some. An
# unrecognised shape is passed through untouched — no note is better than a wrong
# one, and the docstring still says what a queue receipt means.
_QUEUE_RECEIPT = frozenset({"queued"})

_QUEUED_NOTE = (
    "QUEUED, not run: the poll happens in the background, so this reply says "
    "nothing about how many orders were pulled, created or flagged — do not "
    "report orders as synced on the strength of it. The job is de-duplicated "
    "against the scheduled poll, so this joins one already running rather than "
    "starting a second. Read the outcome from the GunBroker Order list and the "
    "Error Log."
)


def _queue_receipt_note(result: Any) -> Any:
    """Add the 'this is only a receipt' note to a bare {"queued": true}."""
    if isinstance(result, dict) and set(result) == _QUEUE_RECEIPT and result["queued"]:
        return {**result, "note": _QUEUED_NOTE}
    return result


def _resolve(which: str) -> str:
    key = (which or "").strip().lower()
    if key not in _SETTINGS:
        raise ValueError(
            f"Unknown settings area '{which}'. Use one of: {', '.join(_SETTINGS)}."
        )
    return _SETTINGS[key]


def register(mcp: Any) -> None:
    @mcp.tool()
    def get_settings(which: str) -> Any:
        """Read an integration's Settings. which: ffl | fastbound | rsr | payroc |
        woocommerce | shipstation | gunbroker |
        sports_south, lipseys, data_service.
        Password fields are never returned by Frappe."""
        dt = _resolve(which)
        return get_client().get_document(dt, dt)

    @mcp.tool()
    def update_settings(which: str, values: dict) -> Any:
        """Update an integration's Settings. which: ffl | fastbound | rsr | payroc |
        woocommerce | shipstation | gunbroker |
        sports_south, lipseys, data_service. Credential/password fields are stripped — set those in Desk.
        On gunbroker the environment, credential and money fields are refused
        outright (enabled, sandbox_mode, base_url_override, dev_key,
        sandbox_dev_key, username, password, end_strategy, check_deposit_account,
        card_checkout_enabled) — the call fails rather than half-applying."""
        dt = _resolve(which)
        check_fields_writable(dt, values)
        clean, stripped = strip_passwords(dt, values)
        return stripped_note(get_client().update_document(dt, dt, clean), stripped)

    @mcp.tool()
    def fastbound_test_connection() -> Any:
        """Probe the FastBound API connection (read-only)."""
        return get_client().call_method("ffl_integrations.fastbound.client_api.test_connection")

    @mcp.tool()
    def woo_test_connection(site: Literal["retail"] = "retail") -> Any:
        """Probe a WooCommerce store's API connection (read-only).
        site: retail (the only store)."""
        return get_client().call_method(
            "ffl_woo_sync.woocommerce.client_api.test_connection", {"site": site}
        )

    @mcp.tool()
    def woo_push_item(item_code: str, site: Literal["retail"] = "retail", confirm: bool = False) -> Any:
        """Push (create or update) the WooCommerce product(s) for an Item.
        Per-serial firearms push every Active Serial No individually; non-firearm
        and uniform-price firearms push a single item-level product. For ONE gun
        use woo_push_serial instead. site: retail (the only store). Consequential
        — confirm=true."""
        require_confirm(f"woo_push_item {item_code} ({site})", confirm)
        return get_client().call_method(
            "ffl_woo_sync.woocommerce.client_api.push_item_now",
            {"item_code": item_code, "site": site},
        )

    @mcp.tool()
    def woo_delist_item(item_code: str, site: Literal["retail"] = "retail", confirm: bool = False) -> Any:
        """Set the WooCommerce product for an Item to Draft + stock 0, hiding it
        from the shop immediately. site: retail (the only store). confirm=true."""
        require_confirm(f"woo_delist_item {item_code} ({site})", confirm)
        return get_client().call_method(
            "ffl_woo_sync.woocommerce.client_api.delist_item_now",
            {"item_code": item_code, "site": site},
        )

    @mcp.tool()
    def woo_reconcile(item_code: str, site: Literal["retail"] = "retail", confirm: bool = False) -> Any:
        """Push the item-level product AND all Active Serial Nos for an Item in
        one call. Suitable for the initial listing of a per-serial firearm where
        everything needs to go live at once. site: retail (the only store). confirm=true."""
        require_confirm(f"woo_reconcile {item_code} ({site})", confirm)
        return get_client().call_method(
            "ffl_woo_sync.woocommerce.client_api.reconcile_now",
            {"item_code": item_code, "site": site},
        )

    @mcp.tool()
    def woo_push_serial(serial_no: str, site: Literal["retail"] = "retail", confirm: bool = False) -> Any:
        """Push (create or update) the WooCommerce product for ONE firearm Serial No
        — the per-gun listing action (SKU item_code::serial). Prefer this over
        woo_push_item when only specific guns changed: woo_push_item pushes EVERY
        Active serial of that item. site: retail (the only store). confirm=true."""
        require_confirm(f"woo_push_serial {serial_no} ({site})", confirm)
        return get_client().call_method(
            "ffl_woo_sync.woocommerce.client_api.push_serial_now",
            {"serial_no": serial_no, "site": site},
        )

    @mcp.tool()
    def woo_delist_serial(serial_no: str, site: Literal["retail"] = "retail", confirm: bool = False) -> Any:
        """Set ONE Serial No's WooCommerce product to Draft + stock 0, hiding that
        gun from the shop immediately. site: retail (the only store). confirm=true."""
        require_confirm(f"woo_delist_serial {serial_no} ({site})", confirm)
        return get_client().call_method(
            "ffl_woo_sync.woocommerce.client_api.delist_serial_now",
            {"serial_no": serial_no, "site": site},
        )

    @mcp.tool()
    def set_serial_title(serial_no: str, title: str) -> Any:
        """Set the per-gun WooCommerce listing title for one firearm (writes
        Serial No.item_name). A per-serial firearm's Woo product name is taken from
        Serial No.item_name, falling back to the shared Item name — so this gives one
        physical gun its own title instead of the model name shared by every serial.
        Not yet live: it takes effect on the next push (woo_push_serial / woo_push_item).
        The field is fetch_if_empty so the value persists once set."""
        # No-op for Serial No today: the rule is that EVERY tool writing a
        # document body passes through the guard, so the set of writers and the
        # set of guarded writers stay identical and a new tool cannot quietly
        # become the exception. Enforced by tests/test_never_writes_surface.py.
        values = {"item_name": title}
        check_fields_writable("Serial No", values)
        return get_client().update_document("Serial No", serial_no, values)

    # ------------------------------------------------- GunBroker channel
    #
    # These reach GunBroker only through the POS: every one calls a whitelisted
    # method in ffl_integrations, which owns the credentials, the listing guards
    # and the sandbox/production choice. Nothing here talks to GunBroker
    # directly.
    #
    # Which GunBroker gets contacted is `GunBroker Settings.sandbox_mode` on the
    # POS site this MCP points at. That field is refused on every write path
    # this server has — update_settings, frappe_update_document,
    # frappe_create_document, and frappe_run_method's field setters all raise
    # (safety.MCP_NEVER_WRITES). It is changed in Desk, by a person.
    #
    # "Every write path" is a claim with a test behind it rather than a promise:
    # tests/test_never_writes_surface.py enumerates the registered tools, drives
    # each writer with a forbidden payload, and fails if a new tool ever writes
    # a document body without the guard. The earlier version of this comment
    # said the same thing when only ONE path was covered, which is worse than
    # saying nothing — people act on it. "The registered tools" now means all
    # four modules server.py registers, pinned against server.py itself — it
    # used to mean two of them, so the same missing guard was caught here and
    # waved through in distributor.py.
    #
    # Roles differ: gb_test_connection and gb_pull_orders need SYSTEM_ROLES on
    # the POS; gb_push_serial / gb_end_listing / gb_listing_status need only
    # STOCK_ROLES. An API user with only stock roles gets a 403 from those two
    # and nothing else — worth knowing, since the probe is the one these docs
    # tell you to call first.

    @mcp.tool()
    def gb_test_connection() -> Any:
        """Probe the GunBroker API connection + credentials (read-only).
        Reports which environment answered: the "sandbox" key in the reply is
        true for api.sandbox.gunbroker.com, false for the real marketplace.
        Read that key before doing anything else — it is how you find out which
        GunBroker this POS is wired to. It cannot be changed from this server:
        every write path refuses sandbox_mode, so switching environment is a
        Desk change made by a person.
        Needs SYSTEM_ROLES on the POS — the other three gb_ tools need only
        STOCK_ROLES, so a stock-only API user sees a 403 here alone."""
        return get_client().call_method(
            "ffl_integrations.gunbroker.client_api.test_connection"
        )

    @mcp.tool()
    def gb_listing_status(serial_no: str) -> Any:
        """What this POS and GunBroker each think of ONE gun's listing
        (read-only). "state" is the POS view — unlisted | push_pending | listed |
        end_pending | sold | sold_unknown | ended — and "remote" is what
        GunBroker says right now, or null when there is no listing to ask about.
        Use this before pushing or ending anything if the two might disagree."""
        return get_client().call_method(
            "ffl_integrations.gunbroker.client_api.listing_status",
            {"serial_no": serial_no},
        )

    # ------------------------------------------------- GunBroker write actions
    #
    # OFF unless GUNSTORE_MCP_GUNBROKER_ACTIONS is explicitly truthy. Not
    # registered at all rather than registered-and-refusing — the same stance
    # tools/distributor.py:169-177 takes for the RSR queue actions, and for the
    # same reason: a confirm= gate catches a misfire, but it does not catch an
    # agent that has reasoned its way into believing it ought to confirm, and the
    # user-level `gunstore-pos` instance really does point at prod. An absent
    # tool cannot be reasoned about.
    #
    # gb_push_serial clears that bar: it puts a real firearm on a public
    # marketplace where a buyer can commit before anyone notices, and pulling it
    # back is a manual job on GunBroker's own site. gb_end_listing is here as its
    # pair — the two belong to the same operator decision, and splitting them
    # would leave someone able to end listings but not to restore one.
    #
    # gb_pull_orders is the third, and it is the one whose name argues against
    # its placement: "pull orders" sounds like a read. The test is consequence,
    # not verb — importing an order writes POS documents and reserves the gun it
    # names, so a poll run against the wrong environment, or after somebody
    # rewound GunBroker Settings.orders_since_override, reserves stock nobody
    # bought. The scheduled poll does this on its own; what the gate withholds
    # is an agent's ability to start one on a hunch.
    #
    # Scoped `if`, not distributor.py's early `return`: that file's actions are
    # the last thing it registers, ours are not — returning here would silently
    # drop every tool defined below.
    if gunbroker_actions_enabled():

        @mcp.tool()
        def gb_push_serial(serial_no: str, confirm: bool = False) -> Any:
            """List ONE firearm on GunBroker as a fixed-price Buy Now, priced from
            Serial No.sell_price. Publishes a gun for sale on a live marketplace —
            confirm=true.

            A refusal is a normal answer, not an error: guards come back as
            {"ok": false, "skipped": "<reason>", "message": "<why>"} — e.g. the gun
            is not in custody, has no price, is already listed, or is reserved by
            another channel. Read the message and fix the cause; re-calling will not
            change the answer. On success the reply carries "gb_item_id".

            To re-list a gun that somebody ended by hand, use the Serial No form —
            that lift is deliberately not available here."""
            require_confirm(f"gb_push_serial {serial_no}", confirm)
            return get_client().call_method(
                "ffl_integrations.gunbroker.client_api.push_serial_now",
                {"serial_no": serial_no},
            )

        @mcp.tool()
        def gb_end_listing(serial_no: str, confirm: bool = False,
                           reason: str | None = None) -> Any:
            """End ONE gun's GunBroker listing (e.g. it just sold at the counter).
            confirm=true. reason is optional and is recorded on the alert if the
            listing cannot be ended automatically.

            Check "confirmed" in the reply, not "ok". confirmed=true means GunBroker
            was read back and the listing is inactive. Anything else comes with
            "pending_manual": true and a "gb_url": the listing is STILL BUYABLE and a
            person has to end it on the GunBroker site — say so plainly rather than
            reporting the call as done."""
            require_confirm(f"gb_end_listing {serial_no}", confirm)
            return get_client().call_method(
                "ffl_integrations.gunbroker.client_api.end_listing_now",
                {"serial_no": serial_no, "reason": reason},
            )

        @mcp.tool()
        def gb_pull_orders(confirm: bool = False) -> Any:
            """Run the GunBroker order poll NOW instead of waiting for the
            schedule. confirm=true.

            Gated and confirm-guarded despite reading orders, because of what
            importing one does: it creates POS documents and RESERVES the guns
            the orders name. A poll started against the wrong environment, or
            after somebody rewound the watermark, reserves stock nobody sold.

            The reply is a receipt, not a result: {"queued": true} means the job
            was accepted, and it says nothing about how many orders arrived. It
            de-duplicates against the scheduled poll, so calling it while one is
            running joins that run. Look at the GunBroker Order list and the
            Error Log for what it actually did — and if you are not sure which
            marketplace this POS is wired to, gb_test_connection first.

            Takes no arguments on purpose: the poll window comes from
            GunBroker Settings, and rewinding it (orders_since_override) re-imports
            old orders — a Desk decision, not a tool call. Needs SYSTEM_ROLES on
            the POS, like gb_test_connection."""
            require_confirm("gb_pull_orders", confirm)
            return _queue_receipt_note(get_client().call_method(
                "ffl_integrations.gunbroker.client_api.sync_orders_now"
            ))

    @mcp.tool()
    def atf_verify_ffl(ffl_number: str, confirm: bool = False) -> Any:
        """Live-verify an FFL number via ATF EZ Check and upsert an ATF FFL Record.
        Creates/updates a record, so requires confirm=true."""
        require_confirm(f"atf_verify_ffl {ffl_number}", confirm)
        return get_client().call_method(
            "ffl_integrations.atf.ez_check.lookup_and_create", {"ffl_number": ffl_number}
        )

    @mcp.tool()
    def firearms_in_stock(warehouse: str | None = None, manufacturer: str | None = None) -> Any:
        """Run the 'Firearms In Stock' report — in-stock firearms by serial, with
        acquisition source + FastBound link. Optional warehouse / manufacturer filter."""
        filters: dict[str, Any] = {}
        if warehouse:
            filters["warehouse"] = warehouse
        if manufacturer:
            filters["manufacturer"] = manufacturer
        return get_client().run_report("Firearms In Stock", filters or None)

    @mcp.tool()
    def find_item(query: str, limit: int = 10) -> Any:
        """Typeahead search for Items by barcode / item code / name."""
        return get_client().call_method(
            "ffl_core.api.receive_goods.search_items", {"query": query, "limit": limit}
        )

    # ---- A. read-only queries ------------------------------------------------

    @mcp.tool()
    def item_stock(item_codes: list[str] | str) -> Any:
        """Stock on hand per item: {item_code: qty} summed across all warehouses.
        Accepts a list, a JSON list, or a comma-separated string. Read-only."""
        return get_client().call_method(
            "ffl_core.api.item_admin.get_item_stock", {"item_codes": item_codes}
        )

    @mcp.tool()
    def available_serials(
        item_codes: list[str] | str, exclude_unavailable: bool = True
    ) -> Any:
        """In-stock Serial Nos per firearm item, each with its per-gun sell price:
        {item_code: [{serial, sell_price, ...}]}. Use for 'which units do we have
        of this model + at what price'. By default mirrors the POS picker: held /
        consigned-out / consignment-warehouse guns are EXCLUDED (they're not
        sellable over the counter). Pass exclude_unavailable=false for the RAW
        list — inventory audits that must also see consigned/held guns. Read-only."""
        return get_client().call_method(
            "ffl_core.firearm.available_serials_with_prices",
            {"item_codes": item_codes,
             "exclude_unavailable": 1 if exclude_unavailable else 0},
        )

    @mcp.tool()
    def rsr_catalog_search(query: str, limit: int = 10) -> Any:
        """Search the RSR distributor CATALOG (not local stock) by keyword / RSR
        stock # / UPC / MFG #. RSR only — distributor_catalog_search spans every
        enabled house (RSR + Sports South). Use find_item for items already in
        this store. Read-only. Promote a catalog row with promote_to_item."""
        return get_client().call_method(
            "ffl_integrations.rsr.search.search", {"query": query, "limit": limit}
        )

    @mcp.tool()
    def boundbook_reconcile(
        apply: bool = False, item_ids: list[str] | None = None, confirm: bool = False
    ) -> Any:
        """Reconcile in-stock firearm Serial Nos against the FastBound bound book —
        find guns FastBound shows as disposed but still Active in our inventory.
        apply=false (default) is a READ-ONLY dry run that just reports them.
        apply=true removes them from stock and requires confirm=true. Optional
        item_ids (FastBound item ids) narrows the scan."""
        if apply:
            require_confirm("boundbook_reconcile apply", confirm)
        return get_client().call_method(
            "ffl_integrations.fastbound.inventory_sync.sync_in_stock_from_boundbook",
            {"dry_run": 0 if apply else 1, "item_ids": item_ids},
            # Pages the whole FastBound book too: a 30s client timeout would report a
            # failure while the server goes on removing stock.
            timeout=130,
        )

    @mcp.tool()
    def boundbook_mismatches() -> Any:
        """Dry-run-only bound-book reconciliation: in-stock firearm Serial Nos that
        FastBound shows as disposed but we still hold Active. Same report as
        boundbook_reconcile without apply; read-only by construction (the POS
        method has no write path) and available in cpa mode, which
        boundbook_reconcile (it can apply) is not. Guns held at a consignment
        dealer are listed separately under consignment_held and are not
        discrepancies. Takes no parameters: a dry run reports every mismatch."""
        return get_client().call_method(
            "ffl_integrations.fastbound.inventory_sync.boundbook_mismatches",
            {},
            # The server pages the whole FastBound book (gunicorn cap 120s).
            timeout=130,
        )

    # ---- B. receiving & stock adjustments (writes) ---------------------------

    @mcp.tool()
    def receive_goods(payload: dict, confirm: bool = False) -> Any:
        """Receive stock via the Receive Goods flow: creates + submits a Purchase
        Receipt; for firearms it also creates one FFL Acquisition per serial and
        pushes them to FastBound. payload mirrors the Receive Goods page:
        {supplier, source, warehouse, acquisition_source, acquisition_type,
        items:[{item_code, qty, rate, serials:[...], manufacturer, model_name,
        caliber, firearm_type, importer, sell_price, service_need}], ...}.
        A purchase PAID to a private seller (acquisition_source "Individual",
        acquisition_type blank / "Purchase" / "Individual", lines costing
        anything; gunstore-pos #705 on) must also say how the seller was paid:
        seller_payment_method "Cash" | "Zelle" | "Check" | "ACH", plus
        seller_payment_reference (check number, Zelle confirmation, ACH ref)
        unless Cash. The POS books it with the receipt (Cash leaves the drawer,
        the rest the bank) and returns seller_payment; without the method the
        receive is refused. Consignment, Gunsmithing, transfer and dealer
        receipts take no payment. Consequential — confirm=true."""
        require_confirm("receive_goods", confirm)
        return get_client().call_method(
            "ffl_core.api.receive_goods.create_receive", {"payload": payload}
        )

    @mcp.tool()
    def add_stock(
        item_code: str, qty: float, warehouse: str | None = None,
        rate: float | None = None, remarks: str | None = None, confirm: bool = False,
    ) -> Any:
        """Add stock for a NON-serialized item (Material Receipt Stock Entry). Do NOT
        use for serial-tracked firearms — those go through receive_goods so the FFL
        flow runs. confirm=true."""
        require_confirm(f"add_stock {item_code}", confirm)
        return get_client().call_method(
            "ffl_core.api.item_admin.add_stock",
            {"item_code": item_code, "qty": qty, "warehouse": warehouse,
             "rate": rate, "remarks": remarks},
        )

    @mcp.tool()
    def set_stock(
        item_code: str, new_qty: float, warehouse: str | None = None,
        reason: str | None = None, confirm: bool = False,
    ) -> Any:
        """Set a NON-serialized item's on-hand qty to an absolute value via Stock
        Reconciliation (physical count / write-off). reason is recorded in the audit
        trail. confirm=true."""
        require_confirm(f"set_stock {item_code}", confirm)
        return get_client().call_method(
            "ffl_core.api.item_admin.set_stock",
            {"item_code": item_code, "new_qty": new_qty, "warehouse": warehouse,
             "reason": reason},
        )

    @mcp.tool()
    def toggle_service_need(serial: str, value: bool, confirm: bool = False) -> Any:
        """Set a firearm's 'Service Needed' (gunsmith) flag on its Serial No, opening
        or closing the gunsmith ToDo accordingly. confirm=true."""
        require_confirm(f"toggle_service_need {serial}", confirm)
        return get_client().call_method(
            "ffl_core.firearm.toggle_serial_service_need",
            {"serial": serial, "value": 1 if value else 0},
        )

    # ---- C. compliance / FastBound (writes) ----------------------------------

    # push_serial_to_fastbound moved to compliance.fastbound_serial_fix(action="push").

    @mcp.tool()
    def verify_supplier_ffl(supplier_name: str, confirm: bool = False) -> Any:
        """Live ATF EZ Check on a Supplier's FFL and update that supplier's
        verification status/fields. confirm=true (it writes to the Supplier)."""
        require_confirm(f"verify_supplier_ffl {supplier_name}", confirm)
        return get_client().call_method(
            "ffl_integrations.atf.ez_check_api.verify_supplier_ffl",
            {"supplier_name": supplier_name},
        )

    @mcp.tool()
    def reverify_all_ffls(confirm: bool = False) -> Any:
        """Re-run ATF EZ Check for EVERY FFL supplier on file (batch re-verification).
        confirm=true."""
        require_confirm("reverify_all_ffls", confirm)
        return get_client().call_method("ffl_integrations.atf.ez_check.re_verify_all")

    # ---- D. RSR promote (writes) ---------------------------------------------

    @mcp.tool()
    def promote_to_item(payload: dict, confirm: bool = False) -> Any:
        """Promote an RSR catalog row into a sellable Item (manufacturer / model /
        caliber / prices / images flow in from the catalog). payload mirrors the
        promote flow, e.g. {rsr_stock_number, item_code?, item_name?, ...}.
        confirm=true. Find candidates with rsr_catalog_search."""
        require_confirm("promote_to_item", confirm)
        return get_client().call_method(
            "ffl_integrations.rsr.promote.promote_to_item", {"payload": payload}
        )

    @mcp.tool()
    def backfill_from_rsr(
        item_codes: list[str] | str | None = None, confirm: bool = False
    ) -> Any:
        """Backfill manufacturer / model / caliber / etc. on existing Items from the
        RSR catalog (fills only empty fields, never overwrites). item_codes omitted =
        all RSR-linked items. confirm=true."""
        require_confirm("backfill_from_rsr", confirm)
        return get_client().call_method(
            "ffl_integrations.rsr.promote.backfill_from_rsr", {"item_codes": item_codes}
        )

    # ---- E. order / fulfillment queue (the Pending Order page's actions) ------

    @mcp.tool()
    def pending_orders() -> Any:
        """The Pending Order queue, counter + dealer rows: submitted firearm
        shipments that still need disposing, payment, or a ShipStation push.
        Web-shop rows come from pending_web_orders. Consignment fulfillment is
        NOT here — outbound consignments live in their own queue (see
        consignment_queue); this list explicitly excludes consignment invoices.
        Read-only."""
        return get_client().call_method(
            "ffl_core.api.manual_order.list_pending_dispositions"
        )

    @mcp.tool()
    def pending_web_orders() -> Any:
        """Paid WooCommerce web orders awaiting fulfillment — the source='woo'
        rows of the Pending Order page. Read-only."""
        return get_client().call_method(
            "ffl_woo_sync.woocommerce.fulfillment.list_pending_web_orders"
        )

    @mcp.tool()
    def dispose_order(sales_invoice: str, confirm: bool = False) -> Any:
        """Dispose (ship) the firearms on a submitted counter/dealer order: books
        one FFL transfer disposition per not-yet-disposed serial; each disposition's
        on_submit issues the stock-out and pushes FastBound. Server blocks it while
        unpaid or the destination FFL is invalid; idempotent. VERIFY the destination
        FFL and serials first (the UI makes staff tick exactly that). confirm=true."""
        require_confirm(f"dispose_order {sales_invoice}", confirm)
        return get_client().call_method(
            "ffl_core.api.manual_order.dispose_order",
            {"sales_invoice": sales_invoice},
        )

    @mcp.tool()
    def dispose_web_order(woo_online_order: str, confirm: bool = False) -> Any:
        """Dispose the firearms on a paid Woo Online Order (web row). No ShipStation
        push here — the store's own WooCommerce plugin ships web orders. Idempotent
        per {serial, order}. confirm=true."""
        require_confirm(f"dispose_web_order {woo_online_order}", confirm)
        return get_client().call_method(
            "ffl_woo_sync.woocommerce.fulfillment.dispose_web_order",
            {"woo_online_order": woo_online_order},
        )

    @mcp.tool()
    def record_payment(
        sales_invoice: str, mode_of_payment: str, amount: float | None = None,
        transaction_number: str | None = None, confirm: bool = False,
    ) -> Any:
        """Record a later payment against a submitted, not-fully-paid Sales Invoice
        — books + submits a Payment Entry and returns the new balance. amount
        omitted = the full outstanding. mode_of_payment is 'Cash', 'Credit Card',
        'Zelle', 'ACH' or 'Check/Money Order' (a paper check or money order:
        booked to the bank, not the cash drawer). 'Zelle' / 'ACH' require
        transaction_number, 'Check/Money Order' its check or money order number
        there, and so does 'Credit Card' while Payroc is live (the
        Virtual Terminal approval code — terminal charges book themselves) or on
        a standalone terminal with approval codes required. confirm=true."""
        require_confirm(f"record_payment {sales_invoice}", confirm)
        return get_client().call_method(
            "ffl_core.api.manual_order.record_payment",
            {"sales_invoice": sales_invoice, "mode_of_payment": mode_of_payment,
             "amount": amount, "transaction_number": transaction_number},
        )

    # Give card money back on a counter Sales Invoice (POS 0.5.0, payments/si_reversal.py).
    # Server-side: manager roles only, money goes back only to the card it came from,
    # and an unconfirmed outcome locks the charge until resolve_card_reversal.

    @mcp.tool()
    def card_charges(sales_invoice: str) -> Any:
        """The card charges on a Sales Invoice that can still be voided or refunded:
        each with its id (pass it as terminal_payment), amount, refunded_amount, left,
        card, gateway reference, can_void, and locked/lock for a void or refund the
        processor never confirmed. Also says whether it is a membership invoice and
        why a Return is not possible (stock / bound book). Read-only; managers only
        (Sales Manager / System Manager), like the void and refund themselves."""
        return get_client().call_method(
            "ffl_integrations.payments.si_reversal.card_charges",
            {"sales_invoice": sales_invoice},
        )

    @mcp.tool()
    def void_card_charge(terminal_payment: str, reason: str, confirm: bool = False) -> Any:
        """Void a whole card charge on a Sales Invoice before the processor settles it
        (usually the same day): the processor reverses it and the invoice owes the
        amount again (today's Payment Entry is cancelled, else a Journal Entry today).
        Refused on a membership invoice (refund it as a Return) or once part was
        refunded; a settled charge is refused by the processor — refund it instead.
        terminal_payment comes from card_charges. confirm=true."""
        require_confirm(f"void_card_charge {terminal_payment}", confirm)
        reason = require_reason(f"void_card_charge {terminal_payment}", reason)
        return get_client().call_method(
            "ffl_integrations.payments.si_reversal.void_sales_invoice_charge",
            {"terminal_payment": terminal_payment, "reason": reason},
        )

    @mcp.tool()
    def refund_card_charge(
        terminal_payment: str, amount: float, refund_type: str, reason: str,
        confirm: bool = False,
    ) -> Any:
        """Refund part or all of a card charge on a Sales Invoice to the card it came
        from. refund_type 'Return' (goods/service given back or price lowered): a
        credit note takes the amount off the sale, tax in proportion — not for
        invoices that touched stock or the bound book (use cancel_order), and on a
        membership invoice only after the membership was cancelled. 'Collect again'
        (the sale stands): the invoice owes the amount again. amount at most what is
        left (card_charges); a Return refunds the credit note total. confirm=true."""
        require_confirm(f"refund_card_charge {terminal_payment}", confirm)
        reason = require_reason(f"refund_card_charge {terminal_payment}", reason)
        return get_client().call_method(
            "ffl_integrations.payments.si_reversal.refund_sales_invoice_charge",
            {"terminal_payment": terminal_payment, "amount": amount,
             "refund_type": refund_type, "reason": reason},
        )

    @mcp.tool()
    def resolve_card_reversal(
        terminal_payment: str, went_through: bool, note: str,
        portal_reference: str | None = None, confirm: bool = False,
    ) -> Any:
        """Settle a void or refund the card processor never confirmed (the charge is
        locked until then). Look it up in the processor's portal FIRST:
        went_through=true books it exactly as an approval would (portal_reference
        required); false unlocks the charge and books nothing. note = what was
        checked. Never guess — a wrong answer books money that did not move.
        confirm=true."""
        require_confirm(f"resolve_card_reversal {terminal_payment}", confirm)
        note = require_reason(f"resolve_card_reversal {terminal_payment}", note)
        return get_client().call_method(
            "ffl_integrations.payments.si_reversal.resolve_card_reversal",
            {"terminal_payment": terminal_payment, "went_through": 1 if went_through else 0,
             "note": note, "portal_reference": portal_reference},
        )

    @mcp.tool()
    def push_shipment(sales_invoice: str, confirm: bool = False) -> Any:
        """Push a submitted order to ShipStation so staff can buy the label there —
        the Pending Order 'Ship' action. Synchronous, idempotent, fails closed on an
        invalid FFL or unpaid order; independent of the Dispose step. confirm=true."""
        require_confirm(f"push_shipment {sales_invoice}", confirm)
        return get_client().call_method(
            "ffl_integrations.shipstation.api.push_shipment",
            {"sales_invoice": sales_invoice},
        )

    @mcp.tool()
    def mark_shipped_manually(sales_invoice: str, confirm: bool = False) -> Any:
        """Clear a DISPOSED order from the Pending Order queue without a ShipStation
        push (label bought elsewhere / integration off / FFL expired after dispose).
        Refuses while any firearm is undisposed. confirm=true."""
        require_confirm(f"mark_shipped_manually {sales_invoice}", confirm)
        return get_client().call_method(
            "ffl_core.api.manual_order.mark_shipped_manually",
            {"sales_invoice": sales_invoice},
        )

    @mcp.tool()
    def shipstation_test_connection() -> Any:
        """Probe the ShipStation API (GET /v2/carriers; read-only)."""
        return get_client().call_method(
            "ffl_integrations.shipstation.api.test_connection"
        )

    # ---- F. POS 4473 / transfers -----------------------------------------------

    @mcp.tool()
    def start_4473(invoice_name: str, serial_by_item_row: dict, confirm: bool = False) -> Any:
        """Kick off the ATF 4473 for a firearm POS/Sales Invoice: holds the invoice
        in Draft and opens a FastBound 4473 for the cashier to complete.
        serial_by_item_row maps invoice ITEM ROW name -> actual serial, e.g.
        {"a1b2c3d4": "SN123"}. Returns the FastBound URL. confirm=true."""
        require_confirm(f"start_4473 {invoice_name}", confirm)
        return get_client().call_method(
            "ffl_integrations.fastbound.pos_4473.start_4473_for_pos_invoice",
            {"invoice_name": invoice_name, "serial_by_item_row": serial_by_item_row},
        )

    @mcp.tool()
    def manager_override_4473(invoice_name: str, reason: str, confirm: bool = False) -> Any:
        """Manager force-complete a stuck firearm POS sale whose 4473 WAS genuinely
        completed in FastBound but never synced back (edited serial, deleted
        disposition, FastBound down). Mints the Retail Sale disposition(s) marked
        manual_override with WHO + WHY — auditable; does NOT push FastBound, so
        reconcile the bound book separately. reason is required. confirm=true."""
        require_confirm(f"manager_override_4473 {invoice_name}", confirm)
        return get_client().call_method(
            "ffl_integrations.fastbound.pos_4473.manager_override_4473",
            {"invoice_name": invoice_name, "reason": reason},
        )

    @mcp.tool()
    def start_transfer_4473(
        serials: list[str], fee_item: str, fee_amount: float, customer: str,
        pos_profile: str | None = None, invoice_name: str | None = None,
        confirm: bool = False,
    ) -> Any:
        """Start a customer-transfer 4473: builds the transfer POS Invoice server-side
        ($0 line per gun + one Transfer Fee line) and opens the FastBound 4473. Get
        fee_item and the default fee via frappe_run_method
        'ffl_core.firearm.get_transfer_config'. FFL-dealer customers are refused
        server-side (their 4473 gate doesn't apply). confirm=true."""
        require_confirm(f"start_transfer_4473 {customer}", confirm)
        return get_client().call_method(
            "ffl_integrations.fastbound.pos_4473.start_transfer_for_pos_invoice",
            {"serials": serials, "fee_item": fee_item, "fee_amount": fee_amount,
             "customer": customer, "pos_profile": pos_profile,
             "invoice_name": invoice_name},
        )

    # ---- G. consignment out (寄售) — the At Dealer queue's full lifecycle -------

    @mcp.tool()
    def consignment_queue(include_closed: bool = False) -> Any:
        """The outbound-consignment "At Dealer" queue — every unfinished
        Consignment Out (Draft / Shipped) as one card: dealer + shippability,
        ShipStation state, tracking, line pills, and the action gating the
        Dispose/Ship buttons read. An INDEPENDENT queue from pending_orders
        (which excludes consignment invoices). include_closed=true also returns
        Closed/Cancelled history. Read-only."""
        return get_client().call_method(
            "osa_consignment.api.consignment_out.list_consignment_queue",
            {"include_closed": 1 if include_closed else 0},
        )

    @mcp.tool()
    def consignment_dealers() -> Any:
        """Every FFL-dealer Customer with its consignment shippability: FFL
        number, eZ-Check status, expiry, plus shippable/block_reason (the same
        gate ship_consignment_out enforces). Use before building a consignment.
        Read-only."""
        return get_client().call_method(
            "osa_consignment.api.consignment_out.list_consignment_dealers"
        )

    @mcp.tool()
    def consignment_serials(
        item_code: str | None = None, search: str | None = None, limit: int = 50
    ) -> Any:
        """In-stock (Active) serials pickable for an outbound consignment,
        annotated with settlement/reference prices (cost = dealer price,
        msrp = retail) and — for blocked guns — why they can't be picked
        (blocked rows are returned greyed, not hidden). Filter by item_code
        and/or a search string. Read-only."""
        return get_client().call_method(
            "osa_consignment.api.consignment_out.available_serials_for_consignment",
            {"item_code": item_code, "search": search, "limit": limit},
        )

    @mcp.tool()
    def consignment_dealer_orders() -> Any:
        """The post-sale consignment settlement queue: Sold lines whose
        settlement invoice isn't booked yet OR whose invoice isn't paid off.
        Failed rows sort first. Retry a failed settlement invoice with
        retry_consignment_invoice. Read-only."""
        return get_client().call_method(
            "osa_consignment.api.dealer_orders.list_dealer_orders"
        )

    @mcp.tool()
    def create_consignment_out(payload: dict, confirm: bool = False) -> Any:
        """Build an outbound consignment to an FFL dealer. payload: {dealer,
        company?, notes?, dispose_now?, lines: [{item_code, serial, cost, msrp}]}.
        dispose_now=0 (default) saves a Draft (guns held, Woo delisted, dispose
        later from the queue); dispose_now=1 also disposes + best-effort
        ShipStation push immediately — if a gate blocks (expired dealer FFL) the
        build still succeeds as Draft with degraded=1. Find candidates with
        consignment_dealers / consignment_serials. Consequential — confirm=true."""
        require_confirm("create_consignment_out", confirm)
        return get_client().call_method(
            "osa_consignment.api.consignment_out.create_consignment_out",
            {"payload": payload},
        )

    @mcp.tool()
    def ship_consignment_out(consignment_out: str, confirm: bool = False) -> Any:
        """Dispose (ship) every Draft line of a Consignment Out: books the
        FFL transfer disposition per gun, moves stock to the consignment
        warehouse, pushes FastBound. All lines are validated before any is
        mutated; re-running ships 0 (idempotent). Server blocks on an invalid
        dealer FFL. VERIFY the dealer + serials first. confirm=true."""
        require_confirm(f"ship_consignment_out {consignment_out}", confirm)
        return get_client().call_method(
            "osa_consignment.api.consignment_out.ship_consignment_out",
            {"consignment_out": consignment_out},
        )

    @mcp.tool()
    def push_consignment_shipment(consignment_out: str, confirm: bool = False) -> Any:
        """Push a DISPOSED Consignment Out to ShipStation so staff can buy the
        outbound label — the At Dealer queue's Ship action. Synchronous,
        idempotent, persists nothing on API failure (safe to retry). Independent
        of the Dispose step. confirm=true."""
        require_confirm(f"push_consignment_shipment {consignment_out}", confirm)
        return get_client().call_method(
            "osa_consignment.shipping.push_consignment_shipment",
            {"consignment_out": consignment_out},
        )

    @mcp.tool()
    def mark_consignment_shipped(
        consignment_out: str, tracking_number: str | None = None,
        carrier: str | None = None, confirm: bool = False,
    ) -> Any:
        """Clear a DISPOSED consignment's Ship step without a ShipStation push
        (label bought elsewhere / integration off / dealer FFL expired after
        dispose) — the consignment twin of mark_shipped_manually. Optional
        tracking_number / carrier hand-record the label so the dealer portal
        shows it. confirm=true."""
        require_confirm(f"mark_consignment_shipped {consignment_out}", confirm)
        return get_client().call_method(
            "osa_consignment.api.consignment_out.mark_consignment_shipped_manually",
            {"consignment_out": consignment_out,
             "tracking_number": tracking_number, "carrier": carrier},
        )

    @mcp.tool()
    def retry_consignment_invoice(line: str, confirm: bool = False) -> Any:
        """Retry the settlement Sales Invoice for a Sold consignment line whose
        automatic booking failed (error log / total mismatch / dealer FFL
        expired at submit) — the Dealer Orders queue's Retry Invoice action.
        line = the Consignment Out Line name (see consignment_dealer_orders).
        To UNDO a booked settlement, cancel that settlement Sales Invoice
        instead (frappe_cancel_document) — the cancel hook reopens the parent.
        confirm=true."""
        require_confirm(f"retry_consignment_invoice {line}", confirm)
        return get_client().call_method(
            "osa_consignment.api.dealer_orders.create_consignment_invoice_now",
            {"line": line},
        )

    @mcp.tool()
    def return_consignment_lines(
        consignment_out: str, lines: list[str],
        to_warehouse: str | None = None, confirm: bool = False,
    ) -> Any:
        """Take unsold consigned guns BACK from the dealer: books a real FFL
        re-acquisition per gun (pushed to FastBound by its own hook) and moves
        stock back to the main (or given) warehouse. lines = Consignment Out
        Line names. confirm=true."""
        require_confirm(f"return_consignment_lines {consignment_out}", confirm)
        return get_client().call_method(
            "osa_consignment.api.consignment_out.return_consignment_lines",
            {"consignment_out": consignment_out, "lines": lines,
             "to_warehouse": to_warehouse},
        )

    @mcp.tool()
    def cancel_consignment(
        consignment_out: str, reason: str, line: str | None = None,
        confirm: bool = False,
    ) -> Any:
        """Cancel a consignment that should never have happened. With line:
        cancel ONE shipped line ("the gun never left the store" — undoes the
        disposition; only reachable pre-invoice). Without line: cancel the whole
        DRAFT document (never-shipped staging record; books/moves nothing).
        reason is required and recorded. confirm=true."""
        require_confirm(f"cancel_consignment {consignment_out}", confirm)
        reason = require_reason(f"cancel_consignment {consignment_out}", reason)
        if line:
            return get_client().call_method(
                "osa_consignment.api.consignment_out.cancel_consignment_line",
                {"consignment_out": consignment_out, "line": line, "reason": reason},
            )
        return get_client().call_method(
            "osa_consignment.api.consignment_out.cancel_consignment_out",
            {"consignment_out": consignment_out, "reason": reason},
        )

    @mcp.tool()
    def update_consignment_prices(
        consignment_out: str, prices: dict, confirm: bool = False,
    ) -> Any:
        """Edit the per-consignment price snapshot on At Dealer lines — the
        queue's "Edit prices" dialog (PR #224). prices: {line_name: {"cost":
        required > 0, "msrp": tri-state}} — omit the "msrp" key to keep the
        stored MSRP, send "" / null to clear it, anything else must be > 0.
        Writes ONLY this shipment's line snapshot ("Dealer Price" + MSRP),
        never Serial No / Item masters; settlement and the dealer portal read
        the same snapshot, so both follow automatically. Server gates: line
        must be At Dealer with no booked settlement invoice; the whole batch
        is validated before anything is written (one bad line rejects all).
        Line names come from consignment_queue. confirm=true."""
        require_confirm(f"update_consignment_prices {consignment_out}", confirm)
        return get_client().call_method(
            "osa_consignment.api.consignment_out.update_consignment_line_prices",
            {"consignment_out": consignment_out, "prices": prices},
        )

    # ---- H. counter-order cancel ---------------------------------------------

    @mcp.tool()
    def cancel_order(
        sales_invoice: str, reason: str, refund_mode: str | None = None,
        refund_reference: str | None = None, confirm: bool = False,
    ) -> Any:
        """Cancel a submitted counter/dealer transfer order — the Pending Order
        page's Cancel Order action. Cascades safely: cancels the invoice (its
        hooks reverse the dispositions → stock back in → FastBound delete
        queued), cancels linked Sales Orders, voids the ShipStation order, and
        books a refund Payment Entry for whatever was actually paid
        (refund_mode/refund_reference when a refund is due: refund_mode is one of
        record_payment's modes; Zelle, ACH and Check/Money Order need
        refund_reference). reason is required
        and recorded. confirm=true."""
        require_confirm(f"cancel_order {sales_invoice}", confirm)
        reason = require_reason(f"cancel_order {sales_invoice}", reason)
        return get_client().call_method(
            "ffl_core.api.manual_order.cancel_order",
            {"sales_invoice": sales_invoice, "reason": reason,
             "refund_mode": refund_mode, "refund_reference": refund_reference},
        )

    # ---- I. file upload ----------------------------------------------------------

    # Remote (http) servers never register it: file_path is a path on the SERVER,
    # so a remote caller could upload the server's own files (env, keys).
    if get_transport() != HTTP:
        @mcp.tool()
        def upload_attachment(
            file_path: str, doctype: str | None = None, name: str | None = None,
            fieldname: str | None = None, is_private: bool = True,
        ) -> Any:
            """Upload a LOCAL file to the POS as a File document, optionally attached to
            a document (doctype + name) and/or set into its Attach field (fieldname).
            Returns the File doc incl. file_url. Default is_private=true — product
            photos that WooCommerce must sideload need is_private=false. For bulk
            photo+gallery imports prefer the firearm-listing-import script (it resizes
            images first; oversized originals break the Woo push)."""
            return get_client().upload_file(
                file_path, doctype=doctype, docname=name,
                fieldname=fieldname, is_private=is_private,
            )

    register_channels(mcp)


# ===================================================================== channels
#
# Web-order repairs, store-wide Woo sweeps, the GunBroker order lane and the card
# processor probe. register() calls register_channels() last, so these share the
# curated bucket and its GunBroker switch.
#
# Signatures checked against gunstore-pos develop (merge 6e1c546). One wrapped
# method does not exist there yet and is coded against the planned POS endpoint:
# ffl_woo_sync.woocommerce.order_audit.shipped_undisposed_orders() (no args).

_WOO = "ffl_woo_sync.woocommerce."
_GB = "ffl_integrations.gunbroker."
_PAY = "ffl_integrations.payments."

# The sweeps and backstops below are synchronous on the POS and can run for
# minutes; a 30 s client timeout would report a failure while the server keeps
# writing (the boundbook_reconcile lesson). gunicorn caps a request at 120 s.
_LONG = 130

# action -> (dotted method, takes woo_online_order, timeout)
_WEB_ORDER_ACTIONS: dict[str, tuple[str, bool, int | None]] = {
    "pickup": (_WOO + "pickup.pickup_web_order", True, None),
    "book_invoice": (_WOO + "revenue.create_web_invoice_now", True, None),
    "reconcile_refund": (_WOO + "refunds.reconcile_web_order_refund", True, None),
    "retire_antique": (_WOO + "antique.retire_antique_web_order_now", True, None),
    "run_backstop": (_WOO + "order_audit.run_web_revenue_backstop_now", False, _LONG),
    "retry_serials": (_WOO + "tasks.retry_stuck_serial_reservations", False, _LONG),
}

# action -> the extra arguments it accepts (anything else given is refused)
_STORE_SWEEP_ARGS: dict[str, frozenset[str]] = {
    "backfill_attributes": frozenset({"serials", "item_codes"}),
    "recategorize": frozenset({"under"}),
    "audit_dangling_ids": frozenset({"batch_size"}),
}

# action -> {target kind: dotted method}; a kind absent from the map is refused
_GB_LISTING_ACTIONS: dict[str, dict[str, str]] = {
    "reprice": {"serial_no": _GB + "client_api.reprice_serial_now",
                "item_code": _GB + "client_api.reprice_item_now"},
    "replace_pictures": {"serial_no": _GB + "client_api.replace_pictures_now",
                         "item_code": _GB + "client_api.replace_pictures_now"},
    "sync_quantity": {"item_code": _GB + "client_api.sync_item_quantity_now"},
    "clear_sold_unknown": {"serial_no": _GB + "client_api.clear_sold_unknown_now"},
}

_GB_UNIT_ACTIONS = frozenset({"assign_unit", "unassign_unit",
                              "mark_unit_refunded", "undo_unit_refunded"})
_GB_ORDER_METHODS: dict[str, str] = {
    "assign_unit": _GB + "fulfillment.assign_gb_unit_serial",
    "unassign_unit": _GB + "fulfillment.assign_gb_unit_serial",
    "mark_unit_refunded": _GB + "fulfillment.mark_gb_unit_refunded",
    "undo_unit_refunded": _GB + "fulfillment.mark_gb_unit_refunded",
    "push_shipment": _GB + "fulfillment.push_gb_shipment",
    "push_tracking": _GB + "fulfillment.push_tracking_now",
    "retry_invoice": _GB + "revenue.create_gb_invoice_now",
}

_GB_PAYMENT_MODES = ("GunBroker Check", "GunBroker Card")

# ffl_core.compliance.cr_transfer.CR_DETAIL_KEYS. The POS drops unknown keys and
# then reports the real one as missing, so a typo'd key is refused here by name.
_CR_DETAIL_KEYS = frozenset({
    "license_number", "license_expiry", "contact_name", "premise_street",
    "premise_street2", "premise_city", "premise_state", "premise_zip",
})


def _pick(choices, value: str, what: str) -> str:
    key = (value or "").strip().lower()
    if key not in choices:
        raise ValueError(f"{what} must be one of {', '.join(choices)} (got {value!r}).")
    return key


def _iso_date(value: str | None, what: str) -> str | None:
    """YYYY-MM-DD or None. The POS's getdate also takes 03/10/2026, which reads as
    March or October depending on who typed it — on a bank deposit date that is a
    wrong period, not a typo."""
    if value is None or not str(value).strip():
        return None
    try:
        return _dt.date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise ValueError(f"Write {what} as YYYY-MM-DD (got {value!r}).") from None


def _part(fn, *args, **kwargs) -> Any:
    """One part of a multi-call read: a POS refusal becomes that part's answer
    ({"error": ...}) instead of hiding the parts the caller may see."""
    try:
        return fn(*args, **kwargs)
    except FrappeAPIError as exc:
        return {"error": str(exc), "status": exc.status, "exc_type": exc.exc_type}


def register_channels(mcp: Any) -> None:
    # ------------------------------------------------- web orders beyond dispose

    @mcp.tool()
    def web_order_action(
        action: Literal["pickup", "book_invoice", "reconcile_refund", "retire_antique",
                        "run_backstop", "retry_serials"],
        woo_online_order: str | None = None, confirm: bool = False,
    ) -> Any:
        """Web order actions beyond dispose_web_order. confirm=true.

        Per order (woo_online_order required):
        * pickup — hand an in-store-pickup order (store_pickup on its
          pending_web_orders row) to the counter. A firearm order opens a DRAFT POS
          Invoice, already paid by WooCommerce, with the serials carried over; run
          start_4473 on it, then finish it with pos_ticket (submit). The order stays
          in the Pending Order queue until that ticket is submitted. An order with
          no regulated firearm is closed on the spot. Reply: {ok, mode:
          "counter"|"closed", pos_invoice, order_status}. Order must be Pending
          Fulfillment and addressed to this store's own FFL.
        * book_invoice — retry the revenue Sales Invoice of a Transferred order
          whose booking failed (Error Log / draft on a total mismatch).
        * reconcile_refund — net the order's Sales Invoice against its WooCommerce
          refunds (books the credit notes, or says it is already in sync).
        * retire_antique — retry retiring an antique-only order (no bound-book entry).

        Store-wide (woo_online_order refused; can run for up to two minutes):
        * run_backstop — the 2-hourly revenue/stock sweep, now: books missing
          revenue invoices and missing accessory stock legs. Reply {booked,
          delivered, failed, held}.
        * retry_serials — re-apply the serial WooCommerce holds to rows stuck on a
          stale reservation error. Reply: retried / cleared / aged-out counts.

        These are the repairs the POS error messages and web_order_exceptions name.
        After a lost reply, re-read pending_web_orders or web_order_exceptions
        instead of calling again. Counter roles."""
        key = _pick(_WEB_ORDER_ACTIONS, action, "action")
        method, per_order, timeout = _WEB_ORDER_ACTIONS[key]
        order = (woo_online_order or "").strip()
        if per_order and not order:
            raise ValueError(f"{key} needs woo_online_order.")
        if not per_order and order:
            raise ValueError(f"{key} is store-wide and takes no woo_online_order — "
                             "refusing rather than ignoring it.")
        require_confirm(f"web_order_action {key} {order}".rstrip(), confirm)
        kwargs = {"woo_online_order": order} if per_order else {}
        if timeout:
            return get_client().call_method(method, kwargs, timeout=timeout)
        return get_client().call_method(method, kwargs)

    @mcp.tool()
    def web_order_exceptions(from_date: str | None = None,
                             to_date: str | None = None) -> Any:
        """Month-end web checks, read-only. Two lists:

        * stock_legs_outstanding {invoices, count} — submitted web Sales Invoices
          whose accessory stock never left (no Delivery Note) with a posting date
          in from_date..to_date (YYYY-MM-DD; default the last 30 days, no upper
          bound). Expect count 0 before closing a month: until it is, the Sales
          Report's cost cannot be trusted against GL COGS. Fix with
          web_order_action(run_backstop); what it cannot issue is handled by hand.
        * shipped_not_disposed [{name, woo_order_id, customer_name, modified}] —
          orders WooCommerce marks completed (shipped) that the POS still holds as
          Pending Fulfillment: a firearm that left with no A&D disposition.
          Reconcile the serial against ShipStation and FastBound — never guess it —
          then dispose_web_order. Dates do not filter this list.

        If the POS predates the shipped-not-disposed endpoint, that half comes back
        as {"error": ...} and the other half is still answered. Counter roles."""
        frm = _iso_date(from_date, "from_date")
        to = _iso_date(to_date, "to_date")
        client = get_client()
        legs = client.call_method(_WOO + "order_audit.web_stock_legs_outstanding",
                                  {"from_date": frm, "to_date": to})
        # NEW POS endpoint (shares tasks._SHIPPED_UNDISPOSED_SQL with the alarm).
        shipped = _part(client.call_method,
                        _WOO + "order_audit.shipped_undisposed_orders", {})
        steps = []
        if isinstance(legs, dict) and legs.get("count"):
            steps.append("stock legs outstanding: run web_order_action(action="
                         "'run_backstop', confirm=true), then call this again.")
        if isinstance(shipped, list) and shipped:
            steps.append("shipped but not disposed: confirm each serial with "
                         "ShipStation and FastBound, then dispose_web_order.")
        return {"stock_legs_outstanding": legs, "shipped_not_disposed": shipped,
                "next_steps": steps}

    @mcp.tool()
    def woo_store_maintenance(
        action: Literal["backfill_attributes", "recategorize", "audit_dangling_ids"],
        apply: bool = False, serials: list[str] | None = None,
        item_codes: list[str] | None = None, under: list[str] | None = None,
        batch_size: int | None = None, confirm: bool = False,
    ) -> Any:
        """Store-wide sweeps of the retail WooCommerce store. apply=false (default)
        only REPORTS what would change; apply=true changes the store and needs
        confirm=true. Run the report first. System Manager.

        * backfill_attributes — re-push ONLY the Caliber / Condition / Manufacturer
          / Model attributes of listed firearms (after a grade or is_used fix; any
          other attribute on those products is removed). serials and/or item_codes
          pick the products; with neither the report covers every live firearm, but
          apply=true needs a list — it is synchronous, so batch by item_codes.
        * recategorize — re-file products into their category-tree term (keeps
          Clearance / Open Box). under = node slugs to limit it to. apply=true
          queues a background job: the reply's queued=true is a receipt, read the
          Error Log for refusals.
        * audit_dangling_ids — find stored Woo product ids whose product was
          deleted (404) — the follow-up to woo_reconcile's dangling list. apply=true
          clears them (Comment + warning log each). batch_size 1..200 (default 50).

        Arguments that do not belong to the action are refused."""
        key = _pick(_STORE_SWEEP_ARGS, action, "action")
        given = {"serials": serials, "item_codes": item_codes, "under": under,
                 "batch_size": batch_size}
        stray = sorted(k for k, v in given.items()
                       if v is not None and k not in _STORE_SWEEP_ARGS[key])
        if stray:
            raise ValueError(f"{stray} do not apply to {key} — refusing rather than "
                             "dropping them silently.")
        if key == "backfill_attributes" and apply and not (serials or item_codes):
            raise ValueError("backfill_attributes with apply=true needs serials or "
                             "item_codes: a whole-store push is synchronous and would "
                             "time out while the server keeps writing. Batch by item_codes.")
        if apply:
            require_confirm(f"woo_store_maintenance {key} apply", confirm)
        if key == "backfill_attributes":
            method = _WOO + "condition_backfill.backfill_condition_attributes"
            kwargs = {"serials": serials, "item_codes": item_codes,
                      "dry_run": 0 if apply else 1}
        elif key == "recategorize":
            method = _WOO + "categories.recategorize_store"
            kwargs = {"site": "retail", "dry_run": 0 if apply else 1, "under": under}
        else:
            method = _WOO + "dangling.woo_audit_dangling_ids"
            kwargs = {"site": "retail", "fix": 1 if apply else 0,
                      "batch_size": 50 if batch_size is None else batch_size}
        return get_client().call_method(method, kwargs, timeout=_LONG)

    # ------------------------------------------------- GunBroker order lane
    #
    # Default-registered, outside GUNSTORE_MCP_GUNBROKER_ACTIONS: these work an
    # order that is already sold. None of them creates, changes or ends a listing
    # offer — that is what the switch is about. push_tracking does write to
    # GunBroker (the tracking number the buyer is waiting for) and assign_unit
    # takes the named gun off the other channels; both are fulfilment of a sale
    # that has happened, and both are confirm-gated.

    @mcp.tool()
    def gb_pending_orders(gunbroker_order: str | None = None) -> Any:
        """Open GunBroker orders — the third lane of the Pending Order page, which
        pending_orders and pending_web_orders do not include. One card per order:
        channel_stage (awaiting payment / ready to dispose / awaiting ship /
        shipped), paid + paid_total vs order_total, assign_units (units of a model
        listing still needing a gun; row = the row_name gb_order_action takes) with
        assign_candidates, pending_serials, serial_errors, stock_units, is_cr +
        cr_prefill, destination_ffl / ffl_expired / ships_direct, sales_invoice and
        its state, ship_pushed. An order stays listed until its tracking number is
        on GunBroker.

        gunbroker_order: return just that card, plus banned_customer (the buyer is
        a Banned customer — the POS does NOT refuse a GunBroker dispose for that;
        stop and ask a manager) and stock_lines (where each loose stock line can be
        taken from — the keys gb_dispose_order's locations uses) when it has loose
        stock; {"found": false} when it is not open. Every GunBroker order action
        takes its inputs from this card. Read-only; counter roles."""
        client = get_client()
        rows = client.call_method(_GB + "fulfillment.list_pending_gb_orders")
        name = (gunbroker_order or "").strip()
        if not name:
            return rows
        card = next((r for r in rows or [] if isinstance(r, dict)
                     and r.get("gunbroker_order") == name), None)
        if card is None:
            return {"found": False, "gunbroker_order": name,
                    "note": "Not an open GunBroker order (finished orders leave the "
                            "list once tracking is on GunBroker)."}
        # The Pending Order page's red badge; not a field on the card itself.
        banned = _part(client.call_method, "ffl_core.customer_flags.banned_order_rows",
                       {"gb_orders": [name]})
        card = {**card, "banned_customer": name in (banned.get("gb") or [])
                if "error" not in banned else banned}
        if card.get("stock_units"):
            card = {**card, "stock_lines": client.call_method(
                _GB + "fulfillment.get_dispose_stock_lines", {"gunbroker_order": name})}
        return card

    @mcp.tool()
    def gb_record_payment(
        gunbroker_order: str,
        mode_of_payment: Literal["GunBroker Check", "GunBroker Card"],
        amount: float, reference_no: str | None = None,
        received_on: str | None = None, replace_posted_receipt: bool = False,
        confirm: bool = False,
    ) -> Any:
        """Record the payment banked for a GunBroker order — the fact the Dispose
        gate opens on (GunBroker's own "paid" flag does not open it). No Payment
        Entry yet: the values are replayed onto one when Dispose raises the invoice.

        mode_of_payment 'GunBroker Check' (reference_no = cheque / money-order
        number, required) or 'GunBroker Card' (refused while no card gateway is
        connected). amount = what was actually banked; a difference from the order
        total is flagged, not refused. received_on = the day it went INTO the bank,
        YYYY-MM-DD (default today, never future). replace_posted_receipt=true to
        record over an order whose receipt is already posted (alerted on).
        Reply {ok, mode_of_payment, amount, received_on, difference}. Counter
        roles. confirm=true."""
        mode = (mode_of_payment or "").strip()
        if mode not in _GB_PAYMENT_MODES:
            raise ValueError(f"mode_of_payment must be one of {', '.join(_GB_PAYMENT_MODES)} "
                             f"(got {mode_of_payment!r}).")
        if amount is None or amount <= 0:
            raise ValueError("amount must be what was banked, above 0.")
        ref = (reference_no or "").strip() or None
        if mode == "GunBroker Check" and not ref:
            raise ValueError("A GunBroker Check needs reference_no (the cheque or "
                             "money-order number) — it ties the deposit to the bank statement.")
        received = _iso_date(received_on, "received_on")
        require_confirm(f"gb_record_payment {gunbroker_order} {mode} ${amount}", confirm)
        return get_client().call_method(
            _GB + "fulfillment.mark_payment_received",
            {"gunbroker_order": gunbroker_order, "mode_of_payment": mode,
             "amount": amount, "reference_no": ref, "received_on": received,
             "replace_posted_receipt": 1 if replace_posted_receipt else 0},
        )

    @mcp.tool()
    def gb_order_action(
        gunbroker_order: str,
        action: Literal["assign_unit", "unassign_unit", "mark_unit_refunded",
                        "undo_unit_refunded", "push_shipment", "push_tracking",
                        "retry_invoice"],
        row_name: str | None = None, serial_no: str | None = None,
        confirm: bool = False,
    ) -> Any:
        """Work a sold GunBroker order (inputs from gb_pending_orders). confirm=true.

        Unit actions (row_name required = assign_units[].row, "<gb item id>:<unit>";
        the reply carries the refreshed card as "row"):
        * assign_unit (serial_no required) — name the gun for one unit of a model
          listing. RESERVES the gun and takes it off the other channels; naming a
          different gun swaps them. Refused for another model, a customer's gun, one
          not free, or (Type 03 orders) one not old enough for C&R.
        * unassign_unit — make the unit an open claim again (releases its gun).
        * mark_unit_refunded — the buyer was refunded for this unit on GunBroker: no
          gun, no bound-book entry, no invoice line (releases a named gun).
        * undo_unit_refunded — undo that mark.

        Order actions (row_name / serial_no refused):
        * push_shipment — push the paid order to ShipStation to buy the label
          (Dispose's gates apply; a C&R order not yet verified must Dispose first).
        * push_tracking — write the label's tracking number back to GunBroker now
          (after Dispose). {ok: false, reason} when there is no live label yet.
        * retry_invoice — retry the revenue booking of a disposed order whose
          invoice failed, stopped in Draft, or posted without its receipt.

        Check banned_customer on the single-order card first. Counter roles."""
        key = _pick(_GB_ORDER_METHODS, action, "action")
        row = (row_name or "").strip()
        serial = (serial_no or "").strip()
        if key in _GB_UNIT_ACTIONS and not row:
            raise ValueError(f"{key} needs row_name (assign_units[].row on the "
                             "gb_pending_orders card).")
        if key not in _GB_UNIT_ACTIONS and row:
            raise ValueError(f"{key} acts on the whole order and takes no row_name.")
        if key == "assign_unit" and not serial:
            raise ValueError("assign_unit needs serial_no (to unassign, use "
                             "action='unassign_unit').")
        if key != "assign_unit" and serial:
            raise ValueError(f"{key} takes no serial_no — refusing rather than ignoring it.")
        require_confirm(f"gb_order_action {key} {gunbroker_order} {row}".rstrip(), confirm)
        kwargs: dict[str, Any] = {"gunbroker_order": gunbroker_order}
        if key in ("assign_unit", "unassign_unit"):
            kwargs.update(row_name=row, serial_no=serial)
        elif key in ("mark_unit_refunded", "undo_unit_refunded"):
            kwargs.update(row_name=row, refunded=1 if key == "mark_unit_refunded" else 0)
        return get_client().call_method(_GB_ORDER_METHODS[key], kwargs)

    @mcp.tool()
    def gb_dispose_order(
        gunbroker_order: str, locations: dict[str, str] | None = None,
        cr_details: dict[str, str] | None = None, confirm: bool = False,
    ) -> Any:
        """Book the bound-book exit of a paid GunBroker order (the Dispose action):
        one submitted FFL Transfer disposition per regulated gun (each issues its
        stock-out and queues its FastBound push), antiques issued out of stock with
        no disposition, loose stock issued, then the revenue invoice raised (a
        failure there keeps the dispositions — retry with gb_order_action
        retry_invoice). Idempotent per {serial, order}.

        VERIFY the destination FFL and the named serials on the gb_pending_orders
        single-order card first, and stop if banned_customer is true. locations: {stock line id: Storage
        Location} from the card's stock_lines (a line left out goes to the "to
        confirm" list). cr_details, Type 03 (C&R) orders only, read off the licence:
        {license_number (1-23-456-03-7C-89012), license_expiry, contact_name,
        premise_street, premise_street2?, premise_city, premise_state, premise_zip}
        — premises must match the ship-to state/ZIP. Reply {ok, dispositions,
        disposed_count, retired, issued, sales_invoice, invoice_status}. Counter
        roles. confirm=true."""
        if cr_details:
            unknown = sorted(set(cr_details) - _CR_DETAIL_KEYS)
            if unknown:
                raise ValueError(f"cr_details keys {unknown} are not C&R fields; use "
                                 f"{', '.join(sorted(_CR_DETAIL_KEYS))}.")
        require_confirm(f"gb_dispose_order {gunbroker_order}", confirm)
        return get_client().call_method(
            _GB + "fulfillment.dispose_gb_order",
            {"gunbroker_order": gunbroker_order, "cr_details": cr_details or None,
             "locations": locations or None},
        )

    # ------------------------------------------------- card processor probe

    @mcp.tool()
    def card_processor_test_connection(processor: str | None = None,
                                       list_terminals: bool = False) -> Any:
        """Why the card terminal will not charge. Read-only.

        overview: every card processor with enabled (its switch) vs ready (switch
        on AND credentials present — on-but-not-ready is its own failure), health
        timestamps, and the card-on-file setting. probes: {code: {checks: [{label,
        ok, detail}]}} — a live, read-only probe of each processor (ok null = not
        applicable, e.g. switched off). processor = one registry code (e.g.
        "payroc") to probe only that one. list_terminals=true also asks the ACTIVE
        processor which terminals its credentials may use ({"skip": true} when none
        is switched on and configured).

        overview and probes need System Manager, terminals a manager role; a part
        this user may not see comes back as {"error": ...} with the rest still
        answered. Switching a processor on/off is not done here."""
        client = get_client()
        overview = _part(client.call_method, _PAY + "hub.overview")
        if processor:
            codes = [processor.strip()]
        elif isinstance(overview, dict) and "error" not in overview:
            codes = [p.get("code") for p in overview.get("processors") or [] if p.get("code")]
        else:
            codes = []
        probes = {c: _part(client.call_method, _PAY + "hub.test_connection",
                           {"processor": c}) for c in codes}
        out: dict[str, Any] = {"overview": overview, "probes": probes}
        if list_terminals:
            out["terminals"] = _part(client.call_method, _PAY + "api.list_terminals")
        return out

    # ------------------------------------------------- GunBroker listing writes
    #
    # Behind GUNSTORE_MCP_GUNBROKER_ACTIONS with gb_push_serial / gb_end_listing /
    # gb_pull_orders (see register()): each changes a live listing offer — its
    # price, photos, quantity (0 ends it) or the "may already be sold" guard.
    if gunbroker_actions_enabled():

        @mcp.tool()
        def gb_update_listing(
            action: Literal["reprice", "replace_pictures", "sync_quantity",
                            "clear_sold_unknown"],
            serial_no: str | None = None, item_code: str | None = None,
            note: str | None = None, confirm: bool = False,
        ) -> Any:
            """Change a LIVE GunBroker listing now. Exactly one of serial_no (a
            per-gun listing) or item_code (a model listing). confirm=true.

            * reprice — send the current price (Serial No.sell_price, or the Item's
              web price) and read it back: trust "verified": true, nothing less.
            * replace_pictures — re-send the photos a new push would send (after a
              photo or watermark change). GunBroker keeps its original search
              thumbnail. Needs SYSTEM_ROLES on the POS.
            * sync_quantity — item_code only: push the model's free stock count.
              At 0 this ENDS the listing (GunBroker refuses a quantity of 0).
            * clear_sold_unknown — serial_no only: lift the "may already be sold on
              GunBroker" flag after checking GunBroker; note (required) says what
              was checked and is recorded on the serial. This flag is what stops a
              gun being sold twice.

            A skip dict ({"ok": false, "skipped": ...}) is a normal answer — read
            its message rather than retrying."""
            key = _pick(_GB_LISTING_ACTIONS, action, "action")
            serial = (serial_no or "").strip()
            item = (item_code or "").strip()
            if bool(serial) == bool(item):
                raise ValueError("give exactly one of serial_no or item_code.")
            kind = "serial_no" if serial else "item_code"
            if kind not in _GB_LISTING_ACTIONS[key]:
                raise ValueError(f"{key} takes {' or '.join(_GB_LISTING_ACTIONS[key])} "
                                 f"only, not {kind}.")
            target = serial or item
            if key != "clear_sold_unknown" and note is not None and note.strip():
                raise ValueError(f"note applies to clear_sold_unknown only, not {key}.")
            kwargs: dict[str, Any] = {kind: target}
            if key == "clear_sold_unknown":
                kwargs["note"] = require_reason(f"gb_update_listing {key} {target}", note)
            require_confirm(f"gb_update_listing {key} {target}", confirm)
            return get_client().call_method(_GB_LISTING_ACTIONS[key][kind], kwargs)
