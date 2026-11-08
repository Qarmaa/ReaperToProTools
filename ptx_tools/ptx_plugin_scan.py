#!/usr/bin/env python3
"""ptx_plugin_scan.py — идентичности установленных AAX-плагинов из DigiPageTable.xml (производитель, продукт, ID по форматам каналов).

У части плагинов (FabFilter, ...) в бандле .aaxplugin лежит Contents/Resources/DigiPageTable.xml со списком `<Plugin manID prodID plugID><Desc>имя (формат)`.
Этого достаточно, чтобы создать в .ptx слот такого плагина (см. ptx_slot_gen.py): Pro Tools узнаёт плагин по тройке FourCC, имя и effect ID он подставляет сам.
Результат — личная таблица ~/Library/Application Support/Reaper to Pro Tools/plugin_ids.json; ptx_plugins._ids_lookup использует её вместе со встроенной.

Второй источник — кэш самого Pro Tools (`/Users/Shared/Pro Tools/AAXPlugInCache/*.plugincache.txt`, его пишет PT при сканировании
плагинов): тройка FourCC и формат каналов для КАЖДОГО установленного AAX-плагина. Читается на лету (read_pt_cache), без DigiShell.

    ptx_plugin_scan.py --scan [папка ...]    -> {"found": n_новых_или_обновлённых, "total": всего}
    ptx_plugin_scan.py --count
    ptx_plugin_scan.py --pt-cache            -> {"plugins": n}
"""
import glob
import json
import os
import re
import sys

USER_DIR = os.path.expanduser('~/Library/Application Support/Reaper to Pro Tools')
USER_FILE = os.path.join(USER_DIR, 'plugin_ids.json')
DIRS = ['/Library/Application Support/Avid/Audio/Plug-Ins', os.path.expanduser('~/Library/Application Support/Avid/Audio/Plug-Ins')]


