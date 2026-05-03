import sys
import os

# Add parent dir to path to import brain
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from brain import (
    compute_morning_bias, 
    institutional_regime, 
    fii_short_trend, 
    validate_yesterday_signal, 
    compute_overnight_delta
)

# Mock ctx generator
def make_ctx(morning=None, chain=None, yday=None, overnight=None, gap=None, accuracy=None, y_signal=None):
    return {
        'morning_input': morning or {'dummy': True}, # Ensure not early-exit
        'chain_data': chain or {},
        'yesterdayHistory': yday or [],
        'overnightDelta': overnight,
        'gap': gap or {'type': 'FLAT', 'gap': 0, 'sigma': 0},
        'signalAccuracy': accuracy or {'total': 30, 'correct': 20, 'pct': 67},
        'yesterdaySignal': y_signal
    }

def run_test(name, func):
    try:
        func()
        print(f"PASS: {name}")
        return True
    except Exception as e:
        print(f"FAIL: {name} - {str(e)}")
        import traceback
        traceback.print_exc()
        return False

# --- 1. compute_morning_bias (15+ tests) ---

def test_bias_fii_cash_bull():
    ctx = make_ctx(morning={'fiiCash': '600'})
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bull'] == 1 and res['votes']['bear'] == 0

def test_bias_fii_cash_bear():
    ctx = make_ctx(morning={'fiiCash': '-600'})
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bear'] == 1 and res['votes']['bull'] == 0

def test_bias_fii_short_bull():
    ctx = make_ctx(morning={'fiiShortPct': '65'})
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bull'] == 1

def test_bias_fii_short_bear_new():
    ctx = make_ctx(morning={'fiiShortPct': '88'}, yday=[{'fii_short_pct': 86}])
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bear'] == 1

def test_bias_fii_short_neutral_covering():
    ctx = make_ctx(morning={'fiiShortPct': '88'}, yday=[{'fii_short_pct': 90}])
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bear'] == 0

def test_bias_close_char_bull():
    ctx = make_ctx(chain={'closeChar': 2})
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bull'] == 1

def test_bias_close_char_bear():
    ctx = make_ctx(chain={'closeChar': -2})
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bear'] == 1

def test_bias_pcr_bull():
    ctx = make_ctx(chain={'nearAtmPCR': 1.3})
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bull'] == 1

def test_bias_pcr_bear():
    ctx = make_ctx(chain={'nearAtmPCR': 0.8})
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bear'] == 1

def test_bias_vix_dir_bull():
    ctx = make_ctx(chain={'vix': 12.0}, yday=[{'vix': 12.5}])
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bull'] == 1

def test_bias_vix_dir_bear():
    ctx = make_ctx(chain={'vix': 13.0}, yday=[{'vix': 12.5}])
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bear'] == 1

def test_bias_fut_prem_bull():
    ctx = make_ctx(chain={'futuresPremium': 0.06})
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bull'] == 1

def test_bias_fut_prem_bear():
    ctx = make_ctx(chain={'futuresPremium': -0.06})
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bear'] == 1

def test_bias_dii_floor_bull():
    ctx = make_ctx(morning={'fiiCash': '-1000', 'diiCash': '1300'}) # ratio 1.3
    res = compute_morning_bias(ctx, [])
    assert res['votes']['bull'] == 1

def test_bias_dii_floor_bear():
    ctx = make_ctx(morning={'fiiCash': '-1000', 'diiCash': '200'}) # ratio 0.2
    res = compute_morning_bias(ctx, [])
    # 1 from Cash BEAR + 1 from DII Floor BEAR
    assert res['votes']['bear'] == 2

def test_bias_chain_val_confirmed_bull():
    # 2 BEAR signals from insti
    morning = {'fiiCash': '-1000', 'fiiShortPct': '90'}
    # Overnight 2 BULL signals -> o_dir = BULL
    overnight = {'signals': [
        {'name': 'GIFT', 'dir': 'BULL', 'pct': 1.0, 'from': 20000, 'to': 20200, 'isSigma': True},
        {'name': 'Dow', 'dir': 'BULL', 'pct': 0.5, 'from': 30000, 'to': 30150}
    ]}
    gap = {'sigma': 0.6} # g_dir = BULL
    ctx = make_ctx(morning=morning, overnight=overnight, gap=gap, yday=[{'fii_short_pct': 80}])
    res = compute_morning_bias(ctx, [])
    assert res['chainValidation'] == 'CONFIRMED'
    assert res['votes']['bull'] == 2
    assert res['votes']['bear'] == 1

