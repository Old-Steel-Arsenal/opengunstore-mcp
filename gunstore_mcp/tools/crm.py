"""CRM tools: leads and deals in Frappe CRM on the POS, their notes and tasks, and opening a POS order
from a deal.

Reads: ``crm_find``, ``crm_deal``. Writes need ``confirm=true``: ``crm_lead_action``, ``crm_note_task``,
``crm_open_order``. A deal never sells anything: ``crm_open_order`` saves a Create Order draft and
someone finishes the sale on that page, with its usual checks. ``crm_find`` leaves out the store's other
mail (senders who are not customers) unless asked. ``crm_deal`` and ``crm_open_order``'s customer
matches need the store's *Link deals to POS orders* switch on.
"""
from __future__ import annotations

from typing import Any, Literal

from ..frappe_client import get_client
from ..safety import check_fields_writable, require_confirm

LEAD, DEAL, NOTE, TASK = "CRM Lead", "CRM Deal", "FCRM Note", "CRM Task"
DOCTYPE = {"lead": LEAD, "deal": DEAL}
OWNER = {LEAD: "lead_owner", DEAL: "deal_owner"}
OTHER_MAIL = "Other mail"
_API = "ffl_core.api.crm."
_CONVERT = "crm.fcrm.doctype.crm_lead.crm_lead.convert_to_deal"
_FIELDS = {LEAD: ["name", "lead_name", "email", "mobile_no", "status", "lead_owner", "communication_status",
                  "ffl_mail_category", "modified"],
           DEAL: ["name", "lead_name", "email", "mobile_no", "status", "deal_owner", "deal_value",
                  "communication_status", "erpnext_customer", "modified"]}


def register(mcp: Any) -> None:
    @mcp.tool()
    def crm_find(kind: Literal["lead", "deal"] = "deal", email: str | None = None, name: str | None = None,
                 status: str | None = None, owner: str | None = None, waiting_for_reply: bool = False,
                 other_mail: bool = False, limit: int = 20) -> Any:
        """Find CRM leads or deals, newest activity first. By customer email, part of a name, status or
        owner; waiting_for_reply keeps those whose last mail came from the customer. Leads leave out
        other mail (senders who are not customers) unless other_mail=true. Read-only."""
        doctype = DOCTYPE[kind]
        filters: list = []
        if email:
            filters.append([doctype, "email", "=", email.strip().lower()])
        if name:
            filters.append([doctype, "lead_name", "like", f"%{name.strip()}%"])
        if status:
            filters.append([doctype, "status", "=", status])
        if owner:
            filters.append([doctype, OWNER[doctype], "=", owner])
        if waiting_for_reply:
            filters.append([doctype, "communication_status", "=", "Open"])
        if doctype == LEAD and not status:
            filters.append([doctype, "status", "=" if other_mail else "!=", OTHER_MAIL])
        return get_client().list_documents(doctype, fields=_FIELDS[doctype], filters=filters,
                                           limit=min(max(limit, 1), 100), order_by="modified desc")

    @mcp.tool()
    def crm_deal(deal: str) -> Any:
        """One deal: its record, its notes and open tasks, and the customer card (status, alerts, FFL,
        lifetime spend, open and last orders; never an ID, a date of birth or a 4473). Read-only."""
        client = get_client()
        record = client.get_document(DEAL, deal)
        notes = client.list_documents(NOTE, fields=["name", "title", "content", "owner", "creation"],
                                      filters=[[NOTE, "reference_doctype", "=", DEAL],
                                               [NOTE, "reference_docname", "=", deal]],
                                      limit=20, order_by="creation desc")
        tasks = client.list_documents(TASK, fields=["name", "title", "status", "priority", "assigned_to", "due_date"],
                                      filters=[[TASK, "reference_doctype", "=", DEAL],
                                               [TASK, "reference_docname", "=", deal],
                                               [TASK, "status", "not in", ["Done", "Canceled"]]],
                                      limit=20, order_by="creation desc")
        card = client.call_method(_API + "deal_customer_summary", {"deal": deal})
        if isinstance(card, dict):
            card = {key: value for key, value in card.items() if key != "html"}
        return {"deal": record, "notes": notes, "tasks": tasks, "customer": card}

    @mcp.tool()
    def crm_lead_action(action: Literal["convert", "set_status", "set_owner"], name: str,
                        kind: Literal["lead", "deal"] = "lead", status: str | None = None,
                        owner: str | None = None, confirm: bool = False) -> Any:
        """Move a lead or deal: convert a lead to a deal, change the status (a lead off Other mail becomes
        an ordinary lead; Won on a deal is normally set by its invoice), or change the owner. Requires
        confirm=true."""
        doctype = DOCTYPE[kind]
        if action == "convert" and kind != "lead":
            raise ValueError("Only a lead can be converted.")
        if action == "set_status" and not status:
            raise ValueError("set_status needs status.")
        if action == "set_owner" and not owner:
            raise ValueError("set_owner needs owner.")
        require_confirm(f"crm_lead_action {action} {doctype} {name}", confirm)
        client = get_client()
        if action == "convert":
            return client.call_method(_CONVERT, {"lead": name})
        values = {"status": status} if action == "set_status" else {OWNER[doctype]: owner}
        check_fields_writable(doctype, values)
        return client.update_document(doctype, name, values)

    @mcp.tool()
    def crm_note_task(what: Literal["note", "task"], kind: Literal["lead", "deal"], name: str, title: str,
                      content: str | None = None, assigned_to: str | None = None, due_date: str | None = None,
                      priority: Literal["Low", "Medium", "High"] = "Medium", confirm: bool = False) -> Any:
        """Add a note or a task to a lead or deal. A task can name who it is for and a due date. Sends
        nothing to the customer. Requires confirm=true."""
        if not title.strip():
            raise ValueError("A note or task needs a title.")
        if what == "note" and (assigned_to or due_date):
            raise ValueError("A note has no assignee or due date: use what='task'.")
        doctype = DOCTYPE[kind]
        require_confirm(f"crm_note_task {what} on {doctype} {name}", confirm)
        client = get_client()
        if what == "note":
            values = {"title": title, "content": content or "", "reference_doctype": doctype,
                      "reference_docname": name}
            check_fields_writable(NOTE, values)
            return client.create_document(NOTE, values)
        values = {"title": title, "description": content or "", "status": "Todo", "priority": priority,
                  "reference_doctype": doctype, "reference_docname": name}
        if assigned_to:
            values["assigned_to"] = assigned_to
        if due_date:
            values["due_date"] = due_date
        check_fields_writable(TASK, values)
        return client.create_document(TASK, values)

    @mcp.tool()
    def crm_open_order(deal: str, customer: str | None = None, create_customer: bool = False,
                       confirm: bool = False) -> Any:
        """Save a deal as a Create Order draft for someone to finish in the POS. A deal with no customer
        yet returns the matches and saves nothing: call again with customer=<one of them>, or
        create_customer=true. Takes no payment and books nothing. Requires confirm=true."""
        if customer and create_customer:
            raise ValueError("Pick a customer or create one, not both.")
        require_confirm(f"crm_open_order {deal}", confirm)
        client = get_client()
        linked = client.call_method(_API + "ensure_deal_customer", {"deal": deal, "customer": customer,
                                                                    "create": 1 if create_customer else 0})
        if not (isinstance(linked, dict) and linked.get("customer")):
            return linked
        return client.call_method(_API + "open_deal_order", {"deal": deal})
