#!/usr/bin/env python3
"""ptx_trackstate.py — состояние дорожек в .ptx: mute, solo, цвет, inactive (чтение и запись).

Места найдены по эталонам Pro Tools 26.4 (lab/oracle8: o_mute, o_solo, o_color*, o_inactive; значения
подтверждены через PTSL GetTrackList). Дорожка i (по порядку) = i-й блок 0x261c в 0x2624 = i-й 0x251a в первой
семье 0x2519. Смещения — по «собственным» байтам блока (сырые сегменты подряд, ctype включён).

  mute     : 0x261c>0x261b>0x260d(1-й)>0x1029 own[7], own[93] и >0x260a(4-й блок-потомок) own[28]   0/1
  solo     : 0x261c>0x261b>0x102d own[2]                                                          0/1
  цвет     : 0x261c>0x200b>0x200a>0x2015 own[25:27] и 0x251a (обе семьи) own[name_end+51 : +53]
             u16 = позиция в палитре Pro Tools (24..95; 24, 48, 72 — дубли соседних); 0xfffe = цвет по умолчанию
  inactive : 0x251a (обе семьи) own[name_end+47] 1 -> 0  и 0x260e (2 штуки) own[5] 1 -> 0
  громкость: 0x261c>0x261b>0x260d(1-й)>0x1029 own[3:7] и own[89:93] — int32 в десятых долях дБ (-60 = -6.0 дБ; -1440 — минус бесконечность);
             копия int16 в own[28:30] третьего блока-потомка 0x260d (0x260a)
  панорама : моно — 0x1029 own[15:19] и [101:105] int32, -100 (<100) .. +100 (100>); копия int16 в 0x260a внутри 6-го потомка 0x260d
             (0x260c). Стерео: левый канал [15] / [101], правый [43] / [129] (по умолчанию -100 / +100), копия правого — в 7-м потомке.
             Поверхностная панорама 5.1 не разобрана.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ptx_model import Block  # noqa: E402

DEFAULT_COLOR_RAW = 0xFFFE
# палитра треков Pro Tools: u16 в файле -> RGB (снято с PT через PTSL GetTrackList, lab/rawcol)
PALETTE_ALL = {24: 0x2c00fc, 25: 0x2c00fc, 26: 0x5600fc, 27: 0x8800fc, 28: 0xbf00fc, 29: 0xbe00c0, 30: 0xbd0088, 31: 0xbd0054, 32: 0xbc000d, 33: 0xbd1e0d, 34: 0xbd520e, 35: 0xbe8911, 36: 0xc0c514, 37: 0x89c511, 38: 0x57c610, 39: 0x2ec60f, 40: 0x1cc60e, 41: 0x1ec654, 42: 0x20c488, 43: 0x23c3c1, 44: 0x27c1fd, 45: 0x2184fc, 46: 0x1c4afc, 47: 0x1900fc, 48: 0x1e00a3, 49: 0x1e00a3, 50: 0x3700a3, 51: 0x5500a3, 52: 0x7400a4, 53: 0x7c0089, 54: 0x7b0066, 55: 0x7a0046, 56: 0x7a000b, 57: 0x7a120b, 58: 0x7a310c, 59: 0x7b510d, 60: 0x898010, 61: 0x66800e, 62: 0x48800d, 63: 0x2d800c, 64: 0x18800c, 65: 0x158033, 66: 0x167f51, 67: 0x1a8c7e, 68: 0x1d8da4, 69: 0x1969a4, 70: 0x1646a3, 71: 0x1423a3, 72: 0x14005f, 73: 0x14005f, 74: 0x21005f, 75: 0x31005f, 76: 0x41005f, 77: 0x4b0057, 78: 0x470042, 79: 0x470031, 80: 0x47000b, 81: 0x470c0b, 82: 0x471c0b, 83: 0x472c0c, 84: 0x574d0f, 85: 0x424a0c, 86: 0x324a0c, 87: 0x234a0c, 88: 0x154b0b, 89: 0x0f4a1d, 90: 0x0f4a2c, 91: 0x14594c, 92: 0x16595f, 93: 0x14475f, 94: 0x13355f, 95: 0x11225f}
ALIASES = (24, 48, 72)                   # те же цвета, что у соседних позиций 25, 49, 73
PALETTE = {r: c for r, c in PALETTE_ALL.items() if r not in ALIASES}      # 69 различимых цветов


def own_bytes(b):
    return b''.join(x for x in b.items if isinstance(x, (bytes, bytearray)))


def own_patch(b, idx, data):
    """Меняет байты по смещению idx в «собственных» байтах блока (через сырые сегменты)."""
    pos = 0
    for k, x in enumerate(b.items):
        if isinstance(x, (bytes, bytearray)):
            if idx < pos + len(x):
                seg = bytearray(x)
                n = min(len(data), pos + len(x) - idx)
                seg[idx - pos:idx - pos + n] = data[:n]
                b.items[k] = bytes(seg)
                if n < len(data):
                    own_patch(b, idx + n, data[n:])
                return
            pos += len(x)
    raise IndexError(idx)


def own_splice(b, start, end, data):
    """Заменяет «собственные» байты блока [start, end) на data (длина может меняться); диапазон — в одном сыром сегменте."""
    pos = 0
    for k, x in enumerate(b.items):
        if isinstance(x, (bytes, bytearray)):
            if start >= pos and end <= pos + len(x):
                b.items[k] = bytes(x[:start - pos]) + bytes(data) + bytes(x[end - pos:])
                return
            pos += len(x)
    raise IndexError((start, end))


def kids(b, ctype):
    return [i for i in b.items if isinstance(i, Block) and i.ctype == ctype]


def root(p, ctype):
    for r in p.roots:
        if isinstance(r, Block) and r.ctype == ctype:
            return r
    return None


def descend(b, *chain):
    """Идёт по цепочке (ctype, индекс среди блок-потомков этого ctype)."""
    for ct, n in chain:
        b = kids(b, ct)[n]
    return b


def track_blocks(p):
    """Контейнеры всех дорожек сессии по порядку (аудио 0x261c, папки 0x263b, остальные типы 0x261e... )."""
    r = root(p, 0x2624)
    return [x for x in r.items if isinstance(x, Block)] if r is not None else []


def aggregate_names(p):
    """[(имя, блок 0x251a первой семьи, блок второй семьи)] в порядке дорожек."""
    r = root(p, 0x2519)
    blocks = kids(r, 0x251a) if r is not None else []
    n = len(track_blocks(p))                    # 0x251a идут двумя семьями по n штук (n = число контейнеров дорожек)
    out = []
    for i in range(n if len(blocks) >= 2 * n else 0):
        o = blocks[i].items[0]
        ln = struct.unpack_from('<I', o, 4)[0]
        out.append((o[8:8 + ln].decode('utf-8', 'replace'), blocks[i], blocks[i + n], 8 + ln))
    return out


def track_id(a251a, name_end):
    """8 байт уникального id дорожки (хвост GUID {00000000-2a000000-XXXXXXXX-XXXXXXXX})."""
    return own_bytes(a251a)[name_end + 10:name_end + 18]


def color_from_raw(raw):
    return PALETTE_ALL.get(raw)


def read_states(p):
    """[{'name', 'mute', 'solo', 'hidden', 'frozen', 'inactive', 'color': 0xRRGGBB | None}] по дорожкам."""
    names = aggregate_names(p)
    out = []
    for i, tb in enumerate(track_blocks(p)):
        st = {'name': names[i][0] if i < len(names) else f'Track {i + 1}', 'mute': False, 'solo': False,
              'hidden': False, 'frozen': False, 'inactive': False, 'color': None, 'volume_db': None, 'pan': None}
        if i < len(names):                                  # цвет у всех типов дорожек — в 0x251a (name_end + 51), по таблице палитры
            a = own_bytes(names[i][1])
            tail = a[names[i][3]:]
            if len(tail) >= 4:
                raw = struct.unpack_from('<H', tail, 51 if len(tail) >= 53 else len(tail) - 2)[0]
                st['color_raw'] = raw
                st['color'] = color_from_raw(raw)
            if len(tail) >= 48:                             # name_end+21: 1 = Frozen; +46: 0 = Hidden (обе семьи 0x251a; проверено оракулом PTSL SetTrackHidden/FrozenState)
                st['frozen'] = bool(tail[21])
                st['hidden'] = tail[46] == 0
        if tb.ctype == 0x261e and i < len(names):           # aux / Routing Folder: только громкость и панорама
            a = own_bytes(names[i][1])
            st['channels'] = {0: 1, 1: 2, 6: 6}.get(a[names[i][3]], 2)
            try:
                st['volume_db'], st['pan'] = read_fader(p, i, st['channels'])
            except (IndexError, struct.error):
                pass
        if tb.ctype in (0x261e, 0x261d):                    # aux / Routing Folder / Master Fader: mute (те же поля, что у аудио)
            try:
                st['mute'] = bool(own_bytes(descend(kids(tb, 0x261b)[0], (0x260d, 0), (0x1029, 0)))[7])
            except (IndexError, struct.error):
                pass
        if tb.ctype == 0x261d:                              # Master Fader: стерео фейдер
            st['channels'] = 2
            try:
                st['volume_db'], st['pan'] = read_fader(p, i, 2)
            except (IndexError, struct.error):
                pass
        if tb.ctype != 0x261c:                              # папки и прочие типы: остальное состояние не читаем
            out.append(st)
            continue
        try:
            b261b = kids(tb, 0x261b)[0]
            st['mute'] = bool(own_bytes(descend(b261b, (0x260d, 0), (0x1029, 0)))[7])
            st['solo'] = bool(own_bytes(descend(b261b, (0x102d, 0)))[2])
            st['rec'] = bool(own_bytes(descend(tb, (0x200b, 0), (0x200a, 0), (0x2015, 0)))[4])
            if i < len(names):
                a = own_bytes(names[i][1])
                st['inactive'] = a[names[i][3] + 47] == 0
                code = a[names[i][3]]
                st['channels'] = {0: 1, 1: 2, 6: 6}.get(code, 1)
                st['volume_db'], st['pan'] = read_fader(p, i, st['channels'])
        except (IndexError, struct.error):
            pass
        out.append(st)
    return out


def nearest_palette_index(rgb):
    r, g, b = (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255
    return min(PALETTE, key=lambda i: ((PALETTE[i] >> 16 & 255) - r) ** 2 + ((PALETTE[i] >> 8 & 255) - g) ** 2
               + ((PALETTE[i] & 255) - b) ** 2)


def write_inactive(p, track, inactive=True):
    """Дорожка track (индекс по порядку) неактивна (Inactive): 0x251a обеих семей own[name_end+47], 0x261b own[5] и own[29],
    0x260e (два блока в 0x261b: через 0x260d и через 0x4856) own[5]. 1 — активна, 0 — неактивна (эталон: PTSL
    SetTrackInactiveState + SaveSessionAs, сравнение ptx_diff; индексы с ctype)."""
    v = bytes([0 if inactive else 1])
    _, a1, a2, end = aggregate_names(p)[track]
    own_patch(a1, end + 47, v)
    own_patch(a2, end + 47, v)
    tb = track_blocks(p)[track]
    b261b = descend(tb, (0x261b, 0))
    own_patch(b261b, 5, v)                                   # 0x261b own[3] и own[27] без ctype (эталон: сравнение двух сохранений PT)
    own_patch(b261b, 29, v)
    for container in (0x260d, 0x4856):
        for blk in kids(b261b, container):
            for b260e in kids(blk, 0x260e):
                own_patch(b260e, 5, v)


def write_state(p, track, mute=None, solo=None, color=None, volume_db=None, pan=None, rec=None):
    """Выставляет состояние дорожки track (индекс по порядку). color — 0xRRGGBB (берётся ближайший цвет палитры)."""
    tb = track_blocks(p)[track]
    if tb.ctype == 0x261d:                                  # Master Fader: фейдер, mute, цвет (в агрегате)
        if volume_db is not None or pan is not None:
            write_fader(p, track, volume_db, pan, 2)
        if mute is not None:
            big = descend(kids(tb, 0x261b)[0], (0x260d, 0))
            own_patch(descend(big, (0x1029, 0)), 7, bytes([1 if mute else 0]))
            own_patch(descend(big, (0x1029, 0)), 93, bytes([1 if mute else 0]))
            own_patch(kids(big, 0x260a)[1], 28, bytes([1 if mute else 0]))
        if color is not None:
            raw = struct.pack('<H', nearest_palette_index(color))
            _, a1, a2, end = aggregate_names(p)[track]
            own_patch(a1, end + 51, raw)
            own_patch(a2, end + 51, raw)
        return
    if tb.ctype != 0x261c:                                  # aux / Routing Folder: фейдер и цвет; папки: только цвет (mute/solo игнорируются)
        if tb.ctype == 0x261e and (volume_db is not None or pan is not None):
            code = own_bytes(aggregate_names(p)[track][1])[aggregate_names(p)[track][3]]
            write_fader(p, track, volume_db, pan, {0: 1, 1: 2, 6: 6}.get(code, 2))
        if tb.ctype == 0x261e and mute is not None:         # mute aux / Routing Folder — те же поля, что у Master Fader
            v = bytes([1 if mute else 0])
            big = descend(kids(tb, 0x261b)[0], (0x260d, 0))
            own_patch(descend(big, (0x1029, 0)), 7, v)
            own_patch(descend(big, (0x1029, 0)), 93, v)
            own_patch(kids(big, 0x260a)[1], 28, v)
        if color is not None:
            raw = struct.pack('<H', nearest_palette_index(color))
            _, a1, a2, end = aggregate_names(p)[track]
            own_patch(a1, end + 51, raw)
            own_patch(a2, end + 51, raw)
        return
    if volume_db is not None or pan is not None:
        code = own_bytes(aggregate_names(p)[track][1])[aggregate_names(p)[track][3]]
        write_fader(p, track, volume_db, pan, {0: 1, 1: 2, 6: 6}.get(code, 1))
    b261b = kids(tb, 0x261b)[0]
    if mute is not None:
        v = bytes([1 if mute else 0])
        big = descend(b261b, (0x260d, 0))
        own_patch(descend(big, (0x1029, 0)), 7, v)
        own_patch(descend(big, (0x1029, 0)), 93, v)
        own_patch(kids(big, 0x260a)[1], 28, v)
    if solo is not None:
        own_patch(descend(b261b, (0x102d, 0)), 2, bytes([1 if solo else 0]))
    if rec is not None:                                     # запись (record enable): 0x200b>0x200a>0x2015 own[4] (снято PTSL SetTrackRecordEnableState)
        own_patch(descend(tb, (0x200b, 0), (0x200a, 0), (0x2015, 0)), 4, bytes([1 if rec else 0]))
    if color is not None:
        raw = struct.pack('<H', nearest_palette_index(color))
        own_patch(descend(tb, (0x200b, 0), (0x200a, 0), (0x2015, 0)), 25, raw)
        _, a1, a2, end = aggregate_names(p)[track]
        own_patch(a1, end + 51, raw)
        own_patch(a2, end + 51, raw)


# ---------------------------------------------------------------------------------------- громкость / панорама

def _fader_blocks(tb):
    b261b = kids(tb, 0x261b)[0]
    d = kids(b261b, 0x260d)[0]
    ch = [x for x in d.items if isinstance(x, Block)]
    return kids(d, 0x1029)[0], ch


def read_fader(p, track, channels):
    """(громкость в дБ | None, панорама) дорожки track. Панорама: моно — число -100..100, стерео — (L, R), 5.1 — None."""
    tb = track_blocks(p)[track]
    f1029, _ = _fader_blocks(tb)
    o = own_bytes(f1029)
    vol = struct.unpack_from('<i', o, 3)[0]
    vdb = None if vol <= -1440 else vol / 10.0
    if channels == 1:
        pan = struct.unpack_from('<i', o, 15)[0]
    elif channels == 2:
        pan = (struct.unpack_from('<i', o, 15)[0], struct.unpack_from('<i', o, 43)[0])
    else:
        pan = None
    return vdb, pan


def write_fader(p, track, volume_db=None, pan=None, channels=1):
    """Громкость (дБ; None — не менять, <= -144 — минус бесконечность) и панорама (моно: -100..100; стерео: (L, R))."""
    tb = track_blocks(p)[track]
    f1029, ch = _fader_blocks(tb)
    if volume_db is not None:
        v = -1440 if volume_db <= -144 else max(-1440, min(120, round(volume_db * 10)))
        for off in (3, 89):
            own_patch(f1029, off, struct.pack('<i', v))
        own_patch(kids(ch[2], 0x260a)[0] if ch[2].ctype != 0x260a else ch[2], 28, struct.pack('<h', v)) if False else own_patch(ch[2], 28, struct.pack('<h', v))
    if pan is None or channels > 2:
        return
    pans = [pan] if channels == 1 else list(pan)
    for k, pv in enumerate(pans):
        pv = max(-100, min(100, int(round(pv))))
        base = 15 + 28 * k
        for off in (base, base + 86):
            own_patch(f1029, off, struct.pack('<i', pv))
        holder = ch[5 + k]
        own_patch(kids(holder, 0x260a)[0], 28, struct.pack('<h', pv))


# ---------------------------------------------------------------------------------------- комментарий дорожки

def read_comment(p, track):
    """Комментарий дорожки (0x2015 own[10] — длина u32, дальше UTF-8); пустая строка, если комментария нет."""
    tb = track_blocks(p)[track]
    try:
        d = descend(tb, (0x200b, 0), (0x200a, 0), (0x2015, 0))
        o = own_bytes(d)
        n = struct.unpack_from('<I', o, 10)[0]
        return o[14:14 + n].decode('utf-8', 'replace') if 0 < n < 4096 else ''
    except (IndexError, struct.error, AttributeError):
        return ''


COMMENT_MAX = 1000                      # ограничение Pro Tools на длину комментария (PTSL: MaxCharactersComments)


def write_comment(p, track, text):
    """Комментарий дорожки: в блоке 0x2015 (0x200b>0x200a>0x2015) по own[10] — u32 длина + UTF-8 (перевод строки 0x0a).
    Вызывать после всех правок этого блока по смещениям (цвет, номер): текст сдвигает байты за ним."""
    tb = track_blocks(p)[track]
    d = None
    stack = [tb]
    while stack and d is None:                          # у аудио 0x200b>0x200a>0x2015, у aux / routing folder 0x200d>0x200a>0x2015
        cur = stack.pop(0)
        for x in cur.items:
            if isinstance(x, Block):
                if x.ctype == 0x2015:
                    d = x
                    break
                stack.append(x)
    if d is None:
        return False
    o = own_bytes(d)
    if len(o) < 14 or o[8] != 0x5c:
        return False
    old = struct.unpack_from('<I', o, 10)[0]
    data = text.replace('\r\n', '\n').replace('\r', '\n')[:COMMENT_MAX].encode('utf-8')
    own_splice(d, 10, 14 + old, struct.pack('<I', len(data)) + data)
    return True
