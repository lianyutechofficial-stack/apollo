# Apollo Agent 构建指南

## 一、项目概述

Apollo Agent 是一个本地桌面客户端，用户双击运行后：
- 启动本地 HTTP 服务（`127.0.0.1:19080`）
- 打开原生桌面窗口（内嵌 WebView 加载本地 UI）
- 网页端（apolloinn.site 用户面板）通过 CORS 调用本地 Agent 完成 Cursor 账号切换

macOS 版已构建完成（.dmg），现在需要在 Windows 上构建 .exe。

---

## 二、需要给 Windows 构建人员的文件

只需要 `gateway-server/` 目录下的以下文件：

```
gateway-server/
├── apollo_agent.py          ← 主程序（全部功能代码）
├── agent_ui.py              ← 内嵌 HTML 页面（被 apollo_agent.py 导入）
├── build/
│   ├── apollo_agent.spec    ← PyInstaller 打包配置（已适配双平台）
│   ├── build.bat            ← Windows 一键构建脚本
│   ├── build.sh             ← macOS 构建脚本（仅参考）
│   ├── icon.ico             ← Windows 图标
│   └── icon.icns            ← macOS 图标（不需要）
```

**最简方式：把整个 `gateway-server/` 文件夹拷给 Windows 机器即可。**

构建只依赖上面这些文件，不需要 `kiro/`、`main.py`、`token_pool.py` 等服务端代码。

---

## 三、Windows 构建步骤

### 环境要求

- Python 3.10+（建议 3.11 或 3.12）
- pip

### 一键构建

```cmd
cd gateway-server
build\build.bat
```

脚本会自动：
1. 安装 `pyinstaller` 和 `pywebview`
2. 执行 PyInstaller 打包
3. 输出 `dist\ApolloAgent.exe`

### 手动构建（如果 bat 有问题）

```cmd
cd gateway-server
pip install pyinstaller pywebview
pyinstaller build\apollo_agent.spec --distpath dist --workpath build\tmp --clean -y
```

产物：`dist\ApolloAgent.exe`（单文件，约 20-40MB）

---

## 四、功能清单 & 双平台对比

### 核心功能（完全一致）

| 功能 | 说明 | macOS | Windows |
|------|------|:-----:|:-------:|
| 本地 HTTP 服务 | 监听 `127.0.0.1:19080`，供网页端调用 | ✅ | ✅ |
| 原生桌面窗口 | 独立窗口内嵌 WebView，不依赖浏览器 | ✅ WKWebView (pyobjc) | ✅ WebView2 (pywebview) |
| 内嵌 UI 页面 | 完整用户面板（登录、统计、换号、配置指南） | ✅ | ✅ |
| 智能换号 | 一键获取新鲜账号并自动切换 Cursor | ✅ | ✅ |
| 激活码激活 | 网页端自动分配激活码，Agent 自动激活 | ✅ | ✅ |
| Cursor 数据库操作 | 读写 state.vscdb（认证、机器码） | ✅ | ✅ |
| 机器码重置 | 重置 storage.json + vscdb + machineId | ✅ | ✅ |
| 缓存清理 | 删除 Cursor Cache/CachedData 目录 | ✅ | ✅ |
| 认证清除 | 清空所有 cursorAuth/* 字段 | ✅ | ✅ |
| 凭证写入 | 写入新账号的 token/email/membership | ✅ | ✅ |
| 写入验证 | 写入后回读确认 email 一致 | ✅ | ✅ |
| 关闭 Cursor | 自动关闭 Cursor 进程 | ✅ osascript + pkill | ✅ taskkill |
| 启动 Cursor | 切换完成后自动打开 Cursor | ✅ open -a | ✅ 查找 exe 启动 |
| Cursor 路径查找 | 自动扫描多个候选路径 | ✅ /Applications + mdfind | ✅ AppData + 注册表 + where |
| cursor-promax API | 对接 promax 服务获取新鲜 token | ✅ | ✅ |
| 配置持久化 | ~/.apollo/config.json 保存激活状态 | ✅ | ✅ |

### HTTP API 端点（完全一致）

| 端点 | 方法 | 功能 |
|------|------|------|
| `/` `/ui` | GET | 返回内嵌 HTML 页面 |
| `/ping` | GET | 心跳检测 |
| `/status` | GET | 返回状态（系统、数据库、激活状态、当前账号） |
| `/switch` | POST | 静态切换（传入凭证） |
| `/smart-switch` | POST | 智能换号（自动获取新鲜 token） |
| `/license-activate` | POST | 激活码激活 |

### 平台差异（仅实现方式不同，用户体验一致）

| 项目 | macOS | Windows |
|------|-------|---------|
| 原生窗口技术 | pyobjc (WKWebView) | pywebview (WebView2/MSHTML) |
| 窗口尺寸 | 860×740，最小 640×500 | 860×740，最小 640×500 |
| 菜单栏 | 原生 macOS 菜单（⌘C/V/X/A） | 系统默认（Ctrl+C/V/X/A 自动支持） |
| 关闭 Cursor | `osascript` + `pkill` | `taskkill /F /IM Cursor.exe` |
| 启动 Cursor | `open -a Cursor` | 查找 exe 路径后 `subprocess.Popen` |
| 数据库路径 | `~/Library/Application Support/Cursor/...` | `%APPDATA%/Cursor/...` |
| 缓存路径 | `~/Library/Caches/Cursor` | `%LOCALAPPDATA%/Cursor/Cache` |
| 打包产物 | `ApolloAgent.app` → `.dmg` | `ApolloAgent.exe`（单文件） |
| 打包方式 | PyInstaller BUNDLE (app bundle) | PyInstaller onefile |

---

## 五、构建后验证清单

在 Windows 上构建完成后，请逐项验证：

1. **双击启动** — `ApolloAgent.exe` 双击后应弹出独立窗口（不是浏览器）
2. **窗口 UI** — 显示 Apollo Agent 登录页面，可输入 apollo-xxx token 登录
3. **登录功能** — 输入有效 token 后进入 Dashboard，显示额度、API Keys 等
4. **Agent 状态** — 网页端（apolloinn.site 用户面板）显示 "Agent 在线"
5. **智能换号** — 点击"智能换号"能完成完整流程（关闭→重置→写入→启动）
6. **窗口关闭** — 关闭窗口后进程应完全退出

---

## 六、发布

构建完成的 `ApolloAgent.exe` 上传到 GitHub Release：

```
https://github.com/ApolloInn/ApolloInn/releases
```

文件名保持 `ApolloAgent.exe`，网页端下载链接已配置为：
```
https://github.com/ApolloInn/ApolloInn/releases/latest/download/ApolloAgent.exe
```

上传后用户端自动生效。
