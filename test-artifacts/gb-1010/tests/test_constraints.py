import os, sys, time, json, importlib
import pytest

ROOT = '/workspace/backend'
sys.path.insert(0, ROOT)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / 'test.db'
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{db}')
    monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path / 'artifacts'))
    monkeypatch.setenv('SEED_DEMO', 'false')
    for name in list(sys.modules):
        if name.startswith('src') or name.startswith('migrations'):
            del sys.modules[name]
    from migrations.initial import migrate
    migrate()
    from src.config.database import Session
    from src.models import User, Dataset
    from src.services.business import seed_records
    from src.utils.serialization import checksum
    from src.utils.security import hash_password
    rows = seed_records()
    with Session() as s:
        admin = User(email='admin@x', name='A', role='admin', password=hash_password('x'))
        analyst = User(email='analyst@x', name='N', role='analyst', password=hash_password('x'))
        viewer = User(email='viewer@x', name='V', role='viewer', password=hash_password('x'))
        s.add_all([admin, analyst, viewer]); s.flush()
        d = Dataset(name='ds', records=rows, checksum=checksum(rows), created_at=time.time())
        s.add(d); s.commit()
        ids = [r['batch_id'] for r in rows]
        yield {'Session': Session, 'dataset_id': d.id, 'ids': ids, 'rows': rows,
               'admin': admin.id, 'analyst': analyst.id, 'viewer': viewer.id}


def _create(env, must, cannot, key=None, base=None, user_id=None):
    from src.services import constraints
    from src.schemas.constraint import ConstraintVersionInput
    from src.models import User
    with env['Session']() as s:
        u = s.get(User, user_id or env['admin'])
        payload = ConstraintVersionInput(dataset_id=env['dataset_id'], must_links=must,
                                        cannot_links=cannot, base_version_id=base)
        v, replayed = constraints.create_version(s, payload, u, key)
        s.commit()
        return v.id, v.version, replayed


def test_validation_missing_and_transitive(env):
    from src.services.constraints import validate_only
    ids = env['ids']
    r = validate_only(env['rows'], [['NOPE', ids[0]]], [])
    assert any(i['code'] == 'unknown_batch' for i in r['issues'])
    r = validate_only(env['rows'], [[ids[0], ids[1]], [ids[1], ids[2]]], [[ids[0], ids[2]]])
    assert any(i['code'] == 'contradiction' for i in r['issues'])
    assert '传递' in r['issues'][0]['message']
    r = validate_only(env['rows'], [[ids[0], ids[0]]], [])
    assert any(i['code'] == 'self_pair' for i in r['issues'])
    # 规范化：反向对与重复对合并
    r = validate_only(env['rows'], [[ids[1], ids[0]], [ids[0], ids[1]]], [])
    assert r['must_links'] == [[ids[0], ids[1]]]


def test_duplicate_batch_ids_rejected(env):
    from src.services.constraints import validate_only
    rows = [dict(r) for r in env['rows']]
    rows[1]['batch_id'] = rows[0]['batch_id']
    r = validate_only(rows, [], [])
    assert any(i['code'] == 'duplicate_batch_id' for i in r['issues'])


def test_version_persistence_and_idempotency(env):
    ids = env['ids']
    vid, ver, replay = _create(env, [[ids[0], ids[1]]], [[ids[0], ids[5]]], key='k1')
    assert ver == 1 and replay is False
    vid2, ver2, replay2 = _create(env, [[ids[0], ids[1]]], [[ids[0], ids[5]]], key='k1')
    assert (vid2, ver2, replay2) == (vid, 1, True)
    # 内容相同、无键：不新增版本
    vid3, ver3, replay3 = _create(env, [[ids[0], ids[1]]], [[ids[0], ids[5]]])
    assert vid3 == vid and replay3 is True
    # 新内容：新增 v2
    vid4, ver4, replay4 = _create(env, [[ids[2], ids[3]]], [])
    assert ver4 == 2 and replay4 is False


def test_stale_base_rejected_without_new_version(env):
    from fastapi import HTTPException
    ids = env['ids']
    _create(env, [[ids[0], ids[1]]], [], key='a')
    with pytest.raises(HTTPException) as exc:
        _create(env, [[ids[2], ids[3]]], [], key='b', base=999)
    assert exc.value.status_code == 409
    from src.models import ConstraintVersion
    with env['Session']() as s:
        assert s.query(ConstraintVersion).count() == 1


