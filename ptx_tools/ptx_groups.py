#!/usr/bin/env python3
"""ptx_groups.py — Clip Groups (Alt+Cmd+G, «аудио-группы») в .ptx: запись.

Эталон — сессии, сохранённые Pro Tools 26.4 после CId_GroupClips (SetTimelineSelection + GroupClips; /tmp/mt/grp, /tmp/mt/grpb),
блоки в ptx_templates_groups.py. Что создаёт группа на дорожке (моно):
  * 0x262c > 0x262b > 0x2628 (+0x2523 > 0x2526) — определение группы, как у региона: pstr имя | V 50 LW REF 08 | ticks 1e12 (5 байт) | длина | [старт x2] | ff*8 | ... | uuid;
    дочерний 0x2523: старт (u64 @2), флаг @18 (старт != 0), ticks старт и конец, конец (u64 @46);
  * 0x2424 > 0x2423 — имя группы; 0x2426 > 0x2425 — путь к сессии (узлы + id тома);
  * 0x2428 > 0x1054 > 0x1052 — содержимое группы: плейлист «?» с теми же событиями (0x1050 > 0x104f), позиция = 1e12 + сэмплы от начала группы;
  * плейлист дорожки: события внутри группы заменены ОДНИМ событием-группой (0x1050 own `00 00 01`, 0x104f: идентификатор = номер группы, флаг o[20] = 1);
  * дорожка 0x2015: байт-флаг «есть группы» (после `d4 30 00 00 00`) и лишний дочерний 0x2038 после 0x2104;
  * таблица 0x0002: записи 0x2423 / 0x2425 / 0x1052 в сериях своих корней (счётчик серии +1, запись — после существующих).
Группа считается только на моно-дорожке; события внутри диапазона группы, не принадлежащие её клипам, группу отменяют (Pro Tools сгруппировал бы и их).
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ptx_model import Block  # noqa: E402
import ptx_build as B  # noqa: E402
import ptx_structure as ST  # noqa: E402
import ptx_templates_groups as TG  # noqa: E402
import ptx_trackstate as TS  # noqa: E402

TICKS_BASE = 10 ** 12
CHAN_SUFFIX = {1: [''], 2: ['.L', '.R'], 6: ['.L', '.C', '.R', '.Ls', '.Rs', '.LFE']}       # имена определений групп по каналам (как у Pro Tools)
CHAN_CODE = {2: bytes.fromhex('0100000101'), 6: bytes.fromhex('0600000606')}         # хвост записи 0x2423 многоканальной группы


def _first(b, ct):
    return [k for k in b.items if isinstance(k, Block) and k.ctype == ct][0]


def make_group_def(name, start, length, rnd, index=0, ordinal=0):
    """0x262b: определение группы длиной length сэмплов, начинающейся на таймлайне в start."""
    b = B.clone(TG.GROUP_DEF)
    d = _first(b, 0x2628)
    head = bytes(d.items[0])
    # `28 26 | pstr имя | 00 50 30 00 08 | ticks(5) | len(3) | ff*8 | fe ff ...` -> перестраиваем до `fe ff`
    n = struct.unpack_from('<I', head, 2)[0]
    k = head.index(b'\xfe\xff\x00\x00\x00\x00\xff\xff', 6 + n)
    rest = head[k:]
    lw = B._width(length)
    geom = bytes([0, 0x50, lw << 4, 0, 8]) + struct.pack('<Q', TICKS_BASE)[:5] + B._le(length, lw)
    if start:
        sw = max(2, B._width(start))
        geom = bytes([0, 0x50, lw << 4, sw * 0x11, 8]) + struct.pack('<Q', TICKS_BASE)[:5] + B._le(length, lw) + B._le(start, sw) * 2
    d.items[0] = head[:2] + B.pstr(name) + geom + b'\xff' * 8 + rest
    # хвостовой сырой сегмент 0x2628: ... ff ff ff ff 00 00 <uuid 16>
    tail = bytearray(d.items[-1])
    tail[-16:] = bytes(rnd.randrange(256) for _ in range(16))
    d.items[-1] = bytes(tail)
    c = _first(d, 0x2523)
    TS.own_patch(c, 2, struct.pack('<Q', start))
    TS.own_patch(c, 18, bytes([index & 255]))
    own = TS.own_bytes(c)
    t1 = own.index(struct.pack('<Q', TICKS_BASE)[:5], 20)
    TS.own_patch(c, t1 + 8, struct.pack('<Q', TICKS_BASE + length)[:5])
    TS.own_patch(c, 46, struct.pack('<Q', start + length))
    b.items[-1] = bytes(b.items[-1])[:1] + bytes([ordinal & 255]) + bytes(b.items[-1])[2:]
    return b


def make_group_name(name, index, tail=b'\0' * 5):
    b = B.clone(TG.GROUP_NAME)
    b.items[0] = b.items[0][:2] + struct.pack('<I', index) + B.pstr(name) + tail
    return b


def make_group_path(nodes, vol_id, flag):
    """0x2425: путь к сессии: узлы (том, папки..., файл .ptx) + id тома."""
    b = B.clone(TG.GROUP_PATH)
    out = b.items[0][:2] + bytes([1, 0, 0, 0]) + b'\0' + struct.pack('<I', flag) + struct.pack('<I', len(nodes))
    for nd in nodes:
        out += B.pstr(nd)
    out += vol_id + b'\0' * 6
    b.items[0] = out
    return b


def make_group_event(index, start):
    b = B.clone(TG.GROUP_EVENT)
    e = _first(b, 0x104f)
    o = bytes(e.items[0])
    o = o[:4] + struct.pack('<I', index) + o[8:9] + struct.pack('<Q', start) + o[17:]
    e.items[0] = o
    return b


def _event_pos(ev):
    return struct.unpack_from('<Q', bytes(_first(ev, 0x104f).items[0]), 9)[0]


def _set_event_pos(ev, pos):
    e = _first(ev, 0x104f)
    o = bytes(e.items[0])
    e.items[0] = o[:9] + struct.pack('<Q', pos) + o[17:]


def _flat_clone(b):
    from ptx_to_reaper import flat
    return B.clone(flat([b]))


def _series_add(p, tk, anchor, new_blocks):
    """Серия таблицы: счётчик +n (последний байт токена перед первой записью), новые записи — после существующих записей серии."""
    i = ST._find_tok(tk, 'rec', anchor)
    ST._be16_bump(p, tk, i, len(new_blocks))
    j = i + 1
    while j < len(tk) and tk[j][0] == 'rec':
        j += 1
    for k, nb in enumerate(new_blocks):
        tk.insert(j + k, ('rec', nb))


def apply_groups(p, specs, rnd, session_path, vol_name, vol_id):
    """specs: [{'track': имя дорожки, 'start': сэмпл, 'end': сэмпл, 'name': имя или None}] -> число созданных групп.

    Вызывать после записи событий в плейлисты (позиции в плейлисте дорожки — сэмплы, таймбаза Samples)."""
    if not specs:
        return 0
    if p.tokens is None:
        p.tokenize()
    tk = p.tokens
    r262c, r2424, r2426, r2428, r1054 = (B.find_root(p, c) for c in (0x262c, 0x2424, 0x2426, 0x2428, 0x1054))
    r2428_54 = _first(r2428, 0x1054)
    order = [n for n, *_ in TS.aggregate_names(p)]
    tracks = TS.track_blocks(p)
    comps = [c for c in os.path.realpath(session_path).strip('/').split('/') if c]
    nodes = ([comps[1]] + comps[2:]) if os.path.realpath(session_path).startswith('/Volumes/') else ([vol_name] + comps)
    used_names = set()
    made_defs, made_names, made_paths, made_lists = [], [], [], []
    tracks_done = set()
    n_groups = struct.unpack_from('<I', bytes(r262c.items[0]), 2)[0]              # определений групп (по каналу на каждый)
    n_logical = struct.unpack_from('<I', bytes(r2424.items[0]), 2)[0]            # групп как объектов (одна на все каналы)
    n_multi = 0
    n_logical_multi_base = 0
    pl_root = B.find_root(p, 0x1054)

    def plname(t):
        h = bytes(t.items[0])
        return h[6:6 + struct.unpack_from('<I', h, 2)[0]].decode()

    for sp in sorted(specs, key=lambda s: (order.index(s['track']) if s['track'] in order else 1 << 30, s['start'])):
        if sp['track'] not in order:
            continue
        pls = [t for t in B.kids(pl_root, 0x1052) if plname(t) == sp['track']]       # по плейлисту на канал
        sufx = CHAN_SUFFIX.get(len(pls))
        if not sufx:
            continue
        empty = bool(sp.get('empty'))                                # пустая группа (Alt+Cmd+G без звука): ни одного события внутри диапазона
        evs0 = [k for k in pls[0].items if isinstance(k, Block) and k.ctype == 0x1050]
        if empty:
            if any(sp['start'] <= _event_pos(e) < sp['end'] for e in evs0):
                continue
            start, length = sp['start'], sp['end'] - sp['start']
            if length <= 0:
                continue
        else:
            ins0 = [e for e in evs0 if sp['start'] <= _event_pos(e) < sp['end'] + 1]
            if not ins0:
                continue
            start = min(_event_pos(e) for e in ins0)                    # группа — ровно по клипам: границы подгоняются под события внутри
            length = sp['end'] - start
            if any(_event_pos(e) >= start + length + 1 for e in ins0):
                continue
        name = sp.get('name') or f"{sp['track']}.grp"
        k = 0
        base = name
        while name.lower() in used_names:
            k += 1
            name = f'{base}-{k:02d}'
        used_names.add(name.lower())
        first_def = n_groups
        j = n_logical
        for c, pl in enumerate(pls):
            if len(pl.items) == 1 and isinstance(pl.items[0], (bytes, bytearray)) and len(pl.items[0]) >= 6:
                whole = bytes(pl.items[0])                          # плейлист совсем без событий: голова со счётчиком и хвост `01 00` одним сегментом
                pl.items = [whole[:-2], whole[-2:]]
            evs = [e for e in pl.items if isinstance(e, Block) and e.ctype == 0x1050]
            inside = [] if empty else [e for e in evs if sp['start'] <= _event_pos(e) < sp['end'] + 1]
            index = n_groups
            n_groups += 1
            made_defs.append(make_group_def(name + sufx[c], start, length, rnd, index, j))
            inner = B.clone(TG.GROUP_PLAYLIST)                       # содержимое: копии событий, позиция = 1e12 + сэмплы от начала группы
            ev_copies = []
            for e in inside:
                cp = _flat_clone(e)
                _set_event_pos(cp, TICKS_BASE + _event_pos(e) - start)
                ev_copies.append(cp)
            head = bytes(inner.items[0])
            inner.items = [head[:-4] + struct.pack('<I', len(ev_copies))] + ev_copies + [b'\x01\x00']
            made_lists.append(inner)
            if inside:                                               # плейлист канала: события внутри группы -> одно событие-группа
                at = pl.items.index(inside[0])
            else:
                later = [e for e in evs if _event_pos(e) > start]
                at = pl.items.index(later[0]) if later else len(pl.items) - 1
            for e in inside:
                pl.items.remove(e)
            pl.items.insert(at, make_group_event(index, start))
            h = bytes(pl.items[0])
            left = len([x for x in pl.items if isinstance(x, Block) and x.ctype == 0x1050])
            pl.items[0] = h[:-4] + struct.pack('<I', left)
        # объект группы: у моно — с именем определения, у многоканальной — «Group-NN» и код формата в хвосте
        if len(pls) == 1:
            made_names.append(make_group_name(name, j))
        else:
            n_multi += 1
            made_names.append(make_group_name(f'Group-{n_logical_multi_base + n_multi:02d}', j, CHAN_CODE[len(pls)]))
        made_paths.append(make_group_path(nodes, vol_id, first_def))
        n_logical += 1
        tracks_done.add(sp['track'])
    if not made_defs:
        return 0
    # --- корни
    def add_children(root, blocks, tail_len=0):
        h = bytes(root.items[0])
        n = struct.unpack_from('<I', h, 2)[0]
        if len(root.items) == 1 and len(h) == 6 + tail_len:      # пустой корень: `ct count` и хвост одним сегментом
            root.items = [h[:2] + struct.pack('<I', n + len(blocks))] + list(blocks) + ([h[6:]] if tail_len else [])
            return
        root.items[0] = h[:2] + struct.pack('<I', n + len(blocks)) + h[6:]
        at = len(root.items) - (1 if tail_len else 0)
        for k, nb in enumerate(blocks):
            root.items.insert(at + k, nb)
    add_children(r262c, made_defs, 4)
    add_children(r2424, made_names)
    add_children(r2426, made_paths)
    # 0x2428 > 0x1054: голова `54 10 count`, дети 0x1052, затем 0x4828
    h = bytes(r2428_54.items[0])
    r2428_54.items[0] = h[:2] + struct.pack('<I', struct.unpack_from('<I', h, 2)[0] + len(made_lists))
    at = next(i for i, x in enumerate(r2428_54.items) if isinstance(x, Block) and x.ctype == 0x4828)
    for k, nb in enumerate(made_lists):
        r2428_54.items.insert(at + k, nb)
    # --- дорожки: флаг и лишний 0x2038
    for t in sorted(tracks_done):
        c = tracks[order.index(t)]
        x = TS.descend(c, (0x200b, 0), (0x200a, 0), (0x2015, 0))
        i2104 = next(i for i, y in enumerate(x.items) if isinstance(y, Block) and y.ctype == 0x2104)
        raw = bytes(x.items[i2104 + 1])
        j = raw.index(b'\xd4\x30\x00\x00\x00') + 5          # байт-флаг «на дорожке есть группы»; сразу за ним (X 00 00 00) вклинивается лишний 0x2038
        if raw[j] == 0:
            x.items[i2104 + 1:i2104 + 2] = [raw[:j] + b'\x01' + raw[j + 1:j + 4], B.clone(TG.BLOCK_2038), raw[j + 4:]]
    # --- таблица
    _series_add(p, tk, r2424, made_names)
    _series_add(p, tk, r2426, made_paths)
    _series_add(p, tk, r2428_54, made_lists)
    return len(made_defs)
