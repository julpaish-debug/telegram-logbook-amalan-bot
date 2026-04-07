"""
Telegram Daily Worship Logbook Bot (Python)

Stack:
- FastAPI webhook server
- python-telegram-bot for sending messages
- Google Sheets via gspread + service account
- Deployable to Render / Railway / Cloud Run

Features (MVP):
- /start
- /help
- /log
- /today
- /rekap
- /cancel
- Checklist flow with inline buttons
- Notes harian
- Preview before save
- Upsert daily record into Google Sheets
- Pilih tanggal saat log
- Overwrite otomatis jika tanggal yang sama sudah ada

Required environment variables:
- BOT_TOKEN
- WEBHOOK_SECRET
- APP_BASE_URL
- GOOGLE_SHEETS_ID
- GOOGLE_SERVICE_ACCOUNT_JSON
- TZ (optional, default Asia/Jakarta)

Endpoints:
- GET  /health
- POST /webhook/{WEBHOOK_SECRET}
- POST /setup-webhook
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import gspread
from fastapi import FastAPI, Header, HTTPException, Request
from google.oauth2.service_account import Credentials
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
TZ_NAME = os.getenv("TZ", "Asia/Jakarta")
TZ = ZoneInfo(TZ_NAME)

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN is empty")
if not WEBHOOK_SECRET:
    logger.warning("WEBHOOK_SECRET is empty")

SECTIONS = [
    {
        "key": "amal_tilawah_ilmu",
        "title": "Amal, Tilawah & Ilmu",
        "items": [
            {"key": "baca_alquran", "label": "Baca Al-Qur’an"},
            {"key": "ngaji_sambung", "label": "Ngaji Sambung"},
            {"key": "manqul_hadist", "label": "Manqul Hadist"},
            {"key": "sedekah_infak", "label": "Sedekah / Infak"},
            {"key": "puasa", "label": "Puasa"},
        ],
    },
    {
        "key": "shalat_sunnah",
        "title": "Shalat Sunnah",
        "items": [
            {"key": "tahajud", "label": "Shalat Tahajud"},
            {"key": "witir", "label": "Shalat Witir"},
            {"key": "dhuha", "label": "Shalat Dhuha"},
            {"key": "sunnah_subuh", "label": "Shalat Sunnah Subuh"},
            {"key": "sunnah_zuhur_before", "label": "Shalat Sunnah Zuhur Sebelum"},
            {"key": "sunnah_zuhur_after", "label": "Shalat Sunnah Zuhur Sesudah"},
            {"key": "sunnah_maghrib_after", "label": "Shalat Sunnah Maghrib Sesudah"},
            {"key": "sunnah_isya_after", "label": "Shalat Sunnah Isya Sesudah"},
            {"key": "istikharah", "label": "Shalat Istikharah"},
            {"key": "hajat", "label": "Shalat Hajat"},
        ],
    },
]

ALL_ITEMS = [item for section in SECTIONS for item in section["items"]]
ITEM_INDEX = {item["key"]: idx for idx, item in enumerate(ALL_ITEMS)}
YES = "✅"
NO = "❌"
SHEET_NAME = "Logbook"

HEADERS = [
    "Date",
    "User ID",
    "Baca Al-Qur’an",
    "Ngaji Sambung",
    "Manqul Hadist",
    "Sedekah / Infak",
    "Puasa",
    "Shalat Tahajud",
    "Shalat Witir",
    "Shalat Dhuha",
    "Shalat Sunnah Subuh",
    "Shalat Sunnah Zuhur Sebelum",
    "Shalat Sunnah Zuhur Sesudah",
    "Shalat Sunnah Maghrib Sesudah",
    "Shalat Sunnah Isya Sesudah",
    "Shalat Istikharah",
    "Shalat Hajat",
    "Notes Harian",
    "Created At",
    "Updated At",
]

KEY_TO_HEADER = {
    "baca_alquran": "Baca Al-Qur’an",
    "ngaji_sambung": "Ngaji Sambung",
    "manqul_hadist": "Manqul Hadist",
    "sedekah_infak": "Sedekah / Infak",
    "puasa": "Puasa",
    "tahajud": "Shalat Tahajud",
    "witir": "Shalat Witir",
    "dhuha": "Shalat Dhuha",
    "sunnah_subuh": "Shalat Sunnah Subuh",
    "sunnah_zuhur_before": "Shalat Sunnah Zuhur Sebelum",
    "sunnah_zuhur_after": "Shalat Sunnah Zuhur Sesudah",
    "sunnah_maghrib_after": "Shalat Sunnah Maghrib Sesudah",
    "sunnah_isya_after": "Shalat Sunnah Isya Sesudah",
    "istikharah": "Shalat Istikharah",
    "hajat": "Shalat Hajat",
}


@dataclass
class DraftState:
    date: str
    current_index: int = 0
    waiting_notes: bool = False
    waiting_date_input: bool = False
    notes_harian: str = ""
    answers: Dict[str, str] = field(default_factory=dict)


DRAFTS: Dict[str, DraftState] = {}


def now_local() -> datetime:
    return datetime.now(TZ)


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def iso_now() -> str:
    return now_local().isoformat()


def empty_answers() -> Dict[str, str]:
    return {item["key"]: "" for item in ALL_ITEMS}


def find_section_by_item(item_key: str) -> Dict:
    return next(section for section in SECTIONS if any(i["key"] == item_key for i in section["items"]))


def build_yes_no_keyboard(item_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(YES, callback_data=f"lg|{item_key}|1"),
            InlineKeyboardButton(NO, callback_data=f"lg|{item_key}|0"),
        ]]
    )


def build_notes_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Tulis Notes", callback_data="notes|write"),
            InlineKeyboardButton("Lewati", callback_data="notes|skip"),
        ]]
    )


def build_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Simpan", callback_data="save|confirm"),
                InlineKeyboardButton("Edit Notes", callback_data="save|edit_notes"),
            ],
            [InlineKeyboardButton("Batal", callback_data="save|cancel")],
        ]
    )


def build_date_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Hari ini", callback_data="date|today"),
            InlineKeyboardButton("Tanggal lain", callback_data="date|other"),
        ]]
    )


def format_log_text(date: str, answers: Dict[str, str], notes: str, preview: bool = False) -> str:
    lines: List[str] = []
    title = "📌 Preview Logbook" if preview else "📒 Logbook Amalan"
    lines.append(f"<b>{title} — {date}</b>")
    lines.append("")
    for section in SECTIONS:
        lines.append(f"<b>{section['title']}</b>")
        for item in section["items"]:
            value = answers.get(item["key"]) or NO
            lines.append(f"- {item['label']}: {value}")
        lines.append("")
    lines.append("<b>Notes Harian:</b>")
    lines.append(notes if notes else "—")
    if preview:
        lines.append("")
        lines.append("Simpan log ini?")
    return "\n".join(lines)


class SheetsRepository:
    def __init__(self) -> None:
        if not GOOGLE_SHEETS_ID:
            raise ValueError("GOOGLE_SHEETS_ID is required")
        if not GOOGLE_SERVICE_ACCOUNT_JSON:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is required")

        creds_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        credentials = Credentials.from_service_account_info(creds_info, scopes=scopes)
        self.client = gspread.authorize(credentials)
        self.spreadsheet = self.client.open_by_key(GOOGLE_SHEETS_ID)
        self.worksheet = self._get_or_create_worksheet()

    def _get_or_create_worksheet(self):
        try:
            ws = self.spreadsheet.worksheet(SHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = self.spreadsheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(HEADERS))
            ws.append_row(HEADERS)
            return ws

        first_row = ws.row_values(1)
        if not first_row:
            ws.append_row(HEADERS)
        elif first_row != HEADERS:
            ws.update("A1:T1", [HEADERS])
        return ws

    def _all_records(self) -> List[Dict[str, str]]:
        return self.worksheet.get_all_records(expected_headers=HEADERS)

    def get_log_by_date(self, user_id: str, date_str: str) -> Optional[Dict[str, str]]:
        for record in self._all_records():
            if str(record.get("User ID")) == str(user_id) and record.get("Date") == date_str:
                return record
        return None

    def get_recent_logs(self, user_id: str, days: int) -> List[Dict[str, str]]:
        cutoff = now_local().date() - timedelta(days=days - 1)
        records: List[Dict[str, str]] = []
        for record in self._all_records():
            if str(record.get("User ID")) != str(user_id):
                continue
            try:
                row_date = datetime.strptime(record.get("Date", ""), "%Y-%m-%d").date()
            except ValueError:
                continue
            if row_date >= cutoff:
                records.append(record)
        records.sort(key=lambda r: r.get("Date", ""))
        return records

    def upsert_daily_log(self, user_id: str, draft: DraftState) -> None:
        records = self._all_records()
        existing_row_number = None
        existing_created_at = None
        for idx, record in enumerate(records, start=2):
            if str(record.get("User ID")) == str(user_id) and record.get("Date") == draft.date:
                existing_row_number = idx
                existing_created_at = record.get("Created At")
                break

        updated_at = iso_now()
        created_at = existing_created_at or updated_at
        row = [
            draft.date,
            user_id,
            draft.answers.get("baca_alquran") or NO,
            draft.answers.get("ngaji_sambung") or NO,
            draft.answers.get("manqul_hadist") or NO,
            draft.answers.get("sedekah_infak") or NO,
            draft.answers.get("puasa") or NO,
            draft.answers.get("tahajud") or NO,
            draft.answers.get("witir") or NO,
            draft.answers.get("dhuha") or NO,
            draft.answers.get("sunnah_subuh") or NO,
            draft.answers.get("sunnah_zuhur_before") or NO,
            draft.answers.get("sunnah_zuhur_after") or NO,
            draft.answers.get("sunnah_maghrib_after") or NO,
            draft.answers.get("sunnah_isya_after") or NO,
            draft.answers.get("istikharah") or NO,
            draft.answers.get("hajat") or NO,
            draft.notes_harian or "",
            created_at,
            updated_at,
        ]

        if existing_row_number:
            self.worksheet.update(f"A{existing_row_number}:T{existing_row_number}", [row])
        else:
            self.worksheet.append_row(row)


repo: Optional[SheetsRepository] = None
telegram_app: Optional[Application] = None
fastapi_app = FastAPI(title="Telegram Logbook Bot")
app = fastapi_app


async def send_start(chat_id: int) -> None:
    assert telegram_app is not None
    text = (
        "Assalamu’alaikum. Bot logbook amalan harian siap digunakan.\n\n"
        "Perintah:\n"
        "- /start untuk menu awal\n"
        "- /help untuk bantuan\n"
        "- /log untuk isi atau revisi log\n"
        "- /today untuk lihat log hari ini\n"
        "- /rekap untuk lihat rekap 7 hari terakhir\n"
        "- /cancel untuk batalkan draft saat ini"
    )
    await telegram_app.bot.send_message(chat_id=chat_id, text=text)


async def start_log(chat_id: int, user_id: str) -> None:
    assert telegram_app is not None
    DRAFTS[user_id] = DraftState(date=today_str(), answers=empty_answers())
    await telegram_app.bot.send_message(
        chat_id=chat_id,
        text="📒 Pilih tanggal logbook:\n- Hari ini\n- Tanggal lain",
        reply_markup=build_date_choice_keyboard(),
    )


async def send_next_item(chat_id: int, user_id: str) -> None:
    assert telegram_app is not None
    draft = DRAFTS.get(user_id)
    if not draft:
        await telegram_app.bot.send_message(chat_id=chat_id, text="Draft tidak ditemukan. Mulai lagi dengan /log")
        return

    item = ALL_ITEMS[draft.current_index]
    section = find_section_by_item(item["key"])
    progress = f"{draft.current_index + 1}/{len(ALL_ITEMS)}"
    text = (
        f"📒 <b>{section['title']}</b>\n"
        f"Progress: {progress}\n\n"
        f"Pilih status untuk:\n<b>{item['label']}</b>"
    )
    await telegram_app.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=build_yes_no_keyboard(item["key"]),
        parse_mode=ParseMode.HTML,
    )


async def ask_for_notes(chat_id: int, user_id: str) -> None:
    assert telegram_app is not None
    draft = DRAFTS.get(user_id)
    if not draft:
        await telegram_app.bot.send_message(chat_id=chat_id, text="Draft tidak ditemukan. Mulai lagi dengan /log")
        return

    draft.waiting_notes = False
    await telegram_app.bot.send_message(
        chat_id=chat_id,
        text="📝 Ada Notes Harian? Bisa dipakai untuk detail bacaan Al-Qur’an atau catatan lain.",
        reply_markup=build_notes_keyboard(),
    )


async def send_preview(chat_id: int, user_id: str) -> None:
    assert telegram_app is not None
    draft = DRAFTS.get(user_id)
    if not draft:
        await telegram_app.bot.send_message(chat_id=chat_id, text="Draft tidak ditemukan. Mulai lagi dengan /log")
        return

    await telegram_app.bot.send_message(
        chat_id=chat_id,
        text=format_log_text(draft.date, draft.answers, draft.notes_harian, preview=True),
        reply_markup=build_preview_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def send_log_by_date(chat_id: int, user_id: str, date_str: str) -> None:
    assert telegram_app is not None
    assert repo is not None
    row = repo.get_log_by_date(user_id, date_str)
    if not row:
        await telegram_app.bot.send_message(chat_id=chat_id, text=f"Belum ada log untuk tanggal {date_str}.")
        return

    answers = {key: row.get(header, NO) for key, header in KEY_TO_HEADER.items()}
    notes = row.get("Notes Harian", "")
    text = format_log_text(row["Date"], answers, notes, preview=False)
    await telegram_app.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)


async def send_today(chat_id: int, user_id: str) -> None:
    await send_log_by_date(chat_id, user_id, today_str())


async def send_recap(chat_id: int, user_id: str, days: int = 7) -> None:
    assert telegram_app is not None
    assert repo is not None
    rows = repo.get_recent_logs(user_id, days)
    if not rows:
        await telegram_app.bot.send_message(chat_id=chat_id, text="Belum ada data rekap.")
        return

    lines = [f"📊 <b>Rekap {days} hari terakhir</b>", ""]
    for row in rows:
        done_items = [
            item["label"]
            for item in ALL_ITEMS
            if row.get(KEY_TO_HEADER[item["key"]]) == YES
        ]
        yes_count = len(done_items)
        done_text = ", ".join(done_items) if done_items else "—"
        notes = row.get("Notes Harian", "").strip() or "—"
        lines.append(f"🗓️ <b>{row.get('Date')}</b>: <b>{yes_count}/{len(ALL_ITEMS)}</b>")
        lines.append(f"✅ <b>Dilakukan:</b> {done_text}")
        lines.append(f"📝 <b>Notes:</b> {notes}")
        lines.append("")

    await telegram_app.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def save_final(chat_id: int, user_id: str) -> None:
    assert telegram_app is not None
    assert repo is not None
    draft = DRAFTS.get(user_id)
    if not draft:
        await telegram_app.bot.send_message(chat_id=chat_id, text="Draft tidak ditemukan. Mulai lagi dengan /log")
        return

    existing = repo.get_log_by_date(user_id, draft.date)
    repo.upsert_daily_log(user_id, draft)
    DRAFTS.pop(user_id, None)

    if existing:
        await telegram_app.bot.send_message(chat_id=chat_id, text=f"✅ Logbook tanggal {draft.date} berhasil direvisi.")
    else:
        await telegram_app.bot.send_message(chat_id=chat_id, text=f"✅ Logbook tanggal {draft.date} berhasil disimpan.")

    await send_log_by_date(chat_id, user_id, draft.date)


async def handle_text_message(update: Update) -> None:
    if not update.message or not update.message.from_user or not update.message.chat:
        return

    text = (update.message.text or "").strip()
    user_id = str(update.message.from_user.id)
    chat_id = update.message.chat.id
    draft = DRAFTS.get(user_id)

    if draft and draft.waiting_date_input and text and not text.startswith("/"):
        try:
            parsed_date = datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text="Format tanggal salah. Gunakan format YYYY-MM-DD, contoh: 2026-04-08",
            )
            return

        draft.date = parsed_date
        draft.waiting_date_input = False
        draft.current_index = 0
        await telegram_app.bot.send_message(chat_id=chat_id, text=f"Tanggal logbook: {parsed_date}")
        await send_next_item(chat_id, user_id)
        return

    if draft and draft.waiting_notes and text and not text.startswith("/"):
        draft.notes_harian = text
        draft.waiting_notes = False
        await send_preview(chat_id, user_id)
        return

    if text == "/start":
        await send_start(chat_id)
    elif text == "/help":
        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=(
                "Perintah yang tersedia:\n"
                "/start - buka menu awal\n"
                "/help - bantuan command\n"
                "/log - isi atau revisi logbook\n"
                "/today - lihat log hari ini\n"
                "/rekap - lihat rekap 7 hari terakhir\n"
                "/cancel - batalkan draft saat ini"
            ),
        )
    elif text == "/log":
        await start_log(chat_id, user_id)
    elif text == "/today":
        await send_today(chat_id, user_id)
    elif text == "/rekap":
        await send_recap(chat_id, user_id, days=7)
    elif text == "/cancel":
        DRAFTS.pop(user_id, None)
        await telegram_app.bot.send_message(chat_id=chat_id, text="Draft log hari ini dibatalkan.")
    else:
        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text="Perintah tidak dikenali. Gunakan /log untuk isi log hari ini atau /start untuk bantuan.",
        )


async def handle_callback_query(update: Update) -> None:
    if not update.callback_query or not update.callback_query.from_user or not update.callback_query.message:
        return

    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    chat_id = query.message.chat.id
    data = query.data or ""
    parts = data.split("|")
    action = parts[0]

    if action == "date":
        mode = parts[1]
        draft = DRAFTS.get(user_id)
        if not draft:
            await telegram_app.bot.send_message(chat_id=chat_id, text="Draft tidak ditemukan. Mulai lagi dengan /log")
            return

        if mode == "today":
            draft.date = today_str()
            draft.waiting_date_input = False
            draft.current_index = 0
            await telegram_app.bot.send_message(chat_id=chat_id, text=f"Tanggal logbook: {draft.date}")
            await send_next_item(chat_id, user_id)
            return

        if mode == "other":
            draft.waiting_date_input = True
            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text="Kirim tanggal logbook dengan format YYYY-MM-DD, contoh: 2026-04-08",
            )
            return

    if action == "lg":
        item_key = parts[1]
        raw_value = parts[2]
        draft = DRAFTS.get(user_id)
        if not draft:
            await telegram_app.bot.send_message(chat_id=chat_id, text="Draft tidak ditemukan. Mulai lagi dengan /log")
            return

        draft.answers[item_key] = YES if raw_value == "1" else NO
        draft.current_index = ITEM_INDEX[item_key] + 1

        if draft.current_index >= len(ALL_ITEMS):
            await ask_for_notes(chat_id, user_id)
        else:
            await send_next_item(chat_id, user_id)
        return

    if action == "notes":
        mode = parts[1]
        draft = DRAFTS.get(user_id)
        if not draft:
            await telegram_app.bot.send_message(chat_id=chat_id, text="Draft tidak ditemukan. Mulai lagi dengan /log")
            return

        if mode == "skip":
            draft.notes_harian = ""
            draft.waiting_notes = False
            await send_preview(chat_id, user_id)
            return

        if mode == "write":
            draft.waiting_notes = True
            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text="📝 Silakan kirim Notes Harian. Bisa berisi detail bacaan Al-Qur’an atau catatan lain.",
            )
            return

    if action == "save":
        mode = parts[1]
        if mode == "confirm":
            await save_final(chat_id, user_id)
            return
        if mode == "cancel":
            DRAFTS.pop(user_id, None)
            await telegram_app.bot.send_message(chat_id=chat_id, text="Draft dibatalkan.")
            return
        if mode == "edit_notes":
            draft = DRAFTS.get(user_id)
            if not draft:
                await telegram_app.bot.send_message(chat_id=chat_id, text="Draft tidak ditemukan. Mulai lagi dengan /log")
                return
            draft.waiting_notes = True
            await telegram_app.bot.send_message(chat_id=chat_id, text="Kirim Notes Harian yang baru sebagai pesan biasa.")
            return


@fastapi_app.on_event("startup")
async def startup_event() -> None:
    global telegram_app, repo

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")

    telegram_app = Application.builder().token(BOT_TOKEN).updater(None).build()
    await telegram_app.initialize()
    repo = SheetsRepository()
    await telegram_app.bot.set_my_commands([
        BotCommand("start", "Menu awal"),
        BotCommand("help", "Bantuan command"),
        BotCommand("log", "Isi atau revisi logbook"),
        BotCommand("today", "Lihat log hari ini"),
        BotCommand("rekap", "Lihat rekap 7 hari terakhir"),
        BotCommand("cancel", "Batalkan draft aktif"),
    ])
    logger.info("Application initialized")


@fastapi_app.on_event("shutdown")
async def shutdown_event() -> None:
    global telegram_app
    if telegram_app is not None:
        await telegram_app.shutdown()


@fastapi_app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@fastapi_app.post("/webhook/{secret}")
async def webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> Dict[str, bool]:
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    _ = x_telegram_bot_api_secret_token
    assert telegram_app is not None

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)

    if update.callback_query:
        await handle_callback_query(update)
    elif update.message:
        await handle_text_message(update)

    return {"ok": True}


@fastapi_app.post("/setup-webhook")
async def setup_webhook() -> Dict[str, str]:
    assert telegram_app is not None
    if not APP_BASE_URL:
        raise HTTPException(status_code=400, detail="APP_BASE_URL is required")

    webhook_url = f"{APP_BASE_URL.rstrip('/')}/webhook/{WEBHOOK_SECRET}"
    await telegram_app.bot.set_webhook(url=webhook_url, drop_pending_updates=False)
    return {"status": "ok", "webhook_url": webhook_url}
