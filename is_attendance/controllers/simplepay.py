# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

import json
from datetime import timedelta
from urllib.parse import urlparse

import frappe
import requests
from frappe import _
from frappe.utils import getdate, now_datetime, flt

SETTINGS = "SimplePay Settings"
SYNC = "SimplePay Leave Sync"


class SimplePayError(Exception):
    pass


def _settings():
    return frappe.get_single(SETTINGS)


def _enabled(settings, operation=None):
    if not settings.enabled:
        raise SimplePayError("SimplePay is disabled in Settings")
    if operation and not settings.get(operation):
        raise SimplePayError(f"SimplePay operation {operation} is disabled")


def _endpoint(settings):
    base = (settings.api_base_url or "").strip().rstrip("/")
    u = urlparse(base)
    if u.scheme != "https" or u.username or u.password or u.query or u.fragment or not u.hostname:
        raise SimplePayError("SimplePay endpoint must be a valid HTTPS API root")
    # Avoid credentials being transmitted to arbitrary hosts through the admin form.
    if u.hostname != "api.payroll.simplepay.cloud":
        raise SimplePayError("Unexpected API host; expected api.payroll.simplepay.cloud")
    if not u.path.rstrip("/").endswith("/v1"):
        raise SimplePayError("SimplePay endpoint must end with /v1")
    return base


