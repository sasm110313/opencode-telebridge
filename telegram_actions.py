import os
import re
import json
import logging
from pathlib import Path
from typing import Optional, Tuple, List, Dict

logger = logging.getLogger("TelegramActions")

HOME = Path.home()
HOME_STR = str(HOME)

CHANNELS_FILE = HOME / ".config" / "opencode" / "user_channels.json"

TELEGRAM_AGENT_SYSTEM_PROMPT = """[TELEGRAM AGENT ENVIRONMENT & CAPABILITIES]
You are operating as an advanced interactive coding and system assistant in a Telegram bot.
You have full access to tools (bash, file edit/read, search, etc.) and can perform real-time actions on the user's Linux machine.


[CRITICAL FORMATTING & VISUAL PRESENTATION RULES]
1. RICH EMOJI USAGE:
   - Always enrich your responses with relevant and expressive emojis!
   - Use emojis for headings, bullet points, statuses, steps, and file categories:
     • Status & Actions: 🚀, ⚡, 🧠, ⏳, ✅, ❌, ⚠️, ℹ️
     • Media & Devices: 🎥, 📹, 📸, 🎙, 🔊, 🎬, 🎵
     • Code & Terminal: 💻, 🖥, ⚙️, 🛠, 🔧, ⌨️
     • Files & Storage: 📁, 📄, 📦, 💾, 🔍, 📊, 📈
     • Highlights: 📌, 💡, ⏱, 🏷, 🎯, ✨

2. SPACED & READABLE PARAGRAPHS (MOBILE-FIRST):
   - NEVER output dense walls of text or glue multiple steps into a single continuous block!
   - NO RUN-ON NARRATION: Never write "متوجه شدم... بگذارید پیدایش کنم. پیدا شد... بگذارید بررسی کنم. پروژه Django است... حالا تستها را میخوانم." in a single paragraph.
   - ALWAYS insert a clean blank line (double newline `\n\n`) between distinct thoughts, steps, and paragraphs.
   - Separate progress notices from final results.
   - Wrap all file paths in backticks (e.g. `/path/to/file`).

3. STRUCTURED BULLET POINTS FOR STATS & SPECS:
   - Whenever you report specs, statistics, steps, file details, or lists, ALWAYS format each item on its OWN line with a bullet point and emoji. Example:
     ✅ **ضبط ویدیو با موفقیت انجام شد:**

     • ⏱ **مدت زمان:** ۶۰ ثانیه
     • 📹 **کیفیت تصویر:** 640x480
     • 🎙 **وضعیت صدا:** ضبط شد و فعال است
     • 📦 **حجم فایل:** ۱.۶ مگابایت
     • 📁 **مسیر فایل:** `/tmp/opencode/video.mp4`

4. HEADINGS & HIGHLIGHTS:
   - Use bold markdown (`**عنوان**` or `### عنوان`) to divide your responses into clear sections.

[SPECIAL NATIVE DIRECTIVES]
1. FILE SENDING / DELIVERY:
When the user asks you to send, download, record, or share a file/photo/video:
- Verify the exact file path on disk.
- Output on its own line:
  MEDIA:/absolute/path/to/file
The Telegram bot will automatically catch this line and send the actual file directly as a document/photo/video to the user!

2. CHANGING WORKING DIRECTORY:
When the user asks you to switch or navigate to a directory:
- Resolve the path.
- Output on its own line:
  WORKDIR:/absolute/path/to/directory
The bot will switch its active workspace to that directory.

3. SENDING TO TELEGRAM CHANNELS:
When the user asks you to post or broadcast a message to a Telegram channel:
- Compose the formatted post in Persian.
- Output on its own line:
  TELEGRAM_POST:@channel_username:Your message here
"""

