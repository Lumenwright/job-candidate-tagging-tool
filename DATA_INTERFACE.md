# Data Interface — Candidate Tagging Tool

This is the contract between **your LLM tagging step** and **the UI**.

The UI is display-only. It consumes one JSON document (`data.json`) that contains:
the predefined **filter catalog**, the **candidates** (with their addressable source
blocks), and the **tags** your model produced (each pointing back to a verbatim
source span). You own everything that produces `tags`. The UI owns everything that
renders them.

```
[local LLM + your prompts]  →  data.json  →  [index.html, display only]
       you build this           contract         I built this
```

---

## Top-level shape

```jsonc
{
  "schema_version": "1.0",
  "filters":          [ Filter,   ... ],   // predefined facet catalog
  "static_questions": [ Question, ... ],    // optional, for nicer labels
  "candidates":       [ Candidate, ... ]
}
```

## Filter

A predefined facet a candidate can be tagged with. Define these once; your prompts
reference them by `id`.

```jsonc
{
  "id":          "shipped-to-production",          // stable slug, referenced by tags
  "label":       "Shipped to production",          // shown in the UI
  "description": "Candidate states a system reached real users / production."
}
```

## Question (optional)

Used only to label answer blocks nicely (e.g. "Q1 answer"). Safe to omit.

```jsonc
{ "question_id": "q1", "text": "Describe something you built end to end..." }
```

## Candidate

```jsonc
{
  "candidate_id": "c01",            // the ONLY identity the UI shows (as "Candidate 01")
  "applicant": {                    // OPTIONAL. The UI never renders these fields.
    "name": "...",                  // deliberately hidden — names are obfuscated
    "location": "...",
    "education": [ ... ]
  },
  "blocks": [ Block, ... ],         // every addressable source span
  "tags":   [ Tag,   ... ]          // your LLM output
}
```

> **Obfuscation:** the UI derives the on-screen label from `candidate_id`
> (`c01` → "Candidate 01") and **never** displays `applicant.name`, location, or
> education. You may include them for your own records; they will not leak into the UI.

## Block

The unit of provenance. One addressable span of the application. IDs follow the
handoff convention (`c01-r1`, `c01-s1`, `c01-q1a1`).

```jsonc
{
  "id":          "c01-q1a1",
  "section":     "question_answer",   // "resume" | "submission" | "question_answer"
  "question_id": "q1",                // only for section == "question_answer"
  "text":        "I built a deduplication service that processes 12M records nightly..."
}
```

## Tag — *the part your LLM produces*

A filter that fired for this candidate, plus the evidence that triggered it.

```jsonc
{
  "filter_id": "shipped-to-production",      // must match a Filter.id
  "evidence": [
    {
      "block_id": "c01-q1a1",                // must match a Block.id on THIS candidate
      "quote":    "processes 12M records nightly"   // VERBATIM substring of that block's text
    }
    // a tag may cite multiple evidence spans, possibly across blocks
  ]
}
```

### Provenance rules (these make the tool trustworthy)

1. **`quote` must be an exact substring of the referenced block's `text`.**
   The UI highlights `quote` inside the full block text. If it isn't found verbatim,
   the UI still shows the quote, but the highlight is the integrity check — a quote
   that can't be located is a hallucination signal.
2. **No tag without evidence.** Every tag needs ≥1 `{ block_id, quote }`. This enforces
   the "extract, don't editorialize / provenance is the check" principle from the brief.
3. **`filter_id` must exist in the top-level `filters` catalog.** Unknown filters are ignored.
4. **`block_id` must belong to the same candidate.** Cross-candidate evidence is dropped.

---

## What the UI does with this

- **Filter list** (left): every filter + how many candidates carry it.
- **Pick a filter** → see the matching candidates (obfuscated), each showing **all
  their other tags** too.
- **Click any tag** → see its evidence: the source block, with the verbatim `quote`
  highlighted in context. One click to the original span.

## Wiring your model in

Keep it a batch step. Your script reads each candidate's `blocks`, runs your
fact-only extraction prompts on your local model, and writes tags into this shape.
Output `data.json`, then load it in the UI (the "Load data.json" button) or drop it
next to `index.html`. No live calls, no CORS, no build step.

A minimal tagging loop (pseudocode):

```python
for candidate in data["candidates"]:
    for filt in data["filters"]:
        result = local_llm(prompt=YOUR_PROMPT[filt["id"]], blocks=candidate["blocks"])
        if result.fires:
            candidate["tags"].append({
                "filter_id": filt["id"],
                "evidence":  [{"block_id": b, "quote": q} for b, q in result.spans],
            })
```
