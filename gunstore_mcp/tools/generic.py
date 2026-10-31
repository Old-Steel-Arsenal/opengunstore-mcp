"""Generic Frappe backbone tools — these cover EVERY doctype and whitelisted
method, present and future. Curated tools (curated.py) are thin sugar on top."""
from __future__ import annotations

import json
import re
import time
from typing import Any

from ..frappe_client import FrappeAPIError, get_client
from ..safety import (
    WriteRefused,
    check_fields_writable,
    check_method_call_writable,
    check_write_allowed,
    require_confirm,
    strip_passwords,
    stripped_note,
)

# A method is confirm-gated when its FINAL path segment matches a destructive /
# consequential verb (matching the segment, not the whole dotted path, avoids
# gating on a module name). The second row is this domain's own high-consequence
# verbs: dispose (bound-book disposition + stock-out), push (live Woo/FastBound/
# ShipStation writes), charge (card terminal), consolidate (posts stock + SI).
# The third row covers the consignment-out / onboarding lifecycle (#196–#222):
# ship (dispose + stock-out), sold/receive (mark_sold mints the settlement
# invoice; receive also gates create_receive's Purchase-Receipt + FastBound
# push), return (books a real re-acquisition), settle, onboard (creates a
# portal login + customer). Over-gating a rare read (get_receive_sources) is
# the accepted cost — the gate fails safe.
_DESTRUCTIVE_METHOD = re.compile(
    r"(delete|destroy|drop|truncate|wipe|reset|purge|cancel|void|refund"
    r"|force|remove|rename|bulk|import"
    r"|dispose|push|charge|consolidate"
    r"|ship|settle|return|onboard|receive|sold)",
    re.I,
)

