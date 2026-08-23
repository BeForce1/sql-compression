# What's left

Costed and ordered. Everything here is blocked on work, not on ideas.

**Where the bytes actually are** — chinook's 73,252 compressed bytes, by column:

| column | xz -9 bytes | kind |
|---|---:|---|
| `Track.1` (names) | 26,755 | string |
| `Track.7` (file sizes) | 13,209 → 10,678 planes | int, already handled |
| `Track.5` (composers) | 10,215 | string |
| `Track.6` (durations) | 10,076 → 8,367 planes | int, already handled |
| everything else | ~15,000 | mixed |

**Strings are ~70% of the residual, and the two cheap re-spellings both lost** (see the
refuted table in the README). What remains there is model-shaped, not spelling-shaped.

---

## 1. Statement-level parsing — **DONE 2026-08-23** (−1.6% / 30.4 KB saved on `wiki.sql`)

Shipped in `sqldump.py`. Quote-aware statement accumulation captures 100% of all 5,065
`INSERT` statements in `wiki.sql` (including all 3,889 multi-line bodies). Length-prefixed
`B:n` format stores multi-line string columns without newline delimiter ambiguity.

---

## 2. More value spellings — **DONE 2026-08-23** (chinook −29.2%)

Shipped in `sqldump.py`:
- **Fixed-precision decimals (`_decimals`)**: Cents-scaled integers + byte-planes (`F:k:w:n`) / delta-planes (`Z:k:wd:n`).
- **SQL datetime format (`_stamps`)**: Space-separated `'YYYY-MM-DD HH:MM:SS'` timestamps to epoch seconds + byte-planes (`Q:w:n`) / delta-planes (`W:wd:n`).
- **`NULL`-presence bitmap (`_encode_col` null path)**: 1-bit-per-row bitmap (`N:n:dense_tok` and `U:n`) allowing dense non-null integers and strings to compress cleanly.

---

## 2. More value spellings — **hours each, speculative**

The timestamp candidate paid 5.8% on the one file that had a timestamp column. The same
pattern may apply to other wasteful notations, and the machinery to add one is ~15 lines:
a detector, an encoder, and an exact re-render gate.

Candidates worth a measurement, in rough order of how often they appear in real dumps:
- **UUIDs / hex blobs** — `X'A1B2...'` and `'550e8400-e29b-...'` are 2 ASCII bytes per
  information byte. Unpack to raw bytes, then planes.
- **Fixed-precision decimals** — `'19.99'` as an integer count of cents.
- **Booleans / low-cardinality enums** — likely already handled well by xz, so measure
  before writing.
- **`NULL`-heavy columns** — a presence bitmap plus the dense values.

Each is only worth writing if a real dump has that column type in volume. **Measure the
column's current xz cost first**: chinook's datetime column costs 652 bytes total, so
perfecting it could never have mattered, while wiki_meta's cost 20,824.

---

## 3. Record-level SQLite columnarisation — **days, real corruption risk**

The only follow-up with a proven mechanism: the same columnar idea that pays on dumps,
applied inside SQLite leaf pages where the fields are behind a binary record format.

**Rescoped by measurement — do not target `wiki.db`.** Leaf-cell payload is 90% of its
compressed size but it is one TEXT column, so the ceiling there is ~0.1%. The databases
where this could pay have payload as a *minority* of bytes (chinook.db: 36% raw, indexes
alone 47%), putting the honest ceiling at **~10%** over page-sorted zstd, on OLTP-shaped
databases only.

**Build a churn fixture first.** All three sample databases are freshly imported and contain
**zero** freeblocks, **zero** overflow cells and **zero** stale bytes in unallocated gaps.
Every hard byte-exact-rebuild path is therefore unexercised: a columnariser that gets the
overflow spill formula (`X=U-35; K=M+(P-M)%(U-4); local = K if K<=X else M`), freeblock
contents, or stale-gap preservation wrong **will pass every round-trip assert in this repo**
and corrupt the first real database with delete/update history. Generate one with mixed
INSERT/DELETE/UPDATE, no VACUUM, and rows over 4,061 bytes, and assert against that.

**A cheaper intermediate step exists.** Parse leaf cells *read-only* — no rebuild, no
corruption risk — and compress the index-page group using the extracted payload as a zstd
dictionary. Measured **−15.7%** on wiki_meta.db's index pages (42,099 → 35,480 B, ~3.8% of
that file). It pays only where indexes are text keys: chinook, whose indexes are integer
foreign keys, gained 0.42%.

---

## 4. Robustness for dumps we don't have — **hours**

The parser now handles quoted/unquoted/backtick names, lowercase, `VALUES (`, CRLF, ragged
arity and multi-row `VALUES(a,b),(c,d)`. Untested against real output from:

- **mysqldump** with `--extended-insert` (very long multi-row statements), `--hex-blob`,
  and backtick identifiers
- **pg_dump**, which defaults to `COPY ... FROM stdin` rather than INSERT — a completely
  different shape, and probably the single biggest coverage gap
- dumps with `ON CONFLICT` / `REPLACE INTO` prefixes

Each one is a fixture plus, at most, a regex widening. `selfcheck()` is the right place.

---

## Explicitly not worth doing

Measured, negative, recorded in `results.json` — do not re-derive:

- **dictionary-encoding or front-coding the string columns.** Both lose to plain xz.
- **RLE on the plan stream.** ≤168 B on a 73 KB output.
- **replacing the zstd-3 proxy with the real backend** for picking column encodings. The
  proxy already picks the xz optimum on every heavy column tested.
- **`zstd -22 --ultra` for the OCI layer.** 0.9% over `-19 --long=27` for ~3× the time.
- **`xz pb=0 lc=4` tuning.** Was worth 1.9 points before the value codec existed and 0.4
  after — it was fixing the same problem (decimal digits misaligned against the coder's
  position bits), and the codec fixes it properly.
