#!/usr/bin/env python3
"""
ClaudeClaw - Telegram to Claude Agent SDK Bridge
Uses ClaudeSDKClient for persistent per-user conversation sessions.
Includes OTP permission gate for tool approvals via Telegram.
"""
import os, sys, asyncio, logging, time, re, secrets, json
from pathlib import Path
from dataclasses import dataclass, field
from datetime import date, datetime
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode, ChatAction
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, HookMatcher
from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

logging.basicConfig(format="%(asctime)s [claudeclaw] %(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger("claudeclaw")


# ─── OTP Permission Gate ───────────────────────────────────────────────────

class PermissionGate:
    """
    OTP-based permission system using a SEPARATE Telegram bot.

    Architecture:
    - Main bot (the main bot) handles conversation
    - Permission bot (the permission bot) sends OTP challenges and receives approvals
    - This keeps permission codes out of the main chat

    When Claude wants to run a tool (Bash, Write, etc.), the gate:
    1. Generates a 6-digit code
    2. Sends it via the PERMISSION bot
    3. Waits for user to reply to the permission bot with the code
    4. Approves or denies the tool call

    Auto-approved tools (Read, Glob, Grep, WebSearch) skip the gate.
    """

    def __init__(self, perm_token=None):
        self.pending = {}       # user_id -> {code, tool_name, command, future}
        self.auto_allow = {     # Tools that never need approval
            "Read", "Glob", "Grep", "LS", "WebSearch", "WebFetch",
            "TodoRead", "TodoWrite", "AskUserQuestion",
            "ExitPlanMode", "BatchTool",
        }
        # Bash command prefixes that are auto-approved (no OTP needed)
        self.auto_allow_bash = {
            "openclaw-memo",
            "openclaw-custodian",
            "openclaw-session-ingest",
            "clawcrashcart",
            "memory-refresh",
            "docker",
            "docker-compose",
            "docker compose",
        }
        self.session_approved = {}  # user_id -> set of approved tool patterns
        self.task_approved = {}     # user_id -> task_id (approved for current task)
        self._current_task = {}     # user_id -> task_id (active task being processed)
        self._current_task_message = {}  # user_id -> original user message text
        self._perm_bot = None       # The permission bot instance (telegram.Bot)
        self._perm_app = None       # The permission bot Application (for polling)
        self._perm_token = perm_token
        self._main_bot = None       # Main bot (for sending status back to main chat)

    def set_main_bot(self, bot):
        """Set the main conversation bot (for optional status messages)."""
        self._main_bot = bot

    async def start_permission_bot(self):
        """Start the permission bot polling in the background."""
        if not self._perm_token:
            logger.warning("No PERMISSION_BOT_TOKEN set, OTP gate disabled")
            return

        from telegram.ext import Application as PermApp
        self._perm_app = PermApp.builder().token(self._perm_token).build()

        # Register handlers on the permission bot
        gate = self

        async def perm_callback_handler(update, context):
            """Handle inline button presses for task permission."""
            query = update.callback_query
            uid = query.from_user.id
            data = query.data  # "approve_once", "deny"

            if uid not in gate.pending:
                await query.answer("No pending request.")
                return

            pending = gate.pending[uid]

            if data == "deny":
                pending["future"].set_result((False, False))
                del gate.pending[uid]
                await query.answer("Denied")
                await query.edit_message_text(
                    query.message.text + "\n\n❌ Task Denied",
                    parse_mode=ParseMode.MARKDOWN
                )
            elif data == "approve_once":
                pending["future"].set_result((True, False))
                del gate.pending[uid]
                await query.answer("Task approved")
                await query.edit_message_text(
                    query.message.text + "\n\n✅ Task Approved",
                    parse_mode=ParseMode.MARKDOWN
                )

        async def perm_message_handler(update, context):
            uid = update.effective_user.id
            text = update.message.text
            if not text:
                return
            text = text.strip()

            # Handle /deny
            if text == "/deny":
                if uid in gate.pending:
                    gate.pending[uid]["future"].set_result((False, False))
                    del gate.pending[uid]
                    await update.message.reply_text("❌ Task denied.")
                else:
                    await update.message.reply_text("No pending task.")
                return

            # Handle /start on the permission bot
            if text == "/start":
                await update.message.reply_text(
                    "🔐 ClaudeClaw Permission Bot\n\n"
                    "I send you task approval requests when you message Claude.\n\n"
                    "Tap ✅ Approve Task to let Claude work, or ❌ Deny to stop.\n"
                    "You can also reply with the 6-digit code, or send /deny."
                )
                return

            # Check if it's an OTP code (fallback to text replies)
            if uid in gate.pending:
                pending = gate.pending[uid]
                code = text.split()[0]
                if code == pending["code"]:
                    pending["future"].set_result((True, False))
                    del gate.pending[uid]
                    await update.message.reply_text("✅ Task approved")
                    return
                else:
                    await update.message.reply_text("❌ Wrong code. Try again or /deny.")
                    return

            await update.message.reply_text("No pending task. Requests appear here when you message Claude.")

        self._perm_app.add_handler(CallbackQueryHandler(perm_callback_handler))
        self._perm_app.add_handler(MessageHandler(filters.TEXT, perm_message_handler))

        # Initialize and start polling
        await self._perm_app.initialize()
        await self._perm_app.start()
        await self._perm_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        self._perm_bot = self._perm_app.bot
        logger.info("  Permission bot started: @%s", (await self._perm_bot.get_me()).username)

    async def stop_permission_bot(self):
        """Stop the permission bot."""
        if self._perm_app:
            try:
                await self._perm_app.updater.stop()
                await self._perm_app.stop()
                await self._perm_app.shutdown()
            except Exception as e:
                logger.warning("Error stopping permission bot: %s", e)

    def is_auto_allowed(self, tool_name, tool_input):
        if tool_name in self.auto_allow:
            return True
        if tool_name == "Bash":
            cmd = tool_input.get("command", "").strip()
            for prefix in self.auto_allow_bash:
                if cmd.startswith(prefix):
                    return True
        return False

    def is_session_approved(self, user_id, tool_name, tool_input):
        if user_id not in self.session_approved:
            return False
        approved = self.session_approved[user_id]
        if tool_name in approved:
            return True
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            for pattern in approved:
                if pattern.startswith("Bash:") and cmd.startswith(pattern[5:]):
                    return True
        return False

    async def request_task_permission(self, user_id, task_id, task_message, tool_name, tool_input):
        """Send task-level permission request via the permission bot.
        
        First tool call for a task triggers the OTP. Shows the user's original
        message so they approve the task, not individual tools. All subsequent
        tool calls within the same task auto-approve.
        """
        # Already approved for this task
        if self.task_approved.get(user_id) == task_id:
            return True

        if not self._perm_bot:
            logger.warning("Permission bot not available, auto-denying")
            return False

        code = "%06d" % secrets.randbelow(1000000)

        # Show first tool as preview
        if tool_name == "Bash":
            cmd = tool_input.get("command", "???")
            first_tool = "First action: `%s`" % cmd[:200]
        elif tool_name in ("Write", "Edit", "MultiEdit"):
            fp = tool_input.get("file_path", tool_input.get("path", "???"))
            first_tool = "First action: %s `%s`" % (tool_name, fp)
        else:
            first_tool = "First action: %s" % tool_name

        # Truncate task message for display
        task_preview = task_message[:300]
        if len(task_message) > 300:
            task_preview += "..."

        msg = (
            "🔐 Task Permission\n\n"
            "Message:\n_%s_\n\n"
            "%s\n\n"
            "Code: `%s`\n"
            "⏱ Expires in 120s"
        ) % (task_preview, first_tool, code)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve Task", callback_data="approve_once"),
            ],
            [
                InlineKeyboardButton("❌ Deny", callback_data="deny"),
            ],
        ])

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self.pending[user_id] = {
            "code": code,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "task_id": task_id,
            "future": future,
        }

        try:
            await self._perm_bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        except Exception as e:
            logger.error("Failed to send task permission to user %d: %s", user_id, e)
            del self.pending[user_id]
            return False

        # Notify in main chat
        if self._main_bot:
            try:
                await self._main_bot.send_message(
                    chat_id=user_id,
                    text="⏳ Waiting for task approval in the permission bot..."
                )
            except:
                pass

        try:
            result = await asyncio.wait_for(future, timeout=120)
            approved, _ = result
            if approved:
                self.task_approved[user_id] = task_id
                logger.info("Task %s approved for user %d", task_id, user_id)
            return approved
        except asyncio.TimeoutError:
            logger.info("Task permission timeout for user %d", user_id)
            if user_id in self.pending:
                del self.pending[user_id]
            try:
                await self._perm_bot.send_message(chat_id=user_id, text="⏰ Task permission expired.")
            except:
                pass
            return False

    def clear_task(self, user_id):
        """Clear task-level approval (call after each message completes)."""
        self.task_approved.pop(user_id, None)
        self._current_task.pop(user_id, None)
        self._current_task_message.pop(user_id, None)

    def start_task(self, user_id, message_text):
        """Mark a new task starting (called before each query)."""
        import hashlib
        task_id = hashlib.md5(("%d:%s:%f" % (user_id, message_text, time.monotonic())).encode()).hexdigest()
        self._current_task[user_id] = task_id
        self._current_task_message[user_id] = message_text
        # Clear previous task approval — new message = new approval
        self.task_approved.pop(user_id, None)
        return task_id

    def add_session_approval(self, user_id, tool_name, tool_input):
        if user_id not in self.session_approved:
            self.session_approved[user_id] = set()
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            prefix = cmd.split()[0] if cmd.split() else cmd
            self.session_approved[user_id].add("Bash:" + prefix)
            logger.info("Session-approved Bash prefix '%s' for user %d", prefix, user_id)
        else:
            self.session_approved[user_id].add(tool_name)
            logger.info("Session-approved tool '%s' for user %d", tool_name, user_id)

    def clear_session(self, user_id):
        self.session_approved.pop(user_id, None)
        self.task_approved.pop(user_id, None)
        self.pending.pop(user_id, None)


