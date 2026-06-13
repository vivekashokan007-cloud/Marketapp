Candidate parity fixtures are for Claude's top-1/top-6 regression gate.

Why they exist:
- The older fixture_a / fixture_b / fixture_c inputs do not include rich
  `bnfChain` / `nfChain` context, so they cannot verify candidate generation
  parity. They only pin verdict-level behavior.

Required capture source:
- one real chain-rich poll export or replayable input bundle containing:
  - poll_json
  - closed_trades_json
  - baseline_json
  - open_trades_json
  - ctx_json with bnfChain / nfChain present
  - optionally candidates_json / strike_oi_json

How to capture:
1. Save the real input bundle as JSON somewhere local.
2. Run:

   python app/src/main/python/tests/capture_candidate_parity_fixture.py \
     /path/to/input_bundle.json \
     monday_nf_bull_put_window

3. This writes two files in this fixtures directory:
   - monday_nf_bull_put_window.candidate_parity.json
   - monday_nf_bull_put_window.candidate_parity.baseline.json

What gets frozen:
- exact top-1 watchlist candidate signature
- exact top-6 watchlist candidate ids
- exact top-6 generated candidate ids

How to verify later:

   python app/src/main/python/tests/test_candidate_parity_contract.py

That is the regression gate Claude asked for before any Round 2 branch/gate reform.
