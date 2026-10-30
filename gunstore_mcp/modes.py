"""Server modes — the cpa read-only gate.

`GUNSTORE_MCP_MODE=cpa` turns the server into an accountant-facing, read-only
surface with three independent defence layers (each fails closed on its own):

  1. Registration (this module's FilteredMCP + CPA_TOOL_NAMES): only the
     allowlisted tools exist in tools/list — the write surface is
     physically absent, not merely guarded.
  2. Client (frappe_client.py): every mutating client method refuses in cpa
     mode, and dotted methods are checked against CPA_METHOD_ALLOWLIST —
     names listed one by one, NO prefix wildcards, NO HTTP-verb heuristics
     (run_report rides POST-adjacent /api/method GET; verbs prove nothing).
  3. Settings reads: the 9 integration Settings doctypes refuse get/list in
     cpa mode — Password masking is a framework behaviour, not this repo's
     guarantee, and the config surface has no accounting purpose.

Allowlist caliber authority: runs/2026-07-16-mcp-cpa-mode/cpa-review.md §4
(9 curated reads kept, 6 cut — available_serials excluded because its
exclude_unavailable=True default silently drops consigned/held guns, which is
exactly wrong in a stock-count/audit context).
"""
from __future__ import annotations

import os

from .config import _load_env

FULL_MODE = "full"
CPA_MODE = "cpa"
_VALID_MODES = (FULL_MODE, CPA_MODE)


class CpaModeRefused(RuntimeError):
    """Raised when cpa (read-only) mode blocks an operation."""


# Layer 1 — the EXACT cpa tools/list (asserted by set equality in tests;
# 4 generic + 12 curated read-only + 6 shop-floor reads + 7 CPA reports = 29).
CPA_TOOL_NAMES: frozenset[str] = frozenset({
    # generic reads (frappe_run_method deliberately absent: reads and writes
    # are statically indistinguishable through it)
    "frappe_list_documents",
    "frappe_get_document",
    "frappe_describe_doctype",
    "frappe_run_report",
    # curated read-only
    "find_item",
    "item_stock",
    "firearms_in_stock",
    "pending_orders",
    "pending_web_orders",
    "consignment_queue",
    "consignment_dealers",
    "consignment_serials",
    "consignment_dealer_orders",
    # dry-run-only bound-book reconcile (boundbook_reconcile can apply, so it is NOT here)
    "boundbook_mismatches",
    # order-exception reads: web stock legs still open / shipped but not disposed,
    # and the open GunBroker orders with their stock lines
    "web_order_exceptions",
    "gb_pending_orders",
    # cash drawer + stocktake reads (tools/shopfloor.py) — owner decision: the
    # accountant reads the drawer's closes / entries / log / weekly report and a count's
    # variance; none of the shop-floor WRITES is here.
    "cash_drawer_closes",
    "cash_drawer_log",
    "cash_drawer_entries",
    "cash_drawer_weekly",
    "inventory_counts",
    "inventory_count_variance",
    # CPA report pack (tools/reports.py)
    "sales_report",
    "inventory_receipts",
    "gl_entries",
    "financial_statement",
    "tax_liability",
    "ar_ap_summary",
    "payroc_transactions",
})

# Layer 2 — dotted methods callable in cpa mode. One name per line, verified
# read-only server-side; prefix wildcards are a forbidden construct here.
CPA_METHOD_ALLOWLIST: frozenset[str] = frozenset({
    "frappe.desk.query_report.run",
    "ffl_core.api.receive_goods.search_items",
    "ffl_core.api.item_admin.get_item_stock",
    "ffl_core.api.manual_order.list_pending_dispositions",
    "ffl_woo_sync.woocommerce.fulfillment.list_pending_web_orders",
    "osa_consignment.api.consignment_out.list_consignment_queue",
    "osa_consignment.api.consignment_out.list_consignment_dealers",
    "osa_consignment.api.consignment_out.available_serials_for_consignment",
    "osa_consignment.api.dealer_orders.list_dealer_orders",
    "ffl_integrations.payroc.ledger.payroc_transactions",
    # The cash-drawer log (the other drawer reads ride REST list / query_report.run):
    # the POS lets the counter roles and Accounts User / Manager read it.
    "ffl_core.api.cash_drawer.get_log",
    # Stocktake reads: the POS gates both on a stock role, so the cpa API user needs one.
    "ffl_core.api.inventory_count.get_counts",
    "ffl_core.api.inventory_count.variance",
    # Bound-book reconcile, dry-run only: the POS method hard-wires dry_run=1 (no
    # write path). The writable sync_in_stock_from_boundbook is deliberately absent.
    "ffl_integrations.fastbound.inventory_sync.boundbook_mismatches",
    # web_order_exceptions / gb_pending_orders: COUNTER_ROLES reads, the same POS gate
    # as list_pending_dispositions. shipped_undisposed_orders is a new POS endpoint.
    "ffl_woo_sync.woocommerce.order_audit.web_stock_legs_outstanding",
    "ffl_woo_sync.woocommerce.order_audit.shipped_undisposed_orders",
    "ffl_integrations.gunbroker.fulfillment.list_pending_gb_orders",
    "ffl_integrations.gunbroker.fulfillment.get_dispose_stock_lines",
    # gb_pending_orders(gunbroker_order): the banned-buyer badge (Customer read).
    "ffl_core.customer_flags.banned_order_rows",
})

# Tools that look like reads but write as a side effect, so they may never join a
# read-only surface: memberships(view='customer') settles a sign-up paid another
# way, and refresh_4473_status writes the 4473 state flags it reports.
NOT_PURE_READS: frozenset[str] = frozenset({"memberships", "refresh_4473_status"})
assert not NOT_PURE_READS & CPA_TOOL_NAMES

# Layer 3 — integration Settings doctypes whose get/list reads are refused in
# cpa mode (config surface, no accounting purpose).
CPA_SETTINGS_READ_BLOCKLIST: frozenset[str] = frozenset({
    "FFL Settings",
    "FastBound Settings",
    "RSR Settings",
    "Payroc Settings",
    "WooCommerce Settings",
    "ShipStation Settings",
    # Holds the GunBroker DevKey and the seller account password. Reading it has
    # no accounting purpose, and Password masking is a framework behaviour rather
    # than a guarantee this repo makes.
    "GunBroker Settings",
    "Sports South Settings",
    # Holds the Lipsey's dealer and drop-ship logins.
    "Lipseys Settings",
    "Data Service Settings",
})


def get_mode() -> str:
    """Resolve the server mode from GUNSTORE_MCP_MODE (default: full).

    Unknown values fail closed with a RuntimeError instead of silently
    running full — a typo'd 'cpa' must never yield a writable server."""
    _load_env()  # honour .env the same way credentials do
    raw = (os.environ.get("GUNSTORE_MCP_MODE") or FULL_MODE).strip().lower()
    if raw not in _VALID_MODES:
        raise RuntimeError(
            f"GUNSTORE_MCP_MODE must be one of {_VALID_MODES} (got {raw!r}) — "
            "refusing to guess a mode."
        )
    return raw


class FilteredMCP:
    """Registration-time filter: only allowlisted tool names reach the real
    MCP; everything else is silently skipped (the tool never exists)."""

    def __init__(self, mcp, allow) -> None:
        self._mcp = mcp
        self._allow = frozenset(allow)

    def tool(self, *args, **kwargs):
        real = self._mcp.tool(*args, **kwargs)

        def deco(fn):
            if fn.__name__ in self._allow:
                return real(fn)
            return fn

        return deco
