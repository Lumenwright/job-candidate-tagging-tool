# Per-filter prompt overrides

Drop a file named `<filter_id>.txt` here to override the extraction instruction
for that filter. The file's contents replace the `extraction_rule` from
`../filters.json` — nothing else. The system prompt, the way candidate blocks are
presented, and the structured JSON output schema are owned by `tag_candidates.py`
and cannot be overridden, so a prompt can never break the provenance contract.

Valid filter ids (one optional file each):

- `shipped_to_production.txt`
- `explicit_governance_or_risk_mitigation.txt`
- `multi_step_workflow_design.txt`
- `non_traditional_or_low_code_delivery.txt`
- `concrete_technical_tradeoffs_stated.txt`

A prompt file is just the "what counts as a match" guidance for the local model,
e.g.:

```
Fires only when the candidate states a system was deployed to real end users or
kept running in production. Treat "demo", "prototype", "local script", "POC", and
"sandbox" as NOT firing unless paired with explicit deployment/maintenance.
```

To see exactly how a prompt is assembled before spending model time:

```
python ../tag_candidates.py --print-prompts --only-filter shipped_to_production --limit 1
```
