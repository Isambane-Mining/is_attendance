# Copyright (c) 2026, BuFf0k and contributors
# SimplePay balances are authoritative; this module never writes to SimplePay.

import json
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime, flt

from is_attendance.controllers import simplepay, simplepay_mapping

DIRECTORY = "SimplePay Employee"
SETTINGS = "SimplePay Settings"
PAYROLL_FIELD = "ir_leave_as_per_payroll"


def _str(value):
    return str(value).strip() if value is not None else ""


def _balanced_payload(payload):
    """Normalize date-specific or latest balances without inventing entitlements."""
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
    return {_str(row.simplepay_type_id) for row in settings.leave_types or []
            if row.enabled and row.leave_type and _str(row.get("simplepay_client_id")) == client_id}


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
    if errors:
        frappe.log_error(title="SimplePay balance pull errors", message=json.dumps(errors[:50]))
    return {"mapped_employees": len(pairs), "updated": len(staged), "failed": len(errors),
            "failures": errors[:20], "as_of": today}


def sync_daily():
    """One scheduled job: catalogue first, balances second."""
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


def _application_mapping(employee, company=None):
    """Differentiate an unmapped employee from a broken SimplePay mapping."""
    settings = frappe.get_single(SETTINGS)
    rows = [row for row in settings.employee_map or [] if row.employee == employee]
    if not rows:
        return None
    if len(rows) != 1:
        raise simplepay.SimplePayError(f"Duplicate SimplePay mappings for Employee {employee}")
    if not settings.enabled:
        raise simplepay.SimplePayError("SimplePay is disabled for this mapped Employee")
    if not company:
        company = frappe.db.get_value("Employee", employee, "company")
    return settings, company, simplepay._mapping(employee, company)


def _application_balance(employee, leave_type=None, as_of=None):
    mapped = _application_mapping(employee)
    if mapped is None:
        return {"mapped": False, "source": "Manual", "balance": None}
    settings, company, identity = mapped
    date = getdate(as_of).isoformat() if as_of else getdate().isoformat()
    if not leave_type:
        return {"mapped": True, "source": "SimplePay", "balance": None, "as_of": date}
    type_row = simplepay._type_row(settings, company, leave_type)
    type_id = _str(type_row.simplepay_type_id)
    response = simplepay._request("GET", f"employees/{int(identity['employee_id'])}/leave_balances",
                                 params={"date": date})
    balances = _balanced_payload(response)
    if type_id not in balances or balances[type_id]["value"] is None:
        raise simplepay.SimplePayError(
            f"SimplePay has no numeric balance for {employee} / {leave_type} as of {date}")
    return {"mapped": True, "source": "SimplePay", "balance": balances[type_id]["value"],
            "leave_type": leave_type, "as_of": date}


@frappe.whitelist()
def get_application_payroll_balance(employee, leave_type=None, as_of=None):
    if not employee or not frappe.has_permission("Employee", "read", employee):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    return _application_balance(employee, leave_type, as_of)


def update_application_payroll_balance(doc, method=None):
    """Update the custom payroll balance before persistence, never the HRMS ledger."""
    if doc.docstatus != 0 or not doc.employee:
        return
    mapping = _application_mapping(doc.employee, doc.company)
    if mapping is None:
        return
    if not frappe.get_meta("Leave Application").has_field(PAYROLL_FIELD):
        frappe.throw(_("Missing Leave Application custom field: {0}").format(PAYROLL_FIELD))
    if not doc.leave_type:
        doc.set(PAYROLL_FIELD, None)
        return
    result = _application_balance(doc.employee, doc.leave_type, doc.from_date)
    doc.set(PAYROLL_FIELD, result["balance"])


def before_submit(doc, method=None):
    """Validate entitlement on demand only when enabled; never modify HRMS ledger."""
    settings = frappe.get_single(SETTINGS)
    if (not settings.enabled or not settings.get("enforce_leave_balance")
            or doc.status != "Approved"):
        return
    if frappe.db.get_value("Leave Type", doc.leave_type, "is_lwp"):
        return
    if _application_mapping(doc.employee, doc.company) is None:
        return
    mapped = simplepay._mapping(doc.employee, doc.company, doc.leave_type)
    sp_type = _str(mapped["type_id"])
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
    simplepay._require_payroll_access()
    if not frappe.get_single(SETTINGS).enabled:
        frappe.throw(_("Enable SimplePay first"))
    frappe.enqueue("is_attendance.controllers.simplepay_balances.sync_daily",
                   queue="long", enqueue_after_commit=True,
                   job_name="simplepay-full-read-sync")
    return {"queued": True}
