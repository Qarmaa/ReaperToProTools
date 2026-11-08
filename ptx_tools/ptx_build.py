#!/usr/bin/env python3
"""
ptx_build.py — добавляет аудио-клипы, фейды и кроссфейды в готовую сессию Pro Tools (.ptx).

Шаблон — сессия, сохранённая самим Pro Tools (дорожки уже есть). Структуры блоков сняты с
эталонов Pro Tools 26.4 (lab/oracle2 -> ptx_templates.py) и сверены с открытой
спецификацией pt_api (MIT).

Поддерживается (v1): моно WAV PCM 24 бит, позиции в сэмплах (дорожки с таймбазой Samples),
целый файл либо подклип с offset == 0, фейд-ин / фейд-аут, кроссфейд по перекрытию двух
клипов одной дорожки (левый клип укорачивается до начала перекрытия).
"""
import os
import random
import struct
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ptx_templates as T                              # noqa: E402
from ptx_model import PTX, Block, parse                # noqa: E402


# приоритет фейд-аута среди событий с одинаковой позицией: -1 = раньше событий клипа, который начинается
# в этой же точке (так Pro Tools считает фейд принадлежащим уходящему клипу); 2 = старое поведение (отладка)
FADEOUT_PRIO = int(os.environ.get('PTX_FADEOUT_PRIO', '-1'))


def u32(n):
    return struct.pack('<I', n)


def u24(n):
    if not 0 <= n < 1 << 24:
        raise ValueError(f'{n} does not fit u24')
    return struct.pack('<I', n)[:3]


def u16(n):
    if not 0 <= n < 1 << 16:
        raise ValueError(f'{n} does not fit u16')
    return struct.pack('<H', n)


def ct(c):
    return struct.pack('<H', c)


def pstr(s):
    b = s.encode('utf-8')
    return u32(len(b)) + b


def filetime(unix_seconds):
    return struct.pack('<Q', int((unix_seconds + 11644473600) * 1e7))


def clone(raw):
    """Блок из сырых байт образца (с заголовком 0x5A|type|size)."""
    b = parse(raw, 0, len(raw))
    assert b is not None
    _reset_orig(b)
    return b


def _reset_orig(b):
    """Клон — новый блок: исходного смещения в файле у него нет (иначе оно перепутается с настоящими)."""
    b.orig = None
    for i in b.items:
        if isinstance(i, Block):
            _reset_orig(i)


def patch(b, idx, offset, data):
    """Заменяет байты в сыром сегменте items[idx] блока (offset считается от начала сегмента)."""
    seg = bytearray(b.items[idx])
    seg[offset:offset + len(data)] = data
    b.items[idx] = bytes(seg)


def find_root(p, ctype):
    for r in p.roots:
        if isinstance(r, Block) and r.ctype == ctype:
            return r
    raise KeyError(f'root {ctype:#06x} not found')


def kids(b, ctype=None):
    return [i for i in b.items if isinstance(i, Block) and (ctype is None or i.ctype == ctype)]


def bump(b, delta=1):
    """Счётчик u32 сразу за ctype в первом сыром сегменте блока."""
    n = struct.unpack_from('<I', b.items[0], 2)[0] + delta
    patch(b, 0, 2, u32(n))
    return n


def _all_blocks(p):
    return p.blocks()


# --------------------------------------------------------------- WAV --------

def _wav_data_offset(path):
    """Смещение первого байта блока 'data' (после его 8-байтного заголовка) — Pro Tools хранит его в записи файла."""
    with open(path, 'rb') as f:
        head = f.read(12)
        pos = 12
        if head[:4] not in (b'RIFF', b'RF64') or head[8:12] != b'WAVE':
            return 44
        while True:
            f.seek(pos)
            h = f.read(8)
            if len(h) < 8:
                return 44
            cid, sz = h[:4], struct.unpack('<I', h[4:])[0]
            if cid == b'data':
                return pos + 8
            pos += 8 + sz + (sz & 1)


class WavInfo:
    def __init__(self, path):
        self.path = path
        with wave.open(path, 'rb') as w:
            self.channels, self.bits = w.getnchannels(), w.getsampwidth() * 8
            self.rate, self.frames = w.getframerate(), w.getnframes()
        self.link_dir = None             # папка оригиналов (режим Link to source); None — файл в Audio Files сессии
        self.data_offset = _wav_data_offset(path)      # где в файле начинаются аудиоданные (у BWF с bext/junk — не 44)
        st = os.stat(path)
        self.mtime = st.st_mtime
        self.birth = getattr(st, 'st_birthtime', st.st_mtime)
        if self.channels not in (1, 2, 6) or self.bits not in (16, 24):
            raise ValueError(f'{os.path.basename(path)}: only mono, stereo or 6-channel 16/24-bit PCM is supported '
                             f'(got {self.channels} ch, {self.bits} bit)')


# ---------------------------------------------------------- генераторы ------

def make_volume_block(name, vol_id, mount):
    """0x0f3c: том, на котором лежат файлы."""
    return Block(1, [ct(0x0f3c) + pstr(name) + vol_id + pstr(mount)])


