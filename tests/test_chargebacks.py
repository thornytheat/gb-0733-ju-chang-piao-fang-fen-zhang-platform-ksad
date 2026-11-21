"""M8 拒付归集：策略口径、幂等、拆分、补正与对账单。"""

import csv
import io
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.modules.m8_chargebacks.rules import reallocation_allocations, setoff_allocations
from app.modules.m8_chargebacks.service import build_snapshot, ledger_index
from app.pipeline import compute
from app.sources import sample_sources


def notice(notice_id, ticket, policy='setoff', amount='19.99', *, channel='wechat-pay',
           occurred='2026-11-20T10:00:00+08:00', note=''):
    return {'notice_id': notice_id, 'ticket': ticket, 'channel': channel, 'policy': policy,
            'occurred_at': occurred, 'evidence_due_at': '2026-11-25T10:00:00+08:00',
            'amount': amount, 'unit': '元', 'note': note}


@pytest.fixture(scope='module')
def sample_run():
    return compute('tour-demo', 'fixture', sample_sources())


@pytest.fixture
def registered(client):
    run_id = client.post('/api/runs/sample').json()['run_id']
    return client, run_id


# ---------- 纯规则 ----------

def test_setoff_consumes_pool_in_show_key_order_and_leaves_recovery():
    allocated, recoverable = setoff_allocations(5000, {'C-DUSK': 1000, 'A-LAKE': 3000})
    assert allocated == {'A-LAKE': 3000, 'C-DUSK': 1000}
    assert recoverable == 1000


def test_reallocate_follows_original_weights_and_caps_at_original_share():
    original = {'A-LAKE': {'weight': 1, 'allocated_cents': 100},
                'B-RAIN': {'weight': 3, 'allocated_cents': 9999}}
    allocated, recoverable = reallocation_allocations(1000, original)
    assert sum(allocated.values()) + recoverable == 1000
    assert allocated == {'A-LAKE': 100, 'B-RAIN': 750}  # A 场被原分账限额截断
    assert recoverable == 150


def test_reallocate_uses_largest_remainder_when_equal_weights(sample_run):
    ledger = ledger_index(sample_run)
    _, rows = ledger
    original = {show: {'weight': row['weight'], 'allocated_cents': row['allocated_cents']}
                for show, row in rows['00107'].items()}
    allocated, recoverable = reallocation_allocations(1000, original)
    assert recoverable == 0
    assert sum(allocated.values()) == 1000
    assert allocated == {'A-LAKE': 334, 'B-RAIN': 333, 'C-DUSK': 333}


# ---------- setoff 口径 ----------

def test_setoff_occupies_future_unsettled_and_keeps_sent_payable_untouched(registered):
    client, run_id = registered
    # 00108 净收款 4001 全部未结（A-LAKE 2000 + C-DUSK 2001）
    reply = client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-1', '00108', amount='40.01'))
    totals = reply.json()['totals']
    assert totals['applied_cents'] == -4001
    assert totals['recoverable_cents'] == 0
    assert totals['original_payable_cents'] == 135546
    assert totals['venue_net_receivable_cents'] == 135546 - 4001
    assert totals['available_unsettled_cents'] == 16617 - 4001
    events = client.get(f'/api/chargebacks/{run_id}', params={'view': 'events'}).json()['items']
    assert [(e['show'], e['applied_cents']) for e in events] == [('A-LAKE', -2000), ('C-DUSK', -2001)]


def test_setoff_without_available_unsettled_hangs_recovery_on_channel(registered):
    client, run_id = registered
    # 00107 的净收款已全部核销应付，没有后续未结可占用
    reply = client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-2', '00107', amount='10.00'))
    totals = reply.json()['totals']
    assert totals['applied_cents'] == 0
    assert totals['recoverable_cents'] == -1000
    assert totals['venue_net_receivable_cents'] == 135546  # 已发出分账未被改写
    assert totals['pending_recovery_cents'] == -1000


