import time
from fastapi import HTTPException
from redis import Redis
from src.config.settings import REDIS_URL
from src.models import ConstraintVersion, Dataset, Job
from src.services.business import metadata
from src.services.constraints import job_is_current


def create(db, data, user):
    dataset = db.get(Dataset, data.dataset_id)
    if not dataset:
        raise HTTPException(404, '数据批次不存在')

    # 只有显式传入非空版本才绑定约束；缺省保持原有的无约束分析语义。
    version = None
    if data.constraint_version_id is not None:
        version = db.get(ConstraintVersion, data.constraint_version_id)
        if not version or version.dataset_id != dataset.id:
            raise HTTPException(422, '约束版本不存在或不属于该数据批次')

    allowed = {p['key']: p for p in metadata().get('parameters', [])}
    if set(data.params) - set(allowed):
        raise HTTPException(422, '包含未知分析参数')
    params = {k: p['default'] for k, p in allowed.items()}
    params.update(data.params)
    for key, p in allowed.items():
        v = params[key]
        if p['type'] == 'number':
            try:
                v = float(v)
                if not (p.get('min', -1e9) <= v <= p.get('max', 1e9)):
                    raise ValueError()
                params[key] = v
            except (ValueError, TypeError):
                raise HTTPException(422, f'{p["label"]} 超出范围或非数值')
        elif str(v) not in [str(o['value']) for o in p.get('options', [])]:
            raise HTTPException(422, f'{p["label"]} 不是有效选项')

    j = Job(
        name=data.name,
        dataset_id=data.dataset_id,
        params=params,
        created_by=user.id,
        created_at=time.time(),
        status='queued',
        constraint_version_id=version.id if version else None,
        constraint_version=version.version if version else None,
        constraint_hash=version.constraint_hash if version else None,
    )
    db.add(j)
    db.flush()
    return j


def enqueue(job_id):
    try:
        r = Redis.from_url(REDIS_URL)
        r.rpush('analysis:queue', job_id)
        r.close()
    except Exception:
        pass  # SQL queued status is the durable fallback


def require_approvable(db, job: Job):
    if job.status != 'completed':
        raise HTTPException(409, '仅已完成结果可以批准')
    if not job_is_current(db, job) or job.invalidated_at is not None:
        raise HTTPException(409, job.invalidation_reason or '该分析绑定的约束版本已被替换，结果失效，不能批准')


def approve(db, job: Job):
    """批准与换版共用数据行锁：两者不可能同时生效。"""
    from src.services.constraints import serialized_dataset_write
    with serialized_dataset_write(db, job.dataset_id):
        db.expire_all()
        job = db.get(Job, job.id)
        require_approvable(db, job)
        job.approved = True
        # 在临界区内提交，保证 SQLite 的进程锁释放前批准已落库。
        db.commit()
    return job
