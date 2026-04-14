"""
ml_train.py — Market Radar v2.1 Nightly Training Script
========================================================
Called by Kotlin TrainingService via Chaquopy at 11 PM nightly.

Usage (Kotlin via Chaquopy):
    Python.getModule("ml_train").callAttr("run",
        "/data/backtest_trades.csv",   # primary training data
        "/data/app_trades.json",        # live app trades (may be empty)
        "/data/ml_model.json",          # current model (replaced if better)
        log_fn)                         # optional Kotlin callback for logs

Usage (CLI for testing):
    python3 ml_train.py /data/backtest.csv /data/app_trades.json /data/model.json

Returns JSON string with:
    {success, deployed, accuracy_new, accuracy_old, n_train, duration_sec, reason}
"""

import json
import math
import os
import csv as _csv
import time

# ── import ml_engine from same directory ────────────────────────────────────
try:
    import ml_engine as _ml
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ml_engine as _ml

# ── Constants ────────────────────────────────────────────────────────────────

MIN_IMPROVEMENT  = 0.005   # deploy new model only if accuracy improves by >0.5%
MIN_TRAIN_ROWS   = 500     # skip training if less than this (not enough data)
APP_TRADE_WEIGHT = 3       # each real app trade replicated 3× in training mix
MAX_APP_ROWS     = 500     # cap app trades to prevent distribution shift


# ─────────────────────────────────────────────────────────────────────────────
# APP TRADE CONVERTER
# ─────────────────────────────────────────────────────────────────────────────

def _app_trade_to_row(t):
    """
    Convert a live app trade dict (trades_v2 schema) to backtest-CSV-compatible row.
    Maps the richer 61-column app format to the 41-column training format.
    Returns None if essential fields missing or trade not closed.
    """
    # Must be closed with a known outcome
    pnl = t.get('pnl') or t.get('net_pnl') or t.get('final_pnl')
    if pnl is None:
        return None
    try:
        pnl = float(pnl)
    except:
        return None

    won = pnl > 0

    # Strategy type normalisation
    stype = str(t.get('strategy', t.get('type', ''))).upper()
    STRAT_MAP = {
        'BEAR CALL': 'BEAR_CALL', 'BEARCALL': 'BEAR_CALL',
        'BULL PUT':  'BULL_PUT',  'BULLPUT':  'BULL_PUT',
        'BEAR PUT':  'BEAR_PUT',  'BEARPUT':  'BEAR_PUT',
        'BULL CALL': 'BULL_CALL', 'BULLCALL': 'BULL_CALL',
        'IRON CONDOR': 'IRON_CONDOR', 'IRONCONDOR': 'IRON_CONDOR',
        'IRON BUTTERFLY': 'IRON_BUTTERFLY',
    }
    stype = STRAT_MAP.get(stype.replace('_', ' '), stype)

    mode = str(t.get('mode', 'intraday')).lower()
    vix  = t.get('entry_vix') or t.get('vix') or 17.0
    spot = t.get('entry_spot') or t.get('spot') or 0
    dte  = t.get('dte') or t.get('tDTE') or 3

    entry_credit = (t.get('entry_credit') or t.get('net_premium') or
                    t.get('netPremium') or 0)
    max_profit   = t.get('max_profit') or t.get('maxProfit') or 0
    max_loss     = t.get('max_loss')   or t.get('maxLoss')   or 0
    width        = t.get('width') or 200
    sigma_away   = t.get('sigma_from_atm') or t.get('sigmaOTM') or t.get('sigma_away') or 0
    index        = str(t.get('index', 'NF'))

    is_credit = stype in ('BEAR_CALL', 'BULL_PUT', 'IRON_CONDOR', 'IRON_BUTTERFLY')
    legs = 4 if 'IRON' in stype else 2

    # Derive vix_regime
    try:
        v = float(vix)
        if v >= 20:   vix_regime = 'HIGH (20-25)'
        elif v >= 15: vix_regime = 'NORMAL (15-20)'
        else:         vix_regime = 'LOW (<15)'
    except:
        vix_regime = 'NORMAL (15-20)'

    return {
        'date': str(t.get('date', '')),
        'index': index,
        'strategy': stype,
        'mode': mode,
        'sell_strike': t.get('sell_strike') or t.get('sellStrike') or 0,
        'buy_strike':  t.get('buy_strike')  or t.get('buyStrike')  or 0,
        'width': width,
        'legs': legs,
        'entry_credit': entry_credit,
        'max_profit': max_profit,
        'max_loss': max_loss,
        'paper_pnl': max_profit if won else -max_loss,
        'cost': 0,
        'net_pnl': pnl,
        'won': str(won),
        'exit_reason': str(t.get('exit_reason', t.get('exitReason', 'CLOSE'))),
        'target_hit': str(won),
        'stop_hit': str(not won),
        'vix': vix,
        'spot': spot,
        'sigma_away': sigma_away,
        'gap_sigma': t.get('gap_sigma') or t.get('sigmaScore') or 0,
        'weekday': t.get('weekday') or 0,
        'dte': dte,
        'sell_strike2': t.get('sell_strike2') or '',
        'buy_strike2':  t.get('buy_strike2')  or '',
        'vix_regime': vix_regime,
        'is_credit': str(is_credit),
        'day_group': 'Mon-Wed',   # live trades: approximate
        'inside_day': '',
        'outside_day': '',
        'uptrend': '',
        'downtrend': '',
        'bullish_close': '',
        'bearish_close': '',
        'day_range_sigma': t.get('day_range_sigma') or '',
        'consec_days': '',
        'day_direction': str(t.get('day_direction', 'FLAT')).upper(),
        'day_range': str(t.get('day_range', 'NORMAL')).upper(),
        'day_vix': 'HIGH' if float(vix) >= 20 else ('LOW' if float(vix) < 15 else 'NORMAL'),
        'move_sigma': t.get('move_sigma') or 0,
    }


