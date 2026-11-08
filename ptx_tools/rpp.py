#!/usr/bin/env python3
"""rpp.py — разбор проекта Reaper (.rpp): дорожки, item'ы (позиция, обрезка, фейды, mute), маркеры.

Тот же разбор, что в reaper_import.cpp, на Python (для режима «без Pro Tools»).
"""
import os
import re
from dataclasses import dataclass, field


_TOK = re.compile(r'"([^"]*)"?|\'([^\']*)\'?|`([^`]*)`?|(\S+)')


def tokenize(s):
    """Токены строки Reaper: слова через пробел или значения в кавычках "…", '…', `…`."""
    if '"' not in s and "'" not in s and '`' not in s:
        return s.split()                                       # быстрый путь: кавычек нет
    return [m.group(m.lastindex) for m in _TOK.finditer(s)]


class Node:
    def __init__(self, name='', args=None):
        self.name, self.args = name, args or []
        self.entries = []              # ('line', key, tokens) | ('child', Node)

    def line(self, key):
        for e in self.entries:
            if e[0] == 'line' and e[1] == key:
                return e[2]
        return None

    @property
    def children(self):
        return [e[1] for e in self.entries if e[0] == 'child']


def parse_block(lines, pos, node):
    while pos < len(lines):
        text = lines[pos].strip()
        pos += 1
        if not text:
            continue
        if text[0] == '>':
            return pos
        if text[0] == '<':
            tok = tokenize(text[1:])
            child = Node(tok[0] if tok else '', tok[1:])
            pos = parse_block(lines, pos, child)
            node.entries.append(('child', child))
            continue
        tok = tokenize(text)
        if tok:
            node.entries.append(('line', tok[0], tok[1:]))
    return pos


def num(tok, i, default=0.0):
    try:
        return float(tok[i])
    except (IndexError, ValueError, TypeError):
        return default


@dataclass
class Item:
    track: int
    order: int
    name: str = ''
    file: str = ''
    skip: str = ''
    pos: float = 0.0
    length: float = 0.0
    soffs: float = 0.0
    rate: float = 1.0
    pitch: float = 0.0
    vol: float = 1.0
    mute: bool = False
    fade_in: float = 0.0
    fade_out: float = 0.0
    lane: int = 0              # номер lane (Takes & Lanes, fixed lanes)
    lanes: int = 1             # число lane дорожки
    fx: list = field(default_factory=list)      # эффекты item'а (TAKEFX): [(имя плагина, bypass, тип)]
    preserve: bool = True      # PLAYRATE: сохранять тон при изменении скорости (растяжка без смены высоты)
    env: list = field(default_factory=list)     # огибающая громкости take (VOLENV): [(время c от начала item, множитель громкости)]
    group: int = 0             # номер группы items (GROUP n); 0 — вне группы
    notes: str = ''            # текст NOTES item'а (у пустого item — имя группы)


@dataclass
class Track:
    name: str
    mute: bool = False
    vol: float = 1.0
    pan: float = 0.0           # -1 (лево) .. +1 (право)
    solo: bool = False
    color: int = -1            # 0xRRGGBB; -1 — цвет не задан
    folder: bool = False       # папка (ISBUS 1 1)
    depth: int = 0             # вложенность (число папок-предков)
    parent: int = -1           # индекс папки-родителя
    note: str = ''             # заметка дорожки (в т. ч. тип папки Pro Tools)
    mainsend: bool = True      # Master/parent send
    active_lane: int = 0       # играющий lane (LANESOLO: первый установленный бит маски, иначе 0)
    lane_names: dict = field(default_factory=dict)      # номер lane -> имя (LANENAME)
    vol_env: list = field(default_factory=list)         # огибающая громкости VOLENV2: [(время c, значение, форма)]
    fx: list = field(default_factory=list)              # цепочка FX: [(имя плагина без префикса/производителя, bypass)]
    pt_tag: tuple = None                               # из заметок проекта: ('audio'|'aux'|'vca'|'instrument'|'video', 'mono'|'stereo'|'5.1'|'')
    pan_env: list = field(default_factory=list)        # огибающая пана PANENV2: [(время c, -1..1, форма)]
    mute_env: list = field(default_factory=list)       # огибающая mute MUTEENV: [(время c, 1 — звучит | 0 — mute, форма)]
    vol_env_type: int = 0                              # VOLTYPE огибающей громкости (0 — амплитуда, 1 — fader scaling)
    recv_envs: list = field(default_factory=list)      # по одному словарю на AUXRECV: {'vol': {...}, 'pan': {...}, 'mute': {...}}
    recvs: list = field(default_factory=list)           # AUXRECV: [(индекс источника, режим, громкость, пан, mute)]
    rec_arm: bool = False                               # REC: дорожка в записи
    folder_closed: bool = False                         # BUSCOMP: папка свёрнута (2)
    fx_clip: int = 0                                    # сколько первых плагинов fx пришло с эффектов клипов (TAKEFX), а не с самой дорожки


