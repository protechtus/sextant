# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

**Note on version numbering:** The package version is `0.1.0`; its first PyPI release
follows the `v0.1.0` tag.
The milestone labels `0.2` through `0.17` below are *internal development milestones*
(sprint arcs, labelled v0.2-D, v0.3-A ... v0.17-CZ, etc.) used to track incremental
progress during continuous development. They are not published package releases. This
history is condensed from the maintainers' per-sprint log, which is not part of the
public tree.

---

## [Unreleased]

### Sweep 16, agentic wave (2026-11-13 evening)

- Redaction: camelCase and long-prefixed key names (`dbPassword`, `MY_APP_PROD_EU_WEST_DB_PASSWORD`), `SECRET_KEY_BASE`, `*_HASH`, `*_v2`, `bearer`, `csrftoken`, `credentials`, Korean and Chinese key names, quoted values with spaces, CLI flags (`--token`), `Authorization: Basic`, passwords holding `@` in URLs; full-width folding touches only the masked value; a placeholder like `********` is left alone.
- The dashboard and the sync hub count an auth failure against the forwarded client only (the proxy's other clients no longer inherit it), ignore a forwarded value that is not an IP address, and lock a listed proxy after ten times the per-client limit when its clients rotate names; the sync hub reads `ATLAS_SYNC_TRUSTED_PEERS` and logs a lockout; compose passes the list to it. The MCP 401 body no longer carries the token fingerprint (the startup log does).
- Sync: devices pull `_atlas/routing.yaml` again (only the hub refuses a push of it); the client prints why a file was denied.
- Stop hook: the summary is refreshed in `updates.txt` too and across local midnight (one entry per session); a plain-text model reply is a failed run, not an entry; the facts stage has its own per-session lock and treats a non-JSON reply as a failed run (no `.done`); a mistyped gate value is reported; the review queue is deduplicated and capped; the exact-dedupe rule keeps letters of every script and sign characters (`-5`, `C++`, `€`).
- Console: text to a Windows console goes through the console stream again (the code-page bytes of the previous build showed replacement characters); pipes get the code page, hooks UTF-8. `review report` and `import` survive a C locale (the import refuses, with the reason, instead of writing duplicates); doctor names the locale instead of the files; `atlas-tickets` output is pipe-safe and a bad project name is one sentence with its characters; `alpha:beta` is two search words.
- Stop hook (MN39): a single-chunk session (the map schema carries no title) is a summary again (every short session had been refused as "not a summary"); a junk or echoed map reply is never cached; `--dry-run` writes no cache; `ATLAS_SESSION_AUTONOMOUS=off` is a silent opt-out and values are stripped; a failed facts stage records `suggest_facts_failed`; doctor's last event is its own line with the full hint; the facts script is found on PATH and in `~/.local/bin`; fsdiff snapshots are capped at 200.
- Versions: `scripts/bump_version.py` (`--part`, `--set`, `--release`, `--check`) is the one way to change the version; `tests/test_version_drift.py` fails when pyproject, the `__init__` fallback and the CHANGELOG headings disagree; public builds between tags carry `X.Y.Z.devN` and `sextant --version` names the source revision once the maintainer turns the stamp on (every build of 2026-11-13 had said 0.1.0 and `pip install -U` was a no-op); `docs/RELEASING.md` describes the scheme and who releases.
- Sweep 17 manual wave (MN44, MN45, MN47): the chat socket stops retrying on a page the Stop button ended (its handshakes locked the remote user out after a restart); `ATLAS_WEB_PANEL_CACHE` follows the on/1/true convention; an unreadable `SSL_CERT_FILE` is named by the sync client instead of a traceback; `sextant doctor` shows `suggest_facts_skipped`; a Stop payload that is not JSON is refused with a sentence instead of summarising the whole day; GETTING-STARTED's hand-test recipe says the `session_id` must be the transcript's own.
- Sweep 17 manual wave (MN43, the expert's fifth sitting): the mid-sentence prose rule reads a bounded window (it was quadratic on a long single line: 26 s on 256 KB); `--secret-key=`, `-Dpassword=`, `-ApiKey:` and other dashed flags are masked again; `dbPass`, `adminPwd`, `PGPASS` are key names again and only the English words (`bypass`, `compass`) are skipped; the dashboard's unchanged-note check over 1 MB is live (the clause had been inside a comment); the index format tag is `3` and the search path honours it; Hebrew maqaf, paseq and sof pasuq are no longer folded away; the duplicate scan's token table holds bit indexes (its memory was quadratic: 799 MB on 98,000 tokens); the breaker clears on any answer; the Brain-not-running panels name `ATLAS_BRAIN_URL`; one quoted prompt phrase is not an echo; `~~~` fences and a fence at the file start are recognised; two updates entries without a blank line between keep the second's rule; doctor cuts a long gate value.
- Sweep 17 manual wave (MN46): the index format tag is honoured on the search path (an upgrade now re-indexes once at the first search; it had been read by `sextant-fts-rebuild --if-stale` only); the duplicate scan folds combining marks and splits Thai-like runs (vocalised Arabic and Hindi notes had shattered and scored 1.00); token, API-key and secret names in Arabic, Hebrew, Thai and Hindi are masked and the Arabic and Hebrew names match with vowel marks between their letters; `remember` names the locale on a codec error in every branch; the similarity note and `--skip-similar` see every script and a dry run says which facts `--auto` would skip; the editor and snippets carry `dir="auto"` for right-to-left text.
- Sweep 17 (AG49, AG50, AG51, AG52): a `key: value` secret at the start of its line is masked again (the prose rule had unmasked YAML, INI and compose values), `bypass =`/`compass =` are not secrets, a YAML block scalar mask keeps sibling entries, the Arabic, Hebrew, Thai and Hindi words for password and `暗証番号`/`비밀키` are recognised, `[::1]` is an address; the chat WebSocket stops reconnecting on a dead session (each retry had counted as an auth failure and locked the remote user out) and a draft on a hidden tab counts as unsaved; a root-level ticket is named by its bare id even when a folder holds the same id; only a reply shaped like a chunk summary and not an echo of the prompt is cached (a JSON list crashed, JSON junk and echoed prompts were cached), items and titles are one line each, a quoted heading-plus-marker inside a code fence and a hand-written `NOTES:` line survive a refresh; doctor prints unmerged git paths in text, reads them NUL-separated, shows facts-stage events and names a bad gate value; a missing facts script and a payload without a session id leave a trace; `[1, 2, 3]` is a junk facts reply; the hub logs one lockout per peer; the sync client sanitises the hub's text; the exact fact dedupe and the duplicate scan tokenise every script (a fact with no ASCII was never "already there"; unrelated non-Latin notes scored 1.00); Thai, Lao, Khmer and Burmese runs are searched like CJK, Arabic/Hebrew marks are folded, and the index carries a format tag so the change re-indexes every note; `remember` in a C locale names the locale; `--check` refuses a dev build of a released version; the dashboard's peer probes share a 20 s circuit breaker (thirteen panels paid 1.2 s each per poll and stalled /health), the phone ticket list shows titles, tapping a ticket scrolls to its pane, an unchanged note over 1 MB is "No change". Performance (AG54): the duplicate scan compares notes as bitsets (`review report` on 5,000 notes was 148 s with the set intersection; a proof of concept ran in 16 to 44 s), every peer client and the Ollama client share one TLS context (building one per client cost 0.43 s on the dashboard's event loop, 27 times a page cycle), the summarizer keeps its chunk cache when a later call fails, dash-dense text redacts in under a second per megabyte, and a refused Brain connect says "not running" instead of a timeout name.
- Manual wave 16 (MN37, MN38, MN41, MN42): redaction masks quoted values of any length (an 837-character JWT leaked), unterminated quotes, `pwd=`/`DB_PASS=`, `--db-password`, `curl -u`, `Authorization: Token`, YAML block scalars and XML elements, leaves code expressions (`token = tokens[0]`) and `PWD=/path` alone, and the full-width pass is one join (97 s per MB before); a forwarded value that is not an IP is its own bounded source (it had switched the throttle off) and a port suffix is dropped; the sync hub has the proxy-flood rule and its audit names the forwarded device; the dashboard stops every poll when a dead session meets unsaved text (the 401s locked remote users out), ticket refs fall back to the bare id for root-level and moved tickets, the Memory tab stacks and the tab bar wraps on a phone, the ticket list hides the project column there, effort XL; a summary refresh removes only an entry whose heading is followed by the marker line and stops at the next entry (hand-written text was deleted); `_norm_fact` keeps combining marks and comparison signs; a plain-English "no facts" reply is an answer; weekly synthesis unpacks the candidates tuple; the sync client prints the hub's denial reason and names a certificate problem; doctor lists unmerged git paths; `atlas-tickets create` refuses a bad project with one sentence; `atlas-suggest-facts` output is pipe-safe; `review report --scope` has help and a hint; dev builds advance to the next patch once the current version is tagged.
- Performance (AG48, 5,000 notes): the duplicate scan bisects its postings and raises its working threshold to the cap's weakest pair (`review report` and `review duplicates` were 135 to 149 s); the JSON curation-report endpoint uses the panel's isolated worker and cache (it ran in-process and stalled search); the summarizer caches chunk summaries per session and re-runs only changed chunks (a 5,000-turn session paid 78 calls per refresh); untagged-scopes is cached.
- Dashboard: every ticket action carries `project/ID` (two projects with the same id acted on the wrong ticket); the form sets `requires`, `verified` and `effort`; a browser opening `/` with a dead session sees the expired page; an expired session never discards typed editor text; a lone-CR note is a no-op save; Brain Chat uses `wss://` behind TLS; the services cache clears after an action; detail panes stack on a phone.

### Contributor entries

(one bullet per change: what changed, for whom, PR number)

**Post-flip sweep 13, agentic wave (2026-11-13, the first wave against the public repository)**: the HTTP MCP server now honours `ATLAS_MCP_ALLOWED_HOSTS` and turns the Host check off on a wider bind (the SDK's DNS-rebinding guard answered 421 to every documented remote route: Caddy, cloudflared, LAN); an MCP caller can no longer claim `human-direct`/`human`/`telegram` provenance (it is written as `agent`, so high confidence stays the operator's); the dashboard in `--no-auth` mode refuses browser cross-site writes and chat sockets (a foreign Origin on a mutating request is 403); the ntfy command listener refuses a shared secret under 16 characters; primed text drops zero-width and bidi format characters, replaces an inline provenance comment with a space instead of gluing the words around it, quotes file names and project names in headings, and `sextant context` says when identity/user.md is still the template; `sextant-init` turns a permission error into one sentence naming the Docker uid; `sextant remember ''` is refused; `sextant search` before init creates no state dir; `sextant verify` accepts the whole registered command after `--command` and stamps the verification fact with the time so a second run is not an exact repeat; the MCP server logs at WARNING by default (`ATLAS_MCP_LOG_LEVEL=INFO` restores the per-request lines). Docs: the README's first screen is the four-command install and says the package is not on PyPI until `v0.1.0`; SECURITY.md names the trusted-peers exemption and the ntfy rule; SUPPORT.md drops the mailing list that never existed; the PowerShell stdin example parses; HOWTO-self-hosting and the cloudflared template name the Host setting. Packaging: the hybrid-search test no longer inherits `ATLAS_SEMANTIC_SEARCH=off` from the environment (the publish workflow's own test gate failed on it); the sdist grafts the whole `tests/` tree so the shipped tests run with their fixtures; Python 3.13 is a classifier (the wheel, every entry point, the dashboard and the suite were exercised on 3.13.13); `atlas-plus` from a package install no longer prints `docs/` paths that do not exist. Manual wave: the README's install block is one command per line with no `&&` (PowerShell 5.1 refused the whole line) and says that `sextant setup` prints the registration line rather than writing it; `sextant find` is an alias of `search`; Ctrl-C at an uninstall prompt prints one line (exit 130, nothing removed); related-fact snippets in primed text drop provenance comments. Later in the same wave: every entry in `ATLAS_MCP_ALLOWED_HOSTS` matches the bare name as well as `name:port` (a client on 443 sends no port, so the documented proxy and tunnel routes still answered 421), IPv6 entries are bracketed, names are case-folded, the 421 body and log name the setting, and `--no-auth` refuses a list; a proxy listed in `ATLAS_MCP_TRUSTED_PEERS` is trusted to forward the real client and a listed source is exempt through a proxy too; Recipe B sets `Cf-Connecting-Ip`; the MCP provenance check compares the value as written (`human--direct` no longer reaches the file as human-direct); primed text keeps ZWNJ/ZWJ and the LRM/RLM marks (Persian and emoji sequences were broken) and strips only bidi overrides, isolates, ZWSP and BOM; the provenance-comment pattern stays on one line; the project `Path:` line is quoted; one identity-template rule for the hook and `sextant context`; a non-UTF-8 note primes a one-line notice instead of mojibake; `sextant-init` reports a permission error on file writes too, with the Windows fix on Windows, and writes LF on every OS (Windows scaffolds and appends were CRLF, which made every synced file a conflict); notes are written with LF; `sextant remember -` refuses a pipe that arrived mangled (PowerShell 5.1 pipes ASCII) and names `$OutputEncoding`; empty facts are refused over MCP as well; the duplicate hint and the weekly duplicate report tokenise CJK bigrams (unrelated Japanese notes scored 1.00); a Japanese sentence that matches no phrase falls back to its bigrams; the dashboard can create tickets again; `--command` accepts `--transport stdio`, repeated `--stdio` and a `sextant-mcp` launcher; the verify fact carries seconds; the ntfy secret must be one word; the Windows-side WSL hook commands carry `MSYS_NO_PATHCONV=1`; `sextant-backup verify` passes on a remote whose folder does not exist yet; HOWTO-sync describes the CRLF conflict and the ten-second manifest cache; HOWTO-self-hosting adds the WSL linger note and a 421 line. Dashboard (sweep 14): Edit loads the whole file so a save never truncates a note over 8,000 characters (and the viewer says when it is cut); the header's file count ignores version copies; a stale session cookie no longer feeds the lockout and a tab that gets 401 leaves for `/expired` instead of polling the owner out through a tunnel; the curation panels count the "Repeated facts" section (the Brain and Telegram summaries too); an "Auth failures" panel exists; the ticket form reports a refused ticket instead of "created: undefined" and keeps the text; the status bar honours `ATLAS_MCP_URL` and `OLLAMA_BASE_URL`; dead peers answer in under a second (connect timeout); the ticket and file-write routes answer 400 with a sentence on bad JSON, a missing field or a too-deep body; an empty search shows the corpus layout instead of blaming `ATLAS_MEMORY_ROOT`; creating a ticket with no corpus is refused like a capture; the capture dropdown no longer offers the maintainer's own project. Transcripts and the Stop hook: the summarizer module gained its entry point (the hook's child had been a silent no-op on every install), the hook probes the same Ollama the summarizer uses (`ATLAS_OLLAMA_URL` first) and summarises the finished session only (`--conversation-id` from the Stop payload); transcript mining targets this workspace's own Claude Code project folder (it was hard-coded to the maintainer's); `sextant-transcripts search` prints previews and retries a non-FTS5 query as a phrase; the drop importer reads UTF-8 BOM and UTF-16 exports and accepts Claude Code's own JSONL line shape; a bad `--since` is one line. Resilience: `sextant search` says when the index could not be opened (disk full, read-only state dir) instead of "No hits"; `sextant-fts-rebuild` fails in one line; a zombie lock holder counts as dead; a lock whose token could not be written is removed; doctor's disk row measures the state dir's filesystem. Locales: `İstanbul` and `istanbul` are one note; the leap-day crash in `review unconfirmed-decay`; POSIX non-UTF-8 stdout replaces instead of raising. Imports at scale: a vault file named `aux.md` or `con.md` is skipped and named instead of blocking the import forever on Windows; doctor's inbound-reference scan matches multi-word note names as token n-grams in one pass per file (8 minutes on a 3,000-note vault, now seconds); a ChatGPT or Claude conversation that gained turns is updated in place behind a version copy instead of being skipped as "an older build"; notes cut at 4,000,000 characters are listed under `truncated` in the report; imported notes carry an `import_vault` stamp and `--archive-stale` judges staleness per vault; `sextant-backup` passes `--no-unicode-normalization` so NFC and NFD siblings both survive, and a fresh state dir counts as a real source; GETTING-STARTED says all of this and names the restore command. Scale (100,000 facts, 5,000 notes): the file-level duplicate scan in `review report` takes its candidates from an inverted index and reads snippets only for the pairs it keeps (it was killed at 621 s and 1.4 GB); history copies can be coalesced within `ATLAS_VERSION_MIN_INTERVAL_SECONDS` (opt-in; the default is a copy per write); every frontmatter read in curation, the weekly report and the dashboard uses libyaml when present. Shared corpora: `remember` refuses to write into a note that holds git conflict markers, autocommit stays out of an open merge, init writes a `.gitattributes` (LF) and ignores the sync conflict and inbox folders; the docs say plainly what two people on one corpus get (hand merges, no per-user identity, `source` is a claim); CONTRIBUTING names the audit and packaging lanes and the style and commit expectations, the PR template and CLA have an actionable path and "n/a" escapes, SUPPORT promises no business-day turnaround, NOTICE carries the LGPL third-party notice for the optional Telegram extra. Expert review of the day's fixes (MN25): the three curation-count parsers are one `curation_counts()` (two of the hand copies crashed on every run); the Stop hook reads its payload whether or not `ATLAS_CLAUDE_CODE_ROOT` is set, so one Stop summarises one session; the summarizer and suggest-facts redact keys before prompting and before writing, name the finished session (a re-fired Stop writes nothing twice), use the Brain's model by default and record `summary_written` / `summarizer_failed` in the activity log; the hook's probe says when the model is not pulled; `sextant-transcripts search` escapes Rich markup, quotes tokens on a retry and scans for CJK; `.txt`/`.md` drops read UTF-16 and BOMs; an unopenable search index is reported over MCP and in the dashboard; a loose CJK bigram match is never above `low`; a C locale's argv is re-decoded; dashboard raw loads keep CRLF bytes and answer 404 for a missing note; JSON bodies with a list or object field answer 400; `/expired` answers HEAD; the status bar and the summarizer probe the same Ollama endpoint; an import claims its unchanged notes so `--archive-stale` can tell whose a note is; ChatGPT notes carry `conversation_id`, so a retitled export is one note; the orphan n-gram path takes only stems made of tokens and spaces; the first write to a CRLF note rewrites it as LF (a version copy keeps the old bytes). Returning user (MN30): identity facts remembered under the init template are primed (the hook read 400 characters); a `git+` install needs `--force-reinstall --no-deps` to update, the docs say so; the repeated-facts count says when it is capped; `atlas-tickets ... | head` ends quietly. History copies are made on every write again (`ATLAS_VERSION_MIN_INTERVAL_SECONDS` is opt-in). Dashboard day two (MN29): the Tailwind tag drops `integrity`/`crossorigin` (cdn.tailwindcss.com sends no CORS header, so every real browser rendered the page unstyled) and the offline banner names the library that failed; a new project's ticket prefix is unique across projects (`proj1` and `proj2` both minted `PRO-001`; `project/ID` addresses an old collision); every Brain call uses a 0.75 s connect timeout (a dead peer cost 2 to 5 s per panel); the editor sends the etag of what it loaded and a note another writer changed since is a 409 instead of silently overwritten, an unchanged save writes nothing, a non-UTF-8 note is refused for editing, and a failed save shows the reason; the stop button reports a refusal; the browse tree hides `versions/` and state folders; a repeated-word query is one word to the index; the six remaining raw JSON routes answer 400 on a bad body; `atlas-tickets create` is interactive like the form; refused attempts show as `locked_out` in the auth panel; the empty security card says why. Sweep 15, agentic wave (the third post-flip wave): refused-while-locked attempts are counted per source outside the failure log (they had evicted the counted failures and lifted the lock after about 500 requests); redaction covers `NAME_PASSWORD=`/`NAME_SECRET=` shapes, AWS, Slack, Stripe, GitHub and Google keys and PEM private-key blocks; an agent-sourced MCP write is capped below `high`; the incoming Host header is case-folded and its trailing dot dropped before the allowed-hosts check; `get_ticket("project/ID")` applies the one project-name rule (`C:` escaped the tickets root on Windows) and a project may not start with `_`; one conflict-marker rule for the writer, the MCP tools and doctor (BOM, tab, bare markers), and a FACT that would introduce markers is refused instead of bricking its note; `memory_health` is degraded when the state dir is a file or the index cannot open; `tickets_status_tool` is an operator action like create; `sextant setup --out` creates parent folders and `--client hooks` prints the gate hint; `ATLAS_SESSION_AUTONOMOUS=on` reaches the fact writer (suggest-facts wrote nothing under the documented gate), suggest-facts redacts before it cuts a turn, runs once per session, skips a fact already on a line of the target note and never auto-writes `identity`; the summarizer checks the gate and the session marker BEFORE any model call and takes the whole session rather than `--since today`; a 404 from Ollama names the missing model; the probe accepts the exact tag only; the session-start hook primes the newest summary (it skipped a log whose first line was the entry) without the marker line; the prompts fence the transcript as data; an import judges only its own vault's notes (two vaults with a `README.md` ping-ponged under `--archive-stale`); a renamed conversation that also gained turns is updated in place; the editor keeps a note's CRLF and BOM through a save; JSON bodies over 2 MB are 413 and booleans or non-finite numbers are 400; the Pending Facts panel says the Brain is optional instead of a bare `Error:`; compose names `ATLAS_MCP_TRUSTED_PEERS`; the backup refusal points at the volume recipe. Performance round four (AG42, 5,000 notes): the index deletes a changed note's row by the rowid it recorded instead of a scan of the FTS table per note (a corpus whose mtimes all changed, after a pull, sync, restore or copy, stalled the first search for 2 to 11 minutes); the file-level duplicate scan uses prefix filtering on document frequency (exact, and a fraction of the posting-list walk that still cost 150 to 190 s); the file-level heading and the CLI say when the 500-pair cap was hit; the repeated-facts section shows at most three lines per note pair; `downgrade_stale_confirmations` reads frontmatter with libyaml; `/api/agents` has the connect timeout its siblings got; the dashboard's curation panel computes its report in a spawned worker process (`ATLAS_WEB_REPORT_SUBPROCESS`), so a long report no longer stalls search and the other panels.