# ─── Configuration ──────────────────────────────────────────────────────────

@dataclass
class Config:
    telegram_token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)
    working_dir: str = ""
    model: str = ""
    max_turns: int = 0
    permission_mode: str = "default"
    allowed_tools: list[str] = field(default_factory=list)
    system_prompt: str = ""
    use_project_settings: bool = True
    openclaw_path: str = ""
    crashcart_path: str = ""
    memory_files: list[str] = field(default_factory=lambda: [
        "soul.md", "identity.md", "USER.md", "MEMORY.md",
        "TOOLS.md", "HEARTBEAT.md", "AGENTS.md",
    ])
    include_daily_log: bool = True
    max_message_length: int = 4096
    require_permission: bool = True  # Enable OTP gate
    permission_bot_token: str = ""   # Separate bot for OTP challenges
    auto_allow_bash: list[str] = field(default_factory=lambda: [
        "openclaw-memo", "openclaw-custodian", "openclaw-session-ingest",
        "clawcrashcart", "memory-refresh", "docker", "docker-compose",
        "docker compose",
    ])
    benchmark_models: list[str] = field(default_factory=lambda: [
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
    ])

    @classmethod
    def from_env(cls):
        cfg = cls()
        cfg.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        cfg.permission_bot_token = os.environ.get("PERMISSION_BOT_TOKEN", "")
        raw_ids = os.environ.get("ALLOWED_USER_IDS", "")
        if raw_ids:
            cfg.allowed_user_ids = [int(x.strip()) for x in raw_ids.split(",") if x.strip()]
        cfg.working_dir = os.environ.get("CLAUDECLAW_WORKING_DIR", str(Path.home()))
        cfg.model = os.environ.get("CLAUDECLAW_MODEL", "")
        cfg.permission_mode = os.environ.get("CLAUDECLAW_PERMISSION_MODE", "default")
        cfg.system_prompt = os.environ.get("CLAUDECLAW_SYSTEM_PROMPT", "")
        cfg.use_project_settings = os.environ.get("CLAUDECLAW_USE_PROJECT_SETTINGS", "true").lower() == "true"
        cfg.openclaw_path = os.environ.get("OPENCLAW_PATH", "")
        cfg.crashcart_path = os.environ.get("CRASHCART_PATH", "")
        cfg.include_daily_log = os.environ.get("CLAUDECLAW_INCLUDE_DAILY_LOG", "true").lower() == "true"
        cfg.require_permission = os.environ.get("CLAUDECLAW_REQUIRE_PERMISSION", "true").lower() == "true"
        raw_bash = os.environ.get("CLAUDECLAW_AUTO_ALLOW_BASH", "")
        if raw_bash:
            cfg.auto_allow_bash = [b.strip() for b in raw_bash.split(",") if b.strip()]
        raw_tools = os.environ.get("CLAUDECLAW_ALLOWED_TOOLS", "")
        if raw_tools:
            cfg.allowed_tools = [t.strip() for t in raw_tools.split(",") if t.strip()]
        mt = os.environ.get("CLAUDECLAW_MAX_TURNS", "0")
        cfg.max_turns = int(mt) if mt else 0
        raw_bm = os.environ.get("CLAUDECLAW_BENCHMARK_MODELS", "")
        if raw_bm:
            cfg.benchmark_models = [m.strip() for m in raw_bm.split(",") if m.strip()]
        return cfg

    def validate(self):
        errors = []
        if not self.telegram_token: errors.append("TELEGRAM_BOT_TOKEN required")
        if not self.allowed_user_ids: errors.append("ALLOWED_USER_IDS required")
        return errors

