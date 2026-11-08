#!/usr/bin/env python3
"""ptx_structure.py — вставка в сессию Pro Tools дорожек не-аудио типов: Basic Folder, Aux, Routing Folder.

Блоки взяты из эталонов Pro Tools 26.4 (lab/oracle9, см. make_struct_templates.py); что и где меняется при вставке
дорожки в позицию pos (считая все дорожки сессии), снято сравнением файлов до/после (PTSL CreateNewTracks):

  0x2624  : контейнер (0x263b / 0x261e) в позицию pos; счётчик u16 @2
  0x2519  : два блока 0x251a (по одному в каждую семью) в позицию pos; счётчик u32 между семьями;
            агрегат (первый сырой сегмент) пересобирается: записи [idx(3) type 0 | pstr имя | B(6) | 2a000000 id8]
  0x2107  : блок 0x210b в позицию pos; счётчик u16 @11
  0x202a  : (в 0x202b) список индексов 0..N-1: счётчик u32 @18 и новый индекс в конец
  таблица 0x0002 (символьно, PTX.tokenize): серия 0x2519 (+rec фам.2), список указателей фам.1, юниты K=1..N,
            серия 0x2624 (+rec контейнера), юниты контейнеров с K = номер контейнера
"""
import os
import random
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ptx_model import Block                        # noqa: E402
from ptx_build import clone, find_root, kids       # noqa: E402
import ptx_templates_struct as TPL                 # noqa: E402
import ptx_trackstate as TS                        # noqa: E402

TYPE_CODE = {'folder': 0x0B, 'aux': 0x02, 'routing': 0x02, 'audio': 0, 'audio_st': 0, 'audio_51': 0, 'master': 0x05, 'video': 0x08}
FMT_CODE_CHANNELS = {0: 1, 1: 2, 6: 6}              # B6[0] в 0x251a аудиодорожки -> число каналов


def pstr(s):
    b = s.encode('utf-8')
    return struct.pack('<I', len(b)) + b


def replace_raw(b, old, new):
    for i, x in enumerate(b.items):
        if isinstance(x, (bytes, bytearray)):
            if old in x:
                b.items[i] = bytes(x).replace(old, new)
        else:
            replace_raw(x, old, new)


# смещения (в байтах блока вместе с 7-байтным заголовком) полей глубины вложенности у контейнера
# при имени шаблона из TPL (для 'F1'; снято сравнением Folder 2 / Folder 1 в lab/folder1/f3.ptx)
def _set_depth(raw, depth):
    # поля глубины в контейнере шаблона с 8-байтным именем (Folder N): @218, @222 = depth, @317 = depth + 1 (f3: 0/0/1 и 1/1/2)
    b = bytearray(raw)
    b[218] = b[222] = depth
    b[317] = depth + 1
    return bytes(b)


def new_id8(rnd, used):
    while True:
        v = bytes(rnd.randrange(256) for _ in range(8))
        if v not in used:
            used.add(v)
            return v


def _walk(b):
    for x in b.items:
        if isinstance(x, Block):
            yield x
            yield from _walk(x)


def insert_child(parent, ctype, k, blk):
    """Вставляет blk k-м потомком с данным ctype (k == число таких потомков — в конец)."""
    ch = [x for x in parent.items if isinstance(x, Block) and x.ctype == ctype]
    idx = parent.items.index(ch[k]) if k < len(ch) else parent.items.index(ch[-1]) + 1
    parent.items.insert(idx, blk)


def _find_tok(tokens, kind, blk):
    for i, t in enumerate(tokens):
        if t[0] == kind and t[1] is blk:
            return i
    raise KeyError('token not found')


def _set_series(p, tokens, i_first_rec, n):
    """Счётчик серии (u16 LE): младший байт — последний байт токена перед первой записью, старший — первый байт самой записи."""
    t = tokens[i_first_rec - 1]
    assert t[0] == 'b'
    b = bytearray(t[1])
    b[-1] = n & 255
    tokens[i_first_rec - 1] = ('b', bytes(b))
    blk = tokens[i_first_rec][1]
    if n >> 8:
        p.rec_hi[id(blk)] = n >> 8
    else:
        p.rec_hi.pop(id(blk), None)


def _series_count(p, tokens, i_first_rec):
    return tokens[i_first_rec - 1][1][-1] | (p.rec_hi.get(id(tokens[i_first_rec][1]), 0) << 8)