def test_setoff_partial_occupancy_marks_partial_status(registered):
    client, run_id = registered
    # 00111 净收款 8888、未结 6666、已应付 2222：申请 8000 只能占用 6666，1334 挂待追回。
    reply = client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-3', '00111', amount='80.00'))
    totals = reply.json()['totals']
    assert totals['applied_cents'] == -6666 and totals['recoverable_cents'] == -1334
    event = client.get(f'/api/chargebacks/{run_id}', params={'view': 'events', 'q': 'CB-3'}).json()['items']
    assert {row['status'] for row in event} == {'PARTIAL'}
    # 已发出应付 2222 不被改写
    assert totals['venue_net_receivable_cents'] == 135546 - 6666


def test_setoff_pool_is_shared_across_notices_without_double_spend(registered):
    client, run_id = registered
    client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-A', '00108', amount='30.00'))
    second = client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-B', '00108', amount='30.00'))
    totals = second.json()['totals']
    # 池总额 4001：首单占 3000，次单只剩 1001，其余 1999 挂待追回
    assert totals['applied_cents'] == -4001 and totals['recoverable_cents'] == -1999


# ---------- reallocate 口径 ----------

def test_reallocate_adjusts_original_show_shares_not_future_unsettled(registered):
    client, run_id = registered
    reply = client.post(f'/api/chargebacks/{run_id}/notices',
                        json=notice('CB-R', '00107', 'reallocate', amount='10.00'))
    totals = reply.json()['totals']
    assert totals['applied_cents'] == -1000
    assert totals['recoverable_cents'] == 0
    assert totals['venue_net_receivable_cents'] == 135546 - 1000
    # 重算原场次份额不触碰后续未结应收池
    assert totals['available_unsettled_cents'] == 16617
    events = client.get(f'/api/chargebacks/{run_id}', params={'view': 'events'}).json()['items']
    assert [(e['show'], e['applied_cents']) for e in events] == [
        ('A-LAKE', -334), ('B-RAIN', -333), ('C-DUSK', -333)]


def test_two_policies_yield_different_net_receivable_and_recovery_balances():
    """同一拒付在两种策略下场馆净应收与待追回余额不同（口径并存可核对）。"""
    snapshot = compute('tour-demo', 'cmp', sample_sources())
    outcomes = {}
    for policy in ('setoff', 'reallocate'):
        notices = [{'notice_id': 'N1', 'ticket': '00107', 'policy': policy,
                    'requested_cents': 1000, 'occurred_at': '2026-11-20T02:00:00+00:00', 'status': 'OPEN'}]
        view = build_snapshot('cmp', policy, notices, [], snapshot, '2026-11-20T03:00:00+00:00')
        outcomes[policy] = view['totals']
    assert outcomes['setoff']['applied_cents'] == 0
    assert outcomes['setoff']['recoverable_cents'] == -1000
    assert outcomes['reallocate']['applied_cents'] == -1000
    assert outcomes['reallocate']['recoverable_cents'] == 0
    assert outcomes['setoff']['venue_net_receivable_cents'] != outcomes['reallocate']['venue_net_receivable_cents']


# ---------- 幂等与校验 ----------

def test_retry_same_notice_returns_same_snapshot_and_deducts_once(registered):
    client, run_id = registered
    payload = notice('CB-IDEM', '00109', amount='19.99')
    first = client.post(f'/api/chargebacks/{run_id}/notices', json=payload)
    retry = client.post(f'/api/chargebacks/{run_id}/notices', json=payload)
    assert first.status_code == 200 and retry.json()['reused'] is True
    totals = retry.json()['totals']
    assert totals['notice_count'] == 1 and totals['applied_cents'] == -1999
    notices = client.get(f'/api/chargebacks/{run_id}', params={'view': 'notices'}).json()
    assert notices['total'] == 1


