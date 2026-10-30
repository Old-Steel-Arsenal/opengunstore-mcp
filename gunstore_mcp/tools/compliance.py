"""Compliance repairs, the store briefing, receiving checks, photo galleries, extensions.

The buttons the POS pages offer when the normal flow left something open: a 4473
that never came back, a gun whose bound-book identity drifted from the POS, an A&D
record FastBound never got, a dealer customer's licence, a mistyped serial or cost.
Plus the read-only helpers an agent needs around them (store_overview,
receive_goods_preview, photo_gallery, extensions_status) and the owner's extension
actions.

Every signature below was checked against gunstore-pos `origin/develop` (merge
6e1c546) before being wrapped. The tests are mocked, so drift would only show
against a live POS.

Split by consequence:

* **reads** — need no confirm. `refresh_4473_status` is listed with them on purpose:
  it only restores flags the committed bound-book entries already prove (the counter's
  Refresh 4473 button), so it is NOT a pure read and stays off the cpa surface.
* **writes** — confirm=true. `edit_photo_gallery` and `manage_extension` gate per
  action: rearranging photos is routine, removing one or publishing a private file is
  not; re-checking the extension registry is routine, installing code is not.

Every write method reached here is also exact-name gated for the bare
`frappe_run_method` path (generic._ALWAYS_CONFIRM_METHODS or the verb regex);
`install_upload` is reachable ONLY through manage_extension(action='update').
"""
from __future__ import annotations

from typing import Any, Literal

from ..config import HTTP, get_transport
from ..frappe_client import FrappeAPIError, get_client
from ..safety import WriteRefused, require_confirm, require_reason

_P4473 = "ffl_integrations.fastbound.pos_4473."
_REC4473 = "ffl_integrations.fastbound.pos_4473_recovery."
_FBR = "ffl_integrations.fastbound.reconcile."
_PH = "ffl_core.api.photos."
_EXT = "ffl_core.api.extensions."
_RG = "ffl_core.api.receive_goods."

_MAX_DIFF_SERIALS = 25
_MAX_PREVIEW_ITEMS = 50     # photos.MAX_FILES_PER_CALL is also 50
_MAX_PREVIEW_SERIALS = 100
_MAX_GALLERY_FILES = 50

# refresh_4473_status state -> what the agent does next (pos_4473_recovery.STATE_*).
_NEXT_4473 = {
    "completed": "4473 Completed / NICS Proceed. Finish the sale at the register "
                 "(pos_ticket action='submit').",
    "repaired_from_ledger": "Every gun has a committed Retail Sale disposition; the "
                            "flags were restored. Finish the sale (pos_ticket action='submit').",
    "fastbound_only": "FastBound shows the gun(s) disposed but the POS has no disposition "
                      "for them. If the 4473 really was completed there, a manager runs "
                      "manager_override_4473 with a reason.",
    "not_ready": "The 4473 is not committed in FastBound yet. Finish it there, then "
                 "refresh again.",
    "fb_unreachable": "FastBound could not be reached. Try again in a few minutes; "
                      "nothing was changed.",
    "no_serials": "No serial is picked on the firearm line(s) yet. Start the 4473 "
                  "with start_4473.",
    "no_firearm": "No regulated firearm on this ticket; no 4473 is needed.",
}

# The Gun Shop home page's "Needs attention" cards (gun_shop_cards.CARDS) and the tool
# or report that lists each one's rows.
_CARDS = {
    "pending_orders": "pending_orders + pending_web_orders + gb_pending_orders",
    "pending_4473": "frappe_run_report('Pending 4473 Orders')",
    "transfer_pickups": "frappe_run_report('Pending Transfer Pickups')",
    "to_receive": "distributor_orders / receive_goods",
    "web_orders": "pending_web_orders",
}


