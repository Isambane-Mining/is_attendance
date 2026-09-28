// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

/*
 * Same visual language as the Clocking Import Issues page (stat cards,
 * donut charts via frappe.Chart, a restyled table) - see that page's own
 * comments for the reasoning behind each pattern reused here (fixed
 * table layout + truncation to avoid a wide-content horizontal scroll,
 * the injected <style> block, etc.). Every number and chart here is
 * derived straight from the Attendance Compliance Summary report's own
 * rows (frappe.desk.query_report.run) - no separate stats endpoint,
 * since every column a chart needs (weekday_missed, total_missed, ...)
 * is already on each row.
 */

const REPORT_NAME = "Attendance Compliance Summary";

const AD_METRICS = ["missed", "no_out", "no_in", "late_in", "early_out"];
const AD_METRIC_LABELS = {
	missed: __("Missed"),
	no_out: __("No Out"),
	no_in: __("No In"),
	late_in: __("Late In"),
	early_out: __("Early Out"),
};
const AD_METRIC_COLORS = {
	missed: "#ef4444",
	no_out: "#f97316",
	no_in: "#f59e0b",
	late_in: "#eab308",
	early_out: "#ec4899",
};
const AD_DAY_TYPES = ["weekday", "saturday", "sunday"];
const AD_DAY_TYPE_LABELS = { weekday: __("Weekday"), saturday: __("Saturday"), sunday: __("Sunday") };
const AD_DAY_TYPE_COLORS = { weekday: "#3b82f6", saturday: "#f59e0b", sunday: "#ef4444" };

frappe.pages["attendance-dashboard"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Attendance Dashboard"),
		single_column: true,
	});

	new is_attendance.AttendanceDashboard(page);
};

frappe.provide("is_attendance");

