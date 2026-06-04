"""
augo Coach Outreach Pipeline
Finds endurance coaches via Exa, drafts personalised outreach messages using Claude,
and saves everything to a CSV.

Usage:
    python pipeline.py              — full run (Exa + Claude)
    python pipeline.py --fetch      — Exa only, saves coaches.json
    python pipeline.py --from-file  — Claude only, loads from coaches.json
    python pipeline.py --test       — fake data, no API calls except Claude
"""

import os
import re
import csv
import json
import sys
import requests
from dotenv import load_dotenv
import anthropic

load_dotenv()

# ── Colors ─────────────────────────────────────────────────────────────────────

GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

CHANNEL_COLOR = {
    "instagram":        GREEN,
    "email":            GREEN,
    "phone":            YELLOW,
    "facebook":         YELLOW,
    "no_contact_found": RED,
}

# ── Config ─────────────────────────────────────────────────────────────────────

EXA_API_KEY       = os.getenv("EXA_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

SENDER_NAME = "Bruna"
SENDER_ROLE = "co-founder of augo"
AUGO_PITCH  = (
    "augo is an intelligent assistant for endurance coaches that brings "
    "athlete communication, session data, and training feedback into one place — "
    "so coaches spend less time searching through messages and more time coaching."
)

SEEN_FILE    = "seen.json"
SKIP_HANDLES = {"p", "explore", "accounts", "stories", "reel", "reels", "tv",
                "sharer", "share", "pages", "groups"}


# ── Seen / Deduplication ───────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

def seen_key(name: str, email: str) -> str:
    if email:
        return email.lower()
    tokens = name.lower().split()
    return " ".join(tokens[:2]) if len(tokens) >= 2 else name.lower()


# ── Phase 1: Discovery ─────────────────────────────────────────────────────────

def find_coaches(query: str, limit: int):
    from exa_py import Exa
    exa = Exa(EXA_API_KEY)

    print(f"\nSearching for coaches...")
    print(f"  Query: {query}\n")

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

    print(f"  Found {len(results.results)} pages\n")
    return results.results


# ── Phase 2: Contact Finding ───────────────────────────────────────────────────

def find_contact_info(page_text: str, website_url: str) -> dict:
    req_headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

    def first_match(pattern, text):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1) if m else None

    # Fetch raw HTML to catch social icon links missed by Exa's text extraction
    raw_html = ""
    if website_url:
        try:
            raw_html = requests.get(website_url, headers=req_headers, timeout=8).text
        except Exception:
            pass

    search_text = page_text + "\n" + raw_html

    # Instagram — scan page first, then guess from domain
    instagram_url = ""
    handle = first_match(r'instagram\.com/([\w.]+)/?', search_text)
    if handle and handle.lower() not in SKIP_HANDLES:
        instagram_url = f"https://instagram.com/{handle}"

    if not instagram_url and website_url:
        from urllib.parse import urlparse
        domain = urlparse(website_url).netloc.lower().replace("www.", "")
        guess  = domain.split(".")[0]
        if guess and len(guess) > 2:
            try:
                resp = requests.get(f"https://www.instagram.com/{guess}/",
                                    headers=req_headers, timeout=6)
                if resp.status_code == 200 and "Page Not Found" not in resp.text:
                    instagram_url = f"https://instagram.com/{guess}"
            except Exception:
                pass

    # Facebook
    facebook_url = ""
    h = first_match(r'facebook\.com/([\w.]+)/?', search_text)
    if h and h.lower() not in SKIP_HANDLES and len(h) >= 5:
        facebook_url = f"https://facebook.com/{h}"

    # Phone — international format only
    phone = ""
    m = re.search(r'\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{3,5}[\s\-]?\d{3,5}', search_text)
    if m:
        phone = m.group(0).strip()

    # Email — skip image filenames and generic addresses
    email = ""
    image_exts = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp"}
    skip_locals = {"noreply", "support", "info", "hello", "contact", "admin", "mail"}
    for m in re.finditer(r'[\w.\-+]+@[\w.\-]+\.[a-z]{2,}', search_text, re.IGNORECASE):
        candidate = m.group(0)
        if candidate.rsplit(".", 1)[-1].lower() in image_exts:
            continue
        if candidate.split("@")[0].lower() in skip_locals:
            continue
        email = candidate
        break

    # Channel priority: instagram > phone > email > facebook
    if instagram_url:
        channel = "instagram"
    elif phone:
        channel = "phone"
    elif email:
        channel = "email"
    elif facebook_url:
        channel = "facebook"
    else:
        channel = "no_contact_found"

    return {
        "channel":       channel,
        "instagram_url": instagram_url,
        "facebook_url":  facebook_url,
        "phone":         phone,
        "email":         email,
    }