**Pipeline sweep, agentic wave (2026-11-11)**: six more strangers (uv-only PowerShell with a non-ASCII path, a wheel package user, WSL reached from Windows, a container in HTTP mode, the returning user, Claude Desktop JSON on a hostile path). Fixed: `sextant verify` before init no longer makes init refuse its own directory (the server's runtime artifacts count as Sextant state); `verify --env NAME=VALUE` carries the registration's variables to a `wsl` launcher through WSLENV, and setup prints a `Verify from Windows` line and Windows-side hook commands for a WSL install; WSLENV appends drop empty entries; wsl.exe's UTF-16 errors are decoded; captured CLI output is UTF-8 on Windows and setup warns on non-ASCII paths; the doctor is plain text when piped or under NO_COLOR; `--version` reports the installed distribution; the uninstall report sees `CLAUDE_CONFIG_DIR`, Claude Desktop and Cursor and names the confirm flags; the publish-tooling test skips in the public tree; PyPI metadata gains a Documentation URL and the README badge links out; `uv.lock` ignored; the session-start hook distinguishes a commit-less repo from a missing git.

**Pipeline sweep, agentic wave, round 2 (2026-11-11)**: the Windows `claude mcp add` line is single-quoted for PowerShell and Git Bash (a double-quoted `$100%` lost its `$100` to variable expansion and the server served a corpus that did not exist while the client said Connected); Windows hook commands carry their variables as `set "NAME=value" &&` prefixes (no `setx`, no `.cmd` wrapper); `setup --client json|claude-desktop|cursor` exits 0 when only the shell line is unprintable; Cursor's file is named; `sextant verify --url http://host:8765/mcp --token ...` proves an HTTP install over Streamable HTTP; setup section 4 shows the container form (`ATLAS_MCP_HOST=0.0.0.0`, port published on the host's loopback), the LAN URL, the health probe, token hygiene and the lockout; `ATLAS_MCP_TRUSTED_PEERS` exempts peers you own from the lockout (a container's Docker bridge gateway locked out the operator), a correct token clears a source's failure count, and the 429 says how long; HOWTO-docker gains a one-container recipe with `--restart unless-stopped` and the read-only-mount note.

**Pipeline sweep, manual wave (2026-11-11)**: six human personas on docs/GETTING-STARTED.md. Fixed: hook commands carry their variables as `--env` flags through `atlas_plus.hooks.run` (Claude Code runs hooks through bash, Git Bash on Windows, so a `set "K=V" &&` prefix primed the wrong corpus while reporting success); `sextant-init` no longer adopts a user's folder as state because one entry happens to be named `backups` (every entry must be something Sextant creates), and `uninstall --corpus` refuses a folder with none of the scaffolded directories and refuses symbolic links instead of following one with chmod and reporting "removed"; scope names reject control characters, leading dots and more than 120 characters; the version copy is taken under the write lock so concurrent writers keep every state; the rename is fsynced through the directory on POSIX; the configured MCP token is trimmed with a warning; `sextant remember` under the write kill switch prints Not stored; quarantined search indexes are capped at three; `sextant setup --out FILE` writes without a BOM; the verify fact reads "Sextant install verified on <date>"; README no longer claims the summarizer falls back to the Anthropic API (it is local-only unless ATLAS_LOCAL_ONLY=off).

**Pipeline sweep, manual wave, round 2 (2026-11-11)**: paranoid, Docker Compose and stock-Ubuntu personas. Dashboard: /docs, /redoc and /openapi.json no longer answer unauthenticated; the two CDN scripts are pinned with subresource integrity (htmx 1.9.12, Tailwind 3.4.17); a Content-Security-Policy, X-Frame-Options, nosniff and no-referrer headers; the bootstrap `?key=` is scrubbed from the address bar. Workflows: every action pinned to a commit, CI token read-only, the publish input never interpolated into a script. constraints.txt is the real dependency closure (an unrelated package and a vulnerable certifi are gone). The session-start hook frames corpus text as the user's notes, not instructions. The uninstall report names a stray default ~/.memory or ~/.atlas, refuses symlinks and junctions, and checks that a corpus looks like one only after the stronger refusals. `sextant components` before init creates nothing; `sextant search ""` asks for a query. Tests skip where git is absent. Docs: HOWTO-docker (`ports: !override`, Git Bash path rewriting, `${PWD}`, the exported-token symptom, loading .env before the one-container recipe, a teardown section), HOWTO-self-hosting (env-file comments on their own lines, the venv fallback), GETTING-STARTED (get-pip without curl), SECURITY.md (a reporting path that exists before the flip), setup section 4 (LAN bind, token generation, the client's own interpreter, container hint).

**Pipeline sweep, manual wave, round 3 (2026-11-12)**: the impatient lane, starting on Python 3.10, hit an `ensurepip` failure from a plain `python -m venv` and never saw the version floor, which the README stated only in a trailing comment on the `py -3` line. The Windows quickstart now opens with the same "Before the block" lead-in the Linux block has: check `py --list`, name the interpreter by number, the symptom on an older Python, pip's clean refusal (`requires a different Python ... not in '>=3.11'`, verified: nothing installed), and that `--no-build-isolation` needs setuptools 77 or newer for the SPDX license field.

*Maintainer verification notes follow: how this build was tested, wave by wave. Skip them if
you only want to know what changed.*

