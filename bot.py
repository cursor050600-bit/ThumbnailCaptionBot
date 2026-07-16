"""
Telegram Video Processor Bot — Final
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ MessageEntity preserved — text_link, bold, italic, etc. sab safe
✅ Thumbnail: PTB v22 ka naya `cover` parameter — file_id se seedha kaam karta hai!
✅ /viewthumb command  
✅ file_id reuse for video — zero bytes transferred
✅ @username replace + credit line
✅ Koi extra library nahi — sirf python-telegram-bot==22.x
✅ Python 3.14 event loop fix
✅ Render port binding fix — health server on PORT env variable
✅ Custom word/phrase removal from captions
"""

import os, re, json, logging, asyncio, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
(SETUP_USERNAME, SETUP_KEEP_LINKS, SETUP_THUMBNAIL, AWAIT_THUMBNAIL_IMAGE, SETTHUMB_AWAIT, SETUP_REMOVE_WORDS) = range(6)


# ══════════════════════════════════════════════
#  Render Health Server — fixes "No open ports" warning
# ══════════════════════════════════════════════
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # silence access logs

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health server running on port {port}")
    server.serve_forever()


# ══════════════════════════════════════════════
#  Config
# ══════════════════════════════════════════════
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════
#  Caption + Entity processing
# ══════════════════════════════════════════════
def process_entities(caption: str, entities: list, my_username: str, keep_links: bool, remove_words: list = None):
    if not caption:
        caption = ""
    entities = list(entities) if entities else []
    clean_uname = my_username.lstrip("@")

    # Remove specified words/phrases from caption before processing
    if remove_words:
        for word in remove_words:
            word = word.strip()
            if not word:
                continue
            if " X" in word or word.endswith("X"):
                # "Video X" => remove "Video 1", "Video 2", "Video 123" etc.
                prefix = word[:-1].rstrip()  # "Video X" -> "Video"
                pattern = re.escape(prefix) + r"\s*\d+"
                caption = re.sub(pattern, "", caption, flags=re.IGNORECASE)
            else:
                caption = caption.replace(word, "")
        caption = re.sub(r"\n{3,}", "\n\n", caption).strip()

    def u16_to_char(text, u16_off):
        return len(text.encode("utf-16-le")[:u16_off * 2].decode("utf-16-le"))

    def u16len_to_charlen(text, char_start, u16_len):
        b_start = len(text[:char_start].encode("utf-16-le"))
        return len(text.encode("utf-16-le")[b_start: b_start + u16_len * 2].decode("utf-16-le"))

    def to_u16len(text):
        return len(text.encode("utf-16-le")) // 2

    char_ents = []
    for ent in sorted(entities, key=lambda e: e.offset):
        try:
            cs = u16_to_char(caption, ent.offset)
            cl = u16len_to_charlen(caption, cs, ent.length)
            char_ents.append({
                "type": ent.type, "cs": cs, "ce": cs + cl,
                "url": getattr(ent, "url", None),
                "user": getattr(ent, "user", None),
                "language": getattr(ent, "language", None),
                "custom_emoji_id": getattr(ent, "custom_emoji_id", None),
            })
        except Exception as ex:
            logger.warning(f"Entity skip: {ex}")

    found_mention = any(e["type"] == MessageEntity.MENTION for e in char_ents)
    # Check if caption already has OUR username as mention
    found_own_mention = any(
        e["type"] == MessageEntity.MENTION and
        caption[e["cs"]:e["ce"]].lstrip("@").lower() == clean_uname.lower()
        for e in char_ents
    )

    out_text = ""
    out_ents = []
    prev = 0

    for e in char_ents:
        cs, ce, et = e["cs"], e["ce"], e["type"]
        out_text += caption[prev:cs]
        cur_u16 = to_u16len(out_text)
        chunk = caption[cs:ce]

        if et == MessageEntity.MENTION:
            mentioned_user = chunk.lstrip("@").lower()
            if mentioned_user == clean_uname.lower():
                # Apna username hai — "Extracted by :- @username" banana hai
                prefix = "Extracted by :- "
                new_m = f"@{clean_uname}"
                full = prefix + new_m
                out_text += full
                mention_u16 = to_u16len(out_text) - to_u16len(new_m)
                out_ents.append(MessageEntity(type=MessageEntity.MENTION,
                    offset=mention_u16, length=to_u16len(new_m)))
            else:
                # Kisi aur ka username — waise hi rehne do, koi change nahi
                out_text += chunk
                out_ents.append(MessageEntity(type=MessageEntity.MENTION,
                    offset=cur_u16, length=to_u16len(chunk)))

        elif et == MessageEntity.TEXT_LINK:
            url = e["url"] or ""
            is_tme = "t.me/" in url or "telegram.me/" in url
            out_text += chunk
            if keep_links and not is_tme:
                out_ents.append(MessageEntity(type=MessageEntity.TEXT_LINK,
                    offset=cur_u16, length=to_u16len(chunk), url=url))

        elif et == MessageEntity.URL:
            url = chunk
            is_tme = "t.me/" in url or "telegram.me/" in url
            if is_tme:
                pass
            elif keep_links:
                out_text += chunk
                out_ents.append(MessageEntity(type=MessageEntity.URL,
                    offset=cur_u16, length=to_u16len(chunk)))

        else:
            out_text += chunk
            try:
                kw = {"type": et, "offset": cur_u16, "length": to_u16len(chunk)}
                if e["url"]:             kw["url"]             = e["url"]
                if e["user"]:            kw["user"]            = e["user"]
                if e["language"]:        kw["language"]        = e["language"]
                if e["custom_emoji_id"]: kw["custom_emoji_id"] = e["custom_emoji_id"]
                out_ents.append(MessageEntity(**kw))
            except Exception:
                pass

        prev = ce

    out_text += caption[prev:]

    out_text = re.sub(r'\S+\.t\.me\S*', '', out_text)
    out_text = re.sub(r'https?://t\.me\S*', '', out_text)
    out_text = re.sub(r't\.me/\S*', '', out_text)

    out_text = re.sub(r"\n{3,}", "\n\n", out_text).strip()

    if not found_mention:
        out_text += f"\n\nExtracted by :- @{clean_uname}"
    elif not found_own_mention:
        # Kisi aur ka mention tha, apna nahi — end me bhi mat add karo
        pass

    return out_text, out_ents