def is_authorized(config, user_id):
    return user_id in config.allowed_user_ids


# ─── OpenClaw Memory Loader ────────────────────────────────────────────────

def load_openclaw_memory(config):
    search_paths = []
    if config.openclaw_path: search_paths.append(Path(config.openclaw_path))
    if config.crashcart_path: search_paths.append(Path(config.crashcart_path))
    search_paths.append(Path(config.working_dir))
    memory_dir = None
    for p in search_paths:
        if p.exists() and any((p / f).exists() for f in config.memory_files):
            memory_dir = p
            break
    if not memory_dir: return ""
    logger.info("Loading OpenClaw memory from: %s", memory_dir)
    sections, loaded = [], []
    for filename in config.memory_files:
        fp = memory_dir / filename
        if fp.exists():
            try:
                content = fp.read_text(encoding="utf-8").strip()
                if content:
                    label = fp.stem.upper()
                    sections.append("<%s>\n%s\n</%s>" % (label, content, label))
                    loaded.append(filename)
            except Exception as e:
                logger.warning("Failed to read %s: %s", fp, e)
    if config.include_daily_log:
        today_str = date.today().strftime("%Y-%m-%d")
        dlp = memory_dir / "memory" / (today_str + ".md")
        if dlp.exists():
            try:
                content = dlp.read_text(encoding="utf-8").strip()
                if content:
                    if len(content) > 4000: content = "...(truncated)\n" + content[-4000:]
                    sections.append("<DAILY_LOG>\n%s\n</DAILY_LOG>" % content)
                    loaded.append("memory/%s.md" % today_str)
            except Exception as e:
                logger.warning("Failed to read daily log: %s", e)
    if not sections: return ""
    logger.info("Loaded %d memory files: %s", len(loaded), ", ".join(loaded))
    header = "Honor soul.md constraints, adopt identity.md persona, use USER.md/MEMORY.md for personalization."
    return "<OPENCLAW_MEMORY>\n%s\n\n%s\n</OPENCLAW_MEMORY>" % (header, "\n\n".join(sections))


