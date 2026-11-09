# Памятка по проекту Reaper ⇄ Pro Tools (для Claude)

Конвертер .rpp ⇄ .ptx без запуска Pro Tools. Движок — Python 3 (`/usr/bin/python3`, только stdlib), GUI — SwiftUI,
расширение Reaper — Obj-C. Пользователь говорит по-русски. Комментарии и докстринги в коде — по-русски.
Здесь — карта кода. Остальная документация: `README.md` (обзор, сборка, внешние зависимости), `docs/FEATURES.md` (что переносится),
`CHANGELOG.md`, `docs/ROADMAP.md`, `docs/research/*.md` (журнал исследований: формат .ptx, плагины, PTSL — только локально, в репозиторий не входит).

**Вести документацию при каждом изменении:** новый/переименованный модуль или главная функция → этот файл; новая или изменённая возможность,
ограничение → `docs/FEATURES.md`; новый проверенный факт о формате → `docs/research/` (и заметка в памяти); задача сделана или отложена →
`docs/ROADMAP.md`; выпуск → `CHANGELOG.md` пишет `release.sh` сам из заметок выпуска.

## Раскладка

| Путь | Что |
|---|---|
| `ptx_tools/` | движок; всё плоско, модули импортируют друг друга по имени (cwd/`sys.path` = эта папка) |
| `ptx_tools/templates/pool_N.ptx` | пустые сессии PT 26.4 с N моно-дорожками (`pool_m1`, `pool_s1` — моно/стерео 1) — база для Reaper→PT |
| `ptx_tools/ptx_templates*.py` | сгенерированные hex-образцы блоков (не править руками; генераторы `make_*.py`) |
| `ptx_tools/reaper_scripts/export_fx_library.lua` | снимает блоки FX у самого Reaper → `fx_library.txt` |
| `reaper_ext/reaper_pt_session_import.m` | расширение Reaper «Import Pro Tools session» (`build.sh` → `out/*.dylib`) |
| `gui/ReaperToProTools/App.swift` | приложение (оба направления), `Updater.swift` — автообновление |
| `gui/build_app.sh` | сборка .app; **список копируемых .py — явный**: новый модуль добавить туда |
| `gui/release/release.sh <ver> "<notes>"` | выпуск на Яндекс.Диск (подписанный latest.json) |
| `vst3host/main.cpp` | нативный хост VST3: `params`, `batch` (состояния компонента и контроллера), `domain [--grid N] [id…]` (шкала: подпись плагина в N точках), `buses` (форматы каналов); сборка `cmake -DVST3_SDK_ROOT=…` → `vst3host/build/vst3host` |

## Поток PT → Reaper (`ptx_to_reaper.py`)

`convert(ptx, out, ..., info=False, off=(), on=(), only=None, offset, param_requests_file)` — одна функция на всё:
1. `PTX(raw)` (ptx_model) → `session_rate`, `Catalog` (файлы 0x1004>0x103a), `parse_regions` (0x2629), `parse_gains`/`parse_gain_envs` (0x2637),
   `parse_geometries` (фейды 0x2630), `parse_tracks`, `parse_playlists`/`_playlist_events` (0x1052), `parse_groups`+`parse_group_playlists` (клип-группы),
   `parse_memory_locations` (маркеры 0x2077, цвет `marker_rgb`, комментарий `marker_comment`), `parse_video`, `read_vca`, `parse_mix_groups`.
2. `resolve_file` (+ `_name_index`, relink JSON) ищет аудио; `build_clips` → `Clip`/`EmptyItem`.
3. `apply_plugins` → `reaper_fx.chain_lines` (+ `fx_state.apply_state` для настроек); `apply_routing` → sends/автоматизация (`ptx_sends.read_routing`).
4. `--info` печатает одну JSON-строку (счётчики, `track_list`, `missing_detail`, `fx_wanted`) и выходит; хост VST3 в этом режиме выключен (`fx_state.HOST_ENABLED=False`).
5. `write_rpp(...)` пишет .rpp (TEMPO из `ptx_tempo.read_bpm`, таймкод `ptx_settings.read_timecode`).
- `--candidates --search DIR` — поиск пропавших файлов (`find_candidates`). `--param-requests F` — параметры для выставления самим Reaper (расширение).
- `off`: clips, clip_mute, clip_colors, track_colors, notes, solo, mute, auto_*, send_auto_*, plugin_settings; `on`: skip_hidden, skip_inactive, trim_overlap.

