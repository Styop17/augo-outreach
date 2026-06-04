"""
augo Coach Outreach Pipeline
Finds coaches via Exa Websets, researches each one, drafts a personalised
email using Gemini, and pushes everything to Attio + a local CSV.

Usage:
    python pipeline.py --query "triathlon coaches in Amsterdam" --limit 20
"""

import os
import csv
import json
import sys
from datetime import datetime
from dotenv import load_dotenv
import anthropic
import requests

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

EXA_API_KEY       = os.getenv("EXA_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ATTIO_API_KEY     = os.getenv("ATTIO_API_KEY")

SENDER_NAME = "Bruna"
SENDER_ROLE = "co-founder of augo"
AUGO_PITCH  = (
    "augo is an intelligent assistant for endurance coaches that brings "
    "athlete communication, session data, and training feedback into one place — "
    "so coaches spend less time searching through messages and more time coaching."
)


SEEN_FILE = "seen.json"

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

def seen_key(name: str, email: str) -> str:
    return email.lower() if email else name.lower()


# ── Phase 1: Discovery ─────────────────────────────────────────────────────────

def find_coaches(query: str, limit: int):
    from exa_py import Exa
    exa = Exa(EXA_API_KEY)

    print(f"\n[1/4] Searching for coaches...")
    print(f"      Query: {query}\n")

    results = exa.search(
        query,
        num_results=limit,
        contents={"text": True},
        exclude_domains=[
            "instagram.com", "strava.com", "linkedin.com",
            "facebook.com", "twitter.com", "youtube.com",
            "trainingpeaks.com", "tiktok.com",
        ],
    )

    print(f"      ✓ Found {len(results.results)} pages\n")
    return results.results


# ── Phase 2: Research ──────────────────────────────────────────────────────────

def extract_coach_info(page_text: str, page_url: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    text_sample = page_text[:2000] + ("\n...\n" + page_text[-500:] if len(page_text) > 2000 else "")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"""
Read this webpage content and extract coach information.

URL: {page_url}
CONTENT: {text_sample}

Return ONLY valid JSON, no markdown:
{{
  "is_coach": true or false,
  "name": "full name or empty string",
  "email": "email address or empty string",
  "website": "website URL or empty string",
  "sport": "triathlon, running, cycling, or mixed endurance",
  "city": "city and country or empty string"
}}

Only set is_coach to true if this page is clearly about a personal endurance coach who coaches individual athletes.
"""}]
    )
    text = response.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def research_coach(page, seen: set) -> dict:
    page_text = page.text or ""
    page_url  = page.url or ""

    info = extract_coach_info(page_text, page_url)

    if not info.get("is_coach") or not info.get("name"):
        return {}

    name    = info.get("name", "")
    email   = info.get("email", "")
    website = info.get("website", page_url)
    sport   = info.get("sport", "endurance sports")
    city    = info.get("city", "")

    if seen_key(name, email) in seen:
        print(f"      Skipping {name} — already processed in a previous run")
        return {}

    return {
        "name":         name,
        "email":        email,
        "website":      website,
        "sport":        sport,
        "city":         city,
        "website_text": page_text[:3000],
    }


# ── Phase 3: Email Drafting ────────────────────────────────────────────────────

