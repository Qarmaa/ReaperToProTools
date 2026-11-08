#!/usr/bin/env python3
"""regress.py — регрессионный прогон конвертера по набору сессий Pro Tools (перед выпуском).

    regress.py run <список.txt | session.ptx ...> [--out результат.json]
    regress.py diff <было.json> <стало.json>

По каждой сессии: сводка чтения (дорожки, клипы, маркеры, плагины, отсутствующие файлы). По каждому плагину: какой блок Reaper подобран,
каким классом перенесены настройки (fx_state.STATS['last']), хэш результата и круг PT -> Reaper -> PT (ptx_plugin_state.blob_from_reaper
возвращает исходное состояние байт в байт или нет). diff печатает отличия; код выхода 1, если они есть.
Нужен vst3host (PX_VST3HOST или ../vst3host/build/vst3host) — иначе плагины класса named дают другой результат.
"""
import contextlib
import hashlib
import io
import json
import os
import sys

import fx_state as F
import ptx_plugin_state as PS
import ptx_plugins as P
import reaper_fx as R
from ptx_model import PTX


def _sessions(args):
    out = []
    for a in args:
        out += [l.strip() for l in open(a) if l.strip()] if a.endswith('.txt') else [a]
    return [f for f in out if 'Session File Backups' not in f]


def run(paths):
    import ptx_to_reaper
    inst, ul = R.load_installed(), R.load_user_library()
    res = {}
    for f in paths:
        key = os.path.join(os.path.basename(os.path.dirname(f)), os.path.basename(f))
        try:
            p = PTX(open(f, 'rb').read())
        except Exception as e:                                 # noqa: BLE001 — файл недоступен / не сессия
            res[key] = {'error': type(e).__name__}
            continue
        with contextlib.redirect_stdout(io.StringIO()):
            info = ptx_to_reaper.convert(f, info=True)
        summary = {k: info.get(k) for k in ('tracks', 'clips', 'markers', 'plugins', 'sends', 'automation', 'folders', 'clip_gain', 'fades', 'video_clips')}
        summary['missing'] = len(info.get('missing') or ())
        plugs = {}
        for ti, tr in enumerate(P.read_plugins(p)):
            for x in tr:
                b = R.block_for(x, inst, ul)
                row = {'name': x['name'], 'format': x['format'], 'block': b[0][:90] if b else None}
                if b is not None:
                    out = F.apply_state(list(b), x)
                    row['class'] = F.STATS['last']
                    row['hash'] = hashlib.sha1('\n'.join(out).encode()).hexdigest()[:12]
                    if row['class'] and x.get('state'):
                        try:
                            h, raw, t = F._decode(out)
                            back = PS.blob_from_reaper(x['state'], raw, None, x['name'])
                            row['roundtrip'] = None if back is None else back == bytes(x['state'])
                        except Exception as e:             # noqa: BLE001
                            row['roundtrip'] = 'error ' + type(e).__name__
                plugs[f'{ti}.{x["slot"]}'] = row
        res[key] = {'summary': summary, 'plugins': plugs}
        print(f'{key}: {len(plugs)} plug-ins', flush=True)
    return res


def diff(a, b):
    n = 0
    for k in sorted(set(a) | set(b)):
        if k not in a or k not in b:
            print(f'{k}: {"new" if k not in a else "gone"}')
            n += 1
            continue
        x, y = a[k], b[k]
        if 'error' in x or 'error' in y:
            if x.get('error') != y.get('error'):
                print(f'{k}: unavailable ({y.get("error") or x.get("error")}) — not compared')
            continue
        if x.get('summary') != y.get('summary'):
            print(f'{k}: summary {x.get("summary")} -> {y.get("summary")}')
            n += 1
        px, py = x.get('plugins') or {}, y.get('plugins') or {}
        for s in sorted(set(px) | set(py)):
            u, v = px.get(s), py.get(s)
            if u != v:
                fields = [f for f in ('block', 'class', 'hash', 'roundtrip') if (u or {}).get(f) != (v or {}).get(f)]
                print(f'{k} [{s}] {(u or v)["name"]}: ' + '; '.join(f'{f}: {(u or {}).get(f)} -> {(v or {}).get(f)}' for f in fields))
                n += 1
    print(f'{n} difference(s)')
    return n


def main():
    if len(sys.argv) > 2 and sys.argv[1] == 'run':
        args = sys.argv[2:]
        out = None
        if '--out' in args:
            i = args.index('--out')
            out = args[i + 1]
            del args[i:i + 2]
        res = run(_sessions(args))
        text = json.dumps(res, ensure_ascii=False, indent=1, sort_keys=True)
        open(out, 'w', encoding='utf-8').write(text) if out else print(text)
    elif len(sys.argv) == 4 and sys.argv[1] == 'diff':
        sys.exit(1 if diff(json.load(open(sys.argv[2])), json.load(open(sys.argv[3]))) else 0)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == '__main__':
    main()
