"""
sync_transport.py — Sync transports (main + options) with translations.
Fixed: baggageAllowance default to 1, and import for options.
"""

import json
import re
import time
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from state_store import StateStore, compute_hash
from translator import get_translator

# ---- Configuration ----
BATCH_SIZE = 10
DELAY_BETWEEN_BATCHES = 2
MAX_OPTION_WORKERS = 5

# ---- Main transport fields ----
MAIN_TEXT_FIELDS = ("name", "description")


def strip_html_and_compress(text: str) -> str:
    if not text:
        return text
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def compress_translatable_fields(fields: Dict[str, str]) -> Dict[str, str]:
    compressed = {}
    for key, value in fields.items():
        if isinstance(value, str):
            compressed[key] = strip_html_and_compress(value)
        else:
            compressed[key] = value
    return compressed


def extract_translatable_fields_from_transport(transport_entry: Dict[str, Any]) -> Dict[str, str]:
    datasheets = transport_entry.get("datasheets")
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
    for f in MAIN_TEXT_FIELDS:
        val = en_entry.get(f)
        if isinstance(val, str) and val.strip():
            fields[f] = val
    return fields


def get_existing_content_for_language(entry: Dict[str, Any], lang: str) -> Dict[str, str]:
    # Try datasheets, then translations, then remarks
    for key in ("datasheets", "translations", "remarks"):
        container = entry.get(key)
        if container and isinstance(container, dict):
            lang_entry = container.get(lang)
            if lang_entry and isinstance(lang_entry, dict):
                fields = {}
                for k, v in lang_entry.items():
                    if isinstance(v, str) and v.strip():
                        fields[k] = v
                if fields:
                    return fields
            elif isinstance(lang_entry, str) and lang_entry.strip():
                return {"remarks": lang_entry}
    # Also check top-level fields like name, description
    fields = {}
    for f in ("name", "description"):
        val = entry.get(f)
        if isinstance(val, str) and val.strip():
            fields[f] = val
    return fields


def build_updated_datasheets(
    original_datasheets: Dict[str, Any],
    translations_by_lang: Dict[str, Dict[str, str]],
    en_entry: Dict[str, Any],
) -> Dict[str, Any]:
    new_datasheets = dict(original_datasheets)
    for lang, trans in translations_by_lang.items():
        base = dict(en_entry)
        for f, text in trans.items():
            base[f] = text
        if lang in original_datasheets:
            for k, v in original_datasheets[lang].items():
                if k not in base:
                    base[k] = v
        new_datasheets[lang] = base
    return new_datasheets


def verify_and_filter_needed(
    store: StateStore,
    entity_type: str,
    supplier_id: str,
    entity_id: str,
    source_hash: str,
    target_languages: List[str],
    current_entry: Dict[str, Any],
    source_fields: Dict[str, str],
    option_code: str = "",
) -> List[str]:
    state = store.get_state(entity_type, supplier_id, entity_id, option_code)
    if state is None or state["source_hash"] != source_hash:
        needed = list(target_languages)
    else:
        already_done = set(state["translated_languages"])
        needed = [lang for lang in target_languages if lang not in already_done]

    truly_needed = []
    languages_to_add_to_state = []

    for lang in needed:
        existing = get_existing_content_for_language(current_entry, lang)
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


