#!/usr/bin/env python3
"""
Candidate tagging pipeline (offline batch stage).

Reads raw candidate data (candidate_data.json), runs each predefined factual
filter against every candidate using a local model served by Ollama, verifies
provenance, and writes the tagged contract (data.json) consumed by index.html.

    candidate_data.json  ->  [local model via Ollama]  ->  data.json
        (raw source)            this script                (tagged contract)

Swapping the model
------------------
Model access is isolated in OllamaModel.extract(). Swap models with --model
(or OLLAMA_MODEL), point at another host with --host (or OLLAMA_HOST). To use a
non-Ollama backend, reimplement OllamaModel.extract() to return the same dict.

Prompts
-------
Each filter's extraction instruction comes from tagging/filters.json
(`extraction_rule`). The separate prompt-generation step can override any filter
by writing tagging/prompts/<filter_id>.txt — that text replaces the instruction.
The system prompt, block formatting, and output schema are enforced here so a
prompt cannot break the provenance contract.

Provenance gate
---------------
After every model call, each cited quote is kept only if it is a verbatim
substring of the cited block (whitespace-tolerant). A tag whose evidence does
not survive verification is dropped. This is the hallucination check.

Usage
-----
    ollama pull llama3.1:8b            # once
    python tagging/tag_candidates.py   # writes ./data.json

    python tagging/tag_candidates.py --model mistral:7b
    python tagging/tag_candidates.py --limit 2 --only-filter shipped_to_production
    python tagging/tag_candidates.py --print-prompts   # inspect prompts, no model calls
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

SYSTEM_PROMPT = """You are a fact-extraction engine for a hiring reviewer tool. \
You do not judge, score, rank, or rate candidates. You only determine whether a \
specific factual filter is supported by VERBATIM text in the candidate's \
application blocks.

Output ONLY a single JSON object with EXACTLY these two keys and no others:
{"fires": <true or false>, "evidence": [{"block_id": "<id>", "quote": "<text copied verbatim from that block>"}]}

Rules:
- Set "fires" to true only if the blocks contain explicit text that satisfies the \
filter. When in doubt, use false.
- For every reason it fires, add one evidence item: the exact "block_id" and a \
"quote" copied CHARACTER-FOR-CHARACTER from that block's text.
- Never paraphrase, summarize, infer, or add adjectives. If you cannot quote it \
verbatim from a block, it does not count.
- If "fires" is false, "evidence" must be an empty list [].
- Output only the JSON object. No prose, no markdown fences, no extra keys."""

# Ollama structured-output JSON schema (passed as the `format` field).
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "fires": {"type": "boolean"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "block_id": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["block_id", "quote"],
            },
        },
    },
    "required": ["fires", "evidence"],
}


# --------------------------------------------------------------------------- #
# Data shaping
# --------------------------------------------------------------------------- #
def flatten_blocks(candidate: dict) -> list[dict]:
    """Flatten the raw source schema into the tagged contract's `blocks` array."""
    blocks: list[dict] = []
    for b in candidate.get("resume", {}).get("blocks", []):
        blocks.append({"id": b["id"], "section": "resume", "text": b["text"]})
    for b in candidate.get("submission_summary", {}).get("blocks", []):
        blocks.append({"id": b["id"], "section": "submission", "text": b["text"]})
    for qa in candidate.get("question_answers", []):
        qid = qa.get("question_id")
        for b in qa.get("answer_blocks", []):
            blocks.append(
                {"id": b["id"], "section": "question_answer", "question_id": qid, "text": b["text"]}
            )
    return blocks


def find_verbatim(text: str, quote: str) -> str | None:
    """Return the exact span from `text` matching `quote`, or None.

    Exact substring first; falls back to a whitespace-tolerant match so trivial
    whitespace differences from the model don't cause a false hallucination flag.
    """
    if not quote:
        return None
    if quote in text:
        return quote
    tokens = quote.split()
    if not tokens:
        return None
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    m = re.search(pattern, text)
    return m.group(0) if m else None


def verify_evidence(blocks_by_id: dict, evidence: list) -> list[dict]:
    """Keep only evidence whose quote is verbatim in the cited block."""
    kept: list[dict] = []
    for ev in evidence or []:
        block_id = ev.get("block_id")
        text = blocks_by_id.get(block_id)
        if text is None:
            continue
        exact = find_verbatim(text, ev.get("quote", ""))
        if exact is None:
            continue
        kept.append({"block_id": block_id, "quote": exact})
    return kept


