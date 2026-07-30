"""
sync_holiday_package.py — GET -> translate -> PUT loop for Holiday Packages.

CONFIRMED SCOPE (from your real GET examples + the official Swagger for
both GET /package/{micrositeId} and PUT /package/{micrositeId}/{holidayPackageId}):

  1. GET /package/{micrositeId}                    <- marketing copy lives HERE
     {"package": [{id, title, largeTitle, description, themes[...], ribbonText, ...}],
      "pagination": {...}}
     This is the ONLY place the translatable fields show up, and it's what we
     translate + write back.

  2. GET /package/{micrositeId}/info/{holidayPackageId}  and
     GET /package/{micrositeId}/{holidayPackageId} (getDayToDay)
     Day-to-day itinerary: destinations, hotels, tickets, transfers per day.
     The text here (hotel descriptions, ticket descriptions) is THIRD-PARTY
     supplier content (Expedia/GIATA hotel copy, Viator/GetYourGuide tour
     copy — see providerCode/ticketId/giataId fields). There is no
     documented PUT for this shape, so it is OUT OF SCOPE.

  3. GET /package/calendar/{micrositeId}/{holidayPackageId}
     Pricing calendar (currency + dates). No translatable text at all.

  4. PUT /package/{micrositeId}/{holidayPackageId}  (updateHolidayPackage)
     CONFIRMED via the official Swagger request schema (IdeaUpdateRequestVO)
     that the ONLY fields this endpoint accepts are:
         active, title, largeTitle, description, remarks, themes,
         visible, order, autocancelable
     Two important consequences of this, confirmed directly from the schema
     rather than guessed:
       - There is NO ownership/permission-scope field anywhere in this
         schema. That means the "401 User not allowed to modify a holiday
         package" error we hit is confirmed to be an ACCOUNT-LEVEL
         permission grant on Travel Compositor's side (something to fix via
         their support/back office), not a payload bug — there is nothing
         in the documented request body we could be getting wrong that
         would cause this.
       - `ribbonText` is NOT part of this write schema at all (it only
         appears in the GET/response schema, IdeaVO). We still send a
         translated ribbonText in the payload as a best-effort extra field
         (harmless if Travel Compositor's deserializer ignores unknown
         field names, which is what its behavior around other extra fields
         suggests) — but until a real live PUT is confirmed to actually
         change ribbonText on a following GET, treat this as UNCONFIRMED.
         If it turns out Travel Compositor silently drops it, that's a
         platform limitation, not something fixable in this code.

TRANSLATABLE FIELDS (confirmed with you against real data): `title`,
`description`, `ribbonText` ONLY.
  - `largeTitle` is dropped from translation — real data shows it's always
    an identical duplicate of `title`, so translating it separately would
    just be wasted cost; it's still passed through untouched in the PUT
    payload since it IS part of the documented write schema.
  - `themes` is dropped from translation AND from the PUT payload entirely
    — used for site filtering/category matching so it must stay in English,
    and separately, GET returns it as plain strings while the PUT schema
    requires an array of ThemeVO objects ({id, name, imageUrl}) — a real
    confirmed schema mismatch (400 error) we can't safely fix without
    knowing each theme's real numeric id, so we simply omit the field and
    let Travel Compositor keep whatever themes the package already has.
  - `ribbonText` (e.g. "Holidays package") IS translated — it's a real
    customer-facing label — but see the PUT-schema caveat above: whether
    Travel Compositor's write endpoint actually persists it is unconfirmed.

PUT PAYLOAD SHAPE: built from ONLY the fields Travel Compositor's own
Swagger documents as writable (`WRITABLE_FIELDS` below), taken from the
original GET entry where present, with the translated title/description
swapped in. We deliberately do NOT copy the entire original GET entry
wholesale anymore — the GET response (IdeaVO) includes many fields (id,
user, email, counters, customer, pricePerPerson, destinations, etc.) that
are not part of the write schema at all, and sending them served no
purpose while adding risk of tripping some other hidden validation (as
happened with themes).
"""

import json
from typing import Dict, Any, List, Optional

from state_store import StateStore, compute_hash

ENTITY_TYPE = "holiday_package"

# The only fields we translate. largeTitle and themes are deliberately
# excluded (see module docstring) and are carried through untouched
# (largeTitle) or dropped (themes).
TEXT_FIELDS = ("title", "description", "ribbonText")