def _be16_bump(p, tokens, i_first_rec, delta=1):
    _set_series(p, tokens, i_first_rec, _series_count(p, tokens, i_first_rec) + delta)


def insert_track(p, kind, name, pos, rnd=None, used_ids=None, depth=0):
    """Вставляет дорожку типа kind ('folder' | 'aux' | 'routing') с именем name в позицию pos (0..N)."""
    rnd = rnd or random.SystemRandom()
    if p.tokens is None:
        p.tokenize()
    T = TPL.STRUCT[kind]
    cont0 = TS.track_blocks(p)
    N = len(cont0)
    assert 0 <= pos <= N
    r2624, r2519, r2107 = find_root(p, 0x2624), find_root(p, 0x2519), find_root(p, 0x2107)
    ag = TS.aggregate_names(p)
    used = used_ids if used_ids is not None else {TS.track_id(a1, e) for (_, a1, _, e) in ag}
    id8 = new_id8(rnd, used)

    def make(raw, tname=None, tid=None):
        b = clone(raw)
        replace_raw(b, pstr(tname or T['name']), pstr(name))
        replace_raw(b, tid or T['id8'], id8)
        return b

    if kind == 'folder':                                    # контейнер папки: из интерфейса PT (верхний уровень / вложенная)
        ui = TPL.FOLDER_UI['top' if depth == 0 else 'nested']
        raw = ui['container']
        if depth > 1:
            raw = _set_depth(raw, depth)
        elif depth == 1:
            raw = _set_depth(raw, 1)
        container = make(raw, ui['name'], ui['id8'])
    else:
        container = make(T['container'])
    a1, a2, b210 = make(T['a251']), make(T['a251']), make(T['b210'])
    # номер дорожки (1-based) в 0x251a: u32 сразу за первым id8
    for a in (a1, a2):
        o = TS.own_bytes(a)
        end = o.find(id8) + 8
        TS.own_patch(a, end, struct.pack('<I', pos + 1))

    # --- блоки -----------------------------------------------------------
    old_cont = list(cont0)
    old_fam1 = [x for x in kids(r2519, 0x251a)][:N]
    old_fam2 = [x for x in kids(r2519, 0x251a)][N:2 * N]
    insert_child(r2624, container.ctype, 0, container) if False else None
    # контейнер: в позицию pos среди всех потомков 0x2624
    blocks = [x for x in r2624.items if isinstance(x, Block)]
    idx = r2624.items.index(blocks[pos]) if pos < N else r2624.items.index(blocks[-1]) + 1
    r2624.items.insert(idx, container)
    for fam, blk in ((old_fam1, a1), (old_fam2, a2)):      # семья 1, затем семья 2: перед fam[pos] или после последнего
        idx = r2519.items.index(fam[pos]) if pos < N else r2519.items.index(fam[-1]) + 1
        r2519.items.insert(idx, blk)
    insert_child(r2107, 0x210b, pos, b210)

    # --- счётчики -------------------------------------------------------
    TS.own_patch(r2624, 2, struct.pack('<H', N + 1))
    TS.own_patch(r2107, 11, struct.pack('<H', N + 1))
    # счётчик между семьями (сырой сегмент из 4 байт сразу после последнего блока семьи 1)
    items = r2519.items
    i1 = items.index(a1 if pos == N else a1)
    j = max(k for k, x in enumerate(items) if x is old_fam1[-1] or x is a1) + 1
    assert isinstance(items[j], (bytes, bytearray)) and len(items[j]) == 4 and struct.unpack('<I', items[j])[0] == N
    items[j] = struct.pack('<I', N + 1)

    # --- агрегат (первый сырой сегмент 0x2519) ---------------------------
    raw = bytes(items[0])
    head, ent, pos0 = bytearray(raw[:17]), [], 17          # голова: ct(2) | 01 00 00 00 | 00*10 | N (u8 @16)
    for (nm, _, _, _) in ag:
        ln = 5 + 4 + len(nm.encode('utf-8')) + 6 + 4 + 8     # [idx(3) type 0] pstr B(6) 2a000000 id8
        ent.append((bytearray(raw[pos0:pos0 + 5]), raw[pos0 + 5:pos0 + ln]))
        pos0 += ln
    trailer = raw[pos0:]
    assert len(trailer) == 3, f'unexpected aggregate trailer {trailer!r}'
    tpl_o = TS.own_bytes(clone(T['a251']))
    tend = 8 + len(T['name'].encode('utf-8'))
    b6 = tpl_o[tend:tend + 6]
    newbody = pstr(name) + b6 + b'\x2a\x00\x00\x00' + id8
    ent.insert(pos, (bytearray([0, 0, 0, TYPE_CODE[kind], 0]), newbody))
    out = bytearray(head)
    for k, (a, body) in enumerate(ent):
        a[0:3] = struct.pack('<I', k)[:3]
        out += a + body
    out += struct.pack('<I', N + 1)[:3]
    out[16:18] = struct.pack('<H', N + 1)                    # число дорожек: u16; старший байт совпадает с первым байтом первой записи
    items[0] = bytes(out)

    # --- 0x202a: список индексов ----------------------------------------
    r202b = find_root(p, 0x202b)
    lst = [x for x in kids(r202b, 0x202a)]
    for b in lst:
        o = TS.own_bytes(b)
        cnt = struct.unpack_from('<I', o, 18)[0]
        if cnt != N:
            continue
        TS.own_patch(b, 18, struct.pack('<I', N + 1))
        # вставка 2 байт в конец списка: сырой сегмент, содержащий o[22 + 2N]
        pos_ins = 22 + 2 * N
        acc = 0
        for k, x in enumerate(b.items):
            if isinstance(x, (bytes, bytearray)):
                if pos_ins <= acc + len(x):
                    s = bytes(x)
                    b.items[k] = s[:pos_ins - acc] + struct.pack('<H', N) + s[pos_ins - acc:]
                    break
                acc += len(x)

    # --- номер дорожки (1-based, среди всех) в блоках 0x2015 контейнеров: u8 @64 ----------------
    for i, c in enumerate(TS.track_blocks(p)[pos:], pos):          # номера дорожек левее вставки не меняются
        for d in _walk(c):
            if d.ctype == 0x2015:
                o = TS.own_bytes(d)
                if len(o) >= 66 and o[62] == 0 and o[63] == 1 and o[65] == 0:
                    TS.own_patch(d, 64, struct.pack('<I', i + 1))
                break

    # --- таблица ---------------------------------------------------------
    _table_insert(p, N, pos, container, a1, a2, old_cont, old_fam1, old_fam2, kind)
    return id8


