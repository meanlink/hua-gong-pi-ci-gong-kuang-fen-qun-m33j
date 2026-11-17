import time, os, csv, io, hashlib, json
from fastapi import HTTPException
from src.models import Job, Report, Delivery
from src.config.settings import ARTIFACT_DIR
from src.services.constraints import job_is_current

def create(db, data):
    j=db.get(Job,data.job_id)
    if not j or j.status!='completed' or not j.approved:
        raise HTTPException(409,'请先选择已完成且获批准的分析结果')
    # 与换版共用数据行锁，避免“刚批准、正在换版”交叉建出失效计划。
    from src.services.constraints import serialized_dataset_write
    with serialized_dataset_write(db, j.dataset_id):
        db.expire_all()
        j=db.get(Job,j.id)
        if not j or j.status!='completed' or not j.approved:
            raise HTTPException(409,'请先选择已完成且获批准的分析结果')
        if not job_is_current(db, j) or j.invalidated_at is not None:
            raise HTTPException(409, j.invalidation_reason or '该分析绑定的约束版本已被替换，结果失效，不能创建分发计划')
        r=Report(name=data.name, job_id=j.id, destination=data.destination, interval_seconds=data.interval_seconds, next_run=time.time()+data.delay_seconds, enabled=True)
        db.add(r); db.flush()
        # 临界区内提交：避免与换版事务交错出“计划已建、规则已换”的窗口。
        db.commit()
    return r
def deliver(db, report):
    j=db.get(Job,report.job_id)
    if not j or not j.approved: return
    if not job_is_current(db, j) or j.invalidated_at is not None:
        # 到期执行时才发现失效：停用计划并留下原因，不生成快照。
        report.enabled=False
        report.disabled_reason=j.invalidation_reason or '绑定分析的约束版本已被替换，分发已停止。'
        return
    out=io.StringIO()
    out.write('\ufeff')
    table=j.result.get('table',{})
    cols=table.get('columns',[])
    writer=csv.writer(out)
    def safe(v):
        if isinstance(v,(int,float)) and not isinstance(v,bool): return v
        text=str(v)
        return "'"+text if text.startswith(('=','+','-','@','\t','\r')) else text
    writer.writerow([safe(c['label']) for c in cols])
    for row in table.get('rows',[]): writer.writerow([safe(row.get(c['key'],'')) for c in cols])
    data=out.getvalue().encode('utf-8')
    delivery=Delivery(report_id=report.id, created_at=time.time(), checksum=hashlib.sha256(data).hexdigest(), filename='', destination=report.destination)
    db.add(delivery); db.flush()
    delivery.filename=f'report-{report.id}-delivery-{delivery.id}.csv'
    os.makedirs(ARTIFACT_DIR,exist_ok=True)
    with open(os.path.join(ARTIFACT_DIR,delivery.filename),'wb') as f:f.write(data)
    with open(os.path.join(ARTIFACT_DIR,delivery.filename+'.json'),'w') as f: json.dump({'job':j.name,'params':j.params,'result':j.result,'checksum':delivery.checksum},f,ensure_ascii=False,allow_nan=False)
    report.next_run=time.time()+report.interval_seconds
    return delivery