# High-consequence whitelisted writes whose LAST path segment carries no
# destructive verb, so the regex above cannot see them (review q2, run
# 2026-07-16-mcp-cpa-mode; symmetry sweep in PR #4 review). Gated by EXACT
# dotted name — list names one by one, no prefixes or wildcards.
# "update"/"create"/"set"/"add"/"record" are deliberately not regex verbs
# (they would over-gate half of Frappe's read-adjacent surface).
# Inclusion criterion: a whitelisted method that books money, moves stock, or
# flips a compliance/tax state, and whose bare frappe_run_method call would
# otherwise pass ungated. Curated wrappers remain the primary gate; this set
# is the backstop for the expert bare path — when a new server write ships,
# check it against this criterion.
_ALWAYS_CONFIRM_METHODS = {
    # The range board's writes (osa_range extension; tools/range_board.py WRITE_METHODS).
    "osa_range.api.board.create_booking",
    "osa_range.api.board.update_booking",
    "osa_range.api.board.cancel_booking",
    "ffl_core.api.manual_order.update_order",
    "osa_consignment.api.consignment_out.update_consignment_line_prices",
    "osa_consignment.api.consignment_out.create_consignment_out",
    "ffl_core.api.manual_order.record_payment",
    # Card void/refund (POS 0.5.0): resolve books money with no gated verb in its name.
    "ffl_integrations.payments.si_reversal.resolve_card_reversal",
    "osa_consignment.api.dealer_orders.create_consignment_invoice_now",
    "ffl_core.api.item_admin.add_stock",
    "ffl_core.api.item_admin.set_stock",
    "ffl_core.api.manual_order.set_customer_tax_exempt",
    # Places every Draft distributor leg of a counter order — a real purchase.
    "ffl_core.api.manual_order.release_order_legs",
    # Shop-floor features (POS 1.5.0-beta.15). Every write of the stocktake, the cash
    # drawer and the storage map; the dedicated tools in tools/shopfloor.py all take
    # confirm, and tests/test_shopfloor.py checks each one is gated here or by the verb
    # regex. Money/stock ones: finalize posts a Stock Reconciliation, close_day /
    # record_* / undo_* book or cancel journal entries. Storage writes only move the
    # tracking layer, but the owner asked for the same confirm everywhere.
    "ffl_core.api.inventory_count.create_count",
    "ffl_core.api.inventory_count.scan",
    "ffl_core.api.inventory_count.set_qty",
    "ffl_core.api.inventory_count.toggle_serial",
    "ffl_core.api.inventory_count.undo",
    "ffl_core.api.inventory_count.finalize",
    "ffl_core.api.cash_drawer.close_day",
    "ffl_core.api.cash_drawer.record_deposit",
    "ffl_core.api.cash_drawer.record_expense",
    "ffl_core.api.cash_drawer.record_from_bank",
    "ffl_core.api.cash_drawer.record_owner_draw",  # gone from POS #688; kept gated for an older POS
    "ffl_core.api.cash_drawer.undo_entry",
    "ffl_core.api.cash_drawer.undo_close",
    "ffl_core.api.cash_drawer.record_payout",
    "ffl_core.api.storage.create_zone",
    "ffl_core.api.storage.add_positions",
    "ffl_core.api.storage.set_disabled",
    "ffl_core.api.storage.scan_move",
    "ffl_core.api.storage.undo_move",
    "ffl_core.api.storage.confirm_taken",
    # Floor plans (POS #691): a save rewrites a whole room's layout. delete_floor_plan is
    # caught by the verb regex.
    "ffl_core.api.storage.create_floor_plan",
    "ffl_core.api.storage.save_floor_plan",
    # Both now book cash on request: a trade-in intake pays the seller out
    # (payout_method -> CASH-PAYOUT) and a cost correction can book the difference
    # (payout_was_different). Neither is wrapped by a dedicated tool.
    "ffl_core.api.trade_in.create_trade_in_intake",
    "ffl_core.api.cost_correction.correct_serial_cost",
    # Full-coverage pass: every write a dedicated tool wraps (and its UI siblings) whose
    # last segment carries no gated verb. The tools take confirm themselves; these keep
    # the bare frappe_run_method path from being the one way round it.
    "ffl_core.api.manual_order.create_order",
    "ffl_core.api.manual_order.fulfill_from_stock",
    "ffl_core.api.manual_order.resend_order_agreement",
    "ffl_core.api.manual_order.resource_line",
    "ffl_core.api.manual_order.save_cr_ffl",
    "ffl_core.api.pos_close.auto_close_open_pos",
    "ffl_core.api.counter_pickup.open_pickup_invoice",
    "ffl_core.api.pos_ticket.open_ticket",  # new POS endpoint
    "ffl_core.customer_profile.merge_customer",
    "ffl_core.customer_flags.override_customer_ban",
    "ffl_core.agreements.request_agreement",
    "ffl_core.agreements.record_paper_agreement",
    "ffl_core.api.cash_drawer.record_goods_payout",
    "ffl_core.api.cash_drawer.mark_paid_outside",
    "ffl_core.api.cash_drawer.clear_paid_outside",
    "ffl_core.api.storage.set_zone_layout",
    "ffl_core.api.serial_correction.correct_serial",
    "ffl_core.api.photos.add_gallery_images",
    "ffl_core.api.extensions.set_enabled",
    "ffl_core.api.extensions.install_on_site",
    "ffl_core.api.extensions.fetch_update",
    "ffl_core.api.extensions.uninstall_from_site",
    "ffl_core.gunsmith.create_work_order",
    "ffl_core.gunsmith.start_work",
    "ffl_core.gunsmith.mark_ready",
    "ffl_core.gunsmith.notify_customer",
    "ffl_core.gunsmith.complete_work",
    "ffl_core.gunsmith.open_pickup",
    "ffl_core.gunsmith.log_in_bound_book",
    "ffl_core.gunsmith.open_deposit",  # new POS endpoint
    "ffl_core.membership.start_membership",
    "ffl_core.membership.invoice_membership",
    "ffl_core.membership.invoice_renewal",
    "ffl_core.membership.retry_renewal",
    "ffl_core.membership.set_auto_renew",
    "ffl_core.membership.change_card",
    "ffl_core.membership.discard_membership",
    "ffl_integrations.catalog_master.m3_resync",
    "ffl_integrations.rsr.promote.migrate_legacy_item_groups",
    "ffl_integrations.rsr.promote.promote_to_item",
    "ffl_integrations.rsr.promote.backfill_from_rsr",
    "ffl_integrations.distributor.api.confirm_order",
    "ffl_integrations.distributor.api.transition_order",
    "ffl_integrations.distributor.api.create_orders",
    "ffl_integrations.distributor.api.ensure_dropship_item",
    "ffl_integrations.distributor.router.update_order_ffl",
    "osa_dropship.web_router.reroute",
    "ffl_integrations.distributor.hub.toggle_flag",
    "ffl_integrations.distributor.hub.reorder",
    "ffl_integrations.atf.ez_check_api.verify_supplier_ffl",
    "ffl_integrations.atf.ez_check_api.verify_customer_ffl",
    "ffl_integrations.fastbound.inventory_sync.sync_in_stock_from_boundbook",
    "ffl_integrations.fastbound.pos_4473.attest_paper_4473",
    "ffl_integrations.fastbound.pos_4473.manager_override_4473",
    "ffl_integrations.fastbound.pos_4473.start_4473_for_pos_invoice",
    "ffl_integrations.fastbound.pos_4473.start_transfer_for_pos_invoice",
    "ffl_integrations.fastbound.pos_4473_recovery.manager_abandon_4473",
    "ffl_integrations.fastbound.pos_4473_recovery.manager_redo_4473",
    "ffl_integrations.fastbound.reconcile.apply_serial_fb_resolution",
    "ffl_integrations.fastbound.reconcile.clear_not_in_bound_book",
    "ffl_integrations.fastbound.reconcile.mark_not_in_bound_book",
    "ffl_integrations.fastbound.reconcile.match_serial_to_fastbound",
    "ffl_integrations.fastbound.reconcile.reconcile_record_to_fastbound",
    "ffl_integrations.fastbound.importer.reassign_serial_to_item",
    "ffl_integrations.gunbroker.client_api.end_listing_now",
    "ffl_integrations.gunbroker.client_api.end_item_listing_now",
    "ffl_integrations.gunbroker.client_api.reprice_serial_now",
    "ffl_integrations.gunbroker.client_api.reprice_item_now",
    "ffl_integrations.gunbroker.client_api.replace_pictures_now",
    "ffl_integrations.gunbroker.client_api.sync_item_quantity_now",
    "ffl_integrations.gunbroker.client_api.sync_orders_now",
    "ffl_integrations.gunbroker.client_api.clear_sold_unknown_now",
    "ffl_integrations.gunbroker.fulfillment.mark_payment_received",
    "ffl_integrations.gunbroker.fulfillment.correct_payment_details",
    "ffl_integrations.gunbroker.fulfillment.assign_gb_unit_serial",
    "ffl_integrations.gunbroker.revenue.create_gb_invoice_now",
    "ffl_integrations.payments.hub.toggle",
    "ffl_integrations.payments.hub.save_card_on_file",
    "ffl_integrations.payments.api.unlock_refund_lock",
    "ffl_woo_sync.woocommerce.antique.retire_antique_web_order_now",
    "ffl_woo_sync.woocommerce.categories.recategorize_store",
    "ffl_woo_sync.woocommerce.condition_backfill.backfill_condition_attributes",
    "ffl_woo_sync.woocommerce.dangling.woo_audit_dangling_ids",
    "ffl_woo_sync.woocommerce.order_audit.run_web_revenue_backstop_now",
    "ffl_woo_sync.woocommerce.pickup.pickup_web_order",
    "ffl_woo_sync.woocommerce.revenue.create_web_invoice_now",
    "ffl_woo_sync.woocommerce.tasks.retry_stuck_serial_reservations",
    "ffl_woo_sync.woocommerce.client_api.pull_product_from_woo",
    "ffl_woo_sync.woocommerce.client_api.delist_item_now",
    "ffl_woo_sync.woocommerce.client_api.delist_serial_now",
    "ffl_woo_sync.woocommerce.client_api.reconcile_now",
    "frappe.core.doctype.communication.email.make",
}

