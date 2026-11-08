#!/usr/bin/env python3
"""ptx_plugin_learn.py — личная библиотека шаблонов плагинов Pro Tools (для переноса Reaper -> Pro Tools).

learn(paths): берёт из сессий Pro Tools слоты инсертов (плагин + формат дорожки), которых нет во встроенной библиотеке, и
дописывает их в ~/Library/Application Support/Reaper to Pro Tools/pt_plugins.dat. Настройки плагина в шаблоне — те, что стояли
в сессии-источнике.

    ptx_plugin_learn.py --learn session.ptx [...]     -> {"added": [...], "total": N}
    ptx_plugin_learn.py --count                       -> число плагинов в личной библиотеке
"""
import ast
import json
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ptx_model import PTX, Block  # noqa: E402
import ptx_to_reaper as R  # noqa: E402
import ptx_trackstate as TS  # noqa: E402

USER_DIR = os.path.expanduser('~/Library/Application Support/Reaper to Pro Tools')
USER_FILE = os.path.join(USER_DIR, 'pt_plugins.dat')
MAX_SLOT = 400000                       # байт: больше — состояние с данными сессии (замеры и т.п.), не шаблон


def load_user():
    try:
        return ast.literal_eval(open(USER_FILE, encoding='utf-8').read())
    except (OSError, ValueError, SyntaxError):
        return {}


def save_user(lib):
    os.makedirs(USER_DIR, exist_ok=True)
    tmp = USER_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(repr(lib))
    os.replace(tmp, USER_FILE)


def _cat_entry(p, key, blk):
    """Запись плагина в каталоге сессии 0x1018 (точная — по номеру в слоте, иначе по производителю и продукту)."""
    r = [x for x in p.roots if isinstance(x, Block) and x.ctype == 0x1018]
    if not r:
        return None
    r = r[0]
    ents = TS.kids(r, 0x1017)
    if TS.kids(blk, 0x2615):
        idx = struct.unpack_from('<H', TS.own_bytes(TS.kids(blk, 0x2615)[0]), 2)[0]
        if idx < len(ents):
            return R.flat([ents[idx]])
    for e in ents:
        o = TS.own_bytes(e)
        n = struct.unpack_from('<I', o, 3)[0]
        f = o[7 + n:7 + n + 12]
        if tuple(bytes(f[i:i + 4][::-1]) for i in (0, 4)) == key[:2]:
            return R.flat([e])
    return None


def learn(paths):
    import ptx_plugins as PL
    from ptx_build import clone
    try:
        from ptx_templates_plugins import SLOTS as BUILTIN
    except ImportError:
        BUILTIN = {}
    lib = load_user()
    added = []
    for path in paths:
        p = PTX(open(path, 'rb').read())
        chans = {i: {1: 'mono', 2: 'stereo', 6: '5.1'}.get(st.get('channels')) for i, st in enumerate(TS.read_states(p))}
        for ti, (tb, pls) in enumerate(zip(TS.track_blocks(p), PL.read_plugins(p))):
            if not pls:
                continue
            d = TS.kids(TS.kids(tb, 0x261b)[0], 0x2627)[0]
            slots = [x for x in d.items if isinstance(x, Block)]
            for pl in pls:
                blk = slots[pl['slot']]
                fmt = chans.get(ti) or pl['format']
                key = pl['key'] + (fmt,)
                if key in BUILTIN or key in lib or fmt not in ('mono', 'stereo', '5.1') or not TS.kids(blk, 0x2615):
                    continue
                flat = R.flat([blk])
                if pl['bypass']:                                   # шаблон хранится в активном состоянии (байт bypass — в конце блока)
                    c = clone(flat)
                    TS.own_patch(c, 2, b'\x00')
                    flat = R.flat([c])
                cat = _cat_entry(p, pl['key'], blk)
                if cat is None or len(flat) > MAX_SLOT or '?' in pl['name']:
                    continue
                lib[key] = {'name': pl['name'], 'format': fmt, 'slot': zlib.compress(flat, 9), 'catalog': cat}
                added.append(f"{pl['name']} ({fmt})")
    if added:
        save_user(lib)
    return added


if __name__ == '__main__':
    if '--learn' in sys.argv:
        paths = [a for a in sys.argv[1:] if not a.startswith('--')]
        added = learn(paths)
        print(json.dumps({'added': added, 'total': len(load_user())}, ensure_ascii=False))
    elif '--count' in sys.argv:
        print(len(load_user()))
