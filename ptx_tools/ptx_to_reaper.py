#!/usr/bin/env python3
"""ptx_to_reaper.py — сессия Pro Tools (.ptx, 26.4) -> проект Reaper (.rpp) БЕЗ запуска Pro Tools.

    ptx_to_reaper.py session.ptx [--out project.rpp] [--media-dir DIR ...] [--info]

Переносятся: дорожки (в порядке сессии; mute, solo, цвет), клипы (позиция, обрезка, mute), фейд-ин/аут и кроссфейды, клип-гейн,
маркеры-точки. Обратная операция к reaper_to_ptx.py; форматы блоков те же (см. ptx_build.py).
Не переносятся: громкость/панорама/плагины дорожек, автоматизация, диапазонные маркеры, альтернативные плейлисты.
Печатает строки `label: n/total` и итог `Done: ...` (как reaper_to_ptx.py).
"""
import argparse
import json
import re
import math
import os
import struct
import sys
import uuid
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ptx_model import PTX, Block          # noqa: E402
import ptx_trackstate as TS               # noqa: E402
import ptx_sends as SN                    # noqa: E402
import ptx_plugins as PLG                 # noqa: E402
import reaper_fx as FX                    # noqa: E402

TICKS_BASE = 10 ** 12                      # позиция дорожки с таймбазой Ticks: 1e12 + 40 * сэмплы (48 кГц)
FOLDER_NOTES = {'folder': 'Pro Tools: Basic Folder', 'routing': 'Pro Tools: Routing Folder'}
KIND_NAMES = {0x261e: 'aux', 0x261f: 'VCA', 0x2621: 'instrument', 0x2623: 'video'}
COMMON_HEAD = bytes.fromhex('feff00000000ffff04000400') + b'\0' * 21
GAIN_STRIDE = 30
NEG_INF_DB = -200.0


# ---------------------------------------------------------------- разбор ----

def find_all(items, ctype):
    for x in items:
        if isinstance(x, Block):
            if x.ctype == ctype:
                yield x
            yield from find_all(x.items, ctype)


def root(p, ctype):
    for r in p.roots:
        if isinstance(r, Block) and r.ctype == ctype:
            return r
    return None


def kids(b, ctype):
    return [i for i in b.items if isinstance(i, Block) and i.ctype == ctype]


def flat(items):
    """Байты содержимого блока целиком: случайные байты внутри полей разобраны как «блоки» — склеиваем обратно."""
    out = bytearray()
    for x in items:
        if isinstance(x, Block):
            out += b'\x5a' + struct.pack('<HI', x.btype, x.size()) + flat(x.items)
        else:
            out += x
    return bytes(out)


def rd_pstr(buf, pos):
    n = struct.unpack_from('<I', buf, pos)[0]
    if n > 4096 or pos + 4 + n > len(buf):
        raise ValueError('bad string')
    return buf[pos + 4:pos + 4 + n].decode('utf-8', 'replace'), pos + 4 + n


def le(b):
    return int.from_bytes(b, 'little')


def session_rate(p):
    b = root(p, 0x1028)
    if b is None or len(b.items[0]) < 8:
        return 48000
    rate = struct.unpack_from('<I', b.items[0], 4)[0]
    return rate if 8000 <= rate <= 384000 else 48000


class Catalog:
    """0x1004>0x103a: папки с файлами и «хвосты» путей (формат — make_catalog_groups в ptx_build.py)."""

    def __init__(self, p, root_ctype=0x1004):
        self.files = []            # [(имя, индекс группы)]
        self.groups = []           # [имя папки]
        self.tails = []            # [(узлы, терминал)]
        self.inline_tails = []     # [(индекс файла, путь)] — пути внешних файлов, записанные среди записей группы
        r1004 = root(p, root_ctype)
        cat = kids(r1004, 0x103a)[0] if r1004 is not None and kids(r1004, 0x103a) else None
        if cat is None or not cat.items or not isinstance(cat.items[0], (bytes, bytearray)):
            return
        buf = bytes(cat.items[0])
        try:
            self._parse(buf)
        except (ValueError, struct.error, IndexError):
            pass

    @staticmethod
    def _strict_tails(buf, pos):
        """Хвосты путей, идущие ПОСЛЕ всех групп (формат Pro Tools 2025+/26): `00 ff ff ff ff`, узлы `метка 4б 01 u32`, терминал.
        Возвращает список хвостов, только если они занимают буфер до конца; иначе None."""
        tails = []
        try:
            while pos < len(buf):
                if buf[pos:pos + 5] != b'\x00\xff\xff\xff\xff':
                    return None
                pos += 5
                nodes = []
                while True:
                    label, pos = rd_pstr(buf, pos)
                    pos += 4
                    if pos < len(buf) and buf[pos] == 1:
                        nodes.append(label)
                        pos += 5
                    else:
                        tails.append((nodes, label))
                        break
        except (ValueError, struct.error, IndexError):
            return None
        return tails if pos == len(buf) and tails else None

    def _parse(self, buf):
        pos = 2 + 4 + 1 + 4                                   # ctype, C1, 01, C2
        while pos < len(buf):
            if buf[pos] == 1:                                 # маркер следующей группы: 01 <u32>
                pos += 5
            elif buf[pos] == 0:
                break
            folder, pos = rd_pstr(buf, pos)
            pos += 4
            self.groups.append(folder)
            while pos < len(buf) and buf[pos] in (0, 2):
                if buf[pos:pos + 5] == b'\x00\xff\xff\xff\xff':
                    tl = self._strict_tails(buf, pos)
                    if tl is not None:                                  # хвосты всех групп после списка групп
                        self.tails = tl
                        return
                    # путь внешнего файла прямо среди записей (сессии Windows / старых версий)
                    pos += 5
                    label, pos = rd_pstr(buf, pos)
                    pos += 4
                    self.inline_tails.append((len(self.files) - 1, label))
                    continue
                if buf[pos] != 2:
                    break
                pos += 5
                name, pos = rd_pstr(buf, pos)
                if not re.fullmatch(rb'[A-Za-z0-9]{4}', buf[pos:pos + 4]):          # EVAW (wav), VooM (mov), 4gpm (mp4) …
                    raise ValueError('no format tag')
                pos += 4
                self.files.append((name, len(self.groups) - 1))
        while pos + 5 <= len(buf) and buf[pos:pos + 5] == b'\x00\xff\xff\xff\xff':
            pos += 5
            nodes = []
            while True:
                label, pos = rd_pstr(buf, pos)
                pos += 4
                if pos < len(buf) and buf[pos] == 1:
                    nodes.append(label)
                    pos += 5
                else:
                    self.tails.append((nodes, label))
                    break

    def dir_candidates(self, group, ptx_dir):
        """Возможные папки группы: рядом с сессией и восстановленные из хвоста пути."""
        out = []
        folder = self.groups[group] if group < len(self.groups) else 'Audio Files'
        out.append(os.path.join(ptx_dir, folder))
        tail = None
        if len(self.tails) == len(self.groups) and self.groups:
            # хвосты идут в порядке «папка сессии (Audio Files) первой, потом остальные по порядку»
            order = sorted(range(len(self.groups)), key=lambda i: (0 if self.groups[i] == 'Audio Files' else 1, i))
            tail = self.tails[order.index(group)] if group in order else None
        elif len(self.tails) == 1:
            tail = self.tails[0]
        elif group < len(self.tails):
            tail = self.tails[group]
        if tail:
            nodes, terminal = tail
            if nodes and nodes[0] != 'Macintosh HD':          # не загрузочный том: /Volumes/<том>/...
                base = '/Volumes/' + '/'.join(nodes + [terminal])
            else:
                base = '/' + '/'.join(nodes[1:] + [terminal])
            out.append(os.path.join(base, folder))
        return out


class Region:
    def __init__(self, name, file_index, offset, length, gain_index, whole, stretch=None):
        self.name, self.file_index, self.offset, self.length = name, file_index, offset, length
        self.gain_index, self.whole = gain_index, whole
        self.stretch = stretch                                   # длина на таймлайне у клипа с Elastic Audio (иначе None)


def parse_region(b2629):
    """0x2629 -> Region или None, если формат не распознан."""
    nm = kids(b2629, 0x2628)
    if not nm:
        return None
    el = [x for x in nm[0].items if isinstance(x, Block) and x.ctype == 0x2636]
    stretch = None
    if el:                                                       # Elastic Audio: 0x2636 — u64 начало, u64 начало клипа, u32 длина источника, u32 конец
        eb = TS.own_bytes(el[0])
        k = 2 + 16 + 18
        if len(eb) >= k + 24:
            _, new_start, _, new_end = struct.unpack_from('<QQII', eb, k)
            stretch = new_end - new_start
    head = b''.join(x for x in nm[0].items if not isinstance(x, Block)) if el else flat(nm[0].items)
    name, pos = rd_pstr(head, 2)
    t = head[pos:]
    if len(t) < 5 + 12:
        return None
    # V | ow<<4 | lw<<4 | C | 08 | offset | length | ts1 | ts2 | ff*8 (подклип);  00 00 | lw<<4 | C | 00 | length | ts1 | ts2 (целый файл).
    # C: младший нибл — ширина первой копии метки времени клипа, старший — второй (0 — копий нет).
    if t[4] == 8:
        ow, lw = t[1] >> 4, t[2] >> 4
        q = 5
        offset = le(t[q:q + ow]) if ow else 0
        q += ow
        length = le(t[q:q + lw])
        q += lw + (t[3] & 15) + (t[3] >> 4) + 8
        whole = False
    elif t[0:2] == b'\0\0' and t[4] == 0 and (t[2] >> 4) in (2, 3, 4):
        lw = t[2] >> 4
        offset, length, whole = 0, le(t[5:5 + lw]), True
        q = 5 + lw + (t[3] & 15) + (t[3] >> 4)
    else:
        return None
    if t[q:q + 2] != b'\xfe\xff':                                # общий хвост: fe ff ... (у региона из PT бывают варианты)
        return None
    gain = -1
    if t[q + 3:q + 4] == b'\x01':
        t = t[:q + 3] + b'\x00' + t[q + 4:]                      # флаг Elastic Audio
    if t[q:q + len(COMMON_HEAD)] == COMMON_HEAD and len(t) >= q + len(COMMON_HEAD) + 4:
        gain = struct.unpack_from('<i', t, q + len(COMMON_HEAD))[0]
    body = flat(b2629.items)                                   # ct | 0x2628 (заголовок+тело) | ident48 | ...
    ident = struct.unpack_from('<I', body, 2 + 7 + nm[0].size())[0]
    return Region(name, ident, offset, length, gain, whole, stretch if stretch and stretch > 0 else None)


def parse_regions(p):
    r = root(p, 0x262a)
    out = []
    if r is not None:
        for b in kids(r, 0x2629):
            try:
                out.append(parse_region(b))
            except (ValueError, struct.error, IndexError):
                out.append(None)
    return out


GAIN_MARK = bytes.fromhex('01 46 01 00')


def parse_groups(p):
    """Clip Groups (в том числе пустые, Alt+Cmd+G): корень 0x262c > 0x262b > 0x2628 — заголовок как у региона (pstr имя, `V ow|lw C 08`, смещение, длина).
    Возвращает [(имя, длина в сэмплах)]; индекс = идентификатор клипа-группы в событии плейлиста (идентификатор >= числа аудио-регионов)."""
    out = []
    r = root(p, 0x262c)
    for b in (kids(r, 0x262b) if r is not None else []):
        nm = kids(b, 0x2628)
        if not nm:
            out.append(('', 0))
            continue
        head = bytes(nm[0].items[0])
        try:
            name, pos = rd_pstr(head, 2)
            t = head[pos:]
            ln = 0
            if len(t) > 8 and t[4] == 8:
                ow, lw = t[1] >> 4, t[2] >> 4
                ln = le(t[5 + ow:5 + ow + lw])
            out.append((name, ln))
        except (ValueError, struct.error, IndexError):
            out.append(('', 0))
    return out


