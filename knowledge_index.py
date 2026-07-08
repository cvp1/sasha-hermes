#!/usr/bin/env python3
"""Knowledge index — warm vector store over memory, vault, and work product.

Builds and queries a local vector index using nomic-embed-text on .21.
No external vector DB needed — the index is small enough for numpy + cosine.

Usage:
    python3 knowledge_index.py build     # (re)build the index from all sources
    python3 knowledge_index.py query "what do I know about peppers" --top 5
    python3 knowledge_index.py status    # index freshness and size
"""
import json, os, re, sys, time, urllib.request
from pathlib import Path

HOME = os.path.expanduser("~")
CC = os.path.join(HOME, "Github", "CC")
INDEX_DIR = os.path.join(os.path.expanduser("~"), ".local", "state", "cc", "knowledge")
os.makedirs(INDEX_DIR, exist_ok=True)

EMBED_URL = "http://192.168.86.21:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"

INDEX_FILE = os.path.join(INDEX_DIR, "index.npz")
META_FILE = os.path.join(INDEX_DIR, "meta.jsonl")

# Sources to index
SOURCES = {
    "mem": os.path.join(HOME, ".claude/projects/-home-cvande-Github-CC/memory"),
    "vault": os.path.join(HOME, "notes"),
    "work": os.path.join(CC),
}


