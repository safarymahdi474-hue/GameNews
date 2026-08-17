"""
ربات اخبار گیم فارسی برای تلگرام — نسخه‌ی خودکار با Gemini
------------------------------------------------------------
این نسخه به‌طور کامل خودکار عمل می‌کنه:
1) از فیدهای RSS سایت‌های خبری گیم (IGN, GameSpot, Eurogamer, PC Gamer)
   جدیدترین خبرها رو می‌گیره (اخبار انیمه دیگه پیگیری نمی‌شه).
2) هر خبر رو به Gemini می‌ده تا:
   - تشخیص بده خبر ارزش انتشار داره یا نه (تخفیف/لیست/ریویو صرف = رد بشه)
   - عنوان و متن رو به فارسیِ روان، جذاب و با لحن خبری-هیجانی بازنویسی کنه
   - چند هشتگ مرتبط فارسی/انگلیسی پیشنهاد بده
3) عکس خبر رو (از خود فید یا og:image صفحه) پیدا می‌کنه.
4) یک استیکر گیمینگ تصادفی (از پک‌های تنظیم‌شده) + عکس خبر + کپشن جذاب رو
   مستقیم به کانال تنظیم‌شده می‌فرسته.
5) برای جلوگیری از تکرار، لینک خبرهای فرستاده‌شده رو تو sent_news.json نگه می‌داره.

نکته: این نسخه دیگه نیازی به «گروه پیش‌نویس» و دستور /post نداره؛ همه‌چیز خودکاره.
اگه هنوز می‌خوای قبل از انتشار خبرها رو تأیید کنی، می‌تونی DRY_RUN=1 بذاری تا
فقط تو ترمینال چاپ بشن و به کانال ارسال نشن.
"""

import os
import json
import random
import asyncio
import logging
import re
from datetime import datetime, timezone

import feedparser
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
from telegram import Bot
from telegram.error import TelegramError

# ---------------------------------------------------------------------------
# تنظیمات
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]                       # مثل @yourchannel یا -100...
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# اسم کوتاه پک‌های استیکر گیمینگ (بخش بعد از t.me/addstickers/ ) — با کاما جدا کن
STICKER_PACK_NAMES = [
    s.strip() for s in os.environ.get("STICKER_PACK_NAMES", "").split(",") if s.strip()
]

# هر چند دقیقه یک‌بار چک کنه (پیش‌فرض ۱۵ دقیقه)
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "15"))

# حداکثر چند خبر تو هر دور بررسی بشه (برای رعایت محدودیت رایگان Gemini)
MAX_ITEMS_PER_RUN = int(os.environ.get("MAX_ITEMS_PER_RUN", "5"))

# اگه ۱ باشه، به‌جای ارسال واقعی فقط تو کنسول چاپ می‌کنه (تست بدون ریسک)
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

SENT_NEWS_FILE = "sent_news.json"

RSS_FEEDS = {
    "IGN": "https://feeds.ign.com/ign/games-all",
    "GameSpot": "https://www.gamespot.com/feeds/game-news/",
    "Eurogamer": "https://www.eurogamer.net/feed",
    "PC Gamer": "https://www.pcgamer.com/rss/",
}

GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("gamenews-bot")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(GEMINI_MODEL_NAME)

bot = Bot(token=BOT_TOKEN)

# ---------------------------------------------------------------------------
# ذخیره‌سازی لینک‌های ارسال‌شده
# ---------------------------------------------------------------------------

def load_sent_links() -> set:
    if os.path.exists(SENT_NEWS_FILE):
        try:
            with open(SENT_NEWS_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError):
            log.warning("فایل sent_news.json خراب بود، از صفر شروع می‌کنیم.")
    return set()


