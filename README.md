# Candidate Tagging Tool

A reviewer-side aid for hiring **AI Builders**. It ingests already-submitted
application materials, uses a small **local** model to tag each candidate with
predefined **factual filters**, and presents the result as a navigation aid —
**no scoring, no ranking, no judgment**. The reviewer facets by filter, sees which
candidates match, and can jump to the verbatim source span behind every tag.

Built to the constraints in the project brief: presents data for human use (never
decides), reduces manual scanning, weighs each candidate on their own merits, and
keeps all AI processing local for privacy.

## How it works

Two decoupled stages joined by a JSON contract. The UI never calls a model.

```
candidate_data.json                 data.json                  index.html
  (raw application)  ──tagging──▶  (tagged + provenance)  ──▶  (reviewer view)
                     local model                               display only
                     via Ollama
```

- **Stage 1 — tagging (offline batch).** `tagging/tag_candidates.py` runs each
  filter against every candidate via a local Ollama model, verifies provenance,
  and writes `data.json`. Re-run anytime; swap models with one flag.
- **Stage 2 — review (browser).** `index.html` loads `data.json` and renders the
  faceting + provenance UI. Self-contained, no build step, no server.

## Quick start

```bash
# Stage 1: generate tags (requires Ollama running + a model pulled)
ollama pull qwen3.5:9b
python tagging/tag_candidates.py            # writes ./data.json

# Stage 2: review
# open index.html in a browser, then "Load data.json"
```

`index.html` ships with sample data baked in, so it also opens and works on a
double-click before you've run the pipeline.

## Files

| Path | What it is |
|------|-----------|
| [`index.html`](index.html) | Self-contained reviewer UI — faceting + provenance, obfuscated identities, no scoring. |
| [`DATA_INTERFACE.md`](DATA_INTERFACE.md) | The `data.json` contract joining the tagging stage to the UI. |
| [`data.json`](data.json) | Sample tagged dataset (8 archetypes) — the contract by example. |
| [`candidate_data.json`](candidate_data.json) | Raw synthetic source dataset (pre-tagging input). |
| [`tagging/tag_candidates.py`](tagging/tag_candidates.py) | The tagging pipeline (Ollama, zero dependencies). |
| [`tagging/filters.json`](tagging/filters.json) | The 5 predefined filters + their extraction rules. |
| [`tagging/prompts/`](tagging/prompts/) | Optional per-filter prompt overrides (drop-in). |
| [`tagging/README.md`](tagging/README.md) | Pipeline usage, model swapping, flags. |

## The filters

Five hardcoded factual filters, each tied to the job description and aimed at a
planted archetype in the test data:

| Filter | Fires when… |
|--------|-------------|
| `shipped_to_production` | A system was deployed to a live/operational environment (not a sandbox or prototype). |
| `explicit_governance_or_risk_mitigation` | Risk, governance, privacy, security, or guardrails are treated as design constraints. |
| `multi_step_workflow_design` | Architecture goes beyond a single call — orchestration, agent routing, human-AI handoffs. |
| `non_traditional_or_low_code_delivery` | Built via iPaaS, low-code, API stitching, or config-driven orchestration. |
| `concrete_technical_tradeoffs_stated` | An explicit engineering/design compromise is named, with what was chosen. |

## Design guarantees

- **Provenance is verified, not trusted.** Every quote a model cites is checked to
  be a verbatim substring of the block it references; evidence that fails is
  discarded, and a tag with no surviving evidence is dropped. Quotes that don't
  verify are the hallucination signal and never reach the reviewer.
- **Identities are obfuscated.** The UI labels candidates `Candidate 01` and never
  renders names, location, or education.
- **No ground-truth leakage.** The raw data's `_planted_trait` archetype label is
  never written into `data.json`; it's used only for an optional console self-check.
- **Faceting, not scoring.** The tool groups by presence/absence of attributes and
  never collapses candidates into a single ranked list.

## Local model

Tagging defaults to Ollama at `http://localhost:11434`. Swap the model with
`--model` (or `OLLAMA_MODEL`) and point elsewhere with `--host` (or `OLLAMA_HOST`).
To use a non-Ollama backend, reimplement `OllamaModel.extract()`. See
[`tagging/README.md`](tagging/README.md) for performance tuning and all flags.
