frappe.ui.form.on('SimplePay Settings', {
  refresh(frm) {
    frm.add_custom_button(__('Test API Connection'), () => {
      frappe.call({
        method: 'is_attendance.controllers.simplepay.test_connection',
        freeze: true,
        freeze_message: __('Connecting to SimplePay...'),
        callback(r) {
          const count = Array.isArray(r.message?.clients) ? r.message.clients.length : 'unknown';
          frappe.msgprint({title: __('SimplePay Connection'), indicator: 'green',
            message: __('Connection succeeded. Accessible clients: {0}', [count])});
        }
      });
    }, __('SimplePay'));
    frm.add_custom_button(__('Fetch Leave Types'), () => {
      const opts = (frm.doc.companies || []).filter(x => x.enabled && x.company)
        .map(x => x.company);
      if (!opts.length) {
        frappe.msgprint(__('Configure an enabled company mapping first.'));
        return;
      }
      frappe.prompt([{fieldname: 'company', label: __('Frappe Company'),
        fieldtype: 'Select', options: opts.join('\n'), reqd: 1}], vals => {
        frappe.call({method: 'is_attendance.controllers.simplepay.list_leave_types',
          args: {company: vals.company},
          callback(r) {
            const data = r.message || {};
            const rows = Object.entries(data).map(([id, label]) =>
              `<tr><td>${frappe.utils.escape_html(String(id))}</td><td>${frappe.utils.escape_html(String(label))}</td></tr>`).join('');
            frappe.msgprint({title: __('SimplePay Leave Types - {0}', [vals.company]),
              message: `<table class="table table-bordered"><thead><tr><th>ID</th><th>Leave Type</th></tr></thead><tbody>${rows}</tbody></table>`,
              wide: true});
          }});
      }, __('Fetch Leave Types'));
    }, __('SimplePay'));
  }
});
