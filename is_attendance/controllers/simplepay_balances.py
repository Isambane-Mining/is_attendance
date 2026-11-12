# Copyright (c) 2026, BuFf0k and contributors
# SimplePay balances are authoritative; this module never writes to SimplePay.

import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime, flt

from is_attendance.controllers import simplepay, simplepay_mapping

DIRECTORY = "SimplePay Employee"
SETTINGS = "SimplePay Settings"


def _str(value):
    return str(value).strip() if value is not None else ""


def _balanced_payload(payload):
    """Normalize the documented date-specific response and 'latest' list.

    An absent leave type is NOT a zero balance. Nonnumeric categories such as
    Unpaid are not converted to numeric leave entitlements.
    """
    result = {}
    if isinstance(payload, dict):
        iterable = [{"type_id": key, "balance": value} for key, value in payload.items()]
    elif isinstance(payload, list):
        iterable = []
        for entry in payload:
            if isinstance(entry, dict) and "type_id" in entry:
                iterable.append(entry)
            elif isinstance(entry, dict) and len(entry) == 1:
                key, value = next(iter(entry.items()))
                iterable.append({"type_id": key, "balance": value})
            else:
                raise simplepay.SimplePayError("Malformed SimplePay leave balances entry")
    else:
        raise simplepay.SimplePayError("Unexpected SimplePay leave balances response")

    for entry in iterable:
        key = _str(entry.get("type_id"))
        if not key.isdigit() or key in result:
            raise simplepay.SimplePayError("Invalid or duplicate leave balance type ID")
        value = entry.get("balance")
        if isinstance(value, bool):
            raise simplepay.SimplePayError("Invalid boolean leave balance")
        try:
            amount = Decimal(str(value))
            if not amount.is_finite():
                raise InvalidOperation()
        except (InvalidOperation, TypeError, ValueError):
            result[key] = {"value": None, "label": _str(value)}
        else:
            result[key] = {"value": float(amount), "label": None}
    return result


def _configured_pairs(settings):
    """Return mapped, active external employees, omitting unmapped clients."""
    clients = {}
    for row in settings.companies or []:
        if row.enabled and row.company:
            cid = _str(row.simplepay_client_id)
            if cid in clients:
                raise simplepay.SimplePayError(f"Duplicate enabled SimplePay client: {cid}")
            clients[cid] = row.company
    seen_local = set()
    pairs = []
    for row in settings.employee_map or []:
        if not row.employee:
            continue
        if row.employee in seen_local:
            raise simplepay.SimplePayError(f"Duplicate mapped Frappe Employee: {row.employee}")
        seen_local.add(row.employee)
        if not row.simplepay_employee:
            continue
        directory = frappe.db.get_value(DIRECTORY, row.simplepay_employee,
            ["simplepay_employee_id", "simplepay_client_id", "is_active"], as_dict=True)
        if not directory or not directory.is_active:
            continue
        client_id = _str(directory.simplepay_client_id)
        company = clients.get(client_id)
        if not company:
            continue
        employee_company = frappe.db.get_value("Employee", row.employee, "company")
        if employee_company != company:
            raise simplepay.SimplePayError(f"Company mismatch for mapped Employee {row.employee}")
        sp_id = _str(directory.simplepay_employee_id)
        if not sp_id.isdigit():
            raise simplepay.SimplePayError(f"Invalid SimplePay ID for {row.employee}")
        pairs.append((row.simplepay_employee, row.employee, sp_id, client_id))
    return pairs


def _enabled_type_ids(settings, client_id):
    return { _str(row.simplepay_type_id) for row in settings.leave_types or []
        if row.enabled and row.leave_type and _str(row.get("simplepay_client_id")) == client_id }


