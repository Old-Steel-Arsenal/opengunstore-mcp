# OpenGunstore MCP: tool reference

This reference is organised by what you want to do. Each tool is marked read or write,
says whether it needs `confirm=true`, and lists the key parameters. The README has the
setup instructions and a compact tool table. The source under `gunstore_mcp/tools/` is
the authority on exact signatures.

## Read this first

1. **Writes are real.** The server acts on whatever POS site `FRAPPE_BASE_URL` points at.
   Listing a gun makes it purchasable, and a disposition writes to the firearms
   acquisition and disposition book. Try new workflows against a dev site first.
2. **The `confirm` mechanism.** An operation with consequences is refused on the first call
   and succeeds only when the call is repeated with `confirm=true`. An assistant should
   restate what it is about to do and get the user's agreement before repeating the call.
3. **Credentials never travel through this MCP.** Password and API-key fields are stripped
   on write and are never returned on read. Change secrets in the POS Desk (My Settings,
   or the relevant Settings page).

Every Woo-related tool takes an optional `site` argument; the only value is `retail`
(the default). The separate dealer storefront was retired with POS 1.11.0.

---

## 1. Look things up (all read-only)

| Task | Tool | Notes |
|---|---|---|
| Find an item by name, barcode or SKU | `find_item` | Keyword in, matching Items out |
| Stock for one or more items | `item_stock` | Accepts several item codes |
| Every in-stock gun of a model, with prices | `available_serials` | `{model: [{serial, sell_price, ...}]}`, cheapest first. Consigned-out and held guns are excluded by default (same rule the counter uses when picking a gun); pass `exclude_unavailable=false` for the full list |
| All firearms in stock | `firearms_in_stock` | Serial, manufacturer, model, caliber, warehouse, source, bound-book link; filter by warehouse or manufacturer |
| Pending counter and dealer orders | `pending_orders` | Orders not yet disposed, not fully paid, or not yet pushed to ShipStation. Consignments are not here; they have their own queue (section 5) |
| Paid web orders waiting for disposition | `pending_web_orders` | Paid Woo orders awaiting `dispose_web_order` |
| Consignment queues | `consignment_queue`, `consignment_dealer_orders` | See section 5 |
| Financial and tax reports | `sales_report`, `gl_entries`, `financial_statement`, `tax_liability`, `ar_ap_summary`, `inventory_receipts`, `payroc_transactions` | Accountant report kit, available in every mode; see section 10 |
| Search the RSR wholesale catalog | `rsr_catalog_search` | By keyword, UPC, RSR stock number or manufacturer part number. This is not store inventory |
| The morning briefing (sales, open 4473s, pickups, stock, drawer, A&D gaps, arriving parcels) and the home page's "needs attention" counts | `store_overview` | Each count names the tool or report that lists its rows. `fresh_ad=true` recounts the A&D block now |
| A customer's standing, alerts, family, IDs and agreements | `customer_overview` | See section 4c |
| The gunsmith queue, memberships | `gunsmith_jobs`, `memberships` | See section 4d. `memberships(view=customer)` is not purely a read: it activates a sign-up whose invoice was since paid another way |
| Search the FFL directory | `ffl_directory_search` | By licence number (exact), business name or city; local records win over the national listing |
| Run any report | `frappe_run_report` | e.g. `Sales Report` (revenue and margin; `filters.view` is `Order`, `Order Detail` or `Product`), `Pending 4473 Orders`, `Pending Transfer Pickups` |
| Read any record | `frappe_list_documents`, `frappe_get_document` | General-purpose queries; see section 9 |

## 2. Listing and delisting on WooCommerce

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| List or update **one gun** | `woo_push_serial` | yes | Pushed by serial number; SKU is `item_code::serial`. Touches only that gun, so it is the everyday choice |
| Delist **one gun** | `woo_delist_serial` | yes | Product becomes a draft and stock goes to zero |
| List or update **every in-stock gun of a model** (or a plain product) | `woo_push_item` | yes | Pushes **every** Active serial under the model |
| Delist a whole model | `woo_delist_item` | yes | |
| First full listing (model plus all serials) | `woo_reconcile` | yes | |
| Give one gun its own listing title | `set_serial_title` | no | Writes `Serial No.item_name`; takes effect on the next push |
| Check the store connection | `woo_test_connection` | no | Read-only probe |
| Store-wide sweeps: re-push firearm attributes, re-file categories, find IDs pointing at deleted products | `woo_store_maintenance(action=backfill_attributes\|recategorize\|audit_dangling_ids)` | only with `apply=true` | `apply=false` (default) reports only. Applying `backfill_attributes` needs `serials` or `item_codes` (it is synchronous). System Manager |
| See a gun's or a model's photo gallery | `photo_gallery` | no | Rows in publish order with the `row_name` the edit takes; for a model, which of its guns still need photos |
| Arrange photos | `edit_photo_gallery(action=add\|reorder\|set_primary\|set_caption\|remove)` | `remove`, and `add` of a `/private/` file | `add` takes files already on the POS and makes them public. On the remote connector a person uploads the files in Desk first |

**Photos.** `upload_attachment` uploads one file at a time (local server only). Product images meant for the
storefront must be uploaded with `is_private=false`, or added with `edit_photo_gallery`, which makes them public. The server does not resize images; resize
large photos before uploading, because oversized originals can make the push time out.

## 2b. GunBroker (a third sales channel)

Fixed-price Buy Now listings, priced from `Serial No.sell_price`. Listings are per gun;
there is no "push a whole model" operation.

6 always-registered GunBroker tools: the reads `gb_test_connection`, `gb_listing_status` and
`gb_pending_orders`, and the order-side writes `gb_record_payment`, `gb_order_action` and
`gb_dispose_order`. The 4 GunBroker write actions that change a listing offer
(`gb_push_serial`, `gb_end_listing`, `gb_update_listing`, `gb_pull_orders`) are **not
registered at all** unless the server is started with `GUNSTORE_MCP_GUNBROKER_ACTIONS=1`.
`confirm=` stops a slip of the finger, but it cannot stop an agent that has talked itself
into confirming. A tool that is absent from the list cannot be talked into anything.

Push, end and pull-orders share one switch on purpose. An instance that can list a gun but
cannot end the listing is in the most dangerous position: the gun sells at the counter, the
assistant has no tool to end the listing, and a second buyer can still buy it.
`gb_pull_orders` sits behind the same switch because importing an order creates POS
documents and reserves the gun. `gb_update_listing` changes a live offer (price, photos,
quantity, the "may already be sold" guard), so it is behind it too.

