frappe.ui.form.on('SimplePay Settings', {
    refresh(frm) {
        for (const table of ['companies', 'leave_types', 'remuneration_mappings']) {
            const grid = frm.fields_dict[table]?.grid;
            if (grid) grid.update_docfield_property('company', 'default', '');
        }

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
                frappe.msgprint(__('Save Settings before refreshing mappings.'));
                return;
            }
            frappe.confirm(
                __('Import SimplePay records and auto-map exact payroll-number matches? Manual mappings are preserved. Only GET requests will be sent to SimplePay.'),
                () => frappe.call({
                    method: 'is_attendance.controllers.simplepay_mapping.refresh_all_mappings',
                    freeze: true,
                    freeze_message: __('Queueing SimplePay import...'),
                    callback(r) {
                        if (r.message?.queued) {
                            frappe.msgprint({
                                title: __('SimplePay Import Queued'),
                                indicator: 'blue',
                                message: __('Reload Settings after the job finishes to inspect mapped and unmapped employees.')
                            });
                        }
                    }
                })
            );
        }, __('SimplePay'));

        frm.set_intro(__('SimplePay Client ID controls the Company shown on imported rows. Select Company only in Company Mapping; dependent Company fields update automatically. Employees are mapped only when SimplePay Payroll Number exactly matches an active Employee ID in that company.'));
    }
});

function propagate_simplepay_companies(frm) {
    const byClient = {};
    (frm.doc.companies || []).forEach(row => {
        const id = String(row.simplepay_client_id || '').trim();
        if (id) byClient[id] = row.company || null;
    });
    for (const table of ['leave_types', 'remuneration_mappings']) {
        (frm.doc[table] || []).forEach(row => {
            const id = String(row.simplepay_client_id || '').trim();
            const expected = byClient[id] || null;
            if ((row.company || null) !== expected) {
                frappe.model.set_value(row.doctype, row.name, 'company', expected);
            }
        });
        frm.refresh_field(table);
    }
}

frappe.ui.form.on('SimplePay Company Map', {
    companies_add(frm, cdt, cdn) {
        frappe.model.set_value(cdt, cdn, 'company', null);
    },
    company(frm) {
        propagate_simplepay_companies(frm);
    },
    simplepay_client_id(frm) {
        propagate_simplepay_companies(frm);
    },
    companies_remove(frm) {
        propagate_simplepay_companies(frm);
    }
});

frappe.ui.form.on('SimplePay Leave Type Map', {
    leave_types_add(frm, cdt, cdn) {
        frappe.model.set_value(cdt, cdn, 'company', null);
    }
});

frappe.ui.form.on('SimplePay Remuneration Map', {
    remuneration_mappings_add(frm, cdt, cdn) {
        frappe.model.set_value(cdt, cdn, 'company', null);
    }
});