# Frappe globally whitelists these generic mutators at /api/method — reaching
# them via frappe_run_method would bypass the structured CRUD guards (denylist,
# password-strip, confirm). Refuse them and point at the guarded tools.
_BLOCKED_GENERIC_METHODS = {
    "frappe.client.set_value",
    "frappe.client.insert",
    "frappe.client.insert_many",
    "frappe.client.save",
    "frappe.client.submit",
    "frappe.client.cancel",
    "frappe.client.delete",
    "frappe.client.delete_doc",
    "frappe.client.bulk_update",
    "frappe.client.rename_doc",
    "frappe.rename_doc.rename_doc",
    "frappe.model.rename_doc.rename_doc",
}

# Endpoints frappe_run_method must never reach, and list_pos_actions never lists: the
# customer-signing chain, terminal pairing, staff PIN/lock, extension sideloading, the
# connector's own audit/identity/OAuth endpoints, the raw 4473 PDF, and guest webhooks.
_NEVER_VIA_MCP = {
    "ffl_core.agreements.load_for_signing",
    "ffl_core.agreements.submit_signature",
    "ffl_core.signing_terminal.start_pairing",
    "ffl_core.signing_terminal.pair_terminal",
    "ffl_core.signing_terminal.terminal_poll",
    "ffl_core.staff_switch.set_pin",
    "ffl_core.staff_switch.clear_pin",
    "ffl_core.staff_switch.switch_with_pin",
    "ffl_core.staff_switch.lock_screen",
    "ffl_core.staff_switch.unlock_screen",
    # Takes the approving manager's counter PIN or password.
    "ffl_core.price_approval.approve",
    "ffl_core.api.extensions.inspect_git",
    "ffl_core.api.extensions.inspect_upload",
    "ffl_core.api.extensions.install_upload",
    "ffl_core.api.connector.log_connector_call",
    "ffl_core.api.connector.connector_identity",
    "ffl_core.api.oauth_dcr.register_client",
    "ffl_integrations.fastbound.pos_4473.view_4473_pdf",
    "ffl_integrations.fastbound.webhook.fastbound_event",
    "ffl_woo_sync.woocommerce.webhook.woo_event",
}
# Every ffl_integrations.gunbroker.webhooks.* endpoint is a guest webhook.
_NEVER_VIA_MCP_PREFIXES = ("ffl_integrations.gunbroker.webhooks.",)

