# Claude Accounts Web — modified integration

基于 QB Gate，版权所有 (C) 2026 smithtaylor7748-ops。
源码：https://github.com/smithtaylor7748-ops/qb-gate

Based on QB Gate, Copyright (C) 2026 smithtaylor7748-ops.
Source code: https://github.com/smithtaylor7748-ops/qb-gate

This is a modified integration named Claude Accounts Web, not an upstream QB Gate
release and not published, endorsed or supported by its copyright holder.
The combined application is provided under AGPL-3.0-only; preserve the upstream
additional terms in LICENSE-ADDITIONAL-TERMS.md for upstream-derived material.

On 2026-10-18, codex_stats.py was ported from QB Gate commit
8669c1451d41015b8e6da190a2ad49f3d5d3bc0d:

- crates/qb-accounts/src/codex/usage.rs — cumulative per-session differences,
  archive deduplication, cache separation, reasoning and long-context treatment.
- crates/qb-accounts/src/codex/ratelimit.rs — latest nonempty local quota snapshot,
  actual window durations and reset timestamps.
- crates/qb-station/src/station/pricing.rs — gpt-5.6-sol standard pricing snapshot,
  verified upstream on 2026-08-28 ($4 input, $20 output, $0.40 cached input / MTok).

Modifications: Python standard-library implementation, incremental numeric-only
SQLite cache, integration with Claude reports and provider filters. Missing price
conditions are left unpriced, rather than shown as a standard-rate lower bound.
On 2026-10-18 pricing was updated from https://developers.openai.com/api/docs/pricing
to include GPT-6.1 Sol, GPT-6 Astra, GPT-6 Luna, GPT-6 Sol and GPT-5.6 Sol Standard
API rates, long-context tiers and distinct cache-write accounting. The numeric
cache is rebuilt automatically for this additional usage field.
Quota selection uses the latest valid measurement timestamp across indexed files.
The default Codex home is local history, not a managed account slot, so its
historical usage and quota are deliberately not attributed to the current login.
The newer token_usage_record events are ignored to avoid double-counting the
same requests alongside the token_count stream used by upstream.

The complete source of this running integration and license texts are available
to authenticated panel users through /source.zip (About → Download source).
No credentials, configuration directories, local databases or conversations are
included in the archive.

On 2026-10-19 account_attribution.py was ported from the same commit's
crates/qb-accounts/src/accounts/tokens.rs OwnerIndex, scratch workspace and legacy
owner-marker logic. Modified to reject conflicting session ownership rather than
last-writer-wins. Additional runtime hooks record only newly appended usage keys,
keep replayed history separate, and reject overlapping live runs. Historical
session attribution is explicitly distinguished from runtime-confirmed records.
Official hook reference: https://code.claude.com/docs/en/hooks .

Codex account management in codex_accounts.py adapts the isolated profile design and local JWT identity display from crates/qb-accounts/src/codex/mod.rs of the same QB Gate revision. Official CLI device authorization replaces its desktop launch integration.
