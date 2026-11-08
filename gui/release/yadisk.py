#!/usr/bin/env python3
"""yadisk.py — минимум Яндекс.Диска для выкладки релизов (токен — из Keychain, сервис yadisk-oauth)."""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

API = 'https://cloud-api.yandex.net/v1/disk'


def token():
    return subprocess.run(['security', 'find-generic-password', '-s', 'yadisk-oauth', '-w'], capture_output=True, text=True).stdout.strip()


def call(method, path, params=None, data=None):
    url = API + path + ('?' + urllib.parse.urlencode(params) if params else '')
    req = urllib.request.Request(url, method=method, headers={'Authorization': 'OAuth ' + token()})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, (json.loads(body) if body else {})


def ensure_folder(path):
    st, _ = call('PUT', '/resources', {'path': path})
    return st in (201, 409)                                # 409 — уже есть


def publish(path):
    call('PUT', '/resources/publish', {'path': path})
    st, d = call('GET', '/resources', {'path': path, 'fields': 'public_url,public_key'})
    return d.get('public_url')


def upload(local, remote, wait=900):
    """Загрузка через curl; сервер Яндекса держит PUT после последнего байта, поэтому готовность проверяем по размеру на Диске."""
    size = os.path.getsize(local)
    st, d = call('GET', '/resources/upload', {'path': remote, 'overwrite': 'true'})
    if st != 200:
        raise RuntimeError(f'upload url: {st} {d}')
    href = d['href']
    p = subprocess.Popen(['curl', '-s', '-S', '-X', 'PUT', '-T', local, href, '--max-time', str(wait)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < wait:
        st, d = call('GET', '/resources', {'path': remote, 'fields': 'size'})
        if st == 200 and d.get('size') == size:
            p.terminate()
            return True
        if p.poll() is not None and p.returncode not in (0, None):
            pass
        time.sleep(3)
    p.terminate()
    return False


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'setup':
        ensure_folder(sys.argv[2])
        print(publish(sys.argv[2]))
    elif cmd == 'upload':
        ok = upload(sys.argv[2], sys.argv[3])
        print(ok)
        sys.exit(0 if ok else 1)