@dataclass
class Marker:
    idx: int
    pos: float
    name: str
    region: bool = False
    end: float = -1.0
    color: int = -1                   # 0xRRGGBB или -1 (цвет не задан)


@dataclass
class Project:
    version: str = ''
    sample_rate: int = 0
    fps: float = 0.0
    bpm: float = 0.0
    tempo_points: int = 0              # число точек огибающей темпа (TEMPOENVEX); > 1 — темп меняется
    ts_num: int = 4
    ts_den: int = 4
    tracks: list = field(default_factory=list)
    items: list = field(default_factory=list)
    markers: list = field(default_factory=list)
    master_vol: float = 1.0           # MASTER_VOLUME: линейная громкость мастера
    master_pan: float = 0.0
    master_mute: bool = False
    master_color: int = -1            # MASTERPEAKCOL: 0xRRGGBB или -1
    master_fx: list = field(default_factory=list)       # <MASTERFXLIST: [(имя плагина, bypass, тип)]
    split_clips: int = 0
    proj_offset: float = 0.0          # PROJOFFS: старт таймкода проекта, секунды
    time_main: int = 1                # формат основной линейки (0 мин:с, 1 такты, 2 такты (компакт), 3 с, 4 сэмплы, 5 таймкод, 8 кадры)
    time_sub: int = -1                # формат дополнительной линейки (-1 — нет)
    pan_law: float = -1.0             # PANLAW (линейный множитель в центре), -1 — не задан


def _fade(tok):
    return max(0.0, num(tok, 1), num(tok, 2)) if tok else 0.0


def _track_color(tok):
    """PEAKCOL: native COLORREF (R | G<<8 | B<<16) | 0x1000000 -> 0xRRGGBB; без флага 0x1000000 цвета нет."""
    n = int(num(tok or [], 0))
    if not n & 0x1000000:
        return -1
    return ((n & 255) << 16) | (n & 0xFF00) | ((n >> 16) & 255)


class Fx(tuple):
    """(имя плагина, bypass, тип) + огибающие параметров плагина (PARMENV): envs = [{'name', 'index', 'pts': [(время c, значение 0..1, форма)]}].
    state — байты состояния VST3 в обёртке Reaper (как в блоке <VST ...>), file — файл .vst3, full — имя «VST3: Pro-Q 3 (FabFilter)»."""
    envs = ()
    state = None
    file = ''
    full = ''


def _vst_state(node):
    """Байты состояния из блока <VST "VST3: ..." файл ...> (строки base64 до закрывающей скобки) или None."""
    if node.name != 'VST' or not node.args or not node.args[0].startswith('VST3'):
        return None
    import base64
    parts = []
    for e in node.entries:
        if e[0] == 'line' and not e[2]:
            try:
                parts.append(base64.b64decode(e[1] + '=' * (-len(e[1]) % 4)))
            except ValueError:
                return None
    return b''.join(parts) or None


