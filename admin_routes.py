"""
Admin API — 管理员端调用的接口。

所有接口需要 admin_key 认证（header: X-Admin-Key）。
管理：tokens、users、combos、用户 API keys。
"""

from fastapi import APIRouter, Request, HTTPException, Depends
from loguru import logger
import os

admin_router = APIRouter(tags=["admin"])


def verify_admin(request: Request):
    key = request.headers.get("X-Admin-Key", "")
    pool = request.app.state.pool
    if not pool.verify_admin_key(key):
        raise HTTPException(status_code=401, detail="Invalid admin key")


# ── Token 管理 ──

@admin_router.get("/tokens", dependencies=[Depends(verify_admin)])
async def list_tokens(request: Request):
    return {"tokens": await request.app.state.pool.list_tokens()}


@admin_router.post("/tokens", dependencies=[Depends(verify_admin)])
async def add_token(request: Request):
    body = await request.json()
    entry = await request.app.state.pool.add_token(body)
    safe = {**entry}
    for f in ("refreshToken", "accessToken", "clientSecret"):
        if safe.get(f):
            safe[f] = safe[f][:16] + "..."
    return {"token": safe}


@admin_router.delete("/tokens/{token_id}", dependencies=[Depends(verify_admin)])
async def remove_token(request: Request, token_id: str):
    ok = await request.app.state.pool.remove_token(token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token not found")
    request.app.state.bridge.remove_manager(token_id)
    return {"ok": True}


@admin_router.post("/tokens/{token_id}/test", dependencies=[Depends(verify_admin)])
async def test_token(request: Request, token_id: str):
    """测试凭证是否有效：尝试获取 access token 和模型列表。"""
    import httpx
    from kiro.auth import AuthType
    from kiro.utils import get_kiro_headers

    pool = request.app.state.pool
    bridge = request.app.state.bridge

    token_entry = await pool.get_token_full(token_id)
    if not token_entry:
        raise HTTPException(status_code=404, detail="Token not found")

    result = {"valid": False, "auth_type": "", "models_count": 0, "error": ""}
    try:
        mgr = bridge.get_or_create_manager(token_entry)
        access_token = await mgr.get_access_token()
        result["auth_type"] = str(mgr.auth_type.value) if hasattr(mgr.auth_type, "value") else str(mgr.auth_type)
        result["valid"] = True

        # 尝试获取模型列表
        headers = get_kiro_headers(mgr, access_token)
        params = {"origin": "AI_EDITOR"}
        if mgr.auth_type == AuthType.KIRO_DESKTOP and mgr.profile_arn:
            params["profileArn"] = mgr.profile_arn
        url = f"{mgr.q_host}/ListAvailableModels"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                result["models_count"] = len(models)
                result["models"] = [m.get("modelId", "") for m in models]
            else:
                result["error"] = f"ListModels returned {resp.status_code}"
    except Exception as e:
        result["valid"] = False
        result["error"] = str(e)[:200]

    return result


@admin_router.get("/tokens/usage/all", dependencies=[Depends(verify_admin)])
async def get_all_token_usage(request: Request):
    """获取所有凭证的用量统计。"""
    return {"usage": await request.app.state.pool.get_all_token_usage()}


@admin_router.get("/tokens/{token_id}/usage", dependencies=[Depends(verify_admin)])
async def get_token_usage(request: Request, token_id: str):
    """获取某个凭证的用量统计。"""
    return await request.app.state.pool.get_token_usage(token_id)


# ── 用户管理 ──

@admin_router.get("/users", dependencies=[Depends(verify_admin)])
async def list_users(request: Request):
    return {"users": await request.app.state.pool.list_users()}


@admin_router.post("/users", dependencies=[Depends(verify_admin)])
async def create_user(request: Request):
    body = await request.json()
    name = body.get("name", "")
    assigned_token_id = body.get("assigned_token_id", "")
    user = await request.app.state.pool.create_user(name, assigned_token_id)
    return {"user": user}


@admin_router.delete("/users/{user_id}", dependencies=[Depends(verify_admin)])
async def remove_user(request: Request, user_id: str):
    ok = await request.app.state.pool.remove_user(user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


@admin_router.get("/users/{user_id}/token", dependencies=[Depends(verify_admin)])
async def get_user_token(request: Request, user_id: str):
    user = await request.app.state.pool.get_user_full(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"usertoken": user["usertoken"]}


# ── 用户 API Key 管理 ──

@admin_router.get("/users/{user_id}/apikeys", dependencies=[Depends(verify_admin)])
async def list_user_apikeys(request: Request, user_id: str):
    keys = await request.app.state.pool.list_user_apikeys(user_id)
    return {"apikeys": keys}


@admin_router.post("/users/{user_id}/apikeys", dependencies=[Depends(verify_admin)])
async def create_user_apikey(request: Request, user_id: str):
    key = await request.app.state.pool.create_user_apikey(user_id)
    if not key:
        raise HTTPException(status_code=404, detail="User not found")
    return {"apikey": key}


@admin_router.delete("/users/{user_id}/apikeys", dependencies=[Depends(verify_admin)])
async def revoke_user_apikey(request: Request, user_id: str):
    body = await request.json()
    apikey = body.get("apikey", "")
    ok = await request.app.state.pool.revoke_user_apikey(user_id, apikey)
    if not ok:
        raise HTTPException(status_code=400, detail="Cannot revoke (not found)")
    return {"ok": True}


# ── Combo 映射 ──

@admin_router.get("/combos", dependencies=[Depends(verify_admin)])
async def list_combos(request: Request):
    return {"combos": await request.app.state.pool.list_combos()}


@admin_router.post("/combos", dependencies=[Depends(verify_admin)])
async def set_combo(request: Request):
    body = await request.json()
    name = body.get("name", "")
    models = body.get("models", [])
    if not name or not models:
        raise HTTPException(status_code=400, detail="name and models required")
    await request.app.state.pool.set_combo(name, models)
    return {"ok": True, "combo": {name: models}}


@admin_router.delete("/combos/{name}", dependencies=[Depends(verify_admin)])
async def remove_combo(request: Request, name: str):
    ok = await request.app.state.pool.remove_combo(name)
    if not ok:
        raise HTTPException(status_code=404, detail="Custom combo not found (built-in combos cannot be deleted)")
    return {"ok": True}


# ── 状态 ──

@admin_router.get("/status", dependencies=[Depends(verify_admin)])
async def status(request: Request):
    pool = request.app.state.pool
    tokens = await pool.list_tokens()
    users = await pool.list_users()
    combos = await pool.list_combos()
    return {
        "tokens": len(tokens),
        "active_tokens": len([t for t in tokens if t.get("status") == "active"]),
        "users": len(users),
        "active_users": len([u for u in users if u.get("status") == "active"]),
        "combos": len(combos),
    }


@admin_router.put("/users/{user_id}/status", dependencies=[Depends(verify_admin)])
async def set_user_status(request: Request, user_id: str):
    body = await request.json()
    st = body.get("status", "")
    if st not in ("active", "suspended"):
        raise HTTPException(status_code=400, detail="status must be 'active' or 'suspended'")
    ok = await request.app.state.pool.set_user_status(user_id, st)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True, "status": st}


@admin_router.put("/users/{user_id}/token", dependencies=[Depends(verify_admin)])
async def assign_token(request: Request, user_id: str):
    """给用户分配/更换转发凭证。body: {"token_id": "xxx"} 或 {"token_id": ""} 取消绑定。"""
    body = await request.json()
    token_id = body.get("token_id", "")
    ok = await request.app.state.pool.assign_token(user_id, token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True, "assigned_token_id": token_id}


# ── 用量监控 ──

@admin_router.get("/usage", dependencies=[Depends(verify_admin)])
async def get_global_usage(request: Request):
    return await request.app.state.pool.get_all_usage()


@admin_router.get("/usage/{user_id}", dependencies=[Depends(verify_admin)])
async def get_user_usage(request: Request, user_id: str):
    data = await request.app.state.pool.get_user_usage(user_id)
    if not data:
        raise HTTPException(status_code=404, detail="User not found")
    return data


@admin_router.put("/users/{user_id}/quota", dependencies=[Depends(verify_admin)])
async def set_user_quota(request: Request, user_id: str):
    body = await request.json()
    ok = await request.app.state.pool.set_user_quota(user_id, body)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True, "quota": body}


