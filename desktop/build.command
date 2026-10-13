#!/bin/zsh
set -eu
CONVOY_PROJECT_DIR="${0:A:h:h}"
CONVOY_BUILD_DIR=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp/}agent-convoy-build.XXXXXX")
CONVOY_APP_DIR="$CONVOY_PROJECT_DIR/Agent Convoy.app"
mkdir -p "$CONVOY_BUILD_DIR/icon.iconset" "$CONVOY_APP_DIR/Contents/MacOS" "$CONVOY_APP_DIR/Contents/Resources"
/usr/bin/swiftc -module-cache-path "$CONVOY_BUILD_DIR/module-cache" \
  "$CONVOY_PROJECT_DIR/desktop/main.swift" -o "$CONVOY_BUILD_DIR/AgentConvoy" -framework AppKit
for CONVOY_ICON_SIZE in 16 32 128 256 512; do
  /usr/bin/sips -z "$CONVOY_ICON_SIZE" "$CONVOY_ICON_SIZE" "$CONVOY_PROJECT_DIR/assets/agent-convoy-logo.png" \
    --out "$CONVOY_BUILD_DIR/icon.iconset/icon_${CONVOY_ICON_SIZE}x${CONVOY_ICON_SIZE}.png" >/dev/null
  CONVOY_DOUBLE_SIZE=$((CONVOY_ICON_SIZE * 2))
  /usr/bin/sips -z "$CONVOY_DOUBLE_SIZE" "$CONVOY_DOUBLE_SIZE" "$CONVOY_PROJECT_DIR/assets/agent-convoy-logo.png" \
    --out "$CONVOY_BUILD_DIR/icon.iconset/icon_${CONVOY_ICON_SIZE}x${CONVOY_ICON_SIZE}@2x.png" >/dev/null
done
/usr/bin/iconutil -c icns "$CONVOY_BUILD_DIR/icon.iconset" -o "$CONVOY_BUILD_DIR/AgentConvoy.icns"
/bin/cp "$CONVOY_BUILD_DIR/AgentConvoy" "$CONVOY_APP_DIR/Contents/MacOS/AgentConvoy"
/bin/cp "$CONVOY_PROJECT_DIR/desktop/Info.plist" "$CONVOY_APP_DIR/Contents/Info.plist"
/bin/cp "$CONVOY_BUILD_DIR/AgentConvoy.icns" "$CONVOY_APP_DIR/Contents/Resources/AgentConvoy.icns"
/usr/bin/codesign --force --sign - "$CONVOY_APP_DIR"
print "应用已生成：$CONVOY_APP_DIR"
print "双击应用或拖入程序坞。使用 Command-Q 退出时会同时关闭本机服务。"
