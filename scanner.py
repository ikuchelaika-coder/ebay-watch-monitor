import os
import re
import json
import base64
import urllib.parse
from io import BytesIO
from PIL import Image
from curl_cffi import requests as cffi_requests
from google import genai
from google.genai import types
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

def fetch_image(src):
    if not src:
        return None
    try:
        if src.startswith("data:image"):
            _, encoded = src.split(",", 1)
            return Image.open(BytesIO(base64.b64decode(encoded))).convert("RGB")
        resp = cffi_requests.get(src, impersonate="chrome", timeout=12)
        if resp.status_code == 200:
            return Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception:
        return None
    return None

def find_ebay_ids_via_ddg(query):
    """DuckDuckGo HTML-დან იღებს რეალურ ebay.com/itm/ ID-ებს 403 ბლოკის გარეშე"""
    ids = []
    search_q = urllib.parse.quote_plus(f'site:ebay.com/itm "{query}"')
    url = f"https://html.duckduckgo.com/html/?q={search_q}"
    try:
        resp = cffi_requests.get(url, impersonate="chrome", timeout=15)
        if resp.status_code == 200:
            found = re.findall(r'ebay\.com%2Fitm%2F(\d{9,14})|ebay\.com/itm/(\d{9,14})', resp.text)
            for g1, g2 in found:
                eid = g1 or g2
                if eid and eid not in ids:
                    ids.append(eid)
    except Exception as e:
        print(f"DDG notice: {e}")
    return ids[:15]

def find_ebay_ids_via_gemini_search(query):
    """Gemini Google Search-ით პოულობს რეალურ ebay.com/itm/ ლინკებს"""
    ids = []
    try:
        resp = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Find active eBay listings (ebay.com/itm/) for: {query}. List the exact ebay.com/itm/ URLs.",
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}]
            )
        )
        text = resp.text or ""
        # ასევე ვიღებთ grounding metadata-დან URL-ებს
        if resp.candidates and resp.candidates[0].grounding_metadata:
            chunks = resp.candidates[0].grounding_metadata.grounding_chunks or []
            for ch in chunks:
                if ch.web and ch.web.uri:
                    text += " " + ch.web.uri
        for eid in re.findall(r'ebay\.com/itm/(?:[^/\s"\']+/)?(\d{9,14})', text):
            if eid not in ids:
                ids.append(eid)
    except Exception as e:
        print(f"Gemini search notice: {e}")
    return ids[:15]

def fetch_ebay_item_details(ebay_id):
    """ხსნის კონკრეტულ eBay ნივთის გვერდს (itm/{id}) და იღებს სათაურს, ფასს და მთავარ ფოტოს"""
    url = f"https://www.ebay.com/itm/{ebay_id}"
    try:
        resp = cffi_requests.get(url, impersonate="chrome", timeout=15)
        if resp.status_code != 200:
            return None
        html = resp.text

        # სათაური og:title-დან
        title_m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
        if not title_m:
            title_m = re.search(r'<title>([^<]+)</title>', html)
        title = title_m.group(1).replace(" | eBay", "").strip() if title_m else f"eBay Watch #{ebay_id}"

        # მთავარი ფოტო og:image-დან
        img_m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
        img_url = img_m.group(1) if img_m else ""
        if img_url:
            img_url = re.sub(r's-l\d+', 's-l800', img_url)

        # ფასი
        price_m = re.search(r'"price"\s*:\s*"?([\d,\.]+)"?', html)
        curr_m = re.search(r'"priceCurrency"\s*:\s*"([A-Z]{3})"', html)
        if price_m:
            curr = "$" if (not curr_m or curr_m.group(1) == "USD") else curr_m.group(1) + " "
            price = f"{curr}{price_m.group(1)}"
        else:
            p_fallback = re.search(r'US\s*\$([\d,\.]+)', html)
            price = f"${p_fallback.group(1)}" if p_fallback else "ფასი იხ. eBay-ზე"

        if not img_url or "ebaystatic" in img_url:
            return None

        return {
            "ebay_id": ebay_id,
            "title": title,
            "price": price,
            "image_url": img_url,
            "ebay_url": url
        }
    except Exception:
        return None

