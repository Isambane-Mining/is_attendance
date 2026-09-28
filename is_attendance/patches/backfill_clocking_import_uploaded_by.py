# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

import frappe

DOCTYPE = "Clocking Import"
FIELDNAME = "uploaded_by"


def execute():
    """
    Backfill the new `uploaded_by` field (added to mirror the standard
    `owner` field - see ClockingImport.validate() and this field's own
    description for why it's a real field rather than a redeclared
    "owner" DocField) for every Clocking Import created before this field
    existed.

    Safe to re-run: only touches rows where uploaded_by is still blank, so
    a document created after this field existed (already populated by
    validate() itself) is never overwritten.

    Registered under [post_model_sync] in patches.txt - by then the
    doctype schema sync that adds this column has already run, so the
    column is guaranteed to exist without this patch needing to create it
    itself first (unlike migrate_employee_checkin_site_to_isa_branch's own
    fixture-based field, which syncs *after* patches run).
    """
    if not frappe.db.exists("DocType", DOCTYPE):
        return
    if not frappe.db.has_column(DOCTYPE, FIELDNAME):
        return

    frappe.db.sql(
        f"""
        UPDATE `tab{DOCTYPE}`
        SET `{FIELDNAME}` = `owner`
        WHERE (`{FIELDNAME}` IS NULL OR `{FIELDNAME}` = '')
          AND (`owner` IS NOT NULL AND `owner` != '')
        """
    )