def _path_nodes(dir_path, vol_name, vol_id):
    """Хвост пути: узлы (том, папки...) и терминальная папка. dir_path — папка, чей РОДИТЕЛЬ описывается."""
    comps = [c for c in dir_path.strip('/').split('/') if c]
    if not comps:                                                  # родитель — корень диска
        return [vol_name], vol_name
    if dir_path.startswith('/Volumes/'):
        return [comps[1]] + comps[2:-1], comps[-1]
    return [vol_name] + comps[:-1], comps[-1]


def make_catalog_groups(groups, vol_name, vol_id):
    """Содержимое богатого 0x103a: файлы по группам-папкам + хвосты путей.

    groups: [{'folder': имя папки, 'parent': путь РОДИТЕЛЬСКОЙ папки, 'files': [имена], 'session': bool}]
    (не более двух: внешняя папка и/или Audio Files сессии). Правила сняты с эталонов Pro Tools
    (lab/oracle5: k1..k4):
      C1 = последний номер терминального узла + 1, C2 = терминальный узел хвоста первой группы;
      узлы нумеруются подряд с N+G (N файлов, G групп); хвост папки сессии идёт первым;
      у второй группы перед именем `01 <терминальный узел её хвоста>`; id записей: 0 / (файлов в 1-й группе + 1).
    """
    N, G = sum(len(g['files']) for g in groups), len(groups)
    assert 1 <= G <= 2
    order = sorted(range(G), key=lambda i: (0 if groups[i].get('session') else 1, i))   # хвост сессии — первым
    gvol = {gi: (groups[gi]['vol'][0], groups[gi]['vol'][1]) if groups[gi].get('vol') else (vol_name, vol_id) for gi in range(G)}
    ids, term, cur = {}, {}, N + G
    for gi in order:
        nodes, terminal = _path_nodes(groups[gi]['parent'], gvol[gi][0], gvol[gi][1])
        ids[gi] = (nodes, terminal, cur)
        cur += len(nodes)
        term[gi] = cur
        cur += 1
    out = u32(cur) + b'\x01' + u32(term[0])                       # C1 = последний терминал + 1, C2 = терминал 1-й группы
    for gi, g in enumerate(groups):
        if gi == 1:
            out += b'\x01' + u32(term[1])
        out += pstr(g['folder']) + b'\0\0\0\0'
        # id записи: у первого файла 2-й группы — число файлов до неё + 1, у остальных 0. Так Pro Tools хранит
        # сессию после сохранения (свежесозданный файл с одинаковым id во всей группе при открытии отвергается)
        first = 0 if gi == 0 else len(groups[0]['files']) + 1
        for k, f in enumerate(g['files']):
            out += b'\x02' + u32(first if k == 0 else 0) + pstr(f) + g.get('tag', b'EVAW')       # EVAW — wav, VooM — mov
    for gi in order:
        nodes, terminal, first = ids[gi]
        out += b'\x00\xff\xff\xff\xff'
        for k, label in enumerate(nodes):
            out += pstr(label) + (gvol[gi][1] if k == 0 else b'\0\0\0\0') + b'\x01' + u32(first + k)
        out += pstr(terminal) + b'\0\0\0\0'
    return ct(0x103a) + out


def _all_at(raw, pat):
    out, i = [], 0
    while True:
        i = raw.find(pat, i)
        if i < 0:
            return out
        out.append(i)
        i += 1


def _anchor(raw, hexpat, expect=1):
    pos = _all_at(raw, bytes.fromhex(hexpat.replace(' ', '')))
    assert len(pos) == expect, f'anchor {hexpat!r}: {len(pos)} matches, expected {expect}'
    return pos


UMID_LABEL = bytes.fromhex('060a2b340101010501010f1013')


def _read_bext_umid(path):
    """Базовый UMID (32 байта) из чанка bext файла BWF (так Pro Tools хранит идентификатор файла) или None."""
    try:
        with open(path, 'rb') as f:
            head = f.read(12)
            if head[:4] not in (b'RIFF', b'RF64') or head[8:12] != b'WAVE':
                return None
            pos = 12
            for _ in range(64):
                f.seek(pos)
                h = f.read(8)
                if len(h) < 8:
                    return None
                cid, sz = h[:4], struct.unpack('<I', h[4:])[0]
                if cid == b'bext':
                    d = f.read(min(sz, 420))
                    u = d[348:380]
                    return u if u.startswith(UMID_LABEL) else None
                if cid == b'data':
                    return None
                pos += 8 + sz + (sz & 1)
    except (OSError, struct.error):
        return None
    return None