def test_idempotency_key_conflicting_content(env):
    from fastapi import HTTPException
    ids = env['ids']
    _create(env, [[ids[0], ids[1]]], [], key='same')
    with pytest.raises(HTTPException) as exc:
        _create(env, [[ids[2], ids[3]]], [], key='same')
    assert exc.value.status_code == 409


def test_swap_invalidates_jobs_and_pauses_reports(env):
    from src.models import Job, Report
    from src.services.jobs import require_approvable
    from fastapi import HTTPException
    ids = env['ids']
    vid, _, _ = _create(env, [[ids[0], ids[1]]], [], key='v1')
    with env['Session']() as s:
        j = Job(name='old', dataset_id=env['dataset_id'], params={'clusters': 3}, status='completed',
                approved=True, created_by=env['admin'], created_at=time.time(),
                constraint_version_id=vid, constraint_version=1, result={'summary': 'x'})
        s.add(j); s.flush()
        r = Report(name='r', job_id=j.id, destination='x', interval_seconds=60,
                   next_run=time.time() + 60, enabled=True)
        s.add(r); s.commit(); jid = j.id
    _create(env, [[ids[2], ids[3]]], [], key='v2')
    with env['Session']() as s:
        j = s.get(Job, jid)
        assert j.invalidated_at is not None
        assert j.approved is False
        assert 'v2' in j.invalidation_reason
        assert s.get(Report, 1).enabled is False
        with pytest.raises(HTTPException):
            require_approvable(s, j)


def test_unconstrained_job_unaffected_by_swap(env):
    from src.models import Job, Dataset
    from src.services.constraints import job_is_current
    ids = env['ids']
    _create(env, [[ids[0], ids[1]]], [], key='v1')
    with env['Session']() as s:
        j = Job(name='plain', dataset_id=env['dataset_id'], params={'clusters': 3}, status='completed',
                approved=True, created_by=env['admin'], created_at=time.time(), result={'summary': 'x'})
        s.add(j); s.commit(); jid = j.id
    _create(env, [[ids[2], ids[3]]], [], key='v2')
    with env['Session']() as s:
        j = s.get(Job, jid)
        assert j.invalidated_at is None and j.approved is True
        assert job_is_current(s, j) is True


def test_constrained_analysis_satisfies_all_constraints(env):
    from src.services.business import analyze
    from src.services.constraints import analyze_constrained
    ids = env['ids']
    must = [[ids[0], ids[1]], [ids[1], ids[2]], [ids[60], ids[61]]]
    cannot = [[ids[0], ids[120]], [ids[60], ids[121]], [ids[3], ids[4]]]
    res = analyze_constrained(env['rows'], {}, 3, must, cannot)
    assert res['status'] == 'satisfied'
    verify = res['constraint']['verification']
    assert verify['must_links_satisfied'] == verify['must_links_total'] == 3
    assert verify['cannot_links_satisfied'] == verify['cannot_links_total'] == 3
    assert verify['non_empty_clusters'] == 3
    # 逐批归群依据
    assign = {a['batch_id']: a['cluster'] for a in res['constraint']['batch_assignments']}
    assert assign[ids[0]] == assign[ids[1]] == assign[ids[2]]
    assert assign[ids[0]] != assign[ids[120]]
    # 180 批：{001,002,003} 合并 3→1，{061,062} 合并 2→1 → 180-2-1=177 组
    assert len(res['constraint']['components']) == 177


def test_infeasible_clique_evidence(env):
    from src.services.constraints import analyze_constrained, ConstraintAnalysisError
    ids = env['ids']
    clique = [ids[i] for i in range(6)]
    cannot = [[clique[i], clique[j]] for i in range(6) for j in range(i + 1, 6)]
    with pytest.raises(ConstraintAnalysisError) as exc:
        analyze_constrained(env['rows'], {}, 3, [], cannot)
    assert exc.value.kind == 'infeasible'
    assert '互斥' in exc.value.message
    assert exc.value.evidence


