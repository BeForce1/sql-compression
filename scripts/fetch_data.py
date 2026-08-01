"""Rebuild the test data. Nothing here is committed.

Three real datasets, no synthetic data - synthetic tables are unrealistically
regular and would make any columnar transform look better than it is:

  chinook.db     a real third-party sample database (many narrow typed columns)
  wiki.db        real Wikipedia revision metadata pulled out of enwik8, loaded
                 into a real SQLite container, with indices
  wiki_meta.db   the same rows minus the body column - isolates "narrow columns"
                 from "one dominant blob column", which turned out to be the
                 whole story for the SQL dump transform
  layer.tar.gz   a real Docker Hub layer blob

Fetches enwik8 itself if needed - the wiki tables are built from real article text.

    python scripts/fetch_shape_data.py
"""
import json
import os
import re
import sqlite3
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, 'data')
ENWIK8 = os.path.join(ROOT, 'corpus', 'enwik8')

# Pinned to a commit, not master: this blob is the source of the best Part 2
# number in the repo, and upstream rebuilds it across releases. Verified
# byte-identical to the measured blob on 2026-07-31.
CHINOOK_SHA = 'ac32dbc3d5b383633c3fd687934f9c719773f00d'
CHINOOK = (f'https://github.com/lerocha/chinook-database/raw/{CHINOOK_SHA}/'
           'ChinookDatabase/DataSources/Chinook_Sqlite.sqlite')
CHINOOK_SIZE_MEASURED = 1_007_616     # the blob the README's -28.6% came from
REPO, TAG = 'library/python', '3.12-slim'
LAYER_SIZE_MEASURED = 29_780_905      # the blob the README's numbers came from


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def chinook():
    dst = os.path.join(DATA, 'chinook.db')
    if os.path.exists(dst):
        print('have chinook.db')
        return
    blob = get(CHINOOK)
    if len(blob) < 100_000:
        sys.exit(f'chinook fetch looks wrong: {len(blob)} bytes')
    open(dst, 'wb').write(blob)
    print(f'  chinook.db: {len(blob):,} bytes')
    if len(blob) != CHINOOK_SIZE_MEASURED:
        print(f'  NOTE: DIFFERS from the measured blob ({CHINOOK_SIZE_MEASURED:,} B).\n'
              '        The SQL-dump figures in the README were measured on that one.')


ENWIK8_ZIP = 'https://mattmahoney.net/dc/enwik8.zip'
ENWIK8_SIZE = 100_000_000


def _fetch_enwik8():
    """Real article text for the wiki tables. Not committed - 100 MB, and it is
    Wikipedia's content under its own licence, not ours to redistribute."""
    import io
    import zipfile
    os.makedirs(os.path.dirname(ENWIK8), exist_ok=True)
    print(f'fetching {ENWIK8_ZIP} (100 MB, one time) ...')
    blob = get(ENWIK8_ZIP)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        data = z.read(next(n for n in z.namelist() if n.endswith('enwik8')))
    if len(data) != ENWIK8_SIZE:
        sys.exit(f'  enwik8: got {len(data):,} bytes, expected {ENWIK8_SIZE:,}')
    open(ENWIK8, 'wb').write(data)
    print(f'  enwik8: {len(data):,} bytes')