def make_media(ordinal, wav, rnd):
    """0x1003: запись физического файла. Правки — по сырым байтам образца (без разбора):
    случайные байты могут выглядеть как заголовки вложенных блоков."""
    raw = bytearray(T.F1003)
    raw[9:13] = u32(ordinal)                                        # 7 (заголовок) + 2 (ctype)
    p = _anchor(raw, '80 bb 00 00 01 18 00 ee 02')[0]               # 0x1001: rate, ch, bits, length
    raw[p:p + 4] = u32(wav.rate)
    raw[p + 4] = wav.channels                                      # число каналов (интерливленный файл остаётся одним)
    raw[p + 5] = wav.bits
    raw[p + 6:p + 10] = u32(wav.frames)                           # длина файла в кадрах — u32 (у длинных файлов > 16 млн кадров старший байт не 0)
    id3 = bytes(rnd.randrange(256) for _ in range(3))
    for q in _anchor(raw, '9d 5b 56 65 ef 82 80', 2):               # id файла: 0x1001 и 0x2106
        raw[q + 1:q + 4] = id3
    q = _anchor(raw, '94 ba 11 74 b3 51 dd 01 01 00 2c')[0]
    raw[q:q + 8] = filetime(wav.mtime)                              # FILETIME: время изменения
    q = _anchor(raw, 'f8 c3 ff 73 b3 51 dd 01')[0]
    raw[q:q + 8] = filetime(wav.birth)                              # FILETIME: время создания
    q = _anchor(raw, '79 a1 85 1a d9 6c 43 c0 b6 4b 34 e5 a5 12 1d 67')[0]
    raw[q:q + 16] = bytes(rnd.randrange(256) for _ in range(16))    # UUID
    q = _anchor(raw, 'f8 d0 e0 a8 01 14 fc a4')[0]
    raw[q:q + 2] = bytes(rnd.randrange(256) for _ in range(2))
    umid = _read_bext_umid(wav.path) if getattr(wav, 'link_dir', None) else None
    if umid:                                                        # файл-ссылка: Pro Tools сверяет с файлом его UMID из bext (иначе файл считается другим и Pro Tools просит указать его)
        i = raw.find(UMID_LABEL)
        assert i > 0 and raw.find(UMID_LABEL, i + 1) < 0
        raw[i:i + 32] = umid                                       # базовый UMID целиком (метка, длина, номер экземпляра, материал)
        assert raw[44] == 0x2a
        raw[45:53] = umid[16:24]                                    # id файла в 0x1001 — первые 8 байт материала
    raw[p + 88:p + 92] = u32(getattr(wav, 'data_offset', 44))     # смещение аудиоданных в файле (у BWF с bext/junk — не 44); после якорей: они включают байт 2c
    assert len(raw) == len(T.F1003)
    return clone(bytes(raw))


# точка клип-гейна: 26 байт метаданных + Float32 (dB); -inf = особое значение Pro Tools
GAIN_POINT_META = bytes.fromhex('01460100' '16000000' '0000' '01000000' '04000000') + b'\0' * 8
assert len(GAIN_POINT_META) == 26


def gain_env_record(points):
    """Запись 0x2637 клип-гейна с огибающей (эталон clipgain.ptx): метка | тип 0x16+8k | 5 нулей | u32 n=k+1 | 04 00 u16 k | u64 id=0 | float статический дБ (0) |
    k точек (u32 время в сэмплах от начала клипа, float дБ — абсолютные значения)."""
    k = len(points)
    out = bytes.fromhex('01460100') + bytes([0x16 + 8 * k]) + b'\0' * 5 + struct.pack('<I', k + 1) + b'\x04\x00' + struct.pack('<H', k) + b'\0' * 8 + struct.pack('<f', 0.0)
    for t_, db_ in points:
        out += struct.pack('<If', int(t_), float(db_))
    return out
GAIN_NEG_INF = -290.2105712890625


def _width(n):
    """Минимальная ширина целого в байтах из набора, который встречается у Pro Tools: 1..4."""
    for w in (1, 2, 3, 4):
        if n < 1 << (8 * w):
            return w
    raise ValueError(f'{n} does not fit u32')


def _le(n, w):
    return n.to_bytes(w, 'little')


# общий хвост 0x2628 после геометрии: fe ff 00 00 00 00 ff ff 04 00 04 00 | 00 x21 | <i32 индекс клип-гейна> 00 00
# (индекс -1 = без клип-гейна, иначе номер точки в словаре 0x2637)
_COMMON_HEAD = bytes.fromhex('feff00000000ffff04000400') + b'\0' * 21


def _common_tail(gain_index, elastic=False):
    head = bytearray(_COMMON_HEAD)
    if elastic:
        head[3] = 1                                      # регион с Elastic Audio: fe ff 00 01 00 00 (эталон elastic_after)
    return bytes(head) + struct.pack('<i', gain_index) + b'\0\0'


