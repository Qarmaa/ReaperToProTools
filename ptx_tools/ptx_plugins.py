#!/usr/bin/env python3
"""ptx_plugins.py — плагины на дорожках .ptx (чтение).

Найдено по сессиям пользователя и PTSL ExportSessionInfoAsText:
  каталог плагинов сессии  : блоки 0x1017 (ctype 0x1017, own[2] == 3): pstr имя | 3 FourCC в обратном порядке байт
                             (производитель, продукт, вариант = конфигурация каналов) | 4 байта каналов (вх, ?, вых, ?) | ... | bundle id
  слоты инсертов дорожки   : 0x261c/0x261e > 0x261b > 0x2627 > дети по порядку: слот A..J; занятый слот = 0x2616, пустой = 0x2625
  0x2616 own[2]            : 1 = плагин в bypass, 0 = активен
  0x2616 > 0x2615 > 0x2613 > 0x1038 : own[18:30] = те же FourCC (прямой порядок), дальше состояние плагина (непрозрачное, у каждого
                             производителя своё) ; 0x2614 > 0x260f = список автоматизируемых параметров с текущими значениями
"""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ptx_model import Block  # noqa: E402
import ptx_trackstate as TS  # noqa: E402

CHAN = {1: 'mono', 2: 'stereo', 3: 'LCR', 4: 'quad', 5: '5.0', 6: '5.1'}


def _walk(items):
    for x in items:
        if isinstance(x, Block):
            yield x
            yield from _walk(x.items)


def read_catalog(p):
    """{(mfr, product, variant): {'name', 'in', 'out', 'bundle'}} — каталог плагинов сессии."""
    cat = {}
    for b in _walk(p.roots):
        if b.ctype != 0x1017:
            continue
        o = TS.own_bytes(b)
        if len(o) < 20 or o[2] != 3:
            continue
        n = struct.unpack_from('<I', o, 3)[0]
        if n > 200:
            continue
        name = o[7:7 + n].decode('utf-8', 'replace')
        f = o[7 + n:7 + n + 12]
        key = tuple(bytes(f[i:i + 4][::-1]) for i in (0, 4, 8))
        ch = o[7 + n + 12:7 + n + 16]
        bundle = ''
        k = o.find(b'com.', 7 + n + 16)
        if k > 4:
            ln = struct.unpack_from('<I', o, k - 4)[0]
            bundle = o[k:k + ln].decode('utf-8', 'replace')
        cat.setdefault(key, {'name': name, 'in': ch[0], 'out': ch[2], 'bundle': bundle})
    return cat


def _read_curves(slot_blk):
    """Нарисованные кривые автоматизации параметров плагина (слот 0x2616 > 0x2614 > 0x260f > 0x260b): [{'name', 'index', 'discrete',
    'initial', 'points': [(сэмпл, значение)]}]. Служебные записи Pro Tools (метки с 4-м байтом != 0) и пустые значения не считаются кривыми."""
    out = []
    lst = TS.kids(slot_blk, 0x2614)
    if not lst:
        return out
    for d in TS.kids(lst[0], 0x260f):
        o = TS.own_bytes(d)
        try:
            n = struct.unpack_from('<I', o, 3)[0]
            name = o[7:7 + n].decode('utf-8', 'replace')
            idx = struct.unpack_from('<I', o, 7 + n)[0]
            vb = TS.kids(d, 0x260b)
            b = TS.own_bytes(vb[0]) if vb else b''
            cnt = struct.unpack_from('<I', b, 12)[0]
        except (struct.error, IndexError):
            continue
        if cnt < 2 or len(b) < 28 + 4 + 8 * (cnt - 1):
            continue
        raw = [(struct.unpack_from('<I', b, 32 + 8 * i)[0], b[36 + 8 * i:40 + 8 * i]) for i in range(cnt - 1)]
        if any(t >> 24 for t, _ in raw) or any(t1 <= t0 for (t0, _), (t1, _) in zip(raw, raw[1:])):
            continue                                             # служебные метки (старший байт != 0) — не кривая
        initial = b[28:32]
        discrete = struct.unpack_from('<I', initial)[0] <= 1
        dec = (lambda v: float(struct.unpack('<I', v)[0])) if discrete else (lambda v: struct.unpack('<f', v)[0])
        out.append({'name': name, 'index': idx, 'aid': _entry_aid(d), 'discrete': discrete, 'initial': dec(initial),
                    'points': [(t, dec(v)) for t, v in raw]})
    return out


