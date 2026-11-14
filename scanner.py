import os
import re
import json
import html
import urllib.parse
import xml.etree.ElementTree as ET
from curl_cffi import requests as cffi_requests
from google import genai
from google.genai import types
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

def get_working_model():
    for m in ["gemini-3.8-flash", "gemini-flash-latest", "gemini-2.0-flash", "gemini-2.5-pro"]:
        try:
            r = ai_client.models.generate_content(model=m, contents="Reply OK")
            if r and r.text:
                print(f"✓ აქტიური AI მოდელი: {m}")
                return m
        except Exception:
            continue
    return "gemini-3.8-flash"

def search_via_bing_rss(query):
    """Bing Search RSS — სუფთა XML, არ იბლოკება და აბრუნებს ზუსტ eBay ლინკებს, სათაურებს და ფასებს"""
    results = []
    q = urllib.parse.quote_plus(f"site:ebay.com/itm {query}")
    url = f"https://www.bing.com/search?q={q}&format=rss"
    try:
        resp = cffi_requests.get(url, impersonate="chrome", timeout=15)
        if resp.status_code == 200 and "<item>" in resp.text:
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item"):
                link = item.findtext("link") or ""
                id_m = re.search(r'/itm/(?:[^/]+/)?(\d{9,14})', link)
                if not id_m:
                    continue
                eid = id_m.group(1)
                title = html.unescape(item.findtext("title") or "").replace(" | eBay", "").strip()
                desc = html.unescape(item.findtext("description") or "")
                price_m = re.search(r'(\$\s*[\d,\.]+|US\s*\$[\d,\.]+)', title + " " + desc)
                price = price_m.group(1).replace("US ", "") if price_m else "ფასი იხ. eBay-ზე"
                if not any(r["ebay_id"] == eid for r in results):
                    results.append({
                        "ebay_id": eid,
                        "title": title,
                        "snippet": desc,
                        "price": price,
                        "ebay_url": f"https://www.ebay.com/itm/{eid}"
                    })
    except Exception as e:
        print(f"Bing RSS notice: {e}")
    return results

def search_via_gemini_google(query, model_name):
    """პირდაპირ Google Search-ით პოულობს რეალურ eBay ლოტებს"""
    results = []
    try:
        resp = ai_client.models.generate_content(
            model=model_name,
            contents=(
                f"Search Google for active eBay watch listings matching: '{query}' (site:ebay.com/itm/). "
                "Return up to 8 real listings with their exact ebay.com/itm/ URL, title, and price if visible."
            ),
            config=types.GenerateContentConfig(tools=[{"google_search": {}}])
        )
        chunks = []
        if resp.candidates and resp.candidates[0].grounding_metadata:
            chunks = resp.candidates[0].grounding_metadata.grounding_chunks or []
        for ch in chunks:
            if ch.web and ch.web.uri:
                uri = ch.web.uri
                title = (ch.web.title or "").replace(" | eBay", "").strip()
                id_m = re.search(r'/itm/(?:[^/]+/)?(\d{9,14})', uri)
                if id_m:
                    eid = id_m.group(1)
                    if not any(r["ebay_id"] == eid for r in results):
                        results.append({
                            "ebay_id": eid,
                            "title": title or f"eBay Watch #{eid}",
                            "snippet": "",
                            "price": "ფასი იხ. eBay-ზე",
                            "ebay_url": f"https://www.ebay.com/itm/{eid}"
                        })
    except Exception as e:
        print(f"Google Grounding notice: {e}")
    return results

def get_thumbnail(title):
    try:
        q = urllib.parse.quote_plus(f"ebay {title[:65]}")
        resp = cffi_requests.get(f"https://www.bing.com/images/search?q={q}", impersonate="chrome", timeout=10)
        if resp.status_code == 200:
            img_m = re.search(r'https://i\.ebayimg\.com/images/g/[A-Za-z0-9_-]+/s-l\d+\.(?:jpg|webp|png)', resp.text)
            if img_m:
                return re.sub(r's-l\d+', 's-l800', img_m.group(0))
            murl_m = re.search(r'murl&quot;:&quot;(https://[^&]+?\.(?:jpg|jpeg|png|webp))', resp.text)
            if murl_m:
                return murl_m.group(1)
    except Exception:
        pass
    return "https://placehold.co/600x400/0f172a/fbbf24?text=eBay+Vintage+Watch"

def analyze_with_ai(title, snippet, model_name):
    prompt = (
        f"Title: '{title}'. Info: '{snippet}'. "
        "Is this a watch or lot of watches (vintage, gold-plated, gold-filled, mechanical, quartz)? "
        "Reject ONLY empty boxes or loose straps with no watch. "
        "Reply ONLY valid JSON: {\"match\": true/false, \"note_ge\": \"მოკლე აღწერა ქართულად (1 წინადადება)\"}"
    )
    try:
        resp = ai_client.models.generate_content(model=model_name, contents=prompt)
        text = resp.text.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return bool(data.get("match", True)), data.get("note_ge", "ვინტაჟური საათების ლოტი eBay-დან")
    except Exception:
        return True, "ნაპოვნია eBay-ზე"

def main():
    print("=== სკანერი დაიწყო ===")
    model_name = get_working_model()
    settings_res = supabase.table("app_settings").select("*").eq("id", 1).execute()
    settings = settings_res.data[0] if settings_res.data else {}
    keywords = settings.get("keywords") or ["vintage gold plated watch lot"]

    existing_res = supabase.table("watch_lots").select("ebay_id").execute()
    existing_ids = {row["ebay_id"] for row in (existing_res.data or [])}
    print(f"ბაზაში უკვე არის: {len(existing_ids)} ლოტი.")

    added_total = 0
    for kw in keywords:
        items = search_via_bing_rss(kw)
        if len(items) < 3:
            extra = search_via_gemini_google(kw, model_name)
            for x in extra:
                if not any(i["ebay_id"] == x["ebay_id"] for i in items):
                    items.append(x)

        print(f"სიტყვა '{kw}' -> ნაპოვნია {len(items)} განცხადება")

        for item in items[:8]:
            if item["ebay_id"] in existing_ids:
                continue
            is_match, note_ge = analyze_with_ai(item["title"], item["snippet"], model_name)
            if is_match:
                img_url = get_thumbnail(item["title"])
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

    print(f"=== დასრულდა! სულ დაემატა {added_total} ახალი ლოტი ===")

if __name__ == "__main__":
    main()