def _table_insert(p, N, pos, container, a1, a2, old_cont, old_fam1, old_fam2, kind):
    tk = p.tokens
    r2624, r2519 = find_root(p, 0x2624), find_root(p, 0x2519)

    # общий счётчик в начале таблицы (u32 после ctype): +1 за папку и видео, +2 за aux/routing (по эталонам)
    t0 = tk[0]
    assert t0[0] == 'b'
    b0 = bytearray(t0[1])
    struct.pack_into('<I', b0, 2, struct.unpack_from('<I', b0, 2)[0] + (1 if kind in ('folder', 'video') else 2))
    tk[0] = ('b', bytes(b0))

    # === серия 0x2624: [rec 2624, rec контейнеров...] ======================
    i0 = _find_tok(tk, 'rec', r2624)
    _be16_bump(p, tk, i0)
    after = old_cont[pos - 1] if pos > 0 else r2624
    tk.insert(_find_tok(tk, 'rec', after) + 1, ('rec', container))

    # юниты контейнеров с блоком 0x261b: b(`.. ff ff ff ff 01 K 00 00 00 1c|1e 26`) ptr(2624) b(..1b 26) ptr(контейнер)
    # b(..27 26) ptr(0x261b) b(..01) rec(0x2627); K — номер контейнера (с 1), 1c — аудио, 1e — aux/routing
    def unit_starts():
        out = []
        for i, t in enumerate(tk):
            if t[0] == 'b' and t[1][-4:-2] == b'\x00\x00' and t[1][-2:] in (b'\x1c\x26', b'\x1d\x26', b'\x1e\x26') \
                    and i + 7 < len(tk) and tk[i + 1] == ('ptr', r2624) and tk[i + 3][0] == 'ptr' and tk[i + 7][0] == 'rec':
                out.append(i)
        return out

    new_order = [x for x in r2624.items if isinstance(x, Block)]
    if kids(container, 0x261b):                              # у aux/routing, как и у аудио, есть 0x261b — нужен свой юнит
        starts = unit_starts()
        prev_units = [i for i in starts if tk[i + 3][1] in new_order and new_order.index(tk[i + 3][1]) < pos]
        src = tk[starts[-1]:starts[-1] + 8]                  # образец — последний юнит
        b261b = kids(container, 0x261b)[0]
        b2627 = kids(b261b, 0x2627)[0]
        unit = [src[0], src[1], src[2], ('ptr', container), src[4], ('ptr', b261b), src[6], ('rec', b2627)]
        ins = (prev_units[-1] + 8) if prev_units else starts[0]
        tk[ins:ins] = unit
    for i in unit_starts():
        cb = tk[i + 3][1]
        if cb in new_order:
            b = bytearray(tk[i][1])
            b[-6:-4] = struct.pack('<H', new_order.index(cb) + 1)   # K = номер контейнера (с 1), u16
            b[-2:] = {0x261c: b'\x1c\x26', 0x261d: b'\x1d\x26'}.get(cb.ctype, b'\x1e\x26')
            tk[i] = ('b', bytes(b))

    # === группа 0x2519 =====================================================
    i0 = _find_tok(tk, 'rec', r2519)
    _be16_bump(p, tk, i0)
    after = old_fam2[pos - 1] if pos > 0 else r2519
    tk.insert(_find_tok(tk, 'rec', after) + 1, ('rec', a2))

    # при N == 1 Pro Tools кодирует «список семьи 1» одной записью (rec), при N >= 2 — списком указателей `L 00 N 00 ptr..`;
    # переводим единственную запись в форму списка сразу с новым указателем (сверено с pool_1 / pool_2)
    fam1_set = {id(x) for x in old_fam1}
    if N == 1:
        for i, t in enumerate(tk):
            if t[0] == 'rec' and t[1] is old_fam1[0] and tk[i - 1] == ('b', b'\x00\x00\x00\x00\x00\x01') \
                    and tk[i - 2] == ('ptr', r2519) and tk[i + 1][0] == 'b':
                nxt = tk[i + 1][1]
                order = [a1, old_fam1[0]] if pos == 0 else [old_fam1[0], a1]
                tk[i - 1:i + 2] = [('b', bytes.fromhex('00 00 00 00 00 01 00 00 00 01 08 00 02 00'.replace(' ', ''))),
                                   ('ptr', order[0]), ('ptr', order[1]), ('b', b'\x00\x00\x00' + nxt)]
                break
        else:
            raise AssertionError('N == 1 fam1 record not found')
        ptr_run = 'done'
    else:
        ptr_run = None
    if ptr_run is None:
        for i, t in enumerate(tk):
            if t[0] == 'ptr' and id(t[1]) in fam1_set and tk[i - 1][0] == 'b' and tk[i - 1][1][-4:] == struct.pack('<HH', 4 * N, N):
                ptr_run = i
                break
        assert ptr_run is not None, 'fam1 pointer list not found'
        hb = bytearray(tk[ptr_run - 1][1])
        hb[-4:] = struct.pack('<HH', 4 * (N + 1), N + 1)
        tk[ptr_run - 1] = ('b', bytes(hb))
        tk.insert(ptr_run + pos, ('ptr', a1))

    # юниты K=1..N: b(... ff ff ff ff 01 K 00 00 00 1a 25) ptr(2519) b(00 00 00 00 00 01) rec(фам.2)
    new_fam2 = [x for x in kids(r2519, 0x251a)][N + 1:2 * (N + 1)]
    first = None
    for i, t in enumerate(tk):
        if t[0] == 'rec' and t[1] is old_fam2[0] and tk[i - 1][0] == 'b' and tk[i - 1][1] == b'\x00\x00\x00\x00\x00\x01':
            first = i
            break
    # первый юнит K=1: его rec — первая из REC после ptr_run; ищем юниты по токену заголовка
    units = []
    for i, t in enumerate(tk):
        if t[0] == 'b' and t[1].endswith(b'\x00\x00\x1a\x25') and tk[i + 1] == ('ptr', r2519) \
                and tk[i + 2][0] == 'b' and tk[i + 2][1] == b'\x00\x00\x00\x00\x00\x01' and tk[i + 3][0] == 'rec':
            units.append(i)
    assert len(units) == N, f'expected {N} fam2 units, got {len(units)}'
    # клон последнего юнита -> юнит N+1
    last = units[-1]
    clone_toks = [tk[last], tk[last + 1], tk[last + 2], tk[last + 3]]
    if clone_toks[0][1].startswith(b'\x00' * 6 + b'\x02'):          # у первого юнита заголовок с 3 лишними нулями (хвост списка)
        clone_toks[0] = ('b', clone_toks[0][1][3:])
    # после последнего юнита в таблице идёт токен заголовка следующей группы (его K = N -> N+1)
    tk[last + 4:last + 4] = list(clone_toks)
    units = []
    for i, t in enumerate(tk):
        if t[0] == 'b' and t[1].endswith(b'\x00\x00\x1a\x25') and tk[i + 1] == ('ptr', r2519) \
                and tk[i + 2][0] == 'b' and tk[i + 2][1] == b'\x00\x00\x00\x00\x00\x01' and tk[i + 3][0] == 'rec':
            units.append(i)
    assert len(units) == N + 1
    for k, i in enumerate(units):
        b = bytearray(tk[i][1])
        b[-6:-4] = struct.pack('<H', k + 1)
        tk[i] = ('b', bytes(b))
        tk[i + 3] = ('rec', new_fam2[k])
    # заголовки следующих групп с K = N (ссылаются на 0x2519): K -> N+1
    for i, t in enumerate(tk):
        if t[0] == 'b' and i + 1 < len(tk) and tk[i + 1] == ('ptr', r2519) and len(t[1]) >= 8 \
                and t[1][-4:] in (b'\x00\x00\x1b\x25', b'\x00\x00\x16\x27') and t[1][-7] == 1 and struct.unpack('<H', t[1][-6:-4])[0] == N \
                and t[1][-11:-7] == b'\xff\xff\xff\xff':
            b = bytearray(t[1])
            b[-6:-4] = struct.pack('<H', N + 1)
            tk[i] = ('b', bytes(b))


