#!/usr/bin/env python3
"""reaper_fx.py — цепочки FX Reaper (<FXCHAIN>) по списку плагинов Pro Tools.

Плагин PT сопоставляется с установленным VST3/VST2 по имени (кэш Reaper: reaper-vstplugins_arm64.ini в
~/Library/Application Support/REAPER); настройки плагина не переносятся — Reaper загружает его с параметрами по умолчанию.
"""
import os
import re
import uuid

try:
    import fx_state as _fx_state
except ImportError:
    _fx_state = None

SUPPORT = os.path.expanduser('~/Library/Application Support/REAPER')
PLUGIN_DIRS = ['/Library/Audio/Plug-Ins/VST3', '/Library/Audio/Plug-Ins/VST', os.path.expanduser('~/Library/Audio/Plug-Ins/VST3'),
               os.path.expanduser('~/Library/Audio/Plug-Ins/VST')]


# Reaper не создаёт плагин без настоящего блока состояния (пустой/нулевой блок: эффект «недоступен» или не загружается),
# поэтому цепочка FX пишется только для плагинов, чей блок состояния по умолчанию взят из проекта, сохранённого самим Reaper
# (reaper_fx_library.py: {id плагина: [строки блока <VST ...>]}, см. make_fx_library.py). Остальные плагины — только в заметках.
try:
    from reaper_fx_library import LIBRARY
except ImportError:
    LIBRARY = {}
if os.environ.get('PX_NO_FX_LIBRARY'):                           # проверка: работа без библиотек (все блоки снимаются у Reaper)
    LIBRARY = {}


def _san(s):
    return re.sub(r'[^A-Za-z0-9]', '_', s)


def _norm(s):
    s = re.sub(r'\s*\((?:mono|stereo|[^()]*)\)\s*$', '', s, flags=re.I) if s.lower().rstrip().endswith(')') else s
    return re.sub(r'[^a-z0-9]', '', s.lower())


def load_installed(support=SUPPORT):
    """[{'kind': 'VST3'|'VST', 'file', 'id', 'display', 'norm', 'mono'}] — из кэша плагинов Reaper."""
    ini = os.path.join(support, 'reaper-vstplugins_arm64.ini')
    if not os.path.exists(ini):
        return []
    files = {}
    for d in PLUGIN_DIRS:
        if os.path.isdir(d):
            for f in os.listdir(d):
                files.setdefault(_san(f), f)
    out = []
    for line in open(ini, encoding='utf-8', errors='replace'):
        if '=' not in line or line.startswith('['):
            continue
        key, val = line.rstrip('\n').split('=', 1)
        parts = val.split(',', 2)
        if len(parts) < 3 or '<SHELL>' in val:
            continue
        key = key.split('<')[0]
        ext = os.path.splitext(key)[1].lower()
        if ext not in ('.vst', '.vst3'):
            continue
        pid, display = parts[1], parts[2]
        if ext == '.vst3' and '{' in pid and not pid.endswith('}'):
            pid += '}'
        if pid == '0' or not pid:
            continue
        fname = files.get(_san(key))
        if fname is None:
            continue
        # дисплейное имя «Pro-Q 3 (FabFilter)» -> без производителя
        mono = bool(re.search(r'\(mono\)', display, re.I))
        base = re.sub(r'\s*\((?:mono|stereo|\d+ch)\)', '', display, flags=re.I)
        base = re.sub(r'\s*\([^()]*\)\s*$', '', base)                 # производитель в конце
        base = re.sub(r'\s*\([^()]*\)\s*$', '', base) if re.search(r'\(([^()]*)\)\s*\(', display) else base
        out.append({'kind': 'VST3' if ext == '.vst3' else 'VST', 'file': fname, 'id': pid, 'display': display,
                    'norm': re.sub(r'[^a-z0-9]', '', base.lower()), 'mono': mono})
    return out


_MODINFO = None


