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
from datetime import datetime, timedelta, timezone

import zstandard

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

# Three verbatim groups: everything up to and including the opening paren, the
# body, and the closing paren onward. restore() re-emits groups 1 and 3 as-is
# rather than rebuilding them, so quoting style, case, spacing and CRLF all
# survive - an earlier version rebuilt the prefix as INSERT INTO "%s" and
# corrupted every dump that did not happen to double-quote its table names.
INSERT = re.compile(rb'^(INSERT INTO .+? VALUES\s*\()(.*)(\);\r?)$', re.IGNORECASE)


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


_PROXY = zstandard.ZstdCompressor(level=3)


def _ints(col):
    """Ints that survive int()->str() exactly, else None. Rejects '007', '+7', ''."""
    out = []
    for v in col:
        # Not lstrip(b'-'): that accepts '--7', which then blows up in int().
        if not (v.isdigit() or (v[:1] == b'-' and v[1:].isdigit())):
            return None
        try:
            n = int(v)
        except ValueError:              # >4300 digits, per Python 3.11+
            return None
        if str(n).encode() != v:
            return None
        out.append(n)
    return out or None


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
TS = re.compile(rb"^'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z'$")


def _ts_render(e):
    d = _EPOCH + timedelta(seconds=e)
    return b"'%04d-%02d-%02dT%02d:%02d:%02dZ'" % (
        d.year, d.month, d.day, d.hour, d.minute, d.second)


def _stamps(col):
    """Quoted ISO-8601 UTC timestamps as epoch seconds, else None.

    Gated on exact re-render, so a value the regex accepts but arithmetic does
    not reproduce (or that datetime rejects outright) falls back to the other
    candidates rather than corrupting.
    """
    out = []
    for v in col:
        m = TS.match(v)
        if not m:
            return None
        try:
            d = datetime(*(int(x) for x in m.groups()), tzinfo=timezone.utc)
            e = int((d - _EPOCH).total_seconds())
        except (ValueError, OverflowError):
            return None
        if _ts_render(e) != v:
            return None
        out.append(e)
    return out or None


def _width(nums):
    lo, hi, w = min(nums), max(nums), 1
    while lo < -(1 << (8 * w - 1)) or hi >= (1 << (8 * w - 1)):
        w += 1
    return w


def _planes(vals, w):
    """Fixed-width big-endian, then byte-transposed: all high bytes, then next."""
    b = b''.join(v.to_bytes(w, 'big', signed=True) for v in vals)
    return b''.join(b[i::w] for i in range(w))


def encode_col(col):
    """(token, blob) for one column, stored the smallest of five ways.

    Decimal ASCII is the worst case twice over: an ID column is a ramp the coder
    re-learns per row, and a wide value column mixes high and low digits in one
    byte stream. Delta fixes the first, byte-planes the second, and which one
    wins is a property of the column, not something worth predicting. A
    timestamp column is a third case - an ISO-8601 string is 22 bytes spelling
    a number that fits in four, so it becomes epoch seconds and then planes.
    """
    nums, kind = _ints(col), b'T'
    if not nums:
        nums, kind = _stamps(col), b'S'
    if not nums:
        return b'A', b'\n'.join(col)
    d = [a - b for a, b in zip(nums, [0] + nums[:-1])]
    n, w, wd = len(nums), _width(nums), _width(d)
    cands = [(b'A', b'\n'.join(col)),
             (b'%s:%d:%d' % (kind, w, n), _planes(nums, w))]
    if kind == b'T':                    # delta spellings are decimal, ints only
        cands += [(b'D', b'\n'.join(str(x).encode() for x in d)),
                  (b'X:%d:%d' % (wd, n), _planes(d, wd))]
    else:
        cands.append((b'Y:%d:%d' % (wd, n), _planes(d, wd)))
    # ponytail: pick with a cheap proxy codec so the transform stays backend-agnostic.
    # Costs ~1% vs picking with the real backend; that needs 4 xz runs per column.
    return min(cands, key=lambda c: len(_PROXY.compress(c[1])))


def decode_col(tok, blob):
    if tok == b'A':
        return blob.split(b'\n')
    if tok == b'D':
        acc, out = 0, []
        for x in blob.split(b'\n'):
            acc += int(x)
            out.append(str(acc).encode())
        return out
    kind, w, n = tok.split(b':')
    w, n = int(w), int(n)
    nums = [int.from_bytes(blob[i::n], 'big', signed=True) for i in range(n)]
    if kind in (b'X', b'Y'):
        acc, out = 0, []
        for x in nums:
            acc += x
            out.append(acc)
        nums = out
    if kind in (b'S', b'Y'):
        return [_ts_render(x) for x in nums]
    return [str(x).encode() for x in nums]


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
    tables = {}                      # prefix -> [ [col0 values], [col1 values], ... ]
    order = []                       # verbatim INSERT prefixes, first-seen order
    literals = []                    # every non-INSERT line, verbatim
    plan = bytearray()               # per line: L, or I/J<prefix idx>,<field count>

    for line in lines:
        m = INSERT.match(line)
        if not m:
            literals.append(line)
            plan += b'L\n'
            continue
        name, fields = m.group(1), split_values(m.group(2))
        if name not in tables:
            tables[name] = []
            order.append(name)
        cols = tables[name]
        while len(cols) < len(fields):
            cols.append([])
        for i, f in enumerate(fields):
            cols[i].append(f)
        # J marks the CRLF spelling of the closing ');' so mixed-EOL dumps rejoin
        # byte-exactly; the prefix itself is stored verbatim in `order`.
        plan += b'%c%d,%d\n' % (b'IJ'[m.group(3).endswith(b'\r')],
                                order.index(name), len(fields))

    streams = [bytes(plan), b'\n'.join(literals), b'\n'.join(order)]
    for name in order:
        cols = tables[name]
        streams.append(str(len(cols)).encode())
        encoded = [encode_col(col) for col in cols]
        streams.append(b' '.join(tok for tok, _ in encoded))
        streams.extend(blob for _, blob in encoded)
    return _pack(streams)


