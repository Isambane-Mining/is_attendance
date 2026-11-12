const SIMPLEPAY_PAYROLL_BALANCE_FIELD = 'ir_leave_as_per_payroll';

function simplepay_application_signature(frm) {
    return [frm.doc.employee || '', frm.doc.leave_type || '', frm.doc.from_date || ''].join('|');
}

async function refresh_simplepay_application_balance(frm) {
    if (frm.doc.docstatus !== 0) return;

    const signature = simplepay_application_signature(frm);
    frm.__simplepay_balance_request = (frm.__simplepay_balance_request || 0) + 1;
    const request = frm.__simplepay_balance_request;

    if (!frm.doc.employee) {
        frm.set_df_property(SIMPLEPAY_PAYROLL_BALANCE_FIELD, 'read_only', 0);
        return;
    }

    try {
        const result = await frappe.call({
            method: 'is_attendance.controllers.simplepay_balances.get_application_payroll_balance',
            args: {
                employee: frm.doc.employee,
                leave_type: frm.doc.leave_type || '',
                as_of: frm.doc.from_date || '',
            },
        });

        // Discard responses for a previous employee, leave type or start date.
        if (request !== frm.__simplepay_balance_request ||
            signature !== simplepay_application_signature(frm)) return;

        const data = result.message || {};
        frm.set_df_property(SIMPLEPAY_PAYROLL_BALANCE_FIELD, 'read_only', data.mapped ? 1 : 0);
        if (!data.mapped) {
            frm.__simplepay_balance_mode = 'manual';
            return;  // Never overwrite manual payroll figures.
        }

        frm.__simplepay_balance_mode = 'simplepay';
        await frm.set_value(SIMPLEPAY_PAYROLL_BALANCE_FIELD,
            data.balance === null || data.balance === undefined ? null : data.balance);
        frappe.show_alert({
            message: data.balance === null || data.balance === undefined
                ? __('SimplePay employee: select a mapped Leave Type for the official balance.')
                : __('SimplePay payroll balance updated: {0} days (as of {1})', [data.balance, data.as_of]),
            indicator: 'blue',
        });
    } catch (error) {
        if (request !== frm.__simplepay_balance_request ||
            signature !== simplepay_application_signature(frm)) return;
        // Fail visibly for mapped or mapping-related errors; do not use a stale figure.
        frm.__simplepay_balance_mode = 'error';
        frm.set_df_property(SIMPLEPAY_PAYROLL_BALANCE_FIELD, 'read_only', 1);
        await frm.set_value(SIMPLEPAY_PAYROLL_BALANCE_FIELD, null);
        frappe.show_alert({
            message: __('Could not retrieve the SimplePay payroll balance. Saving a mapped application will be blocked until the issue is resolved.'),
            indicator: 'red',
        }, 7);
    }
}

frappe.ui.form.on('Leave Application', {
    refresh(frm) {
        if (frm.doc.docstatus === 0 && frm.doc.employee) {
            refresh_simplepay_application_balance(frm);
        }
        if (!frm.doc.employee) return;

        frm.add_custom_button(__('Official Leave Balance'), () => {
            frappe.call({
                method: 'is_attendance.controllers.simplepay.get_leave_balances',
                args: {employee: frm.doc.employee},
                callback(r) {
                    const d = r.message;
                    if (!d) return;
                    const esc = x => frappe.utils.escape_html(String(x ?? ''));
                    const rows = (d.balances || []).map(x =>
                        `<tr><td>${esc(x.leave_type || x.type_id)}</td><td>${esc(x.balance)}</td><td>${esc(x.as_of)}</td></tr>`
                    ).join('');
                    frappe.msgprint({
                        title: __('SimplePay Official Leave Balances'),
                        wide: true,
                        message: `<p>${__('Fetched')}: ${esc(d.fetched_at)}</p>` +
                            `<table class="table table-bordered"><thead><tr>` +
                            `<th>${__('Leave Type')}</th><th>${__('Balance')}</th><th>${__('As Of')}</th>` +
                            `</tr></thead><tbody>${rows}</tbody></table>`,
                    });
                },
            });
        }, __('SimplePay'));
    },
    employee(frm) {
        refresh_simplepay_application_balance(frm);
    },
    leave_type(frm) {
        refresh_simplepay_application_balance(frm);
    },
    from_date(frm) {
        refresh_simplepay_application_balance(frm);
    },
});
