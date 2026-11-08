#!/usr/bin/env python3
"""
reaper_to_ptx.py — проект Reaper (.rpp) -> сессия Pro Tools (.ptx) БЕЗ запуска Pro Tools.

    reaper_to_ptx.py project.rpp --out <папка> [--name <имя>] [--templates <папка с pool_N>]
                     [--media-dir <папка>] [--offset <сек>] [--no-fades] [--info]

Берёт шаблон с нужным числом дорожек (pool_N, сделан Pro Tools один раз), переименовывает дорожки,
расставляет клипы (с обрезкой), фейды и кроссфейды по перекрытиям. Аудио копируется в Audio Files.
Печатает прогресс строками `label: n/total` и итог `Done: ...` (как reaper_import).

Ограничения v1: моно WAV PCM 24 бит; item'ы с playrate != 1, MIDI, видео, не-WAV пропускаются.
"""
import argparse
import collections
import json
import math
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rpp                                              # noqa: E402
from ptx_build import SessionBuilder, WavInfo, Placement  # noqa: E402
from ptx_model import PTX                                # noqa: E402
from ptx_tracks import rename_tracks, EXPECTED_PLACES      # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_RATE = 48000
MAX_FADE = 65535


BOOT_VOLUME = ('Macintosh HD', bytes.fromhex('356c7be0'), '/')       # снято с Pro Tools на этом Mac


_VOL_CACHE_FILE = os.path.expanduser('~/Library/Application Support/Reaper to Pro Tools/volumes.json')
_VOL_MEM = {}


def _vol_cache_load():
    try:
        return json.load(open(_VOL_CACHE_FILE))
    except Exception:                                    # noqa: BLE001
        return {}


def _vol_cache_save(c):
    try:
        os.makedirs(os.path.dirname(_VOL_CACHE_FILE), exist_ok=True)
        json.dump(c, open(_VOL_CACHE_FILE, 'w'))
    except Exception:                                    # noqa: BLE001
        pass


PLACEHOLDER_WHY = 'inactive placeholder: plug-in not found for Pro Tools'


def missing_plugins_comment(lst):
    """Текст комментария дорожки Pro Tools: какие эффекты Reaper не перенесены (и почему) и какие вставлены неактивными заглушками."""
    lines = []
    miss = [x for x in lst if x[2] != PLACEHOLDER_WHY]
    if miss:
        lines.append('Not transferred from Reaper:')
        for origin, label in (('clip effect', 'Clip effects: '), ('track effect', 'Track effects: '), ('master effect', 'Master effects: ')):
            names = [n for n, o, why in miss if o == origin]
            if names:
                lines.append(label + ', '.join(names))
        if any(why == 'not in the plug-in library' for _, _, why in miss):
            lines.append('(reason: these plug-ins are not in the converter plug-in library)')
        if any(why == 'no free insert slot' for _, _, why in miss):
            lines.append('(reason: Pro Tools allows 10 inserts per track)')
    ph = [n for n, _, why in lst if why == PLACEHOLDER_WHY]
    if ph:
        lines.append('Inactive placeholders from Reaper (plug-in not found for Pro Tools, settings not transferred): ' + ', '.join(ph))
    return '\n'.join(lines)


_NET_FS = ('smbfs', 'nfs', 'afpfs', 'webdav', 'cifs', 'ftp')


def _is_network_volume(vol):
    """Том смонтирован по сети (SMB / NFS / AFP): сканировать его в поисках сессий слишком долго, id там не нужен."""
    import subprocess
    try:
        out = subprocess.run(['/sbin/mount'], capture_output=True, text=True, timeout=5).stdout
    except Exception:                                    # noqa: BLE001
        return False
    for line in out.splitlines():
        if f' on {vol} (' in line:
            fs = line.split(' (', 1)[1].split(',')[0].strip().lower()
            return fs in _NET_FS
    return False


def volume_for(path, budget=8.0):
    """(имя, id, mount) тома, на котором будет сессия.

    Идентификатор тома у Pro Tools не вычисляется из UUID, поэтому для внешнего диска читаем его из готовой сессии .ptx
    на этом диске (блок 0x0f3c). В сессии записан том, где она СОЗДАНА: если её скопировали с другого диска, там чужой том,
    поэтому берём только сессии, у которых записанная точка монтирования совпадает с реальной. Результат кэшируется."""
    import time
    path = os.path.realpath(os.path.abspath(path))
    if not path.startswith('/Volumes/'):
        return BOOT_VOLUME
    vol = '/'.join(path.split('/')[:3])
    if vol in _VOL_MEM:
        return _VOL_MEM[vol]
    if _is_network_volume(vol):                          # сетевой том: id не ищем (ссылки на такие файлы пишутся с нулевым id)
        _VOL_MEM[vol] = None
        return None
    cache = _vol_cache_load()
    ent = cache.get(vol)
    if ent and ent.get('id'):
        v = (ent['name'], bytes.fromhex(ent['id']), ent['mount'])
        _VOL_MEM[vol] = v
        return v
    if ent and time.time() - ent.get('t', 0) < 3600:        # недавно искали и не нашли
        _VOL_MEM[vol] = None
        return None
    found, seen, t0 = None, 0, time.time()
    for dp, dirs, fns in os.walk(vol):
        dirs[:] = [d for d in dirs if d not in ('Session File Backups', 'Audio Files', 'Bounced Files')
                   and not d.startswith('.') and dp.count('/') - vol.count('/') < 5]
        for fn in fns:
            if fn.lower().endswith('.ptx') and '.bak.' not in fn and not fn.startswith('._'):
                v = _volume_from_session(os.path.join(dp, fn))
                seen += 1
                if v and v[2].rstrip('/') == vol:
                    found = v
                    break
        if found or seen >= 40 or time.time() - t0 > budget:
            break
    _VOL_MEM[vol] = found
    cache[vol] = ({'name': found[0], 'id': found[1].hex(), 'mount': found[2]} if found else {'t': time.time()})
    _vol_cache_save(cache)
    return found


def _volume_from_session(ptx_path):
    import struct
    from ptx_model import Block
    try:
        p = PTX(open(ptx_path, 'rb').read())
        for r in p.roots:
            if isinstance(r, Block) and r.ctype == 0x0f3d:
                for c in r.items:
                    if isinstance(c, Block) and c.ctype == 0x0f3c:
                        raw = b''.join(i for i in c.items if isinstance(i, (bytes, bytearray)))[2:]
                        n = struct.unpack_from('<I', raw, 0)[0]
                        name = raw[4:4 + n].decode('utf-8')
                        vid = raw[4 + n:8 + n]
                        m = struct.unpack_from('<I', raw, 8 + n)[0]
                        mount = raw[12 + n:12 + n + m].decode('utf-8')
                        return (name, vid, mount)
    except Exception:                                    # noqa: BLE001
        return None
    return None


class MissingWav:
    """Отсутствующий файл: описание для записи ссылки в сессию (параметры взяты из проекта: формат дорожки, частота сессии, 24 бита)."""

    def __init__(self, path, channels, frames):
        import time
        self.path, self.channels, self.bits, self.rate, self.frames = path, channels, 24, SAMPLE_RATE, frames
        self.link_dir = None
        self.data_offset = 44
        self.mtime = self.birth = time.time()
        self.is_missing = True


class FlacInfo:
    """FLAC-файл из Reaper: параметры читаются из STREAMINFO. В сессию такой файл попадает конвертированным
    в 24-бит WAV (Pro Tools .flac не использует), поэтому bits = 24."""
    is_flac = True

    def __init__(self, path):
        self.path = path
        self.link_dir = None
        st = os.stat(path)
        self.mtime = st.st_mtime
        self.birth = getattr(st, 'st_birthtime', st.st_mtime)
        with open(path, 'rb') as f:
            head = f.read(10)
            if head[:3] == b'ID3':                                  # ID3v2 перед fLaC
                size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
                f.seek(10 + size)
            else:
                f.seek(0)
            if f.read(4) != b'fLaC':
                raise ValueError('not a FLAC file')
            hdr = f.read(4)
            if hdr[0] & 0x7F != 0 or int.from_bytes(hdr[1:4], 'big') < 34:
                raise ValueError('FLAC STREAMINFO not found')
            si = f.read(34)
        bits64 = int.from_bytes(si[10:18], 'big')
        self.rate = bits64 >> 44
        self.channels = ((bits64 >> 41) & 7) + 1
        self.src_bits = ((bits64 >> 36) & 31) + 1
        self.frames = bits64 & ((1 << 36) - 1)
        self.bits = 24
        if self.rate != SAMPLE_RATE:                                 # при конвертации приводим к частоте сессии
            self.frames = round(self.frames * SAMPLE_RATE / self.rate)
            self.src_rate, self.rate = self.rate, SAMPLE_RATE
        if self.channels not in (1, 2, 6):
            raise ValueError(f'{os.path.basename(path)}: only mono, stereo or 6-channel files are supported (got {self.channels} ch)')


class RateInfo:
    """WAV с частотой, отличной от частоты сессии: в сессию попадает копия, пересчитанная в 24-бит WAV 48 кГц (Pro Tools при
    импорте делает то же — Convert on import). Параметры исходника читает WavInfo."""
    is_flac = True                                                  # как FLAC / MP3: файл не линкуется, а конвертируется в Audio Files

    def __init__(self, w):
        self.path = w.path
        self.link_dir = None
        self.mtime, self.birth = w.mtime, w.birth
        self.channels, self.bits = w.channels, 24
        self.src_rate = w.rate
        self.rate = SAMPLE_RATE
        self.frames = round(w.frames * SAMPLE_RATE / w.rate)


