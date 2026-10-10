import os
import re
from pathlib import Path
from typing import Optional, Dict, Any

HERMES_DIR = Path.home() / ".hermes"
USER_MD = HERMES_DIR / "memories" / "USER.md"
MEMORY_MD = HERMES_DIR / "memories" / "MEMORY.md"
SOUL_MD = HERMES_DIR / "SOUL.md"

OPENCODE_DIR = Path.home() / ".config" / "opencode"
OPENCODE_MEM_DIR = OPENCODE_DIR / "memories"
OPENCODE_MEM_MD = OPENCODE_MEM_DIR / "MEMORY.md"


class MemoryManager:
    def __init__(self):
        OPENCODE_MEM_DIR.mkdir(parents=True, exist_ok=True)

    def get_user_profile(self) -> str:
        """Reads USER.md from Hermes memories."""
        if USER_MD.exists():
            try:
                return USER_MD.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        return "کاربر گرامی — توسعه‌دهنده نرم‌افزار."

    def get_system_memory(self) -> str:
        """Reads MEMORY.md from Hermes memories."""
        text = ""
        if MEMORY_MD.exists():
            try:
                text = MEMORY_MD.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        if OPENCODE_MEM_MD.exists():
            try:
                opencode_mem = OPENCODE_MEM_MD.read_text(encoding="utf-8").strip()
                if opencode_mem:
                    text += f"\n§\n{opencode_mem}"
            except Exception:
                pass
        return text

    def get_soul(self) -> str:
        """Reads SOUL.md persona."""
        if SOUL_MD.exists():
            try:
                return SOUL_MD.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        return "هویت: دستیار فنی و دقیق، با لحن خودمونی و دوستانه، بدون حشو و مستقیم."

    def add_memory_note(self, note: str, target: str = "memory") -> bool:
        """Appends a new fact to memory file."""
        note = note.strip()
        if not note:
            return False

        target_file = USER_MD if target == "user" else MEMORY_MD
        target_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            current = target_file.read_text(encoding="utf-8").strip() if target_file.exists() else ""
            if current:
                new_content = f"{current}\n§\n{note}\n"
            else:
                new_content = f"{note}\n"
            target_file.write_text(new_content, encoding="utf-8")

            # Also mirror in OpenCode memories
            if OPENCODE_MEM_MD.exists():
                oc_current = OPENCODE_MEM_MD.read_text(encoding="utf-8").strip()
                OPENCODE_MEM_MD.write_text(f"{oc_current}\n§\n{note}\n", encoding="utf-8")
            else:
                OPENCODE_MEM_MD.write_text(f"{note}\n", encoding="utf-8")
            return True
        except Exception:
            return False

    def is_memory_query(self, text: str) -> bool:
        """
        Determines whether the user is asking about identity, user background,
        prior projects, or explicitly requested memory access.
        Ensures normal coding tasks are NEVER polluted by memory context.
        """
        if not text:
            return False

        text_clean = text.lower().strip()

        # Explicit commands or hashtags
        if "#memory" in text_clean or "#whoami" in text_clean or text_clean.startswith(("/memory", "/whoami", "/soul")):
            return True

        keywords = [
            "کی هستم",
            "من کیم",
            "من کی هستم",
            "درباره من",
            "درباره ام",
            "دربارهم",
            "مشخصات من",
            "شناختی از من داری",
            "منو میشناسی",
            "من رو میشناسی",
            "کی هستی",
            "تو کی هستی",
            "الان اینجا چیه",
            "پروژه سنجاق",
            "کانال من",
            "کانال تلگرامم",
            "سلیقه من",
            "سلیقه‌ام",
            "یادت هست",
            "یادت میاد",
            "توی حافظه",
            "از حافظه",
            "حافظه ات",
            "حافظه‌ات",
            "whoami",
            "who am i",
            "about me",
            "remember me",
        ]

        for kw in keywords:
            if kw in text_clean:
                return True

        return False

    def build_memory_context(self) -> str:
        """
        Builds a compact, rich Iranian Persian memory block for OpenCode prompt injection.
        """
        user_prof = self.get_user_profile()
        return (
            "\n\n[حافظه و هویت پایدار (Hermes & OpenCode Memory)]:\n"
            f"• مشخصات کاربر: {user_prof}\n"
            "• محیط کاری سیستم: لینوکس فدورا (Fedora Linux / Wayland) روی لپ‌تاپ کاربر.\n"
            "• پروژه‌های اصلی: سامانه «سنجاق» (تحلیل کانال‌های ایتا)، ربات‌های تلگرام و اتوماسیون، کانال @Sasm110313.\n"
            "• ابزارها و سلیقه: استفاده از پایتون (uv / pytest / Flask / Django)، مرورگر Google Chrome، عدم اتصال مستقیم SSH به سرورهای ریموت.\n"
            "• لحن و شخصیت (SOUL): خودمونی، دوستانه، مستقیم و فنی. بدون تشریفات اضافه یا عذرخواهی بی‌مورد. اگر چیزی را نمیدانی صریح بگو نمیدانم.\n"
            "[دستور: اگر کاربر درباره هویت خود یا سیستم پرسید، با استناد دقیق و محترمانه به این حافظه و با لحن گرم و صمیمی پاسخ بده.]\n"
        )


memory_manager = MemoryManager()