def save_sent_links(links: set) -> None:
    with open(SENT_NEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(links), f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# گرفتن خبرهای جدید از فیدها
# ---------------------------------------------------------------------------

def fetch_latest_entries() -> list:
    """جدیدترین خبر هر منبع رو برمی‌گردونه (چندتا آیتم از هر فید)."""
    entries = []
    for source_name, url in RSS_FEEDS.items():
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries[:3]:  # ۳ خبر اول هر منبع کافیه
                entries.append({
                    "source": source_name,
                    "title": entry.get("title", "").strip(),
                    "link": entry.get("link", "").strip(),
                    "summary": BeautifulSoup(
                        entry.get("summary", entry.get("description", "")),
                        "html.parser",
                    ).get_text().strip(),
                    "image": extract_image_from_entry(entry),
                })
        except Exception as e:
            log.error("خطا در خوندن فید %s: %s", source_name, e)
    return entries


def extract_image_from_entry(entry) -> str | None:
    # 1) media_content / media_thumbnail (استاندارد اکثر فیدهای خبری گیم)
    if hasattr(entry, "media_content") and entry.media_content:
        url = entry.media_content[0].get("url")
        if url:
            return url
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        url = entry.media_thumbnail[0].get("url")
        if url:
            return url
    # 2) enclosure
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image"):
            return link.get("href")
    # 3) عکس داخل خود summary/content
    html = entry.get("summary", "") + str(entry.get("content", ""))
    match = re.search(r'<img[^>]+src="([^"]+)"', html)
    if match:
        return match.group(1)
    return None


def fetch_og_image(page_url: str) -> str | None:
    """اگه فید عکس نداشت، از og:image خود صفحه‌ی خبر می‌گیریم."""
    try:
        resp = requests.get(page_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "html.parser")
        tag = soup.find("meta", property="og:image")
        if tag and tag.get("content"):
            return tag["content"]
    except Exception as e:
        log.warning("نشد og:image رو از %s بگیریم: %s", page_url, e)
    return None


# ---------------------------------------------------------------------------
# بازنویسی جذاب با Gemini
# ---------------------------------------------------------------------------

GEMINI_PROMPT = """
تو یک ادمین حرفه‌ای و باتجربه‌ی یک کانال خبری گیمینگ فارسی هستی که مخاطب‌های
جوان و پرشور داره. یک خبر گیمینگ به زبان انگلیسی بهت می‌دم. باید:

1. تشخیص بدی این خبر ارزش انتشار داره یا نه. خبرهایی مثل «تخفیف فروشگاه»،
   «لیست بهترین بازی‌ها»، «ریویوی یک بازی قدیمی»، «راهنمای گیم‌پلی» ارزش کم دارن
   و نباید منتشر بشن. خبر مهم یعنی: معرفی/رونمایی بازی جدید، آپدیت بزرگ،
   تاریخ انتشار، تریلر جدید، اتفاق مهم صنعت گیم، اخبار شرکت‌های بزرگ گیم.

2. اگه ارزش انتشار داره، عنوان و متن رو کاملاً به فارسیِ روان، خودمونی ولی حرفه‌ای
   و به‌شدت جذاب بازنویسی کن — نه ترجمه‌ی کلمه‌به‌کلمه. از لحن هیجان‌انگیز و
   ریتم خبری استفاده کن. تو عنوان و لابه‌لای متن از **ایموجی‌های معمولیِ کیبورد**
   (مثل 🎮🔥🚀💥🕹️⚡️🆕👀💣🏆) به‌شکل شیک و طبیعی استفاده کن تا خوندنش نشاط
   داشته باشه — ولی زیاده‌روی نکن (در کل متن حداکثر ۴-۶ ایموجی، نه بیشتر، و
   هیچ‌وقت ایموجی پشت‌سرهم توی یک جا).

3. متن نهایی باید ۳ تا ۵ جمله‌ی کوتاه، خوش‌ریتم و خوش‌خوان باشه، نه یک پاراگراف
   طولانی و خسته‌کننده. هر جمله باید حس هیجان و تازگیِ خبر رو منتقل کنه، انگار
   داری برای یه دوست گیمر تعریف می‌کنی، نه این‌که داری گزارش رسمی می‌نویسی.

4. سه تا پنج هشتگ مرتبط فارسی/انگلیسی هم پیشنهاد بده (مثل #گیم #PS5 #بازی_جدید).

فقط و فقط یک JSON خام با این ساختار برگردون، بدون توضیح اضافه و بدون ```:
{{
  "is_worth_posting": true/false,
  "title_fa": "عنوان جذاب فارسی",
  "body_fa": "متن جذاب فارسی",
  "hashtags": ["#تگ۱", "#تگ۲"]
}}

عنوان خبر: {title}
متن خبر: {summary}
منبع: {source}
"""


def rewrite_with_gemini(item: dict) -> dict | None:
    prompt = GEMINI_PROMPT.format(
        title=item["title"], summary=item["summary"][:1500], source=item["source"]
    )
    try:
        response = model.generate_content(prompt)
        raw = response.text.strip()
        raw = re.sub(r"^```json|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        return data
    except Exception as e:
        log.error("خطا در پردازش Gemini برای '%s': %s", item["title"], e)
        return None


# ---------------------------------------------------------------------------
# استیکر تصادفی
# ---------------------------------------------------------------------------

async def get_random_sticker_file_id() -> str | None:
    if not STICKER_PACK_NAMES:
        return None
    pack_name = random.choice(STICKER_PACK_NAMES)
    try:
        sticker_set = await bot.get_sticker_set(pack_name)
        if sticker_set.stickers:
            return random.choice(sticker_set.stickers).file_id
    except TelegramError as e:
        log.warning("نشد پک استیکر '%s' رو بگیریم: %s", pack_name, e)
    return None


# ---------------------------------------------------------------------------
# ارسال به کانال
# ---------------------------------------------------------------------------

async def send_news_to_channel(item: dict, rewritten: dict) -> None:
    caption = f"<b>{rewritten['title_fa']}</b>\n\n{rewritten['body_fa']}\n\n"
    caption += " ".join(rewritten.get("hashtags", []))
    # ایموجی‌ها رو خود Gemini داخل متن قرار می‌ده؛ اینجا چیزی اضافه نمی‌کنیم

    image_url = item.get("image") or fetch_og_image(item["link"])

    if DRY_RUN:
        log.info("—— DRY RUN ——\n%s\nعکس: %s\n", caption, image_url)
        return

    sticker_id = await get_random_sticker_file_id()
    if sticker_id:
        try:
            await bot.send_sticker(chat_id=CHANNEL_ID, sticker=sticker_id)
        except TelegramError as e:
            log.warning("ارسال استیکر ناموفق بود: %s", e)

    try:
        if image_url:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image_url,
                caption=caption,
                parse_mode="HTML",
            )
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=caption, parse_mode="HTML")
        log.info("ارسال شد: %s", rewritten["title_fa"])
    except TelegramError as e:
        log.error("ارسال خبر به کانال ناموفق بود: %s", e)