def sync_transport_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    transport_entry: Dict[str, Any],
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    start_time = time.time()
    transport_id = transport_entry.get("id")
    if not transport_id:
        return {"status": "skipped", "reason": "no 'id' field"}

    datasheets = transport_entry.get("datasheets")
    if not datasheets:
        return {"status": "skipped", "transport_id": transport_id, "reason": "no datasheets"}

    translatable = extract_translatable_fields_from_transport(transport_entry)
    if not translatable:
        return {"status": "skipped", "transport_id": transport_id, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    t0 = time.time()
    if force:
        needed = list(target_languages)
    else:
        needed = verify_and_filter_needed(
            store, "transport", supplier_id, transport_id, source_hash,
            target_languages, transport_entry, translatable
        )
    verify_time = time.time() - t0

    if not needed:
        return {"status": "up_to_date", "transport_id": transport_id}

    compressed_translatable = compress_translatable_fields(translatable)

    combined_translations = {}
    total_batches = (len(needed) + BATCH_SIZE - 1) // BATCH_SIZE
    translation_start = time.time()
    for i in range(0, len(needed), BATCH_SIZE):
        batch = needed[i:i+BATCH_SIZE]
        batch_num = i//BATCH_SIZE + 1
        print(f"   batch {batch_num}/{total_batches}: {batch}")
        batch_result = translator.translate_fields(compressed_translatable, batch)
        combined_translations.update(batch_result)
        if i + BATCH_SIZE < len(needed):
            time.sleep(DELAY_BETWEEN_BATCHES)
    translation_time = time.time() - translation_start

    successful = {}
    for lang, trans in combined_translations.items():
        changed = False
        for field, src in translatable.items():
            if trans.get(field) != src:
                changed = True
                break
        if changed:
            successful[lang] = trans
        else:
            print(f"⚠️  Translation for {lang} identical to source; skipping.")

    if not successful:
        return {"status": "skipped", "transport_id": transport_id, "reason": "no successful translations"}

    en_entry = datasheets.get("EN") or datasheets.get("EN_US") or {}
    new_datasheets = build_updated_datasheets(datasheets, successful, en_entry)

    if dry_run:
        preview = {lang: {k: v for k, v in trans.items() if k in MAIN_TEXT_FIELDS}
                   for lang, trans in successful.items()}
        return {"status": "dry_run_preview", "transport_id": transport_id,
                "languages": list(successful.keys()), "preview": preview}

    write_start = time.time()
    payload = dict(transport_entry)
    payload["datasheets"] = new_datasheets
    payload.setdefault("airlineCode", "")
    result = api.update_transport(supplier_id, payload)
    write_time = time.time() - write_start
    if isinstance(result, dict) and "error" in result:
        return {"status": "put_failed", "transport_id": transport_id, "detail": result}

    written_langs = list(successful.keys())
    prior_state = store.get_state("transport", supplier_id, transport_id)
    prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
    all_langs = sorted(set(prior_langs) | set(written_langs))
    store.upsert_state("transport", supplier_id, transport_id, source_hash, all_langs)

    total_time = time.time() - start_time
    print(f"✅ Transport {transport_id} done in {total_time:.1f}s "
          f"(verify: {verify_time:.1f}s, translate: {translation_time:.1f}s, write: {write_time:.1f}s)")
    return {"status": "updated", "transport_id": transport_id, "languages_written": written_langs}


def sync_transport(api, translator, store: StateStore,
                   supplier_id: str, transport_id: str,
                   target_languages: List[str],
                   dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    transport = api.get_transport(supplier_id, transport_id)
    if isinstance(transport, dict) and "error" in transport:
        return {"status": "fetch_failed", "transport_id": transport_id, "detail": transport}

    main_result = sync_transport_from_data(api, translator, store, supplier_id, transport,
                                           target_languages, dry_run=dry_run, force=force)

    if transport.get("optionCodes"):
        option_results = sync_all_options_for_transport_from_data(
            api, translator, store, supplier_id, transport, target_languages,
            dry_run=dry_run, force=force
        )
        if isinstance(main_result, dict):
            main_result["options"] = option_results
    else:
        if isinstance(main_result, dict):
            main_result["options"] = [{"status": "skipped", "reason": "no options"}]

    return main_result


# ---- Option functions ----
def extract_translatable_fields_from_transport_option(option_entry: Dict[str, Any]) -> Dict[str, str]:
    fields = {}
    # Try translations
    translations = option_entry.get("translations")
    if translations:
        en_entry = translations.get("EN") or translations.get("EN_US") or {}
        if isinstance(en_entry, dict):
            for k, v in en_entry.items():
                if isinstance(v, str) and v.strip():
                    fields[k] = v
        elif isinstance(en_entry, str) and en_entry.strip():
            fields["name"] = en_entry
    # Try datasheets
    datasheets = option_entry.get("datasheets")
    if datasheets:
        en_entry = datasheets.get("EN") or datasheets.get("EN_US") or {}
        if isinstance(en_entry, dict):
            for k, v in en_entry.items():
                if isinstance(v, str) and v.strip() and k not in fields:
                    fields[k] = v
    # Try remarks
    remarks = option_entry.get("remarks")
    if remarks and isinstance(remarks, dict):
        en_remarks = remarks.get("EN") or remarks.get("EN_US") or {}
        if isinstance(en_remarks, dict):
            for k, v in en_remarks.items():
                if isinstance(v, str) and v.strip() and k not in fields:
                    fields[k] = v
        elif isinstance(en_remarks, str) and en_remarks.strip():
            fields["remarks"] = en_remarks
    # Also top-level name/description
    for f in ("name", "description"):
        val = option_entry.get(f)
        if isinstance(val, str) and val.strip() and f not in fields:
            fields[f] = val
    return fields


def build_updated_option(original_option: Dict[str, Any],
                         translations_by_lang: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    new_option = dict(original_option)

    # Determine where to put translations (prefer translations, then datasheets, then remarks)
    target_key = None
    if "translations" in original_option and isinstance(original_option["translations"], dict):
        target_key = "translations"
    elif "datasheets" in original_option and isinstance(original_option["datasheets"], dict):
        target_key = "datasheets"
    elif "remarks" in original_option and isinstance(original_option["remarks"], dict):
        target_key = "remarks"
    else:
        target_key = "translations"
        new_option[target_key] = {}

    original_map = new_option.get(target_key, {})
    new_map = dict(original_map)

    for lang, trans in translations_by_lang.items():
        if isinstance(original_map.get(lang), dict):
            base = dict(original_map.get(lang, {}))
        else:
            base = {}
        for field, text in trans.items():
            base[field] = text
        if target_key == "remarks" and len(trans) == 1 and "remarks" in trans:
            new_map[lang] = trans["remarks"]
        else:
            new_map[lang] = base

    new_option[target_key] = new_map
    return new_option


def sync_transport_option_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    option_entry: Dict[str, Any],
    transport_id: str,
    option_code: str,
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    start_time = time.time()
    translatable = extract_translatable_fields_from_transport_option(option_entry)
    if not translatable:
        return {"status": "skipped", "option_code": option_code, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    entity_id = f"{transport_id}|{option_code}"
    if force:
        needed = list(target_languages)
    else:
        needed = verify_and_filter_needed(
            store, "transport_option", supplier_id, entity_id, source_hash,
            target_languages, option_entry, translatable, option_code=option_code
        )

    if not needed:
        return {"status": "up_to_date", "option_code": option_code}

    compressed_translatable = compress_translatable_fields(translatable)

    combined_translations = {}
    total_batches = (len(needed) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(needed), BATCH_SIZE):
        batch = needed[i:i+BATCH_SIZE]
        batch_num = i//BATCH_SIZE + 1
        print(f"   batch {batch_num}/{total_batches}: {batch}")
        batch_result = translator.translate_fields(compressed_translatable, batch)
        combined_translations.update(batch_result)
        if i + BATCH_SIZE < len(needed):
            time.sleep(DELAY_BETWEEN_BATCHES)

    successful = {}
    for lang, trans in combined_translations.items():
        changed = False
        for field, src in translatable.items():
            if trans.get(field) != src:
                changed = True
                break
        if changed:
            successful[lang] = trans
        else:
            print(f"⚠️  Translation for {lang} identical to source; skipping.")

    if not successful:
        return {"status": "skipped", "option_code": option_code, "reason": "no successful translations"}

    updated_option = build_updated_option(option_entry, successful)

    # Ensure baggageAllowance is a number (default to 1)
    if "baggageAllowance" not in updated_option or not isinstance(updated_option.get("baggageAllowance"), (int, float)):
        updated_option["baggageAllowance"] = 1

    if dry_run:
        preview = {lang: {k: v for k, v in trans.items()} for lang, trans in successful.items()}
        return {"status": "dry_run_preview", "option_code": option_code,
                "languages": list(successful.keys()), "preview": preview}

    result = api.update_transport_option(supplier_id, transport_id, updated_option)
    if isinstance(result, dict) and "error" in result:
        return {"status": "put_failed", "option_code": option_code, "detail": result}

    written_langs = list(successful.keys())
    prior_state = store.get_state("transport_option", supplier_id, entity_id, option_code=option_code)
    prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
    all_langs = sorted(set(prior_langs) | set(written_langs))
    store.upsert_state("transport_option", supplier_id, entity_id, source_hash, all_langs, option_code=option_code)

    elapsed = time.time() - start_time
    print(f"✅ Option {option_code} done in {elapsed:.1f}s")
    return {"status": "updated", "option_code": option_code, "languages_written": written_langs}


def sync_all_options_for_transport_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    transport_entry: Dict[str, Any],
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> List[Dict[str, Any]]:
    option_codes = transport_entry.get("optionCodes", [])
    if not option_codes:
        return [{"status": "skipped", "transport_id": transport_entry.get("id"), "reason": "no options"}]

    transport_id = transport_entry.get("id")
    option_entries = {}
    with ThreadPoolExecutor(max_workers=MAX_OPTION_WORKERS) as executor:
        future_to_code = {
            executor.submit(api.get_transport_option, supplier_id, transport_id, opt_code): opt_code
            for opt_code in option_codes
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

    results = []
    for opt_code in option_codes:
        option = option_entries.get(opt_code)
        if option is None:
            results.append({"status": "fetch_failed", "option_code": opt_code, "detail": "No response"})
            continue
        if isinstance(option, dict) and "error" in option:
            results.append({"status": "fetch_failed", "option_code": opt_code, "detail": option["error"]})
            continue
        result = sync_transport_option_from_data(
            api, translator, store, supplier_id, option, transport_id, opt_code,
            target_languages, dry_run=dry_run, force=force
        )
        results.append(result)
    return results


def fetch_all_transports(api, supplier_id: str, limit: int = None) -> List[Dict[str, Any]]:
    data = api.get_transports(supplier_id)
    transports = data.get("transport", []) if isinstance(data, dict) else []
    if limit:
        transports = transports[:limit]
    return transports


def sync_all_transports_for_supplier(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    target_languages: List[str],
    dry_run: bool = True,
    limit: int = None,
    force: bool = False,
) -> List[Dict[str, Any]]:
    transports = fetch_all_transports(api, supplier_id, limit=limit)
    print(f"📋 Found {len(transports)} transport(s) for supplier {supplier_id}.")

    results = []
    for t in transports:
        transport_id = t.get("id")
        if not transport_id:
            results.append({"status": "skipped", "reason": "no id field", "raw": t})
            continue

        main_result = sync_transport_from_data(
            api, translator, store, supplier_id, t, target_languages,
            dry_run=dry_run, force=force
        )
        results.append(main_result)

        if t.get("optionCodes"):
            option_results = sync_all_options_for_transport_from_data(
                api, translator, store, supplier_id, t, target_languages,
                dry_run=dry_run, force=force
            )
            if isinstance(main_result, dict):
                main_result["options"] = option_results
            else:
                results.extend(option_results)
        else:
            if isinstance(main_result, dict):
                main_result["options"] = [{"status": "skipped", "reason": "no options"}]
    return results