# ─── Session Manager ───────────────────────────────────────────────────────

class SessionManager:
    """Per-user ClaudeSDKClient sessions with OTP permission hooks."""
    def __init__(self, config, gate):
        self.config = config
        self.gate = gate
        self.sessions = {}
        self.locks = {}
        self._active_user = {}  # maps session -> user_id for hook context

    def _build_options(self, user_id):
        opts = {"cwd": self.config.working_dir}
        if self.config.model: opts["model"] = self.config.model
        if self.config.max_turns: opts["max_turns"] = self.config.max_turns
        # We handle permissions via Telegram hooks — tell SDK to accept all tools
        opts["permission_mode"] = "acceptEdits"
        if self.config.allowed_tools: opts["allowed_tools"] = self.config.allowed_tools

        sys_parts = []
        oc = load_openclaw_memory(self.config)
        if oc: sys_parts.append(oc)
        if self.config.system_prompt: sys_parts.append(self.config.system_prompt)
        if sys_parts: opts["system_prompt"] = "\n\n".join(sys_parts)
        if self.config.use_project_settings: opts["setting_sources"] = ["project"]

        # Add PreToolUse hook for OTP gate
        if self.config.require_permission:
            gate = self.gate
            uid = user_id

            async def permission_hook(input_data, tool_use_id, context):
                tool_name = input_data.get("tool_name", "")
                tool_input = input_data.get("tool_input", {})

                # Auto-allow safe tools
                if gate.is_auto_allowed(tool_name, tool_input):
                    return {}

                # Check session approvals
                if gate.is_session_approved(uid, tool_name, tool_input):
                    logger.info("Session-approved: %s for user %d", tool_name, uid)
                    return {}

                # Per-task approval: approved once per message, all tools pass
                task_id = gate._current_task.get(uid)
                task_msg = gate._current_task_message.get(uid, "")
                if task_id and gate.task_approved.get(uid) == task_id:
                    logger.info("Task-approved: %s for user %d (task %s)", tool_name, uid, task_id[:8])
                    return {}

                # Request task-level permission
                logger.info("Requesting task permission for user %d (first tool: %s)", uid, tool_name)
                approved = await gate.request_task_permission(uid, task_id, task_msg, tool_name, tool_input)

                if approved:
                    return {}  # Allow
                else:
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": "User denied task via Telegram",
                        }
                    }

            opts["hooks"] = {
                "PreToolUse": [
                    HookMatcher(matcher="*", hooks=[permission_hook]),
                ],
            }

        return ClaudeAgentOptions(**opts)

    async def get_or_create(self, user_id):
        if user_id not in self.sessions:
            logger.info("Creating new session for user %d", user_id)
            client = ClaudeSDKClient(self._build_options(user_id))
            await client.connect()
            self.sessions[user_id] = client
            self.locks[user_id] = asyncio.Lock()
        return self.sessions[user_id]

    async def query(self, user_id, prompt):
        if user_id not in self.locks:
            self.locks[user_id] = asyncio.Lock()
        async with self.locks[user_id]:
            try:
                client = await self.get_or_create(user_id)
                await client.query(prompt)
                text_parts, tool_log = [], []
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock): text_parts.append(block.text)
                            elif isinstance(block, ToolUseBlock): tool_log.append(block.name)
                parts = []
                if tool_log: parts.append("Tools: " + ", ".join(tool_log))
                if text_parts: parts.append("\n\n".join(text_parts))
                return "\n\n".join(parts) if parts else "No response generated."
            except Exception as e:
                logger.error("Session error user %d: %s", user_id, e, exc_info=True)
                await self.destroy(user_id)
                try:
                    client = await self.get_or_create(user_id)
                    await client.query(prompt)
                    text_parts = []
                    async for msg in client.receive_response():
                        if isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock): text_parts.append(block.text)
                    return "\n\n".join(text_parts) if text_parts else "No response (retry)."
                except Exception as e2:
                    return "Agent error: %s: %s" % (type(e2).__name__, e2)

    async def destroy(self, user_id):
        if user_id in self.sessions:
            try: await self.sessions[user_id].disconnect()
            except: pass
            del self.sessions[user_id]
            self.gate.clear_session(user_id)
            logger.info("Session destroyed for user %d", user_id)

    async def destroy_all(self):
        for uid in list(self.sessions.keys()):
            await self.destroy(uid)

    def info(self, user_id):
        active = "Active" if user_id in self.sessions else "No session"
        approved = self.gate.session_approved.get(user_id, set())
        if approved:
            active += " (%d approvals)" % len(approved)
        return active