def vst3_moduleinfo():
    """{нормализованное имя класса: (бандл, CID)} из Contents/Resources/moduleinfo.json установленных VST3 (VST3 SDK 3.7+), без загрузки плагинов."""
    global _MODINFO
    if _MODINFO is None:
        import glob
        import json
        _MODINFO = {}
        for d in ('/Library/Audio/Plug-Ins/VST3', os.path.expanduser('~/Library/Audio/Plug-Ins/VST3')):
            for b in glob.glob(os.path.join(d, '*.vst3')) + glob.glob(os.path.join(d, '*', '*.vst3')):
                try:
                    txt = open(os.path.join(b, 'Contents', 'Resources', 'moduleinfo.json'), encoding='utf-8').read()
                    info = json.loads(re.sub(r',(\s*[}\]])', r'\1', txt))              # у Steinberg запятые перед } и ] — JSON5
                except (OSError, ValueError):
                    continue
                for c in info.get('Classes') or ():
                    if c.get('Category') == 'Audio Module Class' and c.get('Name') and c.get('CID'):
                        _MODINFO.setdefault(re.sub(r'[^a-z0-9]', '', c['Name'].lower()), (os.path.basename(b), c['CID'].upper()))
    return _MODINFO


def unscanned(names, installed):
    """Имена из names, которые установлены как VST3 (по moduleinfo.json), но которых нет в кэше плагинов Reaper."""
    ids = {e['id'].upper() for e in installed}
    out = []
    for nm in names:
        n = re.sub(r'[^a-z0-9]', '', nm.lower())
        hit = next((v for k, v in vst3_moduleinfo().items() if k == n or n.endswith(k)), None)
        if hit and not any(hit[1] in i for i in ids):
            out.append(nm)
    return out


def find(installed, pt_name, fmt=None):
    """Установленный плагин для имени из Pro Tools (VST3 предпочтительнее VST2; без «Mono» в имени)."""
    n = re.sub(r'[^a-z0-9]', '', pt_name.lower())
    cand = [e for e in installed if e['norm'] and (e['norm'] == n or n.endswith(e['norm']) or e['norm'].endswith(n))]
    cand.sort(key=lambda e: (e['kind'] != 'VST3', e['mono'], 'backwards' in e['display'].lower(), e['norm'] != n, -len(e['norm'])))   # при равном типе точное имя раньше длинного (Radiator, не Little Radiator)
    return cand[0] if cand else None


USER_DIR = os.path.expanduser('~/Library/Application Support/Reaper to Pro Tools')
KIND_RANK = {'VST3': 0, 'VST3i': 0, 'AU': 1, 'AUi': 1, 'VST': 2, 'VSTi': 2, 'CLAP': 3, 'CLAPi': 3}


def load_user_library(path=None):
    """Личная библиотека + дополнительная (переменная окружения PX_FX_LIBRARY: блоки, снятые расширением Reaper у самого Reaper на лету)."""
    out = [] if os.environ.get('PX_NO_FX_LIBRARY') else _load_library_file(path or os.path.join(USER_DIR, 'fx_library.txt'))
    extra = os.environ.get('PX_FX_LIBRARY')
    if extra and os.path.isfile(extra):
        out = _load_library_file(extra) + out                  # свежие блоки впереди
    return out


