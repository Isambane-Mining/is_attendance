# Copyright (c) 2026, BuFf0k and contributors
# SimplePay integration. Frappe Employee records are not modified.


import json
from datetime import timedelta
from urllib.parse import urlparse

import frappe
import requests
from frappe import _
from frappe.utils import flt, getdate, now_datetime

SETTINGS = 'SimplePay Settings'
SYNC = 'SimplePay Leave Sync'
DIRECTORY = 'SimplePay Employee'
MAP = 'SimplePay Employee Map'  # Child table in SimplePay Settings, not a standalone DocType.


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
    parsed = urlparse(base)
    if (parsed.scheme != 'https' or parsed.hostname != 'api.payroll.simplepay.cloud'
            or parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.rstrip('/') != '/v1'):
        raise SimplePayError('SimplePay API URL must be https://api.payroll.simplepay.cloud/v1')
    return base


def _request(method, path, *, params=None, payload=None):
    settings = _settings()
    _enabled(settings)
    api_key = settings.get_password('api_key')
    if not api_key:
        raise SimplePayError('SimplePay API Key is missing')
    try:
        response = requests.request(
            method, _endpoint(settings) + '/' + path.lstrip('/'),
            headers={'Authorization': api_key, 'Accept': 'application/json',
                     'Content-Type': 'application/json'},
            params=params, json=payload,
            timeout=max(5, min(60, int(settings.timeout_seconds or 20))),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise SimplePayError('SimplePay connection failed; a write outcome may be uncertain') from exc
    if response.status_code >= 300:
        raise SimplePayError(f'SimplePay HTTP {response.status_code}')
    try:
        return response.json() if response.content else {}
    except ValueError as exc:
        raise SimplePayError('SimplePay returned invalid JSON') from exc


def _company_row(settings, company):
    rows = [r for r in (settings.companies or []) if r.company == company and r.enabled]
    if len(rows) != 1 or not str(rows[0].simplepay_client_id or '').isdigit():
        raise SimplePayError(f'Exactly one enabled SimplePay client is required for {company}')
    return rows[0]


def _type_row(settings, company, leave_type):
    rows = [r for r in (settings.leave_types or []) if r.company == company and
            r.leave_type == leave_type and r.enabled]
    if len(rows) != 1 or not str(rows[0].simplepay_type_id or '').isdigit():
        raise SimplePayError(f'Exactly one leave type mapping required for {company} / {leave_type}')
    return rows[0]


def _mapping(employee, company, leave_type=None):
    """Resolve one manually selected row, never try matching names or payroll numbers."""
    settings = _settings()
    _enabled(settings)
    client_id = str(_company_row(settings, company).simplepay_client_id)
    rows = [r for r in (settings.employee_map or []) if r.employee == employee]
    if len(rows) != 1 or not rows[0].simplepay_employee:
        raise SimplePayError(f'Exactly one SimplePay mapping required for Employee {employee}')
    directory = frappe.db.get_value(DIRECTORY, rows[0].simplepay_employee,
        ['simplepay_employee_id', 'simplepay_client_id', 'company', 'is_active'], as_dict=True)
    if not directory or not directory.is_active:
        raise SimplePayError(f'SimplePay directory employee is missing or inactive for {employee}')
    if directory.company != company or str(directory.simplepay_client_id) != client_id:
        raise SimplePayError(f'Employee {employee} is mapped to a different payroll company')
    if not str(directory.simplepay_employee_id or '').isdigit():
        raise SimplePayError(f'Invalid SimplePay ID for {employee}')
    result = {'employee_id': str(directory.simplepay_employee_id)}
    if leave_type:
        result['type_id'] = int(_type_row(settings, company, leave_type).simplepay_type_id)
    return result


def _days(doc, type_id, settings):
    start, end = getdate(doc.from_date), getdate(doc.to_date)
    if end < start or (end - start).days > 365:
        raise SimplePayError('Invalid leave date range or leave range longer than one year')
    half = getdate(doc.half_day_date or doc.from_date) if doc.half_day else None
    if half and not start <= half <= end:
        raise SimplePayError('Half-day date outside application')
    if half and flt(settings.half_day_hours) <= 0:
        raise SimplePayError('Configure Half-Day Hours before syncing half-day leave')
    holiday_dates = set()
    if settings.leave_date_mode != 'All Calendar Days':
        holiday_list = doc.get('holiday_list') or frappe.db.get_value('Employee', doc.employee, 'holiday_list')
        if not holiday_list:
            raise SimplePayError('Employee holiday list is missing')
        holiday_dates = {getdate(x.holiday_date) for x in frappe.get_all(
            'Holiday', filters={'parent': holiday_list, 'holiday_date': ['between', [start, end]]},
            fields=['holiday_date'])}
    result = []
    date = start
    while date <= end:
        if date not in holiday_dates:
            result.append({'date': date.isoformat(), 'hours': flt(settings.half_day_hours) if date == half else None,
                           'type_id': type_id})
        date += timedelta(days=1)
    if not result:
        raise SimplePayError('No applicable working dates found')
    return result


def _get_sync(application):
    return frappe.db.get_value(SYNC, {'leave_application': application}, 'name')


def _save_status(name, status, error=None, **fields):
    fields.update({'status': status, 'last_error': str(error)[:1000] if error else ''})
    frappe.db.set_value(SYNC, name, fields, update_modified=True)


def _queue(application, cancellation=False):
    fn = ('is_attendance.controllers.simplepay.process_cancellation' if cancellation else
          'is_attendance.controllers.simplepay.process_approved_leave')
    frappe.enqueue(fn, queue='long', enqueue_after_commit=True,
                   job_name=f"simplepay-{'cancel' if cancellation else 'create'}-{application}",
                   application_name=application)


def on_submit(doc, method=None):
    settings = _settings()
    if doc.status != 'Approved' or not settings.enabled or not settings.sync_approved_leave:
        return
    if _get_sync(doc.name):
        return
    mapped = _mapping(doc.employee, doc.company, doc.leave_type)
    dates = _days(doc, mapped['type_id'], settings)
    frappe.get_doc({
        'doctype': SYNC, 'leave_application': doc.name, 'employee': doc.employee,
        'company': doc.company, 'simplepay_employee_id': mapped['employee_id'],
        'simplepay_type_id': str(mapped['type_id']), 'status': 'Pending',
        'dates_json': json.dumps(dates, sort_keys=True),
    }).insert(ignore_permissions=True)
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


def _normalise_days(payload):
    if not isinstance(payload, list):
        raise SimplePayError('Unexpected SimplePay leave-days response')
    return [item for wrapper in payload for item in (wrapper if isinstance(wrapper, list) else [wrapper])
            if isinstance(item, dict)]


def _matches(external, planned):
    return (str(external.get('date')) == planned['date'] and
            str(external.get('type_id')) == str(planned['type_id']) and
            ((external.get('hours') is None and planned['hours'] is None) or
             (external.get('hours') is not None and planned['hours'] is not None and
              abs(flt(external['hours']) - flt(planned['hours'])) < .001)))


def _external_days(employee_id):
    return _normalise_days(_request('GET', f'employees/{int(employee_id)}/leave_days'))


def process_approved_leave(application_name):
    name = _get_sync(application_name)
    if not name:
        return
    sync = frappe.get_doc(SYNC, name)
    if sync.status != 'Pending':
        return
    application = frappe.get_doc('Leave Application', application_name)
    if application.docstatus != 1 or application.status != 'Approved':
        _save_status(name, 'Error', 'Leave Application is no longer approved and submitted')
        return
    try:
        _enabled(_settings(), 'sync_approved_leave')
        mapped = _mapping(application.employee, application.company, application.leave_type)
        if mapped['employee_id'] != str(sync.simplepay_employee_id) or str(mapped['type_id']) != str(sync.simplepay_type_id):
            raise SimplePayError('Employee / leave mapping changed after submission; manual review required')
        planned = json.loads(sync.dates_json or '[]')
        _save_status(name, 'Syncing', last_attempt=now_datetime())
        frappe.db.commit()  # Durable marker before external POST.
        if any(_matches(x, p) for x in _external_days(sync.simplepay_employee_id) for p in planned):
            _save_status(name, 'Uncertain', 'Matching existing SimplePay leave; manual reconciliation required')
            return
        result = _request('POST', f'employees/{int(sync.simplepay_employee_id)}/leave_days/create_multiple',
                          payload={'dates': planned})
        ids = result.get('ids') if isinstance(result, dict) else None
        if not isinstance(ids, list) or len(ids) != len(planned) or not all(str(x).isdigit() for x in ids):
            _save_status(name, 'Uncertain', 'Leave creation response missing expected IDs')
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
            _save_status(name, 'Uncertain', 'No recorded external IDs; manual reconciliation required')
            return
        _save_status(name, 'Syncing', last_attempt=now_datetime())
        frappe.db.commit()
        by_id = {str(x.get('id')) for x in _external_days(sync.simplepay_employee_id)}
        for day_id in ids:
            if str(day_id) in by_id:
                _request('DELETE', f'leave_days/{int(day_id)}')
        remaining = {str(x.get('id')) for x in _external_days(sync.simplepay_employee_id)}
        if any(str(x) in remaining for x in ids):
            _save_status(name, 'Uncertain', 'Some SimplePay leave days remain')
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
    row = _company_row(_settings(), company)
    return _request('GET', f'clients/{int(row.simplepay_client_id)}/leave_types')


@frappe.whitelist()
def get_leave_balances(employee, as_of=None):
    if not frappe.has_permission('Employee', 'read', employee):
        frappe.throw(_('Not permitted'), frappe.PermissionError)
    employee_doc = frappe.get_doc('Employee', employee)
    mapped = _mapping(employee, employee_doc.company)
    date = (getdate(as_of).isoformat() if as_of and as_of != 'latest'
            else ('latest' if as_of == 'latest' else getdate().isoformat()))
    payload = _request('GET', f"employees/{mapped['employee_id']}/leave_balances", params={'date': date})
    types = {str(x.simplepay_type_id): x.leave_type for x in _settings().leave_types
             if x.company == employee_doc.company and x.enabled}
    if isinstance(payload, dict):
        balances = [{'type_id': k, 'leave_type': types.get(str(k)), 'balance': v, 'as_of': date}
                    for k, v in payload.items()]
    elif isinstance(payload, list):
        balances = [{'type_id': str(x['type_id']), 'leave_type': types.get(str(x['type_id'])),
                     'balance': x['balance'], 'as_of': x.get('date') or date} for x in payload]
    else:
        raise SimplePayError('Unexpected SimplePay balance response')
    return {'source': 'SimplePay', 'employee': employee, 'as_of_requested': date,
            'fetched_at': str(now_datetime()), 'balances': balances}


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
            'owned_ids_present': [x for x in ids if str(x) in found],
            'owned_ids_missing': [x for x in ids if str(x) not in found],
            'date_matches': [{'planned': x, 'matching_external_ids': [d.get('id') for d in existing if _matches(d, x)]}
                             for x in planned]}