def _request(method, path, *, params=None, payload=None):
    settings = _settings()
    _enabled(settings)
    api_key = settings.get_password("api_key")
    if not api_key:
        raise SimplePayError("SimplePay API Key has not been configured")
    # Restrict the caller to paths authored in this controller.
    url = _endpoint(settings) + "/" + path.lstrip("/")
    try:
        response = requests.request(
            method, url,
            headers={"Authorization": api_key, "Accept": "application/json", "Content-Type": "application/json"},
            params=params, json=payload,
            timeout=max(5, min(60, int(settings.timeout_seconds or 20))),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        # The server may have processed a write before a timeout occurred.
        raise SimplePayError("SimplePay connection failed; outcome may be uncertain") from exc
    if response.status_code >= 300:
        # Do not leak server response content, which can include personal data.
        raise SimplePayError(f"SimplePay HTTP {response.status_code}")
    try:
        return response.json() if response.content else {}
    except ValueError as exc:
        raise SimplePayError("SimplePay returned invalid JSON") from exc


def _company_row(settings, company):
    matches = [r for r in settings.companies if r.company == company and r.enabled]
    if len(matches) != 1 or not matches[0].simplepay_client_id:
        raise SimplePayError(f"Exactly one enabled SimplePay company mapping required for {company}")
    return matches[0]


def _type_row(settings, company, leave_type):
    matches = [r for r in settings.leave_types if r.company == company and r.leave_type == leave_type and r.enabled]
    if len(matches) != 1 or not matches[0].simplepay_type_id:
        raise SimplePayError(f"Exactly one SimplePay leave mapping required for {company} / {leave_type}")
    return matches[0]


def _mapping(employee, company, leave_type=None):
    settings = _settings()
    _enabled(settings)
    _company_row(settings, company)
    simplepay_employee_id = frappe.db.get_value("Employee", employee, "custom_simplepay_employee_id")
    if not simplepay_employee_id or not str(simplepay_employee_id).isdigit():
        raise SimplePayError(f"Employee {employee} has no valid SimplePay Employee ID")
    result = {"employee_id": str(simplepay_employee_id)}
    if leave_type:
        result["type_id"] = int(_type_row(settings, company, leave_type).simplepay_type_id)
    return result


def _days(doc, type_id, settings):
    start, end = getdate(doc.from_date), getdate(doc.to_date)
    if end < start:
        raise SimplePayError("Invalid leave date range")
    if (end - start).days > 365:
        raise SimplePayError("Leave range exceeds one year; review manually")
    half = getdate(doc.half_day_date or doc.from_date) if doc.half_day else None
    if half and not (start <= half <= end):
        raise SimplePayError("Half-day date is outside the leave application")
    if doc.half_day and flt(settings.half_day_hours) <= 0:
        raise SimplePayError("Configure Half-Day Hours before sending half-day leave")
    holiday_list = doc.get("holiday_list") or frappe.db.get_value("Employee", doc.employee, "holiday_list")
    holiday_dates = set()
    if settings.leave_date_mode != "All Calendar Days":
        if not holiday_list:
            raise SimplePayError("Employee holiday list required; use All Calendar Days only when appropriate")
        for row in frappe.get_all("Holiday", filters={"parent": holiday_list, "holiday_date": ["between", [start, end]]}, fields=["holiday_date"]):
            holiday_dates.add(getdate(row.holiday_date))
    days = []
    date = start
    while date <= end:
        if date not in holiday_dates:
            days.append({"date": date.isoformat(), "hours": flt(settings.half_day_hours) if date == half else None, "type_id": type_id})
        date += timedelta(days=1)
    if not days:
        raise SimplePayError("No working leave dates to submit")
    return days


def _get_sync(name):
    return frappe.db.get_value(SYNC, {"leave_application": name}, "name")


def _save_status(sync_name, status, error=None, **fields):
    fields.update({"status": status, "last_error": (str(error)[:1000] if error else "")})
    frappe.db.set_value(SYNC, sync_name, fields, update_modified=True)


def _queue(name, cancellation=False):
    frappe.enqueue(
        "is_attendance.controllers.simplepay.process_cancellation" if cancellation else "is_attendance.controllers.simplepay.process_approved_leave",
        queue="long", enqueue_after_commit=True,
        job_name=f"simplepay-{'cancel' if cancellation else 'create'}-{name}",
        application_name=name,
    )


def on_submit(doc, method=None):
    """Supplementary hook; does not replace existing attendance_sync hook."""
    if doc.status != "Approved" or not _settings().enabled or not _settings().sync_approved_leave:
        return
    if _get_sync(doc.name):
        return
    mapping = _mapping(doc.employee, doc.company, doc.leave_type)
    settings = _settings()
    dates = _days(doc, mapping["type_id"], settings)
    sync = frappe.get_doc({
        "doctype": SYNC, "leave_application": doc.name, "employee": doc.employee,
        "company": doc.company, "simplepay_employee_id": mapping["employee_id"],
        "simplepay_type_id": str(mapping["type_id"]), "status": "Pending",
        "dates_json": json.dumps(dates, sort_keys=True),
    })
    sync.insert(ignore_permissions=True)
    _queue(doc.name)


def on_cancel(doc, method=None):
    name = _get_sync(doc.name)
    if not name:
        return
    sync = frappe.get_doc(SYNC, name)
    if sync.status == "Cancelled":
        return
    if sync.status == "Pending" and not sync.external_ids_json:
        _save_status(name, "Cancelled")
        return
    _save_status(name, "Pending Cancellation")
    if _settings().enabled and _settings().sync_cancelled_leave:
        _queue(doc.name, cancellation=True)


def _normalise_days(data):
    """SimplePay examples contain nested one-element arrays."""
    result = []
    for entry in data:
        for item in entry if isinstance(entry, list) else [entry]:
            if isinstance(item, dict):
                result.append(item)
    return result


def _matches(existing, planned):
    return str(existing.get("date")) == planned["date"] and str(existing.get("type_id")) == str(planned["type_id"]) and (
        existing.get("hours") is None and planned["hours"] is None or
        existing.get("hours") is not None and planned["hours"] is not None and abs(flt(existing["hours"]) - flt(planned["hours"])) < .001
    )


def _external_days(employee_id):
    data = _request("GET", f"employees/{employee_id}/leave_days")
    if not isinstance(data, list):
        raise SimplePayError("Unexpected leave days response")
    return _normalise_days(data)


def process_approved_leave(application_name):
    """Non-automatic retry policy: any uncertain result needs reconciliation."""
    name = _get_sync(application_name)
    if not name:
        return
    sync = frappe.get_doc(SYNC, name)
    if sync.status != "Pending":
        return
    doc = frappe.get_doc("Leave Application", application_name)
    if doc.docstatus != 1 or doc.status != "Approved":
        _save_status(name, "Error", "Application is no longer submitted and approved")
        return
    try:
        _enabled(_settings(), "sync_approved_leave")
        planned = json.loads(sync.dates_json)
        _save_status(name, "Syncing", last_attempt=now_datetime())
        frappe.db.commit()  # Persist attempt before external write; recovery sees Syncing.
        existing = _external_days(sync.simplepay_employee_id)
        if any(_matches(day, planned_day) for day in existing for planned_day in planned):
            _save_status(name, "Uncertain", "Overlapping leave already exists in SimplePay; review before creating")
            return
        payload = {"dates": planned}
        response = _request("POST", f"employees/{sync.simplepay_employee_id}/leave_days/create_multiple", payload=payload)
        ids = response.get("ids") if isinstance(response, dict) else None
        if not isinstance(ids, list) or len(ids) != len(planned) or not all(str(v).isdigit() for v in ids):
            _save_status(name, "Uncertain", "SimplePay response did not contain all leave-day IDs")
            return
        _save_status(name, "Synced", external_ids_json=json.dumps(ids), synced_at=now_datetime())
    except Exception as exc:
        # After Syncing, even HTTP errors could have reached SimplePay.
        _save_status(name, "Uncertain", str(exc))
        frappe.log_error(title="SimplePay leave sync", message=f"{application_name}: {type(exc).__name__}: {exc}")


def process_cancellation(application_name):
    name = _get_sync(application_name)
    if not name:
        return
    sync = frappe.get_doc(SYNC, name)
    if sync.status != "Pending Cancellation":
        return
    try:
        _enabled(_settings(), "sync_cancelled_leave")
        ids = json.loads(sync.external_ids_json or "[]")
        if not ids:
            # Unknown creation outcome; never assume cancellation completed.
            _save_status(name, "Uncertain", "No externally owned IDs recorded; reconcile manually")
            return
        _save_status(name, "Syncing", last_attempt=now_datetime())
        frappe.db.commit()
        existing = _external_days(sync.simplepay_employee_id)
        by_id = {str(x.get("id")): x for x in existing}
        for day_id in ids:
            if str(day_id) in by_id:
                _request("DELETE", f"leave_days/{int(day_id)}")
        remaining = {str(x.get("id")) for x in _external_days(sync.simplepay_employee_id)}
        if any(str(day_id) in remaining for day_id in ids):
            _save_status(name, "Uncertain", "Some external leave days remain")
        else:
            _save_status(name, "Cancelled")
    except Exception as exc:
        _save_status(name, "Uncertain", str(exc))
        frappe.log_error(title="SimplePay leave cancellation", message=f"{application_name}: {type(exc).__name__}: {exc}")


def _require_payroll_access():
    if not ({"System Manager", "Payroll Manager", "HR Manager"} & set(frappe.get_roles())):
        frappe.throw(_("Not permitted"), frappe.PermissionError)


@frappe.whitelist()
def test_connection():
    _require_payroll_access()
    clients = _request("GET", "clients/")
    return {"connected": True, "clients": clients}


@frappe.whitelist()
def list_leave_types(company):
    _require_payroll_access()
    settings = _settings()
    client = _company_row(settings, company)
    return _request("GET", f"clients/{int(client.simplepay_client_id)}/leave_types")


@frappe.whitelist()
def get_leave_balances(employee, as_of=None):
    if not frappe.has_permission("Employee", "read", employee):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    _enabled(_settings())
    doc = frappe.get_doc("Employee", employee)
    mapped = _mapping(employee, doc.company)
    effective_date = getdate(as_of).isoformat() if as_of and as_of != "latest" else ("latest" if as_of == "latest" else getdate().isoformat())
    data = _request("GET", f"employees/{mapped['employee_id']}/leave_balances", params={"date": effective_date})
    settings = _settings()
    mappings = {str(m.simplepay_type_id): m.leave_type for m in settings.leave_types if m.company == doc.company and m.enabled}
    if isinstance(data, dict):
        balances = [{"type_id": k, "leave_type": mappings.get(str(k)), "balance": v, "as_of": effective_date} for k, v in data.items()]
    elif isinstance(data, list):
        balances = [{"type_id": str(r["type_id"]), "leave_type": mappings.get(str(r["type_id"])), "balance": r["balance"], "as_of": r.get("date")} for r in data]
    else:
        raise SimplePayError("Unexpected SimplePay balance response")
    return {"source": "SimplePay", "employee": employee, "as_of_requested": effective_date, "fetched_at": str(now_datetime()), "balances": balances}


@frappe.whitelist()
def reconcile(application_name):
    _require_payroll_access()
    name = _get_sync(application_name)
    if not name:
        frappe.throw(_("No SimplePay sync record found"))
    sync = frappe.get_doc(SYNC, name)
    existing = _external_days(sync.simplepay_employee_id)
    planned = json.loads(sync.dates_json or "[]")
    ids = json.loads(sync.external_ids_json or "[]")
    found_ids = {str(x.get("id")) for x in existing}
    matches = [{"planned": p, "matching_external_ids": [x.get("id") for x in existing if _matches(x, p)]} for p in planned]
    return {"status": sync.status, "owned_ids_present": [i for i in ids if str(i) in found_ids], "owned_ids_missing": [i for i in ids if str(i) not in found_ids], "date_matches": matches}
