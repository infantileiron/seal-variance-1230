# 批次清退验证记录 · gb-3105

本批次新增「材料批次到期清退」（页面「05 / BATCH DISPOSAL」）及其并发、失败与重试保障。
证据文件全部位于 `test-artifacts/gb-3105/`，源码目录不含测试脚本。

## 运行方式

```bash
# 后端集成测试。设置真实 PostgreSQL 时启用并发用例；未设置时用 SQLite 进程内锁回退。
export SEAL_TEST_DATABASE_URL='postgresql://postgres@/postgres?host=/tmp/pgdata'
python3 -m pytest test-artifacts/gb-3105/test_purge.py -q

# 前端清退页冒烟测试（真实 ES modules + jsdom）
node test-artifacts/gb-3105/ui_purge.test.mjs

# 已启动 backend + worker（docker compose，SEED_DEMO=true）后的真实剧本
python3 test-artifacts/gb-3105/e2e_purge.py http://127.0.0.1:19204
```

`test_purge.py` 自带隔离：每个用例重建独立数据库、临时产物目录，并用可注入的假 Redis 替代队列。
`ui_purge.test.mjs` 通过 stub `fetch` 驱动真实 `frontend/src` 模块，不依赖浏览器。
`e2e_purge.py` 走真实 HTTP 与 worker，核对清退前后签核评议、导出、样本行与新评议失败态。
`fake_redis.py` 是 e2e 手动联调时的最小 RESP 服务（PING/LPUSH/BRPOP），非必需。

## 覆盖矩阵

| 需求 | 用例 |
|---|---|
| 先核对受影响样本、评议与文件 | `test_preview_lists_affected_samples_assessments_and_files`、`test_preview_requires_lot_and_existing_samples`、UI「清退预览」断言 |
| 仅清除目标批次试条编号/逐条拉力/备注 | `test_completed_purge_only_clears_target_lot_and_keeps_history` |
| 其他批次逐字不动 | 同上的 `HC-2220` 断言、`test_multiple_assessments_and_untouched_lot_untouched`、`test_cross_lot_assessment_keeps_other_lot_samples` |
| 已完成评议保留原统计量与签核 | `test_completed_purge_only_clears_target_lot_and_keeps_history`（F/checksum/approved_by/recommendation）、`test_recommendation_and_approval_keep_artifact_in_sync` |
| 后续分析排除清退数据 | `test_new_assessment_excludes_purged_samples`、`test_active_purge_blocks_assessment_creation_and_export` |
| 样本不足明确失败 | `test_new_assessment_excludes_purged_samples`（`anova` 抛错）、线上剧本「清退后样本不足验证」返回 failed |
| 跨批次评议 | `test_cross_lot_assessment_keeps_other_lot_samples` |
| 文件写入失败 → 保持清退中并阻止导出 | `test_active_purge_blocks_assessment_creation_and_export` |
| 重试幂等、不重复记账 | `test_retry_is_idempotent_and_records_single_audit` |
| 旧计算晚回不重新出现副本 | `test_late_compute_does_not_resurrect_purged_result`、`test_worker_writeback_after_purge_fails_explicitly`、`test_late_compute_completes_normally_when_no_purge` |
| 清退与新评议并发互斥 | `test_concurrent_assessment_creation_after_purge_sees_no_stale_samples`、`test_concurrent_purge_only_one_active_job`（需 PostgreSQL） |
| 预览/发起/进度页面 | `ui_purge.test.mjs`（预览数字、发起按钮、清退中进度、失败原因、重试按钮、完成态） |
| 真实 HTTP + worker 全流程 | `e2e_purge.py`（预览 200、analyst 403、签核保留、导出无泄漏、样本清空、新评议 failed、重复发起 409） |
| 角色权限 | `test_purge_requires_admin` |
| 清退后不能复活明细 | `test_sample_write_into_purged_lot_is_rejected` |
| 孤立残留文件 | `test_orphan_artifact_with_lot_is_removed` |
| 清退中禁止修改/签核评议 | `test_review_edits_blocked_while_purge_pending` |

## 结果

- 带真实 PostgreSQL：**19 passed**
- 无 PostgreSQL（SQLite 回退）：17 passed，2 个并发用例 skipped
- `ui_purge.test.mjs`：passed
- `e2e_purge.py`（真实 backend + worker，seed 数据）：通过

## 并发与一致性设计

1. **互斥**：`src/services/locks.py` 提供 `exclusive(db)`。PostgreSQL 用会话级 `pg_advisory_lock`（不受事务提交/回滚影响，适合清退这种跨多次 commit 的流程），其他方言用进程内可重入锁。
2. **清退流程**：持锁后先重复校验（并发请求不得重复建单），再登记 `purge_items`，清空样本明细 → 清理快照 → 提交 → 逐文件原子重写（写临时文件后 `os.replace`）→ 全量核对 → 记审计 → 置 completed。任一步失败保持 purging。
3. **计算回写**：`src/services/compute.py` 在计算前后各取一次锁并 `db.refresh` 检查 `purged_lots`；期间发生清退则明确失败、不写文件。
4. **导出**：以数据库为权威渲染；存在相关清退中任务时返回 409。
5. **文件核对面**：核对直接扫描整个产物目录，不依赖发起时的条目登记，防止并发写入漏网；孤立副本（评议已不存在）只删除文件。

## 线上剧本（docker compose，真实 worker）

1. seed 后存在已签核评议 #2（F=29.21，n=105）与待执行评议 #1，worker 完成 #1。
2. admin 预览 `HC-1115`：105 条待清退、2 份评议、2 个结果文件。
3. 发起清退 → completed；样本行 `code/force/note` 为空、`purged=true`；评议 #2 保留 F/checksum/签核，`purged_lots=['HC-1115']`，快照样本数 0；导出 200 且不含该批次明细。
4. analyst 清退后新建评议：快照 0 条 → 后台明确 failed「每个方案至少需要2条拉力样本」。