BALE_AGENT_SYSTEM_PROMPT = """[BALE AGENT ENVIRONMENT & CAPABILITIES]
You are operating as an advanced interactive coding and system assistant in a Bale messenger bot.
You have full access to tools (bash, file edit/read, search, etc.) and can perform real-time actions on the user's Linux machine.

[CRITICAL FORMATTING & VISUAL PRESENTATION RULES]
1. RICH EMOJI USAGE:
   - Always enrich your responses with relevant and expressive emojis!
   - Use emojis for headings, bullet points, statuses, steps, and file categories:
     • Status & Actions: 🚀, ⚡, 🧠, ⏳, ✅, ❌, ⚠️, ℹ️
     • Media & Devices: 🎥, 📹, 📸, 🎙, 🔊, 🎬, 🎵
     • Code & Terminal: 💻, 🖥, ⚙️, 🛠, 🔧, ⌨️
     • Files & Storage: 📁, 📄, 📦, 💾, 🔍, 📊, 📈
     • Highlights: 📌, 💡, ⏱, 🏷, 🎯, ✨

2. SPACED & READABLE PARAGRAPHS (MOBILE-FIRST):
   - NEVER output dense walls of text or glue multiple steps into a single continuous block!
   - NO RUN-ON NARRATION: Never write "متوجه شدم... بگذارید پیدایش کنم. پیدا شد... بگذارید بررسی کنم. پروژه Django است... حالا تستها را میخوانم." in a single paragraph.
   - ALWAYS insert a clean blank line (double newline `\\n\\n`) between distinct thoughts, steps, and paragraphs.
   - Separate progress notices from final results.
   - Wrap all file paths in backticks (e.g. `/path/to/file`).

3. STRUCTURED BULLET POINTS FOR STATS & SPECS:
   - Whenever you report specs, statistics, steps, file details, or lists, ALWAYS format each item on its OWN line with a bullet point and emoji. Example:
     ✅ *ضبط ویدیو با موفقیت انجام شد:*

     • ⏱ *مدت زمان:* ۶۰ ثانیه
     • 📹 *کیفیت تصویر:* 640x480
     • 🎙 *وضعیت صدا:* ضبط شد و فعال است
     • 📦 *حجم فایل:* ۱.۶ مگابایت
     • 📁 *مسیر فایل:* `/tmp/opencode/video.mp4`

4. HEADINGS & HIGHLIGHTS:
   - Use bold markdown (`*عنوان*`) to divide your responses into clear sections.

[SPECIAL NATIVE DIRECTIVES]
1. FILE SENDING / DELIVERY:
When the user asks you to send, download, record, or share a file/photo/video:
- Verify the exact file path on disk.
- Output on its own line:
  MEDIA:/absolute/path/to/file
The Bale bot will automatically catch this line and send the actual file directly as a document/photo/video to the user!

2. CHANGING WORKING DIRECTORY:
When the user asks you to switch or navigate to a directory:
- Resolve the path.
- Output on its own line:
  WORKDIR:/absolute/path/to/directory
The bot will switch its active workspace to that directory.
"""

ALIAS_MAP = {
    "دسکتاپ": str(HOME / "Desktop"),
    "میزکار": str(HOME / "Desktop"),
    "desktop": str(HOME / "Desktop"),
    "دانلود": str(HOME / "Downloads"),
    "دانلودها": str(HOME / "Downloads"),
    "downloads": str(HOME / "Downloads"),
    "download": str(HOME / "Downloads"),
    "اسناد": str(HOME / "Documents"),
    "داکیومنت": str(HOME / "Documents"),
    "داکیومنت‌ها": str(HOME / "Documents"),
    "documents": str(HOME / "Documents"),
    "موزیک": str(HOME / "Music"),
    "آهنگ": str(HOME / "Music"),
    "آهنگ‌ها": str(HOME / "Music"),
    "music": str(HOME / "Music"),
    "ویدیو": str(HOME / "Videos"),
    "ویدیوها": str(HOME / "Videos"),
    "فیلم": str(HOME / "Videos"),
    "فیلم‌ها": str(HOME / "Videos"),
    "videos": str(HOME / "Videos"),
    "تصاویر": str(HOME / "Pictures"),
    "عکس": str(HOME / "Pictures"),
    "عکس‌ها": str(HOME / "Pictures"),
    "pictures": str(HOME / "Pictures"),
    "پروژه": str(Path.cwd()),
    "پروژه فعلی": str(Path.cwd()),
    "خانه": HOME_STR,
    "هوم": HOME_STR,
    "home": HOME_STR,
    "~": HOME_STR,
}

