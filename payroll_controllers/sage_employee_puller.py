"""
sage_employee_puller.py - a heartbeat that polls Frappe for pending Sage
employee-pull requests and, for each one it can serve, queries Sage VIP
Premier directly (its own Windows ODBC DSNs, e.g. "VIP_Company001") for
that Company Number's active employee list and posts the result back into
the requesting Sage Payroll Run.

Runs continuously on the Windows PC that already has the VIP_Company00N
ODBC DSNs configured for the existing Excel/VBA payroll tooling - Sage's
DSNs aren't reachable from the Linux Frappe bench, same reason gateway.py/
erp_uploader.py already live here as separate Windows-side scripts (see
../clocking_controllers/README_CON.md for that same pattern, and this
folder's own README_CON.md for this script's).

Frappe never talks to Sage directly and this script never runs unprompted
against Sage - the trigger always starts on the Frappe side: a person
clicks "Request Employee Pull" on a Sage Payroll Run (sets its Status to
"Pull Requested"), and this heartbeat's next poll of
list_pending_pull_requests() is what notices and actually does the ODBC
work. Nothing here decides on its own which Runs need pulling.

Frappe's own Sage Payroll Company record is the sole source of truth for
which Paypoints belong to a Company Number - but only for the downstream
Branch split (see ingest_employees()), not for what gets queried: this
script pulls every active employee for a Company Number in a single query,
regardless of Paypoint, rather than querying per Paypoint and combining
results. The DSN-per-Company mapping used to live in this script's own
local config too, but now lives on this controller's own Sage Remote
Controller record in Frappe instead (fetched fresh every cycle via
get_remote_controller_config, identified by which User this script's
api_key/api_secret belong to) - the local config file now holds only what
Frappe genuinely has no business knowing: how to reach this Frappe site
and authenticate (base_url/api_key/api_secret), and how often to poll.
This means a newly-enabled Company/edited DSN needs no redeploy or restart
on the Windows host at all, and the "Connected controllers" summary on
that doctype's own list view is always current.

The query and connection shape below were extracted directly from the real
Salary Sheet .xls files this integration was reverse-engineered from - a
byte search of the OLE binary turned up the exact embedded MS Query
definition Excel itself was running (shown here as originally found, with
its own per-Paypoint WHERE clause - this script's own SQL_QUERY below
drops that clause and selects PaypointCode as a column instead, to pull
every Paypoint in one go):

    SELECT EMP_INFO_FIXED.Surname AS 'SURNAME', EMP_INFO_FIXED.EmployeeCode AS 'COY',
           EMP_INFO_FIXED.FullNames AS 'NAME', EMP_INFO_FIXED.IDNumber AS 'ID',
           DESC_JOBTITLE.JobTitleLongDesc AS 'OCCUPATION'
    FROM dba.DESC_JOBTITLE DESC_JOBTITLE, dba.EMP_INFO_FIXED EMP_INFO_FIXED
    WHERE EMP_INFO_FIXED.JobTitleCode = DESC_JOBTITLE.JobTitleCode
      AND ((EMP_INFO_FIXED.PaypointCode = ?) AND (EMP_INFO_FIXED.EmployeeStatus = 'N'))
    ORDER BY EMP_INFO_FIXED.Surname, DESC_JOBTITLE.JobTitleLongDesc

    DSN=VIP_Company001;UID=;PWD=;

Only identity columns are proven to be sourced this way - no embedded
query for hours baselines, allowances, or leave balances was found
anywhere in the real files, so this script (and the Frappe-side DocType it
feeds) deliberately doesn't attempt to pull those; Normal/Overtime/Leave
hours are computed on the Frappe side from our own Employee Checkin and
Leave Application data instead (see is_attendance.controllers.sage_payroll).

Needs `pyodbc` and `requests` installed, and the same Sybase SQL Anywhere
/ iAnywhere ODBC driver the existing VBA tooling already depends on (no
new driver install implied - if the Excel query tool works on this PC,
the DSN already works).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pyodbc
import requests

# Wherever this script itself is deployed - no folder convention prescribed
# (unlike gateway.py/erp_uploader.py's own C:\HikGateway\... tree, which
# makes sense for those two since they're tightly coupled to each other and
# to the specific clocking-terminal gateway PC; this script is unrelated to
# HikGateway and may well run on a different machine entirely - the Payroll
# Office's own PC, wherever Sage's ODBC DSNs are already configured for the
# existing Excel/VBA tooling). Config and log live next to the script
# itself, so copy this folder wherever makes sense on that host and it just
# works - still a stable path for a Scheduled Task to point at, since that
# copy stays put once deployed.
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "sage_employee_puller.json"
LOG_FILE = BASE_DIR / "sage_employee_puller.log"

HEARTBEAT_INTERVAL_SECONDS = 60  # overridable per-instance via config's own "heartbeat_interval_seconds"

SQL_QUERY = """
SELECT EMP_INFO_FIXED.Surname AS SURNAME, EMP_INFO_FIXED.EmployeeCode AS COY,
       EMP_INFO_FIXED.FullNames AS NAME, EMP_INFO_FIXED.IDNumber AS ID,
       DESC_JOBTITLE.JobTitleLongDesc AS OCCUPATION, EMP_INFO_FIXED.PaypointCode AS PAYPOINT