def _mk_fx(node, byp):
    nm = node.args[0] if node.args else ''
    full = nm
    nm = _re_fx_prefix.sub('', nm)
    nm = _re_fx_chans.sub('', nm)                     # «SonoBus (Sonosaurus) (18ch)», «S360 Imager Mono/5.1 (Waves) (1->6ch)»: сначала число каналов,
    nm = _re_fx_vendor.sub('', nm)                    # потом производитель — один раз (собственные скобки в имени плагина не трогаем)
    nm = _re_fx_variant.sub('', nm)                   # «FabFilter Pro-DS (Mono) (FabFilter)» -> без «(Mono)»
    fx = Fx((nm.strip(), bool(byp), node.name))
    fx.envs = []
    fx.state = _vst_state(node)
    fx.file = node.args[1] if len(node.args) > 1 else ''
    fx.full = full
    return fx


def _parmenv(node):
    """PARMENV <индекс[:имя]> <мин> <макс> <по умолчанию> "<параметр> / <плагин>" -> словарь или None (выключена / пуста)."""
    act = node.line('ACT') or []
    if act and num(act, 0, 1) == 0:
        return None
    a = node.args
    head = (a[0] if a else '').split(':')
    label = a[-1] if len(a) >= 5 else ''
    pts = [(num(e[2], 0), num(e[2], 1), int(num(e[2], 2))) for e in node.entries if e[0] == 'line' and e[1] == 'PT' and len(e[2]) >= 2]
    if not pts:
        return None
    try:
        idx = int(head[0])
    except ValueError:
        idx = -1
    name = label.split(' / ')[0].strip() if label else (head[1] if len(head) > 1 else '')
    return {'name': name, 'index': idx, 'special': head[1] if len(head) > 1 else '', 'pts': pts,
            'defshape': int(num(node.line('DEFSHAPE') or [], 0))}


def _fx_list(chain):
    """Плагины из блока FXCHAIN / TAKEFX: [(имя без префикса и производителя, bypass, тип)]."""
    out, byp = [], 0
    for fe in chain.entries:
        if fe[0] == 'line' and fe[1] == 'BYPASS':
            byp = int(num(fe[2], 0)) or int(num(fe[2], 1))        # bypass или offline
        elif fe[0] == 'child' and fe[1].name in ('VST', 'AU', 'CLAP', 'JS', 'DX'):
            out.append(_mk_fx(fe[1], byp))
        elif fe[0] == 'child' and fe[1].name == 'PARMENV' and out:
            env = _parmenv(fe[1])
            if env:
                out[-1].envs.append(env)
    return out