# --------------------------------------------------------------------------- #
# Prompting
# --------------------------------------------------------------------------- #
def load_instruction(flt: dict, prompts_dir: Path) -> str:
    """Per-filter instruction: prompts/<id>.txt override, else filters.json rule."""
    override = prompts_dir / f"{flt['id']}.txt"
    if override.exists():
        return override.read_text(encoding="utf-8").strip()
    return flt.get("extraction_rule", "")


def format_blocks(blocks: list[dict]) -> str:
    lines = []
    for b in blocks:
        section = b["section"]
        if section == "question_answer" and b.get("question_id"):
            section = f"{b['question_id']} answer"
        lines.append(f"[{b['id']} | {section}] {b['text']}")
    return "\n".join(lines)


def build_user_prompt(flt: dict, blocks: list[dict], prompts_dir: Path) -> str:
    return (
        f"FILTER: {flt['label']}\n"
        f"WHAT COUNTS: {load_instruction(flt, prompts_dir)}\n\n"
        f"CANDIDATE BLOCKS:\n{format_blocks(blocks)}\n\n"
        "Decide whether this filter fires for this candidate. Cite verbatim "
        "quotes copied character-for-character from the blocks above."
    )


# --------------------------------------------------------------------------- #
# Model backend (swap here for a non-Ollama model)
# --------------------------------------------------------------------------- #
def extract_json(content: str):
    """Best-effort parse of a JSON object from model output.

    Not every model honors the structured-output `format` schema, and some wrap
    the JSON in markdown fences or prose. Try the whole string, then a fenced
    block, then the first balanced {...} object.
    """
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except (json.JSONDecodeError, TypeError):
                    return None
    return None


class OllamaModel:
    def __init__(self, model, host, timeout, num_ctx, num_predict, keep_alive, think=False):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.think = think
        # Performance knobs. Prompts here are short, so a small num_ctx keeps the
        # KV cache (and memory pressure) low; num_predict caps the tiny structured
        # output; keep_alive holds the model resident across the whole batch so the
        # cold-load cost is paid once, not per call.
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.keep_alive = keep_alive

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            # Some models reject `think: false`. Retry once without the field.
            if "think" in body and "think" in err_body.lower():
                return self._post({k: v for k, v in body.items() if k != "think"})
            raise RuntimeError(
                f"Ollama returned HTTP {e.code} at {self.host}: {err_body[:200] or e.reason}"
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # Socket TimeoutError (cold model load) and connection errors land here
            # so the user gets guidance, not a stack trace.
            reason = getattr(e, "reason", e)
            raise RuntimeError(
                f"Could not get a response from Ollama at {self.host} ({reason}). "
                "Check that `ollama serve` is running and the model is pulled "
                f"(`ollama pull {self.model}`). If the model is large, raise --timeout "
                "(the first call pays a cold-load cost)."
            ) from e

    def extract(self, system: str, user: str) -> dict:
        """Call the model and return the parsed {fires, evidence} dict."""
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": EXTRACTION_SCHEMA,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": 0,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
        }
        # Thinking models otherwise spend the whole token budget reasoning and
        # return empty content. Disable unless the user explicitly opts in.
        if not self.think:
            body["think"] = False

        payload = self._post(body)
        message = payload.get("message") or {}
        content = message.get("content") or ""
        result = extract_json(content)
        if not isinstance(result, dict):
            snippet = (content.strip() or "<empty content>")[:160].replace("\n", " ")
            if not content.strip() and message.get("thinking"):
                snippet = "<empty content; model emitted only reasoning — try without --think>"
            sys.stderr.write(f"  ! could not parse JSON from model output ({snippet!r}); no-fire\n")
            return {"fires": False, "evidence": []}
        return {
            "fires": bool(result.get("fires")),
            "evidence": result.get("evidence") or [],
        }


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def tag_candidate(candidate, filters, model, prompts_dir, only_filter):
    blocks = flatten_blocks(candidate)
    blocks_by_id = {b["id"]: b["text"] for b in blocks}
    tags = []
    for flt in filters:
        if only_filter and flt["id"] != only_filter:
            continue
        result = model.extract(SYSTEM_PROMPT, build_user_prompt(flt, blocks, prompts_dir))
        evidence = verify_evidence(blocks_by_id, result.get("evidence"))
        if result.get("fires") and evidence:
            tags.append({"filter_id": flt["id"], "evidence": evidence})
        elif result.get("fires") and not evidence:
            sys.stderr.write(
                f"  ~ {candidate['candidate_id']}/{flt['id']}: model fired but no "
                "verbatim evidence survived -- dropped (integrity gate)\n"
            )
    out = {
        "candidate_id": candidate["candidate_id"],
        "applicant": candidate.get("applicant", {}),
        "blocks": blocks,
        "tags": tags,
    }
    return out


