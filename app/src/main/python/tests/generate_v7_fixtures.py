import json
import random

def generate_fixtures():
    fixtures = []
    for i in range(10):
        # Synthetic chain data
        bnf = {
            'atm': 48000 + (i * 100),
            'strikes': {
                str(48000 + (i * 100)): {'call_oi': 10000, 'put_oi': 12000, 'call_md': {'ltp': 200}, 'put_md': {'ltp': 180}}
            },
            'bnf_spot': 48050 + (i * 100),
            'futuresPremium': 0.05,
            'nearAtmPCR': 1.1 + (i * 0.02),
            'vix': 15 + i
        }
        nf = {
            'atm': 22000 + (i * 50),
            'strikes': {
                str(22000 + (i * 50)): {'call_oi': 50000, 'put_oi': 45000, 'call_md': {'ltp': 100}, 'put_md': {'ltp': 110}}
            },
            'nf_spot': 22025 + (i * 50)
        }
        morning = {
            'fiiCash': str(-1000 + (i * 200)),
            'diiCash': str(800 + (i * 50)),
            'fiiShortPct': str(80 - i),
            'giftSpot': str(22100 + (i * 50))
        }
        eve = {
            'dow': 38000,
            'crude': 85,
            'gift': 22000
        }
        gap = {
            'sigma': 0.5 if i % 2 == 0 else -0.5,
            'gap': 100 if i % 2 == 0 else -100
        }
        
        fixtures.append({
            'name': f'Fixture {i+1}',
            'context': {
                'bnfChain': bnf,
                'nfChain': nf,
                'morning_input': morning,
                'eveningClose': eve,
                'gap': gap,
                'yesterdayHistory': [{'fii_short_pct': 82}],
                'globalDirection': {'dowClose': 38200, 'crudeSettle': 84}
            }
        })
    
    with open('v7_fixtures.json', 'w') as f:
        json.dump(fixtures, f, indent=2)
    print("V7 fixtures generated in v7_fixtures.json")

if __name__ == "__main__":
    generate_fixtures()