def parse_item(node, track_idx, order):
    it = Item(track=track_idx, order=order)
    it.pos = num(node.line('POSITION') or [], 0)
    it.length = num(node.line('LENGTH') or [], 0)
    it.fade_in = _fade(node.line('FADEIN'))
    it.fade_out = _fade(node.line('FADEOUT'))
    it.mute = num(node.line('MUTE') or [], 0) != 0
    it.group = int(num(node.line('GROUP') or [], 0))
    yp = node.line('YPOS')                                    # YPOS <y> <h> <число lane>
    if yp and num(yp, 1) > 0:
        it.lanes = max(1, int(round(num(yp, 2, 1))))
        it.lane = int(round(num(yp, 0) / num(yp, 1)))
    vp = node.line('VOLPAN') or []
    it.vol = num(vp, 0, 1.0) * num(vp, 2, 1.0)             # VOLPAN: громкость take, пан, громкость item (3-е число), panlaw — слышен их продукт

    takes = [dict(name='', soffs=0.0, rate=1.0, pitch=0.0, vol=1.0, source=None, sel=False, fx=[], preserve=True, env=[])]
    for e in node.entries:
        if e[0] == 'child':
            if e[1].name == 'SOURCE':
                takes[-1]['source'] = e[1]
            elif e[1].name == 'TAKEFX':
                takes[-1]['fx'] = _fx_list(e[1])
            elif e[1].name == 'NOTES':
                it.notes = '\n'.join(' '.join([x[1][1:]] + list(x[2])).strip() for x in e[1].entries if x[0] == 'line' and x[1].startswith('|'))
            elif e[1].name == 'VOLENV':
                act = e[1].line('ACT') or []
                if not act or num(act, 0, 1) != 0:
                    takes[-1]['env'] = [(num(x[2], 0), num(x[2], 1)) for x in e[1].entries if x[0] == 'line' and x[1] == 'PT' and len(x[2]) >= 2]
            continue
        _, key, tok = e
        t = takes[-1]
        if key == 'TAKE':
            takes.append(dict(name='', soffs=0.0, rate=1.0, pitch=0.0, vol=1.0, source=None, sel='SEL' in tok, fx=[], preserve=True, env=[]))
        elif key == 'NAME' and tok:
            t['name'] = tok[0]
        elif key == 'SOFFS':
            t['soffs'] = num(tok, 0)
        elif key == 'PLAYRATE':
            t['rate'], t['pitch'] = num(tok, 0, 1.0), num(tok, 2)
            t['preserve'] = num(tok, 1, 1) != 0
        elif key == 'VOLPAN':
            t['vol'] = num(tok, 0, 1.0)
    chosen = next((t for t in takes[1:] if t['sel']), takes[0])
    it.name, it.soffs, it.rate, it.pitch = chosen['name'], chosen['soffs'], chosen['rate'], chosen['pitch']
    it.preserve = chosen['preserve']
    it.fx = chosen['fx']
    it.env = chosen['env']
    if chosen is not takes[0]:                    # у первого take громкость — уже VOLPAN item'а
        it.vol *= chosen['vol']

    src = chosen['source']
    if src is None:
        it.skip = 'no source'
        return it
    if src.args and src.args[0] == 'SECTION':
        sp = src.line('STARTPOS')
        if sp:
            it.soffs += num(sp, 0)
        mode = src.line('MODE')
        if mode and num(mode, 0) != 0:
            it.skip = 'section with reverse/special mode'
        inner = next((c for c in src.children if c.name == 'SOURCE'), None)
        src = inner or src
    kind = src.args[0] if src.args else ''
    f = src.line('FILE')
    if f:
        it.file = f[0]
    if not it.skip:
        if kind == 'VIDEO':
            it.skip = 'video item'
        elif kind in ('MIDI', 'MIDIPOOL'):
            it.skip = 'MIDI item'
        elif not it.file:
            it.skip = f'no file ({kind})'
        elif abs(it.rate - 1.0) > 1e-6 and (not it.preserve or abs(it.pitch) > 1e-6):
            it.skip = 'playrate with pitch change'                  # растяжка без смены тона переносится (Elastic Audio), остальное — нет
    if not it.name and it.file:
        it.name = os.path.splitext(os.path.basename(it.file.replace('\\', '/')))[0]
    return it


def _parse_env(node):
    """Огибающая из узла VOLENV2 / PANENV2 / MUTEENV / AUX*ENV: {'pts': [(время, значение, форма)], 'type': VOLTYPE} | None (выключена)."""
    act = node.line('ACT') or []
    if act and num(act, 0, 1) == 0:
        return None
    vt = node.line('VOLTYPE')
    return {'pts': [(num(e[2], 0), num(e[2], 1), int(num(e[2], 2))) for e in node.entries
                    if e[0] == 'line' and e[1] == 'PT' and len(e[2]) >= 2], 'type': int(num(vt, 0)) if vt else 0}


import re as _re0
_re_fx_prefix = _re0.compile(r'^(?:VST3i?|VSTi?|AUi?|CLAPi?|JS|DXi?):\s*', _re0.I)
_re_fx_vendor = _re0.compile(r'\s*\([^()]*\)\s*$')
_re_fx_chans = _re0.compile(r'\s*\(\d+(?:->\d+)?ch\)\s*$')
_re_fx_variant = _re0.compile(r'\s*\((?:mono|stereo)\)\s*$', _re0.I)


_PARSE_CACHE = {}
_VIRTUAL = {}                       # путь -> текст проекта, которого нет на диске (его собрала другая программа в памяти)


