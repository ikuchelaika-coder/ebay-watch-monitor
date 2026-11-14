import os
import re
import json
import time
import urllib.parse
from curl_cffi import requests as cffi_requests
from google import genai
from supabase import create_client

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

ENDED_PHRASES = [
    "this listing has ended",
    "bidding has ended",
    "this item has sold",
    "item is no longer available",
    "was sold",
    "sold for",
    "out of stock",
    "listing ended",
    "0 available",
]

MUST_HAVE_WORDS = [
    "watch", "watches", "chronograph", "timepiece", "bulova", "omega", "seiko",
    "longines", "tissot", "elgin", "waltham", "hamilton", "gruen", "wittnauer",
    "citizen", "timex", "swatch", "casio", "rolex", "cartier", "zenith", "movado"
]

JUNK_WORDS = [
    "box only", "empty box", "watch box", "display case", "watch band", "watch strap",
    "leather strap", "bracelet only", "buckle", "clasp", "spring bar", "battery",
    "batteries", "wall clock", "alarm clock", "desk clock", "manual", "catalog",
    "advertisement", "ad print", "poster", "tool", "opener", "holder", "crystal only",
    "crown only", "hands only", "dial only", "bezel only", "smartwatch", "apple watch",
    "garmin", "fitbit", "samsung galaxy watch", "charger", "cable"
]

def is_relevant_text(title, snippet=""):
    combined = f"{title} {snippet}".lower()
    if any(junk in combined for junk in JUNK_WORDS):
        return False
    if not any(good in combined for good in MUST_HAVE_WORDS):
        return False
    return True

def get_real_image_for_item(ebay_id, title):
    """პოულობს რეალურ eBay სურათს 3 სხვადასხვა წყაროდან"""
    # წყარო 1: პირდაპირ eBay გვერდის მეტა-ტეგი
    try:
        resp = cffi_requests.get(f"https://www.ebay.com/itm/{ebay_id}", impersonate="chrome", timeout=10)
        if resp.status_code == 200:
            img_m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', resp.text)
            if img_m and "ebaystatic" not in img_m.group(1):
                return re.sub(r's-l\d+', 's-l800', img_m.group(1))
            alt_m = re.search(r'https://i\.ebayimg\.com/images/g/[A-Za-z0-9_-]+/s-l\d+\.(?:jpg|webp|png)', resp.text)
            if alt_m:
                return re.sub(r's-l\d+', 's-l800', alt_m.group(0))
    except Exception:
        pass

    # წყარო 2: DDGS Images ძიება eBay ID-ით და სათაურით
    for q in [f"ebay {ebay_id}", f"ebay {title[:55]}"]:
        try:
            with DDGS() as ddgs:
                imgs = list(ddgs.images(q, max_results=3))
                for im in imgs:
                    url = im.get("image") or im.get("thumbnail") or ""
                    if url and "ebaystatic" not in url and url.startswith("http"):
                        return re.sub(r's-l\d+', 's-l800', url)
        except Exception:
            pass

    # წყარო 3: Bing Images HTML
    try:
        q = urllib.parse.quote_plus(f"ebay {title[:55]}")
        resp = cffi_requests.get(f"https://www.bing.com/images/search?q={q}", impersonate="chrome", timeout=10)
        if resp.status_code == 200:
            img_m = re.search(r'https://i\.ebayimg\.com/images/g/[A-Za-z0-9_-]+/s-l\d+\.(?:jpg|webp|png)', resp.text)
            if img_m:
                return re.sub(r's-l\d+', 's-l800', img_m.group(0))
    except Exception:
        pass

    return ""

def check_if_active_and_extract(ebay_id, fallback_title, fallback_price):
    url = f"https://www.ebay.com/itm/{ebay_id}"
    title = fallback_title
    price = fallback_price

    try:
        resp = cffi_requests.get(url, impersonate="chrome", timeout=12)
        if resp.status_code == 404:
            return None
        if resp.status_code == 200:
            html_lower = resp.text.lower()
            if any(phrase in html_lower for phrase in ENDED_PHRASES):
                print(f"  ⛔ გაყიდული/დასრულებულია: {ebay_id}")
                return None

            title_m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', resp.text)
            if title_m:
                title = title_m.group(1).replace(" | eBay", "").strip()

            price_m = re.search(r'"price"\s*:\s*"?([\d,\.]+)"?', resp.text)
            curr_m = re.search(r'"priceCurrency"\s*:\s*"([A-Z]{3})"', resp.text)
            if price_m:
                curr = "$" if (not curr_m or curr_m.group(1) == "USD") else curr_m.group(1) + " "
                price = f"{curr}{price_m.group(1)}"
    except Exception:
        pass

    if not is_relevant_text(title):
        print(f"  ⛔ უაზრო განცხადება გაიფილტრა: {title[:50]}")
        return None

    img_url = get_real_image_for_item(ebay_id, title)
    if not img_url:
        print(f"  ⛔ ფოტო ვერ მოიძებნა, გამოტოვებულია: {title[:45]}")
        return None

    return {
        "title": title,
        "price": price,
        "image_url": img_url
    }

