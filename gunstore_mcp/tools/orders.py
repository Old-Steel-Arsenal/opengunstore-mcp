"""Counter orders and register tickets: the Create Order page, the Pending Order buttons, the
register's draft tickets (POS Invoices), counter card charges, cards on file and walk-in trade-ins.

Every POS method wrapped here was checked against gunstore-pos (ffl_core.api.manual_order,
counter_pickup, pos_order_status, trade_in, price_approval; ffl_integrations.payments.api and
saved_cards; distributor.api.ensure_dropship_item). Two are NEW POS endpoints this module is
written against before they ship (design.pos_side_changes):

* ``ffl_core.api.pos_ticket.open_ticket(customer, items, company=None)`` →
  ``{pos_invoice, grand_total, payment_modes}`` (pos_ticket action='open');
* ``ffl_integrations.payments.api.card_tender_mode()`` → ``{integrated}`` (pos_ticket tender /
  start_card: a Credit Card tender goes through the terminal whenever a processor is live).

On a POS without them those two actions fail with the POS's own "not found" error; every other
action works on the current POS.

Split by consequence:

* reads — ``preview_order``, ``pos_draft_orders``, ``saved_cards``; registered in full mode and
  on the remote connector, never on the cpa surface (they read customers and open tickets).
* writes — every one needs ``confirm=true``, except two harmless steps: ``create_order`` with
  ``save_as_draft=true`` (a shared draft that reserves nothing) and the card polls
  (``pos_ticket`` poll_card, ``card_terminal_charge`` poll), which only read the terminal.

What these tools never do: approve a price for someone else (a manager's PIN never passes
through the MCP), dispose a gun (``dispose_order`` / the 4473 do that) or take a trade-in as
store credit inside a cart.
"""
from __future__ import annotations

import json
import time
from typing import Any, Literal

from ..frappe_client import get_client
from ..safety import WriteRefused, require_confirm, require_reason

_MO = "ffl_core.api.manual_order."
_PAY = "ffl_integrations.payments.api."
_SC = "ffl_integrations.payments.saved_cards."
_TI = "ffl_core.api.trade_in."
_PICKUP = "ffl_core.api.counter_pickup.open_pickup_invoice"
_PRICE_CHECK = "ffl_core.price_approval.check"
_DRAFT_STATUS = "ffl_core.api.pos_order_status.draft_order_status"   # GET-only
_ENSURE_ITEM = "ffl_integrations.distributor.api.ensure_dropship_item"
_OPEN_TICKET = "ffl_core.api.pos_ticket.open_ticket"                 # NEW POS endpoint
_CARD_MODE = _PAY + "card_tender_mode"                                # NEW POS endpoint

# Every write a tool here reaches. Each must also be confirm-gated on the bare
# frappe_run_method path (generic._ALWAYS_CONFIRM_METHODS or the verb regex).
WRITE_METHODS = frozenset({
    _MO + "create_order", _MO + "save_cr_ffl", _ENSURE_ITEM, _MO + "update_order",
    _MO + "fulfill_from_stock", _MO + "resource_line", _PICKUP, _MO + "resend_order_agreement",
    _OPEN_TICKET, _PAY + "charge_pos_invoice", _PAY + "cancel_pos_charge",
    _PAY + "refund_pos_invoice", _PAY + "charge_sales_invoice", _PAY + "cancel_sales_invoice_charge",
    _SC + "charge_saved_card", _TI + "create_trade_in_intake",
})

_SOURCES = ("stock", "dropship", "to_store")
_ORDERED = ("dropship", "to_store")
_LINE_KEYS = frozenset({"item_code", "sku", "qty", "rate", "serial", "source", "distributor",
                        "storage_location"})
# What get_order_for_edit puts on a line, accepted back unchanged by update_order.
_EDIT_LINE_KEYS = frozenset({"serials", "item_name", "is_firearm", "ordered_do",
                             "dropship_distributor", "channel", "channel_confirmed"})
_PAYMENT_KEYS = frozenset({"mode_of_payment", "amount", "transaction_number"})
_ORDER_MODES = ("Cash", "Credit Card", "Zelle", "ACH", "Check/Money Order")
_REFERENCED_BANK_MODES = ("Zelle", "ACH", "Check/Money Order")   # manual_order._REFERENCED_BANK_MODES
_CR_KEYS = frozenset({"license_number", "license_expiry", "contact_name", "premise_street",
                      "premise_street2", "premise_city", "premise_state", "premise_zip"})
_CR_REQUIRED = ("license_number", "license_expiry", "contact_name", "premise_street",
                "premise_city", "premise_state", "premise_zip")
# update_order: the create_order payload keys an edit may change (they ARE the payload keys).
_UPDATE_KEYS = frozenset({
    "customer", "items", "buyer_type", "destination_ffl", "pickup_in_store", "ship_to",
    "recipient_name", "recipient_phone", "contact_email", "shipping_amount", "tax_exempt",
    "tax_certificate", "le_excise_exempt", "le_exempt_buyer", "le_exempt_certificate",
    "distributor_options", "notes", "pos_profile",
})
_BOOL_KEYS = frozenset({"pickup_in_store", "tax_exempt", "le_excise_exempt"})

# Register tickets.
_TENDER_MODES = ("Cash", "Zelle", "Check/Money Order", "ACH", "Credit Card")
_TENDER_NEEDS_REFERENCE = ("Zelle", "Check/Money Order")   # compliance.payment_mode
_CARD = "Credit Card"
_REFUND_LOCK_UNKNOWN = "UNKNOWN-OUTCOME"      # payments.api.REFUND_LOCK_UNKNOWN
_REFUND_LOCK_PARTIAL = "PARTIAL:"             # payments.api.REFUND_LOCK_PARTIAL_PREFIX
_TICKET_LINE_KEYS = frozenset({"item_code", "qty", "rate", "serial_no", "storage_location"})
_DRAFT_FIELDS = ["name", "customer", "customer_name", "posting_date", "owner", "grand_total",
                 "pos_profile"]
_DRAFT_MAX = 100   # pos_order_status.MAX_INVOICES
_POLL_MAX = 25     # seconds one poll call may wait for the terminal
_POLL_EVERY = 2.0
_CENT = 0.005

# Trade-ins.
_ID_TYPES = ("Driver License", "State ID", "Passport", "Military ID", "Other")
_PAYOUTS = ("Cash", "Zelle", "Check", "ACH")   # cash_drawer.PAYOUT_METHODS
_FIREARM_TYPES = frozenset({
    "Pistol", "Revolver", "Rifle", "Shotgun", "Receiver", "Other Firearm", "Silencer",
    "Short Barreled Rifle", "Short Barreled Shotgun", "Machine Gun", "Destructive Device",
    "Any Other Weapon"})
