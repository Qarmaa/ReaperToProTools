#!/usr/bin/env python3
"""fx_state.py — настройки плагина Pro Tools -> состояние VST3 в чанке Reaper.

Класс B (FabFilter Pro-L 2, Pro-DS): см. transplant_fabf.

Класс A (FabFilter Pro-Q 3/4, Pro-C 3, Saturn 2): блок состояния AAX (0x1038, данные с позиции 66) начинается с `FFBS` и — первые N байт —
совпадает с состоянием компонента VST3 (проверено нативным VST3-хостом vst3host: плагин принимает блок, getState возвращает его же).
N = размер состояния компонента по умолчанию (поле перед `FFBS` в чанке Reaper). Остаток блока PT — состояние контроллера (имя дорожки и т.п.), не нужен.
В чанк Reaper (библиотека reaper_fx_library / fx_library.txt) подставляется только состояние компонента, обёртка и состояние контроллера остаются.
"""
import base64
import struct

DATA_OFF = 66            # 0x1038: 18 байт заголовка + 12 (FourCC производитель/продукт/плагин) + 4 ('elck') + 32 (имя блока)


def _decode(lines):
    """lines — строки блока <VST ...: [заголовок, base64..., '>', ...] -> (заголовок, байты, хвост)."""
    head, out, i = lines[0], b'', 1
    while i < len(lines) and lines[i].strip() != '>':
        s = lines[i].strip()
        out += base64.b64decode(s + '=' * (-len(s) % 4))
        i += 1
    return head, out, list(lines[i:])


def _line_lens(lines):
    out = []
    for l in lines[1:]:
        if l.strip() == '>':
            break
        t = l.strip()
        out.append(len(base64.b64decode(t + '=' * (-len(t) % 4))))
    return out


def _encode(head, raw, tail, lens):
    """Строки пишутся с теми же границами сегментов, что в исходном чанке Reaper (длина состояния не меняется), каждая со своим padding."""
    body, pos = [], 0
    for n in lens:
        body.append(base64.b64encode(raw[pos:pos + n]).decode())
        pos += n
    if pos < len(raw):
        body.append(base64.b64encode(raw[pos:]).decode())
    return [head] + body + tail


def transplant_ffbs(block, state):
    """Подставляет состояние компонента FabFilter из блока PT в блок Reaper. Возвращает новые строки или None (формат не подошёл)."""
    if state is None or len(state) <= DATA_OFF + 12:
        return None
    data = bytes(state[DATA_OFF:])
    if data[:4] != b'FFBS':
        return None
    head, raw, tail = _decode(block)
    i = raw.find(b'FFBS')
    if i < 8:
        return None
    n = struct.unpack_from('<I', raw, i - 8)[0]                 # размер состояния компонента в обёртке Reaper
    if n < 12 or i + n > len(raw) or len(data) < n or raw[i:i + 8] != data[:8]:      # версия формата должна совпадать
        return None
    lib = raw[i:i + n]
    # в блоке PT состояние компонента короче на хвост `FFpr` (его добавляет обёртка VST3), дальше сразу идёт состояние контроллера: <продукт FourCC> 03 00 00 00
    pos = data.find(bytes(state[22:26]) + b'\x03\x00\x00\x00', 12)
    comp = data[:pos] + lib[pos:] if n - 32 <= pos <= n else data[:n]
    return _encode(head, raw[:i] + comp + raw[i + n:], tail, _line_lens(block))


def _pt_entries(data):
    """Стандартный блок AAX (AAX_CChunkDataParser): `01 01 01 01`, затем записи `<тип>_<имя>\0` + значение (big-endian, выравнивание по 4)."""
    if data[:4] != b'\x01\x01\x01\x01':
        return None
    out, j = [], 4
    try:
        while j + 4 <= len(data):
            t = chr(data[j])
            if data[j + 1:j + 2] != b'_' or t not in 'fdls':
                break
            j = data.index(b'\0', j + 2) + 1
            j += (-(j - 4)) % 4
            if t == 'f':
                out.append(struct.unpack_from('>f', data, j)[0]); j += 4
            elif t == 'd':
                out.append(struct.unpack_from('>d', data, j)[0]); j += 8
            elif t == 'l':
                out.append(float(struct.unpack_from('>i', data, j)[0])); j += 4
            else:
                out.append(float(struct.unpack_from('>h', data, j)[0])); j += 4
    except (ValueError, struct.error):
        return None
    return out


