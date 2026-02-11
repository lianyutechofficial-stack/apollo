#!/usr/bin/env python3
"""
Apollo Local Agent v2 — 用户本机运行的轻量服务。

功能：
1. 接收网页端指令，自动切换 Cursor 账号
2. 集成 cursor-promax API，实时获取新鲜 token（无需安装插件）
3. 完整的 Cursor 环境重置（机器码、缓存、认证）

用户执行一次: python apollo_agent.py
之后网页端点击"一键切换"即可直接操作本机 Cursor。

默认监听 http://127.0.0.1:19080
"""

import json
import os
import platform
import shutil
import sqlite3
import subprocess
import time
import uuid
import urllib.request
import urllib.error
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, List, Tuple

PORT = int(os.environ.get("APOLLO_AGENT_PORT", "19080"))
_system = platform.system()

# 内嵌 UI 页面
try:
    from agent_ui import AGENT_HTML
except ImportError:
    AGENT_HTML = "<html><body><h1>Apollo Agent</h1><p>UI module not found. API is still functional.</p></body></html>"

# ═══════════════════════════════════════════════════════
#  cursor-promax API 配置
# ═══════════════════════════════════════════════════════

PROMAX_SERVERS = [
    "http://api.cursorpromax.cn",
    "http://103.91.219.135:18000",
]


def _config_path() -> Path:
    d = Path.home() / ".apollo"
    d.mkdir(exist_ok=True)
    return d / "agent_config.json"


def load_config() -> dict:
    p = _config_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def save_config(cfg: dict):
    _config_path().write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


def _http_get(url: str, params: dict = None, timeout: int = 30) -> dict:
    if params:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "ApolloAgent/2.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {"error": f"HTTP {e.code}"}
        return {"success": False, **body}


def _http_post(url: str, data: dict = None, params: dict = None, timeout: int = 30) -> dict:
    if params:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{qs}"
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "ApolloAgent/2.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
        except Exception:
            err_body = {"error": f"HTTP {e.code}"}
        return {"success": False, **err_body}


# ═══════════════════════════════════════════════════════
#  cursor-promax API 交互
# ═══════════════════════════════════════════════════════

def _get_device_id() -> str:
    cfg = load_config()
    did = cfg.get("device_id")
    if did:
        return did
    did = uuid.uuid4().hex
    cfg["device_id"] = did
    save_config(cfg)
    return did


def _get_promax_api_url() -> str:
    cfg = load_config()
    cached = cfg.get("promax_api_url")
    # 如果有缓存且最近用过，直接返回
    if cached:
        return cached
    for server in PROMAX_SERVERS:
        try:
            r = _http_get(f"{server}/api/server-config", timeout=8)
            if r.get("success") and r.get("data"):
                api_url = r["data"].get("api_url", server)
                cfg["promax_api_url"] = api_url
                save_config(cfg)
                return api_url
        except Exception:
            continue
    return PROMAX_SERVERS[0]


def promax_activate(activation_code: str) -> dict:
    api_url = _get_promax_api_url()
    device_id = _get_device_id()
    r = _http_post(f"{api_url}/api/activate", {
        "code": activation_code,
        "device_id": device_id,
        "device_name": platform.platform(),
        "plugin_version": "2.0.0-apollo",
    })
    if r.get("success"):
        cfg = load_config()
        cfg["activation_code_id"] = r["data"]["activation_code_id"]
        cfg["activation_code"] = activation_code
        cfg["user_type"] = r["data"].get("user_type", "shared")
        save_config(cfg)
        return {"ok": True, "data": r["data"]}
    return {"ok": False, "error": r.get("error", "激活失败")}


def promax_get_account() -> dict:
    cfg = load_config()
    acid = cfg.get("activation_code_id")
    if not acid:
        return {"ok": False, "error": "未激活，请先设置激活码"}
    api_url = _get_promax_api_url()
    device_id = _get_device_id()
    r = _http_get(f"{api_url}/api/current-account", params={
        "activation_code_id": acid, "device_id": device_id,
    })
    if r.get("success") and r.get("data"):
        return {"ok": True, "account": r["data"]}
    return {"ok": False, "error": r.get("error", "获取账号失败")}