def test_same_notice_id_with_changed_content_conflicts(registered):
    client, run_id = registered
    client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-C', '00109', amount='19.99'))
    conflict = client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-C', '00109', amount='1.00'))
    assert (conflict.status_code, conflict.json()['code']) == (409, 'NOTICE_ID_CONFLICT')


def test_policy_cannot_mix_within_one_run(registered):
    client, run_id = registered
    client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-P1', '00109', 'setoff'))
    mixed = client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-P2', '00109', 'reallocate'))
    assert (mixed.status_code, mixed.json()['code']) == (409, 'CHARGEBACK_POLICY_MIXED')


def test_unknown_ticket_and_amount_over_net_are_rejected(registered):
    client, run_id = registered
    bad_ticket = client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-X', 'NO-SUCH'))
    assert (bad_ticket.status_code, bad_ticket.json()['code']) == (422, 'CHARGEBACK_TICKET_UNKNOWN')
    too_big = client.post(f'/api/chargebacks/{run_id}/notices',
                          json=notice('CB-Y', '00109', amount='99999.00'))
    assert (too_big.status_code, too_big.json()['code']) == (422, 'CHARGEBACK_AMOUNT_INVALID')


def test_concurrent_identical_registrations_commit_one_notice(tmp_path):
    from app.main import create_app
    from fastapi.testclient import TestClient
    path = tmp_path / 'parallel.sqlite3'
    app = create_app(path)
    with TestClient(app) as client:
        run_id = client.post('/api/runs/sample').json()['run_id']
        payload = notice('CB-PAR', '00109')

        def call(_):
            with TestClient(app) as other:
                reply = other.post(f'/api/chargebacks/{run_id}/notices', json=payload)
                return reply.json().get('reused')
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(call, range(6)))
        assert results.count(False) == 1 and results.count(True) == 5
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM chargeback_notices').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM chargeback_snapshots').fetchone()[0] == 1


# ---------- 拆分 ----------

def test_split_replaces_parent_occupy_without_double_deduction(registered):
    client, run_id = registered
    client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-P', '00108', amount='40.01'))
    s1 = client.post(f'/api/chargebacks/{run_id}/notices?parent_notice_id=CB-P',
                     json=notice('CB-S1', '00108', amount='20.00'))
    s2 = client.post(f'/api/chargebacks/{run_id}/notices?parent_notice_id=CB-P',
                     json=notice('CB-S2', '00108', amount='20.01'))
    assert s1.status_code == 200 and s2.status_code == 200
    totals = s2.json()['totals']
    # 父单退出归集，两子单合计仍只占用 4001
    assert totals['applied_cents'] == -4001 and totals['notice_count'] == 2
    statuses = {row['notice_id']: row['status']
                for row in client.get(f'/api/chargebacks/{run_id}', params={'view': 'notices'}).json()['items']}
    assert statuses == {'CB-P': 'SPLIT', 'CB-S1': 'OPEN', 'CB-S2': 'OPEN'}


def test_split_amount_cannot_exceed_parent_and_retry_is_idempotent(registered):
    client, run_id = registered
    client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-P', '00108', amount='40.01'))
    client.post(f'/api/chargebacks/{run_id}/notices?parent_notice_id=CB-P',
                json=notice('CB-S1', '00108', amount='20.00'))
    over = client.post(f'/api/chargebacks/{run_id}/notices?parent_notice_id=CB-P',
                       json=notice('CB-S2', '00108', amount='21.00'))
    assert (over.status_code, over.json()['code']) == (409, 'NOTICE_PARENT_SPLIT_AMOUNT')
    retry = client.post(f'/api/chargebacks/{run_id}/notices?parent_notice_id=CB-P',
                        json=notice('CB-S1', '00108', amount='20.00'))
    assert retry.json()['reused'] is True


