# Copyright (c) 2026, BuFf0k and contributors
# SimplePay integration. Frappe Employee records are not modified.


import frappe
from frappe import _
from frappe.utils import now_datetime

from is_attendance.controllers import simplepay

SETTINGS = 'SimplePay Settings'
DIRECTORY = 'SimplePay Employee'
LOCK_KEY = 'simplepay-settings-catalogue-refresh'


def _numeric(value):
    value = str(value or '')
    if not value.isdigit():
        raise simplepay.SimplePayError(f'Expected numeric SimplePay identifier, got {value!r}')
    return value


def _sort_num(value):
    s = str(value or '')
    return (0, int(s)) if s.isdigit() else (1, s)


def _client_payload(payload):
    if not isinstance(payload, list):
        raise simplepay.SimplePayError('Unexpected SimplePay clients response')
    result = {}
    for wrapper in payload:
        obj = wrapper.get('client') if isinstance(wrapper, dict) else None
        if not isinstance(obj, dict):
            raise simplepay.SimplePayError('Unexpected SimplePay client entry')
        cid = _numeric(obj.get('id'))
        if cid in result:
            raise simplepay.SimplePayError(f'Duplicate SimplePay client {cid}')
        result[cid] = str(obj.get('name') or cid)[:140]
    if not result:
        raise simplepay.SimplePayError('No clients returned; refusing empty catalogue')
    return result


def _leave_payload(payload, client):
    if not isinstance(payload, dict):
        raise simplepay.SimplePayError(f'Unexpected leave types response for client {client}')
    result = {}
    for key, label in payload.items():
        tid = _numeric(key)
        result[(client, tid)] = str(label or tid)[:140]
    return result


def _items_payload(payload, client):
    if not isinstance(payload, list) or not payload or not all(isinstance(x, dict) for x in payload):
        raise simplepay.SimplePayError(f'Unexpected items_and_outputs response for client {client}')
    result = {}
    for section in payload:
        for kind, source in [('System', 'system_items'), ('Custom', 'custom_items')]:
            entries = section.get(source, [])
            if not isinstance(entries, list):
                raise simplepay.SimplePayError(f'Invalid {source} for client {client}')
            for item in entries:
                if not isinstance(item, dict):
                    raise simplepay.SimplePayError(f'Invalid {source} entry for client {client}')
                identity = item.get('name') if kind == 'System' else item.get('id')
                if identity is None or str(identity) == '':
                    raise simplepay.SimplePayError(f'{source} item missing identifier for client {client}')
                ident = str(identity)
                key = (client, ident)
                label = str(item.get('label') or ident)[:140]
                if key in result:
                    raise simplepay.SimplePayError(f'Duplicate calculation {ident} for client {client}')
                result[key] = {'label': label, 'type': str(item.get('item_type') or kind)[:140]}
    return result


def _employee_payload(payload, client):
    if not isinstance(payload, list):
        raise simplepay.SimplePayError(f'Unexpected employee list for client {client}')
    result = {}
    for wrapper in payload:
        obj = wrapper.get('employee') if isinstance(wrapper, dict) else None
        if not isinstance(obj, dict):
            raise simplepay.SimplePayError(f'Invalid employee record for client {client}')
        eid = _numeric(obj.get('id'))
        if eid in result:
            raise simplepay.SimplePayError(f'Duplicate employee {eid} for client {client}')
        result[eid] = {
            'record_key': f'SP-{eid}',
            'simplepay_employee_id': eid,
            'simplepay_client_id': client,
            'employee_number': str(obj.get('number') or '')[:140],
            'first_name': str(obj.get('first_name') or '')[:140],
            'last_name': str(obj.get('last_name') or '')[:140],
        }
    return result


def _stage():
    clients = _client_payload(simplepay._request('GET', 'clients/'))
    leave, remuneration, employees = {}, {}, {}
    for cid in clients:
        leave.update(_leave_payload(simplepay._request('GET', f'clients/{int(cid)}/leave_types'), cid))
        remuneration.update(_items_payload(simplepay._request('GET', f'clients/{int(cid)}/items_and_outputs'), cid))
        for eid, record in _employee_payload(simplepay._request('GET', f'clients/{int(cid)}/employees'), cid).items():
            if eid in employees:
                raise simplepay.SimplePayError(f'Employee ID {eid} appears in multiple clients')
            employees[eid] = record
    return clients, leave, remuneration, employees


def _unique_rows(rows, key_fn, name):
    mapping = {}
    for row in rows:
        key = key_fn(row)
        if not all(key):
            continue
        if key in mapping:
            raise simplepay.SimplePayError(f'Duplicate {name} mapping key {key}; correct Settings first')
        mapping[key] = row
    return mapping


def _reorder(settings, fieldname, key_fn):
    rows = list(settings.get(fieldname) or [])
    ordered = sorted(rows, key=key_fn)
    changed = [x.name for x in rows] != [x.name for x in ordered]
    if changed:
        settings.set(fieldname, [])
        settings.set(fieldname, ordered)
    for idx, row in enumerate(ordered, 1):
        if row.idx != idx:
            row.idx = idx
            changed = True
    return changed


