#!/usr/bin/env bash
# Copy hermes skills and register MCP servers.
# Usage: SASHA_SKILLS_SRC=/path/to/skills CC_ROOT=/path/to/CC bash ~/sasha_setup.sh

echo "=== Sasha setup ==="

SKILLS_SRC="${SASHA_SKILLS_SRC:-/path/to/skills}"
CC_ROOT="${CC_ROOT:-$HOME/Github/CC}"
SKILLS_DST=$HOME/.hermes/skills
for skill in board capture improve teach wiki recall triage ingest secret backup restore workflow-visualizer firealert product; do
  if [ -d "$SKILLS_DST/$skill" ]; then
    echo "  skill $skill: exists"
  elif [ -d "$SKILLS_SRC/$skill" ]; then
    cp -r "$SKILLS_SRC/$skill" "$SKILLS_DST/$skill"
    echo "  skill $skill: copied"
  fi
done

MCP_SERVERS=(
  "recall:$CC_ROOT/recall/recall_mcp_server.py"
  "wiki:$CC_ROOT/wiki/wiki_mcp_server.py"
  "garden:$CC_ROOT/garden/garden_mcp_server.py"
  "board:$CC_ROOT/board/board_mcp_server.py"
  "cost:$CC_ROOT/observability/cost_mcp_server.py"
  "firealert:$CC_ROOT/fire-alert/firealert_mcp_server.py"
  "local_llm:$CC_ROOT/ollama-tools/local_llm_mcp_server.py"
  "knowledge:$CC_ROOT/sasha-hermes/knowledge_mcp_server.py"
)

for entry in "${MCP_SERVERS[@]}"; do
  name="${entry%%:*}"
  path="${entry##*:}"
  if [ -f "$path" ]; then
    if ! grep -q "$name" "$HOME/.hermes/config.yaml" 2>/dev/null; then
      echo "y" | hermes mcp add "$name" --command python3 --args "$path"
      echo "  mcp $name: registered"
    else
      echo "  mcp $name: already registered"
    fi
  else
    echo "  mcp $name: script missing ($path)"
  fi
done

echo ""
echo "=== Sasha ready ==="
echo "  Skills: $(ls ~/.hermes/skills/ | wc -l) total"
echo "  MCP:    $(grep -c 'enabled: true' ~/.hermes/config.yaml) servers"
echo ""
echo "  Start a session:  hermes"
