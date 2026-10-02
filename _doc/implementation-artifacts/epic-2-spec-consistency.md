# Epic 2 spec consistency log

## 2026-10-02 — finalisation pass (specs 2.1–2.4)

- 2.1: applied decision 1 (attrition sidecar `exports/<test>-attrition.csv`; `status --json` `by_persona_attribute` only); removed Open Questions; added Design Notes (adapter-backoff scope, no migration, sidecar name clash note); status `ready-for-dev`.
- 2.2: applied decisions 2 and 5 (`settings.thinking_level`, gemini-only, `ThinkingConfig(thinking_level=ThinkingLevel.*)` verified in google-genai 2.27.0, template example `low`; one story, split proposal removed); removed Open Questions; status `ready-for-dev`.
- 2.3: applied decisions 3 and 4 (`reasoning_effort` optional, sent/archived only when set; `documented: null` off Model Studio); removed Open Questions; status `ready-for-dev`.
- 2.4: new spec (fraction/replicates, principal fractions, aliasing, `panel_in_use`); no Open Questions; status `ready-for-dev`.
- Conflict: 2.1 "no backoff inside an adapter" vs 2.2 `prepare` upload retries/polling — clarified (non-frozen notes in 2.1 and 2.2) that the rule covers `submit`/`collect` outcomes only.
- Consistency: settings archive shape aligned — both adapters omit settings that are not sent (2.2 was ambiguous for unset `fps`/`media_resolution`).
- Consistency: 2.3 notes its reliance on 2.1 Categories and SDK `max_retries=0`; 2.2 already disables SDK retries (`HttpRetryOptions(attempts=1)`).
- Checked, no change needed: `raters_for(cfg, model_ids, study)` (2.2, 4 call sites) used consistently; `tests/recorded.py`, `tests/fixtures/{gemini,qwen}/`, marker `live` consistent; no spec adds a migration (`MIGRATIONS` = 5).
- Code map: appended the decision-driven and 2.4 names.
- Context: added Story 2.4 to the Stories list in epic-2-context.md.