def transplant_fabf(block, state):
    """Класс B (FabFilter Pro-L 2, Pro-DS): состояние компонента VST3 = `FabF`, версия, имя пресета (pstr), u32, число параметров, float32 на параметр;
    в блоке AAX (запись `d_`/`l_` без имён) те же значения в том же порядке — подставляются как float32 (проверено хостом: плагин возвращает тот же блок)."""
    if state is None or len(state) <= DATA_OFF + 12:
        return None
    ent = _pt_entries(bytes(state[DATA_OFF:]))
    if not ent:
        return None
    head, raw, tail = _decode(block)
    i = raw.find(b'FabF', 64)
    if i < 8:
        return None
    n = struct.unpack_from('<I', raw, i - 8)[0]
    if n < 20 or i + n > len(raw):
        return None
    try:
        nl = struct.unpack_from('<I', raw, i + 8)[0]
        p = i + 12 + nl
        cnt = struct.unpack_from('<I', raw, p + 4)[0]
    except struct.error:
        return None
    if cnt != len(ent) or p + 8 + 4 * cnt > i + n:
        return None
    vals = b''.join(struct.pack('<f', v) for v in ent)
    return _encode(head, raw[:p + 8] + vals + raw[p + 8 + 4 * cnt:], tail, _line_lens(block))


# ----------------------------------------------------------------------------------------------- класс C: параметры по именам через vst3host
#
# Блок AAX (AAX_CChunkDataParser) хранит пары «имя параметра -> значение» (у soothe2 имена совпадают с названиями параметров VST3).
# Значения подаются в настоящий плагин VST3 нативным хостом vst3host (пакетный режим), хост возвращает состояние компонента/контроллера —
# оно подставляется в чанк Reaper (обёртка берётся из библиотеки FX). Если плагина / хоста нет — настройки не переносятся.
import json
import os
import re
import subprocess

ALIASES = {
    # AAX-имя параметра -> название параметра VST3 (по плагинам)
    'soothe2': {'band0 on': 'low cut on', 'band0 q': 'low cut q', 'band0 freq': 'low cut freq', 'band0 balance': 'low cut balance', 'band0 mode': 'low cut slope',
                'band5 on': 'high cut on', 'band5 q': 'high cut q', 'band5 freq': 'high cut freq', 'band5 balance': 'high cut balance', 'band5 mode': 'high cut slope'},
}
VST3_DIRS = ['/Library/Audio/Plug-Ins/VST3', os.path.expanduser('~/Library/Audio/Plug-Ins/VST3')]
_HERE = os.path.dirname(os.path.abspath(__file__))
_RES = {}            # (bundle, строка параметров) -> (comp, ctrl)
_PARAMS = {}         # bundle -> {название параметра (нижний регистр): параметр} | None
_IDS = {}            # bundle -> {ID параметра: параметр}


NAMED_VIA_HOST = True                                            # False — настройки по именам параметров не строятся хостом: их выставит сам Reaper (расширение; см. build_requests)
HOST_ENABLED = True                                              # False — режим чтения сведений о сессии (--info): хост не запускается, используется только готовый кэш


def _host():
    if not HOST_ENABLED:
        return None
    for c in (os.environ.get('PX_VST3HOST'), os.path.join(_HERE, '..', 'vst3host'), os.path.join(_HERE, '..', 'vst3host', 'build', 'vst3host'),
              os.path.join(_HERE, 'vst3host')):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return os.path.abspath(c)
    return None


def _bundle(block):
    m = re.match(r'<VST "VST3: ([^"]*)" (?:"([^"]+)"|(\S+))', block[0])
    if not m:
        return None, None
    fn = m.group(2) or m.group(3)
    return find_bundle(fn), m.group(1)


def find_bundle(fn):
    """Путь к .vst3 по имени файла: в папках VST3 и на один уровень глубже (например, Sonnox/Oxford Inflator.vst3)."""
    for d in VST3_DIRS:
        if os.path.isdir(os.path.join(d, fn)):
            return os.path.join(d, fn)
        if os.path.isdir(d):
            for sub in os.listdir(d):
                c = os.path.join(d, sub, fn)
                if os.path.isdir(os.path.join(d, sub)) and os.path.isdir(c):
                    return c
    return None


def named_entries(data):
    """[(тип, имя, значение)] из стандартного блока AAX или None."""
    if data[:4] != b'\x01\x01\x01\x01':
        return None
    out, j = [], 4
    try:
        while j + 4 <= len(data):
            t = chr(data[j])
            if data[j + 1:j + 2] != b'_' or t not in 'fdls':
                break
            k = data.index(b'\0', j + 2)
            nm = data[j + 2:k].decode('latin1')
            j = k + 1
            j += (-(j - 4)) % 4
            if t == 'f':
                v = struct.unpack_from('>f', data, j)[0]; j += 4
            elif t == 'd':
                v = struct.unpack_from('>d', data, j)[0]; j += 8
            elif t == 'l':
                v = float(struct.unpack_from('>i', data, j)[0]); j += 4
            else:
                v = float(struct.unpack_from('>h', data, j)[0]); j += 4
            out.append((t, nm, v))
    except (ValueError, struct.error):
        return None
    return out


_CACHE_FILE = os.path.expanduser('~/Library/Caches/Reaper to Pro Tools/vst3_params.json')
_DISK = None