def test_split_with_unknown_parent_and_mismatched_ticket_are_rejected(registered):
    client, run_id = registered
    missing = client.post(f'/api/chargebacks/{run_id}/notices?parent_notice_id=GHOST',
                          json=notice('CB-S', '00109'))
    assert (missing.status_code, missing.json()['code']) == (422, 'NOTICE_PARENT_UNKNOWN')
    client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-P', '00108', amount='40.01'))
    mismatched = client.post(f'/api/chargebacks/{run_id}/notices?parent_notice_id=CB-P',
                             json=notice('CB-S', '00109'))
    assert (mismatched.status_code, mismatched.json()['code']) == (422, 'CHARGEBACK_BAD_SPLIT')


# ---------- 补正 ----------

def test_reversal_releases_occupancy_but_keeps_all_records(registered):
    client, run_id = registered
    client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-1', '00109', amount='19.99'))
    reply = client.post(f'/api/chargebacks/{run_id}/notices/CB-1/reversal',
                        json={'reversal_id': 'RV-1', 'reversed_at': '2026-11-22T10:00:00+08:00',
                              'note': '渠道补齐证据'})
    totals = reply.json()['totals']
    assert totals['applied_cents'] == 0 and totals['recoverable_cents'] == 0
    assert totals['reversed_cents'] == 1999
    assert totals['venue_net_receivable_cents'] == 135546
    assert totals['available_unsettled_cents'] == 16617
    notices = client.get(f'/api/chargebacks/{run_id}', params={'view': 'notices'}).json()['items']
    assert any(row['notice_id'] == 'CB-1' and row['status'] == 'REVERSED' for row in notices)
    reversals = client.get(f'/api/chargebacks/{run_id}', params={'view': 'reversals'}).json()['items']
    assert [row['reversal_id'] for row in reversals] == ['RV-1']


def test_duplicate_and_early_reversal_are_rejected(registered):
    client, run_id = registered
    client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-1', '00109'))
    body = {'reversal_id': 'RV-1', 'reversed_at': '2026-11-22T10:00:00+08:00'}
    client.post(f'/api/chargebacks/{run_id}/notices/CB-1/reversal', json=body)
    duplicate = client.post(f'/api/chargebacks/{run_id}/notices/CB-1/reversal', json=body)
    assert (duplicate.status_code, duplicate.json()['code']) == (422, 'NOTICE_REVERSAL_DUPLICATE')
    early = client.post(f'/api/chargebacks/{run_id}/notices/CB-1/reversal',
                        json={'reversal_id': 'RV-2', 'reversed_at': '2026-11-01T00:00:00+08:00'})
    assert (early.status_code, early.json()['code']) == (422, 'CHARGEBACK_TIME_INVALID')


def test_split_parent_reversal_waits_for_all_children(registered):
    client, run_id = registered
    client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-P', '00108', amount='40.01'))
    client.post(f'/api/chargebacks/{run_id}/notices?parent_notice_id=CB-P',
                json=notice('CB-S1', '00108', amount='20.00'))
    client.post(f'/api/chargebacks/{run_id}/notices?parent_notice_id=CB-P',
                json=notice('CB-S2', '00108', amount='20.01'))
    blocked = client.post(f'/api/chargebacks/{run_id}/notices/CB-P/reversal',
                          json={'reversal_id': 'RV-P', 'reversed_at': '2026-11-22T10:00:00+08:00'})
    assert (blocked.status_code, blocked.json()['code']) == (409, 'NOTICE_PARENT_ALREADY_SPLIT')
    client.post(f'/api/chargebacks/{run_id}/notices/CB-S1/reversal',
                json={'reversal_id': 'RV-1', 'reversed_at': '2026-11-22T10:00:00+08:00'})
    client.post(f'/api/chargebacks/{run_id}/notices/CB-S2/reversal',
                json={'reversal_id': 'RV-2', 'reversed_at': '2026-11-22T11:00:00+08:00'})
    allowed = client.post(f'/api/chargebacks/{run_id}/notices/CB-P/reversal',
                          json={'reversal_id': 'RV-P', 'reversed_at': '2026-11-22T11:30:00+08:00'})
    assert allowed.status_code == 200
    totals = allowed.json()['totals']
    assert totals['applied_cents'] == 0 and totals['reversed_cents'] == 4001


