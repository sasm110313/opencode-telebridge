import asyncio
import html
import json
import logging
import os
import random
import re
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Any, Optional, Set, List, Callable

import aiohttp

import config
from memory_manager import memory_manager
from opencode_client import OpenCodeClient
from telegram_actions import (
    resolve_path,
    detect_directory_intent,
    detect_file_download_intent,
    find_candidate_file,
    BALE_AGENT_SYSTEM_PROMPT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("OpenCodeBaleBot")

# Bale Persistent 3x3 Keyboard
BALE_MAIN_REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "➕ سشن جدید"}, {"text": "🤖 مدل‌ها"}, {"text": "🧠 ایجنت"}],
        [{"text": "📂 مسیر کاری (/cd)"}, {"text": "🛑 توقف (/stop)"}, {"text": "💾 حافظه (/memory)"}],
        [{"text": "📝 تغییرات گیت"}, {"text": "💻 ترمینال (/sh)"}, {"text": "❓ راهنما"}]
    ],
    "resize_keyboard": True
}

MAX_BALE_MSG_LENGTH = 3800


class BaleChatThrottle:
    """Enforces per-chat rate limits for Bale API mutations."""
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_send_time: float = 0.0
        self.last_edit_time: float = 0.0
        self.last_action_time: float = 0.0
        self.flood_wait_until: float = 0.0


