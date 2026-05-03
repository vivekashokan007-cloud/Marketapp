# ═══════════════════════════════════════════════════════════════
# PHASE E — Snapshot / Positioning / Alerts
# ═══════════════════════════════════════════════════════════════
#
# This file contains the four Phase E functions to be inserted into brain.py.
# Each function ports app.js logic VERBATIM under Port-First Principle.
# JS source lines cited inline. No threshold tuning. No "improvements".
#
# Insertion order in brain.py:
#   1. build_chain_snapshot_data         AFTER compute_overnight_delta (~L3290)
#   2. compute_positioning                AFTER build_chain_snapshot_data
#   3. compute_global_boost               AFTER compute_positioning
#   4. evaluate_alerts                    AFTER compute_global_boost
#
# Pipeline integration in analyze() at end of file.
# ═══════════════════════════════════════════════════════════════


# ───────────────────────────────────────────────────────────────
# DECISION #24 — build_chain_snapshot_data
# ───────────────────────────────────────────────────────────────
# JS source: app.js L3458-3482, function buildChainSnapshotData()
# Port-first: JS reads pre-computed values from STATE; Python reads
# pre-computed values from ctx. No new computation, just shape.
# ───────────────────────────────────────────────────────────────

def build_chain_snapshot_data(ctx: dict) -> dict:
    """
    Build snapshot data from current ctx state for 2PM / 3:15PM persistence.

    Port-first from app.js L3458-3482 (function buildChainSnapshotData).
    JS reads STATE.bnfChain, STATE.nfChain, STATE.live, STATE.baseline,
    STATE.bnfBreadth, STATE.nf50Breadth. Python equivalent reads from ctx
    which is populated by Kotlin from SharedPreferences.

    Returns flat dict matching the shape Supabase chain_snapshots.data
    column expects (snake_case keys to match #25's consumer naming).
    """
    bnf_chain = ctx.get('bnfChain', {}) or {}
    nf_chain = ctx.get('nfChain', {}) or {}
    live = ctx.get('live', {}) or {}
    baseline = ctx.get('baseline', {}) or {}
    bnf_breadth = ctx.get('bnfBreadth', {}) or {}
    nf50_breadth = ctx.get('nf50Breadth', {}) or {}

    # JS L3461-3463: spot/vix fall back from live to baseline
    bnf_spot = live.get('bnfSpot') or baseline.get('bnfSpot')
    nf_spot = live.get('nfSpot') or baseline.get('nfSpot')
    vix = live.get('vix') or baseline.get('vix')

    # Field names use snake_case (matches #25's consumer naming via
    # JS L6068-6075 which maps camelCase snapData315 to snake_case for
    # computePositioning). We standardise on snake_case at storage time
    # so #25 reads them directly.
    snapshot = {
        'date': ctx.get('today_ist'),  # Kotlin populates with API.todayIST() equivalent
        'bnf_spot': bnf_spot,
        'nf_spot': nf_spot,
        'vix': vix,
        'bnf_pcr': bnf_chain.get('pcr'),
        'bnf_near_atm_pcr': bnf_chain.get('nearAtmPCR'),
        'nf_pcr': nf_chain.get('pcr'),
        'bnf_max_pain': bnf_chain.get('maxPain'),
        'nf_max_pain': nf_chain.get('maxPain'),
        'bnf_call_wall': bnf_chain.get('callWallStrike'),
        'bnf_call_wall_oi': bnf_chain.get('callWallOI'),
        'bnf_put_wall': bnf_chain.get('putWallStrike'),
        'bnf_put_wall_oi': bnf_chain.get('putWallOI'),
        'bnf_total_call_oi': bnf_chain.get('totalCallOI'),
        'bnf_total_put_oi': bnf_chain.get('totalPutOI'),
        'nf_total_call_oi': nf_chain.get('totalCallOI'),
        'nf_total_put_oi': nf_chain.get('totalPutOI'),
        'bnf_atm_iv': bnf_chain.get('atmIv'),
        'bnf_futures_prem': bnf_chain.get('futuresPremium'),
        'bnf_breadth_pct': bnf_breadth.get('weightedPct'),
        'nf50_advancing': nf50_breadth.get('scaled'),
    }
    return snapshot


# ───────────────────────────────────────────────────────────────
# DECISION #25 — compute_positioning
# ───────────────────────────────────────────────────────────────
# JS source: app.js L3508-3569, function computePositioning(snap2pm, snap315pm)
# Port-first: weighted scoring system (2/2/1.5/1.5/1/1), 6 signals,
# strength tiers, plus the 6th branch that reads pcr_context.
# ───────────────────────────────────────────────────────────────