def search_ebay_items(query, scan_mode="new"):
    results = []
    t_limit = "w" if scan_mode == "new" else "m"

    for sq in [f'site:ebay.com/itm "{query}"', f'site:ebay.com/itm {query}']:
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(sq, timelimit=t_limit, max_results=15):
                    href = r.get("href") or r.get("link") or ""
                    title = (r.get("title") or "").replace(" | eBay", "").strip()
                    body = r.get("body") or r.get("snippet") or ""

                    if not is_relevant_text(title, body):
                        continue

                    combined_low = (title + " " + body).lower()
                    if any(p in combined_low for p in ["sold for", "bidding has ended", "this listing has ended", "was sold"]):
                        continue

                    id_m = re.search(r'ebay\.com/itm/(?:[^/?#]+/)?(\d{9,14})', href)
                    if not id_m:
                        continue
                    eid = id_m.group(1)

                    price_m = re.search(r'(\$\s*[\d,\.]+|US\s*\$[\d,\.]+)', title + " " + body)
                    price = price_m.group(1).replace("US ", "") if price_m else "ფასი იხ. eBay-ზე"

                    if not any(x["ebay_id"] == eid for x in results):
                        results.append({
                            "ebay_id": eid,
                            "title": title,
                            "snippet": body,
                            "price": price,
                            "ebay_url": f"https://www.ebay.com/itm/{eid}"
                        })
        except Exception as e:
            print(f"DDGS notice: {e}")

        if len(results) >= 10:
            break
        time.sleep(1)

    return results[:12]

def analyze_lot(title, snippet):
    prompt = (
        f"You are a strict vintage watch buyer filter. Title: '{title}'. Description: '{snippet}'. "
        "Accept ONLY real wristwatches, pocket watches, or watch lots/bundles (especially vintage, gold-plated, gold-filled, mechanical). "
        "Strictly REJECT parts, empty boxes, straps, tools, clocks, smartwatches, or non-watch items. "
        "Reply ONLY valid JSON: {\"match\": true/false, \"note_ge\": \"მოკლე აღწერა ქართულად (1 წინადადება)\"}"
    )
    for model_id in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]:
        try:
            resp = ai_client.models.generate_content(model=model_id, contents=prompt)
            text = resp.text.strip().replace("```json", "").replace("```", "").strip()
            data = json.loads(text)
            return bool(data.get("match", False)), data.get("note_ge", "ვინტაჟური საათების ლოტი")
        except Exception:
            continue
    # თუ AI დროებით მიუწვდომელია, ტექსტური ფილტრი უკვე გავლილი აქვს
    return True, "საათი / ლოტი eBay-დან: " + title[:75]

def main():
    print("=== სკანერი დაიწყო (მკაცრი ფილტრი + რეალური ფოტოები) ===")
    settings_res = supabase.table("app_settings").select("*").eq("id", 1).execute()
    settings = settings_res.data[0] if settings_res.data else {}
    keywords = settings.get("keywords") or ["vintage gold plated watch lot"]
    scan_mode = settings.get("scan_mode") or "new"

    existing_res = supabase.table("watch_lots").select("ebay_id").execute()
    existing_ids = {row["ebay_id"] for row in (existing_res.data or [])}

    added_total = 0
    for kw in keywords:
        items = search_ebay_items(kw, scan_mode=scan_mode)
        print(f"სიტყვა '{kw}' -> გაფილტრული კანდიდატი: {len(items)}")

        for item in items[:8]:
            if item["ebay_id"] in existing_ids:
                continue

            verified = check_if_active_and_extract(item["ebay_id"], item["title"], item["price"])
            if not verified:
                existing_ids.add(item["ebay_id"])
                continue

            is_match, note_ge = analyze_lot(verified["title"], item["snippet"])
            if is_match:
                try:
                    supabase.table("watch_lots").insert({
                        "ebay_id": item["ebay_id"],
                        "title": verified["title"],
                        "price": verified["price"],
                        "lot_type": "eBay Lot",
                        "image_url": verified["image_url"],
                        "ebay_url": item["ebay_url"],
                        "ai_match": True,
                        "ai_note": note_ge,
                        "status": "new",
                        "search_source": "keyword"
                    }).execute()
                    existing_ids.add(item["ebay_id"])
                    added_total += 1
                    print(f"✅ დაემატა ფოტოთი: {verified['title'][:50]} | {verified['price']}")
                except Exception as db_err:
                    print(f"❌ DB შეცდომა: {db_err}")
            time.sleep(0.5)

    print(f"=== დასრულდა! სულ დაემატა {added_total} სუფთა ლოტი რეალური ფოტოთი ===")

if __name__ == "__main__":
    main()