def analyze_lot(title, img_url, ref_img=None, is_photo_mode=False):
    item_img = fetch_image(img_url)
    if not item_img:
        return False, ""

    if is_photo_mode and ref_img:
        prompt = (
            f"Compare Image 1 (eBay listing: '{title}') with Image 2 (user reference watch). "
            "Are they similar vintage watch styles/lots? "
            "Reply ONLY valid JSON: {\"match\": true/false, \"note_ge\": \"მოკლე აღწერა ქართულად (1 წინადადება)\"}"
        )
        contents = [prompt, item_img, ref_img]
    else:
        prompt = (
            f"Analyze this eBay watch listing: '{title}'. "
            "Is this a vintage watch, lot/bundle of vintage watches, or gold-plated/gold-filled watch? "
            "Reject empty boxes, smartwatches, or loose straps only. "
            "Reply ONLY valid JSON: {\"match\": true/false, \"note_ge\": \"მოკლე აღწერა ქართულად (1 წინადადებით რა ჩანს ფოტოზე)\"}"
        )
        contents = [prompt, item_img]

    try:
        resp = ai_client.models.generate_content(model="gemini-2.5-flash", contents=contents)
        text = resp.text.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return bool(data.get("match", False)), data.get("note_ge", "ვინტაჟური საათების ლოტი")
    except Exception:
        return True, "ვინტაჟური საათი / ლოტი eBay-დან"

def main():
    print("=== სკანერი დაიწყო (შემოვლითი არხით) ===")
    settings_res = supabase.table("app_settings").select("*").eq("id", 1).execute()
    settings = settings_res.data[0] if settings_res.data else {}
    keywords = settings.get("keywords") or ["vintage gold plated watch lot"]
    ref_src = settings.get("reference_image_url") or ""
    ref_img = fetch_image(ref_src)

    existing_res = supabase.table("watch_lots").select("ebay_id").execute()
    existing_ids = {row["ebay_id"] for row in (existing_res.data or [])}
    print(f"ბაზაში უკვე არის: {len(existing_ids)} ლოტი.")

    added_total = 0
    for kw in keywords:
        ids = find_ebay_ids_via_ddg(kw)
        if not ids:
            ids = find_ebay_ids_via_gemini_search(kw)
        print(f"სიტყვა '{kw}' -> ნაპოვნია {len(ids)} eBay ID")

        for eid in ids[:8]:
            if eid in existing_ids:
                continue
            item = fetch_ebay_item_details(eid)
            if not item:
                continue
            is_match, note_ge = analyze_lot(item["title"], item["image_url"])
            if is_match:
                try:
                    supabase.table("watch_lots").insert({
                        "ebay_id": item["ebay_id"],
                        "title": item["title"],
                        "price": item["price"],
                        "lot_type": "eBay Lot",
                        "image_url": item["image_url"],
                        "ebay_url": item["ebay_url"],
                        "ai_match": True,
                        "ai_note": note_ge,
                        "status": "new",
                        "search_source": "keyword"
                    }).execute()
                    existing_ids.add(eid)
                    added_total += 1
                    print(f"✅ დაემატა: {item['title'][:55]} ({item['price']})")
                except Exception as db_err:
                    print(f"DB Error: {db_err}")

    if ref_img:
        for base_q in ["vintage gold watch lot", "antique watches bundle"]:
            ids = find_ebay_ids_via_ddg(base_q) or find_ebay_ids_via_gemini_search(base_q)
            for eid in ids[:8]:
                if eid in existing_ids:
                    continue
                item = fetch_ebay_item_details(eid)
                if not item:
                    continue
                is_match, note_ge = analyze_lot(item["title"], item["image_url"], ref_img=ref_img, is_photo_mode=True)
                if is_match:
                    try:
                        supabase.table("watch_lots").insert({
                            "ebay_id": item["ebay_id"],
                            "title": item["title"],
                            "price": item["price"],
                            "lot_type": "eBay Lot",
                            "image_url": item["image_url"],
                            "ebay_url": item["ebay_url"],
                            "ai_match": True,
                            "ai_note": note_ge,
                            "status": "new",
                            "search_source": "photo"
                        }).execute()
                        existing_ids.add(eid)
                        added_total += 1
                    except Exception as db_err:
                        print(f"DB Error: {db_err}")

    print(f"=== დასრულდა! სულ დაემატა {added_total} ახალი ლოტი ===")

if __name__ == "__main__":
    main()
