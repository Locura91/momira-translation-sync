"""
sync_ticket.py — Optimized with concurrent option fetching.
"""

import json
import re
import time
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from state_store import StateStore, compute_hash
from translator import get_translator, translate_in_batches

# ---- Configuration ----
# CONFIRMED via a real live run (ticket USM-T3, full 30-language target list):
# with BATCH_SIZE=10 and 8 translatable fields per language (name, description,
# meetingPoint, activityType, voucherRemarks, departureTime, includes, excludes
# — and HTML no longer stripped before translation, see strip_html_and_compress's
# docstring), 30 languages / 10 per batch = 3 batches. Only the LAST batch (the
# 10 languages TH/EL/FI/JA/SR/PT/DA/IT/MS/SQ) came back in "languages_written" —
# the first two batches (FR..NL and ES..CS, 20 languages total) silently
# produced nothing: Claude was hitting its request timeout generating that much
# content per call, exactly like the earlier confirmed Closed Tour timeout bug,
# and those failed batches just got dropped instead of erroring loudly. Reduced
# to 1 language per batch so each call has to generate far less content and
# reliably finishes inside the timeout. Batches still run CONCURRENTLY (see
# translate_in_batches in translator.py, default max_workers=4), so this costs
# more API calls (fine — correctness matters more than a few extra cents here)
# but not proportionally more wall-clock time.
BATCH_SIZE = 1
DELAY_BETWEEN_BATCHES = 2
MAX_OPTION_WORKERS = 5   # number of parallel option fetches

# ---- Main ticket fields ----
TEXT_FIELDS = ("name", "description", "meetingPoint", "activityType",
               "voucherRemarks", "departureTime")
LIST_FIELDS = ("includes", "excludes")


def strip_html_and_compress(text: str) -> str:
    """
    NO-OP passthrough now. This used to strip every HTML tag out of a
    field before sending it to the translator, which silently destroyed
    any real formatting the source field had (bullet lists, bold, etc.).
    translator.py's SYSTEM_PROMPT already explicitly instructs the model
    to "preserve HTML tags ... EXACTLY as they appear, untouched, in the
    same position" — but that instruction is meaningless if the tags are
    stripped out before the model ever sees them. Confirmed as the cause
    of translated Closed Tour fields losing all formatting (came back as
    flat <p> text instead of the original's <ul><li>/<b> structure); this
    file shares the exact same bug for any HTML-bearing field, so it's
    fixed the same way here.
    """
    return text


def compress_translatable_fields(fields: Dict[str, str]) -> Dict[str, str]:
    compressed = {}
    for key, value in fields.items():
        if isinstance(value, str):
            compressed[key] = strip_html_and_compress(value)
        else:
            compressed[key] = value
    return compressed


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


def verify_and_filter_needed(
    store: StateStore,
    entity_type: str,
    supplier_id: str,
    entity_id: str,
    source_hash: str,
    target_languages: List[str],
    current_ticket: Dict[str, Any],
    source_fields: Dict[str, str],
    option_code: str = "",
    existing_content_fn=None,
) -> List[str]:
    """
    existing_content_fn: which function to use to read back "does this
    language already have real translated content". Defaults to
    get_existing_content_for_language (checks ticket["datasheets"][lang]) —
    correct for the MAIN ticket, but a ticket OPTION/modality has no
    "datasheets" key at all (confirmed via a real live GET for modality
    "Standard English" on ticket USM-T3 — its translatable content lives in
    "remarks"/"supplements", not "datasheets"). Before this fix, options
    were always calling the ticket-shaped checker, which found
    entry.get("datasheets") empty and always returned {} — meaning this
    verification step could never actually recognize genuinely-already-
    translated option content. The dedicated get_existing_option_content
    function already existed in this file for exactly this purpose but was
    never wired in anywhere; sync_ticket_option_from_data now passes it in
    explicitly.
    """
    if existing_content_fn is None:
        existing_content_fn = get_existing_content_for_language

    state = store.get_state(entity_type, supplier_id, entity_id, option_code)
    if state is None or state["source_hash"] != source_hash:
        needed = list(target_languages)
    else:
        already_done = set(state["translated_languages"])
        needed = [lang for lang in target_languages if lang not in already_done]

    truly_needed = []
    languages_to_add_to_state = []

    for lang in needed:
        existing = existing_content_fn(current_ticket, lang)
        if not existing:
            truly_needed.append(lang)
            continue

        is_identical = True
        for field, src_text in source_fields.items():
            if existing.get(field) != src_text:
                is_identical = False
                break

        if is_identical:
            truly_needed.append(lang)
        else:
            languages_to_add_to_state.append(lang)

    if languages_to_add_to_state:
        prior_state = store.get_state(entity_type, supplier_id, entity_id, option_code)
        prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
        all_langs = sorted(set(prior_langs) | set(languages_to_add_to_state))
        store.upsert_state(entity_type, supplier_id, entity_id, source_hash, all_langs, option_code=option_code)

    return truly_needed


