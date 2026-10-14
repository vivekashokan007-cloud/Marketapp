"""C7: measure engine time, analyze time with/without the experiment, incremental overhead and peak memory (box CPU;
the phone measurement is a pending phone check). Usage: PYTHONPATH=app/src/main/python:app/src/main/python/tests python3 tools/ev_v12_bench.py"""
import json, os, shutil, statistics, tempfile, time, tracemalloc, io, contextlib
import brain
import test_ev_v12_integration as T
import ev_v12_integration as I

inp = T.BrainAnalyze()._fx("fixture_a_bull_credit_range")
args = (inp["poll_json"], inp["closed_trades_json"], inp["baseline_json"], inp["open_trades_json"], "[]", "{}")
base = json.loads(inp["ctx_json"])


def once(with_ev):
    d = tempfile.mkdtemp()
    try:
        ev = T.ctx_for(d)
        c = dict(base, executionMode="paper", nfChain=ev["nfChain"], nfExpiry=ev["nfExpiry"])
        if with_ev:
            c.update({k: ev[k] for k in ev if k.startswith("ev")})
        t = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):
            out = json.loads(brain.analyze(*args, json.dumps(c)))
        return (time.perf_counter() - t) * 1000, out.get("implied_ev_v12")
    finally:
        shutil.rmtree(d)


once(False); once(True)
a = [once(False)[0] for _ in range(5)]
b = []
timings = None
for _ in range(5):
    ms, ev = once(True)
    b.append(ms)
    timings = ev.get("timings")
d = tempfile.mkdtemp()
tracemalloc.start()
I.run(T.ctx_for(d), {}, "paper")
cur, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
shutil.rmtree(d)
print(json.dumps({"analyze_ms_without_ev_median": round(statistics.median(a), 1),
                  "analyze_ms_with_ev_median": round(statistics.median(b), 1),
                  "incremental_ms_median": round(statistics.median(b) - statistics.median(a), 1),
                  "engine_timings_last": timings, "ev_run_peak_traced_mb": round(peak / 1e6, 2)}, indent=1))