class Mp3Info:
    """MP3 из Reaper: Pro Tools при импорте превращает mp3 в WAV сессии, поэтому делаем так же — конвертируем в 24-бит WAV
    с частотой сессии (параметры исходника читает afinfo). frames — оценка по длительности, точное число — у готового WAV."""
    is_flac = True                                                  # как FLAC: файл не линкуется, а конвертируется в Audio Files

    def __init__(self, path):
        import re
        import subprocess
        self.path = path
        self.link_dir = None
        st = os.stat(path)
        self.mtime = st.st_mtime
        self.birth = getattr(st, 'st_birthtime', st.st_mtime)
        r = subprocess.run(['/usr/bin/afinfo', path], capture_output=True, text=True)
        m = re.search(r'(\d+) ch,\s+(\d+) Hz', r.stdout)
        d = re.search(r'estimated duration:\s+([\d.]+) sec', r.stdout)
        if r.returncode != 0 or not m or not d:
            raise ValueError('not a readable MP3 file')
        self.channels = int(m.group(1))
        self.src_rate = int(m.group(2))
        self.rate = SAMPLE_RATE                                     # при конвертации приводим к частоте сессии
        self.frames = round(float(d.group(1)) * SAMPLE_RATE)
        self.bits = 24
        if self.channels not in (1, 2):
            raise ValueError(f'{os.path.basename(path)}: only mono or stereo files are supported (got {self.channels} ch)')


def flac_to_wav(src, dst, rate=None):
    """FLAC / MP3 -> обычный PCM WAV 24 бит (afconvert из macOS, затем заголовок переписывается без EXTENSIBLE);
    rate — частота результата (None — как у исходника)."""
    import subprocess
    import tempfile
    import wave
    tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    tmp.close()
    try:
        r = subprocess.run(['/usr/bin/afconvert', '-f', 'WAVE', '-d', f'LEI24@{rate}' if rate else 'LEI24', src, tmp.name],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f'afconvert failed for {os.path.basename(src)}: {r.stderr.strip() or r.stdout.strip()}')
        with wave.open(tmp.name, 'rb') as w, wave.open(dst, 'wb') as o:
            o.setnchannels(w.getnchannels())
            o.setsampwidth(3)
            o.setframerate(w.getframerate())
            while True:
                chunk = w.readframes(1 << 16)
                if not chunk:
                    break
                o.writeframes(chunk)
    finally:
        os.unlink(tmp.name)


_INFO_CACHE = {}
_DISK_CACHE_FILE = os.path.expanduser('~/Library/Application Support/Reaper to Pro Tools/wavinfo.json')
_DISK_CACHE = None
_DISK_DIRTY = False


def _disk_cache():
    global _DISK_CACHE
    if _DISK_CACHE is None:
        try:
            _DISK_CACHE = json.load(open(_DISK_CACHE_FILE))
        except Exception:                                    # noqa: BLE001
            _DISK_CACHE = {}
        import atexit
        atexit.register(_disk_cache_save)
    return _DISK_CACHE


def _disk_cache_save():
    if not _DISK_DIRTY:
        return
    try:
        os.makedirs(os.path.dirname(_DISK_CACHE_FILE), exist_ok=True)
        tmp = _DISK_CACHE_FILE + '.tmp'
        json.dump(_DISK_CACHE, open(tmp, 'w'))
        os.replace(tmp, _DISK_CACHE_FILE)
    except Exception:                                        # noqa: BLE001
        pass


def make_info(path):
    """Заголовок аудиофайла. Кэшируется в памяти (проект анализируется несколько раз) и на диске по (размер, время изменения):
    сводка и конвертация — разные процессы, а заголовки сотен файлов с сетевого диска читаются долго."""
    global _DISK_DIRTY
    if path in _INFO_CACHE:
        return _INFO_CACHE[path]
    if path.lower().endswith('.flac'):
        _INFO_CACHE[path] = FlacInfo(path)
        return _INFO_CACHE[path]
    if path.lower().endswith('.mp3'):
        _INFO_CACHE[path] = Mp3Info(path)
        return _INFO_CACHE[path]
    st = os.stat(path)
    cache = _disk_cache()
    e = cache.get(path)
    if e and len(e) >= 7 and e[0] == st.st_size and e[1] == st.st_mtime:
        w = WavInfo.__new__(WavInfo)
        w.path, w.link_dir = path, None
        w.channels, w.bits, w.rate, w.frames, w.data_offset = e[2:7]
        w.mtime, w.birth = st.st_mtime, getattr(st, 'st_birthtime', st.st_mtime)
        if w.rate != SAMPLE_RATE:
            w = RateInfo(w)
        _INFO_CACHE[path] = w
        return w
    w = WavInfo(path)                                        # ValueError для неподдерживаемого формата не кэшируется
    cache[path] = [st.st_size, st.st_mtime, w.channels, w.bits, w.rate, w.frames, w.data_offset]
    _DISK_DIRTY = True
    if w.rate != SAMPLE_RATE:
        w = RateInfo(w)
    _INFO_CACHE[path] = w
    return w


def progress(label, n, total):
    print(f'{label}: {n}/{total}', flush=True)


_PROGRESS_SHOWN = False                         # прогресс анализа клипов показываем один раз (проект анализируется повторно из кэша)



def pick_session_rate(pr, resolver):
    """Частота сессии Pro Tools: частота проекта Reaper (SAMPLERATE), если Pro Tools-шаблон умеет такую (44.1 / 48 / 88.2 / 96 кГц);
    если в проекте 0 (как у звуковой карты) или частота необычная — преобладающая среди аудиофайлов проекта, иначе 48 кГц.
    Файлы другой частоты конвертируются в частоту сессии."""
    global SAMPLE_RATE
    import ptx_settings
    rate = pr.sample_rate if pr.sample_rate in ptx_settings.SESSION_RATES else None
    if rate is None and pr.sample_rate == 0:
        votes = collections.Counter()
        seen = {}
        for it in pr.items:
            if it.skip:
                continue
            path = resolver.resolve(it.file)
            if not path:
                continue
            if path not in seen:
                try:
                    info = make_info(path)
                    seen[path] = getattr(info, 'src_rate', info.rate)
                except Exception:                                    # noqa: BLE001
                    seen[path] = None
            if seen[path] in ptx_settings.SESSION_RATES:
                votes[seen[path]] += 1
        if votes:
            rate = votes.most_common(1)[0][0]
    rate = rate or 48000
    if rate != SAMPLE_RATE:
        SAMPLE_RATE = rate
        _INFO_CACHE.clear()
    return rate


def analyse(pr, resolver, split=True, show_progress=False):
    """Находит файлы и отбирает item'ы, которые можно перенести. Возвращает (usable, skipped)."""
    global _PROGRESS_SHOWN
    show_progress = show_progress and not _PROGRESS_SHOWN
    usable, skipped = [], {}
    cache = {}
    total = len(pr.items)
    for n, it in enumerate(pr.items):
        if show_progress and n % 25 == 0:
            progress('Checking clips', n, total)
        reason = it.skip
        info = None
        if not reason:
            path = resolver.resolve(it.file)
            if not path:
                reason = 'file not found'
            else:
                if path not in cache:
                    try:
                        cache[path] = make_info(path)
                    except Exception as e:                       # noqa: BLE001
                        cache[path] = str(e)
                info = cache[path]
                if isinstance(info, str):
                    reason = 'unsupported audio'
                    info = None
                elif info.rate != SAMPLE_RATE:
                    reason = f'sample rate {info.rate} != {SAMPLE_RATE}'
        if reason:
            skipped.setdefault(reason, []).append(it)
        else:
            usable.append((it, info))
    if show_progress:
        progress('Checking clips', total, total)
        _PROGRESS_SHOWN = True
    if not split:
        return usable, skipped
    # формат дорожки Pro Tools один, а эффекты item'ов (TAKEFX) в Pro Tools — только инсерты дорожки. Поэтому у дорожки остаётся
    # преобладающая группа клипов (число каналов + набор эффектов item'а), клипы остальных групп уходят на дополнительные
    # дорожки ниже («имя (stereo)», «имя (FX)»). Эффекты item'а переносятся на инсерты (перед эффектами дорожки, как в Reaper);
    # у дополнительной дорожки те же фейдер, mute, solo, цвет, эффекты дорожки, огибающие и посылы.
    def group_key(it, info):
        return (info.channels, tuple(it.fx))

    per_track = {}
    for it, info in usable:
        per_track.setdefault(it.track, collections.Counter())[group_key(it, info)] += 1
    moved = 0
    import copy
    for t in sorted((t for t, c in per_track.items() if len(c) > 1 or next(iter(c))[1]), reverse=True):   # с конца — индексы выше не сдвигаются
        base = per_track[t].most_common(1)[0][0]
        src = pr.tracks[t]
        orig_fx = list(src.fx)
        extra = {}
        for it, info in usable:
            if it.track == t and group_key(it, info) != base:
                extra.setdefault(group_key(it, info), []).append(it)
        for k, key in enumerate(extra, 1):
            ch, fxs = key
            parts = []
            if ch != base[0]:
                parts.append({1: 'mono', 2: 'stereo', 6: '5.1'}.get(ch, f'{ch} ch'))
            if fxs != base[1]:
                parts.append('FX')
            new_tr = rpp.Track(name=f'{src.name} ({", ".join(parts)})', mute=src.mute, vol=src.vol, pan=src.pan, solo=src.solo,
                               color=src.color, depth=src.depth, parent=src.parent, note=src.note, mainsend=src.mainsend,
                               active_lane=src.active_lane, lane_names=dict(src.lane_names), fx=list(fxs) + orig_fx,
                               vol_env=list(src.vol_env), pan_env=list(src.pan_env), mute_env=list(src.mute_env),
                               vol_env_type=src.vol_env_type, fx_clip=len(fxs))
            at = t + k                                              # сразу ниже исходной (и ниже предыдущих дополнительных)
            for it in pr.items:
                if it.track >= at:
                    it.track += 1
            for tr in pr.tracks:
                if tr.parent >= at:
                    tr.parent += 1
                tr.recvs = [(r[0] + 1 if r[0] >= at else r[0],) + tuple(r[1:]) for r in tr.recvs]
            pr.tracks.insert(at, new_tr)
            for tr in pr.tracks:                                    # посылы исходной дорожки (приходят на приёмники) дублируются
                for ri, r in enumerate(list(tr.recvs)):
                    if r[0] == t and tr is not new_tr:
                        tr.recvs.append((at,) + tuple(r[1:]))
                        if ri < len(tr.recv_envs):
                            tr.recv_envs.append(copy.deepcopy(tr.recv_envs[ri]))
            for it in extra[key]:
                it.track = at
                moved += 1
        if base[1]:                                                 # эффекты преобладающей группы — на инсерты самой дорожки
            src.fx = list(base[1]) + orig_fx
            src.fx_clip = len(base[1])
    pr.split_clips = getattr(pr, 'split_clips', 0) + moved
    return usable, skipped


