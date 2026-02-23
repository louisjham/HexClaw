--
description: Codebase improvement analysis — modeled after us_masscan_vuln enhancements
created: 2026-02-20T00:53:00-06:00
---

# HexStrike AI — Codebase Improvement Analysis

## Context

Last session we enhanced `us_masscan_vuln` with:
1. **Application parameter** for targeted hunting (auto-resolves port/filter from registry)
2. **Structured output** (`top_findings`, `insight_aggregation`, `recommended_next_steps`, `confidence`)
3. **Output parsing** (server parses raw output into actionable structured data)
4. **Rich docstrings** with WHEN TO USE / DO NOT USE / HOW IT DIFFERS sections
5. **Windows-compatible paths** (`./output/...` instead of `/tmp/...`)
6. **Batch execution optimizations** (CIDR batching via temp file, fallback)
7. **Dynamic confidence scoring** based on scan results

Below is the analysis of similar improvements that can be applied across the rest of the codebase, grouped by priority.

---

## 🔴 Priority 1 — High-Impact: Output Structure & Parsing (Server-Side)

### Problem
Out of ~97 API endpoints that return `jsonify(result)`, only **4 tools** have structured output parsers:
- `nmap` → `parse_nmap_output()`
- `masscan` → `parse_masscan_output()`
- `rustscan` → `parse_rustscan_output()`
- `httpx` → `parse_httpx_output()`

The remaining ~93 endpoints return **raw `execute_command()` output** (stdout/stderr/exit_code). This means the AI model has to parse raw CLI text, which is:
- Error-prone (model hallucinates structure)
- Token-expensive (raw output is verbose)
- Inconsistent (no `status` / `confidence` / `metadata` fields)

### Recommended Fixes (by tool priority)

#### Tier A — Most commonly used tools (do these first)
| Tool | Server Endpoint | What to Parse | Structured Fields |
|------|----------------|---------------|-------------------|
| **nuclei** | `nuclei()` ~L10935 | Template match lines, severity counts | `vulnerabilities[]`, `severity_summary`, `templates_matched`, `confidence` |
| **gobuster** | `gobuster()` ~L10883 | `Found:` lines, status codes | `directories[]`, `status_code_summary`, `interesting_paths[]` |
| **nikto** | `nikto()` ~L11493 | `+ ` finding lines | `findings[]`, `severity_summary`, `server_info`, `outdated_software[]` |
| **sqlmap** | `sqlmap()` ~L11522 | Injection type, parameter, DBMS | `injectable_params[]`, `dbms_detected`, `injection_types[]`, `confidence` |
| **ffuf** | `ffuf()` ~L11745 | JSON output mode results | `results[]`, `status_code_summary`, `interesting_paths[]` |

#### Tier B — Frequently used recon tools
| Tool | What to Parse |
|------|---------------|
| **amass** | Subdomain list, ASN info, source attribution |
| **subfinder** | Subdomain list with source |
| **hydra** | Cracked credentials, attempts, service info |
| **wpscan** | WordPress version, plugins, themes, vulns |
| **netexec** | Auth results, shares found, modules output |

#### Implementation Pattern (copy from nmap)
```python
# 1. Add parse function
def parse_nuclei_output(stdout: str) -> Dict[str, Any]:
    vulnerabilities = []
    severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    # Parse [severity] [template-id] ... matched lines
    pattern = re.compile(r'\[(\w+)\]\s+\[([^\]]+)\]')
    for line in stdout.splitlines():
        match = pattern.search(line)
        if match:
            sev, template = match.groups()
            severity_counts[sev.lower()] = severity_counts.get(sev.lower(), 0) + 1
            vulnerabilities.append({"severity": sev, "template": template, "detail": line.strip()})
    return {"vulnerabilities": vulnerabilities, "severity_summary": severity_counts}

# 2. Add metadata function
def get_nuclei_metadata(severity_filter: str) -> Dict[str, Any]:
    return {
        "execution_cost": "medium",
        "aggressiveness": "low",
        "data_source": "nuclei",
        "confidence": 0.9
    }

# 3. Update endpoint to use them
@app.route("/api/tools/nuclei", methods=["POST"])
def nuclei():
    # ... existing code ...
    parsed = parse_nuclei_output(stdout)
    metadata = get_nuclei_metadata(severity)
    summary = {
        "status": "success" if result.get("success") else "error",
        "target": target,
        **parsed,
        **metadata,
        "duration_sec": round(result.get("execution_time", 0), 2)
    }
    return jsonify(summary)
```

**Estimated effort per tool**: ~30-60 min. **Impact**: Massive — model gets structured data instead of raw stdout.

---

## 🟠 Priority 2 — Docstring Enhancement (MCP Client-Side)

