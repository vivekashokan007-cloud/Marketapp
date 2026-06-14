import importlib.util
import os


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BRAIN_PATH = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "brain.py"))


def load_brain():
    spec = importlib.util.spec_from_file_location("brain", BRAIN_PATH)
    brain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(brain)
    return brain


def sells_for(pairs):
    return {pair.get("sell") for pair in pairs}


def test_nf_put_wall_pair_survives_atm_walk():
    brain = load_brain()
    strikes = list(range(22000, 25001, 50))

    pairs = brain._get_strike_pairs(
        "BULL_PUT",
        atm=23500,
        width=150,
        step=50,
        all_strikes=strikes,
        spot=23520,
        is_bnf=False,
        cw=None,
        pw=22800,
    )

    assert len(pairs) == 17
    assert 22800 in sells_for(pairs)


def test_no_wall_small_universe_stays_ordered():
    brain = load_brain()

    pairs = brain._get_strike_pairs(
        "BULL_PUT",
        atm=23500,
        width=150,
        step=50,
        all_strikes=[23300, 23350, 23450, 23500],
        spot=23520,
        is_bnf=False,
        cw=None,
        pw=None,
    )

    assert pairs == [
        {"sell": 23500, "buy": 23350, "sellType": "PE", "buyType": "PE"},
        {"sell": 23450, "buy": 23300, "sellType": "PE", "buyType": "PE"},
    ]


def test_bnf_wall_pairs_survive_with_bounded_volume():
    brain = load_brain()
    strikes = list(range(52000, 61001, 100))

    pairs = brain._get_strike_pairs(
        "BEAR_CALL",
        atm=56800,
        width=500,
        step=100,
        all_strikes=strikes,
        spot=56805,
        is_bnf=True,
        cw=59000,
        pw=None,
    )

    assert len(pairs) == 23
    assert {58900, 59000}.issubset(sells_for(pairs))


def main():
    test_nf_put_wall_pair_survives_atm_walk()
    test_no_wall_small_universe_stays_ordered()
    test_bnf_wall_pairs_survive_with_bounded_volume()
    print("PASS: Stage 1 strike pair truncation checks passed")


if __name__ == "__main__":
    main()