def parse_mix_groups(p):
    """Группы миксера: корень 0x202b > 0x202a (список записан дважды — берём первую семью).
    Запись: `2a 20 | pstr имя | 02 | u32 flagsA | u16 flagsB | u32 число участников | u16 индексы дорожек... | fe ff | 8 байт | u16 индекс VCA-мастера (ffff — нет)`.
    Группа <ALL> другого вида (flagsA = ffffffff) — пропускается. Возвращает [{'name', 'members': [индексы дорожек], 'vca': индекс | None}]."""
    r = root(p, 0x202b)
    out, seen = [], set()
    for b in (kids(r, 0x202a) if r is not None else []):
        o = flat(b.items)
        try:
            name, pos = rd_pstr(o, 2)
            if o[pos] != 2 or o[pos + 1:pos + 5] == b'\xff\xff\xff\xff':
                continue
            q = pos + 1 + 4 + 2
            cnt = struct.unpack_from('<I', o, q)[0]
            q += 4
            members = [struct.unpack_from('<H', o, q + 2 * k)[0] for k in range(cnt)]
            q += 2 * cnt
            if o[q:q + 2] != b'\xfe\xff':
                continue
            vca = struct.unpack_from('<H', o, q + 2 + 8)[0]
        except (ValueError, struct.error, IndexError):
            continue
        key = (name, tuple(members), vca)
        if key in seen:
            continue
        seen.add(key)
        out.append({'name': name, 'members': members, 'vca': None if vca == 0xFFFF else vca})
    return out


def read_vca(tb):
    """Состояние VCA-мастера (контейнер 0x261f; места найдены по эталонам PT через PTSL: VCA1 с фейдером -6 дБ, mute и solo):
    фейдер — int32 десятых дБ по смещению 2 блока 0x2514 (копия по 11); mute — байт по смещению 6 того же блока; solo — первый байт последнего
    сырого сегмента блока 0x102d; автоматизация: 0x2610 > 0x260a #0 — громкость, #1 — mute (точки u32 время | i16 значение).
    Возвращает {'volume_db', 'mute', 'solo', 'auto': [(сэмпл, десятые дБ)] | None, 'auto_mute': [...] | None}."""
    import ptx_sends as SN
    out = {'volume_db': None, 'mute': False, 'solo': False, 'auto': None, 'auto_mute': None}
    b2610 = kids(tb, 0x2610)
    if b2610:
        f = kids(b2610[0], 0x2514)
        if f:
            o = bytes(f[0].items[0])
            if len(o) > 7:
                v = struct.unpack_from('<i', o, 2)[0]
                out['volume_db'] = None if abs(v) < 1 else (-144.0 if v <= -1440 else v / 10.0)
                out['mute'] = o[6] != 0
        lanes = kids(b2610[0], 0x260a)
        if lanes:
            out['auto'] = SN._auto_points(lanes[0])
        if len(lanes) > 1:
            out['auto_mute'] = SN._auto_points(lanes[1])
    b102d = kids(tb, 0x102d)
    if b102d and b102d[0].items and isinstance(b102d[0].items[-1], (bytes, bytearray)) and len(b102d[0].items[-1]) >= 7:
        out['solo'] = b102d[0].items[-1][0] != 0
    return out


def parse_gains(p):
    """Список клип-гейнов 0x2637: записи разделены меткой `01 46 01 00 <тип> ...`; запись типа 0x16 — 30 байт, дБ = float в последних 4 байтах.
    Запись типа 0x26 (46 байт) — клип-гейн с огибающей (точки: u32 время, float дБ): статичного значения у неё нет — возвращаем 0.0.
    Раньше список читался с фиксированным шагом 30 байт и после первой «длинной» записи все значения сдвигались."""
    r = root(p, 0x2637)
    if r is None or not r.items:
        return []
    raw = r.items[0]
    n = struct.unpack_from('<I', raw, 2)[0]
    marks, i = [], 6
    while True:
        i = raw.find(GAIN_MARK, i)
        if i < 0:
            break
        marks.append(i)
        i += 4
    out = []
    if len(marks) == n:
        for a, b in zip(marks, marks[1:] + [len(raw)]):
            rec = raw[a:b]
            if len(rec) == GAIN_STRIDE and rec[4] == 0x16:
                out.append(struct.unpack_from('<f', rec, GAIN_STRIDE - 4)[0])
            else:
                out.append(0.0)
        return out
    for i in range(n):                                          # запасной путь: фиксированный шаг
        o = 6 + i * GAIN_STRIDE + 26
        if o + 4 > len(raw):
            break
        out.append(struct.unpack_from('<f', raw, o)[0])
    return out


def parse_gain_envs(p):
    """Огибающие клип-гейна (записи 0x2637 типа != 0x16): {индекс записи: [(время в сэмплах от начала клипа, дБ)]}.

    Запись: метка(4) | тип(1) | 5 нулей | u32 n (= 1 + число точек) | `04 00 xx 00` | u64 id | float статический дБ | (n-1) x (u32 время, float абсолютные дБ).
    Эталон: clipgain.ptx (клип с точками 1 с / 2 с / ~3,09 с). Время считается от начала клипа (проверено для нетримленного клипа в начале сессии)."""
    r = root(p, 0x2637)
    out = {}
    if r is None or not r.items:
        return out
    raw = r.items[0]
    marks, i = [], 6
    while True:
        i = raw.find(GAIN_MARK, i)
        if i < 0:
            break
        marks.append(i)
        i += 4
    for k, (a, b) in enumerate(zip(marks, marks[1:] + [len(raw)])):
        rec = raw[a:b]
        if len(rec) < 38 or rec[4] == 0x16:
            continue
        n = struct.unpack_from('<I', rec, 10)[0]
        if n < 2 or len(rec) < 30 + 8 * (n - 1):
            continue
        out[k] = [(struct.unpack_from('<I', rec, 30 + 8 * j)[0], struct.unpack_from('<f', rec, 34 + 8 * j)[0]) for j in range(n - 1)]
    return out


def parse_geometries(p):
    """0x2630>0x262f -> [(вид, длина в сэмплах, часть до точки склейки)]; вид: in / out / xfade / None.

    Формат (Pro Tools 26.4): ct | 00*5 | B | 00 00 | длины | маркер | ...  Байт B = (hi << 4) | lo — ширины полей длины.
    Маркер после длин: 03 — фейд-ин (одна длина шириной hi); 02 — фейд-аут (две одинаковые длины шириной hi и lo);
    01 — кроссфейд (сначала pre шириной lo — часть до точки склейки, затем total шириной hi)."""
    r = root(p, 0x2630)
    out = []
    if r is not None:
        for g in kids(r, 0x262f):
            o = g.items[0]
            res = (None, 0, 0)
            if len(o) > 12:
                hi, lo = o[7] >> 4, o[7] & 15
                if 0 < hi <= 4 and lo <= 4:
                    m_in = 10 + hi
                    m_out = 10 + hi + lo
                    if lo == 0 and len(o) > m_in and o[m_in] == 3:
                        res = ('in', le(o[10:10 + hi]), 0)
                    elif lo > 0 and len(o) > m_out and o[m_out] == 2:
                        res = ('out', le(o[10:10 + hi]), 0)
                    elif len(o) > m_out and o[m_out] == 1:
                        res = ('xfade', le(o[10 + lo:10 + lo + hi]), le(o[10:10 + lo]))
            out.append(res)
    return out


def track_names(p):
    r = root(p, 0x2519)
    names = []
    if r is not None:
        for x in r.items:
            if isinstance(x, Block) and x.ctype == 0x251a:
                o = x.items[0]
                try:
                    nm, _ = rd_pstr(o, 4)
                except ValueError:
                    continue
                if nm in names:                                # вторая семья 0x251a повторяет первую
                    break
                names.append(nm)
    return names


def parse_tracks(p):
    """Все дорожки сессии по порядку.

    [{'kind': 'audio'|'folder'|'other', 'name', 'id', 'playlists': [имена] (аудио), 'parent': id папки | None,
      'depth': int, 'state': {...}}]
    Папки: контейнер 0x263b, состав — список 0x210c (владелец, число, id детей); вложенность любая.
    Плейлисты дорожки: u16-список индексов в контейнере 0x261c (имя активного = имени дорожки, остальные — альтернативные).
    """
    cont = TS.track_blocks(p)
    agg = TS.aggregate_names(p)
    states = TS.read_states(p)
    # имена плейлистов по записям 0x1014 (индекс в контейнере — номер записи 0x1014; у многоканальной дорожки
    # запись ссылается на плейлисты каналов, все с именем дорожки)
    pl_names = []
    r1015 = root(p, 0x1015)
    if r1015 is not None:
        for e in kids(r1015, 0x1014):
            f = e.items[0]
            n = struct.unpack_from('<I', f, 2)[0]
            pl_names.append(f[6:6 + n].decode('utf-8', 'replace'))
    tracks = []
    for i, c in enumerate(cont):
        name, a1, _, end = agg[i] if i < len(agg) else (f'Track {i + 1}', None, None, 0)
        tid = TS.track_id(a1, end).hex() if a1 is not None else ''
        kind = {0x261c: 'audio', 0x263b: 'folder'}.get(c.ctype, 'other')
        pls = []
        if kind == 'audio' and len(c.items) > 2 and isinstance(c.items[2], (bytes, bytearray)):
            seg = bytes(c.items[2])                         # после блока 0x261b: idx u32 | n u16 | n x (pl u16, 0 u16) | ff ff
            try:
                n = struct.unpack_from('<H', seg, 4)[0]
                for k in range(n):
                    idx = struct.unpack_from('<H', seg, 6 + 4 * k)[0]
                    if idx < len(pl_names):
                        pls.append(pl_names[idx])
            except struct.error:
                pls = []
        nch = 2
        if kind == 'audio' and a1 is not None:
            code = TS.own_bytes(a1)[end]
            nch = {0: 1, 1: 2, 6: 6}.get(code, 2)
        tracks.append({'kind': kind, 'name': name, 'id': tid, 'playlists': pls, 'parent': None, 'depth': 0, 'nchan': nch,
                       'state': states[i] if i < len(states) else {}, 'ctype': c.ctype})
    # папки: 0x210c = n записей [владелец, число детей, дети...], id в виде 5 байт + `2a 00 00 00` + 8 байт.
    # У части дорожек id лежит не сразу за именем (после другого идентификатора), поэтому id из 0x210c
    # сопоставляем с дорожкой по любому 8-байтному окну её блока 0x251a.
    window = {}
    for i, c in enumerate(cont):
        if i < len(agg):
            ob = TS.own_bytes(agg[i][1])
            for k in range(agg[i][3], len(ob) - 7):
                window.setdefault(ob[k:k + 8].hex(), i)
    b = root(p, 0x210c)
    if b is not None:
        raw = flat(b.items)
        try:
            pos = 2
            nrec = struct.unpack_from('<I', raw, pos)[0]
            pos += 4
            for _ in range(nrec):
                owner = window.get(raw[pos + 9:pos + 17].hex())
                pos += 17
                n = struct.unpack_from('<I', raw, pos)[0]
                pos += 4
                for _ in range(n):
                    child = window.get(raw[pos + 9:pos + 17].hex())
                    pos += 17
                    if child is not None and owner is not None:
                        tracks[child]['parent'] = tracks[owner]['id'] or str(owner)
                        tracks[owner]['id'] = tracks[owner]['id'] or str(owner)
        except struct.error:
            pass
    by_id = {t['id']: t for t in tracks}
    owners = {t['parent'] for t in tracks if t['parent']}
    for t in tracks:
        if t['ctype'] == 0x261e and t['id'] in owners:        # aux-дорожка с детьми = Routing Folder
            t['kind'] = 'routing'
    for t in tracks:
        d, q = 0, t['parent']
        while q and q in by_id and d < 32:
            d += 1
            q = by_id[q]['parent']
        t['depth'] = d
    for t in tracks:                                            # цвет «по умолчанию» PT -> цвет, который Pro Tools показывает
        st = t['state']
        if st.get('color') is None and st.get('color_raw') is not None:
            dc = default_color(st['color_raw'], t['kind'], t['ctype'])
            if dc is not None:
                st['color'] = dc
    return tracks


