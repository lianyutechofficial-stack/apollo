"""
Token Pool — Apollo Gateway 数据管理（PostgreSQL via asyncpg）。

所有方法统一 async，直连 Supabase PostgreSQL。
热数据内存缓存，减少跨海查询延迟。
"""

import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict

from loguru import logger

DEFAULT_COMBOS = {
    "kiro-opus-4-6": ["claude-opus-4.6"],
    "kiro-opus-4-5": ["claude-opus-4.5"],
    "kiro-sonnet-4-5": ["claude-sonnet-4.5"],
    "kiro-sonnet-4": ["claude-sonnet-4"],
    "kiro-haiku-4-5": ["claude-haiku-4.5"],
    "kiro-haiku": ["claude-haiku-4.5"],
    "kiro-auto": ["auto-kiro"],
}

# ── 简易 TTL 缓存 ──
class _Cache:
    def __init__(self, ttl=30):
        self._ttl = ttl
        self._store: Dict[str, tuple] = {}  # key -> (value, expire_time)

    def get(self, key):
        item = self._store.get(key)
        if item and item[1] > time.monotonic():
            return item[0]
        return None

    def set(self, key, value):
        self._store[key] = (value, time.monotonic() + self._ttl)

    def invalidate(self, *keys):
        for k in keys:
            self._store.pop(k, None)

    def clear(self):
        self._store.clear()


