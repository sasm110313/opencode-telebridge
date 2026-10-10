import asyncio
import html
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Any, Optional, Set, List

import aiohttp

import config
from memory_manager import memory_manager
from opencode_client import OpenCodeClient
from telegram_actions import (
    resolve_path,
    detect_directory_intent,
    detect_file_download_intent,
    find_candidate_file,
    detect_channel_post_intent,
    load_user_channels,
    save_user_channels,
    TELEGRAM_AGENT_SYSTEM_PROMPT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("OpenCodeBot")

# Persian persistent keyboard for bottom tray
MAIN_REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "➕ سشن جدید"}, {"text": "🤖 مدل‌ها"}, {"text": "🧠 ایجنت"}],
        [{"text": "📂 مسیر کاری (/cd)"}, {"text": "🛑 توقف (/stop)"}, {"text": "💾 حافظه (/memory)"}],
        [{"text": "📢 ارسال به کانال (/post)"}, {"text": "📝 تغییرات گیت"}, {"text": "❓ راهنما"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}


class ChatThrottle:
    """Enforces per-chat rate limits and serializes outgoing Telegram mutations."""
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_send_time: float = 0.0
        self.last_edit_time: float = 0.0
        self.last_action_time: float = 0.0
        self.edits_in_window: int = 0
        self.window_start: float = time.time()



def beautify_text_layout(text: str) -> str:
    """Enhances mobile readability of Persian/English text:
    - Auto-wraps linux paths in code backticks.
    - Unglues sentences stuck together without spaces/newlines (e.g. 'میکنم.تست' -> 'میکنم.\\n\\nتست').
    - Fixes punctuation/brackets followed directly by Persian text (e.g. '):ضبط' -> '):\\n\\nضبط').
    - Breaks narrative action transitions on sentence ends ('. بگذارید' -> '.\\n\\nبگذارید').
    - Adds contextual emojis to narrative paragraphs (متوجه شدم -> 💡, بگذارید -> 🔍, پیدا شد -> 📁, پروژه -> ⚙️, حالا -> 📖).
    - Fixes emojis glued to hyphens (e.g. '✅- مدت' -> '✅\\n\\n• مدت').
    - Splits inline chained hyphenated specs ('- مدت: ۱ - تصویر: ۲') into separate lines.
    - Converts markdown bullet lists (- or *) into clean unicode bullets (• ).
    - Boldens spec keys before colons on bullet lines (• ⏱ مدت: -> • ⏱ **مدت:**).
    - Adds contextual emojis to technical specs (مدت -> ⏱, تصویر/ویدیو -> 📹, صدا -> 🎙, حجم -> 📦, مسیر -> 📁).
    - Normalizes excessive newlines to double newlines.
    """
    if not text:
        return ""

    # 1. Auto-wrap Linux filesystem paths in inline code backticks if not already in backticks
    text = re.sub(
        r'(?<![`a-zA-Z0-9_\/])(/(?:home|tmp|usr|var|etc|opt|srv)/[a-zA-Z0-9_\-\.\/]+)(?![`a-zA-Z0-9_\/])',
        r'`\1`',
        text
    )

    # 2. Emoji glued to hyphen/dash (e.g. ✅- or ⚠️-)
    text = re.sub(r'([✅❌⚠️ℹ️🚀💡🎯📌🔍📁📦💻🖥🎥🎙⏱🎬📸📊⚡🔔🔒🔑🌐📝🛠🔧])\s*[-–—]\s*', r'\1\n\n• ', text)

    # 3. Punctuation glued directly to Persian words without space (e.g. میکنم.تست or انجام شد.اکنون)
    text = re.sub(r'(?<=[آ-ی])([.؟!])(?=[آ-ی])', r'\1\n\n', text)

    # 4. Closing brackets/parentheses with optional colon glued directly to Persian words (e.g. ):ضبط or ]:ادامه)
    text = re.sub(r'([\)\]\}]:?)\s*(?=[آ-ی])', r'\1\n\n', text)

    # 5. Punctuation followed directly by emojis (e.g. است.✅ or شد:🚀)
    text = re.sub(r'([.؟!:])\s*(?=[✅❌⚠️ℹ️🚀💡🎯📌🔍📁📦💻🖥🎥🎙⏱🎬📸📊⚡🔔🔒🔑🌐📝🛠🔧])', r'\1\n\n', text)

    # 6. Break narrative/action transitions on sentence ends (e.g. 'بررسی کنم. بگذارید پیدایش کنم.' -> 'بررسی کنم.\n\nبگذارید پیدایش کنم.')
    action_starters = (
        r"بگذارید|اجازه دهید|پیدا شد|یافت شد|پروژه|حالا|اکنون|در حال|سپس|"
        r"همچنین|نتیجه|مسیر|فایل|پوشه|کد|دستور|تست|سرویس|خطا|موفق|توجه|نکته|خروجی|متوجه شدم"
    )
    text = re.sub(
        rf'(?<=[آ-ی0-9A-Za-z`])([.؟!])\s+(?=(?:{action_starters})\b)',
        r'\1\n\n',
        text
    )

    # 7. Split inline chained hyphenated specs on the same line
    # e.g. "• مدت: ۶۰ ثانیه - تصویر: 640x480 - صدا: فعال" -> splits before "- تصویر:"
    text = re.sub(r'(?<=[^\n])\s+[-–—]\s+(?=[آ-یa-zA-Z0-9_]+:)', r'\n• ', text)

    # 8. Convert line-starting markdown bullets (- or *) to clean bullet symbol
    text = re.sub(r'^[ \t]*[-*][ \t]+', r'• ', text, flags=re.MULTILINE)

    # 9. Contextual emojis for bullet items if not already prefixed with an emoji
    def bullet_emoji_repl(m):
        rest = m.group(2)
        # Check if already starts with an emoji or special symbol
        if re.match(r'^[\U00010000-\U0010ffff\u2600-\u27bf]', rest.strip()):
            return m.group(0)

        lower_head = rest[:40].lower()
        if any(k in lower_head for k in ("مدت", "زمان", "duration", "time")):
            return f"• ⏱ {rest}"
        elif any(k in lower_head for k in ("تصویر", "ویدیو", "دوربین", "رزولوشن", "کیفیت", "video", "camera", "resolution", "quality")):
            return f"• 📹 {rest}"
        elif any(k in lower_head for k in ("صدا", "میکروفون", "صوتی", "audio", "mic", "voice", "sound")):
            return f"• 🎙 {rest}"
        elif any(k in lower_head for k in ("حجم", "سایز", "اندازه", "size", "bytes")):
            return f"• 📦 {rest}"
        elif any(k in lower_head for k in ("مسیر", "فایل", "ذخیره", "پوشه", "path", "file", "folder", "dir")):
            return f"• 📁 {rest}"
        elif any(k in lower_head for k in ("سرعت", "نرخ", "fps", "speed", "bitrate")):
            return f"• ⚡ {rest}"
        elif any(k in lower_head for k in ("وضعیت", "status")):
            return f"• ℹ️ {rest}"
        elif any(k in lower_head for k in ("دستور", "فرمان", "کد", "command", "terminal")):
            return f"• 💻 {rest}"
        return m.group(0)

    text = re.sub(r'^(•\s+)(.+)$', bullet_emoji_repl, text, flags=re.MULTILINE)

    # 10. Bolden bullet item keys before colon (e.g. • ⏱ مدت: ۶۰ ثانیه -> • ⏱ **مدت:** ۶۰ ثانیه)
    def bullet_bold_key_repl(m):
        prefix = m.group(1)
        key = m.group(2).strip()
        val = m.group(3)
        if "*" in key or "_" in key:
            return m.group(0)
        return f"{prefix}**{key}:**{val}"

    text = re.sub(r'^(•\s+(?:[\U00010000-\U0010ffff\u2600-\u27bf]\s+)?)([^:\n*_\(\)\[\]]{2,30}):(\s+.+)$', bullet_bold_key_repl, text, flags=re.MULTILINE)

    # 11. Contextual emojis for narrative/action lines (not bullets or headings)
    def narrative_emoji_repl(m):
        line = m.group(0)
        s = line.strip()
        if not s or re.match(r'^[\U00010000-\U0010ffff\u2600-\u27bf•\-*#<>]', s):
            return line
        if re.match(r'^(?:متوجه شدم|فهمیدم|باشه|چشم)\b', s):
            return "💡 " + s
        elif re.match(r'^(?:بگذارید|اجازه دهید|در حال جستجو)\b', s):
            return "🔍 " + s
        elif re.match(r'^(?:پیدا شد|یافت شد)\b', s):
            return "📁 " + s
        elif re.match(r'^(?:پروژه)\b', s):
            return "⚙️ " + s
        elif re.match(r'^(?:حالا|اکنون|در حال بررسی|در حال خواندن)\b', s):
            return "📖 " + s
        elif re.match(r'^(?:تست|تست‌ها|آزمون)\b', s):
            return "🧪 " + s
        elif re.match(r'^(?:سرویس|سرویس‌ها)\b', s):
            return "🔧 " + s
        elif re.match(r'^(?:نتیجه)\b', s):
            return "📊 " + s
        elif re.match(r'^(?:خطا|مشکل)\b', s):
            return "❌ " + s
        elif re.match(r'^(?:موفقیت|انجام شد|تکمیل شد)\b', s):
            return "✅ " + s
        return line

    text = re.sub(r'^[^\n]+$', narrative_emoji_repl, text, flags=re.MULTILINE)

    # 12. Clean up 3+ newlines to max 2
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text


def format_to_telegram_html(text: str) -> str:
    """Safely converts standard Markdown LLM output to Telegram-compliant HTML.
    Handles code blocks, inline code, bold, italic, links, and headings."""
    if not text:
        return ""

    # Balance unclosed code blocks (for live streaming deltas)
    if text.count("```") % 2 == 1:
        text += "\n```"

    code_blocks = []
    def cb_repl(m):
        lang = m.group(1) or ""
        code = m.group(2)
        idx = len(code_blocks)
        cls = f' class="language-{lang.strip()}"' if lang.strip() else ""
        escaped_code = html.escape(code.rstrip())
        code_blocks.append(f'<pre><code{cls}>{escaped_code}</code></pre>')
        return f"TGPLACEHOLDERCB{idx}TG"

    text = re.sub(r'```([a-zA-Z0-9_\-\+]*)\n?(.*?)```', cb_repl, text, flags=re.DOTALL)

    # Balance unclosed inline code
    inline_codes = []
    if text.count("`") % 2 == 1:
        text += "`"

    def ic_repl(m):
        code = m.group(1)
        idx = len(inline_codes)
        inline_codes.append(f'<code>{html.escape(code)}</code>')
        return f"TGPLACEHOLDERIC{idx}TG"

    text = re.sub(r'`([^`\n]+)`', ic_repl, text)

    # Beautify prose layout, spacing, bullets, and emojis
    text = beautify_text_layout(text)

    # Escape raw HTML in prose
    text = html.escape(text)

    # Markdown Headings: # Heading -> <b>Heading</b>
    text = re.sub(r'^(?:#{1,6})\s*(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    # Bold: **bold** or __bold__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text, flags=re.DOTALL)

    # Italic: *italic* or _italic_
    text = re.sub(r'(?<!\w)\*([^\*\n]+)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_([^_\n]+)_(?!\w)', r'<i>\1</i>', text)

    # Links: [text](url)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\s\)]+)\)', r'<a href="\2">\1</a>', text)

    # Restore placeholders
    for i, cb in enumerate(code_blocks):
        text = text.replace(f"TGPLACEHOLDERCB{i}TG", cb)
    for i, ic in enumerate(inline_codes):
        text = text.replace(f"TGPLACEHOLDERIC{i}TG", ic)

    return text


class TelegramAPI:
    def __init__(self, token: str, proxy: Optional[str] = None):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.proxy = proxy
        self._session: Optional[aiohttp.ClientSession] = None
        self.chat_throttles: Dict[int, ChatThrottle] = {}
        self.flood_wait_until: Dict[int, float] = {}
        self.global_flood_wait_until: float = 0.0

    def get_throttle(self, chat_id: int) -> ChatThrottle:
        if chat_id not in self.chat_throttles:
            self.chat_throttles[chat_id] = ChatThrottle()
        return self.chat_throttles[chat_id]

    def get_flood_wait(self, chat_id: Optional[int] = None) -> float:
        now = time.time()
        g_wait = max(0.0, self.global_flood_wait_until - now)
        c_wait = max(0.0, self.flood_wait_until.get(chat_id, 0.0) - now) if chat_id else 0.0
        return max(g_wait, c_wait)

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        session = await self.get_session()
        url = f"{self.base_url}/{endpoint}"
        if self.proxy and "proxy" not in kwargs:
            kwargs["proxy"] = self.proxy

        chat_id = None
        if "json" in kwargs and isinstance(kwargs["json"], dict):
            chat_id = kwargs["json"].get("chat_id")
        elif "params" in kwargs and isinstance(kwargs["params"], dict):
            chat_id = kwargs["params"].get("chat_id")

        for attempt in range(4):
            # Circuit breaker: suppress request if chat is under flood wait
            wait = self.get_flood_wait(chat_id)
            if wait > 0:
                logger.debug("Request to %s for chat %s suppressed by circuit breaker (%ds remaining).", endpoint, chat_id, int(wait))
                return {"ok": False, "error_code": 429, "description": f"Flood wait active ({int(wait)}s remaining)"}

            try:
                async with session.request(method, url, **kwargs) as resp:
                    data = await resp.json()
                    if resp.status == 429:
                        retry_after = data.get("parameters", {}).get("retry_after", 5)
                        logger.warning("Telegram 429 rate limit on %s (retry_after=%ss)", endpoint, retry_after)
                        if chat_id:
                            self.flood_wait_until[chat_id] = time.time() + retry_after
                        else:
                            self.global_flood_wait_until = time.time() + retry_after

                        if endpoint in ("editMessageText", "sendChatAction", "setMessageReaction", "pinChatMessage", "unpinChatMessage"):
                            return {"ok": False, "error_code": 429, "description": f"Rate limited on {endpoint} ({retry_after}s)"}

                        if retry_after <= 3:
                            await asyncio.sleep(retry_after)
                            continue
                        return {"ok": False, "error_code": 429, "description": f"Flood wait active ({retry_after}s)"}

                    if not data.get("ok"):
                        err_desc = data.get("description", "Unknown error")
                        if "message is not modified" not in err_desc and "can't parse entities" not in err_desc:
                            logger.warning("Telegram API error (%s): %s", endpoint, err_desc)
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning("Network error calling Telegram (%s): %s (attempt %d/4)", endpoint, e, attempt + 1)
                await asyncio.sleep(1 + attempt)
        return {"ok": False, "description": "Max retries exceeded"}

    async def get_me(self) -> Dict[str, Any]:
        return await self.request("GET", "getMe")

    async def get_updates(self, offset: int = 0, timeout: int = 30) -> Dict[str, Any]:
        params = {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query", "stopped_message_generation"]}
        return await self.request("GET", "getUpdates", params=params, timeout=aiohttp.ClientTimeout(total=timeout + 15))

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: Optional[str] = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None,
        reply_to_message_id: Optional[int] = None,
        reply_parameters: Optional[Dict[str, Any]] = None,
        link_preview_options: Optional[Dict[str, Any]] = None,
        ephemeral_message_parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        wait = self.get_flood_wait(chat_id)
        if wait > 0:
            logger.warning("sendMessage to chat %s suppressed: %ds flood wait remaining.", chat_id, int(wait))
            return {"ok": False, "error_code": 429, "description": f"Suppressed by flood circuit breaker ({int(wait)}s)"}

        throttle = self.get_throttle(chat_id)
        async with throttle.lock:
            now = time.time()
            elapsed = now - throttle.last_send_time
            if elapsed < 1.2:
                await asyncio.sleep(1.2 - elapsed)

            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_markup:
                payload["reply_markup"] = reply_markup

            # Modern Telegram Bot API 7.0+ reply_parameters
            if reply_parameters:
                payload["reply_parameters"] = reply_parameters
            elif reply_to_message_id:
                payload["reply_parameters"] = {
                    "message_id": reply_to_message_id,
                    "allow_sending_without_reply": True
                }

            # Modern Telegram Bot API 7.0+ link_preview_options
            if link_preview_options is not None:
                payload["link_preview_options"] = link_preview_options
            else:
                payload["link_preview_options"] = {"is_disabled": True}

            # Modern Telegram Bot API 10.2+ ephemeral_message_parameters
            if ephemeral_message_parameters:
                payload["ephemeral_message_parameters"] = ephemeral_message_parameters

            throttle.last_send_time = time.time()
            res = await self.request("POST", "sendMessage", json=payload)
            if not res.get("ok"):
                # If ephemeral failed (e.g. BOT_NOT_ADMIN in group), retry without ephemeral_message_parameters
                if ephemeral_message_parameters and "BOT_NOT_ADMIN" in res.get("description", ""):
                    payload.pop("ephemeral_message_parameters", None)
                    res = await self.request("POST", "sendMessage", json=payload)

                if not res.get("ok") and parse_mode:
                    err = res.get("description", "")
                    if "can't parse entities" in err or "entity" in err:
                        payload.pop("parse_mode", None)
                        payload["text"] = re.sub(r"<[^>]+>", "", text)
                        res = await self.request("POST", "sendMessage", json=payload)
            return res

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: Optional[str] = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None,
        link_preview_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        wait = self.get_flood_wait(chat_id)
        if wait > 0:
            return {"ok": False, "error_code": 429, "description": f"Suppressed by flood circuit breaker ({int(wait)}s)"}

        throttle = self.get_throttle(chat_id)
        async with throttle.lock:
            now = time.time()
            if now - throttle.window_start > 60:
                throttle.window_start = now
                throttle.edits_in_window = 0

            # Dynamic backoff: 2.2s normal, 4.0s under high frequency (> 20 edits in 60s)
            min_gap = 4.0 if throttle.edits_in_window >= 20 else 2.2
            elapsed = now - throttle.last_edit_time
            if elapsed < min_gap:
                await asyncio.sleep(min_gap - elapsed)

            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_markup:
                payload["reply_markup"] = reply_markup

            # Modern Telegram Bot API 7.0+ link_preview_options
            if link_preview_options is not None:
                payload["link_preview_options"] = link_preview_options
            else:
                payload["link_preview_options"] = {"is_disabled": True}

            throttle.last_edit_time = time.time()
            throttle.edits_in_window += 1

            res = await self.request("POST", "editMessageText", json=payload)
            if not res.get("ok"):
                err = res.get("description", "")
                if "message is not modified" in err:
                    return {"ok": True}
                if parse_mode and ("can't parse entities" in err or "entity" in err):
                    payload.pop("parse_mode", None)
                    payload["text"] = re.sub(r"<[^>]+>", "", text)
                    res = await self.request("POST", "editMessageText", json=payload)
            return res

    async def send_message_draft(
        self,
        chat_id: int,
        draft_id: int,
        text: str,
        parse_mode: Optional[str] = "HTML",
        can_stop: bool = True,
        keep_on_stop: bool = False,
    ) -> Dict[str, Any]:
        """Stream real-time animated draft preview (Telegram Bot API native streaming)."""
        wait = self.get_flood_wait(chat_id)
        if wait > 0:
            return {"ok": False, "error_code": 429, "description": f"Suppressed by flood circuit breaker ({int(wait)}s)"}

        throttle = self.get_throttle(chat_id)
        async with throttle.lock:
            now = time.time()
            elapsed = now - throttle.last_send_time
            if elapsed < 0.85:
                await asyncio.sleep(0.85 - elapsed)

            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "draft_id": draft_id,
                "text": text,
                "can_stop": can_stop,
                "keep_on_stop": keep_on_stop,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode

            throttle.last_send_time = time.time()
            res = await self.request("POST", "sendMessageDraft", json=payload)
            if not res.get("ok") and parse_mode:
                err = res.get("description", "")
                if "can't parse entities" in err or "entity" in err:
                    payload.pop("parse_mode", None)
                    payload["text"] = re.sub(r"<[^>]+>", "", text)
                    res = await self.request("POST", "sendMessageDraft", json=payload)
            return res

    async def send_rich_message_draft(
        self,
        chat_id: int,
        draft_id: int,
        html_content: str,
        is_rtl: bool = True,
        can_stop: bool = True,
        keep_on_stop: bool = False,
    ) -> Dict[str, Any]:
        """Stream real-time animated rich draft preview with thinking blocks (Telegram Bot API 10.2+)."""
        wait = self.get_flood_wait(chat_id)
        if wait > 0:
            return {"ok": False, "error_code": 429, "description": f"Suppressed by flood circuit breaker ({int(wait)}s)"}

        throttle = self.get_throttle(chat_id)
        async with throttle.lock:
            now = time.time()
            elapsed = now - throttle.last_send_time
            if elapsed < 0.85:
                await asyncio.sleep(0.85 - elapsed)

            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "draft_id": draft_id,
                "rich_message": {
                    "html": html_content,
                    "is_rtl": is_rtl
                },
                "can_stop": can_stop,
                "keep_on_stop": keep_on_stop,
            }
            throttle.last_send_time = time.time()
            return await self.request("POST", "sendRichMessageDraft", json=payload)

    async def send_rich_message(
        self,
        chat_id: int,
        html_content: str,
        is_rtl: bool = True,
        reply_markup: Optional[Dict[str, Any]] = None,
        reply_parameters: Optional[Dict[str, Any]] = None,
        ephemeral_message_parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send rich message with native RTL and expandable blocks (Telegram Bot API 10.2+)."""
        wait = self.get_flood_wait(chat_id)
        if wait > 0:
            return {"ok": False, "error_code": 429, "description": f"Suppressed by flood circuit breaker ({int(wait)}s)"}

        throttle = self.get_throttle(chat_id)
        async with throttle.lock:
            now = time.time()
            elapsed = now - throttle.last_send_time
            if elapsed < 1.2:
                await asyncio.sleep(1.2 - elapsed)

            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "rich_message": {
                    "html": html_content,
                    "is_rtl": is_rtl
                }
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            if reply_parameters:
                payload["reply_parameters"] = reply_parameters
            if ephemeral_message_parameters:
                payload["ephemeral_message_parameters"] = ephemeral_message_parameters

            throttle.last_send_time = time.time()
            return await self.request("POST", "sendRichMessage", json=payload)

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Edit only the inline reply markup of an existing message (Bot API)."""
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self.request("POST", "editMessageReplyMarkup", json=payload)

    async def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None, show_alert: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text:
            payload["text"] = text
        return await self.request("POST", "answerCallbackQuery", json=payload)

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> Dict[str, Any]:
        wait = self.get_flood_wait(chat_id)
        if wait > 0:
            return {"ok": False, "error_code": 429}
        throttle = self.get_throttle(chat_id)
        now = time.time()
        if now - throttle.last_action_time < 5.0:
            return {"ok": True}
        throttle.last_action_time = now
        return await self.request("POST", "sendChatAction", json={"chat_id": chat_id, "action": action})

    async def set_message_reaction(self, chat_id: int, message_id: int, emoji: str) -> Dict[str, Any]:
        """React to a message with an emoji (Bot API 7.0+)."""
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}]
        }
        return await self.request("POST", "setMessageReaction", json=payload)

    async def pin_chat_message(self, chat_id: int, message_id: int, disable_notification: bool = True) -> Dict[str, Any]:
        """Pin incoming user message as a visual activity indicator during task."""
        payload = {"chat_id": chat_id, "message_id": message_id, "disable_notification": disable_notification}
        return await self.request("POST", "pinChatMessage", json=payload)

    async def unpin_chat_message(self, chat_id: int, message_id: int) -> Dict[str, Any]:
        """Unpin message once agent task completes."""
        payload = {"chat_id": chat_id, "message_id": message_id}
        return await self.request("POST", "unpinChatMessage", json=payload)

    async def set_my_short_description(self, short_description: str) -> Dict[str, Any]:
        """Set online/offline status indicator in bot's profile line."""
        payload = {"short_description": short_description}
        return await self.request("POST", "setMyShortDescription", json=payload)

    async def send_photo(
        self,
        chat_id: int,
        photo_path: str,
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = "HTML",
    ) -> Dict[str, Any]:
        """Send a photo/image directly to the user."""
        session = await self.get_session()
        url = f"{self.base_url}/sendPhoto"
        data = aiohttp.FormData()
        data.add_field("chat_id", str(chat_id))
        if caption:
            data.add_field("caption", caption)
        if parse_mode:
            data.add_field("parse_mode", parse_mode)
        if reply_to_message_id:
            data.add_field("reply_parameters", json.dumps({"message_id": reply_to_message_id, "allow_sending_without_reply": True}))

        with open(photo_path, "rb") as f:
            photo_bytes = f.read()
        data.add_field("photo", photo_bytes, filename=os.path.basename(photo_path))

        kwargs: Dict[str, Any] = {}
        if self.proxy:
            kwargs["proxy"] = self.proxy

        try:
            async with session.post(url, data=data, **kwargs) as resp:
                return await resp.json()
        except Exception as e:
            logger.error("Failed to send photo: %s", e)
            return {"ok": False, "description": str(e)}

    async def send_document(
        self,
        chat_id: int,
        file_path: str,
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = "HTML",
    ) -> Dict[str, Any]:
        """Send a document or file directly from the filesystem to the user."""
        session = await self.get_session()
        url = f"{self.base_url}/sendDocument"
        data = aiohttp.FormData()
        data.add_field("chat_id", str(chat_id))
        if caption:
            data.add_field("caption", caption)
        if parse_mode:
            data.add_field("parse_mode", parse_mode)
        if reply_to_message_id:
            data.add_field("reply_parameters", json.dumps({"message_id": reply_to_message_id, "allow_sending_without_reply": True}))

        filename = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        data.add_field("document", file_bytes, filename=filename)

        kwargs: Dict[str, Any] = {}
        if self.proxy:
            kwargs["proxy"] = self.proxy

        try:
            async with session.post(url, data=data, **kwargs) as resp:
                return await resp.json()
        except Exception as e:
            logger.error("Failed to send document: %s", e)
            return {"ok": False, "description": str(e)}

    async def get_file(self, file_id: str) -> Dict[str, Any]:
        return await self.request("GET", "getFile", params={"file_id": file_id})

    async def download_file(self, file_path: str, dest_path: str) -> bool:
        session = await self.get_session()
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        kwargs: Dict[str, Any] = {}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        try:
            async with session.get(url, **kwargs) as resp:
                if resp.status == 200:
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    with open(dest_path, "wb") as f:
                        f.write(await resp.read())
                    return True
        except Exception as e:
            logger.error("Failed to download file from Telegram: %s", e)
        return False

    async def set_my_commands(self, commands: List[Dict[str, Any]], scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"commands": commands}
        if scope:
            payload["scope"] = scope
        return await self.request("POST", "setMyCommands", json=payload)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


