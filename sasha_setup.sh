#!/usr/bin/env bash
# Sasha setup — run this in your own terminal to wire up AI-OS infra.
# Usage: bash ~/sasha_setup.sh

echo "=== Sasha setup ==="

# Copy skills (if not already done by Dex)
SKILLS_SRC=/home/cvande/.hermes/skills
SKILLS_DST=$HOME/.hermes/skills
for skill in board capture improve teach wiki recall triage ingest secret backup restore workflow-visualizer firealert product; do
  if [ -d "$SKILLS_DST/$skill" ]; then
    echo "  skill $skill: exists"
  elif [ -d "$SKILLS_SRC/$skill" ]; then
    cp -r "$SKILLS_SRC/$skill" "$SKILLS_DST/$skill"
    echo "  skill $skill: copied"
  fi
done

# Register MCP servers
MCP_SERVERS=(
  "recall:/home/cvande/Github/CC/recall/recall_mcp_server.py"
  "wiki:/home/cvande/Github/CC/wiki/wiki_mcp_server.py"
  "garden:/home/cvande/Github/CC/garden/garden_mcp_server.py"
  "board:/home/cvande/Github/CC/board/board_mcp_server.py"
  "cost:/home/cvande/Github/CC/observability/cost_mcp_server.py"
  "firealert:/home/cvande/Github/CC/fire-alert/firealert_mcp_server.py"
  "local_llm:/home/cvande/Github/CC/ollama-tools/local_llm_mcp_server.py"
  "knowledge:/home/cvande/Github/CC/sasha-hermes/knowledge_mcp_server.py"
)

for entry in "${MCP_SERVERS[@]}"; do
  name="${entry%%:*}"
  path="${entry##*:}"
  if [ -f "$path" ]; then
    # Check if already registered
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
