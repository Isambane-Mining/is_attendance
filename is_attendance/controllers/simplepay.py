# Copyright (c) 2026, BuFf0k and contributors
# SimplePay leave and employee directory integration.


import json
from datetime import timedelta
from urllib.parse import urlparse

import frappe
import requests
from frappe import _
from frappe.utils import flt, getdate, now_datetime

SETTINGS = 'SimplePay Settings'
SYNC = 'SimplePay Leave Sync'
DIRECTORY = 'SimplePay Employee IDs'
EMPLOYEE_MAP = 'SimplePay Employee Map'


class SimplePayError(Exception):
    pass


def _settings():
    return frappe.get_single(SETTINGS)


def _enabled(settings, operation=None):
    if not settings.enabled:
        raise SimplePayError('SimplePay is disabled in Settings')
    if operation and not settings.get(operation):
        raise SimplePayError(f'SimplePay operation {operation} is disabled')


def _endpoint(settings):
    base = (settings.api_base_url or '').strip().rstrip('/')
    u = urlparse(base)
    if (u.scheme != 'https' or u.hostname != 'api.payroll.simplepay.cloud'
            or u.username or u.password or u.query or u.fragment or u.port
            or u.path.rstrip('/') != '/v1'):
        raise SimplePayError('SimplePay endpoint must be https://api.payroll.simplepay.cloud/v1')
    return base


