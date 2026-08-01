# sasha-hermes — open questions

Unresolved measurements about this harness. Home for questions that are real but
not yet answered, so they stop occupying an always-on memory row (a row that
says "we don't know" costs context every session and delivers nothing an
on-demand read wouldn't).

## Per-turn prompt tax: built-in vs MCP split is UNRESOLVED

**What is measured:** hermes carries roughly 19–22k tokens of per-turn prompt
overhead. That number is real and reproduced.

**What is NOT measured:** how much of it is built-in system prompt versus MCP
tool definitions. Two reasons the obvious instrument fails:

1. **Prompt-size measurement is blind to MCP by construction** — the tool
   schemas are injected downstream of the point the prompt is sized, so a
   prompt-size probe cannot see them at all.
2. **Live token counts vary more than the quantity being measured.** Run-to-run
   variance on the same turn exceeds the size of the split we are trying to
   resolve, so a difference of two live runs is not evidence.

**What would settle it:** an instrument that reads the assembled request as the
API receives it, with the MCP server set as the only variable and enough repeats
to clear the observed variance. Until that exists, any attribution of the tax to
"MCP bloat" or to "the built-in prompt" is a guess wearing a number.

**Why it matters:** the fix differs completely. If it is MCP, trimming the
server fleet reclaims it; if it is the built-in prompt, trimming servers buys
nothing and the effort is wasted.

*Recorded 2026-08-01, moved out of the always-on memory index.*
