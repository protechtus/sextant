# Sextant on LongMemEval

A reproducible run of the LongMemEval_S benchmark (500 questions over long chat histories)
that answers one question: when an assistant has to recall something from a long history, what
do you get, and what do you pay, for looking it up in Sextant instead of pasting the whole
history into the prompt?

This is a measurement, not a leaderboard entry. Read "What this does not show" before quoting
a number from it.

## What is compared

Every arm asks the same model the same question with the same reader prompt (the one in
LongMemEval's generation script). The only thing that changes is which sessions the model is
shown.

| Arm | What the model sees | Why it is here |
|---|---|---|
| `none` | the question and its date | floor: what the model answers with no memory |
| `oracle` | only the sessions that hold the evidence | ceiling: the best any retriever could do |
| `full` | the entire history, about 115k tokens by the dataset's count | the "just use a big context window" baseline |
| `sextant-fts-<build>-k5`, `-k10` | the top 5 or 10 sessions returned by Sextant's `memory_search_tool` for the question as asked | Sextant with its default keyword index, no model and no service involved in retrieval |
| `sextant-hybrid-<build>-k5`, `-k10` | the same with the optional local embedding index (Ollama, `nomic-embed-text`) | Sextant with semantic search switched on |

Sextant is used the way it ships: each question's history is written into a fresh memory root
as plain markdown, one note per session, and searched through the function the MCP server
exposes. There is no extraction step and no model call at write time.

Reported for every arm and model: accuracy with a 95% Wilson interval, accuracy by question
type, abstention accuracy, mean input tokens per question, list-price cost per question, and
an exact McNemar test for each Sextant arm against `full`, `none` and `oracle` on the same
questions. Retrieval is also reported on its own (did the evidence sessions come back?), which
costs nothing to reproduce.

## Results, run of 2026-11-20

One run per arm. Judge: Claude Opus 5.5 with the official prompts. `v1` is Sextant with the
search fix described under "What the run found in Sextant"; `v0` is the build that was public
that morning. Full figures, per-type breakdowns and paired tests: `results/results-2026-11-20.json`.

| What the model was shown | Haiku 5.5 | Sonnet 5.5 | Input tokens per question |
|---|---|---|---|
| Nothing (floor) | 9.0% | 8.8% | 66 |
| Sextant v0, keyword, top 10 | 15.8% | 16.8% | 1,052 |
| Sextant v1, keyword, top 5 | 83.2% | 86.6% | 22,037 |
| Sextant v1, hybrid, top 5 | 86.8% | 88.0% | 21,396 |
| Sextant v1, keyword, top 10 | 88.0% | not run | 42,188 |
| Sextant v1, hybrid, top 10 | 89.8% | not run | 39,556 |
| **Sextant v1 as an agent uses it** (the model searches and reads, up to 5 rounds) | **89.4%** | **91.8%** | 21,597 Haiku, 13,673 Sonnet (all rounds added up) |
| The whole history | 91.6% | 95.6% (251 questions) | 167,702 |
| Evidence sessions only (ceiling) | 93.6% | 96.8% | 9,293 |

The agent arm (`agent.py`) gives the model two tools, `memory_search` and `memory_read`, over
the hybrid index and lets it decide what to look for. Sonnet ran 1.6 searches and opened 1.4
notes per question on average, Haiku 2.7 and 2.0. Against the whole history on the same
questions the agent arm is behind by 2 to 4 points, and at this sample size the difference is
not statistically clear (exact McNemar p = 0.13 on Haiku, 500 questions; p = 0.08 on Sonnet,
251 questions), while reading 13% (Haiku) and 8% (Sonnet) of the tokens.

One repair in that arm is disclosed here: the last round is sent with tools switched off, and
in the first pass 12 conversations per model came back as an empty turn. Those 24 were asked
once more with a line saying the searches were used up; the harness now sends that line on
every final round. One Sonnet conversation still ended without an answer and is scored wrong.

500 questions per cell unless stated; 95% intervals are about plus or minus 3 points at 500
questions. The Sonnet whole-history arm ran on a stratified half (every second question id
within each type) to hold cost down; one of its 251 requests was declined by the model's
safety filter and is scored wrong.

What the numbers say:

- **Pasting the whole history has the highest score on this benchmark.** It beats every
  single-search Sextant arm clearly, and the agent arm by a margin this run cannot separate
  from chance. On Haiku the gap to hybrid top 10 is 1.8 points and
  is not statistically distinguishable (exact McNemar p = 0.20, 15 questions won against 24
  lost). On Sonnet the gap to hybrid top 5 is about 8 points and is clear (p < 0.0001).
- **Sextant gets most of the way there on a fraction of the prompt.** Hybrid top 10 reads 24%
  of the tokens the whole history takes; top 5 reads 13%. At list prices on Haiku that is
  0.4 cents a question against 8.5 cents, because prompts above 100K tokens are billed at
  Haiku's higher tier.
- **Where Sextant loses.** Questions that need several sessions at once: multi-session
  (81.0% against 82.6% on Haiku, 79.3% against 90.6% on Sonnet) and temporal reasoning (89.0%
  against 93.7%, 83.5% against 96.8%). One search with the question as typed brings back every
  evidence session in the top 10 for 91 to 95% of questions; the rest cannot be answered in
  full from what was retrieved.
- **The search fix is most of the story.** The same system scored 16% before it.

## Second round: development half and held-out half (set on 2026-11-20, before any tuning)

After the first run the 500 questions were divided in two by the same rule that chose the
Sonnet whole-history sample: within each question type, ids in sorted order, alternating.

- **Held-out half** (`--every 2`, 251 questions): the half that already has a Sonnet
  whole-history score. Nothing is tuned on it. Final figures for any changed search or agent
  behaviour are reported on this half only.
- **Development half** (`--every 2 --offset 1`, 249 questions): changes to search and to the
  agent's tools and prompt are tried here.

The first-run figures above were all produced before this division and before any tuning.

What was tried on the development half (249 questions), and what was chosen:

| Agent variant | Haiku 5.5 | Sonnet 5.5 | Sonnet input tokens / question |
|---|---|---|---|
| 1: first-run wording | 88.4% | 91.6% | 14,016 |
| 2: wording adds "do not answer from snippets, open every candidate note" | 89.6% | 92.8% | 15,670 |
| 3: a search returns its top 3 notes in full, snippets for the rest | 89.6% | 94.4% | 29,884 |
| Evidence only (ceiling) on the same questions | 93.2% | 96.0% | |

Why variant 3: on this half, when Sonnet had opened every evidence note it was right 131 times
out of 133, and 15 of its 19 misses were answers given from snippets with an evidence note
never opened. Telling it to open more notes (variant 2) raised notes opened from 1.44 to 1.65
a question and the score by a point. Having the tool return the notes did what the wording
could not. None of these differences is statistically clear on 249 questions (variant 3
against 1 on Sonnet: 10 gained, 3 lost, p = 0.09), which is why the held-out half decides.

**Chosen before looking at the held-out half (2026-11-20 14:30):** variant 3 for both models,
hybrid index, reported on the held-out 251 questions for the 50-session haystack and for the
476-session haystack (`m`), against the first-run figures on the same questions. Haiku paid
for variant 3 with many more tokens (71,003 a question, it searches about twice as often) for
no clear gain; that is reported as it lands.

### Held-out result (251 questions, none used for tuning)

Every row is scored on the same 251 questions (`python subset.py <reference-job> <job> ...`).

| What the model was shown | Haiku 5.5 | Sonnet 5.5 | Input tokens per question (Haiku / Sonnet) |
|---|---|---|---|
| Sextant, one search, hybrid (top 10 Haiku, top 5 Sonnet) | 90.8% | 87.3% | 39,833 / 21,451 |
| Sextant as an agent uses it, first-run tools | 90.4% | 92.0% | 21,131 / 13,278 |
| **Sextant as an agent uses it, search returns the top 3 notes in full** | **93.6%** | **93.6%** | 66,491 / 26,334 |
| The whole history | 93.2% | 95.6% | 167,632 / 166,970 |
| Evidence sessions only (ceiling) | 94.0% | 97.6% | 9,308 |

- **Haiku: level with the whole history** (93.6% against 93.2%; 8 questions won, 7 lost, exact
  McNemar p = 1.0) on 40% of the tokens, and within half a point of the ceiling. At list
  prices that is 0.85 cents a question against 8.5.
- **Sonnet: 2 points behind the whole history**, a gap this sample cannot separate from chance
  (7 won, 12 lost, p = 0.36; it was 6 against 15, p = 0.08, before the change), on 16% of the
  tokens. At list prices that is 5.5 cents a question against 33.7.
- **The change did what the development half said it would for Sonnet and more than it said for
  Haiku.** Development half: 94.4% and 89.6%. Held-out: 93.6% and 93.6%. The Haiku figure moved
  4 points between the halves, which is the size of the interval on 250 questions; read both
  halves together (91.6% over 500) rather than the better one.
- **What is left is mostly reading, not finding.** On the held-out half Haiku had every evidence
  note in front of it for all 16 of its answerable misses. Sonnet: 6 misses with everything
  read, 4 with an evidence note listed and not opened, 5 where a search never surfaced one
  (`analyze_agent.py`). The weakest type is `single-session-preference` (10 and 12 of 15),
  where the question shares few words with the note that answers it.

The change is in Sextant itself as `memory_search_tool(..., full=3)`
(`tests/test_search_full_bodies.py`).

### The history that cannot be pasted (LongMemEval_M, same 251 held-out questions)

LongMemEval_M puts each question in a haystack of about 476 sessions, roughly 1.75 million
tokens by our count. That is past the context window of the models used here, so the
whole-history arm cannot be run at all: retrieval is the only way to answer.

| 476-session haystack, agent with `full=3`, keyword index only | Haiku 5.5 | Sonnet 5.5 |
|---|---|---|
| Accuracy (95% interval) | 84.1% (79.0 to 88.1) | 85.3% (80.3 to 89.1) |
| Same agent on the 50-session haystack (hybrid index) | 93.6% | 93.6% |
| Input tokens per question, all rounds | 82,873 | 41,734 |
| Share of the haystack read | about 5% | about 2% |
| List price per question | 1.4 cents | 8.7 cents |

- **Ten times the history costs about 9 points**, with the model reading 2 to 5% of it.
- **Here the loss is finding, not reading.** Of Sonnet's 36 answerable misses, 20 had an
  evidence session that no search surfaced; on the small haystack that figure was 5 of 15.
  Keyword search alone puts every evidence session in the top 10 for 75.5% of these
  questions (91.3% on the small haystack). Weakest types: multi-session (49 and 44 of 64)
  and preference (8 and 11 of 15).
- **Order of the two runs, stated plainly:** the plan named the hybrid index. The embedding
  index for the 500 large corpora was 87 of 500 built when the machine ran short of memory,
  so the keyword run (table above) was done first, on 2026-11-20. The index was finished
  that night and the planned hybrid run followed (table below).

| 476-session haystack, same agent, same 251 questions | Haiku 5.5 | Sonnet 5.5 |
|---|---|---|
| Keyword index only | 84.1% | 85.3% |
| **Hybrid index (the planned run)** | 81.3% (76.0 to 85.6) | 86.1% (81.2 to 89.8) |
| Hybrid against keyword, questions won / lost | 8 / 15, p = 0.21 | 9 / 7, p = 0.80 |
| Input tokens per question, hybrid | 84,560 | 41,014 |

- **Embeddings did not help the agent at this size.** Neither difference is separable from
  chance, and on Haiku it leans the wrong way. Retrieval on its own did improve with the
  hybrid index (every evidence session in the top 10: 79.8% against 75.5%; preference
  questions with evidence in the top 10: 83.3% against 60%), but an agent that writes its
  own keyword queries had already recovered most of what the embeddings add.
- **The open problem is the same under both indexes:** evidence that no search surfaces
  (21 of Sonnet's 34 answerable misses under hybrid, 20 of 36 under keyword), mostly on
  questions whose answer is spread over several conversations (49 of 64 on Sonnet either
  way). Hybrid search here embeds only the opening part of a long note, and a conversation
  is a long note; that is the next thing to change and re-measure.
- The figure to quote for the large haystack is therefore a range, 81 to 86%, not the better
  of the two runs.

### Run-to-run variation (2026-11-23)

Four arms were run a second time on the same 251 held-out questions, with Sextant and its
indexes frozen at the commit the first runs used (`58bded7`), so the only thing that differs
is the model's own sampling.

| Arm (agent, `full=3`) | Run 1 | Run 2 | Questions that flipped (right to wrong / wrong to right) |
|---|---|---|---|
| Sonnet, 50-session haystack, hybrid | 93.6% | 93.6% | 5 / 5 |
| Haiku, 50-session haystack, hybrid | 93.6% | 94.8% | 3 / 6 |
| Sonnet, 476-session haystack, keyword | 85.3% | 86.5% | 6 / 9 |
| Haiku, 476-session haystack, keyword | 84.1% | 84.9% | 10 / 12 |

- **Totals moved by 0 to 1.2 points between identical runs.** Between 4% and 9% of individual
  answers changed. Read every comparison in this document against that: a difference of a
  point or two, or of a handful of questions, is inside what two runs of the same thing do.
- The comparisons that matter survive it. Against the whole history, the second Sonnet run
  is 6 won and 11 lost (p = 0.33; first run 7 and 12), the second Haiku run 10 won and 6
  lost (p = 0.45; first run 8 and 7).
- The whole-history, evidence-only and no-memory arms were not repeated.

### A fix that did not work: passages for long notes (2026-11-21 to 2026-11-23)

The semantic index embeds the first 8,000 characters of a note, and the embedding model
reads less than that, so the later part of a long conversation is invisible to it. The
obvious repair is to embed a note as overlapping passages. It was built (with the embedding
model's query and document prefixes, and fusion from 30 candidates a side instead of 10) and
tested on retrieval alone, on 116 answerable questions from the development half of the
476-session haystack:

| | Whole-note index | Passage index |
|---|---|---|
| Evidence session ranked first | 63.8% | 70.7% |
| Every evidence session in the top 10 | 82.8% | 82.8% (3 gained, 3 lost) |
| Median search time | 204 ms | 473 ms |

The measure it was built to move did not move, and search took twice as long. It was not
merged and no paid run was made. The code is on the branch `passage-index`. What limits the
hard questions is something else, and it is still open.

### Independent code review (2026-11-23)

Before publication the search changes and this harness were put through an automated
multi-agent code review. It reported ten findings; all ten were confirmed and fixed. The two
that touched published figures:

- **Agent cost was overstated for Haiku.** The report priced a conversation's summed tokens
  as one request, which put long Haiku conversations in the above-100K price tier. Costs are
  now summed per request. Haiku agent figures fell (for example 2.7 to 1.4 cents a question
  on the large haystack); Sonnet has one price tier and did not change. Accuracy figures were
  not affected.
- **The chosen agent variant had no paired tests in the generated report.** The pairing
  matched arm names beginning `agent-` and missed `agent2-` and `agent3-`. The paired figures
  quoted above were computed separately with `subset.py`; the report now produces them too,
  and they agree.

The findings in Sextant itself were about the sentence-query fallback: it was on for every
internal caller, and it scored its best partial match at a fixed 0.35 however little of the
question that note held, so a question memory could not answer came back "low confidence"
instead of "nothing found", and the miss was logged as a hit. The fallback is now requested
only by the search tool and the command line, a partial match must hold at least two of the
question's content words and 40% of them to count, notes sharing only stopwords are dropped,
and a search answered only by partial matches is logged as a miss.

Retrieval was then re-run on the fixed code against the same indexes (free), for all 500
questions on the 50-session keyword and hybrid indexes and the 476-session keyword index:

| | Every evidence session in the top 10, before | After | Confidence label changed |
|---|---|---|---|
| 50 sessions, keyword | 91.3% | 91.3% | 5 of 500 |
| 50 sessions, hybrid | 94.9% | 95.1% (1 gained, 0 lost) | 4 of 500 |
| 476 sessions, keyword | 75.5% | 75.5% | 2 of 500 |

The notes found did not change; a few top-10 lists lost entries that had matched on
stopwords alone. The coverage check adds about 10 ms to a search.

The Sonnet agent arm was then run once more on the fixed code, same 251 held-out questions:
93.6% again (3 questions gained, 3 lost against the first run). That makes three runs of
this arm, two on the code as first measured and one on the code as shipped, all at 93.6%.

No model, embedding or service is involved in retrieval for this table: SQLite FTS5 over
about 476 markdown files per question, median search time 281 ms.

## Run it

```
# 1. data (MIT, about 290 MB): put longmemeval_s_cleaned.json and longmemeval_oracle.json
#    from huggingface.co/datasets/xiaowu0162/longmemeval-cleaned in <work>/data/
set SEXTANT_PROOF_WORK=<a folder with a few GB free>

# 2. retrieval: free, local, needs only Sextant itself
python retrieve.py prepare
python retrieve.py run s fts
python retrieve.py recall s fts

# 3. answers and judging: needs an Anthropic API key and spends money
python proof.py build sextant-fts-v1-k5 haiku
python proof.py plan                       # estimate and route for every built job, no network
python proof.py dispatch ans.sextant-fts-v1-k5.haiku.r1
python proof.py collect
python proof.py judge ans.sextant-fts-v1-k5.haiku.r1
python proof.py dispatch judge-opus.ans.sextant-fts-v1-k5.haiku.r1
python proof.py collect
python report.py
```

Step 2 runs under a Python that has Sextant installed. Step 3 needs only the `anthropic`
package.

## Spending rules

All model calls go through `gate.py`, which enforces three rules:

1. A job estimated above 1 USD at interactive prices is sent through the Message Batches API
   (half price). Asking for a synchronous run of such a job is refused.
2. A hard budget covers settled spend plus everything in flight; a dispatch that would cross
   it is refused.
3. Every dispatch and settlement is written to `ledger.jsonl` with the estimate beside the
   amount computed from the usage the API reported.

`tests/test_longmemeval_harness.py` exercises all three against a stand-in client.

## What is kept away from the model

- The dataset marks evidence turns with `has_answer` and names evidence sessions
  `answer_...`. Both are stripped before anything is written to a corpus or a prompt; notes
  are named by position (`s017.md`).
- The judge sees the question, the reference answer and the response, as in the official
  script.

## Differences from the official protocol

- **Judge.** The judge prompts are the official ones, word for word. The judge model is
  Claude Opus 5.5; the official script calls GPT-4o. Numbers here are comparable between the
  arms of this run and not with numbers published by anyone else.
- **Reader.** Answers come from Claude Haiku 5.5 and Claude Sonnet 5.5 at `effort: medium`,
  with the official non-chain-of-thought reader prompt.
- **Dataset.** The cleaned release of September 2025 (sha256 in `common.py`).
- **File times.** Each note's modification time is set so that it is as old, relative to the
  moment of the run, as the session was relative to the question date. Sextant's ranking uses
  file age, and a note written "today" for a 2023 conversation would misstate it.

## What this does not show

- LongMemEval_S fits inside a current context window: it tests retrieval and reading over a
  history a model could also be handed whole. The history too large to paste (LongMemEval_M)
  was run on the held-out half only, once per index.
- The `sextant-*` arms are one search with the question as typed. The `agent-*` arm is the
  model writing its own queries, through two tools shaped like Sextant's MCP tools but defined
  in this harness; it does not go through the MCP server process.
- The benchmark has no development split, so one was made after the first run (see "Second
  round"). The first-run figures predate any tuning. The second-round change was chosen on
  one half and reported on the other. Both changes made to Sextant because of this work are
  general ones (how a sentence query is matched; returning notes with a search), not rules
  fitted to these questions.
- One run per arm unless a repetition is listed in the results. Model sampling is not
  controllable on these models, so run-to-run variation exists and is reported where measured.

## What the run found in Sextant

The first retrieval pass, on the build that was public on 2026-11-20, returned nothing at all
for 406 of the 470 answerable questions. Sextant's keyword search required every word of the
query to appear in a note, so a question typed as a sentence matched nothing. An agent that
writes short keyword queries does not hit this; a person typing a question does.

The fix (`_any_word_fallback` in `fts.py`, tests in `tests/test_any_word_fallback.py`): when no
note holds every word, rank notes by the words they share; when a sentence-like query has only
a few full matches, list the partial matches after them. Partial matches are flagged
`loose_match` and capped in the low confidence band, so the tool still tells the agent to
hedge. Queries written in search syntax (quotes, AND/OR/NOT) keep their exact meaning.

Both builds are reported side by side (`v0` as shipped, `v1` with the fix):

| Keyword index, 470 answerable questions | v0 (as shipped) | v1 (with the fix) |
|---|---|---|
| searches that returned nothing | 406 | 0 |
| at least one evidence session in the top 1 | 11.9% | 86.6% |
| at least one evidence session in the top 5 | 12.6% | 96.4% |
| at least one evidence session in the top 10 | 12.8% | 98.5% |
| every evidence session in the top 5 | 6.8% | 85.3% |
| every evidence session in the top 10 | 7.0% | 91.3% |

No model, embedding or service is involved in these figures: it is SQLite FTS5 ranking over
markdown files. The weakest type under v1 is `single-session-preference` (83.3% in the top 5),
where the question shares few words with the session that answers it.

## Layout

```
common.py     paths, dataset access, prices
corpus.py     one Sextant corpus per question; worker process that builds and searches it
retrieve.py   runs the workers, computes recall
arms.py       prompts for each arm; official judge prompts
gate.py       spend rules, Batch API dispatch, ledger
proof.py      command line for the paid stages
report.py     accuracy, intervals, cost, paired tests
```

LongMemEval is by Di Wu et al. (ICLR 2025), MIT licensed:
https://github.com/xiaowu0162/LongMemEval
