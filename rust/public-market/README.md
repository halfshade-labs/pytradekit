# Native public market foundation (unaccepted)

`ptk-public-market` provides bounded public Binance bookTicker parsing, an
explicit-clock selector, TLS/WebSocket transport, and native route workers.
It is a foundation component under verification, **not an accepted production
backend**. Python remains the default implementation. There is no account,
order-submission, or trading-signal API. The crate does not select a production
backend and is not intended for crates.io publication (`publish = false`).

## Components and caller responsibilities

- `parse_bookticker` validates positive decimal prices/sizes, bid <= ask, symbol,
  update ID, and timestamp provenance. Decimal strings retain their text; exact
  decimal comparison does not use floating-point price/size arithmetic. Spot may
  use explicit local-receive provenance when both exchange timestamps are absent
  or null; perpetual data requires an exchange timestamp.
- `Selector` consumes `Action` values with explicit wall and monotonic clocks.
  It tracks identity/generation, membership versions, serial gaps, highwaters,
  epochs, availability, staged/enqueued delivery, and final consumer gates.
  Modes are explicit: `single`, `shadow`, or `redundant`; shadow backup data
  cannot take over primary delivery. `apply`, `apply_with_diagnostics`, and
  `apply_compact` share transitions and the first-failure latch; compact output
  omits public-state projection. Route-control APIs synchronize metadata without
  copying quote caches.
- `transport::PublicSocket` uses an explicitly resolved socket address. Outside
  loopback it requires `wss` and `fstream.binance.com` or `stream.binance.com`.
  The caller owns address resolution and endpoint policy. TLS certificate and
  hostname validation remain enabled. Connect has a 5-second deadline;
  read/write deadlines are 250 ms. Frame/message/write buffers are bounded;
  there is one outstanding subscription request. ACK, keepalive, raw, and failure
  events are explicit. Loopback fixtures may trust an ephemeral CA explicitly.
- `runtime::Runtime` owns workers and emits feed/spot control and raw rows.
  `recv` performs one bounded queue read/wait boundary. `Control` supplies
  physical truth, promotion feedback, a retained wake signal, clocks, and
  failure/accounting state. The caller must drive selector actions, freshness
  snapshots, final consumer gates, and qualified promotion feedback. On any
  runtime error it must retire the owning selector/session; a partially observed
  owner must not continue or silently switch backend.

Recovery and rotation exist. Workers reconnect with a new generation, explicit
subscription ACK qualification, bounded pre-ACK buffering, exponential backoff
(1-second base, 30-second cap, 60-second stable reset, up to 250 ms jitter), and
interruptible retry waits. Automatic perpetual rotation in non-single modes
starts at 22 hours with a 1-hour backup stagger. Promotion requires current
physical ACK truth and caller qualification. Replacement timeout is 30 seconds,
with retry after 60 seconds. Target changes retire old truth before reconnect.
Shutdown, retirement, joins, and incomplete-owner accounting are implemented;
their presence does not establish sustained exchange acceptance.

## Configured native runtime and handoff

`Runtime::start` retains the shared legacy contract. The explicit Rust opt-in
`Runtime::start_configured` enables configuration handoff and permits empty spot
targets. `enable_handoff` can enable that seam on an existing runtime. There is
no enabled wire configuration protocol in this crate.

The API uses `Targets`, `prepare_handoff`, `revalidate_handoff`, and one bounded
`handoff::Slot`. `commit_logical` fences before a same-thread logical mutation.
`progress_handoff` detaches changed workers, reaps only finished handles without
waiting, applies targets, and checks ACK readiness. `Slot::activate` revalidates
current identity/generation and exact ACK target sets. Only a prepared operation
can be aborted. Status/failure progress are bounded and unfinished cleanup is
explicit. Final destruction retains join ownership and can wait for retiring
workers. The integrating caller owns activation order and session invalidation.

With handoff enabled, native BN raw `b`, `a`, `B`, and `A` must be decimal
strings: JSON numeric values are rejected before parsing/supply for both
perpetual and spot. The normal parser then rejects missing, nondecimal,
nonpositive, or crossed values. Valid string text is preserved. Shared
`parse_bookticker`, selector/replay, and runtime without handoff retain their
legacy numeric compatibility; this gate does not redefine the default parser.

The native ingress channel holds at most 4096 events, and pre-ACK buffers are
bounded. Physical records are capped at three perpetual (including replacement
or retiring owners) and one spot. Each target list is bounded to 64 symbols;
perpetual lifetime membership storage is bounded to 64. Selector bounds still
apply where it is used. JSONL action-count and cumulative-input-byte budgets
below are research executable limits, not native runtime session duration limits
or evidence of acceptance for continuous operation.

## Research JSONL replay

`public-market-replay` reads JSONL on stdin. Configure before other actions:

```json
{"type":"configure","stream_id":"logical","mode":"redundant","symbols":["BTCUSDT"],"wall_ms":1800000000000,"monotonic_ms":0}
{"type":"route","route":"r0","slot":0,"token":"physical0","generation":1,"connected":true,"replacement":false}
{"type":"raw","route":"r0","wall_ms":1800000000000,"monotonic_ms":0,"serial":0,"membership":1,"payload":{"s":"BTCUSDT","u":1,"E":1800000000000,"b":"100","a":"101","B":"2","A":"3","run_time_ms":1800000000000,"_ptk_connection_id":"physical0","_ptk_connection_generation":1}}
{"type":"poll","wall_ms":1800000000000,"monotonic_ms":0,"output_capacity":4096}
```