def test_bias_chain_val_confirmed_bear():
    morning = {'fiiCash': '1000', 'fiiShortPct': '60'} # 2 BULL
    overnight = {'signals': [
        {'name': 'GIFT', 'dir': 'BEAR', 'pct': -1.0, 'from': 20000, 'to': 19800, 'isSigma': True},
        {'name': 'Dow', 'dir': 'BEAR', 'pct': -0.5, 'from': 30000, 'to': 29850}
    ]}
    gap = {'sigma': -0.6} # g_dir = BEAR
    ctx = make_ctx(morning=morning, overnight=overnight, gap=gap, yday=[{'fii_short_pct': 80}])
    res = compute_morning_bias(ctx, [])
    assert res['chainValidation'] == 'CONFIRMED'
    assert res['votes']['bear'] == 2
    assert res['votes']['bull'] == 1

def test_bias_chain_val_likely_bull():
    morning = {'fiiCash': '-1000'} # 1 BEAR
    overnight = {'signals': [
        {'name': 'GIFT', 'dir': 'BULL', 'pct': 1.0, 'from': 20000, 'to': 20200, 'isSigma': True},
        {'name': 'Dow', 'dir': 'BULL', 'pct': 0.5, 'from': 30000, 'to': 30150}
    ]}
    gap = {'sigma': 0.0} # g_dir = NEUTRAL
    ctx = make_ctx(morning=morning, overnight=overnight, gap=gap)
    res = compute_morning_bias(ctx, [])
    assert res['chainValidation'] == 'LIKELY'
    assert res['votes']['bull'] == 1
    assert res['votes']['bear'] == 1

def test_bias_chain_val_likely_bear():
    morning = {'fiiCash': '1000'} # 1 BULL
    overnight = {'signals': [
        {'name': 'GIFT', 'dir': 'BEAR', 'pct': -1.0, 'from': 20000, 'to': 19800, 'isSigma': True},
        {'name': 'Dow', 'dir': 'BEAR', 'pct': -0.5, 'from': 30000, 'to': 29850}
    ]}
    gap = {'sigma': 0.0} # g_dir = NEUTRAL
    ctx = make_ctx(morning=morning, overnight=overnight, gap=gap)
    res = compute_morning_bias(ctx, [])
    assert res['chainValidation'] == 'LIKELY'
    assert res['votes']['bear'] == 1
    assert res['votes']['bull'] == 1

def test_bias_chain_val_uncertain_bull():
    morning = {'fiiCash': '1000'} # 1 BULL
    overnight = {'signals': [
        {'name': 'GIFT', 'dir': 'BULL', 'pct': 1.0, 'from': 20000, 'to': 20200, 'isSigma': True},
        {'name': 'Dow', 'dir': 'BULL', 'pct': 0.1, 'from': 30000, 'to': 30030}
    ]}
    gap = {'sigma': -0.6} # g_dir = BEAR (Conflict)
    ctx = make_ctx(morning=morning, overnight=overnight, gap=gap)
    res = compute_morning_bias(ctx, [])
    assert res['chainValidation'] == 'UNCERTAIN'
    assert res['votes']['bull'] == 1

def test_bias_chain_val_uncertain_bear():
    morning = {'fiiCash': '-1000'} # 1 BEAR
    overnight = {'signals': [
        {'name': 'GIFT', 'dir': 'BEAR', 'pct': -1.0, 'from': 20000, 'to': 19800, 'isSigma': True},
        {'name': 'Dow', 'dir': 'BEAR', 'pct': -0.1, 'from': 30000, 'to': 29970}
    ]}
    gap = {'sigma': 0.6} # g_dir = BULL (Conflict)
    ctx = make_ctx(morning=morning, overnight=overnight, gap=gap)
    res = compute_morning_bias(ctx, [])
    assert res['chainValidation'] == 'UNCERTAIN'
    assert res['votes']['bear'] == 1

# --- 2. institutional_regime (6 tests) ---

def test_regime_panic():
    ctx = make_ctx(morning={'fiiCash': '-1000', 'diiCash': '200', 'fiiStkFut': '-100'})
    assert institutional_regime(ctx)['regime'] == 'PANIC'

def test_regime_repositioning():
    ctx = make_ctx(morning={'fiiCash': '-1000', 'fiiIdxFut': '100'})
    assert institutional_regime(ctx)['regime'] == 'REPOSITIONING'

def test_regime_rotation():
    ctx = make_ctx(morning={'fiiCash': '-1000', 'fiiStkFut': '300'})
    assert institutional_regime(ctx)['regime'] == 'ROTATION'

def test_regime_accumulation():
    ctx = make_ctx(morning={'fiiCash': '1000', 'diiCash': '200'})
    assert institutional_regime(ctx)['regime'] == 'ACCUMULATION'

def test_regime_defended():
    ctx = make_ctx(morning={'fiiCash': '-1000', 'diiCash': '850'})
    assert institutional_regime(ctx)['regime'] == 'DEFENDED'

def test_regime_normal():
    ctx = make_ctx(morning={'fiiCash': '100', 'diiCash': '50'})
    assert institutional_regime(ctx)['regime'] == 'NORMAL'