def playlist_block_names(p):
    """Имена плейлистов 0x1052 по порядку блоков. У части блоков (многоканальные дорожки с длинными именами) имя в самом блоке пустое —
    тогда оно берётся из записи 0x1014 (после имени: u8, u32 число каналов, u16 индексы блоков 0x1052 по каналам)."""
    r = root(p, 0x1054)
    blocks = kids(r, 0x1052) if r is not None else []
    names = []
    for t in blocks:
        hdr = t.items[0]
        n = struct.unpack_from('<I', hdr, 2)[0]
        names.append(hdr[6:6 + n].decode('utf-8', 'replace'))
    r1015 = root(p, 0x1015)
    if r1015 is not None:
        for e in kids(r1015, 0x1014):
            f = e.items[0]
            try:
                n = struct.unpack_from('<I', f, 2)[0]
                nm = f[6:6 + n].decode('utf-8', 'replace')
                q = 6 + n
                cnt = struct.unpack_from('<I', f, q + 1)[0]
                for k in range(min(cnt, 64)):
                    i = struct.unpack_from('<H', f, q + 5 + 2 * k)[0]
                    if i < len(names) and not names[i]:
                        names[i] = nm
            except struct.error:
                continue
    return names


def parse_playlists(p):
    """{имя: [(позиция, тип, ident, mute)]} в порядке следования событий."""
    out = {}
    r = root(p, 0x1054)
    if r is None:
        return out
    bnames = playlist_block_names(p)
    for bi, t in enumerate(kids(r, 0x1052)):
        out[bnames[bi]] = _playlist_events(t)
    return out


def _playlist_events(t, unit=40):
    """События одного плейлиста (блок 0x1052): [(позиция, тип, ident, mute, цвет)]; тип 1 — фейд, 3 — аудио-клип, 5 — клип-группа."""
    ev = []
    for e in kids(t, 0x1050):
        tail = e.items[-1] if e.items and isinstance(e.items[-1], (bytes, bytearray)) else b''
        is_fade = len(tail) >= 1 and tail[0] == 1            # хвост после 0x104f: 01 .. — фейд, 00 .. — клип
        for q in kids(e, 0x104f):
            o = q.items[0]
            pos = struct.unpack_from('<Q', o, 9)[0]
            if pos >= TICKS_BASE:
                pos = (pos - TICKS_BASE) // unit
            craw = struct.unpack_from('<H', o, 18)[0] if len(o) >= 20 else 0xFFFE          # цвет клипа: индекс палитры (0xfffe — по умолчанию)
            is_group = (not is_fade) and len(o) > 20 and o[20] == 1                  # клип-группа (Alt+Cmd+G): после `fe ff` стоит 01 00 00 00 (у аудио-клипа — нули)
            ev.append((pos, 1 if is_fade else (5 if is_group else 3), struct.unpack_from('<I', o, 4)[0], bool(o[2]), craw))
    return ev


def parse_group_playlists(p):
    """Содержимое клип-групп: корень 0x2428 > 0x1054 > 0x1052 — по плейлисту на группу (порядок как в 0x262c); события в том же формате,
    позиции отсчитываются от начала группы и хранятся в сэмплах (в обычных плейлистах — в 1/40 сэмпла)."""
    r = root(p, 0x2428)
    r54 = kids(r, 0x1054) if r is not None else []
    return [_playlist_events(t, 1) for t in (kids(r54[0], 0x1052) if r54 else [])]


def flat_bytes(b):
    out = b''
    for x in b.items:
        out += flat_bytes(x) if isinstance(x, Block) else bytes(x)
    return out


def parse_video(p):
    """Видео сессии: [{'name', 'fps', 'start' (с), 'length' (с), 'mute', 'file_index'}].

    Файлы — каталог 0x1006>0x103a; описание файла — 0x1006>0x1005 (после метки `7f 7f 2a 80`: double fps, u32 число кадров); регион —
    0x262e>0x262d>0x2628 (имя pstr, затем `00 02 20 xx 00`, длина u16 в кадрах); положение на таймлайне — плейлист 0x1055>0x1053>0x1051>0x104f
    (u32 по смещению 9 — кадры от начала сессии, o[2] — mute)."""
    out = []
    r1006, r1055, r262e = root(p, 0x1006), root(p, 0x1055), root(p, 0x262e)
    if r1006 is None or r1055 is None:
        return out
    fps = 0.0
    d5 = kids(r1006, 0x1005)
    if d5:
        raw = flat_bytes(d5[0])
        k = raw.rfind(b'\x7f\x7f\x2a\x80')
        if k >= 0 and len(raw) >= k + 12:
            fps = struct.unpack_from('<d', raw, k + 4)[0]
    if not 10 < fps < 130:
        return out
    regions = {}
    for b in (kids(r262e, 0x262d) if r262e is not None else []):
        for rg in kids(b, 0x2628):
            o = bytes(rg.items[0])
            n = struct.unpack_from('<I', o, 2)[0]
            name = o[6:6 + n].decode('utf-8', 'replace')
            q = 6 + n
            if o[q:q + 2] == b'\x00\x02' and (o[q + 2] >> 4) in (2, 3, 4) and (o[q + 2] & 15) == 0:     # третий байт: ширина поля длины в старшем ниббле (0x20 — u16, 0x30 — u24)
                w = o[q + 2] >> 4
                regions[name] = int.from_bytes(o[q + 5:q + 5 + w], 'little')
    for pl in kids(r1055, 0x1053):
        h = bytes(pl.items[0])
        n = struct.unpack_from('<I', h, 2)[0]
        name = h[6:6 + n].decode('utf-8', 'replace')
        for e in kids(pl, 0x1051):
            for q in kids(e, 0x104f):
                o = bytes(q.items[0])
                pos = struct.unpack_from('<I', o, 9)[0]
                frames = regions.get(name) or (next(iter(regions.values())) if regions else 0)
                out.append({'name': name, 'fps': fps, 'start': pos / fps, 'length': frames / fps, 'mute': bool(o[2]), 'file_index': 0})
    return out


# Цвет маркера: RGB лежит в первом дочернем блоке 0x4826 (01 | R | G | B — по u16 BE, значение в младшем байте); цвет по умолчанию Pro Tools — зелёный 00db32.
MARKER_DEFAULT_RGB = 0x00db32
_MARKER_COMMENT_PAT = bytes.fromhex('ffffffff04000000000000000100000000')


def marker_comment(own):
    """Комментарий памяти-локации (pstr после `ff ff ff ff 04 00 00 00 00 00 00 00 01 00 00 00 00`) по «собственным» байтам блока 0x2077."""
    j = own.find(_MARKER_COMMENT_PAT, 60)
    if j < 0:
        return ''
    try:
        n = struct.unpack_from('<I', own, j + 17)[0]
        if 0 < n < 4096 and j + 21 + n <= len(own):
            return own[j + 21:j + 21 + n].split(b'\x00')[0].decode('utf-8', 'replace')
    except struct.error:
        pass
    return ''


def marker_rgb(m):
    for k in kids(m, 0x4826):
        o = b''.join(bytes(x) for x in k.items if isinstance(x, (bytes, bytearray)))
        if len(o) >= 9 and o[2] == 1:
            return (o[4] << 16) | (o[6] << 8) | o[8]
        return None
    return None


def parse_memory_locations(p):
    """Memory Locations сессии (0x2030 > 0x2077): [{'idx', 'name', 'start', 'end' (сэмплы), 'prop' ('marker' | 'selection' | 'none'), 'track'}].

    prop — младшие 2 бита байта 5 заголовка (1 — Marker, 2 — Selection, 0 — None: только настройки вида, без времени); track — индекс дорожки
    (порядок контейнеров сессии) для маркера на дорожке, иначе None: ссылка лежит в последних 4 байтах первого «сырого» куска блока (ffffffff — линейка)."""
    r = root(p, 0x2030)
    out = []
    if r is None:
        return out
    for m in kids(r, 0x2077):
        h = bytes(m.items[0])
        own = b''.join(bytes(x) for x in m.items if isinstance(x, (bytes, bytearray)))
        try:
            idx = struct.unpack_from('<H', h, 2)[0]
            name, pos = rd_pstr(h, 8)
            start, end = struct.unpack_from('<QQ', h, pos)
        except (ValueError, struct.error):
            continue
        prop = {1: 'marker', 2: 'selection'}.get(h[5] & 3, 'none')
        ref = None
        for x in m.items[1:]:
            if isinstance(x, (bytes, bytearray)) and len(x) >= 51:
                v = struct.unpack_from('<I', x, len(x) - 4)[0]
                ref = None if v == 0xFFFFFFFF else v
                break
        comment = marker_comment(own)
        color = marker_rgb(m)
        if color == MARKER_DEFAULT_RGB:
            color = None                                     # цвет по умолчанию: не красим
        out.append({'idx': idx, 'name': name, 'start': start, 'end': end if prop == 'selection' and end > start else None, 'prop': prop, 'track': ref,
                    'color': color, 'comment': comment})
    return out


def parse_markers(p):
    """Только маркеры на главной линейке: [(idx, имя, позиция в сэмплах)]."""
    return [(m['idx'], m['name'], m['start']) for m in parse_memory_locations(p) if m['prop'] == 'marker' and m['track'] is None]


# ------------------------------------------------------------ сборка клипов --

class EmptyItem:
    """Clip Group Pro Tools без звука -> пустой item Reaper (без источника) с именем группы."""
    empty = True

    def __init__(self, start, length, name):
        self.start, self.length, self.display = start, length, name
        self.mute, self.color, self.gain_lin = False, None, 1.0
        self.offset, self.rate, self.fade_in, self.fade_out = 0, 1.0, 0, 0


class Clip:
    def __init__(self, start, region, mute):
        self.start, self.region, self.mute = start, region, mute
        self.offset, self.length = region.offset, region.stretch or region.length
        self.rate = region.length / self.length if region.stretch else 1.0            # скорость: источник / таймлайн
        self.fade_in = self.fade_out = 0
        self.color = None                                              # цвет клипа Pro Tools (0xRRGGBB) или None

    @property
    def end(self):
        return self.start + self.length


