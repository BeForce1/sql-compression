"""Page-grouping transform for SQLite files. Byte-lossless.

A SQLite file is a flat array of fixed-size pages of wildly different kinds -
table leaves full of records, index leaves full of keys, freelist pages that are
mostly zeros - interleaved in whatever order the database happened to grow. A
general compressor sees one stream that keeps changing character.

This sorts pages by kind (keeping a permutation so it is reversible), so all the
index pages sit together, all the zero-filled free pages sit together, and the
backend gets long runs of similar bytes.

Deliberately does NOT parse the record format. Reconstructing a byte-identical
SQLite file from logical rows is not possible - page layout, free space and
vacuum state are not recoverable from the data - so anything that reads rows and
rewrites them is not a lossless compressor. Working at the page level is what
keeps this honest.

    python sqlitepages.py data/wiki.db
"""
import collections
import lzma
import os
import sys

import zstandard

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

TYPES = {0x02: 'interior index', 0x05: 'interior table',
         0x0A: 'leaf index', 0x0D: 'leaf table'}


def _pack(streams):
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


def page_size(data):
    """Header bytes 16-17, or 0 if this is not a usable page array.

    Not everything a directory walker hands you is a database. An empty file, a
    truncated one, or a -wal sidecar (whose salt-1 sits at offset 16) can all put
    zero here, and len(data) // 0 is a crash rather than a lossless no-op.
    """
    if len(data) < 100:
        return 0
    ps = int.from_bytes(data[16:18], 'big')
    if ps == 1:
        return 65536
    return ps if 512 <= ps <= 65536 and not (ps & (ps - 1)) else 0


def _kind(i, page):
    # Page 1 carries the 100-byte file header before its b-tree header.
    return page[100] if i == 0 else (page[0] if page else 0)


def transform(data):
    ps = page_size(data)
    n = len(data) // ps if ps else 0
    if not n:                                # not a page array: ride in the tail
        return _pack([(0).to_bytes(4, 'big'), b'', b'', data])
    pages = [data[i * ps:(i + 1) * ps] for i in range(n)]
    tail = data[n * ps:]                     # trailing partial page, if any

    # Stable sort by kind: like pages adjacent, original order preserved within
    # a kind so sequential similarity survives. Store the KIND bytes, not the
    # permutation - the sort is a pure function of them, so a 4-byte page index
    # is three bytes of nothing. Worth doing because the whole gain here is 0.4-4%.
    kinds = bytes(_kind(i, pages[i]) for i in range(n))
    order = sorted(range(n), key=lambda i: (kinds[i], i))
    body = b''.join(pages[i] for i in order)
    return _pack([ps.to_bytes(4, 'big'), kinds, body, tail])


def restore(blob):
    ps_b, kinds, body, tail = _unpack(blob)
    ps = int.from_bytes(ps_b, 'big')
    order = sorted(range(len(kinds)), key=lambda i: (kinds[i], i))
    pages = [None] * len(kinds)
    for slot, orig in enumerate(order):
        pages[orig] = body[slot * ps:(slot + 1) * ps]
    return b''.join(pages) + tail


def census(data):
    ps = page_size(data)
    n = len(data) // ps
    c = collections.Counter()
    zero = 0
    for i in range(n):
        p = data[i * ps:(i + 1) * ps]
        c[TYPES.get(_kind(i, p), f'other/0x{_kind(i, p):02x}')] += 1
        if not p.strip(b'\0'):
            zero += 1
    return ps, n, c, zero


BACKENDS = {
    'zstd -19': lambda d: zstandard.ZstdCompressor(level=19).compress(d),
    'xz -9': lambda d: lzma.compress(d, preset=9),
}

def selfcheck():
    """Inputs a directory walker will find that are not databases."""
    for bad in (b'', b'\0' * 50, b'\0' * 4096, os.urandom(200),
                b'SQLite format 3\0' + b'\0' * 200,       # zero page size
                b'SQLite format 3\0' + b'\x00\x07' + b'\0' * 200):  # 7: not a power of 2
        assert restore(transform(bad)) == bad, bad[:20]
    print('selfcheck: ok')


if __name__ == '__main__':
    selfcheck()
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(DATA,'wiki.db')
    data = open(path, 'rb').read()

    t = transform(data)
    back = restore(t)
    assert back == data, f'NOT LOSSLESS: {len(back):,} vs {len(data):,}'

    ps, n, kinds, zero = census(data)
    print(f'{os.path.basename(path)}  {len(data):,} bytes   round-trip: ok')
    print(f'  {n:,} pages of {ps:,} B; {zero:,} are entirely zero')
    for k, v in kinds.most_common():
        print(f'    {v:>7,}  {k}')
    print(f"\n  {'backend':<10} {'plain':>12} {'transformed':>12} {'gain':>8} {'total ratio':>12}")
    for name, fn in BACKENDS.items():
        a, b = len(fn(data)), len(fn(t))
        print(f'  {name:<10} {a:>12,} {b:>12,} {1 - b / a:>7.1%} '
              f'{len(data) / b:>11.2f}x')