def print_prompts(candidates, filters, prompts_dir, only_filter):
    for c in candidates:
        blocks = flatten_blocks(c)
        for flt in filters:
            if only_filter and flt["id"] != only_filter:
                continue
            print("=" * 78)
            print(f"# candidate {c['candidate_id']} · filter {flt['id']}")
            print("-" * 78)
            print("[SYSTEM]\n" + SYSTEM_PROMPT)
            print("\n[USER]\n" + build_user_prompt(flt, blocks, prompts_dir))
            print()


def report(out_candidates, raw_candidates):
    """Console self-check. Uses _planted_trait (never written to data.json)."""
    trait_by_id = {c["candidate_id"]: c.get("_planted_trait", "?") for c in raw_candidates}
    sys.stderr.write("\nTagging summary (planted trait shown for your eyeball test only):\n")
    for oc in out_candidates:
        cid = oc["candidate_id"]
        fired = ", ".join(t["filter_id"] for t in oc["tags"]) or "(none)"
        sys.stderr.write(f"  {cid}  [{trait_by_id.get(cid, '?')}]\n        fired: {fired}\n")


def main() -> int:
    p = argparse.ArgumentParser(description="Tag candidates with factual filters via a local Ollama model.")
    p.add_argument("--input", type=Path, default=REPO_ROOT / "candidate_data.json")
    p.add_argument("--output", type=Path, default=REPO_ROOT / "data.json")
    p.add_argument("--filters", type=Path, default=SCRIPT_DIR / "filters.json")
    p.add_argument("--prompts-dir", type=Path, default=SCRIPT_DIR / "prompts")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default: {DEFAULT_MODEL})")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"Ollama host (default: {DEFAULT_HOST})")
    p.add_argument("--timeout", type=int, default=300, help="Per-call timeout in seconds (first call pays cold-load)")
    p.add_argument("--num-ctx", type=int, default=2048, help="Model context window (prompts are short; small is faster)")
    p.add_argument("--num-predict", type=int, default=256, help="Max output tokens (the structured result is tiny)")
    p.add_argument("--keep-alive", default="10m", help="How long Ollama keeps the model loaded (e.g. 10m, -1 for forever)")
    p.add_argument("--think", action="store_true", help="Allow the model to emit reasoning (off by default; thinking models otherwise return empty output)")
    p.add_argument("--limit", type=int, default=0, help="Only process the first N candidates")
    p.add_argument("--only-filter", default="", help="Run a single filter by id")
    p.add_argument("--print-prompts", action="store_true", help="Print assembled prompts and exit (no model calls)")
    args = p.parse_args()

    raw = json.loads(args.input.read_text(encoding="utf-8"))
    filters = json.loads(args.filters.read_text(encoding="utf-8"))["filters"]
    candidates = raw.get("candidates", [])
    if args.limit:
        candidates = candidates[: args.limit]

    if args.only_filter and not any(f["id"] == args.only_filter for f in filters):
        sys.stderr.write(f"Unknown filter id: {args.only_filter}\n")
        return 2

    if args.print_prompts:
        print_prompts(candidates, filters, args.prompts_dir, args.only_filter)
        return 0

    model = OllamaModel(
        args.model, args.host, args.timeout,
        args.num_ctx, args.num_predict, args.keep_alive, args.think,
    )
    sys.stderr.write(f"Tagging {len(candidates)} candidates with {len(filters)} filters "
                     f"using {args.model} @ {args.host} "
                     f"(num_ctx={args.num_ctx}, num_predict={args.num_predict}, keep_alive={args.keep_alive})\n")

    out_candidates = []
    for i, c in enumerate(candidates, 1):
        sys.stderr.write(f"[{i}/{len(candidates)}] {c['candidate_id']}\n")
        try:
            out_candidates.append(
                tag_candidate(c, filters, model, args.prompts_dir, args.only_filter)
            )
        except RuntimeError as e:
            sys.stderr.write(f"\nERROR: {e}\n")
            return 1

    output = {
        "schema_version": "1.0",
        "filters": [
            {"id": f["id"], "label": f["label"], "description": f["description"]}
            for f in filters
        ],
        "static_questions": raw.get("static_questions", []),
        "candidates": out_candidates,
    }
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    sys.stderr.write(f"\nWrote {args.output}\n")
    report(out_candidates, candidates)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
