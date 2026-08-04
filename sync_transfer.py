"""
sync_transfer.py — Sync transfers (datasheets) with translations.
"""

import json
import re
import time
from typing import Dict, Any, List, Optional

from state_store import StateStore, compute_hash
from translator import get_translator, translate_in_batches

# ---- Configuration ----
BATCH_SIZE = 10
DELAY_BETWEEN_BATCHES = 2

# ---- Translatable fields inside datasheets ----
TEXT_FIELDS = ("name", "description", "pickupInformation")


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


def extract_translatable_fields_from_transfer(transfer_entry: Dict[str, Any]) -> Dict[str, str]:
    """
    Extract translatable fields from the EN datasheet of a transfer.
    Expects transfer_entry to have a 'datasheets' dict mapping language -> descriptor.
    """
    datasheets = transfer_entry.get("datasheets")
    if not datasheets:
        return {}

    # Find EN entry (prefer EN, then EN_US)
    en_entry = None
    for key in ("EN", "EN_US"):
        if key in datasheets:
            en_entry = datasheets[key]
            break
    if en_entry is None:
        # Fallback: first key starting with "EN"
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
    return fields


def get_existing_content_for_language(transfer_entry: Dict[str, Any], lang: str) -> Dict[str, str]:
    datasheets = transfer_entry.get("datasheets", {})
    lang_entry = datasheets.get(lang, {})
    if not lang_entry:
        return {}
    fields = {}
    for f in TEXT_FIELDS:
        val = lang_entry.get(f)
        if isinstance(val, str) and val.strip():
            fields[f] = val
    return fields


def verify_and_filter_needed(
    store: StateStore,
    entity_type: str,
    supplier_id: str,
    entity_id: str,
    source_hash: str,
    target_languages: List[str],
    current_transfer: Dict[str, Any],
    source_fields: Dict[str, str],
) -> List[str]:
    """
    Check state and verify existing content. Returns languages that need translation.
    """
    state = store.get_state(entity_type, supplier_id, entity_id)
    if state is None or state["source_hash"] != source_hash:
        needed = list(target_languages)
    else:
        already_done = set(state["translated_languages"])
        needed = [lang for lang in target_languages if lang not in already_done]

    truly_needed = []
    languages_to_add_to_state = []

    for lang in needed:
        existing = get_existing_content_for_language(current_transfer, lang)
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
        prior_state = store.get_state(entity_type, supplier_id, entity_id)
        prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
        all_langs = sorted(set(prior_langs) | set(languages_to_add_to_state))
        store.upsert_state(entity_type, supplier_id, entity_id, source_hash, all_langs)

    return truly_needed