# Legacy Payroc aliases: the payments.api twin of the same name replaces each.
_LEGACY_PAYROC = "ffl_integrations.payroc.api."
_LEGACY_PAYROC_METHODS = {
    "charge_pos_invoice", "poll_pos_charge", "cancel_pos_charge", "charge_sales_invoice",
    "poll_sales_invoice_charge", "cancel_sales_invoice_charge", "discover_terminals",
    "void_pos_invoice", "refund_pos_invoice", "unlock_refund_lock", "confirm_out_of_band_refund",
}

_ACTION_CATALOG = "ffl_core.api.action_catalog.list_actions"
_ACTIONS_MAX = 100

# Per-method annotation maps (dedicated tool, argument warning, registration switch) live
# in gunstore_mcp/catalog.py so every tool module can feed them. Optional: without it the
# listing still carries confirm/blocked, the two fields that decide what a call does.
try:
    from ..catalog import METHOD_NOTES, METHOD_SWITCH, METHOD_TO_TOOL
except ImportError:  # catalog.py not added yet
    METHOD_TO_TOOL: dict[str, str] = {}
    METHOD_NOTES: dict[str, str] = {}
    METHOD_SWITCH: dict[str, str] = {}


def _needs_confirm(method: str) -> bool:
    """The confirm predicate of frappe_run_method, shared with list_pos_actions so the
    listing and the call can never disagree."""
    return (method in _ALWAYS_CONFIRM_METHODS
            or bool(_DESTRUCTIVE_METHOD.search(method.rsplit(".", 1)[-1])))


def _blocked_reason(method: str, row: dict | None = None) -> str | None:
    """Why frappe_run_method will not run this method, or None if it will."""
    if method in _BLOCKED_GENERIC_METHODS:
        return "generic Frappe mutator; use the frappe_*_document tools"
    if method in _NEVER_VIA_MCP or method.startswith(_NEVER_VIA_MCP_PREFIXES):
        return "never callable through the MCP (credential, signing, webhook or audit endpoint)"
    if row and row.get("guest"):
        return "guest endpoint (webhook); never callable through the MCP"
    if method.startswith(_LEGACY_PAYROC) and method[len(_LEGACY_PAYROC):] in _LEGACY_PAYROC_METHODS:
        return ("legacy alias; call ffl_integrations.payments.api."
                f"{method[len(_LEGACY_PAYROC):]} instead")
    return None


_AGREEMENT_DELIVERIES = {"email", "tablet", "none"}


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def _empty_list(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() in ("", "[]")) or value == []


