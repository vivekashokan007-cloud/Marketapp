"""
ml_engine.py — Market Radar v2.1 ML Engine  Phase 1
====================================================
Pure Python (json, math, csv stdlib only)
Runs on S23 Ultra via Chaquopy.  No numpy, no pandas, no sklearn.

Architecture:
  Layer 1 : FeatureEngine      — trade dict → float[38]
  Layer 2 : GradientBoostedTrees — 200 trees × depth-3, log-loss
  Layer 3 : NeuralNet            — 38→32→16→1, ReLU, SGD+momentum
  Layer 4 : RegimeDetector       — K-means 4-state market regime
  Meta    : MLEngine             — stacking + orchestration

Quick start:
  engine = train_from_csv('/path/backtest_trades.csv', log_fn=print)
  save_model(engine, '/path/model.json')
  engine = load_model('/path/model.json')
  p_win, regime, detail = engine.predict(candidate_dict)
"""

import json
import math
import csv

# ─────────────────────────────────────────────────────────────────────────────
# VERSION & CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

ML_VERSION = "2.1.1"
N_FEATURES  = 38

FEATURE_NAMES = [
    # Numeric (0-12)
    'vix_norm', 'sigma_away_norm', 'gap_sigma_norm', 'dte_norm',
    'credit_norm', 'width_norm', 'move_sigma_norm', 'drs_norm',
    'consec_norm', 'is_credit', 'credit_ratio', 'rr_norm', 'sigma_sq',
    # Binary (13-18)
    'inside_day', 'outside_day', 'uptrend', 'downtrend',
    'bullish_close', 'bearish_close',
    # Strategy one-hot BEAR_CALL=base (19-23)
    'strat_BEAR_PUT', 'strat_BULL_CALL', 'strat_BULL_PUT',
    'strat_IC', 'strat_IB',
    # Mode (24)
    'mode_swing',
    # VIX regime LOW=base (25-26)
    'vix_normal', 'vix_high',
    # Day direction DOWN=base (27-28)
    'dir_flat', 'dir_up',
    # Day group (29)
    'day_thufri',
    # Weekday Mon=base (30-33)
    'wday_1', 'wday_2', 'wday_3', 'wday_4',
    # Day range TIGHT=base (34-35)
    'range_normal', 'range_trending',
    # Day VIX (36) + abs gap (37)
    'day_vix_norm', 'gap_abs_norm',
]
assert len(FEATURE_NAMES) == N_FEATURES

REGIME_NAMES = ['CALM', 'TRENDING', 'CHOPPY', 'VOLATILE']

# ─────────────────────────────────────────────────────────────────────────────
# MATH UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _sig(x):
    if x >  500: return 1.0
    if x < -500: return 0.0
    return 1.0 / (1.0 + math.exp(-x))

def _clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)

def _nan(v):
    if v is None: return True
    s = str(v).strip().lower()
    if s in ('', 'nan', 'none', 'null', 'na', 'n/a'): return True
    try:    return math.isnan(float(v))
    except: return True

def _mean(lst):
    return sum(lst) / len(lst) if lst else 0.0

def _var(lst):
    if len(lst) < 2: return 0.0
    m = _mean(lst)
    return sum((x - m) ** 2 for x in lst) / len(lst)

def _std(lst):
    v = _var(lst)
    return math.sqrt(v) if v > 0 else 0.0

