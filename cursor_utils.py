"""
Cursor 路径探测工具 — 跨平台、多策略、不会漏。

策略优先级：
1. 环境变量 CURSOR_DB_PATH / CURSOR_EXE_PATH（用户显式指定，最高优先）
2. 已知默认路径扫描（APPDATA / LOCALAPPDATA / Home）
3. Windows 注册表查询（App Paths / Uninstall）
4. Windows `where` 命令兜底
5. macOS Spotlight (`mdfind`) 兜底
"""

import os
import platform
import subprocess
import sqlite3
from pathlib import Path
from typing import Optional, List, Tuple

from loguru import logger

_system = platform.system()


# ═══════════════════════════════════════════════════════
#  数据库路径探测
# ═══════════════════════════════════════════════════════

def _candidate_db_paths() -> List[Path]:
    """返回所有可能的 state.vscdb 路径，按优先级排列。"""
    candidates = []

    # 0. 用户显式指定
    env_db = os.environ.get("CURSOR_DB_PATH")
    if env_db:
        candidates.append(Path(env_db))

    home = Path.home()

    if _system == "Windows":
        # APPDATA（Roaming）— 最常见
        for env_key in ("APPDATA",):
            base = os.environ.get(env_key)
            if base:
                candidates.append(Path(base) / "Cursor" / "User" / "globalStorage" / "state.vscdb")
        # 兜底 Roaming
        candidates.append(home / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb")

        # LOCALAPPDATA — 某些版本装在这里
        for env_key in ("LOCALAPPDATA",):
            base = os.environ.get(env_key)
            if base:
                candidates.append(Path(base) / "Cursor" / "User" / "globalStorage" / "state.vscdb")
        candidates.append(home / "AppData" / "Local" / "Cursor" / "User" / "globalStorage" / "state.vscdb")

        # 便携版 / 自定义安装 — 从注册表找安装目录再推导
        install_dir = _win_registry_install_dir()
        if install_dir:
            # 有些便携版把数据放在安装目录旁边
            candidates.append(install_dir / "data" / "User" / "globalStorage" / "state.vscdb")

    elif _system == "Darwin":
        candidates.append(home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb")

    else:  # Linux
        xdg = os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))
        candidates.append(Path(xdg) / "Cursor" / "User" / "globalStorage" / "state.vscdb")
        candidates.append(home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb")

    # 去重，保持顺序
    seen = set()
    unique = []
    for p in candidates:
        resolved = str(p)
        if resolved not in seen:
            seen.add(resolved)
            unique.append(p)
    return unique


def find_cursor_db() -> Tuple[Optional[Path], List[str]]:
    """
    找到 Cursor state.vscdb 数据库路径。

    Returns:
        (db_path, tried_paths)  — db_path 为 None 表示全部失败
    """
    candidates = _candidate_db_paths()
    tried = []
    for p in candidates:
        tried.append(str(p))
        if p.exists():
            logger.debug(f"Cursor DB found: {p}")
            return p, tried

    logger.warning(f"Cursor DB not found. Tried: {tried}")
    return None, tried


# ═══════════════════════════════════════════════════════
#  可执行文件路径探测
# ═══════════════════════════════════════════════════════

def _win_registry_install_dir() -> Optional[Path]:
    """从 Windows 注册表查找 Cursor 安装目录。"""
    if _system != "Windows":
        return None
    try:
        import winreg
        # App Paths
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

        # Uninstall 信息
        uninstall_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, uninstall_key) as key:
                    i = 0
                    while True:
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            i += 1
                            if "cursor" not in subkey_name.lower():
                                continue
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                loc, _ = winreg.QueryValueEx(subkey, "InstallLocation")
                                if loc and Path(loc).exists():
                                    return Path(loc)
                        except OSError:
                            break
            except FileNotFoundError:
                continue
    except Exception as e:
        logger.debug(f"Registry lookup failed: {e}")
    return None