### Problem
The `us_masscan_vuln` now has a rich docstring with WHEN TO USE / DO NOT USE / HOW IT DIFFERS sections. This is **critical for AI tool selection** — the LLM reads docstrings to decide which tool to call. Most other tools have minimal, generic docstrings.

### Tools That Would Benefit Most from Enhanced Docstrings

#### Group 1: Easily confused tools (model often picks the wrong one)
| Tool A | Tool B | Fix |
|--------|--------|-----|
| `nmap_scan` | `rustscan_fast_scan` / `masscan_high_speed` | Add "HOW IT DIFFERS" section to all three |
| `gobuster_scan` | `ffuf_scan` / `dirb_scan` / `dirsearch_scan` / `feroxbuster_scan` | Add "WHEN TO USE" to differentiate |
| `nuclei_scan` | `nikto_scan` / `jaeles_vulnerability_scan` | Clarify scope differences |
| `amass_scan` | `subfinder_scan` | Active vs passive distinction |
| `enum4linux_scan` | `enum4linux_ng_advanced` | Legacy vs modern |
| `sqlmap_scan` | `xsser_scan` / `wfuzz_scan` | SQLi vs XSS vs fuzzing |

#### Docstring Template (based on us_masscan_vuln pattern)
```python
"""
[One-sentence summary of what this tool does]

═══════════════════════════════════════════════
WHEN TO USE THIS TOOL:
═══════════════════════════════════════════════
  • [Specific trigger phrase 1]
  • [Specific trigger phrase 2]
  • [Pattern matching description]

═══════════════════════════════════════════════
DO NOT USE THIS TOOL WHEN:
═══════════════════════════════════════════════
  • [Anti-pattern → use X instead]
  • [Anti-pattern → use Y instead]

═══════════════════════════════════════════════
HOW IT DIFFERS FROM SIMILAR TOOLS:
═══════════════════════════════════════════════
  • tool_a: [1 sentence]
  • tool_b: [1 sentence]
  • THIS TOOL: [1 sentence explaining unique value]

Args:
    [enhanced arg descriptions with enum values and examples]
"""
```

**Estimated effort per tool**: ~10-20 min. **Impact**: High — directly improves AI tool selection accuracy.

---

## 🟡 Priority 3 — Windows Path Compatibility (Both Files)

### Problem
5 MCP tools and 31 server-side references still use `/tmp/` hardcoded Linux paths. These break on Windows or non-Linux deployments.

### MCP Client (`hexstrike_mcp.py`) — Defaults that need fixing
| Line | Tool | Current Default | Fix |
|------|------|----------------|-----|
| 618 | `prowler_scan` | `output_dir="/tmp/prowler_output"` | `"./output/prowler"` |
| 689 | `scout_suite_assessment` | `report_dir="/tmp/scout-suite"` | `"./output/scout-suite"` |
| 851 | `docker_bench_security_scan` | `output_file="/tmp/docker-bench-results.json"` | `"./output/docker-bench-results.json"` |
| 1739 | `autorecon_comprehensive` | `output_dir="/tmp/autorecon"` | `"./output/autorecon"` |
| 3452 | `foremost_carving` | `output_dir="/tmp/foremost_output"` | `"./output/foremost"` |

### Server (`hexstrike_server.py`) — Critical instances
Must audit all 31 `/tmp` references and convert to `./output/<tool_name>` or use `tempfile.mkdtemp()` for truly temporary files.

**Estimated effort**: ~1 hour total. **Impact**: Medium — enables Windows/cross-platform use.

---

## 🟡 Priority 4 — Intent-Based Parameters (Remove `additional_args`)

### Problem
45+ tools expose `additional_args: str = ""` which lets users inject arbitrary CLI flags. This is:
- **A security risk** — allows command injection
- **Anti-pattern for AI** — the model shouldn't need to know CLI flags
- **Inconsistent** — `nmap_scan` already uses intent-based params (`scan_profile`, `port_scope`, `timing`)

### Strategy
The `nmap_scan` and `us_masscan_vuln` tools are the gold standard: they use **intent-level parameters** (e.g., `scan_profile="aggressive"` instead of `additional_args="-A"`). Other tools should follow this pattern.

### Highest-Priority Tools for Intent-Based Conversion
| Tool | Current Raw Params | Intent-Based Replacement |
|------|-------------------|--------------------------|
| `nuclei_scan` | `additional_args` | `scan_mode: "fast" | "thorough"`, `max_templates: int` |
| `gobuster_scan` | `additional_args` | `threads: int`, `extensions: str`, `status_codes: str` |
| `ffuf_scan` | `additional_args` | `threads: int`, `delay: int`, `rate: int` |
| `sqlmap_scan` | `additional_args` | `risk_level: 1-3`, `level: 1-5`, `technique: str` |
| `hydra_attack` | `additional_args` | `threads: int`, `timeout: int`, `verbose: bool` |

