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

INSERT_START = re.compile(rb'^(INSERT INTO .+? VALUES\s*\()(.*)$', re.IGNORECASE)


def _scan_str_quotes(part, in_str):
    idx, lp = 0, len(part)
    while idx < lp:
        c = part[idx:idx + 1]
        if in_str:
            if c == b"'":
                if part[idx + 1:idx + 2] == b"'":
                    idx += 2
                    continue
                in_str = False
        elif c == b"'":
            in_str = True
        idx += 1
    return in_str


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
TS_ISO = re.compile(rb"^'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z'$")
TS_SQL = re.compile(rb"^'(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})'$")
DEC_PAT = re.compile(rb'^(-?\d+)\.(\d+)$')


def _ts_render_iso(e):
    d = _EPOCH + timedelta(seconds=e)
    return b"'%04d-%02d-%02dT%02d:%02d:%02dZ'" % (
        d.year, d.month, d.day, d.hour, d.minute, d.second)


def _ts_render_sql(e):
    d = _EPOCH + timedelta(seconds=e)
    return b"'%04d-%02d-%02d %02d:%02d:%02d'" % (
        d.year, d.month, d.day, d.hour, d.minute, d.second)


def _stamps(col):
    """Quoted ISO-8601 ('...T...Z') or SQL ('... ...') timestamps as epoch seconds.

    Gated on exact re-render, so a value the regex accepts but arithmetic does
    not reproduce falls back to other candidates rather than corrupting.
    """
    if not col:
        return None, None
    m_iso = TS_ISO.match(col[0])
    m_sql = TS_SQL.match(col[0])
    if not m_iso and not m_sql:
        return None, None
    is_iso = bool(m_iso)
    renderer = _ts_render_iso if is_iso else _ts_render_sql
    pat = TS_ISO if is_iso else TS_SQL
    out = []
    for v in col:
        m = pat.match(v)
        if not m:
            return None, None
        try:
            d = datetime(*(int(x) for x in m.groups()), tzinfo=timezone.utc)
            e = int((d - _EPOCH).total_seconds())
        except (ValueError, OverflowError):
            return None, None
        if renderer(e) != v:
            return None, None
        out.append(e)
    return (out or None), (b'S' if is_iso else b'Q')


def _dec_render(n, k):
    sign = b'-' if n < 0 else b''
    n = abs(n)
    s = str(n).encode()
    if len(s) <= k:
        s = b'0' * (k + 1 - len(s)) + s
    return sign + s[:-k] + b'.' + s[-k:]


def _decimals(col):
    """Fixed-precision decimal numbers as integer scaled units, else None."""
    if not col:
        return None, 0
    m0 = DEC_PAT.match(col[0])
    if not m0:
        return None, 0
    k = len(m0.group(2))
    out = []
    for v in col:
        m = DEC_PAT.match(v)
        if not m or len(m.group(2)) != k:
            return None, 0
        int_part, frac_part = m.group(1), m.group(2)
        try:
            sign = -1 if int_part.startswith(b'-') else 1
            abs_int = int_part.lstrip(b'-')
            if not abs_int:
                abs_int = b'0'
            val = sign * (int(abs_int) * (10**k) + int(frac_part))
        except ValueError:
            return None, 0
        if _dec_render(val, k) != v:
            return None, 0
        out.append(val)
    return (out or None), k


def _width(nums):
    lo, hi, w = min(nums), max(nums), 1
    while lo < -(1 << (8 * w - 1)) or hi >= (1 << (8 * w - 1)):
        w += 1
    return w


def _planes(vals, w):
    """Fixed-width big-endian, then byte-transposed: all high bytes, then next."""
    b = b''.join(v.to_bytes(w, 'big', signed=True) for v in vals)
    return b''.join(b[i::w] for i in range(w))


