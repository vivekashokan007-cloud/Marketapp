"""Synthetic record builders for direction-research tests and the end-to-end OC simulation.

Everything here is SYNTHETIC. Calendars produced by ``synthetic_weekdays`` are NOT exchange calendars
(no holidays); they exist only to exercise mechanics.
"""
import math
import random
from datetime import date, timedelta

from dr_core import IST, config_identity, ist_epoch, iso, sha256_hex, canon

H64 = lambda tag: sha256_hex('synthetic:' + tag)  # noqa: E731


def synthetic_weekdays(start, n):
    d = date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


STATIC_CONFIG = {'executionMode': 'paper', 'evExperimentMode': 'off', 'direction_threshold': 0.40,
                 'feature_flags': {'x1': True}}
CONTRACTS = {'poll_schema': 'poll_v7', 'chain_schema': 'chain_v3', 'direction_semantics_version': 'dir_sem_v1'}


def identity(static_config=None, contracts=None, **over):
    sc = dict(STATIC_CONFIG, **(static_config or {}))
    ct = dict(CONTRACTS, **(contracts or {}))
    ident = {'apk_sha256': H64('apk'), 'signing_cert_sha256': H64('cert'), 'version_code': 515,
             'python_bundle_commit': '0' * 40, 'python_sources_sha256': H64('py'),
             'config_identity_sha256': config_identity(sc, ct), 'input_contract_versions': ct,
             'execution_mode': sc['executionMode'], 'execution_environment': 'chaquopy_py311_android'}
    ident.update(over)
    return ident


def at(session, hhmmss, plus_s=0.0):
    return iso(ist_epoch(session, hhmmss) + plus_s)


def spot(obs_id, ix, session, hhmmss, price, plus_s=0.0, feed_lag_s=2.0, feed=True, clock_u=1.0):
    t = ist_epoch(session, hhmmss) + plus_s
    return {'schema': 'dr_spot_obs_v0', 'obs_id': obs_id, 'index': ix, 'price': price,
            'fetch_started_at': iso(t - 1.0), 'fetch_completed_at': iso(t),
            'feed_ts': iso(t - feed_lag_s) if feed else None, 'clock_id': 'device:dev1',
            'clock_uncertainty_s': clock_u, 'source_id': 'synthetic_ltp'}


def forecast(session, slot, label, record_id=None, avail_plus_s=60.0, level='L1', wm_plus_s=20.0,
             nf=24500.0, bnf=55000.0, device='dev1', source_kind='ENVELOPE', ident=None, mode='paper',
             legacy=None, status='OK', clock_u=1.0, server_received_plus_s=None, anchor=True):
    t = ist_epoch(session, slot + ':00')
    rec = {
        'schema': 'dr_forecast_input_v0', 'source_kind': source_kind,
        'record_id': record_id or 'f-%s-%s-%s' % (device, session, slot),
        'device_key': device, 'artifact_identity': ident if ident is not None else identity(), 'mode': mode,
        'session_date': session, 'slot': slot,
        'source_watermark': {'poll_id': 'p-%s-%s' % (session, slot), 'poll_ts': iso(t + wm_plus_s)},
        'availability': {'availability_level': level, 'available_at': iso(t + avail_plus_s),
                         'available_clock_id': 'device:' + device, 'available_clock_uncertainty_s': clock_u,
                         'server_received_at': None, 'anchored_content_sha256': None},
        'forecast': {'market_direction': label, 'status': status},
        'input_spots': {
            'NF': None if nf is None else {'price': nf, 'fetch_completed_at': iso(t + wm_plus_s),
                                           'feed_ts': iso(t + wm_plus_s - 2), 'clock_id': 'device:' + device,
                                           'clock_uncertainty_s': 1.0, 'source_id': 'synthetic_ltp'},
            'BNF': None if bnf is None else {'price': bnf, 'fetch_completed_at': iso(t + wm_plus_s),
                                             'feed_ts': iso(t + wm_plus_s - 2), 'clock_id': 'device:' + device,
                                             'clock_uncertainty_s': 1.0, 'source_id': 'synthetic_ltp'}},
    }
    if legacy:
        rec['legacy'] = legacy
    if level == 'L2':
        rec['availability']['server_received_at'] = iso(t + (server_received_plus_s if server_received_plus_s
                                                              is not None else avail_plus_s))
        if anchor:
            content = {k: rec[k] for k in ('source_kind', 'device_key', 'artifact_identity', 'mode',
                                           'session_date', 'slot', 'source_watermark', 'forecast', 'input_spots')}
            rec['availability']['anchored_content_sha256'] = sha256_hex(canon(content))
    return rec


SLOTS = ('09:30', '10:30', '11:30', '12:30', '13:30', '14:30')


def market_day(session, nf_path, bnf_path, tag='', s0_plus_s=270.0, end_plus_s=20.0, skip=()):
    """Spot observations for one session.

    ``nf_path``/``bnf_path``: dict with keys per slot (reference price at C+30 s) and 'end' (15:05:20).
    Watermark spots live inside forecast records; reference observations at T+4m30s, end at 15:05:20.
    """
    obs = []
    for ix, path in (('NF', nf_path), ('BNF', bnf_path)):
        for slot in SLOTS:
            if (ix, slot) in skip or path.get(slot) is None:
                continue
            obs.append(spot('o-%s%s-%s-%s-ref' % (tag, ix, session, slot), ix, session, slot + ':00', path[slot],
                            plus_s=s0_plus_s))
        if (ix, 'end') not in skip and path.get('end') is not None:
            obs.append(spot('o-%s%s-%s-end' % (tag, ix, session), ix, session, '15:05:00', path['end'],
                            plus_s=end_plus_s))
    return obs


def flat_path(base, end_delta=0.0):
    d = {slot: base for slot in SLOTS}
    d['end'] = base * math.exp(end_delta)
    return d


def history(sessions, rng_seed=7, nf0=24500.0, bnf0=55000.0, drift=0.0):
    """Synthetic market history spots for warm-up (no forecasts)."""
    rng = random.Random(rng_seed)
    obs = []
    for s in sessions:
        e1 = rng.choice((-1, 1)) * 0.004 + drift
        e2 = rng.choice((-1, 1)) * 0.004 + drift
        obs += market_day(s, flat_path(nf0, e1), flat_path(bnf0, e2), tag='h')
    return obs
