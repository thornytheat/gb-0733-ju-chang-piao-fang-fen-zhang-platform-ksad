"""拒付对账单下载。

对账单是归集快照的只读投影：通知台账、补正台账、逐事件占用、按场馆/场次/票码
汇总与两策略口径说明。导出失败不触碰快照，前端保留上一版结果。
"""

import io
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from app.manifest import canonical, checksum
from app.modules.m7_evidence.csv_out import make_csv

NOTICE_FIELDS = ('notice_id', 'parent_notice_id', 'ticket', 'channel', 'policy', 'requested_cents', 'status',
                 'occurred_at', 'registered_at', 'evidence_due_at', 'note')
EVENT_FIELDS = ('event_id', 'notice_id', 'ticket', 'show', 'venue', 'policy', 'requested_cents', 'applied_cents',
                'recoverable_cents', 'status', 'occurred_at', 'reversal')
REVERSAL_FIELDS = ('reversal_id', 'notice_id', 'reversed_at', 'registered_at', 'note')
GROUP_FIELDS = ('requested_cents', 'applied_cents', 'recoverable_cents', 'event_count')

POLICY_NOTES = {
    'setoff': '拒付先占用后续未结应收：已发出分账不改写，未占用部分计入待渠道追回；'
              '渠道补正后占用回退、待追回归零。',
    'reallocate': '拒付按原权益权重重算原场次份额：生成负向调整、不占用后续未结应收，'
                  '超出原分账限额部分计入待渠道追回；渠道补正后调整红冲。',
}


def statement_tables(snapshot):
    totals = snapshot['totals']
    header = [{
        'run_id': snapshot['run_id'], 'policy': snapshot['policy'], 'policy_note': POLICY_NOTES[snapshot['policy']],
        'generated_at': snapshot['generated_at'], 'notice_count': totals['notice_count'],
        'reversed_count': totals['reversed_count'], 'requested_cents': totals['requested_cents'],
        'applied_cents': totals['applied_cents'], 'recoverable_cents': totals['recoverable_cents'],
        'original_payable_cents': totals['original_payable_cents'],
        'venue_net_receivable_cents': totals['venue_net_receivable_cents'],
        'pending_recovery_cents': totals['pending_recovery_cents'],
        'available_unsettled_cents': totals['available_unsettled_cents'],
    }]
    return {
        'chargeback-notices.csv': (snapshot['notices'], NOTICE_FIELDS),
        'chargeback-reversals.csv': (snapshot['reversals'], REVERSAL_FIELDS),
        'chargeback-events.csv': (snapshot['events'], EVENT_FIELDS),
        'chargeback-by-show.csv': (snapshot['by_show'], ('show', 'venue', *GROUP_FIELDS)),
        'chargeback-by-venue.csv': (snapshot['by_venue'], ('venue', *GROUP_FIELDS)),
        'chargeback-by-ticket.csv': (snapshot['by_ticket'], ('ticket', *GROUP_FIELDS)),
        'chargeback-summary.csv': (header, ('run_id', 'policy', 'policy_note', 'generated_at', 'notice_count',
                                            'reversed_count', 'requested_cents', 'applied_cents',
                                            'recoverable_cents', 'original_payable_cents',
                                            'venue_net_receivable_cents', 'pending_recovery_cents',
                                            'available_unsettled_cents')),
    }


def build_statement(snapshot):
    files = {}
    for name, (rows, fields) in statement_tables(snapshot).items():
        files[name] = make_csv(rows, fields)
    manifest = {**{key: snapshot[key] for key in ('run_id', 'policy', 'generated_at')},
                'totals': snapshot['totals'], 'hashes': {name: checksum(data) for name, data in files.items()}}
    files['manifest.json'] = canonical(manifest)
    stream = io.BytesIO()
    with ZipFile(stream, 'w', compression=ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(ZipInfo(name), data, compress_type=ZIP_DEFLATED)
    return stream.getvalue()
