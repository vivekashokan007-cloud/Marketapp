"""Shared loaders for the implied-EV v1.2 tests (development CSV extracts, md5-manifested)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.dirname(HERE)
ROOT = os.path.abspath(os.path.join(PY, "..", "..", "..", ".."))
FIX = os.path.join(ROOT, "research", "ev_v12", "fixtures")
TOOLS = os.path.join(ROOT, "tools")
RESEARCH = os.path.join(ROOT, "research", "ev_v12")
for p in (PY, TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)

_CACHE = {}


def chains_by_day(name="chains.csv"):
    if name in _CACHE:
        return _CACHE[name]
    rows = {}
    with open(os.path.join(FIX, name)) as fh:
        lines = fh.read().splitlines()
    for line in lines:
        p = line.strip().split(",")
        if len(p) < 7:
            continue
        rows.setdefault(p[0], {"time": p[1], "expiry": p[2], "rows": []})["rows"].append(
            {"strike": p[3], "cp": p[4], "bid": p[5], "ask": p[6],
             "instrument_key": "NSE_FO|NIFTY%s%s%s" % (p[2].replace("-", ""), p[3], p[4]),
             "expiry": p[2], "index": "NF"})
    _CACHE[name] = rows
    return rows


def entry_chain(day):
    return chains_by_day("entry_all.csv")[day]


def kotlin_chain(day, fetched_ms):
    """The chain shape MarketWatchService.formatChainForBrain produces (strike -> {CE, PE})."""
    c = entry_chain(day)
    strikes = {}
    for r in c["rows"]:
        node = strikes.setdefault(str(int(float(r["strike"]))), {})
        node[r["cp"]] = {"bid": float(r["bid"]), "ask": float(r["ask"]), "ltp": float(r["bid"]),
                         "instrument_key": r["instrument_key"], "instrumentKey": r["instrument_key"]}
    ks = sorted(int(k) for k in strikes)
    # ATM by smallest |C-P| mid (what the Kotlin formatter reports from spot); enough for v7 to run
    def gap(k):
        n = strikes[str(k)]
        if "CE" not in n or "PE" not in n:
            return 1e18
        return abs((n["CE"]["bid"] + n["CE"]["ask"]) - (n["PE"]["bid"] + n["PE"]["ask"]))
    atm = min(ks, key=gap)
    return {"strikes": strikes, "atm": atm, "allStrikes": ks, "atmIv": 12.0, "pcr": 1.0, "nearAtmPCR": 1.0,
            "callWallStrike": atm + 250, "putWallStrike": atm - 250}