**Pipeline sweep 12, agentic wave (2026-11-12)**: an installer agent on the docs-as-API, two people on one corpus through git, the optional layers without keys, a curator's month-old corpus, a second performance round and a fourth security round. `sextant setup --out` MERGES into an existing JSON config (backup written first) instead of replacing it (an opencode.json lost its other server and a settings.json its permissions); `--client X --json` prints that piece; `sextant verify --json` is one object; `sextant doctor --json --strict` is one object with a `strict` key; `sextant uninstall --dry-run` is a safe preview even with `--all` and `--json` prints the report; an unwritable `--out` path is one line, not a traceback. Corpus in git: `sextant-init` writes a `.gitignore` (history copies, locks, index); a fact appended to an imported note no longer makes the next re-import call that note "edited in the vault" (the import stamps the vault body's hash and compares against it; older stamps get a cautious wording); a lock file last touched over 30 minutes ago is broken at once (a committed lock from another machine cost every write 30 s); doctor names notes holding git conflict markers; the search miss line counts corpus notes, not history copies. Optional layers: the Anthropic provider retries without `temperature` when the SDK refuses it (the summarizer's cloud path failed on every call); `local_only` is a real boolean in the Brain's routing JSON (doctor said "off" while it was on); `sextant components` reads `ATLAS_TELEGRAM_BOT_TOKEN`; the nightly research batch skips when the runner is absent; the four listed-but-untriggerable scheduler jobs can be triggered; the Telegram bot prints one line when Telegram is unreachable; doctor says when tasks wait and no worker has ever claimed. A `\s` in a non-raw regex string no longer prints a SyntaxWarning on the first run. The MCP handshake now names the server `sextant` (it said `atlas-memory`; the version beside it is the MCP SDK's). Performance round two: doctor's `_N` copies check was quadratic (54 s on 8,000 `entry_<i>.md` notes, now one set lookup); the provenance-comment strip in the session hook and `atlas-prime` was quadratic on a run of whitespace (80 KB of spaces cost 11 s before every session) and scanned whole multi-megabyte files for a 600-character block; frontmatter is parsed through libyaml when it is installed (most of the rebuild time on tag-heavy notes); the control-character scrub on every write is two regexes instead of a per-character loop (3 s per 5,000 imported conversations). Known: an Obsidian dry run now costs what the real run costs (about 0.8 ms per note, warm) because it classifies every note; the CJK index adds 15 to 30 percent to a rebuild; the first run over freshly written files is 5 to 10 times slower on Windows (the antivirus reads them first). A version copy whose claimed slot could not be replaced because another writer held it open for a moment (Windows) is written into the slot instead of being left as an empty file. Final readiness pass (six personas on the flip candidate): on Windows the CLI no longer expands `$VAR`, `${VAR}`, `%VAR%` or glob patterns inside a typed fact or query (Click's cmd.exe emulation stored the operator's PATH into a note); piped output follows the console's output code page (a Japanese PowerShell 5.1 console decoded forced UTF-8 as mojibake; UTF-8 with no console); `duplicates --facts` and the weekly report count an ideograph as three letters and split CJK into bigrams, so an 11-character Japanese sentence repeated across scopes is reported; import ids and file names keep letters of any script (`東京旅行の計画` was `untitled`); `memory_remember_tool_v2` provenance values (`source`, `session_id`, `task_id`) are one defused token each (a `source` holding `---` closed the frontmatter early and a `session_id` holding `-->` broke out of the provenance comment); `setup --out` replaces a same-name registration or hook entry instead of merging it field by field (the merge appended the new interpreter as a stray argument and kept the old corpus variable); a dead-lock break puts the file back when it turns out to be a fresh lock another waiter took in the gap (two holders at once on Linux); decisions, anti-memories and tickets are scrubbed of control characters on write and primed text and ticket listings defuse them on read; a second `setup --out` in the same second no longer overwrites the backup of the original, and an unchanged merge writes nothing; `sextant-backup verify` says `ATLAS_RCLONE_REMOTE is not set` instead of `Remote '' not found`; doctor's `_N` copies hint fires only for imported notes; frontmatter with a tab after a colon is parsed the way every reader parses it; search snippets drop provenance comments; a fact typed as `---x` gets a sentence instead of `No such option`; docs: the uninstall step names the hook commands setup prints today, AGENT-INSTALL's update note says what verify writes, the Docker override example covers the sync port, HOWTO-self-hosting points container users at HOWTO-docker up front. Curation (a month-old corpus with planted problems found none of them): `atlas-review-memory duplicates --facts` lists repeated FACTS across notes (exact and near; the file-level scan diluted them away) and the weekly report opens with them; `unconfirmed-decay` counts a never-confirmed note from its last edit (they were skipped) and `stale` no longer flags a note confirmed last week; `contradictions` says when no model answered and exits 2 instead of "none detected", and shows the model 1,200 characters per note; `suggest-tags --write` and `unconfirmed-decay --write` run for a person at the terminal without `ATLAS_AUTONOMOUS` (and the decay write makes a version copy); a `memory_remember` that repeats a rejected idea carries an `anti_warning`, `atlas-prime` lists matching rejected ideas and decisions with their revisit conditions, and anti-memory propagation needs most of the pattern's words (two shared words flagged a note saying the opposite); confirming a decision sets `last_reviewed`; empty revisit conditions are stored as an empty list, not the words "(none recorded)"; the weekly report is written under the state dir, names real commands, and `sextant review report` is the documented weekly check. Security round four: the lock token's host field carries the OS and machine id (a Windows writer judged a live WSL writer dead because both report the same hostname, and broke its lock); a dead lock is broken by rename-then-delete so two waiters cannot both clear it and one delete the other's fresh lock; a directory planted at a lock path is an immediate error instead of a 40 s stall; a slash topic cannot name `_atlas`, `versions`, `archive` or `_sync` in any segment (such notes were searchable but unreadable), and a three-level topic no longer fails with a false "run sextant-init"; the import's corpus index never follows links, reads the origin and stamps from the frontmatter block only (a 4 KB frontmatter made a note "new" every run; `original_path:` in a fact's body made a note look imported), picks the latest copy by import stamp (a note named `meeting_2024.md` was taken for copy 2024 and archived in favour of its old copy), and `--archive-stale` refuses a junctioned `archive/` and any note that is a link or resolves outside the corpus; the closed-pipe wrapper swallows only stream errors with no file behind them (a failed init on a bad path had exited 0 in silence); `sextant remember -` tolerates invalid UTF-8 and a BOM; frontmatter over 256 KB is not parsed and at most 64 KB of it is indexed (one 50 MB tag list cost a minute per search).

**Pipeline sweep 11, manual wave (2026-11-12)**: six human passes on the morning's build. Import: the dry run now predicts the real run (every note classified as new, unchanged, edited or renamed before any write; the destination sample lists only files `--yes` writes); a renamed note is imported under its new name and the old copy named as stale; the stale list names the real previous copy after a second edit, older copies of a note, and notes no longer in the vault, and `--yes --archive-stale` moves them into `archive/`; a CRLF vault was re-imported whole on every run (line endings are not content now); a top-level templates folder is skipped and non-markdown files counted; a ChatGPT or claude.ai conversation an older build filed under another name (both branches, or the run date) is recognised by id and first turn, not filed again. Search: CJK characters glued to Latin letters or digits (`2025年`, `Python编程`, `東京tower`) are separated in the index and the query; sentence punctuation and line breaks break phrase adjacency (`河北。京剧` no longer answers `北京`); non-ASCII frontmatter (a title `大阪城の歴史`, a tag `café`) is indexed as text, not as `\uXXXX` escapes; a file whose name is not valid UTF-8 is skipped by the indexer instead of crashing every search, and doctor names it; the rebuild log names the new format. Hooks: a BOM on `identity/user.md` or `house-rules.md` no longer hides the title or the first rule; house rules accept `- `, `* `, `+ ` and numbered lines and say when a file has text but no list lines; the active project is also detected for a plain folder under the projects root that has a note (not only a git root); git commit subjects, ticket titles and assignees and the compaction timestamp are quoted like notes; provenance comments are stripped from primed text; the clock line says it is a snapshot. CLI: a closed pipe (`| head`) ends quietly; `sextant context` and `sextant web` appear in `--help`; `sextant remember -` reads the fact from stdin (PowerShell quoting); a fact is refused only when a LINE starts with `---`; `reference:obsidian/README` addresses a subfolder note. `sextant uninstall` no longer reports a correct hook command (with arguments) as a stale registration, and finds stale Codex (single-quoted TOML) and opencode (array) registrations. `sextant versions`: refuses a `versions/` that is a link or a file (a prune through a junction deleted a file outside the corpus), `--older-than 0` includes today's copies, the empty plan says why, unparsed files are counted, and the report shows how to restore a copy; doctor counts `_N` import copies. Route targets: internal-folder names are matched case-insensitively and `.MD` is refused; the lock release retries for 3 s and logs when a reader kept the file. Self-hosting: the lock token carries the holder's process start time, so a restarted container (the server is PID 1 again) clears the crashed PID 1's lock at once instead of after 30 s; lock files are created 0644; the uninstall "running?" line probes the dashboard port too and says whether the port came from the variable or the default; token rotation is documented in CONFIGURATION.

**Pipeline sweep 11, agentic wave (2026-11-12)**: six more strangers (a Claude Desktop and Cursor user with only PowerShell, an Obsidian vault and ChatGPT/claude.ai export import, two clients over two transports on one corpus, a hostile-corpus review, a Japanese-locale Windows user, an air-gapped install with a footprint audit). Import: a file symlink pointing outside the vault was followed into the corpus and a linked directory skipped silently (links are never followed and every one is named in the report; a file whose real path is outside the vault is skipped); a note renamed or moved in the vault was imported again under the new name (recognised by content and skipped, with its corpus path named); an edited note's older copy is named as superseded; a ChatGPT export's abandoned branches landed in the conversation (the live branch from `current_node` is followed); an undated conversation was filed under the run date and imported again on another day (filed under `undated/`); the ChatGPT export zip was detected as a claude.ai export (both zips are read as delivered); claude.ai messages whose words sit in content blocks were dropped; a vault key `original_path` overrode the real provenance; notes whose leading `---` block is not a YAML mapping got no provenance stamp; binary and non-UTF-8 notes were imported as garbage (skipped or decoded, and reported); imported files were rewritten with CRLF on Windows (LF, byte-faithful); the provenance path is vault-relative (the absolute form carried the local user name); the dry run lists the first twenty destinations. `atlas-prime` output carries the same data-not-instructions preamble as the session-start hook. Clients: the Microsoft Store build of Claude Desktop keeps its config under `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\` (docs, `setup`, `uninstall`); `uninstall` sees a project-level `.cursor/mcp.json` and reports a registration whose command path no longer exists (a moved venv); `setup --client claude-desktop|cursor|json` says on stderr where the JSON goes and to restart the client; GETTING-STARTED names Cursor's locations; the moved-clone recipe says `python -m pip` (the venv's `pip` launcher is dead after a move). Dashboard: offline, the page now says that its two CDN libraries did not load instead of showing "loading..." forever; the Brain hint names the right interpreter path per OS. Search: CJK text is indexed character by character (an invisible word joiner between adjacent ideographs, and each CJK run in a query becomes a phrase of its characters), so `東京` finds `東京都` and `人口` finds a sentence (a Japanese user had no working keyword search at all); text is NFKC-folded on both sides (full-width ABC, half-width katakana); the existing index is rebuilt once. Console: `sextant-uninstall`, `sextant-reset`, `sextant-fts-rebuild` (new alias), `sextant-intake`, `sextant-sync`, `atlas-temporal`, `atlas-prime`, `sextant-session-start` and the doctor launcher emit UTF-8 when piped (a Japanese corpus path crashed them with UnicodeEncodeError, and doctor printed `?? ??`). A note that is not UTF-8 is named by doctor and by the write that refuses it (the error used to be the codec's line with no file name). Locks: a lock left by a process that no longer runs on this machine is cleared at once instead of after 30 s of silent waiting, a wait longer than 5 s is logged, and doctor names lock files older than a minute; synchronous MCP tools run in a worker thread, so one writer waiting for a lock no longer freezes `/health` and every other client over HTTP; the HTTP start also refuses a port that already answers on loopback when the bind on 0.0.0.0 would have succeeded beside it. `sextant uninstall` accepts `--dry-run` (the default report), says when something answers on the MCP port or a write lock was touched in the last two minutes, and the dashboard's grep fallback no longer lists one hit per version copy. Hostile corpus (a shared or imported `.memory`): a `routing.yaml` target naming a Windows device (`reference/CON.md`) opened the console device and froze the MCP server for good, and `{topic}/x.md` with `evil:_atlas` wrote into the index directory (every component of a route target is now checked like a scope name, and Sextant's own directories are refused); a malformed `routing.yaml` (routes as a list, a rule that is a string, an integer target) raised a traceback out of every write (bad rules are dropped with a log line); `create_if_missing: "false"` counted as true; a junctioned `versions/` or `_atlas/` carried history copies, locks and the routes file outside the corpus (both are refused as links); the search index followed symlinks and junctions to files outside the corpus while the read tool refused them (links are never indexed; doctor skips them too); the session-start hook and `atlas-prime` printed note text raw, so a note could forge the hook's own block markers and `<system>` tags (every line of corpus text is quoted, markers and tags defused); `skills_invoke_tool` and `personas_adopt_tool` told the agent to obey runbook and persona text (now framed as the user's own notes); the ChatGPT import raised on a crafted `create_time` after partial writes and a lone surrogate left a zero-byte note (imports are scrubbed, capped at 4 MB per note and written atomically; a role with line breaks is cleaned). `sextant context` prints the priming text (with words: the task-matched facts) and `sextant web` / `dashboard` start the dashboard, instead of pointing at other commands; the doctor's corpus scan is one pruned walk (no per-file resolve). History: `sextant versions` reports the size of `versions/` and prunes old copies (`--prune --older-than DAYS --keep-last N`, report first, `--yes` to delete; the live notes are never touched), and GETTING-STARTED states the measured growth (one full copy of a note per write).

**Pipeline sweep 10, expert review (2026-11-12)**: a code review of the sweep-10 changes. The control-character scrub ran AFTER the `---` guard, so `-\x07--` passed the guard and the scrub assembled the delimiter (the guard now runs on the scrubbed text); the search log cut a query to 300 characters BEFORE redacting it, so a token straddling the cut left its first half in the log (redaction runs on a wider window first); two servers rotating the search log at once could move a fresh one-line log over the history the other had just rotated (an exclusive guard file); the no-proxy opener applied to every embedding host, so a remote `ATLAS_EMBED_URL` behind a required proxy went direct and failed silently (loopback only now; `ATLAS_EMBED_TIMEOUT` is new); the FTS tokenizer migration was not one transaction. Names: `reference:null` made a note whose id read back as None, `reference:[a` one whose frontmatter did not parse, and `reference:a #b` a title cut at the comment (the id and title are quoted when YAML would read them as anything but that string); the Hangul fillers and the Braille blank are refused, a name needs a letter or digit, and a name over 200 bytes once encoded is refused before the write (120 emoji failed inside the temp file with an internal path).

**Pipeline sweep 10, agentic wave (2026-11-12)**: six more strangers (a wheel-only uv-tool user, Alpine/musl, a returning user upgrading from an earlier build, a hostile-input hardening lane, a performance lane on a 20,000-fact corpus, and a Codex CLI + opencode user). The write lock: `ntpath.realpath` can hand one thread a `\\?\`-prefixed path for a file that does not exist yet (its two probes see different error codes when another writer creates the directory in between), so that writer took a DIFFERENT lock file and one fact in twenty was lost on Windows; the lock key now strips the prefix and folds case (the one-in-many CI loss, reproduced with a ten-round race test that stays in the suite). Hardening: `CONIN$`/`CONOUT$` and the superscript-digit COM/LPT names are refused (one `project:CONIN$` call froze the stdio server); NEL, line and paragraph separators, zero-width and bidi characters and `* ? " < > |` are refused in scope names, names are NFC-normalised, a trailing dot is refused, `identity:<topic>` is an error instead of a silent write to `identity/user.md`; facts are capped at 65,536 characters and stripped of control characters (ESC/BEL from a note no longer reaches the terminal); a failed temp write leaves no `.w-tmp`; the search log keeps 300 characters of a query and rotates at 5 MB (one 50 MB query made a 50 MB line); search queries are capped at 2,000 characters; the dashboard rejects JSON nested deeper than 200 levels before parsing (a segfault inside a worker thread on musl); a hook that hits a read-only corpus prints one line instead of a traceback. Performance: no embedder call when no semantic index exists (2 s per search for everyone without Ollama), loopback calls ignore proxy variables, doctor probes every service at once after a 0.75 s TCP pre-check (4.3-7.3 s to about 1 s with nothing listening), the distribution lookup is two direct queries instead of a scan of every package (0.4 s), `__version__` is resolved lazily (75-90 ms off every process, hooks included), the orphan scan parses each file once. Search: the FTS tokenizer is `unicode61 remove_diacritics` (`zurich` finds Zürich; an existing index is rebuilt once), a Python without FTS5 is named by doctor and in the search log, `sextant-mcp --transport stdio|http` is accepted, and the vector cache key carries a generation counter (a same-size re-index inside one timestamp tick was served stale). Clients: `sextant setup --client codex` (the `codex mcp add` line and the TOML table) and `--client opencode`; the server sends MCP `instructions` telling any client's agent to search memory first and naming a missing root; `sextant-session-start` is the priming text for clients without hooks; `sextant uninstall` finds Codex and opencode registrations and names the hook command correctly; `setup --client hooks --out` writes one JSON object on WSL. Docs: the Updating section's conflict recipe said `--theirs` for the release (it is `--ours`), gains `atlas-temporal build`, a detached-HEAD note and the hook re-check; a Codex/opencode section; the FTS5 requirement; an Alpine note; the `telegram` tests skip without the extra.


### Final install sweep, 2026-11-11 (six strangers: agentic PowerShell, agentic Git Bash, package user, manual 3.11, WSL, Docker)

- `sextant setup` (aliases `mcp`, `connect`): prints the MCP registration for THIS install,
  filled in: the one-line `claude mcp add` with the interpreter quoted and every `ATLAS_*`
  path variable baked in as `-e`, the `mcpServers` JSON for Claude Desktop and Cursor, the
  hook entries, and inside WSL the Windows-side `wsl -d ... --` form with `WSLENV`. A pipx,
  uv-tool or PyPI install has no clone and no `deploy/` snippets; this replaces them.
- `sextant verify` (also `python -m atlas_plus.roundtrip`): the stdio round-trip now ships in
  the package; `scripts/mcp_roundtrip.py` is a wrapper. `--command` runs the server through
  any launcher (`wsl -d Ubuntu -- /home/me/sextant/.venv/bin/python`), so the exact
  registration a Windows client uses against a WSL install can be tested from Windows.
- `memory_health` always names `memory_root`: an "ok" against a stale `~/.memory` (variables
  that never crossed into WSL) looked identical to a healthy install.
- Doctor is a report: the temporal, exhausted-tasks and spend checks no longer create
  `state.sqlite` or `temporal_graph/` on an empty state dir; the Worker row says "no task
  database yet" instead of "state.sqlite not found"; the title is Sextant Doctor on a
  public install however it is invoked.
- The session-start clock leads with the machine's local zone (or `ATLAS_HOME_TZ`), not the
  author's city.
- A corpus kept inside the clone (`memory/`, `state/`, `.memory/` at the top level) is
  git-ignored. `sextant-init` points at `sextant setup` and `sextant verify`. The overview
  prints doc URLs when no `docs/` is on disk.
- Docs: AGENT-INSTALL rewritten around `setup` and `verify` (package-user path, `py --list`,
  precedence of specific path variables over `ATLAS_WORKSPACE_ROOT`, one-shell-per-command
  agents, WSLENV, quoting for paths with spaces, restart the client, clean no-sudo venv
  route, do-not list aligned with the README prompt); README agent prompt points at the
  runbook; GETTING-STARTED (`sextant-mcp --stdio`, `setup` first, PowerShell `~`, doctor is
  read-only, `ATLAS_HOME_TZ`); CONFIGURATION rows; HOWTO-docker (shell overrides `.env`,
  load `.env` before the `claude mcp add --transport http` line, doctor rows mean little
  under compose, `exec mcp sextant verify`, brain key into `.env`, backup of all of `/data`
  with the real volume name and a restore line, override file for bind mounts and ports,
  Windows `.env` encoding); sdist carries `deploy/*.json` and `constraints.txt`.
- Second wave (real `claude mcp add` in an isolated config dir, upgrade, `pip install git+`,
  clean Debian container, adversarial review): a write into a corpus that does not exist is
  refused with the setup hint (it used to create a stray `~/.memory` when the registration
  lacked `-e`); `setup` quotes with `shlex` on POSIX, refuses a quote or line break in a value
  on Windows, doubles a trailing backslash, quotes the WSL interpreter and distro, appends to
  an existing `WSLENV`, carries `ATLAS_UPDATES_FILE`, puts the hook-only variables on the POSIX
  hook line, names its JSON blocks after the file they go in, accepts `--client claude-desktop`
  and `cursor`; `verify` passes only when the search returns the file the write named, parses
  `--command` the way Windows does, never leaks its log file, tolerates non-UTF-8 and non-JSON
  stdout, and ignores server-initiated requests; the session-start hook prints the corpus it
  read; the MCP temporal search looked in `temporal/` while every builder writes
  `temporal_graph/`; `ATLAS_HOME_TZ` with a bad value degrades instead of raising; the Telegram
  remedy and the uninstall note no longer assume uv or a clone; docs carry the `pip install
  "sextant-ai[extras] @ git+..."` form and the exact `--strict` rule. The public mirror now
  keeps its git history (one commit per publish, pushed fast-forward) so the documented
  `git pull` upgrade works; the Updating section adds `sextant-init` (ownership marker),
  `setup` and `verify`; the agent runbook unhooks the client before deleting data; the
  self-hosting env file names the corpus paths; `setup` prints the HTTP-mode and remote
  registration lines for a machine without Claude Code; a get-pip route without curl.
- Known and left: search snippets bracket the matched words and show the `<!-- source -->`
  comment; `atlas-review-memory summary` counts files differently from the doctor; the
  components table folds long paths at 80 columns; Brain pytest executors assume a clone.

### Persona rerun wave 4, 2026-11-10 night (Linux self-hoster, in-place upgrade, claims verifier)

- Forwarded headers: every server now starts uvicorn with its proxy-header handling off, so
  `auth.forwarded_ip()` is the only rule: `Cf-Connecting-Ip` (or `ATLAS_FORWARDED_IP_HEADER`)
  first, else the LAST non-loopback hop of `X-Forwarded-For`; `none` ignores every forwarded
  header. (uvicorn had been rewriting the peer first, so XFF always won and `none` half
  worked.) SECURITY.md states the rule, the MCP server's five-minute window, the 16-character
  key minimum and the Caddy/nginx overwrite lines.
- `_atlas/` (sync inbox proposals, parked conflict copies) is never indexed, searched or
  primed: a peer's push reaches the assistant only after the operator promotes it.
- Sync: a push conflict parks the hub's copy locally in the same run and names it (no second
  sync needed); bare `sextant-sync` lists its subcommands.
- Doctor: a report never creates `state.sqlite` or `search.sqlite`; `--strict` says why a
  row counted (`MCP down, expected because ATLAS_MCP_TOKEN is set`) and ignores the Brain
  diagnostic sections when no Brain key is configured.
- Temporal: the build manifest carries a rules version; after an upgrade a plain
  `atlas-temporal build` re-reads every file once and retires facts the new rules no longer
  derive (template defaults), and a re-indexed file retires the facts it stopped stating.
- Clip inbox dedupe hashes the bytes it writes (CRLF on Windows had made it inert); the
  Brain's key redaction keeps uvicorn's access-log arguments (no logging traceback per
  `?key=` request); an interactive write refused by `ATLAS_INTERACTIVE_WRITES=off` names
  that switch, not `ATLAS_AUTONOMOUS`; the rclone hint matches the platform; compose gives
  the dashboard `ATLAS_BRAIN_URL=http://brain:8767`; docs (test matrix cookie row, Clip
  permissions, uninstall confirmations, hub port for doctor, Updating section).
- Known and left: an old-build peer proposal is re-sent once after upgrading (then
  remembered); `.ndjson.partial` files from before 0.1.0 are promoted by the next recovery
  walk, not by the upgrade; two stdio writers racing on one file can skip a `versions/` copy
  (facts are never lost).

### Persona rerun, 2026-11-10 evening (three more hand installs on the renamed build)

- Quickstarts show the "choose where it lives" variables above `sextant-init`; the Stop hook
  says on stderr and in the activity log when no model can summarize (and the docs say what it
  needs); `atlas-prime <task>` adds the facts that match the task words from any scope.
- Sync: a federation peer's accepted proposals are remembered (not re-sent, not pulled over,
  counted as "awaiting the hub's review"), so `plan` and `checkin` can say a peer is current;
  the conflict line names the parked file; a clean sync reports leftover parked copies;
  `plan` explains an out-of-allowlist share; a dead hub is "no hub answering at ..."; bare
  `sextant-sync` prints help and a hint; `status` says whether this corpus is a hub or a spoke.
