"""
sync_ticket.py — Sync tickets (main + options) with translation.
Now filters out languages where the translation is identical to the source.
"""

import json
from typing import Dict, Any, List, Optional

from state_store import StateStore, compute_hash
from translator import get_translator

# ---- Main ticket fields ----
TEXT_FIELDS = ("name", "description", "meetingPoint", "activityType",
               "voucherRemarks", "departureTime")
LIST_FIELDS = ("includes", "excludes")


def filter_successful_translations(
    translations: Dict[str, Dict[str, str]],
    source_fields: Dict[str, str]
) -> Dict[str, Dict[str, str]]:
    """
    Return only languages where at least one field's translation differs from
    the source. Logs warnings for skipped languages.
    """
    successful = {}
    for lang, trans in translations.items():
        # Check if any field has changed
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
    """Extract translatable fields from the EN datasheet of a ticket."""
    datasheets = ticket_entry.get("datasheets")
    if not datasheets:
        return {}

    # Find EN entry
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
    """Merge translations back into datasheets."""
    new_datasheets = dict(original_datasheets)
    for lang, trans in translations_by_lang.items():
        base = dict(en_entry)  # start from EN to have all fields
        for f, text in trans.items():
            if f in LIST_FIELDS:
                base[f] = [line for line in text.split("\n") if line.strip()] if text.strip() else []
            else:
                base[f] = text
        # Preserve any fields that existed in original language entry
        if lang in original_datasheets:
            for k, v in original_datasheets[lang].items():
                if k not in base:
                    base[k] = v
        new_datasheets[lang] = base
    return new_datasheets


def sync_ticket(api, translator, store: StateStore,
                supplier_id: str, ticket_code: str,
                target_languages: List[str],
                dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    """Sync one main ticket (without options)."""
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
    if force:
        needed = list(target_languages)
    else:
        needed = store.languages_needed("ticket", supplier_id, ticket_code, source_hash, target_languages)
    if not needed:
        return {"status": "up_to_date", "ticket_code": ticket_code}

    print(f"🌐 Translating ticket {ticket_code}: fields={list(translatable.keys())} -> {needed}")
    all_translations = translator.translate_fields(translatable, needed)

    # Filter out languages that didn't actually change
    translations = filter_successful_translations(all_translations, translatable)
    if not translations:
        return {"status": "skipped", "ticket_code": ticket_code,
                "reason": "no successful translations (all identical to source)"}

    en_entry = datasheets.get("EN") or datasheets.get("EN_US") or {}
    new_datasheets = build_updated_datasheets(datasheets, translations, en_entry)

    if dry_run:
        preview = {lang: {k: v for k, v in trans.items() if k in TEXT_FIELDS or k in LIST_FIELDS}
                   for lang, trans in translations.items()}
        return {"status": "dry_run_preview", "ticket_code": ticket_code,
                "languages": list(translations.keys()), "preview": preview}

    payload = dict(ticket)
    payload["datasheets"] = new_datasheets
    result = api.update_ticket(supplier_id, payload)
    if isinstance(result, dict) and "error" in result:
        return {"status": "put_failed", "ticket_code": ticket_code, "detail": result}

    # Update state only for languages that were actually written
    written_langs = list(translations.keys())
    prior_state = store.get_state("ticket", supplier_id, ticket_code)
    prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
    all_langs = sorted(set(prior_langs) | set(written_langs))
    store.upsert_state("ticket", supplier_id, ticket_code, source_hash, all_langs)

    return {"status": "updated", "ticket_code": ticket_code,
            "languages_written": written_langs}


# ---- Ticket option (modality) ----
def extract_translatable_fields_from_option(option_entry: Dict[str, Any]) -> Dict[str, str]:
    """Extract fields from EN remarks and supplements."""
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
    """Merge translations into remarks and supplements."""
    new_option = dict(original_option)

    # Remarks
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

    # Supplements
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
    """Sync one ticket option."""
    option = api.get_ticket_option(supplier_id, ticket_code, option_code)
    if isinstance(option, dict) and "error" in option:
        return {"status": "fetch_failed", "option_code": option_code, "detail": option}

    translatable = extract_translatable_fields_from_option(option)
    if not translatable:
        return {"status": "skipped", "option_code": option_code, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    entity_id = f"{ticket_code}|{option_code}"
    if force:
        needed = list(target_languages)
    else:
        needed = store.languages_needed("ticket_option", supplier_id, entity_id,
                                        source_hash, target_languages, option_code=option_code)
    if not needed:
        return {"status": "up_to_date", "option_code": option_code}

    print(f"🌐 Translating option {option_code} of {ticket_code}: fields={list(translatable.keys())} -> {needed}")
    all_translations = translator.translate_fields(translatable, needed)

    # Filter out languages that didn't actually change
    translations = filter_successful_translations(all_translations, translatable)
    if not translations:
        return {"status": "skipped", "option_code": option_code,
                "reason": "no successful translations (all identical to source)"}

    updated_option = build_updated_option(option, translations)

    if dry_run:
        preview = {lang: {k: v for k, v in trans.items() if k.startswith(("remarks_", "supplement_"))}
                   for lang, trans in translations.items()}
        return {"status": "dry_run_preview", "option_code": option_code,
                "languages": list(translations.keys()), "preview": preview}

    result = api.update_ticket_option(supplier_id, ticket_code, updated_option)
    if isinstance(result, dict) and "error" in result:
        return {"status": "put_failed", "option_code": option_code, "detail": result}

    # Update state only for languages that were actually written
    written_langs = list(translations.keys())
    prior_state = store.get_state("ticket_option", supplier_id, entity_id, option_code=option_code)
    prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
    all_langs = sorted(set(prior_langs) | set(written_langs))
    store.upsert_state("ticket_option", supplier_id, entity_id, source_hash, all_langs, option_code=option_code)

    return {"status": "updated", "option_code": option_code,
            "languages_written": written_langs}


def sync_all_options_for_ticket(api, translator, store: StateStore,
                                supplier_id: str, ticket_code: str,
                                target_languages: List[str],
                                dry_run: bool = True, force: bool = False) -> List[Dict[str, Any]]:
    """Fetch ticket to get modalityCodes and sync each option."""
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
    """Page through all tickets for a supplier."""
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
