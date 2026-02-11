"""
User API — 用户端接口。

用 apollo-xxx（usertoken）登录，管理自己的 ap-xxx API key。
"""

from fastapi import APIRouter, Request, HTTPException
from loguru import logger

user_router = APIRouter(tags=["user"])


async def _get_current_user(request: Request):
    """从 Authorization header 提取 apollo-xxx 并验证登录。"""
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else auth
    if not token:
        raise HTTPException(status_code=401, detail="Missing usertoken")
    user = await request.app.state.pool.validate_login(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid usertoken. Use your apollo-xxx token to login.")
    return user


@user_router.get("/me")
async def get_me(request: Request):
    user = await _get_current_user(request)
    return {
        "id": user["id"], "name": user["name"], "status": user["status"],
        "token_balance": user.get("token_balance", 0), "token_granted": user.get("token_granted", 0),
        "apikeys_count": len(user.get("apikeys", [])),
        "createdAt": user["createdAt"], "lastUsed": user["lastUsed"], "requestCount": user["requestCount"],
    }


@user_router.get("/apikeys")
async def list_apikeys(request: Request):
    user = await _get_current_user(request)
    return {"apikeys": user.get("apikeys", [])}


@user_router.post("/apikeys")
async def create_apikey(request: Request):
    user = await _get_current_user(request)
    key = await request.app.state.pool.create_user_apikey(user["id"])
    if not key:
        raise HTTPException(status_code=500, detail="Failed to create API key")
    return {"apikey": key}


@user_router.delete("/apikeys")
async def revoke_apikey(request: Request):
    user = await _get_current_user(request)
    body = await request.json()
    apikey = body.get("apikey", "")
    if not apikey:
        raise HTTPException(status_code=400, detail="apikey required")
    ok = await request.app.state.pool.revoke_user_apikey(user["id"], apikey)
    if not ok:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"ok": True}


@user_router.get("/usage")
async def get_my_usage(request: Request):
    user = await _get_current_user(request)
    data = await request.app.state.pool.get_user_usage(user["id"])
    if not data:
        raise HTTPException(status_code=500, detail="Failed to get usage data")
    return data


@user_router.get("/combos")
async def get_combos(request: Request):
    await _get_current_user(request)
    combos = await request.app.state.pool.list_combos()
    return {"combos": combos}


@user_router.post("/cursor-claim")
async def claim_cursor_token(request: Request):
    """用户领取 Cursor Pro 凭证。返回完整 token 用于本地写入。"""
    user = await _get_current_user(request)
    token = await request.app.state.pool.claim_cursor_token(user["name"])
    if not token:
        raise HTTPException(status_code=404, detail="暂无可用的 Cursor 凭证，请联系管理员")
    return {
        "email": token["email"],
        "accessToken": token["access_token"],
        "refreshToken": token["refresh_token"],
    }


@user_router.post("/cursor-apply")
async def apply_cursor_token(request: Request):
    """
    一键切换 Cursor Pro 账号（服务端执行）。

    流程：领取凭证 → 关闭 Cursor → 写入 state.vscdb → 重新打开 Cursor。
    自动检测操作系统，适配 macOS / Windows / Linux。
    """
    import asyncio
    import platform
    import subprocess
    import sqlite3
    from pathlib import Path

    user = await _get_current_user(request)
    token = await request.app.state.pool.claim_cursor_token(user["name"])
    if not token:
        raise HTTPException(status_code=404, detail="暂无可用的 Cursor 凭证，请联系管理员")

    email = token["email"]
    access_token = token["access_token"]
    refresh_token = token["refresh_token"]

    # ── 跨平台路径 ──
    system = platform.system()
    if system == "Darwin":
        db_path = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    elif system == "Windows":
        import os as _os
        appdata = Path(_os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        db_path = appdata / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    else:
        db_path = Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"

    if not db_path.exists():
        raise HTTPException(status_code=500, detail=f"未找到 Cursor 数据库: {db_path}\n请确认 Cursor 已安装并至少启动过一次")

    steps = []

    # 1. 关闭 Cursor（跨平台）
    try:
        if system == "Darwin":
            subprocess.run(["osascript", "-e", 'quit app "Cursor"'], capture_output=True, timeout=5)
            await asyncio.sleep(1)
            subprocess.run(["pkill", "-f", "Cursor Helper"], capture_output=True, timeout=3)
            subprocess.run(["pkill", "-f", "Cursor.app"], capture_output=True, timeout=3)
        elif system == "Windows":
            subprocess.run(["taskkill", "/F", "/IM", "Cursor.exe"], capture_output=True, timeout=5)
        else:
            subprocess.run(["pkill", "-f", "cursor"], capture_output=True, timeout=5)
        await asyncio.sleep(2)
        steps.append("关闭 Cursor")
    except Exception as e:
        logger.warning(f"关闭 Cursor 时出错（可忽略）: {e}")
        steps.append("关闭 Cursor（部分）")

    # 2. 写入凭证到 state.vscdb
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        for key, value in [
            ("cursorAuth/accessToken", access_token),
            ("cursorAuth/refreshToken", refresh_token),
            ("cursorAuth/cachedEmail", email),
            ("cursorAuth/cachedSignUpType", "Auth_0"),
            ("cursorAuth/stripeMembershipType", "pro"),
        ]:
            cursor.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()
        steps.append("写入登录凭证")
        logger.info(f"Cursor credentials written for {email}")
    except Exception as e:
        logger.error(f"写入 Cursor 数据库失败: {e}")
        raise HTTPException(status_code=500, detail=f"写入 Cursor 数据库失败: {e}")

    # 3. 重新打开 Cursor（跨平台）
    try:
        if system == "Darwin":
            subprocess.Popen(["open", "-a", "Cursor"])
        elif system == "Windows":
            import os as _os
            local = Path(_os.environ.get("LOCALAPPDATA", ""))
            cursor_exe = local / "Programs" / "cursor" / "Cursor.exe"
            if cursor_exe.exists():
                subprocess.Popen([str(cursor_exe)])
            else:
                subprocess.Popen(["cmd", "/c", "start", "", "Cursor"], shell=False)
        else:
            subprocess.Popen(["cursor"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        steps.append("启动 Cursor")
    except Exception as e:
        logger.warning(f"启动 Cursor 失败: {e}")
        steps.append("启动 Cursor 失败，请手动打开")

    return {
        "ok": True,
        "email": email,
        "steps": steps,
        "message": f"已切换到 {email}",
    }
