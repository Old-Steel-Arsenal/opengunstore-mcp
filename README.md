# OpenGunstore MCP

A local [MCP](https://modelcontextprotocol.io) server that wraps the OpenGunstore
Frappe REST API, so you can read and change a store's settings and content from
Claude or Codex — toggle integration config, edit item pricing/listing, fix
records, trigger RSR/FastBound/ATF operations, run the Firearms-In-Stock report.

## How it works

- A small **generic backbone** (`frappe_*` tools) covers every doctype and every
  whitelisted method — present and future. A **curated layer** makes the frequent
  settings/sync/report ops one call.
- Talks to Frappe over HTTPS with token auth (`Authorization: token key:secret`).
- **Safety**: delete/cancel/submit and destructive methods — including this
  domain's own high-consequence verbs (dispose / push / charge / consolidate /
  ship / return / receive / sold / settle / onboard) —
  require `confirm=true`;
  password/credential fields are never transmitted (set those in Desk);
  schema/permission doctypes are read-only.

## Setup

### 1. Generate a Frappe API key
In Desk as the user you want to act as (Administrator): top-right avatar →
**My Settings** → **API Access** → **Generate Keys**. Copy the **API Key** and
**API Secret** (the secret is shown only once).

### 2. Configure credentials
```bash
cp .env.example .env
# edit .env: FRAPPE_BASE_URL, FRAPPE_API_KEY, FRAPPE_API_SECRET
```
`.env` is git-ignored. Point at a dev site first (`http://dev.localhost:8000`) to test,
then switch to your live POS URL (e.g. `https://pos.example.com`).

### 3. Install
```bash
uv sync          # creates .venv and installs deps
```

### 4. Register with your agent

`/path/to/opengunstore-mcp` below is wherever you cloned this repo (the package is
at the repo root). The server loads `.env` by its own file path, so credentials
never go in the agent config.

**Claude Code** — add to `.mcp.json` (project) or run `claude mcp add`:
```json
{
  "mcpServers": {
    "gunstore-pos": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/opengunstore-mcp", "gunstore-mcp"]
    }
  }
}
```
**Claude Desktop** — the same block in `claude_desktop_config.json`.

**Codex** — add to `~/.codex/config.toml`, then **restart Codex** (it reads MCP
servers at startup):
```toml
[mcp_servers.gunstore-pos]
command = "uv"            # or an absolute path to uv if it isn't on Codex's PATH
args = ["run", "--directory", "/path/to/opengunstore-mcp", "gunstore-mcp"]
startup_timeout_sec = 120
```
Verify with a quick stdio handshake (`initialize` + `tools/list`) or just list
tools from within Codex.

Secrets stay in `.env` (loaded by the server), not in the agent config.

## Tools

> **Tool reference, organised by task (safety notes and alternatives included): [TOOLS.md](TOOLS.md)**

181 tools total: 10 counter orders + 5 customers + 5 gunsmith/membership + 15 compliance/store-admin + 11 generic + 69 curated + 16 distributor + 7 CPA reports + 30 shop-floor + 7 HR + 6 Range; 167 register by default. Three opt-in sets are held back: the 4 distributor queue actions (`GUNSTORE_MCP_DISTRIBUTOR_ACTIONS=1`), the 4 GunBroker write actions (`GUNSTORE_MCP_GUNBROKER_ACTIONS=1`) and the 6 Range tools (`GUNSTORE_MCP_RANGE=1`, only for a store with the Range extension). None is registered otherwise — an absent tool cannot be talked into firing.

**Modes**: `GUNSTORE_MCP_MODE=cpa` starts a read-only accountant surface —
exactly 30 tools (the write surface is never registered), a per-name read-only
method allowlist at the client layer, and the 7 integration Settings doctypes
blocked from reads. Default (`full`) is the whole surface. Register a second
server entry (e.g. `gunstore-pos-cpa`) with the same command plus
`"env": {"GUNSTORE_MCP_MODE": "cpa"}` to run both side by side.

