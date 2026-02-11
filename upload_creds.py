#!/usr/bin/env python3
"""
Apollo Gateway — 本机凭证提取 & 上传脚本

从本机读取 Kiro / Cursor 登录凭证，上传到线上 Apollo Gateway。
用法: python3 upload_creds.py [--api URL] [--key ADMIN_KEY] [--kiro] [--cursor] [--all]
"""

import argparse
import json
import os
import platform
import sqlite3
import sys
from pathlib import Path

try:
    import urllib.request
except ImportError:
    pass

API_BASE = os.environ.get("APOLLO_API", "http://207.148.73.138:8000")
ADMIN_KEY = os.environ.get("APOLLO_ADMIN_KEY", "Ljc17748697418.")


CFG = {"api": API_BASE, "key": ADMIN_KEY}


def post(path: str, data: dict) -> dict:
    """发送 POST 请求到 Apollo API。"""
    url = CFG["api"].rstrip("/") + path
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"X-Admin-Key": CFG["key"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  ✗ 上传失败: {e}")
        return {}


# ── Kiro 凭证提取 ──

def get_kiro_creds() -> dict | None:
    """从本机 AWS SSO cache 读取 Kiro 凭证。"""
    sso_dir = Path.home() / ".aws" / "sso" / "cache"
    auth_file = sso_dir / "kiro-auth-token.json"

    if not auth_file.exists():
        print("  ✗ 未找到 kiro-auth-token.json")
        print(f"    路径: {auth_file}")
        return None

    with open(auth_file) as f:
        auth = json.load(f)

    # 查找 device registration（clientId / clientSecret）
    client_id_hash = auth.get("clientIdHash", "")
    device_file = sso_dir / f"{client_id_hash}.json"
    device = {}
    if device_file.exists():
        with open(device_file) as f:
            device = json.load(f)

    cred = {
        "refreshToken": auth.get("refreshToken", ""),
        "accessToken": auth.get("accessToken", ""),
        "expiresAt": auth.get("expiresAt", ""),
        "region": auth.get("region", "us-east-1"),
        "clientId": device.get("clientId", ""),
        "clientSecret": device.get("clientSecret", ""),
        "authMethod": "AWS_SSO_OIDC",
        "provider": auth.get("provider", "Enterprise"),
        "clientIdHash": client_id_hash,
    }

    if not cred["refreshToken"]:
        print("  ✗ Kiro 凭证无 refreshToken")
        return None

    return cred


def upload_kiro():
    """提取并上传 Kiro 凭证。"""
    print("\n🔑 Kiro 凭证")
    cred = get_kiro_creds()
    if not cred:
        return
    print(f"  Region: {cred['region']}")
    print(f"  Auth: {cred['authMethod']}")
    print(f"  RefreshToken: {cred['refreshToken'][:20]}...")

    result = post("/admin/tokens", cred)
    if result.get("token"):
        tid = result["token"]["id"]
        print(f"  ✓ 已上传，ID: {tid}")
    else:
        print("  ✗ 上传失败")


# ── Cursor 凭证提取 ──

def get_cursor_db_path() -> Path:
    """跨平台获取 Cursor state.vscdb 路径（多策略扫描）。"""
    try:
        from cursor_utils import find_cursor_db
        db_path, _ = find_cursor_db()
        if db_path:
            return db_path
    except ImportError:
        pass
    # fallback: 直接扫描常见路径
    system = platform.system()
    candidates = []
    if system == "Darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb")
    elif system == "Windows":
        for env_key in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(env_key)
            if base:
                candidates.append(Path(base) / "Cursor" / "User" / "globalStorage" / "state.vscdb")
        candidates.append(Path.home() / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb")
        candidates.append(Path.home() / "AppData" / "Local" / "Cursor" / "User" / "globalStorage" / "state.vscdb")
    else:
        candidates.append(Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb")
    for p in candidates:
        if p.exists():
            return p
    # 返回第一个候选路径（即使不存在，让调用方报错）
    return candidates[0] if candidates else Path("state.vscdb")


def get_cursor_creds() -> dict | None:
    """从本机 Cursor state.vscdb 读取登录凭证。"""
    db_path = get_cursor_db_path()
    if not db_path.exists():
        print(f"  ✗ 未找到 Cursor 数据库: {db_path}")
        return None

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    kv = {}
    for key in ["cursorAuth/accessToken", "cursorAuth/refreshToken",
                "cursorAuth/cachedEmail", "cursorAuth/stripeMembershipType"]:
        cur.execute("SELECT value FROM ItemTable WHERE key = ?", (key,))
        row = cur.fetchone()
        kv[key.split("/")[-1]] = row[0] if row else ""
    conn.close()

    if not kv.get("accessToken") and not kv.get("refreshToken"):
        print("  ✗ Cursor 未登录（无 token）")
        return None

    return {
        "email": kv.get("cachedEmail", ""),
        "accessToken": kv.get("accessToken", ""),
        "refreshToken": kv.get("refreshToken", ""),
        "note": f"本机提取 · {kv.get('stripeMembershipType', 'unknown')}",
    }


def upload_cursor():
    """提取并上传 Cursor 凭证。"""
    print("\n🖱  Cursor 凭证")
    cred = get_cursor_creds()
    if not cred:
        return
    print(f"  Email: {cred['email']}")
    print(f"  Note: {cred['note']}")

    result = post("/admin/cursor-tokens", cred)
    if result.get("token"):
        tid = result["token"]["id"]
        print(f"  ✓ 已上传，ID: {tid}")
    else:
        print("  ✗ 上传失败")


# ── Main ──

def main():
    parser = argparse.ArgumentParser(description="Apollo 本机凭证提取 & 上传")
    parser.add_argument("--api", default=API_BASE, help="Apollo API 地址")
    parser.add_argument("--key", default=ADMIN_KEY, help="Admin Key")
    parser.add_argument("--kiro", action="store_true", help="只提取 Kiro")
    parser.add_argument("--cursor", action="store_true", help="只提取 Cursor")
    parser.add_argument("--all", action="store_true", help="提取全部（默认）")
    args = parser.parse_args()

    CFG["api"] = args.api
    CFG["key"] = args.key

    print(f"Apollo Gateway: {CFG['api']}")

    do_all = args.all or (not args.kiro and not args.cursor)

    if do_all or args.kiro:
        upload_kiro()
    if do_all or args.cursor:
        upload_cursor()

    print("\n完成。")


if __name__ == "__main__":
    main()
