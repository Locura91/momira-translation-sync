"""
run_sync_packages.py — command-line entrypoint for Holiday Package translation sync.

FIRST RUN — always start like this (pick a real holiday package id from your
Momira Travel back office, or from a GET /package/{micrositeId} call):

    python run_sync_packages.py --package-id 59582825 --dry-run

This fetches ONE real package, shows you exactly what it detected as
translatable (title / largeTitle / description / ribbonText / remarks —
themes are still intentionally left untouched, since they're internal
category tags, not per-language text) and what it would write for 2 test
languages, and makes NO changes to Travel Compositor.

Then the full language list, still one package:

    python run_sync_packages.py --package-id 59582825 --dry-run --all-languages

Then, only once that looks right, go live on that one package:

    python run_sync_packages.py --package-id 59582825 --all-languages

Now that Travel Compositor has confirmed we're authorized to write Holiday
Packages, this is the real first live test. Read the printed result
carefully. If Travel Compositor still rejects the PUT (look for
"status": "failed" with an API error detail on any language), paste me the
exact error text and we'll adjust the payload from there.

If you need to re-translate something already marked "done" (e.g. right
after fixing a bug in the translator), add --force to bypass the tracker:

    python run_sync_packages.py --package-id 59582825 --dry-run --force

Finally, the whole microsite's package catalog:

    python run_sync_packages.py --all-languages
"""

import os
import sys
import json
import argparse

from dotenv import load_dotenv
load_dotenv()

from travelcompositor_api import TravelCompositorAPI
from translator import get_translator, required_api_key_env_var
from state_store import StateStore
from sync_holiday_package import sync_holiday_package, sync_all_holiday_packages

# Confirmed final list (30 targets, EN is always the source and never
# appears here) — narrowed down from the earlier 35-language draft by
# dropping PT_BR, ZH, FA, CA, EU, which are not needed after all.
DEFAULT_TARGET_LANGUAGES = [
    "FR", "SL", "PL", "DE", "SK", "AR", "HR", "HU", "AZ", "NL", "ES", "TR",
    "KA", "UZ", "RU", "NO", "SV", "RO", "BG", "CS", "TH", "EL", "FI", "JA",
    "SR", "PT", "DA", "IT", "MS", "SQ",
]

TEST_LANGUAGES = ["FR", "DE"]  # small, cheap sample for a first --dry-run


def get_target_languages(all_languages: bool) -> list:
    if all_languages:
        env_override = os.getenv("TC_TARGET_LANGUAGES")
        if env_override:
            return [l.strip().upper() for l in env_override.split(",") if l.strip()]
        return DEFAULT_TARGET_LANGUAGES
    return TEST_LANGUAGES


def main():
    parser = argparse.ArgumentParser(description="nbext translation-sync — Holiday Packages")
    parser.add_argument("--microsite-id", default=os.getenv("TRAVELC_MICROSITE_ID", "momiratravel"),
                         help="Travel Compositor microsite id (default from .env)")
    parser.add_argument("--package-id", help="Sync a single holiday package by id (omit to sync ALL packages for the microsite)")
    parser.add_argument("--all-languages", action="store_true", help="Use the full 30-language target list instead of the 2-language test set")
    parser.add_argument("--dry-run", action="store_true", help="Preview only — never calls PUT")
    parser.add_argument("--force", action="store_true", help="Ignore the 'already translated' tracker and re-translate anyway")
    parser.add_argument("--limit", type=int, default=None, help="When syncing all packages, only process the first N (for testing)")
    args = parser.parse_args()

    required_key = required_api_key_env_var()
    if not os.getenv(required_key):
        print(f"❌ {required_key} is not set. Add it to your .env file or export it, then try again.")
        sys.exit(1)
    if not os.getenv("TRAVELC_USERNAME") or not os.getenv("TRAVELC_PASSWORD"):
        print("❌ TRAVELC_USERNAME / TRAVELC_PASSWORD are not set. Check your .env file.")
        sys.exit(1)

    target_languages = get_target_languages(args.all_languages)
    print(f"🎯 Target languages this run: {target_languages}")
    if args.dry_run:
        print("🧪 DRY RUN MODE — no data will be written to Travel Compositor.\n")
    if args.force:
        print("🔁 FORCE MODE — ignoring the 'already translated' tracker for this run.\n")

    api = TravelCompositorAPI()
    translator = get_translator()
    store = StateStore()

    if args.package_id:
        result = sync_holiday_package(
            api, translator, store, args.microsite_id, args.package_id, target_languages,
            dry_run=args.dry_run, force=args.force,
        )
        print("\n=== RESULT ===")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        results = sync_all_holiday_packages(
            api, translator, store, args.microsite_id, target_languages,
            dry_run=args.dry_run, limit=args.limit, force=args.force,
        )
        print("\n=== SUMMARY ===")
        by_status = {}
        for r in results:
            by_status.setdefault(r["status"], []).append(r)
        for status, items in by_status.items():
            print(f"  {status}: {len(items)}")
        print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
