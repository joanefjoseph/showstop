from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = _env("GEMINI_API_KEY")
    gemini_model: str = _env("GEMINI_MODEL", "gemini-2.5-flash")
    my_name: str = _env("LINKEDIN_MY_NAME")
    data_dir: Path = Path(_env("DATA_DIR", "./data")).resolve()
    
    run_times: tuple[str, ...] = tuple(
        t.strip() for t in _env("RUN_TIMES", "09:00,13:00,17:00").split(",") if t.strip()
    )
    timezone: str = _env("TIMEZONE")

    max_threads_per_run: int = int(_env("MAX_THREADS_PER_RUN", "15"))
    max_messages_per_thread: int = int(_env("MAX_MESSAGES_PER_THREAD", "30"))
    headless: bool = _bool("HEADLESS", False)
    browser_channel: str = _env("BROWSER_CHANNEL")

    # --- connection-note follow-ups ---
    conversation_scan_depth: int = int(_env("CONVERSATION_SCAN_DEPTH", "40"))
    max_followup_checks_per_run: int = int(_env("MAX_FOLLOWUP_CHECKS_PER_RUN", "10"))
    connection_note_max_chars: int = int(_env("CONNECTION_NOTE_MAX_CHARS", "300"))
    followup_max_age_days: int = int(_env("FOLLOWUP_MAX_AGE_DAYS", "14"))  # 0 = no age limit
    
    enable_direct_send: bool = _bool("ENABLE_DIRECT_SEND", True)
    meeting_link: str = _env("MEETING_LINK")

    host: str = _env("HOST", "127.0.0.1")
    port: int = int(_env("PORT", "5055"))
    open_browser_on_new_drafts: bool = _bool("OPEN_BROWSER_ON_NEW_DRAFTS", True)
    llm_min_interval_s: float = float(_env("LLM_MIN_INTERVAL_SECONDS", "7"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def my_names(self) -> list[str]:
        return [n.strip().lower() for n in self.my_name.split(",") if n.strip()]

    @property
    def display_name(self) -> str:
        names = [n.strip() for n in self.my_name.split(",") if n.strip()]
        return names[0] if names else "the user"

    @property
    def ui_url(self) -> str:
        return f"http://{self.host}:{self.port}/"


settings = Settings()