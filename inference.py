"""
HexClaw — inference.py
======================
Thrifty LLM inference with provider tiering and token logging.

PRD compliance:
  • Providers: google_pro(gemini-2.0-flash), z_ai, openrouter(granite-3.1), free(ollama/llama3).
  • Tiers: low, med, high.
  • SQLite: data/token_log.db
"""

import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

import cache

load_dotenv()

log = logging.getLogger("hexclaw.inference")

# ── Config ────────────────────────────────────────────────────────────────────
from config import DATA_DIR, TOKEN_LOG_DB

# Providers
PROVIDERS = {
    "google_pro": "gemini/gemini-2.0-flash-exp",
    "z_ai": "openai/glm-4.7", # Routing through Z.ai's OpenAI-compatible API
    "openrouter": "openrouter/ibm/granite-3.1-8b-instruct",
    "free": "ollama/llama3"
}

# Tiers
TIERS = {
    "high": [PROVIDERS["google_pro"], PROVIDERS["openrouter"]],
    "med":  [PROVIDERS["z_ai"], PROVIDERS["openrouter"]],
    "low":  [PROVIDERS["z_ai"], PROVIDERS["free"]]
}

try:
    import litellm
    LITELLM_AVAILABLE = True
except ImportError:
    litellm = None
    LITELLM_AVAILABLE = False

# ── Database ──────────────────────────────────────────────────────────────────
_db_ready = False

def init_db():
    """Create token_log table if it doesn't exist.  Safe to call multiple times."""
    global _db_ready
    if _db_ready:
        return
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(TOKEN_LOG_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS token_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT,
            model TEXT,
            tier TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost REAL,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    _db_ready = True

def _ensure_db():
    """Lazily initialise the token log DB on first write/read."""
    if not _db_ready:
        init_db()

def log_tokens(provider: str, model: str, tier: str, tokens_in: int, tokens_out: int, cost: float = 0.0):
    try:
        _ensure_db()
        conn = sqlite3.connect(TOKEN_LOG_DB)
        conn.execute(
            "INSERT INTO token_log (provider, model, tier, tokens_in, tokens_out, cost, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (provider, model, tier, tokens_in, tokens_out, cost, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"Failed to log tokens: {e}")

# ── Inference Engine ──────────────────────────────────────────────────────────
class InferenceEngine:
    def select_model(self, complexity: str) -> str:
        """Complexity: low, med, high -> returns first available model in tier."""
        tier = TIERS.get(complexity, TIERS["low"])
        return tier[0]

    async def ask(self, prompt: str, complexity: str = "low", system: str = "You are HexClaw.") -> str:
        # ── Cache check first ─────────────────────────────────────────────────
        hit = cache.get(f"{system}\n\n{prompt}")
        if hit:
            log.info(f"Cache HIT for tier={complexity} — 0 tokens, $0.00")
            log_tokens("cache", "exact-semantic", complexity, 0, 0, 0.0)
            return hit

        if not LITELLM_AVAILABLE:
            log.warning("LiteLLM not available. Falling back to local Ollama API.")
            try:
                import requests
                response = requests.post(
                    "http://localhost:11434/api/generate",
                    json={"model": "llama3", "prompt": prompt, "stream": False},
                    timeout=60
                )
                return response.json()["response"]
            except Exception as e:
                log.error(f"Ollama fallback failed: {e}")
                return ""

        model = self.select_model(complexity)
        log.info(f"LLM call: model={model} tier={complexity} prompt_len={len(prompt)}")
        
        # ── Provider specific logic ───────────────────────────────────────────
        kwargs = {}
        if "openai/" in model and "glm" in model:
            # Special routing for Z.ai OpenAI-compatible endpoint
            kwargs["api_base"] = "https://api.z.ai/api/coding/paas/v4"
            kwargs["api_key"] = os.getenv("ZHIPUAI_API_KEY")
        elif "anthropic/" in model and "glm" in model:
            # Fallback/alternative routing through Z.ai's Anthropic endpoint
            kwargs["api_base"] = "https://api.z.ai/api/anthropic"
            kwargs["api_key"] = os.getenv("ZHIPUAI_API_KEY")

        try:
            response = await litellm.acompletion(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=2048,
                **kwargs
            )
            
            text = response.choices[0].message.content
            usage = response.usage
            u_in = getattr(usage, "prompt_tokens", 0) or 0
            u_out = getattr(usage, "completion_tokens", 0) or 0
            cost = getattr(response, "_hidden_params", {}).get("response_cost") or 0.0
            
            log.info(f"LLM response: {u_in}↑ {u_out}↓ tokens · ${float(cost):.4f} · {len(text)} chars")
            
            # ── Store in cache ────────────────────────────────────────────────
            cache.set(f"{system}\n\n{prompt}", text)
            
            log_tokens(
                provider=model.split("/")[0],
                model=model,
                tier=complexity,
                tokens_in=usage.prompt_tokens,
                tokens_out=usage.completion_tokens,
                cost=cost
            )
            return text
        except Exception as e:
            log.error(f"Inference FAILED for {model}: {e}")
            return f"Error: {e}"

    def ask_sync(self, prompt: str, complexity: str = "low") -> str:
        return asyncio.run(self.ask(prompt, complexity))

def usage_report() -> dict:
    _ensure_db()
    conn = sqlite3.connect(TOKEN_LOG_DB)
    conn.row_factory = sqlite3.Row
    stats = conn.execute("""
        SELECT 
            tier, 
            SUM(tokens_in) as total_in, 
            SUM(tokens_out) as total_out, 
            SUM(cost) as total_cost 
        FROM token_log GROUP BY tier
    """).fetchall()
    conn.close()
    return {row['tier']: dict(row) for row in stats}

# Singleton instance
engine = InferenceEngine()

async def ask(prompt: str, complexity: str = "low", system: str = "You are HexClaw.") -> str:
    return await engine.ask(prompt, complexity, system)

if __name__ == "__main__":
    import json
    print("HexClaw Inference Engine")
    print(f"Usage: {json.dumps(usage_report(), indent=2)}")