def beautify_text_layout(text: str) -> str:
    """Enhances mobile readability of Persian/English text:
    - Auto-wraps linux paths in code backticks.
    - Unglues sentences stuck together without spaces/newlines (e.g. 'میکنم.تست' -> 'میکنم.\n\nتست').
    - Fixes punctuation/brackets followed directly by Persian text (e.g. '):ضبط' -> '):\n\nضبط').
    - Breaks narrative action transitions on sentence ends ('. بگذارید' -> '.\n\nبگذارید').
    - Adds contextual emojis to narrative paragraphs (متوجه شدم -> 💡, بگذارید -> 🔍, پیدا شد -> 📁, پروژه -> ⚙️, حالا -> 📖).
    - Fixes emojis glued to hyphens (e.g. '✅- مدت' -> '✅\n\n• مدت').
    - Splits inline chained hyphenated specs ('- مدت: ۱ - تصویر: ۲') into separate lines.
    - Converts markdown bullet lists (- or *) into clean unicode bullets (• ).
    - Boldens spec keys before colons on bullet lines (• ⏱ مدت: -> • ⏱ *مدت:*).
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
    text = re.sub(r'(?<=[^\n])\s+[-–—]\s+(?=[آ-یa-zA-Z0-9_]+:)', r'\n• ', text)

    # 8. Convert line-starting markdown bullets (- or *) to clean bullet symbol
    text = re.sub(r'^[ \t]*[-*][ \t]+', r'• ', text, flags=re.MULTILINE)

    # 9. Contextual emojis for bullet items if not already prefixed with an emoji
    def bullet_emoji_repl(m):
        rest = m.group(2)
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

    # 10. Bolden bullet item keys before colon for Bale (*key:*)
    def bullet_bold_key_repl(m):
        prefix = m.group(1)
        key = m.group(2).strip()
        val = m.group(3)
        if "*" in key or "_" in key:
            return m.group(0)
        return f"{prefix}*{key}:*{val}"

    text = re.sub(r'^(•\s+(?:[\U00010000-\U0010ffff\u2600-\u27bf]\s+)?)([^:\n*_\(\)\[\]]{2,30}):(\s+.+)$', bullet_bold_key_repl, text, flags=re.MULTILINE)

    # 11. Contextual emojis for narrative/action lines
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


def format_to_bale_markdown(text: str) -> str:
    """Formats text strictly according to Bale Bot API Markdown specifications:
    - Bold: *متن* (must be separated by whitespace/newlines or line boundary)
    - Italic: _متن_
    - Inline code: `کد`
    - Code blocks: ```language\nکد\n```
    - Links: [عنوان](آدرس)
    - Converts HTML tags to Bale Markdown
    - Beautifies spacing and Persian emojis.
    """
    if not text:
        return ""

    # Strip custom agent thinking tags or internal markers
    text = re.sub(r'<tg-thinking>(.*?)</tg-thinking>', r'🧠 *تفکر ایجنت:*\n\1\n', text, flags=re.DOTALL)
    text = re.sub(r'</?(?:tg-thinking|think|thought)>', '', text)

    # Convert HTML code blocks before placeholders
    text = re.sub(r'<pre><code(?:\s+class="language-([a-zA-Z0-9_\-\+]+)")?>(.*?)</code></pre>',
                  lambda m: f"```{m.group(1) or ''}\n{html.unescape(m.group(2)).strip()}\n```", text, flags=re.DOTALL)
    text = re.sub(r'<pre>(.*?)</pre>',
                  lambda m: f"```\n{html.unescape(m.group(1)).strip()}\n```", text, flags=re.DOTALL)
    text = re.sub(r'<code>(.*?)</code>',
                  lambda m: f"`{html.unescape(m.group(1))}`", text, flags=re.DOTALL)

    # Balance unclosed code blocks for live deltas
    if text.count("```") % 2 == 1:
        text += "\n```"

    code_blocks: List[str] = []
    def cb_repl(m):
        lang = m.group(1) or ""
        code = m.group(2)
        idx = len(code_blocks)
        clean_code = code.rstrip("\n")
        code_blocks.append(f"```{lang.strip()}\n{clean_code}\n```")
        return f"BALEPLACEHOLDERCB{idx}BALE"

    text = re.sub(r'```([a-zA-Z0-9_\-\+]*)\n?(.*?)```', cb_repl, text, flags=re.DOTALL)

    # Balance unclosed inline code
    if text.count("`") % 2 == 1:
        text += "`"

    inline_codes: List[str] = []
    def ic_repl(m):
        code = m.group(1)
        idx = len(inline_codes)
        inline_codes.append(f"`{code}`")
        return f"BALEPLACEHOLDERIC{idx}BALE"

    text = re.sub(r'`([^`\n]+)`', ic_repl, text)

    # Convert common HTML inline formatting
    text = re.sub(r'<(?:b|strong)>(.*?)</(?:b|strong)>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<(?:i|em)>(.*?)</(?:i|em)>', r'_\1_', text, flags=re.DOTALL)
    text = re.sub(r'<a\s+href="([^"]+)">([^<]+)</a>', r'[\2](\1)', text)
    text = re.sub(r'<(?:br|p)\s*/?>', '\n', text)
    # Strip any remaining stray HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)

    # Beautify prose layout, spacing, bullets, and emojis
    text = beautify_text_layout(text)

    # Markdown Headings: # Heading -> *Heading*
    text = re.sub(r'^(?:#{1,6})\s*(.+)$', r'*\1*', text, flags=re.MULTILINE)

    # Convert standard double-asterisk bold (**bold** or __bold__) to Bale single asterisk (*bold*)
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'*\1*', text, flags=re.DOTALL)

    # Ensure Bale requirement: *bold* and _italic_ must have spacing or boundary around them
    def fix_bold_spacing(m):
        inner = m.group(1).strip()
        if not inner:
            return ""
        return f" *{inner}* "

    text = re.sub(r'(?<!\*)\*([^*\n\s](?:[^*\n]*[^*\n\s])?)\*(?!\*)', fix_bold_spacing, text)

    # Restore placeholders
    for i, cb in enumerate(code_blocks):
        text = text.replace(f"BALEPLACEHOLDERCB{i}BALE", cb)
    for i, ic in enumerate(inline_codes):
        text = text.replace(f"BALEPLACEHOLDERIC{i}BALE", ic)

    # Normalize excessive spaces and clean up edges
    text = re.sub(r'[ \t]+$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


class BaleAPI:
    """Async API Client for Bale Bot API (https://tapi.bale.ai/bot<token>)."""
    def __init__(self, token: str, proxy: Optional[str] = None):
        self.token = token
        self.base_url = f"{config.BALE_API_BASE_URL}/bot{token}"
        self.file_base_url = f"{config.BALE_API_BASE_URL}/file/bot{token}"
        self.proxy = proxy or config.BALE_PROXY
        self._session: Optional[aiohttp.ClientSession] = None
        self.chat_throttles: Dict[int, BaleChatThrottle] = {}
        self.global_flood_wait_until: float = 0.0

    def get_throttle(self, chat_id: int) -> BaleChatThrottle:
        if chat_id not in self.chat_throttles:
            self.chat_throttles[chat_id] = BaleChatThrottle()
        return self.chat_throttles[chat_id]

    def get_flood_wait(self, chat_id: Optional[int] = None) -> float:
        now = time.time()
        g_wait = max(0.0, self.global_flood_wait_until - now)
        c_wait = max(0.0, self.get_throttle(chat_id).flood_wait_until - now) if chat_id else 0.0
        return max(g_wait, c_wait)

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

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
            wait = self.get_flood_wait(chat_id)
            if wait > 0:
                logger.warning("Bale request to %s suppressed by circuit breaker (%ds remaining).", endpoint, int(wait))
                return {"ok": False, "error_code": 429, "description": f"Flood wait active ({int(wait)}s)"}

            try:
                async with session.request(method, url, timeout=aiohttp.ClientTimeout(total=45), **kwargs) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        raw_body = await resp.text()
                        logger.error("Bale API non-JSON response from %s (HTTP %s): %s", endpoint, resp.status, raw_body[:200])
                        return {"ok": False, "error_code": resp.status, "description": "Invalid JSON response"}

                    if resp.status == 200 and data.get("ok"):
                        return data

                    # Handle 429 / Rate limit
                    if resp.status == 429 or "rate" in str(data.get("description", "")).lower():
                        retry_after = data.get("parameters", {}).get("retry_after", 5)
                        logger.warning("⚠️ Bale Rate-limit (429) on %s! Backing off %ds...", endpoint, retry_after)
                        if chat_id:
                            self.get_throttle(chat_id).flood_wait_until = time.time() + retry_after
                        else:
                            self.global_flood_wait_until = time.time() + retry_after
                        await asyncio.sleep(retry_after)
                        continue

                    # Safe ignore if text was not modified
                    desc = str(data.get("description", ""))
                    if "message is not modified" in desc.lower() or "exactly the same" in desc.lower():
                        return {"ok": True, "result": True, "not_modified": True}

                    logger.debug("Bale API returned status %s on %s: %s", resp.status, endpoint, data)
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning("Network error calling Bale API %s (attempt %d): %s", endpoint, attempt + 1, e)
                await asyncio.sleep(1.0 + attempt * 1.5)

        return {"ok": False, "description": "Max retries exceeded"}

    async def get_me(self) -> Dict[str, Any]:
        return await self.request("GET", "getMe")

    async def get_updates(self, offset: Optional[int] = None, limit: int = 50, timeout: int = 20) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return await self.request("POST", "getUpdates", json=params)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        throttle = self.get_throttle(chat_id)
        async with throttle.lock:
            now = time.time()
            gap = now - throttle.last_send_time
            if gap < 0.35:
                await asyncio.sleep(0.35 - gap)

            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "text": text
            }
            if reply_to_message_id:
                payload["reply_to_message_id"] = reply_to_message_id
            if reply_markup:
                payload["reply_markup"] = reply_markup

            res = await self.request("POST", "sendMessage", json=payload)
            throttle.last_send_time = time.time()
            return res

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        throttle = self.get_throttle(chat_id)
        async with throttle.lock:
            min_edit_gap = config.BALE_STREAM_THROTTLE_MS / 1000.0
            now = time.time()
            gap = now - throttle.last_edit_time
            if gap < min_edit_gap:
                await asyncio.sleep(min_edit_gap - gap)

            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup

            res = await self.request("POST", "editMessageText", json=payload)
            throttle.last_edit_time = time.time()
            return res

    async def delete_message(self, chat_id: int, message_id: int) -> Dict[str, Any]:
        return await self.request("POST", "deleteMessage", json={"chat_id": chat_id, "message_id": message_id})

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> Dict[str, Any]:
        throttle = self.get_throttle(chat_id)
        now = time.time()
        if now - throttle.last_action_time < 3.5:
            return {"ok": True}
        throttle.last_action_time = now
        return await self.request("POST", "sendChatAction", params={"chat_id": str(chat_id), "action": action})

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text:
            payload["text"] = text
        return await self.request("POST", "answerCallbackQuery", json=payload)

    async def set_my_commands(self, commands: List[Dict[str, str]]) -> Dict[str, Any]:
        return await self.request("POST", "setMyCommands", json={"commands": commands})

    async def get_file(self, file_id: str) -> Dict[str, Any]:
        return await self.request("POST", "getFile", json={"file_id": file_id})

    async def download_file(self, file_path_on_bale: str, destination_path: str) -> bool:
        session = await self.get_session()
        url = f"{self.file_base_url}/{file_path_on_bale}"
        kwargs: Dict[str, Any] = {}
        if self.proxy:
            kwargs["proxy"] = self.proxy

        try:
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
            async with session.get(url, **kwargs) as resp:
                if resp.status == 200:
                    with open(destination_path, "wb") as f:
                        while True:
                            chunk = await resp.content.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                    return True
                else:
                    logger.error("Failed to download file from Bale (HTTP %s): %s", resp.status, url)
                    return False
        except Exception as e:
            logger.exception("Exception downloading file from Bale: %s", e)
            return False

    async def send_document(
        self,
        chat_id: int,
        file_path: str,
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        session = await self.get_session()
        url = f"{self.base_url}/sendDocument"
        kwargs: Dict[str, Any] = {}
        if self.proxy:
            kwargs["proxy"] = self.proxy

        try:
            data = aiohttp.FormData()
            data.add_field("chat_id", str(chat_id))
            if caption:
                data.add_field("caption", caption)
            if reply_to_message_id:
                data.add_field("reply_to_message_id", str(reply_to_message_id))
            if reply_markup:
                data.add_field("reply_markup", json.dumps(reply_markup))

            with open(file_path, "rb") as f:
                data.add_field("document", f, filename=os.path.basename(file_path))
                async with session.post(url, data=data, **kwargs) as resp:
                    return await resp.json()
        except Exception as e:
            logger.exception("Error sending document to Bale: %s", e)
            return {"ok": False, "description": str(e)}

    async def send_photo(
        self,
        chat_id: int,
        photo_path: str,
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        session = await self.get_session()
        url = f"{self.base_url}/sendPhoto"
        kwargs: Dict[str, Any] = {}
        if self.proxy:
            kwargs["proxy"] = self.proxy

        try:
            data = aiohttp.FormData()
            data.add_field("chat_id", str(chat_id))
            if caption:
                data.add_field("caption", caption)
            if reply_to_message_id:
                data.add_field("reply_to_message_id", str(reply_to_message_id))
            if reply_markup:
                data.add_field("reply_markup", json.dumps(reply_markup))

            with open(photo_path, "rb") as f:
                data.add_field("photo", f, filename=os.path.basename(photo_path))
                async with session.post(url, data=data, **kwargs) as resp:
                    return await resp.json()
        except Exception as e:
            logger.exception("Error sending photo to Bale: %s", e)
            return {"ok": False, "description": str(e)}


class BaleStreamSession:
    """Manages active streaming message buffer and live tool execution tracking for a Bale turn."""
    def __init__(
        self,
        chat_id: int,
        initial_message_id: int,
        bale: BaleAPI,
        user_message_id: Optional[int] = None,
        workspace_dir: str = "",
        on_workdir_change: Optional[Callable[[str], Any]] = None,
    ):
        self.chat_id = chat_id
        self.current_message_id = initial_message_id
        self.user_message_id = user_message_id
        self.bale = bale
        self.workspace_dir = workspace_dir or config.OPENCODE_WORKSPACE_DIR
        self.on_workdir_change = on_workdir_change

        # Live tool execution tracking
        self.activity_entries: List[Dict[str, Any]] = []
        self.seen_tool_parts: Dict[str, int] = {}
        self.seen_text_parts: Dict[str, int] = {}
        self.start_time: float = time.time()
        self.status_note: str = "در حال اتصال به هوش مصنوعی..."
        self.reasoning_text: str = ""
        self.current_running_tool: Optional[str] = None
        self.tool_start_time: float = 0.0

        self.full_text = ""
        self.last_edit_time = 0.0
        self.last_rendered_text = ""
        self.lock = asyncio.Lock()
        self.is_done = False
        self.ticker_task: Optional[asyncio.Task] = asyncio.create_task(self._ticker_loop())

    def cancel(self):
        """Immediately cancel background ticker."""
        self.is_done = True
        if self.ticker_task and not self.ticker_task.done():
            self.ticker_task.cancel()

    def append_reasoning(self, delta: str):
        if delta:
            self.reasoning_text += delta
            self.current_running_tool = None
            self.status_note = "در حال تحلیل و تفکر عمیق..."

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

        # 1. Shell / Terminal
        if tool_lower in ("bash", "shell", "sh", "exec", "terminal"):
            cmd = input_data.get("command") or title or ""
            cmd_lines = cmd.strip().split("\n")
            first_line = cmd_lines[0].strip() if cmd_lines else ""
            if len(cmd_lines) > 1 and len(first_line) > 35:
                first_line = first_line[:32] + " ..."
            elif len(cmd_lines) > 1:
                first_line = first_line + " ..."
            elif len(first_line) > 45:
                first_line = first_line[:42] + "..."
            return f"• 💻 terminal: `{first_line}`" if first_line else "• 💻 terminal"

        # 2. Reading files
        elif tool_lower in ("read", "view_file", "cat"):
            p = input_data.get("filePath") or input_data.get("path") or title or ""
            p_str = clean_p(p)
            s_line = input_data.get("startLine") or input_data.get("StartLine")
            e_line = input_data.get("endLine") or input_data.get("EndLine")
            lines = f" L{s_line}-{e_line}" if s_line and e_line else (f" L{s_line}" if s_line else "")
            return f"• 📖 خواندن `{p_str}`{lines}" if p_str else "• 📖 خواندن فایل"

        # 3. Writing / Editing files
        elif tool_lower in ("write", "write_to_file", "create_file", "edit", "replace_file_content", "patch"):
            p = input_data.get("filePath") or input_data.get("path") or title or ""
            p_str = clean_p(p)
            return f"• ✍️ ویرایش `{p_str}`" if p_str else "• ✍️ ویرایش فایل"

        # 4. Searching
        elif tool_lower in ("grep", "grep_search", "find", "find_files", "find_by_name"):
            q = input_data.get("query") or input_data.get("pattern") or title or ""
            if len(q) > 35:
                q = q[:32] + "..."
            return f"• 🔍 جستجوی `{q}`" if q else "• 🔍 جستجو در کد"

        # 5. Generic tools
        display_name = title or tool_name or "دستور"
        if len(display_name) > 35:
            display_name = display_name[:32] + "..."
        return f"• ⚙️ `{display_name}`"

    def on_tool_start(self, call_id: str, tool_name: str, input_data: dict, title: str = ""):
        entry_text = self._format_tool_entry(tool_name, input_data, title)
        self.current_running_tool = entry_text
        self.tool_start_time = time.time()
        self.status_note = "در حال اجرای ابزار..."

        if call_id and call_id in self.seen_tool_parts:
            idx = self.seen_tool_parts[call_id]
            self.activity_entries[idx]["text"] = entry_text
            return

        idx = len(self.activity_entries)
        if call_id:
            self.seen_tool_parts[call_id] = idx
        self.activity_entries.append({"key": call_id, "text": entry_text, "duration": None})

    def on_tool_finish(self, call_id: str, output_data: Any = None):
        self.status_note = "در حال تدوین پاسخ..."
        now = time.time()
        if self.tool_start_time > 0 and self.current_running_tool:
            dur = max(1, int(now - self.tool_start_time))
            if call_id and call_id in self.seen_tool_parts:
                idx = self.seen_tool_parts[call_id]
                self.activity_entries[idx]["duration"] = dur

        self.current_running_tool = None
        self.tool_start_time = 0.0

    def append_delta(self, delta: str):
        if not delta:
            return
        self.full_text += delta
        self.status_note = "در حال دریافت پاسخ..."

    def _render_current_display(self, is_final: bool = False) -> str:
        """Assembles the message text with live ticker and formatted markdown."""
        elapsed = int(time.time() - self.start_time)
        sections: List[str] = []

        if not is_final:
            sections.append(f"⏳ *{self.status_note}* ({elapsed}s)")

            if self.activity_entries or self.current_running_tool:
                tools_lines = []
                recent_entries = self.activity_entries[-5:]
                for item in recent_entries:
                    line = item["text"]
                    if item.get("duration"):
                        line += f" ({item['duration']}s)"
                    tools_lines.append(line)

                if self.current_running_tool and not any(e["text"] == self.current_running_tool for e in recent_entries):
                    t_dur = max(1, int(time.time() - self.tool_start_time)) if self.tool_start_time > 0 else 1
                    tools_lines.append(f"{self.current_running_tool} ({t_dur}s) ⏳")

                if tools_lines:
                    sections.append("🛠 *ابزارهای اجرا شده:*\n" + "\n".join(tools_lines))

            if self.reasoning_text and not self.full_text:
                r_clean = self.reasoning_text.strip()
                if len(r_clean) > 250:
                    r_clean = "..." + r_clean[-250:]
                sections.append(f"🧠 *تفکر:*\n_{r_clean}_")

        body = self.full_text.strip()
        if body:
            formatted_body = format_to_bale_markdown(body)
            if not is_final:
                formatted_body += " ▌"
            sections.append(formatted_body)
        elif not is_final and not sections:
            sections.append("⏳ *در حال اتصال به دستیار هوشمند OpenCode...*")

        return "\n\n".join(sections)

    async def _ticker_loop(self):
        """Background ticker to periodically update message and send typing chatAction."""
        while not self.is_done:
            try:
                await asyncio.sleep(1.0)
                if self.is_done:
                    break

                await self.bale.send_chat_action(self.chat_id, "typing")

                now = time.time()
                min_gap = config.BALE_STREAM_THROTTLE_MS / 1000.0
                if now - self.last_edit_time >= min_gap:
                    await self._flush_update()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Error in BaleStreamSession ticker loop: %s", e)

    async def _flush_update(self, is_final: bool = False):
        """Pushes current rendered text to Bale if changed."""
        async with self.lock:
            rendered = self._render_current_display(is_final=is_final)
            if not rendered or rendered == self.last_rendered_text:
                return

            if len(rendered) > MAX_BALE_MSG_LENGTH:
                trimmed = rendered[:MAX_BALE_MSG_LENGTH].rstrip() + "\n\n*(ادامه در پیام بعد...)*"
                await self.bale.edit_message_text(self.chat_id, self.current_message_id, trimmed)
                self.last_rendered_text = trimmed
                self.last_edit_time = time.time()

                remainder = rendered[MAX_BALE_MSG_LENGTH:].lstrip()
                res = await self.bale.send_message(self.chat_id, remainder)
                if res.get("ok"):
                    self.current_message_id = res["result"]["message_id"]
                    self.last_rendered_text = remainder
                return

            res = await self.bale.edit_message_text(self.chat_id, self.current_message_id, rendered)
            if res.get("ok"):
                self.last_rendered_text = rendered
                self.last_edit_time = time.time()

    async def finish(self, success: bool = True):
        """Finalizes the turn: stops ticker, writes complete formatted markdown, checks directives."""
        self.cancel()
        async with self.lock:
            final_text = format_to_bale_markdown(self.full_text)
            if not final_text:
                final_text = "✅ *عملیات با موفقیت انجام شد.*" if success else "❌ *عملیات با خطا متوقف شد.*"

            media_matches = re.findall(r'^MEDIA:(.+)$', final_text, flags=re.MULTILINE)
            workdir_matches = re.findall(r'^WORKDIR:(.+)$', final_text, flags=re.MULTILINE)

            final_text = re.sub(r'^(?:MEDIA|WORKDIR):.+$\n?', '', final_text, flags=re.MULTILINE).strip()

            if workdir_matches and self.on_workdir_change:
                new_dir = workdir_matches[-1].strip()
                if os.path.isdir(new_dir):
                    await self.on_workdir_change(new_dir)
                    final_text += f"\n\n📂 *مسیر کاری با موفقیت تغییر یافت:* `{new_dir}`"

            if len(final_text) > MAX_BALE_MSG_LENGTH:
                chunks = [final_text[i:i + MAX_BALE_MSG_LENGTH] for i in range(0, len(final_text), MAX_BALE_MSG_LENGTH)]
                await self.bale.edit_message_text(self.chat_id, self.current_message_id, chunks[0])
                for ch in chunks[1:]:
                    await self.bale.send_message(self.chat_id, ch)
            else:
                await self.bale.edit_message_text(self.chat_id, self.current_message_id, final_text)

            self.last_rendered_text = final_text

            for m_path in media_matches:
                m_path = m_path.strip()
                if os.path.exists(m_path) and os.path.isfile(m_path):
                    ext = os.path.splitext(m_path)[1].lower()
                    caption = f"📎 `{os.path.basename(m_path)}`"
                    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                        await self.bale.send_photo(self.chat_id, m_path, caption=caption)
                    else:
                        await self.bale.send_document(self.chat_id, m_path, caption=caption)


class OpenCodeBaleBridge:
    """Bridge between Bale Messenger Bot and local OpenCode server."""
    def __init__(self):
        self.bale = BaleAPI(config.BALE_BOT_TOKEN, proxy=config.BALE_PROXY)
        self.opencode = OpenCodeClient(
            base_url=config.OPENCODE_SERVER_URL,
            auto_start=config.OPENCODE_AUTO_START,
            command=config.OPENCODE_COMMAND,
            workspace_dir=config.OPENCODE_WORKSPACE_DIR,
            password=config.OPENCODE_SERVER_PASSWORD
        )
        self.user_sessions: Dict[int, str] = {}
        self.session_to_user: Dict[str, int] = {}
        self.active_streams: Dict[str, BaleStreamSession] = {}
        self.pending_permissions: Dict[str, Dict[str, Any]] = {}
        self.pending_questions: Dict[str, Dict[str, Any]] = {}
        self.user_models: Dict[int, Dict[str, str]] = {}
        self.user_agents: Dict[int, str] = {}
        self.user_workspace_dir: Dict[int, str] = {}
        self.failsafe_poll_task: Optional[asyncio.Task] = None
        self.running = False
        self.bot_id: Optional[int] = None
        self.bot_username: Optional[str] = None

    async def start(self):
        if not config.BALE_BOT_TOKEN:
            logger.error("BALE_BOT_TOKEN is not configured! Please set it in .env or environment.")
            return

        bot_info = await self.bale.get_me()
        if not bot_info.get("ok"):
            logger.error("Failed to connect to Bale Bot API: %s", bot_info.get("description"))
            return

        self.bot_id = bot_info["result"].get("id")
        self.bot_username = bot_info["result"].get("username", "OpenCodeBaleBot")
        logger.info("🤖 Bale Bot connected successfully: @%s (ID: %s)", self.bot_username, self.bot_id)

        await self._register_bot_commands()

        logger.info("Connecting to OpenCode server at %s...", config.OPENCODE_SERVER_URL)
        server_ok = await self.opencode.ensure_server_running()
        if not server_ok:
            logger.warning("Could not immediately reach OpenCode server. Will retry when commands arrive.")
        else:
            logger.info("OpenCode server is online and ready!")

        self.opencode.add_event_listener(self.handle_opencode_event)
        await self.opencode.start_event_stream()

        self.running = True
        self.failsafe_poll_task = asyncio.create_task(self._failsafe_poll_loop())

        logger.info("Starting Bale Bot polling loop...")
        await self.poll_updates()

    async def _register_bot_commands(self):
        """Registers official commands list in Bale Bot."""
        commands = [
            {"command": "start", "description": "شروع کار و راهنمای سیستم"},
            {"command": "help", "description": "راهنمای کامل امکانات و دستورات"},
            {"command": "new", "description": "سشن تازه و پاکسازی تاریخچه مکالمه"},
            {"command": "model", "description": "انتخاب و تغییر مدل هوش مصنوعی"},
            {"command": "agent", "description": "تغییر حالت ایجنت (Build, Plan, Explore, General)"},
            {"command": "cd", "description": "تغییر پوشه کاری دستیار در سیستم"},
            {"command": "pwd", "description": "مشاهده مسیر کاری و فایل‌های موجود"},
            {"command": "git", "description": "وضعیت فایل‌های تغییر یافته گیت"},
            {"command": "diff", "description": "مشاهده کامل تغییرات کد (Diff)"},
            {"command": "todo", "description": "چک‌لیست تسک‌ها و برنامه‌های سشن"},
            {"command": "sh", "description": "اجرای مستقیم دستور در ترمینال سیستم"},
            {"command": "cat", "description": "مشاهده محتوای یک فایل در چت"},
            {"command": "download", "description": "دانلود فایل از سیستم در بله"},
            {"command": "memory", "description": "مشاهده حافظه دائم و تنظیمات پایدار"},
            {"command": "remember", "description": "افزودن نکته به حافظه دائم (/remember <متن>)"},
            {"command": "status", "description": "بررسی وضعیت سرور و سشن جاری"},
            {"command": "stop", "description": "توقف فوری پردازش جاری"},
            {"command": "whoami", "description": "اطلاعات حساب و سطح دسترسی شما"}
        ]
        try:
            await self.bale.set_my_commands(commands)
            logger.info("Successfully registered commands menu in Bale.")
        except Exception as e:
            logger.warning("Failed to register commands in Bale: %s", e)

    def is_user_authorized(self, user_id: int) -> bool:
        if not config.BALE_ALLOWED_USERS:
            config.BALE_ALLOWED_USERS.add(user_id)
            logger.info("🎉 Auto-paired user %s as Bale bot owner!", user_id)
            self._save_owner_to_env(user_id)
            return True
        return user_id in config.BALE_ALLOWED_USERS

    def _save_owner_to_env(self, user_id: int):
        try:
            env_path = config.env_file
            if env_path.exists():
                content = env_path.read_text(encoding="utf-8")
                if "BALE_ALLOWED_USERS=" in content:
                    new_content = re.sub(r"BALE_ALLOWED_USERS=.*", f"BALE_ALLOWED_USERS={user_id}", content)
                else:
                    new_content = content + f"\nBALE_ALLOWED_USERS={user_id}\n"
                env_path.write_text(new_content, encoding="utf-8")
                logger.info("Saved BALE_ALLOWED_USERS=%s to .env", user_id)
        except Exception as e:
            logger.warning("Could not persist BALE_ALLOWED_USERS to .env: %s", e)

    def get_user_workspace(self, user_id: int) -> str:
        return self.user_workspace_dir.get(user_id, config.OPENCODE_WORKSPACE_DIR)

    def set_user_workspace(self, user_id: int, new_dir: str):
        self.user_workspace_dir[user_id] = new_dir

    def is_agent_running(self, user_id: int) -> bool:
        sid = self.user_sessions.get(user_id)
        if not sid or sid not in self.active_streams:
            return False
        stream = self.active_streams[sid]
        if stream.is_done:
            self.active_streams.pop(sid, None)
            return False
        if time.time() - stream.start_time > 900:
            logger.warning("Bale session %s exceeded 15min timeout, clearing lock.", sid)
            self.active_streams.pop(sid, None)
            return False
        return True

    def get_user_model(self, user_id: int) -> Dict[str, str]:
        if user_id not in self.user_models:
            self.user_models[user_id] = {"providerID": "opencode", "modelID": "ling-3.0-flash-fin-free"}
        return self.user_models[user_id]

    async def get_or_create_session(self, user_id: int) -> str:
        if user_id in self.user_sessions:
            return self.user_sessions[user_id]
        cur_dir = self.get_user_workspace(user_id)
        session = await self.opencode.create_session(title=f"Bale Session {user_id}", directory=cur_dir)
        session_id = session.get("id") or str(session)
        self.user_sessions[user_id] = session_id
        self.session_to_user[session_id] = user_id

        # Switch to ling-3.0-flash-fin-free by default
        m = self.get_user_model(user_id)
        try:
            await self.opencode.switch_session_model(session_id, m["providerID"], m["modelID"])
        except Exception as e:
            logger.debug("Could not switch initial model: %s", e)
        return session_id

    async def abort_user_agent(self, chat_id: int, user_id: int):
        sid = self.user_sessions.get(user_id)
        cur_dir = self.get_user_workspace(user_id)
        if not sid or sid not in self.active_streams:
            if sid:
                try:
                    await self.opencode.abort_session(sid, directory=cur_dir)
                except Exception:
                    pass
            await self.bale.send_message(
                chat_id,
                "ℹ️ *در حال حاضر عملیات فعالی برای شما در حال اجرا نیست.*",
                reply_markup=BALE_MAIN_REPLY_KEYBOARD
            )
            return

        logger.info("User %s requested abort for Bale session %s", user_id, sid)
        try:
            await self.opencode.abort_session(sid, directory=cur_dir)
        except Exception as e:
            logger.warning("Error aborting session on OpenCode: %s", e)

        stream = self.active_streams.pop(sid, None)
        if stream:
            stream.cancel()
            await stream.append_delta("\n\n🛑 *عملیات توسط کاربر متوقف شد.*")
            await stream.finish(success=False)

        await self.bale.send_message(chat_id, "🛑 *عملیات جاری با موفقیت لغو و متوقف شد.*", reply_markup=BALE_MAIN_REPLY_KEYBOARD)

    async def handle_opencode_event(self, event: Dict[str, Any]):
        """Processes SSE events streamed from OpenCode server."""
        event_type = event.get("type", "")
        props = event.get("properties", {})

        session_id = props.get("sessionID") or props.get("id")
        if session_id and session_id not in self.session_to_user:
            return

        if event_type == "message.part.delta":
            part_id = props.get("partID") or props.get("id")
            delta = props.get("delta", "")
            part_type = props.get("type", "")
            stream = self.active_streams.get(session_id)
            if stream and delta:
                if part_type == "reasoning":
                    stream.append_reasoning(delta)
                else:
                    stream.append_delta(delta)

        elif event_type == "message.part.start":
            part = props.get("part", {})
            part_type = part.get("type", "")
            call_id = part.get("id") or part.get("callID")
            stream = self.active_streams.get(session_id)
            if stream and part_type == "tool":
                tool_name = part.get("tool", "")
                input_data = part.get("input", {})
                title = part.get("title", "")
                stream.on_tool_start(call_id, tool_name, input_data, title=title)

        elif event_type == "message.part.finish":
            part = props.get("part", {})
            part_type = part.get("type", "")
            call_id = part.get("id") or part.get("callID")
            stream = self.active_streams.get(session_id)
            if stream and part_type == "tool":
                stream.on_tool_finish(call_id, part.get("output"))

        elif event_type in ("permission.asked", "permission.request"):
            await self._dispatch_permission(props)

        elif event_type in ("question.asked", "question.request"):
            await self._dispatch_question(props)

        elif event_type in ("session.idle", "session.status"):
            st_type = props.get("status", {}).get("type") if event_type == "session.status" else "idle"
            if st_type == "idle":
                if session_id in self.active_streams:
                    stream = self.active_streams.pop(session_id)
                    await stream.finish(success=True)

        elif event_type == "session.error":
            err_msg = str(props.get("error", ""))
            if session_id in self.active_streams:
                stream = self.active_streams.pop(session_id)
                stream.cancel()
                if "Aborted" not in err_msg:
                    stream.append_delta(f"\n\n❌ *خطا در اجرای فرآیند:*\n`{err_msg}`")
                await stream.finish(success=False)

    async def _failsafe_poll_loop(self):
        """Failsafe poller for pending OpenCode permissions and questions."""
        while self.running:
            try:
                pending_perms = await self.opencode.get_pending_permissions()
                for perm in pending_perms:
                    sid = perm.get("sessionID")
                    if sid in self.session_to_user:
                        await self._dispatch_permission(perm)

                pending_ques = await self.opencode.get_pending_questions()
                for que in pending_ques:
                    sid = que.get("sessionID")
                    if sid in self.session_to_user:
                        await self._dispatch_question(que)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Error in Bale failsafe poller: %s", e)
            await asyncio.sleep(2.0)

    async def _dispatch_permission(self, props: Dict[str, Any]):
        perm_id = props.get("id") or props.get("permissionID")
        if not perm_id or perm_id in self.pending_permissions:
            return

        session_id = props.get("sessionID")
        user_id = self.session_to_user.get(session_id)
        if not user_id:
            return

        self.pending_permissions[perm_id] = {
            "session_id": session_id,
            "user_id": user_id,
            "props": props
        }

        p_type = props.get("permission") or props.get("type", "دسترسی")
        title = props.get("title", "")
        metadata = props.get("metadata", {})
        filepath = metadata.get("filepath", "")

        inline_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ تایید یک‌باره", "callback_data": f"perm:once:{perm_id}"},
                    {"text": "🔄 همیشه مجاز", "callback_data": f"perm:always:{perm_id}"}
                ],
                [
                    {"text": "❌ رد درخواست", "callback_data": f"perm:reject:{perm_id}"}
                ]
            ]
        }

        card_text = (
            "⚠️ *درخواست مجوز دسترسی سیستم*\n\n"
            f"• *نوع دسترسی:* `{p_type}`\n"
            f"• *عنوان:* `{title or 'اجرای دستور'}`\n"
            + (f"• *مسیر:* `{filepath}`\n" if filepath else "") +
            "\nآیا اجازه اجرای این عملیات را می‌دهید؟"
        )

        msg = await self.bale.send_message(chat_id=user_id, text=card_text, reply_markup=inline_keyboard)
        if msg.get("ok"):
            self.pending_permissions[perm_id]["msg_id"] = msg["result"]["message_id"]

    async def _dispatch_question(self, props: Dict[str, Any]):
        que_id = props.get("id") or props.get("requestID")
        if not que_id or que_id in self.pending_questions:
            return

        session_id = props.get("sessionID")
        user_id = self.session_to_user.get(session_id)
        if not user_id:
            return

        questions = props.get("questions", [])
        if not questions:
            return

        q_item = questions[0]
        q_text = q_item.get("question", "لطفاً یکی از گزینه‌های زیر را انتخاب کنید:")
        options = q_item.get("options", [])

        self.pending_questions[que_id] = {
            "session_id": session_id,
            "user_id": user_id,
            "props": props,
            "options": options
        }

        keyboard_rows = []
        for i, opt in enumerate(options):
            label = opt.strip()
            if len(label) > 35:
                label = label[:32] + "..."
            keyboard_rows.append([{"text": f"🔹 {label}", "callback_data": f"que:ans:{que_id}:{i}"}])

        card_text = f"❓ *پرسش دستیار هوشمند:*\n\n{q_text}\n\nلطفاً پاسخ مورد نظر را انتخاب نمایید:"
        msg = await self.bale.send_message(chat_id=user_id, text=card_text, reply_markup={"inline_keyboard": keyboard_rows})
        if msg.get("ok"):
            self.pending_questions[que_id]["msg_id"] = msg["result"]["message_id"]

    async def process_user_prompt(self, chat_id: int, user_id: int, text: str, msg_id: Optional[int] = None):
        """Dispatches user prompt to OpenCode and sets up live streaming session."""
        cur_dir = self.get_user_workspace(user_id)

        init_res = await self.bale.send_message(
            chat_id=chat_id,
            text="⏳ *در حال تحلیل و بررسی درخواست...*",
            reply_to_message_id=msg_id
        )
        if not init_res.get("ok"):
            logger.error("Failed to send initial message to Bale chat %s: %s", chat_id, init_res)
            return

        bot_msg_id = init_res["result"]["message_id"]
        session_id = await self.get_or_create_session(user_id)

        async def on_dir_change(new_d: str):
            self.set_user_workspace(user_id, new_d)

        stream = BaleStreamSession(
            chat_id=chat_id,
            initial_message_id=bot_msg_id,
            bale=self.bale,
            user_message_id=msg_id,
            workspace_dir=cur_dir,
            on_workdir_change=on_dir_change
        )
        self.active_streams[session_id] = stream

        if memory_manager.is_memory_query(text):
            mem_ctx = memory_manager.build_memory_context()
            full_system = f"{BALE_AGENT_SYSTEM_PROMPT}\n\n{mem_ctx}"
        else:
            full_system = BALE_AGENT_SYSTEM_PROMPT

        model_cfg = self.get_user_model(user_id)
        m_provider = model_cfg.get("providerID", "opencode")
        m_id = model_cfg.get("modelID", "ling-3.0-flash-fin-free")
        agent_name = self.user_agents.get(user_id, "build")

        try:
            await self.opencode.switch_session_model(session_id, m_provider, m_id)
        except Exception:
            pass

        try:
            await self.opencode.send_prompt(
                session_id=session_id,
                prompt_text=text,
                model_provider=m_provider,
                model_id=m_id,
                agent=agent_name,
                directory=cur_dir,
                system=full_system
            )
        except Exception as e:
            logger.exception("Error dispatching prompt to OpenCode: %s", e)
            self.active_streams.pop(session_id, None)
            stream.cancel()
            await self.bale.edit_message_text(
                chat_id,
                bot_msg_id,
                f"❌ *خطا در ارسال درخواست به OpenCode:*\n`{e}`"
            )

    async def handle_callback_query(self, cb: Dict[str, Any]):
        """Handles interactive inline keyboard buttons in Bale."""
        query_id = cb.get("id")
        user_id = cb.get("from", {}).get("id")
        message = cb.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        data = cb.get("data", "")

        if not self.is_user_authorized(user_id):
            await self.bale.answer_callback_query(query_id, "⛔ دسترسی غیرمجاز.", show_alert=True)
            return

        if data.startswith("perm:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                action, perm_id = parts[1], parts[2]
                decision = "always" if action == "always" else ("reject" if action == "reject" else "once")
                await self.opencode.respond_permission(perm_id, decision)
                self.pending_permissions.pop(perm_id, None)
                label = "✅ همیشه مجاز" if decision == "always" else ("✅ تایید یک‌باره" if decision == "once" else "❌ رد شد")
                await self.bale.answer_callback_query(query_id, f"دسترسی: {label}")
                await self.bale.edit_message_text(
                    chat_id,
                    message_id,
                    f"⚙️ *نتیجه درخواست مجوز:*\nوضعیت: *{label}*"
                )

        elif data.startswith("que:ans:"):
            parts = data.split(":", 3)
            if len(parts) == 4:
                que_id, idx_str = parts[2], parts[3]
                que_data = self.pending_questions.pop(que_id, None)
                if que_data and idx_str.isdigit():
                    idx = int(idx_str)
                    opts = que_data.get("options", [])
                    selected = opts[idx] if 0 <= idx < len(opts) else "انتخاب شد"
                    await self.opencode.respond_question(que_id, [selected])
                    await self.bale.answer_callback_query(query_id, f"✅ ثبت شد: {selected}")
                    await self.bale.edit_message_text(
                        chat_id,
                        message_id,
                        f"{message.get('text', '')}\n\n➖➖➖➖➖➖\n✅ *گزینه انتخابی:* {selected}"
                    )

        elif data.startswith("model:set:"):
            parts = data.split(":", 3)
            if len(parts) == 4:
                prov_id, mod_id = parts[2], parts[3]
                self.user_models[user_id] = {"providerID": prov_id, "modelID": mod_id}
                sid = await self.get_or_create_session(user_id)
                await self.opencode.switch_session_model(sid, prov_id, mod_id)
                await self.bale.answer_callback_query(query_id, f"✅ مدل فعال: {mod_id}")
                await self.bale.edit_message_text(
                    chat_id,
                    message_id,
                    f"🤖 *مدل هوش مصنوعی با موفقیت تنظیم شد:*\n\n"
                    f"• *سرویس‌دهنده:* `{prov_id}`\n"
                    f"• *مدل:* `{mod_id}`\n\n"
                    f"از این پس دستورات با این مدل پردازش می‌شوند."
                )

        elif data.startswith("agent:set:"):
            target_agent = data.split(":", 2)[2]
            self.user_agents[user_id] = target_agent
            await self.bale.answer_callback_query(query_id, f"🧠 ایجنت: {target_agent}")
            agent_fa = {
                "build": "Build (کدنویسی و ترمینال)",
                "plan": "Plan (طراحی و بدون ویرایش)",
                "explore": "Explore (کاوش سریع در کد)",
                "general": "General (وظایف عمومی)"
            }
            title = agent_fa.get(target_agent, target_agent)
            await self.bale.edit_message_text(
                chat_id,
                message_id,
                f"✅ *حالت ایجنت فعال به `{title}` تغییر یافت.*"
            )

        elif data == "agent:abort":
            await self.bale.answer_callback_query(query_id, "در حال توقف...")
            await self.abort_user_agent(chat_id, user_id)

        elif data.startswith("cd:to:"):
            target_dir = data.split(":", 2)[2]
            if os.path.isdir(target_dir):
                self.set_user_workspace(user_id, target_dir)
                await self.bale.answer_callback_query(query_id, f"مسیر: {os.path.basename(target_dir)}")
                await self.bale.edit_message_text(
                    chat_id,
                    message_id,
                    f"📂 *مسیر کاری به پوشه زیر تغییر یافت:*\n`{target_dir}`"
                )
            else:
                await self.bale.answer_callback_query(query_id, "❌ پوشه یافت نشد.", show_alert=True)

        elif data.startswith("file:dl:"):
            fpath = data.split(":", 2)[2]
            cur_dir = self.get_user_workspace(user_id)
            full_path = fpath if os.path.isabs(fpath) else os.path.join(cur_dir, fpath)
            if os.path.exists(full_path) and os.path.isfile(full_path):
                await self.bale.answer_callback_query(query_id, "در حال ارسال فایل...")
                await self.bale.send_document(chat_id, full_path, caption=f"📁 `{os.path.basename(full_path)}`")
            else:
                await self.bale.answer_callback_query(query_id, "❌ فایل پیدا نشد.", show_alert=True)

    async def handle_message(self, message: Dict[str, Any]):
        """Handles incoming user messages (text, commands, documents, photos, audio)."""
        chat_id = message.get("chat", {}).get("id")
        user_id = message.get("from", {}).get("id")
        msg_id = message.get("message_id")
        text = message.get("text", "").strip()

        if not user_id or not chat_id:
            return

        if not self.is_user_authorized(user_id):
            await self.bale.send_message(
                chat_id,
                f"⛔ *دسترسی غیرمجاز!*\nشناسه کاربری شما: `{user_id}`\nلطفاً این شناسه را در فایل `.env` پروژه در متغیر `BALE_ALLOWED_USERS` قرار دهید."
            )
            return

        if any(k in message for k in ("document", "photo", "voice", "audio", "video")):
            await self.handle_file_upload(message)
            return

        if not text:
            return

        text_lower = text.lower()
        if text == "➕ سشن جدید":
            await self.handle_command(chat_id, user_id, "/new", msg_id)
            return
        elif text == "🤖 مدل‌ها":
            await self.handle_command(chat_id, user_id, "/model", msg_id)
            return
        elif text == "🧠 ایجنت":
            await self.handle_command(chat_id, user_id, "/agent", msg_id)
            return
        elif text in ("📂 مسیر کاری (/cd)", "/cd"):
            await self.handle_command(chat_id, user_id, "/cd", msg_id)
            return
        elif text in ("🛑 توقف (/stop)", "/stop"):
            await self.abort_user_agent(chat_id, user_id)
            return
        elif text in ("💾 حافظه (/memory)", "/memory"):
            await self.handle_command(chat_id, user_id, "/memory", msg_id)
            return
        elif text == "📝 تغییرات گیت":
            await self.handle_command(chat_id, user_id, "/git", msg_id)
            return
        elif text in ("💻 ترمینال (/sh)", "/sh"):
            await self.bale.send_message(
                chat_id,
                "💻 *اجرای دستور شل در سیستم:*\nبرای اجرای هر دستوری کافیست از پیشوند `/sh` استفاده کنید.\n\nمثال:\n`/sh ls -la`\n`/sh df -h`\n`/sh git status`",
                reply_markup=BALE_MAIN_REPLY_KEYBOARD
            )
            return
        elif text in ("❓ راهنما", "/help"):
            await self.handle_command(chat_id, user_id, "/help", msg_id)
            return

        if text.startswith("/"):
            await self.handle_command(chat_id, user_id, text, msg_id)
            return

        if self.is_agent_running(user_id):
            await self.bale.send_message(
                chat_id,
                "⚠️ *ایجنت در حال حاضر مشغول اجرای دستور قبلی است.*\nلطفاً تا پایان عملیات صبر کنید یا با دستور `/stop` آن را متوقف نمایید.",
                reply_to_message_id=msg_id,
                reply_markup={
                    "inline_keyboard": [[{"text": "⏹ توقف فوری عملیات (/stop)", "callback_data": "agent:abort"}]]
                }
            )
            return

        cur_dir = self.get_user_workspace(user_id)

        dir_intent = detect_directory_intent(text, cur_dir)
        if dir_intent:
            target_dir, remainder_task = dir_intent
            if os.path.isdir(target_dir):
                self.set_user_workspace(user_id, target_dir)
                if remainder_task:
                    await self.bale.send_message(
                        chat_id,
                        f"📂 *مسیر کاری تغییر یافت به:* `{target_dir}`\n🚀 *در حال انجام تسک:* «{remainder_task}»..."
                    )
                    await self.process_user_prompt(chat_id, user_id, remainder_task, msg_id)
                else:
                    await self.bale.send_message(
                        chat_id,
                        f"📂 *مسیر کاری با موفقیت تغییر یافت:*\n`{target_dir}`",
                        reply_markup=BALE_MAIN_REPLY_KEYBOARD
                    )
                return

        file_query = detect_file_download_intent(text)
        if file_query:
            cand = find_candidate_file(file_query, cur_dir)
            if cand and os.path.exists(cand) and os.path.isfile(cand):
                await self.bale.send_chat_action(chat_id, "upload_document")
                ext = os.path.splitext(cand)[1].lower()
                caption = f"📎 فایل درخواستی شما: `{os.path.basename(cand)}`"
                if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                    await self.bale.send_photo(chat_id, cand, caption=caption, reply_to_message_id=msg_id)
                else:
                    await self.bale.send_document(chat_id, cand, caption=caption, reply_to_message_id=msg_id)
                return

        await self.process_user_prompt(chat_id, user_id, text, msg_id)

    async def handle_file_upload(self, message: Dict[str, Any]):
        """Processes file uploads from Bale chat and passes them to OpenCode."""
        chat_id = message.get("chat", {}).get("id")
        user_id = message.get("from", {}).get("id")
        msg_id = message.get("message_id")
        caption = message.get("caption", "").strip()

        if self.is_agent_running(user_id):
            await self.bale.send_message(
                chat_id,
                "⚠️ *ایجنت در حال حاضر مشغول اجرای دستور دیگری است.* لطفاً ابتدا `/stop` را بزنید.",
                reply_to_message_id=msg_id
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

        if not file_id:
            return

        await self.bale.send_chat_action(chat_id, "upload_document")
        file_info = await self.bale.get_file(file_id)
        if not file_info.get("ok"):
            await self.bale.send_message(chat_id, "❌ خطا در دریافت اطلاعات فایل از سرور بله.")
            return

        file_path_on_bale = file_info["result"]["file_path"]
        cur_dir = self.get_user_workspace(user_id)
        dest_dir = os.path.join(cur_dir, "uploads")
        dest_path = os.path.join(dest_dir, file_name)

        success = await self.bale.download_file(file_path_on_bale, dest_path)
        if not success:
            await self.bale.send_message(chat_id, "❌ خطا در دانلود و ذخیره فایل.")
            return

        if is_voice:
            prompt_text = (
                f"کاربر یک پیام صوتی ارسال کرده است.\n"
                f"فایل صوتی در مسیر زیر ذخیره شده است: `{dest_path}`\n"
                f"{caption if caption else 'لطفاً فایل را پردازش و بررسی کنید.'}"
            )
        else:
            prompt_text = (
                f"کاربر فایل `{file_name}` را ارسال کرد.\n"
                f"مسیر ذخیره فایل در سیستم: `{dest_path}`\n"
                f"{caption if caption else 'لطفاً این فایل را بررسی کنید.'}"
            )

        await self.process_user_prompt(chat_id, user_id, prompt_text, msg_id)

    async def handle_command(self, chat_id: int, user_id: int, text: str, msg_id: Optional[int]):
        """Dispatches slash commands."""
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        cur_dir = self.get_user_workspace(user_id)

        if cmd == "/start":
            welcome = (
                "🤖 *به دستیار هوشمند OpenCode در بله خوش آمدید!*\n\n"
                "من دستیار تمام‌عیار برنامه‌نویسی و اتوماسیون لینوکس هستم که به سرور OpenCode متصل شده‌ام.\n\n"
                f"📁 *پوشه کاری فعال:* `{cur_dir}`\n\n"
                "✨ *امکانات اصلی:*\n"
                "• کدنویسی، رفع باگ و ایجاد پروژه‌ها به زبان‌های مختلف\n"
                "• اجرای ایمن دستورات ترمینال سیستم با پیشوند `/sh`\n"
                "• جستجو، مشاهده و ارسال فایل‌ها به چت\n"
                "• مدیریت شاخه‌های گیت، لاگ‌ها و Diff\n"
                "• تغییر مدل هوش مصنوعی (`/model`) و ایجنت (`/agent`)\n"
                "• حافظه پایدار شخصی (`/memory`)\n\n"
                "کافیست دستور یا سوال خود را به فارسی یا انگلیسی بنویسید!"
            )
            await self.bale.send_message(chat_id, welcome, reply_markup=BALE_MAIN_REPLY_KEYBOARD)

        elif cmd == "/help":
            help_text = (
                "📖 *راهنمای دستورات و کلیدهای دستیار OpenCode:*\n\n"
                "➕ `/new` : ایجاد سشن جدید و پاکسازی سابقه\n"
                "🤖 `/model` : انتخاب و تغییر مدل هوش مصنوعی\n"
                "🧠 `/agent` : انتخاب ایجنت (Build, Plan, Explore, General)\n"
                "📂 `/cd [مسیر]` : تغییر پوشه کاری به آدرس دلخواه\n"
                "📁 `/pwd` : مشاهده پوشه کاری جاری و فایل‌های آن\n"
                "🛑 `/stop` : توقف و لغو فوری پردازش جاری\n"
                "💻 `/sh [دستور]` : اجرای مستقیم دستور در ترمینال سیستم\n"
                "📄 `/cat [فایل]` : نمایش محتوای یک فایل متنی در چت\n"
                "📎 `/download [فایل]` : دانلود و ارسال مستقیم فایل به بله\n"
                "📝 `/git` : مشاهده وضعیت فایل‌های تغییریافته گیت\n"
                "🔍 `/diff` : مشاهده کامل خطوط کدهای تغییریافته\n"
                "📋 `/todo` : بررسی چک‌لیست برنامه‌ها و وظایف ایجنت\n"
                "💾 `/memory` : مشاهده و مدیریت حافظه پایدار شخصی\n"
                "📌 `/remember [متن]` : افزودن نکته جدید به حافظه دائم\n"
                "⚡ `/status` : وضعیت سلامت سرور و ارتباط با هوش مصنوعی\n"
                "👤 `/whoami` : مشاهده شناسه و سطح دسترسی شما"
            )
            await self.bale.send_message(chat_id, help_text, reply_markup=BALE_MAIN_REPLY_KEYBOARD)

        elif cmd == "/whoami":
            info_text = (
                "👤 *اطلاعات شناسنامه کاربر:*\n\n"
                f"• *شناسه عددی (User ID):* `{user_id}`\n"
                f"• *شناسه چت (Chat ID):* `{chat_id}`\n"
                f"• *پوشه کاری فعال:* `{cur_dir}`\n"
                f"• *وضعیت دسترسی:* ✅ مجاز (Authorized)"
            )
            await self.bale.send_message(chat_id, info_text, reply_markup=BALE_MAIN_REPLY_KEYBOARD)

        elif cmd == "/new":
            session = await self.opencode.create_session(title=f"Bale Session {user_id}", directory=cur_dir)
            new_sid = session.get("id") or str(session)
            self.user_sessions[user_id] = new_sid
            self.session_to_user[new_sid] = user_id
            await self.bale.send_message(
                chat_id,
                f"✨ *سشن جدید با موفقیت ایجاد شد!*\nشناسه سشن: `{new_sid}`\nتاریخچه مکالمه قبلی ریست شد.",
                reply_markup=BALE_MAIN_REPLY_KEYBOARD
            )

        elif cmd == "/stop":
            await self.abort_user_agent(chat_id, user_id)

        elif cmd == "/model":
            providers_data = await self.opencode.get_providers()
            providers = providers_data.get("providers", []) if isinstance(providers_data, dict) else (providers_data if isinstance(providers_data, list) else [])
            current_model = self.get_user_model(user_id).get("modelID", "ling-3.0-flash-fin-free")

            buttons = []
            for p in providers:
                p_id = p.get("id", "opencode")
                models = p.get("models", {})
                if isinstance(models, dict):
                    model_items = list(models.items())
                elif isinstance(models, list):
                    model_items = [(m.get("id"), m) for m in models]
                else:
                    model_items = []

                for m_id, m_data in model_items:
                    m_name = m_data.get("name", m_id) if isinstance(m_data, dict) else m_id
                    is_active = (m_id == current_model)
                    btn_text = f"✓ {m_name[:25]}" if is_active else f"🤖 {m_name[:25]}"
                    cb_data = f"model:set:{p_id}:{m_id}"
                    buttons.append([{"text": btn_text, "callback_data": cb_data}])

            if not buttons:
                await self.bale.send_message(chat_id, "⚠️ هیچ مدلی یافت نشد.")
                return

            await self.bale.send_message(
                chat_id,
                f"🤖 *انتخاب مدل هوش مصنوعی (Model Picker)*\n\n"
                f"• *مدل فعال:* `{current_model}`\n\n"
                f"روی یکی از مدل‌های زیر کلیک کنید تا برای این گفتگو فعال شود:",
                reply_markup={"inline_keyboard": buttons[:10]}
            )

        elif cmd == "/agent":
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🛠 Build (کدنویسی، ابزار، ترمینال)", "callback_data": "agent:set:build"}],
                    [{"text": "📋 Plan (طراحی و بدون ویرایش فایل)", "callback_data": "agent:set:plan"}],
                    [{"text": "🔍 Explore (کاوش و بررسی کد)", "callback_data": "agent:set:explore"}],
                    [{"text": "🌐 General (وظایف عمومی سیستم)", "callback_data": "agent:set:general"}]
                ]
            }
            cur_agent = self.user_agents.get(user_id, "build")
            await self.bale.send_message(
                chat_id,
                f"🧠 *انتخاب ایجنت کاری OpenCode:*\nحالت فعلی: *{cur_agent}*\n\nحالت مورد نظر خود را انتخاب نمایید:",
                reply_markup=keyboard
            )

        elif cmd == "/cd":
            if args:
                target = resolve_path(args, cur_dir)
                if os.path.isdir(target):
                    self.set_user_workspace(user_id, target)
                    await self.bale.send_message(
                        chat_id,
                        f"📂 *مسیر کاری با موفقیت تغییر یافت:*\n`{target}`",
                        reply_markup=BALE_MAIN_REPLY_KEYBOARD
                    )
                else:
                    await self.bale.send_message(chat_id, f"❌ پوشه‌ای در این مسیر پیدا نشد: `{target}`")
            else:
                subdirs = []
                try:
                    for entry in os.scandir(cur_dir):
                        if entry.is_dir() and not entry.name.startswith("."):
                            subdirs.append(entry.path)
                except Exception:
                    pass

                subdirs = sorted(subdirs)[:8]
                buttons = []
                parent = os.path.dirname(cur_dir)
                if parent and parent != cur_dir:
                    buttons.append([{"text": "⬆️ پوشه والد (..)", "callback_data": f"cd:to:{parent}"}])
                for sd in subdirs:
                    buttons.append([{"text": f"📁 {os.path.basename(sd)}", "callback_data": f"cd:to:{sd}"}])

                await self.bale.send_message(
                    chat_id,
                    f"📂 *مسیر فعلی:* `{cur_dir}`\nجهت جابجایی می‌توانید روی یکی از پوشه‌های زیر کلیک کنید یا دستور `/cd <مسیر>` را بفرستید:",
                    reply_markup={"inline_keyboard": buttons}
                )

        elif cmd == "/pwd":
            items = []
            try:
                for entry in os.scandir(cur_dir):
                    prefix = "📁" if entry.is_dir() else "📄"
                    items.append(f"{prefix} `{entry.name}`")
            except Exception as e:
                items.append(f"خطا در خواندن فایل‌ها: {e}")

            preview = "\n".join(sorted(items)[:20])
            total_count = len(items)
            res_text = (
                f"📂 *مسیر کاری فعلی:*\n`{cur_dir}`\n\n"
                f"📋 *فایل‌ها و پوشه‌ها ({total_count} مورد):*\n"
                f"{preview}"
            )
            await self.bale.send_message(chat_id, res_text, reply_markup=BALE_MAIN_REPLY_KEYBOARD)

        elif cmd == "/git":
            proc = await asyncio.create_subprocess_shell(
                "git status -s",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cur_dir
            )
            stdout, _ = await proc.communicate()
            out = stdout.decode("utf-8").strip()
            if out:
                msg = f"📝 *وضعیت فایل‌های گیت:*\n\n```\n{out}\n```\nجهت مشاهده جزئیات از دستور `/diff` استفاده کنید."
            else:
                msg = "✅ *هیچ تغییر اعمال‌نشده‌ای در گیت وجود ندارد (Working tree clean).*"
            await self.bale.send_message(chat_id, msg, reply_markup=BALE_MAIN_REPLY_KEYBOARD)

        elif cmd == "/diff":
            proc = await asyncio.create_subprocess_shell(
                "git diff --stat && git diff",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cur_dir
            )
            stdout, _ = await proc.communicate()
            out = stdout.decode("utf-8").strip()
            if out:
                if len(out) > 3500:
                    out = out[:3450] + "\n... (بقیه تغییرات به دلیل حجم بالا نمایش داده نشد)"
                msg = f"🔍 *تغییرات کد (Git Diff):*\n\n```diff\n{out}\n```"
            else:
                msg = "ℹ️ تغییری برای نمایش Diff وجود ندارد."
            await self.bale.send_message(chat_id, msg, reply_markup=BALE_MAIN_REPLY_KEYBOARD)

        elif cmd == "/todo":
            sid = self.user_sessions.get(user_id)
            if not sid:
                await self.bale.send_message(chat_id, "ℹ️ سشن فعالی یافت نشد. ابتدا پیامی ارسال کنید.")
                return
            todos = await self.opencode.get_session_todo(sid, directory=cur_dir)
            if todos and isinstance(todos, list):
                lines = []
                for t in todos:
                    status = "✅" if t.get("completed") else "⏳"
                    content = t.get("content", "")
                    lines.append(f"{status} {content}")
                await self.bale.send_message(chat_id, "📋 *چک‌لیست تسک‌های سشن:*\n\n" + "\n".join(lines))
            else:
                await self.bale.send_message(chat_id, "ℹ️ تسک فعالی در این سشن ثبت نشده است.")

        elif cmd == "/sh":
            if not args:
                await self.bale.send_message(chat_id, "⚠️ لطفاً دستور مورد نظر را بعد از `/sh` وارد کنید.\nمثال: `/sh ls -la`")
                return

            await self.bale.send_chat_action(chat_id, "typing")
            try:
                proc = await asyncio.create_subprocess_shell(
                    args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cur_dir
                )
                stdout, stderr = await proc.communicate()
                out = stdout.decode("utf-8", errors="replace").strip()
                err = stderr.decode("utf-8", errors="replace").strip()
                code = proc.returncode

                status_emoji = "✅" if code == 0 else "❌"
                body = []
                if out:
                    body.append(f"📄 *خروجی استاندارد:*\n```\n{out[:3000]}\n```")
                if err:
                    body.append(f"⚠️ *پیام‌های خطا:*\n```\n{err[:1000]}\n```")
                if not body:
                    body.append("ℹ️ دستور بدون خروجی متنی پایان یافت.")

                resp_text = f"{status_emoji} *اجرای دستور:* `{args}` (کد خروج: {code})\n\n" + "\n\n".join(body)
                await self.bale.send_message(chat_id, resp_text)
            except Exception as e:
                await self.bale.send_message(chat_id, f"❌ خطا در اجرای دستور: `{e}`")

        elif cmd == "/cat":
            if not args:
                await self.bale.send_message(chat_id, "⚠️ فرمت دستور: `/cat <مسیر فایل>`")
                return
            cand = find_candidate_file(args, cur_dir) or resolve_path(args, cur_dir)
            if cand and os.path.exists(cand) and os.path.isfile(cand):
                try:
                    with open(cand, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(3500)
                    await self.bale.send_message(
                        chat_id,
                        f"📄 *محتوای فایل:* `{os.path.basename(cand)}`\n\n```\n{content}\n```"
                    )
                except Exception as e:
                    await self.bale.send_message(chat_id, f"❌ خطا در خواندن فایل: `{e}`")
            else:
                await self.bale.send_message(chat_id, f"❌ فایل یافت نشد: `{args}`")

        elif cmd == "/download":
            if not args:
                await self.bale.send_message(chat_id, "⚠️ فرمت دستور: `/download <مسیر فایل>`")
                return
            cand = find_candidate_file(args, cur_dir) or resolve_path(args, cur_dir)
            if cand and os.path.exists(cand) and os.path.isfile(cand):
                await self.bale.send_chat_action(chat_id, "upload_document")
                ext = os.path.splitext(cand)[1].lower()
                caption = f"📎 `{os.path.basename(cand)}`"
                if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                    await self.bale.send_photo(chat_id, cand, caption=caption, reply_to_message_id=msg_id)
                else:
                    await self.bale.send_document(chat_id, cand, caption=caption, reply_to_message_id=msg_id)
            else:
                await self.bale.send_message(chat_id, f"❌ فایل یافت نشد: `{args}`")

        elif cmd == "/memory":
            prof = memory_manager.get_user_profile()
            sys_mem = memory_manager.get_system_memory()
            soul = memory_manager.get_soul()
            mem_text = (
                "💾 *حافظه پایدار دستیار (Hermes Memory):*\n\n"
                f"👤 *مشخصات کاربر:*\n{prof}\n\n"
                f"🧠 *مرام‌نامه و لحن (Soul):*\n{soul}\n\n"
                f"📝 *نکات ثبت‌شده:*\n{sys_mem or '(موردی ثبت نشده)'}\n\n"
                "جهت افزودن نکته جدید: `/remember <متن>`"
            )
            await self.bale.send_message(chat_id, mem_text)

        elif cmd == "/remember":
            if not args:
                await self.bale.send_message(chat_id, "⚠️ فرمت دستور: `/remember <نکته برای ذخیره>`")
                return
            ok = memory_manager.add_memory_note(args, target="memory")
            if ok:
                await self.bale.send_message(chat_id, f"✅ با موفقیت به حافظه دائم افزوده شد:\n«{args}»")
            else:
                await self.bale.send_message(chat_id, "❌ خطا در ذخیره نکته در حافظه.")

        elif cmd == "/status":
            alive = await self.opencode.check_server_alive()
            sid = self.user_sessions.get(user_id, "بدون سشن")
            status_text = (
                "⚡ *وضعیت سیستم و سرور OpenCode:*\n\n"
                f"• *وضعیت سرور:* {'🟢 آنلاین' if alive else '🔴 آفلاین'}\n"
                f"• *آدرس سرور:* `{config.OPENCODE_SERVER_URL}`\n"
                f"• *سشن فعال شما:* `{sid}`\n"
                f"• *پوشه کاری جاری:* `{cur_dir}`\n"
                f"• *ربات بله:* @{self.bot_username} (ID: {self.bot_id})"
            )
            await self.bale.send_message(chat_id, status_text)

        else:
            await self.bale.send_message(
                chat_id,
                f"❓ دستور `{cmd}` ناشناخته است. جهت مشاهده لیست دستورات `/help` را بفرستید."
            )

    async def poll_updates(self):
        """Long polling updates loop for Bale Bot API."""
        offset: Optional[int] = None
        consecutive_errors = 0

        while self.running:
            try:
                res = await self.bale.get_updates(offset=offset, limit=50, timeout=20)
                if not res.get("ok"):
                    consecutive_errors += 1
                    err_desc = res.get("description", "Unknown error")
                    logger.warning("Bale getUpdates returned error: %s (count: %d)", err_desc, consecutive_errors)
                    await asyncio.sleep(min(15, consecutive_errors * 2))
                    continue

                consecutive_errors = 0
                updates = res.get("result", [])

                for update in updates:
                    update_id = update.get("update_id")
                    if update_id is not None:
                        offset = update_id + 1

                    if "callback_query" in update:
                        asyncio.create_task(self.handle_callback_query(update["callback_query"]))
                    elif "message" in update:
                        asyncio.create_task(self.handle_message(update["message"]))

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                logger.exception("Unexpected error in Bale polling loop: %s", e)
                await asyncio.sleep(min(15, consecutive_errors * 2))

    async def stop(self):
        """Clean shutdown handler."""
        logger.info("Stopping Bale Bot service...")
        self.running = False
        if self.failsafe_poll_task:
            self.failsafe_poll_task.cancel()
        for sid, stream in list(self.active_streams.items()):
            stream.cancel()
        await self.bale.close()
        await self.opencode.close()
        logger.info("Bale Bot service stopped.")


async def main():
    bridge = OpenCodeBaleBridge()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _sig_handler():
        logger.info("Received termination signal, shutting down...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig_handler)
        except NotImplementedError:
            pass

    bot_task = asyncio.create_task(bridge.start())

    await stop_event.wait()
    bot_task.cancel()
    await bridge.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
