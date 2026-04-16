import json, math, os as _os

# ── ML Engine bootstrap (silent-fail if model not yet trained) ───────────
_ML_ENGINE     = None
_ML_MODEL_PATH = '/data/data/com.marketradar.app/files/ml_model.json'

def _ml_load_if_needed():
    global _ML_ENGINE
    if _ML_ENGINE is not None:
        return _ML_ENGINE
    try:
        import ml_engine as _mle
        if _os.path.exists(_ML_MODEL_PATH):
            _ML_ENGINE = _mle.load_model(_ML_MODEL_PATH)
    except Exception:
        pass
    return _ML_ENGINE

def _ml_score(candidate_dict):
    """Score candidate with ML engine. Returns {} if model not loaded.
    mlAction='BLOCKED' = model has zero training data for this scenario."""
    engine = _ml_load_if_needed()
    if engine is None:
        return {}
    try:
        p, reg, detail = engine.predict(candidate_dict)
        return {
            'p_ml':          round(p, 4),
            'ml_action':     detail.get('action', 'WATCH'),
            'ml_regime':     reg,
            'ml_edge':       detail.get('edge', 0.0),
            'ml_ood':        detail.get('ood', False),
            'ml_ood_conf':   detail.get('ood_conf', 1.0),
            'ml_ood_warn':   detail.get('ood_warns', []),
            'ml_ood_blocked':detail.get('ood_blocked', False),
        }
    except Exception:
        return {}

def ml_score_bridge(cand_json):
    """Bridge for Kotlin to call _ml_score with JSON string."""
    try:
        cand = json.loads(cand_json)
        res = _ml_score(cand)
        return json.dumps(res)
    except Exception as e:
        return json.dumps({'error': str(e)})

# ─── UTILITIES ───

def lsq_slope(values):
    n = len(values)
    if n < 2: return 0.0
    xm = (n - 1) / 2.0
    ym = sum(values) / n
    num = sum((i - xm) * (v - ym) for i, v in enumerate(values))
    den = sum((i - xm) ** 2 for i in range(n))
    return num / den if den != 0 else 0.0

def pct_change(old, new):
    if not old or old == 0 or new is None: return 0.0
    return (new - old) / abs(old) * 100

def last_n(polls, n=6):
    return polls[-n:] if len(polls) >= n else polls

def get_time_mins(t_str):
    try:
        parts = t_str.split(':')
        return int(parts[0]) * 60 + int(parts[1])
    except: return 0

# ─── SHARED REGIME DETECTOR ───

# ═══ b93: DYNAMIC THRESHOLD INFRASTRUCTURE ═══
# Z-scores replace ALL hardcoded thresholds. (value - mean) / stddev
# If stddev is 0 (constant data), returns 0. Needs minimum 10 samples.

def arr_mean(arr):
    return sum(arr) / len(arr) if arr else 0

def arr_std(arr):
    if len(arr) < 2: return 0
    m = arr_mean(arr)
    variance = sum((x - m) ** 2 for x in arr) / len(arr)
    return math.sqrt(variance) if variance > 0 else 0

def z_score(val, arr):
    """How many standard deviations is val from the mean of arr?"""
    if len(arr) < 10 or val is None: return 0
    m = arr_mean(arr)
    s = arr_std(arr)
    return (val - m) / s if s > 0 else 0

def straddle_velocity(polls, n=4):
    """Is ATM straddle premium expanding or contracting?
    Returns: (current_straddle, z_score_of_change, is_expanding)"""
    straddles = [p.get('straddle') for p in last_n(polls, n) if p.get('straddle')]
    if len(straddles) < 3: return 0, 0, False
    changes = [straddles[i] - straddles[i-1] for i in range(1, len(straddles))]
    avg_change = arr_mean(changes)
    # Expanding = straddle growing while spot may be flat
    return straddles[-1], avg_change, avg_change > 0

def theta_friction_minutes(est_cost, net_theta):
    """How many minutes of theta decay to pay for entry/exit friction?
    Returns minutes. >60 = trade is mathematically dead."""
    if not net_theta or net_theta <= 0 or not est_cost or est_cost <= 0:
        return 999
    theta_per_min = (net_theta * 0.65) / 375  # Gemini fix: only ~65% of daily theta decays during market hours
    return est_cost / theta_per_min if theta_per_min > 0 else 999

def detect_regime(polls, baseline):
    """Returns dict: {type: range|trend|choppy, sigma, direction, trend_pct}"""
    recent = last_n(polls, 6)
    bnfs = [p.get('bnf') for p in recent if p.get('bnf')]
    if len(bnfs) < 3:
        return {"type": "unknown", "sigma": 0, "direction": 0, "trend_pct": 0}
    hi, lo = max(bnfs), min(bnfs)
    rng = hi - lo
    base_vix = baseline.get('vix', 15)
    base_spot = baseline.get('bnfSpot', bnfs[0])
    daily_sigma = base_spot * (base_vix / 100) / math.sqrt(252) if base_spot > 0 else 300
    range_sigma = rng / daily_sigma if daily_sigma > 0 else 0
    direction_votes = 0
    for i in range(1, len(bnfs)):
        if bnfs[i] > bnfs[i-1]: direction_votes += 1
        elif bnfs[i] < bnfs[i-1]: direction_votes -= 1
    trend_pct = abs(direction_votes) / (len(bnfs) - 1) if len(bnfs) > 1 else 0
    if range_sigma < 0.25 and trend_pct < 0.6:
        rtype = "range"
    elif range_sigma > 0.6 and trend_pct > 0.6:
        rtype = "trend"
    elif range_sigma > 0.8:
        rtype = "choppy"
    else:
        rtype = "mild_trend"
    return {"type": rtype, "sigma": range_sigma, "direction": direction_votes, "trend_pct": trend_pct}

def get_pcr_slope(polls):
    pcrs = [p.get('pcr') for p in last_n(polls, 6) if p.get('pcr')]
    if len(pcrs) < 3: return 0, 0
    return lsq_slope(pcrs), pcrs[-1] - pcrs[0]

def get_vix_vals(polls):
    return [p.get('vix') for p in last_n(polls, 6) if p.get('vix')]

# ═══════════════════════════════════════════
# PART 1: MARKET ANALYSES (shown in Market tab)
# ═══════════════════════════════════════════

def pcr_velocity(polls, baseline):
    window = last_n(polls, 6)
    pcrs = [p.get('pcr') for p in window if p.get('pcr') is not None and p.get('pcr') > 0]
    if len(pcrs) < 3: return None
    total_change = pcrs[-1] - pcrs[0]
    if abs(total_change) < 0.08: return None
    mins = len(pcrs) * 5
    if total_change > 0.25:
        return {"type": "pcr", "icon": "📈", "label": "PCR surging",
                "detail": f"{pcrs[0]:.2f} → {pcrs[-1]:.2f} in {mins}min. Puts building — institutional hedging.",
                "impact": "bullish", "strength": min(5, int(abs(total_change) * 12))}
    elif total_change < -0.25:
        return {"type": "pcr", "icon": "📉", "label": "PCR collapsing",
                "detail": f"{pcrs[0]:.2f} → {pcrs[-1]:.2f} in {mins}min. Puts unwinding or calls loading.",
                "impact": "bearish", "strength": min(5, int(abs(total_change) * 12))}
    else:
        d = "rising" if total_change > 0 else "falling"
        imp = "bullish" if total_change > 0 else "bearish"
        return {"type": "pcr", "icon": "🔄", "label": f"PCR {d}",
                "detail": f"{pcrs[0]:.2f} → {pcrs[-1]:.2f} ({mins}min). Gradual shift.",
                "impact": imp, "strength": 2}

def oi_wall_shift(polls, baseline):
    if len(polls) < 3: return None
    first, last = polls[max(0, len(polls)-6)], polls[-1]
    cw0, cw1 = first.get('cw'), last.get('cw')
    pw0, pw1 = first.get('pw'), last.get('pw')
    if cw0 and cw1 and cw0 != cw1:
        moved = cw1 - cw0
        d = "UP" if moved > 0 else "DOWN"
        return {"type": "oi_wall", "icon": "🧱", "label": f"Call wall shifted {d}",
                "detail": f"BNF call wall {cw0} → {cw1} ({'+' if moved > 0 else ''}{moved}). {'Resistance rising.' if moved > 0 else 'Sellers tightening.'}",
                "impact": "bullish" if moved > 0 else "bearish", "strength": 3}
    if pw0 and pw1 and pw0 != pw1:
        moved = pw1 - pw0
        d = "UP" if moved > 0 else "DOWN"
        return {"type": "oi_wall", "icon": "🧱", "label": f"Put wall shifted {d}",
                "detail": f"BNF put wall {pw0} → {pw1} ({'+' if moved > 0 else ''}{moved}). {'Support rising.' if moved > 0 else 'Support crumbling.'}",
                "impact": "bullish" if moved > 0 else "bearish", "strength": 3}
    cwOI0, cwOI1 = first.get('cwOI'), last.get('cwOI')
    if cwOI0 and cwOI1 and cw0 == cw1 and cwOI0 > 0:
        chg = pct_change(cwOI0, cwOI1)
        if abs(chg) > 15:
            return {"type": "oi_wall", "icon": "🏗️" if chg > 0 else "💨",
                    "label": f"Call wall {'strengthening' if chg > 0 else 'weakening'}",
                    "detail": f"OI at {cw1}: {'+' if chg > 0 else ''}{chg:.0f}%. {'Resistance hardening.' if chg > 0 else 'Breakout possible.'}",
                    "impact": "bearish" if chg > 0 else "bullish", "strength": 3 if abs(chg) > 20 else 2}
    return None

def vix_momentum(polls, baseline):
    vixs = get_vix_vals(polls)
    if len(vixs) < 3: return None
    total = vixs[-1] - vixs[0]
    if abs(total) < 0.3: return None
    curr = vixs[-1]
    if curr >= 24 and total < -0.5:
        return {"type": "vix", "icon": "🌊", "label": "VIX mean-reverting DOWN",
                "detail": f"VIX {vixs[0]:.1f} → {curr:.1f}. Extreme vol unwinding — credit shrinking, debit cheaper.",
                "impact": "neutral", "strength": 3}
    elif curr < 16 and total > 0.3:
        return {"type": "vix", "icon": "⚡", "label": "VIX waking up",
                "detail": f"VIX {vixs[0]:.1f} → {curr:.1f}. Vol expanding — premiums inflating.",
                "impact": "neutral", "strength": 2}
    elif total > 0.5:
        return {"type": "vix", "icon": "🔺", "label": "VIX climbing",
                "detail": f"VIX {vixs[0]:.1f} → {curr:.1f} (+{total:.1f}). Fear rising.",
                "impact": "caution", "strength": min(4, int(abs(total) * 2))}
    elif total < -0.5:
        return {"type": "vix", "icon": "🔻", "label": "VIX falling",
                "detail": f"VIX {vixs[0]:.1f} → {curr:.1f} ({total:.1f}). Vol crush — credit positions profit.",
                "impact": "bullish", "strength": min(4, int(abs(total) * 2))}
    return None

def spot_exhaustion(polls, baseline):
    if len(polls) < 6: return None
    recent = polls[-8:] if len(polls) >= 8 else polls
    mid = len(recent) // 2
    def avg_abs_move(seg, key='bnf'):
        moves = []
        for i in range(1, len(seg)):
            v0, v1 = seg[i-1].get(key), seg[i].get(key)
            if v0 and v1: moves.append(abs(v1 - v0))
        return sum(moves) / len(moves) if moves else 0
    m1 = avg_abs_move(recent[:mid])
    m2 = avg_abs_move(recent[mid:])
    if m1 < 30: return None
    ratio = m2 / m1 if m1 > 0 else 1
    if ratio < 0.4:
        return {"type": "exhaustion", "icon": "😤", "label": "Momentum exhausting",
                "detail": f"BNF avg move {m1:.0f} → {m2:.0f} pts/poll. Range-bound ahead?",
                "impact": "neutral", "strength": 3}
    elif ratio > 2.0 and m2 > 50:
        return {"type": "exhaustion", "icon": "🚀", "label": "Momentum accelerating",
                "detail": f"BNF avg move {m1:.0f} → {m2:.0f} pts/poll. Breakout underway.",
                "impact": "caution", "strength": 4}
    return None

def regime_detector(polls, baseline):
    r = detect_regime(polls, baseline)
    if r["type"] == "range":
        return {"type": "regime", "icon": "📦", "label": f"Range-bound ({r['sigma']:.2f}σ)",
                "detail": f"IB/IC candidates favored. Vol crush likely.",
                "impact": "neutral", "strength": 3}
    elif r["type"] == "trend":
        d = "bullish" if r["direction"] > 0 else "bearish"
        arrow = "↗" if r["direction"] > 0 else "↘"
        strat = "Bull Call" if d == "bullish" else "Bear Put"
        return {"type": "regime", "icon": arrow, "label": f"Trending {d} ({r['sigma']:.2f}σ)",
                "detail": f"Directional: {strat} favored.",
                "impact": d, "strength": min(5, int(r['sigma'] * 4))}
    elif r["type"] == "choppy":
        return {"type": "regime", "icon": "⚡", "label": f"Choppy ({r['sigma']:.2f}σ)",
                "detail": f"No clear direction. Widen stops or wait.",
                "impact": "caution", "strength": 4}
    return None

def futures_premium_trend(polls, baseline):
    fps = [p.get('fp') for p in last_n(polls, 6) if p.get('fp') is not None]
    if len(fps) < 3: return None
    total = fps[-1] - fps[0]
    if abs(total) < 0.02: return None
    if total > 0.03:
        return {"type": "futures", "icon": "📊", "label": "Futures premium widening",
                "detail": f"{fps[0]:.3f} → {fps[-1]:.3f}. Longs building — bullish.",
                "impact": "bullish", "strength": 2}
    elif total < -0.03:
        return {"type": "futures", "icon": "📊", "label": "Futures premium narrowing",
                "detail": f"{fps[0]:.3f} → {fps[-1]:.3f}. Longs exiting — bearish.",
                "impact": "bearish", "strength": 2}
    return None

def oi_velocity(polls, baseline):
    if len(polls) < 4: return None
    first, last = polls[max(0, len(polls)-6)], polls[-1]
    t0 = (first.get('bnfCOI', 0) or 0) + (first.get('bnfPOI', 0) or 0)
    t1 = (last.get('bnfCOI', 0) or 0) + (last.get('bnfPOI', 0) or 0)
    if t0 == 0: return None
    chg = pct_change(t0, t1)
    if abs(chg) < 5: return None
    if chg > 10:
        return {"type": "oi_vel", "icon": "🏗️", "label": "OI building fast",
                "detail": f"Total BNF OI +{chg:.0f}%. Expect vol expansion.",
                "impact": "caution", "strength": 3}
    elif chg < -10:
        return {"type": "oi_vel", "icon": "🏚️", "label": "OI unwinding",
                "detail": f"Total BNF OI {chg:.0f}%. Vol crush likely — credit profits.",
                "impact": "bullish", "strength": 3}
    elif abs(chg) >= 5:
        d = "expanding" if chg > 0 else "contracting"
        return {"type": "oi_vel", "icon": "📈" if chg > 0 else "📉", "label": f"OI {d}",
                "detail": f"BNF OI {'+' if chg > 0 else ''}{chg:.0f}%.",
                "impact": "neutral", "strength": 1}
    return None

def institutional_clock(polls, baseline):
    if len(polls) < 2: return None
    last_t = polls[-1].get('t', '')
    mins = get_time_mins(last_t)
    if mins < 825 or mins > 915: return None  # 13:45 to 15:15
    post_2pm = [p for p in polls if get_time_mins(p.get('t', '')) >= 825]
    if len(post_2pm) < 2: return None
    pcr_s, pcr_e = post_2pm[0].get('pcr'), post_2pm[-1].get('pcr')
    if pcr_s and pcr_e and abs(pcr_e - pcr_s) > 0.1:
        d = "bullish" if pcr_e > pcr_s else "bearish"
        return {"type": "inst_clock", "icon": "🏛️", "label": f"Institutional {d} shift",
                "detail": f"2PM→now PCR {pcr_s:.2f} → {pcr_e:.2f}. Tomorrow's intent revealed.",
                "impact": d, "strength": 4}
    cw_s, cw_e = post_2pm[0].get('cw'), post_2pm[-1].get('cw')
    if cw_s and cw_e and cw_s != cw_e:
        d = "bullish" if cw_e > cw_s else "bearish"
        return {"type": "inst_clock", "icon": "🏛️", "label": "Late-day call wall move",
                "detail": f"Call wall {cw_s} → {cw_e} after 2PM.",
                "impact": d, "strength": 3}
    return None

# ═══════════════════════════════════════════
# PART 2: POSITION ANALYSES (shown on each trade card)
# ═══════════════════════════════════════════

def position_wall_proximity(trade, polls, baseline, regime, strike_oi):
    """b96: Is sell strike near a wall? Checks correct wall for each strategy type.
    Bear Call: sell CE vs call wall. Bull Put: sell PE vs put wall.
    IC: sell CE vs call wall AND sell PE (sell_strike2) vs put wall."""
    sell = trade.get('sell_strike', 0)
    sell2 = trade.get('sell_strike2', 0)  # PE sell for IC
    idx = trade.get('index_key', 'BNF')
    stype = trade.get('strategy_type', '')
    last = polls[-1] if polls else {}
    cw = last.get('cw' if idx == 'BNF' else 'nfCW')
    pw = last.get('pw' if idx == 'BNF' else 'nfPW')
    is_bear = 'BEAR' in stype
    is_ic = stype in ('IRON_CONDOR', 'IRON_BUTTERFLY')
    is_bull = 'BULL' in stype
    
    # IC/IB: check BOTH sides
    if is_ic:
        insights = []
        # CE side: call wall should be ABOVE sell CE
        if cw and sell:
            dist = cw - sell
            if dist < 0:
                insights.append(f"CE sell {sell} above call wall {cw}")
            elif 0 <= dist <= 200:
                insights.append(None)  # protected, mark as OK
        # PE side: put wall should be ABOVE sell PE (between spot and sell)
        if pw and sell2:
            dist2 = pw - sell2  # positive = wall above sell PE = protected
            if dist2 < 0:
                insights.append(f"PE sell {sell2} below put wall {pw}")
            elif 0 <= dist2 <= 200:
                insights.append(None)  # protected
        exposed = [i for i in insights if i is not None]
        if len(exposed) == 2:
            return {"icon": "🚨", "label": "Past the wall",
                    "detail": f"{exposed[0]}. {exposed[1]}. Both sides exposed.",
                    "impact": "caution", "strength": 5}
        if len(exposed) == 1:
            return {"icon": "⚠️", "label": "Past the wall",
                    "detail": f"{exposed[0]}. One side unprotected.",
                    "impact": "caution", "strength": 4}
        if len(insights) >= 2 and all(i is None for i in insights):
            return {"icon": "🛡️", "label": "Wall-protected",
                    "detail": f"CE: wall {cw} above sell. PE: wall {pw} above sell.",
                    "impact": "bullish", "strength": 4}
        return None
    
    # Bear Call: call wall should be ABOVE sell CE
    if is_bear and cw:
        dist = cw - sell
        if dist < 0:
            return {"icon": "⚠️", "label": "Past the wall",
                    "detail": f"Sell {sell} is ABOVE call wall {cw}. No OI protection.",
                    "impact": "caution", "strength": 4}
        if 0 <= dist <= 200:
            return {"icon": "🛡️", "label": "Wall-protected",
                    "detail": f"Call wall {cw} {'AT' if dist == 0 else f'{dist}pts above'} sell {sell}.",
                    "impact": "bullish", "strength": 4 if dist == 0 else 3}
    
    # Bull Put: put wall should be ABOVE sell PE (between spot and sell)
    if is_bull and pw:
        dist = pw - sell  # positive = wall above sell = protected
        if dist < 0:
            return {"icon": "⚠️", "label": "Past the wall",
                    "detail": f"Sell {sell} is BELOW put wall {pw}. No OI support above sell.",
                    "impact": "caution", "strength": 4}
        if 0 <= dist <= 200:
            return {"icon": "🛡️", "label": "Wall-protected",
                    "detail": f"Put wall {pw} {'AT' if dist == 0 else f'{dist}pts above'} sell {sell}.",
                    "impact": "bullish", "strength": 4 if dist == 0 else 3}
    
    # OI trend at sell strike
    if len(strike_oi) >= 3:
        oi_field = 'sellCOI' if is_bear else 'sellPOI'
        ois = [s.get(oi_field) for s in strike_oi if s.get(oi_field) is not None]
        if len(ois) >= 3 and ois[0] > 0:
            chg = pct_change(ois[0], ois[-1])
            if chg < -15:
                return {"icon": "💨", "label": "OI at sell strike fading",
                        "detail": f"OI at {sell}: {chg:.0f}%. Protection weakening.",
                        "impact": "caution", "strength": 3}
            elif chg > 15:
                return {"icon": "🏗️", "label": "OI at sell strike building",
                        "detail": f"OI at {sell}: +{chg:.0f}%. Protection strengthening.",
                        "impact": "bullish", "strength": 2}
    return None

