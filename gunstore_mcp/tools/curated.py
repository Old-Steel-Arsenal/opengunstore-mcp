"""Curated convenience tools — one call for the frequent settings / sync / report
ops. Thin wrappers over the generic backbone + the apps' whitelisted methods."""
from __future__ import annotations

import os
from typing import Any, Literal

from ..config import HTTP, _load_env, get_transport
from ..frappe_client import get_client
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

    @mcp.tool()
    def push_serial_to_fastbound(serial: str, confirm: bool = False) -> Any:
        """Push a post-acquisition per-unit correction (manufacturer / importer) for
        one already-booked firearm to FastBound, editing the bound-book item in place
        (PUT /Items/{id}). No-op if the gun isn't in FastBound yet. confirm=true."""
        require_confirm(f"push_serial_to_fastbound {serial}", confirm)
        return get_client().call_method(
            "ffl_integrations.fastbound.tasks.push_serial_to_fastbound", {"serial": serial}
        )

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
