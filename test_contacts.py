import csv
import requests
from pipeline import find_contact_info

CSV_FILE = "5_running_coaches_in_bern_switzerland.csv"

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

with open(CSV_FILE, newline="", encoding="utf-8") as f:
    coaches = list(csv.DictReader(f))

for coach in coaches:
    name    = coach.get("name", "")
    website = coach.get("website", "")

    print(f"\n{name}")
    print(f"  Website: {website}")

    if not website:
        print("  No website")
        continue

    try:
        resp = requests.get(website, headers=headers, timeout=10)
        page_text = resp.text
    except Exception as e:
        print(f"  Could not fetch: {e}")
        continue

    contact = find_contact_info(page_text, website)

    print(f"  Channel:   {contact['channel']}")
    if contact["instagram_url"]: print(f"  Instagram: {contact['instagram_url']}")
    if contact["linkedin_url"]:  print(f"  LinkedIn:  {contact['linkedin_url']}")
    if contact["facebook_url"]:  print(f"  Facebook:  {contact['facebook_url']}")
    if contact["tiktok_url"]:    print(f"  TikTok:    {contact['tiktok_url']}")
    if contact["phone"]:         print(f"  Phone:     {contact['phone']}")
    if contact["email"]:         print(f"  Email:     {contact['email']}")
