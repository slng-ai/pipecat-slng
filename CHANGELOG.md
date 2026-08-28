# Changelog

All notable changes to `pipecat-slng` are documented here. This project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **A TTS session that ends without the server closing the socket is now rebuilt,
  so one bad turn no longer mutes the agent for the rest of the call.** This changes
  connection lifecycle on every WebSocket TTS route and is the reason to read this
  entry.

  Observed with `soniox/tts-rt:v1`: the greeting spoke, and every turn after it
  failed with `SLNG TTS error: Stream <id> not found. Send a start message first.`
  — the same error, forever, with `TTS context ... completed with no audio` behind
  it. The bridge ends its synthesis stream on flush and does not open a new one,
  but the plugin is what made it unrecoverable. Two gaps, both now closed:

  - **A completed utterance now closes the connection when the server left it open.**
    The per-utterance reconnect added in 0.3.0 only fired when the *server* closed
    (`slng/rime/arcana`); a route that finishes the utterance and keeps the socket
    never reached it, so the next turn was sent into a spent stream. Closing routes
    both shapes through the same quiet reconnect.

    `audio_end` and `flushed` are now handled as one "utterance complete" path.
    Which of the two a route sends is a per-route accident: `soniox/tts-rt:v1`
    sends `audio_end` and **never** `flushed`, though the plugin does send the
    `flush` that precedes it. It does close on its own eventually — 40 s after
    `audio_end` in an observed call, long after the next turn needed the session.

    **The rebuild happens as the next turn starts, not when the previous one
    ended.** The bridge drops a session that receives no text within **5 s** of
    `init` (measured at 5.054 s and 5.053 s on consecutive turns), so rebuilding
    at end-of-turn left the new session idle through the user's whole turn and
    it timed out every time — a `Request timeout` error frame per turn and two
    sessions burned. Rebuilding from `on_turn_context_created`, as Pipecat's own
    `services/soniox/tts.py` does, overlaps the handshake with sentence
    aggregation instead: off the critical path, and short enough not to trip the
    idle timer.

    **Only routes that need it pay for it.** The rebuild is armed by a session
    actually failing, not by a model-name table: `cartesia/sonic:3.5` and the
    deepgram routes also send `audio_end` but keep the session usable, and
    rebuilding them anyway cost a measured ~440 ms of time-to-first-audio per
    turn for nothing. A route that never fails is never rebuilt and behaves
    exactly as it did before this release. A route that does fail costs one
    recoverable error per call — inaudible, since recovery no longer drops the
    turn — and is then rebuilt between turns for the rest of the call.

    An interruption counts as spending the session too. A barge-in ends the turn
    before `audio_end` arrives, so the signal the rebuild keys on never comes —
    the turn after an interruption was sent into a dead stream and lost its
    first sentence, because that sentence had already gone out before the error
    came back. Pipecat's own `services/soniox/tts.py` likewise treats an
    interruption as ending the stream.

    **The utterance a dying session takes down with it is spoken anyway.**
    Nothing can predict a route's first failure, so one turn per call is sent
    into a session that has already died — observed as "Of course!" going out
    44 ms before the error came back and never being heard, while every later
    sentence of the same turn waited on the ready gate and played normally.
    Text that drew an error instead of audio is now replayed on the replacement
    session, ahead of the rest of the turn. Text that already produced audio is
    never replayed, so the recovery cannot speak anything twice.

    **Not re-tested live on `slng/rime/arcana:3-en`**, the close-after-flush route
    the 0.3.0 handling was built for — a deliberate call, not a blocked one.
    Covered offline instead — three consecutive per-utterance closes still
    reconnect quietly with no error frames, and a route sending both `audio_end`
    and `flushed` ends its turn exactly once rather than twice. What stays
    unverified for that shape is turn-boundary *timing*: `TTSStoppedFrame` is now
    emitted on whichever terminal signal arrives first, so on a route sending
    `audio_end` before `flushed` the turn ends marginally earlier than it did.

    This also means the workaround retires itself: if the bridge is fixed to
    open a fresh stream per utterance, no session fails, nothing arms, and the
    per-turn rebuild simply never runs.
  - **An `error` on a live session now tears that session down.** Previously the
    error was logged and pushed downstream while the socket stayed open and the
    session stayed marked ready, so every later utterance drew the identical error.
    An error arriving *before* the session is ready — a rejected config, a bad key —
    is deliberately left to Pipecat's backoff and permanent give-up, because
    rebuilding cannot fix it.

  **Timing — partly measured, not yet settled.** The rebuild is triggered at turn
  start and races the LLM's first sentence. In one observed turn on
  `soniox/tts-rt:v1` the handshake took 1.13 s while the LLM gave 0.84 s of lead, so
  the turn's *first* sentence waited ~290 ms on the ready gate; later sentences in
  the same turn were unaffected. **That is a single sample and must not be treated
  as the number** — one run proves nothing. Still owed: several samples per
  configuration, and the same comparison on a route that reuses one session for the
  whole call (`deepgram/aura`, `cartesia/sonic`), which has not been run against
  this version at all.

  **Recovery keeps the turn it interrupts.** The error usually lands mid-turn, before
  the reply's audio has arrived. Tearing the session down through the normal
  disconnect path also tore down the active audio context, so everything the rebuilt
  session then delivered for that turn was discarded — heard as the agent starting a
  sentence or two into its reply. The error path now drops the transport without
  touching the audio context.

  **Rebuilding no longer opens two sockets.** `run_tts` finding no connection and the
  receive task's own rebuild both open one, and only the socket the receive loop picks
  up is ever read — the utterance sent on the other is lost with no error, and the send
  after it draws a server-side timeout. Seen in a live call as two `Connecting to SLNG
  TTS` for a single `session ready`. Connecting is now serialised.

  Also fixed while here: `on_disconnected` fired **twice** per utterance once the
  plugin closes the socket itself, because the reconnect path tore down again
  unconditionally. It now skips the teardown when there is nothing left to tear down,
  so consumers still see exactly one event per reconnect.

  No new dependency, no new constructor argument, no public API change. `run_tts` is
  untouched, so the V15 stale-flag guard from 0.5.0 and its test are unchanged.

  Verified against Pipecat 1.8.0: 65 offline tests pass, including these new checks —
  a completed utterance rebuilds a session the server left open; the utterance after
  an error produces audio; an error before `ready` still delegates to the base class;
  one reconnect fires one `on_disconnected`; and audio delivered after a mid-turn
  error still reaches the listener; one error costs exactly one rebuilt connection; and a route that sends only
  `audio_end` is rebuilt just like one that sends `flushed`.

