import os
import re
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

ATTIO_API_KEY   = os.getenv("ATTIO_API_KEY")
BASE            = "https://api.attio.com/v2"
LIST_SLUG       = "sales_pipeline"
PEOPLE_OBJECT   = "people"


def _h():
    return {
        "Authorization": f"Bearer {ATTIO_API_KEY}",
        "Content-Type": "application/json",
    }


def get_existing_domains() -> list:
    """Return website domains of coaches already in the Sales Pipeline list."""
    domains = []
    offset  = 0

    while True:
        resp = requests.post(
            f"{BASE}/lists/{LIST_SLUG}/entries/query",
            headers=_h(),
            json={"limit": 500, "offset": offset},
        )
        if not resp.ok:
            break

        data = resp.json().get("data", [])
        if not data:
            break

        for entry in data:
            record_id = entry.get("parent_record_id")
            if not record_id or not isinstance(record_id, str):
                continue

            person_resp = requests.get(
                f"{BASE}/objects/{PEOPLE_OBJECT}/records/{record_id}",
                headers=_h(),
            )
            if not person_resp.ok:
                continue

            values = person_resp.json().get("data", {}).get("values", {})
            for company_ref in values.get("company", []):
                company_id = company_ref.get("target_record_id")
                if not company_id:
                    continue
                co_resp = requests.get(
                    f"{BASE}/objects/companies/records/{company_id}",
                    headers=_h(),
                )
                if not co_resp.ok:
                    continue
                for d in co_resp.json().get("data", {}).get("values", {}).get("domains", []):
                    domain = d.get("domain", "")
                    if domain:
                        domains.append(domain)

        if len(data) < 500:
            break
        offset += 500

    return domains


def _name_value(full_name: str) -> list:
    parts = full_name.strip().rsplit(" ", 1)
    first = parts[0] if len(parts) == 2 else full_name
    last  = parts[1] if len(parts) == 2 else ""
    return [{"first_name": first, "last_name": last, "full_name": full_name.strip()}]


def _upsert_company(name: str, website: str):
    """Upsert a Company by domain and return its record_id."""
    domain = urlparse(website).netloc.replace("www.", "")
    if not domain:
        return None

    resp = requests.put(
        f"{BASE}/objects/companies/records",
        headers=_h(),
        params={"matching_attribute": "domains"},
        json={"data": {"values": {
            "name":    [{"value": name}],
            "domains": [{"domain": domain}],
        }}},
    )
    if not resp.ok:
        return None
    return resp.json().get("data", {}).get("id", {}).get("record_id")


def push_coach(coach: dict, message: str, subject: str = "", research_notes: str = "") -> bool:
    """Upsert Person in Attio, link to Company via website domain, add to Sales Pipeline list."""
    values = {"name": _name_value(coach["name"])}

    if coach.get("email"):
        values["email_addresses"] = [{"email_address": coach["email"]}]
    if coach.get("phone") and len(re.sub(r'\D', '', coach["phone"])) >= 10:
        values["phone_numbers"] = [{"original_phone_number": coach["phone"]}]
    if coach.get("instagram_url"):
        values["instagram"] = [{"value": coach["instagram_url"]}]
    if coach.get("facebook_url"):
        values["facebook"] = [{"value": coach["facebook_url"]}]

    desc_parts = []
    if coach.get("entity_type"):
        desc_parts.append(f"Type: {coach['entity_type']}")
    if coach.get("athlete_count_signal"):
        desc_parts.append(f"Athletes: {coach['athlete_count_signal']}")
    if coach.get("tools_mentioned"):
        tools = coach["tools_mentioned"]
        desc_parts.append(f"Tools: {', '.join(tools) if isinstance(tools, list) else tools}")
    if subject:
        desc_parts.append(f"Subject: {subject}")
    if research_notes:
        desc_parts.append(f"Notes: {research_notes}")
    if desc_parts:
        values["description"] = [{"value": "\n".join(desc_parts)}]

    # Link to Company via website domain (use club name if extracted from a club page)
    if coach.get("website"):
        company_name = coach.get("club_name") or coach["name"]
        company_id = _upsert_company(company_name, coach["website"])
        if company_id:
            values["company"] = [{"target_object": "companies", "target_record_id": company_id}]

    if coach.get("email"):
        resp = requests.put(
            f"{BASE}/objects/{PEOPLE_OBJECT}/records",
            headers=_h(),
            params={"matching_attribute": "email_addresses"},
            json={"data": {"values": values}},
        )
    else:
        resp = requests.post(
            f"{BASE}/objects/{PEOPLE_OBJECT}/records",
            headers=_h(),
            json={"data": {"values": values}},
        )
    if not resp.ok:
        print(f"  Attio record failed for {coach['name']}: {resp.status_code} {resp.text[:200]}")
        return False

    record_id = resp.json().get("data", {}).get("id", {}).get("record_id")
    if not record_id:
        return False

    entry_resp = requests.post(
        f"{BASE}/lists/{LIST_SLUG}/entries",
        headers=_h(),
        json={
            "data": {
                "parent_record_id": record_id,
                "parent_object":    PEOPLE_OBJECT,
                "entry_values": {
                    "draft_message":   [{"value": message}],
                    "draft_message_5": [{"value": message}],
                    **( {"country": [{"country_code": coach["country_code"],
                                      "line_1": None, "line_2": None, "line_3": None,
                                      "line_4": None, "locality": None, "region": None,
                                      "postcode": None, "latitude": None, "longitude": None}]}
                        if coach.get("country_code") else {} ),
                },
            }
        },
    )
    if not entry_resp.ok:
        print(f"  Attio list entry failed for {coach['name']}: {entry_resp.status_code} {entry_resp.text[:200]}")
        return False

    return True