# ─── Telegram Helpers ───────────────────────────────────────────────────────

def chunk_message(text, max_length=4096):
    if len(text) <= max_length: return [text]
    chunks = []
    while text:
        if len(text) <= max_length: chunks.append(text); break
        sp = text.rfind("\n", 0, max_length)
        if sp == -1 or sp < max_length // 2: sp = text.rfind(" ", 0, max_length)
        if sp == -1 or sp < max_length // 2: sp = max_length
        chunks.append(text[:sp]); text = text[sp:].lstrip()
    return chunks

async def keep_typing(update, interval=4.0):
    try:
        while True:
            await update.message.chat.send_action(ChatAction.TYPING)
            await asyncio.sleep(interval)
    except asyncio.CancelledError: pass


# ─── Telegram Command Handlers ─────────────────────────────────────────────

async def cmd_start(update, context):
    config = context.bot_data["config"]
    uid = update.effective_user.id
    if not is_authorized(config, uid):
        await update.message.reply_text("Unauthorized. Your user ID is: %d" % uid)
        return
    sessions = context.bot_data["sessions"]
    perm = "Per-task approval enabled" if config.require_permission else "No permission gate"
    text = (
        "ClaudeClaw Online\n\n"
        "Send any message - I maintain full conversation history.\n"
        "Each message triggers one approval for all tools needed.\n\n"
        "Workspace: %s\nModel: %s\nSession: %s\nPermissions: %s\n\n"
        "/new - Fresh conversation\n"
        "/status - Bot status\n"
        "/memory - Memory files\n"
        "/workspace - List files\n"
        "/whoami - Your ID"
    ) % (config.working_dir, config.model or "default", sessions.info(uid), perm)
    await update.message.reply_text(text)

