"""
Proxy Routes — 用户请求转发到 Kiro API（完整管线）。

复用 kiro-gateway 的完整转换、流式、重试、截断恢复逻辑。
支持 combo 映射和模型别名解析。
"""

import sys
import json
import time
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from loguru import logger

KIRO_GW_PATH = str(Path(__file__).parent.parent / "kiro-gateway")
if KIRO_GW_PATH not in sys.path:
    sys.path.append(KIRO_GW_PATH)

from kiro.converters_openai import build_kiro_payload
from kiro.streaming_openai import stream_kiro_to_openai, collect_stream_response
from kiro.auth import KiroAuthManager, AuthType
from kiro.http_client import KiroHttpClient
from kiro.kiro_errors import enhance_kiro_error
from kiro.utils import generate_conversation_id
from kiro.models_openai import ChatCompletionRequest, ChatMessage
from kiro.cache import ModelInfoCache

proxy_router = APIRouter(tags=["proxy"])


def _extract_usertoken(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return auth


async def _validate_user(request):
    usertoken = _extract_usertoken(request)
    if not usertoken:
        raise HTTPException(status_code=401, detail="Missing API key")
    user = await request.app.state.pool.validate_apikey(usertoken)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user


@proxy_router.get("/v1/models")
async def list_models(request: Request):
    await _validate_user(request)
    pool = request.app.state.pool
    model_cache = request.app.state.model_cache
    now = int(time.time())

    models = []
    seen = set()

    for model_id in model_cache.get_all_model_ids():
        models.append({"id": model_id, "object": "model", "created": now, "owned_by": "kiro"})
        seen.add(model_id)

    combos = await pool.list_combos()
    for combo_name in combos:
        if combo_name not in seen:
            models.append({"id": combo_name, "object": "model", "created": now, "owned_by": "apollo-combo"})
            seen.add(combo_name)

    return {"object": "list", "data": models}


@proxy_router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI 兼容的 chat completions 转发（完整管线）。

    模型解析顺序：combo -> 原名
    """
    user = await _validate_user(request)
    pool = request.app.state.pool
    bridge = request.app.state.bridge
    model_cache = request.app.state.model_cache

    # ── 配额检查 ──
    quota_error = await pool.check_quota(user["id"])
    if quota_error:
        raise HTTPException(status_code=429, detail=f"Quota exceeded: {quota_error}")

    token_entry = await pool.get_user_token_entry(user)
    if not token_entry:
        raise HTTPException(status_code=503, detail="No available tokens in pool")

    try:
        raw_body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    try:
        request_data = ChatCompletionRequest(**raw_body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Validation error: {e}")

    # ── 模型解析：combo -> 原名 ──
    original_model = request_data.model
    resolved_model = await pool.resolve_model(original_model)
    if resolved_model != original_model:
        logger.info(f"Model resolved: {original_model} -> {resolved_model}")
        request_data.model = resolved_model

    logger.info(
        f"[{user['name']}] model={request_data.model} stream={request_data.stream} "
        f"token={token_entry['id']} messages={len(request_data.messages)}"
    )

    auth_manager = bridge.get_or_create_manager(token_entry)

    # -- 截断恢复检查 --
    from kiro.truncation_state import get_tool_truncation, get_content_truncation
    from kiro.truncation_recovery import (
        generate_truncation_tool_result,
        generate_truncation_user_message,
    )

    modified_messages = []
    tool_results_modified = 0
    content_notices_added = 0

    for msg in request_data.messages:
        if msg.role == "tool" and msg.tool_call_id:
            truncation_info = get_tool_truncation(msg.tool_call_id)
            if truncation_info:
                synthetic = generate_truncation_tool_result(
                    tool_name=truncation_info.tool_name,
                    tool_use_id=msg.tool_call_id,
                    truncation_info=truncation_info.truncation_info,
                )
                modified_content = f"{synthetic['content']}\n\n---\n\nOriginal tool result:\n{msg.content}"
                modified_msg = msg.model_copy(update={"content": modified_content})
                modified_messages.append(modified_msg)
                tool_results_modified += 1
                continue

        if msg.role == "assistant" and msg.content and isinstance(msg.content, str):
            truncation_info = get_content_truncation(msg.content)
            if truncation_info:
                modified_messages.append(msg)
                synthetic_user_msg = ChatMessage(
                    role="user",
                    content=generate_truncation_user_message(),
                )
                modified_messages.append(synthetic_user_msg)
                content_notices_added += 1
                continue

        modified_messages.append(msg)

    if tool_results_modified > 0 or content_notices_added > 0:
        request_data.messages = modified_messages
        logger.info(
            f"Truncation recovery: modified {tool_results_modified} tool_result(s), "
            f"added {content_notices_added} content notice(s)"
        )

    # -- 构建 Kiro payload --
    conversation_id = generate_conversation_id()
    profile_arn_for_payload = ""
    if auth_manager.auth_type == AuthType.KIRO_DESKTOP and auth_manager.profile_arn:
        profile_arn_for_payload = auth_manager.profile_arn

    try:
        kiro_payload = build_kiro_payload(request_data, conversation_id, profile_arn_for_payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    url = f"{auth_manager.api_host}/generateAssistantResponse"
    logger.debug(f"Kiro API URL: {url}")

    # -- HTTP 客户端（带重试） --
    if request_data.stream:
        http_client = KiroHttpClient(auth_manager, shared_client=None)
    else:
        shared_client = request.app.state.http_client
        http_client = KiroHttpClient(auth_manager, shared_client=shared_client)

    messages_for_tokenizer = [msg.model_dump() for msg in request_data.messages]
    tools_for_tokenizer = (
        [tool.model_dump() for tool in request_data.tools] if request_data.tools else None
    )

    try:
        response = await http_client.request_with_retry("POST", url, kiro_payload, stream=True)

        if response.status_code != 200:
            try:
                error_content = await response.aread()
            except Exception:
                error_content = b"Unknown error"
            await http_client.close()
            error_text = error_content.decode("utf-8", errors="replace")
            error_message = error_text
            try:
                error_json = json.loads(error_text)
                error_info = enhance_kiro_error(error_json)
                error_message = error_info.user_message
                logger.debug(f"Kiro error: {error_info.original_message} (reason: {error_info.reason})")
            except (json.JSONDecodeError, KeyError):
                pass
            logger.warning(f"HTTP {response.status_code} - {error_message[:200]}")
            return JSONResponse(
                status_code=response.status_code,
                content={"error": {"message": error_message, "type": "kiro_api_error", "code": response.status_code}},
            )

        if request_data.stream:
            async def stream_wrapper():
                streaming_error = None
                client_disconnected = False
                accumulated_usage = {"prompt_tokens": 0, "completion_tokens": 0}
                try:
                    async for chunk in stream_kiro_to_openai(
                        http_client.client, response, request_data.model,
                        model_cache, auth_manager,
                        request_messages=messages_for_tokenizer,
                        request_tools=tools_for_tokenizer,
                    ):
                        # 尝试从 SSE chunk 中提取 usage
                        if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                            try:
                                chunk_data = json.loads(chunk[6:])
                                if "usage" in chunk_data:
                                    accumulated_usage["prompt_tokens"] = chunk_data["usage"].get("prompt_tokens", 0)
                                    accumulated_usage["completion_tokens"] = chunk_data["usage"].get("completion_tokens", 0)
                            except (json.JSONDecodeError, KeyError):
                                pass
                        yield chunk
                except GeneratorExit:
                    client_disconnected = True
                    logger.debug("Client disconnected during streaming")
                except Exception as e:
                    streaming_error = e
                    try:
                        yield "data: [DONE]\n\n"
                    except Exception:
                        pass
                    raise
                finally:
                    await http_client.close()
                    if streaming_error:
                        logger.error(f"Streaming error: {type(streaming_error).__name__}: {streaming_error}")
                    elif client_disconnected:
                        logger.info("Streaming: client disconnected")
                    else:
                        logger.info("Streaming: completed")
                    try:
                        await pool.mark_token_used(token_entry["id"])
                        await pool.mark_user_used(user["id"])
                        # ── 记录用量 ──
                        p_tok = accumulated_usage["prompt_tokens"]
                        c_tok = accumulated_usage["completion_tokens"]
                        if p_tok or c_tok:
                            await pool.record_usage(user["id"], request_data.model, p_tok, c_tok, token_entry["id"])
                            logger.info(f"[{user['name']}] stream usage: prompt={p_tok} completion={c_tok}")
                    except Exception:
                        pass

            return StreamingResponse(stream_wrapper(), media_type="text/event-stream")
        else:
            openai_response = await collect_stream_response(
                http_client.client, response, request_data.model,
                model_cache, auth_manager,
                request_messages=messages_for_tokenizer,
                request_tools=tools_for_tokenizer,
            )
            await http_client.close()
            logger.info("Non-streaming: completed")
            await pool.mark_token_used(token_entry["id"])
            await pool.mark_user_used(user["id"])

            # ── 记录用量 ──
            try:
                resp_usage = openai_response.get("usage", {})
                p_tok = resp_usage.get("prompt_tokens", 0)
                c_tok = resp_usage.get("completion_tokens", 0)
                if p_tok or c_tok:
                    await pool.record_usage(user["id"], request_data.model, p_tok, c_tok, token_entry["id"])
                    logger.info(f"[{user['name']}] usage: prompt={p_tok} completion={c_tok}")
            except Exception as e:
                logger.warning(f"Failed to record usage: {e}")

            return JSONResponse(content=openai_response)

    except HTTPException as e:
        await http_client.close()
        logger.error(f"HTTP {e.status_code}: {e.detail}")
        raise
    except Exception as e:
        await http_client.close()
        logger.error(f"Internal error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")
