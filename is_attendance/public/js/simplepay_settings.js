frappe.ui.form.on('SimplePay Settings', {
    refresh(frm) {
        frm.add_custom_button(__('Test API Connection'), () => {
            frappe.call({
                method: 'is_attendance.controllers.simplepay.test_connection',
                freeze: true,
                callback(r) {
                    const clients = r.message?.clients;
                    frappe.msgprint(__('Connection successful. Accessible clients: {0}', [
                        Array.isArray(clients) ? clients.length : '?'
                    ]));
                }
            });
        }, __('SimplePay'));

        frm.add_custom_button(__('Refresh All SimplePay Mappings'), () => {
            if (frm.is_dirty()) {
                frappe.msgprint(__('Save Settings before refreshing the mapping tables.'));
                return;
            }
            frappe.confirm(
                __('Import clients, leave types, remuneration items, and employees from SimplePay? Existing Frappe mappings will be preserved. No information will be sent to SimplePay.'),
                () => frappe.call({
                    method: 'is_attendance.controllers.simplepay_mapping.refresh_all_mappings',
                    freeze: true,
                    freeze_message: __('Queueing SimplePay refresh...'),
                    callback(r) {
                        if (r.message?.queued) {
                            frappe.msgprint({
                                title: __('SimplePay Import Queued'),
                                indicator: 'blue',
                                message: __('Import queued. Reload Settings after the background job completes to see the updated tables.')
                            });
                        }
                    }
                })
            );
        }, __('SimplePay'));

        frm.add_custom_button(__('Fill Missing Company Links'), () => {
            const byId = {};
            (frm.doc.companies || []).forEach(row => {
                if (row.company && row.simplepay_client_id) {
                    byId[String(row.simplepay_client_id)] = row.company;
                }
            });
            let total = 0;
            ['leave_types', 'remuneration_mappings'].forEach(table => {
                (frm.doc[table] || []).forEach(row => {
                    const company = byId[String(row.simplepay_client_id || '')];
                    if (!row.company && company) {
                        frappe.model.set_value(row.doctype, row.name, 'company', company);
                        total++;
                    }
                });
                frm.refresh_field(table);
            });
            frappe.show_alert({
                message: __('Filled {0} empty Company links. Save Settings to retain changes.', [total]),
                indicator: 'blue'
            });
        }, __('SimplePay'));

        // Extra info on how to use the four imported tables.
        frm.set_intro(__('Refresh All imports every available external record. Map Company, Leave Type, Salary Component and Employee manually in the matching tables. Existing links are retained on future refreshes.'));
    }
});