def region_tail(whole, virtual, offset, length, gain_index=-1, elastic=False):
    """Хвост 0x2628 после имени (формат Pro Tools 26.4, моно PCM 24 бит; см. lab/oracle2, lab/oracle3).

    целый файл : 00 00 | 30 | 00 00 | len(u24)                       | fe ff ...
    подклип    : V OFF | LEN | REF 08 | offset | len | offset | offset | ff*8 | fe ff ...
      V = 00/01 (виртуальный), OFF = ширина смещения << 4 (0 если смещение 0),
      LEN = ширина длины << 4, REF = ширина копий смещения (0x22 / 0x33 / 0x44, 0 если смещение 0)
    """
    if whole:
        lw = max(3, _width(length))
        return b'\x00\x00' + bytes([lw << 4, 0, 0]) + _le(length, lw) + _common_tail(gain_index, elastic)
    lw = _width(length)
    if offset == 0:
        return (bytes([1 if virtual else 0, 0, lw << 4, 0, 8]) + _le(length, lw) + b'\xff' * 8 + _common_tail(gain_index, elastic))
    ow = max(2, _width(offset))
    ref = ow * 0x11
    return (bytes([1 if virtual else 0, ow << 4, lw << 4, ref, 8]) + _le(offset, ow) + _le(length, lw)
            + _le(offset, ow) * 2 + b'\xff' * 8 + _common_tail(gain_index, elastic))


def make_region(name, file_index, serial, offset, length, whole, rnd, gain_index=-1, channel=0, elastic=None):
    """0x2629: определение клипа. whole — целый файл-родитель; иначе подклип [offset, offset+length)."""
    b = clone(T.REGION_WHOLE)
    uuid = bytes(rnd.randrange(256) for _ in range(16))
    virtual = (offset == 0)                  # как у подклипа lab_a-01 в эталоне (offset 0 -> виртуальный)
    tail = region_tail(whole, virtual, offset, length, gain_index, bool(elastic)) + uuid
    raw = ct(0x2628) + pstr(name) + tail
    if elastic:                                           # растянутый клип: elastic = длина на таймлайне (сэмплы)
        import ptx_elastic                                # дочерний блок 0x2636 — внутри тела, сразу после `fe ff 00 01`
        k = raw.index(bytes.fromhex('feff00010000ffff0400')) + 4
        b.items[1].items[:] = [raw[:k], ptx_elastic.elastic_region_block(offset, offset, length, offset + elastic), raw[k:]]
    else:
        b.items[1].items[0] = raw
    patch(b, 2, 0, u32(file_index))                      # ident48: индекс файла
    patch(b, 2, 4, u32(channel))                         # ident48: номер канала файла (у моно 0)
    patch(b, 2, 23, uuid)                                # ident48: UUID
    patch(b, 4, 91, bytes([serial & 0xFF]))              # link104: серийный номер объекта
    patch(b, 4, 96, u32(file_index))                     # link104: индекс файла
    return b


# Цвет маркера в .ptx: RGB хранится в первом дочернем блоке 0x4826 (01 | R | G | B — по u16 BE, значение в младшем байте), «истина» — именно он
# (оракул PTSL CreateMemoryLocation + правка байтов: Pro Tools сохраняет любой RGB). Байт кода перед `00 04 00 00 00` пересчитывается PT при загрузке
# (11 — свой цвет, 17 — цвет по умолчанию, зелёный 00db32), поэтому по нему цвет не определять.
MARKER_CUSTOM_RAW = 11


def make_marker(index, name, sample, rnd, end=None, color=None, comment=''):
    """0x2077: маркер в формате Pro Tools 26.4 (ptx_templates_marker.MARKER264, сохранён самим PT); end — конец диапазона: тогда это Selection
    (time properties = Selection); color — 0xRRGGBB (None — цвет по умолчанию); comment — комментарий маркера (хранится в двух местах)."""
    from ptx_templates_marker import MARKER264
    b = clone(MARKER264)
    head = bytes(b.items[0])                                 # ct | idx u16 | flags 4 | name | ts1 | ts2 | rest
    old_n = struct.unpack_from('<I', head, 8)[0]
    rest = head[8 + 4 + old_n + 16:]
    flags = bytearray(head[4:8])
    if end is not None and end > sample:
        flags[1] = (flags[1] & ~3) | 2                       # байт 5 заголовка: младшие 2 бита — time properties (1 — Marker, 2 — Selection, 0 — None)
    j = rest.find(bytes.fromhex('ffffffff04000000000000000100000000')) + 17     # здесь первая копия комментария (pstr; в образце пустая)
    assert j >= 17
    rest = rest[:j] + pstr(comment) + rest[j + 4:]
    if color is not None and color != 'keep':
        rest = rest[:-6] + bytes([MARKER_CUSTOM_RAW]) + rest[-5:]                  # ... XX 00 04 00 00 00 | 00 00 00 00: XX — код цвета (11 — свой цвет)
    if color != 'keep' and color is not None:                # RGB маркера: первый дочерний 0x4826 = 01 | R(u16 BE) | G | B (в младших байтах; белый — ff ff)
        r_, g_, b_ = (color >> 16) & 255, (color >> 8) & 255, color & 255
        rgb = bytes([1]) + b''.join(bytes([255, 255]) if v == 255 else bytes([0, v]) for v in (r_, g_, b_))
        k = [x for x in b.items if isinstance(x, Block) and x.ctype == 0x4826][0]
        k.items = [bytes(k.items[0])[:2] + rgb]
    b.items[0] = head[:2] + u16(index) + bytes(flags) + pstr(name) + struct.pack('<Q', sample) + struct.pack('<Q', end if end is not None and end > sample else sample) + rest
    tail = bytearray(b'\0' * 4 + pstr(comment) + bytes(b.items[5])[8:])       # вторая копия комментария и UUID
    tail[-28:-12] = bytes(rnd.randrange(256) for _ in range(16))
    b.items[5] = bytes(tail)
    return b


