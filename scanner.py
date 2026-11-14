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

def fetch_image(src):
    if not src:
        return None
    try:
        if src.startswith("data:image"):
            header, encoded = src.split(",", 1)
            return Image.open(BytesIO(base64.b64decode(encoded))).convert("RGB")
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return Image.open(BytesIO(resp.read())).convert("RGB")
    except Exception:
        return None

def analyze_for_keyword(title, img_url):
    item_img = fetch_image(img_url)
    if not item_img:
        return False, ""
    prompt = (
        f"Analyze this eBay listing. Title: '{title}'. "
        "Is this a lot/bundle of vintage watches (or valuable gold-plated / gold-filled vintage watches)? "
        "Reject empty boxes, single modern smartwatches, or watch bands without watches. "
        "Reply ONLY in valid JSON: {\"match\": true/false, \"note_ge\": \"მოკლე აღწერა ქართულად (1 წინადადებით)\"}"
    )
    try:
        resp = ai_client.models.generate_content(model="gemini-2.5-flash", contents=[prompt, item_img])
        text = resp.text.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return data.get("match", False), data.get("note_ge", "ვინტაჟური საათების ლოტი")
    except Exception:
        return False, ""

def analyze_for_photo(title, img_url, ref_img):
    item_img = fetch_image(img_url)
    if not item_img or not ref_img:
        return False, ""
    prompt = (
        f"Compare these two images. Image 1 is an eBay watch listing ('{title}'). Image 2 is the user's target reference watch style. "
        "Does Image 1 contain vintage watches/lots visually similar in style, case shape, gold plating, or era to Image 2? "
        "Reply ONLY in valid JSON: {\"match\": true/false, \"note_ge\": \"რით ჰგავს ატვირთულ ფოტოს (1 მოკლე წინადადება ქართულად)\"}"
    )
    try:
        resp = ai_client.models.generate_content(model="gemini-2.5-flash", contents=[prompt, item_img, ref_img])
        text = resp.text.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return data.get("match", False), data.get("note_ge", "ემთხვევა ატვირთულ ფოტოს")
    except Exception:
        return False, ""

def scan_rss(query, source_type, existing_ids, ref_img=None):
    encoded_kw = urllib.parse.quote_plus(query)
    rss_url = f"https://www.ebay.com/sch/i.html?_nkw={encoded_kw}&_sacat=260324&_sop=10&_rss=1"
    try:
        req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
        xml_data = urllib.request.urlopen(req, timeout=15).read()
        root = ET.fromstring(xml_data)
    except Exception:
        return

    for item in root.findall(".//item")[:20]:
        link = item.findtext("link") or ""
        id_match = re.search(r"/itm/(\d+)", link)
        ebay_id = id_match.group(1) if id_match else link
        if ebay_id in existing_ids:
            continue

        title = item.findtext("title") or "No title"
        desc = item.findtext("description") or ""
        img_match = re.search(r'<img[^>]+src="([^"]+)"', desc)
        img_url = img_match.group(1).replace("s-l225.jpg", "s-l800.jpg") if img_match else ""
        price_match = re.search(r"(\$[\d,\.]+)", desc)
        price = price_match.group(1) if price_match else "აუქციონი"

        if source_type == "photo":
            is_match, note_ge = analyze_for_photo(title, img_url, ref_img)
        else:
            is_match, note_ge = analyze_for_keyword(title, img_url)

        if is_match:
            supabase.table("watch_lots").insert({
                "ebay_id": ebay_id,
                "title": title,
                "price": price,
                "lot_type": "eBay Lot",
                "image_url": img_url,
                "ebay_url": link,
                "ai_match": True,
                "ai_note": note_ge,
                "status": "new",
                "search_source": source_type
            }).execute()
            existing_ids.add(ebay_id)

def main():
    settings_res = supabase.table("app_settings").select("*").eq("id", 1).execute()
    settings = settings_res.data[0] if settings_res.data else {}
    keywords = settings.get("keywords") or ["vintage gold plated watch lot"]
    ref_src = settings.get("reference_image_url") or ""
    ref_img = fetch_image(ref_src)

    existing_res = supabase.table("watch_lots").select("ebay_id").execute()
    existing_ids = {row["ebay_id"] for row in (existing_res.data or [])}

    # 1. სიტყვებით ძიების ფეიჯისთვის
    for kw in keywords:
        scan_rss(kw, "keyword", existing_ids)

    # 2. ფოტოთი ძიების ფეიჯისთვის (თუ ფოტო ატვირთულია)
    if ref_img:
        for base_q in ["vintage gold watch lot", "antique watches bundle"]:
            scan_rss(base_q, "photo", existing_ids, ref_img=ref_img)

if __name__ == "__main__":
    main()