def _load_library_file(path):
    """Личная библиотека блоков FX, снятых самим Reaper скриптом reaper_scripts/export_fx_library.lua:
    [{'name': 'VST3: Pro-Q 3 (FabFilter)', 'kind', 'norm', 'mono', 'lines': [...]}]."""
    out, cur = [], None
    if not os.path.exists(path):
        return out
    for line in open(path, encoding='utf-8', errors='replace').read().splitlines():
        if line.startswith('#PLUGIN\t'):
            cur = {'name': line.split('\t', 1)[1], 'lines': []}
        elif line == '#END' and cur is not None:
            kind = cur['name'].split(':', 1)[0]
            disp = cur['name'].split(':', 1)[-1].strip()
            low = disp.lower()
            mono = bool(re.search(r'\bmono\b', low)) and not re.search(r'mono/stereo', low)
            stereo = bool(re.search(r'\bstereo\b', low)) and not re.search(r'mono/stereo', low)
            base = disp
            while True:                                           # убрать хвостовые «(производитель)», «(x64)», «(12ch)» ...
                nb = re.sub(r'\s*\([^()]*\)\s*$', '', base)
                if nb == base:
                    break
                base = nb
            base = re.sub(r'\s+(mono/stereo|mono|stereo)$', '', base, flags=re.I)   # варианты Waves «... Mono», «... Stereo»
            cur.update(kind=kind, mono=mono, stereo=stereo, norm=re.sub(r'[^a-z0-9]', '', base.lower()))
            out.append(cur)
            cur = None
        elif cur is not None:
            cur['lines'].append(line)
    return out


def find_user(entries, pt_name, fmt=None):
    n = re.sub(r'[^a-z0-9]', '', pt_name.lower())
    cand = [e for e in entries if e['norm'] and (e['norm'] == n or n.endswith(e['norm']) or e['norm'].endswith(n))]

    def fmt_penalty(e):                                          # у Waves отдельные записи Mono / Stereo — берём подходящую формату
        if fmt == 'mono' and e['stereo'] or fmt in ('stereo', '5.1') and e['mono']:
            return 1
        return 0
    cand.sort(key=lambda e: (fmt_penalty(e), KIND_RANK.get(e['kind'], 9), e['mono'] and fmt != 'mono', 'backwards' in e['name'].lower(),
                             e['norm'] != n, -len(e['norm'])))           # при равном типе точное имя раньше длинного (Radiator, не Little Radiator)
    return cand[0] if cand else None


def note_wanted(names):
    """Имена плагинов, для которых нет блока: скрипт экспорта в Reaper возьмёт именно их."""
    if not names:
        return
    try:
        os.makedirs(USER_DIR, exist_ok=True)
        path = os.path.join(USER_DIR, 'wanted.txt')
        have = set(open(path, encoding='utf-8').read().splitlines()) if os.path.exists(path) else set()
        new = [n for n in dict.fromkeys(names) if n not in have]
        if new:
            with open(path, 'a', encoding='utf-8') as f:
                f.write('\n'.join(new) + '\n')
    except OSError:
        pass


_AU_CACHE = {}


def _macho_archs(path):
    """Архитектуры исполняемого файла (Mach-O / fat): подмножество {'arm64', 'x86_64'}."""
    import struct
    try:
        with open(path, 'rb') as f:
            head = f.read(4096)
    except OSError:
        return set()
    cpus = {0x0100000C: 'arm64', 0x01000007: 'x86_64'}
    if head[:4] in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):
        n = struct.unpack('>I', head[4:8])[0]
        step = 20 if head[:4] == b'\xca\xfe\xba\xbe' else 32
        return {cpus.get(struct.unpack('>I', head[8 + step * i:12 + step * i])[0]) for i in range(min(n, 8))} - {None}
    if head[:4] in (b'\xcf\xfa\xed\xfe',):
        return {cpus.get(struct.unpack('<I', head[4:8])[0])} - {None}
    return set()


def _auval_sig():
    dirs = ['/Library/Audio/Plug-Ins/Components', os.path.expanduser('~/Library/Audio/Plug-Ins/Components')]
    sig = []
    for d in dirs:
        try:
            sig.append([d, os.stat(d).st_mtime, len(os.listdir(d))])
        except OSError:
            sig.append([d, 0, 0])
    return sig


_AUVAL_CACHE = os.path.expanduser('~/Library/Application Support/Reaper to Pro Tools/auval_cache.json')