def _embed(text):
    """Get embedding vector from .21."""
    body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode()
    req = urllib.request.Request(EMBED_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    return resp.get("embedding", [])


def _chunk_text(text, max_chars=800):
    """Split text into overlapping chunks at paragraph boundaries."""
    if len(text) <= max_chars:
        return [text.strip()]
    chunks = []
    paragraphs = text.split("\n\n")
    current = ""
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if len(current) + len(p) + 2 > max_chars and current:
            chunks.append(current.strip())
            current = p
        else:
            current = (current + "\n\n" + p) if current else p
    if current:
        chunks.append(current.strip())
    return chunks or [""]


def _collect_docs():
    """Yield (source, kind, path, text) for every document to index."""
    # Memory files
    mem_dir = Path(SOURCES["mem"])
    if mem_dir.exists():
        for f in sorted(mem_dir.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                if len(text) > 30:
                    yield ("mem", "memory", str(f), text)
            except Exception:
                pass

    # Vault notes (Obsidian, .md files)
    vault_dir = Path(SOURCES["vault"])
    if vault_dir.exists():
        for f in sorted(vault_dir.rglob("*.md")):
            if ".trash" in str(f) or "_inbox" in str(f):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                # Strip YAML frontmatter
                text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
                if len(text) > 50:
                    yield ("vault", "note", str(f.relative_to(vault_dir)), text)
            except Exception:
                pass

    # Work product — key files from CC workspace
    work_dirs = [
        ("ai-os-pm", ["BACKLOG.md", "STRATEGY.md"]),
        ("career-mgmt", ["CAREER.md", "pipeline.json"]),
        ("board", ["profiles/"]),
    ]
    for rel, names in work_dirs:
        base = Path(CC) / rel
        for name in names:
            path = base / name
            if path.exists():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    yield ("work", rel, str(path), text)
                except Exception:
                    pass
            # Directory of files
            if path.is_dir():
                for f in sorted(path.glob("*.md")):
                    try:
                        text = f.read_text(encoding="utf-8", errors="replace")
                        yield ("work", rel, str(f), text)
                    except Exception:
                        pass


def cmd_build():
    """Build the index from all sources."""
    import numpy as np

    print("Collecting documents...", file=sys.stderr)
    docs = list(_collect_docs())
    print(f"  Found {len(docs)} documents", file=sys.stderr)

    all_embeddings = []
    all_meta = []
    total = len(docs)

    for i, (source, kind, path, text) in enumerate(docs):
        chunks = _chunk_text(text)
        for ci, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            try:
                vec = _embed(chunk)
            except Exception as e:
                print(f"  [{i+1}/{total}] {source}/{kind}: embed error: {e}", file=sys.stderr)
                continue
            all_embeddings.append(vec)
            all_meta.append({
                "source": source, "kind": kind, "path": path,
                "chunk": ci, "text": chunk[:500],
            })
        if (i + 1) % 20 == 0:
            print(f"  [{i+1}/{total}] ...", file=sys.stderr)

    if not all_embeddings:
        print("ERROR: no embeddings generated", file=sys.stderr)
        return 1

    arr = np.array(all_embeddings, dtype=np.float32)
    np.savez_compressed(INDEX_FILE, embeddings=arr)
    with open(META_FILE, "w", encoding="utf-8") as fh:
        for m in all_meta:
            fh.write(json.dumps(m, ensure_ascii=False) + "\n")

    print(f"\nIndex built: {len(all_embeddings)} passages, "
          f"embedding dim={arr.shape[1]}", file=sys.stderr)
    print(f"  Index: {INDEX_FILE}", file=sys.stderr)
    print(f"  Meta:  {META_FILE}", file=sys.stderr)
    return 0


def cmd_query(query, top=5):
    """Query the index."""
    import numpy as np

    if not os.path.exists(INDEX_FILE):
        return json.dumps({"error": "index not built. Run 'knowledge_index.py build' first."})

    # Load index
    data = np.load(INDEX_FILE)
    embeddings = data["embeddings"]
    with open(META_FILE) as fh:
        meta = [json.loads(line) for line in fh]

    # Embed query
    qvec = _embed(query)
    if not qvec:
        return json.dumps({"error": "query embedding failed"})
    qarr = np.array(qvec, dtype=np.float32).reshape(1, -1)

    # Cosine similarity
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    sims = (embeddings @ qarr.T).flatten() / (norms.flatten() * np.linalg.norm(qarr) + 1e-8)

    # Top-K
    top_k = min(top, len(sims))
    idx = np.argpartition(sims, -top_k)[-top_k:]
    idx = idx[np.argsort(-sims[idx])]

    results = []
    for i in idx:
        results.append({
            "score": float(round(sims[i], 4)),
            "source": meta[i]["source"],
            "kind": meta[i]["kind"],
            "path": meta[i]["path"],
            "text": meta[i]["text"],
        })
    return json.dumps({"query": query, "results": results}, indent=2)


def cmd_status():
    """Show index status."""
    if not os.path.exists(INDEX_FILE):
        print("Index not built.", file=sys.stderr)
        return 1
    import numpy as np
    data = np.load(INDEX_FILE)
    with open(META_FILE) as fh:
        meta = [json.loads(line) for line in fh]
    sources = {}
    for m in meta:
        s = m["source"]
        sources[s] = sources.get(s, 0) + 1
    mtime = os.path.getmtime(INDEX_FILE)
    age_h = (time.time() - mtime) / 3600
    print(f"Passages: {len(meta)}", file=sys.stderr)
    print(f"Dim: {data['embeddings'].shape[1]}", file=sys.stderr)
    print(f"Age: {age_h:.1f}h (built {time.ctime(mtime)})", file=sys.stderr)
    for s, cnt in sorted(sources.items()):
        print(f"  {s}: {cnt} passages", file=sys.stderr)
    return 0


def query(query, top=5):
    """Programmatic query interface — returns parsed results for MCP server."""
    result_str = cmd_query(query, top=top)
    data = json.loads(result_str)
    if "error" in data:
        return data
    return data["results"]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Knowledge index — warm vector store")
    ap.add_argument("action", choices=["build", "query", "status"])
    ap.add_argument("query_str", nargs="?", default="", help="query string")
    ap.add_argument("--top", type=int, default=5, help="top-K results")
    args = ap.parse_args()

    if args.action == "build":
        raise SystemExit(cmd_build())
    elif args.action == "query":
        if not args.query_str:
            print("error: query string required", file=sys.stderr)
            raise SystemExit(1)
        print(cmd_query(args.query_str, top=args.top))
    elif args.action == "status":
        raise SystemExit(cmd_status())
