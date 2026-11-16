"""Live end-to-end script for 批次清退 against a running stack.

Prereqs: backend + worker running, demo data seeded (SEED_DEMO=true), and a
material batch with samples (demo: HC-1115).

Usage: python3 test-artifacts/gb-3105/e2e_purge.py http://127.0.0.1:19204
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:19204'


def call(path, method='GET', body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header('Content-Type', 'application/json')
    if token:
        request.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b'{}')


def login(email):
    return call('/auth/login', 'POST', {'email': email, 'password': 'Demo123!'})[1]['access_token']


def main():
    admin, analyst, viewer = login('admin@demo.local'), login('analyst@demo.local'), login('viewer@demo.local')
    status, assessments = call('/assessments', token=admin)
    approved = next(a for a in assessments if a['approved'])
    _, before = call(f"/assessments/{approved['id']}", token=admin)
    _, processes = call('/processes', token=admin)
    process_ids = [p['id'] for p in processes]

    print('viewer 预览:', call('/purges/preview?lot=HC-1115', token=viewer)[0])
    print('analyst 发起被拒:', call('/purges', 'POST', {'lot': 'HC-1115'}, analyst)[0])

    status, job = call('/purges', 'POST', {'lot': 'HC-1115'}, admin)
    print('发起清退:', status, job['status'], job['progress'])

    _, after = call(f"/assessments/{approved['id']}", token=admin)
    assert after['approved'] is True and after['approved_by'] == before['approved_by']
    assert after['result']['f'] == before['result']['f'], '原统计量必须保留'
    assert after['purged_lots'] == ['HC-1115']
    print('签核评议保留:', after['result']['f'], after['purged_lots'],
          '快照样本数', sum(len(g['samples']) for g in after['snapshot']['groups']))

    status, exported = call(f"/assessments/{approved['id']}/export", token=viewer)
    leaked = any(s.get('lot') == 'HC-1115' for g in exported['snapshot']['groups'] for s in g['samples'])
    print('导出:', status, '含该批次明细:', leaked)
    assert status == 200 and not leaked

    _, samples = call('/samples', token=admin)
    purged = [s for s in samples if s.get('purged')]
    assert purged and all(s['code'] is None and s['force'] is None and s['note'] == '' for s in purged)
    print('样本行已清空明细:', len(purged))

    status, created = call('/assessments', 'POST', {
        'title': '清退后样本不足验证', 'lot': 'HC-1115', 'process_ids': process_ids[:2]}, analyst)
    assert status == 202 and sum(len(g['samples']) for g in created['snapshot']['groups']) == 0
    for _ in range(15):
        time.sleep(1)
        _, detail = call(f"/assessments/{created['id']}", token=analyst)
        if detail['status'] in ('completed', 'failed'):
            break
    print('清退后新评议:', detail['status'], '|', detail['error'])
    assert detail['status'] == 'failed'

    print('重复发起被拒:', call('/purges', 'POST', {'lot': 'HC-1115'}, admin)[0])
    print('e2e 通过')


if __name__ == '__main__':
    main()
