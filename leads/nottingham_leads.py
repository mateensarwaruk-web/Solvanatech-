#!/usr/bin/env python3
"""Find Nottingham businesses that need a website or social media help, via Apify.

Runs the Apify Google Maps scraper (compass/crawler-google-places) for three
segments in Nottingham, UK:
  - restaurants/takeaways that offer delivery or takeaway
  - gyms and fitness studios
  - independent shops
Keeps only businesses with no website or a missing/thin social presence, scores
each on verifiable pain points (no website, missing social profiles, low
rating, few reviews, unclaimed listing, unanswered reviews, complaints that
point at online-presence problems), and writes the top N leads to CSV, split
across the segments.

Two data sources:
  --source google  Google Maps Places API (Text Search). Needs GOOGLE_MAPS_API_KEY
                   with "Places API (New)" enabled. Does not return social links,
                   so leads are qualified on "no website".
  --source apify   Apify Google Maps scraper. Needs APIFY_TOKEN. Also scrapes
                   Instagram/Facebook/TikTok, so it can flag thin social presence.

Usage:
    export GOOGLE_MAPS_API_KEY=AIza...
    python3 leads/nottingham_leads.py --source google --limit 100

    export APIFY_TOKEN=apify_api_xxx
    python3 leads/nottingham_leads.py --source apify --limit 100

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
SEGMENT_SEARCHES = {
    "restaurant": [
        "takeaway",
        "restaurant delivery",
        "food delivery",
        "pizza takeaway",
        "indian takeaway",
        "chinese takeaway",
        "kebab shop",
        "fried chicken shop",
        "caribbean takeaway",
        "dessert shop",
    ],
    "gym": [
        "gym",
        "fitness centre",
        "boxing gym",
        "martial arts gym",
        "personal training studio",
        "yoga studio",
    ],
    "shop": [
        "clothing shop",
        "gift shop",
        "florist",
        "bakery",
        "convenience store",
        "phone repair shop",
        "barber shop",
        "beauty salon",
    ],
}
SEARCHES = [q for qs in SEGMENT_SEARCHES.values() for q in qs]
SEARCH_SEGMENT = {q: seg for seg, qs in SEGMENT_SEARCHES.items() for q in qs}
# Default share of the final list per segment (backfilled if one runs short).
SEGMENT_SHARE = {"restaurant": 0.4, "gym": 0.3, "shop": 0.3}
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
    "hidden gem": "'hidden gem' - good reviews but low awareness",
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


GOOGLE_FIELDS = ",".join("places." + f for f in (
    "id", "displayName", "formattedAddress", "nationalPhoneNumber", "websiteUri",
    "rating", "userRatingCount", "googleMapsUri", "primaryTypeDisplayName", "types",
    "delivery", "takeout", "businessStatus", "reviews", "photos",
)) + ",nextPageToken"
NOTTINGHAM = {"latitude": 52.9548, "longitude": -1.1581}


def google_post(key, body):
    req = urllib.request.Request(
        "https://places.googleapis.com/v1/places:searchText",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": GOOGLE_FIELDS,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def from_google(g, query):
    """Map a Places API result onto the Apify item shape used below."""
    status = g.get("businessStatus")
    return {
        "source": "google",
        "searchString": query,
        "placeId": g.get("id"),
        "title": (g.get("displayName") or {}).get("text"),
        "address": g.get("formattedAddress"),
        "phone": g.get("nationalPhoneNumber"),
        "website": g.get("websiteUri"),
        "totalScore": g.get("rating"),
        "reviewsCount": g.get("userRatingCount"),
        "url": g.get("googleMapsUri"),
        "categoryName": (g.get("primaryTypeDisplayName") or {}).get("text"),
        "categories": [t.replace("_", " ") for t in g.get("types") or []],
        "additionalInfo": {"Service options": [
            {"Delivery": bool(g.get("delivery"))},
            {"Takeaway": bool(g.get("takeout"))},
        ]},
        "permanentlyClosed": status == "CLOSED_PERMANENTLY",
        "temporarilyClosed": status == "CLOSED_TEMPORARILY",
        "imagesCount": len(g.get("photos") or []),
        "reviews": [
            {"text": (r.get("text") or {}).get("text"), "publishedAtDate": r.get("publishTime")}
            for r in g.get("reviews") or []
        ],
    }


def run_google(key, per_search):
    places = []
    for query in SEARCHES:
        body = {
            "textQuery": f"{query} in {LOCATION}",
            "regionCode": "GB",
            "pageSize": 20,
            "locationBias": {"circle": {"center": NOTTINGHAM, "radius": 15000.0}},
        }
        got = 0
        while got < per_search:
            data = google_post(key, body)
            batch = data.get("places") or []
            places += [from_google(g, query) for g in batch]
            got += len(batch)
            if not data.get("nextPageToken") or not batch:
                break
            body["pageToken"] = data["nextPageToken"]
        print(f"  {query}: {got}", file=sys.stderr)
    # Text Search can drift outside the city; keep NG postcodes / Nottingham.
    return [p for p in places if " NG" in (p["address"] or "") or "Nottingham" in (p["address"] or "")]


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


def segment(place):
    seg = SEARCH_SEGMENT.get((place.get("searchString") or "").strip().lower())
    if seg:
        return seg
    cats = " ".join([place.get("categoryName") or ""] + (place.get("categories") or [])).lower()
    if any(w in cats for w in ("gym", "fitness", "yoga", "boxing", "martial", "trainer")):
        return "gym"
    if any(w in cats for w in ("restaurant", "takeaway", "take away", "food", "cafe", "pizza", "kebab")):
        return "restaurant"
    return "shop"


def service_options(place):
    """Delivery/takeaway flags from the Google listing's 'Service options'."""
    opts = set()
    for item in (place.get("additionalInfo") or {}).get("Service options") or []:
        for k, v in item.items():
            if v:
                opts.add(k.lower())
    cats = " ".join([place.get("categoryName") or ""] + (place.get("categories") or [])).lower()
    delivery = any("delivery" in o for o in opts) or "delivery" in cats
    takeaway = any(w in o for o in opts for w in ("takeaway", "takeout", "take away")) or "takeaway" in cats
    return delivery, takeaway


