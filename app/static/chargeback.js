const panel = document.querySelector('#chargeback-panel');
if (panel) {
  const run = panel.dataset.run;
  const $ = selector => panel.querySelector(selector);
  const numberFormat = new Intl.NumberFormat('zh-CN');
  const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'});
  const money = cents => {
    const digits = Math.abs(cents || 0);
    return `${(cents || 0) < 0 ? '−' : ''}${numberFormat.format(Math.floor(digits / 100))}.${String(digits % 100).padStart(2, '0')}`;
  };
  const policyLabel = {setoff: '先占用后续未结应收', reallocate: '重算原场次份额'};
  const statusLabel = {
    OPEN: '待处理', PARTIAL: '部分占用', FULLY_SET_OFF: '已全额占用',
    PENDING_RECOVERY: '待渠道追回', SPLIT: '已拆分', REVERSED: '已补正'};

  const columns = {
    notices: [
      ['notice_id', '拒付单号'], ['parent_notice_id', '拆分来源'], ['ticket', '票码'], ['channel', '渠道'],
      ['requested_cents', '申请额 ¥', 'money'], ['status', '状态', 'status'],
      ['occurred_at', '拒付时间', 'time'], ['evidence_due_at', '举证截止', 'time'], ['note', '备注']],
    events: [
      ['notice_id', '拒付单号'], ['ticket', '票码'], ['show', '场次'], ['venue', '场馆'],
      ['requested_cents', '申请额 ¥', 'money'], ['applied_cents', '占用 ¥', 'money'],
      ['recoverable_cents', '待追回 ¥', 'money'], ['status', '状态', 'status'],
      ['occurred_at', '入账时间', 'time'], ['reversal', '红冲', 'flag']],
    venues: [['venue', '场馆'], ['event_count', '事件数', 'number'], ['requested_cents', '申请额 ¥', 'money'],
      ['applied_cents', '占用 ¥', 'money'], ['recoverable_cents', '待追回 ¥', 'money']],
    shows: [['show', '场次'], ['venue', '场馆'], ['event_count', '事件数', 'number'],
      ['requested_cents', '申请额 ¥', 'money'], ['applied_cents', '占用 ¥', 'money'],
      ['recoverable_cents', '待追回 ¥', 'money']],
    tickets: [['ticket', '票码'], ['event_count', '事件数', 'number'], ['requested_cents', '申请额 ¥', 'money'],
      ['applied_cents', '占用 ¥', 'money'], ['recoverable_cents', '待追回 ¥', 'money']],
    reversals: [['reversal_id', '补正单号'], ['notice_id', '原拒付单号'], ['reversed_at', '补正时间', 'time'],
      ['registered_at', '登记时间', 'time'], ['note', '备注']],
  };
  let view = 'notices';
  let request;

  function formatCell(row, key, type) {
    const value = row[key];
    if (type === 'money') return money(value);
    if (type === 'time') return value ? dateFormatter.format(new Date(value)) : '—';
    if (type === 'status') return statusLabel[value] || value || '—';
    if (type === 'flag') return value ? '红冲' : '归集';
    if (type === 'number') return numberFormat.format(value || 0);
    return value === null || value === undefined || value === '' ? '—' : String(value);
  }

  function renderTable(rows) {
    const head = $('#cb-columns');
    head.replaceChildren(...columns[view].map(([key, label, type]) => {
      const th = document.createElement('th');
      th.scope = 'col';
      th.textContent = label;
      if (type === 'money' || type === 'number') th.className = 'num';
      return th;
    }));
    const body = $('#cb-rows');
    body.replaceChildren();
    for (const row of rows) {
      const tr = body.insertRow();
      for (const [key, , type] of columns[view]) {
        const td = tr.insertCell();
        td.textContent = formatCell(row, key, type);
        if (type === 'money' || type === 'number') td.className = 'num';
      }
    }
    $('#cb-empty').hidden = rows.length > 0;
  }

  function renderTotals(data) {
    const totals = data.totals || {};
    for (const [key, node] of Object.entries({
      requested_cents: $('[data-cb-total="requested_cents"]'),
      applied_cents: $('[data-cb-total="applied_cents"]'),
      venue_net_receivable_cents: $('[data-cb-total="venue_net_receivable_cents"]'),
      pending_recovery_cents: $('[data-cb-total="pending_recovery_cents"]'),
      reversed_cents: $('[data-cb-total="reversed_cents"]'),
      available_unsettled_cents: $('[data-cb-total="available_unsettled_cents"]')})) {
      node.textContent = key in totals ? `¥${money(totals[key])}` : '—';
    }
    $('[data-cb-meta="notice_count"]').textContent = totals.notice_count ?? 0;
    $('[data-cb-meta="reversed_count"]').textContent = totals.reversed_count ?? 0;
    $('[data-cb-policy]').textContent = data.policy ? policyLabel[data.policy] || data.policy : '—';
    const generated = $('[data-cb-generated]');
    if (data.generated_at) {
      generated.dateTime = data.generated_at;
      generated.textContent = dateFormatter.format(new Date(data.generated_at));
    }
  }

  async function refresh() {
    request?.abort();
    const controller = new AbortController();
    request = controller;
    const q = $('#cb-q').value.trim();
    $('#cb-status').textContent = '读取中…';
    try {
      const query = new URLSearchParams({view, q, limit: 100});
      const response = await fetch(`/api/chargebacks/${encodeURIComponent(run)}?${query}`,
        {signal: controller.signal});
      if (!response.ok) throw new Error('读取失败');
      const data = await response.json();
      if (request !== controller) return;
      if (data.empty) {
        renderTotals({totals: {}});
        renderTable([]);
        $('#cb-status').textContent = '该批次暂无拒付';
        return;
      }
      renderTotals(data);
      renderTable(data.items);
      $('#cb-status').textContent = `${data.total} 行`;
    } catch (error) {
      if (error.name === 'AbortError' || request !== controller) return;
      // 失败时保留上一版表格结果，只更新状态提示。
      $('#cb-status').textContent = '读取失败，已保留上一版结果';
    }
  }

  panel.querySelectorAll('[data-cb-view]').forEach(button => {
    button.addEventListener('click', () => {
      view = button.dataset.cbView;
      panel.querySelectorAll('[data-cb-view]').forEach(item =>
        item.setAttribute('aria-pressed', String(item === button)));
      refresh();
    });
  });
  $('#chargeback-filters').addEventListener('submit', event => {
    event.preventDefault();
    refresh();
  });
  refresh();
}
