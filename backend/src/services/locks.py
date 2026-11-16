"""Cross-process exclusive guard.

清退会同时改动数据库行与共享产物文件；新建评议、计算回写也必须与之互斥，
否则清退刚删除的旧副本可能被并发请求重新写回。PostgreSQL 使用会话级
advisory lock（与事务提交/回滚无关，适合跨多次 commit 的清退流程），
其余方言（本地 SQLite 测试）退化为进程内可重入锁。
"""
import hashlib
import threading
from contextlib import contextmanager

from sqlalchemy import text

_process_guard = threading.RLock()


def _key(name):
    digest = hashlib.sha256(('seal-variance:' + name).encode('utf-8')).digest()
    return int.from_bytes(digest[:8], 'big', signed=True)


def _engine_for(db):
    bind = db.get_bind()
    return getattr(bind, 'engine', bind)


@contextmanager
def exclusive(db):
    engine = _engine_for(db)
    if engine.dialect.name == 'postgresql':
        connection = engine.connect()
        key = _key('batch-purge')
        try:
            connection.execute(text('SELECT pg_advisory_lock(:key)'), {'key': key})
            connection.commit()
            yield
        finally:
            try:
                connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
                connection.commit()
            finally:
                connection.close()
    else:
        with _process_guard:
            yield
