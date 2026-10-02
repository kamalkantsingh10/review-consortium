# Epic 3 spec consistency log

## 2026-10-02 — finalisation pass (specs 3.1–3.4)

- 3.1: applied decision 1 (NARS structure-only built-in `fidelity_nars`, `draft: true`; `screening.nars_instrument` selects Kamal's user Instrument; validated keys contract with subscales; template `templates/nars_instrument.yaml`; 3 INTERFACE steps; BFI-10 bundled, TIPI fallback) and decision 2 (zero NARS bands: frame did not allow it (`min_length=1`), so the story adds it; 5-check ratio; full-grid Panels byte-identical). Open Questions removed; status `ready-for-dev`.
- 3.3: applied decision 3 (pilot gating); Open Questions removed; status `ready-for-dev`.
- 3.4: applied decision 4 (`panel_frame_mismatch`); Open Questions removed; status `ready-for-dev`.
- 3.2: consistency edits only; status `ready-for-dev`.
- Coverage: 3.3 counted "any perception result" while 3.2 counts only pair checks. One rule now: covered = current perception result with `pair_checks > 0` (new column in migration 6), one function `core.perception.coverage(needed, covered)`; 3.2 reports, 3.3 refuses. Frozen coverage text in 3.2 and 3.3 edited at the caller's request.
- Fidelity key: 3.3 said "each fidelity Instrument"; 3.1 stores one `instrument = 'fidelity'` row per Agent. 3.3 now keys `(model, agent)` and compares against the hash of `load_fidelity_instruments` (frozen text edited for consistency).
- Migrations: 6 = all screening tables (3.1, incl. `screening_test`, `detail`, `pair_checks`, source columns); 7 = `screening_exclusions`, `screening_stamps` (3.3); 3.2 and 3.4 add none. 3.4's "Never: a migration" clarified to "a new migration".
- Layering: `engine/run.py` imports `config.models` types only (precedent: `engine/dispatch.py`), never `config.load`; loading arrives via `ConfigReader`; new forbidden contract. 3.2's loader change and 3.3's gate wiring restated against `engine/run.py` and the `Prepared` hooks (new `resume_check(conn)`); the gate stays in `stages/open.py`.
- `config/panel_files.py` (3.4) kept: layer-legal (config imports stdlib + core only), helpers listed, `_meta`/`canonical_index`/`_swap_into_place` stay in `stages/personas.py`.
- `board/screening.any_runs` moved from 3.3 to 3.1 (3.1 owns the helper set); `current_results` tie rule (highest run wins) made explicit.
- `screening_not_open` reused by `screen models --resume`; `screening_test_not_openable` ordered before the `openable` check so `open s<n>` gets it; `coverage_gap` is a stderr warning plus a stdout summary line; CLI flags unified (`--ceiling USD`, `--study PATH`).
- `panel_in_use` widening stated identically in 3.4 and the code map (`any_trials` or `any_results`).
- Code map: appended "Additions agreed after spec writing".
