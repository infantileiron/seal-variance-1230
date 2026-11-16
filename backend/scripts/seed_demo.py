import random,json
from datetime import datetime,timezone
from sqlalchemy import select,func
from src.config.database import Base,engine,SessionLocal
from src.models.entities import User,Process,Sample,Assessment
from src.utils.security import hash_password
from src.services.assessments import snapshot_for

def seed():
    Base.metadata.create_all(engine);rng=random.Random(3105)
    with SessionLocal() as db:
        for role in ('admin','analyst','viewer'):
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):db.add(User(email=email,role=role,password_hash=hash_password('Demo123!')))
        db.commit()
        for j,(name,temp,dwell,pressure,mean) in enumerate([('稳温基线',160,.8,.32,22.5),('延时封合',160,1.2,.32,25.8),('升温短封',175,.65,.32,24.1)],1):
            code=f'HC-S{j}';process=db.scalar(select(Process).where(Process.code==code))
            if not process:
                process=Process(code=code,name=name,temperature=temp,dwell=dwell,pressure=pressure,note='BOPP/PE复合膜，批号HC-1115，同批独立取样');db.add(process);db.flush()
            for i in range(35):
                code=f'HC-S{j}-{i+1:03}';value=round(max(1,rng.gauss(mean,1.7)),2)
                if not db.scalar(select(Sample).where(Sample.code==code)):db.add(Sample(code=code,process_id=process.id,lot='HC-1115',force=value,flagged=i==0,note='首件标记，保留纳入分析' if i==0 else ''))
        db.commit()
        title='HC-1115 三方案首轮复核'
        if not db.scalar(select(Assessment).where(Assessment.title==title)):
            ids=db.scalars(select(Process.id).where(Process.code.in_(['HC-S1','HC-S2','HC-S3'])).order_by(Process.id)).all()
            db.add(Assessment(title=title,lot='HC-1115',snapshot=snapshot_for(db,ids,'HC-1115'),status='queued',created_at=datetime.now(timezone.utc).isoformat()));db.commit()
        # 已签核的历史评议：供清退场景核对“保留原统计量与签核，仅移除快照明细”。
        approved_title='HC-1115 封合参数复核（已签核）'
        if not db.scalar(select(Assessment).where(Assessment.title==approved_title)):
            from src.services.statistics import anova
            from src.services import artifacts
            ids=db.scalars(select(Process.id).where(Process.code.in_(['HC-S1','HC-S2','HC-S3'])).order_by(Process.id)).all()
            snapshot=snapshot_for(db,ids,'HC-1115')
            item=Assessment(title=approved_title,lot='HC-1115',snapshot=snapshot,status='completed',result=anova(snapshot),
                            recommendation='建议 HC-S2 进入下一轮试产验证；额外保压时间可能增加能耗，需继续观察外观与封口完整性，不能只根据 p 值选型。',
                            selected_process_id=ids[1],approved=True,approved_by='admin@demo.local',
                            created_at=datetime.now(timezone.utc).isoformat())
            db.add(item);db.commit();db.refresh(item)
            try:artifacts.write(item)
            except OSError:pass
        counts={t.__tablename__:db.scalar(select(func.count()).select_from(t)) for t in [User,Process,Sample,Assessment]};print(json.dumps(counts));return counts
if __name__=='__main__':seed()
