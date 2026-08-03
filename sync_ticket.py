"""
sync_ticket.py — Sync tickets (main + options) with batching, verification, and retries.
Batch size reduced to 3 for long descriptions.
"""

import json
import time
from typing import Dict, Any, List, Optional

from state_store import StateStore, compute_hash
from translator import get_translator

# ---- Configuration ----
BATCH_SIZE = 5          # Languages per translation API call (reduced from 5)
MAX_ATTEMPTS = 5        # Max retry attempts per ticket/option
DELAY_BETWEEN_BATCHES = 15   # seconds
DELAY_BETWEEN_ATTEMPTS = 5.0  # seconds

# ---- Main ticket fields ----
TEXT_FIELDS = ("name", "description", "meetingPoint", "activityType",
               "voucherRemarks", "departureTime")
LIST_FIELDS = ("includes", "excludes")


def filter_successful_translations(
    translations: Dict[str, Dict[str, str]],
    source_fields: Dict[str, str]
) -> Dict[str, Dict[str, str]]:
    successful = {}
    for lang, trans in translations.items():
        changed = False
        for field, src in source_fields.items():
            if trans.get(field) != src:
                changed = True
                break
        if changed:
            successful[lang] = trans
        else:
            print(f"⚠️  Translation for {lang} is identical to source; skipping.")
    return successful


def extract_translatable_fields_from_ticket(ticket_entry: Dict[str, Any]) -> Dict[str, str]:
    datasheets = ticket_entry.get("datasheets")
    if not datasheets:
        return {}

    en_entry = None
    for key in ("EN", "EN_US"):
        if key in datasheets:
            en_entry = datasheets[key]
            break
    if en_entry is None:
        for key in datasheets:
            if key.upper().startswith("EN"):
                en_entry = datasheets[key]
                break
    if not en_entry:
        return {}

    fields = {}
    for f in TEXT_FIELDS:
        val = en_entry.get(f)
        if isinstance(val, str) and val.strip():
            fields[f] = val
    for f in LIST_FIELDS:
        val = en_entry.get(f)
        if isinstance(val, list) and val:
            fields[f] = "\n".join(val)
    return fields


def build_updated_datasheets(original_datasheets: Dict[str, Any],
                             translations_by_lang: Dict[str, Dict[str, str]],
                             en_entry: Dict[str, Any]) -> Dict[str, Any]:
    new_datasheets = dict(original_datasheets)
    for lang, trans in translations_by_lang.items():
        base = dict(en_entry)
        for f, text in trans.items():
            if f in LIST_FIELDS:
                base[f] = [line for line in text.split("\n") if line.strip()] if text.strip() else []
            else:
                base[f] = text
        if lang in original_datasheets:
            for k, v in original_datasheets[lang].items():
                if k not in base:
                    base[k] = v
        new_datasheets[lang] = base
    return new_datasheets


def get_existing_content_for_language(ticket: Dict[str, Any], lang: str) -> Dict[str, str]:
    datasheets = ticket.get("datasheets", {})
    lang_entry = datasheets.get(lang, {})
    if not lang_entry:
        return {}
    fields = {}
    for f in TEXT_FIELDS:
        val = lang_entry.get(f)
        if isinstance(val, str) and val.strip():
            fields[f] = val
    for f in LIST_FIELDS:
        val = lang_entry.get(f)
        if isinstance(val, list) and val:
            fields[f] = "\n".join(val)
    return fields


def languages_needed_with_verification(
    store: StateStore,
    entity_type: str,
    supplier_id: str,
    entity_id: str,
    source_hash: str,
    target_languages: List[str],
    current_ticket: Dict[str, Any],
    source_fields: Dict[str, str],
    option_code: str = "",
) -> List[str]:
    state = store.get_state(entity_type, supplier_id, entity_id, option_code)
    if state is None or state["source_hash"] != source_hash:
        return list(target_languages)

    already_done = set(state["translated_languages"])
    needed = []

    for lang in target_languages:
        if lang not in already_done:
            needed.append(lang)
            continue

        existing = get_existing_content_for_language(current_ticket, lang)
        if not existing:
            needed.append(lang)
            continue

        is_identical = True
        for field, src_text in source_fields.items():
            if existing.get(field) != src_text:
                is_identical = False
                break
        if is_identical:
            print(f"🔍 Verification: {lang} content is identical to source – re-translating.")
            needed.append(lang)
    return needed


