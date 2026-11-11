# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt


import frappe
from frappe import _
from frappe.model.document import Document


class SimplePaySettings(Document):
    def validate(self):
        self.propagate_companies()
        self.validate_employee_mappings()
        seen_companies = set()
        seen_clients = set()
        for row in self.companies or []:
            if not row.enabled:
                continue
            if row.company in seen_companies or str(row.simplepay_client_id) in seen_clients:
                frappe.throw(_('Duplicate active SimplePay Company or Client ID'))
            seen_companies.add(row.company)
            seen_clients.add(str(row.simplepay_client_id))

        seen_directory = set()
        seen_employee = set()
        for row in self.employee_map or []:
            if not row.simplepay_employee:
                frappe.throw(_('Every SimplePay Employee Map row needs a SimplePay Employee'))
            if row.simplepay_employee in seen_directory:
                frappe.throw(_('A SimplePay Employee cannot appear more than once'))
            seen_directory.add(row.simplepay_employee)
            if not row.employee:
                continue
            if row.employee in seen_employee:
                frappe.throw(_('A Frappe Employee cannot be mapped to multiple SimplePay employees'))
            seen_employee.add(row.employee)
            directory = frappe.db.get_value('SimplePay Employee', row.simplepay_employee,
                                            ['company', 'simplepay_client_id', 'is_active'], as_dict=True)
            if not directory:
                frappe.throw(_('SimplePay Employee record missing: {0}').format(row.simplepay_employee))
            employee_company = frappe.db.get_value('Employee', row.employee, 'company')
            if employee_company != directory.company:
                frappe.throw(_('Company mismatch for mapped Employee {0}').format(row.employee))
            active_clients = [str(c.simplepay_client_id) for c in self.companies or []
                              if c.enabled and c.company == employee_company]
            if str(directory.simplepay_client_id) not in active_clients:
                frappe.throw(_('Mapped employee belongs to an unconfigured SimplePay payroll'))

    def propagate_companies(self):
        client_map = {}

        for row in self.companies or []:
            client_id = str(row.simplepay_client_id or "").strip()

            if not client_id:
                continue

            if client_id in client_map:
                frappe.throw(
                    _("Duplicate SimplePay Client ID: {0}").format(
                        client_id
                    )
                )

            client_map[client_id] = row.company or None

        for table_name in ("leave_types", "remuneration_mappings"):
            for row in self.get(table_name) or []:
                client_id = str(
                    row.simplepay_client_id or ""
                ).strip()
                row.company = client_map.get(client_id)

    def validate_employee_mappings(self):
        assigned = set()
        external = set()

        for row in self.employee_map or []:
            if row.simplepay_employee in external:
                frappe.throw(
                    _("Duplicate SimplePay Employee mapping: {0}")
                    .format(row.simplepay_employee)
                )

            external.add(row.simplepay_employee)

            if row.employee:
                if row.employee in assigned:
                    frappe.throw(
                        _("Employee {0} is mapped more than once")
                        .format(row.employee)
                    )

                assigned.add(row.employee)