def position_momentum_threat(trade, polls, baseline, regime, strike_oi):
    """Is spot accelerating toward OR already past the sell strike?"""
    sell = trade.get('sell_strike', 0)
    idx = trade.get('index_key', 'BNF')
    spot_key = 'bnf' if idx == 'BNF' else 'nf'
    is_bear = 'BEAR' in trade.get('strategy_type', '')
    recent = last_n(polls, 4)
    spots = [p.get(spot_key) for p in recent if p.get(spot_key)]
    if len(spots) < 3: return None
    curr = spots[-1]
    cushion = (sell - curr) if is_bear else (curr - sell)  # positive = safe
    # b96: BREACH — spot already at or past sell strike
    if cushion <= 0:
        breach = abs(cushion)
        return {"icon": "🚨", "label": f"Spot PAST sell strike by {breach:.0f}pts",
                "detail": f"Spot {curr:.0f} {'above' if is_bear else 'below'} sell {sell}. Position in maximum danger.",
                "impact": "caution", "strength": 5}
    # Velocity toward sell strike
    if is_bear:
        velocity = spots[-1] - spots[-2] if len(spots) >= 2 else 0
    else:
        velocity = spots[-2] - spots[-1] if len(spots) >= 2 else 0
    # velocity > 0 means approaching sell
    if velocity > 0 and cushion < 300:
        polls_to_hit = cushion / velocity if velocity > 0 else 999
        if polls_to_hit <= 3:
            return {"icon": "🚨", "label": f"Sell strike in {polls_to_hit:.0f} polls",
                    "detail": f"Spot {curr:.0f} → sell {sell}. {cushion:.0f}pts at {velocity:.0f}pts/poll.",
                    "impact": "caution", "strength": 5}
        elif polls_to_hit <= 6:
            return {"icon": "⚡", "label": "Spot approaching sell",
                    "detail": f"{cushion:.0f}pts cushion, moving {velocity:.0f}pts/poll.",
                    "impact": "caution", "strength": 3}
    return None

def position_regime_fit(trade, polls, baseline, regime, strike_oi):
    """Does current regime match the trade's strategy type?"""
    stype = trade.get('strategy_type', '')
    rtype = regime.get('type', 'unknown')
    is_4leg = stype in ('IRON_CONDOR', 'IRON_BUTTERFLY')
    is_directional = stype in ('BEAR_CALL', 'BULL_PUT', 'BEAR_PUT', 'BULL_CALL')
    if is_4leg and rtype == 'trend':
        d = "up" if regime["direction"] > 0 else "down"
        return {"icon": "⚠️", "label": "4-leg in trending market",
                "detail": f"Market trending {d} ({regime['sigma']:.2f}σ). One leg under pressure.",
                "impact": "caution", "strength": 3}
    if is_directional and rtype == 'range':
        return {"icon": "📦", "label": "Directional in range",
                "detail": f"Market range-bound ({regime['sigma']:.2f}σ). Theta helps but no directional edge.",
                "impact": "neutral", "strength": 2}
    if is_directional and rtype == 'trend':
        is_bear = 'BEAR' in stype
        trend_dir = regime["direction"]
        # Only flag trend conflict when direction is definitive (not neutral/zero)
        if abs(trend_dir) < 0.01:
            return None
        trend_bull = trend_dir > 0
        if (is_bear and trend_bull) or (not is_bear and not trend_bull):
            return {"icon": "🔴", "label": "Against the trend",
                    "detail": f"Your {'bearish' if is_bear else 'bullish'} trade vs {'bullish' if trend_bull else 'bearish'} trend.",
                    "impact": "caution", "strength": 4}
        else:
            return {"icon": "🟢", "label": "With the trend",
                    "detail": f"Trend confirming your position.",
                    "impact": "bullish", "strength": 2}
    return None

def position_vix_headwind(trade, polls, baseline, regime, strike_oi):
    """Did VIX regime shift unfavorably since entry?"""
    vixs = get_vix_vals(polls)
    if len(vixs) < 2: return None
    curr_vix = vixs[-1]
    entry_vix = trade.get('entry_vix')
    stype = trade.get('strategy_type', '')
    is_credit = stype in ('BEAR_CALL', 'BULL_PUT', 'IRON_CONDOR', 'IRON_BUTTERFLY')
    # Credit trade entered below 24, VIX now above 24
    if is_credit and entry_vix and entry_vix < 24 and curr_vix >= 24:
        return {"icon": "🔥", "label": "VIX crossed VERY_HIGH since entry",
                "detail": f"Entry VIX {entry_vix:.1f} → now {curr_vix:.1f}. Backtest: debit > credit at VIX≥24.",
                "impact": "caution", "strength": 4}
    # Debit trade entered above 24, VIX now below 20
    if not is_credit and entry_vix and entry_vix >= 24 and curr_vix < 20:
        return {"icon": "💨", "label": "Vol crushed since entry",
                "detail": f"Entry VIX {entry_vix:.1f} → now {curr_vix:.1f}. Premium evaporating.",
                "impact": "caution", "strength": 3}
    return None

def position_book_signal(trade, polls, baseline, regime, strike_oi):
    """Combine P&L + CI + brain factors → book / hold / exit."""
    pnl = trade.get('current_pnl', 0)
    max_p = trade.get('max_profit', 1)
    ci = trade.get('controlIndex')
    pnl_pct = pnl / max_p if max_p > 0 else 0
    _, pcr_chg = get_pcr_slope(polls)
    is_credit = trade.get('is_credit', True)
    # Strong book signal: > 50% profit + positive CI + exhausting momentum
    if pnl_pct >= 0.5 and (ci is None or ci > 0):
        # Check if momentum is fading
        rtype = regime.get('type', '')
        if rtype == 'range' or rtype == 'mild_trend':
            return {"icon": "💰", "label": f"BOOK — {pnl_pct*100:.0f}% profit in range",
                    "detail": f"P&L ₹{pnl:.0f} ({pnl_pct*100:.0f}%). Range = theta in your favor. Lock it in.",
                    "impact": "bullish", "strength": 4}
    # Hold signal: good P&L + strong trend in your favor
    if pnl_pct >= 0.3 and pnl_pct < 0.5 and ci and ci > 20:
        return {"icon": "🔒", "label": "Hold — trend + control",
                "detail": f"P&L ₹{pnl:.0f} ({pnl_pct*100:.0f}%). CI {ci}. Let it run.",
                "impact": "bullish", "strength": 2}
    # Danger: losing + against trend
    if pnl < 0 and regime.get('type') == 'trend':
        is_bear = 'BEAR' in trade.get('strategy_type', '')
        trend_bull = regime["direction"] > 0
        if (is_bear and trend_bull) or (not is_bear and not trend_bull):
            max_l = trade.get('max_loss', 1)
            loss_pct = abs(pnl) / max_l if max_l > 0 else 0
            if loss_pct > 0.3:
                return {"icon": "🛑", "label": f"EXIT — against trend, {loss_pct*100:.0f}% of max loss",
                        "detail": f"P&L ₹{pnl:.0f}. Trend working against you.",
                        "impact": "caution", "strength": 5}
    return None

# ═══════════════════════════════════════════
# PART 3: CANDIDATE ANALYSES (shown on each candidate card)
# ═══════════════════════════════════════════

def candidate_flow_alignment(cand, polls, baseline, regime):
    """Does PCR velocity support this candidate's direction?"""
    _, pcr_chg = get_pcr_slope(polls)
    if abs(pcr_chg) < 0.08: return None
    ctype = cand.get('type', '')
    is_bear = 'BEAR' in ctype
    pcr_bull = pcr_chg > 0  # rising PCR = puts building = contrarian bullish
    if is_bear and pcr_bull and pcr_chg > 0.15:
        return {"icon": "⚠️", "label": "Against institutional flow",
                "detail": f"PCR rising ({pcr_chg:+.2f}) = bullish flow vs your bearish trade.",
                "impact": "caution", "strength": 3}
    elif not is_bear and not pcr_bull and pcr_chg < -0.15:
        return {"icon": "⚠️", "label": "Against institutional flow",
                "detail": f"PCR falling ({pcr_chg:+.2f}) = bearish flow vs your bullish trade.",
                "impact": "caution", "strength": 3}
    elif (is_bear and not pcr_bull) or (not is_bear and pcr_bull):
        return {"icon": "✅", "label": "Flow-aligned",
                "detail": f"PCR {'rising' if pcr_bull else 'falling'} confirms {'bullish' if pcr_bull else 'bearish'} flow.",
                "impact": "bullish", "strength": 2}
    return None

def candidate_wall_protection(cand, polls, baseline, regime):
    """b92: Full wall protection check — both sides for IC/IB, exposed detection."""
    sell = cand.get('sellStrike', 0)
    sell2 = cand.get('sellStrike2', 0)  # PE side for IC/IB
    idx = cand.get('index', 'BNF')
    ctype = cand.get('type', '')
    is_bear = 'BEAR' in ctype
    is_4leg = ctype in ('IRON_CONDOR', 'IRON_BUTTERFLY')
    last = polls[-1] if polls else {}
    cw = last.get('cw' if idx == 'BNF' else 'nfCW')
    pw = last.get('pw' if idx == 'BNF' else 'nfPW')
    
    # 4-leg: check BOTH sides independently
    if is_4leg and cw and pw:
        ce_exposed = cw < sell if sell else False  # call wall below CE sell = exposed
        pe_exposed = sell2 > pw if sell2 else False  # sell PE above put wall = no support = exposed
        if ce_exposed and pe_exposed:
            return {"icon": "🚨", "label": "BOTH sides exposed",
                    "detail": f"CE sell {sell} above call wall {cw}. PE sell {sell2} above put wall {pw}. No protection.",
                    "impact": "caution", "strength": 5}
        if ce_exposed:
            return {"icon": "⚠️", "label": f"CE side past call wall",
                    "detail": f"Sell CE {sell} > call wall {cw}. Upside unprotected.",
                    "impact": "caution", "strength": 4}
        if pe_exposed:
            return {"icon": "⚠️", "label": f"PE side past put wall",
                    "detail": f"Sell PE {sell2} > put wall {pw}. No support above sell.",
                    "impact": "caution", "strength": 4}
        # Both protected — wall is between spot and sell on each side
        ce_dist = cw - sell if cw and sell else 999
        pe_dist = pw - sell2 if sell2 and pw else 999  # positive = wall above sell PE = protected
        if ce_dist >= 0 and ce_dist <= 300 and pe_dist >= 0 and pe_dist <= 300:
            return {"icon": "🛡️", "label": "Both sides wall-backed",
                    "detail": f"CE: wall {cw} ({ce_dist}pts above). PE: wall {pw} ({pe_dist}pts above sell).",
                    "impact": "bullish", "strength": 4}
        return None
    
    # 2-leg directional: check relevant wall
    if is_bear and cw:
        dist = cw - sell
        if dist < 0:
            return {"icon": "⚠️", "label": f"Sell ABOVE call wall",
                    "detail": f"Sell {sell} > call wall {cw}. No OI ceiling. Today's rally can hit you.",
                    "impact": "caution", "strength": 5}
        if 0 <= dist <= 300:
            return {"icon": "🛡️", "label": f"Wall at {cw} ({dist}pts above)",
                    "detail": f"Call wall OI protects your sell.",
                    "impact": "bullish", "strength": 3}
    elif not is_bear and not is_4leg and pw:
        dist = sell - pw  # positive = sell ABOVE wall = exposed; negative = sell BELOW wall = protected
        if dist > 0:
            return {"icon": "⚠️", "label": f"Sell ABOVE put wall",
                    "detail": f"Sell {sell} > put wall {pw}. No OI floor. Breakdown can hit you.",
                    "impact": "caution", "strength": 5}
        if -300 <= dist < 0:
            return {"icon": "🛡️", "label": f"Wall at {pw} ({abs(dist)}pts above sell)",
                    "detail": f"Put wall OI protects your sell.",
                    "impact": "bullish", "strength": 3}
    return None

def candidate_regime_fit(cand, polls, baseline, regime):
    """Does this strategy fit the current regime?"""
    ctype = cand.get('type', '')
    rtype = regime.get('type', 'unknown')
    is_4leg = ctype in ('IRON_CONDOR', 'IRON_BUTTERFLY')
    is_directional = ctype in ('BEAR_CALL', 'BULL_PUT', 'BEAR_PUT', 'BULL_CALL')
    if is_4leg and rtype == 'range':
        return {"icon": "✅", "label": "Regime fit: range confirmed",
                "detail": f"Range ({regime['sigma']:.2f}σ). 4-leg profits from vol crush.",
                "impact": "bullish", "strength": 3}
    if is_4leg and rtype == 'trend':
        return {"icon": "⚠️", "label": "Regime mismatch: trending",
                "detail": f"Market trending ({regime['sigma']:.2f}σ). 4-leg has a losing side.",
                "impact": "caution", "strength": 3}
    if is_directional and rtype == 'range':
        return {"icon": "📦", "label": "Range — theta helps, direction doesn't",
                "detail": f"Range ({regime['sigma']:.2f}σ). Credit OK for theta. IB/IC may be better.",
                "impact": "neutral", "strength": 2}
    if is_directional and rtype == 'trend':
        is_bear = 'BEAR' in ctype
        trend_bull = regime["direction"] > 0
        if (is_bear and not trend_bull) or (not is_bear and trend_bull):
            return {"icon": "✅", "label": "Trend-aligned entry",
                    "detail": f"Trend confirms your direction.",
                    "impact": "bullish", "strength": 3}
        else:
            return {"icon": "🔴", "label": "Against the trend",
                    "detail": f"Trend is {'up' if trend_bull else 'down'}, your trade is {'bearish' if is_bear else 'bullish'}.",
                    "impact": "caution", "strength": 4}
    return None

def evaluate_candidate_risk(cand, ctx, open_trades, regime):
    """b92: Function #48 — deep per-candidate risk evaluation.
    Returns LIST of insights. Uses enriched candidate data (20+ fields).
    Checks: cost trap, R:R sanity, open trade conflict, width adequacy, force coherence."""
    insights = []
    ctype = cand.get('type', '')
    idx = cand.get('index', 'BNF')
    max_p = cand.get('maxProfit', 0)
    max_l = cand.get('maxLoss', 0)
    est_cost = cand.get('estCost', 0)
    est_cost_pct = cand.get('estCostPct', 0)
    realistic_mp = cand.get('realisticMaxProfit')
    prob = cand.get('probProfit', 0)
    forces = cand.get('forces') or {}
    ctx_score = cand.get('contextScore', 0)
    
    # 1. COST TRAP — est. cost eats too much of realistic profit
    effective_max = realistic_mp if realistic_mp else max_p
    if effective_max > 0 and est_cost > 0:
        cost_ratio = est_cost / effective_max
        if cost_ratio > 0.30:
            net = effective_max - est_cost
            insights.append({"icon": "💸", "label": f"Cost trap ({cost_ratio*100:.0f}% of profit)",
                    "detail": f"Net after cost: ₹{net:.0f}. Risk ₹{max_l:.0f} for ₹{net:.0f}.",
                    "impact": "caution", "strength": 5 if cost_ratio > 0.5 else 4})
    
    # 2. R:R SANITY — maxLoss > 2× maxProfit is dangerous
    if max_p > 0 and max_l > 0:
        rr = max_p / max_l
        if rr < 0.5 and prob < 0.85:
            insights.append({"icon": "⚖️", "label": f"Poor R:R (1:{1/rr:.1f})",
                    "detail": f"Risk ₹{max_l:.0f} to make ₹{max_p:.0f}. Need {1/(rr+0.001):.0f}x wins per loss.",
                    "impact": "caution", "strength": 3})
    
    # 3. OPEN TRADE CONFLICT — already have a struggling position in same type/index?
    for t in (open_trades or []):
        if t.get('index_key') != idx: continue
        if t.get('paper'): continue
        t_type = t.get('strategy_type', '')
        t_pnl = t.get('current_pnl', 0)
        t_ci = t.get('controlIndex')
        # Same strategy type and struggling
        if t_type == ctype and (t_pnl < 0 or (t_ci is not None and t_ci < -20)):
            insights.append({"icon": "🔄", "label": f"Open {t_type} struggling",
                    "detail": f"Existing {idx} {t_type} at P&L ₹{t_pnl:.0f}, CI {t_ci}. Don't double down.",
                    "impact": "caution", "strength": 4})
            break
        # Any open real trade in same index (overexposure)
        if not t.get('paper') and t.get('index_key') == idx:
            insights.append({"icon": "📋", "label": f"Already in {idx}",
                    "detail": f"Open {t_type} in {idx}. Adding = double exposure.",
                    "impact": "neutral", "strength": 2})
            break
    
    # 4. FORCE COHERENCE — forces say one thing, context says another
    aligned = forces.get('aligned', 0)
    if aligned >= 3 and ctx_score < -0.3:
        insights.append({"icon": "⚠️", "label": "Forces aligned but context negative",
                "detail": f"3/3 forces but contextScore {ctx_score:.2f}. Gap/VIX conflict?",
                "impact": "caution", "strength": 3})
    
    # 5. WIDTH ADEQUACY — narrow widths at high VIX = stop loss hunting
    width = cand.get('width', 0)
    profile = ctx.get('bnfProfile' if idx == 'BNF' else 'nfProfile') or {}
    if width and width > 0:
        min_w = 400 if idx == 'BNF' else 200
        if cand.get('isCredit') and width < min_w:
            insights.append({"icon": "📏", "label": f"Narrow width ({width})",
                    "detail": f"Width {width} < recommended {min_w}. Stop-loss hunting risk.",
                    "impact": "caution", "strength": 2})
    
    # 6. THETA-TO-FRICTION — b93: how long to break even on costs?
    net_theta = cand.get('netTheta', 0)
    if not net_theta or net_theta <= 0:
        net_theta = max_p * 0.01  # fallback rough estimate only if no real theta
    be_mins = theta_friction_minutes(est_cost, net_theta)
    if be_mins > 120 and cand.get('isCredit'):
        insights.append({"icon": "⏳", "label": f"Slow payback ({be_mins:.0f}min to break even)",
                "detail": f"Cost ₹{est_cost:.0f} takes {be_mins:.0f}min of theta to recover. Trade may be dead.",
                "impact": "caution", "strength": 4 if be_mins > 180 else 3})
    
    return insights

# ═══════════════════════════════════════════
# PART 4: TIMING (shown in Market + Trade tabs)
# ═══════════════════════════════════════════

def timing_entry_window(polls, baseline, regime):
    """Is the sweet spot window open?"""
    if not polls: return None
    mins = get_time_mins(polls[-1].get('t', ''))
    if mins == 0: return None
    # Convert to minutes since 9:15
    market_mins = mins - 555
    if 135 <= market_mins <= 315:  # 11:30 to 14:30
        return {"type": "timing", "icon": "🟢", "label": "Sweet spot window OPEN",
                "detail": f"11:30–14:30 zone. Best entries. Noise settled, thesis clear.",
                "impact": "bullish", "strength": 2}
    elif market_mins < 15:
        return {"type": "timing", "icon": "🔇", "label": "Opening noise — wait",
                "detail": f"First 15min. Gap-driven volatility. Don't enter.",
                "impact": "caution", "strength": 3}
    elif market_mins > 345:
        return {"type": "timing", "icon": "🔒", "label": "Last entry window closed",
                "detail": f"After 3:00 PM. No new entries. Manage existing positions only.",
                "impact": "neutral", "strength": 2}
    elif market_mins > 315:
        return {"type": "timing", "icon": "🏛️", "label": "Institutional positioning window",
                "detail": f"2:30–3:15. Watch for tomorrow signal. Position if signal strong.",
                "impact": "neutral", "strength": 2}
    return None

def timing_wait_signal(polls, baseline, regime):
    """Should the trader wait before entering?"""
    if not polls: return None
    rtype = regime.get('type', 'unknown')
    # Momentum accelerating — don't chase
    recent = polls[-6:] if len(polls) >= 6 else polls
    spots = [p.get('bnf') for p in recent if p.get('bnf')]
    if len(spots) >= 4:
        mid = len(spots) // 2
        m1 = sum(abs(spots[i] - spots[i-1]) for i in range(1, mid)) / max(1, mid-1) if mid > 1 else 0
        m2 = sum(abs(spots[i] - spots[i-1]) for i in range(mid+1, len(spots))) / max(1, len(spots)-mid-1) if len(spots) > mid+1 else 0
        if m1 > 0 and m2 / m1 > 2 and m2 > 50:
            return {"type": "timing", "icon": "⏳", "label": "Wait — momentum accelerating",
                    "detail": f"Moves growing ({m1:.0f} → {m2:.0f} pts/poll). Enter on exhaustion, not chase.",
                    "impact": "caution", "strength": 3}
    return None

# ═══════════════════════════════════════════
# PART 5: RISK (portfolio-level)
# ═══════════════════════════════════════════

def risk_kelly_headroom(polls, baseline, open_trades, closed_trades):
    """Kelly % vs current exposure."""
    try:
        if len(closed_trades) < 5: return None
        wins = [t for t in closed_trades if (t.get('actual_pnl') or 0) > 0]
        losses = [t for t in closed_trades if (t.get('actual_pnl') or 0) <= 0]
        w = len(wins) / len(closed_trades)
        
        avg_w = sum(t.get('actual_pnl', 0) or 0 for t in wins) / len(wins) if wins else 0
        avg_l = abs(sum(t.get('actual_pnl', 0) or 0 for t in losses) / len(losses)) if losses else 1
        
        r = avg_w / avg_l if avg_l > 0 else 1
        kelly = max(0, w - ((1 - w) / r)) if r > 0 else 0
        kelly_pct = kelly * 100
        capital = _capital  # set from context in analyze()
        optimal = kelly * capital
        current_exposure = sum(abs(t.get('max_loss', 0) or 0) for t in open_trades if not t.get('paper'))
        headroom = optimal - current_exposure
        if headroom > 5000:
            return {"type": "risk", "icon": "🎰", "label": f"Kelly {kelly_pct:.0f}% — room for entry",
                    "detail": f"Optimal: ₹{optimal:.0f}. Used: ₹{current_exposure:.0f}. Headroom: ₹{headroom:.0f}.",
                    "impact": "neutral", "strength": 2}
        elif headroom < 0:
            return {"type": "risk", "icon": "🎰", "label": f"Kelly {kelly_pct:.0f}% — overexposed",
                    "detail": f"Optimal: ₹{optimal:.0f}. Used: ₹{current_exposure:.0f}. Over by ₹{abs(headroom):.0f}.",
                    "impact": "caution", "strength": 4}
    except Exception:
        pass
    return None