def _disk_cache():
    global _DISK
    if _DISK is None:
        try:
            _DISK = json.load(open(_CACHE_FILE, encoding='utf-8'))
        except (OSError, ValueError):
            _DISK = {}
    return _DISK


def _bundle_sig(bundle):
    """Подпись версии плагина: размер и время изменения его исполняемого файла (при обновлении плагина кэш обновляется)."""
    try:
        mac = os.path.join(bundle, 'Contents', 'MacOS')
        f = os.path.join(mac, sorted(os.listdir(mac))[0])
        st = os.stat(f)
        return f'{st.st_size}:{int(st.st_mtime)}'
    except (OSError, IndexError):
        return None


def _index_params(bundle, ps):
    res = {}
    for i, q in enumerate(ps):
        q['index'] = i                                        # индекс параметра в Reaper = порядок в IEditController (проверено на Pro-Q 3)
        res.setdefault(q['title'].lower(), q)                 # одинаковые названия — берётся первый параметр
    _IDS[bundle] = {str(q['id']): q for q in ps}
    return res


def _params(bundle):
    """Параметры VST3 (название -> параметр). Список запрашивается у хоста один раз на версию плагина и запоминается на диске."""
    if bundle in _PARAMS:
        return _PARAMS[bundle]
    sig, res = _bundle_sig(bundle), None
    cached = _disk_cache().get(bundle)
    if cached and sig and cached.get('sig') == sig:
        res = _index_params(bundle, cached['params'])
    else:
        host = _host()
        if host:
            try:
                r = subprocess.run([host, bundle, 'params'], capture_output=True, text=True, timeout=120)
                ps = json.loads(r.stdout)['params']
                res = _index_params(bundle, ps)
                if sig:
                    _disk_cache()[bundle] = {'sig': sig, 'params': ps}
                    try:
                        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
                        with open(_CACHE_FILE + '.tmp', 'w', encoding='utf-8') as f:
                            json.dump(_disk_cache(), f)
                        os.replace(_CACHE_FILE + '.tmp', _CACHE_FILE)
                    except OSError:
                        pass
            except (subprocess.SubprocessError, ValueError, KeyError, OSError):
                res = None
    _PARAMS[bundle] = res
    return res


def _host_json(bundle, key, args, timeout=120):
    """Ответ хоста (JSON) для плагина, запомненный на диске по версии плагина; None — хоста нет или плагин не загрузился."""
    sig, k = _bundle_sig(bundle), bundle + '#' + key
    c = _disk_cache().get(k)
    if c and sig and c.get('sig') == sig:
        return c['data']
    host = _host()
    if not host:
        return None
    try:
        r = subprocess.run([host, bundle] + args, capture_output=True, text=True, timeout=timeout)
        data = json.loads(r.stdout)
    except (subprocess.SubprocessError, ValueError, OSError):
        return None
    if sig:
        _disk_cache()[k] = {'sig': sig, 'data': data}
        try:
            os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
            with open(_CACHE_FILE + '.tmp', 'w', encoding='utf-8') as f:
                json.dump(_disk_cache(), f)
            os.replace(_CACHE_FILE + '.tmp', _CACHE_FILE)
        except OSError:
            pass
    return data


def buses(bundle):
    """{'mono': bool, 'stereo': ..., '5.1': ...} — какие форматы каналов (вход = выход) принимает VST3-плагин, или None."""
    return _host_json(bundle, 'buses', ['buses'])


def _num(s):
    """Число из подписи плагина: «3190 Hz» -> 3190, «1.20 kHz» / «1.2k» -> 1200; None, если числа нет («Off», «-inf»)."""
    m = re.match(r'\s*([-+]?\d+(?:[.,]\d+)?)\s*(k(?![a-z]*b))?', s.lower())
    return float(m.group(1).replace(',', '.')) * (1000 if m.group(2) else 1) if m else None


def plain_to_norm(bundle, pid, v):
    """Нормализованное значение (0..1) параметра pid, при котором плагин показывает число v (значение в его единицах: Гц, мс, дБ).
    Шкала снимается хостом (domain, 101 точка) и интерполируется линейно между соседними точками; None — шкала немонотонна или v вне её."""
    dom = _host_json(bundle, 'domain', ['domain', '--grid', '101'])
    pts = [(x, _num(t)) for x, t in (dom or {}).get(str(pid), ())]
    pts = [(x, y) for x, y in pts if y is not None]
    if len(pts) < 2:
        return None
    ys = [y for _, y in pts]
    if not (all(a <= b for a, b in zip(ys, ys[1:])) or all(a >= b for a, b in zip(ys, ys[1:]))):
        return None
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if min(y0, y1) <= v <= max(y0, y1):
            return x0 if y1 == y0 else x0 + (x1 - x0) * (v - y0) / (y1 - y0)
    return None


def warm(bundles):
    """Заранее получает параметры нескольких плагинов параллельно (хост запускается только для тех, что не в кэше)."""
    todo = [b for b in set(bundles) if b and b not in _PARAMS]
    if len(todo) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(_params, todo))


