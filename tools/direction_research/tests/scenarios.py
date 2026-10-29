"""Deterministic SYNTHETIC counterexample scenarios (serialised to fixtures/ by make_fixtures.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dr_synth as S  # noqa: E402

WARM = 25


def world(n_study=3, start='2026-11-02', seed=7):
    ss = S.synthetic_weekdays(start, WARM + n_study)
    return ss, ss[WARM], S.history(ss[:WARM], rng_seed=seed)


def base(n_study=3, label='BULL', end_delta=0.002):
    ss, st, obs = world(n_study)
    fc = []
    for s in ss[WARM:]:
        obs += S.market_day(s, S.flat_path(24500.0, end_delta), S.flat_path(55000.0, -end_delta))
        for sl in S.SLOTS:
            fc.append(S.forecast(s, sl, label))
    return {'sessions': ss, 'start': st, 'forecasts': fc, 'spots': obs}


def late_forecast_attractive_start():
    """Forecast available at C+30 s; an attractive (low) price was observed at T+60 s, before C."""
    sc = base(1)
    s = sc['start']
    sc['forecasts'] = [f for f in sc['forecasts'] if f['slot'] != '09:30']
    sc['forecasts'].append(S.forecast(s, '09:30', 'BULL', avail_plus_s=270.0))
    sc['spots'].append(S.spot('o-early-NF', 'NF', s, '09:30:00', 24300.0, plus_s=60.0))
    sc['spots'].append(S.spot('o-early-BNF', 'BNF', s, '09:30:00', 55400.0, plus_s=60.0))
    return sc


def post_cutoff_perturbation():
    sc = base(1)
    s = sc['start']
    sc['forecasts'].append(S.forecast(s, '10:30', 'BEAR', record_id='f-late-rewrite', avail_plus_s=600.0))
    return sc


def duplicates():
    sc = base(1)
    s = sc['start']
    sc['forecasts'].append(S.forecast(s, '09:30', 'BULL', record_id='f-dup-identical'))
    sc['forecasts'].append(S.forecast(s, '10:30', 'BEAR', record_id='f-dup-conflict', avail_plus_s=30.0))
    # combined-scope conflict that differs ONLY in the NF input spot
    sc['forecasts'].append(S.forecast(s, '11:30', 'BULL', record_id='f-dup-nfonly', nf=24499.0))
    return sc


def missing_or_stale_spots():
    sc = base(1)
    s = sc['start']
    keep = []
    for o in sc['spots']:
        if o['obs_id'] == 'o-NF-%s-09:30-ref' % s:
            continue                                        # NF S0 missing at 09:30
        if o['obs_id'] == 'o-BNF-%s-10:30-ref' % s:
            o = dict(o, feed_ts=S.at(s, '10:30:00', 100.0))  # quote formed before C: stale
        if o['obs_id'] == 'o-NF-%s-11:30-ref' % s:
            o = dict(o, feed_ts=None)                       # untimed
        if o['obs_id'] == 'o-BNF-%s-12:30-ref' % s:
            o = dict(o, clock_uncertainty_s=45.0)           # clock uncertainty straddles C
        if o['obs_id'] == 'o-NF-%s-end' % s:
            continue                                        # NF S1 missing
        keep.append(o)
    keep.append(S.spot('o-NF-early-0930', 'NF', s, '09:30:00', 24400.0, plus_s=200.0))   # before C
    keep.append(S.spot('o-NF-pre-end', 'NF', s, '15:04:00', 24600.0))                    # before end window
    sc['spots'] = keep
    return sc


def neutral_and_missing_slot():
    sc = base(1)
    s = sc['start']
    sc['forecasts'] = [f for f in sc['forecasts'] if f['slot'] not in ('09:30', '10:30')]
    sc['forecasts'].append(S.forecast(s, '09:30', 'NEUTRAL'))
    return sc


def boundary_label():
    sc = base(1)
    s = sc['start']
    sc['forecasts'] = [f for f in sc['forecasts'] if f['slot'] != '09:30']
    sc['forecasts'].append(S.forecast(s, '09:30', 'NEUTRAL',
                                      legacy={'direction': 'BULLISH', 'bull_score': 0.396, 'bear_score': 0.21,
                                              'confidence': 0.4}))
    sc['forecasts'] = [f for f in sc['forecasts'] if f['slot'] != '10:30']
    sc['forecasts'].append(S.forecast(s, '10:30', None, legacy={'direction': 'BULLISH', 'bull_score': 0.62}))
    return sc


def context_change():
    """Same Python sources; only evExperimentMode (static config) changes on session 2."""
    sc = base(2)
    s2 = sc['sessions'][WARM + 1]
    alt = S.identity(static_config={'evExperimentMode': 'observe'})
    sc['forecasts'] = [f if f['session_date'] != s2 else dict(f, artifact_identity=alt) for f in sc['forecasts']]
    return sc


def regime_span_sessions():
    """Synthetic weekdays spanning the R1/R2 boundary (7 Dec 2026)."""
    return S.synthetic_weekdays('2026-11-30', 50)


def mean_vs_majority():
    """19 small positive and 1 large negative 09:30 outcomes: majority BULL, mean-sign BEAR."""
    ss = S.synthetic_weekdays('2026-11-02', 22)
    obs = []
    for i, s in enumerate(ss[:20]):
        d = -0.05 if i == 10 else 0.001
        obs += S.market_day(s, S.flat_path(24500.0, d), S.flat_path(55000.0, d))
    s = ss[20]
    obs += S.market_day(s, S.flat_path(24500.0, 0.002), S.flat_path(55000.0, 0.002))
    fc = [S.forecast(s, sl, 'BEAR') for sl in S.SLOTS]
    return {'sessions': ss[:21], 'start': s, 'forecasts': fc, 'spots': obs}


def trend_gap():
    """B-TREND unavailable on one slot (no NF input spot) -> slot excluded for BOTH comparators."""
    sc = base(1)
    s = sc['start']
    sc['forecasts'] = [f for f in sc['forecasts'] if f['slot'] != '12:30']
    sc['forecasts'].append(S.forecast(s, '12:30', 'BULL', nf=None))
    return sc


def clustered_gap():
    """10 study sessions; sessions 3-6 have no forecasts at all (clustered missing run)."""
    sc = base(10)
    gap = set(sc['sessions'][WARM + 3:WARM + 7])
    sc['forecasts'] = [f for f in sc['forecasts'] if f['session_date'] not in gap]
    return sc


def l2_anchor():
    sc = base(1)
    s = sc['start']
    sc['forecasts'] = [f for f in sc['forecasts'] if f['slot'] not in ('09:30', '10:30', '11:30')]
    sc['forecasts'].append(S.forecast(s, '09:30', 'BULL', level='L2', server_received_plus_s=100.0))
    bad = S.forecast(s, '10:30', 'BULL', level='L2', server_received_plus_s=100.0)
    bad['availability']['anchored_content_sha256'] = S.H64('other-content')
    sc['forecasts'].append(bad)
    sc['forecasts'].append(S.forecast(s, '11:30', 'BULL', level='L2', server_received_plus_s=100.0, anchor=False))
    return sc


ALL = {k: v for k, v in globals().items() if callable(v) and k not in ('world', 'regime_span_sessions')
       and not k.startswith('_') and k not in ('ALL',) and getattr(v, '__module__', '') == __name__}
