"""
Gateway Server — 主入口。

提供：
- /v1/chat/completions — OpenAI 兼容转发（完整 kiro-gateway 管线）
- /v1/models — 动态模型列表（从 Kiro API 获取）
- /admin/* — 管理员 API（token 管理、用户管理）
- /health — 健康检查
"""

import sys
import os
import argparse
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from loguru import logger
from dotenv import load_dotenv

load_dotenv()


from kiro.cache import ModelInfoCache
from kiro.auth import AuthType
from kiro.utils import get_kiro_headers
from kiro.config import STREAMING_READ_TIMEOUT

from token_pool import TokenPool
from auth_bridge import AuthBridge
from admin_routes import admin_router
from proxy_routes import proxy_router
from user_routes import user_router

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

logger.remove()
logger.add(
    sys.stderr, level=LOG_LEVEL, colorize=True,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | <level>{message}</level>",
)

FALLBACK_MODELS = [
    {"modelId": "auto-kiro", "modelName": "Auto", "tokenLimits": {"maxInputTokens": 200000}},
    {"modelId": "claude-sonnet-4.5", "modelName": "Claude Sonnet 4.5", "tokenLimits": {"maxInputTokens": 200000}},
    {"modelId": "claude-opus-4.5", "modelName": "Claude Opus 4.5", "tokenLimits": {"maxInputTokens": 200000}},
    {"modelId": "claude-opus-4.6", "modelName": "Claude Opus 4.6", "tokenLimits": {"maxInputTokens": 200000}},
    {"modelId": "claude-sonnet-4", "modelName": "Claude Sonnet 4", "tokenLimits": {"maxInputTokens": 200000}},
    {"modelId": "claude-haiku-4.5", "modelName": "Claude Haiku 4.5", "tokenLimits": {"maxInputTokens": 200000}},
    {"modelId": "claude-3.7-sonnet", "modelName": "Claude 3.7 Sonnet", "tokenLimits": {"maxInputTokens": 200000}},
]


async def _load_models_for_token(auth_manager, model_cache):
    """用一个 auth_manager 从 Kiro API 加载模型列表到 cache。"""
    try:
        token = await auth_manager.get_access_token()
        headers = get_kiro_headers(auth_manager, token)
        params = {"origin": "AI_EDITOR"}
        if auth_manager.auth_type == AuthType.KIRO_DESKTOP and auth_manager.profile_arn:
            params["profileArn"] = auth_manager.profile_arn
        list_models_url = f"{auth_manager.q_host}/ListAvailableModels"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(list_models_url, headers=headers, params=params)
            if resp.status_code == 200:
                models_list = resp.json().get("models", [])
                await model_cache.update(models_list)
                logger.info(f"Loaded {len(models_list)} models from Kiro API")
                return True
            else:
                logger.warning(f"ListAvailableModels returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"Failed to load models: {e}")
    return False


@asynccontextmanager
async def lifespan(app):
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        logger.error("DATABASE_URL not set! Cannot start without database.")
        raise RuntimeError("DATABASE_URL environment variable is required")

    pool = TokenPool(dsn)
    await pool.init()

    bridge = AuthBridge()
    model_cache = ModelInfoCache()

    app.state.pool = pool
    app.state.bridge = bridge
    app.state.model_cache = model_cache
    app.state.http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30.0),
        timeout=httpx.Timeout(connect=30, read=STREAMING_READ_TIMEOUT, write=30, pool=30),
        follow_redirects=True,
    )

    admin_key = pool.get_admin_key()
    logger.info(f"Admin key: {admin_key}")

    tokens = await pool.list_tokens()
    users = await pool.list_users()
    logger.info(f"Tokens: {len(tokens)}, Users: {len(users)}")

    loaded = False
    active_tokens_full = []
    for t in tokens:
        if t.get("status") == "active":
            full = await pool.get_token_full(t["id"])
            if full:
                active_tokens_full.append(full)

    for t in active_tokens_full:
        try:
            mgr = bridge.get_or_create_manager(t)
            loaded = await _load_models_for_token(mgr, model_cache)
            if loaded:
                break
        except Exception as e:
            logger.warning(f"Token {t['id']} failed: {e}")

    if not loaded:
        logger.warning("Using fallback model list")
        await model_cache.update(FALLBACK_MODELS)

    logger.info(f"Model cache: {model_cache.size} models")
    yield
    await app.state.http_client.aclose()
    logger.info("Server shutdown.")


app = FastAPI(title="Apollo Gateway", version="1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
app.include_router(admin_router, prefix="/admin")
app.include_router(user_router, prefix="/user")
app.include_router(proxy_router)

# ── 静态页面 ──
STATIC_DIR = Path(__file__).parent / "static"

@app.get("/panel/admin")
async def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")

@app.get("/panel/user")
async def user_page():
    return FileResponse(STATIC_DIR / "user.html")


@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok", "service": "apollo-gateway"}


@app.get("/agent-download")
async def agent_download():
    """下载 apollo_agent.py 供用户本地运行。"""
    agent_file = Path(__file__).parent / "apollo_agent.py"
    if not agent_file.exists():
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"error": "agent file not found"})
    return FileResponse(agent_file, filename="apollo_agent.py", media_type="text/x-python")


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--port", type=int, default=None)
    parser.add_argument("-H", "--host", type=str, default=None)
    args = parser.parse_args()
    host = args.host or SERVER_HOST
    port = args.port or SERVER_PORT
    print(f"\n  Apollo Gateway")
    print(f"  http://{('localhost' if host == '0.0.0.0' else host)}:{port}")
    print(f"  Admin API: /admin\n")
    uvicorn.run("main:app", host=host, port=port)