def build_clips(events, regions, geoms):
    clips, pending_in, stats = [], {}, {'skipped': 0}
    for pos, typ, ident, mute, craw in events:
        if typ in (2, 3):                                      # аудио-клип (2 — клип с другим флагом)
            rg = regions[ident] if ident < len(regions) else None
            if rg is None:
                stats['skipped'] += 1
                continue
            c = Clip(pos, rg, mute)
            c.color = TS.color_from_raw(craw) if craw != 0xFFFE else None
            fi = pending_in.pop(pos, None)
            if fi:
                total, pre = fi
                pre = min(pre, c.offset)                       # правый клип начинается раньше точки склейки на pre
                c.start -= pre
                c.offset -= pre
                c.length += pre
                c.fade_in = total
            clips.append(c)
        elif typ == 1 and ident < len(geoms):                  # фейд
            kind, ln, pre = geoms[ident]
            if kind == 'in':
                pending_in[pos] = (ln, 0)
            elif kind == 'out':
                left = next((c for c in reversed(clips) if c.end == pos), None)
                if left:
                    left.fade_out = ln
            elif kind == 'xfade':
                left = next((c for c in reversed(clips) if c.end == pos), None)
                if left:                                       # левый клип заканчивается в точке склейки
                    left.length += max(0, ln - pre)
                    left.fade_out = ln
                pending_in[pos] = (ln, pre)
    return clips, stats


# ------------------------------------------------------------------ файлы ----

def wav_rate(path, default):
    try:
        with wave.open(path, 'rb') as w:
            return w.getframerate()
    except Exception:
        return default


_NAME_INDEX = {}


def _name_index(root, depth=3, limit=20000):
    """{имя файла (нижний регистр): путь} для папки root на глубину depth (кэш на запуск; огромные деревья не обходим)."""
    if root in _NAME_INDEX:
        return _NAME_INDEX[root]
    out, n = {}, 0
    if root and os.path.isdir(root):
        base = root.rstrip('/').count('/')
        for dp, dns, fns in os.walk(root):
            if dp.count('/') - base >= depth:
                dns[:] = []
            dns[:] = [d for d in dns if not d.startswith('.') and d not in ('Session File Backups', 'Backups')]
            for fn in fns:
                out.setdefault(fn.lower(), os.path.join(dp, fn))
                n += 1
            if n > limit:
                break
    _NAME_INDEX[root] = out
    return out


def resolve_file(cat, index, ptx_dir, media_dirs, cache, relink=None):
    if index in cache:
        return cache[index]
    name, grp = cat.files[index]
    found = None
    cands = [os.path.join(d, name) for d in cat.dir_candidates(grp, ptx_dir)]
    expected = cands[1 if len(cands) > 1 else 0]               # путь, под которым файл числится отсутствующим
    if relink and relink.get(expected) and os.path.isfile(relink[expected]):          # ручная привязка (Relink)
        cache[index] = (relink[expected], True)
        return cache[index]
    cands += [os.path.join(d, name) for d in media_dirs]
    for c in cands:
        if os.path.isfile(c):
            found = c
            break
    if not found:                                              # как Pro Tools: ищет файл по имени в папке сессии и рядом с записанным путём (файлы могли переехать)
        roots = [ptx_dir] + [os.path.dirname(os.path.dirname(c)) if os.path.basename(os.path.dirname(c)) else os.path.dirname(c) for c in cands[1:2]]
        recorded = os.path.dirname(cands[1]) if len(cands) > 1 else None
        if recorded:
            roots.insert(1, recorded)
        for r_ in roots:
            hit = _name_index(r_).get(name.lower())
            if hit:
                found = hit
                break
    if not found:
        for d in media_dirs:                                   # поиск по имени вглубь
            for dp, _, fns in os.walk(d):
                if name in fns:
                    found = os.path.join(dp, name)
                    break
            if found:
                break
    cache[index] = (found or expected, bool(found))
    return cache[index]


# ---------------------------------------------------------------- вывод rpp --

def q(s):
    return '"' + s.replace('"', "'") + '"'


def guid():
    return '{' + str(uuid.uuid4()).upper() + '}'


def num(x):
    return repr(round(x, 12)) if x else '0'


_OFFSET = 0.0                                                   # смещение всех времён (с), задаётся в write_rpp


def env_block(tag, pts, shape):
    """Блок огибающей Reaper (<VOLENV2 / PANENV2 / MUTEENV / AUXVOLENV ...>); pts — [(время c, значение)]."""
    L = [f'    <{tag}', f'      EGUID {guid()}', '      ACT 1 -1', '      VIS 1 1 1', '      LANEHEIGHT 0 0', '      ARM 1',
         f'      DEFSHAPE {shape} -1 -1']
    L += [f'      PT {num(t + _OFFSET)} {num(v)} {shape}' for t, v in pts]
    L.append('    >')
    return L


def write_rpp(path, rate, tracks, markers, master=None, tc=None, counters=None, regions=(), offset=0.0, bpm=None):
    global _OFFSET
    _OFFSET = offset
    """tracks: [{'name', 'items', 'state', 'folder': bool, 'depth': int}] в порядке проекта (папка перед детьми)."""
    L = ['<REAPER_PROJECT 0.1 "7.0/macOS-arm64" 1700000000', f'  SAMPLERATE {rate} 0 0',
         '  TIMEMODE 0 5 5 30 0 -1 -1', '  PANMODE 3']
    if bpm:
        L.append(f'  TEMPO {num(bpm)} 4 4 0')                          # темп сессии Pro Tools (постоянный)
    if counters and counters[0]:                             # форматы счётчиков PT -> основная / дополнительная линейка Reaper
        import ptx_settings as _S
        main_ = _S.COUNTER_TO_REAPER.get(counters[0], 1)
        sub_ = _S.COUNTER_TO_REAPER.get(counters[1], -1) if counters[1] else -1
        fps_ = tc[0] if tc else 30
        L[-2] = f'  TIMEMODE {main_} {sub_} -1 {num(fps_)} 0 -1 -1 0'
    if tc:                                                   # частота таймкода и старт сессии Pro Tools -> SMPTESYNC / PROJOFFS
        fps, drop, frames = tc
        L.append(f'  SMPTESYNC 0 {fps} 100 40 1000 300 0 0 {1 if drop else 0} 0 0')
        L.append(f'  PROJOFFS {num(frames / fps)} 0 0')
    notes = []                                               # заметки проекта: тип папок и плагины Pro Tools по дорожкам
    for n, tr in enumerate(tracks, 1):
        if tr.get('note'):
            notes.append(f'Track {n} "{tr["name"]}": {tr["note"]}')
        if tr.get('pt_tag'):                                    # тип и формат дорожки Pro Tools — чтобы вернуть их при обратной конвертации
            notes.append(f'Track {n} "{tr["name"]}": Pro Tools track: {tr["pt_tag"]}')
        for x in tr.get('plugin_note') or []:
            notes.append(f'Track {n} "{tr["name"]}": {x}')
        for x in (tr.get('comment') or '').splitlines():           # комментарий дорожки Pro Tools
            if x.strip():
                notes.append(f'Track {n} "{tr["name"]}": Comment: {x}')
    for x in (master or {}).get('plugin_note') or []:
        notes.append(f'Master: {x}')
    for x in ((master or {}).get('comment') or '').splitlines():
        if x.strip():
            notes.append(f'Master: Comment: {x}')
    for i, (idx, name, sample, end, mcolor, mcomment) in enumerate(markers, 1):        # комментарии маркеров Pro Tools (в Reaper у маркера нет поля комментария) -> заметки проекта
        for x in mcomment.splitlines():
            if x.strip():
                notes.append(f'Marker {i} "{name}": Comment: {x}')
    if notes:
        L += ['  <NOTES 0 2'] + [f'    |{x}' for x in notes] + ['  >']
    for i, (idx, name, sample, end, mcolor, _mc) in enumerate(markers, 1):
        mc = 0x1000000 | ((mcolor >> 16) & 255) | (mcolor & 0xFF00) | ((mcolor & 255) << 16) if mcolor is not None else 0       # COLORREF | 0x1000000
        if end is None:
            L.append(f'  MARKER {i} {num(sample / rate + offset)} {q(name)} 0 {mc} 1 B {guid()} 0')
        else:                                                        # Selection Pro Tools -> регион: начало с именем и строка конца
            L.append(f'  MARKER {i} {num(sample / rate + offset)} {q(name)} 1 {mc} 1 B {guid()} 0')
            L.append(f'  MARKER {i} {num(end / rate + offset)} "" 1')
    for k, (rname, rs, re_) in enumerate(regions, len(markers) + 1):          # регионы (группы клипов без звука): начало с именем и конец
        rg_guid = guid()
        L.append(f'  MARKER {k} {num(rs)} {q(rname)} 1 0 1 B {rg_guid} 0')
        L.append(f'  MARKER {k} {num(re_)} "" 1')
    if master:                                               # родной мастер Reaper: громкость, пан, mute, цвет и цепочка FX
        mst = master.get('state', {})
        mvol, mpan = 1.0, 0.0
        if mst.get('volume_db') is not None:
            mvol = 10 ** (mst['volume_db'] / 20)
        pn = mst.get('pan')
        if isinstance(pn, tuple):
            mpan = max(-1.0, min(1.0, (pn[0] + pn[1]) / 100))
        L.append(f'  MASTER_VOLUME {num(mvol)} {num(mpan)} -1 -1 1')
        L.append(f"  MASTERMUTESOLO {1 if mst.get('mute') else 0}")
        if mst.get('color') is not None:
            c = mst['color']
            L.append(f'  MASTERPEAKCOL {0x1000000 | ((c >> 16) & 255) | (c & 0xFF00) | ((c & 255) << 16)}')
        fxl = list(master.get('fx') or [])
        if fxl:
            fxl[0] = '<MASTERFXLIST'
            L += ['  ' + x for x in fxl]

    for n, tr in enumerate(tracks):
        name, items, st = tr['name'], tr['items'], tr.get('state', {})
        nxt = tracks[n + 1]['depth'] if n + 1 < len(tracks) else 0
        if tr['folder']:
            isbus = '1 1' if nxt > tr['depth'] else '1 0'      # пустая папка не открывает уровень
        elif nxt < tr['depth']:
            isbus = f'2 {nxt - tr["depth"]}'                    # последний в папке: закрывает уровни
        else:
            isbus = '0 0'
        tg = guid()
        L += [f'  <TRACK {tg}', f'    NAME {q(name)}']
        if st.get('color') is not None:                     # COLORREF: R | G<<8 | B<<16, флаг 0x1000000
            c = st['color']
            L.append(f'    PEAKCOL {0x1000000 | ((c >> 16) & 255) | (c & 0xFF00) | ((c & 255) << 16)}')
        lanes = tr.get('lanes')                                  # [(имя lane, items)] — альтернативные плейлисты как Takes & Lanes
        vol, pan = 1.0, 0.0
        if st.get('volume_db') is not None or st.get('pan') is not None:
            vdb = st.get('volume_db')
            vol = 0.0 if vdb is None else 10 ** (vdb / 20)
            pn = st.get('pan')
            if isinstance(pn, tuple):
                pan = max(-1.0, min(1.0, (pn[0] + pn[1]) / 100))      # обратно к (L, R) = (-100 + 100·max(p,0), 100 + 100·min(p,0))
            elif pn is not None:
                pan = pn / 100
        if tr.get('auto'):                                       # автоматизация громкости PT заменяет фейдер: значения абсолютные
            vol = 1.0
        if tr.get('pan_auto'):
            pan = 0.0                                            # автоматизация пана PT заменяет положение пана
        L += [f'    VOLPAN {num(vol) if vol else 0} {num(pan)} -1 -1 1', f"    MUTESOLO {1 if st.get('mute') and not tr.get('mute_auto') else 0} {1 if st.get('solo') else 0} 0",
              f'    ISBUS {isbus}']
        if tr.get('vca_lead') or tr.get('vca_follow'):           # VCA: GROUP_FLAGS — 22 значений, [20] = ведущий VCA-громкости (маска групп), [21] = ведомый
            gf = [0] * 22
            gf[20], gf[21] = tr.get('vca_lead', 0), tr.get('vca_follow', 0)
            L.append('    GROUP_FLAGS ' + ' '.join(str(x) for x in gf))
        if tr.get('closed'):                                     # закрытая папка Pro Tools -> свёрнутая папка Reaper
            L.append('    BUSCOMP 2 0 0 0 0')
        if st.get('rec'):                                        # дорожка в записи
            L.append('    REC 1 0 0 0 0 0 0 0')
        if st.get('hidden'):                                     # скрытая дорожка Pro Tools -> скрыта в списке дорожек и в микшере Reaper
            L.append('    SHOWINMIX 0 0.6667 0.5 0 0.5 0 0 0 0')
        if lanes:
            L += ['    FREEMODE 2', '    FIXEDLANES 9 0 0 0 0', f'    LANESOLO {1 << 0} 0 0 0 0 0 0 0']      # играет lane 0 (активный плейлист)
            L += [f'    LANENAME {k} {q(nm)}' for k, (nm, _) in enumerate(lanes)]
        L += [f'    NCHAN {tr.get("nchan", 2)}', f'    TRACKID {tg}']
        for src, rvol, rpan, rmute, rmode, renv in tr.get('recvs', ()):     # sends Pro Tools -> приёмы (AUXRECV) на принимающей дорожке
            if renv.get('vol'):
                rvol = 1.0                                       # автоматизация заменяет уровень send
            if renv.get('pan'):
                rpan = 0.0
            if renv.get('mute'):
                rmute = False
            L.append(f'    AUXRECV {src} {rmode} {num(rvol)} {num(rpan)} {1 if rmute else 0} 0 0 0 0 -1:U 0 -1 \'\'')
            for key, tag, shape in (('vol', 'AUXVOLENV', 0), ('pan', 'AUXPANENV', 0), ('mute', 'AUXMUTEENV', 1)):
                if renv.get(key):
                    L += env_block(tag, renv[key], shape)
        if tr.get('auto'):
            L += env_block('VOLENV2', tr['auto'], 0)
        if tr.get('pan_auto'):
            L += env_block('PANENV2', tr['pan_auto'], 0)
        if tr.get('mute_auto'):
            L += env_block('MUTEENV', tr['mute_auto'], 1)
        if lanes:
            L.append(f'    ITEMLANES {len(lanes)}')
            items = [(it, k) for k, (_, its) in enumerate(lanes) for it in its]
        else:
            items = [(it, None) for it in items]
        L += ['    ' + x for x in (tr.get('fx') or [])]
        for v in tr.get('video_items') or ():
            L += ['    <ITEM', f'      POSITION {num(v["start"] + offset)}', '      SNAPOFFS 0', f'      LENGTH {num(v["length"])}', '      LOOP 0', '      ALLTAKES 0',
                  f'      FADEIN 1 0 0 1 0 0 0', f'      FADEOUT 1 0 0 1 0 0 0', f'      MUTE {1 if v["mute"] else 0} 0', '      SEL 0',
                  f'      IGUID {guid()}', f'      NAME {q(os.path.basename(v["file"]) or v["name"])}', '      VOLPAN 1 0 1 -1', '      SOFFS 0',
                  '      CHANMODE 0', f'      GUID {guid()}', '      <SOURCE VIDEO', f'        FILE {q(v["file"])}', '      >', '    >']
        for it, lane in items:
            c, (fpath, frate), pos_s = it
            if getattr(c, 'empty', False):                                # пустой item (Clip Group Pro Tools): без источника, имя группы — в заметках item
                L += ['    <ITEM', f'      POSITION {num(c.start / rate + offset)}', '      SNAPOFFS 0', f'      LENGTH {num(c.length / rate)}', '      LOOP 0',
                      '      ALLTAKES 0', '      FADEIN 1 0 0 1 0 0 0', '      FADEOUT 1 0 0 1 0 0 0', '      MUTE 0 0', '      SEL 0']
                if lane is not None:
                    L.append(f'      YPOS {num(lane / len(lanes))} {num(1 / len(lanes))} {len(lanes)}')
                L += [f'      IGUID {guid()}', f'      GUID {guid()}', '      <NOTES', f'        |{c.display}', '      >', '    >']
                continue
            L += ['    <ITEM', f'      POSITION {num(c.start / rate + offset)}', '      SNAPOFFS 0',
                  f'      LENGTH {num(c.length / rate)}', '      LOOP 1', '      ALLTAKES 0',
                  f'      FADEIN 1 {num(c.fade_in / rate)} 0 1 0 0 0',
                  f'      FADEOUT 1 {num(c.fade_out / rate)} 0 1 0 0 0',
                  f'      MUTE {1 if c.mute else 0} 0', '      SEL 0']
            if lane is not None:
                nl = len(lanes)
                L.append(f'      YPOS {num(lane / nl)} {num(1 / nl)} {nl}')
            if getattr(c, 'color', None) is not None:                    # цвет клипа: COLORREF | 0x1000000
                cc = c.color
                L.append(f'      COLOR {0x1000000 | ((cc >> 16) & 255) | (cc & 0xFF00) | ((cc & 255) << 16)}')
            if getattr(c, 'gid', None):                                  # клип внутри клип-группы Pro Tools -> группа items Reaper
                L.append(f'      GROUP {c.gid}')
            L += [f'      IGUID {guid()}',
                  f'      NAME {q(getattr(c, "display", c.region.name))}', f'      VOLPAN {num(c.gain_lin)} 0 1 -1',
                  f'      SOFFS {num(c.offset / frate)}', f'      PLAYRATE {num(getattr(c, "rate", 1.0))} 1 0 -1 0 0.0025',
                  '      CHANMODE 0', f'      GUID {guid()}', '      <SOURCE WAVE', f'        FILE {q(fpath)}',
                  '      >']
            if getattr(c, 'gain_env', None):                              # клип-гейн с огибающей -> огибающая громкости тейка (значения абсолютные, 1.0 = 0 дБ)
                L += ['      <VOLENV', f'        EGUID {guid()}', '        ACT 1 -1', '        VIS 1 1 1', '        LANEHEIGHT 0 0', '        ARM 0', '        DEFSHAPE 0 -1 -1']
                for t_, db_ in c.gain_env:
                    L.append(f'        PT {num(t_ / rate)} {num(10 ** (db_ / 20))} 0')
                L.append('      >')
            L.append('    >')
        L.append('  >')
    L.append('>')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')