## [0.5.1] - 2026-08-27

### Changed

- **Requires `pipecat-ai>=1.8.0`. Versions 0.5.0 and earlier cannot be used with
  Pipecat 1.8.0 at all** — every `import pipecat_slng` raises `ImportError`, so
  this is not an optional upgrade if you are moving to 1.8.0.

  `stt.py` and `tts.py` imported `_NotGiven` from `pipecat.services.settings`.
  That sentinel was always private, and 1.8.0 moved it to
  `pipecat/utils/types.py` and made it public as `NotGiven`. The other names on
  the same import line (`NOT_GIVEN`, `is_given`, the `*Settings` bases) are
  re-exported and still resolve; only the private one is gone. The fix is a
  plain rename to the public `NotGiven` — no behavior change, no public API
  change to this package.

  Verified against Pipecat 1.8.0: the package and both submodules import, all
  three services construct, and both `is_given` branches behave. Offline suite
  is 59 passing on 1.8.0.

  This is deliberately a hard cut rather than a `try/except ImportError` alias:
  `NotGiven` does not exist before 1.8.0, so one import line cannot serve both,
  and shimming would preserve a range that was never tested anyway.

- **The declared Pipecat floor is now the version CI actually runs.** `uv.lock`
  pinned `pipecat-ai` 1.3.0 while `pyproject.toml` declared `>=1.3.0`, and CI
  installs with `uv sync --locked` — so the suite had only ever run against the
  bottom of a range whose upper end was a guess. That is why the plugin's own
  tests were green on the day it stopped importing. The lock now pins 1.8.0,
  matching the floor, and `tests/test_dependency_contract.py` fails if the two
  ever drift apart (V27) or if any module under `src/pipecat_slng/` imports a
  leading-underscore name from `pipecat.*` again (V26).

## [0.5.0] - 2026-08-26

### Fixed

- **Turn timing: the user turn now ends when the transcript arrives, not 1.0 s after
  the caller stops speaking.** This changes conversational timing for every existing
  downstream user and is the reason to read this entry.

  `SlngSTTService` sent `{"type": "finalize"}` on `VADUserStoppedSpeakingFrame` but
  never called Pipecat's `request_finalize()`, and it only called `confirm_finalize()`
  when a transcript carried `from_finalize` — a Deepgram field that is **not part of
  the SLNG bridge protocol** and that only appeared when a route happened to pass the
  raw upstream payload through. So `TranscriptionFrame.finalized` was `False` on every
  frame the plugin ever emitted.

  In Pipecat 1.7.0 the turn ends on a finalized transcript, and otherwise waits out a
  safety-net timer anchored to `speech_end + ttfs_p99_latency`
  (`turn_analyzer_user_turn_stop_strategy.py:236`). With no declared TTFS the fallback
  is `DEFAULT_TTFS_P99 = 1.0`, so **every turn waited a fixed 1.0 s after end of
  speech**, however fast the bridge replied. Lowering the VAD `stop_secs` did not help,
  because the deadline is anchored to actual end of speech rather than to VAD's report
  of it.

  Both `TurnAnalyzerUserTurnStopStrategy` and `SpeechTimeoutUserTurnStopStrategy` now
  short-circuit that timer. Measured against the live bridge at `stop_secs=0.2`, turn-end
  moves from a flat **1004 ms** after end of speech (10/10 runs within 2 ms, on both
  routes, regardless of how fast the bridge replied) to tracking the transcript:
  **605 ms** on `slng/deepgram/nova:3-en` and **281 ms** on `deepgram/nova:3` — a saving
  of ~400 ms and ~723 ms respectively. If you tuned `stop_secs`,
  `user_turn_stop_timeout`, or an `endpointing_delay` to compensate for the old fixed
  second, re-check those values.

