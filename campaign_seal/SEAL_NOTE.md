# Campaign artifact seal — 2026-07 injection campaign

**Sealed:** 2026-07-16T20:20:50Z
**Campaign records collected at seal time:** 7 (blind line-count of `campaign_2026_07.jsonl`; contents not inspected)
**Pool:** 300 contiguous specs (spec_0000 … spec_0299)

## What this proves

At the sealed timestamp above, the injection truth set and pool were fixed. The
manifest [`SHA256SUMS.txt`](SHA256SUMS.txt) hashes every truth-bearing artifact:

- `campaign_truth.jsonl` — the sealed answer key (masses, SNR, sky position, kind)
- `pool_index.json` — spec → gps map
- `pool_gen.log` — generation log (contains masses/SNR)
- all 300 `pool/*.npz` — the injection/noise arrays themselves

**Aggregate hash (SHA-256 of the manifest):**
```
4d249376ee97dfbf4526ccef1c8c06e3487c04e7c1743360793bea28dc7b5c2c
```

Committing this manifest publicly timestamps the truth set. Anyone can later
re-hash the artifacts and confirm they were not altered after sealing — this
upgrades the §7.5 blinding from an honor-system promise to a verifiable fact.

## What is NOT in this repo

The campaign data itself is deliberately absent. Only hashes are committed. The
truth files live in `~/experiment-data/ligo/campaign/` (gitignored, off-repo) and
are now filesystem read-only (`0444`; pool dir `0555`).

## Honest disclosure

Sealing happened *after* 7 campaign records had already been collected (the
campaign began before this hardening pass). Those 7 runs are structurally blind
(the planner never receives injection truth — enforced by `_assert_blind()`), and
no human compared them against truth. The 7-record head start is disclosed here
rather than hidden. The pool/truth themselves were generated before any scored
run, so the answer key predates all 300 experiments.

## Verifying the seal later

```
cd ~/experiment-data/ligo/campaign
shasum -a 256 -c <(grep -v '^#' /path/to/ligo-mcp/campaign_seal/SHA256SUMS.txt)
```
All lines must report `OK`.