def read_param_list(slot_blk):
    """Список параметров слота (0x2614>0x260f): [(имя, индекс, число состояний (0 — непрерывный), значение 0..1)].
    Значение — 4 байта по смещению 28 блока 0x260b: непрерывный параметр — знаковое 2.30 fixed-point (2n-1)*2^30, дискретный — номер состояния
    (число состояний — u32 в хвосте записи). Проверено на Symphony (76 из 76 непрерывных совпали с блоком состояния)."""
    out = []
    lst = TS.kids(slot_blk, 0x2614)
    if not lst:
        return out
    for e in TS.kids(lst[0], 0x260f):
        o = TS.own_bytes(e)
        try:
            n = struct.unpack_from('<I', o, 3)[0]
            name = o[7:7 + n].decode('utf-8', 'replace')
            idx = struct.unpack_from('<I', o, 7 + n)[0]
            steps = struct.unpack_from('<I', o, 7 + n + 4 + 3)[0]
            raw = struct.unpack_from('<i', TS.own_bytes(TS.kids(e, 0x260b)[0]), 28)[0]
        except (struct.error, IndexError):
            continue
        if steps > 1:
            v = max(0.0, min(1.0, raw / (steps - 1)))
        else:
            steps = 0
            v = max(0.0, min(1.0, (raw / 2 ** 30 + 1) / 2))
        out.append((name, idx, steps, v))
    return out


def _plugin_block(x):
    """Блок слота 0x2616 с плагином; слот с параметрами (напр. Slapper ST) обёрнут в 0x2617 > 0x2616."""
    if x.ctype == 0x2617:
        k = TS.kids(x, 0x2616)
        return k[0] if k else None
    return x if x.ctype == 0x2616 else None


def read_plugins(p):
    """По дорожкам (порядок track_blocks): [{'slot': 0..9, 'name', 'format', 'bypass', 'key'}]."""
    cat = read_catalog(p)
    out = []
    for tb in TS.track_blocks(p):
        lst = []
        b261b = TS.kids(tb, 0x261b)
        d = TS.kids(b261b[0], 0x2627) if b261b else []
        if d:
            for si, x in enumerate(i for i in d[0].items if isinstance(i, Block)):
                x = _plugin_block(x)
                if x is None:
                    continue
                try:
                    h = TS.kids(TS.kids(x, 0x2615)[0], 0x2613)[0]
                    st = TS.own_bytes(TS.kids(h, 0x1038)[0])
                except IndexError:
                    continue
                key = tuple(bytes(st[18 + 4 * k:22 + 4 * k]) for k in range(3))
                if st[18:22] in (b'\0\0\0\0',):
                    continue
                info = cat.get(key) or next((v for k, v in cat.items() if k[:2] == key[:2]), None)   # вариант каналов мог не попасть в каталог
                o = TS.own_bytes(x)
                lst.append({'slot': si, 'key': key, 'name': info['name'] if info else '/'.join(k.decode('latin1') for k in key),
                            'format': CHAN.get(info['in'], str(info['in'])) if info else '?', 'bypass': bool(o[2]),
                            'bundle': info['bundle'] if info else '', 'curves': _read_curves(x), 'state': bytes(st), 'plist': read_param_list(x)})
        out.append(lst)
    return out


if __name__ == '__main__':
    from ptx_model import PTX
    pp = PTX(open(sys.argv[1], 'rb').read())
    for n, pl in zip(TS.aggregate_names(pp), read_plugins(pp)):
        if pl:
            print(n[0], [(x['slot'], x['name'], x['format'], 'bypass' if x['bypass'] else '') for x in pl])


# ----------------------------------------------------------------------------------------------------------- запись

_TPL_CACHE = None