def draft_email(coach: dict) -> dict:
    prompt = f"""
You are writing a cold outreach email on behalf of {SENDER_NAME}, {SENDER_ROLE}.

{AUGO_PITCH}

COACH DETAILS:
Name: {coach['name']}
Sport: {coach['sport']}
Location: {coach['city']}

WEBSITE CONTENT:
{coach['website_text'][:2000] if coach['website_text'] else 'No website content available.'}

INSTRUCTIONS:
1. Read the website content carefully
2. Pick the single best personalisation hook from the website — ranked by priority:
   athlete_achievement > their_race > content_published > coaching_philosophy > fallback (generic)
3. Write a short, direct email from {SENDER_NAME}

EMAIL RULES:
- Subject: 4–6 words maximum, reference the hook directly (e.g. a name, a result, a place), never generic, no fluff
- Opening: one sentence directly referencing the hook
- Who I am: "I'm {SENDER_NAME}, {SENDER_ROLE}" + one-liner about augo
- Why them: one sentence tying back to the hook
- CTA: ask for 30 minutes to learn about their coaching workflow
- Sign-off: "{SENDER_NAME}" only — nothing else
- Body length: under 150 words
- Tone: peer-to-peer — a coach writing to a coach, not a sales pitch

WRITING RULES — STRICT:
- Never use an em dash (—) anywhere in the email
- Never use words like: seamlessly, leverage, revolutionize, game-changer, cutting-edge, innovative, transform, elevate, streamline, supercharge, unlock, empower
- Write like a real person texting a colleague — simple words, short sentences
- No buzzwords, no corporate language, no AI-sounding phrases
- If you want to connect two thoughts, use a full stop and start a new sentence instead of a dash

Return ONLY valid JSON, no markdown, no explanation:
{{
  "hook_type": "athlete_achievement | their_race | content_published | fallback",
  "hook_text": "the specific hook sentence used in the opening",
  "hook_source_url": "the url where the hook was found, or empty string if fallback",
  "research_notes": "3-line summary: who they are, what they coach, what makes them distinctive",
  "email_subject": "the subject line",
  "email_body": "the full email body"
}}
"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    text = response.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    return json.loads(text)


# ── Phase 4: Push to Attio ─────────────────────────────────────────────────────

def push_to_attio(coach: dict, draft: dict) -> bool:
    if not ATTIO_API_KEY:
        return False

    headers = {
        "Authorization": f"Bearer {ATTIO_API_KEY}",
        "Content-Type": "application/json",
    }

    name_parts = coach["name"].split(" ", 1)
    first_name = name_parts[0]
    last_name  = name_parts[1] if len(name_parts) > 1 else ""

    values = {
        "name":             [{"first_name": first_name, "last_name": last_name}],
        "outreach_status":  [{"value": "draft"}],
        "sport":            [{"value": coach["sport"]}],
        "hook_type":        [{"value": draft["hook_type"]}],
        "hook_text":        [{"value": draft["hook_text"]}],
        "hook_source_url":  [{"value": draft["hook_source_url"]}],
        "research_notes":   [{"value": draft["research_notes"]}],
        "email_subject":    [{"value": draft["email_subject"]}],
        "email_draft":      [{"value": draft["email_body"]}],
    }

    if coach["email"]:
        values["email_addresses"] = [{"email_address": coach["email"]}]

    response = requests.put(
        "https://api.attio.com/v2/objects/people/records",
        headers=headers,
        json={"data": {"values": values}},
    )

    return response.status_code in (200, 201)


# ── Main ───────────────────────────────────────────────────────────────────────

FAKE_COACHES = [
    {
        "name":         "Jan de Vries",
        "email":        "jan@jancoaching.nl",
        "website":      "https://jancoaching.nl",
        "sport":        "Triathlon",
        "city":         "Amsterdam, Netherlands",
        "website_text": (
            "Jan de Vries is a triathlon coach based in Amsterdam with over 10 years of experience. "
            "He coaches around 15 athletes individually, focusing on Ironman 70.3 distance. "
            "Jan uses TrainingPeaks and Garmin to track athlete data and provides weekly feedback sessions. "
            "His philosophy is built around consistency and data-driven training. "
            "Former competitive swimmer who transitioned to triathlon in 2012."
        ),
        "hook_results": [
            {
                "type": "general",
                "snippets": [
                    "Jan de Vries athlete Emma Bakker finished Ironman 70.3 Maastricht in 4:32, a new personal best. "
                    "Coach Jan has been working with Emma for two years focusing on race-day execution."
                ],
                "urls": ["https://results.ironman.com/maastricht-2026"],
            }
        ],
    }
]


def main():
    print("\n=== augo Coach Outreach Pipeline ===\n")

    test_mode     = "--test"        in sys.argv
    fetch_mode    = "--fetch"       in sys.argv
    from_file     = "--from-file"   in sys.argv
    test_attio    = "--test-attio"  in sys.argv

    seen = load_seen()

    # ── ATTIO TEST MODE: push one fake coach to Attio ─────────────────────────
    if test_attio:
        print("Testing Attio connection — pushing one fake coach...\n")
        fake_coach = FAKE_COACHES[0]
        fake_draft = {
            "hook_type":        "athlete_achievement",
            "hook_text":        "Emma Bakker just finished Ironman 70.3 Maastricht in 4:32",
            "hook_source_url":  "https://results.ironman.com/maastricht-2026",
            "research_notes":   "Jan coaches ~15 triathletes in Amsterdam. Uses TrainingPeaks. Focuses on Ironman 70.3.",
            "email_subject":    "Emma's 4:32 at Maastricht",
            "email_body":       "Saw Emma just finished Ironman 70.3 in 4:32 under your coaching. That's a solid result.\n\nI'm Bruna, co-founder of augo. We help endurance coaches keep athlete communication, session data and feedback in one place.\n\nWould you have 30 minutes to chat about how you currently manage your athletes?\n\nBruna",
        }
        ok = push_to_attio(fake_coach, fake_draft)
        if ok:
            print("✓ Success — check your Attio Sales Pipeline list for Jan de Vries")
        else:
            print("✗ Failed — check your ATTIO_API_KEY in .env and that the custom fields are set up in Attio")
        return

    # ── TEST MODE: fake data, Anthropic only ───────────────────────────────────
    if test_mode:
        print("Running in TEST MODE — Exa is skipped, using fake coach data.\n")
        coaches_data = FAKE_COACHES
        query = "test"

    # ── FETCH MODE: run Exa only, save to coaches.json ────────────────────────
    elif fetch_mode:
        query = input("What do you want to search for?\n> ").strip()
        while not query:
            query = input("Please enter a search query\n> ").strip()

        limit_input = input("\nHow many coaches? (max 20, default: 5)\n> ").strip()
        try:
            limit = min(int(limit_input), 20) if limit_input else 5
        except ValueError:
            limit = 5

        print(f"\nSearching for: {query}")
        print(f"Target count:  {limit} coaches\n")

        raw_coaches = find_coaches(query, limit)
        coaches_data = [research_coach(c, seen) for c in raw_coaches]
        coaches_data = [c for c in coaches_data if c]

        with open("coaches.json", "w") as f:
            json.dump(coaches_data, f, indent=2)

        print(f"\n✓ {len(coaches_data)} coaches saved to coaches.json")
        print(f"  Run with --from-file to draft emails without calling Exa again.")
        return

    # ── FROM FILE MODE: load coaches.json, draft only ─────────────────────────
    elif from_file:
        if not os.path.exists("coaches.json"):
            print("coaches.json not found — run with --fetch first.")
            return
        with open("coaches.json") as f:
            coaches_data = json.load(f)
        query = "from_file"
        print(f"Loaded {len(coaches_data)} coaches from coaches.json\n")

    # ── FULL MODE: Exa + research + draft + Attio ─────────────────────────────
    else:
        query = input("What do you want to search for?\n> ").strip()
        while not query:
            query = input("Please enter a search query\n> ").strip()

        limit_input = input("\nHow many coaches? (max 20, default: 10)\n> ").strip()
        try:
            limit = min(int(limit_input), 20) if limit_input else 10
        except ValueError:
            limit = 10

        print(f"\nSearching for: {query}")
        print(f"Target count:  {limit} coaches")

        raw_coaches = find_coaches(query, limit)
        coaches_data = [research_coach(c, seen) for c in raw_coaches]
        coaches_data = [c for c in coaches_data if c]

    import re
    slug        = re.sub(r"[^\w\s-]", "", query.lower()).strip()
    slug        = re.sub(r"[\s]+", "_", slug)
    output_file = f"{slug}.csv"

    results = []
    no_email_count = 0

    print(f"Drafting emails...\n")

    for i, coach_data in enumerate(coaches_data, 1):
        print(f"  [{i}/{len(coaches_data)}] ", end="", flush=True)

        if not coach_data["name"]:
            print("Skipped — no name found")
            continue

        print(f"{coach_data['name']}...", end=" ", flush=True)

        try:
            draft = draft_email(coach_data)
        except Exception as e:
            print(f"✗ Drafting failed: {e}")
            continue

        if test_mode or from_file:
            print(f"✓ Draft complete\n")
            print(f"  Hook type:  {draft['hook_type']}")
            print(f"  Hook:       {draft['hook_text']}")
            print(f"  Subject:    {draft['email_subject']}")
            print(f"\n--- EMAIL DRAFT ---\n")
            print(draft["email_body"])
            print(f"\n-------------------\n")
        else:
            attio_ok = push_to_attio(coach_data, draft)
            if not coach_data["email"]:
                no_email_count += 1
            status = "✓" if attio_ok else "✓ (Attio skipped — check key)"
            print(f"{status} | {draft['hook_type']} | \"{draft['email_subject']}\"")

        seen.add(seen_key(coach_data["name"], coach_data["email"]))
        save_seen(seen)

        results.append({
            "name":            coach_data["name"],
            "email":           coach_data["email"],
            "website":         coach_data["website"],
            "sport":           coach_data["sport"],
            "location":        coach_data["city"],
            "research_notes":  draft["research_notes"],
            "email_subject":   draft["email_subject"],
            "email_body":      draft["email_body"],
        })

    # Save CSV
    if results:
        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)

    if test_mode or from_file:
        print(f"\nDone — {len(results)} draft(s) generated.")
    else:
        print(f"\n[4/4] Done!")
        print(f"      {len(results)} coaches processed")
        print(f"      {no_email_count} flagged — no public email found (LinkedIn outreach instead)")
        print(f"      CSV saved: {output_file}")
        if ATTIO_API_KEY:
            print(f"      Attio: check your Sales Pipeline list")


if __name__ == "__main__":
    main()
