import json, math, os as _os, statistics, hashlib, time
from datetime import datetime, timezone, timedelta

# ── ML Engine bootstrap (silent-fail if model not yet trained) ───────────
_ML_ENGINE     = None
_ML_MODEL_PATH = '/data/data/com.marketradar.app/files/ml_model.json'
_TEMPORAL_ENGINE = None
_TEMPORAL_CHECKED = False
_TEMPORAL_MODEL_PATH = '/data/data/com.marketradar.app/files/temporal_model.json'
_TEMPORAL_MIN_VAL_ACC = 0.60
_STAGE2A_TABLE_CACHE = {'path': None, 'table': None, 'error': None}
_STAGE2A_DEFAULT_MODE = 'shadow'
_STAGE2A_MIN_PRIOR_BUCKET_N = 5

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

def _ml_invalidate():
    """Forces ML engine reload on next call.
    Used by Kotlin hot-reload path (MarketMLService.kt) after importlib.reload(ml_engine)."""
    global _ML_ENGINE, _TEMPORAL_ENGINE, _TEMPORAL_CHECKED
    _ML_ENGINE = None
    _TEMPORAL_ENGINE = None
    _TEMPORAL_CHECKED = False

def _temporal_load_if_ready():
    """Load temporal model only when validation quality clears the activation gate."""
    global _TEMPORAL_ENGINE, _TEMPORAL_CHECKED
    if _TEMPORAL_ENGINE is not None:
        return _TEMPORAL_ENGINE
    if _TEMPORAL_CHECKED:
        return None
    _TEMPORAL_CHECKED = True
    try:
        import ml_temporal as _mlt
        if not _os.path.exists(_TEMPORAL_MODEL_PATH):
            return None
        engine = _mlt.load_temporal(_TEMPORAL_MODEL_PATH)
        if not getattr(engine, 'trained', False):
            return None
        if float(getattr(engine, 'val_acc', 0.0) or 0.0) < _TEMPORAL_MIN_VAL_ACC:
            return None
        _TEMPORAL_ENGINE = engine
        return _TEMPORAL_ENGINE
    except Exception:
        return None

def _ml_score(candidate_dict):
    """Score candidate with ML engine. Returns {} if model not loaded.
    mlAction='BLOCKED' = model has zero training data for this scenario.
    mlAction='UNSURE' = model is weak/OOD/ambiguous; brain math must fallback."""
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
            'ml_ood_flag':   detail.get('ood_flag', detail.get('ood', False)),
            'ml_ood_conf':   detail.get('ood_conf', 1.0),
            'ml_ood_warn':   detail.get('ood_warns', []),
            'ml_ood_blocked':detail.get('ood_blocked', False),
            'ml_unsure':     detail.get('ml_unsure', False),
            'ml_unsure_reason': detail.get('unsure_reason', []),
            'decision_source': detail.get('decision_source', 'ML_ADVISORY'),
        }
    except Exception:
        return {}


def _stage2a_normalize_mode(raw_mode):
    mode = str(raw_mode or _STAGE2A_DEFAULT_MODE).strip().lower()
    if mode in ('live', 'shadow', 'off'):
        return mode
    return _STAGE2A_DEFAULT_MODE


def _stage2a_vix_bucket(vix_value):
    vix = _float_or_none(vix_value)
    if vix is None:
        return 'unknown'
    if vix < 12:
        return 'VIX_LT_12'
    if vix < 14:
        return 'VIX_12_14'
    if vix < 16:
        return 'VIX_14_16'
    if vix < 18:
        return 'VIX_16_18'
    return 'VIX_18_PLUS'


def _stage2a_dte_bucket(candidate, ctx=None):
    ctx = ctx or {}
    t_dte = _float_or_none(candidate.get('tDTE'))
    if t_dte is None:
        expiry_text = str(candidate.get('expiry') or '').strip()
        session_date = str(ctx.get('today_ist') or ctx.get('session_date') or ctx.get('sessionDate') or '').strip()
        try:
            expiry_dt = datetime.strptime(expiry_text, "%Y-%m-%d").date() if expiry_text else None
            session_dt = datetime.strptime(session_date, "%Y-%m-%d").date() if session_date else None
            if expiry_dt and session_dt:
                t_dte = max((expiry_dt - session_dt).days, 0)
        except Exception:
            t_dte = None
    if t_dte is None:
        return 'unknown'
    dte = int(max(round(t_dte), 0))
    if dte <= 0:
        return 'DTE_0'
    if dte == 1:
        return 'DTE_1'
    if dte <= 3:
        return 'DTE_2_3'
    if dte <= 7:
        return 'DTE_4_7'
    return 'DTE_8_PLUS'


def _stage2a_load_teacher_table(path):
    global _STAGE2A_TABLE_CACHE
    if not path:
        return None, 'teacher_table_path_missing'
    if (
        _STAGE2A_TABLE_CACHE.get('path') == path
        and _STAGE2A_TABLE_CACHE.get('table') is not None
    ):
        return _STAGE2A_TABLE_CACHE.get('table'), _STAGE2A_TABLE_CACHE.get('error')
    table = None
    err = None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
        rows = payload.get('rows') if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError('teacher_table_rows_missing')
        by_key = {}
        min_prior = int(payload.get('min_prior_bucket_n') or _STAGE2A_MIN_PRIOR_BUCKET_N)
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (
                str(row.get('strategy_type') or ''),
                str(row.get('regime_bucket') or ''),
                str(row.get('vix_bucket') or ''),
                str(row.get('dte_bucket') or ''),
            )
            by_key[key] = {
                'n': int(row.get('n') or 0),
                'avg_r': _float_or_none(row.get('avg_r')),
                'success_rate_pct': _float_or_none(row.get('success_rate_pct')),
                'low_confidence': bool(row.get('low_confidence')),
            }
        table = {
            'schema': payload.get('schema'),
            'source': payload.get('source'),
            'source_scope': payload.get('source_scope'),
            'generated_at_utc': payload.get('generated_at_utc'),
            'min_prior_bucket_n': min_prior,
            'row_count': len(rows),
            'by_key': by_key,
        }
    except Exception as exc:
        err = str(exc)
        table = None
    _STAGE2A_TABLE_CACHE = {'path': path, 'table': table, 'error': err}
    return table, err


def _stage2a_annotate_candidates(candidates, ctx):
    mode = _stage2a_normalize_mode(ctx.get('stage2a_mode'))
    requested_min_prior = int(ctx.get('stage2a_min_prior_bucket_n') or _STAGE2A_MIN_PRIOR_BUCKET_N)
    table_path = str(ctx.get('stage2a_teacher_table_path') or '').strip()
    table, table_error = _stage2a_load_teacher_table(table_path) if mode != 'off' else (None, None)
    table_min_prior = int((table or {}).get('min_prior_bucket_n') or requested_min_prior)
    min_prior = max(requested_min_prior, table_min_prior)
    entry_vix = _float_or_none(
        ctx.get('vix')
        or ctx.get('indiaVix')
        or ctx.get('entry_vix')
        or ctx.get('snapshot_vix')
    )
    teacher_config = _teacher_default_config()
    regime_bucket = _teacher_regime_bucket(entry_vix, teacher_config)
    summary = {
        'mode': mode,
        'table_ready': bool(table),
        'table_error': table_error,
        'table_path': table_path,
        'table_rows': int((table or {}).get('row_count') or 0),
        'min_prior_bucket_n': min_prior,
        'entry_vix': entry_vix,
        'regime_bucket': regime_bucket,
        'candidate_count': 0,
        'covered_count': 0,
        'positive_count': 0,
        'thin_count': 0,
        'unseen_count': 0,
        'shadow_changes_top': False,
        'live_changes_top': False,
        'hard_wait_triggered': False,
        'hard_wait_reason': None,
        'deterministic_top_candidate_id': None,
        'shadow_top_candidate_id': None,
        'live_top_candidate_id': None,
    }
    if not isinstance(candidates, list):
        return summary

    summary['candidate_count'] = len(candidates)
    table_map = (table or {}).get('by_key') or {}
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        strategy_type = str(cand.get('type') or '')
        vix_bucket = _stage2a_vix_bucket(entry_vix)
        dte_bucket = _stage2a_dte_bucket(cand, ctx)
        bucket_key = f"{strategy_type}|{regime_bucket}|{vix_bucket}|{dte_bucket}"
        bucket = table_map.get((strategy_type, regime_bucket, vix_bucket, dte_bucket))
        teacher_n = int(bucket.get('n') or 0) if bucket else 0
        teacher_r = _float_or_none(bucket.get('avg_r')) if bucket else None
        coverage = 'off'
        if mode != 'off':
            if bucket is None:
                coverage = 'unseen'
                summary['unseen_count'] += 1
            elif teacher_n < min_prior:
                coverage = 'thin'
                summary['thin_count'] += 1
            elif teacher_r is not None and teacher_r > 0:
                coverage = 'covered_positive'
                summary['covered_count'] += 1
                summary['positive_count'] += 1
            else:
                coverage = 'covered_negative'
                summary['covered_count'] += 1
        cand['teacher_bucket_key'] = bucket_key
        cand['teacher_regime_bucket'] = regime_bucket
        cand['teacher_vix_bucket'] = vix_bucket
        cand['teacher_dte_bucket'] = dte_bucket
        cand['teacher_bucket_n'] = teacher_n
        cand['teacher_r_score'] = round(teacher_r, 4) if teacher_r is not None else None
        cand['teacher_success_rate_pct'] = _float_or_none(bucket.get('success_rate_pct')) if bucket else None
        cand['teacher_low_confidence'] = bool(bucket.get('low_confidence')) if bucket else None
        cand['teacher_coverage'] = coverage
        cand['teacher_recommendable'] = coverage == 'covered_positive'
        cand['teacher_ranking_eligible'] = coverage in ('covered_positive', 'covered_negative')
    return summary

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
    except Exception as e:
        # BR2: Silent return 0 misclassified time. Log for debugging.
        if t_str:  # suppress empty-string noise
            print(f"get_time_mins: parse '{t_str}': {e}")
        return 0

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
    def series_stats(key, base_key):
        vals = [p.get(key) for p in recent if p.get(key)]
        if len(vals) < 3:
            return None
        hi, lo = max(vals), min(vals)
        rng = hi - lo
        base_vix = baseline.get('vix', 15)
        base_spot = baseline.get(base_key, vals[0])
        daily_sigma = base_spot * (base_vix / 100) / math.sqrt(252) if base_spot > 0 else 300
        range_sigma = rng / daily_sigma if daily_sigma > 0 else 0
        direction_votes = 0
        for i in range(1, len(vals)):
            if vals[i] > vals[i-1]: direction_votes += 1
            elif vals[i] < vals[i-1]: direction_votes -= 1
        trend_pct = abs(direction_votes) / (len(vals) - 1) if len(vals) > 1 else 0
        return {"sigma": range_sigma, "direction": direction_votes, "trend_pct": trend_pct}

    bnf_stats = series_stats('bnf', 'bnfSpot')
    nf_stats = series_stats('nf', 'nfSpot')
    if not bnf_stats and not nf_stats:
        return {"type": "unknown", "sigma": 0, "direction": 0, "trend_pct": 0}
    primary = bnf_stats or nf_stats
    range_sigma = primary["sigma"]
    direction_votes = primary["direction"]
    trend_pct = primary["trend_pct"]
    nf_sigma = nf_stats["sigma"] if nf_stats else None
    nf_direction = nf_stats["direction"] if nf_stats else None
    if range_sigma < 0.25 and trend_pct < 0.6:
        rtype = "range"
    elif range_sigma > 0.6 and trend_pct > 0.6:
        rtype = "trend"
    elif range_sigma > 0.8:
        rtype = "choppy"
    else:
        rtype = "mild_trend"
    if bnf_stats and nf_stats and abs((bnf_stats["direction"] or 0) - (nf_stats["direction"] or 0)) >= 4:
        rtype = "divergence"
    return {
        "type": rtype,
        "sigma": range_sigma,
        "direction": direction_votes,
        "trend_pct": trend_pct,
        "nf_sigma": nf_sigma,
        "nf_direction": nf_direction,
    }

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

def _synthesize_market_phase(regime, ctx):
    """Returns {id, label, detail, hint} from detect_regime output."""
    rtype = regime.get('type', 'unknown')
    direction = regime.get('direction', 0)
    sigma = regime.get('sigma', 0)
    trend_pct = regime.get('trend_pct', 0)

    if rtype == 'range':
        return {
            'id': 'RANGE',
            'label': 'Range-bound',
            'detail': f'{sigma:.2f}σ range',
            'hint': 'Credit-friendly. Watch for breakout.',
            'sigma': sigma,
            'trend_pct': trend_pct
        }
    elif rtype == 'trend':
        d = 'UP' if direction > 0 else 'DOWN'
        return {
            'id': f'TREND_{d}',
            'label': f'Trending {d.lower()}',
            'detail': f'{sigma:.2f}σ, {trend_pct:.1f}% trend',
            'hint': 'Debit-friendly in trend direction.',
            'sigma': sigma,
            'trend_pct': trend_pct
        }
    elif rtype == 'choppy':
        return {
            'id': 'CHOPPY',
            'label': 'Choppy',
            'detail': f'{sigma:.2f}σ, no clear direction',
            'hint': 'Avoid directional plays. IB/IC if intraday.',
            'sigma': sigma,
            'trend_pct': trend_pct
        }
    elif rtype == 'divergence':
        return {
            'id': 'DIVERGENCE',
            'label': 'NF-BNF divergence',
            'detail': f'BNF {direction:+d}, NF {regime.get("nf_direction", 0):+d}',
            'hint': 'Reduce directional confidence until indices confirm.',
            'sigma': sigma,
            'trend_pct': trend_pct
        }
    return {'id': 'UNKNOWN', 'label': 'Unknown', 'detail': '', 'hint': ''}

def institutional_regime(ctx):
    """Phase B: classify FII/DII institutional positioning regime.
    Replaces JS computeInstitutionalRegime(). Returns dict or None.
    """
    morning = ctx.get('morning_input') or {}
    fii_cash = float(morning.get('fiiCash') or 0)
    dii_cash = float(morning.get('diiCash') or 0)
    fii_idx_fut = float(morning.get('fiiIdxFut') or 0)
    fii_stk_fut = float(morning.get('fiiStkFut') or 0)

    # Skip if no institutional data entered
    if not morning.get('diiCash') and not morning.get('fiiIdxFut') and not morning.get('fiiStkFut'):
        return None

    # Derived metrics
    abs_fii_cash = abs(fii_cash)
    absorption_ratio = round(dii_cash / abs_fii_cash, 2) if abs_fii_cash > 0 else None
    fii_deriv_net = int(fii_idx_fut + fii_stk_fut)
    is_rotation = fii_cash < -500 and fii_stk_fut > 200
    is_panic = fii_cash < -500 and absorption_ratio is not None and absorption_ratio < 0.5 and fii_stk_fut < 0
    is_accumulation = fii_cash > 500 and dii_cash > 0
    is_repositioning = fii_cash < -500 and fii_idx_fut > 0

    # Classify regime
    regime, regime_color, regime_detail, credit_confidence = None, None, None, None

    if is_panic:
        regime = 'PANIC'
        regime_color = 'var(--danger)'
        regime_detail = 'No floor — DII not absorbing, FII selling everything. Avoid credit near ATM.'
        credit_confidence = 'LOW'
    elif is_repositioning:
        regime = 'REPOSITIONING'
        regime_color = 'var(--warn)'
        regime_detail = 'FII selling cash but buying futures — setting up for bounce. Direction may flip.'
        credit_confidence = 'MEDIUM'
    elif is_rotation:
        regime = 'ROTATION'
        regime_color = 'var(--green)'
        regime_detail = 'Orderly rotation — selling index, buying stocks. Floor exists. Credit spreads safe.'
        credit_confidence = 'HIGH'
    elif is_accumulation:
        regime = 'ACCUMULATION'
        regime_color = 'var(--green)'
        regime_detail = 'FII + DII both buying. Rally likely. Credit bull spreads ride it.'
        credit_confidence = 'HIGH'
    elif absorption_ratio is not None and absorption_ratio >= 0.8 and fii_cash < -500:
        regime = 'DEFENDED'
        regime_color = '#2196F3'
        regime_detail = f'DII absorbing {int(absorption_ratio * 100)}% of FII selling. Support holding.'
        credit_confidence = 'HIGH'
    else:
        regime = 'NORMAL'
        regime_color = 'var(--text-muted)'
        regime_detail = 'No extreme institutional pattern detected.'
        credit_confidence = 'MEDIUM'

    return {
        'regime': regime, 'regimeColor': regime_color, 'regimeDetail': regime_detail,
        'creditConfidence': credit_confidence,
        'fiiCash': fii_cash, 'diiCash': dii_cash, 'fiiIdxFut': fii_idx_fut, 'fiiStkFut': fii_stk_fut,
        'absorptionRatio': absorption_ratio, 'fiiDerivNet': fii_deriv_net, 'isRotation': is_rotation
    }

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
    if trade.get('strategy_type') in ('BULL_CALL', 'BEAR_PUT'):
        return None  # BR15: approaching sell = max profit for debit, not danger
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
    elif ctype == 'BULL_PUT' and pw:  # BR20: explicit — Bull Call is debit, doesn't check put wall
        dist = sell - pw
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
    prob = cand.get('trueProb', cand.get('probProfit', 0.5)) or 0.5
    forces = cand.get('forces') or {}
    profile = ctx.get('bnfProfile' if idx == 'BNF' else 'nfProfile') or {}
    ctx_score = cand.get('contextScore')
    if ctx_score is None:
        ctx_score = profile.get('contextScore', 0)
    # 1. COST TRAP — est. cost eats too much of max profit
    if max_p > 0 and est_cost > 0:
        cost_ratio = est_cost / max_p
        if cost_ratio > 0.30:
            net = max_p - est_cost
            insights.append({"icon": "💸", "label": f"Cost trap ({cost_ratio*100:.0f}% of profit)",
                    "detail": f"Net after cost: ₹{net:.0f}. Risk ₹{max_l:.0f} for ₹{net:.0f}.",
                    "impact": "caution", "strength": 5 if cost_ratio > 0.5 else 4})
    
    # 2. R:R SANITY — maxLoss > 2× maxProfit is dangerous
    if max_p > 0 and max_l > 0:
        rr = max_p / max_l
        if rr < 0.5:
            insights.append({"icon": "⚖️", "label": f"Poor R:R (1:{1/rr:.1f})",
                    "detail": f"Risk ₹{max_l:.0f} to make ₹{max_p:.0f}. Need {1/(rr+0.001):.0f}x wins per loss.",
                    "impact": "caution", "strength": 3})
    
    # 3. OPEN TRADE CONFLICT — already have a struggling position in same type/index?
    # BR22: Check for struggling trade and index exposure separately.
    struggling_found = False
    for t in (open_trades or []):
        if t.get('index_key') != idx or t.get('paper'): continue
        if (t.get('current_pnl') or 0) < 0 or (t.get('controlIndex') or 0) < -20:
            struggling_found = True; break
    
    if struggling_found:
        insights.append({"icon": "🔄", "label": f"Existing {idx} position struggling",
                "detail": "Market currently fighting your open trade. Don't double down until bounce confirmed.",
                "impact": "caution", "strength": 4})
    elif any(t.get('index_key') == idx and not t.get('paper') for t in (open_trades or [])):
        insights.append({"icon": "📋", "label": f"Already in {idx}",
                "detail": f"Adding more {idx} exposure. Ensure total capital/index limit safe.",
                "impact": "neutral", "strength": 2})
    
    # 4. FORCE COHERENCE — forces say one thing, context says another
    aligned = forces.get('aligned', 0)
    if aligned >= 3 and ctx_score < -0.3:
        insights.append({"icon": "⚠️", "label": "Forces aligned but context negative",
                "detail": f"3/3 forces but contextScore {ctx_score:.2f}. Gap/VIX conflict?",
                "impact": "caution", "strength": 3})
    
    # 5. WIDTH ADEQUACY — narrow widths at high VIX = stop loss hunting
    width = cand.get('width', 0)
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
_cal_signature = None
_capital = 250000

def build_calibration(closed_trades):
    global _calibration, _cal_count, _cal_signature
    trades = [t for t in closed_trades if t.get('status') == 'CLOSED' and t.get('actual_pnl') is not None]
    # BR31: Signature detects PnL corrections at same count (dev/debug scenario)
    sig = f"{len(trades)}|{sum((t.get('actual_pnl') or 0) for t in trades):.2f}"
    if sig == _cal_signature and _calibration:
        return _calibration
    _cal_count = len(trades)
    _cal_signature = sig
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
        vix = t.get('entry_vix') or 15
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
        vix = t.get('entry_vix') or 15
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
        vix = t.get('entry_vix') or 15
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
        except Exception as e:
            # BR34: Malformed entry_date → defaults to hour 0 (morning bucket). Log it.
            print(f"time_of_day bucket: entry_date '{entry}' parse: {e}")
            hour = 0
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
        sigma = snap.get('sigma_otm') or snap.get('sigmaOTM') or snap.get('sigma_from_atm')
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
    # BR24: Scaled threshold. Intraday sellers capture MORE. 60% too low.
    if cap < 70:
        return {"type": "risk", "icon": "💸", "label": f"Capturing {cap:.0f}% of peaks",
                "detail": f"Avg peak ₹{ex['avg_peak']:.0f} → exit ₹{ex['avg_exit']:.0f}. Capture efficiency low.",
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
    # BR41: Warn if DTE missing — expected set after v2.2.7 Fix C1
    dte = ctx.get('bnfDTE')
    if dte is None:
        print("max_pain_gravity: bnfDTE missing, using default 5")
        dte = 5
    """Max pain as magnet — strongest on DTE 0-1."""
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

def fii_short_trend(ctx):
    """Phase B: 3-day FII Short% trend classifier.
    Replaces JS getFiiShortTrend(currentShort, history).
    """
    morning = ctx.get('morning_input') or {}
    current_short = morning.get('fiiShortPct')
    if current_short is None or current_short == '':
        return None
    
    try:
        current_short = float(current_short)
    except:
        return None

    yday_hist = ctx.get('yesterdayHistory') or []
    vals = [current_short]
    for h in yday_hist[:2]:
        val = h.get('fii_short_pct')
        if val is not None:
            vals.append(float(val))
    
    if len(vals) < 2:
        return None

    changes = []
    for i in range(len(vals) - 1):
        changes.append(vals[i] - vals[i+1])

    trend = 'FLAT'
    label = ''
    all_down = all(c < 0 for c in changes)
    all_up = all(c > 0 for c in changes)

    # Show oldest -> newest in label (human-readable trend direction)
    vals_str = '→'.join(f'{v:.1f}' for v in reversed(vals))

    if all_down:
        trend = 'COVERING'
        label = f'↓↓ Covering ({vals_str}) — bullish'
    elif all_up:
        trend = 'BUILDING'
        label = f'↑↑ Building ({vals_str}) — bearish'
    elif len(changes) >= 2 and (1 if changes[0] > 0 else -1 if changes[0] < 0 else 0) != (1 if changes[1] > 0 else -1 if changes[1] < 0 else 0):
        trend = 'INFLECTION'
        label = f'⟳ Inflection ({vals_str}) — direction changed'
    else:
        label = vals_str

    accel = False
    if len(changes) >= 2 and abs(changes[0]) > abs(changes[1]) * 1.3:
        accel = True
    
    aggressive = len(changes) > 0 and abs(changes[0]) >= 3

    return {
        'trend': trend, 'label': label, 'accel': accel, 'aggressive': aggressive,
        'values': vals, 'changes': changes
    }

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

def validate_yesterday_signal(ctx):
    """Phase B: validate yesterday's tomorrow_signal against today's gap.
    Replaces async JS validateYesterdaySignal(todayGap).
    """
    gap = ctx.get('gap')
    if not gap or gap.get('type') == 'UNKNOWN':
        return None
    
    yday_hist = ctx.get('yesterdayHistory') or []
    if not yday_hist:
        return None
    
    yday_date = yday_hist[0].get('date')
    if not yday_date:
        return None
    
    yday_signal = ctx.get('yesterdaySignal')
    if not yday_signal or not yday_signal.get('tomorrow_signal'):
        return None
    
    predicted = yday_signal.get('tomorrow_signal')
    actual_gap = gap.get('gap', 0)
    actual_dir = 'BULLISH' if actual_gap > 50 else 'BEARISH' if actual_gap < -50 else 'NEUTRAL'
    
    correct = (predicted == actual_dir) or (predicted == 'NEUTRAL' and abs(actual_gap) < 100)
    
    accuracy_stats = ctx.get('signalAccuracy') or {}
    total_signals = accuracy_stats.get('total', 0)
    
    return {
        'date': yday_date,
        'predicted': predicted,
        'strength': yday_signal.get('signal_strength'),
        'actualGap': actual_gap,
        'actualDir': actual_dir,
        'correct': correct,
        'totalSignals': total_signals
    }

# ─── CANDLESTICK PATTERN DETECTION ───

def _ohlc_vals(polls, key):
    return [p.get(key) for p in polls if p.get(key) is not None]

def _is_bullish(c):
    return c['close'] >= c['open']

def _is_bearish(c):
    return c['close'] < c['open']

def _real_body(c):
    return abs(c['close'] - c['open'])

def _upper_shadow(c):
    return c['high'] - max(c['open'], c['close'])

def _lower_shadow(c):
    return min(c['open'], c['close']) - c['low']

def _total_range(c):
    r = c['high'] - c['low']
    return r if r > 0 else 0.001

def _body_pct(c):
    return _real_body(c) / _total_range(c)

def aggregate_15m_candles(polls, key='bnf'):
    groups = []
    for i in range(0, len(polls), 3):
        block = polls[i:i+3]
        if len(block) < 2:
            break
        vals = _ohlc_vals(block, key)
        if len(vals) < 2:
            continue
        groups.append({
            'open': block[0].get(key),
            'high': max(vals),
            'low': min(vals),
            'close': block[-1].get(key),
            'ts': block[-1].get('t', ''),
            'count': len(vals)
        })
    return groups

def build_daily_candle(polls, key='bnf'):
    vals = _ohlc_vals(polls, key)
    if not vals:
        return None
    return {
        'open': polls[0].get(key),
        'high': max(vals),
        'low': min(vals),
        'close': vals[-1],
        'ts': polls[-1].get('t', ''),
        'count': len(vals)
    }

def _prior_trend(candles, n=5, direction='down'):
    if len(candles) < n:
        return False
    segment = candles[-n:]
    closes = [c['close'] for c in segment]
    slope = lsq_slope(closes)
    threshold = _CONST.get('CANDLE_PRIOR_TREND_THRESHOLD', 0.3)
    if direction == 'down':
        return slope < -threshold
    elif direction == 'up':
        return slope > threshold
    return abs(slope) <= threshold

def _is_bullish_marubozu(c):
    sp = _CONST.get('CANDLE_MARUBOZU_SHADOW_PCT', 0.05)
    return _is_bullish(c) and _upper_shadow(c) / _total_range(c) <= sp and _lower_shadow(c) / _total_range(c) <= sp

def _is_bearish_marubozu(c):
    sp = _CONST.get('CANDLE_MARUBOZU_SHADOW_PCT', 0.05)
    return _is_bearish(c) and _upper_shadow(c) / _total_range(c) <= sp and _lower_shadow(c) / _total_range(c) <= sp

def _is_doji(c):
    return _body_pct(c) <= _CONST.get('CANDLE_DOJI_BODY_PCT', 0.05)

def _is_spinning_top(c):
    min_b = _CONST.get('CANDLE_SPINNING_MIN_BODY_PCT', 0.05)
    max_b = _CONST.get('CANDLE_SPINNING_MAX_BODY_PCT', 0.20)
    b = _body_pct(c)
    if not (min_b < b <= max_b):
        return False
    us = _upper_shadow(c)
    ls = _lower_shadow(c)
    if us == 0 or ls == 0:
        return False
    ratio = us / ls
    rmin = _CONST.get('CANDLE_SHADOW_RATIO_MIN', 0.5)
    rmax = _CONST.get('CANDLE_SHADOW_RATIO_MAX', 2.0)
    return rmin <= ratio <= rmax

def _is_hammer(c, prior_down):
    if not prior_down:
        return False
    body = _real_body(c)
    if body == 0:
        return False
    candle_range = _total_range(c)
    max_upper = max(
        _CONST.get('CANDLE_HAMMER_UPPER_MAX_BODY', 0.5) * body,
        _CONST.get('CANDLE_HAMMER_UPPER_MAX_RANGE', 0.10) * candle_range,
    )
    return (
        _lower_shadow(c) >= _CONST.get('CANDLE_HAMMER_SHADOW_MIN', 2.0) * body
        and _upper_shadow(c) <= max_upper
    )

def _is_hanging_man(c, prior_up):
    if not prior_up:
        return False
    body = _real_body(c)
    if body == 0:
        return False
    candle_range = _total_range(c)
    max_upper = max(
        _CONST.get('CANDLE_HAMMER_UPPER_MAX_BODY', 0.5) * body,
        _CONST.get('CANDLE_HAMMER_UPPER_MAX_RANGE', 0.10) * candle_range,
    )
    return (
        _lower_shadow(c) >= _CONST.get('CANDLE_HAMMER_SHADOW_MIN', 2.0) * body
        and _upper_shadow(c) <= max_upper
    )

def _is_bullish_engulfing(p1, p2, prior_down):
    if not prior_down:
        return False
    if not _is_bearish(p1) or not _is_bullish(p2):
        return False
    return p2['close'] > p1['open'] and p2['open'] < p1['close']

def _is_bearish_engulfing(p1, p2, prior_up):
    if not prior_up:
        return False
    if not _is_bullish(p1) or not _is_bearish(p2):
        return False
    return p2['open'] > p1['close'] and p2['close'] < p1['open']

def _is_bullish_harami(p1, p2, prior_down):
    if not prior_down:
        return False
    if not _is_bearish(p1) or not _is_bullish(p2):
        return False
    if _real_body(p1) <= _real_body(p2) * _CONST.get('CANDLE_ENGULF_BODY_MIN', 1.5):
        return False
    return p2['open'] > p1['close'] and p2['close'] < p1['open']

def _is_bearish_harami(p1, p2, prior_up):
    if not prior_up:
        return False
    if not _is_bullish(p1) or not _is_bearish(p2):
        return False
    if _real_body(p1) <= _real_body(p2) * _CONST.get('CANDLE_ENGULF_BODY_MIN', 1.5):
        return False
    return p2['open'] < p1['close'] and p2['close'] > p1['open']

def _is_morning_star(p1, p2, p3, prior_down):
    if not prior_down:
        return False
    if not _is_bearish(p1) or not _is_bullish(p3):
        return False
    if not (_is_doji(p2) or _is_spinning_top(p2)):
        return False
    gap_pct = _CONST.get('CANDLE_GAP_PCT', 0.001)
    gap_down = p2['open'] < p1['close'] * (1 - gap_pct)
    gap_up = p3['open'] > p2['close'] * (1 + gap_pct)
    if gap_down and gap_up and p3['close'] > p1['open']:
        return True
    # Intraday index candles rarely gap between adjacent 15m bars; require confirmation through midpoint recovery.
    midpoint = (p1['open'] + p1['close']) / 2
    return _real_body(p3) >= _real_body(p2) and p3['close'] > midpoint and p3['close'] > p2['high']

def _is_evening_star(p1, p2, p3, prior_up):
    if not prior_up:
        return False
    if not _is_bullish(p1) or not _is_bearish(p3):
        return False
    if not (_is_doji(p2) or _is_spinning_top(p2)):
        return False
    gap_pct = _CONST.get('CANDLE_GAP_PCT', 0.001)
    gap_up = p2['open'] > p1['close'] * (1 + gap_pct)
    gap_down = p3['open'] < p2['close'] * (1 - gap_pct)
    if gap_up and gap_down and p3['close'] < p1['open']:
        return True
    # Intraday index candles rarely gap between adjacent 15m bars; require confirmation through midpoint loss.
    midpoint = (p1['open'] + p1['close']) / 2
    return _real_body(p3) >= _real_body(p2) and p3['close'] < midpoint and p3['close'] < p2['low']

def _candle_insight(pattern_name, label, detail, impact, strength, index, timeframe, candle):
    return {
        "type": "candle", "icon": "🕯", "label": label, "detail": detail,
        "impact": impact, "strength": strength,
        "pattern": pattern_name, "index": index, "timeframe": timeframe,
        "candle": {"open": candle['open'], "high": candle['high'],
                   "low": candle['low'], "close": candle['close']}
    }

def _detect_15m_patterns(candles, index_key):
    insights = []
    if not candles:
        return insights
    c = candles[-1]
    prior_down = _prior_trend(candles[:-1], direction='down')
    prior_up = _prior_trend(candles[:-1], direction='up')
    if _is_bullish_marubozu(c):
        insights.append(_candle_insight("BULLISH_MARUBOZU",
            f"Bullish Marubozu on {index_key} (15m)",
            f"Open={c['open']:.0f} Close={c['close']:.0f}. No wicks, extreme buying.",
            "bullish", 3, index_key, "15m", c))
    if _is_bearish_marubozu(c):
        insights.append(_candle_insight("BEARISH_MARUBOZU",
            f"Bearish Marubozu on {index_key} (15m)",
            f"Open={c['open']:.0f} Close={c['close']:.0f}. No wicks, extreme selling.",
            "bearish", 3, index_key, "15m", c))
    if _is_doji(c):
        insights.append(_candle_insight("DOJI",
            f"Doji on {index_key} (15m)",
            f"Open={c['open']:.0f} Close={c['close']:.0f}. Indecision, equilibrium.",
            "caution", 2, index_key, "15m", c))
    if _is_spinning_top(c):
        insights.append(_candle_insight("SPINNING_TOP",
            f"Spinning Top on {index_key} (15m)",
            f"Open={c['open']:.0f} Close={c['close']:.0f}. Small body, indecision.",
            "caution", 2, index_key, "15m", c))
    if _is_hammer(c, prior_down):
        insights.append(_candle_insight("HAMMER",
            f"Hammer on {index_key} (15m)",
            f"Low={c['low']:.0f} Close={c['close']:.0f}. Long lower wick in downtrend.",
            "bullish", 3, index_key, "15m", c))
    if _is_hanging_man(c, prior_up):
        insights.append(_candle_insight("HANGING_MAN",
            f"Hanging Man on {index_key} (15m)",
            f"Low={c['low']:.0f} Close={c['close']:.0f}. Long lower wick in uptrend.",
            "bearish", 3, index_key, "15m", c))
    if len(candles) >= 2:
        p1 = candles[-2]
        p2 = candles[-1]
        prior_down_2 = _prior_trend(candles[:-2], direction='down') if len(candles) > 2 else False
        prior_up_2 = _prior_trend(candles[:-2], direction='up') if len(candles) > 2 else False
        if _is_bullish_engulfing(p1, p2, prior_down_2):
            insights.append(_candle_insight("BULLISH_ENGULFING",
                f"Bullish Engulfing on {index_key} (15m)",
                f"P1 red ({p1['close']:.0f}) -> P2 blue ({p2['close']:.0f}) engulfs. Strong reversal.",
                "bullish", 4, index_key, "15m", p2))
        if _is_bearish_engulfing(p1, p2, prior_up_2):
            insights.append(_candle_insight("BEARISH_ENGULFING",
                f"Bearish Engulfing on {index_key} (15m)",
                f"P1 blue ({p1['close']:.0f}) -> P2 red ({p2['close']:.0f}) engulfs. Strong reversal.",
                "bearish", 4, index_key, "15m", p2))
        if _is_bullish_harami(p1, p2, prior_down_2):
            insights.append(_candle_insight("BULLISH_HARAMI",
                f"Bullish Harami on {index_key} (15m)",
                f"P1 long red -> P2 small blue inside. Potential reversal up.",
                "bullish", 3, index_key, "15m", p2))
        if _is_bearish_harami(p1, p2, prior_up_2):
            insights.append(_candle_insight("BEARISH_HARAMI",
                f"Bearish Harami on {index_key} (15m)",
                f"P1 long blue -> P2 small red inside. Potential reversal down.",
                "bearish", 3, index_key, "15m", p2))
    if len(candles) >= 3:
        p1 = candles[-3]
        p2 = candles[-2]
        p3 = candles[-1]
        prior_down_3 = _prior_trend(candles[:-3], direction='down') if len(candles) > 3 else False
        prior_up_3 = _prior_trend(candles[:-3], direction='up') if len(candles) > 3 else False
        if _is_morning_star(p1, p2, p3, prior_down_3):
            insights.append(_candle_insight("MORNING_STAR",
                f"Morning Star on {index_key} (15m)",
                f"P1 red, P2 doji (gap down), P3 blue > P1 open. Strong bullish reversal.",
                "bullish", 5, index_key, "15m", p3))
        if _is_evening_star(p1, p2, p3, prior_up_3):
            insights.append(_candle_insight("EVENING_STAR",
                f"Evening Star on {index_key} (15m)",
                f"P1 blue, P2 doji (gap up), P3 red < P1 open. Strong bearish reversal.",
                "bearish", 5, index_key, "15m", p3))
    return insights

def _detect_daily_patterns(today, yesterday, index_key):
    insights = []
    if not today or not yesterday:
        return insights
    if not isinstance(yesterday, dict):
        return insights
    y = yesterday
    if not all(k in y for k in ('open', 'high', 'low', 'close')):
        return insights
    if _is_bullish_marubozu(today):
        insights.append(_candle_insight("BULLISH_MARUBOZU",
            f"Bullish Marubozu on {index_key} (Daily)",
            f"Open={today['open']:.0f} Close={today['close']:.0f}. Extreme buying all session.",
            "bullish", 3, index_key, "daily", today))
    if _is_bearish_marubozu(today):
        insights.append(_candle_insight("BEARISH_MARUBOZU",
            f"Bearish Marubozu on {index_key} (Daily)",
            f"Open={today['open']:.0f} Close={today['close']:.0f}. Extreme selling all session.",
            "bearish", 3, index_key, "daily", today))
    if _is_doji(today):
        insights.append(_candle_insight("DOJI",
            f"Doji on {index_key} (Daily)",
            f"Open={today['open']:.0f} Close={today['close']:.0f}. Market indecision.",
            "caution", 2, index_key, "daily", today))
    if _is_spinning_top(today):
        insights.append(_candle_insight("SPINNING_TOP",
            f"Spinning Top on {index_key} (Daily)",
            f"Open={today['open']:.0f} Close={today['close']:.0f}. Indecision.",
            "caution", 2, index_key, "daily", today))
    prior_down = _is_bearish(y)
    prior_up = _is_bullish(y)
    if _is_bullish_engulfing(y, today, prior_down):
        insights.append(_candle_insight("BULLISH_ENGULFING",
            f"Bullish Engulfing on {index_key} (Daily)",
            f"Yesterday red -> Today blue engulfs. Strong daily reversal.",
            "bullish", 4, index_key, "daily", today))
    if _is_bearish_engulfing(y, today, prior_up):
        insights.append(_candle_insight("BEARISH_ENGULFING",
            f"Bearish Engulfing on {index_key} (Daily)",
            f"Yesterday blue -> Today red engulfs. Strong daily reversal.",
            "bearish", 4, index_key, "daily", today))
    if _is_bullish_harami(y, today, prior_down):
        insights.append(_candle_insight("BULLISH_HARAMI",
            f"Bullish Harami on {index_key} (Daily)",
            f"Yesterday long red -> Today small blue inside. Potential reversal.",
            "bullish", 3, index_key, "daily", today))
    if _is_bearish_harami(y, today, prior_up):
        insights.append(_candle_insight("BEARISH_HARAMI",
            f"Bearish Harami on {index_key} (Daily)",
            f"Yesterday long blue -> Today small red inside. Potential reversal.",
            "bearish", 3, index_key, "daily", today))
    return insights

def compute_candle_signals(polls, ctx):
    result = {}
    for index_key, poll_key in [('BNF', 'bnf'), ('NF', 'nf')]:
        candles_15m = aggregate_15m_candles(polls, poll_key)
        daily = build_daily_candle(polls, poll_key)
        patterns_15m = _detect_15m_patterns(candles_15m, index_key)
        ctx_ohlc = ctx.get(f'{poll_key}OHLC')
        patterns_daily = _detect_daily_patterns(daily, ctx_ohlc, index_key) if daily and ctx_ohlc else []
        result[f'candle_{poll_key}'] = {
            'current': daily,
            'last_15m': candles_15m[-1] if candles_15m else None,
            'patterns': patterns_15m + patterns_daily,
        }
    return result

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
    TOTAL_SIGNALS = 8
    
    # ═══ MORNING WEIGHT DECAY (time-aware) ═══
    # Keep morning thesis dominant until entry window (~11:00 IST), then decay.
    mins_since_open = ctx.get('mins_since_open', 0) or 0
    if mins_since_open <= 105:      # 9:15 -> 11:00
        morning_weight = max(0.60, 1.0 - poll_count * 0.03)
    elif mins_since_open <= 165:    # 11:00 -> 12:00
        morning_weight = max(0.45, 0.75 - (mins_since_open - 105) * 0.005)
    else:                           # post-noon
        morning_weight = max(0.25, 0.45 - (mins_since_open - 165) * 0.002)
    intraday_weight = 1.0 - morning_weight
    
    # ═══ FIRST 15 MINUTES SUPPRESSION ═══
    # Opening noise — gap repricing, market maker activity. Signals unreliable.
    if poll_count < 3:
        early_bias = 'BULL' if morning_net >= 1 else 'BEAR' if morning_net <= -1 else 'NEUTRAL'
        early_strength = 'STRONG' if abs(morning_net) >= 2 else 'MILD' if abs(morning_net) >= 1 else ''
        return {
            'bias': early_bias,
            'strength': early_strength,
            'label': f"{early_strength} {early_bias}".strip(),
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
    
    # --- 7. NF50 breadth (macro market breadth) ---
    nf50_pct = (ctx.get('nf50Breadth') or {}).get('pct', 50)
    if nf50_pct > 65: signals.append(2); drift_reasons.append(f"NF50 {nf50_pct:.0f}%")
    elif nf50_pct > 55: signals.append(1)
    elif nf50_pct < 35: signals.append(-2); drift_reasons.append(f"NF50 {nf50_pct:.0f}%")
    elif nf50_pct < 45: signals.append(-1)
    else: signals.append(0)
    
    # --- 8. Regime (range pushes opposite to morning direction) ---
    regime_type = regime.get('type', 'unknown') if regime else 'unknown'
    regime_dir = regime.get('direction', 0) if regime else 0
    if regime_type == 'range':
        # BR50: Range pushes toward NEUTRAL only if morning was MILD (|net| == 1).
        # Don't override STRONG morning conviction (|net| >= 2) — it may continue.
        if abs(morning_net) == 1:
            morning_sign = 1 if morning_net > 0 else -1
            signals.append(-morning_sign)
            drift_reasons.append("Range → push NEUTRAL (mild morning)")
        elif abs(morning_net) >= 2:
            signals.append(0)
            drift_reasons.append("Range but strong morning — no override")
        else:
            signals.append(0)
    elif regime_type == 'trend':
        if abs(regime_dir) >= 3:
            signals.append(2 if regime_dir > 0 else -2)
            drift_reasons.append(f"Trend {'↑' if regime_dir > 0 else '↓'}")
        elif abs(regime_dir) >= 1:
            signals.append(1 if regime_dir > 0 else -1)
        else: signals.append(0)
    else: signals.append(0)
    
    # ═══ BLEND: morning prior × intraday evidence ═══
    intraday_net = sum(signals)
    # BR51: Proper normalization. Previous (intraday_net / 7) * 3 could hit ±6 with
    # 7 signals at ±2 each. New max_possible = TOTAL_SIGNALS * 2 = 14. Range now ±3.
    # BR52: Classification thresholds (2=STRONG, 1=MILD) stay — fire less often post-fix.
    max_possible_intraday = TOTAL_SIGNALS * 2  # each signal is -2..+2
    intraday_normalized = (intraday_net / max_possible_intraday) * 3
    
    effective_net = morning_net * morning_weight + intraday_normalized * intraday_weight
    # Hysteresis: avoid flip-flop around neutral unless opposing evidence is meaningful.
    if poll_count < 30 and abs(effective_net) < 1.0 and abs(morning_net) >= 1:
        effective_net = morning_net * 0.9
    
    # Classify
    if effective_net >= 2: bias, strength = 'BULL', 'STRONG'
    elif effective_net >= 1: bias, strength = 'BULL', 'MILD'
    elif effective_net <= -2: bias, strength = 'BEAR', 'STRONG'
    elif effective_net <= -1: bias, strength = 'BEAR', 'MILD'
    else: bias, strength = 'NEUTRAL', ''
    
    return {
        'bias': bias,
        'strength': strength,
        'label': f"{strength} {bias}".strip(),
        'net': round(effective_net, 2),
        'morning_weight': round(morning_weight, 2),
        'signals': signals,
        'intraday_net': intraday_net,
        'drift_reasons': drift_reasons[:5]
    }

def compute_morning_bias(ctx, polls):
    """Phase B: 7-signal voting + 8th chain-validation overlay.
    Replaces JS computeBias(). Reads from ctx. Returns dict.
    """
    votes = {'bull': 0, 'bear': 0}
    signals = []
    morning = ctx.get('morning_input') or {}
    chain_data = ctx.get('chain_data') or ctx.get('bnfChain') or {}
    yday_hist = ctx.get('yesterdayHistory') or []
    
    if not morning:
        return {'bias': 'NEUTRAL', 'strength': '', 'net': 0,
                'votes': votes, 'signals': signals,
                'label': 'NEUTRAL', 'upstoxAgrees': None,
                'chainValidation': 'NONE'}
    
    def _to_float(v):
        if v is None or v == '':
            return None
        try:
            f = float(v)
            return f if f == f else None  # NaN check
        except (ValueError, TypeError):
            return None
    
    # 1. FII Cash
    fc = _to_float(morning.get('fiiCash'))
    if fc is not None:
        if fc > 500:
            votes['bull'] += 1
            signals.append({'name': 'FII Cash', 'value': f'₹{fc}Cr', 'dir': 'BULL'})
        elif fc < -500:
            votes['bear'] += 1
            signals.append({'name': 'FII Cash', 'value': f'₹{fc}Cr', 'dir': 'BEAR'})
        else:
            signals.append({'name': 'FII Cash', 'value': f'₹{fc}Cr', 'dir': 'NEUTRAL'})
    
    # 2. FII Short%
    fsp = _to_float(morning.get('fiiShortPct'))
    if fsp is not None:
        prev = float(yday_hist[0].get('fii_short_pct')) if (yday_hist and yday_hist[0].get('fii_short_pct') is not None) else None
        if prev is None:
            if fsp > 85:
                votes['bear'] += 1
                signals.append({'name': 'FII Short%', 'value': f'{fsp}% (prev: N/A)', 'dir': 'BEAR'})
            elif fsp < 70:
                votes['bull'] += 1
                signals.append({'name': 'FII Short%', 'value': f'{fsp}%', 'dir': 'BULL'})
            else:
                signals.append({'name': 'FII Short%', 'value': f'{fsp}%', 'dir': 'NEUTRAL'})
        elif fsp > 85 and fsp > prev:
            votes['bear'] += 1
            signals.append({'name': 'FII Short%', 'value': f'{fsp}%↑ (was {prev})', 'dir': 'BEAR'})
        elif fsp > 85 and fsp < prev:
            signals.append({'name': 'FII Short%', 'value': f'{fsp}%↓ covering (was {prev})', 'dir': 'NEUTRAL'})
        elif fsp > 85 and fsp == prev:
            signals.append({'name': 'FII Short%', 'value': f'{fsp}% → flat (was {prev})', 'dir': 'NEUTRAL'})
        elif fsp < 70:
            votes['bull'] += 1
            signals.append({'name': 'FII Short%', 'value': f'{fsp}%', 'dir': 'BULL'})
        else:
            signals.append({'name': 'FII Short%', 'value': f'{fsp}%', 'dir': 'NEUTRAL'})

    # 3. Close Char
    cc = chain_data.get('closeChar')
    if cc is not None:
        if cc >= 1:
            votes['bull'] += 1
            sign = '+' if cc > 0 else ''
            signals.append({'name': 'Close Char', 'value': f'{sign}{cc} (auto)', 'dir': 'BULL'})
        elif cc <= -1:
            votes['bear'] += 1
            signals.append({'name': 'Close Char', 'value': f'{cc} (auto)', 'dir': 'BEAR'})
        else:
            signals.append({'name': 'Close Char', 'value': f'{cc} (auto)', 'dir': 'NEUTRAL'})
            
    # 4. PCR
    pcr = _to_float(chain_data.get('nearAtmPCR'))
    if pcr is not None:
        if pcr > 1.2:
            votes['bull'] += 1
            signals.append({'name': 'PCR', 'value': f'{pcr:.2f} (near-ATM)', 'dir': 'BULL'})
        elif pcr < 0.9:
            votes['bear'] += 1
            signals.append({'name': 'PCR', 'value': f'{pcr:.2f} (near-ATM)', 'dir': 'BEAR'})
        else:
            signals.append({'name': 'PCR', 'value': f'{pcr:.2f} (near-ATM)', 'dir': 'NEUTRAL'})

    # 5. VIX Dir
    cur_vix = _to_float(chain_data.get('vix'))
    if yday_hist and cur_vix is not None:
        y_vix = _to_float(yday_hist[0].get('vix'))
        if y_vix:
            diff = cur_vix - y_vix
            if diff > 0.3:
                votes['bear'] += 1
                signals.append({'name': 'VIX Dir', 'value': f'{cur_vix:.1f} ↑{diff:.1f}', 'dir': 'BEAR'})
            elif diff < -0.3:
                votes['bull'] += 1
                signals.append({'name': 'VIX Dir', 'value': f'{cur_vix:.1f} ↓{abs(diff):.1f}', 'dir': 'BULL'})
            else:
                signals.append({'name': 'VIX Dir', 'value': f'{cur_vix:.1f} →', 'dir': 'NEUTRAL'})

    # 6. Futures Prem
    fp = _to_float(chain_data.get('futuresPremium'))
    if fp is not None:
        if fp > 0.05:
            votes['bull'] += 1
            signals.append({'name': 'Futures Prem', 'value': f'{fp:.3f}%', 'dir': 'BULL'})
        elif fp < -0.05:
            votes['bear'] += 1
            signals.append({'name': 'Futures Prem', 'value': f'{fp:.3f}%', 'dir': 'BEAR'})
        else:
            signals.append({'name': 'Futures Prem', 'value': f'{fp:.3f}%', 'dir': 'NEUTRAL'})

    # 7. DII Floor
    dc = _to_float(morning.get('diiCash'))
    if dc is not None and fc is not None and abs(fc) > 100:
        today_abs = round(dc / abs(fc), 2)
        yday = yday_hist[0] if yday_hist else None
        ydc = _to_float(yday.get('dii_cash')) if yday else None
        yfc = _to_float(yday.get('fii_cash')) if yday else None
        y_abs = round(ydc / abs(yfc), 2) if (ydc is not None and yfc is not None and abs(yfc) > 100) else None
        change = round(today_abs - y_abs, 2) if y_abs is not None else None
        c_str = f' (was {y_abs}×, Δ{"+" if change > 0 else ""}{change})' if change is not None else ''
        
        if change is not None:
            if change < -0.2 and today_abs <= 1.0:
                votes['bear'] += 1
                signals.append({'name': 'DII Floor', 'value': f'{today_abs}×{c_str}', 'dir': 'BEAR'})
            elif change > 0.2 and today_abs >= 0.7:
                votes['bull'] += 1
                signals.append({'name': 'DII Floor', 'value': f'{today_abs}×{c_str}', 'dir': 'BULL'})
            elif today_abs > 1.2:
                votes['bull'] += 1
                signals.append({'name': 'DII Floor', 'value': f'{today_abs}× strong{c_str}', 'dir': 'BULL'})
            elif today_abs < 0.3:
                votes['bear'] += 1
                signals.append({'name': 'DII Floor', 'value': f'{today_abs}× panic{c_str}', 'dir': 'BEAR'})
            else:
                signals.append({'name': 'DII Floor', 'value': f'{today_abs}×{c_str}', 'dir': 'NEUTRAL'})
        else:
            if today_abs > 1.0:
                votes['bull'] += 1
                signals.append({'name': 'DII Floor', 'value': f'{today_abs}× (no prev)', 'dir': 'BULL'})
            elif today_abs < 0.5:
                votes['bear'] += 1
                signals.append({'name': 'DII Floor', 'value': f'{today_abs}× (no prev)', 'dir': 'BEAR'})
            else:
                signals.append({'name': 'DII Floor', 'value': f'{today_abs}× (no prev)', 'dir': 'NEUTRAL'})

    # 8. Overnight Chain Validation
    chain_validation = 'NONE'
    overnight = ctx.get('overnightDelta')
    gap = ctx.get('gap')
    if overnight and overnight.get('signals'):
        o_bulls = len([s for s in overnight['signals'] if s['dir'] == 'BULL'])
        o_bears = len([s for s in overnight['signals'] if s['dir'] == 'BEAR'])
        o_dir = 'BEAR' if o_bears >= 2 else 'BULL' if o_bulls >= 2 else 'MIXED'
        
        gap_sigma = gap.get('sigma', 0) if gap else 0
        g_dir = ('BEAR' if gap_sigma < -0.5 else 'BULL' if gap_sigma > 0.5 else 'NEUTRAL') if gap else 'NEUTRAL'
        
        g_confirms = (o_dir == g_dir)
        g_conflicts = (o_dir == 'BULL' and g_dir == 'BEAR') or (o_dir == 'BEAR' and g_dir == 'BULL')
        
        if o_dir != 'MIXED' and g_confirms:
            chain_validation = 'CONFIRMED'
        elif o_dir != 'MIXED' and g_dir == 'NEUTRAL':
            chain_validation = 'LIKELY'
        elif o_dir != 'MIXED' and g_conflicts:
            chain_validation = 'UNCERTAIN'
            
        for s in overnight['signals']:
            v_str = f"{s['pct']:.2f}σ" if s.get('isSigma') else f"{'+' if s['pct'] > 0 else ''}{s['pct']}%"
            signals.append({'name': f"🌙 {s['name']}", 'value': f"{s['from']}→{s.get('to') or '?'} ({v_str})", 'dir': s['dir']})
            
        if chain_validation == 'CONFIRMED':
            confirmed_dir = o_dir
            stale_neutralized = 0
            for i in range(len(signals)):
                s = signals[i]
                is_stale = any(name in s['name'] for name in ['Close Char', 'FII Short%', 'DII Floor'])
                if is_stale and s['dir'] != 'NEUTRAL' and s['dir'] != confirmed_dir:
                    if s['dir'] == 'BULL': votes['bull'] -= 1
                    if s['dir'] == 'BEAR': votes['bear'] -= 1
                    signals[i] = {**s, 'value': s['value'] + ' [stale⚠️]', 'dir': 'NEUTRAL'}
                    stale_neutralized += 1
            if confirmed_dir == 'BEAR': votes['bear'] += 2
            elif confirmed_dir == 'BULL': votes['bull'] += 2
            signals.append({'name': '🔗 Chain', 'value': f'CONFIRMED {confirmed_dir} ({stale_neutralized} stale neutralized)', 'dir': confirmed_dir})
        elif chain_validation == 'LIKELY':
            likely_dir = o_dir
            if likely_dir == 'BEAR': votes['bear'] += 1
            elif likely_dir == 'BULL': votes['bull'] += 1
            signals.append({'name': '🔗 Chain', 'value': f'LIKELY {likely_dir} (gap flat, not confirmed)', 'dir': likely_dir})
        elif chain_validation == 'UNCERTAIN':
            signals.append({'name': '🔗 Chain', 'value': f'UNCERTAIN (overnight {o_dir} but gap {g_dir})', 'dir': 'NEUTRAL'})

    # Final scoring
    net = votes['bull'] - votes['bear']
    if net >= 3: bias, strength = 'BULL', 'STRONG'
    elif net >= 1: bias, strength = 'BULL', 'MILD'
    elif net <= -3: bias, strength = 'BEAR', 'STRONG'
    elif net <= -1: bias, strength = 'BEAR', 'MILD'
    else: bias, strength = 'NEUTRAL', ''
    
    ub = morning.get('upstoxBias')
    u_agrees = None
    if ub and ub != '':
        u_dir = 'BULL' if ub == 'Bullish' else 'BEAR' if ub == 'Bearish' else 'NEUTRAL'
        u_agrees = (bias == u_dir) or (bias == 'NEUTRAL' and u_dir == 'NEUTRAL')
        signals.append({'name': 'Upstox', 'value': f"{ub} {'✅ agrees' if u_agrees else '⚠️ DISAGREES'}", 'dir': u_dir, 'isComparison': True})
        
    return {
        'bias': bias, 'strength': strength, 'net': net, 'votes': votes,
        'signals': signals, 'label': f'{strength} {bias}'.strip(),
        'upstoxAgrees': u_agrees, 'chainValidation': chain_validation
    }

def compute_overnight_delta(ctx):
    """Phase B: compute Dow/Crude/GIFT deltas since evening close.
    Replaces JS computeOvernightDelta(currentNfSpot).
    """
    DOW_THRESHOLD = 0.5
    CRUDE_THRESHOLD = 1.5
    
    eve = ctx.get('eveningClose')
    if not eve:
        return None
        
    morning_global = ctx.get('globalDirection') or {}
    m_dow = morning_global.get('dowClose')
    m_crude = morning_global.get('crudeSettle')
    
    delta = {'signals': [], 'summary': ''}
    
    if eve.get('dow') and m_dow:
        e_dow = float(eve['dow'])
        pct = round((m_dow - e_dow) / e_dow * 100, 2)
        dir = 'NEUTRAL' if abs(pct) < DOW_THRESHOLD else ('BULL' if pct > 0 else 'BEAR')
        delta['signals'].append({'name': 'Dow', 'from': e_dow, 'to': m_dow, 'pct': pct, 'dir': dir, 'threshold': DOW_THRESHOLD})
        
    if eve.get('crude') and m_crude:
        e_crude = float(eve['crude'])
        pct = round((m_crude - e_crude) / e_crude * 100, 2)
        # Crude up = bearish
        dir = 'NEUTRAL' if abs(pct) < CRUDE_THRESHOLD else ('BEAR' if pct > 0 else 'BULL')
        delta['signals'].append({'name': 'Crude', 'from': e_crude, 'to': m_crude, 'pct': pct, 'dir': dir, 'threshold': CRUDE_THRESHOLD})
        
    gift_spot = ctx.get('morning_input', {}).get('giftSpot')
    if eve.get('gift') and gift_spot:
        e_gift = float(eve['gift'])
        m_gift = float(gift_spot)
        pct = round((m_gift - e_gift) / e_gift * 100, 2)
        g_dir = 'BULL' if pct > 0.4 else 'BEAR' if pct < -0.4 else 'NEUTRAL'
        delta['signals'].append({'name': 'GIFT', 'from': e_gift, 'to': m_gift, 'pct': pct, 'dir': g_dir, 'threshold': 0.4})
    elif eve.get('gift') and ctx.get('gap'):
        # Fallback to sigma if giftSpot missing (preserving existing resilience)
        gap_sigma = ctx['gap'].get('sigma', 0)
        g_dir = 'BULL' if gap_sigma > 0.3 else 'BEAR' if gap_sigma < -0.3 else 'NEUTRAL'
        delta['signals'].append({'name': 'GIFT', 'from': eve['gift'], 'to': '?', 'pct': gap_sigma, 'dir': g_dir, 'isSigma': True})
        
    bulls = len([s for s in delta['signals'] if s['dir'] == 'BULL'])
    bears = len([s for s in delta['signals'] if s['dir'] == 'BEAR'])
    
    if bears >= 2: delta['summary'] = '🔴 OVERNIGHT BEARISH'
    elif bulls >= 2: delta['summary'] = '🟢 OVERNIGHT BULLISH'
    elif bears > bulls: delta['summary'] = '🟡 OVERNIGHT MILDLY BEARISH'
    elif bulls > bears: delta['summary'] = '🟡 OVERNIGHT MILDLY BULLISH'
    else: delta['summary'] = '⚪ OVERNIGHT NEUTRAL'
    return delta

# ───────────────────────────────────────────────────────────────
# DECISION #24 — build_chain_snapshot_data
# ───────────────────────────────────────────────────────────────
def build_chain_snapshot_data(ctx: dict) -> dict:
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except Exception:
            return {}
    if not isinstance(ctx, dict):
        return {}

    bnf_chain = ctx.get('bnfChain', {}) or {}
    nf_chain = ctx.get('nfChain', {}) or {}
    live = ctx.get('live', {}) or {}
    baseline = ctx.get('baseline', {}) or {}
    bnf_breadth = ctx.get('bnfBreadth', {}) or {}
    nf50_breadth = ctx.get('nf50Breadth', {}) or {}

    bnf_spot = live.get('bnfSpot') or baseline.get('bnfSpot')
    nf_spot = live.get('nfSpot') or baseline.get('nfSpot')
    vix = live.get('vix') or baseline.get('vix')

    snapshot = {
        'date': ctx.get('today_ist'),
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
def compute_positioning(snap_2pm: dict, snap_315pm: dict, ctx: dict) -> dict:
    if not snap_2pm or not snap_315pm:
        return None

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
        'snap2pm': snap_2pm,
        'snap315pm': snap_315pm,
    }

    bear_score = 0.0
    bull_score = 0.0

    if delta['callOiDelta'] > delta['putOiDelta'] * 1.5:
        bear_score += 2
    elif delta['putOiDelta'] > delta['callOiDelta'] * 1.5:
        bull_score += 2

    if delta['pcrChange'] < -0.05:
        bear_score += 1.5
    elif delta['pcrChange'] > 0.05:
        bull_score += 1.5

    if delta['vixChange'] > 0.3:
        bear_score += 1.5
    elif delta['vixChange'] < -0.3:
        bull_score += 1.5

    if delta['maxPainShift'] < -100:
        bear_score += 1
    elif delta['maxPainShift'] > 100:
        bull_score += 1

    if delta['breadthChange'] < -0.5:
        bear_score += 1
    elif delta['breadthChange'] > 0.5:
        bull_score += 1

    pcr_context = ctx.get('pcrContext')
    if pcr_context and pcr_context.get('confidence') != 'LOW':
        bias = pcr_context.get('bias')
        if bias == 'BULL':
            bull_score += 1
        elif bias == 'BEAR':
            bear_score += 1
        elif bias == 'MILD_BULL':
            bull_score += 0.5

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
def compute_global_boost(positioning_result: dict, ctx: dict) -> dict:
    if not positioning_result:
        return None

    base_signal = positioning_result.get('signal')
    if base_signal == 'NEUTRAL':
        return None

    tomorrow_signal = {
        'signal': base_signal,
        'strength': positioning_result.get('strength', 1),
        'globalBoost': 0,
    }

    gd = ctx.get('globalDirection', {}) or {}
    is_bull = base_signal == 'BULLISH'
    boost = 0

    dow_threshold = _CONST.get('DOW_THRESHOLD', 0.5)
    crude_threshold = _CONST.get('CRUDE_THRESHOLD', 1.5)
    gift_threshold = _CONST.get('GIFT_THRESHOLD', 0.3)

    dow_close = gd.get('dowClose')
    dow_now = gd.get('dowNow')
    if dow_close and dow_now:
        dow_pct = ((dow_now - dow_close) / dow_close) * 100.0
        if abs(dow_pct) >= dow_threshold:
            if (dow_pct > 0 and is_bull) or (dow_pct < 0 and not is_bull):
                boost += 1
            else:
                boost -= 1

    crude_settle = gd.get('crudeSettle')
    crude_now = gd.get('crudeNow')
    if crude_settle and crude_now:
        crude_pct = ((crude_now - crude_settle) / crude_settle) * 100.0
        if abs(crude_pct) >= crude_threshold:
            if (crude_pct < 0 and is_bull) or (crude_pct > 0 and not is_bull):
                boost += 1
            else:
                boost -= 1

    gift_ref = (ctx.get('eveningClose') or {}).get('gift')
    gift_now = gd.get('giftNow')
    if gift_ref and gift_now:
        gift_pct = ((gift_now - gift_ref) / gift_ref) * 100.0
        if abs(gift_pct) >= gift_threshold:
            if (gift_pct > 0 and is_bull) or (gift_pct < 0 and not is_bull):
                boost += 1
            else:
                boost -= 1
    elif ctx.get('gap') and ctx['gap'].get('sigma') is not None:
        gap_sigma = ctx['gap']['sigma']
        gift_bull = gap_sigma > 0.3
        gift_bear = gap_sigma < -0.3
        if (gift_bull and is_bull) or (gift_bear and not is_bull):
            boost += 1
        elif (gift_bull and not is_bull) or (gift_bear and is_bull):
            boost -= 1

    if boost != 0:
        base_strength = positioning_result.get('strength', 1)
        tomorrow_signal['strength'] = max(1, min(5, base_strength + boost))
        tomorrow_signal['globalBoost'] = boost

    return tomorrow_signal

# ───────────────────────────────────────────────────────────────
# DECISION #27 — evaluate_alerts
# ───────────────────────────────────────────────────────────────
def evaluate_alerts(open_trades: list, watchlist: list, result: dict, ctx: dict) -> list:
    alerts = []
    elapsed = ctx.get('mins_since_open', 0)
    now_ms = ctx.get('now_ms', 0)
    last_routine_notify = ctx.get('last_routine_dispatch_ms', 0)

    if elapsed < _CONST.get('NOISE_WINDOW', 15):
        return alerts

    significant_move = ctx.get('significant_move', False)
    entry_window = ctx.get('entry_window_active', False)
    abs_spot_sigma = ctx.get('abs_spot_sigma', 0.0)
    abs_nf_spot_sigma = ctx.get('abs_nf_spot_sigma', 0.0)
    abs_vix_sigma = ctx.get('abs_vix_sigma', 0.0)

    # WATCHLIST alerts: use entry_window (0.3σ) not significant_move (1.5σ)
    # This allows entry notifications on normal trading days
    if entry_window or significant_move:
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

            if aligned == 3 and prev_aligned < 3:
                if elapsed < _CONST.get('LAST_ENTRY_CUTOFF', 345):
                    alerts.append({
                        'key': f"WATCHLIST_ENTRY_{cand_index}_{sell_strike}_{buy_strike}",
                        'category': 'WATCHLIST',
                        'priority': 'entry',
                        'title': '🎯 Entry Window',
                        'body': f"{cand_index} {cand_type} {sell_strike}/{buy_strike} — 3/3 aligned. {'Credit' if is_credit else 'Debit'} ₹{net_premium}",
                    })
            elif prev_aligned == 3 and aligned < 3:
                alerts.append({
                    'key': f"WATCHLIST_CLOSING_{cand_index}_{sell_strike}_{buy_strike}",
                    'category': 'WATCHLIST',
                    'priority': 'important',
                    'title': '⚠️ Window Closing',
                    'body': f"{cand_index} {cand_type} {sell_strike}/{buy_strike} — dropped to {aligned}/3",
                })

    # POSITION alerts: kept under significant_move (these are high-stakes;
    # stop-loss and target alerts should only fire when market has actually moved)
    if significant_move:
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
            trade_label = f"{t_index} {t_type} {t_sell_strike}"

            if max_profit and current_pnl >= max_profit * _CONST.get('TARGET_NEAR_RATIO', 0.8):
                pct_of_max = round(current_pnl / max_profit * 100) if max_profit else 0
                alerts.append({
                    'key': f"POS_TARGET_{t_id}",
                    'category': 'POSITION',
                    'priority': 'urgent',
                    'title': '💰 Target Near',
                    'body': f"{trade_label} P&L ₹{current_pnl} ({pct_of_max}% of max). Book profit.",
                })
            if max_loss and current_pnl <= -max_loss * _CONST.get('STOP_LOSS_RATIO', 0.7):
                alerts.append({
                    'key': f"POS_STOP_{t_id}",
                    'category': 'POSITION',
                    'priority': 'urgent',
                    'title': '🛑 Stop Loss Near',
                    'body': f"{trade_label} P&L ₹{current_pnl}. Cut position.",
                })
            if t_aligned is not None and t_aligned <= 1 and current_pnl > 0:
                alerts.append({
                    'key': f"POS_BOOK_{t_id}",
                    'category': 'POSITION',
                    'priority': 'urgent',
                    'title': '⚡ Book Profit',
                    'body': f"{trade_label} Forces {t_aligned}/3 but profitable ₹{current_pnl}. Take it.",
                })

        if (abs_spot_sigma > _CONST.get('SIGMA_IMPORTANT_THRESHOLD', 2.0)
                or abs_nf_spot_sigma > _CONST.get('SIGMA_IMPORTANT_THRESHOLD', 2.0)
                or abs_vix_sigma > _CONST.get('SIGMA_IMPORTANT_THRESHOLD', 2.0)):
            live = ctx.get('live', {}) or {}
            bnf_spot = live.get('bnfSpot') or ctx.get('bnfSpot')
            nf_spot = live.get('nfSpot') or ctx.get('nfSpot')
            vix = live.get('vix') or ctx.get('vix')
            spot_sigma = live.get('spotSigma', 0)
            nf_spot_sigma = live.get('nfSpotSigma', 0)
            vix_sigma = live.get('vixSigma', 0)
            bnf_str = f"{int(bnf_spot)}" if bnf_spot is not None else 'N/A'
            nf_str = f"{int(nf_spot)}" if nf_spot is not None else 'N/A'
            vix_str = f"{vix:.1f}" if vix is not None else 'N/A'
            alerts.append({
                'key': f"SIG_MOVE_{bnf_str}_{nf_str}_{vix_str}",
                'category': 'MARKET',
                'priority': 'important',
                'title': '📊 Significant Move',
                'body': f"BNF {bnf_str} ({spot_sigma}σ) | NF {nf_str} ({nf_spot_sigma}σ) | VIX {vix_str} ({vix_sigma}σ)",
            })

    if (now_ms - last_routine_notify) >= _CONST.get('ROUTINE_NOTIFY_MS', 3600000):
        live = ctx.get('live', {}) or {}
        bnf_spot = live.get('bnfSpot') or ctx.get('bnfSpot')
        nf_spot = live.get('nfSpot') or ctx.get('nfSpot')
        vix = live.get('vix') or ctx.get('vix')
        bnf_str = f"{int(bnf_spot)}" if bnf_spot is not None else 'N/A'
        nf_str = f"{int(nf_spot)}" if nf_spot is not None else 'N/A'
        vix_str = f"{vix:.1f}" if vix is not None else 'N/A'
        body = f"BNF {bnf_str} | NF {nf_str} | VIX {vix_str}"
        if open_trades:
            total_pnl = sum((t.get('current_pnl') or 0) for t in open_trades)
            body += f" | {len(open_trades)} pos P&L ₹{total_pnl}"
        if watchlist and not open_trades:
            top = watchlist[0]
            top_forces = top.get('forces') or {}
            top_aligned = top_forces.get('aligned', 0)
            top_type = top.get('type', '')
            body += f" | Top: {top_aligned}/3 {top_type}"
        alerts.append({
            'key': f"ROUTINE_{int(now_ms / _CONST.get('ROUTINE_NOTIFY_MS', 3600000))}",
            'category': 'ROUTINE',
            'priority': 'routine',
            'title': '📈 Market Update',
            'body': body,
        })
    return alerts


# ───────────────────────────────────────────────────────────────
# EXPLANATION + AUDIT AGENT — Phase A JSON only
# ───────────────────────────────────────────────────────────────
def _safe_num(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _top_insights(items, limit=4):
    scored = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        label = item.get("label") or item.get("title") or ""
        if not label:
            continue
        strength = _safe_num(item.get("strength", 1), 1)
        scored.append((strength, label, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [x[2] for x in scored[:limit]]


def build_session_explanation(result, ctx):
    verdict = result.get("verdict") or {}
    final = verdict.get("final") or {}
    eb = result.get("effective_bias") or {}
    regime = result.get("regime") or {}
    market_phase = result.get("marketPhase") or {}

    state = final.get("action") or verdict.get("action") or "WAIT"
    confidence = int(_safe_num(final.get("confidence", verdict.get("confidence", 0)), 0))
    bias = eb.get("bias") or verdict.get("direction") or "NEUTRAL"
    net = _safe_num(eb.get("net", verdict.get("net", 0)), 0)
    range_sigma = _safe_num(result.get("rangeSigma", regime.get("sigma", 0)), 0)
    regime_type = (regime.get("type") or market_phase.get("type") or "UNKNOWN").upper()

    all_insights = (
        (result.get("market") or []) +
        (result.get("timing") or []) +
        (result.get("risk") or [])
    )
    why = []
    for ins in _top_insights(all_insights, 5):
        label = ins.get("label", "")
        detail = ins.get("detail", "")
        why.append(f"{label}: {detail}" if detail else label)

    conflicts = []
    for c in verdict.get("conflicts") or []:
        if isinstance(c, str):
            conflicts.append(c)
        elif isinstance(c, dict):
            conflicts.append(c.get("label") or c.get("reason") or str(c))
    if regime_type == "RANGE" and abs(net) >= 1.0:
        conflicts.append(f"Brain bias {bias} but session is range-bound")
    if state == "WAIT" and confidence >= 50:
        conflicts.append("Confidence rising but executable strategy is not aligned")

    watch_for = []
    if state == "WAIT":
        watch_for.extend([
            "3/3 force alignment",
            "range break or confirmed range credit setup",
            "VIX and breadth confirmation",
        ])
    elif state in ("BUY PREMIUM", "SELL PREMIUM", "ENTER"):
        watch_for.extend([
            "force alignment staying >=2/3",
            "wall protection around sell strike",
            "premium behavior confirming thesis",
        ])

    headline = f"{state}: {regime_type.lower()} session, bias {bias}"
    if range_sigma:
        headline += f", range {range_sigma:.2f} sigma"

    return {
        "headline": headline,
        "state": state,
        "confidence": confidence,
        "regime": regime_type,
        "range_sigma": range_sigma,
        "bias": bias,
        "bias_net": net,
        "why": why[:5],
        "conflicts": conflicts[:5],
        "watch_for": watch_for[:5],
    }


def build_trade_audit(trade, position_result, ctx):
    trade_id = str(trade.get("id") or "")
    if not trade_id:
        return "", {}

    strategy = trade.get("strategy_type") or trade.get("type") or ""
    index_key = trade.get("index_key") or trade.get("index") or ""
    mode = trade.get("trade_mode") or trade.get("mode") or ""
    verdict = (position_result or {}).get("verdict") or {}
    insights = (position_result or {}).get("insights") or []
    live = (position_result or {}).get("live") or {}

    action = verdict.get("action") or "HOLD"
    urgency = verdict.get("urgency") or "ROUTINE"
    pnl = _safe_num(trade.get("current_pnl", live.get("current_pnl")), 0)
    max_profit = _safe_num(trade.get("max_profit"), 0)
    max_loss = _safe_num(trade.get("max_loss"), 0)
    pnl_pct = round((pnl / max_profit) * 100) if max_profit > 0 else 0

    forces = trade.get("forces") or {}
    aligned = forces.get("aligned", trade.get("force_alignment"))
    aligned_n = _safe_num(aligned, 0)

    is_ic_ib = strategy in ("IRON_CONDOR", "IRON_BUTTERFLY")
    ic_ib_intraday_ok = (not is_ic_ib) or (mode == "intraday")
    max_loss_ok = max_loss <= _safe_num(ctx.get("capital", 250000), 250000) * 0.10
    forces_ok = aligned is None or aligned_n >= 2

    why = []
    if verdict.get("reason"):
        why.append(verdict["reason"])
    if max_profit:
        why.append(f"P&L is {pnl_pct}% of max profit")
    if aligned is not None:
        why.append(f"Forces are {int(aligned_n)}/3")
    for ins in _top_insights(insights, 3):
        label = ins.get("label", "")
        if label:
            why.append(label)

    risk_flags = []
    if not ic_ib_intraday_ok:
        risk_flags.append("IC/IB must not be overnight")
    if not max_loss_ok:
        risk_flags.append("Max loss exceeds 10% capital")
    if aligned is not None and aligned_n <= 1:
        risk_flags.append("Forces weakened to danger zone")

    forces_component = (aligned_n / 3.0) * 40 if aligned is not None else 20
    pnl_component = max(0, pnl_pct) * 0.6
    score = min(100, max(0, int(forces_component + pnl_component)))

    headline = f"{action}: {index_key} {strategy}"
    if pnl:
        headline += f" P&L Rs {round(pnl)}"

    return trade_id, {
        "headline": headline,
        "action": action,
        "urgency": urgency,
        "score": score,
        "why": why[:5],
        "risk_flags": risk_flags[:5],
        "rule_checks": {
            "max_loss_ok": bool(max_loss_ok),
            "ic_ib_intraday_ok": bool(ic_ib_intraday_ok),
            "forces_ok": bool(forces_ok),
        },
    }


def decide_agent_notification(agent, result, ctx):
    summary = agent.get("session_summary") or {}
    state = summary.get("state", "WAIT")
    confidence = int(_safe_num(summary.get("confidence"), 0))
    regime = summary.get("regime", "UNKNOWN")
    range_sigma = _safe_num(summary.get("range_sigma"), 0)
    conflicts = summary.get("conflicts") or []

    if state in ("BUY PREMIUM", "SELL PREMIUM", "ENTER") and confidence >= 55:
        return {
            "severity": "entry",
            "dedupe_bucket": f"SETUP_{state}_{regime}",
            "should_notify": True,
            "reason": "Actionable setup with sufficient confidence",
        }
    if state == "WAIT" and conflicts and confidence >= 45:
        return {
            "severity": "routine",
            "dedupe_bucket": f"WAIT_CONFLICT_{regime}",
            "should_notify": False,
            "reason": "Conflict explains WAIT but is not actionable",
        }
    if regime == "RANGE" and range_sigma <= 0.35:
        return {
            "severity": "routine",
            "dedupe_bucket": "RANGE_BOUND_LOW_SIGMA",
            "should_notify": False,
            "reason": "Range continues without new entry trigger",
        }
    return {
        "severity": "routine",
        "dedupe_bucket": f"{state}_{regime}",
        "should_notify": False,
        "reason": "No actionable state change",
    }


def build_explanation_audit_agent(result, ctx, open_trades):
    agent = {
        "schema": 1,
        "mode": "EXPLAIN_AUDIT",
        "session_summary": build_session_explanation(result, ctx),
        "notification_context": {},
        "trade_audits": {},
        "data_quality": {"missing": [], "warnings": []},
    }

    positions = result.get("positions") or {}
    for trade in open_trades or []:
        tid = str(trade.get("id") or "")
        pos = positions.get(tid) or {}
        audit_id, audit = build_trade_audit(trade, pos, ctx)
        if audit_id:
            agent["trade_audits"][audit_id] = audit

    if not result.get("effective_bias"):
        agent["data_quality"]["warnings"].append("effective_bias missing")
    if result.get("rangeSigma") is None:
        agent["data_quality"]["warnings"].append("rangeSigma missing")
    if not result.get("watchlist"):
        agent["data_quality"]["warnings"].append("watchlist empty")

    agent["notification_context"] = decide_agent_notification(agent, result, ctx)
    return agent


def compute_position_live(trade, bnf_chain, nf_chain, spots, vix, ctx, breadth):
    """Phase D: brain.py becomes sole producer of position P&L.
    Replaces JS updateOpenTradePnL (2-leg only) and Kotlin 
    updateOpenTradesPnL (silently skipped on missing lot_size).
    """
    def _num(value, default=0):
        try:
            if value is None or value == '':
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    idx = trade.get('index_key', 'BNF')
    base_lot = 30 if idx == 'BNF' else 65 if idx == 'NF' else 0

    # `lots` is trade quantity in lots, not lot size. App trades usually store
    # `lots: 1`, so treating it as lot_size under-scales BNF P&L by 30x.
    entry_snapshot = trade.get('entry_snapshot') or {}
    if not isinstance(entry_snapshot, dict):
        entry_snapshot = {}
    explicit_lot_size = _num(
        trade.get('lot_size') or trade.get('lotSize') or entry_snapshot.get('lot_size') or entry_snapshot.get('lotSize'),
        0
    )
    lots_count = max(_num(trade.get('lots'), 1), 1)
    lot_size = explicit_lot_size if explicit_lot_size > 0 else base_lot * lots_count

    if lot_size <= 0:
        return None

    sell_s   = trade.get('sell_strike', 0)
    buy_s    = trade.get('buy_strike', 0)
    sell_s2  = trade.get('sell_strike2', 0)
    buy_s2   = trade.get('buy_strike2', 0)
    stype    = trade.get('strategy_type')
    if not stype:
        return None

    is_credit = trade.get('is_credit', False) or stype in (
        'BEAR_CALL', 'BULL_PUT', 'IRON_CONDOR', 'IRON_BUTTERFLY'
    )
    entry_premium = trade.get('entry_premium', 0)

    # Build cache from chain or ltpMap
    cache = {}
    # Prefer ltpMap from ctx if available for performance
    ltp_map = ctx.get('bnfLtpMap' if idx == 'BNF' else 'nfLtpMap')
    if ltp_map:
        for k_str, pair in ltp_map.items():
            cache[int(float(k_str))] = (pair.get('CE', 0), pair.get('PE', 0))
    else:
        chain = bnf_chain if idx == 'BNF' else nf_chain
        strikes = chain.get('strikes', {})
        for k_str, sd in strikes.items():
            try:
                k = int(float(k_str))
                ce_ltp = (sd.get('CE') or {}).get('ltp', 0) or 0
                pe_ltp = (sd.get('PE') or {}).get('ltp', 0) or 0
                cache[k] = (ce_ltp, pe_ltp)
            except: continue

    spot  = spots.get('bnfSpot' if idx == 'BNF' else 'nfSpot', 0)

    def _intrinsic(strike, leg):  # leg: 0=CE, 1=PE
        if not spot or strike is None:
            return 0.0
        if leg == 0:  # CE
            return max(0.0, float(spot) - float(strike))
        return max(0.0, float(strike) - float(spot))  # PE

    def _quote_present(strike):
        try:
            return int(strike) in cache
        except Exception:
            return False

    def _get(strike, leg):  # leg: 0=CE, 1=PE
        raw = (cache.get(int(strike)) or (0, 0))[leg]
        # Some first-poll payloads report 0 LTP for deep OTM legs. Treat this as
        # a valid mark and fall back to intrinsic so P&L is still computable.
        return raw if raw > 0 else _intrinsic(strike, leg)

    current_net = 0.0
    if stype == 'BEAR_CALL':
        current_net = _get(sell_s, 0) - _get(buy_s, 0)
    elif stype == 'BULL_PUT':
        current_net = _get(sell_s, 1) - _get(buy_s, 1)
    elif stype in ('IRON_CONDOR', 'IRON_BUTTERFLY'):
        current_net = (_get(sell_s, 0) - _get(buy_s, 0)) + \
                      (_get(sell_s2, 1) - _get(buy_s2, 1))
    elif stype == 'BULL_CALL':
        current_net = _get(buy_s, 0) - _get(sell_s, 0)
    elif stype == 'BEAR_PUT':
        current_net = _get(buy_s, 1) - _get(sell_s, 1)

    required_legs = []
    if stype in ('BEAR_CALL', 'BULL_CALL'):
        required_legs = [(sell_s, 0), (buy_s, 0)]
    elif stype in ('BULL_PUT', 'BEAR_PUT'):
        required_legs = [(sell_s, 1), (buy_s, 1)]
    elif stype in ('IRON_CONDOR', 'IRON_BUTTERFLY'):
        required_legs = [(sell_s, 0), (buy_s, 0), (sell_s2, 1), (buy_s2, 1)]

    has_any_chain_leg = any(_quote_present(s) for s, _ in required_legs)
    if not has_any_chain_leg and spot <= 0:
        return None  # neither chain nor spot fallback available

    if is_credit:
        pnl = (entry_premium - current_net) * lot_size
    else:
        pnl = (current_net - entry_premium) * lot_size

    # Peak tracking — only positive peaks
    prev_peak = trade.get('peak_pnl', 0) or 0
    new_peak = max(prev_peak, pnl) if pnl > 0 else prev_peak

    # Trough tracking — most negative seen
    prev_trough = trade.get('trough_pnl', 0) or 0
    new_trough = min(prev_trough, pnl) if pnl < 0 else prev_trough

    # Erosion — % of peak lost (only if peak >= 500)
    if new_peak >= 500:
        erosion = round((new_peak - pnl) / new_peak * 100, 1)
    else:
        erosion = 0.0

    # VIX change since entry
    entry_vix = trade.get('entry_vix', 0) or 0
    v_curr = vix or 15
    vix_change = round(v_curr - entry_vix, 2) if entry_vix > 0 else 0.0

    # Journey point (10-min throttle)
    import time
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    now_dt = datetime.now(ist)
    now_str = now_dt.strftime("%H:%M")

    journey = trade.get('journey', []) or []
    if isinstance(journey, str): # Handle possible json string
        import json
        try: journey = json.loads(journey)
        except: journey = []
    
    last = journey[-1] if journey else None
    add_point = True
    if last:
        try:
            last_t = last.get('t', '')
            h1, m1 = map(int, last_t.split(':'))
            h2, m2 = map(int, now_str.split(':'))
            diff = (h2*60 + m2) - (h1*60 + m1)
            if diff < 10:
                add_point = False
        except: add_point = True

    journey_point = None
    if add_point:
        journey_point = {'t': now_str, 'pnl': round(pnl), 'spot': round(spot, 2)}
        journey.append(journey_point)
        if len(journey) > 100:
            journey = journey[-100:]

    return {
        'trade_id': trade.get('id'),
        'current_pnl': round(pnl),
        'current_spot': round(spot, 2),
        'peak_pnl': round(new_peak),
        'trough_pnl': round(new_trough),
        'peak_erosion': erosion,
        'vix_change': vix_change,
        'lot_size_resolved': lot_size,
        'journey': journey,
        'journey_point_added': journey_point,
        'current_net_premium': round(current_net, 2),
    }

def compute_control_index(trade, chain, spot, breadth):
    """Decision #17. Returns int (-100 to +100)."""
    if not trade or not chain: return 0

    stype = trade.get('strategy_type', '')
    is_bear = 'BEAR' in stype
    is_bull = 'BULL' in stype
    is_ic = stype in ('IRON_CONDOR', 'IRON_BUTTERFLY')
    score = 0

    # Signal 1: Max Pain Migration (35%)
    entry_mp = trade.get('entry_max_pain') or chain.get('maxPain')
    curr_mp = chain.get('maxPain')
    if entry_mp and curr_mp:
        mp_move = curr_mp - entry_mp
        mp_score = 0
        if is_bear and mp_move < 0: mp_score = 1
        elif is_bear and mp_move > 0: mp_score = -1
        elif is_bull and mp_move > 0: mp_score = 1
        elif is_bull and mp_move < 0: mp_score = -1
        elif is_ic: mp_score = 1 if abs(mp_move) < 200 else -1
        score += mp_score * 35

    # Signal 2: Sell Strike OI change (30%)
    strikes = chain.get('strikes', {})
    s_strike = trade.get('sell_strike')
    s_type = trade.get('sell_type') or ('CE' if is_bear else 'PE' if is_bull else None)
    
    sell_data = strikes.get(str(s_strike), {}).get(s_type) if s_strike and s_type else None
    entry_oi = trade.get('entry_sell_oi')
    if sell_data and entry_oi:
        oi_change = sell_data.get('oi', 0) - entry_oi
        if is_ic:
            score += (15 if oi_change > 0 else -15 if oi_change < 0 else 0)
            s_strike2 = trade.get('sell_strike2')
            s_type2 = trade.get('sell_type2')
            sell_data2 = strikes.get(str(s_strike2), {}).get(s_type2) if s_strike2 and s_type2 else None
            entry_oi2 = trade.get('entry_sell_oi2')
            if sell_data2 and entry_oi2:
                oi_change2 = sell_data2.get('oi', 0) - entry_oi2
                score += (15 if oi_change2 > 0 else -15 if oi_change2 < 0 else 0)
        else:
            score += (30 if oi_change > 0 else -30 if oi_change < 0 else 0)

    # Signal 3: PCR Shift (25%)
    entry_pcr = trade.get('entry_pcr')
    curr_pcr = chain.get('nearAtmPCR') or chain.get('pcr')
    if entry_pcr and curr_pcr:
        pcr_change = curr_pcr - entry_pcr
        pcr_score = 0
        if is_ic:
            if abs(pcr_change) < 0.05: pcr_score = 1
            elif abs(pcr_change) > 0.15: pcr_score = -1
        elif is_bear and pcr_change < -0.05: pcr_score = 1
        elif is_bear and pcr_change > 0.05: pcr_score = -1
        elif is_bull and pcr_change > 0.05: pcr_score = 1
        elif is_bull and pcr_change < -0.05: pcr_score = -1
        score += pcr_score * 25

    # Signal 4: Heavyweight Divergence (10%)
    if breadth and trade.get('index_key') == 'BNF':
        wp = breadth.get('weightedPct', 0)
        hw_score = 0
        if is_bear and wp < -0.5: hw_score = 1
        elif is_bear and wp > 0.5: hw_score = -1
        elif is_bull and wp > 0.5: hw_score = 1
        elif is_bull and wp < -0.5: hw_score = -1
        score += hw_score * 10

    # Signal 5: Strike Breach Override
    if spot and s_strike:
        breach = False
        if is_bear and spot > s_strike: breach = True
        elif is_bull and spot < s_strike: breach = True
        elif is_ic:
            s_strike2 = trade.get('sell_strike2')
            if s_strike2:
                if stype == 'IRON_BUTTERFLY':
                    tol = (trade.get('width') or 300) / 4
                    if spot > s_strike + tol or spot < s_strike2 - tol: breach = True
                else:
                    if spot > s_strike or spot < s_strike2: breach = True
        if breach:
            score = min(score, -50)

    return int(max(-100, min(100, score)))

def compute_wall_drift(trade, chain):
    """Decision #18. Per-trade wall drift tracker."""
    if not trade or not chain: return None
    if not trade.get('is_credit') and trade.get('strategy_type') not in ('BEAR_CALL', 'BULL_PUT', 'IRON_CONDOR', 'IRON_BUTTERFLY'):
        return None

    stype = trade.get('strategy_type', '')
    is_bear = 'BEAR' in stype
    is_bull = 'BULL' in stype
    is_ic = stype == 'IRON_CONDOR'
    if stype == 'IRON_BUTTERFLY': return None

    # Decision #6 / Phase D Item 2: Use 200/100 for consistency
    step = 200 if trade.get('index_key') == 'BNF' else 100
    
    e_snapshot = trade.get('entry_snapshot') or {}
    e_cw = e_snapshot.get('call_wall') or trade.get('entry_call_wall')
    e_pw = e_snapshot.get('put_wall') or trade.get('entry_put_wall')
    c_cw = chain.get('callWallStrike')
    c_pw = chain.get('putWallStrike')

    res = {'callSide': None, 'putSide': None, 'warning': None, 'severity': 0}

    if (is_bear or is_ic) and e_cw and c_cw:
        sell_ce = trade.get('sell_strike')
        c_gap = c_cw - sell_ce
        w_move = c_cw - e_cw
        if w_move < -step:
            res['callSide'] = {'entry': e_cw, 'current': c_cw, 'drift': w_move, 'status': 'EXPOSED' if c_gap <= 0 else 'WEAKENED'}
            res['severity'] = max(res['severity'], 2 if c_gap <= 0 else 1)

    if (is_bull or is_ic) and e_pw and c_pw:
        sell_pe = trade.get('sell_strike2') if is_ic else trade.get('sell_strike')
        c_gap = sell_pe - c_pw
        w_move = c_pw - e_pw
        if w_move > step:
            res['putSide'] = {'entry': e_pw, 'current': c_pw, 'drift': w_move, 'status': 'EXPOSED' if c_gap <= 0 else 'WEAKENED'}
            res['severity'] = max(res['severity'], 2 if c_gap <= 0 else 1)

    if res['callSide']:
        s = res['callSide']
        res['warning'] = f"🛡️↓ Call wall {s['entry']}→{s['current']} ({s['drift']}). {'SELL EXPOSED' if s['status']=='EXPOSED' else 'Weakened.'}"
    if res['putSide']:
        s = res['putSide']
        m = f"🛡️↑ Put wall {s['entry']}→{s['current']} (+{s['drift']}). {'SELL EXPOSED' if s['status']=='EXPOSED' else 'Weakened.'}"
        res['warning'] = (res['warning'] + ' | ' + m) if res['warning'] else m

    return res if res['severity'] > 0 else None


def update_watchlist_forces(watchlist, ctx, vix, iv_pctl, regime=None):
    """Decision #19. Mutates watchlist in place."""
    bias = (ctx.get('effective_bias') or ctx.get('morningBias') 
            or {'bias': 'NEUTRAL', 'strength': '', 'net': 0})
    for c in watchlist:
        try:
            c['forces'] = _get_forces(c.get('type'), bias, vix, iv_pctl, regime)
        except: pass
    return watchlist

def get_contrarian_pcr(chain):
    """Decision #20. Extreme PCR readings = contrarian signal."""
    flags = []
    if not chain:
        return {'flags': flags}
    pcr = chain.get('nearAtmPCR') or chain.get('pcr')
    if pcr is None: return {'flags': flags}
    
    # Directive: 1.6 (corrected to 1.5 per JS L2839)
    if pcr < 0.6:
        flags.append({
            'type': 'contrarian_pcr', 'icon': '🔄',
            'label': f'PCR extremely low ({pcr:.2f})',
            'detail': 'Excessive call buying — contrarian bearish.',
            'impact': 'bearish', 'strength': 3
        })
    elif pcr > 1.5:
        flags.append({
            'type': 'contrarian_pcr', 'icon': '🔄',
            'label': f'PCR extremely high ({pcr:.2f})',
            'detail': 'Excessive put buying — contrarian bullish.',
            'impact': 'bullish', 'strength': 3
        })
    return {'flags': flags}

def get_institutional_pcr(current_pcr, vix, gap_info, history, afternoon_baseline, live_chain):
    """Decision #21. High calibration institutional PCR read."""
    if not current_pcr: return None
    
    pcr = current_pcr
    is_high = pcr > 1.3
    is_low = pcr < 0.7
    is_extreme = pcr > 1.5 or pcr < 0.6
    high_vix = vix >= 20
    very_high_vix = vix >= 24
    
    gap_sigma = gap_info.get('sigma', 0) if gap_info else 0
    big_gap_down = gap_sigma <= -1
    big_gap_up = gap_sigma >= 1
    
    yday_pcr = history[0].get('pcr') if history and len(history) > 0 else None
    pcr_rising = (pcr - yday_pcr > 0.05) if yday_pcr else False
    pcr_falling = (pcr - yday_pcr < -0.05) if yday_pcr else False
    
    reading = ''
    bias = 'NEUTRAL'
    confidence = 'LOW'
    severity = 3 # medium
    
    # 9-branch decision tree from JS L2893
    if is_high and very_high_vix and big_gap_down:
        reading = f"PCR {pcr:.2f} — Fear hedging (VIX {vix:.1f}, {gap_sigma}σ gap-down). Panic puts, not institutional conviction."
        bias = 'NEUTRAL'; confidence = 'LOW'; severity = 3
    elif is_high and high_vix and big_gap_down:
        reading = f"PCR {pcr:.2f} — Defensive hedging (VIX {vix:.1f}, gap-down). Floor may hold but driven by fear."
        bias = 'MILD_BULL'; confidence = 'LOW'; severity = 3
    elif is_high and high_vix and not big_gap_down:
        reading = f"PCR {pcr:.2f} — Institutional floor + elevated IV (VIX {vix:.1f}). Support exists, credit sellers favored."
        bias = 'BULL'; confidence = 'MEDIUM'; severity = 4
    elif is_high and not high_vix and not big_gap_down:
        reading = f"PCR {pcr:.2f} — Institutional floor (VIX {vix:.1f}, normal day). Deliberate put writing = support."
        bias = 'BULL'; confidence = 'HIGH'; severity = 4
    elif is_high and pcr_rising and not big_gap_down:
        reading = f"PCR {pcr:.2f} — Active floor building (rising from {yday_pcr:.2f if yday_pcr else 0}). Institutions adding support."
        bias = 'BULL'; confidence = 'MEDIUM'; severity = 4
    elif is_high and pcr_falling:
        reading = f"PCR {pcr:.2f} — Floor unwinding (was {yday_pcr:.2f if yday_pcr else 0}). Support weakening."
        bias = 'BEAR'; confidence = 'MEDIUM'; severity = 4
    elif is_low and high_vix and big_gap_up:
        reading = f"PCR {pcr:.2f} — Euphoria calls (VIX {vix:.1f}, gap-up). Caution — VIX still elevated."
        bias = 'NEUTRAL'; confidence = 'LOW'; severity = 3
    elif is_low and not high_vix:
        reading = f"PCR {pcr:.2f} — Institutions buying calls. Directional bullish bet."
        bias = 'BEAR'; confidence = 'HIGH'; severity = 4
    elif is_low and high_vix:
        reading = f"PCR {pcr:.2f} — Low PCR despite high VIX. Aggressive call buying or put unwinding."
        bias = 'BEAR'; confidence = 'MEDIUM'; severity = 4
    elif is_extreme:
        reading = f"PCR {pcr:.2f} — Extreme level. Watch for reversal."
        bias = 'BULL' if pcr > 1.5 else 'BEAR'; confidence = 'MEDIUM'; severity = 4
    else:
        reading = f"PCR {pcr:.2f} — Normal range. No extreme institutional signal."
        bias = 'NEUTRAL'; confidence = 'LOW'; severity = 2
        
    # Phase B: 2pm OI enrichment
    oi_delta = None
    if afternoon_baseline and live_chain:
        b_c_oi = afternoon_baseline.get('bnfTotalCallOi') or afternoon_baseline.get('bnf_total_call_oi') or 0
        b_p_oi = afternoon_baseline.get('bnfTotalPutOi') or afternoon_baseline.get('bnf_total_put_oi') or 0
        l_c_oi = live_chain.get('totalCallOI') or 0
        l_p_oi = live_chain.get('totalPutOI') or 0
        
        if b_c_oi > 0 and b_p_oi > 0:
            c_delta = l_c_oi - b_c_oi
            p_delta = l_p_oi - b_p_oi
            oi_delta = {'callDelta': c_delta, 'putDelta': p_delta}
            
            def fmt(v): return f"{'+' if v>0 else '-'}{abs(v)/100000:.1f}L"
            
            if c_delta > p_delta * 1.5 and c_delta > 50000:
                reading += f" Since 2PM: Calls {fmt(c_delta)} vs Puts {fmt(p_delta)} — ceiling building."
            elif p_delta > c_delta * 1.5 and p_delta > 50000:
                reading += f" Since 2PM: Puts {fmt(p_delta)} vs Calls {fmt(c_delta)} — active floor building."
            elif abs(c_delta) < 30000 and abs(p_delta) < 30000:
                reading += " Since 2PM: Minimal OI change."
            else:
                reading += f" Since 2PM: Calls {fmt(c_delta)}, Puts {fmt(p_delta)} — balanced."

    return {
        'pcr': float(pcr),
        'reading': reading,
        'bias': bias,
        'confidence': confidence,
        'severity': severity,
        'sessionTrend': 'flat', # Placeholder for multi-session logic
        'oiDelta': oi_delta,
        'phase': 'afternoon' if afternoon_baseline else 'morning',
        'vix': float(vix),
        'gapSigma': float(gap_sigma)
    }

def get_session_trajectory(history):
    """Decision #22. Ported JS faithful (Option A)."""
    if not history or len(history) < 2: return None
    sessions = history[:5][::-1] # oldest first
    fields = ['vix', 'pcr', 'fii_cash', 'fii_short_pct', 'bnf_spot', 'nf_spot']
    labels = ['VIX', 'PCR', 'FII Cash', 'FII Short%', 'BNF', 'NF']
    trajectory = []

    for f_idx, field in enumerate(fields):
        row = {'label': labels[f_idx], 'arrows': []}
        for i in range(1, len(sessions)):
            prev = sessions[i-1].get(field)
            curr = sessions[i].get(field)
            if prev is None or curr is None:
                row['arrows'].append('-')
                continue
            diff = curr - prev
            # Thresholds from JS L3193
            thresh = 0.3 if field == 'vix' else 0.03 if field == 'pcr' else 0.5 if field == 'fii_short_pct' else 50 if 'spot' in field else 100
            row['arrows'].append('↑' if diff > thresh else '↓' if diff < -thresh else '→')
        trajectory.append(row)

    rev_count = 0
    if len(sessions) >= 3:
        for row in trajectory:
            if len(row['arrows']) >= 2:
                last = row['arrows'][-1]
                prev = row['arrows'][-2]
                if prev != '→' and last != '→' and last != prev:
                    rev_count += 1

    last_arrows = [r['arrows'][-1] for r in trajectory if r['arrows'] and r['arrows'][-1] != '—']
    up_c = last_arrows.count('↑')
    dn_c = last_arrows.count('↓')

    return {
        'trajectory': trajectory,
        'dates': [s.get('date') for s in sessions],
        'reversal': 'Possible institutional shift — 3+ signals reversed' if rev_count >= 3 else None,
        'alignment': 'Institutional accumulation pressure — 4+ signals rising' if up_c >= 4 else 'Institutional selling pressure — 4+ signals falling' if dn_c >= 4 else None
    }

def get_strike(strikes_dict, k):

    """Helper for Phase C: handle both string and numeric strike keys."""
    if not strikes_dict: return None
    return strikes_dict.get(k) or strikes_dict.get(str(k)) or strikes_dict.get(str(float(k))) or strikes_dict.get(int(k) if isinstance(k, (int, float)) else None)

def chain_profile(chain, spot, ohlc, vix=None, gap=None):
    """Phase C: 30-feature chain profile builder.
    Replaces JS computeChainProfile(). Returns dict or None.
    """
    if not chain or not chain.get('strikes') or not chain.get('atm') or not chain.get('allStrikes'):
        return None
        
    strikes = chain['strikes']
    all_k = chain['allStrikes']
    vix = vix if vix is not None else 20
    
    # daily_sigma preference: ctx-provided > computed
    daily_sigma = chain.get('dailySigma') or _daily_sigma(spot, vix)
    if not daily_sigma or daily_sigma <= 0:
        return None
        
    step = (all_k[1] - all_k[0]) if len(all_k) > 1 else (100 if spot > 30000 else 50)
    if step <= 0:
        step = 100 if spot > 30000 else 50

    # IV Skew: OTM put IV vs OTM call IV at ~0.5σ
    otm_dist_raw = round(daily_sigma * 0.5 / step) * step
    otm_dist = otm_dist_raw if otm_dist_raw else step
    
    atm = chain['atm']
    c_iv = (get_strike(strikes, atm + otm_dist) or {}).get('CE', {}).get('iv', 0) or 0
    p_iv = (get_strike(strikes, atm - otm_dist) or {}).get('PE', {}).get('iv', 0) or 0

    # Sigma-zone PCR
    def zone_pcr(lo, hi):
        c_oi, p_oi = 0, 0
        for k in all_k:
            sigma = abs(k - atm) / daily_sigma
            if lo <= sigma < hi:
                sd = get_strike(strikes, k) or {}
                c_oi += sd.get('CE', {}).get('oi', 0) or 0
                p_oi += sd.get('PE', {}).get('oi', 0) or 0
        return round(p_oi / c_oi, 2) if c_oi > 0 else 0.0

    # Wall freshness + OI change
    def wall_fresh(strike, leg_type):
        sd = get_strike(strikes, strike) or {}
        d = sd.get(leg_type) or {}
        vol = d.get('volume', 0) or 0
        oi = d.get('oi', 0) or 0
        prev_oi = d.get('prev_oi')
        fresh = round(vol / max(1, oi), 3)
        oi_chg = (oi - prev_oi) if prev_oi is not None else None
        return {'fresh': fresh, 'oiChg': oi_chg}

    cw = wall_fresh(chain.get('callWallStrike'), 'CE')
    pw = wall_fresh(chain.get('putWallStrike'), 'PE')

    # ATM data
    atm_data = get_strike(strikes, atm) or {}
    atm_ce = atm_data.get('CE') or {}
    atm_pe = atm_data.get('PE') or {}
    
    # Day range
    day_range = 0.5
    if ohlc and ohlc.get('high', 0) > ohlc.get('low', 0):
        day_range = round((spot - ohlc['low']) / (ohlc['high'] - ohlc['low']), 2)
        
    # Concentration (top 3)
    near_k = [k for k in all_k if abs(k - atm) / daily_sigma < 1.5]
    c_ois = sorted([(get_strike(strikes, k) or {}).get('CE', {}).get('oi', 0) or 0 for k in near_k], reverse=True)
    p_ois = sorted([(get_strike(strikes, k) or {}).get('PE', {}).get('oi', 0) or 0 for k in near_k], reverse=True)
    c_total = sum(c_ois)
    p_total = sum(p_ois)

    # Gap fill
    gap_val = gap.get('gap') if gap else None
    gap_fill = None
    if ohlc and gap_val and abs(gap_val) > 10:
        gap_fill = round(min(1.0, abs(spot - ohlc.get('open', spot)) / abs(gap_val)), 2)
        
    # Greeks
    # 1. IV Slope
    otm_dist2_raw = round(daily_sigma * 1.0 / step) * step
    otm_dist2 = otm_dist2_raw if otm_dist2_raw else step * 2
    far_p_iv = (get_strike(strikes, atm - otm_dist2) or {}).get('PE', {}).get('iv', 0) or 0
    iv_slope = round((far_p_iv - c_iv) / 2, 1) if far_p_iv > 0 and c_iv > 0 else 0.0
    
    # 2. Gamma Cluster
    g_total, g_near = 0.0, 0.0
    for k in near_k:
        sd = get_strike(strikes, k) or {}
        g = abs(sd.get('CE', {}).get('gamma', 0) or 0) + abs(sd.get('PE', {}).get('gamma', 0) or 0)
        g_total += g
        if abs(k - atm) / daily_sigma < 0.3:
            g_near += g
    gamma_cluster = round(g_near / g_total, 3) if g_total > 0 else 0.0
    
    # 3. Volume Ratio
    c_vol, p_vol = 0, 0
    for k in near_k:
        sd = get_strike(strikes, k) or {}
        c_vol += sd.get('CE', {}).get('volume', 0) or 0
        p_vol += sd.get('PE', {}).get('volume', 0) or 0
    if p_vol > 0:
        vol_ratio = round(c_vol / p_vol, 2)
    else:
        vol_ratio = 5.0 if c_vol > 0 else 1.0
        
    # 4. Net Delta
    n_delta = 0.0
    for k in near_k:
        sd = get_strike(strikes, k) or {}
        ce = sd.get('CE') or {}
        pe = sd.get('PE') or {}
        n_delta += (ce.get('oi', 0) or 0) * (ce.get('delta', 0) or 0) + (pe.get('oi', 0) or 0) * (pe.get('delta', 0) or 0)
    norm_delta = round(n_delta / 100000, 2)
    
    # 5. Avg Theta/Vega
    n_len = len(near_k)
    t_sum, v_sum = 0.0, 0.0
    for k in near_k:
        sd = get_strike(strikes, k) or {}
        t_sum += abs(sd.get('CE', {}).get('theta', 0) or 0) + abs(sd.get('PE', {}).get('theta', 0) or 0)
        v_sum += abs(sd.get('CE', {}).get('vega', 0) or 0) + abs(sd.get('PE', {}).get('vega', 0) or 0)
    avg_theta = round(t_sum / n_len, 2) if n_len > 0 else 0.0
    avg_vega = round(v_sum / n_len, 2) if n_len > 0 else 0.0
    
    # 7. OI Velocity
    v_oi = 0
    top_c_k = sorted(near_k, key=lambda k: (get_strike(strikes, k) or {}).get('CE', {}).get('oi', 0) or 0, reverse=True)[:5]
    top_p_k = sorted(near_k, key=lambda k: (get_strike(strikes, k) or {}).get('PE', {}).get('oi', 0) or 0, reverse=True)[:5]
    for k in top_c_k:
        d = (get_strike(strikes, k) or {}).get('CE') or {}
        if d.get('prev_oi') is not None: v_oi += (d.get('oi', 0) or 0) - d['prev_oi']
    for k in top_p_k:
        d = (get_strike(strikes, k) or {}).get('PE') or {}
        if d.get('prev_oi') is not None: v_oi += (d.get('oi', 0) or 0) - d['prev_oi']
    norm_v_oi = round(v_oi / 10000, 2)
    
    # 8. Bid-Ask Quality
    s_sum, s_cnt = 0.0, 0
    for k in near_k:
        sd = get_strike(strikes, k) or {}
        for leg in ['CE', 'PE']:
            d = sd.get(leg) or {}
            ltp = d.get('ltp', 0) or 0
            if ltp > 1:
                ask = d.get('ask', 0) or 0
                bid = d.get('bid', 0) or 0
                s_sum += (ask - bid) / ltp
                s_cnt += 1
    ba_quality = round(s_sum / s_cnt * 100, 2) if s_cnt > 0 else 0.0
    
    # 9. Cluster Depth
    radius = 5 if spot > 30000 else 4
    cw_k = chain.get('callWallStrike')
    pw_k = chain.get('putWallStrike')
    all_c_oi = [(get_strike(strikes, k) or {}).get('CE', {}).get('oi', 0) or 0 for k in all_k]
    all_p_oi = [(get_strike(strikes, k) or {}).get('PE', {}).get('oi', 0) or 0 for k in all_k]
    c_med = statistics.median([v for v in all_c_oi if v > 0]) if any(v > 0 for v in all_c_oi) else 0
    p_med = statistics.median([v for v in all_p_oi if v > 0]) if any(v > 0 for v in all_p_oi) else 0
    
    cc_depth, pc_depth = 0, 0
    for k in all_k:
        sd = get_strike(strikes, k) or {}
        if cw_k and abs(k - cw_k) <= radius * step and (sd.get('CE', {}).get('oi', 0) or 0) > c_med * 2:
            cc_depth += 1
        if pw_k and abs(k - pw_k) <= radius * step and (sd.get('PE', {}).get('oi', 0) or 0) > p_med * 2:
            pc_depth += 1

    return {
        'ivSkew': round(p_iv - c_iv, 1),
        'pcrZ1': zone_pcr(0.3, 0.5), 'pcrZ2': zone_pcr(0.5, 0.8), 'pcrZ3': zone_pcr(0.8, 1.2),
        'cwFresh': cw['fresh'], 'cwOiChg': cw['oiChg'], 'pwFresh': pw['fresh'], 'pwOiChg': pw['oiChg'],
        'atmSpread': round((atm_ce.get('ask', 0) or 0) - (atm_ce.get('bid', 0) or 0) + (atm_pe.get('ask', 0) or 0) - (atm_pe.get('bid', 0) or 0), 1),
        'dayRange': day_range, 'gapFill': gap_fill,
        'callConc': round(sum(c_ois[:3]) / c_total, 3) if c_total > 0 else 0.0,
        'putConc': round(sum(p_ois[:3]) / p_total, 3) if p_total > 0 else 0.0,
        'pctFromOpen': round((spot - ohlc['open']) / ohlc['open'] * 100, 2) if ohlc and ohlc.get('open') else 0.0,
        'maxPain': chain.get('maxPain'), 'callWall': chain.get('callWallStrike'), 'putWall': chain.get('putWallStrike'),
        'atm': atm, 'spot': spot, 'atmGamma': atm_ce.get('gamma'),
        'ivSlope': iv_slope, 'gammaCluster': gamma_cluster, 'volRatio': vol_ratio,
        'netDelta': norm_delta, 'avgTheta': avg_theta, 'avgVega': avg_vega,
        'oiVelocity': norm_v_oi, 'bidAskQuality': ba_quality,
        'callClusterDepth': cc_depth, 'putClusterDepth': pc_depth
    }

def chain_profile_insights(ctx):
    """Phase C: emit chain insight cards from bnfProfile features.
    Replaces chain_intelligence().
    """
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
    
    return insights

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
    # TASK 5.3 — snapshot inputs for trace
    if ctx.get('_trace') is not None:
        _trace = ctx['_trace']['verdict']
        vixs_here = get_vix_vals(polls)
        _trace['inputs'] = {
            'insights_count': len(all_insights),
            'regime_type': regime.get('type', 'unknown'),
            'vix_current': vixs_here[-1] if vixs_here else None,
            'bnfDTE': ctx.get('bnfDTE'),
            'nfDTE': ctx.get('nfDTE'),
            'tradeMode': ctx.get('tradeMode'),
            'breadth_pct': (ctx.get('bnfBreadth') or {}).get('pct'),
            'iv_skew': (ctx.get('bnfProfile') or {}).get('ivSkew', 0),
            'cw_fresh': (ctx.get('bnfProfile') or {}).get('cwFresh', 0),
            'pw_fresh': (ctx.get('bnfProfile') or {}).get('pwFresh', 0),
            'fii_history_len': len(ctx.get('fiiHistory', [])),
        }
    for ins in all_insights:
        w = (ins.get('strength', 1)) / 5.0
        imp = ins.get('impact', 'neutral')
        # TASK 5.3 — log every insight contribution (taken or neutral)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'insight_contributions', {
                'label': ins.get('label', ''),
                'type': ins.get('type', ''),
                'impact': imp,
                'strength': ins.get('strength', 1),
                'weight': w,
                'applied_to': 'bull' if imp == 'bullish' else ('bear' if imp == 'bearish' else ('caution_count' if imp == 'caution' else 'none')),
            })
        if imp == 'bullish':
            bull += w
            # TASK 5.3 (Bullish Contribution)
            if ctx.get('_trace') is not None:
                _trace_append(ctx['_trace']['verdict'], 'bull_contributions', {
                    'src': 'market_insight', 'label': ins.get('label', ''),
                    'value': w, 'running': round(bull, 2)
                })
        elif imp == 'bearish':
            bear += w
            # TASK 5.3 (Bearish Contribution)
            if ctx.get('_trace') is not None:
                _trace_append(ctx['_trace']['verdict'], 'bear_contributions', {
                    'src': 'market_insight', 'label': ins.get('label', ''),
                    'value': w, 'running': round(bear, 2)
                })
        elif imp == 'caution': cautions += 1

    # Context signals (numeric, not insights)
    profile = ctx.get('bnfProfile') or {}
    breadth = ctx.get('bnfBreadth') or {}
    b_pct = breadth.get('pct', 50)
    if b_pct > 65:
        bull += 0.4
        # TASK 5.3 (C1)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'bull_contributions', {
                'src': 'breadth_high', 'value': 0.4,
                'inputs': {'b_pct': b_pct, 'threshold': 65},
                'reason': f'b_pct={b_pct} > 65',
            })
    elif b_pct < 35:
        bear += 0.4
        # TASK 5.3 (C2)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'bear_contributions', {
                'src': 'breadth_low', 'value': 0.4,
                'inputs': {'b_pct': b_pct, 'threshold': 35},
                'reason': f'b_pct={b_pct} < 35',
            })
    skew = profile.get('ivSkew', 0)
    if skew > 3:
        bear += 0.2
        # TASK 5.3 (C3)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'bear_contributions', {
                'src': 'iv_skew_call_heavy', 'value': 0.2,
                'inputs': {'skew': skew},
                'reason': f'skew={skew} > 3 (call-side pressure)',
            })
    elif skew < -2:
        bull += 0.2
        # TASK 5.3 (C4)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'bull_contributions', {
                'src': 'iv_skew_put_heavy', 'value': 0.2,
                'inputs': {'skew': skew},
                'reason': f'skew={skew} < -2 (put-side pressure)',
            })
    cwF = profile.get('cwFresh', 0)
    pwF = profile.get('pwFresh', 0)
    if cwF > 0.25:
        bear += 0.15
        # TASK 5.3 (C5)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'bear_contributions', {
                'src': 'cw_fresh', 'value': 0.15,
                'inputs': {'cwF': cwF},
                'reason': f'call wall fresh {cwF:.2f} > 0.25',
            })
    if pwF > 0.25:
        bull += 0.15
        # TASK 5.3 (C6)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'bull_contributions', {
                'src': 'pw_fresh', 'value': 0.15,
                'inputs': {'pwF': pwF},
                'reason': f'put wall fresh {pwF:.2f} > 0.25',
            })
    fii_hist = ctx.get('fiiHistory', [])
    fii_sum = 0
    for h in fii_hist[:5]:
        v = h.get('fiiCash', 0)
        try: fii_sum += float(v) if v is not None else 0
        except (ValueError, TypeError): pass
    if fii_sum < -3000:
        bear += 0.3
        # TASK 5.3 (C7)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'bear_contributions', {
                'src': 'fii_selling', 'value': 0.3,
                'inputs': {'fii_sum': fii_sum, 'threshold': -3000},
                'reason': f'FII 5d sum {fii_sum:.0f} < -3000',
            })
    elif fii_sum > 3000:
        bull += 0.3
        # TASK 5.3 (C8)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'fii_buying', {
                'src': 'fii_buying', 'value': 0.3,
                'inputs': {'fii_sum': fii_sum, 'threshold': 3000},
                'reason': f'FII 5d sum {fii_sum:.0f} > 3000',
            })

    # Direction
    if bull > bear + 0.4: direction = 'BULL'
    elif bear > bull + 0.4: direction = 'BEAR'
    else: direction = 'NEUTRAL'
    # TASK 5.3 — direction decision snapshot
    if ctx.get('_trace') is not None:
        ctx['_trace']['verdict']['direction_decision'] = {
            'bull': round(bull, 4),
            'bear': round(bear, 4),
            'gap': round(abs(bull - bear), 4),
            'threshold': 0.4,
            'result': direction,
        }
        ctx['_trace']['verdict']['caution_count'] = cautions

    # Confidence
    total = bull + bear + 0.001
    dominant = max(bull, bear)
    confidence = int(dominant / total * 80)  # base max 80
    # TASK 5.3 (F1)
    if ctx.get('_trace') is not None:
        _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
            'step': 'base', 'final': confidence,
            'formula': 'dominant/total*80',
            'inputs': {'dominant': round(dominant, 4), 'total': round(total, 4)},
        })
    if cautions >= 3:
        confidence -= 15
        # TASK 5.3 (F2)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'cautions_penalty', 'delta': -15,
                'inputs': {'cautions': cautions, 'threshold': 3},
                'running': confidence,
            })
    if bull > 0.5 and bear > 0.5:
        confidence -= 20  # conflicting
        # TASK 5.3 (F3)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'conflict_penalty', 'delta': -20,
                'inputs': {'bull': round(bull, 4), 'bear': round(bear, 4)},
                'running': confidence,
            })
    rtype = regime.get('type', 'unknown')
    if rtype in ('range', 'trend'):
        confidence += 10  # clear regime = higher confidence
        # TASK 5.3 (F4)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'regime_clarity_bonus', 'delta': 10,
                'inputs': {'rtype': rtype},
                'running': confidence,
            })
    if rtype == 'choppy':
        confidence -= 10
        # TASK 5.3 (F5)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'regime_choppy_penalty', 'delta': -10,
                'inputs': {'rtype': rtype},
                'running': confidence,
            })

    coherence = signal_coherence(polls, ctx)
    if coherence and coherence.get('impact') == 'caution':
        coh_strength = int(coherence.get('strength') or 0)
        coh_penalty = min(10, 2 * max(0, coh_strength))
        confidence -= coh_penalty
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'coherence_caution_penalty', 'delta': -coh_penalty,
                'inputs': {
                    'label': coherence.get('label', ''),
                    'impact': coherence.get('impact', ''),
                    'strength': coh_strength,
                },
                'running': confidence,
            })
    elif coherence and coherence.get('impact') in ('bullish', 'bearish'):
        # Directive §4: coherence can only subtract from confidence.
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'coherence_positive_noop', 'delta': 0,
                'inputs': {
                    'label': coherence.get('label', ''),
                    'impact': coherence.get('impact', ''),
                    'strength': int(coherence.get('strength') or 0),
                },
                'running': confidence,
            })

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
        # BR57: IB on dte<=1 depends on correct DTE from ctx.
        elif vix_z >= 0.5 and dte is not None and dte <= 1:
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
    # TASK 5.3 — strategy selection snapshot (captures outcome of 1730-1759 block)
    if ctx.get('_trace') is not None:
        ctx['_trace']['verdict']['strategy_selection'] = {
            'regime_type': rtype,
            'direction': direction,
            'vix': vix,
            'vix_z': round(vix_z, 3) if vix_z is not None else None,
            'dte': dte,
            'straddle_expanding': straddle_expanding,
            'straddle_chg': round(straddle_chg, 2) if straddle_chg else 0,
            'initial_action': action,
            'initial_strategy': strategy,
        }

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
            # TASK 5.3 (F6)
            if ctx.get('_trace') is not None:
                _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                    'step': 'calibration_penalty', 'delta': -20,
                    'inputs': {'total': n, 'rate': round(rate, 4), 'strategy': strategy},
                    'running': confidence,
                })
            conflicts.append(f"Your {strategy}: {cal.get('wins',0)}/{n} ({rate*100:.0f}%)")
        elif n >= 5 and rate > 0.7:
            confidence += 10
            # TASK 5.3 (F7)
            if ctx.get('_trace') is not None:
                _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                    'step': 'calibration_bonus', 'delta': 10,
                    'inputs': {'total': n, 'rate': round(rate, 4), 'strategy': strategy},
                    'running': confidence,
                })

    # b92: Candidate menu awareness — does recommended strategy have viable candidates?
    if strategy and candidates and cand_insights is not None:
        strat_cands = [c for c in (candidates or []) if c.get('type') == strategy]
        if not strat_cands:
            conflicts.append(f"No {strategy} candidates generated")
            confidence -= 10
            # TASK 5.3 (F8)
            if ctx.get('_trace') is not None:
                _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                    'step': 'missing_candidates_penalty', 'delta': -10,
                    'inputs': {'strategy': strategy},
                    'running': confidence,
                })
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
                # TASK 5.3 (F9)
                if ctx.get('_trace') is not None:
                    _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                        'step': 'all_cautioned_penalty', 'delta': -15,
                        'inputs': {'strategy': strategy, 'cands_count': len(strat_cands)},
                        'running': confidence,
                    })

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
            _conf_before = confidence
            confidence = max(confidence, 30)
            # TASK 5.3 (F10)
            if ctx.get('_trace') is not None and _conf_before != confidence:
                _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                    'step': 'fallback_recovery',
                    'from': _conf_before, 'to': confidence,
                    'reason': 'Fallback strategy found — restore floor 30',
                    'running': confidence,
                })

    # ═══ b92: FULL OMNISCIENCE CHECKS — use all 12 new data streams ═══

    # Trade mode conflict — brain recommends IC/IB but user is in SWING
    tm = ctx.get('tradeMode', 'swing')
    if strategy in ('IRON_CONDOR', 'IRON_BUTTERFLY') and tm == 'swing':
        conflicts.append(f"{strategy} is intraday only — switch to INTRADAY")
        confidence -= 20
        # TASK 5.3 (F11)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'trade_mode_conflict', 'delta': -20,
                'inputs': {'strategy': strategy, 'trade_mode': tm},
                'running': confidence,
            })

    # Overnight delta conflict — brain says BULL but overnight is BEAR
    od = ctx.get('overnightDelta')
    if od and od.get('summary'):
        if 'BEARISH' in od['summary'] and direction == 'BULL':
            conflicts.append("Overnight BEARISH vs brain BULL")
            confidence -= 10
            # TASK 5.3 (F12)
            if ctx.get('_trace') is not None:
                _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                    'step': 'overnight_bear_conflict', 'delta': -10,
                    'inputs': {'brain_direction': direction, 'overnight': od['summary']},
                    'running': confidence,
                })
        elif 'BULLISH' in od['summary'] and direction == 'BEAR':
            conflicts.append("Overnight BULLISH vs brain BEAR")
            confidence -= 10
            # TASK 5.3 (F13)
            if ctx.get('_trace') is not None:
                _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                    'step': 'overnight_bull_conflict', 'delta': -10,
                    'inputs': {'brain_direction': direction, 'overnight': od['summary']},
                    'running': confidence,
                })

    # Institutional regime — low credit confidence but selling premium
    ir = ctx.get('institutionalRegime')
    if ir and ir.get('creditConfidence') == 'LOW' and action == 'SELL PREMIUM':
        conflicts.append("Low institutional credit confidence")
        confidence -= 10
        # TASK 5.3 (F14/F15)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'institutional_low_credit', 'delta': -10,
                'inputs': {'ir_credit': 'LOW', 'action': action},
                'running': confidence,
            })

    # Bias drift — morning thesis no longer holds
    drift = ctx.get('biasDrift', 0)
    if abs(drift) >= 2:
        conflicts.append(f"Bias drifted {drift:+d} from morning")
        confidence -= 10
        # TASK 5.3 (F15)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'bias_drift_divergence', 'delta': -10,
                'inputs': {'drift': drift},
                'running': confidence,
            })

    # Scan freshness — stale data
    scan_age = ctx.get('scanAgeMin')
    if scan_age and scan_age >= 30:
        conflicts.append(f"Data is {scan_age}min stale — rescan first")
        confidence -= 15
        # TASK 5.3 (F16)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'stale_data_penalty', 'delta': -15,
                'inputs': {'scan_age': scan_age, 'threshold': 30},
                'running': confidence,
            })

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
        # TASK 5.3 (F17)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'global_conflict_penalty', 'delta': -15,
                'inputs': {'gd_conflicts': gd_conflicts, 'threshold': 2},
                'running': confidence,
            })

    # Brain flip-flop detection — prior verdict contradicts current
    prior = ctx.get('priorVerdict')
    if prior and prior.get('direction') and prior['direction'] != direction:
        if prior.get('confidence', 0) >= 40:
            conflicts.append(f"Flip: was {prior['direction']} {prior.get('confidence',0)}% → now {direction}")
            confidence -= 10
            # TASK 5.3 (F18)
            if ctx.get('_trace') is not None:
                _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                    'step': 'prior_flip_penalty', 'delta': -10,
                    'inputs': {'prior_direction': prior['direction'], 'current': direction},
                    'running': confidence,
                })

    # Varsity alignment — brain strategy vs Varsity PRIMARY
    vf = ctx.get('varsityFilter')
    if vf and strategy:
        if strategy in (vf.get('primary') or []):
            confidence += 5  # brain agrees with Varsity
            # TASK 5.3 (F19)
            if ctx.get('_trace') is not None:
                _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                    'step': 'varsity_agree_bonus', 'delta': 5,
                    'inputs': {'strategy': strategy, 'varsity_primary': vf.get('primary')},
                    'running': confidence,
                })
        elif strategy not in (vf.get('allowed') or []) and strategy not in (vf.get('primary') or []):
            conflicts.append(f"Brain picks {strategy} but Varsity doesn't allow it")
            confidence -= 10
            # TASK 5.3 (F20)
            if ctx.get('_trace') is not None:
                _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                    'step': 'varsity_mismatch_penalty', 'delta': -10,
                    'inputs': {'strategy': strategy, 'varsity_allowed': vf.get('allowed')},
                    'running': confidence,
                })

    # Urgency
    mins = get_time_mins(polls[-1].get('t', '')) - 555 if polls else 0
    if 135 <= mins <= 315 and confidence >= 35: urgency = 'ENTER NOW'
    elif 135 <= mins <= 315: urgency = 'READY'
    elif mins < 15: urgency = 'WAIT — opening noise'
    elif mins > 345: urgency = 'WINDOW CLOSED'
    else: urgency = 'READY'

    # Daily P&L gate
    if ctx.get('dailyPnl', 0) < -2000:
        action, strategy, urgency = 'STOP', None, 'DONE FOR TODAY'
        _conf_before_pnl = confidence
        confidence = 0
        # TASK 5.3 (F21)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'daily_pnl_stop_zero', 'from': _conf_before_pnl, 'to': 0,
                'reason': f"Daily P&L {ctx.get('dailyPnl')} < -2000",
                'running': 0,
            })
    elif ctx.get('dailyTradeCount', 0) >= 3:
        confidence -= 15
        # TASK 5.3 (F22)
        if ctx.get('_trace') is not None:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'multi_trade_penalty', 'delta': -15,
                'inputs': {'count': ctx.get('dailyTradeCount')},
                'running': confidence,
            })
        if confidence < 50: urgency = 'CAUTION — 3+ trades today'

    _conf_pre_clamp = confidence
    confidence = max(0, min(100, confidence))
    # TASK 5.3 (F23)
    if ctx.get('_trace') is not None and _conf_pre_clamp != confidence:
        _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
            'step': 'final_clamp', 'from': _conf_pre_clamp, 'to': confidence,
            'running': confidence,
        })

    if action == 'WAIT' or action == 'STOP':
        _conf_pre_zero = confidence
        confidence = 0
        # TASK 5.3 (F24)
        if ctx.get('_trace') is not None and _conf_pre_zero != 0:
            _trace_append(ctx['_trace']['verdict'], 'confidence_adjustments', {
                'step': 'wait_stop_zero', 'from': _conf_pre_zero, 'to': 0,
                'reason': f'Action is {action}',
                'running': 0,
            })

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
    idx_key = trade.get('index_key', 'BNF')
    dte_key = 'bnfDTE' if idx_key == 'BNF' else 'nfDTE'
    dte = ctx.get(dte_key)
    if dte is None:
        print(f"position_verdict: {dte_key} missing for trade {trade.get('id','?')} — default 5")
        dte = 5
    """ONE action per trade: BOOK / HOLD / EXIT + urgency + reason."""

    # TASK 5.4 — init per-position trace slot
    _trace_root = ctx.get('_trace')
    _pos_trace = None
    if _trace_root is not None:
        _tid = str(trade.get('id', 'unknown'))
        _pos_trace = {
            'inputs': {
                'trade_id': _tid,
                'pnl': trade.get('current_pnl', 0),
                'max_profit': trade.get('max_profit'),
                'max_loss': trade.get('max_loss'),
                'strategy_type': trade.get('strategy_type', ''),
                'is_credit': trade.get('is_credit'),
                'ci': trade.get('controlIndex'),
                'wall_sev': (trade.get('wallDrift') or {}).get('severity', 0),
                'vix_chg': trade.get('vixChange', 0),
                'peak_erosion': trade.get('peakErosion', 0),
                'peak_pnl': trade.get('peak_pnl', 0),
                'dte': dte,
            },
            'danger_components': [],
            'danger_final': 0,
            'structural_exits': {},
            'decision_path': [],
            'final': {},
        }
        _trace_root['positions'][_tid] = _pos_trace
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
    # TASK 5.4 (initialization)
    if ctx.get('_trace') is not None and _pos_trace is not None:
        _pos_trace['danger_initial'] = 0
    reasons = []

    # Wall drift
    _wall_matched = False
    if wall_sev >= 2:
        danger += 40
        # TASK 5.4 (D1)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _trace_append(_pos_trace, 'danger_components', {
                'check': 'wall_exposed', 'matched': True, 'condition': 'wall_sev >= 2',
                'delta': 40, 'running_danger': danger,
                'inputs': {'wall_sev': wall_sev, 'warning': wall.get('warning', '')[:50]},
            })
        reasons.append(f"Wall EXPOSED ({wall.get('warning', '')[:50]})")
        _wall_matched = True
    elif wall_sev == 1:
        danger += 15
        # TASK 5.4 (D2)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _trace_append(_pos_trace, 'danger_components', {
                'check': 'wall_weakened', 'matched': True, 'condition': 'wall_sev == 1',
                'delta': 15, 'running_danger': danger,
                'inputs': {'wall_sev': wall_sev},
            })
        reasons.append("Wall weakened")
        _wall_matched = True
    # TASK 5.4 (Wall Skip)
    if ctx.get('_trace') is not None and _pos_trace is not None and not _wall_matched:
        _trace_append(_pos_trace, 'danger_components', {
            'check': 'wall', 'matched': False, 'skip_reason': 'wall_sev==0 (wall stable)',
            'running_danger': danger, 'inputs': {'wall_sev': wall_sev},
        })

    # VIX spike (worst for credit sellers)
    _vix_matched = False
    if is_credit and vix_chg >= 2.0:
        danger += 35
        # TASK 5.4 (D3)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _trace_append(_pos_trace, 'danger_components', {
                'check': 'vix_spike_credit', 'matched': True, 'condition': 'is_credit and vix_chg >= 2.0',
                'delta': 35, 'running_danger': danger,
                'inputs': {'is_credit': is_credit, 'vix_chg': vix_chg, 'threshold': 2.0},
            })
        reasons.append(f"VIX spiked +{vix_chg:.1f} — premiums expanding")
        _vix_matched = True
    elif is_credit and vix_chg >= 1.0:
        danger += 15
        # TASK 5.4 (D4)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _trace_append(_pos_trace, 'danger_components', {
                'check': 'vix_rising_credit', 'matched': True, 'condition': 'is_credit and vix_chg >= 1.0',
                'delta': 15, 'running_danger': danger,
                'inputs': {'is_credit': is_credit, 'vix_chg': vix_chg, 'threshold': 1.0},
            })
        reasons.append(f"VIX rising +{vix_chg:.1f}")
        _vix_matched = True
    # TASK 5.4 (VIX Skip)
    if ctx.get('_trace') is not None and _pos_trace is not None and not _vix_matched:
        _trace_append(_pos_trace, 'danger_components', {
            'check': 'vix', 'matched': False, 'skip_reason': 'not credit or vix_chg below threshold',
            'running_danger': danger, 'inputs': {'is_credit': is_credit, 'vix_chg': vix_chg},
        })

    # Peak erosion — SCALED (b104 fix: 864% got same score as 51%)
    # Debit trades: premium decay from theta is EXPECTED, halve the impact
    erosion_mult = 0.5 if not is_credit else 1.0
    _erosion_matched = False
    if peak_pnl >= 500 and peak_erosion > 0:  # Gemini fix: ignore tiny peaks (<₹500)
        if peak_erosion > 500:
            _d = int(40 * erosion_mult)
            danger += _d
            # TASK 5.4 (D5)
            if ctx.get('_trace') is not None and _pos_trace is not None:
                _trace_append(_pos_trace, 'danger_components', {
                    'check': 'peak_erosion_extreme', 'matched': True, 'condition': 'peak_erosion > 500',
                    'delta': _d, 'running_danger': danger,
                    'inputs': {'peak_erosion': peak_erosion, 'peak_pnl': peak_pnl, 'mult': erosion_mult},
                })
            reasons.append(f"Peak erosion {peak_erosion:.0f}% (was ₹{peak_pnl:.0f})")
            _erosion_matched = True
        elif peak_erosion > 200:
            _d = int(30 * erosion_mult)
            danger += _d
            # TASK 5.4 (D6)
            if ctx.get('_trace') is not None and _pos_trace is not None:
                _trace_append(_pos_trace, 'danger_components', {
                    'check': 'peak_erosion_high', 'matched': True, 'condition': 'peak_erosion > 200',
                    'delta': _d, 'running_danger': danger,
                    'inputs': {'peak_erosion': peak_erosion, 'peak_pnl': peak_pnl, 'mult': erosion_mult},
                })
            reasons.append(f"Peak erosion {peak_erosion:.0f}% (was ₹{peak_pnl:.0f})")
            _erosion_matched = True
        elif peak_erosion > 50:
            _d = int(20 * erosion_mult)
            danger += _d
            # TASK 5.4 (D7)
            if ctx.get('_trace') is not None and _pos_trace is not None:
                _trace_append(_pos_trace, 'danger_components', {
                    'check': 'peak_erosion_medium', 'matched': True, 'condition': 'peak_erosion > 50',
                    'delta': _d, 'running_danger': danger,
                    'inputs': {'peak_erosion': peak_erosion, 'peak_pnl': peak_pnl, 'mult': erosion_mult},
                })
            reasons.append(f"Peak erosion {peak_erosion:.0f}% (was ₹{peak_pnl:.0f})")
            _erosion_matched = True
        elif peak_erosion > 30:
            _d = int(10 * erosion_mult)
            danger += _d
            # TASK 5.4 (D8)
            if ctx.get('_trace') is not None and _pos_trace is not None:
                _trace_append(_pos_trace, 'danger_components', {
                    'check': 'peak_erosion_fade', 'matched': True, 'condition': 'peak_erosion > 30',
                    'delta': _d, 'running_danger': danger,
                    'inputs': {'peak_erosion': peak_erosion, 'peak_pnl': peak_pnl, 'mult': erosion_mult},
                })
            reasons.append(f"Profit fading ({peak_erosion:.0f}% from peak)")
            _erosion_matched = True

    # TASK 5.4 (Erosion Skip)
    if ctx.get('_trace') is not None and _pos_trace is not None and not _erosion_matched:
        _trace_append(_pos_trace, 'danger_components', {
            'check': 'peak_erosion', 'matched': False,
            'skip_reason': f'peak_pnl={peak_pnl} < 500 or erosion={peak_erosion} <= 30',
            'running_danger': danger, 'inputs': {'peak_pnl': peak_pnl, 'peak_erosion': peak_erosion},
        })

    # Profit-to-loss flip — was making money, now losing (b104 fix)
    _flip_matched = False
    if peak_pnl > 0 and pnl < 0:
        danger += 15
        # TASK 5.4 (D9)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _trace_append(_pos_trace, 'danger_components', {
                'check': 'profit_to_loss_flip', 'matched': True, 'condition': 'peak_pnl > 0 and pnl < 0',
                'delta': 15, 'running_danger': danger,
                'inputs': {'peak_pnl': peak_pnl, 'pnl': pnl},
            })
        reasons.append(f"Flipped from +₹{peak_pnl:.0f} to -₹{abs(pnl):.0f}")
        _flip_matched = True
    # TASK 5.4 (Flip Skip)
    if ctx.get('_trace') is not None and _pos_trace is not None and not _flip_matched:
        _trace_append(_pos_trace, 'danger_components', {
            'check': 'profit_to_loss_flip', 'matched': False, 'skip_reason': 'peak_pnl<=0 or pnl>=0',
            'running_danger': danger, 'inputs': {'peak_pnl': peak_pnl, 'pnl': pnl},
        })

    # CI — RELAXED thresholds (b104 fix: CI -5 got ZERO before)
    # Gemini fix: IB not totally exempt — check for extreme degradation beyond baseline
    _ci_matched = False
    if ci is not None:
        if is_ib:
            if ci < -75:
                danger += 20
                # TASK 5.4 (D10)
                if ctx.get('_trace') is not None and _pos_trace is not None:
                    _trace_append(_pos_trace, 'danger_components', {
                        'check': 'ib_ci_collapse', 'matched': True, 'condition': 'is_ib and ci < -75',
                        'delta': 20, 'running_danger': danger,
                        'inputs': {'ci': ci, 'threshold': -75, 'is_ib': True},
                    })
                reasons.append(f"IB CI collapsed to {ci} (beyond normal ATM range)")
                _ci_matched = True
        else:
            if ci < -40:
                danger += 25
                # TASK 5.4 (D11)
                if ctx.get('_trace') is not None and _pos_trace is not None:
                    _trace_append(_pos_trace, 'danger_components', {
                        'check': 'ci_opponent_control', 'matched': True, 'condition': 'not is_ib and ci < -40',
                        'delta': 25, 'running_danger': danger,
                        'inputs': {'ci': ci},
                    })
                reasons.append(f"Opponent in control (CI {ci})")
                _ci_matched = True
            elif ci < -20:
                danger += 15
                # TASK 5.4 (D12)
                if ctx.get('_trace') is not None and _pos_trace is not None:
                    _trace_append(_pos_trace, 'danger_components', {
                        'check': 'ci_opponent_gaining', 'matched': True, 'condition': 'ci < -20',
                        'delta': 15, 'running_danger': danger,
                        'inputs': {'ci': ci},
                    })
                reasons.append(f"Opponent gaining (CI {ci})")
                _ci_matched = True
            elif ci < 0:
                danger += 5
                # TASK 5.4 (D13)
                if ctx.get('_trace') is not None and _pos_trace is not None:
                    _trace_append(_pos_trace, 'danger_components', {
                        'check': 'ci_opponent_slight', 'matched': True, 'condition': 'ci < 0',
                        'delta': 5, 'running_danger': danger,
                        'inputs': {'ci': ci},
                    })
                _ci_matched = True
    # TASK 5.4 (CI Skip)
    if ctx.get('_trace') is not None and _pos_trace is not None and not _ci_matched:
        _trace_append(_pos_trace, 'danger_components', {
            'check': 'ci', 'matched': False, 'skip_reason': 'ci is None' if ci is None else 'ci positive',
            'running_danger': danger, 'inputs': {'ci': ci},
        })

    # b115: Breakeven cushion — the REAL danger line, not sell strike
    # be_upper = upper breakeven (IC/IB/Bear Call), be_lower = lower breakeven (IC/IB/Bull Put)
    be_upper = trade.get('be_upper') or trade.get('beUpper')
    be_lower = trade.get('be_lower') or trade.get('beLower')
    sell_strike = trade.get('sell_strike', 0)
    spot = trade.get('current_spot', 0)
    _be_matched = False
    if spot and (be_upper or be_lower):
        _be_eligible = True
        vix = ctx.get('vix', 20)
        # BR73: Annualized to daily volatility denominator fix
        daily_sigma = spot * (vix / 100) / math.sqrt(252) if spot > 0 else 300
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
                # TASK 5.4 (D14)
                if ctx.get('_trace') is not None and _pos_trace is not None:
                    _trace_append(_pos_trace, 'danger_components', {
                        'check': 'be_breached', 'matched': True, 'condition': 'near_cushion <= 0',
                        'delta': 40, 'running_danger': danger,
                        'inputs': {'near_cushion': near_cushion, 'near_label': near_label},
                    })
                reasons.append(f"BREACHED — spot past {near_label}")
                _be_matched = True
            elif cushion_sigma < 0.15:
                danger += 30
                # TASK 5.4 (D15)
                if ctx.get('_trace') is not None and _pos_trace is not None:
                    _trace_append(_pos_trace, 'danger_components', {
                        'check': 'be_cushion_critical', 'matched': True, 'condition': 'cushion_sigma < 0.15',
                        'delta': 30, 'running_danger': danger,
                        'inputs': {'near_cushion': near_cushion, 'cushion_sigma': cushion_sigma},
                    })
                reasons.append(f"Only {near_cushion:.0f}pts to {near_label} ({cushion_sigma:.2f}σ)")
                _be_matched = True
            elif cushion_sigma < 0.30:
                danger += 20
                # TASK 5.4 (D16)
                if ctx.get('_trace') is not None and _pos_trace is not None:
                    _trace_append(_pos_trace, 'danger_components', {
                        'check': 'be_cushion_thin', 'matched': True, 'condition': 'cushion_sigma < 0.30',
                        'delta': 20, 'running_danger': danger,
                        'inputs': {'near_cushion': near_cushion, 'cushion_sigma': cushion_sigma},
                    })
                reasons.append(f"Thin BE cushion {near_cushion:.0f}pts to {near_label}")
                _be_matched = True
            elif cushion_sigma < 0.50:
                danger += 10
                # TASK 5.4 (D17)
                if ctx.get('_trace') is not None and _pos_trace is not None:
                    _trace_append(_pos_trace, 'danger_components', {
                        'check': 'be_cushion_approaching', 'matched': True, 'condition': 'cushion_sigma < 0.50',
                        'delta': 10, 'running_danger': danger,
                        'inputs': {'near_cushion': near_cushion, 'cushion_sigma': cushion_sigma},
                    })
                reasons.append(f"Approaching {near_label} ({near_cushion:.0f}pts)")
                _be_matched = True
        # b115: Early BOOK — profitable + near breakeven = take money now
        if pnl > 0 and near_cushion > 0 and daily_sigma > 0 and near_cushion / daily_sigma < 0.20:
            # TASK 5.4 (decision_path)
            if ctx.get('_trace') is not None and _pos_trace is not None:
                _pos_trace['decision_path'].append('early_book_cushion: pnl>0 and cushion_sigma < 0.20')
            _final_verdict = {"action": "BOOK", "urgency": "NOW",
                    "reason": f"₹{pnl:.0f} profit but only {near_cushion:.0f}pts to {near_label}. Premium at risk — lock in now."}
            # TASK 5.4 (Final Snapshot)
            if ctx.get('_trace') is not None and _pos_trace is not None:
                _pos_trace['danger_final'] = danger
                _pos_trace['final'] = dict(_final_verdict)
                _pos_trace['structural_exits']['be_cushion_early_book'] = True
            return _final_verdict
    elif sell_strike and spot and is_credit and not is_ib:
        # Fallback: use sell_strike if no breakeven stored (old trades)
        # TASK 5.4 — R3-4 Skip Label (D14-D17 group was eligible based on inputs but be_upper/lower missing)
        # be_cushion_absent_fallback_fired
        cushion = abs(sell_strike - spot)
        vix = ctx.get('vix', 20)
        # BR73: Annualized to daily volatility denominator fix
        daily_sigma = spot * (vix / 100) / math.sqrt(252) if spot > 0 else 300
        if daily_sigma > 0:
            cushion_sigma = cushion / daily_sigma
            if cushion_sigma < 0.25:
                danger += 25
                # TASK 5.4 (D18)
                if ctx.get('_trace') is not None and _pos_trace is not None:
                    _trace_append(_pos_trace, 'danger_components', {
                        'check': 'sell_strike_close', 'matched': True, 'condition': 'cushion_sigma < 0.25',
                        'delta': 25, 'running_danger': danger,
                        'inputs': {'cushion': cushion, 'cushion_sigma': cushion_sigma},
                    })
                reasons.append(f"Only {cushion:.0f}pts ({cushion_sigma:.2f}σ) from sell strike")
                _be_matched = True
            elif cushion_sigma < 0.5:
                danger += 15
                # TASK 5.4 (D19)
                if ctx.get('_trace') is not None and _pos_trace is not None:
                    _trace_append(_pos_trace, 'danger_components', {
                        'check': 'sell_strike_thin', 'matched': True, 'condition': 'cushion_sigma < 0.5',
                        'delta': 15, 'running_danger': danger,
                        'inputs': {'cushion': cushion, 'cushion_sigma': cushion_sigma},
                    })
                reasons.append(f"Thin cushion ({cushion_sigma:.2f}σ from sell)")
                _be_matched = True
    # TASK 5.4 — BE Cushion Skip logic (highly informative for regression)
    if ctx.get('_trace') is not None and _pos_trace is not None and not _be_matched:
        _skip_reason = 'be_upper and be_lower are None — BR115 class silent failure'
        if not spot: _skip_reason = 'spot missing'
        elif (be_upper or be_lower) and _be_eligible:
             _skip_reason = 'cushion outside danger thresholds'
             # be_cushion_absent_fallback_eligible_not_matched
        # Check against R3-4 failure conditions
        if not be_upper and not be_lower and (is_credit or is_4leg):
             # be_cushion_silent_failure
             pass # label anchor
        _trace_append(_pos_trace, 'danger_components', {
            'check': 'be_cushion', 'matched': False, 'skip_reason': _skip_reason,
            'running_danger': danger, 'inputs': {'be_upper': be_upper, 'be_lower': be_lower, 'spot': spot},
        })

    # Momentum threat
    _mom_matched = False
    if momentum_threat:
        danger += 30
        # TASK 5.4 (D20)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _trace_append(_pos_trace, 'danger_components', {
                'check': 'momentum_threat', 'matched': True, 'condition': 'momentum_threat',
                'delta': 30, 'running_danger': danger,
                'inputs': {'insight_labels': [i.get('label', '') for i in insights if 'sell strike' in i.get('label', '').lower()]},
            })
        reasons.append("Spot approaching sell strike")
        _mom_matched = True
    # TASK 5.4 (Momentum Skip)
    if ctx.get('_trace') is not None and _pos_trace is not None and not _mom_matched:
        _trace_append(_pos_trace, 'danger_components', {
            'check': 'momentum_threat', 'matched': False, 'skip_reason': 'no sell-strike-approach insight',
            'running_danger': danger, 'inputs': {},
        })

    # Phase mismatch (credit in trending phase = danger)
    _phase_matched = False
    if is_credit and str(phase).startswith('TREND'):
        danger += 10
        # TASK 5.4 (D21)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _trace_append(_pos_trace, 'danger_components', {
                'check': 'phase_mismatch_credit', 'matched': True, 'condition': "is_credit and phase == 'TRENDING'",
                'delta': 10, 'running_danger': danger,
                'inputs': {'is_credit': is_credit, 'phase': phase},
            })
        reasons.append("Trending market — credit at risk")
        _phase_matched = True
    # TASK 5.4 (Phase Skip)
    if ctx.get('_trace') is not None and _pos_trace is not None and not _phase_matched:
        _trace_append(_pos_trace, 'danger_components', {
            'check': 'phase_mismatch', 'matched': False,
            'skip_reason': f'phase={phase} or not credit',
            'running_danger': danger, 'inputs': {'is_credit': is_credit, 'phase': phase},
        })

    # b96: "Past the wall" insight — position_wall_proximity detected exposure
    _past_wall_matched = False
    past_wall = any('Past the wall' in i.get('label', '') for i in insights)
    if past_wall:
        danger += 35
        # TASK 5.4 (D22)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _trace_append(_pos_trace, 'danger_components', {
                'check': 'past_wall', 'matched': True, 'condition': 'past_wall insight',
                'delta': 35, 'running_danger': danger,
                'inputs': {'has_past_wall_insight': True},
            })
        reasons.append("Sell past OI wall — no protection")
        _past_wall_matched = True
    # TASK 5.4 (Past Wall Skip)
    if ctx.get('_trace') is not None and _pos_trace is not None and not _past_wall_matched:
        _trace_append(_pos_trace, 'danger_components', {
            'check': 'past_wall', 'matched': False, 'skip_reason': 'no past-wall insight',
            'running_danger': danger, 'inputs': {},
        })

    # b96: Loss magnitude — deep loss even with stable danger should escalate
    _loss_matched = False
    if pnl < 0:
        if loss_pct > 0.5:
            danger += 30
            # TASK 5.4 (D23)
            if ctx.get('_trace') is not None and _pos_trace is not None:
                _trace_append(_pos_trace, 'danger_components', {
                    'check': 'loss_deep', 'matched': True, 'condition': 'loss_pct > 0.5',
                    'delta': 30, 'running_danger': danger,
                    'inputs': {'loss_pct': loss_pct, 'pnl': pnl, 'max_loss': max_l},
                })
            reasons.append(f"Deep loss ({loss_pct*100:.0f}% of max)")
            _loss_matched = True
        elif loss_pct > 0.3:
            danger += 15
            # TASK 5.4 (D24)
            if ctx.get('_trace') is not None and _pos_trace is not None:
                _trace_append(_pos_trace, 'danger_components', {
                    'check': 'loss_significant', 'matched': True, 'condition': 'loss_pct > 0.3',
                    'delta': 15, 'running_danger': danger,
                    'inputs': {'loss_pct': loss_pct, 'pnl': pnl, 'max_loss': max_l},
                })
            reasons.append(f"Significant loss ({loss_pct*100:.0f}% of max)")
            _loss_matched = True
    # TASK 5.4 (Loss Skip)
    if ctx.get('_trace') is not None and _pos_trace is not None and not _loss_matched:
        _trace_append(_pos_trace, 'danger_components', {
            'check': 'loss_magnitude', 'matched': False, 'skip_reason': 'pnl>=0 (not in loss)',
            'running_danger': danger, 'inputs': {'pnl': pnl},
        })

    # ═══ EXIT — compound danger high ═══
    if danger >= 60:
        urgency = 'NOW' if danger >= 80 else 'SOON'
        # TASK 5.4 (decision_path)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['decision_path'].append(f'danger>={60}: urgency={urgency}')
        _final_verdict = {"action": "EXIT", "urgency": urgency,
                "reason": f"Danger {danger}/100. {'. '.join(reasons[:3])}"}
        # TASK 5.4 (Final Snapshot)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['danger_final'] = danger
            _pos_trace['final'] = dict(_final_verdict)
            _pos_trace['structural_exits']['danger_threshold_hit'] = True
        return _final_verdict

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
        # TASK 5.4 (decision_path)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['decision_path'].append('thesis_broken and pnl<0')
        _final_verdict = {"action": "EXIT", "urgency": "SOON",
                "reason": f"Thesis broken. Entered {entry_bias}, market now {current_bias}. Credit spread fighting the trend."}
        # TASK 5.4 (Final Snapshot)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['danger_final'] = danger
            _pos_trace['final'] = dict(_final_verdict)
            _pos_trace['structural_exits']['thesis_broken'] = True
        return _final_verdict
    if thesis_broken and pnl >= 0:
        # TASK 5.4 (decision_path)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['decision_path'].append('thesis_broken and pnl>=0')
        _final_verdict = {"action": "BOOK", "urgency": "NOW",
                "reason": f"Thesis broken — market flipped {entry_bias}→{current_bias}. Lock in ₹{pnl:.0f} before it reverses."}
        # TASK 5.4 (Final Snapshot)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['danger_final'] = danger
            _pos_trace['final'] = dict(_final_verdict)
            _pos_trace['structural_exits']['thesis_broken'] = True
        return _final_verdict

    # ═══ EXIT — structural threats (independent of danger score) ═══
    if is_4leg and dte <= 1:
        if pnl > 0:
            # TASK 5.4 (decision_path)
            if ctx.get('_trace') is not None and _pos_trace is not None:
                _pos_trace['decision_path'].append('4leg_expiry_win')
            _final_verdict = {"action": "BOOK", "urgency": "NOW", "reason": "4-leg + expiry day. 0% overnight survival."}
        else:
            # TASK 5.4 (decision_path)
            if ctx.get('_trace') is not None and _pos_trace is not None:
                _pos_trace['decision_path'].append('4leg_expiry_loss')
            _final_verdict = {"action": "EXIT", "urgency": "NOW", "reason": "4-leg + expiry. Cut loss, don't hold overnight."}

        # TASK 5.4 (Final Snapshot)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['danger_final'] = danger
            _pos_trace['final'] = dict(_final_verdict)
            _pos_trace['structural_exits']['four_leg_expiry'] = True
        return _final_verdict

    # ═══ BOOK — profitable + reasons to take money ═══
    if pnl_pct >= 0.5:
        book_reasons = []
        if danger >= 30: book_reasons.append(f"rising danger ({danger})")
        if peak_erosion > 20: book_reasons.append(f"peak fading {peak_erosion:.0f}%")
        if regime.get('type') == 'range': book_reasons.append("range — theta captured")
        if vix_chg < -1.0 and is_credit: book_reasons.append(f"VIX crushed {vix_chg:.1f} — lock gains")
        urgency = 'NOW' if pnl_pct >= 0.7 or danger >= 30 else 'SOON'
        # TASK 5.4 (decision_path)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['decision_path'].append(f'book_50pct: urgency={urgency}')
        reason = f"{pnl_pct*100:.0f}% of max."
        if book_reasons: reason += f" {'. '.join(book_reasons[:2])}"
        _final_verdict = {"action": "BOOK", "urgency": urgency, "reason": reason}
        # TASK 5.4 (Final Snapshot)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['danger_final'] = danger
            _pos_trace['final'] = dict(_final_verdict)
        return _final_verdict

    if pnl_pct >= 0.3 and (against_trend or danger >= 25):
        # TASK 5.4 (decision_path)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['decision_path'].append('book_30pct_risk')
        _final_verdict = {"action": "BOOK", "urgency": "SOON",
                "reason": f"{pnl_pct*100:.0f}% profit + {'risk building' if danger >= 25 else 'against trend'}. Don't give it back."}
        # TASK 5.4 (Final Snapshot)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['danger_final'] = danger
            _pos_trace['final'] = dict(_final_verdict)
        return _final_verdict

    # ═══ HOLD — positive conditions ═══
    if pnl > 0 and danger < 20:
        hold_reasons = []
        if ci and ci > 20: hold_reasons.append(f"CI {ci}")
        if has_wall and wall_sev == 0: hold_reasons.append("wall protecting")
        if regime.get('type') == 'range': hold_reasons.append("range — theta working")
        if vix_chg < 0 and is_credit: hold_reasons.append("VIX falling — good for credit")
        # TASK 5.4 (decision_path)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['decision_path'].append('hold_win_low_risk')
        reason = f"P&L ₹{pnl:.0f} ({pnl_pct*100:.0f}%)."
        if hold_reasons: reason += f" {'. '.join(hold_reasons[:2])}"
        _final_verdict = {"action": "HOLD", "urgency": "WATCH", "reason": reason}
        # TASK 5.4 (Final Snapshot)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['danger_final'] = danger
            _pos_trace['final'] = dict(_final_verdict)
        return _final_verdict

    # ═══ DEFAULT ═══
    if pnl >= 0:
        # TASK 5.4 (decision_path)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['decision_path'].append('default_hold_profit')
        _final_verdict = {"action": "HOLD", "urgency": "WATCH", "reason": f"P&L ₹{pnl:.0f}. Danger {danger}/100."}
    else:
        # TASK 5.4 (decision_path)
        if ctx.get('_trace') is not None and _pos_trace is not None:
            _pos_trace['decision_path'].append('default_hold_loss')
        _final_verdict = {"action": "HOLD", "urgency": "MONITOR",
                "reason": f"Loss ₹{pnl:.0f} ({loss_pct*100:.0f}%). Danger {danger}/100. {'. '.join(reasons[:2]) if reasons else 'Watch CI.'}"}

    # TASK 5.4 (Final Snapshot)
    if ctx.get('_trace') is not None and _pos_trace is not None:
        _pos_trace['danger_final'] = danger
        _pos_trace['final'] = dict(_final_verdict)
    return _final_verdict

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

def _bs_theta(spot, strike, T, vol, opt_type):
    """Black-Scholes theta (per-day). Simplified for near-money options."""
    if T <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return 0
    r = 0.065
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * T) / (vol * math.sqrt(T))
    # Standard BS theta term for non-dividend stock (per year)
    theta_ann = -(spot * vol * math.exp(-d1*d1/2) / (2 * math.sqrt(2 * math.pi * T)))
    return theta_ann / 365

def _daily_sigma(spot, vix):
    """Daily 1σ move from VIX"""
    if spot <= 0 or vix <= 0: return 300
    return spot * (vix / 100) / math.sqrt(252)

def _realized_vol_proxy(vix):
    """Bootstrap realized-vol proxy from VIX until a trailing realized series exists."""
    try:
        value = float(vix)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return (value * 0.85) / 100.0

def _sigma_days(spot, vix, dte):
    """Multi-day σ move"""
    return _daily_sigma(spot, vix) * math.sqrt(max(1, dte))

# ─── CONSTANTS ───

_CONST = {
    'BNF_LOT': 30, 'NF_LOT': 65,
    'BNF_SHORT_MARGIN': 75000,
    'NF_SHORT_MARGIN': 50000,
    'CAPITAL': 250000, 'MAX_RISK_PCT': 10,
    'BNF_WIDTHS': [200, 300, 400, 500, 600, 800, 1000],
    'NF_WIDTHS': [100, 150, 200, 250, 300, 400],
    'IV_HIGH': 20, 'IV_VERY_HIGH': 24, 'IV_LOW': 15,
    'MIN_PROB': 0.50, 'MIN_CREDIT_RATIO': 0.10,
    'IV_RICH_MIN': 1.15, 'MIN_CREDIT_DTE': 1,
    'MIN_SIGMA_OTM': 0.5, 'MAX_SIGMA_OTM': 1.15,
    'IC_WALL_MAX_SIGMA': 1.5,
    'MIN_WIDTH_BNF': 400, 'MIN_WIDTH_NF': 150,
    'CREDIT_TYPES': ['BEAR_CALL', 'BULL_PUT', 'IRON_CONDOR', 'IRON_BUTTERFLY'],
    'DEBIT_TYPES': ['BEAR_PUT', 'BULL_CALL', 'DOUBLE_DEBIT'],
    'NEUTRAL_TYPES': ['IRON_CONDOR', 'IRON_BUTTERFLY', 'DOUBLE_DEBIT'],
    'DIR_BULL': ['BULL_CALL', 'BULL_PUT'],
    'DIR_BEAR': ['BEAR_CALL', 'BEAR_PUT'],
    'DOW_THRESHOLD': 0.5,
    'CRUDE_THRESHOLD': 1.5,
    'GIFT_THRESHOLD': 0.3,
    'NOISE_WINDOW': 15,
    'LAST_ENTRY_CUTOFF': 345,
    'ROUTINE_NOTIFY_MS': 1800000, # 30 min in ms
    'SIGMA_IMPORTANT_THRESHOLD': 2.0,
    'TARGET_NEAR_RATIO': 0.8,
    'STOP_LOSS_RATIO': 0.7,
    'SIGMA_ENTRY_THRESHOLD': 1.5,
    'SIGMA_EXIT_THRESHOLD': 1.0,
    'CANDLE_MARUBOZU_SHADOW_PCT': 0.05,
    'CANDLE_DOJI_BODY_PCT': 0.05,
    'CANDLE_SPINNING_MIN_BODY_PCT': 0.05,
    'CANDLE_SPINNING_MAX_BODY_PCT': 0.20,
    'CANDLE_SHADOW_RATIO_MIN': 0.5,
    'CANDLE_SHADOW_RATIO_MAX': 2.0,
    'CANDLE_HAMMER_SHADOW_MIN': 2.0,
    'CANDLE_HAMMER_UPPER_MAX_BODY': 0.5,
    'CANDLE_HAMMER_UPPER_MAX_RANGE': 0.10,
    'CANDLE_ENGULF_BODY_MIN': 1.5,
    'CANDLE_PRIOR_TREND_CANDLES': 5,
    'CANDLE_PRIOR_TREND_THRESHOLD': 0.3,
    'CANDLE_GAP_PCT': 0.001,
    # NSE F&O trading holidays for calendar year 2026.
    # Source: NSE circular NSE/FAOP/71777, dated 2025-12-12.
    'NSE_HOLIDAYS': [
        '2026-01-26', '2026-03-03', '2026-03-26', '2026-03-31',
        '2026-04-03', '2026-04-14', '2026-05-01', '2026-05-28',
        '2026-06-26', '2026-09-14', '2026-10-02', '2026-10-20',
        '2026-11-10', '2026-11-24', '2026-12-25',
    ],
}


# ═══════════════════════════════════════════════════════════════
# CHAIN_DEBUG FOUNDATION — v2.2.9
# Trace collector constants + utilities used by analyze(), synthesize_verdict,
# position_verdict, generate_candidates, _build_candidate, and replay().
# Guarded by ctx.get('_trace') — zero cost when debug disabled.
# ═══════════════════════════════════════════════════════════════

# TASK 5.1 — Version + schema markers
BRAIN_VERSION = "2.4.71"
TRACE_SCHEMA_VERSION = "1.1"
MAX_TRACE_ITEMS = 500  # Hard cap per trace array — prevents runaway memory
TRACE_ATTEMPT_SAMPLE_CAP = 12
REJECTED_CANDIDATE_SAMPLE_CAP = 20


# TASK 5.1 — Fresh trace skeleton for each analyze() call
def _new_trace(source='live'):
    """Return a fresh trace dict with full schema pre-populated.
    
    All sections present so downstream code can append without
    checking existence. candidates keyed by index (BNF/NF) because
    generate_candidates is called once per index.
    """
    return {
        'meta': {
            'brain_version': BRAIN_VERSION,
            'trace_schema_version': TRACE_SCHEMA_VERSION,
            'source': source,              # 'live' or 'replay'
            'ts_iso': None,                # populated in analyze() — Task 5.10
            'session_date': None,          # populated in analyze() — Task 5.10
            'inputs_hash': None,           # populated in analyze() — Task 5.9
            'brain_execution_time_ms': None,  # populated in analyze() — Task 5.11
            'truncated': False,            # set by _trace_append if MAX_TRACE_ITEMS exceeded
        },
        'verdict': {
            'inputs': {},
            'insight_contributions': [],   # one entry per all_insights loop iteration
            'bull_contributions': [],      # context signals that increased bull
            'bear_contributions': [],      # context signals that increased bear
            'caution_count': 0,
            'direction_decision': {},
            'confidence_adjustments': [],
            'strategy_selection': {},
            'conflicts': [],
            'final': {},
        },
        'positions': {},   # keyed by trade_id
        'candidates': {
            'BNF': None,   # filled by generate_candidates when BNF chain present
            'NF':  None,   # filled by generate_candidates when NF chain present
        },
        'ml_budget': None,  # R3-2: populated by Task 5.13 after ML enrichment loop
        'errors': [],       # insight function exceptions captured here
    }


# TASK 5.1 — Safe JSON serialization (handles nan, inf, non-finite floats)
def _safe_json(obj):
    """Serialize dict to JSON string, replacing nan/inf with None.
    
    Standard json.dumps raises ValueError on nan/inf. Brain's BS math
    occasionally produces these on edge cases (near-expiry, zero-vol inputs).
    Without this wrapper, trace persistence would crash on those polls.
    
    Returns: (json_str, had_unsafe_values: bool)
    """
    import json as _json
    import math as _math
    
    def _sanitize(v):
        if isinstance(v, float):
            if _math.isnan(v) or _math.isinf(v):
                return None
            return v
        if isinstance(v, dict):
            return {k: _sanitize(vv) for k, vv in v.items()}
        if isinstance(v, (list, tuple)):
            return [_sanitize(vv) for vv in v]
        return v
    
    had_unsafe = False
    try:
        # Fast path — assume safe (allow_nan=False ensures we catch non-standard JSON)
        return _json.dumps(obj, allow_nan=False), False
    except (ValueError, TypeError):
        had_unsafe = True
        safe_obj = _sanitize(obj)
        return _json.dumps(safe_obj), True


# TASK 5.1 — Bounded append to trace array with truncation flag
def _trace_append(trace_dict, array_key, entry):
    """Append entry to trace_dict[array_key]; set trace.meta.truncated if cap hit.
    
    Every trace array append goes through this wrapper. Prevents any single
    poll from producing unbounded trace payload.
    
    If trace_dict is None or falsy, no-op (zero-cost guard for disabled mode).
    """
    if not trace_dict:
        return
    arr = trace_dict.get(array_key)
    if arr is None:
        return
    if len(arr) >= MAX_TRACE_ITEMS:
        # Already capped — mark meta once, then silently drop
        meta = trace_dict.get('meta', {})
        if not meta.get('truncated'):
            meta['truncated'] = True
            # Record truncation event in errors
            errs = trace_dict.get('errors', [])
            errs.append({
                'function': '_trace_append',
                'error': 'MAX_TRACE_ITEMS exceeded',
                'array_key': array_key,
                'cap': MAX_TRACE_ITEMS,
            })
        return
    arr.append(entry)


# TASK 5.1 — Verdict flip detection (accepts dict or JSON string — R2 fix)
def _detect_flip(current_verdict, previous_verdict):
    """Compare current vs previous verdict. Returns flip descriptor or None.
    
    Accepts dict or JSON string for both arguments — Kotlin calls via Chaquopy
    typically pass JSON strings (brain.callAttr stringifies JSONObject args),
    Python callers pass dicts. Parses as needed.
    
    Returns dict with 'event': 'flip' and changed fields, or None if no flip.
    """
    import json as _json
    
    def _coerce(v):
        """Accept dict pass-through or JSON string parse. None/malformed → None."""
        if isinstance(v, str):
            try:
                return _json.loads(v)
            except (ValueError, TypeError):
                return None
        return v
    
    current = _coerce(current_verdict)
    previous = _coerce(previous_verdict)
    
    if not isinstance(current, dict) or not isinstance(previous, dict):
        return None
    
    changed = {}
    for field in ('direction', 'action', 'strategy'):
        prev_v = previous.get(field)
        curr_v = current.get(field)
        if prev_v != curr_v and prev_v is not None:
            changed[field] = {'from': prev_v, 'to': curr_v}
    if not changed:
        return None
    return {
        'event': 'flip',
        'changed': changed,
    }

# END TASK 5.1

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
        # DOUBLE_DEBIT is not built by the live candidate generator.
        # Use the buildable neutral premium structure as the primary lane.
        primary = ['IRON_CONDOR']
        allowed = ['BEAR_CALL', 'BULL_PUT']
        blocked = ['BEAR_PUT', 'BULL_CALL', 'DOUBLE_DEBIT']

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

    # Range detection override — IB/IC first, regardless of IV bucket.
    if range_detected:
        primary = ['IRON_BUTTERFLY', 'IRON_CONDOR']
        if b == 'BEAR':
            allowed = ['BEAR_CALL', 'BULL_PUT']
        elif b == 'BULL':
            allowed = ['BULL_PUT', 'BEAR_CALL']
        else:
            allowed = ['BEAR_CALL', 'BULL_PUT']
        blocked = [s for s in blocked if s not in ('IRON_BUTTERFLY', 'IRON_CONDOR')]

    # IB blocked by default (margin concern) unless range override
    if 'IRON_BUTTERFLY' not in primary and 'IRON_BUTTERFLY' not in allowed:
        if 'IRON_BUTTERFLY' not in blocked: blocked.append('IRON_BUTTERFLY')

    return {'primary': primary, 'allowed': allowed, 'blocked': blocked}

# ─── FORCE ENGINE ───

def _assess_force1(stype, bias, regime=None):
    """Direction force: does bias support this strategy?"""
    b = bias.get('bias', 'NEUTRAL')
    rtype = regime.get('type', 'unknown') if isinstance(regime, dict) else 'unknown'
    is_neutral = stype in _CONST['NEUTRAL_TYPES']
    is_bull = stype in _CONST['DIR_BULL']
    is_bear = stype in _CONST['DIR_BEAR']
    if is_neutral:
        # Decision #1 (2026-04-26) — REVERTS BR84 back to JS behavior.
        # JS source of truth: app.js assessForce1_Intrinsic L3010 — "Neutral strategies: LOVE neutral bias".
        # NEUTRAL+NEUTRAL must auto-reward +1 to align ranking with currently-live PWA behavior.
        if b == 'NEUTRAL' or rtype == 'range':
            return 1  # neutral bias or active range regime + neutral strategy = full alignment
        if bias.get('strength', '') == 'MILD':
            return 0
        return -1  # strong directional bias + neutral strategy = opposing
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
    return 1

def _get_forces(stype, bias, vix, iv_pctl, regime=None):
    f1 = _assess_force1(stype, bias, regime)
    f2 = _assess_force2(stype)
    f3 = _assess_force3(stype, vix, iv_pctl)
    aligned = sum(1 for f in [f1, f2, f3] if f == 1)
    against = sum(1 for f in [f1, f2, f3] if f == -1)
    return {'f1': f1, 'f2': f2, 'f3': f3, 'aligned': aligned, 'against': against, 'score': f1+f2+f3}

# ─── CHAIN HELPERS ───

def _interpolate_strike_value(strikes, price, opt_type, field):
    """
    Linear interpolation of a chain greek (delta or theta) at an off-strike price.
    
    Mirrors JS chainDeltaAtPrice (app.js L4111-4140). Used by both _chain_delta
    and _chain_theta to honor "API first, BS fallback only" principle (Decision #13a).
    
    Returns:
        float — interpolated or single-bracket value if chain data sufficient.
        None  — if chain unusable; caller falls back to BS computation.
    
    Behavior:
        - Empty/single-strike chain → None (fall to BS).
        - Exact strike hit (price equals a chain key) → return chain value directly
          (no interpolation, exact-equality bypass).
        - Both brackets have non-null field → linear interpolate.
        - Only one bracket has value → return that value.
        - Neither bracket has value → None.
        - Price outside chain entirely → use nearest strike's field if non-null, else None.
    """
    if not strikes:
        return None
    try:
        all_k = sorted(int(k) for k in strikes.keys())
    except (ValueError, TypeError):
        return None
    if len(all_k) < 2:
        return None
    
    p = float(price)
    
    # Exact strike hit — bypass interpolation
    if int(p) in all_k and float(int(p)) == p:
        v = strikes.get(str(int(p)), {}).get(opt_type, {}).get(field)
        if v is not None:
            return v
        # Exact strike but value missing — fall through to bracket logic
    
    # Find bracketing strikes
    lo = None
    hi = None
    for i in range(len(all_k) - 1):
        if all_k[i] <= p <= all_k[i + 1]:
            lo = all_k[i]
            hi = all_k[i + 1]
            break
    
    if lo is None or hi is None:
        # Price outside chain — use nearest strike
        nearest = min(all_k, key=lambda k: abs(k - p))
        v = strikes.get(str(nearest), {}).get(opt_type, {}).get(field)
        return v  # may be None — caller falls to BS
    
    v_lo = strikes.get(str(lo), {}).get(opt_type, {}).get(field)
    v_hi = strikes.get(str(hi), {}).get(opt_type, {}).get(field)
    
    if v_lo is not None and v_hi is not None:
        # Both brackets — interpolate
        if hi == lo:
            return v_lo  # degenerate — exact match
        frac = (p - lo) / (hi - lo)
        return v_lo + frac * (v_hi - v_lo)
    if v_lo is not None:
        return v_lo
    if v_hi is not None:
        return v_hi
    return None


def _chain_delta(strikes, price, opt_type, spot, T, vol):
    """
    Chain delta lookup with off-strike interpolation (Decision #13a).
    Uses Upstox per-strike delta (incorporates IV smile) when available.
    Falls back to BS if chain unusable.
    """
    v = _interpolate_strike_value(strikes, price, opt_type, 'delta')
    if v is not None:
        return v
    return _bs_delta(spot, price, T, vol, opt_type)

def _chain_theta(strikes, price, opt_type, spot, T, vol):
    """
    Chain theta lookup with off-strike interpolation (Decision #13a — symmetric port).
    Uses Upstox per-strike theta when available. Falls back to BS if chain unusable.
    """
    v = _interpolate_strike_value(strikes, price, opt_type, 'theta')
    if v is not None:
        return v
    return _bs_theta(spot, price, T, vol, opt_type)

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


def _wall_tag(score, strategy_type):
    """Wall protection tag — display-only, mirrors JS computeWallScore tag mapping (app.js L3949).

    Returns one of: '🛡️ Wall', '🛡️', '🛡️🛡️', '📌 Pinned',
    '⚠️ Wall blocks', or '' (empty).

    Per-strategy asymmetry preserved per Decision #6 lock (Vivek 2026-04-26):
    BEAR_CALL and BULL_PUT use different tier thresholds for identical
    score values. Origin (intentional design vs JS oversight) is
    tracked as Audit Finding #15 for future investigation when trade
    history is sufficient for correlation analysis. Tag is display-only
    — does NOT affect trade-decision math (rank_candidates gates on
    wallScore, never wallTag).

    Defensive: returns '' on None, non-numeric input, or unknown
    strategy_type. Mirrors A.4a `_gamma_tag` pattern.
    """
    try:
        s = float(score)
    except (TypeError, ValueError):
        return ''
    if strategy_type == 'BEAR_CALL':
        if s == 1.0:
            return '🛡️ Wall'
        if s == 0.7:
            return '🛡️'
        return ''
    if strategy_type == 'BULL_PUT':
        if s >= 0.7:
            return '🛡️ Wall'
        if s >= 0.4:
            return '🛡️'
        return ''
    if strategy_type == 'IRON_CONDOR':
        if s >= 0.5:
            return '🛡️🛡️'
        if s >= 0.3:
            return '🛡️'
        return ''
    if strategy_type == 'IRON_BUTTERFLY':
        if s == 0.6:
            return '📌 Pinned'
        return ''
    if strategy_type in ('BULL_CALL', 'BEAR_PUT'):
        if s == -0.5:
            return '⚠️ Wall blocks'
        return ''
    return ''

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

def _gamma_tag(risk):
    """
    Decision #5 (A.4): map gamma_risk score to display tag.
    Brain.py is sole producer of gammaTag; MarketVivi reads cand['gammaTag'].
    Threshold 0.7 here MUST stay numerically identical to the swing-credit
    block consumer (`cand['gammaRisk'] >= 0.7`); behavioral and display
    thresholds share the same value by design.
    """
    try:
        r = float(risk)
    except (TypeError, ValueError):
        return ''
    if r >= 0.7:
        return '\u26a0\ufe0f High \u03b3'   # ⚠️ High γ
    if r >= 0.4:
        return '\u26a0\ufe0f \u03b3'         # ⚠️ γ
    return ''

def _credit_sigma_limits(ctx=None):
    min_sigma = _CONST.get('MIN_SIGMA_OTM', 0.5)
    max_sigma = _CONST.get('MAX_SIGMA_OTM', 0.8)
    if isinstance(ctx, dict):
        learned = ctx.get('_learned_branch_overrides') or {}
        try:
            if learned.get('min_sigma_otm') is not None:
                min_sigma = float(learned.get('min_sigma_otm'))
        except (TypeError, ValueError):
            pass
        try:
            if learned.get('max_sigma_otm') is not None:
                max_sigma = float(learned.get('max_sigma_otm'))
        except (TypeError, ValueError):
            pass
    return (min_sigma, max_sigma)

def _sigma_above_max(value, max_sigma):
    # Strike-step rounding can make a visually valid 0.8σ strike compute as
    # 0.8004σ. Treat that as within the calibrated bucket, but still reject
    # genuinely far OTM strikes such as 10σ wall anchors.
    return value is not None and value > (max_sigma + 0.02)

def _sigma_otm_value(strike, spot, daily_sigma):
    if not daily_sigma or daily_sigma <= 0:
        return None
    try:
        return abs(float(strike) - float(spot)) / float(daily_sigma)
    except (TypeError, ValueError, ZeroDivisionError):
        return None

def _normalize_approved_branch_proposals(ctx):
    raw = ctx.get('approvedProposals') or ctx.get('approved_proposals') or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    if isinstance(raw, dict):
        raw = [raw]
    out = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        payload = row.get('proposal_json')
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = None
        if not isinstance(payload, dict):
            payload = row
        merged = dict(payload)
        merged.setdefault('proposal_id', row.get('proposal_id') or row.get('id') or payload.get('proposal_id') or '')
        merged.setdefault('status', row.get('status') or payload.get('status') or '')
        merged.setdefault('index', row.get('index_key') or payload.get('index') or payload.get('index_key') or '')
        merged.setdefault('category', row.get('category') or payload.get('category') or '')
        merged.setdefault('priority', row.get('priority') or payload.get('priority') or '')
        out.append(merged)
    return out

def _proposal_list(value):
    if isinstance(value, str) and value.strip():
        return [value.strip().upper()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip().upper() for v in value if str(v).strip()]
    return []

def _proposal_matches(proposal, index_key, ctx, regime, vix):
    proposal_index = str(proposal.get('index') or proposal.get('index_key') or '').upper()
    if proposal_index and proposal_index != index_key:
        return False
    conditions = proposal.get('conditions') if isinstance(proposal.get('conditions'), dict) else {}
    regime_list = _proposal_list(conditions.get('regime'))
    if regime_list:
        current_regime = str(regime or ctx.get('regime') or '').upper()
        if current_regime not in regime_list:
            return False
    mode_required = str(conditions.get('trade_mode') or conditions.get('mode') or '').strip().lower()
    if mode_required and str(ctx.get('tradeMode', '')).strip().lower() != mode_required:
        return False
    bias_list = _proposal_list(conditions.get('bias'))
    if bias_list:
        active_bias = str(
            (ctx.get('effective_bias') or {}).get('bias')
            or (ctx.get('morningBias') or {}).get('bias')
            or ''
        ).upper()
        if active_bias not in bias_list:
            return False
    try:
        vix_min = conditions.get('vix_min')
        if vix_min is not None and float(vix) < float(vix_min):
            return False
    except (TypeError, ValueError):
        pass
    try:
        vix_max = conditions.get('vix_max')
        if vix_max is not None and float(vix) > float(vix_max):
            return False
    except (TypeError, ValueError):
        pass
    return True

def _learned_branch_overrides(ctx, index_key, regime, vix):
    proposals = _normalize_approved_branch_proposals(ctx)
    overrides = {
        'strategy_allow': set(),
        'strategy_block': set(),
        'min_sigma_otm': None,
        'max_sigma_otm': None,
        'matched_ids': [],
        'approved_count': len(proposals),
    }
    matched = []
    for proposal in proposals:
        if not _proposal_matches(proposal, index_key, ctx, regime, vix):
            continue
        matched.append(proposal.get('proposal_id') or proposal.get('id') or '')
        action = proposal.get('action') if isinstance(proposal.get('action'), dict) else {}
        for key in ('strategy_allow', 'allow'):
            for item in _proposal_list(action.get(key) or proposal.get(key)):
                overrides['strategy_allow'].add(item)
        for key in ('strategy_block', 'block'):
            for item in _proposal_list(action.get(key) or proposal.get(key)):
                overrides['strategy_block'].add(item)
        for field in ('min_sigma_otm', 'max_sigma_otm'):
            target = action.get(field, proposal.get(field))
            if target is None:
                continue
            try:
                overrides[field] = float(target)
            except (TypeError, ValueError):
                pass
    ctx.setdefault('_learned_branch_matches', {})[index_key] = [m for m in matched if m]
    return overrides

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

    # Stage 1 ranking correction: do not cap before sigma/probability gates and ranking.
    return pairs

# ─── BUILD SINGLE 2-LEG CANDIDATE ───

LEG_SCHEMA_VERSION = 1
CANDIDATE_SCHEMA_VERSION = 1

def _leg_market_value(leg_data):
    if not isinstance(leg_data, dict):
        return None
    return (
        leg_data.get('ltp')
        or leg_data.get('last_price')
        or leg_data.get('lastPrice')
        or leg_data.get('bid')
        or leg_data.get('ask')
    )

def _build_leg_record(strike, option_type, action, expiry, leg_data, atm):
    leg_data = leg_data or {}
    return {
        'leg_schema_version': LEG_SCHEMA_VERSION,
        'strike': strike,
        'option_type': option_type,
        'action': action,
        'expiry': expiry,
        'ltp': _leg_market_value(leg_data),
        'bid': leg_data.get('bid'),
        'ask': leg_data.get('ask'),
        'oi': leg_data.get('oi'),
        'volume': leg_data.get('volume'),
        'iv': leg_data.get('iv'),
        'delta': leg_data.get('delta'),
        'theta': leg_data.get('theta'),
        'gamma': leg_data.get('gamma'),
        'vega': leg_data.get('vega'),
        'pop': leg_data.get('pop'),
        'instrument_key': (leg_data.get('instrument_key') or leg_data.get('instrumentKey') or '') or None,
        'distance_from_atm': None if atm in (None, 0) else round(strike - atm, 2),
    }

def _candidate_lane(index_key, trade_mode):
    mode = 'swing' if str(trade_mode or 'intraday').lower() == 'swing' else 'intraday'
    return f"{index_key}_{mode}"

def _trade_lane(trade):
    if not isinstance(trade, dict):
        return 'NF_intraday'
    index_key = str(trade.get('index_key') or trade.get('index') or 'NF').upper()
    if index_key not in ('BNF', 'NF'):
        index_key = 'BNF' if 'BANK' in index_key else 'NF'
    trade_mode = str(trade.get('trade_mode') or trade.get('mode') or 'intraday').lower()
    return _candidate_lane(index_key, trade_mode)

def _current_ist_poll_ts():
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%dT%H:%M:%S%z")

def _derive_poll_timestamp(session_date, latest_poll):
    poll = latest_poll if isinstance(latest_poll, dict) else {}
    date_str = str(session_date or '').strip()
    time_str = str(poll.get('t') or poll.get('time') or '').strip()
    if date_str and time_str:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"):
            try:
                dt = datetime.strptime(f"{date_str} {time_str.upper()}", fmt)
                return dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%dT%H:%M:%S%z")
            except Exception:
                continue
    return _current_ist_poll_ts()

def _trade_outcome_bool(trade):
    if not isinstance(trade, dict):
        return None
    canonical = trade.get('canonical_won')
    if canonical is True or canonical is False:
        return canonical
    outcome_h2 = trade.get('outcome_h2')
    if outcome_h2 in (1, '1', True):
        return True
    if outcome_h2 in (0, '0', False):
        return False
    won = trade.get('won')
    if won is True or won is False:
        return won
    pnl = trade.get('actual_pnl')
    if pnl is None:
        return None
    try:
        pnl_num = float(pnl)
    except Exception:
        return None
    if pnl_num > 0:
        return True
    if pnl_num < 0:
        return False
    return None

def _load_signal_reliability(lane, ctx):
    rows = ctx.get('signalReliability') or ctx.get('signal_reliability') or []
    lane_key = str(lane or '').strip().lower()
    signals = {}
    low_sample = []

    if isinstance(rows, dict):
        rows = rows.get('rows') or rows.get('data') or rows.get(lane) or rows.get(lane_key) or []

    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        row_lane = str(row.get('lane') or '').strip().lower()
        if row_lane and row_lane != lane_key:
            continue
        signal_name = str(row.get('signal_name') or row.get('signal') or '').strip()
        if not signal_name:
            continue
        sample_size = row.get('sample_size')
        try:
            sample_size = int(sample_size if sample_size is not None else 0)
        except Exception:
            sample_size = 0
        raw_score = row.get('reliability_score')
        try:
            raw_score = float(raw_score) if raw_score is not None else None
        except Exception:
            raw_score = None
        effective_score = 0.5 if raw_score is None or sample_size < 30 else raw_score
        if sample_size < 30:
            low_sample.append(signal_name)
        signals[signal_name] = {
            'reliability_score': round(max(0.0, min(1.0, effective_score)), 4),
            'sample_size': sample_size,
            'raw_score': None if raw_score is None else round(raw_score, 4),
            'fallback_applied': sample_size < 30 or raw_score is None,
        }

    return {
        'lane': lane,
        'coverage': len(signals),
        'low_sample_signals': sorted(low_sample),
        'signals': signals,
    }

def build_ml_memory_block(candidate, ctx, calibration, closed_trades):
    lane = candidate.get('lane') or _candidate_lane(candidate.get('index'), ctx.get('tradeMode'))
    lane_trades = [
        t for t in (closed_trades or [])
        if isinstance(t, dict)
        and str(t.get('status') or '').upper() == 'CLOSED'
        and _trade_lane(t) == lane
    ]
    labeled_trades = [t for t in lane_trades if _trade_outcome_bool(t) is not None]
    wins = [t for t in labeled_trades if _trade_outcome_bool(t) is True]
    recent_trades = sorted(
        lane_trades,
        key=lambda t: str(t.get('exit_date') or t.get('updated_at') or t.get('created_at') or ''),
        reverse=True
    )[:5]
    recent_summary = []
    for trade in recent_trades:
        recent_summary.append({
            'id': trade.get('id'),
            'strategy_type': trade.get('strategy_type'),
            'actual_pnl': trade.get('actual_pnl'),
            'canonical_won': _trade_outcome_bool(trade),
            'exit_date': trade.get('exit_date') or trade.get('updated_at') or trade.get('created_at'),
        })

    lane_pnls = []
    for trade in labeled_trades:
        pnl = trade.get('actual_pnl')
        try:
            if pnl is not None:
                lane_pnls.append(float(pnl))
        except Exception:
            pass
    avg_pnl = round(sum(lane_pnls) / len(lane_pnls), 2) if lane_pnls else 0.0
    win_rate = round(len(wins) / len(labeled_trades), 4) if labeled_trades else None

    return {
        'lane': lane,
        'trade_count_closed': len(lane_trades),
        'trade_count_labeled': len(labeled_trades),
        'win_count': len(wins),
        'win_rate': win_rate,
        'avg_pnl': avg_pnl,
        'signal_reliability': _load_signal_reliability(lane, ctx),
        'recent_closed_trades': recent_summary,
        'calibration_signature': calibration.get('signature') if isinstance(calibration, dict) else None,
        'max_loss_streak': calibration.get('max_loss_streak') if isinstance(calibration, dict) else None,
    }

def build_elephant_fact_pack(result, ctx, polls, calibration, closed_trades):
    result = result if isinstance(result, dict) else {}
    ctx = ctx if isinstance(ctx, dict) else {}
    polls = polls if isinstance(polls, list) else []
    latest_poll = polls[-1] if polls else {}
    verdict = result.get('verdict') or {}
    generated = result.get('generated_candidates') or []
    rejected_candidates = result.get('rejected_candidates') or []
    watchlist = result.get('watchlist') or []
    generation_skip_reasons = result.get('generation_skip_reasons') or []
    rejected_sample, rejected_stats = _compact_rejected_candidates(rejected_candidates)
    signal_independence = _compute_signal_independence(result, ctx)
    coherence_signal = signal_coherence(polls, ctx)
    poll_ts = _derive_poll_timestamp(ctx.get('today_ist'), latest_poll)

    candidates = []
    for rank, cand in enumerate(generated, start=1):
        if not isinstance(cand, dict):
            continue
        lane = cand.get('lane') or _candidate_lane(cand.get('index'), ctx.get('tradeMode'))
        candidates.append({
            'candidate_id': cand.get('id'),
            'rank': rank,
            'lane': lane,
            'index': cand.get('index'),
            'strategy_type': cand.get('type'),
            'trade_mode': 'swing' if lane.endswith('_swing') else 'intraday',
            'watchlist_rank': next((i + 1 for i, w in enumerate(watchlist) if isinstance(w, dict) and w.get('id') == cand.get('id')), None),
            'economics': {
                'net_premium': cand.get('netPremium'),
                'max_profit': cand.get('maxProfit'),
                'max_loss': cand.get('maxLoss'),
                'risk_reward': cand.get('riskReward'),
                'premium_edge': cand.get('premiumEdge'),
                'ev_per_1k': cand.get('evPer1k'),
                'est_cost': cand.get('estCost'),
                'capital_blocked': cand.get('capitalBlocked'),
            },
            'structure': {
                'width': cand.get('width'),
                'expiry': cand.get('expiry'),
                'sigma_otm': cand.get('sigmaOTM'),
                'iv_richness': cand.get('ivRichness'),
                'credit_width_ratio': cand.get('creditWidthRatio'),
                'is_credit': cand.get('isCredit'),
                'legs': cand.get('legs'),
                'candidate_schema_version': cand.get('candidate_schema_version'),
                'leg_schema_version': cand.get('leg_schema_version'),
            },
            'execution': {
                'execution_ready': cand.get('executionReady'),
                'execution_gate': cand.get('executionGate'),
                'entry_action': cand.get('entryAction'),
                'direction_safe': cand.get('directionSafe'),
            },
            'ml_overlay': {
                'brain_score': cand.get('brainScore'),
                'p_ml': cand.get('p_ml'),
                'ml_action': cand.get('mlAction'),
                'ml_edge': cand.get('mlEdge'),
                'ml_regime': cand.get('mlRegime'),
                'ml_unsure': cand.get('mlUnsure'),
                'ml_ood_flag': cand.get('mlOodFlag'),
                'decision_source': cand.get('decisionSource'),
                'decision_reason': cand.get('decisionReason'),
            },
            'ml_memory': build_ml_memory_block(cand, ctx, calibration, closed_trades),
        })

    return {
        'schema_version': 1,
        'observe_only': True,
        'quality_tag': 'qualitative_prompt_v2',
        'poll_timestamp': poll_ts,
        'session_date': ctx.get('today_ist'),
        'trade_mode': ctx.get('tradeMode') or ctx.get('trade_mode'),
        'decision_source': result.get('decision_source') or result.get('decisionSource'),
        'signal_independence': signal_independence,
        'coherence_signal': coherence_signal,
        'market_context': {
            'vix': latest_poll.get('vix') or latest_poll.get('VIX') or ctx.get('vix'),
            'bnf_spot': latest_poll.get('bnfSpot') or latest_poll.get('bnf') or latest_poll.get('BNF') or ctx.get('bnfSpot'),
            'nf_spot': latest_poll.get('nfSpot') or latest_poll.get('nf') or latest_poll.get('NF') or ctx.get('nfSpot'),
            'gap_info': result.get('gapInfo'),
            'nf_gap_info': result.get('nfGapInfo'),
            'range_sigma': result.get('rangeSigma'),
            'regime': result.get('regime'),
            'market_phase': result.get('marketPhase'),
            'institutional_regime': result.get('institutionalRegime'),
            'effective_bias': result.get('effective_bias'),
            'morning_bias': result.get('morningBias'),
            'bnf_breadth': ctx.get('bnfBreadth'),
            'nf50_breadth': ctx.get('nf50Breadth'),
            'signal_accuracy': ctx.get('signalAccuracy'),
        },
        'verdict_context': {
            'action': verdict.get('action'),
            'strategy': verdict.get('strategy'),
            'direction': verdict.get('direction'),
            'confidence': verdict.get('confidence'),
            'reasoning': verdict.get('reasoning'),
            'conflicts': verdict.get('conflicts'),
        },
        'candidate_counts': {
            'generated': len(generated) if isinstance(generated, list) else 0,
            'watchlist': len(watchlist) if isinstance(watchlist, list) else 0,
            'rejected': len(rejected_candidates) if isinstance(rejected_candidates, list) else 0,
        },
        'generation_skip_reason': generation_skip_reasons[0] if generation_skip_reasons else None,
        'generation_skip_reasons': generation_skip_reasons,
        'rejected_candidate_stats': rejected_stats,
        'rejected_candidates': rejected_sample,
        'candidates': candidates,
    }

def _record_rejected_candidate(
    ctx,
    *,
    index_key,
    trade_mode,
    expiry,
    stype,
    width,
    stage,
    reason,
    pair=None,
    atm=None,
    legs=None,
    is_credit=None,
    net_premium=None,
    max_profit=None,
    max_loss=None,
    tdte=None,
    iv_richness=None,
    credit_width_ratio=None,
    sigma_otm=None,
    extra=None,
):
    rejected = ctx.setdefault('_rejected_candidates', [])
    rec = {
        'candidate_schema_version': CANDIDATE_SCHEMA_VERSION,
        'leg_schema_version': LEG_SCHEMA_VERSION,
        'index': index_key,
        'lane': _candidate_lane(index_key, trade_mode),
        'strategy_type': stype,
        'expiry': expiry,
        'width': width,
        'is_credit': is_credit,
        'netPremium': net_premium,
        'maxProfit': max_profit,
        'maxLoss': max_loss,
        'tDTE': tdte,
        'ivRichness': iv_richness,
        'creditWidthRatio': credit_width_ratio,
        'sigmaOTM': sigma_otm,
        'poll_ts': _current_ist_poll_ts(),
        'rejection_stage': stage,
        'rejection_reason': reason,
        'sellStrike': pair.get('sell') if isinstance(pair, dict) else None,
        'sellType': pair.get('sellType') if isinstance(pair, dict) else None,
        'buyStrike': pair.get('buy') if isinstance(pair, dict) else None,
        'buyType': pair.get('buyType') if isinstance(pair, dict) else None,
        'legs': legs or [],
    }
    if isinstance(extra, dict):
        rec.update(extra)
    rejected.append(rec)

def _build_candidate(stype, pair, strikes, spot, lot_size, width, T, tdte, vol, expiry, is_bnf, vix, trade_mode, ctx, atm):
    sp_sell = str(int(pair['sell']))
    sp_buy = str(int(pair['buy']))
    sell_data = strikes.get(sp_sell, {}).get(pair['sellType'], {})
    buy_data = strikes.get(sp_buy, {}).get(pair['buyType'], {})
    idx = 'BNF' if is_bnf else 'NF'
    base_is_credit = stype in _CONST['CREDIT_TYPES']

    def leg_records():
        return [
            _build_leg_record(pair.get('buy'), pair.get('buyType'), 'BUY', expiry, buy_data, atm),
            _build_leg_record(pair.get('sell'), pair.get('sellType'), 'SELL', expiry, sell_data, atm),
        ]

    def trace_attempt(payload):
        if ctx.get('_trace') is None:
            return
        _active = ctx.get('_active_cand_trace')
        if _active is not None:
            _trace_append(_active, 'attempts', payload)

    def record_rejection(stage, reason, **extra):
        payload = {
            'stype': stype,
            'sell': pair.get('sell'),
            'buy': pair.get('buy'),
            'result': 'rejected',
            'stage': stage,
            'reason': reason,
        }
        payload.update(extra)
        trace_attempt(payload)
        _record_rejected_candidate(
            ctx,
            index_key=idx,
            trade_mode=trade_mode,
            expiry=expiry,
            stype=stype,
            width=width,
            stage=stage,
            reason=reason,
            pair=pair,
            atm=atm,
            legs=leg_records(),
            is_credit=base_is_credit,
            net_premium=extra.get('net_prem'),
            max_profit=extra.get('max_profit'),
            max_loss=extra.get('max_loss'),
            tdte=tdte,
            iv_richness=extra.get('iv_richness'),
            credit_width_ratio=extra.get('credit_ratio'),
            sigma_otm=extra.get('sigma_otm'),
            extra={k: v for k, v in extra.items() if k not in {'net_prem', 'max_profit', 'max_loss', 'iv_richness', 'credit_ratio', 'sigma_otm'}},
        )

    if not sell_data or not buy_data:
        record_rejection('strike_data_missing', 'sell_data or buy_data missing')
        return None

    is_credit = base_is_credit
    # Use executable pricing for both credit and debit spreads:
    # short leg sells at bid, long/protection leg buys at ask.
    sell_price = sell_data.get('bid', 0)
    buy_price = buy_data.get('ask', 0)
    if not sell_price or not buy_price:
        record_rejection('price_zero', 'sell_price or buy_price zero')
        return None

    if is_credit:
        net_prem = sell_price - buy_price
        if net_prem <= 0:
            record_rejection('credit_non_positive', 'is_credit and net_prem <= 0', net_prem=round(net_prem, 4))
            return None
        max_profit = net_prem * lot_size
        max_loss = (width - net_prem) * lot_size
    else:
        net_prem = buy_price - sell_price
        if net_prem <= 0:
            record_rejection('debit_credit_conflict_non_positive_net', 'net_prem <= 0 in debit block', net_prem=round(net_prem, 4))
            return None
        max_profit = (width - net_prem) * lot_size
        max_loss = net_prem * lot_size

    if max_loss <= 0 or max_profit <= 0:
        record_rejection('max_loss_non_positive', 'max_loss <= 0 or max_profit <= 0', net_prem=round(net_prem, 4), max_profit=round(max_profit, 2), max_loss=round(max_loss, 2))
        return None
    capital = _capital
    # BR118: Risk limit check (10% of capital)
    if max_loss > capital * 0.10:
        record_rejection('capital_limit_exceeded', 'max_loss > capital * 0.10', net_prem=round(net_prem, 4), max_profit=round(max_profit, 2), max_loss=round(max_loss, 2))
        return None

    # Sigma OTM filter — credit directional only.
    # Reject both too-close sells and economically worthless far-OTM sells.
    sigma_otm = None
    ds = _daily_sigma(spot, vix)
    if is_credit and stype in ('BEAR_CALL', 'BULL_PUT') and ds > 0:
        sigma_otm = abs(pair['sell'] - spot) / ds
        min_sigma, max_sigma = _credit_sigma_limits(ctx)
        if sigma_otm < min_sigma:
            record_rejection('sigma_otm_too_close', 'is_credit directional and sigma_otm < 0.5',
                             sigma_otm=round(sigma_otm, 2),
                             net_prem=round(net_prem, 4),
                             max_profit=round(max_profit, 2),
                             max_loss=round(max_loss, 2),
                             credit_ratio=round(net_prem / width, 4) if width else None)
            return None
        if _sigma_above_max(sigma_otm, max_sigma):
            record_rejection('sigma_otm_too_far', 'is_credit directional and sigma_otm > max_sigma',
                             sigma_otm=round(sigma_otm, 2),
                             max_sigma=max_sigma,
                             net_prem=round(net_prem, 4),
                             max_profit=round(max_profit, 2),
                             max_loss=round(max_loss, 2),
                             credit_ratio=round(net_prem / width, 4) if width else None)
            return None
        sigma_otm = round(sigma_otm, 2)

    # Minimum width filter — narrow credit directional rejected
    if is_credit and stype in ('BEAR_CALL', 'BULL_PUT'):
        min_w = _CONST['MIN_WIDTH_BNF'] if is_bnf else _CONST['MIN_WIDTH_NF']
        if width < min_w:
            record_rejection('width_too_narrow', 'is_credit directional and width < min_width')
            return None
        if tdte < _CONST['MIN_CREDIT_DTE']:
            record_rejection('credit_dte_below_floor', 'is_credit directional and tdte < MIN_CREDIT_DTE')
            return None
        credit_ratio = (net_prem / width) if width > 0 else 0
        if credit_ratio < _CONST['MIN_CREDIT_RATIO']:
            record_rejection('credit_ratio_below_floor', 'is_credit directional and credit/width < MIN_CREDIT_RATIO', credit_ratio=round(credit_ratio, 4), net_prem=round(net_prem, 4))
            return None
        realized_vol = _realized_vol_proxy(vix)
        iv_richness = (vol / realized_vol) if realized_vol and vol > 0 else None
        if iv_richness is not None and iv_richness < _CONST['IV_RICH_MIN']:
            record_rejection('iv_not_rich', 'is_credit directional and iv_richness < IV_RICH_MIN', iv_richness=round(iv_richness, 3), atm_iv_pct=round(vol * 100, 2), vix=vix, net_prem=round(net_prem, 4), credit_ratio=round(credit_ratio, 4))
            return None
    else:
        credit_ratio = (net_prem / width) if (is_credit and width > 0) else None
        iv_richness = None

    # Probability at breakeven
    if is_credit:
        be = pair['sell'] + net_prem if pair['sellType'] == 'CE' else pair['sell'] - net_prem
        prob = 1 - abs(_chain_delta(strikes, be, pair['sellType'], spot, T, vol))
    else:
        be = pair['buy'] + net_prem if pair['buyType'] == 'CE' else pair['buy'] - net_prem
        prob = abs(_chain_delta(strikes, be, pair['buyType'], spot, T, vol))

    # BR98: Credit requires 50% prob floor. Debit can be acceptable <50% if EV clearly positive.
    if is_credit and prob < _CONST['MIN_PROB']:
        record_rejection('credit_prob_below_floor', 'is_credit and prob < MIN_PROB', net_prem=round(net_prem, 4), max_profit=round(max_profit, 2), max_loss=round(max_loss, 2), prob=round(prob, 4))
        return None
    if not is_credit:
        # For debit: require prob >= 40% floor AND 10% positive EV buffer
        if prob < 0.40:
            record_rejection('debit_prob_below_floor', 'not is_credit and prob < 0.40', net_prem=round(net_prem, 4), max_profit=round(max_profit, 2), max_loss=round(max_loss, 2), prob=round(prob, 4))
            return None
        expected_win = prob * max_profit
        expected_loss = (1 - prob) * max_loss
        if expected_win < expected_loss * 1.10:  # require 10% positive EV buffer
            record_rejection('debit_negative_ev', 'not is_credit and EV check fails', net_prem=round(net_prem, 4), max_profit=round(max_profit, 2), max_loss=round(max_loss, 2), expected_win=round(expected_win, 2), expected_loss=round(expected_loss, 2))
            return None

    true_prob = prob
    if is_credit:
        realized_vol = _realized_vol_proxy(vix)
        if realized_vol:
            true_prob = 1 - abs(_chain_delta(strikes, be, pair['sellType'], spot, T, realized_vol))
    premium_edge = round(true_prob * max_profit - (1 - true_prob) * max_loss)
    ev = round(prob * max_profit * 0.65 - (1 - prob) * max_loss)
    sell_theta = _chain_theta(strikes, pair['sell'], pair['sellType'], spot, T, vol) * lot_size
    buy_theta = _chain_theta(strikes, pair['buy'], pair['buyType'], spot, T, vol) * lot_size
    net_theta = round(-(sell_theta - buy_theta) if is_credit else (sell_theta - buy_theta))
    cid = f"{stype}_{idx}_{pair['sell']}_{pair['buy']}_W{width}"

    # BR100: IC margin typically 1.05× max_loss.
    est_cost = round(max_loss * (1.05 if stype in ('IRON_CONDOR', 'IRON_BUTTERFLY') else 1.0))
    est_cost_pct = round(est_cost / max(1, _capital) * 100, 1)
    # netDelta: directional exposure (positive = bullish, negative = bearish)
    sell_delta = sell_data.get('delta', 0) or 0
    buy_delta  = buy_data.get('delta', 0) or 0
    net_delta  = round((sell_delta - buy_delta) if is_credit else (buy_delta - sell_delta), 4)
    # netMaxProfit: same as maxProfit for spreads (legs already netted)
    net_max_profit = round(max_profit)
    # upstoxPop: Upstox's own P(profit) from option_greeks.pop for sell leg
    upstox_pop = sell_data.get('pop')
    sell_instrument_key = (sell_data.get('instrument_key') or sell_data.get('instrumentKey') or '').strip()
    buy_instrument_key = (buy_data.get('instrument_key') or buy_data.get('instrumentKey') or '').strip()
    sell_symbol = (sell_data.get('symbol') or sell_data.get('trading_symbol') or '').strip()
    buy_symbol = (buy_data.get('symbol') or buy_data.get('trading_symbol') or '').strip()

    # TASK 5.6 — trace accepted candidate
    trace_attempt({
        'stype': stype,
        'sell': pair.get('sell'), 'buy': pair.get('buy'),
        'width': width, 'result': 'accepted',
    })
    return {
        'id': cid, 'type': stype, 'width': width, 'legs': [
        _build_leg_record(pair['buy'], pair['buyType'], 'BUY', expiry, buy_data, atm),
        _build_leg_record(pair['sell'], pair['sellType'], 'SELL', expiry, sell_data, atm),
        ],
        'legCount': 2,
        'lane': _candidate_lane(idx, trade_mode),
        'leg_schema_version': LEG_SCHEMA_VERSION,
        'candidate_schema_version': CANDIDATE_SCHEMA_VERSION,
        'sellStrike': pair['sell'], 'buyStrike': pair['buy'],
        'sellType': pair['sellType'], 'buyType': pair['buyType'],
        'sellLTP': sell_price, 'buyLTP': buy_price,
        'sellOI': sell_data.get('oi', 0), 'buyOI': buy_data.get('oi', 0),
        'netPremium': round(net_prem, 2), 'maxProfit': round(max_profit),
        'maxLoss': round(max_loss), 'probProfit': round(prob, 3),
        'trueProb': round(true_prob, 3),
        'premiumEdge': premium_edge,
        'creditWidthRatio': round(credit_ratio, 4) if credit_ratio is not None else None,
        'ivRichness': round(iv_richness, 3) if iv_richness is not None else None,
        'ev': ev, 'netTheta': net_theta, 'isCredit': is_credit,
        'lotSize': lot_size, 'index': idx, 'expiry': expiry, 'tDTE': tdte,
        'sigmaOTM': sigma_otm,
        'riskReward': f"1:{max_profit/max_loss:.2f}" if max_loss > 0 else '--',
        'targetProfit': round(max_profit * 0.5),
        'stopLoss': round(max_loss * 0.6 if is_credit else max_loss * 0.5),
        # ── 5 display fields ──
        'estCost':       est_cost,
        'estCostPct':    est_cost_pct,
        'netDelta':      net_delta,
        'netMaxProfit':  net_max_profit,
        'upstoxPop':     upstox_pop,
        'sellInstrumentKey': sell_instrument_key or None,
        'buyInstrumentKey': buy_instrument_key or None,
        'sellSymbol': sell_symbol or None,
        'buySymbol': buy_symbol or None,
        # ML fields are populated by SPLICE 4 after full live context is available.
        'p_ml':          None,
        'mlAction':      None,
        'mlRegime':      None,
        'mlEdge':        None,
        'mlOod':         False,
        'mlOodFlag':     False,
        'mlOodConf':     1.0,
        'mlOodBlocked':  False,
        'mlUnsure':      False,
        'mlUnsureReason': [],
        'decisionSource': 'DEFAULT_BRAIN_MATH',
        'decision_source': 'DEFAULT_BRAIN_MATH',
        'decisionReason': 'candidate ranked by deterministic brain rules',
        'decision_reason': 'candidate ranked by deterministic brain rules',
        'beUpper': round(pair['sell'] + net_prem) if (is_credit and pair['sellType'] == 'CE') else (round(pair['buy'] + net_prem) if (not is_credit and pair['buyType'] == 'CE') else None),
        'beLower': round(pair['sell'] - net_prem) if (is_credit and pair['sellType'] == 'PE') else (round(pair['buy'] - net_prem) if (not is_credit and pair['buyType'] == 'PE') else None),
    }

def check_execution_readiness(candidate, current_result, ctx):
    """Phase 12A readiness contract.
    Annotates execution gates without changing trade-selection behavior."""
    candidate = candidate or {}
    current_result = current_result or {}
    ctx = ctx or {}

    mode = str(ctx.get('executionMode') or 'paper').lower()
    if mode not in ('paper', 'sandbox', 'live'):
        mode = 'paper'

    sell_key = str(candidate.get('sellInstrumentKey') or '').strip()
    buy_key = str(candidate.get('buyInstrumentKey') or '').strip()
    has_instrument_keys = bool(sell_key and buy_key)

    token_ready = bool(ctx.get('authTokenReady'))
    if not token_ready:
        raw_token = str(ctx.get('authToken') or '').strip()
        token_ready = bool(raw_token)

    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    mins = now_ist.hour * 60 + now_ist.minute
    is_weekday = now_ist.weekday() < 5
    in_hours = (9 * 60 + 15) <= mins <= (15 * 60 + 30)
    not_holiday = now_ist.strftime('%Y-%m-%d') not in _CONST.get('NSE_HOLIDAYS', [])
    market_hours_ok = is_weekday and in_hours and not_holiday

    proxy_url = str(ctx.get('orderProxyUrl') or '').strip()
    proxy_ready = proxy_url.startswith('https://')
    sandbox_enabled = bool(ctx.get('sandboxEnabled'))

    reasons = []
    if not has_instrument_keys:
        reasons.append('instrument keys missing')
    if mode in ('sandbox', 'live') and not token_ready:
        reasons.append('token not ready')
    if mode == 'sandbox' and not sandbox_enabled:
        reasons.append('sandbox disabled')
    if mode == 'live' and not proxy_ready:
        reasons.append('proxy not configured')
    if mode in ('sandbox', 'live') and not market_hours_ok:
        reasons.append('outside NSE market hours')

    ready = len(reasons) == 0
    return {
        'ready': ready,
        'mode': mode,
        'gate': 'READY' if ready else 'WAIT',
        'reasons': reasons,
        'checks': {
            'hasInstrumentKeys': has_instrument_keys,
            'tokenReady': token_ready,
            'sandboxEnabled': sandbox_enabled,
            'proxyReady': proxy_ready,
            'marketHoursOk': market_hours_ok,
        }
    }

# ─── MAIN: GENERATE ALL CANDIDATES FOR ONE INDEX ───

def generate_candidates(chain, spot, index_key, expiry, vix, bias, iv_pctl, ctx, regime=None):
    if not chain or not chain.get('strikes') or not chain.get('atm'):
        return [], []
    is_bnf = index_key == 'BNF'
    idx = 'BNF' if is_bnf else 'NF'

    # TASK 5.5 — trace init for this index
    _trace_root = ctx.get('_trace')
    _cand_trace = None
    if _trace_root is not None:
        _cand_trace = {
            'config': {
                'index_key': index_key, 'spot': spot, 'atm': chain.get('atm'),
                'expiry': expiry, 'vix': vix,
                'bias': bias.get('bias', 'NEUTRAL') if isinstance(bias, dict) else str(bias),
                'bias_strength': bias.get('strength', '') if isinstance(bias, dict) else '',
                'iv_pctl': iv_pctl,
                'lot_size': _CONST['BNF_LOT'] if is_bnf else _CONST['NF_LOT'],
                'widths': list(_CONST['BNF_WIDTHS'] if is_bnf else _CONST['NF_WIDTHS']),
                'trade_mode': ctx.get('tradeMode', 'swing'),
                'range_detected': (ctx.get('rangeSigma') or 999) < 0.3,
                'strikes_count': len(chain.get('strikes', {})),
                'cw_strike': chain.get('callWallStrike', 0),
                'pw_strike': chain.get('putWallStrike', 0),
            },
            'varsity': {},
            'attempts': [],
            'summary': None,
        }
        _trace_root['candidates'][index_key] = _cand_trace
        ctx['_active_cand_trace'] = _cand_trace
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
    mins_since_open = ctx.get('mins_since_open', ctx.get('minsSinceOpen', 0)) or 0

    # Range detection from context
    range_detected = (ctx.get('rangeSigma') or 999) < 0.3

    varsity = _get_varsity_filter(bias, vix, trade_mode, range_detected)
    allowed_types = varsity['primary'] + varsity['allowed']
    learned = _learned_branch_overrides(ctx, index_key, regime, vix)
    ctx['_learned_branch_overrides'] = learned
    for stype in learned['strategy_allow']:
        if stype and stype not in allowed_types:
            allowed_types.append(stype)
    if learned['strategy_block']:
        allowed_types = [stype for stype in allowed_types if stype not in learned['strategy_block']]
    # TASK 5.5 — varsity snapshot
    if _cand_trace is not None:
        _cand_trace['varsity'] = {
            'primary': list(varsity.get('primary', [])),
            'allowed': list(varsity.get('allowed', [])),
            'blocked': list(varsity.get('blocked', [])),
            'learned_allow': sorted(list(learned.get('strategy_allow', []))),
            'learned_block': sorted(list(learned.get('strategy_block', []))),
            'matched_branch_ids': list(learned.get('matched_ids', [])),
        }
    candidates = []
    rejected_baseline = len(ctx.setdefault('_rejected_candidates', []))

    # ═══ 1. DIRECTIONAL 2-LEG SPREADS ═══
    dir_types = [t for t in ['BEAR_CALL', 'BULL_PUT', 'BEAR_PUT', 'BULL_CALL'] if t in allowed_types]
    for stype in dir_types:
        for width in widths:
            pairs = _get_strike_pairs(stype, atm, width, step, all_strikes, spot, is_bnf, cw, pw)
            for pair in pairs:
                cand = _build_candidate(stype, pair, strikes, spot, lot_size, width, T, tdte, vol, expiry, is_bnf, vix, trade_mode, ctx, atm)
                if not cand: continue

                # Range budget filter — debit only
                if stype in _CONST['DEBIT_TYPES']:
                    ds = _daily_sigma(spot, vix)
                    trade_sigma = _sigma_days(spot, vix, tdte)
                    if stype == 'BULL_CALL' and width > trade_sigma * 1.2: continue
                    if stype == 'BEAR_PUT' and width > trade_sigma * 1.2: continue

                cand['forces'] = _get_forces(stype, bias, vix, iv_pctl, regime)
                cand['varsityTier'] = 'PRIMARY' if stype in varsity['primary'] else 'ALLOWED'
                cand['wallScore'] = _compute_wall_score(cand, chain, is_bnf)
                cand['wallTag'] = _wall_tag(cand['wallScore'], cand['type'])
                cand['gammaRisk'] = _compute_gamma_risk(cand, spot, tdte)
                cand['gammaTag'] = _gamma_tag(cand['gammaRisk'])
                cand['contextScore'] = _compute_context_score(cand, spot, tdte, vix, ctx)
                cand['directionSafe'] = cand['forces']['f1'] >= 0
                cand['capitalBlocked'] = False

                # Block high gamma ATM sells in swing mode
                if trade_mode == 'swing' and cand['isCredit'] and cand['gammaRisk'] >= 0.7:
                    cand['capitalBlocked'] = True

                candidates.append(cand)

    def _record_multi_leg_rejection(
        *,
        stype,
        width,
        stage,
        reason,
        legs=None,
        is_credit=True,
        net_premium=None,
        max_profit=None,
        max_loss=None,
        iv_richness=None,
        credit_width_ratio=None,
        sigma_otm=None,
        extra=None,
    ):
        if ctx.get('_trace') is not None:
            _active = ctx.get('_active_cand_trace')
            if _active is not None:
                payload = {
                    'stype': stype,
                    'width': width,
                    'result': 'rejected',
                    'stage': stage,
                    'reason': reason,
                }
                if isinstance(extra, dict):
                    payload.update(extra)
                if net_premium is not None:
                    payload['net_premium'] = round(net_premium, 4)
                if max_profit is not None:
                    payload['max_profit'] = round(max_profit, 2)
                if max_loss is not None:
                    payload['max_loss'] = round(max_loss, 2)
                if iv_richness is not None:
                    payload['iv_richness'] = round(iv_richness, 3)
                if credit_width_ratio is not None:
                    payload['credit_ratio'] = round(credit_width_ratio, 4)
                if sigma_otm is not None:
                    payload['sigma_otm'] = round(sigma_otm, 2)
                _trace_append(_active, 'attempts', payload)
        _record_rejected_candidate(
            ctx,
            index_key=idx,
            trade_mode=trade_mode,
            expiry=expiry,
            stype=stype,
            width=width,
            stage=stage,
            reason=reason,
            atm=atm,
            legs=legs or [],
            is_credit=is_credit,
            net_premium=round(net_premium, 4) if net_premium is not None else None,
            max_profit=max_profit,
            max_loss=max_loss,
            tdte=tdte,
            iv_richness=round(iv_richness, 3) if iv_richness is not None else None,
            credit_width_ratio=round(credit_width_ratio, 4) if credit_width_ratio is not None else None,
            sigma_otm=round(sigma_otm, 2) if sigma_otm is not None else None,
            extra=extra,
        )

    # ═══ 2. IRON CONDOR (intraday only — 0% overnight survival) ═══
    if 'IRON_CONDOR' in allowed_types and trade_mode != 'swing' and mins_since_open < 300:
        for width in widths:
            dist_pairs = []
            # BR103: Sigma-based spacing — 5-6 candidates per width (was 17-20).
            _IC_MAX_PAIRS = 6
            ds_local = _daily_sigma(spot, vix)
            if ds_local > 0:
                sigma_multipliers = [0.5, 0.75, 1.0, 1.25, 1.5]
                for sm in sigma_multipliers:
                    dist = round(ds_local * sm / step) * step
                    if width <= dist <= 1200: # caps at 1200 strike dist
                        dist_pairs.append((dist, dist))
                # Add wall pairs if valid
                if cw and pw and cw > atm and pw < atm:
                    cw_d = cw - atm; pw_d = atm - pw
                    wall_ce_sigma = _sigma_otm_value(cw, spot, ds_local)
                    wall_pe_sigma = _sigma_otm_value(pw, spot, ds_local)
                    wall_max_sigma = _CONST.get('IC_WALL_MAX_SIGMA', 1.5)
                    if (
                        wall_ce_sigma is not None and wall_pe_sigma is not None
                        and not _sigma_above_max(wall_ce_sigma, wall_max_sigma)
                        and not _sigma_above_max(wall_pe_sigma, wall_max_sigma)
                        and cw_d <= 1200 and pw_d <= 1200
                        and (cw_d, pw_d) not in dist_pairs
                    ):
                        dist_pairs.append((cw_d, pw_d))
            else:
                # Fallback: bounded geometric spacing
                count = 0
                for dist in range(width, 1200, step * 2):
                    dist_pairs.append((dist, dist))
                    count += 1
                    if count >= _IC_MAX_PAIRS: break

            seen_ic = set()
            for call_dist, put_dist in dist_pairs:
                sell_call = atm + call_dist
                buy_call = sell_call + width
                sell_put = atm - put_dist
                buy_put = sell_put - width
                ds_for_gate = ds_local if ds_local > 0 else _daily_sigma(spot, vix)
                min_sigma, _ = _credit_sigma_limits(ctx)
                max_sigma = _CONST.get('IC_WALL_MAX_SIGMA', 1.5)
                call_sigma = _sigma_otm_value(sell_call, spot, ds_for_gate)
                put_sigma = _sigma_otm_value(sell_put, spot, ds_for_gate)
                if call_sigma is None or put_sigma is None:
                    _record_multi_leg_rejection(
                        stype='IRON_CONDOR',
                        width=width,
                        stage='sigma_data_missing',
                        reason='iron_condor missing sigma values for sell legs',
                        sigma_otm=None,
                        extra={'sell_call': sell_call, 'sell_put': sell_put, 'buy_call': buy_call, 'buy_put': buy_put},
                    )
                    continue
                if call_sigma < min_sigma or put_sigma < min_sigma:
                    _record_multi_leg_rejection(
                        stype='IRON_CONDOR',
                        width=width,
                        stage='sigma_otm_too_close',
                        reason='iron_condor sell leg sigma below minimum',
                        sigma_otm=max(call_sigma, put_sigma),
                        extra={'sell_call': sell_call, 'sell_put': sell_put, 'buy_call': buy_call, 'buy_put': buy_put},
                    )
                    continue
                if _sigma_above_max(call_sigma, max_sigma) or _sigma_above_max(put_sigma, max_sigma):
                    _record_multi_leg_rejection(
                        stype='IRON_CONDOR',
                        width=width,
                        stage='sigma_otm_too_far',
                        reason='iron_condor sell leg sigma above maximum',
                        sigma_otm=max(call_sigma, put_sigma),
                        extra={'sell_call': sell_call, 'sell_put': sell_put, 'buy_call': buy_call, 'buy_put': buy_put, 'max_sigma': max_sigma},
                    )
                    continue
                if sell_call not in all_set or buy_call not in all_set or sell_put not in all_set or buy_put not in all_set:
                    _record_multi_leg_rejection(
                        stype='IRON_CONDOR',
                        width=width,
                        stage='strike_missing',
                        reason='iron_condor strike missing in chain strike set',
                        sigma_otm=max(call_sigma, put_sigma),
                        extra={'sell_call': sell_call, 'sell_put': sell_put, 'buy_call': buy_call, 'buy_put': buy_put},
                    )
                    continue
                pk = f"{sell_call}_{sell_put}_{width}"
                if pk in seen_ic: continue
                seen_ic.add(pk)

                sc = str(int(sell_call)); bc = str(int(buy_call))
                sp = str(int(sell_put)); bp = str(int(buy_put))
                ce_s = strikes.get(sc, {}).get('CE', {})
                ce_b = strikes.get(bc, {}).get('CE', {})
                pe_s = strikes.get(sp, {}).get('PE', {})
                pe_b = strikes.get(bp, {}).get('PE', {})
                ic_legs = [
                    _build_leg_record(buy_call, 'CE', 'BUY', expiry, ce_b, atm),
                    _build_leg_record(sell_call, 'CE', 'SELL', expiry, ce_s, atm),
                    _build_leg_record(buy_put, 'PE', 'BUY', expiry, pe_b, atm),
                    _build_leg_record(sell_put, 'PE', 'SELL', expiry, pe_s, atm),
                ]
                if not all([ce_s, ce_b, pe_s, pe_b]):
                    _record_multi_leg_rejection(
                        stype='IRON_CONDOR',
                        width=width,
                        stage='leg_data_missing',
                        reason='iron_condor CE/PE leg data missing',
                        legs=ic_legs,
                        sigma_otm=max(call_sigma, put_sigma),
                        extra={'sell_call': sell_call, 'sell_put': sell_put, 'buy_call': buy_call, 'buy_put': buy_put},
                    )
                    continue

                call_credit = (ce_s.get('bid', 0) or 0) - (ce_b.get('ask', 0) or 0)
                put_credit = (pe_s.get('bid', 0) or 0) - (pe_b.get('ask', 0) or 0)
                if call_credit <= 0 or put_credit <= 0:
                    _record_multi_leg_rejection(
                        stype='IRON_CONDOR',
                        width=width,
                        stage='credit_non_positive',
                        reason='iron_condor leg credit non-positive',
                        legs=ic_legs,
                        sigma_otm=max(call_sigma, put_sigma),
                        extra={'sell_call': sell_call, 'sell_put': sell_put, 'buy_call': buy_call, 'buy_put': buy_put, 'call_credit': round(call_credit, 4), 'put_credit': round(put_credit, 4)},
                    )
                    continue
                total_credit = call_credit + put_credit
                max_loss_ps = width - total_credit
                if max_loss_ps <= 0:
                    _record_multi_leg_rejection(
                        stype='IRON_CONDOR',
                        width=width,
                        stage='max_loss_non_positive',
                        reason='iron_condor width minus total credit <= 0',
                        legs=ic_legs,
                        net_premium=total_credit,
                        max_loss=max_loss_ps * lot_size,
                        sigma_otm=max(call_sigma, put_sigma),
                        extra={'sell_call': sell_call, 'sell_put': sell_put, 'buy_call': buy_call, 'buy_put': buy_put},
                    )
                    continue

                max_profit = round(total_credit * lot_size)
                max_loss = round(max_loss_ps * lot_size)
                capital = _capital
                # BR118: Risk limit check
                if max_loss > capital * 0.10:
                    _record_multi_leg_rejection(
                        stype='IRON_CONDOR',
                        width=width,
                        stage='capital_limit_exceeded',
                        reason='iron_condor max_loss exceeds capital risk limit',
                        legs=ic_legs,
                        net_premium=total_credit,
                        max_profit=max_profit,
                        max_loss=max_loss,
                        sigma_otm=max(call_sigma, put_sigma),
                        extra={'sell_call': sell_call, 'sell_put': sell_put, 'buy_call': buy_call, 'buy_put': buy_put},
                    )
                    continue
                if max_loss <= 0 or max_profit <= 0:
                    _record_multi_leg_rejection(
                        stype='IRON_CONDOR',
                        width=width,
                        stage='economics_invalid',
                        reason='iron_condor max_loss <= 0 or max_profit <= 0',
                        legs=ic_legs,
                        net_premium=total_credit,
                        max_profit=max_profit,
                        max_loss=max_loss,
                        sigma_otm=max(call_sigma, put_sigma),
                        extra={'sell_call': sell_call, 'sell_put': sell_put, 'buy_call': buy_call, 'buy_put': buy_put},
                    )
                    continue

                # Probability: spot stays between breakevens
                upper_be = sell_call + total_credit
                lower_be = sell_put - total_credit
                prob_above_put = 1 - abs(_bs_delta(spot, lower_be, T, vol, 'PE'))
                prob_below_call = 1 - abs(_bs_delta(spot, upper_be, T, vol, 'CE'))
                prob = max(0, prob_above_put + prob_below_call - 1)
                if prob < _CONST['MIN_PROB']:
                    _record_multi_leg_rejection(
                        stype='IRON_CONDOR',
                        width=width,
                        stage='prob_below_floor',
                        reason='iron_condor probability below minimum floor',
                        legs=ic_legs,
                        net_premium=total_credit,
                        max_profit=max_profit,
                        max_loss=max_loss,
                        sigma_otm=max(call_sigma, put_sigma),
                        extra={'sell_call': sell_call, 'sell_put': sell_put, 'buy_call': buy_call, 'buy_put': buy_put, 'prob': round(prob, 4)},
                    )
                    continue
                credit_ratio = total_credit / width if width else None
                if credit_ratio is not None and credit_ratio < _CONST['MIN_CREDIT_RATIO']:
                    _record_multi_leg_rejection(
                        stype='IRON_CONDOR',
                        width=width,
                        stage='credit_ratio_below_floor',
                        reason='iron_condor credit/width below minimum ratio',
                        legs=ic_legs,
                        net_premium=total_credit,
                        max_profit=max_profit,
                        max_loss=max_loss,
                        credit_width_ratio=credit_ratio,
                        sigma_otm=max(call_sigma, put_sigma),
                        extra={'sell_call': sell_call, 'sell_put': sell_put, 'buy_call': buy_call, 'buy_put': buy_put},
                    )
                    continue
                realized_vol = _realized_vol_proxy(vix)
                iv_richness = (vol / realized_vol) if realized_vol and vol > 0 else None
                if iv_richness is not None and iv_richness < _CONST['IV_RICH_MIN']:
                    _record_multi_leg_rejection(
                        stype='IRON_CONDOR',
                        width=width,
                        stage='iv_not_rich',
                        reason='iron_condor and iv_richness < IV_RICH_MIN',
                        legs=ic_legs,
                        net_premium=total_credit,
                        max_profit=max_profit,
                        max_loss=max_loss,
                        iv_richness=iv_richness,
                        credit_width_ratio=credit_ratio,
                        sigma_otm=max(call_sigma, put_sigma),
                        extra={'sell_call': sell_call, 'sell_put': sell_put, 'buy_call': buy_call, 'buy_put': buy_put, 'atm_iv_pct': round(vol * 100, 2), 'vix': vix},
                    )
                    continue

                ev = round(prob * max_profit * 0.65 - (1 - prob) * max_loss)  # Gemini fix
                net_theta = round(abs(
                    _chain_theta(strikes, sell_call, 'CE', spot, T, vol) +
                    _chain_theta(strikes, sell_put, 'PE', spot, T, vol) -
                    _chain_theta(strikes, buy_call, 'CE', spot, T, vol) -
                    _chain_theta(strikes, buy_put, 'PE', spot, T, vol)
                ) * lot_size)

                ic = {
                    'id': f"IC_{idx}_{sell_call}_{sell_put}_W{width}",
                    'type': 'IRON_CONDOR', 'width': width, 'legs': [
                        _build_leg_record(buy_call, 'CE', 'BUY', expiry, ce_b, atm),
                        _build_leg_record(sell_call, 'CE', 'SELL', expiry, ce_s, atm),
                        _build_leg_record(buy_put, 'PE', 'BUY', expiry, pe_b, atm),
                        _build_leg_record(sell_put, 'PE', 'SELL', expiry, pe_s, atm),
                    ],
                    'legCount': 4,
                    'lane': _candidate_lane(idx, trade_mode),
                    'leg_schema_version': LEG_SCHEMA_VERSION,
                    'candidate_schema_version': CANDIDATE_SCHEMA_VERSION,
                    'sellStrike': sell_call, 'buyStrike': buy_call,
                    'sellStrike2': sell_put, 'buyStrike2': buy_put,
                    'sellType': 'CE', 'buyType': 'CE', 'sellType2': 'PE', 'buyType2': 'PE',
                    'sellLTP': ce_s.get('bid', 0), 'buyLTP': ce_b.get('ask', 0),
                    'sellLTP2': pe_s.get('bid', 0), 'buyLTP2': pe_b.get('ask', 0),
                    'sellInstrumentKey': (ce_s.get('instrument_key') or ce_s.get('instrumentKey') or '') or None,
                    'buyInstrumentKey': (ce_b.get('instrument_key') or ce_b.get('instrumentKey') or '') or None,
                    'sellInstrumentKey2': (pe_s.get('instrument_key') or pe_s.get('instrumentKey') or '') or None,
                    'buyInstrumentKey2': (pe_b.get('instrument_key') or pe_b.get('instrumentKey') or '') or None,
                    'sellSymbol': (ce_s.get('symbol') or ce_s.get('trading_symbol') or '') or None,
                    'buySymbol': (ce_b.get('symbol') or ce_b.get('trading_symbol') or '') or None,
                    'sellSymbol2': (pe_s.get('symbol') or pe_s.get('trading_symbol') or '') or None,
                    'buySymbol2': (pe_b.get('symbol') or pe_b.get('trading_symbol') or '') or None,
                    'netPremium': round(total_credit, 2),
                    'maxProfit': max_profit, 'maxLoss': max_loss,
                    'probProfit': round(prob, 3), 'ev': ev, 'netTheta': net_theta,
                    'isCredit': True, 'lotSize': lot_size, 'index': idx,
                    'expiry': expiry, 'tDTE': tdte,
                    'ivRichness': round(iv_richness, 3) if iv_richness is not None else None,
                    'sigmaOTM': round(max(call_sigma, put_sigma), 2),
                    'sigmaOTMCall': round(call_sigma, 2),
                    'sigmaOTMPut': round(put_sigma, 2),
                    'riskReward': f"1:{max_profit/max_loss:.2f}" if max_loss > 0 else '--',
                    'targetProfit': round(max_profit * 0.5),
                    'stopLoss': round(max_loss * 0.6),
                    'forces': _get_forces('IRON_CONDOR', bias, vix, iv_pctl, regime),
                    'varsityTier': 'PRIMARY' if 'IRON_CONDOR' in varsity['primary'] else 'ALLOWED',
                    'wallScore': 0, 'gammaRisk': 0, 'contextScore': 0,
                    'directionSafe': True, 'capitalBlocked': False,
                    'beUpper': round(upper_be), 'beLower': round(lower_be),
                }
                ic['wallScore'] = _compute_wall_score(ic, chain, is_bnf)
                ic['wallTag'] = _wall_tag(ic['wallScore'], ic['type'])
                ic['gammaRisk'] = _compute_gamma_risk(ic, spot, tdte)
                ic['gammaTag'] = _gamma_tag(ic['gammaRisk'])
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
            if buy_call not in all_set or buy_put not in all_set:
                _record_multi_leg_rejection(
                    stype='IRON_BUTTERFLY',
                    width=width,
                    stage='strike_missing',
                    reason='iron_butterfly strike missing in chain strike set',
                    sigma_otm=0.0,
                    extra={'sell_call': atm, 'sell_put': atm, 'buy_call': buy_call, 'buy_put': buy_put},
                )
                continue

            sc = str(int(atm))
            ce_s = strikes.get(sc, {}).get('CE', {})
            pe_s = strikes.get(sc, {}).get('PE', {})
            ce_b = strikes.get(str(int(buy_call)), {}).get('CE', {})
            pe_b = strikes.get(str(int(buy_put)), {}).get('PE', {})
            ib_legs = [
                _build_leg_record(buy_call, 'CE', 'BUY', expiry, ce_b, atm),
                _build_leg_record(atm, 'CE', 'SELL', expiry, ce_s, atm),
                _build_leg_record(buy_put, 'PE', 'BUY', expiry, pe_b, atm),
                _build_leg_record(atm, 'PE', 'SELL', expiry, pe_s, atm),
            ]
            if not all([ce_s, pe_s, ce_b, pe_b]):
                _record_multi_leg_rejection(
                    stype='IRON_BUTTERFLY',
                    width=width,
                    stage='leg_data_missing',
                    reason='iron_butterfly CE/PE leg data missing',
                    legs=ib_legs,
                    sigma_otm=0.0,
                    extra={'sell_call': atm, 'sell_put': atm, 'buy_call': buy_call, 'buy_put': buy_put},
                )
                continue

            call_credit = (ce_s.get('bid', 0) or 0) - (ce_b.get('ask', 0) or 0)
            put_credit = (pe_s.get('bid', 0) or 0) - (pe_b.get('ask', 0) or 0)
            if call_credit <= 0 or put_credit <= 0:
                _record_multi_leg_rejection(
                    stype='IRON_BUTTERFLY',
                    width=width,
                    stage='credit_non_positive',
                    reason='iron_butterfly leg credit non-positive',
                    legs=ib_legs,
                    sigma_otm=0.0,
                    extra={'sell_call': atm, 'sell_put': atm, 'buy_call': buy_call, 'buy_put': buy_put, 'call_credit': round(call_credit, 4), 'put_credit': round(put_credit, 4)},
                )
                continue
            total_credit = call_credit + put_credit
            max_loss_ps = width - total_credit
            if max_loss_ps <= 0:
                _record_multi_leg_rejection(
                    stype='IRON_BUTTERFLY',
                    width=width,
                    stage='max_loss_non_positive',
                    reason='iron_butterfly width minus total credit <= 0',
                    legs=ib_legs,
                    net_premium=total_credit,
                    max_loss=max_loss_ps * lot_size,
                    sigma_otm=0.0,
                    extra={'sell_call': atm, 'sell_put': atm, 'buy_call': buy_call, 'buy_put': buy_put},
                )
                continue

            max_profit = round(total_credit * lot_size)
            max_loss = round(max_loss_ps * lot_size)
            # BR118: Risk limit check
            if max_loss > _capital * 0.10:
                _record_multi_leg_rejection(
                    stype='IRON_BUTTERFLY',
                    width=width,
                    stage='capital_limit_exceeded',
                    reason='iron_butterfly max_loss exceeds capital risk limit',
                    legs=ib_legs,
                    net_premium=total_credit,
                    max_profit=max_profit,
                    max_loss=max_loss,
                    sigma_otm=0.0,
                    extra={'sell_call': atm, 'sell_put': atm, 'buy_call': buy_call, 'buy_put': buy_put},
                )
                continue

            upper_be = atm + total_credit
            lower_be = atm - total_credit
            prob_above = 1 - abs(_bs_delta(spot, lower_be, T, vol, 'PE'))
            prob_below = 1 - abs(_bs_delta(spot, upper_be, T, vol, 'CE'))
            prob = max(0, prob_above + prob_below - 1)
            if prob < _CONST['MIN_PROB']:
                _record_multi_leg_rejection(
                    stype='IRON_BUTTERFLY',
                    width=width,
                    stage='prob_below_floor',
                    reason='iron_butterfly probability below minimum floor',
                    legs=ib_legs,
                    net_premium=total_credit,
                    max_profit=max_profit,
                    max_loss=max_loss,
                    sigma_otm=0.0,
                    extra={'sell_call': atm, 'sell_put': atm, 'buy_call': buy_call, 'buy_put': buy_put, 'prob': round(prob, 4)},
                )
                continue
            realized_vol = _realized_vol_proxy(vix)
            iv_richness = (vol / realized_vol) if realized_vol and vol > 0 else None
            if iv_richness is not None and iv_richness < _CONST['IV_RICH_MIN']:
                _record_multi_leg_rejection(
                    stype='IRON_BUTTERFLY',
                    width=width,
                    stage='iv_not_rich',
                    reason='iron_butterfly and iv_richness < IV_RICH_MIN',
                    legs=ib_legs,
                    net_premium=total_credit,
                    max_profit=max_profit,
                    max_loss=max_loss,
                    iv_richness=iv_richness,
                    sigma_otm=0.0,
                    extra={'sell_call': atm, 'sell_put': atm, 'buy_call': buy_call, 'buy_put': buy_put, 'atm_iv_pct': round(vol * 100, 2), 'vix': vix},
                )
                continue

            ev = round(prob * max_profit * 0.65 - (1 - prob) * max_loss)  # Gemini fix
            ib = {
                'id': f"IB_{idx}_{atm}_W{width}",
                'type': 'IRON_BUTTERFLY', 'width': width, 'legs': [
                    _build_leg_record(buy_call, 'CE', 'BUY', expiry, ce_b, atm),
                    _build_leg_record(atm, 'CE', 'SELL', expiry, ce_s, atm),
                    _build_leg_record(buy_put, 'PE', 'BUY', expiry, pe_b, atm),
                    _build_leg_record(atm, 'PE', 'SELL', expiry, pe_s, atm),
                ],
                'legCount': 4,
                'lane': _candidate_lane(idx, trade_mode),
                'leg_schema_version': LEG_SCHEMA_VERSION,
                'candidate_schema_version': CANDIDATE_SCHEMA_VERSION,
                'sellStrike': atm, 'buyStrike': buy_call,
                'sellStrike2': atm, 'buyStrike2': buy_put,
                'sellType': 'CE', 'buyType': 'CE', 'sellType2': 'PE', 'buyType2': 'PE',
                'sellLTP': ce_s.get('bid', 0), 'buyLTP': ce_b.get('ask', 0),
                'sellLTP2': pe_s.get('bid', 0), 'buyLTP2': pe_b.get('ask', 0),
                'sellInstrumentKey': (ce_s.get('instrument_key') or ce_s.get('instrumentKey') or '') or None,
                'buyInstrumentKey': (ce_b.get('instrument_key') or ce_b.get('instrumentKey') or '') or None,
                'sellInstrumentKey2': (pe_s.get('instrument_key') or pe_s.get('instrumentKey') or '') or None,
                'buyInstrumentKey2': (pe_b.get('instrument_key') or pe_b.get('instrumentKey') or '') or None,
                'sellSymbol': (ce_s.get('symbol') or ce_s.get('trading_symbol') or '') or None,
                'buySymbol': (ce_b.get('symbol') or ce_b.get('trading_symbol') or '') or None,
                'sellSymbol2': (pe_s.get('symbol') or pe_s.get('trading_symbol') or '') or None,
                'buySymbol2': (pe_b.get('symbol') or pe_b.get('trading_symbol') or '') or None,
                'netPremium': round(total_credit, 2),
                'maxProfit': max_profit, 'maxLoss': max_loss,
                'probProfit': round(prob, 3), 'ev': ev, 'isCredit': True,
                'lotSize': lot_size, 'index': idx, 'expiry': expiry, 'tDTE': tdte,
                'ivRichness': round(iv_richness, 3) if iv_richness is not None else None,
                'forces': _get_forces('IRON_BUTTERFLY', bias, vix, iv_pctl, regime),
                'varsityTier': 'PRIMARY' if 'IRON_BUTTERFLY' in varsity['primary'] else 'ALLOWED',
                'wallScore': _compute_wall_score({'type': 'IRON_BUTTERFLY', 'sellStrike': atm}, chain, is_bnf),
                'gammaRisk': 0.5 if tdte <= 2 else 0.3 if tdte <= 3 else 0.1,
                'contextScore': 0, 'directionSafe': True, 'capitalBlocked': False,
                'riskReward': f"1:{max_profit/max_loss:.2f}" if max_loss > 0 else '--',
                'targetProfit': round(max_profit * 0.5),
                'stopLoss': round(max_loss * 0.6),
                'beUpper': round(upper_be), 'beLower': round(lower_be),
            }
            ib['gammaTag'] = _gamma_tag(ib['gammaRisk'])
            ib['wallTag'] = _wall_tag(ib['wallScore'], ib['type'])
            candidates.append(ib)

    # TASK 5.5 — Summary Trace
    if _cand_trace is not None:
        _cand_trace['summary'] = {
            'total_found': len(candidates),
            'types_found': sorted(list(set(c['type'] for c in candidates))),
            'primary_found': len([c for c in candidates if c.get('varsityTier') == 'PRIMARY']),
            'timestamp': time.time() if 'time' in dir() else 0,
        }
    return candidates, ctx.get('_rejected_candidates', [])[rejected_baseline:]

# ─── RANK CANDIDATES ───

def rank_candidates(candidates, calibration=None, brain_verdict=None, stage2a=None):
    """Varsity waterfall ranking. Premium edge outranks raw win-rate once safety gates pass."""
    cal = calibration or {}
    strat_cal = cal.get('strategy', {})
    stage2a = stage2a or {}
    teacher_live = bool(stage2a.get('ranking_active'))

    def sort_key(c):
        # 0: Direction safety — F1-against always last
        safe = 0 if c.get('directionSafe', True) else 1
        # 1: Varsity tier
        tier = 0 if c.get('varsityTier') == 'PRIMARY' else 1
        teacher_rank_active = 1
        teacher_score = 0.0
        teacher_n = 0
        if teacher_live and c.get('teacher_ranking_eligible'):
            teacher_rank_active = 0
            teacher_score = _safe_num(c.get('teacher_r_score'), 0.0)
            teacher_n = int(c.get('teacher_bucket_n') or 0)
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
        # 8: Premium edge — honest EV-like ranking signal after hard gates pass
        premium_edge = c.get('premiumEdge', c.get('ev', 0))
        # 9: Probability
        prob = c.get('probProfit', 0)
        # 10: ML score — tiebreaker. Neutralised when OOD/UNSURE.
        p_ml = c.get('p_ml') or 0.0
        if c.get('mlUnsure') or c.get('mlAction') == 'UNSURE':
            p_ml = 0.0
        elif c.get('mlOod') and (c.get('mlOodConf') or 1.0) < 0.6:
            p_ml = 0.0

        return (
            safe, tier,
            teacher_rank_active, -teacher_score, -teacher_n,
            -premium_edge, bv, -win_rate, -aligned, against, -ctx_score, gamma, -wall, -prob, -p_ml
        )

    ranked = [c for c in candidates if not c.get('capitalBlocked')]
    ranked.sort(key=sort_key)
    return ranked

def _strategy_action(strategy_type):
    return 'BUY PREMIUM' if strategy_type in _CONST.get('DEBIT_TYPES', set()) else 'SELL PREMIUM'

def _strategy_direction(strategy_type):
    if strategy_type in ('BULL_CALL', 'BULL_PUT'):
        return 'BULL'
    if strategy_type in ('BEAR_CALL', 'BEAR_PUT'):
        return 'BEAR'
    return 'NEUTRAL'

def _build_watchlist_from_ranked(ranked, per_index_diverse=3, head_count=6):
    """Top-ranked executable list plus per-index diversity picks."""
    watchlist = list(ranked[:head_count])
    seen_ids = set(c.get('id') for c in watchlist)
    for idx_pick in ('BNF', 'NF'):
        seen_types = set()
        idx_added = 0
        for c in ranked:
            if c.get('index') != idx_pick:
                continue
            if c.get('capitalBlocked'):
                continue
            if c.get('type') in seen_types:
                continue
            if c.get('id') in seen_ids:
                continue
            seen_types.add(c.get('type'))
            seen_ids.add(c.get('id'))
            watchlist.append(c)
            idx_added += 1
            if idx_added >= per_index_diverse:
                break
    return watchlist


def _stage2a_stamp_candidate_ranks(ranked, field_name):
    if not isinstance(ranked, list):
        return
    for idx, cand in enumerate(ranked, start=1):
        if isinstance(cand, dict):
            cand[field_name] = idx


def _stage2a_apply_live_wait_guard(result, watchlist, stage2a_summary):
    if not isinstance(stage2a_summary, dict) or stage2a_summary.get('mode') != 'live':
        return result
    if not stage2a_summary.get('table_ready'):
        stage2a_summary['hard_wait_triggered'] = True
        stage2a_summary['hard_wait_reason'] = 'teacher_table_unavailable'
    elif int(stage2a_summary.get('positive_count') or 0) <= 0:
        stage2a_summary['hard_wait_triggered'] = True
        stage2a_summary['hard_wait_reason'] = 'no_positive_teacher_bucket'
    else:
        return result

    verdict = dict((result or {}).get('verdict') or {})
    conflicts = list(verdict.get('conflicts') or [])
    conflicts.append('Stage 2A teacher guard forced WAIT: no candidate cleared positive covered expectancy')
    verdict.update({
        'action': 'WAIT',
        'strategy': None,
        'direction': 'NEUTRAL',
        'confidence': 0,
        'urgency': 'WAIT — teacher expectancy guard',
        'reasoning': 'Teacher ranking did not find any candidate with covered positive expectancy. Hard WAIT preserved.',
        'conflicts': conflicts,
        'decision_source': 'TEACHER_ONLY',
        'decisionSource': 'TEACHER_ONLY',
    })
    result['verdict'] = verdict
    result['decisionSource'] = 'TEACHER_ONLY'
    result['decision_source'] = 'TEACHER_ONLY'
    result['decisionReason'] = 'Stage 2A teacher guard forced WAIT because no candidate had positive covered expectancy.'
    result['decision_reason'] = result['decisionReason']
    result['stage2a_wait_guard'] = {
        'triggered': True,
        'reason': stage2a_summary.get('hard_wait_reason'),
    }
    return result

def _align_verdict_to_watchlist(verdict, watchlist, ctx=None):
    """Make final brain verdict match the executable candidate lane shown to user.

    synthesize_verdict() runs before native candidate generation, so it can
    describe the market thesis while the generated executable candidates belong
    to a different lane. The app must not display BUY PREMIUM while the primary
    card is IC/Bear Call credit. This reconciles the final verdict to the top
    executable candidate and records the adjustment in conflicts.
    """
    if not isinstance(verdict, dict):
        return verdict
    if verdict.get('action') in ('WAIT', 'STOP', None):
        return verdict

    executable = [
        c for c in (watchlist or [])
        if isinstance(c, dict) and not c.get('capitalBlocked') and c.get('type')
    ]
    if not executable:
        final = dict(verdict)
        final['pre_alignment_action'] = final.get('action')
        final['pre_alignment_strategy'] = final.get('strategy')
        final['action'] = 'WAIT'
        final['strategy'] = None
        final['direction'] = 'NEUTRAL'
        final['confidence'] = 0
        final['execution_aligned'] = False
        conflicts = list(final.get('conflicts') or [])
        conflicts.append('No executable candidates - WAIT')
        final['conflicts'] = conflicts
        return final

    top = executable[0]
    top_type = top.get('type')
    if not top_type:
        return verdict
    lane_counts = {}
    for cand in executable:
        lane = cand.get('type')
        if lane:
            lane_counts[lane] = lane_counts.get(lane, 0) + 1
    dominant_lane = max(lane_counts, key=lane_counts.get) if lane_counts else top_type

    final = dict(verdict)
    old_action = final.get('action')
    old_strategy = final.get('strategy')
    new_action = _strategy_action(top_type)
    new_direction = _strategy_direction(top_type)
    aligned = (top.get('forces') or {}).get('aligned', 0)
    floor = 0
    if isinstance(aligned, (int, float)):
        floor = 70 if aligned >= 3 else 45 if aligned >= 2 else 30
    old_conf = final.get('confidence') or 0
    changed = (
        old_action != new_action
        or old_strategy != top_type
        or final.get('direction') != new_direction
        or final.get('execution_candidate_id') != top.get('id')
        or final.get('execution_candidate_index') != top.get('index')
        or old_conf < floor
    )
    if not changed and final.get('execution_aligned'):
        return verdict

    final['pre_alignment_action'] = old_action
    final['pre_alignment_strategy'] = old_strategy
    final['action'] = new_action
    final['strategy'] = top_type
    final['direction'] = new_direction
    final['execution_aligned'] = True
    final['execution_candidate_id'] = top.get('id')
    final['execution_candidate_index'] = top.get('index')
    final['dominant_lane'] = dominant_lane
    final['dominant_count'] = lane_counts.get(dominant_lane, 0)

    conflicts = list(final.get('conflicts') or [])
    if old_action != new_action or old_strategy != top_type:
        conflicts.append(f"Execution aligned to top candidate: {old_strategy or '--'}/{old_action or '--'} -> {top_type}/{new_action}")
        final['conflicts'] = conflicts

    if floor:
        final['confidence'] = max(final.get('confidence') or 0, floor)

    reasoning = final.get('reasoning') or ''
    exec_reason = f"Top executable: {top.get('index', '')} {top_type}"
    final['reasoning'] = f"{reasoning} | {exec_reason}" if reasoning else exec_reason
    return final


def analyze(poll_json, trades_json, baseline_json, open_trades_json, candidates_json, strike_oi_json, context_json='{}'):
    polls = json.loads(poll_json)
    closed_trades = json.loads(trades_json) if trades_json else []
    baseline = json.loads(baseline_json) if baseline_json else {}
    open_trades = json.loads(open_trades_json) if open_trades_json else []
    candidates = json.loads(candidates_json) if candidates_json else []
    strike_oi = json.loads(strike_oi_json) if strike_oi_json else {}
    ctx = json.loads(context_json) if context_json else {}

    # TASK 5.2 — trace init
    # Install trace if debug enabled. Source tag 'replay' overridden by replay() endpoint.
    if ctx.get('_debug'):
        _trace_source = ctx.get('_trace_source_override', 'live')
        ctx['_trace'] = _new_trace(source=_trace_source)

    result = {"verdict": None, "market": [], "positions": {}, "candidates": {}, "timing": [], "risk": [], "learnedBranches": {}}
    if len(polls) < 3:
        # Do not return an empty stub for low-history sessions. Emit an explicit
        # WAIT verdict so UI and logs can explain why strategy generation is empty.
        result["verdict"] = {
            "action": "WAIT",
            "strategy": None,
            "direction": "NEUTRAL",
            "confidence": 0,
            "urgency": "WAIT — building poll history",
            "reasoning": f"Insufficient history ({len(polls)} polls). Need >=3 for full analysis.",
            "conflicts": [f"Insufficient poll history: {len(polls)}/3"],
            "bull": 0.0,
            "bear": 0.0,
        }
        result["generated_candidates"] = []
        result["watchlist"] = []
        result["candidateTrace"] = False
        result["decisionSource"] = "DEFAULT_BRAIN_MATH"
        result["decision_source"] = "DEFAULT_BRAIN_MATH"
        result["decisionReason"] = "insufficient poll history; no ML decision applied"
        result["decision_reason"] = result["decisionReason"]
        result["learnedBranches"] = {
            "approved_count": len(_normalize_approved_branch_proposals(ctx)),
            "matched_by_index": {},
        }
        try:
            result["agent"] = build_explanation_audit_agent(result, ctx, open_trades)
        except Exception as e:
            result["agent_error"] = str(e)

        # Even on early return, embed trace so consumer knows it ran
        if ctx.get('_trace'):
            result['_trace'] = ctx['_trace']
        return json.dumps(result)

    # Set capital from JS context (single source of truth: C.CAPITAL)
    global _capital
    _capital = ctx.get('capital', 250000)

    build_calibration(closed_trades)
    regime = detect_regime(polls, baseline)
    ctx['rangeSigma'] = regime.get('sigma', 0)
    ctx['regime'] = regime
    result['regime'] = regime
    result['rangeSigma'] = regime.get('sigma', 0)
    result['marketPhase'] = _synthesize_market_phase(regime, ctx)
    ctx['marketPhase'] = result['marketPhase'].get('id', 'UNKNOWN')
    latest_poll = polls[-1] if isinstance(polls, list) and polls else {}

    def _latest_spot_value(index_key):
        base_key = 'bnfSpot' if index_key == 'BNF' else 'nfSpot'
        poll_key = 'bnf' if index_key == 'BNF' else 'nf'
        return (
            latest_poll.get(base_key)
            or latest_poll.get(poll_key)
            or latest_poll.get(poll_key.upper())
            or ctx.get(base_key)
            or baseline.get(base_key)
            or 0
        )

    # Phase C: compute profiles before context-aware insights/verdict use them.
    bnf_chain = ctx.get('bnfChain') or {}
    nf_chain = ctx.get('nfChain') or {}
    bnf_spot = _latest_spot_value('BNF')
    nf_spot = _latest_spot_value('NF')
    bnf_ohlc = ctx.get('bnfOHLC')
    nf_ohlc = ctx.get('nfOHLC')
    vix = ctx.get('vix') or 15
    gap = ctx.get('gap') or {}

    result["bnfProfile"] = chain_profile(bnf_chain, bnf_spot, bnf_ohlc, vix, gap) or {}
    result["nfProfile"] = chain_profile(nf_chain, nf_spot, nf_ohlc, vix, gap) or {}
    ctx["bnfProfile"] = result["bnfProfile"]
    ctx["nfProfile"] = result["nfProfile"]

    result["bnfContrarian"] = get_contrarian_pcr(result["bnfProfile"])
    result["nfContrarian"] = get_contrarian_pcr(result["nfProfile"])

    hist = ctx.get('yesterdayHistory', [])
    result["pcrContext"] = get_institutional_pcr(
        result["bnfProfile"].get('nearAtmPCR') or result["bnfProfile"].get('pcr'),
        vix, gap, hist, baseline, bnf_chain
    )
    result["sessionTrajectory"] = get_session_trajectory(hist)
    ctx["pcrContext"] = result["pcrContext"]

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
    # Candlestick pattern detection
    try:
        candle_data = compute_candle_signals(polls, ctx)
        for ckey in ('candle_bnf', 'candle_nf'):
            result[ckey] = candle_data[ckey]
            for ins in candle_data[ckey].get('patterns', []):
                result["market"].append(ins)
    except Exception as e:
        print(f"DEBUG: candlestick detection failed: {e}")
    # b92: chain_profile_insights returns LIST
    try:
        ci_insights = chain_profile_insights(ctx)
        if ci_insights:
            result["market"].extend(ci_insights)
    except Exception as e:
        print(f"chain_profile_insights: {e}")

    # Positions — verdict + insights + Phase D (P&L + CI + WallDrift)
    result["position_live"] = {}
    spots = {'bnfSpot': bnf_spot, 'nfSpot': nf_spot}
    bnf_breadth = ctx.get('bnfBreadth')
    
    for t in open_trades:
        tid = t.get("id", "")
        # Decision #17/#18/#Issue9: Live metrics
        idx = t.get('index_key', 'BNF')
        chain = result["bnfProfile"] if idx == 'BNF' else result["nfProfile"]
        spot = bnf_spot if idx == 'BNF' else nf_spot
        
        pl_data = compute_position_live(t, bnf_chain, nf_chain, spots, vix, ctx, bnf_breadth)
        if pl_data:
            result["position_live"][tid] = pl_data
            # Update trade object for downstream insights
            t['current_pnl'] = pl_data['current_pnl']
            t['current_spot'] = pl_data['current_spot']
        
        t['controlIndex'] = compute_control_index(t, chain, spot, bnf_breadth)
        t['wallDrift'] = compute_wall_drift(t, chain)

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
        result["positions"][tid] = {
            "verdict": pv,
            "insights": ins,
            "controlIndex": t.get("controlIndex", 0),
            "wallDrift": t.get("wallDrift")
        }


    # ═══════════════════════════════════════════════════════════════
    # PHASE E — Snapshot / Positioning / Alerts (Decisions #24-27)
    # ═══════════════════════════════════════════════════════════════
    if 'pcrContext' in result:
        ctx['pcrContext'] = result['pcrContext']

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

    # Candidates — existing + liquidity + pattern match + b92 risk evaluation
    for c in candidates:
        cid = c.get("id", "")
        ins = []
        for fn in [candidate_flow_alignment, candidate_wall_protection, candidate_regime_fit, candidate_pattern_match]:
            try:
                r = fn(c, polls, baseline, regime)
                if r: ins.append(r)
            except Exception as e:
                print(f"candidate_insight {fn.__name__} for {cl_id_dummy if False else cid}: {e}")
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
        import time
        _START_ML = time.time()
        _MAX_ML_BUDGET = 2.5  # seconds — BR126: prevent background service ANR
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

    # Phase B: morning bias + regime + trends
    result["overnightDelta"] = compute_overnight_delta(ctx)
    ctx["overnightDelta"] = result["overnightDelta"]

    result["morningBias"] = compute_morning_bias(ctx, polls)
    ctx["morningBias"] = result["morningBias"]

    result["institutionalRegime"] = institutional_regime(ctx)
    ctx["institutionalRegime"] = result["institutionalRegime"]

    # ═══ THE VERDICT ═══
    all_insights = result["market"] + result["timing"] + result["risk"]
    try:
        result["verdict"] = synthesize_verdict(all_insights, regime, ctx, polls, baseline, candidates, result.get("candidates", {}))
    except Exception as e:
        print(f"DEBUG: synthesize_verdict failed: {e}")

    # b97: Effective bias — Bayesian decay of morning prior with intraday evidence
    try:
        result["effective_bias"] = compute_effective_bias(polls, baseline, ctx, regime)
        ctx["effective_bias"] = result["effective_bias"]
    except Exception as e:
        print(f"DEBUG: compute_effective_bias failed: {e}")

    result["fiiTrend"] = fii_short_trend(ctx)
    result["signalValidation"] = validate_yesterday_signal(ctx)

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
        all_rejected = []
        generation_skip_reasons = []

        def _record_generation_skip(index_key, chain_key, reason_code, detail):
            generation_skip_reasons.append({
                'index': index_key,
                'chain_key': chain_key,
                'reason_code': reason_code,
                'detail': detail,
            })

        for chain_key, idx_key in [('bnfChain', 'BNF'), ('nfChain', 'NF')]:
            chain_data = ctx.get(chain_key)
            # BR125: Explicit logging when chain missing — catches Kotlin wiring silent failures
            if not chain_data:
                print(f"analyze: {chain_key} missing from ctx — skipping {idx_key} candidates")
                _record_generation_skip(idx_key, chain_key, 'missing_chain', f'{chain_key} missing from ctx')
                continue
            if not chain_data.get('strikes'):
                print(f"analyze: {chain_key} has no strikes — skipping {idx_key}")
                _record_generation_skip(idx_key, chain_key, 'missing_strikes', f'{chain_key} has no strikes')
                continue
            if not chain_data.get('atm'):
                print(f"analyze: {chain_key} missing atm — skipping {idx_key}")
                _record_generation_skip(idx_key, chain_key, 'missing_atm', f'{chain_key} missing atm')
                continue
            
            spot = _latest_spot_value(idx_key)
            if spot <= 0:
                print(f"analyze: {idx_key} spot resolved to {spot} — skipping candidate generation")
                _record_generation_skip(idx_key, chain_key, 'spot_zero', f'{idx_key} spot resolved to {spot}')
                continue

            expiry_key = 'bnfExpiry' if idx_key == 'BNF' else 'nfExpiry'
            expiry = ctx.get(expiry_key, '')
            cands, rejected = generate_candidates(chain_data, spot, idx_key, expiry, cur_vix, active_bias, iv_pctl, ctx, regime)
            all_cands.extend(cands)
            all_rejected.extend(rejected)
        result['rejected_candidates'] = all_rejected
        result['generation_skip_reasons'] = generation_skip_reasons
        result['generation_skip_reason'] = generation_skip_reasons[0] if generation_skip_reasons else None
        if not all_cands:
            no_trade_reason = (
                "All candidates rejected by the gate waterfall. "
                "No tradeable setup right now; most common causes are weak premium edge, "
                "poor credit-vs-width economics, low IV richness, or incomplete chain data."
            )
            verdict = dict(result.get('verdict') or {})
            existing_conflicts = list(verdict.get('conflicts') or [])
            existing_conflicts.append("No viable candidates passed economic and structure gates")
            verdict.update({
                'action': 'WAIT',
                'strategy': None,
                'confidence': 0,
                'urgency': 'WAIT — no viable candidates today',
                'reasoning': no_trade_reason,
                'conflicts': existing_conflicts,
            })
            result['verdict'] = verdict
            result['decisionSource'] = 'NO_CANDIDATES'
            result['decision_source'] = result['decisionSource']
            result['decisionReason'] = no_trade_reason
            result['decision_reason'] = no_trade_reason
            result['no_candidates_reason'] = no_trade_reason
            result['generated_candidates'] = []
            result['watchlist'] = []
        if all_cands:
            brain_verdict = result.get('verdict')
            stage2a_summary = _stage2a_annotate_candidates(all_cands, ctx)
            stage2a_shadow_active = stage2a_summary.get('mode') in ('shadow', 'live') and stage2a_summary.get('table_ready')
            stage2a_live_active = stage2a_summary.get('mode') == 'live' and stage2a_summary.get('table_ready')

            def _recompute_rankings(verdict_obj):
                det = rank_candidates(all_cands, _calibration, verdict_obj)
                _stage2a_stamp_candidate_ranks(det, 'deterministic_rank')
                shadow = rank_candidates(
                    all_cands,
                    _calibration,
                    verdict_obj,
                    stage2a={'ranking_active': stage2a_shadow_active}
                )
                _stage2a_stamp_candidate_ranks(shadow, 'teacher_shadow_rank')
                live = rank_candidates(
                    all_cands,
                    _calibration,
                    verdict_obj,
                    stage2a={'ranking_active': stage2a_live_active}
                )
                _stage2a_stamp_candidate_ranks(live, 'stage2a_live_rank')
                stage2a_summary['deterministic_top_candidate_id'] = det[0].get('id') if det else None
                stage2a_summary['shadow_top_candidate_id'] = shadow[0].get('id') if shadow else None
                stage2a_summary['live_top_candidate_id'] = live[0].get('id') if live else None
                stage2a_summary['shadow_changes_top'] = (
                    stage2a_summary.get('shadow_top_candidate_id') is not None
                    and stage2a_summary.get('shadow_top_candidate_id') != stage2a_summary.get('deterministic_top_candidate_id')
                )
                stage2a_summary['live_changes_top'] = stage2a_live_active and (
                    stage2a_summary.get('live_top_candidate_id') is not None
                    and stage2a_summary.get('live_top_candidate_id') != stage2a_summary.get('deterministic_top_candidate_id')
                )
                return live

            ranked = _recompute_rankings(brain_verdict)
            result['stage2a'] = stage2a_summary
            pre_watchlist = _build_watchlist_from_ranked(ranked)
            aligned_verdict = _align_verdict_to_watchlist(brain_verdict, pre_watchlist, ctx)
            if aligned_verdict is not brain_verdict:
                result['verdict'] = aligned_verdict
                brain_verdict = aligned_verdict
                ranked = _recompute_rankings(brain_verdict)

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
                            # ML enrichment uses pre-computed signals
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
                            temporal = _temporal_load_if_ready()
                            temporal_active = False
                            if temporal is not None and len(polls) >= 4:
                                p2 = temporal.blend(p2, polls[-8:])
                                temporal_active = True
                            c['p_ml']         = round(p2, 4)
                            c['mlAction']     = d2.get('action', 'WATCH')
                            c['mlRegime']     = reg2
                            c['mlEdge']       = d2.get('edge', 0.0)
                            c['mlOod']        = d2.get('ood', False)
                            c['mlOodFlag']    = d2.get('ood_flag', d2.get('ood', False))
                            c['mlOodConf']    = d2.get('ood_conf', 1.0)
                            c['mlOodWarn']    = d2.get('ood_warns', [])
                            c['mlOodBlocked'] = d2.get('ood_blocked', False)
                            c['mlUnsure']     = bool(d2.get('ml_unsure') or c['mlAction'] == 'UNSURE')
                            c['mlUnsureReason'] = d2.get('unsure_reason', [])
                            if c['mlUnsure']:
                                c['decisionSource'] = 'ML_UNSURE_FALLBACK'
                                c['decision_source'] = 'ML_UNSURE_FALLBACK'
                                c['decisionReason'] = 'ML returned UNSURE; ranking used deterministic brain rules'
                                c['decision_reason'] = 'ML returned UNSURE; ranking used deterministic brain rules'
                            else:
                                c['decisionSource'] = 'ML_ADVISORY'
                                c['decision_source'] = 'ML_ADVISORY'
                                c['decisionReason'] = 'ML annotated candidate after deterministic ranking'
                                c['decision_reason'] = 'ML annotated candidate after deterministic ranking'
                            c['mlTemporalActive'] = temporal_active
                            c['mlTemporalValAcc'] = round(float(getattr(temporal, 'val_acc', 0.0)), 4) if temporal_active else None
                        except Exception:
                            pass
                    ranked = _recompute_rankings(brain_verdict)
                except Exception:
                    pass

            # Watchlist: top 6 + diverse picks per index.
            # Final verdict must match the executable lane shown to the user.
            watchlist = _build_watchlist_from_ranked(ranked)
            final_verdict = _align_verdict_to_watchlist(result.get('verdict'), watchlist, ctx)
            if final_verdict is not result.get('verdict'):
                result['verdict'] = final_verdict
                ranked = _recompute_rankings(final_verdict)
                watchlist = _build_watchlist_from_ranked(ranked)

            for c in ranked:
                readiness = check_execution_readiness(c, result, ctx)
                c['executionReadiness'] = readiness
                c['executionReady'] = readiness.get('ready', False)
                c['executionGate'] = readiness.get('gate', 'WAIT')
            
            # Decision #19: Refresh forces for the top picks
            result["watchlist"] = update_watchlist_forces(watchlist, ctx, cur_vix, iv_pctl, regime)
            for c in result["watchlist"]:
                readiness = check_execution_readiness(c, result, ctx)
                c['executionReadiness'] = readiness
                c['executionReady'] = readiness.get('ready', False)
                c['executionGate'] = readiness.get('gate', 'WAIT')
            final_verdict = _align_verdict_to_watchlist(result.get('verdict'), result["watchlist"], ctx)
            if final_verdict is not result.get('verdict'):
                result['verdict'] = final_verdict
            result = _stage2a_apply_live_wait_guard(result, result.get("watchlist"), stage2a_summary)
            top = (result.get("watchlist") or [{}])[0] if result.get("watchlist") else {}
            if stage2a_summary.get('mode') == 'live':
                if stage2a_summary.get('hard_wait_triggered'):
                    top['decisionSource'] = 'TEACHER_ONLY'
                    top['decision_source'] = 'TEACHER_ONLY'
                    top['decisionReason'] = 'Stage 2A teacher guard forced WAIT because no candidate had positive covered expectancy.'
                    top['decision_reason'] = top['decisionReason']
                else:
                    top['decisionSource'] = 'TEACHER_ONLY'
                    top['decision_source'] = 'TEACHER_ONLY'
                    top['decisionReason'] = 'Teacher expectancy ranking applied in guarded Stage 2A mode'
                    top['decision_reason'] = top['decisionReason']
            result['decisionSource'] = top.get('decisionSource', 'DEFAULT_BRAIN_MATH')
            result['decision_source'] = result['decisionSource']
            result['decisionReason'] = top.get('decisionReason', 'top decision ranked by deterministic brain rules')
            result['decision_reason'] = result['decisionReason']
            # BR129: Cap at 30 for PWA consumption
            result["generated_candidates"] = ranked[:30]
            result["rejected_candidates"] = all_rejected

            by_index = {"BNF": 0, "NF": 0}
            by_lane = {}
            for c in all_cands:
                idx = c.get("index")
                lane = c.get("lane")
                if idx in by_index:
                    by_index[idx] += 1
                if lane:
                    by_lane[lane] = by_lane.get(lane, 0) + 1

            rejected_by_index = {"BNF": 0, "NF": 0}
            rejected_by_stage = {}
            for c in all_rejected:
                idx = c.get("index")
                stage = c.get("rejection_stage")
                if idx in rejected_by_index:
                    rejected_by_index[idx] += 1
                if stage:
                    rejected_by_stage[stage] = rejected_by_stage.get(stage, 0) + 1

            result["candidate_stats"] = {
                "total": len(all_cands),
                "rejected": len(all_rejected),
                "ranked": len(ranked),
                "watchlist": len(watchlist),
                "by_index": by_index,
                "rejected_by_index": rejected_by_index,
                "rejected_by_stage": rejected_by_stage,
                "by_lane": by_lane,
                "by_type": {}
            }
            for c in all_cands:
                t = c['type']
                result["candidate_stats"]["by_type"][t] = result["candidate_stats"]["by_type"].get(t, 0) + 1
    except Exception as e:
        result["candidate_error"] = str(e)

    # BR119: Single source of truth. Sophisticated generator above handles all logic.
    # Phase 3 fallback (A3) fully retired.
    # BR130: Market Radar Engine v2.2.8 finalized.
    # TASK 5.2 — embed trace in result
    if ctx.get('_trace') is not None:
        result['_trace'] = ctx['_trace']
    # Use _safe_json to handle any nan/inf produced by BS math edge cases
    # Phase F.1 — emit structured gap info for PWA render
    gap = ctx.get('gap') or {}
    result['gapInfo'] = {
        'gap': gap.get('bnf', {}).get('points', 0),
        'pct': gap.get('bnf', {}).get('pct', 0),
        'sigma': gap.get('bnf', {}).get('sigma', 0),
        'type': gap.get('bnf', {}).get('type', 'NORMAL'),
    }
    result['nfGapInfo'] = {
        'gap': gap.get('nf', {}).get('points', 0),
        'pct': gap.get('nf', {}).get('pct', 0),
        'sigma': gap.get('nf', {}).get('sigma', 0),
        'type': gap.get('nf', {}).get('type', 'NORMAL'),
    }
    result['learnedBranches'] = {
        'approved_count': len(_normalize_approved_branch_proposals(ctx)),
        'matched_by_index': ctx.get('_learned_branch_matches', {}),
    }

    # Phase A agent: deterministic explanation/audit JSON only. This must run
    # after candidates/watchlist/gaps/positions are populated.
    try:
        result["agent"] = build_explanation_audit_agent(result, ctx, open_trades)
    except Exception as e:
        result["agent_error"] = str(e)

    # #27 — Evaluate alerts after watchlist generation. Earlier placement used
    # an empty watchlist and silently disabled candidate/watchlist alerts.
    try:
        result['alerts'] = evaluate_alerts(
            open_trades=open_trades,
            watchlist=result.get('watchlist') or [],
            result=result,
            ctx=ctx,
        )
    except Exception as e:
        result['alerts'] = []
        result['alert_error'] = str(e)

    try:
        result['elephant_fact_pack'] = build_elephant_fact_pack(
            result=result,
            ctx=ctx,
            polls=polls,
            calibration=_calibration,
            closed_trades=closed_trades,
        )
    except Exception as e:
        result['elephant_fact_pack_error'] = str(e)

    out_str, _ = _safe_json(result)
    return out_str



# ═══════════════════════════════════════════════════════════════════════════
# TASK 5.7 (Ship 2 Phase 0.3) — replay() ENDPOINT
# Re-runs analyze() against stored inputs for regression verification.
# Depends on: _new_trace, _calibration, _cal_signature, BRAIN_VERSION,
#             TRACE_SCHEMA_VERSION, analyze() — all defined above.
# ═══════════════════════════════════════════════════════════════════════════


# Fields compared when expected_baseline is provided.
# Direction/action/strategy: exact match. confidence/bull/bear: tolerance.
# Excluded: reasoning, urgency (non-deterministic), market[] (order-sensitive),
# effective_bias (derived), conflicts list (exact match separately).
REPLAY_COMPARE_EXACT = ('direction', 'action', 'strategy')
REPLAY_COMPARE_TOLERANT = ('confidence', 'bull', 'bear')
REPLAY_COMPARE_LIST_EXACT = ('conflicts',)
REPLAY_DEFAULT_TOLERANCE = {'confidence': 0, 'bull': 0.01, 'bear': 0.01}


def replay(inputs, calibration_override=None, expected_baseline=None,
           tolerance=None, source_tag='replay'):
    """
    Re-run analyze() against stored inputs for regression verification.

    Parameters
    ----------
    inputs : dict
        Fixture format: has 'inputs' sub-dict with poll_json, ctx_json,
        baseline_json, closed_trades_json, open_trades_json.
        Raw format: has poll_json, trades_json, baseline_json, open_trades_json,
        candidates_json, strike_oi_json, context_json at top level.
    calibration_override : dict | None
        If provided, overrides _calibration global during replay.
        None → rebuild from trades_json normally.
        {} → run with no calibration.
    expected_baseline : dict | None
        If provided, compares result.verdict to expected_baseline.verdict.
        Shape matches fixture_X.baseline.json (has 'verdict' key).
    tolerance : dict | None
        Field tolerances. Default REPLAY_DEFAULT_TOLERANCE.
    source_tag : str
        trace.meta.source value. Default 'replay'.

    Returns
    -------
    dict with keys 'result' (analyze dict) and 'replay_meta'.

    Raises
    ------
    ValueError: malformed inputs or expected_baseline.
    TypeError: wrong type for override or tolerance.
    """
    import datetime as _dt

    global _calibration, _cal_signature

    # ─── Parameter validation ───
    if not isinstance(inputs, dict):
        raise TypeError("replay: inputs must be a dict")
    if calibration_override is not None and not isinstance(calibration_override, dict):
        raise TypeError("replay: calibration_override must be dict or None")
    if tolerance is not None and not isinstance(tolerance, dict):
        raise TypeError("replay: tolerance must be dict or None")
    if expected_baseline is not None:
        if not isinstance(expected_baseline, dict):
            raise TypeError("replay: expected_baseline must be dict or None")
        if 'verdict' not in expected_baseline:
            raise ValueError("replay: expected_baseline missing 'verdict' key")

    # ─── Format detection and normalization to 7 analyze() args ───
    if 'inputs' in inputs and isinstance(inputs.get('inputs'), dict):
        input_format = 'fixture'
        fx_inputs = inputs['inputs']
        poll_json     = fx_inputs.get('poll_json')
        trades_json   = fx_inputs.get('closed_trades_json', '[]')
        baseline_json = fx_inputs.get('baseline_json', '{}')
        opentrades_js = fx_inputs.get('open_trades_json', '[]')
        candidates_js = fx_inputs.get('candidates_json', '[]')
        strike_oi_js  = fx_inputs.get('strike_oi_json', '{}')
        context_json  = fx_inputs.get('ctx_json', '{}')
        if poll_json is None:
            raise ValueError("replay: fixture.inputs.poll_json missing")
    else:
        input_format = 'raw'
        poll_json     = inputs.get('poll_json')
        trades_json   = inputs.get('trades_json', '[]')
        baseline_json = inputs.get('baseline_json', '{}')
        opentrades_js = inputs.get('open_trades_json', '[]')
        candidates_js = inputs.get('candidates_json', '[]')
        strike_oi_js  = inputs.get('strike_oi_json', '{}')
        context_json  = inputs.get('context_json', '{}')
        if poll_json is None:
            raise ValueError("replay: raw.poll_json missing")

    # ─── Inject _debug + source override into context ───
    try:
        ctx_dict = json.loads(context_json) if context_json else {}
    except (ValueError, TypeError):
        raise ValueError("replay: context_json is not valid JSON")
    ctx_dict['_debug'] = True
    ctx_dict['_trace_source_override'] = source_tag
    context_json = json.dumps(ctx_dict)

    # ─── Calibration override (save, set, restore in finally) ───
    _saved_cal = _calibration
    _saved_sig = _cal_signature
    override_used = calibration_override is not None

    errors = []
    result_dict = None
    try:
        if override_used:
            _calibration = calibration_override
            _cal_signature = '__REPLAY_OVERRIDE__'

        try:
            result_str = analyze(poll_json, trades_json, baseline_json,
                                 opentrades_js, candidates_js, strike_oi_js,
                                 context_json)
            result_dict = json.loads(result_str)
        except Exception as e:
            errors.append({'where': 'analyze', 'type': type(e).__name__, 'msg': str(e)})
    finally:
        _calibration = _saved_cal
        _cal_signature = _saved_sig

    # ─── Build replay_meta ───
    ist = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
    replay_meta = {
        'source_tag': source_tag,
        'brain_version': BRAIN_VERSION,
        'trace_schema_version': TRACE_SCHEMA_VERSION,
        'calibration_override_used': override_used,
        'replayed_at_ist': _dt.datetime.now(ist).isoformat(),
        'input_format': input_format,
        'errors': errors,
    }

    # ─── Version mismatch detection (warning only, per D7) ───
    if expected_baseline is not None:
        base_ver = expected_baseline.get('meta', {}).get('brain_version')
        if base_ver is not None and base_ver != BRAIN_VERSION:
            replay_meta['version_mismatch'] = {
                'baseline_version': base_ver,
                'current_version': BRAIN_VERSION,
                'warning': 'Baseline from older brain. Mismatches may reflect code changes, not regressions.'
            }

    # ─── Diff computation (only if baseline provided AND analyze succeeded) ───
    if expected_baseline is not None and result_dict is not None:
        tol = dict(REPLAY_DEFAULT_TOLERANCE)
        if tolerance:
            tol.update(tolerance)
        exp_verdict = expected_baseline['verdict']
        act_verdict = (result_dict or {}).get('verdict') or {}
        mismatches = []

        for field in REPLAY_COMPARE_EXACT:
            if exp_verdict.get(field) != act_verdict.get(field):
                mismatches.append({
                    'field': field,
                    'expected': exp_verdict.get(field),
                    'actual': act_verdict.get(field)
                })

        for field in REPLAY_COMPARE_TOLERANT:
            e_val = exp_verdict.get(field)
            a_val = act_verdict.get(field)
            if e_val is None and a_val is None:
                continue
            if e_val is None or a_val is None:
                mismatches.append({
                    'field': field, 'expected': e_val, 'actual': a_val,
                    'reason': 'one_side_none'
                })
                continue
            try:
                diff = abs(float(a_val) - float(e_val))
            except (TypeError, ValueError):
                mismatches.append({
                    'field': field, 'expected': e_val, 'actual': a_val,
                    'reason': 'non_numeric'
                })
                continue
            allowed = tol.get(field, 0)
            if diff > allowed:
                mismatches.append({
                    'field': field, 'expected': e_val, 'actual': a_val,
                    'diff': round(diff, 4), 'tolerance': allowed
                })

        for field in REPLAY_COMPARE_LIST_EXACT:
            e_list = exp_verdict.get(field) or []
            a_list = act_verdict.get(field) or []
            if sorted(e_list) != sorted(a_list):
                mismatches.append({
                    'field': field, 'expected': e_list, 'actual': a_list
                })

        replay_meta['diff'] = {
            'match': len(mismatches) == 0,
            'fields_checked': (len(REPLAY_COMPARE_EXACT)
                               + len(REPLAY_COMPARE_TOLERANT)
                               + len(REPLAY_COMPARE_LIST_EXACT)),
            'mismatches': mismatches,
            'tolerance_applied': tol,
        }

    return {'result': result_dict, 'replay_meta': replay_meta}


# ═══════════════════════════════════════════════════════════════
# ML ARCHITECTURE V2 — Poll Snapshot & Labeling
# ═══════════════════════════════════════════════════════════════

def _is_labelable(result, ctx):
    """Checks if the poll is an actionable recommendation worth labeling."""
    verdict = result.get('verdict', {})

    if verdict.get('action') in ('WAIT', 'STOP', None):
        return False
    if verdict.get('confidence', 0) < 35:
        return False

    executable = [c for c in result.get('watchlist', [])
                  if isinstance(c, dict) and not c.get('capitalBlocked') and c.get('type')]
    if len(executable) == 0:
        return False

    mins_since_open = ctx.get('mins_since_open', ctx.get('minsSinceOpen', 0)) or 0
    try:
        mins_since_open = int(float(mins_since_open))
    except Exception:
        mins_since_open = 0
    if mins_since_open < 15 or mins_since_open > 360:
        return False

    return True


def _bridge_json_obj(value):
    """Bridge helper: accept JSON string or dict/list and return parsed object."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return value


def _compute_signal_independence(result, ctx):
    groups = set()
    details = []
    if result.get('pcrContext') or result.get('bnfProfile') or result.get('nfProfile'):
        groups.add('oi')
        details.append('oi-derived')
    if result.get('gapInfo') or result.get('nfGapInfo') or result.get('overnightDelta') or result.get('sessionTrajectory'):
        groups.add('price')
        details.append('price-derived')
    if result.get('institutionalRegime') or ctx.get('fiiCash') is not None or ctx.get('diiCash') is not None:
        groups.add('macro')
        details.append('macro')
    if result.get('regime') or result.get('marketPhase') or result.get('rangeSigma') is not None:
        groups.add('structural')
        details.append('structural')
    base_score = len(groups) / 4.0
    if 'structural' in groups:
        base_score += 0.1
    return {
        'score': round(min(1.0, base_score), 3),
        'groups': sorted(groups),
        'details': details,
    }


def _candidate_view(c):
    return {
        'id': c.get('id'),
        'type': c.get('type'),
        'index': c.get('index'),
        'lane': c.get('lane'),
        'width': c.get('width'),
        'expiry': c.get('expiry'),
        'legs': c.get('legs'),
        'legCount': c.get('legCount', len(c.get('legs') or [])),
        'leg_schema_version': c.get('leg_schema_version'),
        'candidate_schema_version': c.get('candidate_schema_version'),
        'sellStrike': c.get('sellStrike'),
        'sellType': c.get('sellType'),
        'buyStrike': c.get('buyStrike'),
        'buyType': c.get('buyType'),
        'sellStrike2': c.get('sellStrike2'),
        'sellType2': c.get('sellType2'),
        'buyStrike2': c.get('buyStrike2'),
        'buyType2': c.get('buyType2'),
        'netPremium': c.get('netPremium'),
        'maxProfit': c.get('maxProfit'),
        'maxLoss': c.get('maxLoss'),
        'isCredit': c.get('isCredit'),
        'lotSize': c.get('lotSize'),
        'estCost': c.get('estCost'),
        'capitalBlocked': c.get('capitalBlocked'),
        'executionReady': c.get('executionReady'),
        'executionGate': c.get('executionGate'),
        'entryAction': c.get('entryAction'),
        'directionSafe': c.get('directionSafe'),
        'brainScore': c.get('brainScore'),
        'p_ml': c.get('p_ml'),
        'mlAction': c.get('mlAction'),
        'mlEdge': c.get('mlEdge'),
        'mlOod': c.get('mlOod'),
        'mlOodFlag': c.get('mlOodFlag'),
        'mlOodConf': c.get('mlOodConf'),
        'mlOodBlocked': c.get('mlOodBlocked'),
        'ivRichness': c.get('ivRichness'),
        'creditWidthRatio': c.get('creditWidthRatio'),
        'sigmaOTM': c.get('sigmaOTM'),
        'premiumEdge': c.get('premiumEdge'),
        'deterministic_rank': c.get('deterministic_rank'),
        'teacher_shadow_rank': c.get('teacher_shadow_rank'),
        'stage2a_live_rank': c.get('stage2a_live_rank'),
        'teacher_bucket_key': c.get('teacher_bucket_key'),
        'teacher_bucket_n': c.get('teacher_bucket_n'),
        'teacher_r_score': c.get('teacher_r_score'),
        'teacher_success_rate_pct': c.get('teacher_success_rate_pct'),
        'teacher_coverage': c.get('teacher_coverage'),
        'teacher_recommendable': c.get('teacher_recommendable'),
        'realMargin': c.get('realMargin'),
        'upstoxRequiredMargin': c.get('upstoxRequiredMargin'),
        'upstoxFinalMargin': c.get('upstoxFinalMargin'),
        'upstoxSpanMargin': c.get('upstoxSpanMargin'),
        'upstoxExposureMargin': c.get('upstoxExposureMargin'),
        'upstoxNetBuyPremium': c.get('upstoxNetBuyPremium'),
        'marginQuoteStatus': c.get('marginQuoteStatus'),
        'marginQuoteSource': c.get('marginQuoteSource'),
        'marginQuotedAt': c.get('marginQuotedAt'),
        'marginRequestUrl': c.get('marginRequestUrl'),
        'marginQuoteError': c.get('marginQuoteError'),
        'marginQuote': c.get('marginQuote'),
    }


def _snapshot_teaching_band(index_key, chain, spot, vix, expiry):
    if not isinstance(chain, dict):
        return []
    strikes = chain.get('strikes') or {}
    atm = chain.get('atm')
    all_strikes = chain.get('allStrikes') or sorted(int(k) for k in strikes.keys())
    if not strikes or not atm or len(all_strikes) < 2:
        return []
    step = all_strikes[1] - all_strikes[0]
    lo = atm - (8 * step)
    hi = atm + (8 * step)
    band_rows = []
    for strike in all_strikes:
        if strike < lo or strike > hi:
            continue
        strike_row = strikes.get(str(int(strike))) or strikes.get(strike) or {}
        for opt_type in ('CE', 'PE'):
            leg_data = strike_row.get(opt_type) or {}
            if not leg_data:
                continue
            row = _build_leg_record(strike, opt_type, 'OBSERVE', expiry, leg_data, atm)
            row.update({
                'index': index_key,
                'atm_strike': atm,
                'spot': spot,
                'vix': vix,
            })
            band_rows.append(row)
    return band_rows


def _compact_candidate_trace(trace_root):
    if not isinstance(trace_root, dict):
        return {'meta': {}, 'by_index': {}, 'rejected_count': 0, 'accepted_count': 0}

    def _bump(counter, key):
        if key is None:
            key = 'unknown'
        key = str(key)
        counter[key] = int(counter.get(key, 0)) + 1

    def _sample_attempt(a):
        return {
            'stype': a.get('stype'),
            'result': a.get('result'),
            'stage': a.get('stage'),
            'reason': a.get('reason'),
            'width': a.get('width'),
            'sell': a.get('sell'),
            'buy': a.get('buy'),
            'sell_call': a.get('sell_call'),
            'sell_put': a.get('sell_put'),
            'iv_richness': a.get('iv_richness'),
            'credit_width_ratio': a.get('credit_width_ratio'),
            'premium_edge': a.get('premium_edge'),
        }

    out = {
        'meta': dict(trace_root.get('meta') or {}),
        'by_index': {},
        'rejected_count': 0,
        'accepted_count': 0,
    }
    candidates = trace_root.get('candidates') or {}
    for index_key in ('BNF', 'NF'):
        row = candidates.get(index_key)
        if not isinstance(row, dict):
            continue
        attempts = row.get('attempts') or []
        compact_attempts = []
        attempt_stats = {
            'total': 0,
            'accepted': 0,
            'rejected': 0,
            'by_result': {},
            'by_stage': {},
            'by_reason': {},
            'by_strategy': {},
        }
        for a in attempts:
            if not isinstance(a, dict):
                continue
            attempt_stats['total'] += 1
            result = a.get('result') or 'unknown'
            _bump(attempt_stats['by_result'], result)
            _bump(attempt_stats['by_stage'], a.get('stage'))
            _bump(attempt_stats['by_reason'], a.get('reason'))
            _bump(attempt_stats['by_strategy'], a.get('stype'))
            if len(compact_attempts) < TRACE_ATTEMPT_SAMPLE_CAP:
                compact_attempts.append(_sample_attempt(a))
            if result == 'rejected':
                attempt_stats['rejected'] += 1
                out['rejected_count'] += 1
            elif result == 'accepted':
                attempt_stats['accepted'] += 1
                out['accepted_count'] += 1
        out['by_index'][index_key] = {
            'config': dict(row.get('config') or {}),
            'summary': dict(row.get('summary') or {}),
            'varsity': dict(row.get('varsity') or {}),
            'attempt_stats': attempt_stats,
            'attempt_sample_cap': TRACE_ATTEMPT_SAMPLE_CAP,
            'attempts_truncated': len(attempts) > TRACE_ATTEMPT_SAMPLE_CAP,
            'attempts': compact_attempts,
        }
    return out


def _compact_rejected_candidates(rejected_candidates):
    if not isinstance(rejected_candidates, list):
        return [], {
            'total': 0,
            'sample_cap': REJECTED_CANDIDATE_SAMPLE_CAP,
            'truncated': False,
            'by_stage': {},
            'by_reason': {},
            'by_strategy': {},
            'by_lane': {},
        }

    def _bump(counter, key):
        if key is None:
            key = 'unknown'
        key = str(key)
        counter[key] = int(counter.get(key, 0)) + 1

    def _sample_row(row):
        return {
            'index': row.get('index'),
            'lane': row.get('lane'),
            'strategy_type': row.get('strategy_type'),
            'expiry': row.get('expiry'),
            'width': row.get('width'),
            'is_credit': row.get('is_credit'),
            'netPremium': row.get('netPremium'),
            'maxProfit': row.get('maxProfit'),
            'maxLoss': row.get('maxLoss'),
            'tDTE': row.get('tDTE'),
            'ivRichness': row.get('ivRichness'),
            'creditWidthRatio': row.get('creditWidthRatio'),
            'sigmaOTM': row.get('sigmaOTM'),
            'rejection_stage': row.get('rejection_stage'),
            'rejection_reason': row.get('rejection_reason'),
            'sellStrike': row.get('sellStrike'),
            'sellType': row.get('sellType'),
            'buyStrike': row.get('buyStrike'),
            'buyType': row.get('buyType'),
        }

    stats = {
        'total': 0,
        'sample_cap': REJECTED_CANDIDATE_SAMPLE_CAP,
        'truncated': False,
        'by_stage': {},
        'by_reason': {},
        'by_strategy': {},
        'by_lane': {},
    }
    sample = []
    for row in rejected_candidates:
        if not isinstance(row, dict):
            continue
        stats['total'] += 1
        _bump(stats['by_stage'], row.get('rejection_stage'))
        _bump(stats['by_reason'], row.get('rejection_reason'))
        _bump(stats['by_strategy'], row.get('strategy_type'))
        _bump(stats['by_lane'], row.get('lane'))
        if len(sample) < REJECTED_CANDIDATE_SAMPLE_CAP:
            sample.append(_sample_row(row))
    stats['truncated'] = stats['total'] > len(sample)
    return sample, stats


def _full_rejected_candidate_view(row):
    if not isinstance(row, dict):
        return None
    return {
        'candidate_schema_version': row.get('candidate_schema_version'),
        'leg_schema_version': row.get('leg_schema_version'),
        'index': row.get('index'),
        'lane': row.get('lane'),
        'strategy_type': row.get('strategy_type'),
        'expiry': row.get('expiry'),
        'width': row.get('width'),
        'is_credit': row.get('is_credit'),
        'netPremium': row.get('netPremium'),
        'maxProfit': row.get('maxProfit'),
        'maxLoss': row.get('maxLoss'),
        'tDTE': row.get('tDTE'),
        'ivRichness': row.get('ivRichness'),
        'creditWidthRatio': row.get('creditWidthRatio'),
        'sigmaOTM': row.get('sigmaOTM'),
        'rejection_stage': row.get('rejection_stage'),
        'rejection_reason': row.get('rejection_reason'),
        'sellStrike': row.get('sellStrike'),
        'sellType': row.get('sellType'),
        'buyStrike': row.get('buyStrike'),
        'buyType': row.get('buyType'),
        'sellStrike2': row.get('sellStrike2'),
        'sellType2': row.get('sellType2'),
        'buyStrike2': row.get('buyStrike2'),
        'buyType2': row.get('buyType2'),
        'legs': row.get('legs') or [],
    }


def _audit_snapshot_replayability(snapshot_context, ctx):
    """Log exact-replay capture regressions without changing live behavior."""
    warns = []
    try:
        snapshot_context = snapshot_context if isinstance(snapshot_context, dict) else {}
        ctx = ctx if isinstance(ctx, dict) else {}
        for idx_key in ('nfChain', 'bnfChain'):
            chain = ctx.get(idx_key) or {}
            strikes = chain.get('strikes') or {}
            if not strikes:
                warns.append(f"{idx_key}.strikes empty")
                continue
            if not chain.get('atm'):
                warns.append(f"{idx_key}.atm missing")
            if chain.get('atmIv') in (None, 0):
                warns.append(f"{idx_key}.atmIv missing/zero")
            sample = next(iter(strikes.values()), {}) or {}
            for side in ('CE', 'PE'):
                leg = sample.get(side) or {}
                for f in ('bid', 'ask', 'ltp', 'iv', 'oi'):
                    if f not in leg:
                        warns.append(f"{idx_key} leg missing '{f}'")
                        break
        if not snapshot_context.get('snapshot_generated_candidates') and not snapshot_context.get('snapshot_generation_skip_reasons'):
            warns.append("generated_candidates empty without skip_reason")
        if 'snapshot_rejected_candidates_full' not in snapshot_context:
            warns.append("rejected_candidates_full absent")
    except Exception as exc:
        warns.append(f"replay_audit_exception={exc}")
    if warns:
        try:
            print(f"REPLAY_CAPTURE_WARN poll={snapshot_context.get('snapshot_latest_poll',{}).get('t')} issues={warns}")
        except Exception:
            pass


def take_poll_snapshot(result, ctx, polls):
    """Takes snapshot including surfaced and full generated candidates.
    primary_candidate_json = the #1 surfaced recommendation (ML truth target).
    top_candidates_json   = top watchlist candidates kept for compatibility.
    context_json         = carries full generated candidate set for evaluator/research."""
    result = _bridge_json_obj(result) or {}
    ctx = _bridge_json_obj(ctx) or {}
    polls = _bridge_json_obj(polls) or []

    watchlist = result.get('watchlist', [])
    generated_candidates = result.get('generated_candidates', [])
    rejected_candidates = result.get('rejected_candidates', [])
    top_5_nf = [c for c in watchlist if isinstance(c, dict) and c.get('index') == 'NF'][:5]
    top_5_bnf = [c for c in watchlist if isinstance(c, dict) and c.get('index') == 'BNF'][:5]
    top_5_cands = (top_5_nf + top_5_bnf)[:10]

    top_cand = top_5_cands[0] if top_5_cands else None
    verdict = result.get('verdict', {})
    latest_poll = polls[-1] if isinstance(polls, list) and polls else {}
    bnf_profile = result.get('bnfProfile') or {}
    nf_profile = result.get('nfProfile') or {}
    signal_independence = _compute_signal_independence(result, ctx)
    if isinstance(verdict, dict):
        verdict['signal_independence_score'] = signal_independence.get('score')
        verdict['signal_independence_groups'] = signal_independence.get('groups')

    def _oi_window_velocity(rows, call_key, put_key, window=6):
        if not isinstance(rows, list) or len(rows) < 2:
            return None
        recent = rows[-window:]
        first = recent[0] or {}
        last = recent[-1] or {}
        t0 = (first.get(call_key) or 0) + (first.get(put_key) or 0)
        t1 = (last.get(call_key) or 0) + (last.get(put_key) or 0)
        if t0 <= 0:
            return None
        try:
            return round((t1 - t0) / t0 * 100.0, 2)
        except Exception:
            return None

    bnf_oi_vel_pct = _oi_window_velocity(polls, 'bnfCOI', 'bnfPOI')
    nf_oi_vel_pct = _oi_window_velocity(polls, 'nfCOI', 'nfPOI')

    # Generate Recommendation ID
    band = f"{(verdict.get('confidence', 0) // 10) * 10}"
    key = f"{verdict.get('action')}|{verdict.get('strategy')}|{top_cand.get('sellStrike') if top_cand else ''}|{verdict.get('direction')}|{band}"
    rec_id = hashlib.md5(key.encode()).hexdigest()[:8]

    # Clean primary candidate (surfaced recommendation — the ML truth target)
    primary = {}
    if top_cand:
        primary = {
            'id': top_cand.get('id'),
            'type': top_cand.get('type'),
            'index': top_cand.get('index'),
            'lane': top_cand.get('lane'),
            'legs': top_cand.get('legs'),
            'legCount': top_cand.get('legCount', len(top_cand.get('legs') or [])),
            'leg_schema_version': top_cand.get('leg_schema_version'),
            'candidate_schema_version': top_cand.get('candidate_schema_version'),
            'sellStrike': top_cand.get('sellStrike'),
            'sellType': top_cand.get('sellType'),
            'buyStrike': top_cand.get('buyStrike'),
            'buyType': top_cand.get('buyType'),
            'sellStrike2': top_cand.get('sellStrike2'),
            'sellType2': top_cand.get('sellType2'),
            'buyStrike2': top_cand.get('buyStrike2'),
            'buyType2': top_cand.get('buyType2'),
            'netPremium': top_cand.get('netPremium'),
            'maxProfit': top_cand.get('maxProfit'),
            'maxLoss': top_cand.get('maxLoss'),
            'estCost': top_cand.get('estCost'),
            'lotSize': top_cand.get('lotSize'),
            'expiry': top_cand.get('expiry'),
            'width': top_cand.get('width'),
            'deterministic_rank': top_cand.get('deterministic_rank'),
            'teacher_shadow_rank': top_cand.get('teacher_shadow_rank'),
            'stage2a_live_rank': top_cand.get('stage2a_live_rank'),
            'teacher_bucket_key': top_cand.get('teacher_bucket_key'),
            'teacher_bucket_n': top_cand.get('teacher_bucket_n'),
            'teacher_r_score': top_cand.get('teacher_r_score'),
            'teacher_success_rate_pct': top_cand.get('teacher_success_rate_pct'),
            'teacher_coverage': top_cand.get('teacher_coverage'),
            'teacher_recommendable': top_cand.get('teacher_recommendable'),
        }

    # Clean top-5 candidates for secondary research only
    clean_cands = []
    for c in top_5_cands:
        clean_cands.append(_candidate_view(c))
    clean_top_5_nf = [_candidate_view(c) for c in top_5_nf]
    clean_top_5_bnf = [_candidate_view(c) for c in top_5_bnf]

    # Clean ALL generated candidates for offline research/retraining.
    # Stored inside context_json so this works even on older DB schemas.
    clean_generated = []
    if isinstance(generated_candidates, list):
        for c in generated_candidates:
            if not isinstance(c, dict):
                continue
            clean_generated.append(_candidate_view(c))
    clean_rejected, rejected_stats = _compact_rejected_candidates(rejected_candidates)
    clean_rejected_full = []
    if isinstance(rejected_candidates, list):
        for c in rejected_candidates:
            clean = _full_rejected_candidate_view(c)
            if clean is not None:
                clean_rejected_full.append(clean)

    def _candidate_leg_ledger(cand):
        if not isinstance(cand, dict):
            return None
        legs = []

        def _append_leg(prefix, suffix=''):
            strike = cand.get(f'{prefix}Strike{suffix}')
            opt_type = cand.get(f'{prefix}Type{suffix}')
            if not strike or not opt_type:
                return
            leg = {
                'side': prefix,
                'strike': strike,
                'option_type': opt_type,
                'entry_ltp': cand.get(f'{prefix}LTP{suffix}'),
            }
            if suffix:
                leg['suffix'] = suffix
            legs.append(leg)

        _append_leg('sell')
        _append_leg('buy')
        _append_leg('sell', '2')
        _append_leg('buy', '2')

        return {
            'candidate_id': cand.get('id'),
            'strategy_type': cand.get('type'),
            'index': cand.get('index'),
            'lane': cand.get('lane'),
            'expiry': cand.get('expiry'),
            'trade_mode': ctx.get('tradeMode') or ctx.get('trade_mode'),
            'legs': cand.get('legs') or legs,
            'leg_schema_version': cand.get('leg_schema_version'),
            'candidate_schema_version': cand.get('candidate_schema_version'),
        }

    evaluation_ledger = []
    for c in generated_candidates if isinstance(generated_candidates, list) else []:
        ledger_row = _candidate_leg_ledger(c)
        if ledger_row and ledger_row.get('candidate_id'):
            evaluation_ledger.append(ledger_row)

    market_forces = {
        'poll_count': len(polls) if isinstance(polls, list) else 0,
        'bnf_spot': latest_poll.get('bnfSpot') or latest_poll.get('bnf') or latest_poll.get('BNF'),
        'nf_spot': latest_poll.get('nfSpot') or latest_poll.get('nf') or latest_poll.get('NF'),
        'vix': latest_poll.get('vix') or latest_poll.get('VIX'),
        'bnf_pcr': latest_poll.get('bnfPcr') or latest_poll.get('pcr'),
        'nf_pcr': latest_poll.get('nfPcr'),
        'breadth': latest_poll.get('breadth') or latest_poll.get('nf50Breadth'),
        'range_sigma': result.get('rangeSigma') or verdict.get('range_sigma') or verdict.get('rangeSigma'),
        'regime': result.get('regime') or verdict.get('regime'),
        'bnf_total_call_oi': latest_poll.get('bnfCOI'),
        'bnf_total_put_oi': latest_poll.get('bnfPOI'),
        'nf_total_call_oi': latest_poll.get('nfCOI'),
        'nf_total_put_oi': latest_poll.get('nfPOI'),
        'bnf_oi_velocity_pct': bnf_oi_vel_pct,
        'nf_oi_velocity_pct': nf_oi_vel_pct,
        'bnf_profile_oi_velocity_l': bnf_profile.get('oiVelocity'),
        'nf_profile_oi_velocity_l': nf_profile.get('oiVelocity'),
    }
    poll_summary = {
        'poll_count': len(polls) if isinstance(polls, list) else 0,
        'latest_time': latest_poll.get('t') or latest_poll.get('time'),
        'entry_window_active': ctx.get('entry_window_active'),
        'trade_mode': ctx.get('tradeMode') or ctx.get('trade_mode'),
        'watchlist_count': len(watchlist) if isinstance(watchlist, list) else 0,
        'generated_count': len(generated_candidates) if isinstance(generated_candidates, list) else 0,
        'rejected_count': len(rejected_candidates) if isinstance(rejected_candidates, list) else 0,
        'top_candidate_type': top_cand.get('type') if top_cand else None,
        'signal_independence_score': signal_independence.get('score'),
        'is_labelable': _is_labelable(result, ctx),
    }

    # Enrich context payload with full market/candidate capture for post-mortem ML analysis.
    # Keep existing keys intact; append under snapshot_* namespaces.
    snapshot_context = dict(ctx) if isinstance(ctx, dict) else {}
    snapshot_context['snapshot_generated_candidates'] = clean_generated
    snapshot_context['snapshot_rejected_candidates'] = clean_rejected
    snapshot_context['snapshot_rejected_candidates_full'] = clean_rejected_full
    snapshot_context['snapshot_rejected_candidate_stats'] = rejected_stats
    snapshot_context['snapshot_brain_notification'] = result.get('brain_notification') if isinstance(result.get('brain_notification'), dict) else {}
    snapshot_context['snapshot_watchlist'] = clean_cands
    snapshot_context['snapshot_generation_skip_reason'] = result.get('generation_skip_reason')
    snapshot_context['snapshot_generation_skip_reasons'] = result.get('generation_skip_reasons') or []
    snapshot_context['snapshot_evaluation_legs'] = evaluation_ledger
    snapshot_context['snapshot_latest_poll'] = latest_poll if isinstance(latest_poll, dict) else {}
    snapshot_context['top_5_nf'] = clean_top_5_nf
    snapshot_context['top_5_bnf'] = clean_top_5_bnf
    snapshot_context['signal_independence'] = signal_independence
    snapshot_context['candidate_generation_trace'] = _compact_candidate_trace(result.get('_trace'))
    snapshot_context['teaching_snapshot_staging'] = {
        'bnf': _snapshot_teaching_band(
            'BNF',
            ctx.get('bnfChain') or {},
            latest_poll.get('bnfSpot') or latest_poll.get('bnf') or latest_poll.get('BNF'),
            latest_poll.get('vix') or latest_poll.get('VIX') or ctx.get('vix'),
            ctx.get('expiry_bnf'),
        ),
        'nf': _snapshot_teaching_band(
            'NF',
            ctx.get('nfChain') or {},
            latest_poll.get('nfSpot') or latest_poll.get('nf') or latest_poll.get('NF'),
            latest_poll.get('vix') or latest_poll.get('VIX') or ctx.get('vix'),
            ctx.get('expiry_nf'),
        ),
    }
    snapshot_context['snapshot_market_profiles'] = {
        'bnfProfile': bnf_profile if isinstance(bnf_profile, dict) else {},
        'nfProfile': nf_profile if isinstance(nf_profile, dict) else {},
        'pcrContext': result.get('pcrContext'),
        'marketPhase': result.get('marketPhase'),
        'effective_bias': result.get('effective_bias'),
        'regime': result.get('regime'),
        'rangeSigma': result.get('rangeSigma'),
    }
    snapshot_context['snapshot_stage2a'] = result.get('stage2a') if isinstance(result.get('stage2a'), dict) else {}

    _audit_snapshot_replayability(snapshot_context, ctx)

    return {
        'poll_ts': datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%dT%H:%M:%S%z"),
        'session_date': ctx.get('today_ist'),
        'recommendation_id': rec_id,
        'action': verdict.get('action'),
        'strategy': verdict.get('strategy'),
        'direction': verdict.get('direction'),
        'confidence': verdict.get('confidence'),
        'pre_alignment_action': verdict.get('pre_alignment_action'),
        'pre_alignment_strategy': verdict.get('pre_alignment_strategy'),
        'dominant_lane': verdict.get('dominant_lane'),
        'dominant_count': verdict.get('dominant_count'),
        'execution_aligned': verdict.get('execution_aligned'),
        'primary_candidate_json': json.dumps(primary),
        'top_candidates_json': json.dumps(clean_cands),
        'context_json': json.dumps(snapshot_context),
        'verdict_json': json.dumps(verdict),
        'market_forces_json': json.dumps(market_forces),
        'poll_summary_json': json.dumps(poll_summary),
        'is_labelable': _is_labelable(result, ctx),
    }


# ═══════════════════════════════════════════════════════════════
# ML ARCHITECTURE V2 — Evening Evaluator (4-Leg Support)
# ═══════════════════════════════════════════════════════════════

def _parse_ist_hour_min(poll_ts_str):
    """Extract (hour, minute) from ISO timestamp string. Returns None on failure."""
    try:
        dt = _parse_iso_ts(poll_ts_str)
        if dt is None:
            return None
        ist = timezone(timedelta(hours=5, minutes=30))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ist)
        else:
            dt = dt.astimezone(ist)
        return dt.hour, dt.minute
    except Exception:
        return None


def _is_h2_window(poll_ts_str):
    """True if timestamp falls inside H2 evaluation window (15:15-15:30 IST)."""
    hm = _parse_ist_hour_min(poll_ts_str)
    if hm is None:
        return False
    h, m = hm
    return h == 15 and 15 <= m <= 30


def _is_h2_preferred_window(poll_ts_str):
    """Preferred evaluation window (15:15-15:25 IST)."""
    hm = _parse_ist_hour_min(poll_ts_str)
    if hm is None:
        return False
    h, m = hm
    return h == 15 and 15 <= m <= 25


def _is_h2_final_fallback_window(poll_ts_str):
    """Final fallback window near close (15:26-15:30 IST)."""
    hm = _parse_ist_hour_min(poll_ts_str)
    if hm is None:
        return False
    h, m = hm
    return h == 15 and 26 <= m <= 30


def _parse_iso_ts(ts_str):
    try:
        if not ts_str:
            return None
        if ts_str.endswith('Z'):
            ts_str = ts_str[:-1] + '+00:00'
        if len(ts_str) > 5 and (ts_str[-5] in ('+', '-')) and ts_str[-3] != ':':
            ts_str = ts_str[:-2] + ':' + ts_str[-2:]
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


def get_price(chain_rows, cand, side, suffix=''):
    """Pull price from chain_lookup within H2 window.

    Preferred source:
    - earliest valid mark in 15:15-15:25 IST
    Fallback source:
    - latest valid mark in the final 15:26-15:30 IST close window
    Returns a float or None if no valid row exists.
    """
    strike_key = f"{side}Strike{suffix}"
    type_key = f"{side}Type{suffix}"
    strike = cand.get(strike_key)
    opt_type = cand.get(type_key)
    if not strike or not opt_type:
        return None
    index_key = cand.get('index') or cand.get('index_key') or 'BNF'
    expiry = cand.get('expiry') or ''
    preferred_row = None
    preferred_ts = None
    fallback_row = None
    fallback_ts = None
    for row_data in chain_rows:
        if row_data.get('index_key') != index_key:
            continue
        if row_data.get('strike') != strike:
            continue
        if row_data.get('option_type') != opt_type:
            continue
        row_expiry = row_data.get('expiry') or ''
        if expiry and row_expiry and row_expiry != expiry:
            continue
        ts_str = row_data.get('poll_ts', '')
        if not _is_h2_window(ts_str):
            continue
        row_ts = _parse_iso_ts(ts_str)
        if row_ts is None:
            continue
        if _is_h2_preferred_window(ts_str):
            if preferred_ts is None or row_ts < preferred_ts:
                preferred_ts = row_ts
                preferred_row = row_data
            continue
        if _is_h2_final_fallback_window(ts_str):
            if fallback_ts is None or row_ts > fallback_ts:
                fallback_ts = row_ts
                fallback_row = row_data
    chosen = preferred_row or fallback_row
    return None if chosen is None else chosen.get('ltp', None)


def _teacher_default_config():
    return {
        'label_version': 'teacher_v1',
        'config_version': '2026-06-15',
        'tp_capture_pct': 0.50,
        'sl_loss_multiple': 1.00,
        'brokerage_per_order': 20.0,
        'gst_rate': 0.18,
        'stamp_duty_buy_rate': 0.00003,
        'sebi_rate': 0.000001,
        'ipft_rate': 0.000000001,
        'exchange_options_rate_pre_2026_03_01': 0.0003503,
        'exchange_options_rate_from_2026_03_01': 0.0003553,
        'stt_options_rate_pre_2026_04_01': 0.0010,
        'stt_options_rate_from_2026_04_01': 0.0015,
        'bid_ask_slippage_fallback_points': 0.25,
        'vix_buckets': {
            'very_low_max': 14.0,
            'low_max': 16.0,
            'normal_max': 19.0,
            'high_max': 24.0,
        },
    }


def _teacher_merge_config(config_obj):
    base = _teacher_default_config()
    if not isinstance(config_obj, dict):
        return base
    for key, value in config_obj.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged = dict(base[key])
            merged.update(value)
            base[key] = merged
        else:
            base[key] = value
    return base


def _normalize_option_type(value):
    return str(value or '').strip().upper()


def _float_or_none(value):
    try:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return float(value)
    except Exception:
        return None


def _strike_matches(left, right):
    l_val = _float_or_none(left)
    r_val = _float_or_none(right)
    if l_val is None or r_val is None:
        return str(left) == str(right)
    return abs(l_val - r_val) < 0.01


def _resolve_entry_vix(snap):
    direct = _float_or_none(snap.get('vix'))
    if direct is not None:
        return direct
    for field in ('context_json', 'poll_summary_json', 'verdict_json', 'market_forces_json'):
        raw = snap.get(field)
        parsed = {}
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {}
        elif isinstance(raw, dict):
            parsed = raw
        if not isinstance(parsed, dict):
            continue
        for key in ('vix', 'current_vix'):
            val = _float_or_none(parsed.get(key))
            if val is not None:
                return val
        poll = parsed.get('poll') if isinstance(parsed.get('poll'), dict) else {}
        val = _float_or_none(poll.get('vix'))
        if val is not None:
            return val
    return None


def _teacher_regime_bucket(entry_vix, config):
    if entry_vix is None:
        return 'unknown'
    buckets = config.get('vix_buckets', {})
    if entry_vix <= float(buckets.get('very_low_max', 14.0)):
        return 'VIX_VERY_LOW'
    if entry_vix <= float(buckets.get('low_max', 16.0)):
        return 'VIX_LOW'
    if entry_vix <= float(buckets.get('normal_max', 19.0)):
        return 'VIX_NORMAL'
    if entry_vix <= float(buckets.get('high_max', 24.0)):
        return 'VIX_HIGH'
    return 'VIX_VERY_HIGH'


def _teacher_option_charge_rates(trade_dt, config):
    stt_cutover = datetime(2026, 4, 1, tzinfo=timezone.utc)
    exchange_cutover = datetime(2026, 3, 1, tzinfo=timezone.utc)
    if trade_dt is None:
        trade_dt = stt_cutover
    stt_rate = (
        float(config.get('stt_options_rate_from_2026_04_01', 0.0015) or 0.0015)
        if trade_dt >= stt_cutover
        else float(config.get('stt_options_rate_pre_2026_04_01', 0.0010) or 0.0010)
    )
    exchange_rate = (
        float(config.get('exchange_options_rate_from_2026_03_01', 0.0003553) or 0.0003553)
        if trade_dt >= exchange_cutover
        else float(config.get('exchange_options_rate_pre_2026_03_01', 0.0003503) or 0.0003503)
    )
    return {
        'stt_rate': stt_rate,
        'exchange_rate': exchange_rate,
        'gst_rate': float(config.get('gst_rate', 0.18) or 0.18),
        'stamp_duty_buy_rate': float(config.get('stamp_duty_buy_rate', 0.00003) or 0.00003),
        'sebi_rate': float(config.get('sebi_rate', 0.000001) or 0.000001),
        'ipft_rate': float(config.get('ipft_rate', 0.000000001) or 0.000000001),
        'brokerage_per_order': float(config.get('brokerage_per_order', 20.0) or 20.0),
        'slippage_fallback_points': float(config.get('bid_ask_slippage_fallback_points', 0.25) or 0.25),
    }


def _entry_snapshot_point(snap, cand):
    ctx = {}
    raw = snap.get('context_json')
    if isinstance(raw, str) and raw:
        try:
            ctx = json.loads(raw)
        except Exception:
            ctx = {}
    elif isinstance(raw, dict):
        ctx = raw
    if not isinstance(ctx, dict):
        return {}
    index_key = cand.get('index') or cand.get('index_key') or 'BNF'
    chain_key = 'nfChain' if str(index_key).upper() == 'NF' else 'bnfChain'
    chain = ctx.get(chain_key) if isinstance(ctx.get(chain_key), dict) else {}
    strikes = chain.get('strikes') if isinstance(chain.get('strikes'), dict) else {}
    leg_specs = [
        ('sell', cand.get('sellStrike'), cand.get('sellType')),
        ('buy', cand.get('buyStrike'), cand.get('buyType')),
    ]
    if cand.get('sellStrike2') is not None:
        leg_specs.extend([
            ('sell2', cand.get('sellStrike2'), cand.get('sellType2')),
            ('buy2', cand.get('buyStrike2'), cand.get('buyType2')),
        ])
    point = {}
    for label, strike, option_type in leg_specs:
        leg_payload = None
        for strike_key, strike_payload in strikes.items():
            if not isinstance(strike_payload, dict):
                continue
            if not _strike_matches(strike_key, strike):
                continue
            maybe = strike_payload.get(_normalize_option_type(option_type))
            if isinstance(maybe, dict):
                leg_payload = maybe
                break
        if not isinstance(leg_payload, dict):
            return {}
        point[label] = _float_or_none(leg_payload.get('ltp'))
        point[f'{label}_bid'] = _float_or_none(leg_payload.get('bid'))
        point[f'{label}_ask'] = _float_or_none(leg_payload.get('ask'))
    return point


def _teacher_candidate_leg_specs(cand):
    leg_specs = [
        ('sell', cand.get('sellStrike'), cand.get('sellType')),
        ('buy', cand.get('buyStrike'), cand.get('buyType')),
    ]
    if cand.get('sellStrike2') is not None:
        leg_specs.extend([
            ('sell2', cand.get('sellStrike2'), cand.get('sellType2')),
            ('buy2', cand.get('buyStrike2'), cand.get('buyType2')),
        ])
    return leg_specs


def _teacher_candidate_is_credit(cand):
    return str(cand.get('type') or '') in ('BEAR_CALL', 'BULL_PUT', 'IRON_CONDOR', 'IRON_BUTTERFLY')


def _teacher_execution_basis(snap, cand, point, entry_point=None):
    is_credit = _teacher_candidate_is_credit(cand)
    lot_size = _float_or_none(cand.get('lotSize')) or (30 if (cand.get('index') or 'BNF') == 'BNF' else 65)
    entry_point = entry_point if isinstance(entry_point, dict) else _entry_snapshot_point(snap, cand)
    leg_specs = _teacher_candidate_leg_specs(cand)
    short_entry_points = 0.0
    long_entry_points = 0.0
    short_close_points = 0.0
    long_close_points = 0.0
    short_mark_points = 0.0
    legs = []
    for label, _, _ in leg_specs:
        is_short = label.startswith('sell')
        entry_px = (
            _float_or_none(entry_point.get(f'{label}_bid'))
            if is_short
            else _float_or_none(entry_point.get(f'{label}_ask'))
        )
        if entry_px is None:
            entry_px = _float_or_none(entry_point.get(label))
        close_px = (
            _float_or_none(point.get(f'{label}_ask'))
            if is_short
            else _float_or_none(point.get(f'{label}_bid'))
        )
        if close_px is None:
            close_px = _float_or_none(point.get(label))
        if entry_px is None or close_px is None:
            return None
        entry_px = max(entry_px, 0.0)
        close_px = max(close_px, 0.0)
        mark_px = max(_float_or_none(point.get(label)) or close_px, 0.0)
        if is_short:
            short_entry_points += entry_px
            short_close_points += close_px
            short_mark_points += mark_px
        else:
            long_entry_points += entry_px
            long_close_points += close_px
        legs.append({
            'label': label,
            'entry_px': round(entry_px, 4),
            'close_px': round(close_px, 4),
            'mark_px': round(mark_px, 4),
            'side': 'short' if is_short else 'long',
        })
    if is_credit:
        entry_basis_points = short_entry_points - long_entry_points
        exit_value_points = short_close_points - long_close_points
        gross_pnl = (entry_basis_points - exit_value_points) * lot_size
        entry_label = 'credit_received'
        exit_label = 'cost_to_close'
        gross_formula = '(credit_received - cost_to_close) * lot'
    else:
        entry_basis_points = long_entry_points - short_entry_points
        exit_value_points = long_close_points - short_close_points
        gross_pnl = (exit_value_points - entry_basis_points) * lot_size
        entry_label = 'debit_paid'
        exit_label = 'value_to_close'
        gross_formula = '(value_to_close - debit_paid) * lot'
    return {
        'is_credit': is_credit,
        'lot_size': lot_size,
        'entry_basis_points': round(entry_basis_points, 4),
        'entry_basis_currency': round(entry_basis_points * lot_size, 2),
        'exit_value_points': round(exit_value_points, 4),
        'exit_value_currency': round(exit_value_points * lot_size, 2),
        'gross_pnl': round(gross_pnl, 2),
        'entry_label': entry_label,
        'exit_label': exit_label,
        'gross_formula': gross_formula,
        'short_mark_value': round(short_mark_points * lot_size, 2),
        'legs': legs,
    }


def _teacher_round_trip_cost(trade_dt, snap, cand, point, config):
    rates = _teacher_option_charge_rates(trade_dt, config)
    lot_size = _float_or_none(cand.get('lotSize')) or (30 if (cand.get('index') or 'BNF') == 'BNF' else 65)
    entry_point = _entry_snapshot_point(snap, cand)
    leg_specs = _teacher_candidate_leg_specs(cand)
    leg_count = len(leg_specs)
    entry_turnover = 0.0
    close_turnover = 0.0
    short_sell_premium = 0.0
    buy_side_premium = 0.0
    slippage = 0.0
    missing_spread_labels = []
    for label, _, _ in leg_specs:
        is_short = label.startswith('sell')
        bid = _float_or_none(point.get(f'{label}_bid'))
        ask = _float_or_none(point.get(f'{label}_ask'))
        spread_points = None
        if bid is not None and ask is not None and ask >= bid:
            spread_points = ask - bid
        else:
            spread_points = rates['slippage_fallback_points']
            missing_spread_labels.append(label)
        slippage += (spread_points / 2.0) * lot_size
        if is_short:
            entry_px = _float_or_none(entry_point.get(f'{label}_bid')) or _float_or_none(entry_point.get(label))
        else:
            entry_px = _float_or_none(entry_point.get(f'{label}_ask')) or _float_or_none(entry_point.get(label))
        entry_px = max(entry_px or 0.0, 0.0)
        entry_turnover += entry_px * lot_size
        if is_short:
            short_sell_premium += entry_px * lot_size
            ask_px = _float_or_none(point.get(f'{label}_ask')) or _float_or_none(point.get(label)) or 0.0
            close_turnover += max(ask_px, 0.0) * lot_size
            buy_side_premium += max(ask_px, 0.0) * lot_size
        else:
            bid_px = _float_or_none(point.get(f'{label}_bid')) or _float_or_none(point.get(label)) or 0.0
            close_turnover += max(bid_px, 0.0) * lot_size
            buy_side_premium += entry_px * lot_size
    slippage *= 2.0
    brokerage = rates['brokerage_per_order'] * leg_count * 2.0
    exchange = (entry_turnover + close_turnover) * rates['exchange_rate']
    ipft = (entry_turnover + close_turnover) * rates['ipft_rate']
    gst = (brokerage + exchange + ipft) * rates['gst_rate']
    stt = short_sell_premium * rates['stt_rate']
    stamp = buy_side_premium * rates['stamp_duty_buy_rate']
    sebi = (entry_turnover + close_turnover) * rates['sebi_rate']
    total = brokerage + exchange + gst + stt + stamp + sebi + ipft + slippage
    return {
        'total': round(total, 2),
        'brokerage': round(brokerage, 2),
        'exchange': round(exchange, 2),
        'gst': round(gst, 2),
        'stt': round(stt, 2),
        'stamp': round(stamp, 2),
        'sebi': round(sebi, 2),
        'ipft': round(ipft, 2),
        'slippage': round(slippage, 2),
        'entry_turnover': round(entry_turnover, 2),
        'close_turnover': round(close_turnover, 2),
        'buy_side_premium': round(buy_side_premium, 2),
        'short_sell_premium': round(short_sell_premium, 2),
        'missing_spread_labels': missing_spread_labels,
        'rates': {
            'stt_rate': rates['stt_rate'],
            'exchange_rate': rates['exchange_rate'],
            'gst_rate': rates['gst_rate'],
            'stamp_duty_buy_rate': rates['stamp_duty_buy_rate'],
            'sebi_rate': rates['sebi_rate'],
            'ipft_rate': rates['ipft_rate'],
        },
    }


def _build_candidate_path(chain_rows, snap, cand):
    index_key = cand.get('index') or cand.get('index_key') or 'BNF'
    expiry = str(cand.get('expiry') or '').strip()
    entry_ts = _parse_iso_ts(snap.get('poll_ts', ''))
    leg_specs = [
        ('sell', cand.get('sellStrike'), cand.get('sellType')),
        ('buy', cand.get('buyStrike'), cand.get('buyType')),
    ]
    if cand.get('sellStrike2') is not None:
        leg_specs.extend([
            ('sell2', cand.get('sellStrike2'), cand.get('sellType2')),
            ('buy2', cand.get('buyStrike2'), cand.get('buyType2')),
        ])

    grouped = {}
    for row_data in chain_rows:
        if row_data.get('index_key') != index_key:
            continue
        row_expiry = str(row_data.get('expiry') or '').strip()
        if expiry and row_expiry and row_expiry != expiry:
            continue
        row_ts_raw = row_data.get('poll_ts', '')
        row_ts = _parse_iso_ts(row_ts_raw)
        if row_ts is None:
            continue
        if entry_ts is not None and row_ts <= entry_ts:
            continue
        strike = row_data.get('strike')
        opt_type = _normalize_option_type(row_data.get('option_type'))
        ltp = _float_or_none(row_data.get('ltp'))
        if ltp is None:
            continue
        bucket = grouped.setdefault(row_ts_raw, {'ts': row_ts, 'prices': {}, 'quotes': {}, 'underlying_spot': None})
        if bucket.get('underlying_spot') is None:
            bucket['underlying_spot'] = (
                row_data.get('underlying_spot')
                or row_data.get('spot')
                or row_data.get('underlying')
            )
        bucket['prices'][(str(strike), opt_type)] = ltp
        bucket['quotes'][(str(strike), opt_type)] = {
            'bid': _float_or_none(row_data.get('bid')),
            'ask': _float_or_none(row_data.get('ask')),
            'ltp': ltp,
        }

    points = []
    for ts_raw, bucket in grouped.items():
        prices = bucket['prices']
        quotes = bucket['quotes']
        point = {'poll_ts': ts_raw, 'ts': bucket['ts'], 'underlying_spot': bucket.get('underlying_spot')}
        complete = True
        for label, strike, opt_type in leg_specs:
            if strike is None or not opt_type:
                complete = False
                break
            wanted_type = _normalize_option_type(opt_type)
            price_val = None
            for (row_strike, row_type), row_ltp in prices.items():
                if row_type != wanted_type:
                    continue
                if _strike_matches(row_strike, strike):
                    price_val = row_ltp
                    quote = quotes.get((row_strike, row_type)) or {}
                    break
            if price_val is None:
                complete = False
                break
            point[label] = price_val
            point[f'{label}_bid'] = quote.get('bid')
            point[f'{label}_ask'] = quote.get('ask')
        if complete:
            points.append(point)
    points.sort(key=lambda item: item['ts'])
    return points


def _credit_structure_breached(cand, point):
    spot = _float_or_none(point.get('underlying_spot'))
    if spot is None:
        return False
    stype = cand.get('type', '')
    call_short = _float_or_none(cand.get('sellStrike'))
    put_short = _float_or_none(cand.get('sellStrike2'))
    if stype == 'BEAR_CALL':
        return call_short is not None and spot >= call_short
    if stype == 'BULL_PUT':
        put_short = _float_or_none(cand.get('sellStrike'))
        return put_short is not None and spot <= put_short
    if stype in ('IRON_CONDOR', 'IRON_BUTTERFLY'):
        call_breach = call_short is not None and spot >= call_short
        put_breach = put_short is not None and spot <= put_short
        return bool(call_breach or put_breach)
    return False


def _gross_spread_pnl(snap, cand, point, entry_point=None):
    basis = _teacher_execution_basis(snap, cand, point, entry_point=entry_point)
    if not isinstance(basis, dict):
        return None, None
    return round(basis['gross_pnl'], 2), round(basis['short_mark_value'], 2)


def _managed_teacher_outcome(chain_rows, snap, cand, config):
    max_profit = _float_or_none(cand.get('maxProfit'))
    if max_profit is None or max_profit <= 0:
        return None
    max_loss = _float_or_none(cand.get('maxLoss'))
    is_credit = _teacher_candidate_is_credit(cand)
    path_points = _build_candidate_path(chain_rows, snap, cand)
    if not path_points:
        return None

    entry_point = _entry_snapshot_point(snap, cand)
    first_basis = _teacher_execution_basis(snap, cand, path_points[0], entry_point=entry_point)
    if not isinstance(first_basis, dict):
        return None
    tp_threshold = round(max_profit * float(config.get('tp_capture_pct', 0.50) or 0.50), 2)
    if is_credit:
        if max_loss is not None and max_loss > 0:
            risk_at_entry = round(max_loss, 2)
        else:
            risk_at_entry = round(max_profit * float(config.get('sl_loss_multiple', 1.00) or 1.00), 2)
    else:
        risk_at_entry = round(first_basis.get('entry_basis_currency') or 0.0, 2)
        if risk_at_entry <= 0 and max_loss is not None and max_loss > 0:
            risk_at_entry = round(max_loss, 2)
    if risk_at_entry <= 0:
        return None

    exit_reason = 'EOD'
    exit_point = path_points[-1]
    exit_step = len(path_points)
    managed_gross_pnl = None
    friction_cost = None
    managed_pnl = None

    trade_dt = _parse_iso_ts(snap.get('poll_ts', ''))
    for idx, point in enumerate(path_points, start=1):
        gross_pnl, sell_premium_value = _gross_spread_pnl(snap, cand, point, entry_point=entry_point)
        if gross_pnl is None:
            continue
        cost_breakdown = _teacher_round_trip_cost(trade_dt, snap, cand, point, config)
        round_trip_cost = cost_breakdown['total']
        net_pnl = round(gross_pnl - round_trip_cost, 2)
        if net_pnl >= tp_threshold:
            exit_reason = 'TP'
            exit_point = point
            exit_step = idx
            managed_gross_pnl = gross_pnl
            friction_cost = round_trip_cost
            managed_pnl = net_pnl
            break
        stop_allowed = True
        if is_credit:
            stop_allowed = _credit_structure_breached(cand, point)
        if stop_allowed and net_pnl <= -risk_at_entry:
            exit_reason = 'SL'
            exit_point = point
            exit_step = idx
            managed_gross_pnl = gross_pnl
            friction_cost = round_trip_cost
            # Preserve gap-through-stop losses instead of clipping to the
            # configured stop threshold. The teacher should reflect the actual
            # realized path, not the intended stop.
            managed_pnl = net_pnl
            break
        managed_gross_pnl = gross_pnl
        friction_cost = round_trip_cost
        managed_pnl = net_pnl

    entry_vix = _resolve_entry_vix(snap)
    regime_bucket = _teacher_regime_bucket(entry_vix, config)
    managed_pnl = round(managed_pnl if managed_pnl is not None else 0.0, 2)
    r_multiple = round(managed_pnl / risk_at_entry, 4) if risk_at_entry > 0 else None
    captured_pct = round(managed_pnl / max_profit, 4) if max_profit > 0 else None
    success = exit_reason == 'TP'
    avg_win_r = max(tp_threshold / risk_at_entry, 0.0) if risk_at_entry > 0 else 0.0
    avg_loss_r = 1.0
    break_even = round((avg_loss_r / (avg_loss_r + avg_win_r)) * 100.0, 2) if avg_win_r > 0 else 100.0

    return {
        'managed_pnl': managed_pnl,
        'managed_gross_pnl': round(managed_gross_pnl or 0.0, 2),
        'friction_cost': round(friction_cost or 0.0, 2),
        'exit_reason': exit_reason,
        'exit_step': exit_step,
        'exit_ts': exit_point.get('poll_ts'),
        'path_points_count': len(path_points),
        'r_multiple': r_multiple,
        'captured_pct': captured_pct,
        'is_success': 1 if success else 0,
        'risk_at_entry': round(risk_at_entry, 2),
        'regime_bucket': regime_bucket,
        'label_version': config.get('label_version', 'teacher_v1'),
        'teacher_config_version': config.get('config_version', '2026-06-15'),
        'tp_threshold': round(tp_threshold, 2),
        'sl_threshold': round(risk_at_entry, 2),
        'break_even_win_rate_pct': break_even,
    }


def _eval_single_candidate(chain_rows, snap, cand, teacher_config=None):
    """Evaluate one candidate with both legacy H2 labels and teacher_v1 shadow labels.
    Returns outcome dict, or None if the candidate has no evaluable price path."""
    stype = cand.get('type', '')
    is_4_leg = cand.get('sellStrike2') is not None

    sell_ltp_h2 = get_price(chain_rows, cand, 'sell')
    buy_ltp_h2 = get_price(chain_rows, cand, 'buy')
    sim_pnl = None

    if sell_ltp_h2 is not None and buy_ltp_h2 is not None:
        legacy_complete = True
        if is_4_leg:
            sell2_ltp_h2 = get_price(chain_rows, cand, 'sell', suffix='2')
            buy2_ltp_h2 = get_price(chain_rows, cand, 'buy', suffix='2')
            if sell2_ltp_h2 is None or buy2_ltp_h2 is None:
                legacy_complete = False
            else:
                later_net_credit = (sell_ltp_h2 - buy_ltp_h2) + (sell2_ltp_h2 - buy2_ltp_h2)
        else:
            later_net_credit = sell_ltp_h2 - buy_ltp_h2

        if legacy_complete:
            entry_premium = cand.get('netPremium', 0)
            lot_size = cand.get('lotSize') or (30 if (cand.get('index') or 'BNF') == 'BNF' else 65)
            is_credit = stype in ('BEAR_CALL', 'BULL_PUT', 'IRON_CONDOR', 'IRON_BUTTERFLY')
            if is_credit:
                sim_pnl = (entry_premium - later_net_credit) * lot_size
            else:
                sim_pnl = (later_net_credit - entry_premium) * lot_size

    index_key = cand.get('index') or cand.get('index_key') or 'BNF'
    lane = cand.get('lane')
    trade_mode = cand.get('trade_mode') or cand.get('mode')
    if not trade_mode and isinstance(lane, str):
        if lane.endswith('_intraday'):
            trade_mode = 'intraday'
        elif lane.endswith('_swing'):
            trade_mode = 'swing'
    strategy_type = cand.get('type') or cand.get('strategy_type') or ''
    outcome = {
        'snapshot_id': snap.get('id'),
        'session_date': snap.get('session_date'),
        'candidate_id': cand.get('id'),
        'lane': lane or _candidate_lane(index_key, trade_mode or 'intraday'),
        'index_key': index_key,
        'trade_mode': trade_mode or 'unknown',
        'strategy_type': strategy_type,
        'sim_pnl_h2': sim_pnl,
        'canonical_won': (1 if sim_pnl > 0 else 0) if sim_pnl is not None else None,
        'outcome_h2': (1 if sim_pnl > 0 else 0) if sim_pnl is not None else None,
        'won': (1 if sim_pnl > 0 else 0) if sim_pnl is not None else None,
    }
    teacher = _managed_teacher_outcome(chain_rows, snap, cand, teacher_config or _teacher_default_config())
    if teacher is None:
        return None
    outcome.update(teacher)
    return outcome


_EVAL_JOB_CACHE = {}


def _safe_json_field(raw_value, default):
    if isinstance(raw_value, str):
        if not raw_value:
            return default
        try:
            return json.loads(raw_value)
        except Exception:
            return default
    if raw_value is None:
        return default
    return raw_value


def _load_json_file(path, default):
    if not path:
        return default
    with open(path, 'r', encoding='utf-8') as handle:
        while True:
            first = handle.read(1)
            if not first:
                return default
            if not first.isspace():
                handle.seek(0)
                break
        return json.load(handle)


def _evaluate_snapshot_outcomes(snap, chain_rows, teacher_config):
    outcomes = []
    errors = []

    primary_json = snap.get('primary_candidate_json', '{}')
    primary = _safe_json_field(primary_json, {})
    if primary_json and isinstance(primary_json, str) and not isinstance(primary, dict):
        primary = {}
        errors.append({
            'scope': 'primary_candidate_json',
            'snapshot_id': snap.get('id'),
            'error': 'primary_candidate_json_not_object',
        })
    elif not isinstance(primary, dict):
        primary = {}

    if primary and primary.get('id'):
        try:
            outcome = _eval_single_candidate(chain_rows, snap, primary, teacher_config)
            if outcome is not None:
                outcome['role'] = 'primary'
                outcome['rank_in_snapshot'] = 1
                outcome['varsity_tier'] = primary.get('varsityTier')
                outcome['premium_edge'] = primary.get('premiumEdge')
                outcome['credit_width_ratio'] = primary.get('creditWidthRatio')
                outcome['sigma_otm'] = primary.get('sigmaOTM')
                outcomes.append(outcome)
        except Exception as exc:
            errors.append({
                'scope': 'primary',
                'snapshot_id': snap.get('id'),
                'candidate_id': primary.get('id'),
                'error': str(exc),
            })

    context_json = snap.get('context_json', '{}')
    snap_ctx = _safe_json_field(context_json, {})
    if context_json and isinstance(context_json, str) and not isinstance(snap_ctx, dict):
        snap_ctx = {}
        errors.append({
            'scope': 'context_json',
            'snapshot_id': snap.get('id'),
            'error': 'context_json_not_object',
        })
    elif not isinstance(snap_ctx, dict):
        snap_ctx = {}

    generated = snap_ctx.get('snapshot_generated_candidates')
    if not isinstance(generated, list) or not generated:
        cands_json = snap.get('top_candidates_json', '[]')
        generated = _safe_json_field(cands_json, [])
        if cands_json and isinstance(cands_json, str) and not isinstance(generated, list):
            generated = []
            errors.append({
                'scope': 'top_candidates_json',
                'snapshot_id': snap.get('id'),
                'error': 'top_candidates_json_not_list',
            })
        elif not isinstance(generated, list):
            generated = []

    seen_ids = set()
    if primary and primary.get('id'):
        seen_ids.add(primary.get('id'))

    for rank_idx, cand in enumerate(generated, start=1):
        if not isinstance(cand, dict):
            errors.append({
                'scope': 'generated_candidate',
                'snapshot_id': snap.get('id'),
                'error': 'generated_candidate_not_object',
            })
            continue
        cand_id = cand.get('id')
        if cand_id in seen_ids:
            continue
        seen_ids.add(cand_id)
        try:
            outcome = _eval_single_candidate(chain_rows, snap, cand, teacher_config)
            if outcome is not None:
                outcome['role'] = 'secondary'
                outcome['rank_in_snapshot'] = rank_idx
                outcome['varsity_tier'] = cand.get('varsityTier')
                outcome['premium_edge'] = cand.get('premiumEdge')
                outcome['credit_width_ratio'] = cand.get('creditWidthRatio')
                outcome['sigma_otm'] = cand.get('sigmaOTM')
                outcomes.append(outcome)
        except Exception as exc:
            errors.append({
                'scope': 'secondary',
                'snapshot_id': snap.get('id'),
                'candidate_id': cand_id,
                'error': str(exc),
            })

    return {'outcomes': outcomes, 'errors': errors}


def evaluation_job_prepare(run_id, snapshots_path, chain_slices_path, teacher_config_json_str=None):
    teacher_config = _teacher_default_config()
    if teacher_config_json_str:
        try:
            teacher_config = _teacher_merge_config(json.loads(teacher_config_json_str))
        except Exception:
            teacher_config = _teacher_default_config()

    snapshots = _load_json_file(snapshots_path, [])
    chain_slices = _load_json_file(chain_slices_path, [])
    if not isinstance(snapshots, list):
        snapshots = []
    if not isinstance(chain_slices, list):
        chain_slices = []

    _EVAL_JOB_CACHE[str(run_id)] = {
        'snapshots': snapshots,
        'chain_rows': chain_slices,
        'teacher_config': teacher_config,
    }
    return json.dumps({
        'ok': True,
        'snapshot_count': len(snapshots),
        'chain_row_count': len(chain_slices),
    })


def evaluation_job_run_batch(run_id, start_idx, batch_size=10):
    job = _EVAL_JOB_CACHE.get(str(run_id))
    if not job:
        return json.dumps({'ok': False, 'error': 'RUN_NOT_PREPARED'})

    snapshots = job.get('snapshots') or []
    chain_rows = job.get('chain_rows') or []
    teacher_config = job.get('teacher_config') or _teacher_default_config()
    start = max(int(start_idx or 0), 0)
    size = max(int(batch_size or 1), 1)
    end = min(start + size, len(snapshots))

    outcomes = []
    errors = []
    processed = 0
    for idx in range(start, end):
        snap = snapshots[idx]
        try:
            result = _evaluate_snapshot_outcomes(snap, chain_rows, teacher_config)
            outcomes.extend(result.get('outcomes') or [])
            errors.extend(result.get('errors') or [])
        except Exception as exc:
            errors.append({
                'scope': 'snapshot',
                'snapshot_id': snap.get('id') if isinstance(snap, dict) else None,
                'error': str(exc),
            })
        processed += 1

    return json.dumps({
        'ok': True,
        'start': start,
        'end': end,
        'processed': processed,
        'snapshot_count': len(snapshots),
        'produced_count': len(outcomes),
        'error_count': len(errors),
        'errors': errors[:20],
        'outcomes': outcomes,
    })


def evaluation_job_finalize(run_id):
    _EVAL_JOB_CACHE.pop(str(run_id), None)
    return json.dumps({'ok': True})


def evening_evaluator(session_date_str, snapshots_json_str, chain_slices_json_str, teacher_config_json_str=None):
    """Evaluates P&L for both 2-leg and 4-leg strategies.
    Data is passed as JSON strings from Kotlin. No Supabase calls inside Python.

    PRIMARY outcome = surfaced recommendation (ML training target).
    SECONDARY outcomes = all generated candidates captured in snapshot context."""
    if session_date_str in _CONST.get('NSE_HOLIDAYS', []):
        return json.dumps([])

    snapshots = json.loads(snapshots_json_str)
    chain_slices = json.loads(chain_slices_json_str) if chain_slices_json_str else []
    teacher_config = _teacher_default_config()
    if teacher_config_json_str:
        try:
            teacher_config = _teacher_merge_config(json.loads(teacher_config_json_str))
        except Exception:
            teacher_config = _teacher_default_config()

    chain_rows = list(chain_slices)

    outcomes = []

    for snap in snapshots:
        result = _evaluate_snapshot_outcomes(snap, chain_rows, teacher_config)
        outcomes.extend(result.get('outcomes') or [])

    return json.dumps(outcomes)


def _count_key(counter, key):
    key = str(key or '').strip() or 'UNKNOWN'
    counter[key] = counter.get(key, 0) + 1


def _safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _summary_from_outcomes(rows):
    count = 0
    success = 0
    positive = 0
    sum_r = 0.0
    sum_pnl = 0.0
    pnl_count = 0
    for row in rows:
        r_val = _safe_float(row.get('r_multiple'))
        if r_val is None:
            continue
        count += 1
        sum_r += r_val
        if r_val > 0:
            positive += 1
        pnl = _safe_float(row.get('managed_pnl'))
        if pnl is not None:
            pnl_count += 1
            sum_pnl += pnl
            if pnl > 0 and r_val <= 0:
                positive += 0
        success_val = row.get('is_success')
        if success_val is True or success_val == 1 or str(success_val).lower() in ('1', 'true', 'yes'):
            success += 1
    return {
        'rows': count,
        'successes': success,
        'success_rate_pct': round((success * 100.0 / count), 2) if count else 0.0,
        'positive_r_rows': positive,
        'positive_r_pct': round((positive * 100.0 / count), 2) if count else 0.0,
        'avg_r': round((sum_r / count), 4) if count else 0.0,
        'avg_managed_pnl': round((sum_pnl / pnl_count), 2) if pnl_count else 0.0,
    }


def _top_counter_items(counter, limit=12):
    return [
        {'key': key, 'count': count}
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def session_teacher_research_report(session_date_str, snapshots_json_str, outcomes_json_str):
    """Build the Friday-style explanation artifact for one completed session.

    This is measurement only. It compares the chosen primary against the full
    generated candidate menu already saved in each snapshot and the teacher
    outcomes already produced by the evaluator.
    """
    try:
        snapshots = json.loads(snapshots_json_str) if snapshots_json_str else []
        outcomes = json.loads(outcomes_json_str) if outcomes_json_str else []
    except Exception as exc:
        return json.dumps({'ok': False, 'error': str(exc), 'session_date': session_date_str})
    if not isinstance(snapshots, list):
        snapshots = []
    if not isinstance(outcomes, list):
        outcomes = []

    action_counts = {}
    strategy_counts = {}
    primary_type_counts = {}
    generated_type_counts = {}
    generated_first_type_counts = {}
    rank_pair_counts = {}
    vix_values = []
    bnf_values = []
    nf_values = []
    significant_moves = 0
    flat_gap_count = 0
    generated_meta = {}
    stage2a_mode_counts = {}
    stage2a_table_error_counts = {}
    stage2a_chosen_coverage_counts = {}
    stage2a_snapshot_count = 0
    stage2a_table_ready_count = 0
    stage2a_shadow_compared = 0
    stage2a_shadow_top_changed = 0
    stage2a_live_compared = 0
    stage2a_live_top_changed = 0
    stage2a_hard_wait_count = 0
    stage2a_covered_snapshot_count = 0
    stage2a_positive_snapshot_count = 0
    stage2a_thin_snapshot_count = 0
    stage2a_unseen_snapshot_count = 0
    stage2a_chosen_teacher_r_sum = 0.0
    stage2a_chosen_teacher_r_count = 0
    stage2a_chosen_teacher_n_sum = 0
    stage2a_chosen_teacher_n_count = 0
    snapshots_with = {
        'with_primary': 0,
        'with_generated': 0,
        'with_rejected': 0,
        'with_context': 0,
        'with_bear_call': 0,
        'with_bull_put': 0,
        'with_both_bear_call_bull_put': 0,
        'bear_call_before_bull_put': 0,
        'bull_put_before_bear_call': 0,
    }

    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        _count_key(action_counts, snap.get('action'))
        _count_key(strategy_counts, snap.get('strategy'))
        sid = snap.get('id')
        raw_context_json = snap.get('context_json', '{}')
        if isinstance(raw_context_json, str) and raw_context_json.strip():
            snapshots_with['with_context'] += 1
        ctx = _safe_json_field(raw_context_json, {})
        if not isinstance(ctx, dict):
            ctx = {}
        primary = _safe_json_field(snap.get('primary_candidate_json', '{}'), {})
        if isinstance(primary, dict) and primary.get('id'):
            snapshots_with['with_primary'] += 1
            _count_key(primary_type_counts, primary.get('type'))
            chosen_coverage = str(primary.get('teacher_coverage') or '').strip().lower()
            if chosen_coverage:
                _count_key(stage2a_chosen_coverage_counts, chosen_coverage)
            chosen_teacher_r = _safe_float(primary.get('teacher_r_score'))
            if chosen_teacher_r is not None:
                stage2a_chosen_teacher_r_sum += chosen_teacher_r
                stage2a_chosen_teacher_r_count += 1
            chosen_teacher_n = primary.get('teacher_bucket_n')
            try:
                chosen_teacher_n = int(chosen_teacher_n)
                stage2a_chosen_teacher_n_sum += chosen_teacher_n
                stage2a_chosen_teacher_n_count += 1
            except Exception:
                pass
        vix = _safe_float(ctx.get('vix'))
        if vix is not None:
            vix_values.append(vix)
        bnf = _safe_float(ctx.get('bnfSpot'))
        nf = _safe_float(ctx.get('nfSpot'))
        if bnf is not None:
            bnf_values.append(bnf)
        if nf is not None:
            nf_values.append(nf)
        if ctx.get('significant_move') is True:
            significant_moves += 1
        gap = ctx.get('gap') if isinstance(ctx.get('gap'), dict) else {}
        if str(gap.get('type') or '').upper() == 'FLAT':
            flat_gap_count += 1

        generated = ctx.get('snapshot_generated_candidates')
        if not isinstance(generated, list) or not generated:
            generated = _safe_json_field(snap.get('top_candidates_json', '[]'), [])
        if not isinstance(generated, list):
            generated = []
        if generated:
            snapshots_with['with_generated'] += 1
        rejected = ctx.get('snapshot_rejected_candidates')
        if isinstance(rejected, list) and rejected:
            snapshots_with['with_rejected'] += 1

        stage2a = ctx.get('snapshot_stage2a')
        if isinstance(stage2a, dict) and stage2a:
            stage2a_snapshot_count += 1
            mode = str(stage2a.get('mode') or 'unknown').strip().lower() or 'unknown'
            _count_key(stage2a_mode_counts, mode)
            if stage2a.get('table_ready') is True:
                stage2a_table_ready_count += 1
            table_error = str(stage2a.get('table_error') or '').strip()
            if table_error:
                _count_key(stage2a_table_error_counts, table_error)
            if int(stage2a.get('covered_count') or 0) > 0:
                stage2a_covered_snapshot_count += 1
            if int(stage2a.get('positive_count') or 0) > 0:
                stage2a_positive_snapshot_count += 1
            if int(stage2a.get('thin_count') or 0) > 0:
                stage2a_thin_snapshot_count += 1
            if int(stage2a.get('unseen_count') or 0) > 0:
                stage2a_unseen_snapshot_count += 1
            if stage2a.get('shadow_top_candidate_id') is not None and stage2a.get('deterministic_top_candidate_id') is not None:
                stage2a_shadow_compared += 1
                if stage2a.get('shadow_changes_top') is True:
                    stage2a_shadow_top_changed += 1
            if stage2a.get('live_top_candidate_id') is not None and stage2a.get('deterministic_top_candidate_id') is not None:
                stage2a_live_compared += 1
                if stage2a.get('live_changes_top') is True:
                    stage2a_live_top_changed += 1
            if stage2a.get('hard_wait_triggered') is True:
                stage2a_hard_wait_count += 1

        first_pos = {}
        for rank_idx, cand in enumerate(generated, start=1):
            if not isinstance(cand, dict):
                continue
            cand_id = cand.get('id')
            stype = cand.get('type') or cand.get('strategy_type') or 'UNKNOWN'
            _count_key(generated_type_counts, stype)
            if rank_idx == 1:
                _count_key(generated_first_type_counts, stype)
            if stype not in first_pos:
                first_pos[stype] = rank_idx
            if sid is not None and cand_id:
                generated_meta[(sid, cand_id)] = {
                    'rank': rank_idx,
                    'type': stype,
                    'width': cand.get('width'),
                    'premium_edge': cand.get('premiumEdge'),
                    'credit_width_ratio': cand.get('creditWidthRatio'),
                    'sigma_otm': cand.get('sigmaOTM'),
                }
        has_bc = 'BEAR_CALL' in first_pos
        has_bp = 'BULL_PUT' in first_pos
        if has_bc:
            snapshots_with['with_bear_call'] += 1
        if has_bp:
            snapshots_with['with_bull_put'] += 1
        if has_bc and has_bp:
            snapshots_with['with_both_bear_call_bull_put'] += 1
            pair_key = f"{first_pos['BEAR_CALL']}|{first_pos['BULL_PUT']}"
            _count_key(rank_pair_counts, pair_key)
            if first_pos['BEAR_CALL'] < first_pos['BULL_PUT']:
                snapshots_with['bear_call_before_bull_put'] += 1
            elif first_pos['BULL_PUT'] < first_pos['BEAR_CALL']:
                snapshots_with['bull_put_before_bear_call'] += 1

    enriched_outcomes = []
    by_snapshot = {}
    by_strategy = {}
    by_rank = {}
    primary_rows = []
    secondary_rows = []
    for row in outcomes:
        if not isinstance(row, dict):
            continue
        sid = row.get('snapshot_id')
        cid = row.get('candidate_id')
        meta = generated_meta.get((sid, cid), {})
        enriched = dict(row)
        if meta:
            enriched.setdefault('rank_in_snapshot', meta.get('rank'))
            enriched.setdefault('strategy_type', meta.get('type'))
            enriched.setdefault('width', meta.get('width'))
            enriched.setdefault('premium_edge', meta.get('premium_edge'))
            enriched.setdefault('credit_width_ratio', meta.get('credit_width_ratio'))
            enriched.setdefault('sigma_otm', meta.get('sigma_otm'))
        enriched_outcomes.append(enriched)
        by_snapshot.setdefault(sid, []).append(enriched)
        by_strategy.setdefault(enriched.get('strategy_type') or 'UNKNOWN', []).append(enriched)
        rank = enriched.get('rank_in_snapshot')
        if rank is not None:
            by_rank.setdefault(str(rank), []).append(enriched)
        role = str(enriched.get('role') or '').lower()
        if role == 'primary':
            primary_rows.append(enriched)
        else:
            secondary_rows.append(enriched)

    best_type_counts = {}
    best_rank_counts = {}
    snapshot_compared = 0
    primary_best = 0
    better_available = 0
    positive_alternative = 0
    all_non_positive = 0
    improvement_sum = 0.0
    improvement_count = 0
    for sid, rows in by_snapshot.items():
        primary = next((r for r in rows if str(r.get('role') or '').lower() == 'primary'), None)
        scored = [r for r in rows if _safe_float(r.get('r_multiple')) is not None]
        if not primary or not scored:
            continue
        primary_r = _safe_float(primary.get('r_multiple'))
        if primary_r is None:
            continue
        best = max(scored, key=lambda r: _safe_float(r.get('r_multiple')) or -999999.0)
        best_r = _safe_float(best.get('r_multiple'))
        if best_r is None:
            continue
        snapshot_compared += 1
        _count_key(best_type_counts, best.get('strategy_type') or best.get('type'))
        rank = best.get('rank_in_snapshot')
        if rank is not None:
            _count_key(best_rank_counts, str(rank))
        if best.get('candidate_id') == primary.get('candidate_id'):
            primary_best += 1
        if best_r > primary_r:
            better_available += 1
        if any((_safe_float(r.get('r_multiple')) or 0.0) > 0 for r in rows if r is not primary):
            positive_alternative += 1
        if all((_safe_float(r.get('r_multiple')) or 0.0) <= 0 for r in rows):
            all_non_positive += 1
        improvement_sum += (best_r - primary_r)
        improvement_count += 1

    def series_summary(values):
        if not values:
            return {}
        return {
            'first': round(values[0], 2),
            'last': round(values[-1], 2),
            'min': round(min(values), 2),
            'max': round(max(values), 2),
            'avg': round(sum(values) / len(values), 2),
            'change': round(values[-1] - values[0], 2),
        }

    strategy_summaries = {}
    for key, rows in by_strategy.items():
        strategy_summaries[key] = _summary_from_outcomes(rows)
    rank_summaries = {}
    for key, rows in by_rank.items():
        rank_summaries[key] = _summary_from_outcomes(rows)

    primary_snapshot_ids = sorted({row.get('snapshot_id') for row in primary_rows if row.get('snapshot_id') is not None})
    primary_snapshot_lookup = {}
    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        sid = snap.get('id')
        if sid is not None and sid not in primary_snapshot_lookup:
            primary_snapshot_lookup[sid] = snap

    primary_context_ready = 0
    primary_generated_ready = 0
    primary_rejected_ready = 0
    primary_primary_json_ready = 0
    primary_context_missing_ids = []
    primary_generated_missing_ids = []
    primary_rejected_missing_ids = []
    primary_primary_json_missing_ids = []
    for sid in primary_snapshot_ids:
        snap = primary_snapshot_lookup.get(sid)
        if not snap:
            primary_context_missing_ids.append(sid)
            primary_generated_missing_ids.append(sid)
            primary_rejected_missing_ids.append(sid)
            primary_primary_json_missing_ids.append(sid)
            continue

        raw_context_json = snap.get('context_json', '{}')
        ctx = _safe_json_field(raw_context_json, {})
        if isinstance(raw_context_json, str) and raw_context_json.strip() and isinstance(ctx, dict):
            primary_context_ready += 1
        else:
            primary_context_missing_ids.append(sid)

        primary = _safe_json_field(snap.get('primary_candidate_json', '{}'), {})
        if isinstance(primary, dict) and primary.get('id'):
            primary_primary_json_ready += 1
        else:
            primary_primary_json_missing_ids.append(sid)

        generated = ctx.get('snapshot_generated_candidates')
        if not isinstance(generated, list) or not generated:
            generated = _safe_json_field(snap.get('top_candidates_json', '[]'), [])
        if isinstance(generated, list) and generated:
            primary_generated_ready += 1
        else:
            primary_generated_missing_ids.append(sid)

        rejected = ctx.get('snapshot_rejected_candidates')
        if isinstance(rejected, list) and rejected:
            primary_rejected_ready += 1
        else:
            primary_rejected_missing_ids.append(sid)

    no_class_a_session = not primary_snapshot_ids and snapshots_with['with_primary'] == 0 and snapshots_with['with_generated'] == 0
    class_a_blocked_reasons = []
    if not primary_snapshot_ids and not no_class_a_session:
        class_a_blocked_reasons.append('no_primary_teacher_rows_were_available')
    if primary_context_ready != len(primary_snapshot_ids):
        class_a_blocked_reasons.append(f"context_json_missing_or_empty_on_{len(primary_snapshot_ids) - primary_context_ready}_primary_snapshots")
    if primary_primary_json_ready != len(primary_snapshot_ids):
        class_a_blocked_reasons.append(f"primary_candidate_json_missing_on_{len(primary_snapshot_ids) - primary_primary_json_ready}_primary_snapshots")
    if primary_generated_ready != len(primary_snapshot_ids):
        class_a_blocked_reasons.append(f"generated_menu_missing_on_{len(primary_snapshot_ids) - primary_generated_ready}_primary_snapshots")
    if primary_rejected_ready != len(primary_snapshot_ids):
        class_a_blocked_reasons.append(f"rejected_menu_missing_on_{len(primary_snapshot_ids) - primary_rejected_ready}_primary_snapshots")
    if snapshot_compared != len(primary_snapshot_ids):
        class_a_blocked_reasons.append(f"primary_vs_best_comparison_only_covered_{snapshot_compared}_of_{len(primary_snapshot_ids)}_primary_snapshots")

    class_a_status = 'N/A' if no_class_a_session else ('PASS' if not class_a_blocked_reasons else 'FAIL')
    class_a_gate = {
        'status': class_a_status,
        'scope': 'saved_live_session_parity_gate',
        'session_date': session_date_str,
        'primary_snapshot_count': len(primary_snapshot_ids),
        'comparison_ready_count': snapshot_compared,
        'context_ready_count': primary_context_ready,
        'primary_candidate_ready_count': primary_primary_json_ready,
        'generated_menu_ready_count': primary_generated_ready,
        'rejected_menu_ready_count': primary_rejected_ready,
        'snapshot_count': len(snapshots),
        'outcome_count': len(enriched_outcomes),
        'primary_outcome_count': len(primary_rows),
        'secondary_outcome_count': len(secondary_rows),
        'with_context_snapshots': snapshots_with['with_context'],
        'with_generated_snapshots': snapshots_with['with_generated'],
        'with_rejected_snapshots': snapshots_with['with_rejected'],
        'no_class_a_session': no_class_a_session,
        'na_reason': 'no_evaluable_candidate_legs_captured' if no_class_a_session else None,
        'blocked_reasons': class_a_blocked_reasons,
        'ready_for_tomorrow_comparison': class_a_status == 'PASS',
    }

    stage2a_blockers = []
    if stage2a_snapshot_count <= 0:
        stage2a_blockers.append('no_stage2a_snapshot_context')
    if stage2a_snapshot_count > 0 and stage2a_table_ready_count <= 0:
        stage2a_blockers.append('teacher_table_not_ready_in_saved_snapshots')
    if stage2a_snapshot_count > 0 and stage2a_shadow_compared <= 0:
        stage2a_blockers.append('no_shadow_vs_deterministic_comparison')
    if stage2a_snapshot_count > 0 and stage2a_covered_snapshot_count <= 0:
        stage2a_blockers.append('no_covered_teacher_buckets_seen')
    if stage2a_snapshot_count > 0 and not any(str(k).lower() == 'shadow' for k in stage2a_mode_counts.keys()):
        stage2a_blockers.append('shadow_mode_not_observed')
    if stage2a_snapshot_count <= 0:
        stage2a_audit_status = 'NO_EVIDENCE'
    elif stage2a_blockers:
        stage2a_audit_status = 'COLLECT_MORE_SHADOW'
    else:
        stage2a_audit_status = 'READY_FOR_MANUAL_REVIEW'

    stage2a_shadow = {
        'audit_status': stage2a_audit_status,
        'blocked_reasons': stage2a_blockers,
        'snapshot_count': stage2a_snapshot_count,
        'mode_counts': stage2a_mode_counts,
        'table_ready_count': stage2a_table_ready_count,
        'table_error_counts': stage2a_table_error_counts,
        'shadow_compared': stage2a_shadow_compared,
        'shadow_top_changed': stage2a_shadow_top_changed,
        'live_compared': stage2a_live_compared,
        'live_top_changed': stage2a_live_top_changed,
        'hard_wait_count': stage2a_hard_wait_count,
        'covered_snapshot_count': stage2a_covered_snapshot_count,
        'positive_snapshot_count': stage2a_positive_snapshot_count,
        'thin_snapshot_count': stage2a_thin_snapshot_count,
        'unseen_snapshot_count': stage2a_unseen_snapshot_count,
        'chosen_coverage_counts': stage2a_chosen_coverage_counts,
        'chosen_avg_teacher_r': round(stage2a_chosen_teacher_r_sum / stage2a_chosen_teacher_r_count, 4) if stage2a_chosen_teacher_r_count else None,
        'chosen_avg_teacher_bucket_n': round(stage2a_chosen_teacher_n_sum / stage2a_chosen_teacher_n_count, 2) if stage2a_chosen_teacher_n_count else None,
        'shadow_change_rate_pct': round((stage2a_shadow_top_changed * 100.0) / stage2a_shadow_compared, 2) if stage2a_shadow_compared else None,
        'live_change_rate_pct': round((stage2a_live_top_changed * 100.0) / stage2a_live_compared, 2) if stage2a_live_compared else None,
        'scope': 'stage2a_shadow_vs_deterministic',
    }

    return json.dumps({
        'ok': True,
        'schema_version': 1,
        'session_date': session_date_str,
        'scope': 'daily_primary_vs_generated_teacher_research',
        'snapshot_count': len(snapshots),
        'outcome_count': len(enriched_outcomes),
        'class_a_gate': class_a_gate,
        'market': {
            'vix': series_summary(vix_values),
            'bnf': series_summary(bnf_values),
            'nf': series_summary(nf_values),
            'significant_move_snapshots': significant_moves,
            'flat_gap_snapshots': flat_gap_count,
        },
        'brain_behavior': {
            'action_counts': action_counts,
            'strategy_counts': strategy_counts,
            'primary_type_counts': primary_type_counts,
            'generated_type_counts': generated_type_counts,
            'generated_first_type_counts': generated_first_type_counts,
            'snapshots': snapshots_with,
            'bear_call_bull_put_first_position_pairs': _top_counter_items(rank_pair_counts, 8),
        },
        'teacher_outcomes': {
            'primary': _summary_from_outcomes(primary_rows),
            'secondary': _summary_from_outcomes(secondary_rows),
            'by_strategy': strategy_summaries,
            'by_rank': rank_summaries,
        },
        'stage2a_shadow': stage2a_shadow,
        'primary_vs_best': {
            'snapshots_compared': snapshot_compared,
            'primary_was_best': primary_best,
            'better_candidate_available': better_available,
            'positive_alternative_available': positive_alternative,
            'all_candidates_non_positive': all_non_positive,
            'avg_best_minus_primary_r': round(improvement_sum / improvement_count, 4) if improvement_count else 0.0,
            'best_type_counts': best_type_counts,
            'best_rank_counts': best_rank_counts,
        },
    })


# ═══════════════════════════════════════════════════════════════
# NOTIFICATION AGENT — State Machine with Chop Lock / Hysteresis
# ═══════════════════════════════════════════════════════════════

class NotificationAgent:
    def __init__(self, state=None):
        if state is None:
            state = {
                'action': 'WAIT',
                'strategy': None,
                'confidence': 0,
                'timestamp': 0,
                'cooldown_until': 0,
                'verdict_history': [],
                'best_candidate_id': None,
                'best_candidate_history': [],
                'position_alert_keys': [],
            }
        self.last_state = {
            'action': state.get('action', 'WAIT'),
            'strategy': state.get('strategy'),
            'confidence': state.get('confidence', 0),
            'timestamp': state.get('timestamp', 0),
            'cooldown_until': state.get('cooldown_until', 0),
            'best_candidate_id': state.get('best_candidate_id'),
        }
        self.verdict_history = list(state.get('verdict_history', []) or [])
        self.best_candidate_history = list(state.get('best_candidate_history', []) or [])
        self.position_alert_keys = set(state.get('position_alert_keys', []) or [])

    def _best_executable_candidate(self, result):
        watchlist = result.get('watchlist') or []
        if not isinstance(watchlist, list):
            return None
        for cand in watchlist:
            if not isinstance(cand, dict):
                continue
            if cand.get('capitalBlocked'):
                continue
            if cand.get('executionReady') is False:
                continue
            if cand.get('entryAction') == 'BLOCKED' or cand.get('blocked') is True:
                continue
            if cand.get('directionSafe') is False:
                continue
            if not cand.get('type'):
                continue
            return cand
        return None

    def _priority_rank(self, priority):
        order = {
            'urgent': 4,
            'important': 3,
            'entry': 2,
            'routine': 1,
        }
        return order.get(str(priority or '').lower(), 0)

    def _position_alert_decision_type(self, alert):
        key = str((alert or {}).get('key') or '')
        if key.startswith('POS_TARGET_') or key.startswith('POS_BOOK_'):
            return 'POSITION_EXIT'
        return 'POSITION_RISK'

    def _position_alert_notification_kind(self, alert):
        return 'EXIT' if self._position_alert_decision_type(alert) == 'POSITION_EXIT' else 'RISK'

    def _resolve_source_mode(self, result, verdict, best):
        decision_source = str(
            verdict.get('decision_source')
            or verdict.get('decisionSource')
            or (best.get('decision_source') if isinstance(best, dict) else None)
            or (best.get('decisionSource') if isinstance(best, dict) else None)
            or result.get('decision_source')
            or result.get('decisionSource')
            or 'DEFAULT_BRAIN_MATH'
        )
        if decision_source in ('ML_ADVISORY', 'ML_UNSURE_FALLBACK', 'LLM_ADVISORY'):
            return 'teacher_plus_llm'
        if decision_source == 'TEACHER_ONLY':
            return 'teacher_only'
        return 'deterministic_brain'

    def _contract_base(self, result, ctx, best, verdict):
        action = verdict.get('action', 'WAIT')
        strategy = verdict.get('strategy')
        confidence = _safe_num(verdict.get('confidence'), 0)
        teacher_r_score = None
        teacher_bucket_n = None
        teacher_coverage = None
        if isinstance(best, dict):
            teacher_r_score = _safe_num(
                best.get('teacher_r_score')
                if best.get('teacher_r_score') is not None
                else best.get('r_multiple'),
                None,
            )
            teacher_bucket_n = best.get('teacher_bucket_n')
            teacher_coverage = best.get('teacher_coverage')
        lane = None
        candidate_id = None
        if isinstance(best, dict):
            lane = best.get('lane')
            candidate_id = best.get('id')
        if not lane:
            lane = verdict.get('dominant_lane')
        return {
            'schema': 1,
            'decision_type': 'WAIT' if action == 'WAIT' else 'TRADE',
            'notify_user': False,
            'notification_kind': 'NONE',
            'candidate_id': candidate_id,
            'lane': lane,
            'strategy_type': strategy,
            'confidence': round(confidence, 2),
            'teacher_r_score': teacher_r_score,
            'teacher_bucket_n': teacher_bucket_n,
            'teacher_coverage': teacher_coverage,
            'title': '',
            'body': '',
            'reason_code': 'NO_ACTIONABLE_CHANGE',
            'reason_text': 'No actionable trading notification for this poll.',
            'source_mode': self._resolve_source_mode(result, verdict, best),
            'poll_id': ctx.get('poll_id') or ctx.get('pollCount'),
            'session_date': ctx.get('session_date') or ctx.get('sessionDate'),
            'sound_class': 'routine',
            'alert_key': None,
        }

    def _build_contract(self, base, **updates):
        contract = dict(base or {})
        contract.update(updates)
        return contract

    def _alert_to_contract(self, alert, base, decision_type, notification_kind, reason_code, reason_text):
        return self._build_contract(
            base,
            decision_type=decision_type,
            notify_user=True,
            notification_kind=notification_kind,
            title=alert.get('title', ''),
            body=alert.get('message', ''),
            reason_code=reason_code,
            reason_text=reason_text,
            sound_class=alert.get('sound_class') or self._compute_sound_class(
                alert.get('urgency'), base.get('confidence', 0)
            ),
            alert_key=alert.get('dedupe_bucket'),
        )

    def _position_alert_to_contract(self, alert, base):
        return self._build_contract(
            base,
            decision_type=self._position_alert_decision_type(alert),
            notify_user=True,
            notification_kind=self._position_alert_notification_kind(alert),
            title=alert.get('title', ''),
            body=alert.get('body', ''),
            reason_code=alert.get('key') or 'POSITION_ALERT',
            reason_text='Open position crossed a risk/exit threshold.',
            sound_class=alert.get('priority', 'urgent'),
            alert_key=alert.get('key'),
            candidate_id=(str(alert.get('key') or '').split('_', 2)[-1] if alert.get('key') else None),
        )

    def snapshot_state(self):
        full = dict(self.last_state)
        full['verdict_history'] = list(self.verdict_history)
        full['best_candidate_history'] = list(self.best_candidate_history)
        full['position_alert_keys'] = sorted(self.position_alert_keys)
        return full

    def process_contract(self, result, ctx):
        current_time = ctx.get('now_ms', 0)
        verdict = result.get('verdict', {}) or {}
        action = verdict.get('action', 'WAIT')
        strategy = verdict.get('strategy')
        confidence = _safe_num(verdict.get('confidence'), 0)
        best = self._best_executable_candidate(result)
        best_id = best.get('id') if isinstance(best, dict) else None
        best_type = best.get('type') if isinstance(best, dict) else None
        best_index = best.get('index') if isinstance(best, dict) else None
        base = self._contract_base(result, ctx, best, verdict)

        position_alerts = []
        for alert in (result.get('alerts') or []):
            if isinstance(alert, dict) and alert.get('category') == 'POSITION':
                position_alerts.append(alert)
        position_alerts.sort(key=lambda item: self._priority_rank(item.get('priority')), reverse=True)
        current_position_keys = {
            str(alert.get('key') or '').strip()
            for alert in position_alerts
            if str(alert.get('key') or '').strip()
        }
        unseen_position_alerts = [
            alert for alert in position_alerts
            if str(alert.get('key') or '').strip() not in self.position_alert_keys
        ]

        self.verdict_history.append(action)
        if len(self.verdict_history) > 6:
            self.verdict_history.pop(0)
        self.best_candidate_history.append(best_id)
        if len(self.best_candidate_history) > 6:
            self.best_candidate_history.pop(0)

        contract = None

        if current_time < self.last_state['cooldown_until']:
            contract = self._build_contract(
                base,
                reason_code='COOLDOWN_ACTIVE',
                reason_text='Notification cooldown is active.',
            )
        elif self._is_market_choppy():
            self.last_state['cooldown_until'] = current_time + (45 * 60 * 1000)
            self.last_state['timestamp'] = current_time
            alert = self._build_alert(
                urgency="WARNING",
                title="Market Whipsawing",
                message="Detected 3 direction flips in 30 minutes. Muting setup alerts for 45 mins.",
                sound_class='warning'
            )
            contract = self._alert_to_contract(
                alert,
                base,
                decision_type='WARNING',
                notification_kind='UPDATE',
                reason_code='MARKET_WHIPSAW',
                reason_text='Market whipsaw detected; setup alerts muted temporarily.',
            )
        elif action == self.last_state['action'] and strategy == self.last_state['strategy'] and best_id == self.last_state.get('best_candidate_id'):
            if abs(confidence - self.last_state['confidence']) >= 15:
                msg = f"{best_index or ''} {best_type or strategy} conviction shifted from {self.last_state['confidence']}% to {confidence}%."
                self._update_state(action, strategy, confidence, current_time, best_id)
                alert = self._build_alert("UPDATE", "Conviction Update", msg,
                                          sound_class='update')
                contract = self._alert_to_contract(
                    alert,
                    base,
                    decision_type='POSITION_UPDATE',
                    notification_kind='UPDATE',
                    reason_code='CONVICTION_SHIFT',
                    reason_text='Conviction changed materially for the same setup.',
                )
            else:
                contract = self._build_contract(
                    base,
                    decision_type='WAIT' if action == 'WAIT' else 'TRADE',
                    reason_code='UNCHANGED_SETUP',
                    reason_text='Same setup and conviction; no new notification.',
                )
        elif self.last_state['action'] != 'WAIT' and (action == 'WAIT' or best_id is None):
            if len(self.verdict_history) >= 2 and self.verdict_history[-2:] == ['WAIT', 'WAIT'] and \
               len(self.best_candidate_history) >= 2 and self.best_candidate_history[-2:] == [None, None]:
                msg = f"Previous {self.last_state['strategy']} thesis invalidated by contrary price action."
                self._update_state('WAIT', None, 0, current_time, None)
                alert = self._build_alert("INFO", "Setup Invalidated", msg,
                                          sound_class='routine')
                contract = self._alert_to_contract(
                    alert,
                    base,
                    decision_type='INVALIDATED',
                    notification_kind='UPDATE',
                    reason_code='SETUP_INVALIDATED',
                    reason_text='Prior setup became invalid after repeated WAIT confirmation.',
                )
            else:
                contract = self._build_contract(
                    base,
                    decision_type='WAIT',
                    reason_code='WAIT_TRANSITION',
                    reason_text='Verdict transitioned to WAIT without stable invalidation yet.',
                )
        elif action != 'WAIT' and confidence >= 55 and ctx.get('entry_window_active', False) and best_id:
            stable_action = len(self.verdict_history) >= 2 and self.verdict_history[-2:] == [action, action]
            stable_best = len(self.best_candidate_history) >= 2 and self.best_candidate_history[-2:] == [best_id, best_id]
            if stable_action and stable_best:
                msg = f"Entry Window OPEN. Best setup: {best_index or ''} {best_type or strategy} with {confidence}% conviction."
                self._update_state(action, strategy, confidence, current_time, best_id)
                alert = self._build_alert(
                    "HIGH", "New Setup Ready", msg,
                    sound_class=self._compute_sound_class('HIGH', confidence)
                )
                contract = self._alert_to_contract(
                    alert,
                    base,
                    decision_type='TRADE',
                    notification_kind='ENTRY',
                    reason_code='SETUP_READY',
                    reason_text='Stable actionable setup confirmed across consecutive polls.',
                )
            else:
                contract = self._build_contract(
                    base,
                    decision_type='TRADE',
                    reason_code='SETUP_NOT_STABLE',
                    reason_text='Actionable setup exists but has not met stability confirmation yet.',
                )
        else:
            contract = self._build_contract(
                base,
                decision_type='WAIT' if action == 'WAIT' else 'TRADE',
                reason_code='NO_ACTIONABLE_STATE_CHANGE',
                reason_text='No actionable trading notification for this poll.',
            )

        self.position_alert_keys = current_position_keys

        if unseen_position_alerts:
            contract = self._position_alert_to_contract(unseen_position_alerts[0], base)

        return {
            'brain_notification': contract,
            'agent_state': self.snapshot_state(),
        }

    def _is_market_choppy(self):
        # Tracks action-level flips only (e.g., 'SELL PREMIUM' ↔ 'BUY PREMIUM').
        # Strategy-level flips within the same action (BEAR_CALL ↔ BULL_PUT)
        # are naturally suppressed by the two-consecutive-poll confirmation gate.
        flips = 0
        for i in range(1, len(self.verdict_history)):
            if self.verdict_history[i] != self.verdict_history[i-1] and self.verdict_history[i] != 'WAIT':
                flips += 1
        return flips >= 3

    def _compute_sound_class(self, urgency, confidence):
        """
        Determines the notification sound class for routing in Kotlin.
        Trading semantics stay in brain.py; Kotlin routes presentation only.
        """
        if urgency == 'HIGH':
            return 'perfect' if confidence >= 75 else 'entry'
        elif urgency == 'UPDATE':
            return 'update'
        elif urgency == 'WARNING':
            return 'warning'
        elif urgency == 'ERROR':
            return 'urgent'
        else:
            return 'routine'

    def _update_state(self, action, strategy, confidence, ts, best_candidate_id=None):
        self.last_state.update({
            'action': action, 'strategy': strategy,
            'confidence': confidence, 'timestamp': ts,
            'best_candidate_id': best_candidate_id,
        })

    def _build_alert(self, urgency, title, message, sound_class=None):
        return {
            'type': 'NOTIFICATION',
            'urgency': urgency,
            'title': title,
            'message': message,
            'timestamp': self.last_state['timestamp'],
            'sound_class': sound_class or self._compute_sound_class(
                urgency, self.last_state.get('confidence', 0)
            ),
        }


# Global NotificationAgent singleton (persists across poll cycles in Chaquopy)
_NOTIFICATION_AGENT = NotificationAgent()

def notification_agent_state_json():
    """Returns the agent's full state including verdict_history for persistence."""
    try:
        return json.dumps(_NOTIFICATION_AGENT.snapshot_state())
    except Exception:
        return 'null'


def brain_notification_process(result, ctx):
    """Unified trading-notification contract plus shadow-compatible legacy stream."""
    try:
        result = _bridge_json_obj(result) or {}
        ctx = _bridge_json_obj(ctx) or {}
        payload = _NOTIFICATION_AGENT.process_contract(result, ctx)
        return json.dumps(payload)
    except Exception as e:
        fallback = {
            'brain_notification': {
                'schema': 1,
                'decision_type': 'WAIT',
                'notify_user': False,
                'notification_kind': 'NONE',
                'candidate_id': None,
                'lane': None,
                'strategy_type': None,
                'confidence': 0,
                'teacher_r_score': None,
                'title': '',
                'body': '',
                'reason_code': 'PROCESS_ERROR',
                'reason_text': str(e),
                'source_mode': 'deterministic_brain',
                'poll_id': None,
                'session_date': None,
                'sound_class': 'urgent',
                'alert_key': None,
            },
            'agent_state': {},
        }
        return json.dumps(fallback)


def reset_notification_agent(state_json='null'):
    """Reset agent state (used on app restart). Pass previous JSON state to restore."""
    global _NOTIFICATION_AGENT
    if state_json and state_json != 'null':
        try:
            state = json.loads(state_json)
            _NOTIFICATION_AGENT = NotificationAgent(state)
        except Exception:
            _NOTIFICATION_AGENT = NotificationAgent()
    else:
        _NOTIFICATION_AGENT = NotificationAgent()
    return 'ok'
