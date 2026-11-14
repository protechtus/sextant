# Security claims and the tests that hold them

Every claim in the README's "Security model, honestly" section is pinned
by automated tests that run on three operating systems on every push.
This matrix maps claim to enforcement so you can check our homework --
and so a future change that breaks a promise turns a build red instead
of shipping quietly.

| Claim | Held by |
| --- | --- |
| Background writes are off by default and fail closed (only `on`/`1`/`true` enables; typos stay off) | `tests/test_autonomous_gate_drift.py` -- including a static gate that forbids new direct env reads from bypassing the canonical predicate |
| Interactive writes are consent-by-request and `ATLAS_INTERACTIVE_WRITES=off` locks them down | `tests/test_interactive_writes.py` |
| Every network surface requires a token; missing config refuses rather than opens | dashboard: `tests/test_web_app.py` (401/refuse-to-start paths); Brain: `tests/test_brain_server.py`; MCP: `tests/test_memory_server.py`; sync: `tests/test_sync_api.py` |
| The dashboard auth cookie alone never authorises a mutation: a POST/PUT/DELETE with the cookie is refused unless it also carries the `X-Sextant-Request` header and an `Origin` that matches the dashboard exactly; method-override tricks and the Clip inbox route refuse the cookie outright | `tests/test_web_app.py` (cookie method matrix) |
| Repeated auth failures rate-limit, alert, and hard-lock the source | web: `tests/test_auth_failures.py`; MCP: `tests/test_mcp_lockout.py` (bounded tracking tables, expiry sweeps); Brain HTTP + WebSocket, sync hub and the dashboard WebSocket share the same failure accounting: `tests/test_wave2.py` |
| Rate-limiter state cannot be grown unboundedly by a caller-controlled identity | `tests/test_auth.py` (bucket ceiling sweep) |
| A forwarded client address is believed only from loopback or a listed proxy, must be an IP address, counts against that client alone, and a flood through one proxy locks the proxy | `tests/test_sweep16.py` (auth accounting), `tests/test_sweep15.py` (trusted-peer matcher) |
| Secrets in transcripts and facts are redacted before any model call or write: key/value pairs in every common spelling (snake, camel, prefixed, suffixed, quoted, full-width, Japanese/Korean/Chinese names), URL credentials, provider tokens, PEM/PGP blocks, CLI flags, Basic auth; the rule is linear in the input | `tests/test_sweep16.py`, `tests/test_sweep15.py`, `tests/test_redaction.py` |
| The sync hub refuses a non-loopback bind with an empty or revoked-only device registry | `tests/test_sync_api.py` (bind guard, both entry paths) |
| The sync health endpoint discloses nothing but liveness | `tests/test_sync_api.py` (no-disclosure assertion) |
| Device tokens are hashed at rest and compared timing-safely | `tests/test_sync_devices.py`; comparison via `hmac.compare_digest` |
| Corpus writes are crash-safe: cross-process lock + atomic write-and-replace; a stolen-lock scenario cannot admit concurrent writers | `tests/test_mcp_routing.py` (static no-plain-write gate) + `tests/test_interactive_writes.py` (concurrent writers lossless; lock ownership) |
| A corrupt search index self-heals (in-place rebuild preserving co-resident data) and a file damaged beyond that is quarantined by rename, never deleted | `tests/test_fts.py` (shadow-table and file-level corruption injected; both recovery paths pinned) |
| Version-before-edit snapshots survive concurrent writers (atomic slot claim, no overwrites) and crash-mid-copy never fakes history | `tests/test_versioning.py` (concurrency no-loss/no-empties; edge probes) |
| Uninstall/reset validate every confirmation before deleting anything; the `.sextant-protect` marker is absolute | `tests/test_uninstall_cmd.py`, `tests/test_reset_cmd.py` |
| Web capture (Sextant Clip) cannot be CSRF'd into your corpus -- the inbox endpoint is header-auth only | `tests/test_clip_inbox.py` |
| Corpus content cannot inject terminal styling or break search rendering | `tests/test_umbrella_clis.py` (markup escaping) |
| Corpus-derived strings (filenames, tags, ticket text) cannot execute as JS in the dashboard -- every event-handler argument is embedded as a JSON literal, not a quoted splice | `tests/test_web_app.py` (hostile-filename renders inert + a static gate forbidding the vulnerable pattern) |
| Hostile scope input (path traversal, Windows reserved device names) is rejected | `tests/test_interactive_writes.py`, `tests/test_temporal_graph.py` (path guards) |
| Outbound URLs from user config are scheme-allowlisted (no `file://`) | `tests/test_embeddings.py` |
| A hostile import archive cannot decompression-bomb the host -- the export zip's declared inflated size is capped before any bytes are read, and a corrupt archive fails with a clean error | `tests/test_import_cmd.py` (TestZipHardening) |
| The operator action log cannot be forged -- newlines in a completion note or a hand-written ticket's frontmatter are flattened at the sink, so one action is always exactly one log line | `tests/test_web_app.py` (TestOperatorPanel forge tests) |
| `ATLAS_LOCAL_ONLY=on` refuses cloud providers on every LLM path -- the provider registry AND the summarizer's own fallback chain (the stop hook, suggest-facts, dream mode, contradictions, reflexion and weekly synthesis all route through the latter) | `tests/test_llm.py` (TestLocalOnly), `tests/test_summarizer_providers.py` (TestLocalOnly) |
| Dependencies audit clean, and a new CVE is a red build | the gating `deps-audit` CI lane (pip-audit) |
| The shipped package is rebuilt, gate-scanned, built into sdist+wheel, and metadata-pinned on every push | the gating `public-package` CI lane |

Claims deliberately NOT made -- and therefore not in this table: no
encryption at rest, no multi-user auth, no protection from what your
own model does with retrieved content. See the README's security
section for those non-goals in full.
