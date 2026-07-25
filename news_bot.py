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

# کلید API آنتروپیک برای ترجمه با کیفیت بالا (اختیاری - نیاز به شارژ داره)
# از https://console.anthropic.com بگیرید. اگه خالی باشه از Gemini یا گوگل ترنسلیت استفاده می‌شه.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# مدلی که برای ترجمه با Claude استفاده می‌شه
TRANSLATION_MODEL = os.environ.get("TRANSLATION_MODEL", "claude-haiku-4-5-20251001")

# کلید API رایگان گوگل جمینای برای ترجمه با کیفیت بالا و بدون هزینه
# از https://aistudio.google.com/apikey بگیرید (نیاز به کارت بانکی نداره)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

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
# نکته: چون هر چرخه حداکثر یک درخواست به AI می‌زنه، اگه با خطای ۴۲۹ (quota) مواجه شدید
# این عدد رو بزرگ‌تر کنید (مثلاً ۳۶۰۰ برای هر ساعت) یا کمی صبر کنید تا سهمیه‌تون ریست بشه.
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "1800"))  # هر ۳۰ دقیقه

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


def fetch_page_description(url: str) -> str:
    """
    وقتی فید هیچ خلاصه‌ای نداشت، از تگ‌های meta description / og:description
    خود صفحه‌ی خبر استفاده می‌کنیم (تقریباً همه‌ی سایت‌های خبری این تگ رو دارن).
    """
    try:
        r = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; GameNewsBot/1.0)"},
        )
        if r.status_code != 200:
            return ""
        page_html = r.text[:200000]  # فقط بخش ابتدایی صفحه کافیه (تگ‌های meta تو <head> هستن)

        patterns = [
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']',
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, page_html, re.IGNORECASE)
            if match:
                return html.unescape(match.group(1)).strip()
    except Exception as e:
        log.warning(f"خطا در گرفتن توضیحات از صفحه‌ی خبر: {e}")
    return ""


def extract_entry_summary(entry) -> str:
    """
    بعضی فیدها (مثل Anime News Network) به‌جای تگ استاندارد summary/description از
    content:encoded یا تگ‌های دیگه استفاده می‌کنن. این تابع چند منبع رو امتحان می‌کنه.
    """
    # ۱. تگ استاندارد summary/description
    for key in ("summary", "description"):
        val = entry.get(key)
        if val and clean_html(val):
            return val

    # ۲. content:encoded (feedparser این رو به‌صورت لیست تو entry.content می‌ذاره)
    content_list = entry.get("content")
    if content_list:
        for c in content_list:
            val = c.get("value", "")
            if val and clean_html(val):
                return val

    # ۳. subtitle (بعضی فیدها از این استفاده می‌کنن)
    val = entry.get("subtitle")
    if val and clean_html(val):
        return val

    # ۴. tags/categories رو به‌عنوان آخرین راه‌حل به عنوان زمینه اضافه می‌کنیم (بهتر از هیچی)
    return ""


ANALYSIS_SYSTEM_PROMPT = (
    "You are an editor for a Persian (Farsi) gaming and anime news Telegram channel. "
    "You will receive a JSON array of news items, each with an \"index\", \"title\", and "
    "\"summary\". For EACH item, do two things:\n"
    "1. Decide if it is IMPORTANT enough to publish to a broad audience of gaming/anime fans.\n"
    "   IMPORTANT: major game/anime announcements, confirmed release dates, trailers for "
    "highly-anticipated titles, major updates/DLC/expansions, significant business news "
    "(acquisitions, studio closures, major layoffs), award wins, major esports results, "
    "platform-defining news.\n"
    "   NOT IMPORTANT (reject): listicles/roundups ('10 best games...'), opinion/editorial "
    "pieces, reviews, minor patch notes, sales/deals/discount promos, giveaways/contests, "
    "unconfirmed rumors, how-to/guide articles, sponsored content, or anything with little "
    "real news value.\n"
    "2. If important, translate the title and summary into natural, fluent, journalistic "
    "Persian that a native Persian gaming/anime fan would enjoy reading. Keep game/anime "
    "titles, character names, and studio/company names in their commonly-used form among "
    "Persian gaming/anime communities (often left in Latin script or transliterated, "
    "whichever is more natural).\n\n"
    "Respond with ONLY a raw JSON array, no markdown code fences, no extra text, with one "
    "object per input item, each in exactly this shape and in the SAME ORDER as the input:\n"
    '[{"index": 0, "important": true or false, "title_fa": "...", "summary_fa": "..."}, ...]\n'
    "If important is false for an item, you may leave its title_fa and summary_fa as empty "
    "strings. The output array MUST have exactly as many objects as the input array."
)


