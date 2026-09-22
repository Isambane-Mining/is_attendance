app_name = "is_attendance"
app_title = "Attendance"
app_publisher = "BuFf0k"
app_description = "Attendance Management and Integration Tool"
app_email = "buff0k@gmail.com"
app_license = "mit"
app_logo_url = "/assets/is_attendance/images/is-logo.svg"
app_home = "/desk/is-attendance"
required_apps = ["frappe/hrms"]
add_to_apps_screen = [
	{
		"name": app_name,
		"logo": "/assets/is_attendance/images/is-logo.svg",
		"title": app_title,
		"route": app_home,
	}
]
fixtures = [
	{"dt": "Custom Field", "filters": [["name", "in", [
		"Employee Checkin-isa_branch",
		"Employee Checkin-isa_clocking_machine",
		"Employee Checkin-isa_import_doctype",
		"Employee Checkin-isa_import_reference",
	]]]},
	{"dt": "Custom DocPerm", "filters": [["parent", "=", "Employee Checkin"], ["role", "in", [
		"Production Area Manager",
		"Engineering Area Manager",
	]]]},
]
doc_events = {
	"Employee Checkin": {
		"after_insert": "is_attendance.controllers.attendance_sync.on_employee_checkin",
	},
	"Leave Application": {
		"on_submit": "is_attendance.controllers.attendance_sync.on_leave_application_change",
		"on_cancel": "is_attendance.controllers.attendance_sync.on_leave_application_change",
		"on_update_after_submit": "is_attendance.controllers.attendance_sync.on_leave_application_change",
	},
}
scheduler_events = {
	"daily": [
		"is_attendance.controllers.attendance_sync.enqueue_daily_sync",
		"is_attendance.controllers.watch_group_digest.send_daily_watch_group_digests",
	],
	"weekly": [
		"is_attendance.controllers.watch_group_digest.send_weekly_watch_group_digests",
	],
	"monthly": [
		"is_attendance.controllers.watch_group_digest.send_monthly_watch_group_digests",
	],
	"cron": {
		# A document can only genuinely still be "Importing" for as long as
		# IMPORT_JOB_TIMEOUT allows - checking every 15 minutes catches a
		# real stall reasonably soon after that ceiling, without being so
		# frequent it's doing real work most ticks find nothing to do.
		"*/15 * * * *": [
			"is_attendance.controllers.clocking_import.recover_stalled_imports",
		],
	},
}
permission_query_conditions = {
	"Employee Checkin": "is_attendance.permissions.employee_checkin_permission_query_conditions",
	"Clocking Adjustment": "is_attendance.permissions.clocking_adjustment_permission_query_conditions",
}
has_permission = {
	"Employee Checkin": "is_attendance.permissions.employee_checkin_has_permission",
	"Clocking Adjustment": "is_attendance.permissions.clocking_adjustment_has_permission",
}