- **Low-confidence final transcripts are no longer dropped.** The `confidence < 0.5`
  filter applied to finals as well as partials. Dropping a final does not lose a word —
  it hangs the turn: the turn-stop strategy returns early when it has no text
  (`turn_analyzer_user_turn_stop_strategy.py:350`), and the timeout handler routes
  through the same check, so the turn did not stop at all until a later transcript
  arrived. On noisy or accented audio that presented as an agent frozen mid-call.
  The filter now applies to partial transcripts only, which is what the Pipecat
  community-integration guide's ">50% confidence" bullet was reaching for.

- **`SlngTTSService` now sends the bridge's `keepalive` while idle.** It previously
  sent none, so a long caller turn could leave the synthesis socket idle long enough
  for the bridge to close it for inactivity — putting a full WebSocket reconnect plus
  `init` handshake on the path to the next segment's first audio byte. The interval is
  30 s, matching the STT side of the same bridge; neither bridge reference documents an
  interval or an idle timeout. The task is cancelled on disconnect and does not outlive
  its socket.

### Deferred — blocked on live bridge access

Recorded so the reasoning is not re-derived. None of these ship in this release.

- **A default `ttfs_p99_latency` constant.** No code is needed — the kwarg already
  reaches the base class, so `SlngSTTService(..., ttfs_p99_latency=0.7)` works today and
  is the supported way to right-size the residual timer per deployment. Only the default
  is unset, so Pipecat still substitutes 1.0 s and warns at pipeline start; that warning
  is now cosmetic, since a finalized transcript cancels the timer it sizes.

  The span *was* measured at `stop_secs=0.2`: **605 ms median** (597–659, n=16) on
  `slng/deepgram/nova:3-en` and **280 ms median** (273–338, n=13) on `deepgram/nova:3`.
  **The two routes differ by 2.2×**, so a single default cannot suit both — surfacing
  that rather than choosing, since which route to standardise on is a separate decision.
  These figures are also not interchangeable with Pipecat's built-in table, which is P99
  over 1000 `stt-benchmark` samples; a P99 over 16 samples is not a P99, and
  `stt-benchmark` only accepts services in its built-in registry, which excludes this
  package. `0.7` would cover both observed maxima and beat the current 1.0 s fallback if
  a default is wanted before then.
- **A warm standby TTS socket (`warm_standby_enabled`).** Not implemented; a consumer
  passing the kwarg still has it absorbed by `**kwargs` with no effect. Whether it is
  worth building depends on whether any route actually closes after `flushed`. The
  expected-close reconnect log now names which event armed the flag, so that question
  is answerable from any real call's logs rather than by speculation.
- **`utterance_end` as a finalization signal.** The bridge scopes it to models whose
  catalog declares a `tokenStream` with `finalMarkers` (its documented examples are
  Soniox `<end>`/`<fin>` tokens); the `deepgram/nova` routes in use are not such
  models, and its only payload field is a timestamp, so it carries no transcript. Never
  observed on either route across 29 live runs. Left logged at trace level.
- **A `mulaw`/8000 Hz phone leg.** Only half available: the STT bridge accepts
  `linear16`, `mp3`, and `opus` — **not** `mulaw` — though it does accept 8000 Hz. The
  TTS bridge does list `mulaw`. Not pursued without a phone-leg measurement.

Tested with Pipecat v1.7.0 (declared floor remains `pipecat-ai>=1.3.0`; the finalize
and TTFS APIs are identical at both versions).

## [0.4.0] - 2026-06-12

### Added
- BYOK (Bring Your Own Key) and provider-agnostic model routing. A `model`
  string's `slng/` prefix selects SLNG-hosted; any other route (e.g.
  `deepgram/aura:2`, `elevenlabs/...`, `cartesia/sonic:3`) is an external
  provider proxied through SLNG. The three supported routes:
  - `slng/...` — hosted by SLNG, billed by SLNG.
  - external route, no key — proxied via SLNG's own provider account, billed
    by SLNG.
  - external route + your `provider_key` — proxied with your key; the provider
    bills you directly (no SLNG audio-minute fees; SLNG cache still applies).
