"""Service tools: gunsmith work orders and memberships.

Both are counter features of ffl_core with their own page backends
(``ffl_core/gunsmith.py``, ``ffl_core/membership.py``). Every signature below was checked
against the POS source; the tests are mocked, so drift would only show against a live POS.

* Gunsmith: ``gunsmith_jobs`` (read), ``gunsmith_intake`` and ``gunsmith_job_action``
  (writes). A job's money moves through register tickets (draft POS Invoices): taking a
  payment, picking up with a bill and refunding a deposit each return ``{pos_invoice}``,
  which the clerk (or ``pos_ticket``) tenders and submits. ``take_payment`` calls
  ``ffl_core.gunsmith.open_deposit(name, amount)``, a POS endpoint added for the MCP
  (the register itself uses the cart-only ``deposit_line``); an older POS answers it
  with "method not found".
* Memberships: ``memberships`` (read) and ``membership_action`` (writes).
  ``memberships(view='customer')`` is NOT a pure read: like opening the customer's panel,
  it activates a sign-up whose invoice was since paid another way, which can send that
  receipt email. It must stay off every read-only surface (cpa).

Every write needs ``confirm=true``; arguments that do not belong to the chosen action are
refused rather than silently dropped. The POS enforces roles per call (counter roles;
cancel and retry_card on a membership, and the needs-attention list, need a manager).
"""
from __future__ import annotations

import re
from typing import Any, Literal

from ..frappe_client import get_client
from ..safety import check_fields_writable, require_confirm, require_reason

_GS = "ffl_core.gunsmith."
_MB = "ffl_core.membership."
_WO = "Gunsmith Work Order"

FirearmType = Literal[
    "Pistol", "Revolver", "Rifle", "Shotgun", "Receiver", "Other Firearm", "Silencer",
    "Short Barreled Rifle", "Short Barreled Shotgun", "Machine Gun", "Destructive Device",
    "Any Other Weapon",
]
IdType = Literal["Driver License", "State ID", "Passport", "Military ID", "Other"]

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Never returned by gunsmith_jobs: the customer's ID number and the scan of the ID.
# (scan_id is cleared by the POS on every save; dropped here in case an old row kept one.)
_WO_HIDDEN = ("id_number", "id_document", "scan_id")
_LINE_KEYS = frozenset({"item_code", "qty", "rate", "description"})

# gunsmith_job_action: which optional arguments each action takes, and the method it calls.
_JOB_ARGS = {
    "start": (),
    "ready": ("work_performed", "lines"),
    "notify": ("via",),
    "take_payment": ("amount",),
    "pickup": ("work_performed", "lines"),
    "cancel": ("reason",),
    "log_bound_book": ("id_type", "id_number"),
    "refund_deposit": (),
}
_JOB_METHOD = {
    "start": "start_work",
    "notify": "notify_customer",
    "take_payment": "open_deposit",   # NEW POS endpoint: open_deposit(name, amount)
    "cancel": "cancel_work_order",
    "log_bound_book": "log_in_bound_book",
    "refund_deposit": "refund_deposit",
}

# memberships: which arguments each view takes.
_VIEW_ARGS = {
    "overview": (),
    "members": ("plan", "status", "search", "start"),
    "customer": ("customer",),
    "attention": (),
}

# membership_action: allowed / required arguments and the method each action calls.
_MB_ARGS = {
    "start": ("customer", "plan", "auto_renew"),
    "invoice": ("membership",),
    "invoice_renewal": ("membership",),
    "retry_card": ("membership",),
    "set_auto_renew": ("membership", "auto_renew"),
    "change_card": ("membership", "saved_card"),
    "cancel": ("membership", "reason", "end_now"),
    "drop": ("membership", "reason"),
}
_MB_REQUIRED = {
    "start": ("customer", "plan"),
    "invoice": ("membership",),
    "invoice_renewal": ("membership",),
    "retry_card": ("membership",),
    "set_auto_renew": ("membership", "auto_renew"),
    "change_card": ("membership", "saved_card"),
    "cancel": ("membership", "reason"),
    "drop": ("membership", "reason"),
}
_MB_METHOD = {
    "start": "start_membership",
    "invoice": "invoice_membership",
    "invoice_renewal": "invoice_renewal",
    "retry_card": "retry_renewal",
    "set_auto_renew": "set_auto_renew",
    "change_card": "change_card",
    "cancel": "cancel_membership",
    "drop": "discard_membership",
}


