# Copyright (c) 2026, BuFf0k and contributors
"""Make Salary Components mirror SimplePay, then map SimplePay lines to them 1:1.

Driven by patches/data/simplepay_salary_components.json (a snapshot of SimplePay; no API
calls are made during migrate):

1. Every old component listed under a target's "replaces" is renamed to the target (or
   merged into it when the target already exists). Frappe's rename re-points every Link
   field (Contract of Employment rows, Contract Defaults, Status Change Detail, IR Payroll
   Cost Settings, Overtime Type, Salary Structures/Slips, Payroll Settings, ...).
2. Each target is created if missing, and its SARS / PAYE / UIF / SDL handling is set
   from SimplePay. Formulas, payment-days settings and GL accounts are left alone.
   Where SimplePay names a line differently per client ("company_splits"), contract rows
   of the other company are re-pointed to that company's component.
3. Components that are neither a target nor in "keep" are deleted (skipped, with an
   Error Log, if anything still links to them).
4. Payroll Settings statutory component fields are pointed at the SimplePay components.
5. SimplePay Settings remuneration rows are mapped to the component named exactly like
   the SimplePay line (so Med/ Pla -> Isambane, M/Plan -> Excavo, by label).
"""

import json

import frappe
from frappe.model.rename_doc import rename_doc

DATA_FILE = ("is_attendance", "patches", "data", "simplepay_salary_components.json")
COMPONENT = "Salary Component"

TREATMENT_BY_CODE = {
	"3601": "Regular Remuneration",
	"3605": "Annual Payment",
	"3606": "Commission",
	"3607": "Overtime",
	"3701": "Fixed Travel Allowance",
	"3713": "Regular Remuneration",
	"3802": "Regular Remuneration",
	"3805": "Regular Remuneration",
	"3806": "Regular Remuneration",
	"3825": "Regular Remuneration",
	"3901": "Severance Benefit",
	"4102": "PAYE",
	"4141": "UIF",
	"4142": "SDL",
	"4003": "Retirement Fund",
	"4473": "Retirement Fund",
}


def load_data():
	with open(frappe.get_app_path(*DATA_FILE)) as handle:
		return json.load(handle)


def execute():
	if not frappe.db.exists("DocType", COMPONENT):
		return
	data = load_data()
	_ensure_sars_codes(data.get("sars_codes") or [])
	for spec in data["components"]:
		_consolidate(spec)
		_upsert(spec)
	_split_by_company(data.get("company_splits") or [])
	_delete_obsolete({spec["name"] for spec in data["components"]} | set(data.get("keep") or []))
	_payroll_settings(data.get("payroll_settings") or {})
	_map_simplepay_lines()


# (child doctype, parent doctype) pairs whose parent carries a company field.
COMPANY_SCOPED_ROWS = (
	("Contract of Employment Earning", "Contract of Employment"),
	("Contract of Employment Deduction", "Contract of Employment"),
)


def _split_by_company(splits):
	"""Where SimplePay names the same line differently per client (Med/ Pla at Isambane,
	M/Plan at Excavo), re-point rows of contracts at another company to that company's line."""
	for split in splits:
		source = split["component"]
		for company, target in split["by_company"].items():
			if not (_exists(source) and _exists(target)):
				continue
			for child, parent in COMPANY_SCOPED_ROWS:
				frappe.db.sql(f"""
					update `tab{child}` row join `tab{parent}` doc on doc.name = row.parent
					set row.salary_component = %s
					where row.parenttype = %s and row.salary_component = %s and doc.company = %s""",
					(target, parent, source, company))


def _exists(name):
	"""Exact-case existence (MariaDB name comparison is case-insensitive)."""
	found = frappe.db.get_value(COMPONENT, name, "name")
	return found if found == name else None


def _ensure_sars_codes(rows):
	if not frappe.db.exists("DocType", "SARS Payroll Code"):
		return
	for row in rows:
		if not frappe.db.exists("SARS Payroll Code", row["code"]):
			frappe.get_doc({"doctype": "SARS Payroll Code", "active": 1, **row}).insert(ignore_permissions=True)


