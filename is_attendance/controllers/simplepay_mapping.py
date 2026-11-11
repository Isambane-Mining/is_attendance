# Copyright (c) 2026, BuFf0k and contributors
# Read-only SimplePay catalogue importer. All writes are local to Frappe.


import frappe
from frappe import _
from frappe.utils import now_datetime

from is_attendance.controllers import simplepay

SETTINGS = "SimplePay Settings"
DIRECTORY = "SimplePay Employee"
LOCK_KEY = "simplepay-settings-catalogue-refresh"


def _str(value):
    return str(value).strip() if value is not None else ""


def _numeric(value):
    value = _str(value)
    if not value.isdigit():
        raise simplepay.SimplePayError(f"Expected numeric SimplePay identifier, got {value!r}")
    return value


def _sort_num(value):
    value = _str(value)
    return (0, int(value), value) if value.isdigit() else (1, value)


def _clients(payload):
    if not isinstance(payload, list) or not payload:
        raise simplepay.SimplePayError("Unexpected or empty SimplePay clients response")
    result = {}
    for wrapper in payload:
        raw = wrapper.get("client") if isinstance(wrapper, dict) else None
        if not isinstance(raw, dict):
            raise simplepay.SimplePayError("Invalid SimplePay client record")
        client_id = _numeric(raw.get("id"))
        if client_id in result:
            raise simplepay.SimplePayError(f"Duplicate SimplePay client ID {client_id}")
        result[client_id] = _str(raw.get("name") or client_id)[:140]
    return result


def _leave_types(payload, client_id):
    if not isinstance(payload, dict):
        raise simplepay.SimplePayError(f"Unexpected leave types response for client {client_id}")
    return {(client_id, _numeric(k)): _str(v or k)[:140] for k, v in payload.items()}


def _remuneration(payload, client_id):
    # Actual observed SimplePay response: {system_items: [], custom_items: [], accounts: []}
    if payload is None or payload == []:
        return {}
    sections = [payload] if isinstance(payload, dict) else payload
    if not isinstance(sections, list):
        raise simplepay.SimplePayError(f"Unexpected items_and_outputs response for client {client_id}")
    result = {}
    for section in sections:
        if not isinstance(section, dict):
            raise simplepay.SimplePayError(f"Invalid remuneration catalogue section for client {client_id}")
        for kind, key in (("System", "system_items"), ("Custom", "custom_items")):
            records = section.get(key)
            if records is None:
                records = []
            if not isinstance(records, list):
                raise simplepay.SimplePayError(f"Unexpected {key} for client {client_id}")
            for item in records:
                if not isinstance(item, dict):
                    raise simplepay.SimplePayError(f"Invalid {key} entry for client {client_id}")
                identifier = _str(item.get("name") if kind == "System" else item.get("id"))
                if not identifier:
                    raise simplepay.SimplePayError(f"Missing {key} identifier for client {client_id}")
                compound = (client_id, identifier)
                if compound in result:
                    raise simplepay.SimplePayError(f"Duplicate remuneration item {compound}")
                result[compound] = {
                    "label": _str(item.get("label") or identifier)[:140],
                    "type": _str(item.get("item_type") or kind)[:140],
                }
    return result


def _employees(payload, client_id):
    if not isinstance(payload, list):
        raise simplepay.SimplePayError(f"Unexpected employee listing for client {client_id}")
    result = {}
    for wrapper in payload:
        raw = wrapper.get("employee") if isinstance(wrapper, dict) else None
        if not isinstance(raw, dict):
            raise simplepay.SimplePayError(f"Invalid employee entry for client {client_id}")
        employee_id = _numeric(raw.get("id"))
        if employee_id in result:
            raise simplepay.SimplePayError(f"Duplicate employee {employee_id} for client {client_id}")
        result[employee_id] = {
            "record_key": f"SP-{employee_id}",
            "simplepay_client_id": client_id,
            "simplepay_employee_id": employee_id,
            "employee_number": _str(raw.get("number"))[:140],
            "first_name": _str(raw.get("first_name"))[:140],
            "last_name": _str(raw.get("last_name"))[:140],
        }
    return result


def _stage():
    clients = _clients(simplepay._request("GET", "clients/"))
    leave, remuneration, employees = {}, {}, {}
    for client_id in clients:
        leave.update(_leave_types(simplepay._request("GET", f"clients/{int(client_id)}/leave_types"), client_id))
        remuneration.update(_remuneration(simplepay._request("GET", f"clients/{int(client_id)}/items_and_outputs"), client_id))
        for employee_id, record in _employees(simplepay._request("GET", f"clients/{int(client_id)}/employees"), client_id).items():
            if employee_id in employees:
                raise simplepay.SimplePayError(f"SimplePay Employee ID {employee_id} appears in multiple clients")
            employees[employee_id] = record
    return clients, leave, remuneration, employees