MAX_TRACKS = 512                               # предел дорожек сессии Pro Tools (Studio); больше — проект делится на несколько сессий


def track_weights(pr, usable):
    """Сколько дорожек Pro Tools получится из каждой дорожки Reaper (после разнесения форматов): {индекс: вес}."""
    track_ids = {it.track for it, _ in usable}
    empty, aux, _skipped = plan_empty_tracks(pr, track_ids)
    w = {}
    for t, tr in enumerate(pr.tracks):
        if t in track_ids or t in empty or t in aux or tr.folder:
            w[t] = 1
    return w


def plan_parts(pr, weights, limit=MAX_TRACKS):
    """Делит дорожки Reaper на части не больше limit дорожек Pro Tools; папка остаётся вместе со своим содержимым.
    Возвращает список списков индексов или None, если всё помещается в одну сессию."""
    if sum(weights.values()) <= limit:
        return None
    n = len(pr.tracks)

    def descendants(t):
        out, q = [], t + 1
        while q < n and _inside(pr, q, t):
            out.append(q)
            q += 1
        return out

    units = []
    for t in range(n):
        if pr.tracks[t].parent >= 0:
            continue
        group = [t] + descendants(t)
        if sum(weights.get(q, 0) for q in group) <= limit:
            units.append(group)
        else:                                       # папка больше предела: дорожки (и вложенные папки) идут отдельными единицами
            units.append([t])
            for c in range(t + 1, n):
                if pr.tracks[c].parent == t:
                    units.append([c] + descendants(c))
    parts, cur, cw = [], [], 0
    for u in units:
        uw = sum(weights.get(q, 0) for q in u)
        if cur and cw + uw > limit:
            parts.append(cur)
            cur, cw = [], 0
        cur += u
        cw += uw
    if cur:
        parts.append(cur)
    return parts


def subset_project(pr, keep):
    """Оставляет в проекте только дорожки keep (индексы исходной нумерации) и их item'ы; индексы пересчитываются.
    Возвращает число пропущенных sends (источник или приёмник в другой части)."""
    order = sorted(keep)
    new_index = {old: i for i, old in enumerate(order)}
    dropped = 0
    new_tracks = []
    for old in order:
        tr = pr.tracks[old]
        tr.parent = new_index.get(tr.parent, -1)
        kept_r, kept_e = [], []
        for ri, r in enumerate(tr.recvs):
            if r[0] in new_index:
                kept_r.append((new_index[r[0]],) + tuple(r[1:]))
                if ri < len(tr.recv_envs):
                    kept_e.append(tr.recv_envs[ri])
            else:
                dropped += 1
        tr.recvs, tr.recv_envs = kept_r, kept_e
        new_tracks.append(tr)
    pr.tracks = new_tracks
    pr.items = [it for it in pr.items if it.track in new_index]
    for it in pr.items:
        it.track = new_index[it.track]
    return dropped


def plan_sessions(rpp_path, media_dirs=(), overrides=None, show_progress=False):
    """(части, всего дорожек Pro Tools): части — None, если проект помещается в одну сессию."""
    pr = rpp.load(rpp_path)
    resolver = rpp.FileResolver(os.path.dirname(os.path.abspath(rpp_path)), media_dirs, overrides)
    pick_session_rate(pr, resolver)
    usable, _skipped = analyse(pr, resolver, show_progress=show_progress)
    w = track_weights(pr, usable)
    return plan_parts(pr, w), sum(w.values())


def missing_detail(items):
    """Уникальные отсутствующие файлы: путь из RPP, имя без пути и сколько секунд файла нужно проекту."""
    out = {}
    for it in items:
        d = out.setdefault(it.file, dict(path=it.file, name=os.path.basename(it.file.replace('\\', '/')), seconds=0.0))
        d['seconds'] = max(d['seconds'], it.soffs + it.length * it.rate)
    return sorted(out.values(), key=lambda d: d['name'].lower())


def _norm(name):
    import re
    return re.sub(r'[^0-9a-zа-я]', '', os.path.splitext(name)[0].lower())


def find_candidates(rpp_path, folders, overrides=None):
    """Для каждого отсутствующего файла ищет в папках (рекурсивно) файлы с тем же именем.

    Возвращает {путь из RPP: [кандидаты]}; кандидат: path, frames, rate, bits, channels, seconds,
    exact (имя совпало без учёта регистра), format_ok (моно 24 бит 48 кГц), duration_ok (хватает длины)."""
    pr = rpp.load(rpp_path)
    resolver = rpp.FileResolver(os.path.dirname(os.path.abspath(rpp_path)), (), overrides)
    pick_session_rate(pr, resolver)
    _, skipped = analyse(pr, resolver)
    missing = missing_detail(skipped.get('file not found', []))
    by_exact, by_norm = {}, {}
    for folder in folders:
        for dp, _, fns in os.walk(folder):
            for fn in fns:
                if fn.startswith('.') or not fn.lower().endswith(('.wav', '.wave', '.bwf', '.flac', '.mp3')):
                    continue
                full = os.path.join(dp, fn)
                by_exact.setdefault(fn.lower(), []).append(full)
                by_norm.setdefault(_norm(fn), []).append(full)
    out = {}
    for m in missing:
        paths, exact = [], set()
        for p in by_exact.get(m['name'].lower(), []):
            paths.append(p)
            exact.add(p)
        for p in by_norm.get(_norm(m['name']), []):
            if p not in exact and p not in paths:
                paths.append(p)
        cands = []
        for p in paths:
            c = dict(path=p, exact=p in exact, frames=0, rate=0, bits=0, channels=0, seconds=0.0,
                     format_ok=False, duration_ok=False)
            try:
                import wave
                if p.lower().endswith(('.flac', '.mp3')):
                    fi = FlacInfo(p) if p.lower().endswith('.flac') else Mp3Info(p)
                    c.update(frames=fi.frames, rate=fi.rate, bits=fi.bits, channels=fi.channels)
                else:
                    with wave.open(p, 'rb') as w:
                        c.update(frames=w.getnframes(), rate=w.getframerate(), bits=w.getsampwidth() * 8,
                                 channels=w.getnchannels())
                c['seconds'] = c['frames'] / c['rate'] if c['rate'] else 0.0
                c['format_ok'] = c['channels'] == 1 and c['bits'] == 24 and c['rate'] == SAMPLE_RATE
                c['duration_ok'] = c['seconds'] + 0.002 >= m['seconds']
            except Exception:                                      # noqa: BLE001
                pass
            cands.append(c)
        out[m['path']] = cands
    return dict(missing=missing, candidates=out)


def unique_track_names(names):
    seen, out = {}, []
    for n in names:
        k = seen.get(n, 0)
        seen[n] = k + 1
        out.append(n if k == 0 else f'{n} {k + 1}')
    return out


def folder_parent(pr, t, layout, have):
    """Ближайшая папка PT (включённая в layout) среди предков дорожки t."""
    q = pr.tracks[t].parent
    while q >= 0:
        if q in layout and pr.tracks[q].folder and q not in have:
            return q
        q = pr.tracks[q].parent
    return None


_CH = {'mono': 1, 'stereo': 2, '5.1': 6}


def plan_empty_tracks(pr, track_ids):
    """Дорожки Reaper без клипов -> ({индекс: число каналов} пустые аудио, {индексы} aux, число пропущенных).
    Метка из заметок проекта («Pro Tools track: audio mono» и т.п.) возвращает исходный тип; VCA / instrument / video не создаются.
    Без метки: дорожка, на которую приходят sends — aux, любая другая пустая дорожка — пустая моно-аудиодорожка (с её фейдером, цветом, mute)."""
    empty, aux, skipped = {}, set(), 0
    for t, tr in enumerate(pr.tracks):
        if t in track_ids or tr.folder:
            continue
        tag = tr.pt_tag
        if tag and tag[0] in ('vca', 'instrument', 'video'):
            skipped += 1
        elif tag and tag[0] == 'audio':
            empty[t] = _CH.get(tag[1], 1)
        elif tag and tag[0] == 'aux':
            aux.add(t)
        elif tr.recvs:                                          # на дорожку приходят sends — это приёмник (aux)
            aux.add(t)
        else:                                                   # остальные пустые дорожки — аудио (фейдер, цвет, mute, FX переносятся как у обычных)
            empty[t] = 1
    return empty, aux, skipped


_KIND_SUFFIX = __import__('re').compile(r' \((?:aux|VCA|instrument|track)\)$')


def pt_track_name(tr):
    """Имя дорожки Reaper без суффикса типа, который добавляет конвертер Pro Tools -> Reaper (если есть метка типа)."""
    return _KIND_SUFFIX.sub('', tr.name) if tr.pt_tag else tr.name