def _given(value) -> bool:
    """An argument counts as passed unless it is None, blank or False (a default)."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _refuse_stray(what: str, allowed, given: dict) -> None:
    stray = sorted(k for k, v in given.items() if _given(v) and k not in allowed)
    if stray:
        raise ValueError(f"{stray} do not apply to {what} — refusing rather than dropping them "
                         "silently.")


def _choice(name: str, value: str | None, options) -> str:
    v = (value or "").strip()
    if v not in options:
        raise ValueError(f"{name} must be one of {', '.join(options)} (got {value!r}).")
    return v


def _clean_lines(lines) -> list[dict]:
    """Validate the charges shape; the POS checks the items themselves (exists, not a
    firearm, not a locked line)."""
    if not isinstance(lines, list):
        raise ValueError("lines is a list of {item_code, qty?, rate?, description?}.")
    out = []
    for i, row in enumerate(lines, 1):
        if not isinstance(row, dict):
            raise ValueError(f"lines[{i}] must be an object {{item_code, qty?, rate?, description?}}.")
        stray = sorted(set(row) - _LINE_KEYS)
        if stray:
            raise ValueError(f"lines[{i}] has keys a charge does not take: {stray}.")
        item_code = str(row.get("item_code") or "").strip()
        if not item_code:
            raise ValueError(f"lines[{i}] needs an item_code.")
        qty = 1 if row.get("qty") in (None, "") else float(row["qty"])
        if qty <= 0:
            raise ValueError(f"lines[{i}] ({item_code}): qty must be above 0.")
        rate = None if row.get("rate") in (None, "") else float(row["rate"])
        if rate is not None and rate < 0:
            raise ValueError(f"lines[{i}] ({item_code}): rate must be 0 or more.")
        line: dict[str, Any] = {"item_code": item_code, "qty": qty, "rate": rate}
        desc = (row.get("description") or "").strip()
        if desc:
            line["description"] = desc
        out.append(line)
    return out


def _price_lines(client, lines: list[dict], customer: str | None) -> list[dict]:
    """Fill a missing rate with the item's list price for the customer, as the register's
    charge picker does (charge_details)."""
    for line in lines:
        if line["rate"] is None:
            got = client.call_method(_GS + "charge_details",
                                     {"item_code": line["item_code"], "customer": customer})
            line["rate"] = float((got or {}).get("rate") or 0)
    return lines


def register(mcp: Any) -> None:
    # ===================================================== gunsmith: read

    @mcp.tool()
    def gunsmith_jobs(search: str | None = None, work_order: str | None = None) -> Any:
        """The gunsmith queue: what is in, in progress or ready for pickup, what each job owes,
        deposits held, open register tickets, and whether the work authorization is signed.
        Read-only.

        No work_order: the list (search matches part of the job name, customer, make, model or
        serial). Open jobs come first, Ready at the top, then closed jobs still holding a
        deposit to refund. Each row: name, customer, status (Received | In Progress | Ready |
        Picked Up | Cancelled), the gun, work_requested, work_performed, estimated_cost,
        lines [{item_code, description, qty, rate}] (the charges), deposit_held, logged (in the
        bound book), drafts {deposit, pickup, refund} (open register tickets) and authorization
        (signed / pending / missing …, open jobs only); plus labor_item. At most 100 a group.

        work_order: one job of any status, with every field of the form (the customer's ID
        number and ID scan are never returned; id_on_file says whether one is held) merged with
        its live state: labor_item, deposit_held, charges (total), authorization {template,
        state, agreement} and drafts."""
        if search and work_order:
            raise ValueError("give search or work_order, not both.")
        client = get_client()
        if not work_order:
            return client.call_method(_GS + "register_work_orders", {"search_term": search})
        doc = client.get_document(_WO, work_order)
        out = {k: v for k, v in (doc or {}).items() if k not in _WO_HIDDEN}
        out["id_on_file"] = bool((doc or {}).get("id_number"))
        out.update(client.call_method(_GS + "get_state", {"name": work_order}) or {})
        return out

    # ===================================================== gunsmith: writes

    @mcp.tool()
    def gunsmith_intake(
        customer: str, manufacturer: str, model: str, serial_number: str, work_requested: str,
        caliber: str | None = None, firearm_type: FirearmType | None = None,
        accessories: str | None = None, condition: str | None = None,
        estimated_cost: float | None = None, estimated_completion: str | None = None,
        log_in_bound_book: bool = False, id_type: IdType | None = None,
        id_number: str | None = None, confirm: bool = False,
    ) -> Any:
        """Take a customer's gun in for repair and open a work order (status Received; its
        first charge is the labor item at estimated_cost). customer is the Customer name (id).
        estimated_completion is YYYY-MM-DD.

        log_in_bound_book=true also logs the gun in the bound book now: a Gunsmithing
        acquisition from the customer into customer custody (zero cost, never for sale) and
        the FastBound push, all or nothing. It needs caliber, firearm_type, id_type and
        id_number, and the customer's first/last name and street address on file. A job can
        be logged later with gunsmith_job_action(action='log_bound_book'); never un-logged.

        No payment is taken here. Next steps: get the work authorization signed with
        agreement_request(reference_doctype='Gunsmith Work Order', reference_name=<name>), and
        take a deposit with gunsmith_job_action(action='take_payment').
        Returns {name, customer, customer_name, status, purchase_receipt (when logged),
        authorization_template}. Requires confirm=true."""
        required = {"customer": customer, "manufacturer": manufacturer, "model": model,
                    "serial_number": serial_number, "work_requested": work_requested}
        missing = [k for k, v in required.items() if not (v or "").strip()]
        if missing:
            raise ValueError(f"a work order needs {', '.join(missing)}.")
        if log_in_bound_book:
            need = {"caliber": caliber, "firearm_type": firearm_type, "id_type": id_type,
                    "id_number": id_number}
            missing = [k for k, v in need.items() if not (v or "").strip()]
            if missing:
                raise ValueError("logging the gun in the bound book needs "
                                 f"{', '.join(missing)} — nothing was sent.")
        if estimated_completion and not _DAY_RE.match(estimated_completion):
            raise ValueError(f"estimated_completion is YYYY-MM-DD (got {estimated_completion!r}).")
        if estimated_cost is not None and estimated_cost < 0:
            raise ValueError("estimated_cost must be 0 or more.")
        require_confirm(f"gunsmith_intake {serial_number}"
                        + (" (logs it in the bound book)" if log_in_bound_book else ""), confirm)
        fields = {**required, "caliber": caliber, "firearm_type": firearm_type,
                  "accessories": accessories, "condition": condition,
                  "estimated_cost": estimated_cost, "estimated_completion": estimated_completion,
                  "id_type": id_type, "id_number": id_number}
        payload: dict[str, Any] = {
            k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items()
            if v is not None and not (isinstance(v, str) and not v.strip())}
        payload["log_in_bound_book"] = 1 if log_in_bound_book else 0
        # pay_now is never sent: it only builds a cart line for the register.
        return get_client().call_method(_GS + "create_work_order", {"payload": payload})

    @mcp.tool()
    def gunsmith_job_action(
        work_order: str,
        action: Literal["start", "ready", "notify", "take_payment", "pickup", "cancel",
                        "log_bound_book", "refund_deposit"],
        work_performed: str | None = None, lines: list[dict] | None = None,
        via: Literal["Email", "Phone", "Text"] | None = None, amount: float | None = None,
        reason: str | None = None, id_type: IdType | None = None, id_number: str | None = None,
        confirm: bool = False,
    ) -> Any:
        """Move a repair job through its life. action:

        * start — Received (or Ready, reopened) → In Progress.
        * ready — the work is done: Received / In Progress → Ready. Optional work_performed
          and lines (the charges: [{item_code, qty=1, rate?, description?}], they REPLACE the
          job's; a missing rate is the item's list price for the customer) are saved first.
          The job needs work_performed. New work_performed / lines are refused while a
          deposit or pick-up ticket is open.
        * notify — tell the customer it is ready (via: Email sends the store's email now;
          Phone / Text only record that staff called or texted). The job must be Ready.
        * take_payment — a payment towards an open job, any amount above 0, any time: opens
          (or returns the open) deposit register ticket. Returns {pos_invoice}.
        * pickup — hand the gun back. With work_performed / lines, they are confirmed first
          (lines omitted = the job's current charges are kept) and the job is marked Ready.
          With nothing billed and no deposit, the job is Picked Up at once (a logged gun's
          bound-book disposition is booked) and {picked_up, refund_due} comes back; otherwise
          {pos_invoice} — the pick-up ticket with the charges less the deposit, whose submit
          picks the job up.
        * cancel — close a job that will not be done (reason required). A logged gun cannot be
          cancelled: clear its charges and pick it up instead.
        * log_bound_book — log the gun in the bound book now (id_type / id_number when the job
          has none). Logged once, never un-logged.
        * refund_deposit — a Picked Up / Cancelled job still holding a deposit: opens the
          register return that pays it back the way it was paid. Returns {pos_invoice, amount}.

        A {pos_invoice} is a DRAFT register ticket: complete it with pos_ticket (tender, then
        submit) or at the register. Arguments that do not belong to the action are refused.
        Requires confirm=true."""
        action = _choice("action", action, _JOB_ARGS)
        _refuse_stray(f"action={action}", _JOB_ARGS[action], {
            "work_performed": work_performed, "lines": lines, "via": via, "amount": amount,
            "reason": reason, "id_type": id_type, "id_number": id_number})
        clean = _clean_lines(lines) if lines is not None else None
        kwargs: dict[str, Any] = {"name": work_order}
        if action == "notify":
            kwargs["via"] = _choice("via", via, ("Email", "Phone", "Text"))
        elif action == "take_payment":
            if amount is None or amount <= 0:
                raise ValueError("take_payment needs an amount above 0.")
            kwargs["amount"] = amount
        elif action == "cancel":
            kwargs["reason"] = require_reason("gunsmith_job_action cancel", reason)
        elif action == "log_bound_book":
            kwargs.update({"id_type": id_type, "id_number": (id_number or "").strip() or None})
        require_confirm(f"gunsmith_job_action {action} {work_order}", confirm)
        client = get_client()

        if action in _JOB_METHOD:
            return client.call_method(_GS + _JOB_METHOD[action], kwargs)

        if action == "pickup" and clean is None and not _given(work_performed):
            return client.call_method(_GS + "open_pickup", kwargs)

        # ready (with or without new charges) and pickup with charges both need the job.
        if action == "ready" and clean is None and not _given(work_performed):
            return client.call_method(_GS + "mark_ready", kwargs)
        doc = client.get_document(_WO, work_order) or {}
        if clean is None:
            # complete_work REPLACES the charges, so omitted lines must carry the current ones.
            clean = [{"item_code": r.get("item_code"), "qty": r.get("qty") or 1,
                      "rate": r.get("rate") or 0, "description": r.get("description")}
                     for r in doc.get("lines") or []]
        clean = _price_lines(client, clean, doc.get("customer"))
        performed = (work_performed or "").strip() or (doc.get("work_performed") or "").strip()

        if action == "pickup":
            if not performed:
                raise ValueError("pickup with charges needs work_performed (what was done).")
            return client.call_method(_GS + "complete_work", {
                "name": work_order, "work_performed": performed, "lines": clean})

        # ready with new work_performed / lines: save them on the job, then mark it Ready.
        state = client.call_method(_GS + "get_state", {"name": work_order}) or {}
        drafts = state.get("drafts") or {}
        open_ = [f"{k}: {drafts[k]}" for k in ("deposit", "pickup") if drafts.get(k)]
        if open_:
            raise ValueError(f"a register ticket for this job is open ({', '.join(open_)}); "
                             "complete or delete it before changing the charges.")
        values: dict[str, Any] = {}
        if _given(work_performed):
            values["work_performed"] = performed
        if lines is not None:
            values["lines"] = clean
        check_fields_writable(_WO, values)
        client.update_document(_WO, work_order, values)
        if doc.get("status") == "Ready":
            return {"status": "Ready", "updated": sorted(values)}
        out = client.call_method(_GS + "mark_ready", kwargs) or {}
        return {**out, "updated": sorted(values)}

    # ===================================================== memberships: read

    @mcp.tool()
    def memberships(
        view: Literal["overview", "members", "customer", "attention"] = "overview",
        customer: str | None = None, plan: str | None = None,
        status: Literal["Pending", "Active", "Past Due", "Suspended", "Cancelled",
                        "Expired"] | None = None,
        search: str | None = None, start: int = 0,
    ) -> Any:
        """Memberships. view:

        * overview — the plans (price, billing period, fee, members), totals {active, monthly
          revenue (Active + Past Due, a yearly price counts a twelfth), renewing_soon (next 7
          days), attention}, and for managers the needs-attention list.
        * members — the member roster, 50 a page by customer name: plan?, status?, search?
          (part of the customer's name, id or mobile), start (rows to skip). Rows: name,
          customer, plan, status, auto_renew, next_charge_date, ends_on, card_summary; more
          says whether another page follows.
        * customer — one customer's memberships (customer required): each row's status, terms,
          what is overdue, whether the card can be retried; the plans on sale; the customer's
          cards saved for recurring charges; can_cancel. NOT purely a read: like opening the
          customer's panel, it activates a sign-up whose invoice was since paid another way,
          which can send that receipt email.
        * attention — managers: Past Due, Suspended, unconfirmed card charges, renewals that
          could not be charged, sign-ups unpaid after a few days.

        Arguments that do not belong to the view are refused."""
        view = _choice("view", view, _VIEW_ARGS)
        _refuse_stray(f"view={view}", _VIEW_ARGS[view], {
            "customer": customer, "plan": plan, "status": status, "search": search,
            "start": start or None})
        client = get_client()
        if view == "overview":
            return client.call_method(_MB + "page_overview")
        if view == "attention":
            return client.call_method(_MB + "needs_attention")
        if view == "customer":
            if not (customer or "").strip():
                raise ValueError("view=customer needs customer.")
            return client.call_method(_MB + "get_memberships", {"customer": customer.strip()})
        if start < 0:
            raise ValueError("start must be 0 or more.")
        return client.call_method(_MB + "page_members", {
            "plan": plan, "status": status, "search": search, "start": start})

    # ===================================================== memberships: write

    @mcp.tool()
    def membership_action(
        action: Literal["start", "invoice", "invoice_renewal", "retry_card", "set_auto_renew",
                        "change_card", "cancel", "drop"],
        membership: str | None = None, customer: str | None = None, plan: str | None = None,
        auto_renew: bool | None = None, saved_card: str | None = None,
        reason: str | None = None, end_now: bool = False, confirm: bool = False,
    ) -> Any:
        """Sell and manage memberships. action:

        * start — customer + plan (+ auto_renew; omitted = the plan's default): the customer's
          Pending membership (an existing Pending one is reused). Then get the plan's terms
          signed (agreement_request) and invoice it.
        * invoice — a Pending membership whose terms are signed: submits its first Sales
          Invoice (or returns the unpaid one) → {sales_invoice, outstanding}. Take the payment
          with record_payment or card_terminal_charge; paid = Active.
        * invoice_renewal — an overdue renewal's invoice, for payment by any tender.
        * retry_card — managers: charge the saved card now for an overdue renewal →
          {outcome: renewed | declined | unknown | not_started, message, row}.
        * set_auto_renew — auto_renew true / false (on needs a card saved for recurring
          charges).
        * change_card — saved_card: another of the customer's recurring-use cards.
        * cancel — managers, reason required: runs to the end of the paid period, or ends
          today with end_now=true (the rest of the deferred income is recognized today). The
          invoice is never cancelled; a refund is a separate credit note.
        * drop — reason required: a Pending sign-up nobody paid; its unpaid invoice is
          cancelled.

        membership is the Membership name (every action but start). Arguments that do not
        belong to the action are refused. Requires confirm=true."""
        action = _choice("action", action, _MB_ARGS)
        given = {"membership": membership, "customer": customer, "plan": plan,
                 "auto_renew": auto_renew, "saved_card": saved_card, "reason": reason,
                 "end_now": end_now}
        # auto_renew=False is a real answer, not "not passed".
        present = {k: (True if k == "auto_renew" and v is not None else v)
                   for k, v in given.items()}
        _refuse_stray(f"action={action}", _MB_ARGS[action], present)
        missing = [k for k in _MB_REQUIRED[action] if not _given(present[k])]
        if missing:
            raise ValueError(f"action={action} needs {', '.join(missing)}.")
        require_confirm(f"membership_action {action} {membership or customer}", confirm)
        if action == "start":
            kwargs: dict[str, Any] = {
                "customer": customer.strip(), "plan": plan.strip(),
                "auto_renew": None if auto_renew is None else (1 if auto_renew else 0)}
        else:
            kwargs = {"membership": membership.strip()}
            if action == "set_auto_renew":
                kwargs["enabled"] = 1 if auto_renew else 0
            elif action == "change_card":
                kwargs["saved_card"] = saved_card.strip()
            elif action in ("cancel", "drop"):
                kwargs["reason"] = reason.strip()
                if action == "cancel":
                    kwargs["end_now"] = 1 if end_now else 0
        return get_client().call_method(_MB + _MB_METHOD[action], kwargs)