def _templates():
    """Встроенные шаблоны + личная библиотека пользователя (ptx_plugin_learn); при совпадении ключа побеждает встроенный."""
    global _TPL_CACHE
    if _TPL_CACHE is None:
        try:
            from ptx_templates_plugins import SLOTS
        except ImportError:
            SLOTS = {}
        merged = dict(SLOTS)
        try:
            from ptx_templates_plugins2 import SLOTS2            # слоты, созданные самим Pro Tools (make_plugin_templates2.py)
            for k, v in SLOTS2.items():
                merged.setdefault(k, v)
        except ImportError:
            pass
        try:
            import ptx_plugin_learn
            for k, v in ptx_plugin_learn.load_user().items():
                merged.setdefault(k, v)
        except Exception:
            pass
        _TPL_CACHE = merged
    return _TPL_CACHE


def template_out_channels(tpl):
    """Число выходных каналов плагина по записи каталога шаблона (у PanMan mono — 2: следующий в цепочке должен быть стерео)."""
    try:
        o = tpl['catalog'][7:]
        n = struct.unpack_from('<I', o, 3)[0]
        return o[7 + n + 14]
    except (KeyError, IndexError, struct.error):
        return None


def find_template(name, fmt, placeholder=True):
    """Шаблон вставки плагина по имени и формату (mono/stereo/5.1) -> (key, шаблон) | None.
    Нет готового шаблона -> слот строится по таблице идентичностей (ptx_plugin_ids), иначе (placeholder) — слот-заглушка: Pro Tools
    покажет плагин неактивным («отсутствует») с этим именем."""
    import re
    # Waves и др. пишут вариант в имени VST3 («S1 Imager Stereo», «Doubler2 Mono/Stereo»); в Pro Tools вариант — это формат слота
    short = re.sub(r'\s+(?:mono/stereo|mono|stereo|5\.0|5\.1|7\.1)\s*$', '', name.strip(), flags=re.I)
    for nm in dict.fromkeys((name, short)):
        n = ''.join(c for c in nm.lower() if c.isalnum())
        for key, v in _templates().items():
            t = ''.join(c for c in v['name'].lower() if c.isalnum())
            if v['format'] == fmt and n and (t == n or t.endswith(n)):
                return key, v
        if nm == short or _ids_lookup(n, fmt):
            return _synth_template(nm, fmt, n, placeholder)
    return None


_BASE_KEYS = {'mono': (b'FabF', b'FQ3p', b'FQ32'), 'stereo': (b'FabF', b'FQ3p', b'FQ3p'),
              'LCR': (b'FabF', b'FQ3p', b'3Q3p'), '5.1': (b'FabF', b'FQ3p', b'6Q3p')}
SYNTH_ENABLED = True                                # слоты по таблице идентичностей / заглушки: включить, когда таблица ptx_plugin_ids собрана
_PH_MFR = b'RtoP'                                   # производитель заглушки: такого плагина нет, Pro Tools пометит его отсутствующим


def _ids_lookup(n, fmt):
    try:
        from ptx_plugin_ids import IDS
    except ImportError:
        IDS = {}
    try:
        import ptx_plugin_scan
        user = ptx_plugin_scan.load_user()                  # личная таблица: DigiPageTable.xml установленных плагинов (ptx_plugin_scan.py --scan)
        ptc = ptx_plugin_scan.read_pt_cache()               # кэш Pro Tools: все установленные AAX; без него — page tables бандлов
        ptc = {**ptx_plugin_scan.read_waves(), **(ptc or ptx_plugin_scan.read_page_tables())}   # Waves в кэше PT нет (WaveShell)
    except Exception:
        user, ptc = {}, {}
    if user or ptc:
        IDS = {**IDS, **ptc, **user}
    best = None
    for name, d in IDS.items():
        for alias in [name] + list(d.get('aliases') or ()):
            t = ''.join(c for c in alias.lower() if c.isalnum())
            if n and (t == n or t.endswith(n)):
                if t == n:
                    return name, d
                if best is None or len(t) < len(best[0]):
                    best = (t, name, d)
    return (best[1], best[2]) if best else None