def register_virtual(path, text):
    """Проект .rpp, существующий только в памяти: load(path) читает его отсюда, а не с диска (путь нужен лишь для поиска медиа)."""
    _VIRTUAL[os.path.abspath(path)] = text


def _parse_file(path):
    """Разбор файла в дерево Node; результат кэшируется (файл читается один раз за запуск, даже если load вызывают дважды)."""
    virt = _VIRTUAL.get(os.path.abspath(path))
    if virt is not None:
        lines = virt.splitlines()
        if not lines or not lines[0].startswith('<REAPER_PROJECT'):
            raise ValueError(f'{path} is not a Reaper project')
        head = tokenize(lines[0][1:])
        root = Node('REAPER_PROJECT')
        parse_block(lines, 1, root)
        return head, root
    try:
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key and key in _PARSE_CACHE:
        return _PARSE_CACHE[key]
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = f.read().splitlines()
    if not lines or not lines[0].startswith('<REAPER_PROJECT'):
        raise ValueError(f'{path} is not a Reaper project')
    head = tokenize(lines[0][1:])
    root = Node('REAPER_PROJECT')
    parse_block(lines, 1, root)
    if key:
        _PARSE_CACHE.clear()
        _PARSE_CACHE[key] = (head, root)
    return head, root


def load(path):
    head, root = _parse_file(path)

    pr = Project(version=head[2] if len(head) > 2 else '')
    pr.sample_rate = int(num(root.line('SAMPLERATE') or [], 0))
    pr.fps = num(root.line('SMPTESYNC') or [], 1)
    tm = root.line('TIMEMODE') or []                                          # TIMEMODE <основной формат> <дополнительный> ? <fps> ...
    if tm:
        pr.time_main, pr.time_sub = int(num(tm, 0, 1)), int(num(tm, 1, -1))
        if num(tm, 3) > 0:
            pr.fps = num(tm, 3)                                                # частота кадров проекта (линейка таймкода), а не fps синхронизации
    pr.proj_offset = num(root.line('PROJOFFS') or [], 0)                      # PROJOFFS: время начала проекта (старт таймкода), с
    for nominal, pulled in ((24.0, 23.976), (30.0, 29.97)):                  # Reaper хранит и 23.976, и 24 как «24» (30 и 29.97 как «30»): старт таймкода по целому числу кадров выдаёт настоящую частоту
        if pr.fps == nominal and pr.proj_offset > 0:
            e_nom = abs(pr.proj_offset * nominal - round(pr.proj_offset * nominal))
            e_pull = abs(pr.proj_offset * pulled - round(pr.proj_offset * pulled))
            if e_pull < 1e-4 and e_nom > 1e-3:
                pr.fps = pulled
    pr.pan_law = num(root.line('PANLAW') or [], 0, -1.0)                       # PANLAW: линейный множитель в центре (1 — 0 дБ, 0.7079 — −3 дБ), -1 — не задан
    pr.bpm = num(root.line('TEMPO') or [], 0)
    for c in root.entries:                                                     # огибающая темпа: <TEMPOENVEX ... PT время bpm форма ...>
        if c[0] == 'child' and c[1].name == 'TEMPOENVEX':
            pts = [x for x in c[1].entries if x[0] == 'line' and x[1] == 'PT' and len(x[2]) >= 2]
            pr.tempo_points = len(pts)
            if pts:
                pr.bpm = num(pts[0][2], 1) or pr.bpm
    tempo = root.line('TEMPO') or []
    pr.ts_num, pr.ts_den = int(num(tempo, 1, 4)), int(num(tempo, 2, 4))
    mv = root.line('MASTER_VOLUME')
    if mv:
        pr.master_vol, pr.master_pan = num(mv, 0, 1.0), num(mv, 1)
    pr.master_mute = num(root.line('MASTERMUTESOLO') or [], 0) != 0
    pr.master_color = _track_color(root.line('MASTERPEAKCOL'))
    for c in root.children:
        if c.name == 'MASTERFXLIST':
            pr.master_fx = _fx_list(c)
    order = 0
    for e in root.entries:
        if e[0] == 'line' and e[1] == 'MARKER' and len(e[2]) >= 4:
            tok = e[2]
            m = Marker(idx=int(num(tok, 0)), pos=num(tok, 1), name=tok[2], region=num(tok, 3) != 0, color=_track_color(tok[4:5]) if len(tok) > 4 else -1)
            if m.region and not m.name:                                     # вторая строка региона: тот же номер, пустое имя -> конец региона
                closed = False
                for r in pr.markers:
                    if r.region and r.end < 0 and r.idx == m.idx:
                        r.end, closed = m.pos, True
                        break
                if closed:
                    continue
            pr.markers.append(m)
        elif e[0] == 'child' and e[1].name == 'TRACK':
            tn = e[1]
            nm = (tn.line('NAME') or [''])[0] or f'Track {len(pr.tracks) + 1}'
            ms = tn.line('MUTESOLO')
            tr = Track(name=nm, mute=bool(ms and num(ms, 0) != 0), vol=num(tn.line('VOLPAN') or [], 0, 1.0), pan=num(tn.line('VOLPAN') or [], 1, 0.0),
                       solo=bool(ms and num(ms, 1) != 0), color=_track_color(tn.line('PEAKCOL')))
            ib = tn.line('ISBUS') or []
            tr.folder = bool(ib) and num(ib, 0) == 1 and num(ib, 1) > 0
            tr._isbus_delta = int(num(ib, 1)) if ib else 0
            tr.rec_arm = num(tn.line('REC') or [], 0) == 1                              # REC <armed> ...
            tr.folder_closed = tr.folder and num(tn.line('BUSCOMP') or [], 0) == 2       # BUSCOMP <0 открыта | 1 мелко | 2 свёрнута> ...
            ls = tn.line('LANESOLO') or []
            mask = int(num(ls, 0)) if ls else 0
            tr.active_lane = next((k for k in range(32) if mask >> k & 1), 0)
            for e in tn.entries:
                if e[0] == 'line' and e[1] == 'LANENAME' and len(e[2]) >= 2:
                    try:
                        tr.lane_names[int(float(e[2][0]))] = e[2][1]
                    except ValueError:
                        pass
            ms2 = tn.line('MAINSEND') or []
            tr.mainsend = not ms2 or num(ms2, 0) != 0
            for e in tn.entries:
                if e[0] == 'line' and e[1] == 'AUXRECV' and len(e[2]) >= 5:
                    tr.recvs.append((int(num(e[2], 0, -1)), int(num(e[2], 1)), num(e[2], 2, 1.0), num(e[2], 3), num(e[2], 4) != 0))
            cur_recv = -1
            for e in tn.entries:                              # огибающие sends идут сразу за своим AUXRECV
                if e[0] == 'line' and e[1] == 'AUXRECV' and len(e[2]) >= 5:
                    cur_recv += 1
                    tr.recv_envs.append({})
                elif e[0] == 'child' and e[1].name in ('AUXVOLENV', 'AUXPANENV', 'AUXMUTEENV') and cur_recv >= 0:
                    env = _parse_env(e[1])
                    if env is not None:
                        tr.recv_envs[cur_recv][{'AUXVOLENV': 'vol', 'AUXPANENV': 'pan', 'AUXMUTEENV': 'mute'}[e[1].name]] = env
            for c in tn.children:
                if c.name in ('PANENV2', 'MUTEENV'):
                    env = _parse_env(c)
                    if env is not None:
                        (tr.pan_env if c.name == 'PANENV2' else tr.mute_env).extend(env['pts'])
                if c.name == 'FXCHAIN':
                    byp = 0
                    for fe in c.entries:
                        if fe[0] == 'line' and fe[1] == 'BYPASS':
                            byp = int(num(fe[2], 0)) or int(num(fe[2], 1))        # bypass или offline
                        elif fe[0] == 'child' and fe[1].name in ('VST', 'AU', 'CLAP', 'JS', 'DX'):
                            tr.fx.append(_mk_fx(fe[1], byp))
                        elif fe[0] == 'child' and fe[1].name == 'PARMENV' and tr.fx:
                            env = _parmenv(fe[1])
                            if env:
                                tr.fx[-1].envs.append(env)
                if c.name == 'VOLENV2':
                    act = c.line('ACT') or []
                    if act and num(act, 0, 1) == 0:                  # огибающая отключена (bypass)
                        continue
                    tr.vol_env = [(num(e[2], 0), num(e[2], 1, 1.0), int(num(e[2], 2))) for e in c.entries
                                  if e[0] == 'line' and e[1] == 'PT' and len(e[2]) >= 2]
                    vt = c.line('VOLTYPE')
                    tr.vol_env_type = int(num(vt, 0)) if vt else 0
                if c.name == 'NOTES':                                   # устаревший формат: заметка на дорожке
                    tr.note = ' '.join(' '.join([e[1]] + list(e[2])) for e in c.entries if e[0] == 'line').replace('|', '').strip()
            idx = len(pr.tracks)
            pr.tracks.append(tr)
            for c in tn.children:
                if c.name == 'ITEM':
                    pr.items.append(parse_item(c, idx, order))
                    order += 1
    import re as _re                                         # заметки проекта: «Track N "имя": Pro Tools: Basic|Routing Folder»
    for e in root.entries:
        if e[0] == 'child' and e[1].name == 'NOTES':
            for ln in e[1].entries:
                if ln[0] != 'line':
                    continue
                txt = ' '.join([ln[1]] + list(ln[2])).lstrip('|')
                m = _re.match(r'Track (\d+) .*?: (Pro Tools: (?:Basic|Routing) Folder)\s*$', txt)
                if m and 0 < int(m.group(1)) <= len(pr.tracks):
                    pr.tracks[int(m.group(1)) - 1].note = m.group(2)
                m = _re.match(r'Track (\d+) .*?: Pro Tools track: (\w+)(?: (\S+))?\s*$', txt)
                if m and 0 < int(m.group(1)) <= len(pr.tracks):
                    pr.tracks[int(m.group(1)) - 1].pt_tag = (m.group(2).lower(), (m.group(3) or '').lower())
    for m in pr.markers:
        if m.region and m.end < 0:
            m.region = False
    # вложенность папок: ISBUS a d — d = изменение глубины после дорожки (+1 открывает папку, -k закрывает)
    stack, depth = [], 0
    for i, t in enumerate(pr.tracks):
        t.depth = len(stack)
        t.parent = stack[-1] if stack else -1
        d = t._isbus_delta
        if t.folder:
            stack.append(i)
        elif d < 0:
            for _ in range(min(-d, len(stack))):
                stack.pop()
    return pr


class FileResolver:
    """Файлы из RPP: относительные, абсолютные и пути с другой машины (D:\\...) — по имени."""

    def __init__(self, rpp_dir, media_dirs=(), overrides=None):
        self.rpp_dir, self.dirs = rpp_dir, [d for d in media_dirs if d]
        self.overrides = overrides or {}          # {путь как в RPP: фактический путь} — ручной Relink
        self._index = None

    def _build(self):
        self._index = {}
        for root in [self.rpp_dir] + self.dirs:
            for dp, _, fns in os.walk(root):
                for fn in fns:
                    self._index.setdefault(fn, os.path.join(dp, fn))

    def resolve(self, rpp_path):
        ov = self.overrides.get(rpp_path)
        if ov and os.path.exists(ov):
            return ov
        p = rpp_path.replace('\\', '/')
        if p.startswith('/') and os.path.exists(p):
            return p
        if not (len(p) > 2 and p[1] == ':') and not p.startswith('/'):
            cand = os.path.join(self.rpp_dir, p)
            if os.path.exists(cand):
                return cand
        if self._index is None:
            self._build()
        return self._index.get(os.path.basename(p))
