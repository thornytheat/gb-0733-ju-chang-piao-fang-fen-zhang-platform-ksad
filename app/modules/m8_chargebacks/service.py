"""拒付归集的重放引擎与登记校验。

事件表是追加式的归集台账：同一 run 的全部拒付按业务时刻、单号顺序重放，
setoff 池逐单扣减、补正逐单回退，因此重试、拆分与补正都不会重复扣款。
每次写入后整器重放生成快照，写入失败时旧快照原样保留。
"""

from collections import Counter, defaultdict

from app.contracts import cents, key, text, timestamp
from app.errors import ChargebackError
from app.modules.m8_chargebacks.rules import reallocation_allocations, setoff_allocations

POLICIES = ('setoff', 'reallocate')


UNASSIGNED_SHOW = 'UNASSIGNED'


def ledger_index(snapshot):
    """从已发布批次的结算账构建票码索引（拒付只引用批次内真实数据）。"""
    tickets = {row['ticket']: row for row in snapshot['funding']['tickets']}
    by_ticket = defaultdict(dict)
    for row in snapshot['funding']['ledger']:
        by_ticket[row['ticket']][row['show']] = row
    # 未分配票款也是后续未结应收的一部分，作为虚拟场次行纳入 setoff 池。
    for row in snapshot['publication']['unassigned']:
        by_ticket[row['ticket']].setdefault(UNASSIGNED_SHOW, {
            'ticket': row['ticket'], 'show': UNASSIGNED_SHOW, 'venue': '', 'weight': 0,
            'allocated_cents': 0, 'unsettled_cents': row['unsettled_cents']})
    return tickets, by_ticket


def parse_notice(values):
    """登记表单 → 规范化通知字段。"""
    return {
        'notice_id': key(values['notice_id']),
        'ticket': key(values['ticket']),
        'channel': text(values['channel']),
        'policy': text(values['policy']),
        'occurred_at': timestamp(values['occurred_at']),
        'evidence_due_at': timestamp(values['evidence_due_at']),
        'note': values.get('note', '')[:200],
        'requested_cents': cents(values['amount'], values['unit']),
    }


def validate_common(notice, run_id, ledger, run_policy):
    if notice['policy'] not in POLICIES:
        raise ChargebackError('CHARGEBACK_POLICY_UNKNOWN', {'policy': notice['policy'], 'allowed': list(POLICIES)})
    if run_policy is not None and notice['policy'] != run_policy:
        raise ChargebackError('CHARGEBACK_POLICY_MIXED', {'expected': run_policy, 'policy': notice['policy']})
    ticket = notice['ticket']
    tickets, _ = ledger
    if ticket not in tickets:
        raise ChargebackError('CHARGEBACK_TICKET_UNKNOWN', {'ticket': ticket, 'run_id': run_id})
    if not 0 < notice['requested_cents'] <= tickets[ticket]['net_cents']:
        raise ChargebackError('CHARGEBACK_AMOUNT_INVALID',
                              {'requested_cents': notice['requested_cents'],
                               'net_cents': tickets[ticket]['net_cents']})
    return notice


def _event(notice_id, seq, ticket, show, venue, policy, requested, applied, recoverable,
           status, occurred_at, reversal=False):
    # 符号约定：归集占用与待追回为负（减少净应收/挂渠道账），补正回退为正。
    sign = 1 if reversal else -1
    return {
        'event_id': f'{notice_id}:{show or "RECOVERY"}:{seq}', 'notice_id': notice_id, 'seq': seq,
        'ticket': ticket, 'show': show, 'venue': venue, 'policy': policy,
        'requested_cents': requested, 'applied_cents': sign * applied,
        'recoverable_cents': sign * recoverable, 'status': 'REVERSED' if reversal else status,
        'occurred_at': occurred_at, 'reversal': reversal,
    }


def _status(applied_total, recoverable):
    if applied_total == 0:
        return 'PENDING_RECOVERY'
    return 'PARTIAL' if recoverable else 'FULLY_SET_OFF'