# ── Phase 3: Research ──────────────────────────────────────────────────────────

def extract_coach_info(page_text: str, page_url: str) -> dict:
    client      = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
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
  "website": "website URL or empty string"
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
    parsed = json.loads(text)
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else {}
    return parsed


def research_coach(page, seen: set) -> dict:
    page_text = page.text or ""
    page_url  = page.url or ""

    info = extract_coach_info(page_text, page_url)

    if not info.get("is_coach") or not info.get("name"):
        return {}

    name    = info.get("name", "")
    website = info.get("website", page_url)

    if seen_key(name, "") in seen:
        print(f"  Skipping {name} — already processed in a previous run")
        return {}

    contact = find_contact_info(page_text, website)

    return {
        "name":          name,
        "website":       website,
        "website_text":  page_text[:3000],
        "channel":       contact["channel"],
        "instagram_url": contact["instagram_url"],
        "facebook_url":  contact["facebook_url"],
        "phone":         contact["phone"],
        "email":         contact["email"],
    }


# ── Phase 4: Message Drafting ──────────────────────────────────────────────────

WRITING_RULES = """
WRITING RULES — STRICT:
- Never use an em dash (—) anywhere
- Never start with "Noticed", "I noticed", "I came across", "I stumbled upon"
- Never use: seamlessly, leverage, revolutionize, game-changer, cutting-edge, innovative,
  transform, elevate, streamline, coaches like you, stands out, empower, unlock,
  data points, juggling, valuable, insights, holistic, journey, dedicated, passionate
- No corporate language, no AI-sounding phrases
- Write like a real person, not a marketer
"""

def _call_claude(prompt: str) -> str:
    client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text

def _clean(text: str) -> str:
    return text.replace("—", "-").replace("–", "-")

def draft_dm(coach: dict) -> dict:
    prompt = f"""
You are writing a cold Instagram DM on behalf of {SENDER_NAME}, {SENDER_ROLE}.

{AUGO_PITCH}

COACH DETAILS:
Name: {coach['name']}

WEBSITE CONTENT:
{coach['website_text'][:2000] if coach['website_text'] else 'No website content available.'}

INSTRUCTIONS:
1. Read the website content carefully
2. Pick one specific, concrete detail — a real result, athlete name, race, place, or decision the coach made
3. Write a 3-sentence Instagram DM from {SENDER_NAME}

DM RULES:
- Sentence 1: a genuine observation using the specific detail. Must reference something concrete (name, place, number, event). NOT a compliment.
- Sentence 2: who you are + what augo does in plain words. One short sentence.
- Sentence 3: a casual CTA — "up for a quick chat?" or "would love to hear how you manage it"
- No sign-off. 3 sentences only.

BAD: "Noticed you're managing 15 athletes focused on 70.3. I'm Bruna, we help coaches bring everything into one place. Up for a quick chat?"
GOOD: "Saw your athlete Emma ran a 4:32 at Maastricht 70.3 last month. I'm Bruna, I'm building a tool to help coaches keep athlete messages and training data in one spot. Would love to hear how you currently manage it all."

{WRITING_RULES}

Return ONLY valid JSON, no markdown:
{{
  "hook_type": "athlete_achievement | their_race | content_published | coaching_philosophy | fallback",
  "hook_text": "the specific opening sentence used",
  "research_notes": "2-line summary: who they are and what makes them distinctive",
  "dm_message": "the full DM text"
}}
"""
    result = json.loads(_call_claude(prompt))
    if isinstance(result, list):
        result = result[0]
    result["dm_message"] = _clean(result["dm_message"])
    return result


