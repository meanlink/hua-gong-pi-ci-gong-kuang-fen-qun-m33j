from fastapi import APIRouter, Depends, HTTPException, Header, Query
from sqlalchemy import select
from src.config.database import get_db
from src.models import ConstraintVersion, Dataset, Job
from src.schemas.constraint import ConstraintVersionInput, ConstraintValidateInput
from src.middlewares.auth import current_user, can_write
from src.services import constraints
from src.utils.serialization import constraint_version_dict, job_dict

router = APIRouter()


def _dataset(db, dataset_id):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, '数据批次不存在')
    return dataset


@router.get('/datasets/{dataset_id}/constraint-versions')
def listing(dataset_id: int, db=Depends(get_db), u=Depends(current_user)):
    dataset = _dataset(db, dataset_id)
    versions = list(db.scalars(
        select(ConstraintVersion).where(ConstraintVersion.dataset_id == dataset_id).order_by(ConstraintVersion.version.desc())
    ))
    return {
        'dataset_id': dataset_id,
        'active_version_id': dataset.active_constraint_version_id,
        'versions': [constraint_version_dict(v, dataset.active_constraint_version_id) for v in versions],
    }


# 静态路径必须先于 /{version_id} 注册，否则 "validate" 会被当成版本号。
@router.post('/datasets/{dataset_id}/constraint-versions/validate')
def validate(dataset_id: int, data: ConstraintValidateInput, db=Depends(get_db), u=Depends(can_write)):
    if data.dataset_id != dataset_id:
        raise HTTPException(422, '路径与请求体中的数据集不一致')
    dataset = _dataset(db, dataset_id)
    checked = constraints.validate_only(dataset.records, data.must_links, data.cannot_links)
    return {
        'ok': not checked['issues'],
        'issues': checked['issues'],
        'truncated': checked['truncated'],
        'unknown_batches': checked['unknown_batches'],
        'constraint_hash': checked['constraint_hash'],
        'normalized': {'must_links': checked['must_links'], 'cannot_links': checked['cannot_links']},
    }


@router.get('/datasets/{dataset_id}/constraint-versions/{version_id}')
def detail(dataset_id: int, version_id: int, db=Depends(get_db), u=Depends(current_user)):
    dataset = _dataset(db, dataset_id)
    version = db.get(ConstraintVersion, version_id)
    if not version or version.dataset_id != dataset_id:
        raise HTTPException(404, '约束版本不存在')
    return constraint_version_dict(version, dataset.active_constraint_version_id)


@router.post('/datasets/{dataset_id}/constraint-versions', status_code=201)
def create(
    dataset_id: int,
    data: ConstraintVersionInput,
    db=Depends(get_db),
    u=Depends(can_write),
    header_key: str | None = Header(default=None, alias='Idempotency-Key'),
    query_key: str | None = Query(default=None, alias='idempotency_key'),
):
    if data.dataset_id != dataset_id:
        raise HTTPException(422, '路径与请求体中的数据集不一致')
    _dataset(db, dataset_id)
    key = (header_key or query_key or data.idempotency_key or '').strip() or None
    version, replayed = constraints.create_version(db, data, u, key, audit_actor=u.email)
    dataset = _dataset(db, dataset_id)
    body = constraint_version_dict(version, dataset.active_constraint_version_id)
    body['replayed'] = replayed
    return body


@router.get('/datasets/{dataset_id}/jobs')
def jobs_for_dataset(dataset_id: int, db=Depends(get_db), u=Depends(current_user)):
    """归群依据与失效原因：某批次下所有分析任务（含绑定的约束版本）。"""
    _dataset(db, dataset_id)
    jobs = list(db.scalars(select(Job).where(Job.dataset_id == dataset_id).order_by(Job.id.desc()).limit(100)))
    return [job_dict(j) for j in jobs]
