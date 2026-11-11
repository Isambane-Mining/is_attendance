frappe.ui.form.on('SimplePay Settings', {
  refresh(frm) {
    frm.add_custom_button(__('Test API Connection'), () => {
      frappe.call({method:'is_attendance.controllers.simplepay.test_connection',
        freeze:true, callback(r){
          const clients = r.message?.clients || [];
          frappe.msgprint(__('Connected. Accessible clients: {0}', [Array.isArray(clients) ? clients.length : '?']));
        }});
    }, __('SimplePay'));
    frm.add_custom_button(__('Refresh Employee Directory'), () => {
      if (frm.is_dirty()) {frappe.msgprint(__('Save SimplePay Settings before refreshing.')); return;}
      frappe.call({method:'is_attendance.controllers.simplepay.refresh_employee_directory',
        callback(r){if(r.message?.queued) frappe.show_alert({message:__('Employee directory refresh queued'),indicator:'green'});}});
    }, __('SimplePay'));
    frm.add_custom_button(__('Fetch Leave Types'), () => {
      const opts=(frm.doc.companies||[]).filter(r=>r.enabled && r.company).map(r=>r.company);
      if(!opts.length){frappe.msgprint(__('Configure an enabled company mapping first.'));return;}
      frappe.prompt([{fieldname:'company',fieldtype:'Select',label:__('Company'),options:opts.join('\n'),reqd:1}], v=>{
        frappe.call({method:'is_attendance.controllers.simplepay.list_leave_types',args:{company:v.company},callback(r){
          const esc=x=>frappe.utils.escape_html(String(x??''));
          const rows=Object.entries(r.message||{}).map(([id,label])=>`<tr><td>${esc(id)}</td><td>${esc(label)}</td></tr>`).join('');
          frappe.msgprint({title:__('SimplePay Leave Types'),wide:true,message:`<table class="table table-bordered"><tbody>${rows}</tbody></table>`});
        }});
      }, __('Choose company'));
    }, __('SimplePay'));
  }
});