def plan_sends(pr, track_ids, tch, folder_ids=frozenset()):
    """Sends Reaper -> sends PT. Приёмник — любая дорожка Reaper с моно/стерео каналом: без клипов она станет aux с входом-шиной,
    с клипами — аудиодорожка, вход которой переключён на шину. Источник — моно/стерео аудиодорожка с клипами; стерео -> моно не
    переносится. Возвращает ({индекс приёмника: [(источник, громкость, пан, mute)]}, пропущено)."""
    plan, skipped = {}, 0
    pairs = {(src, dst) for dst, tr in enumerate(pr.tracks) for src, *_ in tr.recvs}
    for dst, tr in enumerate(pr.tracks):
        dch = tch.get(dst, 2) if dst in track_ids else 2
        for ri, (src, mode, vol, pan, mute) in enumerate(tr.recvs):
            sch = tch.get(src, 2 if src in folder_ids else 1)           # источник — аудиодорожка с клипами или папка (Routing Folder, стерео)
            if (tr.folder or src == dst or (src not in track_ids and src not in folder_ids) or sch > 2 or dch > 2
                    or (sch == 2 and dch == 1) or (dst, src) in pairs):
                skipped += 1
                continue
            # в Pro Tools mute дорожки не отключает pre-fader sends (в Reaper — отключает): посылы замьюченной дорожки глушатся сами
            plan.setdefault(dst, []).append((src, vol, pan, mute or pr.tracks[src].mute, mode in (1, 3),
                                             tr.recv_envs[ri] if ri < len(tr.recv_envs) else {}))
    return plan, skipped


def plan_layout(pr, track_ids, folders_on=True, aux_ids=()):
    """Итоговый порядок дорожек PT: аудио с клипами и папки с содержимым (в порядке Reaper). Возвращает (layout, 0)."""
    have = set(track_ids)

    def has_content(f):
        return any(t in have and _inside(pr, t, f) for t in range(len(pr.tracks)))

    layout = []
    for i, t in enumerate(pr.tracks):
        if i in have or i in aux_ids or (folders_on and t.folder):
            layout.append(i)
    return layout, 0


def _inside(pr, t, folder):
    q = pr.tracks[t].parent
    while q >= 0:
        if q == folder:
            return True
        q = pr.tracks[q].parent
    return False


FX_CLIP_COLOR = 0xBD0088                        # цвет клипов, у которых были эффекты (item/take FX) — малиновый из палитры Pro Tools (отличается от цветов дорожек Reaper)
MAX_POOL = 64                                  # наибольший шаблон templates/pool_N.ptx; дорожек больше — вставляются в pool_64


def pick_template(templates_dir, n_tracks):
    cand = os.path.join(templates_dir, f'pool_{n_tracks}.ptx')
    if not os.path.exists(cand):
        raise FileNotFoundError(f'template for {n_tracks} tracks not found: {cand}')
    return cand


def _env_points(env, offset, conv, step=False):
    """Огибающая Reaper [(время, значение, форма)] -> [(сэмпл, conv(значение))]. Точки формы «ступень» (1), а также все точки
    при step=True (mute) превращаются в пару (t, v), (t_next - 1, v) — так записывает сам Pro Tools."""
    env = sorted(env)
    pts = {}
    for k, (t, v, shape) in enumerate(env):
        smp = max(0, round((t + offset) * SAMPLE_RATE))
        pts[smp] = conv(v)
        if (shape == 1 or step) and k + 1 < len(env):
            nxt = max(0, round((env[k + 1][0] + offset) * SAMPLE_RATE)) - 1
            if nxt > smp:
                pts[nxt] = conv(v)
    return sorted(pts.items())


def volume_auto_points(tr, offset=0.0):
    """Огибающая громкости дорожки Reaper -> [(сэмпл, дБ | None)] для PT (значение = огибающая × фейдер дорожки).
    VOLTYPE 1 (fader scaling) — только отображение: значения точек в файле остаются амплитудой (проверено по lab/volenv.rpp)."""
    if not tr.vol_env:
        return []
    return _env_points(tr.vol_env, offset, lambda v: None if v * tr.vol <= 1e-7 else 20 * math.log10(v * tr.vol))


def send_volume_points(env, base, offset=0.0):
    if not env:
        return []
    return _env_points(env['pts'], offset, lambda v: None if v * base <= 1e-7 else 20 * math.log10(v * base))


def mute_auto_points(env_pts, offset=0.0):
    """Огибающая mute Reaper (1 — звучит, 0 — mute) -> [(сэмпл, 1 | 0)] PT (1 — замьючено)."""
    return _env_points(env_pts, offset, lambda v: 0 if v >= 0.5 else 1, step=True) if env_pts else []


def stereo_pan_points(env_pts, offset=0.0):
    """Баланс Reaper -1..1 -> два списка PT: L = -100 + 100·max(p, 0), R = 100 + 100·min(p, 0); точки перехода через 0 добавляются."""
    env = sorted(env_pts)
    ext = []
    for k, (t, v, shape) in enumerate(env):
        ext.append((t, v, shape))
        if k + 1 < len(env) and shape != 1 and v * env[k + 1][1] < 0:
            t2, v2, _ = env[k + 1]
            ext.append((t + (t2 - t) * v / (v - v2), 0.0, shape))
    left = _env_points(ext, offset, lambda v: round(-100 + 100 * max(v, 0)))
    right = _env_points(ext, offset, lambda v: round(100 + 100 * min(v, 0)))
    return left, right


def pan_auto_points(env_pts, offset=0.0):
    return _env_points(env_pts, offset, lambda v: max(-100, min(100, round(v * 100)))) if env_pts else []


def param_env_apply(p, track, slot, env, offset, stats, vst_file=None):
    """Огибающая параметра плагина Reaper (PARMENV) -> кривая автоматизации параметра в Pro Tools (по имени параметра)."""
    import ptx_plugins
    if env.get('special') == 'bypass' or env['name'].lower() == 'bypass':
        stats['skipped'].append((env['name'], 'bypass envelopes are not supported yet'))
        return
    pts = []
    src = env['pts']
    for i, (t, v, shape) in enumerate(src):
        s = round((t + offset) * SAMPLE_RATE)
        if s < 0:
            continue
        if pts and shape_prev == 1 and s - 1 > pts[-1][0]:      # квадратная форма: значение держится до следующей точки
            pts.append((s - 1, pts[-1][1]))
        pts.append((s, max(0.0, min(1.0, v))))
        shape_prev = shape
    if not pts:
        return
    import fx_state
    vid = fx_state.vst3_param_id(vst_file, env['index']) if vst_file and env.get('index') is not None else None   # точнее имени: по ID параметра
    why = ptx_plugins.set_param_curve(p, track, slot, env['name'], pts, vid)
    if why:
        stats['skipped'].append((env['name'], why))
    else:
        stats['done'] += 1


