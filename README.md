# Reaper to Pro Tools

Конвертер сессий Reaper (.rpp) ⇄ Pro Tools (.ptx) без запуска Pro Tools: приложение, расширение Reaper и движок на Python.

- `ptx_tools/` — движок (`reaper_to_ptx.py`, `ptx_to_reaper.py` и модули), шаблоны сессий `templates/`, генераторы шаблонов `make_*.py`.
- `gui/` — приложение SwiftUI (`build_app.sh`), выпуск обновлений `release/` (`release.sh <версия> "<заметки>"`).
- `reaper_ext/` — расширение Reaper (Obj-C), собирается из `build.sh`, входит в приложение.
- `vst3host/` — хост VST3 для переноса настроек плагинов.

## Документация

- [docs/FEATURES.md](docs/FEATURES.md) — что переносится в обе стороны и ограничения.
- [CHANGELOG.md](CHANGELOG.md) — история версий (пополняется `release.sh`).
- [docs/ROADMAP.md](docs/ROADMAP.md) — открытые задачи и технический долг.
- [CLAUDE.md](CLAUDE.md) — карта кода: модули, функции, потоки, правила сборки и проверки.
- [docs/research/](docs/research/) — проверенные факты о формате .ptx, плагинах, маркерах/клип-группах/видео и особенностях PTSL (журнал исследований).

## Сборка

    APP_VERSION=1.2.38 sh gui/build_app.sh /tmp/out          # приложение (+ расширение)
    cmake -S vst3host -B vst3host/build -DVST3_SDK_ROOT=<путь к vst3sdk> && cmake --build vst3host/build   # до build_app.sh

## Внешние зависимости (не входят в проект, только ссылки)

- VST3 SDK (MIT) — `-DVST3_SDK_ROOT`, для `vst3host`.
- PTSL SDK (Avid) — только для проверок в живом Pro Tools: `ptx_tools/pt_cycle.sh` берёт `ptsl_cli` из `$PTSL_CLI`
  (по умолчанию `~/PTSL_SDK_CPP.2026.04.0.1301892/build/ptsl_cli`).
- AAX Developer Tools / DigiShell (Avid, закрытые) — только для `make_plugin_ids.py`, путь в `$DIGISHELL`. В проект и в приложение не копировать.
- Ключ подписи обновлений — `~/Library/Application Support/Reaper to Pro Tools Release/signing.key`, токен Яндекс.Диска — в Keychain (`yadisk-oauth`).