def draft_email(coach: dict) -> dict:
    prompt = f"""
You are writing a cold outreach email on behalf of {SENDER_NAME}, {SENDER_ROLE}.

{AUGO_PITCH}

COACH DETAILS:
Name: {coach['name']}

WEBSITE CONTENT:
{coach['website_text'][:2000] if coach['website_text'] else 'No website content available.'}

INSTRUCTIONS:
1. Read the website content carefully
2. Pick one specific, concrete detail — a real result, athlete name, race, place, or decision the coach made
3. Write a short cold email from {SENDER_NAME}

EMAIL RULES:
- Subject: 4-6 words, reference something specific
- Opening: one sentence using the specific detail
- Who I am: "{SENDER_NAME}, co-founder of augo" + what augo does in plain words
- CTA: ask for 30 minutes to learn about their coaching workflow
- Sign-off: "{SENDER_NAME}" only
- Body: under 120 words
- Tone: peer-to-peer, not a sales pitch

{WRITING_RULES}

Return ONLY valid JSON, no markdown:
{{
  "hook_type": "athlete_achievement | their_race | content_published | coaching_philosophy | fallback",
  "hook_text": "the specific opening sentence used",
  "research_notes": "2-line summary: who they are and what makes them distinctive",
  "subject": "the subject line",
  "message": "the full email body"
}}
"""
    result = json.loads(_call_claude(prompt))
    if isinstance(result, list):
        result = result[0]
    result["message"] = _clean(result["message"])
    return result


# ── Fake data for --test mode ──────────────────────────────────────────────────