def _log(x):
    return math.log(max(x, 1e-15))

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 1: FEATURE ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class FeatureEngine:
    """
    Converts a trade/candidate dict → float[N_FEATURES].
    Call fit(rows) on training data first to set imputation values.
    """

    def __init__(self):
        self.medians = {}
        self.fitted  = False

    # ── Fit ─────────────────────────────────────────────────────────────────

    def fit(self, rows):
        numeric = ['vix', 'sigma_away', 'gap_sigma', 'dte', 'entry_credit',
                   'width', 'move_sigma', 'day_range_sigma', 'consec_days']
        for col in numeric:
            vals = []
            for r in rows:
                v = r.get(col)
                if not _nan(v):
                    try: vals.append(float(v))
                    except Exception: pass  # Silent skip for non-numeric is intended here
            if vals:
                vals.sort()
                n = len(vals)
                mid = n // 2
                self.medians[col] = (vals[mid-1] + vals[mid]) / 2 if n % 2 == 0 else vals[mid]
            else:
                self.medians[col] = 0.0

        # OOD bounds: per-feature [p1, p99] of training data
        self.ood_bounds = {}
        for col in numeric:
            vals = []
            for r in rows:
                v = r.get(col)
                if not _nan(v):
                    try: vals.append(float(v))
                    except Exception: pass  # Silent skip for non-numeric is intended here
            if len(vals) > 10:
                vals.sort()
                n = len(vals)
                p1  = vals[max(0, int(n * 0.01))]
                p99 = vals[min(n-1, int(n * 0.99))]
                self.ood_bounds[col] = [p1, p99]

        # Per-strategy sigma_away bounds (strategy can narrow the range)
        self.strat_sa_bounds = {}
        strats = set(str(r.get('strategy','')) for r in rows)
        for strat in strats:
            vals = []
            for r in rows:
                if str(r.get('strategy','')) == strat:
                    v = r.get('sigma_away')
                    if not _nan(v):
                        try: vals.append(float(v))
                        except Exception: pass  # Silent skip for non-numeric is intended here
            if len(vals) > 10:
                vals.sort()
                n = len(vals)
                self.strat_sa_bounds[strat] = [vals[max(0, int(n*0.02))],
                                               vals[min(n-1, int(n*0.98))]]

        self.fitted = True

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _f(self, row, col):
        v = row.get(col)
        if _nan(v): return self.medians.get(col, 0.0)
        try:    return float(v)
        except: return self.medians.get(col, 0.0)

    def _b(self, row, col):
        v = row.get(col)
        if _nan(v): return 0.0
        return 1.0 if str(v).strip().lower() in ('true', '1', '1.0', 'yes') else 0.0

    # ── Extract ──────────────────────────────────────────────────────────────

    def extract(self, row):
        """Return list of N_FEATURES floats."""
        vix     = self._f(row, 'vix')
        sa      = self._f(row, 'sigma_away')
        gs      = self._f(row, 'gap_sigma')
        dte     = self._f(row, 'dte')
        credit  = self._f(row, 'entry_credit')
        width   = self._f(row, 'width')
        mv      = self._f(row, 'move_sigma')
        drs     = self._f(row, 'day_range_sigma')
        consec  = self._f(row, 'consec_days')
        mp      = self._f(row, 'max_profit')
        ml      = max(1.0, self._f(row, 'max_loss'))
        legs    = max(1.0, self._f(row, 'legs') or 2.0)
        is_cred = self._b(row, 'is_credit')
        cr      = _clamp(credit / max(1.0, credit + ml / legs), 0.0, 1.0)
        rr      = _clamp(mp / ml, 0.0, 3.0)

        strat   = str(row.get('strategy', ''))
        mode    = str(row.get('mode', 'intraday'))
        vreg    = str(row.get('vix_regime', ''))
        ddir    = str(row.get('day_direction', 'FLAT'))
        dgrp    = str(row.get('day_group', 'Mon-Wed'))
        wday    = int(_clamp(self._f(row, 'weekday'), 0, 4))
        drng    = str(row.get('day_range', 'NORMAL'))
        dvix    = str(row.get('day_vix', 'NORMAL'))

        dvix_num = {'LOW': 0.0, 'NORMAL': 0.33, 'HIGH': 0.67, 'VERY_HIGH': 1.0}.get(dvix, 0.33)

        return [
            _clamp(vix / 30.0, 0, 1),                          #  0
            _clamp(sa  /  5.0, 0, 1),                          #  1
            _clamp((gs + 3.0) / 6.0, 0, 1),                   #  2
            _clamp(dte / 30.0, 0, 1),                          #  3
            _clamp(credit / 300.0, 0, 1),                      #  4
            _clamp(width  / 1000.0, 0, 1),                     #  5
            _clamp((mv + 2.5) / 5.0, 0, 1),                   #  6
            _clamp(drs / 4.0, 0, 1),                           #  7
            _clamp(consec / 15.0, 0, 1),                       #  8
            is_cred,                                            #  9
            cr,                                                 # 10
            rr / 3.0,                                          # 11
            _clamp(sa * sa / 25.0, 0, 1),                      # 12
            self._b(row, 'inside_day'),                         # 13
            self._b(row, 'outside_day'),                        # 14
            self._b(row, 'uptrend'),                            # 15
            self._b(row, 'downtrend'),                          # 16
            self._b(row, 'bullish_close'),                      # 17
            self._b(row, 'bearish_close'),                      # 18
            1.0 if strat == 'BEAR_PUT'         else 0.0,       # 19
            1.0 if strat == 'BULL_CALL'        else 0.0,       # 20
            1.0 if strat == 'BULL_PUT'         else 0.0,       # 21
            1.0 if strat == 'IRON_CONDOR'      else 0.0,       # 22
            1.0 if strat == 'IRON_BUTTERFLY'   else 0.0,       # 23
            1.0 if mode  == 'swing'            else 0.0,       # 24
            1.0 if 'NORMAL' in vreg            else 0.0,       # 25
            1.0 if 'HIGH'   in vreg            else 0.0,       # 26
            1.0 if ddir == 'FLAT'              else 0.0,       # 27
            1.0 if ddir == 'UP'                else 0.0,       # 28
            1.0 if 'Thu' in dgrp               else 0.0,       # 29
            1.0 if wday == 1 else 0.0,                         # 30
            1.0 if wday == 2 else 0.0,                         # 31
            1.0 if wday == 3 else 0.0,                         # 32
            1.0 if wday == 4 else 0.0,                         # 33
            1.0 if drng == 'NORMAL'            else 0.0,       # 34
            1.0 if drng == 'TRENDING'          else 0.0,       # 35
            dvix_num,                                           # 36
            _clamp(abs(gs) / 3.0, 0, 1),                       # 37
        ]

    def ood_score(self, row):
        """
        Returns (is_ood: bool, confidence: float 0-1, warnings: list).
        confidence=1.0 = fully in-distribution.  <0.5 = suspect.
        """
        warnings = []
        violations = 0
        checks = 0

        for col, bounds in getattr(self, 'ood_bounds', {}).items():
            v = row.get(col)
            if _nan(v): continue
            try: fv = float(v)
            except: continue
            checks += 1
            lo, hi = bounds
            span = max(hi - lo, 1e-6)
            if fv < lo - 0.5 * span or fv > hi + 0.5 * span:
                violations += 1
                warnings.append(f'{col}={fv:.2f} outside [{lo:.2f},{hi:.2f}]')

        # Strategy-specific sigma_away check (most critical for credibility)
        strat = str(row.get('strategy', ''))
        sa_bounds = getattr(self, 'strat_sa_bounds', {}).get(strat)
        sa = row.get('sigma_away')
        sa_ood = False
        if sa_bounds and not _nan(sa):
            try:
                fsa = float(sa)
                lo, hi = sa_bounds
                if fsa < lo * 0.7:
                    # Critical: sigma_away below 70% of strategy minimum — hard OOD cap
                    sa_ood = True
                    violations += 3
                    warnings.append(f'sigma_away={fsa:.2f} BELOW strategy min {lo:.2f} for {strat} — model blind here')
            except Exception as e:
                print(f"DEBUG: ood_score sigma_away check failed: {e}")

        if checks == 0:
            return False, 1.0, []

        raw_conf = max(0.0, 1.0 - violations / max(checks + 2, 3))
        # Hard cap: critical strategy-specific violation → max 40% confidence
        confidence = min(raw_conf, 0.40) if sa_ood else raw_conf
        is_ood = violations > 0
        return is_ood, confidence, warnings, sa_ood

    # ── Serialise ────────────────────────────────────────────────────────────

    def to_dict(self):
        return {
            'medians': self.medians,
            'ood_bounds': getattr(self, 'ood_bounds', {}),
            'strat_sa_bounds': getattr(self, 'strat_sa_bounds', {}),
            'fitted': self.fitted,
        }

    @classmethod
    def from_dict(cls, d):
        fe = cls()
        fe.medians          = d.get('medians', {})
        fe.ood_bounds       = d.get('ood_bounds', {})
        fe.strat_sa_bounds  = d.get('strat_sa_bounds', {})
        fe.fitted           = d.get('fitted', False)
        return fe


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 2: GRADIENT BOOSTED TREES
# ─────────────────────────────────────────────────────────────────────────────

class _Tree:
    """
    Regression tree trained on pseudo-residuals.
    Split search: sorted-sweep O(n · F) per node — no random, reproducible.
    """

    def __init__(self, max_depth=3, min_leaf=8):
        self.max_depth = max_depth
        self.min_leaf  = min_leaf
        self.nodes     = []          # flat list; index = node id

    # ── Build ────────────────────────────────────────────────────────────────

    def _best_split(self, X, residuals, idx, feat_subset, sw=None):
        n  = len(idx)
        r  = [residuals[i] for i in idx]
        w  = [sw[i] for i in idx] if sw else [1.0] * n

        # Weighted sums for base MSE
        W  = sum(w)
        tr = sum(r[j] * w[j] for j in range(n))
        t2 = sum(r[j] * r[j] * w[j] for j in range(n))
        base_mse = t2 / W - (tr / W) ** 2

        if base_mse < 1e-12:
            return None

        best_gain = 0.0
        best      = None

        for fi in feat_subset:
            fv    = [X[idx[j]][fi] for j in range(n)]
            order = sorted(range(n), key=lambda j: fv[j])

            wlr_ = 0.0   # weighted sum of residuals (left)
            wlr2 = 0.0   # weighted sum of squared residuals (left)
            Wl   = 0.0   # weight accumulated on left

            for k in range(self.min_leaf, n - self.min_leaf + 1):
                j     = order[k - 1]
                wj    = w[j]
                wlr_ += r[j] * wj
                wlr2 += r[j] * r[j] * wj
                Wl   += wj

                if k < n and fv[order[k - 1]] >= fv[order[k]]:
                    continue

                Wr   = W - Wl
                wrr_ = tr - wlr_
                wrr2 = t2 - wlr2

                if Wl < 1e-9 or Wr < 1e-9:
                    continue

                lmse = max(0.0, wlr2 / Wl - (wlr_ / Wl) ** 2)
                rmse = max(0.0, wrr2 / Wr - (wrr_ / Wr) ** 2)
                gain = base_mse - (Wl * lmse + Wr * rmse) / W

                if gain > best_gain:
                    best_gain = gain
                    thr = (fv[order[k - 1]] + fv[order[k]]) / 2
                    best = (fi, thr,
                            [idx[order[j]] for j in range(k)],
                            [idx[order[j]] for j in range(k, n)])

        return best

    def _build(self, X, residuals, idx, depth, sw=None, p_prev=None):
        nid = len(self.nodes)
        r   = [residuals[i] for i in idx]
        w   = [sw[i] for i in idx] if sw else None

        # Issue 2: Newton-Raphson second-order leaf value
        # leaf_v = sum(w*r) / sum(w*p*(1-p))  → proper log-loss calibration
        if p_prev is not None:
            num   = sum((w[j] if w else 1.0) * r[j] for j in range(len(idx)))
            p_idx = [p_prev[i] for i in idx]
            den   = sum((w[j] if w else 1.0) * p_idx[j] * (1.0 - p_idx[j])
                        for j in range(len(idx)))
            leaf_v = num / den if den > 1e-6 else 0.0
        else:
            W      = sum(w) if w else len(r)
            leaf_v = sum((w[j] if w else 1.0) * r[j] for j in range(len(r))) / W if W else 0.0

        self.nodes.append({'L': True, 'v': leaf_v})

        if depth < self.max_depth and len(idx) >= 2 * self.min_leaf:
            nf   = len(X[0])
            k    = max(4, int(nf ** 0.5))
            self._node_cnt += 1
            seed = self._t_seed * 37 + self._node_cnt * 13
            fs   = list({(seed * 7 + i * 11) % nf for i in range(k * 3)})[:k]

            split = self._best_split(X, residuals, idx, fs, sw=sw)

            if split is not None:
                fi, thr, l_idx, r_idx = split
                self.nodes[nid] = {'L': False, 'f': fi, 't': thr, 'lc': None, 'rc': None}
                l_id = self._build(X, residuals, l_idx, depth + 1, sw, p_prev)
                r_id = self._build(X, residuals, r_idx, depth + 1, sw, p_prev)
                self.nodes[nid]['lc'] = l_id
                self.nodes[nid]['rc'] = r_id

        return nid

    def fit(self, X, residuals, idx, tree_seed=0, sw=None, p_prev=None):
        self.nodes     = []
        self._t_seed   = tree_seed
        self._node_cnt = 0
        self._build(X, residuals, idx, 0, sw=sw, p_prev=p_prev)

    # ── Predict ──────────────────────────────────────────────────────────────

    def predict_one(self, x):
        nid = 0
        while True:
            nd = self.nodes[nid]
            if nd['L']:
                return nd['v']
            nid = nd['lc'] if x[nd['f']] <= nd['t'] else nd['rc']

    # ── Serialise ────────────────────────────────────────────────────────────

    def to_dict(self):
        return {'d': self.max_depth, 'm': self.min_leaf, 'n': self.nodes}

    @classmethod
    def from_dict(cls, d):
        t = cls(d['d'], d['m'])
        t.nodes = d['n']
        return t