# Fields Travel Compositor's own Swagger documents as accepted by
# PUT /package/{micrositeId}/{holidayPackageId} (schema: IdeaUpdateRequestVO).
# Anything NOT in this list is left out of the payload on purpose (see
# module docstring) — except ribbonText, which we still send as a
# best-effort extra despite not being in this documented list (see below).
WRITABLE_FIELDS = (
    "active", "title", "largeTitle", "description", "remarks",
    "visible", "order", "autocancelable",
)


def extract_translatable_fields(package_entry: Dict[str, Any]) -> Dict[str, str]:
    """
    Pulls out everything translatable from ONE package entry (as returned
    inside the "package" list by GET /package/{micrositeId}): title,
    description, ribbonText — only if present and non-empty.
    """
    fields: Dict[str, str] = {}
    for key in TEXT_FIELDS:
        val = package_entry.get(key)
        if isinstance(val, str) and val.strip():
            fields[key] = val
    return fields


def build_updated_package_payload(
    original_entry: Dict[str, Any],
    lang_fields: Dict[str, str],
) -> Dict[str, Any]:
    """
    Builds the PUT body for ONE target language.

    Starts from ONLY the fields Travel Compositor's Swagger documents as
    writable (WRITABLE_FIELDS), copied from the original EN entry when
    present, then swaps in the translated title/description/ribbonText.

    `themes` is deliberately DROPPED from the payload entirely — confirmed
    against a real live PUT attempt that Travel Compositor's write endpoint
    expects `themes` as an array of `ThemeVO` objects, while GET returns it
    as plain strings (a real schema mismatch between GET and PUT on Travel
    Compositor's side, not something we can fix by reshaping the data
    ourselves without knowing each theme's real numeric id).

    `ribbonText` is NOT in Travel Compositor's documented write schema at
    all, but we include it anyway as a best-effort extra field — cheap to
    try, and their deserializer appears to silently ignore field names it
    doesn't recognize (based on the themes 400 error being a TYPE mismatch
    on a recognized field, not a rejection of unrecognized ones). Confirm
    with a real GET after a live PUT whether this actually takes effect.
    """
    payload: Dict[str, Any] = {}
    for key in WRITABLE_FIELDS:
        if key in original_entry:
            payload[key] = original_entry[key]

    for key in TEXT_FIELDS:
        if key in lang_fields:
            payload[key] = lang_fields[key]

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
    translator,
    store: StateStore,
    microsite_id: str,
    entry: Dict[str, Any],
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
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
    if force:
        # Ignore whatever the state store thinks is already done — useful
        # right after fixing a translator bug, so you're not stuck waiting
        # on a stale "already translated" record from a bad prior run.
        needed = list(target_languages)
    else:
        needed = store.languages_needed(ENTITY_TYPE, microsite_id, package_id, source_hash, target_languages)
    if not needed:
        return {"status": "up_to_date", "package_id": package_id}

    print(f"🌐 Translating package {package_id} ('{entry.get('title', '')}'): "
          f"fields={list(translatable.keys())} -> {needed}")

    translations = translator.translate_fields(translatable, needed)

    per_lang_status = {}
    written_languages = []
    for lang in needed:
        lang_fields = translations.get(lang, {})
        payload = build_updated_package_payload(entry, lang_fields)

        if dry_run:
            preview = {k: payload.get(k) for k in TEXT_FIELDS if k in payload}
            print(f"--- DRY RUN preview: package {package_id}, lang {lang} ---")
            print(json.dumps(preview, indent=2, ensure_ascii=False))
            # Include the actual translated text in the returned result (not
            # just a status string) so it's visible in the Streamlit app's
            # "Full result" panel too, without needing server console access.
            per_lang_status[lang] = {"status": "dry_run_preview", "preview": preview}
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
    translator,
    store: StateStore,
    microsite_id: str,
    package_id: str,
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    entry = fetch_holiday_package_by_id(api, microsite_id, package_id)
    if entry is None:
        return {"status": "fetch_failed", "package_id": package_id}
    return sync_one_package_entry(api, translator, store, microsite_id, entry, target_languages, dry_run=dry_run, force=force)


def sync_all_holiday_packages(
    api,
    translator,
    store: StateStore,
    microsite_id: str,
    target_languages: List[str],
    dry_run: bool = True,
    limit: int = None,
    force: bool = False,
) -> List[Dict[str, Any]]:
    packages = fetch_all_holiday_packages(api, microsite_id, limit=limit)
    print(f"📋 Found {len(packages)} holiday package(s) for microsite '{microsite_id}'.")

    results = []
    for entry in packages:
        results.append(
            sync_one_package_entry(api, translator, store, microsite_id, entry, target_languages, dry_run=dry_run, force=force)
        )
    return results