_CONDITIONS = frozenset({"New", "Excellent", "Very Good", "Good", "Fair"})
_GUN_REQUIRED = ("serial_no", "manufacturer", "model_name", "caliber", "firearm_type",
                 "condition_grade", "acquisition_cost")
_GUN_KEYS = frozenset(_GUN_REQUIRED) | {"retail_price", "importer", "barrel_length", "finish",
                                        "year_made", "item_code", "storage_location"}
_SELLER_NAME = ("source_first_name", "source_middle_name", "source_last_name", "source_suffix")
_SELLER_ADDRESS = ("source_address_line1", "source_city", "source_state", "source_zip")


def _f(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _unknown(what: str, given, allowed) -> None:
    extra = sorted(set(given) - set(allowed))
    if extra:
        raise ValueError(f"{what}: unknown key(s) {extra}; allowed: {sorted(allowed)}.")


def _order_line(raw: Any, idx: int, *, edit: bool = False) -> dict:
    """One create_order line in the payload's own shape. ``serial`` becomes ``serials``;
    source 'stock' is the payload's blank source. With ``edit``, the keys get_order_for_edit
    adds to a line are accepted back as they are."""
    if not isinstance(raw, dict):
        raise ValueError(f"items[{idx}] must be an object.")
    _unknown(f"items[{idx}]", raw, _LINE_KEYS | (_EDIT_LINE_KEYS if edit else set()))
    item_code = str(raw.get("item_code") or "").strip()
    sku = str(raw.get("sku") or "").strip()
    source = str(raw.get("source") or "stock").strip().lower()
    distributor = str(raw.get("distributor") or raw.get("dropship_distributor") or "").strip()
    if source not in _SOURCES:
        raise ValueError(f"items[{idx}].source must be one of {list(_SOURCES)} (got {source!r}).")
    if not item_code and not sku:
        raise ValueError(f"items[{idx}] needs item_code (or, for a distributor line, sku).")
    if source in _ORDERED and not distributor:
        raise ValueError(f"items[{idx}] is a {source} line: name its distributor.")
    if source == "stock" and not item_code:
        raise ValueError(f"items[{idx}]: a line from stock needs item_code (sku is for "
                         "dropship / to_store lines).")
    if raw.get("rate") is None:
        raise ValueError(f"items[{idx}] needs rate (the price charged per unit).")
    serial = str(raw.get("serial") or "").strip()
    serials = raw.get("serials")
    if serial and serials:
        raise ValueError(f"items[{idx}]: pass serial or serials, not both.")
    if (serial or serials) and source != "stock":
        raise ValueError(f"items[{idx}]: only a line from stock carries a serial.")
    line = {k: v for k, v in raw.items() if k in _EDIT_LINE_KEYS} if edit else {}
    line.update({"item_code": item_code, "qty": raw.get("qty") or 1, "rate": raw.get("rate")})
    line["serials"] = [serial] if serial else list(serials or [])
    if source != "stock":
        line["source"] = source
        line["distributor"] = distributor
    if sku:
        line["sku"] = sku
    if raw.get("storage_location"):
        line["storage_location"] = raw["storage_location"]
    return line


def _order_lines(items, *, edit: bool = False) -> list[dict]:
    if not items or not isinstance(items, list):
        raise ValueError("items: give at least one order line.")
    return [_order_line(raw, i, edit=edit) for i, raw in enumerate(items)]


def _ensure_items(client, lines: list[dict]) -> list[dict]:
    """Give every sku-only distributor line its Item, as the Create Order page does: claim=1 for
    a drop-ship line, 0 for ship-to-store (that line buys into our stock and says nothing about
    who drop-ships the Item on the web). Returns what was ensured."""
    done = []
    for line in lines:
        if line["item_code"]:
            continue
        res = client.call_method(_ENSURE_ITEM, {
            "sku": line["sku"], "distributor": line["distributor"],
            "claim": 1 if line.get("source") == "dropship" else 0}) or {}
        if not res.get("item_code"):
            raise WriteRefused(f"The POS made no Item for SKU {line['sku']} ({res!r}); nothing "
                               "was ordered.")
        line["item_code"] = res["item_code"]
        done.append({"sku": line["sku"], "distributor": line["distributor"],
                     "item_code": res["item_code"]})
    return done


def _payment(payment: dict | None) -> dict:
    if not payment:
        return {}
    if not isinstance(payment, dict):
        raise ValueError("payment must be an object {mode_of_payment, amount?, transaction_number?}.")
    _unknown("payment", payment, _PAYMENT_KEYS)
    mode = payment.get("mode_of_payment")
    if mode not in _ORDER_MODES:
        raise ValueError(f"payment.mode_of_payment must be one of {list(_ORDER_MODES)}.")
    ref = str(payment.get("transaction_number") or "").strip()
    if mode in _REFERENCED_BANK_MODES and not ref:
        raise ValueError(f"payment.transaction_number is required for {mode}.")
    out = {"payment_received": 1, "mode_of_payment": mode, "transaction_number": ref or None}
    if payment.get("amount") is not None:
        out["paid_amount"] = payment["amount"]
    return out


def _cr_license(cr: dict) -> dict:
    if not isinstance(cr, dict):
        raise ValueError("cr_license must be an object.")
    _unknown("cr_license", cr, _CR_KEYS)
    missing = [k for k in _CR_REQUIRED if not str(cr.get(k) or "").strip()]
    if missing:
        raise ValueError(f"cr_license is missing {missing}.")
    return cr


def _total(doc: dict) -> float:
    return round(_f(doc.get("rounded_total")) or _f(doc.get("grand_total")), 2)


def _payment_row(doc: dict, mode: str) -> dict:
    rows = doc.get("payments") or []
    row = next((r for r in rows if r.get("mode_of_payment") == mode), None)
    if row is None:
        have = [r.get("mode_of_payment") for r in rows]
        raise WriteRefused(f"{doc.get('name')}: {mode} is not a payment method of this register "
                           f"(it takes {have}). Add it to the POS Profile in Desk.")
    return row


def _put_payments(client, doc: dict, changes: dict[str, dict]) -> Any:
    """Rewrite the whole payments table. REST replaces a child table with what it is sent, so
    every row goes back whole (account, type, default...), with only ``changes`` applied."""
    rows = [{**r, **changes.get(r.get("mode_of_payment"), {})} for r in doc.get("payments") or []]
    return client.update_document("POS Invoice", doc["name"], {"payments": rows})


def _is_refund_lock(value: str | None) -> bool:
    v = value or ""
    return v == _REFUND_LOCK_UNKNOWN or v.startswith(_REFUND_LOCK_PARTIAL)


def _in_progress(res: Any) -> bool:
    return isinstance(res, dict) and not res.get("ok") and res.get("status") == "IN_PROGRESS"


def _poll(fn, wait_seconds: int) -> Any:
    """Poll once, or keep polling every 2 s for up to wait_seconds (max 25) while the
    terminal still holds the charge."""
    deadline = time.monotonic() + max(0, min(int(wait_seconds or 0), _POLL_MAX))
    while True:
        res = fn()
        left = deadline - time.monotonic()
        if not _in_progress(res) or left <= 0:
            return res
        time.sleep(min(_POLL_EVERY, left))


def _only(action: str, given: dict, allowed: tuple = (), required: tuple = ()) -> None:
    """Refuse an argument that does not belong to ``action`` (it would be silently ignored),
    and a missing one it needs."""
    extra = sorted(k for k, v in given.items() if v not in (None, False, 0, "") and k not in allowed)
    if extra:
        raise ValueError(f"{extra} do(es) not apply to action={action!r}; it takes {list(allowed)}.")
    missing = [k for k in required if given.get(k) in (None, "")]
    if missing:
        raise ValueError(f"action={action!r} needs {missing}.")


def _skip_note(res: Any, how: str) -> Any:
    if isinstance(res, dict) and res.get("skip"):
        return {**res, "note": "No card processor is switched on and configured, so nothing was "
                               f"charged. Take the card on the standalone terminal, then {how}."}
    return res


def _card_integrated(client) -> bool:
    """card_tender_mode (NEW POS endpoint): is a card processor live with a device?"""
    return bool((client.call_method(_CARD_MODE) or {}).get("integrated"))


def _waiting_instruction(doc: dict) -> str:
    waiting = [c.get("instruction_id") for c in doc.get("card_charges") or []
               if c.get("status") == "IN_PROGRESS"]
    if len(waiting) != 1:
        raise WriteRefused(f"{doc.get('name')} has {len(waiting)} card charge(s) waiting; "
                           "pass instruction_id.")
    return waiting[0]


def register(mcp: Any) -> None:
    # ===================================================== counter orders

    @mcp.tool()
    def preview_order(
        sales_invoice: str | None = None, customer: str | None = None,
        items: list[dict] | None = None, le_excise_exempt: bool = False,
        include_costs: bool = True,
    ) -> Any:
        """Check a counter, phone or transfer order before create_order: whether each typed
        serial is free (not sold, held or reserved) and its tag price, each line's cost, the tax
        rate per item, whether a price below list needs a manager, the customer's standing tax
        exemption and the store's tax config. Read-only.

        items: the SAME shape as create_order — [{item_code, qty=1, rate, serial?,
        source: 'stock'|'dropship'|'to_store' (default stock), distributor? (dropship/to_store),
        sku?, storage_location?}]. A sku-only line (no Item yet) is checked for nothing but its
        cost. Returns {lines: [{item_code, serial, rate, ok, reason, tag_price, cost, tax_rate}],
        price_approval: {needs_approval, approver (true = the connected user is a manager, whose
        own order needs no one else), lines, discount, approvers}, customer_tax: {is_tax_exempt,
        certificate}, config: {home_state, rate, freight_taxable, shipping_tax_inclusive,
        le_exempt_offered, ...}}.

        sales_invoice ALONE returns that pending order's current editable form instead:
        {current: {payload (create_order-shaped — edit it and pass the changed keys to
        update_order), edit_ctx: {grand_total, paid_total, last_payment_mode, ...}},
        agreements: {email, rows: [{template, serial_no, state, ...}]}}. With items too, the
        lines are checked as an edit of that order (its own hold on a serial is not a clash)."""
        client = get_client()
        if sales_invoice and not items:
            if customer:
                raise ValueError("customer is only read with items (an edit preview).")
            return {
                "current": client.call_method(_MO + "get_order_for_edit",
                                              {"sales_invoice": sales_invoice}),
                "agreements": client.call_method(_MO + "order_agreements",
                                                 {"sales_invoice": sales_invoice}),
            }
        if not customer:
            raise ValueError("customer is required to preview an order.")
        lines = _order_lines(items, edit=bool(sales_invoice))
        payload = {"customer": customer, "items": lines}
        serials = [s for line in lines for s in line["serials"]]
        checked = client.call_method(_MO + "check_serials_available", {
            "serials": serials, "ignore_invoice": sales_invoice}) if serials else {}
        codes = sorted({line["item_code"] for line in lines if line["item_code"]})
        rates = client.call_method(_MO + "item_tax_rates", {
            "item_codes": codes, "excise_exempt": 1 if le_excise_exempt else 0}) if codes else {}
        costs = client.call_method(_MO + "order_line_costs", {
            "items": lines, "sales_invoice": sales_invoice}) if include_costs else None
        out_lines = []
        for i, line in enumerate(lines):
            serial = line["serials"][0] if line["serials"] else None
            check = (checked or {}).get(serial) or {} if serial else {}
            out_lines.append({
                "item_code": line["item_code"] or None, "sku": line.get("sku"), "serial": serial,
                "rate": line["rate"],
                "ok": check.get("ok", True) if serial else True,
                "reason": check.get("reason"), "tag_price": check.get("sell_price"),
                "cost": costs[i] if isinstance(costs, list) and i < len(costs) else None,
                "tax_rate": (rates or {}).get(line["item_code"]),
            })
        return {
            "lines": out_lines,
            "price_approval": client.call_method(_PRICE_CHECK, {
                "payload": payload, "sales_invoice": sales_invoice}),
            "customer_tax": client.call_method(_MO + "get_customer_tax_status",
                                               {"customer": customer}),
            "config": client.call_method(_MO + "quote_config"),
        }

    @mcp.tool()
    def create_order(
        customer: str, items: list[dict],
        buyer_type: Literal["individual", "business"] = "individual",
        destination_ffl: str | None = None, cr_license: dict | None = None,
        pickup_in_store: bool = False, ship_to: dict | None = None,
        recipient_name: str | None = None, recipient_phone: str | None = None,
        contact_email: str | None = None, shipping_amount: float = 0,
        payment: dict | None = None, tax_exempt: bool | None = None,
        tax_certificate: str | None = None, le_excise_exempt: bool = False,
        le_exempt_buyer: str | None = None, le_exempt_certificate: str | None = None,
        distributor_options: dict | None = None, notes: str | None = None,
        pos_profile: str | None = None, from_draft: str | None = None,
        save_as_draft: bool = False, draft_name: str | None = None, title: str | None = None,
        confirm: bool = False,
    ) -> Any:
        """Take a counter, phone or dealer transfer order (the Create Order page). It books a
        Sales Order and Sales Invoice and, with payment, the payment; the gun leaves the bound
        book later through dispose_order (or, for a pickup, the register ticket and its 4473).
        Run preview_order first and settle every line it flags.

        items: [{item_code, qty=1, rate, serial? (one in-stock serial per firearm line),
        source: 'stock' (default) | 'dropship' (the distributor ships to the buyer) | 'to_store'
        (it ships to us), distributor (required for dropship / to_store), sku? (a distributor SKU
        with no Item yet: the Item is made on create), storage_location?}].
        A firearm order needs destination_ffl (an ATF FFL Record) or cr_license
        {license_number, license_expiry, contact_name, premise_street, premise_street2?,
        premise_city, premise_state, premise_zip} for a C&R (03) collector — read off the license
        copy; the record is made on create. pickup_in_store=true: the customer collects here (no
        ship-to; a gun only as a to_store line, handed over on a register ticket with its 4473).
        ship_to: {address_line1, address_line2?, city, state, pincode} for a non-firearm order.
        payment: {mode_of_payment: Cash | Credit Card | Zelle | ACH | Check/Money Order, amount?
        (blank = the full total; less = a deposit), transaction_number? (required for Zelle,
        ACH, Check/Money Order; for Credit Card the approval code)} — to charge the counter
        terminal instead, create without payment and run card_terminal_charge.
        tax_exempt: true/false also sets the customer's standing exemption; omitted = the
        customer's standing flag. le_excise_exempt (a peace officer's sale) needs le_exempt_buyer
        and le_exempt_certificate. distributor_options: {'<distributor>|<order_type>|<0|1>':
        {signature, adult_signature, insurance}} as distributor_fulfillment_options gives them.
        from_draft: a Create Order Draft this order finishes (deleted once the order exists).

        A price below list is booked only when the connected user is a manager; otherwise the
        POS refuses and a manager approves on the counter screen. Returns {sales_order,
        sales_invoice, paid_amount, outstanding, agreements (links emailed for unsigned
        agreements), ...}. confirm=true.

        save_as_draft=true only saves a shared draft staff can finish on the page (draft_name
        updates one; title names it): nothing is created, reserved or charged, sku-only lines are
        stored as typed (the page does not reload a C&R license: re-enter it when finishing),
        and no confirm is needed."""
        lines = _order_lines(items)
        if destination_ffl and cr_license:
            raise ValueError("Give destination_ffl or cr_license, not both.")
        if cr_license is not None:
            cr_license = _cr_license(cr_license)
        if (draft_name or title) and not save_as_draft:
            raise ValueError("draft_name and title only apply with save_as_draft=true.")
        if save_as_draft and from_draft:
            raise ValueError("from_draft finishes a draft; to update one, pass draft_name.")
        payload: dict[str, Any] = {
            "customer": customer, "buyer_type": buyer_type, "items": lines,
            "destination_ffl": destination_ffl,
            "pickup_in_store": 1 if pickup_in_store else 0,
            "ship_to": ship_to or {}, "recipient_name": recipient_name,
            "recipient_phone": recipient_phone, "contact_email": contact_email,
            "shipping_amount": shipping_amount, "notes": notes, "pos_profile": pos_profile,
            **_payment(payment),
        }
        if tax_exempt is not None:   # the key's PRESENCE sets the customer's standing flag
            payload["tax_exempt"] = 1 if tax_exempt else 0
            payload["tax_certificate"] = tax_certificate
        elif tax_certificate:
            raise ValueError("tax_certificate goes with tax_exempt=true.")
        if le_excise_exempt:
            payload.update({"le_excise_exempt": 1, "le_exempt_buyer": le_exempt_buyer,
                            "le_exempt_certificate": le_exempt_certificate})
        elif le_exempt_buyer or le_exempt_certificate:
            raise ValueError("le_exempt_buyer / le_exempt_certificate go with le_excise_exempt=true.")
        if distributor_options:
            payload["distributor_options"] = distributor_options
        payload = {k: v for k, v in payload.items() if v is not None}
        client = get_client()

        if save_as_draft:
            if cr_license:
                payload["cr_details"] = cr_license
            return client.call_method(_MO + "save_order_draft", {
                "payload": payload, "name": draft_name, "title": title})

        require_confirm(f"create_order {customer} ({len(lines)} line(s))", confirm)
        extra: dict[str, Any] = {}
        if cr_license:
            rec = client.call_method(_MO + "save_cr_ffl", {"cr_details": cr_license}) or {}
            payload["destination_ffl"] = rec.get("name")
            extra["destination_ffl"] = rec.get("name")
        ensured = _ensure_items(client, lines)
        if ensured:
            extra["ensured_items"] = ensured
        if from_draft:
            payload["draft"] = from_draft
        res = client.call_method(_MO + "create_order", {"payload": payload})
        return {**res, **extra} if isinstance(res, dict) and extra else res

    @mcp.tool()
    def update_order(
        sales_invoice: str, changes: dict, refund_mode: str | None = None,
        refund_reference: str | None = None, confirm: bool = False,
    ) -> Any:
        """Edit a pending counter order (price, swap a gun, ship-to, add or drop a line, buyer).
        The tool reads the order's current form from the POS (preview_order(sales_invoice=...)
        shows it), replaces only the top-level keys in `changes`, and sends the whole order back,
        so nothing left out is dropped.

        changes uses create_order's names: customer, items, buyer_type, destination_ffl,
        pickup_in_store, ship_to, recipient_name, recipient_phone, contact_email,
        shipping_amount, tax_exempt, tax_certificate, le_excise_exempt, le_exempt_buyer,
        le_exempt_certificate, distributor_options, notes, pos_profile. `items` replaces the
        WHOLE line list: start from the current payload's items (their extra keys such as
        ordered_do may stay), change what you need; a line already ordered from a distributor
        must come back with the same house, lane and quantity (only its price may change).
        A sku-only distributor line gets its Item made first, as in create_order.

        The POS cancels the invoice and rebuilds it in one transaction (all or nothing).
        Money follows the order; when the total drops below what was paid it refunds the
        difference by refund_mode (refund_reference needed for Zelle, ACH or Check/Money Order),
        and a changed customer is refunded in full. Returns the POS result {sales_invoice (the
        NEW invoice), replaced, grand_total, refunded_amount, outstanding, ...} plus
        changed_keys. confirm=true."""
        if not changes or not isinstance(changes, dict):
            raise ValueError("changes: give at least one key to change.")
        if "payment" in changes or "cr_license" in changes:
            raise ValueError("An edit takes no payment (use record_payment / card_terminal_charge) "
                             "and no cr_license (record it with create_order's C&R path, or pass "
                             "its ATF FFL Record as destination_ffl).")
        _unknown("changes", changes, _UPDATE_KEYS)
        overlay = dict(changes)
        if "items" in overlay:
            overlay["items"] = _order_lines(overlay["items"], edit=True)
        for k in _BOOL_KEYS & overlay.keys():
            overlay[k] = 1 if overlay[k] else 0
        require_confirm(f"update_order {sales_invoice} ({', '.join(sorted(changes))})", confirm)
        client = get_client()
        current = client.call_method(_MO + "get_order_for_edit", {"sales_invoice": sales_invoice})
        payload = dict((current or {}).get("payload") or {})
        if not payload:
            raise WriteRefused(f"The POS returned no editable form for {sales_invoice}; nothing "
                               "was changed.")
        if "items" in overlay:
            _ensure_items(client, overlay["items"])
        payload.update(overlay)
        res = client.call_method(_MO + "update_order", {
            "sales_invoice": sales_invoice, "payload": payload,
            "refund_mode": refund_mode, "refund_reference": refund_reference})
        keys = sorted(changes)
        return {**res, "changed_keys": keys} if isinstance(res, dict) \
            else {"result": res, "changed_keys": keys}

    @mcp.tool()
    def pending_order_action(
        sales_invoice: str,
        action: Literal["fulfill_from_stock", "resource_line", "open_pickup", "resend_agreement"],
        row_name: str | None = None, serial_no: str | None = None,
        distributor: str | None = None, template: str | None = None,
        email: str | None = None, confirm: bool = False,
    ) -> Any:
        """The Pending Order buttons pending_orders shows but no other tool runs:

        * fulfill_from_stock (row_name; serial_no for a gun): serve a late ship-to-store line
          from a unit on the shelf. The distributor's order is not cancelled; its parcel books
          as plain restock when it lands.
        * resource_line (row_name, distributor): order a stalled ship-to-store line again from
          another house — creates a Draft Distributor Order only (placed from the queue as usual;
          phone the old house, it has no cancel API).
        * open_pickup: open (or return) the $0 register ticket that hands over an arrived gun
          of a pickup order. Returns {pos_invoice, already, serial_by_item_row}; next run
          start_4473(pos_invoice, serial_by_item_row), then pos_ticket(action='submit').
        * resend_agreement (template; serial_no for an each-firearm agreement; email? to send
          elsewhere): email the buyer a fresh signing link; the old one stops working.

        row_name is the Sales Invoice Item row from pending_orders (legs.substitutable /
        legs.trouble). Arguments that do not belong to the action are refused. confirm=true."""
        given = {"row_name": row_name, "serial_no": serial_no, "distributor": distributor,
                 "template": template, "email": email}
        spec = {
            "fulfill_from_stock": (("row_name", "serial_no"), ("row_name",)),
            "resource_line": (("row_name", "distributor"), ("row_name", "distributor")),
            "open_pickup": ((), ()),
            "resend_agreement": (("template", "serial_no", "email"), ("template",)),
        }
        if action not in spec:
            raise ValueError(f"action must be one of {list(spec)}.")
        _only(action, given, *spec[action])
        require_confirm(f"pending_order_action {action} {sales_invoice}", confirm)
        client = get_client()
        if action == "fulfill_from_stock":
            return client.call_method(_MO + "fulfill_from_stock", {
                "sales_invoice": sales_invoice, "row_name": row_name, "serial_no": serial_no})
        if action == "resource_line":
            return client.call_method(_MO + "resource_line", {
                "sales_invoice": sales_invoice, "row_name": row_name, "distributor": distributor})
        if action == "resend_agreement":
            return client.call_method(_MO + "resend_order_agreement", {
                "sales_invoice": sales_invoice, "template": template, "serial_no": serial_no,
                "email": email})
        res = client.call_method(_PICKUP, {"sales_invoice": sales_invoice}) or {}
        ticket = client.get_document("POS Invoice", res["pos_invoice"]) if res.get("pos_invoice") else {}
        return {"pos_invoice": res.get("pos_invoice"), "already": bool(res.get("already")),
                "serial_by_item_row": {r["name"]: r["serial_no"] for r in ticket.get("items") or []
                                       if (r.get("serial_no") or "").strip()}}

    # ===================================================== register tickets

    @mcp.tool()
    def pos_draft_orders(
        customer: str | None = None, pos_profile: str | None = None,
        only_firearm: bool = False, limit: int = 50,
    ) -> Any:
        """The tickets parked at the register (draft POS Invoices, newest first) with what each
        still needs: due, paid_amount, card_status / charged, held, paid_online (a paid web
        order), payments, counter_order (a counter pickup: its Sales Invoice's total and
        outstanding — the ticket itself is $0), has_firearm, form_4473_status, nics_status.
        Use it to find the ticket to finish with start_4473 and pos_ticket. only_firearm keeps
        the tickets with a gun (filtered within the page read). limit ≤ 100. Read-only."""
        client = get_client()
        filters: list = [["docstatus", "=", 0], ["is_return", "=", 0]]
        if customer:
            filters.append(["customer", "=", customer])
        if pos_profile:
            filters.append(["pos_profile", "=", pos_profile])
        rows = client.list_documents(
            "POS Invoice", fields=_DRAFT_FIELDS, filters=filters,
            limit=max(1, min(int(limit), _DRAFT_MAX)), order_by="modified desc") or []
        if not rows:
            return []
        status = client.get_method(_DRAFT_STATUS, {
            "invoices": json.dumps([r["name"] for r in rows])}) or {}
        out = [{**r, **(status.get(r["name"]) or {})} for r in rows]
        return [r for r in out if r.get("has_firearm")] if only_firearm else out

    @mcp.tool()
    def pos_ticket(
        action: Literal["open", "tender", "clear_tender", "start_card", "poll_card",
                        "cancel_card", "submit"],
        invoice_name: str | None = None, customer: str | None = None,
        items: list[dict] | None = None,
        mode_of_payment: Literal["Cash", "Zelle", "Check/Money Order", "ACH",
                                 "Credit Card"] | None = None,
        amount: float | None = None, reference: str | None = None,
        terminal: str | None = None, save_card: bool = False,
        instruction_id: str | None = None, wait_seconds: int = 0,
        reason: str | None = None, confirm: bool = False,
    ) -> Any:
        """Finish a register ticket (draft POS Invoice): a gunsmith pickup with a balance, a
        transfer fee, a counter or web pickup, a gun sale held for its 4473, or a return.

        * open (customer, items [{item_code, qty=1, rate?, serial_no?, storage_location?}]): a
          new ticket on the open register shift (refused without one). Returns {pos_invoice,
          grand_total, payment_modes}.
        * tender (mode_of_payment, amount?, reference?): set what the customer pays by that
          method. amount blank = what is still due; more than due only for Cash (the change is
          returned). reference is required for Zelle and Check/Money Order. Credit Card here is
          only for a store with no card processor live (reference = the standalone terminal's
          approval code); with one live, use start_card.
        * clear_tender (mode_of_payment): set that method back to 0.
        * start_card (amount? = blank: what is still due; terminal?; save_card?): charge the
          counter terminal; the customer taps or inserts. Returns an instruction_id. A second
          start returns the charge already waiting instead of charging twice.
        * poll_card (instruction_id?, wait_seconds 0–25): ask the terminal; repeat until
          ok=true (approved, recorded on the ticket) or a final status. No confirm needed.
        * cancel_card (instruction_id?): stop a charge still waiting (an approval that won the
          race is recorded instead). instruction_id blank = the ticket's one waiting charge.
        * submit (reason? — required for a return with a card part): book the sale. The POS runs
          every check (4473 and NICS for guns, paid in full, card charged = card tender, price
          approval, banned customer). On a return the card part is first refunded to the
          original card; a refund the processor never confirmed stops here (settle it with
          resolve_card_reversal), and a failed submit never refunds twice.

        invoice_name is needed by every action except open. Arguments that do not belong to the
        action are refused. confirm=true for everything except poll_card."""
        given = {"invoice_name": invoice_name, "customer": customer, "items": items,
                 "mode_of_payment": mode_of_payment, "amount": amount, "reference": reference,
                 "terminal": terminal, "save_card": save_card, "instruction_id": instruction_id,
                 "wait_seconds": wait_seconds, "reason": reason}
        spec = {
            "open": (("customer", "items"), ("customer", "items")),
            "tender": (("invoice_name", "mode_of_payment", "amount", "reference"),
                       ("invoice_name", "mode_of_payment")),
            "clear_tender": (("invoice_name", "mode_of_payment"), ("invoice_name", "mode_of_payment")),
            "start_card": (("invoice_name", "amount", "terminal", "save_card"), ("invoice_name",)),
            "poll_card": (("invoice_name", "instruction_id", "wait_seconds"), ("invoice_name",)),
            "cancel_card": (("invoice_name", "instruction_id"), ("invoice_name",)),
            "submit": (("invoice_name", "reason"), ("invoice_name",)),
        }
        if action not in spec:
            raise ValueError(f"action must be one of {list(spec)}.")
        _only(action, given, *spec[action])
        if mode_of_payment is not None and mode_of_payment not in _TENDER_MODES:
            raise ValueError(f"mode_of_payment must be one of {list(_TENDER_MODES)}.")
        if amount is not None and amount <= 0 and action == "start_card":
            raise ValueError("amount must be more than 0.")
        if action == "tender" and mode_of_payment in _TENDER_NEEDS_REFERENCE \
                and not (reference or "").strip():
            raise ValueError(f"reference is required for a {mode_of_payment} tender (the "
                             "confirmation or check number).")
        if action == "open":
            if not items or not isinstance(items, list):
                raise ValueError("open needs items: at least one line.")
            ticket_lines = []
            for i, raw in enumerate(items or []):
                if not isinstance(raw, dict) or not str(raw.get("item_code") or "").strip():
                    raise ValueError(f"items[{i}] needs item_code.")
                _unknown(f"items[{i}]", raw, _TICKET_LINE_KEYS)
                ticket_lines.append({"qty": 1, **raw})
        if action != "poll_card":
            require_confirm(f"pos_ticket {action} {invoice_name or customer}", confirm)
        client = get_client()

        if action == "open":
            return client.call_method(_OPEN_TICKET, {"customer": customer, "items": ticket_lines})

        doc = client.get_document("POS Invoice", invoice_name) or {}
        if doc.get("docstatus") != 0:
            raise WriteRefused(f"{invoice_name} is not a draft ticket (docstatus "
                               f"{doc.get('docstatus')}); nothing was changed.")

        if action in ("poll_card", "cancel_card"):
            iid = instruction_id or _waiting_instruction(doc)
            args = {"invoice_name": invoice_name, "instruction_id": iid}
            if action == "cancel_card":
                return client.call_method(_PAY + "cancel_pos_charge", args)
            return _poll(lambda: client.call_method(_PAY + "poll_pos_charge", args), wait_seconds)

        if action == "clear_tender":
            _payment_row(doc, mode_of_payment)
            return _put_payments(client, doc, {mode_of_payment: {"amount": 0, "reference_no": None}})

        if action == "tender":
            if mode_of_payment == _CARD and _card_integrated(client):
                raise WriteRefused("A card processor is live: charge the card with "
                                   "pos_ticket(action='start_card'), never as a typed tender.")
            row = _payment_row(doc, mode_of_payment)
            total = _total(doc)
            others = sum(_f(r.get("amount")) for r in doc.get("payments") or [] if r is not row)
            due = round(total - others, 2)
            amt = due if amount is None else round(float(amount), 2)
            # lazy: a return's tender is taken as given (negative); only a sale's is range-checked.
            if not doc.get("is_return"):
                if amt <= 0:
                    raise WriteRefused(f"{invoice_name}: nothing is left to tender (due {due}).")
                if amt > due + _CENT and mode_of_payment != "Cash":
                    raise WriteRefused(f"{amt} is more than the {due} still due; only Cash may be "
                                       "more (change is given back).")
            res = _put_payments(client, doc, {mode_of_payment: {
                "amount": amt, "reference_no": (reference or "").strip() or None}})
            return {"invoice_name": invoice_name, "mode_of_payment": mode_of_payment,
                    "amount": amt, "due_before": due,
                    "change": round(max(0.0, amt - due), 2), "result": res}

        if action == "start_card":
            if not _card_integrated(client):
                raise WriteRefused("No card processor is live: take the card on the standalone "
                                   "terminal, then pos_ticket(action='tender', mode_of_payment="
                                   "'Credit Card', reference=<approval code>).")
            charges = doc.get("card_charges") or []
            args = {"invoice_name": invoice_name, "terminal": terminal,
                    "save_card": 1 if save_card else 0}
            if any(c.get("status") == "IN_PROGRESS" for c in charges):
                # The POS hands back the waiting charge instead of starting another.
                return client.call_method(_PAY + "charge_pos_invoice", {**args, "amount": None})
            card_row = _payment_row(doc, _CARD)
            charged = sum(_f(c.get("amount")) for c in charges if c.get("status") == "CHARGED")
            others = sum(_f(r.get("amount")) for r in doc.get("payments") or [] if r is not card_row)
            left = round(_total(doc) - others - charged, 2)
            amt = left if amount is None else round(float(amount), 2)
            if amt <= 0 or amt > left + _CENT:
                raise WriteRefused(f"{invoice_name}: the card can take {left} at most "
                                   f"(asked {amt}).")
            tender = round(charged + amt, 2)
            if abs(_f(card_row.get("amount")) - tender) > _CENT:
                # The terminal charges against the Credit Card tender: set it first.
                _put_payments(client, doc, {_CARD: {"amount": tender}})
            return _skip_note(client.call_method(_PAY + "charge_pos_invoice",
                                                 {**args, "amount": amt}),
                              "pos_ticket(action='tender', mode_of_payment='Credit Card', "
                              "reference=<approval code>)")

        # submit
        refund = None
        card_part = any(r.get("mode_of_payment") == _CARD and _f(r.get("amount"))
                        for r in doc.get("payments") or [])
        if doc.get("is_return") and card_part:
            reason = require_reason(f"pos_ticket submit {invoice_name} (card refund)", reason)
            ref = (doc.get("card_refund_reference") or "").strip()
            if _is_refund_lock(ref):
                raise WriteRefused(
                    f"{invoice_name}: the card refund's outcome is unknown ({ref}). Check the "
                    "processor's portal and settle it with resolve_card_reversal first; nothing "
                    "was submitted.")
            if not ref:
                refund = client.call_method(_PAY + "refund_pos_invoice",
                                            {"return_invoice_name": invoice_name, "reason": reason})
        try:
            submitted = client.submit_document("POS Invoice", invoice_name)
        except Exception as e:
            if refund is not None:
                raise WriteRefused(
                    f"The card refund went through ({refund!r}) but {invoice_name} did not "
                    f"submit: {e}. Fix that and submit again — the refund is recorded on the "
                    "ticket and will not run twice.") from e
            raise
        return {"submitted": submitted, "card_refund": refund} if refund is not None else submitted

    # ===================================================== counter-order card payments

    @mcp.tool()
    def card_terminal_charge(
        sales_invoice: str, action: Literal["start", "poll", "cancel"],
        amount: float | None = None, terminal: str | None = None, save_card: bool = False,
        instruction_id: str | None = None, wait_seconds: int = 0, confirm: bool = False,
    ) -> Any:
        """Charge a card on the counter terminal against a submitted counter order (Sales
        Invoice): a balance, a deposit or a membership fee. The customer taps or inserts at the
        counter.

        * start (amount? at most the balance, blank = all of it; terminal?; save_card? keeps
          the card on file when the customer signed for it): returns {instruction_id, status}.
          A second start returns the charge already waiting instead of charging twice.
          {skip: true} means no card processor is live: take the card on the standalone
          terminal and record_payment(mode_of_payment='Credit Card', transaction_number=
          <approval code>).
        * poll (instruction_id?, wait_seconds 0–25): ask the terminal. ok=true = approved, the
          Payment Entry is booked and the new balance returned; a final status = declined or
          cancelled. No confirm needed.
        * cancel (instruction_id?): stop a charge still waiting (an approval that won the race
          is booked instead).

        instruction_id blank = the invoice's one waiting charge (refused when there is not
        exactly one). confirm=true for start and cancel."""
        given = {"amount": amount, "terminal": terminal, "save_card": save_card,
                 "instruction_id": instruction_id, "wait_seconds": wait_seconds}
        spec = {"start": ("amount", "terminal", "save_card"),
                "poll": ("instruction_id", "wait_seconds"),
                "cancel": ("instruction_id",)}
        if action not in spec:
            raise ValueError(f"action must be one of {list(spec)}.")
        _only(action, given, spec[action])
        if amount is not None and amount <= 0:
            raise ValueError("amount must be more than 0 (blank = the whole balance).")
        if action != "poll":
            require_confirm(f"card_terminal_charge {action} {sales_invoice}", confirm)
        client = get_client()
        if action == "start":
            return _skip_note(client.call_method(_PAY + "charge_sales_invoice", {
                "sales_invoice": sales_invoice, "amount": amount, "terminal": terminal,
                "save_card": 1 if save_card else 0}),
                "record_payment(mode_of_payment='Credit Card', transaction_number=<approval code>)")
        iid = instruction_id
        if not iid:
            waiting = client.list_documents(
                "Card Terminal Payment", fields=["name", "instruction_id"],
                filters=[["sales_invoice", "=", sales_invoice], ["status", "=", "IN_PROGRESS"]],
                limit=2) or []
            if len(waiting) != 1:
                raise WriteRefused(f"{sales_invoice} has {len(waiting)} card charge(s) waiting; "
                                   "pass instruction_id.")
            iid = waiting[0]["instruction_id"]
        args = {"sales_invoice": sales_invoice, "instruction_id": iid}
        if action == "cancel":
            return client.call_method(_PAY + "cancel_sales_invoice_charge", args)
        return _poll(lambda: client.call_method(_PAY + "poll_sales_invoice_charge", args),
                     wait_seconds)

    @mcp.tool()
    def saved_cards(customer: str) -> Any:
        """The cards a customer has on file: {cards: [{name, card_summary (brand and last four),
        card_expiry, is_default, usage, saved_on}], can_manage (whether the connected user may
        charge or remove them)}. `name` is what charge_saved_card takes. Never the card token.
        Read-only."""
        return get_client().call_method(_SC + "list_saved_cards", {"customer": customer})

    @mcp.tool()
    def charge_saved_card(
        sales_invoice: str, saved_card: str, amount: float | None = None, confirm: bool = False,
    ) -> Any:
        """Charge a balance on a submitted Sales Invoice to the customer's card on file, with no
        customer present (managers only; the card must be this invoice customer's, from
        saved_cards). amount at most the balance; blank = all of it. Returns {ok, status,
        approval_code, amount, payment_entry, outstanding} on approval, {ok: false, status,
        error} on a decline.

        If the answer is lost ('Charge not confirmed'), do NOT take another payment: call again
        with the same arguments a few minutes later and the POS looks the charge up instead of
        charging twice (resolved=true). If it stays unconfirmed after 5 minutes, a manager checks
        the processor's portal and releases it with frappe_run_method(
        'ffl_integrations.payments.saved_cards.release_saved_card_charge', confirm=true).
        confirm=true."""
        if amount is not None and amount <= 0:
            raise ValueError("amount must be more than 0 (blank = the whole balance).")
        require_confirm(f"charge_saved_card {sales_invoice} {saved_card}", confirm)
        return get_client().call_method(_SC + "charge_saved_card", {
            "sales_invoice": sales_invoice, "saved_card": saved_card, "amount": amount})

    # ===================================================== walk-in trade-in

    @mcp.tool()
    def trade_in_intake(
        seller_customer: str,
        id_type: Literal["Driver License", "State ID", "Passport", "Military ID", "Other"],
        id_number: str, payout_method: Literal["Cash", "Zelle", "Check", "ACH"],
        payout_reference: str | None = None, firearm: dict | None = None,
        accessories: list[dict] | None = None,
        source_first_name: str | None = None, source_middle_name: str | None = None,
        source_last_name: str | None = None, source_suffix: str | None = None,
        source_address_line1: str | None = None, source_city: str | None = None,
        source_state: str | None = None, source_zip: str | None = None,
        confirm: bool = False,
    ) -> Any:
        """Buy a gun (and/or accessories or ammo) from a walk-in seller at the counter and pay
        them now: it makes the gun's own Used Item, books the A&D acquisition and its FastBound
        push, receives the stock and pays the seller (Cash comes out of the drawer). The
        counter's path (Sales User may run it). Receiving from a supplier, or into a catalog
        Item, goes through receive_goods. Trade-in as store credit inside a cart is not offered.

        seller_customer: the seller's Customer. Their name and address (ATF wants both) come from
        the Customer; pass source_* to override (source_state is the 2-letter code); refused
        before anything is written if still missing. payout_reference is required unless Cash.
        firearm: {serial_no, manufacturer, model_name, caliber, firearm_type (Pistol, Revolver,
        Rifle, Shotgun, Receiver, Other Firearm, ...), condition_grade (New, Excellent, Very
        Good, Good, Fair), acquisition_cost (what the store pays), retail_price?, importer?,
        barrel_length?, finish?, year_made?, item_code? (an existing used-firearm Item; blank
        makes one), storage_location?}. accessories: [{item_code, qty, rate (paid per unit)}].
        Returns {item_code, acquisition, purchase_receipt, payout: {entry, journal_entry}}.
        confirm=true."""
        if not firearm and not accessories:
            raise ValueError("Nothing to buy: give firearm and/or accessories.")
        if id_type not in _ID_TYPES:
            raise ValueError(f"id_type must be one of {list(_ID_TYPES)}.")
        if payout_method not in _PAYOUTS:
            raise ValueError(f"payout_method must be one of {list(_PAYOUTS)}.")
        ref = (payout_reference or "").strip()
        if payout_method == "Cash" and ref:
            raise ValueError("payout_reference is for Zelle, Check or ACH, not Cash.")
        if payout_method != "Cash" and not ref:
            raise ValueError(f"payout_reference is required for a {payout_method} payout.")
        gun: dict = {}
        if firearm:
            if not isinstance(firearm, dict):
                raise ValueError("firearm must be an object.")
            _unknown("firearm", firearm, _GUN_KEYS)
            missing = [k for k in _GUN_REQUIRED if firearm.get(k) in (None, "")]
            if missing:
                raise ValueError(f"firearm is missing {missing}.")
            if firearm["firearm_type"] not in _FIREARM_TYPES:
                raise ValueError(f"firearm.firearm_type must be one of {sorted(_FIREARM_TYPES)}.")
            if firearm["condition_grade"] not in _CONDITIONS:
                raise ValueError(f"firearm.condition_grade must be one of {sorted(_CONDITIONS)}.")
            if _f(firearm["acquisition_cost"]) <= 0:
                raise ValueError("firearm.acquisition_cost (what the store pays) must be more than 0.")
            gun = dict(firearm)
        for i, a in enumerate(accessories or []):
            if not isinstance(a, dict):
                raise ValueError(f"accessories[{i}] must be an object.")
            _unknown(f"accessories[{i}]", a, {"item_code", "qty", "rate"})
            if not a.get("item_code") or _f(a.get("qty")) <= 0 or _f(a.get("rate")) <= 0:
                raise ValueError(f"accessories[{i}] needs item_code, qty > 0 and rate > 0.")
        require_confirm(f"trade_in_intake {seller_customer} "
                        f"{gun.get('serial_no') or 'accessories'} (pays the seller)", confirm)
        client = get_client()
        defaults = client.call_method(_TI + "seller_defaults", {"customer": seller_customer}) or {}
        if not defaults:
            raise WriteRefused(f"Customer {seller_customer!r} was not found; nothing was written.")
        given = {"source_first_name": source_first_name, "source_middle_name": source_middle_name,
                 "source_last_name": source_last_name, "source_suffix": source_suffix,
                 "source_address_line1": source_address_line1, "source_city": source_city,
                 "source_state": source_state, "source_zip": source_zip}
        seller = {k: (str(given[k]).strip() if given[k] else str(defaults.get(k) or "").strip())
                  for k in _SELLER_NAME + _SELLER_ADDRESS}
        missing = [k for k in ("source_first_name", "source_last_name") + _SELLER_ADDRESS
                   if not seller[k]]
        if missing:
            raise ValueError(f"The seller's ATF name/address is incomplete: {missing}. Pass them "
                             "(or fix the Customer's primary address); nothing was written.")
        payload = {
            "source_name": seller_customer, "id_type": id_type, "id_number": id_number,
            **seller, "has_firearm": 1 if gun else 0, **gun,
            "accessories": accessories or [], "apply_credit": 0,
            "payout_method": payout_method, "payout_reference": ref or None,
        }
        return client.call_method(_TI + "create_trade_in_intake", {"payload": payload})