# ══════════════════════════════════════════════
#  Thumbnail cache helper
# ══════════════════════════════════════════════
async def get_thumbnail_file_id(context, chat_id: int, cfg: dict) -> str | None:
    cached = cfg.get("thumbnail_file_id")
    if cached:
        return cached

    local = cfg.get("thumbnail_local")
    if not local or not os.path.exists(local):
        return None

    try:
        with open(local, "rb") as f:
            sent = await context.bot.send_photo(
                chat_id=chat_id,
                photo=f,
                disable_notification=True,
            )
        fid = sent.photo[-1].file_id
        await sent.delete()
        cfg["thumbnail_file_id"] = fid
        save_config(cfg)
        logger.info("Thumbnail cached with file_id")
        return fid
    except Exception as e:
        logger.error(f"Thumbnail cache error: {e}")
        return None


# ══════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if cfg:
        thumb_ok = bool(cfg.get("thumbnail_local") and os.path.exists(cfg.get("thumbnail_local", "")))
        remove_words = cfg.get("remove_words", [])
        rw_display = ", ".join(remove_words) if remove_words else "None"
        text = (
            "✅ *Bot is ready!*\n\n"
            f"👤 Username: `@{cfg.get('username','not set')}`\n"
            f"🔗 Keep links: `{'Yes' if cfg.get('keep_links') else 'No'}`\n"
            f"🖼 Thumbnail: `{'Set ✅' if thumb_ok else 'Not set ❌'}`\n"
            f"🗑 Remove words: `{rw_display}`\n\n"
            "📨 Send any text, file, or media — processed instantly!\n\n"
            "/setup · /settings · /setthumb · /viewthumb"
        )
    else:
        text = "👋 *Welcome!*\n\nUse /setup to configure the bot."
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════
#  /setup
# ══════════════════════════════════════════════
async def setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ *Setup — Step 1/4*\n\nEnter your Telegram username:\n_(e.g. `Coursesbuying`)_",
        parse_mode=ParseMode.MARKDOWN)
    return SETUP_USERNAME