### Standalone CPA install

To install **only** the read-only accountant surface (no full server) — e.g. on
a second machine or for an analyst agent:

```bash
git clone git@github.com:Old-Steel-Arsenal/opengunstore-mcp.git ~/opengunstore-mcp
claude mcp add gunstore-pos-cpa --scope user \
  --env GUNSTORE_MCP_MODE=cpa \
  --env FRAPPE_BASE_URL=https://pos.example.com \
  --env FRAPPE_API_KEY=<key> \
  --env FRAPPE_API_SECRET=<secret> \
  -- uv run --directory ~/opengunstore-mcp gunstore-mcp
```

- **Process env beats `.env`**: the server loads `.env` with `override=False`,
  so env vars set in the registration win. A standalone install needs no
  `.env` file at all — and the same checkout can serve several entries with
  different `FRAPPE_BASE_URL` / mode combinations (e.g. a dev-site instance).
- **Verify**: after connecting, `tools/list` must show exactly **30** tools and
  the server name `gunstore-pos-cpa`. A misspelled/unknown mode value refuses
  to start (fail-closed) rather than silently degrading to the writable surface.
- **Security boundary — read before handing this to a third party**: the
  three-layer gate restricts the *MCP call layer* only. The API key/secret in
  the registration is whatever that Frappe user can do — with a System Manager
  key, anyone who extracts it can write via plain REST, MCP gate or not. For a
  third-party (e.g. an outside accountant's machine), create a dedicated
  Frappe API user with read-only roles and register with *that* key pair.

### Generic backbone
| Tool | Purpose |
|---|---|
| `frappe_list_documents` | list any doctype (filters / fields / limit; `limit=0` = all) |
| `frappe_get_document` | fetch one doc (Single: `name == doctype`) |
| `frappe_describe_doctype` | fields incl. custom fields; flags password fields |
| `frappe_create_document` | create (credential fields stripped) |
| `frappe_update_document` | update (credential fields stripped) |
| `frappe_delete_document` | delete — needs `confirm=true` |
| `frappe_submit_document` | submit a draft — needs `confirm=true` |
| `frappe_cancel_document` | cancel a submitted doc — needs `confirm=true` |
| `frappe_run_method` | call any whitelisted method by dotted path (credential, signing, webhook and legacy-alias endpoints refused; consequential ones need `confirm`; GET-only endpoints are sent as GET) |
| `list_pos_actions` | search the POS endpoints the signed-in user can run: arguments, roles, whether `confirm` is needed, and the dedicated tool to prefer (needs a POS release with the action catalog) |
| `frappe_run_report` | run a Script/Query report |

### Curated
| Tool | Purpose |
|---|---|
| `get_settings` / `update_settings` | `ffl` \| `fastbound` \| `rsr` \| `payroc` \| `woocommerce` \| `shipstation` \| `gunbroker` \| `sports_south` \| `lipseys` \| `data_service` |
| `find_item` / `item_stock` / `available_serials` | typeahead item search / stock per item / in-stock serials + per-gun prices |
| `firearms_in_stock` | the Firearms In Stock report |
| `receive_goods_preview` | what `receive_goods` would do with a payload: the private-seller payout, unknown items, serials already on file (read-only) |
| `distributor_incoming` / `distributor_receive` / `distributor_ensure_item` | parcels on their way (read-only) / receive one against its Distributor Order or store invoice (booking needs `confirm`) / find or create the Item for a distributor SKU (`confirm`) |
| `receive_goods` | Purchase Receipt + FFL acquisitions + FastBound push (`confirm`); a paid purchase from a private seller (`acquisition_source` "Individual", type blank / Purchase / Individual) needs `seller_payment_method` (Cash / Zelle / Check / ACH) + `seller_payment_reference` unless Cash (gunstore-pos #705 on) |
| `add_stock` / `set_stock` | non-serialized stock add / absolute set (`confirm`) |
| `toggle_service_need` | gunsmith flag on a Serial No (`confirm`) |
| `rsr_catalog_search` | RSR-only catalog search (`distributor_catalog_search` spans every enabled house) |
| `promote_to_item` / `backfill_from_rsr` | RSR catalog row → sellable Item / backfill Item fields (`confirm`) |
| `fastbound_test_connection` / `boundbook_reconcile` | FB probe / bound-book reconcile (apply needs `confirm`) |
| `boundbook_mismatches` | dry-run-only bound-book reconcile (read-only; also in cpa mode) |
| `atf_verify_ffl` / `verify_supplier_ffl` / `reverify_all_ffls` | ATF eZ-Check verifies (`confirm`) |
| `woo_test_connection` / `woo_push_item` / `woo_delist_item` / `woo_reconcile` | store probe / list / delist / reconcile an Item — all take `site: retail` (the only store; writes need `confirm`) |
| `woo_push_serial` / `woo_delist_serial` | list / delist ONE gun (SKU `item_code::serial`; `site`; `confirm`) |
| `set_serial_title` | per-gun Woo listing title (writes `Serial No.item_name`; takes effect on next push) |
| **GunBroker channel** | *the environment, credential and money fields of `GunBroker Settings` are refused on every write path — `update_settings`, `frappe_update_document`, `frappe_create_document` and `frappe_run_method`'s field setters (method names matching `set_value`/`db_set`/`save_doc`-alike, `safety._SETTER_METHOD`), enforced by a test over the whole registered surface — every tool module the server registers. Sandbox vs live is a Desk change, not a tool call. The 4 listing writes below need `GUNSTORE_MCP_GUNBROKER_ACTIONS=1` to be registered at all* |
| `gb_test_connection` / `gb_listing_status` | GunBroker probe — the reply's `sandbox` flag says which marketplace answered (needs SYSTEM_ROLES, as does `gb_pull_orders`; the other three need STOCK_ROLES) / POS-vs-GunBroker view of ONE gun's listing (read-only) |
| `gb_push_serial` / `gb_end_listing` | list ONE gun as a fixed-price Buy Now / end its listing (opt-in via `GUNSTORE_MCP_GUNBROKER_ACTIONS=1`, then `confirm`; both reach GunBroker only via the POS) |
| `gb_update_listing` | reprice / replace photos / sync a model's quantity (0 ends it) / lift the "may already be sold" flag on a live listing (opt-in + `confirm`) |
| `gb_pending_orders` / `gb_record_payment` / `gb_order_action` / `gb_dispose_order` | open GunBroker orders (read-only; also cpa) / record the banked payment / assign units, refunds, shipment and tracking push, invoice retry / book the dispositions (`confirm`). Not behind the switch: they fulfil an order already sold and never change a listing offer |
| `gb_pull_orders` | run the order poll now (opt-in + `confirm`; takes no arguments). Importing an order creates POS documents and reserves the gun, which is why it is gated with the writes. The reply `{"queued": true}` is a receipt — the poll runs in the background and reports no counts |
| `pending_orders` / `pending_web_orders` | the Pending Order queue: counter/dealer rows + paid web orders (read-only; consignments live in `consignment_queue`) |
| `dispose_order` / `dispose_web_order` | book the FFL transfer dispositions — stock-out + FastBound push (`confirm`) |
| `record_payment` | Payment Entry against an unpaid submitted invoice; Zelle needs `transaction_number` (`confirm`) |
| `card_charges` | Card charges on a Sales Invoice that can still be voided or refunded (read-only) |
| `void_card_charge` | Void a whole card charge before it settles; the invoice owes it again (`confirm`) |
| `refund_card_charge` | Refund part or all of a card charge: `Return` (credit note) or `Collect again` (`confirm`) |
| `resolve_card_reversal` | Book or release a void/refund the processor never confirmed, after checking its portal (`confirm`) |
| `cancel_order` | cancel a submitted counter/dealer order — cascades disposition/stock/FastBound/ShipStation reversal + refund (`confirm` + `reason`) |
| `push_shipment` / `mark_shipped_manually` / `shipstation_test_connection` | ShipStation label push (`confirm`) / no-push escape hatch (`confirm`) / probe |
| `consignment_queue` / `consignment_dealers` / `consignment_serials` / `consignment_dealer_orders` | outbound-consignment reads: At-Dealer queue / shippable FFL dealers / pickable serials / settlement queue |
| `create_consignment_out` / `ship_consignment_out` / `push_consignment_shipment` / `mark_consignment_shipped` | build / dispose+ship / ShipStation push / manual-ship w/ tracking (`confirm`) |
| `retry_consignment_invoice` / `return_consignment_lines` / `cancel_consignment` | settlement-invoice retry / take unsold guns back / cancel line-or-draft (`confirm`; cancel needs `reason`) |
| `update_consignment_prices` | change cost / MSRP on guns out on consignment (`confirm`) |
| `start_4473` / `manager_override_4473` / `start_transfer_4473` | 4473 kickoff / stuck-sale manager override / customer transfer (`confirm`) |
| `web_order_action` / `web_order_exceptions` / `woo_store_maintenance` | web-order pickup, invoice retry, refund reconcile, antique retire, backstop, stuck reservations (`confirm`) / month-end web checks (read-only; also cpa) / store-wide attribute, category and dangling-ID sweeps (apply needs `confirm`) |
| `card_processor_test_connection` | why the card terminal will not charge: switch vs readiness, live probe, terminals (read-only) |
| `upload_attachment` | multipart file upload → File doc, optionally attached to a doctype+name / Attach field |

### Counter orders and register tickets (`tools/orders.py`)
| Tool | Purpose |
|---|---|
| `preview_order` / `create_order` / `update_order` | check an order (serials, tag prices, costs, tax, price approval; read-only) / take a counter, phone or transfer order / edit a pending one (`confirm`; `save_as_draft` needs none) |
| `pending_order_action` | fulfil from stock, re-source a line, open the pickup ticket, resend the agreement (`confirm`) |
| `pos_draft_orders` / `pos_ticket` | tickets parked at the register (read-only) / open, tender, card start/poll/cancel, submit a ticket (`confirm` except `poll_card`) |
| `card_terminal_charge` / `saved_cards` / `charge_saved_card` | counter terminal charge on a Sales Invoice (`confirm` for start and cancel) / cards on file (read-only) / charge a card on file (`confirm`; managers) |
| `trade_in_intake` | buy a gun from a walk-in seller and pay them now (`confirm`) |

### Customers, gunsmith, memberships (`tools/customers.py`, `tools/services.py`)
| Tool | Purpose |
|---|---|
| `customer_overview` / `customer_find` | standing, alerts, family, IDs, agreements (and what a cart still needs signed) / find by email, phone, ID or name (read-only) |
| `customer_manage` / `agreement_request` / `agreement_manage` | merge, override a ban, set status, add an alert / send an agreement by email, tablet or for paper / void or record a paper signature (`confirm`) |
| `gunsmith_jobs` / `gunsmith_intake` / `gunsmith_job_action` | the repair queue (read-only) / take a gun in / start, ready, notify, payment, pickup, cancel, refund, log in the bound book (`confirm`) |
| `memberships` / `membership_action` | plans, roster, one customer's (no confirm; never on a read-only surface) / start, invoice, renew, retry the card, auto-renew, change card, cancel, drop a Pending sign-up (`confirm`) |

### Compliance repairs and store admin (`tools/compliance.py`)
| Tool | Purpose |
|---|---|
| `refresh_4473_status` / `resolve_4473` | re-check a ticket's 4473 and say the next step / attest a paper 4473 (remote only), redo or abandon (`confirm`) |
| `fastbound_serial_diff` / `fastbound_serial_fix` / `boundbook_record_fix` | compare guns with the bound book (read-only) / match, apply the differences, push a correction (`confirm`) / retry, link or mark an A&D record FastBound never got (`confirm`) |
| `verify_customer_ffl` / `ffl_directory_search` | eZ Check a dealer customer's FFL (`confirm`) / search the FFL directory (read-only) |
| `firearm_costs` / `correct_firearm` | per-gun cost (read-only) / correct a cost or a mistyped serial (`confirm` + `reason`) |
| `store_overview` / `receive_goods_preview` | the morning briefing and needs-attention counts / check a receive before booking it (read-only) |
| `photo_gallery` / `edit_photo_gallery` | a gun's or model's gallery (read-only) / add, reorder, set primary, caption, remove (`confirm` for remove and private files) |
| `extensions_status` / `manage_extension` | installed extensions and updates (read-only) / refresh, enable, disable, install, two-step update (System Manager; `confirm`) |

### Shop floor: stocktake, cash drawer, storage locations (POS 1.5.0-beta.15)
Every write needs `confirm=true`; there is no registration gate (they are the same counter actions the POS pages offer, role-checked per call and audited on the remote connector). Firearms are never adjusted by a stocktake and storage moves touch no stock or books.

| Tool | Purpose |
|---|---|
| `inventory_counts` / `inventory_count_state` / `inventory_count_variance` | list stocktakes / one count's scans + expected rows / counted-vs-system variance (read-only; counts + variance are also on the cpa surface) |
| `inventory_count_create` / `inventory_count_cancel` | start (whole store or scoped) / cancel an open count (`confirm`) |
| `inventory_count_scan` / `inventory_count_set_qty` / `inventory_count_toggle_serial` / `inventory_count_undo` | record a serial or UPC scan / type a quantity / tick a gun by hand / remove an entry (`confirm`) |
| `inventory_count_finalize` | posts ONE Stock Reconciliation for the non-serialized differences, saves the firearm report (`confirm`; firearms never adjusted) |
| `cash_drawer_today` / `cash_drawer_preview_close` | the drawer page (expected cash + lines) / what a close WOULD do (read-only) |
| `cash_drawer_closes` / `cash_drawer_entries` / `cash_drawer_log` / `cash_drawer_weekly` | daily counts / deposits, cash from the bank, expenses, seller payouts / every cash movement with who and the running balance / the Cash Drawer Weekly report (read-only; also on the cpa surface) |
| `cash_drawer_close_day` | count the drawer, close POS shifts, book the over/short or first-count entry (`confirm`) |
| `cash_drawer_record_entry` | `kind` = deposit \| from_bank \| expense (`confirm`); an expense needs `expense_account` + `memo`; a receipt is optional (POS 1.8.2+), and when given must be a file the same POS user uploaded within a day — remote has no upload tool, so the user uploads it in the POS and passes the `file_url` |
| `cash_drawer_undo` / `cash_drawer_record_payout` | cancel an entry or the latest close / change the method of / re-book an old-way seller payment (first payments go through the receipt) (`confirm`; managers) |
| `storage_map` / `storage_location` / `storage_where` / `storage_unassigned` | the store map (`zones_only` = zone list) / one location's contents / where a serial or item is / unassigned and to-confirm lists (read-only) |
| `storage_create_zone` / `storage_add_positions` / `storage_set_disabled` | zone management (`confirm`; Stock Manager) |
| `storage_scan_move` / `storage_undo_move` / `storage_confirm_taken` | assign a gun / put units into a location / undo it / say where sold units came from (`confirm`) |

### Distributor direct-ship (`tools/distributor.py`)
| Tool | Purpose |
|---|---|
| `distributor_orders` / `distributor_route_queue` / `distributor_catalog_search` / `distributor_test_connection` | Distributor Orders / the confirmation queue / local catalog typeahead / probe one house (read-only) |
| `distributor_quote` / `distributor_check_availability` / `distributor_precheck_fds` / `distributor_fulfillment_options` | cost, MAP and block flags / live stock re-check / will it ship to that dealer / per-line options for one web order (read-only) |
| `distributor_create_draft_order` | Draft restock order(s) shipped to the store (`confirm`) |
| `distributor_confirm_order` / `distributor_cancel_order` / `distributor_reroute` / `distributor_update_order_ffl` | the 4 queue actions: place / cancel / re-route / fix an FDS hold (opt-in via `GUNSTORE_MCP_DISTRIBUTOR_ACTIONS=1`, then `confirm`; never on the remote connector) |

### Frappe HR (`tools/hr.py`; needs the `hrms` app on the site)
Attendance, shifts and leave only; payroll is out of scope. Every write needs `confirm=true`, and there is no registration gate. On a site without hrms every tool answers "Frappe HR (hrms) is not installed on this site".

| Tool | Purpose |
|---|---|
| `hr_staff` / `hr_attendance` / `hr_leave` | active staff with shift, holiday list, approver, whether a User Permission limits their login, and today's last clock-in / check-ins and Attendance per day with totals (range capped at 62 days) / leave balances or the Open Leave Applications (read-only; nothing personal is returned) |
| `hr_hours_summary` | days Present / Half Day / Absent / On Leave and working hours per employee for a period, with the count of days that have check-ins but no Attendance yet (read-only; also on the cpa surface) |
| `hr_leave_decide` | approve or reject an Open Leave Application, a reject needs a reason (`confirm`) |
| `hr_attendance_fix` | `action` = add_checkin \| mark_attendance: add a missed punch / mark and submit a day's Attendance, with a recorded reason (`confirm`) |
| `hr_onboard` | make an existing login an employee in one step: Employee, User Permission rule, shift, holiday list and the Leave Policy Assignment for the current anniversary year (`confirm`) |

### Range board (`tools/range_board.py`, opt-in via `GUNSTORE_MCP_RANGE=1`)
| Tool | Purpose |
|---|---|
| `range_board` / `range_free_lanes` / `range_find_bookings` | one day's board / lanes free at a time / a customer's bookings (read-only) |
| `range_book` / `range_update` / `range_cancel` | book a lane or block / change a booking / cancel with a reason (`confirm`) |

### CPA reports (read-only; registered in both modes)
| Tool | Purpose |
|---|---|
| `sales_report` | the POS Sales Report, payload passed through unchanged (views Order / Order Detail / Product; channel POS/Web/Manual) |
| `inventory_receipts` | the POS Inventory Receipts report — everything that entered stock in a period (per unit / per serial, or a category × receipt-type summary), straight from the stock ledger; classes Purchase / Trade-in / Consignment / Intake / Return / Adjustment / Revaluation (cost correction, 0 units), with No-cost / No-A&D flags |
| `gl_entries` | GL rows for a date range (`is_cancelled=0` always; explicit `truncated:true`) |
| `financial_statement` | P&L / Balance Sheet (Date Range) / Trial Balance (fiscal-year auto-resolved) |
| `tax_liability` | sales-tax liability roll-forward from the GL — accounts resolved from the default sales-tax template, vouchers bucketed fail-closed, cent-exact identity asserted |
| `payroc_transactions` | every Payroc card transaction for a date range (≤31 days), read live from the gateway — counter and Woo web orders, sales / refunds / declines, portal refunds and voids included — each matched to the POS with disagreement flags; card type + last 4 only, plus the cardholder name. Needs an OpenGunstore release carrying `payroc/ledger.py`, and an API user with System Manager / Accounts Manager / Accounts User |
| `ar_ap_summary` | aged AR / AP as of a date (Posting Date basis, 30/60/90/120). AP is only meaningful when purchases are recorded as Purchase Invoices in ERPNext — reference only |

## Remote connector (OAuth, no API key)

The same server runs as a remote MCP endpoint with `GUNSTORE_MCP_TRANSPORT=http`.
It is an OAuth **resource server**; the **POS itself is the authorization server**
(Frappe v16's built-in OAuth: dynamic client registration + PKCE). A user adds the
connector URL in claude.ai (Settings → Connectors) or Claude Code
(`claude mcp add --transport http <name> <url>`), signs in to the POS in the browser
and approves. No key is typed anywhere, and the server holds none.

- Every request carries that user's bearer token. The server checks it against the
  POS (`ffl_core.api.connector.connector_identity`, cached 60 s; the token is never
  logged or cached in the clear) and forwards it on every call, so **each call runs
  with the signed-in user's own POS roles**.
- **Works with any MCP client that supports OAuth** — Claude (claude.ai
  Connectors, Claude Code `claude mcp add --transport http`) and ChatGPT (custom
  connectors / developer mode, or the Responses API `mcp` tool with the user's
  token). Each client registers itself with the POS (dynamic client
  registration), and the audit shows which one made each call.
- **Switched in the POS**: Desk → MCP Settings turns the read-only and full
  servers on or off separately (and owns the OAuth settings they sign in
  through); a switched-off server tells the user so on every call.
- **Only connector tokens are accepted**: a token the POS issued to a client created
  by dynamic registration. Tokens of OAuth apps made in Desk are refused.
- **Every call is audited in the POS** (Desk → Connector Audit Log, permanent, rows
  cannot be deleted; reads included): who, through which connector, which tool,
  which arguments (secrets masked by key, in filters and inside JSON strings),
  Started → Success / Failed. The row is written before the tool runs — if it
  cannot be, the tool does not run. Needs an OpenGunstore release carrying
  `ffl_core/api/connector.py` and the Connector Audit Log doctype.
- A user with **Restrict IP** set cannot use the connector: calls reach the POS from
  the connector (and ultimately from Claude's servers), never from the user's IP.
- `cpa` mode keeps all three read-only layers. The full surface is **166 tools**
  remotely: `upload_attachment` reads a path on the *server* and is never registered
  there. The distributor queue actions never open remotely (the server refuses to start;
  `distributor_create_draft_order` is not one of them and is there);
  the 4 GunBroker writes open on the **full** connector only, where the POS deploy
  sets `GUNSTORE_MCP_GUNBROKER_ACTIONS` from that store's own GunBroker switch — a
  store that lists on GunBroker must be able to end a listing. The Range tools open
  where the POS deploy sets `MCP_RANGE=1` (a store running the Range extension).
- Env: `GUNSTORE_MCP_TRANSPORT=http`, `FRAPPE_BASE_URL` (the store's public POS URL,
  also the OAuth issuer), `GUNSTORE_MCP_PUBLIC_URL`
  (`https://pos.<domain>/connector/<mode>/mcp`, the URL users are given),
  `GUNSTORE_MCP_PORT`, `GUNSTORE_MCP_MODE`; optional `GUNSTORE_MCP_HOST` (listen
  address, default 127.0.0.1) and `FRAPPE_INTERNAL_URL` (loopback URL of the local
  POS frontend — calls then go there directly with the site's Host header). It runs
  on the POS host behind that host's reverse proxy, which maps the public path to
  `/mcp` and forwards `/.well-known/oauth-protected-resource/<public path>` unchanged.
- Deployment: a container per surface on each store's POS host, behind that host's
  Caddy — image `ghcr.io/old-steel-arsenal/gunstore-pos-mcp` (built on every push to main),
  deployed by each POS release (opengunstore `deploy/prod/mcp`, pinned
  `MCP_TAG`) — see `deploy/README.md`.
- POS side, once per site (OAuth Settings): *Show Auth Server Metadata* and
  *Enable Dynamic Client Registration* on; *Skip Authorization* off (every user
  approves). Revoke a user's access in Desk under OAuth Bearer Token.

## Security notes

- Local (stdio) server: uses the key's user (Administrator) = full access. Run it
  **locally only**; keep `.env` out of git (it is, by default). The remote connector
  holds no key — see "Remote connector" above.
- Credentials never transit the MCP: password-type and credential-named fields are
  stripped from every write. Set secrets in Desk directly.
- All writes are logged by Frappe under the key's user (audit trail).
- To revoke access, regenerate that user's keys in Desk.
