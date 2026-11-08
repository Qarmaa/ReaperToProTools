#!/usr/bin/env python3
"""ptx_video.py — видеодорожка в .ptx (запись) и сведения о видеофайле (.mov/.mp4).

Эталон — lab/video/v0_base.ptx -> v1_start.ptx (Pro Tools Studio: ImportVideo в пустую сессию; v2_pos — то же в позицию 4324320 сэмплов).
Что добавляет видеофайл с одной видеодорожкой:
  * контейнер 0x2623 в 0x2624, две записи 0x251a (по семье), 0x210b — как у остальных дорожек (ptx_structure.insert_track, вид 'video');
  * в агрегате 0x2519 первые 17 байт «головы»: байты 4..12 = частота кадров видео (double); тип записи 0x08, B6 = 0c 00 00 00 00 00;
  * корень 0x1006 (счётчик 1): каталог 0x103a с файлом (тег формата `VooM`, хвост пути как у аудиофайлов вне сессии) и дескриптор 0x1005:
    double fps | u32 кадров | u32 0 | u32 (случайный, у нас 0) | u32 0 | u16 высота | u16 ширина | FILETIME файла | 16 байт GUID;
  * корень 0x262e (счётчик 1): регион 0x262d>0x2628: pstr имя | 00 02 W0 xx 00 | длина в кадрах (W = 2 или 3 байта) | [xx=0x33: 2 x 3 байта TC] | fe ff ... | GUID;
  * корень 0x1055 (счётчик 1): плейлист 0x1053 (pstr имя | 01 00 00 00) > 0x1051 > 0x104f: u32 кадров от начала сессии по смещению 9, mute — o[2];
  * 0x201f own[48:52] = ffffffff (признак «в сессии есть видео»; в шаблонах 05 00 00 00);
  * таблица: запись 0x1053 в серии корня 0x1055 (+1 к счётчику серии), общий счётчик +2 (как у aux).
Pro Tools Studio принимает ОДНУ видеодорожку (ImportVideo второго файла: «does not support requested amount of video tracks»).
Длина региона — поле шириной 2 или 3 байта (0x20 / 0x30 в третьем байте), т.е. до 16 777 215 кадров.
"""
import os
import random
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ptx_model import Block  # noqa: E402
from ptx_build import clone, find_root, kids, pstr, filetime, make_catalog_groups  # noqa: E402
import ptx_structure as ST  # noqa: E402
import ptx_templates_struct as TPL  # noqa: E402

MAX_FRAMES = 0xFFFFFF


# ------------------------------------------------------------------------------------------- сведения о файле