Raw actions enqueue the full message, membership, serial, and arrival clocks.
`raw_captured` separates ingress from processing clocks. Poll, snapshot, and gate
qualify pending input in stable (monotonic arrival, update ID) order. Poll stages
and enqueues transitions using a 20 ms physical-state sweep cadence; its state is
cached post-poll state. Snapshot and gate reselect even inside that interval.
`state`, `state_for_symbol`, and `symbol_control` are cached projections, not
freshness actions. Poll state/projections cannot replace a fresh snapshot or
consumer gate. Pass a returned frame unchanged to `gate` for atomic final
acceptance and post-gate state. `targets`, `gap`, `retire`, and `close` are explicit
controls. Initial membership is 1; remove/re-add increments it twice.

Research limits: 64 lifetime symbols, 3 route records, 4096 pending ingress per
route, 12288 staged delivery frames, output capacity <= 4096, 65536 input bytes
per action, 1,048,576 total actions, and 512 MiB total input. Bound exhaustion is
fail-closed through rejection/accounting or explicit error, never latest-only
coalescing. Captured native ingress overflow errors; legacy raw overflow records
a serial gap. Selector errors latch failure and stop the owner. Source, receive,
and captured monotonic age must be nonnegative and <= 1000 ms; future timestamps
and 1001 ms ages are rejected. Wall time is positive and <= 4e12 ms; monotonic
processing clocks cannot regress.

Only validated nonempty EOF emits `complete`. Malformed, duplicate-key,
oversized, over-budget, or failed input has no completion attestation. Consumers
must check process exit status and completion even if earlier rows were written.
Replay does not open exchange connections.

## Native wall-clock compatibility

The shared SystemTime wall-ms helper matches the measured CPython 3.11.15
`_PyTime_AsSecondsDouble` conversion followed by PTK
`46d92fcca6b58e705d8e8e4c88f2fbaa35ddbf82`'s
`int(round(time.time() * 1000))`. Non-whole seconds convert total nanoseconds to
f64 then divide by 1e9; exact whole seconds convert integer seconds directly.
It multiplies by 1000 and rounds ties to even. Separately converting a fraction
can produce different bits. Float is used only for timestamp compatibility,
never price/size arithmetic.

`tests/fixtures/cpython_wall_reference.tsv` has 280 fixed four-column
`seconds|nanos|seconds_bits_hex|expected_ptk_ms` rows, SHA256
`6776a33eeecd605dcc736afc1bcc9d405e093cc192d43a5dfb91271c4e636874`.
Reference attribution is the actual interpreter's exported C conversion via
`ctypes.c_longlong/c_double`, followed by the installed PTK method with that C
result supplied as `time.time()`. Method-source SHA256:
`21fef98353160a9d65f180729ea301bfde7ffc0a7b1eb85903ae655984ac0b64`.
Expected values were copied unchanged, not generated by the Rust helper. The
sample includes present-time boundaries, even/odd half ties, carries, and larger
representable values: floor differs in 155 rows and split conversion in 40 bit
patterns. This is bounded evidence for that interpreter/reference domain, not
universal clock equivalence. The test
`runtime::wall_clock_tests::wall_ms_matches_all_actual_cpython_31115_ptk_reference_vectors`
checks both independent C double bits and PTK integer milliseconds.

Nonnegative Duration must fit signed-i64 nanoseconds. Outside that domain the
helper returns zero, rejected by Clock validation; pre-Epoch SystemTime retains
zero behavior. The selector's positive 4e12 ms ceiling is unchanged. Worker
ingress, runtime processing, and live Control share the helper. Monotonic floor,
fixture anchoring, waits, lifecycle deadlines, and exchange E/T are unchanged.
This crate makes no claim about an integrating application's fallback clock
when it does not use Control.

## Verification and acceptance boundary

`.github/workflows/rust-public-market.yml` uses the fixed Rust 1.90 bookworm
image digest. Dependency fetch is the only network-enabled container phase.
Unit, integration, and doc tests and a release replay build run locked/offline,
with no network, 1 CPU, 768 MiB, 64 PIDs, read-only root/checkout, dropped
capabilities, no new privileges, bounded temporary storage, and dedicated
writable cache/target directories. No account files or credentials are mounted.
TLS fixtures need the image's OpenSSL CLI, native TLS headers/libraries,
pkg-config, and C linker; temporary loopback credentials are generated and
removed during the test. Existing Python CI is separate.

Included tests exercise selector/final-gate semantics, exact decimal validation,
private-state equivalence, owner release/failure ordering, wake behavior,
configuration retirement, recovery/rotation, real loopback WebSocket framing,
configured numeric admission, and TLS CA/hostname validation. The 70-case
backoff fixture is source-extracted and I/O-free, distinct from socket tests.
A finite fixture or proposed workflow is not a runtime PASS or complete
independent Python-oracle coverage. This snapshot does not claim all 72
historical selector-oracle cases, sustained real-exchange operation, production
capacity improvement, complete application/consumer integration, independent
acceptance, exact-merge CI, or deployment approval. A component test cannot
establish that historical production overflow has been fixed.
