#!/bin/sh
# Выпуск новой версии: ./release.sh <версия> "<заметки>"
# Собирает приложение, пакует, подписывает манифест и выкладывает zip + latest.json релизом на GitHub (и на Яндекс.Диск на переходный период).
# Нужны: gh (gh auth login), ключ подписи; репозиторий — из update_url.txt (https://github.com/<владелец>/<репо>).
set -e
VER="$1"; NOTES="$2"
[ -n "$VER" ] || { echo "usage: release.sh <version> [notes]"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
GUI="$HERE/.."
KEY="$HOME/Library/Application Support/Reaper to Pro Tools Release/signing.key"
[ -f "$KEY" ] || { echo "no signing key at $KEY"; exit 1; }
[ -x "$HERE/sign" ] || swiftc -O "$HERE/sign.swift" -o "$HERE/sign"

# версия попадает в Info.plist через переменную окружения APP_VERSION
APP_VERSION="$VER" "$GUI/build_app.sh" "$HERE/out" >/dev/null
APP="$HERE/out/Reaper to Pro Tools.app"
FILE="ReaperToProTools-$VER.zip"
rm -f "$HERE/out/$FILE"
ditto -c -k --keepParent "$APP" "$HERE/out/$FILE"
SHA="$(shasum -a 256 "$HERE/out/$FILE" | cut -d' ' -f1)"
SIZE="$(stat -f %z "$HERE/out/$FILE")"
SIG="$("$HERE/sign" sign "$KEY" "$VER
$FILE
$SHA
$SIZE")"
python3 - "$VER" "$FILE" "$SHA" "$SIZE" "$NOTES" "$SIG" "$HERE/out/latest.json" <<'PY'
import json, sys
v, f, sha, size, notes, sig, out = sys.argv[1:]
json.dump({"version": v, "file": f, "sha256": sha, "size": int(size), "notes": notes, "signature": sig}, open(out, "w"), ensure_ascii=False, indent=1)
PY
# GitHub: релиз v<версия> с архивом и подписанным latest.json; приложение берёт …/releases/latest/download/latest.json
REPO="$(tr -d '\n ' < "$HERE/update_url.txt" | sed 's#^https://github.com/##')"
gh release create "v$VER" "$HERE/out/$FILE" "$HERE/out/latest.json" --repo "$REPO" --title "$VER" --notes "$NOTES"
# Переходный период: копии до 1.2.46 проверяют Яндекс.Диск — кладём туда же, пока все машины не обновятся (YADISK=0 — пропустить)
if [ "${YADISK:-1}" != 0 ]; then
    python3 "$HERE/yadisk.py" upload "$HERE/out/$FILE" "/ReaperToProTools-updates/$FILE"
    python3 "$HERE/yadisk.py" upload "$HERE/out/latest.json" "/ReaperToProTools-updates/latest.json"
fi
python3 - "$VER" "$NOTES" "$GUI/../CHANGELOG.md" <<'PY'
import datetime, re, sys
v, notes, path = sys.argv[1:]
text = open(path, encoding='utf-8').read()
if f'## {v} ' not in text:                                   # повторный выпуск той же версии не дублирует запись
    items = '\n'.join('- ' + re.sub(r'^[•\-]\s*', '', l).strip() for l in notes.splitlines() if l.strip())
    i = text.find('\n## ') + 1 or len(text)
    open(path, 'w', encoding='utf-8').write(text[:i] + f'## {v} — {datetime.date.today()}\n{items}\n\n' + text[i:])
PY
echo "released $VER"
