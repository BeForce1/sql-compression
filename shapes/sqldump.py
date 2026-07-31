"""Columnar transform for SQL dumps. Byte-lossless, backend-agnostic.

A dump stores a table row-major, so a timestamp sits next to a title next to an
integer - three different distributions interleaved, which is the worst case for
any entropy coder. This regroups the bytes column-major so like sits with like,
then hands the result to a normal compressor.

Nothing is thrown away: non-INSERT lines are kept verbatim, and a plan stream
records the original line order, so restore() reproduces the input byte for byte.
That is asserted on every run - if a dump uses a format we mis-parse, the assert
fires rather than silently corrupting.

    python sqldump.py data/wiki.sql
"""
import lzma
import os
import re
import sys

import zstandard

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

INSERT = re.compile(rb'^INSERT INTO (?:"([^"]+)"|(\S+)) VALUES\((.*)\);$')


def split_values(s):
    """Split a VALUES(...) body on top-level commas, respecting SQL quoting."""
    out, buf, i, n, in_str = [], bytearray(), 0, len(s), False
    while i < n:
        c = s[i:i + 1]
        if in_str:
            if c == b"'":
                if s[i + 1:i + 2] == b"'":       # '' is an escaped quote
                    buf += b"''"
                    i += 2
                    continue
                in_str = False
            buf += c
        elif c == b"'":
            in_str = True
            buf += c
        elif c == b',':
            out.append(bytes(buf))
            buf = bytearray()
        else:
            buf += c
        i += 1
    out.append(bytes(buf))
    return out


def _pack(streams):
    """Length-prefixed container. Boring on purpose."""
    head = len(streams).to_bytes(4, 'big')
    head += b''.join(len(s).to_bytes(8, 'big') for s in streams)
    return head + b''.join(streams)


def _unpack(blob):
    n = int.from_bytes(blob[:4], 'big')
    sizes = [int.from_bytes(blob[4 + 8 * i:12 + 8 * i], 'big') for i in range(n)]
    off = 4 + 8 * n
    out = []
    for s in sizes:
        out.append(blob[off:off + s])
        off += s
    return out


def transform(data):
    """Row-major bytes in, column-major container out."""
    lines = data.split(b'\n')
    tables = {}                      # name -> [ [col0 values], [col1 values], ... ]
    order = []                       # table names, first-seen order
    literals = []                    # every non-INSERT line, verbatim
    plan = bytearray()               # per line: L, or I<table idx>,<field count>

    for line in lines:
        m = INSERT.match(line)
        if not m:
            literals.append(line)
            plan += b'L\n'
            continue
        name = m.group(1) or m.group(2)
        fields = split_values(m.group(3))
        if name not in tables:
            tables[name] = []
            order.append(name)
        cols = tables[name]
        while len(cols) < len(fields):
            cols.append([])
        for i, f in enumerate(fields):
            cols[i].append(f)
        plan += b'I%d,%d\n' % (order.index(name), len(fields))

    streams = [bytes(plan), b'\n'.join(literals), b'\n'.join(order)]
    for name in order:
        cols = tables[name]
        streams.append(str(len(cols)).encode())
        for col in cols:
            streams.append(b'\n'.join(col))
    return _pack(streams)


def restore(blob):
    streams = _unpack(blob)
    plan = streams[0].split(b'\n')[:-1]
    literals = streams[1].split(b'\n')
    order = streams[2].split(b'\n') if streams[2] else []

    tables, k = [], 3
    for _ in order:
        ncol = int(streams[k]); k += 1
        cols = [streams[k + i].split(b'\n') for i in range(ncol)]
        k += ncol
        tables.append(cols)

    lit_i = 0
    row_i = [0] * len(tables)
    out = []
    for step in plan:
        if step == b'L':
            out.append(literals[lit_i]); lit_i += 1
            continue
        t, nf = step[1:].split(b',')
        t, nf = int(t), int(nf)
        r = row_i[t]; row_i[t] += 1
        fields = [tables[t][i][r] for i in range(nf)]
        out.append(b'INSERT INTO "%s" VALUES(%s);' % (order[t], b','.join(fields)))
    return b'\n'.join(out)


BACKENDS = {
    'zstd -19': lambda d: zstandard.ZstdCompressor(level=19).compress(d),
    'xz -9': lambda d: lzma.compress(d, preset=9),
}

if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(DATA,'wiki.sql')
    data = open(path, 'rb').read()

    t = transform(data)
    back = restore(t)
    assert back == data, (
        f'NOT LOSSLESS: {len(back):,} vs {len(data):,} bytes; first diff at '
        f'{next((i for i, (a, b) in enumerate(zip(back, data)) if a != b), "len")}')

    print(f'{os.path.basename(path)}  {len(data):,} bytes   round-trip: ok')
    print(f'  transformed container: {len(t):,} bytes (before any compression)\n')
    print(f"  {'backend':<10} {'plain':>12} {'transformed':>12} {'gain':>8} {'total ratio':>12}")
    for name, fn in BACKENDS.items():
        a, b = len(fn(data)), len(fn(t))
        print(f'  {name:<10} {a:>12,} {b:>12,} {1 - b / a:>7.1%} '
              f'{len(data) / b:>11.2f}x')