def risk_regime_shift(polls, baseline, open_trades, closed_trades):
    """VIX crossed a regime threshold mid-session."""
    vixs = get_vix_vals(polls)
    if len(vixs) < 3: return None
    morning_vix = vixs[0]
    curr_vix = vixs[-1]
    # Check for regime boundary crossings: 15, 20, 24
    for threshold in [24, 20, 15]:
        if (morning_vix < threshold and curr_vix >= threshold) or (morning_vix >= threshold and curr_vix < threshold):
            crossed_up = curr_vix >= threshold
            has_credit = any(t.get('is_credit') for t in open_trades if not t.get('paper'))
            has_debit = any(not t.get('is_credit') for t in open_trades if not t.get('paper'))
            label = f"VIX crossed {threshold} {'↑' if crossed_up else '↓'}"
            detail = f"Morning {morning_vix:.1f} → now {curr_vix:.1f}."
            if threshold == 24 and crossed_up and has_credit:
                detail += " Backtest: debit > credit at VIX≥24. Open credit trades face headwind."
                return {"type": "risk", "icon": "🔥", "label": label, "detail": detail, "impact": "caution", "strength": 5}
            elif threshold == 24 and not crossed_up:
                detail += " VIX normalizing. Credit strategies favored."
                return {"type": "risk", "icon": "📉", "label": label, "detail": detail, "impact": "bullish", "strength": 3}
            else:
                detail += " Regime boundary crossed — review strategy alignment."
                return {"type": "risk", "icon": "⚡", "label": label, "detail": detail, "impact": "caution", "strength": 3}
    return None

# ═══════════════════════════════════════════
# PART 6: LEARNING — builds knowledge from YOUR trade history
# Cached: recomputes only when trade count changes
# ═══════════════════════════════════════════

_calibration = None
_cal_count = 0
_capital = 110000

def build_calibration(closed_trades):
    global _calibration, _cal_count
    trades = [t for t in closed_trades if t.get('status') == 'CLOSED' and t.get('actual_pnl') is not None]
    if len(trades) == _cal_count and _calibration:
        return _calibration
    _cal_count = len(trades)
    if len(trades) < 5:
        _calibration = None
        return None

    cal = {}

    # 1. Strategy win rates
    cal['strategy'] = {}
    for t in trades:
        st = t.get('strategy_type', 'UNKNOWN')
        if st not in cal['strategy']:
            cal['strategy'][st] = {'wins': 0, 'total': 0, 'pnls': []}
        cal['strategy'][st]['total'] += 1
        if t['actual_pnl'] > 0:
            cal['strategy'][st]['wins'] += 1
        cal['strategy'][st]['pnls'].append(t['actual_pnl'])
    for st in cal['strategy']:
        s = cal['strategy'][st]
        s['rate'] = s['wins'] / s['total'] if s['total'] > 0 else 0
        s['avg_pnl'] = sum(s['pnls']) / len(s['pnls']) if s['pnls'] else 0

    # 2. VIX regime rates
    cal['vix_regime'] = {}
    for t in trades:
        vix = t.get('entry_vix') or 20
        regime = 'VH' if vix >= 24 else 'H' if vix >= 20 else 'N' if vix >= 16 else 'L'
        if regime not in cal['vix_regime']:
            cal['vix_regime'][regime] = {'wins': 0, 'total': 0}
        cal['vix_regime'][regime]['total'] += 1
        if (t.get('actual_pnl') or 0) > 0:
            cal['vix_regime'][regime]['wins'] += 1
    for r in cal['vix_regime']:
        s = cal['vix_regime'][r]
        s['rate'] = s['wins'] / s['total'] if s['total'] > 0 else 0

    # 3. Credit vs debit
    cal['side'] = {'credit': {'wins': 0, 'total': 0}, 'debit': {'wins': 0, 'total': 0}}
    for t in trades:
        key = 'credit' if t.get('is_credit') else 'debit'
        cal['side'][key]['total'] += 1
        if (t.get('actual_pnl') or 0) > 0:
            cal['side'][key]['wins'] += 1
    for k in cal['side']:
        s = cal['side'][k]
        s['rate'] = s['wins'] / s['total'] if s['total'] > 0 else 0

    # 4. Multi-factor: strategy + VIX regime
    cal['multi'] = {}
    for t in trades:
        st = t.get('strategy_type', 'UNKNOWN')
        vix = t.get('entry_vix') or 20
        regime = 'VH' if vix >= 24 else 'H' if vix >= 20 else 'N' if vix >= 16 else 'L'
        key = f"{st}|{regime}"
        if key not in cal['multi']:
            cal['multi'][key] = {'wins': 0, 'total': 0, 'pnls': []}
        cal['multi'][key]['total'] += 1
        if (t.get('actual_pnl') or 0) > 0:
            cal['multi'][key]['wins'] += 1
        cal['multi'][key]['pnls'].append(t.get('actual_pnl', 0))
    for k in cal['multi']:
        s = cal['multi'][k]
        s['rate'] = s['wins'] / s['total'] if s['total'] > 0 else 0
        s['avg_pnl'] = sum(s['pnls']) / len(s['pnls']) if s['pnls'] else 0

    # 5. Force alignment impact
    cal['forces'] = {}
    for fname in ['force_f1', 'force_f2', 'force_f3']:
        pos = {'wins': 0, 'total': 0}
        neg = {'wins': 0, 'total': 0}
        for t in trades:
            fval = t.get(fname, 0)
            bucket = pos if fval and fval > 0 else neg
            bucket['total'] += 1
            if (t.get('actual_pnl') or 0) > 0:
                bucket['wins'] += 1
        pr = pos['wins'] / pos['total'] if pos['total'] > 0 else 0
        nr = neg['wins'] / neg['total'] if neg['total'] > 0 else 0
        cal['forces'][fname] = {'pos_rate': pr, 'neg_rate': nr, 'spread': pr - nr, 'n': pos['total'] + neg['total']}

    # 6. Exit analysis — are you capturing peak profit?
    winners = [t for t in trades if (t.get('actual_pnl') or 0) > 0 and t.get('peak_pnl')]
    if len(winners) >= 3:
        peaks = [t['peak_pnl'] for t in winners]
        exits = [t['actual_pnl'] for t in winners]
        cal['exit'] = {
            'avg_peak': sum(peaks) / len(peaks),
            'avg_exit': sum(exits) / len(exits),
            'capture_pct': sum(exits) / sum(peaks) * 100 if sum(peaks) > 0 else 0,
            'left_on_table': sum(p - e for p, e in zip(peaks, exits)) / len(peaks),
            'n': len(winners)
        }
    else:
        cal['exit'] = None

    # 7. Consecutive losses — max streak
    streak = 0
    max_streak = 0
    for t in sorted(trades, key=lambda x: x.get('exit_date', '')):
        if (t.get('actual_pnl') or 0) <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    cal['max_loss_streak'] = max_streak

    # 8. Trade mode rates
    cal['mode'] = {}
    for t in trades:
        mode = t.get('trade_mode', 'unknown')
        if mode not in cal['mode']:
            cal['mode'][mode] = {'wins': 0, 'total': 0}
        cal['mode'][mode]['total'] += 1
        if (t.get('actual_pnl') or 0) > 0:
            cal['mode'][mode]['wins'] += 1
    for m in cal['mode']:
        s = cal['mode'][m]
        s['rate'] = s['wins'] / s['total'] if s['total'] > 0 else 0

    cal['total_trades'] = len(trades)

    # ═══ b92: LEARNING — what made trades WIN or LOSE? ═══

    # 9. Wall protection correlation — were wall-backed trades more successful?
    cal['wall'] = {'backed': {'wins': 0, 'total': 0}, 'exposed': {'wins': 0, 'total': 0}}
    for t in trades:
        snap = t.get('entry_snapshot') or {}
        sell = t.get('sell_strike', 0)
        stype = t.get('strategy_type', '')
        cw = snap.get('call_wall')
        pw = snap.get('put_wall')
        ws = snap.get('wall_score', 0)
        # Determine if wall-backed at entry
        backed = False
        if 'BEAR' in stype and cw and sell and cw >= sell:
            backed = True
        elif 'BULL' in stype and 'CALL' not in stype and pw and sell and pw <= sell:
            backed = True
        elif stype in ('IRON_CONDOR', 'IRON_BUTTERFLY') and ws and ws > 0:
            backed = True
        elif ws and ws > 0:
            backed = True
        bucket = cal['wall']['backed'] if backed else cal['wall']['exposed']
        bucket['total'] += 1
        if (t.get('actual_pnl') or 0) > 0:
            bucket['wins'] += 1
    for k in cal['wall']:
        s = cal['wall'][k]
        s['rate'] = s['wins'] / s['total'] if s['total'] > 0 else 0

    # 10. Multi-factor: strategy + VIX + wall protection
    for t in trades:
        st = t.get('strategy_type', 'UNKNOWN')
        vix = t.get('entry_vix') or 20
        regime = 'VH' if vix >= 24 else 'H' if vix >= 20 else 'N' if vix >= 16 else 'L'
        snap = t.get('entry_snapshot') or {}
        ws = snap.get('wall_score', 0)
        wall_key = 'wall' if ws and ws > 0 else 'nowall'
        key = f"{st}|{regime}|{wall_key}"
        if key not in cal['multi']:
            cal['multi'][key] = {'wins': 0, 'total': 0, 'pnls': []}
        cal['multi'][key]['total'] += 1
        if (t.get('actual_pnl') or 0) > 0:
            cal['multi'][key]['wins'] += 1
        cal['multi'][key]['pnls'].append(t.get('actual_pnl', 0))
    # Recompute rates for new multi keys
    for k in cal['multi']:
        s = cal['multi'][k]
        s['rate'] = s['wins'] / s['total'] if s['total'] > 0 else 0
        s['avg_pnl'] = sum(s['pnls']) / len(s['pnls']) if s['pnls'] else 0

    # 11. Exit reason patterns — why do you close trades?
    cal['exit_reasons'] = {}
    for t in trades:
        reason = t.get('exit_reason', 'unknown') or 'unknown'
        if reason not in cal['exit_reasons']:
            cal['exit_reasons'][reason] = {'wins': 0, 'total': 0, 'avg_pnl': 0, 'pnls': []}
        cal['exit_reasons'][reason]['total'] += 1
        pnl = t.get('actual_pnl', 0)
        if pnl > 0: cal['exit_reasons'][reason]['wins'] += 1
        cal['exit_reasons'][reason]['pnls'].append(pnl)
    for k in cal['exit_reasons']:
        s = cal['exit_reasons'][k]
        s['rate'] = s['wins'] / s['total'] if s['total'] > 0 else 0
        s['avg_pnl'] = sum(s['pnls']) / len(s['pnls']) if s['pnls'] else 0

    # ═══ b93: 4 NEW CALIBRATION DIMENSIONS ═══

    # 12. Time-of-day win rates — morning vs afternoon entries
    cal['time_of_day'] = {}
    for t in trades:
        entry = t.get('entry_date', '')
        try:
            hour = int(entry.split('T')[1].split(':')[0]) if 'T' in entry else 0
        except: hour = 0
        bucket = 'morning' if hour < 12 else 'afternoon' if hour < 15 else 'late'
        if bucket not in cal['time_of_day']:
            cal['time_of_day'][bucket] = {'wins': 0, 'total': 0, 'pnls': []}
        cal['time_of_day'][bucket]['total'] += 1
        if (t.get('actual_pnl') or 0) > 0:
            cal['time_of_day'][bucket]['wins'] += 1
        cal['time_of_day'][bucket]['pnls'].append(t.get('actual_pnl', 0))
    for k in cal['time_of_day']:
        s = cal['time_of_day'][k]
        s['rate'] = s['wins'] / s['total'] if s['total'] > 0 else 0
        s['avg_pnl'] = sum(s['pnls']) / len(s['pnls']) if s['pnls'] else 0

    # 13. Width bucket win rates — which widths actually perform?
    cal['width'] = {}
    for t in trades:
        w = t.get('width', 0)
        if not w: continue
        bucket = f"W{w}"
        if bucket not in cal['width']:
            cal['width'][bucket] = {'wins': 0, 'total': 0, 'pnls': []}
        cal['width'][bucket]['total'] += 1
        if (t.get('actual_pnl') or 0) > 0:
            cal['width'][bucket]['wins'] += 1
        cal['width'][bucket]['pnls'].append(t.get('actual_pnl', 0))
    for k in cal['width']:
        s = cal['width'][k]
        s['rate'] = s['wins'] / s['total'] if s['total'] > 0 else 0
        s['avg_pnl'] = sum(s['pnls']) / len(s['pnls']) if s['pnls'] else 0

    # 14. VIX change during trade — did vol crush or expand while holding?
    cal['vix_change'] = {'crush': {'wins': 0, 'total': 0}, 'expand': {'wins': 0, 'total': 0}, 'flat': {'wins': 0, 'total': 0}}
    for t in trades:
        entry_v = t.get('entry_vix')
        snap = t.get('exit_snapshot') or {}
        exit_v = snap.get('vix') or snap.get('exit_vix')
        if entry_v and exit_v:
            diff = exit_v - entry_v
            bucket = 'crush' if diff < -0.5 else 'expand' if diff > 0.5 else 'flat'
            cal['vix_change'][bucket]['total'] += 1
            if (t.get('actual_pnl') or 0) > 0:
                cal['vix_change'][bucket]['wins'] += 1
    for k in cal['vix_change']:
        s = cal['vix_change'][k]
        s['rate'] = s['wins'] / s['total'] if s['total'] > 0 else 0

    # 15. Sigma OTM at entry — does distance from ATM predict success?
    cal['sigma_otm'] = {}
    for t in trades:
        snap = t.get('entry_snapshot') or {}
        sigma = snap.get('sigma_otm') or snap.get('sigmaOTM')
        if sigma is not None:
            bucket = 'close' if sigma < 0.4 else 'sweet' if sigma <= 0.8 else 'far'
            if bucket not in cal['sigma_otm']:
                cal['sigma_otm'][bucket] = {'wins': 0, 'total': 0, 'pnls': []}
            cal['sigma_otm'][bucket]['total'] += 1
            if (t.get('actual_pnl') or 0) > 0:
                cal['sigma_otm'][bucket]['wins'] += 1
            cal['sigma_otm'][bucket]['pnls'].append(t.get('actual_pnl', 0))
    for k in cal['sigma_otm']:
        s = cal['sigma_otm'][k]
        s['rate'] = s['wins'] / s['total'] if s['total'] > 0 else 0
        s['avg_pnl'] = sum(s['pnls']) / len(s['pnls']) if s['pnls'] else 0

    _calibration = cal
    return cal

def candidate_pattern_match(cand, polls, baseline, regime):
    """b92: Score candidate from YOUR trade history in similar conditions.
    Now uses 3-factor key: strategy + VIX + wall protection."""
    if not _calibration:
        return None
    ctype = cand.get('type', '')
    # Current VIX regime
    vixs = [p.get('vix') for p in polls[-3:] if p.get('vix')]
    vix = vixs[-1] if vixs else 20
    vr = 'VH' if vix >= 24 else 'H' if vix >= 20 else 'N' if vix >= 16 else 'L'
    # b92: Wall status from enriched candidate data
    wall_key = 'wall' if (cand.get('wallScore') or 0) > 0 else 'nowall'
    # Try 3-factor key first: strategy + VIX + wall
    key3 = f"{ctype}|{vr}|{wall_key}"
    match3 = _calibration.get('multi', {}).get(key3)
    if match3 and match3['total'] >= 3:
        rate = match3['rate']
        wall_label = "wall-backed" if wall_key == 'wall' else "unprotected"
        return {"icon": "📊", "label": f"Your data: {match3['wins']}/{match3['total']} ({rate*100:.0f}%)",
                "detail": f"{ctype} at {vr} VIX, {wall_label}. Avg P&L ₹{match3['avg_pnl']:.0f}.",
                "impact": "bullish" if rate >= 0.6 else "caution" if rate < 0.4 else "neutral",
                "strength": 4 if match3['total'] >= 5 else 3}
    # Fall back to 2-factor: strategy + VIX
    key2 = f"{ctype}|{vr}"
    match2 = _calibration.get('multi', {}).get(key2)
    if match2 and match2['total'] >= 3:
        rate = match2['rate']
        return {"icon": "📊", "label": f"Your data: {match2['wins']}/{match2['total']} ({rate*100:.0f}%)",
                "detail": f"{ctype} at {vr} VIX. Avg P&L ₹{match2['avg_pnl']:.0f}.",
                "impact": "bullish" if rate >= 0.6 else "caution" if rate < 0.4 else "neutral",
                "strength": 4 if match2['total'] >= 5 else 3}
    # Fall back to strategy-only
    strat = _calibration.get('strategy', {}).get(ctype)
    if strat and strat['total'] >= 2:
        rate = strat['rate']
        return {"icon": "📊", "label": f"Your {ctype}: {strat['wins']}/{strat['total']} ({rate*100:.0f}%)",
                "detail": f"Avg P&L ₹{strat['avg_pnl']:.0f}. {'Edge confirmed.' if rate > 0.6 else 'Needs more data.' if rate >= 0.4 else 'Below 40% — paper first.'}",
                "impact": "bullish" if rate >= 0.6 else "caution" if rate < 0.4 else "neutral",
                "strength": 3 if strat['total'] >= 5 else 2}
    # b92: Wall protection aggregate insight
    wall_cal = _calibration.get('wall', {})
    if wall_cal.get('backed', {}).get('total', 0) >= 3 and wall_cal.get('exposed', {}).get('total', 0) >= 3:
        b_rate = wall_cal['backed']['rate']
        e_rate = wall_cal['exposed']['rate']
        if abs(b_rate - e_rate) > 0.15:
            better = "wall-backed" if b_rate > e_rate else "unprotected"
            return {"icon": "📊", "label": f"Wall data: {'backed' if wall_key == 'wall' else 'exposed'}",
                    "detail": f"Backed: {b_rate*100:.0f}% win. Exposed: {e_rate*100:.0f}% win. {better} performs better.",
                    "impact": "bullish" if (wall_key == 'wall' and b_rate > e_rate) or (wall_key == 'nowall' and e_rate > b_rate) else "caution",
                    "strength": 3}
    # Never traded this type
    if ctype not in _calibration.get('strategy', {}):
        return {"icon": "🆕", "label": f"No history for {ctype}",
                "detail": "First time. Consider paper trade.", "impact": "caution", "strength": 2}
    return None

def risk_exit_analysis(polls, baseline, open_trades, closed_trades):
    if not _calibration or not _calibration.get('exit'):
        return None
    ex = _calibration['exit']
    cap = ex['capture_pct']
    if cap < 60:
        return {"type": "risk", "icon": "💸", "label": f"Capturing only {cap:.0f}% of peaks",
                "detail": f"Avg peak ₹{ex['avg_peak']:.0f} → exit ₹{ex['avg_exit']:.0f}. Book at 50% more often.",
                "impact": "caution", "strength": 3}
    elif cap > 80:
        return {"type": "risk", "icon": "🎯", "label": f"Exit discipline: {cap:.0f}% captured",
                "detail": f"Strong execution. Avg ₹{ex['left_on_table']:.0f} left per trade.", "impact": "bullish", "strength": 2}
    return None

def risk_factor_importance(polls, baseline, open_trades, closed_trades):
    if not _calibration or not _calibration.get('forces'):
        return None
    best_name, best_spread = None, 0
    for fname, fdata in _calibration['forces'].items():
        if fdata['n'] >= 10 and fdata['spread'] > best_spread:
            best_name, best_spread = fname, fdata['spread']
    if best_name and best_spread > 0.15:
        nice = {'force_f1': 'Direction (F1)', 'force_f2': 'Theta (F2)', 'force_f3': 'IV (F3)'}
        return {"type": "risk", "icon": "🔑", "label": f"{nice.get(best_name, best_name)} is your edge",
                "detail": f"Win rate +{best_spread*100:.0f}% when aligned. Most predictive force.",
                "impact": "neutral", "strength": 3}
    return None

def risk_streak_warning(polls, baseline, open_trades, closed_trades):
    if not _calibration:
        return None
    streak = _calibration.get('max_loss_streak', 0)
    if streak >= 3:
        return {"type": "risk", "icon": "📉", "label": f"Max losing streak: {streak}",
                "detail": f"Worst run was {streak} consecutive losses. Size accordingly.",
                "impact": "caution", "strength": 3 if streak >= 4 else 2}
    return None

# ═══════════════════════════════════════════
# PART 7: SYNTHESIS — ONE answer, not 14 whispers
# Uses ALL signals + context + calibration
# ═══════════════════════════════════════════

