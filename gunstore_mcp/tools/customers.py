"""Customer tools: the counter's check on a customer, finding duplicates, manager actions on a
customer, and the customer's agreements (waivers, used-gun acknowledgments, gunsmith
authorizations, membership terms).

Every wrapped method lives in ffl_core (``customer_flags``, ``customer_profile``,
``agreements``, ``signing_terminal``); signatures were checked against gunstore-pos
develop. The tests are mocked, so drift would only show against a live POS.

Reads: ``customer_overview``, ``customer_find``. Neither returns an ID number or a date of
birth (the POS methods never send them).

Writes need ``confirm=true``: ``customer_manage``, ``agreement_request``,
``agreement_manage``. The POS enforces the roles per call (managers for merge, the ban
override, a change to or from Banned, voids and paper copies; counter roles for a request).
Arguments that do not belong to the chosen action are refused rather than dropped.

Never offered: an agreement's signing link or QR code (``deliver="qr"``). The link is the
customer's key to sign; it goes to the customer by email or to the counter tablet, and is
stripped from any answer.
"""
from __future__ import annotations

import datetime
from typing import Any, Literal

from ..frappe_client import get_client
from ..safety import check_fields_writable, require_confirm, require_reason

_CF = "ffl_core.customer_flags."
_CP = "ffl_core.customer_profile."
_AG = "ffl_core.agreements."
_TERMINALS = "ffl_core.signing_terminal.list_terminals"

_FIND_MAX = 10
_LINK_KEYS = ("sign_url", "qr_svg")
_STATUSES = ("Active", "Inactive", "Prospect", "Banned")

# customer_manage: which optional arguments each action takes (required ones first).
_MANAGE_ARGS = {
    "merge": ("into",),
    "override_ban": ("reason",),
    "set_status": ("status", "reason"),
    "add_alert": ("message",),
}
_MANAGE_REQUIRED = {
    "merge": ("into",),
    "override_ban": ("reason",),
    "set_status": ("status",),
    "add_alert": ("message",),
}


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _refuse_stray(what: str, given: dict, allowed) -> None:
    stray = sorted(k for k, v in given.items() if v and k not in allowed)
    if stray:
        raise ValueError(f"{stray} do not apply to {what} — refusing rather than dropping "
                         "them silently.")


_SAVED_KEYS = ("name", "customer_name", "customer_status", "status_reason", "customer_alerts",
               "modified")


def _saved(doc: Any) -> Any:
    """A saved Customer, minus its date of birth and ID documents (update_document answers
    with the whole record)."""
    return {k: doc.get(k) for k in _SAVED_KEYS} if isinstance(doc, dict) else doc


def _no_link(result: Any) -> Any:
    """Drop the signing link and its QR code from an agreement answer."""
    if isinstance(result, dict) and any(k in result for k in _LINK_KEYS):
        return {k: v for k, v in result.items() if k not in _LINK_KEYS}
    return result


def _first_serial(value: Any) -> str:
    # A cart line may hold several serials, one per line (agreements._row_serials).
    return next((s.strip() for s in str(value or "").splitlines() if s.strip()), "")


def _cart_rows(client, cart: list[dict]) -> list[dict]:
    """The cart as agreements_for_sale wants it: every row with an item_code (the POS drops
    rows without one), filled from the Serial No when only the serial was given."""
    rows = []
    for i, r in enumerate(cart):
        if not isinstance(r, dict):
            raise ValueError(f"cart[{i}] must be an object {{serial_no?, item_code?, rate?}}.")
        serial, item = _clean(r.get("serial_no")), _clean(r.get("item_code"))
        if not serial and not item:
            raise ValueError(f"cart[{i}] needs a serial_no or an item_code.")
        rows.append({"item_code": item or None, "serial_no": serial or None, "rate": r.get("rate")})
    missing = sorted({_first_serial(r["serial_no"]) for r in rows if not r["item_code"]})
    if missing:
        found = {s["name"]: s.get("item_code") for s in client.list_documents(
            "Serial No", fields=["name", "item_code"], filters=[["name", "in", missing]],
            limit=len(missing)) or []}
        unknown = [s for s in missing if not found.get(s)]
        if unknown:
            raise ValueError(f"Serial No not found: {', '.join(unknown)}.")
        for r in rows:
            if not r["item_code"]:
                r["item_code"] = found[_first_serial(r["serial_no"])]
    return [{k: v for k, v in r.items() if v is not None} for r in rows]


