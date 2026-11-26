from fastapi import APIRouter, Depends
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import canonical_json, download
from src.models.tables import audit, cases, evidence, runs
from src.schemas.workflow import CaseForm, LoginForm, RestoreForm, ReviewForm
from src.services import domain, workflow
from src.services.security import (authenticated, current_user, login,
                                   operator, reviewer)

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


@router.get('/evidence/{identity}/backup')
def backup_signed_batch(identity: int, actor=Depends(authenticated)):
    """已签批次的离线备份包；档案岗另存的校验凭据在 /credential 单独下载。"""
    bundle, _ = workflow.backup_evidence(identity, actor)
    return canonical_json(bundle, f'signed-batch-{identity}.json')


@router.get('/evidence/{identity}/credential')
def backup_credential(identity: int, actor=Depends(authenticated)):
    """档案岗另存的校验凭据，与备份包分离保存，恢复时凭它逐字段验真。"""
    _, digest = workflow.backup_evidence(identity, actor)
    return canonical_json({'evidence_id': identity, 'root_digest': digest},
                          f'credential-{identity}.json')


@router.post('/restore', status_code=201)
def restore_signed_batch(form: RestoreForm, actor=Depends(operator)):
    return workflow.restore_backup(form.bundle, form.credential, actor)


@router.get('/restore/batches')
def restored_batches(_=Depends(authenticated)):
    return workflow.list_restored()


@router.get('/restore/active')
def active_restored_batch(_=Depends(authenticated)):
    return workflow.current_restored()


@router.get('/restore/{identity}/slips/{kind}')
def restored_batch_slips(identity: int, kind: str, _=Depends(authenticated)):
    return workflow.restored_slips(identity, kind)


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)
