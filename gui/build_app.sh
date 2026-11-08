#!/bin/sh
# Собирает "Reaper to Pro Tools.app" из App.swift и Python-движка ../ptx_tools.
# Использование: ./build_app.sh [каталог для .app]   (движок — Python из ../ptx_tools, шаблоны pool_N там же)
set -e
OUT="${1:-$(dirname "$0")/build}"
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$OUT/Reaper to Pro Tools.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

VERSION="${APP_VERSION:-1.1}"
CFG="$(mktemp -d)/UpdateConfig.swift"
"$HERE/release/make_config.sh" "$CFG"
swiftc -parse-as-library -target arm64-apple-macos14.0 \
    "$HERE/ReaperToProTools/App.swift" "$HERE/ReaperToProTools/Updater.swift" "$CFG" -o "$APP/Contents/MacOS/ReaperToProTools"

PT="$HERE/../ptx_tools"
mkdir -p "$APP/Contents/Resources/ptx_tools"
for f in ptx.py ptx_model.py ptx_build.py ptx_templates.py ptx_tracks.py rpp.py reaper_to_ptx.py ptx_to_reaper.py ptx_trackstate.py ptx_structure.py ptx_templates_struct.py ptx_sends.py ptx_templates_sends.py ptx_plugins.py ptx_plugin_learn.py reaper_fx.py reaper_fx_library.py ptx_templates_plugins.py ptx_elastic.py ptx_settings.py ptx_rate_table.py ptx_video.py fx_state.py ptx_plugin_state.py ptx_slot_gen.py ptx_plugin_ids.py ptx_templates_plugins2.py ptx_templates_params.py ptx_templates_marker.py ptx_groups.py ptx_templates_groups.py ptx_plugin_scan.py ptx_tempo.py; do
    cp "$PT/$f" "$APP/Contents/Resources/ptx_tools/$f"
done
"$HERE/../reaper_ext/build.sh" >/dev/null
mkdir -p "$APP/Contents/Resources/ReaperExtension"
cp "$HERE/../reaper_ext/out/reaper_pt_session_import.dylib" "$APP/Contents/Resources/ReaperExtension/"
# VST3-хост для переноса настроек плагинов (собирается отдельно: vst3host/build, см. vst3host/CMakeLists.txt)
if [ -x "$HERE/../vst3host/build/vst3host" ]; then cp "$HERE/../vst3host/build/vst3host" "$APP/Contents/Resources/vst3host"; fi
cp "$HERE/icon/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp -R "$PT/templates" "$APP/Contents/Resources/ptx_tools/templates"
cp -R "$PT/reaper_scripts" "$APP/Contents/Resources/ptx_tools/reaper_scripts"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Reaper to Pro Tools</string>
<key>CFBundleDisplayName</key><string>Reaper to Pro Tools</string>
<key>CFBundleIdentifier</key><string>local.pavel.reaper-to-protools</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>CFBundleExecutable</key><string>ReaperToProTools</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>NSPrincipalClass</key><string>NSApplication</string>
<key>CFBundleShortVersionString</key><string>$VERSION</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

# ad-hoc подпись всего пакета: сначала убираем расширенные атрибуты (иначе codesign отказывается), затем проверяем.
# Без корректной подписи скачанная копия не открывается («приложение повреждено»).
xattr -cr "$APP"
codesign --force --deep --sign - "$APP"
codesign -v --deep --strict "$APP"
echo "$APP"