def _argument_refusal(method: str, kwargs: dict) -> str | None:
    """Calls the bare path must not make: the dedicated tool does them safely."""
    if method == "ffl_integrations.distributor.receive_bridge.receive_from_do":
        return ("it books every serial the pre-filled payload echoes, not the ones read "
                "off the guns. Use distributor_receive.")
    if method == "ffl_woo_sync.woocommerce.condition_backfill.backfill_condition_attributes":
        return ("it defaults to apply and an empty list means the whole store, pushed "
                "synchronously. Use woo_store_maintenance(action='backfill_attributes').")
    if method == "ffl_core.agreements.request_agreement":
        deliver = str(kwargs.get("deliver") or "").strip().lower()
        if deliver not in _AGREEMENT_DELIVERIES:
            return ("without deliver email/tablet/none the POS answers with the customer's "
                    "signing link and QR code. Use agreement_request.")
    if method == "ffl_integrations.fastbound.inventory_sync.sync_in_stock_from_boundbook":
        if not _truthy(kwargs.get("dry_run")) and _empty_list(kwargs.get("item_ids")):
            return ("applying with no item_ids issues stock for every gun FastBound "
                    "reports disposed. Use boundbook_reconcile (dry run first, then "
                    "apply with the item_ids it lists).")
    return None


def _annotate(row: dict) -> dict:
    method = row.get("method") or ""
    out = {k: v for k, v in row.items() if k != "guest"}
    out["confirm"] = _needs_confirm(method)
    blocked = _blocked_reason(method, row)
    if blocked:
        out["blocked"] = blocked
    for key, table in (("use_tool", METHOD_TO_TOOL), ("note", METHOD_NOTES),
                       ("switch", METHOD_SWITCH)):
        if method in table:
            out[key] = table[method]
    return out


def _catalog_missing(exc: FrappeAPIError) -> bool:
    """A POS older than the action catalog: Frappe cannot import the endpoint."""
    text = " ".join(exc.messages)
    return (exc.status == 404
            or exc.exc_type in ("ModuleNotFoundError", "ImportError", "AttributeError")
            or "action_catalog" in text)


# HTTP verb per method, read from the action catalog: Frappe answers 405 when a
# GET-only endpoint (methods=["GET"]) is POSTed to. Process-wide, keyed by dotted path;
# the verb belongs to the code, not to the caller.
_VERB_TTL = 600.0
_VERB_CACHE: dict[str, tuple[float, str]] = {}


def _method_verb(client: Any, method: str) -> str:
    """'GET' when the catalog lists the method as GET-only, else 'POST' (also on a POS
    without the catalog, or when the method is not listed)."""
    hit = _VERB_CACHE.get(method)
    if hit and time.monotonic() - hit[0] < _VERB_TTL:
        return hit[1]
    try:
        result = client.call_method(_ACTION_CATALOG, {"method": method})
    except FrappeAPIError as exc:
        if not _catalog_missing(exc):
            return "POST"  # transient or permission error: do not pin it for 10 minutes
        result = None
    row = result[0] if isinstance(result, list) and result else result
    http = row.get("http") if isinstance(row, dict) else None
    verbs = {str(v).upper() for v in ([http] if isinstance(http, str) else http or [])}
    verb = "GET" if "GET" in verbs and "POST" not in verbs else "POST"
    _VERB_CACHE[method] = (time.monotonic(), verb)
    return verb


def _query_params(kwargs: dict) -> dict:
    """GET sends kwargs as query args: lists/dicts as JSON (the form Frappe parses), booleans
    as 1/0 (Frappe's cint reads "True" as 0)."""
    def enc(v: Any) -> Any:
        if isinstance(v, bool):
            return int(v)
        return json.dumps(v) if isinstance(v, (dict, list)) else v
    return {k: enc(v) for k, v in kwargs.items()}