def _load_app_trades(path):
    """Load app trades JSON → list of training rows."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        trades = raw if isinstance(raw, list) else raw.get('trades', [])
        rows = []
        for t in trades[:MAX_APP_ROWS]:
            r = _app_trade_to_row(t)
            if r:
                rows.append(r)
        return rows
    except Exception as e:
        return []


def _load_csv_rows(path):
    """Load backtest CSV rows as list of dicts."""
    if not path or not os.path.exists(path):
        return []
    try:
        rows = []
        with open(path, 'r', newline='', encoding='utf-8') as f:
            reader = _csv.DictReader(f)
            for row in reader:
                rows.append(dict(row))
        return rows
    except Exception as e:
        return []


def _evaluate_on_holdout(engine, rows):
    """
    Quick holdout accuracy: last 15% of rows.
    Returns accuracy float.
    """
    n      = len(rows)
    val_n  = max(8, min(int(n * 0.15), n // 4))
    tr_n   = n - val_n
    val_rows = rows[tr_n:]

    correct = 0
    for r in val_rows:
        won = str(r.get('won', '')).lower() in ('true', '1')
        p, _, _ = engine.predict(r)
        if (p >= engine.thr_take) == won or (p < engine.thr_watch) == (not won):
            correct += 1
    return correct / max(len(val_rows), 1)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def run(backtest_csv_path, app_trades_path, model_path, log_fn=None):
    """
    Nightly training run.

    Steps:
      1. Load backtest CSV + app trades
      2. Mix: app trades replicated APP_TRADE_WEIGHT times (recency boost)
      3. Train new MLEngine
      4. Compare holdout accuracy to deployed model
      5. Deploy new model if accuracy improved by MIN_IMPROVEMENT
      6. Return JSON result string

    Returns: JSON string with {success, deployed, accuracy_new, accuracy_old,
                                n_train, n_app, duration_sec, reason}
    """
    def log(msg):
        if log_fn:
            try: log_fn(msg)
            except: pass

    t0 = time.time()
    result = {
        'success': False, 'deployed': False,
        'accuracy_new': 0.0, 'accuracy_old': 0.0,
        'n_train': 0, 'n_app': 0,
        'duration_sec': 0.0, 'reason': '',
        'top_features': [],
    }

    try:
        # ── 1. Load data ─────────────────────────────────────────────────────
        log("ml_train: loading backtest CSV …")
        bt_rows   = _load_csv_rows(backtest_csv_path)
        log(f"ml_train: {len(bt_rows)} backtest rows loaded")

        log("ml_train: loading app trades …")
        app_rows  = _load_app_trades(app_trades_path)
        log(f"ml_train: {len(app_rows)} app trades loaded")
        result['n_app'] = len(app_rows)

        # ── 2. Mix ───────────────────────────────────────────────────────────
        # App trades go last (most recent), replicated for recency weight
        # Issue 1 (Antigravity): row duplication destroys GBT MSE splitting.
        # Use sample_weights instead — app rows weighted 3x, backtest weighted 1x.
        all_rows       = bt_rows + app_rows   # app_rows appended ONCE, not duplicated
        sample_weights = [1.0] * len(bt_rows) + [float(APP_TRADE_WEIGHT)] * len(app_rows)

        n = len(all_rows)
        log(f"ml_train: {n} rows ({len(bt_rows)} backtest + {len(app_rows)} live @ weight={APP_TRADE_WEIGHT})")

        if n < MIN_TRAIN_ROWS:
            result['reason'] = f'Insufficient data: {n} rows < {MIN_TRAIN_ROWS} minimum'
            log(f"ml_train: SKIPPED — {result['reason']}")
            result['duration_sec'] = round(time.time() - t0, 1)
            return json.dumps(result)

        # ── 3. Train ─────────────────────────────────────────────────────────
        log("ml_train: starting training (GBT + NN) …")
        new_engine = _ml.train_from_csv.__wrapped__(all_rows, log_fn=log) if hasattr(_ml.train_from_csv, '__wrapped__') \
                     else _train_from_rows(all_rows, log_fn=log, sample_weights=sample_weights)
        result['n_train'] = n

        # ── 4. Compare to deployed model ─────────────────────────────────────
        acc_new = getattr(new_engine.gbt, 'val_acc', 0.0)
        result['accuracy_new'] = round(acc_new, 4)

        acc_old = 0.0
        if os.path.exists(model_path):
            try:
                old_engine = _ml.load_model(model_path)
                acc_old    = getattr(old_engine.gbt, 'val_acc', 0.0)
                log(f"ml_train: old model accuracy: {acc_old:.3f}")
            except Exception as e:
                log(f"ml_train: could not load old model: {e}")
        result['accuracy_old'] = round(acc_old, 4)

        # ── 5. Deploy ────────────────────────────────────────────────────────
        improvement = acc_new - acc_old
        if acc_old == 0.0 or improvement >= MIN_IMPROVEMENT:
            _ml.save_model(new_engine, model_path)
            result['deployed'] = True
            result['reason'] = (f'Deployed: accuracy {acc_old:.3f} → {acc_new:.3f} '
                                f'(+{improvement:.3f})')
            log(f"ml_train: {result['reason']}")
        else:
            result['reason'] = (f'Kept old model: improvement {improvement:+.3f} '
                                f'below threshold {MIN_IMPROVEMENT:.3f}')
            log(f"ml_train: {result['reason']}")

        # ── 6. Feature importance ─────────────────────────────────────────────
        result['top_features'] = [
            {'name': fn, 'importance': round(imp, 4)}
            for fn, imp in new_engine.gbt.top_features(5)
        ]

        result['success'] = True

    except Exception as ex:
        result['reason']  = f'Exception: {ex}'
        result['success'] = False
        log(f"ml_train: ERROR — {ex}")

    result['duration_sec'] = round(time.time() - t0, 1)
    log(f"ml_train: done in {result['duration_sec']}s  deployed={result['deployed']}")
    return json.dumps(result)


def _train_from_rows(rows, log_fn=None, sample_weights=None):
    """
    Like train_from_csv but accepts a list of dicts directly (no file I/O).
    Internal — used by run() after mixing backtest + app trades.
    """
    def log(msg):
        if log_fn: log_fn(msg)

    # Parse target
    y = []
    clean = []
    for r in rows:
        v = r.get('won', '')
        if str(v).strip().lower() in ('true', '1'):
            y.append(1); clean.append(r)
        elif str(v).strip().lower() in ('false', '0'):
            y.append(0); clean.append(r)

    n = len(clean)
    if n == 0:
        raise ValueError('No valid rows with won=True/False')

    engine = _ml.MLEngine()
    engine.n_train       = n
    engine.base_win_rate = sum(y) / n

    engine.feature_engine.fit(clean)
    X = [engine.feature_engine.extract(r) for r in clean]

    engine.regime.fit(clean, log_fn=log_fn)
    engine.gbt.fit(X, y, log_fn=log, sample_weights=sample_weights)
    engine.nn.fit(X, y, log_fn=log)

    gbt_preds = [engine.gbt.predict_proba(X[i]) for i in range(n)]
    nn_preds  = [engine.nn.predict_proba(X[i]) for i in range(n)]
    regimes   = [engine.regime.predict(clean[i])[0] for i in range(n)]
    engine.meta.calibrate(gbt_preds, nn_preds, regimes, y)

    # Holdout thresholds
    val_n  = max(8, min(int(n * 0.15), n // 4))
    tr_n   = n - val_n
    val_preds = []
    for i in range(tr_n, n):
        feat = engine.feature_engine.extract(clean[i])
        pg   = engine.gbt.predict_proba(feat)
        pn   = engine.nn.predict_proba(feat) if engine.nn.trained else 0.5
        reg  = regimes[i]
        pm   = engine.meta.predict(pg, pn, reg)
        val_preds.append((pm, y[i]))

    best_thr = 0.65
    for thr_step in range(70, 42, -2):
        thr  = thr_step / 100.0
        take = [(p, yi) for p, yi in val_preds if p >= thr]
        if len(take) < 20: continue
        prec = sum(yi for _, yi in take) / len(take)
        if prec >= 0.96:
            best_thr = thr
            break

    engine.thr_take  = best_thr
    engine.thr_watch = max(0.46, best_thr - 0.12)
    engine.trained   = True
    return engine


# ─────────────────────────────────────────────────────────────────────────────
# ONLINE LEARNING — gradient update after a single closed trade
# ─────────────────────────────────────────────────────────────────────────────

def online_update(model_path, trade_dict, log_fn=None):
    """
    Lightweight gradient update after a trade closes.
    Updates only the NN (GBT can't be updated online — trees are immutable).
    Fast: <1 sec on S23 Ultra.

    trade_dict: app trade dict with 'won' key set.
    Returns JSON: {success, p_before, p_after, direction_correct}
    """
    def log(msg):
        if log_fn:
            try: log_fn(msg)
            except: pass

    result = {'success': False, 'p_before': 0.0, 'p_after': 0.0,
               'direction_correct': False}
    try:
        engine = _ml.load_model(model_path)
        row    = _app_trade_to_row(trade_dict)
        if row is None:
            result['reason'] = 'Could not convert trade'
            return json.dumps(result)

        won = str(trade_dict.get('won', '')).lower() in ('true', '1')
        feat = engine.feature_engine.extract(row)

        # Score before update
        p_before, _, _ = engine.predict(row)
        result['p_before'] = round(p_before, 4)

        # Single-sample gradient step on the NN only (3 micro-steps, lr=0.005)
        engine.nn.online_step(feat, 1 if won else 0, lr=0.005, n_steps=3)

        # Score after update
        p_after, _, _ = engine.predict(row)
        result['p_after']            = round(p_after, 4)
        result['direction_correct']  = (p_after > p_before) == won
        result['success']            = True

        # Save updated model
        _ml.save_model(engine, model_path)
        log(f"online_update: p {p_before:.3f} → {p_after:.3f}  won={won}")

    except Exception as ex:
        result['reason'] = str(ex)
        log(f"online_update: ERROR {ex}")

    return json.dumps(result)


# ─────────────────────────────────────────────────────────────────────────────
# QUICK VALIDATION — called by Kotlin after loading to confirm model is usable
# ─────────────────────────────────────────────────────────────────────────────

def validate_model(model_path):
    """
    Confirm model loads and predicts without error.
    Returns JSON: {ok, version, n_train, thr_take, thr_watch, base_wr}
    """
    try:
        engine = _ml.load_model(model_path)
        # Smoke test: one prediction
        sample = {
            'strategy': 'BEAR_CALL', 'mode': 'intraday', 'vix': 18.0,
            'sigma_away': 0.65, 'gap_sigma': 0.0, 'dte': 3,
            'entry_credit': 60, 'width': 200, 'move_sigma': -0.1,
            'day_range_sigma': 0.8, 'consec_days': 2,
            'max_profit': 3900, 'max_loss': 9100, 'legs': 2,
            'is_credit': True, 'vix_regime': 'NORMAL (15-20)',
            'day_group': 'Mon-Wed', 'day_direction': 'DOWN',
            'day_range': 'NORMAL', 'day_vix': 'NORMAL', 'weekday': 1,
        }
        p, reg, _ = engine.predict(sample)
        assert 0 < p < 1, f'Bad prediction: {p}'
        return json.dumps({
            'ok': True,
            'version':   engine.ml_version,
            'n_train':   engine.n_train,
            'thr_take':  engine.thr_take,
            'thr_watch': engine.thr_watch,
            'base_wr':   round(engine.base_win_rate, 3),
            'sample_p':  round(p, 3),
            'regime':    reg,
        })
    except Exception as ex:
        return json.dumps({'ok': False, 'error': str(ex)})


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == 'validate':
        model_path = sys.argv[2] if len(sys.argv) > 2 else 'model.json'
        print(validate_model(model_path))
        sys.exit(0)

    if len(sys.argv) >= 2 and sys.argv[1] == 'online':
        # ml_train.py online model.json '{"strategy":"BEAR_CALL","won":"True",...}'
        model_path  = sys.argv[2] if len(sys.argv) > 2 else 'model.json'
        trade_json  = sys.argv[3] if len(sys.argv) > 3 else '{}'
        trade_dict  = json.loads(trade_json)
        print(online_update(model_path, trade_dict, log_fn=print))
        sys.exit(0)

    # Default: full training
    bt_path    = sys.argv[1] if len(sys.argv) > 1 else 'backtest_trades.csv'
    app_path   = sys.argv[2] if len(sys.argv) > 2 else 'app_trades.json'
    model_path = sys.argv[3] if len(sys.argv) > 3 else 'model.json'

    result = run(bt_path, app_path, model_path, log_fn=print)
    print('\nResult:', result)