- New `provider_key` constructor kwarg on `SlngSTTService`, `SlngTTSService`,
  and `SlngHttpTTSService`. When set, the key is forwarded as the
  `X-Slng-Provider-Key` header — a key distinct from `SLNG_API_KEY`. Defaults
  to `None` — no wire change for existing call sites. Valid only on external
  routes; an `slng/...` route + key is rejected with a 400. See
  [BYOK docs](https://docs.slng.ai/execution-layer/byok).
- Tests covering all three routes: BYOK header present/absent across the three
  services, external-route-without-key sends no BYOK header, and live smoke
  tests for the SLNG-hosted, external-no-key, and BYOK routes.
- README "Model routing & bring-your-own-key (BYOK)" section with the full
  route/billing matrix and error surfaces.
- `examples/bot.py` env-driven routing: `SLNG_STT_MODEL` / `SLNG_TTS_MODEL` /
  `SLNG_TTS_VOICE` pick the routes (default `slng/...`), and an optional
  `SLNG_PROVIDER_KEY` enables BYOK on an external route.

### Changed
- WebSocket connect-rejection errors now include the server's response body,
  not just the HTTP status — e.g. a BYOK request to an `slng/...` route now
  reports *"HTTP 400 — BYOK is only supported for external STT/TTS routes"*
  instead of a bare `HTTP 400`.

## [0.3.0] - 2026-06-10

### Fixed
- `SlngTTSService` now treats a server-initiated WebSocket close after `audio_end`/`flushed` as the expected per-utterance lifecycle (observed with `slng/rime/arcana` models) and reconnects quietly. Previously every bot turn triggered Pipecat reconnect warnings, and three short turns in a row could trip Pipecat's consecutive quick-failure cap and shut the TTS service down mid-call. Unexpected closes keep the full Pipecat retry/failure machinery.

### Added
- Top-level constructor kwargs for runtime-tunable settings:
  - `SlngSTTService`: `language`, `enable_vad`, `enable_partials`
  - `SlngTTSService`: `language`, `speed`
  - `SlngHttpTTSService`: `language`, `speed` (kept for parity; not sent over wire — HTTP body is `{text, voice}` only per the SLNG OpenAPI)
- STT confidence filter: drop transcripts with top-level `confidence < 0.5`, matching the Pipecat community-integration guide. No-op when the bridge does not surface confidence.
- `py.typed` marker (PEP 561) — downstream type checkers now see inline types.
- GitHub Actions CI workflow: ruff + ruff-format + ty + pytest matrix on Python 3.11/3.12/3.13.
- New unit tests covering region/world routing headers, WS-TTS interruption (`clear`/`flush`), STT finalize (`finalize` + `from_finalize`→`confirm_finalize`), and graceful disconnect (`{type: close}`). Suite now 23 unit + 3 live (gated).

### Changed
- Error handling tightened to the community-integration guide ("raise AND push"):
  - `_connect_websocket` (STT + WS-TTS) now raises after `push_error`, so connect failures surface through `PipelineRunner` instead of dribbling silent send-after-disconnect errors.
  - In-stream send / non-200 / compressed-format paths in `run_stt` and `run_tts` (WS + HTTP) now call `push_error` alongside the existing `yield ErrorFrame`.
- `examples/bot.py` defaults to the streaming `SlngTTSService`; removed the three commented-out TTS variants (incl. the "Problematic provider" Cartesia stub).
- `README.md` reorganised "WebSocket first, HTTP fallback"; added explicit company attribution under the title; documented the HTTP body contract (`{text, voice}` only).

Tested with Pipecat v1.3.0.

## [0.2.0] - 2026-05-29

### Added
- `SlngHttpTTSService` — non-streaming HTTP/REST text-to-speech via the SLNG Unified TTS bridge (`POST /v1/bridges/unmute/tts/{model}`), built on `aiohttp`.

### Changed
- `SlngTTSService` now applies runtime settings updates: a `voice`/`speed`/`language` change reconnects to re-run the init handshake.

Tested with Pipecat v1.3.0.

## [0.1.0] - 2026-05-29

### Added
- `SlngSTTService` — real-time WebSocket speech-to-text via the SLNG Unmute STT bridge.
- `SlngTTSService` — real-time WebSocket text-to-speech via the SLNG Unmute TTS bridge.
- Region routing via `region_override` / `world_part_override`.
- Foundational cascade example (`examples/bot.py`).
- Unit tests (fake WebSocket) and gated live smoke tests.

Tested with Pipecat v1.3.0.
