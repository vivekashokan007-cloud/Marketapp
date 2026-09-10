"""Regression contract for malformed evaluator output checkpoint recovery.

The Kotlin evaluator deliberately rejects malformed JSON. The resume decision
must instead discard the derived outcomes checkpoint, reset once, and restart
from the separately saved brain snapshots.
"""

import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
SERVICE = os.path.join(
    ROOT, "app", "src", "main", "java", "com", "marketradar", "app", "MarketMLService.kt"
)


class EvaluationMalformedOutputRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SERVICE, encoding="utf-8") as source:
            cls.service = source.read()

    def test_resume_count_is_guarded_by_the_expected_output_validation_error(self):
        self.assertRegex(
            self.service,
            re.compile(
                r"if \(canResume\) \{\s*"
                r"val existingProduced = try \{\s*"
                r"countJsonArrayFile\(outputsFile\)\s*"
                r"\} catch \(t: IllegalStateException\) \{",
                re.S,
            ),
        )

    def test_recovery_falls_through_to_the_single_existing_reset(self):
        match = re.search(
            r"canResume = false\s*\n\s*0\s*\n\s*\}"
            r"[\s\S]*?if \(!canResume\) \{\s*"
            r"writeJsonArrayFile\(outputsFile, org\.json\.JSONArray\(\)\)",
            self.service,
        )
        self.assertIsNotNone(match)
        decision = re.search(
            r"var canResume = \(prefs\.getString\(\"evaluation_job_date\"[\s\S]*?"
            r"EVAL_RESUME_CHECKPOINT",
            self.service,
        )
        self.assertIsNotNone(decision)
        self.assertEqual(
            decision.group(0).count("writeJsonArrayFile(outputsFile, org.json.JSONArray())"),
            1,
        )

    def test_recovery_is_durable_and_strict_reader_remains_strict(self):
        marker = "EVAL_RESUME_DISCARDED_MALFORMED_OUTPUTS: date=$sessionDate "
        self.assertGreaterEqual(self.service.count(marker), 2)
        self.assertIn("Log.w(", self.service)
        self.assertIn("LogBuffer.add(", self.service)
        self.assertIn('throw IllegalStateException("Evaluation output ${file.name} is malformed", t)', self.service)

    def test_atomic_second_batch_append_preserves_the_copied_json_prefix(self):
        # The first batch creates `[rows]`. For the second one, the writer
        # copies the prefix through the closing bracket and must append to
        # that temp file. File.outputStream() would truncate the prefix and
        # create `,rows]`, the exact field failure from 2026-09-10.
        self.assertIn("import java.io.FileOutputStream", self.service)
        self.assertRegex(
            self.service,
            re.compile(
                r"copyFilePrefix\(file, temp, closingOffset\)\s*"
                r"//[\s\S]*?FileOutputStream\(temp, true\)\.buffered\(\)\.use",
                re.S,
            ),
        )
        append_block = re.search(
            r"private fun appendJsonArrayFile\([\s\S]*?\n    \}",
            self.service,
        )
        self.assertIsNotNone(append_block)
        self.assertNotIn("temp.outputStream().buffered()", append_block.group(0))


if __name__ == "__main__":
    unittest.main()
