"""
HexClaw — inference.py
======================
Thrifty LLM inference with provider tiering and token logging.

PRD compliance:
  • Providers: google_pro(gemini-2.0-flash), z_ai, openrouter(g
ranite-3.1), free(ollama/llama3).  • Tiers: low, med, high.
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

# Z.AI base URL (OpenAI-compatible)
Z_AI_BASE = "https://api.z.ai/api/coding/paas/v4"

# Providers
PROVIDERS = {
    "google_pro": "gemini/gemini-2.0-flash",
    "z_ai":       "openai/glm-4.7",       # Z.AI — full model, high/med tasks
    "z_ai_lite":  "openai/glm-4.5-air",   # Z.AI — lightweight, low-cost tasks
    "openrouter": "openrouter/ibm/granite-3.1-8b-instruct",
    "free":       "ollama/llama3"
}

# Tiers — Z.ai primary; Gemini Flash / OpenRouter / Ollama as fallbacks
TIERS = {
    "high": [PROVIDERS["z_ai"],      PROVIDERS["google_pro"], PROVIDERS["openrouter"]],
    "med":  [PROVIDERS["z_ai"],      PROVIDERS["google_pro"], PROVIDERS["openrouter"]],
    "low":  [PROVIDERS["z_ai_lite"], PROVIDERS["google_pro"], PROVIDERS["free"]]
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
    def __init__(self):
        log.info(f"Inference Engine initialized (LiteLLM available: {LITELLM_AVAILABLE})")

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
            log.warning(f"LiteLLM not available. Falling back to local Ollama API for tier={complexity} prompt_len={len(prompt)}.")
            try:
                import requests
                response = requests.post(
                    "http://localhost:11434/api/generate",
                    json={"model": "llama3", "prompt": prompt, "stream": False},
                    timeout=60
                )
                text = response.json().get("response", "")
                log.info(f"Ollama response: 0↑ 0↓ tokens · $0.00 · {len(text)} chars")
                
                # Cache the response
                cache.set(f"{system}\n\n{prompt}", text)
                
                log_tokens(
                    provider="ollama",
                    model="llama3",
                    tier=complexity,
                    tokens_in=0,
                    tokens_out=0,
                    cost=0.0
                )
                return text
            except Exception as e:
                log.error(f"Ollama fallback failed: {e}")
                return ""

        model = self.select_model(complexity)
        log.info(f"LLM call: model={model} tier={complexity} prompt_len={len(prompt)}")
        
        # ── Provider specific kwargs ──────────────────────────────────────────
        kwargs = {}
        if "gemini/" in model:
            # Google Gemini via LiteLLM — uses GOOGLE_API_KEY automatically
            pass
        elif model in (PROVIDERS["z_ai"], PROVIDERS["z_ai_lite"]):
            # Both Z.AI models share the same endpoint and API key.
            # Set explicitly so LiteLLM never picks up a wrong OPENAI_API_BASE from env.
            kwargs["api_base"] = Z_AI_BASE
            kwargs["api_key"]  = os.getenv("ZHIPUAI_API_KEY") or os.getenv("OPENAI_API_KEY", "")
            log.debug("Z.AI call -> base=%s  model=%s", kwargs["api_base"], model)

        try:
            log.debug("litellm.acompletion -> model=%s  kwargs_keys=%s", model, list(kwargs))
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
            
            log.info("LLM response: %d in %d out tokens | $%.4f | %d chars", u_in, u_out, float(cost), len(text))
            
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
            # Encode to ASCII for the console — error messages from Z.AI
            # are in Chinese and crash Windows CP1252 stream handlers
            safe_err = str(e).encode("ascii", errors="replace").decode("ascii")
            log.error("Inference FAILED for %s — %s", model, safe_err)
            log.debug("Full error (raw): %r", str(e))
            return f"Error: {safe_err}"

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