- Keys shorter than 16 characters refuse to start (dashboard, Brain, MCP). `doctor` shows
  Web and Sync rows, probes the configured ports, hides the tunnel row unless
  `ATLAS_TUNNEL_CHECK=on`, never creates the task database, prints `strict: OK|FAIL (...)` on
  stdout, and `--strict` counts a service as unhealthy only when this install is configured
  to run it.
- Temporal: template defaults (`status: active`, `priority: normal`, `type`) are not facts.
  Anti-memory accepts the write-scope grammar; decisions are dated locally; `ATLAS_USER_NAME`
  shows in the primed identity; an unparsable search scope is reported instead of searched
  globally; the Clip inbox ignores an identical double submission and promotes its transcript
  file on ingest; backup refusals say exactly what is missing; `sextant-backup install` is
  hidden where the installer does not ship; Rich tables fold instead of truncating (no
  mojibake on cp1252 consoles).

### Distribution name, 2026-11-10

- The PyPI distribution is `sextant-ai` (was to be `sextant-cp`; `sextant` itself is an
  unrelated Helm tool). Commands and the import package are unchanged.

### Persona pass, 2026-11-10 (six simulated people installed the public build by hand)

- Paths: the workspace root is `ATLAS_WORKSPACE_ROOT` or the home directory, nothing else; the
  maintainer's install folder is no longer autodetected. `ATLAS_*_PORT` values are validated
  with a named message. `OLLAMA_BASE_URL` and `OLLAMA_HOST` are honoured when
  `ATLAS_BRAIN_OLLAMA_URL` is unset; the compose brain profile now sets the variable the Brain
  reads and requires both keys.
- Shell: `sextant remember "<fact>" --scope <scope>`; `sextant search` accepts the write-scope
  grammar, prints the confidence band and an honest miss message; `components`/`status`/the
  overview show the configured ports and say what a probe is; umbrella `--help` reaches the real
  command; scope kinds are case-insensitive.
- Imports: ChatGPT/Claude imports are idempotent and report mode writes nothing (`would_import`);
  collision copies are recognised by origin; BOM-saved notes read clean; the index hint names a
  real command. `atlas-temporal build` indexes your corpus by default (fixtures are demo-only).
- Hooks: git hardening keeps the system config (CRLF repos no longer report dirty); the Stop hook
  honours its payload's transcript root; the identity excerpt is cut at a line break; the
  fsdiff disclosure lists everything recorded.
- Sync: the manifest hash is content identity only (`checkin` agrees on identical corpora);
  subdirectories of `_atlas/` never sync; `checkin` refuses as a peer without a share and sends
  no host name; push conflicts are reported as parked on the hub; the share notes compare
  prefixes correctly; the hub answers `/health`.
- Network: one forwarded-IP rule for every surface (`ATLAS_FORWARDED_IP_HEADER`; SECURITY.md
  "Behind a reverse proxy"); the MCP server never locks out the operator's own loopback clients
  and writes no access log; `/status` counts a 429 as degraded; the capture route is a
  user action (not gated by `ATLAS_AUTONOMOUS`), as are decisions, tickets and anti-memories
  requested through the MCP tools; the Clip route, header and body are documented; the dashboard
  warns once when the Brain is unreachable and about short keys.
- Backup: refuses before any transfer unless the rclone remote exists, never prompts for a config
  password, has no default remote, keeps dry runs apart from real runs; doctor names the real
  db-backup commands (`snapshot`, `restore`, `prune`) and probes the configured MCP port;
  `doctor --strict` counts errors.
- Docs: GETTING-STARTED (shell commands, backup, imports, updates.txt default), HOWTO-sync
  (first-sync identity note, resolution, prefixes are directories, hub-side parking),
  HOWTO-docker (real HTTP MCP wiring), CONFIGURATION (Windows persistence, new variables),
  Clip README, Linux hook pair, hook interpreter note.

### Hardening, 2026-11-10, round C (three independent re-reviews of the wave-2 fixes)

- Sync: a file you edited that the hub then deleted is reported as deleted remotely (it was
  an error on every run); a local delete stays reported and is never pulled back; a `--share`
  run keeps the base entries it could not see; the hub's own allowlist is intersected with
  your share before any byte is read; a 409, 5xx or ingress HTML page on one file is a
  per-file error and the run completes; a push failure never aborts the run; names Windows
  cannot store (`?<>:"|*`, control chars) are refused; parked-copy dedupe never builds a glob
  from the file name and scans only the relevant directories; a sidecar that is not a JSON
  object is ignored and one bad change can never 500 a push batch; a hub that does not say
  how it pairs you is refused without a share; `checkin --share`; a share that matches no
  local file is reported.
