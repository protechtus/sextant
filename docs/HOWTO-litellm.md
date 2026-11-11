# HOWTO: Sextant behind a LiteLLM proxy (budgets, keys, routing)

If you route your AI usage through LiteLLM (or any OpenAI-compatible
gateway), Sextant slots straight in: the Brain's provider layer speaks the
OpenAI API, so the gateway's budgets, virtual keys, model routing, and
spend logging all apply to Sextant's background jobs -- with zero Sextant
configuration beyond two environment variables.

Division of labor: LiteLLM meters and routes the calls; Sextant decides
WHAT is worth calling for (memory hygiene, verification, synthesis) and
remembers the results. They are complementary layers, not competitors.

## Point the Brain at your proxy

```bash
# wherever your Brain service gets its environment:
ATLAS_MODEL_BRAIN_PROVIDER=openai
OPENAI_BASE_URL=http://your-litellm-host:4000/v1
OPENAI_API_KEY=<a LiteLLM virtual key>
```

That is the whole integration. Every nightly job (suggest-facts, dream,
contradiction checks, reverification) now flows through the proxy under
that virtual key -- so you can give Sextant's background work its OWN
budget, cheaper model routing, and a hard monthly cap, separately from
your interactive usage.

Local-first still applies: with no proxy configured, the Brain defaults to
Ollama and nothing meters anything because nothing costs anything.

## Practical patterns

- SEPARATE KEY PER CONCERN: one LiteLLM virtual key for Sextant's nightly
  jobs, one for your interactive tools. The proxy's spend dashboard then
  answers "what does my memory hygiene actually cost?" per key.
- CHEAP-MODEL ROUTING: point the key's routing at an inexpensive model
  tier; Sextant's hygiene jobs are volume work, not frontier work.
- HARD CAPS: LiteLLM `max_budget` on Sextant's key means a runaway job can
  never surprise you on an invoice; Sextant's jobs fail soft and retry the
  next night.
- SEMANTIC SEARCH STAYS LOCAL: the embedding index always uses your local
  Ollama endpoint directly; search never routes through the proxy and
  never costs tokens.

## Reading the spend

Use LiteLLM's own `/spend` endpoints or dashboard, filtered to Sextant's
virtual key. Native per-job token accounting inside Sextant's own doctor/
dashboard is on the roadmap (the provider seam already isolates where
usage data returns); until then the proxy's ledger is the truth, which is
where most teams want it anyway.
