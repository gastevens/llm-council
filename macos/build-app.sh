#!/bin/bash
# (Re)build ~/Applications/War Room.app from this repo.
#   macos/build-app.sh [path/to/War Room.app]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:-$HOME/Applications/War Room.app}"
ICON_SRC="$REPO/slack/icon.png"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# --- icon: macOS-style rounded square (824px on a 1024 canvas) if ImageMagick is available
MAGICK="$(command -v magick || true)"
[ -z "$MAGICK" ] && [ -x /opt/homebrew/bin/magick ] && MAGICK=/opt/homebrew/bin/magick
BASE="$TMP/base.png"
if [ -n "$MAGICK" ]; then
  "$MAGICK" "$ICON_SRC" -resize 824x824 \
    \( +clone -alpha off -fill black -colorize 100 -fill white -draw "roundrectangle 0,0 823,823 185,185" \) \
    -alpha off -compose CopyOpacity -composite -compose Over \
    -background none -gravity center -extent 1024x1024 "PNG32:$BASE"
else
  cp "$ICON_SRC" "$BASE"
fi
ICONSET="$TMP/AppIcon.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z "$s" "$s" "$BASE" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$BASE" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# --- scripts (repo path baked in)
sed "s|__REPO__|$REPO|g" "$REPO/macos/launcher.sh" > "$APP/Contents/Resources/launcher.sh"
sed "s|__REPO__|$REPO|g" "$REPO/macos/war-room-session.sh" > "$APP/Contents/Resources/war-room-session.sh"
chmod +x "$APP/Contents/Resources/launcher.sh" "$APP/Contents/Resources/war-room-session.sh"

# --- executable: a tiny Swift app so folders dropped on the icon (or
# `open -a "War Room" ~/folder`) become the context folder. Without swiftc,
# fall back to the plain launcher script (no drag-and-drop).
SWIFTC="$(command -v swiftc || true)"
if [ -n "$SWIFTC" ] && "$SWIFTC" -O -o "$APP/Contents/MacOS/war-room" "$REPO/macos/WarRoomLauncher.swift" 2>"$TMP/swiftc.log"; then
  :
else
  echo "swiftc unavailable or failed; using the script launcher (no folder drag-and-drop)." >&2
  [ -s "$TMP/swiftc.log" ] && cat "$TMP/swiftc.log" >&2
  cp "$APP/Contents/Resources/launcher.sh" "$APP/Contents/MacOS/war-room"
fi
chmod +x "$APP/Contents/MacOS/war-room"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>War Room</string>
<key>CFBundleDisplayName</key><string>War Room</string>
<key>CFBundleIdentifier</key><string>local.gregorystevens.war-room</string>
<key>CFBundleExecutable</key><string>war-room</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>LSUIElement</key><true/>
<key>CFBundleDocumentTypes</key>
<array><dict>
  <key>CFBundleTypeName</key><string>Context folder</string>
  <key>CFBundleTypeRole</key><string>Viewer</string>
  <key>LSHandlerRank</key><string>Alternate</string>
  <key>LSItemContentTypes</key><array><string>public.folder</string><string>public.item</string></array>
</dict></array>
</dict></plist>
PLIST

codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
touch "$APP"
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LSREG" ] && "$LSREG" -f "$APP" || true
echo "Built $APP"
