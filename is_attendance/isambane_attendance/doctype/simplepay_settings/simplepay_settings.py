# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt


from frappe import _
import frappe
from frappe.model.document import Document


class SimplePaySettings(Document):
    def validate(self):
        if self.read_only_mode and (self.sync_approved_leave or self.sync_cancelled_leave):
            frappe.throw(_("Leave push/cancel writes to SimplePay; untick Read-Only Mode first"))
        self._propagate_companies()
        self._validate_mappings()
        self._validate_remuneration()

    def _validate_remuneration(self):
        """Mapping Status follows the Salary Component; the component type must fit the line type."""
        from is_attendance.controllers.simplepay_mapping import component_type_for_line
        components = {c.name: c for c in frappe.get_all(
            "Salary Component", fields=["name", "type", "do_not_include_in_total", "statistical_component"],
            filters={"name": ["in", list({r.salary_component for r in self.remuneration_mappings or []
                                          if r.salary_component})]})}
        for row in self.remuneration_mappings or []:
            if not row.salary_component:
                if row.mapping_status != "Ignored":
                    row.mapping_status = "Unmapped"
                continue
            row.mapping_status = "Mapped"
            line_type = row.simplepay_item_type
            expected = component_type_for_line(line_type)
            component = components.get(row.salary_component)
            label = f"{row.simplepay_item_label} ({row.simplepay_client_id}, row {row.idx})"
            if not expected or not component:
                frappe.throw(_("SimplePay line {0} cannot be mapped").format(label))
            if component.type != expected:
                frappe.throw(_("SimplePay {0} line {1} needs a {2} Salary Component; {3} is {4}").format(
                    line_type, label, expected, row.salary_component, component.type))
            if line_type == "benefit" and not (component.do_not_include_in_total or component.statistical_component):
                frappe.throw(_("SimplePay benefit line {0} is a non-cash fringe benefit; Salary Component {1} "
                               "must be Do Not Include in Total or Statistical").format(label, row.salary_component))

    def _client_map(self):
        """client ID -> Company. A Company may own several clients; a client only one Company."""
        result = {}
        for row in self.companies or []:
            client = str(row.simplepay_client_id or "").strip()
            if not client:
                frappe.throw(_("Every Company Mapping row needs a SimplePay Client ID"))
            if client in result:
                frappe.throw(_("Duplicate SimplePay Client ID {0}").format(client))
            company = row.company or None
            if row.enabled and not company:
                frappe.throw(_("Client {0} is enabled but has no mapped Frappe Company").format(client))
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