# --- 3. fii_short_trend (6 tests) ---

def test_fii_trend_covering():
    ctx = make_ctx(morning={'fiiShortPct': '80'}, yday=[{'fii_short_pct': 82}, {'fii_short_pct': 85}])
    assert fii_short_trend(ctx)['trend'] == 'COVERING'

def test_fii_trend_building():
    ctx = make_ctx(morning={'fiiShortPct': '85'}, yday=[{'fii_short_pct': 82}, {'fii_short_pct': 80}])
    assert fii_short_trend(ctx)['trend'] == 'BUILDING'

def test_fii_trend_inflection():
    ctx = make_ctx(morning={'fiiShortPct': '82'}, yday=[{'fii_short_pct': 80}, {'fii_short_pct': 85}])
    assert fii_short_trend(ctx)['trend'] == 'INFLECTION'

def test_fii_trend_flat():
    ctx = make_ctx(morning={'fiiShortPct': '80'}, yday=[{'fii_short_pct': 80}])
    assert fii_short_trend(ctx)['trend'] == 'FLAT'

def test_fii_trend_accel():
    ctx = make_ctx(morning={'fiiShortPct': '85'}, yday=[{'fii_short_pct': 80}, {'fii_short_pct': 77}])
    # changes: 5, 3. 5 > 3*1.3 (3.9).
    assert fii_short_trend(ctx)['accel'] is True

def test_fii_trend_aggressive():
    ctx = make_ctx(morning={'fiiShortPct': '85'}, yday=[{'fii_short_pct': 81}])
    assert fii_short_trend(ctx)['aggressive'] is True

# --- 4. validate_yesterday_signal (6 tests) ---

def test_val_bull_correct():
    ctx = make_ctx(gap={'gap': 100}, yday=[{'date': 'D'}], y_signal={'tomorrow_signal': 'BULLISH'})
    assert validate_yesterday_signal(ctx)['correct'] is True

def test_val_bull_incorrect():
    ctx = make_ctx(gap={'gap': -100}, yday=[{'date': 'D'}], y_signal={'tomorrow_signal': 'BULLISH'})
    assert validate_yesterday_signal(ctx)['correct'] is False

def test_val_bear_correct():
    ctx = make_ctx(gap={'gap': -100}, yday=[{'date': 'D'}], y_signal={'tomorrow_signal': 'BEARISH'})
    assert validate_yesterday_signal(ctx)['correct'] is True

def test_val_neutral_correct_tight():
    ctx = make_ctx(gap={'gap': 30}, yday=[{'date': 'D'}], y_signal={'tomorrow_signal': 'NEUTRAL'})
    assert validate_yesterday_signal(ctx)['correct'] is True

def test_val_neutral_correct_tolerance():
    ctx = make_ctx(gap={'gap': 80}, yday=[{'date': 'D'}], y_signal={'tomorrow_signal': 'NEUTRAL'})
    # actualDir is BULLISH (>50), but correct is true if predicted neutral and gap < 100
    assert validate_yesterday_signal(ctx)['correct'] is True

def test_val_neutral_incorrect():
    ctx = make_ctx(gap={'gap': 120}, yday=[{'date': 'D'}], y_signal={'tomorrow_signal': 'NEUTRAL'})
    assert validate_yesterday_signal(ctx)['correct'] is False

# --- 5. compute_overnight_delta (7 tests) ---

def test_delta_dow_bull():
    ctx = make_ctx()
    ctx['eveningClose'] = {'dow': 30000}
    ctx['globalDirection'] = {'dowClose': 30300}
    res = compute_overnight_delta(ctx)
    assert any(s['name'] == 'Dow' and s['dir'] == 'BULL' for s in res['signals'])

def test_delta_dow_bear():
    ctx = make_ctx()
    ctx['eveningClose'] = {'dow': 30000}
    ctx['globalDirection'] = {'dowClose': 29700}
    res = compute_overnight_delta(ctx)
    assert any(s['name'] == 'Dow' and s['dir'] == 'BEAR' for s in res['signals'])

def test_delta_crude_bull():
    ctx = make_ctx()
    ctx['eveningClose'] = {'crude': 80}
    ctx['globalDirection'] = {'crudeSettle': 78} # crude down = bull
    res = compute_overnight_delta(ctx)
    assert any(s['name'] == 'Crude' and s['dir'] == 'BULL' for s in res['signals'])

def test_delta_crude_bear():
    ctx = make_ctx()
    ctx['eveningClose'] = {'crude': 80}
    ctx['globalDirection'] = {'crudeSettle': 82} # crude up = bear
    res = compute_overnight_delta(ctx)
    assert any(s['name'] == 'Crude' and s['dir'] == 'BEAR' for s in res['signals'])