class StreamSession:
    """Manages active streaming message buffer and live Hermes-style execution tracking for an OpenCode turn."""
    def __init__(
        self,
        chat_id: int,
        initial_message_id: int,
        tg: TelegramAPI,
        user_message_id: Optional[int] = None,
        workspace_dir: str = "",
        on_workdir_change: Optional[Callable[[str], Any]] = None,
        on_channel_post: Optional[Callable[[str, str], Any]] = None,
    ):
        self.chat_id = chat_id
        self.current_message_id = initial_message_id
        self.user_message_id = user_message_id
        self.tg = tg
        self.workspace_dir = workspace_dir or config.OPENCODE_WORKSPACE_DIR
        self.on_workdir_change = on_workdir_change
        self.on_channel_post = on_channel_post

        # Live execution & Hermes-style tracking
        self.activity_entries: List[Dict[str, Any]] = []  # [{"key": str, "text": str, "count": int}]
        self.seen_tool_parts: Dict[str, int] = {}  # call_id -> index in activity_entries
        self.seen_text_parts: Dict[str, int] = {}  # part_id -> length ingested
        self.current_step: int = 1
        self.max_steps: int = 150
        self.start_time: float = time.time()
        self.status_note: str = "waiting for provider response (streaming)"
        self.rate_limited: bool = False
        self.skill_created: Optional[str] = None
        self.reasoning_text: str = ""
        self.draft_id: int = random.randint(100000, 999999)
        self.use_draft: bool = (self.chat_id > 0)
        self.last_text_delta_time: float = 0.0
        self.current_running_tool: Optional[str] = None
        self.tool_start_time: float = 0.0

        self.full_text = ""
        self.current_chunk = ""
        self.last_edit_time = 0.0
        self.last_rendered_text = ""
        self.scheduled_task: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()
        self.is_done = False
        self.ticker_task: Optional[asyncio.Task] = asyncio.create_task(self._ticker_loop())

    def cancel(self):
        """Immediately cancel background ticker and scheduled tasks."""
        self.is_done = True
        if self.ticker_task and not self.ticker_task.done():
            self.ticker_task.cancel()
        if self.scheduled_task and not self.scheduled_task.done():
            self.scheduled_task.cancel()

    def append_reasoning(self, delta: str):
        if delta:
            self.reasoning_text += delta
            self.current_running_tool = None
            self.status_note = "thinking / analyzing..."

    def _format_tool_entry(self, tool_name: str, input_data: dict, title: str) -> str:
        tool_lower = (tool_name or "").lower().strip()
        home = os.path.expanduser("~")

        def clean_p(p: str) -> str:
            if not p:
                return ""
            if p.startswith(home):
                return "~" + p[len(home):]
            if self.workspace_dir and p.startswith(self.workspace_dir):
                rel = os.path.relpath(p, self.workspace_dir)
                if not rel.startswith(".."):
                    return rel
            return p

        # 1. Shell / Terminal (e.g. 💻 terminal: cat ...)
        if tool_lower in ("bash", "shell", "sh", "exec", "terminal"):
            cmd = input_data.get("command") or title or ""
            cmd_lines = cmd.strip().split("\n")
            first_line = cmd_lines[0].strip() if cmd_lines else ""
            if len(cmd_lines) > 1 and len(first_line) > 40:
                first_line = first_line[:37] + " ..."
            elif len(cmd_lines) > 1:
                first_line = first_line + " ..."
            elif len(first_line) > 55:
                first_line = first_line[:52] + "..."
            return f"💻 terminal: <code>{html.escape(first_line)}</code>" if first_line else "💻 terminal"

        # 2. Reading files (e.g. 📖 Reading notify-forwarder.service)
        elif tool_lower in ("read", "view_file", "cat"):
            p = input_data.get("filePath") or input_data.get("path") or title or ""
            p_str = clean_p(p)
            s_line = input_data.get("startLine") or input_data.get("StartLine")
            e_line = input_data.get("endLine") or input_data.get("EndLine")
            lines = ""
            if s_line and e_line:
                lines = f" L{s_line}-{e_line}"
            elif s_line:
                lines = f" L{s_line}"
            return f"📖 Reading <code>{html.escape(p_str)}</code>{lines}" if p_str else "📖 Reading file"

        # 3. Writing files (e.g. ✍️ Writing ~/.local/bin/notify-forwarder.py)
        elif tool_lower in ("write", "write_to_file", "create_file"):
            p = input_data.get("filePath") or input_data.get("path") or title or ""
            p_str = clean_p(p)
            if "skill" in p.lower() or "SKILL.md" in p:
                skill_name = os.path.basename(os.path.dirname(p)) if p.endswith("SKILL.md") else os.path.basename(p)
                self.skill_created = skill_name
            return f"✍️ Writing <code>{html.escape(p_str)}</code>" if p_str else "✍️ Writing file"

        # 4. Editing files (e.g. ✏️ Editing notify-forwarder.py)
        elif tool_lower in ("edit", "replace_file_content", "patch"):
            p = input_data.get("filePath") or input_data.get("path") or title or ""
            p_str = clean_p(p)
            return f"✍️ Writing <code>{html.escape(p_str)}</code>" if p_str else "✍️ Writing file"

        # 5. Searching (e.g. 🔍 Searching ...)
        elif tool_lower in ("grep", "grep_search", "find", "find_files", "find_by_name"):
            q = input_data.get("query") or input_data.get("pattern") or title or ""
            if len(q) > 40:
                q = q[:37] + "..."
            return f"🔍 Searching <code>{html.escape(q)}</code>" if q else "🔍 Searching files"

        # 6. Web Search
        elif tool_lower in ("web_search", "search_web", "websearch"):
            q = input_data.get("query") or title or ""
            if len(q) > 40:
                q = q[:37] + "..."
            return f"🌐 Web search: <code>{html.escape(q)}</code>" if q else "🌐 Web search"

        # 7. Process / wait (e.g. ⚙️ process: "wait proc_... 10s")
        elif tool_lower in ("process", "wait", "sleep"):
            detail = input_data.get("detail") or title or ""
            return f"⚙️ process: <code>{html.escape(detail)}</code>" if detail else "⚙️ process"

        # 8. Fallback
        else:
            desc = title or input_data.get("command") or input_data.get("query") or ""
            if len(desc) > 35:
                desc = desc[:32] + "..."
            return f"🔧 {html.escape(tool_lower)}" + (f": <code>{html.escape(desc)}</code>" if desc else "")

    def append_step_separator(self):
        """Ensures clean paragraph separation between multi-step assistant turns and tool results."""
        if self.full_text and not self.full_text.endswith("\n\n"):
            if self.full_text.endswith("\n"):
                self.full_text += "\n"
            else:
                self.full_text += "\n\n"
        if self.current_chunk and not self.current_chunk.endswith("\n\n"):
            if self.current_chunk.endswith("\n"):
                self.current_chunk += "\n"
            else:
                self.current_chunk += "\n\n"

    async def record_tool(self, call_id: str, tool_name: str, input_data: dict, title: str, status: str):
        async with self.lock:
            if not tool_name:
                return

            self.append_step_separator()

            entry_text = self._format_tool_entry(tool_name, input_data, title)
            if not entry_text:
                return

            self.current_running_tool = entry_text
            self.tool_start_time = time.time()
            self.status_note = f"executing {tool_name}..."

            # If we already recorded this call_id, check if we can improve generic text with specifics
            if call_id in self.seen_tool_parts:
                idx = self.seen_tool_parts[call_id]
                if idx < len(self.activity_entries):
                    old_text = self.activity_entries[idx]["text"]
                    if ("<code>" not in old_text and "<code>" in entry_text) or len(entry_text) > len(old_text):
                        self.activity_entries[idx]["text"] = entry_text
                        self.activity_entries[idx]["key"] = entry_text
                        await self._schedule_or_edit()
                return

            # Collapse identical consecutive tool actions (e.g. Writing file (×3))
            if self.activity_entries and self.activity_entries[-1]["key"] == entry_text:
                self.activity_entries[-1]["count"] += 1
                self.seen_tool_parts[call_id] = len(self.activity_entries) - 1
            else:
                self.activity_entries.append({
                    "key": entry_text,
                    "text": entry_text,
                    "count": 1
                })
                self.seen_tool_parts[call_id] = len(self.activity_entries) - 1

            await self._schedule_or_edit()

    async def on_step_start(self):
        async with self.lock:
            self.current_step += 1
            self.current_running_tool = None
            self.append_step_separator()
            self.status_note = "receiving stream response" if self.full_text else "waiting for provider response (streaming)"
            await self._schedule_or_edit()

    async def on_rate_limit(self):
        async with self.lock:
            self.rate_limited = True
            await self._schedule_or_edit()

    async def append_delta(self, delta: str):
        async with self.lock:
            if not delta:
                return
            self.last_text_delta_time = time.time()
            self.current_running_tool = None
            self.full_text += delta
            self.current_chunk += delta
            self.status_note = "receiving stream response"

            # Check if current chunk exceeds Telegram message limit (~3800 chars)
            if len(self.current_chunk) >= config.MAX_MESSAGE_LENGTH:
                if self.use_draft:
                    chunk_text = format_to_telegram_html(self.current_chunk.strip())
                    await self.tg.send_rich_message(
                        self.chat_id,
                        chunk_text,
                        is_rtl=True,
                        reply_parameters={"message_id": self.user_message_id, "allow_sending_without_reply": True} if self.user_message_id else None
                    )
                    self.current_chunk = ""
                    self.draft_id = random.randint(100000, 999999)
                else:
                    await self._do_edit()
                    new_msg = await self.tg.send_message(
                        self.chat_id,
                        "✍️ <i>(ادامه پاسخ... / Continuing...)</i>",
                        parse_mode="HTML"
                    )
                    if new_msg.get("ok"):
                        self.current_message_id = new_msg["result"]["message_id"]
                        self.current_chunk = ""
                    else:
                        logger.error("Failed to split message: %s", new_msg)

            await self._schedule_or_edit()

    def _render_activity_block(self) -> str:
        if not self.activity_entries:
            return ""
        # Keep recent entries to stay within message limits
        recent = self.activity_entries[-10:]
        lines = []
        for item in recent:
            count_suffix = f" (×{item['count']})" if item['count'] > 1 else ""
            lines.append(f"• {item['text']}{count_suffix}")

        inner = "\n".join(lines)
        total_count = len(self.activity_entries)
        header = f"🛠 <b>ابزارهای اجراشده ({total_count} مورد):</b>\n\n"
        return f"<blockquote expandable>{header}{inner}</blockquote>"

    def _render_reasoning_block(self) -> str:
        if not self.reasoning_text:
            return ""
        reason = self.reasoning_text.strip()
        if len(reason) > 1200:
            reason = reason[:1180] + "..."
        escaped_reason = html.escape(reason)
        return f"<blockquote expandable>🧠 <b>تفکر و تحلیل مدل (Reasoning):</b>\n\n{escaped_reason}</blockquote>"

    def _render_status_banner(self) -> str:
        if self.is_done:
            return ""
        elapsed = int(time.time() - self.start_time)
        elapsed_min = elapsed // 60
        elapsed_sec = elapsed % 60
        time_str = f"{elapsed_min}m {elapsed_sec}s" if elapsed_min > 0 else f"{elapsed_sec}s"
        total_tools = len(self.activity_entries)

        lines = []
        if self.current_running_tool:
            tool_elapsed = int(time.time() - self.tool_start_time)
            t_str = f" ({tool_elapsed}s)" if tool_elapsed >= 2 else ""
            lines.append(f"⚡ <b>در حال اجرا:</b> {self.current_running_tool}{t_str}")
        elif self.status_note:
            lines.append(f"🧠 <i>{html.escape(self.status_note)}</i>")

        lines.append(f"⏳ <b>زمان کل:</b> {time_str} | <b>گام:</b> {self.current_step}/{self.max_steps} | <b>ابزارها:</b> {total_tools}")

        if self.rate_limited:
            lines.append("⏱️ <i>محدودیت نرخ (Rate Limit) ارائه‌دهنده مدل فعال است. لطفاً کمی شکیبا باشید...</i>")

        inner = "\n".join(lines)
        return f"<blockquote>{inner}</blockquote>"

    def _render_full_display(self) -> str:
        parts = []
        act = self._render_activity_block()
        if act:
            parts.append(act)

        reason = self._render_reasoning_block()
        if reason:
            parts.append(reason)

        body = self.current_chunk.strip()
        is_actively_typing = (time.time() - self.last_text_delta_time) < 2.5
        if body:
            formatted_body = format_to_telegram_html(body)
            cursor = " ▌" if (not self.is_done and is_actively_typing) else ""
            parts.append(formatted_body + cursor)
        elif not act and not reason and not self.is_done:
            parts.append("🧠 <i>در حال پردازش و تحلیل...</i> ▌")

        if not self.is_done:
            parts.append(self._render_status_banner())

        return "\n\n".join(p for p in parts if p).strip()

    async def _ticker_loop(self):
        try:
            while not self.is_done:
                await asyncio.sleep(2.0)
                if self.is_done:
                    break
                now = time.time()
                # Stop ticker after 10 minutes to avoid runaway background edits
                if (now - self.start_time) > 600:
                    break
                if (now - self.last_edit_time) >= 2.0:
                    async with self.lock:
                        if not self.is_done:
                            await self._do_edit()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Ticker loop error: %s", e)

    async def _schedule_or_edit(self):
        now = time.time()
        elapsed_ms = (now - self.last_edit_time) * 1000
        if elapsed_ms >= config.STREAM_THROTTLE_MS:
            if self.scheduled_task and not self.scheduled_task.done():
                self.scheduled_task.cancel()
            await self._do_edit()
        else:
            if self.scheduled_task is None or self.scheduled_task.done():
                delay = (config.STREAM_THROTTLE_MS - elapsed_ms) / 1000.0
                self.scheduled_task = asyncio.create_task(self._delayed_edit(delay))

    async def _delayed_edit(self, delay: float):
        try:
            await asyncio.sleep(delay)
            async with self.lock:
                await self._do_edit()
        except asyncio.CancelledError:
            pass

    async def _do_edit(self):
        display_text = self._render_full_display()
        if not display_text or display_text == self.last_rendered_text:
            return
        self.last_rendered_text = display_text
        self.last_edit_time = time.time()
        if len(display_text) > 3900:
            display_text = display_text[:3900]

        # Use native Telegram Bot API 10.2+ sendRichMessageDraft with RTL
        if self.use_draft:
            draft_html = display_text

            res = await self.tg.send_rich_message_draft(
                self.chat_id,
                self.draft_id,
                draft_html,
                is_rtl=True,
                can_stop=True,
                keep_on_stop=False
            )
            if res.get("ok"):
                return

            res = await self.tg.send_message_draft(
                self.chat_id,
                self.draft_id,
                display_text,
                parse_mode="HTML",
                can_stop=True
            )
            if res.get("ok"):
                return

            clean_text = re.sub(r"<[^>]+>", "", display_text)
            res = await self.tg.send_message_draft(
                self.chat_id,
                self.draft_id,
                clean_text,
                parse_mode=None,
                can_stop=True
            )
            if res.get("ok"):
                return

            if self.chat_id > 0:
                # In private chats, never fall back to editMessageText; stay strictly in stream draft mode
                return
            else:
                self.use_draft = False

        if not self.current_message_id:
            init_res = await self.tg.send_message(
                self.chat_id,
                display_text,
                parse_mode="HTML",
                reply_to_message_id=self.user_message_id,
                link_preview_options={"is_disabled": True}
            )
            if init_res.get("ok"):
                self.current_message_id = init_res["result"]["message_id"]
            return

        res = await self.tg.edit_message_text(
            self.chat_id,
            self.current_message_id,
            display_text,
            parse_mode="HTML"
        )
        if isinstance(res, dict) and not res.get("ok"):
            err_desc = res.get("description", "")
            if "can't parse entities" in err_desc or "entity" in err_desc:
                clean = re.sub(r"<[^>]+>", "", display_text)
                await self.tg.edit_message_text(
                    self.chat_id,
                    self.current_message_id,
                    clean,
                    parse_mode=""
                )

    async def finish(self, success: bool = True):
        async with self.lock:
            self.is_done = True
            if self.ticker_task and not self.ticker_task.done():
                self.ticker_task.cancel()
            if self.scheduled_task and not self.scheduled_task.done():
                self.scheduled_task.cancel()

            display_text = self.current_chunk.strip() or self.full_text.strip()

            # 1. Auto-deliver MEDIA / FILE / SEND_FILE tags natively
            file_patterns = [
                r"MEDIA:\s*[\"']?([^\s\"'\n]+)[\"']?",
                r"FILE:\s*[\"']?([^\s\"'\n]+)[\"']?",
                r"SEND_FILE:\s*[\"']?([^\s\"'\n]+)[\"']?",
                r"DOWNLOAD:\s*[\"']?([^\s\"'\n]+)[\"']?",
            ]
            delivered_paths = set()
            for fpat in file_patterns:
                for mpath in re.findall(fpat, self.full_text):
                    if mpath in delivered_paths:
                        continue
                    delivered_paths.add(mpath)
                    full_path = mpath if os.path.isabs(mpath) else os.path.join(self.workspace_dir, mpath)
                    if not os.path.exists(full_path):
                        exp = os.path.expanduser(mpath)
                        if os.path.exists(exp):
                            full_path = exp
                    if os.path.exists(full_path) and os.path.isfile(full_path):
                        ext = os.path.splitext(full_path)[1].lower()
                        caption = f"📎 فایل ضمیمه: <code>{html.escape(os.path.basename(full_path))}</code>"
                        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                            asyncio.create_task(self.tg.send_photo(self.chat_id, full_path, caption=caption, parse_mode="HTML"))
                        else:
                            asyncio.create_task(self.tg.send_document(self.chat_id, full_path, caption=caption, parse_mode="HTML"))

            # 2. Check for WORKDIR / CD directives
            workdir_matches = re.findall(r"(?:WORKDIR|CD):\s*([^\s\n]+)", self.full_text)
            for wdir in workdir_matches:
                resolved = resolve_path(wdir, self.workspace_dir)
                if resolved and os.path.isdir(resolved):
                    if self.on_workdir_change:
                        asyncio.create_task(self.on_workdir_change(resolved))

            # 3. Check for TELEGRAM_POST / CHANNEL_POST directives
            post_matches = re.findall(
                r"(?:TELEGRAM_POST|CHANNEL_POST|POST_TO_CHANNEL):\s*(@\w+|-\d+)\s*:\s*(.+?)(?=\n[A-Z_]+:|$)",
                self.full_text,
                re.DOTALL
            )
            for target_channel, post_text in post_matches:
                post_text = post_text.strip()
                if self.on_channel_post:
                    asyncio.create_task(self.on_channel_post(target_channel, post_text))

            # Remove raw directive lines from display text for elegance
            clean_display_text = display_text
            for pat in [
                r"MEDIA:\s*[\"']?[^\s\"'\n]+[\"']?",
                r"FILE:\s*[\"']?[^\s\"'\n]+[\"']?",
                r"SEND_FILE:\s*[\"']?[^\s\"'\n]+[\"']?",
                r"DOWNLOAD:\s*[\"']?[^\s\"'\n]+[\"']?",
                r"WORKDIR:\s*[^\s\n]+",
                r"CD:\s*[^\s\n]+",
                r"(?:TELEGRAM_POST|CHANNEL_POST|POST_TO_CHANNEL):\s*(@\w+|-\d+)\s*:\s*.+?(?=\n[A-Z_]+:|$)"
            ]:
                clean_display_text = re.sub(pat, "", clean_display_text, flags=re.DOTALL)

            clean_display_text = clean_display_text.strip()

            # Assemble final text with activity summary & reasoning
            act = self._render_activity_block()
            reason = self._render_reasoning_block()
            sections = []
            if act:
                sections.append(act)
            if reason:
                sections.append(reason)
            if clean_display_text:
                sections.append(format_to_telegram_html(clean_display_text))
            elif not act and not reason:
                sections.append("✅ <i>عملیات با موفقیت انجام شد.</i>" if success else "❌ <i>عملیات متوقف شد.</i>")

            # Self-improvement skill notice (Hermes parity)
            if self.skill_created:
                sections.append(f"💾 <b>Self-improvement review:</b> Skill <code>{html.escape(self.skill_created)}</code> created.")

            final_text = "\n\n".join(sections).strip()

            # Safety trim for Telegram 4096 char limit
            if len(final_text) > 3900:
                if len(clean_display_text) > 3000:
                    clean_display_text = clean_display_text[:3000] + "\n...(خلاصه شد)"
                formatted_body = format_to_telegram_html(clean_display_text)
                final_parts = [p for p in [act, reason, formatted_body] if p]
                final_text = "\n\n".join(final_parts)
                if len(final_text) > 3900:
                    final_text = final_text[:3900]

            if self.use_draft or not self.current_message_id:
                # Deliver final permanent message to commit from draft (Bot API 10.2+ RichMessage with RTL)
                self.last_rendered_text = final_text
                reply_params = {
                    "message_id": self.user_message_id,
                    "allow_sending_without_reply": True
                } if self.user_message_id else None

                res = await self.tg.send_rich_message(
                    self.chat_id,
                    final_text,
                    is_rtl=True,
                    reply_parameters=reply_params
                )
                if res.get("ok"):
                    self.current_message_id = res["result"]["message_id"]
                else:
                    res = await self.tg.send_message(
                        self.chat_id,
                        final_text,
                        parse_mode="HTML",
                        reply_to_message_id=self.user_message_id,
                        link_preview_options={"is_disabled": True}
                    )
                    if res.get("ok"):
                        self.current_message_id = res["result"]["message_id"]
                    else:
                        clean = re.sub(r"<[^>]+>", "", final_text)
                        fallback_res = await self.tg.send_message(
                            self.chat_id,
                            clean,
                            parse_mode="",
                            reply_to_message_id=self.user_message_id,
                            link_preview_options={"is_disabled": True}
                        )
                        if fallback_res.get("ok"):
                            self.current_message_id = fallback_res["result"]["message_id"]
            elif final_text != self.last_rendered_text:
                self.last_rendered_text = final_text
                res = await self.tg.edit_message_text(
                    self.chat_id,
                    self.current_message_id,
                    final_text,
                    parse_mode="HTML"
                )
                if isinstance(res, dict) and not res.get("ok"):
                    err_desc = res.get("description", "")
                    if "can't parse entities" in err_desc or "entity" in err_desc:
                        clean = re.sub(r"<[^>]+>", "", final_text)
                        await self.tg.edit_message_text(
                            self.chat_id,
                            self.current_message_id,
                            clean,
                            parse_mode=""
                        )

            # Reactions & Unpin (Hermes parity)
            if self.user_message_id:
                emoji = "👍" if success else "👎"
                asyncio.create_task(self.tg.set_message_reaction(self.chat_id, self.user_message_id, emoji))
                asyncio.create_task(self.tg.unpin_chat_message(self.chat_id, self.user_message_id))