def set_membership(p, groups):
    """Состав папок (корневой блок 0x210c). groups: {индекс папки: [индексы дочерних дорожек]} в итоговой нумерации.

    Формат: ct | n записей u32 | записи [владелец (5 нулей + 2a000000 + id8) | число детей u32 | дети (как владелец)]."""
    ag = TS.aggregate_names(p)
    ids = [TS.track_id(a1, end) for (_, a1, _, end) in ag]

    def ent(i):
        return b'\0\0\0\0\0' + b'\x2a\x00\x00\x00' + ids[i]

    body = bytearray(struct.pack('<I', len(groups)))
    for owner in sorted(groups):
        ch = sorted(groups[owner])
        body += ent(owner) + struct.pack('<I', len(ch))
        for c in ch:
            body += ent(c)
    r = find_root(p, 0x210c)
    r.items = [struct.pack('<H', 0x210c) + bytes(body)]


def rebuild_strips(p):
    """Полосы микшера 0x2589 (кэш Pro Tools): у каждой дорожки своя, idx = позиция дорожки. Для аудио переприсваиваем idx,
    для Basic Folder добавляем полосу (200 байт, образец из эталона). У вложенных папок без полосы Pro Tools падает."""
    r = find_root(p, 0x2587)
    try:
        st = kids(r, 0x2551)[0]
        b258a = kids(kids(st, 0x258b)[0], 0x258a)[0]
    except (IndexError, KeyError):
        return
    entries = kids(b258a, 0x2589)
    cont = TS.track_blocks(p)
    audio_pos = [i for i, c in enumerate(cont) if c.ctype == 0x261c]
    old = sorted((e for e in entries if len(R_flat(e)) == 271), key=lambda e: _strip_idx(e))
    if not old or len(old) > len(audio_pos):
        return                                              # незнакомая раскладка — не трогаем
    while len(old) < len(audio_pos):                         # новые аудиодорожки (стерео/5.1): полоса как у моно
        old.append(clone(R_flat(old[-1])))
    new = []
    for e, pos in zip(old, audio_pos):
        _set_strip_idx(e, pos)
        new.append((pos, e))
    for i, c in enumerate(cont):
        if c.ctype == 0x263b:
            b = clone(TPL.FOLDER_STRIP)
            _set_strip_idx(b, i)
            new.append((i, b))
    new.sort(key=lambda t: t[0])
    first = b258a.items.index(entries[0])
    for e in entries:
        b258a.items.remove(e)
    for k, (_, e) in enumerate(new):
        b258a.items.insert(first + k, e)
    TS.own_patch(b258a, 2, struct.pack('<I', len(new)))


