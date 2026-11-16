from sqlalchemy import String,Float,Boolean,Integer,ForeignKey,JSON,Text,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column,relationship
from src.config.database import Base
class User(Base):
    __tablename__='users'
    id:Mapped[int]=mapped_column(primary_key=True)
    email:Mapped[str]=mapped_column(String(120),unique=True)
    role:Mapped[str]=mapped_column(String(20))
    password_hash:Mapped[str]=mapped_column(String(200))
class Process(Base):
    __tablename__='processes'
    id:Mapped[int]=mapped_column(primary_key=True)
    code:Mapped[str]=mapped_column(String(50),unique=True)
    name:Mapped[str]=mapped_column(String(100))
    temperature:Mapped[float]=mapped_column(Float)
    dwell:Mapped[float]=mapped_column(Float)
    pressure:Mapped[float]=mapped_column(Float)
    note:Mapped[str]=mapped_column(Text,default='')
class Sample(Base):
    __tablename__='samples'
    id:Mapped[int]=mapped_column(primary_key=True)
    code:Mapped[str|None]=mapped_column(String(50),unique=True,nullable=True)
    process_id:Mapped[int]=mapped_column(ForeignKey('processes.id'))
    lot:Mapped[str]=mapped_column(String(60),index=True)
    force:Mapped[float|None]=mapped_column(Float,nullable=True)
    flagged:Mapped[bool]=mapped_column(Boolean,default=False)
    note:Mapped[str]=mapped_column(Text,default='')
    # 到期清退后保留行与批次/工艺归属，但试条编号、逐条拉力、备注不再保留。
    purged:Mapped[bool]=mapped_column(Boolean,default=False,index=True)
    purged_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    purge_id:Mapped[int|None]=mapped_column(ForeignKey('purge_jobs.id'),nullable=True)
class Assessment(Base):
    __tablename__='assessments'
    id:Mapped[int]=mapped_column(primary_key=True)
    title:Mapped[str]=mapped_column(String(120),unique=True)
    lot:Mapped[str]=mapped_column(String(60),default='全部')
    status:Mapped[str]=mapped_column(String(20),default='queued')
    snapshot:Mapped[dict]=mapped_column(JSON)
    result:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    error:Mapped[str]=mapped_column(Text,default='')
    recommendation:Mapped[str]=mapped_column(Text,default='')
    selected_process_id:Mapped[int|None]=mapped_column(ForeignKey('processes.id'),nullable=True)
    approved:Mapped[bool]=mapped_column(Boolean,default=False)
    approved_by:Mapped[str]=mapped_column(String(120),default='')
    # 已完成评议保留原统计量与签核；快照中到期批次的逐条明细被移除。
    purged_lots:Mapped[list|None]=mapped_column(JSON,nullable=True)
    purged_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    created_at:Mapped[str]=mapped_column(String(40))
class PurgeJob(Base):
    __tablename__='purge_jobs'
    id:Mapped[int]=mapped_column(primary_key=True)
    lot:Mapped[str]=mapped_column(String(60),index=True)
    status:Mapped[str]=mapped_column(String(20),default='purging',index=True)
    created_by:Mapped[str]=mapped_column(String(120),default='')
    created_at:Mapped[str]=mapped_column(String(40))
    updated_at:Mapped[str]=mapped_column(String(40))
    completed_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    error:Mapped[str]=mapped_column(Text,default='')
    verify_error:Mapped[str]=mapped_column(Text,default='')
    audit_recorded:Mapped[bool]=mapped_column(Boolean,default=False)
    items:Mapped[list['PurgeItem']]=relationship('PurgeItem',cascade='all, delete-orphan',order_by='PurgeItem.id')
class PurgeItem(Base):
    __tablename__='purge_items'
    __table_args__=(UniqueConstraint('purge_id','kind','ref_id',name='uq_purge_item'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    purge_id:Mapped[int]=mapped_column(ForeignKey('purge_jobs.id'),index=True)
    kind:Mapped[str]=mapped_column(String(20))  # sample / assessment / file
    ref_id:Mapped[int]=mapped_column(Integer)
    processed:Mapped[bool]=mapped_column(Boolean,default=False)
    error:Mapped[str]=mapped_column(Text,default='')
class Audit(Base):
    __tablename__='audit_logs'
    id:Mapped[int]=mapped_column(primary_key=True)
    actor:Mapped[str]=mapped_column(String(120))
    action:Mapped[str]=mapped_column(String(200))
    status:Mapped[int]=mapped_column(Integer)
    created_at:Mapped[str]=mapped_column(String(40))
