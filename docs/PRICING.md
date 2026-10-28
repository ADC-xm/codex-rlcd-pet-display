# Token and API-equivalent cost accounting

Verified 2026-09-07 against:
- https://developers.openai.com/api/docs/pricing
- https://developers.openai.com/api/docs/models/gpt-6-astra
- https://developers.openai.com/api/docs/guides/prompt-caching

The dashboard estimates API token cost, not ChatGPT/Codex subscription charges
or remaining quota. Quota comes from active Codex account queries; local
snapshots are used only while the first active query is pending.

Each local call uses the preceding turn_context model and service_tier.
Missing/auto tiers are estimated at Standard rates; the actual served tier is
not exposed in the inspected Codex logs. Mixed days use each call's model.
Unknown models/tiers keep token counts but produce an unknown price, not zero.
The price table is verified on the date above and is not downloaded at runtime.

Standard USD per million tokens (ordinary input / cache read / cache write / output):
- GPT-6 Astra: 10 / 1 / 12.5 / 50
- GPT-5.6 Sol: 4 / 0.4 / 5 / 20 (current promotional rate)
- GPT-5.6 Terra: 2 / 0.2 / 2.5 / 12
- GPT-5.6 Luna: 0.2 / 0.02 / 0.25 / 1.2

For these models, requests above 272,000 input tokens apply 2x input/cache
rates and 1.5x output rates to the whole request. Fast/priority applies 2x;
Batch/Flex applies 0.5x. Threshold is actual request input, not accumulated
session tokens or the configured context window capacity.

Input already contains cached reads and cache writes. Subtract both to price
ordinary input. Output already includes reasoning tokens. Total equals input
plus output; hourly buckets use the local event time. Identical cumulative
usage notifications are skipped. Per-call usage is preferred over cumulative
deltas across model changes. No user text is re-tokenized.

Scope remains local, non-forked/non-subagent session logs, including archived
sessions modified today. This is not guaranteed to include all account/cloud
usage. Cache writes are included in the display's IN value and priced separately.
Tool fees, regional uplifts and contractual discounts are outside this estimate.

The bridge sends every 10 seconds. Account quota is queried separately through
the local Codex app-server about every 60 seconds plus polling delay, even when
no new session-log event is written. Queries time out after 15 seconds, with
bounded retry backoff. This does not generate model turns or change login settings.
