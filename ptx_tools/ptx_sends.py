#!/usr/bin/env python3
"""ptx_sends.py — sends и автоматизация громкости в .ptx (чтение).

Найдено по эталонам lab/sends (snd0/snd1/snd2/aut1) и сессии пользователя:

  контейнер дорожки 0x261c / 0x261e > 0x261b содержит 0x260d по числу «слотов»: слот 0 — основной выход и фейдер,
  слоты 1..10 — sends A..J (пустой слот: 0x260d без детей-блоков 0x1029 / 0x260e).
  0x260d > 0x1029   : как у фейдера — громкость int32 в десятых дБ own[3] (и копия own[89]; -1440 = -inf), mute own[7],
                      панорама int32 own[15] (-100..100; стерео: L [15], R [43])
  0x260d > 0x260e   : путь-назначение: u16 id пути own[2:4] (= id входа aux-дорожки, own[3:5] её 0x261b), имя пути
  0x260d > 0x260a   : 1-й — громкость (копия int16 own[28]), 2-й — mute; когда на параметре есть автоматизация, блок длиннее:
                      own[12:16] u32 = число точек, own[24 + 6k] u32 = время в сэмплах, own[28 + 6k] int16 = значение
                      (для громкости десятые дБ); k-я точка 6 байт, в конце 2 нулевых байта.
  вход дорожки      : 0x261b own[3:5] u16 (id пути, 0xffff — нет входа)
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ptx_model import Block  # noqa: E402
import ptx_trackstate as TS  # noqa: E402

NEG_INF = -1440


def _slots(track_block):
    b261b = TS.kids(track_block, 0x261b)
    if not b261b:
        return None, []
    return b261b[0], TS.kids(b261b[0], 0x260d)


def _auto_points(a260a):
    """[(сэмпл, десятые дБ)] из длинного блока 0x260a; None, если автоматизации нет."""
    o = TS.own_bytes(a260a)
    if len(o) <= 32:
        return None
    n = struct.unpack_from('<I', o, 12)[0]
    if 24 + 6 * n > len(o):
        return None
    return [struct.unpack_from('<Ih', o, 24 + 6 * k) for k in range(n)]


def _slot_params(d):
    """Блоки, где слот хранит автоматизируемые параметры: (громкость, mute, пан) — 0x260a #0, #1 и 0x260a первого заполненного 0x260c."""
    a = TS.kids(d, 0x260a)
    vol = a[0] if a else None
    mute = a[1] if len(a) > 1 else None
    pans = []
    for h in TS.kids(d, 0x260c):
        k = TS.kids(h, 0x260a)
        if k:
            pans.append(k[0])                                # пан канала: моно — один, стерео — L и R
    return vol, mute, pans


def _db(v):
    return None if v <= NEG_INF else v / 10.0


def _pan(o):
    return struct.unpack_from('<i', o, 15)[0]


