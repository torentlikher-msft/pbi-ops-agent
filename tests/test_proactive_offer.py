import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from eventhouse_monitor import format_greeting
from proactive_offer import (
    grounded_prompt,
    is_affirmative,
    is_negative,
    pending_expired,
)


class ProactiveOfferFallbackTests(unittest.TestCase):
    """`is_affirmative` / `is_negative` are the deterministic *fallback* used only
    when the model-based intent classifier fails; the probabilistic primary path is
    covered in ``test_intent_classifier.py``. These tests pin the fallback behaviour."""

    def test_affirmative_variants(self):
        for text in ["yes", "Yes please", "sure", "OK", "yeah", "yes, help me"]:
            self.assertTrue(is_affirmative(text), text)

    def test_negative_variants(self):
        for text in ["no", "No thanks", "not now", "later"]:
            self.assertTrue(is_negative(text), text)

    def test_unrelated_text_is_neither(self):
        self.assertFalse(is_affirmative("what does this measure do?"))
        self.assertFalse(is_negative("what does this measure do?"))

    def test_pending_expiry(self):
        self.assertTrue(pending_expired({"expiresAt": int(time.time()) - 10}))
        self.assertFalse(pending_expired({"expiresAt": int(time.time()) + 1000}))
        self.assertFalse(pending_expired({}))

    def test_grounded_prompt_embeds_query_and_scopes_analysis(self):
        prompt = grounded_prompt(
            {
                "model": "Sales",
                "report": "Exec Dashboard",
                "durationMs": 151091,
                "eventText": "EVALUATE Sales",
            }
        )
        self.assertIn("EVALUATE Sales", prompt)
        self.assertIn("Sales", prompt)
        self.assertIn("Exec Dashboard", prompt)
        self.assertIn("151.1s", prompt)
        self.assertIn("do not scan the entire model", prompt)

    def test_grounded_prompt_without_report(self):
        prompt = grounded_prompt({"model": "Sales", "eventText": "EVALUATE Sales"})
        self.assertNotIn("report and", prompt)


class FormatGreetingTests(unittest.TestCase):
    def test_greeting_with_report(self):
        message = format_greeting("Sales", "Exec Dashboard", 151091, 0)
        self.assertIn("Greetings!", message)
        self.assertIn("Exec Dashboard", message)
        self.assertIn("Sales", message)
        self.assertIn("151.1s", message)
        self.assertNotIn("other slow", message)

    def test_greeting_without_report_and_with_extras(self):
        message = format_greeting("Sales", None, 42000, 2)
        self.assertNotIn("report", message)
        self.assertIn("2 other slow queries", message)


if __name__ == "__main__":
    unittest.main()