def compute_positioning(snap_2pm: dict, snap_315pm: dict, ctx: dict) -> dict:
    """
    Compare 2PM vs 3:15PM snapshots → detect institutional positioning.

    Port-first from app.js L3508-3569 (function computePositioning).
    Returns None if either snapshot missing (JS L3509).

    The 6th signal reads ctx.pcr_context which is the output of
    get_institutional_pcr (Phase D D6). The integration step in analyze()
    must populate ctx['pcr_context'] from Phase D's result before this
    function runs.
    """
    # JS L3509
    if not snap_2pm or not snap_315pm:
        return None

    # JS L3511-3524: build delta dict
    delta = {
        'callOiDelta': (snap_315pm.get('bnf_total_call_oi') or 0) - (snap_2pm.get('bnf_total_call_oi') or 0),
        'putOiDelta': (snap_315pm.get('bnf_total_put_oi') or 0) - (snap_2pm.get('bnf_total_put_oi') or 0),
        'nfCallOiDelta': (snap_315pm.get('nf_total_call_oi') or 0) - (snap_2pm.get('nf_total_call_oi') or 0),
        'nfPutOiDelta': (snap_315pm.get('nf_total_put_oi') or 0) - (snap_2pm.get('nf_total_put_oi') or 0),
        'pcrChange': (snap_315pm.get('bnf_pcr') or 0) - (snap_2pm.get('bnf_pcr') or 0),
        'nearPcrChange': (snap_315pm.get('bnf_near_atm_pcr') or 0) - (snap_2pm.get('bnf_near_atm_pcr') or 0),
        'vixChange': (snap_315pm.get('vix') or 0) - (snap_2pm.get('vix') or 0),
        'maxPainShift': (snap_315pm.get('bnf_max_pain') or 0) - (snap_2pm.get('bnf_max_pain') or 0),
        'breadthChange': (snap_315pm.get('bnf_breadth_pct') or 0) - (snap_2pm.get('bnf_breadth_pct') or 0),
        'spotChange': (snap_315pm.get('bnf_spot') or 0) - (snap_2pm.get('bnf_spot') or 0),
        # JS L3522-3523: raw values for display
        'snap2pm': snap_2pm,
        'snap315pm': snap_315pm,
    }

    # JS L3527: score each signal
    bear_score = 0.0
    bull_score = 0.0

    # Signal 1: OI imbalance — JS L3530-3532
    # Note: net_oi_delta is computed by JS but never used (dead variable)
    if delta['callOiDelta'] > delta['putOiDelta'] * 1.5:
        bear_score += 2  # heavy call writing = bearish
    elif delta['putOiDelta'] > delta['callOiDelta'] * 1.5:
        bull_score += 2  # heavy put writing = bullish (defense)

    # Signal 2: PCR direction — JS L3535-3536
    if delta['pcrChange'] < -0.05:
        bear_score += 1.5
    elif delta['pcrChange'] > 0.05:
        bull_score += 1.5

    # Signal 3: VIX direction — JS L3539-3540
    if delta['vixChange'] > 0.3:
        bear_score += 1.5
    elif delta['vixChange'] < -0.3:
        bull_score += 1.5

    # Signal 4: Max Pain shift — JS L3543-3544
    if delta['maxPainShift'] < -100:
        bear_score += 1
    elif delta['maxPainShift'] > 100:
        bull_score += 1

    # Signal 5: BNF Breadth — JS L3547-3548
    if delta['breadthChange'] < -0.5:
        bear_score += 1
    elif delta['breadthChange'] > 0.5:
        bull_score += 1

    # Signal 6: PCR context — JS L3552-3556
    # Reads ctx.pcr_context (output of get_institutional_pcr Phase D D6)
    pcr_context = ctx.get('pcr_context')
    if pcr_context and pcr_context.get('confidence') != 'LOW':
        bias = pcr_context.get('bias')
        if bias == 'BULL':
            bull_score += 1
        elif bias == 'BEAR':
            bear_score += 1
        elif bias == 'MILD_BULL':
            bull_score += 0.5

    # JS L3559-3566: signal + strength tiers
    net_score = bull_score - bear_score
    if net_score >= 3:
        signal = 'BULLISH'
        strength = min(5, round(net_score))
    elif net_score >= 1:
        signal = 'BULLISH'
        strength = min(3, round(net_score))
    elif net_score <= -3:
        signal = 'BEARISH'
        strength = min(5, round(abs(net_score)))
    elif net_score <= -1:
        signal = 'BEARISH'
        strength = min(3, round(abs(net_score)))
    else:
        signal = 'NEUTRAL'
        strength = 1

    # JS L3568: return shape
    return {
        'delta': delta,
        'signal': signal,
        'strength': strength,
        'bullScore': bull_score,
        'bearScore': bear_score,
        'netScore': net_score,
    }