# --------------------------------------------------------------------- main --

# цвета Pro Tools «по умолчанию» (u16 0xfffe в 0x251a) зависят от типа дорожки; 0xffff — серый (без цвета); значения сняты через PTSL
DEFAULT_COLORS = {'audio': 0x13355f, 'folder': 0x1969a4, 'routing': 0x1969a4, 0x261e: 0x324a0c, 0x261f: 0x11225f,
                  0x2621: 0x7b510d, 0x2623: 0x14594c}


def default_color(raw, kind, ctype):
    if raw == 0xFFFF:
        return 0x2a2a2a
    if raw == 0xFFFE:
        return DEFAULT_COLORS.get(kind) if kind in ('audio', 'folder', 'routing') else DEFAULT_COLORS.get(ctype)
    return None


def track_state(st, states, fader):
    out = {}
    if states:
        out.update({k: st.get(k) for k in ('mute', 'solo', 'color', 'hidden') if k in st})
    if fader:
        out.update({k: st.get(k) for k in ('volume_db', 'pan') if k in st})
    return out


def pt_tag_other(t):
    """Метка типа для не-аудио дорожки (папки несут свою метку «Pro Tools: Basic/Routing Folder»)."""
    if t['kind'] in ('folder', 'routing'):
        return None
    if t['ctype'] == 0x261e:
        return 'aux ' + {1: 'mono', 2: 'stereo', 6: '5.1'}.get(t['state'].get('channels'), 'stereo')
    return {0x261f: 'vca', 0x2621: 'instrument', 0x2623: 'video'}.get(t['ctype'])


FX_WANTED = []                                                  # имена FX Reaper, блоки которых нужно получить у самого Reaper (см. harvest_name)


def apply_plugins(p, structure, tracks, settings=True, requests=None):
    """Плагины PT -> цепочка FX Reaper (по установленным VST3/VST2 с тем же именем) + список в заметке дорожки."""
    plug = PLG.read_plugins(p)
    installed = FX.load_installed()
    rpos = {t['pt_index']: k for k, t in enumerate(tracks) if 'pt_index' in t}
    n, wanted = 0, []
    ulib0 = FX.load_user_library()
    for i, pl in enumerate(plug):                              # плагины, блок которых нужно снять у самого Reaper (расширение делает это на лету)
        if i in rpos:
            for x_ in pl:
                nm_ = FX.harvest_name(x_, installed, ulib0)
                if nm_ and nm_ not in FX_WANTED:
                    FX_WANTED.append(nm_)
    if settings:                                               # настройки плагинов по именам параметров: одним запуском VST3-хоста на плагин
        import fx_state as _FS0
        ul_ = FX.load_user_library()
        pairs_ = []
        for i, pl in enumerate(plug):
            if i in rpos:
                for x_ in pl:
                    b_ = FX.block_for(x_, installed, ul_)
                    if b_ is not None:
                        pairs_.append((list(b_), x_))
        _FS0.warm([_FS0._bundle(b_)[0] for b_, _ in pairs_])
        _FS0.prefetch(pairs_)
    for i, pl in enumerate(plug):
        if not pl or i not in rpos:
            continue
        tr = tracks[rpos[i]]
        reqs_ = [] if requests is not None else None
        tr['fx'], missing = FX.chain_lines(pl, installed, session_rate(p), plugin_settings=settings, requests=reqs_)
        if reqs_:
            requests.extend(dict(r_, track=rpos[i]) for r_ in reqs_)
        tr['plugin_note'] = ['Pro Tools plug-ins: ' + ', '.join(x['name'] + (' [bypass]' if x['bypass'] else '') for x in pl)]
        if missing:
            tr['plugin_note'].append('FX chain not created for: ' + ', '.join(missing))
            uns_ = FX.unscanned(missing, installed)
            if uns_:
                tr['plugin_note'].append('Installed as VST3 but not scanned by Reaper (rescan plug-ins in Reaper Preferences): ' + ', '.join(uns_))
        narrow_ = []                                           # многоканальная дорожка: VST3-версия плагина может не принимать такой формат
        if settings:
            import fx_state as _FSB
            for x_ in pl:
                if x_.get('format') not in (None, 'mono', 'stereo'):
                    b_ = FX.block_for(x_, installed, ul_)
                    bs_ = _FSB.buses(_FSB._bundle(list(b_))[0]) if b_ is not None and _FSB._bundle(list(b_))[0] else None
                    if bs_ and bs_.get(x_['format']) is False:
                        narrow_.append(f"{x_['name']} ({x_['format']})")
        if narrow_:
            tr['plugin_note'].append('VST3 version does not accept this channel format, only part of the channels is processed: ' + ', '.join(narrow_))
            wanted.extend(missing)
        n += len(pl)
    FX.note_wanted(wanted)
    return n


def merge_stereo_pan(left, right):
    """Автоматизация (L, R) стерео-пана PT -> баланс Reaper -1..1: на объединённой сетке времён, линейная интерполяция."""
    def at(pts, t, default):
        if not pts:
            return default
        if t <= pts[0][0]:
            return pts[0][1]
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t0 <= t <= t1:
                return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        return pts[-1][1]
    times = sorted({t for pts in (left, right) if pts for t, _ in pts})
    return [(t, max(-1.0, min(1.0, (at(left, t, -100) + at(right, t, 100)) / 100))) for t in times]


