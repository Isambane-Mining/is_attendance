# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Shared plumbing behind Sage Payroll Run - receiving the employee list
sage_employee_puller.py (Windows host, next to Sage VIP Premier's ODBC
DSNs) pulls straight from Sage, computing each matched employee's Normal /
Overtime 1.5 / Leave hours for the period from our own Employee Checkin /
Leave Application data, and generating the fixed-width .txt Sage's own
Batch Import function expects.

Pull direction is heartbeat-driven, not on-demand: sage_employee_puller.py
runs continuously on the Windows host and polls list_pending_pull_requests()
every cycle rather than being invoked with a specific Run to fetch. A
person requests a pull by clicking "Request Employee Pull" on a Sage
Payroll Run (SagePayrollRun.request_employee_pull, sets Status to "Pull
Requested"); the heartbeat's next poll finds it, does the real ODBC work,
and posts the result to ingest_employees() - same as before, just no
longer needing a person to go run a script by hand with the right
arguments. See is_attendance/payroll_controllers/README_CON.md.

The .txt format and the live Sage query it's paired with were both
reverse-engineered this session from real files - see
is_attendance/payroll_controllers/README_CON.md and the plan this feature
was built from for the full evidence trail. Two things are deliberately
left as flagged placeholders rather than guessed:

- Overtime 2 is always 0 - no trigger rule for it exists anywhere in the
  source VBA (BatchImport_Premier_PV3.xls) or the process documentation.
- A full leave day is credited DEFAULT_LEAVE_DAY_HOURS (12, the most common
  shift length in the one real sample sheet available) - the real sheet's
  own "A/LEAVE 12Hrs OR 10Hrs" / "9Hrs OR 8Hrs" columns suggest this
  actually varies by site/shift, but no rule for *which* applies when was
  documented anywhere available this session.
"""

from __future__ import annotations

from datetime import time as dt_time

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, now_datetime

from is_attendance.controllers.clocking_import import resolve_employee
from is_attendance.isambane_attendance.report.attendance_compliance_summary.attendance_compliance_summary import (
	_classify_day,
	_get_checkins_grouped,
	_get_leave_detail_by_day,
)

PAYROLL_ROLES = {"System Manager", "Payroll Manager", "Payroll User"}

DEFAULT_START_TIME = dt_time(6, 0, 0)
DEFAULT_END_TIME = dt_time(16, 0, 0)
DEFAULT_LEAVE_DAY_HOURS = 12.0

SETTINGS_DOCTYPE = "IS Attendance Settings"

AMT_SOURCE_FIELD_MAP = {
	"Normal Hours": "computed_normal_hours",
	"Overtime 1.5": "computed_ot_1_5",
	"Overtime 2": "computed_ot_2",
	"Leave Hours - Annual": "leave_hours_annual",
	"Leave Hours - Sick": "leave_hours_sick",
	"Leave Hours - Family": "leave_hours_family",
}


def _require_payroll_role() -> None:
	if not PAYROLL_ROLES & set(frappe.get_roles()):
		frappe.throw(_("You are not permitted to work with Sage Payroll data."), frappe.PermissionError)


# ---------------------------------------------------------------------------
# 1. Ingest - called by sage_employee_puller.py via run_doc_method
# ---------------------------------------------------------------------------

def ingest_employees(run_doc, records) -> dict:
	"""Upsert one Sage Payroll Employee row per pulled record. Idempotent on
	(sage_payroll_run, employee_code) - re-running the puller script simply
	refreshes the same rows, so it's safe to call again to pick up a Sage-
	side correction without creating duplicates."""
	_require_payroll_role()

	if run_doc.docstatus != 0:
		frappe.throw(_("This Sage Payroll Run has already been submitted."))

	if isinstance(records, str):
		records = frappe.parse_json(records)
	if not records:
		frappe.throw(_("No records were sent to ingest."))

	company = frappe.get_doc("Sage Payroll Company", run_doc.sage_payroll_company)
	paypoint_branch = {row.paypoint_code: row.branch for row in company.paypoints}

	created = updated = unmatched = 0

	for record in records:
		employee_code = (record.get("employee_code") or "").strip()
		if not employee_code:
			continue

		paypoint_code = (record.get("paypoint_code") or "").strip()
		branch = paypoint_branch.get(paypoint_code)

		employee = resolve_employee({"employee_code": employee_code})
		if employee:
			employee_status = frappe.db.get_value("Employee", employee, "status")
			match_status = "Matched" if employee_status == "Active" else "Excluded"
		else:
			match_status = "Unmatched"
			unmatched += 1

		values = {
			"sage_payroll_run": run_doc.name,
			"employee_code": employee_code,
			"surname": record.get("surname"),
			"full_names": record.get("full_names"),
			"id_number": record.get("id_number"),
			"occupation": record.get("occupation"),
			"paypoint_code": paypoint_code,
			"branch": branch,
			"employee": employee,
			"match_status": match_status,
		}

		existing_name = frappe.db.get_value(
			"Sage Payroll Employee",
			{"sage_payroll_run": run_doc.name, "employee_code": employee_code},
			"name",
		)
		if existing_name:
			frappe.get_doc("Sage Payroll Employee", existing_name).update(values).save(
				ignore_permissions=True
			)
			updated += 1
		else:
			frappe.get_doc({"doctype": "Sage Payroll Employee", **values}).insert(
				ignore_permissions=True
			)
			created += 1

	total = frappe.db.count("Sage Payroll Employee", {"sage_payroll_run": run_doc.name})
	frappe.db.set_value("Sage Payroll Run", run_doc.name, "employee_count", total)
	if run_doc.status in ("Draft", "Pull Requested") and total:
		frappe.db.set_value("Sage Payroll Run", run_doc.name, "status", "Employees Loaded")

	log_line = (
		f"{now_datetime()}: ingested {len(records)} record(s) from Sage - "
		f"{created} created, {updated} updated, {unmatched} unmatched."
	)
	frappe.db.set_value(
		"Sage Payroll Run",
		run_doc.name,
		"ingest_log",
		((run_doc.ingest_log or "") + "\n" + log_line).strip(),
	)

	_stamp_controller_sync(run_doc.name, log_line)

	return {"created": created, "updated": updated, "unmatched": unmatched, "total": total}


def _stamp_controller_sync(run_name: str, summary: str) -> None:
	"""Records this successful ingest against whichever Sage Remote
	Controller posted it (identified by frappe.session.user, same as
	get_remote_controller_config()) - purely for the last-sync visibility
	on that doctype's own list view. Silently does nothing if the caller
	isn't a registered controller (e.g. a person triggering this by hand
	from the desk) - that's a normal case, not an error."""
	controller_name = frappe.db.get_value("Sage Remote Controller", {"api_user": frappe.session.user}, "name")
	if not controller_name:
		return

	frappe.db.set_value(
		"Sage Remote Controller",
		controller_name,
		{"last_sync_at": now_datetime(), "last_sync_summary": f"{run_name}: {summary}"},
	)


