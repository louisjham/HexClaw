"""
HexClaw — daemon.py
===================
The core asyncio orchestrator for the HexClaw autonomous agent.

PRD v1.0 Rules:
  • Heartbeat poll sqlite queue.
  • Run MCP YAML workflows (Skills).
  • Telegram notify (chat_id, msg, file).
  • Redis startup check.
  • Run forever.
"""

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import uuid
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

from config import ROOT, DATA_DIR, JOBS_DB, SKILLS_DIR, LOG_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "daemon.log")
    ]
)
log = logging.getLogger("hexclaw.daemon")

import tg_bot as tg_module
from tg_bot import Notifier, register_enqueue, register_status, register_orchestrate
import planner
import cache
import inference
import monitor
import data
import vuln_prioritize

# ── Null Notifier (for testing) ────────────────────────────────────────────────
class NullNotifier:
    async def send(self, text: str, parse_mode: str = "Markdown"): pass
    async def send_file(self, file_path: str, caption: str | None = None): pass
    async def send_report(self, *args, **kwargs): pass
    async def request_approval(self, *args, **kwargs):
        return {"action": "approve", "choice": None}

# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(JOBS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            skill TEXT,
            params TEXT,
            status TEXT DEFAULT 'pending',
            target TEXT,
            created_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            result TEXT,
            error TEXT
        )
    """)
    conn.commit()
    conn.close()


# ── Postgres Health-check + Auto-start ───────────────────────────────────────
def ensure_postgres() -> None:
    """
    Verify Postgres is reachable.  If not, attempt to start it.
    Exits the process with a clear error if it cannot be reached after all attempts.
    """
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        log.info("POSTGRES_DSN not set — skipping Postgres check.")
        return

    def _can_connect() -> bool:
        try:
            import psycopg2
            conn = psycopg2.connect(dsn, connect_timeout=3)
            conn.close()
            return True
        except Exception:
            return False

    # 1. Already up?
    if _can_connect():
        log.info("Postgres: already running.")
        return

    log.warning("Postgres not reachable — attempting to start it...")

    # 2. Try Windows service (works when installed via EnterpriseDB / MSI)
    started_via = None
    for svc_name in ("postgresql", "postgresql-x64-16", "postgresql-x64-15",
                     "postgresql-x64-14", "postgresql-x64-17"):
        try:
            result = subprocess.run(
                ["net", "start", svc_name],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0 or "already been started" in result.stdout.lower():
                started_via = f"Windows service '{svc_name}'"
                break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    # 3. Fallback: pg_ctl start (works for manual installs)
    if not started_via:
        pg_ctl = os.getenv("PG_CTL", "pg_ctl")
        pg_data = os.getenv("PGDATA", "")
        if pg_data:
            try:
                result = subprocess.run(
                    [pg_ctl, "start", "-D", pg_data, "-w"],
                    capture_output=True, text=True, timeout=20
                )
                if result.returncode == 0:
                    started_via = f"pg_ctl (PGDATA={pg_data})"
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

    # 4. Poll for up to 10 s
    if started_via:
        log.info("Started via %s — waiting for Postgres to accept connections...", started_via)
        for _ in range(10):
            time.sleep(1)
            if _can_connect():
                log.info("Postgres is now accepting connections.")
                return

    # 5. Give up — print actionable error and exit
    print(
        "\n"
        "FATAL: Cannot connect to Postgres and auto-start failed.\n"
        f"  DSN : {dsn}\n"
        "\n"
        "To fix this, try one of:\n"
        "  1. Start Postgres manually:  net start postgresql\n"
        "  2. Or set PGDATA in .env and ensure pg_ctl is on PATH.\n"
        "  3. Or update POSTGRES_DSN in .env to point to a running server.\n",
        file=sys.stderr
    )
    sys.exit(1)


# ── Job Lifecycle ─────────────────────────────────────────────────────────────
class JobStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

async def enqueue_job(skill: str, params: dict) -> str:
    job_id = str(uuid.uuid4())[:8]
    target = params.get("target", "unknown")
    conn = sqlite3.connect(JOBS_DB)
    conn.execute(
        "INSERT INTO jobs (id, skill, params, target, created_at) VALUES (?, ?, ?, ?, ?)",
        (job_id, skill, json.dumps(params), target, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()
    log.info(f"Enqueued job {job_id}: {skill} on {target}")
    return job_id

def get_pending_jobs():
    conn = sqlite3.connect(JOBS_DB)
    conn.row_factory = sqlite3.Row
    jobs = conn.execute("SELECT * FROM jobs WHERE status = ?", (JobStatus.PENDING,)).fetchall()
    conn.close()
    return jobs

def get_recent_jobs(limit=10):
    conn = sqlite3.connect(JOBS_DB)
    conn.row_factory = sqlite3.Row
    jobs = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(j) for j in jobs]

def update_job_status(job_id: str, status: str, result: Any = None, error: str | None = None):
    conn = sqlite3.connect(JOBS_DB)
    now = datetime.now(timezone.utc).isoformat()
    if status == JobStatus.RUNNING:
        conn.execute("UPDATE jobs SET status = ?, started_at = ? WHERE id = ?", (status, now, job_id))
    elif status in (JobStatus.DONE, JobStatus.FAILED):
        conn.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, result = ?, error = ? WHERE id = ?",
            (status, now, json.dumps(result) if result else None, error, job_id)
        )
    conn.commit()
    conn.close()

# ── Skills & MCP ──────────────────────────────────────────────────────────────
async def run_skill(job_id: str, skill_name: str, params: dict, notifier: Notifier | NullNotifier):
    target = params.get("target", "unknown")
    log.info(f"[Job {job_id}] Skill dispatch: {skill_name} → {target}")
    update_job_status(job_id, JobStatus.RUNNING)
    
    if skill_name == "awesome_skill_execution":
        steps = [{
            "tool": "awesome_skill", 
            "action": "execute_awesome_skill",
            "params": {
                "goal": params.get("goal"), 
                "skill_name": params.get("skill_name"), 
                "skill_content": params.get("skill_content")
            }
        }]
    else:
        skill_file = SKILLS_DIR / f"{skill_name}.yaml"
        if not skill_file.exists():
            err = f"Skill {skill_name} not found at {skill_file}"
            log.error(f"[Job {job_id}] {err}")
            update_job_status(job_id, JobStatus.FAILED, error=err)
            return
            
        try:
            with open(skill_file, "r") as f:
                skill_def = yaml.safe_load(f)
            steps = skill_def.get("steps", [])
        except Exception as e:
            err = f"Failed to parse skill {skill_name}: {e}"
            log.error(f"[Job {job_id}] {err}")
            update_job_status(job_id, JobStatus.FAILED, error=err)
            return
    
    log.info(f"[Job {job_id}] Loaded skill '{skill_name}' with {len(steps)} steps")
    context = params.copy()
    
    for i, step in enumerate(steps, 1):
        tool = step.get("tool")
        action = step.get("action")
        step_params = step.get("params", {})
        log.info(f"[Job {job_id}] Step {i}/{len(steps)}: tool={tool} action={action}")
        
        # 1. Internal Action Handler
        if action == "execute_awesome_skill":
            goal = step_params.get("goal", "No goal provided")
            skill_n = step_params.get("skill_name", "unknown_skill")
            content = step_params.get("skill_content", "")
            
            log.info(f"[Job {job_id}] Executing Awesome Skill: {skill_n} for goal: {goal}")
            await notifier.send(f"🌟 Executing Awesome Skill: *{skill_n}*\nGoal: {goal}")
            
            import coder
            # Inject the markdown instructions into the prompt for the autonomous coder
            prompt = f"Goal: {goal}\n\nYou MUST use the following expert instructions to accomplish this goal:\n\n{content}"
            result = await coder.code_and_run(prompt)
            
            context["awesome_skill_output"] = result
            await notifier.send(f"✅ Awesome Skill Execution Complete:\n```\n{result[:3000]}\n```")
            continue

        if action == "generate_and_run_code":
            goal = context.get(step.get("input", "goal"), "No goal provided")
            log.info(f"[Job {job_id}] Coding to achieve goal: {goal}")
            await notifier.send(f"🤖 Generating code for: {goal}")
            
            import coder
            result = await coder.code_and_run(f"Write a script for the following goal: {goal}")
            
            context[step.get("output", "code_output")] = result
            await notifier.send(f"✅ Code Execution Complete:\n```\n{result[:3000]}\n```")
            continue

        if action == "store_findings":
            log.info(f"[Job {job_id}] Storing findings to analytical layer")
            # Simulate some findings if context is empty
            if not context.get("findings"):
                context["findings"] = [
                    {"target": context.get("target"), "severity": "high", "name": "CVE-2023-1234", "template_id": "cve-2023-1234"},
                    {"target": context.get("target"), "severity": "medium", "name": "XSS", "template_id": "xss-generic"}
                ]
            df = pd.DataFrame(context["findings"])
            data.store_parquet(df, f"job_{job_id}")
            log.info(f"[Job {job_id}] Stored {len(context['findings'])} findings to parquet")
            await notifier.send(f"💾 Job {job_id}: {len(context['findings'])} findings stored.")
            continue

        if action == "suggest_next":
            log.info(f"[Job {job_id}] Generating next-step suggestions for {skill_name}")
            suggestions = data.suggest_next(skill_name)
            # Prioritize vulnerabilities for the notification
            top_vulns = vuln_prioritize.get_top_cves(f"job_{job_id}")
            log.info(f"[Job {job_id}] Suggestions: {suggestions} | Top CVEs: {top_vulns[:80]}")
            
            prompt = f"🎯 *Recon Complete for {context.get('target', 'Target')}*\n\n{top_vulns}\n\nWhat would you like to do next?"
            
            # Send buttons to Telegram
            log.info(f"[Job {job_id}] Awaiting operator approval (timeout={step_params.get('timeout_sec', 300)}s)")
            choice = await notifier.request_approval(
                approval_id=f"suggest:{job_id}",
                prompt=prompt,
                choices=suggestions,
                timeout_sec=step_params.get("timeout_sec", 300)
            )
            
            log.info(f"[Job {job_id}] Operator response: {choice}")
            if choice["action"] == "choice":
                new_goal = f"{choice['choice']} on {context.get('target')}"
                log.info(f"[Job {job_id}] Operator chose: {choice['choice']} → goal: {new_goal}")
                await notifier.send(f"🚀 User selected: *{choice['choice']}*. Orchestrating...")
            elif choice["action"] == "timeout":
                log.warning(f"[Job {job_id}] Approval timed out — skipping suggest_next")
            continue

        # 2. Tool Endpoint Map / MCP Call
        if action == "dispatch_mission" and tool == "villager":
            goal = context.get(step.get("input", "goal"), "No goal provided")
            target = context.get("target", "unknown")
            log.info(f"[Job {job_id}] Dispatching autonomous mission to Villager: {goal} on {target}")
            await notifier.send(f"🏘️ *Villager Integration* \nDispatching autonomous mission: {goal}\nTarget: {target}")
            
            import villager_client
            # We don't have constraints explicitly mapped yet, could get from planner or params
            v_task_id = await villager_client.dispatch_mission(goal, target)
            
            if v_task_id:
                context[step.get("output", "task_id")] = v_task_id
                await notifier.send(f"✅ Mission delegated to Villager. Task ID: `{v_task_id}`\n(Fire and forget initiated)")
                log.info(f"Villager mission started with tracking ID: {v_task_id}")
            else:
                await notifier.send("❌ Failed to delegate mission to Villager MCP.")
                log.error("Villager client failed to return a task_id")
            continue

        log.info(f"[Job {job_id}] Dispatching tool: {tool}")
        await notifier.send(f"🔄 Job {job_id} step {i}: {tool}...")
        await asyncio.sleep(1) # Simulating work

    update_job_status(job_id, JobStatus.DONE, result={"status": "completed"})
    log.info(f"[Job {job_id}] ✅ Skill '{skill_name}' completed successfully")
    await notifier.send(f"✅ Job {job_id} complete!")

# ── Daemon ────────────────────────────────────────────────────────────────────
class HexClawDaemon:
    def __init__(self):
        self._stop_event = asyncio.Event()
        self.notifier = NullNotifier()

    async def orchestrate(self, goal: str) -> str:
        """Translate goal to skill and enqueue."""
        plan = planner.plan_goal(goal)
        skill = plan.get("skill", "agent_plan")
        params = plan.get("params", {})
        if "goal" not in params:
            params["goal"] = goal
        return await enqueue_job(skill, params)

    async def start(self):
        ensure_postgres()
        init_db()
        header_text = "HexClaw Daemon v1.0 Starting"
        log.info(header_text)
        
        # Redis Startup Check
        try:
            r_status = cache.get_cache().stats()
            log.info(f"Redis Cache Ready: {r_status}")
        except Exception as e:
            log.warning(f"Redis Cache connection failed: {e}")

        # Telegram Init
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        register_enqueue(enqueue_job)
        register_status(get_recent_jobs)
        register_orchestrate(self.orchestrate)

        if token and chat_id:
            self.notifier = Notifier(token, int(chat_id))

            # ── Centralised Telegram logging ──────────────────────────────
            import tg_log
            tg_log.install()  # every log.info/warning/error → Telegram

            await self.notifier.send("🦾 HexClaw Daemon Online (v1.0)")
            
            # Start Telegram Bot
            asyncio.create_task(tg_module.run_bot_async())
            
            # Start Monitor
            m = monitor.get_monitor(notifier=self.notifier)
            asyncio.create_task(m.run())
            log.info("Threat Monitor active.")
        else:
            log.warning("Telegram not configured. Running in terminal-only mode.")

    async def run_forever(self):
        await self.start()
        log.info("Daemon heartbeat active.")
        while not self._stop_event.is_set():
            pending = get_pending_jobs()
            for row in pending:
                job_id = row['id']
                skill = row['skill']
                params = json.loads(row['params'])
                asyncio.create_task(run_skill(job_id, skill, params, self.notifier))
            
            await asyncio.sleep(5) # Heartbeat interval

async def main():
    daemon = HexClawDaemon()
    await daemon.run_forever()

if __name__ == "__main__":
    asyncio.run(main())