def _synth_template(name, fmt, n, placeholder):
    if not SYNTH_ENABLED or fmt not in _BASE_KEYS or not n:
        return None
    tpls = _templates()
    base = tpls.get(_BASE_KEYS[fmt] + (fmt,))
    if base is None:
        return None
    import ptx_slot_gen
    hit = _ids_lookup(n, fmt)
    if hit and fmt not in hit[1]['plugs']:
        return None                                      # плагин известен, но в таком формате каналов его нет — не переносим (это не «отсутствующий»)
    if hit:
        pname, d = hit
        mfr, prod, plug = (x.encode('latin-1') for x in (d['mfr'], d['prod'], d['plugs'][fmt]))
        eid = d.get('eid') or ''
    elif placeholder:
        import zlib
        pname, eid = name.strip(), ''
        c = zlib.crc32((name.strip().lower() + '|' + fmt).encode('utf-8'))
        alpha = b'abcdefghijklmnopqrstuvwxyz0123456789'
        prod = bytes(alpha[(c >> (5 * i)) % 36] for i in range(4))
        plug = bytes(alpha[(c >> (5 * i + 20)) % 36] for i in range(4))
        mfr = _PH_MFR
    else:
        return None
    key = (mfr, prod, plug, fmt)
    try:
        from ptx_templates_plugins2 import NOT_INSERTABLE
        if (mfr, prod, plug) in NOT_INSERTABLE:
            return None                                  # Pro Tools не вставляет этот плагин в аудио-дорожку (MIDI / инструмент)
    except ImportError:
        pass
    if key not in tpls:
        tpls[key] = ptx_slot_gen.synth(base, pname, mfr, prod, plug, eid)
        tpls[key]['synth'] = 'ids' if hit else 'placeholder'
    return key, tpls[key]


def add_plugin(p, track, slot, key, bypass=False):
    """Вставляет плагин (шаблон по ключу: 3 FourCC + формат) в слот инсерта slot (0..9) дорожки track; слот должен быть пустым."""
    from ptx_build import clone
    tpl = _templates()[key]
    tb = TS.track_blocks(p)[track]
    d = TS.kids(TS.kids(tb, 0x261b)[0], 0x2627)[0]
    blocks = [x for x in d.items if isinstance(x, Block)]
    if slot >= len(blocks) or blocks[slot].ctype != 0x2625:
        return False
    import zlib
    new = clone(zlib.decompress(tpl['slot']))
    TS.own_patch(new, 2, b'\x01' if bypass else b'\x00')
    snap = _slot_catalog_keys(p)                       # вставка записи в каталог сдвигает номера: запоминаем, кто на что ссылался
    d.items[d.items.index(blocks[slot])] = new
    _ensure_catalog(p, tpl)
    snap.append((new, _cat_key(clone(tpl['catalog']))[0]))
    _apply_catalog_indices(p, snap)
    return True


def slot_state_block(p, track, slot):
    """Блок 0x1038 (состояние плагина) в слоте инсерта дорожки или None."""
    tb = TS.track_blocks(p)[track]
    d = TS.kids(TS.kids(tb, 0x261b)[0], 0x2627)[0]
    blocks = [x for x in d.items if isinstance(x, Block)]
    pb = _plugin_block(blocks[slot]) if slot < len(blocks) else None
    if pb is None:
        return None
    try:
        h = TS.kids(TS.kids(pb, 0x2615)[0], 0x2613)[0]
        return TS.kids(h, 0x1038)[0]
    except IndexError:
        return None


def apply_reaper_state(p, track, slot, wrapper, vst_file, plugname):
    """Переносит состояние VST3 Reaper в только что вставленный плагин (блок 0x1038 слота). True — перенесено."""
    b = slot_state_block(p, track, slot)
    if b is None or any(isinstance(x, Block) for x in b.items):
        return False
    import ptx_plugin_state as PS
    tpl = bytes(TS.own_bytes(b))
    new = PS.blob_from_reaper(tpl, wrapper, vst_file, plugname)
    if new is None:
        return False
    b.items = [bytes(new)]
    return True


def _slot_catalog_keys(p):
    """[(блок 0x2616, ключ записи каталога)] по текущим номерам 0x2615 own[2:4] всех занятых слотов."""
    from ptx_build import find_root
    ents = TS.kids(find_root(p, 0x1018), 0x1017)
    out = []
    for tb in TS.track_blocks(p):
        b = TS.kids(tb, 0x261b)
        d = TS.kids(b[0], 0x2627) if b else []
        if not d:
            continue
        for x in (_plugin_block(i) for i in d[0].items if isinstance(i, Block)):
            if x is not None and TS.kids(x, 0x2615):
                idx = struct.unpack_from('<H', TS.own_bytes(TS.kids(x, 0x2615)[0]), 2)[0]
                if idx < len(ents):
                    out.append((x, _cat_key(ents[idx])[0]))
    return out


