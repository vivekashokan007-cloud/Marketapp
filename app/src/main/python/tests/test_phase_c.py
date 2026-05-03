import sys
import os
import statistics

# Add parent dir to path to import brain
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from brain import chain_profile, chain_profile_insights

# --- Mock Data Helpers ---

def make_strike(strike, c_oi=100, c_vol=50, c_iv=15.0, c_gr=None, p_oi=100, p_vol=50, p_iv=15.0, p_gr=None):
    return {
        'CE': {
            'oi': c_oi, 'volume': c_vol, 'iv': c_iv,
            'delta': (c_gr or {}).get('delta', 0.5),
            'gamma': (c_gr or {}).get('gamma', 0.001),
            'theta': (c_gr or {}).get('theta', -10.0),
            'vega': (c_gr or {}).get('vega', 5.0),
            'pop': (c_gr or {}).get('pop', 60.0),
            'prev_oi': (c_gr or {}).get('prev_oi')
        },
        'PE': {
            'oi': p_oi, 'volume': p_vol, 'iv': p_iv,
            'delta': (p_gr or {}).get('delta', -0.5),
            'gamma': (p_gr or {}).get('gamma', 0.001),
            'theta': (p_gr or {}).get('theta', -10.0),
            'vega': (p_gr or {}).get('vega', 5.0),
            'pop': (p_gr or {}).get('pop', 60.0),
            'prev_oi': (p_gr or {}).get('prev_oi')
        }
    }

def make_chain(strikes_list, atm=50000, cw=50500, pw=49500):
    all_k = sorted(strikes_list)
    strikes_dict = {str(k): make_strike(k) for k in all_k}
    return {
        'strikes': strikes_dict,
        'allStrikes': all_k,
        'atm': atm,
        'callWallStrike': cw,
        'putWallStrike': pw,
        'maxPain': atm
    }

# --- TG1: chain_profile Basic Shape ---

def test_profile_empty():
    assert chain_profile({}, 50000, {}) is None

def test_profile_no_atm():
    assert chain_profile({'strikes': {'50000': {}}, 'allStrikes': [50000]}, 50000, {}) is None

def test_profile_valid_keys():
    c = make_chain([49000, 49500, 50000, 50500, 51000])
    p = chain_profile(c, 50000, {'open': 50000, 'high': 50200, 'low': 49800})
    assert p is not None
    required = ['ivSkew', 'pcrZ1', 'pcrZ2', 'pcrZ3', 'cwFresh', 'cwOiChg', 'pwFresh', 'pwOiChg', 
                'atmSpread', 'dayRange', 'gapFill', 'callConc', 'putConc', 'pctFromOpen', 
                'maxPain', 'callWall', 'putWall', 'atm', 'spot', 'atmGamma', 'ivSlope', 
                'gammaCluster', 'volRatio', 'netDelta', 'avgTheta', 'avgVega', 'oiVelocity', 
                'bidAskQuality', 'callClusterDepth', 'putClusterDepth']
    for key in required:
        assert key in p, f"Missing key {key}"

# --- TG2: chain_profile Features ---

def test_profile_pcr_zones():
    # Sigma zones: atm=50000, vix=20 -> dailySigma=50000*0.2/sqrt(252) approx 630
    # k=50300 -> sigma = 300/630 approx 0.47 -> Zone 1
    # k=50400 -> sigma = 400/630 approx 0.63 -> Zone 2
    c = make_chain([50000, 50300, 50400, 50600])
    c['strikes']['50300']['PE']['oi'] = 200 # PCR=2.0
    p = chain_profile(c, 50000, None, vix=20)
    assert p['pcrZ1'] == 2.0

def test_profile_wall_freshness_oi_chg():
    c = make_chain([50000, 50500], cw=50500)
    c['strikes']['50500']['CE']['oi'] = 1000
    c['strikes']['50500']['CE']['volume'] = 500
    c['strikes']['50500']['CE']['prev_oi'] = 800
    p = chain_profile(c, 50000, None)
    assert p['cwFresh'] == 0.5
    assert p['cwOiChg'] == 200

def test_profile_iv_slope():
    # otmDist2 (1.0 sigma) approx 630. step=500. otmDist2=500*1=500
    # atm-500 = 49500
    c = make_chain([49500, 50000, 50500])
    c['strikes']['49500']['PE']['iv'] = 20.0
    c['strikes']['50500']['CE']['iv'] = 14.0
    p = chain_profile(c, 50000, None, vix=20)
    # slope = (20 - 14)/2 = 3.0
    assert p['ivSlope'] == 3.0

def test_profile_gamma_cluster():
    c = make_chain([50000, 50100, 50500])
    c['strikes']['50000']['CE']['gamma'] = 0.005
    c['strikes']['50100']['CE']['gamma'] = 0.005
    c['strikes']['50500']['CE']['gamma'] = 0.001
    p = chain_profile(c, 50000, None, vix=20)
    # gammaNear (k=50000, 50100) = 0.01 + some PE
    # gammaTotal includes 50500
    assert p['gammaCluster'] > 0.8