def _refresh_all_balances():
    settings = frappe.get_single(SETTINGS)
    if not settings.enabled or not settings.get("sync_leave_balances"):
        return {"skipped": "Leave balance sync disabled"}

    pairs = _configured_pairs(settings)
    staged = []
    errors = []
    today = getdate().isoformat()
    for directory_name, employee, sp_id, client_id in pairs:
        expected = _enabled_type_ids(settings, client_id)
        if not expected:
            continue
        try:
            response = simplepay._request("GET", f"employees/{int(sp_id)}/leave_balances",
                params={"date": today})
            balances = _balanced_payload(response)
            # Do not replace an existing successful snapshot with incomplete data.
            missing = expected - set(balances)
            if missing:
                raise simplepay.SimplePayError(
                    f"Missing mapped leave types in response: {', '.join(sorted(missing))}")
            staged.append((directory_name, balances))
        except Exception as exc:
            errors.append({"employee": employee, "error": str(exc)[:200]})

    fetched_at = now_datetime()
    for directory_name, balances in staged:
        frappe.db.set_value(DIRECTORY, directory_name, {
            "leave_balances_json": json.dumps({"as_of": today, "balances": balances}, sort_keys=True),
            "leave_balances_fetched_at": fetched_at,
        }, update_modified=False)
    # Partial failures retain previous snapshots, but these must never be treated
    # as authoritative for approval; approval always performs a fresh GET.
    if errors:
        frappe.log_error(title="SimplePay balance pull errors", message=json.dumps(errors[:50]))
    return {"mapped_employees": len(pairs), "updated": len(staged), "failed": len(errors),
            "failures": errors[:20], "as_of": today}


def sync_daily():
    """One scheduled job: catalogue first, balances second; never two daily hooks."""
    settings = frappe.get_single(SETTINGS)
    if not settings.enabled:
        return {"skipped": "SimplePay disabled"}
    catalogue = simplepay_mapping.sync_all_mappings()
    balances = _refresh_all_balances()
    return {"catalogue": catalogue, "balances": balances}


@frappe.whitelist()
def refresh_leave_balances():
    simplepay._require_payroll_access()
    frappe.enqueue("is_attendance.controllers.simplepay_balances.pull_leave_balances",
                   queue="long", enqueue_after_commit=True,
                   job_name="simplepay-leave-balance-refresh")
    return {"queued": True}


def pull_leave_balances():
    return _refresh_all_balances()


def before_submit(doc, method=None):
    """Fail-closed against SimplePay when the selected company/leave is mapped.

    A daily snapshot is NEVER used as approval authorization. This uses an
    on-demand GET; the API/leave-type mapping must be available or submission
    is rejected. No SimplePay writes happen here.
    """
    settings = frappe.get_single(SETTINGS)
    if (not settings.enabled or not settings.get("enforce_leave_balance")
            or doc.status != "Approved"):
        return
    # Explicitly exempt Leave Without Pay, which does not require entitlement.
    if frappe.db.get_value("Leave Type", doc.leave_type, "is_lwp"):
        return
    company_row = simplepay._company_row(settings, doc.company)
    type_row = simplepay._type_row(settings, doc.company, doc.leave_type)
    sp_type = _str(type_row.simplepay_type_id)
    mapped = simplepay._mapping(doc.employee, doc.company, doc.leave_type)
    if not sp_type.isdigit():
        frappe.throw(_("Invalid SimplePay Leave Type mapping"))
    # Balance as of application start, not yesterday's daily snapshot.
    request_date = getdate(doc.from_date).isoformat()
    response = simplepay._request("GET", f"employees/{int(mapped['employee_id'])}/leave_balances",
                                 params={"date": request_date})
    balances = _balanced_payload(response)
    if sp_type not in balances:
        frappe.throw(_("SimplePay has no balance for this mapped Leave Type; approval blocked"))
    balance = balances[sp_type]["value"]
    if balance is None:
        frappe.throw(_("SimplePay returned a nonnumeric leave balance; approval blocked"))
    requested = flt(doc.total_leave_days)
    if requested <= 0:
        frappe.throw(_("Leave days must be positive before approval"))
    if Decimal(str(balance)) < Decimal(str(requested)):
        frappe.throw(_("Insufficient SimplePay leave balance: available {0}, requested {1}").format(
            balance, requested))


@frappe.whitelist()
def refresh_all_data():
    """Queue the same sequential sync invoked once per day by the scheduler."""
    simplepay._require_payroll_access()
    if not frappe.get_single(SETTINGS).enabled:
        frappe.throw(_("Enable SimplePay first"))
    frappe.enqueue("is_attendance.controllers.simplepay_balances.sync_daily",
                   queue="long", enqueue_after_commit=True,
                   job_name="simplepay-full-read-sync")
    return {"queued": True}