def read_routing(p):
    """По дорожкам (в порядке track_blocks): {'input': id пути | None, 'auto': [(сэмпл, дБ|None)] | None,
    'sends': [{'dest_path', 'level_db', 'pan', 'mute', 'auto'}]}"""
    out = []
    for tb in TS.track_blocks(p):
        info = {'input': None, 'auto': None, 'auto_mute': None, 'auto_pan': None, 'auto_pan_r': None, 'sends': []}
        b261b, slots = _slots(tb)
        e485 = []
        if b261b is not None and TS.kids(b261b, 0x4856):
            e485 = [TS.own_bytes(e) for e in TS.kids(TS.kids(b261b, 0x4856)[0], 0x485e)]
        if b261b is None:
            out.append(info)
            continue
        o = TS.own_bytes(b261b)
        inp = struct.unpack_from('<H', o, 3)[0] if len(o) > 5 else 0xFFFF
        info['input'] = None if inp == 0xFFFF else inp
        for si, d in enumerate(slots):
            f1029 = TS.kids(d, 0x1029)
            a260a = TS.kids(d, 0x260a)
            if not f1029:
                continue
            fo = TS.own_bytes(f1029[0])
            pv, pm, pans = _slot_params(d)
            pp = pans[0] if pans else None
            ppr = pans[1] if len(pans) > 1 else None
            auto = _auto_points(pv) if pv is not None else None
            if auto is not None:
                auto = [(t, _db(v)) for t, v in auto]
            amute = _auto_points(pm) if pm is not None else None
            apan = _auto_points(pp) if pp is not None else None
            apan_r = _auto_points(ppr) if ppr is not None else None
            if si == 0:
                info['auto'], info['auto_mute'], info['auto_pan'], info['auto_pan_r'] = auto, amute, apan, apan_r
                continue
            dest = TS.kids(d, 0x260e)
            if not dest:
                continue
            k = len(info['sends']) + 1                       # k-я запись списка выходов (0 — основной выход)
            pre = fo[2] == 0                                # send pre-fader: байт own[2] блока 0x1029 слота = 0 (и байт в 0x485e)
            info['sends'].append({'pre': pre, 'dest_path': struct.unpack_from('<H', TS.own_bytes(dest[0]), 2)[0],
                                  'level_db': _db(struct.unpack_from('<i', fo, 3)[0]), 'pan': _pan(fo),
                                  'pan_r': struct.unpack_from('<i', fo, 43)[0],
                                  'mute': bool(fo[7]), 'auto': auto, 'auto_mute': amute, 'auto_pan': apan, 'auto_pan_r': apan_r})
        out.append(info)
    return out


if __name__ == '__main__':
    from ptx_model import PTX
    pp = PTX(open(sys.argv[1], 'rb').read())
    names = TS.aggregate_names(pp)
    for n, r in zip(names, read_routing(pp)):
        if r['sends'] or r['auto']:
            print(n[0], r)


# ------------------------------------------------------------------------------------------------------ запись

def _set_points(a, pts):
    """Записывает список точек [(сэмпл, int16)] в блок параметра 0x260a (формат — в docstring модуля)."""
    pts = sorted(pts)
    if not pts or pts[0][0] != 0:
        pts.insert(0, (0, pts[0][1] if pts else 0))
    o = bytearray(TS.own_bytes(a)[:24])
    n = len(pts)
    struct.pack_into('<I', o, 6, 24 + 6 * n + 2 - 12)
    struct.pack_into('<I', o, 12, n)
    struct.pack_into('<H', o, 16, 2)
    struct.pack_into('<H', o, 18, n - 1)
    for t, v in pts:
        o += struct.pack('<Ih', t, v)
    o += b'\0\0'
    a.items = [bytes(o)]
    return pts


def write_auto(p, track, points, slot=0, param='vol'):
    """Автоматизация параметра слота (0 — фейдер дорожки, 1.. — sends) дорожки track.
    param 'vol': points = [(сэмпл, дБ | None)]; 'mute': [(сэмпл, 0 | 1)] (1 — замьючено); 'pan': [(сэмпл, -100..100)].
    Первая точка должна быть в 0; недостающую добавляем со значением первой."""
    tb = TS.track_blocks(p)[track]
    _, slots = _slots(tb)
    d = slots[slot]
    pv, pm, pans = _slot_params(d)
    if param == 'vol':
        pts = [(int(t), NEG_INF if v is None else max(NEG_INF, min(120, int(round(v * 10))))) for t, v in points]
        pts = _set_points(pv, pts)
        v0 = pts[0][1]
        f = TS.kids(d, 0x1029)[0]
        for off in (3, 89):
            TS.own_patch(f, off, struct.pack('<i', v0))
    elif param == 'mute':
        _set_points(pm, [(int(t), 1 if v else 0) for t, v in points])
    elif param in ('pan', 'pan_r'):                              # pan_r — правый канал стерео-дорожки
        idx = 1 if param == 'pan_r' else 0
        if len(pans) <= idx:
            return False
        _set_points(pans[idx], [(int(t), max(-100, min(100, int(round(v))))) for t, v in points])
    return True


# ---------------------------------------------------------------------------------- шины, aux-вход, sends (запись)

def _lazy():
    import ptx_structure as ST
    import ptx_templates_sends as TP
    from ptx_build import clone, find_root
    return ST, TP, clone, find_root


