import os
from datetime import datetime,timezone
from sqlalchemy import select
from fastapi import HTTPException
import redis
from src.models.entities import Assessment,Process,Sample
from src.services import artifacts,locks,purges
queue=redis.Redis.from_url(os.environ['REDIS_URL'])
def listing(db):return db.scalars(select(Assessment).order_by(Assessment.id.desc())).all()
def get(db,id):
    item=db.get(Assessment,id)
    if not item:raise HTTPException(404,'评议任务不存在')
    return item
def snapshot_for(db,ids,lot):
    if len(ids)!=len(set(ids)):raise HTTPException(422,'不能重复选择同一工艺')
    groups=[]
    for id in ids:
        process=db.get(Process,id)
        if not process:raise HTTPException(404,'工艺方案不存在')
        q=select(Sample).where(Sample.process_id==id,Sample.purged.is_(False)).order_by(Sample.code)
        if lot!='全部':q=q.where(Sample.lot==lot)
        samples=db.scalars(q).all()
        groups.append({'id':id,'code':process.code,'name':process.name,'temperature':process.temperature,'dwell':process.dwell,'pressure':process.pressure,'samples':[{'code':s.code,'force':s.force,'flagged':s.flagged,'lot':s.lot} for s in samples]})
    return {'lot':lot,'groups':groups}
def create(db,data):
    if len(db.scalars(select(Assessment).where(Assessment.status.in_(['queued','running']))).all())>=10:raise HTTPException(429,'最多10个待执行任务，请稍后重试')
    # 与批次清退互斥：清退进行中不允许新建评议，避免把即将清除的旧明细再次冻结进快照。
    with locks.exclusive(db):
        busy=purges.active_purge_covers(db,data.lot)
        if busy:raise HTTPException(409,f'批次 {busy.lot} 正在清退（任务 #{busy.id}），完成后才能新建评议')
        snapshot=snapshot_for(db,data.process_ids,data.lot)
        item=Assessment(title=data.title,lot=data.lot,snapshot=snapshot,status='queued',created_at=datetime.now(timezone.utc).isoformat())
        db.add(item);db.commit();db.refresh(item)
    try:queue.lpush('seal:assessments',item.id)
    except redis.RedisError:
        item.status='failed';item.error='队列不可用，请恢复后新建评议';db.commit();raise HTTPException(503,item.error)
    return item
def _refresh_artifact(db,item):
    # 结果文件是评议的共享副本，推荐/签核变化必须同步，且与清退互斥，避免旧副本被写回。
    with locks.exclusive(db):
        try:artifacts.write(item)
        except OSError as exc:raise HTTPException(503,f'结果文件更新失败：{exc}；请重试')
def _reject_if_purging(db,item):
    busy=purges.blocking_job(db,item)
    if busy:raise HTTPException(409,f'材料批次 {busy.lot} 正在清退（任务 #{busy.id}），完成并核对一致后才能修改评议')
def recommend(db,id,data):
    item=get(db,id)
    if item.status!='completed':raise HTTPException(409,'计算完成后才能起草工艺评议')
    if item.approved:raise HTTPException(409,'已签核评议冻结，请新建评议留痕')
    _reject_if_purging(db,item)
    if data.selected_process_id not in [g['id'] for g in item.snapshot['groups']]:raise HTTPException(422,'只能推荐本次比较内的工艺')
    item.selected_process_id=data.selected_process_id;item.recommendation=data.recommendation
    db.commit();_refresh_artifact(db,item);return item
def approve(db,id,user):
    item=get(db,id)
    if item.status!='completed' or not item.recommendation or not item.selected_process_id:raise HTTPException(409,'请先完成计算并填写推荐方案与业务理由')
    if item.approved:raise HTTPException(409,'评议已签核')
    _reject_if_purging(db,item)
    item.approved=True;item.approved_by=user.email;db.commit();_refresh_artifact(db,item);return item
