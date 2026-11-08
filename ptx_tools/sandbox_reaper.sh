#!/bin/bash
# sandbox_reaper.sh <ptx> <out.rpp> [mode]: импорт .ptx через расширение в ОТДЕЛЬНОМ экземпляре Reaper (-newinst, свой конфиг в /tmp/mt/rcfg),
# сохраняет проект в out.rpp. Пользовательский Reaper не трогаем. Лог: <out>.log. Нужны: приложение в /Applications, /tmp/mt/rcfg (создаётся здесь).
PTX="$1"; OUT="$2"; MODE="${3:-link}"
RC=/tmp/mt/rcfg; mkdir -p $RC/UserPlugins
SUP="$HOME/Library/Application Support/REAPER"
[ -f $RC/reaper.ini ] || { printf '[REAPER]\n' > $RC/reaper.ini; for f in reaper-vstplugins_arm64.ini reaper-vstshells_arm64.ini reaper-auplugins_arm64.ini reaper-auplugins_arm64-bc.ini reaper-clap-macos-aarch64.ini reaper-fxtags.ini; do cp "$SUP/$f" $RC/ 2>/dev/null; done; }
APP="${APP:-/Applications/Reaper to Pro Tools.app}"
cp "$APP/Contents/Resources/ReaperExtension/reaper_pt_session_import.dylib" $RC/UserPlugins/
rm -f "$OUT" "$OUT.log"
(REAPER_PTX_ENGINE="$APP/Contents/Resources/ptx_tools" REAPER_PTX_TEST_PTX="$PTX" REAPER_PTX_TEST_LOG="$OUT.log" REAPER_PTX_TEST_MODE=$MODE REAPER_PTX_TEST_AUTOIMPORT=1 REAPER_PTX_TEST_SAVE="$OUT" nohup /Applications/REAPER.app/Contents/MacOS/REAPER -newinst -nosplash -noactivate -cfgfile $RC/reaper.ini >/dev/null 2>&1 &)
for i in $(seq 1 ${TIMEOUT:-240}); do sleep 1; grep -q "^IMPORTED" "$OUT.log" 2>/dev/null && [ -s "$OUT" ] && break; done
sleep 3; pkill -f "cfgfile $RC"; ls -la "$OUT"; grep -E "TIMING|IMPORTED" "$OUT.log" | cut -c1-140
