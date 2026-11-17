import hashlib, json
def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
def dataset_dict(d):
    return {'id':d.id, 'name':d.name, 'source':d.source, 'rows':len(d.records), 'checksum':d.checksum,
            'active_constraint_version_id':d.active_constraint_version_id, 'created_at':d.created_at}
def job_dict(j):
    data={k:getattr(j,k) for k in ['id','name','dataset_id','params','status','result','error','approved',
                                   'constraint_version_id','constraint_version','constraint_hash',
                                   'invalidated_at','invalidation_reason','created_at','finished_at']}
    return data
def constraint_version_dict(v, active_id=None):
    return {
        'id':v.id, 'dataset_id':v.dataset_id, 'version':v.version,
        'must_links':v.must_links, 'cannot_links':v.cannot_links,
        'constraint_hash':v.constraint_hash, 'note':v.note,
        'base_version_id':v.base_version_id, 'created_at':v.created_at,
        'is_active': v.id == active_id,
    }
def report_dict(r):
    return {**{k:getattr(r,k) for k in ['id','name','job_id','destination','interval_seconds','next_run','enabled']},
            'disabled_reason':r.disabled_reason}
def delivery_dict(d):
    return {k:getattr(d,k) for k in ['id','report_id','created_at','checksum','filename','destination']}
