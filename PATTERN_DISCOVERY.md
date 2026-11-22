# Pattern discovery workflow

Pattern discovery reads only `current_selected_matching_input` and the active,
approved Pattern Library. It does not use legacy normalized records,
alternatives, rejected evidence, or unapproved discovery proposals as model
context.

## Discovery

- `python pattern_discovery.py mine` runs the local heuristic fallback.
- `python pattern_discovery.py ai` runs the provider-neutral AI adapter.
- `python prepare_pattern_discovery.py` writes a selected-evidence-only bundle
  and request prompt for offline use. Import its response with
  `python pattern_discovery.py import-ai suggestions.json`.

Sentinel includes a provider-neutral adapter interface, an OpenAI Responses API
adapter, and a Google Gemini GenerateContent adapter. Other providers can be
installed as entry points in the `sentinel.pattern_discovery_providers` group.
Tests use injected mocks and require no network or API credentials.

Configure adapters with environment variables:

- `SENTINEL_DISCOVERY_PROVIDER`: `openai`, `gemini`, another installed
  entry-point name, or `disabled` (the default).
- `SENTINEL_DISCOVERY_MODEL`: model identifier, required when enabled.
- `OPENAI_API_KEY`: required by the OpenAI adapter. Keep the value in the
  process environment or a secret manager; never place it in source, request
  data, logs, or run metadata.
- `GEMINI_API_KEY`: required by the Gemini adapter. The root `.env` file is
  loaded for provider settings and credentials when running Sentinel; process
  environment variables override `.env` values. Keep `.env` local and never
  commit it.
- `SENTINEL_DISCOVERY_ENDPOINT`: optional endpoint for an adapter. The built-in
  OpenAI adapter only accepts `https://api.openai.com/v1/responses`.
- `SENTINEL_DISCOVERY_API_KEY_ENV`: secret-variable name for other adapters.
  OpenAI and Gemini fix this to their respective key variables.
- `SENTINEL_DISCOVERY_TIMEOUT_SECONDS`: 1–600 seconds, defaults to 60; the
  adapter is responsible for enforcing the timeout.

For OpenAI, configure `SENTINEL_DISCOVERY_PROVIDER=openai`, set
`SENTINEL_DISCOVERY_MODEL` to a model available to the account, and provide
`OPENAI_API_KEY` outside the repository. The adapter uses Responses API JSON
Schema output and disables response storage. No default model is assumed.

For Gemini, set `GEMINI_API_KEY`, `SENTINEL_DISCOVERY_PROVIDER=gemini`, and
`SENTINEL_DISCOVERY_MODEL` in the project root `.env`. Before generating, the
adapter queries the Gemini Models API and requires the configured model to be
listed with `generateContent` support. This catalog check does not guarantee
project-level access; the generation request can still be rejected. The adapter
sends selected evidence and the active library in one structured JSON request;
source citations are independently validated against Sentinel's database
before any pending-review record is written.

Every AI proposal must include a candidate name and supported discovery
category, one or more evidence candidate/artifact IDs with exact source
excerpts, reasoning, confidence and its basis, and novelty type (`new_pattern`,
`variant`, or `possible_duplicate`). Variants and possible duplicates must cite
an active approved target pattern. The importer validates all citations and
quotes against actual current selected raw evidence before persistence.

Suggestions are stored in discovery candidate tables, separate from approved
patterns. There is no automatic approval path. Only explicit human review may
publish a new pattern or add a reviewed variant to its target pattern. Exact
duplicates cannot be approved; near-duplicate overrides require an explicit
reviewer decision.

The local miner finds repeated phrases and capitalized names as heuristic leads.
It remains available when no AI provider is configured. AI and heuristic
confidence values are uncalibrated and are not probabilities of correctness or
risk. Mocked tests do not verify live provider access; a real inference run
requires the environment configuration above.