def _tokens(x):
    """Слова названия: «Split1LM» / «lm split1» / «band2 q» -> нижний регистр, camelCase и цифры разделены, порядок не важен."""
    x = re.sub(r'([a-z])([A-Z])', r'\1 \2', x)
    x = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', x)
    x = re.sub(r'([A-Za-z])(\d)', r'\1 \2', x)
    x = re.sub(r'(\d)([A-Za-z])', r'\1 \2', x)
    return tuple(sorted(re.findall(r'[a-z0-9]+', x.lower())))


def make_matcher(bundle, plugname, names):
    """Функция имя параметра AAX -> параметр VST3 (или None): точное имя, синоним плагина, имя без регистра/пробелов, набор слов, номер (= ID параметра)."""
    ps = _params(bundle) or {}
    al = {k.lower(): v.lower() for k, v in ALIASES.get(plugname, {}).items()}
    squash = lambda x: re.sub(r'[^a-z0-9]', '', x.lower())
    by_sq, by_tok = {}, {}
    for k, q in ps.items():
        by_sq.setdefault(squash(k), q)
        by_tok.setdefault(_tokens(k), q)
    by_id = _IDS.get(bundle, {})
    numeric = bool(names) and all(n.isdigit() for n in names)

    def match(nm):
        q = by_id.get(str(juce_id(nm)))                              # плагины на JUCE: ID параметра VST3 = хэш строкового ID (тот же, что имя записи AAX)
        if q is not None:
            return q
        key = al.get(nm.lower(), nm.lower())
        return ps.get(key) or by_sq.get(squash(key)) or by_tok.get(_tokens(key)) or (by_id.get(nm) if numeric else None)
    return match


def juce_id(s):
    """ID параметра VST3 у плагинов на JUCE: String::hashCode() строкового ID (31*h + символ, 32 бита) & 0x7fffffff
    (JUCE_USE_STUDIO_ONE_COMPATIBLE_PARAMETERS). Сверено: NUGEN ISL2 22/22, SonoBus 37/37, ValhallaSupermassive 18/18, Accentize Chameleon2 1003/1003."""
    h = 0
    for ch in s.encode('utf-8'):
        h = (31 * h + ch) & 0xFFFFFFFF
    return h & 0x7FFFFFFF


def _setline(bundle, plugname, state):
    """Строка «id=значение ...» для хоста или None, если блок не подходит."""
    if state is None or len(state) <= DATA_OFF + 12:
        return None
    ent = named_entries(bytes(state[DATA_OFF:]))
    if not ent or not all(nm for _, nm, _ in ent):
        return None
    if not _params(bundle):
        return None
    match = make_matcher(bundle, plugname, [nm for _, nm, _ in ent])
    out, hit = [], 0
    for t, nm, v in ent:
        q = match(nm)
        if q is None:
            continue
        hit += 1
        if t in 'ls' and q['steps'] > 0:
            v = v / q['steps']
        elif t in 'fd' and not 0.0 <= v <= 1.0:
            v = plain_to_norm(bundle, q['id'], v)                   # значение в единицах плагина (Гц, мс, дБ) -> 0..1 по его же шкале
            if v is None:
                return None
        out.append(f"{q['id']}={min(1.0, max(0.0, v)):.9g}")
    return ' '.join(out) if hit * 10 >= len(ent) * 4 else None


def prefetch(pairs):
    """pairs — [(блок Reaper, плагин PT)]: одним запуском хоста на каждый плагин получает состояния всех экземпляров."""
    host = _host()
    if not host:
        return
    todo = {}
    for block, pl in pairs:
        bundle, vname = _bundle(block)
        if not bundle:
            continue
        line = _setline(bundle, pl['name'], pl.get('state'))
        if line is not None and (bundle, line) not in _RES:
            todo.setdefault(bundle, set()).add(line)
    def run(item):
        bundle, lines = item[0], sorted(item[1])
        try:
            r = subprocess.run([host, bundle, 'batch'], input='\n'.join(l if l else '' for l in lines) + '\n', capture_output=True, text=True, timeout=600)
            outs = [json.loads(x) for x in r.stdout.splitlines() if x.startswith('{')]
        except (subprocess.SubprocessError, ValueError, OSError):
            return bundle, lines, None
        return bundle, lines, (outs if len(outs) == len(lines) else None)
    items = list(todo.items())
    if len(items) > 1:                                         # разные плагины — параллельно
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(run, items))
    else:
        results = [run(i) for i in items]
    for bundle, lines, outs in results:
        if outs is None:
            continue
        for l, o in zip(lines, outs):
            _RES[(bundle, l)] = (base64.b64decode(o['comp']), base64.b64decode(o['ctrl']))


