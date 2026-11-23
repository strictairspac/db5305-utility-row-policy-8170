from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download
from src.models.tables import audit, cases, evidence, runs
from src.schemas.workflow import AckForm, CaseForm, LoginForm, ReviewForm
from src.services import domain, handover, workflow
from src.services.security import current_user, login, operator, reviewer, zone_lead

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent(evidence)


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/rectifications/pending')
def pending_rectifications(user=Depends(zone_lead)):
    """领单：只返回本区未关闭派单，high_watermark 供断网恢复后校准续传游标。"""
    return handover.pending(user['zone'])


@router.get('/rectifications/feed')
def rectification_feed(user=Depends(zone_lead), since: int = Query(0, ge=0)):
    """续传：本区 seq 大于游标的交接事件；断网恢复后从本地 last_seq 接着补。"""
    return handover.feed(user['zone'], since)


@router.post('/rectifications/{dispatch_id}/ack')
def ack_rectification(dispatch_id: int, form: AckForm, user=Depends(zone_lead)):
    """回执：只认派单号 + 代次，旧代次的晚到回执确认不了新派单。"""
    return handover.ack(dispatch_id, form.generation, user['zone'], user['sub'], form.note.strip())


@router.get('/handovers')
def list_handovers(_=Depends(current_user)):
    """最近整改派单及其交接状态，证据页据此展示。"""
    return handover.recent()


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)