class OpenCodeTelegramBridge:
    def __init__(self):
        self.tg = TelegramAPI(config.TELEGRAM_BOT_TOKEN, proxy=config.TELEGRAM_PROXY)
        self.opencode = OpenCodeClient(
            base_url=config.OPENCODE_SERVER_URL,
            auto_start=config.OPENCODE_AUTO_START,
            command=config.OPENCODE_COMMAND,
            workspace_dir=config.OPENCODE_WORKSPACE_DIR,
            password=config.OPENCODE_SERVER_PASSWORD
        )
        self.user_sessions: Dict[int, str] = {}
        self.session_to_user: Dict[str, int] = {}
        self.active_streams: Dict[str, StreamSession] = {}
        self.typing_tasks: Dict[str, asyncio.Task] = {}
        self.pending_permissions: Dict[str, Dict[str, Any]] = {}
        self.pending_questions: Dict[str, Dict[str, Any]] = {}
        self.part_types: Dict[str, str] = {}
        self.message_roles: Dict[str, str] = {}
        self.user_models: Dict[int, Dict[str, str]] = {}
        self.user_agents: Dict[int, str] = {}
        self.user_workspace_dir: Dict[int, str] = {}
        self.user_default_channels: Dict[str, str] = load_user_channels()
        try:
            self.bot_id: Optional[int] = int(config.TELEGRAM_BOT_TOKEN.split(":")[0]) if ":" in config.TELEGRAM_BOT_TOKEN else None
        except Exception:
            self.bot_id = None
        self.failsafe_poll_task: Optional[asyncio.Task] = None
        self.running = False

    async def start(self):
        if not config.TELEGRAM_BOT_TOKEN:
            logger.error("TELEGRAM_BOT_TOKEN is not set! Please configure it in .env")
            return

        bot_info = await self.tg.get_me()
        if not bot_info.get("ok"):
            logger.error("Failed to connect to Telegram Bot: %s", bot_info.get("description"))
            return

        self.bot_id = bot_info["result"].get("id")
        bot_name = bot_info["result"].get("username", "UnknownBot")
        logger.info("🤖 Telegram Bot connected: @%s (ID: %s)", bot_name, self.bot_id)

        # Set status indicator to Online (Hermes feature)
        try:
            await self.tg.set_my_short_description("🟢 آنلاین (Online) | دستیار هوشمند OpenCode")
        except Exception as e:
            logger.debug("Failed to set online status: %s", e)

        # Register official Bot commands menu in Telegram
        await self._register_bot_commands()

        # Check and initialize OpenCode
        logger.info("Initializing OpenCode connection...")
        server_ok = await self.opencode.ensure_server_running()
        if not server_ok:
            logger.warning("Could not connect or start OpenCode server at %s. Will retry when commands arrive.", config.OPENCODE_SERVER_URL)
        else:
            logger.info("OpenCode server is ready!")

        # Register event callback
        self.opencode.add_event_listener(self.handle_opencode_event)
        await self.opencode.start_event_stream()

        self.running = True
        # Start background failsafe watcher for permissions and questions
        self.failsafe_poll_task = asyncio.create_task(self._failsafe_poll_loop())

        logger.info("Starting Telegram polling loop...")
        await self.poll_updates()

    async def _register_bot_commands(self):
        """Register command menu in Telegram."""
        commands = [
            {"command": "whoami", "description": "شناسنامه شما، هویت و سطح دسترسی"},
            {"command": "memory", "description": "مدیریت حافظه پایدار (Hermes & OpenCode)"},
            {"command": "remember", "description": "افزودن نکته به حافظه دائم (/remember <متن>)"},
            {"command": "soul", "description": "مشاهده هویت و مرام‌نامه دستیار (SOUL.md)"},
            {"command": "new", "description": "سشن تازه و پاکسازی تاریخچه مکالمه"},
            {"command": "model", "description": "انتخاب و سوییچ مدل هوش مصنوعی"},
            {"command": "agent", "description": "تغییر ایجنت (Build, Plan, Explore, General)"},
            {"command": "git", "description": "وضعیت فایل‌های تغییر یافته (Git Status)"},
            {"command": "diff", "description": "مشاهده کامل خطوط تغییر یافته (Diff)"},
            {"command": "todo", "description": "چک‌لیست برنامه‌ها و تسک‌های دستیار"},
            {"command": "share", "description": "اشتراک‌گذاری و دریافت لینک وب سشن"},
            {"command": "fork", "description": "انشعاب و کپی از سشن فعلی"},
            {"command": "revert", "description": "بازگردانی آخرین تغییر کد"},
            {"command": "unrevert", "description": "اعمال مجدد تغییرات بازگردانده شده"},
            {"command": "usage", "description": "آمار دقیق توکن‌ها و هزینه‌ها"},
            {"command": "sh", "description": "اجرای مستقیم دستور در ترمینال سیستم"},
            {"command": "find", "description": "جستجوی فایل در محیط پروژه"},
            {"command": "symbol", "description": "جستجوی توابع و کلاس‌ها در کد (LSP)"},
            {"command": "cd", "description": "تغییر پوشه کاری ایجنت به مسیر دیگر"},
            {"command": "pwd", "description": "مشاهده مسیر کاری فعلی و فایل‌های آن"},
            {"command": "post", "description": "ارسال مستقیم پست به کانال‌های تلگرام"},
            {"command": "set_channel", "description": "تنظیم کانال پیش‌فرض جهت انتشار"},
            {"command": "channels", "description": "مشاهده کانال‌های تلگرام متصل"},
            {"command": "cat", "description": "مشاهده متن فایل در چت تلگرام"},
            {"command": "download", "description": "دانلود فایل از سیستم در تلگرام"},
            {"command": "skills", "description": "لیست مهارت‌های فعال OpenCode"},
            {"command": "status", "description": "بررسی وضعیت سرور و سشن جاری"},
            {"command": "stop", "description": "لغو و توقف فوری پردازش جاری"},
            {"command": "help", "description": "راهنمای کامل امکانات و دکمه‌ها"},
        ]
        try:
            await self.tg.set_my_commands(commands)
            # Group chats scope with ephemeral commands (Bot API 10.2+)
            group_commands = [
                {"command": "status", "description": "بررسی وضعیت سرور و سشن جاری", "is_ephemeral": True},
                {"command": "stop", "description": "لغو و توقف فوری پردازش جاری", "is_ephemeral": True},
                {"command": "help", "description": "راهنمای کامل امکانات و دکمه‌ها", "is_ephemeral": True},
                {"command": "pwd", "description": "مشاهده مسیر کاری فعلی و فایل‌های آن", "is_ephemeral": True},
                {"command": "usage", "description": "آمار دقیق توکن‌ها و هزینه‌ها", "is_ephemeral": True},
                {"command": "model", "description": "مشاهده مدل فعال هوش مصنوعی", "is_ephemeral": True},
            ]
            await self.tg.set_my_commands(group_commands, scope={"type": "all_group_chats"})
            logger.info("Successfully registered Telegram bot command menus (private & group scopes).")
        except Exception as e:
            logger.warning("Failed to register Telegram bot commands: %s", e)

    async def _failsafe_poll_loop(self):
        """Polls OpenCode /permission and /question periodically as failsafe."""
        while self.running:
            try:
                # 1. Permissions
                pending_perms = await self.opencode.get_pending_permissions()
                for perm in pending_perms:
                    await self._dispatch_permission(perm)

                # 2. Questions
                pending_ques = await self.opencode.get_pending_questions()
                for que in pending_ques:
                    await self._dispatch_question(que)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Error in failsafe poller: %s", e)
            await asyncio.sleep(1.5)

    async def _dispatch_permission(self, props: Dict[str, Any]):
        perm_id = props.get("id") or props.get("permissionID")
        if not perm_id or perm_id in self.pending_permissions:
            return

        session_id = props.get("sessionID")
        user_id = self.session_to_user.get(session_id)
        if not user_id:
            if config.TELEGRAM_ALLOWED_USERS:
                user_id = next(iter(config.TELEGRAM_ALLOWED_USERS))
            else:
                return

        self.pending_permissions[perm_id] = {
            "session_id": session_id,
            "user_id": user_id,
            "props": props
        }

        p_type = props.get("permission") or props.get("type", "دسترسی")
        title = props.get("title", "")
        patterns = props.get("patterns") or props.get("pattern", "")
        if isinstance(patterns, list):
            patterns = ", ".join(patterns)
        metadata = props.get("metadata", {})
        filepath = metadata.get("filepath", "")

        logger.info("Dispatching permission prompt to user %s: ID=%s, Type=%s", user_id, perm_id, p_type)

        inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ تایید یک‌باره (Once)", "callback_data": f"perm:once:{perm_id}", "style": "success"},
                    {"text": "🔄 همیشه مجاز (Always)", "callback_data": f"perm:always:{perm_id}", "style": "primary"},
                ],
                [
                    {"text": "❌ رد درخواست (Reject)", "callback_data": f"perm:reject:{perm_id}", "style": "danger"}
                ]
            ]
        }

        title_desc = f"\n📋 *عملیات:* `{title}`" if title else ""
        path_desc = f"\n📁 *مسیر:* `{filepath}`" if filepath else ""
        pat_desc = f"\n🔍 *الگو/دستور:* `{patterns}`" if patterns else ""

        card_text = (
            "⚠️ *درخواست مجوز دسترسی (Permission Request)*\n\n"
            f"⚙️ *نوع دسترسی:* `{p_type}`"
            f"{title_desc}"
            f"{path_desc}"
            f"{pat_desc}\n\n"
            "آیا اجازه می‌دهید OpenCode این عملیات را اجرا کند؟"
        )

        msg = await self.tg.send_message(
            chat_id=user_id,
            text=card_text,
            parse_mode="Markdown",
            reply_markup=inline_keyboard
        )
        if msg.get("ok"):
            self.pending_permissions[perm_id]["msg_id"] = msg["result"]["message_id"]

    async def _dispatch_question(self, props: Dict[str, Any]):
        """Renders interactive multiple-choice question from OpenCode to Telegram."""
        que_id = props.get("id") or props.get("requestID")
        if not que_id or que_id in self.pending_questions:
            return

        session_id = props.get("sessionID")
        user_id = self.session_to_user.get(session_id)
        if not user_id:
            if config.TELEGRAM_ALLOWED_USERS:
                user_id = next(iter(config.TELEGRAM_ALLOWED_USERS))
            else:
                return

        questions = props.get("questions", [])
        if not questions:
            return

        self.pending_questions[que_id] = {
            "session_id": session_id,
            "user_id": user_id,
            "props": props
        }

        q = questions[0]
        q_text = q.get("question", "یکی از گزینه‌های زیر را انتخاب نمایید:")
        header = q.get("header", "سوال از کاربر")
        options = q.get("options", [])

        inline_buttons = []
        for idx, opt in enumerate(options):
            lbl = opt.get("label", f"گزینه {idx + 1}")
            desc = opt.get("description", "")
            btn_text = f"🔘 {lbl}"
            if desc:
                btn_text += f" ({desc[:25]})"
            inline_buttons.append([{"text": btn_text, "callback_data": f"que:{que_id}:{idx}", "style": "primary"}])

        inline_buttons.append([{"text": "❌ رد سوال (Reject)", "callback_data": f"que:reject:{que_id}", "style": "danger"}])

        card_text = (
            f"❓ *{header}*\n\n"
            f"{q_text}\n\n"
            "جهت پاسخ روی گزینه مورد نظر خود کلیک کنید:"
        )

        msg = await self.tg.send_message(
            chat_id=user_id,
            text=card_text,
            parse_mode="Markdown",
            reply_markup={"inline_keyboard": inline_buttons}
        )
        if msg.get("ok"):
            self.pending_questions[que_id]["msg_id"] = msg["result"]["message_id"]

    async def handle_opencode_event(self, event: Dict[str, Any]):
        """Callback for all events arriving from OpenCode SSE."""
        event_type = event.get("type", "")
        props = event.get("properties", {})

        # Track message roles (assistant vs user)
        if event_type == "message.updated":
            info = props.get("info", {})
            msg_id = info.get("id")
            role = info.get("role")
            if msg_id and role:
                self.message_roles[msg_id] = role

        # Track part types (reasoning vs text vs tool)
        elif event_type == "message.part.updated":
            session_id = props.get("sessionID")
            part = props.get("part", {})
            part_id = part.get("id")
            part_type = part.get("type")
            msg_id = part.get("messageID")

            if part_id and part_type:
                self.part_types[part_id] = part_type

            # Reasoning / thinking status tracking
            if part_type in ("reasoning", "thought", "thinking") and session_id in self.active_streams:
                stream = self.active_streams[session_id]
                stream.status_note = "thinking / analyzing..."
                part_text = part.get("text", "")
                if part_text:
                    stream.append_reasoning(part_text)

            # Step start (Hermes iteration increment)
            elif part_type == "step-start" and session_id in self.active_streams:
                stream = self.active_streams[session_id]
                await stream.on_step_start()

            # Tool execution (Hermes-style live activity: terminal, Reading, Writing, etc.)
            elif part_type == "tool" and session_id in self.active_streams:
                stream = self.active_streams[session_id]
                tool_name = part.get("tool", "")
                call_id = part.get("callID") or part_id or f"tool_{len(stream.activity_entries)}"
                state = part.get("state", {})
                input_data = state.get("input", {})
                title = state.get("title", "")
                status = state.get("status", "")
                await stream.record_tool(call_id, tool_name, input_data, title, status)

            # Ingest assistant text parts (catches text across turns and steps)
            elif part_type == "text" and self.message_roles.get(msg_id) == "assistant" and session_id in self.active_streams:
                text = part.get("text", "")
                stream = self.active_streams[session_id]
                if text:
                    already_ingested = stream.seen_text_parts.get(part_id, 0) if part_id else 0
                    if len(text) > already_ingested:
                        missing = text[already_ingested:]
                        if part_id:
                            stream.seen_text_parts[part_id] = len(text)
                        if session_id in self.typing_tasks:
                            self.typing_tasks[session_id].cancel()
                            self.typing_tasks.pop(session_id, None)
                        await stream.append_delta(missing)

        # 1. Delta stream update (live token streaming)
        elif event_type == "message.part.delta":
            session_id = props.get("sessionID")
            part_id = props.get("partID")
            field = props.get("field", "")
            delta = props.get("delta")

            # Track internal reasoning/Chain-of-Thought
            if field in ("reasoning", "thought", "thinking") or self.part_types.get(part_id) in ("reasoning", "thought", "thinking"):
                if part_id:
                    self.part_types[part_id] = "reasoning"
                if session_id and session_id in self.active_streams:
                    stream = self.active_streams[session_id]
                    if delta:
                        stream.append_reasoning(delta)
                    stream.status_note = "thinking / analyzing..."
                return

            if session_id and delta and session_id in self.active_streams:
                if session_id in self.typing_tasks:
                    self.typing_tasks[session_id].cancel()
                    self.typing_tasks.pop(session_id, None)

                stream = self.active_streams[session_id]
                if part_id:
                    stream.seen_text_parts[part_id] = stream.seen_text_parts.get(part_id, 0) + len(delta)
                await stream.append_delta(delta)

        # 2. Permission requested by OpenCode
        elif event_type in ("permission.asked", "permission.v2.asked", "permission.updated"):
            await self._dispatch_permission(props)

        # 3. Permission replied confirmation
        elif event_type in ("permission.replied", "permission.v2.replied"):
            perm_id = props.get("permissionID") or props.get("id") or props.get("requestID")
            if perm_id:
                self.pending_permissions.pop(perm_id, None)

        # 4. Question requested by OpenCode
        elif event_type in ("question.asked", "question.v2.asked"):
            await self._dispatch_question(props)

        # 5. Question replied or rejected
        elif event_type in ("question.replied", "question.v2.replied", "question.rejected", "question.v2.rejected"):
            que_id = props.get("requestID") or props.get("id")
            if que_id:
                self.pending_questions.pop(que_id, None)

        # 6. Session status & rate-limit check
        elif event_type == "session.status":
            session_id = props.get("sessionID") or props.get("id")
            st = props.get("status", {})
            st_type = st.get("type", "")
            if st_type == "idle":
                if session_id in self.typing_tasks:
                    self.typing_tasks[session_id].cancel()
                    self.typing_tasks.pop(session_id, None)
                if session_id in self.active_streams:
                    stream = self.active_streams.pop(session_id)
                    await stream.finish()
            elif st_type in ("retry", "rate_limited"):
                if session_id in self.active_streams:
                    await self.active_streams[session_id].on_rate_limit()

        # 7. Session turn finished / idle
        elif event_type == "session.idle":
            session_id = props.get("sessionID") or props.get("id")
            if session_id in self.typing_tasks:
                self.typing_tasks[session_id].cancel()
                self.typing_tasks.pop(session_id, None)

            if session_id in self.active_streams:
                stream = self.active_streams.pop(session_id)
                await stream.finish()

        # 8. Session error or rate-limiting
        elif event_type == "session.error":
            session_id = props.get("sessionID")
            err_raw = props.get("error", "")
            err_msg = str(err_raw)

            # Clean handling of normal user/system aborts
            if "MessageAbortedError" in err_msg or "Aborted" in err_msg:
                logger.info("Session %s aborted normally.", session_id)
                if session_id in self.typing_tasks:
                    self.typing_tasks[session_id].cancel()
                    self.typing_tasks.pop(session_id, None)
                if session_id in self.active_streams:
                    stream = self.active_streams.pop(session_id)
                    stream.cancel()
                    await stream.finish(success=False)
                return

            if session_id in self.active_streams:
                stream = self.active_streams[session_id]
                if "429" in err_msg or "rate" in err_msg.lower():
                    await stream.on_rate_limit()
                else:
                    if session_id in self.typing_tasks:
                        self.typing_tasks[session_id].cancel()
                        self.typing_tasks.pop(session_id, None)
                    self.active_streams.pop(session_id, None)
                    stream.cancel()
                    await stream.append_delta(f"\n\n❌ *خطا در اجرای دستور:*\n`{err_msg}`")
                    await stream.finish(success=False)

    def is_user_authorized(self, user_id: int) -> bool:
        if not config.TELEGRAM_ALLOWED_USERS:
            config.TELEGRAM_ALLOWED_USERS.add(user_id)
            logger.info("🎉 Auto-paired user %s as bot owner!", user_id)
            self._save_owner_to_env(user_id)
            return True
        return user_id in config.TELEGRAM_ALLOWED_USERS

    def is_agent_running(self, user_id: int) -> bool:
        """Checks if there is an active execution for this user."""
        sid = self.user_sessions.get(user_id)
        if not sid or sid not in self.active_streams:
            return False
        stream = self.active_streams[sid]
        if stream.is_done:
            self.active_streams.pop(sid, None)
            return False
        # Safety timeout: if running for > 15 minutes, allow user to proceed
        if time.time() - stream.start_time > 900:
            logger.warning("Session %s exceeded 15min running timeout, releasing lock.", sid)
            self.active_streams.pop(sid, None)
            return False
        return True

    async def abort_user_agent(self, chat_id: int, user_id: int):
        """Aborts active execution for user and releases lock."""
        sid = self.user_sessions.get(user_id)
        cur_dir = self.get_user_workspace(user_id)
        if not sid or sid not in self.active_streams:
            if sid:
                try:
                    await self.opencode.abort_session(sid, directory=cur_dir)
                except Exception:
                    pass
            await self.tg.send_message(
                chat_id,
                "ℹ️ در حال حاضر عملیات فعالی برای شما در حال اجرا نیست.",
                reply_markup=MAIN_REPLY_KEYBOARD
            )
            return

        logger.info("User %s requested abort for session %s (dir=%s)", user_id, sid, cur_dir)

        # 1. Abort on OpenCode server
        try:
            await self.opencode.abort_session(sid, directory=cur_dir)
        except Exception as e:
            logger.warning("Error aborting session on OpenCode server: %s", e)

        # 2. Cancel typing task
        if sid in self.typing_tasks:
            self.typing_tasks[sid].cancel()
            self.typing_tasks.pop(sid, None)

        # 3. Finish stream session with canceled notice
        if sid in self.active_streams:
            stream = self.active_streams.pop(sid)
            stream.cancel()
            await stream.append_delta("\n\n🛑 *عملیات با دستور توقف کاربر (/stop) لغو شد.*")
            await stream.finish(success=False)

        # 4. Confirmation message
        await self.tg.send_message(
            chat_id,
            "🛑 *عملیات جاری با موفقیت متوقف شد.*\nسشن آزاد شد و اکنون می‌توانید دستور یا پیام جدید خود را ارسال نمایید.",
            parse_mode="Markdown",
            reply_markup=MAIN_REPLY_KEYBOARD
        )

    def _save_owner_to_env(self, user_id: int):
        try:
            env_path = config.env_file
            if env_path.exists():
                content = env_path.read_text(encoding="utf-8")
                if "TELEGRAM_ALLOWED_USERS=" in content:
                    new_content = re.sub(
                        r"TELEGRAM_ALLOWED_USERS=.*",
                        f"TELEGRAM_ALLOWED_USERS={user_id}",
                        content
                    )
                else:
                    new_content = content + f"\nTELEGRAM_ALLOWED_USERS={user_id}\n"
                env_path.write_text(new_content, encoding="utf-8")
                logger.info("Saved TELEGRAM_ALLOWED_USERS=%s to .env", user_id)
        except Exception as e:
            logger.warning("Could not persist TELEGRAM_ALLOWED_USERS to .env: %s", e)

    def get_user_workspace(self, user_id: int) -> str:
        return self.user_workspace_dir.get(user_id, config.OPENCODE_WORKSPACE_DIR)

    def set_user_workspace(self, user_id: int, new_dir: str):
        self.user_workspace_dir[user_id] = new_dir

    async def switch_user_directory(self, chat_id: int, user_id: int, target_dir: str, announce: bool = True) -> str:
        old_sid = self.user_sessions.get(user_id)
        if old_sid:
            old_dir = self.get_user_workspace(user_id)
            if old_sid in self.active_streams:
                stream = self.active_streams.pop(old_sid)
                stream.cancel()
            try:
                await self.opencode.abort_session(old_sid, directory=old_dir)
            except Exception:
                pass
        self.set_user_workspace(user_id, target_dir)
        new_sess = await self.opencode.create_session(
            title=f"TG User {user_id} ({os.path.basename(target_dir)})",
            directory=target_dir
        )
        self.user_sessions[user_id] = new_sess["id"]
        self.session_to_user[new_sess["id"]] = user_id

        if announce:
            files_count = 0
            try:
                files_count = len(list(os.scandir(target_dir)))
            except Exception:
                pass

            msg = (
                "📂 *مسیر کاری ایجنت تغییر یافت:*\n"
                f"📍 `{target_dir}`\n\n"
                f"📊 تعداد آیتم‌ها: `{files_count}`\n"
                "⚡ _از این پس تمامی دستورات شل، جستجوها و عملیات ایجنت در این پوشه انجام خواهند شد._"
            )
            inline_buttons = [
                [
                    {"text": "📁 لیست فایل‌ها", "callback_data": f"dir:list:{target_dir}"},
                    {"text": "🖥 وضعیت گیت", "callback_data": "vcs:status"}
                ],
                [
                    {"text": "↩️ بازگشت به پروژه اصلی", "callback_data": f"cd:to:{config.OPENCODE_WORKSPACE_DIR}"}
                ]
            ]
            await self.tg.send_message(chat_id, msg, parse_mode="Markdown", reply_markup={"inline_keyboard": inline_buttons})
        return new_sess["id"]

    async def post_to_channel(self, chat_id: int, target_channel: str, text: str):
        """Sends a message to a Telegram channel and reports outcome."""
        target = target_channel.strip()
        if not (target.startswith("@") or target.startswith("-")):
            target = f"@{target}"

        logger.info("Attempting to post message to channel %s: %s", target, text[:40])
        res = await self.tg.send_message(target, text, parse_mode="Markdown")
        if res.get("ok"):
            preview = text[:150] + ("..." if len(text) > 150 else "")
            await self.tg.send_message(
                chat_id,
                f"📢 *پیام شما با موفقیت در کانال منتشر شد!*\n"
                f"🎯 کانال: `{target}`\n\n"
                f"📝 *پیش‌نمایش متن:*\n_{preview}_",
                parse_mode="Markdown"
            )
        else:
            err = res.get("description", "Unknown error")
            logger.warning("Failed to post to channel %s: %s", target, err)
            tip = ""
            if "not a member" in err.lower() or "not an administrator" in err.lower():
                tip = "\n\n💡 *راهنما:* مطمئن شوید ربات `@opencodealibot` را در کانال عضو کرده و به عنوان *مدیر (Admin)* با دسترسی ارسال پیام تنظیم کرده‌اید."
            elif "chat not found" in err.lower():
                tip = "\n\n💡 *راهنما:* کانال با این شناسه یافت نشد. برای کانال‌های عمومی از `@username` و برای کانال‌های خصوصی از شناسه عددی (مثل `-100...`) استفاده کنید."

            await self.tg.send_message(
                chat_id,
                f"❌ *خطا در ارسال پیام به کانال `{target}`:*\n`{err}`{tip}",
                parse_mode="Markdown"
            )

    async def get_or_create_session(self, user_id: int, directory: Optional[str] = None) -> str:
        cur_dir = directory or self.get_user_workspace(user_id)
        if user_id in self.user_sessions:
            return self.user_sessions[user_id]

        new_sess = await self.opencode.create_session(
            title=f"TG User {user_id} ({os.path.basename(cur_dir)})",
            directory=cur_dir
        )
        session_id = new_sess["id"]
        self.user_sessions[user_id] = session_id
        self.session_to_user[session_id] = user_id
        logger.info("Created new OpenCode session for user %s: %s (dir: %s)", user_id, session_id, cur_dir)
        return session_id

    def _check_env_token_hot_swap(self):
        try:
            env_path = config.env_file
            if not env_path.exists():
                return
            content = env_path.read_text(encoding="utf-8")
            match = re.search(r"^\s*TELEGRAM_BOT_TOKEN\s*=\s*([^\s#]+)", content, re.MULTILINE)
            if match:
                new_token = match.group(1).strip().strip('"').strip("'")
                if new_token and new_token != self.tg.token:
                    logger.info("🔑 New TELEGRAM_BOT_TOKEN detected in .env! Hot-swapping bot connection...")
                    old_tg = self.tg
                    self.tg = TelegramAPI(new_token, proxy=config.TELEGRAM_PROXY)
                    config.TELEGRAM_BOT_TOKEN = new_token
                    asyncio.create_task(old_tg.close())
                    asyncio.create_task(self._reinit_bot_info())
        except Exception as e:
            logger.debug("Error checking token hot-swap: %s", e)

    async def _reinit_bot_info(self):
        try:
            bot_info = await self.tg.get_me()
            if bot_info.get("ok"):
                self.bot_id = bot_info["result"].get("id")
                bot_name = bot_info["result"].get("username", "UnknownBot")
                logger.info("🤖 Telegram Bot hot-swapped to: @%s (ID: %s)", bot_name, self.bot_id)
                await self._register_bot_commands()
                await self.tg.set_my_short_description("🟢 آنلاین (Online) | دستیار هوشمند OpenCode")
                for uid in config.TELEGRAM_ALLOWED_USERS:
                    await self.tg.send_message(
                        uid,
                        f"🎉 *ارتباط تلگرام با توکن جدید با موفقیت فعال شد!*\nربات: @{bot_name}\nوضعیت: 🟢 کاملاً آنلاین و آماده کار",
                        parse_mode="Markdown",
                        reply_markup=MAIN_REPLY_KEYBOARD
                    )
        except Exception as e:
            logger.warning("Error re-initializing bot info: %s", e)

    async def poll_updates(self):
        offset = 0
        loop_count = 0
        while self.running:
            try:
                loop_count += 1
                if loop_count % 5 == 0:
                    self._check_env_token_hot_swap()

                updates_resp = await self.tg.get_updates(offset=offset, timeout=20)
                if not updates_resp.get("ok"):
                    await asyncio.sleep(2)
                    continue

                for update in updates_resp.get("result", []):
                    offset = max(offset, update["update_id"] + 1)
                    asyncio.create_task(self.dispatch_update(update))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in polling loop: %s", e)
                await asyncio.sleep(3)

    async def dispatch_update(self, update: Dict[str, Any]):
        try:
            if "message" in update:
                msg = update["message"]
                sender = msg.get("from", {})
                sender_id = sender.get("id")
                if (
                    sender.get("is_bot")
                    or (self.bot_id and sender_id == self.bot_id)
                    or "pinned_message" in msg
                ):
                    return
                await self.handle_message(msg)
            elif "callback_query" in update:
                cq = update["callback_query"]
                sender = cq.get("from", {})
                sender_id = sender.get("id")
                if sender.get("is_bot") or (self.bot_id and sender_id == self.bot_id):
                    return
                await self.handle_callback_query(cq)
            elif "stopped_message_generation" in update:
                smg = update["stopped_message_generation"]
                chat = smg.get("chat", {})
                chat_id = chat.get("id")
                target_user = None
                for uid, sid in list(self.user_sessions.items()):
                    if sid in self.active_streams and self.active_streams[sid].chat_id == chat_id:
                        target_user = uid
                        break
                if target_user:
                    logger.info("Native stop requested by user %s (chat %s)", target_user, chat_id)
                    await self.abort_user_agent(chat_id, target_user)
                return
            elif "channel_post" in update:
                # Bot posted to a channel, ignore echo update
                return
        except Exception as e:
            logger.exception("Error dispatching update: %s", e)

    async def handle_callback_query(self, query: Dict[str, Any]):
        query_id = query["id"]
        user_id = query["from"]["id"]
        data = query.get("data", "")
        message = query.get("message", {})
        chat_id = message.get("chat", {}).get("id", user_id)
        message_id = message.get("message_id")

        if not self.is_user_authorized(user_id):
            await self.tg.answer_callback_query(query_id, "⛔ دسترسی شما مجاز نیست.", show_alert=True)
            return

        # 0. Agent Execution Abort & Status Callbacks (Hermes parity)
        if data == "agent:abort":
            await self.tg.answer_callback_query(query_id, "⏹ در حال توقف عملیات...")
            await self.abort_user_agent(chat_id, user_id)
            return
        elif data == "agent:status_check":
            sid = self.user_sessions.get(user_id)
            stream = self.active_streams.get(sid)
            if stream:
                elapsed = int(time.time() - stream.start_time)
                elapsed_min = elapsed // 60
                elapsed_sec = elapsed % 60
                time_str = f"{elapsed_min}m {elapsed_sec}s" if elapsed_min > 0 else f"{elapsed_sec}s"
                await self.tg.answer_callback_query(
                    query_id,
                    f"⏳ در حال اجرا: {time_str} | گام {stream.current_step}/{stream.max_steps}\nوضعیت: {stream.status_note}",
                    show_alert=True
                )
            else:
                await self.tg.answer_callback_query(query_id, "✅ هیچ عملیاتی در حال حاضر در حال اجرا نیست.")
            return

        # Guard against modifying session/model while running
        if data.startswith(("model:set:", "agent:set:", "sess:switch:", "vcs:revert")):
            if self.is_agent_running(user_id):
                await self.tg.answer_callback_query(
                    query_id,
                    "⚠️ ایجنت در حال اجراست — لطفاً صبور باشید یا ابتدا /stop کنید.\n(Agent is running — wait or /stop first)",
                    show_alert=True
                )
                return

        # 1. Permission Approval Callbacks (perm:{action}:{permission_id})
        if data.startswith("perm:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                action, perm_id = parts[1], parts[2]
                perm_info = self.pending_permissions.get(perm_id, {})
                session_id = perm_info.get("session_id")
                await self.opencode.respond_permission(session_id, perm_id, action)

                status_emoji = "✅" if action in ("once", "always") else "❌"
                action_fa = "تایید یک‌باره" if action == "once" else ("تایید همیشگی" if action == "always" else "رد درخواست")
                toast = f"{status_emoji} {action_fa} ثبت شد."

                await self.tg.answer_callback_query(query_id, toast)

                current_text = message.get("text", "")
                updated_text = (
                    f"{current_text}\n\n"
                    f"➖➖➖➖➖➖➖➖➖➖\n"
                    f"📌 پاسخ شما: {status_emoji} {action_fa}"
                )
                btn_style = "success" if action in ("once", "always") else "danger"
                disabled_keyboard = {
                    "inline_keyboard": [[
                        {"text": f"{status_emoji} {action_fa} (ثبت شد)", "callback_data": "noop", "disabled": True, "style": btn_style}
                    ]]
                }
                await self.tg.edit_message_text(
                    chat_id,
                    message_id,
                    updated_text,
                    parse_mode=None,
                    reply_markup=disabled_keyboard
                )
                self.pending_permissions.pop(perm_id, None)

        # 2. Interactive Question Callbacks (que:{id}:{option_idx} or que:reject:{id})
        elif data.startswith("que:"):
            parts = data.split(":")
            if len(parts) >= 3:
                if parts[1] == "reject":
                    que_id = parts[2]
                    await self.opencode.reject_question(que_id)
                    self.pending_questions.pop(que_id, None)
                    await self.tg.answer_callback_query(query_id, "❌ سوال رد شد.")
                    disabled_keyboard = {
                        "inline_keyboard": [[
                            {"text": "❌ رد شده توسط کاربر", "callback_data": "noop", "disabled": True, "style": "danger"}
                        ]]
                    }
                    await self.tg.edit_message_text(
                        chat_id,
                        message_id,
                        f"{message.get('text', '')}\n\n➖➖➖➖➖➖➖➖➖➖\n❌ *سوال توسط کاربر رد شد.*",
                        parse_mode=None,
                        reply_markup=disabled_keyboard
                    )
                else:
                    que_id = parts[1]
                    try:
                        opt_idx = int(parts[2])
                    except ValueError:
                        return
                    q_data = self.pending_questions.get(que_id, {})
                    questions = q_data.get("props", {}).get("questions", [])
                    if questions and opt_idx < len(questions[0].get("options", [])):
                        selected_label = questions[0]["options"][opt_idx]["label"]
                        await self.opencode.reply_question(que_id, [[selected_label]])
                        self.pending_questions.pop(que_id, None)
                        await self.tg.answer_callback_query(query_id, f"✅ پاسخ انتخاب شد: {selected_label}")
                        disabled_keyboard = {
                            "inline_keyboard": [[
                                {"text": f"✅ {selected_label} (انتخاب شد)", "callback_data": "noop", "disabled": True, "style": "success"}
                            ]]
                        }
                        await self.tg.edit_message_text(
                            chat_id,
                            message_id,
                            f"{message.get('text', '')}\n\n➖➖➖➖➖➖➖➖➖➖\n✅ *پاسخ انتخابی:* {selected_label}",
                            parse_mode=None,
                            reply_markup=disabled_keyboard
                        )

        # 3. Model Selection Callback (model:set:{providerID}:{modelID})
        elif data.startswith("model:set:"):
            parts = data.split(":", 3)
            if len(parts) == 4:
                provider_id, model_id = parts[2], parts[3]
                self.user_models[user_id] = {"providerID": provider_id, "modelID": model_id}
                session_id = await self.get_or_create_session(user_id)
                await self.opencode.switch_session_model(session_id, provider_id, model_id)

                await self.tg.answer_callback_query(query_id, f"✅ مدل فعال شد: {model_id}")
                disabled_keyboard = {
                    "inline_keyboard": [[
                        {"text": f"✅ مدل فعال: {model_id[:25]}", "callback_data": "noop", "disabled": True, "style": "success"}
                    ]]
                }
                await self.tg.edit_message_text(
                    chat_id,
                    message_id,
                    f"🤖 *مدل هوش مصنوعی با موفقیت تنظیم شد:*\n\n"
                    f"• *سرویس‌دهنده:* `{provider_id}`\n"
                    f"• *شناسه مدل:* `{model_id}`\n\n"
                    f"از این پس دستورات شما با این مدل اجرا می‌شوند.",
                    parse_mode="Markdown",
                    reply_markup=disabled_keyboard
                )

        # 4. Agent Selection Callback (agent:set:{agent_name})
        elif data.startswith("agent:set:"):
            target_agent = data.split(":", 2)[2]
            self.user_agents[user_id] = target_agent
            await self.tg.answer_callback_query(query_id, f"🧠 ایجنت به {target_agent} تغییر یافت.")
            agent_fa = {
                "build": "Build (کدنویسی و ترمینال)",
                "plan": "Plan (طراحی و بدون ویرایش)",
                "explore": "Explore (کاوش سریع در کد)",
                "general": "General (وظایف چندمرحله‌ای)"
            }
            title = agent_fa.get(target_agent, target_agent)
            disabled_keyboard = {
                "inline_keyboard": [[
                    {"text": f"✅ ایجنت فعال: {title}", "callback_data": "noop", "disabled": True, "style": "success"}
                ]]
            }
            await self.tg.edit_message_text(
                chat_id,
                message_id,
                f"✅ *حالت ایجنت فعال به `{title}` تغییر یافت.*\nاز این پس درخواست‌های شما با این ایجنت پردازش می‌شوند.",
                parse_mode="Markdown",
                reply_markup=disabled_keyboard
            )

        # 5. Session Switching Callback (sess:switch:{session_id})
        elif data.startswith("sess:switch:"):
            target_sid = data.split(":", 2)[2]
            self.user_sessions[user_id] = target_sid
            self.session_to_user[target_sid] = user_id
            await self.tg.answer_callback_query(query_id, "✅ به سشن انتخابی منتقل شدید.")
            disabled_keyboard = {
                "inline_keyboard": [[
                    {"text": f"✅ سشن فعال: {target_sid[:15]}", "callback_data": "noop", "disabled": True, "style": "success"}
                ]]
            }
            await self.tg.edit_message_text(
                chat_id,
                message_id,
                f"📂 *سشن فعال تغییر یافت:*\n`{target_sid}`\nمکالمات بعدی در این سشن ادامه می‌یابند.",
                parse_mode="Markdown",
                reply_markup=disabled_keyboard
            )

        # 6. Session Unshare Callback (sess:unshare:{session_id})
        elif data.startswith("sess:unshare:"):
            sid = data.split(":", 2)[2]
            await self.opencode.unshare_session(sid)
            await self.tg.answer_callback_query(query_id, "🔒 سشن خصوصی شد.")
            await self.tg.edit_message_text(
                chat_id,
                message_id,
                "🔒 *اشتراک سشن با موفقیت لغو شد.* این مکالمه دیگر به صورت عمومی در دسترس نیست.",
                parse_mode="Markdown",
                reply_markup=None
            )

        # 7. Git Diff View Callback (vcs:diff)
        elif data == "vcs:diff":
            await self.tg.answer_callback_query(query_id, "در حال تولید Diff...")
            await self.handle_command(chat_id, user_id, "/diff", None)

        # 8. Revert Callback (vcs:revert)
        elif data == "vcs:revert":
            await self.tg.answer_callback_query(query_id, "در حال بازگردانی آخرین تغییر...")
            await self.handle_command(chat_id, user_id, "/revert", None)

        # 9. File View Callback (file:view:{path})
        elif data.startswith("file:view:"):
            file_path = data.split(":", 2)[2]
            cur_dir = self.get_user_workspace(user_id)
            await self.tg.answer_callback_query(query_id, "در حال بارگذاری فایل...")
            content = await self.opencode.get_file_content(file_path, directory=cur_dir)
            if content is not None:
                preview = content[:3500]
                if len(content) > 3500:
                    preview += "\n... (ادامه فایل به دلیل محدودیت تلگرام کوتاه شد)"
                await self.tg.send_message(
                    chat_id,
                    f"📄 *محتوای فایل:* `{file_path}`\n\n```\n{preview}\n```",
                    parse_mode="Markdown"
                )
            else:
                await self.tg.send_message(chat_id, f"❌ فایل یافت نشد: `{file_path}`", parse_mode="Markdown")

        # 10. File Download Callback (file:dl:{path})
        elif data.startswith("file:dl:"):
            file_path = data.split(":", 2)[2]
            cur_dir = self.get_user_workspace(user_id)
            await self.tg.answer_callback_query(query_id, "در حال ارسال فایل...")
            full_path = file_path if os.path.isabs(file_path) else os.path.join(cur_dir, file_path)
            if not os.path.exists(full_path):
                exp = os.path.expanduser(file_path)
                if os.path.exists(exp):
                    full_path = exp
            if os.path.exists(full_path) and os.path.isfile(full_path):
                await self.tg.send_document(chat_id, full_path, caption=f"📁 `{os.path.basename(full_path)}`")
            else:
                await self.tg.send_message(chat_id, f"❌ فایل در مسیر یافت نشد: `{full_path}`", parse_mode="Markdown")

        # 10.1 CD to Directory Callback (cd:to:{path})
        elif data.startswith("cd:to:"):
            target_dir = data.split(":", 2)[2]
            if os.path.isdir(target_dir):
                await self.tg.answer_callback_query(query_id, f"مسیر تغییر یافت به {os.path.basename(target_dir)}")
                await self.switch_user_directory(chat_id, user_id, target_dir, announce=True)
            else:
                await self.tg.answer_callback_query(query_id, "❌ پوشه یافت نشد.", show_alert=True)

        # 10.2 Directory List Callback (dir:list:{path})
        elif data.startswith("dir:list:"):
            target_dir = data.split(":", 2)[2]
            await self.tg.answer_callback_query(query_id, "در حال بررسی محتویات...")
            try:
                entries = list(os.scandir(target_dir))[:15]
                lines = [f"📁 *محتویات پوشه `{os.path.basename(target_dir)}`:*\n"]
                inline_btns = []
                for e in entries:
                    icon = "📁" if e.is_dir() else "📄"
                    lines.append(f"{icon} `{e.name}`")
                    if e.is_file():
                        inline_btns.append([{"text": f"⬇️ دانلود {e.name[:20]}", "callback_data": f"file:dl:{e.path}"}])
                    elif e.is_dir():
                        inline_btns.append([{"text": f"📂 ورود به {e.name[:20]}", "callback_data": f"cd:to:{e.path}"}])
                await self.tg.send_message(chat_id, "\n".join(lines), parse_mode="Markdown", reply_markup={"inline_keyboard": inline_btns[:6]})
            except Exception as e:
                await self.tg.send_message(chat_id, f"❌ خطا در خواندن پوشه: `{e}`", parse_mode="Markdown")

        # 10.3 Quick CD Prompt
        elif data == "cmd:cd":
            await self.tg.answer_callback_query(query_id, "تغییر مسیر کاری")
            await self.handle_command(chat_id, user_id, "/cd", None)

        # 11. Memory Callbacks (mem:*)
        elif data.startswith("mem:"):
            action = data.split(":", 1)[1]
            if action == "user":
                prof = memory_manager.get_user_profile()
                await self.tg.answer_callback_query(query_id, "مشخصات کاربر بارگذاری شد.")
                await self.tg.send_message(
                    chat_id,
                    f"👤 *پروفایل کاربر (`USER.md`):*\n\n{prof}",
                    parse_mode="Markdown"
                )
            elif action == "sys":
                sys_mem = memory_manager.get_system_memory()
                await self.tg.answer_callback_query(query_id, "حافظه سیستم بارگذاری شد.")
                preview = sys_mem[:3500]
                await self.tg.send_message(
                    chat_id,
                    f"🧠 *دانش و حافظه سیستم (`MEMORY.md`):*\n\n{preview}",
                    parse_mode="Markdown"
                )
            elif action == "soul":
                soul = memory_manager.get_soul()
                await self.tg.answer_callback_query(query_id, "مرام‌نامه بارگذاری شد.")
                await self.tg.send_message(
                    chat_id,
                    f"🎭 *هویت و مرام‌نامه دستیار (`SOUL.md`):*\n\n{soul}",
                    parse_mode="Markdown"
                )
            elif action == "add":
                await self.tg.answer_callback_query(query_id, "افزودن به حافظه")
                await self.tg.send_message(
                    chat_id,
                    "📝 *افزودن نکته به حافظه دائم:*\nجهت ثبت هر نکته کافیست از دستور `/remember` استفاده کنید:\nمثال:\n`/remember پورت تست سرور 8080 است`",
                    parse_mode="Markdown"
                )

    async def handle_message(self, message: Dict[str, Any]):
        user = message.get("from", {})
        user_id = user.get("id")
        chat_id = message.get("chat", {}).get("id", user_id)
        text = message.get("text", "").strip()
        msg_id = message.get("message_id")

        if (
            not user_id
            or user.get("is_bot")
            or (self.bot_id and user_id == self.bot_id)
            or "pinned_message" in message
        ):
            return

        if not self.is_user_authorized(user_id):
            if chat_id in config.TELEGRAM_ALLOWED_USERS:
                logger.warning(
                    "Ignored unauthorized check for internal message in authorized chat %s (from user %s)",
                    chat_id,
                    user_id,
                )
                return
            logger.warning("Unauthorized access attempt by user %s (@%s)", user_id, user.get("username"))
            await self.tg.send_message(
                chat_id,
                f"⛔ *دسترسی غیرمجاز / Unauthorized*\n\n"
                f"شناسه عددی تلگرام شما: `{user_id}`\n\n"
                f"شما اجازه ارسال دستور به این بات را ندارید.",
                parse_mode="Markdown"
            )
            return

        # Concurrency Lock (Hermes parity): Do not allow new instructions until turn is finished
        if self.is_agent_running(user_id):
            text_lower = text.strip()
            # Allow stopping/aborting
            if text_lower in ("/stop", "/abort", "stop", "abort", "توقف", "لغو", "کنسل", "🛑 توقف (/stop)", "🛑 توقف"):
                await self.abort_user_agent(chat_id, user_id)
                return

            if text_lower in ("/status", "وضعیت"):
                sid = self.user_sessions.get(user_id)
                stream = self.active_streams.get(sid)
                if stream:
                    elapsed = int(time.time() - stream.start_time)
                    await self.tg.send_message(
                        chat_id,
                        f"⏳ *تسک جاری در حال اجرا است*\n\n"
                        f"• مدت زمان: `{elapsed} ثانیه`\n"
                        f"• گام: `{stream.current_step}/{stream.max_steps}`\n"
                        f"• وضعیت: `{stream.status_note}`\n\n"
                        f"جهت لغو می‌توانید از دکمه زیر یا دستور `/stop` استفاده کنید.",
                        parse_mode="Markdown",
                        reply_markup={
                            "inline_keyboard": [[{"text": "⏹ توقف فوری عملیات (/stop)", "callback_data": "agent:abort", "style": "danger"}]]
                        },
                        reply_to_message_id=msg_id
                    )
                return

            target_action = "ارسال دستور جدید"
            if text.startswith("/model") or text_lower in ("🤖 مدل‌ها", "🤖 انتخاب مدل"):
                target_action = "تغییر مدل (switch models)"
            elif text.startswith("/agent") or text_lower in ("🧠 ایجنت", "🧠 انتخاب ایجنت"):
                target_action = "تغییر ایجنت (switch agents)"
            elif text.startswith("/cd") or text_lower in ("📂 مسیر کاری (/cd)", "📂 مسیر کاری"):
                target_action = "تغییر مسیر کاری (change directory)"
            elif text.startswith("/new") or text_lower in ("➕ سشن جدید",):
                target_action = "ایجاد سشن جدید (create new session)"
            elif text.startswith("/sh") or text_lower in ("⚡ ترمینال",):
                target_action = "اجرای دستور ترمینال (run shell)"

            stop_markup = {
                "inline_keyboard": [
                    [
                        {"text": "⏹ توقف فوری عملیات (/stop)", "callback_data": "agent:abort", "style": "danger"},
                        {"text": "⏳ بررسی وضعیت (/status)", "callback_data": "agent:status_check", "style": "primary"}
                    ]
                ]
            }

            await self.tg.send_message(
                chat_id,
                f"⚠️ *ایجنت در حال اجراست / Agent is running*\n\n"
                f"تسک قبلی هنوز به پایان نرسیده است. تا قبل از اتمام پاسخ نمی‌توانید {target_action} را انجام دهید.\n\n"
                f"لطفاً صبور باشید تا پاسخ کامل شود، یا جهت لغو فوری از دکمه زیر یا ارسال `/stop` استفاده کنید:\n\n"
                f"_Agent is running — wait or /stop first._",
                parse_mode="Markdown",
                reply_markup=stop_markup,
                reply_to_message_id=msg_id
            )
            return

        # Handle File / Document / Photo / Voice / Audio Upload
        if "document" in message or "photo" in message or "voice" in message or "audio" in message or "video" in message:
            await self.handle_file_upload(message)
            return

        if not text:
            return

        # Persistent Keyboard button mapping
        text_lower = text.strip()
        if text_lower in ("🛑 توقف (/stop)", "🛑 توقف", "/stop", "/abort"):
            await self.abort_user_agent(chat_id, user_id)
            return
        elif text_lower in ("➕ سشن جدید", "/new"):
            await self.handle_command(chat_id, user_id, "/new", msg_id)
            return
        elif text_lower in ("🤖 مدل‌ها", "🤖 انتخاب مدل", "/model"):
            await self.handle_command(chat_id, user_id, "/model", msg_id)
            return
        elif text_lower in ("🧠 ایجنت", "🧠 انتخاب ایجنت", "/agent"):
            await self.handle_command(chat_id, user_id, "/agent", msg_id)
            return
        elif text_lower in ("👤 کیستی؟ (/whoami)", "👤 کیستی؟", "/whoami"):
            await self.handle_command(chat_id, user_id, "/whoami", msg_id)
            return
        elif text_lower in ("💾 حافظه (/memory)", "💾 حافظه", "/memory"):
            await self.handle_command(chat_id, user_id, "/memory", msg_id)
            return
        elif text_lower in ("📝 تغییرات گیت", "/git"):
            await self.handle_command(chat_id, user_id, "/git", msg_id)
            return
        elif text_lower in ("📋 تسک‌ها", "📋 چک‌لیست تسک‌ها", "/todo"):
            await self.handle_command(chat_id, user_id, "/todo", msg_id)
            return
        elif text_lower in ("🌐 اشتراک سشن", "🌐 اشتراک‌گذاری", "/share"):
            await self.handle_command(chat_id, user_id, "/share", msg_id)
            return
        elif text_lower in ("📊 آمار مصرف", "/usage"):
            await self.handle_command(chat_id, user_id, "/usage", msg_id)
            return
        elif text_lower in ("📂 مسیر کاری (/cd)", "📂 مسیر کاری", "/cd"):
            await self.handle_command(chat_id, user_id, "/cd", msg_id)
            return
        elif text_lower in ("📢 ارسال به کانال (/post)", "📢 ارسال به کانال", "/post"):
            await self.handle_command(chat_id, user_id, "/post", msg_id)
            return
        elif text_lower in ("⚡ ترمینال", "/sh"):
            await self.tg.send_message(
                chat_id,
                "💻 *اجرای دستور شل در سیستم:*\nبرای اجرای هر دستوری کافیست از پیشوند `/sh` استفاده کنید.\nمثال:\n`/sh ls -la`\n`/sh df -h`\n`/sh git status`",
                parse_mode="Markdown",
                reply_markup=MAIN_REPLY_KEYBOARD
            )
            return
        elif text_lower in ("❓ راهنما", "/help"):
            await self.handle_command(chat_id, user_id, "/help", msg_id)
            return

        # Handle Slash Commands
        if text.startswith("/"):
            await self.handle_command(chat_id, user_id, text, msg_id)
            return

        # Check Natural Language Intents
        cur_dir = self.get_user_workspace(user_id)

        # 1. Natural Language Working Directory Switch (e.g. "برو به پوشه دسکتاپ")
        dir_intent = detect_directory_intent(text, cur_dir)
        if dir_intent:
            target_dir, remainder_task = dir_intent
            if os.path.isdir(target_dir):
                logger.info("Natural language directory switch: %s -> %s (remainder: %s)", text, target_dir, remainder_task)
                await self.switch_user_directory(chat_id, user_id, target_dir, announce=(remainder_task is None))
                if remainder_task:
                    await self.tg.send_message(
                        chat_id,
                        f"📂 _مسیر کاری تغییر یافت به: `{target_dir}`_\n🚀 _در حال انجام تسک بعدی: «{remainder_task}»..._",
                        parse_mode="Markdown"
                    )
                    await self.process_user_prompt(chat_id, user_id, remainder_task, msg_id)
                return

        # 2. Natural Language File Download Request (e.g. "فایل کانفیگ رو میخوام برام بفرستی")
        file_query = detect_file_download_intent(text)
        if file_query:
            logger.info("Natural language file request: query='%s'", file_query)
            cand = find_candidate_file(file_query, cur_dir)
            if cand and os.path.exists(cand) and os.path.isfile(cand):
                await self.tg.send_chat_action(chat_id, "upload_document")
                ext = os.path.splitext(cand)[1].lower()
                caption = f"📎 فایل درخواستی شما: `{os.path.basename(cand)}`"
                if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                    await self.tg.send_photo(chat_id, cand, caption=caption, reply_to_message_id=msg_id)
                else:
                    await self.tg.send_document(chat_id, cand, caption=caption, reply_to_message_id=msg_id)
                await self.tg.send_message(
                    chat_id,
                    f"✅ فایل `{os.path.basename(cand)}` با موفقیت برای شما ارسال شد.",
                    parse_mode="Markdown"
                )
                return

        # 3. Natural Language Channel Post Request (e.g. "ارسال به کانال @Sasm110313: متن")
        channel_post_intent = detect_channel_post_intent(text)
        if channel_post_intent:
            target_channel, post_body = channel_post_intent
            logger.info("Natural language channel post: target=%s", target_channel)
            await self.post_to_channel(chat_id, target_channel, post_body)
            return

        # React to prompt message (Bot API 7.0+)
        asyncio.create_task(self.tg.set_message_reaction(chat_id, msg_id, "👀"))

        # Pin message during agent turn (Hermes visual activity indicator)
        asyncio.create_task(self.tg.pin_chat_message(chat_id, msg_id, disable_notification=True))

        # Normal prompt to OpenCode
        await self.process_user_prompt(chat_id, user_id, text, msg_id)

    async def handle_file_upload(self, message: Dict[str, Any]):
        """Handles document, photo, or voice uploads from user."""
        chat_id = message.get("chat", {}).get("id")
        user_id = message.get("from", {}).get("id")
        caption = message.get("caption", "").strip()
        msg_id = message.get("message_id")

        if self.is_agent_running(user_id):
            await self.tg.send_message(
                chat_id,
                "⚠️ *ایجنت در حال حاضر مشغول اجرای دستور قبلی است.*\nلطفاً تا پایان عملیات صبر کنید یا با دکمه زیر یا `/stop` آن را متوقف کنید.\n\n_Agent is running — wait or /stop first._",
                parse_mode="Markdown",
                reply_to_message_id=msg_id,
                reply_markup={
                    "inline_keyboard": [[{"text": "⏹ توقف فوری عملیات (/stop)", "callback_data": "agent:abort", "style": "danger"}]]
                }
            )
            return

        file_id = None
        file_name = "upload.dat"
        is_voice = False

        if "document" in message:
            doc = message["document"]
            file_id = doc.get("file_id")
            file_name = doc.get("file_name", "document.dat")
        elif "photo" in message:
            photos = message["photo"]
            file_id = photos[-1].get("file_id")
            file_name = f"photo_{int(time.time())}.jpg"
        elif "voice" in message:
            voice = message["voice"]
            file_id = voice.get("file_id")
            file_name = f"voice_{int(time.time())}.ogg"
            is_voice = True
        elif "audio" in message:
            audio = message["audio"]
            file_id = audio.get("file_id")
            file_name = audio.get("file_name", f"audio_{int(time.time())}.mp3")
            is_voice = True
        elif "video" in message:
            vid = message["video"]
            file_id = vid.get("file_id")
            file_name = vid.get("file_name", f"video_{int(time.time())}.mp4")

        if not file_id:
            return

        await self.tg.send_chat_action(chat_id, "upload_document")
        file_info = await self.tg.get_file(file_id)
        if not file_info.get("ok"):
            await self.tg.send_message(chat_id, "❌ خطا در دریافت اطلاعات فایل از سرور تلگرام.")
            return

        file_path_on_tg = file_info["result"]["file_path"]
        cur_dir = self.get_user_workspace(user_id)
        uploads_dir = os.path.join(cur_dir, "uploads")
        dest_path = os.path.join(uploads_dir, file_name)

        success = await self.tg.download_file(file_path_on_tg, dest_path)
        if not success:
            await self.tg.send_message(chat_id, "❌ خطا در دانلود و ذخیره فایل.")
            return

        asyncio.create_task(self.tg.set_message_reaction(chat_id, msg_id, "👀"))
        asyncio.create_task(self.tg.pin_chat_message(chat_id, msg_id, disable_notification=True))

        if is_voice:
            # Voice Memo (Hermes audio handling)
            wav_path = dest_path.replace(".ogg", ".wav")
            try:
                os.system(f"ffmpeg -y -i '{dest_path}' '{wav_path}' -loglevel quiet")
            except Exception:
                pass
            prompt_text = (
                f"کاربر یک پیام صوتی (Voice Memo) در تلگرام ارسال کرده است.\n"
                f"فایل صوتی ذخیره شده: `{dest_path}`\n"
                f"نسخه تبدیل شده: `{wav_path}`\n"
                f"توضیحات/کپشن: {caption or 'لطفاً فایل صوتی را بررسی و به آن پاسخ دهید.'}"
            )
        else:
            prompt_text = (
                f"کاربر فایلی را در مسیر `{dest_path}` آپلود کرده است.\n"
                f"توضیحات/دستور کاربر: {caption or 'لطفاً این فایل را بررسی و تحلیل کن.'}"
            )

        await self.process_user_prompt(chat_id, user_id, prompt_text, msg_id)

    async def handle_command(self, chat_id: int, user_id: int, command_text: str, msg_id: Optional[int] = None):
        parts = command_text.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        # /whoami - Identity & Permissions Card
        if cmd == "/whoami":
            user_prof = memory_manager.get_user_profile()
            whoami_text = (
                "👤 *شناسنامه و هویت کاربر (بر اساس حافظه Hermes):*\n\n"
                f"• *نام و هویت:* سیدعلی سیدموسوی (علی)\n"
                f"• *نقش:* طلبه حوزه علمیه قم و برنامه‌نویس ارشد (Python, Django, Flask, Linux)\n"
                f"• *سیستم‌عامل:* Fedora 44 روی لپ‌تاپ (Wayland)\n"
                f"• *مرورگر ترجیحی:* Google Chrome\n"
                f"• *پروژه‌ها:* سامانه سنجاق، کانال تلگرام @Sasm110313، ربات‌های تلگرام و ایتا\n"
                f"• *سطح دسترسی:* 👑 مالک کل (Owner) — شناسه `{user_id}`\n"
                f"• *سبک ارتباط:* خودمونی، دوستانه، مستقیم، بدون حشو و تعارف\n\n"
                f"📝 *خلاصه فایل USER.md:*\n_{user_prof[:250]}..._"
            )
            inline_buttons = [
                [
                    {"text": "👤 مشخصات کامل (USER.md)", "callback_data": "mem:user"},
                    {"text": "🧠 حافظه سیستم (MEMORY.md)", "callback_data": "mem:sys"}
                ],
                [
                    {"text": "🎭 مرام‌نامه و لحن (SOUL.md)", "callback_data": "mem:soul"}
                ]
            ]
            keyboard = {"inline_keyboard": inline_buttons}
            await self.tg.send_message(chat_id, whoami_text, parse_mode="Markdown", reply_markup=keyboard)

        # /memory - Interactive Memory Center
        elif cmd == "/memory":
            mem_text = (
                "💾 *مرکز مدیریت حافظه پایدار (Hermes & OpenCode Memory)*\n\n"
                "دستیار شما دارای حافظه تفکیک‌شده و هوشمند است تا در زمان نیاز به کار آید و تمرکز شما را در کدنویسی به هم نریزد:\n\n"
                "• 👤 *USER.md:* هویت، علایق، سوابق و سلیقه برنامه‌نویسی شما\n"
                "• 🧠 *MEMORY.md:* نکات سیستم، ابزارهای محیطی و پروژه‌های گذشته\n"
                "• 🎭 *SOUL.md:* لحن، شخصیت و خطوط قرمز دستیار\n\n"
                "روی یکی از بخش‌های زیر کلیک کنید یا با `/remember` نکته تازه بنویسید:"
            )
            inline_buttons = [
                [
                    {"text": "👤 مشخصات کاربر", "callback_data": "mem:user"},
                    {"text": "🧠 یادداشت‌های سیستم", "callback_data": "mem:sys"}
                ],
                [
                    {"text": "🎭 هویت دستیار (SOUL)", "callback_data": "mem:soul"},
                    {"text": "➕ افزودن نکته تازه", "callback_data": "mem:add"}
                ]
            ]
            keyboard = {"inline_keyboard": inline_buttons}
            await self.tg.send_message(chat_id, mem_text, parse_mode="Markdown", reply_markup=keyboard)

        # /remember - Append fact to persistent memory
        elif cmd == "/remember":
            if not args:
                await self.tg.send_message(chat_id, "⚠️ نکته‌ای که می‌خواهید به خاطر بسپارد را وارد کنید.\nمثال: `/remember پورت تست سرور 8080 است`", parse_mode="Markdown")
                return

            ok = memory_manager.add_memory_note(args, target="memory")
            if ok:
                await self.tg.send_message(
                    chat_id,
                    f"✅ *نکته با موفقیت در حافظه پایدار ثبت شد:*\n`{args}`\nاین نکته از این پس در دسترس خواهد بود.",
                    parse_mode="Markdown"
                )
            else:
                await self.tg.send_message(chat_id, "❌ خطا در ثبت نکته در حافظه.")

        # /soul - Persona and tone
        elif cmd == "/soul":
            soul_text = memory_manager.get_soul()
            await self.tg.send_message(chat_id, f"🎭 *هویت و لحن دستیار (`SOUL.md`):*\n\n{soul_text}", parse_mode="Markdown")

        # /start and /help
        elif cmd in ("/start", "/help"):
            help_msg = (
                "🤖 *دستیار فوق‌پیشرفته OpenCode در تلگرام*\n\n"
                "این ربات با موتور **OpenCode** و حافظه پایدار **Hermes Agent** کار می‌کند:\n\n"
                "🧠 *هویت و حافظه:* \n"
                "• `/whoami` — شناسنامه کاربر و بررسی دسترسی‌ها\n"
                "• `/memory` — مشاهده و مدیریت حافظه پایدار کاربر و سیستم\n"
                "• `/remember <نکته>` — ثبت نکته جدید در حافظه سیستم\n"
                "• `/soul` — مشاهده مرام‌نامه و شخصیت دستیار\n\n"
                "📋 *دستورات اصلی گفتگو:*\n"
                "• `/new` — سشن تازه و پاکسازی تاریخچه مکالمه\n"
                "• `/model` — انتخاب و تعویض مدل هوش مصنوعی (دکمه‌های اینلاین)\n"
                "• `/agent` — سوییچ حالت ایجنت (Build, Plan, Explore, General)\n"
                "• `/sessions` — مشاهده و بازگشت به گفتگوهای قبلی\n"
                "• `/fork` — انشعاب و ایجاد کپی از سشن فعلی\n"
                "• `/share` — دریافت لینک وب قابل مشاهده مکالمه\n"
                "• `/stop` — متوقف‌سازی و لغو فوری پردازش جاری\n\n"
                "🛠 *ابزارهای کدنویسی، Git و سیستم:*\n"
                "• `/git` — مشاهده وضعیت فایل‌های تغییر یافته پروژه\n"
                "• `/diff` — مشاهده خط‌به‌خط تغییرات کد (Git Diff)\n"
                "• `/revert` — بازگردانی آخرین تغییرات اعمال‌شده در کد\n"
                "• `/unrevert` — اعمال مجدد تغییرات بازگردانده شده\n"
                "• `/todo` — مشاهده چک‌لیست برنامه‌ها و اقدامات دستیار\n"
                "• `/usage` — گزارش دقیق مصرف توکن‌ها و هزینه‌ها\n"
                "• `/skills` — فهرست مهارت‌های نصب‌شده OpenCode\n\n"
                "💻 *ترمینال و جستجو:*\n"
                "• `/sh <دستور>` — اجرای مستقیم در ترمینال (مانند `/sh ls -la`)\n"
                "• `/find <نام>` — جستجوی سریع فایل در پروژه\n"
                "• `/symbol <نام>` — جستجوی توابع و کلاس‌ها در کد (LSP)\n"
                "• `/cat <مسیر>` — نمایش متن یک فایل در تلگرام\n"
                "• `/download <مسیر>` — ارسال فایل سیستم به عنوان فایل تلگرام\n"
                "• `/status` — وضعیت سلامت سرور و متغیرهای پروژه\n\n"
                "📎 *ارسال فایل و صوت:* هر عکس، سند یا ویس بفرستید، دریافت و بررسی می‌شود.\n"
                "🔐 *مجوزها و سوالات:* تایید عملیات با دکمه‌های شیشه‌ای انجام می‌شود.\n\n"
                f"👤 *شناسه شما:* `{user_id}`\n"
                f"📁 *محیط پروژه:* `{config.OPENCODE_WORKSPACE_DIR}`"
            )
            await self.tg.send_message(chat_id, help_msg, parse_mode="Markdown", reply_markup=MAIN_REPLY_KEYBOARD)

        # /model - Interactive Model Switcher
        elif cmd == "/model":
            providers_data = await self.opencode.get_providers()
            providers = providers_data.get("providers", [])
            current_model = self.user_models.get(user_id, {}).get("modelID", "پیش‌فرض OpenCode")

            inline_buttons = []
            for p in providers:
                p_id = p.get("id")
                models = p.get("models", {})
                for m_id, m_data in list(models.items())[:8]:
                    m_name = m_data.get("name", m_id)
                    is_active = (m_id == current_model)
                    btn_text = f"✓ {m_name}" if is_active else m_name
                    btn_obj = {"text": btn_text, "callback_data": f"model:set:{p_id}:{m_id}"}
                    if is_active:
                        btn_obj["style"] = "success"
                        btn_obj["disabled"] = True
                    else:
                        btn_obj["style"] = "primary"
                    inline_buttons.append([btn_obj])

            keyboard = {"inline_keyboard": inline_buttons} if inline_buttons else None
            msg_text = (
                f"🤖 *انتخاب مدل هوش مصنوعی (Model Picker)*\n\n"
                f"• *مدل فعال:* `{current_model}`\n\n"
                f"روی یکی از مدل‌های زیر کلیک کنید تا برای این گفتگو فعال شود:"
            )
            await self.tg.send_message(chat_id, msg_text, parse_mode="Markdown", reply_markup=keyboard)

        # /agent - Interactive Agent Switcher
        elif cmd == "/agent":
            agents = await self.opencode.list_agents()
            current_agent = self.user_agents.get(user_id, "build")
            inline_buttons = []

            agent_emojis = {
                "build": "🔨",
                "plan": "📋",
                "explore": "🔍",
                "general": "🚀"
            }
            agent_fa = {
                "build": "Build (کدنویسی و ترمینال)",
                "plan": "Plan (طراحی و بدون ویرایش)",
                "explore": "Explore (کاوش سریع در کد)",
                "general": "General (وظایف چندمرحله‌ای)"
            }

            for a in agents:
                aname = a.get("name")
                is_active = (aname == current_agent)
                emoji = agent_emojis.get(aname, "🤖")
                title = agent_fa.get(aname, aname)
                btn_text = f"✓ {emoji} {title}" if is_active else f"{emoji} {title}"
                btn_obj = {"text": btn_text, "callback_data": f"agent:set:{aname}"}
                if is_active:
                    btn_obj["style"] = "success"
                    btn_obj["disabled"] = True
                else:
                    btn_obj["style"] = "primary"
                inline_buttons.append([btn_obj])

            keyboard = {"inline_keyboard": inline_buttons}
            text = (
                f"🧠 *انتخاب ایجنت OpenCode (Agent Selector)*\n\n"
                f"• *ایجنت فعال:* `{current_agent}`\n\n"
                f"حالت‌های در دسترس:\n"
                f"• 🔨 *Build:* اجرای ابزارها، ویرایش کد و ترمینال\n"
                f"• 📋 *Plan:* بررسی ساختار و طرح‌ریزی بدون تغییر در فایل‌ها\n"
                f"• 🔍 *Explore:* جستجوی سریع در کدهای پروژه\n"
                f"• 🚀 *General:* اجرای گام‌به‌گام و موازی وظایف\n\n"
                f"جهت سوییچ، روی ایجنت مورد نظر کلیک کنید:"
            )
            await self.tg.send_message(chat_id, text, parse_mode="Markdown", reply_markup=keyboard)

        # /git - VCS Status
        elif cmd == "/git":
            vcs_status = await self.opencode.get_vcs_status()
            if not vcs_status:
                await self.tg.send_message(chat_id, "🌿 *وضعیت گیت:* شاخه کاملاً تمیز است و تغییری وجود ندارد.", parse_mode="Markdown")
                return

            lines = ["📝 *وضعیت تغییرات پروژه (Git Status):*\n"]
            for f in vcs_status:
                status = f.get("status", "modified")
                fname = f.get("file", "")
                adds = f.get("additions", 0)
                dels = f.get("deletions", 0)
                icon = "🟢" if status == "added" else ("🔴" if status == "deleted" else "🟡")
                stat_str = f"+{adds}" if adds else ""
                if dels:
                    stat_str += f" -{dels}"
                lines.append(f"{icon} `{fname}` ({status}) `{stat_str}`")

            inline_buttons = [
                [
                    {"text": "📜 مشاهده Diff کامل", "callback_data": "vcs:diff"},
                    {"text": "↩️ بازگردانی آخرین تغییر", "callback_data": "vcs:revert"}
                ]
            ]
            keyboard = {"inline_keyboard": inline_buttons}
            await self.tg.send_message(chat_id, "\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)

        # /diff - File Changes Diff
        elif cmd == "/diff":
            session_id = await self.get_or_create_session(user_id)
            diffs = await self.opencode.get_vcs_diff("git")
            if not diffs:
                diffs = await self.opencode.get_diff(session_id)

            if not diffs:
                await self.tg.send_message(chat_id, "ℹ️ هیچ تغییری در فایل‌ها یافت نشد.")
                return

            diff_text = ""
            for d in diffs:
                path = d.get("file") or d.get("path", "file")
                patch = d.get("patch", "")
                diff_text += f"--- {path}\n{patch}\n\n"

            if len(diff_text) <= 3500:
                await self.tg.send_message(
                    chat_id,
                    f"📝 *تغییرات فایل‌ها (Git Diff):*\n```diff\n{diff_text}\n```",
                    parse_mode="Markdown"
                )
            else:
                tmp_patch = os.path.join(tempfile.gettempdir(), f"changes_{int(time.time())}.patch")
                with open(tmp_patch, "w", encoding="utf-8") as f:
                    f.write(diff_text)
                await self.tg.send_message(chat_id, f"📝 تغییرات زیاد بود ({len(diff_text):,} کاراکتر). در حال ارسال فایل کامل Patch...")
                await self.tg.send_document(chat_id, tmp_patch, caption="📄 فایل تغییرات (Git Patch)")
                try:
                    os.remove(tmp_patch)
                except Exception:
                    pass

        # /revert - Revert last modification
        elif cmd == "/revert":
            session_id = await self.get_or_create_session(user_id)
            ok = await self.opencode.revert_session(session_id)
            if ok:
                await self.tg.send_message(chat_id, "↩️ *آخرین تغییر کد و پیام با موفقیت بازگردانده (Revert) شد.*", parse_mode="Markdown")
            else:
                await self.tg.send_message(chat_id, "⚠️ بازگردانی امکان‌پذیر نبود یا تغییری برای revert یافت نشد.")

        # /unrevert - Redo reverted changes
        elif cmd == "/unrevert":
            session_id = await self.get_or_create_session(user_id)
            ok = await self.opencode.unrevert_session(session_id)
            if ok:
                await self.tg.send_message(chat_id, "🔁 *تغییرات بازگردانده شده مجدداً اعمال (Unrevert) شدند.*", parse_mode="Markdown")
            else:
                await self.tg.send_message(chat_id, "⚠️ اعمال مجدد امکان‌پذیر نبود.")

        # /share - Share Session publicly
        elif cmd == "/share":
            session_id = await self.get_or_create_session(user_id)
            await self.tg.send_chat_action(chat_id, "typing")
            share_url = await self.opencode.share_session(session_id)
            if share_url:
                inline_buttons = [
                    [{"text": "🌐 مشاهده سشن در مرورگر", "url": share_url}],
                    [{"text": "🔒 لغو اشتراک (Unshare)", "callback_data": f"sess:unshare:{session_id}"}]
                ]
                keyboard = {"inline_keyboard": inline_buttons}
                text = (
                    "🌐 *لینک اشتراک‌گذاری سشن ساخته شد!*\n\n"
                    f"🔗 `{share_url}`\n\n"
                    "با کلیک روی دکمه زیر می‌توانید گفتگوی کامل را در مرورگر وب مشاهده کنید:"
                )
                await self.tg.send_message(chat_id, text, parse_mode="Markdown", reply_markup=keyboard)
            else:
                await self.tg.send_message(chat_id, "❌ خطا در ساخت لینک اشتراک‌گذاری سشن.")

        # /fork - Fork current session
        elif cmd == "/fork":
            session_id = await self.get_or_create_session(user_id)
            new_id = await self.opencode.fork_session(session_id)
            if new_id:
                self.user_sessions[user_id] = new_id
                self.session_to_user[new_id] = user_id
                await self.tg.send_message(
                    chat_id,
                    f"🌿 *سشن فعلی منشعب (Fork) شد!*\n\nسشن جدید: `{new_id}`\nتمام تاریخچه تا این لحظه حفظ شد و از این پس روی شاخه جدید کار می‌کنید.",
                    parse_mode="Markdown"
                )
            else:
                await self.tg.send_message(chat_id, "❌ خطا در fork سشن.")

        # /sessions - Session Browser
        elif cmd == "/sessions":
            sessions = await self.opencode.list_sessions()
            if not sessions:
                await self.tg.send_message(chat_id, "ℹ️ هیچ سشنی یافت نشد.")
                return

            current_sid = self.user_sessions.get(user_id)
            msg_lines = ["📂 *لیست گفتگوها و سشن‌های اخیر:* \n"]
            inline_buttons = []

            for idx, s in enumerate(reversed(sessions[-6:]), 1):
                s_id = s.get("id", "")
                title = s.get("title", "بدون عنوان")
                is_curr = (s_id == current_sid)
                marker = "👉 " if is_curr else ""
                msg_lines.append(f"{idx}. {marker}*{title}*\n   `{s_id}`")
                btn_title = f"{'✓ ' if is_curr else ''}#{idx} {title[:20]}"
                inline_buttons.append([{"text": btn_title, "callback_data": f"sess:switch:{s_id}"}])

            keyboard = {"inline_keyboard": inline_buttons}
            await self.tg.send_message(chat_id, "\n".join(msg_lines), parse_mode="Markdown", reply_markup=keyboard)

        # /todo - AI Task Checklist
        elif cmd == "/todo":
            session_id = await self.get_or_create_session(user_id)
            todos = await self.opencode.get_todo(session_id)
            if not todos:
                await self.tg.send_message(chat_id, "ℹ️ تسک‌لیستی برای این سشن ثبت نشده است.")
                return

            lines = ["📋 *چک‌لیست وظایف و اقدامات دستیار:*\n"]
            for t in todos:
                status = t.get("status", "pending")
                title = t.get("title", "")
                icon = "✅" if status == "done" else ("⏳" if status == "in_progress" else "⬜")
                lines.append(f"{icon} {title}")

            await self.tg.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

        # /usage - Token Usage & Cost Stats
        elif cmd == "/usage":
            session_id = await self.get_or_create_session(user_id)
            try:
                sess_info = await self.opencode.get_session(session_id)
                tokens = sess_info.get("tokens", {})
                cost = sess_info.get("cost", 0)
                in_tok = tokens.get("input", 0)
                out_tok = tokens.get("output", 0)
                reason_tok = tokens.get("reasoning", 0)
                cache_read = tokens.get("cache", {}).get("read", 0)

                stats_msg = (
                    "📊 *آمار مصرف توکن‌ها در سشن جاری:*\n\n"
                    f"• 📥 *توکن‌های ورودی (Input):* `{in_tok:,}`\n"
                    f"• 📤 *توکن‌های خروجی (Output):* `{out_tok:,}`\n"
                    f"• 🧠 *استدلال و تفکر (Reasoning):* `{reason_tok:,}`\n"
                    f"• ⚡ *کش خوانده‌شده (Cache Hit):* `{cache_read:,}`\n"
                    f"• 💰 *هزینه تخمینی:* `${cost:.4f}`"
                )
                await self.tg.send_message(chat_id, stats_msg, parse_mode="Markdown")
            except Exception as e:
                await self.tg.send_message(chat_id, f"❌ خطا در دریافت آمار سشن: `{e}`", parse_mode="Markdown")

        # /sh - Shell Command Execution
        elif cmd == "/sh":
            if not args:
                await self.tg.send_message(chat_id, "⚠️ دستور را وارد کنید. مثال: `/sh ls -la`", parse_mode="Markdown")
                return

            cur_dir = self.get_user_workspace(user_id)
            session_id = await self.get_or_create_session(user_id, directory=cur_dir)

            # Stream execution status via draft instead of sending and editing a message
            draft_id = msg_id or random.randint(100000, 999999)
            mid = 0
            if chat_id > 0:
                await self.tg.send_message_draft(
                    chat_id,
                    draft_id,
                    f"⚡ <i>در حال اجرای دستور شل:</i> <code>{html.escape(args)}</code>...",
                    parse_mode="HTML",
                    can_stop=False
                )
            else:
                init_msg = await self.tg.send_message(chat_id, f"⚡ _در حال اجرای دستور شل:_ `{args}`...", parse_mode="Markdown")
                mid = init_msg.get("result", {}).get("message_id", 0)

            try:
                res = await self.opencode.run_shell(session_id, args, directory=cur_dir)
                out = ""
                for p in res.get("parts", []):
                    if p.get("type") == "tool":
                        out = p.get("state", {}).get("output", "")
                        break
                if not out:
                    out = "دستور بدون خروجی اجرا شد."

                if len(out) > 3800:
                    out = out[:3800] + "\n...(خلاصه شد)"

                output_text = f"🖥 *خروجی دستور شل:* `{args}`\n📍 *مسیر:* `{cur_dir}`\n\n```bash\n{out}\n```"
                if chat_id > 0:
                    await self.tg.send_message(chat_id, output_text, parse_mode="Markdown", reply_to_message_id=msg_id)
                else:
                    await self.tg.edit_message_text(chat_id, mid, output_text, parse_mode="Markdown")
            except Exception as e:
                err_text = f"❌ خطا در اجرای دستور: `{e}`"
                if chat_id > 0:
                    await self.tg.send_message(chat_id, err_text, parse_mode="Markdown", reply_to_message_id=msg_id)
                else:
                    await self.tg.edit_message_text(chat_id, mid, err_text, parse_mode="Markdown")

        # /cd - Change Working Directory
        elif cmd == "/cd":
            cur_dir = self.get_user_workspace(user_id)
            home_dir = str(Path.home())
            desktop_dir = str(Path.home() / "Desktop")
            downloads_dir = str(Path.home() / "Downloads")
            documents_dir = str(Path.home() / "Documents")
            if not args:
                msg = (
                    "📂 *مدیریت مسیر کاری ایجنت*\n\n"
                    f"📍 مسیر فعلی: `{cur_dir}`\n\n"
                    "جهت تغییر مسیر می‌توانید بنویسید:\n"
                    f"`/cd {desktop_dir}`\n"
                    "`/cd ..`\n"
                    "یا در چت به زبان طبیعی بگویید: *«برو به پوشه دسکتاپ»*\n\n"
                    "میان‌برهای سریع:"
                )
                shortcuts = [
                    [
                        {"text": "🖥 دسکتاپ", "callback_data": f"cd:to:{desktop_dir}"},
                        {"text": "📥 دانلودها", "callback_data": f"cd:to:{downloads_dir}"}
                    ],
                    [
                        {"text": "📄 اسناد", "callback_data": f"cd:to:{documents_dir}"},
                        {"text": "📁 پوشه پیش‌فرض", "callback_data": f"cd:to:{config.OPENCODE_WORKSPACE_DIR}"}
                    ],
                    [
                        {"text": "🏠 خانه (Home)", "callback_data": f"cd:to:{home_dir}"}
                    ]
                ]
                await self.tg.send_message(chat_id, msg, parse_mode="Markdown", reply_markup={"inline_keyboard": shortcuts})
                return


            resolved = resolve_path(args, cur_dir)
            if resolved and os.path.isdir(resolved):
                await self.switch_user_directory(chat_id, user_id, resolved, announce=True)
            else:
                await self.tg.send_message(chat_id, f"❌ پوشه مورد نظر یافت نشد:\n`{args}`", parse_mode="Markdown")

        # /pwd or /cwd - Current Working Directory
        elif cmd in ("/pwd", "/cwd"):
            cur_dir = self.get_user_workspace(user_id)
            files_count = 0
            try:
                files_count = len(list(os.scandir(cur_dir)))
            except Exception:
                pass
            msg = (
                "📍 *مسیر کاری فعلی ایجنت:*\n"
                f"`{cur_dir}`\n\n"
                f"📊 تعداد آیتم‌های این پوشه: `{files_count}`"
            )
            inline_buttons = [
                [
                    {"text": "📁 لیست فایل‌ها", "callback_data": f"dir:list:{cur_dir}"},
                    {"text": "🔄 تغییر مسیر (/cd)", "callback_data": "cmd:cd"}
                ]
            ]
            await self.tg.send_message(chat_id, msg, parse_mode="Markdown", reply_markup={"inline_keyboard": inline_buttons})

        # /post - Broadcast to Telegram Channels
        elif cmd == "/post":
            cur_channel = self.user_default_channels.get(str(user_id), "تنظیم نشده")
            if not args:
                await self.tg.send_message(
                    chat_id,
                    "📢 *ارسال پیام به کانال تلگرام*\n\n"
                    f"📌 کانال پیش‌فرض فعلی: `{cur_channel}`\n\n"
                    "نحوه استفاده:\n"
                    "• `/post @channel متن پیام شما`\n"
                    "• `/post متن پیام شما` (ارسال به کانال پیش‌فرض)\n"
                    "• `/set_channel @channel` (تنظیم کانال پیش‌فرض)\n\n"
                    "💡 _توجه: ربات باید در کانال ادمین با دسترسی ارسال پیام باشد._",
                    parse_mode="Markdown"
                )
                return

            post_parts = args.split(maxsplit=1)
            first_word = post_parts[0]
            if first_word.startswith("@") or (first_word.startswith("-") and first_word[1:].isdigit()):
                target_channel = first_word
                message_text = post_parts[1] if len(post_parts) > 1 else ""
            else:
                target_channel = self.user_default_channels.get(str(user_id))
                message_text = args

            if not target_channel:
                await self.tg.send_message(
                    chat_id,
                    "⚠️ کانالی مشخص نشده است. لطفاً شناسه کانال را وارد کنید:\n`/post @channel_name متن پیام`\nیا ابتدا با `/set_channel @channel_name` کانال پیش‌فرض را تعیین کنید.",
                    parse_mode="Markdown"
                )
                return

            if not message_text.strip():
                await self.tg.send_message(chat_id, "⚠️ متن پیام برای ارسال به کانال خالی است.", parse_mode="Markdown")
                return

            await self.post_to_channel(chat_id, target_channel, message_text)

        # /set_channel - Set default channel
        elif cmd == "/set_channel":
            if not args:
                await self.tg.send_message(chat_id, "⚠️ لطفاً شناسه یا یوزرنیم کانال را وارد کنید.\nمثال: `/set_channel @Sasm110313`", parse_mode="Markdown")
                return
            target = args.strip()
            if not (target.startswith("@") or target.startswith("-")):
                target = f"@{target}"
            self.user_default_channels[str(user_id)] = target
            save_user_channels(self.user_default_channels)
            await self.tg.send_message(
                chat_id,
                f"✅ *کانال پیش‌فرض شما با موفقیت ذخیره شد:*\n`{target}`\nاز این پس می‌توانید با دستور ساده `/post <متن>` یا از طریق هوش مصنوعی پیام‌هایتان را در این کانال منتشر کنید.",
                parse_mode="Markdown"
            )

        # /channels - View connected channels
        elif cmd == "/channels":
            cur_channel = self.user_default_channels.get(str(user_id), "تنظیم نشده")
            await self.tg.send_message(
                chat_id,
                f"📢 *کانال‌های متصل:*\n\n• کانال پیش‌فرض شما: `{cur_channel}`\n\nبرای تغییر کانال پیش‌فرض:\n`/set_channel @channel`",
                parse_mode="Markdown"
            )

        # /find - Project File Search
        elif cmd == "/find":
            if not args:
                await self.tg.send_message(chat_id, "⚠️ عبارت جستجو را وارد کنید. مثال: `/find py`", parse_mode="Markdown")
                return

            cur_dir = self.get_user_workspace(user_id)
            matches = await self.opencode.find_files(args, directory=cur_dir)
            if not matches:
                await self.tg.send_message(chat_id, f"🔍 فایلی با عبارت `{args}` در `{cur_dir}` یافت نشد.", parse_mode="Markdown")
                return

            lines = [f"🔍 *فایل‌های منطبق با `{args}` (در `{os.path.basename(cur_dir)}`):*\n"]
            inline_buttons = []
            for f in matches[:8]:
                lines.append(f"• `{f}`")
                inline_buttons.append([
                    {"text": f"📄 مشاهده {f[:18]}", "callback_data": f"file:view:{f}"},
                    {"text": "⬇️ دانلود", "callback_data": f"file:dl:{f}"}
                ])

            keyboard = {"inline_keyboard": inline_buttons}
            await self.tg.send_message(chat_id, "\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)

        # /symbol - Search symbols (functions, classes) via LSP
        elif cmd == "/symbol":
            if not args:
                await self.tg.send_message(chat_id, "⚠️ نام نماد را وارد کنید. مثال: `/symbol StreamSession`", parse_mode="Markdown")
                return

            symbols = await self.opencode.find_symbols(args)
            if not symbols:
                await self.tg.send_message(chat_id, f"🔍 هیچ نماد یا تابعی با نام `{args}` یافت نشد.", parse_mode="Markdown")
                return

            lines = [f"🔍 *نمادها و توابع منطبق با `{args}`:*\n"]
            for s in symbols[:10]:
                sname = s.get("name", "")
                skind = s.get("kind", "symbol")
                sloc = s.get("location", {})
                suri = sloc.get("uri", "")
                sline = sloc.get("range", {}).get("start", {}).get("line", 0) + 1
                fname = os.path.basename(suri)
                lines.append(f"• `{sname}` ({skind}) در `{fname}:{sline}`")

            await self.tg.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

        # /cat - File Viewer
        elif cmd == "/cat":
            if not args:
                await self.tg.send_message(chat_id, "⚠️ مسیر فایل را وارد کنید. مثال: `/cat config.py`", parse_mode="Markdown")
                return

            cur_dir = self.get_user_workspace(user_id)
            content = await self.opencode.get_file_content(args, directory=cur_dir)
            if content is not None:
                preview = content[:3800]
                if len(content) > 3800:
                    preview += "\n... (ادامه فایل کوتاه شد)"
                await self.tg.send_message(
                    chat_id,
                    f"📄 *فایل:* `{args}`\n\n```\n{preview}\n```",
                    parse_mode="Markdown"
                )
            else:
                await self.tg.send_message(chat_id, f"❌ فایل یافت نشد: `{args}`", parse_mode="Markdown")

        # /download - Send File Attachment to Telegram
        elif cmd == "/download":
            cur_dir = self.get_user_workspace(user_id)
            if not args:
                try:
                    files = [f for f in os.scandir(cur_dir) if f.is_file()][:10]
                    if not files:
                        await self.tg.send_message(chat_id, f"📁 هیچ فایلی در پوشه `{cur_dir}` یافت نشد.", parse_mode="Markdown")
                        return
                    inline_btns = []
                    for f in files:
                        inline_btns.append([{"text": f"⬇️ {f.name[:25]}", "callback_data": f"file:dl:{f.path}"}])
                    await self.tg.send_message(
                        chat_id,
                        "📂 *انتخاب فایل برای دانلود در تلگرام:*\nروی فایل مورد نظر کلیک کنید یا مسیر آن را وارد نمایید (مثال: `/download config.py`):",
                        parse_mode="Markdown",
                        reply_markup={"inline_keyboard": inline_btns}
                    )
                except Exception as e:
                    await self.tg.send_message(chat_id, "⚠️ مسیر فایل را وارد کنید. مثال: `/download config.py`", parse_mode="Markdown")
                return

            cand = find_candidate_file(args, cur_dir)
            if cand and os.path.exists(cand) and os.path.isfile(cand):
                await self.tg.send_chat_action(chat_id, "upload_document")
                ext = os.path.splitext(cand)[1].lower()
                caption = f"📁 `{os.path.basename(cand)}`"
                if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                    await self.tg.send_photo(chat_id, cand, caption=caption)
                else:
                    await self.tg.send_document(chat_id, cand, caption=caption)
            else:
                await self.tg.send_message(chat_id, f"❌ فایل در سیستم یافت نشد: `{args}`", parse_mode="Markdown")

        # /skills - List OpenCode Skills
        elif cmd == "/skills":
            skills = await self.opencode.list_skills()
            if not skills:
                await self.tg.send_message(chat_id, "ℹ️ مهارتی تعریف نشده است.")
                return

            lines = ["⚡ *فهرست مهارت‌ها (Skills) فعال در OpenCode:*\n"]
            for s in skills[:15]:
                name = s.get("name", "")
                desc = s.get("description", "").split("\n")[0][:60]
                lines.append(f"• *{name}*: {desc}")

            await self.tg.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

        # /new - Fresh Session
        elif cmd == "/new":
            cur_dir = self.get_user_workspace(user_id)
            old_sid = self.user_sessions.get(user_id)
            if old_sid:
                if old_sid in self.active_streams:
                    stream = self.active_streams.pop(old_sid)
                    stream.cancel()
                try:
                    await self.opencode.abort_session(old_sid, directory=cur_dir)
                except Exception:
                    pass
            new_sess = await self.opencode.create_session(title=f"TG User {user_id} - Fresh", directory=cur_dir)
            self.user_sessions[user_id] = new_sess["id"]
            self.session_to_user[new_sess["id"]] = user_id
            await self.tg.send_message(
                chat_id,
                f"🔄 *سشن جدید ساخته شد!*\nسشن قبلی ریست شد و زمینه مکالمه تازه است.\nشناسه سشن: `{new_sess['id']}`\n📍 مسیر کاری: `{cur_dir}`",
                parse_mode="Markdown",
                reply_markup=MAIN_REPLY_KEYBOARD
            )

        # /stop or /abort - Abort Task (Hermes parity)
        elif cmd in ("/stop", "/abort"):
            await self.abort_user_agent(chat_id, user_id)

        # /status
        elif cmd == "/status":
            alive = await self.opencode.check_server_alive()
            sess_id = self.user_sessions.get(user_id, "ثبت نشده")
            curr_model = self.user_models.get(user_id, {}).get("modelID", "پیش‌فرض OpenCode")
            curr_agent = self.user_agents.get(user_id, "build")
            cur_dir = self.get_user_workspace(user_id)
            status_text = (
                "📊 *وضعیت سیستم و سرور OpenCode:*\n\n"
                f"• وضعیت سرور: {'🟢 فعال و متصل' if alive else '🔴 غیرفعال'}\n"
                f"• آدرس سرور: `{config.OPENCODE_SERVER_URL}`\n"
                f"• سشن جاری شما: `{sess_id}`\n"
                f"• ایجنت فعال: `{curr_agent}`\n"
                f"• مدل انتخابی: `{curr_model}`\n"
                f"• مسیر کاری فعال: `{cur_dir}`\n"
                f"• کانال پیش‌فرض: `{self.user_default_channels.get(str(user_id), 'تنظیم نشده')}`\n"
                f"• نرخ بروزرسانی استریم: `{config.STREAM_THROTTLE_MS}ms`"
            )
            await self.tg.send_message(chat_id, status_text, parse_mode="Markdown", reply_markup=MAIN_REPLY_KEYBOARD)

        else:
            await self.tg.send_message(chat_id, "دستور نامعتبر است. از /help برای مشاهده لیست کامل استفاده کنید.", reply_markup=MAIN_REPLY_KEYBOARD)

    async def _typing_loop(self, chat_id: int):
        """Sends typing action periodically until output stream begins (max 45 seconds)."""
        start = time.time()
        try:
            while time.time() - start < 45:
                await self.tg.send_chat_action(chat_id, "typing")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    async def process_user_prompt(self, chat_id: int, user_id: int, prompt_text: str, reply_to_id: Optional[int]):
        server_ok = await self.opencode.ensure_server_running()
        if not server_ok:
            await self.tg.send_message(
                chat_id,
                "❌ سرور OpenCode در دسترس نیست و امکان اجرای خودکار آن وجود نداشت.\n"
                f"لطفاً مطمئن شوید دستور `{config.OPENCODE_COMMAND} serve` روی سیستم شما قابل اجراست.",
                parse_mode="Markdown"
            )
            return

        cur_dir = self.get_user_workspace(user_id)
        session_id = await self.get_or_create_session(user_id, directory=cur_dir)

        # In private chats, use native animated draft without dummy placeholder message!
        # In groups/channels, send initial placeholder for editMessageText fallback.
        initial_msg_id = 0
        if chat_id < 0:
            init_msg = await self.tg.send_message(
                chat_id,
                "🧠 <i>در حال پردازش و تحلیل...</i>\n\n⏳ <i>Working — 1s — iteration 1/150, waiting for provider response (streaming)</i>",
                parse_mode="HTML",
                reply_to_message_id=reply_to_id
            )
            if init_msg.get("ok"):
                initial_msg_id = init_msg["result"]["message_id"]

        # Callbacks for StreamSession
        async def on_workdir_change(new_dir: str):
            await self.switch_user_directory(chat_id, user_id, new_dir, announce=True)

        async def on_channel_post(channel: str, post_text: str):
            await self.post_to_channel(chat_id, channel, post_text)

        # Create active stream session with message reaction tracking
        stream_session = StreamSession(
            chat_id,
            initial_msg_id,
            self.tg,
            user_message_id=reply_to_id,
            workspace_dir=cur_dir,
            on_workdir_change=on_workdir_change,
            on_channel_post=on_channel_post,
        )
        self.active_streams[session_id] = stream_session

        # Immediate first draft update for instantaneous visual feedback
        asyncio.create_task(stream_session._do_edit())

        # Start typing status task
        self.typing_tasks[session_id] = asyncio.create_task(self._typing_loop(chat_id))

        # Check if memory context should be injected (NON-INTRUSIVE)
        prompt_to_send = prompt_text
        if memory_manager.is_memory_query(prompt_text):
            logger.info("Memory query detected for prompt: %s. Injecting Hermes & OpenCode memory context.", prompt_text[:50])
            memory_ctx = memory_manager.build_memory_context()
            prompt_to_send = f"{prompt_text}\n\n{memory_ctx}"

        # Send prompt to OpenCode
        try:
            user_model = self.user_models.get(user_id)
            m_provider = user_model.get("providerID") if user_model else None
            m_id = user_model.get("modelID") if user_model else None
            agent_mode = self.user_agents.get(user_id, "build")
            await self.opencode.send_prompt(
                session_id,
                prompt_to_send,
                model_provider=m_provider,
                model_id=m_id,
                agent=agent_mode,
                directory=cur_dir,
                system=TELEGRAM_AGENT_SYSTEM_PROMPT
            )
        except Exception as e:
            logger.exception("Error sending prompt to OpenCode: %s", e)
            if session_id in self.typing_tasks:
                self.typing_tasks[session_id].cancel()
                self.typing_tasks.pop(session_id, None)
            if session_id in self.active_streams:
                stream = self.active_streams.pop(session_id)
                stream.cancel()
            if initial_msg_id > 0:
                await self.tg.edit_message_text(
                    chat_id,
                    initial_msg_id,
                    f"❌ <b>خطا در ارسال دستور به OpenCode:</b>\n<code>{html.escape(str(e))}</code>",
                    parse_mode="HTML"
                )
            else:
                await self.tg.send_message(
                    chat_id,
                    f"❌ <b>خطا در ارسال دستور به OpenCode:</b>\n<code>{html.escape(str(e))}</code>",
                    parse_mode="HTML",
                    reply_to_message_id=reply_to_id
                )
            if reply_to_id:
                asyncio.create_task(self.tg.set_message_reaction(chat_id, reply_to_id, "👎"))
                asyncio.create_task(self.tg.unpin_chat_message(chat_id, reply_to_id))

    async def shutdown(self):
        self.running = False
        if self.failsafe_poll_task and not self.failsafe_poll_task.done():
            self.failsafe_poll_task.cancel()
        try:
            await self.tg.set_my_short_description("🔴 آفلاین (Offline) | دستیار هوشمند OpenCode")
        except Exception:
            pass
        await self.opencode.close()
        await self.tg.close()


async def main():
    bridge = OpenCodeTelegramBridge()
    try:
        await bridge.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
    finally:
        await bridge.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
