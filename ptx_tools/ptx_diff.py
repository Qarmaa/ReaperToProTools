#!/usr/bin/env python3
"""Древовидное сравнение двух .ptx (быстро): какие блоки добавлены/удалены/изменены.

    ptx_diff.py a.ptx b.ptx [--max N]      (по умолчанию печатает 25 типов различий)
"""
import difflib
import sys

sys.path.insert(0, '.')
import ptx


def load(path):
    buf = ptx.crypt(open(path, 'rb').read())
    top, _ = ptx.parse_tree(buf)
    return buf, top


def bytes_diff(a, b, limit=24):
    """Быстро: общий префикс/суффикс; при равной длине — позиционные отличия."""
    out = []
    if len(a) == len(b):
        i = 0
        n = len(a)
        while i < n and len(out) < 6:
            if a[i] != b[i]:
                j = i
                while j < n and a[j] != b[j]:
                    j += 1
                out.append(f'@{i}: {a[i:j][:limit].hex(" ")} -> {b[i:j][:limit].hex(" ")}')
                i = j
            else:
                i += 1
        return out
    p = 0
    while p < min(len(a), len(b)) and a[p] == b[p]:
        p += 1
    q = 0
    while q < min(len(a), len(b)) - p and a[-1 - q] == b[-1 - q]:
        q += 1
    return [f'@{p}: {a[p:len(a) - q][:limit].hex(" ")} -> {b[p:len(b) - q][:limit].hex(" ")}']


class Diff:
    def __init__(self, ba, bb):
        self.ba, self.bb = ba, bb
        self.items = []         # (kind, path, details)

    def blocks(self, A, B, path):
        ka = [b.ctype for b in A]
        kb = [b.ctype for b in B]
        if ka == kb:
            pairs = list(zip(A, B))
        else:
            pairs = []
            sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == 'equal':
                    pairs += list(zip(A[i1:i2], B[j1:j2]))
                else:
                    if i2 > i1:
                        self.items.append(('DELETED', path, [f'{len(A[i1:i2])} block(s): ' +
                                                             ','.join(f'{b.ctype:#06x}' for b in A[i1:i2][:6])]))
                    if j2 > j1:
                        self.items.append(('INSERTED', path, [f'{len(B[j1:j2])} block(s): ' +
                                                              ','.join(f'{b.ctype:#06x}' for b in B[j1:j2][:6])]))
        for a, b in pairs:
            p = path + (a.ctype,)
            oa, ob = ptx.own_payload(self.ba, a), ptx.own_payload(self.bb, b)
            if oa != ob:
                self.items.append(('CHANGED', p, [f'own {len(oa)}->{len(ob)}'] + bytes_diff(oa, ob)[:4]))
            self.blocks(a.children, b.children, p)


def fmt(p):
    return '>'.join(f'{c:#06x}' for c in p[-4:]) or '(root)'


def main(argv):
    mx = int(argv[argv.index('--max') + 1]) if '--max' in argv else 25
    ba, A = load(argv[1])
    bb, B = load(argv[2])
    d = Diff(ba, bb)
    d.blocks(A, B, ())
    summ = {}
    for kind, p, det in d.items:
        summ.setdefault((kind, fmt(p)), []).append(det)
    print(f'{len(d.items)} differences in {len(summ)} kinds')
    for (kind, p), lst in sorted(summ.items(), key=lambda kv: -len(kv[1]))[:mx]:
        print(f'{len(lst):4d} x {kind:9s} {p}')
        for x in lst[0][:3]:
            print('        ', x)


if __name__ == '__main__':
    main(sys.argv)
