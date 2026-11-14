import os
import re
import json
import html
import urllib.parse
from curl_cffi import requests as cffi_requests
from google import genai
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

def search_ebay_via_search_engines(query):
    """იღებს რეალურ eBay ლოტებს საძიებო სისტემებიდან (ebay.com-ზე შესვლის გარეშე, 0 ბლოკით)"""
    results = []
    q = urllib.parse.quote_plus(f'site:ebay.com/itm {query}')
    
    # წყარო 1: DuckDuckGo HTML
    try:
        resp = cffi_requests.get(f"https://html.duckduckgo.com/html/?q={q}", impersonate="chrome", timeout=15)
        if resp.status_code == 200:
            # ვპოულობთ თითოეულ ბლოკს სათაურითა და ლინკით
            links = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', resp.text, re.S)
            snippets = re.findall(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.S)
            for i, (raw_href, raw_title) in enumerate(links):
                decoded_url = urllib.parse.unquote(raw_href)
                id_m = re.search(r'ebay\.com/itm/(?:[^/]+/)?(\d{9,14})', decoded_url)
                if not id_m:
                    continue
                eid = id_m.group(1)
                clean_title = html.unescape(re.sub(r'<[^>]+>', '', raw_title)).replace(" | eBay", "").strip()
                snip = html.unescape(re.sub(r'<[^>]+>', '', snippets[i])) if i < len(snippets) else ""
                price_m = re.search(r'(\$\s*[\d,\.]+|US\s*\$[\d,\.]+)', clean_title + " " + snip)
                price = price_m.group(1).replace("US ", "") if price_m else "ფასი იხ. eBay-ზე"
                if not any(r["ebay_id"] == eid for r in results):
                    results.append({
                        "ebay_id": eid,
                        "title": clean_title,
                        "snippet": snip,
                        "price": price,
                        "ebay_url": f"https://www.ebay.com/itm/{eid}"
                    })
    except Exception as e:
        print(f"DDG err: {e}")

    # წყარო 2: Bing Web Search (თუ მეტი შედეგი გვინდა)
    if len(results) < 5:
        try:
            resp = cffi_requests.get(f"https://www.bing.com/search?q={q}", impersonate="chrome", timeout=15)
            if resp.status_code == 200:
                items = re.findall(r'<h2><a[^>]+href="(https://www\.ebay\.com/itm/[^"]+)"[^>]*>(.*?)</a></h2>', resp.text, re.S)
                for raw_url, raw_title in items:
                    id_m = re.search(r'/itm/(?:[^/]+/)?(\d{9,14})', raw_url)
                    if not id_m:
                        continue
                    eid = id_m.group(1)
                    clean_title = html.unescape(re.sub(r'<[^>]+>', '', raw_title)).replace(" | eBay", "").strip()
                    if not any(r["ebay_id"] == eid for r in results):
                        results.append({
                            "ebay_id": eid,
                            "title": clean_title,
                            "snippet": "",
                            "price": "ფასი იხ. eBay-ზე",
                            "ebay_url": f"https://www.ebay.com/itm/{eid}"
                        })
        except Exception as e:
            print(f"Bing err: {e}")

    return results[:15]

def get_thumbnail_for_title(title):
    """პოულობს ნივთის რეალურ ფოტოს Bing Images-იდან (ebay.com-ის გვერდის ავლით)"""
    try:
        q = urllib.parse.quote_plus(f"ebay {title[:70]}")
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

def analyze_with_ai(title, snippet):
    prompt = (
        f"Title: '{title}'. Description: '{snippet}'. "
        "Is this a watch or lot of watches (vintage, gold-plated, gold-filled, mechanical, quartz)? "
        "Reject ONLY empty boxes or loose straps with no watch. "
        "Reply ONLY valid JSON: {\"match\": true/false, \"note_ge\": \"მოკლე აღწერა ქართულად (1 წინადადება)\"}"
    )
    for model_id in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-3.8-flash"]:
        try:
            resp = ai_client.models.generate_content(model=model_id, contents=prompt)
            text = resp.text.strip().replace("```json", "").replace("```", "").strip()
            data = json.loads(text)
            return bool(data.get("match", True)), data.get("note_ge", "ვინტაჟური საათების ლოტი eBay-დან")
        except Exception:
            continue
    return True, "ნაპოვნია eBay-ზე"

def main():
    print("=== სკანერი დაიწყო (პირდაპირი ინდექსით, 0 ბლოკით) ===")
    settings_res = supabase.table("app_settings").select("*").eq("id", 1).execute()
    settings = settings_res.data[0] if settings_res.data else {}
    keywords = settings.get("keywords") or ["vintage gold plated watch lot"]

    existing_res = supabase.table("watch_lots").select("ebay_id").execute()
    existing_ids = {row["ebay_id"] for row in (existing_res.data or [])}
    print(f"ბაზაში უკვე არის: {len(existing_ids)} ლოტი.")

    added_total = 0
    for kw in keywords:
        items = search_ebay_via_search_engines(kw)
        print(f"სიტყვა '{kw}' -> ნაპოვნია {len(items)} რეალური eBay განცხადება")

        for item in items[:8]:
            if item["ebay_id"] in existing_ids:
                continue
            is_match, note_ge = analyze_with_ai(item["title"], item["snippet"])
            if is_match:
                img_url = get_thumbnail_for_title(item["title"])
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
