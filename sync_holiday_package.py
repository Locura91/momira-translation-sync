"""
sync_holiday_package.py — GET -> translate -> PUT loop for Holiday Packages.

CONFIRMED SCOPE (from your 3 real GET examples):

  1. GET /package/{micrositeId}                    <- marketing copy lives HERE
     {"package": [{id, title, largeTitle, description, themes[...], ...}],
      "pagination": {...}}
     This is the ONLY place `title` / `largeTitle` / `description` / `themes`
     show up. It's also the source for the documented PUT
     (IdeaUpdateRequestVO), so this is what we translate + write back.

  2. GET /package/{micrositeId}/info/{holidayPackageId}
     Day-to-day itinerary: destinations, hotels, tickets, transfers per day.
     The text here (hotel descriptions, ticket descriptions) is THIRD-PARTY
     supplier content (Expedia/GIATA hotel copy, Viator/GetYourGuide tour
     copy — see providerCode/ticketId/giataId fields). There is no
     documented PUT for this shape, so it is OUT OF SCOPE: nothing to write
     translations back to, and overwriting supplier-authored copy with our
     own AI translation would likely be worse than what those suppliers
     already provide natively for other locales.

  3. GET /package/calendar/{micrositeId}/{holidayPackageId}
     Pricing calendar (currency + dates). No translatable text at all.

So this module only ever touches #1: fetch the EN entry, translate
title/largeTitle/description/themes, PUT back once per target language via
PUT /package/{micrositeId}/{holidayPackageId}?lang={target}.

OPEN ITEM carried into the first live (non-dry-run) attempt: the documented
PUT body (IdeaUpdateRequestVO) includes `visible`, `autocancelable`, and
`remarks` fields that never appear in real GET data. This code does NOT
fabricate values for them — it PUTs back the original entry with only the
translated fields swapped in. If Travel Compositor rejects that with a 400
for a missing required field, the run_sync_packages.py output will show you
the exact API error text; paste it back and we'll adjust the payload builder
(most likely: it's fine to omit them and the API keeps existing values, or
there's a documented default we're missing).
"""

import json
from typing import Dict, Any, List, Optional

from state_store import StateStore, compute_hash
from translator import ClaudeTranslator

ENTITY_TYPE = "holiday_package"

# Plain string fields we translate as-is.
TEXT_FIELDS = ("title", "largeTitle", "description")


def extract_translatable_fields(package_entry: Dict[str, Any]) -> Dict[str, str]:
    """
    Pulls out everything translatable from ONE package entry (as returned
    inside the "package" list by GET /package/{micrositeId}):
      - title / largeTitle / description, only if present and non-empty
      - themes (a flat list of strings), packed into indexed keys
        "theme_0", "theme_1", ... so they ride through the same
        field->string translation interface as everything else.
    """
    fields: Dict[str, str] = {}
    for key in TEXT_FIELDS:
        val = package_entry.get(key)
        if isinstance(val, str) and val.strip():
            fields[key] = val

    themes = package_entry.get("themes")
    if isinstance(themes, list):
        for i, theme in enumerate(themes):
            if isinstance(theme, str) and theme.strip():
                fields[f"theme_{i}"] = theme

    return fields


def build_updated_package_payload(
    original_entry: Dict[str, Any],
    lang_fields: Dict[str, str],
) -> Dict[str, Any]:
    """
    Builds the PUT body for ONE target language: a copy of the original EN
    entry (preserving every field we don't understand/touch — pricing,
    ids, dates, counters, etc.) with title/largeTitle/description/themes
    swapped for their translated versions.
    """
    payload = dict(original_entry)

    for key in TEXT_FIELDS:
        if key in lang_fields:
            payload[key] = lang_fields[key]

    theme_count = len(original_entry.get("themes") or [])
    if theme_count and any(k.startswith("theme_") for k in lang_fields):
        translated_themes = []
        for i in range(theme_count):
            theme_key = f"theme_{i}"
            if theme_key in lang_fields:
                translated_themes.append(lang_fields[theme_key])
            else:
                # Fallback: keep the original theme string if translation
                # for this particular index is missing for some reason.
                original_themes = original_entry.get("themes") or []
                if i < len(original_themes):
                    translated_themes.append(original_themes[i])
        payload["themes"] = translated_themes

    return payload


def fetch_holiday_package_by_id(
    api,
    microsite_id: str,
    package_id: str,
    lang: str = "EN",
    page_size: int = 100,
) -> Optional[Dict[str, Any]]:
    """
    GET /package/{micrositeId} only returns a LIST (no single-ID GET for the
    marketing fields exists per the confirmed endpoints) — so this pages
    through that list looking for the matching id. Pagination param names
    (firstResult/pageResults) are inferred from the pagination object's own
    key names in the real response; if Travel Compositor uses different
    query param names, this will just fetch page 1 repeatedly — tell me
    what --dry-run prints and we'll fix the param names.
    """
    first_result = 0
    seen = 0
    while True:
        data = api.get_holiday_packages(
            microsite_id, lang=lang, firstResult=first_result, pageResults=page_size
        )
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