# Employee directory and Settings child-table mapping --------------------------------

def _directory_records(payload, client_id, company):
    if not isinstance(payload, list):
        raise SimplePayError(f'Unexpected employee API response for {company}')
    records = {}
    for item in payload:
        raw = item.get('employee') if isinstance(item, dict) else None
        if not isinstance(raw, dict) or not str(raw.get('id') or '').isdigit():
            raise SimplePayError(f'Employee API entry without valid ID for {company}; no changes applied')
        employee_id = str(raw['id'])
        if employee_id in records:
            raise SimplePayError(f'Duplicate SimplePay ID {employee_id} returned for {company}')
        records[employee_id] = {
            'record_key': f'SP-{employee_id}', 'company': company,
            'simplepay_client_id': str(client_id), 'simplepay_employee_id': employee_id,
            'employee_number': str(raw.get('number') or ''),
            'first_name': str(raw.get('first_name') or '')[:140],
            'last_name': str(raw.get('last_name') or '')[:140],
        }
    if not records:
        raise SimplePayError(f'Empty SimplePay employee list for {company}; refusing destructive update')
    return records


def _configured_clients(settings):
    result, companies, clients = [], set(), set()
    for row in settings.companies or []:
        if not row.enabled:
            continue
        company = row.company
        client = str(row.simplepay_client_id or '')
        if not company or not client.isdigit() or company in companies or client in clients:
            raise SimplePayError('Every enabled Company must have a unique numeric SimplePay Client ID')
        companies.add(company)
        clients.add(client)
        result.append((company, client))
    if not result:
        raise SimplePayError('No enabled SimplePay company mappings configured')
    return result


