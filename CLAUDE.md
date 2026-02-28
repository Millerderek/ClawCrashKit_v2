# ClaudeClaw

Telegram-to-Claude Agent SDK bridge. Single-file Python bot (~400 LOC).

## Architecture
- `claudeclaw.py` — entire bot, no framework beyond python-telegram-bot + claude-agent-sdk
- Uses `ClaudeSDKClient` for persistent per-user conversation sessions (not one-shot `query()`)
- Polling-based (no inbound ports needed)
- Auth: Claude Code OAuth (subscription) preferred, API key fallback
- OpenClaw memory files (soul.md, identity.md, etc.) loaded as system prompt context

## Key patterns
- `Config.from_env()` reads all config from env vars / .env
- `PermissionGate` — OTP-based tool approval via Telegram (6-digit codes, session memory)
- `SessionManager` — per-user `ClaudeSDKClient` instances with asyncio locks, PreToolUse hooks
- `load_openclaw_memory()` searches OPENCLAW_PATH -> CRASHCART_PATH -> working dir
- `chunk_message()` splits responses for Telegram's 4096 char limit
- `keep_typing()` sends typing indicator while waiting for Claude
- `/new` destroys session AND clears session-approved tools
- `/approvals` shows what tools/commands are approved for the session

## Testing
- `python3 -c "import ast; ast.parse(open('claudeclaw.py').read()); print('OK')"`
- Send `/start` to your bot on Telegram after launching
- Test multi-turn memory: tell it your name, then ask "what's my name?"