def _parse_analysis_array(raw_text: str, expected_len: int) -> list | None:
    if not raw_text:
        return None
    cleaned = raw_text.strip()
    # حذف فنس‌های مارک‌داون اگه مدل با ```json برگردونده باشه
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, list) and len(data) == expected_len:
            return data
    except Exception:
        pass
    return None


def analyze_batch_with_gemini(items: list) -> list | None:
    """با یک درخواست، همه‌ی خبرهای جدید رو با Gemini هم فیلتر و هم ترجمه می‌کنه (رایگان)."""
    if not GEMINI_API_KEY or not items:
        return None
    try:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        )
        user_payload = [
            {"index": i, "title": it["title"], "summary": it["summary"]}
            for i, it in enumerate(items)
        ]
        payload = {
            "systemInstruction": {"parts": [{"text": ANALYSIS_SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": json.dumps(user_payload, ensure_ascii=False)}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        r = requests.post(url, json=payload, timeout=60)
        data = r.json()
        if r.status_code != 200:
            log.warning(f"خطای Gemini API (کد {r.status_code}): {data}")
            return None
        candidates = data.get("candidates", [])
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        raw_text = "".join(p.get("text", "") for p in parts).strip()
        return _parse_analysis_array(raw_text, len(items))
    except Exception as e:
        log.warning(f"خطا در تحلیل دسته‌ای با Gemini: {e}")
        return None


def analyze_batch_with_claude(items: list) -> list | None:
    """با یک درخواست، همه‌ی خبرهای جدید رو با Claude هم فیلتر و هم ترجمه می‌کنه (نیاز به شارژ)."""
    if not ANTHROPIC_API_KEY or not items:
        return None
    try:
        user_payload = [
            {"index": i, "title": it["title"], "summary": it["summary"]}
            for i, it in enumerate(items)
        ]
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": TRANSLATION_MODEL,
                "max_tokens": 4000,
                "system": ANALYSIS_SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}
                ],
            },
            timeout=60,
        )
        data = response.json()
        if response.status_code != 200:
            log.warning(f"خطای Claude API (کد {response.status_code}): {data}")
            return None
        parts = data.get("content", [])
        raw_text = "".join(
            p.get("text", "") for p in parts if p.get("type") == "text"
        ).strip()
        return _parse_analysis_array(raw_text, len(items))
    except Exception as e:
        log.warning(f"خطا در تحلیل دسته‌ای با Claude: {e}")
        return None


# کلیدواژه‌هایی که در نبود هوش مصنوعی برای فیلتر کردن اخبار کم‌ارزش استفاده می‌شن
LOW_VALUE_KEYWORDS = [
    # --- تخفیف / فروش / پرومو ---
    "deal", "deals", "% off", "discount", "coupon", "bundle sale",
    "cheapest", "price drop", "on sale", "save $", "steam sale",
    "amazon prime day", "black friday", "cyber monday", "sale ends",
    "sale alert", "flash sale", "clearance", "free download",
    "free this week", "epic games store free", "buy now",
    "where to buy", "pre-order guide", "preorder guide",

    # --- گیوآوی / مسابقه ---
    "giveaway", "sweepstakes", "win a", "enter to win", "contest",
    "raffle",

    # --- لیست‌ها / رنکینگ ---
    "top 10", "top ten", "top 5", "top five", "best of", "ranked",
    "ranking", "every game", "everything you need to know",
    "everything we know", "roundup", "recap", "definitive guide",
    "ultimate guide", "complete list", "all the", "worth playing",
    "games to play", "games you missed", "underrated games",

    # --- ریویو / پیش‌نمایش / نظری ---
    "review:", "our review", "hands-on", "hands on", "preview:",
    "impressions", "we played", "opinion:", "editorial:", "op-ed",
    "column:", "in defense of", "why i", "why you should",
    "first look", "early access impressions", "our thoughts",
    "is it worth", "should you buy", "should you play",

    # --- آموزش / راهنما ---
    "how to", "guide:", "tips and tricks", "walkthrough", "cheats",
    "codes for", "redeem codes", "best settings", "best build",
    "best loadout", "tier list", "best class", "beginner's guide",

    # --- محتوای تعاملی/تبلیغاتی کم‌ارزش ---
    "watch:", "video:", "livestream", "twitch stream", "let's play",
    "unboxing", "reaction", "quiz:", "poll:", "which character are you",
    "sponsored", "advertisement", "partner content", "in partnership with",
    "promoted", "affiliate",

    # --- زمان‌بندی/جزئیات فرعی (نه خبر اصلی) ---
    "release time", "what time does", "how to watch", "how to stream",
]


