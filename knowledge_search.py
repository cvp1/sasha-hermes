#!/usr/bin/env python3
"""Knowledge search — embed a query, cosine-sim against the index, return top results.

Usage:
    python3 knowledge_search.py "your query here" [--top 10] [--min-score 0.3]

Outputs JSON to stdout: {"query":"...","results":[{"score":0.85,"text":"...","source":"...","path":"..."},...]}
"""
import json, os, sys, urllib.request, argparse
import numpy as np

CC = os.path.join(os.path.expanduser("~"), "Github", "CC")
INDEX_DIR = os.path.join(CC, "_lib", "knowledge_index_data")
EMBED_URL = "http://192.168.86.21:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"

def embed(text):
    body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode()
    req = urllib.request.Request(EMBED_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["embedding"]

def search(query, top=10, min_score=0.3):
    # Load index
    idx = np.load(os.path.join(INDEX_DIR, "index.npz"))
    embeddings = idx["embeddings"]  # (N, 768)

    # Load metadata
    metas = []
    with open(os.path.join(INDEX_DIR, "meta.jsonl")) as f:
        for line in f:
            metas.append(json.loads(line))

    # Embed query
    q_vec = np.array(embed(query), dtype=np.float32)

    # Cosine similarity (normalized dot product)
    norms = np.linalg.norm(embeddings, axis=1)
    q_norm = np.linalg.norm(q_vec)
    if q_norm == 0:
        return []
    scores = np.dot(embeddings, q_vec) / (norms * q_norm + 1e-10)

    # Get top indices above threshold
    indices = np.where(scores >= min_score)[0]
    order = indices[np.argsort(-scores[indices])][:top]

    results = []
    for i in order:
        m = metas[i]
        text = m.get("text", "")[:300]
        # Truncate to first sentence-ish
        text = text.strip()[:250]
        results.append({
            "score": round(float(scores[i]), 3),
            "text": text,
            "source": m.get("source", ""),
            "kind": m.get("kind", ""),
            "path": m.get("path", ""),
        })
    return results

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", help="Search query (or read from stdin)")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--min-score", type=float, default=0.3)
    args = ap.parse_args()

    query = args.query
    if not query:
        query = sys.stdin.read().strip()
    if not query:
        print(json.dumps({"error": "no query"}))
        return 1

    try:
        results = search(query, top=args.top, min_score=args.min_score)
        print(json.dumps({"query": query, "results": results, "count": len(results)}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