is_attendance.AttendanceDashboard = class AttendanceDashboard {
	constructor(page) {
		this.page = page;

		// The Table MultiSelect control reads its linked child doctype's
		// meta synchronously (frappe.get_meta) - on a real document form
		// that's always pre-loaded before the form renders, but a bare
		// Page never fetches it, so creating the field before this
		// resolves throws and aborts the rest of the page's setup. Load it
		// explicitly first.
		frappe.model.with_doctype("Attendance Dashboard Employee", () => {
			ad_ensure_style();
			this.make_filters();
			this.make_body();
			this.run();
		});
	}

	make_filters() {
		this.filters = {};

		this.filters.employees = this.page.add_field({
			fieldname: "employees",
			label: __("Employees"),
			fieldtype: "Table MultiSelect",
			options: "Attendance Dashboard Employee",
			change: () => this.run(),
		});

		this.filters.watch_group = this.page.add_field({
			fieldname: "watch_group",
			label: __("Watch Group"),
			fieldtype: "Link",
			options: "Watch Group",
			description: __("Restricts to this group's own Employees - combines with Employees/Branch/etc. below, doesn't replace them."),
			change: () => this.run(),
		});

		this.filters.company = this.page.add_field({
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			change: () => this.run(),
		});

		this.filters.branch = this.page.add_field({
			fieldname: "branch",
			label: __("Branch"),
			fieldtype: "Link",
			options: "Branch",
			change: () => this.run(),
		});

		this.filters.department = this.page.add_field({
			fieldname: "department",
			label: __("Department"),
			fieldtype: "Link",
			options: "Department",
			change: () => this.run(),
		});

		this.filters.payroll_cost_center = this.page.add_field({
			fieldname: "payroll_cost_center",
			label: __("Payroll Cost Center"),
			fieldtype: "Link",
			options: "Cost Center",
			change: () => this.run(),
		});

		this.filters.occupational_level = this.page.add_field({
			// Employee.za_occupational_level (za_local's own Employment
			// Equity Act field) - same options list, reused directly.
			fieldname: "occupational_level",
			label: __("Occupational Level"),
			fieldtype: "Select",
			options: "\nTop Management\nSenior Management\nProfessionally Qualified\nSkilled Technical\nSemi-Skilled\nUnskilled\nTemporary Employees\nNon-Permanent",
			change: () => this.run(),
		});

		this.filters.include_inactive = this.page.add_field({
			fieldname: "include_inactive",
			label: __("Include Inactive Employees"),
			fieldtype: "Check",
			default: 0,
			change: () => this.run(),
		});

		this.filters.exclude_weekends = this.page.add_field({
			fieldname: "exclude_weekends",
			label: __("Exclude Weekends from Calculations"),
			fieldtype: "Check",
			default: 0,
			description: __("Saturdays/Sundays still show, they just never trigger Missed/No Out/No In/Late In/Early Out."),
			change: () => this.run(),
		});

		this.filters.exclude_public_holidays = this.page.add_field({
			fieldname: "exclude_public_holidays",
			label: __("Exclude Public Holidays from Calculations"),
			fieldtype: "Check",
			default: 1,
			description: __("On by default - a public holiday still shows, it just never triggers Missed/No Out/No In/Late In/Early Out. Uncheck to treat it as a normal day for those flags."),
			change: () => this.run(),
		});

		this.filters.from_date = this.page.add_field({
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
			change: () => this.run(),
		});

		this.filters.to_date = this.page.add_field({
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			change: () => this.run(),
		});

		this.filters.start_time = this.page.add_field({
			fieldname: "start_time",
			label: __("Start Time"),
			fieldtype: "Time",
			default: "06:00:00",
			change: () => this.run(),
		});

		this.filters.end_time = this.page.add_field({
			fieldname: "end_time",
			label: __("End Time"),
			fieldtype: "Time",
			default: "16:00:00",
			change: () => this.run(),
		});

		this.filters.threshold = this.page.add_field({
			fieldname: "threshold",
			label: __("Threshold (min)"),
			fieldtype: "Int",
			default: 15,
			change: () => this.run(),
		});

		this.filters.normal_hours_threshold = this.page.add_field({
			fieldname: "normal_hours_threshold",
			label: __("Normal Hours Threshold"),
			fieldtype: "Float",
			default: 195,
			description: __("Per employee, per month - hours above this count as Overtime in the Overtime Distribution table below."),
			change: () => this.run(),
		});

		this.page.set_primary_action(__("Refresh"), () => this.run(), "refresh");
		this.page.add_inner_button(__("Export to Excel"), () => this.export_excel());
	}

	make_body() {
		this.$body = $(`
			<div class="ad-page">
				<div class="ad-stats-row"></div>
				<div class="ad-charts-row"></div>
				<div class="ad-section">
					<div class="ad-section-header">
						<h4>${__("Employees")}</h4>
						<p class="text-muted">${__("Click a row to see its own day-by-day breakdown for the period.")}</p>
					</div>
					<div class="ad-table-wrap"></div>
				</div>
				<div class="ad-section">
					<div class="ad-section-header">
						<h4>${__("Overtime Distribution")}</h4>
						<p class="text-muted">${__("One column per calendar month in the selected range. Normal vs Overtime is split per employee per month at the Normal Hours Threshold above (default 195).")}</p>
					</div>
					<div class="ad-overtime-table-wrap"></div>
				</div>
			</div>
		`).appendTo(this.page.main);

		this.$stats_row = this.$body.find(".ad-stats-row");
		this.$charts_row = this.$body.find(".ad-charts-row");
		this.$table = this.$body.find(".ad-table-wrap");
		this.$overtime_table = this.$body.find(".ad-overtime-table-wrap");
	}

	get_filter_values() {
		const values = {};
		for (const key of Object.keys(this.filters)) {
			values[key] = this.filters[key].get_value();
		}
		// Table MultiSelect's value is an array of child rows ({employee:
		// "..."}), not plain employee names - the report/export filters
		// want the names.
		values.employees = (values.employees || []).map((row) => row.employee);
		return values;
	}

	run() {
		const filters = this.get_filter_values();
		if (!filters.from_date || !filters.to_date) return;

		frappe.call({
			method: "frappe.desk.query_report.run",
			args: {
				report_name: REPORT_NAME,
				filters: filters,
			},
			callback: (r) => this.render(r.message),
		});

		frappe.call({
			method: "is_attendance.isambane_attendance.page.attendance_dashboard.attendance_dashboard.get_overtime_distribution",
			args: { filters: JSON.stringify(filters) },
			callback: (r) => this.render_overtime_table(r.message || { months: [], rows: [] }),
		});
	}

	render(message) {
		this.columns = (message && message.columns) || [];
		this.rows = (message && message.result) || [];
		this.render_stats(this.rows);
		this.render_table(this.columns, this.rows);
	}

	render_stats(rows) {
		const metric_totals = { missed: 0, no_out: 0, no_in: 0, late_in: 0, early_out: 0 };
		const day_type_totals = { weekday: 0, saturday: 0, sunday: 0 };
		let employees_with_issues = 0;

		for (const row of rows) {
			let row_has_issue = false;
			for (const metric of AD_METRICS) {
				const total = row[`total_${metric}`] || 0;
				metric_totals[metric] += total;
				if (total) row_has_issue = true;

				for (const day_type of AD_DAY_TYPES) {
					day_type_totals[day_type] += row[`${day_type}_${metric}`] || 0;
				}
			}
			if (row_has_issue) employees_with_issues += 1;
		}

		const total_flags = AD_METRICS.reduce((sum, metric) => sum + metric_totals[metric], 0);

		const cards = [
			{ label: __("Employees Shown"), value: rows.length, tone: "neutral" },
			{
				label: __("Employees With Issues"),
				value: employees_with_issues,
				sub: __("of {0}", [rows.length]),
				tone: employees_with_issues ? "bad" : "good",
			},
			{ label: __("Missed"), value: metric_totals.missed, tone: metric_totals.missed ? "bad" : "good" },
			{ label: __("No Out"), value: metric_totals.no_out, tone: metric_totals.no_out ? "bad" : "good" },
			{ label: __("No In"), value: metric_totals.no_in, tone: metric_totals.no_in ? "bad" : "good" },
			{ label: __("Late In"), value: metric_totals.late_in, tone: metric_totals.late_in ? "bad" : "good" },
			{ label: __("Early Out"), value: metric_totals.early_out, tone: metric_totals.early_out ? "bad" : "good" },
		];

		this.$stats_row.html(
			cards
				.map(
					(card) => `
						<div class="ad-stat-card ad-tone-${card.tone}">
							<div class="ad-stat-value">${card.value ?? 0}</div>
							<div class="ad-stat-label">${card.label}</div>
							${card.sub ? `<div class="ad-stat-sub">${card.sub}</div>` : ""}
						</div>
					`
				)
				.join("")
		);

		this.render_charts(rows.length, employees_with_issues, metric_totals, total_flags, day_type_totals);
	}

	render_charts(total_employees, employees_with_issues, metric_totals, total_flags, day_type_totals) {
		this.$charts_row.html(`
			<div class="ad-chart-card">
				<div class="ad-chart-title">${__("Compliance Flags Breakdown")}</div>
				<div id="ad-chart-flags"></div>
			</div>
			<div class="ad-chart-card">
				<div class="ad-chart-title">${__("Employees: Clean vs With Issues")}</div>
				<div id="ad-chart-employees"></div>
			</div>
			<div class="ad-chart-card">
				<div class="ad-chart-title">${__("Flags by Day Type")}</div>
				<div id="ad-chart-daytype"></div>
			</div>
		`);

		const metrics_present = AD_METRICS.filter((metric) => metric_totals[metric] > 0);
		ad_render_donut(
			"#ad-chart-flags",
			metrics_present.map((metric) => AD_METRIC_LABELS[metric]),
			metrics_present.map((metric) => metric_totals[metric]),
			metrics_present.map((metric) => AD_METRIC_COLORS[metric]),
			total_flags
		);

		const employees_clean = total_employees - employees_with_issues;
		ad_render_donut(
			"#ad-chart-employees",
			[__("Clean"), __("With Issues")],
			[employees_clean, employees_with_issues],
			["#22c55e", "#ef4444"],
			total_employees
		);

		const day_types_present = AD_DAY_TYPES.filter((day_type) => day_type_totals[day_type] > 0);
		const total_day_type_flags = AD_DAY_TYPES.reduce((sum, day_type) => sum + day_type_totals[day_type], 0);
		ad_render_donut(
			"#ad-chart-daytype",
			day_types_present.map((day_type) => AD_DAY_TYPE_LABELS[day_type]),
			day_types_present.map((day_type) => day_type_totals[day_type]),
			day_types_present.map((day_type) => AD_DAY_TYPE_COLORS[day_type]),
			total_day_type_flags
		);
	}

	render_table(columns, rows) {
		if (!columns.length) {
			this.$table.html(`<div class="ad-empty">${__("No data for the selected filters.")}</div>`);
			return;
		}

		// Reset on every full re-render (filters changed, Refresh clicked) -
		// a stale employee's cached daily detail from a previous period/
		// filter set must never be shown under a new set of results.
		this._detail_cache = {};

		const colspan = AD_METRICS.length + 5;

		const header = `
			<th class="ad-col-narrow"></th>
			<th class="ad-col-code">${__("Employee")}</th>
			<th>${__("Employee Name")}</th>
			<th class="ad-col-code">${__("Branch")}</th>
			${AD_METRICS.map((metric) => `<th class="ad-col-narrow">${AD_METRIC_LABELS[metric]}</th>`).join("")}
			<th class="ad-col-action"></th>
		`;

		const body = rows
			.map((row) => {
				const employee = frappe.utils.escape_html(row.employee ?? "");
				const row_has_issue = AD_METRICS.some((metric) => row[`total_${metric}`]);
				return `
					<tr class="ad-summary-row ${row_has_issue ? "ad-row-issue" : ""}" data-employee="${employee}">
						<td class="ad-drill-toggle">&#9656;</td>
						<td>${employee}</td>
						<td class="ad-col-truncate" title="${frappe.utils.escape_html(row.employee_name ?? "")}">${frappe.utils.escape_html(row.employee_name ?? "")}</td>
						<td class="ad-col-truncate" title="${frappe.utils.escape_html(row.branch ?? "")}">${frappe.utils.escape_html(row.branch ?? "")}</td>
						${AD_METRICS.map((metric) => {
							const value = row[`total_${metric}`] ?? 0;
							return `<td>${value ? `<span class="ad-badge">${value}</span>` : `<span class="text-muted">0</span>`}</td>`;
						}).join("")}
						<td><button class="btn btn-xs btn-default ad-create-adjustment-btn">${__("Create Clocking Adjustment")}</button></td>
					</tr>
					<tr class="ad-detail-row" data-employee="${employee}" style="display: none;">
						<td colspan="${colspan}"><div class="ad-detail-container text-muted">${__("Loading...")}</div></td>
					</tr>
				`;
			})
			.join("");

		this.$table.html(`
			<table class="ad-table ad-table-fixed">
				<thead><tr>${header}</tr></thead>
				<tbody>${body || `<tr><td colspan="${colspan}" class="text-muted text-center">${__("No records")}</td></tr>`}</tbody>
			</table>
		`);

		this.wire_table_events();
	}

	wire_table_events() {
		this.$table.off("click", ".ad-drill-toggle").on("click", ".ad-drill-toggle", (event) => {
			const $row = $(event.currentTarget).closest("tr");
			this.toggle_detail($row.data("employee"), $row);
		});

		this.$table.off("click", ".ad-create-adjustment-btn").on("click", ".ad-create-adjustment-btn", (event) => {
			const $row = $(event.currentTarget).closest("tr");
			this.create_adjustment($row.data("employee"));
		});
	}

	render_overtime_table(message) {
		const months = message.months || [];
		const rows = message.rows || [];

		if (!months.length || !rows.length) {
			this.$overtime_table.html(`<div class="ad-empty">${__("No data for the selected filters.")}</div>`);
			return;
		}

		const format_value = (row, month_key) => {
			const value = row.values[month_key] ?? 0;
			return row.type === "count" ? String(value) : frappe.format(value, { fieldtype: "Float", precision: 2 });
		};

		const header = `
			<th class="ad-ot-label-col"></th>
			${months.map((month) => `<th>${frappe.utils.escape_html(month.label)}</th>`).join("")}
		`;

		const body = rows
			.map((row) => {
				const row_class = row.group_start ? "ad-ot-group-start" : "";
				return `
					<tr class="${row_class}">
						<td class="ad-ot-label-col"><strong>${frappe.utils.escape_html(row.label)}</strong></td>
						${months.map((month) => `<td>${format_value(row, month.key)}</td>`).join("")}
					</tr>
				`;
			})
			.join("");

		this.$overtime_table.html(`
			<table class="ad-table ad-ot-table">
				<thead><tr>${header}</tr></thead>
				<tbody>${body}</tbody>
			</table>
		`);
	}

	toggle_detail(employee, $summary_row) {
		const $detail_row = $summary_row.next(".ad-detail-row");
		const $toggle = $summary_row.find(".ad-drill-toggle");

		if ($detail_row.is(":visible")) {
			$detail_row.hide();
			$toggle.html("&#9656;");
			return;
		}

		$detail_row.show();
		$toggle.html("&#9662;");

		if (this._detail_cache[employee]) {
			this.render_detail($detail_row, this._detail_cache[employee]);
			return;
		}

		frappe.call({
			method: "is_attendance.isambane_attendance.page.attendance_dashboard.attendance_dashboard.get_daily_detail",
			args: { filters: JSON.stringify(this.get_filter_values()), employee },
			callback: (r) => {
				this._detail_cache[employee] = r.message || [];
				this.render_detail($detail_row, this._detail_cache[employee]);
			},
		});
	}

	render_detail($detail_row, day_rows) {
		const $container = $detail_row.find(".ad-detail-container");

		if (!day_rows.length) {
			$container.html(`<span class="text-muted">${__("No days in range.")}</span>`);
			return;
		}

		const flag_labels = AD_METRIC_LABELS;

		const rows_html = day_rows
			.map((day) => {
				const flags = Object.keys(flag_labels)
					.filter((key) => day[key])
					.map((key) => `<span class="indicator-pill red" style="margin-right: 4px;">${flag_labels[key]}</span>`)
					.join("");
				const holiday = day.public_holiday
					? `<span class="indicator-pill blue" style="margin-right: 4px;">${frappe.utils.escape_html(day.public_holiday)}</span>`
					: "";
				// on_leave/half_day_leave both cover an Open (pending) request
				// the same as an Approved one (see attendance_compliance_summary.py's
				// EXEMPTING_LEAVE_STATUSES) - leave_status refines the label so
				// "still pending" doesn't read as if it were already decided.
				const is_pending = day.leave_status === "Open";
				const on_leave = day.on_leave
					? `<span class="indicator-pill ${is_pending ? "yellow" : "green"}" style="margin-right: 4px;">${
							is_pending ? __("Leave (Pending)") : __("On Leave")
					  }</span>`
					: "";
				const half_day_leave = day.half_day_leave
					? `<span class="indicator-pill ${is_pending ? "yellow" : "orange"}" style="margin-right: 4px;">${
							is_pending ? __("Half Day (Pending)") : __("Half Day Leave")
					  }</span>`
					: "";
				// Rejected/Cancelled never exempts anything (a Missed flag above
				// still fires normally) - shown so that flag isn't an
				// unexplained "why is this Missed" moment.
				const non_exempting_leave =
					!day.on_leave && !day.half_day_leave && day.leave_status
						? `<span class="indicator-pill red" style="margin-right: 4px;">${__("Leave {0}", [day.leave_status])}</span>`
						: "";

				return `
					<tr>
						<td>${day.date ? frappe.datetime.str_to_user(day.date) : ""}</td>
						<td>${frappe.utils.escape_html(day.day || "")}</td>
						<td>${day.in_time ? frappe.datetime.get_time(day.in_time) : ""}</td>
						<td>${day.out_time ? frappe.datetime.get_time(day.out_time) : ""}</td>
						<td title="${__("Sum of every clock-in/out pair this day, not simply Out minus In - a day with more than one session (e.g. a lunch break) has gaps in between that aren't worked time.")}">${(day.hours_worked || 0).toFixed(2)}</td>
						<td>${flags}${holiday}${on_leave}${half_day_leave}${non_exempting_leave}</td>
					</tr>
				`;
			})
			.join("");

		$container.html(`
			<table class="ad-detail-table">
				<thead>
					<tr>
						<th>${__("Date")}</th>
						<th>${__("Day")}</th>
						<th>${__("In")}</th>
						<th>${__("Out")}</th>
						<th>${__("Hours Worked")}</th>
						<th>${__("Flags")}</th>
					</tr>
				</thead>
				<tbody>${rows_html}</tbody>
			</table>
		`);
	}

	create_adjustment(employee) {
		const filters = this.get_filter_values();
		frappe.new_doc("Clocking Adjustment", {
			employee: employee,
			from_date: filters.from_date,
			to_date: filters.to_date,
		});
	}

	export_excel() {
		open_url_post(frappe.request.url, {
			cmd: "is_attendance.isambane_attendance.page.attendance_dashboard.attendance_dashboard.export_compliance_excel",
			filters: JSON.stringify(this.get_filter_values()),
		});
	}
};

