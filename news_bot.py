"""
ربات اخبار وارتاندر فارسی برای تلگرام — نسخه‌ی Gemini + تأیید پیش‌نویس
--------------------------------------------------------------------
جریان کار:
1) از دو منبع رسمی خبر وارتاندر رو می‌گیره:
   - سایت اصلی (warthunder.com/en/news)
   - بخش «Official News, Development Blogs and Updates» فروم رسمی (RSS)
2) هر خبر رو به Gemini می‌ده تا:
   - تشخیص بده خبر ارزش انتشار داره یا نه (تخفیف صرف فروشگاه و مناسبت‌های
     کم‌اهمیت رد می‌شن؛ آپدیت بزرگ، دِوبلاگ، رویداد جدید، وسیله‌ی نقلیه‌ی
     جدید، تغییرات مهم گیم‌پلی مهم در نظر گرفته می‌شن)
   - عنوان و متن رو به فارسیِ روان، جذاب، خلاصه و با ایموجی‌های کیبورد
     بازنویسی کنه
3) عکس خبر رو پیدا می‌کنه (از og:image صفحه‌ی خبر).
4) به‌جای ارسال مستقیم، خبرِ آماده رو به‌عنوان **پیش‌نویس** به گروه پیش‌نویس
   (DRAFT_GROUP_ID) می‌فرسته.
5) هر عضو گروه با ریپلای‌کردن روی همون پیش‌نویس و نوشتن دستور /post، خبر رو
   (همراه با یک استیکر گیمینگ تصادفی) به کانال اصلی (CHANNEL_ID) منتشر می‌کنه.
6) قبل از فرستادن هر خبر به Gemini، عنوانش با موضوع‌های اخیر و بقیه‌ی خبرهای
   همون دور مقایسه می‌شه؛ اگه هر دو منبع یک خبر رو با عنوان مشابه پوشش داده
   باشن، فقط اولی پردازش می‌شه و بقیه بدون مصرف توکن Gemini رد می‌شن.
7) برای جلوگیری از پردازش دوباره‌ی یک خبر، لینک خبرهای بررسی‌شده تو
   sent_news.json و موضوع‌هاشون تو sent_topics.json نگه داشته می‌شه.
   پیش‌نویس‌های در انتظار تأیید هم تو pending_drafts.json ذخیره می‌شن تا با
   ری‌استارت ربات از دست نرن.

با DRY_RUN=1 می‌تونی بدون ارسال واقعی، فقط تو کنسول ببینی خروجی چطوریه.
"""

import os
import json
import random
import logging
import re
import asyncio

import feedparser
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
from telegram import Update, InputMediaPhoto
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------------------------------------------------------------------
# تنظیمات
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]                        # کانال نهایی: @yourchannel یا -100...
DRAFT_GROUP_ID = os.environ["DRAFT_GROUP_ID"]                # گروه پیش‌نویس برای تأیید خبرها
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# اسم کوتاه پک‌های استیکر گیمینگ (بخش بعد از t.me/addstickers/ ) — با کاما جدا کن
STICKER_PACK_NAMES = [
    s.strip() for s in os.environ.get("STICKER_PACK_NAMES", "").split(",") if s.strip()
]

# اختیاری: اگه بخوای فقط آیدی‌های خاصی اجازه‌ی /post داشته باشن (با کاما جدا کن)
# خالی بذاری یعنی هر عضو گروه پیش‌نویس می‌تونه تأیید کنه.
ALLOWED_APPROVER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_APPROVER_IDS", "").split(",") if x.strip().isdigit()
}

CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "60"))
MAX_ITEMS_PER_RUN = int(os.environ.get("MAX_ITEMS_PER_RUN", "5"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

SENT_NEWS_FILE = "sent_news.json"
SENT_TOPICS_FILE = "sent_topics.json"
PENDING_DRAFTS_FILE = "pending_drafts.json"

# چند تا موضوعِ اخیر برای مقایسه‌ی شباهت نگه داشته بشه (برای جلوگیری از رشد بی‌نهایت فایل)
MAX_STORED_TOPICS = 400
# چه‌قدر شباهتِ کلمات کلیدیِ دو عنوان لازمه تا «همون خبر» در نظر گرفته بشن (۰ تا ۱)
TOPIC_SIMILARITY_THRESHOLD = 0.5

# منبع اول: فید RSS بخش اخبار رسمی فروم وارتاندر
RSS_FEEDS = {
    "War Thunder Forum": "https://forum.warthunder.com/c/official-news-and-information/7.rss",
}

# منبع دوم: سایت اصلی وارتاندر (این صفحه فید RSS نداره، پس مستقیم اسکرپ می‌شه)
OFFICIAL_SITE_NEWS_URL = "https://warthunder.com/en/news"
OFFICIAL_SITE_BASE = "https://warthunder.com"
OFFICIAL_SITE_MAX_ITEMS = 15

GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

# این امضا فقط موقع انتشار نهایی در کانال اصلی اضافه می‌شه (نه تو پیش‌نویس)
CHANNEL_SIGNATURE = "𝐈𝐃 : @War_Thunder_MRX"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("warthunder-news-bot")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(GEMINI_MODEL_NAME)

# ---------------------------------------------------------------------------
# ذخیره‌سازی روی دیسک
# ---------------------------------------------------------------------------

def _load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("فایل %s خراب بود، از صفر شروع می‌کنیم.", path)
    return default


def _save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_sent_links() -> set:
    return set(_load_json(SENT_NEWS_FILE, []))


def save_sent_links(links: set) -> None:
    _save_json(SENT_NEWS_FILE, sorted(links))


def load_pending_drafts() -> dict:
    return _load_json(PENDING_DRAFTS_FILE, {})


def save_pending_drafts(drafts: dict) -> None:
    _save_json(PENDING_DRAFTS_FILE, drafts)


def load_sent_topics() -> list:
    return _load_json(SENT_TOPICS_FILE, [])


def save_sent_topics(topics: list) -> None:
    # فقط آخرین‌ها رو نگه می‌داریم که فایل بی‌نهایت بزرگ نشه
    _save_json(SENT_TOPICS_FILE, topics[-MAX_STORED_TOPICS:])


# ---------------------------------------------------------------------------
# تشخیص خبرهای تکراری/مشابه (وقتی هم فروم هم سایت اصلی یک خبر رو پوشش دادن)
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on",
    "for", "and", "with", "at", "by", "from", "as", "its", "it", "this",
    "that", "new", "will", "has", "have", "be", "after", "how", "what",
    "why", "get", "gets", "you", "your",
}


def extract_keywords(title: str) -> set:
    words = re.findall(r"[a-zA-Z0-9']+", title.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def topic_similarity(words_a: set, words_b: set) -> float:
    """ضریب هم‌پوشانی نسبت به کوچک‌ترین مجموعه — برای عنوان‌های هم‌طول نامساوی
    (که دو منبع مختلف معمولاً دارن) بهتر از Jaccard جواب می‌ده."""
    if not words_a or not words_b:
        return 0.0
    intersection = len(words_a & words_b)
    return intersection / min(len(words_a), len(words_b))


def is_duplicate_topic(title: str, known_topics: list) -> bool:
    words = extract_keywords(title)
    for known in known_topics:
        if topic_similarity(words, set(known)) >= TOPIC_SIMILARITY_THRESHOLD:
            return True
    return False


# ---------------------------------------------------------------------------
# گرفتن خبرهای جدید از منابع
# ---------------------------------------------------------------------------

MAX_IMAGES_PER_POST = 3


def fetch_rss_entries() -> list:
    entries = []
    for source_name, url in RSS_FEEDS.items():
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries[:15]:
                entries.append({
                    "source": source_name,
                    "title": entry.get("title", "").strip(),
                    "link": entry.get("link", "").strip(),
                    "summary": BeautifulSoup(
                        entry.get("summary", entry.get("description", "")),
                        "html.parser",
                    ).get_text().strip(),
                    "images": extract_images_from_entry(entry),
                })
        except Exception as e:
            log.error("خطا در خوندن فید %s: %s", source_name, e)
    return entries


def extract_images_from_entry(entry, max_images: int = MAX_IMAGES_PER_POST) -> list:
    """تا max_images تا لینک عکس از یک آیتم فید (فروم) درمی‌آره، به‌ترتیب:
    media_content، media_thumbnail، enclosure، و بعد خودِ عکس‌های داخل متن خبر."""
    images = []

    def add(url):
        if url and url not in images and len(images) < max_images:
            images.append(url)

    if hasattr(entry, "media_content"):
        for media in entry.media_content:
            add(media.get("url"))
    if hasattr(entry, "media_thumbnail"):
        for media in entry.media_thumbnail:
            add(media.get("url"))
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image"):
            add(link.get("href"))

    if len(images) < max_images:
        html = entry.get("summary", "") + str(entry.get("content", ""))
        for src in re.findall(r'<img[^>]+src="([^"]+)"', html):
            add(src)
            if len(images) >= max_images:
                break

    return images


def fetch_page_meta(page_url: str, max_images: int = MAX_IMAGES_PER_POST) -> dict:
    """og:title / og:description و تا چند تا og:image یک صفحه رو برمی‌گردونه."""
    result = {"title": None, "description": None, "images": []}
    try:
        resp = requests.get(page_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "html.parser")

        title_tag = soup.find("meta", property="og:title")
        if title_tag and title_tag.get("content"):
            result["title"] = title_tag["content"].strip()

        desc_tag = soup.find("meta", property="og:description")
        if desc_tag and desc_tag.get("content"):
            result["description"] = desc_tag["content"].strip()

        for image_tag in soup.find_all("meta", property="og:image")[:max_images]:
            content = image_tag.get("content")
            if content:
                result["images"].append(content.strip())
    except Exception as e:
        log.warning("نشد اطلاعات صفحه‌ی %s رو بگیریم: %s", page_url, e)
    return result


def fetch_page_images(page_url: str, max_images: int = MAX_IMAGES_PER_POST) -> list:
    return fetch_page_meta(page_url, max_images=max_images).get("images", [])


def get_item_images(item: dict) -> list:
    """لیست نهاییِ عکس‌های یک خبر رو برمی‌گردونه (حداکثر ۳ تا)؛ اگه فید عکسی
    نداشت، از صفحه‌ی خودِ خبر (og:image) به‌عنوان جایگزین استفاده می‌کنه."""
    images = item.get("images") or []
    if not images:
        images = fetch_page_images(item["link"])
    return images[:MAX_IMAGES_PER_POST]


def fetch_official_site_entries() -> list:
    """سایت اصلی وارتاندر فید RSS نداره، پس لیست خبرها رو مستقیم اسکرپ می‌کنیم:
    از صفحه‌ی اصلی فقط لینک هر خبر رو درمی‌آریم، بعد از خودِ صفحه‌ی هر خبر
    عنوان/توضیح/عکس‌ها (og:title, og:description, og:image) رو می‌گیریم — این‌ها
    متادیتای استانداردن و بدون وابستگی به ساختار دقیق HTML لیست کار می‌کنن."""
    entries = []
    try:
        resp = requests.get(
            OFFICIAL_SITE_NEWS_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"}
        )
        hrefs = re.findall(r'href="(/en/news/\d+-[a-z0-9\-]+-en)"', resp.text)
        seen = set()
        unique_hrefs = []
        for href in hrefs:
            if href not in seen:
                seen.add(href)
                unique_hrefs.append(href)

        for href in unique_hrefs[:OFFICIAL_SITE_MAX_ITEMS]:
            full_url = OFFICIAL_SITE_BASE + href
            meta = fetch_page_meta(full_url)
            if not meta.get("title"):
                continue
            entries.append({
                "source": "War Thunder Official Site",
                "title": meta["title"],
                "link": full_url,
                "summary": meta.get("description") or "",
                "images": meta.get("images", []),
            })
    except Exception as e:
        log.error("خطا در خوندن لیست اخبار سایت رسمی: %s", e)
    return entries


def fetch_latest_entries() -> list:
    return fetch_rss_entries() + fetch_official_site_entries()


# ---------------------------------------------------------------------------
# بازنویسی جذاب با Gemini
# ---------------------------------------------------------------------------

GEMINI_PROMPT = """
تو یک ادمین حرفه‌ای و باتجربه‌ی یک کانال خبری فارسیِ بازی **War Thunder** هستی
که مخاطب‌های جوان و پرشور داره. یک خبر War Thunder به زبان انگلیسی بهت می‌دم.
باید:

1. تشخیص بدی این خبر ارزش انتشار داره یا نه. تقریباً همه‌ی خبرهای رسمی
   وارتاندر (شامل **تخفیف‌های فروشگاهی و پک‌های ویژه** هم) ارزش انتشار
   دارن، چون طرفدارها دنبال این‌جور خبرها هم هستن. خبر مهم یعنی: آپدیت بزرگ
   (Major Update)، دِوبلاگ معرفیِ وسیله‌ی نقلیه‌ی جدید، رویداد جدید (Event)،
   تغییرات مهم گیم‌پلی/بالانس، تریلر آپدیت، اخبار مسابقات (WTCS/Esports)،
   تخفیف یا پک ویژه‌ی فروشگاهی، یا هر اطلاعیه‌ی مهم دیگه‌ی سازنده (Gaijin)
   درباره‌ی بازی. فقط خبرهای کاملاً بی‌ربط به بازی یا محتوای تکراری/بی‌محتوا
   رو رد کن.

2. اگه ارزش انتشار داره، عنوان و متن رو کاملاً به فارسیِ روان، خودمونی ولی
   حرفه‌ای و به‌شدت جذاب بازنویسی کن — نه ترجمه‌ی کلمه‌به‌کلمه. از لحن
   هیجان‌انگیز و ریتم خبری استفاده کن. تو عنوان و لابه‌لای متن از
   **ایموجی‌های معمولیِ کیبورد** (مثل 🎮🔥🚀💥🕹️⚡️🆕👀💣🏆✈️🚂🚢) به‌شکل شیک و
   طبیعی استفاده کن تا خوندنش نشاط داشته باشه — ولی زیاده‌روی نکن (در کل متن
   حداکثر ۴-۶ ایموجی، نه بیشتر، و هیچ‌وقت ایموجی پشت‌سرهم توی یک جا).

3. متن نهایی باید **خلاصه و فشرده** باشه — فقط ۲ تا ۳ جمله‌ی کوتاه و خوش‌ریتم،
   فقط نکته‌ی اصلیِ خبر. جزئیات حاشیه‌ای، توضیحات تکراری، پس‌زمینه‌ی غیرضروری،
   یا نقل‌قول‌های طولانی رو کامل حذف کن. در عین حال نباید آنقدر کوتاه بشه که
   خبر گنگ یا ناقص به‌نظر برسه — فقط خلاصه، نه سرسری. هر جمله باید حس هیجان و
   تازگیِ خبر رو منتقل کنه، انگار داری برای یه دوست هم‌تیمی تعریف می‌کنی، نه
   این‌که داری گزارش رسمی می‌نویسی. اگه اسم وسیله‌ی نقلیه یا کشور/شاخه‌ی
   تحقیقاتی خاصی تو خبر بود، حتماً نگهش دار چون برای طرفدارها مهمه.

4. هیچ هشتگی به متن اضافه نکن.

فقط و فقط یک JSON خام با این ساختار برگردون، بدون توضیح اضافه و بدون ```:
{{
  "is_worth_posting": true/false,
  "title_fa": "عنوان جذاب فارسی",
  "body_fa": "متن خلاصه‌شده‌ی جذاب فارسی"
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
        return json.loads(raw)
    except Exception as e:
        log.error("خطا در پردازش Gemini برای '%s': %s", item["title"], e)
        return None


def build_caption(rewritten: dict, with_signature: bool = False) -> str:
    caption = f"<blockquote><b>{rewritten['title_fa']}</b></blockquote>\n\n{rewritten['body_fa']}"
    if with_signature:
        caption += f"\n\n<blockquote>{CHANNEL_SIGNATURE}</blockquote>"
    return caption


# ---------------------------------------------------------------------------
# استیکر تصادفی
# ---------------------------------------------------------------------------

async def get_random_sticker_file_id(bot) -> str | None:
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
# فرستادن پیش‌نویس به گروه تأیید
# ---------------------------------------------------------------------------

async def send_news_message(bot, chat_id, caption: str, images: list):
    """یک خبر رو با ۰، ۱، یا چند (حداکثر ۳) عکس، همیشه در **یک پیام واحد**
    می‌فرسته — با آلبوم (media group) وقتی چند عکسه، تا هیچ‌وقت پیام دومی
    ساخته نشه. کپشن فقط روی اولین عکسِ آلبوم قرار می‌گیره."""
    if len(images) >= 2:
        media = [InputMediaPhoto(images[0], caption=caption, parse_mode="HTML")]
        for url in images[1:MAX_IMAGES_PER_POST]:
            media.append(InputMediaPhoto(url))
        msgs = await bot.send_media_group(chat_id=chat_id, media=media)
        return msgs[0]
    elif len(images) == 1:
        return await bot.send_photo(
            chat_id=chat_id, photo=images[0], caption=caption, parse_mode="HTML"
        )
    else:
        return await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")


async def send_draft(bot, item: dict, rewritten: dict) -> None:
    caption = build_caption(rewritten)
    images = get_item_images(item)
    footer = "\n\n———\n🗂 برای انتشار در کانال، رو همین پیام ریپلای کن و بنویس: /post"

    if DRY_RUN:
        log.info(
            "—— DRY RUN (پیش‌نویس) ——\n%s%s\nعکس‌ها (%d): %s\n",
            caption, footer, len(images), images,
        )
        return

    try:
        msg = await send_news_message(bot, DRAFT_GROUP_ID, caption + footer, images)
    except TelegramError as e:
        log.error("ارسال پیش‌نویس به گروه ناموفق بود: %s", e)
        return

    drafts = load_pending_drafts()
    drafts[str(msg.message_id)] = {
        "title_fa": rewritten["title_fa"],
        "body_fa": rewritten["body_fa"],
        "image_urls": images,
        "source_link": item["link"],
    }
    save_pending_drafts(drafts)
    log.info("پیش‌نویس فرستاده شد و منتظر تأیید (/post) هست: %s", rewritten["title_fa"])


# ---------------------------------------------------------------------------
# دستور /post — انتشار پیش‌نویس تأییدشده به کانال
# ---------------------------------------------------------------------------

async def handle_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or str(message.chat_id) != str(DRAFT_GROUP_ID):
        return  # فقط تو گروه پیش‌نویس فعاله

    if message.reply_to_message is None:
        await message.reply_text("باید روی خودِ پیام پیش‌نویس ریپلای کنی و /post بزنی.")
        return

    if ALLOWED_APPROVER_IDS and message.from_user.id not in ALLOWED_APPROVER_IDS:
        await message.reply_text("متأسفم، اجازه‌ی تأیید و انتشار خبر رو نداری.")
        return

    draft_id = str(message.reply_to_message.message_id)
    drafts = load_pending_drafts()
    draft = drafts.get(draft_id)

    if draft is None:
        await message.reply_text("این پیش‌نویس پیدا نشد (شاید قبلاً منتشر شده یا منقضی شده).")
        return

    rewritten = {
        "title_fa": draft["title_fa"],
        "body_fa": draft["body_fa"],
    }
    caption = build_caption(rewritten, with_signature=True)
    images = draft.get("image_urls", [])
    bot = context.bot

    sticker_id = await get_random_sticker_file_id(bot)
    if sticker_id:
        try:
            await bot.send_sticker(chat_id=CHANNEL_ID, sticker=sticker_id)
        except TelegramError as e:
            log.warning("ارسال استیکر ناموفق بود: %s", e)

    try:
        await send_news_message(bot, CHANNEL_ID, caption, images)
    except TelegramError as e:
        log.error("ارسال خبر به کانال ناموفق بود: %s", e)
        await message.reply_text(f"❌ ارسال به کانال ناموفق بود: {e}")
        return

    del drafts[draft_id]
    save_pending_drafts(drafts)
    await message.reply_text("✅ منتشر شد تو کانال.")
    log.info("خبر با تأیید کاربر منتشر شد: %s", draft["title_fa"])


# ---------------------------------------------------------------------------
# چک دوره‌ای برای خبر جدید (job)
# ---------------------------------------------------------------------------

async def check_for_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    sent_links = load_sent_links()
    sent_topics = load_sent_topics()
    entries = fetch_latest_entries()

    candidates = [e for e in entries if e["link"] and e["link"] not in sent_links]

    # حذف خبرهای تکراری/مشابه (وقتی هم فروم هم سایت اصلی یک خبر رو پوشش دادن)
    # قبل از مصرف توکن Gemini.
    unique_entries = []
    seen_this_run = []
    for entry in candidates:
        if is_duplicate_topic(entry["title"], sent_topics) or is_duplicate_topic(
            entry["title"], seen_this_run
        ):
            log.info(
                "خبر مشابه/تکراری رد شد (بدون مصرف Gemini): [%s] %s",
                entry["source"],
                entry["title"],
            )
            sent_links.add(entry["link"])  # دیگه هیچ‌وقت دوباره چک نشه
            continue
        unique_entries.append(entry)
        seen_this_run.append(extract_keywords(entry["title"]))

    new_entries = unique_entries[:MAX_ITEMS_PER_RUN]

    if not new_entries:
        log.info("خبر جدیدی نبود.")
        save_sent_links(sent_links)
        return

    for item in new_entries:
        log.info("در حال بررسی: [%s] %s", item["source"], item["title"])
        rewritten = rewrite_with_gemini(item)
        sent_links.add(item["link"])  # چه پیش‌نویس بشه چه رد بشه، دوباره چک نمی‌شه
        sent_topics.append(list(extract_keywords(item["title"])))

        if not rewritten or not rewritten.get("is_worth_posting"):
            log.info("رد شد (کم‌ارزش یا خطا): %s", item["title"])
            continue

        await send_draft(context.bot, item, rewritten)
        await asyncio.sleep(2)  # فاصله‌ی کوتاه بین درخواست‌های Gemini برای رعایت محدودیت RPM

    save_sent_links(sent_links)
    save_sent_topics(sent_topics)


# ---------------------------------------------------------------------------
# راه‌اندازی برنامه
# ---------------------------------------------------------------------------

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("post", handle_post_command))
    app.job_queue.run_repeating(
        check_for_news, interval=CHECK_INTERVAL_MINUTES * 60, first=5
    )
    log.info(
        "ربات اخبار وارتاندر شروع به کار کرد. هر %s دقیقه چک می‌کنه و پیش‌نویس‌ها منتظر /post می‌مونن.",
        CHECK_INTERVAL_MINUTES,
    )
    app.run_polling()


if __name__ == "__main__":
    main()
