# Position exit policy v1 — net target and executable exit contract

**Contract version:** `position_exit_policy_v1_net_20260912`  
**Net-target version:** `net_target_v1_gross_minus_costs_once_20260912`  
**Legacy monitor identity:** `POSITION_POLICY_V1` (gross SHADOW_* gate; unchanged)  
**Date:** 2026-09-12  
**Package:** G4 (Release C prep)

> **Net-basis alignment IS a new policy version.** It is not a silent
> reinterpretation of `canonical_won`, `outcome_h2`, `won`, or TP-hit
> (`is_success` / `target_was_reached`). Those columns keep their
> historical meanings. New learning-target fields are additive.

Do **not** add DTE scaling or optimize `TP_MULT` / `SL_MULT` in this
package. Training remains frozen. Live orders remain off.

Shared conformance fixtures: [`fixtures/position_exit_policy_v1.json`](fixtures/position_exit_policy_v1.json).

---

## 1. Identity

| Field | Definition |
|---|---|
| `position_exit_policy_version` | `position_exit_policy_v1_net_20260912` |
| `net_target_version` | `net_target_v1_gross_minus_costs_once_20260912` |
| `legacy_position_policy_version` | `POSITION_POLICY_V1` |
| Instrument / expiry / index / strategy | Required on every evaluation |
| Execution mode | `intraday` vs `swing` / overnight — separate cohorts |
| Decision time | Instant used for entry-window and availability |
| Candidate / trade identity | Stable id; missing id → no entry |

Record model / feature / selector / evaluator hashes on the provenance
block when available. Teacher outcomes and actual fills **share this
policy definition** and keep different **evidence grades**:
`COUNTERFACTUAL_TEACHER` vs `OBSERVED_MONITOR`.

---

## 2. Quantity

| Field | Definition |
|---|---|
| `lot_size` | Effective-date exchange lot size (positive) |
| `lots` | Explicit lot count (positive) |
| `unit` | **`INR_TOTAL` only** on this contract |

Do **not** conflate per-unit premium with total currency. A stream whose
`unit` is `PER_UNIT_PREMIUM` is rejected (`quantity_unit_not_total_currency`).

---

## 3. Entry

| Rule | Definition |
|---|---|
| Executable price | Short entry = bid; long entry = ask. LTP is not an executable fill. |
| Quote age / quality | `OK` / `EXECUTABLE` required. Negative age is invalid. |
| Complete legs | Every required strategy leg present. Incomplete → `NO_ENTRY`. |
| Cost estimate | Friction known **at entry**. Later closing quotes and realized future costs must not rewrite entry thresholds. |
| Intraday cutoff | No new intraday entry at/after **15:15 IST** (`no_entry_after_eod_intent`). Native entry window is already 09:15–15:15; this contract restates it. Overnight/swing is a different cohort. |

---

## 4. Net target (new learning fields)

```
net = executable_gross − applicable_costs    # exactly once
```

| Result | Rule | New field |
|---|---|---|
| `WIN` | `net > 0` | `learning_result_net=WIN`, `learning_won_net=true` |
| `FLAT` | `net == 0` (explicit) | `learning_result_net=FLAT`, `learning_flat_net=true` |
| `LOSS` | `net < 0` | `learning_result_net=LOSS` |
| `UNAVAILABLE` | net is null | `learning_result_net=UNAVAILABLE`, won/flat null |

A **positive EOD exit is a net win** even if TP was never hit.

### Legacy columns (readable, not rewritten)

| Column | Historical meaning (unchanged) |
|---|---|
| `canonical_won` | H2 structure valuation `sim_pnl_h2 > 0` |
| `outcome_h2` / `won` | Same H2 label |
| `is_success` / `target_was_reached` | Teacher **TP-hit**, not “net > 0” |

Consumers that want the G4 learning target must read
`learning_result_net` / `learning_won_net` and the version constants.
Do not treat a new net win as a rewrite of `canonical_won`.

Currency tolerance for teacher/monitor agreement: **₹1.00**.

---

## 5. Exit

### Reference parameters (do not optimize)

| Constant | Value | Notes |
|---|---|---|
| `TP_MULT` | `0.50` | Applied to **net** max-profit at entry |
| `SL_MULT` | `0.60` | Applied to **net** max-loss at entry (credit-loss multiplier) |
| Policy exit intent | **15:15 IST** | Voluntary paper/teacher cutoff — **not** exchange close |