def _atoms(buf, start, end):
    i = start
    while i + 8 <= end:
        size, typ = struct.unpack_from('>I4s', buf, i)
        hdr = 8
        if size == 1 and i + 16 <= end:
            size = struct.unpack_from('>Q', buf, i + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            break
        yield typ, i + hdr, i + size
        i += size


def mov_info(path):
    """{'fps', 'frames', 'width', 'height', 'mtime'} видеофайла QuickTime/MP4; None, если разобрать не удалось.
    Частота — число отсчётов в секунду / длительность отсчёта из stts, число кадров — сумма отсчётов (stts)."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            moov = None
            pos = 0
            while pos + 8 <= size:
                f.seek(pos)
                hdr = f.read(16)
                sz, typ = struct.unpack_from('>I4s', hdr, 0)
                h = 8
                if sz == 1:
                    sz = struct.unpack_from('>Q', hdr, 8)[0]
                    h = 16
                elif sz == 0:
                    sz = size - pos
                if sz < h:
                    break
                if typ == b'moov':
                    f.seek(pos + h)
                    moov = f.read(sz - h)
                    break
                pos += sz
        if moov is None:
            return None
        for typ, a, b in _atoms(moov, 0, len(moov)):
            if typ != b'trak':
                continue
            hdlr = tkhd = stts = mdhd = stsd = None
            def walk(s, e):
                nonlocal hdlr, tkhd, stts, mdhd, stsd
                for t, x, y in _atoms(moov, s, e):
                    if t in (b'mdia', b'minf', b'stbl'):
                        walk(x, y)
                    elif t == b'hdlr' and hdlr is None:                    # первый hdlr — медиа-уровня (в minf есть ещё один, для dinf)
                        hdlr = moov[x + 8:x + 12]
                    elif t == b'mdhd':
                        mdhd = (x, y)
                    elif t == b'stts':
                        stts = (x, y)
                    elif t == b'stsd':
                        stsd = (x, y)
            walk(a, b)
            if hdlr != b'vide' or not (mdhd and stts):
                continue
            ver = moov[mdhd[0]]
            timescale = struct.unpack_from('>I', moov, mdhd[0] + (20 if ver == 1 else 12))[0]
            n = struct.unpack_from('>I', moov, stts[0] + 4)[0]
            frames, first_delta = 0, 0
            for k in range(n):
                cnt, delta = struct.unpack_from('>II', moov, stts[0] + 8 + 8 * k)
                frames += cnt
                first_delta = first_delta or delta
            if not first_delta:
                continue
            w = h = 0
            if stsd:
                w, h = struct.unpack_from('>HH', moov, stsd[0] + 8 + 8 + 24)       # visual sample entry: width/height u16
            return {'fps': timescale / first_delta, 'frames': frames, 'width': w, 'height': h, 'mtime': os.path.getmtime(path)}
    except (OSError, struct.error, IndexError):
        return None
    return None


# ------------------------------------------------------------------------------------------------ запись в сессию

def _bump_count(root_blk, n):
    o = bytes(root_blk.items[0])
    root_blk.items[0] = o[:2] + struct.pack('<I', n) + o[6:]


def add_video(p, path, info, pos_frames=0, mute=False, vol=None, name=None, rnd=None, tc_frames=None):
    """Добавляет в сессию p (PTX) видеодорожку с одним клипом.

    path — путь к видеофайлу, info — mov_info(path) (fps, frames, width, height, mtime), pos_frames — положение клипа в кадрах от начала
    сессии, vol — (имя тома, id тома) для файла на внешнем диске (или None: загрузочный том / сетевой). Возвращает имя дорожки."""
    rnd = rnd or random.SystemRandom()
    if find_root(p, 0x1055) is None or kids(find_root(p, 0x1055), 0x1053):
        raise ValueError('video track already present or template without video roots')
    fps, frames = info['fps'], info['frames']
    if frames > MAX_FRAMES:
        raise ValueError(f'video is {frames} frames; region length is stored as u16 (max {MAX_FRAMES})')
    base = os.path.basename(path)
    name = name or os.path.splitext(base)[0]
    N = len(ST.TS.track_blocks(p))
    ST.insert_track(p, 'video', name, N, rnd=rnd)
    # голова агрегата 0x2519: частота кадров видео
    r2519 = find_root(p, 0x2519)
    raw = bytearray(r2519.items[0])
    raw[4:12] = struct.pack('<d', fps)
    r2519.items[0] = bytes(raw)
    # 0x201f own[48:52] = ff ff ff ff
    r201f = find_root(p, 0x201f)
    seg = r201f.items[-1]
    r201f.items[-1] = bytes(seg[:48]) + b'\xff\xff\xff\xff' + bytes(seg[52:])

    # --- полоса микшера видеодорожки (0x2587 > 0x2551 > 0x258b > 0x258a): без неё Pro Tools не открывает сессию («end of stream») ----------------
    r258b = kids(kids(find_root(p, 0x2587), 0x2551)[0], 0x258b)[0]
    r258a = kids(r258b, 0x258a)[0]
    strips = kids(r258a, 0x2589)
    strip = clone(TPL.VIDEO['strip'])
    hdr = bytearray(strip.items[0])
    hdr[2:4] = struct.pack('<H', N)                                # индекс дорожки (0-based)
    strip.items[0] = bytes(hdr)
    r258a.items.insert(r258a.items.index(strips[-1]) + 1, strip)
    k = r258a.items.index(strips[0]) - 1
    r258a.items[k] = struct.pack('<I', struct.unpack('<I', bytes(r258a.items[k]))[0] + 1)      # число полос
    r2553 = kids(r258b, 0x2553)[0]
    seg = bytearray(r2553.items[0])
    struct.pack_into('<I', seg, 7, struct.unpack_from('<I', seg, 7)[0] + 6)                    # +6 на видеополосу
    r2553.items[0] = bytes(seg)

    # --- каталог видеофайлов и дескриптор (0x1006) ---------------------------
    r1006 = find_root(p, 0x1006)
    _bump_count(r1006, 1)
    folder = os.path.basename(os.path.dirname(path)) or 'Video Files'
    parent = os.path.dirname(os.path.dirname(path))
    vol = vol or ('Macintosh HD', bytes.fromhex('00000000'))
    cat = make_catalog_groups([{'folder': folder, 'parent': parent, 'files': [base], 'vol': vol, 'tag': b'VooM'}], vol[0], vol[1])
    kids(r1006, 0x103a)[0].items = [cat]
    desc = clone(TPL.VIDEO['desc'])
    d = bytearray(desc.items[-1])
    d[0:8] = struct.pack('<d', fps)
    d[8:12] = struct.pack('<I', frames)
    d[12:24] = b'\0' * 12
    d[24:28] = struct.pack('<HH', info.get('height') or 0, info.get('width') or 0)
    d[28:36] = filetime(info.get('mtime') or 0)
    d[36:52] = bytes(rnd.randrange(256) for _ in range(16))
    desc.items[-1] = bytes(d)
    r1006.items.append(desc)

    # --- регионы (0x262e): как у Pro Tools при ImportVideo в непустую сессию — регион целого файла и регион клипа «<имя>-01» ------------------
    r262e = find_root(p, 0x262e)
    _bump_count(r262e, 2)
    w = 2 if frames <= 0xFFFF else 3                                                # ширина поля длины (u16 / u24) кодируется в третьем байте: 0x20 / 0x30
    for k, (rname, sub) in enumerate(((name, False), (name + '-01', True))):
        reg = clone(TPL.VIDEO['region'])
        r2628 = kids(reg, 0x2628)[0]
        old = bytes(r2628.items[0])
        n = struct.unpack_from('<I', old, 2)[0]
        q = old.index(b'\xfe\xff', 6 + n)                       # после `00 02 20 xx 00 <длина> [TC]` идёт `fe ff`
        tail = bytearray(old[q:])
        tail[-16:] = bytes(rnd.randrange(256) for _ in range(16))
        cbyte = 0x33 if tc_frames is not None else 0                # C: ширины (3+3 байта) двух копий начального таймкода файла (кадры); 0 — копий нет
        tcb = (tc_frames.to_bytes(3, 'little') * 2) if tc_frames is not None else b''
        if sub:                                                   # регион клипа: `01 02 W0 C 08 <длина> [TC TC] ff*8 fe ff ...`
            head_b = bytes([1, 2, w << 4, cbyte, 8]) + frames.to_bytes(w, 'little') + tcb + b'\xff' * 8
        else:                                                     # регион файла: `00 02 W0 C 00 <длина> [TC TC] fe ff ...`
            head_b = bytes([0, 2, w << 4, cbyte, 0]) + frames.to_bytes(w, 'little') + tcb
        r2628.items[0] = old[:2] + pstr(rname) + head_b + bytes(tail)
        r262e.items.append(reg)

    # --- плейлист (0x1055) -------------------------------------------------------
    r1055 = find_root(p, 0x1055)
    _bump_count(r1055, 1)
    pl = clone(TPL.VIDEO['playlist'])
    hdr = bytes(pl.items[0])
    nn = struct.unpack_from('<I', hdr, 2)[0]
    pl.items[0] = hdr[:2] + pstr(name) + hdr[6 + nn:]
    ev = kids(kids(pl, 0x1051)[0], 0x104f)[0]
    o = bytearray(ev.items[0])
    o[2] = 1 if mute else 0
    o[9:13] = struct.pack('<I', int(pos_frames))
    o[4:8] = struct.pack('<I', 1)                                  # ident клипа = регион «<имя>-01»
    ev.items[0] = bytes(o)
    r1055.items.append(pl)

    # --- таблица: запись 0x1053 в серии корня 0x1055, общий счётчик +1 (уже сделан insert_track) ---------------
    tk = p.tokens
    i0 = ST._find_tok(tk, 'rec', r1055)
    ST._be16_bump(p, tk, i0)
    tk.insert(i0 + 1, ('rec', pl))
    return name


def read_back(p):
    """Видео сессии (для проверки): тот же разбор, что в ptx_to_reaper.parse_video."""
    import ptx_to_reaper as R
    return R.parse_video(p)


if __name__ == '__main__':
    import json
    print(json.dumps(mov_info(sys.argv[1]), indent=1))