### Approach
1. Add intent-based params to MCP tool signature
2. Translate them to flags in server-side command builder
3. Keep `additional_args` temporarily for backward compat but log a deprecation warning
4. Remove `additional_args` in next major version

**Estimated effort per tool**: ~30-45 min. **Impact**: Medium-high — improves security and AI usability.

---

## 🟢 Priority 5 — Recovery/Error Handling Consistency

### Problem
Only some tools use `execute_command_with_recovery()`:
- ✅ `nmap`, `gobuster`, `nuclei` — have recovery
- ❌ `nikto`, `sqlmap`, `hydra`, `dirb`, `ffuf`, `amass`, etc. — use plain `execute_command()`

### Fix
Add `use_recovery` parameter to all tool endpoints (server-side) and default it to `True`. The MCP client already passes `use_recovery=True` for some tools but not all.

**Estimated effort**: ~2-3 hours for all tools. **Impact**: Medium — improves reliability.

---

## 🟢 Priority 6 — Structured Error Responses

### Problem
Most endpoints return ad-hoc error formats:
```python
# Current inconsistent patterns:
return jsonify({"error": "URL parameter is required"}), 400
return jsonify({"error": f"Server error: {str(e)}"}), 500
```

The enhanced `nmap` endpoint uses a structured pattern:
```python
return jsonify({
    "status": "error",
    "error_type": "invalid_input",  # or "server_error"
    "message": str(e),
    "retryable": False              # helps AI decide whether to retry
}), 400
```

### Fix
Apply the structured error format to all endpoints. Could be done with a decorator or helper function.

**Estimated effort**: ~2 hours. **Impact**: Medium — consistent error handling for AI.

---

## 🔵 Priority 7 — MCP Client Result Handling

### Problem
Most MCP client tools (`hexstrike_mcp.py`) have minimal post-processing:
```python
# Minimal pattern (most tools):
result = hexstrike_client.safe_post("api/tools/dirb", data)
if result.get("success"):
    logger.info("✅ Dirb scan completed for {url}")
else:
    logger.error("❌ Dirb scan failed for {url}")
return result
```

The enhanced `us_masscan_vuln` and `nmap_scan` tools have much richer client-side handling:
- Log severity-colored insights
- Check for recovery info
- Check for human escalation
- Log top findings

### Fix
Apply the enhanced pattern to all MCP tools. At minimum:
1. Check for `recovery_info` and log it
2. Check for `human_escalation`
3. Log key metrics from structured response (if server-side parsing exists)

---

## 📊 Summary Table

| Priority | Category | Tools Affected | Effort | Impact |
|----------|----------|---------------|--------|--------|
| 🔴 P1 | Structured output parsing | ~10 key tools | 5-10 hours | **Critical** |
| 🟠 P2 | Enhanced docstrings | ~15 tools | 3-5 hours | **High** |
| 🟡 P3 | Windows path compat | 5 MCP + 31 server | 1 hour | Medium |
| 🟡 P4 | Intent-based params | ~10 key tools | 5-8 hours | Medium-high |
| 🟢 P5 | Recovery consistency | ~40 tools | 2-3 hours | Medium |
| 🟢 P6 | Structured errors | ~50 endpoints | 2 hours | Medium |
| 🔵 P7 | MCP client handling | ~50 tools | 3-4 hours | Low-medium |

---

## Recommended Execution Order

### Session 1: Quick Wins (P3 + P6)
- Fix all `/tmp` paths → `./output/<tool>` (fast, low risk)
- Apply structured error pattern to all endpoints (templatable)

### Session 2: Nuclei Enhancement (P1 + P2 + P4 for nuclei)
- Add `parse_nuclei_output()` + `get_nuclei_metadata()`
- Enhance nuclei docstring with WHEN TO USE / HOW IT DIFFERS
- Add intent-based params (`scan_mode`, `max_templates`)

### Session 3: Directory Discovery Tools (P1 + P2 for gobuster/ffuf/dirb)
- Add parsers for gobuster, ffuf, dirb
- Enhance docstrings to differentiate them
- Add intent-based params

### Session 4: Offensive Tools (P1 + P2 for sqlmap/nikto/hydra)
- Add parsers for sqlmap, nikto, hydra
- Enhance docstrings
- Add recovery support (P5)

### Session 5: Recon Tools (P1 + P2 for amass/subfinder/netexec/wpscan)
- Add parsers
- Enhance docstrings
- Add recovery support