def _strip_list(p):
    r = find_root(p, 0x2587)
    try:
        st = kids(r, 0x2551)[0]
        b258a = kids(kids(st, 0x258b)[0], 0x258a)[0]
    except (IndexError, KeyError):
        return None, []
    return b258a, kids(b258a, 0x2589)


def set_folders_closed(p, positions):
    """Закрытые папки (дети скрыты): флаг в полосе микшера папки (0x2589 own[72]; снято PTSL SetTrackOpenState). У Routing Folder из
    шаблона полосы нет — создаём по образцу полосы аудиодорожки (как у Pro Tools: полоса есть у каждой дорожки)."""
    b258a, entries = _strip_list(p)
    if b258a is None or not entries or not positions:
        return False
    by_idx = {_strip_idx(e): e for e in entries}
    proto = R_flat(entries[0])
    changed = False
    for pos in sorted(positions):
        e = by_idx.get(pos)
        if e is None:
            e = clone(proto)
            _set_strip_idx(e, pos)
            by_idx[pos] = e
        TS.own_patch(e, 72, b'\x01')
        changed = True
    if changed:
        new = [by_idx[k] for k in sorted(by_idx)]
        first = b258a.items.index(entries[0])
        for e in entries:
            b258a.items.remove(e)
        for k, e in enumerate(new):
            b258a.items.insert(first + k, e)
        TS.own_patch(b258a, 2, struct.pack('<I', len(new)))
    return changed


