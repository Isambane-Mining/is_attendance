frappe.ui.form.on('SimplePay Settings', {
  refresh(frm) {
    frm.add_custom_button(__('Refresh SimplePay Employees'), () => {
      if (frm.is_dirty()) {
        frappe.msgprint(__('Save the Settings before refreshing the directory.'));
        return;
      }
      frappe.confirm(__('Fetch all SimplePay employees from every enabled company and preserve existing Employee mappings?'), () => {
        frappe.call({
          method: 'is_attendance.controllers.simplepay.refresh_employee_directory',
          freeze: true,
          freeze_message: __('Scheduling SimplePay employee refresh...'),
          callback(r) {
            if (r.message?.queued) {
              frappe.msgprint({
                title: __('SimplePay Employee Refresh'),
                indicator: 'blue',
                message: __('The refresh was queued. Reopen or reload SimplePay Settings after the background job finishes to see the new, numerically sorted rows. Existing Employee mappings are preserved.')
              });
            }
          }
        });
      });
    }, __('SimplePay'));

    frm.add_custom_button(__('Test API Connection'), () => {
      frappe.call({
        method: 'is_attendance.controllers.simplepay.test_connection',
        freeze: true,
        callback(r) {
          const clients = r.message?.clients || [];
          frappe.msgprint(__('Connected. Accessible clients: {0}', [Array.isArray(clients) ? clients.length : '?']));
        }
      });
    }, __('SimplePay'));

    frm.add_custom_button(__('Fetch Leave Types'), () => {
      const companies = (frm.doc.companies || []).filter(r => r.enabled && r.company).map(r => r.company);
      if (!companies.length) {
        frappe.msgprint(__('Configure an enabled company mapping first.'));
        return;
      }
      frappe.prompt([{fieldname: 'company', fieldtype: 'Select', label: __('Company'),
        options: companies.join('\n'), reqd: 1}], values => {
        frappe.call({
          method: 'is_attendance.controllers.simplepay.list_leave_types',
          args: {company: values.company},
          callback(r) {
            const esc = value => frappe.utils.escape_html(String(value ?? ''));
            const data = r.message || {};
            const rows = Object.entries(data).map(([id, label]) =>
              `<tr><td>${esc(id)}</td><td>${esc(label)}</td></tr>`).join('');
            frappe.msgprint({title: __('SimplePay Leave Types'), wide: true,
              message: `<table class="table table-bordered"><thead><tr><th>${__('ID')}</th><th>${__('Leave Type')}</th></tr></thead><tbody>${rows}</tbody></table>`});
          }
        });
      }, __('Choose company'));
    }, __('SimplePay'));
  }
});
