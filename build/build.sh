#!/bin/bash
# ============================================
#  Apollo Agent 打包脚本
#  macOS 上运行生成 .dmg，Windows 上生成 .exe
# ============================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$SCRIPT_DIR"
DIST_DIR="$PROJECT_DIR/dist"

echo "╔══════════════════════════════════════╗"
echo "║   Apollo Agent Build                 ║"
echo "╚══════════════════════════════════════╝"
echo ""

# 确保 pyinstaller 已安装
if ! command -v pyinstaller &> /dev/null; then
    echo "安装 PyInstaller..."
    pip3 install pyinstaller
fi

# 清理旧构建
rm -rf "$PROJECT_DIR/dist" "$PROJECT_DIR/__pycache__"

# 打包
echo "开始打包..."
cd "$PROJECT_DIR"
pyinstaller build/apollo_agent.spec --distpath dist --workpath build/tmp --clean -y

OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
    APP_PATH="$DIST_DIR/ApolloAgent.app"
    DMG_PATH="$DIST_DIR/ApolloAgent.dmg"

    if [ -d "$APP_PATH" ]; then
        echo ""
        echo "生成 DMG..."

        # 创建临时 DMG 目录
        DMG_TMP="$BUILD_DIR/tmp_dmg"
        rm -rf "$DMG_TMP"
        mkdir -p "$DMG_TMP"
        cp -R "$APP_PATH" "$DMG_TMP/"

        # 添加 Applications 快捷方式
        ln -s /Applications "$DMG_TMP/Applications"

        # 生成 DMG
        hdiutil create -volname "Apollo Agent" \
            -srcfolder "$DMG_TMP" \
            -ov -format UDZO \
            "$DMG_PATH"

        rm -rf "$DMG_TMP"

        echo ""
        echo "✓ 构建完成:"
        echo "  App: $APP_PATH"
        echo "  DMG: $DMG_PATH"
        SIZE=$(du -sh "$DMG_PATH" | cut -f1)
        echo "  大小: $SIZE"
    fi
else
    EXE_PATH="$DIST_DIR/ApolloAgent.exe"
    if [ -f "$EXE_PATH" ]; then
        echo ""
        echo "✓ 构建完成:"
        echo "  EXE: $EXE_PATH"
        SIZE=$(du -sh "$EXE_PATH" | cut -f1)
        echo "  大小: $SIZE"
    fi
fi

echo ""
echo "用户下载后双击即可运行，无需安装 Python。"
