"""
HexClaw — planner.py
===================
Agent Planner for translating high-level goals into executable workflows.

v2.0: Uses LiteLLM for dynamic planning.
Fallback: Rule-based keyword matching.
"""

import logging
import os
from typing import Any

# Try to import litellm for dynamic planning
try:
    import litellm
except ImportError:
    litellm = None

# Import awesome_skills for dynamic planning
import awesome_skills

log = logging.getLogger("hexclaw.planner")

def plan_goal(goal: str) -> dict[str, Any]:
    """
    Given a goal string, return a workflow definition.
    Workflow definition:
    {
        "skill": str,
        "params": dict,
        "steps": list (optional override)
    }
    """
    log.info("Planning goal: '%s'", goal)
    
    # ── 1. Check for Explicit @skill-name Invocation ──
    import re
    match = re.search(r"@([a-zA-Z0-9_-]+)", goal)
    if match:
        skill_name = match.group(1)
        log.info(f"Explicit skill requested via @ syntax: {skill_name}")
        awesome_match = awesome_skills.get_skill_by_name(skill_name)
        if awesome_match:
            # Extract target if possible
            domain_match = re.search(r'([a-z0-9]+(-[a-z0-9]+)*\.)+([a-z]{2,})', goal.lower())
            target = domain_match.group(0) if domain_match else "unknown"
            
            return {
                "skill": "awesome_skill_execution",
                "params": {
                    "target": target,
                    "goal": goal,
                    "skill_name": awesome_match["name"],
                    "skill_content": awesome_match["raw_content"]
                }
            }
        else:
            log.warning(f"Requested skill '@{skill_name}' not found in Awesome Skills index. Proceeding with standard planning.")

    # ── 2. Standard LLM Planning (if available) ──
    # If API keys are available and litellm is installed, try LLM planning
    if litellm and os.getenv("GOOGLE_API_KEY"):
        log.info("Using LLM planner (litellm + Gemini available)")
        return _plan_with_llm(goal)
    
    # ── 3. Fallback: Rule-based planning ──
    log.info("Using rule-based planner (no LLM available)")
    return _plan_with_rules(goal)

def _plan_with_rules(goal: str) -> dict[str, Any]:
    goal_lower = goal.lower()
    
    # Domain extraction
    import re
    domain_match = re.search(r'([a-z0-9]+(-[a-z0-9]+)*\.)+([a-z]{2,})', goal_lower)
    target = domain_match.group(0) if domain_match else "unknown"
    log.info("Extracted target: %s", target)
    
    # Cyber / Recon
    if any(kw in goal_lower for kw in ["scan", "recon", "domain", "vuln", "nuclei"]):
        log.info("Rule match → skill: recon_osint")
        return {
            "skill": "recon_osint",
            "params": {"target": target, "description": "Auto-planned recon based on goal"}
        }
    
    # Dev / Git
    if any(kw in goal_lower for kw in ["git", "clone", "deploy", "lint", "test"]):
        log.info("Rule match → skill: dev_ops")
        return {
            "skill": "dev_ops",
            "params": {"target": target, "action": "clone_and_test"}
        }
        
    # Coding / Scripting
    if any(kw in goal_lower for kw in ["code", "script", "app", "write", "python"]):
        log.info("Rule match → skill: autonomous_coder")
        return {
            "skill": "autonomous_coder",
            "params": {"target": target, "goal": goal}
        }
    
    # OSINT / Social
    if any(kw in goal_lower for kw in ["breach", "social", "darkweb", "email"]):
        log.info("Rule match → skill: osint_mapping")
        return {
            "skill": "osint_mapping",
            "params": {"target": target}
        }
    
    # Default: agent_plan (generic)
    log.info("No rule matched → default skill: agent_plan")
    
    # Fallback: Check Awesome Skills
    log.info(f"No built-in rule matched. Searching Awesome Skills for: {goal}")
    awesome_match = awesome_skills.find_relevant_skill(goal)
    if awesome_match:
        log.info(f"Awesome Skill match found: {awesome_match['name']}")
        return {
            "skill": "awesome_skill_execution", # We will handle this dynamically in the daemon
            "params": {
                "target": target, 
                "goal": goal,
                "skill_name": awesome_match["name"],
                "skill_content": awesome_match["raw_content"]
            }
        }
        
    return {"skill": "agent_plan", "params": {"target": target, "goal": goal}}

def _plan_with_llm(goal: str) -> dict[str, Any]:
    """
    Experimental: LiteLLM planner.
    Uses a very low-token prompt to decide which skill to run.
    """
    try:
        import json
        import asyncio
        import inference

        prompt = f"""
        You are the HexClaw Orchestrator. 
        Goal: "{goal}"
        Available Skills: [recon_osint, dev_ops, osint_mapping]
        
        Respond ONLY with a JSON object:
        {{"skill": "skill_name", "params": {{"target": "extracted_target"}}}}
        """
        
        system_prompt = 'You must strictly output ONLY valid JSON: {"skill":"<name>","params":{"target":"<value>"}}'
        
        response_text = asyncio.run(inference.ask(prompt, complexity="low", system=system_prompt))
        
        try:
            clean_text = response_text.replace("```json", "").replace("```", "").strip()
            data = json.loads(clean_text)
            
            if "skill" in data and "params" in data:
                return data
            else:
                log.error("LLM Planning missing keys.")
                return _plan_with_rules(goal)
        except json.JSONDecodeError as e:
            log.error(f"LLM Planning invalid JSON: {e}")
            return _plan_with_rules(goal)

    except Exception as e:
        log.error("LLM Planning failed: %s", e)
        return _plan_with_rules(goal)
