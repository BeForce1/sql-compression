"""What do the existing tools already achieve on these three shapes?

Run this BEFORE writing any transform. If zstd -19 is already close to the
entropy of the data, there is nothing to win and the honest answer is to stop.

    python baseline.py
"""
import bz2
import gzip
import io
import lzma
import os
import sys
import tarfile
import time

import zstandard

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')


def codecs():
    return {
        'gzip -6': lambda d: gzip.compress(d, 6),
        'gzip -9': lambda d: gzip.compress(d, 9),
        'zstd -19': lambda d: zstandard.ZstdCompressor(level=19).compress(d),
        'bz2 -9': lambda d: bz2.compress(d, 9),
        'xz -9': lambda d: lzma.compress(d, preset=9),
    }


# Two flags that matter only on a big binary blob, so they are not in the
# default table: a 128 MB window (the decoder's own default limit, so the frame
# still decodes everywhere) and the x86 branch filter for ELF-heavy tars.
def oci_codecs():
    p = zstandard.ZstdCompressionParameters.from_level(19, window_log=27, enable_ldm=True)
    return {
        'zstd -19 --long': lambda d: zstandard.ZstdCompressor(compression_params=p).compress(d),
        'xz -9 +BCJ': lambda d: lzma.compress(d, filters=[
            {'id': lzma.FILTER_X86}, {'id': lzma.FILTER_LZMA2, 'preset': 9}]),
    }


def report(label, data, note='', extra=None):
    print(f'\n{label}  -  {len(data):,} bytes {note}')
    print(f"  {'codec':<15} {'size':>12} {'ratio':>7} {'% of raw':>9} {'MB/s':>7}")
    best = None
    for name, fn in {**codecs(), **(extra or {})}.items():
        t = time.perf_counter()
        out = fn(data)
        el = time.perf_counter() - t
        mbs = len(data) / el / 1e6
        print(f'  {name:<15} {len(out):>12,} {len(data)/len(out):>6.2f}x '
              f'{100*len(out)/len(data):>8.1f}% {mbs:>7.1f}')
        if best is None or len(out) < best[1]:
            best = (name, len(out))
    print(f'  -> best: {best[0]} at {best[1]:,} bytes')
    return best


if __name__ == '__main__':
    which = sys.argv[1] if len(sys.argv) > 1 else 'all'

    if which in ('all', 'sql'):
        for f in ('wiki.sql', 'chinook.sql'):
            p = os.path.join(DATA, f)
            if os.path.exists(p):
                report(f, open(p, 'rb').read(), '(real SQL dump)')

    if which in ('all', 'sqlite'):
        for f in ('wiki.db', 'chinook.db'):
            p = os.path.join(DATA, f)
            if os.path.exists(p):
                report(f, open(p, 'rb').read(), '(real SQLite file)')

    if which in ('all', 'oci'):
        p = os.path.join(DATA, 'layer.tar.gz')
        if os.path.exists(p):
            blob = open(p, 'rb').read()
            print(f'\nlayer.tar.gz  -  {len(blob):,} bytes  (real Docker layer, as shipped)')
            tar = gzip.decompress(blob)
            print(f'  inner tar is {len(tar):,} bytes -> the gzip achieves '
                  f'{len(tar)/len(blob):.2f}x')
            with tarfile.open(fileobj=io.BytesIO(tar)) as t:
                members = t.getmembers()
            print(f'  {len(members):,} tar members')
            # A registry re-encode: same tar content, better container.
            report('layer, re-encoded from the inner tar', tar,
                   f'(vs {len(blob):,} B as shipped)', extra=oci_codecs())
