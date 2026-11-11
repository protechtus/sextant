"""Atlas Brain agent registry -- capability matching, routing, and live status.

Defines all available agents (local Ollama models + CLI agents), their
capabilities, and routing rules for dispatching tasks to the best agent.

Agent types:
  ollama     -- local Ollama model, called directly via /api/chat
  claude-cli -- claude CLI (subprocess), for complex coding + architecture
  codex-cli  -- codex CLI (subprocess), for coding with Atlas memory context

Routing tiers (checked in order):
  1. Explicit override  -- caller specifies agent name
  2. Keyword rules      -- instruction matches a capability pattern
  3. Default            -- qwen3:8b (Brain)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

import logging

import httpx

_STATUS_CACHE_TTL = 15.0  # seconds between full Ollama re-pings
_last_refresh: float = 0.0

OLLAMA_BASE_URL = __import__('atlas_plus.llm.ollama_url', fromlist=['default_ollama_url']).default_ollama_url()
CLAUDE_CLI = os.environ.get("ATLAS_CLAUDE_CLI", "claude")
CODEX_CLI = os.environ.get("ATLAS_CODEX_CLI", "codex")
OPENCODE_CLI = os.environ.get("ATLAS_OPENCODE_CLI", "opencode")

# Hybrid router: classify instructions via fast LLM before keyword routing.
# Default on -- qwen3:1.7b adds ~500ms first call, cached thereafter.
_SMART_ROUTING = os.environ.get("ATLAS_SMART_ROUTING", "on").lower() in ("on", "1", "true")
_ROUTING_CACHE: dict[str, str] = {}  # instruction_prefix -> category

# Local-only mode: API agents (claude-code, codex, opencode) treated as permanently offline.
# Set ATLAS_LOCAL_ONLY=1 to force all tasks to Qwen3 local agents.
def _strict_local_only() -> bool:
    # One predicate for the whole program (round-B 2026-11-09, N-F): "on" refuses cloud
    # everywhere; unset means local by default with cloud only where a task's routing names
    # a cloud provider explicitly (no silent fallback); "off" also allows the auto-chain fallback.
    from atlas_plus.llm import _local_only
    return _local_only()


class _LocalOnlyFlag:
    """Module-level flag that reads the shared predicate at use time."""
    def __bool__(self) -> bool:
        return _strict_local_only()


_LOCAL_ONLY = _LocalOnlyFlag()
_LOCAL_AGENT_TYPES = {"ollama", "research"}  # agent_type values that run locally

_ROUTING_CATEGORIES = {"simple", "coding", "complex-coding", "research", "planning", "analysis"}
_SMART_ROUTING_MAP = {
    "simple": "fast",
    "coding": "coder",
    "complex-coding": "claude-code",
    "research": "research",
    "planning": "brain",
    "analysis": "brain",
}

log = logging.getLogger("atlas.brain.agents")


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

@dataclass
class Agent:
    """Registry entry for a routable Brain agent: name, model, type (ollama|claude-cli|codex-cli), and capabilities."""

    name: str
    model: str
    agent_type: str          # ollama | claude-cli | codex-cli
    capabilities: list[str]  # quick, reasoning, coding, memory, planning, search
    description: str
    online: bool = False
    current_task: Optional[str] = None

    def as_dict(self) -> dict:
        """Serialize to a JSON-safe dict for API responses and Web UI rendering."""
        return {
            "name": self.name,
            "model": self.model,
            "type": self.agent_type,
            "capabilities": self.capabilities,
            "description": self.description,
            "online": self.online,
            "current_task": self.current_task,
        }


# ---------------------------------------------------------------------------
# Registry -- edit this to add/remove agents
# ---------------------------------------------------------------------------

REGISTRY: list[Agent] = [
    Agent(
        name="fast",
        model="qwen3:1.7b",
        agent_type="ollama",
        capabilities=["quick", "search", "simple_qa"],
        description="Quick answers, simple lookups, fast responses. Use when speed matters more than depth.",
    ),
    Agent(
        name="brain",
        model=os.environ.get("ATLAS_BRAIN_MODEL", "qwen3:8b"),
        agent_type="ollama",
        capabilities=["reasoning", "planning", "memory", "tool_use", "analysis"],
        description="Main reasoning agent. Planning, memory operations, tool use, complex analysis.",
    ),
    Agent(
        name="mistral",
        model="mistral:latest",
        agent_type="ollama",
        capabilities=["reasoning", "creative", "alternative"],
        description="Alternative reasoning model. Use for second opinions or when Brain is busy.",
    ),
    Agent(
        name="coder",
        model=os.environ.get("ATLAS_OPENCODE_MODEL", "qwen2.5-coder:7b"),
        agent_type="ollama",
        capabilities=["coding", "refactor", "debugging", "code-review", "local-coding"],
        description="Local coding specialist. Code generation, debugging, refactoring. No token cost.",
    ),
    Agent(
        name="claude-code",
        model="claude-cli",
        agent_type="claude-cli",
        capabilities=["coding", "refactor", "architecture", "multi-file", "debugging"],
        description="Claude Code CLI. Best for complex coding, multi-file edits, architecture decisions.",
    ),
    Agent(
        name="codex",
        model="codex-cli",
        agent_type="codex-cli",
        capabilities=["coding", "memory-aware-coding"],
        description="Codex CLI with Atlas memory context. Coding tasks that benefit from project history.",
    ),
    Agent(
        name="opencode",
        model="opencode/big-pickle",
        agent_type="opencode-cli",
        capabilities=["coding", "refactor", "debugging", "free-coding"],
        description="OpenCode CLI. Free cloud coding model -- use when Claude Code tokens are exhausted.",
    ),
    Agent(
        name="research",
        model=os.environ.get("ATLAS_BRAIN_MODEL", "qwen3:8b"),
        agent_type="research",
        capabilities=["research", "investigation", "synthesis", "report"],
        description="Research agent. Investigates topics, searches memory, writes structured reports to .atlas/reports/.",
    ),
]

_registry_by_name: dict[str, Agent] = {a.name: a for a in REGISTRY}


# ---------------------------------------------------------------------------
# Status checks
# ---------------------------------------------------------------------------

def _fetch_ollama_models() -> list[str]:
    """Return list of pulled model names from Ollama, or [] on any error."""
    try:
        r = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3.0)
        if r.status_code == 200:
            return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass
    return []


def _check_cli(cli: str) -> bool:
    return shutil.which(cli) is not None


def _check_one(agent: Agent, ollama_models: list[str]) -> bool:
    # Local-only mode: API agents are treated as permanently offline.
    if _LOCAL_ONLY and agent.agent_type not in _LOCAL_AGENT_TYPES:
        return False
    if agent.agent_type == "ollama":
        return agent.model in ollama_models
    if agent.agent_type == "claude-cli":
        return _check_cli(CLAUDE_CLI)
    if agent.agent_type == "codex-cli":
        return _check_cli(CODEX_CLI)
    if agent.agent_type == "opencode-cli":
        return _check_cli(OPENCODE_CLI)
    if agent.agent_type == "research":
        # Research delegates to the brain Ollama model -- no separate process to check.
        brain = _registry_by_name.get("brain")
        return (brain.model in ollama_models) if brain else False
    return False


def refresh_status() -> list[Agent]:
    """Ping all agents in parallel and update online status."""
    global _last_refresh
    now = time.monotonic()
    if now - _last_refresh < _STATUS_CACHE_TTL:
        return REGISTRY
    _last_refresh = now
    # Fetch Ollama model list once; all Ollama-type agents share the same endpoint.
    # This reduces N parallel HTTP calls to 1, then threads only for CLI checks.
    ollama_models = _fetch_ollama_models()
    with ThreadPoolExecutor(max_workers=len(REGISTRY)) as ex:
        futures = {ex.submit(_check_one, a, ollama_models): a for a in REGISTRY}
        for fut, agent in futures.items():
            try:
                agent.online = fut.result(timeout=5.0)
            except Exception:
                agent.online = False
    return REGISTRY


def get_agents(refresh: bool = True) -> list[Agent]:
    """Return the full agent registry, optionally refreshing online status first."""
    if refresh:
        refresh_status()
    return REGISTRY


def get_agent(name: str) -> Optional[Agent]:
    """Look up a single agent by name; returns None if not registered."""
    return _registry_by_name.get(name)


# ---------------------------------------------------------------------------
# Task routing
# ---------------------------------------------------------------------------

_RESEARCH_PATTERNS = {
    "research ", "investigate ", "find out about", "what do we know about",
    "research the", "write a report on", "write a report about",
    "deep dive into", "analyze the topic", "investigate the topic",
}

_QUICK_PATTERNS = {
    "what is", "what are", "who is", "define ", "list ", "show me",
    "quick", "fast", "brief", "summary of", "how many",
}

_CODING_PATTERNS = {
    "write code", "implement ", "fix bug", "debug ", "refactor ",
    "add function", "create file", "edit file", "update file",
    "add a test", "write a test", "write tests",
}

_PLANNING_PATTERNS = {
    "plan ", "break down", "decompose", "steps to", "how to build",
    "design ", "architect", "strategy for",
}


def _classify_instruction(instruction: str) -> str:
    """Call qwen3:1.7b to classify the instruction into a routing category.

    Returns one of: simple | coding | complex-coding | research | planning | analysis
    Falls back to 'analysis' on any error. Result is cached in _ROUTING_CACHE.
    Only called when ATLAS_SMART_ROUTING=on.
    """
    key = instruction[:120]
    if key in _ROUTING_CACHE:
        return _ROUTING_CACHE[key]
    try:
        payload = {
            "model": "qwen3:1.7b",
            "messages": [{
                "role": "user",
                "content": (
                    "Classify this task into exactly one of these categories: "
                    "simple, coding, complex-coding, research, planning, analysis.\n"
                    "Reply with ONLY the category name.\n\n"
                    f"Task: {instruction[:400]}"
                ),
            }],
            "stream": False,
            "options": {"num_predict": 8},
        }
        resp = httpx.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=5.0)
        if resp.status_code == 200:
            raw = resp.json().get("message", {}).get("content", "").strip().lower()
            # Strip thinking tags (qwen3 /no_think pattern)
            raw = raw.split("/no_think")[-1].strip()
            word = raw.split()[0] if raw.split() else ""
            category = word if word in _ROUTING_CATEGORIES else "analysis"
            _ROUTING_CACHE[key] = category
            if len(_ROUTING_CACHE) > 500:
                # Simple FIFO eviction: instruction prefixes are stable, so
                # the oldest entry is the least likely to be needed again.
                _ROUTING_CACHE.pop(next(iter(_ROUTING_CACHE)))
            log.debug("Smart route classify: %r -> %s", instruction[:40], category)
            return category
    except Exception:
        pass
    return "analysis"


def route(instruction: str, override: Optional[str] = None) -> Agent:
    """Return the best available agent for the given instruction.

    Precedence:
      1. override name (if valid and online)
      2. keyword routing
      3. default (brain)
    """
    if override:
        agent = _registry_by_name.get(override)
        if agent:
            refresh_status()
            if agent.online:
                return agent

    low = instruction.lower()
    refresh_status()

    # Smart routing: use fast LLM classifier when ATLAS_SMART_ROUTING=on
    if _SMART_ROUTING:
        category = _classify_instruction(instruction)
        preferred_name = _SMART_ROUTING_MAP.get(category, "brain")
        # Local-only: reroute API-bound categories to the best local alternative
        if _LOCAL_ONLY and preferred_name in ("claude-code", "codex", "opencode"):
            preferred_name = "coder"
        preferred = _registry_by_name.get(preferred_name)
        if preferred and preferred.online:
            log.info("Smart route: %r -> %s (category=%s)", instruction[:50], preferred.name, category)
            return preferred
        # Preferred agent offline: fall through to keyword routing rather than blocking.
        # This prevents a single offline agent from stopping all task dispatch.
        log.debug("Smart route preferred %r offline, falling through to keyword routing", preferred_name)

    # Research investigation -> research agent
    if any(p in low for p in _RESEARCH_PATTERNS):
        research = _registry_by_name.get("research")
        if research and research.online:
            return research

    # Coding -> local coder first, claude-code for multi-file/architecture
    if any(p in low for p in _CODING_PATTERNS):
        is_complex = any(p in low for p in ("multi-file", "architect", "redesign", "codebase"))
        if is_complex:
            claude = _registry_by_name.get("claude-code")
            if claude and claude.online:
                return claude
        coder = _registry_by_name.get("coder")
        if coder and coder.online:
            return coder
        claude = _registry_by_name.get("claude-code")
        if claude and claude.online:
            return claude
        opencode = _registry_by_name.get("opencode")
        if opencode and opencode.online:
            return opencode
        brain = _registry_by_name.get("brain")
        if brain and brain.online:
            return brain

    # Quick lookups -> fast model if online
    if any(p in low for p in _QUICK_PATTERNS):
        fast = _registry_by_name.get("fast")
        if fast and fast.online:
            return fast

    # Default: brain
    brain = _registry_by_name.get("brain")
    if brain and brain.online:
        return brain

    # Last resort: any online agent
    for agent in REGISTRY:
        if agent.online:
            return agent

    # Nothing online -- return brain anyway (will fail gracefully)
    return _registry_by_name["brain"]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch(agent: Agent, instruction: str, session_id: str, timeout: int = 300) -> str:
    """Run an instruction through the given agent. Returns result text.

    For ollama agents: calls agent.py chat() with the specified model.
    For CLI agents: runs the CLI as a subprocess.
    """
    if agent.agent_type == "ollama":
        from .agent import chat
        result = chat(
            session_id=session_id,
            user_message=instruction,
            model=agent.model,
        )
        return result.text

    if agent.agent_type in ("claude-cli", "codex-cli"):
        cli = CLAUDE_CLI if agent.agent_type == "claude-cli" else CODEX_CLI
        safe = instruction.replace("<", "").replace(">", "")[:2000]
        try:
            result = subprocess.run(
                [cli, "--print", safe],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode != 0:
                return f"[{agent.name}] exited {result.returncode}: {output[:500]}"
            return output[:4000]
        except subprocess.TimeoutExpired:
            return f"[{agent.name}] timed out after {timeout}s"
        except FileNotFoundError:
            return f"[{agent.name}] CLI not found: {cli!r}"

    if agent.agent_type == "opencode-cli":
        safe = instruction.replace("<", "").replace(">", "")[:2000]
        try:
            result = subprocess.run(
                [OPENCODE_CLI, "run", safe],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode != 0:
                return f"[opencode] exited {result.returncode}: {output[:500]}"
            return output[:4000]
        except subprocess.TimeoutExpired:
            return f"[opencode] timed out after {timeout}s"
        except FileNotFoundError:
            return f"[opencode] CLI not found: {OPENCODE_CLI!r}"
        except Exception as exc:
            log.error("opencode dispatch failed: %s", exc)
            return f"[opencode] failed: {exc}"

    if agent.agent_type == "research":
        from .research import run_research
        # --force prefix bypasses deduplication check
        force = instruction.startswith("--force ")
        topic = instruction[len("--force "):].strip() if force else instruction
        try:
            report_text, report_path = run_research(topic, session_id, force=force)
            return f"Research complete. Report: {report_path}\n\n{report_text[:2000]}"
        except ValueError as dedup_err:
            # Dedup block: surface as a successful result (not a failure) so the
            # task does not show as failed -- the existing report IS the answer.
            return (
                f"[research] Duplicate detected. {dedup_err}\n\n"
                f"To create a new report anyway, use: --force {topic}"
            )
        except Exception as exc:
            return f"[research] failed: {exc}"

    return f"[dispatch] unknown agent type: {agent.agent_type!r}"
