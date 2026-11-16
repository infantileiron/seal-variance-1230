"""按材料批次清退拉力明细。

语义：
- 只处理目标批次；其他批次的样本、快照与结果文件保持不动。
- 样本行保留批次/工艺归属，但试条编号、逐条拉力、备注被清除。
- 已完成的评议保留原统计量、推荐与签核；快照和结果文件中的该批次逐条明细被移除，
  评议记录 purged_lots 以便追溯。
- 尚无结论的评议（queued/running）在输入被清退后明确失败，不写回旧结果。
- 任一数据库或文件清除失败都保持 purging 状态并阻止相关导出；重试幂等，
  只在全部核对一致后才标记 completed 并记一次清退台账。
"""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from src.constants.enums import PURGE_STATUS
from src.models.entities import Assessment, Audit, Process, PurgeItem, PurgeJob, Sample
from src.services import artifacts, locks
PURGING, COMPLETED = PURGE_STATUS


def _now():
    return datetime.now(timezone.utc).isoformat()


def lot_samples(db, lot):
    return db.scalars(select(Sample).where(Sample.lot == lot).order_by(Sample.id)).all()


def _has_lot_samples(snapshot, lot):
    groups = (snapshot or {}).get('groups') or []
    return any(isinstance(s, dict) and s.get('lot') == lot
               for group in groups for s in (group.get('samples') or []))


def affected_assessments(db, lot):
    return [item for item in db.scalars(select(Assessment).order_by(Assessment.id)).all()
            if _has_lot_samples(item.snapshot, lot)]


def active_job(db, lot):
    return db.scalar(select(PurgeJob).where(PurgeJob.lot == lot, PurgeJob.status == PURGING)
                     .order_by(PurgeJob.id.desc()))


def active_purge_covers(db, lot):
    """创建评议前检查：目标批次（或“全部”）是否仍有进行中的清退。"""
    query = select(PurgeJob).where(PurgeJob.status == PURGING)
    if lot != '全部':
        query = query.where(PurgeJob.lot == lot)
    return db.scalar(query.order_by(PurgeJob.id.desc()))


def _assessment_lots(item):
    lots = {item.lot}
    for group in (item.snapshot or {}).get('groups') or []:
        for sample in group.get('samples') or []:
            if isinstance(sample, dict) and sample.get('lot'):
                lots.add(sample['lot'])
    lots.update(item.purged_lots or [])
    return lots


def blocking_job(db, item):
    """导出拦截：有任何仍在清退中的批次与这份评议相关，就不允许导出。"""
    jobs = db.scalars(select(PurgeJob).where(PurgeJob.status == PURGING)).all()
    if not jobs:
        return None
    lots = _assessment_lots(item)
    for job in jobs:
        if job.lot in lots:
            return job
    return None


def preview(db, lot):
    lot = (lot or '').strip()
    if not lot:
        raise HTTPException(422, '请填写材料批次')
    samples = lot_samples(db, lot)
    if not samples:
        raise HTTPException(404, '该材料批次没有拉力样本，无需清退')
    affected = affected_assessments(db, lot)
    files = [item.id for item in affected if artifacts.path_for(item.id).exists()]
    process_codes = {p.id: p.code for p in db.scalars(select(Process)).all()}
    pending = [s for s in samples if not s.purged]
    purged = [s for s in samples if s.purged]
    other_lots = {s.lot for s in db.scalars(select(Sample)).all() if s.lot != lot}
    job = active_job(db, lot)
    return {
        'lot': lot,
        'samples': {
            'total': len(samples),
            'pending': len(pending),
            'already_purged': len(purged),
            'processes': sorted({process_codes.get(s.process_id, '—') for s in samples}),
            'codes': [s.code for s in pending],
            'flagged': sum(1 for s in pending if s.flagged),
        },
        'assessments': [{
            'id': item.id,
            'title': item.title,
            'status': item.status,
            'approved': item.approved,
            'approved_by': item.approved_by,
            'snapshot_samples': sum(1 for group in (item.snapshot or {}).get('groups') or []
                                    for s in group.get('samples') or []
                                    if isinstance(s, dict) and s.get('lot') == lot),
            'already_purged': lot in (item.purged_lots or []),
        } for item in affected],
        'files': files,
        'other_lots_untouched': sorted(other_lots),
        'active_job': serialize(job) if job else None,
        'export_blocked': bool(job),
        'can_start': bool(pending) and job is None,
    }


