# SLNG Plugin for Pipecat

Pipecat is an open-source Python framework for building real-time voice and multimodal conversational agents.

## Documentation
To fetch documentations, if no default MCPs are provided, use context7.

### Pipecat
Use pipecat-docs MCP server if you need anything from Pipecat Side.

### Slang.ai
For STT/TTS models and configuration:
- General: https://docs.slng.ai/
- Unified API (What we actually leverage for STT and TTS integrations): https://docs.slng.ai/unified-api/overview
- Parameter coverage across models: https://docs.slng.ai/unified-api/parameters-coverage
- Unified STT (ws): https://docs.slng.ai/api-reference/unified-api/unmute-stt-bridge/unmute-stt-bridge-ws
- Unified TTS (ws): https://docs.slng.ai/api-reference/unified-api/unmute-tts-bridge/unmute-tts-bridge-ws

## Skills to use

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
- **`simplify`** — review changed code for reuse and clarity before finishing.

### Code review
- **`superpowers:requesting-code-review`** before merging significant work.
- **`superpowers:receiving-code-review`** when responding to review feedback.

## Codebase exploration
For broad or open-ended searches, dispatch the **Explore** subagent.

## Conventions
- Tests are mandatory for developments and go to live
- Verify before declaring done — `superpowers:verification-before-completion`.
- Don't commit API keys; load them from environment variables.
- Always use docstrings for all functions and classes.
