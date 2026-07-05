# Graduating to Sasha on hermes (the web chat)

You've been using **Sasha on Claude Code** — the copy-paste setup. This layer
gives the *same* Sasha a warm web page: a greeting, plain-word buttons, and a
real chat in your browser. **You lose nothing by adding it**: both surfaces
read and write the same passport — `~/ai-os/me/WHOAMI.md` and
`~/ai-os/me/HOW-I-WORK.md` — so the Sasha in the browser already knows who you
are and how you like things.

**Honest prerequisites, up front**
- A machine that stays on when you want the page up. A laptop works
  (`run-mac.sh`, page stops when it sleeps); a small always-on Linux box is
  the household setup (`install.sh`, runs as services).
- hermes needs **its own model key** (OpenRouter / DeepSeek / a local model) —
  separate from your Claude plan. Costs are pay-per-use and small, but real.
- This layer has run in exactly one household so far. Expect rough edges;
  the README's "known first-run issues" section is the medicine cabinet.

## Path A — your laptop (macOS or any machine, no root)

1. **Have (or make) your `me/` files.** If Sasha on Claude Code is already on
   this machine, they exist — done. If not, either run the product setup first
   (recommended: [the walkthrough](https://cvp1.github.io/ai-os/)) or start
   blank — Sasha seeds skeletons and learns as you talk.
2. **Install hermes** ([docs](https://hermes-agent.nousresearch.com/docs/)) —
   use the official installer; it brings its own Python and puts the command
   on PATH for all shells:
   ```sh
   curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
   ```
   Open a **new terminal**, then:
   ```sh
   hermes setup        # the wizard: pick a provider, paste its key
   ```
   *(Alternate: `pip install hermes-agent` works ONLY on Python 3.11–3.13 —
   Macs ship 3.9, where pip reports "package not found." If you do use pip
   and get "command not found" after: `export PATH="$(python3 -m site
   --user-base)/bin:$PATH"` in your ~/.zshrc.)*
3. **Get this repo, then run it:**
   ```sh
   gh repo clone cvp1/sasha-hermes    # or: git clone https://github.com/cvp1/sasha-hermes.git
   cd sasha-hermes
   ./run-mac.sh --name YourName --place "Home"
   ```
   (Private repo → needs auth: `gh auth login` once, or a GitHub personal
   access token as the git password. Public clone works with neither.)
   It prints your sign-in once, wires the passport bridge into hermes, starts
   the gateway + the page, and opens at `http://127.0.0.1:7790/`. Ctrl-C stops it.
4. **Prove the graduation:** ask the web Sasha *"what do you know about me
   from your files?"* — it should answer from the same `me/` files your
   Claude Code Sasha wrote. Correct it ("shorter, please") and check
   `~/ai-os/me/HOW-I-WORK.md` — the correction lands in the file both
   surfaces honor.

## Path B — a household box (Linux, systemd)

Same as A, but hermes is installed for the target user and the layer runs as
services (auto-start, LAN access, per-user instances, terminal fallback):
```sh
sudo ./install.sh --user alice --name Alice --place "Home"
```
See the README for what the installer verifies (loopback-only backends, auth).

## The passport across machines

Same machine → automatic (both surfaces read the same folder).
Two machines (product on your laptop, web layer on a server) → the passport
doesn't teleport: copy `~/ai-os/me/` over, or keep `~/ai-os` in a cloud-synced
folder on both. Both Sashas are told they are not the only writer — they
re-read before writing and merge rather than overwrite.

## Status

Path A (macOS laptop) is being validated on its first real machine now; this
document gets patched wherever reality disagrees with it.
