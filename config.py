import os
import sys
from pathlib import Path

# Load .env file manually if python-dotenv is not installed
def load_env(env_path: Path):
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v

env_file = Path(__file__).resolve().parent / ".env"
load_env(env_file)

# Telegram Bot Token from @BotFather
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Allowed numeric Telegram user IDs (comma-separated, e.g. "123456789,987654321")
raw_allowed = os.getenv("TELEGRAM_ALLOWED_USERS", "").strip()
TELEGRAM_ALLOWED_USERS = set()
if raw_allowed:
    for uid in raw_allowed.split(","):
        uid = uid.strip()
        if uid.isdigit():
            TELEGRAM_ALLOWED_USERS.add(int(uid))

# Bale Bot Token (from @BotFather on Bale messenger)
BALE_BOT_TOKEN = os.getenv("BALE_BOT_TOKEN", "").strip()

# Allowed numeric Bale user IDs (comma-separated, e.g. "123456789,987654321")
raw_bale_allowed = os.getenv("BALE_ALLOWED_USERS", "").strip()
BALE_ALLOWED_USERS = set()
if raw_bale_allowed:
    for uid in raw_bale_allowed.split(","):
        uid = uid.strip()
        if uid.isdigit():
            BALE_ALLOWED_USERS.add(int(uid))

BALE_API_BASE_URL = os.getenv("BALE_API_BASE_URL", "https://tapi.bale.ai").rstrip("/")
BALE_PROXY = os.getenv("BALE_PROXY", "").strip() or None
BALE_STREAM_THROTTLE_MS = int(os.getenv("BALE_STREAM_THROTTLE_MS", "2200"))

# Add ~/.opencode/bin to PATH if not already present
user_opencode_bin = os.path.expanduser("~/.opencode/bin")
if os.path.exists(user_opencode_bin) and user_opencode_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{user_opencode_bin}:{os.environ.get('PATH', '')}"

# OpenCode Server Settings
OPENCODE_SERVER_URL = os.getenv("OPENCODE_SERVER_URL", "http://127.0.0.1:4096").rstrip("/")
OPENCODE_AUTO_START = os.getenv("OPENCODE_AUTO_START", "true").lower() in ("true", "1", "yes")
default_cmd = os.path.join(user_opencode_bin, "opencode") if os.path.exists(os.path.join(user_opencode_bin, "opencode")) else "opencode"
OPENCODE_COMMAND = os.getenv("OPENCODE_COMMAND", default_cmd).strip()
OPENCODE_WORKSPACE_DIR = os.getenv("OPENCODE_WORKSPACE_DIR", os.getcwd()).strip()
OPENCODE_SERVER_PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD", "").strip()

# Streaming and UI throttles
STREAM_THROTTLE_MS = int(os.getenv("STREAM_THROTTLE_MS", "850"))
MAX_MESSAGE_LENGTH = 3900

# Proxy Settings (e.g. "http://127.0.0.1:10809")
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or None