def build_updated_datasheets(
    original_datasheets: Dict[str, Any],
    translations_by_lang: Dict[str, Dict[str, str]],
    en_entry: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Merge translations back into the datasheets map.
    """
    new_datasheets = dict(original_datasheets)
    for lang, trans in translations_by_lang.items():
        # Start with a copy of the EN entry to ensure all fields exist
        base = dict(en_entry)
        for f, text in trans.items():
            base[f] = text
        # Preserve any fields that were in the original language entry
        if lang in original_datasheets:
            for k, v in original_datasheets[lang].items():
                if k not in base:
                    base[k] = v
        new_datasheets[lang] = base
    return new_datasheets


def sync_transfer_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    transfer_entry: Dict[str, Any],
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Sync one transfer using an already fetched entry.
    """
    start_time = time.time()
    transfer_id = transfer_entry.get("id")
    if not transfer_id:
        return {"status": "skipped", "reason": "no 'id' field"}

    datasheets = transfer_entry.get("datasheets")
    if not datasheets:
        return {"status": "skipped", "transfer_id": transfer_id, "reason": "no datasheets"}

    translatable = extract_translatable_fields_from_transfer(transfer_entry)
    if not translatable:
        return {"status": "skipped", "transfer_id": transfer_id, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)

    t0 = time.time()
    if force:
        needed = list(target_languages)
    else:
        needed = verify_and_filter_needed(
            store, "transfer", supplier_id, transfer_id, source_hash,
            target_languages, transfer_entry, translatable
        )
    verify_time = time.time() - t0

    if not needed:
        return {"status": "up_to_date", "transfer_id": transfer_id}

    compressed_translatable = compress_translatable_fields(translatable)

    # Translate in batches, run concurrently instead of one-after-another
    translation_start = time.time()
    combined_translations, failed_languages = translate_in_batches(translator, compressed_translatable, needed, batch_size=BATCH_SIZE)
    translation_time = time.time() - translation_start

    # NOTE: this used to filter out any language whose translated text was
    # identical to the source, on the assumption that meant the call had
    # silently failed. That's wrong for short/common words that legitimately
    # translate to themselves (or a near-identical spelling) in several
    # languages (e.g. a pickup point name) — it would silently and
    # permanently drop a correct result. Now we only exclude languages
    # translate_in_batches itself reports as having failed (see its
    # docstring in translator.py); everything else is trusted as real.
    successful = {}
    for lang, trans in combined_translations.items():
        if lang in failed_languages:
            print(f"⚠️  Translation batch for {lang} failed; skipping.")
        else:
            successful[lang] = trans

    if not successful:
        return {"status": "skipped", "transfer_id": transfer_id, "reason": "no successful translations"}

    en_entry = datasheets.get("EN") or datasheets.get("EN_US") or {}
    new_datasheets = build_updated_datasheets(datasheets, successful, en_entry)

    if dry_run:
        preview = {lang: {k: v for k, v in trans.items() if k in TEXT_FIELDS}
                   for lang, trans in successful.items()}
        return {"status": "dry_run_preview", "transfer_id": transfer_id,
                "languages": list(successful.keys()), "preview": preview}

    # Write
    write_start = time.time()
    payload = dict(transfer_entry)
    payload["datasheets"] = new_datasheets
    result = api.update_transfer(supplier_id, payload)
    write_time = time.time() - write_start
    if isinstance(result, dict) and "error" in result:
        return {"status": "put_failed", "transfer_id": transfer_id, "detail": result}

    written_langs = list(successful.keys())
    prior_state = store.get_state("transfer", supplier_id, transfer_id)
    prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
    all_langs = sorted(set(prior_langs) | set(written_langs))
    store.upsert_state("transfer", supplier_id, transfer_id, source_hash, all_langs)

    total_time = time.time() - start_time
    print(f"✅ Transfer {transfer_id} done in {total_time:.1f}s "
          f"(verify: {verify_time:.1f}s, translate: {translation_time:.1f}s, write: {write_time:.1f}s)")
    return {"status": "updated", "transfer_id": transfer_id, "languages_written": written_langs}


def sync_transfer(api, translator, store: StateStore,
                  supplier_id: str, transfer_id: str,
                  target_languages: List[str],
                  dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    """
    Sync a single transfer by ID (fetches it first).
    """
    transfer = api.get_transfer(supplier_id, transfer_id)
    if isinstance(transfer, dict) and "error" in transfer:
        return {"status": "fetch_failed", "transfer_id": transfer_id, "detail": transfer}
    return sync_transfer_from_data(api, translator, store, supplier_id, transfer,
                                   target_languages, dry_run=dry_run, force=force)


def fetch_all_transfers(api, supplier_id: str, limit: int = None) -> List[Dict[str, Any]]:
    """
    Fetch all transfers for a supplier using GET /transfer/{supplierId}.
    """
    data = api.get_transfers(supplier_id)
    transfers = data.get("transfer", []) if isinstance(data, dict) else []
    if limit:
        transfers = transfers[:limit]
    return transfers


def sync_all_transfers_for_supplier(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    target_languages: List[str],
    dry_run: bool = True,
    limit: int = None,
    force: bool = False,
) -> List[Dict[str, Any]]:
    """
    Sync all transfers for a supplier.
    """
    transfers = fetch_all_transfers(api, supplier_id, limit=limit)
    print(f"📋 Found {len(transfers)} transfer(s) for supplier {supplier_id}.")

    results = []
    for t in transfers:
        # The transfer list entries already contain the full data (including datasheets)
        # so we can use sync_transfer_from_data directly.
        result = sync_transfer_from_data(
            api, translator, store, supplier_id, t, target_languages,
            dry_run=dry_run, force=force
        )
        results.append(result)
    return results