def encode_col(col, allow_null=True):
    """(token, blob) for one column, stored the smallest of candidate ways."""
    n = len(col)
    cands = []

    # Check for NULLs: bitmap + dense encoding of non-nulls
    if allow_null and any(v == b'NULL' for v in col):
        dense = [v for v in col if v != b'NULL']
        if not dense:
            return (b'U:%d' % n), b''
        mask = bytearray((n + 7) // 8)
        for i, v in enumerate(col):
            if v != b'NULL':
                mask[i // 8] |= (1 << (7 - (i % 8)))
        dense_tok, dense_blob = encode_col(dense, allow_null=False)
        cands.append(((b'N:%d:%s' % (n, dense_tok)), bytes(mask) + dense_blob))

    nums, kind, k_dec = _ints(col), b'T', 0
    if not nums:
        s_nums, s_kind = _stamps(col)
        if s_nums:
            nums, kind = s_nums, s_kind
    if not nums:
        d_nums, d_k = _decimals(col)
        if d_nums:
            nums, kind, k_dec = d_nums, b'F', d_k
    if not nums:
        if not any(b'\n' in v for v in col):
            cands.append((b'A', b'\n'.join(col)))
        else:
            lens = b''.join(len(v).to_bytes(4, 'big') for v in col)
            cands.append(((b'B:%d' % n), lens + b''.join(col)))
    else:
        d = [a - b for a, b in zip(nums, [0] + nums[:-1])]
        w, wd = _width(nums), _width(d)
        if not any(b'\n' in v for v in col):
            cands.append((b'A', b'\n'.join(col)))
        else:
            lens = b''.join(len(v).to_bytes(4, 'big') for v in col)
            cands.append(((b'B:%d' % n), lens + b''.join(col)))

        if kind == b'T':                    # Integers
            cands += [(b'T:%d:%d' % (w, n), _planes(nums, w)),
                      (b'D', b'\n'.join(str(x).encode() for x in d)),
                      (b'X:%d:%d' % (wd, n), _planes(d, wd))]
        elif kind in (b'S', b'Q'):          # Timestamps: S=ISO, Q=SQL
            cands += [(b'%s:%d:%d' % (kind, w, n), _planes(nums, w)),
                      (b'%s:%d:%d' % (b'Y' if kind == b'S' else b'W', wd, n), _planes(d, wd))]
        elif kind == b'F':                  # Fixed decimals: F=planes, Z=delta-planes
            cands += [(b'F:%d:%d:%d' % (k_dec, w, n), _planes(nums, w)),
                      (b'Z:%d:%d:%d' % (k_dec, wd, n), _planes(d, wd))]

    # ponytail: pick with a cheap proxy codec so the transform stays backend-agnostic.
    return min(cands, key=lambda c: len(_PROXY.compress(c[1])))


def decode_col(tok, blob):
    if tok.startswith(b'U:'):
        n = int(tok.split(b':')[1])
        return [b'NULL'] * n
    if tok.startswith(b'N:'):
        parts = tok.split(b':', 2)
        n = int(parts[1])
        dense_tok = parts[2]
        mask_bytes = (n + 7) // 8
        mask = blob[:mask_bytes]
        dense_blob = blob[mask_bytes:]
        dense_vals = decode_col(dense_tok, dense_blob)
        out, d_i = [], 0
        for i in range(n):
            bit = (mask[i // 8] >> (7 - (i % 8))) & 1
            if bit:
                out.append(dense_vals[d_i])
                d_i += 1
            else:
                out.append(b'NULL')
        return out
    if tok == b'A':
        return blob.split(b'\n')
    if tok.startswith(b'B:'):
        n = int(tok.split(b':')[1])
        lens = [int.from_bytes(blob[4 * i:4 * i + 4], 'big') for i in range(n)]
        off = 4 * n
        out = []
        for l in lens:
            out.append(blob[off:off + l])
            off += l
        return out
    if tok == b'D':
        acc, out = 0, []
        for x in blob.split(b'\n'):
            acc += int(x)
            out.append(str(acc).encode())
        return out
    parts = tok.split(b':')
    kind = parts[0]
    if kind in (b'T', b'S', b'Q', b'X', b'Y', b'W'):
        w, n = int(parts[1]), int(parts[2])
        nums = [int.from_bytes(blob[i::n], 'big', signed=True) for i in range(n)]
        if kind in (b'X', b'Y', b'W'):
            acc, out = 0, []
            for x in nums:
                acc += x
                out.append(acc)
            nums = out
        if kind in (b'S', b'Y'):
            return [_ts_render_iso(x) for x in nums]
        if kind in (b'Q', b'W'):
            return [_ts_render_sql(x) for x in nums]
        return [str(x).encode() for x in nums]
    elif kind in (b'F', b'Z'):
        k_dec, w, n = int(parts[1]), int(parts[2]), int(parts[3])
        nums = [int.from_bytes(blob[i::n], 'big', signed=True) for i in range(n)]
        if kind == b'Z':
            acc, out = 0, []
            for x in nums:
                acc += x
                out.append(acc)
            nums = out
        return [_dec_render(x, k_dec) for x in nums]


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
    i = 0
    n_lines = len(lines)

    while i < n_lines:
        line = lines[i]
        m = INSERT_START.match(line)
        if not m:
            literals.append(line)
            plan += b'L\n'
            i += 1
            continue

        prefix = m.group(1)
        curr_lines = [line]
        in_str = _scan_str_quotes(m.group(2), False)
        while in_str or not (curr_lines[-1].endswith(b');') or curr_lines[-1].endswith(b');\r')):
            i += 1
            if i >= n_lines:
                break
            curr_lines.append(lines[i])
            in_str = _scan_str_quotes(lines[i], in_str)

        if not in_str and (curr_lines[-1].endswith(b');') or curr_lines[-1].endswith(b');\r')):
            raw_stmt = b'\n'.join(curr_lines)
            suffix = b');\r' if curr_lines[-1].endswith(b');\r') else b');'
            body = raw_stmt[len(prefix):-len(suffix)]
            fields = split_values(body)
            if prefix not in tables:
                tables[prefix] = []
                order.append(prefix)
            cols = tables[prefix]
            while len(cols) < len(fields):
                cols.append([])
            for j, f in enumerate(fields):
                cols[j].append(f)
            # J marks the CRLF spelling of the closing ');' so mixed-EOL dumps rejoin byte-exactly.
            plan += b'%c%d,%d\n' % (b'IJ'[suffix.endswith(b'\r')],
                                    order.index(prefix), len(fields))
            i += 1
        else:
            for l in curr_lines:
                literals.append(l)
                plan += b'L\n'
            i += 1

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
        [b"'2005-12-27T18:46:47Z'", b"'1970-01-01T00:00:00Z'"],   # timestamps (ISO)
        [b"'2021-01-01 00:00:00'", b"'1970-01-01 12:30:00'"],     # timestamps (SQL)
        [b"'1948-05-28T03:04:05Z'"],          # pre-epoch: negative seconds
        [b"'0000-99-99T99:99:99Z'", b"'2005-12-27T18:46:47Z'"],   # shaped, invalid
        [b"'2005-12-27T18:46:47Z'", b'NULL'],  # not all timestamps
        [b'0.99', b'1.98', b'3.96', b'5.94'], # fixed decimals (2 places)
        [b'-0.50', b'123.45', b'0.00'],       # signed decimals
        [b'0.9', b'1.99'],                    # mixed precision -> must stay ASCII
        [b'1.99', b'NULL'],                   # decimal with NULL -> ASCII
        [b"'hello\nworld'", b"'multiline\ntext\n123'"], # multiline string column -> B:n
        [b'NULL', b'NULL', b'NULL'],          # all NULLs -> U:n
        [b'NULL', b'1', b'2', b'NULL', b'3'], # mixed NULLs + ints -> N:5:T...
        [b'NULL', b'0.99', b'1.98', b'NULL'], # mixed NULLs + decimals -> N:4:F...
    ]
    for col in cols:
        tok, blob = encode_col(col)
        assert decode_col(tok, blob) == col, (tok, col)
    assert encode_col([b'007', b'8'])[0] == b'A'
    assert encode_col([b'--7', b'8'])[0] == b'A'
    assert encode_col([b"'0000-99-99T99:99:99Z'", b"'2005-12-27T18:46:47Z'"])[0] == b'A'
    assert encode_col([b'0.9', b'1.99'])[0] == b'A'
    assert encode_col([b"'hello\nworld'", b"'test'"])[0].startswith(b'B:')
    assert encode_col([b'NULL', b'NULL'])[0] == b'U:2'
    assert encode_col([b'NULL', b'1', b'2', b'NULL'])[0].startswith(b'N:4:')

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