def make_event(kind, ident, position, mute=False, color=None):
    """0x1050: audio (type 3) или fade (type 1); position — UInt64 в сэмплах; color — 0xRRGGBB цвет клипа (ближайший цвет
    палитры; u16 в 0x104f own[16:18], 0xfffe — по умолчанию; эталон cc_a/cc_b)."""
    b = clone(T.EVENT_AUDIO if kind == 'audio' else T.EVENT_FADE)
    e = kids(b, 0x104f)[0]
    patch(e, 0, 2 + 0, bytes([1 if mute else 0]))
    patch(e, 0, 2 + 2, u32(ident))
    patch(e, 0, 2 + 7, struct.pack('<Q', position))
    if color is not None and kind == 'audio':
        from ptx_trackstate import nearest_palette_index
        patch(e, 0, 2 + 16, u16(nearest_palette_index(color)))
    return b


def make_geometry(kind, length, pre=0):
    b = clone({'in': T.GEOM_IN, 'out': T.GEOM_OUT, 'xfade': T.GEOM_XFADE}[kind])
    patch(b, 0, 2 + 8, u16(length))
    if kind == 'out':
        patch(b, 0, 2 + 10, u16(length))
    return b


# ---------------------------------------------------------- сборка ----------

class Placement:
    def __init__(self, track, wav, clip_name, start, length=None, fade_in=0, fade_out=0, mute=False, offset=0, gain_db=0.0, label=None,
                 playlist=None, color=None, stretch=None, gain_env=None, gid=0):
        self.track, self.wav, self.name = track, wav, clip_name
        self.gid = gid                                  # номер группы items Reaper (0 — вне группы): на моно-дорожке станет Clip Group Pro Tools
        self.gain_env = tuple(gain_env) if gain_env else None   # огибающая клип-гейна: ((сэмплы от начала клипа, дБ), ...)
        self.playlist = playlist or track              # имя плейлиста (у основного совпадает с именем дорожки)
        self.gain_db, self.label = gain_db, label
        self.start, self.offset = start, offset
        self.length = length if length is not None else wav.frames
        self.fade_in, self.fade_out, self.mute = fade_in, fade_out, mute
        self.color = color                              # 0xRRGGBB или None (цвет по умолчанию)
        self.stretch = stretch                          # длина на таймлайне (сэмплы) у растянутого клипа (Elastic Audio) или None


