#!/usr/bin/env python3
"""
Apollo Local Agent — 用户本机运行的轻量服务。

用户执行一次: python apollo_agent.py
之后网页端点击"一键切换"即可直接操作本机 Cursor。

默认监听 http://127.0.0.1:19080
"""

import json
import os
import platform
import sqlite3
import subprocess
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, List, Tuple

PORT = int(os.environ.get("APOLLO_AGENT_PORT", "19080"))
_system = platform.system()


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
        # 注册表
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


def find_cursor_exe() -> Optional[Path]:
    home = Path.home()
    candidates = []
    env_exe = os.environ.get("CURSOR_EXE_PATH")
    if env_exe:
        candidates.append(Path(env_exe))

    if _system == "Windows":
        local = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        candidates.append(Path(local) / "Programs" / "cursor" / "Cursor.exe")
        candidates.append(Path(local) / "Programs" / "Cursor" / "Cursor.exe")
        install_dir = _win_registry_install_dir()
        if install_dir:
            candidates.append(install_dir / "Cursor.exe")
    elif _system == "Darwin":
        candidates.append(Path("/Applications/Cursor.app"))
        candidates.append(home / "Applications" / "Cursor.app")
    else:
        candidates.append(Path("/usr/bin/cursor"))
        candidates.append(home / ".local" / "bin" / "cursor")

    for p in candidates:
        if p.exists():
            return p

    # 兜底
    try:
        if _system == "Windows":
            r = subprocess.run(["where", "Cursor.exe"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    p = Path(line.strip())
                    if p.exists():
                        return p
        elif _system == "Darwin":
            r = subprocess.run(
                ["mdfind", "kMDItemCFBundleIdentifier == 'com.todesktop.230313mzl4w4u92'"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    p = Path(line.strip())
                    if p.exists():
                        return p
        else:
            r = subprocess.run(["which", "cursor"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return Path(r.stdout.strip())
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════
#  操作
# ═══════════════════════════════════════════════════════

def kill_cursor() -> str:
    try:
        if _system == "Darwin":
            subprocess.run(["osascript", "-e", 'quit app "Cursor"'], capture_output=True, timeout=5)
            subprocess.run(["pkill", "-f", "Cursor"], capture_output=True, timeout=3)
        elif _system == "Windows":
            subprocess.run(["taskkill", "/F", "/IM", "Cursor.exe"], capture_output=True, timeout=5)
        else:
            subprocess.run(["pkill", "-f", "cursor"], capture_output=True, timeout=5)
        return "ok"
    except Exception as e:
        return str(e)


def launch_cursor() -> str:
    exe = find_cursor_exe()
    try:
        if _system == "Darwin":
            app = exe if exe else Path("/Applications/Cursor.app")
            subprocess.Popen(["open", "-a", str(app)])
            return "ok"
        elif _system == "Windows":
            if exe:
                subprocess.Popen([str(exe)])
                return "ok"
            subprocess.Popen(["cmd", "/c", "start", "", "Cursor"], shell=False)
            return "ok (start)"
        else:
            subprocess.Popen(["cursor"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "ok"
    except Exception as e:
        return str(e)


def write_creds(db_path: Path, email: str, access_token: str, refresh_token: str) -> None:
    """
    写入 Cursor 登录凭证。

    Cursor 当前版本使用 WorkOS 认证，核心字段是 workosSessionToken。
    同时兼容写入旧版字段（accessToken/refreshToken）以防回退。

    access_token 可能是：
    - WorkOS session token（格式: userId::jwt）→ 写入 workosSessionToken
    - 纯 JWT / 旧版 token → 写入 accessToken
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # 判断是否是 WorkOS 格式（包含 :: 分隔的 userId 和 jwt）
    is_workos = "::" in access_token or "%3A%3A" in access_token

    if is_workos:
        # 解析 userId
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
            ("cursorAuth/cachedSignUpType", "Auth_0"),
        ]
    else:
        # 旧版格式 fallback
        entries = [
            ("cursorAuth/accessToken", access_token),
            ("cursorAuth/refreshToken", refresh_token),
            ("cursorAuth/cachedEmail", email),
            ("cursorAuth/email", email),
            ("cursorAuth/cachedSignUpType", "Auth_0"),
            ("cursorAuth/stripeMembershipType", "pro"),
            ("cursorAuth/stripeSubscriptionStatus", "active"),
        ]

    for key, value in entries:
        if value:  # 只写非空值
            cur.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", (key, value))

    conn.commit()
    conn.close()


def do_switch(data: dict) -> dict:
    email = data.get("email", "")
    access_token = data.get("accessToken", "")
    refresh_token = data.get("refreshToken", "")

    if not access_token and not refresh_token:
        return {"ok": False, "error": "缺少 accessToken 或 refreshToken"}

    steps = []

    # 1. 找数据库
    db_path, tried = find_cursor_db()
    if not db_path:
        return {"ok": False, "error": "未找到 Cursor 数据库。已尝试:\n" + "\n".join(tried)}
    steps.append(f"找到数据库: {db_path}")

    # 2. 关闭 Cursor
    r = kill_cursor()
    steps.append(f"关闭 Cursor: {r}")
    time.sleep(2)

    # 3. 写入凭证
    try:
        write_creds(db_path, email, access_token, refresh_token)
        steps.append("写入凭证成功")
    except Exception as e:
        return {"ok": False, "error": f"写入数据库失败: {e}", "steps": steps}

    # 4. 启动 Cursor
    r = launch_cursor()
    steps.append(f"启动 Cursor: {r}")

    return {"ok": True, "email": email, "steps": steps, "dbPath": str(db_path)}


def do_status() -> dict:
    db_path, tried = find_cursor_db()
    exe = find_cursor_exe()
    return {
        "ok": True,
        "system": _system,
        "dbFound": db_path is not None,
        "dbPath": str(db_path) if db_path else None,
        "dbTried": tried,
        "exeFound": exe is not None,
        "exePath": str(exe) if exe else None,
    }


# ═══════════════════════════════════════════════════════
#  HTTP 服务
# ═══════════════════════════════════════════════════════

class AgentHandler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_response(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/status":
            self._json_response(200, do_status())
        elif self.path == "/ping":
            self._json_response(200, {"ok": True, "agent": "apollo-local-agent"})
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/switch":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            result = do_switch(body)
            self._json_response(200 if result["ok"] else 500, result)
        else:
            self._json_response(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        print(f"  {args[0]}")


def main():
    print(f"""
  ╔══════════════════════════════════════╗
  ║     Apollo Local Agent  v1.0        ║
  ║     http://127.0.0.1:{PORT}           ║
  ╚══════════════════════════════════════╝
""")

    status = do_status()
    print(f"  系统: {status['system']}")
    print(f"  数据库: {'✓ ' + (status['dbPath'] or '') if status['dbFound'] else '✗ 未找到'}")
    print(f"  Cursor: {'✓ ' + (status['exePath'] or '') if status['exeFound'] else '✗ 未找到'}")
    print()
    print("  等待网页端指令... (Ctrl+C 退出)")
    print()

    server = HTTPServer(("127.0.0.1", PORT), AgentHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  已退出。")
        server.server_close()


if __name__ == "__main__":
    main()