function ad_render_donut(selector, labels, values, colors, total) {
	const $container = $(selector);
	if (!total) {
		$container.html(`<div class="ad-chart-empty">${__("No data yet")}</div>`);
		return;
	}

	new frappe.Chart(selector, {
		type: "donut",
		height: 200,
		data: { labels: labels, datasets: [{ values: values }] },
		colors: colors,
		maxSlices: labels.length,
		tooltipOptions: {
			formatTooltipY: (value) => `${value} (${Math.round((value / total) * 100)}%)`,
		},
	});
}

function ad_ensure_style() {
	if (document.getElementById("ad-style")) return;

	const style = document.createElement("style");
	style.id = "ad-style";
	style.textContent = `
		.ad-page { padding-bottom: 20px; }

		.ad-stats-row {
			display: grid;
			grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
			gap: 12px;
			margin: 15px 0 20px;
		}
		.ad-stat-card {
			background: var(--card-bg, #fff);
			border: 1px solid var(--border-color, #d1d8dd);
			border-radius: 8px;
			padding: 16px;
			text-align: center;
			border-top: 3px solid var(--border-color, #d1d8dd);
		}
		.ad-stat-card.ad-tone-good { border-top-color: #22c55e; }
		.ad-stat-card.ad-tone-bad { border-top-color: #ef4444; }
		.ad-stat-value { font-size: 26px; font-weight: 700; line-height: 1.2; }
		.ad-tone-good .ad-stat-value { color: #16a34a; }
		.ad-tone-bad .ad-stat-value { color: #dc2626; }
		.ad-stat-label { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
		.ad-stat-sub { font-size: 11px; color: var(--text-light, #aaa); margin-top: 2px; }

		.ad-charts-row {
			display: grid;
			grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
			gap: 12px;
			margin-bottom: 30px;
		}
		.ad-chart-card {
			background: var(--card-bg, #fff);
			border: 1px solid var(--border-color, #d1d8dd);
			border-radius: 8px;
			padding: 12px 16px;
		}
		.ad-chart-title { font-size: 13px; font-weight: 600; color: var(--text-muted); margin-bottom: 4px; }
		.ad-chart-empty {
			height: 200px;
			display: flex;
			align-items: center;
			justify-content: center;
			color: var(--text-muted);
			font-size: 12px;
		}

		.ad-section {
			background: var(--card-bg, #fff);
			border: 1px solid var(--border-color, #d1d8dd);
			border-radius: 8px;
			margin-bottom: 20px;
			overflow: hidden;
		}
		.ad-section-header { padding: 14px 18px 10px; border-bottom: 1px solid var(--border-color, #d1d8dd); }
		.ad-section-header h4 { margin: 0 0 4px; }
		.ad-section-header p { margin: 0; font-size: 12px; }

		.ad-table-wrap { overflow-x: auto; width: 100%; max-width: 100%; min-width: 0; }
		.ad-empty { padding: 24px 18px; color: var(--text-muted); font-size: 13px; }

		.ad-overtime-table-wrap { overflow-x: auto; width: 100%; max-width: 100%; min-width: 0; }
		.ad-ot-table td, .ad-ot-table th { text-align: right; white-space: nowrap; }
		.ad-ot-table .ad-ot-label-col { text-align: left; white-space: normal; }
		.ad-ot-table tr.ad-ot-group-start td { border-top: 2px solid var(--border-color, #d1d8dd); }

		/* table-layout: fixed - see the Clocking Import Issues page's own
		   CSS comment for why this matters: without it, a single long
		   unbroken value (a long Employee Name/Branch) can force the whole
		   table wider than its container, escaping .ad-table-wrap's own
		   overflow-x and pushing a horizontal scroll onto the page itself
		   rather than staying contained. */
		.ad-table { width: 100%; max-width: 100%; border-collapse: collapse; font-size: 13px; }
		.ad-table-fixed { table-layout: fixed; }
		.ad-table th {
			text-align: left;
			font-weight: 600;
			font-size: 11px;
			text-transform: uppercase;
			letter-spacing: 0.03em;
			color: var(--text-muted);
			padding: 10px 14px;
			border-bottom: 1px solid var(--border-color, #d1d8dd);
			white-space: nowrap;
			overflow: hidden;
			text-overflow: ellipsis;
		}
		.ad-table td {
			padding: 8px 14px;
			border-bottom: 1px solid var(--border-color, #eef1f2);
			vertical-align: middle;
			overflow-wrap: break-word;
		}
		.ad-table tbody tr.ad-summary-row:hover { background: var(--control-bg, #f4f5f6); }
		.ad-table tbody tr.ad-row-issue { background: rgba(239, 68, 68, 0.04); }
		.ad-drill-toggle { cursor: pointer; width: 24px; }
		.ad-col-narrow { width: 76px; }
		.ad-col-code { width: 130px; }
		.ad-col-action { width: 190px; }
		.ad-col-truncate {
			max-width: 0;
			overflow: hidden;
			text-overflow: ellipsis;
			white-space: nowrap;
		}

		.ad-badge {
			display: inline-block;
			min-width: 22px;
			text-align: center;
			background: #fee2e2;
			color: #b91c1c;
			border-radius: 10px;
			padding: 1px 8px;
			font-size: 12px;
			font-weight: 600;
		}

		.ad-detail-table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 0; }
		.ad-detail-table th {
			text-align: left;
			font-weight: 600;
			font-size: 11px;
			text-transform: uppercase;
			letter-spacing: 0.03em;
			color: var(--text-muted);
			padding: 8px 12px;
			border-bottom: 1px solid var(--border-color, #d1d8dd);
		}
		.ad-detail-table td {
			padding: 6px 12px;
			border-bottom: 1px solid var(--border-color, #eef1f2);
		}
		.ad-detail-table tbody tr:last-child td { border-bottom: none; }
	`;
	document.head.appendChild(style);
}