def read_closed_folders(p):
    """Позиции дорожек, у которых в полосе микшера стоит флаг «закрыта» (папки)."""
    _, entries = _strip_list(p)
    out = set()
    for e in entries:
        o = TS.own_bytes(e)
        if len(o) > 72 and o[72]:
            out.add(_strip_idx(e))
    return out


def R_flat(b):
    from ptx_to_reaper import flat
    return flat([b])


def _strip_idx(e):
    return struct.unpack_from('<H', TS.own_bytes(e), 2)[0]


def _set_strip_idx(e, i):
    TS.own_patch(e, 2, struct.pack('<H', i))


# ----------------------------------------------------------------------------------------------------------------
# плейлисты аудиодорожек: 0x1052 (по одному на канал, все с именем дорожки) и 0x1014 (запись дорожки)

def audio_channels(p):
    """[(индекс контейнера, имя, число каналов, id8)] аудиодорожек в порядке дорожек."""
    cont = TS.track_blocks(p)
    out = []
    for i, (nm, a1, _, end) in enumerate(TS.aggregate_names(p)):
        if cont[i].ctype != 0x261c:
            continue
        o = TS.own_bytes(a1)
        code = o[end + 0]                                  # B6[0] сразу за именем: 0 моно, 1 стерео, 6 5.1
        out.append((i, nm, FMT_CODE_CHANNELS.get(code, 1), o[end + 10:end + 18]))
    return out


def _entry_1014(name, id8, kind, idxs):
    c = len(idxs)
    return (struct.pack('<H', 0x1014) + pstr(name) + bytes([kind]) + struct.pack('<I', c)
            + b''.join(struct.pack('<H', i) for i in idxs) + b'\0' * 4 + b'\x2a\0\0\0' + id8
            + bytes([kind, 0, kind]) + b'\0' * 4 + b''.join(struct.pack('<I', i) for i in idxs) + bytes([kind]) + b'\0' * 5)