def _apply_catalog_indices(p, pairs):
    from ptx_build import find_root
    ents = TS.kids(find_root(p, 0x1018), 0x1017)
    keys = [_cat_key(e)[0] for e in ents]
    for blk, ck in pairs:
        if ck in keys:
            TS.own_patch(TS.kids(blk, 0x2615)[0], 2, struct.pack('<H', keys.index(ck)))


def _name_of(e):
    o = TS.own_bytes(e)
    n = struct.unpack_from('<I', o, 3)[0]
    return o[7:7 + n].decode('utf-8', 'replace')


def _cat_key(e):
    """(FourCC-ключ, число каналов на входе) записи каталога 0x1017."""
    o = TS.own_bytes(e)
    n = struct.unpack_from('<I', o, 3)[0]
    f = o[7 + n:7 + n + 12]
    return tuple(bytes(f[i:i + 4][::-1]) for i in (0, 4, 8)), o[7 + n + 12]


def _sort_key(e):
    return _name_of(e).lower(), _cat_key(e)[1]


def _ensure_catalog(p, tpl):
    """Запись плагина (вариант каналов) в каталоге сессии 0x1018; список отсортирован по имени и числу каналов."""
    from ptx_build import clone, find_root
    r = find_root(p, 0x1018)
    ents = TS.kids(r, 0x1017)
    new = clone(tpl['catalog'])
    if any(_cat_key(e)[0] == _cat_key(new)[0] for e in ents):
        return False
    pos = next((e for e in ents if _sort_key(e) > _sort_key(new)), None)
    r.items.insert(r.items.index(pos) if pos is not None else r.items.index(ents[-1]) + 1, new)
    TS.own_patch(r, 2, struct.pack('<I', len(ents) + 1))
    return True


# ------------------------------------------------------------------------------- автоматизация параметров плагина

MAX_PARAM_TIME = 0x7FFFFFFF         # время точки — u32 little-endian, сэмплы (старший байт у служебных записей Pro Tools — метка 04)


def _param_value_block(initial, points, discrete=False):
    """0x260b (значение + кривая): заголовок 26 B | начальное значение 4 B | (n-1) точек [время 3B BE | 01 | значение 4 B] | 00 00.
    Непрерывный параметр — float32, дискретный — u32 0/1. points = [(сэмпл, значение)] по возрастанию времени."""
    def val(v):
        return struct.pack('<I', 1 if v >= 0.5 else 0) if discrete else struct.pack('<f', v)
    n = 1 + len(points)
    body = bytes([0x01, 0x46, 0x01, 0x00])
    size_after_ct = 26 + 4 + 8 * len(points) + 2
    body += struct.pack('<I', size_after_ct - 10) + b'\0\0' + struct.pack('<I', n) + b'\x04\x00' + struct.pack('<H', len(points)) + bytes(8)
    body += val(initial)
    for t, v in points:
        body += struct.pack('<I', t) + val(v)
    return b'\x0b\x26' + body + b'\0\0'


def _plugin_slot_block(p, track, slot):
    tb = TS.track_blocks(p)[track]
    d = TS.kids(TS.kids(tb, 0x261b)[0], 0x2627)[0]
    blocks = [x for x in d.items if isinstance(x, Block)]
    if slot >= len(blocks):
        return None
    return _plugin_block(blocks[slot])


def _entry_name(e):
    o = TS.own_bytes(e)
    n = struct.unpack_from('<I', o, 3)[0]
    return o[7:7 + n].decode('utf-8', 'replace')


def _entry_aid(e):
    """Внутренний ID параметра AAX из записи 0x260f (последнее поле: «2», «aa70», «MasterBypassID») или ''."""
    o = TS.own_bytes(e)
    try:
        n = struct.unpack_from('<I', o, 3)[0]
        q = 7 + n + 12
        m = struct.unpack_from('<I', o, q)[0]
        return o[q + 4:q + 4 + m].decode('latin-1')
    except struct.error:
        return ''


def _entry_is(e, want, vst3_id):
    """Запись — нужный параметр: по ID VST3 (через ID AAX), иначе по имени (без регистра и пробелов)."""
    if vst3_id is not None:
        import fx_state
        if str(vst3_id) in fx_state.aax_id_keys(_entry_aid(e)):
            return True
    return ''.join(c for c in _entry_name(e).lower() if c.isalnum()) == want