def replay(notices, reversals, ledger):
    """把追加式通知台账与补正台账重放为事件台账。"""
    _, by_ticket = ledger
    pools, applied_by_notice = defaultdict(Counter), {}
    recovery_by_notice = {}
    events = []
    reversal_by_notice = {row['notice_id']: row for row in reversals}
    # 父单身份由是否存在子单决定（补正后状态会从 SPLIT 变为 REVERSED）：
    # 已拆分父单退出归集，占用完全由子单表达；通知行本身保留可审计。
    split_parents = {item['parent_notice_id'] for item in notices if item.get('parent_notice_id')}
    ordered = sorted((item for item in notices if item['notice_id'] not in split_parents),
                     key=lambda item: (item['occurred_at'], item['notice_id']))

    for notice in ordered:
        ticket, policy = notice['ticket'], notice['policy']
        rows = by_ticket.get(ticket, {})
        venues = {show: row['venue'] for show, row in rows.items()}
        if policy == 'setoff':
            pool = {show: row['unsettled_cents'] - pools[ticket][show]
                    for show, row in rows.items() if row['unsettled_cents'] - pools[ticket][show] > 0}
            allocations, recoverable = setoff_allocations(notice['requested_cents'], pool)
            for show, amount in allocations.items():
                pools[ticket][show] += amount
        else:
            original = {show: row for show, row in rows.items() if row['allocated_cents'] > 0}
            allocations, recoverable = reallocation_allocations(notice['requested_cents'], original)
        applied_by_notice[notice['notice_id']] = allocations
        recovery_by_notice[notice['notice_id']] = recoverable
        status = _status(sum(allocations.values()), recoverable)
        seq = len(events)
        for show in sorted(allocations):
            events.append(_event(notice['notice_id'], seq, ticket, show, venues[show], policy,
                                 notice['requested_cents'], allocations[show], 0, status, notice['occurred_at']))
            seq += 1
        if recoverable or not allocations:
            events.append(_event(notice['notice_id'], seq, ticket, '', next(iter(venues.values()), ''), policy,
                                 notice['requested_cents'], 0, recoverable, status, notice['occurred_at']))

    for notice in ordered:
        reversal = reversal_by_notice.get(notice['notice_id'])
        if not reversal:
            continue
        ticket, policy = notice['ticket'], notice['policy']
        rows = by_ticket.get(ticket, {})
        venues = {show: row['venue'] for show, row in rows.items()}
        allocations = applied_by_notice[notice['notice_id']]
        recoverable = recovery_by_notice[notice['notice_id']]
        if policy == 'setoff':  # 占用回退到未结应收池
            for show, amount in allocations.items():
                pools[ticket][show] -= amount
        seq = len(events)
        for show in sorted(allocations):
            events.append(_event(notice['notice_id'], seq, ticket, show, venues[show], policy,
                                 notice['requested_cents'], allocations[show], 0, 'REVERSED',
                                 reversal['reversed_at'], reversal=True))
            seq += 1
        if recoverable or not allocations:
            events.append(_event(notice['notice_id'], seq, ticket, '', next(iter(venues.values()), ''), policy,
                                 notice['requested_cents'], 0, recoverable, 'REVERSED',
                                 reversal['reversed_at'], reversal=True))
    # 已拆分父单不产生归集事件，其补正仅保留通知与补正留痕，不生成红冲事件。
    events = [row for row in events if row['notice_id'] not in split_parents]
    return events


def build_snapshot(run_id, policy, notices, reversals, run_snapshot, generated_at):
    ledger = ledger_index(run_snapshot)
    events = replay(notices, reversals, ledger)
    totals = summarize(run_snapshot, events)
    return {'run_id': run_id, 'policy': policy, 'generated_at': generated_at,
            'notices': sorted(notices, key=lambda item: (item['occurred_at'], item['notice_id'])),
            'reversals': reversals, 'events': events, 'totals': totals,
            'by_show': group_by(events, 'show'), 'by_venue': group_by(events, 'venue'),
            'by_ticket': group_by(events, 'ticket')}


def _sum(rows, field):
    return sum(row[field] for row in rows)


def group_by(events, field):
    groups = defaultdict(list)
    for row in events:
        groups[row[field] or '（待追回）'].append(row)
    result = []
    for value in sorted(groups):
        rows = groups[value]
        # requested 按通知号只计一次（同一拒付拆出的多事件不重复累计），
        # applied/recoverable 按事件带符号求和。
        seen_notices = set()
        requested = 0
        for row in rows:
            if row['reversal'] or row['notice_id'] in seen_notices:
                continue
            seen_notices.add(row['notice_id'])
            requested += row['requested_cents']
        result.append({
            field: value,
            'requested_cents': requested,
            'applied_cents': _sum(rows, 'applied_cents'),
            'recoverable_cents': _sum(rows, 'recoverable_cents'),
            'event_count': len(rows),
            'venue': next((row['venue'] for row in rows if row['venue']), ''),
            'ticket': next((row['ticket'] for row in rows if row['ticket']), ''),
        })
    return result


def summarize(run_snapshot, events):
    active = [row for row in events if not row['reversal']]
    reversed_events = [row for row in events if row['reversal']]
    base = run_snapshot['funding']['totals']
    open_notices = {row['notice_id'] for row in active}
    reversed_notices = {row['notice_id'] for row in reversed_events}
    # applied_cents / recoverable_cents 已带符号：归集为负、补正为正，直接求和即净额。
    net_applied = _sum(events, 'applied_cents')
    net_recoverable = _sum(events, 'recoverable_cents')
    # setoff 才占用后续未结应收；reallocate 只重算原场次份额，未结池保持不变。
    setoff_applied = sum(row['applied_cents'] for row in events if row['policy'] == 'setoff')
    requested_notices = {row['notice_id']: row['requested_cents'] for row in active}
    # reversed_cents 是补正释放的占用绝对值，作为运营指标恒为非负。
    reversed_cents = sum(max(row['applied_cents'], 0) for row in reversed_events)
    return {
        'notice_count': len(open_notices),
        'reversed_count': len(reversed_notices),
        'event_count': len(events),
        'requested_cents': sum(requested_notices.values()),
        'applied_cents': net_applied,
        'recoverable_cents': net_recoverable,
        'reversed_cents': reversed_cents,
        'original_payable_cents': base['payable_cents'],
        'original_unsettled_cents': base['unsettled_cents'],
        'venue_net_receivable_cents': base['payable_cents'] + net_applied,
        'pending_recovery_cents': net_recoverable,
        'available_unsettled_cents': base['unsettled_cents'] + setoff_applied,
    }
