from sqlalchemy import select
from fastapi import HTTPException
from src.models.entities import Sample,Process
from src.services import locks,purges
def listing(db):return db.scalars(select(Sample).order_by(Sample.id)).all()
def save(db,data,id=None):
    if not db.get(Process,data.process_id):raise HTTPException(404,'工艺方案不存在')
    # 整个写入都在清退互斥锁内完成：若先检查后写入，清退可能在间隙完成并把明细重新写回。
    with locks.exclusive(db):
        item=db.get(Sample,id) if id else Sample()
        if not item:raise HTTPException(404,'拉力样本不存在')
        if item.purged:raise HTTPException(409,'该试条已随材料批次到期清退，不能恢复或重新写入明细')
        busy=purges.active_purge_covers(db,data.lot)
        if busy:raise HTTPException(409,f'批次 {busy.lot} 正在清退（任务 #{busy.id}），完成后才能登记拉力')
        for k,v in data.model_dump().items():setattr(item,k,v)
        db.add(item);db.commit();db.refresh(item);return item