def convert(rpp_path, out_dir, name=None, templates_dir=None, media_dirs=(), offset=0.0,
            fades=True, info_only=False, volume=None, link=False, gain=True, placeholders=True, overrides=None,
            markers=True, states=True, folders_on=True, lanes_on=True, fader=True, automation=True, sends=True, plugins=True, unknown_disk='zero', keep_tracks=None, master=True, routing=True, main_output='Analog 1-2', video=True, plugin_settings=True, item_groups=True):
    pr = rpp.load(rpp_path)
    start_frames = None                                         # старт таймкода сессии (PROJOFFS); дробная часть кадра уходит в сдвиг клипов
    if pr.proj_offset > 0:
        import ptx_settings
        fps_ = pr.fps or 30.0
        codes = ptx_settings.fps_codes(fps_)
        if codes is not None:
            start_frames = int(pr.proj_offset * fps_ + 1e-6)
            offset += pr.proj_offset - start_frames / fps_
    resolver = rpp.FileResolver(os.path.dirname(os.path.abspath(rpp_path)), media_dirs, overrides)
    session_rate = pick_session_rate(pr, resolver)
    usable, skipped = analyse(pr, resolver, show_progress=not info_only)
    if keep_tracks is not None:                                # часть проекта, не помещающегося в одну сессию Pro Tools
        dropped_sends = subset_project(pr, keep_tracks)
        kept_ids = {id(it) for it in pr.items}
        usable = [(it, w) for it, w in usable if id(it) in kept_ids]
        skipped = {k: [it for it in v if id(it) in kept_ids] for k, v in skipped.items()}
        skipped = {k: v for k, v in skipped.items() if v}
        if dropped_sends:
            print(f'Warning: {dropped_sends} sends between tracks that are in different sessions were skipped.', flush=True)
    if not lanes_on:                                           # только играющий lane, без альтернативных плейлистов
        keep = []
        for it, w in usable:
            if it.lanes > 1 and it.lane != pr.tracks[it.track].active_lane:
                skipped.setdefault('alternate lane (not selected for transfer)', []).append(it)
            else:
                keep.append((it, w))
        usable = keep

    # дорожки, на которых есть что переносить, в порядке Reaper
    # отсутствующие файлы: регионы остаются в сессии (Pro Tools при открытии покажет окно поиска файлов)
    missing_items = list(skipped.get('file not found', [])) if placeholders else []        # клипы отсутствующих файлов остаются оффлайн-регионами
    track_ids = sorted({it.track for it, _ in usable} | {it.track for it in missing_items})
    result = dict(tracks=len(track_ids), clips=len(usable) + len(missing_items), placeholders=len(missing_items),
                  skipped={k: len(v) for k, v in skipped.items()},
                  missing=sorted({it.file for it in skipped.get('file not found', [])}))
    if info_only:
        video = [it for it in pr.items if it.skip == 'video item']
        audio = [it for it in pr.items if it.skip != 'video item']
        files = {it.file for it in audio if it.file}
        result.update(
            start_tc=(__import__('ptx_settings').format_start(round(pr.proj_offset * pr.fps), pr.fps) if pr.proj_offset > 0 and pr.fps else None),
            pan_law_db=(round(20 * math.log10(pr.pan_law), 1) if pr.pan_law and pr.pan_law > 0 else None),
            reaper_version=pr.version, sample_rate=pr.sample_rate, fps=pr.fps, bpm=pr.bpm,
            time_signature=f'{pr.ts_num}/{pr.ts_den}', tracks=len(pr.tracks), clips=len(audio),
            fades=sum((it.fade_in > 0) + (it.fade_out > 0) for it in audio),
            files=len(files), markers=len(pr.markers), importable_clips=len(usable) + len(missing_items),
            importable_tracks=len(track_ids) + len(plan_empty_tracks(pr, set(track_ids))[0]) + len(plan_empty_tracks(pr, set(track_ids))[1]),
            folders=sum(1 for t in plan_layout(pr, track_ids)[0] if t not in track_ids),
            nested_folders=plan_layout(pr, track_ids)[1],
            alt_playlists=sum(1 for it in pr.items if it.lanes > 1 and it.lane != pr.tracks[it.track].active_lane
                              for _ in [0]) and len({(it.track, it.lane) for it in pr.items if it.lanes > 1 and it.lane != pr.tracks[it.track].active_lane}),
            split_clips=getattr(pr, 'split_clips', 0),
            pt_tracks=sum(track_weights(pr, usable).values()),
            sessions=len(plan_parts(pr, track_weights(pr, usable)) or [0]),
            missing_files=result['missing'],
            missing_detail=missing_detail(skipped.get('file not found', [])),
            video=dict(tracks=len({it.track for it in video}), clips=len(video),
                       files=len({it.file for it in video if it.file}),
                       missing=len({it.file for it in video if it.file and resolver.resolve(it.file) is None})))
        return result
    if not pr.tracks:
        raise ValueError('nothing to import')

    name = name or os.path.splitext(os.path.basename(rpp_path))[0]
    templates_dir = templates_dir or os.path.join(HERE, 'templates')
    tch = {}                                                               # число каналов аудиодорожки Reaper
    for it, w in usable:
        tch[it.track] = w.channels
    for it in missing_items:
        tch.setdefault(it.track, 1)
    empty_audio, aux_extra, skipped_kinds = plan_empty_tracks(pr, set(track_ids))   # дорожки без клипов
    track_ids = sorted(set(track_ids) | set(empty_audio))
    for t, c in empty_audio.items():
        tch[t] = c
    if not track_ids:                                                      # в шаблоне Pro Tools должна быть хоть одна аудиодорожка
        filler = next((t for t in range(len(pr.tracks)) if not pr.tracks[t].folder and t not in aux_extra), None)
        if filler is None:
            filler = 0
        track_ids, tch[filler] = [filler], 1
    n_mono = sum(1 for t in track_ids if tch.get(t, 1) == 1)
    first_multi = next((t for t in track_ids if tch.get(t, 1) > 1), None)
    if n_mono:
        template = pick_template(templates_dir, min(n_mono, MAX_POOL))     # шаблон по числу моно-дорожек (больше MAX_POOL — остальные вставляются)
        base_multi = None
    else:                                                                  # только стерео/5.1: шаблон с одной такой дорожкой
        base_multi = first_multi
        template = os.path.join(templates_dir, 'pool_s1.ptx' if tch[first_multi] == 2 else 'pool_m1.ptx')

    sdir = os.path.join(os.path.abspath(out_dir), name)
    if os.path.exists(sdir):
        raise FileExistsError(sdir)

    # структура: папки Reaper с содержимым -> папки Pro Tools (Folder Bus -> Routing Folder, заметка «Basic Folder» -> Basic);
    # вложенные папки пока приводятся к внешней (дети переходят во внешнюю)
    folder_src = {t for t, tr_ in enumerate(pr.tracks) if tr_.folder and folders_on and 'basic' not in tr_.note.lower()}
    send_plan, sends_skipped = plan_sends(pr, track_ids, tch, folder_src) if sends else ({}, 0)
    aux_ids = ({d for d in send_plan if d not in track_ids} | aux_extra) - set(track_ids)      # приёмники sends и дорожки с содержимым без клипов -> aux
    layout, nested_flattened = plan_layout(pr, track_ids, folders_on, aux_ids)
    all_names = unique_track_names([pt_track_name(pr.tracks[t]) for t in layout])
    pt_names = {t: n for t, n in zip(layout, all_names)}
    # Takes & Lanes -> альтернативные плейлисты: играющий lane — основной плейлист (имя дорожки), остальные — альтернативы
    alt_names, taken = {}, {n.lower() for n in all_names}
    for it, _ in usable:
        tr_ = pr.tracks[it.track]
        if it.lanes > 1 and it.lane != tr_.active_lane and (it.track, it.lane) not in alt_names:
            nm = (tr_.lane_names.get(it.lane) or '').strip() or f'{pt_names[it.track]}.{it.lane + 1:02d}'
            base_nm, k = nm, 1
            while nm.lower() in taken:
                k += 1
                nm = f'{base_nm} {k}'
            taken.add(nm.lower())
            alt_names[(it.track, it.lane)] = nm
    for it in missing_items:
        tr_ = pr.tracks[it.track]
        if it.lanes > 1 and it.lane != tr_.active_lane and (it.track, it.lane) not in alt_names:
            nm = (tr_.lane_names.get(it.lane) or '').strip() or f'{pt_names[it.track]}.{it.lane + 1:02d}'
            base_nm, k = nm, 1
            while nm.lower() in taken:
                k += 1
                nm = f'{base_nm} {k}'
            taken.add(nm.lower())
            alt_names[(it.track, it.lane)] = nm

    def playlist_of(it):
        return alt_names.get((it.track, it.lane)) if it.lanes > 1 else None

    alts_by_track = {}
    for (t_, ln), nm in sorted(alt_names.items()):
        alts_by_track.setdefault(pt_names[t_], []).append(nm)
    audio_ids = [t for t in layout if t in track_ids and (tch.get(t, 1) == 1 or t == base_multi)]   # дорожки шаблона
    extra_mono = set(audio_ids[MAX_POOL:])                                    # моно-дорожки сверх шаблона (вставляются как стерео / 5.1)
    audio_ids = audio_ids[:MAX_POOL]
    names = [pt_names[t] for t in audio_ids]
    folders = [(i, t) for i, t in enumerate(layout) if t not in track_ids and t not in aux_ids]
    auxes = [(i, t) for i, t in enumerate(layout) if t in aux_ids]
    multi = [(i, t) for i, t in enumerate(layout) if t in track_ids and (tch.get(t, 1) > 1 or t in extra_mono) and t != base_multi]   # вставляются

    # шаблон: T01..TN -> имена Reaper
    p = PTX(open(template, 'rb').read())
    mapping = {f'T{i + 1:02d}': names[i] for i in range(len(names))}
    counts = rename_tracks(p, mapping)
    exp = {f'T{i + 1:02d}': EXPECTED_PLACES + (tch.get(t, 1) - 1 if t == base_multi else 0) for i, t in enumerate(audio_ids)}
    bad = {k: v for k, v in counts.items() if v != exp[k]}
    if bad:
        raise ValueError(f'unexpected template layout: {bad}')
    if folders or multi or auxes or base_multi is not None or alts_by_track:
        import ptx_structure
        inserts = sorted([(i, t, 'f') for i, t in folders] + [(i, t, 'm') for i, t in multi] + [(i, t, 'a') for i, t in auxes])
        for i, t, what in inserts:                             # в порядке возрастания позиции
            if what == 'a':
                ptx_structure.insert_track(p, 'aux', pt_names[t], i)
                continue
            if what == 'm':
                ptx_structure.insert_track(p, 'audio' if tch[t] == 1 else ('audio_st' if tch[t] == 2 else 'audio_51'), pt_names[t], i)
                continue
            note = pr.tracks[t].note.lower()
            depth = 0
            par = folder_parent(pr, t, layout, set(track_ids))
            while par is not None:
                depth += 1
                par = folder_parent(pr, par, layout, set(track_ids))
            ptx_structure.insert_track(p, 'folder' if 'basic' in note else 'routing', pt_names[t], i, depth=depth)
        if multi or base_multi is not None or alts_by_track:
            ptx_structure.reindex_playlists(p, alts_by_track)
        groups = {}
        for i, t in enumerate(layout):
            par = folder_parent(pr, t, layout, set(track_ids))
            if par is not None:
                groups.setdefault(layout.index(par), []).append(i)
        if folders:
            ptx_structure.set_membership(p, groups)
        ptx_structure.rebuild_strips(p)
        if states:                                           # свёрнутые папки Reaper (BUSCOMP 2) -> закрытые папки Pro Tools
            closed = [layout.index(t) for t in layout if pr.tracks[t].folder_closed and t not in track_ids and t not in aux_ids]
            if closed:
                ptx_structure.set_folders_closed(p, closed)
        if nested_flattened:
            print(f'Warning: {nested_flattened} nested folder(s) were merged into their outer folder.', flush=True)
    master_name = None
    import ptx_structure
    if routing or master:                                    # маршрутизация: выходы дорожек -> первый выход I/O; папки Routing Folder — шина папки
        import ptx_sends
        main_idx, main_name, main_id8 = ptx_sends.first_output(p)
        bus = ptx_sends.main_output_bus(p, main_output)      # шина первого выхода (в Pro Tools: «Analog 1-2»)
        if bus:
            main_idx, main_name, main_id8 = bus
        folder_bus = {}
        if routing:
            for f in layout:
                tr_f = pr.tracks[f]
                if tr_f.folder and f not in track_ids and f not in aux_ids and 'basic' not in tr_f.note.lower():
                    bname = f'{pt_names[f]} Bus'
                    bidx, bid, _bk = ptx_sends.add_bus(p, bname, 2)
                    ptx_sends.set_aux_input(p, layout.index(f), bidx, bname, bid)
                    folder_bus[f] = (bidx, bname, bid)
            def route_of(t):
                q = pr.tracks[t].parent
                while q >= 0:                                # ближайшая папка-шина среди предков
                    if q in folder_bus:
                        return folder_bus[q]
                    q = pr.tracks[q].parent
                return (main_idx, main_name, main_id8)
            for i_t, t in enumerate(layout):
                ptx_sends.set_main_output(p, i_t, *route_of(t))
        if master and plugins and pr.master_fx:               # MASTER Reaper -> Master Fader Pro Tools — только если на мастере есть плагины
            master_name = unique_track_names([pt_names[t] for t in layout] + ['Master'])[-1]
            n_before = len(layout)
            ptx_structure.insert_track(p, 'master', master_name, n_before)
            ptx_sends.set_main_output(p, n_before, main_idx, main_name, main_id8)
    n_sends = 0
    if send_plan:                                            # sends: шина на каждый приёмник, вход приёмника = шина, слоты sends
        import ptx_sends
        for dst in sorted(send_plan):
            di = layout.index(dst)
            bus_idx, bus_id, bus_kind = ptx_sends.add_bus(p, pt_names[dst], tch.get(dst, 2) if dst in track_ids else 2)
            ptx_sends.set_aux_input(p, di, bus_idx, pt_names[dst], bus_id)
            for src, rvol, rpan, rmute, rpre, renv in send_plan[dst]:
                ok = ptx_sends.add_send(p, layout.index(src), bus_idx, pt_names[dst], bus_id, bus_kind,
                                        None if rvol <= 1e-7 else 20 * math.log10(rvol), rmute, tch.get(src, 2 if src in folder_src else 1),
                                        pan=rpan * 100, pre=rpre)
                n_sends += 1 if ok else 0
                sends_skipped += 0 if ok else 1
                if ok and automation:                         # огибающие send (громкость / mute / пан)
                    si = layout.index(src)
                    pts = send_volume_points(renv.get('vol'), rvol, offset)
                    if pts:
                        ptx_sends.write_auto(p, si, pts, ok, 'vol')
                    if renv.get('mute'):
                        ptx_sends.write_auto(p, si, mute_auto_points(renv['mute']['pts'], offset), ok, 'mute')
                    if renv.get('pan') and tch.get(src, 2 if src in folder_src else 1) == 1 and bus_kind == 'stereo':
                        ptx_sends.write_auto(p, si, pan_auto_points(renv['pan']['pts'], offset), ok, 'pan')
    n_plugins = 0
    n_states = 0                                             # плагинов, у которых перенесены настройки (состояние VST3 -> блок AAX)
    param_stats = {'done': 0, 'skipped': []}                 # огибающие параметров плагинов: перенесено / пропущено (причина)
    plugins_missing = {}                                     # имя дорожки PT -> [(плагин, откуда, причина)] — не перенесённые плагины
    if plugins:                                              # FX Reaper -> инсерты PT (только плагины с эталоном в ptx_templates_plugins)
        import ptx_plugins
        fmt_name = {1: 'mono', 2: 'stereo', 6: '5.1'}
        for t in layout:
            tr = pr.tracks[t]
            if not tr.fx or (tr.folder and 'basic' in tr.note.lower()):
                continue
            fmt = fmt_name.get(tch.get(t, 2) if t in track_ids else 2)
            slot = 0
            for fi, (nm, byp, kind) in enumerate(tr.fx):
                found = ptx_plugins.find_template(nm, fmt, placeholder=not _is_builtin_fx(tr.fx[fi])) if fmt and kind in ('VST', 'AU') else None
                if found is None or slot > 9:                   # не перенесённый плагин — в комментарий дорожки
                    plugins_missing.setdefault(pt_names[t], []).append((nm, 'clip effect' if fi < tr.fx_clip else 'track effect',
                                                                        'no free insert slot' if found is not None else 'not in the plug-in library'))
                    continue
                if ptx_plugins.add_plugin(p, layout.index(t), slot, found[0], byp):
                    n_plugins += 1
                    fxo = tr.fx[fi]
                    if found[1].get('synth') == 'placeholder':      # плагина нет в Pro Tools: слот-заглушка (неактивный), настройки не переносятся
                        plugins_missing.setdefault(pt_names[t], []).append((nm, 'clip effect' if fi < tr.fx_clip else 'track effect',
                                                                            PLACEHOLDER_WHY))
                        fmt = fmt_name.get(ptx_plugins.template_out_channels(found[1]), fmt)
                        slot += 1
                        continue
                    if plugin_settings and getattr(fxo, 'state', None):
                        ok_ = ptx_plugins.apply_reaper_state(p, layout.index(t), slot, fxo.state, fxo.file, nm)
                        n_states += 1 if ok_ else 0
                        if os.environ.get('PX_DEBUG_FX'):
                            print(f'DEBUG_FX {pt_names[t]} / {nm}: state {"applied" if ok_ else "NOT applied"}', flush=True)
                    for env in getattr(tr.fx[fi], 'envs', ()):               # огибающие параметров плагина (PARMENV)
                        param_env_apply(p, layout.index(t), slot, env, offset, param_stats, getattr(fxo, 'file', None))
                    fmt = fmt_name.get(ptx_plugins.template_out_channels(found[1]), fmt)   # плагин моно->стерео: следующие — стерео
                slot += 1
    if plugins and master_name and pr.master_fx:               # эффекты мастера Reaper -> инсерты Master Fader
        import ptx_plugins
        slot = 0
        for mfi, (nm, byp, kind) in enumerate(pr.master_fx):
            found = ptx_plugins.find_template(nm, 'stereo', placeholder=not _is_builtin_fx(pr.master_fx[mfi])) if kind in ('VST', 'AU') else None
            if found is None or slot > 9:
                plugins_missing.setdefault(master_name, []).append((nm, 'master effect',
                                                                    'no free insert slot' if found is not None else 'not in the plug-in library'))
                continue
            if ptx_plugins.add_plugin(p, len(layout), slot, found[0], byp):
                n_plugins += 1
                mfx = pr.master_fx[mfi]
                if found[1].get('synth') == 'placeholder':
                    plugins_missing.setdefault(master_name, []).append((nm, 'master effect', PLACEHOLDER_WHY))
                    slot += 1
                    continue
                if plugin_settings and getattr(mfx, 'state', None) and ptx_plugins.apply_reaper_state(p, len(layout), slot, mfx.state, mfx.file, nm):
                    n_states += 1
                for env in getattr(pr.master_fx[mfi], 'envs', ()):
                    param_env_apply(p, len(layout), slot, env, offset, param_stats, getattr(mfx, 'file', None))
            slot += 1
    if pr.fps:                                               # частота таймкода проекта Reaper -> сессия Pro Tools
        import ptx_settings
        if not ptx_settings.set_timecode_rate(p, pr.fps):
            print(f'Warning: timecode rate {pr.fps} fps is not supported yet; the session keeps the template rate.', flush=True)
        elif start_frames is not None:
            ptx_settings.set_session_start(p, start_frames)
    import ptx_settings
    ptx_settings.set_session_rate(p, session_rate)           # частота сессии = частота проекта (44.1 / 48 / 88.2 / 96 кГц)
    # формат счётчиков: основная линейка Reaper -> основной счётчик, дополнительная -> sub
    ptx_settings.set_counters(p, ptx_settings.REAPER_TO_COUNTER.get(pr.time_main),
                              ptx_settings.REAPER_TO_COUNTER.get(pr.time_sub) if pr.time_sub >= 0 else None)
    if plugins:
        ptx_plugins.trim_param_lists(p)                       # в списках параметров — только автоматизированные (значения из списка PT ставит поверх настроек)
    if n_states:
        print(f'Plug-in settings transferred: {n_states}.', flush=True)
    if param_stats['done'] or param_stats['skipped']:
        print(f"Plugin parameter envelopes: {param_stats['done']} transferred, {len(param_stats['skipped'])} skipped.", flush=True)
        for nm_, why in param_stats['skipped'][:10]:
            print(f'Warning: parameter envelope {nm_!r} not transferred: {why}.', flush=True)
    if video:                                                 # видео Reaper -> видеодорожка Pro Tools (Studio: одна дорожка, один файл)
        vitems = [it for it in pr.items if it.skip == 'video item' and it.file]
        if vitems:
            import ptx_video
            first = vitems[0]
            vpath = resolver.resolve(first.file)
            vinfo = ptx_video.mov_info(vpath) if vpath else None
            if vinfo is None:
                print(f'Warning: video file {os.path.basename(first.file)} not found or unreadable; video not transferred.', flush=True)
            elif vinfo['frames'] > ptx_video.MAX_FRAMES:
                print(f"Warning: video {os.path.basename(vpath)} is longer than {ptx_video.MAX_FRAMES} frames; video not transferred.", flush=True)
            else:
                vfps = vinfo['fps']
                vinfo['frames'] = min(vinfo['frames'], max(1, round(first.length * vfps)))
                if first.soffs > 0.001:
                    print('Warning: the video item starts inside the file; Pro Tools plays the clip from the start of the file.', flush=True)
                try:
                    vv = volume_for(vpath)
                    ptx_video.add_video(p, vpath, vinfo, pos_frames=max(0, round((first.pos + offset) * vfps)), mute=first.mute,
                                        vol=(vv[0], vv[1]) if vv else None)
                except ValueError as e:                       # noqa: BLE001
                    print(f'Warning: video not transferred: {e}.', flush=True)
                extra_v = len(vitems) - 1
                if extra_v:
                    print(f'Warning: {extra_v} more video item(s) skipped: Pro Tools Studio takes one video track with one video clip here.', flush=True)
    tmp_template = p.save()

    os.makedirs(os.path.join(sdir, 'Audio Files'))
    if os.path.realpath(sdir).startswith('/Volumes/'):
        print('Checking disks: 0/1', flush=True)
    vol = volume or volume_for(sdir)
    if vol is None:
        print('Warning: no volume info for this disk; the session may ask to locate its audio files.', flush=True)
        vol = BOOT_VOLUME
    sb = SessionBuilder(tmp_template, sdir, vol=vol, crossfades=fades)
    sb.clip_groups = item_groups
    if item_groups:                                       # пустой item Reaper (без источника) с текстом в NOTES -> пустая Clip Group Pro Tools с этим именем
        for it_ in pr.items:
            if it_.skip == 'no source' and it_.notes.strip() and it_.length > 0 and it_.track in pt_names:
                sb.empty_groups.append({'track': pt_names[it_.track], 'start': max(0, round((it_.pos + offset) * SAMPLE_RATE)),
                                        'end': max(0, round((it_.pos + it_.length + offset) * SAMPLE_RATE)), 'name': it_.notes.strip().splitlines()[0]})
    if pr.bpm and abs(pr.bpm - 120.0) > 1e-6:
        sb.bpm = pr.bpm
    if pr.tempo_points > 1:
        print(f'Warning: the Reaper project changes tempo ({pr.tempo_points} tempo points); only the starting tempo {pr.bpm:g} BPM is written to the session.', flush=True)
    sb.ptx_path = os.path.join(sdir, name + '.ptx')
    for name_, lst in plugins_missing.items():            # плагины, которые не удалось перенести, — в комментарий дорожки
        sb.set_track_comment(name_, missing_plugins_comment(lst))
    if master_name:                                       # состояние Master Fader: громкость, панорама, mute, цвет
        mv = pr.master_vol
        vdb = None if abs(mv - 1.0) < 1e-6 or not fader else (-144.0 if mv <= 1e-7 else 20 * math.log10(mv))
        mpan = (-100 + 100 * max(pr.master_pan, 0), 100 + 100 * min(pr.master_pan, 0)) if fader and abs(pr.master_pan) > 1e-6 else None
        sb.set_track_state(master_name, mute=True if (states and pr.master_mute) else None,
                           color=pr.master_color if states and pr.master_color >= 0 else None, volume_db=vdb, pan=mpan)
    for t, n in pt_names.items():                         # mute / solo / цвет / громкость / панорама
        if t not in track_ids:
            if fader and (t in aux_ids or (pr.tracks[t].folder and 'basic' not in pr.tracks[t].note.lower())):   # Folder Bus -> Routing Folder, приёмник sends -> aux: фейдер
                trf = pr.tracks[t]
                if abs(trf.vol - 1.0) > 1e-6 or abs(trf.pan) > 1e-6:
                    sb.set_track_state(n, volume_db=-144.0 if trf.vol <= 1e-7 else 20 * math.log10(trf.vol),
                                       pan=(-100 + 100 * max(trf.pan, 0), 100 + 100 * min(trf.pan, 0)))
            if states and (pr.tracks[t].color >= 0 or pr.tracks[t].mute) and t not in track_ids:   # цвет и mute aux / папок
                st0 = sb.track_states.get(n, {})
                sb.set_track_state(n, color=pr.tracks[t].color if pr.tracks[t].color >= 0 else None, mute=True if pr.tracks[t].mute else None,
                                   volume_db=st0.get('volume_db'), pan=st0.get('pan'))
            continue
        tr = pr.tracks[t]
        vdb, pan = None, None
        if fader and (abs(tr.vol - 1.0) > 1e-6 or abs(tr.pan) > 1e-6):
            vdb = -144.0 if tr.vol <= 1e-7 else 20 * math.log10(tr.vol)
            ch = tch.get(t, 1)
            if ch == 1:
                pan = tr.pan * 100
            elif ch == 2:                                      # баланс Reaper -> пан каналов PT (приближение)
                pan = (-100 + 100 * max(tr.pan, 0), 100 + 100 * min(tr.pan, 0))
        if (states and (tr.mute or tr.solo or tr.color >= 0)) or vdb is not None:
            sb.set_track_state(n, mute=(tr.mute or None) if states else None, solo=(tr.solo or None) if states else None,
                               color=(tr.color if tr.color >= 0 else None) if states else None,
                               volume_db=vdb, pan=pan)

    if automation:                                         # огибающая громкости Reaper -> автоматизация громкости PT
        for t, n in pt_names.items():
            if t in track_ids or t in aux_ids or (pr.tracks[t].folder and 'basic' not in pr.tracks[t].note.lower()):
                pts = volume_auto_points(pr.tracks[t], offset)
                if pts:
                    sb.set_track_auto(n, pts)
                if pr.tracks[t].mute_env:
                    sb.set_track_auto(n, mute_auto_points(pr.tracks[t].mute_env, offset), 'mute')
                if pr.tracks[t].pan_env and t in track_ids and tch.get(t, 1) == 1:
                    sb.set_track_auto(n, pan_auto_points(pr.tracks[t].pan_env, offset), 'pan')
                elif pr.tracks[t].pan_env and t in track_ids and tch.get(t, 1) == 2:
                    lp, rp = stereo_pan_points(pr.tracks[t].pan_env, offset)
                    sb.set_track_auto(n, lp, 'pan')
                    sb.set_track_auto(n, rp, 'pan_r')

    # файлы -> Audio Files (уникальные имена); в режиме Link to source файлы одной папки остаются на месте
    # и записываются в сессию ссылкой на оригинал (как Add Audio в Pro Tools) — без копий и символьных ссылок
    link_dir = None
    if link:
        dirs = collections.Counter(os.path.dirname(os.path.abspath(w.path)) for _, w in usable
                                   if not getattr(w, 'is_flac', False))      # FLAC конвертируется в Audio Files сессии
        for d, _ in dirs.most_common():
            if os.path.realpath(d).startswith('/Volumes/'):
                print('Checking disks: 0/1', flush=True)
            v1 = volume_for(d)
            if v1 is None and unknown_disk and os.path.realpath(d).startswith('/Volumes/'):
                # диск без известного идентификатора (например сетевой): ссылка с нулевым id — Pro Tools открывает такую сессию
                # и находит файлы по пути (проверено на SMB-шаре; id тома сессии вместо нулевого вызывает окно поиска файлов)
                real = os.path.realpath(d).split('/')
                v1 = (real[2], bytes(4) if unknown_disk == 'zero' else vol[1], '/Volumes/' + real[2] + '/')
            if v1 and vol:                                              # диск файлов известен: ссылка пишется с его идентификатором
                link_dir = d
                if v1[1] != vol[1]:
                    sb.link_vol = v1                                     # файлы на другом диске, чем сессия
                break
        if link_dir is None:
            print('Warning: cannot determine the disk id of the source files; copying instead of linking.', flush=True)
    copied, used_names = {}, set()
    for it, w in usable:
        if w.path in copied:
            continue
        absd = os.path.dirname(os.path.abspath(w.path))
        if link_dir and absd == link_dir and not getattr(w, 'is_flac', False):
            copied[w.path] = os.path.abspath(w.path)                    # ссылка: файл остаётся на месте
            continue
        base = os.path.basename(w.path)
        stem, ext = os.path.splitext(base)
        if getattr(w, 'is_flac', False):
            ext = '.wav'                                            # .flac -> .wav рядом с остальными файлами сессии
            base = stem + ext
        k = 1
        while base.lower() in used_names:
            k += 1
            base = f'{stem}_{k}{ext}'
        used_names.add(base.lower())
        copied[w.path] = os.path.join(sdir, 'Audio Files', base)
    total = len(copied)
    to_convert = {w.path for _, w in usable if getattr(w, 'is_flac', False)}      # FLAC, MP3, WAV не в частоте сессии
    for i, (src, dst) in enumerate(copied.items(), 1):
        if src in to_convert:
            flac_to_wav(src, dst, SAMPLE_RATE)
            progress('Converting audio', i, total)
            continue
        if os.path.abspath(src) != dst:
            shutil.copy2(src, dst)
        progress('Linking audio' if link_dir and os.path.abspath(src) == dst else 'Copying audio', i, total)

    winfo = {}
    for src, dst in copied.items():
        winfo[src] = WavInfo(dst)
        if link_dir and dst == os.path.abspath(src) and os.path.dirname(dst) == link_dir:
            winfo[src].link_dir = link_dir
    for n, (it, w) in enumerate(usable, 1):
        wi = winfo[w.path]
        start = round((it.pos + offset) * SAMPLE_RATE)
        end = round((it.pos + it.length + offset) * SAMPLE_RATE)
        length = max(1, end - start)
        soffs = round(it.soffs * w.rate)
        stretch = None
        src_len = length
        if abs(it.rate - 1.0) > 1e-6:                           # растяжка без смены тона: Elastic Audio (Polyphonic); в источнике — length * rate
            src_len = max(1, round(length * it.rate))
            stretch = length
        if soffs + src_len > wi.frames:                         # зацикленный item: берём до конца файла
            src_len = max(1, wi.frames - soffs)
            length = max(1, round(src_len / it.rate)) if stretch else src_len
            if stretch:
                stretch = length
            else:
                length = src_len
        fin = min(MAX_FADE, round(it.fade_in * SAMPLE_RATE)) if fades else 0
        fout = min(MAX_FADE, round(it.fade_out * SAMPLE_RATE)) if fades else 0
        if fin + fout > length:                                  # фейды не должны пересекаться
            k = length / (fin + fout)
            fin, fout = int(fin * k), int(fout * k)
        gain_db = 0.0
        if gain and abs(it.vol - 1.0) > 1e-4:                    # громкость item'а -> клип-гейн (dB)
            gain_db = -300.0 if it.vol <= 0 else round(20 * math.log10(it.vol), 2)
        env = None
        if gain and len(it.env) >= 2:                            # огибающая громкости take -> клип-гейн с огибающей (абсолютные дБ; время от начала клипа, в сэмплах)
            env = []
            for t_, a_ in sorted(it.env):
                db_ = -144.0 if a_ <= 1e-7 else max(-144.0, min(36.0, 20 * math.log10(a_ * it.vol)))
                env.append((max(0, round(t_ * SAMPLE_RATE)), round(db_, 4)))
            gain_db = 0.0
        sb.add(Placement(pt_names[it.track], wi, it.name or 'clip', start, length=src_len,
                         fade_in=fin, fade_out=fout, mute=it.mute, offset=soffs, gain_db=gain_db,
                         playlist=playlist_of(it), color=FX_CLIP_COLOR if it.fx else None, stretch=stretch, gain_env=env, gid=it.group))
        if n % 20 == 0 or n == len(usable):
            progress('Placing clips', n, len(usable))

    if missing_items:
        # Файла нет — в сессию записывается ссылка на путь из проекта Reaper, клипы остаются регионами без файла (оффлайн): Pro Tools покажет окно поиска файлов.
        # Длину и параметры файла взять неоткуда: длина = сколько нужно клипам (смещение + длина), формат — по дорожке, частота сессии, 24 бита.
        proj_dir = os.path.dirname(os.path.abspath(rpp_path))
        norm = lambda f: (lambda g: g if os.path.isabs(g) else os.path.normpath(os.path.join(proj_dir, g)))(f.replace('\\', '/'))      # относительные пути Reaper — от папки проекта
        by_file = collections.OrderedDict()
        for it in missing_items:
            by_file.setdefault(it.file, []).append(it)
        if link_dir is None:                                               # общая папка для ссылок на отсутствующие файлы (в сессии ссылочные файлы — из одной папки)
            dirs = collections.Counter(os.path.dirname(norm(f)) for f in by_file)
            link_dir = dirs.most_common(1)[0][0]
            real = os.path.realpath(link_dir).split('/')
            if len(real) > 2 and real[1] == 'Volumes':
                sb.link_vol = (real[2], bytes(4), '/Volumes/' + real[2] + '/')      # том неизвестен (может быть не подключён): нулевой id, как у диска без идентификатора
        wmiss = {}
        for f, its in by_file.items():
            C = tch.get(its[0].track, 1)
            need = max(round(it.soffs * SAMPLE_RATE) + max(1, round((it.pos + it.length + offset) * SAMPLE_RATE) - round((it.pos + offset) * SAMPLE_RATE)) for it in its)
            mw = MissingWav(os.path.join(link_dir, os.path.basename(f.replace('\\', '/'))), C if C in (1, 2, 6) else 2, need)
            mw.link_dir = link_dir
            wmiss[f] = mw
        for it in missing_items:
            mw = wmiss[it.file]
            start0 = round((it.pos + offset) * SAMPLE_RATE)
            length = max(1, round((it.pos + it.length + offset) * SAMPLE_RATE) - start0)
            fin = min(MAX_FADE, round(it.fade_in * SAMPLE_RATE)) if fades else 0
            fout = min(MAX_FADE, round(it.fade_out * SAMPLE_RATE)) if fades else 0
            if fin + fout > length:
                k = length / (fin + fout)
                fin, fout = int(fin * k), int(fout * k)
            stem = os.path.splitext(os.path.basename(it.file.replace('\\', '/')))[0] or it.name
            sb.add(Placement(pt_names[it.track], mw, it.name or stem, start0, length=length, fade_in=fin, fade_out=fout, mute=it.mute,
                             offset=round(it.soffs * SAMPLE_RATE), gain_db=0.0, playlist=playlist_of(it)))
        progress('Missing files', len(missing_items), len(missing_items))
    for m in (pr.markers if markers else []):
        sb.add_marker(m.name or (f'Region {m.idx}' if m.region else f'Marker {m.idx}'), max(0, round((m.pos + offset) * SAMPLE_RATE)),
                      max(0, round((m.end + offset) * SAMPLE_RATE)) if m.region and m.end > m.pos else None,           # регион Reaper -> Selection
                      color=m.color if m.color >= 0 else None)
    progress('Saving session', 0, 1)
    data = sb.build()
    ptx_path = os.path.join(sdir, name + '.ptx')
    with open(ptx_path, 'wb') as f:
        f.write(data)
    progress('Saving session', 1, 1)
    result.update(session=ptx_path)
    return result