@frappe.whitelist()
def get_remote_controller_config() -> dict:
	"""Polled by sage_employee_puller.py once per heartbeat cycle, before
	fetch_pending_requests() - replaces the old local `companies` block in
	sage_employee_puller.json (dsn + Sage Company Number mappings used to
	live only on the Windows host, duplicated by hand in a gitignored file
	there; now they're a Sage Remote Controller record here instead, one
	per deployed controller).

	Identifies the calling controller by frappe.session.user (the User its
	api_key/api_secret belong to) - no separate controller ID needs to be
	configured on the host itself. Also stamps last_heartbeat, exactly
	like the old heartbeat-liveness signal list_pending_pull_requests()
	already provided, just now recorded somewhere a person can see it (the
	Sage Remote Controller list view) instead of only in that host's own
	local log file."""
	_require_payroll_role()

	controller_name = frappe.db.get_value(
		"Sage Remote Controller", {"api_user": frappe.session.user, "enabled": 1}, "name"
	)
	if not controller_name:
		frappe.throw(
			_(
				"No enabled Sage Remote Controller is registered for User {0}. "
				"Create one (or enable the existing one) before this heartbeat can be configured."
			).format(frappe.session.user)
		)

	frappe.db.set_value("Sage Remote Controller", controller_name, "last_heartbeat", now_datetime())
	frappe.db.commit()

	controller = frappe.get_cached_doc("Sage Remote Controller", controller_name)
	return {
		"companies": [
			{
				"sage_company_no": row.sage_payroll_company,
				"dsn": row.dsn,
				"enabled": bool(row.enabled),
			}
			for row in controller.companies
		]
	}