async def cmd_new(update, context):
    config = context.bot_data["config"]
    uid = update.effective_user.id
    if not is_authorized(config, uid): return
    sessions = context.bot_data["sessions"]
    await sessions.destroy(uid)
    await update.message.reply_text("Conversation and approvals reset. Send a message to start fresh.")

async def cmd_approvals(update, context):
    """Show current session-approved tools."""
    config = context.bot_data["config"]
    uid = update.effective_user.id
    if not is_authorized(config, uid): return
    gate = context.bot_data["gate"]
    approved = gate.session_approved.get(uid, set())
    if not approved:
        await update.message.reply_text(
            "No session approvals.\n\n"
            "When Claude requests a tool, reply with `<code> always` to approve "
            "that tool/command for the rest of the session."
        )
        return
    lines = ["Session-approved tools:\n"]
    for item in sorted(approved):
        if item.startswith("Bash:"):
            lines.append("  Bash: %s*" % item[5:])
        else:
            lines.append("  %s" % item)
    lines.append("\nUse /new to reset all approvals.")
    await update.message.reply_text("\n".join(lines))

async def cmd_status(update, context):
    config = context.bot_data["config"]
    if not is_authorized(config, update.effective_user.id): return
    wd = Path(config.working_dir)
    has_claude_md = (wd / "CLAUDE.md").exists()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    auth_method = "API Key" if api_key else "Claude Code OAuth"
    sessions = context.bot_data["sessions"]
    gate = context.bot_data["gate"]
    perm_status = "OTP gate enabled" if config.require_permission else "Disabled"
    approved_count = len(gate.session_approved.get(update.effective_user.id, set()))
    mem_info = "No OpenClaw memory files found"
    for label, p in [("OpenClaw", config.openclaw_path), ("CrashCart", config.crashcart_path), ("Workspace", config.working_dir)]:
        if not p: continue
        pp = Path(p)
        if pp.exists():
            found = [f for f in config.memory_files if (pp / f).exists()]
            if found:
                mem_info = "Memory: %s (%s)\n  %s" % (label, p, ", ".join(found))
                break
    text = (
        "ClaudeClaw Status\n\nAuth: %s\nWorkspace: %s\nCLAUDE.md: %s\n"
        "Mode: %s\nModel: %s\nTools: %s\n"
        "Permissions: %s\nSession approvals: %d\n"
        "Session: %s\nActive sessions: %d\n\n%s"
    ) % (auth_method, config.working_dir, "Found" if has_claude_md else "Not found",
         config.permission_mode, config.model or "default",
         ", ".join(config.allowed_tools) or "defaults",
         perm_status, approved_count,
         sessions.info(update.effective_user.id), len(sessions.sessions), mem_info)
    await update.message.reply_text(text)

async def cmd_workspace(update, context):
    config = context.bot_data["config"]
    if not is_authorized(config, update.effective_user.id): return
    wd = Path(config.working_dir)
    try:
        items = sorted(wd.iterdir())
        dirs = [d.name + "/" for d in items if d.is_dir() and not d.name.startswith(".")]
        files = [f.name for f in items if f.is_file() and not f.name.startswith(".")]
        listing = "\n".join(dirs[:20] + files[:20])
    except Exception as e: listing = "Error: %s" % e
    await update.message.reply_text("Workspace: %s\n\n%s" % (config.working_dir, listing))

async def cmd_whoami(update, context):
    config = context.bot_data["config"]
    u = update.effective_user
    await update.message.reply_text("User ID: %d\nUsername: %s\nAuthorized: %s" % (
        u.id, u.username or "N/A", "Yes" if is_authorized(config, u.id) else "No"))

async def cmd_memory(update, context):
    config = context.bot_data["config"]
    if not is_authorized(config, update.effective_user.id): return
    oc = load_openclaw_memory(config)
    if not oc:
        await update.message.reply_text("No OpenClaw memory loaded.\nSet OPENCLAW_PATH or CRASHCART_PATH.")
        return
    section_names = [s for s in re.findall(r"<(\w+)>", oc) if s != "OPENCLAW_MEMORY"]
    summary = "OpenClaw Memory (~%d chars)\n\n" % len(oc)
    for name in section_names:
        m = re.search("<%s[^>]*>(.*?)</%s>" % (name, name), oc, re.DOTALL)
        if m:
            c = m.group(1).strip()
            preview = c[:100].replace("\n", " ")
            if len(c) > 100: preview += "..."
            summary += "%s (%d chars): %s\n\n" % (name, len(c), preview)
    for chunk in chunk_message(summary, config.max_message_length):
        await update.message.reply_text(chunk)