def _auval_refresh(timeout=300):
    """Запускает `auval -a` (от 7 с до полутора минут) и пишет кэш; при неудаче возвращает None."""
    import json
    import subprocess
    sig = _auval_sig()
    try:
        r = subprocess.run(['/usr/bin/auval', '-a'], capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    items = []
    for line in r.stdout.splitlines():
        m = re.match(r'^(aufx|aumf) (\S{4}) (\S{4})\s+-\s+(.+)$', line)
        if m:
            items.append((m.group(1), m.group(2), m.group(3), re.sub(r'SDPA:.*$', '', m.group(4)).strip()))
    try:
        os.makedirs(os.path.dirname(_AUVAL_CACHE), exist_ok=True)
        with open(_AUVAL_CACHE + '.tmp', 'w') as f:
            json.dump({'sig': sig, 'items': items}, f)
        os.replace(_AUVAL_CACHE + '.tmp', _AUVAL_CACHE)
    except Exception:
        pass
    return items


def _auval_list():
    """Список Audio Units из `auval -a`, кэшируется на диске. Если папки Components изменились, отдаётся старый кэш,
    а `auval` пересобирает его в фоне: чтение сессии не ждёт сканирования (оно бывает дольше минуты)."""
    import json
    import subprocess
    import sys
    import time
    try:
        c = json.load(open(_AUVAL_CACHE))
    except Exception:
        c = None
    if c is None:
        return _auval_refresh() or []                      # первый запуск: кэша нет, ждём
    if c.get('sig') != _auval_sig():
        lock = _AUVAL_CACHE + '.lock'
        try:
            fresh = time.time() - os.stat(lock).st_mtime < 600
        except OSError:
            fresh = False
        if not fresh:                                      # одно фоновое обновление за раз
            try:
                open(lock, 'w').close()
                here = os.path.dirname(os.path.abspath(__file__))
                subprocess.Popen([sys.executable, '-c', 'import os,reaper_fx as r; r._auval_refresh(); os.remove(r._AUVAL_CACHE + ".lock")'],
                                 cwd=here, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    return [tuple(x) for x in c.get('items', [])]


def au_registry(with_auval=False):
    """Установленные Audio Units: из пакетов .component (Info.plist) и из утилиты auval. Записи x86_64-only получают суффикс
    «(x64)» в названии, как их показывает Reaper на Apple Silicon."""
    if with_auval in _AU_CACHE:
        return _AU_CACHE[with_auval]
    import glob
    import plistlib
    seen, out = set(), []

    def add(typ, sub, manu, full, x64=False):
        key = (typ, sub, manu)
        if key in seen:
            return
        seen.add(key)
        vendor, _, product = full.partition(': ')
        if not product:
            vendor, product = '', full
        if x64 and '(x64)' not in product:
            product += ' (x64)'
        base = product
        while True:
            nb = re.sub(r'\s*\([^()]*\)\s*$', '', base)
            if nb == base:
                break
            base = nb
        out.append({'type': typ, 'sub': sub, 'manu': manu, 'vendor': vendor, 'product': product,
                    'norm': re.sub(r'[^a-z0-9]', '', base.lower())})
    for root in ('/Library/Audio/Plug-Ins/Components', os.path.expanduser('~/Library/Audio/Plug-Ins/Components')):
        for bundle in glob.glob(os.path.join(root, '*.component')):
            try:
                plist = plistlib.load(open(os.path.join(bundle, 'Contents', 'Info.plist'), 'rb'))
                exe = os.path.join(bundle, 'Contents', 'MacOS', plist.get('CFBundleExecutable', ''))
                archs = _macho_archs(exe)
                for c in plist.get('AudioComponents', []):
                    if c.get('type') in ('aufx', 'aumf') and len(c.get('subtype', '')) == 4 and len(c.get('manufacturer', '')) == 4:
                        add(c['type'], c['subtype'], c['manufacturer'], c.get('name', os.path.basename(bundle)[:-10]),
                            x64=bool(archs) and 'arm64' not in archs)
            except Exception:
                continue
    if with_auval:
        for typ, sub, manu, full in _auval_list():
            add(typ, sub, manu, full)
    _AU_CACHE[with_auval] = out
    return out


def _fourcc_int(s):
    import struct
    return struct.unpack('>I', s.encode('latin1')[:4].ljust(4, b' '))[0]


def _au_find(pt_name, registry):
    n = re.sub(r'[^a-z0-9]', '', pt_name.lower())
    best = None
    for e in registry:
        b = e['norm']
        if not b or not n:
            continue
        if n == b or (len(b) >= 5 and n.endswith(b)) or (len(n) >= 5 and b.endswith(n)):
            if best is None or len(b) > len(best['norm']):
                best = e
    return best


def au_block(pt_name):
    """Блок <AU ...> для плагина Pro Tools по имени, если такой Audio Unit установлен. У AU по умолчанию нет блока состояния:
    Reaper загружает его с параметрами по умолчанию, поэтому блок собирается на лету, без библиотеки. Сначала быстрый список из
    пакетов .component; медленный auval — только если там не нашлось."""
    best = _au_find(pt_name, au_registry()) or _au_find(pt_name, au_registry(with_auval=True))
    if best is None:
        return None
    disp = f"AU: {best['product']} ({best['vendor']})" if best['vendor'] else f"AU: {best['product']}"
    return [f'<AU "{disp}" "" "" {_fourcc_int(best["type"])} {_fourcc_int(best["sub"])} {_fourcc_int(best["manu"])}', '>']


def parmenv_lines(cv, plugin_name, rate, block=None):
    """Огибающая параметра плагина Reaper (<PARMENV>) из кривой Pro Tools; точки: (время c, значение 0..1).
    Индекс параметра берётся по ИМЕНИ из настоящего плагина VST3 (хост vst3host): номер параметра в Pro Tools с индексом Reaper не совпадает.
    Если хоста/плагина нет — старое правило (номер PT - 1), оно верно не для всех плагинов; если плагин есть, а параметра с таким именем нет — кривая пропускается."""
    idx, steps = max(0, cv['index'] - 1), 0
    if _fx_state is not None and block is not None:
        q, host_ok = _fx_state.param_for(block, cv['name'], cv.get('aid'))
        if host_ok:
            if q is None:
                return []
            idx, steps = q['index'], q['steps']
    pts = [(0.0, cv['initial'])] + [(t / rate, v) for t, v in cv['points']]
    shape = 1 if cv.get('discrete') else 0
    if cv.get('discrete') and steps > 0:                                   # номера состояний -> нормализованное значение 0..1
        pts = [(t, v / steps) for t, v in pts]
    out = [f'<PARMENV {idx}:{idx} 0 1 0.5 "{cv["name"]} / {plugin_name}"', f'EGUID {{{str(uuid.uuid4()).upper()}}}', 'ACT 1 -1', 'VIS 1 1 1',
           'LANEHEIGHT 0 0', 'ARM 1', f'DEFSHAPE {shape} -1 -1']
    for t, v in pts:
        out.append(f'PT {t:.9g} {min(1.0, max(0.0, v)) if steps else v:.9g} {shape}')
    return out + ['>']


def harvest_name(pl, installed, ulib):
    """Имя FX Reaper («VST3: Pro-Q 3 (FabFilter)»), блок которого нужно снять у самого Reaper: плагин установлен как VST3, а блока VST3 в библиотеке нет."""
    u = find_user(ulib, pl['name'], pl.get('format'))
    if u is not None and u['kind'] == 'VST3':
        return None
    e = find(installed, pl['name'], pl.get('format'))
    if e is None or e['kind'] != 'VST3':
        return None
    if LIBRARY.get(e['id'].rstrip('}')) is not None:
        return None
    return f"VST3: {e['display']}"


def _waves_variant(pl, ulib):
    """Waves: вариант (Mono / Stereo / Mono/Stereo) по SubComp из состояния PT — у них одно имя, а XML пресета подходит только своему варианту."""
    import fx_state
    st = pl.get('state')
    ax = fx_state.waves_aax_xml(st) if st else None
    sub = fx_state.waves_sub(ax[0]) if ax else None
    if not sub:
        return None
    n = re.sub(r'[^a-z0-9]', '', pl['name'].lower())
    for e in ulib:
        if e['kind'] == 'VST3' and e['norm'].startswith(n):
            try:
                head, raw, tail = fx_state._decode(list(e['lines']))
                sp = fx_state._split(raw)
            except Exception:
                continue
            if sp and raw[sp[0] + 20 + 12:sp[0] + 20 + 16] == sub:
                return e
    return None


def block_for(pl, installed, ulib):
    """Блок <VST ...> библиотеки для плагина PT (личная библиотека, затем встроенная, затем AU) или None."""
    u = _waves_variant(pl, ulib) or find_user(ulib, pl['name'], pl.get('format'))
    if u is not None:
        return u['lines']
    e = find(installed, pl['name'], pl.get('format'))
    block = LIBRARY.get(e['id'].rstrip('}')) if e is not None else None
    return block if block is not None else au_block(pl['name'])


def chain_lines(plugins, installed, rate=48000, plugin_settings=True, requests=None):
    """Строки <FXCHAIN> для дорожки; plugins — как из ptx_plugins.read_plugins. Возвращает (строки, не найденные имена)."""
    lines, missing = [], []
    body = []
    ulib = load_user_library()
    n_fx = 0
    for pl in plugins:
        block = block_for(pl, installed, ulib)
        if block is None:
            missing.append(pl['name'])
            continue
        applied0 = _fx_state.STATS['applied'] if _fx_state else 0
        block = _fx_state.apply_state(list(block), pl) if _fx_state and plugin_settings else block
        if requests is not None and _fx_state and plugin_settings and _fx_state.STATS['applied'] == applied0:
            params = _fx_state.build_requests(pl, block)            # состояние перенести не удалось — параметры выставит по именам сам Reaper
            if params:
                requests.append({'fx': n_fx, 'name': pl['name'], 'params': params})
        n_fx += 1
        body += [f'BYPASS {1 if pl["bypass"] else 0} 0 0'] + list(block) + ['FLOATPOS 0 0 0 0', f'FXID {{{str(uuid.uuid4()).upper()}}}']
        for cv in pl.get('curves') or ():                      # автоматизация параметра PT -> PARMENV (индекс Reaper = номер параметра PT - 1)
            if cv.get('points') and all(v == cv.get('initial') for _, v in cv['points']):
                continue                                       # константная кривая (все точки равны начальному значению) ничего не меняет — не пишем
            body += parmenv_lines(cv, pl['name'], rate, block)
        body += ['WAK 0 0']
    if body:
        lines = ['<FXCHAIN', 'SHOW 0', 'LASTSEL 0', 'DOCKED 0'] + body + ['>']
    return lines, missing


def prepare():
    """Готовит скрипт экспорта и список нужных плагинов (wanted.txt) в личной папке; возвращает путь к скрипту."""
    import shutil
    os.makedirs(USER_DIR, exist_ok=True)
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reaper_scripts', 'export_fx_library.lua')
    dst = os.path.join(USER_DIR, 'export_fx_library.lua')
    shutil.copyfile(src, dst)
    try:
        import ptx_templates_plugins as T
        note_wanted(sorted({v['name'] for v in T.SLOTS.values()}))
    except ImportError:
        pass
    return dst


def library_count():
    path = os.path.join(USER_DIR, 'fx_library.txt')
    if not os.path.exists(path):
        return 0
    return sum(1 for ln in open(path, encoding='utf-8', errors='replace') if ln.startswith('#PLUGIN\t'))


if __name__ == '__main__':
    import sys
    if '--prepare' in sys.argv:
        print(prepare())
    elif '--count' in sys.argv:
        print(library_count())