class SessionBuilder:
    def __init__(self, template_bytes, session_dir, vol=('Macintosh HD', bytes.fromhex('356c7be0'), '/'),
                 seed=int(os.environ.get('PTX_SEED', '1')), crossfades=True):
        self.p = PTX(template_bytes)
        self.session_dir = session_dir
        self.vol = vol
        self.link_vol = None             # том файлов в режиме Link to source (если не совпадает с томом сессии)
        self.rnd = random.Random(seed)
        self.crossfades = crossfades       # False: перекрытия остаются простым наложением, без кроссфейдов
        self.files = []                  # WavInfo в порядке первого использования
        self.placements = []
        self.markers = []                # (имя, позиция в сэмплах, конец диапазона в сэмплах или None, цвет 0xRRGGBB или None, комментарий)
        self.track_states = {}           # имя дорожки -> {mute, solo, color}
        self.track_comments = {}         # имя дорожки -> текст комментария
        self.track_auto = {}             # имя дорожки -> [(сэмпл, дБ | None)] автоматизация громкости
        self.clip_groups = False         # группы items Reaper (Placement.gid) -> Clip Groups Pro Tools
        self.empty_groups = []           # пустые items Reaper (с именем в NOTES) -> пустые Clip Groups: [{'track', 'start', 'end', 'name'}]
        self.bpm = 0.0                   # постоянный темп проекта Reaper (BPM) -> темп сессии; 0 — не менять
        self.ptx_path = None             # путь итогового .ptx (в блок 0x2425 группы)
        self.groups_made = 0

    def set_track_state(self, name, mute=None, solo=None, color=None, volume_db=None, pan=None, rec=None):
        self.track_states[name] = dict(mute=mute, solo=solo, color=color, volume_db=volume_db, pan=pan, rec=rec)

    def set_track_comment(self, name, text):
        self.track_comments[name] = text

    def set_track_auto(self, name, points, param='vol'):
        self.track_auto[(name, param)] = points

    def add_marker(self, name, sample, end=None, color=None, comment=''):
        self.markers.append((name, sample, end, color, comment))

    def add(self, pl):
        if pl.wav.path not in [w.path for w in self.files]:
            self.files.append(pl.wav)
        self.placements.append(pl)

    # --- основная операция ---------------------------------------------------
    def build(self):
        p = self.p
        # порядок каталога: сначала файлы вне сессии (ссылки), затем файлы из Audio Files сессии
        ext = [w for w in self.files if w.link_dir]
        loc = [w for w in self.files if not w.link_dir]
        if ext and len({w.link_dir for w in ext}) > 1:
            raise ValueError('linked files must come from one folder')
        self.files = ext + loc
        names = [os.path.basename(w.path) for w in self.files]
        findex = {w.path: i for i, w in enumerate(self.files)}
        groups = []
        if ext:
            groups.append({'folder': os.path.basename(ext[0].link_dir), 'parent': os.path.dirname(ext[0].link_dir),
                           'files': [os.path.basename(w.path) for w in ext], 'session': False,
                           'vol': self.link_vol or self.vol})
        if loc:
            groups.append({'folder': 'Audio Files', 'parent': self.session_dir,
                           'files': [os.path.basename(w.path) for w in loc], 'session': True})

        # граница сессии: шаблон создан с 2 минутами (00:02:00:00), а клипы длиннее -> Pro Tools при открытии
        # спрашивает про расширение и убирает клипы за границей. Ставим максимум 24 часа, как это делает сам Pro Tools.
        r204d = find_root(p, 0x204d)
        r204d.items = [i.replace(b'00:02:00:00', b'24:00:00:00') if isinstance(i, bytes) else i for i in r204d.items]

        # том и каталог файлов
        r0f3d = find_root(p, 0x0f3d)
        if struct.unpack_from('<I', r0f3d.items[0], 2)[0] == 0:
            # таблица томов: Pro Tools перечисляет тома, на которых лежат файлы каталога (в сессии из эталона lkref: только том ссылок).
            # Без тома ссылочных файлов в таблице файл числится недоступным и Pro Tools спрашивает, где он.
            vols = []
            for v in ([self.link_vol or self.vol] if ext else []) + ([self.vol] if (loc or not ext) else []):
                if (v[0], v[1]) not in [(x[0], x[1]) for x in vols]:
                    vols.append(v)
            for v in vols:
                bump(r0f3d)
                vb = make_volume_block(*v)
                r0f3d.items.append(vb)
                p.extra.append((r0f3d, vb))             # запись в таблице 0x0002 (как у Pro Tools)
        r1004 = find_root(p, 0x1004)
        patch(r1004, 0, 2, u32(len(self.files)))
        catalog = kids(r1004, 0x103a)[0]
        if groups:                                      # без аудиофайлов остаётся каталог шаблона
            catalog.items = [make_catalog_groups(groups, self.vol[0], self.vol[1])]
        pos = r1004.items.index(catalog) + 1
        for i, w in enumerate(self.files, 1):
            ordinal = i + (1 if (ext and not w.link_dir) else 0)        # во 2-й группе номер сдвинут на 1 (эталоны k2..k4)
            r1004.items.insert(pos, make_media(ordinal, w, self.rnd))
            pos += 1

        # регионы: целый файл-родитель есть всегда; подклипы — по мере надобности. У многоканального файла на каждый
        # канал свой регион (имя с суффиксом .L/.R для стерео, .A1..An для остальных); событие на дорожке — по
        # плейлисту на канал. Список клипов у Pro Tools отсортирован по имени, индексы событий считаем после сортировки.
        r262a = find_root(p, 0x262a)
        specs = {}                                      # key -> dict(name, fi, length, whole, ch)
        for w in self.files:
            specs[(w.path, 0, w.frames, 0.0, None)] = dict(name=os.path.splitext(os.path.basename(w.path))[0],
                                                     fi=findex[w.path], offset=0, length=w.frames, whole=True, gain=0.0,
                                                     ch=w.channels)
        sub_count = {}
        used_names = {sp['name'].lower() for sp in specs.values()}

        def region_key(pl, length):
            key = (pl.wav.path, pl.offset, length, round(pl.gain_db, 2), pl.label, pl.stretch, pl.gain_env)
            if key not in specs:
                stem = os.path.splitext(os.path.basename(pl.wav.path))[0]
                if pl.label:                                       # своё имя (например MISSING <файл>)
                    name, k = pl.label, 1
                    while name.lower() in used_names:
                        k += 1
                        name = f'{pl.label} {k}'
                else:
                    n = sub_count.get(stem, 0) + 1
                    sub_count[stem] = n
                    name = f'{stem}-{n:02d}'
                used_names.add(name.lower())
                specs[key] = dict(name=name, fi=findex[pl.wav.path], offset=pl.offset,
                                  length=length, whole=False, gain=round(pl.gain_db, 2), ch=pl.wav.channels, elastic=pl.stretch, env=pl.gain_env)
            return key

        # геометрии фейдов создаются по одной на (событие, канал)
        r2630 = find_root(p, 0x2630)
        ngeom = [0]

        def add_geom(kind, length):
            r2630.items.append(make_geometry(kind, length))
            ngeom[0] += 1
            return ngeom[0] - 1

        # события по дорожкам: порядок (позиция, приоритет); id региона/геометрии подставляем при записи плейлиста
        per_track = {}
        for pl in self.placements:
            per_track.setdefault(pl.playlist, []).append(pl)
        track_ch = {}
        for track, pls in per_track.items():
            chs = {pl.wav.channels for pl in pls}
            if len(chs) != 1:
                raise ValueError(f'track {track!r} mixes files with different channel counts: {sorted(chs)}')
            track_ch[track] = chs.pop()

        events = {}                                     # track -> [(pos, prio, kind, key | (вид, длина), extra)]
        for track, pls in per_track.items():
            pls.sort(key=lambda x: x.start)
            ev = events.setdefault(track, [])
            prev_xf = False
            for i, pl in enumerate(pls):
                nxt = pls[i + 1] if i + 1 < len(pls) else None
                end = pl.start + (pl.stretch or pl.length)          # конец на таймлайне (у растянутого клипа — с учётом растяжки)
                length = pl.length
                xf = None
                # перекрытие двух клипов (правый выступает за левый) -> кроссфейд; вложенный клип
                # и слишком длинное перекрытие (> u16) остаются обычным наложением
                if self.crossfades and nxt and not pl.stretch and not nxt.stretch and nxt.start < end < nxt.start + nxt.length \
                        and end - nxt.start <= 65535 and nxt.start > pl.start:
                    xf = end - nxt.start
                    length = nxt.start - pl.start
                key = region_key(pl, length)
                if pl.fade_in and not prev_xf:
                    ev.append((pl.start, 0, 'fade', ('in', pl.fade_in), None))
                prev_xf = bool(xf)
                ev.append((pl.start, 1, 'audio', key, (pl.mute, pl.color)))
                if pl.fade_out and not xf:
                    ev.append((end, FADEOUT_PRIO, 'fade', ('out', pl.fade_out), None))
                if xf:
                    ev.append((nxt.start, 0, 'fade', ('xfade', xf), None))

        def ch_name(base, c, C):
            if C == 1:
                return base
            return f'{base}.{"LR"[c]}' if C == 2 else f'{base}.A{c + 1}'

        rlist = []                                       # (имя региона, key, канал)
        for k, sp in specs.items():
            for c in range(sp['ch']):
                rlist.append((ch_name(sp['name'], c, sp['ch']), k, c))
        rlist.sort(key=lambda t: (t[0].lower(), t[0]))
        region_index = {(k, c): i for i, (_, k, c) in enumerate(rlist)}
        serial = 0x30
        r2637 = find_root(p, 0x2637)
        npoints = struct.unpack_from('<I', r2637.items[0], 2)[0]
        for nm, k, c in rlist:
            sp = specs[k]
            gi = -1
            if sp.get('env'):                                # клип-гейн с огибающей: запись типа 0x16 + 8*(n-1), точки (u32 сэмплы, float дБ)
                gi = npoints
                npoints += 1
                pts = sp['env']
                r2637.items[0] += gain_env_record(pts)
                patch(r2637, 0, 2, u32(npoints))
            elif sp['gain']:
                gi = npoints
                npoints += 1
                r2637.items[0] += GAIN_POINT_META + struct.pack('<f', GAIN_NEG_INF if sp['gain'] <= -200 else sp['gain'])
                patch(r2637, 0, 2, u32(npoints))
            r262a.items.append(make_region(nm, sp['fi'], serial, sp['offset'], sp['length'], sp['whole'],
                                           self.rnd, gi, channel=c, elastic=sp.get('elastic')))
            serial += 1

        bump(r262a, len(rlist))

        # плейлисты: у многоканальной дорожки по одному плейлисту (с именем дорожки) на канал
        pl_root = find_root(p, 0x1054)
        seen = {}
        for t in kids(pl_root, 0x1052):
            hdr = t.items[0]
            nlen = struct.unpack_from('<I', hdr, 2)[0]
            tname = hdr[6:6 + nlen].decode()
            c = seen.get(tname, 0)
            seen[tname] = c + 1
            if tname not in events:
                continue
            if c >= track_ch[tname]:
                continue                                 # лишний плейлист (дорожка в шаблоне многоканальнее файлов)
            ev = []
            for pos, prio, kind, ref, extra in sorted(events[tname], key=lambda e: (e[0], e[1])):
                ident = region_index[(ref, c)] if kind == 'audio' else add_geom(*ref)
                ev.append((pos, prio, make_event(kind, ident, pos, bool(extra[0]) if kind == 'audio' else False, extra[1] if kind == 'audio' else None)))
            trailer = t.items[-1]                        # '01 00' в голове пустой плейлиста
            if len(t.items) == 1:                        # пустой: '... count(4) 01 00' одним сегментом
                head = hdr[:-6]
                trailer = hdr[-2:]
                t.items = [head + u32(len(ev))] + [e[2] for e in ev] + [trailer]
            else:
                raise NotImplementedError('track already has events')
        for tname, C in track_ch.items():
            if seen.get(tname, 0) < C:
                raise ValueError(f'track {tname!r} has {seen.get(tname, 0)} playlists but its files have {C} channels')
        bump(r2630, ngeom[0])

        if self.markers:
            self._write_markers()
        if self.track_states:
            import ptx_trackstate as TS
            order = [n for n, *_ in TS.aggregate_names(p)]
            for name, st in self.track_states.items():
                if name in order:
                    TS.write_state(p, order.index(name), mute=st['mute'], solo=st['solo'], color=st['color'], rec=st.get('rec'),
                                   volume_db=st['volume_db'], pan=st['pan'])
        if self.track_auto:
            import ptx_trackstate as TS
            import ptx_sends
            order = [n for n, *_ in TS.aggregate_names(p)]
            for (name, param), pts in self.track_auto.items():
                if name in order:
                    ptx_sends.write_auto(p, order.index(name), pts, 0, param)
        stretched = sorted({pl.track for pl in self.placements if pl.stretch})
        if stretched:                                    # Elastic Audio (Polyphonic) на дорожках с растянутыми клипами
            import ptx_trackstate as TS
            import ptx_elastic
            order = [n for n, *_ in TS.aggregate_names(p)]
            for name in stretched:
                if name in order:
                    ptx_elastic.enable_track(p, order.index(name))
        if self.bpm:
            import ptx_tempo
            ptx_tempo.write_bpm(p, self.bpm)
        if self.clip_groups:
            specs = self._group_specs(track_ch) + [dict(e, empty=True) for e in self.empty_groups]
            if specs:
                import ptx_groups
                self.groups_made = ptx_groups.apply_groups(p, specs, self.rnd, self.ptx_path or os.path.join(self.session_dir, 'session.ptx'), self.vol[0], self.vol[1])
        if self.track_comments:                          # в самом конце: текст сдвигает байты за ним в блоке 0x2015
            import ptx_trackstate as TS
            order = [n for n, *_ in TS.aggregate_names(p)]
            for name, text in self.track_comments.items():
                if name in order:
                    TS.write_comment(p, order.index(name), text)
        return self.p.save()

    def _group_specs(self, track_ch):
        """Группы items Reaper -> [{'track', 'start', 'end'}]: только основной плейлист дорожки (моно / стерео / 5.1), >= 2 клипа, внутри диапазона нет чужих клипов."""
        by = {}
        for pl in self.placements:
            if pl.gid and pl.playlist == pl.track and track_ch.get(pl.track) in (1, 2, 6):
                by.setdefault((pl.track, pl.gid), []).append(pl)
        specs = []
        for (track, gid), mem in by.items():
            if len(mem) < 2:
                continue
            start = min(m.start for m in mem)
            end = max(m.start + (m.stretch or m.length) for m in mem)
            foreign = [o for o in self.placements if o.track == track and o.playlist == o.track and o not in mem and o.start < end and o.start + (o.stretch or o.length) > start]
            if foreign:
                continue
            specs.append({'track': track, 'start': start, 'end': end})
        return specs

    def _write_markers(self):
        p = self.p
        r2030 = find_root(p, 0x2030)
        if struct.unpack_from('<I', r2030.items[0], 2)[0] != 0:
            raise NotImplementedError('template already has markers')
        n = len(self.markers)
        head = r2030.items[0]
        r2030.items = [head[:2] + u32(n)]
        for i, (name, sample, end, color, comment) in enumerate(sorted(self.markers, key=lambda m: m[1]), 1):
            r2030.items.append(make_marker(i, name, sample, self.rnd, end, color, comment))
        r2030.items.append(b'\0' * 8)
        # 0x201f: поле «последний индекс маркера» (u32 @33 сырого сегмента после дочернего блока)
        r201f = find_root(p, 0x201f)
        j = [k for k, x in enumerate(r201f.items) if isinstance(x, bytes)][-1]
        patch(r201f, j, 33, struct.pack('<i', n - 1))
        # 0x251c первой линейки маркеров: флаг «используется»
        r2519 = find_root(p, 0x2519)
        for c in kids(r2519, 0x251b):
            first = kids(c, 0x251c)[0]
            patch(first, 0, 2 + 6, b'\x01')


def build_session(template_path, out_dir, name, placements, vol=None):
    """Создаёт <out_dir>/<name>/<name>.ptx и копирует аудио в Audio Files (v1)."""
    import shutil
    sdir = os.path.join(out_dir, name)
    if os.path.exists(sdir):
        raise FileExistsError(sdir)
    os.makedirs(os.path.join(sdir, 'Audio Files'))
    b = SessionBuilder(open(template_path, 'rb').read(), sdir, **({'vol': vol} if vol else {}))
    copied = {}
    for pl in placements:
        base = os.path.basename(pl.wav.path)
        dst = os.path.join(sdir, 'Audio Files', base)
        if pl.wav.path not in copied:
            shutil.copy2(pl.wav.path, dst)
            copied[pl.wav.path] = WavInfo(dst)
        pl.wav = copied[pl.wav.path]
        b.add(pl)
    data = b.build()
    open(os.path.join(sdir, name + '.ptx'), 'wb').write(data)
    return os.path.join(sdir, name + '.ptx')