async def cmd_benchmark(update, context):
    """Run the same prompt against multiple Claude models in parallel and compare."""
    config = context.bot_data["config"]
    uid = update.effective_user.id
    if not is_authorized(config, uid):
        await update.message.reply_text("Unauthorized."); return

    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        model_list = "\n".join("- " + m for m in config.benchmark_models)
        await update.message.reply_text(
            "Usage: /benchmark <prompt>\n\n"
            "Runs your prompt against multiple models simultaneously.\n\n"
            "Models:\n%s\n\n"
            "Override with CLAUDECLAW_BENCHMARK_MODELS env var (comma-separated)." % model_list
        )
        return

    models = config.benchmark_models
    await update.message.reply_text(
        "Benchmarking %d models...\n%s" % (len(models), "\n".join("- " + m for m in models))
    )

    typing_task = asyncio.create_task(keep_typing(update))

    async def run_model(model_id):
        opts = ClaudeAgentOptions(
            cwd=config.working_dir,
            model=model_id,
            max_turns=1,
            permission_mode="acceptEdits",
        )
        t0 = time.monotonic()
        try:
            client = ClaudeSDKClient(opts)
            await client.connect()
            await client.query(prompt)
            text_parts = []
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
            await client.disconnect()
            elapsed = time.monotonic() - t0
            return model_id, "\n\n".join(text_parts) or "(no text response)", elapsed, None
        except Exception as e:
            elapsed = time.monotonic() - t0
            return model_id, None, elapsed, str(e)

    try:
        results = await asyncio.gather(*[run_model(m) for m in models])
    finally:
        typing_task.cancel()
        try: await typing_task
        except asyncio.CancelledError: pass

    for model_id, text, elapsed, err in results:
        if err:
            await update.message.reply_text(
                "--- %s (%.1fs) ---\nERROR: %s" % (model_id, elapsed, err)
            )
        else:
            header = "--- %s (%.1fs) ---\n\n" % (model_id, elapsed)
            for chunk in chunk_message(header + text, config.max_message_length):
                await update.message.reply_text(chunk)


# ─── Telegram Message Handlers ─────────────────────────────────────────────

async def handle_message(update, context):
    config = context.bot_data["config"]
    uid = update.effective_user.id
    if not is_authorized(config, uid):
        await update.message.reply_text("Unauthorized. ID: %d" % uid); return
    prompt = update.message.text
    if not prompt: return

    logger.info("User %d: %s", uid, prompt[:100])
    sessions = context.bot_data["sessions"]
    gate = context.bot_data["gate"]

    # Start a new task — clears previous task approval
    gate.start_task(uid, prompt)

    typing_task = asyncio.create_task(keep_typing(update))
    try:
        t0 = time.monotonic()
        response = await sessions.query(uid, prompt)
        elapsed = time.monotonic() - t0
        logger.info("Response in %.1fs (%d chars)", elapsed, len(response))
        full = response + "\n\n(%.1fs)" % elapsed
        for chunk in chunk_message(full, config.max_message_length):
            await update.message.reply_text(chunk)
    except Exception as e:
        logger.error("Error: %s", e, exc_info=True)
        await update.message.reply_text("Error: %s: %s" % (type(e).__name__, e))
    finally:
        gate.clear_task(uid)
        typing_task.cancel()
        try: await typing_task
        except asyncio.CancelledError: pass

async def handle_document(update, context):
    config = context.bot_data["config"]
    uid = update.effective_user.id
    if not is_authorized(config, uid): return
    doc = update.message.document
    if not doc: return
    f = await context.bot.get_file(doc.file_id)
    dest = Path(config.working_dir) / "uploads" / doc.file_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    await f.download_to_drive(str(dest))
    caption = update.message.caption or ""
    sessions = context.bot_data["sessions"]
    gate = context.bot_data["gate"]
    if caption:
        task_msg = "File uploaded to %s. %s" % (dest, caption)
        gate.start_task(uid, task_msg)
        await update.message.reply_text("File saved: %s\nProcessing..." % dest)
        typing_task = asyncio.create_task(keep_typing(update))
        try:
            resp = await sessions.query(uid, task_msg)
            for chunk in chunk_message(resp, config.max_message_length):
                await update.message.reply_text(chunk)
        finally:
            gate.clear_task(uid)
            typing_task.cancel()
    else:
        await update.message.reply_text("File saved: %s\nSend a message to tell me what to do with it." % dest)