def passes_keyword_filter(title: str, summary: str) -> bool:
    combined = f"{title} {summary}".lower()
    return not any(keyword in combined for keyword in LOW_VALUE_KEYWORDS)


def analyze_and_translate_batch(items: list) -> list:
    """
    ورودی: [{"title": ..., "summary": ...}, ...]
    خروجی: [{"important": bool, "title_fa": str, "summary_fa": str}, ...] هم‌طول و هم‌ترتیب با ورودی

    اول با یک درخواست دسته‌ای به Gemini، بعد Claude امتحان می‌کنه. اگه هیچ‌کدوم
    در دسترس نبودن یا جواب معتبر ندادن، برای هر خبر با فیلتر کلیدواژه‌ای ساده +
    گوگل ترنسلیت پیش می‌ره.
    """
    if not items:
        return []

    raw_result = analyze_batch_with_gemini(items)
    if raw_result is None:
        raw_result = analyze_batch_with_claude(items)

    if raw_result is not None:
        results = []
        for entry in raw_result:
            results.append({
                "important": bool(entry.get("important", False)),
                "title_fa": entry.get("title_fa", "") or "",
                "summary_fa": entry.get("summary_fa", "") or "",
            })
        return results

    # --- حالت پشتیبان: بدون هوش مصنوعی، تک‌تک با فیلتر کلیدواژه‌ای + گوگل ترنسلیت ---
    log.warning("هوش مصنوعی در دسترس نبود؛ از فیلتر کلیدواژه‌ای و گوگل ترنسلیت استفاده می‌شه.")
    results = []
    for it in items:
        title, summary = it["title"], it["summary"]
        important = passes_keyword_filter(title, summary)
        if not important:
            results.append({"important": False, "title_fa": "", "summary_fa": ""})
            continue
        try:
            title_fa = GoogleTranslator(source="auto", target="fa").translate(title)
            summary_fa = GoogleTranslator(source="auto", target="fa").translate(summary[:4000])
        except Exception as e:
            log.warning(f"خطا در ترجمه: {e}")
            title_fa, summary_fa = title, summary
        results.append({"important": True, "title_fa": title_fa, "summary_fa": summary_fa})
    return results


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
    skipped_low_value = 0

    # مرحله ۱: جمع‌آوری تمام خبرهای جدید از همه‌ی فیدها
    candidates = []  # هر آیتم: {"entry":..., "link":..., "title":..., "summary":..., "emoji":..., "label":...}
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
            raw_summary = extract_entry_summary(entry)
            summary = clean_html(raw_summary)[:600]

            if not summary:
                # اگه فید هیچ خلاصه‌ای نداشت، از خود صفحه‌ی خبر بگیر
                summary = clean_html(fetch_page_description(link))[:600]

            candidates.append({
                "entry": entry,
                "link": link,
                "title": title,
                "summary": summary,
                "emoji": emoji,
                "label": label,
            })

    if not candidates:
        log.info("خبر جدیدی برای بررسی پیدا نشد.")
        return

    # مرحله ۲: تحلیل و ترجمه‌ی دسته‌ای همه‌ی کاندیدها با یک درخواست
    log.info(f"در حال تحلیل {len(candidates)} خبر جدید...")
    analyses = analyze_and_translate_batch(
        [{"title": c["title"], "summary": c["summary"]} for c in candidates]
    )

    # مرحله ۳: ارسال خبرهای مهم به تلگرام
    for candidate, analysis in zip(candidates, analyses):
        link = candidate["link"]
        title = candidate["title"]

        if not analysis["important"]:
            log.info(f"رد شد (کم‌ارزش): {title}")
            sent_links.add(link)  # دیگه دوباره بررسیش نکنه
            skipped_low_value += 1
            continue

        title_fa = analysis["title_fa"] or title
        summary_fa = analysis["summary_fa"] or candidate["summary"]
        image_url = extract_image_url(candidate["entry"])

        caption = build_caption(title_fa, summary_fa, link, candidate["emoji"])

        log.info(f"در حال ارسال خبر ({candidate['label']}): {title}")

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

    if new_items_found or skipped_low_value:
        save_sent_links(sent_links)
        log.info(
            f"مجموعاً {new_items_found} خبر مهم ارسال شد، "
            f"{skipped_low_value} خبر کم‌ارزش رد شد."
        )
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