def sync_ticket(api, translator, store: StateStore,
                supplier_id: str, ticket_code: str,
                target_languages: List[str],
                dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    ticket = api.get_ticket(supplier_id, ticket_code)
    if isinstance(ticket, dict) and "error" in ticket:
        return {"status": "fetch_failed", "ticket_code": ticket_code, "detail": ticket}

    datasheets = ticket.get("datasheets")
    if not datasheets:
        return {"status": "skipped", "ticket_code": ticket_code, "reason": "no datasheets"}

    translatable = extract_translatable_fields_from_ticket(ticket)
    if not translatable:
        return {"status": "skipped", "ticket_code": ticket_code, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    en_entry = datasheets.get("EN") or datasheets.get("EN_US") or {}

    # Retry loop
    attempt = 0
    all_translations = {}   # accumulate successful translations across attempts
    last_missing = list(target_languages)  # start with all languages
    
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        if force:
            needed = list(target_languages)
        else:
            needed = languages_needed_with_verification(
                store, "ticket", supplier_id, ticket_code, source_hash,
                target_languages, ticket, translatable
            )
        if not needed:
            break

        print(f"🔄 Attempt {attempt}/{MAX_ATTEMPTS}: translating {len(needed)} languages for ticket {ticket_code}")
        print(f"   Missing: {needed}")

        combined = {}
        # Translate in batches
for i in range(0, len(needed), BATCH_SIZE):
    batch = needed[i:i+BATCH_SIZE]
    print(f"🌐 Translating ... (batch {i//BATCH_SIZE + 1})")
    batch_result = translator.translate_fields(translatable, batch)
    combined_translations.update(batch_result)
    # Wait between batches, but not after the last one
    if i + BATCH_SIZE < len(needed):
        print(f"⏳ Waiting {DELAY_BETWEEN_BATCHES}s to respect rate limits...")
        time.sleep(DELAY_BETWEEN_BATCHES)
          
        # Filter only successful translations (changed from source)
        successful = filter_successful_translations(combined, translatable)
        print(f"   Successful translations in this attempt: {list(successful.keys())}")

        if not successful:
            print(f"⚠️  No successful translations in attempt {attempt}")
            if attempt < MAX_ATTEMPTS:
                print(f"   Waiting {DELAY_BETWEEN_ATTEMPTS}s before retry...")
                time.sleep(DELAY_BETWEEN_ATTEMPTS)
            continue

        all_translations.update(successful)

        # Build and write the updated datasheets
        if not dry_run:
            new_datasheets = build_updated_datasheets(datasheets, successful, en_entry)
            payload = dict(ticket)
            payload["datasheets"] = new_datasheets
            result = api.update_ticket(supplier_id, payload)
            if isinstance(result, dict) and "error" in result:
                print(f"❌ PUT failed for ticket {ticket_code}: {result}")
                break  # Stop retrying if write fails

            # Update state with the newly written languages
            written_langs = list(successful.keys())
            if written_langs:
                prior_state = store.get_state("ticket", supplier_id, ticket_code)
                prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
                all_langs = sorted(set(prior_langs) | set(written_langs))
                store.upsert_state("ticket", supplier_id, ticket_code, source_hash, all_langs)

            # Re‑fetch the ticket to get the latest content for verification
            ticket = api.get_ticket(supplier_id, ticket_code)
            if isinstance(ticket, dict) and "error" in ticket:
                print(f"⚠️  Could not re‑fetch ticket {ticket_code} for verification")
                break
        else:
            # In dry-run, we only do one attempt (no need to loop)
            break

    # Final result
    if dry_run:
        return {
            "status": "dry_run_preview",
            "ticket_code": ticket_code,
            "languages": list(all_translations.keys()) if all_translations else [],
            "preview": {lang: {k: v for k, v in trans.items() if k in TEXT_FIELDS or k in LIST_FIELDS}
                        for lang, trans in all_translations.items()}
        }

    # After all attempts, check what's still missing
    final_needed = languages_needed_with_verification(
        store, "ticket", supplier_id, ticket_code, source_hash,
        target_languages, ticket, translatable
    )
    if final_needed:
        return {
            "status": "partial",
            "ticket_code": ticket_code,
            "missing_languages": final_needed,
            "languages_written": list(all_translations.keys())
        }
    else:
        return {
            "status": "updated",
            "ticket_code": ticket_code,
            "languages_written": list(all_translations.keys())
        }


# ---- Option functions (with retries and smaller batch) ----
def extract_translatable_fields_from_option(option_entry: Dict[str, Any]) -> Dict[str, str]:
    fields = {}
    remarks = option_entry.get("remarks", {})
    en_remarks = remarks.get("EN") or remarks.get("EN_US") or {}
    if isinstance(en_remarks, dict):
        if en_remarks.get("name"):
            fields["remarks_name"] = en_remarks["name"]
        if en_remarks.get("remarks"):
            fields["remarks_remarks"] = en_remarks["remarks"]

    supplements = option_entry.get("supplements", [])
    for idx, supp in enumerate(supplements):
        trans = supp.get("translations", {})
        en_supp = trans.get("EN") or trans.get("EN_US") or {}
        if isinstance(en_supp, dict) and en_supp.get("name"):
            fields[f"supplement_{idx}_name"] = en_supp["name"]
    return fields


def get_existing_option_content(option_entry: Dict[str, Any], lang: str) -> Dict[str, str]:
    fields = {}
    remarks = option_entry.get("remarks", {})
    lang_remarks = remarks.get(lang, {})
    if isinstance(lang_remarks, dict):
        if lang_remarks.get("name"):
            fields["remarks_name"] = lang_remarks["name"]
        if lang_remarks.get("remarks"):
            fields["remarks_remarks"] = lang_remarks["remarks"]

    supplements = option_entry.get("supplements", [])
    for idx, supp in enumerate(supplements):
        trans = supp.get("translations", {})
        lang_supp = trans.get(lang, {})
        if isinstance(lang_supp, dict) and lang_supp.get("name"):
            fields[f"supplement_{idx}_name"] = lang_supp["name"]
    return fields


def build_updated_option(original_option: Dict[str, Any],
                         translations_by_lang: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    new_option = dict(original_option)

    remarks = dict(original_option.get("remarks", {}))
    for lang, trans in translations_by_lang.items():
        lang_remarks = remarks.get(lang, {})
        if not isinstance(lang_remarks, dict):
            lang_remarks = {}
        if "remarks_name" in trans:
            lang_remarks["name"] = trans["remarks_name"]
        if "remarks_remarks" in trans:
            lang_remarks["remarks"] = trans["remarks_remarks"]
        remarks[lang] = lang_remarks
    new_option["remarks"] = remarks

    supplements = list(original_option.get("supplements", []))
    for idx, supp in enumerate(supplements):
        supp_trans = dict(supp.get("translations", {}))
        for lang, trans in translations_by_lang.items():
            key = f"supplement_{idx}_name"
            if key in trans:
                lang_supp = supp_trans.get(lang, {})
                if not isinstance(lang_supp, dict):
                    lang_supp = {}
                lang_supp["name"] = trans[key]
                supp_trans[lang] = lang_supp
        new_supp = dict(supp)
        new_supp["translations"] = supp_trans
        supplements[idx] = new_supp
    new_option["supplements"] = supplements

    return new_option


def sync_ticket_option(api, translator, store: StateStore,
                       supplier_id: str, ticket_code: str, option_code: str,
                       target_languages: List[str],
                       dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    option = api.get_ticket_option(supplier_id, ticket_code, option_code)
    if isinstance(option, dict) and "error" in option:
        return {"status": "fetch_failed", "option_code": option_code, "detail": option}

    translatable = extract_translatable_fields_from_option(option)
    if not translatable:
        return {"status": "skipped", "option_code": option_code, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    entity_id = f"{ticket_code}|{option_code}"

    attempt = 0
    all_translations = {}
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        if force:
            needed = list(target_languages)
        else:
            needed = languages_needed_with_verification(
                store, "ticket_option", supplier_id, entity_id, source_hash,
                target_languages, option, translatable, option_code=option_code
            )
        if not needed:
            break

        print(f"🔄 Attempt {attempt}/{MAX_ATTEMPTS}: translating {len(needed)} languages for option {option_code}")
        print(f"   Missing: {needed}")
        combined = {}
        for i in range(0, len(needed), BATCH_SIZE):
            batch = needed[i:i+BATCH_SIZE]
            print(f"🌐 Translating option {option_code} (batch {i//BATCH_SIZE + 1}): {batch}")
            batch_result = translator.translate_fields(translatable, batch)
            combined.update(batch_result)
            time.sleep(DELAY_BETWEEN_BATCHES)

        successful = filter_successful_translations(combined, translatable)
        print(f"   Successful translations in this attempt: {list(successful.keys())}")
        if not successful:
            print(f"⚠️  No successful translations in attempt {attempt}")
            if attempt < MAX_ATTEMPTS:
                print(f"   Waiting {DELAY_BETWEEN_ATTEMPTS}s before retry...")
                time.sleep(DELAY_BETWEEN_ATTEMPTS)
            continue

        all_translations.update(successful)
        updated_option = build_updated_option(option, successful)

        if not dry_run:
            result = api.update_ticket_option(supplier_id, ticket_code, updated_option)
            if isinstance(result, dict) and "error" in result:
                print(f"❌ PUT failed for option {option_code}: {result}")
                break

            written_langs = list(successful.keys())
            if written_langs:
                prior_state = store.get_state("ticket_option", supplier_id, entity_id, option_code=option_code)
                prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
                all_langs = sorted(set(prior_langs) | set(written_langs))
                store.upsert_state("ticket_option", supplier_id, entity_id, source_hash, all_langs, option_code=option_code)

            # Re‑fetch option for verification
            option = api.get_ticket_option(supplier_id, ticket_code, option_code)
            if isinstance(option, dict) and "error" in option:
                print(f"⚠️  Could not re‑fetch option {option_code} for verification")
                break
        else:
            break

    if dry_run:
        return {
            "status": "dry_run_preview",
            "option_code": option_code,
            "languages": list(all_translations.keys()) if all_translations else [],
            "preview": {lang: {k: v for k, v in trans.items() if k.startswith(("remarks_", "supplement_"))}
                        for lang, trans in all_translations.items()}
        }

    final_needed = languages_needed_with_verification(
        store, "ticket_option", supplier_id, entity_id, source_hash,
        target_languages, option, translatable, option_code=option_code
    )
    if final_needed:
        return {
            "status": "partial",
            "option_code": option_code,
            "missing_languages": final_needed,
            "languages_written": list(all_translations.keys())
        }
    else:
        return {
            "status": "updated",
            "option_code": option_code,
            "languages_written": list(all_translations.keys())
        }


def sync_all_options_for_ticket(api, translator, store: StateStore,
                                supplier_id: str, ticket_code: str,
                                target_languages: List[str],
                                dry_run: bool = True, force: bool = False) -> List[Dict[str, Any]]:
    ticket = api.get_ticket(supplier_id, ticket_code)
    if isinstance(ticket, dict) and "error" in ticket:
        return [{"status": "fetch_ticket_failed", "ticket_code": ticket_code, "detail": ticket}]

    modality_codes = ticket.get("modalityCodes", [])
    if not modality_codes:
        return [{"status": "skipped", "ticket_code": ticket_code, "reason": "no options"}]

    results = []
    for opt_code in modality_codes:
        results.append(sync_ticket_option(api, translator, store, supplier_id, ticket_code, opt_code,
                                          target_languages, dry_run=dry_run, force=force))
    return results


def fetch_all_tickets(api, supplier_id: str, limit: int = None, page_size: int = 100) -> List[Dict[str, Any]]:
    all_tickets = []
    first = 0
    while True:
        data = api.get_tickets(supplier_id, first=first, limit=page_size)
        if isinstance(data, dict) and "error" in data:
            break
        tickets = data.get("tickets", []) if isinstance(data, dict) else []
        if not tickets:
            break
        all_tickets.extend(tickets)
        if limit and len(all_tickets) >= limit:
            all_tickets = all_tickets[:limit]
            break
        pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
        total = pagination.get("totalResults", len(all_tickets))
        first += page_size
        if len(all_tickets) >= total:
            break
    return all_tickets
