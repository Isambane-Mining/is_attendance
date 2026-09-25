# Payroll Controllers

One standalone Python script that runs on the Windows PC where Sage VIP
Premier's ODBC DSNs are configured - outside the Frappe bench entirely,
same pattern as `../clocking_controllers/` (`gateway.py`/`erp_uploader.py`
- see that folder's own README_CON.md for the fuller narrative on *why*
this split exists). It lives here (inside the `is_attendance` app source)
so it's version-controlled alongside the ERP logic it feeds, but doesn't
import Frappe or run on the bench.

| Script | Runs where | Job |
|---|---|---|
| `sage_employee_puller.py` | Windows host, next to the Sage VIP Premier ODBC DSNs | Queries Sage directly for one Company Number's active employee list, posts it into a Frappe `Sage Payroll Run`. |

## Credentials are never committed

`sage_employee_puller.json` (Frappe API key/secret) is gitignored.
`sage_employee_puller.example.json` is the tracked template. The runtime
log (`sage_employee_puller.log`) is also gitignored - host-local, never
meaningful to commit.

## Deployment

Unlike `gateway.py`/`erp_uploader.py`, this script doesn't prescribe a
folder tree - it isn't related to HikGateway and may well run on a
different machine entirely (the Payroll Office's own PC, wherever Sage's
ODBC DSNs are already configured for the existing Excel/VBA tooling, not
necessarily the clocking-terminal gateway PC). Config and log always live
next to the script itself (`sage_employee_puller.json`/`.log` beside
`sage_employee_puller.py`), so copy this folder's contents wherever makes
sense on that host - `C:\SagePayrollPuller\`, a subfolder next to the
existing Excel tooling, wherever - and it works from there. That copy is
still a stable path for a Scheduled Task to point at, since it stays put
once deployed; it just isn't a path this repo dictates.

**Setup on the Windows host**: copy this folder's contents to wherever
you're deploying it, copy `sage_employee_puller.example.json` to
`sage_employee_puller.json` next to the script, then fill in real values.
Never rename the `.example.json` in place and edit it directly inside a
git checkout - copy it out, or the real one will eventually get committed
the first time someone runs a blanket `git add`.

## `sage_employee_puller.py`

A heartbeat, same shape as `gateway.py`/`erp_uploader.py` in
`../clocking_controllers/`: runs continuously, polling Frappe rather than
being invoked with a specific Run to fetch. The trigger always starts on
the Frappe side - a person clicks **"Request Employee Pull"** on a
`Sage Payroll Run` (sets its Status to "Pull Requested"); this heartbeat's
next poll of `is_attendance.controllers.sage_payroll.list_pending_pull_requests`
is what notices and actually does the ODBC work. Nothing here ever queries
Sage unprompted.

Each cycle (`heartbeat_interval_seconds` in the config, default 60s):

1. GETs `list_pending_pull_requests` - every Sage Payroll Run currently at
   Status "Pull Requested", with the Sage Company Number and Paypoints
   each one needs (read from that Run's own `Sage Payroll Company` record
   on the Frappe side). Paypoints here are for reference/logging only now,
   not a query filter - see point 2.
2. For each request whose Company Number matches a **configured and
   enabled** entry on this controller's own `Sage Remote Controller`
   record in Frappe (fetched fresh every cycle via
   `get_remote_controller_config` - see below), connects to that
   Company's own ODBC DSN (`DSN=VIP_Company001;UID=;PWD=;` - blank
   UID/PWD, same connection string the real Excel query tooling already
   uses, confirmed by byte-searching a real Salary Sheet `.xls`'s embedded
   query definition), runs **one query pulling every active employee for
   that whole Company Number** (no per-Paypoint filtering - PaypointCode
   comes back as a column on each row instead), and POSTs the full
   employee list to that Run via `/api/method/run_doc_method`
   (`method: "ingest_employees"`) - the same mechanism `erp_uploader.py`
   already uses for `queue_import()`. Frappe's own `Sage Payroll
   Company.paypoints` does the Branch split downstream from each row's own
   PaypointCode (see `ingest_employees()`) - a Paypoint the Sage data
   contains but nobody's mapped in Frappe yet still comes through, just
   without a Branch resolved, rather than silently never being queried.
3. A request for a Company Number this instance doesn't have
   configured/enabled is skipped with a log line, not an error - lets
   several bench instances (or a future second Windows host) share the
   same polling endpoint without stepping on each other.

The Company Number/DSN mapping (`get_remote_controller_config`) is
re-fetched every cycle too, so enabling/disabling a company or changing
its DSN in Frappe doesn't need the heartbeat restarted, and each poll also
stamps that controller's own `Last Heartbeat` there - the Sage Remote
Controller list view is always the current "who's connected" picture, no
need to check a log file on the host itself.

**The query this script actually runs** (adapted from the real embedded
MS Query definition found in a Salary Sheet `.xls` - see the script's own
module docstring for the original, per-Paypoint form and the full byte-
search evidence; this version drops that WHERE clause and selects
PaypointCode as a column instead, to pull every Paypoint in one go):

```sql
SELECT EMP_INFO_FIXED.Surname AS SURNAME, EMP_INFO_FIXED.EmployeeCode AS COY,
       EMP_INFO_FIXED.FullNames AS NAME, EMP_INFO_FIXED.IDNumber AS ID,
       DESC_JOBTITLE.JobTitleLongDesc AS OCCUPATION, EMP_INFO_FIXED.PaypointCode AS PAYPOINT