class TokenPool:
    def __init__(self, database_url: str):
        self._dsn = database_url
        self._pool = None
        self._rr_index = 0
        # 内存缓存：认证 30s，模型映射 60s
        self._auth_cache = _Cache(ttl=30)
        self._mapping_cache = _Cache(ttl=60)

    async def init(self):
        import asyncpg
        import ssl
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10, ssl=ssl_ctx)
        await self._ensure_schema()
        await self._seed_builtins()
        logger.info("TokenPool initialized (PostgreSQL)")

    async def _ensure_schema(self):
        schema_file = Path(__file__).parent / "schema.sql"
        if schema_file.exists():
            sql = schema_file.read_text()
            async with self._pool.acquire() as conn:
                await conn.execute(sql)

    async def _seed_builtins(self):
        async with self._pool.acquire() as conn:
            for name, targets in DEFAULT_COMBOS.items():
                await conn.execute(
                    """INSERT INTO model_mappings (name, type, targets, is_builtin)
                       VALUES ($1, 'combo', $2, true)
                       ON CONFLICT (name) DO UPDATE SET targets = $2""",
                    name, json.dumps(targets),
                )

    # ── Admin Key ──

    ADMIN_KEY = "Ljc17748697418."

    async def _load_admin_key(self) -> str:
        return self.ADMIN_KEY

    def get_admin_key(self):
        return self.ADMIN_KEY

    def verify_admin_key(self, key):
        return bool(key) and key == self.ADMIN_KEY

    # ── Token CRUD ──

    async def add_token(self, token_data):
        client_id_hash = token_data.get("clientIdHash", "")
        now = datetime.now(timezone.utc)

        # 同 clientIdHash 的凭证已存在 → 更新
        if client_id_hash:
            async with self._pool.acquire() as conn:
                existing = await conn.fetchrow(
                    "SELECT id FROM tokens WHERE client_id_hash = $1", client_id_hash
                )
            if existing:
                tid = existing["id"]
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE tokens SET refresh_token=$1, access_token=$2, expires_at=$3,
                           region=$4, client_id=$5, client_secret=$6, auth_method=$7,
                           provider=$8, profile_arn=$9, status='active'
                           WHERE id=$10""",
                        token_data.get("refreshToken", ""), token_data.get("accessToken", ""),
                        token_data.get("expiresAt", ""), token_data.get("region", "us-east-1"),
                        token_data.get("clientId", ""), token_data.get("clientSecret", ""),
                        token_data.get("authMethod", ""), token_data.get("provider", ""),
                        token_data.get("profileArn", ""), tid,
                    )
                logger.info(f"Token updated (same clientIdHash): id={tid}")
                self._auth_cache.invalidate("all_tokens")
                return {"id": tid, "status": "active", "addedAt": now.isoformat(),
                        "useCount": 0, "updated": True, **token_data}

        # 新凭证 → 插入
        tid = secrets.token_hex(8)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO tokens (id, refresh_token, access_token, expires_at, region,
                   client_id_hash, client_id, client_secret, auth_method, provider, profile_arn,
                   status, added_at, use_count)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'active',$12,0)""",
                tid, token_data.get("refreshToken", ""), token_data.get("accessToken", ""),
                token_data.get("expiresAt", ""), token_data.get("region", "us-east-1"),
                client_id_hash, token_data.get("clientId", ""),
                token_data.get("clientSecret", ""), token_data.get("authMethod", ""),
                token_data.get("provider", ""), token_data.get("profileArn", ""), now,
            )
        entry = {"id": tid, "status": "active", "addedAt": now.isoformat(), "useCount": 0, **token_data}
        logger.info(f"Token added: id={tid}")
        self._auth_cache.invalidate("all_tokens")
        return entry

    async def remove_token(self, token_id):
        async with self._pool.acquire() as conn:
            res = await conn.execute("DELETE FROM tokens WHERE id = $1", token_id)
        self._auth_cache.invalidate("all_tokens")
        return res == "DELETE 1"

    def _row_to_token(self, r):
        return {
            "id": r["id"], "refreshToken": r["refresh_token"], "accessToken": r["access_token"],
            "expiresAt": r["expires_at"], "region": r["region"], "clientIdHash": r["client_id_hash"],
            "clientId": r["client_id"], "clientSecret": r["client_secret"],
            "authMethod": r["auth_method"], "provider": r["provider"], "profileArn": r["profile_arn"],
            "status": r["status"],
            "addedAt": r["added_at"].isoformat() if r["added_at"] else None,
            "lastUsed": r["last_used"].isoformat() if r["last_used"] else None,
            "useCount": r["use_count"],
        }

    async def list_tokens(self):
        cached = self._auth_cache.get("all_tokens")
        if cached is not None:
            return cached
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM tokens ORDER BY added_at")
        result = []
        for r in rows:
            t = self._row_to_token(r)
            for f in ("refreshToken", "accessToken", "clientSecret"):
                if t.get(f):
                    t[f] = t[f][:16] + "..."
            result.append(t)
        self._auth_cache.set("all_tokens", result)
        return result

    async def get_token_full(self, token_id):
        async with self._pool.acquire() as conn:
            r = await conn.fetchrow("SELECT * FROM tokens WHERE id = $1", token_id)
        if not r:
            return None
        return self._row_to_token(r)

    async def get_next_token(self):
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM tokens WHERE status = 'active' ORDER BY added_at")
        if not rows:
            return None
        self._rr_index = self._rr_index % len(rows)
        r = rows[self._rr_index]
        self._rr_index = (self._rr_index + 1) % len(rows)
        return self._row_to_token(r)

    async def mark_token_used(self, token_id):
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE tokens SET last_used = $1, use_count = use_count + 1 WHERE id = $2", now, token_id,
            )

    async def update_token_credentials(self, token_id, updates):
        col_map = {"accessToken": "access_token", "refreshToken": "refresh_token",
                    "expiresAt": "expires_at", "clientSecret": "client_secret"}
        sets, vals = [], []
        i = 1
        for k, v in updates.items():
            sets.append(f"{col_map.get(k, k)} = ${i}")
            vals.append(v)
            i += 1
        if sets:
            vals.append(token_id)
            async with self._pool.acquire() as conn:
                await conn.execute(f"UPDATE tokens SET {', '.join(sets)} WHERE id = ${i}", *vals)

    # ── User CRUD ──

    def _row_to_user(self, r, apikeys=None):
        u = {
            "id": r["id"], "name": r["name"], "usertoken": r["usertoken"],
            "status": r["status"],
            "assigned_token_id": r.get("assigned_token_id", "") or "",
            "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
            "lastUsed": r["last_used"].isoformat() if r["last_used"] else None,
            "requestCount": r["request_count"],
            "token_balance": r["token_balance"], "token_granted": r["token_granted"],
            "quota": {
                "daily_tokens": r["quota_daily_tokens"],
                "monthly_tokens": r["quota_monthly_tokens"],
                "daily_requests": r["quota_daily_requests"],
            },
        }
        if apikeys is not None:
            u["apikeys"] = apikeys
        return u

    async def create_user(self, name="", assigned_token_id=""):
        uid = secrets.token_hex(8)
        uname = name or f"User-{secrets.token_hex(4)}"
        usertoken = f"apollo-{secrets.token_hex(8)}"
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO users (id, name, usertoken, status, assigned_token_id, created_at, request_count,
                   token_balance, token_granted, quota_daily_tokens, quota_monthly_tokens, quota_daily_requests)
                   VALUES ($1,$2,$3,'active',$4,$5,0,0,0,0,0,0)""",
                uid, uname, usertoken, assigned_token_id, now,
            )
        logger.info(f"User created: {uname}, assigned_token={assigned_token_id or 'none'}")
        self._auth_cache.invalidate("all_users")
        return {
            "id": uid, "name": uname, "usertoken": usertoken, "apikeys": [],
            "status": "active", "assigned_token_id": assigned_token_id,
            "createdAt": now.isoformat(), "lastUsed": None, "requestCount": 0,
            "usage": {"total_prompt_tokens": 0, "total_completion_tokens": 0, "total_tokens": 0, "by_model": {}, "by_date": {}},
            "token_balance": 0, "token_granted": 0,
            "quota": {"daily_tokens": 0, "monthly_tokens": 0, "daily_requests": 0},
        }

    async def remove_user(self, user_id):
        async with self._pool.acquire() as conn:
            res = await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        self._auth_cache.clear()
        return res == "DELETE 1"

    async def list_users(self):
        cached = self._auth_cache.get("all_users")
        if cached is not None:
            return cached
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM users ORDER BY created_at")
            apikeys = await conn.fetch("SELECT user_id, count(*) as cnt FROM user_apikeys GROUP BY user_id")
        key_counts = {r["user_id"]: r["cnt"] for r in apikeys}
        result = []
        for r in rows:
            u = self._row_to_user(r)
            u["usertoken"] = u["usertoken"][:12] + "..."
            u["apikeys_count"] = key_counts.get(r["id"], 0)
            result.append(u)
        self._auth_cache.set("all_users", result)
        return result

    async def get_user_full(self, user_id):
        async with self._pool.acquire() as conn:
            r = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            if not r:
                return None
            keys = await conn.fetch("SELECT apikey FROM user_apikeys WHERE user_id = $1", user_id)
        return self._row_to_user(r, apikeys=[k["apikey"] for k in keys])

    # ── 认证 ──

    async def validate_login(self, usertoken):
        cached = self._auth_cache.get(f"login:{usertoken}")
        if cached is not None:
            return cached
        async with self._pool.acquire() as conn:
            r = await conn.fetchrow("SELECT * FROM users WHERE usertoken = $1 AND status = 'active'", usertoken)
            if not r:
                return None
            keys = await conn.fetch("SELECT apikey FROM user_apikeys WHERE user_id = $1", r["id"])
        result = self._row_to_user(r, apikeys=[k["apikey"] for k in keys])
        self._auth_cache.set(f"login:{usertoken}", result)
        return result

    async def validate_apikey(self, apikey):
        cached = self._auth_cache.get(f"apikey:{apikey}")
        if cached is not None:
            return cached
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT u.* FROM users u JOIN user_apikeys k ON k.user_id = u.id
                   WHERE k.apikey = $1 AND u.status = 'active'""", apikey,
            )
            if not row:
                return None
            keys = await conn.fetch("SELECT apikey FROM user_apikeys WHERE user_id = $1", row["id"])
        result = self._row_to_user(row, apikeys=[k["apikey"] for k in keys])
        self._auth_cache.set(f"apikey:{apikey}", result)
        return result

    async def mark_user_used(self, user_id):
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_used = $1, request_count = request_count + 1 WHERE id = $2", now, user_id,
            )

    async def set_user_status(self, user_id, status):
        async with self._pool.acquire() as conn:
            res = await conn.execute("UPDATE users SET status = $1 WHERE id = $2", status, user_id)
            if res == "UPDATE 1":
                logger.info(f"User {user_id} status -> {status}")
                self._auth_cache.clear()
                return True
        return False

    async def assign_token(self, user_id: str, token_id: str) -> bool:
        """给用户分配/更换转发凭证。token_id 为空字符串表示取消绑定（回退到全局轮询）。"""
        async with self._pool.acquire() as conn:
            res = await conn.execute("UPDATE users SET assigned_token_id = $1 WHERE id = $2", token_id, user_id)
            if res == "UPDATE 1":
                self._auth_cache.clear()
                logger.info(f"User {user_id} assigned token -> {token_id or 'global'}")
                return True
        return False

    async def get_user_token_entry(self, user):
        """获取用户应该使用的凭证。优先用绑定的，否则全局轮询。"""
        assigned = user.get("assigned_token_id", "")
        if assigned:
            entry = await self.get_token_full(assigned)
            if entry and entry.get("status") == "active":
                return entry
            logger.warning(f"User {user['id']} assigned token {assigned} unavailable, falling back to global")
        return await self.get_next_token()

    # ── 用户 API Key ──

    async def create_user_apikey(self, user_id):
        new_key = f"ap-{secrets.token_hex(8)}"
        async with self._pool.acquire() as conn:
            r = await conn.fetchrow("SELECT id FROM users WHERE id = $1", user_id)
            if not r:
                return None
            await conn.execute("INSERT INTO user_apikeys (apikey, user_id) VALUES ($1, $2)", new_key, user_id)
        self._auth_cache.clear()
        logger.info(f"API key created for user {user_id}: {new_key[:8]}...")
        return new_key

    async def revoke_user_apikey(self, user_id, apikey):
        async with self._pool.acquire() as conn:
            res = await conn.execute("DELETE FROM user_apikeys WHERE apikey = $1 AND user_id = $2", apikey, user_id)
        self._auth_cache.clear()
        return res == "DELETE 1"

    async def list_user_apikeys(self, user_id):
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT apikey FROM user_apikeys WHERE user_id = $1", user_id)
        return [r["apikey"] for r in rows]

    # ── Combo ──

    async def resolve_combo(self, name):
        cached = self._mapping_cache.get(f"combo:{name}")
        if cached is not None:
            return cached
        async with self._pool.acquire() as conn:
            r = await conn.fetchrow("SELECT targets FROM model_mappings WHERE name = $1 AND type = 'combo'", name)
        result = json.loads(r["targets"]) if r else None
        if result is not None:
            self._mapping_cache.set(f"combo:{name}", result)
        return result

    async def list_combos(self):
        cached = self._mapping_cache.get("all_combos")
        if cached is not None:
            return cached
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT name, targets FROM model_mappings WHERE type = 'combo' ORDER BY name")
        result = {r["name"]: json.loads(r["targets"]) for r in rows}
        self._mapping_cache.set("all_combos", result)
        return result

    async def set_combo(self, name, models):
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO model_mappings (name, type, targets, is_builtin) VALUES ($1, 'combo', $2, false)
                   ON CONFLICT (name) DO UPDATE SET targets = $2""", name, json.dumps(models),
            )
        self._mapping_cache.clear()

    async def remove_combo(self, name):
        async with self._pool.acquire() as conn:
            res = await conn.execute("DELETE FROM model_mappings WHERE name = $1 AND type = 'combo' AND is_builtin = false", name)
        self._mapping_cache.clear()
        return res == "DELETE 1"

    async def resolve_model(self, name):
        cached = self._mapping_cache.get(f"resolve:{name}")
        if cached is not None:
            return cached
        combo = await self.resolve_combo(name)
        if combo:
            self._mapping_cache.set(f"resolve:{name}", combo[0])
            return combo[0]
        return name

    # ── 用量追踪 ──

    async def record_usage(self, user_id: str, model: str, prompt_tokens: int, completion_tokens: int, token_id: str = ""):
        total = prompt_tokens + completion_tokens
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO usage_records (user_id, model, prompt_tokens, completion_tokens, token_id) VALUES ($1,$2,$3,$4,$5)",
                user_id, model, prompt_tokens, completion_tokens, token_id,
            )
            await conn.execute(
                "UPDATE users SET token_balance = GREATEST(0, token_balance - $1) WHERE id = $2", total, user_id,
            )
        logger.debug(f"Usage recorded: user={user_id} model={model} token={token_id} +{total}")
        return True

    async def get_token_usage(self, token_id: str) -> Dict:
        """获取某个凭证的用量统计。"""
        async with self._pool.acquire() as conn:
            totals = await conn.fetchrow(
                "SELECT COALESCE(SUM(prompt_tokens),0) as tp, COALESCE(SUM(completion_tokens),0) as tc, COUNT(*) as cnt "
                "FROM usage_records WHERE token_id = $1", token_id,
            )
            by_model = await conn.fetch(
                "SELECT model, SUM(prompt_tokens) as p, SUM(completion_tokens) as c, COUNT(*) as r "
                "FROM usage_records WHERE token_id = $1 GROUP BY model", token_id,
            )
        return {
            "token_id": token_id,
            "total_prompt_tokens": totals["tp"],
            "total_completion_tokens": totals["tc"],
            "total_tokens": totals["tp"] + totals["tc"],
            "total_requests": totals["cnt"],
            "by_model": {r["model"]: {"prompt": r["p"], "completion": r["c"], "requests": r["r"]} for r in by_model},
        }

    async def get_all_token_usage(self) -> Dict[str, Dict]:
        """获取所有凭证的用量统计。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT token_id, COALESCE(SUM(prompt_tokens),0) as tp, COALESCE(SUM(completion_tokens),0) as tc, COUNT(*) as cnt "
                "FROM usage_records WHERE token_id != '' GROUP BY token_id"
            )
        return {
            r["token_id"]: {
                "total_prompt_tokens": r["tp"], "total_completion_tokens": r["tc"],
                "total_tokens": r["tp"] + r["tc"], "total_requests": r["cnt"],
            } for r in rows
        }

    async def check_quota(self, user_id: str) -> Optional[str]:
        async with self._pool.acquire() as conn:
            u = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            if not u:
                return None
            if u["token_balance"] <= 0:
                return f"Token balance exhausted (granted: {u['token_granted']})"
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if u["quota_daily_requests"] > 0:
                cnt = await conn.fetchval(
                    "SELECT COUNT(*) FROM usage_records WHERE user_id = $1 AND recorded_at::date = $2::date", user_id, today,
                )
                if cnt >= u["quota_daily_requests"]:
                    return f"Daily request limit reached ({u['quota_daily_requests']})"
            if u["quota_daily_tokens"] > 0:
                t = await conn.fetchval(
                    "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) FROM usage_records WHERE user_id=$1 AND recorded_at::date=$2::date",
                    user_id, today,
                )
                if t >= u["quota_daily_tokens"]:
                    return f"Daily token limit reached ({u['quota_daily_tokens']})"
            if u["quota_monthly_tokens"] > 0:
                ms = datetime.now(timezone.utc).strftime("%Y-%m-01")
                t = await conn.fetchval(
                    "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) FROM usage_records WHERE user_id=$1 AND recorded_at>=$2::date",
                    user_id, ms,
                )
                if t >= u["quota_monthly_tokens"]:
                    return f"Monthly token limit reached ({u['quota_monthly_tokens']})"
        return None

    async def get_user_usage(self, user_id: str) -> Optional[Dict]:
        async with self._pool.acquire() as conn:
            u = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            if not u:
                return None
            by_model_rows = await conn.fetch(
                "SELECT model, SUM(prompt_tokens) as p, SUM(completion_tokens) as c, COUNT(*) as r FROM usage_records WHERE user_id=$1 GROUP BY model", user_id,
            )
            by_date_rows = await conn.fetch(
                "SELECT recorded_at::date as d, SUM(prompt_tokens) as p, SUM(completion_tokens) as c, COUNT(*) as r FROM usage_records WHERE user_id=$1 GROUP BY d ORDER BY d DESC", user_id,
            )
            totals = await conn.fetchrow(
                "SELECT COALESCE(SUM(prompt_tokens),0) as tp, COALESCE(SUM(completion_tokens),0) as tc FROM usage_records WHERE user_id=$1", user_id,
            )
        return {
            "user_id": u["id"], "name": u["name"],
            "token_balance": u["token_balance"], "token_granted": u["token_granted"],
            "usage": {
                "total_prompt_tokens": totals["tp"], "total_completion_tokens": totals["tc"],
                "total_tokens": totals["tp"] + totals["tc"],
                "by_model": {r["model"]: {"prompt": r["p"], "completion": r["c"], "requests": r["r"]} for r in by_model_rows},
                "by_date": {str(r["d"]): {"prompt": r["p"], "completion": r["c"], "requests": r["r"]} for r in by_date_rows},
            },
            "quota": {"daily_tokens": u["quota_daily_tokens"], "monthly_tokens": u["quota_monthly_tokens"], "daily_requests": u["quota_daily_requests"]},
            "requestCount": u["request_count"],
        }

    async def get_all_usage(self) -> Dict:
        async with self._pool.acquire() as conn:
            totals = await conn.fetchrow("SELECT COALESCE(SUM(prompt_tokens),0) as tp, COALESCE(SUM(completion_tokens),0) as tc FROM usage_records")
            total_requests = await conn.fetchval("SELECT COALESCE(SUM(request_count),0) FROM users")
            by_model_rows = await conn.fetch(
                "SELECT model, SUM(prompt_tokens) as p, SUM(completion_tokens) as c, COUNT(*) as r FROM usage_records GROUP BY model"
            )
            by_date_rows = await conn.fetch(
                "SELECT recorded_at::date as d, SUM(prompt_tokens) as p, SUM(completion_tokens) as c, COUNT(*) as r FROM usage_records GROUP BY d ORDER BY d DESC"
            )
            users_rows = await conn.fetch(
                """SELECT u.id, u.name, u.status, u.token_balance, u.token_granted, u.request_count,
                   COALESCE(SUM(r.prompt_tokens+r.completion_tokens),0) as total_tokens
                   FROM users u LEFT JOIN usage_records r ON r.user_id=u.id GROUP BY u.id ORDER BY total_tokens DESC"""
            )
        return {
            "total_prompt_tokens": totals["tp"], "total_completion_tokens": totals["tc"],
            "total_tokens": totals["tp"] + totals["tc"], "total_requests": total_requests,
            "by_model": {r["model"]: {"prompt": r["p"], "completion": r["c"], "requests": r["r"]} for r in by_model_rows},
            "by_date": {str(r["d"]): {"prompt": r["p"], "completion": r["c"], "requests": r["r"]} for r in by_date_rows},
            "users": [{"user_id": r["id"], "name": r["name"], "status": r["status"],
                        "token_balance": r["token_balance"], "token_granted": r["token_granted"],
                        "total_tokens": r["total_tokens"], "requestCount": r["request_count"]} for r in users_rows],
        }

    async def set_user_quota(self, user_id: str, quota_updates: Dict) -> bool:
        col_map = {"daily_tokens": "quota_daily_tokens", "monthly_tokens": "quota_monthly_tokens", "daily_requests": "quota_daily_requests"}
        sets, vals = [], []
        i = 1
        for k in ("daily_tokens", "monthly_tokens", "daily_requests"):
            if k in quota_updates:
                sets.append(f"{col_map[k]} = ${i}")
                vals.append(int(quota_updates[k]))
                i += 1
        if not sets:
            return False
        vals.append(user_id)
        async with self._pool.acquire() as conn:
            res = await conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ${i}", *vals)
            return res == "UPDATE 1"

    async def grant_tokens(self, user_id: str, amount: int) -> Optional[Dict]:
        async with self._pool.acquire() as conn:
            u = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            if not u:
                return None
            new_balance = max(0, u["token_balance"] + amount)
            new_granted = u["token_granted"] + amount if amount > 0 else u["token_granted"]
            await conn.execute("UPDATE users SET token_balance=$1, token_granted=$2 WHERE id=$3", new_balance, new_granted, user_id)
        logger.info(f"Tokens granted to {user_id}: +{amount}, balance={new_balance}")
        return {"user_id": user_id, "name": u["name"], "token_balance": new_balance, "token_granted": new_granted}

    async def reset_user_usage(self, user_id: str) -> bool:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM usage_records WHERE user_id = $1", user_id)
            res = await conn.execute("UPDATE users SET request_count = 0 WHERE id = $1", user_id)
            return res == "UPDATE 1"

    # ── Cursor Pro 凭证管理 ──

    def _row_to_cursor_token(self, r):
        return {
            "id": r["id"], "email": r["email"],
            "access_token": r["access_token"], "refresh_token": r["refresh_token"],
            "note": r["note"], "status": r["status"],
            "assigned_user": r["assigned_user"] or "",
            "addedAt": r["added_at"].isoformat() if r["added_at"] else None,
            "lastUsed": r["last_used"].isoformat() if r["last_used"] else None,
            "useCount": r["use_count"],
        }

    async def add_cursor_token(self, data: Dict) -> Dict:
        email = data.get("email", "")
        now = datetime.now(timezone.utc)

        # 同 email 的凭证已存在 → 更新
        if email:
            async with self._pool.acquire() as conn:
                existing = await conn.fetchrow(
                    "SELECT id FROM cursor_tokens WHERE email = $1", email
                )
            if existing:
                tid = existing["id"]
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE cursor_tokens SET access_token=$1, refresh_token=$2,
                           note=$3, status='active' WHERE id=$4""",
                        data.get("accessToken", ""), data.get("refreshToken", ""),
                        data.get("note", ""), tid,
                    )
                logger.info(f"Cursor token updated (same email): id={tid} email={email}")
                return {"id": tid, "email": email, "status": "active",
                        "note": data.get("note", ""), "addedAt": now.isoformat(),
                        "useCount": 0, "updated": True}

        # 新凭证 → 插入
        tid = secrets.token_hex(8)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO cursor_tokens (id, email, access_token, refresh_token, note, status, added_at, use_count)
                   VALUES ($1,$2,$3,$4,$5,'active',$6,0)""",
                tid, email, data.get("accessToken", ""),
                data.get("refreshToken", ""), data.get("note", ""), now,
            )
        logger.info(f"Cursor token added: id={tid} email={email}")
        return {"id": tid, "email": email, "status": "active",
                "note": data.get("note", ""), "addedAt": now.isoformat(), "useCount": 0}

    async def list_cursor_tokens(self):
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM cursor_tokens ORDER BY added_at")
        result = []
        for r in rows:
            t = self._row_to_cursor_token(r)
            # 脱敏
            if t["access_token"]:
                t["access_token"] = t["access_token"][:16] + "..."
            if t["refresh_token"]:
                t["refresh_token"] = t["refresh_token"][:16] + "..."
            result.append(t)
        return result

    async def remove_cursor_token(self, token_id: str) -> bool:
        async with self._pool.acquire() as conn:
            res = await conn.execute("DELETE FROM cursor_tokens WHERE id = $1", token_id)
        return res == "DELETE 1"

    async def get_cursor_token_full(self, token_id: str):
        async with self._pool.acquire() as conn:
            r = await conn.fetchrow("SELECT * FROM cursor_tokens WHERE id = $1", token_id)
        if not r:
            return None
        return self._row_to_cursor_token(r)

    async def assign_cursor_token(self, token_id: str, user_name: str) -> bool:
        """给 Cursor 凭证标记分配用户（仅记录，不强制）。"""
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE cursor_tokens SET assigned_user = $1, last_used = $2, use_count = use_count + 1 WHERE id = $3",
                user_name, now, token_id,
            )
        return res == "UPDATE 1"

    async def claim_cursor_token(self, user_name: str):
        """用户领取一个可用的 Cursor 凭证。同一凭证可被多人重复领取。"""
        async with self._pool.acquire() as conn:
            # 优先返回已分配给该用户的
            r = await conn.fetchrow(
                "SELECT * FROM cursor_tokens WHERE assigned_user = $1 AND status = 'active'", user_name,
            )
            if not r:
                # 取使用次数最少的活跃凭证（不限是否已分配）
                r = await conn.fetchrow(
                    "SELECT * FROM cursor_tokens WHERE status = 'active' ORDER BY use_count ASC LIMIT 1",
                )
            if not r:
                return None
            await conn.execute(
                "UPDATE cursor_tokens SET assigned_user = $1, last_used = $2, use_count = use_count + 1 WHERE id = $3",
                user_name, datetime.now(timezone.utc), r["id"],
            )
            return self._row_to_cursor_token(r)