def serialize(job):
    if not job:
        return None
    items = job.items if hasattr(job, 'items') else []
    return {
        'id': job.id,
        'lot': job.lot,
        'status': job.status,
        'created_by': job.created_by,
        'created_at': job.created_at,
        'updated_at': job.updated_at,
        'completed_at': job.completed_at,
        'error': job.error,
        'verify_error': job.verify_error,
        'progress': {
            'samples': sum(1 for i in items if i.kind == 'sample' and i.processed),
            'samples_total': sum(1 for i in items if i.kind == 'sample'),
            'assessments': sum(1 for i in items if i.kind == 'assessment' and i.processed),
            'assessments_total': sum(1 for i in items if i.kind == 'assessment'),
            'files': sum(1 for i in items if i.kind == 'file' and i.processed),
            'files_total': sum(1 for i in items if i.kind == 'file'),
            'failed': [{'kind': i.kind, 'ref_id': i.ref_id, 'error': i.error}
                       for i in items if i.error],
        },
    }


def listing(db):
    return db.scalars(select(PurgeJob).order_by(PurgeJob.id.desc())).all()


def get(db, id):
    job = db.get(PurgeJob, id)
    if not job:
        raise HTTPException(404, '清退任务不存在')
    return job


def create(db, lot, user):
    lot = (lot or '').strip()
    if not lot:
        raise HTTPException(422, '请填写材料批次')
    if lot == '全部':
        raise HTTPException(422, '清退必须指定具体材料批次，不能使用“全部”')
    if not lot_samples(db, lot):
        raise HTTPException(404, '该材料批次没有拉力样本，无需清退')
    with locks.exclusive(db):
        # 拿到互斥锁后重新校验，避免并发请求都通过锁外检查而重复建单。
        samples = lot_samples(db, lot)
        if not samples:
            raise HTTPException(404, '该材料批次没有拉力样本，无需清退')
        if all(s.purged for s in samples) and not affected_assessments(db, lot):
            raise HTTPException(409, '该材料批次已清退完成，无需重复发起')
        current = active_job(db, lot)
        if current:
            raise HTTPException(409, f'批次 {lot} 已在清退中（任务 #{current.id}），请查看进度或重试')
        job = PurgeJob(lot=lot, status=PURGING, created_by=user.email,
                       created_at=_now(), updated_at=_now(), error='', verify_error='')
        db.add(job)
        db.commit()
        db.refresh(job)
        _run_locked(db, job)
        return job


def retry(db, id):
    job = get(db, id)
    if job.status == COMPLETED:
        raise HTTPException(409, '该批次清退已完成，无需重试')
    with locks.exclusive(db):
        job = db.get(PurgeJob, id)
        if job.status == COMPLETED:
            raise HTTPException(409, '该批次清退已完成，无需重试')
        job.error = ''
        job.verify_error = ''
        job.updated_at = _now()
        db.commit()
        _run_locked(db, job)
        return job


def _ensure_items(db, job):
    existing = {(i.kind, i.ref_id) for i in db.scalars(
        select(PurgeItem).where(PurgeItem.purge_id == job.id)).all()}
    def add(kind, ref_id):
        if (kind, ref_id) not in existing:
            db.add(PurgeItem(purge_id=job.id, kind=kind, ref_id=ref_id, processed=False, error=''))
            existing.add((kind, ref_id))
    for sample in lot_samples(db, job.lot):
        add('sample', sample.id)
    for item in affected_assessments(db, job.lot):
        add('assessment', item.id)
        if artifacts.path_for(item.id).exists():
            add('file', item.id)
    # 产物目录里可能还有评审关联文件之外的残留副本（例如并发写入的旧结果），
    # 一并纳入条目，重试时才能真正清除。
    for target in sorted(artifacts.root().glob('*.json')):
        document = artifacts.read(target.stem)
        if document is not None and artifacts.contains_lot(document, job.lot):
            try:
                add('file', int(target.stem))
            except ValueError:
                continue
    db.flush()
    return {(i.kind, i.ref_id): i for i in db.scalars(
        select(PurgeItem).where(PurgeItem.purge_id == job.id)).all()}


def _purge_sample_rows(db, job, items):
    for sample in lot_samples(db, job.lot):
        if not sample.purged:
            sample.code = None
            sample.force = None
            sample.note = ''
            sample.flagged = False
            sample.purged = True
            sample.purged_at = sample.purged_at or _now()
            sample.purge_id = sample.purge_id or job.id
        item = items.get(('sample', sample.id))
        if item:
            item.processed = True
            item.error = ''