def _only(action: str, allowed: tuple[str, ...], **given: Any) -> None:
    """Refuse an argument that does not belong to `action` — a silently dropped
    reason or ref would hide a mistake (same rule as cash_drawer_record_entry)."""
    extra = sorted(k for k, v in given.items() if v is not None and k not in allowed)
    if extra:
        raise ValueError(f"{extra} do not apply to action '{action}' (it takes {list(allowed) or 'none'}).")


def _need(action: str, **given: Any) -> None:
    missing = sorted(k for k, v in given.items() if v is None or (isinstance(v, str) and not v.strip())
                     or (isinstance(v, (list, dict)) and not v))
    if missing:
        raise ValueError(f"action '{action}' needs {missing}.")


def _gallery(doc: dict) -> list[dict]:
    """A document's gallery in publish order, shaped like photos.gallery()."""
    rows = sorted(doc.get("image_gallery") or [],
                  key=lambda r: (0 if r.get("is_primary") else 1, r.get("idx") or 0))
    return [{"name": r.get("name"), "image": r.get("image"),
             "is_primary": 1 if r.get("is_primary") else 0,
             "sort_order": r.get("sort_order"), "caption": r.get("caption") or ""}
            for r in rows]


def register(mcp: Any) -> None:
    # ============================================================== 4473

    @mcp.tool()
    def refresh_4473_status(invoice_name: str) -> Any:
        """Re-check a register ticket's 4473 against the bound book and FastBound (the
        counter's Refresh 4473 button), and say what to do next. Returns the POS answer
        {state, form_4473_status, nics_status, covered, missing, dispositions?, fastbound?}
        plus next_step. state: completed | repaired_from_ledger (every gun has a committed
        Retail Sale disposition, so the stale flags were restored) | fastbound_only |
        not_ready | fb_unreachable | no_serials | no_firearm. It only restores flags the
        committed bound-book entries already prove and never infers NICS, so it needs no
        confirm. invoice_name is a POS Invoice."""
        out = get_client().call_method(_REC4473 + "refresh_4473_status",
                                       {"invoice_name": invoice_name})
        if isinstance(out, dict):
            out = {**out, "next_step": _NEXT_4473.get(out.get("state"), "")}
        return out

    @mcp.tool()
    def resolve_4473(
        invoice_name: str, action: Literal["attest_paper", "redo", "abandon"],
        reason: str | None = None, confirm: bool = False,
    ) -> Any:
        """Close out a 4473 the normal flow left open, on a DRAFT POS Invoice.

        * attest_paper — record that the paper 4473(s) for this ticket were completed at
          the counter and book the Retail Sale disposition per gun. Only in Paper 4473
          Mode, only when the staff member says so (never on inference), and refused
          while an electronic FastBound 4473 is in flight. The POS records the SIGNED-IN
          user as the attesting person, so this is only offered on the remote connector,
          where that user is the staff member; the local server (shop API key) refuses it.
        * redo (manager, reason required) — re-open a COMPLETED 4473: cancels this
          ticket's committed Retail Sale dispositions and clears the 4473 flags so a fresh
          4473 can be started. Does NOT touch FastBound: void the disposition there first.
        * abandon (manager, reason required) — clear an abandoned counter order: deletes
          FastBound's pending disposition for every gun, then deletes the draft ticket.
          Nothing changes if FastBound cannot be reached.
        Requires confirm=true."""
        _only(action, ("reason",) if action in ("redo", "abandon") else (), reason=reason)
        if action != "attest_paper":
            reason = require_reason(f"resolve_4473 {action}", reason)
        require_confirm(f"resolve_4473 {action} {invoice_name}", confirm)
        if action == "attest_paper":
            if get_transport() != HTTP:
                raise WriteRefused(
                    "Refused: attest_paper records the signed-in user as the person who "
                    "attests the paper 4473. On this local server that is the shop's API "
                    "user, not the staff member — attest it at the counter or through the "
                    "remote connector signed in as that person.")
            return get_client().call_method(_P4473 + "attest_paper_4473",
                                             {"invoice_name": invoice_name})
        if action == "redo":
            return get_client().call_method(_REC4473 + "manager_redo_4473",
                                             {"invoice_name": invoice_name, "reason": reason})
        return get_client().call_method(
            _REC4473 + "manager_abandon_4473",
            {"invoice_name": invoice_name, "reason": reason},
            # One FastBound read plus one DELETE per gun, then a re-read.
            timeout=90)

    # ============================================================== FastBound identity

    @mcp.tool()
    def fastbound_serial_diff(serials: list[str]) -> Any:
        """Compare guns' identity (manufacturer, importer, model, caliber, type) between
        the POS and the FastBound bound book, one live FastBound lookup per serial
        (1–25 serials). Returns {serial: {status, fb_item_number?, in_sync?, diffs?:
        [{field, label, pos_value, fb_value}]}}. status: ok | not_in_fastbound (no
        FastBound item linked: fastbound_serial_fix action='match') | not_configured |
        fb_missing | fb_disposed | error (with the message; the other serials still
        run). Pass the diffs to fastbound_serial_fix(action='apply'). Read-only."""
        names = [s.strip() for s in serials or [] if s and s.strip()]
        if not 1 <= len(names) <= _MAX_DIFF_SERIALS:
            raise ValueError(f"Pass 1–{_MAX_DIFF_SERIALS} serials (got {len(names)}).")
        client = get_client()
        out: dict[str, Any] = {}
        for s in dict.fromkeys(names):
            try:
                out[s] = client.call_method(_FBR + "compute_serial_fb_diff", {"serial": s})
            except FrappeAPIError as e:
                out[s] = {"status": "error", "serial": s, "error": str(e)}
        return out

    @mcp.tool()
    def fastbound_serial_fix(
        serial: str, action: Literal["match", "apply", "push"],
        choices: dict[str, Literal["pos", "fb"]] | None = None,
        seen_diffs: list[dict] | None = None, confirm: bool = False,
    ) -> Any:
        """Fix one gun's bound-book identity.

        * match — link a serial with no FastBound item to its item, found by serial
          ({ok, item_id} | {ok, already} | {ok: false, reason: not_found|not_configured}).
        * apply — resolve the differences fastbound_serial_diff reported. choices =
          {field: "pos" | "fb"}: "pos" pushes the POS value to FastBound, "fb" writes the
          FastBound value onto the POS serial. seen_diffs = that serial's `diffs` list
          exactly as fastbound_serial_diff returned it; each choice is sent with the
          fb_value you saw, so a field whose FastBound value changed since is skipped
          (reason "changed"), never overwritten blind. A field not in seen_diffs is
          refused. Returns {ok, applied: {fb, pos}, skipped: [{field, reason}],
          fastbound_updated}.
        * push — push the POS manufacturer / importer to the FastBound item (edit in
          place) after a correction on the serial; {ok: false, reason} when the gun is
          not in FastBound yet.
        Edits the bound book or the serial — confirm=true."""
        _only(action, ("choices", "seen_diffs") if action == "apply" else (),
              choices=choices, seen_diffs=seen_diffs)
        if action == "apply":
            _need(action, choices=choices, seen_diffs=seen_diffs)
            shown = {d.get("field"): d.get("fb_value") for d in seen_diffs if isinstance(d, dict)}
            unknown = sorted(f for f in choices if f not in shown)
            if unknown:
                raise ValueError(f"{unknown} are not in seen_diffs — read fastbound_serial_diff "
                                 "for this serial and choose among the fields it reported.")
            bad = sorted(f for f, c in choices.items() if c not in ("pos", "fb"))
            if bad:
                raise ValueError(f"choices for {bad} must be 'pos' or 'fb'.")
        require_confirm(f"fastbound_serial_fix {action} {serial}", confirm)
        client = get_client()
        if action == "match":
            return client.call_method(_FBR + "match_serial_to_fastbound", {"serial": serial})
        if action == "push":
            return client.call_method("ffl_integrations.fastbound.tasks.push_serial_to_fastbound",
                                      {"serial": serial})
        resolutions = {f: {"choice": c, "fb_value": shown[f]} for f, c in choices.items()}
        return client.call_method(_FBR + "apply_serial_fb_resolution",
                                  {"serial": serial, "resolutions": resolutions})

    @mcp.tool()
    def boundbook_record_fix(
        doctype: Literal["FFL Acquisition", "FFL Disposition"], name: str,
        action: Literal["retry_push", "link_existing", "mark_not_in_book", "clear_mark"],
        fastbound_ref: str | None = None, reason: str | None = None, confirm: bool = False,
    ) -> Any:
        """Clear a submitted A&D record the bound book never got (the list is
        store_overview's ad.records).

        * retry_push — push an FFL Disposition to FastBound again, now ({ok, name,
          fastbound_id, fastbound_sync_error}; ok false carries FastBound's error).
          Disposition only; a Retail Sale (owned by FastBound's 4473) is refused.
        * link_existing — the bound book ALREADY has this event (entered in FastBound,
          or a push that landed but lost its id): link the record to it. fastbound_ref =
          the FastBound item GUID or the item number shown in FastBound (zero-padded,
          e.g. "01171"). Verified live: same serial, and disposed for a Disposition.
          Never retry_push such a record — it would double-book.
        * mark_not_in_book — a person checked it and it does not belong in the bound book
          (e.g. an antique booked by mistake): the daily retry and the overview stop
          listing it. reason required.
        * clear_mark — undo mark_not_in_book; the daily retry pushes it again.
        Requires confirm=true."""
        allowed = {"link_existing": ("fastbound_ref",),
                   "mark_not_in_book": ("reason",)}.get(action, ())
        _only(action, allowed, fastbound_ref=fastbound_ref, reason=reason)
        if action == "retry_push" and doctype != "FFL Disposition":
            raise ValueError("retry_push is for an FFL Disposition only. An FFL Acquisition is "
                             "re-pushed by the daily retry; if FastBound already has it, use "
                             "link_existing.")
        if action == "link_existing":
            _need(action, fastbound_ref=fastbound_ref)
        if action == "mark_not_in_book":
            reason = require_reason("boundbook_record_fix mark_not_in_book", reason)
        require_confirm(f"boundbook_record_fix {action} {doctype} {name}", confirm)
        client = get_client()
        if action == "retry_push":
            # The task runs inline with its retry ladder off; FastBound can still be slow.
            return client.call_method(_FBR + "retry_disposition_push", {"name": name}, timeout=60)
        if action == "link_existing":
            return client.call_method(_FBR + "reconcile_record_to_fastbound",
                                      {"doctype": doctype, "name": name,
                                       "fastbound_ref": fastbound_ref.strip()})
        if action == "mark_not_in_book":
            return client.call_method(_FBR + "mark_not_in_bound_book",
                                      {"doctype": doctype, "name": name, "reason": reason})
        return client.call_method(_FBR + "clear_not_in_bound_book",
                                  {"doctype": doctype, "name": name})

    # ============================================================== FFL licences

    @mcp.tool()
    def verify_customer_ffl(customer_name: str, confirm: bool = False) -> Any:
        """Re-check a dealer customer's FFL with ATF eZ Check and stamp the result
        (status, licensee, expiry) on the Customer. Dispose and dealer shipments need a
        Verified, unexpired licence; a provider outage stamps Failed, which blocks
        shipments to that dealer until re-run. Returns {ok, status, licensee_name,
        expiry_date}. Writes the Customer — confirm=true."""
        require_confirm(f"verify_customer_ffl {customer_name}", confirm)
        return get_client().call_method("ffl_integrations.atf.ez_check_api.verify_customer_ffl",
                                         {"customer_name": customer_name})

    @mcp.tool()
    def ffl_directory_search(query: str, limit: int = 20) -> Any:
        """Search the FFL directory by licence number (exact), business name or city —
        before adding a dealer, receiving a transfer or running atf_verify_ffl. Returns up
        to `limit` (1–200) FFL record dicts; local records (live-verified expiry, phone)
        win over the national listing. Read-only."""
        return get_client().call_method("ffl_integrations.atf.search.search",
                                         {"query": query, "limit": limit})

    # ============================================================== firearm corrections

    @mcp.tool()
    def firearm_costs(item_code: str) -> Any:
        """Per-gun cost of a firearm Item's in-stock, owned serials — the read before
        correct_firearm(action='cost'). Returns {item_code, is_firearm, serials: [{serial,
        warehouse, purchase_rate, valuation, cash_payout, goods_payout}]}; cash_payout
        true means the seller's payment for that gun was recorded in the cash drawer, the
        only case where payout_was_different can book anything. Guns consigned TO the
        store are left out. Read-only; needs a Stock Manager."""
        return get_client().call_method("ffl_core.api.cost_correction.list_item_serials_for_cost",
                                         {"item_code": item_code})

    @mcp.tool()
    def correct_firearm(
        serial: str, action: Literal["cost", "serial_number"], reason: str,
        new_cost: float | None = None, payout_was_different: bool | None = None,
        new_serial: str | None = None, confirm: bool = False,
    ) -> Any:
        """Manager corrections on a gun already booked (Stock Manager; reason required,
        written to the audit trail).

        * cost — fix an in-stock, owned gun's acquisition cost (new_cost > 0): books a
          Stock Reconciliation today. payout_was_different=true ONLY when the seller was
          actually paid a different amount and firearm_costs shows cash_payout: the
          difference is then booked against the same cash or bank account (a journal
          entry). Returns {stock_reconciliation, old_cost, new_cost, delta,
          payout_adjustment, payout_note, ...}.
        * serial_number — fix a mistyped serial on an in-stock, unsold gun: renames the
          Serial No, repoints its A&D record and edits the FastBound item, all or nothing.
          Refused if new_serial already exists or the gun has a committed disposition.
        Requires confirm=true."""
        if action == "cost":
            _only(action, ("new_cost", "payout_was_different"), new_serial=new_serial)
            _need(action, new_cost=new_cost)
            if new_cost <= 0:
                raise ValueError(f"new_cost must be greater than 0 (got {new_cost}).")
        else:
            _only(action, ("new_serial",), new_cost=new_cost,
                  payout_was_different=payout_was_different)
            _need(action, new_serial=new_serial)
        reason = require_reason(f"correct_firearm {action}", reason)
        require_confirm(f"correct_firearm {action} {serial}", confirm)
        if action == "cost":
            return get_client().call_method("ffl_core.api.cost_correction.correct_serial_cost", {
                "serial": serial, "new_cost": new_cost, "reason": reason,
                "payout_was_different": 1 if payout_was_different else 0})
        return get_client().call_method("ffl_core.api.serial_correction.correct_serial", {
            "serial": serial, "new_serial": new_serial.strip(), "reason": reason})

    # ============================================================== briefing / receiving

    @mcp.tool()
    def store_overview(fresh_ad: bool = False) -> Any:
        """The morning briefing (the Gun Shop Overview page): 8 days of sales, open 4473s,
        pickups waiting, stock totals, the cash drawer, the A&D block (ad.records = records
        missing from FastBound — fix them with boundbook_record_fix), arriving distributor
        parcels — plus `cards`, the home page's "Needs attention" counts, each {count,
        list_with: the tool or report that lists its rows}. A block the user may not see is
        null, a block that failed is {error}; a card the user may not open counts 0. Counts
        are shared for up to a minute; fresh_ad=true recounts the A&D block now. web_orders
        is part of pending_orders. Read-only."""
        client = get_client()
        out = client.call_method("ffl_core.api.store_overview.get_overview",
                                 {"with_records": 0, "fresh_ad": 1 if fresh_ad else 0})
        cards: dict[str, Any] = {}
        for key, list_with in _CARDS.items():
            try:
                res = client.call_method("ffl_core.api.gun_shop_cards.get_count",
                                         {"filters": {"card": key}})
                cards[key] = {"count": (res or {}).get("value"), "list_with": list_with}
            except FrappeAPIError as e:
                cards[key] = {"count": None, "error": str(e), "list_with": list_with}
        return {**(out or {}), "cards": cards}

    @mcp.tool()
    def receive_goods_preview(payload: dict | None = None) -> Any:
        """Check a receive_goods payload before booking it. Without payload: {sources (the
        only valid `source` values), payment_methods (how a private seller can be paid)}.
        With payload (the exact receive_goods payload; at most 50 items and 100 serials):
        also seller_payment {pays, amount, methods} — what Submit will pay a private seller;
        show it to the person before confirming receive_goods — items {item_code: the
        lookup_item answer, found false = unknown code} and serials {serial: lookup_serial,
        found true = already on file, a duplicate intake}. Records nothing."""
        client = get_client()
        sources = client.call_method(_RG + "get_receive_sources")
        if payload is None:
            preview = client.call_method(_RG + "seller_payment_preview", {"payload": {"items": []}})
            return {"sources": sources, "payment_methods": (preview or {}).get("methods")}
        lines = payload.get("items") or []
        codes = list(dict.fromkeys(str(i.get("item_code") or "").strip() for i in lines
                                   if isinstance(i, dict) and str(i.get("item_code") or "").strip()))
        serials = list(dict.fromkeys(str(s).strip() for i in lines if isinstance(i, dict)
                                     for s in (i.get("serials") or []) if str(s or "").strip()))
        if len(lines) > _MAX_PREVIEW_ITEMS or len(serials) > _MAX_PREVIEW_SERIALS:
            raise ValueError(f"At most {_MAX_PREVIEW_ITEMS} items and {_MAX_PREVIEW_SERIALS} serials "
                             f"per preview (got {len(lines)} and {len(serials)}).")
        return {
            "sources": sources,
            "source_valid": payload.get("source") in (sources or []),
            "seller_payment": client.call_method(_RG + "seller_payment_preview",
                                                 {"payload": payload}),
            "items": {c: client.call_method(_RG + "lookup_item", {"scan": c}) for c in codes},
            "serials": {s: client.call_method("ffl_core.api.firearm_intake.lookup_serial",
                                              {"serial": s}) for s in serials},
        }

    # ============================================================== photos

    @mcp.tool()
    def photo_gallery(
        item_code: str, serial_no: str | None = None, in_stock_only: bool = True,
        search: str | None = None,
    ) -> Any:
        """A gun's or an Item's photo gallery in publish order (primary first), each row
        {name, image, is_primary, sort_order, caption}; `name` is the row_name
        edit_photo_gallery takes. With serial_no: that gun's gallery. Without: the Item's
        own gallery plus `serials` — each of its serials (in stock only unless
        in_stock_only=false; `search` filters serial numbers) with photo_count and a few
        thumbs, so you can see which guns still need photos. Read-only."""
        client = get_client()
        if serial_no:
            doc = client.get_document("Serial No", serial_no) or {}
            return {"doctype": "Serial No", "name": serial_no, "item_code": doc.get("item_code"),
                    "image": doc.get("image"), "gallery": _gallery(doc)}
        doc = client.get_document("Item", item_code) or {}
        return {
            "doctype": "Item", "name": item_code, "image": doc.get("image"),
            "gallery": _gallery(doc),
            "serials": client.call_method(_PH + "serials_for_item", {
                "item_code": item_code, "in_stock_only": 1 if in_stock_only else 0,
                "search": search or ""}),
        }

    @mcp.tool()
    def edit_photo_gallery(
        doctype: Literal["Item", "Serial No"], name: str,
        action: Literal["add", "reorder", "set_primary", "set_caption", "remove"],
        file_urls: list[str] | None = None, row_names: list[str] | None = None,
        row_name: str | None = None, caption: str | None = None, confirm: bool = False,
    ) -> Any:
        """Arrange a gun's (Serial No) or an Item's photos. Read the rows with
        photo_gallery first. Returns the gallery in publish order.

        * add — file_urls (max 50): files ALREADY on the POS (uploaded in Desk, or with
          upload_attachment locally). Adding makes the file PUBLIC, so a /private/ URL
          needs confirm=true. A URL already in the gallery is skipped; the first photo of
          an empty gallery becomes primary.
        * reorder — row_names: every current row name exactly once, in the new order.
        * set_primary — row_name becomes the featured photo.
        * set_caption — row_name's caption (also its alt text; blank clears it).
        * remove — row_name leaves the gallery, and its file is deleted when nothing else
          uses it. confirm=true.
        A change on a gun listed on the web store re-sends it there automatically."""
        allowed = {"add": ("file_urls",), "reorder": ("row_names",),
                   "set_primary": ("row_name",), "set_caption": ("row_name", "caption"),
                   "remove": ("row_name",)}[action]
        _only(action, allowed, file_urls=file_urls, row_names=row_names, row_name=row_name,
              caption=caption)
        base = {"doctype": doctype, "name": name}
        if action == "add":
            urls = [u.strip() for u in file_urls or [] if u and u.strip()]
            if not 1 <= len(urls) <= _MAX_GALLERY_FILES:
                raise ValueError(f"Pass 1–{_MAX_GALLERY_FILES} file_urls (got {len(urls)}).")
            private = [u for u in urls if u.startswith("/private/")]
            if private:
                require_confirm(f"edit_photo_gallery add makes {private} public", confirm)
            return get_client().call_method(_PH + "add_gallery_images", {**base, "file_urls": urls})
        if action == "reorder":
            _need(action, row_names=row_names)
            if len(set(row_names)) != len(row_names):
                raise ValueError("row_names lists a row more than once; list every row exactly once.")
            return get_client().call_method(_PH + "reorder_gallery", {**base, "row_names": row_names})
        _need(action, row_name=row_name)
        if action == "set_primary":
            return get_client().call_method(_PH + "set_primary_image", {**base, "row_name": row_name})
        if action == "set_caption":
            return get_client().call_method(_PH + "set_image_caption",
                                        {**base, "row_name": row_name, "caption": caption or ""})
        require_confirm(f"edit_photo_gallery remove {doctype} {name} {row_name}", confirm)
        return get_client().call_method(_PH + "remove_gallery_image", {**base, "row_name": row_name})

    # ============================================================== extensions

    @mcp.tool()
    def extensions_status(extension: str | None = None) -> Any:
        """Installed POS extensions: {core_version, extensions: [{name, title, version,
        enabled, effective (on AND installed AND compatible), compatible,
        incompatible_reason, permissions, high_risk, job_state, job_message, update (null or
        {version, compatible, reason})}], busy, restart, update_service}. With `extension`
        also `detail`: its release_notes, disable_notice (what turning it off interrupts;
        "" = nothing) and uninstall_preview (what uninstalling removes; blocked = it holds
        records, so only turning it off is possible). Does not re-check the update service
        (manage_extension action='refresh' does). To uninstall: frappe_run_method(
        'ffl_core.api.extensions.uninstall_from_site', {extension, confirm_id: <the same
        extension id>}, confirm=true). Read-only; System Manager."""
        client = get_client()
        out = client.get_method(_EXT + "list_extensions")
        if extension:
            args = {"extension": extension}
            out = {**(out or {}), "detail": {
                "extension": extension,
                "release_notes": client.get_method(_EXT + "release_notes", args),
                "disable_notice": client.get_method(_EXT + "disable_notice", args),
                "uninstall_preview": client.get_method(_EXT + "uninstall_preview", args),
            }}
        return out

    @mcp.tool()
    def manage_extension(
        action: Literal["refresh", "enable", "disable", "install", "update_preview", "update"],
        extension: str | None = None, accept_permissions: list[str] | None = None,
        confirm: bool = False,
    ) -> Any:
        """Owner actions on POS extensions (System Manager).

        * refresh — re-read the server and ask the update service what is newer; returns
          the extensions_status shape plus sync / updates. A stuck job is marked Failed.
        * enable / disable — turn an installed extension on or off for the store. Read
          extensions_status(extension) first and show the person its disable_notice.
          confirm=true.
        * install — install an extension that is on the server but not on this store
          (background job; poll extensions_status). confirm=true.
        * update_preview — download the newer version the update service offers and check
          it: {extension, from, to, ok, errors, allowed, reason, release_notes,
          permissions_added, permissions_removed, high_risk_added}. Changes nothing
          installed. Show the person the release notes and the new permissions (high-risk
          ones especially) before updating.
        * update — fetches the package again and installs it only if it passes the checks,
          the upgrade is allowed, and accept_permissions lists EXACTLY its
          permissions_added (pass [] when there are none). The store restarts (1–3
          minutes); poll extensions_status. confirm=true.
        Uninstalling is not here — see extensions_status."""
        _only(action, ("accept_permissions",) if action == "update" else (),
              accept_permissions=accept_permissions)
        if action == "refresh":
            if extension is not None:
                raise ValueError("refresh re-checks every extension; it takes no extension.")
            return get_client().call_method(_EXT + "refresh")
        _need(action, extension=extension)
        if action in ("enable", "disable"):
            require_confirm(f"manage_extension {action} {extension}", confirm)
            return get_client().call_method(_EXT + "set_enabled", {
                "extension": extension, "enabled": 1 if action == "enable" else 0})
        if action == "install":
            require_confirm(f"manage_extension install {extension}", confirm)
            return get_client().call_method(_EXT + "install_on_site", {"extension": extension})
        if action == "update":
            if accept_permissions is None:
                raise ValueError("update needs accept_permissions: the permissions_added list "
                                 "from update_preview, exactly ([] when there are none).")
            require_confirm(f"manage_extension update {extension}", confirm)
        client = get_client()
        # Downloads the package (the server checks it against the announced sha256).
        report = client.call_method(_EXT + "fetch_update", {"extension": extension},
                                    timeout=120) or {}
        upgrade = report.get("upgrade") or {}
        added = list(upgrade.get("permissions_added") or [])
        summary = {
            "extension": extension, "from": upgrade.get("from"), "to": upgrade.get("to"),
            "ok": bool(report.get("ok")), "errors": report.get("errors"),
            "allowed": bool(upgrade.get("allowed")), "reason": upgrade.get("reason") or "",
            "release_notes": upgrade.get("release_notes") or "",
            "permissions_added": added,
            "permissions_removed": list(upgrade.get("permissions_removed") or []),
            "high_risk_added": [p for p in added if p in set(report.get("high_risk") or [])],
        }
        if action == "update_preview":
            return summary
        if not upgrade:
            raise WriteRefused(f"Refused: {extension} is not on this server, so this is not an update.")
        if not summary["ok"] or not summary["allowed"]:
            raise WriteRefused(f"Refused: {extension} {summary['to']} cannot be installed "
                               f"({summary['reason'] or str(summary['errors']) + ' check errors'}). "
                               "Nothing was changed.")
        if set(accept_permissions) != set(added):
            raise WriteRefused(
                f"Refused: {extension} {summary['to']} asks for new permissions {added}, but "
                f"accept_permissions was {sorted(accept_permissions)}. Show the person the new "
                "permissions and pass exactly those once they agree. Nothing was changed.")
        # The sha256 comes from this fetch only — never from the caller.
        result = client.call_method(_EXT + "install_upload",
                                    {"sha256": report.get("sha256"), "confirm_id": extension})
        return {**summary, "result": result}
