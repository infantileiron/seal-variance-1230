"""计算任务的回写路径。

worker 在主循环里调用，测试也可直接调用。回写前后都重新获取清退互斥锁并刷新任务，
确保清退已移除的批次不会因为晚回的计算而重新出现旧结果或旧结果文件：
- 计算开始前已清退：直接明确失败，不把“样本不足”当作普通计算错误。
- 计算期间发生清退：回写前再核对一次，放弃写入。
"""
from src.services import artifacts, locks
from src.services.statistics import anova

PURGED_ERROR = ('材料批次 {lots} 的逐条明细已到期清退，本次计算不再写回；'
                '若需继续比较，请核对剩余样本后重新提交。')


def _purged_lots(db, task):
    with locks.exclusive(db):
        db.refresh(task)
        return sorted(set(task.purged_lots or []))


def write_back(db, task):
    """执行计算并回写；返回最终状态。调用方负责提交。"""
    try:
        purged = _purged_lots(db, task)
        if purged:
            task.status = 'failed'
            task.result = None
            task.error = PURGED_ERROR.format(lots='、'.join(purged))
            return task.status
        result = anova(task.snapshot)
        with locks.exclusive(db):
            db.refresh(task)
            purged = sorted(set(task.purged_lots or []))
            if purged:
                task.status = 'failed'
                task.result = None
                task.error = PURGED_ERROR.format(lots='、'.join(purged))
                return task.status
            task.result = result
            task.status = 'completed'
            try:
                artifacts.write(task)
            except OSError as exc:
                task.status = 'failed'
                task.result = None
                task.error = f'结果文件写入失败：{exc}；请稍后重试'
    except Exception as exc:  # noqa: BLE001 - 明确失败，不静默
        task.result = None
        task.error = str(exc)
        task.status = 'failed'
    return task.status


def finish(db, task_id):
    """worker 使用：按 id 加载任务、运行回写并提交。"""
    from src.models.entities import Assessment

    task = db.get(Assessment, task_id)
    if not task:
        return None
    status = write_back(db, task)
    db.commit()
    return status