@admin_router.post("/usage/{user_id}/reset", dependencies=[Depends(verify_admin)])
async def reset_user_usage(request: Request, user_id: str):
    ok = await request.app.state.pool.reset_user_usage(user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


@admin_router.post("/users/{user_id}/grant", dependencies=[Depends(verify_admin)])
async def grant_tokens(request: Request, user_id: str):
    body = await request.json()
    amount = body.get("amount", 0)
    if not amount or not isinstance(amount, (int, float)):
        raise HTTPException(status_code=400, detail="amount required (integer)")
    result = await request.app.state.pool.grant_tokens(user_id, int(amount))
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return result


# ── 本机配置提取 & Cursor Pro 凭证管理 ──

def _get_cursor_db_path():
    """跨平台获取 Cursor state.vscdb 路径（多策略扫描）。"""
    from cursor_utils import find_cursor_db
    db_path, _ = find_cursor_db()
    return db_path


def _get_kiro_cli_paths():
    """跨平台获取 kiro-cli SQLite 可能路径。"""
    import platform
    from pathlib import Path
    system = platform.system()
    if system == "Windows":
        appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return [
            appdata / "kiro-cli" / "data.sqlite3",
            appdata / "amazon-q" / "data.sqlite3",
        ]
    else:  # macOS / Linux
        return [
            Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3",
            Path.home() / ".local" / "share" / "amazon-q" / "data.sqlite3",
        ]


@admin_router.post("/extract/cursor", dependencies=[Depends(verify_admin)])
async def extract_cursor_config(request: Request):
    """提取本机 Cursor 登录凭证并直接存入 Cursor 凭证池。"""
    from cursor_utils import find_cursor_db, read_cursor_creds

    creds = read_cursor_creds()
    if not creds:
        _, tried = find_cursor_db()
        raise HTTPException(
            status_code=404,
            detail=f"未找到 Cursor 凭证。可能原因：Cursor 未安装、未登录、或数据库路径非标准。\n"
                   f"可设置环境变量 CURSOR_DB_PATH 手动指定。\n"
                   f"已尝试路径:\n" + "\n".join(f"  · {p}" for p in tried),
        )

    email = creds["email"]
    membership = creds["membership"]

    # 直接存入数据库
    pool = request.app.state.pool
    entry = await pool.add_cursor_token({
        "email": email,
        "accessToken": creds["accessToken"],
        "refreshToken": creds["refreshToken"],
        "note": f"本机提取 · {membership}",
    })

    return {"ok": True, "email": email, "membership": membership, "token_id": entry["id"],
            "dbPath": creds.get("dbPath", "")}


@admin_router.post("/extract/kiro", dependencies=[Depends(verify_admin)])
async def extract_kiro_config(request: Request):
    """提取本机 Kiro 凭证（从 kiro-cli SQLite）并直接存入 Kiro 凭证池。"""
    import sqlite3
    import json as _json

    creds = None
    source = ""
    for cli_path in _get_kiro_cli_paths():
        if not cli_path.exists():
            continue
        try:
            conn = sqlite3.connect(str(cli_path))
            cur = conn.cursor()
            for tk in ["kirocli:social:token", "kirocli:odic:token", "codewhisperer:odic:token"]:
                cur.execute("SELECT value FROM auth_kv WHERE key = ?", (tk,))
                row = cur.fetchone()
                if row:
                    data = _json.loads(row[0])
                    creds = {
                        "refreshToken": data.get("refresh_token", ""),
                        "accessToken": data.get("access_token", ""),
                        "expiresAt": data.get("expires_at", ""),
                        "region": data.get("region", "us-east-1"),
                        "profileArn": data.get("profile_arn", ""),
                    }
                    # clientId / clientSecret
                    for dk in ["kirocli:odic:device-registration", "codewhisperer:odic:device-registration"]:
                        cur.execute("SELECT value FROM auth_kv WHERE key = ?", (dk,))
                        drow = cur.fetchone()
                        if drow:
                            dd = _json.loads(drow[0])
                            creds["clientId"] = dd.get("client_id", "")
                            creds["clientSecret"] = dd.get("client_secret", "")
                            break
                    creds["authMethod"] = "AWS_SSO_OIDC" if creds.get("clientId") else "KIRO_DESKTOP"
                    source = str(cli_path)
                    break
            conn.close()
            if creds:
                break
        except Exception as e:
            logger.warning(f"读取 {cli_path} 失败: {e}")

    if not creds or not creds.get("refreshToken"):
        raise HTTPException(status_code=404, detail="未找到 Kiro 凭证。需要 kiro-cli 已登录（~/.local/share/kiro-cli/data.sqlite3）")

    # 直接存入数据库
    pool = request.app.state.pool
    entry = await pool.add_token(creds)

    return {"ok": True, "region": creds.get("region", ""), "authMethod": creds.get("authMethod", ""),
            "source": source, "token_id": entry["id"]}


# ── Promax 激活码管理 ──

@admin_router.get("/promax-keys", dependencies=[Depends(verify_admin)])
async def list_promax_keys(request: Request):
    return {"keys": await request.app.state.pool.list_promax_keys()}


@admin_router.post("/promax-keys", dependencies=[Depends(verify_admin)])
async def add_promax_key(request: Request):
    body = await request.json()
    api_key = body.get("api_key", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    r = await request.app.state.pool.add_promax_key(api_key, body.get("note", ""))
    return r


@admin_router.delete("/promax-keys/{key_id}", dependencies=[Depends(verify_admin)])
async def remove_promax_key(request: Request, key_id: str):
    ok = await request.app.state.pool.remove_promax_key(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"ok": True}


@admin_router.put("/promax-keys/{key_id}/assign", dependencies=[Depends(verify_admin)])
async def assign_promax_key(request: Request, key_id: str):
    body = await request.json()
    user_name = body.get("user_name", "")
    ok = await request.app.state.pool.assign_promax_key(key_id, user_name)
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"ok": True}
