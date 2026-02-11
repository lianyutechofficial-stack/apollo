"""
Auth Bridge — 为 token 池中的每个凭证创建独立的 KiroAuthManager。

每个 token 有自己的 auth manager 实例，独立刷新，互不影响。
"""

import json
import tempfile
from typing import Dict, Optional, Any

from loguru import logger


from kiro.auth import KiroAuthManager
from kiro.config import get_kiro_api_host, get_kiro_q_host


class AuthBridge:
    """为每个 pool token 维护一个 KiroAuthManager 实例。"""

    def __init__(self):
        self._managers: Dict[str, KiroAuthManager] = {}

    def get_or_create_manager(self, token_entry: Dict[str, Any]) -> KiroAuthManager:
        """
        获取或创建 token 对应的 auth manager。

        Args:
            token_entry: token pool 中的一条记录

        Returns:
            KiroAuthManager 实例
        """
        token_id = token_entry["id"]

        if token_id in self._managers:
            return self._managers[token_id]

        region = token_entry.get("region", "us-east-1")

        # 如果有 clientId/clientSecret，直接传入
        client_id = token_entry.get("clientId", "")
        client_secret = token_entry.get("clientSecret", "")

        # 如果有 clientIdHash 但没有 clientId，需要写临时文件让 auth manager 加载
        creds_file = None
        if token_entry.get("clientIdHash") and not client_id:
            # 写一个临时 JSON 凭证文件
            creds = {
                "refreshToken": token_entry.get("refreshToken", ""),
                "accessToken": token_entry.get("accessToken", ""),
                "expiresAt": token_entry.get("expiresAt", ""),
                "region": region,
                "clientIdHash": token_entry.get("clientIdHash", ""),
                "profileArn": token_entry.get("profileArn", ""),
            }
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, prefix="kiro_creds_"
            )
            json.dump(creds, tmp, ensure_ascii=False)
            tmp.close()
            creds_file = tmp.name

        manager = KiroAuthManager(
            refresh_token=token_entry.get("refreshToken", "") or None,
            profile_arn=token_entry.get("profileArn", "") or None,
            region=region,
            creds_file=creds_file,
            client_id=client_id or None,
            client_secret=client_secret or None,
        )

        self._managers[token_id] = manager
        logger.debug(f"AuthManager created for token {token_id}, type={manager.auth_type}")
        return manager

    def remove_manager(self, token_id: str) -> None:
        self._managers.pop(token_id, None)

    async def get_access_token(self, token_entry: Dict[str, Any]) -> str:
        """获取有效的 access token（自动刷新）。"""
        manager = self.get_or_create_manager(token_entry)
        return await manager.get_access_token()

    def get_headers(self, token_entry: Dict[str, Any], access_token: str) -> Dict[str, str]:
        """构建 Kiro API 请求头。"""
        manager = self.get_or_create_manager(token_entry)
        from kiro.utils import get_kiro_headers
        return get_kiro_headers(manager, access_token)

    def get_api_host(self, token_entry: Dict[str, Any]) -> str:
        manager = self.get_or_create_manager(token_entry)
        return manager._api_host

    def get_q_host(self, token_entry: Dict[str, Any]) -> str:
        manager = self.get_or_create_manager(token_entry)
        return manager._q_host