def _apply(clients, leave, remuneration, employees):
    settings = frappe.get_single(SETTINGS)
    changed = False
    stats = {'clients_added': 0, 'leave_types_added': 0,
             'calculations_added': 0, 'employees_created': 0,
             'employees_updated': 0, 'employee_rows_added': 0}

    companies = _unique_rows(settings.get('companies') or [],
                             lambda r: (str(r.simplepay_client_id or ''),), 'company')
    types = _unique_rows(settings.get('leave_types') or [],
                         lambda r: (str(r.get('simplepay_client_id') or ''), str(r.simplepay_type_id or '')), 'leave type')
    items = _unique_rows(settings.get('remuneration_mappings') or [],
                         lambda r: (str(r.get('simplepay_client_id') or ''), str(r.simplepay_calculation or '')), 'calculation')
    employee_rows = _unique_rows(settings.get('employee_map') or [],
                                 lambda r: (str(r.simplepay_employee or ''),), 'employee')

    for cid, label in clients.items():
        row = companies.get((cid,))
        if not row:
            row = settings.append('companies', {'simplepay_client_id': cid, 'enabled': 0})
            companies[(cid,)] = row
            changed = True
            stats['clients_added'] += 1
        if row.get('simplepay_client_name') != label:
            row.simplepay_client_name = label
            changed = True

    client_company = {cid: (row.company or None) for (cid,), row in companies.items()}
    for (cid, tid), label in leave.items():
        row = types.get((cid, tid))
        if not row:
            row = settings.append('leave_types', {
                'simplepay_client_id': cid, 'simplepay_type_id': tid,
                'enabled': 1, 'company': client_company.get(cid),
            })
            types[(cid, tid)] = row
            changed = True
            stats['leave_types_added'] += 1
        if row.get('simplepay_type_name') != label:
            row.simplepay_type_name = label
            changed = True
        if not row.company and client_company.get(cid):
            row.company = client_company[cid]
            changed = True

    for (cid, ident), item in remuneration.items():
        row = items.get((cid, ident))
        if not row:
            row = settings.append('remuneration_mappings', {
                'simplepay_client_id': cid, 'simplepay_calculation': ident,
                'company': client_company.get(cid), 'enabled': 1,
            })
            items[(cid, ident)] = row
            changed = True
            stats['calculations_added'] += 1
        if row.get('simplepay_item_label') != item['label']:
            row.simplepay_item_label = item['label']
            changed = True
        if row.get('simplepay_item_type') != item['type']:
            row.simplepay_item_type = item['type']
            changed = True
        if not row.company and client_company.get(cid):
            row.company = client_company[cid]
            changed = True

    for eid, record in employees.items():
        old = frappe.db.get_value(DIRECTORY, f'SP-{eid}',
                                  ['simplepay_client_id'], as_dict=True)
        if old and str(old.simplepay_client_id) != record['simplepay_client_id']:
            raise simplepay.SimplePayError(f'SimplePay employee {eid} moved client; review manually')

    at = now_datetime()
    for eid, record in employees.items():
        name = f'SP-{eid}'
        record['company'] = client_company.get(record['simplepay_client_id'])
        record['is_active'] = 1
        record['last_seen'] = at
        if frappe.db.exists(DIRECTORY, name):
            frappe.db.set_value(DIRECTORY, name, record, update_modified=True)
            stats['employees_updated'] += 1
        else:
            frappe.get_doc(dict(doctype=DIRECTORY, **record)).insert(ignore_permissions=True)
            stats['employees_created'] += 1
        if (name,) not in employee_rows:
            row = settings.append('employee_map', {'simplepay_employee': name})
            employee_rows[(name,)] = row
            stats['employee_rows_added'] += 1
            changed = True

    changed |= _reorder(settings, 'companies', lambda r: _sort_num(r.simplepay_client_id))
    changed |= _reorder(settings, 'leave_types', lambda r: (_sort_num(r.get('simplepay_client_id')), _sort_num(r.simplepay_type_id)))
    changed |= _reorder(settings, 'remuneration_mappings', lambda r: (_sort_num(r.get('simplepay_client_id')), str(r.simplepay_calculation or '')))
    changed |= _reorder(settings, 'employee_map', lambda r: _sort_num(str(r.simplepay_employee or '').removeprefix('SP-')))
    if changed:
        settings.save(ignore_permissions=True)
    stats.update({'client_rows': len(settings.get('companies') or []),
                  'leave_rows': len(settings.get('leave_types') or []),
                  'remuneration_rows': len(settings.get('remuneration_mappings') or []),
                  'employee_rows': len(settings.get('employee_map') or []),
                  'finished_at': str(at)})
    return stats


def sync_all_mappings():
    """Used by scheduler and manual queue; never writes to SimplePay."""
    if not frappe.get_single(SETTINGS).enabled:
        return {'skipped': 'SimplePay disabled'}
    with frappe.cache().lock(LOCK_KEY, timeout=1800, blocking_timeout=1):
        staged = _stage()
        return _apply(*staged)


@frappe.whitelist()
def refresh_all_mappings():
    if not ({'System Manager', 'HR Manager'} & set(frappe.get_roles())):
        frappe.throw(_('Not permitted'), frappe.PermissionError)
    if not frappe.get_single(SETTINGS).enabled:
        frappe.throw(_('Enable SimplePay first'))
    frappe.enqueue('is_attendance.controllers.simplepay_mapping.sync_all_mappings',
                   queue='long', enqueue_after_commit=True,
                   job_name='simplepay-full-mapping-refresh')
    return {'queued': True}


@frappe.whitelist()
def list_clients():
    if not ({'System Manager', 'HR Manager'} & set(frappe.get_roles())):
        frappe.throw(_('Not permitted'), frappe.PermissionError)
    clients = _client_payload(simplepay._request('GET', 'clients/'))
    return [{'id': cid, 'name': name} for cid, name in clients.items()]