def _copy_missing_accounts(source, target):
	"""A merge deletes the source with its child rows; keep GL accounts the target lacks."""
	have = {a.company for a in frappe.get_all("Salary Component Account", filters={
		"parent": target, "parenttype": COMPONENT}, fields=["company"])}
	missing = [a for a in frappe.get_all("Salary Component Account", filters={
		"parent": source, "parenttype": COMPONENT}, fields=["company", "account"]) if a.company not in have]
	if missing:
		doc = frappe.get_doc(COMPONENT, target)
		for a in missing:
			doc.append("accounts", {"company": a.company, "account": a.account})
			have.add(a.company)
		doc.flags.ignore_validate = True
		doc.save(ignore_permissions=True)


def _consolidate(spec):
	target = spec["name"]
	for old in spec.get("replaces") or []:
		if old == target or not _exists(old):
			continue
		if _exists(target):
			_copy_missing_accounts(old, target)
			rename_doc(COMPONENT, old, target, merge=True, force=True,
			                  ignore_permissions=True, show_alert=False, rebuild_search=False)
		else:
			rename_doc(COMPONENT, old, target, force=True,
			                  ignore_permissions=True, show_alert=False, rebuild_search=False)


def _handling(spec, meta):
	"""Tax handling derived from SimplePay's line type, flags and SARS code."""
	code = spec.get("sars_code")
	earning = spec["type"] == "Earning"
	benefit = spec["line"] == "benefit"
	taxable = int(bool(spec.get("taxable"))) if earning else 0
	values = {
		"type": spec["type"],
		"za_sars_payroll_code": code or None,
		"za_payroll_treatment": spec.get("treatment") or TREATMENT_BY_CODE.get(code or "", ""),
		"za_uif_applicable": int(bool(spec.get("uif"))) if earning else 0,
		"za_sdl_applicable": int(bool(spec.get("sdl"))) if earning else 0,
		"za_coida_applicable": int(bool(spec.get("uif"))) if earning and not benefit else 0,
		"za_paye_inclusion_percentage": (spec.get("paye_pct") or 100) if taxable else 0,
		"za_variable_pay_treatment": spec.get("variable_pay") or (
			"Once-Off Full Tax" if spec.get("annual") else "Recurring Annualised"),
		"za_is_annual_bonus": 0,
		"za_exclude_from_irp5": 0,
	}
	values["is_tax_applicable"] = taxable
	if benefit:
		# Non-cash fringe benefit: taxed and reported, never paid or posted.
		values.update(do_not_include_in_total=1, do_not_include_in_accounts=1, depends_on_payment_days=0)
	return {k: v for k, v in values.items() if meta.has_field(k)}


def _upsert(spec):
	meta = frappe.get_meta(COMPONENT)
	values = _handling(spec, meta)
	name = spec["name"]
	if _exists(name):
		frappe.db.set_value(COMPONENT, name, values)
		return
	doc = frappe.get_doc({
		"doctype": COMPONENT,
		"salary_component": name,
		"salary_component_abbr": spec.get("abbr"),
		"description": f"SimplePay {spec['line'].replace('_', ' ')} line",
		"depends_on_payment_days": 0,
		"remove_if_zero_valued": 1,
		**values,
	})
	doc.insert(ignore_permissions=True)


def _delete_obsolete(wanted):
	for name in frappe.get_all(COMPONENT, pluck="name"):
		if name in wanted:
			continue
		try:
			frappe.delete_doc(COMPONENT, name, ignore_permissions=True)
		except frappe.LinkExistsError:
			frappe.clear_last_message()
			frappe.log_error(title=f"SimplePay component alignment: kept linked component {name}",
			                 message=frappe.get_traceback())
			print(f"  kept {name}: still linked")


def _payroll_settings(fields):
	meta = frappe.get_meta("Payroll Settings")
	for field, component in fields.items():
		if meta.has_field(field) and _exists(component):
			frappe.db.set_single_value("Payroll Settings", field, component)


def _map_simplepay_lines():
	if not frappe.db.exists("DocType", "SimplePay Settings"):
		return
	from is_attendance.controllers.simplepay_mapping import automap_components

	settings = frappe.get_single("SimplePay Settings")
	if automap_components(settings):
		settings.save(ignore_permissions=True)