def apply_routing(p, rate, structure, tracks, sends, automation, off=frozenset()):
    """Sends (приёмы на aux-дорожках) и автоматизация громкости -> поля 'recvs' / 'auto' в списке дорожек Reaper."""
    routing = SN.read_routing(p)
    rpos = {t['pt_index']: k for k, t in enumerate(tracks) if 'pt_index' in t}
    by_input = {}
    for i, r in enumerate(routing):
        if r['input'] is not None and i < len(structure) and structure[i]['ctype'] in (0x261e, 0x261c):   # приёмник sends: aux или аудиодорожка со входом-шиной
            by_input.setdefault(r['input'], i)
    n_sends = n_auto = 0
    for i, r in enumerate(routing):
        if i not in rpos or i >= len(structure):
            continue
        nch = structure[i].get('nchan', 1)
        if automation and r['auto'] and 'auto_vol' not in off:
            tracks[rpos[i]]['auto'] = [(t / rate, 0.0 if v is None else 10 ** (v / 20)) for t, v in r['auto']]
            n_auto += 1
        if automation and r.get('auto_mute') and 'auto_mute' not in off and (any(v for _, v in r['auto_mute']) or tracks[rpos[i]].get('state', {}).get('mute')):    # лейн «всегда не заглушено» ничего не меняет — пропускаем                      # mute PT: 1 — замьючено; в Reaper 1 — звучит
            tracks[rpos[i]]['mute_auto'] = [(t / rate, 1 - v) for t, v in r['auto_mute']]
            n_auto += 1
        if automation and r.get('auto_pan') and nch == 1 and 'auto_pan' not in off:
            tracks[rpos[i]]['pan_auto'] = [(t / rate, v / 100) for t, v in r['auto_pan']]
            n_auto += 1
        elif automation and nch == 2 and (r.get('auto_pan') or r.get('auto_pan_r')) and 'auto_pan' not in off:   # стерео: баланс = (L + R) / 100
            tracks[rpos[i]]['pan_auto'] = [(t / rate, v) for t, v in merge_stereo_pan(r.get('auto_pan'), r.get('auto_pan_r'))]
            n_auto += 1
        if not sends:
            continue
        for sd in r['sends']:
            dst = by_input.get(sd['dest_path'])
            if dst is None or dst not in rpos or dst == i:
                continue
            lvl = 0.0 if sd['level_db'] is None else 10 ** (sd['level_db'] / 20)
            nch = structure[i].get('nchan', 1)
            pan = sd['pan'] / 100 if nch == 1 else (sd['pan'] + sd['pan_r']) / 100 if nch == 2 else 0.0   # стерео: баланс из L/R
            renv = {}
            if automation:
                if sd.get('auto') and 'send_auto_vol' not in off:
                    renv['vol'] = [(t / rate, 0.0 if v is None else 10 ** (v / 20)) for t, v in sd['auto']]
                if sd.get('auto_mute') and 'send_auto_mute' not in off and (any(v for _, v in sd['auto_mute']) or sd['mute']):
                    renv['mute'] = [(t / rate, 1 - v) for t, v in sd['auto_mute']]
                if sd.get('auto_pan') and nch == 1 and 'send_auto_pan' not in off:
                    renv['pan'] = [(t / rate, v / 100) for t, v in sd['auto_pan']]
                n_auto += len(renv)
            tracks[rpos[dst]].setdefault('recvs', []).append((rpos[i], lvl, max(-1.0, min(1.0, pan)), sd['mute'], 3 if sd.get('pre') else 0, renv))
            n_sends += 1
    return n_sends, n_auto


def copy_media_files(tracks, proj_dir, media_dir=None):
    """Копирует найденные аудиофайлы дорожек в <proj_dir>/Media и подменяет пути в клипах на относительные; при media_dir —
    в эту папку с абсолютными путями (прямой импорт в открытый проект Reaper)."""
    import shutil
    media = media_dir or os.path.join(proj_dir, 'Media')
    os.makedirs(media, exist_ok=True)
    used = []
    for tr in tracks:
        its = list(tr.get('items') or [])
        for _, lit in (tr.get('lanes') or []):
            its += list(lit)
        for it in its:
            f = it[1][0]
            if f not in used and os.path.isfile(f):
                used.append(f)
    mapping, taken = {}, set()
    for k, f in enumerate(used, 1):
        base = os.path.basename(f)
        stem, ext = os.path.splitext(base)
        n = 1
        while base.lower() in taken:
            n += 1
            base = f'{stem}_{n}{ext}'
        taken.add(base.lower())
        dst = os.path.join(media, base)
        if not os.path.exists(dst):
            shutil.copy2(f, dst)
        mapping[f] = os.path.join(media, base) if media_dir else 'Media/' + base
        print(f'Copying audio: {k}/{len(used)}', flush=True)

    def remap(lst):
        return [(c, (mapping.get(fp, fp), fr), pos) for c, (fp, fr), pos in lst]
    for tr in tracks:
        if tr.get('items'):
            tr['items'] = remap(tr['items'])
        if tr.get('lanes'):
            tr['lanes'] = [(nm, remap(lit)) for nm, lit in tr['lanes']]


def _guess_bits(cache):
    for path, ok in cache.values():
        if ok:
            try:
                with wave.open(path, 'rb') as w:
                    return w.getsampwidth() * 8
            except Exception:
                pass
    return None


def _audio_props(path):
    """(кадров, частота, разрядность, каналов) для wav/flac; None — формат не разбираем (видео и т. п.)."""
    low = path.lower()
    if low.endswith('.flac'):
        with open(path, 'rb') as f:
            h = f.read(42)
        if h[:4] != b'fLaC' or len(h) < 42:
            raise ValueError('not flac')
        v = int.from_bytes(h[18:26], 'big')                    # 20 бит частота, 3 канала-1, 5 бит разрядность-1, 36 бит кадров
        return v & ((1 << 36) - 1), v >> 44, ((v >> 36) & 31) + 1, ((v >> 41) & 7) + 1
    if low.endswith(('.wav', '.wave', '.bwf')):
        with wave.open(path, 'rb') as w:
            return w.getnframes(), w.getframerate(), w.getsampwidth() * 8, w.getnchannels()
    return None


def _norm(name):
    return re.sub(r'[^0-9a-zа-я]', '', os.path.splitext(name)[0].lower())


def find_candidates(ptx_path, folders, relink=None):
    """Для каждого отсутствующего файла ищет в папках (рекурсивно) wav/aiff с тем же именем (или похожим без учёта регистра и символов)."""
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        res = convert(ptx_path, info=True, relink=relink)
    missing, rate = res['missing_detail'], res['rate']
    folders = [os.path.abspath(f) for f in folders]
    by_exact, by_norm = {}, {}
    for folder in folders:
        for dp, _, fns in os.walk(folder):
            for fn in fns:
                if fn.startswith('.') or not fn.lower().endswith(('.wav', '.wave', '.bwf', '.aif', '.aiff', '.flac', '.mp3', '.mov', '.mp4', '.m4v', '.mxf')):
                    continue
                full = os.path.join(dp, fn)
                by_exact.setdefault(os.path.splitext(fn)[0].lower(), []).append(full)
                by_norm.setdefault(_norm(fn), []).append(full)
    out = {}
    for m in missing:
        paths, exact = [], set()
        for p in by_exact.get(os.path.splitext(m['name'])[0].lower(), []):
            paths.append(p)
            exact.add(p)
        for p in by_norm.get(_norm(m['name']), []):
            if p not in exact and p not in paths:
                paths.append(p)
        cands = []
        for p in paths:
            c = dict(path=p, exact=p in exact, frames=0, rate=0, bits=0, channels=0, seconds=0.0, format_ok=False, duration_ok=False)
            try:
                pr = _audio_props(p)
                if pr is None:                                 # видео и прочее — формат не проверяем
                    c.update(format_ok=True, duration_ok=True)
                else:
                    c.update(frames=pr[0], rate=pr[1], bits=pr[2], channels=pr[3])
                    c['seconds'] = c['frames'] / c['rate'] if c['rate'] else 0.0
                    c['format_ok'] = c['rate'] == rate
                    c['duration_ok'] = c['seconds'] + 0.002 >= m['seconds']
            except Exception:                                  # noqa: BLE001
                pass
            cands.append(c)
        out[m['path']] = cands
    return dict(missing=missing, candidates=out)