FROM dba.DESC_JOBTITLE DESC_JOBTITLE, dba.EMP_INFO_FIXED EMP_INFO_FIXED
WHERE EMP_INFO_FIXED.JobTitleCode = DESC_JOBTITLE.JobTitleCode
  AND EMP_INFO_FIXED.EmployeeStatus = 'N'
ORDER BY EMP_INFO_FIXED.Surname, DESC_JOBTITLE.JobTitleLongDesc
"""

logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s %(levelname)s %(message)s",
	handlers=[
		logging.FileHandler(LOG_FILE, encoding="utf-8"),
		logging.StreamHandler(),
	],
)


# ============================================================
# CONFIG
# ============================================================

def load_config() -> dict[str, Any]:
	if not CONFIG_FILE.exists():
		raise RuntimeError(
			f"Missing config file: {CONFIG_FILE}. Copy sage_employee_puller.example.json there and fill in real values."
		)
	with CONFIG_FILE.open("r", encoding="utf-8") as file:
		return json.load(file)


def find_company_config(companies: list[dict[str, Any]], sage_company_no: str) -> dict[str, Any] | None:
	"""Looks up the DSN mapping for a Sage Company Number within this
	cycle's own get_remote_controller_config() response - just
	`{sage_company_no, dsn, enabled}`. Everything else about that Company
	(Paypoints, Branch mapping, Amt layout) is read fresh from Frappe on
	every poll, never duplicated here or on the Sage Remote Controller
	record either."""
	for company in companies:
		if company.get("sage_company_no") == sage_company_no and company.get("enabled"):
			return company
	return None


# ============================================================
# SAGE (ODBC)
# ============================================================

def pull_company_employees(dsn: str, sage_company_no: str) -> list[dict[str, Any]]:
	"""Connects to this Company Number's own Sage ODBC DSN and pulls every
	active employee across every Paypoint in a single query - Frappe's own
	Sage Payroll Company.paypoints mapping does the branch split downstream
	(see ingest_employees()) from each row's own PaypointCode, rather than
	this script querying per Paypoint and combining the results. One query
	per Company Number is both simpler and cheaper than the old N-per-
	Paypoint approach, and it also means a Paypoint the Sage data actually
	contains but nobody's mapped in Frappe yet still comes through (just
	with no Branch resolved) instead of silently never being queried."""
	connection_string = f"DSN={dsn};UID=;PWD=;"
	records: list[dict[str, Any]] = []

	with pyodbc.connect(connection_string) as connection:
		cursor = connection.cursor()
		cursor.execute(SQL_QUERY)
		for row in cursor.fetchall():
			records.append(
				{
					"surname": row.SURNAME,
					"employee_code": row.COY,
					"full_names": row.NAME,
					"id_number": row.ID,
					"occupation": row.OCCUPATION,
					"paypoint_code": row.PAYPOINT,
				}
			)
		logging.info("Company %s: %d employee(s) pulled from %s.", sage_company_no, len(records), dsn)

	return records


# ============================================================
# FRAPPE
# ============================================================

def auth_headers(config: dict[str, Any]) -> dict[str, str]:
	return {"Authorization": f"token {config['api_key']}:{config['api_secret']}"}


def fetch_remote_controller_config(config: dict[str, Any]) -> list[dict[str, Any]]:
	"""Polls is_attendance.controllers.sage_payroll.get_remote_controller_config
	- this cycle's current {sage_company_no, dsn, enabled} list for
	whichever Sage Remote Controller record matches this script's own
	api_key/api_secret. Raises (via raise_for_status) if no such
	controller is registered/enabled in Frappe yet - a clear, loud failure
	in the log rather than silently pulling nothing every cycle."""
	response = requests.get(
		f"{config['base_url']}/api/method/is_attendance.controllers.sage_payroll.get_remote_controller_config",
		headers=auth_headers(config),
		timeout=30,
	)
	response.raise_for_status()
	return (response.json().get("message") or {}).get("companies") or []


def fetch_pending_requests(config: dict[str, Any]) -> list[dict[str, Any]]:
	"""Polls is_attendance.controllers.sage_payroll.list_pending_pull_requests
	- every Sage Payroll Run currently waiting on a pull, with the Sage
	Company Number and Paypoints each one needs (from its own Sage Payroll
	Company record). Not every entry necessarily belongs to this heartbeat
	instance - filtered against this script's own configured+enabled
	companies in run_heartbeat_cycle() below, so several bench instances
	(or a future second Windows host) can share the same polling endpoint
	without stepping on each other."""
	response = requests.get(
		f"{config['base_url']}/api/method/is_attendance.controllers.sage_payroll.list_pending_pull_requests",
		headers=auth_headers(config),
		timeout=30,
	)
	response.raise_for_status()
	return response.json().get("message") or []


def post_to_frappe(config: dict[str, Any], sage_payroll_run: str, records: list[dict[str, Any]]) -> dict[str, Any]:
	response = requests.post(
		f"{config['base_url']}/api/method/run_doc_method",
		headers={**auth_headers(config), "Content-Type": "application/json"},
		json={
			"dt": "Sage Payroll Run",
			"dn": sage_payroll_run,
			"method": "ingest_employees",
			"records": records,
		},
		timeout=120,
	)
	response.raise_for_status()
	return response.json().get("message", {})


# ============================================================
# HEARTBEAT CYCLE
# ============================================================

def run_heartbeat_cycle(config: dict[str, Any]) -> None:
	try:
		companies = fetch_remote_controller_config(config)
	except requests.RequestException:
		logging.exception("Failed to fetch this controller's Company/DSN config - check it's registered and enabled.")
		return

	try:
		pending = fetch_pending_requests(config)
	except requests.RequestException:
		logging.exception("Failed to poll for pending pull requests.")
		return

	if not pending:
		logging.info("Heartbeat: no pending pull requests.")
		return

	logging.info("Heartbeat: %d pending pull request(s).", len(pending))

	for request in pending:
		run_name = request.get("run")
		sage_company_no = request.get("sage_company_no")

		company = find_company_config(companies, sage_company_no)
		if not company:
			logging.warning(
				"Run %s wants Company %s, which isn't configured/enabled on this controller's own "
				"Sage Remote Controller record in Frappe - skipping (fine if a different controller owns that company).",
				run_name,
				sage_company_no,
			)
			continue

		# No per-Paypoint filtering here any more - one query pulls every
		# active employee for the whole Company Number, and Frappe's own
		# Sage Payroll Company.paypoints does the branch split downstream
		# from each row's own PaypointCode (see ingest_employees()). A
		# Company with no Paypoints mapped yet still gets pulled - it just
		# won't have a Branch resolved on any row until someone adds them.
		if not request.get("paypoints"):
			logging.info(
				"Run %s: Company %s has no Paypoints configured on its Sage Payroll Company record yet - "
				"pulling anyway, but Branch won't resolve on any row until some are added.",
				run_name,
				sage_company_no,
			)

		try:
			records = pull_company_employees(company["dsn"], sage_company_no)
			logging.info(
				"Run %s: pulled %d employee record(s) for Company %s.", run_name, len(records), sage_company_no
			)
			result = post_to_frappe(config, run_name, records)
			logging.info("Run %s: ingest result %s", run_name, result)
		except Exception:
			logging.exception("Run %s: heartbeat cycle failed.", run_name)


def main() -> None:
	logging.info("sage_employee_puller heartbeat started.")

	while True:
		try:
			config = load_config()  # re-read every cycle - a newly-enabled company/edited paypoint list needs no restart
			interval = config.get("heartbeat_interval_seconds") or HEARTBEAT_INTERVAL_SECONDS
			run_heartbeat_cycle(config)
		except Exception:
			logging.exception("Unhandled heartbeat-cycle exception.")
			interval = HEARTBEAT_INTERVAL_SECONDS

		time.sleep(interval)


if __name__ == "__main__":
	try:
		main()
	except KeyboardInterrupt:
		logging.info("sage_employee_puller stopped.")