@frappe.whitelist()
def list_pending_pull_requests() -> list[dict]:
	"""Polled by sage_employee_puller.py's heartbeat (on the Windows host,
	next to Sage's ODBC DSNs) - a plain module-level endpoint, not a doc
	method, since the heartbeat doesn't know which Runs exist until it
	asks. Returns one entry per Sage Payroll Run currently sitting at
	Status "Pull Requested" (set by SagePayrollRun.request_employee_pull,
	the "Request Employee Pull" button), naming the Sage Company Number to
	pull (a single query per Company, not per Paypoint - see
	sage_employee_puller.py) and its currently-configured Paypoints, sent
	along for the heartbeat's own logging/sanity-checking only, since the
	real Branch split from each pulled row's own PaypointCode happens
	server-side in ingest_employees(), not on the Windows host."""
	_require_payroll_role()

	rows = frappe.get_all(
		"Sage Payroll Run",
		filters={"status": "Pull Requested", "docstatus": 0},
		fields=["name", "sage_payroll_company"],
	)

	pending = []
	for row in rows:
		company = frappe.get_cached_doc("Sage Payroll Company", row.sage_payroll_company)
		pending.append(
			{
				"run": row.name,
				"sage_company_no": company.sage_company_no,
				"paypoints": [paypoint.paypoint_code for paypoint in company.paypoints],
			}
		)

	return pending


# ---------------------------------------------------------------------------
# 2. Compute - Normal/OT1.5/Leave hours from our own clocking + leave data
# ---------------------------------------------------------------------------

def compute_hours_and_leave(run_doc) -> None:
	_require_payroll_role()

	rows = frappe.get_all(
		"Sage Payroll Employee",
		filters={"sage_payroll_run": run_doc.name, "match_status": "Matched"},
		fields=["name", "employee"],
	)
	if not rows:
		frappe.throw(_("No Matched employees on this Run to compute hours for - ingest employees first."))

	from_date = getdate(run_doc.from_date)
	to_date = getdate(run_doc.to_date)
	employees = [row.employee for row in rows]

	checkins_by_day = _get_checkins_grouped(employees, from_date, to_date)
	leave_detail = _get_leave_detail_by_day(employees, from_date, to_date)
	leave_code_map = _leave_type_to_sage_code_map()
	normal_hours_cap = flt(run_doc.normal_hours_cap) or 195.0

	leave_field_by_code = {
		"A": "leave_hours_annual",
		"S": "leave_hours_sick",
		"F": "leave_hours_family",
	}

	for row in rows:
		total_hours = 0.0
		leave_hours = {"A": 0.0, "S": 0.0, "F": 0.0}

		current = from_date
		while current <= to_date:
			day_checkins = checkins_by_day.get((row.employee, current), [])
			classification = _classify_day(day_checkins, DEFAULT_START_TIME, DEFAULT_END_TIME, 0)
			total_hours += classification["hours_worked"]

			leave_here = leave_detail.get((row.employee, current))
			if leave_here:
				code = leave_code_map.get(leave_here["leave_type"])
				if code:
					day_hours = DEFAULT_LEAVE_DAY_HOURS / 2 if leave_here["half_day"] else DEFAULT_LEAVE_DAY_HOURS
					leave_hours[code] += day_hours

			current = add_days(current, 1)

		normal_hours = min(total_hours, normal_hours_cap)
		ot_1_5 = max(0.0, total_hours - normal_hours_cap)

		frappe.db.set_value(
			"Sage Payroll Employee",
			row.name,
			{
				"computed_normal_hours": round(normal_hours, 2),
				"computed_ot_1_5": round(ot_1_5, 2),
				"computed_ot_2": 0.0,
				leave_field_by_code["A"]: round(leave_hours["A"], 2),
				leave_field_by_code["S"]: round(leave_hours["S"], 2),
				leave_field_by_code["F"]: round(leave_hours["F"], 2),
			},
		)

	frappe.db.set_value("Sage Payroll Run", run_doc.name, "status", "Computed")