def convert(ptx_path, out_path=None, media_dirs=(), info=False, alt_playlists=True, alt_mode='lanes',
            fades=True, gain=True, markers_on=True, states=True, folders_on=True, aux_on=True, fader=True,
            sends=True, automation=True, plugins=True, copy_media=False, copy_media_to=None, relink=None, off=(), on=(), only=None, offset=0.0, param_requests_file=None):
    # off — отключённые пункты Track Data: clips, clip_mute, clip_colors, track_colors, notes, solo, mute, auto_vol, auto_pan, auto_mute, send_auto_vol, send_auto_pan, send_auto_mute
    # on — включаемые пункты (по умолчанию выключены): skip_hidden, skip_inactive, trim_overlap
    off, on = set(off), set(on)
    ptx_path = os.path.abspath(ptx_path)
    ptx_dir = os.path.dirname(ptx_path)
    if not info:
        print('Reading session: 0/1', flush=True)
    raw_ptx = open(ptx_path, 'rb').read()
    p = PTX(raw_ptx)
    rate = session_rate(p)
    cat = Catalog(p)
    regions = parse_regions(p)
    gains = parse_gains(p)
    gain_envs = parse_gain_envs(p)
    geoms = parse_geometries(p) if fades else []              # без геометрий фейды и кроссфейды игнорируются
    plists = parse_playlists(p)
    names = track_names(p) or list(plists)
    mls = parse_memory_locations(p) if markers_on else []
    markers = [(m['idx'], m['name'], m['start'], m['end'], m.get('color'), m.get('comment') or '') for m in mls if m['prop'] != 'none' and m['track'] is None]     # линейка: маркеры и Selection (-> регионы)
    track_markers = [m for m in mls if m['prop'] != 'none' and m['track'] is not None]            # маркеры на дорожках -> пустые items на этих дорожках
    markers_dropped = sum(1 for m in mls if m['prop'] == 'none')                                 # Time Properties = None: только настройки вида (зум, высоты, показ дорожек), аналога в Reaper нет
    structure = parse_tracks(p)
    cache, tracks, missing, skipped, total, other = {}, [], {}, 0, 0, 0
    skipped_detail = []                                         # [{'name', 'reason'}] — дорожки, которые не удалось перенести
    video_clips = parse_video(p)
    structure_video = [t_ for t_ in parse_tracks(p) if t_['ctype'] == 0x2623]
    need = {}                                                  # отсутствующий файл -> сколько секунд нужно проекту
    pl_events = plists

    for rg in regions:
        if rg is not None and rg.file_index is not None and rg.file_index < len(cat.files):
            resolve_file(cat, rg.file_index, ptx_dir, list(media_dirs), cache, relink)
    for i in range(len(cat.files)):                           # все файлы сессии (как в списке «Missing Files» Pro Tools), а не только используемые клипами
        resolve_file(cat, i, ptx_dir, list(media_dirs), cache, relink)
    vcat, vcache = Catalog(p, 0x1006), {}                     # видеофайлы
    for i in range(len(vcat.files)):
        resolve_file(vcat, i, ptx_dir, list(media_dirs), vcache, relink)

    group_defs = parse_groups(p)
    group_inner = parse_group_playlists(p)
    gcount = [0]
    group_regions = []                                          # (имя, начало с, конец с) — группы клипов без звука -> пустые items Reaper

    def make_items(plname, multich=False):
        nonlocal skipped
        evs, gitems, gclips = [], [], []
        for e_ in pl_events.get(plname, []):
            if e_[1] == 5:                                                       # событие-группа, а не аудио-клип
                if 'groups' in off or e_[2] >= len(group_defs):
                    continue
                inner = group_inner[e_[2]] if e_[2] < len(group_inner) else []
                if any(x_[1] == 3 for x_ in inner):                              # группа со звуком: клипы внутри — обычные items, связанные в группу items
                    gcount[0] += 1
                    cl_, st_ = build_clips([(x_[0] + e_[0],) + tuple(x_[1:]) for x_ in inner if x_[1] != 5], regions, geoms)
                    skipped += st_['skipped']
                    for c_ in cl_:
                        c_.gid = gcount[0]
                    gclips += cl_
                elif group_defs[e_[2]][0]:                                       # пустая группа -> пустой item с именем
                    group_regions.append((group_defs[e_[2]][0], e_[0] / rate, (e_[0] + group_defs[e_[2]][1]) / rate))
                    gitems.append((EmptyItem(e_[0], group_defs[e_[2]][1], group_defs[e_[2]][0]), ('', rate), 0))
                continue
            evs.append(e_)
        clips, st = build_clips(evs, regions, geoms)
        clips += gclips
        skipped += st['skipped']
        if 'clips' in off:
            return gitems
        if 'trim_overlap' in on:                               # «Trim Overlapping Clips»: конец предыдущего клипа обрезается по началу следующего
            clips.sort(key=lambda c_: c_.start)
            for a_, b_ in zip(clips, clips[1:]):
                if b_.start < a_.end:
                    a_.length = max(0, b_.start - a_.start)
                    a_.fade_out = min(a_.fade_out, a_.length)
        for c in clips:
            if 'clip_mute' in off:
                c.mute = False
            if 'clip_colors' in off:
                c.color = None
        items = []
        for c in clips:
            rg = c.region
            if rg.file_index is None or rg.file_index >= len(cat.files):
                skipped += 1
                continue
            fpath, ok = cache[rg.file_index]
            if not ok:
                missing[fpath] = True
                need[fpath] = max(need.get(fpath, 0.0), (rg.offset + rg.length) / rate)
            g = gains[rg.gain_index] if 0 <= rg.gain_index < len(gains) else 0.0
            if not gain:
                g = 0.0
            if not -144.0 <= g <= 60.0:                          # не клип-гейн (формат точки другой) — игнорируем
                g = 0.0 if g > NEG_INF_DB else g
            c.gain_lin = 0.0 if g <= NEG_INF_DB else 10 ** (g / 20)
            c.gain_env = gain_envs.get(rg.gain_index) if gain else None        # огибающая клип-гейна -> огибающая громкости тейка
            frate = wav_rate(fpath, rate) if ok else rate
            c.display = re.sub(r'\.(L|R|C|Ls|Rs|LFE|A\d+)$', '', rg.name) if multich else rg.name
            items.append((c, (fpath, frate), 0))
        return items + gitems

    only = set(only) if only is not None else None             # индексы дорожек Pro Tools (порядок сессии), выбранные для переноса
    if only is not None:                                       # глубина папок — по выбранным предкам
        by_id_ = {(t_['id'] or str(i_)): t_ for i_, t_ in enumerate(structure)}
        for i_, t_ in enumerate(structure):
            d_, q_ = 0, t_['parent']
            while q_ and q_ in by_id_:
                anc_ = by_id_[q_]
                if structure.index(anc_) in only:
                    d_ += 1
                q_ = anc_['parent']
            t_['depth'] = d_
    cont_blocks = TS.track_blocks(p)
    for n, t in enumerate(structure, 1):
        if not info:
            print(f'Reading tracks: {n}/{len(structure)}', flush=True)
        if only is not None and n - 1 not in only:
            continue
        if 'skip_inactive' in on and t['state'].get('inactive'):
            continue
        if 'skip_hidden' in on and t['state'].get('hidden'):
            continue
        kind = t['kind']
        if (kind == 'other' and not aux_on and not (t['ctype'] == 0x261f and 'vca' not in off)) or (kind in ('folder', 'routing') and not folders_on):
            continue
        if kind == 'other' and t['ctype'] == 0x2623:             # видеодорожка: клип видео -> item с источником VIDEO
            vids = [v for v in video_clips if v['name'] == t['name']] or (video_clips if len(structure_video) == 1 else [])
            if 'video' in off:
                continue
            if not vids:
                other += 1
                skipped_detail.append({'name': t['name'], 'reason': 'no video clip'})
                continue
            vpath = vcache[0][0] if vcache else ''
            if not vpath:                                      # в каталоге сессии нет видеофайла (клип без файла) — переносить нечего
                other += 1
                skipped_detail.append({'name': t['name'], 'reason': 'no video file in the session'})
                continue
            tracks.append({'name': t['name'], 'pt_index': n - 1, 'pt_tag': 'video', 'items': [], 'video_items': [dict(v, file=vpath) for v in vids],
                           'state': track_state(t['state'], states, False), 'folder': False, 'depth': t['depth'] if folders_on else 0})
            continue
        if kind == 'audio':
            items = make_items(t['name'], t['nchan'] > 1)
            total += sum(1 for i_ in items if not getattr(i_[0], 'empty', False))
            tracks.append({'name': t['name'], 'pt_index': n - 1, 'pt_tag': 'audio ' + {1: 'mono', 2: 'stereo', 6: '5.1'}.get(t['nchan'], 'stereo'), 'items': items, 'state': track_state(t['state'], states, fader), 'folder': False,
                           'depth': t['depth'] if folders_on else 0,
                           'nchan': max(2, t['nchan'] + (t['nchan'] & 1))})
            alts = [pn for pn in t['playlists'] if pn != t['name']] if alt_playlists else []
            if alts and alt_mode == 'lanes':                     # альтернативные плейлисты -> Takes & Lanes (fixed lanes)
                lanes = [(t['name'], items)]
                for pn in alts:
                    alt = make_items(pn, t['nchan'] > 1)
                    total += sum(1 for i_ in alt if not getattr(i_[0], 'empty', False))
                    lanes.append((pn, alt))
                tracks[-1]['lanes'] = lanes
            else:
                for pn in alts:                                  # или заглушённые дорожки ниже
                    alt = make_items(pn, t['nchan'] > 1)
                    total += sum(1 for i_ in alt if not getattr(i_[0], 'empty', False))
                    tracks.append({'name': f"{t['name']} [alt: {pn}]", 'items': alt, 'folder': False,
                                   'state': {'mute': True}, 'depth': t['depth'] if folders_on else 0})
        else:                                                      # папки, aux, VCA, instrument: дорожки без клипов
            is_folder = kind in ('folder', 'routing')
            nm = t['name'] if (is_folder or t['ctype'] == 0x261f) else f"{t['name']} ({KIND_NAMES.get(t['ctype'], 'track')})"
            tracks.append({'name': nm, 'pt_index': n - 1, 'pt_tag': pt_tag_other(t), 'items': [], 'state': track_state(t['state'], states, fader if t['ctype'] in (0x261e, 0x261d) else False), 'folder': is_folder, 'depth': t['depth'] if folders_on else 0,
                           'note': FOLDER_NOTES.get(kind)})
            if t['ctype'] == 0x261f and 'vca' not in off:        # VCA: фейдер, mute, solo и автоматизация громкости / mute
                vs = read_vca(cont_blocks[n - 1])
                tr_v = tracks[-1]
                if states:
                    tr_v['state'] = dict(tr_v['state'], mute=vs['mute'], solo=vs['solo'])
                if fader and vs['volume_db'] is not None:
                    tr_v['state']['volume_db'] = vs['volume_db']
                if automation and 'auto_vol' not in off and vs['auto'] and any(v_ != vs['auto'][0][1] for _, v_ in vs['auto']):
                    tr_v['auto'] = [(t_ / rate, 0.0 if v_ <= -1440 else 10 ** (v_ / 200)) for t_, v_ in vs['auto']]
                if automation and 'auto_mute' not in off and vs['auto_mute'] and any(v_ for _, v_ in vs['auto_mute']):
                    tr_v['mute_auto'] = [(t_ / rate, 1 - (1 if v_ else 0)) for t_, v_ in vs['auto_mute']]

    n_vca = 0
    if aux_on and 'vca' not in off:                            # VCA Pro Tools -> VCA Reaper: ведущий и ведомые дорожки через GROUP_FLAGS (группа k — бит 1 << (k-1))
        by_pt = {t_['pt_index']: t_ for t_ in tracks if 'pt_index' in t_}
        k_bit = 0
        for g_ in parse_mix_groups(p):
            lead = by_pt.get(g_['vca']) if g_['vca'] is not None else None
            mem = [by_pt[i_] for i_ in g_['members'] if i_ in by_pt and by_pt[i_] is not lead]
            if lead is None or not mem or k_bit >= 32:
                continue
            bit = 1 << k_bit
            k_bit += 1
            lead['vca_lead'] = lead.get('vca_lead', 0) | bit
            for m_ in mem:
                m_['vca_follow'] = m_.get('vca_follow', 0) | bit
            n_vca += 1

    import ptx_structure
    closed_pos = ptx_structure.read_closed_folders(p) if folders_on else set()
    for tr_ in tracks:                                         # закрытые папки Pro Tools
        if tr_.get('folder') and tr_.get('pt_index') in closed_pos:
            tr_['closed'] = True
    for tr_ in tracks:                                         # комментарии дорожек Pro Tools -> заметки проекта
        if 'pt_index' in tr_ and 'notes' not in off:
            tr_['comment'] = TS.read_comment(p, tr_['pt_index'])
        for key_, opt_ in (('color', 'track_colors'), ('solo', 'solo'), ('mute', 'mute')):
            if opt_ in off:
                tr_.get('state', {}).pop(key_, None)
    n_plug = 0
    del FX_WANTED[:]
    param_requests = [] if param_requests_file else None          # параметры плагинов, которые выставит по именам Reaper (расширение); без файла — хост строит состояние сам
    import fx_state as _FSN
    _FSN.NAMED_VIA_HOST = not param_requests_file
    if plugins:
        import fx_state as _FS
        _FS.STATS['applied'] = _FS.STATS['unsupported'] = 0
        _FS.HOST_ENABLED = not info                       # при чтении сведений о сессии хост (запуск плагинов) не нужен: настройки не переносятся, только считаются
        n_plug = apply_plugins(p, structure, tracks, settings=('plugin_settings' not in off) and not info, requests=param_requests)
    n_sends = n_auto = 0
    if sends or automation:
        n_sends, n_auto = apply_routing(p, rate, structure, tracks, sends, automation, off)

    master = None                                              # Master Fader Pro Tools -> родной мастер Reaper (а не отдельная дорожка)
    for k, tr in enumerate(tracks):
        if 'pt_index' in tr and tr['pt_index'] < len(structure) and structure[tr['pt_index']]['ctype'] == 0x261d:
            master = tracks.pop(k)
            break
    import ptx_settings as _PS
    _tc = _PS.read_timecode(p)
    n_fades = len(geoms) if fades else 0
    n_cgain = sum(1 for t_ in tracks for it_ in t_.get('items', ()) if abs(getattr(it_[0], 'gain_lin', 1.0) - 1.0) > 1e-6)
    for path_, ok_ in list(cache.values()) + list(vcache.values()):
        if not ok_:
            missing[path_] = True
    import ptx_settings as _PS2
    raw_clips = raw_gain = raw_fades = 0                       # клипы/фейды по всем плейлистам и клипы с ненулевым клип-гейном (как их считает сводка Pro Tools)
    used_files = set()                                         # файлы, на которые ссылаются клипы
    pl_root = root(p, 0x1054)
    groups = {}                                                # многоканальные дорожки (5.1/7.1) хранят одинаковый плейлист на каждый канал — считаем такую группу один раз
    bnames_ = playlist_block_names(p)
    for bi_, t_ in enumerate(kids(pl_root, 0x1052) if pl_root is not None else []):
        name_ = bnames_[bi_]
        sig_ = []
        for e_ in kids(t_, 0x1050):
            tail_ = e_.items[-1] if e_.items and isinstance(e_.items[-1], (bytes, bytearray)) else b''
            for q_ in kids(e_, 0x104f):
                sig_.append((tail_[:1] == b'\x01', struct.unpack_from('<Q', q_.items[0], 9)[0], len(q_.items[0]) > 20 and q_.items[0][20] == 1))
        key_ = (name_, tuple(sig_))
        groups.setdefault(key_, []).append(t_)
    for (name_, sig_), members_ in groups.items():
        mult_ = 1 if len(members_) <= 2 else len(members_)       # одна пара (стерео L/R) считается как есть, 3+ одинаковых (5.1, 7.1) — один раз
        t_ = members_[0]
        for e_ in kids(t_, 0x1050):
            tail_ = e_.items[-1] if e_.items and isinstance(e_.items[-1], (bytes, bytearray)) else b''
            is_fade_ = tail_[:1] == b'\x01'
            for q_ in kids(e_, 0x104f):
                if (not is_fade_) and len(q_.items[0]) > 20 and q_.items[0][20] == 1:           # клип-группа — не аудио-клип
                    continue
                n_ = len(members_) if mult_ == 1 else 1
                if is_fade_:
                    raw_fades += n_
                    continue
                raw_clips += n_
        for t2_ in members_:                                   # файлы и клип-гейн считаем по всем каналам
            for e_ in kids(t2_, 0x1050):
                tail_ = e_.items[-1] if e_.items and isinstance(e_.items[-1], (bytes, bytearray)) else b''
                if tail_[:1] == b'\x01':
                    continue
                for q_ in kids(e_, 0x104f):
                    ident_ = struct.unpack_from('<I', q_.items[0], 4)[0]
                    rg_ = regions[ident_] if ident_ < len(regions) else None
                    if rg_ is None:
                        continue
                    if rg_.file_index is not None:
                        used_files.add(rg_.file_index)
                    if 0 <= rg_.gain_index < len(gains) and abs(gains[rg_.gain_index]) > 1e-9:
                        raw_gain += 1
    by_pt_ = {t_['pt_index']: t_ for t_ in tracks if 'pt_index' in t_}
    n_track_markers = 0
    for m_ in track_markers:
        tr_ = next((t_ for t_ in tracks if t_.get('pt_index') == m_['track']), None)
        if tr_ is None:
            continue
        ln_ = (m_['end'] - m_['start']) if m_['end'] else int(rate * 0.5)                         # точечному маркеру — короткий item, диапазону — его длина
        tr_.setdefault('items', []).append((EmptyItem(m_['start'], ln_, m_['name']), ('', rate), 0))
        n_track_markers += 1
    vclips = len(video_clips)
    start_tc = _PS2.format_start(_tc[2], _tc[0]) if _tc else None
    result = {'app_info': _PS2.read_app_info(raw_ptx), 'start_tc': start_tc, 'start_seconds': (_tc[2] / _tc[0]) if _tc else 0.0, 'audio_tracks': sum(1 for t_ in structure if t_['kind'] == 'audio'), 'clips_raw': raw_clips, 'fades_raw': raw_fades, 'files_used': len(used_files),
              'clip_gain_entries': raw_gain, 'vol_points': sum(len(t_.get('auto') or ()) for t_ in tracks), 'video_clips': vclips, 'fades': n_fades, 'clip_gain': n_cgain, 'fps': _tc[0] if _tc else None, 'files': len(cat.files) + len(vcat.files), 'video_files': len(vcat.files), 'tracks': len(tracks), 'plugins': n_plug, 'sends': n_sends, 'automation': n_auto, 'folders': sum(1 for t in tracks if t['folder']),
              'routing_folders': sum(1 for t in structure if t['kind'] == 'routing'),
              'alt_playlists': sum(1 for t in structure for pn in t['playlists'] if pn != t['name']),
              'vca_groups': n_vca, 'skipped_tracks': other, 'skipped_detail': skipped_detail, 'fx_wanted': list(FX_WANTED), 'clips': total, 'markers': len(markers) + n_track_markers, 'marker_regions': sum(1 for m_ in markers if m_[3]), 'track_markers': n_track_markers, 'markers_dropped': markers_dropped, 'plugin_states': (_FSS.STATS['applied'] if (_FSS := sys.modules.get('fx_state')) else 0), 'groups': len(set(group_regions)), 'rate': rate,
              'missing': sorted(missing), 'skipped': skipped,
              'missing_detail': [dict(path=f, name=os.path.basename(f), seconds=need.get(f, 0.0)) for f in sorted(missing, key=lambda x: os.path.basename(x).lower())],
              'track_list': [dict(index=i_, name=t_['name'], kind={'audio': 'audio', 'folder': 'folder', 'routing': 'routing'}.get(t_['kind']) or KIND_NAMES.get(t_['ctype'], 'track'),
                                  depth=t_['depth'], clips=sum(1 for e_ in pl_events.get(t_['name'], []) if e_[1] == 3 and e_[2] < len(regions) and regions[e_[2]] is not None) if t_['kind'] == 'audio' else 0,
                                  inactive=bool(t_['state'].get('inactive')), hidden=bool(t_['state'].get('hidden')), frozen=bool(t_['state'].get('frozen')), automation=bool((by_pt_.get(i_) or {}).get('auto') or (by_pt_.get(i_) or {}).get('pan_auto') or (by_pt_.get(i_) or {}).get('mute_auto')),
                                  markers=sum(1 for m_ in track_markers if m_['track'] == i_))
                             for i_, t_ in enumerate(structure)],
              'video_tracks': sum(1 for t in structure if t['ctype'] == 0x2623), 'bits': _guess_bits(cache)}
    if info:
        print(json.dumps(result, ensure_ascii=False))
        return result
    out_path = out_path or os.path.join(ptx_dir, os.path.splitext(os.path.basename(ptx_path))[0] + '.rpp')
    if copy_media:                                             # проект в своей папке: <папка>/<имя>/<имя>.rpp + <папка>/<имя>/Media
        stem = os.path.splitext(os.path.basename(out_path))[0]
        proj_dir = os.path.join(os.path.dirname(os.path.abspath(out_path)), stem)
        out_path = os.path.join(proj_dir, stem + '.rpp')
        copy_media_files(tracks, proj_dir)
    if copy_media_to:                                          # прямой импорт: аудио в указанную папку, пути абсолютные
        copy_media_files(tracks, None, media_dir=copy_media_to)
    if param_requests_file:
        with open(param_requests_file, 'w', encoding='utf-8') as f_:
            json.dump(param_requests or [], f_)
    import ptx_settings
    write_rpp(out_path, rate, tracks, markers, master, ptx_settings.read_timecode(p), ptx_settings.read_counters(p), offset=offset, bpm=(__import__('ptx_tempo').read_bpm(p)[0] if __import__('ptx_tempo').read_bpm(p)[0] not in (None, 120.0) else None))
    print(f'Done: {total} clips on {len(tracks)} tracks, {len(markers) + n_track_markers} markers, '
          f'{len(missing)} missing files -> {out_path}', flush=True)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('ptx')
    ap.add_argument('--out')
    ap.add_argument('--media-dir', action='append', default=[])
    ap.add_argument('--info', action='store_true')
    ap.add_argument('--no-alt-playlists', action='store_true', help='не переносить альтернативные плейлисты')
    ap.add_argument('--no-fades', action='store_true')
    ap.add_argument('--no-gain', action='store_true')
    ap.add_argument('--no-markers', action='store_true')
    ap.add_argument('--copy-media', action='store_true', help='скопировать аудио в папку проекта (Media) вместо ссылок на оригиналы')
    ap.add_argument('--copy-media-to', help='скопировать аудио в эту папку (абсолютные пути в проекте)')
    ap.add_argument('--no-plugins', action='store_true', help='не переносить плагины')
    ap.add_argument('--no-sends', action='store_true', help='не переносить sends')
    ap.add_argument('--no-automation', action='store_true', help='не переносить автоматизацию громкости')
    ap.add_argument('--no-fader', action='store_true', help='не переносить громкость и панораму дорожек')
    ap.add_argument('--no-states', action='store_true', help='не переносить mute/solo/цвет дорожек')
    ap.add_argument('--no-folders', action='store_true')
    ap.add_argument('--no-aux', action='store_true', help='не создавать пустые дорожки для aux/VCA/instrument')
    ap.add_argument('--relink', help='JSON {ожидаемый путь: фактический файл}')
    ap.add_argument('--candidates', action='store_true', help='найти кандидатов на отсутствующие файлы')
    ap.add_argument('--search', action='append', default=[], help='папка поиска для --candidates')
    ap.add_argument('--off', default='', help='отключаемые пункты Track Data через запятую (clips,clip_mute,clip_colors,track_colors,notes,solo,mute,auto_vol,auto_pan,auto_mute,send_auto_vol,send_auto_pan,send_auto_mute)')
    ap.add_argument('--on', default='', help='включаемые пункты: skip_hidden,skip_inactive,trim_overlap')
    ap.add_argument('--tracks', default='', help='индексы переносимых дорожек Pro Tools через запятую (порядок сессии, с 0); по умолчанию все')
    ap.add_argument('--offset', type=float, default=0.0, help='сместить все времена (клипы, маркеры, автоматизацию) на столько секунд; может быть отрицательным')
    ap.add_argument('--fx-library', default='', help='файл с блоками FX Reaper (формат fx_library.txt), снятыми у самого Reaper; добавляется к личной библиотеке')
    ap.add_argument('--param-requests', default='', help='записать сюда параметры плагинов (по именам), которые выставит Reaper (расширение): [{track, fx, name, params}]')
    ap.add_argument('--alt-as-tracks', action='store_true', help='альтернативные плейлисты — заглушённые дорожки вместо lanes')
    a = ap.parse_args()
    if a.fx_library:
        os.environ['PX_FX_LIBRARY'] = os.path.abspath(a.fx_library)
    relink = json.load(open(a.relink, encoding='utf-8')) if a.relink else None
    if a.candidates:
        print(json.dumps(find_candidates(a.ptx, a.search, relink), ensure_ascii=False))
        return
    convert(a.ptx, a.out, a.media_dir, a.info, not a.no_alt_playlists, 'tracks' if a.alt_as_tracks else 'lanes',
            fades=not a.no_fades, gain=not a.no_gain, markers_on=not a.no_markers, states=not a.no_states,
            folders_on=not a.no_folders, aux_on=not a.no_aux, fader=not a.no_fader,
            sends=not a.no_sends, automation=not a.no_automation, plugins=not a.no_plugins, copy_media=a.copy_media, copy_media_to=a.copy_media_to, relink=relink,
            off=[x for x in a.off.split(',') if x], on=[x for x in a.on.split(',') if x],
            only=[int(x) for x in a.tracks.split(',') if x.strip().isdigit()] if a.tracks.strip() else None, offset=a.offset,
            param_requests_file=a.param_requests or None)


if __name__ == '__main__':
    main()