# ───────────────────────────────────────────────────────────────
# DECISION #26 — compute_global_boost
# ───────────────────────────────────────────────────────────────
# JS source: app.js L3572-3621, function computeGlobalBoost(tomorrowSignal, positioningResult)
# Port-first: 3 global signals (Dow, Crude, GIFT), boost ±1 per agreement,
# strength clamped to 1..5. JS mutates tomorrowSignal in place; Python
# returns new dict (Decision 2 from Part 1, behavior identical).
# ───────────────────────────────────────────────────────────────

# Constants reused from Phase B compute_overnight_delta port:
# DOW_THRESHOLD = 0.5    (% change)
# CRUDE_THRESHOLD = 1.5  (% change)
# GIFT_THRESHOLD = 0.3   (% change)
# These are already in brain.py _CONST{} from Phase B. DO NOT duplicate.
# If grep at P5 reveals they're absent, add them from app.js L94-96.

def compute_global_boost(positioning_result: dict, ctx: dict,
                         dow_threshold: float = 0.5,
                         crude_threshold: float = 1.5,
                         gift_threshold: float = 0.3) -> dict:
    """
    Combine positioning result with Dow/Crude/GIFT direction → tomorrow_signal.

    Port-first from app.js L3572-3621 (function computeGlobalBoost).
    Returns None if positioning is None or NEUTRAL (matches JS L3573 early return).

    JS mutates tomorrowSignal in place. Python returns a new dict
    instead — behavior identical (same output values), cleaner architecture.

    Constants default to Phase B values for testability; in production
    integration, brain.py's _CONST DOW_THRESHOLD / CRUDE_THRESHOLD /
    GIFT_THRESHOLD are passed in (or wired in via module-level constants).
    """
    if not positioning_result:
        return None

    # JS L3573: early return if NEUTRAL or missing
    base_signal = positioning_result.get('signal')
    if base_signal == 'NEUTRAL':
        return None

    # JS L3576-3577: reset to base strength, init globalBoost = 0
    tomorrow_signal = {
        'signal': base_signal,
        'strength': positioning_result.get('strength', 1),
        'globalBoost': 0,
    }

    gd = ctx.get('globalDirection', {}) or {}
    is_bull = base_signal == 'BULLISH'
    boost = 0

    # Signal 1: Dow — JS L3584-3590
    dow_close = gd.get('dowClose')
    dow_now = gd.get('dowNow')
    if dow_close and dow_now:
        dow_pct = ((dow_now - dow_close) / dow_close) * 100.0
        if abs(dow_pct) >= dow_threshold:
            if (dow_pct > 0 and is_bull) or (dow_pct < 0 and not is_bull):
                boost += 1
            else:
                boost -= 1

    # Signal 2: Crude — JS L3593-3599 (INVERTED for India: rising crude = bearish)
    crude_settle = gd.get('crudeSettle')
    crude_now = gd.get('crudeNow')
    if crude_settle and crude_now:
        crude_pct = ((crude_now - crude_settle) / crude_settle) * 100.0
        if abs(crude_pct) >= crude_threshold:
            if (crude_pct < 0 and is_bull) or (crude_pct > 0 and not is_bull):
                boost += 1
            else:
                boost -= 1

    # Signal 3: GIFT — JS L3601-3615
    # Primary: live GIFT vs evening close
    gift_ref = (ctx.get('eveningClose') or {}).get('gift')
    gift_now = gd.get('giftNow')
    if gift_ref and gift_now:
        gift_pct = ((gift_now - gift_ref) / gift_ref) * 100.0
        if abs(gift_pct) >= gift_threshold:
            if (gift_pct > 0 and is_bull) or (gift_pct < 0 and not is_bull):
                boost += 1
            else:
                boost -= 1
    elif ctx.get('gapInfo') and ctx['gapInfo'].get('sigma') is not None:
        # JS L3609-3615: fallback to morning gap
        gap_sigma = ctx['gapInfo']['sigma']
        gift_bull = gap_sigma > 0.3
        gift_bear = gap_sigma < -0.3
        if (gift_bull and is_bull) or (gift_bear and not is_bull):
            boost += 1
        elif (gift_bull and not is_bull) or (gift_bear and is_bull):
            boost -= 1

    # JS L3617-3620: apply boost
    if boost != 0:
        base_strength = positioning_result.get('strength', 1)
        tomorrow_signal['strength'] = max(1, min(5, base_strength + boost))
        tomorrow_signal['globalBoost'] = boost

    return tomorrow_signal