def _unique_rows(rows, key_fn, table):
    indexed = {}
    for row in rows:
        key = key_fn(row)
        if not all(key):
            continue
        if key in indexed:
            raise simplepay.SimplePayError(f"Duplicate {table} external identity {key}")
        indexed[key] = row
    return indexed


def _set_if_changed(row, fieldname, value):
    if row.get(fieldname) != value:
        row.set(fieldname, value)
        return True
    return False


def _sort_rows(settings, table, key):
    rows = list(settings.get(table) or [])
    ordered = sorted(rows, key=key)
    changed = [r.name for r in rows] != [r.name for r in ordered]
    if changed:
        # Keep original child-document objects and names; never delete/recreate mappings.
        settings.set(table, ordered)
    for i, row in enumerate(ordered, 1):
        if row.idx != i:
            row.idx = i
            changed = True
    return changed


def _automap_employees(settings, records, client_company):
    """Exact SimplePay Payroll Number -> Frappe Employee.name, company-scoped.

    No name-based guesses, no leading-zero coercion, no changes to manual selections.
    Ambiguous source payroll numbers are left unmatched.
    """
    assigned = {row.employee for row in settings.employee_map or [] if row.employee}
    source_counts = {}
    for rec in records.values():
        company = client_company.get(rec["simplepay_client_id"])
        number = rec["employee_number"]
        if company and number:
            key = (company, number)
            source_counts[key] = source_counts.get(key, 0) + 1

    candidate_keys = set()
    for rec in records.values():
        company = client_company.get(rec["simplepay_client_id"])
        number = rec["employee_number"]
        if company and number and source_counts[(company, number)] == 1:
            candidate_keys.add((company, number))
    frappe_candidates = {}
    if candidate_keys:
        names = sorted({number for _, number in candidate_keys})
        for row in frappe.get_all("Employee", filters={"name": ["in", names], "status": "Active"}, fields=["name", "company"]):
            frappe_candidates.setdefault((row.company, row.name), []).append(row.name)

    matched = 0
    for row in settings.employee_map or []:
        if row.employee or not row.simplepay_employee:
            continue
        external_id = _str(row.simplepay_employee)
        if not external_id.startswith("SP-"):
            continue
        rec = records.get(external_id[3:])
        if not rec:
            continue
        company = client_company.get(rec["simplepay_client_id"])
        number = rec["employee_number"]
        if not company or not number or source_counts.get((company, number)) != 1:
            continue
        choices = frappe_candidates.get((company, number), [])
        if len(choices) != 1 or choices[0] in assigned:
            continue
        row.employee = choices[0]
        assigned.add(choices[0])
        matched += 1
    return matched