def signal_coherence(polls, ctx):
    """Are VIX, spot, breadth telling the same story?"""
    vixs = get_vix_vals(polls)
    spots = [p.get('bnf') for p in last_n(polls, 4) if p.get('bnf')]
    if len(vixs) < 3 or len(spots) < 3: return None
    vix_dir = 1 if vixs[-1] > vixs[0] + 0.3 else -1 if vixs[-1] < vixs[0] - 0.3 else 0
    spot_dir = 1 if spots[-1] > spots[0] + 30 else -1 if spots[-1] < spots[0] - 30 else 0
    breadth = ctx.get('bnfBreadth') or {}
    b_dir = 1 if breadth.get('pct', 50) > 60 else -1 if breadth.get('pct', 50) < 40 else 0
    # Normal: VIX opposes spot. Abnormal: same direction
    vix_spot_coherent = (vix_dir * spot_dir) <= 0  # opposite or flat = coherent
    breadth_spot_coherent = (b_dir == spot_dir) or b_dir == 0 or spot_dir == 0
    if not vix_spot_coherent:
        return {"type": "coherence", "icon": "⚠️", "label": "VIX-Spot divergence",
                "detail": f"VIX {'rising' if vix_dir>0 else 'falling'} WITH spot {'rising' if spot_dir>0 else 'falling'}. Unusual — proceed with caution.",
                "impact": "caution", "strength": 4}
    if not breadth_spot_coherent:
        return {"type": "coherence", "icon": "⚠️", "label": f"Narrow {'rally' if spot_dir>0 else 'decline'}",
                "detail": f"Spot moving {'up' if spot_dir>0 else 'down'} but breadth {'bearish' if b_dir<0 else 'neutral'}. Move may reverse.",
                "impact": "caution", "strength": 3}
    if vix_spot_coherent and breadth_spot_coherent and spot_dir != 0:
        return {"type": "coherence", "icon": "✅", "label": "Signals aligned",
                "detail": f"VIX, spot, breadth all consistent. Move is real.", "impact": "bullish" if spot_dir > 0 else "bearish", "strength": 3}
    return None

def max_pain_gravity(polls, ctx):
    """Max pain as magnet — strongest on DTE 0-1."""
    dte = ctx.get('bnfDTE', 5)
    profile = ctx.get('bnfProfile') or {}
    mp = profile.get('maxPain')
    spot = profile.get('spot')
    if not mp or not spot: return None
    dist = spot - mp
    if dte <= 1 and abs(dist) > 50:
        d = "DOWN" if dist > 0 else "UP"
        return {"type": "maxpain", "icon": "🧲", "label": f"Max pain pull {d} ({abs(dist):.0f}pts)",
                "detail": f"DTE {dte}. Spot {spot:.0f}, max pain {mp:.0f}. Expiry day magnet.",
                "impact": "bearish" if dist > 0 else "bullish", "strength": 4}
    elif dte <= 3 and abs(dist) > 100:
        return {"type": "maxpain", "icon": "🧲", "label": f"Max pain at {mp:.0f} ({abs(dist):.0f}pts away)",
                "detail": f"DTE {dte}. Gravitational pull building.", "impact": "neutral", "strength": 2}
    return None

def fii_trend(polls, ctx):
    """5-day FII trend from premiumHistory."""
    hist = ctx.get('fiiHistory', [])
    if len(hist) < 3: return None
    fii_vals = []
    for h in hist:
        v = h.get('fiiCash')
        if v is not None:
            try: fii_vals.append(float(v))
            except (ValueError, TypeError): pass
    if len(fii_vals) < 3: return None
    total = sum(fii_vals)
    avg = total / len(fii_vals)
    if total < -3000:
        return {"type": "fii", "icon": "🏦", "label": f"FII selling {len(fii_vals)} days (₹{total:.0f}Cr)",
                "detail": f"Sustained institutional selling. Bearish conviction.", "impact": "bearish", "strength": 4}
    elif total > 3000:
        return {"type": "fii", "icon": "🏦", "label": f"FII buying {len(fii_vals)} days (₹{total:.0f}Cr)",
                "detail": f"Sustained institutional buying. Bullish conviction.", "impact": "bullish", "strength": 4}
    elif total < -1000:
        return {"type": "fii", "icon": "🏦", "label": f"FII net sellers (₹{total:.0f}Cr/{len(fii_vals)}d)",
                "detail": f"Mild selling pressure.", "impact": "bearish", "strength": 2}
    return None

def nf_bnf_divergence(polls, ctx):
    """NF and BNF moving in different directions?"""
    bnf_pct = (ctx.get('bnfProfile') or {}).get('pctFromOpen', 0)
    nf_pct = (ctx.get('nfProfile') or {}).get('pctFromOpen', 0)
    if abs(bnf_pct - nf_pct) > 0.3:
        leader = "BNF" if abs(bnf_pct) > abs(nf_pct) else "NF"
        return {"type": "diverge", "icon": "↔️", "label": f"NF-BNF divergence",
                "detail": f"BNF {bnf_pct:+.1f}% vs NF {nf_pct:+.1f}%. {leader} leading. Watch for convergence.",
                "impact": "caution", "strength": 2}
    return None

def day_range_position(polls, ctx):
    """Where in today's range — near high (caution for bears) or low?"""
    profile = ctx.get('bnfProfile') or {}
    pos = profile.get('dayRange', 0.5)
    if pos > 0.85:
        return {"type": "range_pos", "icon": "📍", "label": "At day HIGH",
                "detail": f"BNF at {pos*100:.0f}% of day range. Breakout or reversal zone.",
                "impact": "caution", "strength": 2}
    elif pos < 0.15:
        return {"type": "range_pos", "icon": "📍", "label": "At day LOW",
                "detail": f"BNF at {pos*100:.0f}% of day range. Bounce or breakdown zone.",
                "impact": "caution", "strength": 2}
    return None

def wall_freshness(polls, ctx):
    """Are OI walls actively defended today or stale from yesterday?"""
    profile = ctx.get('bnfProfile') or {}
    cwF = profile.get('cwFresh', 0)
    pwF = profile.get('pwFresh', 0)
    cwChg = profile.get('cwOiChg')
    insights = []
    if cwF > 0.25 and cwChg and cwChg > 0:
        insights.append({"type": "fresh", "icon": "🏗️", "label": "Call wall FRESH — actively built today",
                "detail": f"Volume/OI ratio {cwF:.1%}. +{cwChg:,.0f} new OI. Resistance is real.",
                "impact": "bearish", "strength": 3})
    elif cwF < 0.05 and cwChg is not None and cwChg <= 0:
        insights.append({"type": "fresh", "icon": "💨", "label": "Call wall STALE — no fresh defense",
                "detail": f"Volume/OI {cwF:.1%}. May not hold if tested.", "impact": "caution", "strength": 2})
    return insights[0] if insights else None

def yesterday_signal_prior(polls, ctx):
    """Yesterday's positioning signal as morning prior."""
    sig = ctx.get('yesterdaySignal')
    acc = ctx.get('signalAccuracy')
    if not sig: return None
    pct = acc.get('pct', 0) if acc else 0
    return {"type": "prior", "icon": "📡", "label": f"Yesterday: {sig['signal']} ({sig['strength']}/5)",
            "detail": f"Signal accuracy: {pct}% over {acc.get('total',0) if acc else 0} signals.",
            "impact": "bearish" if sig['signal']=='BEARISH' else "bullish" if sig['signal']=='BULLISH' else "neutral",
            "strength": 2 if pct < 60 else 3}

def dte_urgency(polls, ctx):
    """DTE-aware urgency for timing."""
    dte = ctx.get('bnfDTE', 5)
    if dte <= 1:
        return {"type": "timing", "icon": "⏰", "label": "EXPIRY DAY — theta maximum",
                "detail": "Credit sellers: theta melting fastest. Debit buyers: theta death zone. IB/IC exit by 3PM.",
                "impact": "neutral", "strength": 4}
    elif dte == 2:
        return {"type": "timing", "icon": "⏰", "label": "DTE 2 — theta accelerating",
                "detail": "Credit favored. Debit positions lose value rapidly.", "impact": "neutral", "strength": 2}
    return None

def compute_effective_bias(polls, baseline, ctx, regime):
    """b97: Bayesian effective bias — morning prior decays as intraday evidence accumulates.
    Morning data = where we came from (context). Intraday polls = what's happening now.
    By sweet spot (11 AM), intraday dominates 80%. Morning never disappears (20% floor).
    Returns: {bias, strength, net, morning_weight, signals, drift_reasons}"""
    
    morning_bias = ctx.get('morningBias') or {}
    morning_net = morning_bias.get('net', 0)
    poll_count = len(polls)
    TOTAL_SIGNALS = 7
    
    # ═══ MORNING WEIGHT DECAY ═══
    # 100% at poll 0, decays 5%/poll, floor 20%. Sweet spot ~poll 16-20.
    morning_weight = max(0.20, 1.0 - poll_count * 0.05)
    intraday_weight = 1.0 - morning_weight
    
    # ═══ FIRST 15 MINUTES SUPPRESSION ═══
    # Opening noise — gap repricing, market maker activity. Signals unreliable.
    if poll_count < 3:
        return {
            'bias': 'BULL' if morning_net >= 1 else 'BEAR' if morning_net <= -1 else 'NEUTRAL',
            'strength': 'STRONG' if abs(morning_net) >= 2 else 'MILD' if abs(morning_net) >= 1 else '',
            'net': morning_net,
            'morning_weight': 1.0,
            'signals': [0] * TOTAL_SIGNALS,
            'drift_reasons': ['Too early — morning dominant']
        }
    
    # ═══ 7 INTRADAY SIGNALS (each -2/-1/0/+1/+2) ═══
    signals = []
    drift_reasons = []
    last = polls[-1] if polls else {}
    first = polls[0] if polls else {}
    
    # --- 1. Spot σ from morning (3-poll smoothed) ---
    base_spot = baseline.get('bnfSpot', 0)
    base_vix = baseline.get('vix', 18)
    daily_sigma = base_spot * (base_vix / 100) / math.sqrt(252) if base_spot > 0 else 300
    recent_spots = [p.get('bnf') for p in polls[-4:] if p.get('bnf')]
    if len(recent_spots) >= 3 and daily_sigma > 0:
        spot_avg = sum(recent_spots[-3:]) / 3  # 3-poll smoothed
        spot_move_sigma = (spot_avg - base_spot) / daily_sigma
        if spot_move_sigma > 0.8: signals.append(2); drift_reasons.append(f"Spot +{spot_move_sigma:.1f}σ")
        elif spot_move_sigma > 0.3: signals.append(1); drift_reasons.append(f"Spot +{spot_move_sigma:.1f}σ")
        elif spot_move_sigma < -0.8: signals.append(-2); drift_reasons.append(f"Spot {spot_move_sigma:.1f}σ")
        elif spot_move_sigma < -0.3: signals.append(-1); drift_reasons.append(f"Spot {spot_move_sigma:.1f}σ")
        else: signals.append(0)
    else:
        signals.append(0)
    
    # --- 2. VIX from morning (3-poll smoothed) ---
    recent_vix = [p.get('vix') for p in polls[-4:] if p.get('vix')]
    if len(recent_vix) >= 3:
        vix_avg = sum(recent_vix[-3:]) / 3
        vix_change = vix_avg - base_vix
        if vix_change < -1.5: signals.append(2); drift_reasons.append(f"VIX {vix_change:+.1f}")
        elif vix_change < -0.5: signals.append(1)
        elif vix_change > 1.5: signals.append(-2); drift_reasons.append(f"VIX {vix_change:+.1f}")
        elif vix_change > 0.5: signals.append(-1)
        else: signals.append(0)
    else:
        signals.append(0)
    
    # --- 3. PCR from morning (3-poll smoothed) ---
    recent_pcr = [p.get('pcr') for p in polls[-4:] if p.get('pcr')]
    pcr_morning = first.get('pcr', 0) if first else 0
    if len(recent_pcr) >= 3 and pcr_morning > 0:
        pcr_avg = sum(recent_pcr[-3:]) / 3
        pcr_change = pcr_avg - pcr_morning
        if pcr_change > 0.2: signals.append(2); drift_reasons.append(f"PCR +{pcr_change:.2f}")
        elif pcr_change > 0.1: signals.append(1)
        elif pcr_change < -0.2: signals.append(-2); drift_reasons.append(f"PCR {pcr_change:.2f}")
        elif pcr_change < -0.1: signals.append(-1)
        else: signals.append(0)
    else:
        signals.append(0)
    
    # --- 4. Straddle direction (last 4 polls) ---
    straddles = [p.get('straddle') for p in polls[-4:] if p.get('straddle')]
    if len(straddles) >= 3:
        straddle_chg = straddles[-1] - straddles[0]
        if straddle_chg < -50: signals.append(2)  # shrinking fast → range/BULL
        elif straddle_chg < -20: signals.append(1)
        elif straddle_chg > 50: signals.append(-2)  # expanding fast → fear/BEAR
        elif straddle_chg > 20: signals.append(-1)
        else: signals.append(0)
    else:
        signals.append(0)
    
    # --- 5. Wall movement from morning ---
    cw_now = last.get('cw', 0)
    pw_now = last.get('pw', 0)
    cw_morning = baseline.get('bnfCallWall', 0)
    pw_morning = baseline.get('bnfPutWall', 0)
    wall_signal = 0
    if cw_now and cw_morning:
        cw_move = cw_now - cw_morning
        if cw_move > 200: wall_signal = 2; drift_reasons.append(f"CW +{cw_move}")
        elif cw_move > 100: wall_signal = 1
        elif cw_move < -200: wall_signal = -2; drift_reasons.append(f"CW {cw_move}")
        elif cw_move < -100: wall_signal = -1
    if pw_now and pw_morning:
        pw_move = pw_now - pw_morning
        if pw_move > 200: wall_signal = max(wall_signal, 1)  # put wall rising = BULL
        elif pw_move < -200: wall_signal = min(wall_signal, -1)
    signals.append(wall_signal)
    
    # --- 6. Breadth ---
    breadth_pct = (ctx.get('bnfBreadth') or {}).get('pct', 50)
    if breadth_pct > 65: signals.append(2); drift_reasons.append(f"Breadth {breadth_pct:.0f}%")
    elif breadth_pct > 55: signals.append(1)
    elif breadth_pct < 35: signals.append(-2); drift_reasons.append(f"Breadth {breadth_pct:.0f}%")
    elif breadth_pct < 45: signals.append(-1)
    else: signals.append(0)
    
    # --- 7. Regime (range pushes opposite to morning direction) ---
    regime_type = regime.get('type', 'unknown') if regime else 'unknown'
    regime_dir = regime.get('direction', 0) if regime else 0
    if regime_type == 'range':
        # Range contradicts directional bias — push toward NEUTRAL
        morning_sign = 1 if morning_net > 0 else -1 if morning_net < 0 else 0
        signals.append(-morning_sign)  # push opposite
        if morning_sign != 0: drift_reasons.append("Range → push NEUTRAL")
    elif regime_type == 'trend':
        if abs(regime_dir) >= 3:
            signals.append(2 if regime_dir > 0 else -2)
            drift_reasons.append(f"Strong trend {'↑' if regime_dir > 0 else '↓'}")
        elif abs(regime_dir) >= 1:
            signals.append(1 if regime_dir > 0 else -1)
        else:
            signals.append(0)
    else:
        signals.append(0)
    
    # ═══ BLEND: morning prior × intraday evidence ═══
    intraday_net = sum(signals)
    # Normalize to -3..+3 (same scale as morning). /TOTAL_SIGNALS so 1 signal is weak.
    intraday_normalized = (intraday_net / TOTAL_SIGNALS) * 3
    
    effective_net = morning_net * morning_weight + intraday_normalized * intraday_weight
    
    # Classify
    if effective_net >= 2: bias, strength = 'BULL', 'STRONG'
    elif effective_net >= 1: bias, strength = 'BULL', 'MILD'
    elif effective_net <= -2: bias, strength = 'BEAR', 'STRONG'
    elif effective_net <= -1: bias, strength = 'BEAR', 'MILD'
    else: bias, strength = 'NEUTRAL', ''
    
    return {
        'bias': bias,
        'strength': strength,
        'net': round(effective_net, 2),
        'morning_weight': round(morning_weight, 2),
        'signals': signals,
        'intraday_net': intraday_net,
        'drift_reasons': drift_reasons[:5]
    }

def chain_intelligence(polls, ctx):
    """b92: Deep chain analysis — returns LIST of ALL qualifying insights (was single-return).
    Uses 10 computed features from computeChainProfile."""
    profile = ctx.get('bnfProfile') or {}
    insights = []
    
    # 1. IV Smile Slope — steepness indicates fear/hedging
    iv_slope = profile.get('ivSlope', 0)
    if iv_slope > 3:
        insights.append({"type": "market", "icon": "📉", "label": f"Fear skew steep ({iv_slope:.1f})",
                "detail": "Put IV higher than call. Institutions hedging downside.",
                "impact": "bearish", "strength": 3})
    elif iv_slope < -2:
        insights.append({"type": "market", "icon": "📈", "label": f"Call skew unusual ({iv_slope:.1f})",
                "detail": "Call IV higher than put. Unusual bullish positioning.",
                "impact": "bullish", "strength": 2})
    
    # 2. Gamma Clustering — market coiled for move
    gamma_c = profile.get('gammaCluster', 0)
    if gamma_c > 0.6:
        insights.append({"type": "market", "icon": "⚡", "label": f"Gamma concentrated ({gamma_c:.0%} near ATM)",
                "detail": "High gamma at ATM. Coiled for sharp move.",
                "impact": "caution", "strength": 4})
    
    # 3. Volume Ratio — real-time institutional flow
    vol_r = profile.get('volRatio', 1.0)
    if vol_r > 2.0:
        insights.append({"type": "market", "icon": "📞", "label": f"Call buying surge ({vol_r:.1f}x)",
                "detail": "Call volume 2x put. Aggressive bullish flow.",
                "impact": "bullish", "strength": 3})
    elif vol_r < 0.5:
        insights.append({"type": "market", "icon": "📉", "label": f"Put buying surge ({vol_r:.1f}x)",
                "detail": "Put volume 2x call. Aggressive bearish flow.",
                "impact": "bearish", "strength": 3})
    
    # 4. OI Velocity — wall building speed
    oi_vel = profile.get('oiVelocity', 0)
    if abs(oi_vel) > 5:
        direction = "building" if oi_vel > 0 else "unwinding"
        insights.append({"type": "market", "icon": "🏗️", "label": f"OI {direction} fast ({oi_vel:.1f}L)",
                "detail": f"Institutional {'conviction' if oi_vel > 0 else 'exit'}.",
                "impact": "neutral", "strength": 3})
    
    # 5. Bid-Ask Quality — liquidity warning
    baq = profile.get('bidAskQuality', 0)
    if baq > 15:
        insights.append({"type": "market", "icon": "⚠️", "label": f"Poor liquidity ({baq:.1f}% spread)",
                "detail": "Wide spreads. Entry/exit costly.",
                "impact": "caution", "strength": 3})
    
    # 6. Net Delta — institutional directional bias
    nd = profile.get('netDelta', 0)
    if nd > 3.0:
        insights.append({"type": "market", "icon": "📊", "label": f"Net delta bullish ({nd:.1f})",
                "detail": "OI weighted bullish. Institutions positioned for up.",
                "impact": "bullish", "strength": 2})
    elif nd < -3.0:
        insights.append({"type": "market", "icon": "📊", "label": f"Net delta bearish ({nd:.1f})",
                "detail": "OI weighted bearish. Institutions positioned for down.",
                "impact": "bearish", "strength": 2})
    
    # 7. Wall Cluster Depth — fortress vs fragile walls
    cc_depth = profile.get('callClusterDepth', 0)
    pc_depth = profile.get('putClusterDepth', 0)
    if cc_depth >= 3 and pc_depth >= 3:
        insights.append({"type": "market", "icon": "🏰", "label": f"Both walls fortified (C:{cc_depth} P:{pc_depth})",
                "detail": "Heavy OI clusters on both sides. Strong range — IC/IB favorable.",
                "impact": "neutral", "strength": 4})
    elif cc_depth >= 3:
        insights.append({"type": "market", "icon": "🏰", "label": f"Call wall fortress ({cc_depth} deep)",
                "detail": "Multiple heavy resistance strikes. Hard ceiling above.",
                "impact": "bearish", "strength": 3})
    elif pc_depth >= 3:
        insights.append({"type": "market", "icon": "🏰", "label": f"Put wall fortress ({pc_depth} deep)",
                "detail": "Multiple heavy support strikes. Strong floor below.",
                "impact": "bullish", "strength": 3})
    elif cc_depth <= 1 or pc_depth <= 1:
        fragile = "call" if cc_depth <= 1 else "put"
        depth = cc_depth if fragile == "call" else pc_depth
        insights.append({"type": "market", "icon": "⚠️", "label": f"Fragile {fragile} wall (depth {depth})",
                "detail": f"Single-strike {fragile} wall. One unwind breaks it.",
                "impact": "caution", "strength": 3})
    
    return insights  # LIST — can be empty, 1, or multiple

def daily_pnl_check(polls, ctx):
    """Prevent overtrading and chasing losses."""
    pnl = ctx.get('dailyPnl', 0)
    count = ctx.get('dailyTradeCount', 0)
    if count >= 3:
        return {"type": "risk", "icon": "🛑", "label": f"3+ trades today — slow down",
                "detail": f"Net today: ₹{pnl:.0f} from {count} trades. Overtrading risk. Stop if losing.",
                "impact": "caution", "strength": 4}
    if pnl < -2000 and count >= 2:
        return {"type": "risk", "icon": "🛑", "label": f"Down ₹{abs(pnl):.0f} today — STOP trading",
                "detail": f"Chasing losses kills capital. Walk away.", "impact": "caution", "strength": 5}
    if pnl > 3000:
        return {"type": "risk", "icon": "💰", "label": f"Up ₹{pnl:.0f} today — protect gains",
                "detail": f"Good day. Only high-confidence entries from here.", "impact": "neutral", "strength": 2}
    return None

def candidate_liquidity(cand, ctx):
    """Bid-ask spread assessment from chain profile."""
    profile = ctx.get('bnfProfile' if cand.get('index')=='BNF' else 'nfProfile') or {}
    spread = profile.get('atmSpread', 0)
    if spread > 8:
        return {"icon": "⚠️", "label": f"Wide spreads (₹{spread:.0f})",
                "detail": "Slippage will eat into profits. Use limit orders.", "impact": "caution", "strength": 2}
    elif spread < 2:
        return {"icon": "✅", "label": "Tight spreads",
                "detail": "Good liquidity. Entry/exit efficient.", "impact": "bullish", "strength": 1}
    return None