def _leave_type_to_sage_code_map() -> dict[str, str]:
	rows = frappe.get_all(
		"IS Attendance Sage Leave Mapping",
		filters={"parenttype": SETTINGS_DOCTYPE, "parent": SETTINGS_DOCTYPE},
		fields=["leave_type", "sage_leave_code"],
	)
	return {row.leave_type: row.sage_leave_code for row in rows}


# ---------------------------------------------------------------------------
# 3. Export - the fixed-width .txt, reverse-engineered from
#    BatchImport_Premier_PV3.xls's VBA (Module1.bas)
# ---------------------------------------------------------------------------

def _format_amt(value) -> tuple[str, int]:
	"""11-digit zero-padded absolute cents + trailing sign, 12 chars total -
	Format(x * 100, "00000000000+;00000000000-;00000000000+") in the
	original VBA. Returns (formatted, cents) - cents is accumulated by the
	caller for the totals line."""
	cents = int(round(flt(value) * 100))
	sign = "-" if cents < 0 else "+"
	return f"{abs(cents):011d}{sign}", cents


def build_detail_line(company, employee_row: dict) -> tuple[str, list[int]]:
	ind = "D"
	co_no = (company.sage_company_no or "").zfill(3)[:3]
	bat_no = (company.batch_no or " ")[:1]
	emp_code = (employee_row.get("employee_code") or "")[:8].ljust(8)

	amt_parts: list[str] = []
	amt_cents: list[int] = []
	for slot in range(1, 7):
		source = company.get(f"amt_{slot}_source")
		field = AMT_SOURCE_FIELD_MAP.get(source)
		if not field:
			amt_parts.append(" " * 12)
			amt_cents.append(0)
			continue
		formatted, cents = _format_amt(employee_row.get(field))
		amt_parts.append(formatted)
		amt_cents.append(cents)

	filler = " " * 13
	end_ind = "Z"
	line = ind + co_no + bat_no + emp_code + "".join(amt_parts) + filler + end_ind
	return line, amt_cents


def build_totals_line(company, slot_totals: list[int]) -> str:
	co_no = (company.sage_company_no or "").zfill(3)[:3]
	bat_no = (company.batch_no or " ")[:1]
	amt_parts = []
	for cents in slot_totals:
		sign = "-" if cents < 0 else "+"
		amt_parts.append(f"{abs(cents):011d}{sign}")
	filler = " " * 13
	return "T" + co_no + bat_no + (" " * 8) + "".join(amt_parts) + filler + "Z"


def export_txt(run_doc) -> None:
	_require_payroll_role()

	if run_doc.status not in ("Computed", "Exported"):
		frappe.throw(_('Run "Compute Hours & Leave" first - Status must be Computed before exporting.'))

	company = frappe.get_doc("Sage Payroll Company", run_doc.sage_payroll_company)

	rows = frappe.get_all(
		"Sage Payroll Employee",
		filters={"sage_payroll_run": run_doc.name, "match_status": "Matched"},
		fields=[
			"employee_code",
			"computed_normal_hours",
			"computed_ot_1_5",
			"computed_ot_2",
			"leave_hours_annual",
			"leave_hours_sick",
			"leave_hours_family",
		],
	)
	if not rows:
		frappe.throw(_("No Matched employees to export."))

	lines: list[str] = []
	slot_totals = [0, 0, 0, 0, 0, 0]
	for row in rows:
		line, amt_cents = build_detail_line(company, row)
		lines.append(line)
		for index, cents in enumerate(amt_cents):
			slot_totals[index] += cents

	include_totals = (
		cint_or_default(run_doc.get("include_totals_line"))
		if run_doc.get("include_totals_line") is not None
		else company.include_totals_line
	)
	if include_totals:
		lines.append(build_totals_line(company, slot_totals))

	content = "\r\n".join(lines) + "\r\n"

	frappe.db.set_value("Sage Payroll Run", run_doc.name, "status", "Exported")

	filename = f"IMP{company.sage_company_no}{company.batch_no}_{run_doc.name}.txt"
	frappe.response["filename"] = filename
	frappe.response["filecontent"] = content
	frappe.response["type"] = "download"


def cint_or_default(value) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return 0
