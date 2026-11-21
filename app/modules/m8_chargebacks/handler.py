"""拒付归集 HTTP 接口。

登记类接口全部走 ChargebackStore 单事务：成功返回新版快照，失败由全局异常处理
返回原因码，库内上一版快照与前端页面结果都不受影响。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app.browse import Browse
from app.config import PREVIEW
from app.contracts import key, text, timestamp
from app.errors import ChargebackError, Problem
from app.modules.m8_chargebacks.ledger import ChargebackStore
from app.modules.m8_chargebacks.service import parse_notice
from app.modules.m8_chargebacks.statement import build_statement

router = APIRouter(prefix='/api/chargebacks')


class NoticeIn(BaseModel):
    notice_id: Annotated[str, Field(min_length=1, max_length=64)]
    ticket: Annotated[str, Field(min_length=1, max_length=64)]
    channel: Annotated[str, Field(min_length=1, max_length=64)]
    policy: Literal['setoff', 'reallocate']
    occurred_at: str
    evidence_due_at: str
    amount: str
    unit: Literal['元', '分']
    note: str = ''


class ReversalIn(BaseModel):
    reversal_id: Annotated[str, Field(min_length=1, max_length=64)]
    reversed_at: str
    note: str = ''


def _store(request):
    return ChargebackStore(request.app.state.store)


def _parse(body: NoticeIn):
    try:
        return parse_notice(body.model_dump())
    except Exception as exc:  # 契约层 RowError 归一为拒付表单错误
        code = getattr(exc, 'code', 'MISSING_VALUE')
        mapping = {'INVALID_AMOUNT': 'CHARGEBACK_AMOUNT_INVALID', 'INVALID_UNIT': 'CHARGEBACK_AMOUNT_INVALID',
                   'INVALID_TIME': 'CHARGEBACK_TIME_INVALID', 'INVALID_KEY': 'INVALID_KEY',
                   'MISSING_VALUE': 'MISSING_VALUE'}
        raise ChargebackError(mapping.get(code, code)) from exc


@router.post('/{run_id}/notices')
def register_notice(request: Request, run_id: str, body: NoticeIn, parent_notice_id: str = ''):
    if run_id == PREVIEW:
        raise Problem(422, 'CHARGEBACK_NO_PREVIEW', {'run_id': run_id})
    notice = _parse(body)
    snapshot, reused = _store(request).register(run_id, notice, parent_notice_id=parent_notice_id or None)
    return {'reused': reused, 'totals': snapshot['totals'], 'policy': snapshot['policy'],
            'notice': next(row for row in snapshot['notices'] if row['notice_id'] == notice['notice_id'])}


@router.post('/{run_id}/notices/{notice_id}/reversal')
def register_reversal(request: Request, run_id: str, notice_id: str, body: ReversalIn):
    if run_id == PREVIEW:
        raise Problem(422, 'CHARGEBACK_NO_PREVIEW', {'run_id': run_id})
    try:
        normalized_id = key(notice_id)
        reversal_id = key(body.reversal_id)
        reversed_at = timestamp(body.reversed_at)
    except Exception as exc:
        raise ChargebackError(getattr(exc, 'code', 'MISSING_VALUE')) from exc
    if body.note.strip():
        text(body.note)
    snapshot, reused = _store(request).reverse(run_id, normalized_id, reversal_id, reversed_at, body.note)
    return {'reused': reused, 'totals': snapshot['totals'], 'policy': snapshot['policy']}


@router.get('/{run_id}')
def chargebacks(request: Request, run_id: str, view: str = 'notices', browse: Browse = Depends()):
    if run_id == PREVIEW:
        return {'items': [], 'total': 0, 'offset': browse.offset, 'limit': browse.limit,
                'empty': True, 'policy': 'setoff', 'totals': {}}
    snapshot = _store(request).snapshot(run_id)
    views = {'notices': snapshot['notices'], 'events': snapshot['events'],
             'reversals': snapshot['reversals'], 'shows': snapshot['by_show'],
             'venues': snapshot['by_venue'], 'tickets': snapshot['by_ticket']}
    browse.view = view
    rows = browse.choose(views)
    return browse.page(rows, totals=snapshot['totals'], policy=snapshot['policy'],
                       generated_at=snapshot['generated_at'])


@router.get('/{run_id}/statement')
def statement(request: Request, run_id: str):
    snapshot = _store(request).snapshot(run_id)
    data = build_statement(snapshot)
    return Response(data, media_type='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="chargebacks-{run_id}.zip"'})