async def setup_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip().lstrip("@")
    if not re.match(r"^\w{3,32}$", username):
        await update.message.reply_text("❌ Invalid (3–32 chars, no spaces). Try again:")
        return SETUP_USERNAME
    context.user_data["u"] = username
    kb = [[InlineKeyboardButton("✅ Keep links", callback_data="links_yes"),
           InlineKeyboardButton("❌ Remove links", callback_data="links_no")]]
    await update.message.reply_text(
        f"✅ `@{username}`\n\n*Step 2/4:* Keep clickable links in captions?",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    return SETUP_KEEP_LINKS

async def setup_keep_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data["kl"] = (q.data == "links_yes")
    kb = [[InlineKeyboardButton("📤 Send thumbnail", callback_data="thumb_send"),
           InlineKeyboardButton("⏭ Skip", callback_data="thumb_skip")]]
    await q.edit_message_text(
        f"{'✅ Links kept.' if context.user_data['kl'] else '❌ Links removed.'}\n\n"
        "*Step 3/4:* Set a custom thumbnail?",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    return SETUP_THUMBNAIL

async def setup_thumbnail_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "thumb_skip":
        context.user_data["tp"] = None
        await q.edit_message_text(
            "⏭ No thumbnail.\n\n*Step 4/4:* Caption se koi word/phrase delete karna hai?\n\n"
            "Ek ek karke likho, har word/phrase alag line mein.\n"
            "Ya `skip` likho agar kuch nahi hatana.",
            parse_mode=ParseMode.MARKDOWN)
        return SETUP_REMOVE_WORDS
    await q.edit_message_text("🖼 Send thumbnail image now (as photo):")
    return AWAIT_THUMBNAIL_IMAGE

async def setup_recv_thumb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1] if update.message.photo else None
    if not photo:
        await update.message.reply_text("❌ Send a photo please.")
        return AWAIT_THUMBNAIL_IMAGE
    f = await photo.get_file()
    os.makedirs("thumbnails", exist_ok=True)
    p = f"thumbnails/thumb_{update.effective_user.id}.jpg"
    await f.download_to_drive(p)
    context.user_data["tp"] = p
    await update.message.reply_text(
        "✅ Thumbnail saved!\n\n*Step 4/4:* Caption se koi word/phrase delete karna hai?\n\n"
        "Ek ek karke likho, har word/phrase alag line mein.\n"
        "Ya `skip` likho agar kuch nahi hatana.",
        parse_mode=ParseMode.MARKDOWN)
    return SETUP_REMOVE_WORDS

async def setup_remove_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() == "skip":
        context.user_data["rw"] = []
    else:
        # Har line ek alag word/phrase
        words = [w.strip() for w in text.split("\n") if w.strip()]
        context.user_data["rw"] = words
    return await _finalize_setup(update, context)

async def _finalize_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    remove_words = ud.get("rw", [])
    cfg = {
        "username": ud["u"],
        "keep_links": ud.get("kl", True),
        "thumbnail_local": ud.get("tp"),
        "thumbnail_file_id": None,
        "remove_words": remove_words,
    }
    save_config(cfg)
    rw_display = "\n".join(f"  • `{w}`" for w in remove_words) if remove_words else "  _None_"
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(
        f"🎉 *Done!*\n\n👤 `@{cfg['username']}`\n"
        f"🔗 Links: `{'Keep' if cfg['keep_links'] else 'Remove'}`\n"
        f"🖼 Thumb: `{'Set ✅' if cfg['thumbnail_local'] else 'Not set'}`\n"
        f"🗑 Remove words:\n{rw_display}\n\n"
        "📨 Send any text, file, or media now!",
        parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def setup_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ══════════════════════════════════════════════
#  /setthumb
# ══════════════════════════════════════════════
async def setthumb_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🖼 Send new thumbnail image (as photo):")
    return SETTHUMB_AWAIT

async def setthumb_recv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1] if update.message.photo else None
    if not photo:
        await update.message.reply_text("❌ Send a photo.")
        return SETTHUMB_AWAIT
    f = await photo.get_file()
    os.makedirs("thumbnails", exist_ok=True)
    p = f"thumbnails/thumb_{update.effective_user.id}.jpg"
    await f.download_to_drive(p)
    cfg = load_config()
    cfg["thumbnail_local"] = p
    cfg["thumbnail_file_id"] = None
    save_config(cfg)
    await update.message.reply_text("✅ Thumbnail updated!")
    return ConversationHandler.END


# ══════════════════════════════════════════════
#  /viewthumb
# ══════════════════════════════════════════════
async def viewthumb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    p = cfg.get("thumbnail_local")
    if not p or not os.path.exists(p):
        await update.message.reply_text("❌ No thumbnail set. Use /setthumb")
        return
    with open(p, "rb") as f:
        await update.message.reply_photo(f, caption="🖼 Current thumbnail")


# ══════════════════════════════════════════════
#  /settings
# ══════════════════════════════════════════════
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if not cfg:
        await update.message.reply_text("⚠️ Not configured. Use /setup")
        return
    thumb_ok = bool(cfg.get("thumbnail_local") and os.path.exists(cfg.get("thumbnail_local", "")))
    remove_words = cfg.get("remove_words", [])
    rw_display = ", ".join(remove_words) if remove_words else "None"
    await update.message.reply_text(
        "⚙️ *Settings*\n\n"
        f"👤 `@{cfg.get('username','not set')}`\n"
        f"🔗 Links: `{'Keep' if cfg.get('keep_links') else 'Remove'}`\n"
        f"🖼 Thumb: `{'Set ✅' if thumb_ok else 'Not set ❌'}`\n"
        f"🗑 Remove words: `{rw_display}`\n\n"
        "/setup · /setthumb · /viewthumb",
        parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════
#  MESSAGE HANDLER
# ══════════════════════════════════════════════
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if not cfg:
        await update.message.reply_text("⚠️ Run /setup first!")
        return

    msg = update.message
    if not msg:
        return

    is_video = bool(msg.video)
    is_doc   = bool(msg.document)
    is_text  = bool(msg.text)
    is_caption = bool(msg.caption)
    captionable = bool(msg.video or msg.document or msg.photo or msg.audio or msg.animation)
    has_payload = bool(
        msg.text or msg.caption or msg.video or msg.document or msg.photo or
        msg.audio or msg.animation or msg.voice or msg.sticker or msg.video_note
    )
    if not has_payload:
        return

    new_caption = None
    new_ents = None
    if is_text or is_caption or captionable:
        text = msg.text if is_text else (msg.caption or "")
        entities = list(msg.entities) if is_text and msg.entities else list(msg.caption_entities) if msg.caption_entities else []

        new_caption, new_ents = process_entities(
            text,
            entities,
            cfg["username"],
            cfg.get("keep_links", True),
            cfg.get("remove_words", [])
        )

    thumb_local = cfg.get("thumbnail_local")
    thumb_ok    = bool(thumb_local and os.path.exists(thumb_local))

    status = await msg.reply_text("⚡ Processing...")

    try:
        if is_text:
            await context.bot.send_message(
                chat_id=msg.chat_id,
                text=new_caption,
                entities=new_ents or None,
            )
            await status.delete()
            return

        if is_video and thumb_ok:
            thumb_fid = await get_thumbnail_file_id(context, msg.chat_id, cfg)

            if thumb_fid:
                file_id  = msg.video.file_id
                duration = msg.video.duration
                width    = msg.video.width
                height   = msg.video.height
                await context.bot.send_video(
                    chat_id=msg.chat_id,
                    video=file_id,
                    caption=new_caption,
                    caption_entities=new_ents or None,
                    cover=thumb_fid,
                    supports_streaming=True,
                    duration=duration,
                    width=width,
                    height=height,
                )
            elif is_doc:
                file_id = msg.document.file_id
                with open(thumb_local, "rb") as thumbnail_file:
                    await context.bot.send_document(
                        chat_id=msg.chat_id,
                        document=file_id,
                        caption=new_caption,
                        caption_entities=new_ents or None,
                        thumbnail=thumbnail_file,
                    )
            else:
                await context.bot.copy_message(
                    chat_id=msg.chat_id,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    caption=new_caption,
                    caption_entities=new_ents or None,
                )
        elif is_doc and thumb_ok:
            file_id = msg.document.file_id
            with open(thumb_local, "rb") as thumbnail_file:
                await context.bot.send_document(
                    chat_id=msg.chat_id,
                    document=file_id,
                    caption=new_caption,
                    caption_entities=new_ents or None,
                    thumbnail=thumbnail_file,
                )
        elif is_video:
            await context.bot.copy_message(
                chat_id=msg.chat_id,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                caption=new_caption,
                caption_entities=new_ents or None,
            )
        else:
            await context.bot.copy_message(
                chat_id=msg.chat_id,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                caption=new_caption,
                caption_entities=new_ents or None,
            )

        await status.delete()

    except TelegramError as e:
        logger.error(f"Telegram error: {e}")
        await status.edit_text(f"❌ Error: {e}")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await status.edit_text(f"❌ Error: {e}")


# ══════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token and os.path.exists(".env"):
        with open(".env") as f:
            for line in f:
                line = line.strip()
                if line.startswith("BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not token:
        raise ValueError("BOT_TOKEN not set! Add to .env:  BOT_TOKEN=your_token")

    # ✅ Fix 1: Python 3.14 event loop fix
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # ✅ Fix 2: Render port binding — start health server in background thread
    threading.Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(token).build()

    setup_conv = ConversationHandler(
        entry_points=[CommandHandler("setup", setup_start)],
        states={
            SETUP_USERNAME:        [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_username)],
            SETUP_KEEP_LINKS:      [CallbackQueryHandler(setup_keep_links, pattern="^links_")],
            SETUP_THUMBNAIL:       [CallbackQueryHandler(setup_thumbnail_choice, pattern="^thumb_")],
            AWAIT_THUMBNAIL_IMAGE: [MessageHandler(filters.PHOTO, setup_recv_thumb)],
            SETUP_REMOVE_WORDS:    [MessageHandler(filters.TEXT, setup_remove_words)],
        },
        fallbacks=[CommandHandler("cancel", setup_cancel)],
    )
    setthumb_conv = ConversationHandler(
        entry_points=[CommandHandler("setthumb", setthumb_start)],
        states={SETTHUMB_AWAIT: [MessageHandler(filters.PHOTO, setthumb_recv)]},
        fallbacks=[CommandHandler("cancel", setup_cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("viewthumb", viewthumb))
    app.add_handler(setup_conv)
    app.add_handler(setthumb_conv)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("🤖 Bot running — PTB v22 + Render health server!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
