#!/usr/bin/env python3
"""Переименование дорожек в .ptx: имя хранится в 7 местах как `len u32 | utf-8`.

Места (подтверждено на pool64): 0x2519 (сводка), 0x251a x2 (две семьи), 0x1052,
0x1014, 0x210b и 0x2619 (внутри 0x261c>0x261b>0x102d).

    ptx_tracks.py rename <in.ptx> <out.ptx> OLD=NEW [OLD=NEW ...]
"""
import re
import struct
import sys

sys.path.insert(0, '.')
from ptx_model import PTX

EXPECTED_PLACES = 7


def pack(name):
    b = name.encode('utf-8')
    return struct.pack('<I', len(b)) + b


def rename_tracks(p, mapping):
    """mapping: {old: new}. Меняет все вхождения; возвращает счётчик по именам."""
    counts = {k: 0 for k in mapping}
    pats = {k: pack(k) for k in mapping}
    any_pat = re.compile(b'|'.join(re.escape(x) for x in pats.values())) if pats else None
    for blk in p.blocks():
        for i, item in enumerate(blk.items):
            if isinstance(item, (bytes, bytearray)):
                if any_pat is None or not any_pat.search(item):          # быстрый отсев: в большинстве сегментов имён нет
                    continue
                new = bytes(item)
                for old, new_name in mapping.items():
                    n = new.count(pats[old])
                    if n:
                        new = new.replace(pats[old], pack(new_name))
                        counts[old] += n
                if new != item:
                    blk.items[i] = new
    return counts


def main(argv):
    if len(argv) < 5 or argv[1] != 'rename':
        print(__doc__)
        return 1
    _, _, src, dst, *pairs = argv
    mapping = dict(x.split('=', 1) for x in pairs)
    raw = open(src, 'rb').read()
    p = PTX(raw)
    counts = rename_tracks(p, mapping)
    bad = {k: v for k, v in counts.items() if v != EXPECTED_PLACES}
    for k, v in counts.items():
        print(f'  {k!r} -> {mapping[k]!r}: {v} places')
    if bad:
        print(f'ERROR: expected {EXPECTED_PLACES} places per track, got {bad}')
        return 1
    open(dst, 'wb').write(p.save())
    print('saved', dst)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