def test_odd_cycle_proven_infeasible(env):
    from src.services.constraints import analyze_constrained, ConstraintAnalysisError
    ids = env['ids']
    cycle = [ids[i] for i in range(5)]
    cannot = [[cycle[i], cycle[(i + 1) % 5]] for i in range(5)]
    with pytest.raises(ConstraintAnalysisError) as exc:
        analyze_constrained(env['rows'], {}, 2, [], cannot)
    assert exc.value.kind == 'infeasible'
    assert exc.value.search.get('proven') is True


def test_timeout_distinct_from_infeasible(env, monkeypatch):
    import src.services.constraints as C
    ids = env['ids'][:60]
    rng = __import__('numpy').random.default_rng(7)
    cannot = []
    for i in range(60):
        for j in range(i + 1, 60):
            if rng.random() < 0.12:
                cannot.append([ids[i], ids[j]])
    monkeypatch.setattr(C, 'CONSTRAINT_SEARCH_NODE_LIMIT', 5)
    with pytest.raises(C.ConstraintAnalysisError) as exc:
        C.analyze_constrained(env['rows'], {}, 3, [], cannot)
    assert exc.value.kind == 'timeout'
    assert exc.value.search['timed_out'] is True
    assert '不代表已证明无解' in exc.value.message


def test_fewer_groups_than_k_is_infeasible(env):
    from src.services.constraints import analyze_constrained, ConstraintAnalysisError
    ids = env['ids']
    # 把全部 180 批压成 5 个同群组（36 批一组），K=6 必然无法保证每群非空。
    # 每 36 批用链式同群边连成一组。
    must = []
    for g in range(5):
        base = g * 36
        for i in range(35):
            must.append([ids[base + i], ids[base + i + 1]])
    with pytest.raises(ConstraintAnalysisError) as exc:
        analyze_constrained(env['rows'], {}, 6, must, [])
    assert exc.value.kind == 'infeasible'
    assert '少于指定群数' in exc.value.message


def test_non_empty_every_cluster(env):
    from src.services.constraints import analyze_constrained
    ids = env['ids']
    # 一个必须同群的大组 + 其余：确保拆分后每群非空
    must = [[ids[0], ids[1]], [ids[1], ids[2]], [ids[2], ids[3]], [ids[3], ids[4]]]
    res = analyze_constrained(env['rows'], {}, 4, must, [])
    counts = [row['count'] for row in res['table']['rows']]
    assert len(counts) == 4 and all(c > 0 for c in counts)


def test_result_json_safe(env):
    from src.services.constraints import analyze_constrained
    ids = env['ids']
    res = analyze_constrained(env['rows'], {}, 3, [[ids[0], ids[1]]], [[ids[0], ids[2]]])
    json.dumps(res, allow_nan=False, ensure_ascii=False)


def test_concurrent_swap_only_one_wins(env):
    """四个并发换版请求只允许一个成功，其余 409 且不新增版本。"""
    import threading
    from src.services import constraints
    from src.schemas.constraint import ConstraintVersionInput
    from src.models import User, ConstraintVersion
    from fastapi import HTTPException
    ids = env['ids']
    barrier = threading.Barrier(4)
    results, errors = [], []

    def worker(n):
        try:
            with env['Session']() as db:
                u = db.get(User, env['admin'])
                payload = ConstraintVersionInput(dataset_id=env['dataset_id'],
                                                 must_links=[[ids[n], ids[n + 1]]], cannot_links=[])
                barrier.wait()
                v, replay = constraints.create_version(db, payload, u, idempotency_key=f'k{n}')
                db.commit()
                results.append(v.version)
        except HTTPException as exc:
            errors.append(exc.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    with env['Session']() as s:
        assert s.query(ConstraintVersion).count() == 1
    assert len(results) == 1 and errors == [409] * 3


def test_retry_same_key_no_new_version(env):
    """换版请求重试（同幂等键）不新增版本。"""
    from src.models import ConstraintVersion
    ids = env['ids']
    vid, ver, _ = _create(env, [[ids[0], ids[1]]], [], key='retry')
    for _ in range(3):
        vid2, ver2, replay = _create(env, [[ids[0], ids[1]]], [], key='retry')
        assert (vid2, ver2, replay) == (vid, 1, True)
    with env['Session']() as s:
        assert s.query(ConstraintVersion).count() == 1