def _candidate_exe_paths() -> List[Path]:
    """返回所有可能的 Cursor 可执行文件路径。"""
    candidates = []
    home = Path.home()

    # 0. 用户显式指定
    env_exe = os.environ.get("CURSOR_EXE_PATH")
    if env_exe:
        candidates.append(Path(env_exe))

    if _system == "Windows":
        local = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        candidates.append(Path(local) / "Programs" / "cursor" / "Cursor.exe")
        candidates.append(Path(local) / "Programs" / "Cursor" / "Cursor.exe")
        candidates.append(home / "AppData" / "Local" / "Programs" / "cursor" / "Cursor.exe")

        # 注册表
        install_dir = _win_registry_install_dir()
        if install_dir:
            candidates.append(install_dir / "Cursor.exe")

        # Program Files
        for pf in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(pf)
            if base:
                candidates.append(Path(base) / "Cursor" / "Cursor.exe")

    elif _system == "Darwin":
        candidates.append(Path("/Applications/Cursor.app"))
        candidates.append(home / "Applications" / "Cursor.app")

    else:  # Linux
        candidates.append(Path("/usr/bin/cursor"))
        candidates.append(Path("/usr/local/bin/cursor"))
        candidates.append(Path("/snap/bin/cursor"))
        candidates.append(home / ".local" / "bin" / "cursor")

    seen = set()
    unique = []
    for p in candidates:
        resolved = str(p)
        if resolved not in seen:
            seen.add(resolved)
            unique.append(p)
    return unique