def promax_quick_switch() -> dict:
    """
    智能换号：优先用 billing/request-switch（真正从号池换新号），
    失败时 fallback 到 billing/request-reassign，
    最后 fallback 到 quick-switch。
    """
    cfg = load_config()
    acid = cfg.get("activation_code_id")
    code = cfg.get("activation_code", "")
    if not acid:
        return {"ok": False, "error": "未激活，请先设置激活码"}
    api_url = _get_promax_api_url()
    device_id = _get_device_id()

    # 方式1: billing/request-switch（插件原生换号 API）
    if code:
        try:
            r = _http_post(f"{api_url}/api/billing/request-switch", data={
                "activation_code": code,
                "device_id": device_id,
                "reason": "quota_exhausted",
            })
            if r.get("success") and r.get("switched") and r.get("new_account"):
                return {"ok": True, "account": r["new_account"]}
        except Exception:
            pass

        # 方式2: billing/request-reassign
        try:
            r = _http_post(f"{api_url}/api/billing/request-reassign", params={
                "activation_code": code, "device_id": device_id,
            })
            if r.get("success") and r.get("account"):
                return {"ok": True, "account": r["account"]}
        except Exception:
            pass

    # 方式3: fallback 到旧的 quick-switch
    r = _http_post(f"{api_url}/api/quick-switch", params={
        "activation_code_id": acid, "device_id": device_id,
    })
    if r.get("success") and r.get("data"):
        return {"ok": True, "account": r["data"]}
    return {"ok": False, "error": r.get("error") or r.get("detail", "换号失败")}


# ═══════════════════════════════════════════════════════
#  Cursor 路径探测
# ═══════════════════════════════════════════════════════

