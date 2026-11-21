"""拒付台账存储。

所有登记在 ``BEGIN IMMEDIATE`` 单事务内完成：通知、补正、快照同生共死，
任何一步失败整体回滚，上一版归集快照保持可读（失败时保留旧版结果）。
通知行是追加式的业务事实，补正写入独立的 reversals 表，绝不删除历史通知。
"""

import json
from contextlib import closing

from app.errors import ChargebackError, Problem
from app.manifest import canonical, utc_now
from app.modules.m8_chargebacks.service import build_snapshot, ledger_index, validate_common


class ChargebackStore:
    def __init__(self, store):
        self.store = store

    def _run(self, db, run_id):
        row = db.execute('SELECT payload FROM runs WHERE id = ?', (run_id,)).fetchone()
        if not row:
            raise Problem(404, 'RUN_NOT_FOUND', {'run_id': run_id})
        return json.loads(row[0])

    def _notices(self, db, run_id):
        rows = db.execute('SELECT payload FROM chargeback_notices WHERE run_id = ?', (run_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _reversals(self, db, run_id):
        rows = db.execute('SELECT payload FROM chargeback_reversals WHERE run_id = ?', (run_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _active_policy(self, db, run_id):
        row = db.execute(
            "SELECT n.policy FROM chargeback_notices n WHERE n.run_id = ? AND NOT EXISTS "
            "(SELECT 1 FROM chargeback_reversals r WHERE r.notice_id = n.notice_id) LIMIT 1",
            (run_id,)).fetchone()
        return row[0] if row else None

    def snapshot(self, run_id):
        with closing(self.store.connect()) as db:
            if not db.execute('SELECT 1 FROM runs WHERE id = ?', (run_id,)).fetchone():
                raise Problem(404, 'RUN_NOT_FOUND', {'run_id': run_id})
            row = db.execute('SELECT payload FROM chargeback_snapshots WHERE run_id = ?', (run_id,)).fetchone()
            if not row:
                raise Problem(404, 'CHARGEBACK_EMPTY', {'run_id': run_id})
            return json.loads(row[0])

    def _persist(self, db, run_id, run_snapshot, policy):
        snapshot = build_snapshot(run_id, policy, self._notices(db, run_id), self._reversals(db, run_id),
                                  run_snapshot, utc_now())
        db.execute(
            'INSERT INTO chargeback_snapshots(run_id, policy, generated_at, payload) VALUES (?, ?, ?, ?) '
            'ON CONFLICT(run_id) DO UPDATE SET policy = excluded.policy, '
            'generated_at = excluded.generated_at, payload = excluded.payload',
            (run_id, policy, snapshot['generated_at'], canonical(snapshot)))
        return snapshot

    def register(self, run_id, notice, *, parent_notice_id=None):
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            run_snapshot = self._run(db, run_id)
            ledger = ledger_index(run_snapshot)
            existing = db.execute('SELECT payload FROM chargeback_notices WHERE notice_id = ?',
                                  (notice['notice_id'],)).fetchone()
            if existing:
                prior = json.loads(existing[0])
                fields = ('ticket', 'channel', 'policy', 'requested_cents', 'occurred_at')
                if {key: prior.get(key) for key in fields} == {key: notice.get(key) for key in fields}:
                    return self._snapshot_locked(db, run_id), True
                raise ChargebackError('NOTICE_ID_CONFLICT', {'notice_id': notice['notice_id']})
            validate_common(notice, run_id, ledger, self._active_policy(db, run_id))
            if parent_notice_id:
                parent = self._parent(db, parent_notice_id)
                self._validate_split(db, parent, notice)
            self._insert_notice(db, run_id, notice, parent_notice_id)
            if parent_notice_id:
                self._mark_status(db, parent_notice_id, 'SPLIT')
            return self._persist(db, run_id, run_snapshot, notice['policy']), False

    def _mark_status(self, db, notice_id, status):
        row = db.execute('SELECT payload FROM chargeback_notices WHERE notice_id = ?', (notice_id,)).fetchone()
        record = json.loads(row[0])
        record['status'] = status
        db.execute('UPDATE chargeback_notices SET status = ?, payload = ? WHERE notice_id = ?',
                   (status, canonical(record), notice_id))

    def _parent(self, db, parent_notice_id):
        row = db.execute('SELECT payload FROM chargeback_notices WHERE notice_id = ?',
                         (parent_notice_id,)).fetchone()
        if not row:
            raise ChargebackError('NOTICE_PARENT_UNKNOWN', {'parent_notice_id': parent_notice_id})
        return json.loads(row[0])

    def _validate_split(self, db, parent, child):
        if parent['ticket'] != child['ticket'] or parent['channel'] != child['channel']:
            raise ChargebackError('CHARGEBACK_BAD_SPLIT', {'parent_notice_id': parent['notice_id']})
        children_total = db.execute(
            'SELECT COALESCE(SUM(requested_cents), 0) FROM chargeback_notices WHERE parent_notice_id = ?',
            (parent['notice_id'],)).fetchone()[0]
        if children_total + child['requested_cents'] > parent['requested_cents']:
            raise ChargebackError('NOTICE_PARENT_SPLIT_AMOUNT',
                                  {'parent_cents': parent['requested_cents'], 'children_cents': children_total,
                                   'requested_cents': child['requested_cents']})

    def reverse(self, run_id, notice_id, reversal_id, reversed_at, note):
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            run_snapshot = self._run(db, run_id)
            target = db.execute('SELECT payload FROM chargeback_notices WHERE notice_id = ? AND run_id = ?',
                                (notice_id, run_id)).fetchone()
            if not target:
                raise ChargebackError('NOTICE_REVERSAL_UNKNOWN', {'notice_id': notice_id})
            original = json.loads(target[0])
            if reversed_at < original['occurred_at']:
                raise ChargebackError('CHARGEBACK_TIME_INVALID',
                                      {'notice_id': notice_id, 'occurred_at': original['occurred_at'],
                                       'reversed_at': reversed_at})
            if db.execute('SELECT 1 FROM chargeback_reversals WHERE notice_id = ?', (notice_id,)).fetchone():
                raise ChargebackError('NOTICE_REVERSAL_DUPLICATE', {'notice_id': notice_id})
            # 已拆分的父单：所有子单先补正后才能补正父单（父单本身已无独立占用）
            children = db.execute('SELECT notice_id FROM chargeback_notices WHERE parent_notice_id = ?',
                                  (notice_id,)).fetchall()
            if children:
                pending = [cid for (cid,) in children if not db.execute(
                    'SELECT 1 FROM chargeback_reversals WHERE notice_id = ?', (cid,)).fetchone()]
                if pending:
                    raise ChargebackError('NOTICE_PARENT_ALREADY_SPLIT',
                                          {'parent_notice_id': notice_id, 'open_children': pending})
            record = {'reversal_id': reversal_id, 'notice_id': notice_id, 'run_id': run_id,
                      'reversed_at': reversed_at, 'registered_at': utc_now(), 'note': note[:200]}
            db.execute(
                'INSERT INTO chargeback_reversals(reversal_id, notice_id, run_id, reversed_at, '
                'registered_at, note, payload) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (reversal_id, notice_id, run_id, reversed_at, record['registered_at'], record['note'],
                 canonical(record)))
            self._mark_status(db, notice_id, 'REVERSED')
            return self._persist(db, run_id, run_snapshot, original['policy']), False

    def _insert_notice(self, db, run_id, notice, parent_notice_id):
        record = {**notice, 'run_id': run_id, 'book': self._book(db, run_id),
                  'parent_notice_id': parent_notice_id, 'status': 'OPEN', 'registered_at': utc_now()}
        db.execute(
            'INSERT INTO chargeback_notices(notice_id, parent_notice_id, run_id, book, ticket, channel, policy, '
            'requested_cents, status, occurred_at, registered_at, evidence_due_at, note, payload) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (record['notice_id'], parent_notice_id, run_id, record['book'], record['ticket'], record['channel'],
             record['policy'], record['requested_cents'], 'OPEN', record['occurred_at'], record['registered_at'],
             record['evidence_due_at'], record['note'], canonical(record)))

    def _book(self, db, run_id):
        return db.execute('SELECT book FROM runs WHERE id = ?', (run_id,)).fetchone()[0]

    def _snapshot_locked(self, db, run_id):
        row = db.execute('SELECT payload FROM chargeback_snapshots WHERE run_id = ?', (run_id,)).fetchone()
        if not row:  # 幂等重试发生在首单事务内不会走到这里；防御性保留
            raise ChargebackError('CHARGEBACK_SNAPSHOT_STALE', {'run_id': run_id})
        return json.loads(row[0])