def register(mcp: Any) -> None:
    # ===================================================== reads

    @mcp.tool()
    def customer_overview(customer: str, cart: list[dict] | None = None,
                          sale: str | None = None) -> Any:
        """Check a customer before a sale, trade-in or transfer: status (banned, or under a
        manager's override until `override_until`), staff alerts, tags, family, ID documents
        expired or expiring within 30 days, and their agreement copies.

        Returns {flags: {name, customer_name, status, status_reason, banned, override_until,
        can_override, alerts, tags, family, ids: [{id_type, expiry_date, expired}], agreements
        (the ones on the counter panel: template, state, expires_on, agreement)}, family:
        {primary, relationship, members, can_merge}, agreements: {rows: [{name (a Customer
        Agreement), template, serial_no, status, signed_on, signed_via, expires_on,
        signed_pdf}] newest first, can_manage}}.

        With a cart ([{serial_no?, item_code?, rate?}]; item_code is filled from the Serial No
        when missing) or a sale (a POS Invoice name; its lines become the cart), also
        sale_agreements: {rows: [{template, scope, requirement, required, state signed |
        pending | missing | expiring | expired, expires_on, agreement, firearms}]}. The POS
        refuses to submit the sale until every required one is signed; send one with
        agreement_request. Give cart or sale, not both.

        Read-only; it never returns ID numbers or dates of birth."""
        customer = _clean(customer)
        if not customer:
            raise ValueError("customer is required.")
        if cart and sale:
            raise ValueError("give cart or sale, not both.")
        client = get_client()
        out = {
            "flags": client.call_method(_CF + "get_customer_flags", {"customer": customer}),
            "family": client.call_method(_CP + "get_family", {"customer": customer}),
            "agreements": client.call_method(_AG + "get_customer_agreements", {"customer": customer}),
        }
        if sale:
            inv = client.get_document("POS Invoice", sale) or {}
            if inv.get("customer") and inv["customer"] != customer:
                raise ValueError(f"{sale} is {inv['customer']}'s sale, not {customer}'s.")
            items = [{k: row.get(k) for k in ("item_code", "serial_no", "rate")}
                     for row in inv.get("items") or []]
            out["sale_agreements"] = client.call_method(_AG + "agreements_for_sale", {
                "customer": customer, "items": items, "sale": sale})
        elif cart:
            out["sale_agreements"] = client.call_method(_AG + "agreements_for_sale", {
                "customer": customer, "items": _cart_rows(client, cart)})
        return out

    @mcp.tool()
    def customer_find(email: str | None = None, phone: str | None = None,
                      id_number: str | None = None, first_name: str | None = None,
                      last_name: str | None = None) -> Any:
        """Find existing customers before creating one or merging duplicates: by email (emails
        merged into a customer included), phone (compared on the last 10 digits), ID number,
        or first AND last name together. Give at least one.

        Returns up to 10 rows: [{name, customer_name, customer_status, matched: [email |
        phone | id_number | name], address? (name matches)}]. A name match is only a guess —
        confirm with the person. ID-number matching returns nothing unless the store keeps ID
        numbers (FFL Settings → Store Customer ID Numbers). Read-only."""
        email, phone, id_number = _clean(email), _clean(phone), _clean(id_number)
        first, last = _clean(first_name), _clean(last_name)
        if bool(first) != bool(last):
            raise ValueError("give first_name and last_name together.")
        if not (email or phone or id_number or first):
            raise ValueError("give at least one of email, phone, id_number, or first_name + last_name.")
        client = get_client()
        rows: dict[str, dict] = {}
        if email or phone or id_number:
            for r in client.call_method(_CF + "find_possible_duplicates", {
                    "email": email or None, "phone": phone or None,
                    "id_number": id_number or None}) or []:
                rows[r["name"]] = {**r, "matched": list(r.get("matched") or [])}
        if first:
            for r in client.call_method(_CF + "find_customers_by_scan",
                                        {"first": first, "last": last}) or []:
                row = rows.setdefault(r["name"], {
                    "name": r["name"], "customer_name": r.get("customer_name"),
                    "customer_status": r.get("customer_status"), "matched": []})
                row["matched"].append("name")
                if r.get("address"):
                    row["address"] = r["address"]
        return list(rows.values())[:_FIND_MAX]

    # ===================================================== writes

    @mcp.tool()
    def customer_manage(
        action: Literal["merge", "override_ban", "set_status", "add_alert"], customer: str,
        into: str | None = None, reason: str | None = None,
        status: Literal["Active", "Inactive", "Prospect", "Banned"] | None = None,
        message: str | None = None, confirm: bool = False,
    ) -> Any:
        """⚠ Manager actions on a customer. action:

        * merge — customer (the DUPLICATE, which is deleted) into `into` (the record to
          keep). Irreversible: every invoice, 4473, order, payment, contact and address
          moves to `into`; alerts and ID documents are copied; the duplicate's emails and
          GunBroker id become aliases of `into`; a Banned duplicate makes `into` Banned.
          The POS refuses the web-store and walk-in customers, two dealers with different
          licences, and a merge that would nest families. Compare both with
          customer_overview first and confirm which one stays with the user.
        * override_ban — let a Banned customer be served for the next 30 minutes; `reason`
          is required and goes on the customer's timeline. The customer stays Banned.
        * set_status — Active | Inactive | Prospect | Banned. Banned needs a `reason`;
          changing to or from Banned needs a manager.
        * add_alert — add a staff alert (`message`) shown whenever the customer is picked
          at the counter; existing alerts are kept. Refused if the customer changed since
          it was read (call again).

        Arguments that do not belong to the action are refused. The POS enforces the
        roles: merge, override_ban and any change to or from Banned need a manager
        (Sales Manager); other status changes and alerts need Customer write.
        Requires confirm=true."""
        customer = _clean(customer)
        if not customer:
            raise ValueError("customer is required.")
        if action not in _MANAGE_ARGS:
            raise ValueError(f"action must be one of {', '.join(_MANAGE_ARGS)} (got {action!r}).")
        given = {"into": _clean(into), "reason": _clean(reason), "status": status,
                 "message": _clean(message)}
        _refuse_stray(f"action {action}", given, _MANAGE_ARGS[action])
        missing = [k for k in _MANAGE_REQUIRED[action] if not given[k]]
        if missing:
            raise ValueError(f"action {action} needs {', '.join(missing)}.")
        if action == "merge" and given["into"] == customer:
            raise ValueError("a customer cannot be merged into itself.")
        if action == "set_status":
            if status not in _STATUSES:
                raise ValueError(f"status must be one of {', '.join(_STATUSES)}.")
            if status == "Banned" and not given["reason"]:
                raise ValueError("a Banned customer needs a reason.")
        target = f" into {given['into']}" if action == "merge" else ""
        require_confirm(f"customer_manage {action} {customer}{target}", confirm)
        client = get_client()
        if action == "merge":
            return client.call_method(_CP + "merge_customer",
                                      {"source": customer, "target": given["into"]})
        if action == "override_ban":
            return client.call_method(_CF + "override_customer_ban",
                                      {"customer": customer, "reason": given["reason"]})
        if action == "set_status":
            values: dict[str, Any] = {"customer_status": status}
            if given["reason"]:
                values["status_reason"] = given["reason"]
            check_fields_writable("Customer", values)
            return _saved(client.update_document("Customer", customer, values))
        # add_alert: the child table is replaced by what is sent, so send the stored rows
        # back with the new one; `modified` makes the POS refuse if someone saved meanwhile.
        doc = client.get_document("Customer", customer) or {}
        alerts = [{k: a.get(k) for k in ("name", "message", "active", "created_by", "created_on")}
                  for a in doc.get("customer_alerts") or []]
        alerts.append({"message": given["message"], "active": 1})
        values = {"customer_alerts": alerts, "modified": doc.get("modified")}
        check_fields_writable("Customer", values)
        return _saved(client.update_document("Customer", customer, values))

    @mcp.tool()
    def agreement_request(
        customer: str, template: str, deliver: Literal["email", "tablet", "none"] = "email",
        terminal: str | None = None, serial_nos: list[str] | None = None,
        prices: list[float] | None = None, reference_doctype: str | None = None,
        reference_name: str | None = None, replace: bool = False, confirm: bool = False,
    ) -> Any:
        """⚠ Send a customer an agreement to sign (range waiver, used-gun acknowledgment,
        gunsmith authorization, membership terms). template: an active Agreement Template.
        deliver:

        * email — the signing link goes to the customer's email address (refused when the
          customer has none). The link works for 72 hours.
        * tablet — the counter signing tablet opens it. terminal: a paired, active Signing
          Terminal; empty = the only one, else refused with the list.
        * none — only a Pending copy, for a paper signature a manager then records with
          agreement_manage(action="record_paper").

        A template signed for each firearm sold needs serial_nos (one copy covers them all,
        unless the template is one copy per gun — then one gun per call); prices (same
        order, optional) fill the text's price. A template signed for one record (a
        Gunsmith Work Order, a Membership) needs reference_doctype + reference_name, a
        record of this customer.

        If a copy for the same thing already has an open link (the customer may be signing
        it right now), nothing changes and the answer says link_open=true: ask the user
        before calling again with replace=true, which kills that link and sends a new one.
        Returns {name (the Customer Agreement), deliver, token_expires, email? | terminal?}.
        Requires confirm=true."""
        customer, template = _clean(customer), _clean(template)
        if not customer or not template:
            raise ValueError("customer and template are required.")
        if deliver not in ("email", "tablet", "none"):
            raise ValueError("deliver must be email, tablet or none.")
        terminal = _clean(terminal)
        if terminal and deliver != "tablet":
            raise ValueError("terminal applies only to deliver=tablet.")
        serials = [_clean(s) for s in serial_nos or []]
        if any(not s for s in serials):
            raise ValueError("serial_nos has an empty entry.")
        if prices and len(prices) != len(serials):
            raise ValueError("prices must have one entry per serial_no, in the same order.")
        ref_dt, ref_name = _clean(reference_doctype), _clean(reference_name)
        if bool(ref_dt) != bool(ref_name):
            raise ValueError("give reference_doctype and reference_name together.")
        if ref_dt and serials:
            raise ValueError("give serial_nos (a per-firearm template) or a reference record "
                             "(a per-document template), not both.")
        what = ", ".join(serials) or (f"{ref_dt} {ref_name}" if ref_dt else "")
        require_confirm(f"agreement_request {template} for {customer} by {deliver}"
                        + (f" ({what})" if what else ""), confirm)
        client = get_client()
        if deliver == "tablet" and not terminal:
            tablets = [t for t in client.call_method(_TERMINALS) or [] if t.get("paired")]
            if len(tablets) != 1:
                names = ", ".join(t["name"] for t in tablets) or "none"
                raise ValueError(f"pick the tablet: terminal must be one of the paired, active "
                                 f"signing tablets ({names}).")
            terminal = tablets[0]["name"]
        kwargs: dict[str, Any] = {"customer": customer, "template": template, "deliver": deliver,
                                  "replace": 1 if replace else 0}
        if terminal:
            kwargs["terminal"] = terminal
        if len(serials) == 1:
            kwargs["serial_no"] = serials[0]
            if prices:
                kwargs["price"] = prices[0]
        elif serials:
            kwargs["firearms"] = [{"serial_no": s, "price": prices[i] if prices else None}
                                  for i, s in enumerate(serials)]
        if ref_dt:
            kwargs.update(reference_doctype=ref_dt, reference_name=ref_name)
        out = _no_link(client.call_method(_AG + "request_agreement", kwargs))
        if isinstance(out, dict) and out.get("link_open"):
            out = {**out, "note": "Nothing was sent: this copy's link is still open and the "
                   "customer may be signing it now. Ask the user before calling again with "
                   "replace=true."}
        return out

    @mcp.tool()
    def agreement_manage(
        action: Literal["void", "record_paper"], agreement: str, reason: str | None = None,
        file_url: str | None = None, signer_name: str | None = None,
        paper_signed_on: str | None = None, participant_is_minor: bool = False,
        signer_relationship: Literal["Self", "Parent", "Guardian"] = "Self",
        confirm: bool = False,
    ) -> Any:
        """⚠ Manager actions on one agreement copy (a Customer Agreement name, from
        customer_overview or agreement_request). action:

        * void — `reason` required (sent by mistake, signed by the wrong person). A Pending
          copy's link stops working; the copy is kept, marked Void.
        * record_paper — a Pending copy the customer signed on paper. file_url: the scan
          (PDF or image) ALREADY attached to this Customer Agreement — attach it on the
          agreement's form in the POS first (the local server can also use
          upload_attachment with doctype "Customer Agreement"); it is made private and
          becomes the signed copy. signer_name: the name as signed; paper_signed_on: the date
          on the paper (YYYY-MM-DD, not in the future). A participant under 18
          (participant_is_minor=true) is signed for by a Parent or Guardian
          (signer_relationship).

        Arguments that do not belong to the action are refused. Managers only (Sales
        Manager). Requires confirm=true."""
        agreement = _clean(agreement)
        if not agreement:
            raise ValueError("agreement is required.")
        if action == "void":
            _refuse_stray("action void", {
                "file_url": _clean(file_url), "signer_name": _clean(signer_name),
                "paper_signed_on": _clean(paper_signed_on),
                "participant_is_minor": participant_is_minor,
                "signer_relationship": signer_relationship != "Self"}, ())
            reason = require_reason(f"agreement_manage void {agreement}", reason)
            require_confirm(f"agreement_manage void {agreement}", confirm)
            return get_client().call_method(_AG + "void_agreement",
                                            {"name": agreement, "reason": reason})
        if action != "record_paper":
            raise ValueError(f"action must be void or record_paper (got {action!r}).")
        _refuse_stray("action record_paper", {"reason": _clean(reason)}, ())
        file_url, signer_name, day = _clean(file_url), _clean(signer_name), _clean(paper_signed_on)
        missing = [k for k, v in (("file_url", file_url), ("signer_name", signer_name),
                                  ("paper_signed_on", day)) if not v]
        if missing:
            raise ValueError(f"record_paper needs {', '.join(missing)}.")
        try:
            datetime.date.fromisoformat(day)
        except ValueError:
            raise ValueError(f"paper_signed_on must be YYYY-MM-DD (got {day!r}).") from None
        if signer_relationship not in ("Self", "Parent", "Guardian"):
            raise ValueError("signer_relationship must be Self, Parent or Guardian.")
        if participant_is_minor and signer_relationship == "Self":
            raise ValueError("a participant under 18 is signed for by a Parent or Guardian.")
        require_confirm(f"agreement_manage record_paper {agreement}", confirm)
        return get_client().call_method(_AG + "record_paper_agreement", {
            "name": agreement, "file_url": file_url, "signer_name": signer_name,
            "paper_signed_on": day, "participant_is_minor": 1 if participant_is_minor else 0,
            "signer_relationship": signer_relationship})