def test_profile_vol_ratio():
    c = make_chain([50000, 50100])
    c['strikes']['50000']['CE']['volume'] = 1000
    c['strikes']['50000']['PE']['volume'] = 500
    c['strikes']['50100']['CE']['volume'] = 0
    c['strikes']['50100']['PE']['volume'] = 0
    p = chain_profile(c, 50000, None)
    assert p['volRatio'] == 2.0

def test_profile_oi_velocity():
    c = make_chain([50000, 50100, 50200])
    c['strikes']['50100']['CE']['oi'] = 20000
    c['strikes']['50100']['CE']['prev_oi'] = 10000
    p = chain_profile(c, 50000, None)
    # velocity = (20000-10000)/10000 = 1.0 Lakh
    assert p['oiVelocity'] == 1.0

def test_profile_cluster_depth():
    # strikes with OI > 2*median
    # strikes: 49800, 49900, 50000, 50100, 50200. Median = ?
    strikes = [49800, 49900, 50000, 50100, 50200]
    c = make_chain(strikes, atm=50000, cw=50000, pw=50000)
    for k in strikes:
        c['strikes'][str(k)]['CE']['oi'] = 100
    # median = 100. threshold = 200
    c['strikes']['50000']['CE']['oi'] = 300
    c['strikes']['50100']['CE']['oi'] = 300
    p = chain_profile(c, 50000, None)
    # spot=50000 > 30000 -> radius=5. all strikes within radius
    assert p['callClusterDepth'] == 2

# --- TG4: chain_profile_insights ---

def test_insight_fear_skew():
    ctx = {'bnfProfile': {'ivSlope': 4.0}}
    ins = chain_profile_insights(ctx)
    assert any(i['label'].startswith("Fear skew steep") for i in ins)
    assert ins[0]['impact'] == 'bearish'

def test_insight_gamma_concentrated():
    ctx = {'bnfProfile': {'gammaCluster': 0.7}}
    ins = chain_profile_insights(ctx)
    assert any("Gamma concentrated" in i['label'] for i in ins)
    assert ins[0]['impact'] == 'caution'

def test_insight_vol_surge():
    ctx = {'bnfProfile': {'volRatio': 2.5}}
    ins = chain_profile_insights(ctx)
    assert any("Call buying surge" in i['label'] for i in ins)
    assert ins[0]['impact'] == 'bullish'

def test_insight_fortified_walls():
    ctx = {'bnfProfile': {'callClusterDepth': 3, 'putClusterDepth': 3}}
    ins = chain_profile_insights(ctx)
    assert any("Both walls fortified" in i['label'] for i in ins)
    assert ins[0]['impact'] == 'neutral'

def test_insight_fragile_wall():
    # Only fragile if neither is a fortress (>=3)
    ctx = {'bnfProfile': {'callClusterDepth': 1, 'putClusterDepth': 2}}
    ins = chain_profile_insights(ctx)
    assert any("Fragile call wall" in i['label'] for i in ins)
    assert ins[0]['impact'] == 'caution'

def test_insight_multiple():
    # Provide cluster depths to avoid "Fragile wall" insight
    ctx = {
        'bnfProfile': {
            'ivSlope': 4.0, 
            'volRatio': 0.2, 
            'netDelta': -4.0,
            'callClusterDepth': 2,
            'putClusterDepth': 2
        }
    }
    ins = chain_profile_insights(ctx)
    assert len(ins) == 3
    labels = [i['label'] for i in ins]
    assert any("Fear skew" in l for l in labels)
    assert any("Put buying surge" in l for l in labels)
    assert any("Net delta bearish" in l for l in labels)

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

if __name__ == "__main__":
    test_list = [
        ("test_profile_empty", test_profile_empty),
        ("test_profile_no_atm", test_profile_no_atm),
        ("test_profile_valid_keys", test_profile_valid_keys),
        ("test_profile_pcr_zones", test_profile_pcr_zones),
        ("test_profile_wall_freshness_oi_chg", test_profile_wall_freshness_oi_chg),
        ("test_profile_iv_slope", test_profile_iv_slope),
        ("test_profile_gamma_cluster", test_profile_gamma_cluster),
        ("test_profile_vol_ratio", test_profile_vol_ratio),
        ("test_profile_oi_velocity", test_profile_oi_velocity),
        ("test_profile_cluster_depth", test_profile_cluster_depth),
        ("test_insight_fear_skew", test_insight_fear_skew),
        ("test_insight_gamma_concentrated", test_insight_gamma_concentrated),
        ("test_insight_vol_surge", test_insight_vol_surge),
        ("test_insight_fortified_walls", test_insight_fortified_walls),
        ("test_insight_fragile_wall", test_insight_fragile_wall),
        ("test_insight_multiple", test_insight_multiple)
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
