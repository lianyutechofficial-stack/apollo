# -*- mode: python ; coding: utf-8 -*-
import platform

block_cipher = None
is_mac = platform.system() == "Darwin"
is_win = platform.system() == "Windows"

# 平台相关的隐藏导入
hidden = ["agent_ui"]
if is_mac:
    hidden += ["objc", "Foundation", "AppKit", "WebKit"]
elif is_win:
    hidden += [
        "webview", "webview.platforms", "webview.platforms.edgechromium",
        "clr", "pythonnet",
    ]

a = Analysis(
    ["../apollo_agent.py"],
    pathex=[],
    binaries=[],
    datas=[("../agent_ui.py", ".")],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "doctest"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if is_win:
    # Windows: 单文件 .exe
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="ApolloAgent",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        icon="icon.ico",
    )
else:
    # macOS / Linux: app bundle
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="ApolloAgent",
        debug=False,
        bootloader_ignore_signals=False,
        strip=True,
        upx=True,
        console=False,
        icon="icon.icns" if is_mac else None,
        target_arch=None,
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=True,
        upx=True,
        name="ApolloAgent",
    )

    if is_mac:
        app = BUNDLE(
            coll,
            name="ApolloAgent.app",
            icon="icon.icns",
            bundle_identifier="site.apolloinn.agent",
            info_plist={
                "CFBundleShortVersionString": "2.0.0",
                "CFBundleName": "Apollo Agent",
                "LSBackgroundOnly": False,
                "LSUIElement": False,
                "NSAppTransportSecurity": {
                    "NSAllowsArbitraryLoads": True,
                },
            },
        )
