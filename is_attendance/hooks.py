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
		"on_submit": [
			"is_attendance.controllers.attendance_sync.on_leave_application_change",
			"is_attendance.controllers.simplepay.on_submit",
		],
		"on_cancel": [
			"is_attendance.controllers.attendance_sync.on_leave_application_change",
			"is_attendance.controllers.simplepay.on_cancel",
		],
		"on_update_after_submit": "is_attendance.controllers.attendance_sync.on_leave_application_change",
	},
}
scheduler_events = {
	"daily": [
		"is_attendance.controllers.attendance_sync.enqueue_daily_sync",
		"is_attendance.controllers.watch_group_digest.send_daily_watch_group_digests",
		"is_attendance.controllers.simplepay.sync_employee_directory",
	],
	"weekly": [
		"is_attendance.controllers.watch_group_digest.send_weekly_watch_group_digests",
	],
	"monthly": [
		"is_attendance.controllers.watch_group_digest.send_monthly_watch_group_digests",
	],
	"cron": {
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
doctype_js = {
	"SimplePay Settings": "public/js/simplepay_settings.js",
	"Leave Application": "public/js/simplepay_leave_application.js",
}