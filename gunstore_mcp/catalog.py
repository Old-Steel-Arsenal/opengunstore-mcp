"""Annotation maps for list_pos_actions (tools/generic.py).

METHOD_TO_TOOL: a POS method a dedicated tool wraps -> that tool. The listing tells the
agent to prefer the tool. tests/test_catalog.py checks every tool named here is
registered and that its module really calls the method.
METHOD_NOTES: argument-dependent behaviour a listing row cannot show.
METHOD_SWITCH: methods whose dedicated tool exists only behind an env switch. The bare
method stays reachable through frappe_run_method with confirm.
"""
from __future__ import annotations

METHOD_TO_TOOL: dict[str, str] = {
    # preview_order
    "ffl_core.api.manual_order.check_serials_available": "preview_order",
    "ffl_core.price_approval.check": "preview_order",
    "ffl_core.api.manual_order.item_tax_rates": "preview_order",
    "ffl_core.api.manual_order.quote_config": "preview_order",
    "ffl_core.api.manual_order.get_customer_tax_status": "preview_order",
    "ffl_core.api.manual_order.order_line_costs": "preview_order",
    # update_order
    "ffl_core.api.manual_order.get_order_for_edit": "update_order",
    # preview_order
    "ffl_core.api.manual_order.order_agreements": "preview_order",
    # create_order
    "ffl_core.api.manual_order.create_order": "create_order",
    "ffl_core.api.manual_order.save_order_draft": "create_order",
    "ffl_core.api.manual_order.save_cr_ffl": "create_order",
    # distributor_ensure_item
    "ffl_integrations.distributor.api.ensure_dropship_item": "distributor_ensure_item",
    # update_order
    "ffl_core.api.manual_order.update_order": "update_order",
    # pending_order_action
    "ffl_core.api.manual_order.fulfill_from_stock": "pending_order_action",
    "ffl_core.api.manual_order.resource_line": "pending_order_action",
    "ffl_core.api.counter_pickup.open_pickup_invoice": "pending_order_action",
    "ffl_core.api.manual_order.resend_order_agreement": "pending_order_action",
    # pos_draft_orders
    "ffl_core.api.pos_order_status.draft_order_status": "pos_draft_orders",
    # pos_ticket
    "ffl_core.api.pos_ticket.open_ticket": "pos_ticket",
    "ffl_integrations.payments.api.card_tender_mode": "pos_ticket",
    "ffl_integrations.payments.api.charge_pos_invoice": "pos_ticket",
    "ffl_integrations.payments.api.poll_pos_charge": "pos_ticket",
    "ffl_integrations.payments.api.cancel_pos_charge": "pos_ticket",
    "ffl_integrations.payments.api.refund_pos_invoice": "pos_ticket",
    # card_terminal_charge
    "ffl_integrations.payments.api.charge_sales_invoice": "card_terminal_charge",
    "ffl_integrations.payments.api.poll_sales_invoice_charge": "card_terminal_charge",
    "ffl_integrations.payments.api.cancel_sales_invoice_charge": "card_terminal_charge",
    # saved_cards
    "ffl_integrations.payments.saved_cards.list_saved_cards": "saved_cards",
    # charge_saved_card
    "ffl_integrations.payments.saved_cards.charge_saved_card": "charge_saved_card",
    # trade_in_intake
    "ffl_core.api.trade_in.create_trade_in_intake": "trade_in_intake",
    "ffl_core.api.trade_in.seller_defaults": "trade_in_intake",
    # customer_overview
    "ffl_core.customer_flags.get_customer_flags": "customer_overview",
    "ffl_core.customer_profile.get_family": "customer_overview",
    "ffl_core.agreements.get_customer_agreements": "customer_overview",
    "ffl_core.agreements.agreements_for_sale": "customer_overview",
    # customer_find
    "ffl_core.customer_flags.find_possible_duplicates": "customer_find",
    "ffl_core.customer_flags.find_customers_by_scan": "customer_find",
    "ffl_core.customer_flags.find_customers_by_id_number": "customer_find",
    # customer_manage
    "ffl_core.customer_profile.merge_customer": "customer_manage",
    "ffl_core.customer_flags.override_customer_ban": "customer_manage",
    # agreement_request
    "ffl_core.agreements.request_agreement": "agreement_request",
    "ffl_core.signing_terminal.list_terminals": "agreement_request",
    # agreement_manage
    "ffl_core.agreements.void_agreement": "agreement_manage",
    "ffl_core.agreements.record_paper_agreement": "agreement_manage",
    # gunsmith_jobs
    "ffl_core.gunsmith.register_work_orders": "gunsmith_jobs",
    "ffl_core.gunsmith.get_state": "gunsmith_jobs",
    # gunsmith_intake
    "ffl_core.gunsmith.create_work_order": "gunsmith_intake",
    # gunsmith_job_action
    "ffl_core.gunsmith.start_work": "gunsmith_job_action",
    "ffl_core.gunsmith.mark_ready": "gunsmith_job_action",
    "ffl_core.gunsmith.notify_customer": "gunsmith_job_action",
    "ffl_core.gunsmith.open_deposit": "gunsmith_job_action",
    "ffl_core.gunsmith.complete_work": "gunsmith_job_action",
    "ffl_core.gunsmith.open_pickup": "gunsmith_job_action",
    "ffl_core.gunsmith.cancel_work_order": "gunsmith_job_action",
    "ffl_core.gunsmith.log_in_bound_book": "gunsmith_job_action",
    "ffl_core.gunsmith.refund_deposit": "gunsmith_job_action",
    "ffl_core.gunsmith.charge_details": "gunsmith_job_action",
    # memberships
    "ffl_core.membership.page_overview": "memberships",
    "ffl_core.membership.page_members": "memberships",
    "ffl_core.membership.get_memberships": "memberships",
    "ffl_core.membership.needs_attention": "memberships",
    # membership_action
    "ffl_core.membership.start_membership": "membership_action",
    "ffl_core.membership.invoice_membership": "membership_action",
    "ffl_core.membership.invoice_renewal": "membership_action",
    "ffl_core.membership.retry_renewal": "membership_action",
    "ffl_core.membership.set_auto_renew": "membership_action",
    "ffl_core.membership.change_card": "membership_action",
    "ffl_core.membership.cancel_membership": "membership_action",
    "ffl_core.membership.discard_membership": "membership_action",
    # refresh_4473_status
    "ffl_integrations.fastbound.pos_4473_recovery.refresh_4473_status": "refresh_4473_status",
    # resolve_4473
    "ffl_integrations.fastbound.pos_4473.attest_paper_4473": "resolve_4473",
    "ffl_integrations.fastbound.pos_4473_recovery.manager_redo_4473": "resolve_4473",
    "ffl_integrations.fastbound.pos_4473_recovery.manager_abandon_4473": "resolve_4473",
    # fastbound_serial_diff
    "ffl_integrations.fastbound.reconcile.compute_serial_fb_diff": "fastbound_serial_diff",
    # fastbound_serial_fix
    "ffl_integrations.fastbound.reconcile.match_serial_to_fastbound": "fastbound_serial_fix",
    "ffl_integrations.fastbound.reconcile.apply_serial_fb_resolution": "fastbound_serial_fix",
    "ffl_integrations.fastbound.tasks.push_serial_to_fastbound": "fastbound_serial_fix",
    # boundbook_record_fix
    "ffl_integrations.fastbound.reconcile.retry_disposition_push": "boundbook_record_fix",
    "ffl_integrations.fastbound.reconcile.reconcile_record_to_fastbound": "boundbook_record_fix",
    "ffl_integrations.fastbound.reconcile.mark_not_in_bound_book": "boundbook_record_fix",
    "ffl_integrations.fastbound.reconcile.clear_not_in_bound_book": "boundbook_record_fix",
    # verify_customer_ffl
    "ffl_integrations.atf.ez_check_api.verify_customer_ffl": "verify_customer_ffl",
    # ffl_directory_search
    "ffl_integrations.atf.search.search": "ffl_directory_search",
    # firearm_costs
    "ffl_core.api.cost_correction.list_item_serials_for_cost": "firearm_costs",
    # correct_firearm
    "ffl_core.api.cost_correction.correct_serial_cost": "correct_firearm",
    "ffl_core.api.serial_correction.correct_serial": "correct_firearm",
    # store_overview
    "ffl_core.api.store_overview.get_overview": "store_overview",
    "ffl_core.api.gun_shop_cards.get_count": "store_overview",
    # receive_goods_preview
    "ffl_core.api.receive_goods.get_receive_sources": "receive_goods_preview",
    "ffl_core.api.receive_goods.seller_payment_preview": "receive_goods_preview",
    "ffl_core.api.receive_goods.lookup_item": "receive_goods_preview",
    "ffl_core.api.firearm_intake.lookup_serial": "receive_goods_preview",
    # photo_gallery
    "ffl_core.api.photos.serials_for_item": "photo_gallery",
    # edit_photo_gallery
    "ffl_core.api.photos.add_gallery_images": "edit_photo_gallery",
    "ffl_core.api.photos.reorder_gallery": "edit_photo_gallery",
    "ffl_core.api.photos.set_primary_image": "edit_photo_gallery",
    "ffl_core.api.photos.set_image_caption": "edit_photo_gallery",
    "ffl_core.api.photos.remove_gallery_image": "edit_photo_gallery",
    # extensions_status
    "ffl_core.api.extensions.list_extensions": "extensions_status",
    "ffl_core.api.extensions.release_notes": "extensions_status",
    "ffl_core.api.extensions.disable_notice": "extensions_status",
    "ffl_core.api.extensions.uninstall_preview": "extensions_status",
    # manage_extension
    "ffl_core.api.extensions.refresh": "manage_extension",
    "ffl_core.api.extensions.set_enabled": "manage_extension",
    "ffl_core.api.extensions.install_on_site": "manage_extension",
    "ffl_core.api.extensions.fetch_update": "manage_extension",
    # distributor_incoming
    "ffl_integrations.distributor.receive_bridge.list_incoming": "distributor_incoming",
    "ffl_integrations.distributor.receive_bridge.match_incoming": "distributor_incoming",
    # distributor_receive
    "ffl_integrations.distributor.receive_bridge.receive_lines": "distributor_receive",
    "ffl_integrations.distributor.receive_bridge.receive_invoice_lines": "distributor_receive",
    "ffl_integrations.distributor.receive_bridge.stage_invoice": "distributor_receive",
    # web_order_action
    "ffl_woo_sync.woocommerce.pickup.pickup_web_order": "web_order_action",
    "ffl_woo_sync.woocommerce.revenue.create_web_invoice_now": "web_order_action",
    "ffl_woo_sync.woocommerce.refunds.reconcile_web_order_refund": "web_order_action",
    "ffl_woo_sync.woocommerce.antique.retire_antique_web_order_now": "web_order_action",
    "ffl_woo_sync.woocommerce.order_audit.run_web_revenue_backstop_now": "web_order_action",
    "ffl_woo_sync.woocommerce.tasks.retry_stuck_serial_reservations": "web_order_action",
    # web_order_exceptions
    "ffl_woo_sync.woocommerce.order_audit.web_stock_legs_outstanding": "web_order_exceptions",
    "ffl_woo_sync.woocommerce.order_audit.shipped_undisposed_orders": "web_order_exceptions",
    # woo_store_maintenance
    "ffl_woo_sync.woocommerce.condition_backfill.backfill_condition_attributes": "woo_store_maintenance",
    "ffl_woo_sync.woocommerce.categories.recategorize_store": "woo_store_maintenance",
    "ffl_woo_sync.woocommerce.dangling.woo_audit_dangling_ids": "woo_store_maintenance",
    # gb_pending_orders
    "ffl_integrations.gunbroker.fulfillment.list_pending_gb_orders": "gb_pending_orders",
    "ffl_integrations.gunbroker.fulfillment.get_dispose_stock_lines": "gb_pending_orders",
    # gb_update_listing
    "ffl_integrations.gunbroker.client_api.reprice_serial_now": "gb_update_listing",
    "ffl_integrations.gunbroker.client_api.reprice_item_now": "gb_update_listing",
    "ffl_integrations.gunbroker.client_api.replace_pictures_now": "gb_update_listing",
    "ffl_integrations.gunbroker.client_api.sync_item_quantity_now": "gb_update_listing",
    "ffl_integrations.gunbroker.client_api.clear_sold_unknown_now": "gb_update_listing",
    # gb_record_payment
    "ffl_integrations.gunbroker.fulfillment.mark_payment_received": "gb_record_payment",
    # gb_order_action
    "ffl_integrations.gunbroker.fulfillment.assign_gb_unit_serial": "gb_order_action",
    "ffl_integrations.gunbroker.fulfillment.mark_gb_unit_refunded": "gb_order_action",
    "ffl_integrations.gunbroker.fulfillment.push_gb_shipment": "gb_order_action",
    "ffl_integrations.gunbroker.fulfillment.push_tracking_now": "gb_order_action",
    "ffl_integrations.gunbroker.revenue.create_gb_invoice_now": "gb_order_action",
    # gb_dispose_order
    "ffl_integrations.gunbroker.fulfillment.dispose_gb_order": "gb_dispose_order",
    # card_processor_test_connection
    "ffl_integrations.payments.hub.overview": "card_processor_test_connection",
    "ffl_integrations.payments.hub.test_connection": "card_processor_test_connection",
    "ffl_integrations.payments.api.list_terminals": "card_processor_test_connection",
}