def restore(blob):
    streams = _unpack(blob)
    plan = streams[0].split(b'\n')[:-1]
    literals = streams[1].split(b'\n')
    order = streams[2].split(b'\n') if streams[2] else []

    tables, k = [], 3
    for _ in order:
        ncol = int(streams[k]); k += 1
        toks = streams[k].split(b' '); k += 1
        cols = [decode_col(toks[i], streams[k + i]) for i in range(ncol)]
        k += ncol
        tables.append(cols)

    lit_i = 0
    # One read pointer PER COLUMN, not per table: transform only appends to
    # column i for rows that actually have an i-th field, so a table whose row
    # arity varies (extended INSERTs, concatenated dumps) desynchronises a
    # single shared counter and then runs off the end.
    ptrs = [[0] * len(cols) for cols in tables]
    out = []
    for step in plan:
        if step == b'L':
            out.append(literals[lit_i]); lit_i += 1
            continue
        t, nf = step[1:].split(b',')
        t, nf = int(t), int(nf)
        cols, ptr, fields = tables[t], ptrs[t], []
        for i in range(nf):
            fields.append(cols[i][ptr[i]])
            ptr[i] += 1
        out.append(order[t] + b','.join(fields) +
                   (b');\r' if step[:1] == b'J' else b');'))
    return b'\n'.join(out)


BACKENDS = {
    'zstd -19': lambda d: zstandard.ZstdCompressor(level=19).compress(d),
    'xz -9': lambda d: lzma.compress(d, preset=9),
}

def selfcheck():
    """Cases the sample dumps do not happen to contain. Cheap, so it runs always."""
    cols = [
        [b'007', b'8'],                       # leading zero: must stay ASCII
        [b'+7', b'8'], [b'-0', b'1'],         # int() would eat these
        [b'--7', b'8'],                       # lstrip(b'-') used to pass this to int()
        [str(9).encode() * 5000, b'1'],       # over Python's 4300-digit int() cap
        [b"'text'", b'NULL'], [b''],          # not ints at all
        [b'5'],                               # single value
        [str(i).encode() for i in range(500)],            # ramp -> delta
        [str(i * 7919 % 99991).encode() for i in range(500)],  # wide -> planes
        [b'-9', b'300', b'-40000', b'1'],     # signed, width crossings
        [str(2 ** 200).encode(), b'0'],       # bigger than any fixed width
        [b"'2005-12-27T18:46:47Z'", b"'1970-01-01T00:00:00Z'"],   # timestamps
        [b"'1948-05-28T03:04:05Z'"],          # pre-epoch: negative seconds
        [b"'0000-99-99T99:99:99Z'", b"'2005-12-27T18:46:47Z'"],   # shaped, invalid
        [b"'2005-12-27T18:46:47Z'", b'NULL'],  # not all timestamps
    ]
    for col in cols:
        tok, blob = encode_col(col)
        assert decode_col(tok, blob) == col, (tok, col)
    assert encode_col([b'007', b'8'])[0] == b'A'
    assert encode_col([b'--7', b'8'])[0] == b'A'
    assert encode_col([b"'0000-99-99T99:99:99Z'", b"'2005-12-27T18:46:47Z'"])[0] == b'A'

    # Whole-dump shapes. Every one of these round-tripped to DIFFERENT bytes or
    # crashed before the 2026-07-31 audit; the shipped dumps hid all of them by
    # being uniformly double-quoted, uniform-arity and LF-terminated.
    rows = b"INSERT INTO %s VALUES(1,'a');\nINSERT INTO %s VALUES(2,'b');"
    dumps = [
        b'CREATE TABLE t (a,b);\n' + rows % (b't', b't'),          # unquoted name
        rows % (b'`t`', b'`t`'),                                   # backticks
        rows % (b'"a""b"', b'"a""b"'),                             # escaped quotes
        b'insert into "t" values(1,2);',                           # lowercase
        b'INSERT INTO "t" VALUES (1,2);',                          # space before (
        b'INSERT INTO "t" VALUES(1,2);\r\nINSERT INTO "t" VALUES(3,4);\r',  # CRLF
        b'INSERT INTO "t" VALUES(1,2);\nINSERT INTO "t" VALUES(3,4);\r',    # mixed
        b'INSERT INTO "t" VALUES(1,\'a\');\nINSERT INTO "t" VALUES(2,\'b\',9);',  # ragged
        b'INSERT INTO "t" VALUES(1,2),(3,4);\nINSERT INTO "t" VALUES(5,6);',  # multi-row
        b"INSERT INTO \"t\" VALUES(1,'two\nlines');",              # embedded newline
        b'INSERT INTO "t" VALUES(--7);',                           # crashed _ints
        b'',                                                       # empty input
    ]
    for d in dumps:
        assert restore(transform(d)) == d, d[:60]
    print('selfcheck: ok')


if __name__ == '__main__':
    selfcheck()
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