## Поток Reaper → PT (`reaper_to_ptx.py`)

`convert(rpp, out_dir, name, templates_dir, ..., info_only, placeholders, item_groups, plugin_settings, ...)`:
1. `rpp.load` → `Project` (Track/Item/Marker/Fx, `tempo_points`, `bpm`), `FileResolver`.
2. `analyse` (что переносится), `make_info` (заголовки wav/flac/mp3, дисковый кэш), FLAC/MP3/чужая частота → `flac_to_wav`/afconvert.
3. `plan_sessions`/`plan_parts` — делит на несколько сессий, если дорожек больше, чем в самом большом pool; `plan_layout`, `plan_empty_tracks`, `plan_sends`.
4. `pick_template` → `ptx_build.SessionBuilder` (`add`, `add_marker`, `set_track_state/comment/auto`, `clip_groups`, `empty_groups`, `bpm`, `build`).
   Дальше модули дописывают сессию: `ptx_structure.insert_track` (папки/aux/routing), `ptx_sends.add_bus/add_send/set_main_output`,
   `ptx_plugins.add_plugin`+`apply_reaper_state`, `param_env_apply`, `ptx_video.add_video`, `ptx_settings.*`, `ptx_groups.apply_groups`, `ptx_tempo.write_bpm`.
5. `volume_for` (том сессии, `_is_network_volume`, кэш `volumes.json`). `missing_plugins_comment` — что не перенеслось (в комментарий дорожки).

## Ядро формата

- `ptx.py` — XOR-контейнер (`crypt`, `xor_key`), старый парсер `parse_tree`/`dump` (CLI-дамп).
- `ptx_model.py` — **основная модель**: `PTX(raw)`, `Block` (`ctype`, `items` = bytes|Block, `size/total`), `PTX.tokenize()` таблицы 0x0002
  (токены `b`/`rec`/`ptr`), `save()` пересчитывает смещения. `roundtrip(path)` — проверка.
- `ptx_trackstate.py` — общие хелперы `own_bytes/own_patch/own_splice`, `kids/root/descend`, `track_blocks`, `aggregate_names` (0x251a),
  mute/solo/цвет/inactive/hidden/frozen (`read_states`, `write_state`, `write_inactive`), фейдер (`read/write_fader`), комментарий (0x2015).
- `ptx_structure.py` — серии таблицы (`_find_tok`, `_set_series`, `_be16_bump`), `insert_track`, `set_membership` (0x210c), `rebuild_strips` (0x2589), `reindex_playlists`.
- `ptx_build.py` — клоны блоков (`clone`, `patch`, `bump`), `make_media` (0x1003), `make_region` (0x2629), `make_event` (0x1050), `make_marker` (MARKER264),
  `gain_env_record`, `make_catalog_groups`, `SessionBuilder`, `build_session`.
- `ptx_elastic.py` — Elastic Audio, `_splice` по сырым сегментам. `ptx_tracks.py` — переименование дорожек (7 мест). `ptx_diff.py` — дерево-дифф двух .ptx.
- `ptx_settings.py` — fps/старт TC (0x204d), счётчики, частота сессии, `read_app_info`. `ptx_tempo.py` — только постоянный темп (оба 0x2028).
- `ptx_groups.py` — клип-группы (0x262c/0x262b, 0x2424/0x2423, 0x2426/0x2425, 0x2428), многоканальные: определение на канал (`CHAN_SUFFIX`).
- `ptx_video.py` — `mov_info`, `add_video`, `read_back`. `ptx_sends.py` — шины, sends, выходы, автоматизация 0x260a.

## Плагины

- `ptx_plugins.py` — чтение (`read_catalog` 0x1017/0x1018, `read_plugins`, `read_param_list`, `_read_curves`), запись (`add_plugin`, `_ensure_catalog`,
  `set_param_curve(…, vst3_id)` — параметр ищется по ID (`_entry_aid`, `_entry_is`), затем по имени), `_plugin_block` разворачивает обёртку 0x2617. Нет параметра в списке слота — `_ensure_param_entry` берёт запись из
  `ptx_templates_params.PARAMS` (генератор `make_param_lists.py <сессии|список.txt>`, ключ — тройка FourCC); `trim_param_lists(p)` в конце
  Reaper → PT оставляет в списках только автоматизированные параметры (PT ставит значения списка поверх состояния). `find_template(name, fmt, placeholder)` = SLOTS + SLOTS2 + личная библиотека,
  иначе (после отбрасывания хвоста варианта «Mono/Stereo/5.1» из имени) `_synth_template` (через `ptx_slot_gen.synth` по идентичности из `ptx_plugin_ids.IDS` / `plugin_ids.json`), иначе заглушка `RtoP`.
