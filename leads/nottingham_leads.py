#!/usr/bin/env python3
"""Find Nottingham food businesses that need social media help, via Apify.

Runs the Apify Google Maps scraper (compass/crawler-google-places) for
restaurants, takeaways and delivery shops in Nottingham, UK, then scores each
business on verifiable pain points (no website, missing social profiles, low
rating, few reviews, unclaimed listing, unanswered reviews, complaints that
point at online-presence problems). Writes the top N leads to CSV.

Usage:
    export APIFY_TOKEN=apify_api_xxx
    python3 leads/nottingham_leads.py --limit 100 --out leads/nottingham_leads.csv

No third-party packages needed (standard library only).
"""

import argparse
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

ACTOR = "compass~crawler-google-places"
SEARCHES = [
    "takeaway",
    "restaurant",
    "food delivery",
    "pizza takeaway",
    "indian takeaway",
    "chinese takeaway",
    "kebab shop",
    "fried chicken shop",
    "cafe",
    "dessert shop",
]
LOCATION = "Nottingham, United Kingdom"

# Phrases in customer reviews that signal an online-presence / comms problem.
COMPLAINT_PHRASES = {
    "phone": "customers can't get through on the phone",
    "no answer": "customers can't reach them",
    "didn't answer": "customers can't reach them",
    "website": "website problems mentioned in reviews",
    "online": "online ordering/info problems mentioned",
    "menu": "menu confusion (outdated/unavailable online menu)",
    "opening hours": "wrong opening hours online",
    "was closed": "listed as open but was closed",
    "photos": "food doesn't match photos / no photos",
    "hidden gem": "'hidden gem' - low awareness despite good food",
    "deserves more": "under-discovered business",
}


def run_actor(token, per_search):
    body = {
        "searchStringsArray": SEARCHES,
        "locationQuery": LOCATION,
        "maxCrawledPlacesPerSearch": per_search,
        "language": "en",
        "countryCode": "gb",
        "scrapeContacts": True,  # pulls Instagram/Facebook/TikTok/email from websites
        "maxReviews": 20,
        "reviewsSort": "newest",
        "scrapeReviewsPersonalData": False,
        "skipClosedPlaces": True,
    }
    url = (
        f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
        f"?token={token}&timeout=1800"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=1900) as resp:
        return json.load(resp)


def socials(place):
    found = {}
    for key, label in (
        ("instagrams", "Instagram"),
        ("facebooks", "Facebook"),
        ("tiktoks", "TikTok"),
        ("twitters", "X/Twitter"),
    ):
        links = place.get(key) or []
        if links:
            found[label] = links[0]
    return found


def score(place):
    """Return (score, list of evidence-backed pain points)."""
    pts, pains = 0, []
    rating = place.get("totalScore")
    count = place.get("reviewsCount") or 0
    reviews = place.get("reviews") or []
    social = socials(place)

    if not place.get("website"):
        pts += 3
        pains.append("No website on Google listing")
    if not social:
        pts += 3
        pains.append("No Instagram/Facebook/TikTok found")
    elif len(social) == 1:
        pts += 1
        pains.append(f"Only on {next(iter(social))} - thin social presence")
    if rating is not None and rating < 3.5:
        pts += 3
        pains.append(f"Low Google rating ({rating})")
    elif rating is not None and rating < 4.0:
        pts += 2
        pains.append(f"Below-average Google rating ({rating})")
    if count < 50:
        pts += 2
        pains.append(f"Low visibility - only {count} Google reviews")
    if place.get("claimThisBusiness"):
        pts += 2
        pains.append("Google Business Profile not claimed")
    if not place.get("imageUrls") and (place.get("imagesCount") or 0) < 10:
        pts += 1
        pains.append("Few photos on Google listing")

    if reviews:
        replied = sum(1 for r in reviews if r.get("responseFromOwnerText"))
        if replied == 0:
            pts += 1
            pains.append(f"Owner hasn't replied to any of the last {len(reviews)} reviews")
        dates = [r.get("publishedAtDate") for r in reviews if r.get("publishedAtDate")]
        if dates:
            newest = max(datetime.fromisoformat(d.replace("Z", "+00:00")) for d in dates)
            if newest < datetime.now(timezone.utc) - timedelta(days=90):
                pts += 1
                pains.append("No new reviews in 90+ days (low engagement)")
        hits = set()
        for r in reviews:
            text = (r.get("text") or "").lower()
            for phrase, meaning in COMPLAINT_PHRASES.items():
                if phrase in text:
                    hits.add(meaning)
        for meaning in sorted(hits)[:3]:
            pts += 1
            pains.append(f"Reviews mention: {meaning}")
    return pts, pains


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--per-search", type=int, default=60)
    ap.add_argument("--min-score", type=int, default=4)
    ap.add_argument("--out", default="leads/nottingham_leads.csv")
    ap.add_argument("--from-json", help="score a saved Apify dataset instead of calling the API")
    args = ap.parse_args()

    if args.from_json:
        with open(args.from_json) as f:
            places = json.load(f)
    else:
        token = os.environ.get("APIFY_TOKEN")
        if not token:
            sys.exit("Set APIFY_TOKEN (Apify console > Settings > Integrations).")
        places = run_actor(token, args.per_search)
        with open(os.path.splitext(args.out)[0] + "_raw.json", "w") as f:
            json.dump(places, f)

    seen, leads = set(), []
    for p in places:
        key = p.get("placeId") or (p.get("title"), p.get("address"))
        if key in seen or p.get("permanentlyClosed") or p.get("temporarilyClosed"):
            continue
        seen.add(key)
        s, pains = score(p)
        if s < args.min_score:
            continue
        soc = socials(p)
        leads.append({
            "score": s,
            "name": p.get("title"),
            "category": p.get("categoryName"),
            "address": p.get("address"),
            "phone": p.get("phone"),
            "email": ", ".join(p.get("emails") or []),
            "website": p.get("website") or "",
            "instagram": soc.get("Instagram", ""),
            "facebook": soc.get("Facebook", ""),
            "tiktok": soc.get("TikTok", ""),
            "google_rating": p.get("totalScore"),
            "google_reviews": p.get("reviewsCount"),
            "pain_points": " | ".join(pains),
            "google_maps_url": p.get("url"),
        })

    leads.sort(key=lambda r: (-r["score"], -(r["google_reviews"] or 0)))
    leads = leads[: args.limit]
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(leads[0]) if leads else ["score"])
        w.writeheader()
        w.writerows(leads)
    print(f"{len(places)} places scraped, {len(leads)} qualified leads -> {args.out}")


if __name__ == "__main__":
    main()