# ---------- 失败保留旧版 ----------

def test_failed_persistence_rolls_back_and_keeps_previous_snapshot(registered):
    client, run_id = registered
    client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-1', '00109', amount='19.99'))
    previous = client.get(f'/api/chargebacks/{run_id}').json()['totals']
    with sqlite3.connect(client.app.state.store.path) as db:
        db.executescript("""
            CREATE TRIGGER fail_chargeback_snapshot BEFORE INSERT ON chargeback_snapshots
            BEGIN SELECT RAISE(ABORT, 'snapshot storage stopped'); END;
        """)
    with pytest.raises(sqlite3.IntegrityError, match='snapshot storage stopped'):
        client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-2', '00113', amount='9.99'))
    with sqlite3.connect(client.app.state.store.path) as db:
        db.execute('DROP TRIGGER fail_chargeback_snapshot')
    kept = client.get(f'/api/chargebacks/{run_id}').json()['totals']
    assert kept == previous
    recovered = client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-2', '00113', amount='9.99'))
    assert recovered.status_code == 200 and recovered.json()['totals']['notice_count'] == 2


# ---------- 对账单 ----------

def test_statement_zip_covers_notices_events_groups_and_manifest(registered):
    client, run_id = registered
    client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-1', '00108', amount='40.01'))
    response = client.get(f'/api/chargebacks/{run_id}/statement')
    assert response.status_code == 200
    assert response.headers['content-disposition'] == f'attachment; filename="chargebacks-{run_id}.zip"'
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {'chargeback-notices.csv', 'chargeback-events.csv', 'chargeback-by-show.csv',
                'chargeback-by-venue.csv', 'chargeback-by-ticket.csv', 'chargeback-summary.csv',
                'chargeback-reversals.csv', 'manifest.json'} <= names
        manifest = json.loads(archive.read('manifest.json'))
        assert set(manifest['hashes']) == names - {'manifest.json'}
        events = list(csv.DictReader(io.StringIO(
            archive.read('chargeback-events.csv').decode('utf-8-sig'))))
        assert sum(int(row['applied_cents']) for row in events) == -4001


def test_statement_is_byte_stable_and_empty_run_returns_404(registered):
    client, run_id = registered
    empty = client.get(f'/api/chargebacks/{run_id}/statement')
    assert (empty.status_code, empty.json()['code']) == (404, 'CHARGEBACK_EMPTY')
    client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-1', '00109'))
    first = client.get(f'/api/chargebacks/{run_id}/statement').content
    second = client.get(f'/api/chargebacks/{run_id}/statement').content
    assert first == second


def test_browse_views_filter_and_page(registered):
    client, run_id = registered
    client.post(f'/api/chargebacks/{run_id}/notices', json=notice('CB-1', '00108', amount='40.01'))
    page = client.get(f'/api/chargebacks/{run_id}', params={'view': 'venues', 'q': '剧场'})
    assert page.status_code == 200 and page.json()['total'] >= 1
    by_show = client.get(f'/api/chargebacks/{run_id}', params={'view': 'shows', 'q': 'A-LAKE'})
    assert by_show.json()['total'] == 1
    bad_view = client.get(f'/api/chargebacks/{run_id}', params={'view': 'nope'})
    assert (bad_view.status_code, bad_view.json()['code']) == (422, 'VIEW_UNKNOWN')
    missing_run = client.get('/api/chargebacks/missing-run')
    assert (missing_run.status_code, missing_run.json()['code']) == (404, 'RUN_NOT_FOUND')
    empty = client.get(f'/api/chargebacks/{run_id}/statement')
    # 已有登记，对账单可下载；未登记批次在另一个测试覆盖 404
    assert empty.status_code == 200
