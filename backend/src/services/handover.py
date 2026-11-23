"""整改派单与交接的数据库层：签署对账、领单、续传、回执。

断网恢复定下的接法：交接事件表的 seq 是续传游标。区线端本地保存 last_seq，
恢复后先拉 pending 快照校准本区待办并以 high_watermark 重置游标，再用
feed?since=last_seq 增量补齐；游标只接受服务端返回的 seq，客户端不自造。
"""

from fastapi import HTTPException
from sqlalchemy import func, select

from src.config.settings import engine
from src.constants.workflow import Dispatch, Handover
from src.models.tables import dispatches, handovers
from src.services.handover_core import plan_ack, plan_reconcile
from src.utils.time import utc_time


def _emit(conn, dispatch, event, actor, evidence_id, note=None):
    conn.execute(handovers.insert().values(
        dispatch_id=dispatch['id'], difference_key=dispatch['difference_key'],
        zone=dispatch['zone'], kind=dispatch['kind'], operator_id=dispatch['operator_id'],
        order_id=dispatch['order_id'], generation=dispatch['generation'],
        event=event, actor=actor, evidence_id=evidence_id, note=note, created_at=utc_time()))


def _high_watermark(conn):
    return conn.execute(select(func.max(handovers.c.seq))).scalar_one() or 0


def reconcile_on_review(conn, evidence_id, report, actor):
    """复核签署时对账派单，与签署同事务：撤下已消失的、开出新出现的、保留没复验的。"""
    open_rows = [dict(row) for row in conn.execute(
        select(dispatches).where(dispatches.c.status != Dispatch.CLOSED)).mappings()]
    generations = {}
    for key, generation in conn.execute(select(dispatches.c.difference_key, dispatches.c.generation)):
        generations[key] = max(generations.get(key, 0), generation)
    closes, updates, opens = plan_reconcile(report, open_rows, generations)
    by_key = {row['difference_key']: row for row in open_rows}
    now = utc_time()
    for key in closes:
        dispatch = by_key[key]
        conn.execute(dispatches.update().where(dispatches.c.id == dispatch['id']).values(
            status=Dispatch.CLOSED, open_key=None, closed_by_evidence_id=evidence_id, closed_at=now))
        _emit(conn, dispatch, Handover.CLOSED, actor, evidence_id)
    for key in updates:
        conn.execute(dispatches.update().where(dispatches.c.id == by_key[key]['id'])
                     .values(last_seen_evidence_id=evidence_id))
    for spec in opens:
        dispatch_id = conn.execute(dispatches.insert().values(
            **spec, open_key=spec['difference_key'], status=Dispatch.OPEN,
            source_evidence_id=evidence_id, last_seen_evidence_id=evidence_id,
            opened_at=now)).inserted_primary_key[0]
        _emit(conn, {**spec, 'id': dispatch_id}, Handover.OPENED, actor, evidence_id)
    return {'撤下': len(closes), '新开': len(opens), '保留': len(updates)}


def pending(zone):
    """领单：只返回本区未关闭派单，附交接水位供断网恢复后校准游标。"""
    with engine.connect() as conn:
        items = [dict(row) for row in conn.execute(
            select(dispatches).where(dispatches.c.zone == zone, dispatches.c.status != Dispatch.CLOSED)
            .order_by(dispatches.c.id)).mappings()]
        return {'zone': zone, 'items': items, 'high_watermark': _high_watermark(conn)}


def feed(zone, since):
    """续传：本区 seq 大于游标的交接事件，按 seq 升序，附当前水位。"""
    with engine.connect() as conn:
        events = [dict(row) for row in conn.execute(
            select(handovers).where(handovers.c.zone == zone, handovers.c.seq > since)
            .order_by(handovers.c.seq).limit(500)).mappings()]
        return {'zone': zone, 'events': events, 'high_watermark': _high_watermark(conn)}


def ack(dispatch_id, generation, zone, actor, note):
    """回执：只认派单号 + 代次；旧代次或已撤下的派单一律 409，不确认新派单。"""
    with engine.begin() as conn:
        row = conn.execute(select(dispatches).where(dispatches.c.id == dispatch_id)
                           .with_for_update()).mappings().first()
        if row is None or row['zone'] != zone:
            raise HTTPException(404, '找不到该派单')
        dispatch = dict(row)
        outcome, message = plan_ack(dispatch, generation, actor)
        if outcome == 'conflict':
            raise HTTPException(409, message)
        if outcome == 'accept':
            now = utc_time()
            conn.execute(dispatches.update().where(dispatches.c.id == dispatch_id).values(
                status=Dispatch.ACKNOWLEDGED, acked_by=actor, acked_at=now, ack_note=note or None))
            _emit(conn, dispatch, Handover.ACKNOWLEDGED, actor, None, note or None)
            dispatch.update(status=Dispatch.ACKNOWLEDGED, acked_by=actor, acked_at=now, ack_note=note or None)
        return dispatch


def recent():
    """最近派单（含回执与撤下信息），证据页据此展示交接状态。"""
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(
            select(dispatches).order_by(dispatches.c.id.desc()).limit(100)).mappings()]
