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

Sentinel ships no vendor adapter and does not assume credentials. An adapter
must be installed as an entry point in the `sentinel.pattern_discovery_providers`
group. Its factory receives `ProviderConfig` and returns an object implementing
`generate(request)`. Tests use an injected mock and require no network or API
credentials.

Configure adapters with environment variables:

- `SENTINEL_DISCOVERY_PROVIDER`: installed entry-point name; defaults to
  `disabled`.
- `SENTINEL_DISCOVERY_MODEL`: model identifier, required when enabled.
- `SENTINEL_DISCOVERY_ENDPOINT`: optional provider endpoint.
- `SENTINEL_DISCOVERY_API_KEY_ENV`: name of the environment variable holding
  the provider secret. The secret value is never stored in the database or
  included in the Sentinel evidence request. Adapters may resolve it with
  `resolve_api_key` for provider authentication.
- `SENTINEL_DISCOVERY_TIMEOUT_SECONDS`: 1–600 seconds, defaults to 60; the
  adapter is responsible for enforcing the timeout.

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
It remains available when no AI adapter is configured. AI and heuristic
confidence values are uncalibrated and are not probabilities of correctness or
risk. Actual provider execution remains unverified until an adapter is
installed and configured.