def _is_builtin_fx(fx):
    """Штатный эффект самого Reaper (ReaEQ и т. п.): для него заглушку в Pro Tools не создаём."""
    return '(cockos)' in (getattr(fx, 'full', '') or '').lower()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('rpp')
    ap.add_argument('--out')
    ap.add_argument('--name')
    ap.add_argument('--templates')
    ap.add_argument('--media-dir', action='append', default=[])
    ap.add_argument('--offset', type=float, default=0.0)
    ap.add_argument('--no-fades', action='store_true')
    ap.add_argument('--unknown-disk', choices=['zero', 'session', 'off'], default='zero', help='files on a disk with unknown id: link with a zero id (default), with the session volume id, or copy (off)')
    ap.add_argument('--link', action='store_true', help='symlinks to the original audio instead of copies')
    ap.add_argument('--no-gain', action='store_true')
    ap.add_argument('--no-markers', action='store_true')
    ap.add_argument('--no-plugins', action='store_true', help='не переносить плагины')
    ap.add_argument('--no-groups', action='store_true', help='группы items Reaper не превращать в Clip Groups')
    ap.add_argument('--no-sends', action='store_true', help='не переносить sends')
    ap.add_argument('--no-automation', action='store_true', help='не переносить автоматизацию громкости')
    ap.add_argument('--no-fader', action='store_true', help='не переносить громкость и панораму дорожек')
    ap.add_argument('--no-states', action='store_true', help='не переносить mute/solo/цвет дорожек')
    ap.add_argument('--no-folders', action='store_true')
    ap.add_argument('--no-plugin-settings', action='store_true', help='не переносить настройки плагинов (только сами плагины)')
    ap.add_argument('--no-video', action='store_true', help='не переносить видео Reaper в видеодорожку Pro Tools')
    ap.add_argument('--no-master', action='store_true', help='не создавать Master Fader (по умолчанию он создаётся только если на мастере Reaper есть плагины)')
    ap.add_argument('--main-output', default='Analog 1-2', help='имя шины первого выхода (выход по умолчанию в I/O)')
    ap.add_argument('--no-routing', action='store_true', help='не менять маршрутизацию (выходы дорожек остаются как в шаблоне)')
    ap.add_argument('--no-lanes', action='store_true', help='не переносить альтернативные lane (только играющий)')
    ap.add_argument('--relink', help='JSON {путь из RPP: фактический путь}')
    ap.add_argument('--candidates', action='store_true', help='найти кандидатов на отсутствующие файлы')
    ap.add_argument('--search', action='append', default=[], help='папка поиска кандидатов')
    ap.add_argument('--no-placeholders', action='store_true', help='skip clips whose audio file is missing')
    ap.add_argument('--info', action='store_true')
    a = ap.parse_args()
    overrides = json.load(open(a.relink, encoding='utf-8')) if a.relink else None
    if a.candidates:
        print(json.dumps(find_candidates(a.rpp, a.search, overrides), ensure_ascii=False))
        return
    if not a.info and not a.out:
        ap.error('--out is required')
    kw = dict(unknown_disk=None if a.unknown_disk == 'off' else a.unknown_disk, link=a.link, gain=not a.no_gain,
              placeholders=not a.no_placeholders, overrides=overrides, markers=not a.no_markers, states=not a.no_states,
              folders_on=not a.no_folders, lanes_on=not a.no_lanes, fader=not a.no_fader, automation=not a.no_automation,
              sends=not a.no_sends, plugins=not a.no_plugins, master=not a.no_master, routing=not a.no_routing, main_output=a.main_output, video=not a.no_video, plugin_settings=not a.no_plugin_settings, item_groups=not a.no_groups)
    if not a.info:
        print('Reading project: 0/1', flush=True)
    parts, total = (None, 0) if a.info else plan_sessions(a.rpp, a.media_dir, overrides, show_progress=True)
    if parts is None:
        r = convert(a.rpp, a.out, a.name, a.templates, a.media_dir, a.offset, not a.no_fades, a.info, **kw)
        if a.info:
            print(json.dumps(r, ensure_ascii=False))
        else:
            print(f"Done: {r['clips']} clips on {r['tracks']} tracks -> {r['session']}")
        return
    # проект больше предела дорожек сессии Pro Tools: несколько сессий в общей папке <имя>/
    name = a.name or os.path.splitext(os.path.basename(a.rpp))[0]
    container = os.path.join(os.path.abspath(a.out), name)
    if os.path.exists(container):
        raise FileExistsError(container)
    print(f'Warning: the project needs {total} Pro Tools tracks, more than a session can hold ({MAX_TRACKS}); '
          f'converting into {len(parts)} sessions in "{name}".', flush=True)
    os.makedirs(container)
    clips = tracks = 0
    first = None
    for k, keep in enumerate(parts, 1):
        r = convert(a.rpp, container, f'{name} part {k}', a.templates, a.media_dir, a.offset, not a.no_fades, False,
                    keep_tracks=keep, **kw)
        clips += r['clips']
        tracks += r['tracks']
        first = first or r['session']
        print(f'Part {k}/{len(parts)}: {r["tracks"]} tracks, {r["clips"]} clips -> {r["session"]}', flush=True)
    print(f'Done: {clips} clips on {tracks} tracks in {len(parts)} sessions -> {container}')

if __name__ == '__main__':
    main()