def _pstr(s):
    b = s.encode('utf-8')
    return struct.pack('<I', len(b)) + b


def _name_end(o):
    return 8 + struct.unpack_from('<I', o, 4)[0]


def _walk(b):
    for x in b.items:
        if isinstance(x, Block):
            yield x
            yield from _walk(x)


def _next_channel(p):
    _, _, _, find_root = _lazy()
    mx = 0
    r = find_root(p, 0x2603)
    for e in TS.kids(r, 0x2602):
        o = TS.own_bytes(e)
        k = _name_end(o)
        cnt = struct.unpack_from('<I', o, k)[0]
        for c in struct.unpack_from('<%dH' % cnt, o, k + 4):
            mx = max(mx, c)
    return mx + 1


def _fill_bus(b, kind, name, id8, chans):
    """Подставляет имя/id/каналы шины в клон шаблона (запись 0x1021 или 0x2602)."""
    ST, TP, _, _ = _lazy()
    ST.replace_raw(b, _pstr(TP.BUS_NAME[kind]), _pstr(name))
    ST.replace_raw(b, TP.BUS_ID8[kind], id8)
    if kind == 'mono':
        ST.replace_raw(b, b'\x01\x00\x00\x00\x0d\x00\xff\xff', b'\x01\x00\x00\x00' + struct.pack('<H', chans[0]) + b'\xff\xff')
        ST.replace_raw(b, b'\x0b\x00\xfe\xff', struct.pack('<H', chans[0]) + b'\xfe\xff')
    else:
        for tail in (b'\xff\xff', b'\xfe\xff'):
            ST.replace_raw(b, b'\x0c\x00\x0d\x00' + tail, struct.pack('<HH', *chans) + tail)


def add_bus(p, name, channels=2, rnd=None):
    """Добавляет шину в I/O Setup (обе половины списка 0x1022) и в список 0x2603; возвращает (индекс пути, id8, kind)."""
    import random
    ST, TP, clone, find_root = _lazy()
    rnd = rnd or random.SystemRandom()
    kind = 'mono' if channels == 1 else 'stereo'
    id8 = bytes(rnd.randrange(256) for _ in range(8))
    first = _next_channel(p)
    chans = (first,) if kind == 'mono' else (first, first + 1)
    r = find_root(p, 0x1022)
    ents = [k for k in r.items if isinstance(k, Block)]
    half = len(ents) // 2

    def last_bus(lo, hi):
        return max(i for i in range(lo, hi) if TS.own_bytes(ents[i])[2] == 2)
    pos1, pos2 = last_bus(0, half) + 1, last_bus(half, len(ents)) + 1
    for e in ents:                                       # родитель одноканальных записей сдвигается, если он за точкой вставки
        o = TS.own_bytes(e)
        if o[3] != 0 or o[2] == 2:
            continue
        k = _name_end(o)
        if struct.unpack_from('<I', o, k)[0] != 1:
            continue
        par = struct.unpack_from('<H', o, k + 6)[0]
        if par != 0xFFFF and par >= pos1:
            TS.own_patch(e, k + 6, struct.pack('<H', par + 1))
    for pos in (pos2, pos1):                             # сначала вторая половина — позиции первой не меняются
        b = clone(TP.BUS_ENTRY[kind])
        _fill_bus(b, kind, name, id8, chans)
        r.items.insert(r.items.index(ents[pos]), b)
    TS.own_patch(r, 2, struct.pack('<I', half + 1))
    TS.own_patch(r, 10, struct.pack('<I', half + 1))
    r2603 = find_root(p, 0x2603)
    rec = clone(TP.REC2602[kind])
    _fill_bus(rec, kind, name, id8, chans)
    last = TS.kids(r2603, 0x2602)[-1]
    r2603.items.insert(r2603.items.index(last) + 1, rec)
    TS.own_patch(r2603, 2, struct.pack('<I', len(TS.kids(r2603, 0x2602))))
    return pos1, id8, kind


