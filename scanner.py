import os
import re
import json
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

def fetch_image(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return Image.open(BytesIO(resp.read())).convert("RGB")
    except Exception:
        return None

def analyze_lot(title, img_url, ref_img):
    item_img = fetch_image(img_url)
    if not item_img:
        return False, ""

    prompt = (
        f"Analyze this eBay listing for a vintage watch buyer. Title: '{title}'. "
        "Check the photo carefully: Is this a lot/bundle of vintage watches (or valuable gold-plated / gold-filled vintage watches)? "
        "Reject empty boxes, single modern smartwatches, or watch bands without watches. "
        "If a reference watch image is also provided, check if the style/era is similar. "
        "Reply ONLY in valid JSON format: {\"match\": true/false, \"note_ge\": \"მოკლე აღწერა ქართულად (1 წინადადებით რა ჩანს ფოტოზე)\"}"
    )

    contents = [prompt, item_img]
    if ref_img:
        contents.append(ref_img)

    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
        )
        text = response.text.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return data.get("match", False), data.get("note_ge", "ვინტაჟური საათების ლოტი")
    except Exception as e:
        print("AI Error:", e)
        return False, ""

def main():
    settings_res = supabase.table("app_settings").select("*").eq("id", 1).execute()
    settings = settings_res.data[0] if settings_res.data else {}
    keywords = settings.get("keywords") or ["vintage gold plated watch lot"]
    ref_url = settings.get("reference_image_url") or ""
    ref_img = fetch_image(ref_url) if ref_url else None

    existing_res = supabase.table("watch_lots").select("ebay_id").execute()
    existing_ids = {row["ebay_id"] for row in (existing_res.data or [])}

    for kw in keywords:
        encoded_kw = urllib.parse.quote_plus(kw)
        rss_url = f"https://www.ebay.com/sch/i.html?_nkw={encoded_kw}&_sacat=260324&_sop=10&_rss=1"
        print(f"Checking: {kw}")

        try:
            req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
            xml_data = urllib.request.urlopen(req, timeout=15).read()
            root = ET.fromstring(xml_data)
        except Exception as e:
            print("RSS Error:", e)
            continue

        for item in root.findall(".//item")[:25]:
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
            price = price_match.group(1) if price_match else "აუქციონი / ფასი იხ. ლინკზე"

            is_match, note_ge = analyze_lot(title, img_url, ref_img)
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
                    "status": "new"
                }).execute()
                existing_ids.add(ebay_id)
                print(f"Added: {title}")

if __name__ == "__main__":
    main()
