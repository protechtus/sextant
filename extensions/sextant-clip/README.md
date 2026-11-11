# Sextant Clip

One click sends the AI web chat you are looking at -- claude.ai, ChatGPT,
Gemini, or any page at all -- into your local Sextant memory. No more
begging the model to "generate a markdown file to download."

Everything stays on your machine: the extension talks only to your own
dashboard on `127.0.0.1`, which drops the conversation into the same
transcript pipeline as `sextant-transcripts ingest` (indexed and
searchable immediately; originals archived, never deleted).

## Install (unpacked -- no store account needed)

1. Create a dashboard key if you have not already (the dashboard refuses
   to start without one):

   ```powershell
   # Windows PowerShell
   $env:ATLAS_WEB_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
   echo $env:ATLAS_WEB_API_KEY   # copy this for step 4
   ```

   ```bash
   # Linux / macOS
   export ATLAS_WEB_API_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
   echo $ATLAS_WEB_API_KEY
   ```

   Make it stick past this terminal window:

   ```powershell
   setx ATLAS_WEB_API_KEY "$env:ATLAS_WEB_API_KEY"   # Windows: new terminals keep it
   ```

   ```bash
   echo "export ATLAS_WEB_API_KEY=$ATLAS_WEB_API_KEY" >> ~/.bashrc   # Linux/macOS
   ```
2. Start the dashboard in that same shell: `sextant-web` (it hosts the
   inbox endpoint).
3. Chrome/Edge/Brave: `chrome://extensions` -> enable Developer mode ->
   "Load unpacked" -> select this `sextant-clip/` directory.
4. Click the extension's options and paste the key from step 1.
5. Open a chat on claude.ai / chatgpt.com / gemini.google.com and click
   the toolbar button. The badge shows how many turns landed ("N/A" means
   the current tab is a browser-internal page that cannot be scraped).

## What it captures

Per-provider DOM scraping with role attribution (user vs assistant).
Chat sites redesign constantly; when the selectors drift, Clip falls
back to capturing the whole visible page as a single document turn --
lossless beats schema-perfect. `source` is tagged (`claude-web`,
`chatgpt-web`, `gemini-web`, `web-page`) so you can tell captures apart
in search.

## Security posture

- Permissions the manifest asks for: `activeTab` and `scripting` (inject
  the scraper into the tab you clicked on, only then), `storage` (your
  endpoint and key), and host access to `http://127.0.0.1/*` and
  `http://localhost/*` (where the dashboard lives). Nothing else.
- The API key lives in `chrome.storage.local` (plain text inside your
  browser profile, like every extension setting), sent only to the endpoint
  you configured (loopback by default).
- The scraper injects ONLY when you click the button (`activeTab`), never
  runs in the background, and reads nothing on its own.
- The inbox endpoint is `POST /api/inbox/conversation` on the dashboard,
  body `{"title": "...", "source": "claude", "messages": [{"role": "user",
  "content": "..."}, ...]}` (content must be a string), header
  `Authorization: Bearer <ATLAS_WEB_API_KEY>`. It requires header auth (the
  dashboard's cookie is deliberately NOT accepted on POSTs), so a malicious
  page cannot push content into your corpus. Any script can use it the same
  way; the quick single-fact route is `POST /api/memory/capture` with
  `{"fact": "...", "scope": "reference:topic"}`.

Firefox note: MV3 support works but `chrome.*` -> `browser.*` shims and a
`gecko` id are needed for a signed build; unpacked temporary loading works
for testing. A store-published build (Chrome Web Store / AMO) is tracked
on the ROADMAP -- listing requires developer accounts, privacy
disclosures, and review cycles, none of which block personal use of the
unpacked extension today.
