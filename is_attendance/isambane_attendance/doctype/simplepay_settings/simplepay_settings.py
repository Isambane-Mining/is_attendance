# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt


from frappe import _
import frappe
from frappe.model.document import Document


class SimplePaySettings(Document):
    def validate(self):
        self._propagate_companies()
        self._validate_mappings()

    def _client_map(self):
        result = {}
        company_to_client = {}
        for row in self.companies or []:
            client = str(row.simplepay_client_id or "").strip()
            if not client:
                frappe.throw(_("Every Company Mapping row needs a SimplePay Client ID"))
            if client in result:
                frappe.throw(_("Duplicate SimplePay Client ID {0}").format(client))
            company = row.company or None
            if row.enabled and not company:
                frappe.throw(_("Client {0} is enabled but has no mapped Frappe Company").format(client))
            if company:
                if company in company_to_client and company_to_client[company] != client:
                    frappe.throw(_("Company {0} is mapped to multiple SimplePay clients").format(company))
                company_to_client[company] = client
            result[client] = company
        return result

    def _propagate_companies(self):
        clients = self._client_map()
        for table in ("leave_types", "remuneration_mappings"):
            for row in self.get(table) or []:
                row.company = clients.get(str(row.get("simplepay_client_id") or "").strip())

    def _validate_mappings(self):
        clients = self._client_map()
        assigned_employees = set()
        assigned_external = set()
        for row in self.employee_map or []:
            external = row.simplepay_employee
            if not external:
                frappe.throw(_("Every SimplePay Employee Map row needs a SimplePay Employee"))
            if external in assigned_external:
                frappe.throw(_("Duplicate SimplePay Employee {0}").format(external))
            assigned_external.add(external)
            if not row.employee:
                continue
            if row.employee in assigned_employees:
                frappe.throw(_("Frappe Employee {0} is mapped more than once").format(row.employee))
            assigned_employees.add(row.employee)
            directory = frappe.db.get_value("SimplePay Employee", external,
                                            ["simplepay_client_id", "employee_number"], as_dict=True)
            if not directory:
                frappe.throw(_("SimplePay Employee record not found: {0}").format(external))
            expected_company = clients.get(str(directory.simplepay_client_id))
            employee_company = frappe.db.get_value("Employee", row.employee, "company")
            if not expected_company or employee_company != expected_company:
                frappe.throw(_("Company mismatch for Employee {0} and SimplePay Employee {1}").format(row.employee, external))

    def on_update(self):
        clients = self._client_map()
        for client_id, company in clients.items():
            rows = frappe.get_all("SimplePay Employee", filters={"simplepay_client_id": client_id},
                                  fields=["name", "company"])
            for row in rows:
                if row.company != company:
                    frappe.db.set_value("SimplePay Employee", row.name, "company", company,
                                        update_modified=False)
