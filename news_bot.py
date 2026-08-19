"""
ربات اخبار گیم فارسی برای تلگرام — نسخه‌ی Gemini + تأیید پیش‌نویس
--------------------------------------------------------------------
جریان کار:
1) از فیدهای RSS سایت‌های خبری گیم (IGN, GameSpot, Eurogamer, PC Gamer)
   جدیدترین خبرها رو می‌گیره (اخبار انیمه پیگیری نمی‌شه).
2) هر خبر رو به Gemini می‌ده تا:
   - تشخیص بده خبر ارزش انتشار داره یا نه (تخفیف/لیست/ریویو صرف = رد بشه)
   - عنوان و متن رو به فارسیِ روان، جذاب و با ایموجی‌های کیبورد بازنویسی کنه
   - چند هشتگ مرتبط پیشنهاد بده
3) عکس خبر رو پیدا می‌کنه.
4) به‌جای ارسال مستقیم، خبرِ آماده رو به‌عنوان **پیش‌نویس** به گروه پیش‌نویس
   (DRAFT_GROUP_ID) می‌فرسته.
5) هر عضو گروه با ریپلای‌کردن روی همون پیش‌نویس و نوشتن دستور /post، خبر رو
   (همراه با یک استیکر گیمینگ تصادفی) به کانال اصلی (CHANNEL_ID) منتشر می‌کنه.
6) برای جلوگیری از پردازش دوباره‌ی یک خبر، لینک خبرهای بررسی‌شده تو
   sent_news.json نگه داشته می‌شه. پیش‌نویس‌های در انتظار تأیید هم تو
   pending_drafts.json ذخیره می‌شن تا با ری‌استارت ربات از دست نرن.

با DRY_RUN=1 می‌تونی بدون ارسال واقعی، فقط تو کنسول ببینی خروجی چطوریه.
"""

import os
import json
import random
import logging
import re

import feedparser
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
from telegram import Update
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

CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "15"))
MAX_ITEMS_PER_RUN = int(os.environ.get("MAX_ITEMS_PER_RUN", "5"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

SENT_NEWS_FILE = "sent_news.json"
PENDING_DRAFTS_FILE = "pending_drafts.json"

RSS_FEEDS = {
    "IGN": "https://feeds.ign.com/ign/games-all",
    "GameSpot": "https://www.gamespot.com/feeds/game-news/",
    "Eurogamer": "https://www.eurogamer.net/feed",
    "PC Gamer": "https://www.pcgamer.com/rss/",
}

GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("gamenews-bot")

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


# ---------------------------------------------------------------------------
# گرفتن خبرهای جدید از فیدها
# ---------------------------------------------------------------------------

def fetch_latest_entries() -> list:
    entries = []
    for source_name, url in RSS_FEEDS.items():
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries[:3]:
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
    if hasattr(entry, "media_content") and entry.media_content:
        url = entry.media_content[0].get("url")
        if url:
            return url
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        url = entry.media_thumbnail[0].get("url")
        if url:
            return url
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image"):
            return link.get("href")
    html = entry.get("summary", "") + str(entry.get("content", ""))
    match = re.search(r'<img[^>]+src="([^"]+)"', html)
    if match:
        return match.group(1)
    return None


def fetch_og_image(page_url: str) -> str | None:
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

3. متن نهایی باید **خلاصه و فشرده** باشه — فقط ۲ تا ۳ جمله‌ی کوتاه و خوش‌ریتم،
   فقط نکته‌ی اصلیِ خبر. جزئیات حاشیه‌ای، توضیحات تکراری، پس‌زمینه‌ی غیرضروری،
   یا نقل‌قول‌های طولانی رو کامل حذف کن. در عین حال نباید آنقدر کوتاه بشه که
   خبر گنگ یا ناقص به‌نظر برسه — فقط خلاصه، نه سرسری. هر جمله باید حس هیجان و
   تازگیِ خبر رو منتقل کنه، انگار داری برای یه دوست گیمر تعریف می‌کنی، نه
   این‌که داری گزارش رسمی می‌نویسی.

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


# این امضا فقط موقع انتشار نهایی در کانال اصلی اضافه می‌شه (نه تو پیش‌نویس)
CHANNEL_SIGNATURE = "𝐈𝐃 : @HiromiyaStudio"


def build_caption(rewritten: dict, with_signature: bool = False) -> str:
    caption = f"<b>{rewritten['title_fa']}</b>\n\n{rewritten['body_fa']}"
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

async def send_draft(bot, item: dict, rewritten: dict) -> None:
    caption = build_caption(rewritten)
    image_url = item.get("image") or fetch_og_image(item["link"])
    footer = "\n\n———\n🗂 برای انتشار در کانال، رو همین پیام ریپلای کن و بنویس: /post"

    if DRY_RUN:
        log.info("—— DRY RUN (پیش‌نویس) ——\n%s%s\nعکس: %s\n", caption, footer, image_url)
        return

    try:
        if image_url:
            msg = await bot.send_photo(
                chat_id=DRAFT_GROUP_ID,
                photo=image_url,
                caption=caption + footer,
                parse_mode="HTML",
            )
        else:
            msg = await bot.send_message(
                chat_id=DRAFT_GROUP_ID, text=caption + footer, parse_mode="HTML"
            )
    except TelegramError as e:
        log.error("ارسال پیش‌نویس به گروه ناموفق بود: %s", e)
        return

    drafts = load_pending_drafts()
    drafts[str(msg.message_id)] = {
        "title_fa": rewritten["title_fa"],
        "body_fa": rewritten["body_fa"],
        "image_url": image_url,
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
    image_url = draft.get("image_url")
    bot = context.bot

    sticker_id = await get_random_sticker_file_id(bot)
    if sticker_id:
        try:
            await bot.send_sticker(chat_id=CHANNEL_ID, sticker=sticker_id)
        except TelegramError as e:
            log.warning("ارسال استیکر ناموفق بود: %s", e)

    try:
        if image_url:
            await bot.send_photo(
                chat_id=CHANNEL_ID, photo=image_url, caption=caption, parse_mode="HTML"
            )
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=caption, parse_mode="HTML")
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
    entries = fetch_latest_entries()

    new_entries = [e for e in entries if e["link"] and e["link"] not in sent_links]
    new_entries = new_entries[:MAX_ITEMS_PER_RUN]

    if not new_entries:
        log.info("خبر جدیدی نبود.")
        return

    for item in new_entries:
        log.info("در حال بررسی: [%s] %s", item["source"], item["title"])
        rewritten = rewrite_with_gemini(item)
        sent_links.add(item["link"])  # چه پیش‌نویس بشه چه رد بشه، دوباره چک نمی‌شه

        if not rewritten or not rewritten.get("is_worth_posting"):
            log.info("رد شد (کم‌ارزش یا خطا): %s", item["title"])
            continue

        await send_draft(context.bot, item, rewritten)

    save_sent_links(sent_links)


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
        "ربات اخبار گیم شروع به کار کرد. هر %s دقیقه چک می‌کنه و پیش‌نویس‌ها منتظر /post می‌مونن.",
        CHECK_INTERVAL_MINUTES,
    )
    app.run_polling()


if __name__ == "__main__":
    main()