# ----- Main ticket sync from pre-fetched data -----
def sync_ticket_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    ticket_entry: Dict[str, Any],
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    start_time = time.time()
    ticket_code = ticket_entry.get("code")
    if not ticket_code:
        return {"status": "skipped", "reason": "no code field"}

    datasheets = ticket_entry.get("datasheets")
    if not datasheets:
        return {"status": "skipped", "ticket_code": ticket_code, "reason": "no datasheets"}

    translatable = extract_translatable_fields_from_ticket(ticket_entry)
    if not translatable:
        return {"status": "skipped", "ticket_code": ticket_code, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    t0 = time.time()
    if force:
        needed = list(target_languages)
    else:
        needed = verify_and_filter_needed(
            store, "ticket", supplier_id, ticket_code, source_hash,
            target_languages, ticket_entry, translatable
        )
    verify_time = time.time() - t0

    if not needed:
        return {"status": "up_to_date", "ticket_code": ticket_code}

    compressed_translatable = compress_translatable_fields(translatable)

    total_batches = (len(needed) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"   translating {len(needed)} language(s) in {total_batches} batch(es), running concurrently")
    translation_start = time.time()
    combined_translations, failed_languages = translate_in_batches(translator, compressed_translatable, needed, batch_size=BATCH_SIZE)
    translation_time = time.time() - translation_start

    # NOTE: this used to filter out any language whose translated text was
    # identical to the source, on the assumption that meant the call had
    # silently failed. That's wrong for short/common words that legitimately
    # translate to themselves (or a near-identical spelling) in several
    # languages — it would silently and permanently drop a correct result.
    # Now we only exclude languages translate_in_batches itself reports as
    # having failed (see its docstring); everything else is trusted as real.
    successful = {}
    for lang, trans in combined_translations.items():
        if lang in failed_languages:
            print(f"⚠️  Translation batch for {lang} failed; skipping.")
        else:
            successful[lang] = trans

    if not successful:
        return {"status": "skipped", "ticket_code": ticket_code, "reason": "no successful translations"}

    en_entry = datasheets.get("EN") or datasheets.get("EN_US") or {}
    new_datasheets = build_updated_datasheets(datasheets, successful, en_entry)

    if dry_run:
        preview = {lang: {k: v for k, v in trans.items() if k in TEXT_FIELDS or k in LIST_FIELDS}
                   for lang, trans in successful.items()}
        return {"status": "dry_run_preview", "ticket_code": ticket_code,
                "languages": list(successful.keys()), "preview": preview}

    write_start = time.time()
    payload = dict(ticket_entry)
    payload["datasheets"] = new_datasheets
    result = api.update_ticket(supplier_id, payload)
    write_time = time.time() - write_start
    if isinstance(result, dict) and "error" in result:
        return {"status": "put_failed", "ticket_code": ticket_code, "detail": result}

    written_langs = list(successful.keys())
    prior_state = store.get_state("ticket", supplier_id, ticket_code)
    prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
    all_langs = sorted(set(prior_langs) | set(written_langs))
    store.upsert_state("ticket", supplier_id, ticket_code, source_hash, all_langs)

    total_time = time.time() - start_time
    print(f"✅ Ticket {ticket_code} done in {total_time:.1f}s "
          f"(verify: {verify_time:.1f}s, translate: {translation_time:.1f}s, write: {write_time:.1f}s)")
    return {"status": "updated", "ticket_code": ticket_code, "languages_written": written_langs}


# Original sync_ticket wrapper (for single ticket mode)
def sync_ticket(api, translator, store: StateStore,
                supplier_id: str, ticket_code: str,
                target_languages: List[str],
                dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    ticket = api.get_ticket(supplier_id, ticket_code)
    if isinstance(ticket, dict) and "error" in ticket:
        return {"status": "fetch_failed", "ticket_code": ticket_code, "detail": ticket}
    return sync_ticket_from_data(api, translator, store, supplier_id, ticket,
                                 target_languages, dry_run=dry_run, force=force)


# ---- Option functions (with concurrent fetching) ----
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


def sync_ticket_option_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    option_entry: Dict[str, Any],
    ticket_code: str,
    option_code: str,
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    start_time = time.time()
    translatable = extract_translatable_fields_from_option(option_entry)
    if not translatable:
        return {"status": "skipped", "option_code": option_code, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    entity_id = f"{ticket_code}|{option_code}"
    if force:
        needed = list(target_languages)
    else:
        needed = verify_and_filter_needed(
            store, "ticket_option", supplier_id, entity_id, source_hash,
            target_languages, option_entry, translatable, option_code=option_code,
            existing_content_fn=get_existing_option_content,
        )

    if not needed:
        return {"status": "up_to_date", "option_code": option_code}

    compressed_translatable = compress_translatable_fields(translatable)
    combined_translations, failed_languages = translate_in_batches(translator, compressed_translatable, needed, batch_size=BATCH_SIZE)

    # See the comment on the main-ticket translate call above: a modality
    # name like "Standard" can legitimately translate to "Standard" in
    # several languages (real, correct result) — only drop languages that
    # translate_in_batches reports as actually having failed.
    successful = {}
    for lang, trans in combined_translations.items():
        if lang in failed_languages:
            print(f"⚠️  Translation batch for {lang} failed; skipping.")
        else:
            successful[lang] = trans

    if not successful:
        return {"status": "skipped", "option_code": option_code, "reason": "no successful translations"}

    updated_option = build_updated_option(option_entry, successful)

    if dry_run:
        preview = {lang: {k: v for k, v in trans.items() if k.startswith(("remarks_", "supplement_"))}
                   for lang, trans in successful.items()}
        return {"status": "dry_run_preview", "option_code": option_code,
                "languages": list(successful.keys()), "preview": preview}

    result = api.update_ticket_option(supplier_id, ticket_code, updated_option)
    if isinstance(result, dict) and "error" in result:
        return {"status": "put_failed", "option_code": option_code, "detail": result}

    written_langs = list(successful.keys())
    prior_state = store.get_state("ticket_option", supplier_id, entity_id, option_code=option_code)
    prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
    all_langs = sorted(set(prior_langs) | set(written_langs))
    store.upsert_state("ticket_option", supplier_id, entity_id, source_hash, all_langs, option_code=option_code)

    elapsed = time.time() - start_time
    print(f"✅ Option {option_code} done in {elapsed:.1f}s")
    return {"status": "updated", "option_code": option_code, "languages_written": written_langs}


def sync_all_options_for_ticket_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    ticket_entry: Dict[str, Any],
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> List[Dict[str, Any]]:
    modality_codes = ticket_entry.get("modalityCodes", [])
    if not modality_codes:
        return [{"status": "skipped", "ticket_code": ticket_entry.get("code"), "reason": "no options"}]

    ticket_code = ticket_entry.get("code")
    # ---- Fetch options concurrently ----
    option_entries = {}
    with ThreadPoolExecutor(max_workers=MAX_OPTION_WORKERS) as executor:
        future_to_code = {
            executor.submit(api.get_ticket_option, supplier_id, ticket_code, opt_code): opt_code
            for opt_code in modality_codes
        }
        for future in as_completed(future_to_code):
            opt_code = future_to_code[future]
            try:
                option = future.result()
                if isinstance(option, dict) and "error" in option:
                    option_entries[opt_code] = {"error": option}
                else:
                    option_entries[opt_code] = option
            except Exception as e:
                option_entries[opt_code] = {"error": str(e)}

    # ---- Process options sequentially (translation/writing) ----
    results = []
    for opt_code in modality_codes:
        option = option_entries.get(opt_code)
        if option is None:
            results.append({"status": "fetch_failed", "option_code": opt_code, "detail": "No response"})
            continue
        if isinstance(option, dict) and "error" in option:
            results.append({"status": "fetch_failed", "option_code": opt_code, "detail": option["error"]})
            continue
        result = sync_ticket_option_from_data(
            api, translator, store, supplier_id, option, ticket_code, opt_code,
            target_languages, dry_run=dry_run, force=force
        )
        results.append(result)
    return results


# Legacy wrapper (uses extra GET) – kept for backward compatibility
def sync_all_options_for_ticket(api, translator, store: StateStore,
                                supplier_id: str, ticket_code: str,
                                target_languages: List[str],
                                dry_run: bool = True, force: bool = False) -> List[Dict[str, Any]]:
    ticket = api.get_ticket(supplier_id, ticket_code)
    if isinstance(ticket, dict) and "error" in ticket:
        return [{"status": "fetch_ticket_failed", "ticket_code": ticket_code, "detail": ticket}]
    return sync_all_options_for_ticket_from_data(
        api, translator, store, supplier_id, ticket, target_languages,
        dry_run=dry_run, force=force
    )


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