def resolve_path(raw: str, base_dir: Optional[str] = None) -> Optional[str]:
    """Resolve a raw path or alias into an absolute existing directory."""
    if not base_dir:
        base_dir = HOME_STR

    raw = raw.strip().strip("'\"`")
    if not raw:
        return None

    low = raw.lower()
    if low in ALIAS_MAP:
        target = ALIAS_MAP[low]
        if os.path.isdir(target):
            return target

    # Expand tilde
    expanded = os.path.expanduser(raw)
    if os.path.isdir(expanded):
        return os.path.abspath(expanded)

    # Absolute path
    if os.path.isabs(raw) and os.path.isdir(raw):
        return os.path.abspath(raw)

    # Relative to base_dir
    rel = os.path.join(base_dir, raw)
    if os.path.isdir(rel):
        return os.path.abspath(rel)

    # Relative to home
    in_home = os.path.join(HOME_STR, raw)
    if os.path.isdir(in_home):
        return os.path.abspath(in_home)

    # Search subdirectories in base_dir
    try:
        for entry in os.scandir(base_dir):
            if entry.is_dir() and entry.name.lower() == low:
                return entry.path
    except Exception:
        pass

    # Search subdirectories in home
    try:
        for entry in os.scandir(HOME_STR):
            if entry.is_dir() and entry.name.lower() == low:
                return entry.path
    except Exception:
        pass

    return None

DIR_PATTERNS = [
    r'^(?:لطفاً\s+|لطفا\s+)?(?:برو\s+(?:به|توی?)\s+(?:پوشه\s+|فولدر\s+|دایرکتوری\s+)?)(.+?)(?:\s+(?:و|سپس|بعدش)\s+(.+))?$',
    r'^(?:لطفاً\s+|لطفا\s+)?(?:میخوام\s+برم\s+(?:به|توی?)\s+(?:پوشه\s+|فولدر\s+|دایرکتوری\s+)?)(.+?)(?:\s+(?:و|سپس|بعدش)\s+(.+))?$',
    r'^(?:مسیر\s+(?:کاری\s+)?(?:ایجنت\s+)?(?:رو\s+)?(?:عوض\s+کن|تغییر\s+بده|بذار)\s+(?:به|روی)\s+)(.+?)(?:\s+(?:و|سپس|بعدش)\s+(.+))?$',
    r'^(?:تغییر\s+مسیر\s+(?:به|روی)\s+)(.+?)(?:\s+(?:و|سپس|بعدش)\s+(.+))?$',
    r'^cd\s+([^\n]+)$',
]

def detect_directory_intent(text: str, current_dir: Optional[str] = None) -> Optional[Tuple[str, Optional[str]]]:
    """
    Detects if the user wants to switch directory.
    Returns (target_directory, remainder_prompt) or None.
    """
    if not current_dir:
        current_dir = HOME_STR

    clean_text = text.strip()
    for pat in DIR_PATTERNS:
        m = re.match(pat, clean_text, re.IGNORECASE)
        if m:
            raw_target = m.group(1).strip()
            remainder = m.group(2).strip() if len(m.groups()) > 1 and m.group(2) else None
            resolved = resolve_path(raw_target, current_dir)
            if resolved:
                return (resolved, remainder)
    return None

