# Voice AI Backend

A backend for building voice agents with **LiveKit** (Agents SDK + Cloud) and **Slang.ai** (`slng.ai`) for STT/TTS.

## Documentation

### LiveKit
LiveKit evolves quickly — always check the latest docs before implementing.

Use the `lk docs` CLI. Run `lk docs --help` to discover commands, and `lk docs <command> --help` before using a command for the first time.

- `lk docs overview` — start here for any new topic
- `lk docs get-page` — full pages (best context)
- `lk docs search` — when you know what to look for
- `lk docs code-search` — last resort, raw code only
- `lk docs changelog` — recent changes
- `lk docs pricing-info` — pricing/plan questions

Prefer browsing (`overview`, `get-page`) over `search`, and `search` over `code-search` — pages give better context than raw code.

### Slang.ai
For STT/TTS models and configuration:
- General: https://docs.slng.ai/
- LiveKit plugin: https://docs.slng.ai/agents/livekit-plugin

## Skills to use

### Primary: voice agent work
- **`livekit-agents`** — the canonical reference for any voice agent code (workflows, handoffs, tools, sessions). It requires tests for every implementation. Use it whenever touching agent code.

### Interactions
- Always use python-dev skill to work with python code and codebase (ALWAYS)
- Always use the caveman skill for agent interactions.
- Always use the superpowers skill for feature developments interactions.
- Always use the systematic debugging skill from superpowers to debug failing code

### New features and changes
Run the superpowers workflow in order:
1. **`superpowers:brainstorming`** — before any feature/component work, to align on intent and design.
2. **`superpowers:writing-plans`** — turn the brief into a plan once requirements are clear.
3. **`superpowers:executing-plans`** or **`superpowers:subagent-driven-development`** — execute the plan; use subagent-driven when independent tasks can run in parallel.
4. **`superpowers:test-driven-development`** — write tests before implementation.
5. **`superpowers:verification-before-completion`** — never claim "done" without running verification commands and citing output.

### Debugging
- **`superpowers:systematic-debugging`** — use before proposing any fix; don't guess.

### Code quality (use during implementation, not as gatekeepers)
- **`python-dev`** — working with python code and projects, module organization, code style, dependencies, typechecking, linting, formatting.
- **`python-asyncio-aiohttp`** — async patterns (voice pipelines are async-heavy).
- **`simplify`** — review changed code for reuse and clarity before finishing.

### Code review
- **`superpowers:requesting-code-review`** before merging significant work.
- **`superpowers:receiving-code-review`** when responding to review feedback.

## Codebase exploration

For broad or open-ended searches, dispatch the **Explore** subagent.

## Conventions
- Tests are mandatory for voice agent code (enforced by `livekit-agents`).
- Verify before declaring done — `superpowers:verification-before-completion`.
- Don't commit API keys; load them from environment variables.
- Always use docstrings for all functions and classes.
- Simplicity over complexity — prefer straightforward implementations over complex ones.