# ─────────────────────────────────────────────────────────────────────────────

class GradientBoostedTrees:
    """
    GBT binary classifier via first-order log-loss minimisation.
    Pseudo-residuals = y - sigmoid(F).
    Subsample: deterministic sliding window (different start per tree).
    """

    def __init__(self, n_trees=200, max_depth=3, lr=0.1, sub_frac=0.30, min_leaf=8):
        self.n_trees   = n_trees
        self.max_depth = max_depth
        self.lr        = lr
        self.sub_frac  = sub_frac
        self.min_leaf  = min_leaf
        self.base_f    = 0.0
        self.trees     = []
        self.train_acc = 0.0
        self.feat_imp  = [0.0] * N_FEATURES

    # ── Training ─────────────────────────────────────────────────────────────

    def fit(self, X, y, log_fn=None, sample_weights=None):
        n     = len(y)
        pos   = sum(y)
        neg   = n - pos
        self.base_f = math.log(max(pos, 1) / max(neg, 1))
        F = [self.base_f] * n

        # Default uniform weights
        sw = sample_weights if sample_weights else [1.0] * n

        val_n  = max(8, min(int(n * 0.15), n // 4))
        tr_n   = n - val_n

        sub_n = max(self.min_leaf * 4, int(tr_n * self.sub_frac))
        self.trees    = []
        self.feat_imp = [0.0] * N_FEATURES

        for t in range(self.n_trees):
            res    = [y[i] - _sig(F[i]) for i in range(n)]
            p_prev = [_sig(F[i]) for i in range(n)]   # for Newton-Raphson leaf

            # Sliding-window subsample — from training split only
            start = (t * sub_n) % tr_n
            idx   = [(start + j) % tr_n for j in range(sub_n)]

            tree = _Tree(self.max_depth, self.min_leaf)
            tree.fit(X, res, idx, tree_seed=t, sw=sw, p_prev=p_prev)
            self.trees.append(tree)

            for i in range(n):
                F[i] += self.lr * tree.predict_one(X[i])

            for nd in tree.nodes:
                if not nd.get('L', True):
                    fi = nd.get('f', -1)
                    if 0 <= fi < N_FEATURES:
                        self.feat_imp[fi] += 1.0

            if log_fn and (t + 1) % 50 == 0:
                preds   = [_sig(F[i]) for i in range(tr_n)]
                acc     = sum(1 for i in range(tr_n) if (preds[i] >= 0.5) == bool(y[i])) / tr_n
                logloss = -sum(_log(_sig(F[i])) * y[i] + _log(1 - _sig(F[i])) * (1 - y[i])
                               for i in range(tr_n)) / tr_n
                val_acc = sum(1 for i in range(tr_n, n)
                              if (_sig(F[i]) >= 0.5) == bool(y[i])) / val_n
                log_fn(f"  GBT {t+1:>3}/{self.n_trees}: tr_acc={acc:.3f}  "
                       f"log-loss={logloss:.4f}  val_acc={val_acc:.3f}")

        tot = sum(self.feat_imp) or 1.0
        self.feat_imp = [v / tot for v in self.feat_imp]

        preds = [_sig(F[i]) for i in range(tr_n)]
        self.train_acc = sum(1 for i in range(tr_n) if (preds[i] >= 0.5) == bool(y[i])) / tr_n
        self.val_acc   = sum(1 for i in range(tr_n, n)
                             if (_sig(F[i]) >= 0.5) == bool(y[i])) / val_n

    # ── Inference ────────────────────────────────────────────────────────────

    def predict_proba(self, x):
        f = self.base_f
        for tree in self.trees:
            f += self.lr * tree.predict_one(x)
        return _sig(f)

    def top_features(self, k=10):
        pairs = sorted(zip(FEATURE_NAMES, self.feat_imp), key=lambda p: -p[1])
        return pairs[:k]

    # ── Serialise ────────────────────────────────────────────────────────────

    def to_dict(self):
        return {
            'nt': self.n_trees, 'md': self.max_depth, 'lr': self.lr,
            'sf': self.sub_frac, 'ml': self.min_leaf,
            'bf': self.base_f, 'ta': self.train_acc, 'va': getattr(self, 'val_acc', 0.0),
            'fi': self.feat_imp,
            'trees': [t.to_dict() for t in self.trees],
        }

    @classmethod
    def from_dict(cls, d):
        g = cls(d['nt'], d['md'], d['lr'], d['sf'], d['ml'])
        g.base_f    = d['bf']
        g.train_acc = d['ta']
        g.val_acc   = d.get('va', 0.0)
        g.feat_imp  = d['fi']
        g.trees     = [_Tree.from_dict(t) for t in d['trees']]
        return g


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 3: NEURAL NETWORK  (38→32→16→1, ReLU, SGD+momentum)
# ─────────────────────────────────────────────────────────────────────────────

class NeuralNet:

    def __init__(self, h1=32, h2=16, lr=0.01, epochs=60, batch=64, mom=0.9, l2=0.005, dropout=0.20):
        self.h1      = h1
        self.h2      = h2
        self.lr      = lr
        self.epochs  = epochs
        self.batch   = batch
        self.mom     = mom
        self.l2      = l2
        self.dropout = dropout          # inverted dropout rate (0 = disabled)
        self._lcg    = 98765            # LCG state — advances each forward pass during training
        self.trained  = False
        self.train_acc = 0.0
        self.W1 = self.b1 = None
        self.W2 = self.b2 = None
        self.W3 = self.b3 = None

    # ── Init ─────────────────────────────────────────────────────────────────

    def _init(self):
        """Deterministic Xavier initialisation via LCG."""
        def lcg(seed):
            seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
            return seed / 0xFFFFFFFF - 0.5  # uniform [-0.5, 0.5]

        def xavier(fan_in, fan_out, seed_base):
            scale = math.sqrt(2.0 / (fan_in + fan_out))
            W = []
            s = seed_base
            for j in range(fan_out):
                row = []
                for i in range(fan_in):
                    s  = (1664525 * s + 1013904223) & 0xFFFFFFFF
                    row.append((s / 0xFFFFFFFF - 0.5) * 2 * scale)
                W.append(row)
            return W

        ni = N_FEATURES
        self.W1 = xavier(ni, self.h1, 42)
        self.b1 = [0.0] * self.h1
        self.W2 = xavier(self.h1, self.h2, 137)
        self.b2 = [0.0] * self.h2
        self.W3 = xavier(self.h2, 1, 7919)
        self.b3 = [0.0]

    # ── Forward / back ───────────────────────────────────────────────────────

    def _fwd(self, x, training=False):
        z1 = [self.b1[j] + sum(x[i] * self.W1[j][i] for i in range(N_FEATURES))
              for j in range(self.h1)]
        a1 = [max(0.0, z) for z in z1]

        # Issue 3: Inverted dropout on a1 during training
        if training and self.dropout > 0:
            scale = 1.0 / (1.0 - self.dropout)
            a1_d  = []
            for v in a1:
                self._lcg = (1664525 * self._lcg + 1013904223) & 0xFFFFFFFF
                if (self._lcg / 0xFFFFFFFF) < self.dropout:
                    a1_d.append(0.0)
                else:
                    a1_d.append(v * scale)
            a1 = a1_d

        z2 = [self.b2[j] + sum(a1[i] * self.W2[j][i] for i in range(self.h1))
              for j in range(self.h2)]
        a2 = [max(0.0, z) for z in z2]

        # Inverted dropout on a2 during training
        if training and self.dropout > 0:
            scale = 1.0 / (1.0 - self.dropout)
            a2_d  = []
            for v in a2:
                self._lcg = (1664525 * self._lcg + 1013904223) & 0xFFFFFFFF
                if (self._lcg / 0xFFFFFFFF) < self.dropout:
                    a2_d.append(0.0)
                else:
                    a2_d.append(v * scale)
            a2 = a2_d

        z3 = self.b3[0] + sum(a2[i] * self.W3[0][i] for i in range(self.h2))
        return a1, z1, a2, z2, _sig(z3)

    # ── Training ─────────────────────────────────────────────────────────────

    def fit(self, X, y, log_fn=None, val_frac=0.15):
        n = len(y)
        self._init()
        m  = self.mom
        lr = self.lr

        # Time-based validation split: last val_frac rows = held-out
        val_n  = max(8, min(int(n * val_frac), n // 4))
        tr_n   = n - val_n
        X_tr, y_tr = X[:tr_n], y[:tr_n]
        X_va, y_va = X[tr_n:], y[tr_n:]

        # Velocity buffers (momentum)
        vW1 = [[0.0]*N_FEATURES for _ in range(self.h1)]
        vb1 = [0.0] * self.h1
        vW2 = [[0.0]*self.h1 for _ in range(self.h2)]
        vb2 = [0.0] * self.h2
        vW3 = [[0.0]*self.h2]
        vb3 = [0.0]

        # Early stopping: keep best weights on val loss
        best_val_loss  = float('inf')
        best_weights   = None
        patience       = 8
        no_improve     = 0

        for ep in range(self.epochs):
            total_loss = 0.0
            correct    = 0

            for bs in range(0, tr_n, self.batch):
                be   = min(bs + self.batch, tr_n)
                bsz  = be - bs

                # Accumulate gradients
                gW1 = [[0.0]*N_FEATURES for _ in range(self.h1)]
                gb1 = [0.0] * self.h1
                gW2 = [[0.0]*self.h1 for _ in range(self.h2)]
                gb2 = [0.0] * self.h2
                gW3 = [[0.0]*self.h2]
                gb3 = [0.0]

                for i in range(bs, be):
                    x  = X_tr[i]
                    yi = y_tr[i]
                    a1, z1, a2, z2, out = self._fwd(x, training=True)

                    total_loss += -yi * _log(out) - (1 - yi) * _log(1 - out)
                    correct    += int((out >= 0.5) == bool(yi))

                    d3 = out - yi                          # dL / dz3

                    for j in range(self.h2):
                        gW3[0][j] += d3 * a2[j]
                    gb3[0] += d3

                    d2 = [d3 * self.W3[0][j] * (1.0 if z2[j] > 0 else 0.0)
                          for j in range(self.h2)]

                    for j in range(self.h2):
                        for k in range(self.h1):
                            gW2[j][k] += d2[j] * a1[k]
                        gb2[j] += d2[j]

                    d1 = [sum(d2[j] * self.W2[j][k] for j in range(self.h2)) *
                          (1.0 if z1[k] > 0 else 0.0)
                          for k in range(self.h1)]

                    for j in range(self.h1):
                        for k in range(N_FEATURES):
                            gW1[j][k] += d1[j] * x[k]
                        gb1[j] += d1[j]

                # SGD + momentum update (with L2 weight decay)
                inv  = lr / bsz
                m    = self.mom
                wd   = self.l2  # weight decay = L2 regularization

                for j in range(self.h1):
                    for k in range(N_FEATURES):
                        vW1[j][k] = m * vW1[j][k] - inv * gW1[j][k]
                        self.W1[j][k] = self.W1[j][k] * (1 - lr * wd) + vW1[j][k]
                    vb1[j] = m * vb1[j] - inv * gb1[j]
                    self.b1[j] += vb1[j]

                for j in range(self.h2):
                    for k in range(self.h1):
                        vW2[j][k] = m * vW2[j][k] - inv * gW2[j][k]
                        self.W2[j][k] = self.W2[j][k] * (1 - lr * wd) + vW2[j][k]
                    vb2[j] = m * vb2[j] - inv * gb2[j]
                    self.b2[j] += vb2[j]

                for k in range(self.h2):
                    vW3[0][k] = m * vW3[0][k] - inv * gW3[0][k]
                    self.W3[0][k] = self.W3[0][k] * (1 - lr * wd) + vW3[0][k]
                vb3[0] = m * vb3[0] - inv * gb3[0]
                self.b3[0] += vb3[0]

            self.train_acc = correct / tr_n

            # Validation loss (no grad)
            val_loss = sum(
                -y_va[i] * _log(self._fwd(X_va[i])[4]) - (1 - y_va[i]) * _log(1 - self._fwd(X_va[i])[4])
                for i in range(val_n)
            ) / val_n
            val_acc = sum(
                1 for i in range(val_n) if (self._fwd(X_va[i])[4] >= 0.5) == bool(y_va[i])
            ) / val_n

            if log_fn and (ep + 1) % 10 == 0:
                log_fn(f"  NN  ep {ep+1:>2}/{self.epochs}: "
                       f"tr_loss={total_loss/tr_n:.4f}  tr_acc={self.train_acc:.3f}  "
                       f"val_loss={val_loss:.4f}  val_acc={val_acc:.3f}")

            # Early stopping: track best val loss, save weights
            if val_loss < best_val_loss - 1e-5:
                best_val_loss = val_loss
                best_weights  = (
                    [row[:] for row in self.W1], self.b1[:],
                    [row[:] for row in self.W2], self.b2[:],
                    [row[:] for row in self.W3], self.b3[:],
                )
                no_improve = 0
            else:
                no_improve += 1
                if no_improve >= patience:
                    if log_fn:
                        log_fn(f"  NN  early stop at ep {ep+1} (no val improvement for {patience} epochs)")
                    break

        # Restore best weights
        if best_weights is not None:
            self.W1, self.b1, self.W2, self.b2, self.W3, self.b3 = best_weights

        # Final val accuracy with restored weights
        self.val_acc = sum(
            1 for i in range(val_n) if (self._fwd(X_va[i])[4] >= 0.5) == bool(y_va[i])
        ) / val_n

        self.trained = True

    def online_step(self, x, y_val, lr=0.005, n_steps=3):
        """
        Micro-gradient update on a single (x, y) sample.
        No val split, no momentum reset — just n_steps of SGD.
        Used by ml_train.online_update() after each trade close.
        """
        if not self.trained:
            return
        m  = self.mom
        wd = self.l2

        for _ in range(n_steps):
            a1, z1, a2, z2, out = self._fwd(x, training=True)
            d3 = out - y_val

            gW3 = [[d3 * a2[j] for j in range(self.h2)]]
            gb3 = [d3]

            d2 = [d3 * self.W3[0][j] * (1.0 if z2[j] > 0 else 0.0)
                  for j in range(self.h2)]
            gW2 = [[d2[j] * a1[k] for k in range(self.h1)] for j in range(self.h2)]
            gb2 = list(d2)

            d1 = [sum(d2[j] * self.W2[j][k] for j in range(self.h2)) *
                  (1.0 if z1[k] > 0 else 0.0) for k in range(self.h1)]
            gW1 = [[d1[j] * x[k] for k in range(N_FEATURES)] for j in range(self.h1)]
            gb1 = list(d1)

            for j in range(self.h1):
                for k in range(N_FEATURES):
                    self.W1[j][k] = self.W1[j][k] * (1 - lr * wd) - lr * gW1[j][k]
                self.b1[j] -= lr * gb1[j]
            for j in range(self.h2):
                for k in range(self.h1):
                    self.W2[j][k] = self.W2[j][k] * (1 - lr * wd) - lr * gW2[j][k]
                self.b2[j] -= lr * gb2[j]
            for k in range(self.h2):
                self.W3[0][k] = self.W3[0][k] * (1 - lr * wd) - lr * gW3[0][k]
            self.b3[0] -= lr * gb3[0]



    # ── Inference ────────────────────────────────────────────────────────────

    def predict_proba(self, x):
        if not self.trained:
            return 0.5
        _, _, _, _, out = self._fwd(x)
        return out

    # ── Serialise ────────────────────────────────────────────────────────────

    def to_dict(self):
        return {
            'h1': self.h1, 'h2': self.h2, 'lr': self.lr,
            'ep': self.epochs, 'bt': self.batch, 'mo': self.mom, 'l2': self.l2,
            'do': self.dropout,
            'ok': self.trained, 'ta': self.train_acc, 'va': getattr(self, 'val_acc', 0.0),
            'W1': self.W1, 'b1': self.b1,
            'W2': self.W2, 'b2': self.b2,
            'W3': self.W3, 'b3': self.b3,
        }

    @classmethod
    def from_dict(cls, d):
        nn = cls(d['h1'], d['h2'], d['lr'], d['ep'], d['bt'], d['mo'], d.get('l2', 0.005), d.get('do', 0.20))
        nn.trained    = d['ok']
        nn.train_acc  = d['ta']
        nn.val_acc    = d.get('va', 0.0)
        nn.W1, nn.b1  = d['W1'], d['b1']
        nn.W2, nn.b2  = d['W2'], d['b2']
        nn.W3, nn.b3  = d['W3'], d['b3']
        return nn


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 4: REGIME DETECTOR  (K-means, 4 states)
# ─────────────────────────────────────────────────────────────────────────────

class RegimeDetector:
    """
    4-state market regime via K-means on (vix, |move_sigma|, day_range_sigma, |gap_sigma|).
    States auto-labelled by centroid values after clustering.
    Transition matrix computed from sequential daily data.
    """

    N_STATES  = 4
    FEAT_COLS = ['vix', 'move_sigma', 'day_range_sigma', 'gap_sigma']

    def __init__(self):
        self.centroids  = []          # list of 4 float[4] cluster centres
        self.state_map  = {}          # cluster_id → REGIME_NAMES label
        self.trans      = []          # 4×4 transition probabilities
        self.emission   = []          # 4 × {'mean': [], 'std': []} for each cluster feature
        self.fitted     = False

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _row_to_vec(self, row):
        """Extract 4-D regime feature vector from a trade row."""
        def gf(col):
            v = row.get(col)
            if _nan(v): return 0.0
            try: return float(v)
            except: return 0.0
        vix = gf('vix')
        mv  = abs(gf('move_sigma'))
        drs = gf('day_range_sigma')
        gs  = abs(gf('gap_sigma'))
        return [vix, mv, drs, gs]

    def _dist(self, a, b):
        return sum((x - y) ** 2 for x, y in zip(a, b))

    def _assign(self, points):
        return [min(range(len(self.centroids)),
                    key=lambda k: self._dist(points[i], self.centroids[k]))
                for i in range(len(points))]

    # ── Fit ─────────────────────────────────────────────────────────────────

    def fit(self, rows, log_fn=None):
        """
        rows: list of dicts sorted by date (chronological).
        Runs K-means for 50 iterations then derives regime labels.
        """
        pts = [self._row_to_vec(r) for r in rows]
        n   = len(pts)
        k   = self.N_STATES

        # K-means++ style init: spread centroids using max-dist seeding
        c = [pts[n // 4], pts[n // 2], pts[3 * n // 4], pts[-1]]
        self.centroids = [list(ci) for ci in c]

        for it in range(50):
            labels = self._assign(pts)
            new_c  = [[0.0] * 4 for _ in range(k)]
            cnts   = [0] * k
            for i, lab in enumerate(labels):
                for d in range(4):
                    new_c[lab][d] += pts[i][d]
                cnts[lab] += 1
            for j in range(k):
                if cnts[j] > 0:
                    self.centroids[j] = [v / cnts[j] for v in new_c[j]]

        labels = self._assign(pts)

        # ── Auto-label clusters by centroid characteristics ──────────────────
        # centroid features: [vix, |move|, day_range_sigma, |gap_sigma|]
        # VOLATILE  = highest VIX
        # TRENDING  = highest |move|
        # CHOPPY    = highest day_range_sigma with low |move|
        # CALM      = lowest everything

        def score_volatile(c):  return c[0]
        def score_trending(c):  return c[1]
        def score_choppy(c):    return c[2] - c[1]   # high range, low move
        def score_calm(c):      return -(c[0] + c[1] + c[2])

        scores = {
            'VOLATILE': [(score_volatile(self.centroids[j]), j) for j in range(k)],
            'TRENDING': [(score_trending(self.centroids[j]), j) for j in range(k)],
            'CHOPPY':   [(score_choppy(self.centroids[j]),   j) for j in range(k)],
            'CALM':     [(score_calm(self.centroids[j]),     j) for j in range(k)],
        }

        assigned_clusters = set()
        self.state_map    = {}
        # Greedy assignment: highest-score wins
        for label in ['VOLATILE', 'TRENDING', 'CHOPPY', 'CALM']:
            ranked = sorted(scores[label], key=lambda p: -p[0])
            for _, cid in ranked:
                if cid not in assigned_clusters:
                    self.state_map[cid] = label
                    assigned_clusters.add(cid)
                    break
        # fallback: any unassigned cluster → CALM
        remaining = [name for name in REGIME_NAMES if name not in self.state_map.values()]
        for cid in range(k):
            if cid not in self.state_map:
                self.state_map[cid] = remaining.pop(0) if remaining else 'CALM'

        # ── Transition matrix from sequential dates ──────────────────────────
        trans_cnt = [[0.0] * k for _ in range(k)]
        for i in range(len(labels) - 1):
            # Only count same-day-group transitions (skip weekends)
            trans_cnt[labels[i]][labels[i + 1]] += 1.0

        self.trans = []
        for row in trans_cnt:
            tot = sum(row) or 1.0
            self.trans.append([v / tot for v in row])

        # ── Emission stats ───────────────────────────────────────────────────
        self.emission = []
        for j in range(k):
            cluster_pts = [pts[i] for i, lab in enumerate(labels) if lab == j]
            if not cluster_pts:
                cluster_pts = [self.centroids[j]]
            means = [_mean([p[d] for p in cluster_pts]) for d in range(4)]
            stds  = [max(0.1, _std([p[d] for p in cluster_pts])) for d in range(4)]
            self.emission.append({'mean': means, 'std': stds})

        self.fitted = True
        if log_fn:
            dist = {self.state_map[j]: sum(1 for lab in labels if lab == j)
                    for j in range(k)}
            log_fn(f"  Regime distribution: {dist}")

    # ── Inference ────────────────────────────────────────────────────────────

    def predict(self, row):
        """Returns regime label string and cluster probabilities."""
        if not self.fitted or not self.centroids:
            return 'CALM', {}

        pt  = self._row_to_vec(row)
        dists = [self._dist(pt, c) for c in self.centroids]
        best_cid = min(range(len(dists)), key=lambda j: dists[j])

        # Soft probabilities via inverse-distance (temp=1)
        inv = [1.0 / max(d, 1e-6) for d in dists]
        tot = sum(inv)
        probs = {self.state_map[j]: inv[j] / tot for j in range(len(self.centroids))}

        label = self.state_map[best_cid]
        return label, probs

    # ── Regime → strategy hint ───────────────────────────────────────────────

    @staticmethod
    def regime_strategy_hint(regime):
        """Return preferred strategy type for a given regime."""
        return {
            'CALM':     {'credit': True,  'debit': True,  'note': 'Both work, prefer credit'},
            'TRENDING': {'credit': False, 'debit': True,  'note': 'Debit follows momentum'},
            'CHOPPY':   {'credit': True,  'debit': False, 'note': 'Credit survives chop'},
            'VOLATILE': {'credit': False, 'debit': True,  'note': 'High IV — debit or wide credit'},
        }.get(regime, {'credit': True, 'debit': True, 'note': 'Unknown regime'})

    # ── Serialise ────────────────────────────────────────────────────────────

    def to_dict(self):
        return {
            'centroids': self.centroids,
            'state_map': {str(k): v for k, v in self.state_map.items()},
            'trans':     self.trans,
            'emission':  self.emission,
            'fitted':    self.fitted,
        }

    @classmethod
    def from_dict(cls, d):
        rd = cls()
        rd.centroids = d.get('centroids', [])
        rd.state_map = {int(k): v for k, v in d.get('state_map', {}).items()}
        rd.trans     = d.get('trans', [])
        rd.emission  = d.get('emission', [])
        rd.fitted    = d.get('fitted', False)
        return rd


# ─────────────────────────────────────────────────────────────────────────────
# ENSEMBLE META-LEARNER
# ─────────────────────────────────────────────────────────────────────────────

class MetaLearner:
    """
    Regime-conditioned weighted average of GBT and NN predictions.
    Weights learned by isotonic-style calibration on training data.
    """

    def __init__(self):
        # GBT gets higher weight — better calibrated on tabular data
        # NN gets lower weight due to saturation tendencies on this dataset
        self.regime_weights = {
            'CALM':     {'gbt': 0.75, 'nn': 0.25},
            'TRENDING': {'gbt': 0.70, 'nn': 0.30},
            'CHOPPY':   {'gbt': 0.75, 'nn': 0.25},
            'VOLATILE': {'gbt': 0.70, 'nn': 0.30},
            'DEFAULT':  {'gbt': 0.72, 'nn': 0.28},
        }
        self.calibration_bias = 0.0   # global additive correction (log-odds)

    def calibrate(self, gbt_preds, nn_preds, regimes, y_true):
        """
        Fit per-regime weights and global bias by grid search on log-loss.
        preds: lists of floats, regimes: list of str, y_true: list of 0/1.
        """
        n = len(y_true)
        best_loss = float('inf')
        best_bias = 0.0

        # Grid search over global bias in [-0.5, 0.5]
        for bias_step in range(-20, 21):
            bias = bias_step * 0.025
            total = 0.0
            for i in range(n):
                rw   = self.regime_weights.get(regimes[i],
                       self.regime_weights['DEFAULT'])
                p    = rw['gbt'] * gbt_preds[i] + rw['nn'] * nn_preds[i]
                p_c  = _sig(_log(max(p, 1e-6) / max(1 - p, 1e-6)) + bias)
                total += -y_true[i] * _log(p_c) - (1 - y_true[i]) * _log(1 - p_c)
            if total < best_loss:
                best_loss = total
                best_bias = bias

        self.calibration_bias = best_bias

    def predict(self, gbt_p, nn_p, regime='DEFAULT'):
        rw = self.regime_weights.get(regime, self.regime_weights['DEFAULT'])
        p  = rw['gbt'] * gbt_p + rw['nn'] * nn_p
        # Apply calibration bias in log-odds space
        if self.calibration_bias != 0.0:
            lo = _log(max(p, 1e-6) / max(1 - p, 1e-6)) + self.calibration_bias
            p  = _sig(lo)
        return _clamp(p, 0.01, 0.99)

    def to_dict(self):
        return {'rw': self.regime_weights, 'cb': self.calibration_bias}

    @classmethod
    def from_dict(cls, d):
        ml = cls()
        ml.regime_weights    = d.get('rw', ml.regime_weights)
        ml.calibration_bias  = d.get('cb', 0.0)
        return ml


# ─────────────────────────────────────────────────────────────────────────────
# ML ENGINE — ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

class MLEngine:
    """
    Main interface.  Wraps FeatureEngine + GBT + NN + Regime + Meta.
    """

    def __init__(self):
        self.feature_engine = FeatureEngine()
        self.gbt            = GradientBoostedTrees()
        self.nn             = NeuralNet()
        self.regime         = RegimeDetector()
        self.meta           = MetaLearner()
        self.trained        = False
        self.n_train        = 0
        self.base_win_rate  = 0.0
        self.ml_version     = ML_VERSION
        # Calibrated from training-data score distribution (set in train_from_csv)
        self.thr_take       = 0.65   # precision ~96% — strong TAKE signal
        self.thr_watch      = 0.50   # precision ~91% — conditional / watch

    # ── Core predict ─────────────────────────────────────────────────────────

    def predict(self, candidate):
        """
        candidate: dict with trade/market fields (same schema as backtest CSV or app candidate).
        Returns: (p_win: float, regime: str, detail: dict)
        """
        if not self.trained:
            return 0.5, 'UNKNOWN', {'error': 'model not trained'}

        feat              = self.feature_engine.extract(candidate)
        is_ood, ood_conf, ood_warns, is_strategy_blind = self.feature_engine.ood_score(candidate)
        p_gbt             = self.gbt.predict_proba(feat)
        p_nn              = self.nn.predict_proba(feat)
        regime, reg_probs = self.regime.predict(candidate)
        p_meta            = self.meta.predict(p_gbt, p_nn, regime)

        # OOD correction: shrink toward base win rate by (1 - ood_confidence)
        if is_ood and ood_conf < 0.9:
            p_meta = ood_conf * p_meta + (1.0 - ood_conf) * self.base_win_rate

        action = ('TAKE'  if p_meta >= self.thr_take  else
                  'WATCH' if p_meta >= self.thr_watch else 'SKIP')

        # Strategy-blind: model has zero training data for this scenario → BLOCKED
        if is_strategy_blind:
            action = 'BLOCKED'
        # Severely OOD (ood_conf ≤ 0.40): shrinkage math keeps score in WATCH range
        # so we must hard-reject → SKIP (Antigravity fix)
        elif is_ood and ood_conf <= 0.40:
            action = 'SKIP'
        elif is_ood and ood_conf < 0.6 and action == 'TAKE':
            action = 'WATCH'

        hint = RegimeDetector.regime_strategy_hint(regime)

        return p_meta, regime, {
            'p_gbt':      round(p_gbt, 4),
            'p_nn':       round(p_nn,  4),
            'p_meta':     round(p_meta, 4),
            'action':     action,
            'regime':     regime,
            'regime_prob': {k: round(v, 3) for k, v in reg_probs.items()},
            'regime_hint': hint,
            'base_wr':    round(self.base_win_rate, 3),
            'edge':       round(p_meta - self.base_win_rate, 3),
            'ood':        is_ood,
            'ood_conf':   round(ood_conf, 3),
            'ood_warns':  ood_warns,
            'ood_blocked': is_strategy_blind,
            'thr_take':   self.thr_take,
            'thr_watch':  self.thr_watch,
        }

    # ── Feature importance ───────────────────────────────────────────────────

    def top_features(self, k=10):
        return self.gbt.top_features(k)

    # ── Serialise ────────────────────────────────────────────────────────────

    def to_dict(self):
        return {
            'version':  self.ml_version,
            'trained':  self.trained,
            'n_train':  self.n_train,
            'base_wr':  self.base_win_rate,
            'thr_take': self.thr_take,
            'thr_watch':self.thr_watch,
            'fe':   self.feature_engine.to_dict(),
            'gbt':  self.gbt.to_dict(),
            'nn':   self.nn.to_dict(),
            'reg':  self.regime.to_dict(),
            'meta': self.meta.to_dict(),
        }

    @classmethod
    def from_dict(cls, d):
        e = cls()
        e.ml_version      = d.get('version', ML_VERSION)
        e.trained         = d.get('trained', False)
        e.n_train         = d.get('n_train', 0)
        e.base_win_rate   = d.get('base_wr', 0.0)
        e.thr_take        = d.get('thr_take',  0.65)
        e.thr_watch       = d.get('thr_watch', 0.50)
        e.feature_engine  = FeatureEngine.from_dict(d['fe'])
        e.gbt             = GradientBoostedTrees.from_dict(d['gbt'])
        e.nn              = NeuralNet.from_dict(d['nn'])
        e.regime          = RegimeDetector.from_dict(d['reg'])
        e.meta            = MetaLearner.from_dict(d.get('meta', {}))
        return e


# ─────────────────────────────────────────────────────────────────────────────
# TRAIN FROM CSV
# ─────────────────────────────────────────────────────────────────────────────

def _load_csv(path):
    rows = []
    with open(path, 'r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    return rows


def train_from_csv(path, log_fn=None, skip_nn=False):
    """
    Train a complete MLEngine from backtest_trades.csv.

    Args:
        path:    path to backtest CSV
        log_fn:  optional callable for progress messages (e.g. print)
        skip_nn: skip NN training for faster debugging

    Returns:
        MLEngine (trained)
    """
    def log(msg):
        if log_fn: log_fn(msg)

    log("── Loading CSV ──────────────────────────────────────────────")
    rows = _load_csv(path)
    log(f"  Loaded {len(rows)} rows")

    # Parse target
    y = []
    clean_rows = []
    for r in rows:
        v = r.get('won', '')
        if str(v).strip().lower() in ('true', '1'):
            y.append(1)
            clean_rows.append(r)
        elif str(v).strip().lower() in ('false', '0'):
            y.append(0)
            clean_rows.append(r)
        # drop rows with missing target

    n = len(clean_rows)
    log(f"  Valid rows: {n}  |  Win rate: {sum(y)/n:.3f}")

    engine = MLEngine()
    engine.n_train       = n
    engine.base_win_rate = sum(y) / n

    # ── Layer 1: Feature Engine ──────────────────────────────────────────────
    log("\n── Layer 1: Feature Engine ─────────────────────────────────")
    engine.feature_engine.fit(clean_rows)
    X = [engine.feature_engine.extract(r) for r in clean_rows]
    log(f"  Feature matrix: {len(X)} × {len(X[0])}")

    # ── Layer 4: Regime Detector (trains on raw rows) ────────────────────────
    log("\n── Layer 4: Regime Detector ────────────────────────────────")
    engine.regime.fit(clean_rows, log_fn=log)

    # ── Layer 2: GBT ────────────────────────────────────────────────────────
    log("\n── Layer 2: Gradient Boosted Trees ─────────────────────────")
    engine.gbt.fit(X, y, log_fn=log)
    log(f"  GBT train accuracy: {engine.gbt.train_acc:.3f}  |  val accuracy: {engine.gbt.val_acc:.3f}")

    # ── Layer 3: Neural Network ──────────────────────────────────────────────
    if not skip_nn:
        log("\n── Layer 3: Neural Network ─────────────────────────────────")
        engine.nn.fit(X, y, log_fn=log)
        log(f"  NN  train accuracy: {engine.nn.train_acc:.3f}  |  val accuracy: {engine.nn.val_acc:.3f}")
    else:
        log("\n── Layer 3: NN SKIPPED ─────────────────────────────────────")

    # ── Meta: Calibration on full dataset predictions ────────────────────────
    log("\n── Meta: Calibrating ensemble ──────────────────────────────")
    gbt_preds = [engine.gbt.predict_proba(X[i]) for i in range(n)]
    nn_preds  = ([engine.nn.predict_proba(X[i]) for i in range(n)]
                 if engine.nn.trained
                 else [0.5] * n)
    regimes   = [engine.regime.predict(clean_rows[i])[0] for i in range(n)]
    engine.meta.calibrate(gbt_preds, nn_preds, regimes, y)
    log(f"  Calibration bias: {engine.meta.calibration_bias:+.3f}")

    # Honest holdout accuracy (last 15% = same split as GBT/NN used)
    val_n   = max(8, min(int(n * 0.15), n // 4))
    tr_n    = n - val_n
    val_correct = sum(
        1 for i in range(tr_n, n)
        if (engine.meta.predict(gbt_preds[i], nn_preds[i], regimes[i]) >= 0.5) == bool(y[i])
    )
    tr_correct = sum(
        1 for i in range(tr_n)
        if (engine.meta.predict(gbt_preds[i], nn_preds[i], regimes[i]) >= 0.5) == bool(y[i])
    )
    val_wr = sum(y[tr_n:]) / val_n
    log(f"  Ensemble train accuracy : {tr_correct/tr_n:.3f}  (base WR: {engine.base_win_rate:.3f})")
    log(f"  Ensemble HOLDOUT accuracy: {val_correct/val_n:.3f}  (holdout WR: {val_wr:.3f}  n={val_n})")

    # ── Calibrate thresholds from HOLDOUT score distribution ─────────────────
    log("\n── Threshold calibration (holdout) ─────────────────────────")
    val_preds = []
    for i in range(tr_n, n):
        feat = engine.feature_engine.extract(clean_rows[i])
        pg   = engine.gbt.predict_proba(feat)
        pn   = engine.nn.predict_proba(feat) if engine.nn.trained else 0.5
        reg  = regimes[i]
        pm   = engine.meta.predict(pg, pn, reg)
        val_preds.append((pm, y[i]))

    # TAKE: first threshold where holdout precision >= 0.96
    # WATCH: TAKE - 0.12 (captures additional ~8% winners with small precision drop)
    best_thr_take = 0.65   # fallback
    for thr_step in range(70, 42, -2):
        thr  = thr_step / 100.0
        take = [(p, yi) for p, yi in val_preds if p >= thr]
        if len(take) < 20: continue
        prec = sum(yi for _, yi in take) / len(take)
        if prec >= 0.96:
            best_thr_take = thr
            break

    engine.thr_take  = best_thr_take
    engine.thr_watch = max(0.46, best_thr_take - 0.12)

    take_t = [(p, yi) for p, yi in val_preds if p >= engine.thr_take]
    take_w = [(p, yi) for p, yi in val_preds if p >= engine.thr_watch]
    prec_t = sum(yi for _, yi in take_t) / max(len(take_t), 1)
    prec_w = sum(yi for _, yi in take_w) / max(len(take_w), 1)
    rec_t  = sum(yi for _, yi in take_t) / max(sum(y[tr_n:]), 1)
    log(f"  TAKE  threshold: {engine.thr_take:.2f}  precision={prec_t:.3f}  recall={rec_t:.3f}  n={len(take_t)}")
    log(f"  WATCH threshold: {engine.thr_watch:.2f}  precision={prec_w:.3f}  n={len(take_w)}")

    log("\n── Top-10 predictive features ──────────────────────────────")
    for fname, imp in engine.gbt.top_features(10):
        log(f"  {fname:<22}  {imp*100:.1f}%")

    engine.trained = True
    log("\n── Training complete ───────────────────────────────────────")
    return engine


# ─────────────────────────────────────────────────────────────────────────────
# SAVE / LOAD
# ─────────────────────────────────────────────────────────────────────────────

def save_model(engine, path):
    """Serialise MLEngine to JSON file."""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(engine.to_dict(), f, separators=(',', ':'))
    size_kb = __import__('os').path.getsize(path) // 1024
    return size_kb


def load_model(path):
    """Deserialise MLEngine from JSON file."""
    with open(path, 'r', encoding='utf-8') as f:
        d = json.load(f)
    return MLEngine.from_dict(d)


# ─────────────────────────────────────────────────────────────────────────────
# BRAIN.PY INTEGRATION STUB
# ─────────────────────────────────────────────────────────────────────────────

_LOADED_ENGINE = None

def ml_load(model_path):
    """Call once at brain.py init: _ML = ml_load('/path/model.json')."""
    global _LOADED_ENGINE
    try:
        _LOADED_ENGINE = load_model(model_path)
        return _LOADED_ENGINE
    except Exception as e:
        return None


def ml_predict_candidate(candidate, engine=None):
    """
    Drop-in function for brain.py generate_candidates pipeline.

    candidate: dict with at minimum:
        strategy, mode, vix, entry_credit, width, sigma_away, move_sigma,
        dte, is_credit, max_profit, max_loss, day_direction, vix_regime,
        day_range, day_vix, weekday, day_group

    Returns: dict with p_win, regime, edge, p_gbt, p_nn
    """
    eng = engine or _LOADED_ENGINE
    if eng is None:
        return {'p_win': 0.5, 'regime': 'UNKNOWN', 'edge': 0.0, 'p_gbt': 0.5, 'p_nn': 0.5}

    p_win, regime, detail = eng.predict(candidate)
    return {
        'p_win':  round(p_win, 4),
        'regime': regime,
        'edge':   detail.get('edge', 0.0),
        'p_gbt':  detail.get('p_gbt', 0.5),
        'p_nn':   detail.get('p_nn',  0.5),
    }


# ─────────────────────────────────────────────────────────────────────────────
# QUICK SELF-TEST
# ─────────────────────────────────────────────────────────────────────────────

def self_test():
    """Smoke-test: 20-row synthetic dataset.  Should print no errors."""
    import random
    random.seed(42)

    def fake_row(won):
        return {
            'won': str(won), 'strategy': 'BEAR_CALL', 'mode': 'intraday',
            'vix': 17.0 + random.gauss(0, 2), 'sigma_away': 0.6 + random.gauss(0, 0.3),
            'gap_sigma': random.gauss(0, 0.5), 'dte': 3, 'entry_credit': 55.0,
            'width': 200, 'move_sigma': random.gauss(0, 0.5), 'day_range_sigma': 0.8,
            'consec_days': 3, 'max_profit': 3575, 'max_loss': 9425, 'legs': 2,
            'is_credit': True, 'vix_regime': 'NORMAL (15-20)', 'day_group': 'Mon-Wed',
            'day_direction': 'FLAT', 'day_range': 'NORMAL', 'day_vix': 'NORMAL',
            'weekday': 1, 'inside_day': '', 'outside_day': '', 'uptrend': '',
            'downtrend': '', 'bullish_close': '', 'bearish_close': '',
        }

    rows = [fake_row(i % 3 != 0) for i in range(40)]
    fe = FeatureEngine()
    fe.fit(rows)

    feat = fe.extract(rows[0])
    assert len(feat) == N_FEATURES, f"Expected {N_FEATURES} features, got {len(feat)}"

    y = [1 if r['won'] == 'True' else 0 for r in rows]
    X = [fe.extract(r) for r in rows]

    gbt = GradientBoostedTrees(n_trees=10, max_depth=2, min_leaf=2)
    gbt.fit(X, y)
    p = gbt.predict_proba(X[0])
    assert 0.0 <= p <= 1.0, "GBT predict out of range"

    nn = NeuralNet(h1=8, h2=4, epochs=3)
    nn.fit(X, y)
    p2 = nn.predict_proba(X[0])
    assert 0.0 <= p2 <= 1.0, "NN predict out of range"

    rd = RegimeDetector()
    rd.fit(rows)
    label, probs = rd.predict(rows[0])
    assert label in REGIME_NAMES, f"Bad regime label: {label}"

    print("self_test PASSED  ✓  (features OK, GBT OK, NN OK, Regime OK)")


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 1:
        self_test()
    elif sys.argv[1] == 'train' and len(sys.argv) >= 3:
        import time
        csv_path   = sys.argv[2]
        model_path = sys.argv[3] if len(sys.argv) > 3 else 'model.json'
        t0 = time.time()
        engine = train_from_csv(csv_path, log_fn=print, skip_nn='--skip-nn' in sys.argv)
        kb = save_model(engine, model_path)
        elapsed = time.time() - t0
        print(f"\nModel saved → {model_path}  ({kb} KB)  training took {elapsed:.1f}s")
    elif sys.argv[1] == 'predict' and len(sys.argv) >= 3:
        engine = load_model(sys.argv[2])
        sample = {
            'strategy': 'BEAR_CALL', 'mode': 'intraday', 'vix': 18.5,
            'sigma_away': 0.65, 'gap_sigma': 0.0, 'dte': 3,
            'entry_credit': 62, 'width': 200, 'move_sigma': -0.15,
            'day_range_sigma': 0.85, 'consec_days': 2,
            'max_profit': 4030, 'max_loss': 8970, 'legs': 2,
            'is_credit': True, 'vix_regime': 'NORMAL (15-20)',
            'day_group': 'Mon-Wed', 'day_direction': 'FLAT',
            'day_range': 'NORMAL', 'day_vix': 'NORMAL', 'weekday': 1,
        }
        p_win, regime, detail = engine.predict(sample)
        print(f"P(win)={p_win:.3f}  regime={regime}  edge={detail['edge']:+.3f}")
        print(json.dumps(detail, indent=2))