def _find_exe_via_system_command() -> Optional[Path]:
    """用系统命令兜底查找 Cursor 可执行文件。"""
    try:
        if _system == "Windows":
            # where 命令
            result = subprocess.run(
                ["where", "Cursor.exe"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    p = Path(line.strip())
                    if p.exists():
                        return p
            # PowerShell Get-Command
            result = subprocess.run(
                ["powershell", "-Command", "(Get-Command Cursor.exe -ErrorAction SilentlyContinue).Source"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                p = Path(result.stdout.strip())
                if p.exists():
                    return p

        elif _system == "Darwin":
            result = subprocess.run(
                ["mdfind", "kMDItemCFBundleIdentifier == 'com.todesktop.230313mzl4w4u92'"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    p = Path(line.strip())
                    if p.exists():
                        return p
        else:
            result = subprocess.run(["which", "cursor"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return Path(result.stdout.strip())
    except Exception as e:
        logger.debug(f"System command exe lookup failed: {e}")
    return None


def find_cursor_exe() -> Tuple[Optional[Path], List[str]]:
    """
    找到 Cursor 可执行文件路径。

    Returns:
        (exe_path, tried_paths)
    """
    candidates = _candidate_exe_paths()
    tried = []
    for p in candidates:
        tried.append(str(p))
        if p.exists():
            logger.debug(f"Cursor exe found: {p}")
            return p, tried

    # 兜底：系统命令
    exe = _find_exe_via_system_command()
    if exe:
        tried.append(f"(system command) {exe}")
        logger.debug(f"Cursor exe found via system command: {exe}")
        return exe, tried

    logger.warning(f"Cursor exe not found. Tried: {tried}")
    return None, tried


# ═══════════════════════════════════════════════════════
#  高层操作
# ═══════════════════════════════════════════════════════

def read_cursor_creds() -> Optional[dict]:
    """从本机 Cursor state.vscdb 读取登录凭证。兼容 WorkOS 和旧版格式。"""
    db_path, tried = find_cursor_db()
    if not db_path:
        logger.warning(f"Cursor DB not found, tried: {tried}")
        return None

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        kv = {}
        for key in [
            "cursorAuth/workosSessionToken",
            "cursorAuth/email",
            "cursorAuth/userId",
            "cursorAuth/accessToken",
            "cursorAuth/refreshToken",
            "cursorAuth/cachedEmail",
            "cursorAuth/stripeMembershipType",
            "cursorAuth/stripeSubscriptionStatus",
        ]:
            cur.execute("SELECT value FROM ItemTable WHERE key = ?", (key,))
            row = cur.fetchone()
            kv[key.split("/")[-1]] = row[0] if row else ""
        conn.close()
    except Exception as e:
        logger.error(f"读取 Cursor 数据库失败 ({db_path}): {e}")
        return None

    # WorkOS 格式优先
    workos_token = kv.get("workosSessionToken", "")
    email = kv.get("email", "") or kv.get("cachedEmail", "")
    access_token = workos_token or kv.get("accessToken", "")
    refresh_token = kv.get("refreshToken", "")

    if not access_token and not refresh_token:
        return None

    return {
        "email": email,
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "membership": kv.get("stripeMembershipType", ""),
        "subscriptionStatus": kv.get("stripeSubscriptionStatus", ""),
        "userId": kv.get("userId", ""),
        "dbPath": str(db_path),
        "authType": "workos" if workos_token else "legacy",
    }


def clear_cursor_auth(db_path: Path) -> None:
    """
    清除所有 Cursor 认证字段（设为空字符串）。
    参考 cursor-promax 插件的 clearCursorAuthFromLocal 实现。
    必须在写入新凭证之前调用，否则 Cursor 会读取缓存的旧 token。
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    keys_to_clear = [
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
    for key in keys_to_clear:
        cur.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", (key, ""))
    conn.commit()
    conn.close()
    logger.debug(f"Cleared all cursorAuth fields in {db_path}")


def write_cursor_creds(db_path: Path, email: str, access_token: str, refresh_token: str) -> None:
    """
    写入凭证到 Cursor state.vscdb。先清除所有旧认证字段，再写入新凭证。

    access_token 可能是：
    - WorkOS session token（格式: userId::jwt 或 userId%3A%3Ajwt）→ 写入 workosSessionToken
    - 纯 JWT / 旧版 token → 写入 accessToken
    """
    # 第一步：清除所有旧认证字段
    clear_cursor_auth(db_path)

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    is_workos = "::" in access_token or "%3A%3A" in access_token

    if is_workos:
        if "%3A%3A" in access_token:
            user_id = access_token.split("%3A%3A")[0]
        else:
            user_id = access_token.split("::")[0]

        entries = [
            ("cursorAuth/workosSessionToken", access_token),
            ("cursorAuth/email", email),
            ("cursorAuth/userId", user_id),
            ("cursorAuth/cachedEmail", email),
            ("cursorAuth/stripeMembershipType", "pro"),
            ("cursorAuth/stripeSubscriptionStatus", "active"),
            ("cursorAuth/sign_up_type", "Auth_0"),
            ("cursorAuth/cachedSignUpType", "Auth_0"),
        ]
    else:
        entries = [
            ("cursorAuth/accessToken", access_token),
            ("cursorAuth/refreshToken", refresh_token),
            ("cursorAuth/cachedEmail", email),
            ("cursorAuth/email", email),
            ("cursorAuth/sign_up_type", "Auth_0"),
            ("cursorAuth/cachedSignUpType", "Auth_0"),
            ("cursorAuth/stripeMembershipType", "pro"),
            ("cursorAuth/stripeSubscriptionStatus", "active"),
        ]

    for key, value in entries:
        if value:
            cur.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", (key, value))

    conn.commit()
    conn.close()


def kill_cursor() -> bool:
    """关闭 Cursor 进程。返回是否成功。"""
    try:
        if _system == "Darwin":
            subprocess.run(["osascript", "-e", 'quit app "Cursor"'], capture_output=True, timeout=5)
            subprocess.run(["pkill", "-f", "Cursor Helper"], capture_output=True, timeout=3)
            subprocess.run(["pkill", "-f", "Cursor.app"], capture_output=True, timeout=3)
        elif _system == "Windows":
            subprocess.run(["taskkill", "/F", "/IM", "Cursor.exe"], capture_output=True, timeout=5)
        else:
            subprocess.run(["pkill", "-f", "cursor"], capture_output=True, timeout=5)
        return True
    except Exception as e:
        logger.warning(f"关闭 Cursor 失败: {e}")
        return False


def launch_cursor() -> Tuple[bool, str]:
    """启动 Cursor。返回 (成功, 消息)。"""
    if _system == "Darwin":
        try:
            subprocess.Popen(["open", "-a", "Cursor"])
            return True, "启动 Cursor"
        except Exception as e:
            return False, f"启动失败: {e}"

    elif _system == "Windows":
        exe, tried = find_cursor_exe()
        if exe:
            try:
                subprocess.Popen([str(exe)])
                return True, f"启动 Cursor ({exe})"
            except Exception as e:
                return False, f"启动失败 ({exe}): {e}"
        # 最后兜底：用 start 命令
        try:
            subprocess.Popen(["cmd", "/c", "start", "", "Cursor"], shell=False)
            return True, "启动 Cursor (start 命令)"
        except Exception as e:
            return False, f"启动失败，请手动打开 Cursor。尝试过: {tried}"

    else:  # Linux
        try:
            subprocess.Popen(["cursor"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "启动 Cursor"
        except Exception as e:
            return False, f"启动失败: {e}"