def set_aux_input(p, track, path_idx, name, id8):
    """Вход aux-дорожки = шина (индекс пути path_idx)."""
    tb = TS.track_blocks(p)[track]
    b261b = TS.kids(tb, 0x261b)[0]
    o = TS.own_bytes(b261b)
    TS.own_patch(b261b, 3, struct.pack('<H', path_idx))
    # область между списком идентификаторов и маркером ffffffff00ff7f: [00][pstr имя][00000000][2a000000 id8][хвост 7 байт]
    end = o.index(b'\xff\xff\xff\xff\x00\xff\x7f')
    start = o.index(b'\x00\x00\x00\x00\x08\x00\x00\x00', 40) + 8
    payload = b'\x00' + _pstr(name) + b'\0' * 4 + b'\x2a\x00\x00\x00' + id8 + bytes.fromhex('00020000000000')
    acc = 0
    for k, x in enumerate(b261b.items):
        if not isinstance(x, (bytes, bytearray)):
            continue
        if acc <= start and end <= acc + len(x):
            x = bytes(x)
            b261b.items[k] = x[:start - acc] + payload + x[end - acc:]
            break
        acc += len(x)
    else:
        raise ValueError('aux input region spans several segments')
    for d in _walk(tb):
        if d.ctype == 0x2015:
            TS.own_patch(d, 5, b'\x01')
            break


MAX_SENDS = 10


