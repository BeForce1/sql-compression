# What's left

Costed and ordered. Everything here is blocked on work, not on ideas.

**Where the bytes actually are** — chinook's 64 columns, each compressed with `xz -9` in
isolation, re-measured 2026-08-31 at the shipped encoders:

| column | xz -9 bytes | encoding chosen | kind |
|---|---:|---|---|
| `Track.1` (names) | 26,224 | `K:A` | string |
| `Track.7` (file sizes) | 10,700 | `T:4` planes | int, handled |
| `Track.5` (composers) | 10,064 | `N:K:A` | string, NULL-heavy |
| `Track.6` (durations) | 8,352 | `T:3` planes | int, handled |
| `Album.1` (titles) | 4,072 | `K:A` | string |
| `Artist.1` | 3,028 | `K:A` | string |
| `PlaylistTrack.1` | 2,528 | `D` delta | int, handled |
| the other 57 | 14,472 | mixed | mixed |

Those sum to 79,440 against a whole-file 71,768, because compressing each column alone
throws away the cross-column context and pays 64 stream headers. Use it for *shares*, not
for a budget.

**Text columns are 69.0% of the residual (54,812 of 79,440), and all three cheap
re-spellings that could apply to them have now been tried**: dictionary-encoding lost,
front-coding lost, and the quote strip won 844 B. What remains there is model-shaped, not
spelling-shaped.

---

## ~~1. Statement-level parsing~~ — **DONE 2026-08-23** (`wiki.sql` 0.2% → 1.7%)

Shipped in `sqldump.py`. Quote-aware statement accumulation captures all 5,065 `INSERT`
statements in `wiki.sql`, including all 3,889 multi-line bodies — **86.4% of the file's
bytes**, up from 2.3%. Length-prefixed `B:n` stores multi-line string columns without
newline-delimiter ambiguity.

This also settled a claim the repo had been carrying as "not supported": with the
instrument fixed, a 37× increase in coverage bought 1.5 points, so the shape hypothesis is
now *earned* rather than guessed. See the README's refuted table.

---

## ~~2. More value spellings~~ — **DONE 2026-08-23 / 08-31** (chinook −28.6% → −30.0%)

Shipped in `sqldump.py`, in the order they landed:
- **Fixed-precision decimals (`_decimals`)** — cents-scaled integers + byte-planes
  (`F:k:w:n`) / delta-planes (`Z:k:wd:n`).
- **SQL datetime (`_stamps`)** — `'YYYY-MM-DD HH:MM:SS'` to epoch seconds + planes (`Q:w:n`)
  / delta-planes (`W:wd:n`).
- **`NULL`-presence bitmap** — 1 bit per row (`N:n:dense_tok`, `U:n`), so a dense run of
  integers or strings compresses without the literal `NULL`s interleaved.
- **Quote strip (`K:<inner>`)** — 2026-08-31, −844 B chinook / −660 B wiki_meta /
  −1,584 B wiki.sql. Also lets a quoted number or timestamp reach the integer path.

### 2b. The spellings still untried — **hours each, speculative**

The machinery to add one is ~15 lines: a detector, an encoder, and an exact re-render gate.
Only worth writing if a real dump has that column type in volume, so **measure the column's
current xz cost first** — chinook's datetime column costs 652 bytes total, so perfecting it
could never have mattered, while wiki_meta's cost 20,824.

- **UUIDs / hex blobs** — `X'A1B2...'` and `'550e8400-e29b-...'` are 2 ASCII bytes per
  information byte. Unpack to raw bytes, then planes. **No column in the three sample dumps
  has this shape**, so it needs a fixture before it can be measured at all.
- **Booleans / low-cardinality enums** — likely already handled well by xz. Measure first.
- **Dictionary/front-coding the text columns** — already tried, already lost. Do not redo.

The honest read on this section: text is 69% of chinook's residual and every cheap
re-spelling for it is now spent. The next real win there is a better *model*, not a better
notation — which is the other repo's problem.

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
- **RLE on the plan stream.** ≤168 B on a 72 KB output.
- **replacing the zstd-3 proxy with the real backend** for picking column encodings. The
  proxy already picks the xz optimum on every heavy column tested.
- **`zstd -22 --ultra` for the OCI layer.** 0.9% over `-19 --long=27` for ~3× the time.
- **`xz pb=0 lc=4` tuning.** Was worth 1.9 points before the value codec existed and 0.4
  after — it was fixing the same problem (decimal digits misaligned against the coder's
  position bits), and the codec fixes it properly.
