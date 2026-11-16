"""Shared result-file (artifact) storage.

结果文件与数据库评议快照一样，可能包含逐条拉力副本。清退时必须与数据库
一起清除，并且写入需要原子完成，避免中途失败留下半份旧副本。
"""
import json
import os
from pathlib import Path


def root():
    return Path(os.getenv('ARTIFACTS_DIR', '/app/artifacts'))


def path_for(assessment_id):
    return root() / f'{assessment_id}.json'


def payload(item):
    """Canonical artifact content: 评议元信息 + 快照 + 统计结果。"""
    return {
        'assessment_id': item.id,
        'title': item.title,
        'lot': item.lot,
        'status': item.status,
        'created_at': item.created_at,
        'snapshot': item.snapshot,
        'result': item.result,
        'recommendation': item.recommendation,
        'selected_process_id': item.selected_process_id,
        'approved': item.approved,
        'approved_by': item.approved_by,
        'purged_lots': list(item.purged_lots or []),
        'purged_at': item.purged_at,
    }


def render(item):
    return json.dumps(payload(item), ensure_ascii=False, indent=2)


def write(item):
    """原子写入结果文件；失败抛出 OSError，由调用方决定清退状态。"""
    target = path_for(item.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f'.{target.name}.{os.getpid()}.tmp')
    try:
        with open(temp, 'w', encoding='utf-8') as handle:
            handle.write(render(item))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass
    return target


def read(assessment_id):
    target = path_for(assessment_id)
    try:
        return json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def contains_lot(document, lot):
    """递归检查文件里是否还留着指定批次的逐条样本。"""
    if isinstance(document, dict):
        samples = document.get('samples')
        if isinstance(samples, list) and any(isinstance(s, dict) and s.get('lot') == lot for s in samples):
            return True
        return any(contains_lot(value, lot) for value in document.values())
    if isinstance(document, list):
        return any(contains_lot(value, lot) for value in document)
    return False
