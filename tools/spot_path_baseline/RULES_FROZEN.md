# Spot-path baseline — frozen rule registry

Recorded **before** scoring. Every rule here is recovered from a verified source; nothing
is invented. Where a rule could not be recovered it is listed as UNFROZEN and is not scored.

## Verified sources

| source | identity | verified |
|---|---|---|
| observer blob | `0ea85ce1dd42df8c3c4c2ac2acd920adaf6fca9a` | yes — `git ls-tree` on the branch below |
| observer branch revision | `9bb4a69f4c5645b99a437f14c9605df3a79683d0` | `review/claude-empirical-direction-20261107` |
| observer path | `app/src/main/python/index_empirical_direction_v1.py` | |
| pinned native base | `83de973c364073d952fbcabf2b9435b27dadd873` | this branch's base |

## Frozen rules

| rule | value | provenance |
|---|---|---|
| horizons | 15, 30, 60 min | observer `FRAME_WINDOWS_MIN` |
| anchor tolerance | 2.5 min | observer `ANCHOR_TOLERANCE_MIN` |
| path version | `observed_path_v1_log_frames_retained_extrema` | observer `PATH_VERSION` |
| rank version | `empirical_midrank_v1` | observer `RANK_VERSION` |
| target label | log return of index spot, anchor poll → anchor + horizon | handoff |
| label maturity | anchor + horizon must fall inside the same session, else `UNMATURED_LABEL` | observer reason code |
| forecast timestamp | the anchor poll; no later information may enter | handoff |
| missing data | a frame with a missing endpoint is excluded and counted, never interpolated | observer |
| **dependence** | **at most one observation per earlier session per population**, deduplicated by min digest | observer: "78 correlated polls from one session must never count as 78 independent observations" |
| session split | chronological; a frame is scored only against strictly earlier sessions | handoff |
| separation | NIFTY and BANKNIFTY never pooled | handoff |

## UNFROZEN — not scored

* **E1 neighbour-selection rule**, **neighbour count k**, **weighting**. These are defined in
  review patch `19f7828ddd1b84f482f5542ef7c343e07407be6d3fa15d6f420dc86616e8b176`, which is
  not present in this session. The verified observer blob supplies the *mechanism*
  (midrank against comparable frames, one per earlier session) but not the selection rule.
  Scoring E1 would mean inventing a frozen rule, so no E1 number is reported.

## Holdout status

The sessions used here were already read during earlier exploratory work in this same
session. They are therefore **exploratory**, not untouched validation data. No holdout
claim is made.