FILE_DOWNLOAD_PATTERNS = [
    r'^(?:لطفاً\s+|لطفا\s+|میشه\s+)?فایل\s+(.+?)\s+رو\s+(?:میخوام\s+)?(?:برام\s+)?(?:بفرستی|ارسال\s+کنی|دانلود\s+کنم|بفرست|ارسال\s+کن)(?:\s+برام)?$',
    r'^(?:لطفاً\s+|لطفا\s+)?(?:دانلود|ارسال|فرستادن)\s+فایل\s+(.+)$',
    r'^(?:لطفاً\s+|لطفا\s+)?فایل\s+(.+?)\s+(?:رو\s+)?(?:برام\s+)?(?:بفرست|ارسال\s+کن)(?:\s+برام)?$',
]

def detect_file_download_intent(text: str) -> Optional[str]:
    """Detects if user is asking to send/download a specific file."""
    clean_text = text.strip()
    for pat in FILE_DOWNLOAD_PATTERNS:
        m = re.match(pat, clean_text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None

def find_candidate_file(query: str, workspace_dir: str) -> Optional[str]:
    """Locate candidate file in workspace or system."""
    q = query.strip().strip("'\"`")
    low = q.lower()

    # Common semantic shortcuts
    if low in ("کانفیگ", "کانفیگ ها", "تنظیمات", "تنظیمات بات", "config", "configuration"):
        for cand in ["config.py", ".env", "opencode.jsonc"]:
            p = os.path.join(workspace_dir, cand)
            if os.path.exists(p) and os.path.isfile(p):
                return os.path.abspath(p)
        p_cfg = HOME / ".config" / "opencode" / "opencode.jsonc"
        if p_cfg.exists():
            return str(p_cfg)

    # Direct match in workspace_dir
    direct = os.path.join(workspace_dir, q)
    if os.path.exists(direct) and os.path.isfile(direct):
        return os.path.abspath(direct)

    # Absolute path
    if os.path.isabs(q) and os.path.exists(q) and os.path.isfile(q):
        return os.path.abspath(q)

    # Expand user
    expanded = os.path.expanduser(q)
    if os.path.exists(expanded) and os.path.isfile(expanded):
        return os.path.abspath(expanded)

    # Search in workspace_dir
    try:
        for root, dirs, files in os.walk(workspace_dir):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".venv")]
            for f in files:
                if f.lower() == low or f.lower() == f"{low}.py" or f.lower() == f"{low}.txt":
                    return os.path.join(root, f)
        for root, dirs, files in os.walk(workspace_dir):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".venv")]
            for f in files:
                if low in f.lower():
                    return os.path.join(root, f)
    except Exception:
        pass

    return None

CHANNEL_POST_PATTERNS = [
    r'^(?:لطفاً\s+|لطفا\s+)?(?:ارسال\s+به|بفرست\s+به|پست\s+کن\s+توی?)\s+کانال\s+(@\w+|-\d+)\s*[:：]?\s*(.+)$',
    r'^(?:لطفاً\s+|لطفا\s+)?(?:توی?|در)\s+کانال\s+(@\w+|-\d+)\s+(?:این\s+پیام\s+رو\s+)?(?:ارسال\s+کن|بفرست|پست\s+کن)\s*[:：]?\s*(.+)$',
]

def detect_channel_post_intent(text: str) -> Optional[Tuple[str, str]]:
    """Detects if user asks to directly post message to a channel."""
    clean_text = text.strip()
    for pat in CHANNEL_POST_PATTERNS:
        m = re.match(pat, clean_text, re.DOTALL | re.IGNORECASE)
        if m:
            channel_target = m.group(1).strip()
            message_body = m.group(2).strip()
            return (channel_target, message_body)
    return None

def load_user_channels() -> Dict[str, str]:
    if CHANNELS_FILE.exists():
        try:
            return json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read user channels: %s", e)
    return {}

def save_user_channels(channels: Dict[str, str]):
    try:
        CHANNELS_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHANNELS_FILE.write_text(json.dumps(channels, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error("Could not save user channels: %s", e)