FAKE_COACHES = [
    {
        "name":          "Jan de Vries",
        "website":       "https://jancoaching.nl",
        "channel":       "instagram",
        "instagram_url": "https://instagram.com/jandevries_tri",
        "facebook_url":  "",
        "phone":         "",
        "email":         "",
        "website_text": (
            "Jan de Vries is a triathlon coach based in Amsterdam. "
            "This month his athlete Emma Bakker finished Ironman 70.3 Maastricht in 4:32, a new personal best. "
            "Jan has been coaching Emma for two years, building her race-day execution from scratch. "
            "He coaches 15 athletes individually, all focused on the 70.3 distance. "
            "Former competitive swimmer who transitioned to triathlon coaching in 2012."
        ),
    }
]


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("\n=== augo Coach Outreach Pipeline ===\n")

    test_mode  = "--test"      in sys.argv
    fetch_mode = "--fetch"     in sys.argv
    from_file  = "--from-file" in sys.argv

    seen = load_seen()

    if test_mode:
        print("TEST MODE — using fake coach data, no Exa.\n")
        coaches_data = FAKE_COACHES
        query = "test"

    elif fetch_mode:
        query = input("What do you want to search for?\n> ").strip()
        while not query:
            query = input("Please enter a search query\n> ").strip()
        limit_input = input("\nHow many coaches? (max 20, default: 5)\n> ").strip()
        try:
            limit = min(int(limit_input), 20) if limit_input else 5
        except ValueError:
            limit = 5

        raw_coaches  = find_coaches(query, limit)
        coaches_data = [research_coach(c, seen) for c in raw_coaches]
        coaches_data = [c for c in coaches_data if c]

        with open("coaches.json", "w") as f:
            json.dump(coaches_data, f, indent=2)
        print(f"\n✓ {len(coaches_data)} coaches saved to coaches.json")
        print(f"  Run with --from-file to draft messages without calling Exa again.")
        return

    elif from_file:
        if not os.path.exists("coaches.json"):
            print("coaches.json not found — run with --fetch first.")
            return
        with open("coaches.json") as f:
            coaches_data = json.load(f)
        query = "from_file"
        print(f"Loaded {len(coaches_data)} coaches from coaches.json\n")

    else:
        query = input("What do you want to search for?\n> ").strip()
        while not query:
            query = input("Please enter a search query\n> ").strip()
        limit_input = input("\nHow many coaches? (max 20, default: 10)\n> ").strip()
        try:
            limit = min(int(limit_input), 20) if limit_input else 10
        except ValueError:
            limit = 10

        raw_coaches  = find_coaches(query, limit)
        coaches_data = [research_coach(c, seen) for c in raw_coaches]
        coaches_data = [c for c in coaches_data if c]

    # Deduplicate within the same batch
    seen_names, unique = set(), []
    for c in coaches_data:
        key = seen_key(c["name"], c.get("email", ""))
        if key not in seen_names:
            seen_names.add(key)
            unique.append(c)
    coaches_data = unique

    slug        = re.sub(r"[\s]+", "_", re.sub(r"[^\w\s-]", "", query.lower()).strip())
    output_file = f"{slug}.csv"
    results     = []

    print(f"Drafting messages...\n")

    for i, coach_data in enumerate(coaches_data, 1):
        print(f"  [{i}/{len(coaches_data)}] {coach_data['name']}...", end=" ", flush=True)

        channel = coach_data.get("channel", "")
        try:
            if channel == "email":
                draft   = draft_email(coach_data)
                subject = draft.get("subject", "")
                message = draft.get("message", "")
            else:
                draft   = draft_dm(coach_data)
                subject = ""
                message = draft.get("dm_message", "")
        except Exception as e:
            print(f"✗ {e}")
            continue

        instagram = coach_data.get("instagram_url", "")
        email_val = coach_data.get("email", "")
        phone_val = coach_data.get("phone", "")
        facebook  = coach_data.get("facebook_url", "")
        website   = coach_data.get("website", "")

        socials = []
        if instagram: socials.append(f"Instagram: {instagram}")
        if email_val: socials.append(f"Email:     {email_val}")
        if phone_val: socials.append(f"Phone:     {phone_val}")
        if facebook:  socials.append(f"Facebook:  {facebook}")
        if website:   socials.append(f"Website:   {website}")

        color = CHANNEL_COLOR.get(channel, RESET)
        research_notes = draft.get("research_notes", "")

        print(f"✓")
        print(f"\n{'='*60}")
        print(f"  {BOLD}{coach_data['name']}{RESET}")
        print(f"  Channel: {color}{channel}{RESET}")
        for s in socials:
            print(f"  {s}")
        if research_notes:
            print(f"\n  About:   {research_notes}")
        if subject:
            print(f"\n  Subject: {subject}")
        print(f"\n  Message:\n  {message.replace(chr(10), chr(10) + '  ')}")
        print(f"{'='*60}\n")

        seen.add(seen_key(coach_data["name"], coach_data.get("email", "")))
        save_seen(seen)

        results.append({
            "name":           coach_data["name"],
            "channel":        channel,
            "instagram_url":  coach_data.get("instagram_url", ""),
            "phone":          coach_data.get("phone", ""),
            "email":          coach_data.get("email", ""),
            "facebook_url":   coach_data.get("facebook_url", ""),
            "website":        coach_data["website"],
            "research_notes": draft["research_notes"],
            "subject":        subject,
            "message":        message,
        })

    if results:
        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)

    channels = {}
    for r in results:
        channels[r["channel"]] = channels.get(r["channel"], 0) + 1

    print(f"\nDone — {len(results)} coaches processed")
    for ch, count in channels.items():
        print(f"  {ch}: {count}")
    if results:
        print(f"  CSV: {output_file}")


if __name__ == "__main__":
    main()
