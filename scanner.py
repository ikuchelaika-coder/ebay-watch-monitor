import os
import re
import json
import base64
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from io import BytesIO
from PIL import Image
from google import genai
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def fetch_image(src):
    if not src:
        return None
    try:
        if src.startswith("data:image"):
            _, encoded = src.split(",", 1)
            return Image.open(BytesIO(base64.b64decode(encoded))).convert("RGB")
        req = urllib.request.Request(src, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=12) as resp:
            return Image.open(BytesIO(resp.read())).convert("RGB")
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
    except Exception as e:
        print(f"Gemini error: {e}")
        return False, ""

def get_ebay_candidates(query):
    candidates = []
    encoded = urllib.parse.quote_plus(query)

    # მეთოდი 1: eBay RSS (თუ არ ბლოკავს)
    rss_url = f"https://www.ebay.com/sch/i.html?_nkw={encoded}&_sacat=260324&_sop=10&_rss=1"
    try:
        req = urllib.request.Request(rss_url, headers=HEADERS)
        xml_data = urllib.request.urlopen(req, timeout=15).read()
        root = ET.fromstring(xml_data)
        for item in root.findall(".//item")[:20]:
            link = item.findtext("link") or ""
            id_m = re.search(r"/itm/(\d+)", link)
            ebay_id = id_m.group(1) if id_m else ""
            if not ebay_id:
                continue
            title = item.findtext("title") or ""
            desc = item.findtext("description") or ""
            img_m = re.search(r'<img[^>]+src="([^"]+)"', desc)
            img_url = img_m.group(1).replace("s-l225.jpg", "s-l800.jpg") if img_m else ""
            price_m = re.search(r"(\$[\d,\.]+)", desc)
            price = price_m.group(1) if price_m else "ფასი იხ. ლინკზე"
            candidates.append({"ebay_id": ebay_id, "title": title, "price": price, "image_url": img_url, "ebay_url": f"https://www.ebay.com/itm/{ebay_id}"})
    except Exception as e:
        print(f"RSS skipped: {e}")

    if candidates:
        return candidates

    # მეთოდი 2: eBay პირდაპირი საძიებო გვერდის HTML პარსინგი (მუშაობს როცა RSS გათიშულია)
    html_url = f"https://www.ebay.com/sch/i.html?_nkw={encoded}&_sacat=260324&_sop=10"
    try:
        req = urllib.request.Request(html_url, headers=HEADERS)
        html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="ignore")
        # ვპოულობთ ყველა itm ID-ს და მიმდებარე მონაცემებს
        blocks = re.split(r'data-view="mi:1686\|iid:\d+"', html)[1:25]
        for blk in blocks:
            id_m = re.search(r'https://www\.ebay\.com/itm/(\d+)', blk)
            if not id_m:
                continue
            ebay_id = id_m.group(1)
            title_m = re.search(r'alt="([^"]{10,160})"', blk)
            title = title_m.group(1) if title_m else f"Vintage Watch Lot #{ebay_id}"
            if "Shop on eBay" in title:
                continue
            img_m = re.search(r'src="(https://i\.ebayimg\.com/images/g/[^"]+)"', blk)
            img_url = img_m.group(1).replace("s-l225", "s-l800").replace("s-l300", "s-l800").replace("s-l500", "s-l800") if img_m else ""
            price_m = re.search(r'(\$\d[\d,\.]*)', blk)
            price = price_m.group(1) if price_m else "აუქციონი / ფასი"
            if img_url:
                candidates.append({"ebay_id": ebay_id, "title": title, "price": price, "image_url": img_url, "ebay_url": f"https://www.ebay.com/itm/{ebay_id}"})
    except Exception as e:
        print(f"HTML parse error: {e}")

    return candidates

def main():
    print("=== სკანერი დაიწყო ===")
    settings_res = supabase.table("app_settings").select("*").eq("id", 1).execute()
    settings = settings_res.data[0] if settings_res.data else {}
    keywords = settings.get("keywords") or ["vintage gold plated watch lot"]
    ref_src = settings.get("reference_image_url") or ""
    ref_img = fetch_image(ref_src)

    existing_res = supabase.table("watch_lots").select("ebay_id").execute()
    existing_ids = {row["ebay_id"] for row in (existing_res.data or [])}
    print(f"ბაზაში უკვე არის: {len(existing_ids)} ლოტი. მოწმდება სიტყვები: {keywords}")

    added_total = 0
    for kw in keywords:
        cands = get_ebay_candidates(kw)
        print(f"სიტყვა '{kw}' -> ნაპოვნია {len(cands)} კანდიდატი eBay-ზე")
        for c in cands:
            if c["ebay_id"] in existing_ids:
                continue
            is_match, note_ge = analyze_lot(c["title"], c["image_url"])
            if is_match:
                try:
                    supabase.table("watch_lots").insert({
                        "ebay_id": c["ebay_id"],
                        "title": c["title"],
                        "price": c["price"],
                        "lot_type": "eBay Lot",
                        "image_url": c["image_url"],
                        "ebay_url": c["ebay_url"],
                        "ai_match": True,
                        "ai_note": note_ge,
                        "status": "new",
                        "search_source": "keyword"
                    }).execute()
                    existing_ids.add(c["ebay_id"])
                    added_total += 1
                    print(f"✅ დაემატა: {c['title'][:50]}")
                except Exception as db_err:
                    print(f"DB Insert error: {db_err}")

    if ref_img:
        for base_q in ["vintage gold watch lot", "antique watches bundle"]:
            cands = get_ebay_candidates(base_q)
            for c in cands:
                if c["ebay_id"] in existing_ids:
                    continue
                is_match, note_ge = analyze_lot(c["title"], c["image_url"], ref_img=ref_img, is_photo_mode=True)
                if is_match:
                    try:
                        supabase.table("watch_lots").insert({
                            "ebay_id": c["ebay_id"],
                            "title": c["title"],
                            "price": c["price"],
                            "lot_type": "eBay Lot",
                            "image_url": c["image_url"],
                            "ebay_url": c["ebay_url"],
                            "ai_match": True,
                            "ai_note": note_ge,
                            "status": "new",
                            "search_source": "photo"
                        }).execute()
                        existing_ids.add(c["ebay_id"])
                        added_total += 1
                    except Exception as db_err:
                        print(f"DB Insert error: {db_err}")

    print(f"=== დასრულდა! სულ დაემატა {added_total} ახალი ლოტი ===")

if __name__ == "__main__":
    main()