The order-side tools stay outside the switch: `gb_record_payment`, `gb_order_action`
(including `push_tracking`, which writes tracking to GunBroker, and `assign_unit`, which
reserves the gun and announces its exit) and `gb_dispose_order` fulfil an order that is
already sold. They never create, change or end a listing offer, and they all take `confirm`.

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| List **one gun** | `gb_push_serial` | yes (and switch on) | A guard refusal returns `{"ok": false, "skipped": ..., "message": ...}`. That is a normal answer, not an error; act on the message, as retrying will not change it |
| End **one gun's** listing | `gb_end_listing` | yes (and switch on) | Check `confirmed`, not `ok`. `confirmed=false` always comes with `pending_manual` and `gb_url`, meaning the gun can still be bought on GunBroker and someone must end it on the site |
| Listing status of one gun | `gb_listing_status` | no | `state` is the POS view (seven states); `remote` is what GunBroker reports right now |
| Check the GunBroker connection | `gb_test_connection` | no | The `sandbox` field in the reply says which environment answered |
| Change a live listing: reprice, replace photos, sync a model's quantity, lift the "may already be sold" flag | `gb_update_listing(action, serial_no\|item_code)` | yes (and switch on) | `sync_quantity` at 0 ends the listing. `clear_sold_unknown` needs a `note` saying what was checked on GunBroker. `replace_pictures` needs SYSTEM_ROLES |
| Open GunBroker orders (the Pending Order page's third lane) | `gb_pending_orders(gunbroker_order?)` | no | One card per order: stage, paid vs total, units still needing a gun, C&R prefill, destination FFL, invoice state. With `gunbroker_order` also the stock lines for `gb_dispose_order`. Also on the cpa surface |
| Record the payment banked for an order | `gb_record_payment` | yes | `GunBroker Check` (needs `reference_no`) or `GunBroker Card`; `received_on` is the bank date (YYYY-MM-DD). This is what opens the Dispose gate |
| Assign or release a unit's gun, mark a unit refunded, push the shipment or tracking, retry the invoice | `gb_order_action` | yes | Unit actions take the card's `row_name`. `assign_unit` reserves the gun |
| **Dispose** a paid GunBroker order | `gb_dispose_order` | yes | One FFL Transfer disposition per gun plus the FastBound push, then the revenue invoice. Check the destination FFL and serials on the card first; Type 03 orders need `cr_details` |
| Run an order poll now | `gb_pull_orders` | yes (and switch on) | No arguments. The reply is a receipt, not a result: `{"queued": true}` only says the job was queued (a poll already running is joined, not duplicated) and carries no order count. Look at the GunBroker Order list and the Error Log for the outcome. Moving the poll's start point (`orders_since_override`) is only possible in the Desk, because winding it back re-imports old orders and re-reserves guns |

**Sandbox versus live cannot be chosen from here.** It is decided solely by
`GunBroker Settings.sandbox_mode` on the target POS site, and that field cannot be written
through any MCP path. The ten keys `enabled`, `sandbox_mode`, `base_url_override`,
`dev_key`, `sandbox_dev_key`, `username`, `password`, `end_strategy`,
`check_deposit_account` and `card_checkout_enabled` cause the **whole call to be refused**
when any of them is present in `update_settings`, `frappe_update_document`,
`frappe_create_document` or the field setters reachable through `frappe_run_method`. The
call is not partially applied, because a half-applied write is worse than none. These
fields are changed by a person in the Desk. `tests/test_never_writes_surface.py` feeds the
forbidden keys to every registered tool and statically checks that any new tool that
writes a document body goes through the same guard.

All GunBroker tools go through whitelisted POS methods; the MCP never talks to GunBroker
directly.

**A local site is not necessarily a sandbox.** Pointing the MCP at `dev.localhost:8000`
gives you that site's `sandbox_mode`. A dev site configured with live credentials can
create real listings. Check the `sandbox` field of `gb_test_connection` before the first
`gb_push_serial`.

**Roles differ.** `gb_test_connection` and `gb_pull_orders` need the POS `SYSTEM_ROLES`;
the other three need only `STOCK_ROLES`. An API user with stock roles only will get a 403
on just those two tools. Since the connection probe is the usual first call, a 403 there
usually means a role gap, not a broken connection.

Re-listing a gun that was ended by hand is not offered through the MCP; use the Serial No form,
where the reason it was ended is visible.

## 3. Receiving goods and adjusting stock

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| Check a receive before booking it | `receive_goods_preview(payload?)` | no | Without a payload: the valid `source` values and seller payment methods. With the exact `receive_goods` payload: what Submit will pay a private seller, unknown item codes, serials already on file |
| Buy a gun from a walk-in seller and pay them now | `trade_in_intake` | yes | Makes the gun's Used Item, books the acquisition and FastBound push, receives the stock and pays the seller (Cash comes out of the drawer; other methods need `payout_reference`). Store credit inside a cart is not offered |
| Receive a **distributor** parcel | `distributor_incoming`, then `distributor_receive` | booking only | See section 9b. Use this, not `receive_goods`, for anything a distributor shipped, or the waiting order never gets its gun |
| Receive goods (including firearms) | `receive_goods` | yes | Creates and submits a Purchase Receipt; each firearm gets an FFL Acquisition and is pushed to FastBound. Firearms must come in this way, not through `add_stock`. A paid purchase from a private seller (`acquisition_source="Individual"` with a blank, `Purchase` or `Individual` type and a cost) needs `seller_payment_method` (`Cash`, `Zelle`, `Check` or `ACH`) plus `seller_payment_reference` unless Cash. The POS books the payment in the same transaction and returns `seller_payment`; without those fields the receipt is refused. Consignment, gunsmithing, transfers and dealer purchases take no payment |
| Add stock for a plain product | `add_stock` | yes | Ammunition, accessories and other non-serialized items |
| Set a counted quantity | `set_stock` | yes | Leaves an audit record with the reason |
| Flag or clear "needs gunsmith" | `toggle_service_need` | yes | Opens or closes the gunsmith ToDo |
| Turn an RSR catalog row into a sellable Item | `promote_to_item` | yes | Manufacturer, model, caliber and image carried over |
| Fill an Item's empty fields from RSR data | `backfill_from_rsr` | yes | Fills blanks only; never overwrites |
| Per-gun cost of a model's in-stock guns | `firearm_costs` | no | Read before `correct_firearm(action='cost')`; `cash_payout` says whether a payout difference can be booked. Stock Manager |
| Correct a gun's acquisition cost or a mistyped serial | `correct_firearm(action=cost\|serial_number)` | yes, with `reason` | `cost` books a Stock Reconciliation (and, with `payout_was_different`, the payout difference); `serial_number` renames the Serial No, its A&D record and the FastBound item together. Stock Manager |

## 3b. Stocktake, cash drawer, storage locations (`tools/shopfloor.py`)

Every write needs `confirm=true`. There is deliberately **no** registration-time switch,
unlike the GunBroker and distributor actions: these are the routine actions the POS pages
offer to staff with the matching roles, the POS checks the role on each call, the remote
connector writes an audit row for each call, and most of them can be undone. Two actions
cannot be reversed: `cash_drawer_close_day` (POS shifts it closes are not reopened by an
undo) and `inventory_count_finalize` (a posted Stock Reconciliation can only be cancelled in
the Desk).

### Inventory count

A count only **reports** firearms and never adjusts them (no disposition, no bound-book
change); a missing gun has to be investigated. Only non-serialized items are adjusted at
finalize.

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| List counts | `inventory_counts` | no | Latest 50 with status, scope and scan count; on the cpa surface |
| Progress of one count | `inventory_count_state` | no | Scanned guns and quantities, unknown barcodes, expected list; `counts_only=true` returns scans only |
| Variance | `inventory_count_variance` | no | `serial.{missing,unexpected,unknown}` is report-only; `items[]` is what finalize will adjust; rows with `open_register_sales` wait until the register shift closes; on the cpa surface |
| Start a count | `inventory_count_create` | yes | No scope means the whole store; the warehouse must be one of the company's own stock warehouses |
| Scan | `inventory_count_scan` | yes | A serial number (one gun, once) or a UPC (+1, needs `warehouse`). `result=error` means nothing was stored (it is a reply, not an exception); the returned `entry` is what undo takes |
| Type a quantity | `inventory_count_set_qty` | yes | For items without barcodes; stored as a delta so other devices' scans are kept; entering 0 counts as having counted it |
| Tick or untick a gun by hand | `inventory_count_toggle_serial` | yes | For unreadable labels; removing someone else's scan needs Stock Manager |
| Undo one scan | `inventory_count_undo` | yes | Own scans, or Stock Manager |
| Void a count | `inventory_count_cancel` | yes | Adjusts nothing, keeps the scans; Stock Manager |
| **Finalize** | `inventory_count_finalize` | yes | Posts **one** Stock Reconciliation that sets non-serialized items to the counted quantity. `item_rows=[{item_code, warehouse}]` selects rows; omitted means every row with a variance **except** rows nobody scanned (those become 0 only when explicitly listed). Read the variance and confirm with the user first. Refused, leaving no trace, when there are no scans, the period is frozen or a register shift is open |

### Cash drawer

Expected cash is the Cash account balance plus the cash of unconsolidated POS invoices.

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| Today's drawer | `cash_drawer_today` | no | Expected cash, lines since the last close (with `can_undo`), thresholds, expense cap, selectable expense accounts |
| Preview a close | `cash_drawer_preview_close` | no | Computes without posting: expected, variance, `needs_reason`, `first_count` |
| Daily closes | `cash_drawer_closes` | no | Excludes undone closes by default; on the cpa surface |
| Drawer entries | `cash_drawer_entries` | no | Deposits, cash from bank, expenses, seller payments; Posted only by default; on the cpa surface |
| Cash movement log | `cash_drawer_log(from_date, to_date)` | no | Every cash movement with who handled it, the document, running balance, opening and closing balance and totals; default last 7 days, at most one year; on the cpa surface |
| Weekly accountant report | `cash_drawer_weekly(from_date, to_date)` | no | The Cash Drawer Weekly report as is, with a reconciliation block (difference must be 0.00, unclassified rows should be empty); at most 400 days; on the cpa surface |
| **Close the day** | `cash_drawer_close_day` | yes | Counts the drawer, closes POS shifts and books the over/short entry. The company's first count instead adjusts the books to actual cash (CASH-CUTOFF). A difference at or above the configured threshold (default $20) needs a `reason`. Preview first and pass the previewed `expected` as `expected_seen` (refused if the books moved) |
| Deposit, cash from bank, expense | `cash_drawer_record_entry(kind=deposit\|from_bank\|expense)` | yes | An expense needs `expense_account` and `memo`, has a cap (default $200) and only allowed accounts; `receipt` is optional (POS 1.8.2+). Parameters that do not belong to the kind are rejected, not silently dropped |
| Undo | `cash_drawer_undo(entry\|close)` | yes | Exactly one argument: undo one entry (refused if a count came after it; undoing a seller payment needs System Manager) or the **latest** close (shifts are not reopened). Manager |
| Re-book an old-style seller payment | `cash_drawer_record_payout(acquisition, method)` | yes | Changes the method of a pre-1.8.4 seller payment or re-books an undone one; cannot create new ones (since 1.8.4 sellers are paid with the receipt). Manager |

**Expense receipts.** When a `receipt` is passed, the server requires a file URL that the
same POS user uploaded within the last day, that is still private, that is attached to no
document and that no other expense has used. The remote connector has no
`upload_attachment`, so the user uploads the image in the POS and passes the resulting
`file_url`. The local stdio server can use `upload_attachment(file_path, is_private=true)`
without `doctype`/`name`. A receipt that fails validation is rejected by the POS and nothing is booked.

### Storage locations

These tools touch only the tracking layer: no stock or accounting documents are created and
no stock or ledger entries change. A Slots zone has numbered slots (A1, A2, ...), one gun per slot; an
Open zone is a single named location (shelf, case, safe) with no capacity limit.

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| Store map | `storage_map` | no | Contents of each zone and position, `unassigned`, and `plans` (floor plan per room, in feet; drawn in the POS); `zones_only=true` lists zones only (cheap) |
| Contents of a location | `storage_location` | no | The location name is also its barcode |
| Where is a gun or item | `storage_where` | no | `serial_no` returns its `storage_location`; `item_codes` returns quantity per location, quantity not yet placed, and `sole` |
| Unplaced and to-confirm | `storage_unassigned` | no | `to_confirm` lists items sold or shipped without saying which location they came from |
| Create a zone | `storage_create_zone` | yes | `kind` is Slots (`count` slots) or Open (one location; `count` ignored). Slots zones accept `sides` (2 for a double-sided rack; 0 or omitted follows the drawn shape; rejected for Open zones) and `numbering` (`Odd / even` default, or `In order`; only with two sides). Needs POS 1.8.5 or newer (older POS versions silently ignore both). Stock Manager |
| Add slots | `storage_add_positions` | yes | Never renumbers or removes; refused for Open zones |
| Disable or enable a zone or location | `storage_set_disabled` | yes | Exactly one of `zone` or `location`; disabling requires it to be empty |
| Place a gun or item | `storage_scan_move` | yes | Both "assign a serial number" and "put a quantity in" (`code` is a serial or UPC, plus `qty`). If the source is ambiguous nothing moves and `result=choose` lists `options`; repeat with `from_location`. `error` is a reply, not an exception |
| Undo a move | `storage_undo_move` | yes | Manual moves only, one step, and only while the thing is still where it was put; `ok=false` is a reply |
| Confirm where sold units came from | `storage_confirm_taken` | yes | The only action that takes a quantity out of a location; cannot exceed the pending amount |

## 3c. Range board (`tools/range_board.py`): 6 Range tools, three that read and three that write

Only for a store running the **Range** extension (osa_range, paid, on stores Old Steel Arsenal
hosts): registered when `GUNSTORE_MCP_RANGE=1` (the POS deploy's `MCP_RANGE`), otherwise
absent. Each call is the counter's own board endpoint, so the POS checks that Range is on,
that the user holds a counter role, and every booking rule. Days are `YYYY-MM-DD`, times
`HH:MM` (24-hour, the store's clock).

| Tool | What it does |
|---|---|
| `range_board(date?)` | One day: hours, lanes by area, services, every booking (who, what, when, lanes, note, `moved_note` = tell the customer), block reasons. Read-only. |
| `range_free_lanes(date, start, end, service?, exclude_lanes?)` | Lanes free then (cleaning included), only in the service's areas. Read-only. |
| `range_find_bookings(customer?, date_from?, date_to?, include_cancelled?)` | A customer's (or everyone's) bookings, earliest first; `truncated` when the POS hit its limit. Read-only. |
| `range_book(kind, date, start, end, lanes, …, confirm)` | Reservation (customer, service, party_size), Custom Slot (customer, custom_title, custom_price) or Block (block_reason). Never moves another booking to make room: a clash is refused naming the booking in the way. `confirm=true`. |
| `range_update(booking, start?, end?, lanes?, note?, party_size?, confirm)` | Change time on the same day, lanes, note or people; re-reads the booking first. `confirm=true`. |
| `range_cancel(booking, reason, confirm)` | Cancel with a reason; it stays in the bookings list. `confirm=true`. |

The three board writes are also in `frappe_run_method`'s always-confirm list.

## 4. Orders, payment and shipping (the Pending Order queue)

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| Record a payment on an unpaid order | `record_payment` | yes | Omitting the amount pays the balance. Modes: Cash, Credit Card, Zelle, ACH, Check/Money Order. Zelle and ACH need `transaction_number`, Check/Money Order its check or money order number there; when Payroc is enabled a Credit Card payment taken on the virtual terminal needs the authorization code |
| See the card charges on an invoice that can be given back | `card_charges` | no | Each charge's id (`terminal_payment`), amount, refunded, left, card, gateway reference, whether it can be voided, and whether a void/refund is waiting to be confirmed. Says if it is a membership invoice and why a Return is not possible |
| Void a card charge | `void_card_charge` | yes | The whole charge, before the processor settles it (usually the same day). The invoice owes the amount again. Not for membership invoices or a charge already partly refunded; a settled charge is refused by the processor — refund it instead. Managers only |
| Refund a card charge to its card | `refund_card_charge` | yes | Any part of what is left. `refund_type` `Return` (goods given back or price lowered: a credit note takes it off the sale, tax in proportion; not for invoices that moved stock or touched the bound book, membership invoices only after the membership is cancelled) or `Collect again` (the sale stands; the invoice owes it again). Managers only |
| Settle a void or refund the processor never confirmed | `resolve_card_reversal` | yes | Check the processor's portal first. `went_through=true` books it (needs `portal_reference`), `false` unlocks the charge. Managers only |
| **Dispose** the guns on a counter or dealer order | `dispose_order` | yes | Books the transfer disposition per gun: stock out and push to FastBound. The server refuses unpaid orders and an invalid receiving FFL. Check the FFL and serial numbers before running it |
| **Dispose** the guns on a web order | `dispose_web_order` | yes | Web orders are not pushed to ShipStation (the Woo plugin ships them itself) |
| Push an order to ShipStation for a label | `push_shipment` | yes | Idempotent; fails safe on an invalid FFL or unpaid order |
| Mark shipped without ShipStation | `mark_shipped_manually` | yes | Fallback when the label was bought elsewhere or the integration is off; refused while guns are not yet disposed |
| **Cancel a counter or dealer transfer order** | `cancel_order` | yes, with `reason` | Safe cascade: reverses the disposition, reverses stock, queues the bound-book deletion, voids the ShipStation shipment and refunds what was collected (`refund_mode`, `refund_reference`). Use this instead of reversing documents by hand |
| Check the ShipStation connection | `shipstation_test_connection` | no | Read-only probe |

| Web-order steps beyond dispose: hand a pickup order to the counter, retry its revenue invoice, reconcile a partial Woo refund, retire an antique order, run the revenue backstop, retry stuck serial reservations | `web_order_action` | yes | `pickup` opens a paid register ticket for a firearm order; finish it with `start_4473` and `pos_ticket(submit)` |
| Month-end web checks | `web_order_exceptions` | no | Web invoices whose stock never left, and orders shipped in WooCommerce but not disposed here. Also on the cpa surface |

Typical flow: `pending_orders` to see the queue, `record_payment` if money is short,
`dispose_order`, then `push_shipment`. For web orders: `pending_web_orders`, then
`dispose_web_order` (no shipment push).

## 4b. Counter orders and register tickets (`tools/orders.py`)

The Create Order page, the Pending Order buttons and the register. None of these is on the
cpa surface: they read and write customer and ticket data.

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| Check an order before taking it | `preview_order` | no | Same `items` shape as `create_order`: whether each serial is free and its tag price, line costs, tax rate per item, whether a price below list needs a manager, the customer's tax exemption. With `sales_invoice` it shows an existing order's editable form |
| Take a counter, phone or dealer transfer order | `create_order` | yes (not for `save_as_draft=true`) | Books the Sales Order and Sales Invoice and, with `payment`, the payment. A firearm order needs `destination_ffl`. `sku`-only lines get their Item made on create. Zelle, ACH and Check/Money Order need `transaction_number` |
| Edit a pending order | `update_order` | yes | Reads the current order, replaces only the keys in `changes` and sends the whole order back (cancel-and-rebuild, only before disposition or shipment push). `payment` and `cr_license` cannot be changed here |
| The Pending Order buttons: serve a late line from stock, re-source a stalled line, open the pickup ticket, resend the agreement | `pending_order_action` | yes | Releasing distributor legs stays on `frappe_run_method` with confirm |
| Tickets parked at the register | `pos_draft_orders` | no | What each still needs: due, card status, held, paid online, 4473 and NICS status |
| Finish a register ticket | `pos_ticket(action=open\|tender\|start_card\|poll_card\|cancel_card\|submit)` | yes, except `poll_card` | `open` needs an open register shift. Credit Card goes through the terminal when a processor is live. `submit` runs the POS's own 4473, price-approval and deposit guards; a return's card refund runs once |
| Charge a card on the counter terminal against a submitted Sales Invoice | `card_terminal_charge(action=start\|poll\|cancel)` | `start` and `cancel` | A second `start` returns the charge already waiting. `{skip: true}` means no processor is live: use the standalone terminal and `record_payment` |
| A customer's cards on file | `saved_cards` | no | Brand, last four, expiry; never the token |
| Charge a card on file with no customer present | `charge_saved_card` | yes | Managers only. If the answer is lost, call again with the same arguments: the POS looks the charge up instead of charging twice |

`pos_ticket` needs a POS release that ships `ffl_core.api.pos_ticket.open_ticket` (for
`open`) and `ffl_integrations.payments.api.card_tender_mode` (for card tenders); an older
POS answers those steps with its own "method not found" error.

## 4c. Customers and agreements (`tools/customers.py`)

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| Check a customer before a sale, trade-in or transfer | `customer_overview(customer, cart?\|sale?)` | no | Status (banned, or served under a manager's override), alerts, tags, family, expired IDs, agreements; with a cart or a sale, which agreements it still needs signed |
| Find existing customers | `customer_find` | no | By email, phone, ID number, or first and last name together; up to 10 rows with what matched |
| Merge a duplicate, override a ban, set the status, add a staff alert | `customer_manage(action=merge\|override_ban\|set_status\|add_alert)` | yes | Manager actions. A merge is irreversible: compare both with `customer_overview` first |
| Send an agreement to sign | `agreement_request(deliver=email\|tablet\|none)` | yes | Never returns the signing link or QR code. `frappe_run_method` refuses `request_agreement` without one of these deliveries |
| Void an agreement copy, or record a paper signature | `agreement_manage(action=void\|record_paper)` | yes | Managers only. `record_paper` takes a scan already attached to the Customer Agreement |

## 4d. Gunsmith and memberships (`tools/services.py`)

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| The gunsmith queue, or one job | `gunsmith_jobs(search?\|work_order?)` | no | What each job owes, deposits held, open register tickets, whether the work authorization is signed; one job says whether an ID is on file for the bound book |
| Take a gun in for repair | `gunsmith_intake` | yes | Opens the work order. `log_in_bound_book=true` also books it in the A&D book now (needs caliber, firearm type and the customer's ID). No payment is taken here |
| Move a job: start, ready, notify, take a payment, pick up, cancel, refund the deposit, log in the bound book | `gunsmith_job_action` | yes | `take_payment` and `pickup` leave a register ticket: finish it with `pos_ticket`. `take_payment` needs a POS release with `ffl_core.gunsmith.open_deposit`; it returns `{pos_invoice, already, amount}` (an already-open deposit ticket comes back at its own amount) and is refused while the job's pick-up ticket is open |
| Memberships: plans and totals, the roster, one customer's | `memberships(view=overview\|members\|customer)` | no | `view=customer` can settle a sign-up that was paid another way, so this tool is never on a read-only surface |
| Sell and manage memberships | `membership_action` | yes | `start`, `invoice`, `invoice_renewal`, `retry_card`, `set_auto_renew`, `change_card`, `cancel`, `drop` (discard a Pending sign-up). Take the invoice's payment with `record_payment` or `card_terminal_charge` |

## 4e. Staff, attendance and leave (`tools/hr.py`)

Needs the Frappe HR (`hrms`) app on the site; without it every tool answers "Frappe HR (hrms) is not installed on this site". The scope is attendance, shifts and leave. Payroll is not covered. The reads never write; every write needs `confirm=true`.

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| Who works here, and who has clocked in | `hr_staff` | no | Active employees: shift, holiday list, leave approver, `restricted_to_employees` (the Employee ids a User Permission limits that login to; `[]` = sees everyone) and today's last check-in. Birth date, address, bank and other personal fields are never returned |
| Check-ins and Attendance by day | `hr_attendance(from_date, to_date, employee?)` | no | Range capped at 62 days. Per employee: the days with check-ins and Attendance, plus totals. Draft Attendance is shown but not counted |
| Leave balances, or what is waiting for a decision | `hr_leave(view=balances\|pending, employee?)` | no | `balances` is hrms's own `get_leave_details` per employee, valid today. `pending` lists Open Leave Applications (the employee's reason text is not returned) |
| Hours per employee for a period | `hr_hours_summary(from_date, to_date)` | no | Also in cpa mode. Totals count submitted Attendance only; `checkin_days_without_attendance` > 0 means the hours are low until auto-attendance has run for those days |
| Approve or reject a leave request | `hr_leave_decide(leave_application, decision=approve\|reject, reason?)` | yes | Only an Open application. A reject needs a reason. Sets the status and submits; if the submit fails the status goes back to Open. The reason is stored as a Comment (hrms has no field for it) |
| Fix a missed punch, or mark a day | `hr_attendance_fix(action=add_checkin\|mark_attendance, employee, reason, ...)` | yes | `add_checkin` needs `time` + `log_type`; `mark_attendance` needs `attendance_date` + `status` (Present, Absent, Half Day, Work From Home), optional `working_hours`. Refused if that day already has an Attendance (the message says how to correct it in Desk). Leave is not marked here: approve the Leave Application. Stray arguments are refused; the reason is stored as a Comment |
| Make an existing login an employee | `hr_onboard` | yes | Creates the Employee, a Leave Policy Assignment for the joining-anniversary year that contains today, and picks shift / holiday list / policy when there is only one. Staff get a User Permission limiting them to their own Employee; System Manager and HR Manager logins do not. Never edits the user's Role Profile: if it lacks the roles `Employee` / `Employee Self Service` the result warns, because a profile sync would strip them |

## 5. Consignment out (the whole At Dealer lifecycle)

Consignments have their own queue and flow and do **not** use the Pending Order queue of
section 4.

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| In-transit consignments | `consignment_queue` | no | One card per consignment: dealer eligibility, ShipStation status, tracking, gun pills; `include_closed=true` adds history |
| Dealers that can receive consignments | `consignment_dealers` | no | All FFL dealer customers with `shippable` and `block_reason` (an expired FFL is flagged) |
| Guns that can be consigned | `consignment_serials` | no | Active in-stock serials with settlement and reference prices; unselectable guns are returned with the reason |
| Create a consignment | `create_consignment_out` | yes | Payload `{dealer, lines: [{item_code, serial, cost, msrp}], dispose_now?}`. Saved as a draft by default; `dispose_now=1` disposes and pushes to ShipStation at once (a blocked FFL gate degrades to a kept draft) |
| **Dispose (ship)** a consignment | `ship_consignment_out` | yes | Books an FFL transfer disposition per gun, moves stock and pushes to FastBound. Validates every line before touching any, idempotent. Check the dealer FFL and serials first |
| Push to ShipStation | `push_consignment_shipment` | yes | Disposed consignments only; idempotent, leaves nothing half-done on failure |
| Record shipment manually | `mark_consignment_shipped` | yes | May carry `tracking_number` and `carrier`, which the dealer portal shows |
| **Change prices** of guns out on consignment | `update_consignment_prices` | yes | `prices: {row_name: {cost (required, >0), msrp}}`; for `msrp`, a missing key leaves it, an empty value clears it, otherwise it must be >0. Changes only this document's line snapshot, not the Serial No or Item master; settlement and the portal follow. Only At Dealer lines with no settlement invoice yet; validated as a batch before any write. Row names come from `consignment_queue` |
| Settlement queue (sold, not yet paid) | `consignment_dealer_orders` | no | Sold lines whose settlement invoice is missing or unpaid; failures first |
| Retry a failed settlement invoice | `retry_consignment_invoice` | yes | Takes the Consignment Out Line name from the settlement queue |
| Take back unsold guns | `return_consignment_lines` | yes | Books a real re-acquisition per gun (pushed to FastBound) and moves stock back to the main warehouse |
| Cancel a consignment (whole draft or one wrongly shipped line) | `cancel_consignment` | yes, with `reason` | Without `line` it cancels the whole draft; with `line` it cancels one shipped line (the gun never left the store), only before settlement |

**Settlement is automatic.** When the dealer marks a line Sold in the portal, the system
issues the settlement invoice (a failure lands in the settlement queue for retry). To undo a
settlement, cancel the settlement Sales Invoice with `frappe_cancel_document`; the cancel
hook reopens the parent document symmetrically (Closed back to Shipped). There is no
dedicated endpoint.

**Acting for the dealer is not possible.** `mark_received` and `mark_sold` are bound to the
portal dealer's session identity and are refused when called with an admin key (section 11).

## 6. Compliance (FastBound, ATF)

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| Start a 4473 for a counter firearm sale | `start_4473` | yes | The invoice is held while the form is completed in FastBound; the argument maps invoice lines to serial numbers |
| **Unstick**: the 4473 is complete in FastBound but the order is stuck | `manager_override_4473` | yes | Needs manager rights and a reason; creates the Retail Sale disposition (flagged `manual_override` for audit). Does **not** push back to FastBound, so reconcile the bound book separately |
| Start a 4473 for a customer transfer in (transfer fee) | `start_transfer_4473` | yes | Server creates the POS invoice with a $0 gun line and a transfer-fee line; look up the fee with `frappe_run_method` on `ffl_core.firearm.get_transfer_config` |
| Verify an FFL number (eZ Check) | `atf_verify_ffl` | yes | Verifies online and stores or updates the ATF FFL Record |
| Verify a supplier's FFL | `verify_supplier_ffl` | yes | Also updates the verification status on the supplier |
| Re-verify every FFL supplier | `reverify_all_ffls` | yes | Bulk |
| Verify a **customer's** (dealer's) FFL | `verify_customer_ffl` | yes | Stamps the eZ Check result on the Customer; dispose and dealer shipments need a Verified, unexpired licence |
| Re-check a register ticket's 4473 and get the next step | `refresh_4473_status` | no | Only restores flags the committed bound-book entries already prove. It writes those flags, so it is never on a read-only surface |
| Close out a 4473 the normal flow left open | `resolve_4473(action=attest_paper\|redo\|abandon)` | yes | `attest_paper` (Paper 4473 Mode) records the signed-in user as the attester, so it runs on the remote connector only. `redo` and `abandon` are manager actions with a reason |
| Compare guns' identity with the bound book | `fastbound_serial_diff` | no | 1 to 25 serials, one live FastBound lookup each |
| Fix one gun's bound-book identity | `fastbound_serial_fix(action=match\|apply\|push)` | yes | `apply` takes the diffs `fastbound_serial_diff` returned, so a value that changed since is skipped. `push` sends a corrected manufacturer or importer to FastBound |
| Clear a submitted A&D record the bound book never got | `boundbook_record_fix(action=retry_push\|link_existing\|mark_not_in_book\|clear_mark)` | yes | The list is `store_overview`'s `ad.records`. Never `retry_push` a record the book already has: `link_existing` it |
| Reconcile: disposed in FastBound but still in stock here | `boundbook_reconcile` | only with `apply=true` | Reports only by default; does not change stock |
| Report guns disposed in FastBound but still in stock here (read-only) | `boundbook_mismatches` | no | The `boundbook_reconcile` dry run as its own tool: the POS method has no write path, so it is also available in cpa mode. Needs a POS release with `inventory_sync.boundbook_mismatches` |
| Check the FastBound connection | `fastbound_test_connection` | no | Read-only |

## 7. Distributor catalogs

Catalog and stock feeds are synchronised server-side through the data service configured in
Data Service Settings (for both RSR and Sports South). A local catalog can be re-synced now
with `frappe_run_method('ffl_integrations.distributor.hub.sync_now', ...)` (System Manager).
Probe a distributor with `distributor_test_connection`; catalog health is on the Catalog
Service page in the Desk.

## 8. Settings and files

| Task | Tool | Confirm | Notes |
|---|---|---|---|
| Read an integration's configuration | `get_settings` | no | `ffl`, `fastbound`, `rsr`, `payroc`, `woocommerce`, `shipstation`, `gunbroker`, `sports_south`, `lipseys` or `data_service` |
| Change configuration (non-secret fields) | `update_settings` | no | Password and key fields are stripped; change those in the Desk. `FFL Settings.store_customer_id_numbers` (switching it off erases stored IDs) and `Payroc Settings.enabled` (use `payments.hub.toggle`) are refused on every write path |
| Why the card terminal will not charge | `card_processor_test_connection` | no | Read-only. Each processor's switch vs readiness, a live probe, and optionally the terminals the active processor may use |
| Installed POS extensions, their updates, what turning one off interrupts | `extensions_status(extension?)` | no | Read-only. System Manager |
| Refresh, enable, disable, install or update an extension | `manage_extension` | `enable`, `disable`, `install`, `update` | System Manager. `refresh` and `update_preview` change nothing installed. An update is two steps: `update_preview` shows the new and high-risk permissions, and `update` runs only when `accept_permissions` echoes them. Uninstall is `frappe_run_method('ffl_core.api.extensions.uninstall_from_site', {extension, confirm_id: extension}, confirm=true)` |
| Upload a local file to the POS | `upload_attachment` | no | Can attach to a record (`doctype` + `name`) or fill an Attach field. Private by default; images used on the storefront need `is_private=false`. Local stdio server only |

## 9. The generic `frappe_*` tools

Anything not covered above is reachable through the generic tools, so new POS features are
usable the day they ship, without an MCP update.

- `frappe_list_documents`, `frappe_get_document`, `frappe_describe_doctype`: read any doctype (describe first to see field names).
- `frappe_create_document`, `frappe_update_document`: create or change any record (credential fields stripped).
- `frappe_delete_document`, `frappe_submit_document`, `frappe_cancel_document`: delete, submit, cancel (all need `confirm`).
- `list_pos_actions` (read-only): search every POS endpoint the signed-in user can run (core apps and installed extensions): arguments, HTTP verb, roles, whether `frappe_run_method` will ask for `confirm`, and `use_tool` when a dedicated tool does the job. Needs a POS release with `ffl_core.api.action_catalog.list_actions`; registered wherever `frappe_run_method` is (never cpa).
- `frappe_run_method`: call any whitelisted method by dotted path. Sent as GET when the action catalog lists the method as GET-only (cached 10 minutes per method), otherwise POST, including on a POS without the catalog. A method name containing a high-consequence verb (delete, cancel, refund, dispose, push, charge, consolidate, ship, return, receive, sold, settle, onboard and similar) needs `confirm`. An explicit list of methods without such a verb but with consequences (`_ALWAYS_CONFIRM_METHODS`: every write a dedicated tool wraps, plus order, consignment, payment, stock, tax, stocktake, cash drawer, storage, gunsmith, membership, 4473, bound-book, GunBroker, Woo and extension writes) needs `confirm` as well. The test for inclusion is: it books money, moves stock, or changes compliance, tax or a live listing. Refused outright: credential, signing-chain, terminal-pairing, staff-PIN, price-approval PIN, sideload, webhook and audit endpoints; the legacy `ffl_integrations.payroc.api.*` aliases (call `payments.api`); `receive_from_do` (use `distributor_receive`), `backfill_condition_attributes` (use `woo_store_maintenance`), `request_agreement` without `deliver` email/tablet/none (use `agreement_request`) and `sync_in_stock_from_boundbook` applied without `item_ids` (use `boundbook_reconcile`).
- `frappe_run_report`: run any report.

Handy dotted paths for operations without a dedicated tool (all via `frappe_run_method`):

| Scenario | Dotted path |
|---|---|
| Manually consolidate a stuck POS invoice (when a gun does not leave stock) | `ffl_core.api.pos_consolidate.consolidate_pos_invoice_now` (confirm) |
| Delete an Item | `ffl_core.api.item_admin.preview_delete`, then `force_delete` (confirm). It does **not** keep the A&D trail: it deletes non-submitted acquisitions and dispositions and raw-deletes ledger rows |
| Read or set a customer's tax-exempt status | `ffl_core.api.manual_order.get_customer_tax_status`, `set_customer_tax_exempt` (confirm) |
| Onboard a consignment dealer (FFL lookup, then Customer plus portal account) | `osa_consignment.api.dealer_onboarding.lookup_ffl`, then `onboard_dealer` (confirm) |
| Turn a card processor on or off | `ffl_integrations.payments.hub.toggle` (confirm; refuses a second active processor) |
| Place a counter order's Draft distributor legs (a real purchase) | `ffl_core.api.manual_order.release_order_legs` (confirm) |
| Undo a consignment settlement | cancel the settlement Sales Invoice with `frappe_cancel_document` |

## 9b. Distributor direct-ship (RSR Direct Connect, Sports South): `distributor_*`, 16 tools (12 always registered + 4 queue actions)

Nine read-only tools, three confirm-gated always-registered writes (draft order, receive, ensure item) and four confirm-gated queue actions. It deliberately excludes
placing an order directly, writes to Settings, and any mutation surface beyond what is
listed here. The four queue actions are registered only when
`GUNSTORE_MCP_DISTRIBUTOR_ACTIONS=1` is set, and never on the remote connector. "Always registered" means in `full` mode; cpa mode registers none of the writes.

| Tool | Server method | Notes |
|---|---|---|
| `distributor_orders` | `distributor.api.list_orders` | List Distributor Orders, filterable by status and distributor |
| `distributor_route_queue` | `distributor.router.route_queue` | **Confirmation queue**: Draft orders waiting for confirmation, plus web orders that were held back (unpaid, buyer FFL missing or expired, incomplete address) with the reason and the destination FFL's expiry. Read this before confirming anything |
| `distributor_catalog_search` | `distributor.api.search` | Catalog typeahead over the locally synced catalog (not live stock); spans every enabled distributor when none is given |
| `distributor_test_connection` | `distributor.hub.test_connection` | Probe one distributor: data-service catalog health plus ordering API credentials. Read-only; `ok: null` means not configured or not applicable, which is not a failure |
| `distributor_quote` | `distributor.api.quote` | Per-item cost, MAP, MSRP, suggested price, restricted states, block flags; quantity comes from the cached catalog and is not guaranteed fresh |
| `distributor_check_availability` | `distributor.api.check_availability` | **Live** quantity and price re-check (makes an HTTP call to the distributor; read-only) |
| `distributor_precheck_fds` | `distributor.router.precheck_fds` | Asks the distributor whether it will ship to that transfer dealer (HTTP call; read-only) |
| `distributor_fulfillment_options` | `distributor.options.fulfillment_options_for_order` | Decision panel for one web order: candidate distributors per line (cheapest first), a local-stock comparison row and a verdict. Read-only. **Read the three-state note below before using the result** |
| `distributor_create_draft_order` | `distributor.api.create_orders` | Needs `confirm`. Draft restock order(s) from one distributor, shipped to the store; lines are `{sku, qty, item_code?, upc?}`. A line with no Item gets the shop's existing Item, else one made from the catalog. May split into several drafts; places nothing. Not idempotent: after a failed call, check `distributor_orders` for Drafts before retrying. Registered on the remote connector too |
| `distributor_incoming` | `distributor.receive_bridge.list_incoming` / `match_incoming` | Parcels on their way and what is outstanding on each, grouped by the order they are for; with `item_code`, which waiting lines a unit that just arrived could belong to. Read-only. Stock Manager |
| `distributor_receive` | `distributor.receive_bridge.receive_lines` / `receive_invoice_lines` / `stage_invoice` | Receive a parcel against its Distributor Order or store invoice. Without `items` it prepares (no confirm; staging an invoice can create local Items); with `items` it books stock and the acquisitions and hands the gun to the waiting order (confirm) |
| `distributor_ensure_item` | `distributor.api.ensure_dropship_item` | Needs `confirm`. Find or create the store Item for a distributor SKU; `dropship=true` also marks it drop-shipped by that house, which changes web routing |
| `distributor_confirm_order` | `distributor.api.confirm_order` | Needs `confirm`. Draft to Queued and starts the ordering worker, which is **a real, irreversible purchase**; RSR has no cancel API. Counter-origin orders also require the invoice to be paid in full |
| `distributor_cancel_order` | `distributor.api.cancel_order` | Needs `confirm` and a `reason`. Safe only before ordering (Draft or Queued); an order already placed is flagged and operations are alerted, because returns are a manual process |
| `distributor_reroute` | `distributor.router.reroute` | Needs `confirm`. Re-runs routing after the blocking reason is fixed; only creates Drafts, idempotent |
| `distributor_update_order_ffl` | `distributor.router.update_order_ffl` | Needs `confirm`. The standard fix for an FDS hold; the new licence passes the same canonical validation chain as the counter (not expired, complete address) before the distributor is called |

### Return contract of `distributor_fulfillment_options`

**Two fields are three-state, and `if not x` is wrong for them.** `null` means "cannot
tell yet", not "fine":

| Field | Values | Meaning of `null` |
|---|---|---|
| `verdict.fulfillable` | `true`, `false`, `null` | Cannot be decided; see the sibling `reason`: `unknown_destination` (the destination state is not captured yet, the normal case while an order is in Pending Route or the buyer's FFL is not uploaded) or `stale_feed` (the only candidate with enough quantity comes from an out-of-date feed) |
| `restricted_state` | `true`, `false`, `null` | Same: the restricted-state check cannot be made yet |

Treat any `null` as a **hold**: say it is undecided, quote the `reason`, and do not say the
line can ship. A restricted item with an unresolved destination is exactly the case that
must never be let through.

**Candidate flags.** `not_carried` means the distributor's catalog does not have the item
(listed, not dropped, so it is not misread as out of stock). `blocked` means in stock but not
purchasable (distributor block or manufacturer approval needed). `stale` means the quantity
comes from an out-of-date feed. Unbuyable rows sort last, so the first candidate is the most
likely to be actionable, but the row's own `verdict` and flags decide; the ordering is not
permission.

**Amounts.** `unit_landed` is per unit and covers the goods only. Shipping does not exist
before an order is placed, so `shipping` is `null` and `shipping_known` is `false`. Do not
present `unit_landed` as a landed cost, and do not multiply it by quantity as the final cost.

## 10. CPA mode (read-only accountant surface) and the report kit

**Mode switch.** Set `GUNSTORE_MCP_MODE=cpa` at startup. The default `full` mode offers all
181 tools, of which 167 register by default (the 4 distributor queue actions, the 4
GunBroker write actions and the 6 Range tools need explicit opt-in); the behaviour of `full` is unchanged. An
unknown value refuses to start rather than silently degrading to a writable surface. In cpa
mode the **write surface does not exist in the tool list**; it is not "present but
refusing". There are three layers of defence, and each still holds if another fails:

1. **Registration layer.** `tools/list` is exactly the 30 names below (set equality, pinned by tests).
2. **Client layer.** Every write method and every dotted-path method not individually listed is refused with `CpaModeRefused` (`frappe_run_method` is not registered at all). The read-only dotted-path allowlist names each method; wildcards are not allowed.
3. **Settings layer.** `get`/`list` reads of the seven integration Settings doctypes are blocked too (configuration is useless to an accountant; password masking is framework behaviour, not something this repo guarantees).

**The 30 cpa-mode tools:**
- Generic reads (4): `frappe_list_documents`, `frappe_get_document`, `frappe_describe_doctype`, `frappe_run_report`.
- Business reads (12): `find_item`, `item_stock`, `firearms_in_stock`, `pending_orders`, `pending_web_orders`, `web_order_exceptions`, `gb_pending_orders`, `consignment_queue`, `consignment_dealers`, `consignment_serials`, `consignment_dealer_orders`, `boundbook_mismatches` (the dry-run-only bound-book reconcile; `boundbook_reconcile` can apply, so it stays full-mode only).
- Stocktake and cash drawer reads (6, see section 3b; also available in `full`): `cash_drawer_closes`, `cash_drawer_entries`, `cash_drawer_log`, `cash_drawer_weekly`, `inventory_counts`, `inventory_count_variance`. None of the writes is included. The two stocktake reads need a Stock role on the API user; the four drawer reads need only Accounts User.
- `boundbook_mismatches` reads the whole FastBound book (one slow call, 130 s timeout). The POS allows it for System Manager, Sales User/Manager, Stock User/Manager and Accounts User.
- Report kit (7, below; also available in `full`).
- `hr_hours_summary` (section 4e): hours per employee, names only. It uses plain REST reads of Employee, Employee Check-in and Attendance, so the method allowlist gains nothing; the API user needs read access to those three. The other `hr_*` reads and all the writes stay full-mode only.

`memberships` and `refresh_4473_status` look like reads but write as a side effect, so they
are listed in `modes.NOT_PURE_READS` and may never join the cpa surface.

`available_serials` is **not** in cpa mode. It hides consigned-out and held guns by default,
which would hide guns during a count; use `firearms_in_stock` for that.

**Remote connector (OAuth, no API key).** With `GUNSTORE_MCP_TRANSPORT=http` the server is
an OAuth resource server for the POS. A user enters the URL in claude.ai or Claude Code,
signs in to the POS in the browser and approves. **Each call runs with the signed-in user's
own POS roles.** Only tokens issued to Claude connectors (dynamically registered clients)
are accepted, and every call is written to the POS Connector Audit Log permanently (who,
which connector, which tool, which arguments with secrets masked, success or failure; if the row
cannot be written the tool does not run). The three cpa layers apply unchanged. The full
surface is 166 tools remotely (`upload_attachment` reads a path on the server and is never
registered remotely). The distributor queue actions never open remotely; `distributor_create_draft_order` does. The four GunBroker
write actions open on the full connector only, where the POS deployment sets the switch
from that store's own GunBroker setting. See the README, "Remote connector".

**Accounting basis.** ERPNext is the business system and the data source for these
reports. Whether it is also the book of record depends on the company. `sales_report`,
`inventory_receipts`, `tax_liability` and the standard Stock Balance report are the
dependable extracts. Treat `financial_statement` and `ar_ap_summary` as reference when
expenses and supplier invoices are kept in another ledger.

**Report kit** (read-only, registered in both modes):

| Task | Tool | Notes |
|---|---|---|
| Revenue and margin for a period | `sales_report(from_date, to_date, view="Product", channel?, product_type?)` | The Sales Report passed through unchanged (including summary cards). `view`: Order, Order Detail or Product; `channel`: POS, Web or Manual |
| What entered stock in a period | `inventory_receipts(from_date, to_date, view="Units", category?, receipt_class?, supplier?, include_transfers=False, include_custody=False)` | The Inventory Receipts report as is. Read straight from the stock ledger (`actual_qty>0`), so Cost Received reconciles with Stock In Hand. `view`: Units (default, one row per unit), Summary (category by receipt type) or Receipts (Desk tree, rows carry `indent`). Classes: Purchase, Trade-in, Consignment, Intake, Return, Adjustment, Revaluation (a cost correction: zero units, booked as an inventory adjustment). Inter-warehouse transfers and customer-custody guns are left out unless the switch is set or the class is named. Rows flagged No cost or No A&D |
| Trace general-ledger detail | `gl_entries(from_date, to_date, account?, party?, voucher_no?, voucher_type?, limit=500)` | Always `is_cancelled=0` (cancelled and amended vouchers drop out). Truncation is explicit (`truncated:true`), never silent; `limit` is clamped to 1..5000 (0 or empty means 500), page by date range for more. Single-company basis |
| Financial statements | `financial_statement(statement, from_date, to_date, periodicity="Monthly")` | `statement`: `pnl`, `balance_sheet` or `trial_balance`. P&L and balance sheet use Date Range; Trial Balance needs dates in one Fiscal Year (resolved automatically, a range spanning years is refused) |
| Sales-tax liability roll-forward | `tax_liability(from_date, to_date)` | Opening, collected, remitted, closing per voucher; unusual vouchers are listed separately (fail-closed). Accounts are resolved from the default sales-tax template. Note that an invoice's "Total Taxes and Charges" includes shipping, so it is not the sales tax |
| Payroc card transactions reconciled to the POS | `payroc_transactions(from_date, to_date)` | Reads the Payroc gateway live and lists every transaction in the date range (type SALE or REFUND; statuses such as COMPLETE, READY, DECLINED, VOID; portal refunds and voids included), matched row by row on `orderId` to the POS (counter: POS or Sales Invoice name; web: Woo order number). `flags` marks mismatches: no POS record, amount differs, POS says paid but the gateway voided, refund not recorded in the POS. `pos_only` lists POS records the date search did not return (looked up individually). `summary` counts only money actually held (COMPLETE and READY). Card type and last 4 digits only. At most 31 days per call; if `truncated:true`, split the range. The API user needs System Manager, Accounts Manager or Accounts User. Needs a POS release with `payroc/ledger.py`. Read-only |
| Aged receivables or payables | `ar_ap_summary(kind, as_on_date)` | `kind`: `ar` or `ap`. Posting Date basis, 30/60/90/120 buckets; consignment settlement receivables are listed per dealer. `ap` is only meaningful when purchases are recorded as Purchase Invoices in ERPNext, so treat it as reference |

**Standard reports often used for month-end and tax work** (run with `frappe_run_report`; filter keys checked against the ERPNext v16 source):

| Report | Key filters |
|---|---|
| `Sales Register` | company, from_date, to_date, customer, warehouse, mode_of_payment, item_group |
| `General Ledger` | company, from_date, to_date, account, party_type + party, voucher_no, categorize_by |
| `Stock Balance` | company, from_date, to_date, item_code, item_group, warehouse |
| `Accounts Receivable` / `Accounts Payable` | company, report_date, ageing_based_on ("Posting Date" or "Due Date"), range ("30, 60, 90, 120") |
| `Trial Balance` | company, **fiscal_year (required)**, from_date, to_date |

A dedicated `stock_valuation` tool is deliberately not provided: for a year-end inventory
tie-out, run `frappe_run_report("Stock Balance", ...)`.

## 11. What this MCP cannot do (use another route)

| Cannot do | Alternative |
|---|---|
| Change password or API-key fields | Change them in the Desk (by design, to prevent leaks) |
| Upload gun photos in bulk | Use a script that resizes images first (for example the `firearm-listing-import` skill's script); arrange the gallery with `edit_photo_gallery` |
| Change doctype structure, permissions or roles | Do it in code and migrations; the MCP write denylist blocks it |
| Approve a price below list with another manager's PIN or password | A manager-connected agent approves its own price; anyone else asks a manager at the counter (credentials never pass through the MCP) |
| Collect a customer's signature, pair a signing tablet, switch staff with a PIN | Done by a person on the device (`agreement_request` sends the request) |
| View a signed 4473 PDF | Read the Form 4473 Log metadata and give a person the Desk link (each view is logged as a human view) |
| Install an extension from an uploaded package or a git URL | Only the publisher-checked update path (`manage_extension(action='update')`) is offered |
| Take a trade-in as store credit inside a cart | Not offered yet; `trade_in_intake` pays the seller now |
| Fill in the 4473 form itself in FastBound | Use the FastBound web UI (the form can only be completed there) |
| Mark received or sold on behalf of a consignment dealer (`mark_received`, `mark_sold`) | These methods are bound to the portal dealer's session identity and refuse admin-key calls; the dealer does it in the portal |
| Trigger the ShipStation tracking poll manually | It runs on a schedule (`poll_consignment_tracking` is not a whitelisted method); check the ShipStation dashboard for an urgent lookup |

---

*Total tools: 181 (11 generic + 69 dedicated + 16 distributor + 7 report tools + 6 Range tools + 10 counter-order tools + 5 customer tools + 5 gunsmith/membership tools + 15 compliance and store-admin tools + 7 HR tools + 30 shop-floor). 167 register by default: the 4 distributor queue actions need `GUNSTORE_MCP_DISTRIBUTOR_ACTIONS=1`, the 4 GunBroker write actions need `GUNSTORE_MCP_GUNBROKER_ACTIONS=1` and the 6 Range tools need `GUNSTORE_MCP_RANGE=1`. `GUNSTORE_MCP_MODE=cpa` registers exactly 30 of them. Matches version v0.13.0; the README and the source in `gunstore_mcp/tools/` are authoritative for behaviour.*
