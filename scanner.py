import os
import re
import json
import html
import time
import urllib.parse
from curl_cffi import requests as cffi_requests
from google import genai
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

def search_ebay_ddg(query):
    """DuckDuckGo HTML & Lite — უფასო, სწრაფი და ნამდვილად აბრუნებს ebay.com/itm/ ლინკებს"""
    results = []
    search_queries = [
        f'site:ebay.com/itm {query}',
        f'site:ebay.com/itm "{query}"'
    ]

    for sq in search_queries:
        encoded_q = urllib.parse.quote_plus(sq)
        url = f"https://html.duckduckgo.com/html/?q={encoded_q}"
        try:
            resp = cffi_requests.get(url, impersonate="chrome", timeout=15)
            if resp.status_code != 200:
                continue

            # DuckDuckGo-ს თითოეული შედეგის ბლოკი
            blocks = re.split(r'class="result results_links', resp.text)[1:]
            for blk in blocks:
                # ვპოულობთ ლინკს (პირდაპირს ან uddg= პარამეტრში ჩასმულს)
                href_m = re.search(r'class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
                if not href_m:
                    continue
                raw_href, raw_title = href_m.group(1), href_m.group(2)

                # ვშიფრავთ uddg= ლინკს
                if "uddg=" in raw_href:
                    uddg_m = re.search(r'uddg=([^&"]+)', raw_href)
                    real_url = urllib.parse.unquote(uddg_m.group(1)) if uddg_m else raw_href
                else:
                    real_url = urllib.parse.unquote(raw_href)

                id_m = re.search(r'ebay\.com/itm/(?:[^/?#]+/)?(\d{9,14})', real_url)
                if not id_m:
                    continue
                eid = id_m.group(1)

                clean_title = html.unescape(re.sub(r'<[^>]+>', '', raw_title)).replace(" | eBay", "").strip()
                snip_m = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', blk, re.S)
                snippet = html.unescape(re.sub(r'<[^>]+>', '', snip_m.group(1))).strip() if snip_m else ""

                price_m = re.search(r'(\$\s*[\d,\.]+|US\s*\$[\d,\.]+)', clean_title + " " + snippet)
                price = price_m.group(1).replace("US ", "") if price_m else "ფასი იხ. eBay-ზე"

                if not any(r["ebay_id"] == eid for r in results):
                    results.append({
                        "ebay_id": eid,
                        "title": clean_title or f"eBay Watch #{eid}",
                        "snippet": snippet,
                        "price": price,
                        "ebay_url": f"https://www.ebay.com/itm/{eid}"
                    })
        except Exception as e:
            print(f"DDG notice: {e}")

        if len(results) >= 10:
            break
        time.sleep(1)

    return results[:15]

def get_thumbnail(title):
    """იღებს რეალურ ფოტოს Bing Images-იდან"""
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

def analyze_lot(title, snippet):
    """ჩვეულებრივი Gemini ტექსტური ანალიზი (Search Grounding-ის გარეშე, უფასო გეგმაზე მუშაობს)"""
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
    # თუ უფასო API ლიმიტი დროებით გადატვირთულია, მაინც ვინახავთ ნაპოვნ საათს
    return True, "ნაპოვნია eBay-ზე (" + (snippet[:90] if snippet else title[:90]) + ")"

def main():
    print("=== სკანერი დაიწყო (429 შეცდომის გარეშე) ===")
    settings_res = supabase.table("app_settings").select("*").eq("id", 1).execute()
    settings = settings_res.data[0] if settings_res.data else {}
    keywords = settings.get("keywords") or ["vintage gold plated watch lot"]

    existing_res = supabase.table("watch_lots").select("ebay_id").execute()
    existing_ids = {row["ebay_id"] for row in (existing_res.data or [])}
    print(f"ბაზაში უკვე არის: {len(existing_ids)} ლოტი.")

    added_total = 0
    for kw in keywords:
        items = search_ebay_ddg(kw)
        print(f"სიტყვა '{kw}' -> ნაპოვნია {len(items)} რეალური განცხადება")

        for item in items[:8]:
            if item["ebay_id"] in existing_ids:
                continue
            is_match, note_ge = analyze_lot(item["title"], item["snippet"])
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
            time.sleep(0.5)

    print(f"=== დასრულდა! სულ დაემატა {added_total} ახალი ლოტი ===")

if __name__ == "__main__":
    main()
