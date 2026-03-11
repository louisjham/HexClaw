"""
HexClaw — tg_bot.py
==================
Telegram bot hub for the HexClaw autonomous agent.

Responsibilities:
  • /recon <target>    → enqueue recon_osint skill chain
  • /status            → list running / queued jobs
  • /stats             → inference usage dashboard (0 tokens — SQL only)
  • /cancel <job_id>   → mark job cancelled
  • /help              → show all commands

  • Inline approval buttons  → 0-inference human-in-the-loop
  • Multi-choice keyboards   → let operator pick next action

Bot lifecycle:
  telegram.py is started inside daemon.py in a background asyncio task.
  It can also be run standalone: python telegram.py

Environment variables (from .env):
  TELEGRAM_BOT_TOKEN  — BotFather token
  TELEGRAM_CHAT_ID    — operator chat / group ID (whitelist)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

from dotenv import load_dotenv

# ── Optional runtime imports (graceful if deps not yet installed) ─────────────
try:
    from telegram import (
        Bot,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Update,
    )
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        ConversationHandler,
        filters,
        ContextTypes,
    )
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    # Define stubs to avoid NameErrors
    Bot = None
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None
    Update = None
    Application = None
    CallbackQueryHandler = None
    CommandHandler = None
    MessageHandler = None
    ConversationHandler = None
    filters = None
    ContextTypes = None

load_dotenv()

log = logging.getLogger("hexclaw.tg_bot")

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

from config import ROOT, TOKEN_LOG_DB
BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_ID: int | None = int(os.getenv("TELEGRAM_CHAT_ID", "0")) or None

# Filled in by daemon.py when it starts, so telegram.py can enqueue work
_enqueue_callback: Callable[[str, dict[str, Any]], Coroutine] | None = None
_status_callback: Callable[[], list[dict[str, Any]]] | None = None
_orchestrate_callback: Callable[[str], Coroutine] | None = None

# Pending approval gates: approval_id → asyncio.Future
_pending_approvals: dict[str, asyncio.Future] = {}

# Pending orchestration plans: approval_id → {skill, params, goal}
_pending_plans: dict[str, dict[str, Any]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Registration helpers (called by daemon.py at startup)
# ─────────────────────────────────────────────────────────────────────────────

def register_enqueue(fn: Callable) -> None:
    """Register the daemon's enqueue coroutine so /recon can submit jobs."""
    global _enqueue_callback
    _enqueue_callback = fn


def register_status(fn: Callable) -> None:
    """Register the daemon's status getter so /status can query jobs."""
    global _status_callback
    _status_callback = fn


def register_orchestrate(fn: Callable) -> None:
    """Register the daemon's orchestrate coroutine."""
    global _orchestrate_callback
    _orchestrate_callback = fn


# ─────────────────────────────────────────────────────────────────────────────
# Auth guard
# ─────────────────────────────────────────────────────────────────────────────

def _is_allowed(update: Update) -> bool:
    if ALLOWED_CHAT_ID is None:
        return True  # No restriction set
    chat_id = (
        update.effective_chat.id
        if update.effective_chat
        else update.effective_user.id
    )
    return chat_id == ALLOWED_CHAT_ID


async def _unauthorized(update: Update) -> None:
    if update.message:
        await update.effective_message.reply_text("⛔ Unauthorized.")
    log.warning("Unauthorized access attempt from chat %s", update.effective_chat)


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

# States for editing workflows
STATE_AWAIT_YAML, STATE_VALIDATE_WRITE = range(2)

