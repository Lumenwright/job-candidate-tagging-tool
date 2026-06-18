# Tagging pipeline

Offline batch stage that turns raw candidate data into the tagged contract the UI reads.

```
candidate_data.json  ->  tag_candidates.py (local model via Ollama)  ->  data.json
   (raw source)                                                          (read by ../index.html)
```

The UI never calls a model. This script runs separately, as often as you like.

## Prerequisites

- Python 3.8+ (standard library only — nothing to install)
- [Ollama](https://ollama.com) running locally, with a model pulled:

  ```
  ollama pull llama3.1:8b
  ```

## Run

From the repo root:

```
python tagging/tag_candidates.py
```

Writes `./data.json`, then prints a per-candidate summary so you can confirm each
archetype fired the filter it should (the summary uses `_planted_trait`, which is
**never** written into `data.json`). Open `index.html` and load the new `data.json`.

## Swap the model

```
python tagging/tag_candidates.py --model mistral:7b
python tagging/tag_candidates.py --model qwen2.5:7b --host http://localhost:11434
```

Or via env: `OLLAMA_MODEL`, `OLLAMA_HOST`. To use a non-Ollama backend, reimplement
`OllamaModel.extract()` — it just needs to return `{"fires": bool, "evidence": [...]}`.

## Iterate on prompts

The five filters and their starter extraction rules live in `filters.json`.
Override any filter's instruction by adding `prompts/<filter_id>.txt` (see
`prompts/README.md`). Inspect the exact assembled prompt without calling the model:

```
python tagging/tag_candidates.py --print-prompts --only-filter shipped_to_production --limit 1
```

## Useful flags

| Flag | Purpose |
|------|---------|
| `--limit N` | Process only the first N candidates (fast iteration) |
| `--only-filter ID` | Run a single filter |
| `--print-prompts` | Dump assembled prompts and exit (no model calls) |
| `--input` / `--output` | Override file paths |
| `--timeout N` | Per-call timeout (seconds); the first call pays the cold-load cost |
| `--num-ctx N` | Context window (default 2048) |
| `--num-predict N` | Max output tokens (default 256) |
| `--keep-alive D` | How long Ollama keeps the model loaded (default `10m`; `-1` = forever) |

## Performance tuning

Tagging makes `candidates × filters` calls (40 for the demo), so latency adds up.
Biggest levers, in order of impact:

1. **Fit the model entirely on GPU.** Run `ollama ps` mid-run and confirm the
   `PROCESSOR` column says `100% GPU`. Any CPU spill collapses throughput — use a
   smaller model or a more aggressive quant before tuning anything else.
2. **Keep context small.** Prompts here are short; the KV cache is sized to
   `num_ctx` regardless of actual tokens, so the default `--num-ctx 2048` is
   deliberately low. Don't raise it.
3. **Cap output.** `--num-predict 256` is ample for the tiny structured result.
4. **Stay resident.** `--keep-alive 10m` (or `-1`) avoids reloading the model
   between calls — the cold-load cost you otherwise pay every call.
5. **Right-size the model.** Verbatim fact-extraction with a constrained JSON
   schema is not a hard reasoning task; an 8–9B model is usually as accurate as a
   much larger one here and several times faster. Confirm with the console
   self-check that each archetype still fires.

Env-level (set before `ollama serve`): `OLLAMA_FLASH_ATTENTION=1` and
`OLLAMA_KV_CACHE_TYPE=q8_0` speed things up and free VRAM so more layers fit on GPU.

## Integrity guarantee

Every quote the model cites is verified to be a verbatim substring of the block it
references (whitespace-tolerant). Evidence that fails is discarded; a tag with no
surviving evidence is dropped. Quotes that don't verify are the hallucination
signal — they never reach the reviewer.
