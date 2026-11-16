# 和川包装 · 包装工艺方差评估

面向封合工艺工程师的小型业务应用：登记工艺参数与拉力测量，提交单因素 ANOVA，结合工程条件起草推荐，由负责人签核冻结并导出完整评议。

## 真实场景

模拟客户为和川包装工艺研究室：6 位工艺工程师、1 位负责人，比较同批 BOPP/PE 复合膜的三种封口参数。原先由实验员把试条拉力抄入工作簿，工程师手算平均数后在聊天里确认参数，负责人另存结论。三个痛点是异常样本是否剔除说不清、工艺参数与历史结论脱节、只看平均值而忽略组内变异。

上游是封口机参数单与独立试条测量记录；本应用替代人工统计、证据归档与结论确认；下游为负责人批准下一轮试产验证。应用不把统计显著性直接当作工艺合格判定。

## 业务模块

1. **方案库**：维护方案编号、封口温度、保压时间、压力、材料说明；支持修订。
2. **拉力录入**：按方案与材料批次登记最大拉力，支持筛选、异常标记与修订；标记样本保留在统计中。
3. **方差分析**：选择至少两个方案与材料批次，后台 worker 计算 SS、F、p、η²；每次提交冻结参数和测量快照。
4. **工艺评议**：查看组别均值/标准差，填写推荐方案和至少20字业务理由，负责人签核后冻结，下载 JSON，负责人可查看操作审计。
5. **批次清退**：拉力明细到期后按材料批次清退，先预览受影响样本、评议与结果文件，再清除试条编号、逐条拉力与备注；其他批次不动。

### 角色与账号

| 角色 | 账号 | 权限与典型动作 |
|---|---|---|
| admin / 工艺负责人 | admin@demo.local | 所有业务编辑、计算、推荐、签核冻结、批次清退、审计 |
| analyst / 工艺工程师 | analyst@demo.local | 方案与样本新增/修订、提交计算、起草推荐；不能签核 |
| viewer / 只读观察员 | viewer@demo.local | 查看各模块与清退进度、下载已完成结果；不能编辑、提交任务或发起清退 |

三个演示账号密码均为 `Demo123!`。权限由后端 JWT 与数据库角色共同校验；前端同步隐藏写入入口。