def load_user():
    try:
        return json.load(open(USER_FILE, encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_user(d):
    os.makedirs(USER_DIR, exist_ok=True)
    tmp = USER_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, USER_FILE)


def parse_table(xml):
    """[(manID, prodID, plugID, описание)] из page table (DigiPageTable.xml, AAXPageTables.xml, *.PageTables.xml, page_table.xml …)."""
    return re.findall(r"<Plugin\s+manID=['\"](.{4})['\"]\s+prodID=['\"](.{4})['\"]\s+plugID=['\"](.{4})['\"]\s*>\s*<Desc>\s*(.*?)\s*</Desc>", xml, re.S)


PT_FMT = {'mono': 'mono', 'stereo': 'stereo', 'lcr': 'LCR', 'lcrs': 'LCRS', 'quad': 'quad', 'quadraphonic': 'quad', '5.0': '5.0', '5.1': '5.1'}
PT_CH = {1: 'mono', 2: 'stereo', 3: 'LCR', 5: '5.0', 6: '5.1'}


def split_desc(desc):
    """(имя, формат) из описания варианта в page table или (имя, None), если формат не наш (AudioSuite, DSP, вход != выход, 7.1.2 …).
    Виды: «Pro-Q 3 (Stereo)», «Altiverb 8 Stereo Native», «RX 12 De-hum Mono Native», «UAD Pultec EQP-1A 2 -> 2 by Universal Audio, Inc.»,
    «LX480 Native, 2 in X 2 out»."""
    d = re.sub(r'\s+by\s+[^()]*$', '', desc.replace('&gt;', '>').replace('&amp;', '&')).strip()
    if re.search(r'audiosuite|\bdsp\b|\btdm\b|\bhdx\b', d, re.I):
        return d, None
    m = re.match(r'(.*?)\s*\(([^()]*)\)$', d)
    if m:
        f = m.group(2).split(',')[-1].strip().lower()
        return m.group(1), PT_FMT.get(f)
    m = re.match(r'(.*?)\s+(\d+)\s*->\s*(\d+)$', d) or re.match(r'(.*?)\s+(?:native,\s*)?(\d+) in x (\d+) out$', d, re.I)
    if m:
        base = re.sub(r'\s+native,?$', '', m.group(1), flags=re.I)
        return base, PT_CH.get(int(m.group(2))) if m.group(2) == m.group(3) else None
    if not re.search(r'\s+native$', d, re.I):
        return d, None                                         # формат последним словом — только у вариантов «… Native» (у Slapper «stereo Stereo» = вход/выход DSP)
    d = re.sub(r'\s+native$', '', d, flags=re.I)
    if re.search(r'\bto\s+\S+$', d, re.I):
        return d, None                                         # «Mono to Stereo», «Stereo to 5.1»: вход != выход
    m = re.match(r'(.*?)\s+(\S+)$', d)
    if m and m.group(2).lower() in PT_FMT:
        return m.group(1), PT_FMT[m.group(2).lower()]
    return d, None


def scan(dirs=None):
    """Идентичности AAX-плагинов из их page tables (без Pro Tools: на машине, где PT ещё не сканировал плагины)."""
    out = {}
    for d in dirs or DIRS:
        for b in sorted(glob.glob(os.path.join(d, '**', '*.aaxplugin'), recursive=True)):
            res = os.path.join(b, 'Contents', 'Resources')
            rows = []
            for f in sorted(glob.glob(os.path.join(res, '*.xml'))):
                if re.search(r'page_?tables?', os.path.basename(f), re.I):
                    try:
                        rows += parse_table(open(f, encoding='utf-8', errors='replace').read())
                    except OSError:
                        pass
            stem = os.path.basename(b)[:-len('.aaxplugin')]
            prods = {}
            for man, prod, plug, desc in rows:
                base, fmt = split_desc(desc)
                if not fmt and plug == prod and base == desc.strip():
                    fmt = 'stereo'                                 # описание без формата и plugID == prodID — основной (стерео) вариант
                if not fmt:
                    continue
                e = prods.setdefault(base, {'mfr': man, 'prod': prod, 'plugs': {}})
                if (e['mfr'], e['prod']) == (man, prod):
                    e['plugs'].setdefault(fmt, plug)
            for base, v in prods.items():                       # имя — из описания («UAD Pultec EQP-1A»), имя бандла — синоним
                out[base] = {'mfr': v['mfr'], 'prod': v['prod'], 'plugs': v['plugs'], 'bundle': os.path.basename(b), 'eid': '',
                             'aliases': [stem] if len(prods) == 1 and stem != base else []}
    return out


_PAGES = None


def read_page_tables():
    """scan() один раз за запуск — запасной источник, когда кэша Pro Tools нет (PT не установлен или ещё не сканировал плагины)."""
    global _PAGES
    if _PAGES is None:
        _PAGES = scan()
    return _PAGES


PT_CACHE_DIR = '/Users/Shared/Pro Tools/AAXPlugInCache'
PT_STEMS = {'0': 'mono', '1': 'stereo', '2': 'LCR', '3': 'quad', '4': 'LCRS', '5': '5.0', '6': '5.1'}   # StemFormats.Input (сверено с таблицей DigiShell)
PT_NATIVE = '3'                                                    # PlugInType Native: этот plugInID и пишется в сессию
_PT_CACHE = None


def parse_pt_cache(text, bundle=''):
    """{имя: запись как в ptx_plugin_ids.IDS} из одного .plugincache.txt; только варианты Native со входом == выходу."""
    prods = {}
    for blk in re.split(r'\n\tplugin: \d+\n', text)[1:]:
        f = dict(re.findall(r'^\t\t([\w.]+): ?(.*)$', blk, re.M))
        cc = {k: (re.match(r'(.{4}) \(\d+\)$', f.get(k, '')) or [None, None])[1] for k in ('manufacturerID', 'productID', 'plugInID')}
        stem = PT_STEMS.get(f.get('StemFormats.Input'))
        if f.get('PlugInType') != PT_NATIVE or not all(cc.values()) or not stem or f.get('StemFormats.Input') != f.get('StemFormats.Output'):
            continue
        txt = lambda k: (f.get(k) or '').encode('latin-1', 'replace').decode('utf-8', 'replace').strip()   # имена — UTF-8
        name = txt('name')
        if not name:
            continue
        # группа — по имени: у бандлов-оболочек (Soundtoys) все плагины делят производителя и продукт, различается только plugInID
        d = prods.setdefault((cc['manufacturerID'], cc['productID'], name), {'aliases': [], 'plugs': {}})
        d['plugs'].setdefault(stem, cc['plugInID'])
        pi = txt('PIName')
        if pi and pi != name and pi not in d['aliases']:
            d['aliases'].append(pi)
    return {n: {'mfr': m, 'prod': pr, 'plugs': d['plugs'], 'bundle': bundle, 'eid': '', 'aliases': d['aliases']}
            for (m, pr, n), d in prods.items()}


def read_pt_cache(folder=PT_CACHE_DIR):
    """Все установленные AAX-плагины из кэша Pro Tools (пустой словарь, если Pro Tools на машине нет)."""
    global _PT_CACHE
    if _PT_CACHE is None or folder != PT_CACHE_DIR:
        out = {}
        for fn in sorted(glob.glob(os.path.join(folder, '*.plugincache.txt'))):
            try:
                text = open(fn, encoding='latin-1').read()                       # FourCC — сырые байты (бывают не ASCII)
                out.update(parse_pt_cache(text, os.path.basename(fn).split('.aaxplugin')[0] + '.aaxplugin'))
            except OSError:
                pass
        if folder != PT_CACHE_DIR:
            return out
        _PT_CACHE = out
    return _PT_CACHE


WAVES_DIRS = ('/Applications/Waves',)
WAVES_CH = {1: 'mono', 2: 'stereo', 3: 'LCR', 4: 'LCRS', 5: '5.0', 6: '5.1'}
_WAVES = None


def parse_waves(pagetable, procxml, other=None):
    """{имя: запись как в IDS} одного бандла Waves. В PageTable на вариант по две тройки: SubComp (WD2S — внутреннее имя компонента,
    встречается в остальных XML бандла: ProcessXML, ParamXML, ...) и ID, который Pro Tools пишет в сессию (YF4U, только в PageTable) —
    берутся вторые. other — текст остальных XML (по умолчанию procxml). Имя — из ProcessXML (`<SubComponentType Name="…"> 'T37S'`)
    у SubComp с тем же описанием; если продукт в бандле один (все описания одного продукта) — его имя (в PageTable J37 называется «TJ37»)."""
    other = procxml if other is None else other
    rows = re.findall(r"<Plugin\s+manID=['\"](.{4})['\"]\s+prodID=['\"](.{4})['\"]\s+plugID=['\"](.{4})['\"]\s*>\s*<Desc>\s*(.*?)\s*</Desc>", pagetable, re.S)
    named = dict((sc, nm) for nm, sc in re.findall(r"<SubComponentType\s+Name=\"([^\"]+)\">\s*'(.{4})'", procxml))
    subs = {pl for _, _, pl, _ in rows if pl in named or pl in other}

    def split(desc):
        d = desc.replace('&gt;', '>').replace(' by Waves Audio', '').strip()
        m = re.match(r'(.*?)\s+(\d+)\s*->\s*(\d+)$', d)
        if m:
            return (m.group(1), WAVES_CH.get(int(m.group(2)))) if m.group(2) == m.group(3) else (m.group(1), None)   # 1 -> 2: вход != выход
        if re.search(r'\bto (?:mono|stereo)$', d, re.I):
            return d, None                                     # «ARPlates Mono to Stereo», «NX 5 to Stereo»: вход != выход
        m = re.match(r'(.*?)\s+(Mono|Stereo)$', d, re.I)
        return (m.group(1), m.group(2).lower()) if m else (d, None)
    by_desc = {d: named[pl] for _, _, pl, d in rows if pl in named}
    bases = {split(d)[0] for _, _, pl, d in rows if pl not in subs}
    only = next(iter(set(named.values()))) if len(set(named.values())) == 1 and len(bases) == 1 else None
    if only and next(iter(bases)).lower().startswith(only.lower() + ' '):
        only = None                                            # «CLA-2A Lab» при имени «CLA-2A» — другой продукт, не переименовываем
    out = {}
    for man, prod, plug, desc in rows:
        base, fmt = split(desc)
        if plug in subs or not fmt:
            continue
        name = by_desc.get(desc) or only or base
        e = out.setdefault(name, {'mfr': man, 'prod': prod, 'plugs': {}, 'bundle': '', 'eid': '', 'aliases': [base] if base != name else []})
        e['plugs'].setdefault(fmt, plug)
    for e in out.values():                                     # у Vocal Bender в PageTable подписи Mono/Stereo перепутаны; ID Waves: моно …O, стерео …U
        pl = e['plugs']
        if pl.get('mono', '').endswith('U') and pl.get('stereo', '').endswith('O'):
            pl['mono'], pl['stereo'] = pl['stereo'], pl['mono']
    return out


def read_waves(dirs=WAVES_DIRS):
    """Установленные плагины Waves (/Applications/Waves/Plug-Ins V*/<x>.bundle): их нет в кэше Pro Tools (они внутри WaveShell)."""
    global _WAVES
    if _WAVES is None or dirs != WAVES_DIRS:
        out = {}
        for d in dirs:
            for b in sorted(glob.glob(os.path.join(d, 'Plug-Ins V*', '*.bundle'))):          # по возрастанию версии: новая перекрывает старую
                res = os.path.join(b, 'Contents', 'Resources')
                try:
                    pt = ''.join(open(f, encoding='utf-8', errors='replace').read() for f in glob.glob(os.path.join(res, 'PageTable', '*.xml')))
                    px = ''.join(open(f, encoding='utf-8', errors='replace').read() for f in glob.glob(os.path.join(res, 'ProcessXML', '*.xml')))
                    other = ''.join(open(f, encoding='utf-8', errors='replace').read() for f in glob.glob(os.path.join(res, '*', '*.xml'))
                                    if os.sep + 'PageTable' + os.sep not in f)
                except OSError:
                    continue
                for k, v in parse_waves(pt, px, other).items():
                    v['bundle'] = os.path.basename(b)
                    out[k] = v
        if dirs != WAVES_DIRS:
            return out
        _WAVES = out
    return _WAVES


def _selftest():
    blk = lambda pid, typ, st: (f"\tplugin: 1\n\t\tname: Shell A\n\t\tmanufacturerID: WM   (1)\n\t\tproductID: SToy (2)\n\t\tplugInID: {pid} (3)\n"
                                f"\t\tPIName: A\n\t\tPlugInType: {typ}\n\t\tStemFormats.Input: {st}\n\t\tStemFormats.Output: {st}\n")
    t = 'numPlugInFileEntries: 1\n' + blk('AAm1', 3, 0) + blk('AAs1', 3, 1) + blk('AAd1', 1, 1) + blk('AAx1', 3, 33)
    r = parse_pt_cache(t, 'x.aaxplugin')
    assert r == {'Shell A': {'mfr': 'WM  ', 'prod': 'SToy', 'plugs': {'mono': 'AAm1', 'stereo': 'AAs1'}, 'bundle': 'x.aaxplugin', 'eid': '', 'aliases': ['A']}}, r
    pt = ("<Plugin manID='ksWV' prodID='TJ37' plugID='V59O'><Desc>TJ37 Mono by Waves Audio</Desc></Plugin>"
          "<Plugin manID=\"ksWV\" prodID=\"WDBL\" plugID=\"WD2S\"><Desc>Doubler2 2 -&gt; 2 by Waves Audio</Desc></Plugin>"
          "<Plugin manID=\"ksWV\" prodID=\"WDBL\" plugID=\"YF4U\"><Desc>Doubler2 2 -&gt; 2 by Waves Audio</Desc></Plugin>"
          "<Plugin manID=\"ksWV\" prodID=\"WDBL\" plugID=\"YF4Z\"><Desc>Doubler2 1 -&gt; 2 by Waves Audio</Desc></Plugin>")
    w = parse_waves(pt[90:], "<SubComponentType Name=\"Doubler2\"> 'WD2S' </SubComponentType>")
    assert w == {'Doubler2': {'mfr': 'ksWV', 'prod': 'WDBL', 'plugs': {'stereo': 'YF4U'}, 'bundle': '', 'eid': '', 'aliases': []}}, w
    j = parse_waves(pt[:90], "<SubComponentType Name=\"Abbey Road J37 Tape\"> 'T37M'")
    assert j['Abbey Road J37 Tape']['plugs'] == {'mono': 'V59O'} and j['Abbey Road J37 Tape']['aliases'] == ['TJ37'], j
    lab = ("<Plugin manID='ksWV' prodID='LA2A' plugID='LL2M'><Desc>CLA-2A Lab 1 -&gt; 1</Desc></Plugin>"
           "<Plugin manID='ksWV' prodID='LA2A' plugID='NN4O'><Desc>CLA-2A Lab 1 -&gt; 1</Desc></Plugin>")
    c = parse_waves(lab, "<SubComponentType Name=\"CLA-2A\">'LA2M'", other="... 'LL2M' ...")      # SubComp, которого нет в ProcessXML
    assert c == {'CLA-2A Lab': {'mfr': 'ksWV', 'prod': 'LA2A', 'plugs': {'mono': 'NN4O'}, 'bundle': '', 'eid': '', 'aliases': []}}, c
    sd = split_desc
    assert sd('Pro-Q 3 (Stereo)') == ('Pro-Q 3', 'stereo') and sd('RX 12 De-hum Mono Native') == ('RX 12 De-hum', 'mono')
    assert sd('UAD Pultec EQP-1A 2 -&gt; 2 by Universal Audio, Inc.') == ('UAD Pultec EQP-1A', 'stereo') and sd('LX480 Native, 2 in X 2 out')[1] == 'stereo'
    assert not sd('Altiverb 8 Mono to 5.1 Native')[1] and not sd('LX480 AudioSuite, 2 in X 2 out')[1] and not sd('Slapper stereo Stereo')[1]
    assert not sd('UAD Pultec EQP-1A 1 -&gt; 2 by Universal Audio, Inc.')[1] and not sd('Altiverb 8 7.1.2 Native')[1]
    print('ok')


def main():
    if '--scan' in sys.argv:
        dirs = [a for a in sys.argv[1:] if not a.startswith('--')] or None
        found = scan(dirs)
        cur = load_user()
        changed = sum(1 for k, v in found.items() if cur.get(k) != v)
        cur.update(found)
        save_user(cur)
        print(json.dumps({'found': changed, 'total': len(cur), 'scanned': len(found)}, ensure_ascii=False))
    elif '--selftest' in sys.argv:
        _selftest()
    elif '--pt-cache' in sys.argv:
        print(json.dumps({'plugins': len(read_pt_cache()), 'waves': len(read_waves())}))
    elif '--count' in sys.argv:
        print(len(load_user()))


if __name__ == '__main__':
    main()
