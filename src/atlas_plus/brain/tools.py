"""Atlas Brain tool definitions and dispatch.

Defines the subset of Atlas MCP tools available to the brain's Ollama agent,
with Ollama-compatible JSON schema definitions and a unified dispatch function.

Tool tiers:
  READ  -- always available (memory search/read, temporal query, tickets list, project state)
  WRITE -- gated by ATLAS_AUTONOMOUS=on (memory_remember, tickets_create, tickets_status)

The brain calls Atlas tool functions directly via Python import -- no HTTP
round-trip to the MCP server needed.
"""

from __future__ import annotations

import json
import os
from typing import Any

from atlas_plus.temporal_graph.build import build_temporal_graph
from atlas_plus import paths as atlas_paths
from atlas_plus.autonomous import autonomous_enabled


def _is_autonomous() -> bool:
    return autonomous_enabled()  # canonical allowlist gate (audit 2026-09-21)


# ---------------------------------------------------------------------------
# Ollama tool schema definitions
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "Search .memory/ files by keyword. Use for general knowledge queries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "scope": {"type": "string", "description": "Optional scope prefix (e.g. 'feedback', 'projects')"},
                    "limit": {"type": "integer", "description": "Max results (default 8)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_read",
            "description": "Read the full content of a .memory/ file by relative path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path e.g. 'feedback/testing.md'"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_list",
            "description": "List .memory/ files, optionally filtered by a prefix.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Optional path prefix (e.g. 'feedback')"},
                    "limit": {"type": "integer", "description": "Max results (default 30)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "temporal_query",
            "description": "Query the temporal knowledge graph. Use 'current' for what is true now, 'as-of' for a specific past date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "mode": {"type": "string", "enum": ["current", "as-of"], "description": "Query mode"},
                    "at": {"type": "string", "description": "ISO 8601 date for as-of mode (e.g. '2026-05-08')"},
                    "limit": {"type": "integer", "description": "Max results (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "temporal_history",
            "description": "Show the full temporal history of an entity (project, file, preference, decision).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Entity name or alias"},
                },
                "required": ["entity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "temporal_build",
            "description": "Incrementally build the derived temporal graph index. Writes only under .atlas/temporal_graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["atlas", "memory", "sessions", "transcripts", "all", "fixtures"],
                        "description": "Source to ingest. atlas = memory + sessions.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tickets_list",
            "description": "List tickets, optionally filtered by project or status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name filter"},
                    "status": {"type": "string", "description": "Status filter: todo|in-progress|blocked|done"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_state",
            "description": "Get git state, open tickets, and dep manifest for a project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Project name (e.g. 'atlas-memory+')"},
                },
                "required": [],
            },
        },
    },
    # Write tools (gated)
    {
        "type": "function",
        "function": {
            "name": "memory_remember",
            "description": "Write a fact to .memory/. Requires user permission and ATLAS_AUTONOMOUS=on.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Scope e.g. 'preference:testing', 'project:atlas-memory+'"},
                    "fact": {"type": "string", "description": "The fact to write (1-3 sentences)"},
                },
                "required": ["scope", "fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tickets_create",
            "description": "Create a new ticket. Requires ATLAS_AUTONOMOUS=on.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "project": {"type": "string"},
                    "priority": {"type": "string", "enum": ["critical", "high", "normal", "low"]},
                    "effort": {"type": "string", "enum": ["S", "M", "L", "XL"]},
                    "body": {"type": "string"},
                    "assignee": {"type": "string", "enum": ["human", "claude-code"]},
                },
                "required": ["title", "project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tickets_status",
            "description": "Update a ticket's status. Requires ATLAS_AUTONOMOUS=on.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["todo", "in-progress", "done", "blocked", "archived"]},
                },
                "required": ["ticket_id", "status"],
            },
        },
    },
]

_WRITE_TOOLS = {"memory_remember", "tickets_create", "tickets_status"}


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def dispatch(tool_name: str, args: dict) -> str:
    """Execute a tool call and return the result as a JSON string.

    Returns an error JSON string if the tool is blocked or fails.
    """
    if tool_name in _WRITE_TOOLS and not _is_autonomous():
        return json.dumps({
            "error": f"Tool '{tool_name}' requires ATLAS_AUTONOMOUS=on. "
                     "Set it in the environment or ask the operator to enable it."
        })

    try:
        result = _call(tool_name, args)
        return json.dumps(result, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _call(tool_name: str, args: dict) -> Any:
    if tool_name == "memory_search":
        from atlas_plus.mcp.memory_server.fts import search
        return search(args["query"], args.get("scope", ""), args.get("limit", 8))

    if tool_name == "memory_read":
        from atlas_plus.mcp.memory_server.tools.read import memory_read
        return memory_read(args["path"])

    if tool_name == "memory_list":
        from atlas_plus.mcp.memory_server.tools.list import memory_list
        return memory_list(args.get("prefix", ""), args.get("limit", 30))

    if tool_name == "temporal_query":
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        import os
        from pathlib import Path
        provider = FileTemporalProvider(
            graph_root=atlas_paths.state_dir() / "temporal_graph"
        )
        mode = args.get("mode", "current")
        if mode == "as-of" and args.get("at"):
            results = provider.search_as_of(args["query"], at=args["at"], limit=args.get("limit", 5))
        else:
            results = provider.search_current(args["query"], limit=args.get("limit", 5))
        return [
            {
                "fact": r.fact.fact_text,
                "valid_from": r.fact.valid_from[:10],
                "invalid_at": r.fact.invalid_at[:10] if r.fact.invalid_at else None,
                "score": round(r.score, 3),
                "provenance": r.provenance[:2],
            }
            for r in results
        ]

    if tool_name == "temporal_history":
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        import os
        from pathlib import Path
        provider = FileTemporalProvider(
            graph_root=atlas_paths.state_dir() / "temporal_graph"
        )
        h = provider.history(args["entity"])
        return {
            "entity": h.entity.name,
            "current": h.current_fact.fact_text if h.current_fact else None,
            "history": [
                {"fact": f.fact_text, "valid_from": f.valid_from[:10],
                 "invalid_at": f.invalid_at[:10] if f.invalid_at else None}
                for f in h.facts
            ],
            "warnings": h.warnings,
        }

    if tool_name == "temporal_build":
        return build_temporal_graph(args.get("source", "atlas"))

    if tool_name == "tickets_list":
        from atlas_plus.tickets.store import list_tickets
        tickets = list_tickets(
            project=args.get("project") or None,
            status=args.get("status") or None,
        )
        return [t.as_dict() for t in tickets]

    if tool_name == "project_state":
        from atlas_plus.mcp.memory_server.tools.project_state import project_state
        return project_state(args.get("name") or None)

    if tool_name == "memory_remember":
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember
        return memory_remember(args["scope"], args["fact"])

    if tool_name == "tickets_create":
        from atlas_plus.tickets.store import create_ticket
        ticket = create_ticket(
            title=args["title"],
            project=args["project"],
            assignee=args.get("assignee", "human"),
            priority=args.get("priority", "normal"),
            effort=args.get("effort", "M"),
            body=args.get("body", ""),
        )
        return ticket.as_dict()

    if tool_name == "tickets_status":
        from atlas_plus.tickets.store import update_ticket_status
        t = update_ticket_status(args["ticket_id"], args["status"])
        return t.as_dict()

    return {"error": f"Unknown tool: {tool_name!r}"}