- Brain: the worker's heartbeat keeper renews only its own lease and stops when the lease is
  lost (the run's result is then discarded with an attempt row); the worker fence includes the
  attempt number; any fence implies a running row; attempt telemetry is recorded before
  post-processing and on refusals; dedup keys on kind and never folds an explicit task_id;
  `POST /task` writes off the event loop and returns 503 when the store is locked.
- Reparse guard (MCP write path, sync walk and policy) keys on the name-surrogate reparse
  tag (junction, symlink) instead of the attribute, so cloud placeholders (OneDrive, Dropbox
  Files On-Demand), app-exec links and WOF-compressed files are not refused.
- Imports: collision copies are recognised by their `original_path`, over one cached listing
  per directory (bracket names, 10x faster on large vaults, sibling notes never mistaken for
  copies). Clip inbox: a RecursionError from deeply nested JSON is a 400; message content must
  be a string. `sextant-backup status` tolerates odd state files and names the real command.
  Anti-memory project slug is stripped like routing's.

### Hardening, 2026-11-10 (six-lane validation, wave 2)

- Federation egress is decided locally: `sextant-sync sync --share <prefix>` (repeatable, or
  `ATLAS_SYNC_SHARE`) is the spoke's own allowlist; nothing outside it is read, serialized,
  sent or pulled. A hub that pairs you as a peer refuses `sync`/`plan` until you declare one.
  The hub's manifest now names the principal it sees.
- A sync conflict stays a conflict until resolved: the file keeps its previous three-way base,
  so a second sync no longer pulls the hub's copy over the local edit. Identical parked copies
  are not parked twice (conflicts and peer inbox).
- The manifest walk never enters a Windows junction; the MCP write path refuses a scope whose
  directory is a symlink or junction (reads already did).
- Brain task store: `mark_done/mark_blocked/mark_failed` take `owner`/`attempt` and change
  nothing when the lease moved on (the worker passes its lease); zombie cleanup requeues a
  leased task while attempts remain instead of failing it; `create_task(dedup=True)` checks
  and inserts in one write transaction.
- Failed-auth throttling now covers the Brain (HTTP and WebSocket), the sync hub and the
  dashboard WebSocket handshake, with the same per-source accounting as the dashboard and
  MCP. The `Bearer` scheme is case-insensitive; the token is not.
- `sextant-mcp --no-auth` starts (it crashed on an SDK signature) and `python -m
  atlas_plus.web.app --no-auth` / `python -m atlas_plus.brain.server --no-auth` serve the
  configured app instead of a fresh import that 503s.
- Clip inbox returns 400 for non-object JSON and non-object messages; ChatGPT/Claude imports
  skip wrong-shaped entries with a reason; an unchanged re-import of an already re-imported
  Obsidian note no longer grows `_2`, `_3`, ...; project-scoped anti-memory propagation sees
  `projects/<slug>.md`; `sextant-backup status` keeps the last real run and success separate
  from dry runs; the installer fallback names the real command (`now`).
- Docs: Linux uses the `.macos.json` hook pair (POSIX paths); hooks do not inherit the MCP
  registration's `env`, so set the roots where the hook can see them.

### Hardening, 2026-11-09 (launch review rounds A and B)

- Search index moved to its own file, `<state>/search.sqlite` (was shared with the Brain task
  queue in `state.sqlite`). Rebuilt automatically; the old tables are left in place.
- Dashboard: cookie-authenticated mutating requests need the page's `X-Sextant-Request`
  header and an Origin matching the dashboard's own host and port; the chat WebSocket checks
  Origin; `/api/inbox/conversation` is bearer-only; writing the curation report needs POST.
- Every git call Sextant makes goes through one hardened helper (`atlas_plus.gitsafe`): hooks,
  fsmonitor, filters, external diff and signing programs from repo config never execute.
- Sync, intake and `import --scope` check paths as the file system sees them (8.3 aliases,
  trailing dots, junctions).
- `sextant-init` refuses to claim a non-empty directory that is not already Sextant state;
  `reset --state` keeps logs and CLI history; destructive guards decide containment by file
  identity and refuse paths that resolve to a network share.
- Backups: the rclone mirror refuses a source without this install's signature and moves
  remote deletions and overwrites to a dated trash; the state dir gets a dated archive too.

### Fixed (2026-10-16, persona batch 3: the full personality spread)

Findings from eight more persona tests (careful PM, Ubuntu hobbyist,
skeptical claims-auditor, Windows junior, macOS skimmer, non-dev power
user, in-place upgrader, improviser).

- **`ATLAS_LOCAL_ONLY` now holds on the summarizer's own code path.** The
  earlier guard covered the provider registry only; the stop hook,
  suggest-facts, dream mode, contradictions, reflexion and weekly
  synthesis call the summarizer's `complete()` directly, which could still
  fall back to the Anthropic API with a key in the environment and Ollama
  down. The refusal is now inside that fallback too, with regression
  tests, and the claim joined the security test matrix.
- **`memory_health` no longer reports `ok` over a missing memory root.**
  Skipping `sextant-init` (or an `ATLAS_MEMORY_ROOT` set only in a shell
  profile a GUI-launched client never reads) used to produce a false pass
  on the docs' own verify step. The reply is now `degraded` with a hint
  naming both causes.
- **The Unix quickstart works on a stock machine.** `python3.11 -m venv`
  resolved on neither stock macOS nor Ubuntu 24.04; the block now uses
  `python3` with real prerequisite notes (python.org/brew on macOS, the
  Xcode CLT dialog, `python3.12-venv` on Ubuntu), and the README says up
  front that Claude Code itself must be installed first.
- **Session hooks now ship in both platform forms.** The single hook
  JSONs carried the Windows command as the body with the macOS form
  buried inside `_note`; a Mac user pasting the file verbatim registered
  a hook that failed every session start. Added
  `deploy/session_start_hook.macos.json` and `deploy/stop_hook.macos.json`,
  and both `_note`s now walk through a safe merge (back up first, extend
  an existing `hooks` key, drop `_note`).
- **The MCP registration block now protects itself.** The `cd`-dependence
  of `$PWD` and the `claude mcp list` verification moved into the code
  blocks skimmers actually paste; the verify step documents the
  `degraded` reply and the failure branch; the docs now say the clone
  path is load-bearing (move/rename = re-register) and give the
  `sextant-mcp --stdio` form for pipx/uv-tool installs.
- **`atlas-fts-rebuild --force` removed from the import guide.** The flag
  does not exist (a full rebuild is the default); copy-pasting it errored.
- **Updating section answers the data question first.** Corpus safety,
  registration survival, and why the pip reinstall must not be skipped
  now lead the section instead of trailing it.
- Security section honesty: the summarizer fallback names Anthropic (not
  OpenAI) on the hook path; the opt-in research web search and the link
  checker's HEAD requests are disclosed; `ATLAS_SESSION_AUTONOMOUS` is
  documented beside `ATLAS_AUTONOMOUS`; the stop hook's background jobs
  are listed. Plus: a documented "let Claude Code do the install" prompt,
  and `sextant-init`'s next-steps now point at the README one-liner.

### Fixed (2026-10-16, persona fan-out batch)

Findings from four persona tests (non-dev, thorough Windows dev, Linux
contributor, SRE uninstall) plus a CI-caught timestamp bug.

- **96 broken command hints fixed.** Hint strings across ten modules said
  `atlas review-memory ...`; no `atlas` umbrella command exists, so every
  hint was a command-not-found. All now say `atlas-review-memory`.
- **MCP registration now survives changing directories.** The documented
  `claude mcp add` lacked `-s user`, so the default `local` scope tied the
  server to the clone directory -- invisible from the user's real projects,
  failing silently. README and GETTING-STARTED now pass `-s user` and
  explain both traps (scope and absolute path).
- **Stop hook survives paths with spaces.** The hook command was unquoted;
  a Windows profile with a space in its name split it. Now quoted, with
  the macOS/Linux form in the `_note`.
- **Test suite can no longer write to a contributor's real `~/.memory`.**
  `TICKETS_ROOT` is import-time bound, so a bare `pytest` created ticket
  dirs in the real corpus (the cleanup fixture only covered three slugs).
  An autouse fixture now points the store at a per-test directory -- same
  treatment the state DB got on 2026-10-12.
- **Uninstall docs tell the truth.** "Leaves exactly two things" was wrong:
  the stop-hook summarizer also writes a session log and version copies,
  and nothing covered unhooking the MCP registration or Stop hook (deleting
  the clone first leaves Claude Code erroring every session). The section
  is now an ordered walkthrough; `sextant-uninstall`'s report also surfaces
  the session log and any Claude config that still references the install
  (report-only, never deleted).
- **README quickstart honesty.** New "Who this is for" block (browser-only
  claude.ai users cannot connect yet); `py -3` with install guidance
  replaces the hardcoded `py -3.11`; `memory_health` expected reply matches
  reality; alias table gains the missing `sextant-import` and
  `sextant-embed-index`; the PowerShell-invalid `&&` update block is split
  per platform.

### Fixed (2026-10-16)

Follow-up hardening from an adversarial review of the 2026-10-12 fixes.

- **Web UI task submits no longer fail silently on HTTP errors.** Seven
  fetch paths (`submitTask`, `confirmDispatch`, `escalateTask`, `quickRun`
  fallback, `retryTask`, `stopTask`, `submitClaudeTask`) read the response
  body without checking `resp.ok`; a 422 showed "queued: undefined" and
  opened a live stream for task `undefined`. A shared `jsonOrThrow` helper
  now surfaces validation detail readably.
- **`_claude_task` blocks cleanly on a non-string JSON prompt.** A payload
  like `{"prompt": 123}` hit the empty-prompt guard with a non-string and
  raised `AttributeError`; it now returns a blocked result with a clear
  message.
- **Curation report timeout leaves an artifact.** `subprocess.TimeoutExpired`
  escaped `_curation_report` as a raw exception; it is now caught and
  recorded as a failed result with an artifact, like every other path.
- **Verify env scrub widened and made case-insensitive.** `_clean_pytest_env`
  also drops `RESEARCH_API_KEY` and `TELEGRAM_BOT_TOKEN` (same failure class
  as `ATLAS_WEB_API_KEY`), and matches `ATLAS_*` case-insensitively so a
  lowercase variable cannot leak on POSIX.
- **Brain server rejects whitespace-only instructions (422).** `min_length=1`
  caught `""` but not `"   "`.
- **Fixed a vacuous test.** `test_submits_task_valid_request_accepted`
  patched `_DB_PATH` with a `str`; after the 2026-10-12 call-time resolution
  change that produced an `AttributeError` -> 500, and the `in (202, 500)`
  assertion passed without exercising `create_task`. The patch is gone (the
  autouse isolation fixture covers it) and the test now requires 202.

### Fixed (2026-10-12, evening)

- **Brain verify runs no longer fail against the live environment.** The
  always-on worker runs with the deployment environment loaded; the
  `verify`/`pytest` executors inherited it, so every verify run executed
  the suite with a live `ATLAS_WEB_API_KEY` and `ATLAS_STATE_DIR` --
  ~260 tests failed per run (61 failed verify tasks in 14 days) and test
  fixtures resolved live operator state. Both executors now scrub every
  `ATLAS_*` variable from the subprocess environment so a verify run
  looks like a clean CI shell.
- **The test suite can no longer write the live task queue.** Brain state
  modules (`tasks`, `history`, `improvement`, `task_facts`, worker
  defaults) bound their `state.sqlite` path at import time, which made
  test patches silent no-ops: every full-suite run planted real tasks
  ("test instruction", "Verify ticket TES-001") in the live queue, the
  worker executed them, and the verify kind re-ran the suite --
  breeding the next generation. DB paths now resolve at call time and
  an autouse conftest fixture points every state consumer at a
  per-test database (no `ATLAS_STATE_DIR` passthrough, deliberately).
- **`suggest-facts --auto` no longer hangs headless runs.** A candidate
  below the auto-approve threshold fell through to the interactive
  `input()` prompt; inside the Brain's scheduled task that blocked until
  the 1200s timeout killed the run (the recurring nightly
  `suggest_facts_auto` failures). Auto mode now skips sub-threshold
  candidates instead of prompting -- they are still never written.
- **Nightly curation report timeout raised 300s -> 900s** (it timed out
  twice in October as the memory corpus grew).
- **Blank task instructions are rejected at the API.** An empty
  instruction used to flow through `/api/tasks` / `/api/tasks/claude`
  to the worker and die inside `claude --print` with a confusing CLI
  error. The web layer now 422s blank instructions, the Brain server
  requires `min_length=1`, and the claude executor blocks an empty
  prompt with a clear message before spawning the subprocess.

### Added (2026-10-12)

- **Sextant branding in the web dashboard**: the page now ships the
  brand favicon (inline data URIs, SVG with ICO fallback -- no package
  data needed), the tab title reads "Sextant Helm", and the header
  carries the sextant mark. The brand kit's `favicon.svg` shipped with
  its `.t`/`.s` style classes undefined (invisible strokes) and a
  large embedded C2PA metadata blob; the embedded copy fixes the
  styles and strips the blob (8.2KB -> 0.5KB).
- The public README opens with the brand banner (dark/light variants,
  served from usesextant.com -- the landing page must be live before
  the repository goes public).
- **Map tab**: a live system visualization in the web dashboard.
  Services are probed over their health endpoints, data stores are
  read from what exists on disk, and the connections between them
  pulse in real time as components talk -- with a ticker listing each
  event and a reference to where the data involved lives (memory
  path, ticket file, task artifact, truncated query). Backed by a new
  cross-component activity feed (`logs/activity.ndjson`, metadata and
  references only, never contents; self-trimming) that the Brain task
  lifecycle, MCP tool dispatch, ticket store, temporal builds, and
  sync operations all append to. New endpoints `/api/topology` and
  `/api/activity`; new env vars `ATLAS_SYNC_URL` and
  `OLLAMA_BASE_URL` documented in CONFIGURATION.

### Fixed (2026-10-12)

- Tab switching in the web dashboard is now data-driven. The old
  parallel tab array was missing two tabs, which misaligned the bar
  highlight from the third tab on and left the Operator and
  Watchtower panels active when switching away.

### Changed (2026-10-11)

- Session-start hook: the temporal context block is now cached across
  sessions, keyed on the graph's `facts.jsonl` identity (any graph
  write invalidates) plus a one-day freshness bucket. Project session
  starts drop from ~560ms to ~315ms; a cache hit no longer imports or
  loads the temporal stack at all.

### Fixed (2026-10-11)

- The ntfy control HOWTO's setup now uses only what actually ships:
  run the listener via the `atlas-ntfy-listener` console script (or
  `python -m atlas_plus.ntfy.inbound`) with the two env vars in its
  process environment -- the old steps named maintainer-only scripts
  that are not in the public tree. A build-time gate now rejects any
  shipped doc that references a `deploy/` file that does not ship.

### Changed (2026-10-10)

- Ticket listing prunes archived subtrees at descent instead of
  enumerating and discarding them: on a store whose `_archive/` holds
  two hundred times the live ticket count, `list_tickets` dropped from
  54.5 to 7.7ms (7x) with identical results -- paid by the Operator
  panel's refresh, board renders, and session start.

- The Brain's `/health` endpoint now reports model-backend
  reachability: `{"status": "degraded", "backend": "unreachable"}`
  when the local model server cannot be reached (probe cached 30s,
  1.5s timeout), instead of answering ok while every model-dependent
  job is silently broken. The HTTP code stays 200 while the process
  is alive -- watchdogs restart on non-200, and a restart cannot fix
  the backend -- and `sextant-doctor` surfaces the degraded state.

### Fixed (2026-10-09)

- Search now survives a truly trashed index file: file-level
  corruption raises at connect time (before the query-level self-heal
  could see it), and a leaked connection handle was blocking the
  quarantine rename on Windows -- both paths now heal once, retry
  once, and preserve the corrupt file as evidence. The first-search
  freshness sweep is also 2.4x faster (relative paths ride the scan
  stack instead of being recomputed per directory entry: walk median
  94.5 -> 39.6ms on a 2,220-file corpus, cold first search
  688 -> 482ms).
- Deleted memory files now retract their temporal-graph facts at the
  next build: a file the manifest knows but the walk no longer sees
  (removed, or moved under an archive directory) has its facts
  invalidated -- out of current search, kept for as-of history -- and
  graphs built before the archive-exclusion fix heal on their next
  build. Previously nothing ever retracted, so ghost facts persisted
  indefinitely. Also: a fact superseding itself (same id, new value,
  e.g. a re-indexed transcript's token counter) no longer raises a
  false contradiction warning against its own previous version. The
  build output now reports this maintenance -- retracted files,
  invalidated facts, and per-file compaction savings print when they
  happen instead of running silently.

### Security (2026-10-09)

- The operator action log is structurally append-only now: control
  characters in a completion note (or in a hand-written ticket's
  title/project frontmatter) are flattened at the write sink, so a
  note can no longer forge standalone log entries in a file that
  sessions read back as memory. The Operator queue also no longer
  errors out on a hand-written ticket with an empty priority.
  Dependency audit of the full environment: clean (pip-audit 2.10.1,
  2026-10-09).

### Fixed (2026-10-08)

- Temporal graph ingest skips underscore-prefixed directories
  (`tickets/_archive/` especially). The exclusion list named `archive`
  but the ticket store archives to `_archive`, so archived tickets were
  resurrected into the graph on every build -- on a long-lived corpus,
  81% of live facts were archived-ticket residue. Multi-project
  `applies_to:` lists also no longer raise false contradiction warnings
  (`applies_to_project` is multi-valued by nature).

### Added (2026-10-08)

- Operator panel: a dashboard tab that gathers every open human-assigned
  ticket across all projects into one priority-sorted queue with the
  step's instructions inline. Tickets can carry a `verified:` date
  marking instructions as researched and ready (unverified steps are
  flagged NEEDS RESEARCH). Marking a step done or skipped records the
  action -- with an optional note -- in `operator/action-log.md` inside
  the corpus, so completions are searchable memory, and session start
  injects a one-line summary of how many steps await the operator
  (nothing on a fresh corpus).

### Changed (quality wave 2, 2026-09-25 .. 2026-10-09)

- Session-start hook trimmed further (measure-first): one shared
  ticket-store scan serves both the operator-queue line and the
  open-tickets section (was one scan each), and the git summary asks
  git twice instead of three times (branch and dirtiness ride one
  `status --porcelain=v2 --branch` call). End-to-end median
  647 -> 592ms on the reference corpus; the shared scan stops the
  full-store pass from multiplying per consumer as tickets grow.

- Docs now say exactly what the code does on re-verification and task
  verification: the scheduled re-verification pass reports (clock reset
  is its own opt-in switch), OUTDATED/NEEDS_HUMAN are report lists (not
  moves or notifications), and ticket verification is an optional
  post-run test check (`ATLAS_VERIFY_TICKETS`, default off) that reopens
  a ticket on failure -- not a gate in front of results. A shipped-docs
  test now rejects the old overclaiming phrasings. The Operator queue is
  documented in USAGE (human-assigned tickets, `verified:` dates, the
  action log).

- The temporal graph compacts its append-only files after every build
  (latest-wins, atomic, with a one-generation safety copy), so load
  time tracks live data instead of total history -- a long-lived graph
  had grown 94x its live size. Temporal queries also stop paying for
  results they discard: timestamps parse once at index time and only
  the returned top-k is materialized. Session-start context injection
  2.2s -> 0.9s on a project-shaped session at ~15k facts; temporal
  search 132 -> 28ms.
- Sync manifest builds skip excluded trees entirely instead of
  enumerating and discarding them: on a workspace-shaped corpus
  (hundreds of live files beside tens of thousands under `archive/`,
  `versions/` and `.git`) a build drops from 308ms to 12ms; corpora
  without such trees are unchanged.
- The embedding index reports how many files were too large to embed
  in full (`truncated_to_head`), so weakened tail recall is visible
  instead of silent.

- Semantic search caches its unpacked vectors between queries
  (invalidated automatically by any index write), and uses numpy as an
  optional accelerator when it happens to be installed -- never as a
  dependency. Hybrid search 219ms -> 85ms at ~2,200 files on top of
  the freshness-window win below; the pure-Python path returns
  identical results.
- Searches no longer pay the index-freshness sweep on every query:
  within a short window (`ATLAS_FTS_FRESHNESS_WINDOW`, default 10s) a
  burst of searches reuses the index, while the engine's own memory
  writes always void the window so remember-then-search stays exact.
  Keyword search 200.7ms -> 63.4ms and hybrid search 370.5ms ->
  219.3ms at ~2,200 files.

- Search freshness sweep is 4x faster (scandir walk with directory-read
  mtimes; excluded subtrees pruned, never entered) and fails open when
  the index database is briefly busy instead of erroring a search.
- Change detection compares (mtime, size), so a same-mtime rewrite on a
  coarse-timestamp mount (SMB, some Docker volumes) still reindexes.
- Human text that happens to be invalid FTS5 syntax retries as a
  literal phrase instead of failing with a message that blamed a
  "corrupt" index for a bad query.
- `sextant ingest` lands (forwards to `sextant-transcripts ingest`);
  corpus confirm paths compare separator- and case-insensitively.
- The umbrella CLI defers rich imports to first use (module import
  260ms -> 130ms measured).
- `memory_topics` aggregates tags from the search index instead of
  re-reading every corpus file per call (1,319ms -> 85ms at 2,000
  files, identical visibility).
- The dashboard's corpus-tree endpoint renders 4.8x faster (263ms ->
  55ms at 2,000 files) and the walk runs off the event loop, so a
  large corpus no longer stalls concurrent dashboard requests.