def _request(method, path, *, params=None, payload=None):
    settings = _settings()
    _enabled(settings)
    key = settings.get_password('api_key')
    if not key:
        raise SimplePayError('SimplePay API Key has not been configured')
    try:
        response = requests.request(
            method, _endpoint(settings) + '/' + path.lstrip('/'),
            headers={'Authorization': key, 'Accept': 'application/json', 'Content-Type': 'application/json'},
            params=params, json=payload, timeout=max(5, min(60, int(settings.timeout_seconds or 20))),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise SimplePayError('SimplePay connection failed; write outcome may be uncertain') from exc
    if response.status_code >= 300:
        raise SimplePayError(f'SimplePay HTTP {response.status_code}')
    try:
        return response.json() if response.content else {}
    except ValueError as exc:
        raise SimplePayError('SimplePay returned invalid JSON') from exc


def _company_row(settings, company):
    rows = [r for r in (settings.companies or []) if r.company == company and r.enabled]
    if len(rows) != 1 or not str(rows[0].simplepay_client_id or '').isdigit():
        raise SimplePayError(f'Exactly one enabled SimplePay client mapping required for {company}')
    return rows[0]


def _type_row(settings, company, leave_type):
    rows = [r for r in (settings.leave_types or []) if r.company == company and r.leave_type == leave_type and r.enabled]
    if len(rows) != 1 or not str(rows[0].simplepay_type_id or '').isdigit():
        raise SimplePayError(f'Exactly one enabled leave-type mapping required for {company} / {leave_type}')
    return rows[0]


def _mapping(employee, company, leave_type=None):
    settings = _settings()
    _enabled(settings)
    client_id = str(_company_row(settings, company).simplepay_client_id)
    mappings = frappe.get_all(EMPLOYEE_MAP, filters={'employee': employee, 'enabled': 1}, fields=['name', 'simplepay_employee'])
    if len(mappings) != 1:
        raise SimplePayError(f'Exactly one enabled SimplePay Employee Map required for {employee}')
    directory = frappe.db.get_value(DIRECTORY, mappings[0].simplepay_employee,
        ['simplepay_employee_id', 'simplepay_client_id', 'company', 'is_active'], as_dict=True)
    if not directory or not directory.is_active:
        raise SimplePayError(f'Mapped SimplePay employee for {employee} is missing or inactive')
    if str(directory.simplepay_client_id) != client_id or directory.company != company:
        raise SimplePayError(f'SimplePay employee mapping for {employee} does not match company {company}')
    if not str(directory.simplepay_employee_id).isdigit():
        raise SimplePayError(f'Invalid SimplePay employee identifier for {employee}')
    result = {'employee_id': str(directory.simplepay_employee_id)}
    if leave_type:
        result['type_id'] = int(_type_row(settings, company, leave_type).simplepay_type_id)
    return result


def _days(doc, type_id, settings):
    start, end = getdate(doc.from_date), getdate(doc.to_date)
    if end < start or (end - start).days > 365:
        raise SimplePayError('Invalid leave date range or range longer than a year')
    half = getdate(doc.half_day_date or doc.from_date) if doc.half_day else None
    if half and not start <= half <= end:
        raise SimplePayError('Half-day date is outside the leave application')
    if half and flt(settings.half_day_hours) <= 0:
        raise SimplePayError('Configure Half-Day Hours')
    holiday_dates = set()
    if settings.leave_date_mode != 'All Calendar Days':
        holiday_list = doc.get('holiday_list') or frappe.db.get_value('Employee', doc.employee, 'holiday_list')
        if not holiday_list:
            raise SimplePayError('Employee holiday list is not configured')
        rows = frappe.get_all('Holiday', filters={'parent': holiday_list, 'holiday_date': ['between', [start, end]]}, fields=['holiday_date'])
        holiday_dates = {getdate(row.holiday_date) for row in rows}
    days, day = [], start
    while day <= end:
        if day not in holiday_dates:
            days.append({'date': day.isoformat(), 'hours': flt(settings.half_day_hours) if day == half else None, 'type_id': type_id})
        day += timedelta(days=1)
    if not days:
        raise SimplePayError('No working leave days to submit')
    return days


def _get_sync(application):
    return frappe.db.get_value(SYNC, {'leave_application': application}, 'name')


def _save_status(name, status, error=None, **extra):
    extra.update({'status': status, 'last_error': str(error)[:1000] if error else ''})
    frappe.db.set_value(SYNC, name, extra, update_modified=True)


def _queue(application, cancellation=False):
    frappe.enqueue('is_attendance.controllers.simplepay.process_cancellation' if cancellation
        else 'is_attendance.controllers.simplepay.process_approved_leave',
        queue='long', enqueue_after_commit=True,
        job_name=f"simplepay-{'cancel' if cancellation else 'create'}-{application}", application_name=application)


def on_submit(doc, method=None):
    settings = _settings()
    if doc.status != 'Approved' or not settings.enabled or not settings.sync_approved_leave:
        return
    if _get_sync(doc.name):
        return
    mapped = _mapping(doc.employee, doc.company, doc.leave_type)
    dates = _days(doc, mapped['type_id'], settings)
    frappe.get_doc({'doctype': SYNC, 'leave_application': doc.name, 'employee': doc.employee,
        'company': doc.company, 'simplepay_employee_id': mapped['employee_id'],
        'simplepay_type_id': str(mapped['type_id']), 'status': 'Pending',
        'dates_json': json.dumps(dates, sort_keys=True)}).insert(ignore_permissions=True)
    _queue(doc.name)


def on_cancel(doc, method=None):
    name = _get_sync(doc.name)
    if not name:
        return
    sync = frappe.get_doc(SYNC, name)
    if sync.status == 'Cancelled':
        return
    if sync.status == 'Pending' and not sync.external_ids_json:
        _save_status(name, 'Cancelled')
        return
    _save_status(name, 'Pending Cancellation')
    settings = _settings()
    if settings.enabled and settings.sync_cancelled_leave:
        _queue(doc.name, cancellation=True)


def _normalise_days(data):
    result = []
    for entry in data:
        for item in entry if isinstance(entry, list) else [entry]:
            if isinstance(item, dict):
                result.append(item)
    return result


def _matches(existing, planned):
    return str(existing.get('date')) == planned['date'] and str(existing.get('type_id')) == str(planned['type_id']) and (
        (existing.get('hours') is None and planned['hours'] is None) or
        (existing.get('hours') is not None and planned['hours'] is not None and abs(flt(existing['hours']) - flt(planned['hours'])) < .001))


def _external_days(employee_id):
    data = _request('GET', f'employees/{int(employee_id)}/leave_days')
    if not isinstance(data, list):
        raise SimplePayError('Unexpected leave days response')
    return _normalise_days(data)


def process_approved_leave(application_name):
    name = _get_sync(application_name)
    if not name:
        return
    sync = frappe.get_doc(SYNC, name)
    if sync.status != 'Pending':
        return
    doc = frappe.get_doc('Leave Application', application_name)
    if doc.docstatus != 1 or doc.status != 'Approved':
        _save_status(name, 'Error', 'Leave Application is not submitted and approved')
        return
    try:
        _enabled(_settings(), 'sync_approved_leave')
        # Confirm that the company / employee mapping has not been changed since submission.
        mapped = _mapping(doc.employee, doc.company, doc.leave_type)
        if mapped['employee_id'] != str(sync.simplepay_employee_id) or str(mapped['type_id']) != str(sync.simplepay_type_id):
            raise SimplePayError('Employee or leave mapping changed since submission; review manually')
        planned = json.loads(sync.dates_json)
        _save_status(name, 'Syncing', last_attempt=now_datetime())
        frappe.db.commit()  # Durable attempt marker before external write.
        existing = _external_days(sync.simplepay_employee_id)
        if any(_matches(day, p) for day in existing for p in planned):
            _save_status(name, 'Uncertain', 'Overlapping SimplePay leave dates found; review before creating')
            return
        response = _request('POST', f'employees/{int(sync.simplepay_employee_id)}/leave_days/create_multiple', payload={'dates': planned})
        ids = response.get('ids') if isinstance(response, dict) else None
        if not isinstance(ids, list) or len(ids) != len(planned) or not all(str(i).isdigit() for i in ids):
            _save_status(name, 'Uncertain', 'Response did not contain all leave-day IDs')
            return
        _save_status(name, 'Synced', external_ids_json=json.dumps(ids), synced_at=now_datetime())
    except Exception as exc:
        _save_status(name, 'Uncertain', str(exc))
        frappe.log_error(title='SimplePay leave sync', message=f'{application_name}: {type(exc).__name__}: {exc}')


def process_cancellation(application_name):
    name = _get_sync(application_name)
    if not name:
        return
    sync = frappe.get_doc(SYNC, name)
    if sync.status != 'Pending Cancellation':
        return
    try:
        _enabled(_settings(), 'sync_cancelled_leave')
        ids = json.loads(sync.external_ids_json or '[]')
        if not ids:
            _save_status(name, 'Uncertain', 'No known external IDs; reconcile manually')
            return
        _save_status(name, 'Syncing', last_attempt=now_datetime())
        frappe.db.commit()
        existing = _external_days(sync.simplepay_employee_id)
        by_id = {str(x.get('id')): x for x in existing}
        for day_id in ids:
            if str(day_id) in by_id:
                _request('DELETE', f'leave_days/{int(day_id)}')
        remaining = {str(x.get('id')) for x in _external_days(sync.simplepay_employee_id)}
        if any(str(day_id) in remaining for day_id in ids):
            _save_status(name, 'Uncertain', 'Some external leave days remain')
        else:
            _save_status(name, 'Cancelled')
    except Exception as exc:
        _save_status(name, 'Uncertain', str(exc))
        frappe.log_error(title='SimplePay leave cancellation', message=f'{application_name}: {type(exc).__name__}: {exc}')


def _require_payroll_access():
    if not ({'System Manager', 'Payroll Manager', 'HR Manager'} & set(frappe.get_roles())):
        frappe.throw(_('Not permitted'), frappe.PermissionError)


@frappe.whitelist()
def test_connection():
    _require_payroll_access()
    return {'connected': True, 'clients': _request('GET', 'clients/')}


@frappe.whitelist()
def list_leave_types(company):
    _require_payroll_access()
    client = _company_row(_settings(), company)
    return _request('GET', f'clients/{int(client.simplepay_client_id)}/leave_types')


@frappe.whitelist()
def get_leave_balances(employee, as_of=None):
    if not frappe.has_permission('Employee', 'read', employee):
        frappe.throw(_('Not permitted'), frappe.PermissionError)
    doc = frappe.get_doc('Employee', employee)
    mapped = _mapping(employee, doc.company)
    date = getdate(as_of).isoformat() if as_of and as_of != 'latest' else ('latest' if as_of == 'latest' else getdate().isoformat())
    data = _request('GET', f"employees/{mapped['employee_id']}/leave_balances", params={'date': date})
    names = {str(m.simplepay_type_id): m.leave_type for m in _settings().leave_types if m.company == doc.company and m.enabled}
    if isinstance(data, dict):
        balances = [{'type_id': k, 'leave_type': names.get(str(k)), 'balance': v, 'as_of': date} for k, v in data.items()]
    elif isinstance(data, list):
        balances = [{'type_id': str(r['type_id']), 'leave_type': names.get(str(r['type_id'])), 'balance': r['balance'], 'as_of': r.get('date')} for r in data]
    else:
        raise SimplePayError('Unexpected SimplePay balance response')
    return {'source': 'SimplePay', 'employee': employee, 'as_of_requested': date, 'fetched_at': str(now_datetime()), 'balances': balances}


@frappe.whitelist()
def reconcile(application_name):
    _require_payroll_access()
    name = _get_sync(application_name)
    if not name:
        frappe.throw(_('No SimplePay sync record found'))
    sync = frappe.get_doc(SYNC, name)
    existing = _external_days(sync.simplepay_employee_id)
    planned = json.loads(sync.dates_json or '[]')
    ids = json.loads(sync.external_ids_json or '[]')
    found = {str(x.get('id')) for x in existing}
    return {'status': sync.status,
        'owned_ids_present': [i for i in ids if str(i) in found],
        'owned_ids_missing': [i for i in ids if str(i) not in found],
        'date_matches': [{'planned': p, 'matching_external_ids': [x.get('id') for x in existing if _matches(x, p)]} for p in planned]}


def _directory_records(payload, client_id, company):
    if not isinstance(payload, list):
        raise SimplePayError('Unexpected SimplePay employee listing response')
    records = {}
    for wrapper in payload:
        raw = wrapper.get('employee') if isinstance(wrapper, dict) else None
        if not isinstance(raw, dict) or not str(raw.get('id') or '').isdigit():
            raise SimplePayError('SimplePay employee listing contained a record without an ID; no updates applied')
        ident = str(raw['id'])
        if ident in records:
            raise SimplePayError(f'Duplicate SimplePay employee ID {ident} in API response')
        records[ident] = {
            'simplepay_employee_id': ident, 'simplepay_client_id': str(client_id), 'company': company,
            'employee_number': str(raw.get('number') or ''),
            'first_name': str(raw.get('first_name') or '')[:140],
            'last_name': str(raw.get('last_name') or '')[:140],
        }
    return records


def _pull_company(company, client_id):
    # Stage/validate the full API response before touching local data.
    records = _directory_records(_request('GET', f'clients/{int(client_id)}/employees'), client_id, company)
    at = now_datetime()
    if not records:
        raise SimplePayError('Empty employee list: refusing to deactivate existing directory')
    for ident, values in records.items():
        # ID is globally stable in SimplePay; name is the numeric external ID, with prefix.
        name = f'SP-{ident}'
        if frappe.db.exists(DIRECTORY, name):
            old = frappe.db.get_value(DIRECTORY, name, ['company', 'simplepay_client_id'], as_dict=True)
            if old.company != company or str(old.simplepay_client_id) != str(client_id):
                raise SimplePayError(f'Employee {ident} already belongs to another client/company; manual review required')
            frappe.db.set_value(DIRECTORY, name, dict(values, is_active=1, last_seen=at), update_modified=True)
        else:
            frappe.get_doc(dict(doctype=DIRECTORY, record_key=name, is_active=1, last_seen=at, **values)).insert(ignore_permissions=True)
    # Mark missing records inactive, never delete: mappings and audit history remain intact.
    existing = frappe.get_all(DIRECTORY, filters={'company': company, 'simplepay_client_id': str(client_id)}, fields=['name'])
    for row in existing:
        if row.name not in {f'SP-{ident}' for ident in records}:
            frappe.db.set_value(DIRECTORY, row.name, 'is_active', 0, update_modified=True)
    return len(records)


def sync_employee_directory():
    """Called by daily scheduler; safe to rerun, never touches Employee or mapping records."""
    settings = _settings()
    if not settings.enabled or not settings.get('sync_employee_directory'):
        return
    clients = [(r.company, str(r.simplepay_client_id)) for r in settings.companies if r.enabled]
    for company, client_id in clients:
        try:
            _pull_company(company, client_id)
            frappe.db.commit()  # Isolate failures by company.
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title='SimplePay directory sync', message=frappe.get_traceback())


@frappe.whitelist()
def refresh_employee_directory():
    _require_payroll_access()
    settings = _settings()
    _enabled(settings)
    if not settings.get('sync_employee_directory'):
        frappe.throw(_('Enable Employee Directory Sync in SimplePay Settings first'))
    frappe.enqueue('is_attendance.controllers.simplepay.sync_employee_directory', queue='long', enqueue_after_commit=True)
    return {'queued': True}
