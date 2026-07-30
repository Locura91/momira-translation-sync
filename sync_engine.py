"""
sync_engine.py — the actual GET -> translate -> merge -> PUT loop.

This prototype implements ONE inventory type end to end: Transfers (the
simplest — no sub-options). Once you've run this against real data and
confirmed the output looks right, the same pattern (see sync_transfer
below) extends directly to Transport, Hotels, Tickets, and Closed Tours —
each just needs its own thin sync_<type> function calling the matching
get_/update_ methods already added to travelcompositor_api.py.

Key design decision: instead of guessing the exact field names inside
Travel Compositor's "datasheets" object (title? description? something
else?), this code inspects whatever string fields actually exist in the
real EN entry at runtime and translates all of them, preserving whatever
key names/other fields it finds. This sidesteps the "confirm the schema"
open item entirely — run it once with --dry-run and read the printed
preview to see exactly what got picked up.
"""

import json
from typing import Dict, Any, List, Tuple

from state_store import StateStore, compute_hash
from translator import ClaudeTranslator

# Fields we NEVER treat as translatable text, even if they're strings,
# because they're identifiers/codes rather than customer-facing content.
EXCLUDED_KEYS = {"code", "id", "language", "lang", "locale", "type", "currency", "providerCode"}


class ShapeDetectionError(Exception):
    """Raised when datasheets doesn't match either known shape — needs a human to look at it."""


def extract_en_entry(datasheets_field: Any) -> Tuple[str, Dict[str, Any]]:
    """
    Detects which of the two shapes Travel Compositor is using and returns
    (shape, the EN sub-object). shape is "dict" (keyed by language code,
    e.g. {"EN": {...}, "FR": {...}}) or "list" (list of per-language objects,
    e.g. [{"language": "EN", ...}, ...]).
    """
    if isinstance(datasheets_field, dict):
        if "EN" in datasheets_field and isinstance(datasheets_field["EN"], dict):
            return "dict", datasheets_field["EN"]
    elif isinstance(datasheets_field, list):
        for item in datasheets_field:
            if isinstance(item, dict):
                lang_val = item.get("language") or item.get("lang") or item.get("locale")
                if lang_val and str(lang_val).upper() == "EN":
                    return "list", item
    raise ShapeDetectionError(
        f"Could not find an EN entry in datasheets. Raw value:\n{json.dumps(datasheets_field, indent=2, ensure_ascii=False)}"
    )


def extract_translatable_fields(entry: Dict[str, Any]) -> Dict[str, str]:
    """Every non-excluded string field with real content — regardless of what it's named."""
    return {
        k: v for k, v in entry.items()
        if isinstance(v, str) and k not in EXCLUDED_KEYS and v.strip()
    }


def build_updated_datasheets(
    shape: str,
    original_datasheets: Any,
    translations_by_lang: Dict[str, Dict[str, str]],
    en_entry: Dict[str, Any],
) -> Any:
    """
    Merges new/updated translations into the ORIGINAL datasheets structure,
    preserving every language and every field this run didn't touch
    (non-destructive merge, per the design doc's idempotency rules).
    """
    if shape == "dict":
        new_datasheets = dict(original_datasheets)
        for lang, translated_fields in translations_by_lang.items():
            lang_entry = dict(original_datasheets.get(lang) or en_entry)
            lang_entry.update(translated_fields)
            new_datasheets[lang] = lang_entry
        return new_datasheets

    elif shape == "list":
        new_list = [dict(item) for item in original_datasheets]
        lang_key = next((k for k in ("language", "lang", "locale") if k in en_entry), "language")
        existing_by_lang = {
            str(item.get(lang_key)).upper(): idx
            for idx, item in enumerate(new_list) if item.get(lang_key)
        }
        for lang, translated_fields in translations_by_lang.items():
            if lang in existing_by_lang:
                new_list[existing_by_lang[lang]].update(translated_fields)
            else:
                new_entry = dict(en_entry)
                new_entry[lang_key] = lang
                new_entry.update(translated_fields)
                new_list.append(new_entry)
        return new_list

    raise ValueError(f"Unknown shape: {shape}")


def sync_transfer(
    api,
    translator: ClaudeTranslator,
    store: StateStore,
    supplier_id: str,
    transfer_id: str,
    target_languages: List[str],
    dry_run: bool = True,
) -> Dict[str, Any]:
    transfer = api.get_transfer(supplier_id, transfer_id)
    if isinstance(transfer, dict) and "error" in transfer:
        return {"status": "fetch_failed", "transfer_id": transfer_id, "detail": transfer}

    datasheets = transfer.get("datasheets")
    if not datasheets:
        return {"status": "skipped", "transfer_id": transfer_id, "reason": "no 'datasheets' field present"}

    try:
        shape, en_entry = extract_en_entry(datasheets)
    except ShapeDetectionError as e:
        return {"status": "needs_manual_review", "transfer_id": transfer_id, "detail": str(e)}

    translatable = extract_translatable_fields(en_entry)
    if not translatable:
        return {"status": "skipped", "transfer_id": transfer_id, "reason": "no translatable text fields found in EN entry"}

    source_hash = compute_hash(translatable)
    needed = store.languages_needed("transfer", supplier_id, transfer_id, source_hash, target_languages)
    if not needed:
        return {"status": "up_to_date", "transfer_id": transfer_id}

    print(f"🌐 Translating transfer {transfer_id}: {list(translatable.keys())} -> {needed}")
    translations = translator.translate_fields(translatable, needed)
    new_datasheets = build_updated_datasheets(shape, datasheets, translations, en_entry)

    if dry_run:
        print(f"--- DRY RUN preview for transfer {transfer_id} ---")
        print(json.dumps(translations, indent=2, ensure_ascii=False))
        return {"status": "dry_run_preview", "transfer_id": transfer_id, "languages": needed}

    updated_payload = dict(transfer)
    updated_payload["datasheets"] = new_datasheets
    result = api.update_transfer(supplier_id, updated_payload)
    if isinstance(result, dict) and "error" in result:
        return {"status": "put_failed", "transfer_id": transfer_id, "detail": result}

    prior_state = store.get_state("transfer", supplier_id, transfer_id)
    prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
    all_langs = sorted(set(prior_langs) | set(needed))
    store.upsert_state("transfer", supplier_id, transfer_id, source_hash, all_langs)
    return {"status": "updated", "transfer_id": transfer_id, "languages_written": needed}


def sync_all_transfers_for_supplier(
    api,
    translator: ClaudeTranslator,
    store: StateStore,
    supplier_id: str,
    target_languages: List[str],
    dry_run: bool = True,
    limit: int = None,
) -> List[Dict[str, Any]]:
    data = api.get_transfers(supplier_id)
    transfers = data.get("transfer", []) if isinstance(data, dict) else []
    if limit:
        transfers = transfers[:limit]
    print(f"📋 Found {len(transfers)} transfer(s) for supplier {supplier_id}.")

    results = []
    for t in transfers:
        transfer_id = t.get("id")
        if transfer_id is None:
            results.append({"status": "skipped", "reason": "no 'id' field on list entry", "raw": t})
            continue
        results.append(
            sync_transfer(api, translator, store, supplier_id, transfer_id, target_languages, dry_run=dry_run)
        )
    return results
