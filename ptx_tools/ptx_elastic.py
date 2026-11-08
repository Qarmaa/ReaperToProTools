#!/usr/bin/env python3
"""ptx_elastic.py — Elastic Audio (Polyphonic) для дорожек и растянутых клипов Pro Tools (.ptx).

Образцы Pro Tools 26.4: lab/oracle_elastic (mono: elastic_before/after; stereo: stereo_before/after).
Дорожка: плагин Elastic Audio (блок 0x252c «DigiPElasPoly… Polyphonic») + флаги в 0x261c и в агрегате 0x251a (name_end + 22).
Клип: у региона (0x2628) байт после `fe ff` становится `00 01 00 00` и добавляется дочерний блок 0x2636 (62 байта):
UUID16 | 18 байт заголовка | u64 начало в источнике | u64 начало клипа | u32 длина источника | u32 конец клипа (начало + длина на
таймлайне) | u32 0. Исходный файл не меняется: растяжка = отношение (конец - начало) / длина источника.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ptx_model import Block  # noqa: E402
import ptx_trackstate as TS  # noqa: E402

REGION_ELASTIC_ID = bytes.fromhex('f14ed6f00f49414381ed380b62c36b02')
REGION_ELASTIC_MID = bytes.fromhex('014601002600000000000200000008000100')
ELASTIC_PLUGIN_BLOB = bytes.fromhex(
    '02015a02006b0000002c2569676944506c6546796c6f505800000000000058ffffffff4469676946656c50506f6c7965'
    '6c636b436f6d706c65746520436f6e74726f6c7320537461746500000000000000000001010101645f6330303100003f'
    'a47ae147c5a1ca6c5f63303032000000000000000a000000506f6c7970686f6e6963'
)      # 130 байт: блок 0x252c; вставляется с удалением двух следующих за ним нулей (итого +128)


def _flat(items):
    out = bytearray()
    for x in items:
        if isinstance(x, Block):
            out += b'\x5a' + struct.pack('<HI', x.btype, x.size()) + _flat(x.items)
        else:
            out += x
    return bytes(out)


def _find_seg(items, off, base=0):
    """Сырой сегмент поддерева, содержащий смещение off (в «плоских» байтах): (список items, индекс, начало сегмента)."""
    pos = base
    for i, x in enumerate(items):
        if isinstance(x, Block):
            sub = _find_seg(x.items, off, pos + 7)
            if sub:
                return sub
            pos += 7 + len(_flat(x.items))
        else:
            if pos <= off < pos + len(x) or (off == pos + len(x) and False):
                return items, i, pos
            pos += len(x)
    return None


def _splice(tb, start, end, data):
    """Заменяет плоские байты [start, end) блока tb на data; диапазон должен лежать в одном сыром сегменте."""
    items = tb.items
    seg = _find_seg(items, start, 7) if start < end else _find_seg_insert(items, start, 7)
    if seg is None:
        raise ValueError('splice: segment not found')
    its, i, p0 = seg
    raw = bytes(its[i])
    a, b = start - p0, end - p0
    if b > len(raw):
        raise ValueError('splice: range crosses segments')
    its[i] = raw[:a] + bytes(data) + raw[b:]


def _find_seg_insert(items, off, base):
    """Сегмент для вставки в точку off (граница сегмента принадлежит предыдущему)."""
    pos = base
    last = None
    for i, x in enumerate(items):
        if isinstance(x, Block):
            sub = _find_seg_insert(x.items, off, pos + 7)
            if sub:
                return sub
            pos += 7 + len(_flat(x.items))
        else:
            if pos < off <= pos + len(x):
                return items, i, pos
            pos += len(x)
    return last


_P_FLAGS = bytes.fromhex('5a0100020000003720000001000000005c'.replace(' ', ''))     # ... 37 20 00 00 01 00 00 00 00 5c
_P_FLAG3 = bytes.fromhex('3720010000000 0ffff00000000'.replace(' ', ''))
_P_MID_A = bytes(8)
_P_MID_B = bytes.fromhex('010000005a0100' '0c000000382' '05a01000200000037' '20' '0000000000')
_VOL_ANCHOR = bytes.fromhex('356c7be0') + bytes(9)


def enable_track(p, track):
    """Включает Elastic Audio (Polyphonic) на дорожке track (индекс по порядку). Возвращает True, если что-то изменилось."""
    tb = TS.track_blocks(p)[track]
    F = _flat([tb])
    if b'Polyphonic' in F:
        return False
    # 1. флаги элемента 0x37 0x20: две единицы
    k = F.find(bytes.fromhex('5a0100020000003720000001000000005c'))
    if k < 0:
        raise ValueError('elastic: flags anchor not found')
    base = k + 9                                            # сразу после `37 20`
    # ... 37 20 | 00 | 00 01 00 00 00 | 00 5c  ->  ... 37 20 | 00 | 01 01 00 00 01 | 00 5c
    assert F[base] == 0 and F[base + 1] == 0 and F[base + 5] == 0, 'elastic: unexpected flags'
    # правки от конца к началу, чтобы смещения не плыли
    ops = []                                                 # (start, end, data)
    ops.append((base + 1, base + 2, b'\x01'))
    ops.append((base + 5, base + 6, b'\x01'))
    # 2. третий флаг: `37 20 01 00 00 00 00 ff ff 00 00 00 00 | 00 | 00 00 00 00 00 5a 01 00 0c`
    pat3 = bytes.fromhex('372001000000 00ffff00000000'.replace(' ', ''))
    k3 = F.find(pat3, base)
    if k3 < 0:
        raise ValueError('elastic: flag3 anchor not found')
    ops.append((k3 + len(pat3), k3 + len(pat3) + 1, b'\x01'))
    # 3. 19 байт после `d4 30 00 00 00` (у дорожки из шаблона их нет)
    k4 = F.find(bytes.fromhex('d430000000'))
    if k4 < 0:
        raise ValueError('elastic: d430 anchor not found')
    mid = k4 + 5
    if F[mid:mid + 8] == _P_MID_A and F[mid + 8] == 7:
        ops.append((mid, mid + 8, _P_MID_B))
    # 4. блок плагина Elastic Audio после идентификатора тома
    k5 = F.rfind(_VOL_ANCHOR)
    if k5 < 0:
        raise ValueError('elastic: volume anchor not found')
    ops.append((k5 + len(_VOL_ANCHOR), k5 + len(_VOL_ANCHOR) + 2, ELASTIC_PLUGIN_BLOB))
    for s, e, d in sorted(ops, key=lambda o: -o[0]):
        _splice(tb, s, e, d)
    # 5. флаг дорожки в агрегате 0x251a обеих семей
    _, a1, a2, end = TS.aggregate_names(p)[track]
    for a in (a1, a2):
        TS.own_patch(a, end + 22, b'\x01')
    return True


def elastic_region_block(src_start, new_start, src_len, new_end):
    from ptx_build import clone
    body = (REGION_ELASTIC_ID + REGION_ELASTIC_MID.ljust(18, b'\0')[:18] + struct.pack('<QQII', src_start, new_start, src_len, new_end)
            + bytes(4))
    assert len(body) == 62, len(body)
    return clone(b'\x5a' + struct.pack('<HI', 1, len(body) + 2) + b'\x36\x26' + body)
