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


# SimplePay output-line ("account") types that carry money onto a payslip, and the
# HRMS Salary Component type each must map to. Every other account type (totals,
# intermediate, leave, quantity, rate, summary, liability, taxable_income_deduction,
# tax_credit, tax_exemption, other, employer_income) is informational and never imported.
MAPPABLE_LINE_TYPES = {
    "income": "Earning",
    "allowance": "Earning",
    "benefit": "Earning",
    "deduction": "Deduction",
    "employer_contribution": "Company Contribution",
}
# Payslip group keys that are SimplePay totals, never mapped.
TOTAL_CODES = {"total", "nett_pay"}


def account_code(account):
    """Payslip code for an output line: system/derived `name`, or custom numeric `id`."""
    return _str(account.get("id") if account.get("type") == "custom_item" else account.get("name"))


def _remuneration(payload, client_id):
    """Output lines (items_and_outputs.accounts) keyed by (client, payslip code).

    Output lines, not pay items, are what appear on payslips: statutory lines (tax,
    uif_self, uif_employer, sdl_employer) are derived outputs with no item, and one
    item can yield several lines (e.g. provident_fund -> employee + employer).
    """
    if not isinstance(payload, dict):
        raise simplepay.SimplePayError(f"Unexpected items_and_outputs response for client {client_id}")
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise simplepay.SimplePayError(f"Empty or invalid output catalogue for client {client_id}; no changes applied")
    result = {}
    for account in accounts:
        if not isinstance(account, dict):
            raise simplepay.SimplePayError(f"Invalid output line entry for client {client_id}")
        line_type = _str(account.get("account_type"))
        if line_type not in MAPPABLE_LINE_TYPES:
            continue
        source = _str(account.get("type"))
        code = account_code(account)
        if source not in ("system_item", "custom_item", "derived") or not code:
            raise simplepay.SimplePayError(f"Unrecognised output line {account!r} for client {client_id}")
        compound = (client_id, code)
        if compound in result:
            raise simplepay.SimplePayError(f"Duplicate output line {compound}")
        result[compound] = {
            "label": _str(account.get("label") or code)[:140],
            "type": line_type,
            "source": source,
            "item": _str(account.get(source) if source != "derived" else code)[:140] or code,
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


_LINE_TYPE_ORDER = {t: i for i, t in enumerate(("income", "allowance", "benefit", "deduction", "employer_contribution"))}


def _remuneration_sort_key(row):
    """Lines seen on payslips first, then client, line type, label."""
    return (0 if row.get("in_use") else 1, _sort_num(row.get("simplepay_client_id")),
            _LINE_TYPE_ORDER.get(_str(row.simplepay_item_type), 99), _str(row.simplepay_item_label).lower(),
            _str(row.get("simplepay_account")))


def _apply(clients, leave, remuneration, employees):
    settings = frappe.get_single(SETTINGS)
    changed = False
    stats = dict(clients_added=0, leave_types_added=0, calculations_added=0, legacy_rows_removed=0,
                 employees_created=0, employees_updated=0, employee_rows_added=0,
                 auto_mapped=0, unmapped=0, company_fields_corrected=0)
    companies = _unique_rows(settings.companies or [], lambda r: (_str(r.simplepay_client_id),), "company")
    types = _unique_rows(settings.leave_types or [], lambda r: (_str(r.get("simplepay_client_id")), _str(r.simplepay_type_id)), "leave")
    # Retire legacy item-keyed rows (pre output-line catalogue) only when nobody mapped them.
    legacy = [r for r in settings.remuneration_mappings or [] if not _str(r.get("simplepay_account"))]
    if any(r.salary_component for r in legacy):
        raise simplepay.SimplePayError("Legacy item-keyed remuneration rows carry Salary Components; "
                                       "re-map them to output lines manually before refreshing")
    if legacy:
        legacy_ids = {id(r) for r in legacy}
        settings.remuneration_mappings = [r for r in settings.remuneration_mappings if id(r) not in legacy_ids]
        stats["legacy_rows_removed"] = len(legacy)
        changed = True
    items = _unique_rows(settings.remuneration_mappings or [], lambda r: (_str(r.get("simplepay_client_id")), _str(r.simplepay_account)), "remuneration")
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

    for (cid, code), item in remuneration.items():
        row = items.get((cid, code))
        if not row:
            row = settings.append("remuneration_mappings", {
                "simplepay_client_id": cid,
                "simplepay_account": code,
                "simplepay_calculation": item["item"],
                "simplepay_source": item["source"],
                "simplepay_item_label": item["label"],
                "simplepay_item_type": item["type"],
                "mapping_status": "Unmapped",
                "company": "",
                "salary_component": "",
                "enabled": 1,
            })
            items[(cid, code)] = row
            changed = True
            stats["calculations_added"] += 1
        changed |= _set_if_changed(row, "simplepay_calculation", item["item"])
        changed |= _set_if_changed(row, "simplepay_source", item["source"])
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
    changed |= _sort_rows(settings, "remuneration_mappings", _remuneration_sort_key)
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


# ---------------------------------------------------------------------------
# Payslip usage scan (read-only against SimplePay; writes only Settings rows)
# ---------------------------------------------------------------------------

USAGE_JOB = "simplepay-payslip-usage-scan"


def payslip_lines(detail):
    """Yield (code, amount) for every non-total line of a GET /payslips/:id response.

    Shape: {"income": [[code, amt], ...], "deduction": [...], "grand_total": [...],
            "sideline": [[group, [[code, amt], ...]], ...]}; custom codes are ints.
    """
    if not isinstance(detail, dict):
        raise simplepay.SimplePayError("Unexpected payslip response")

    def pairs(rows):
        for pair in rows or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2 and not isinstance(pair[1], list):
                yield pair

    for section in ("income", "deduction", "grand_total"):
        yield from ((_str(c), a) for c, a in pairs(detail.get(section)) if _str(c) not in TOTAL_CODES)
    for group in detail.get("sideline") or []:
        if isinstance(group, (list, tuple)) and len(group) == 2 and isinstance(group[1], list):
            yield from ((_str(c), a) for c, a in pairs(group[1]) if _str(c) not in TOTAL_CODES)


def _nonzero(amount):
    try:
        return abs(float(amount or 0)) > 0.0001
    except (TypeError, ValueError):
        return False


def _stage_usage(recent):
    from frappe.utils import getdate
    settings = frappe.get_single(SETTINGS)
    clients = {_str(r.simplepay_client_id) for r in settings.companies or [] if r.enabled and r.company}
    if not clients:
        raise simplepay.SimplePayError("No enabled, Company-mapped SimplePay clients")
    catalogue = {}
    for cid in sorted(clients):
        payload = simplepay._request("GET", f"clients/{int(cid)}/items_and_outputs")
        catalogue[cid] = {account_code(a): (_str(a.get("account_type")), _str(a.get("label")))
                          for a in (payload or {}).get("accounts") or [] if isinstance(a, dict)}
    employees = frappe.get_all(DIRECTORY, filters={"is_active": 1, "simplepay_client_id": ["in", sorted(clients)]},
                               fields=["name", "simplepay_employee_id", "simplepay_client_id"])
    today = getdate()
    hits, last_seen, unknown, informational = {}, {}, {}, {}
    slips_read = 0
    for emp in employees:
        cid = _str(emp.simplepay_client_id)
        listing = simplepay._request("GET", f"employees/{int(_numeric(emp.simplepay_employee_id))}/payslips")
        if not isinstance(listing, list):
            raise simplepay.SimplePayError(f"Unexpected payslip listing for {emp.name}")
        rows = [x.get("payslip", x) for x in listing if isinstance(x, dict)]
        rows = sorted((r for r in rows if r.get("finalised") and r.get("date") and getdate(r["date"]) <= today),
                      key=lambda r: r["date"], reverse=True)[:recent]
        for slip in rows:
            detail = simplepay._request("GET", f"payslips/{int(_numeric(slip['id']))}")
            slips_read += 1
            for code, amount in payslip_lines(detail):
                if not _nonzero(amount):
                    continue
                line_type, label = catalogue[cid].get(code, (None, None))
                if line_type is None:
                    unknown.setdefault((cid, code), slip["id"])
                elif line_type not in MAPPABLE_LINE_TYPES:
                    informational[(cid, code)] = f"{label} [{line_type}]"
                else:
                    key = (cid, code)
                    hits[key] = hits.get(key, 0) + 1
                    last_seen[key] = max(last_seen.get(key, slip["date"]), slip["date"])
    return dict(employees=len(employees), slips_read=slips_read, hits=hits, last_seen=last_seen,
                unknown=unknown, informational=informational)


def _apply_usage(staged):
    settings = frappe.get_single(SETTINGS)
    rows = {(_str(r.get("simplepay_client_id")), _str(r.get("simplepay_account"))): r
            for r in settings.remuneration_mappings or []}
    missing_rows = sorted(k for k in staged["hits"] if k not in rows)
    for key, row in rows.items():
        count = staged["hits"].get(key, 0)
        row.in_use = 1 if count else 0
        row.payslip_hits = count
        if count:
            row.last_seen_on_payslip = staged["last_seen"][key]
    _sort_rows(settings, "remuneration_mappings", _remuneration_sort_key)
    settings.save(ignore_permissions=True)
    return {"employees_scanned": staged["employees"], "payslips_read": staged["slips_read"],
            "lines_in_use": sum(1 for r in rows.values() if r.in_use),
            "in_use_unmapped": sum(1 for r in rows.values() if r.in_use and r.mapping_status == "Unmapped"),
            "in_use_without_catalogue_row": [f"{c}:{k}" for c, k in missing_rows],
            "unknown_codes": [f"{c}:{k} (payslip {p})" for (c, k), p in sorted(staged["unknown"].items())],
            "informational_lines_seen": sorted({v for v in staged["informational"].values()})}


def scan_payslip_usage(recent=3):
    """Mark which output lines appear (non-zero) on each active employee's last `recent` finalised payslips."""
    if not frappe.get_single(SETTINGS).enabled:
        return {"skipped": "SimplePay disabled"}
    recent = max(1, min(24, int(recent or 3)))
    with frappe.cache().lock(LOCK_KEY, timeout=3600, blocking_timeout=1):
        result = _apply_usage(_stage_usage(recent))
    frappe.db.commit()
    return result


@frappe.whitelist()
def queue_payslip_usage_scan(recent=3):
    simplepay._require_payroll_access()
    if not frappe.get_single(SETTINGS).enabled:
        frappe.throw(_("Enable SimplePay first"))
    frappe.enqueue("is_attendance.controllers.simplepay_mapping.scan_payslip_usage", queue="long",
                   timeout=3600, enqueue_after_commit=True, job_name=USAGE_JOB, recent=recent)
    return {"queued": True}


# ---------------------------------------------------------------------------
# Salary Component picker for the Remuneration Map grid
# ---------------------------------------------------------------------------

def component_type_for_line(line_type):
    """The single HRMS Salary Component type a SimplePay line type may map to (None if unmappable)."""
    return MAPPABLE_LINE_TYPES.get(_str(line_type))


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def salary_component_query(doctype, txt, searchfield, start, page_len, filters):
    """Link search: only Salary Components whose type fits the row's SimplePay line type.

    Benefit lines are non-cash fringe benefits, so they are further limited to components
    marked Do Not Include in Total or Statistical (the same rule Settings.validate enforces).
    """
    filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
    line_type = _str(filters.get("line_type"))
    component_type = component_type_for_line(line_type)
    if not component_type:
        return []
    conditions = [["type", "=", component_type], ["disabled", "=", 0]]
    or_conditions = None
    if line_type == "benefit":
        or_conditions = [["do_not_include_in_total", "=", 1], ["statistical_component", "=", 1]]
    if txt:
        text = f"%{txt}%"
        # Combine the text match with the benefit OR-group without losing either.
        names = frappe.get_list("Salary Component", filters=conditions, or_filters=or_conditions, pluck="name",
                                limit_page_length=0)
        return frappe.get_list("Salary Component", filters=[["name", "in", names or [""]]],
                               or_filters=[["name", "like", text], ["salary_component_abbr", "like", text]],
                               fields=["name", "type", "salary_component_abbr"], order_by="name asc",
                               limit_start=start, limit_page_length=page_len, as_list=True)
    return frappe.get_list("Salary Component", filters=conditions, or_filters=or_conditions,
                           fields=["name", "type", "salary_component_abbr"], order_by="name asc",
                           limit_start=start, limit_page_length=page_len, as_list=True)
