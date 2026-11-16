import time,traceback
from pathlib import Path
from sqlalchemy import select
from src.config.database import SessionLocal
from src.models.entities import Assessment
from src.services import compute
from src.services.assessments import queue
with SessionLocal() as db:
    for task in db.scalars(select(Assessment).where(Assessment.status=='running')).all():task.status='queued'
    db.commit()
print('seal variance worker started',flush=True)
while True:
    Path('/tmp/worker-heartbeat').touch()
    try:
        queue.brpop('seal:assessments',timeout=2)
        with SessionLocal() as db:
            task=db.scalar(select(Assessment).where(Assessment.status=='queued').order_by(Assessment.id).with_for_update(skip_locked=True))
            if not task:continue
            task.status='running';db.commit();task_id=task.id
        with SessionLocal() as db:
            status=compute.finish(db,task_id)
        if status:print(f'assessment={task_id} status={status}',flush=True)
    except Exception:traceback.print_exc();time.sleep(2)
