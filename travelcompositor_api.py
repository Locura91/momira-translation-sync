import os
import json
import difflib
import requests
from typing import Dict, Any, Optional, List
from dotenv import load_dotenv
load_dotenv()


def _try_parse_json(text: str):
    """Best-effort JSON parse for including a response body in a log block."""
    try:
        return json.loads(text)
    except Exception:
        return text


def _mask_auth_token_header(headers: Dict[str, str]) -> Dict[str, str]:
    """
    Masks the auth-token value (e.g. 'abcd1234...wxyz') before it goes into
    any log/result we might show in the app or paste into a support ticket.
    Keeps enough of it visible (first 6 / last 4 chars) to prove a token was
    present without exposing the live session token in full.
    """
    masked = {}
    for k, v in dict(headers).items():
        if k.lower() == "auth-token" and isinstance(v, str) and v:
            masked[k] = f"{v[:6]}...{v[-4:]}" if len(v) > 12 else "***"
        else:
            masked[k] = v
    return masked


class TravelCompositorAPI:
    """
    Single, shared client for all Travel Compositor API interactions:
    authentication, destination resolution, and closed-tour uploads.
    This replaces the destination-resolution logic that used to be
    duplicated (and inconsistent) across main.py, get_tc_destinations.py,
    and step2_parser.py.

    --- nbext addition ---
    This file is the ORIGINAL working client, unchanged, with new methods
    appended at the bottom for Transfer, Transport, Hotel, and Holiday
    Package / Idea endpoints (needed for the translation-sync engine).
    Nothing above the "TRANSLATION-SYNC ADDITIONS" marker was touched.
    """

    def __init__(self):
        self.api_base_url = os.getenv("TRAVELC_BASE_URL", "https://online.travelcompositor.com/resources").rstrip("/")
        self.microsite_id = os.getenv("TRAVELC_MICROSITE_ID", "momiratravel")
        self.username = os.getenv("TRAVELC_USERNAME", "")
        self.password = os.getenv("TRAVELC_PASSWORD", "")
        self.auth_token: Optional[str] = None
        self._destination_cache: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # AUTH
    # ------------------------------------------------------------------
    def authenticate(self, force: bool = False) -> str:
        """
        Logs in via POST /authentication/authenticate to obtain an active auth-token.
        Set force=True to bypass the cached token and get a fresh one (e.g. after a 401).
        """
        if self.auth_token and not force:
            return self.auth_token
        url = f"{self.api_base_url}/authentication/authenticate"
        payload = {
            "username": self.username,
            "password": self.password,
            "micrositeId": self.microsite_id
        }
        headers = {"Content-Type": "application/json"}
        print(f"🔑 Authenticating via POST {url}...")
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        if res.status_code == 200:
            self.auth_token = res.headers.get("auth-token") or res.headers.get("Auth-Token")
            if not self.auth_token and res.text:
                try:
                    data = res.json()
                    self.auth_token = data.get("token") or data.get("authToken") or data.get("auth-token")
                except Exception:
                    self.auth_token = res.text.strip('"')
            print("✅ Auth successful! Token acquired.")
            return self.auth_token
        else:
            print(f"❌ Auth failed (Status {res.status_code}): {res.text}")
            res.raise_for_status()

    def get_headers(self) -> Dict[str, str]:
        if not self.auth_token:
            self.authenticate()
        return {
            "auth-token": self.auth_token,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        Wraps requests.request() with automatic re-authentication if the
        token has expired (401). Without this, an expired token mid-session
        looks like a random "connection failure" instead of an auth issue.
        """
        kwargs.setdefault("timeout", 15)
        res = requests.request(method, url, headers=self.get_headers(), **kwargs)
        if res.status_code == 401:
            print("♻️  Auth token expired/rejected — re-authenticating and retrying once...")
            self.authenticate(force=True)
            res = requests.request(method, url, headers=self.get_headers(), **kwargs)
        return res

    # ------------------------------------------------------------------
    # DESTINATIONS  (the consolidated, correct resolver)
    # ------------------------------------------------------------------
    def _get_all_destinations(self, lang: str = "EN") -> List[Dict[str, Any]]:
        """
        Fetches and caches the FULL destination list for the microsite.
        The Travel Compositor API has NO free-text search parameter
        (only countryCode / iata filters exist) - this is the only
        reliable way to match destinations by name.
        """
        if self._destination_cache is not None:
            return self._destination_cache
        url = f"{self.api_base_url}/destination/{self.microsite_id}"
        res = self._request("GET", url, params={"lang": lang})
        res.raise_for_status()
        data = res.json()
        destinations = data.get("destination", []) if isinstance(data, dict) else data
        self._destination_cache = destinations or []
        print(f"📥 Cached {len(self._destination_cache)} destinations for '{self.microsite_id}'.")
        return self._destination_cache

    def find_destinations_in_text(self, text: str, min_name_length: int = 4) -> List[Dict[str, Any]]:
        """
        Scans arbitrary text (e.g. a scraped web page heading or paragraph)
        for mentions of any real Travel Compositor destination name, using
        the full cached destination list. Matches on word boundaries to
        avoid false positives from short/common names.
        Returns matches in the order their destination NAME first appears
        in the text (useful for reconstructing itinerary order).
        """
        import re
        destinations = self._get_all_destinations()
        text_lower = text.lower()
        candidates = []
        for dest in destinations:
            name = dest.get("name", "")
            code = dest.get("code")
            if not code or len(name) < min_name_length:
                continue
            match = re.search(r'\b' + re.escape(name.lower()) + r'\b', text_lower)
            if match:
                candidates.append((match.start(), code, name))
        candidates.sort(key=lambda c: c[0])
        seen = set()
        results = []
        for _, code, name in candidates:
            if code not in seen:
                seen.add(code)
                results.append({"code": code, "name": name})
        return results

    def resolve_destination_geolocation(self, query_term: str) -> Dict[str, Any]:
        """
        Reuses the SAME destination search/cache as resolve_destination(), but
        for Tickets, which need latitude/longitude instead of a destination code.
        NOT YET CONFIRMED against live data whether Travel Compositor's
        destination records actually include coordinates - this attempts a
        few common field name variants (latitude/longitude, lat/lng, lat/lon)
        and clearly reports if none were found, so the human knows they may
        need to enter coordinates manually as a fallback.
        Returns: {"latitude": float|None, "longitude": float|None, "name": str,
                   "valid": bool, "source": str}
        """
        clean_query = (query_term or "").strip()
        if not clean_query:
            return {"latitude": None, "longitude": None, "name": None, "valid": False, "source": "empty_query"}

        def _extract_coords(d: dict):
            for lat_key, lng_key in [("latitude", "longitude"), ("lat", "lng"), ("lat", "lon")]:
                if d.get(lat_key) is not None and d.get(lng_key) is not None:
                    try:
                        return float(d[lat_key]), float(d[lng_key])
                    except (TypeError, ValueError):
                        continue
            return None, None

        code_candidate = clean_query.upper()
        url_direct = f"{self.api_base_url}/destination/{self.microsite_id}/{code_candidate}"
        try:
            res = self._request("GET", url_direct, params={"lang": "EN"})
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, dict) and data.get("code"):
                    lat, lng = _extract_coords(data)
                    return {
                        "latitude": lat, "longitude": lng, "name": data.get("name", clean_query),
                        "valid": lat is not None and lng is not None, "source": "direct_code"
                    }
        except requests.RequestException:
            pass

        try:
            destinations = self._get_all_destinations()
        except requests.RequestException:
            destinations = []
        query_lower = clean_query.lower()
        for dest in destinations:
            if dest.get("name", "").strip().lower() == query_lower:
                lat, lng = _extract_coords(dest)
                return {
                    "latitude": lat, "longitude": lng, "name": dest.get("name"),
                    "valid": lat is not None and lng is not None, "source": "exact_name"
                }
        matches = [d for d in destinations if query_lower in d.get("name", "").lower()]
        if matches:
            lat, lng = _extract_coords(matches[0])
            return {
                "latitude": lat, "longitude": lng, "name": matches[0].get("name"),
                "valid": lat is not None and lng is not None, "source": "partial_name"
            }
        return {"latitude": None, "longitude": None, "name": clean_query, "valid": False, "source": "not_found"}

    def resolve_destination(self, query_term: str) -> Dict[str, Any]:
        """
        Resolves ANY destination input:
          - Exact/custom codes (e.g. 'ASW', 'EDF-2') -> direct GET by ID
          - Free-text names (e.g. 'Edfu', 'Kom Ombo') -> local match against
            the cached full destination list (exact -> substring -> fuzzy)
        Returns: {"tc_code": str, "name": str, "valid": bool, ...}
        """
        clean_query = (query_term or "").strip()
        if not clean_query:
            return {"tc_code": None, "name": None, "valid": False}

        code_candidate = clean_query.upper()
        url_direct = f"{self.api_base_url}/destination/{self.microsite_id}/{code_candidate}"
        try:
            res = self._request("GET", url_direct, params={"lang": "EN"})
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, dict) and data.get("code"):
                    code, name = data["code"], data.get("name", data["code"])
                    print(f"✅ RESOLVED (by code): '{clean_query}' -> {code} ({name})")
                    return {"tc_code": code, "name": name, "valid": True, "match_type": "code"}
        except requests.RequestException as e:
            print(f"⚠️ Direct code lookup failed for '{clean_query}': {e}")

        try:
            destinations = self._get_all_destinations()
        except requests.RequestException as e:
            print(f"⚠️ Could not fetch destination list: {e}")
            destinations = []
        query_lower = clean_query.lower()

        for dest in destinations:
            if dest.get("name", "").strip().lower() == query_lower:
                code, name = dest.get("code"), dest.get("name")
                print(f"✅ RESOLVED (exact name): '{clean_query}' -> {code} ({name})")
                return {"tc_code": code, "name": name, "valid": True, "match_type": "exact_name"}

        matches = [d for d in destinations if query_lower in d.get("name", "").lower()]
        if matches:
            best = matches[0]
            code, name = best.get("code"), best.get("name")
            print(f"✅ RESOLVED (partial name, {len(matches)} candidates): '{clean_query}' -> {code} ({name})")
            return {"tc_code": code, "name": name, "valid": True, "match_type": "partial_name", "alternatives": len(matches)}

        names = [d.get("name", "") for d in destinations if d.get("name")]
        close = difflib.get_close_matches(clean_query, names, n=1, cutoff=0.75)
        if close:
            best = next(d for d in destinations if d.get("name") == close[0])
            code, name = best.get("code"), best.get("name")
            print(f"✅ RESOLVED (fuzzy): '{clean_query}' -> {code} ({name})")
            return {"tc_code": code, "name": name, "valid": True, "match_type": "fuzzy"}

        print(f"⚠️ Destination '{clean_query}' not found anywhere. Flagging as invalid.")
        return {"tc_code": code_candidate, "name": clean_query, "valid": False, "match_type": "none"}

    def lookup_destination_code(self, destination_id: str) -> str:
        """
        Backwards-compatible shim so existing callers (e.g. builder.py)
        keep working. Internally now uses the full resolver instead of
        a bare direct-code-only GET.
        """
        result = self.resolve_destination(destination_id)
        return result["tc_code"]

    def resolve_destinations_bulk(self, terms: List[str]) -> List[Dict[str, Any]]:
        """Resolve a list of names/codes in one call, de-duplicated, in order."""
        results = []
        seen_codes = set()
        for term in terms:
            r = self.resolve_destination(term)
            if r["tc_code"] and r["tc_code"] not in seen_codes:
                seen_codes.add(r["tc_code"])
                results.append(r)
        return results

    def test_connection_destination(self, test_dest_id: str = "ASW") -> Dict[str, Any]:
        """Quick manual sanity check of the destination endpoint."""
        result = self.resolve_destination(test_dest_id)
        if result["valid"]:
            print(f"✅ Connection OK. {test_dest_id} -> {result['tc_code']} ({result['name']})")
        else:
            print(f"❌ Could not resolve '{test_dest_id}'.")
        return result

    # ------------------------------------------------------------------
    # CLOSED TOUR UPLOADS
    # ------------------------------------------------------------------
    def get_all_suppliers(self) -> List[Dict[str, Any]]:
        """
        Executes GET /suppliers — returns the full list of ContractSupplierVO
        for this operator (each has 'id', 'commercialName', 'legalName', etc).
        Used to build a human-friendly supplier picker instead of requiring
        people to know/type numeric supplier IDs by heart.
        """
        url = f"{self.api_base_url}/suppliers"
        res = self._request("GET", url)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return []
        data = res.json()
        return data if isinstance(data, list) else []

    def get_all_users(self) -> List[Dict[str, Any]]:
        """
        Executes GET /user/{micrositeId} — returns real, formally-registered
        users for this microsite. Used to check whether a userId we send in
        payloads (e.g. 'momiratravel-Christian') actually corresponds to a
        real account, or is being silently ignored/replaced.
        """
        url = f"{self.api_base_url}/user/{self.microsite_id}"
        res = self._request("GET", url)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return []
        data = res.json()
        return data if isinstance(data, list) else []

    def get_closed_tour(self, supplier_id: str, closed_tour_code: str) -> Dict[str, Any]:
        """
        Executes GET /closedtour/{supplierId}/{closedTourCode} — returns the
        full existing tour (name, itinerary, modalityCodes list, etc).
        NOTE: the tour's own 'price' field is deprecated and always 0 -
        real pricing lives per-option, fetched via get_closed_tour_option().
        """
        url = f"{self.api_base_url}/closedtour/{supplier_id}/{closed_tour_code}"
        res = self._request("GET", url)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def get_closed_tour_option(self, supplier_id: str, closed_tour_code: str, option_code: str) -> Dict[str, Any]:
        """
        Executes GET /closedtour/{supplierId}/{closedTourCode}/{optionCode}
        — returns one specific option's full details, including its live
        priceList. Use this before updating an option, to see exactly
        what's currently there.
        """
        url = f"{self.api_base_url}/closedtour/{supplier_id}/{closed_tour_code}/{option_code}"
        res = self._request("GET", url)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def create_closed_tour(self, supplier_id: str, payload: dict) -> Dict[str, Any]:
        """Executes POST /closedtour/{supplierId} — creates main tour (draft, active: False)."""
        url = f"{self.api_base_url}/closedtour/{supplier_id}"
        res = self._request("POST", url, json=payload)
        if res.status_code not in (200, 201):
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def create_closed_tour_option(self, supplier_id: str, closed_tour_code: str, payload: dict) -> Dict[str, Any]:
        """Executes POST /closedtour/{supplierId}/{closedTourCode} — pushes modality/pricing option."""
        url = f"{self.api_base_url}/closedtour/{supplier_id}/{closed_tour_code}"
        res = self._request("POST", url, json=payload)
        if res.status_code not in (200, 201):
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def update_closed_tour(self, supplier_id: str, payload: dict) -> Dict[str, Any]:
        """
        Executes PUT /closedtour/{supplierId} — updates an EXISTING tour's
        details (name, description, itinerary, etc). The payload's 'code'
        field identifies which existing tour to update. Use create_closed_tour
        (POST) instead when creating a brand-new tour.
        """
        url = f"{self.api_base_url}/closedtour/{supplier_id}"
        res = self._request("PUT", url, json=payload)
        if res.status_code not in (200, 201):
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def update_closed_tour_option(self, supplier_id: str, closed_tour_code: str, payload: dict) -> Dict[str, Any]:
        """
        Executes PUT /closedtour/{supplierId}/{closedTourCode} — updates an
        EXISTING option (pricing, operational days, etc). The payload's
        'code' field identifies which existing option to update. Use
        create_closed_tour_option (POST) instead to add a brand-new option.
        """
        url = f"{self.api_base_url}/closedtour/{supplier_id}/{closed_tour_code}"
        res = self._request("PUT", url, json=payload)
        if res.status_code not in (200, 201):
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    # ------------------------------------------------------------------
    # TICKET UPLOADS (excursions - single destination, no overnight)
    # Confirmed against real Swagger + live GET examples.
    # ------------------------------------------------------------------
    def get_tickets(self, supplier_id: str, first: int = 0, limit: int = 50) -> Dict[str, Any]:
        """Executes GET /tickets/{supplierId} — returns paginated list of tickets for this supplier."""
        url = f"{self.api_base_url}/tickets/{supplier_id}"
        merged_headers = {**self.get_headers(), "first": str(first), "limit": str(limit)}
        res = requests.request("GET", url, headers=merged_headers, timeout=15)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def get_ticket(self, supplier_id: str, ticket_code: str) -> Dict[str, Any]:
        """Executes GET /tickets/{supplierId}/{ticketCode} — returns the full existing ticket."""
        url = f"{self.api_base_url}/tickets/{supplier_id}/{ticket_code}"
        res = self._request("GET", url)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def get_ticket_option(self, supplier_id: str, ticket_code: str, option_code: str) -> Dict[str, Any]:
        """Executes GET /tickets/{supplierId}/{ticketCode}/{optionCode} — returns a specific ticket modality."""
        url = f"{self.api_base_url}/tickets/{supplier_id}/{ticket_code}/{option_code}"
        res = self._request("GET", url)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def create_ticket(self, supplier_id: str, payload: dict) -> Dict[str, Any]:
        """Executes POST /tickets/{supplierId} — creates a new ticket."""
        url = f"{self.api_base_url}/tickets/{supplier_id}"
        res = self._request("POST", url, json=payload)
        if res.status_code not in (200, 201):
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def create_ticket_option(self, supplier_id: str, ticket_code: str, payload: dict) -> Dict[str, Any]:
        """Executes POST /tickets/{supplierId}/{ticketCode} — creates a new ticket option/modality."""
        url = f"{self.api_base_url}/tickets/{supplier_id}/{ticket_code}"
        res = self._request("POST", url, json=payload)
        if res.status_code not in (200, 201):
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def update_ticket(self, supplier_id: str, payload: dict) -> Dict[str, Any]:
        """Executes PUT /tickets/{supplierId} — updates an EXISTING ticket's details."""
        url = f"{self.api_base_url}/tickets/{supplier_id}"
        res = self._request("PUT", url, json=payload)
        if res.status_code not in (200, 201):
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def update_ticket_option(self, supplier_id: str, ticket_code: str, payload: dict) -> Dict[str, Any]:
        """Executes PUT /tickets/{supplierId}/{ticketCode} — updates an EXISTING ticket option/modality."""
        url = f"{self.api_base_url}/tickets/{supplier_id}/{ticket_code}"
        res = self._request("PUT", url, json=payload)
        if res.status_code not in (200, 201):
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    # ==================================================================
    # TRANSLATION-SYNC ADDITIONS (nbext) — everything below is new.
    # Same pattern as the Ticket/ClosedTour methods above: GET the full
    # entity, PUT the full entity back with only the translation-target
    # fields changed. Endpoint paths confirmed against the live Swagger
    # at momira.travel/api (Contract - Transfer / Transport / Hotel,
    # and Packages).
    # ==================================================================

    # ---- Transfers ----------------------------------------------------
    def get_transfers(self, supplier_id: str) -> Dict[str, Any]:
        """Executes GET /transfer/{supplierId} — returns {"transfer": [...]} for this supplier."""
        url = f"{self.api_base_url}/transfer/{supplier_id}"
        res = self._request("GET", url)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def get_transfer(self, supplier_id: str, transfer_id: str) -> Dict[str, Any]:
        """Executes GET /transfer/{supplierId}/{transferId} — returns the full existing transfer."""
        url = f"{self.api_base_url}/transfer/{supplier_id}/{transfer_id}"
        res = self._request("GET", url)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def update_transfer(self, supplier_id: str, payload: dict) -> Dict[str, Any]:
        """Executes PUT /transfer/{supplierId} — updates an EXISTING transfer's details."""
        url = f"{self.api_base_url}/transfer/{supplier_id}"
        res = self._request("PUT", url, json=payload)
        if res.status_code not in (200, 201):
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    # ---- Transport ------------------------------------------------------
    def get_transports(self, supplier_id: str) -> Dict[str, Any]:
        """Executes GET /transport/{supplierId} — returns {"transport": [...]} for this supplier."""
        url = f"{self.api_base_url}/transport/{supplier_id}"
        res = self._request("GET", url)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def get_transport(self, supplier_id: str, transport_id: str) -> Dict[str, Any]:
        """Executes GET /transport/{supplierId}/{transportId} — returns the full existing transport."""
        url = f"{self.api_base_url}/transport/{supplier_id}/{transport_id}"
        res = self._request("GET", url)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def get_transport_option(self, supplier_id: str, transport_id: str, option_code: str) -> Dict[str, Any]:
        """Executes GET /transport/{supplierId}/{transportId}/{optionCode} — returns a specific transport option."""
        url = f"{self.api_base_url}/transport/{supplier_id}/{transport_id}/{option_code}"
        res = self._request("GET", url)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def update_transport(self, supplier_id: str, payload: dict) -> Dict[str, Any]:
        """Executes PUT /transport/{supplierId} — updates an EXISTING transport's details."""
        url = f"{self.api_base_url}/transport/{supplier_id}"
        res = self._request("PUT", url, json=payload)
        if res.status_code not in (200, 201):
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def update_transport_option(self, supplier_id: str, transport_id: str, payload: dict) -> Dict[str, Any]:
        """Executes PUT /transport/{supplierId}/{transportId} — updates an EXISTING transport option."""
        url = f"{self.api_base_url}/transport/{supplier_id}/{transport_id}"
        res = self._request("PUT", url, json=payload)
        if res.status_code not in (200, 201):
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    # ---- Hotels ---------------------------------------------------------
    def get_hotels(self, supplier_id: str) -> Dict[str, Any]:
        """Executes GET /hotel/{supplierId} — returns {"hotel": [...]} for this supplier."""
        url = f"{self.api_base_url}/hotel/{supplier_id}"
        res = self._request("GET", url)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def get_hotel(self, supplier_id: str, provider_code: str) -> Dict[str, Any]:
        """Executes GET /hotel/{supplierId}/{providerCode} — returns the full existing hotel contract."""
        url = f"{self.api_base_url}/hotel/{supplier_id}/{provider_code}"
        res = self._request("GET", url)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def update_hotel(self, supplier_id: str, payload: dict) -> Dict[str, Any]:
        """Executes PUT /hotel/{supplierId} — updates an EXISTING hotel contract's details."""
        url = f"{self.api_base_url}/hotel/{supplier_id}"
        res = self._request("PUT", url, json=payload)
        if res.status_code not in (200, 201):
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    # ---- Holiday Packages / Ideas (lang-parameter pattern) --------------
    def get_holiday_package_info(self, microsite_id: str, holiday_package_id: str, lang: str = "EN") -> Dict[str, Any]:
        """Executes GET /package/{micrositeId}/info/{holidayPackageId}?lang= — flat title/description for ONE language."""
        url = f"{self.api_base_url}/package/{microsite_id}/info/{holiday_package_id}"
        res = self._request("GET", url, params={"lang": lang})
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()

    def update_holiday_package(self, microsite_id: str, holiday_package_id: str, payload: dict, lang: str = "EN") -> Dict[str, Any]:
        """
        Executes PUT /package/{micrositeId}/{holidayPackageId}?lang= — writes
        ONE language's title/description/remarks.

        On failure, the returned dict includes a "request_response_log" block
        with the full request (method, exact URL incl. query string, headers
        with auth-token masked, JSON body) and full response (status code,
        headers, body) — formatted so you can paste it directly into a
        Travel Compositor support ticket when they ask for RQ/RS logs.
        """
        url = f"{self.api_base_url}/package/{microsite_id}/{holiday_package_id}"
        res = self._request("PUT", url, params={"lang": lang}, json=payload)
        if res.status_code not in (200, 201):
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            req = res.request
            return {
                "error": res.status_code,
                "message": res.text,
                "request_response_log": {
                    "request": {
                        "method": req.method if req is not None else "PUT",
                        "url": req.url if req is not None else f"{url}?lang={lang}",
                        "headers": _mask_auth_token_header(req.headers if req is not None else self.get_headers()),
                        "body": payload,
                    },
                    "response": {
                        "status_code": res.status_code,
                        "headers": dict(res.headers),
                        "body": _try_parse_json(res.text),
                    },
                },
            }
        return res.json()

    def get_holiday_packages(self, microsite_id: str, lang: str = "EN", **filters) -> Dict[str, Any]:
        """Executes GET /package/{micrositeId} — returns {"pagination":..., "package": [...]}, filters passed through as query params."""
        url = f"{self.api_base_url}/package/{microsite_id}"
        params = {"lang": lang, **filters}
        res = self._request("GET", url, params=params)
        if res.status_code != 200:
            print(f"\n❌ API Error ({res.status_code}):\n{res.text}")
            return {"error": res.status_code, "message": res.text}
        return res.json()