# ─── Bot Setup ──────────────────────────────────────────────────────────────

async def post_init(app):
    # Start the permission bot in the background
    gate = app.bot_data["gate"]
    gate.set_main_bot(app.bot)
    await gate.start_permission_bot()
    await app.bot.set_my_commands([
        BotCommand("start", "Welcome and config"),
        BotCommand("status", "Bot and memory status"),
        BotCommand("memory", "Inspect memory files"),
        BotCommand("new", "Fresh conversation + approvals"),
        BotCommand("approvals", "View session-approved tools"),
        BotCommand("benchmark", "Compare models on a prompt"),
        BotCommand("workspace", "List files"),
        BotCommand("whoami", "Your Telegram ID"),
    ])

async def post_shutdown(app):
    gate = app.bot_data.get("gate")
    if gate: await gate.stop_permission_bot()
    sessions = app.bot_data.get("sessions")
    if sessions: await sessions.destroy_all()

def main():
    from dotenv import load_dotenv
    load_dotenv()
    config = Config.from_env()
    errors = config.validate()
    if errors:
        for e in errors: logger.error(e)
        print("\nTELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS required.")
        print("Auth: run `claude` to login (OAuth) or set ANTHROPIC_API_KEY")
        sys.exit(1)

    # ─── Permission Bot Setup Wizard ────────────────────────────────
    if config.require_permission and not config.permission_bot_token:
        print("\n" + "=" * 60)
        print("  🔐 ClaudeClaw Permission Bot Setup")
        print("=" * 60)
        print()
        print("ClaudeClaw uses a separate Telegram bot for tool approvals.")
        print("When Claude wants to run a command or edit a file, the")
        print("permission bot sends you Approve/Deny buttons.")
        print()
        print("To set this up:")
        print("  1. Open Telegram and message @BotFather")
        print("  2. Send /newbot")
        print("  3. Name it something like 'ClaudeClaw Permissions'")
        print("  4. Copy the bot token")
        print("  5. Message /start on your new bot (so it can DM you)")
        print()
        token = input("Paste your permission bot token (or Enter to skip): ").strip()
        if token:
            config.permission_bot_token = token
            # Save to .env for next time
            env_path = Path(config.working_dir) / ".env" if not Path(".env").exists() else Path(".env")
            # Check multiple .env locations
            for ep in [Path(".env"), Path(config.working_dir) / ".env"]:
                if ep.exists():
                    env_path = ep
                    break
            try:
                with open(env_path, "a") as f:
                    f.write("\nPERMISSION_BOT_TOKEN=%s\n" % token)
                print("\n✅ Token saved to %s" % env_path)
            except Exception as e:
                print("\n⚠️  Could not save to .env: %s" % e)
                print("Add manually: PERMISSION_BOT_TOKEN=%s" % token)
            print()
        else:
            print("\nSkipped. Permission gate disabled.")
            print("Claude will have unrestricted tool access.")
            print("To enable later, set PERMISSION_BOT_TOKEN in your .env")
            print()
            config.require_permission = False

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    logger.info("ClaudeClaw starting... (per-task permissions v2)")
    logger.info("  Auth: %s", "API Key" if api_key else "Claude Code OAuth")
    logger.info("  Workspace: %s", config.working_dir)
    logger.info("  Allowed users: %s", config.allowed_user_ids)
    logger.info("  Session mode: persistent (ClaudeSDKClient)")
    logger.info("  Permission gate: %s", "OTP via separate bot" if config.require_permission and config.permission_bot_token else "disabled")
    if config.openclaw_path: logger.info("  OpenClaw: %s", config.openclaw_path)
    if config.crashcart_path: logger.info("  CrashCart: %s", config.crashcart_path)
    mem = load_openclaw_memory(config)
    if mem: logger.info("  Memory loaded (%d chars)", len(mem))
    else: logger.warning("  No OpenClaw memory files found")

    gate = PermissionGate(perm_token=config.permission_bot_token if config.require_permission else None)
    gate.auto_allow_bash = set(config.auto_allow_bash)
    sessions = SessionManager(config, gate)

    app = Application.builder().token(config.telegram_token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.bot_data["config"] = config
    app.bot_data["sessions"] = sessions
    app.bot_data["gate"] = gate

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("approvals", cmd_approvals))
    app.add_handler(CommandHandler("workspace", cmd_workspace))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("benchmark", cmd_benchmark))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("ClaudeClaw live! Persistent sessions with OTP permission gate.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