def position_gamma_alert(trade, polls, strike_oi):
    """Track gamma acceleration at traded strikes across polls."""
    soi = strike_oi if isinstance(strike_oi, list) else []
    if len(soi) < 3: return None
    is_bear = 'BEAR' in trade.get('strategy_type', '')
    field = 'sellCOI' if is_bear else 'sellPOI'
    # Check if OI at sell strike is rapidly changing
    ois = [s.get(field) for s in soi if s.get(field) is not None]
    if len(ois) < 3: return None
    # Compute acceleration (rate of change of rate of change)
    changes = [ois[i] - ois[i-1] for i in range(1, len(ois))]
    if len(changes) < 2: return None
    accel = changes[-1] - changes[0]
    if abs(accel) > 5000:
        d = "building" if accel > 0 else "unwinding"
        return {"icon": "⚡", "label": f"OI {d} at sell strike",
                "detail": f"Acceleration detected. Position dynamics shifting.", "impact": "caution" if accel < 0 else "bullish", "strength": 3}
    return None

# ═══ THE VERDICT ═══

def synthesize_verdict(all_insights, regime, ctx, polls, baseline, candidates=None, cand_insights=None):
    """THE function. All intelligence in. ONE answer out.
    b92: Now receives candidates + their insights for menu awareness."""
    bull = bear = 0.0
    cautions = 0
    for ins in all_insights:
        w = (ins.get('strength', 1)) / 5.0
        imp = ins.get('impact', 'neutral')
        if imp == 'bullish': bull += w
        elif imp == 'bearish': bear += w
        elif imp == 'caution': cautions += 1

    # Context signals (numeric, not insights)
    profile = ctx.get('bnfProfile') or {}
    breadth = ctx.get('bnfBreadth') or {}
    b_pct = breadth.get('pct', 50)
    if b_pct > 65: bull += 0.4
    elif b_pct < 35: bear += 0.4
    skew = profile.get('ivSkew', 0)
    if skew > 3: bear += 0.2
    elif skew < -2: bull += 0.2
    cwF = profile.get('cwFresh', 0)
    pwF = profile.get('pwFresh', 0)
    if cwF > 0.25: bear += 0.15
    if pwF > 0.25: bull += 0.15
    fii_hist = ctx.get('fiiHistory', [])
    fii_sum = 0
    for h in fii_hist[:5]:
        v = h.get('fiiCash', 0)
        try: fii_sum += float(v) if v is not None else 0
        except (ValueError, TypeError): pass
    if fii_sum < -3000: bear += 0.3
    elif fii_sum > 3000: bull += 0.3

    # Direction
    if bull > bear + 0.4: direction = 'BULL'
    elif bear > bull + 0.4: direction = 'BEAR'
    else: direction = 'NEUTRAL'

    # Confidence
    total = bull + bear + 0.001
    dominant = max(bull, bear)
    confidence = int(dominant / total * 80)  # base max 80
    if cautions >= 3: confidence -= 15
    if bull > 0.5 and bear > 0.5: confidence -= 20  # conflicting
    rtype = regime.get('type', 'unknown')
    if rtype in ('range', 'trend'): confidence += 10  # clear regime = higher confidence
    if rtype == 'choppy': confidence -= 10

    # Personal calibration boost/penalty
    vixs = get_vix_vals(polls)
    vix = vixs[-1] if vixs else 20
    dte = ctx.get('bnfDTE', 5)

    # b93: Z-SCORE VIX REGIME — dynamic, not hardcoded
    vix_hist = ctx.get('vixHistory', [])
    vix_z = z_score(vix, vix_hist) if len(vix_hist) >= 10 else (1.5 if vix >= 24 else 0.5 if vix >= 20 else -0.5 if vix >= 16 else -1.5)
    # vix_z > 1.5 = extreme high (was vix>=24), vix_z > 0.5 = high (was vix>=20)
    # vix_z < -1.0 = low VIX regime, negative = cheap premiums

    # b93: STRADDLE VETO — Premium is King
    _, straddle_chg, straddle_expanding = straddle_velocity(polls)
    
    # Strategy selection
    conflicts = []
    if rtype == 'range':
        action = 'SELL PREMIUM'
        # Straddle expanding in range = market makers pricing in breakout — don't sell
        if straddle_expanding and straddle_chg > 5:
            conflicts.append(f"Straddle expanding +₹{straddle_chg:.0f} — breakout priced in")
            action = 'WAIT'
            strategy = None
        elif vix_z >= 0.5 and dte <= 1:
            strategy = 'IRON_BUTTERFLY'
        else:
            strategy = 'IRON_CONDOR'
        if direction != 'NEUTRAL' and strategy:
            conflicts.append(f"Range but bias {direction}")
    elif direction == 'BULL':
        if vix_z >= 1.5: action, strategy = 'BUY PREMIUM', 'BULL_CALL'
        elif vix_z >= 0.5: action, strategy = 'SELL PREMIUM', 'BULL_PUT'
        else: action, strategy = 'BUY PREMIUM', 'BULL_CALL'  # low VIX = cheap options, buy
    elif direction == 'BEAR':
        if vix_z >= 1.5: action, strategy = 'BUY PREMIUM', 'BEAR_PUT'
        elif vix_z >= 0.5: action, strategy = 'SELL PREMIUM', 'BEAR_CALL'
        else: action, strategy = 'BUY PREMIUM', 'BEAR_PUT'
    else:
        if rtype == 'choppy' or cautions >= 3:
            action, strategy = 'WAIT', None
        elif vix_z >= 0.5:
            action, strategy = 'SELL PREMIUM', 'IRON_CONDOR'
        else:
            action, strategy = 'WAIT', None

    # b93: HARD VETO — calibration kill switch (0% win rate = never recommend)
    vetoed_strategy = None
    if _calibration and strategy:
        cal = _calibration.get('strategy', {}).get(strategy, {})
        n = cal.get('total', 0)
        rate = cal.get('rate', 0.5)
        if n >= 5 and rate < 0.15:
            conflicts.append(f"VETO: {strategy} wins {cal.get('wins',0)}/{n} ({rate*100:.0f}%). Brain refuses.")
            vetoed_strategy = strategy
            strategy = None
            action = 'WAIT'
        elif n >= 5 and rate < 0.3:
            confidence -= 20
            conflicts.append(f"Your {strategy}: {cal.get('wins',0)}/{n} ({rate*100:.0f}%)")
        elif n >= 5 and rate > 0.7:
            confidence += 10

    # b92: Candidate menu awareness — does recommended strategy have viable candidates?
    if strategy and candidates and cand_insights is not None:
        strat_cands = [c for c in (candidates or []) if c.get('type') == strategy]
        if not strat_cands:
            conflicts.append(f"No {strategy} candidates generated")
            confidence -= 10
        elif strat_cands:
            # Check if ALL candidates of this type have caution insights
            all_cautioned = True
            for sc in strat_cands:
                cid = sc.get('id', '')
                c_ins = cand_insights.get(cid, [])
                has_severe = any(i.get('strength', 0) >= 4 and i.get('impact') == 'caution' for i in c_ins)
                if not has_severe:
                    all_cautioned = False
                    break
            if all_cautioned and len(strat_cands) > 0:
                conflicts.append(f"All {strategy} candidates have risk warnings")
                confidence -= 15

    # b95: SMART FALLBACK — if recommended strategy is dead, find best available alternative
    # Triggers when: (a) no candidates generated, (b) calibration <30%, OR (c) strategy was VETOED
    needs_fallback = False
    original_strategy = strategy or vetoed_strategy  # capture even if vetoed to None
    if candidates:
        if vetoed_strategy:
            needs_fallback = True  # veto killed it — MUST find alternative
        elif strategy:
            has_cands = any(c.get('type') == strategy for c in candidates)
            cal_dead = False
            if _calibration:
                cal_check = _calibration.get('strategy', {}).get(strategy, {})
                cal_dead = cal_check.get('total', 0) >= 5 and cal_check.get('rate', 0.5) < 0.3
            if not has_cands or cal_dead:
                needs_fallback = True

    if needs_fallback and candidates:
        # Score each available strategy type by: calibration win rate × number of candidates
        available_types = {}
        for c in candidates:
            ct = c.get('type', '')
            if ct not in available_types:
                available_types[ct] = {'count': 0, 'best_score': 0}
            available_types[ct]['count'] += 1
            available_types[ct]['best_score'] = max(available_types[ct]['best_score'], c.get('contextScore', 0))

        best_alt = None
        best_alt_score = -999
        for ct, info in available_types.items():
            if info['count'] == 0: continue
            # Calibration rate (default 0.5 if unknown)
            cal_rate = 0.5
            if _calibration:
                cal_s = _calibration.get('strategy', {}).get(ct, {})
                if cal_s.get('total', 0) >= 3:
                    cal_rate = cal_s.get('rate', 0.5)
            # Score = calibration × log(count+1) × context quality
            score = cal_rate * math.log(info['count'] + 1) * (1 + info['best_score'])
            # Bonus for IC/IB in range regime
            if ct in ('IRON_CONDOR', 'IRON_BUTTERFLY') and rtype == 'range':
                score *= 1.5
            if score > best_alt_score:
                best_alt_score = score
                best_alt = ct

        if best_alt and best_alt != original_strategy:
            # Determine action from strategy type
            credit_types = ['BEAR_CALL', 'BULL_PUT', 'IRON_CONDOR', 'IRON_BUTTERFLY']
            alt_action = 'SELL PREMIUM' if best_alt in credit_types else 'BUY PREMIUM'
            conflicts.append(f"Fallback: {original_strategy} → {best_alt} (available + proven)")
            strategy = best_alt
            action = alt_action
            # Restore some confidence — we found a viable alternative
            confidence = max(confidence, 30)

    # ═══ b92: FULL OMNISCIENCE CHECKS — use all 12 new data streams ═══

    # Trade mode conflict — brain recommends IC/IB but user is in SWING
    tm = ctx.get('tradeMode', 'swing')
    if strategy in ('IRON_CONDOR', 'IRON_BUTTERFLY') and tm == 'swing':
        conflicts.append(f"{strategy} is intraday only — switch to INTRADAY")
        confidence -= 20

    # Overnight delta conflict — brain says BULL but overnight is BEAR
    od = ctx.get('overnightDelta')
    if od and od.get('summary'):
        if 'BEARISH' in od['summary'] and direction == 'BULL':
            conflicts.append("Overnight BEARISH vs brain BULL")
            confidence -= 10
        elif 'BULLISH' in od['summary'] and direction == 'BEAR':
            conflicts.append("Overnight BULLISH vs brain BEAR")
            confidence -= 10

    # Institutional regime — low credit confidence but selling premium
    ir = ctx.get('institutionalRegime')
    if ir and ir.get('creditConfidence') == 'LOW' and action == 'SELL PREMIUM':
        conflicts.append("Low institutional credit confidence")
        confidence -= 10

    # Bias drift — morning thesis no longer holds
    drift = ctx.get('biasDrift', 0)
    if abs(drift) >= 2:
        conflicts.append(f"Bias drifted {drift:+d} from morning")
        confidence -= 10

    # Scan freshness — stale data
    scan_age = ctx.get('scanAgeMin')
    if scan_age and scan_age >= 30:
        conflicts.append(f"Data is {scan_age}min stale — rescan first")
        confidence -= 15

    # Global direction conflict — Dow/Crude/GIFT contradict brain direction
    gd = ctx.get('globalDirection') or {}
    gd_conflicts = 0
    if gd.get('dowPct') is not None and abs(gd['dowPct']) >= 0.5:
        dow_bull = gd['dowPct'] > 0
        if (dow_bull and direction == 'BEAR') or (not dow_bull and direction == 'BULL'):
            gd_conflicts += 1
    if gd.get('crudePct') is not None and abs(gd['crudePct']) >= 1.5:
        crude_bull = gd['crudePct'] < 0  # crude up = bearish for India
        if (crude_bull and direction == 'BEAR') or (not crude_bull and direction == 'BULL'):
            gd_conflicts += 1
    if gd.get('giftPct') is not None and abs(gd['giftPct']) >= 0.3:
        gift_bull = gd['giftPct'] > 0
        if (gift_bull and direction == 'BEAR') or (not gift_bull and direction == 'BULL'):
            gd_conflicts += 1
    if gd_conflicts >= 2:
        conflicts.append(f"Global direction contradicts ({gd_conflicts}/3 against)")
        confidence -= 15

    # Brain flip-flop detection — prior verdict contradicts current
    prior = ctx.get('priorVerdict')
    if prior and prior.get('direction') and prior['direction'] != direction:
        if prior.get('confidence', 0) >= 40:
            conflicts.append(f"Flip: was {prior['direction']} {prior.get('confidence',0)}% → now {direction}")
            confidence -= 10

    # Varsity alignment — brain strategy vs Varsity PRIMARY
    vf = ctx.get('varsityFilter')
    if vf and strategy:
        if strategy in (vf.get('primary') or []):
            confidence += 5  # brain agrees with Varsity
        elif strategy not in (vf.get('allowed') or []) and strategy not in (vf.get('primary') or []):
            conflicts.append(f"Brain picks {strategy} but Varsity doesn't allow it")
            confidence -= 10

    # Urgency
    mins = get_time_mins(polls[-1].get('t', '')) - 555 if polls else 0
    if 135 <= mins <= 315: urgency = 'ENTER NOW'
    elif mins < 15: urgency = 'WAIT — opening noise'
    elif mins > 345: urgency = 'WINDOW CLOSED'
    else: urgency = 'READY'

    # Daily P&L gate
    if ctx.get('dailyPnl', 0) < -2000:
        action, strategy, urgency = 'STOP', None, 'DONE FOR TODAY'
        confidence = 0
    elif ctx.get('dailyTradeCount', 0) >= 3:
        confidence -= 15
        if confidence < 50: urgency = 'CAUTION — 3+ trades today'

    confidence = max(0, min(100, confidence))
    if action == 'WAIT' or action == 'STOP':
        confidence = 0

    # Build reasoning
    reasons = []
    if rtype != 'unknown': reasons.append(f"{'Range' if rtype=='range' else 'Trend' if rtype=='trend' else rtype.title()} {regime.get('sigma',0):.1f}σ")
    if vix_z >= 1.5: reasons.append(f"VIX {vix:.1f} (Z:{vix_z:+.1f} EXTREME)")
    elif vix_z >= 0.5: reasons.append(f"VIX {vix:.1f} (Z:{vix_z:+.1f} HIGH)")
    elif vix_z <= -1.0: reasons.append(f"VIX {vix:.1f} (Z:{vix_z:+.1f} LOW)")
    if abs(b_pct - 50) > 10: reasons.append(f"Breadth {'strong' if b_pct>60 else 'weak'} ({b_pct:.0f}%)")
    if abs(fii_sum) > 1000: reasons.append(f"FII {'+' if fii_sum>0 else ''}₹{fii_sum:.0f}Cr/5d")
    if abs(skew) > 2: reasons.append(f"Skew {'steep' if skew>0 else 'flat'} ({skew:.0f})")
    if dte <= 1: reasons.append(f"EXPIRY (DTE {dte})")
    if _calibration and strategy:
        cal = _calibration.get('strategy', {}).get(strategy, {})
        if cal.get('total', 0) >= 3:
            reasons.append(f"Your {strategy}: {cal.get('wins',0)}/{cal['total']}")
    # b92: Context-aware reasoning
    if ctx.get('tradeMode') == 'intraday': reasons.append("Mode: INTRADAY")
    if od and 'BEARISH' in od.get('summary', ''): reasons.append("Overnight: BEAR")
    elif od and 'BULLISH' in od.get('summary', ''): reasons.append("Overnight: BULL")
    if gd_conflicts >= 2: reasons.append(f"Global: {gd_conflicts}/3 against")
    if abs(drift) >= 2: reasons.append(f"Drift: {drift:+d}")
    if straddle_expanding and straddle_chg > 3: reasons.append(f"Straddle expanding +₹{straddle_chg:.0f}")
    reasons.append(f"Bull {bull:.1f} vs Bear {bear:.1f}")

    return {
        "action": action, "strategy": strategy, "direction": direction,
        "confidence": confidence, "urgency": urgency,
        "reasoning": " · ".join(reasons[:8]),
        "conflicts": conflicts, "bull": round(bull, 2), "bear": round(bear, 2)
    }