def first_output(p):
    """Первый выходной стерео-путь ввода-вывода по умолчанию: (индекс пути, имя, id8). Запись 0x1021: own[2] — тип
    (0 вход, 1 выход, 2 шина, 3 insert), own[3] — 1 для стерео-пути верхнего уровня."""
    _, _, _, find_root = _lazy()
    r = find_root(p, 0x1022)
    ents = [k for k in r.items if isinstance(k, Block)]
    for i, b in enumerate(ents[:len(ents) // 2]):
        o = TS.own_bytes(b)
        if o[2] == 1 and o[3] == 1:
            n = struct.unpack_from('<I', o, 4)[0]
            k = o.find(b'\x2a\x00\x00\x00', 8 + n)
            return i, o[8:8 + n].decode('utf-8'), o[k + 4:k + 12]
    raise ValueError('no output path in the I/O setup')


def _replace_first(b, old, new, count):
    """Заменяет первые count вхождений old в сырых сегментах блока (по порядку)."""
    left = [count]

    def rec(x):
        for i, y in enumerate(x.items):
            if isinstance(y, (bytes, bytearray)):
                y = bytes(y)
                out, pos = b'', 0
                while left[0] and (j := y.find(old, pos)) >= 0:
                    out += y[pos:j] + new
                    pos = j + len(old)
                    left[0] -= 1
                if pos:
                    x.items[i] = out + y[pos:]
            elif isinstance(y, Block):
                rec(y)
    rec(b)


def main_output_bus(p, out_name):
    """Шина, питающая первый выход ввода-вывода (те же каналы, что у выхода), переименовывается в out_name — так Pro Tools
    показывает «Analog 1-2» (эталон: routing_after, выбор выхода у дорожки; шина шаблона `VOICES 2` -> `Analog 1-2`).
    Правятся: запись 0x1021 в обеих половинах (первые два вхождения имени), запись 0x2602 в 0x2603 и 0x2600 (тип 0x2f) с id
    выходного пути. Возвращает (индекс шины, имя, id8 шины) или None, если подходящей шины нет."""
    _, _, _, find_root = _lazy()
    r = find_root(p, 0x1022)
    ents = [k for k in r.items if isinstance(k, Block)]
    half = len(ents) // 2
    oi, oname, oid8 = first_output(p)
    oo = TS.own_bytes(ents[oi])
    k = _name_end(oo)
    cnt = struct.unpack_from('<I', oo, k)[0]
    pins = oo[k + 4:k + 4 + 2 * cnt]
    bi = None
    for i in range(half):
        o = TS.own_bytes(ents[i])
        if o[2] != 2 or o[3] != 1:
            continue
        kk = _name_end(o)
        if struct.unpack_from('<I', o, kk)[0] == cnt and o[kk + 4:kk + 4 + 2 * cnt] == pins:
            bi = i
            break
    if bi is None:
        return None
    ob = TS.own_bytes(ents[bi])
    old = ob[8:_name_end(ob)].decode('utf-8')
    kb = ob.find(b'\x2a\x00\x00\x00', _name_end(ob))
    bid8 = ob[kb + 4:kb + 12]
    if old == out_name:
        return bi, old, bid8
    for e in (ents[bi], ents[bi + half]):
        o = TS.own_bytes(e)
        _replace_first(e, _pstr(old), _pstr(out_name), 2)
        # длина имени в начале записи тоже входит в pstr; id пути не меняется
    r2603 = find_root(p, 0x2603)
    for rec in TS.kids(r2603, 0x2602):
        o = TS.own_bytes(rec)
        if o[8:_name_end(o)].decode('utf-8', 'replace') == old:
            _replace_first(rec, _pstr(old), _pstr(out_name), 1)
            for x in _walk(rec):
                if x.ctype == 0x2600 and TS.own_bytes(x)[2] == 0x2f:
                    _replace_first(x, _pstr(old), _pstr(out_name), 1)
                    xo = TS.own_bytes(x)
                    j = xo.find(b'\x2a\x00\x00\x00')
                    _replace_first(x, xo[j + 4:j + 12], oid8, 1)
            break
    return bi, out_name, bid8


def _patho_fields(o):
    """Смещения в «собственных» байтах объекта пути 0x260e: (id8, (offset имени, длина)). Раскладка: ctype(2) idx(2)
    `01 01 00 00 00 00` `2a000000` id8(8) 16 нулей, pstr имя, хвост."""
    k = o.index(b'\x2a\x00\x00\x00') + 4
    pos = k + 8 + 16
    n = struct.unpack_from('<I', o, pos)[0]
    if not 0 < n < 128:
        raise ValueError('path object name not found')
    return k, (pos, n)


def set_main_output(p, track, path_idx, name, id8):
    """Основной выход дорожки (слот 0 и список выходов 0x4856) = путь path_idx. Вызывать до add_send: sends перенумеровываются."""
    ST, TP, clone, _ = _lazy()
    tb = TS.track_blocks(p)[track]
    if not TS.kids(tb, 0x261b):                           # Basic Folder: у него нет своего выхода
        return
    b261b = TS.kids(tb, 0x261b)[0]
    slot0 = TS.kids(b261b, 0x260d)[0]
    old_po = TS.kids(slot0, 0x260e)[0]
    o = TS.own_bytes(old_po)
    k, (npos, nlen) = _patho_fields(o)
    old_id8, old_name = o[k:k + 8], o[npos + 4:npos + 4 + nlen]
    po = clone_item(old_po)
    ST.replace_raw(po, _pstr(old_name.decode('utf-8')), _pstr(name))
    ST.replace_raw(po, old_id8, id8)
    TS.own_patch(po, 2, struct.pack('<H', path_idx))
    slot0.items[slot0.items.index(old_po)] = po
    old4856 = TS.kids(b261b, 0x4856)[0]
    flags = [len(TS.own_bytes(e)) >= 8 and TS.own_bytes(e)[7] == 0 for e in TS.kids(old4856, 0x485e)[1:]]
    patho = [clone_item(x) for x in TS.kids(old4856, 0x260e)]
    patho[0] = clone_item(po)
    _rebuild_4856(b261b, patho, flags)


def _rebuild_4856(b261b, patho_blocks, pre_flags=()):
    """Список выходов дорожки 0x4856: основной выход + sends (по эталонам n = 1..3 в lab/sends)."""
    ST, TP, clone, _ = _lazy()
    old = TS.kids(b261b, 0x4856)[0]
    n = len(patho_blocks)
    fl = [1 if (k > 0 or n == 1) else 0 for k in range(n)]
    seg0 = bytearray(bytes.fromhex('5648000000000000000001000000000000000000000001000000ffffffff01000000'))
    struct.pack_into('<I', seg0, 22, n)
    items = [bytes(seg0)]
    for k, po in enumerate(patho_blocks):
        items.append(po)
        if k < n - 1:
            items.append(b'\x02\x00\x02\x00' + struct.pack('<I', fl[k]) + b'\x01\x00\x00\x00')
        else:
            items.append(b'\x02\x00\x02\x00' + struct.pack('<I', fl[k]) + b'\0' * 5 + struct.pack('<I', n) +
                         b'\xff\xff\xff\xff\x01\x00\x00\x00')
    for k in range(n):
        e = clone(TP.E485E_MAIN if k == 0 else TP.E485E_SEND)
        if k > 0 and k - 1 < len(pre_flags) and pre_flags[k - 1]:        # send pre-fader: байт own[7] = 0 (post-fader = 1)
            TS.own_patch(e, 7, b'\x00')
        items.append(e)
        if k < n - 1:
            items.append(struct.pack('<I', 0 if k == 0 else 1) + b'\x01\x00\x00\x00')
        else:
            items.append(b'\x01')
    old.items = items


def add_send(p, track, path_idx, name, id8, kind, level_db=0.0, mute=False, src_channels=1, pan=0.0, pre=False):
    """Новый send дорожки track на шину (path_idx, name, id8, kind 'mono' | 'stereo'). Слот — копия основного выхода."""
    ST, TP, clone, _ = _lazy()
    tb = TS.track_blocks(p)[track]
    b261b = TS.kids(tb, 0x261b)[0]
    slots = TS.kids(b261b, 0x260d)
    used = [d for d in slots if TS.kids(d, 0x260e)]
    k = len(used)
    if k >= len(slots):
        return 0
    tpl = TP.SLOT.get(('mono' if src_channels == 1 else 'stereo', kind))
    new = clone(tpl) if tpl is not None else clone_item(used[0])
    old_empty = slots[k]
    b261b.items[b261b.items.index(old_empty)] = new
    # путь-назначение
    po = clone(TP.PATHOBJ[kind])
    ST.replace_raw(po, _pstr(TP.BUS_NAME[kind]), _pstr(name))
    ST.replace_raw(po, TP.BUS_ID8[kind], id8)
    TS.own_patch(po, 2, struct.pack('<H', path_idx))
    old_po = TS.kids(new, 0x260e)[0]
    new.items[new.items.index(old_po)] = po
    # уровень, mute, заголовок слота
    v = NEG_INF if level_db is None or level_db <= -144 else max(NEG_INF, min(120, int(round(level_db * 10))))
    f = TS.kids(new, 0x1029)[0]
    for off in (3, 89):
        TS.own_patch(f, off, struct.pack('<i', v))
    a = TS.kids(new, 0x260a)
    TS.own_patch(a[0], 28, struct.pack('<h', v))
    if pan and src_channels == 1:                              # панорама send (моно-источник): int32 -100..100 @15 / @101
        pv = max(-100, min(100, int(round(pan))))
        for off in (15, 101):
            TS.own_patch(f, off, struct.pack('<i', pv))
        holders = [x for x in TS.kids(new, 0x260c) if TS.kids(x, 0x260a)]
        if holders:
            TS.own_patch(TS.kids(holders[0], 0x260a)[0], 28, struct.pack('<h', pv))
    if pre:                                                     # pre-fader: own[2] блока 0x1029 слота (post-fader = 1) + байт в 0x485e
        TS.own_patch(f, 2, b'\x00')
    if mute:
        for off in (7, 93):
            TS.own_patch(f, off, b'\x01')
        TS.own_patch(a[1], 28, b'\x01')
    # список выходов дорожки
    old4856 = TS.kids(b261b, 0x4856)[0]
    flags = [len(TS.own_bytes(e)) >= 8 and TS.own_bytes(e)[7] == 0 for e in TS.kids(old4856, 0x485e)[1:]]   # уже стоящие pre-fader
    patho = [clone_item(x) for x in TS.kids(old4856, 0x260e)]
    patho.append(clone_item(po))
    _rebuild_4856(b261b, patho, flags + [bool(pre)])
    return k


def clone_item(x):
    """Глубокая копия элемента блока (bytes или Block)."""
    from ptx_build import clone
    import ptx_to_reaper as R
    if isinstance(x, Block):
        return clone(R.flat([x]))
    return bytes(x)