def _candidate_db_paths() -> List[Path]:
    candidates = []
    home = Path.home()
    env_db = os.environ.get("CURSOR_DB_PATH")
    if env_db:
        candidates.append(Path(env_db))
    if _system == "Windows":
        for env_key in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(env_key)
            if base:
                candidates.append(Path(base) / "Cursor" / "User" / "globalStorage" / "state.vscdb")
        candidates.append(home / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb")
        candidates.append(home / "AppData" / "Local" / "Cursor" / "User" / "globalStorage" / "state.vscdb")
        install_dir = _win_registry_install_dir()
        if install_dir:
            candidates.append(install_dir / "data" / "User" / "globalStorage" / "state.vscdb")
    elif _system == "Darwin":
        candidates.append(home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))
        candidates.append(Path(xdg) / "Cursor" / "User" / "globalStorage" / "state.vscdb")
        candidates.append(home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb")
    seen = set()
    unique = []
    for p in candidates:
        s = str(p)
        if s not in seen:
            seen.add(s)
            unique.append(p)
    return unique


def _win_registry_install_dir() -> Optional[Path]:
    if _system != "Windows":
        return None
    try:
        import winreg
        for key_path in (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Cursor.exe",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\cursor.exe",
        ):
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        val, _ = winreg.QueryValueEx(key, "")
                        if val and Path(val).exists():
                            return Path(val).parent
                except FileNotFoundError:
                    continue
    except Exception:
        pass
    return None


def find_cursor_db() -> Tuple[Optional[Path], List[str]]:
    candidates = _candidate_db_paths()
    tried = []
    for p in candidates:
        tried.append(str(p))
        if p.exists():
            return p, tried
    return None, tried


def _candidate_exe_paths() -> List[Path]:
    candidates = []
    home = Path.home()
    env_exe = os.environ.get("CURSOR_EXE_PATH")
    if env_exe:
        candidates.append(Path(env_exe))
    if _system == "Windows":
        local = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        candidates.append(Path(local) / "Programs" / "cursor" / "Cursor.exe")
        candidates.append(Path(local) / "Programs" / "Cursor" / "Cursor.exe")
        candidates.append(home / "AppData" / "Local" / "Programs" / "cursor" / "Cursor.exe")
        install_dir = _win_registry_install_dir()
        if install_dir:
            candidates.append(install_dir / "Cursor.exe")
        for pf in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(pf)
            if base:
                candidates.append(Path(base) / "Cursor" / "Cursor.exe")
    elif _system == "Darwin":
        candidates.append(Path("/Applications/Cursor.app"))
        candidates.append(home / "Applications" / "Cursor.app")
    else:
        candidates.append(Path("/usr/bin/cursor"))
        candidates.append(Path("/usr/local/bin/cursor"))
        candidates.append(Path("/snap/bin/cursor"))
        candidates.append(home / ".local" / "bin" / "cursor")
    seen = set()
    unique = []
    for p in candidates:
        s = str(p)
        if s not in seen:
            seen.add(s)
            unique.append(p)
    return unique


def find_cursor_exe() -> Tuple[Optional[Path], List[str]]:
    candidates = _candidate_exe_paths()
    tried = []
    for p in candidates:
        tried.append(str(p))
        if p.exists():
            return p, tried
    # 系统命令兜底
    try:
        if _system == "Windows":
            result = subprocess.run(["where", "Cursor.exe"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    pp = Path(line.strip())
                    if pp.exists():
                        tried.append(f"(where) {pp}")
                        return pp, tried
        elif _system == "Darwin":
            result = subprocess.run(
                ["mdfind", "kMDItemCFBundleIdentifier == 'com.todesktop.230313mzl4w4u92'"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    pp = Path(line.strip())
                    if pp.exists():
                        tried.append(f"(mdfind) {pp}")
                        return pp, tried
        else:
            result = subprocess.run(["which", "cursor"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                pp = Path(result.stdout.strip())
                tried.append(f"(which) {pp}")
                return pp, tried
    except Exception:
        pass
    return None, tried


# ═══════════════════════════════════════════════════════
#  Cursor 进程管理
# ═══════════════════════════════════════════════════════

def kill_cursor() -> bool:
    try:
        if _system == "Darwin":
            subprocess.run(["osascript", "-e", 'quit app "Cursor"'], capture_output=True, timeout=5)
            time.sleep(1)
            subprocess.run(["pkill", "-f", "Cursor Helper"], capture_output=True, timeout=3)
            subprocess.run(["pkill", "-f", "Cursor.app"], capture_output=True, timeout=3)
        elif _system == "Windows":
            subprocess.run(["taskkill", "/F", "/IM", "Cursor.exe"], capture_output=True, timeout=5)
        else:
            subprocess.run(["pkill", "-f", "cursor"], capture_output=True, timeout=5)
        time.sleep(2)
        return True
    except Exception:
        return False


def launch_cursor() -> Tuple[bool, str]:
    if _system == "Darwin":
        try:
            subprocess.Popen(["open", "-a", "Cursor"])
            return True, "已启动 Cursor"
        except Exception as e:
            return False, f"启动失败: {e}"
    elif _system == "Windows":
        exe, _ = find_cursor_exe()
        if exe:
            try:
                subprocess.Popen([str(exe)])
                return True, f"已启动 Cursor ({exe})"
            except Exception as e:
                return False, f"启动失败: {e}"
        try:
            subprocess.Popen(["cmd", "/c", "start", "", "Cursor"], shell=False)
            return True, "已启动 Cursor (start)"
        except Exception:
            return False, "启动失败，请手动打开 Cursor"
    else:
        try:
            subprocess.Popen(["cursor"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "已启动 Cursor"
        except Exception as e:
            return False, f"启动失败: {e}"


# ═══════════════════════════════════════════════════════
#  机器码重置（参考 cursor-promax resetCursorMachineId）
# ═══════════════════════════════════════════════════════

def _get_cursor_data_dir(db_path: Path) -> Path:
    """从 db_path 推导 Cursor 数据根目录（…/Cursor/User/globalStorage → …/Cursor）。"""
    return db_path.parent.parent.parent


def reset_cursor_machine_ids(db_path: Path) -> List[str]:
    """
    重置 Cursor 机器码（storage.json + state.vscdb + machineId 文件）。
    返回操作日志列表。
    """
    import hashlib

    steps = []
    cursor_dir = _get_cursor_data_dir(db_path)
    global_storage = db_path.parent

    # 生成新 ID
    dev_device_id = str(uuid.uuid4())
    machine_id = hashlib.sha256(os.urandom(32)).hexdigest()
    mac_machine_id = hashlib.sha512(os.urandom(64)).hexdigest()
    sqm_id = "{" + str(uuid.uuid4()).upper() + "}"

    new_ids = {
        "telemetry.devDeviceId": dev_device_id,
        "telemetry.macMachineId": mac_machine_id,
        "telemetry.machineId": machine_id,
        "telemetry.sqmId": sqm_id,
        "storage.serviceMachineId": dev_device_id,
    }

    # 1. 更新 storage.json
    storage_json = global_storage / "storage.json"
    if storage_json.exists():
        try:
            cfg = json.loads(storage_json.read_text(encoding="utf-8"))
            cfg.update(new_ids)
            storage_json.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
            steps.append("已重置 storage.json")
        except Exception as e:
            steps.append(f"storage.json 失败: {e}")
    else:
        steps.append("storage.json 不存在，跳过")

    # 2. 更新 state.vscdb
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        for key, value in new_ids.items():
            cur.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()
        steps.append("已重置 vscdb 机器码")
    except Exception as e:
        steps.append(f"vscdb 机器码失败: {e}")

    # 3. 更新 machineId 文件
    machine_id_file = cursor_dir / "machineId"
    try:
        machine_id_file.parent.mkdir(parents=True, exist_ok=True)
        machine_id_file.write_text(dev_device_id, encoding="utf-8")
        steps.append("已重置 machineId 文件")
    except Exception as e:
        steps.append(f"machineId 文件失败: {e}")

    return steps



# ═══════════════════════════════════════════════════════
#  缓存清理（参考 cursor-promax clearCursorCache）
# ═══════════════════════════════════════════════════════

def clear_cursor_cache() -> List[str]:
    """删除 Cursor 缓存目录，返回操作日志。"""
    steps = []
    home = Path.home()

    if _system == "Darwin":
        dirs = [
            home / "Library" / "Caches" / "Cursor",
            home / "Library" / "Application Support" / "Cursor" / "Cache",
            home / "Library" / "Application Support" / "Cursor" / "CachedData",
        ]
    elif _system == "Windows":
        appdata = os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))
        localappdata = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        dirs = [
            Path(localappdata) / "Cursor" / "Cache",
            Path(localappdata) / "Cursor" / "CachedData",
            Path(appdata) / "Cursor" / "Cache",
            Path(appdata) / "Cursor" / "CachedData",
        ]
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))
        xdg_cache = os.environ.get("XDG_CACHE_HOME", str(home / ".cache"))
        dirs = [
            Path(xdg_cache) / "Cursor",
            Path(xdg) / "Cursor" / "Cache",
            Path(xdg) / "Cursor" / "CachedData",
        ]

    for d in dirs:
        if d.exists():
            try:
                shutil.rmtree(d)
                steps.append(f"已删除 {d.name}")
            except Exception as e:
                steps.append(f"删除 {d.name} 失败: {e}")
    if not steps:
        steps.append("无缓存需要清理")
    return steps


# ═══════════════════════════════════════════════════════
#  认证操作
# ═══════════════════════════════════════════════════════

AUTH_KEYS = [
    "cursorAuth/accessToken",
    "cursorAuth/refreshToken",
    "cursorAuth/workosSessionToken",
    "cursorAuth/userId",
    "cursorAuth/email",
    "cursorAuth/cachedEmail",
    "cursorAuth/stripeMembershipType",
    "cursorAuth/sign_up_type",
    "cursorAuth/cachedSignUpType",
    "cursorAuth/stripeSubscriptionStatus",
]


def clear_cursor_auth(db_path: Path) -> str:
    """清除所有 cursorAuth 字段。"""
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        for key in AUTH_KEYS:
            cur.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", (key, ""))
        conn.commit()
        conn.close()
        return "已清除旧认证"
    except Exception as e:
        return f"清除认证失败: {e}"


def write_cursor_creds(db_path: Path, account: dict) -> str:
    """
    写入新鲜凭证。account 字段兼容 cursor-promax API 返回格式：
    - access_token / accessToken
    - refresh_token / refreshToken
    - workos_token / workosSessionToken
    - email
    - user_id / userId
    """
    access_token = account.get("access_token") or account.get("accessToken") or ""
    refresh_token = account.get("refresh_token") or account.get("refreshToken") or ""
    workos_token = account.get("workos_token") or account.get("workosSessionToken") or ""
    email = account.get("email") or ""
    user_id = account.get("user_id") or account.get("userId") or ""

    # 判断 token 类型
    token = workos_token or access_token
    is_workos = "::" in token or "%3A%3A" in token

    if is_workos and not user_id:
        sep = "%3A%3A" if "%3A%3A" in token else "::"
        user_id = token.split(sep)[0]

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        if is_workos:
            entries = [
                ("cursorAuth/workosSessionToken", token),
                ("cursorAuth/accessToken", access_token if access_token != token else ""),
                ("cursorAuth/refreshToken", refresh_token),
                ("cursorAuth/email", email),
                ("cursorAuth/cachedEmail", email),
                ("cursorAuth/userId", user_id),
                ("cursorAuth/stripeMembershipType", "pro"),
                ("cursorAuth/stripeSubscriptionStatus", "active"),
                ("cursorAuth/sign_up_type", "Auth_0"),
                ("cursorAuth/cachedSignUpType", "Auth_0"),
            ]
        else:
            entries = [
                ("cursorAuth/accessToken", access_token),
                ("cursorAuth/refreshToken", refresh_token),
                ("cursorAuth/email", email),
                ("cursorAuth/cachedEmail", email),
                ("cursorAuth/userId", user_id),
                ("cursorAuth/stripeMembershipType", "pro"),
                ("cursorAuth/stripeSubscriptionStatus", "active"),
                ("cursorAuth/sign_up_type", "Auth_0"),
                ("cursorAuth/cachedSignUpType", "Auth_0"),
            ]

        for key, value in entries:
            cur.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()
        return f"已写入凭证 ({email})"
    except Exception as e:
        return f"写入凭证失败: {e}"


def verify_account_written(db_path: Path, email: str) -> Tuple[bool, str]:
    """验证凭证是否写入成功。"""
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key = ?", ("cursorAuth/email",))
        row = cur.fetchone()
        conn.close()
        if row and row[0] == email:
            return True, "验证通过"
        return False, f"验证失败: 期望 {email}，实际 {row[0] if row else '空'}"
    except Exception as e:
        return False, f"验证异常: {e}"


# ═══════════════════════════════════════════════════════
#  编排：完整切换流程
# ═══════════════════════════════════════════════════════

def do_switch(account: dict) -> dict:
    """
    完整切换流程（与 cursor-promax 插件一致）：
    1. 关闭 Cursor
    2. 找到数据库
    3. 重置机器码
    4. 清除旧认证
    5. 写入新凭证
    6. 验证写入
    7. 清除缓存
    8. 启动 Cursor
    """
    steps = []

    # 1. 关闭 Cursor
    kill_cursor()
    steps.append("关闭 Cursor")

    # 2. 找数据库
    db_path, tried = find_cursor_db()
    if not db_path:
        return {"ok": False, "error": f"未找到 Cursor 数据库。尝试过: {tried}", "steps": steps}
    steps.append(f"找到数据库: {db_path.name}")

    # 3. 重置机器码
    id_steps = reset_cursor_machine_ids(db_path)
    steps.extend(id_steps)

    # 4. 清除旧认证
    clear_msg = clear_cursor_auth(db_path)
    steps.append(clear_msg)

    # 5. 写入新凭证
    write_msg = write_cursor_creds(db_path, account)
    steps.append(write_msg)

    # 6. 验证
    email = account.get("email", "")
    ok, verify_msg = verify_account_written(db_path, email)
    steps.append(verify_msg)
    if not ok:
        return {"ok": False, "error": verify_msg, "steps": steps}

    # 7. 清除缓存
    cache_steps = clear_cursor_cache()
    steps.extend(cache_steps)

    # 8. 启动 Cursor
    launched, launch_msg = launch_cursor()
    steps.append(launch_msg)

    return {"ok": True, "email": email, "steps": steps}


def do_promax_switch() -> dict:
    """通过 cursor-promax API 获取新鲜 token 并完成切换。"""
    # 获取新鲜账号
    result = promax_quick_switch()
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "获取账号失败")}

    account = result["account"]
    return do_switch(account)


def do_status() -> dict:
    """返回当前状态信息。"""
    db_path, tried = find_cursor_db()
    cfg = load_config()

    info = {
        "ok": True,
        "system": _system,
        "db_found": db_path is not None,
        "db_path": str(db_path) if db_path else None,
        "tried_paths": tried,
        "license_activated": bool(cfg.get("activation_code_id")),
        "activation_code": cfg.get("activation_code", ""),
        "device_id": cfg.get("device_id", ""),
    }

    # 读取当前登录信息
    if db_path and db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("SELECT value FROM ItemTable WHERE key = ?", ("cursorAuth/email",))
            row = cur.fetchone()
            info["current_email"] = row[0] if row else ""
            cur.execute("SELECT value FROM ItemTable WHERE key = ?", ("cursorAuth/stripeMembershipType",))
            row = cur.fetchone()
            info["membership"] = row[0] if row else ""
            conn.close()
        except Exception:
            pass

    return info


# ═══════════════════════════════════════════════════════
#  HTTP 服务
# ═══════════════════════════════════════════════════════

class AgentHandler(BaseHTTPRequestHandler):
    """轻量 HTTP handler，供网页端调用。"""

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/" or path == "/ui":
            self._html(AGENT_HTML)

        elif path == "/ping":
            self._json(200, {"ok": True, "agent": "apollo-v2", "system": _system})

        elif path == "/status":
            self._json(200, do_status())

        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        content_len = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_len > 0:
            try:
                body = json.loads(self.rfile.read(content_len))
            except Exception:
                pass

        if path == "/switch":
            # 静态 token 切换（从网页端传入凭证）
            email = body.get("email", "")
            access_token = body.get("accessToken", "")
            refresh_token = body.get("refreshToken", "")
            if not access_token:
                self._json(400, {"ok": False, "error": "缺少 accessToken"})
                return
            account = {
                "email": email,
                "accessToken": access_token,
                "refreshToken": refresh_token,
            }
            result = do_switch(account)
            self._json(200, result)

        elif path == "/smart-switch":
            # 智能换号（自动获取新鲜 token）
            try:
                result = do_promax_switch()
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            self._json(200, result)

        elif path == "/license-activate":
            # 激活码激活
            code = body.get("code", "")
            if not code:
                self._json(400, {"ok": False, "error": "缺少激活码"})
                return
            try:
                result = promax_activate(code)
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            self._json(200, result)

        else:
            self._json(404, {"ok": False, "error": "not found"})

    def log_message(self, format, *args):
        # 简化日志
        print(f"  [{time.strftime('%H:%M:%S')}] {args[0] if args else ''}")


# ═══════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════

def _run_macos_native(url: str):
    """macOS: 用 pyobjc 创建原生 WKWebView 窗口。"""
    from Foundation import NSObject, NSURL, NSURLRequest
    from AppKit import (
        NSApplication, NSMenu, NSMenuItem,
        NSWindow, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
        NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
        NSBackingStoreBuffered, NSScreen, NSApplicationActivationPolicyRegular,
    )
    from WebKit import WKWebView

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    # ── 菜单栏（让 ⌘C ⌘V ⌘X ⌘A 生效）──
    menubar = NSMenu.alloc().init()
    app_menu = NSMenu.alloc().initWithTitle_("Apollo Agent")
    app_menu.addItemWithTitle_action_keyEquivalent_("关于 Apollo Agent", None, "")
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItemWithTitle_action_keyEquivalent_("退出 Apollo Agent", "terminate:", "q")
    app_item = NSMenuItem.alloc().init()
    app_item.setSubmenu_(app_menu)
    menubar.addItem_(app_item)

    edit_menu = NSMenu.alloc().initWithTitle_("编辑")
    edit_menu.addItemWithTitle_action_keyEquivalent_("撤销", "undo:", "z")
    edit_menu.addItemWithTitle_action_keyEquivalent_("重做", "redo:", "Z")
    edit_menu.addItem_(NSMenuItem.separatorItem())
    edit_menu.addItemWithTitle_action_keyEquivalent_("剪切", "cut:", "x")
    edit_menu.addItemWithTitle_action_keyEquivalent_("拷贝", "copy:", "c")
    edit_menu.addItemWithTitle_action_keyEquivalent_("粘贴", "paste:", "v")
    edit_menu.addItemWithTitle_action_keyEquivalent_("全选", "selectAll:", "a")
    edit_item = NSMenuItem.alloc().init()
    edit_item.setSubmenu_(edit_menu)
    menubar.addItem_(edit_item)

    app.setMainMenu_(menubar)

    # ── 窗口 ──
    screen = NSScreen.mainScreen().frame()
    w, h = 860, 740
    x = (screen.size.width - w) / 2
    y = (screen.size.height - h) / 2

    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
             NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((x, y), (w, h)), style, NSBackingStoreBuffered, False
    )
    window.setTitle_("Apollo Agent")
    window.setMinSize_((640, 500))

    webview = WKWebView.alloc().initWithFrame_(((0, 0), (w, h)))
    webview.setAutoresizingMask_(0x12)  # NSViewWidthSizable | NSViewHeightSizable
    req = NSURLRequest.requestWithURL_(NSURL.URLWithString_(url))
    webview.loadRequest_(req)
    window.setContentView_(webview)
    window.makeKeyAndOrderFront_(None)

    # 窗口关闭时退出
    class AppDelegate(NSObject):
        def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
            return True

    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.activateIgnoringOtherApps_(True)

    app.run()


def _run_windows_native(url: str):
    """Windows: 用 pywebview 创建原生 WebView2/MSHTML 窗口。"""
    import webview
    webview.create_window(
        "Apollo Agent",
        url,
        width=860,
        height=740,
        min_size=(640, 500),
        resizable=True,
    )
    webview.start()


def main():
    import threading

    url = f"http://127.0.0.1:{PORT}"

    # 启动 HTTP server（后台线程）
    server = HTTPServer(("127.0.0.1", PORT), AgentHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    opened = False

    if _system == "Darwin":
        try:
            _run_macos_native(url)
            opened = True
        except Exception:
            pass
    elif _system == "Windows":
        try:
            _run_windows_native(url)
            opened = True
        except Exception:
            pass

    if opened:
        server.shutdown()
        return

    # fallback: 浏览器（Linux 或依赖缺失时）
    webbrowser.open(url)
    try:
        server_thread.join()
    except KeyboardInterrupt:
        pass
    server.shutdown()


if __name__ == "__main__":
    main()