def score(place):
    """Return (score, list of evidence-backed pain points)."""
    pts, pains = 0, []
    rating = place.get("totalScore")
    count = place.get("reviewsCount") or 0
    reviews = place.get("reviews") or []
    social = socials(place)
    google = place.get("source") == "google"

    if not place.get("website"):
        pts += 3
        pains.append("No website on Google listing")
    if google:
        pass
    elif not social:
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
        if replied == 0 and not google:
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
    ap.add_argument("--per-search", type=int, default=40)
    ap.add_argument("--min-score", type=int, default=4)
    ap.add_argument("--out", default="leads/nottingham_leads.csv")
    ap.add_argument("--source", choices=["google", "apify"], default="google")
    ap.add_argument("--from-json", help="score a saved Apify dataset instead of calling the API")
    args = ap.parse_args()

    if args.from_json:
        with open(args.from_json) as f:
            places = json.load(f)
    elif args.source == "google":
        key = os.environ.get("GOOGLE_MAPS_API_KEY")
        if not key:
            sys.exit("Set GOOGLE_MAPS_API_KEY (Google Cloud console > APIs & Services > Credentials).")
        places = run_google(key, min(args.per_search, 60))
        with open(os.path.splitext(args.out)[0] + "_raw.json", "w") as f:
            json.dump(places, f)
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
        seg = segment(p)
        delivery, takeaway = service_options(p)
        if seg == "restaurant" and not (delivery or takeaway):
            continue
        soc = socials(p)
        # Must need a website or social media help. Google can't see socials,
        # so there "needs help" means no website.
        if p.get("website") and (p.get("source") == "google" or len(soc) >= 2):
            continue
        s, pains = score(p)
        if s < args.min_score:
            continue
        leads.append({
            "segment": seg,
            "score": s,
            "name": p.get("title"),
            "category": p.get("categoryName"),
            "delivery": "yes" if delivery else "",
            "takeaway": "yes" if takeaway else "",
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
    picked = []
    for seg, share in SEGMENT_SHARE.items():
        picked += [r for r in leads if r["segment"] == seg][: round(args.limit * share)]
    rest = [r for r in leads if r not in picked]
    picked += rest[: args.limit - len(picked)]
    leads = sorted(picked, key=lambda r: (r["segment"], -r["score"], -(r["google_reviews"] or 0)))
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(leads[0]) if leads else ["score"])
        w.writeheader()
        w.writerows(leads)
    counts = {seg: sum(r["segment"] == seg for r in leads) for seg in SEGMENT_SHARE}
    print(f"{len(places)} places scraped, {len(leads)} qualified leads {counts} -> {args.out}")


if __name__ == "__main__":
    main()