# ───────────────────────────────────────────────────────────────
# DECISION #27 — evaluate_alerts
# ───────────────────────────────────────────────────────────────
# JS source: app.js L5928-6018, function handleNotifications (alert
# decision logic only — orchestration L6020-6131 stays in Kotlin).
#
# Ports 7 alert categories per Decision 3 in Part 1. Calibration values
# verbatim from JS:
#   TARGET_NEAR ratio = 0.8 (NOT 0.5 — that's deferred per BL-27a)
#   STOP_LOSS ratio   = 0.7
#   FORCE_DETERIORATES: aligned <= 1 AND current_pnl > 0
#   SIGMA_IMPORTANT_THRESHOLD = 2.0
#   NOISE_WINDOW = 15
#   LAST_ENTRY_CUTOFF = 345
#   ROUTINE_NOTIFY_MS = 30 * 60 * 1000
# ───────────────────────────────────────────────────────────────

# Constants — port verbatim from app.js L33,38,41,44,94-96.
# These are likely already in brain.py from Phase B (verify P5).
# If absent, ADD to brain.py _CONST{} block.
NOISE_WINDOW = 15  # minutes since open — first 15 min suppress all alerts
LAST_ENTRY_CUTOFF = 345  # minutes since open — 3:00 PM
ROUTINE_NOTIFY_MS = 30 * 60 * 1000  # 30 minutes in milliseconds
SIGMA_IMPORTANT_THRESHOLD = 2.0
TARGET_NEAR_RATIO = 0.8  # PORT-FIRST: 0.8 verbatim from JS L5966
STOP_LOSS_RATIO = 0.7   # PORT-FIRST: 0.7 verbatim from JS L5975


def _friendly_type(strategy_type: str) -> str:
    """
    Match JS friendlyType() — used for alert body labels.
    Port from app.js (search 'friendlyType' callsite L5947, L5963, etc.)
    Brain.py likely has its own equivalent helper. If not, this is a
    minimal shim. Antigravity verifies during integration.
    """
    mapping = {
        'BEAR_CALL': 'Bear Call',
        'BULL_PUT': 'Bull Put',
        'IRON_CONDOR': 'Iron Condor',
        'IRON_BUTTERFLY': 'Iron Butterfly',
        'BULL_CALL': 'Bull Call',
        'BEAR_PUT': 'Bear Put',
    }
    return mapping.get(strategy_type, strategy_type)


