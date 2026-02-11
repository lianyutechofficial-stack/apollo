-- Apollo Gateway — Supabase/PostgreSQL Schema
-- 在 Supabase SQL Editor 中执行此文件建表

-- 全局配置
CREATE TABLE IF NOT EXISTS admin_config (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Kiro 凭证池
CREATE TABLE IF NOT EXISTS tokens (
  id             TEXT PRIMARY KEY,
  refresh_token  TEXT DEFAULT '',
  access_token   TEXT DEFAULT '',
  expires_at     TEXT DEFAULT '',
  region         TEXT DEFAULT 'us-east-1',
  client_id_hash TEXT DEFAULT '',
  client_id      TEXT DEFAULT '',
  client_secret  TEXT DEFAULT '',
  auth_method    TEXT DEFAULT '',
  provider       TEXT DEFAULT '',
  profile_arn    TEXT DEFAULT '',
  status         TEXT DEFAULT 'active',
  added_at       TIMESTAMPTZ DEFAULT now(),
  last_used      TIMESTAMPTZ,
  use_count      INTEGER DEFAULT 0
);

-- 用户
CREATE TABLE IF NOT EXISTS users (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  usertoken      TEXT UNIQUE NOT NULL,
  status         TEXT DEFAULT 'active',
  assigned_token_id TEXT DEFAULT '',
  created_at     TIMESTAMPTZ DEFAULT now(),
  last_used      TIMESTAMPTZ,
  request_count  INTEGER DEFAULT 0,
  token_balance  BIGINT DEFAULT 0,
  token_granted  BIGINT DEFAULT 0,
  quota_daily_tokens    INTEGER DEFAULT 0,
  quota_monthly_tokens  INTEGER DEFAULT 0,
  quota_daily_requests  INTEGER DEFAULT 0
);

-- 用户 API Keys
CREATE TABLE IF NOT EXISTS user_apikeys (
  apikey  TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_apikeys_user ON user_apikeys(user_id);

-- 用量记录（逐条）
CREATE TABLE IF NOT EXISTS usage_records (
  id               BIGSERIAL PRIMARY KEY,
  user_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  model            TEXT NOT NULL,
  prompt_tokens    INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  token_id         TEXT DEFAULT '',
  recorded_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_records(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_records(recorded_at);
CREATE INDEX IF NOT EXISTS idx_usage_token ON usage_records(token_id);

-- 模型映射（combo + alias 合并）
CREATE TABLE IF NOT EXISTS model_mappings (
  name       TEXT PRIMARY KEY,
  type       TEXT NOT NULL CHECK (type IN ('combo', 'alias')),
  targets    JSONB NOT NULL,  -- combo: ["model1","model2"], alias: "model1"
  is_builtin BOOLEAN DEFAULT false
);

-- Cursor Pro 登录凭证池
CREATE TABLE IF NOT EXISTS cursor_tokens (
  id             TEXT PRIMARY KEY,
  email          TEXT DEFAULT '',
  access_token   TEXT DEFAULT '',
  refresh_token  TEXT DEFAULT '',
  note           TEXT DEFAULT '',
  status         TEXT DEFAULT 'active',
  assigned_user  TEXT DEFAULT '',
  added_at       TIMESTAMPTZ DEFAULT now(),
  last_used      TIMESTAMPTZ,
  use_count      INTEGER DEFAULT 0
);

-- ── 迁移：为已有表添加新列 ──
DO $$ BEGIN
  ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS token_id TEXT DEFAULT '';
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE users ADD COLUMN IF NOT EXISTS assigned_token_id TEXT DEFAULT '';
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_usage_token ON usage_records(token_id);
