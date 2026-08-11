import importlib.util
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[5]
MODULE_PATH = REPO_ROOT / "tools" / "c3_context_percentile_backfill.py"
SPEC = importlib.util.spec_from_file_location("c3_context_percentile_backfill", MODULE_PATH)
assert SPEC and SPEC.loader
C3 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C3)


def _poll_row(day, value, *, verified=True, poll="09:20:00"):
    return {
        "session_date": day,
        "poll_ts": f"{day}T{poll}+00:00",
        "variable_name": "iv_richness_menu_median",
        "value": value,
        "pre_t_clean": True,
        "extra_json": {
            "candidate_population_scope": (
                C3.CALIBRATION_POPULATION_SCOPE if verified else "unverified_generated_population_only"
            ),
            "calibration_population_version": (
                C3.CALIBRATION_POPULATION_VERSION if verified else "unverified"
            ),
        },
    }


class TestC3CensoredCalibration(unittest.TestCase):
    def test_daily_rows_use_prior_days_only_and_keep_union_provenance(self):
        rows = C3._daily_calibration_rows(
            [
                _poll_row("2026-08-03", 1.0),
                _poll_row("2026-08-03", 1.2, poll="09:25:00"),
                _poll_row("2026-08-04", 1.3),
            ]
        )

        self.assertEqual(len(rows), 2)
        first, second = rows
        self.assertEqual(first["value"], 1.1)
        self.assertIsNone(first["pct_30"])
        self.assertEqual(first["support_count_30"], 0)
        self.assertIsNone(first["poll_ts"])
        self.assertEqual(second["pct_30"], 100.0)
        self.assertEqual(second["support_count_30"], 1)
        self.assertEqual(
            second["extra_json"]["candidate_population_scope"],
            C3.CALIBRATION_POPULATION_SCOPE,
        )
        self.assertEqual(
            second["extra_json"]["calibration_population_version"],
            C3.CALIBRATION_POPULATION_VERSION,
        )

    def test_daily_row_fails_provenance_closed_when_any_poll_is_unverified(self):
        rows = C3._daily_calibration_rows(
            [
                _poll_row("2026-08-03", 1.0),
                _poll_row("2026-08-03", 1.2, verified=False, poll="09:25:00"),
            ]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_quality"], "DAILY_CALIBRATION_PROVENANCE_UNVERIFIED")
        self.assertEqual(rows[0]["extra_json"]["calibration_population_version"], "unverified")

    def test_daily_row_ids_are_stable_for_resume_upserts(self):
        poll_rows = [_poll_row("2026-08-03", 1.0), _poll_row("2026-08-03", 1.2)]
        first = C3._daily_calibration_rows(poll_rows)
        second = C3._daily_calibration_rows(list(reversed(poll_rows)))

        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(first[0]["value"], second[0]["value"])


if __name__ == "__main__":
    unittest.main()
