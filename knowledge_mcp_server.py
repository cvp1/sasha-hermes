#!/usr/bin/env python3
"""MCP server wrapping the persistent knowledge index — automatic per-turn retrieval.

Exposes query() and status() tools so any model can retrieve relevant context
from memory + vault + work product without an explicit /recall call.

Usage:
    python3 knowledge_mcp_server.py

HOMING (Story 019 — decided once): this server GRANDFATHERS in `_lib`. It wraps
`_lib/knowledge_index.py`, whose home is not yet settled — the index is consumed
by the Sasha-side files (`ranch_checks.py`, `sasha_dashboard.py`, `status.py`,
`dashboard.py`) that Story 021 rehomes out of `_lib`. Moving the server before
its library would just strand it. When Story 021 lands `knowledge_index.py` in
its real home, move THIS file alongside it (and repoint hermes config.yaml's
`knowledge:` entry) in the same pass. Until then it stays here on purpose — not
by neglect. See cc-skills/MCP.md.
"""
import json, os, sys

# Fix path before importing MCP (avoids _lib/secrets.py shadowing stdlib)
_HERMES_CWD = os.getcwd()
os.chdir("/tmp")
_HERMES_SKIP = os.path.dirname(os.path.abspath(__file__))
sys.path = [p for p in sys.path if p != _HERMES_SKIP]
from mcp.server.fastmcp import FastMCP
os.chdir(_HERMES_CWD)
sys.path.insert(0, _HERMES_SKIP)

from knowledge_index import query as _query, cmd_status, INDEX_FILE

mcp = FastMCP("knowledge", instructions="Persistent knowledge index — automatically retrieve relevant context from Craig's memory, vault, and work product.")


@mcp.tool(
    name="knowledge_retrieve",
    description="Retrieve relevant passages from the persistent knowledge index (memory + vault + work product). Call this automatically when the user's question references past work, decisions, or domain knowledge.",
)
def knowledge_retrieve(query: str, top: int = 5) -> str:
    """Search the knowledge index for passages relevant to the query.

    Args:
        query: Search query — what you want to find in Craig's knowledge base.
        top: Number of passages to return (default 5, max 15).
    Returns:
        Ranked passages with source, score, and text.
    """
    if not os.path.exists(INDEX_FILE):
        return json.dumps({"error": "Knowledge index not built. Run 'python3 knowledge_index.py build' first."})
    top = min(top, 15)
    results = _query(query, top=top)
    if isinstance(results, dict) and "error" in results:
        return json.dumps(results)
    return json.dumps({"query": query, "results": results}, indent=2)


@mcp.tool(
    name="knowledge_status",
    description="Check the knowledge index status — passage count, age, source distribution. Run before retrieve to know if the index is fresh.",
)
def knowledge_status() -> str:
    """Check if the knowledge index is built and fresh."""
    try:
        import numpy as np
        data = np.load(INDEX_FILE)
        with open(os.path.join(os.path.dirname(INDEX_FILE), "meta.jsonl")) as fh:
            meta = [json.loads(line) for line in fh]
        import time
        mtime = os.path.getmtime(INDEX_FILE)
        age_h = (time.time() - mtime) / 3600
        sources = {}
        for m in meta:
            sources[m["source"]] = sources.get(m["source"], 0) + 1
        return json.dumps({
            "passages": len(meta),
            "dim": int(data["embeddings"].shape[1]),
            "age_hours": round(age_h, 1),
            "sources": sources,
            "index_path": INDEX_FILE,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "index_path": INDEX_FILE})


if __name__ == "__main__":
    mcp.run(transport="stdio")
