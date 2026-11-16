// UI smoke tests for the 批次清退 page, driven through the real ES modules.
// Usage: node --experimental-vm-modules test-artifacts/gb-3105/ui_purge.test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { JSDOM } from '/tmp/node_modules/jsdom/lib/api.js';

const dom = new JSDOM('<!doctype html><meta charset=utf-8><body><div id=app></div><div id=flash hidden></div></body>', {
  url: 'http://localhost/#purges',
  pretendToBeVisual: true,
});
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.location = dom.window.location;
globalThis.sessionStorage = dom.window.sessionStorage;
globalThis.FormData = dom.window.FormData;
globalThis.URL = dom.window.URL;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.Node = dom.window.Node;
globalThis.setInterval = () => 0;  // no polling inside the smoke test
globalThis.clearInterval = () => {};
globalThis.setTimeout = (fn) => 0;

const state = {
  samples: [], assessments: [], purges: [], preview: null, job: null,
  requests: [],
};

function json(body, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

globalThis.fetch = async (path, options = {}) => {
  state.requests.push([options.method || 'GET', path]);
  const url = path.replace(/^\/api/, '');
  const method = options.method || 'GET';
  if (url === '/auth/me') return json({ email: 'analyst@demo.local', role: 'analyst' });
  if (url === '/processes') return json([{ id: 1, code: 'HC-S1', name: '稳温基线', temperature: 160, dwell: 0.8, pressure: 0.32, note: '' }]);
  if (url === '/samples') return json(state.samples);
  if (url === '/assessments') return json(state.assessments);
  if (url.startsWith('/purges/preview')) return json(state.preview || {});
  if (url === '/purges' && method === 'GET') return json(state.purges);
  if (url === '/purges' && method === 'POST') {
    state.job = {
      id: 9, lot: JSON.parse(options.body).lot, status: 'purging', created_by: 'admin@demo.local',
      created_at: '2026-11-16T01:00:00+00:00', updated_at: '2026-11-16T01:00:00+00:00',
      completed_at: null, error: '文件清除失败：磁盘只读', verify_error: '',
      progress: { samples: 15, samples_total: 15, assessments: 1, assessments_total: 1, files: 0, files_total: 1, failed: [{ kind: 'file', ref_id: 4, error: '磁盘只读' }] },
    };
    state.purges = [state.job];
    return json(state.job, true, 201);
  }
  if (url.startsWith('/purges/')) return json(state.job);
  throw new Error('unexpected request ' + path);
};

function previewFixture(lot) {
  return {
    lot,
    samples: { total: 15, pending: 15, already_purged: 0, processes: ['HC-S1', 'HC-S2'], codes: ['HC-1115-001'], flagged: 3 },
    assessments: [{ id: 4, title: '已签核复核', status: 'completed', approved: true, approved_by: 'admin@demo.local', snapshot_samples: 15, already_purged: false }],
    files: [4],
    other_lots_untouched: ['HC-2220'],
    active_job: null,
    export_blocked: false,
    can_start: true,
  };
}

const sleep = (ms = 0) => new Promise((resolve) => setImmediate(resolve));
const text = () => document.body.textContent.replace(/\s+/g, ' ');

sessionStorage.setItem('seal-token', 'fake');
await import('/workspace/frontend/src/app.js');
await sleep(5);

assert.match(text(), /材料批次清退/, '清退页面导航可见');
assert.match(text(), /按材料批次发起/, '发起表单渲染');

// 预览：填写批次并提交，展示受影响样本、评议与文件。
const form = document.querySelector('#purge-form');
assert.ok(form, '存在预览表单');
state.preview = previewFixture('HC-1115');
form.elements.lot.value = 'HC-1115';
form.dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
await sleep(10);
assert.match(text(), /清退预览 · HC-1115/, '预览标题');
assert.match(text(), /待清退试条\s*15/, '待清退样本数');
assert.match(text(), /涉及评议\s*1/, '涉及评议数');
assert.match(text(), /结果文件\s*1/, '结果文件数');
assert.match(text(), /已签核复核/, '列出评议');
assert.match(text(), /其他批次不受影响：HC-2220/, '其他批次提示');
assert.match(text(), /发起清退 · HC-1115/, '发起按钮');

// 发起：清退中要显示进度与阻止导出，并允许重试。
const startButton = [...document.querySelectorAll('button')].find((b) => /发起清退/.test(b.textContent));
startButton.click();
await sleep(10);
assert.match(text(), /清退中/, '显示清退中状态');
assert.match(text(), /导出已阻止/, '预览再次显示导出阻止');
assert.match(text(), /试条明细\s*15 \/ 15/, '试条进度');
assert.match(text(), /结果文件\s*0 \/ 1/, '文件未完成');
assert.match(text(), /磁盘只读/, '展示失败原因');
assert.match(text(), /重试未完成部分/, '管理员可重试');

// 查看进度：历史列表中的任务可打开，完成后显示“清退完成”与核对提示。
state.job = { ...state.job, status: 'completed', error: '', completed_at: '2026-11-16T01:02:00+00:00',
  progress: { ...state.job.progress, files: 1, failed: [] } };
state.purges = [state.job];
const viewButton = [...document.querySelectorAll('button')].find((b) => b.dataset.purgeView);
assert.ok(viewButton, '历史列表提供查看进度');
viewButton.click();
await sleep(10);
assert.match(text(), /清退完成/, '任务完成后显示完成');
assert.match(text(), /全部核对一致/, '完成提示');
assert.ok(document.querySelector('#purge-preview'), '清退页布局存在');

console.log('ui purge tests passed');
process.exit(0);