- `ptx_slot_gen.py` — слот «с нуля» по тройке FourCC (PT опознаёт плагин только по ней). `ptx_plugin_scan.py --scan` — page tables бандлов (`DigiPageTable.xml`, `AAXPageTables.xml`, `*.PageTables.xml`, `page_table.xml`…,
  разбор описаний `split_desc`: «(Stereo)», «Mono Native», «2 -> 2 by …», «2 in X 2 out»; только Native, вход = выход) → `plugin_ids.json`;
  `read_page_tables()` — то же на лету, когда кэша PT нет;
  `read_pt_cache()` — все установленные AAX из `/Users/Shared/Pro Tools/AAXPlugInCache/*.plugincache.txt` (StemFormats 0..6, PlugInType 3 = Native), `--selftest`.
  `read_waves()` — плагины Waves из `/Applications/Waves/Plug-Ins V*/<x>.bundle` (PageTable: ID сессии; ProcessXML: имя и SubComp) — их нет в кэше PT.
  Порядок в `_ids_lookup`: IDS < Waves < кэш PT (нет — page tables) < plugin_ids.json.
- `ptx_plugin_learn.py` — личная библиотека шаблонов из сессий пользователя. `make_plugin_ids.py` — dev, нужен `$DIGISHELL`.
- **Reaper→PT настройки**: `ptx_plugin_state.blob_from_reaper(tpl_state, wrapper, vst_file, plugname)` — классы `_ffbs`, `_fabf`, `_xml`, `_vstw_xml`,
  `_embedded`, `_named` (через хост), `_juce` (+ `juce_state`), `_soundtoys` (текст `WIDGET = …`), `_waves` (XML `PresetChunkXMLTree`, по SubComp), `waves_blob` (состояние Waves с нуля для слота без образца). Блок 0x1038: данные с 66, `_resize_header` (LE @6 = len−14).
- **PT→Reaper настройки**: `fx_state.apply_state(block, plugin)` — проход по `TRANSPLANTS` (класс сработавшего — `STATS['last']`): `transplant_ffbs/fabf/xml/vstw_xml/soundtoys/waves/juce/embedded/named`; `rebuild(block, comp, ctrl=)` пишет
  компонент и контроллер (n = cs + ks + 16); `_host_json` — ответы хоста с дисковым кэшем; `aax_id_keys(aid)` (ID AAX -> ID VST3: FourCC-число у Audio Ease, число у FabFilter/Exponential, хэш JUCE для строковых ID),
  `param_for(block, name, aid)` (автоматизация PT -> Reaper: сначала по ID), `vst3_param_id(file, index)` (Reaper -> PT), `buses(bundle)`, `plain_to_norm(bundle, pid, v)` (значение в
  единицах плагина → 0..1 по шкале `domain`, используется в `_setline`); `python3 fx_state.py` — самопроверка `rebuild`; `_params(bundle)` —
  параметры через vst3host с кэшем `~/Library/Caches/Reaper to Pro Tools/vst3_params.json`; `prefetch`/`warm` — пакетно; `juce_id` — ID параметра JUCE.
- `reaper_fx.py` — FX Reaper: `load_installed` (кэш плагинов Reaper), `find`, `block_for` (личная библиотека → встроенная `reaper_fx_library` → AU),
  `au_registry`/`_auval_list` (кэш `auval_cache.json`, обновление в фоне), `_waves_variant` (вариант Waves по SubComp),
  `vst3_moduleinfo`/`unscanned` (VST3 установлен, но Reaper его не сканировал — заметка дорожки), `chain_lines`, `parmenv_lines`, `harvest_name`/`note_wanted`/`prepare` (экспорт у Reaper).
- `rpp.py` — парсер .rpp (`load`, `_parse_file` кэш, `register_virtual`), `Fx` с `PARMENV`, `_vst_state`, `Item.env/group/notes`, `Marker.color`.

## Расширение Reaper (`reaper_pt_session_import.m`)