def wiki_tables():
    """Real Wikipedia revision metadata -> a real SQLite file and its SQL dump."""
    if os.path.exists(os.path.join(DATA, 'wiki.db')):
        print('have wiki.db')
        return
    if not os.path.exists(ENWIK8):
        _fetch_enwik8()
    raw = open(ENWIK8, 'rb').read(40_000_000).decode('utf-8', errors='ignore')
    pages = re.findall(
        r'<title>(.*?)</title>.*?<id>(\d+)</id>.*?<timestamp>(.*?)</timestamp>.*?'
        r'<(?:username>(.*?)</username|ip>(.*?)</ip)>.*?<text[^>]*>(.*?)</text>',
        raw, re.S)
    print(f'  parsed {len(pages):,} revisions from enwik8')
    rows = [(int(i), t, ts, (u or ip), 1 if ip else 0, len(b), b[:2000])
            for t, i, ts, u, ip, b in pages]

    specs = [
        ('wiki.db', 'wiki.sql',
         'CREATE TABLE revision (id INTEGER PRIMARY KEY, title TEXT, ts TEXT, '
         'editor TEXT, is_ip INTEGER, textlen INTEGER, body TEXT)',
         ['CREATE INDEX idx_title ON revision(title)',
          'CREATE INDEX idx_ts ON revision(ts)'],
         lambda r: r),
        ('wiki_meta.db', 'wiki_meta.sql',
         'CREATE TABLE revision (id INTEGER PRIMARY KEY, title TEXT, ts TEXT, '
         'editor TEXT, is_ip INTEGER, textlen INTEGER)',
         ['CREATE INDEX idx_ts ON revision(ts)'],
         lambda r: r[:6]),
    ]
    for dbname, sqlname, ddl, idx, project in specs:
        path = os.path.join(DATA, dbname)
        if os.path.exists(path):
            os.remove(path)
        db = sqlite3.connect(path)
        db.execute(ddl)
        for i in idx:
            db.execute(i)
        vals = [project(r) for r in rows]
        ph = ','.join('?' * len(vals[0]))
        db.executemany(f'INSERT OR REPLACE INTO revision VALUES ({ph})', vals)
        db.commit()
        with open(os.path.join(DATA, sqlname), 'w', encoding='utf-8', newline='\n') as f:
            for line in db.iterdump():
                f.write(line + '\n')
        db.close()
        print(f'  {dbname}: {os.path.getsize(path):,} bytes   '
              f'{sqlname}: {os.path.getsize(os.path.join(DATA, sqlname)):,} bytes')


def docker_layer():
    """Largest layer of python:3.12-slim, via the anonymous registry API.

    The tag is rebuilt periodically, so the blob you get may not be the one the
    README measured. Size is reported and compared rather than asserted - a
    different layer is fine, it just is not the same number.
    """
    dst = os.path.join(DATA, 'layer.tar.gz')
    if os.path.exists(dst):
        print('have layer.tar.gz')
        return
    tok = json.loads(get('https://auth.docker.io/token?service=registry.docker.io'
                         f'&scope=repository:{REPO}:pull'))['token']
    accept = ','.join([
        'application/vnd.oci.image.index.v1+json',
        'application/vnd.docker.distribution.manifest.list.v2+json',
        'application/vnd.docker.distribution.manifest.v2+json',
        'application/vnd.oci.image.manifest.v1+json'])
    h = {'Authorization': f'Bearer {tok}', 'Accept': accept}
    man = json.loads(get(f'https://registry-1.docker.io/v2/{REPO}/manifests/{TAG}', h))
    if 'manifests' in man:
        d = next(m['digest'] for m in man['manifests']
                 if m['platform']['architecture'] == 'amd64'
                 and m['platform']['os'] == 'linux')
        man = json.loads(get(f'https://registry-1.docker.io/v2/{REPO}/manifests/{d}', h))
    big = max(man['layers'], key=lambda l: l['size'])
    blob = get(f"https://registry-1.docker.io/v2/{REPO}/blobs/{big['digest']}",
               {'Authorization': f'Bearer {tok}'})
    open(dst, 'wb').write(blob)
    note = ('matches the measured blob' if len(blob) == LAYER_SIZE_MEASURED else
            f'DIFFERS from the measured {LAYER_SIZE_MEASURED:,} B - the tag was rebuilt, '
            'so your numbers will not match the README exactly')
    print(f'  layer.tar.gz: {len(blob):,} bytes  ({note})')


if __name__ == '__main__':
    os.makedirs(DATA, exist_ok=True)
    chinook()
    wiki_tables()
    docker_layer()
    print('\nshape data ready. None of it is committed - it is other people\'s '
          'content under their own licences.')
