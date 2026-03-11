"""
HexClaw — data.py
================
Hybrid analytical data engine.

PRD compliance:
  • DuckDB (':memory:') for fast analytical queries on local findings.
  • psycopg2 for persistence/heavy lookups in Postgres.
  • query(prompt): text-to-sql -> DataFrame.
  • store_parquet(df, name): save results for persistence.
  • suggest_next(workflow_id): Internal logic to pick next actions based on data.
"""

import logging
import os
import sqlite3
from typing import Any, List, Optional

import duckdb
import pandas as pd
from dotenv import load_dotenv

import inference

load_dotenv()

log = logging.getLogger("hexclaw.data")

# ── Config ────────────────────────────────────────────────────────────────────
from config import DATA_DIR, JOBS_DB

# ── Engines ───────────────────────────────────────────────────────────────────
_duck = duckdb.connect(':memory:')

def get_duck():
    return _duck

def get_pg_conn():
    """Return a psycopg2 connection if POSTGRES_DSN is set and server is reachable."""
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=3)
        return conn
    except Exception as e:
        log.debug("Postgres unavailable (will use SQLite fallback): %s", e)
        return None

# ── Analytics ─────────────────────────────────────────────────────────────────
async def query(prompt: str) -> pd.DataFrame:
    """
    Translate natural language to SQL and run against DuckDB.
    In v1.0, this is a 'Thrifty' shim using LLM only for SQL generation.
    """
    # 1. Get Schema (simplified for v1.0)
    schema = "Tables: jobs(id, skill, target, status), token_log(provider, model, cost)" 
    
    # 2. Text-to-SQL or Direct SQL
    if any(keyword in prompt.upper() for keyword in ["SELECT ", "WITH ", "DESCRIBE "]):
        sql = prompt
    else:
        sql_prompt = f"Convert this request to a DuckDB SQL query. Only respond with the SQL.\nSchema: {schema}\nRequest: {prompt}"
        sql = await inference.ask(sql_prompt, complexity="med", system="You are a SQL expert. Output ONLY valid DuckDB SQL.")
    
    # Clean SQL if LLM included backticks or returned an error message
    sql = sql.replace("```sql", "").replace("```", "").strip()
    if sql.startswith("Error:"):
        log.error(f"LLM returned error instead of SQL: {sql}")
        return pd.DataFrame()
    
    log.info("Executing SQL: %s", sql)

    # 3. Try Postgres first, fall through to DuckDB+SQLite on any failure
    pg_conn = get_pg_conn()
    if pg_conn:
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                df = pd.read_sql_query(sql, pg_conn)
            pg_conn.close()
            return df
        except Exception as e:
            log.warning("Postgres query failed, falling back to DuckDB: %s", e)
            try:
                pg_conn.close()
            except Exception:
                pass
            # Fall through to DuckDB below

    # 4. DuckDB over local SQLite jobs DB
    try:
        _duck.execute("INSTALL sqlite; LOAD sqlite;")
        _duck.execute(f"ATTACH IF NOT EXISTS '{JOBS_DB}' AS main_jobs (TYPE SQLITE)")
        _duck.execute("SET search_path = 'main_jobs,main'")
        df = _duck.query(sql).to_df()
        return df
    except Exception as e:
        log.error("Data query failed: %s", e)
        return pd.DataFrame()

def store_parquet(df: pd.DataFrame, name: str):
    """Store results as Parquet in the data directory."""
    path = DATA_DIR / f"{name}.parquet"
    df.to_parquet(path)
    log.info(f"Stored {len(df)} rows to {path}")
    
    pg_conn = get_pg_conn()
    if pg_conn:
        try:
            cur = pg_conn.cursor()
            cols = ",".join(df.columns)
            vals = ",".join(["%s"] * len(df.columns))
            insert_q = f"INSERT INTO {name} ({cols}) VALUES ({vals})"
            
            df_clean = df.where(pd.notnull(df), None)
            records = [tuple(x) for x in df_clean.values.tolist()]
            
            cur.executemany(insert_q, records)
            pg_conn.commit()
            cur.close()
            pg_conn.close()
            log.info(f"Mirrored {len(df)} rows to Postgres table {name}")
        except Exception as e:
            log.error(f"Postgres mirror failed: {e}")
            pg_conn.rollback()
            pg_conn.close()

# ── Workflows ─────────────────────────────────────────────────────────────────
def suggest_next(workflow_id: str) -> List[str]:
    """
    Rule-based + SQL suggestion logic.
    Analyzes findings for a job and suggests logical next steps.
    """
    suggestions = []
    
    try:
        # Check for high CVSS scores
        res_cve = _duck.query(
            f"SELECT COUNT(*) FROM read_parquet('{DATA_DIR}/findings.parquet') "
            f"WHERE workflow_id = '{workflow_id}' AND cvss_score >= 7.0"
        ).fetchone()
        if res_cve and res_cve[0] > 0:
            suggestions.append("Exploit CVE")
            
        # Check for HTTP services on web ports
        res_http = _duck.query(
            f"SELECT COUNT(*) FROM read_parquet('{DATA_DIR}/findings.parquet') "
            f"WHERE workflow_id = '{workflow_id}' "
            f"AND port IN (80, 443, 8080, 8443) "
            f"AND service ILIKE '%http%'"
        ).fetchone()
        if res_http and res_http[0] > 0:
            suggestions.append("Gobuster Scan")
            
        # Check for SSH service
        res_ssh = _duck.query(
            f"SELECT COUNT(*) FROM read_parquet('{DATA_DIR}/findings.parquet') "
            f"WHERE workflow_id = '{workflow_id}' "
            f"AND service ILIKE '%ssh%'"
        ).fetchone()
        if res_ssh and res_ssh[0] > 0:
            suggestions.append("Bruteforce SSH")
            
    except Exception as e:
        log.warning(f"Error checking findings for suggestions: {e}")
        pass
        
    if not suggestions:
        suggestions = ["Recon target", "Identify tech stack"]
        
    return suggestions[:4]

# ── Telegram Integration ─────────────────────────────────────────────────────
async def get_summary_df() -> str:
    """Returns a markdown summary of the last 5 jobs for Telegram."""
    df = await query("Show me the last 5 jobs and their status")
    if df.empty:
        return "No job data available."
    return df.to_markdown(index=False)
