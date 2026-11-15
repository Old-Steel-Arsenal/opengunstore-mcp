"""Marketing tools: the POS's own marketing list (Marketing Contact, its consent status, tags, event log and
back-in-stock / model-alert reminder requests).

Reads: ``marketing_find``, ``marketing_contact``, ``marketing_requests`` with action="list". Writes need
``confirm=true``: ``marketing_tags``, ``marketing_consent``, ``marketing_requests`` close. Every call goes
through a POS endpoint (``ffl_core.api.marketing``) that keeps the marketing rules server-side: consent
only moves forward (an unsubscribed, bounced or complained contact is never put back here), only a
manager may unsubscribe someone, a tag is a slug. Nothing here sends email. A store with marketing
switched off (Marketing Settings › Enabled) refuses every call and says so.

Full surface only. A user whose only POS role is Marketing User can sign in to the full connector and use
these; the POS refuses everything else for them.
"""
from __future__ import annotations

from typing import Any, Literal

from ..frappe_client import get_client
from ..safety import require_confirm

_API = "ffl_core.api.marketing."
Status = Literal["Not Subscribed", "Subscribed", "Unsubscribed", "Bounced", "Complained"]


def _tags(value: list[str] | None) -> list[str]:
    return [t.strip() for t in value or [] if t and t.strip()]


def register(mcp: Any) -> None:
    @mcp.tool()
    def marketing_find(email: str | None = None, name: str | None = None, tag: str | None = None,
                       status: Status | None = None, source: str | None = None, limit: int = 20,
                       start: int = 0) -> Any:
        """Find marketing contacts, newest first: by part of the email, part of the first or last name, a tag
        (exact), the consent status, or where the contact first came from (a prefix: "fluentform" matches
        "fluentform:3"). Returns {total: how many match, contacts: [email, names, status, source, consent
        time and channel, status_changed_at and status_reason, customer, tags]}. Read-only."""
        return get_client().get_method(_API + "find_contacts", {
            "email": email, "name": name, "tag": tag, "status": status, "source": source,
            "limit": min(max(limit, 1), 200), "start": max(start, 0)})

    @mcp.tool()
    def marketing_contact(contact: str, events: int = 20) -> Any:
        """One marketing contact (by email): the record and its tags, whether it is suppressed (unsubscribed,
        bounced or complained: never sent marketing mail), the latest events newest first (subscribed,
        unsubscribed, requested, sent, opened, clicked, ...) and the reminder requests still Open.
        Read-only."""
        return get_client().get_method(_API + "contact_card", {"contact": contact,
                                                               "events": min(max(events, 1), 200)})

    @mcp.tool()
    def marketing_tags(contact: str, add: list[str] | None = None, remove: list[str] | None = None,
                       confirm: bool = False) -> Any:
        """Add tags to a marketing contact and take tags off it. A tag is lowercase letters, digits and
        dashes ("new-arrivals"); anything else is refused and nothing changes. Tags decide who is in an
        audience; they send nothing. Requires confirm=true."""
        add, remove = _tags(add), _tags(remove)
        if not add and not remove:
            raise ValueError("Name a tag to add or to remove.")
        if set(add) & set(remove):
            raise ValueError("A tag cannot be added and removed at once.")
        require_confirm(f"marketing_tags on {contact}", confirm)
        return get_client().call_method(_API + "tag_contact", {"contact": contact, "add": add, "remove": remove})

    @mcp.tool()
    def marketing_consent(contact: str, action: Literal["subscribe", "unsubscribe"], channel: str | None = None,
                          note: str | None = None, confirm: bool = False) -> Any:
        """Change a marketing contact's consent, forward only.
        subscribe: record a consent given outside the website (channel says where: "in store", "phone";
        note is optional). Only a Not Subscribed contact moves to Subscribed; one that unsubscribed, bounced
        or complained is refused (only a manager, on the contact's form in the POS, can put one back).
        unsubscribe: stop marketing mail to the contact (note is the reason, optional). Managers only (Sales
        Manager); a contact already bounced or complained stays as it is.
        Who did it goes on the contact's log. Sends nothing. Requires confirm=true."""
        if action == "subscribe" and not (channel or "").strip():
            raise ValueError("subscribe needs channel: where the consent was given.")
        if action == "unsubscribe" and channel:
            raise ValueError("unsubscribe takes no channel: give the reason as note.")
        require_confirm(f"marketing_consent {action} {contact}", confirm)
        client = get_client()
        if action == "subscribe":
            return client.call_method(_API + "record_consent", {"contact": contact, "channel": channel,
                                                                "note": note})
        return client.call_method(_API + "unsubscribe", {"contact": contact, "reason": note})

    @mcp.tool()
    def marketing_requests(action: Literal["list", "close"] = "list", request: str | None = None,
                           status: Literal["Open", "Notified", "Closed", "all"] = "Open",
                           kind: Literal["Back in stock", "Model alert"] | None = None,
                           contact: str | None = None, item: str | None = None, limit: int = 50,
                           start: int = 0, confirm: bool = False) -> Any:
        """Back-in-stock and model-alert reminder requests: people waiting to hear about a product or a model.
        list (read-only): newest first, Open by default ("all" for every status), by kind, contact (email)
        or POS item.
        close: stop waiting on Open request *request*; it is never notified. A request already Notified or
        Closed is left as it is. Requires confirm=true."""
        client = get_client()
        if action == "list":
            if request:
                raise ValueError("list takes no request: filter by contact, item, kind or status.")
            return client.get_method(_API + "list_requests", {
                "status": status, "kind": kind, "contact": contact, "item": item,
                "limit": min(max(limit, 1), 200), "start": max(start, 0)})
        if not request:
            raise ValueError("close needs request.")
        require_confirm(f"marketing_requests close {request}", confirm)
        return client.call_method(_API + "close_request", {"request": request})