def position_verdict(trade, insights, regime, ctx):
    """ONE action per trade: BOOK / HOLD / EXIT + urgency + reason.
    b89: Now receives wallDrift, vixChange, peakErosion from JS poll loop.
    Brain is the SINGLE decision maker — weighs ALL signals together."""
    pnl = trade.get('current_pnl', 0)
    max_p = trade.get('max_profit', 1)
    max_l = trade.get('max_loss', 1)
    ci = trade.get('controlIndex')
    pnl_pct = pnl / max_p if max_p > 0 else 0
    loss_pct = abs(pnl) / max_l if max_l > 0 and pnl < 0 else 0
    stype = trade.get('strategy_type', '')
    is_4leg = stype in ('IRON_CONDOR', 'IRON_BUTTERFLY')
    is_ib = stype == 'IRON_BUTTERFLY'
    is_credit = trade.get('is_credit', False)
    dte = ctx.get('bnfDTE' if trade.get('index_key') == 'BNF' else 'nfDTE', 5)
    phase = ctx.get('marketPhase', 'UNKNOWN')

    # b89: New signals from poll loop
    wall = trade.get('wallDrift') or {}
    wall_sev = wall.get('severity', 0)
    vix_chg = trade.get('vixChange', 0)
    peak_erosion = trade.get('peakErosion', 0)  # % of peak lost
    peak_pnl = trade.get('peak_pnl', 0)

    # Check insights for strong signals
    has_wall = any(i.get('label', '').startswith('Wall') for i in insights)
    against_trend = any('Against' in i.get('label', '') for i in insights)
    momentum_threat = any('sell strike' in i.get('label', '').lower() for i in insights)

    # ═══ DANGER SCORE — compound risk assessment ═══
    danger = 0
    reasons = []

    # Wall drift
    if wall_sev >= 2:
        danger += 40
        reasons.append(f"Wall EXPOSED ({wall.get('warning', '')[:50]})")
    elif wall_sev == 1:
        danger += 15
        reasons.append("Wall weakened")

    # VIX spike (worst for credit sellers)
    if is_credit and vix_chg >= 2.0:
        danger += 35
        reasons.append(f"VIX spiked +{vix_chg:.1f} — premiums expanding")
    elif is_credit and vix_chg >= 1.0:
        danger += 15
        reasons.append(f"VIX rising +{vix_chg:.1f}")

    # Peak erosion — SCALED (b104 fix: 864% got same score as 51%)
    # Debit trades: premium decay from theta is EXPECTED, halve the impact
    erosion_mult = 0.5 if not is_credit else 1.0
    if peak_pnl >= 500 and peak_erosion > 0:  # Gemini fix: ignore tiny peaks (<₹500)
        if peak_erosion > 500:
            danger += int(40 * erosion_mult)
            reasons.append(f"Peak erosion {peak_erosion:.0f}% (was ₹{peak_pnl:.0f})")
        elif peak_erosion > 200:
            danger += int(30 * erosion_mult)
            reasons.append(f"Peak erosion {peak_erosion:.0f}% (was ₹{peak_pnl:.0f})")
        elif peak_erosion > 50:
            danger += int(20 * erosion_mult)
            reasons.append(f"Peak erosion {peak_erosion:.0f}% (was ₹{peak_pnl:.0f})")
        elif peak_erosion > 30:
            danger += int(10 * erosion_mult)
            reasons.append(f"Profit fading ({peak_erosion:.0f}% from peak)")

    # Profit-to-loss flip — was making money, now losing (b104 fix)
    if peak_pnl > 0 and pnl < 0:
        danger += 15
        reasons.append(f"Flipped from +₹{peak_pnl:.0f} to -₹{abs(pnl):.0f}")

    # CI — RELAXED thresholds (b104 fix: CI -5 got ZERO before)
    # Gemini fix: IB not totally exempt — check for extreme degradation beyond baseline
    if ci is not None:
        if is_ib:
            if ci < -75:
                danger += 20
                reasons.append(f"IB CI collapsed to {ci} (beyond normal ATM range)")
        else:
            if ci < -40:
                danger += 25
                reasons.append(f"Opponent in control (CI {ci})")
            elif ci < -20:
                danger += 15
                reasons.append(f"Opponent gaining (CI {ci})")
            elif ci < 0:
                danger += 5

    # b115: Breakeven cushion — the REAL danger line, not sell strike
    # be_upper = upper breakeven (IC/IB/Bear Call), be_lower = lower breakeven (IC/IB/Bull Put)
    be_upper = trade.get('be_upper') or trade.get('beUpper')
    be_lower = trade.get('be_lower') or trade.get('beLower')
    sell_strike = trade.get('sell_strike', 0)
    spot = trade.get('current_spot', 0)
    if spot and (be_upper or be_lower):
        vix = ctx.get('vix', 20)
        daily_sigma = spot * (vix / 100) / 15.87 if spot > 0 else 300
        if is_4leg and be_upper and be_lower:
            upper_cushion = be_upper - spot
            lower_cushion = spot - be_lower
            near_cushion = min(upper_cushion, lower_cushion)
            near_label = f"upper BE {be_upper}" if upper_cushion < lower_cushion else f"lower BE {be_lower}"
        elif be_upper:
            near_cushion = be_upper - spot
            near_label = f"BE {be_upper}"
        else:
            near_cushion = spot - be_lower
            near_label = f"BE {be_lower}"
        if daily_sigma > 0:
            cushion_sigma = near_cushion / daily_sigma
            if near_cushion <= 0:
                danger += 40
                reasons.append(f"BREACHED — spot past {near_label}")
            elif cushion_sigma < 0.15:
                danger += 30
                reasons.append(f"Only {near_cushion:.0f}pts to {near_label} ({cushion_sigma:.2f}σ)")
            elif cushion_sigma < 0.30:
                danger += 20
                reasons.append(f"Thin BE cushion {near_cushion:.0f}pts to {near_label}")
            elif cushion_sigma < 0.50:
                danger += 10
                reasons.append(f"Approaching {near_label} ({near_cushion:.0f}pts)")
        # b115: Early BOOK — profitable + near breakeven = take money now
        if pnl > 0 and near_cushion > 0 and daily_sigma > 0 and near_cushion / daily_sigma < 0.20:
            return {"action": "BOOK", "urgency": "NOW",
                    "reason": f"₹{pnl:.0f} profit but only {near_cushion:.0f}pts to {near_label}. Premium at risk — lock in now."}
    elif sell_strike and spot and is_credit and not is_ib:
        # Fallback: use sell_strike if no breakeven stored (old trades)
        cushion = abs(sell_strike - spot)
        vix = ctx.get('vix', 20)
        daily_sigma = spot * (vix / 100) / 15.87 if spot > 0 else 300
        if daily_sigma > 0:
            cushion_sigma = cushion / daily_sigma
            if cushion_sigma < 0.25:
                danger += 25
                reasons.append(f"Only {cushion:.0f}pts ({cushion_sigma:.2f}σ) from sell strike")
            elif cushion_sigma < 0.5:
                danger += 15
                reasons.append(f"Thin cushion ({cushion_sigma:.2f}σ from sell)")

    # Momentum threat
    if momentum_threat:
        danger += 30
        reasons.append("Spot approaching sell strike")

    # Phase mismatch (credit in trending phase = danger)
    if is_credit and phase == 'TRENDING':
        danger += 10
        reasons.append("Trending market — credit at risk")

    # b96: "Past the wall" insight — position_wall_proximity detected exposure
    past_wall = any('Past the wall' in i.get('label', '') for i in insights)
    if past_wall:
        danger += 35
        reasons.append("Sell past OI wall — no protection")

    # b96: Loss magnitude — deep loss even with stable danger should escalate
    if pnl < 0:
        if loss_pct > 0.5:
            danger += 30
            reasons.append(f"Deep loss ({loss_pct*100:.0f}% of max)")
        elif loss_pct > 0.3:
            danger += 15
            reasons.append(f"Significant loss ({loss_pct*100:.0f}% of max)")

    # ═══ EXIT — compound danger high ═══
    if danger >= 60:
        urgency = 'NOW' if danger >= 80 else 'SOON'
        return {"action": "EXIT", "urgency": urgency,
                "reason": f"Danger {danger}/100. {'. '.join(reasons[:3])}"}

    # ═══ b114: THESIS_BROKEN — entry bias contradicts current effective bias ═══
    entry_bias = trade.get('entry_bias', '')
    current_bias = (ctx.get('effective_bias') or {}).get('bias', '') or ''
    thesis_broken = False
    if is_credit and not is_4leg and entry_bias and current_bias:
        bull_entry = 'BULL' in entry_bias.upper()
        bear_entry = 'BEAR' in entry_bias.upper()
        bull_now = 'BULL' in current_bias.upper()
        bear_now = 'BEAR' in current_bias.upper()
        if (bull_entry and bear_now) or (bear_entry and bull_now):
            thesis_broken = True
    if thesis_broken and pnl < 0:
        return {"action": "EXIT", "urgency": "SOON",
                "reason": f"Thesis broken. Entered {entry_bias}, market now {current_bias}. Credit spread fighting the trend."}
    if thesis_broken and pnl >= 0:
        return {"action": "BOOK", "urgency": "NOW",
                "reason": f"Thesis broken — market flipped {entry_bias}→{current_bias}. Lock in ₹{pnl:.0f} before it reverses."}

    # ═══ EXIT — structural threats (independent of danger score) ═══
    if is_4leg and dte <= 1:
        if pnl > 0:
            return {"action": "BOOK", "urgency": "NOW", "reason": "4-leg + expiry day. 0% overnight survival."}
        else:
            return {"action": "EXIT", "urgency": "NOW", "reason": "4-leg + expiry. Cut loss, don't hold overnight."}

    # ═══ BOOK — profitable + reasons to take money ═══
    if pnl_pct >= 0.5:
        book_reasons = []
        if danger >= 30: book_reasons.append(f"rising danger ({danger})")
        if peak_erosion > 20: book_reasons.append(f"peak fading {peak_erosion:.0f}%")
        if regime.get('type') == 'range': book_reasons.append("range — theta captured")
        if vix_chg < -1.0 and is_credit: book_reasons.append(f"VIX crushed {vix_chg:.1f} — lock gains")
        urgency = 'NOW' if pnl_pct >= 0.7 or danger >= 30 else 'SOON'
        reason = f"{pnl_pct*100:.0f}% of max."
        if book_reasons: reason += f" {'. '.join(book_reasons[:2])}"
        return {"action": "BOOK", "urgency": urgency, "reason": reason}

    if pnl_pct >= 0.3 and (against_trend or danger >= 25):
        return {"action": "BOOK", "urgency": "SOON",
                "reason": f"{pnl_pct*100:.0f}% profit + {'risk building' if danger >= 25 else 'against trend'}. Don't give it back."}

    # ═══ HOLD — positive conditions ═══
    if pnl > 0 and danger < 20:
        hold_reasons = []
        if ci and ci > 20: hold_reasons.append(f"CI {ci}")
        if has_wall and wall_sev == 0: hold_reasons.append("wall protecting")
        if regime.get('type') == 'range': hold_reasons.append("range — theta working")
        if vix_chg < 0 and is_credit: hold_reasons.append("VIX falling — good for credit")
        reason = f"P&L ₹{pnl:.0f} ({pnl_pct*100:.0f}%)."
        if hold_reasons: reason += f" {'. '.join(hold_reasons[:2])}"
        return {"action": "HOLD", "urgency": "WATCH", "reason": reason}

    # ═══ DEFAULT ═══
    if pnl >= 0:
        return {"action": "HOLD", "urgency": "WATCH", "reason": f"P&L ₹{pnl:.0f}. Danger {danger}/100."}
    else:
        return {"action": "HOLD", "urgency": "MONITOR",
                "reason": f"Loss ₹{pnl:.0f} ({loss_pct*100:.0f}%). Danger {danger}/100. {'. '.join(reasons[:2]) if reasons else 'Watch CI.'}"}

# ═══════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# CANDIDATE GENERATION ENGINE — Phase 3
# Ported from app.js generateCandidates (1500 lines JS → ~550 lines Python)
# Premium is king. Dynamic is the way.
# ═══════════════════════════════════════════════════════════════

# ─── BLACK-SCHOLES MATH ───

