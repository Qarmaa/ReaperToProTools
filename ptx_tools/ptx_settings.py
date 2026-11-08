#!/usr/bin/env python3
"""ptx_settings.py — настройки сессии Pro Tools в .ptx: частота таймкода (fps).

Снято через PTSL (CId_SetSessionTimeCodeRate + SaveSessionAs, 11 значений, 2026-10-13): блок 0x204d (own[0] — класс, own[40] — код) и
0x204b (own[144] — код). Коды: 24:1, 25:2, 29.97:3, 29.97 drop:5, 30:7, 30 drop:8, 23.976:9, 50:10, 59.94:11, 60:13, 48:16;
у 0x204d own[0] для 48/50/59.94/60 равен классу базовой частоты 1/2/3/7.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ptx_trackstate as TS  # noqa: E402

FPS_CODES = {23.976: (9, 9), 24.0: (1, 1), 25.0: (2, 2), 29.97: (3, 3), 30.0: (7, 7), 48.0: (1, 16), 50.0: (2, 10),
             59.94: (3, 11), 60.0: (7, 13)}
FPS_DROP = {29.97: (5, 5), 30.0: (8, 8)}


def fps_codes(fps, drop=False):
    """(класс, код) для частоты кадров fps (ближайшая из поддерживаемых) или None."""
    table = FPS_DROP if drop and any(abs(fps - k) < 0.05 for k in FPS_DROP) else FPS_CODES
    best = min(table, key=lambda k: abs(k - fps))
    return table[best] if abs(best - fps) < 0.05 else None


def set_timecode_rate(p, fps, drop=False):
    """Выставляет частоту таймкода сессии. Возвращает True, если частота известна и записана."""
    codes = fps_codes(fps, drop)
    if codes is None:
        return False
    cls, code = codes
    r4d = TS.root(p, 0x204d)
    r4b = TS.root(p, 0x204b)
    if r4d is None or r4b is None:
        return False
    TS.own_patch(r4d, 2 + 0, bytes([cls]))
    TS.own_patch(r4d, 2 + 40, bytes([code]))
    TS.own_patch(r4b, 2 + 144, bytes([code]))
    return True


def set_session_start(p, frames):
    """Старт таймкода сессии в кадрах текущей частоты (0x204d own[9:12] и own[44:47], u24 little-endian; снято через
    PTSL SetSessionStartTime: 01:00:00:00 при 30 fps = 108000 = e0 a5 01)."""
    r = TS.root(p, 0x204d)
    if r is None or frames < 0 or frames > 0xFFFFFF:
        return False
    data = int(frames).to_bytes(3, 'little')
    TS.own_patch(r, 2 + 9, data)
    TS.own_patch(r, 2 + 44, data)
    return True


def read_timecode(p):
    """(fps, drop, старт в кадрах) сессии или None."""
    r = TS.root(p, 0x204d)
    r4b = TS.root(p, 0x204b)
    if r is None or r4b is None:
        return None
    o4b = TS.own_bytes(r4b)
    code = o4b[2 + 144] if len(o4b) > 2 + 144 else 0
    m = o4b.find(b'\x02\x01\x00\x32')                       # положение кода fps зависит от длины строк в начале блока: отсчёт от этой метки
    if m >= 0 and m + 25 < len(o4b) and o4b[m + 25] in (v[1] for v in FPS_CODES.values()):
        code = o4b[m + 25]
    inv = {v[1]: (k, False) for k, v in FPS_CODES.items()}
    inv.update({v[1]: (k, True) for k, v in FPS_DROP.items()})
    o4d = TS.own_bytes(r)
    code4d = struct.unpack_from('<I', o4d, 2)[0] if len(o4d) >= 6 else -1       # 0x204d own[2:6] — код частоты кадров (3 = 29.97, 9 = 23.976, 7 = 30 ...): надёжнее кода в 0x204b
    if code4d in inv:
        code = code4d
    if code not in inv:
        return None
    fps, drop = inv[code]
    return fps, drop, int.from_bytes(TS.own_bytes(r)[2 + 9:2 + 12], 'little')


# ----------------------------------------------------------------------------------------------- формат счётчиков

COUNTER_INDEX = {'BarsBeats': 0, 'MinSecs': 1, 'TimeCode': 2, 'FeetFrames': 3, 'Samples': 4}
REAPER_TO_COUNTER = {0: 'MinSecs', 1: 'BarsBeats', 2: 'BarsBeats', 3: 'MinSecs', 4: 'Samples', 5: 'TimeCode', 8: 'TimeCode'}
COUNTER_TO_REAPER = {'BarsBeats': 1, 'MinSecs': 0, 'TimeCode': 5, 'FeetFrames': 5, 'Samples': 4}


def _all_blocks(items, ctype):
    from ptx_model import Block
    for x in items:
        if isinstance(x, Block):
            if x.ctype == ctype:
                yield x
            yield from _all_blocks(x.items, ctype)


def set_counters(p, main=None, sub=None):
    """Формат основного / дополнительного счётчика (имена: BarsBeats, MinSecs, TimeCode, FeetFrames, Samples). Снято через PTSL
    SetMainCounterFormat / SetSubCounterFormat (25 сочетаний): основной — копии индекса в 0x2056 (own[21], 3 блока), 0x2024 own[1],
    0x2016>0x258b own[257], 0x2066 own[6], 0x2530 own[6]; дополнительный — 0x201f own[4] и 0x2016 own[97]."""
    ok = True
    if main in COUNTER_INDEX:
        v = bytes([COUNTER_INDEX[main]])
        for b in _all_blocks(p.roots, 0x2056):
            TS.own_patch(b, 2 + 21, v)
        for ct, off in ((0x2024, 1), (0x2066, 6), (0x2530, 6)):
            r = TS.root(p, ct)
            if r is None:
                ok = False
                continue
            TS.own_patch(r, 2 + off, v)
        r2016 = TS.root(p, 0x2016)
        for b in (_all_blocks(r2016.items, 0x258b) if r2016 is not None else ()):
            if len(TS.own_bytes(b)) > 2 + 257:
                TS.own_patch(b, 2 + 257, v)
                break
    if sub in COUNTER_INDEX:
        r = TS.root(p, 0x201f)
        if r is None:
            return False
        TS.own_patch(r, 2 + 4, bytes([COUNTER_INDEX[sub]]))
        r2016 = TS.root(p, 0x2016)
        if r2016 is None:
            return False
        TS.own_patch(r2016, 2 + 97, bytes([COUNTER_INDEX[sub]]))
    return ok


def read_counters(p):
    """(основной, дополнительный) счётчики сессии — имена из COUNTER_INDEX или None."""
    inv = {v: k for k, v in COUNTER_INDEX.items()}
    r24, r1f = TS.root(p, 0x2024), TS.root(p, 0x201f)
    try:
        return inv.get(TS.own_bytes(r24)[2 + 1]), inv.get(TS.own_bytes(r1f)[2 + 4])
    except (IndexError, AttributeError):
        return None, None


# ----------------------------------------------------------------------------------------------- частота сессии

SESSION_RATES = (44100, 48000, 88200, 96000)


def _blocks_by_path(p, path):
    """Блоки, у которых цепочка ctype от корня равна path, в порядке обхода в глубину (как у ptx_diff)."""
    from ptx_model import Block
    out = []

    def walk(items, depth):
        for b in items:
            if isinstance(b, Block):
                if b.ctype == path[depth]:
                    if depth == len(path) - 1:
                        out.append(b)
                    walk(b.items, depth + 1) if depth + 1 < len(path) else None
    walk(p.roots, 0)
    return out


def set_session_rate(p, rate):
    """Частота дискретизации сессии (44100 / 48000 / 88200 / 96000): значения полей сняты с сессий, созданных PTSL CreateSession
    на этих частотах (ptx_rate_table.RATE_TABLE: частота в 0x1028, 0x204b, а также зависящие от неё поля 0x2025, 0x2023, 0x2028,
    0x2718..0x271c и состояние окон). Возвращает True, если записано."""
    if rate not in SESSION_RATES:
        return False
    from ptx_rate_table import RATE_TABLE
    for path, idx, pos, vals in RATE_TABLE:
        blocks = _blocks_by_path(p, path)
        if idx >= len(blocks):
            continue
        data = vals[rate]
        for j, off in enumerate(pos):
            try:
                TS.own_patch(blocks[idx], 2 + off, data[j:j + 1])
            except IndexError:                                   # поле в сырых данных, разбитых вложенными блоками (0x2016>0x258b @204 — вид окна)
                pass
    return True


# ------------------------------------------------------------------------------------ сведения о приложении и сессии

def read_app_info(raw):
    """«Pro Tools Studio 2024.10.1 (MacOS)» из хвоста файла (строки приложения, версия, платформа) или None."""
    import re
    import ptx
    buf = ptx.crypt(raw)[-65536:]
    m = re.search(rb'(\d{4}\.\d{1,2}(?:\.\d{1,2})?)[\x00-\x20]{0,3}[\x00-\x20]*\x07\x00\x00\x00Release', buf)
    if not m:
        return None
    ver = m.group(1).decode()
    edition = None
    for e in re.finditer(rb'Pro Tools(?: [A-Za-z]+)?', buf[:m.start()]):
        edition = e.group().decode()
    plat = re.search(rb'Pro Tools Session File[\x00-\x20]*(MacOS|Windows)', buf)
    out = f'{edition or "Pro Tools"} {ver}'
    return out + (f' ({plat.group(1).decode()})' if plat else '')


def format_start(frames, fps):
    """Старт сессии в виде HH:MM:SS (с кадрами, если они не нулевые)."""
    f = int(round(fps))
    if f <= 0:
        return None
    sec, ff = divmod(int(frames), f)
    h, rem = divmod(sec, 3600)
    mi, ss = divmod(rem, 60)
    return f'{h:02d}:{mi:02d}:{ss:02d}' + (f':{ff:02d}' if ff else '')
