"""Measure the safety layers on a small labelled set.

    python scripts/eval_safety.py                  # layer 1 (regex) only, offline
    GROQ_API_KEY=... python scripts/eval_safety.py --model   # layer 1 + 2 as the app runs them

The cases are synthetic and hand-written: use this to see *what kind* of message each layer
catches or misses, and to compare model/prompt changes, not as a clinical benchmark.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.safety import assess_message, regex_category  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="store_true", help="also run the model classifier (needs GROQ_API_KEY)")
    parser.add_argument("--verbose", action="store_true", help="list every miss / false alarm")
    args = parser.parse_args()

    key = os.getenv("GROQ_API_KEY")
    if args.model and not key:
        sys.exit("--model needs GROQ_API_KEY in the environment")

    cases = json.loads((ROOT / "scripts" / "safety_cases.json").read_text(encoding="utf-8"))
    cases.pop("_note", None)
    # "flagged" means the app would show helplines / take extra care: crisis for crisis_* sets,
    # crisis-or-concern for the concern set; for benign text any flag is a false alarm.
    wants = {"crisis_explicit": {"crisis"}, "crisis_indirect": {"crisis"},
             "concern": {"concern", "crisis"}, "benign": {"none"}}

    def level(text):
        if args.model:
            time.sleep(0.2)  # be polite to the rate limit
            return assess_message([{"role": "user", "content": text}], key).level
        return "crisis" if regex_category(text) else "none"

    print(f"layers: {'regex + model' if args.model else 'regex only'}\n")
    print(f"{'set':<17}{'correct':>9}   what a wrong answer means")
    meaning = {"crisis_explicit": "missed an explicit crisis", "crisis_indirect": "missed a paraphrased crisis",
               "concern": "no extra care for a struggling user", "benign": "false alarm on everyday language"}
    for name, texts in cases.items():
        wrong = [t for t in texts if level(t) not in wants[name]]
        print(f"{name:<17}{len(texts) - len(wrong):>4}/{len(texts):<4}  {meaning[name]}")
        if args.verbose:
            for t in wrong:
                print(f"    - {t}")


if __name__ == "__main__":
    main()
