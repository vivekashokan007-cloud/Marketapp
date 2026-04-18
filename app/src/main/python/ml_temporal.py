"""
ml_temporal.py — Market Radar v2.1 Temporal Model (Layer 4)
============================================================
Pure Python (json + math only). Runs on S23 Ultra via Chaquopy.

Architecture: mini-GRU over last N_SEQ=6 poll readings.
Input:  float[N_SEQ][SEQ_FEAT]  — 6 polls × 6 features each
Output: float                   — sequence quality score (0–1)

6 sequence features per poll:
  0: vix_norm          — VIX / 30
  1: pcr_norm          — PCR / 2
  2: bias_net_norm     — (bias_net + 3) / 6
  3: breadth_norm      — breadth / 100
  4: spot_move_norm    — 5-min spot move / daily_sigma (clamped)
  5: futures_prem_norm — futures_prem / 300

Training:
  Phase 1 — synthetic: backtest trades → simulated 6-poll sequences (8,372 samples)
  Phase 2 — real: journey timeline rows from Supabase (grows with each trade)

Integration: TemporalEngine wraps this. Exported via integrate_with_ml_engine().
"""

import json
import math
import os

# ── Constants ─────────────────────────────────────────────────────────────────

TEMPORAL_VERSION = '1.0'
N_SEQ     = 6     # polls in sequence
SEQ_FEAT  = 6     # features per poll
HIDDEN    = 8     # GRU hidden size (keeps inference fast on S23)

def _sig(x):
    if x >  500: return 1.0
    if x < -500: return 0.0
    return 1.0 / (1.0 + math.exp(-x))

def _tanh(x):
    if x >  20: return  1.0
    if x < -20: return -1.0
    e2 = math.exp(2 * x)
    return (e2 - 1) / (e2 + 1)

def _clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)

def _lcg(seed, n):
    """Deterministic LCG pseudo-random floats in [-1, 1] for weight init."""
    out = []
    s = seed
    for _ in range(n):
        s = (1664525 * s + 1013904223) & 0xFFFFFFFF
        out.append(s / 0x7FFFFFFF - 1.0)
    return out

# ─────────────────────────────────────────────────────────────────────────────
# POLL FEATURE EXTRACTOR
# ─────────────────────────────────────────────────────────────────────────────

def extract_poll_features(poll):
    """
    Convert a single poll dict → float[SEQ_FEAT].
    Accepts both app poll dicts and Supabase poll history rows.
    """
    def gf(key, alt_keys=(), default=0.0):
        v = poll.get(key)
        if v is None:
            for k in alt_keys:
                v = poll.get(k)
                if v is not None: break
        if v is None: return default
        try: return float(v)
        except: return default

    vix        = gf('vix', ('VIX', 'vix_value'))
    pcr        = gf('pcr', ('nearAtmPcr', 'bnf_near_atm_pcr', 'near_atm_pcr'))
    bias_net   = gf('biasNet', ('bias_net', 'biasScore'))
    breadth    = gf('breadth', ('bnfBreadth', 'breadth_pct', 'bnf_breadth_pct'))
    spot_move  = gf('spotMovePct', ('spot_move', 'movePct'), 0.0)
    fut_prem   = gf('futuresPrem', ('futures_prem', 'futuresPremium', 'bnf_futures_prem'))

    # Spot move as σ-normalised: need vix for scale
    daily_sigma_pct = (vix / 100.0) / math.sqrt(252) if vix > 0 else 0.01
    spot_move_norm  = _clamp(spot_move / max(daily_sigma_pct, 0.001), -3, 3) / 3.0

    return [
        _clamp(vix / 30.0, 0, 1),                  # 0
        _clamp(pcr / 2.0, 0, 1),                   # 1
        _clamp((bias_net + 3) / 6.0, 0, 1),        # 2
        _clamp(breadth / 100.0, 0, 1),             # 3
        (spot_move_norm + 1.0) / 2.0,              # 4  rescale [-1,1]→[0,1]
        _clamp((fut_prem + 1.0) / 2.0, 0, 1),      # 5: futures %
    ]