FROM dba.DESC_JOBTITLE DESC_JOBTITLE, dba.EMP_INFO_FIXED EMP_INFO_FIXED
WHERE EMP_INFO_FIXED.JobTitleCode = DESC_JOBTITLE.JobTitleCode
  AND EMP_INFO_FIXED.EmployeeStatus = 'N'
ORDER BY EMP_INFO_FIXED.Surname, DESC_JOBTITLE.JobTitleLongDesc
```

Only identity columns (Surname/EmployeeCode/FullNames/IDNumber/
JobTitleLongDesc) are pulled this way - no evidence of a live query for
hours baselines, allowances, or leave balances was found in either real
sample file, so this script doesn't attempt to pull those. Normal /
Overtime 1.5 / Leave hours are computed entirely on the Frappe side from
our own Employee Checkin and Leave Application data instead (see
`is_attendance.controllers.sage_payroll.compute_hours_and_leave`).

**Idempotent**: re-running this script for the same Sage Payroll Run
simply refreshes the same `Sage Payroll Employee` rows (matched on
Sage Employee Code) rather than creating duplicates - safe to re-run to
pick up a Sage-side correction.

**Config** - `sage_employee_puller.json`, shape documented in
`sage_employee_puller.example.json`:

```json
{
  "base_url": "https://eben.isambane.co.za",
  "api_key": "...",
  "api_secret": "...",
  "heartbeat_interval_seconds": 60
}
```

That's the whole local config now - just enough to reach and authenticate
against this Frappe site. The Company Number/DSN mapping (formerly a
`companies` block here) now lives on a **Sage Remote Controller** record
in Frappe (Attendance workspace > Payroll > Sage Remote Controller),
matched to whichever User `api_key`/`api_secret` above belong to - create
one there (a Payroll Manager can do this) before running the script.
Paypoints, Branch mapping, and the Amt column layout still live on the
`Sage Payroll Company` doctype as before, also read fresh on every poll.
Adding a new site later is just: add its Paypoint on `Sage Payroll
Company`, and (only for a brand-new *Company Number* - a new DSN) a row on
the relevant Sage Remote Controller's own Companies Served table - nothing
on the Windows host itself needs editing or redeploying either way.

**Run it**: `python sage_employee_puller.py` (needs `pyodbc` and
`requests` installed, plus the same Sybase SQL Anywhere / iAnywhere ODBC
driver the existing VBA tooling already depends on). Intended to run
continuously - a Scheduled Task (see the Permissions note above before
making it unattended) or a Windows service wrapper, entirely independent
of `gateway.py`/`erp_uploader.py` since it isn't necessarily even on the
same machine as those two.

## Permissions

**Frappe-side**: the API user needs the **System Manager**, **Payroll
Manager**, or **Payroll User** role - `list_pending_pull_requests` and
`ingest_employees` both explicitly check for one of those three
(`is_attendance.controllers.sage_payroll._require_payroll_role`), on top
of the normal doctype-level create/write rights that role set already
carries for `Sage Payroll Employee`/`Sage Payroll Run`. Generate an API
key/secret for that user - that pair is what goes into
`sage_employee_puller.json`.

**Windows-side**: whatever account runs this script (interactively, as a
Scheduled Task, or as a service) needs:

- Read/write access to wherever it's deployed (to read its own config,
  write its own log).
- Outbound HTTPS to reach `base_url` (allowlist it if there's an egress
  firewall/proxy in the way).
- Whatever lets the existing Excel/VBA query tooling connect through each
  configured ODBC DSN today - **not independently verified this session**.
  The real connection string has blank credentials
  (`DSN=VIP_Company001;UID=;PWD=;`), which usually means the DSN itself
  holds (or otherwise resolves) the actual credential rather than the
  caller supplying one - but *how* it resolves that isn't confirmed. Some
  legacy Sybase/iAnywhere ODBC setups cache that per Windows user (under
  `HKEY_CURRENT_USER`), in which case only an interactive session logged
  in as that specific user can actually connect, and a headless Scheduled
  Task/service running as a different account would fail even though the
  DSN "exists" system-wide. **Before scheduling this unattended, confirm
  it actually connects under the account that will really run it** - e.g.
  a one-off interactive `python sage_employee_puller.py` test logged in as
  that account, or a manual Scheduled Task run - rather than assuming it
  behaves like the Excel tool just because the DSN is configured.

## Deliberately out of scope for this pass

The reverse direction - generating the Sage batch-import `.txt` from
Frappe and getting it into Sage - stays manual: download the `.txt` from
a `Sage Payroll Run`'s "Export .txt" button in Desk, and import it into
Sage via its own Batch Import function by hand. An automated Windows-side
executor for that direction is a later addition, not this one.