def evaluate_alerts(open_trades: list, watchlist: list, result: dict, ctx: dict) -> list:
    """
    Decide which alerts to fire this poll. Returns list of alert dicts.

    Port-first from app.js L5928-6018 (handleNotifications, alert decision
    logic only). Orchestration (2PM/3:15PM scans, DB writes, positioning
    candidate generation) stays in Kotlin / pipeline orchestration.

    Each alert dict has:
      key: str        — dedup key (Kotlin's lastAlertKeys uses this)
      category: str   — one of WATCHLIST | POSITION | MARKET | ROUTINE
      priority: str   — one of urgent | important | entry | routine
      title: str      — notification title (mobile: <60 chars)
      body: str       — notification body (mobile: <120 chars)

    Time gates (JS L5933): if elapsed < NOISE_WINDOW, return [].
    """
    alerts = []

    elapsed = ctx.get('mins_since_open', 0)
    now_ms = ctx.get('now_ms', 0)
    last_routine_notify = ctx.get('last_routine_dispatch_ms', 0)

    # JS L5933: noise window — first 15 min, suppress all
    if elapsed < NOISE_WINDOW:
        return alerts

    # JS L5936: significantMove flag — Kotlin computes and passes via ctx
    significant_move = ctx.get('significant_move', False)
    abs_spot_sigma = ctx.get('abs_spot_sigma', 0.0)
    abs_vix_sigma = ctx.get('abs_vix_sigma', 0.0)

    # ═══ IMPORTANT NOTIFICATIONS — JS L5935-6001 ═══
    if significant_move:

        # JS L5939-5959: Watchlist alignment alerts
        for cand in (watchlist or []):
            if not cand.get('_alignmentChanged'):
                continue

            forces = cand.get('forces') or {}
            aligned = forces.get('aligned', 0)
            prev_aligned = cand.get('_prevAlignment', 0)

            cand_index = cand.get('index', '')
            cand_type = cand.get('type', '')
            sell_strike = cand.get('sellStrike', '')
            buy_strike = cand.get('buyStrike', '')
            is_credit = cand.get('isCredit', False)
            net_premium = cand.get('netPremium', 0)

            # JS L5942-5950: 3/3 alignment achieved
            if aligned == 3 and prev_aligned < 3:
                if elapsed < LAST_ENTRY_CUTOFF:
                    alerts.append({
                        'key': f"WATCHLIST_ENTRY_{cand_index}_{sell_strike}_{buy_strike}",
                        'category': 'WATCHLIST',
                        'priority': 'entry',
                        'title': '🎯 Entry Window',
                        'body': f"{cand_index} {_friendly_type(cand_type)} {sell_strike}/{buy_strike} — 3/3 aligned. {'Credit' if is_credit else 'Debit'} ₹{net_premium}",
                    })
            # JS L5951-5958: 3/3 alignment lost
            elif prev_aligned == 3 and aligned < 3:
                alerts.append({
                    'key': f"WATCHLIST_CLOSING_{cand_index}_{sell_strike}_{buy_strike}",
                    'category': 'WATCHLIST',
                    'priority': 'important',
                    'title': '⚠️ Window Closing',
                    'body': f"{cand_index} {_friendly_type(cand_type)} {sell_strike}/{buy_strike} — dropped to {aligned}/3",
                })

        # JS L5962-5991: Position exit signals
        for trade in (open_trades or []):
            t_index = trade.get('index_key', '')
            t_type = trade.get('strategy_type', '')
            t_sell_strike = trade.get('sell_strike', '')
            t_id = trade.get('id', '')
            current_pnl = trade.get('current_pnl', 0)
            max_profit = trade.get('max_profit', 0)
            max_loss = trade.get('max_loss', 0)
            t_forces = trade.get('forces') or {}
            t_aligned = t_forces.get('aligned')

            trade_label = f"{t_index} {_friendly_type(t_type)} {t_sell_strike}"

            # JS L5966-5972: Target Near at 0.8
            # PORT-FIRST: 0.8 verbatim. The 0.5 fix is BL-27a deferred.
            if max_profit and current_pnl >= max_profit * TARGET_NEAR_RATIO:
                pct_of_max = round(current_pnl / max_profit * 100) if max_profit else 0
                alerts.append({
                    'key': f"POS_TARGET_{t_id}",
                    'category': 'POSITION',
                    'priority': 'urgent',
                    'title': '💰 Target Near',
                    'body': f"{trade_label} P&L ₹{current_pnl} ({pct_of_max}% of max). Book profit.",
                })

            # JS L5975-5981: Stop Loss at 0.7
            if max_loss and current_pnl <= -max_loss * STOP_LOSS_RATIO:
                alerts.append({
                    'key': f"POS_STOP_{t_id}",
                    'category': 'POSITION',
                    'priority': 'urgent',
                    'title': '🛑 Stop Loss Near',
                    'body': f"{trade_label} P&L ₹{current_pnl}. Cut position.",
                })

            # JS L5984-5990: Force Deterioration with profit
            if t_aligned is not None and t_aligned <= 1 and current_pnl > 0:
                alerts.append({
                    'key': f"POS_BOOK_{t_id}",
                    'category': 'POSITION',
                    'priority': 'urgent',
                    'title': '⚡ Book Profit',
                    'body': f"{trade_label} Forces {t_aligned}/3 but profitable ₹{current_pnl}. Take it.",
                })

        # JS L5994-6000: Significant Move
        if abs_spot_sigma > SIGMA_IMPORTANT_THRESHOLD or abs_vix_sigma > SIGMA_IMPORTANT_THRESHOLD:
            live = ctx.get('live', {}) or {}
            bnf_spot = live.get('bnfSpot')
            vix = live.get('vix')
            spot_sigma = live.get('spotSigma', 0)
            vix_sigma = live.get('vixSigma', 0)
            bnf_str = f"{int(bnf_spot)}" if bnf_spot is not None else 'N/A'
            vix_str = f"{vix:.1f}" if vix is not None else 'N/A'
            alerts.append({
                'key': f"SIG_MOVE_{bnf_str}_{vix_str}",
                'category': 'MARKET',
                'priority': 'important',
                'title': '📊 Significant Move',
                'body': f"BNF {bnf_str} ({spot_sigma}σ) VIX {vix_str} ({vix_sigma}σ)",
            })

    # ═══ ROUTINE NOTIFICATIONS — JS L6004-6018 ═══
    if (now_ms - last_routine_notify) >= ROUTINE_NOTIFY_MS:
        live = ctx.get('live', {}) or {}
        bnf_spot = live.get('bnfSpot')
        vix = live.get('vix')
        bnf_str = f"{int(bnf_spot)}" if bnf_spot is not None else 'N/A'
        vix_str = f"{vix:.1f}" if vix is not None else 'N/A'

        body = f"BNF {bnf_str} | VIX {vix_str}"

        if open_trades:
            total_pnl = sum((t.get('current_pnl') or 0) for t in open_trades)
            body += f" | {len(open_trades)} pos P&L ₹{total_pnl}"

        # JS L6012-6015: top watchlist if no positions
        if watchlist and not open_trades:
            top = watchlist[0]
            top_forces = top.get('forces') or {}
            top_aligned = top_forces.get('aligned', 0)
            top_type = top.get('type', '')
            body += f" | Top: {top_aligned}/3 {_friendly_type(top_type)}"

        alerts.append({
            'key': f"ROUTINE_{int(now_ms / ROUTINE_NOTIFY_MS)}",
            'category': 'ROUTINE',
            'priority': 'routine',
            'title': '📈 Market Update',
            'body': body,
        })

    return alerts