# ---------------------------------------------------------------------------
# حلقه‌ی اصلی
# ---------------------------------------------------------------------------

async def run_once() -> None:
    sent_links = load_sent_links()
    entries = fetch_latest_entries()

    new_entries = [e for e in entries if e["link"] and e["link"] not in sent_links]
    new_entries = new_entries[:MAX_ITEMS_PER_RUN]

    if not new_entries:
        log.info("خبر جدیدی نبود.")
        return

    for item in new_entries:
        log.info("در حال بررسی: [%s] %s", item["source"], item["title"])
        rewritten = rewrite_with_gemini(item)
        sent_links.add(item["link"])  # چه منتشر بشه چه نشه، دیگه دوباره چک نمی‌شه

        if not rewritten or not rewritten.get("is_worth_posting"):
            log.info("رد شد (کم‌ارزش یا خطا): %s", item["title"])
            continue

        await send_news_to_channel(item, rewritten)
        await asyncio.sleep(3)  # فاصله‌ی کوچیک بین پیام‌ها

    save_sent_links(sent_links)


async def main_loop() -> None:
    log.info("ربات اخبار گیم شروع به کار کرد. هر %s دقیقه چک می‌کنه.", CHECK_INTERVAL_MINUTES)
    while True:
        try:
            await run_once()
        except Exception as e:
            log.exception("خطای غیرمنتظره در حلقه‌ی اصلی: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    asyncio.run(main_loop())
