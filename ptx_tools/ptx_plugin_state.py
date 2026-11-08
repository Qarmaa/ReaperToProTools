#!/usr/bin/env python3
"""ptx_plugin_state.py — настройки плагина Reaper (состояние VST3 в обёртке Reaper) -> блок состояния AAX в сессии Pro Tools (0x1038).

Обратное к fx_state.py. Берётся блок шаблона (состояние плагина по умолчанию, снятое из настоящей сессии Pro Tools) и в нём заменяются данные:
  FFBS  (FabFilter Pro-Q, Pro-C, Saturn): состояние компонента VST3 без хвоста `FFpr…` (12 байт), хвост контроллера — из шаблона;
  FabF  (Pro-L 2, Pro-DS): float32 состояния компонента -> значения записей `d_`/`l_` блока AAX (по порядку, на месте);
  XML   (Audio Ease): XML состояния компонента, размеры в заголовке пересчитываются;
  имена (soothe2, Symphony ...): значения параметров читаются из настоящего плагина VST3 (vst3host) и записываются в записи с тем же именем.
Всё, что не удалось сопоставить, остаётся как в шаблоне (по умолчанию). Возвращает новые байты блока 0x1038 или None.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fx_state as F  # noqa: E402

DATA_OFF = F.DATA_OFF


def _layout(data):
    """Записи стандартного блока AAX: [(тип, имя, смещение значения в data, размер)] или None."""
    if data[:4] != b'\x01\x01\x01\x01':
        return None
    out, j = [], 4
    try:
        while j + 4 <= len(data):
            t = chr(data[j])
            if data[j + 1:j + 2] != b'_' or t not in 'fdls':
                break
            k = data.index(b'\0', j + 2)
            nm = data[j + 2:k].decode('latin1')
            j = k + 1
            j += (-(j - 4)) % 4
            size = {'f': 4, 'd': 8, 'l': 4, 's': 2}[t]
            out.append((t, nm, j, size))
            j += 4 if t == 's' else size
    except (ValueError, IndexError):
        return None
    return out


def _comp_of(wrapper):
    sp = F._split(wrapper)
    if not sp:
        return None
    q, n, cs = sp
    return wrapper[q + 20:q + 20 + cs]


def _put(buf, t, off, v):
    if t == 'd':
        struct.pack_into('>d', buf, off, float(v))
    elif t == 'f':
        struct.pack_into('>f', buf, off, float(v))
    elif t == 'l':
        struct.pack_into('>i', buf, off, int(round(v)))
    else:
        struct.pack_into('>h', buf, off, int(round(v)))


def _resize_header(blob, old_len):
    """Размер блока: LE u32 по смещению 6 = длина - 14; у части плагинов то же число (BE u16) лежит по смещению 12."""
    blob = bytearray(blob)
    be_same = struct.unpack_from('>H', blob, 12)[0] == (struct.unpack_from('<I', blob, 6)[0] & 0xFFFF)
    struct.pack_into('<I', blob, 6, len(blob) - 14)
    if be_same:
        struct.pack_into('>H', blob, 12, (len(blob) - 14) & 0xFFFF)
    return bytes(blob)


def _ffbs(tpl, comp):
    data = bytes(tpl[DATA_OFF:])
    pos = data.find(bytes(tpl[22:26]) + b'\x03\x00\x00\x00', 12)           # начало состояния контроллера в блоке PT
    if pos < 0 or comp[:4] != b'FFBS' or len(comp) - 12 != pos or comp[:8] != data[:8]:
        return None
    return bytes(tpl[:DATA_OFF]) + comp[:pos] + data[pos:]


def _fabf(tpl, comp):
    data = bytearray(tpl[DATA_OFF:])
    lay = _layout(bytes(data))
    if not lay or comp[:4] != b'FabF':
        return None
    nl = struct.unpack_from('<I', comp, 8)[0]
    p = 12 + nl
    try:
        cnt = struct.unpack_from('<I', comp, p + 4)[0]
    except struct.error:
        return None
    if cnt != len(lay) or p + 8 + 4 * cnt > len(comp):
        return None
    vals = struct.unpack_from('<%df' % cnt, comp, p + 8)
    for (t, nm, off, sz), v in zip(lay, vals):
        _put(data, t, off, v)
    return bytes(tpl[:DATA_OFF]) + bytes(data)


def _xml(tpl, comp):
    data = bytes(tpl[DATA_OFF:])
    if data[:5] != b'<?xml' or len(comp) < 9 or comp[4:9] != b'<?xml':
        return None
    n = struct.unpack_from('<I', comp, 0)[0]
    xml = comp[4:4 + n]
    end_t = data.rfind(b'>') + 1
    new = bytes(tpl[:DATA_OFF]) + xml + data[end_t:]
    return _resize_header(new, len(tpl))


def _vstw_xml(tpl, comp):
    """Sonnox: состояние компонента — VST2-банк с XML внутри -> XML в блок AAX (хвост нулей — из шаблона)."""
    data = bytes(tpl[DATA_OFF:])
    span = F._vstw_xml_span(comp)
    if data[:5] != b'<?xml' or not span:
        return None
    i, n = span
    xml = comp[i:i + n].rstrip(b'\0')
    end_t = data.rfind(b'>') + 1
    new = bytes(tpl[:DATA_OFF]) + xml + data[end_t:]
    return _resize_header(new, len(tpl))


def _soundtoys(tpl, comp):
    """Soundtoys: текст `WIDGET = …` из VST2-банка в состоянии компонента -> [u32 BE длина][текст] блока AAX (хвост нулей — из шаблона)."""
    data = bytes(tpl[DATA_OFF:])
    span = F._vstw_xml_span(comp, F.ST_MARK)
    n_old = struct.unpack_from('>I', data, 0)[0]
    if data[4:4 + len(F.ST_MARK)] != F.ST_MARK or not span or comp[span[0]:span[0] + 30].split(b';')[0] != data[4:4 + 30].split(b';')[0]:
        return None                                       # другой плагин Soundtoys (WIDGET не совпадает) — не подставляем
    i, n = span
    text = comp[i:i + n]
    new = bytes(tpl[:DATA_OFF]) + struct.pack('>I', len(text)) + text + data[4 + n_old:]
    return _resize_header(new, len(tpl))


def _waves(tpl, comp):
    """Waves: XML из состояния компонента VST3 -> чанк `XPst` блока AAX (чанк `curS` и остальное — из шаблона). Только при одинаковом SubComp."""
    ax = F.waves_aax_xml(tpl)
    n = F.waves_comp_span(comp)
    if not ax or n is None or F.waves_sub(ax[0]) != comp[12:16]:
        return None
    xml = comp[28:28 + n]
    data = bytes(tpl[DATA_OFF:])
    new = bytearray(bytes(tpl[:DATA_OFF]) + xml + data[ax[1]:])
    d = len(new) - len(tpl)
    struct.pack_into('<I', new, 6, struct.unpack_from('<I', new, 6)[0] + d)           # общий размер блока
    struct.pack_into('>I', new, 10, struct.unpack_from('>I', new, 10)[0] + d)         # размер первого чанка (XPst)
    return bytes(new)


def waves_blob(triad, xml):
    """Блок 0x1038 Waves «с нуля» (слот без образца, собранный по PageTable Waves): чанк `XPst` с XML пресета + чанк `curS` (сетап A).
    Раскладка снята с 19 шаблонов, сохранённых Pro Tools (9 совпадают байт в байт, у остальных лишь нули выравнивания):
    LE u32 @6 = длина - 10, BE u32 @10 = длина первого чанка + 4, чанк = [u32 2][тройка][тег][имя 32][данные]."""
    c1 = b'\x00\x00\x00\x02' + triad + b'XPst' + bytes(32) + xml
    c2 = struct.pack('>I', 60) + b'\x00\x00\x00\x02' + triad + b'curS' + bytes(32) + b'Ates'
    own = b'\x38\x10\x01\x00\x00\x00' + bytes(4) + struct.pack('>I', len(c1) + 4) + c1 + c2
    return own[:6] + struct.pack('<I', len(own) - 10) + own[10:]


def _embedded(tpl, comp):
    """Состояние VST3 содержит такой же блок записей `d_имя`, как блок AAX: значения возвращаются по именам (единицы плагина, типы и размеры совпадают)."""
    data = bytearray(tpl[DATA_OFF:])
    lay = _layout(bytes(data))
    emb = F.find_embedded(comp)
    if not lay or not all(nm for _, nm, _, _ in lay) or not emb:
        return None
    k, lay_c = emb
    nc = {nm: (t, off, sz) for t, nm, off, sz in lay_c}
    hit = 0
    for t, nm, off, sz in lay:
        o = nc.get(nm)
        if o and o[0] == t and o[2] == sz:
            data[off:off + sz] = comp[k + o[1]:k + o[1] + sz]
            hit += 1
    if hit * 2 < len(lay):
        return None
    return bytes(tpl[:DATA_OFF]) + bytes(data)


def _named(tpl, comp, bundle, plugname):
    data = bytearray(tpl[DATA_OFF:])
    lay = _layout(bytes(data))
    if not lay or not all(nm for _, nm, _, _ in lay) or not bundle:
        return None
    host = F._host()
    ps = F._params(bundle)
    if not host or not ps:
        return None
    if all(nm.isdigit() for _, nm, _, _ in lay):
        ids = F._IDS.get(bundle, {})                 # параметры-номера: годятся только у плагинов на JUCE (ID VST3 = хэш строки «0», «1», ...)
        if sum(1 for _, nm, _, _ in lay if str(F.juce_id(nm)) in ids) * 10 < len(lay) * 8:
            return None    # Symphony и т. п.: Pro Tools игнорирует блок (проверено: даже при согласованном списке параметров 0x260f восстанавливает своё состояние)
    import json
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        f.write(comp)
        path = f.name
    try:
        r = subprocess.run([host, bundle, 'params', '--comp', path], capture_output=True, text=True, timeout=120)
        now = {q['id']: q for q in json.loads(r.stdout)['params']}
    except (subprocess.SubprocessError, ValueError, KeyError, OSError):
        return None
    finally:
        os.unlink(path)
    # значения шаблона вне 0..1 — реальные единицы плагина (как в fx_state): такие плагины не переносим
    for t, nm, off, sz in lay:
        if t in 'fd':
            v = struct.unpack_from('>d' if t == 'd' else '>f', data, off)[0]
            if not 0.0 <= v <= 1.0:
                return None
    match = F.make_matcher(bundle, plugname, [nm for _, nm, _, _ in lay])
    hit = 0
    for t, nm, off, sz in lay:
        q = match(nm)
        if q is None or q['id'] not in now:
            continue
        v = now[q['id']]['norm']
        if t in 'ls' and q['steps'] > 0:
            v = v * q['steps']
        _put(data, t, off, v)
        hit += 1
    if hit * 10 < len(lay) * 4:
        return None
    return bytes(tpl[:DATA_OFF]) + bytes(data)


def juce_state(comp):
    """Состояние плагина на JUCE (getStateInformation) из состояния компонента VST3 или None.

    VST3-обёртка JUCE дописывает хвост «приватных данных»: [данные][int64 их длина]['JUCEPrivateData'] — его отрезаем;
    у плагинов с VST2-совместимой обёрткой (`VstW` + fxBank `CcnK`/`FBCh` или fxProgram `FPCh`) берём чанк из неё."""
    if comp[:4] == b'VstW' and comp[16:20] == b'CcnK':
        kind = comp[24:28]
        at = {b'FBCh': 172, b'FPCh': 72}.get(kind)
        if at is None or at + 4 > len(comp):
            return None
        n = struct.unpack_from('>I', comp, at)[0]
        if at + 4 + n > len(comp):
            return None
        comp = comp[at + 4:at + 4 + n]
    tag = b'JUCEPrivateData'
    if comp.endswith(tag) and len(comp) >= len(tag) + 8:
        n = struct.unpack_from('<q', comp, len(comp) - len(tag) - 8)[0]
        cut = len(comp) - len(tag) - 8 - n
        if not 0 <= cut <= len(comp):
            return None
        comp = comp[:cut]
    else:
        return None
    if comp[:4] == b'VC2!' and len(comp) >= 8:                  # XML JUCE (copyXmlToBinary): ровно 8 + размер, дальше 5 нулей — как пишет AAX-обёртка
        n = struct.unpack_from('<I', comp, 4)[0]
        if 8 + n <= len(comp):
            comp = comp[:8 + n] + b'\0' * 5
    return comp


def juce_chunk(st):
    """Позиция тега `juce` второго чанка блока AAX (`<тройка FourCC> juce <имя 32>`) или None. Ищется тройка + тег, а не просто `juce`:
    эти 4 байта могут встретиться внутри данных."""
    i = bytes(st).find(bytes(st[18:30]) + b'juce', DATA_OFF)
    return i + 12 if i >= 0 else None


def _juce(tpl, comp):
    """Блок AAX плагина на JUCE: после записей «Complete Controls» второй чанк `<тройка FourCC> juce <имя 32>` с тем же состоянием,
    что у VST3 (getStateInformation) — плагин восстанавливается именно из него (записи Pro Tools при загрузке не читает: проверено,
    правка только записей сбрасывается). Размеры: BE u32 за 20 байт до `juce` = длина данных + 52, LE u32 @6 = длина блока - 14."""
    j = juce_chunk(tpl)
    if j is None:
        return None
    start = j + 36
    if struct.unpack_from('>I', tpl, j - 20)[0] != len(tpl) - start + 52:
        return None
    st = juce_state(comp)
    if not st or st[:4] != bytes(tpl[start:start + 4]):
        return None                                    # другой формат состояния (не тот плагин / версия): такое Pro Tools может повесить при открытии
    new = bytearray(tpl[:start] + st)
    struct.pack_into('>I', new, j - 20, len(st) + 52)
    struct.pack_into('<I', new, 6, len(new) - 14)
    return bytes(new)


def blob_from_reaper(tpl_state, wrapper, vst_file, plugname):
    """Новые байты блока 0x1038 из шаблона PT (tpl_state) и состояния VST3 Reaper (wrapper) или None."""
    comp = _comp_of(wrapper) if wrapper else None
    if comp is not None and tpl_state is not None and len(tpl_state) <= DATA_OFF + 8 and bytes(tpl_state[18:22]) == b'ksWV':
        n = F.waves_comp_span(comp)                           # слот Waves без образца: состояние строится из XML Reaper
        return waves_blob(bytes(tpl_state[18:30]), comp[28:28 + n]) if n else None
    if comp is None or tpl_state is None or len(tpl_state) <= DATA_OFF + 8:
        return None
    data = bytes(tpl_state[DATA_OFF:])
    new = None
    if data[:4] == b'FFBS':
        new = _ffbs(tpl_state, comp)
    elif data[:5] == b'<?xml':
        new = _xml(tpl_state, comp) if comp[4:9] == b'<?xml' else _vstw_xml(tpl_state, comp)
    elif data[:len(F.WV_MARK)] == F.WV_MARK:
        new = _waves(tpl_state, comp)
    elif data[4:4 + len(F.ST_MARK)] == F.ST_MARK:
        new = _soundtoys(tpl_state, comp)
    elif data[:4] == b'\x01\x01\x01\x01':
        if comp[:4] == b'FabF':
            new = _fabf(tpl_state, comp)
        else:
            new = _juce(tpl_state, comp)                       # плагин на JUCE восстанавливается из чанка `juce` (записи PT не читает) — хост не нужен
            if new is None:
                new = _embedded(tpl_state, comp)
            if new is None:
                bundle = F.find_bundle(vst_file) if vst_file else None
                new = _named(tpl_state, comp, bundle, plugname)
    return new


def _selftest():
    tri = b'MfgXPrdXPlgX'
    pay = b'VC2!' + struct.pack('<I', 6) + b'<a b/>' + b'\0' * 5

    def block(payload, decoy=b''):
        head = bytearray(DATA_OFF)
        head[18:30] = tri
        entries = b'\x01\x01\x01\x01' + decoy + b'\0' * 8          # «juce» внутри записей не должен сбивать поиск чанка
        chunk2 = struct.pack('>I', len(payload) + 52) + b'\0' * 4 + tri + b'juce' + b'\0' * 32 + payload
        b = bytearray(bytes(head) + entries + chunk2)
        struct.pack_into('<I', b, 6, len(b) - 14)
        return bytes(b)
    tpl = block(pay, decoy=b'juce')
    new_xml = b'VC2!' + struct.pack('<I', 9) + b'<a b="1"/>'[:9]
    priv = b'PRIV'
    comp = new_xml + b'\0' * 3 + priv + struct.pack('<q', len(priv)) + b'JUCEPrivateData'
    st = juce_state(comp)
    assert st == new_xml + b'\0' * 5, st
    out = _juce(tpl, comp)
    assert out == block(st, decoy=b'juce'), 'payload / sizes'
    assert _juce(tpl, b'XML!' + comp[4:]) is None, 'other state format must be refused'
    assert juce_state(b'no tail') is None
    x = b'<PresetChunkXMLTree version="3">\n</PresetChunkXMLTree>\n'
    w = waves_blob(b'ksWVWDBLYF4U', x)
    assert struct.unpack_from('<I', w, 6)[0] == len(w) - 10 and w[18:34] == b'ksWVWDBLYF4UXPst' and w[-4:] == b'Ates'
    assert F.waves_aax_xml(w) == (x, len(x)), F.waves_aax_xml(w)
    print('ok')


if __name__ == '__main__':
    _selftest()