def reindex_playlists(p, alts=None):
    """Пересобирает 0x1052/0x1014 по текущему набору аудиодорожек (в порядке дорожек): каналы -> плейлисты,
    индексы в 0x1014 и в контейнерах (idx дорожки, список индексов записей 0x1014), счётчики и серии таблицы.

    alts: {имя дорожки: [имена альтернативных плейлистов]}. Основные плейлисты идут первыми (по порядку дорожек), затем
    альтернативные (по дорожкам); контейнер дорожки перечисляет свою запись и записи альтернатив: активный плейлист —
    с индексом, равным idx дорожки (так в сессиях PB/f3)."""
    alts = alts or {}
    if p.tokens is None:
        p.tokenize()
    tk = p.tokens
    r1054, r1015 = find_root(p, 0x1054), find_root(p, 0x1015)
    old52, old14 = kids(r1054, 0x1052), kids(r1015, 0x1014)
    by_name = {}
    for b in old52:
        n = struct.unpack_from('<I', b.items[0], 2)[0]
        by_name.setdefault(b.items[0][6:6 + n].decode(), []).append(b)
    e14 = {}
    for b in old14:
        f = b.items[0]
        n = struct.unpack_from('<I', f, 2)[0]
        e14[f[6:6 + n].decode()] = b
    tracks = audio_channels(p)
    kind_of = {1: 0, 2: 1, 6: 6}
    new52, new14, container_lists = [], [], {}
    base = 0
    proto = old52[0]

    def playlist_blocks(name, C, existing):
        pls = list(existing)
        while len(pls) < C:
            b = clone(R_flat_block(proto))
            n0 = struct.unpack_from('<I', proto.items[0], 2)[0]
            replace_raw(b, pstr(proto.items[0][6:6 + n0].decode()), pstr(name))
            pls.append(b)
        return pls[:C]

    # основные плейлисты
    main_idx = {}
    for k, (ci, nm, C, id8) in enumerate(tracks):
        pls = playlist_blocks(nm, C, by_name.get(nm, []))
        new52 += pls
        pl_id = id8[:4] + struct.pack('<I', (struct.unpack('<I', id8[4:])[0] + 1) & 0xFFFFFFFF)
        blk = e14.get(nm)
        if blk is not None:
            o = TS.own_bytes(blk)
            n = struct.unpack_from('<I', o, 2)[0]
            pl_id = o[2 + 4 + n + 1 + 4 + 2 * struct.unpack_from('<I', o, 2 + 4 + n + 1)[0] + 4 + 4:][:8]
        raw = _entry_1014(nm, pl_id, kind_of[C], list(range(base, base + C)))
        base += C
        if blk is not None:
            blk.items = [raw]
        else:
            blk = Block(old14[0].btype, [raw])
        new14.append(blk)
        main_idx[nm] = k
    # альтернативные плейлисты: в конец обоих списков
    used = {id8 for (_, _, _, id8) in tracks}
    rnd = random.SystemRandom()
    alt_idx = {}
    for k, (ci, nm, C, id8) in enumerate(tracks):
        for an in alts.get(nm, []):
            pls = playlist_blocks(an, C, [])
            new52 += pls
            pid = new_id8(rnd, used)
            raw = _entry_1014(an, pid, kind_of[C], list(range(base, base + C)))
            base += C
            blk = Block(old14[0].btype, [raw])
            alt_idx.setdefault(nm, []).append(len(new14))
            new14.append(blk)
    # контейнеры: idx дорожки, список записей 0x1014 (своя + альтернативы)
    cont_all = TS.track_blocks(p)
    for k, (ci, nm, C, id8) in enumerate(tracks):
        entries = [k] + alt_idx.get(nm, [])
        seg = struct.pack('<IH', k, len(entries)) + b''.join(struct.pack('<HH', e, 0) for e in entries) + b'\xff\xff'
        cont_all[ci].items[2] = seg
    # заменить блоки в деревьях
    for root, olds, news in ((r1054, old52, new52), (r1015, old14, new14)):
        first = root.items.index(olds[0])
        for b in olds:
            root.items.remove(b)
        for j, b in enumerate(news):
            root.items.insert(first + j, b)
    TS.own_patch(r1054, 2, struct.pack('<I', len(new52)))
    TS.own_patch(r1015, 2, struct.pack('<I', len(new14)))
    # серии таблицы
    for parent, olds, news in ((r1054, old52, new52), (r1015, old14, new14)):
        i0 = _find_tok(tk, 'rec', parent)
        old_ids = {id(b) for b in olds}
        j = i0 + 1
        while j < len(tk) and tk[j][0] == 'rec' and (id(tk[j][1]) in old_ids):
            j += 1
        tk[i0 + 1:j] = [('rec', b) for b in news]
        _set_series(p, tk, i0, 1 + len(news))


def R_flat_block(b):
    from ptx_to_reaper import flat
    return flat([b])