def test_delta_gift_bull():
    ctx = make_ctx(gap={'sigma': 0.5})
    ctx['eveningClose'] = {'gift': 20000}
    res = compute_overnight_delta(ctx)
    assert any(s['name'] == 'GIFT' and s['dir'] == 'BULL' for s in res['signals'])

def test_delta_summary_bearish():
    ctx = make_ctx()
    ctx['eveningClose'] = {'dow': 30000, 'crude': 80}
    ctx['globalDirection'] = {'dowClose': 29700, 'crudeSettle': 82}
    res = compute_overnight_delta(ctx)
    assert res['summary'] == '🔴 OVERNIGHT BEARISH'

def test_delta_summary_neutral():
    ctx = make_ctx()
    ctx['eveningClose'] = {'dow': 30000, 'crude': 80}
    ctx['globalDirection'] = {'dowClose': 30000, 'crudeSettle': 80}
    res = compute_overnight_delta(ctx)
    assert res['summary'] == '⚪ OVERNIGHT NEUTRAL'

if __name__ == "__main__":
    test_list = [
        # Bias (21)
        ("test_bias_fii_cash_bull", test_bias_fii_cash_bull),
        ("test_bias_fii_cash_bear", test_bias_fii_cash_bear),
        ("test_bias_fii_short_bull", test_bias_fii_short_bull),
        ("test_bias_fii_short_bear_new", test_bias_fii_short_bear_new),
        ("test_bias_fii_short_neutral_covering", test_bias_fii_short_neutral_covering),
        ("test_bias_close_char_bull", test_bias_close_char_bull),
        ("test_bias_close_char_bear", test_bias_close_char_bear),
        ("test_bias_pcr_bull", test_bias_pcr_bull),
        ("test_bias_pcr_bear", test_bias_pcr_bear),
        ("test_bias_vix_dir_bull", test_bias_vix_dir_bull),
        ("test_bias_vix_dir_bear", test_bias_vix_dir_bear),
        ("test_bias_fut_prem_bull", test_bias_fut_prem_bull),
        ("test_bias_fut_prem_bear", test_bias_fut_prem_bear),
        ("test_bias_dii_floor_bull", test_bias_dii_floor_bull),
        ("test_bias_dii_floor_bear", test_bias_dii_floor_bear),
        ("test_bias_chain_val_confirmed_bull", test_bias_chain_val_confirmed_bull),
        ("test_bias_chain_val_confirmed_bear", test_bias_chain_val_confirmed_bear),
        ("test_bias_chain_val_likely_bull", test_bias_chain_val_likely_bull),
        ("test_bias_chain_val_likely_bear", test_bias_chain_val_likely_bear),
        ("test_bias_chain_val_uncertain_bull", test_bias_chain_val_uncertain_bull),
        ("test_bias_chain_val_uncertain_bear", test_bias_chain_val_uncertain_bear),
        # Regime (6)
        ("test_regime_panic", test_regime_panic),
        ("test_regime_repositioning", test_regime_repositioning),
        ("test_regime_rotation", test_regime_rotation),
        ("test_regime_accumulation", test_regime_accumulation),
        ("test_regime_defended", test_regime_defended),
        ("test_regime_normal", test_regime_normal),
        # FII Trend (6)
        ("test_fii_trend_covering", test_fii_trend_covering),
        ("test_fii_trend_building", test_fii_trend_building),
        ("test_fii_trend_inflection", test_fii_trend_inflection),
        ("test_fii_trend_flat", test_fii_trend_flat),
        ("test_fii_trend_accel", test_fii_trend_accel),
        ("test_fii_trend_aggressive", test_fii_trend_aggressive),
        # Validation (6)
        ("test_val_bull_correct", test_val_bull_correct),
        ("test_val_bull_incorrect", test_val_bull_incorrect),
        ("test_val_bear_correct", test_val_bear_correct),
        ("test_val_neutral_correct_tight", test_val_neutral_correct_tight),
        ("test_val_neutral_correct_tolerance", test_val_neutral_correct_tolerance),
        ("test_val_neutral_incorrect", test_val_neutral_incorrect),
        # Delta (7)
        ("test_delta_dow_bull", test_delta_dow_bull),
        ("test_delta_dow_bear", test_delta_dow_bear),
        ("test_delta_crude_bull", test_delta_crude_bull),
        ("test_delta_crude_bear", test_delta_crude_bear),
        ("test_delta_gift_bull", test_delta_gift_bull),
        ("test_delta_summary_bearish", test_delta_summary_bearish),
        ("test_delta_summary_neutral", test_delta_summary_neutral),
    ]
    
    passed = 0
    for name, func in test_list:
        if run_test(name, func):
            passed += 1
            
    print(f"\nRESULT: {passed}/{len(test_list)} tests passed.")
    if passed == len(test_list):
        sys.exit(0)
    else:
        sys.exit(1)
