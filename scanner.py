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

def check_if_active_and_extract(ebay_id, fallback_title, fallback_price):
    """ამოწმებს ნივთი რეალურად აქტიურია თუ უკვე გაყიდული/დასრულებული, და იღებს ზუსტ ფოტოსა და ფასს"""
    url = f"https://www.ebay.com/itm/{ebay_id}"
    try:
        resp = cffi_requests.get(url, impersonate="chrome", timeout=12)
        # თუ eBay-მ 200 დააბრუნა, ვამოწმებთ სტატუსს
        if resp.status_code == 200:
            html_lower = resp.text.lower()
            for phrase in ENDED_PHRASES:
                if phrase in html_lower:
                    print(f"  ⛔ გამოტოვებულია (უკვე გაყიდული/დასრულებული): {ebay_id}")
                    return None

            # სათაური
            title_m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', resp.text)
            title = title_m.group(1).replace(" | eBay", "").strip() if title_m else fallback_title

            # რეალური მთავარი ფოტო
            img_m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', resp.text)
            img_url = img_m.group(1) if img_m else ""
            if img_url and "ebaystatic" not in img_url:
                img_url = re.sub(r's-l\d+', 's-l800', img_url)
            else:
                img_url = ""

            # ზუსტი ფასი
            price_m = re.search(r'"price"\s*:\s*"?([\d,\.]+)"?', resp.text)
            curr_m = re.search(r'"priceCurrency"\s*:\s*"([A-Z]{3})"', resp.text)
            if price_m:
                curr = "$" if (not curr_m or curr_m.group(1) == "USD") else curr_m.group(1) + " "
                price = f"{curr}{price_m.group(1)}"
            else:
                price = fallback_price

            return {
                "active": True,
                "title": title,
                "price": price,
                "image_url": img_url
            }
        elif resp.status_code == 404:
            print(f"  ⛔ გამოტოვებულია (წაშლილი განცხადება): {ebay_id}")
            return None
    except Exception:
        pass

    # თუ itm გვერდმა დროებით არ გვიპასუხა, ვამოწმებთ ტექსტში ხომ არ წერია Sold/Ended
    low_title = fallback_title.lower()
    if any(w in low_title for w in ["sold", "ended", "completed"]):
        return None

    return {
        "active": True,
        "title": fallback_title,
        "price": fallback_price,
        "image_url": ""
    }

def search_ebay_items(query, scan_mode="new"):
    """ეძებს მხოლოდ ბოლო დღეების/კვირის აქტიურ განცხადებებს (3 თვის ძველებს აღარ იღებს)"""
    results = []
    # new რეჟიმში ვეძებთ ბოლო კვირის ('w') შედეგებს, deep-ში ბოლო თვის ('m')
    t_limit = "w" if scan_mode == "new" else "m"

    for sq in [f'site:ebay.com/itm {query}', f'site:ebay.com/itm "{query}"']:
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(sq, timelimit=t_limit, max_results=15):
                    href = r.get("href") or r.get("link") or ""
                    title = (r.get("title") or "").replace(" | eBay", "").strip()
                    body = r.get("body") or r.get("snippet") or ""

                    # თავიდანვე ვფილტრავთ გაყიდულებს
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
                            "title": title or f"eBay Watch #{eid}",
                            "snippet": body,
                            "price": price,
                            "ebay_url": f"https://www.ebay.com/itm/{eid}"
                        })
        except Exception as e:
            print(f"DDGS notice ({t_limit}): {e}")

        if len(results) >= 10:
            break
        time.sleep(1)

    return results[:15]

def get_fallback_thumbnail(title):
    try:
        with DDGS() as ddgs:
            imgs = list(ddgs.images(f"ebay {title[:60]}", max_results=2))
            if imgs and imgs[0].get("image"):
                return imgs[0]["image"]
    except Exception:
        pass
    return "https://placehold.co/600x400/0f172a/fbbf24?text=eBay+Vintage+Watch"

def analyze_lot(title, snippet):
    prompt = (
        f"Title: '{title}'. Description: '{snippet}'. "
        "Is this an active vintage watch or lot of watches (gold-plated, gold-filled, mechanical, quartz)? "
        "Reject ONLY empty boxes or loose straps with no watch. "
        "Reply ONLY valid JSON: {\"match\": true/false, \"note_ge\": \"მოკლე აღწერა ქართულად (1 წინადადება)\"}"
    )
    for model_id in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]:
        try:
            resp = ai_client.models.generate_content(model=model_id, contents=prompt)
            text = resp.text.strip().replace("```json", "").replace("```", "").strip()
            data = json.loads(text)
            return bool(data.get("match", True)), data.get("note_ge", "ვინტაჟური საათების ლოტი eBay-დან")
        except Exception:
            continue
    return True, "აქტიური ლოტი eBay-დან: " + (snippet[:80] if snippet else title[:80])

def main():
    print("=== სკანერი დაიწყო (მხოლოდ ახალი და აქტიური განცხადებები) ===")
    settings_res = supabase.table("app_settings").select("*").eq("id", 1).execute()
    settings = settings_res.data[0] if settings_res.data else {}
    keywords = settings.get("keywords") or ["vintage gold plated watch lot"]
    scan_mode = settings.get("scan_mode") or "new"

    existing_res = supabase.table("watch_lots").select("ebay_id").execute()
    existing_ids = {row["ebay_id"] for row in (existing_res.data or [])}

    added_total = 0
    for kw in keywords:
        items = search_ebay_items(kw, scan_mode=scan_mode)
        print(f"სიტყვა '{kw}' -> ნაპოვნია {len(items)} ბოლოდროინდელი კანდიდატი")

        for item in items[:10]:
            if item["ebay_id"] in existing_ids:
                continue

            # ვამოწმებთ ნამდვილად აქტიურია თუ არა ნივთი eBay-ზე
            verified = check_if_active_and_extract(item["ebay_id"], item["title"], item["price"])
            if not verified:
                existing_ids.add(item["ebay_id"])
                continue

            is_match, note_ge = analyze_lot(verified["title"], item["snippet"])
            if is_match:
                img_url = verified["image_url"] or get_fallback_thumbnail(verified["title"])
                try:
                    supabase.table("watch_lots").insert({
                        "ebay_id": item["ebay_id"],
                        "title": verified["title"],
                        "price": verified["price"],
                        "lot_type": "eBay Lot",
                        "image_url": img_url,
                        "ebay_url": item["ebay_url"],
                        "ai_match": True,
                        "ai_note": note_ge,
                        "status": "new",
                        "search_source": "keyword"
                    }).execute()
                    existing_ids.add(item["ebay_id"])
                    added_total += 1
                    print(f"✅ დაემატა აქტიური: {verified['title'][:55]} ({verified['price']})")
                except Exception as db_err:
                    print(f"❌ DB შეცდომა: {db_err}")
            time.sleep(0.5)

    print(f"=== დასრულდა! სულ დაემატა {added_total} აქტიური ახალი ლოტი ===")

if __name__ == "__main__":
    main()
