import os
import re
import json
import html
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

def search_ebay_items(query):
    """იღებს რეალურ eBay ლოტებს პირდაპირ DDGS API-ით + Yahoo Search ფოლბექით"""
    results = []

    # 1. DDGS (პირდაპირი JSON შედეგები, HTML კლასების გარეშე)
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(f"site:ebay.com/itm {query}", max_results=12):
                href = r.get("href") or r.get("link") or ""
                title = (r.get("title") or "").replace(" | eBay", "").strip()
                body = r.get("body") or r.get("snippet") or ""
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
        print(f"DDGS notice: {e}")

    # 2. Yahoo Search ფოლბექი (თუ DDGS-მა ცოტა დააბრუნა)
    if len(results) < 4:
        try:
            q = urllib.parse.quote_plus(f"site:ebay.com/itm {query}")
            resp = cffi_requests.get(f"https://search.yahoo.com/search?p={q}", impersonate="chrome", timeout=15)
            if resp.status_code == 200:
                found_urls = re.findall(r'https%3a%2f%2fwww\.ebay\.com%2fitm%2f[^"&]+|https://www\.ebay\.com/itm/[^"&]+', resp.text, re.I)
                for raw_u in found_urls:
                    u = urllib.parse.unquote(raw_u)
                    id_m = re.search(r'/itm/(?:[^/?#]+/)?(\d{9,14})', u)
                    if not id_m:
                        continue
                    eid = id_m.group(1)
                    if not any(x["ebay_id"] == eid for x in results):
                        results.append({
                            "ebay_id": eid,
                            "title": f"{query.title()} — eBay Lot #{eid}",
                            "snippet": query,
                            "price": "ფასი იხ. eBay-ზე",
                            "ebay_url": f"https://www.ebay.com/itm/{eid}"
                        })
        except Exception as e:
            print(f"Yahoo notice: {e}")

    return results[:12]

def get_thumbnail(title, query):
    """იღებს რეალურ ფოტოს DDGS Images ან Bing Images-იდან"""
    try:
        with DDGS() as ddgs:
            imgs = list(ddgs.images(f"ebay {title[:60]}", max_results=2))
            if imgs and imgs[0].get("image"):
                return imgs[0]["image"]
    except Exception:
        pass

    try:
        q = urllib.parse.quote_plus(f"ebay {title[:60]}")
        resp = cffi_requests.get(f"https://www.bing.com/images/search?q={q}", impersonate="chrome", timeout=10)
        if resp.status_code == 200:
            img_m = re.search(r'https://i\.ebayimg\.com/images/g/[A-Za-z0-9_-]+/s-l\d+\.(?:jpg|webp|png)', resp.text)
            if img_m:
                return re.sub(r's-l\d+', 's-l800', img_m.group(0))
    except Exception:
        pass

    return "https://placehold.co/600x400/0f172a/fbbf24?text=eBay+Vintage+Watch"

def analyze_lot(title, snippet):
    prompt = (
        f"Title: '{title}'. Description: '{snippet}'. "
        "Is this a watch or lot of watches (vintage, gold-plated, gold-filled, mechanical, quartz)? "
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
    return True, "ნაპოვნია eBay-ზე (" + (snippet[:80] if snippet else title[:80]) + ")"

def main():
    print("=== სკანერი დაიწყო (DDGS + Yahoo) ===")
    settings_res = supabase.table("app_settings").select("*").eq("id", 1).execute()
    settings = settings_res.data[0] if settings_res.data else {}
    keywords = settings.get("keywords") or ["vintage gold plated watch lot"]

    existing_res = supabase.table("watch_lots").select("ebay_id").execute()
    existing_ids = {row["ebay_id"] for row in (existing_res.data or [])}
    print(f"ბაზაში უკვე არის: {len(existing_ids)} ლოტი.")

    added_total = 0
    for kw in keywords:
        items = search_ebay_items(kw)
        print(f"სიტყვა '{kw}' -> ნაპოვნია {len(items)} რეალური განცხადება")

        for item in items[:8]:
            if item["ebay_id"] in existing_ids:
                continue
            is_match, note_ge = analyze_lot(item["title"], item["snippet"])
            if is_match:
                img_url = get_thumbnail(item["title"], kw)
                try:
                    supabase.table("watch_lots").insert({
                        "ebay_id": item["ebay_id"],
                        "title": item["title"],
                        "price": item["price"],
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
                    print(f"✅ დაემატა: {item['title'][:60]}")
                except Exception as db_err:
                    print(f"❌ DB შეცდომა: {db_err}")
            time.sleep(0.5)

    print(f"=== დასრულდა! სულ დაემატა {added_total} ახალი ლოტი ===")

if __name__ == "__main__":
    main()