async def cmd_help(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    if not _is_allowed(update):
        return await _unauthorized(update)

    text = (
        "📧 *Email Commands*\n"
        "`/email_sort <domain>` — Sort & label emails\n"
        "`/new_inbox <purpose>` — Create alias monitor\n"
        "`/reply <msg_id> <content>` — Draft a reply with approval\n"
        "\n"
        "📊 *Data Commands*\n"
        "`/status` — Queue + Data Summary\n"
        "`/data <query>` — Natural language SQL query\n"
        "`/stats` — Inference usage dashboard\n"
        "\n"
        "🛠 *Execution*\n"
        "`/orchestrate <goal>` — Plan and run goal\n"
        "`/skills [category]` — List available Agentic Skills\n"
        "`/recon <target>` — Full recon chain\n"
        "`/cidr_masscan <target>` — US IPv4 CIDR masscan and vulnerability check\n"
        "`/cancel <job_id>` — Cancel a job\n"
        "`/help` — Show help"
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown")


async def cmd_skills(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    if not _is_allowed(update):
        return await _unauthorized(update)

    args = context.args
    skills_cat = awesome_skills.get_skills_by_category()
    
    if not args:
        # Show Categories
        text = "🗂️ *Available Skill Bundles*\n\n"
        for cat, skills in sorted(skills_cat.items()):
            text += f"• `{cat}` ({len(skills)} skills)\n"
        text += "\n_Use `/skills <category>` to view specific commands._"
        # Truncate if somehow we still hit limits
        if len(text) > 4000:
            text = text[:4000] + "...\n[Truncated]"
        await update.effective_message.reply_text(text, parse_mode="Markdown")
        return

    # Show Specific Skills in Category
    cat_name = args[0].lower().strip()
    if cat_name not in skills_cat:
        await update.effective_message.reply_text(f"❌ Category `{cat_name}` not found.\nUse `/skills` to see all bundles.", parse_mode="Markdown")
        return

    text = f"🛠️ *Skills in bundle: {cat_name}*\n\n"
    for skill in sorted(skills_cat[cat_name], key=lambda x: x['name']):
        desc = skill['description'][:100] + "..." if len(skill['description']) > 100 else skill['description']
        # Escape markdown chars in description
        desc_safe = desc.replace('_', '\\_').replace('*', '\\*').replace('`', '\\`')
        text += f"• `@{(skill['name'])}` - {desc_safe}\n"
    
    # Telegram max message length is 4096. 
    # If the category has too many skills, we must split it.
    if len(text) <= 4000:
        await update.effective_message.reply_text(text, parse_mode="Markdown")
    else:
        # Quick split for very large categories
        parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for part in parts:
            await update.effective_message.reply_text(part, parse_mode="Markdown")


async def cmd_recon(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    if not _is_allowed(update):
        return await _unauthorized(update)

    args = context.args
    if not args:
        await update.effective_message.reply_text(
            "Usage: `/recon <target>`\nExample: `/recon example.com`",
            parse_mode="Markdown",
        )
        return

    target = args[0].strip()
    msg = await update.effective_message.reply_text(
        f"🔍 Queuing recon for `{target}`...", parse_mode="Markdown"
    )

    if _enqueue_callback is None:
        await msg.edit_text("❌ Daemon not connected. Is daemon.py running?")
        return

    try:
        job_id = await _enqueue_callback("recon_osint", {"target": target})
        await msg.edit_text(
            f"✅ Job `{job_id}` queued for *{target}*\n"
            f"Use /status to track progress.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        log.exception("Enqueue failed")
        await msg.edit_text(f"❌ Failed to enqueue: {exc}")


async def cmd_cidr_masscan(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    if not _is_allowed(update):
        return await _unauthorized(update)

    args = context.args
    if not args:
        await update.effective_message.reply_text(
            "Usage: `/cidr_masscan <target>`\nExample: `/cidr_masscan example.com`",
            parse_mode="Markdown",
        )
        return

    target = args[0].strip()
    msg = await update.effective_message.reply_text(
        f"🔍 Queuing US Masscan Vuln pipeline for `{target}`...", parse_mode="Markdown"
    )

    if _enqueue_callback is None:
        await msg.edit_text("❌ Daemon not connected. Is daemon.py running?")
        return

    try:
        job_id = await _enqueue_callback("us_masscan_vuln", {"target": target})
        await msg.edit_text(
            f"✅ Job `{job_id}` queued for *{target}*\n"
            f"Use /status to track progress.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        log.exception("Enqueue failed")
        await msg.edit_text(f"❌ Failed to enqueue: {exc}")


async def cmd_orchestrate(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    if not _is_allowed(update):
        return await _unauthorized(update)

    args = context.args
    if not args:
        await update.effective_message.reply_text(
            "Usage: `/orchestrate <goal>`\nExample: `/orchestrate \"scan vulnweb.com\"`",
            parse_mode="Markdown",
        )
        return

    goal = " ".join(args).strip()
    
    import planner
    import yaml
    import data
    
    # 1. Generate plan
    plan = planner.plan_goal(goal)
    skill = plan.get("skill", "agent_plan")
    params = plan.get("params", {})
    
    plan_text = f"🤖 *Goal:* {goal}\n\n"
    plan_text += f"🛠 *Planned Skill:* `{skill}`\n"
    plan_text += f"📦 *Parameters:*\n```yaml\n{yaml.dump(params)}```\n"
    
    # Add data-driven suggestions
    suggestions = data.suggest_next(skill)
    if suggestions:
        plan_text += "💡 *Suggestions:* " + ", ".join(suggestions) + "\n\n"
        
    plan_text += "Proceed with orchestration?"
    
    # 2. Ask for approval
    approval_id = f"orch:{os.urandom(4).hex()}"
    
    buttons = [
        [
            InlineKeyboardButton("🚀 Approve", callback_data=f"orch_ok:{approval_id}"),
            InlineKeyboardButton("🚫 Abort", callback_data=f"orch_no:{approval_id}"),
        ],
        [InlineKeyboardButton("🔍 Ports Only", callback_data=f"orch_ports:{approval_id}")]
    ]
    
    # Store the plan temporarily associated with this approval_id
    _pending_plans[approval_id] = {
        "skill": skill,
        "params": params,
        "goal": goal
    }
    
    await update.effective_message.reply_text(
        plan_text, 
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cmd_edit_start(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> int:
    if not _is_allowed(update):
        await _unauthorized(update)
        return ConversationHandler.END

    args = context.args
    if not args:
        await update.effective_message.reply_text("Usage: `/edit <workflow_name>`", parse_mode="Markdown")
        return ConversationHandler.END

    workflow = args[0].strip()
    skill_file = ROOT / "skills" / f"{workflow}.yaml"
    
    if not skill_file.exists():
        await update.effective_message.reply_text(f"❌ Skill file `{workflow}.yaml` not found.")
        return ConversationHandler.END
        
    try:
        content = skill_file.read_text()
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Error reading file: {e}")
        return ConversationHandler.END

    context.user_data['edit_file'] = str(skill_file)
    context.user_data['edit_workflow'] = workflow

    await update.effective_message.reply_text(
        f"📝 *YAML Editor*\nEditing `{workflow}.yaml`\n\nCurrent content:\n```yaml\n{content}\n```\n\nPaste updated YAML or /cancel",
        parse_mode="Markdown",
    )
    return STATE_AWAIT_YAML

async def cmd_edit_receive(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> int:
    new_yaml = update.message.text
    skill_file = Path(context.user_data.get('edit_file'))
    
    import yaml
    try:
        # Validate YAML
        yaml.safe_load(new_yaml)
    except yaml.YAMLError as e:
        await update.effective_message.reply_text(f"❌ YAML syntax error:\n```\n{e}\n```\n\nPlease fix and resend, or /cancel", parse_mode="Markdown")
        return STATE_AWAIT_YAML
        
    try:
        skill_file.write_text(new_yaml)
        await update.effective_message.reply_text("✅ Skill updated.")
        return ConversationHandler.END
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Error writing file: {e}")
        return ConversationHandler.END

async def cmd_edit_cancel(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> int:
    await update.effective_message.reply_text("❌ Edit cancelled.")
    return ConversationHandler.END


async def cmd_status(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    if not _is_allowed(update):
        return await _unauthorized(update)

    if _status_callback is None:
        await update.effective_message.reply_text("❌ Daemon not connected.")
        return

    try:
        import data
        
        # 1. Get raw jobs from callback
        jobs = _status_callback()
        
        # 2. Get rich summary from data.py (DuckDB)
        rich_summary = await data.get_summary_df()
        
        # Build message
        lines = ["📊 *System Status*"]
        
        lines.append("\n📋 *Active Queue:*")
        if not jobs:
            lines.append("  _No active jobs_")
        else:
            icons = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌"}
            for j in jobs[:5]:
                icon = icons.get(j['status'], "❓")
                lines.append(f"  {icon} `{j['id']}` {j['skill']} ({j['target']})")
        
        lines.append("\n📈 *Recent Activity (Analytics):*")
        lines.append(f"```\n{rich_summary}\n```")
        
        # 3. Summarised Usage (Short)
        try:
            import inference
            usage = inference.usage_report()
            total_cost = sum(v['total_cost'] for v in usage.values() if v.get('total_cost'))
            lines.append(f"\n💸 *Cost Usage:* `${total_cost:.4f}`")
        except:
            pass
            
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")
        
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ Error fetching status: {exc}")

async def cmd_email_sort(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    if not _is_allowed(update):
        return await _unauthorized(update)
    if not context.args:
        return await update.effective_message.reply_text("Usage: `/email_sort <domain>`")
    
    domain = context.args[0]
    msg = await update.effective_message.reply_text(f"📧 Sorting emails for `{domain}`...")
    
    try:
        from email.m365 import M365Engine
        engine = M365Engine()
        results = engine.classify_and_label(domain)
        if not results:
            await msg.edit_text(f"📭 No emails found for `{domain}`.")
        else:
            text = f"✅ Classified {len(results)} emails for `{domain}`:\n"
            for r in results:
                text += f"• `{r['id']}` → `{r['label']}`\n"
            await msg.edit_text(text)
    except Exception as e:
        await msg.edit_text(f"❌ Email sort failed: {e}")

async def cmd_new_inbox(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    if not _is_allowed(update):
        return await _unauthorized(update)
    if not context.args:
        return await update.effective_message.reply_text("Usage: `/new_inbox <purpose>`")
    
    purpose = context.args[0]
    try:
        from email.gmail import new_inbox
        alias = new_inbox(purpose)
        await update.effective_message.reply_text(f"🛰 *Alias Monitor Active*\nCreated tracking for: `{alias}`\nPurpose: _{purpose}_")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Failed to create monitor: {e}")

async def cmd_reply(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    if not _is_allowed(update):
        return await _unauthorized(update)
    if len(context.args) < 2:
        return await update.effective_message.reply_text("Usage: `/reply <msg_id> <content>`")
    
    msg_id = context.args[0]
    content = " ".join(context.args[1:])
    
    # 1. Ask for approval
    approval_id = f"reply:{os.urandom(4).hex()}"
    prompt = f"✉️ *Drafting Reply*\nTo message: `{msg_id}`\nContent: _{content}_\n\nApprove draft?"
    
    res = await request_approval(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        approval_id=approval_id,
        prompt=prompt
    )
    
    if res["action"] == "approve":
        try:
            from email.m365 import M365Engine
            engine = M365Engine()
            draft_id = engine.draft_reply(msg_id, content)
            await update.effective_message.reply_text(f"✅ Draft created! ID: `{draft_id}`")
        except Exception as e:
            await update.effective_message.reply_text(f"❌ Drafting failed: {e}")
    else:
        await update.effective_message.reply_text("🚫 Draft aborted.")

async def cmd_data(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    """Natural language data query command."""
    if not _is_allowed(update):
        return await _unauthorized(update)

    if not context.args:
        return await update.effective_message.reply_text("Usage: `/data <natural language query>`")

    prompt = " ".join(context.args)
    msg = await update.effective_message.reply_text(f"🔍 Querying: `{prompt}`...")

    try:
        import data
        df = await data.query(prompt)
        if df.empty:
            await msg.edit_text("📭 No results or query failed.")
        else:
            summary = df.to_markdown(index=False)
            # Cap summary for Telegram
            if len(summary) > 3000:
                summary = summary[:3000] + "\n\n...[truncated]"
            await msg.edit_text(f"📊 *Query Results*\n```\n{summary}\n```", parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Query error: {e}")


async def cmd_stats(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    """Zero-inference stats — reads SQLite token log directly."""
    if not _is_allowed(update):
        return await _unauthorized(update)

    import sqlite3

    db_path = TOKEN_LOG_DB
    if not db_path.exists():
        await update.effective_message.reply_text("📊 No token log found yet.")
        return

    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("""
            SELECT
                provider,
                model,
                COUNT(*)        AS calls,
                SUM(tokens_in)  AS tok_in,
                SUM(tokens_out) AS tok_out,
                SUM(cost)       AS cost
            FROM token_log
            GROUP BY provider, model
            ORDER BY cost DESC
        """).fetchall()

        totals = conn.execute("""
            SELECT
                COUNT(*)        AS calls,
                SUM(tokens_in)  AS tok_in,
                SUM(tokens_out) AS tok_out,
                SUM(cost)       AS cost
            FROM token_log
        """).fetchone()
        conn.close()
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ DB error: {exc}")
        return

    lines = ["📊 *Inference Usage Dashboard*\n"]
    for row in rows[:10]:
        provider, model, calls, tok_in, tok_out, cost = row
        cost_str = f"${cost:.4f}" if cost else "$0.0000"
        lines.append(
            f"• `{model}` ({provider})\n"
            f"  {calls} calls · {tok_in or 0}↑ {tok_out or 0}↓ tokens · "
            f"{cost_str}"
        )

    if totals:
        calls, tok_in, tok_out, cost = totals
        cost_str = f"${cost:.4f}" if cost else "$0.0000"
        lines.append(
            f"\n*Totals*: {calls} calls · "
            f"{tok_in or 0}↑ {tok_out or 0}↓ tokens · "
            f"{cost_str}"
        )

    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_cancel(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    if not _is_allowed(update):
        return await _unauthorized(update)

    args = context.args
    if not args:
        await update.effective_message.reply_text("Usage: `/cancel <job_id>`", parse_mode="Markdown")
        return

    job_id = args[0].strip()
    # Signal daemon via the status callback's cancel mechanism (set job cancelled in DB)
    # For now, we emit a sentinel to the pending approvals map
    future = _pending_approvals.get(f"cancel:{job_id}")
    if future and not future.done():
        future.set_result({"action": "cancel"})
        await update.effective_message.reply_text(f"🚫 Cancellation signal sent for `{job_id}`.", parse_mode="Markdown")
    else:
        # Emit via global cancellation set (daemon polls this)
        _cancelled_jobs.add(job_id)
        await update.effective_message.reply_text(
            f"🚫 Job `{job_id}` marked for cancellation.", parse_mode="Markdown"
        )


# Global set that daemon.py polls to cancel jobs
_cancelled_jobs: set[str] = set()


def is_cancelled(job_id: str) -> bool:
    return job_id in _cancelled_jobs


def clear_cancel(job_id: str) -> None:
    _cancelled_jobs.discard(job_id)


# ─────────────────────────────────────────────────────────────────────────────
# Inline approval gates (human-in-the-loop, 0 inference)
# ─────────────────────────────────────────────────────────────────────────────

async def request_approval(
    bot: "Bot",
    chat_id: int,
    approval_id: str,
    prompt: str,
    choices: list[str] | None = None,
    timeout_sec: int = 300,
) -> dict[str, Any]:
    """
    Send an inline keyboard to the operator and wait for a response.

    Args:
        bot:         Telegram Bot instance
        chat_id:     Operator chat ID
        approval_id: Unique ID for this approval gate
        prompt:      Message text shown to operator
        choices:     If given, show these as multi-choice buttons.
                     If None, shows [✅ Approve] [❌ Deny] only.
        timeout_sec: How long to wait before auto-denying

    Returns:
        dict with keys: action (str), choice (str|None)
    """
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    _pending_approvals[approval_id] = future

    if choices:
        buttons = [
            [InlineKeyboardButton(c, callback_data=f"choice:{approval_id}:{c}")]
            for c in choices
        ]
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"deny:{approval_id}")])
    else:
        buttons = [[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{approval_id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"deny:{approval_id}"),
        ]]

    keyboard = InlineKeyboardMarkup(buttons)
    await bot.send_message(
        chat_id=chat_id,
        text=prompt,
        reply_markup=keyboard,
        parse_mode="Markdown",
    )

    try:
        result = await asyncio.wait_for(future, timeout=timeout_sec)
    except asyncio.TimeoutError:
        result = {"action": "timeout", "choice": None}
        log.warning("Approval gate %s timed out after %ss", approval_id, timeout_sec)
    finally:
        _pending_approvals.pop(approval_id, None)

    return result



async def _handle_callback(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    """Resolve pending approval futures when operator presses an inline button."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data: str = query.data or ""
    parts = data.split(":", 2)

    if not parts:
        return

    action = parts[0]

    if action == "orch_ok" and len(parts) >= 2:
        approval_id = parts[1]
        plan = _pending_plans.pop(approval_id, None)
        if plan and _enqueue_callback:
            try:
                job_id = await _enqueue_callback(plan['skill'], plan['params'])
                await query.edit_message_text(f"🚀 *Orchestration Approved*\nEnqueued Job `{job_id}`", parse_mode="Markdown")
            except Exception as e:
                await query.edit_message_text(f"❌ Enqueue failed: {e}")

    elif action == "orch_no" and len(parts) >= 2:
        approval_id = parts[1]
        _pending_plans.pop(approval_id, None)
        await query.edit_message_text("🚫 *Orchestration Aborted*", parse_mode="Markdown")

    elif action == "orch_ports" and len(parts) >= 2:
        approval_id = parts[1]
        plan = _pending_plans.pop(approval_id, None)
        if plan and _enqueue_callback:
            params = plan['params'].copy()
            params['ports_only'] = True
            try:
                job_id = await _enqueue_callback(plan['skill'], params)
                await query.edit_message_text(f"🔍 *Port Scan Only Approved*\nEnqueued Job `{job_id}`", parse_mode="Markdown")
            except Exception as e:
                await query.edit_message_text(f"❌ Enqueue failed: {e}")

    elif action == "approve" and len(parts) >= 2:
        approval_id = parts[1]
        future = _pending_approvals.get(approval_id)
        if future and not future.done():
            future.set_result({"action": "approve", "choice": None})
        await query.edit_message_text("✅ Approved.")

    elif action == "deny" and len(parts) >= 2:
        approval_id = parts[1]
        future = _pending_approvals.get(approval_id)
        if future and not future.done():
            future.set_result({"action": "deny", "choice": None})
        await query.edit_message_text("❌ Denied.")

    elif action == "choice" and len(parts) >= 3:
        approval_id = parts[1]
        choice = parts[2]
        future = _pending_approvals.get(approval_id)
        if future and not future.done():
            future.set_result({"action": "choice", "choice": choice})
        await query.edit_message_text(f"✅ Selected: *{choice}*", parse_mode="Markdown")

    else:
        log.warning("Unknown callback data: %s", data)


# ─────────────────────────────────────────────────────────────────────────────
# Outbound notification helpers (called by daemon.py)
# ─────────────────────────────────────────────────────────────────────────────

class Notifier:
    """
    Simple async helper for daemon.py to send messages without holding
    a full Application reference.

    Usage:
        notifier = Notifier(bot_token, chat_id)
        await notifier.send("💥 Scan complete!")
        await notifier.send_report(job_id, target, findings)
    """

    def __init__(self, token: str, chat_id: int) -> None:
        self._token = token
        self._chat_id = chat_id
        self._bot: "Bot | None" = None

    def _get_bot(self) -> Any:
        if self._bot is None:
            if not TELEGRAM_AVAILABLE:
                raise ImportError("python-telegram-bot not installed")
            self._bot = Bot(token=self._token)
        return self._bot

    async def send(self, text: str, parse_mode: str = "Markdown", **kwargs) -> None:
        try:
            await self._get_bot().send_message(
                chat_id=self._chat_id,
                text=text[:4096],  # Telegram max
                parse_mode=parse_mode,
                **kwargs
            )
        except Exception as exc:
            log.error("Telegram send failed: %s", exc)

    async def send_file(self, file_path: str, caption: str | None = None) -> None:
        """Send a document/file to the operator."""
        try:
            path = Path(file_path)
            if not path.exists():
                log.error(f"File not found for Telegram: {file_path}")
                return
            with open(path, "rb") as fh:
                await self._get_bot().send_document(
                    chat_id=self._chat_id,
                    document=fh,
                    caption=caption[:1024] if caption else None,
                )
        except Exception as exc:
            log.error(f"Telegram file send failed: {exc}")

    async def send_report(
        self,
        job_id: str,
        target: str,
        skill: str,
        findings: list[dict[str, Any]],
        top_findings: list[str] | None = None,
        next_steps: list[str] | None = None,
    ) -> None:
        """
        Send a structured scan report with optional suggest_next buttons.
        0 inference — all content comes from structured scan output.
        """
        sev_counts: dict[str, int] = {}
        for f in findings:
            sev = f.get("severity", "info").lower()
            sev_counts[sev] = sev_counts.get(sev, 0) + 1

        sev_line = " · ".join(
            f"{v} {k}" for k, v in sorted(sev_counts.items(), key=lambda x: ["critical","high","medium","low","info"].index(x[0]) if x[0] in ["critical","high","medium","low","info"] else 99)
        ) or "no findings"

        lines = [
            f"🎯 *HexClaw Report* — `{target}`",
            f"Skill: `{skill}` · Job: `{job_id}`",
            f"Severity: {sev_line}",
            f"Total findings: {len(findings)}",
        ]

        if top_findings:
            lines.append("\n🔝 *Top Findings*")
            for tf in top_findings[:5]:
                lines.append(f"  • {tf}")

        if next_steps:
            lines.append("\n⏭ *Suggested Next Steps*")
            for step in next_steps[:3]:
                lines.append(f"  → {step}")

        await self.send("\n".join(lines))

    async def send_alert(
        self,
        source: str,
        title: str,
        url: str | None = None,
        severity: str = "unknown",
    ) -> None:
        """Send a CVE / RSS alert notification."""
        sev_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(
            severity.lower(), "⚪"
        )
        lines = [
            f"{sev_icon} *{source.upper()} Alert*",
            f"*{title}*",
        ]
        if url:
            lines.append(f"[Read more]({url})")
        await self.send("\n".join(lines))

    async def request_approval(
        self,
        approval_id: str,
        prompt: str,
        choices: list[str] | None = None,
        timeout_sec: int = 300,
    ) -> dict[str, Any]:
        """Delegate to module-level request_approval."""
        return await request_approval(
            bot=self._get_bot(),
            chat_id=self._chat_id,
            approval_id=approval_id,
            prompt=prompt,
            choices=choices,
            timeout_sec=timeout_sec,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Application factory
# ─────────────────────────────────────────────────────────────────────────────

def build_application() -> "Application":
    """Build and return the configured PTB Application (not started yet)."""
    if not TELEGRAM_AVAILABLE:
        raise ImportError("python-telegram-bot not installed. Run: pip install 'python-telegram-bot>=21.0'")

    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set in .env")

    from telegram.request import HTTPXRequest
    # Use a larger connection pool — the default (1 slot) exhausts quickly
    # when the tg_log background thread and the main updater compete.
    request = HTTPXRequest(connection_pool_size=8, pool_timeout=30.0)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .build()
    )

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("orchestrate", cmd_orchestrate))
    
    # Inline YAML Editor
    edit_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("edit", cmd_edit_start)],
        states={
            STATE_AWAIT_YAML: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), cmd_edit_receive)
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_edit_cancel)],
    )
    app.add_handler(edit_conv_handler)
    
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("recon", cmd_recon))
    app.add_handler(CommandHandler("cidr_masscan", cmd_cidr_masscan))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("data", cmd_data))
    app.add_handler(CommandHandler("email_sort", cmd_email_sort))
    app.add_handler(CommandHandler("new_inbox", cmd_new_inbox))
    app.add_handler(CommandHandler("reply", cmd_reply))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(_handle_callback))

    return app


async def run_bot_async() -> None:
    """
    Run the bot using long-polling inside the current event loop.
    Called from daemon.py as an asyncio task.
    """
    if not TELEGRAM_AVAILABLE:
        log.error("python-telegram-bot not installed — Telegram bot disabled")
        return

    if not BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled")
        return

    app = build_application()
    log.info("Starting Telegram bot (long-polling)...")

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot is running. Waiting for messages...")
        # Keep alive — daemon.py cancels this task on shutdown
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await app.updater.stop()
            await app.stop()
    log.info("Telegram bot stopped.")


# ─────────────────────────────────────────────────────────────────────────────
# Standalone entry-point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    log.info("Running telegram.py standalone...")

    if not TELEGRAM_AVAILABLE:
        print("ERROR: python-telegram-bot not installed.")
        print("Run:   pip install 'python-telegram-bot>=21.0'")
        raise SystemExit(1)

    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        raise SystemExit(1)

    asyncio.run(run_bot_async())
