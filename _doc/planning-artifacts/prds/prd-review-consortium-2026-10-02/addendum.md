# Addendum: Review Consortium PRD

Detail that informs the PRD but belongs downstream (architecture, methods section) or outside the PRD body.

## A1. Landscape research digest (2026-10-02)

Web-research subagent digest. Treat items marked *(snippet)* as unverified.

### Comparable tools: none combine video + audio, blinding and protocol freeze
- **EDSL (Expected Parrot)**: MIT-licensed Python library for persona-agent surveys across many LLMs. Handles images and PDFs; no confirmed video or audio; no blinding or freeze. https://github.com/expectedparrot/edsl
- **Althing**: MIT-licensed CLI for synthetic focus groups. Images, PDFs and URLs only; no audio or video. Has seeds, Likert and pick-one answers, and stops when answers stabilize. https://github.com/DataViking-Tech/Althing
- **SynthBench**: benchmarks synthetic respondents; does not run studies. https://github.com/topics/persona-simulation
- **Commercial** (Simile, Aaru, Synthetic Users, Ditto): closed, and none mention video stimuli. *(snippet)*
- **HRI prior work**:
  - LLM Godspeed/RoSAS ratings from text transcripts: https://arxiv.org/html/2606.23339v1
  - Gemini reading HRI video, checked against expert annotations: https://arxiv.org/html/2512.07177
- **Gap** (search not exhaustive): no open-source VLM panel over video + audio with a rater-flow report found.

### Reviewer critiques and the safeguards they imply
- **Weak individual-level fidelity and stereotyping.** Identity effects run up to about 40x too strong; reordering answer options flips 9–23% of answers. Safeguards: report individual and aggregate fidelity, compare against non-LLM baselines, report invalid-answer rates. https://arxiv.org/html/2607.26348v1
- **Low variance and prompt sensitivity.** Safeguards: compare against human distributions and test prompt variants. https://arxiv.org/pdf/2608.14606
- **Persona caricature** (Nature Machine Intelligence 2025): https://www.nature.com/articles/s42256-025-00986-z
- **Constructs the model can't perceive.** In the HRI study, strangeness and discomfort came out inverted or stuck at the floor — directly relevant to the aliveness and presence scales. https://arxiv.org/html/2606.23339v1
- **Pairwise position bias.** Systematic; fix by swapping the order and running both. https://aclanthology.org/2025.ijcnlp-long.18/
- **Model drift and deprecation.** Record version, access date and decoding settings; include an open-weights baseline. *(snippet)*
- **p-hacking.** Preregister, then run on a model released after registration. https://arxiv.org/abs/2606.27687
- **Unsourced inference.** Godspeed norms in training data may anchor ratings; manipulated catch clips can help detect it.

### Vision-language models that take video with audio
| Provider | Video + audio input | Notes |
|---|---|---|
| Gemini | Yes, natively | 1 fps by default (adjustable); about 100 tokens per second; seed setting exists but runs are reported non-reproducible. https://ai.google.dev/gemini-api/docs/video-understanding |
| Qwen3-Omni | Yes, open weights (Apache-2.0) | `use_audio_in_video` flag; can be self-hosted on vLLM, so the version can be pinned. https://github.com/QwenLM/Qwen3-Omni |
| OpenAI | No native video | Workaround is extracting frames, which loses the audio (issue closed as not planned, Mar 2026). https://github.com/openai/openai-node/issues/1778 |
| Claude | No video or audio | *(snippet; verify against Anthropic docs)* |

### Reporting standards
- **GUIDE-LLM** (Nature Human Behaviour 2026): 12 core items, including model, version and access date; temperature, seed and number of runs; exact prompts; human validation; and code sharing. https://sfeuerriegel.github.io/llm-checklist/checklist/
- **Lin 2026** (AMPPS): "LLMs as Psychological Simulators" checklist. *(snippet)*
- **No CONSORT-style rater-flow standard exists** for synthetic raters, so the tool's rater-flow report would be new.

## A2. Model choice rationale

- Only Gemini and Qwen-Omni take video with audio natively (A1). OpenAI and Claude don't.
- Persona supplies rater diversity. A second Model guards against findings that come from one Model's perceptual quirks; it doesn't add rater types.
- **Corrected 2026-10-02 (architecture review):** hosted `qwen3-omni-flash` is a closed, further-trained variant, not the open weights. It accepts at most 150 s of media and takes local files only as base64 under 10 MB. v1 therefore uses hosted `qwen3.8-omni-flash` (up to 2 h of media). The adapter is OpenAI-compatible, so self-hosting open-weight Qwen3-Omni on vLLM is a configuration change (spine AD-14).
- v1 calls Qwen3-Omni through Alibaba Cloud Model Studio (DashScope API), so no GPU is needed. A hosted serving stack may differ from self-hosted vLLM (quantization, `use_audio_in_video` defaults); their equivalence is unverified.
- Temperature 0 was rejected because identical Repeats make test–retest trivially perfect. The default is a pinned temperature above 0 with recorded seeds; Prompt variants add a second robustness check.

## A3. Architecture handoff (carried from the source spec)

Details the PRD deliberately leaves to architecture:

- **Board state:** SQLite (`board.db`) in the Study folder.
- **Package layout (source spec, renamed):** `consortium/`, containing `cli.py`, `board/` (clip store, blinding key, tests, assignments), `personas/`, `screening/`, `runner/`, `models/` (one adapter per provider), `instruments/`, `quality/` and `web/`. Repo docs: `docs/INTERFACE.md` and `docs/protocol-template.md`.
- **Study folder layout:** `study.yaml`, `protocol.md` and `protocol.lock`, `tests/*.yaml`, `board.db`, `blinding_key.csv`, `archive/`, `exports/`, and a Panel directory (Persona cards and screening results).
- **Source-spec defaults:** `practice_clips: 2`, `repeats: 3`, `max_retries: 2`, `pairing: all_pairs`. The spec pinned `temperature: 0`; the PRD overrides this to a value above 0 (A2).
- **Cost estimate method:**
  - Sum each planned request's provider token count (Gemini `countTokens`, the DashScope equivalent, or a documented formula).
  - Multiply by a price table versioned in the repo, and add a retry allowance.
  - The cost ceiling reserves the worst-case cost of each in-flight request.
- **Sampling-frame assignment:** a seeded marginal-balance or Latin-square assignment of the demographic attributes over the 64 Big Five × NARS cells. Choose a replacement policy for excluded Agents: none, resample within cell, or oversample at generation.
- **Persona-fidelity instrument:** short forms such as the BFI-10 plus a NARS short form, scored as directional matches against the card's high/low labels.
- **Clip canonicalization:** re-mux or re-encode with ffmpeg, strip container metadata, and upload to providers under a random name.
- **Placeholder ownership:** Model IDs and fps are set at Model onboarding. The cost ceiling is set by the pilot.

## A4. Open follow-ups from review

- The GUIDE-LLM checklist has 12 core items; A1 lists 5. Pull the full list and map each item to "tool fills" or "author supplies" (PRD Q2).
- Full reviewer output: `review-rubric.md`, `review-adversarial.md`, `reconcile-repo-spec.md`. The remaining medium and low items were folded into PRD v2 or listed here.
