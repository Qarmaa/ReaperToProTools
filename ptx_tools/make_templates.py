#!/usr/bin/env python3
"""Снимает образцы блоков с эталонов Pro Tools (lab/oracle2) -> ptx_templates.py (hex)."""
import sys
sys.path.insert(0, '.')
import ptx


def load(p):
    buf = ptx.crypt(open(p, 'rb').read())
    top, _ = ptx.parse_tree(buf)
    return buf, top


def walk(bs):
    for b in bs:
        yield b
        yield from walk(b.children)


def raw(buf, b):
    return bytes(buf[b.pos:b.end])


out = {}
buf, top = load('lab/oracle2/o_clip.ptx')
r1004 = [b for b in top if b.ctype == 0x1004][0]
out['F1003'] = raw(buf, [c for c in r1004.children if c.ctype == 0x1003][0])
r262a = [b for b in top if b.ctype == 0x262a][0]
out['REGION_WHOLE'] = raw(buf, [c for c in r262a.children if c.ctype == 0x2629][0])
ev = [b for b in walk(top) if b.ctype == 0x1050][0]
out['EVENT_AUDIO'] = raw(buf, ev)
buf, top = load('lab/oracle2/o_fadein.ptx')
evs = [b for b in walk(top) if b.ctype == 0x1050]
out['EVENT_FADE'] = raw(buf, evs[0])                       # тип 0x01
geoms = [b for b in walk(top) if b.ctype == 0x262f]
out['GEOM_IN'] = raw(buf, geoms[0])
buf, top = load('lab/oracle2/o_fadeout.ptx')
out['GEOM_OUT'] = raw(buf, [b for b in walk(top) if b.ctype == 0x262f][1])
buf, top = load('lab/oracle2/o_xfade.ptx')
out['GEOM_XFADE'] = raw(buf, [b for b in walk(top) if b.ctype == 0x262f][1])
r262a = [b for b in top if b.ctype == 0x262a][0]
out['REGION_SUB'] = raw(buf, [c for c in r262a.children if c.ctype == 0x2629][1])

buf, top = load('lab/oracle4/m1.ptx')
out['MARKER'] = raw(buf, [c for b in top if b.ctype == 0x2030 for c in b.children if c.ctype == 0x2077][0])

with open('ptx_templates.py', 'w') as f:
    f.write('"""Образцы блоков Pro Tools 26.4 (снято make_templates.py с lab/oracle2). Не редактировать."""\n')
    f.write('import binascii\n\n')
    for k, v in out.items():
        f.write(f'{k} = binascii.unhexlify(\n')
        h = v.hex()
        for i in range(0, len(h), 96):
            f.write(f"    '{h[i:i+96]}'\n")
        f.write(')\n\n')
print({k: len(v) for k, v in out.items()})