def _split(raw):
    """Разбор обёртки Reaper VST3: позиция q поля N (общий размер блоков), размер компонента, размер контроллера."""
    for q in range(40, min(len(raw) - 24, 4000), 4):          # заголовок растёт с числом шин (SonoBus 18ch: > 200 байт)
        n, one, flags, cs, one2 = struct.unpack_from('<5I', raw, q)
        if one == 1 and one2 == 1 and flags in (0x10ffff, 0x100000) and q + 20 + cs <= len(raw):
            return q, n, cs
    return None


def rebuild(block, comp, lens_ok=True, ctrl=None):
    """Подставляет новое состояние компонента (и контроллера, если ctrl задан) в обёртку Reaper, размеры пересчитываются.
    Обёртка с позиции q: [n][1][флаги][cs][1][компонент cs][ks][контроллер ks][хвост], n = cs + ks + 16."""
    head, raw, tail = _decode(block)
    sp = _split(raw)
    if not sp:
        return None
    q, n, cs = sp
    rest = raw[q + 20 + cs:]
    if ctrl is not None and len(rest) >= 4:
        ks = struct.unpack_from('<I', rest, 0)[0]
        if 4 + ks <= len(rest):
            rest = struct.pack('<I', len(ctrl)) + ctrl + rest[4 + ks:]
            n = n - ks + len(ctrl)
    new = raw[:q] + struct.pack('<I', n + len(comp) - cs) + raw[q + 4:q + 12] + struct.pack('<I', len(comp)) + raw[q + 16:q + 20] + comp + rest
    lens = _line_lens(block)
    if len(new) == len(raw):
        return _encode(head, new, tail, lens)
    first = lens[0] if lens else 76
    step = lens[1] if len(lens) > 1 else 96
    return _encode(head, new, tail, [first] + [step] * ((len(new) - first) // step))


def transplant_named(block, plugin):
    if not NAMED_VIA_HOST:
        return None
    bundle, vname = _bundle(block)
    if not bundle:
        return None
    line = _setline(bundle, plugin['name'], plugin.get('state'))
    if line is None:
        return None
    if (bundle, line) not in _RES:
        prefetch([(block, plugin)])
    res = _RES.get((bundle, line))
    if res is None:
        return None
    return rebuild(block, res[0], ctrl=res[1] or None)                   # контроллер — из того же запуска хоста (часть настроек плагины держат там)


def transplant_xml(block, state):
    """Класс X (Altiverb 8, Speakerphone — Audio Ease): блок AAX = XML (plist) + нули, состояние компонента VST3 = u32 длина XML + тот же XML.
    Проверено хостом: плагин принимает состояние, ключи на выходе совпадают с блоком PT."""
    if state is None or len(state) <= DATA_OFF + 12:
        return None
    data = bytes(state[DATA_OFF:])
    if data[:5] != b'<?xml':
        return None
    head, raw, tail = _decode(block)
    sp = _split(raw)
    if not sp or raw[sp[0] + 24:sp[0] + 29] != b'<?xml':             # у плагина в библиотеке состояние компонента должно быть тоже [длина][XML]
        return None
    end = data.rfind(b'>') + 1
    return rebuild(block, struct.pack('<I', end) + data[:end])


def _vstw_xml_span(comp, marker=b'<?xml'):
    """Состояние компонента Sonnox / Soundtoys: обёртка VST2-банка `VstW … CcnK <размер> FBCh …`, в конце [u32 BE длина][чанк]
    (XML у Sonnox, текст `WIDGET = …` у Soundtoys). Возвращает (начало чанка, длина) или None."""
    if comp[:4] != b'VstW' or comp[16:20] != b'CcnK':
        return None
    i = comp.find(marker)
    if i < 4:
        return None
    n = struct.unpack_from('>I', comp, i - 4)[0]
    return (i, n) if i + n <= len(comp) else None


def transplant_vstw_xml(block, state):
    """Класс W (Oxford Inflator / Dynamics — Sonnox): блок AAX = XML (preset) + нули, состояние компонента VST3 = VST2-банк с тем же XML внутри."""
    if state is None or len(state) <= DATA_OFF + 12:
        return None
    data = bytes(state[DATA_OFF:])
    if data[:5] != b'<?xml':
        return None
    head, raw, tail = _decode(block)
    sp = _split(raw)
    if not sp:
        return None
    q, n, cs = sp
    comp = raw[q + 20:q + 20 + cs]
    span = _vstw_xml_span(comp)
    if not span:
        return None
    i, old = span
    end = data.rfind(b'>') + 1
    xml = data[:end]
    new = comp[:i - 4] + struct.pack('>I', len(xml)) + xml + comp[i + old:]
    new = new[:20] + struct.pack('>I', len(new) - 24) + new[24:]          # размер CcnK
    return rebuild(block, new)


ST_MARK = b'WIDGET = '


def transplant_soundtoys(block, state):
    """Класс S (Soundtoys): блок AAX `pset` = [u32 BE длина][текст `WIDGET = …; PRESET = …`] + нули, состояние компонента VST3 —
    VST2-банк с тем же текстом (FBCh `StDc`, chunkSize @172)."""
    if state is None or len(state) <= DATA_OFF + 16:
        return None
    data = bytes(state[DATA_OFF:])
    n = struct.unpack_from('>I', data, 0)[0]
    if data[4:4 + len(ST_MARK)] != ST_MARK or 4 + n > len(data):
        return None
    head, raw, tail = _decode(block)
    sp = _split(raw)
    if not sp:
        return None
    q, _, cs = sp
    comp = raw[q + 20:q + 20 + cs]
    span = _vstw_xml_span(comp, ST_MARK)
    if not span:
        return None
    i, old = span
    text = data[4:4 + n]
    if comp[i:i + 30].split(b';')[0] != text[:30].split(b';')[0]:
        return None                                       # другой плагин Soundtoys (WIDGET не совпадает)
    new = comp[:i - 4] + struct.pack('>I', len(text)) + text + comp[i + old:]
    new = new[:20] + struct.pack('>I', len(new) - 24) + new[24:]          # размер CcnK
    return rebuild(block, new)


WV_MARK, WV_END = b'<PresetChunkXMLTree', b'</PresetChunkXMLTree>'


def waves_sub(xml):
    """Вариант плагина Waves (<PluginSubComp>: WD2S, T37M, ...) из XML пресета или None."""
    m = re.search(rb'<PluginSubComp>(.{4})</PluginSubComp>', xml[:2000])
    return m.group(1) if m else None


def waves_comp_span(comp):
    """Состояние компонента Waves VST3: [u32 BE 28+L][3][1][SubComp]['setA'][u32 BE L]['XPst'][XML L байт]<Bypass .../>. -> L или None."""
    if len(comp) < 48 or comp[24:28] != b'XPst' or comp[28:28 + len(WV_MARK)] != WV_MARK:
        return None
    n = struct.unpack_from('>I', comp, 20)[0]
    return n if 28 + n <= len(comp) else None


def waves_aax_xml(state):
    """XML пресета из блока AAX Waves (чанк `XPst`, затем чанк `curS`): (XML вместе с переводом строки, конец в data) или None."""
    data = bytes(state[DATA_OFF:])
    if bytes(state[30:34]) != b'XPst' or data[:len(WV_MARK)] != WV_MARK:
        return None
    e = data.find(WV_END)
    if e < 0:
        return None
    e += len(WV_END) + (data[e + len(WV_END):e + len(WV_END) + 1] == b'\n')
    return data[:e], e


def transplant_waves(block, state):
    """Класс V (Waves): XML `PresetChunkXMLTree` из блока AAX в состояние компонента VST3 (тот же XML). Только при одинаковом SubComp."""
    if state is None or len(state) <= DATA_OFF + 32:
        return None
    ax = waves_aax_xml(state)
    head, raw, tail = _decode(block)
    sp = _split(raw)
    if not ax or not sp:
        return None
    q, _, cs = sp
    comp = raw[q + 20:q + 20 + cs]
    n = waves_comp_span(comp)
    if n is None or waves_sub(ax[0]) != comp[12:16]:
        return None                                       # другой вариант плагина (моно / стерео): его XML этому компоненту не подходит
    x = ax[0]
    new = struct.pack('>I', 28 + len(x)) + comp[4:20] + struct.pack('>I', len(x)) + b'XPst' + x + comp[28 + n:]
    return rebuild(block, new)


def entries_layout(data):
    """Записи стандартного блока AAX: [(тип, имя, смещение значения, размер)] или None (блок начинается с `01 01 01 01`)."""
    if data[:4] != b'\x01\x01\x01\x01':
        return None
    out, j = [], 4
    try:
        while j + 4 <= len(data):
            t = chr(data[j])
            if data[j + 1:j + 2] != b'_' or t not in 'fdls':
                break
            k = data.index(b'\0', j + 2)
            nm = data[j + 2:k].decode('latin1')
            j = k + 1
            j += (-(j - 4)) % 4
            size = {'f': 4, 'd': 8, 'l': 4, 's': 2}[t]
            out.append((t, nm, j, size))
            j += 4 if t == 's' else size
    except (ValueError, IndexError):
        return None
    return out


def find_embedded(raw):
    """Блок AAX (записи `d_имя`), встроенный в состояние VST3: (смещение блока, записи) с наибольшим числом записей или None."""
    best, i = None, -1
    while True:
        i = raw.find(b'\x01\x01\x01\x01', i + 1)
        if i < 0:
            break
        lay = entries_layout(raw[i:])
        if lay and len(lay) >= 3 and (best is None or len(lay) > len(best[1])):
            best = (i, lay)
    return best


def transplant_embedded(block, state):
    """Класс E (Slapper ST и подобные): плагин пишет состояние VST3 тем же форматом записей `d_имя`, что и блок AAX — значения переносятся по именам записей
    в единицах самого плагина (без перевода в 0..1). Проверено хостом: плагин принимает состояние и показывает те же значения."""
    if state is None or len(state) <= DATA_OFF + 12:
        return None
    data = bytes(state[DATA_OFF:])
    lay_b = entries_layout(data)
    if not lay_b or not all(nm for _, nm, _, _ in lay_b):
        return None
    head, raw, tail = _decode(block)
    sp = _split(raw)
    if not sp:
        return None
    q, n, cs = sp
    comp = bytearray(raw[q + 20:q + 20 + cs])
    emb = find_embedded(bytes(comp))
    if not emb:
        return None
    k, lay_d = emb
    nb = {nm: (t, off, sz) for t, nm, off, sz in lay_b}
    hit = 0
    for t, nm, off, sz in lay_d:
        o = nb.get(nm)
        if o and o[0] == t and o[2] == sz:
            comp[k + off:k + off + sz] = data[o[1]:o[1] + sz]
            hit += 1
    if hit * 2 < len(lay_d):
        return None
    return rebuild(block, bytes(comp))


def transplant_juce(block, state):
    """Плагин на JUCE: в блоке AAX второй чанк `<тройка FourCC> juce <имя 32>` = getStateInformation — то же состояние, что у VST3
    (обёртка VST3 дописывает хвост JUCEPrivateData — его берём из блока Reaper). Для VST2-совместимой обёртки (`VstW`) не применяется."""
    if state is None or len(state) <= DATA_OFF + 40:
        return None
    st = bytes(state)
    i = st.find(st[18:30] + b'juce', DATA_OFF)                    # тройка + тег: просто `juce` может встретиться внутри данных
    if i < 0:
        return None
    j = i + 12
    pay = st[j + 36:]
    if pay[:4] == b'VC2!' and len(pay) >= 8:
        n = struct.unpack_from('<I', pay, 4)[0]
        if 8 + n > len(pay):
            return None
        pay = pay[:8 + n]
    head, raw, tail = _decode(block)
    sp = _split(raw)
    if not sp:
        return None
    q, n_, cs = sp
    comp = raw[q + 20:q + 20 + cs]
    tag = b'JUCEPrivateData'
    if comp[:4] == b'VstW' or not comp.endswith(tag) or len(comp) < len(tag) + 8:
        return None
    m = struct.unpack_from('<q', comp, len(comp) - len(tag) - 8)[0]
    cut = len(comp) - len(tag) - 8 - m
    if not 0 <= cut <= len(comp):
        return None
    if comp[:4] == b'VC2!' and pay[:4] == b'VC2!':                # между XML и хвостом VST3-обёртка держит те же байты выравнивания, что были
        k = 8 + struct.unpack_from('<I', comp, 4)[0]
        pad = comp[k:cut] if k <= cut else b''
        pay = pay + pad
    return rebuild(block, pay + comp[cut:])


STATS = {'applied': 0, 'unsupported': 0, 'last': None}


def apply_state(block, plugin):
    """Строки блока <VST ...> с перенесёнными настройками (или исходный блок, если формат плагина не поддержан).
    Какой класс сработал — в STATS['last'] (для регрессионного прогона regress.py)."""
    for name, fn in TRANSPLANTS:
        new = fn(block, plugin) if name == 'named' else fn(block, plugin.get('state'))
        if new is not None:
            STATS['applied'] += 1
            STATS['last'] = name
            return new
    STATS['unsupported'] += 1
    STATS['last'] = None
    return block


def aax_id_keys(aid):
    """ID параметра VST3 (строкой), которым может соответствовать ID параметра AAX aid: у Audio Ease ID VST3 = FourCC AAX как число
    ('aa70' -> 1633761072), у FabFilter — то же число («12»), у JUCE — хэш строкового ID (juce_id). Проверено на 147 плагинах библиотеки
    параметров: по ID сопоставляются и 689 параметров, у которых имена AAX и VST3 различаются (Symphony, Stratus, Pro-Q 4)."""
    out = set()
    if aid:
        if len(aid) == 4:
            out.add(str(int.from_bytes(aid.encode('latin-1'), 'big')))
        if aid.isdigit():
            out.add(str(int(aid)))                             # хэш JUCE от «0», «1» попадает в чужие маленькие ID (hash('0') = 48) — не для чисел
        else:
            out.add(str(juce_id(aid)))
    return out


def param_for(block, name, aid=None):
    """Параметр VST3 для автоматизации Pro Tools: по ID параметра AAX (aid), иначе по имени: {'index', 'steps', 'title', 'id'} или None.
    (None и host_ok=False -> хоста/плагина нет.)"""
    bundle, _ = _bundle(block)
    if not bundle:
        return None, False
    ps = _params(bundle)
    if not ps:
        return None, False
    ids = _IDS.get(bundle) or {}
    q = next((ids[k] for k in aax_id_keys(aid) if k in ids), None)
    if q is not None:
        return {'index': q['index'], 'steps': q['steps'], 'title': q['title'], 'id': q['id']}, True
    k = name.lower()
    q = ps.get(k)
    if q is None:
        sq = re.sub(r'[^a-z0-9]', '', k)
        q = next((v for t, v in ps.items() if re.sub(r'[^a-z0-9]', '', t) == sq), None)
    return ({'index': q['index'], 'steps': q['steps'], 'title': q['title'], 'id': q['id']} if q else None), True


def vst3_param_id(vst_file, index):
    """ID параметра VST3 с номером index (номер в Reaper = порядок в IEditController) плагина из файла vst_file, или None."""
    bundle = find_bundle(vst_file) if vst_file else None
    if not bundle or not _params(bundle):
        return None
    return next((q['id'] for q in (_IDS.get(bundle) or {}).values() if q['index'] == index), None)


def build_requests(plugin, block=None):
    """Параметры плагина PT для выставления средствами Reaper: [[имя параметра, значение, вид, индекс]], вид 'n' — нормализованное 0..1, 'i' — номер состояния,
    индекс — номер параметра в плагине Reaper, если его удалось определить по списку параметров VST3 (из кэша/хоста), иначе -1 (Reaper найдёт по имени).
    Источники: записи блока AAX с именами; записи с номерами — имена берутся из списка параметров слота (индекс списка - 2 = номер), а индекс — по ID параметра VST3;
    блок непрозрачен — сам список параметров."""
    st = plugin.get('state')
    plist = plugin.get('plist') or []
    ent = named_entries(bytes(st[DATA_OFF:])) if st is not None and len(st) > DATA_OFF + 12 else None
    al = {k.lower(): v for k, v in ALIASES.get(plugin['name'], {}).items()}
    bundle = _bundle(block)[0] if block else None
    names_all = [nm for _, nm, _ in ent] if ent else [nm for nm, _, _, _ in plist]
    match = make_matcher(bundle, plugin['name'], names_all) if bundle and _params(bundle) else (lambda n: None)
    out = []
    if ent and all(nm for _, nm, _ in ent):
        numeric = all(nm.isdigit() for _, nm, _ in ent)
        names = {idx - 2: nm for nm, idx, _, _ in plist} if numeric else {}
        for k, (t, nm, v) in enumerate(ent):
            name = names.get(k) if numeric else nm
            q = match(nm)
            if not name and not q:
                continue
            name = al.get(name.lower(), name) if name else q['title']
            if t not in 'ls' and not 0.0 <= v <= 1.0:
                return []                                         # значения в реальных единицах плагина — без знания плагина перевести нельзя
            out.append([name, v, 'i' if t in 'ls' else 'n', q['index'] if q else -1])
        if out:
            return out
    for nm, idx, steps, v in plist:                               # непрозрачный блок: значения из списка параметров
        if nm.lower() in ('master bypass', 'bypass'):
            continue
        name = al.get(nm.lower(), nm)
        q = match(name)
        out.append([name, v * (steps - 1) if steps > 1 else v, 'i' if steps > 1 else 'n', q['index'] if q else -1])
    return out


TRANSPLANTS = (('ffbs', transplant_ffbs), ('fabf', transplant_fabf), ('xml', transplant_xml), ('vstw_xml', transplant_vstw_xml),
               ('soundtoys', transplant_soundtoys), ('waves', transplant_waves), ('juce', transplant_juce), ('embedded', transplant_embedded),
               ('named', transplant_named))


def _selftest():
    import base64 as b64
    assert aax_id_keys('aa70') >= {'1633761072'} and aax_id_keys('2') == {'2'} and aax_id_keys('1001') == {'1001', str(0x31303031)}
    assert aax_id_keys('AutoAlertIntDialogMin') == {str(juce_id('AutoAlertIntDialogMin'))} and aax_id_keys('') == set()
    comp, ctrl = b'C' * 37, b'K' * 11
    raw = b'\x00' * 48 + struct.pack('<5I', len(comp) + len(ctrl) + 16, 1, 0x100000, len(comp), 1) + comp + struct.pack('<I', len(ctrl)) + ctrl + b'\x00' * 10
    enc = b64.b64encode(raw).decode()
    block = ['<VST "VST3: T (X)" t.vst3 0 "" 1{00} ""', enc[:76], enc[76:], '>']
    assert rebuild(block, comp) == _encode(*_decode(block), _line_lens(block))            # без изменений — те же байты
    for c2, k2 in ((b'N' * 50, None), (b'N' * 5, b'Z' * 70), (comp, b'')):
        h, r, t = _decode(rebuild(block, c2, ctrl=k2))
        q, n, cs = _split(r)
        k = struct.unpack_from('<I', r, q + 20 + cs)[0]
        want = ctrl if k2 is None else k2
        assert r[q + 20:q + 20 + cs] == c2 and r[q + 24 + cs:q + 24 + cs + k] == want and n == cs + k + 16 and r.endswith(b'\x00' * 10), (len(c2), k)
    print('ok')


if __name__ == '__main__':
    _selftest()
