# SPEC — pipecat-slng 0.3.0

## §G Goal

Ship 0.3.0 polish for pipecat community-integration listing: ergonomic ctor kwargs, raise-on-connect, WS-first example/docs, coverage gaps, py.typed, CI.

## §C Constraints

- py >=3.11
- pipecat-ai >=1.3.0; tested v1.3.0
- BSD-2-Clause
- uv env/deps; ruff lint+format; ty typecheck; pytest+pytest-asyncio
- aiohttp for HTTP; websockets for WS
- src/ layout; hatchling build; single distribution (no split per service)
- ctor changes additive only — existing call sites unbroken
- PyPI publish, public repo flip, demo video, Discord, listing PR: OOB (human, not in spec)
- AGENTS.md unstaged diff: OOB (separate concern, not in spec)
- docs/ stays gitignored — design notes local only

## §I Interfaces

- I.STT — `SlngSTTService(*, api_key, model, base_url, encoding, sample_rate, region_override, world_part_override, settings, language?, enable_vad?, enable_partials?, **kw)` — `src/pipecat_slng/stt.py`
- I.STTSettings — `SlngSTTSettings(model, language, enable_vad, enable_partials)`
- I.TTSWS — `SlngTTSService(*, api_key, model, voice, base_url, encoding, sample_rate, region_override, world_part_override, settings, language?, speed?, **kw)` — `src/pipecat_slng/tts.py`
- I.TTSHTTP — `SlngHttpTTSService(*, api_key, model, voice, base_url, aiohttp_session, sample_rate, region_override, world_part_override, settings, language?, speed?, **kw)`
- I.TTSSettings — `SlngTTSSettings(model, voice, language, speed)`
- I.wire-WS-STT — `wss://api.slng.ai/v1/bridges/unmute/stt/{model}`. init+ready handshake. Binary audio frames. Control: keepalive/finalize/close.
- I.wire-WS-TTS — `wss://api.slng.ai/v1/bridges/unmute/tts/{model}`. init+ready handshake. Client: text/flush/clear/close. Server binary audio + JSON.
- I.wire-HTTP-TTS — `POST https://api.slng.ai/v1/bridges/unmute/tts/{model}`. Body `{text, voice}`. Query params `region`, `world-part`.
- I.example — `examples/bot.py` — STT + LLM + TTS cascade
- I.readme — `README.md`
- I.changelog — `CHANGELOG.md`
- I.gitignore — `.gitignore`
- I.pyproject — `pyproject.toml`
- I.ci — `.github/workflows/ci.yml`
- I.typed — `src/pipecat_slng/py.typed`

## §V Invariants

- V1 — HTTP TTS body = `{text, voice}` only. Sending `config` object → 400. Per OpenAPI `unmute-tts-bridge-http`.
- V2 — WS-TTS connection-level config (voice/speed/language) applies only via reconnect. Bridge consumes them in `init` message.
- V3 — STT must wait for server `ready` before sending audio frames. Else WS policy violation 1008 closes socket.
- V4 — Connect failures (`_connect_websocket` in STT + WS-TTS) push_error AND raise. PipelineRunner must surface.
- V5 — In-stream send errors (`run_stt`, `run_tts` WS + HTTP) yield ErrorFrame AND call push_error. No raise inside async generator.
- V6 — `examples/bot.py` defaults to WS TTS (`SlngTTSService`). Streaming UX is primary story; HTTP is fallback.
- V7 — README carries explicit company attribution ("Built and maintained by the SLNG team"). Community-guide MUST.
- V8 — `py.typed` packaged in the built wheel. Verify `unzip -l dist/*.whl | grep py.typed` nonempty.
- V9 — Public ctor surface additive only: `language`, `speed`, `enable_vad`, `enable_partials` default to NOT_GIVEN; mapped to `default_settings` only when caller passes explicit value. Existing call sites unbroken.
- V10 — CI default run excludes live-API tests. `pytest -k 'not live'`. No SLNG_API_KEY in CI secrets.
- V11 — HTTP TTS compressed-format response (MP3/Ogg) → ErrorFrame. Never silently passed as PCM.
- V12 — `pyproject.toml` version = `0.3.0`. CHANGELOG `[0.3.0]` entry mirrors A/B/C/D blocks.
- V13 — STT confidence filter is conditional: applied iff bridge surfaces `confidence` on `final_transcript` payload (top-level or `channel.alternatives[0].confidence`). Threshold 0.5. If absent, docstring note instead — no code branch.

## §T Tasks

```
id  | st | task                                                                 | cites
T1  | x  | A1 add ctor kwargs language/enable_vad/enable_partials to SlngSTTService | V9,I.STT,I.STTSettings
T2  | x  | A1 add ctor kwargs language/speed to SlngTTSService                  | V9,I.TTSWS,I.TTSSettings
T3  | x  | A1 add ctor kwargs language/speed to SlngHttpTTSService              | V9,I.TTSHTTP,I.TTSSettings
T4  | x  | A2 _connect_websocket STT raise after push_error                     | V4,I.STT
T5  | x  | A2 _connect_websocket WS-TTS raise after push_error                  | V4,I.TTSWS
T6  | x  | A2 run_stt push_error before yield ErrorFrame                        | V5,I.STT
T7  | x  | A2 run_tts WS push_error before yield ErrorFrame                     | V5,I.TTSWS
T8  | x  | A2 run_tts HTTP push_error before yield ErrorFrame                   | V5,I.TTSHTTP,V11
T9  | .  | A3 deferred-verify STT confidence: live log + apply iff present      | V13,I.STT,I.wire-WS-STT
T10 | .  | B1 rewrite examples/bot.py WS default, drop commented variants       | V6,I.example
T11 | .  | B2 README attribution + WS-first restructure + HTTP body doc         | V7,V1,V11,I.readme
T12 | .  | C1 tests: region/world headers STT + WS-TTS                          | I.STT,I.TTSWS,I.wire-WS-STT,I.wire-WS-TTS
T13 | .  | C2 tests: on_audio_context_interrupted→clear; flush_audio→flush      | I.TTSWS
T14 | .  | C3 tests: VAD stop→finalize; from_finalize→confirm_finalize          | I.STT,V3
T15 | .  | C4 tests: EndFrame/CancelFrame→{type:close} both                     | I.STT,I.TTSWS
T16 | .  | D1 add src/pipecat_slng/py.typed + wheel verify                      | V8,I.typed,I.pyproject
T17 | .  | D2 CI workflow: ruff+ty+pytest matrix py3.11/3.12/3.13               | V10,I.ci
T18 | .  | D3 .gitignore append .claude/, .pytest_cache/, .ruff_cache/          | I.gitignore
T19 | .  | D4 pyproject.toml version 0.3.0                                      | V12,I.pyproject
T20 | .  | D5 CHANGELOG.md [0.3.0] entry                                        | V12,I.changelog
```

Status legend: `.` todo | `~` in-progress | `x` done | `!` blocked

## §B Bugs

```
id | date | cause | fix
```