## 快速启动

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
# 首次启动自动 seed，手动重复执行同样安全：
docker compose exec -T backend python -m scripts.seed_demo
```

访问 [前端](http://localhost:18204)、[后端 OpenAPI](http://localhost:19204/docs)。数据库映射 `localhost:17704`。五服务为 frontend、backend、worker、db、redis，均配置健康检查；后端与 worker 等待数据库和 Redis，前端等待后端。worker 每2秒检查队列与数据库待处理任务，处理后写数据库与共享产物卷。单 worker，重启时将中断任务恢复待执行。测试完成可 `docker compose down -v --remove-orphans`（删除本项目数据卷）。

Docker 默认网络池不足时使用独立可配置子网，默认 `10.253.105.0/24`；若与部署环境重叠，在 `.env` 修改 `NETWORK_SUBNET`。所有服务容器名基于 `COMPOSE_PROJECT_NAME`。

### 环境变量

| 变量 | 默认值 / 用途 |
|---|---|
| COMPOSE_PROJECT_NAME | seal-variance |
| FRONTEND_PORT / BACKEND_PORT / DB_PORT | 18204 / 19204 / 17704 |
| POSTGRES_PASSWORD | seal_local_pass，数据库凭据 |
| JWT_SECRET | 示例本地 JWT 签名密钥，可在 .env 替换 |
| SEED_DEMO | true，启动时执行幂等 seed；失败记日志而不阻塞服务 |
| NETWORK_SUBNET | 10.253.105.0/24 |

## 技术与本地开发

| 层 | 技术 |
|---|---|
| 前端 | 原生 HTML、CSS、JavaScript ES modules，Nginx 1.27 |
| 后端 | Python 3.11、FastAPI 0.115.6、SQLAlchemy 2.0.36、PyJWT |
| 计算 | SciPy 1.14.1 F 分布尾概率；其余方差分解由 Python 实算 |
| 队列/数据 | Redis 7、PostgreSQL 16 |

前端无构建步骤。后端可在 `backend/` 创建虚拟环境并安装 `requirements.txt`，设置 `DATABASE_URL`、`REDIS_URL`、`JWT_SECRET`（可选 `ARTIFACTS_DIR`，默认 `/app/artifacts`）后运行 `uvicorn src.main:app --reload`，另开终端 `python -m src.worker`。既有数据库升级执行 `backend/migrations/002_batch_purge.sql`。开发时前端请求 `/api/`，Nginx 去掉此代理前缀；直接访问后端时路由不带 `/api`。数据库启动时由 SQLAlchemy 建表，`backend/migrations/001_initial.sql` 提供同等初始 DDL。

## 批次清退口径

清退以材料批次为单位，仅负责人（admin）可发起；任何人可预览范围、查看进度。执行口径：

- **样本行**：保留行、工艺归属、材料批次与清退时间，但试条编号、逐条拉力、备注与异常标记被清除（数据库列置空），行本身用于核对条数。
- **评议**：已完成评议保留原统计量（SS/F/p/η² 与 checksum）、推荐与签核，只从快照和结果文件中移除该批次的逐条明细，并记录 `purged_lots`。尚未完成（queued/running）的评议因输入失效明确失败，不写回旧结果。
- **其他批次**：样本、快照与结果文件逐字不变。
- **结果文件**：与数据库同步清除；清退时扫描整个产物目录，孤立残留副本一并处理。
- **并发**：清退、新建评议、样本写入与计算回写共用同一互斥锁（PostgreSQL 会话级 advisory lock，SQLite 退化为进程内锁），旧副本不会在清退后重新出现。
- **失败与重试**：数据库或任一文件清除失败都保持「清退中」，阻止相关导出与该批次的新建评议；重试幂等，只补做未完成条目。全部核对一致后才显示「清退完成」并记一次操作审计，重试不重复记账。

清退不可撤销；清退后该批次不参与新评议，样本不足时后台任务明确失败。

## 算法口径与失败态

输入为选定工艺与材料批次的全部有效拉力样本（单位 N，0 < N ≤ 200）。组数 2–8，每组至少2条；选重复工艺或不存在的方案时直接拒绝请求，样本不足时后台任务记为 failed，不静默移除小样本组。`lot=全部` 会跨材料批次合并，需工程师先确认可比性。

对 k 组、总样本数 N，整体均值为 x̄，组均值为 x̄ᵢ：

- SS_between = Σ nᵢ(x̄ᵢ − x̄)²；SS_within = ΣΣ(xᵢⱼ − x̄ᵢ)²。
- SS_total = SS_between + SS_within，df_between = k−1，df_within = N−k。
- F = (SS_between / df_between) / (SS_within / df_within)。
- p = SciPy F 分布 survival function；保留完整浮点，前端极小值科学计数法展示。
- η² = SS_between / SS_total，组均值差异的整体检验阈值 α=0.05。

组内方差为零（SS_within ≤ 1e−12）时明确失败，避免无穷 F 进入 JSON。所有标记异常的样本仍纳入；修订测量只影响新任务。结果保存组别 n、均值、样本标准差、最小/最大、异常标记数、所有平方和/自由度/均方与输入 SHA-256。

假设为独立样本、组内近似正态和方差齐性；本版不执行正态/方差齐性自动检验或事后多重比较，因此整体显著不说明具体哪两组不同。推荐需人工考虑产品下限、外观、热损伤与能耗，并安排验证。只有本次比较中的工艺可被推荐。签核需先有完整推荐；签核后评议不可覆盖，新建任务留痕。

## Seed 与一天的工作剧本

随机种子 `3105`。自然键为用户邮箱、工艺编号、样本编号、评议标题；已有对象不覆盖，重复 seed 不增行、不覆盖用户修改。初始3账号、3方案、105样本、1个待执行评议（worker 真实计算）与 1 个已签核评议（供清退核对）。

| 方案 | 温度 ℃ | 保压 s | 压力 MPa | 样本 |
|---|---:|---:|---:|---:|
| HC-S1 稳温基线 | 160 | 0.80 | 0.32 | 35 |
| HC-S2 延时封合 | 160 | 1.20 | 0.32 | 35 |
| HC-S3 升温短封 | 175 | 0.65 | 0.32 | 35 |

1. 08:50 观察员查看材料批次 `HC-1115` 的三方案台账。
2. 09:00 工程师核对 `HC-S1` 参数 160℃ / 0.8s / 0.32MPa。
3. 09:10 在 HC-S1 下录入 `HC-S1-036`，批次 HC-1115，最大拉力 23.40N。
4. 09:20 核对首件异常标记；本版保留该样本并在说明中解释。
5. 09:30 在方差分析选择 HC-S1、HC-S2、HC-S3 与 HC-1115，标题为“HC-1115 封合参数复核”。
6. 09:40 等待后台完成，核对平方和守恒、F/p/η²与组别标准差。
7. 10:00 起草推荐 HC-S2，说明额外保压时间可能增加能耗，需继续验证外观和封口完整性，不能只根据 p 值选型。
8. 10:10 负责人登录，检查推荐理由后签核，评议冻结。
9. 10:20 观察员下载 `seal-variance-<id>.json`，包含快照与签核者，交给试产负责人。
10. 到期后负责人在「批次清退」预览并清退 `HC-1115`：已完成评议保留原统计量与签核，逐条拉力明细与结果文件副本清除，导出不再含该批次明细；其他批次不动。

## 目录与共享枚举

```
frontend/src/  api / stores / types / components/common / hooks / pages / router / utils / constants
backend/src/   routes / controllers / services / models / schemas / middlewares / utils / constants / config
backend/scripts/seed_demo.py
backend/migrations/001_initial.sql
```

共享枚举为 ROLES、TASK_STATUS/STATUS 与 PURGE_STATUS：后端 `src/constants/enums.py`，认证判断在 `src/utils/security.py`，状态在 `src/services/assessments.py`、`src/services/purges.py` 与 `src/services/compute.py`；前端 `src/constants/enums.js`、`src/components/common/status.js`、`src/components/purge.js`、`src/app.js`。角色枚举 admin/analyst/viewer；任务状态 queued/running/completed/failed；清退状态 purging/completed，另有 approved 布尔业务状态。

## 已知范围

小型项目逐条登记，不含 Excel/CSV 批量导入、删除、分页、自动取样设备连接、事后多重比较或外部电子签名。评议是本地数据库签核。批次清退只清除拉力明细，不删除评议、方案或样本行；没有自动到期定时任务，由负责人按批次手动发起。时间戳以 UTC 留痕。前端字体从 Google Fonts 加载，离线时使用指定 serif 回退；核心业务不依赖字体服务。测试脚本、截图与 TESTING.md 位于批次 `test-artifacts/gb-3105/`，源码目录不存放这些证据。

MIT License，见 LICENSE。