METHOD_NOTES: dict[str, str] = {
    "ffl_core.agreements.request_agreement":
        "frappe_run_method refuses it unless deliver is email, tablet or none; "
        "agreement_request does the job.",
    "ffl_integrations.fastbound.inventory_sync.sync_in_stock_from_boundbook":
        "Applying needs item_ids from a dry run; frappe_run_method refuses an apply "
        "without them. boundbook_reconcile does the job.",
    "ffl_woo_sync.woocommerce.condition_backfill.backfill_condition_attributes":
        "Defaults to apply, and empty lists mean the whole store; frappe_run_method "
        "refuses it. Use woo_store_maintenance.",
    "ffl_integrations.distributor.receive_bridge.receive_from_do":
        "Books the serials the pre-filled payload echoes; frappe_run_method refuses it. "
        "Use distributor_receive.",
    "ffl_core.api.item_admin.force_delete":
        "Does not keep the A&D trail: deletes non-submitted acquisitions/dispositions "
        "and raw-deletes ledger rows.",
    "ffl_core.api.manual_order.release_order_legs":
        "Places every Draft distributor leg of the order: a real purchase.",
}

_GB_SWITCH = "GUNSTORE_MCP_GUNBROKER_ACTIONS"
_DIST_SWITCH = "GUNSTORE_MCP_DISTRIBUTOR_ACTIONS"
METHOD_SWITCH: dict[str, str] = {
    "ffl_integrations.gunbroker.client_api.push_serial_now": _GB_SWITCH,
    "ffl_integrations.gunbroker.client_api.end_listing_now": _GB_SWITCH,
    "ffl_integrations.gunbroker.client_api.sync_orders_now": _GB_SWITCH,
    "ffl_integrations.gunbroker.client_api.reprice_serial_now": _GB_SWITCH,
    "ffl_integrations.gunbroker.client_api.reprice_item_now": _GB_SWITCH,
    "ffl_integrations.gunbroker.client_api.replace_pictures_now": _GB_SWITCH,
    "ffl_integrations.gunbroker.client_api.sync_item_quantity_now": _GB_SWITCH,
    "ffl_integrations.gunbroker.client_api.clear_sold_unknown_now": _GB_SWITCH,
    "ffl_integrations.distributor.api.confirm_order": _DIST_SWITCH,
    "ffl_integrations.distributor.api.cancel_order": _DIST_SWITCH,
    "osa_dropship.web_router.reroute": _DIST_SWITCH,
    "ffl_integrations.distributor.router.update_order_ffl": _DIST_SWITCH,
}