`EngineDir()` ищет движок: /Applications/…app/Contents/Resources/ptx_tools → ~/Applications/… → `~/Library/Application Support/Reaper to Pro Tools/engine/ptx_tools`.
Окно: `loadInfo` (`ptx_to_reaper.py <ptx> --info`, фаза «Reading…») → `showInfo` → `PXTrackPicker` (выбор дорожек, `--tracks`), `PXRelink`
(`--candidates`, `--relink`) → импорт: `--copy-media-to`, `--offset`, `--param-requests` → `ParseRPP` → `InsertIntoProjectAsync`
(чанки дорожек/items через API, `ApplyParamRequest` выставляет параметры по именам). `HarvestFX` снимает блоки FX, которых нет в библиотеке.
`runEngine:` — NSTask с `/usr/bin/python3`, построчный вывод.

## Приложение (`App.swift`)

`Model` (Reaper→PT, `reaper_to_ptx.py`), `ReverseModel` (PT→Reaper, `ptx_to_reaper.py`); окно в две колонки: слева Input / Preflight / Output,
справа `TracksCard` (выбор дорожек из `track_list` в `--info`: фильтр, меню фильтра, свёртка папок, цвета; `selectedTracks` -> `--tracks`), `RelinkSheet`, `FXLibrarySheet` (`reaper_fx.py`),
`PTLibrarySheet` (`ptx_plugin_learn.py`, кнопка Scan → `ptx_plugin_scan.py --scan`), `ReaperExtension` (установка dylib в UserPlugins), `Router`/`RootView`.
Опции GUI → флаги CLI (`--no-*`, `--off/--on`).

## Данные пользователя

`~/Library/Application Support/Reaper to Pro Tools/`: `fx_library.txt`, `wanted.txt`, `plugin_ids.json`, личные шаблоны PT, `auval_cache.json`,
`volumes.json`, `wavinfo.json`, `engine/` (запасной движок). Кэши: `~/Library/Caches/Reaper to Pro Tools/vst3_params.json`.

## Сборка, установка, проверка

- Сборка: `APP_VERSION=<выше опубликованной> bash gui/build_app.sh` → `gui/build/Reaper to Pro Tools.app`.
- Установка: закрыть приложение, заменить `/Applications/Reaper to Pro Tools.app`, скопировать `Contents/Resources/ReaperExtension/*.dylib`
  в `~/Library/Application Support/REAPER/UserPlugins/`, попросить перезапустить Reaper.
- Код — на GitHub (публичный https://github.com/Qarmaa/ReaperToProTools; автор коммитов — noreply-адрес). Обновления — релизы GitHub
  (`release.sh` -> `gh release create`, приложение читает …/releases/latest/download/latest.json); Яндекс.Диск — только переходный период (YADISK=0 — выключить).
  Перед коммитом: не добавлять названия проектов пользователя, личные пути и разборы сторонних продуктов (docs/research и regress/ — в .gitignore).
- Публикация — только по команде пользователя («опубликуй/выложи»), заметки ≤5 пунктов, ~800 символов.
- Reaper-проверки — только в песочнице `ptx_tools/sandbox_reaper.sh` (`-newinst -cfgfile /tmp/mt/rcfg/reaper.ini`), не в Reaper пользователя.
- Тестовые сессии Reaper → PT из больших проектов собирать с `--link` (без копий аудио): копии 7687 клипов заполнили системный диск,
  и Pro Tools завис на открытии (2026-10-30). Рабочую папку после тестов чистить от `Audio Files`.
- Проверки в живом PT — `ptx_tools/pt_cycle.sh` (Open→SaveAs→Close через `$PTSL_CLI`); сам отказывается, если открыта сессия. PT не перезапускать без разрешения.
- AAX SDK / DigiShell / AAX Developer Tools — конфиденциальны: не копировать в проект и приложение.
- Перед выпуском: `cd ptx_tools && python3 regress.py run ../regress/sessions.txt --out /tmp/new.json && python3 regress.py diff ../regress/baseline.json /tmp/new.json`;
  отличия разобрать, намеренные — обновить базу (`regress/baseline.json`). Самопроверки: `python3 fx_state.py`, `ptx_plugin_state.py`, `ptx_plugin_scan.py --selftest`.
- Профилирование: `python3 -m cProfile -s cumtime ptx_to_reaper.py <ptx> --info` (типично 2–3 с на большой сессии).

## Известные хвосты

См. `docs/ROADMAP.md`.
