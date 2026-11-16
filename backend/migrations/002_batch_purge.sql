-- 材料批次到期清退：拉力明细不再保留，已完成评议保留统计量与签核。
-- 既有数据库升级时执行；全新库由 SQLAlchemy Base.metadata.create_all 建表。
ALTER TABLE samples ADD COLUMN IF NOT EXISTS purged BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE samples ADD COLUMN IF NOT EXISTS purged_at VARCHAR(40);
ALTER TABLE samples ADD COLUMN IF NOT EXISTS purge_id INTEGER;
ALTER TABLE samples ALTER COLUMN code DROP NOT NULL;
ALTER TABLE samples ALTER COLUMN force DROP NOT NULL;
CREATE INDEX IF NOT EXISTS ix_samples_purged ON samples(purged);
CREATE INDEX IF NOT EXISTS ix_samples_lot ON samples(lot);

ALTER TABLE assessments ADD COLUMN IF NOT EXISTS purged_lots JSON;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS purged_at VARCHAR(40);

CREATE TABLE IF NOT EXISTS purge_jobs(
  id SERIAL PRIMARY KEY,
  lot VARCHAR(60) NOT NULL,
  status VARCHAR(20) NOT NULL,
  created_by VARCHAR(120) NOT NULL,
  created_at VARCHAR(40) NOT NULL,
  updated_at VARCHAR(40) NOT NULL,
  completed_at VARCHAR(40),
  error TEXT NOT NULL,
  verify_error TEXT NOT NULL,
  audit_recorded BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS ix_purge_jobs_lot ON purge_jobs(lot);
CREATE INDEX IF NOT EXISTS ix_purge_jobs_status ON purge_jobs(status);

CREATE TABLE IF NOT EXISTS purge_items(
  id SERIAL PRIMARY KEY,
  purge_id INTEGER NOT NULL REFERENCES purge_jobs(id),
  kind VARCHAR(20) NOT NULL,
  ref_id INTEGER NOT NULL,
  processed BOOLEAN NOT NULL DEFAULT FALSE,
  error TEXT NOT NULL,
  CONSTRAINT uq_purge_item UNIQUE (purge_id, kind, ref_id)
);
CREATE INDEX IF NOT EXISTS ix_purge_items_purge_id ON purge_items(purge_id);

ALTER TABLE samples DROP CONSTRAINT IF EXISTS samples_purge_id_fkey;
ALTER TABLE samples ADD CONSTRAINT samples_purge_id_fkey FOREIGN KEY (purge_id) REFERENCES purge_jobs(id);