# ═══════════════════════════════════════════════════════════════
# ANALYZE() INTEGRATION
# ═══════════════════════════════════════════════════════════════
# This block is INSERTED into brain.py analyze() AFTER the Phase D
# position monitoring block, BEFORE the synthesize_verdict call.
#
# Antigravity's integration step: paste the block below into analyze()
# at the appropriate point. The exact line number depends on existing
# brain.py structure — Antigravity reads brain.py to find the right spot.
# ═══════════════════════════════════════════════════════════════

PHASE_E_INTEGRATION_BLOCK = '''
    # ═══════════════════════════════════════════════════════════════
    # PHASE E — Snapshot / Positioning / Alerts (Decisions #24-27)
    # Insert AFTER Phase D position-monitoring block.
    # Insert BEFORE final result return.
    # ═══════════════════════════════════════════════════════════════

    # Wire pcr_context into ctx for #25 consumption
    # (output of Phase D D6 get_institutional_pcr — verify field name in brain.py)
    if 'pcr_context' in result:
        ctx['pcr_context'] = result['pcr_context']

    mins_since_open = ctx.get('mins_since_open', 0)

    # #24 — Build current snapshot if at/past 2PM (mins >= 270)
    snapshot_now = None
    if mins_since_open >= 270:
        snapshot_now = build_chain_snapshot_data(ctx)
        result['chain_snapshot_now'] = snapshot_now

    # #25 — Compute positioning if at 3:15PM (mins >= 345) AND 2PM baseline exists
    positioning_result = None
    snap_2pm = ctx.get('snap_2pm_today')  # Kotlin populates from SharedPreferences
    if mins_since_open >= 345 and snap_2pm and snapshot_now:
        positioning_result = compute_positioning(snap_2pm, snapshot_now, ctx)
        if positioning_result:
            result['positioning'] = positioning_result

    # #26 — Compute global boost (only if positioning produced non-NEUTRAL signal)
    tomorrow_signal_result = None
    if positioning_result:
        tomorrow_signal_result = compute_global_boost(positioning_result, ctx)
        if tomorrow_signal_result:
            result['tomorrow_signal'] = tomorrow_signal_result

    # #27 — Evaluate alerts (every poll, after positioning/signal computed)
    alerts_result = evaluate_alerts(
        open_trades=ctx.get('open_trades') or [],
        watchlist=ctx.get('watchlist') or [],
        result=result,
        ctx=ctx,
    )
    result['alerts'] = alerts_result

    # END PHASE E
    # ═══════════════════════════════════════════════════════════════
'''
