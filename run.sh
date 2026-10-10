#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Check if .env exists
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        echo "⚠️  فایل .env یافت نشد. در حال ایجاد از روی .env.example..."
        cp .env.example .env
        echo "❗ لطفاً مقادیر TELEGRAM_BOT_TOKEN و TELEGRAM_ALLOWED_USERS را در فایل .env وارد کنید:"
        echo "   nano $DIR/.env"
        exit 1
    else
        echo "❌ فایل .env یا .env.example یافت نشد."
        exit 1
    fi
fi

# Check Python version
if ! command -v python3 &>/dev/null; then
    echo "❌ پایتون 3 یافت نشد / Python 3 not found."
    exit 1
fi

# Check required dependency
if ! python3 -c "import aiohttp" &>/dev/null; then
    echo "📦 در حال نصب پیش‌نیازها از requirements.txt..."
    pip3 install -r requirements.txt || python3 -m pip install -r requirements.txt
fi

echo "🚀 در حال راه‌اندازی دستیار OpenCode TeleBridge..."
python3 bot.py
