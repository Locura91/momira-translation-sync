"""
sync_holiday_package.py — GET -> translate -> PUT loop for Holiday Packages.
Now filters out languages where translation is identical to the source.
"""

import json
from typing import Dict, Any, List, Optional

from state_store import StateStore, compute_hash

ENTITY_TYPE = "holiday_package"
TEXT_FIELDS = ("title", "description", "ribbonText")
WRITABLE_FIELDS = (
    "active", "title", "largeTitle", "description", "remarks",
    "visible", "order", "autocancelable",
)


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


def extract_translatable_fields(package_entry: Dict[str, Any]) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for key in TEXT_FIELDS:
        val = package_entry.get(key)
        if isinstance(val, str) and val.strip():
            fields[key] = val
    return fields


def build_updated_package_payload(original_entry: Dict[str, Any],
                                  lang_fields: Dict[str, str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key in WRITABLE_FIELDS:
        if key in original_entry:
            payload[key] = original_entry[key]
    for key in TEXT_FIELDS:
        if key in lang_fields:
            payload[key] = lang_fields[key]
    return payload


def fetch_holiday_package_by_id(api, microsite_id: str, package_id: str,
                                lang: str = "EN", page_size: int = 100) -> Optional[Dict[str, Any]]:
    first_result = 0
    seen = 0
    while True:
        data = api.get_holiday_packages(microsite_id, lang=lang, firstResult=first_result, pageResults=page_size)
        if isinstance(data, dict) and "error" in data:
            return None
        packages = data.get("package", []) if isinstance(data, dict) else []
        for p in packages:
            if str(p.get("id")) == str(package_id):
                return p
        if not packages:
            return None
        pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
        total = pagination.get("totalResults", len(packages))
        seen += len(packages)
        first_result += page_size
        if seen >= total:
            return None


def fetch_all_holiday_packages(api, microsite_id: str, lang: str = "EN",
                               page_size: int = 100, limit: int = None) -> List[Dict[str, Any]]:
    all_packages: List[Dict[str, Any]] = []
    first_result = 0
    while True:
        data = api.get_holiday_packages(microsite_id, lang=lang, firstResult=first_result, pageResults=page_size)
        if isinstance(data, dict) and "error" in data:
            break
        packages = data.get("package", []) if isinstance(data, dict) else []
        if not packages:
            break
        all_packages.extend(packages)
        if limit and len(all_packages) >= limit:
            all_packages = all_packages[:limit]
            break
        pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
        total = pagination.get("totalResults", len(all_packages))
        first_result += page_size
        if len(all_packages) >= total:
            break
    return all_packages


def sync_one_package_entry(api, translator, store: StateStore,
                           microsite_id: str, entry: Dict[str, Any],
                           target_languages: List[str],
                           dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    package_id = entry.get("id")
    if package_id is None:
        return {"status": "skipped", "reason": "no 'id' field on package entry"}

    if entry.get("active") is not True:
        return {"status": "skipped", "package_id": package_id, "reason": "active is not true"}

    translatable = extract_translatable_fields(entry)
    if not translatable:
        return {"status": "skipped", "package_id": package_id, "reason": "no translatable text fields found"}

    source_hash = compute_hash(translatable)
    if force:
        needed = list(target_languages)
    else:
        needed = store.languages_needed(ENTITY_TYPE, microsite_id, package_id, source_hash, target_languages)
    if not needed:
        return {"status": "up_to_date", "package_id": package_id}

    print(f"🌐 Translating package {package_id} ('{entry.get('title', '')}'): fields={list(translatable.keys())} -> {needed}")
    all_translations = translator.translate_fields(translatable, needed)

    # Filter out languages that didn't actually change
    translations = filter_successful_translations(all_translations, translatable)
    if not translations:
        return {"status": "skipped", "package_id": package_id,
                "reason": "no successful translations (all identical to source)"}

    per_lang_status = {}
    written_languages = []
    for lang, lang_fields in translations.items():
        payload = build_updated_package_payload(entry, lang_fields)
        if dry_run:
            preview = {k: payload.get(k) for k in TEXT_FIELDS if k in payload}
            per_lang_status[lang] = {"status": "dry_run_preview", "preview": preview}
        else:
            result = api.update_holiday_package(microsite_id, package_id, payload, lang=lang)
            if isinstance(result, dict) and "error" in result:
                per_lang_status[lang] = {"status": "failed", "detail": result}
            else:
                per_lang_status[lang] = "written"
                written_languages.append(lang)

    if not dry_run and written_languages:
        prior_state = store.get_state(ENTITY_TYPE, microsite_id, package_id)
        prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
        all_langs = sorted(set(prior_langs) | set(written_languages))
        store.upsert_state(ENTITY_TYPE, microsite_id, package_id, source_hash, all_langs)

    return {
        "status": "dry_run_preview" if dry_run else "updated",
        "package_id": package_id,
        "languages": per_lang_status,
    }


def sync_holiday_package(api, translator, store: StateStore,
                         microsite_id: str, package_id: str,
                         target_languages: List[str],
                         dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    entry = fetch_holiday_package_by_id(api, microsite_id, package_id)
    if entry is None:
        return {"status": "fetch_failed", "package_id": package_id}
    return sync_one_package_entry(api, translator, store, microsite_id, entry,
                                  target_languages, dry_run=dry_run, force=force)


def sync_all_holiday_packages(api, translator, store: StateStore,
                              microsite_id: str, target_languages: List[str],
                              dry_run: bool = True, limit: int = None,
                              force: bool = False) -> List[Dict[str, Any]]:
    packages = fetch_all_holiday_packages(api, microsite_id, limit=limit)
    print(f"📋 Found {len(packages)} holiday package(s) for microsite '{microsite_id}'.")

    results = []
    for entry in packages:
        results.append(sync_one_package_entry(api, translator, store, microsite_id, entry,
                                              target_languages, dry_run=dry_run, force=force))
    return results