def polls_to_sequence(polls, n=N_SEQ):
    """
    Takes last n app poll dicts → float[n][SEQ_FEAT].
    Pads with neutral values if fewer than n polls available.
    """
    neutral = [0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
    seqs = [extract_poll_features(p) for p in polls[-n:]]
    while len(seqs) < n:
        seqs.insert(0, neutral[:])
    return seqs[-n:]


# ─────────────────────────────────────────────────────────────────────────────
# MINI-GRU
# ─────────────────────────────────────────────────────────────────────────────

class MiniGRU:
    """
    Single-layer GRU: hidden_dim=HIDDEN, input_dim=SEQ_FEAT.
    Weights stored as flat float lists for JSON serialisation.
    """

    def __init__(self, input_dim=SEQ_FEAT, hidden_dim=HIDDEN):
        self.in_dim  = input_dim
        self.hid_dim = hidden_dim
        d = input_dim + hidden_dim
        # Gate weight matrices (update z, reset r, candidate h̃)
        self.Wz = None; self.bz = None
        self.Wr = None; self.br = None
        self.Wh = None; self.bh = None
        # Output layer
        self.Wo = None; self.bo = None
        self._init()

    def _init(self):
        d = self.in_dim + self.hid_dim
        h = self.hid_dim
        scale = math.sqrt(2.0 / (d + h))
        def w(seed, rows, cols):
            return [v * scale for v in _lcg(seed, rows * cols)]
        self.Wz = w(11,  h, d); self.bz = [0.0] * h
        self.Wr = w(23,  h, d); self.br = [0.0] * h
        self.Wh = w(37,  h, d); self.bh = [0.0] * h
        self.Wo = w(97,  1, h); self.bo = [0.0]

    # ── GRU cell: one step ───────────────────────────────────────────────────

    def _step(self, h, x):
        xh = x + h                        # concat [x_t, h_{t-1}]
        d  = self.in_dim + self.hid_dim

        def gate_vec(W, b, act):
            return [act(sum(W[j*d + i] * xh[i] for i in range(d)) + b[j])
                    for j in range(self.hid_dim)]

        z  = gate_vec(self.Wz, self.bz, _sig)
        r  = gate_vec(self.Wr, self.br, _sig)
        rh = [r[j] * h[j] for j in range(self.hid_dim)]
        xrh = x + rh
        h_tilde = [_tanh(sum(self.Wh[j*d + i] * xrh[i] for i in range(d)) + self.bh[j])
                   for j in range(self.hid_dim)]
        h_new = [(1 - z[j]) * h[j] + z[j] * h_tilde[j]
                 for j in range(self.hid_dim)]
        return h_new, z, r, h_tilde

    # ── Full forward pass over sequence ─────────────────────────────────────

    def forward(self, seq):
        """seq: list of SEQ_FEAT-length float vectors. Returns (p_win, h_final)."""
        h = [0.0] * self.hid_dim
        cache = []
        for x in seq:
            h, z, r, h_tilde = self._step(h, x)
            cache.append((h[:], z, r, h_tilde))
        # Output: linear → sigmoid
        out_raw = sum(self.Wo[i] * h[i] for i in range(self.hid_dim)) + self.bo[0]
        p = _sig(out_raw)
        return p, h, cache

    # ── BPTT (truncated, last-step only for speed) ────────────────────────────

    def train_step(self, seq, y_true, lr=0.005, l2=0.002):
        """Single sequence BPTT. Returns loss."""
        p, h_final, cache = self.forward(seq)
        loss = -y_true * math.log(max(p, 1e-15)) - (1 - y_true) * math.log(max(1 - p, 1e-15))

        # Output layer grad
        dout   = p - y_true
        dWo    = [dout * h_final[i] for i in range(self.hid_dim)]
        dbo    = [dout]
        dh     = [dout * self.Wo[i] for i in range(self.hid_dim)]

        d_total = self.in_dim + self.hid_dim

        # Truncated BPTT through full N_SEQ steps (TM5: increase from 2 to 6)
        bptt_start = max(0, len(seq) - 6)
        for t in reversed(range(bptt_start, len(seq))):
            h_prev = [0.0] * self.hid_dim if t == 0 else cache[t - 1][0]
            h_t, z, r, h_tilde = cache[t]
            x = seq[t]
            xh = x + h_prev

            # Gradients through GRU cell
            dh_tilde = [dh[j] * z[j] for j in range(self.hid_dim)]
            dz       = [dh[j] * (h_tilde[j] - h_prev[j]) for j in range(self.hid_dim)]
            dh_prev  = [(1 - z[j]) * dh[j] for j in range(self.hid_dim)]

            # h_tilde gradient
            dht_pre  = [dh_tilde[j] * (1 - h_tilde[j] ** 2) for j in range(self.hid_dim)]
            rh       = [r[j] * h_prev[j] for j in range(self.hid_dim)]
            xrh      = x + rh

            # z gate gradient
            dz_pre   = [dz[j] * z[j] * (1 - z[j]) for j in range(self.hid_dim)]
            # r gate gradient (via h_tilde)
            dr_pre   = [sum(dht_pre[j] * self.Wh[j * d_total + self.in_dim + k] * h_prev[k]
                            for j in range(self.hid_dim))
                        for k in range(self.hid_dim)]
            dr_pre2  = [dr_pre[k] * r[k] * (1 - r[k]) for k in range(self.hid_dim)]

            # Update output weights (L2)
            for i in range(self.hid_dim):
                self.Wo[i] -= lr * (dWo[i] + l2 * self.Wo[i])
            self.bo[0] -= lr * dbo[0]

            # Update Wh, bh
            for j in range(self.hid_dim):
                for i in range(d_total):
                    grad = dht_pre[j] * xrh[i] if i < self.in_dim else dht_pre[j] * rh[i - self.in_dim]
                    self.Wh[j * d_total + i] -= lr * (grad + l2 * self.Wh[j * d_total + i])
                self.bh[j] -= lr * dht_pre[j]

            # Update Wz, bz
            for j in range(self.hid_dim):
                for i in range(d_total):
                    self.Wz[j * d_total + i] -= lr * (dz_pre[j] * xh[i] + l2 * self.Wz[j * d_total + i])
                self.bz[j] -= lr * dz_pre[j]

            # Update Wr, br
            for j in range(self.hid_dim):
                for i in range(d_total):
                    self.Wr[j * d_total + i] -= lr * (dr_pre2[j] * xh[i] + l2 * self.Wr[j * d_total + i])
                self.br[j] -= lr * dr_pre2[j]

            # Propagate hidden grad back
            for k in range(self.hid_dim):
                dh_prev[k] += sum(
                    dz_pre[j] * self.Wz[j * d_total + self.in_dim + k] +
                    dr_pre2[j] * self.Wr[j * d_total + self.in_dim + k] +
                    dht_pre[j] * self.Wh[j * d_total + self.in_dim + k] * r[j]
                    for j in range(self.hid_dim)
                )
            dh = dh_prev

        return loss

    # ── Serialise ────────────────────────────────────────────────────────────

    def to_dict(self):
        return {
            'in': self.in_dim, 'hid': self.hid_dim,
            'Wz': self.Wz, 'bz': self.bz,
            'Wr': self.Wr, 'br': self.br,
            'Wh': self.Wh, 'bh': self.bh,
            'Wo': self.Wo, 'bo': self.bo,
        }

    @classmethod
    def from_dict(cls, d):
        m = cls(d['in'], d['hid'])
        m.Wz, m.bz = d['Wz'], d['bz']
        m.Wr, m.br = d['Wr'], d['br']
        m.Wh, m.bh = d['Wh'], d['bh']
        m.Wo, m.bo = d['Wo'], d['bo']
        return m


# ─────────────────────────────────────────────────────────────────────────────
# SYNTHETIC SEQUENCE GENERATOR (for backtest pre-training)
# ─────────────────────────────────────────────────────────────────────────────

def _synthetic_seq(row, n=N_SEQ):
    """
    Generate a synthetic N-poll sequence from a backtest trade row.
    Simulates how VIX/PCR/breadth might have evolved leading up to entry.
    Uses small LCG noise keyed on row values for reproducibility.
    """
    try:
        vix   = float(row.get('vix', 17))
        gs    = float(row.get('gap_sigma', 0) or 0)
        mv    = float(row.get('move_sigma', 0) or 0)
        drs   = float(row.get('day_range_sigma', 0.8) or 0.8)
        bias  = {'UP': 1, 'DOWN': -1, 'FLAT': 0}.get(
                    str(row.get('day_direction', 'FLAT')).upper(), 0)
    except:
        vix = 17; gs = 0; mv = 0; drs = 0.8; bias = 0

    daily_sigma_pct = (vix / 100.0) / math.sqrt(252)

    seq = []
    # LCG seed from row content for reproducibility
    seed = int(abs(vix * 100 + gs * 37 + mv * 13)) % 999983 + 1

    for t in range(n):
        # Polls 0..n-2: pre-entry conditions (less extreme)
        # Poll n-1: entry conditions
        frac = t / max(n - 1, 1)

        # VIX: starts closer to daily avg, arrives at entry VIX
        # TM7: Dynamic baseline to prevent "always rising" pattern in high VIX regimes
        vix_daily_avg = 17.0 if vix < 20 else (vix - 2.0)
        vix_t = vix_daily_avg + (vix - vix_daily_avg) * frac
        # Add small noise
        seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
        vix_t += (seed / 0xFFFFFFFF - 0.5) * 0.5

        # PCR: biased by day_direction (DOWN = more puts = higher PCR)
        pcr_base = 0.9 + bias * (-0.15) + gs * 0.08
        seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
        pcr_t = _clamp(pcr_base + (seed / 0xFFFFFFFF - 0.5) * 0.2, 0.3, 2.5)

        # Bias net: evolves toward final bias
        bias_t = bias * frac + (seed / 0xFFFFFFFF - 0.5) * 0.3

        # Breadth: correlated with day direction
        breadth_base = 50 + bias * 15
        seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
        breadth_t = _clamp(breadth_base + (seed / 0xFFFFFFFF - 0.5) * 20, 0, 100)

        # Spot move: evolves toward final move_sigma
        spot_move_t = mv * frac * daily_sigma_pct

        # Futures prem: small positive bias during up days
        fut_t = 15 + bias * 20

        seq.append(extract_poll_features({
            'vix': vix_t, 'pcr': pcr_t, 'biasNet': bias_t * 3,
            'breadth': breadth_t, 'spotMovePct': spot_move_t,
            'futuresPrem': fut_t,
        }))

    return seq


# ─────────────────────────────────────────────────────────────────────────────
# TEMPORAL ENGINE — wraps MiniGRU + metadata
# ─────────────────────────────────────────────────────────────────────────────

class TemporalEngine:

    WEIGHT = 0.15   # blend weight in ensemble: p_final = (1-W)*p_base + W*p_temporal

    def __init__(self):
        self.gru        = MiniGRU()
        self.trained    = False
        self.train_acc  = 0.0
        self.val_acc    = 0.0
        self.n_train    = 0
        self.version    = TEMPORAL_VERSION

    # ── Synthetic pre-training from backtest CSV rows ────────────────────────

    def fit_synthetic(self, rows, epochs=8, lr=0.005, log_fn=None):
        """
        Pre-train on synthetic sequences derived from backtest trade rows.
        Gives the GRU a baseline understanding of sequence→outcome mapping.
        """
        def log(msg):
            if log_fn: log_fn(msg)

        seqs = []
        labels = []
        for r in rows:
            y = str(r.get('won', '')).lower()
            if y not in ('true', '1', 'false', '0'):
                continue
            labels.append(1 if y in ('true', '1') else 0)
            seqs.append(_synthetic_seq(r))

        n = len(seqs)
        if n == 0: return
        self.n_train = n

        # Holdout: last 15%
        val_n = max(4, min(int(n * 0.15), n // 4))
        tr_n  = n - val_n

        import random
        indices = list(range(tr_n))

        for ep in range(epochs):
            total_loss = 0.0
            # TM10: Shuffle indices each epoch to improve SGD exploration
            random.shuffle(indices)
            for i in indices:
                total_loss += self.gru.train_step(seqs[i], labels[i], lr=lr)
            tr_acc = sum(
                1 for i in range(tr_n)
                if (self.gru.forward(seqs[i])[0] >= 0.5) == bool(labels[i])
            ) / max(tr_n, 1)
            val_acc = sum(
                1 for i in range(tr_n, n)
                if (self.gru.forward(seqs[i])[0] >= 0.5) == bool(labels[i])
            ) / max(val_n, 1)

            if log_fn and (ep + 1) % 5 == 0:
                log(f"  GRU ep {ep+1:>2}/{epochs}: "
                    f"loss={total_loss/tr_n:.4f}  tr_acc={tr_acc:.3f}  val_acc={val_acc:.3f}")

        self.train_acc = tr_acc
        self.val_acc   = val_acc
        self.trained   = True

    # ── Real sequence fine-tuning from journey timelines ────────────────────

    def fit_real(self, journey_rows, trade_outcomes, lr=0.003, log_fn=None):
        """
        Fine-tune on real poll sequences from journey timeline rows.
        journey_rows: list of {trade_id, vix, pcr, bias_net, ...} per poll
        trade_outcomes: dict of trade_id (int/str) → won (bool)
        """
        from collections import defaultdict
        by_trade = defaultdict(list)
        
        # T1: Ensure trade_ids are strings for consistent lookup
        outcomes = {str(k): bool(v) for k, v in trade_outcomes.items()}
        
        for r in journey_rows:
            tid = str(r.get('trade_id') or r.get('id') or '')
            if tid in outcomes:
                by_trade[tid].append(r)

        n_real = 0
        total_loss = 0.0
        for tid, polls in by_trade.items():
            # T2: Need at least 2 polls for a "sequence" signal
            if len(polls) < 2:
                continue
            
            # Extract features for all polls in journey
            seq_raw = [extract_poll_features(p) for p in polls]
            
            # Use polls_to_sequence to ensure fixed N_SEQ length with padding
            seq = polls_to_sequence(polls, n=N_SEQ)
            
            y = 1 if outcomes[tid] else 0
            total_loss += self.gru.train_step(seq, y, lr=lr)
            n_real += 1

        if log_fn and n_real > 0:
            log_fn(f"  GRU real fine-tune: {n_real} trades  avg_loss={total_loss/n_real:.4f}")

        return n_real

    # ── Online update — single sequence after trade closes ────────────────────

    def online_step(self, poll_sequence, won, lr=0.003):
        """Micro-update on a single trade's poll sequence."""
        if not self.trained: return
        seq = polls_to_sequence(poll_sequence) if isinstance(poll_sequence[0], dict) else poll_sequence
        self.gru.train_step(seq, 1 if won else 0, lr=lr)

    # ── Inference ────────────────────────────────────────────────────────────

    def predict(self, polls):
        """
        polls: last N app poll dicts (or fewer — will be zero-padded).
        Returns float[0..1] — temporal sequence quality score.
        """
        if not self.trained:
            return 0.5
        seq = polls_to_sequence(polls)
        p, _, _ = self.gru.forward(seq)
        return p

    def blend(self, p_base, polls):
        """
        Blend temporal score with base MLEngine prediction.
        Returns blended p_win.
        """
        if not self.trained or not polls:
            return p_base
        p_temp = self.predict(polls)
        return (1 - self.WEIGHT) * p_base + self.WEIGHT * p_temp

    # ── Serialise ────────────────────────────────────────────────────────────

    def to_dict(self):
        return {
            'ver': self.version, 'trained': self.trained,
            'n_train': self.n_train, 'ta': self.train_acc, 'va': self.val_acc,
            'gru': self.gru.to_dict(),
        }

    @classmethod
    def from_dict(cls, d):
        te = cls()
        te.version   = d.get('ver', TEMPORAL_VERSION)
        te.trained   = d.get('trained', False)
        te.n_train   = d.get('n_train', 0)
        te.train_acc = d.get('ta', 0.0)
        te.val_acc   = d.get('va', 0.0)
        te.gru       = MiniGRU.from_dict(d['gru'])
        return te


# ─────────────────────────────────────────────────────────────────────────────
# TRAIN FROM BACKTEST CSV
# ─────────────────────────────────────────────────────────────────────────────

def train_temporal(csv_path=None, rows=None, epochs=20, log_fn=None):
    """
    Train TemporalEngine from backtest CSV.
    Accepts either csv_path (string) or pre-loaded rows (list of dicts).
    """
    import csv as _csv
    if rows is None:
        rows = []
        with open(csv_path, 'r', newline='', encoding='utf-8') as f:
            for row in _csv.DictReader(f):
                rows.append(row)

    te = TemporalEngine()
    te.fit_synthetic(rows, epochs=epochs, log_fn=log_fn)
    return te


# ─────────────────────────────────────────────────────────────────────────────
# INTEGRATION HELPERS  (called from ml_train.py)
# ─────────────────────────────────────────────────────────────────────────────

def save_temporal(te, path):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(te.to_dict(), f, separators=(',', ':'))
    return os.path.getsize(path) // 1024


def load_temporal(path):
    with open(path, 'r', encoding='utf-8') as f:
        d = json.load(f)
    return TemporalEngine.from_dict(d)


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST
# ─────────────────────────────────────────────────────────────────────────────

def self_test():
    # Test poll feature extraction
    poll = {'vix': 18.5, 'pcr': 1.1, 'biasNet': 1, 'breadth': 55.0,
            'spotMovePct': 0.002, 'futuresPrem': 25}
    feat = extract_poll_features(poll)
    assert len(feat) == SEQ_FEAT, f"Expected {SEQ_FEAT} features, got {len(feat)}"
    assert all(0 <= f <= 1 for f in feat), f"Features out of range: {feat}"

    # Test sequence padding
    seq = polls_to_sequence([poll] * 3)
    assert len(seq) == N_SEQ
    assert len(seq[0]) == SEQ_FEAT

    # Test GRU forward
    gru = MiniGRU()
    p, h, _ = gru.forward(seq)
    assert 0 < p < 1, f"GRU output out of range: {p}"
    assert len(h) == HIDDEN

    # Test GRU train step
    loss0 = gru.train_step(seq, 1)
    loss1 = gru.train_step(seq, 1)
    assert loss1 < loss0 * 1.5, "Loss not converging"

    # Test TemporalEngine synthetic training (tiny)
    fake_rows = [
        {'won': 'True',  'vix': 18.0, 'gap_sigma': 0.0, 'move_sigma': -0.2,
         'day_range_sigma': 0.8, 'day_direction': 'DOWN'},
        {'won': 'False', 'vix': 24.0, 'gap_sigma': 1.5, 'move_sigma':  1.2,
         'day_range_sigma': 2.1, 'day_direction': 'UP'},
    ] * 20  # 40 rows

    te = TemporalEngine()
    te.fit_synthetic(fake_rows, epochs=3)
    assert te.trained

    # Test blend
    p_blended = te.blend(0.70, [poll] * 4)
    assert 0 < p_blended < 1, f"Blend out of range: {p_blended}"

    # Test serialise/deserialise
    d = te.to_dict()
    te2 = TemporalEngine.from_dict(d)
    p1 = te.predict([poll] * 4)
    p2 = te2.predict([poll] * 4)
    assert abs(p1 - p2) < 1e-6, f"Serialise mismatch: {p1} vs {p2}"

    print("self_test PASSED  ✓  (poll features, GRU forward, BPTT, synthetic train, serialise)")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    import time

    if len(sys.argv) == 1:
        self_test()
        sys.exit(0)

    if sys.argv[1] == 'train':
        csv_path   = sys.argv[2] if len(sys.argv) > 2 else 'backtest_trades.csv'
        model_path = sys.argv[3] if len(sys.argv) > 3 else 'temporal_model.json'
        epochs     = int(sys.argv[4]) if len(sys.argv) > 4 else 8

        t0 = time.time()
        te = train_temporal(csv_path=csv_path, epochs=epochs, log_fn=print)
        kb = save_temporal(te, model_path)
        elapsed = time.time() - t0
        print(f"\nTemporal model saved → {model_path}  ({kb} KB)  {elapsed:.1f}s")
        print(f"train_acc={te.train_acc:.3f}  val_acc={te.val_acc:.3f}  n={te.n_train}")

    elif sys.argv[1] == 'test':
        model_path = sys.argv[2] if len(sys.argv) > 2 else 'temporal_model.json'
        te = load_temporal(model_path)
        sample_polls = [
            {'vix': 18.0, 'pcr': 0.9, 'biasNet': -1, 'breadth': 35,
             'spotMovePct': -0.003, 'futuresPrem': 10},
        ] * N_SEQ
        p = te.predict(sample_polls)
        print(f"Sample prediction (6× bearish polls): {p:.4f}")
        p2 = te.blend(0.72, sample_polls)
        print(f"Blended with p_base=0.72: {p2:.4f}")