- Sync manifest builds are 8.5x faster when little has changed
  (866ms -> 102ms at 2,000 files, on both ends of every sync cycle):
  unchanged files reuse a cached digest keyed by (mtime, size), the
  directory walk reads entry types from the directory itself, and
  the secret fence's patterns compile once instead of matching
  per-file. A missing or corrupt cache just means a full re-hash.
- `sextant -v` works alongside `-V`/`--version`; `sextant-import`
  accepts `--dry-run` as an alias of its default report mode (and
  refuses the contradictory `--dry-run --yes`); the dashboard's
  missing-key refusal shows how to set the key in both shells; bare
  `sextant` with no corpus names the init command to run.
- Setting `SEXTANT_MEMORY_ROOT` (or any `SEXTANT_*` twin of a real
  `ATLAS_*` path variable) now warns on stderr instead of being
  silently ignored -- the natural wrong guess when every command is
  `sextant-*` used to land data at the default path with no error.
- The quickstart installs, smokes, and now also initializes the
  corpus; its full-test-suite line is labeled optional with an honest
  duration; the `pip install sextant` wrong-package warning sits
  directly above the install block instead of far below the fold.

### Fixed (quality wave 2, 2026-09-25 .. 2026-10-08)

- A corrupt search index now heals itself: FTS5 shadow-table damage
  can pass SQLite's integrity check yet fail every query -- search
  detects it, rebuilds the index tables in place (embedding vectors
  in the same file survive), and retries once; a file damaged beyond
  that is quarantined by rename, never deleted. Previously the raw
  database error reached the caller and keyword search stayed down.
- Version-before-edit snapshots: a file outside the corpus no longer
  crashes the write that tried to version it; extensionless files and
  names containing glob characters no longer produce a duplicate
  snapshot on every identical write; and two concurrent writers can
  no longer overwrite each other's snapshot (atomic slot claim).

- A search returning exactly one hit reported confidence "starved"
  (telling the model to ask a clarifying question instead of
  answering): the lone result skipped re-ranking and kept a raw
  negative FTS5 rank. A single match -- often the exact-unique-term
  best case -- now scores on the same 0-1 blend as everything else.
- Session-start no longer injects any built-in "house rules": standing
  rules come from your own corpus (`identity/house-rules.md`, bullet
  lines) and a fresh corpus injects none. Previously the maintainer's
  personal workspace conventions leaked into every session.

- The search freshness window is now voided by EVERY memory write path
  -- decisions, anti-memories, and confirmations included -- so
  write-then-search always sees its own write (previously only plain
  remembers did); a write landing during a sweep can no longer be
  hidden for a window.
- A corpus on an unwritable lock directory (container uid mismatch,
  ACL deny) fails fast with the real permission error instead of
  stalling 40 seconds and blaming a nonexistent concurrent writer.
- The sync digest cache follows git's racy-timestamp rule: a file
  rewritten within the same filesystem timestamp tick can no longer
  pin a stale hash (which would have reported in-sync while silently
  never pushing the change).

- Semantic indexing silently skipped any memory file larger than the
  embedding model's context window when running against newer Ollama
  releases: Ollama's legacy `/api/embeddings` endpoint now errors on
  over-length input instead of truncating, so the biggest (often most
  important) files quietly fell out of hybrid search while keyword
  search kept working. All three embed call sites now use the modern
  `/api/embed` endpoint, which truncates gracefully, and a source gate
  keeps the legacy endpoint from coming back.

- Scheduler catch-up runs recorded heartbeats under a synthetic job id,
  so every service restart replayed the entire weekly notification
  fleet (digest/decay/reverify pushes repeating near-daily). Heartbeats
  now record canonically and pre-fix ledgers self-heal.
- Cross-process write locks carry an owner token and release only
  their own lock: a slow (not crashed) holder whose lock was
  stale-broken could previously delete the successor's lock and admit
  a third concurrent writer (POSIX-only exposure).
- Transcript drop re-ingestion is idempotent in the search index
  (deterministic turn ids): a crash between store-append and archive
  no longer duplicates every turn on the next run.
- Three capture-pipeline concurrency bugs: concurrent same-titled
  browser captures no longer collide on a per-second filename (uuid
  suffix); same-stem re-drops within one second archive cleanly
  (counter suffix); concurrent first-time index initializations no
  longer race on the WAL journal-mode switch.
- Clip shows an "N/A" badge on browser-internal pages instead of doing
  nothing, and its README starts at API-key generation.
- Re-importing an Obsidian vault no longer duplicates every note: an
  unchanged note is recognized as already imported and skipped, while
  a changed note still lands as a new suffixed copy (imports never
  overwrite).
- On Windows, a concurrent memory writer could crash with "Permission
  denied" instead of waiting its turn: creating the lock file while
  the previous holder's delete was still pending raises a different
  error there, which the retry loop did not treat as "busy". It does
  now, with the same staleness and deadline discipline.

### Security (quality wave 2, 2026-09-25 .. 2026-10-08)

- The import CLI refuses decompression bombs: the export zip's
  `conversations.json` must declare under 1 GiB inflated before it is
  read into memory (a genuine larger export can be unzipped and
  imported as JSON directly). A corrupt or truncated zip now reports
  a clean error instead of a traceback.
- The dashboard's memory edit view no longer renders the file path as
  markup (a synced filename could carry HTML on POSIX); every
  `innerHTML` sink in the embedded UI is now inventoried and pinned
  by a test, so a new one fails the build until classified.

- The tier-3 secret fence now matches case-insensitively on every OS:
  `.ENV` or `server.KEY` no longer slips past sync on case-sensitive
  (Linux/macOS) filesystems. A filename with invalid UTF-8 bytes can
  no longer crash the manifest build, and a file that becomes
  unreadable between manifest and pull returns a clean per-file error
  instead of a server 500.
- The Brain chat pane renders model output as text, never as HTML --
  closing the same stored-injection class as the earlier click-handler
  hardening (model text is steerable by corpus content).

- Dependency floors for the network surfaces: `starlette>=1.3.1` and
  `python-multipart>=0.0.31` are now declared directly (fastapi's own
  range permits versions with published vulnerabilities), and the MCP
  SDK floor rises to `mcp>=1.28.1`. Fresh installs can no longer
  resolve into the known-vulnerable range; `constraints.txt` already
  pinned clean versions for those who use it.

- Bandit triage to zero actionable: scheme allowlists on the two
  user-configurable outbound URLs (a poisoned setting can no longer
  become a `file://` read), SHA1 entity ids marked non-security,
  documented suppressions for the parameterized-SQL false positives.
- The release builder verifies the built repository's actual commit
  identity and hard-aborts on mismatch; the dashboard auth cookie is
  test-pinned to never authenticate PUT/PATCH/DELETE or
  method-override tricks; rate-limiter buckets sweep at a ceiling so a
  caller-controlled identity header cannot grow memory unboundedly.
- The sync hub's bind guard is test-held: a non-loopback bind with an
  empty or revoked-only device registry refuses to start, and the
  unauthenticated health endpoint is pinned to disclose nothing.
- `docs/SECURITY-TEST-MATRIX.md` maps every README security claim to
  the exact tests that hold it, with a gate that fails the build if a
  cited test file is ever renamed or removed.
- Stored XSS in the dashboard closed: a corpus filename (or tag,
  ticket text, session name) containing an apostrophe could execute
  as JavaScript when its row was clicked, because HTML-escaping does
  not survive the browser's entity-decode before onclick JS parses.
  Every event-handler argument is now embedded as a JSON literal and
  then HTML-escaped, and a static gate keeps the vulnerable pattern
  from returning.

### Added (product-gap + quality wave, 2026-08-13 .. 2026-09-25)

- **Hybrid semantic search**: FTS5 fused with local embeddings
  (Ollama `nomic-embed-text`, reciprocal-rank fusion, automatic
  keyword-only degradation when embeddings are unavailable;
  `ATLAS_SEMANTIC_SEARCH=off` kill switch), plus `sextant-embed-index`
  and a nightly index-refresh job surfaced in the doctor.
- **Importers**: `sextant-import` for Obsidian vaults, ChatGPT exports,
  and claude.ai exports, with source auto-detection.
- **Docker path**: Dockerfile + compose recipe (`docs/HOWTO-docker.md`).
- **Sextant Clip** (`extensions/sextant-clip/`): one-click browser
  capture of claude.ai / ChatGPT / Gemini conversations into memory via
  the dashboard's new `POST /api/inbox/conversation` (header-only auth,
  size caps); generic transcript drop-dir ingestion
  (`sextant-transcripts ingest`) accepts `.jsonl`/`.json`/`.md`/`.txt`
  from any tool.
- **Spend visibility**: token usage captured at the provider seam for
  every Brain turn (tasks and interactive chat), Brain `GET /spend`,
  dashboard `/api/spend`, doctor "Token Spend (7d)" line; cost estimates
  from an `ATLAS_MODEL_PRICES`-extendable table (local models cost 0).
- **Safety-gated teardown**: `sextant-uninstall` / `sextant-reset` with
  report-only default, exact-path corpus confirmation, the
  `.sextant-protect` marker, and two-phase validation (nothing is
  deleted unless every confirmation passes).
- **Docs**: `docs/CONFIGURATION.md` (every environment variable, with a
  test that fails if a new env read ships undocumented), `docs/USAGE.md`
  rewritten as the public day-to-day MCP guide, public security-model
  section (capabilities and explicit non-goals), port-conflict and
  backup guidance, `constraints.txt` known-good pins with honesty tests.
- **CI**: Python 3.12 lane; gating `public-package` lane (rebuilds the
  public tree, builds sdist+wheel, twine-checks, pins wheel metadata);
  gating `deps-audit` lane (pip-audit; promoted after the tree audited
  clean); stranger-journey and docs-claims tests run on every push.

### Changed (2026-08-13 .. 2026-09-25)

- Search freshness is incremental: only changed files are re-indexed on
  the search path (measured 4.0x faster sweep; the former
  any-change-full-rebuild cliff is gone; `atlas-fts-rebuild --force`
  remains for recovery).
- Dashboard auth: the authed page load sets an HttpOnly SameSite=Strict
  cookie accepted for GET/HEAD only -- download/export links and the
  live-update streams carry no `?key=` in URLs; mutating routes remain
  header-auth-only (no CSRF surface).
- Auth primitives consolidated into `atlas_plus.auth` (timing-safe
  compare, bearer parsing, sliding-window rate limiter, failure log);
  web, Brain, and MCP compose from one module.
- CLI surfaces brand by invoked name (`sextant --version`, doctor
  header, per-command help never say Atlas when invoked as sextant-*).
- Public packaging metadata: SPDX `Apache-2.0` license expression,
  wheel metadata pinned by CI.

### Fixed (2026-08-13 .. 2026-09-25)

- Interactive remember was dead on fresh installs (background-write
  gate + missing routing defaults) -- found by a first-run usability
  audit on a clean machine.
- Background-write gate drift: one canonical fail-closed allowlist
  predicate everywhere, with a static regression gate (a helper
  previously wrote on `ATLAS_AUTONOMOUS=yes`).
- Three non-atomic memory-write paths (fact append, confidence
  downgrade, contradiction marking) now go through the cross-process
  lock + atomic write-and-replace; concurrent writers proven lossless.
- FTS search output no longer swallows matched terms (Rich markup
  escaping) and corpus text can no longer inject terminal styling.
- Windows reserved device names (CON, NUL, ...) rejected in scopes; a
  scope like `reference:CON` previously hung the server thread.
- `--help` is side-effect free on every command (the Telegram job
  poller used to process jobs when asked for help).

### Security (2026-08-13 .. 2026-09-25)

- Adversarial security review of the built release tree: private-project
  executor extracted and excluded from the public build, hardcoded
  personal rosters replaced with env configuration, internal codenames
  and example names swept, and two honest disclosures added to the
  security section (initial page-load URL token; optional ntfy relay
  transits ntfy.sh unless self-hosted).
- Dependency tree audited clean via pip-audit; quickstart and CI
  upgrade venv-bootstrap pip/setuptools so fresh environments carry the
  fixes.

### Added (launch-readiness arc, 2026-08-02 .. 2026-08-12)

- **Sextant Sync** (new subsystem, `atlas_plus.sync`): multi-device corpus
  sync -- hub/spoke over an authenticated HTTP protocol, content-hash
  manifests with three-way diff, per-device pairing tokens (hashed at rest,
  revocable), conflict quarantine (divergent edits never overwrite; both
  sides keep evidence), federation peers with default-deny path allowlists
  and a proposed-changes inbox, and a hard code-level fence so secrets never
  sync. CLI: `sextant-sync` (serve / pair / devices / revoke / status /
  plan / sync / checkin). Docs: `docs/HOWTO-sync.md`.
- **Self-hosting guide** (`docs/HOWTO-self-hosting.md`): whole-stack server
  deployment -- systemd units, a Tailscale-mesh recipe (recommended) and a
  public VPS recipe behind Caddy with automatic HTTPS, device wiring, and
  hardening notes.
- **`sextant-uninstall`**: safety-first clean removal. Report-only by
  default; deletions require explicit flags plus `--yes`; removing the
  corpus additionally requires typing its exact path back; a
  `.sextant-protect` marker in a corpus root refuses deletion
  unconditionally.
- **`sextant-reset`**: wipe an area and immediately re-scaffold it (same
  safety gates), for clean reinstall/debug loops.
