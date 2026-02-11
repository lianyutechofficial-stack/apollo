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


@user_router.get("/cursor-activation")
async def get_cursor_activation(request: Request):
    """获取分配给当前用户的 Cursor 激活码。"""
    user = await _get_current_user(request)
    key = await request.app.state.pool.get_promax_key_for_user(user["name"])
    if not key:
        raise HTTPException(status_code=404, detail="暂无可用激活码，请联系管理员")
    return {"activation_code": key}


@user_router.post("/smart-switch")
async def smart_switch(request: Request):
    """
    服务端换号：调 promax billing/request-switch 获取新号，
    返回新账号凭证给网页端，网页端再传给本地 Agent 写入。
    """
    import httpx

    user = await _get_current_user(request)
    key = await request.app.state.pool.get_promax_key_for_user(user["name"])
    if not key:
        raise HTTPException(status_code=404, detail="暂无可用激活码")

    promax_api = "http://api.cursorpromax.cn"
    device_id = user["id"][:32]  # 用 user id 作为 device_id

    async with httpx.AsyncClient(timeout=30) as client:
        # 方式1: billing/request-switch
        try:
            r = await client.post(f"{promax_api}/api/billing/request-switch", json={
                "activation_code": key,
                "device_id": device_id,
                "reason": "quota_exhausted",
            })
            data = r.json()
            if data.get("success") and data.get("switched") and data.get("new_account"):
                account = data["new_account"]
                logger.info(f"smart-switch OK via request-switch: {account.get('email')}")
                return {"ok": True, "account": account}
        except Exception as e:
            logger.warning(f"request-switch failed: {e}")

        # 方式2: billing/request-reassign
        try:
            r = await client.post(
                f"{promax_api}/api/billing/request-reassign",
                params={"activation_code": key, "device_id": device_id},
                json={},
            )
            data = r.json()
            if data.get("success") and data.get("account"):
                account = data["account"]
                logger.info(f"smart-switch OK via request-reassign: {account.get('email')}")
                return {"ok": True, "account": account}
        except Exception as e:
            logger.warning(f"request-reassign failed: {e}")

        # 方式3: fallback quick-switch（需要 activation_code_id）
        try:
            # 先激活拿 acid
            r = await client.post(f"{promax_api}/api/activate", json={
                "code": key, "device_id": device_id,
                "device_name": "apollo-gateway", "plugin_version": "2.0.0-apollo",
            })
            act_data = r.json()
            acid = act_data.get("data", {}).get("activation_code_id")
            if acid:
                r = await client.post(
                    f"{promax_api}/api/quick-switch",
                    params={"activation_code_id": str(acid), "device_id": device_id},
                    json={},
                )
                data = r.json()
                if data.get("success") and data.get("data"):
                    logger.info(f"smart-switch OK via quick-switch: {data['data'].get('email')}")
                    return {"ok": True, "account": data["data"]}
        except Exception as e:
            logger.warning(f"quick-switch fallback failed: {e}")

    return {"ok": False, "error": "号池暂无可用账号，请稍后再试"}
