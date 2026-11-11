frappe.ui.form.on('Leave Application', {
  refresh(frm) {
    if (!frm.doc.employee) return;
    frm.add_custom_button(__('Official Leave Balance'), () => {
      frappe.call({method:'is_attendance.controllers.simplepay.get_leave_balances',args:{employee:frm.doc.employee},callback(r){
        const d=r.message;if(!d)return;
        const esc=x=>frappe.utils.escape_html(String(x??''));
        const rows=(d.balances||[]).map(x=>`<tr><td>${esc(x.leave_type||x.type_id)}</td><td>${esc(x.balance)}</td><td>${esc(x.as_of)}</td></tr>`).join('');
        frappe.msgprint({title:__('SimplePay Official Leave Balances'),wide:true,
          message:`<p>${__('Fetched')}: ${esc(d.fetched_at)}</p><table class="table table-bordered"><thead><tr><th>${__('Leave Type')}</th><th>${__('Balance')}</th><th>${__('As Of')}</th></tr></thead><tbody>${rows}</tbody></table>`});
      }});
    }, __('SimplePay'));
  }
});
