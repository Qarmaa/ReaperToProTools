#!/usr/bin/env python3
"""ptx_slot_gen.py — шаблон слота плагина Pro Tools «с нуля» по идентичности плагина (без сессии-образца).

synth(base, name, mfr, prod, plug, effect_id, keep_state=False) -> шаблон {'name','format','slot','catalog'}
  base — готовый шаблон ТОГО ЖЕ формата каналов (mono/stereo/5.1/...): из него берутся служебные поля, которые от плагина не зависят.
  Меняются: имя, три FourCC (в слоте — 0x1038, в записи каталога 0x1017 — задом наперёд), строка effect ID; состояние плагина
  выбрасывается (Pro Tools создаёт значения по умолчанию сам).
"""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ptx_build as B  # noqa: E402
import ptx_to_reaper as R  # noqa: E402
import ptx_trackstate as TS  # noqa: E402


def _rev(f):
    return bytes(f[::-1])


def synth_catalog(base_cat, name, mfr, prod, plug, effect_id, flag=b'\x00\x00\x00'):
    """Запись каталога: имя, 3 FourCC задом наперёд, формат (4 байта) + флаги + effect ID + хвост — как у образца, кроме идентичности."""
    blk = B.clone(base_cat)
    o = TS.own_bytes(blk)
    n = struct.unpack_from('<I', o, 3)[0]
    rest = o[7 + n + 12:]                                  # 4 байта формата, 3 байта флагов, u32 len + effect ID, хвост
    fmt4 = rest[:4]
    m = struct.unpack_from('<I', rest, 7)[0]
    tail = rest[11 + m:]
    nm = name.encode('utf-8')
    eid = effect_id.encode('utf-8')
    new = (o[:3] + struct.pack('<I', len(nm)) + nm + _rev(mfr) + _rev(prod) + _rev(plug) + fmt4 + flag +
           struct.pack('<I', len(eid)) + eid + tail)
    return b'\x5a' + struct.pack('<HI', blk.btype, len(new)) + new


def synth_slot(base_slot_z, mfr, prod, plug, keep_state=False, drop_state_block=False):
    blk = B.clone(zlib.decompress(base_slot_z))
    cont = TS.kids(TS.kids(blk, 0x2615)[0], 0x2613)[0]
    st = TS.kids(cont, 0x1038)
    if st and drop_state_block:
        cont.items.remove(st[0])
    elif st:
        s = st[0]
        o = TS.own_bytes(s)
        if not keep_state:
            TS.own_splice(s, 66, len(o), b'')
            o = TS.own_bytes(s)
        TS.own_patch(s, 18, mfr + prod + plug)
        size = len(o) - 14
        TS.own_patch(s, 6, struct.pack('<I', size))
        TS.own_patch(s, 12, struct.pack('>H', size))
    return zlib.compress(R.flat([blk]), 9)


def synth(base, name, mfr, prod, plug, effect_id, keep_state=False, drop_state_block=False):
    return {'name': name, 'format': base['format'],
            'slot': synth_slot(base['slot'], mfr, prod, plug, keep_state, drop_state_block),
            'catalog': synth_catalog(base['catalog'], name, mfr, prod, plug, effect_id)}
