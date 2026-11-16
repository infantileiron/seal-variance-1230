"""Integration tests for 材料批次清退 (batch purge).

Run against a real PostgreSQL when SEAL_TEST_DATABASE_URL is set; otherwise a
local SQLite file database is used (the process-local lock fallback applies).
Artifacts go to a temp directory per test.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import pytest

BACKEND = Path(__file__).resolve().parents[2] / 'backend'
sys.path.insert(0, str(BACKEND))

DB_URL = os.getenv('SEAL_TEST_DATABASE_URL')


def _psycopg_url(url):
    """embedded-postgres returns libpq URI; SQLAlchemy needs the psycopg dialect."""
    if url.startswith('postgresql://') or url.startswith('postgres://'):
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        host = query.get('host', ['localhost'])[0]
        return f"postgresql+psycopg://{parts.username or 'postgres'}@/{parts.path.lstrip('/') or 'postgres'}?host={host}"
    return url


def _fresh_database():
    if not DB_URL:
        return 'sqlite:///' + str(Path(__file__).parent / '_purge_test.sqlite3')
    url = _psycopg_url(DB_URL)
    import psycopg
    admin = psycopg.connect(_psycopg_url(DB_URL).replace('postgresql+psycopg://', 'postgresql://'), autocommit=True)
    with admin.cursor() as cur:
        cur.execute('DROP DATABASE IF EXISTS seal_purge_test')
        cur.execute('CREATE DATABASE seal_purge_test')
    admin.close()
    return url.replace('/postgres?', '/seal_purge_test?')


@pytest.fixture()
def env(tmp_path, monkeypatch):
    database = _fresh_database()
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir()
    monkeypatch.setenv('DATABASE_URL', database)
    monkeypatch.setenv('REDIS_URL', 'redis://localhost:6379/0')
    monkeypatch.setenv('JWT_SECRET', 'test-secret')
    monkeypatch.setenv('ARTIFACTS_DIR', str(artifacts))
    monkeypatch.setenv('SEED_DEMO', 'false')

    # A fake Redis keeps the queue calls local and inspectable.
    import redis
    import fakeredis

    class RecordingRedis(fakeredis.FakeRedis):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.pushed = []
            self.store = {}

        def lpush(self, key, value):
            self.pushed.append((key, value))
            self.store[key] = value
            return 1

        def brpop(self, key, timeout=0):
            if key in self.store:
                return key, self.store.pop(key)
            return None

    fake = RecordingRedis()
    redis.Redis.from_url = classmethod(lambda cls, *a, **kw: fake)

    for mod in [m for m in list(sys.modules) if m.startswith('src')]:
        del sys.modules[mod]

    from src.config.database import Base, SessionLocal, engine
    from src.models import entities  # noqa: F401
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    yield {
        'session': SessionLocal,
        'engine': engine,
        'artifacts': artifacts,
        'redis': fake,
        'database': database,
    }
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def api(env, monkeypatch):
    from fastapi.testclient import TestClient
    from src.main import app
    from src.config.database import SessionLocal
    from src.models.entities import User
    from src.utils.security import hash_password

    with SessionLocal() as db:
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(email=role + '@demo.local', role=role,
                        password_hash=hash_password('Demo123!')))
        db.commit()

    with TestClient(app) as client:
        tokens = {}
        for role in ('admin', 'analyst', 'viewer'):
            response = client.post('/auth/login', json={'email': role + '@demo.local', 'password': 'Demo123!'})
            assert response.status_code == 200, response.text
            tokens[role] = response.json()['access_token']
        yield {'client': client, 'tokens': tokens}


def headers(api, role='admin'):
    return {'Authorization': 'Bearer ' + api['tokens'][role]}


def seed_processes_and_samples(api, lot='HC-1115', other_lot='HC-2220', per_process=5, other_per_process=3):
    from src.config.database import SessionLocal
    from src.models.entities import Process, Sample

    with SessionLocal() as db:
        processes = []
        for index, mean in enumerate((22.5, 25.8, 24.1), 1):
            process = Process(code=f'HC-S{index}', name=f'方案{index}', temperature=160, dwell=0.8,
                              pressure=0.32, note='')
            db.add(process)
            db.flush()
            processes.append(process)
            for i in range(per_process):
                db.add(Sample(code=f'{lot}-S{index}-{i+1:03}', process_id=process.id, lot=lot,
                              force=round(mean + (i - 2) * 0.3, 2), flagged=(i == 0),
                              note='首件' if i == 0 else ''))
            for i in range(other_per_process):
                db.add(Sample(code=f'{other_lot}-S{index}-{i+1:03}', process_id=process.id, lot=other_lot,
                              force=round(mean + (i - 1) * 0.3, 2), flagged=False, note=''))
        db.commit()
        return [p.id for p in processes]


def make_completed_assessment(api, process_ids, lot='HC-1115', title='复核', approve=False):
    from src.config.database import SessionLocal
    from src.models.entities import Assessment
    from src.services import artifacts
    from src.services.assessments import snapshot_for
    from src.services.statistics import anova

    with SessionLocal() as db:
        snapshot = snapshot_for(db, process_ids, lot)
        result = anova(snapshot)
        item = Assessment(title=title, lot=lot, snapshot=snapshot, status='completed', result=result,
                          created_at=datetime.now(timezone.utc).isoformat())
        if approve:
            item.recommendation = '结合拉力下限与能耗权衡，建议进入下一轮验证并复核外观。'
            item.selected_process_id = process_ids[0]
            item.approved = True
            item.approved_by = 'admin@demo.local'
        db.add(item)
        db.commit()
        db.refresh(item)
        artifacts.write(item)
        return item.id


def sample_rows(lot):
    from src.config.database import SessionLocal
    from src.models.entities import Sample
    from sqlalchemy import select
    with SessionLocal() as db:
        return [dict(id=s.id, code=s.code, force=s.force, note=s.note, flagged=s.flagged,
                     purged=s.purged, purged_at=s.purged_at, purge_id=s.purge_id)
                for s in db.scalars(select(Sample).where(Sample.lot == lot).order_by(Sample.id)).all()]


def test_preview_lists_affected_samples_assessments_and_files(api, env):
    ids = seed_processes_and_samples(api)
    assessment = make_completed_assessment(api, ids[:2], approve=True)

    response = api['client'].get('/purges/preview?lot=HC-1115', headers=headers(api, 'viewer'))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['lot'] == 'HC-1115'
    assert body['samples']['total'] == 15
    assert body['samples']['pending'] == 15
    assert len(body['samples']['codes']) == 15
    assert [a['id'] for a in body['assessments']] == [assessment]
    assert body['assessments'][0]['approved'] is True
    assert body['files'] == [assessment]
    assert 'HC-2220' in body['other_lots_untouched']


def test_preview_requires_lot_and_existing_samples(api, env):
    seed_processes_and_samples(api)
    assert api['client'].get('/purges/preview?lot=', headers=headers(api)).status_code == 422
    assert api['client'].get('/purges/preview?lot=HC-9999', headers=headers(api)).status_code == 404


def test_completed_purge_only_clears_target_lot_and_keeps_history(api, env):
    ids = seed_processes_and_samples(api)
    assessment = make_completed_assessment(api, ids[:3], lot='HC-1115', approve=True)
    original = json.loads((env['artifacts'] / f'{assessment}.json').read_text())

    response = api['client'].post('/purges', json={'lot': 'HC-1115'}, headers=headers(api))
    assert response.status_code == 201, response.text
    job = response.json()
    assert job['status'] == 'completed'
    assert job['progress']['samples'] == 15
    assert job['progress']['samples_total'] == 15
    assert job['progress']['files'] == 1
    assert job['progress']['failed'] == []

    purged = sample_rows('HC-1115')
    assert len(purged) == 15
    for row in purged:
        assert row['purged'] is True
        assert row['code'] is None
        assert row['force'] is None
        assert row['note'] == ''
        assert row['flagged'] is False
        assert row['purged_at']
        assert row['purge_id'] == job['id']

    untouched = sample_rows('HC-2220')
    assert len(untouched) == 9
    assert all(r['code'] and r['force'] is not None and r['note'] == '' for r in untouched)
    assert all(r['purged'] is False for r in untouched)

    # 已完成评议保留原统计量、推荐与签核，只移除快照中的批次逐条明细。
    from src.config.database import SessionLocal
    from src.models.entities import Assessment
    with SessionLocal() as db:
        item = db.get(Assessment, assessment)
        assert item.status == 'completed'
        assert item.approved is True
        assert item.approved_by == 'admin@demo.local'
        assert item.recommendation
        assert item.result['f'] == pytest.approx(original['result']['f'])
        assert item.result['checksum'] == original['result']['checksum']
        assert item.purged_lots == ['HC-1115']
        assert all(group['samples'] == [] for group in item.snapshot['groups'])
        # 快照中的组参数仍在，便于复核口径。
        assert all(group['code'] for group in item.snapshot['groups'])

    document = json.loads((env['artifacts'] / f'{assessment}.json').read_text())
    assert document['result']['f'] == pytest.approx(original['result']['f'])
    assert document['approved'] is True
    assert document['purged_lots'] == ['HC-1115']
    assert not any(s.get('lot') == 'HC-1115' for g in document['snapshot']['groups'] for s in g['samples'])

    # 导出仍可用（清退已完成），但不含该批次明细。
    export = api['client'].get(f'/assessments/{assessment}/export', headers=headers(api, 'viewer'))
    assert export.status_code == 200
    exported = export.json()
    assert not any(s.get('lot') == 'HC-1115' for g in exported['snapshot']['groups'] for s in g['samples'])

    # 复核记录一次，不重复记账。
    from src.models.entities import Audit
    from sqlalchemy import select
    with SessionLocal() as db:
        logs = [a for a in db.scalars(select(Audit).where(Audit.action.like('PURGE%')).order_by(Audit.id)).all()]
        assert len(logs) == 1
        assert logs[0].actor == 'admin@demo.local'


def test_cross_lot_assessment_keeps_other_lot_samples(api, env):
    ids = seed_processes_and_samples(api)
    assessment = make_completed_assessment(api, ids[:2], lot='全部', title='跨批次复核')

    response = api['client'].post('/purges', json={'lot': 'HC-1115'}, headers=headers(api))
    assert response.status_code == 201, response.text
    assert response.json()['status'] == 'completed'

    document = json.loads((env['artifacts'] / f'{assessment}.json').read_text())
    remaining = [s for g in document['snapshot']['groups'] for s in g['samples']]
    assert remaining, '其他批次样本必须保留'
    assert all(s['lot'] == 'HC-2220' for s in remaining)
    assert not any(s['lot'] == 'HC-1115' for s in remaining)

    from src.config.database import SessionLocal
    from src.models.entities import Assessment
    with SessionLocal() as db:
        item = db.get(Assessment, assessment)
        assert item.status == 'completed'
        assert item.purged_lots == ['HC-1115']
        # 原统计量按清退前的完整输入保留（5+3 per process），不重算。
        assert item.result['n'] == 16
        assert item.result['checksum'] == document['result']['checksum']
        # 快照只移除到期批次，其余批次逐条明细仍在。
        snapshot_remaining = [s for g in item.snapshot['groups'] for s in g['samples']]
        assert len(snapshot_remaining) == 6
        assert all(s['lot'] == 'HC-2220' for s in snapshot_remaining)


def test_new_assessment_excludes_purged_samples(api, env):
    ids = seed_processes_and_samples(api)
    assert api['client'].post('/purges', json={'lot': 'HC-1115'}, headers=headers(api)).status_code == 201

    response = api['client'].post('/assessments', json={'title': '清退后复核', 'lot': 'HC-1115',
                                                        'process_ids': ids[:2]}, headers=headers(api, 'analyst'))
    assert response.status_code == 202, response.text
    body = response.json()
    total = sum(len(g['samples']) for g in body['snapshot']['groups'])
    assert total == 0

    # 样本不足：worker  расчета должен явно упасть, не показывая старые данные.
    from src.services.statistics import anova
    with pytest.raises(ValueError):
        anova(body['snapshot'])


def test_active_purge_blocks_assessment_creation_and_export(api, env, monkeypatch):
    ids = seed_processes_and_samples(api)
    assessment = make_completed_assessment(api, ids[:3], lot='HC-1115')

    from src.services import artifacts as artifact_module
    real_write = artifact_module.write

    def failing_write(item):
        raise OSError('磁盘只读')

    monkeypatch.setattr(artifact_module, 'write', failing_write)

    response = api['client'].post('/purges', json={'lot': 'HC-1115'}, headers=headers(api))
    assert response.status_code == 201, response.text
    job = response.json()
    assert job['status'] == 'purging'
    assert '文件清除失败' in job['error']

    # 数据库已清除，但导出被阻止。
    assert all(r['purged'] for r in sample_rows('HC-1115'))
    export = api['client'].get(f'/assessments/{assessment}/export', headers=headers(api, 'viewer'))
    assert export.status_code == 409
    assert '清退' in export.json()['detail']

    preview = api['client'].get('/purges/preview?lot=HC-1115', headers=headers(api))
    assert preview.json()['export_blocked'] is True

    # 清退中不允许新建评议。
    created = api['client'].post('/assessments', json={'title': '并发新建', 'lot': 'HC-1115',
                                                       'process_ids': ids[:2]}, headers=headers(api, 'analyst'))
    assert created.status_code == 409

    # 文件仍含旧副本；重试在修复后完成，且清理生效。
    doc = json.loads((env['artifacts'] / f'{assessment}.json').read_text())
    assert any(s.get('lot') == 'HC-1115' for g in doc['snapshot']['groups'] for s in g['samples'])

    monkeypatch.setattr(artifact_module, 'write', real_write)
    retried = api['client'].post(f"/purges/{job['id']}/retry", headers=headers(api))
    assert retried.status_code == 200, retried.text
    assert retried.json()['status'] == 'completed'
    assert retried.json()['progress']['files'] == 1
    doc = json.loads((env['artifacts'] / f'{assessment}.json').read_text())
    assert not any(s.get('lot') == 'HC-1115' for g in doc['snapshot']['groups'] for s in g['samples'])
    export = api['client'].get(f'/assessments/{assessment}/export', headers=headers(api))
    assert export.status_code == 200


def test_retry_is_idempotent_and_records_single_audit(api, env, monkeypatch):
    ids = seed_processes_and_samples(api)
    assessment = make_completed_assessment(api, ids[:2])

    from src.services import artifacts as artifact_module
    real_write = artifact_module.write
    attempts = {'count': 0}

    def write_then_fail_halfway(item):
        attempts['count'] += 1
        if attempts['count'] == 1:
            real_write(item)
            raise OSError('fsync 中断')
        return real_write(item)

    monkeypatch.setattr(artifact_module, 'write', write_then_fail_halfway)
    job = api['client'].post('/purges', json={'lot': 'HC-1115'}, headers=headers(api)).json()
    assert job['status'] == 'purging'

    first = api['client'].post(f"/purges/{job['id']}/retry", headers=headers(api))
    assert first.status_code == 200, first.text
    assert first.json()['status'] == 'completed'

    # 再次重试已完成任务被拒绝，不重复记账。
    again = api['client'].post(f"/purges/{job['id']}/retry", headers=headers(api))
    assert again.status_code == 409

    from src.config.database import SessionLocal
    from src.models.entities import Audit, PurgeJob, PurgeItem
    from sqlalchemy import select
    with SessionLocal() as db:
        logs = db.scalars(select(Audit).where(Audit.action.like('PURGE%'))).all()
        assert len(logs) == 1
        stored = db.get(PurgeJob, job['id'])
        items = db.scalars(select(PurgeItem).where(PurgeItem.purge_id == job['id'])).all()
        assert len(items) == len({(i.kind, i.ref_id) for i in items})
        assert all(i.processed for i in items)


def test_late_compute_does_not_resurrect_purged_result(api, env):
    ids = seed_processes_and_samples(api)
    from src.config.database import SessionLocal
    from src.models.entities import Assessment
    from src.services.assessments import snapshot_for
    from src.services import compute

    with SessionLocal() as db:
        snapshot = snapshot_for(db, ids[:2], 'HC-1115')
        item = Assessment(title='晚回计算', lot='HC-1115', snapshot=snapshot, status='running',
                          created_at=datetime.now(timezone.utc).isoformat())
        db.add(item)
        db.commit()
        late_id = item.id

    job = api['client'].post('/purges', json={'lot': 'HC-1115'}, headers=headers(api)).json()
    assert job['status'] == 'completed'

    # 模拟 worker 在清退后晚回：回写前看到 purged_lots，拒绝写结果并明确失败。
    with SessionLocal() as db:
        assert compute.finish(db, late_id) == 'failed'
        item = db.get(Assessment, late_id)
        assert item.status == 'failed'
        assert item.result is None
        assert '清退' in item.error
    assert not (env['artifacts'] / f'{late_id}.json').exists()


def test_late_compute_completes_normally_when_no_purge(api, env):
    ids = seed_processes_and_samples(api)
    from src.config.database import SessionLocal
    from src.models.entities import Assessment
    from src.services.assessments import snapshot_for
    from src.services import compute

    with SessionLocal() as db:
        snapshot = snapshot_for(db, ids[:2], 'HC-1115')
        item = Assessment(title='正常晚回', lot='HC-1115', snapshot=snapshot, status='running',
                          created_at=datetime.now(timezone.utc).isoformat())
        db.add(item)
        db.commit()
        task_id = item.id

    with SessionLocal() as db:
        assert compute.finish(db, task_id) == 'completed'
        assert db.get(Assessment, task_id).result['n'] == 10
    assert (env['artifacts'] / f'{task_id}.json').exists()


def test_worker_writeback_after_purge_fails_explicitly(api, env):
    ids = seed_processes_and_samples(api)
    from src.config.database import SessionLocal
    from src.models.entities import Assessment
    from src.services.assessments import snapshot_for
    from src.services import locks, artifacts

    with SessionLocal() as db:
        snapshot = snapshot_for(db, ids[:2], 'HC-1115')
        item = Assessment(title='任务晚回', lot='HC-1115', snapshot=snapshot, status='running',
                          created_at=datetime.now(timezone.utc).isoformat())
        db.add(item)
        db.commit()
        task_id = item.id

    api['client'].post('/purges', json={'lot': 'HC-1115'}, headers=headers(api))

    # 模拟 worker 回写路径：拿锁、刷新、看到清退标记后不写文件。
    with SessionLocal() as db:
        task = db.get(Assessment, task_id)
        with locks.exclusive(db):
            db.refresh(task)
            assert task.purged_lots == ['HC-1115']
            assert task.result is None
            # 若误写，会被这里的断言抓住：文件不存在且导出含旧明细。
            assert not (env['artifacts'] / f'{task_id}.json').exists()


def test_purge_requires_admin(api, env):
    seed_processes_and_samples(api)
    for role in ('analyst', 'viewer'):
        response = api['client'].post('/purges', json={'lot': 'HC-1115'}, headers=headers(api, role))
        assert response.status_code == 403


def test_rejects_all_lot_and_empty_batches(api, env):
    seed_processes_and_samples(api)
    assert api['client'].post('/purges', json={'lot': '全部'}, headers=headers(api)).status_code == 422
    assert api['client'].post('/purges', json={'lot': 'HC-9999'}, headers=headers(api)).status_code == 404


def test_sample_write_into_purged_lot_is_rejected(api, env):
    ids = seed_processes_and_samples(api)
    purged = sample_rows('HC-1115')[0]
    response = api['client'].put(f"/samples/{purged['id']}", json={
        'code': 'HC-1115-REVIVE', 'process_id': ids[0], 'lot': 'HC-1115', 'force': 30,
        'flagged': False, 'note': ''}, headers=headers(api, 'analyst'))
    # 未清退前允许修订；清退后禁止复活。
    assert response.status_code == 200
    api['client'].post('/purges', json={'lot': 'HC-1115'}, headers=headers(api))
    response = api['client'].put(f"/samples/{purged['id']}", json={
        'code': 'HC-1115-REVIVE2', 'process_id': ids[0], 'lot': 'HC-1115', 'force': 30,
        'flagged': False, 'note': ''}, headers=headers(api, 'analyst'))
    assert response.status_code == 409
    assert all(r['force'] is None for r in sample_rows('HC-1115'))


def test_multiple_assessments_and_untouched_lot_untouched(api, env):
    ids = seed_processes_and_samples(api)
    a1 = make_completed_assessment(api, ids[:2], lot='HC-1115', title='一')
    a2 = make_completed_assessment(api, ids[1:], lot='HC-2220', title='二')
    before_other = json.loads((env['artifacts'] / f'{a2}.json').read_text())

    job = api['client'].post('/purges', json={'lot': 'HC-1115'}, headers=headers(api)).json()
    assert job['progress']['assessments'] == 1
    assert job['progress']['files'] == 1

    after_other = json.loads((env['artifacts'] / f'{a2}.json').read_text())
    assert after_other == before_other


# ---------------------------------------------------------------------------
# 并发：PostgreSQL 会话级 advisory lock 必须让清退与新建评议/计算回写互斥。
# 这些用例需要真实 PostgreSQL（SQLite 只用进程内锁，无法跨线程验证）。
# ---------------------------------------------------------------------------

requires_postgres = pytest.mark.skipif(not DB_URL, reason='需要真实 PostgreSQL 验证跨会话互斥')


@requires_postgres
def test_concurrent_assessment_creation_after_purge_sees_no_stale_samples(api, env):
    """清退提交后并发新建评议：快照不能包含已清退批次。"""
    ids = seed_processes_and_samples(api)
    make_completed_assessment(api, ids[:2], title='基线')

    from concurrent.futures import ThreadPoolExecutor
    from src.config.database import SessionLocal
    from src.services import purges
    from src.schemas.business import AssessmentInput
    from src.services import assessments as assessment_service
    from src.utils.security import current_user

    class Actor:
        email = 'admin@demo.local'

    def do_purge():
        with SessionLocal() as db:
            try:
                return purges.create(db, 'HC-1115', Actor()).status
            except Exception as exc:
                return getattr(exc, 'status_code', 'error')

    def do_create(index):
        with SessionLocal() as db:
            try:
                item = assessment_service.create(db, AssessmentInput(
                    title=f'并发新建-{index}', process_ids=ids[:2], lot='HC-1115'))
                total = sum(len(g['samples']) for g in item.snapshot['groups'])
                return ('ok', total)
            except Exception as exc:  # 409 表示正确被互斥挡住
                return ('blocked', getattr(exc, 'status_code', None))

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(do_purge) for _ in range(2)]
        futures += [pool.submit(do_create, i) for i in range(6)]
        results = [f.result() for f in futures]

    purges_result = [r for r in results if isinstance(r, str) or r == 409]
    creates = [r for r in results if isinstance(r, tuple)]
    assert 'completed' in purges_result
    assert all(r in ('completed', 409) for r in purges_result)
    for kind, value in creates:
        if kind == 'ok':
            # 清退先完成时，后续快照不能再含旧明细。
            assert value == 0
        else:
            assert value == 409


@requires_postgres
def test_concurrent_purge_only_one_active_job(api, env):
    ids = seed_processes_and_samples(api)
    make_completed_assessment(api, ids[:2])

    from concurrent.futures import ThreadPoolExecutor
    from src.config.database import SessionLocal
    from src.services import purges

    class Actor:
        email = 'admin@demo.local'

    def do_purge():
        with SessionLocal() as db:
            try:
                return purges.create(db, 'HC-1115', Actor()).status
            except Exception as exc:
                return getattr(exc, 'status_code', 'error')

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(do_purge) for _ in range(5)]
        results = [f.result() for f in futures]

    from src.models.entities import PurgeJob
    from sqlalchemy import select
    with SessionLocal() as db:
        jobs = db.scalars(select(PurgeJob).where(PurgeJob.lot == 'HC-1115')).all()
    assert len(jobs) == 1
    assert jobs[0].status == 'completed'
    assert all(r in ('completed', 409) for r in results)


def test_recommendation_and_approval_keep_artifact_in_sync(api, env):
    ids = seed_processes_and_samples(api)
    from src.config.database import SessionLocal
    from src.models.entities import Assessment
    from src.services.assessments import snapshot_for
    from src.services.statistics import anova
    from src.services import artifacts

    with SessionLocal() as db:
        snapshot = snapshot_for(db, ids[:2], 'HC-1115')
        item = Assessment(title='待评议', lot='HC-1115', snapshot=snapshot, status='completed',
                          result=anova(snapshot), created_at=datetime.now(timezone.utc).isoformat())
        db.add(item)
        db.commit()
        artifacts.write(item)
        assessment = item.id

    body = {'selected_process_id': ids[0],
            'recommendation': '结合拉力下限与能耗权衡，建议进入下一轮验证并复核外观。'}
    assert api['client'].patch(f'/assessments/{assessment}/recommendation', json=body,
                               headers=headers(api, 'analyst')).status_code == 200
    document = json.loads((env['artifacts'] / f'{assessment}.json').read_text())
    assert document['recommendation'] == body['recommendation']

    assert api['client'].post(f'/assessments/{assessment}/approve',
                              headers=headers(api)).status_code == 200
    document = json.loads((env['artifacts'] / f'{assessment}.json').read_text())
    assert document['approved'] is True and document['approved_by'] == 'admin@demo.local'


def test_review_edits_blocked_while_purge_pending(api, env, monkeypatch):
    ids = seed_processes_and_samples(api)
    assessment = make_completed_assessment(api, ids[:2], lot='HC-1115')

    from src.services import artifacts as artifact_module
    real_write = artifact_module.write
    monkeypatch.setattr(artifact_module, 'write', lambda item: (_ for _ in ()).throw(OSError('只读')))
    job = api['client'].post('/purges', json={'lot': 'HC-1115'}, headers=headers(api)).json()
    assert job['status'] == 'purging'

    body = {'selected_process_id': ids[0],
            'recommendation': '结合拉力下限与能耗权衡，建议进入下一轮验证并复核外观。'}
    recommend = api['client'].patch(f'/assessments/{assessment}/recommendation', json=body,
                                    headers=headers(api, 'analyst'))
    assert recommend.status_code == 409
    assert '清退' in recommend.json()['detail']
    assert api['client'].post(f'/assessments/{assessment}/approve',
                              headers=headers(api)).status_code == 409

    monkeypatch.setattr(artifact_module, 'write', real_write)
    assert api['client'].post(f"/purges/{job['id']}/retry", headers=headers(api)).json()['status'] == 'completed'
    assert api['client'].patch(f'/assessments/{assessment}/recommendation', json=body,
                               headers=headers(api, 'analyst')).status_code == 200


def test_orphan_artifact_with_lot_is_removed(api, env):
    seed_processes_and_samples(api)
    # 一个不再关联任何评议的残留结果文件，仍带该批次逐条明细。
    orphan = env['artifacts'] / '999.json'
    orphan.write_text(json.dumps({'assessment_id': 999, 'lot': 'HC-1115', 'result': {'n': 1},
                                  'snapshot': {'lot': 'HC-1115', 'groups': [
                                      {'id': 1, 'code': 'HC-S1', 'samples': [
                                          {'code': 'HC-1115-X', 'force': 23.4, 'lot': 'HC-1115'}]}]}},
                                 ensure_ascii=False))
    job = api['client'].post('/purges', json={'lot': 'HC-1115'}, headers=headers(api)).json()
    assert job['status'] == 'completed'
    assert not orphan.exists()
