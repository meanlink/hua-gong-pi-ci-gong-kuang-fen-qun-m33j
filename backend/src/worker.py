import time, json, logging
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset, ConstraintVersion, Report
from src.services.business import analyze
from src.services.constraints import ConstraintAnalysisError, job_is_current
from src.services.reports import deliver
from src.services.audit import record
logging.basicConfig(level=logging.INFO)


def _claim(job_id):
    """原子领取：只有仍处于 queued 且约束未失效的任务才会转为 running。"""
    with Session() as db:
        j=db.get(Job,job_id)
        if not j or j.status!='queued':return None
        if j.invalidated_at is not None or not job_is_current(db, j):
            j.status='invalidated'
            j.invalidated_at=j.invalidated_at or time.time()
            j.invalidation_reason=j.invalidation_reason or '约束规则已更新，排队任务不再执行。'
            record(db,'worker','job.rejected',{'id':j.id,'reason':'stale_constraint_version'})
            db.commit();return None
        j.status='running';db.commit()
        return j.dataset_id,j.params,j.constraint_version_id


def _finalize(job_id, dataset_id, params, version_id, result=None, error=None):
    """在新事务里落库，并最后一次核对现行版本，避免与换版请求交错。"""
    with Session() as db:
        j=db.get(Job,job_id)
        if not j:return
        if j.invalidated_at is not None or not job_is_current(db, j):
            # 计算期间已换版：无论结果是否算出都拒收，绝不改标签凑数。
            j.status='invalidated'
            j.invalidated_at=j.invalidated_at or time.time()
            j.invalidation_reason=j.invalidation_reason or '约束规则在计算期间已更新，结果失效。'
            j.result={}
            record(db,'worker','job.rejected',{'id':j.id,'reason':'stale_constraint_version'})
            db.commit();return
        if result is not None:
            j.result=result;j.status='completed';j.error='';j.finished_at=time.time()
            record(db,'worker','job.completed',{'id':j.id,'constraint_version_id':version_id})
        else:
            kind=getattr(error,'kind','failed')
            j.result=error.result() if isinstance(error,ConstraintAnalysisError) else {}
            j.status='failed';j.error=str(error)[:990];j.finished_at=time.time()
            record(db,'worker','job.'+kind,{'id':j.id,'error':j.error,
                   **((error.search) if isinstance(error,ConstraintAnalysisError) else {})})
        db.commit()


def execute(job_id):
    claimed=_claim(job_id)
    if not claimed:return
    dataset_id,params,version_id=claimed
    try:
        with Session() as db:
            d=db.get(Dataset,dataset_id)
            version=db.get(ConstraintVersion,version_id) if version_id is not None else None
            if version_id is not None and (version is None or version.dataset_id!=d.id):
                raise ConstraintAnalysisError('invalid','约束版本缺失，无法计算。')
            records=d.records
        result=analyze(records,params,version)
        json.dumps(result,allow_nan=False)
        _finalize(job_id,dataset_id,params,version_id,result=result)
    except ConstraintAnalysisError as e:
        logging.info('Constrained analysis %s: %s',e.kind,e.message)
        _finalize(job_id,dataset_id,params,version_id,error=e)
    except Exception as e:
        logging.exception('Analysis failed')
        _finalize(job_id,dataset_id,params,version_id,error=e)


def tick():
    with Session() as db:
        queued=list(db.scalars(select(Job.id).where(Job.status=='queued').limit(3)))
    for job_id in queued:execute(job_id)
    with Session() as db:
        for r in db.scalars(select(Report).where(Report.enabled==True,Report.next_run<=time.time())):
            d=deliver(db,r)
            if d:record(db,'worker','report.delivered',{'id':r.id,'delivery':d.id})
        db.commit()


def main():
    r=Redis.from_url(REDIS_URL,socket_timeout=3)
    # This Compose stack has a single worker. A crash must not strand its jobs.
    with Session() as db:
        for j in db.scalars(select(Job).where(Job.status=='running')):
            j.status='queued';record(db,'worker','job.recovered',{'id':j.id})
        db.commit()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item=r.blpop('analysis:queue',timeout=1)
            if item:execute(int(item[1]))
            tick()
        except Exception:
            logging.exception('Worker retry');time.sleep(2)
if __name__=='__main__':main()
