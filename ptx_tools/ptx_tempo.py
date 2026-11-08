#!/usr/bin/env python3
"""ptx_tempo.py — темп сессии Pro Tools (ПОСТОЯННЫЙ): корень 0x2718 > 0x2028 («Tempo»).

Формат (PT 26.4, сверено по 73 сессиям с одной записью и 5 значениями темпа 113/120/123/129/149):
  28 20 | 'Tempo' | 02 00 | u32 размер (65 = 4 + 61) | u32 число записей (1) | ... 'Const' ... 'TMS' ... | запись: ticks(5) 00 10 a5 d4 e8 = 1e12 + позиция | 5 байт | double BPM (+10) | ... | double частота дискретизации в конце.
Запись одна (темп с начала сессии). Раскладка с НЕСКОЛЬКИМИ записями (изменения темпа, рампа) не проверена — нужен образец; тогда читаем/пишем только темп начала.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ptx_model import Block  # noqa: E402
import ptx_trackstate as TS  # noqa: E402

TICKS5 = bytes.fromhex('0010a5d4e8')


def _blocks(p):
    """Блоки «Tempo» 0x2028: корневой и копия в 0x2718 (обе хранят темп; правим оба)."""
    out = [x for x in p.roots if isinstance(x, Block) and x.ctype == 0x2028]
    for r in p.roots:
        if isinstance(r, Block) and r.ctype == 0x2718:
            out += [k for k in r.items if isinstance(k, Block) and k.ctype == 0x2028]
    return out


def _block(p):
    b = _blocks(p)
    return b[0] if b else None


def read_bpm(p):
    """Темп сессии в BPM (по первой записи) или None; second value — число записей в заголовке."""
    d = _block(p)
    if d is None:
        return None, 0
    o = TS.own_bytes(d)
    if o[2:7] != b'Tempo' or len(o) < 80:
        return None, 0
    n = struct.unpack_from('<I', o, 13)[0]
    i = o.find(TICKS5, 40)
    if i < 0 or i + 18 > len(o):
        return None, n
    bpm = struct.unpack_from('<d', o, i + 10)[0]
    return (bpm if 10 <= bpm <= 1000 else None), n


def write_bpm(p, bpm):
    """Записывает постоянный темп (сессия с одной записью). True — записано."""
    ok = False
    for d in _blocks(p):
        o = TS.own_bytes(d)
        n = struct.unpack_from('<I', o, 13)[0] if o[2:7] == b'Tempo' and len(o) >= 17 else 0
        i = o.find(TICKS5, 40)
        if n != 1 or i < 0 or not 10 <= bpm <= 1000:
            continue
        TS.own_patch(d, i + 10, struct.pack('<d', float(bpm)))
        ok = True
    return ok