- **`sextant-*` command aliases** for the user-facing family (`sextant`,
  `-init`, `-doctor`, `-mcp`, `-web`, `-brain`, `-backup`, `-transcripts`,
  `-dream`, `-sync`, `-uninstall`, `-reset`); the `atlas-*` names remain.
- **Scheduler outage catch-up**: nightly/weekly jobs missed while the
  machine was off are run once, staggered, at the next start
  (`ATLAS_SCHEDULER_CATCHUP=off` to disable); the doctor warns when the
  nightly family is stale.
- **Doctor first-run visibility**: a "Memory corpus" section with an
  explicit `sextant-init` hint on empty installs; a "Sync" section
  (devices, last check-ins, pending conflicts) when sync is in use.
- Contributor License Agreement (`CLA.md`) and a bug-report issue template.
- Advisory CI lanes: pip-audit and bandit run non-blocking on every push.

### Fixed

- Ticket listings treated archived tickets as live work (phantom open
  tickets fed the SLA checker and dashboards); archive directories are now
  excluded everywhere and ticket IDs are never recycled.
- The dashboard's service-control endpoints ran a blocking PowerShell call
  on the event loop, stalling all clients for seconds at a time.
- Unbounded growth: per-IP auth-failure tracking on the MCP server, the
  sync audit trail, and service log files (size-based rotation) are all
  capped now.
- Piped doctor output crashed with a Unicode error on Windows consoles.
- `requires-python` claimed 3.10 support while the code imports `tomllib`
  (3.11+); the floor is now stated truthfully everywhere.
- Silent exception swallows in the nightly contradiction/reflexion ticket
  paths now log warnings instead of hiding failures.

### Launch cleanup (Sextant public repo, 2026-07-27)

- docs/ curation: the public cut now ships an explicit re-include list only
  (ARCHITECTURE, USAGE, DECAY-AS-BEHAVIOR, INSPIRATION-ENGINE, six HOWTOs,
  seven ADRs). Internal sprint plans, competitive intel, threat plans,
  work summaries, QUICKSTART (machine-specific runbook), AGENTS.md,
  HUMANBLOCKERS.md, WhereFromHere.md, uv.lock, output/, and the personal ops
  scripts no longer ship.
- Builder genericizes retained docs at build time (workspace paths and the
  internal project label); private docs stay operator-accurate.
- Public packaging identity: distribution renamed `sextant-cp` in the built tree
  (import stays `atlas_plus`), description/keywords/classifiers/[project.urls]
  injected by the builder.
- Release gate hardened: names category is now case-insensitive (a lowercase
  ntfy-topic leak got through), new `workspace-path` category, prefix-capable
  allowlist; the builder itself no longer ships (it carries the patterns).
- New public-facing files: CONTRIBUTING.md, SECURITY.md, GitHub Actions CI
  (windows-latest, full suite).
- src docstring/comment sweep: remaining literal workspace-path examples
  genericized; `bulk_archive_tickets.py` now resolves the memory root via
  `atlas_plus.paths` instead of a hardcoded default.

### Release prep (Sextant public fork, 2026-07-05)

- Public name DECIDED: **Sextant** (ATL-022; "Sanctuary" was retired for the
  Sanctuary AI collision). License DECIDED: **Apache-2.0** -- LICENSE (verbatim
  2.0 text) + NOTICE (Protechtus LLC) added; `license` metadata in pyproject.

- The `atlas-memory` kernel `file:///` path dependency moved out of the core
  dependency list into an optional `[kernel]` extra (it was a phantom -- zero
  imports anywhere). The package now installs on machines that are not this one.
- Operator identity parameterized: new `atlas_plus.identity` resolves the identity
  file (`ATLAS_IDENTITY_PATH` -> `identity/user.md` -> first `identity/*.md`) and
  display name (`ATLAS_USER_NAME` -> frontmatter `title:` -> generic). Brain system
  prompt, prime hook, and session_start hook use it; personal-name and employer
  references swept out of `src/` docstrings and comments.
- Cross-platform path defaults: new `atlas_plus.paths` resolves workspace, memory,
  state, projects, and updates paths (env var -> legacy-workspace autodetect ->
  home-relative). ~100 hardcoded workstation-path fallbacks across ~50 modules replaced;
  `ATLAS_UPDATES_FILE` and `ATLAS_WORKSPACE_ROOT` are now honored everywhere.
- Deploy snippets templated (`<ATLAS_PLUS_DIR>` etc. in the Claude config snippets
  and stop hook); `run-weekly-status.ps1` is self-locating; personal name removed
  from deploy comments and the Task Scheduler XML. Remaining: the operational
  `deploy/*.ps1` fleet still hardcodes the local workspace (live Task Scheduler
  targets -- convert alongside a service restart window, not blind).
- Clean-machine boot gate: `scripts/boot-test-clean.ps1` clones to a temp dir,
  scrubs ATLAS_* env + HOME, fresh venv, installs without the kernel extra, and
  exercises imports, both session hooks, console scripts, and a unit subset --
  9/9 PASS on 2026-07-05 (the Sprint 3 exit criterion).
- Sanitization inventory (`docs/SANITIZATION-INVENTORY-2026-07-05.md`) +
  `scripts/release_gate.py` personal-identifier gate for the public push
  (baseline 276 hits, zero live secrets); runtime-code scrubs done: transcript
  watcher DEFAULT_ROOT home-relative, temporal fixture domain genericized.
- SCRUB EXECUTED, both gates green: `scripts/public_exclude.txt` (canonical
  not-shipped list, gitignore-style, shared by the gate and the future public
  repo builder) + `scripts/scrub_public_docs.py` (persona swap in test
  fixtures, operator/placeholder rules in docs; 66 files). release_gate.py now
  reports CLEAN over shippable files; `gitleaks git .` clean over all 514
  commits with `.gitleaks.toml` allowlisting the two redaction tests that
  plant synthetic secrets by design.

### Added

- `atlas-plus` namesake umbrella CLI (overview, `--version`, `commands` discovery of the
  installed atlas-* family) and `atlas-transcripts` CLI (`list` / `search` / `recover`
  over the transcript index) -- both console scripts previously pointed at modules that
  were never built and were DOA on install.
- Entry-point validation test: every `[project.scripts]` target must import and resolve.
- atlas-doctor: tunnel + policy-gate checks; operator secret-rotation checklist (ATL-007).

### Fixed

- `atlas-fts-rebuild` console script (pointed at a nonexistent `fts:__main__`; now a
  proper `main()` that forces a rebuild and exits 0). Stale `atlas-bootstrap` entry
  removed (the package was never built).
- sqlite connection leak in Brain `worker_detail()` on the error path.
- Scheduler `_sla_check` crash + stuck-alert state on a malformed `started_at`.
- ticket_sync executor shredded every Windows repo path (`replace(":", ";")` split
  drive letters, so a Windows drive path split into its drive letter and the rest); repo lists are now
  `;`-separated only, with a regression test pinning intact `cwd` paths.

### Security

- Redaction (ATL-017): JSON-shaped and quoted secrets (`{"api_key": "..."}`,
  `token='...'`) now redact -- the value pattern previously excluded quotes, so the
  dominant secret shape in Brain task results passed through unmasked. Pasted
  provider-prefixed keys (`sk-...`, `ghp_`, `github_pat_`) now redact even when they
  are not this machine's own env values. `RCLONE_CONFIG_PASS` added to the masked
  env-value list.
- The `ATLAS_AUTONOMOUS` kill switch is now a fail-closed allowlist (`on`/`1`/`true`);
  previously any garbage value (e.g. a typo) silently failed OPEN and allowed
  autonomous writes.
- ntfy inbound listener survives non-ASCII messages: a single emoji message on the
  (world-publishable) command topic raised TypeError in the constant-time compare and
  tore down the stream -- an outsider-inducible connection flap. Auth now compares
  bytes, and per-message failures are isolated from the stream loop.
