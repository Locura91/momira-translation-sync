#!/usr/bin/env python3
"""
run_sync_tickets.py — sync tickets and their options.
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
from sync_ticket import (
    sync_ticket,
    sync_all_options_for_ticket,
    fetch_all_tickets,
)

DEFAULT_TARGET_LANGUAGES = [
    "FR", "SL", "PL", "DE", "SK", "AR", "HR", "HU", "AZ", "NL", "ES", "TR",
    "KA", "UZ", "RU", "NO", "SV", "RO", "BG", "CS", "TH", "EL", "FI", "JA",
    "SR", "PT", "DA", "IT", "MS", "SQ",
]
TEST_LANGUAGES = ["FR", "DE"]


def get_target_languages(all_languages: bool) -> list:
    if all_languages:
        env_override = os.getenv("TC_TARGET_LANGUAGES")
        if env_override:
            return [l.strip().upper() for l in env_override.split(",") if l.strip()]
        return DEFAULT_TARGET_LANGUAGES
    return TEST_LANGUAGES


def sync_all_tickets(api, translator, store: StateStore,
                     supplier_id: str, target_languages: List[str],
                     dry_run: bool = True, limit: int = None,
                     force: bool = False) -> List[Dict[str, Any]]:
    """Sync all tickets for a supplier and their options."""
    tickets = fetch_all_tickets(api, supplier_id, limit=limit)
    print(f"📋 Found {len(tickets)} ticket(s) for supplier {supplier_id}.")

    results = []
    for t in tickets:
        code = t.get("code")
        if not code:
            results.append({"status": "skipped", "reason": "no code field", "raw": t})
            continue

        # Sync main ticket
        main_result = sync_ticket(api, translator, store, supplier_id, code,
                                  target_languages, dry_run=dry_run, force=force)
        results.append(main_result)

        # Sync options if main ticket was not a fatal failure
        if main_result.get("status") not in ("fetch_failed", "skipped"):
            option_results = sync_all_options_for_ticket(api, translator, store,
                                                         supplier_id, code,
                                                         target_languages,
                                                         dry_run=dry_run, force=force)
            if isinstance(main_result, dict):
                main_result["options"] = option_results
            else:
                results.extend(option_results)

    return results


def main():
    parser = argparse.ArgumentParser(description="Sync tickets and their options")
    parser.add_argument("--supplier-id", required=True, help="Numeric supplier ID")
    parser.add_argument("--ticket-code", help="Sync a specific ticket (and its options); if omitted, sync all")
    parser.add_argument("--all-languages", action="store_true", help="Use full language list")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument("--force", action="store_true", help="Ignore state tracker and re-translate")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of tickets (only when syncing all)")
    args = parser.parse_args()

    required_key = required_api_key_env_var()
    if not os.getenv(required_key):
        print(f"❌ {required_key} is not set.")
        sys.exit(1)
    if not os.getenv("TRAVELC_USERNAME") or not os.getenv("TRAVELC_PASSWORD"):
        print("❌ TRAVELC_USERNAME / TRAVELC_PASSWORD are not set.")
        sys.exit(1)

    target_languages = get_target_languages(args.all_languages)
    print(f"🎯 Target languages: {target_languages}")
    if args.dry_run:
        print("🧪 DRY RUN MODE\n")
    if args.force:
        print("🔁 FORCE MODE\n")

    api = TravelCompositorAPI()
    translator = get_translator()
    store = StateStore()

    if args.ticket_code:
        # Sync one ticket + options
        main_result = sync_ticket(api, translator, store,
                                  args.supplier_id, args.ticket_code,
                                  target_languages, dry_run=args.dry_run, force=args.force)
        option_results = sync_all_options_for_ticket(api, translator, store,
                                                     args.supplier_id, args.ticket_code,
                                                     target_languages, dry_run=args.dry_run, force=args.force)
        if isinstance(main_result, dict):
            main_result["options"] = option_results
        results = [main_result] if isinstance(main_result, dict) else [main_result] + option_results
    else:
        results = sync_all_tickets(api, translator, store,
                                   args.supplier_id, target_languages,
                                   dry_run=args.dry_run, limit=args.limit, force=args.force)

    print("\n=== SUMMARY ===")
    by_status = {}
    def count_results(r):
        if isinstance(r, dict):
            status = r.get("status", "unknown")
            by_status.setdefault(status, []).append(r)
            if "options" in r and isinstance(r["options"], list):
                for opt in r["options"]:
                    count_results(opt)
        else:
            by_status.setdefault("unknown", []).append(r)
    for r in results:
        count_results(r)
    for status, items in by_status.items():
        print(f"  {status}: {len(items)}")
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