def _ensure_param_entry(p, track, slot, lst, want, vst3_id=None):
    """Параметра want нет в списке слота (шаблон без списка, слот по кэшу PT) — дописывает его запись из библиотеки ptx_templates_params
    (номер и ID параметра AAX, снятые с сессий Pro Tools; придуманные Pro Tools не принимает)."""
    if any(_entry_is(e, want, vst3_id) for e in TS.kids(lst, 0x260f)):
        return
    sb = slot_state_block(p, track, slot)
    try:
        from ptx_templates_params import PARAMS
    except ImportError:
        return
    z = PARAMS.get(tuple(bytes(TS.own_bytes(sb))[i:i + 4] for i in (18, 22, 26))) if sb is not None else None
    if not z:
        return
    raw, pos = zlib.decompress(z), 0
    from ptx_model import parse
    while pos < len(raw):
        b = parse(raw, pos, len(raw))
        if b is None:
            return
        pos += b.total()
        if b.ctype == 0x260f and _entry_is(b, want, vst3_id):
            import ptx_build
            ptx_build._reset_orig(b)
            lst.items.append(b)
            lst.items[0] = lst.items[0][:2] + struct.pack('<I', len(TS.kids(lst, 0x260f))) + lst.items[0][6:]
            return


def trim_param_lists(p):
    """Оставляет в списках параметров слотов только параметры с кривыми автоматизации. Pro Tools применяет значения из списка
    поверх состояния плагина (проверено в PT 26.4.1: чужой список Pro-Q 3 перебил 19 параметров перенесённого состояния),
    а значения в списках шаблонов — от сессии, с которой шаблон снят. Список только с автоматизированными параметрами PT принимает."""
    for t in range(len(TS.track_blocks(p))):
        k = 0
        while True:
            try:
                blk = _plugin_slot_block(p, t, k)
            except IndexError:                                 # дорожка без инсертов (папка и т. п.)
                break
            if blk is None:
                break
            k += 1
            for lst in TS.kids(blk, 0x2614):
                keep = {c['name'] for c in _read_curves(blk)}
                ents = [e for e in TS.kids(lst, 0x260f) if _entry_name(e) in keep]
                head = next((i for i in lst.items if not isinstance(i, Block)), b'\x14\x26' + bytes(4))
                lst.items[:] = [bytes(head[:2]) + struct.pack('<I', len(ents)) + bytes(head[6:])] + ents


def set_param_curve(p, track, slot, param_name, points, vst3_id=None):
    """Записывает кривую автоматизации параметра param_name плагина в слоте slot дорожки track.
    points — [(сэмпл, значение 0..1)]. Возвращает None при успехе, иначе строку с причиной (параметра нет в списке
    автоматизируемых параметров плагина / время за пределами 3 байт)."""
    blk = _plugin_slot_block(p, track, slot)
    if blk is None:
        return 'no plug-in in the slot'
    lst = TS.kids(blk, 0x2614)
    if not lst:
        return 'plug-in has no automatable parameter list'
    want = ''.join(c for c in param_name.lower() if c.isalnum())
    _ensure_param_entry(p, track, slot, lst[0], want, vst3_id)
    ents = TS.kids(lst[0], 0x260f)
    hit = [d for d in ents if vst3_id is not None and _entry_is(d, '\0', vst3_id)] or [d for d in ents if _entry_is(d, want, None)]
    for d in hit[:1]:
        vb = TS.kids(d, 0x260b)
        if not vb:
            return 'parameter has no value block'
        old = TS.own_bytes(vb[0])
        discrete = struct.unpack_from('<I', old, 28)[0] <= 1                     # шаблонное значение u32 0/1 — дискретный параметр
        if any(t > MAX_PARAM_TIME for t, _ in points):
            return 'time beyond 3-byte limit (349 s)'
        pts = sorted(points)
        initial = pts[0][1]
        rest = pts[1:] if pts[0][0] <= 0 else pts
        vb[0].items[:] = [_param_value_block(initial, rest, discrete)]
        return None
    return f'parameter {param_name!r} is not in the plug-in automation list'