- Telegram allowlist gate fails closed on an empty set (was: allow everyone; only
  reachable by bypassing main()'s startup guard, but a fail-open trap).
- Job poller: the Brain-fallback write-back watcher polled a garbage task id parsed
  from a display string (broken on the happy path), leaving every Brain-fallback
  ticket silently stuck in-progress. The id now flows as data; a dead watcher (404)
  writes back "blocked" visibly.

### Performance

- Excluded the reference/research mirror (~54% of the corpus) from memory_server
  FTS/search/list/catalog/topics AND from all curation walks; orphan inbound-ref map
  tokenized (123 s -> 1.1 s); doctor `collect()` parallelized with per-check isolation
  (~98 min -> seconds-scale).

---

## [0.17] -- TUI ecosystem integration, deep test coverage, docstring 100%, temporal graph full-stack

**Theme:** Achieve 100% public-function docstring coverage; complete the temporal graph across
all five access interfaces (CLI, Brain REPL, Web UI, Telegram, MCP); wire the operator's
TUI companion apps into Brain; and run a
sustained coverage sprint that pushed the test suite past 3,270 tests.

### Added

- Temporal graph accessible from all interfaces: CLI (`--json`, `entities` command),
  Brain REPL (`/temporal-search`, `/temporal-history`, `/temporal-entities`),
  Telegram (`/temporal_search`, `/temporal_stats`, `/temporal_history`, `/temporal_rebuild`),
  Web UI (`/api/temporal/history`, `/api/temporal/entities`), and MCP
  (`temporal_entities_tool`, `temporal_history_tool`, `temporal_search_tool`, `temporal_build_tool`).
- TUI ecosystem Brain wiring: `atlas_tui.BrainClient` + `get_brain_client()` factory;
  Brain proxy endpoints `GET /memory/search` + `POST /memory/remember`; companion TUI apps
  send session events, record domain facts, and queue agent tasks.
- suggest-facts pipeline: `skip_similar_threshold` Jaccard deduplication parameter;
  configurable `ATLAS_SUGGEST_CONFIDENCE_THRESHOLD` env var; `--threshold` flag across
  CLI, curation, Brain REPL, and Web API.
- Web UI Memory tab: Next Actions panel, Curation Health card, Untagged by Scope panel,
  Convergence Flags panel; `/api/memory/suggest-facts` and `/api/memory/suggest-tags`
  endpoints; Temporal Stats staleness warning.
- Brain CLI: `/decay-report`, `/temporal-rebuild`, `/suggest-facts`, `/suggest-tags`,
  `/queue-research`, `/next-actions`, `/gap-report`, `/curation-report`, `/session-log`
  commands; `/health` shows temporal staleness warning.
- Telegram: `/decay_report`, `/temporal_rebuild`, `/suggest_facts`, `/suggest_tags`,
  `/next_actions`, `/curation_report`, `/session_log`, `/gap_report` commands; long-output
  `_reply_long()` splits at the 4,096-char Telegram limit (fixes BadRequest crash).
- `atlas-doctor` TUI Activity panel (scans `.memory/sessions/` for per-app event counts);
  `check_research_gaps()` gap tickets panel.
- Policy gate `warn_only` parameter: denied/unavailable logs a warning and proceeds
  (completes the SHOULD-gate of the write-policy contract).
- Brain server `GET /tasks/export` endpoint (CSV + JSON, capped at 500 rows).
- WebSocket/SSE chat streaming: `POST /chat/stream` SSE relay; browser shows live
  tool-call progress; `progress_fn` fires `tool_call` events.
- `docs/TUI-INTEGRATION.md` integration guide; `ARCHITECTURE.md` and `README.md`
  public-polish passes (generic tagline, env var table, architecture diagram, accurate counts).

### Fixed

- Brain `server.py`: removed three undeclared `_check_auth()` calls in `/workers`,
  `/worker/health`, `/session/{id}/export` that caused NameError -> 500.
- Telegram `cmd_curation_report`, `cmd_temporal_rebuild`, `cmd_next_actions`, `cmd_session_log`
  now use `asyncio.to_thread` (async correctness); duplicate imports removed; positional
  arg bug fixed in `cmd_session_log`.

### Changed

- 100% public-function/class docstring coverage achieved across 50+ files.
- Test suite grown from ~2,015 to 3,270+ tests, covering every `check_*` doctor function,
  all Telegram `cmd_*` functions, and zero-to-full coverage for curation submodules
  (`decay`, `orphan`, `ref_graph`, `contradictions`, `link_check`, `tag_suggest`,
  `rederive`, `suggest_facts`, `duplicates`, `stale`, `weekly_report`) and MCP tool internals.

---

## [0.16] -- Research quality pipeline, gap-report automation, curation CLI completeness

**Theme:** Close the research-gap -> ticket -> research loop end-to-end; add research quality
as a first-class observable; reach full test coverage for dispatcher, curation, and Telegram.

### Added

- Dashboard Research Quality panel (`/api/research-quality-panel`, 60 s HTMX refresh).
- Brain CLI `/research-gaps [N]` and Telegram `/research_gaps [N]`: miss queries with
  ticket and report coverage; `--autoticket` flag files ATL tickets for uncovered gaps.
- Brain CLI `/gap-report [--watch] <topic>` and Telegram `/gap_report <topic>`:
  queue `research_gap` task from REPL or mobile.
- Telegram `/research_quality [topic]` topic filter (scans report frontmatter/filename).
- Web UI Memory tab Untagged by Scope panel (`/api/memory/untagged-scopes`, HTMX).
- `check_untagged_by_scope()` in atlas-doctor + scope breakdown in weekly digest.

### Fixed

- Dispatcher: `classify_result_quality()` heuristic now correctly routes `research_gap`
  ticket titles to the `research_gap` executor kind.

### Changed

- Full test coverage for `dispatcher.py`, Telegram `commands.py` (all `cmd_*` functions),
  and the curation stale/duplicates/weekly_report modules.
- `PLAN-v0.16.md` created; `ARCHITECTURE.md` updated with the research_gap kind and new
  Telegram/Brain CLI commands.

---

## [0.15] -- Ntfy task completions, research quality gate, scope tagging, system health from Telegram

**Theme:** Close the research pipeline with a quality gate and retry; surface system health
from mobile; automate the gap-to-ticket flow.

### Added

- Worker ntfy push on task completion (`ATLAS_NTFY_COMPLETIONS=on`): high-priority
  notification when a task is done or failed.
- `research_gap` executor: extracts topic from ticket title, calls `run_research()`,
  handles dedup (marks done on ValueError) and failure.
- Research retry on low quality: `_retry_low_quality()` step in `run_research()`, gated by
  `ATLAS_RESEARCH_RETRY=on`; re-runs synthesis with a stricter prompt when the initial
  `quality_score` falls below `ATLAS_RESEARCH_RETRY_THRESHOLD`.
- Telegram `/sys_health` (alias `/health`): compact health snapshot (services, worker,
  memory confirmed%/tagged%, pending facts, exhausted tasks).
- `stale_running` count in `task_stats()` (running tasks with heartbeat >30 min old).
- `check_untagged_by_scope()` in atlas-doctor; `--autoticket` flag in curation gaps;
  weekly digest untagged-file hint when `untagged_files > 10`.

---

## [0.14] -- Task telemetry, exhausted-task tracking, Brain CLI /plan and /recent

**Theme:** Make the task lifecycle fully observable: attempt history, exhausted-retry
tracking, a plan view, and per-task retry status surfaced everywhere.

### Added

- Task attempt telemetry: `task_attempts` SQLite table; `record_task_attempt()` +
  `list_task_attempts()`; worker records a row after every execution; Brain CLI `/task <id>`
  shows the attempt-history table.
- Exhausted task tracking: `list_exhausted_tasks()` and `check_exhausted_tasks()`;
  Brain CLI `/exhausted`; Telegram `/exhausted`; Web UI exhausted badge; `exhausted_total`
  in `/tasks/stats`.
- Brain CLI `/plan [N]` and Telegram `/dispatch_plan`: dispatch-eligible todo tickets
  ranked by priority. Brain CLI `/recent [N]` and Telegram `/recent_tasks [N]`.
- Worker heartbeat monitoring: `update_heartbeat()` + `_HeartbeatKeeper` daemon thread
  renews the lease every `ATLAS_WORKER_HEARTBEAT_INTERVAL` seconds.
- Research quality gate: `_apply_quality_gate()` checks word count and zero-citation
  conditions; writes `quality_gate_passed:` / `quality_gate_flags:` frontmatter.
- Auto-session-log scheduler job (daily 23:30 UTC); `atlas-sprint-summary write-session-log` CLI.

### Changed

- `ARCHITECTURE.md` updated (task_attempts table, heartbeat keeper, auto-session-log,
  new env vars); `the Testing section of docs/ARCHITECTURE.md` synced.

---

## [0.13] -- Research dedup body-recall, atlas-doctor summary, Brain CLI coverage sprint

**Theme:** Harden the research pipeline (dedup, quality gate, retry), make atlas-doctor
comprehensive, and reach full Brain CLI test coverage.

### Added

- Research dedup body-recall: a secondary signal in `find_similar_research_reports()`
  (fraction of topic tokens in the first ~300 body words * 0.6) catches differently-titled
  reports on the same topic.
- `atlas review-memory summary`: aggregate memory-health dashboard (total files, confirmed%,
  tagged%, avg health, orphan candidates, hub files, stale confirmed).
- `suggest-tags` summary stats (`no_suggestion_count` + `already_tagged_count`).
- Full Brain CLI command test coverage (30+ commands).

### Fixed

- `write-session-log` "today"/"yesterday": `_is_date()` extended to match
  `YYYY-MM-DD HH:MM:SS` for correct git date filtering.
- Brain CLI `/research-index` `ValueError` on a 5-element tuple unpack after the tuple
  grew to 6 fields.

---

## [0.12] -- Sprint summary generator, memory reference graph, escalation fixes

**Theme:** Instrument the build process itself (sprint summary), add memory-graph topology
(hub detection), and harden the escalation + dispatch pipeline.

### Added

- Sprint summary generator (`sprint_summary.py` + `atlas-sprint-summary` CLI): parses git
  log by sprint tag, extracts test counts, emits table/agents-md/session-log/JSON.
- Memory reference graph: `ref_graph.py` (`find_hub_files()`); `atlas review-memory ref-graph`;
  `check_ref_graph()` in atlas-doctor; Brain CLI `/loop`.
- Research abstract injection (`## Summary` -> `abstract:` frontmatter) and cross-links
  column (`cross_ref_count`) in research-index.
- `test_brain_server.py` (37 tests: auth middleware, health, dispatch-stats, scheduler,
  routing, credit, rate-limit).

### Fixed

- Escalation: `verify` and `claude_supervisor` added to `_NON_AGENT_KINDS` so verify
  failures reopen the ticket rather than spawning a new Claude Code escalation task.

### Changed

- `README.md` complete rewrite: automation-loop diagram, all services, Brain CLI commands,
  scheduler jobs, docs table, MCP tools table.

---

## [0.11] -- Ticket reopen tracking, memory orphan detection, verify pipeline

**Theme:** Close the automated verify loop (run pytest after agent completion) and make
orphaned memory files and chronically-reopened tickets first-class observables.

### Added

- Auto-verify pipeline: `_verify` executor runs the full pytest suite; `create_verify_task()`
  queues verify after agent completion (`ATLAS_VERIFY_TICKETS=on`);
  `_reopen_ticket_from_verify_failure()` reopens the ticket on test failure; verify badge in
  the Web UI ticket board.
- Memory orphan detection: `orphan.py` composite `orphan_score`; `atlas review-memory orphans`;
  `check_orphans()`; Brain CLI `/orphans`; Telegram `/orphans`.
- Ticket reopen count: `Ticket.reopen_count` incremented on every todo transition; Web UI
  "Reopened Nx" badge; atlas-doctor `high_reopen` list (>= 3x).
- Research cross-links: `inject_cross_refs()` scans `references:` URLs against
  `.memory/reference/`.

### Changed

- `ATLAS_RESEARCH_DEDUP_DAYS` env var drives `run_research()` `max_age_days` (default 30).

---

## [0.10] -- Automated ticket dispatch, quality gate, dispatch observability

**Theme:** Fully automate the ticket -> task -> result -> reopen loop with quality scoring,
SLA alerting, and a dedicated dispatch-stats panel.

### Added

- Automated ticket dispatcher (`dispatcher.py`): `_ticket_dispatch` scheduler job (every
  10 min); agent + priority maps; marks ticket in-progress after dispatch.
- Result quality gate: `classify_result_quality()` 0-10 score; `reopen_ticket_if_low_quality()`
  reopens when score < `ATLAS_TICKET_QUALITY_THRESHOLD`; wired into `worker._finish_task()`.
- Dispatch observability: `GET /dispatch-stats`; Web UI dispatch-stats badge;
  `check_dispatcher()` in atlas-doctor; ntfy push on dispatch.
- Verifier executor (`_verify`) auto-queued after agent completion.

---

## [0.9] -- Research index CLI, freshness scoring, memory file health, link validation

**Theme:** Make the research library self-maintaining: index it, score freshness, check
link rot, score file health, and surface gaps as auto-filed tickets.

### Added

- `atlas review-memory research-index` (date/score/citations/words; `--since`, `--min-score`,
  `--sort`, `--json`); Brain CLI `/research-index`.
- Research freshness score (`compute_freshness_score()` linear decay 1.0 today -> 0.0 at
  365 days) injected to frontmatter.
- Memory file health score (`compute_file_health()` composite of tags + confirmed + updated
  + confidence); `atlas review-memory health-score`.
- Research link validation: `check_url()` HEAD+GET fallback; concurrent `check_report_links()`;
  `remove_broken_links()` `--fix`.
- Memory tag auto-suggestion (`tag_suggest.py`) + nightly scheduler job (04:15 UTC).
- Weekly Brain digest push (Monday 09:00 UTC) via ntfy.

### Fixed

- Agent dedup: `ValueError` on duplicate research now marks the task done (not failed).
- `_topic_tokens()` hyphen-split fix (`[a-zA-Z]{4,}`).

---

## [0.8] -- Research quality scoring, citation tracking, result digests, memory trends

**Theme:** Give every research report a deterministic quality score and provenance trail;
add a memory-trend time series; surface Brain task result digests.

### Added

- Research quality scoring (`score_research_report()` deterministic 0-1) + nightly backfill
  (04:10 UTC).
- Research citation tracking (`extract_report_citations()` + `references:` frontmatter).
- Research topic deduplication (`find_similar_research_reports()` Jaccard `threshold=0.5`;
  `--force` bypass).
- Fact provenance trail: `session_id`/`task_id` on `append_fact()` + `memory_remember()`;
  `source_session:`/`source_task:` frontmatter; `memory_remember_tool_v2`.
- Memory trends: daily job (04:05 UTC) appends to `.atlas/memory-trends.jsonl`; atlas-doctor
  Memory Quality panel sparkline + delta.
- Brain task result digest (`result_digest` column; Qwen3:1.7b, `ATLAS_DIGEST_RESULTS=on`).
- Memory git autocommit (`ATLAS_MEMORY_AUTOCOMMIT=on`).

---

## [0.7] -- Memory topic index, duplicate detection, Brain session export, Telegram memory access

**Theme:** Give memory a topology (topic cloud/index) and add self-service memory access
from Telegram and Brain CLI export.

### Added

- Memory topic index (`_build_topic_index()`; `/api/memory/topics`; `memory_topics_tool`).
- Duplicate fact detection in `memory_remember_tool` (`_jaccard()` + `_find_duplicate()`,
  FTS search, Jaccard >= 0.45; `possible_duplicate` in response).
- Brain session export (`session_export_markdown()`; `GET /session/{id}/export`).
- Telegram `/memory <query>` (FTS5 search) and `/confirm <path>`.
- Confidence decay for stale confirmed facts (`downgrade_stale_confirmations()`;
  `_unconfirmed_decay_weekly` job, Sunday 03:40 UTC).
- Web UI dark/light theme toggle.

### Fixed

- `json.dumps(default=str)` handles PyYAML date objects in the `last_confirmed` column.

---

## [0.6] -- Automated DB backup, task deduplication, FTS confirmed-fact scoring, anti-memory propagation

**Theme:** Make memory writes self-protecting (dedup, anti-propagation, DB backup) and add a
per-worker health API.

### Added

- `atlas-db-backup` CLI (WAL-safe `VACUUM INTO`; list/restore/prune/status); daily job
  (02:00 UTC); `check_db_backup()`; `ATLAS_DB_BACKUP_KEEP` (default 14).
- Task dedup at `/task` (`find_duplicate_pending()` before `create_task()`).
- FTS re-ranking blends BM25 65% + mtime 20% + `last_confirmed` 15% (safe column migration).
- Anti-memory propagation (`propagate_anti_rejection()` flags keyword-matching files with
  `anti_flagged: true`).
- `worker_detail()` per-worker stats; `GET /worker/health`; `check_worker_detail()`.

### Fixed

- FTS ESCAPE bug: corrected single-quote escaping (now uses a pipe escape char).
- Confidence decay correctly skips no-`last_confirmed`, already-low, archive, and
  recently-confirmed files.

---

## [0.5] -- Brain CLI 45+ commands, SLA alerting, Telegram /watch, Claude Code escalation

**Theme:** Promote the Brain REPL to a full operational console; add SLA-breach alerting and
escalation to Claude Code for complex failures.

### Added

- Claude Code escalation pipeline (`escalation.py`: failure/quality gates, circuit breaker);
  Telegram `/claude` + `/supervisor`; Web UI Ask Claude panel; `claude-supervisor` weekly
  executor.
- SLA alerting: `_sla_check()` scheduler job (every 10 min); `ATLAS_SLA_MINUTES`; ntfy
  high-priority push on breach; atlas-doctor SLA panel.
- Brain CLI promoted to 45+ commands (tasks/submit/approve/reject/search/agents/sessions/
  browse/save/stats/priority/retry/escalate/kill/watch/verify/export-session ...).
- Telegram `/watch <task_id>` (polls every 15 s to terminal state).
- Scope inference for extracted facts (`_infer_scope()`).
- FTS5 semantic re-ranking (BM25 0.75 + recency 0.25 + 1.2x authority boost), scores 0-1.

### Fixed

- FTS5 dedup collapses same-path multi-matches to the highest-scoring row.
- `/search` confidence-band display (was a dead code path).

---

## [0.4] -- Convergence pipeline, stale decisions, re-derivable detection, 1000-test milestone

**Theme:** Build the full memory curation pipeline (convergence scanning, stale-decision
review, re-derivable fact detection) and run a broad coverage sprint to 1,000 tests.

### Added

- Convergence integration (`check_convergence()`; `/convergence`; `convergence_report`
  executor; nightly job Sunday 03:35 UTC).
- Stale Decisions tracking (`atlas review-memory decisions`, flags >90-day unreviewed;
  `check_decisions()`; Web UI panel).
- Re-derivable memory detector (`atlas review-memory rederive`; `check_rederivable()`).
- Mining-source auto-trigger in the Stop hook (`ATLAS_DISABLE_TRANSCRIPT_MINING=1` kill switch).
- New test modules reaching the 1,000-test milestone (session_patterns, memory_agent,
  temporal_graph_scoring, contradictions, hook_entry, renderer, search_log, anti, fts,
  versioning).

### Fixed

- `session_patterns.py`: fixed `build_cooccurrence` return-type ambiguity; removed a dead
  `isinstance` guard in `save_patterns`.

---

## [0.3] -- Session narrative, routing stats, Web UI expansions, research web search

**Theme:** Expand the Brain's self-awareness: cross-session narrative, routing observability,
research web search, and transcript mining.

### Added

- Session narrative (`session_narrative.py`: open loops, direction signals, energy; weekly
  job Sunday 03:55 UTC; Web UI panel; `atlas review-memory narrative`).
- Routing stats (`GET /routing/stats`; Web UI Agents-tab panel; `/diagnostic` routing stats).
- atlas-doctor worker liveness check (`check_worker_status()`).
- Research agent web search (DDG Instant Answer + SearXNG; `ATLAS_RESEARCH_WEB_SEARCH=on`;
  result cache).
- Curation gaps `--autofile` (stub `.memory/gaps/` files); `TranscriptMiningSource`;
  `atlas review-memory convergence`.
- Epistemic provenance: `memory_confirm_tool` + `confirm_fact()` + `mark_contradiction()`
  (`first_observed`, `source`, `confidence`, `last_confirmed`).
- `docs/ARCHITECTURE.md` created.

### Fixed

- `date.today()` UTC/local drift in `routing.py`.
- `web/app.py` double-error bug (error shown over result).
- Memory path traversal in `memory_confirm` (`_safe_resolve()`).
- Malformed-JSON crash: Ollama 200 + bad body now raises `ValueError`.

### Changed

- Ticket loop closed (task done/failed/blocked -> ticket done/todo/blocked); assignee wired
  as agent hint; smart routing default on; `ATLAS_LOCAL_ONLY` mode.
- `list_tasks()` gains `sort_by` + `sort_desc` + rowid tiebreaker.

---

## [0.2] -- Brain v0.2, MCP memory server, session summarizer, opt-in remembering

**Theme:** Build the core Atlas stack: MCP memory server, Brain agent (Qwen3, 8-agent
registry), session summarizer, opt-in fact extraction, and harden the foundation.

### Added

- MCP read-only memory server (`atlas_plus.mcp.memory_server`) with the memory list/read/
  frontmatter/search/remember/anti/decision tools.
- Session summarizer (`atlas-summarize-session`): Stop hook writes `.memory/sessions/` +
  `updates.txt`; git diff captured to `.atlas/fsdiffs/`.
- Brain v0.2: local Qwen3 orchestration agent, async task system, 8-agent registry,
  Web UI v0.5.0 (7 tabs), self-improvement loop, credit fallback to Claude Haiku.
- Decision memory type (`memory_remember_decision_tool`: `revisit_if`, `conditions`,
  `alternatives_rejected`); routing.yaml decision/constraint/observation scopes.
- Task-to-memory pipeline (`PendingFacts` table + Web UI panel; high-confidence
  claude-code facts auto-approved at >= 0.95).
- FTS5 full-text search (confidence bands, miss logging, LIKE-wildcard escaping,
  `_safe_resolve()` path-traversal guard).
- Personas framework; skills/runbooks tools; `project_state_tool`; ticket system tools.
- `ATLAS_AUTONOMOUS` kill switch enforced at all write primitives.

### Fixed

- Ollama tags fetched once per refresh (N -> 1) via a 15 s TTL cache.
- OpenCode exception handling; `run-brain` env source fix.

### Security

- Brain and Web UI bound to loopback with fail-closed Bearer auth (`hmac.compare_digest`);
  Cloudflare named tunnel + Access Layer 1 (email + service-token policy, default-deny).
- Backups use the crypt remote (`gdrive_crypt:`); `_SECRET_PATTERNS` hard-excludes Tier 3
  files regardless of caller.
- `ATLAS_PARANOID=1` gates JWT/RSA enrollment (off by default).