def _norm_cdf(x):
    """Cumulative normal distribution using math.erf (stdlib, exact)"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))

def _bs_delta(spot, strike, T, vol, opt_type):
    """Black-Scholes delta. opt_type = 'CE' or 'PE'"""
    if T <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return 0.5 if opt_type == 'CE' else -0.5
    r = 0.065  # Gemini fix: Indian risk-free rate ~6.5%
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * T) / (vol * math.sqrt(T))
    if opt_type == 'CE':
        return _norm_cdf(d1)
    else:
        return _norm_cdf(d1) - 1.0

def _daily_sigma(spot, vix):
    """Daily 1σ move from VIX"""
    if spot <= 0 or vix <= 0: return 300
    return spot * (vix / 100) / math.sqrt(252)

def _sigma_days(spot, vix, dte):
    """Multi-day σ move"""
    return _daily_sigma(spot, vix) * math.sqrt(max(1, dte))

# ─── CONSTANTS ───

_CONST = {
    'BNF_LOT': 30, 'NF_LOT': 65,
    'CAPITAL': 250000, 'MAX_RISK_PCT': 10,
    'BNF_WIDTHS': [200, 300, 400, 500, 600, 800, 1000],
    'NF_WIDTHS': [100, 150, 200, 250, 300, 400],
    'IV_HIGH': 20, 'IV_VERY_HIGH': 24, 'IV_LOW': 15,
    'MIN_PROB': 0.50, 'MIN_CREDIT_RATIO': 0.10,
    'MIN_SIGMA_OTM': 0.5, 'MIN_WIDTH_BNF': 400, 'MIN_WIDTH_NF': 150,
    'CREDIT_TYPES': ['BEAR_CALL', 'BULL_PUT', 'IRON_CONDOR', 'IRON_BUTTERFLY'],
    'DEBIT_TYPES': ['BEAR_PUT', 'BULL_CALL', 'DOUBLE_DEBIT'],
    'NEUTRAL_TYPES': ['IRON_CONDOR', 'IRON_BUTTERFLY', 'DOUBLE_DEBIT'],
    'DIR_BULL': ['BULL_CALL', 'BULL_PUT'],
    'DIR_BEAR': ['BEAR_CALL', 'BEAR_PUT'],
}

# ─── VARSITY FILTER ───

def _get_varsity_filter(bias, vix, trade_mode, range_detected=False):
    """Maps bias + VIX → PRIMARY / ALLOWED / BLOCKED strategy types.
    Exact port of Zerodha Varsity Modules 5, 6 logic."""
    b = bias.get('bias', 'NEUTRAL')
    strength = bias.get('strength', '')
    is_strong = strength == 'STRONG'
    iv_high = vix >= _CONST['IV_HIGH']
    very_high = vix >= _CONST['IV_VERY_HIGH']

    if b == 'BEAR' and iv_high:
        primary = ['BEAR_CALL']
        allowed = [] if is_strong else ['BULL_PUT', 'IRON_CONDOR']
        blocked = ['BEAR_PUT', 'BULL_CALL', 'DOUBLE_DEBIT']
    elif b == 'BULL' and iv_high:
        primary = ['BULL_PUT']
        allowed = [] if is_strong else ['BEAR_CALL', 'IRON_CONDOR']
        blocked = ['BULL_CALL', 'BEAR_PUT', 'DOUBLE_DEBIT']
    elif b == 'NEUTRAL' and iv_high:
        primary = ['IRON_CONDOR']
        allowed = ['BEAR_CALL', 'BULL_PUT']
        blocked = ['BEAR_PUT', 'BULL_CALL', 'DOUBLE_DEBIT']
    elif b == 'BEAR' and not iv_high:
        primary = ['BEAR_PUT']
        allowed = [] if is_strong else ['BEAR_CALL']
        blocked = ['BULL_PUT', 'BULL_CALL', 'IRON_CONDOR', 'DOUBLE_DEBIT']
    elif b == 'BULL' and not iv_high:
        primary = ['BULL_CALL']
        allowed = [] if is_strong else ['BULL_PUT']
        blocked = ['BEAR_CALL', 'BEAR_PUT', 'IRON_CONDOR', 'DOUBLE_DEBIT']
    else:  # NEUTRAL + low IV
        primary = ['DOUBLE_DEBIT']
        allowed = ['IRON_CONDOR']
        blocked = ['BEAR_PUT', 'BULL_CALL', 'BEAR_CALL', 'BULL_PUT']

    # VERY HIGH VIX override — debit co-PRIMARY (backtest: VIX≥24 debit 91.7%)
    if very_high:
        if b == 'BEAR':
            if 'BEAR_PUT' not in primary: primary.append('BEAR_PUT')
            blocked = [s for s in blocked if s != 'BEAR_PUT']
        elif b == 'BULL':
            if 'BULL_CALL' not in primary: primary.append('BULL_CALL')
            blocked = [s for s in blocked if s != 'BULL_CALL']
        else:
            for s in ['BEAR_PUT', 'BULL_CALL']:
                if s not in allowed: allowed.append(s)
            blocked = [s for s in blocked if s not in ('BEAR_PUT', 'BULL_CALL')]

    # Range detection override — IB/IC default (100% vs 47% directional)
    if range_detected and iv_high:
        primary = ['IRON_BUTTERFLY', 'IRON_CONDOR']
        if b == 'BEAR': allowed = ['BEAR_CALL']
        elif b == 'BULL': allowed = ['BULL_PUT']
        else: allowed = []
        blocked = [s for s in blocked if s not in ('IRON_BUTTERFLY', 'IRON_CONDOR')]

    # IB blocked by default (margin concern) unless range override
    if 'IRON_BUTTERFLY' not in primary and 'IRON_BUTTERFLY' not in allowed:
        if 'IRON_BUTTERFLY' not in blocked: blocked.append('IRON_BUTTERFLY')

    return {'primary': primary, 'allowed': allowed, 'blocked': blocked}

# ─── FORCE ENGINE ───

def _assess_force1(stype, bias):
    """Direction force: does bias support this strategy?"""
    b = bias.get('bias', 'NEUTRAL')
    is_neutral = stype in _CONST['NEUTRAL_TYPES']
    is_bull = stype in _CONST['DIR_BULL']
    is_bear = stype in _CONST['DIR_BEAR']
    if is_neutral:
        if b == 'NEUTRAL': return 1
        if bias.get('strength', '') == 'MILD': return 0
        return -1
    if b == 'BULL' and is_bull: return 1
    if b == 'BEAR' and is_bear: return 1
    if b == 'NEUTRAL': return 0
    if b == 'BULL' and is_bear: return -1
    if b == 'BEAR' and is_bull: return -1
    return 0

def _assess_force2(stype):
    """Theta force: credit = +1, debit = -1"""
    return 1 if stype in _CONST['CREDIT_TYPES'] else -1

def _assess_force3(stype, vix, iv_pctl):
    """IV force: VIX regime → favor credit or debit"""
    is_credit = stype in _CONST['CREDIT_TYPES']
    is_debit = stype in _CONST['DEBIT_TYPES']
    regime = 'NORMAL'
    if vix >= _CONST['IV_HIGH'] or (iv_pctl is not None and iv_pctl > 65): regime = 'HIGH'
    if vix >= _CONST['IV_VERY_HIGH'] or (iv_pctl is not None and iv_pctl > 85): regime = 'VERY_HIGH'
    if vix <= _CONST['IV_LOW'] or (iv_pctl is not None and iv_pctl < 25): regime = 'LOW'
    if regime == 'VERY_HIGH':
        if is_debit: return 1
        return 1 if stype in _CONST['NEUTRAL_TYPES'] else 0
    if regime == 'HIGH': return 1 if is_credit else -1
    if regime == 'LOW': return 1 if is_debit else -1
    return 0

def _get_forces(stype, bias, vix, iv_pctl):
    f1 = _assess_force1(stype, bias)
    f2 = _assess_force2(stype)
    f3 = _assess_force3(stype, vix, iv_pctl)
    aligned = sum(1 for f in [f1, f2, f3] if f == 1)
    against = sum(1 for f in [f1, f2, f3] if f == -1)
    return {'f1': f1, 'f2': f2, 'f3': f3, 'aligned': aligned, 'against': against, 'score': f1+f2+f3}

# ─── CHAIN HELPERS ───

def _chain_delta(strikes, price, opt_type, spot, T, vol):
    """Get delta from chain (Upstox IV smile) with BS fallback"""
    sp = str(int(price))
    s = strikes.get(sp, {}).get(opt_type, {})
    d = s.get('delta')
    if d is not None and d != 0: return d
    return _bs_delta(spot, price, T, vol, opt_type)

def _chain_theta(strikes, price, opt_type, spot, T, vol):
    """Get theta from chain with BS fallback"""
    sp = str(int(price))
    s = strikes.get(sp, {}).get(opt_type, {})
    t = s.get('theta')
    if t is not None: return t
    # Simple BS theta approximation with risk-free rate
    if T <= 0 or vol <= 0: return 0
    r = 0.065  # Gemini fix: Indian risk-free rate
    d1 = (math.log(spot / max(1, price)) + (r + 0.5 * vol * vol) * T) / (vol * math.sqrt(T))
    return -(spot * vol * math.exp(-d1*d1/2) / (2 * math.sqrt(2 * math.pi * T))) / 365

# ─── SCORING ───

def _compute_wall_score(cand, chain, is_bnf):
    step = 200 if is_bnf else 100
    cw = chain.get('callWallStrike', 0)
    pw = chain.get('putWallStrike', 0)
    if not cw or not pw: return 0
    stype = cand['type']
    score = 0
    if stype in ('BEAR_CALL', 'IRON_CONDOR'):
        dist = abs(cand['sellStrike'] - cw)
        if dist == 0: score = 1.0
        elif dist <= step: score = 0.7
        elif dist <= step * 2: score = 0.4
    if stype in ('BULL_PUT', 'IRON_CONDOR'):
        sell_put = cand.get('sellStrike2', cand['sellStrike'])
        dist = abs(sell_put - pw)
        ps = 1.0 if dist == 0 else 0.7 if dist <= step else 0.4 if dist <= step * 2 else 0
        if stype == 'IRON_CONDOR': score = (score + ps) / 2
        else: score = ps
    if stype == 'IRON_BUTTERFLY':
        if abs(cand['sellStrike'] - cw) > step * 3 and abs(cand['sellStrike'] - pw) > step * 3:
            score = 0.6
    if stype == 'BULL_CALL' and abs(cand.get('buyStrike', 0) - cw) <= step: score = -0.5
    if stype == 'BEAR_PUT' and abs(cand.get('buyStrike', 0) - pw) <= step: score = -0.5
    return round(score, 2)

def _compute_gamma_risk(cand, spot, tdte):
    if not cand.get('isCredit'): return 0
    dist = abs(cand['sellStrike'] - spot)
    step = 200 if spot > 30000 else 100
    steps_away = dist / max(1, step)
    risk = 0
    if steps_away <= 2: risk += 0.5
    elif steps_away <= 4: risk += 0.3
    elif steps_away <= 6: risk += 0.1
    if tdte <= 2: risk += 0.5
    elif tdte <= 3: risk += 0.3
    elif tdte <= 5: risk += 0.1
    return round(min(1.0, risk), 2)

def _compute_context_score(cand, spot, tdte, vix, ctx):
    penalty = 0.0
    is_credit = cand.get('isCredit', False)
    stype = cand['type']
    is_bear = stype in _CONST['DIR_BEAR']
    is_bull = stype in _CONST['DIR_BULL']
    trade_mode = ctx.get('tradeMode', 'swing')
    ds = _daily_sigma(spot, vix)
    if ds <= 0: return 0

    # 1. VIX direction (Varsity M6 Ch8.4)
    fii_hist = ctx.get('fiiHistory', [])
    yday_vix = fii_hist[0].get('vix') if fii_hist else None
    if yday_vix and is_credit:
        vc = vix - yday_vix
        if trade_mode == 'swing':
            if vc < -0.5: penalty -= 0.3
            elif vc < -0.2: penalty -= 0.15
        else:
            if vc < -0.5: penalty -= 0.1

    # 2. Gap conflict
    gap = ctx.get('gap', {})
    gap_sigma = gap.get('sigma', 0) if gap else 0
    if abs(gap_sigma) > 0.8:
        if (gap_sigma > 0.8 and is_bear) or (gap_sigma < -0.8 and is_bull):
            penalty -= 0.4
            if abs(gap_sigma) > 1.5: penalty -= 0.3

    # 3. Strike distance — credit sweet spot 0.5-0.8σ (backtest confirmed)
    if is_credit and ds > 0:
        sigma_away = abs(cand['sellStrike'] - spot) / ds
        if sigma_away < 0.3: penalty -= 0.5
        elif sigma_away < 0.5: penalty -= 0.25
        if 0.5 <= sigma_away <= 0.8: penalty += 0.2
        elif 0.8 < sigma_away <= 1.0: penalty -= 0.15
        elif sigma_away > 1.0: penalty -= 0.3

    # 4. Width bonus — wider is better (+0.727 correlation with P&L)
    if is_credit:
        min_w = _CONST['MIN_WIDTH_BNF'] if spot > 30000 else _CONST['MIN_WIDTH_NF']
        if cand['width'] < min_w: penalty -= 0.3
        if cand['width'] >= min_w * 2: penalty += 0.1
        if trade_mode == 'swing' and cand['width'] < 200: penalty -= 0.1

    # 5. Far OTM debit + high DTE — swing only
    if trade_mode == 'swing' and not is_credit and tdte > 5:
        buy_dist = abs(cand.get('buyStrike', spot) - spot)
        if buy_dist / ds > 3: penalty -= 0.3

    return round(penalty, 2)

# ─── STRIKE PAIR GENERATION ───

def _get_strike_pairs(stype, atm, width, step, all_strikes, spot, is_bnf, cw, pw):
    pairs = []
    seen = set()
    rng = 2000 if is_bnf else 800
    all_set = set(all_strikes)

    if stype == 'BEAR_CALL':
        sell = atm
        while sell <= atm + rng:
            buy = sell + width
            if sell in all_set and buy in all_set:
                pairs.append({'sell': sell, 'buy': buy, 'sellType': 'CE', 'buyType': 'CE'})
                seen.add(sell)
            sell += step
        if cw and cw > atm and cw not in seen:
            buy = cw + width
            if cw in all_set and buy in all_set:
                pairs.append({'sell': cw, 'buy': buy, 'sellType': 'CE', 'buyType': 'CE'})
        if cw and cw - step > atm and cw - step not in seen:
            s = cw - step; b = s + width
            if s in all_set and b in all_set:
                pairs.append({'sell': s, 'buy': b, 'sellType': 'CE', 'buyType': 'CE'})

    elif stype == 'BULL_PUT':
        sell = atm
        while sell >= atm - rng:
            buy = sell - width
            if sell in all_set and buy in all_set:
                pairs.append({'sell': sell, 'buy': buy, 'sellType': 'PE', 'buyType': 'PE'})
                seen.add(sell)
            sell -= step
        if pw and pw < atm and pw not in seen:
            buy = pw - width
            if pw in all_set and buy in all_set:
                pairs.append({'sell': pw, 'buy': buy, 'sellType': 'PE', 'buyType': 'PE'})
        if pw and pw + step < atm and pw + step not in seen:
            s = pw + step; b = s - width
            if s in all_set and b in all_set:
                pairs.append({'sell': s, 'buy': b, 'sellType': 'PE', 'buyType': 'PE'})

    elif stype == 'BEAR_PUT':
        buy_k = atm
        while buy_k >= atm - rng:
            sell_k = buy_k - width
            if buy_k in all_set and sell_k in all_set:
                pairs.append({'sell': sell_k, 'buy': buy_k, 'sellType': 'PE', 'buyType': 'PE'})
            buy_k -= step

    elif stype == 'BULL_CALL':
        buy_k = atm
        while buy_k <= atm + rng:
            sell_k = buy_k + width
            if buy_k in all_set and sell_k in all_set:
                pairs.append({'sell': sell_k, 'buy': buy_k, 'sellType': 'CE', 'buyType': 'CE'})
            buy_k += step

    return pairs[:10]

# ─── BUILD SINGLE 2-LEG CANDIDATE ───

def _build_candidate(stype, pair, strikes, spot, lot_size, width, T, tdte, vol, expiry, is_bnf, vix, trade_mode):
    sp_sell = str(int(pair['sell']))
    sp_buy = str(int(pair['buy']))
    sell_data = strikes.get(sp_sell, {}).get(pair['sellType'], {})
    buy_data = strikes.get(sp_buy, {}).get(pair['buyType'], {})
    if not sell_data or not buy_data: return None

    is_credit = stype in _CONST['CREDIT_TYPES']
    sell_price = sell_data.get('bid', 0) if is_credit else sell_data.get('ask', 0)
    buy_price = buy_data.get('ask', 0) if is_credit else buy_data.get('bid', 0)
    if not sell_price or not buy_price: return None

    if is_credit:
        net_prem = sell_price - buy_price
        if net_prem <= 0: return None
        max_profit = net_prem * lot_size
        max_loss = (width - net_prem) * lot_size
    else:
        net_prem = buy_price - sell_price
        if net_prem <= 0: return None
        max_profit = (width - net_prem) * lot_size
        max_loss = net_prem * lot_size

    if max_loss <= 0 or max_profit <= 0: return None
    capital = _capital
    if max_loss > capital * _CONST['MAX_RISK_PCT'] / 100: return None

    # Sigma OTM filter — credit directional only (0.5σ minimum)
    sigma_otm = None
    ds = _daily_sigma(spot, vix)
    if is_credit and stype in ('BEAR_CALL', 'BULL_PUT') and ds > 0:
        sigma_otm = abs(pair['sell'] - spot) / ds
        if sigma_otm < _CONST['MIN_SIGMA_OTM']: return None
        sigma_otm = round(sigma_otm, 2)

    # Minimum width filter — narrow credit directional rejected
    if is_credit and stype in ('BEAR_CALL', 'BULL_PUT'):
        min_w = _CONST['MIN_WIDTH_BNF'] if is_bnf else _CONST['MIN_WIDTH_NF']
        if width < min_w: return None

    # Probability at breakeven
    if is_credit:
        be = pair['sell'] + net_prem if pair['sellType'] == 'CE' else pair['sell'] - net_prem
        prob = 1 - abs(_chain_delta(strikes, be, pair['sellType'], spot, T, vol))
    else:
        be = pair['buy'] + net_prem if pair['buyType'] == 'CE' else pair['buy'] - net_prem
        prob = abs(_chain_delta(strikes, be, pair['buyType'], spot, T, vol))

    if prob < _CONST['MIN_PROB']: return None
    if is_credit and (net_prem / width) < _CONST['MIN_CREDIT_RATIO']: return None

    ev = round(prob * max_profit * 0.65 - (1 - prob) * max_loss)  # Gemini fix: profit capture discount
    sell_theta = _chain_theta(strikes, pair['sell'], pair['sellType'], spot, T, vol) * lot_size
    buy_theta = _chain_theta(strikes, pair['buy'], pair['buyType'], spot, T, vol) * lot_size
    net_theta = round(-(sell_theta - buy_theta) if is_credit else (sell_theta - buy_theta))

    idx = 'BNF' if is_bnf else 'NF'
    cid = f"{stype}_{idx}_{pair['sell']}_{pair['buy']}_W{width}"

    # ── 5 display fields ─────────────────────────────────────────────────
    # estCost: capital to allocate (broker margin ≈ maxLoss for spreads)
    est_cost = round(max_loss * (1.3 if stype in ('IRON_CONDOR', 'IRON_BUTTERFLY') else 1.0))
    est_cost_pct = round(est_cost / max(1, _capital) * 100, 1)
    # netDelta: directional exposure (positive = bullish, negative = bearish)
    sell_delta = sell_data.get('delta', 0) or 0
    buy_delta  = buy_data.get('delta', 0) or 0
    net_delta  = round((sell_delta - buy_delta) if is_credit else (buy_delta - sell_delta), 4)
    # netMaxProfit: same as maxProfit for spreads (legs already netted)
    net_max_profit = round(max_profit)
    # upstoxPop: Upstox's own P(profit) from option_greeks.pop for sell leg
    upstox_pop = sell_data.get('pop')

    # ── ML scoring (SPLICE 2) ─────────────────────────────────────────────
    _ml_cand = {
        'strategy':       stype,
        'mode':           trade_mode,
        'vix':            vix,
        'sigma_away':     sigma_otm or 0,
        'gap_sigma':      0,
        'dte':            tdte,
        'entry_credit':   round(net_prem, 2),
        'width':          width,
        'move_sigma':     0,
        'day_range_sigma':0,
        'consec_days':    0,
        'max_profit':     round(max_profit),
        'max_loss':       round(max_loss),
        'legs':           2,
        'is_credit':      is_credit,
        'vix_regime':     ('HIGH (20-25)' if vix >= 20 else 'LOW (<15)' if vix < 15 else 'NORMAL (15-20)'),
        'day_group':      'Mon-Wed',
        'day_direction':  'FLAT',
        'day_range':      'NORMAL',
        'day_vix':        'HIGH' if vix >= 20 else ('LOW' if vix < 15 else 'NORMAL'),
        'weekday':        0,
    }
    ml = _ml_score(_ml_cand)

    return {
        'id': cid, 'type': stype, 'width': width, 'legs': 2,
        'sellStrike': pair['sell'], 'buyStrike': pair['buy'],
        'sellType': pair['sellType'], 'buyType': pair['buyType'],
        'sellLTP': sell_price, 'buyLTP': buy_price,
        'sellOI': sell_data.get('oi', 0), 'buyOI': buy_data.get('oi', 0),
        'netPremium': round(net_prem, 2), 'maxProfit': round(max_profit),
        'maxLoss': round(max_loss), 'probProfit': round(prob, 3),
        'ev': ev, 'netTheta': net_theta, 'isCredit': is_credit,
        'lotSize': lot_size, 'index': idx, 'expiry': expiry, 'tDTE': tdte,
        'sigmaOTM': sigma_otm,
        'riskReward': f"1:{max_profit/max_loss:.2f}" if max_loss > 0 else '--',
        'targetProfit': round(abs(net_theta) * 0.5) if trade_mode == 'intraday' and tdte > 2 and is_credit and abs(net_theta) > 0 else round(max_profit * 0.5),
        'stopLoss': round(abs(net_theta)) if trade_mode == 'intraday' and tdte > 2 and is_credit and abs(net_theta) > 0 else round(max_profit if is_credit else max_loss * 0.5),
        # ── 5 display fields ──
        'estCost':       est_cost,
        'estCostPct':    est_cost_pct,
        'netDelta':      net_delta,
        'netMaxProfit':  net_max_profit,
        'upstoxPop':     upstox_pop,
        # ── ML fields (None if model not loaded) ──
        'p_ml':          ml.get('p_ml'),
        'mlAction':      ml.get('ml_action'),
        'mlRegime':      ml.get('ml_regime'),
        'mlEdge':        ml.get('ml_edge'),
        'mlOod':         ml.get('ml_ood', False),
        'mlOodConf':     ml.get('ml_ood_conf', 1.0),
        'mlOodBlocked':  ml.get('ml_ood_blocked', False),
    }

# ─── MAIN: GENERATE ALL CANDIDATES FOR ONE INDEX ───

def generate_candidates(chain, spot, index_key, expiry, vix, bias, iv_pctl, ctx):
    """Generate ALL trading candidates for one index (BNF or NF).
    Premium is king — every candidate scored by premium quality.
    Dynamic is the way — calibration feeds into scoring."""
    if not chain or not chain.get('strikes') or not chain.get('atm'): return []

    is_bnf = index_key == 'BNF'
    lot_size = _CONST['BNF_LOT'] if is_bnf else _CONST['NF_LOT']
    widths = _CONST['BNF_WIDTHS'] if is_bnf else _CONST['NF_WIDTHS']
    atm = chain['atm']
    strikes = chain['strikes']
    all_strikes = [int(k) for k in chain.get('allStrikes', sorted(strikes.keys()))]
    all_set = set(all_strikes)
    step = all_strikes[1] - all_strikes[0] if len(all_strikes) > 1 else (100 if is_bnf else 50)
    cw = chain.get('callWallStrike', 0)
    pw = chain.get('putWallStrike', 0)
    atm_iv = chain.get('atmIv', vix / 100)
    vol = atm_iv / 100 if atm_iv > 1 else atm_iv
    tdte = ctx.get('bnfDTE' if is_bnf else 'nfDTE', 5)
    T = tdte / 252
    trade_mode = ctx.get('tradeMode', 'swing')

    # Range detection from context
    range_detected = (ctx.get('rangeSigma') or 999) < 0.3

    varsity = _get_varsity_filter(bias, vix, trade_mode, range_detected)
    allowed_types = varsity['primary'] + varsity['allowed']
    candidates = []

    # ═══ 1. DIRECTIONAL 2-LEG SPREADS ═══
    dir_types = [t for t in ['BEAR_CALL', 'BULL_PUT', 'BEAR_PUT', 'BULL_CALL'] if t in allowed_types]
    for stype in dir_types:
        for width in widths:
            pairs = _get_strike_pairs(stype, atm, width, step, all_strikes, spot, is_bnf, cw, pw)
            for pair in pairs:
                cand = _build_candidate(stype, pair, strikes, spot, lot_size, width, T, tdte, vol, expiry, is_bnf, vix, trade_mode)
                if not cand: continue

                # Range budget filter — debit only
                if stype in _CONST['DEBIT_TYPES']:
                    ds = _daily_sigma(spot, vix)
                    trade_sigma = _sigma_days(spot, vix, tdte)
                    if stype == 'BULL_CALL' and width > trade_sigma * 1.2: continue
                    if stype == 'BEAR_PUT' and width > trade_sigma * 1.2: continue

                cand['forces'] = _get_forces(stype, bias, vix, iv_pctl)
                cand['varsityTier'] = 'PRIMARY' if stype in varsity['primary'] else 'ALLOWED'
                cand['wallScore'] = _compute_wall_score(cand, chain, is_bnf)
                cand['gammaRisk'] = _compute_gamma_risk(cand, spot, tdte)
                cand['contextScore'] = _compute_context_score(cand, spot, tdte, vix, ctx)
                cand['directionSafe'] = cand['forces']['f1'] >= 0
                cand['capitalBlocked'] = False

                # Block high gamma ATM sells in swing mode
                if trade_mode == 'swing' and cand['isCredit'] and cand['gammaRisk'] >= 0.7:
                    cand['capitalBlocked'] = True

                candidates.append(cand)

    # ═══ 2. IRON CONDOR (intraday only — 0% overnight survival) ═══
    if 'IRON_CONDOR' in allowed_types and trade_mode != 'swing':
        for width in widths:
            rng = 2000 if is_bnf else 800
            dist_pairs = []
            for dist in range(width, rng + 1, step):
                dist_pairs.append((dist, dist))
            # Wall-anchored asymmetric
            if cw and pw and cw > atm and pw < atm:
                cw_dist = cw - atm
                pw_dist = atm - pw
                if cw_dist >= step and pw_dist >= step:
                    dist_pairs.append((cw_dist, pw_dist))
                    if cw_dist - step >= step and pw_dist - step >= step:
                        dist_pairs.append((cw_dist - step, pw_dist - step))

            seen_ic = set()
            for call_dist, put_dist in dist_pairs:
                sell_call = atm + call_dist
                buy_call = sell_call + width
                sell_put = atm - put_dist
                buy_put = sell_put - width
                if sell_call not in all_set or buy_call not in all_set: continue
                if sell_put not in all_set or buy_put not in all_set: continue
                pk = f"{sell_call}_{sell_put}_{width}"
                if pk in seen_ic: continue
                seen_ic.add(pk)

                sc = str(int(sell_call)); bc = str(int(buy_call))
                sp = str(int(sell_put)); bp = str(int(buy_put))
                ce_s = strikes.get(sc, {}).get('CE', {})
                ce_b = strikes.get(bc, {}).get('CE', {})
                pe_s = strikes.get(sp, {}).get('PE', {})
                pe_b = strikes.get(bp, {}).get('PE', {})
                if not all([ce_s, ce_b, pe_s, pe_b]): continue

                call_credit = (ce_s.get('bid', 0) or 0) - (ce_b.get('ask', 0) or 0)
                put_credit = (pe_s.get('bid', 0) or 0) - (pe_b.get('ask', 0) or 0)
                if call_credit <= 0 or put_credit <= 0: continue
                total_credit = call_credit + put_credit
                max_loss_ps = width - total_credit
                if max_loss_ps <= 0: continue

                max_profit = round(total_credit * lot_size)
                max_loss = round(max_loss_ps * lot_size)
                capital = _capital
                if max_loss > capital * _CONST['MAX_RISK_PCT'] / 100: continue
                if max_loss <= 0 or max_profit <= 0: continue

                # Probability: spot stays between breakevens
                upper_be = sell_call + total_credit
                lower_be = sell_put - total_credit
                prob_above_put = 1 - abs(_bs_delta(spot, lower_be, T, vol, 'PE'))
                prob_below_call = 1 - abs(_bs_delta(spot, upper_be, T, vol, 'CE'))
                prob = max(0, prob_above_put + prob_below_call - 1)
                if prob < _CONST['MIN_PROB']: continue
                if total_credit / width < _CONST['MIN_CREDIT_RATIO']: continue

                ev = round(prob * max_profit * 0.65 - (1 - prob) * max_loss)  # Gemini fix
                net_theta = round(abs(
                    _chain_theta(strikes, sell_call, 'CE', spot, T, vol) +
                    _chain_theta(strikes, sell_put, 'PE', spot, T, vol) -
                    _chain_theta(strikes, buy_call, 'CE', spot, T, vol) -
                    _chain_theta(strikes, buy_put, 'PE', spot, T, vol)
                ) * lot_size)

                idx = 'BNF' if is_bnf else 'NF'
                ic = {
                    'id': f"IC_{idx}_{sell_call}_{sell_put}_W{width}",
                    'type': 'IRON_CONDOR', 'width': width, 'legs': 4,
                    'sellStrike': sell_call, 'buyStrike': buy_call,
                    'sellStrike2': sell_put, 'buyStrike2': buy_put,
                    'sellType': 'CE', 'buyType': 'CE', 'sellType2': 'PE', 'buyType2': 'PE',
                    'sellLTP': ce_s.get('bid', 0), 'buyLTP': ce_b.get('ask', 0),
                    'sellLTP2': pe_s.get('bid', 0), 'buyLTP2': pe_b.get('ask', 0),
                    'netPremium': round(total_credit, 2),
                    'maxProfit': max_profit, 'maxLoss': max_loss,
                    'probProfit': round(prob, 3), 'ev': ev, 'netTheta': net_theta,
                    'isCredit': True, 'lotSize': lot_size, 'index': idx,
                    'expiry': expiry, 'tDTE': tdte,
                    'riskReward': f"1:{max_profit/max_loss:.2f}" if max_loss > 0 else '--',
                    'targetProfit': round(net_theta * 0.5) if tdte > 2 and net_theta > 0 else round(max_profit * 0.5),
                    'stopLoss': net_theta if tdte > 2 and net_theta > 0 else round(max_profit),
                    'forces': _get_forces('IRON_CONDOR', bias, vix, iv_pctl),
                    'varsityTier': 'PRIMARY' if 'IRON_CONDOR' in varsity['primary'] else 'ALLOWED',
                    'wallScore': 0, 'gammaRisk': 0, 'contextScore': 0,
                    'directionSafe': True, 'capitalBlocked': False,
                }
                ic['wallScore'] = _compute_wall_score(ic, chain, is_bnf)
                ic['gammaRisk'] = _compute_gamma_risk(ic, spot, tdte)
                # IC context: only VIX direction penalty
                fii_hist = ctx.get('fiiHistory', [])
                yv = fii_hist[0].get('vix') if fii_hist else None
                ic['contextScore'] = -0.15 if (yv and vix - yv < -0.5) else 0
                candidates.append(ic)

    # ═══ 3. IRON BUTTERFLY (intraday only, usually blocked) ═══
    if 'IRON_BUTTERFLY' in allowed_types and trade_mode != 'swing':
        for width in widths:
            sell_call = atm; buy_call = atm + width
            sell_put = atm; buy_put = atm - width
            if buy_call not in all_set or buy_put not in all_set: continue

            sc = str(int(atm))
            ce_s = strikes.get(sc, {}).get('CE', {})
            pe_s = strikes.get(sc, {}).get('PE', {})
            ce_b = strikes.get(str(int(buy_call)), {}).get('CE', {})
            pe_b = strikes.get(str(int(buy_put)), {}).get('PE', {})
            if not all([ce_s, pe_s, ce_b, pe_b]): continue

            call_credit = (ce_s.get('bid', 0) or 0) - (ce_b.get('ask', 0) or 0)
            put_credit = (pe_s.get('bid', 0) or 0) - (pe_b.get('ask', 0) or 0)
            if call_credit <= 0 or put_credit <= 0: continue
            total_credit = call_credit + put_credit
            max_loss_ps = width - total_credit
            if max_loss_ps <= 0: continue

            max_profit = round(total_credit * lot_size)
            max_loss = round(max_loss_ps * lot_size)
            if max_loss > _capital * _CONST['MAX_RISK_PCT'] / 100: continue

            upper_be = atm + total_credit
            lower_be = atm - total_credit
            prob_above = 1 - abs(_bs_delta(spot, lower_be, T, vol, 'PE'))
            prob_below = 1 - abs(_bs_delta(spot, upper_be, T, vol, 'CE'))
            prob = max(0, prob_above + prob_below - 1)
            if prob < _CONST['MIN_PROB']: continue

            ev = round(prob * max_profit * 0.65 - (1 - prob) * max_loss)  # Gemini fix
            idx = 'BNF' if is_bnf else 'NF'
            ib = {
                'id': f"IB_{idx}_{atm}_W{width}",
                'type': 'IRON_BUTTERFLY', 'width': width, 'legs': 4,
                'sellStrike': atm, 'buyStrike': buy_call,
                'sellStrike2': atm, 'buyStrike2': buy_put,
                'sellType': 'CE', 'buyType': 'CE', 'sellType2': 'PE', 'buyType2': 'PE',
                'sellLTP': ce_s.get('bid', 0), 'buyLTP': ce_b.get('ask', 0),
                'sellLTP2': pe_s.get('bid', 0), 'buyLTP2': pe_b.get('ask', 0),
                'netPremium': round(total_credit, 2),
                'maxProfit': max_profit, 'maxLoss': max_loss,
                'probProfit': round(prob, 3), 'ev': ev, 'isCredit': True,
                'lotSize': lot_size, 'index': idx, 'expiry': expiry, 'tDTE': tdte,
                'forces': _get_forces('IRON_BUTTERFLY', bias, vix, iv_pctl),
                'varsityTier': 'PRIMARY' if 'IRON_BUTTERFLY' in varsity['primary'] else 'ALLOWED',
                'wallScore': _compute_wall_score({'type': 'IRON_BUTTERFLY', 'sellStrike': atm}, chain, is_bnf),
                'gammaRisk': _compute_gamma_risk({'sellStrike': atm, 'isCredit': True}, spot, tdte),
                'contextScore': 0, 'directionSafe': True, 'capitalBlocked': False,
                'riskReward': f"1:{max_profit/max_loss:.2f}" if max_loss > 0 else '--',
            }
            candidates.append(ib)

    return candidates

# ─── RANK CANDIDATES ───

def rank_candidates(candidates, calibration=None, brain_verdict=None):
    """Varsity waterfall ranking. Premium is king — EV/capital efficiency is key metric."""
    cal = calibration or {}
    strat_cal = cal.get('strategy', {})

    def sort_key(c):
        # 0: Direction safety — F1-against always last
        safe = 0 if c.get('directionSafe', True) else 1
        # 1: Varsity tier
        tier = 0 if c.get('varsityTier') == 'PRIMARY' else 1
        # 2: Brain verdict alignment
        bv = 0
        if brain_verdict and brain_verdict.get('action') and (brain_verdict.get('confidence', 0) >= 30):
            is_buy = brain_verdict['action'] == 'BUY PREMIUM'
            is_sell = brain_verdict['action'] == 'SELL PREMIUM'
            is_debit = not c.get('isCredit')
            is_4leg = c['type'] in ('IRON_CONDOR', 'IRON_BUTTERFLY')
            if is_buy and is_debit: bv = -2
            elif is_sell and is_4leg: bv = -2
            elif is_sell and c.get('isCredit') and not is_4leg: bv = -1
            elif is_buy and is_4leg: bv = 1
            elif is_sell and is_debit: bv = 1
        # 3: Calibration win rate
        sc = strat_cal.get(c['type'], {})
        win_rate = sc['wins'] / sc['total'] if sc.get('total', 0) >= 3 else 0.5
        # 4: Force alignment
        aligned = c.get('forces', {}).get('aligned', 0)
        against = c.get('forces', {}).get('against', 0)
        # 5: Context + brain score
        ctx_score = (c.get('contextScore', 0) + c.get('brainScore', 0))
        # 6: Gamma risk
        gamma = c.get('gammaRisk', 0)
        # 7: Wall score
        wall = c.get('wallScore', 0)
        # 8: EV / peak cash
        buy_leg = (c.get('buyLTP', 0) or 0) + (c.get('buyLTP2', 0) or 0) if c.get('legs', 2) == 4 else (c.get('buyLTP', 0) or 0)
        pc = max(1, round(buy_leg * c.get('lotSize', 30)))
        eff = c.get('ev', 0) / pc if pc > 0 else 0
        # 9: Probability
        prob = c.get('probProfit', 0)
        # 10: ML score — tiebreaker. Neutralised when OOD.
        p_ml = c.get('p_ml') or 0.0
        if c.get('mlOod') and (c.get('mlOodConf') or 1.0) < 0.6:
            p_ml = 0.0

        return (safe, tier, bv, -win_rate, -aligned, against, -ctx_score, gamma, -wall, -eff, -prob, -p_ml)

    ranked = [c for c in candidates if not c.get('capitalBlocked')]
    ranked.sort(key=sort_key)
    return ranked


def _ltp(sd, strike, ot):
    k = str(int(strike))
    return ((sd.get(k) or sd.get(int(strike)) or {}).get(ot) or {}).get('ltp', 0) or 0

def _delta_val(sd, strike, ot):
    k = str(int(strike))
    d = ((sd.get(k) or sd.get(int(strike)) or {}).get(ot) or {}).get('delta', None)
    return d

def _oi_val(sd, strike, ot):
    k = str(int(strike))
    return ((sd.get(k) or sd.get(int(strike)) or {}).get(ot) or {}).get('oi', 0) or 0

def _forces_py(stype, bias, iv_pctl):
    credit = stype in ('BULL_PUT', 'BEAR_CALL', 'IRON_CONDOR', 'IRON_BUTTERFLY')
    debit = stype in ('BULL_CALL', 'BEAR_PUT')
    bull_dir = stype in ('BULL_CALL', 'BULL_PUT')
    bear_dir = stype in ('BEAR_CALL', 'BEAR_PUT')
    f1 = 0
    if bull_dir: f1 = 1 if bias in ('BULL', 'MILD_BULL', 'STRONG_BULL') else (-1 if bias in ('BEAR', 'MILD_BEAR', 'STRONG_BEAR') else 0)
    if bear_dir: f1 = 1 if bias in ('BEAR', 'MILD_BEAR', 'STRONG_BEAR') else (-1 if bias in ('BULL', 'MILD_BULL', 'STRONG_BULL') else 0)
    f2 = 1 if credit else -1
    iv_high = iv_pctl is None or iv_pctl >= 25
    if iv_high: f3 = 1 if credit else 0
    else: f3 = 1 if debit else -1
    return {'f1': f1, 'f2': f2, 'f3': f3, 'aligned': f1 + f2 + f3}

def _varsity_py(bias, iv_pctl, vix):
    iv_high = vix >= 20 or (iv_pctl is not None and iv_pctl >= 25)
    if 'BULL' in (bias or ''):
        return ['BULL_PUT', 'BULL_CALL', 'IRON_CONDOR', 'IRON_BUTTERFLY'] if iv_high else ['BULL_CALL', 'BULL_PUT', 'IRON_BUTTERFLY', 'IRON_CONDOR']
    elif 'BEAR' in (bias or ''):
        return ['BEAR_CALL', 'BEAR_PUT', 'IRON_CONDOR', 'IRON_BUTTERFLY'] if iv_high else ['BEAR_PUT', 'BEAR_CALL', 'IRON_BUTTERFLY', 'IRON_CONDOR']
    else:
        return ['IRON_BUTTERFLY', 'IRON_CONDOR', 'BULL_PUT', 'BEAR_CALL']

def _closest(all_s, target):
    return min(all_s, key=lambda x: abs(x - target))

def _build_cand_py(stype, atm, width, step, all_s, sd, spot, lot, daily_sig, idx, expiry, dte, forces, capital, chain):
    try:
        sell_k = sell_t = buy_k = buy_t = None
        sell_k2 = sell_t2 = buy_k2 = buy_t2 = None

        if stype == 'BULL_CALL':
            buy_k, sell_k, buy_t, sell_t = atm, _closest(all_s, atm + width), 'CE', 'CE'
        elif stype == 'BEAR_PUT':
            buy_k, sell_k, buy_t, sell_t = atm, _closest(all_s, atm - width), 'PE', 'PE'
        elif stype == 'BULL_PUT':
            sell_k = _closest(all_s, atm - round(0.5 * daily_sig / step) * step)
            buy_k = _closest(all_s, sell_k - width)
            buy_t = sell_t = 'PE'
        elif stype == 'BEAR_CALL':
            sell_k = _closest(all_s, atm + round(0.5 * daily_sig / step) * step)
            buy_k = _closest(all_s, sell_k + width)
            buy_t = sell_t = 'CE'
        elif stype == 'IRON_BUTTERFLY':
            sell_k, buy_k, sell_t, buy_t = atm, _closest(all_s, atm + width), 'CE', 'CE'
            sell_k2, buy_k2, sell_t2, buy_t2 = atm, _closest(all_s, atm - width), 'PE', 'PE'
        elif stype == 'IRON_CONDOR':
            sell_k = _closest(all_s, atm + round(0.5 * daily_sig / step) * step)
            buy_k = _closest(all_s, sell_k + width)
            sell_k2 = _closest(all_s, atm - round(0.5 * daily_sig / step) * step)
            buy_k2 = _closest(all_s, sell_k2 - width)
            sell_t = buy_t = 'CE'; sell_t2 = buy_t2 = 'PE'

        if sell_k is None or buy_k is None: return None

        sl = _ltp(sd, sell_k, sell_t); bl = _ltp(sd, buy_k, buy_t)
        if sl <= 0 or bl <= 0: return None

        sl2 = bl2 = 0
        if sell_k2 is not None:
            sl2 = _ltp(sd, sell_k2, sell_t2); bl2 = _ltp(sd, buy_k2, buy_t2)
            if sl2 <= 0 or bl2 <= 0: return None

        credit = stype in ('BULL_PUT', 'BEAR_CALL', 'IRON_CONDOR', 'IRON_BUTTERFLY')
        if stype in ('IRON_BUTTERFLY', 'IRON_CONDOR'):
            net = (sl + sl2) - (bl + bl2)
        elif credit: net = sl - bl
        else: net = bl - sl

        if net <= 0: return None

        if credit: mp = round(net * lot); ml = round((width - net) * lot)
        else: mp = round((width - net) * lot); ml = round(net * lot)

        if ml <= 0 or mp <= 0: return None
        if ml > capital * 0.10: return None

        sd_val = _delta_val(sd, sell_k, sell_t)
        prob = max(0.50, min(0.97, (1 - abs(sd_val)) if sd_val is not None else 0.65))

        ev = prob * mp - (1 - prob) * ml
        if ev <= 0: return None

        cand = {
            'id': f"{idx}_{stype}_{sell_k}_{width}_py",
            'index': idx, 'type': stype, 'expiry': expiry, 'tDTE': dte,
            'sellStrike': sell_k, 'sellType': sell_t, 'sellLTP': round(sl, 2),
            'buyStrike': buy_k, 'buyType': buy_t, 'buyLTP': round(bl, 2),
            'width': width, 'netPremium': round(net, 2), 'isCredit': credit,
            'maxProfit': mp, 'maxLoss': ml, 'riskReward': round(mp/ml, 2),
            'probProfit': round(prob, 3), 'pRange': round(prob, 3),
            'ev': round(ev), 'ev1k': round(ev / (ml / 1000)) if ml > 0 else 0,
            'forces': forces, 'varsityTier': 1 if forces['aligned'] == 3 else 2,
            'source': 'brain'
        }
        if sell_k2 is not None:
            cand.update({'sellStrike2': sell_k2, 'sellType2': sell_t2, 'sellLTP2': round(sl2, 2),
                         'buyStrike2': buy_k2, 'buyType2': buy_t2, 'buyLTP2': round(bl2, 2)})
        # b115: Breakeven — real danger lines
        if stype in ('IRON_BUTTERFLY', 'IRON_CONDOR'):
            cand['beUpper'] = round(sell_k + net)
            cand['beLower'] = round((sell_k2 if sell_k2 else sell_k) - net)
        elif credit:
            if sell_t == 'CE': cand['beUpper'] = round(sell_k + net)
            else: cand['beLower'] = round(sell_k - net)
        else:
            if buy_t == 'CE': cand['beUpper'] = round(buy_k + net)
            else: cand['beLower'] = round(buy_k - net)
        return cand
    except: return None

def generate_candidates_py(ctx, effective_bias):
    """Phase 3: Brain generates trade candidates directly from chain data."""
    eb = effective_bias or {}
    bias = eb.get('bias', 'NEUTRAL')
    iv_pctl = ctx.get('ivPercentile', None)
    vix = ctx.get('vix', 18) or 18
    capital = ctx.get('capital', 250000)
    trade_mode = ctx.get('tradeMode', 'intraday')
    allowed = _varsity_py(bias, iv_pctl, vix)

    candidates = []
    for idx in ['NF', 'BNF']:
        chain = ctx.get('bnfChain' if idx == 'BNF' else 'nfChain', {})
        if not chain: continue
        atm = chain.get('atm')
        sd = chain.get('strikes', {})
        all_s_raw = chain.get('allStrikes', list(sd.keys()))
        if not atm or not sd or not all_s_raw: continue
        try:
            all_s = sorted([int(k) for k in all_s_raw])
        except: continue
        if len(all_s) < 4: continue
        step = all_s[1] - all_s[0] if len(all_s) > 1 else (100 if idx == 'BNF' else 50)
        spot = chain.get('spot', atm)
        lot = 30 if idx == 'BNF' else 65
        atm_iv = chain.get('atmIv', 0) or 0
        daily_sig = (atm_iv / 100) * spot / 15.87 if atm_iv > 0 else step * 3
        expiry = chain.get('expiry', ctx.get('bnfExpiry' if idx == 'BNF' else 'nfExpiry', ''))
        dte = ctx.get('bnfDTE' if idx == 'BNF' else 'nfDTE', 4)
        widths = [400, 500, 600, 800, 1000] if idx == 'BNF' else [100, 150, 200, 250, 300, 400]

        for stype in allowed:
            if stype in ('IRON_CONDOR', 'IRON_BUTTERFLY') and trade_mode == 'swing' and (dte or 0) > 2:
                continue
            forces = _forces_py(stype, bias, iv_pctl)
            if forces['aligned'] < 1: continue
            for width in widths:
                c = _build_cand_py(stype, atm, width, step, all_s, sd, spot, lot,
                                   daily_sig, idx, expiry, dte, forces, capital, chain)
                if c: candidates.append(c)

    candidates.sort(key=lambda c: c.get('ev', 0), reverse=True)
    return candidates[:25]

def analyze(poll_json, trades_json, baseline_json, open_trades_json, candidates_json, strike_oi_json, context_json='{}'):
    polls = json.loads(poll_json)
    closed_trades = json.loads(trades_json) if trades_json else []
    baseline = json.loads(baseline_json) if baseline_json else {}
    open_trades = json.loads(open_trades_json) if open_trades_json else []
    candidates = json.loads(candidates_json) if candidates_json else []
    strike_oi = json.loads(strike_oi_json) if strike_oi_json else {}
    ctx = json.loads(context_json) if context_json else {}

    result = {"verdict": None, "market": [], "positions": {}, "candidates": {}, "timing": [], "risk": []}
    if len(polls) < 3:
        return json.dumps(result)

    # Set capital from JS context (single source of truth: C.CAPITAL)
    global _capital
    _capital = ctx.get('capital', 110000)

    build_calibration(closed_trades)
    regime = detect_regime(polls, baseline)

    # Market (existing 8 + new 7)
    for fn in [pcr_velocity, oi_wall_shift, vix_momentum, spot_exhaustion,
               regime_detector, futures_premium_trend, oi_velocity, institutional_clock]:
        try:
            r = fn(polls, baseline)
            if r: result["market"].append(r)
        except Exception as e:
            print(f"DEBUG: Market insight {fn.__name__} failed: {e}")
    # New context-aware market functions
    for fn in [signal_coherence, max_pain_gravity, fii_trend, nf_bnf_divergence,
               day_range_position, wall_freshness, yesterday_signal_prior]:
        try:
            r = fn(polls, ctx)
            if r: result["market"].append(r)
        except Exception as e:
            print(f"DEBUG: Context insight {fn.__name__} failed: {e}")
    # b92: chain_intelligence returns LIST (was single dict)
    try:
        ci_insights = chain_intelligence(polls, ctx)
        if ci_insights:
            result["market"].extend(ci_insights)
    except: pass

    # Positions — verdict + insights
    for t in open_trades:
        tid = t.get("id", "")
        ins = []
        soi = strike_oi.get(tid, [])
        for fn in [position_wall_proximity, position_momentum_threat,
                   position_regime_fit, position_vix_headwind, position_book_signal]:
            try:
                r = fn(t, polls, baseline, regime, soi)
                if r: ins.append(r)
            except Exception as e:
                print(f"DEBUG: Position insight {fn.__name__} failed for tid {tid}: {e}")
        try:
            r = position_gamma_alert(t, polls, soi)
            if r: ins.append(r)
        except Exception as e:
            print(f"DEBUG: position_gamma_alert failed for tid {tid}: {e}")
        pv = position_verdict(t, ins, regime, ctx)
        result["positions"][tid] = {"verdict": pv, "insights": ins}

    # Candidates — existing + liquidity + pattern match + b92 risk evaluation
    for c in candidates:
        cid = c.get("id", "")
        ins = []
        for fn in [candidate_flow_alignment, candidate_wall_protection, candidate_regime_fit, candidate_pattern_match]:
            try:
                r = fn(c, polls, baseline, regime)
                if r: ins.append(r)
            except: pass
        try:
            r = candidate_liquidity(c, ctx)
            if r: ins.append(r)
        except Exception as e:
            print(f"DEBUG: candidate_liquidity failed for cid {cid}: {e}")
        # b92: Deep risk evaluation (returns LIST) — cost trap, conflict, R:R, force coherence
        try:
            risk_ins = evaluate_candidate_risk(c, ctx, open_trades, regime)
            if risk_ins: ins.extend(risk_ins)
        except Exception as e:
            print(f"DEBUG: evaluate_candidate_risk failed for cid {cid}: {e}")
        if ins: result["candidates"][cid] = ins

    # Timing — existing + DTE urgency
    for fn in [timing_entry_window, timing_wait_signal]:
        try:
            r = fn(polls, baseline, regime)
            if r: result["timing"].append(r)
        except Exception as e:
            print(f"DEBUG: Timing insight {fn.__name__} failed: {e}")
    try:
        r = dte_urgency(polls, ctx)
        if r: result["timing"].append(r)
    except Exception as e:
        print(f"DEBUG: dte_urgency failed: {e}")

    # Risk — existing + daily PnL
    for fn in [risk_kelly_headroom, risk_regime_shift, risk_exit_analysis, risk_factor_importance, risk_streak_warning]:
        try:
            r = fn(polls, baseline, open_trades, closed_trades)
            if r: result["risk"].append(r)
        except Exception as e:
            print(f"DEBUG: Risk insight {fn.__name__} failed: {e}")
    try:
        r = daily_pnl_check(polls, ctx)
        if r: result["risk"].append(r)
    except Exception as e:
        print(f"DEBUG: daily_pnl_check failed: {e}")

    # ═══ THE VERDICT ═══
    all_insights = result["market"] + result["timing"] + result["risk"]
    try:
        result["verdict"] = synthesize_verdict(all_insights, regime, ctx, polls, baseline, candidates, result.get("candidates", {}))
    except Exception as e:
        print(f"DEBUG: synthesize_verdict failed: {e}")

    # b97: Effective bias — Bayesian decay of morning prior with intraday evidence
    try:
        result["effective_bias"] = compute_effective_bias(polls, baseline, ctx, regime)
    except Exception as e:
        print(f"DEBUG: compute_effective_bias failed: {e}")

    # Phase 3: Generate trading candidates from chain data
    # Premium is king — every candidate scored by premium quality
    # Dynamic is the way — calibration feeds into scoring
    try:
        eb = result.get("effective_bias", {})
        active_bias = {'bias': eb.get('bias', 'NEUTRAL'), 'strength': eb.get('strength', ''), 'net': eb.get('net', 0)} if eb else ctx.get('morningBias', {'bias': 'NEUTRAL', 'strength': '', 'net': 0})
        vixs = get_vix_vals(polls)
        cur_vix = vixs[-1] if vixs else 20
        iv_pctl = ctx.get('ivPercentile', 50)
        all_cands = []
        for chain_key, idx_key in [('bnfChain', 'BNF'), ('nfChain', 'NF')]:
            chain_data = ctx.get(chain_key)
            if chain_data and chain_data.get('strikes') and chain_data.get('atm'):
                spot_key = 'bnfSpot' if idx_key == 'BNF' else 'nfSpot'
                spot = None
                for p in reversed(polls):
                    s = p.get('bnf' if idx_key == 'BNF' else 'nf')
                    if s: spot = s; break
                if not spot: spot = baseline.get(spot_key, 0)
                if spot > 0:
                    expiry_key = 'bnfExpiry' if idx_key == 'BNF' else 'nfExpiry'
                    expiry = ctx.get(expiry_key, '')
                    cands = generate_candidates(chain_data, spot, idx_key, expiry, cur_vix, active_bias, iv_pctl, ctx)
                    all_cands.extend(cands)
        if all_cands:
            brain_verdict = result.get('verdict')
            ranked = rank_candidates(all_cands, _calibration, brain_verdict)

            # ── SPLICE 4: Enrich ML with live context, BEFORE watchlist build ──
            engine = _ml_load_if_needed()
            if engine is not None and ranked:
                try:
                    last = polls[-1] if polls else {}
                    gap_s  = last.get('gapSigma') or last.get('gap_sigma') or 0
                    move_s = last.get('moveSigma') or last.get('move_sigma') or 0
                    drs    = last.get('dayRangeSigma') or last.get('day_range_sigma') or 0
                    dd     = str(last.get('dayDirection') or last.get('day_direction') or 'FLAT').upper()
                    dr     = str(last.get('dayRange') or last.get('day_range') or 'NORMAL').upper()
                    consec = last.get('consecDays') or last.get('consec_days') or 0
                    wday   = last.get('weekday') or 0
                    lv     = last.get('vix') or cur_vix or 17
                    for c in ranked:
                        try:
                            enriched = {
                                'strategy': c['type'], 'mode': ctx.get('tradeMode', 'intraday'),
                                'vix': lv, 'sigma_away': c.get('sigmaOTM') or 0,
                                'gap_sigma': gap_s, 'dte': c.get('tDTE', 3),
                                'entry_credit': c.get('netPremium', 0), 'width': c['width'],
                                'move_sigma': move_s, 'day_range_sigma': drs,
                                'consec_days': consec, 'max_profit': c.get('maxProfit', 0),
                                'max_loss': c.get('maxLoss', 1), 'legs': c.get('legs', 2),
                                'is_credit': c.get('isCredit', True),
                                'vix_regime': ('HIGH (20-25)' if lv >= 20 else 'LOW (<15)' if lv < 15 else 'NORMAL (15-20)'),
                                'day_group': 'Thu-Fri' if wday >= 3 else 'Mon-Wed',
                                'day_direction': dd, 'day_range': dr,
                                'day_vix': 'HIGH' if lv >= 20 else 'LOW' if lv < 15 else 'NORMAL',
                                'weekday': wday,
                            }
                            p2, reg2, d2 = engine.predict(enriched)
                            c['p_ml']         = round(p2, 4)
                            c['mlAction']     = d2.get('action', 'WATCH')
                            c['mlRegime']     = reg2
                            c['mlEdge']       = d2.get('edge', 0.0)
                            c['mlOod']        = d2.get('ood', False)
                            c['mlOodConf']    = d2.get('ood_conf', 1.0)
                            c['mlOodWarn']    = d2.get('ood_warns', [])
                            c['mlOodBlocked'] = d2.get('ood_blocked', False)
                        except Exception:
                            pass
                    ranked = rank_candidates(ranked, _calibration, brain_verdict)
                except Exception:
                    pass

            # Watchlist: top 6 + diverse picks per index
            watchlist = ranked[:6]
            seen_ids = set(c['id'] for c in watchlist)
            for idx in ['BNF', 'NF']:
                seen_types = set()
                for c in ranked:
                    if c['index'] == idx and not c.get('capitalBlocked') and c['type'] not in seen_types and c['id'] not in seen_ids:
                        seen_types.add(c['type'])
                        seen_ids.add(c['id'])
                        watchlist.append(c)
                    if len(seen_types) >= 5: break
            result["generated_candidates"] = ranked
            result["watchlist"] = watchlist
            result["candidate_stats"] = {
                "total": len(all_cands),
                "ranked": len(ranked),
                "watchlist": len(watchlist),
                "by_type": {}
            }
            for c in all_cands:
                t = c['type']
                result["candidate_stats"]["by_type"][t] = result["candidate_stats"]["by_type"].get(t, 0) + 1
    except Exception as e:
        result["candidate_error"] = str(e)

    # Phase 3: Brain candidate generation using effective_bias
    try:
        result["generated_candidates"] = generate_candidates_py(ctx, result.get("effective_bias"))
    except Exception as e:
        result["generated_candidates"] = []
        result["candidate_error"] = str(e)

    return json.dumps(result)