def register(mcp: Any) -> None:
    @mcp.tool()
    def frappe_list_documents(
        doctype: str,
        fields: list[str] | None = None,
        filters: list | dict | None = None,
        limit: int = 20,
        start: int = 0,
        order_by: str | None = None,
    ) -> Any:
        """List documents of any doctype.

        filters are Frappe-style: a list of [field, operator, value] (e.g.
        [["is_firearm","=",1],["status","=","Active"]]) or a simple {field: value}
        dict. limit defaults to 20; pass limit=0 for all rows. Call
        frappe_describe_doctype first if unsure of field names."""
        return get_client().list_documents(
            doctype, fields=fields, filters=filters, limit=limit, start=start, order_by=order_by
        )

    @mcp.tool()
    def frappe_get_document(doctype: str, name: str) -> Any:
        """Fetch one document by name. For a Single/Settings doctype, name == doctype
        (e.g. doctype='RSR Settings', name='RSR Settings')."""
        return get_client().get_document(doctype, name)

    @mcp.tool()
    def frappe_describe_doctype(doctype: str) -> Any:
        """List a doctype's fields (custom fields included): fieldname, label,
        fieldtype, options, reqd. Fields flagged is_password=true can never be
        written through this MCP — set those in Desk."""
        out = []
        for f in get_client().describe_doctype(doctype):
            ft = f.get("fieldtype")
            if ft in ("Section Break", "Column Break", "Tab Break", "HTML", "Button"):
                continue
            out.append({
                "fieldname": f.get("fieldname"),
                "label": f.get("label"),
                "fieldtype": ft,
                "options": f.get("options"),
                "reqd": f.get("reqd"),
                "is_password": ft == "Password",
            })
        return out

    @mcp.tool()
    def frappe_create_document(doctype: str, values: dict) -> Any:
        """Create a document. Credential/password fields are stripped automatically.
        A few fields are refused outright rather than stripped — see
        frappe_update_document."""
        check_write_allowed(doctype)
        # BEFORE strip_passwords, always: afterwards the credential fields are
        # already gone and the refusal would quietly become a strip.
        check_fields_writable(doctype, values)
        clean, stripped = strip_passwords(doctype, values)
        return stripped_note(get_client().create_document(doctype, clean), stripped)

    @mcp.tool()
    def frappe_update_document(doctype: str, name: str, values: dict) -> Any:
        """Update fields on a document. Credential/password fields are stripped
        automatically (set those in Desk). Some fields are refused outright and
        fail the whole call: on GunBroker Settings the environment switches
        (sandbox_mode, enabled, base_url_override), the credentials and the money
        fields — those decide whether a real firearm reaches the real
        marketplace, so they are a Desk change by a person."""
        check_write_allowed(doctype)
        check_fields_writable(doctype, values)  # before strip_passwords
        clean, stripped = strip_passwords(doctype, values)
        return stripped_note(get_client().update_document(doctype, name, clean), stripped)

    @mcp.tool()
    def frappe_delete_document(doctype: str, name: str, confirm: bool = False) -> Any:
        """Delete a document. Requires confirm=true. Note that
        ffl_core.api.item_admin.force_delete does NOT keep the A&D trail: it deletes
        non-submitted acquisitions/dispositions and raw-deletes ledger rows."""
        check_write_allowed(doctype)
        require_confirm(f"delete {doctype} {name}", confirm)
        get_client().delete_document(doctype, name)
        return {"deleted": name, "doctype": doctype}

    @mcp.tool()
    def frappe_submit_document(doctype: str, name: str, confirm: bool = False) -> Any:
        """Submit a draft document (docstatus 0 -> 1). Requires confirm=true —
        submitting can fire side effects (e.g. a FastBound push on FFL Acquisition)."""
        check_write_allowed(doctype)
        require_confirm(f"submit {doctype} {name}", confirm)
        return get_client().submit_document(doctype, name)

    @mcp.tool()
    def frappe_cancel_document(doctype: str, name: str, confirm: bool = False) -> Any:
        """Cancel a submitted document (docstatus 1 -> 2). Requires confirm=true."""
        check_write_allowed(doctype)
        require_confirm(f"cancel {doctype} {name}", confirm)
        return get_client().cancel_document(doctype, name)

    @mcp.tool()
    def frappe_run_method(method: str, kwargs: dict | None = None, confirm: bool = False) -> Any:
        """Call any whitelisted server method by dotted path, e.g.
        'osa_dropship.listing_sync.sync_listings'. list_pos_actions finds the method,
        its arguments, and whether it needs confirm. Generic Frappe mutators
        (frappe.client.set_value/insert/save/delete/bulk_update/...) are refused —
        use the structured frappe_*_document tools, which enforce the guards.
        Also refused: credential, signing-chain, webhook and audit endpoints, the
        legacy ffl_integrations.payroc.api aliases (call payments.api), and calls a
        dedicated tool does safely (receive_from_do -> distributor_receive,
        backfill_condition_attributes -> woo_store_maintenance, request_agreement
        without deliver email/tablet/none -> agreement_request,
        sync_in_stock_from_boundbook applied with no item_ids -> boundbook_reconcile).
        Methods whose name implies a destructive action — or that are on the
        explicit high-consequence list — require confirm=true. GET-only endpoints
        (per the action catalog) are sent as GET with kwargs as query arguments."""
        # run_method is the escape hatch around the structured CRUD guards, so
        # the never-writable rule has to be enforced here too — by field name,
        # since there is no document body to inspect.
        check_method_call_writable(method, kwargs)
        if method in _BLOCKED_GENERIC_METHODS:
            raise WriteRefused(
                f"Refused: '{method}' is a generic Frappe mutator that would bypass the "
                "MCP write guards (denylist, password-strip, confirm). Use the structured "
                "tools instead: frappe_create_document / frappe_update_document / "
                "frappe_delete_document / frappe_submit_document / frappe_cancel_document."
            )
        blocked = _blocked_reason(method)
        if blocked:
            raise WriteRefused(f"Refused: '{method}': {blocked}.")
        refusal = _argument_refusal(method, kwargs or {})
        if refusal:
            raise WriteRefused(f"Refused: '{method}' through frappe_run_method: {refusal}")
        if _needs_confirm(method):
            require_confirm(f"run_method {method}", confirm)
        client = get_client()
        if _method_verb(client, method) == "GET":
            return client.get_method(method, _query_params(kwargs or {}))
        return client.call_method(method, kwargs)

    @mcp.tool()
    def frappe_run_report(report_name: str, filters: dict | list | None = None) -> Any:
        """Run a Script/Query Report (e.g. 'Firearms In Stock') and return its
        columns + result rows."""
        return get_client().run_report(report_name, filters)

    @mcp.tool()
    def list_pos_actions(
        query: str | None = None,
        module: str | None = None,
        method: str | None = None,
        include_blocked: bool = False,
        limit: int = 25,
    ) -> Any:
        """Search every POS server endpoint the signed-in user can run (core apps plus
        installed extensions): summary, arguments, HTTP verb, roles, and whether
        frappe_run_method will ask for confirm. Use it when no dedicated tool fits, then
        call the method with frappe_run_method.

        query: words matched against the dotted path, summary and argument names.
        module: a dotted prefix, e.g. 'ffl_integrations.gunbroker'.
        method: one exact dotted path -> that endpoint's full row (description, Returns
        text, every argument's description).
        limit: rows returned, 1-100 (default 25).

        Each row: method, summary, args [{name, type, required}], http, roles or perm,
        runnable (true, or 'checked at call time' when the POS decides per call),
        confirm (frappe_run_method needs confirm=true). A row with use_tool names the
        dedicated tool that does the job safely; prefer it. A row with note warns about
        argument-dependent refusals. A row with switch names the env switch that
        registers its dedicated tool. Credential, signing-chain, webhook and
        legacy-alias endpoints are never listed (include_blocked=true shows them with
        the reason in blocked; frappe_run_method still refuses them)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _ACTIONS_MAX:
            raise ValueError(f"limit must be a whole number from 1 to {_ACTIONS_MAX} (got {limit!r}).")
        query = (query or "").strip() or None
        module = (module or "").strip().rstrip(".") or None
        method = (method or "").strip() or None
        if method and (query or module):
            raise ValueError("Pass method alone (one endpoint), or query/module to search.")
        payload: dict[str, Any] = {}
        if method:
            payload["method"] = method
        else:
            if query:
                payload["query"] = query
            if module:
                payload["module"] = module
            # Ask for the full page: blocked rows are dropped here, after the POS ranks.
            payload["limit"] = _ACTIONS_MAX
        try:
            result = get_client().call_method(_ACTION_CATALOG, payload)
        except FrappeAPIError as exc:
            if _catalog_missing(exc):
                raise RuntimeError(
                    "This POS has no action catalog (ffl_core.api.action_catalog); upgrade "
                    "the POS to list its endpoints. Dedicated tools and frappe_run_method "
                    "still work.") from exc
            raise
        if method:
            if not result:
                return {"action": None,
                        "note": f"No endpoint '{method}' that you can run on this POS."}
            row = result[0] if isinstance(result, list) else result
            return {"action": _annotate(row)}
        rows = [_annotate(r) for r in (result or []) if isinstance(r, dict)]
        if not include_blocked:
            rows = [r for r in rows if "blocked" not in r]
        return {"actions": rows[:limit], "count": min(len(rows), limit),
                "truncated": len(rows) > limit}