```
net_max_profit_at_entry = max(gross_max_profit − entry_cost, 0)
net_max_loss_at_entry   = gross_max_loss + entry_cost
tp_threshold            = TP_MULT × net_max_profit_at_entry
sl_threshold            = − SL_MULT × net_max_loss_at_entry
```

Thresholds are frozen at entry. Trigger basis is **net P&L**.

### Stop / target precedence

On the same mark:

1. **SL** (`net <= sl_threshold`)
2. **TP** (`net >= tp_threshold`)
3. **EOD** (intraday, first usable mark at/after 15:15 IST)

This matches the Kotlin monitor's protective order. A later change is a
new policy version.

### Published-threshold composition

Publication may only make an alert fire **earlier**:

| Arm | Composition |
|---|---|
| Target | `min(constant, published)` |
| Stop | `max(constant, published)` (both typically negative; closer-to-zero fires first) |

Stale / absent publication → constants stand.

### EOD / overnight

| Cohort | Rule |
|---|---|
| Intraday | Exit at first usable mark at/after 15:15. If the stream ends earlier with a valued mark, bind EOD to that last executable mark. If **no** valued mark exists, `MISSING_QUOTES` and net is null — **do not invent a 15:15 fill**. |
| Overnight / swing / manual | **Explicit**. Do not force a same-day EOD label. If the stream ends without TP/SL → `OVERNIGHT_HOLD`. |

### Gap / slippage

When the next executable mark crosses the stop, keep the **realized**
net. Do not clip the loss to `sl_threshold`.

### Five-minute replay

`replay_resolution=FIVE_MINUTE_APPROXIMATE` is labelled and
`replay_certified_tick_equivalent=false`. It cannot certify equivalence
to an unobserved tick path.

---

## 6. Extrema

| Field | Definition |
|---|---|
| Basis | `NET_POST_ENTRY_OBSERVED` on this contract |
| Unit | INR total for recorded lots |
| Coverage | `full` / `partial` / `none` over the post-entry stream |
| Projection | `false` — values are observations, not forecasts |
| Timestamps | `peak_ts` / `trough_ts` of the observed net extrema |

G3 top-level `peak_pnl` / `trough_pnl` on `trades_v2` remain **GROSS**.
Do not overwrite those columns with net extrema.

---

## 7. Provenance

Every result carries:

- `data_cutoff_ts` (events after cutoff are ignored)
- `input_hash` (SHA-256 of identity + entry + events + published)
- `policy_version` / `net_target_version`
- `evaluator` identity
- source quality / skipped unusable events

Entry-time thresholds must not use later closing quotes or realized
future costs. Monitor decisions use only data then available.

---

## 8. Market open / close vs policy exit intent

Do **not** randomly change unrelated windows. Document the discrepancy
and use **15:15** consistently for **this** contract.

| Clock | Where | Role |
|---|---|---|
| 09:15 IST | Native + Python | Session / entry open |
| **15:15 IST** | `PositionPolicyV1.EOD_HH_MM`, native entry window 09:15–15:15, **this contract** | Policy exit intent and no-new-intraday-entry |
| 15:30 IST | Python `check_execution_readiness` | Readiness helper (cash-market close) |
| 15:40 IST | `MarketWatchService`, `PositionTickService`, `NativeBridge`, `MarketOpenScheduler` | Native poll / session-close window |

A change to any of those clocks is a separately versioned experiment.

---

## 9. Active vs contract evaluation

| Path | Behaviour after G4 |
|---|---|
| Live SHADOW_* alerts | **Unchanged.** Still `POSITION_POLICY_V1` on **gross** `current_pnl` vs `0.50 * max_profit` / `−0.60 * max_loss`. |
| Recommendation / `p_ml` gate | **Unchanged.** |
| Teacher `canonical_won` / H2 / `is_success` | **Unchanged.** |
| New contract fields + traces | Additive. Teacher and monitor evaluate the **net** contract on the same stream. |

Wiring a consumer to the contract version constant does not, by itself,
change the active gate.

---

## 10. Conformance

On the same ordered event/quote stream, the Python teacher path and the
Kotlin monitor semantics must agree on:

- entry validity
- trigger precedence
- exit reason
- exit timestamp
- net P&L within ₹1.00

Required fixture coverage (see `fixtures/position_exit_policy_v1.json`):

positive EOD / non-TP; zero net; missing quotes; late quotes; gap
through stop; lot sizes; incomplete legs; published thresholds; no
entry after EOD intent; explicit overnight; same-mark SL-before-TP;
five-minute approximate label; per-unit quantity rejected.