def fetch_all_holiday_packages(
    api, microsite_id: str, lang: str = "EN", page_size: int = 100, limit: int = None
) -> List[Dict[str, Any]]:
    """Pages through the full list once (used by sync_all_holiday_packages)."""
    all_packages: List[Dict[str, Any]] = []
    first_result = 0
    while True:
        data = api.get_holiday_packages(
            microsite_id, lang=lang, firstResult=first_result, pageResults=page_size
        )
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


def sync_one_package_entry(
    api,
    translator: ClaudeTranslator,
    store: StateStore,
    microsite_id: str,
    entry: Dict[str, Any],
    target_languages: List[str],
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Core sync logic given an already-fetched EN package entry (dict)."""
    package_id = entry.get("id")
    if package_id is None:
        return {"status": "skipped", "reason": "no 'id' field on package entry"}

    # Rule: only active:true packages get translated. Sold-out/draft/retired
    # packages (active: false) are skipped entirely — no fetch of translations,
    # no state-store entry, no PUT.
    if entry.get("active") is not True:
        return {"status": "skipped", "package_id": package_id, "reason": "active is not true"}

    translatable = extract_translatable_fields(entry)
    if not translatable:
        return {"status": "skipped", "package_id": package_id, "reason": "no translatable text fields found"}

    source_hash = compute_hash(translatable)
    needed = store.languages_needed(ENTITY_TYPE, microsite_id, package_id, source_hash, target_languages)
    if not needed:
        return {"status": "up_to_date", "package_id": package_id}

    text_field_names = [k for k in translatable if not k.startswith("theme_")]
    theme_count = len([k for k in translatable if k.startswith("theme_")])
    print(f"🌐 Translating package {package_id} ('{entry.get('title', '')}'): "
          f"fields={text_field_names} + {theme_count} theme(s) -> {needed}")

    translations = translator.translate_fields(translatable, needed)

    per_lang_status = {}
    written_languages = []
    for lang in needed:
        lang_fields = translations.get(lang, {})
        payload = build_updated_package_payload(entry, lang_fields)

        if dry_run:
            print(f"--- DRY RUN preview: package {package_id}, lang {lang} ---")
            preview = {k: payload.get(k) for k in ("title", "largeTitle", "description", "themes")}
            print(json.dumps(preview, indent=2, ensure_ascii=False))
            per_lang_status[lang] = "dry_run_preview"
            continue

        result = api.update_holiday_package(microsite_id, package_id, payload, lang=lang)
        if isinstance(result, dict) and "error" in result:
            per_lang_status[lang] = {"status": "failed", "detail": result}
        else:
            per_lang_status[lang] = "written"
            written_languages.append(lang)

    if not dry_run and written_languages:
        prior_state = store.get_state(ENTITY_TYPE, microsite_id, package_id)
        prior_langs = (
            prior_state["translated_languages"]
            if prior_state and prior_state["source_hash"] == source_hash
            else []
        )
        all_langs = sorted(set(prior_langs) | set(written_languages))
        store.upsert_state(ENTITY_TYPE, microsite_id, package_id, source_hash, all_langs)

    return {
        "status": "dry_run_preview" if dry_run else "updated",
        "package_id": package_id,
        "languages": per_lang_status,
    }


def sync_holiday_package(
    api,
    translator: ClaudeTranslator,
    store: StateStore,
    microsite_id: str,
    package_id: str,
    target_languages: List[str],
    dry_run: bool = True,
) -> Dict[str, Any]:
    entry = fetch_holiday_package_by_id(api, microsite_id, package_id)
    if entry is None:
        return {"status": "fetch_failed", "package_id": package_id}
    return sync_one_package_entry(api, translator, store, microsite_id, entry, target_languages, dry_run=dry_run)


def sync_all_holiday_packages(
    api,
    translator: ClaudeTranslator,
    store: StateStore,
    microsite_id: str,
    target_languages: List[str],
    dry_run: bool = True,
    limit: int = None,
) -> List[Dict[str, Any]]:
    packages = fetch_all_holiday_packages(api, microsite_id, limit=limit)
    print(f"📋 Found {len(packages)} holiday package(s) for microsite '{microsite_id}'.")

    results = []
    for entry in packages:
        results.append(
            sync_one_package_entry(api, translator, store, microsite_id, entry, target_languages, dry_run=dry_run)
        )
    return results
