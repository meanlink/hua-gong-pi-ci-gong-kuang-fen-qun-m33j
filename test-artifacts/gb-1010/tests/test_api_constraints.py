import os, sys, time
import pytest

ROOT = '/workspace/backend'
sys.path.insert(0, ROOT)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/api.db')
    monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path / 'artifacts'))
    monkeypatch.setenv('SEED_DEMO', 'true')
    for name in list(sys.modules):
        if name.startswith('src') or name.startswith('migrations') or name.startswith('scripts'):
            del sys.modules[name]
    from fastapi.testclient import TestClient
    from src.main import app
    with TestClient(app) as c:
        yield c


def login(c, email):
    r = c.post('/auth/login', json={'email': email, 'password': 'Demo123!'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['access_token']}


def test_meta_and_versions_listing(client):
    admin = login(client, 'admin@demo.local')
    viewer = login(client, 'viewer@demo.local')
    ds = client.get('/datasets', headers=viewer).json()[0]
    listing = client.get(f"/datasets/{ds['id']}/constraint-versions", headers=viewer).json()
    assert listing['active_version_id'] == ds['active_constraint_version_id']
    assert listing['versions'] and listing['versions'][0]['is_active']


def test_permissions(client):
    admin = login(client, 'admin@demo.local')
    analyst = login(client, 'analyst@demo.local')
    viewer = login(client, 'viewer@demo.local')
    ds = client.get('/datasets', headers=admin).json()[0]
    payload = {'dataset_id': ds['id'], 'must_links': [], 'cannot_links': []}
    assert client.post(f"/datasets/{ds['id']}/constraint-versions", headers=viewer, json=payload).status_code == 403
    assert client.post(f"/datasets/{ds['id']}/constraint-versions/validate", headers=viewer, json=payload).status_code == 403
    assert client.post(f"/datasets/{ds['id']}/constraint-versions/validate", headers=analyst, json=payload).status_code == 200
    job = client.post('/jobs', headers=analyst, json={'name': '任务', 'dataset_id': ds['id'], 'params': {'clusters': 3}})
    assert job.status_code == 201
    assert client.post(f"/jobs/{job.json()['id']}/approve", headers=analyst).status_code == 403


def test_validate_endpoint_reports_missing_and_contradiction(client):
    admin = login(client, 'admin@demo.local')
    ds = client.get('/datasets', headers=admin).json()[0]
    recs = client.get(f"/datasets/{ds['id']}", headers=admin).json()['records']
    a, b, c = recs[0]['batch_id'], recs[1]['batch_id'], recs[2]['batch_id']
    r = client.post(f"/datasets/{ds['id']}/constraint-versions/validate", headers=admin, json={
        'dataset_id': ds['id'], 'must_links': [['ZZ-404', a]], 'cannot_links': []})
    body = r.json()
    assert body['ok'] is False and body['unknown_batches'] == ['ZZ-404']
    r = client.post(f"/datasets/{ds['id']}/constraint-versions/validate", headers=admin, json={
        'dataset_id': ds['id'], 'must_links': [[a, b], [b, c]], 'cannot_links': [[a, c]]})
    assert any(i['code'] == 'contradiction' for i in r.json()['issues'])


def test_idempotent_retry_does_not_create_version(client):
    admin = login(client, 'admin@demo.local')
    ds = client.get('/datasets', headers=admin).json()[0]
    recs = client.get(f"/datasets/{ds['id']}", headers=admin).json()['records']
    a, b = recs[10]['batch_id'], recs[11]['batch_id']
    headers = {**admin, 'Idempotency-Key': 'retry-1'}
    body = {'dataset_id': ds['id'], 'must_links': [[a, b]], 'cannot_links': []}
    first = client.post(f"/datasets/{ds['id']}/constraint-versions", headers=headers, json=body)
    second = client.post(f"/datasets/{ds['id']}/constraint-versions", headers=headers, json=body)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()['id'] == second.json()['id']
    assert second.json()['replayed'] is True
    count = len(client.get(f"/datasets/{ds['id']}/constraint-versions", headers=admin).json()['versions'])
    assert count == 2  # seed v1 + 本次 v2


def test_worker_compute_approve_swap_blocks_all(client):
    admin = login(client, 'admin@demo.local')
    analyst = login(client, 'analyst@demo.local')
    import src.worker as W
    ds = client.get('/datasets', headers=admin).json()[0]
    did, vid = ds['id'], ds['active_constraint_version_id']
    job = client.post('/jobs', headers=analyst, json={
        'name': '约束验收', 'dataset_id': did, 'params': {'clusters': 3}, 'constraint_version_id': vid}).json()
    W.execute(job['id'])
    got = client.get(f"/jobs/{job['id']}", headers=admin).json()
    assert got['status'] == 'completed'
    assert got['constraint_version'] == 1
    assert got['result']['constraint']['verification']['non_empty_clusters'] == 3
    assert client.post(f"/jobs/{job['id']}/approve", headers=admin).status_code == 200
    assert client.post('/reports', headers=admin, json={'name': '旧简报', 'job_id': job['id']}).status_code == 201
    recs = client.get(f"/datasets/{did}", headers=admin).json()['records']
    swap = client.post(f"/datasets/{did}/constraint-versions",
                       headers={**admin, 'Idempotency-Key': 'swap-e2e'},
                       json={'dataset_id': did, 'must_links': [[recs[20]['batch_id'], recs[21]['batch_id']]],
                             'cannot_links': [], 'base_version_id': vid})
    assert swap.status_code == 201
    got = client.get(f"/jobs/{job['id']}", headers=admin).json()
    assert got['status'] == 'completed' and got['approved'] is False and got['invalidated_at']
    assert client.post(f"/jobs/{job['id']}/approve", headers=admin).status_code == 409
    assert client.post('/reports', headers=admin, json={'name': '新简报', 'job_id': job['id']}).status_code == 409
    reports = client.get('/reports', headers=admin).json()
    mine = [r for r in reports if r['job_id'] == job['id']][0]
    assert mine['enabled'] is False and mine['disabled_reason']
    assert client.post(f"/reports/{mine['id']}/toggle", headers=admin).status_code == 409


def test_worker_rejects_job_queued_then_stale(client):
    admin = login(client, 'admin@demo.local')
    analyst = login(client, 'analyst@demo.local')
    import src.worker as W
    ds = client.get('/datasets', headers=admin).json()[0]
    did, vid = ds['id'], ds['active_constraint_version_id']
    job = client.post('/jobs', headers=analyst, json={
        'name': '排队换版', 'dataset_id': did, 'params': {'clusters': 3}, 'constraint_version_id': vid}).json()
    recs = client.get(f"/datasets/{did}", headers=admin).json()['records']
    client.post(f"/datasets/{did}/constraint-versions",
                headers={**admin, 'Idempotency-Key': 'swap-before-run'},
                json={'dataset_id': did, 'must_links': [[recs[30]['batch_id'], recs[31]['batch_id']]],
                      'cannot_links': [], 'base_version_id': vid})
    W.execute(job['id'])
    got = client.get(f"/jobs/{job['id']}", headers=admin).json()
    assert got['status'] == 'invalidated'
    assert got['result'] == {}


def test_infeasible_job_records_failure_kind(client):
    admin = login(client, 'admin@demo.local')
    analyst = login(client, 'analyst@demo.local')
    import src.worker as W
    ds = client.get('/datasets', headers=admin).json()[0]
    did = ds['id']
    recs = client.get(f"/datasets/{did}", headers=admin).json()['records']
    clique = [r['batch_id'] for r in recs[:6]]
    cannot = [[clique[i], clique[j]] for i in range(6) for j in range(i + 1, 6)]
    v = client.post(f"/datasets/{did}/constraint-versions",
                    headers={**admin, 'Idempotency-Key': 'clique'},
                    json={'dataset_id': did, 'must_links': [], 'cannot_links': cannot}).json()
    job = client.post('/jobs', headers=analyst, json={
        'name': '无解验收', 'dataset_id': did, 'params': {'clusters': 3}, 'constraint_version_id': v['id']}).json()
    W.execute(job['id'])
    got = client.get(f"/jobs/{job['id']}", headers=admin).json()
    assert got['status'] == 'failed'
    assert got['result']['failure']['kind'] == 'infeasible'
    assert got['result']['failure']['evidence']


def test_unconstrained_job_remains_available_after_swap(client):
    admin = login(client, 'admin@demo.local')
    seed_job = [j for j in client.get('/jobs', headers=admin).json() if j['constraint_version_id'] is None][0]
    ds = client.get('/datasets', headers=admin).json()[0]
    recs = client.get(f"/datasets/{ds['id']}", headers=admin).json()['records']
    client.post(f"/datasets/{ds['id']}/constraint-versions",
                headers={**admin, 'Idempotency-Key': 'swap-unconstrained'},
                json={'dataset_id': ds['id'], 'must_links': [[recs[40]['batch_id'], recs[41]['batch_id']]],
                      'cannot_links': [], 'base_version_id': ds['active_constraint_version_id']})
    got = client.get(f"/jobs/{seed_job['id']}", headers=admin).json()
    assert got['invalidated_at'] is None and got['approved'] is True
