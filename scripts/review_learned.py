"""What the model has told callers that no database row backs. Review it; decide what is true.

    python scripts/review_learned.py                     # list, newest-used first
    python scripts/review_learned.py --confirm "<question>"   # agree: keep, stop flagging
    python scripts/review_learned.py --forget  "<question>"   # disagree: next caller gets a fresh answer
    python scripts/review_learned.py --language hin           # a language other than English

WHY THIS EXISTS. A caller asks something the hotel has no row for -- "is there a rooftop terrace?" --
and the model answers plausibly. That answer is written down so the next caller is not told
something different; see `aether/hotel/learned.py`.

A remembered answer is **consistent, not true**. Nothing verified the pool. Until a person says
otherwise it stays flagged as a guess, and this is where a person says otherwise.

Read it as a to-do list: anything with a high use count is a question your callers keep asking and
your database cannot answer. Confirming it is fine; adding the fact to the database properly is
better, because a real row is spoken in all three languages and this is not.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--confirm", metavar="QUESTION", help="mark this answer as agreed")
    ap.add_argument("--forget", metavar="QUESTION", help="delete it; the model answers afresh")
    ap.add_argument("--language", default="eng", help="eng (default), hin or spa")
    ap.add_argument("--all", action="store_true", help="include answers already confirmed")
    args = ap.parse_args()

    from aether.hotel.learned import LearnedAnswers

    store = LearnedAnswers()
    try:
        if args.confirm:
            ok = store.confirm(args.confirm, args.language)
            print("confirmed" if ok else "no such remembered question")
            return
        if args.forget:
            ok = store.forget(args.forget, args.language)
            print("forgotten" if ok else "no such remembered question")
            return

        rows = store.all(unconfirmed_only=not args.all)
        if not rows:
            print("Nothing remembered yet.")
            print("The model writes an answer down only when a caller asks something the database "
                  "cannot answer.")
            return

        print(f"{len(rows)} remembered answer(s), most-used first. "
              f"Stored in {store.path.name}.\n")
        for row in rows:
            flag = "CONFIRMED" if row.confirmed else "guess    "
            print(f"  [{flag}] [{row.language}] used {row.times_used}x   {row.created_at}")
            print(f"     asked : {row.asked_as}")
            print(f"     said  : {row.answer}")
            print()
        if any(not r.confirmed for r in rows):
            print("Each 'guess' is something the model made up because no row covered it.")
            print("  agree    : --confirm \"<the question>\"")
            print("  disagree : --forget  \"<the question>\"")
            print("  better   : put the fact in the database, so all three languages can say it.")
    finally:
        store.close()


if __name__ == "__main__":
    main()