def _sort_key(employee_id):
    return (int(employee_id), str(employee_id)) if str(employee_id).isdigit() else (10**30, str(employee_id))


def _refresh_directory():
    """Fetch all clients before modifying DB. Preserve mapping selections and child row names."""
    settings = _settings()
    _enabled(settings)
    clients = _configured_clients(settings)
    staged = {}
    for company, client_id in clients:
        staged[(company, client_id)] = _directory_records(
            _request('GET', f'clients/{int(client_id)}/employees'), client_id, company)

    # Existing maps are preserved, including unmapped and employees no longer returned by API.
    # Validate the entire import before changing the directory or Settings.
    seen_ids = set()
    for records in staged.values():
        for external_id in records:
            if external_id in seen_ids:
                raise SimplePayError(f'SimplePay employee {external_id} appears in multiple configured clients')
            seen_ids.add(external_id)
            existing = frappe.db.get_value(DIRECTORY, f'SP-{external_id}',
                                            ['company', 'simplepay_client_id'], as_dict=True)
            if existing and ((existing.company, str(existing.simplepay_client_id)) not in staged):
                raise SimplePayError(f'SimplePay employee {external_id} has an existing conflicting company')
            if existing and (existing.company, str(existing.simplepay_client_id)) != next(
                    k for k, recs in staged.items() if external_id in recs):
                raise SimplePayError(f'SimplePay employee {external_id} moved clients; review manually')

    at = now_datetime()
    created = updated = deactivated = added_rows = 0
    configured_companies = [c for c, _ in clients]
    for (company, client_id), records in staged.items():
        for external_id, values in records.items():
            name = f'SP-{external_id}'
            if frappe.db.exists(DIRECTORY, name):
                frappe.db.set_value(DIRECTORY, name, dict(values, is_active=1, last_seen=at), update_modified=True)
                updated += 1
            else:
                frappe.get_doc(dict(doctype=DIRECTORY, is_active=1, last_seen=at, **values)).insert(ignore_permissions=True)
                created += 1
        # Only mark missing IDs inactive after a valid, nonempty fetch from that client.
        names = {f'SP-{x}' for x in records}
        for row in frappe.get_all(DIRECTORY, filters={'company': company,
                'simplepay_client_id': client_id, 'is_active': 1}, fields=['name']):
            if row.name not in names:
                frappe.db.set_value(DIRECTORY, row.name, 'is_active', 0, update_modified=True)
                deactivated += 1

    # Re-read just before saving to retain current manually selected employee links.
    settings = _settings()
    rows = settings.get('employee_map') or []
    by_directory = {}
    for row in rows:
        if not row.simplepay_employee:
            raise SimplePayError('An Employee Mapping row has no SimplePay Employee; correct it before sync')
        if row.simplepay_employee in by_directory:
            raise SimplePayError('Duplicate SimplePay Employee in Settings child table')
        by_directory[row.simplepay_employee] = row
    for records in staged.values():
        for external_id in records:
            name = f'SP-{external_id}'
            if name not in by_directory:
                settings.append('employee_map', {'simplepay_employee': name})
                added_rows += 1
    # Sort by numeric SimplePay ID, NOT payroll number or lexicographic link name.
    # Sorting includes historical rows, so their manually chosen Employee remains untouched.
    def key(row):
        directory_name = str(row.simplepay_employee or '')
        external_id = directory_name[3:] if directory_name.startswith('SP-') else directory_name
        return _sort_key(external_id)
    prior_order = [row.name for row in settings.employee_map]
    settings.employee_map = sorted(settings.employee_map, key=key)
    for idx, row in enumerate(settings.employee_map, 1):
        row.idx = idx
    new_order = [row.name for row in settings.employee_map]
    if added_rows or new_order != prior_order:
        settings.save(ignore_permissions=True)
    else:
        # Even without new rows the order is kept in memory; no need to rewrite settings.
        pass
    return {'created': created, 'updated': updated, 'inactive': deactivated,
            'added_to_mapping': added_rows, 'total_mapping_rows': len(settings.employee_map),
            'companies': configured_companies}


def sync_employee_directory():
    """Daily scheduler; API fetch then update, no changes to mapped Frappe Employees."""
    if not _settings().enabled:
        return
    # The lock prevents overlapping scheduler / manual refreshes on the same site.
    with frappe.cache().lock('simplepay-employee-refresh', timeout=900, blocking_timeout=1):
        try:
            result = _refresh_directory()
            frappe.db.commit()
            return result
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title='SimplePay employee directory refresh', message=frappe.get_traceback())
            raise


@frappe.whitelist()
def refresh_employee_directory():
    _require_payroll_access()
    _enabled(_settings())
    frappe.enqueue('is_attendance.controllers.simplepay.sync_employee_directory',
                   queue='long', enqueue_after_commit=True,
                   job_name='simplepay-employee-refresh')
    return {'queued': True}