def _apply(clients, leave, remuneration, employees):
    settings = frappe.get_single(SETTINGS)
    changed = False
    stats = dict(clients_added=0, leave_types_added=0, calculations_added=0,
                 employees_created=0, employees_updated=0, employee_rows_added=0,
                 auto_mapped=0, unmapped=0, company_fields_corrected=0)
    companies = _unique_rows(settings.companies or [], lambda r: (_str(r.simplepay_client_id),), "company")
    types = _unique_rows(settings.leave_types or [], lambda r: (_str(r.get("simplepay_client_id")), _str(r.simplepay_type_id)), "leave")
    items = _unique_rows(settings.remuneration_mappings or [], lambda r: (_str(r.get("simplepay_client_id")), _str(r.simplepay_calculation)), "remuneration")
    employee_rows = _unique_rows(settings.employee_map or [], lambda r: (_str(r.simplepay_employee),), "employee")

    for cid, label in clients.items():
        row = companies.get((cid,))
        if not row:
            # IMPORTANT: pass explicit empty Link values *during* construction.
            # Omitting company allows Frappe's default Company to be injected.
            row = settings.append("companies", {
                "simplepay_client_id": cid,
                "simplepay_client_name": label,
                "company": "",
                "enabled": 0,
            })
            companies[(cid,)] = row
            changed = True
            stats["clients_added"] += 1
        changed |= _set_if_changed(row, "simplepay_client_name", label)

    # Company mapping is controlled by the user. An unmapped client stays blank.
    client_company = {cid: _str(row.company) for (cid,), row in companies.items()}

    for (cid, tid), label in leave.items():
        row = types.get((cid, tid))
        if not row:
            row = settings.append("leave_types", {
                "simplepay_client_id": cid,
                "simplepay_type_id": tid,
                "simplepay_type_name": label,
                "company": "",
                "leave_type": "",
                "enabled": 1,
            })
            types[(cid, tid)] = row
            changed = True
            stats["leave_types_added"] += 1
        changed |= _set_if_changed(row, "simplepay_type_name", label)
        if _set_if_changed(row, "company", client_company.get(cid, "")):
            stats["company_fields_corrected"] += 1
            changed = True

    for (cid, ident), item in remuneration.items():
        row = items.get((cid, ident))
        if not row:
            row = settings.append("remuneration_mappings", {
                "simplepay_client_id": cid,
                "simplepay_calculation": ident,
                "simplepay_item_label": item["label"],
                "simplepay_item_type": item["type"],
                "company": "",
                "salary_component": "",
                "enabled": 1,
            })
            items[(cid, ident)] = row
            changed = True
            stats["calculations_added"] += 1
        changed |= _set_if_changed(row, "simplepay_item_label", item["label"])
        changed |= _set_if_changed(row, "simplepay_item_type", item["type"])
        if _set_if_changed(row, "company", client_company.get(cid, "")):
            stats["company_fields_corrected"] += 1
            changed = True

    # Reject cross-client reassignment of SimplePay Employee ID before any directory changes.
    for employee_id, rec in employees.items():
        old = frappe.db.get_value(DIRECTORY, f"SP-{employee_id}", "simplepay_client_id")
        if old and _str(old) != rec["simplepay_client_id"]:
            raise simplepay.SimplePayError(f"SimplePay employee {employee_id} changed client; review manually")

    at = now_datetime()
    for employee_id, rec in employees.items():
        name = f"SP-{employee_id}"
        doc_values = dict(rec, company=client_company.get(rec["simplepay_client_id"], ""), is_active=1, last_seen=at)
        if frappe.db.exists(DIRECTORY, name):
            frappe.db.set_value(DIRECTORY, name, doc_values, update_modified=True)
            stats["employees_updated"] += 1
        else:
            frappe.get_doc({"doctype": DIRECTORY, **doc_values}).insert(ignore_permissions=True)
            stats["employees_created"] += 1
        row = employee_rows.get((name,))
        if not row:
            row = settings.append("employee_map", {
                "simplepay_employee": name,
                "employee": "",
                "employee_number": rec["employee_number"],
                "first_name": rec["first_name"],
                "last_name": rec["last_name"],
            })
            employee_rows[(name,)] = row
            changed = True
            stats["employee_rows_added"] += 1
        changed |= _set_if_changed(row, "employee_number", rec["employee_number"])
        changed |= _set_if_changed(row, "first_name", rec["first_name"])
        changed |= _set_if_changed(row, "last_name", rec["last_name"])

    for table in ("leave_types", "remuneration_mappings"):
        for row in settings.get(table) or []:
            cid = _str(row.get("simplepay_client_id"))
            if _set_if_changed(row, "company", client_company.get(cid, "")):
                changed = True
                stats["company_fields_corrected"] += 1

    matching_companies = {cid: _str(row.company) if row.enabled else ""
                          for (cid,), row in companies.items()}
    stats["auto_mapped"] = _automap_employees(settings, employees, matching_companies)
    changed |= bool(stats["auto_mapped"])
    stats["unmapped"] = sum(not bool(row.employee) for row in settings.employee_map or [])

    changed |= _sort_rows(settings, "companies", lambda r: _sort_num(r.simplepay_client_id))
    changed |= _sort_rows(settings, "leave_types", lambda r: (_sort_num(r.get("simplepay_client_id")), _sort_num(r.simplepay_type_id)))
    changed |= _sort_rows(settings, "remuneration_mappings", lambda r: (_sort_num(r.get("simplepay_client_id")), _str(r.simplepay_calculation)))
    changed |= _sort_rows(settings, "employee_map", lambda r: (_sort_num(r.employee_number), _sort_num(_str(r.simplepay_employee).removeprefix("SP-"))))
    if changed:
        settings.save(ignore_permissions=True)
    stats.update(client_rows=len(settings.companies or []), leave_rows=len(settings.leave_types or []),
                 remuneration_rows=len(settings.remuneration_mappings or []),
                 employee_rows=len(settings.employee_map or []), finished_at=str(at))
    return stats


def sync_all_mappings():
    """Sole daily SimplePay mapping job, also used by manual refresh."""
    if not frappe.get_single(SETTINGS).enabled:
        return {"skipped": "SimplePay disabled"}
    with frappe.cache().lock(LOCK_KEY, timeout=1800, blocking_timeout=1):
        return _apply(*_stage())


@frappe.whitelist()
def refresh_all_mappings():
    if not ({"System Manager", "HR Manager"} & set(frappe.get_roles())):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    if not frappe.get_single(SETTINGS).enabled:
        frappe.throw(_("Enable SimplePay first"))
    frappe.enqueue("is_attendance.controllers.simplepay_mapping.sync_all_mappings",
                   queue="long", enqueue_after_commit=True,
                   job_name="simplepay-full-mapping-refresh")
    return {"queued": True}


@frappe.whitelist()
def list_clients():
    if not ({"System Manager", "HR Manager"} & set(frappe.get_roles())):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    return [{"id": client_id, "name": name}
            for client_id, name in _clients(simplepay._request("GET", "clients/")).items()]
