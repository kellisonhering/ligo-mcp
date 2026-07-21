# Campaign replacement artifact seal — §7.5d

**Sealed:** 2026-07-21T09:08:09Z  
**Campaign records collected at seal time:** 76  
**Replacement pool:** 52 specs, one for each frozen exclusion

## Why this seal exists

A blind numerical audit found 52 original campaign specs containing non-finite
values. The outcome-independent exclusion rule and complete ID list were frozen
in `REVIEW_FINDINGS.md` §7.5d and `campaign_repair/invalid_specs.json` before the
campaign resumed. Original artifacts and records were not changed.

The replacement generator preserved each excluded spec's preselected
injection/control assignment and signal parameters without printing them, then
drew fresh O3 noise until every stored array passed strict finiteness checks.
No campaign outcome was read or compared with truth during this process.

## What this proves

`SHA256SUMS.txt` hashes every truth-bearing replacement artifact:

- `campaign_truth_replacements.jsonl`
- `pool_index_replacements.json`
- `pool_gen_replacements.log`
- all 52 `pool/replacement_spec_*.npz` files

**Aggregate hash (SHA-256 of the manifest):**

```
a86736e9f0e40628aac194da1975466e985b4cee015173d03431643ed543850c
```

The replacement data lives outside the repository at
`~/experiment-data/ligo/campaign_replacements_2026_07/`. Only its hashes and
this note are committed. The original `campaign_seal/` remains authoritative
for the original 300-spec pool and was not rewritten or extended.

## Final accounting

Final scoring uses 248 finite original specs plus these 52 replacements for
exactly 300 valid runs. Attempts involving excluded original IDs remain in the
append-only campaign record as disclosed invalid attempts and are not scored.