def _purge_snapshots(db, job, items):
    for item in affected_assessments(db, job.lot):
        snapshot = item.snapshot or {}
        groups = snapshot.get('groups') or []
        changed = False
        cleaned = []
        for group in groups:
            kept = [s for s in (group.get('samples') or [])
                    if not (isinstance(s, dict) and s.get('lot') == job.lot)]
            if len(kept) != len(group.get('samples') or []):
                changed = True
            cleaned.append({**group, 'samples': kept})
        purged_lots = list(item.purged_lots or [])
        if changed or item.lot == job.lot:
            if changed:
                item.snapshot = {**snapshot, 'groups': cleaned}
            if job.lot not in purged_lots:
                purged_lots.append(job.lot)
                item.purged_lots = purged_lots
            item.purged_at = item.purged_at or _now()
        if changed and item.status in ('queued', 'running'):
            item.status = 'failed'
            item.result = None
            item.error = (f'材料批次 {job.lot} 的逐条明细已到期清退，本次计算不再写回；'
                          '若需继续比较，请核对剩余样本后重新提交。')
        entry = items.get(('assessment', item.id))
        if entry:
            entry.processed = True
            entry.error = ''


def _purge_files(db, job, items):
    for entry in [i for i in items.values() if i.kind == 'file' and not i.processed]:
        target = artifacts.path_for(entry.ref_id)
        if not target.exists():
            entry.processed = True
            entry.error = ''
            db.commit()
            continue
        document = artifacts.read(entry.ref_id)
        if document is None:
            entry.error = '结果文件无法读取，不能确认明细已清除'
            db.commit()
            raise OSError(entry.error)
        if artifacts.contains_lot(document, job.lot):
            item = db.get(Assessment, entry.ref_id)
            if not item:
                # 孤立副本（评议已不存在）直接删除，避免旧明细长期滞留。
                try:
                    target.unlink()
                except OSError as exc:
                    entry.error = f'孤立结果文件删除失败：{exc}'
                    db.commit()
                    raise
            else:
                try:
                    artifacts.write(item)
                except OSError as exc:
                    entry.error = f'结果文件写入失败：{exc}'
                    db.commit()
                    raise
        entry.processed = True
        entry.error = ''
        db.commit()


def _verify(db, job, items):
    problems = []
    for sample in lot_samples(db, job.lot):
        if not sample.purged or sample.code is not None or sample.force is not None or sample.note:
            problems.append(f'样本 #{sample.id} 仍保留明细')
    for item in db.scalars(select(Assessment).order_by(Assessment.id)).all():
        if _has_lot_samples(item.snapshot, job.lot):
            problems.append(f'评议 #{item.id} 快照仍含批次明细')
    for entry in items.values():
        if not entry.processed:
            problems.append(f'{entry.kind} #{entry.ref_id} 尚未处理完成')
    # 直接扫描整个产物目录：任何文件残留该批次明细都不算完成，
    # 不依赖发起时登记的条目集合，防止并发写入漏网。
    for target in sorted(artifacts.root().glob('*.json')):
        document = artifacts.read(target.stem)
        if document is None:
            problems.append(f'结果文件 {target.name} 无法读取')
        elif artifacts.contains_lot(document, job.lot):
            problems.append(f'结果文件 {target.name} 仍含批次明细')
    return problems


def _record_audit(db, job):
    if job.audit_recorded:
        return
    db.add(Audit(actor=job.created_by, action=f'PURGE lot={job.lot} job={job.id}',
                 status=200, created_at=_now()))
    job.audit_recorded = True


def _run_locked(db, job):
    job = db.get(PurgeJob, job.id)
    if job is None or job.status == COMPLETED:
        return job
    job.updated_at = _now()
    job.error = ''
    job.verify_error = ''
    try:
        items = _ensure_items(db, job)
        _purge_sample_rows(db, job, items)
        _purge_snapshots(db, job, items)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 保留清退中，等待重试
        db.rollback()
        job = db.get(PurgeJob, job.id)
        job.error = f'数据库清除失败：{exc}'
        job.updated_at = _now()
        db.commit()
        return job
    try:
        _purge_files(db, job, items)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        job = db.get(PurgeJob, job.id)
        job.error = f'文件清除失败：{exc}'
        job.updated_at = _now()
        db.commit()
        return job
    try:
        items = {(i.kind, i.ref_id): i for i in db.scalars(
            select(PurgeItem).where(PurgeItem.purge_id == job.id)).all()}
        problems = _verify(db, job, items)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        job = db.get(PurgeJob, job.id)
        job.error = f'清退核对失败：{exc}'
        job.updated_at = _now()
        db.commit()
        return job
    job = db.get(PurgeJob, job.id)
    if problems:
        job.status = PURGING
        job.verify_error = '；'.join(problems)
        job.updated_at = _now()
        db.commit()
        return job
    job.status = COMPLETED
    job.completed_at = _now()
    job.updated_at = job.completed_at
    job.error = ''
    job.verify_error = ''
    _record_audit(db, job)
    db.commit()
    return job
