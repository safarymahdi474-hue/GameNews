"""
ربات اخبار گیم فارسی برای تلگرام
----------------------------------
این اسکریپت جدیدترین اخبار دنیای گیم رو از چند منبع معتبر می‌گیره،
با گوگل ترنسلیت به فارسی برمی‌گردونه و همراه با عکس (اگه موجود باشه)
به یه کانال تلگرام می‌فرسته.

قبل از اجرا حتماً بخش تنظیمات (CONFIG) رو کامل کنید.
"""

import os
import re
import html
import json
import time
import logging

import feedparser
import requests
from deep_translator import GoogleTranslator

# ============================================================
# تنظیمات (این‌ها رو با اطلاعات خودتون پر کنید)
# ============================================================

# توکن ربات تلگرام - از @BotFather بگیرید
BOT_TOKEN = os.environ.get("BOT_TOKEN", "REPLACE_WITH_YOUR_BOT_TOKEN")

# آیدی یا یوزرنیم کانال مقصد
# اگه کانال پابلیکه: "@yourchannel"
# اگه پرایوته: عددی شبیه "-1001234567890" (ربات باید ادمین کانال باشه)
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@your_channel_username")

# منابع خبری RSS (می‌تونید فید بیشتری اضافه یا کم کنید)
# هر فید یک دسته (category) و ایموجی مخصوص خودش داره که تو پیام تلگرام نمایش داده می‌شه
RSS_FEEDS = [
    # --- اخبار گیم ---
    {"url": "https://feeds.ign.com/ign/games-all", "emoji": "🎮", "label": "گیم"},
    {"url": "https://www.gamespot.com/feeds/game-news/", "emoji": "🎮", "label": "گیم"},
    {"url": "https://www.eurogamer.net/feed", "emoji": "🎮", "label": "گیم"},
    {"url": "https://www.pcgamer.com/rss/", "emoji": "🎮", "label": "گیم"},
    # --- اخبار انیمه ---
    {"url": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us", "emoji": "🎌", "label": "انیمه"},
    {"url": "https://otakumode.com/news/feed", "emoji": "🎌", "label": "انیمه"},
]

# هر چند ثانیه یک‌بار فیدها رو چک کنه
CHECK_INTERVAL_SECONDS = 600  # هر ۱۰ دقیقه

# حداکثر تعداد خبر جدید از هر فید در هر بار چک کردن
MAX_ITEMS_PER_FEED = 8

# فایلی که لینک خبرهای ارسال‌شده رو نگه می‌داره تا تکراری ارسال نشه
SENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_news.json")

# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("gamenews_bot")


def load_sent_links():
    if os.path.exists(SENT_FILE):
        try:
            with open(SENT_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_sent_links(links_set):
    # فقط ۵۰۰ تای آخر رو نگه می‌داریم تا فایل بی‌نهایت بزرگ نشه
    trimmed = list(links_set)[-500:]
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False)


def clean_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def translate_to_fa(text: str) -> str:
    if not text:
        return ""
    try:
        # گوگل ترنسلیت محدودیت طول داره، پس تیکه‌تیکه ترجمه می‌کنیم
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
        translated_chunks = [
            GoogleTranslator(source="auto", target="fa").translate(chunk)
            for chunk in chunks
        ]
        return " ".join(translated_chunks)
    except Exception as e:
        log.warning(f"خطا در ترجمه: {e}")
        return text  # اگه ترجمه شکست خورد، متن اصلی رو برمی‌گردونیم


def extract_image_url(entry) -> str | None:
    # media:content
    media_content = entry.get("media_content")
    if media_content:
        for m in media_content:
            if m.get("url"):
                return m["url"]

    # media:thumbnail
    media_thumbnail = entry.get("media_thumbnail")
    if media_thumbnail:
        for m in media_thumbnail:
            if m.get("url"):
                return m["url"]

    # enclosure links با نوع image
    for link in entry.get("links", []):
        if str(link.get("type", "")).startswith("image"):
            return link.get("href")

    # جستجوی تگ <img> داخل summary یا content
    for field in ("summary", "content"):
        value = entry.get(field)
        if isinstance(value, list) and value:
            value = value[0].get("value", "")
        if value:
            match = re.search(r'<img[^>]+src="([^"]+)"', value)
            if match:
                return match.group(1)

    return None


def send_photo(image_url: str, caption: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": CHANNEL_ID,
        "photo": image_url,
        "caption": caption,
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, data=payload, timeout=20)
        result = r.json()
        if result.get("ok"):
            return True
        log.warning(f"ارسال عکس ناموفق بود: {result}")
        return False
    except Exception as e:
        log.warning(f"خطا در ارسال عکس: {e}")
        return False


def send_text(caption: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": caption,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, data=payload, timeout=20)
        result = r.json()
        if result.get("ok"):
            return True
        log.warning(f"ارسال متن ناموفق بود: {result}")
        return False
    except Exception as e:
        log.warning(f"خطا در ارسال متن: {e}")
        return False


def build_caption(title_fa: str, summary_fa: str, link: str, emoji: str = "🎮") -> str:
    caption = f"{emoji} <b>{html.escape(title_fa)}</b>\n\n{html.escape(summary_fa)}\n\n🔗 <a href=\"{link}\">منبع خبر</a>"
    # تلگرام کپشن عکس رو حداکثر ۱۰۲۴ کاراکتر قبول می‌کنه
    if len(caption) > 1000:
        allowed_summary_len = 1000 - len(title_fa) - len(link) - 60
        short_summary = summary_fa[: max(allowed_summary_len, 0)] + "…"
        caption = f"{emoji} <b>{html.escape(title_fa)}</b>\n\n{html.escape(short_summary)}\n\n🔗 <a href=\"{link}\">منبع خبر</a>"
    return caption


def process_feeds():
    sent_links = load_sent_links()
    new_items_found = 0

    for feed_info in RSS_FEEDS:
        feed_url = feed_info["url"]
        emoji = feed_info.get("emoji", "🎮")
        label = feed_info.get("label", "خبر")

        log.info(f"در حال بررسی فید {label}: {feed_url}")
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            log.warning(f"خطا در خواندن فید {feed_url}: {e}")
            continue

        for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
            link = entry.get("link")
            if not link or link in sent_links:
                continue

            title = entry.get("title", "").strip()
            raw_summary = entry.get("summary", "") or entry.get("description", "")
            summary = clean_html(raw_summary)[:600]

            title_fa = translate_to_fa(title)
            summary_fa = translate_to_fa(summary)
            image_url = extract_image_url(entry)

            caption = build_caption(title_fa, summary_fa, link, emoji)

            log.info(f"در حال ارسال خبر ({label}): {title}")

            sent_ok = False
            if image_url:
                sent_ok = send_photo(image_url, caption)
                if not sent_ok:
                    # اگه عکس مشکل داشت، به‌صورت متنی بفرست
                    sent_ok = send_text(caption)
            else:
                sent_ok = send_text(caption)

            if sent_ok:
                sent_links.add(link)
                new_items_found += 1
                # کمی مکث بین پیام‌ها تا به محدودیت تلگرام نخوریم
                time.sleep(3)

    if new_items_found:
        save_sent_links(sent_links)
        log.info(f"مجموعاً {new_items_found} خبر جدید ارسال شد.")
    else:
        log.info("خبر جدیدی برای ارسال پیدا نشد.")


def main():
    if BOT_TOKEN.startswith("REPLACE_WITH"):
        log.error("لطفاً ابتدا BOT_TOKEN و CHANNEL_ID رو در تنظیمات وارد کنید.")
        return

    log.info("ربات اخبار گیم شروع به کار کرد...")
    while True:
        try:
            process_feeds()
        except Exception as e:
            log.error(f"خطای کلی: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
