import asyncio
import json
import os
import requests
import random
from dataclasses import dataclass
from datetime import datetime, date
from typing import List, Set, Dict, Optional
import hashlib
import time
import threading

from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from playwright.async_api import async_playwright, Browser, BrowserContext, Page as PlaywrightPage, TimeoutError as PlaywrightTimeoutError


BASE = "https://reservation.frontdesksuite.com"

NO_WITNESSES_URL = (
    BASE + "/aabenraavielse/vielse/ReserveTime/StartReservation"
           "?pageId=b373305a-e1ef-4f58-8e27-fbfbf65b417a"
           "&buttonId=25803dc3-fdee-4af6-bc51-2c62de114ceb"
           "&culture=en&uiCulture=en"
)

BOOKING_TYPES = [
    {
        "name": "Own witnesses",
        "url": BASE + "/aabenraavielse/vielse/ReserveTime/StartReservation"
                      "?pageId=b373305a-e1ef-4f58-8e27-fbfbf65b417a"
                      "&buttonId=e7d25fd6-807f-45db-882c-79114e239c89"
                      "&culture=en&uiCulture=en",
        "auto_book": False,
    },
    {
        "name": "No witnesses",
        "url": NO_WITNESSES_URL,
        "auto_book": True,
    },
]

# Date windows for auto-booking (inclusive)
BOOKING_WINDOWS = [
    (date(2026, 10, 12), date(2026, 10, 24)),
    (date(2026, 11, 2),  date(2026, 11, 7)),
]

_last_update_id: int = 0


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def telegram_send(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def telegram_send_photo(image_bytes: bytes, caption: str = "") -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        requests.post(
            url,
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": ("screenshot.png", image_bytes, "image/png")},
            timeout=30,
        )
    except Exception:
        pass


def telegram_get_updates(offset: int = 0) -> list:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return []
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 5}, timeout=10)
        return r.json().get("result", [])
    except Exception:
        return []


def poll_for_code(timeout_seconds: int = 600) -> Optional[str]:
    """Block until a 4-digit reply arrives via Telegram or timeout elapses."""
    global _last_update_id
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        updates = telegram_get_updates(offset=_last_update_id + 1)
        for upd in updates:
            _last_update_id = upd["update_id"]
            msg = upd.get("message", {})
            text = msg.get("text", "").strip()
            from_id = str(msg.get("chat", {}).get("id", ""))
            if from_id == chat_id and len(text) == 4 and text.isdigit():
                return text
        time.sleep(3)

    return None


# ---------------------------------------------------------------------------
# Booking details from env
# ---------------------------------------------------------------------------

@dataclass
class BookingDetails:
    email: str
    case_number: str
    p1_name: str
    p1_dob: str
    p1_email: str
    p2_name: str
    p2_dob: str
    p2_email: str
    language: str = "english"

    @classmethod
    def from_env(cls) -> "BookingDetails":
        return cls(
            email=os.getenv("BOOKING_EMAIL", ""),
            case_number=os.getenv("BOOKING_CASE_NUMBER", ""),
            p1_name=os.getenv("BOOKING_P1_NAME", ""),
            p1_dob=os.getenv("BOOKING_P1_DOB", ""),
            p1_email=os.getenv("BOOKING_P1_EMAIL", ""),
            p2_name=os.getenv("BOOKING_P2_NAME", ""),
            p2_dob=os.getenv("BOOKING_P2_DOB", ""),
            p2_email=os.getenv("BOOKING_P2_EMAIL", ""),
            language=os.getenv("BOOKING_LANGUAGE", "english").lower(),
        )

    def is_configured(self) -> bool:
        return all([
            self.email, self.case_number,
            self.p1_name, self.p1_dob, self.p1_email,
            self.p2_name, self.p2_dob, self.p2_email,
        ])


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    interval_seconds: int = 30
    jitter_seconds: int = 15

    cutoff_year: int = 2026
    cutoff_month: int = 11
    cutoff_day: int = 17

    seen_file: str = "seen_slots.json"
    headless: bool = True

    telegram_min_interval_seconds: int = 30 * 60
    telegram_max_items: int = 10

    code_timeout_seconds: int = 600


def cutoff_date(cfg: Config) -> date:
    return date(cfg.cutoff_year, cfg.cutoff_month, cfg.cutoff_day)


# ---------------------------------------------------------------------------
# Slot helpers
# ---------------------------------------------------------------------------

def load_seen(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def save_seen(path: str, seen: Set[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def slots_fingerprint(slots: List[datetime]) -> str:
    payload = "|".join(s.isoformat(timespec="minutes") for s in slots)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_times_from_html(html: str) -> List[datetime]:
    soup = BeautifulSoup(html, "lxml")
    out: List[datetime] = []

    for day_div in soup.select("div.date.one-queue"):
        header = day_div.select_one("span.header-text")
        if not header:
            continue
        day_text = header.get_text(strip=True)

        time_spans = day_div.select("span.available-time")
        if not time_spans:
            continue

        try:
            day = dtparser.parse(day_text, fuzzy=True).date()
        except Exception:
            continue

        for ts in time_spans:
            ttxt = ts.get_text(strip=True)
            try:
                dt = dtparser.parse(f"{day.isoformat()} {ttxt}", fuzzy=True)
                out.append(dt.replace(second=0, microsecond=0))
            except Exception:
                continue

    uniq = {x.isoformat(): x for x in out}
    return sorted(uniq.values())


def is_before_cutoff(dt: datetime, cfg: Config) -> bool:
    return dt.date() <= cutoff_date(cfg)


def is_in_booking_window(dt: datetime) -> bool:
    d = dt.date()
    return any(start <= d <= end for start, end in BOOKING_WINDOWS)


# ---------------------------------------------------------------------------
# Slot scraping — reuses a single browser context, one page at a time
# ---------------------------------------------------------------------------

async def get_slots(url: str, context: BrowserContext, keep_page_if_bookable: bool = False):
    """
    Returns (slots, page, element_handle) where:
    - page is the live Playwright page (caller must close) if keep_page_if_bookable=True
      and a bookable slot was found, else None
    - element_handle is the span.available-time ElementHandle for the first bookable slot,
      so attempt_booking can click it directly without re-matching by text
    """
    page = await context.new_page()
    try:
        busted_url = f"{url}&_={int(time.time())}"
        try:
            await page.goto(busted_url, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightTimeoutError:
            await page.close()
            return [], None, None

        try:
            await page.wait_for_selector("div.date.one-queue", timeout=20000)
        except PlaywrightTimeoutError:
            pass

        html = await page.content()
        slots = parse_times_from_html(html)

        if keep_page_if_bookable:
            bookable = [s for s in slots if is_in_booking_window(s)]
            if bookable:
                # Collect all span.available-time handles in DOM order — they match
                # the order of slots returned by parse_times_from_html, so we can
                # look up the handle by index without any text re-parsing.
                all_handles = await page.query_selector_all("div.date.one-queue span.available-time")
                target = bookable[0]
                try:
                    target_idx = slots.index(target)
                    target_handle = all_handles[target_idx] if target_idx < len(all_handles) else None
                except (ValueError, IndexError):
                    target_handle = None
                return slots, page, target_handle  # caller must close this page

        await page.close()
        return slots, None, None
    except Exception:
        await page.close()
        raise


# ---------------------------------------------------------------------------
# Booking flow — receives the already-loaded calendar page, clicks slot on it
# ---------------------------------------------------------------------------

async def attempt_booking(slot: datetime, details: BookingDetails, cfg: Config, page: PlaywrightPage, slot_handle=None) -> bool:
    slot_label = slot.isoformat(sep=" ", timespec="minutes")
    telegram_send(f"Slot found in booking window: {slot_label}\nAttempting to book now...")

    try:
        # 1. Click the exact element handle captured during scraping — no re-matching needed
        if slot_handle:
            try:
                await slot_handle.click()
            except Exception as e:
                telegram_send(f"Could not click slot {slot_label} — it may have been taken. ({e})")
                return False
        else:
            telegram_send(f"No element handle for slot {slot_label} — cannot book.")
            return False

        # 3. Wait for the booking form
        await page.wait_for_selector("input[type='text'], input[type='email']", timeout=15000)

        # 4. Fill form fields using exact IDs
        await page.locator("#email").fill(details.email)
        await page.locator("#field9238").fill(details.case_number)

        # Part 1
        await page.locator("#field9243").fill(details.p1_name)
        await page.locator("#field9244").fill(details.p1_dob)
        await page.locator("#field9242").fill(details.p1_email)

        # Part 2
        await page.locator("#field9229").fill(details.p2_name)
        await page.locator("#field9241").fill(details.p2_dob)
        await page.locator("#field9247").fill(details.p2_email)

        # 5. Language checkbox
        lang_id_map = {"english": "3307field9245", "danish": "3303field9245", "german": "3304field9245"}
        lang_id = lang_id_map.get(details.language, "3307field9245")
        lang_cb = page.locator(f"#{lang_id}")
        if not await lang_cb.is_checked():
            await lang_cb.check()

        # 6. "Both understand" → Yes radio
        yes_radio = page.locator("#3305field9246")
        if not await yes_radio.is_checked():
            await yes_radio.check()

        # 7. Click Confirm
        await page.locator("#submit-btn").click()

        # 8. Wait for 4-digit code screen
        try:
            await page.wait_for_selector(
                "input[maxlength='4'], input[placeholder*='code' i], input[placeholder*='kode' i]",
                timeout=15000
            )
        except PlaywrightTimeoutError:
            content = await page.content()
            if any(w in content.lower() for w in ["confirm", "success", "thank"]):
                telegram_send(f"Booking confirmed (no code needed)!\nSlot: {slot_label}")
                return True
            telegram_send(f"Unexpected page after Confirm — could not find code input.\nSlot: {slot_label}")
            return False

        # 9. Ask user for the code via Telegram
        telegram_send(
            f"Booking in progress for {slot_label}.\n"
            f"A 4-digit code has been sent to {details.email}.\n"
            f"Reply here with the 4-digit code within 10 minutes."
        )

        # 10. Poll Telegram for the reply (runs in a thread so the event loop stays free)
        code = await asyncio.get_event_loop().run_in_executor(
            None, poll_for_code, cfg.code_timeout_seconds
        )

        if not code:
            telegram_send(f"No code received within {cfg.code_timeout_seconds // 60} minutes. Booking aborted.")
            return False

        # 11. Enter the code and submit
        code_input = page.locator(
            "input[maxlength='4'], input[placeholder*='code' i], input[placeholder*='kode' i]"
        ).first
        await code_input.fill(code)
        await page.locator("#submit-btn").click()

        async def screenshot_and_send(caption: str):
            img = await page.screenshot(full_page=True)
            telegram_send_photo(img, caption=caption)

        # 12. Wait for the intermediate confirmation page, screenshot it, then click Confirm
        try:
            await page.wait_for_selector("#submit-btn", timeout=15000)
        except PlaywrightTimeoutError:
            pass

        await screenshot_and_send(f"Step 1 of 2: booking details — {slot_label}")

        return_home = page.locator("a:has-text('Return to Home'), button:has-text('Return to Home')")
        confirm_btn = page.locator("#submit-btn")
        if not await return_home.is_visible(timeout=3000) and await confirm_btn.is_visible(timeout=3000):
            await confirm_btn.click()

        # 13. Wait for the final confirmation page and screenshot it
        try:
            await page.wait_for_selector(
                "text=Your appointment, text=reservation code, text=Return to Home",
                timeout=15000
            )
        except PlaywrightTimeoutError:
            pass

        await screenshot_and_send(f"Final confirmation — {slot_label}")

        content = await page.content()
        if any(w in content.lower() for w in ["reservation code", "your appointment", "confirmed", "return to home"]):
            telegram_send(
                f"Booking CONFIRMED!\nSlot: {slot_label}\n"
                f"See screenshot above for your reservation code.\n"
                f"A confirmation email will also be sent to {details.email}."
            )
            return True
        else:
            telegram_send(
                f"Code submitted but final confirmation unclear. "
                f"See screenshot above and check {details.email}."
            )
            return False

    except Exception as e:
        err = f"Booking error for {slot_label}: {e}"
        print(err)
        telegram_send(err)
        return False
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main_async():
    cfg = Config()
    details = BookingDetails.from_env()
    seen = load_seen(cfg.seen_file)

    last_fingerprint: Dict[str, str] = {bt["name"]: "" for bt in BOOKING_TYPES}
    last_sent_at: Dict[str, float] = {bt["name"]: 0.0 for bt in BOOKING_TYPES}

    booking_in_progress = False
    booking_succeeded = False

    print(f"Cutoff: on or before {cutoff_date(cfg).isoformat()}")
    print(f"Booking windows: {[(str(s), str(e)) for s, e in BOOKING_WINDOWS]}")
    print(f"Checking every {cfg.interval_seconds}s (+ up to {cfg.jitter_seconds}s jitter).")
    print(f"Auto-booking configured: {details.is_configured()}")

    if not details.is_configured():
        print("WARNING: Booking env vars not fully set — auto-booking disabled.")

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=cfg.headless)
        context: BrowserContext = await browser.new_context(
            extra_http_headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"}
        )

        try:
            while True:
                for bt in BOOKING_TYPES:
                    name = bt["name"]
                    try:
                        slots, live_page, slot_handle = await get_slots(
                            bt["url"], context,
                            keep_page_if_bookable=bt["auto_book"] and details.is_configured() and not booking_in_progress and not booking_succeeded
                        )
                        good = [s for s in slots if is_before_cutoff(s, cfg)]

                        now_str = datetime.now().isoformat(sep=" ", timespec="seconds")
                        print(f"\n{now_str} [{name}] — {len(good)} slot(s) before cutoff")
                        for s in good:
                            print("   ", s.isoformat(sep=" "))

                        for s in good:
                            k = f"{name}|{s.isoformat()}"
                            if k not in seen:
                                seen.add(k)
                        if good:
                            save_seen(cfg.seen_file, seen)

                        # Auto-book: only "No witnesses", only in booking windows
                        if (
                            bt["auto_book"]
                            and details.is_configured()
                            and not booking_in_progress
                            and not booking_succeeded
                        ):
                            window_slots = [s for s in good if is_in_booking_window(s)]
                            if window_slots and live_page:
                                target = window_slots[0]
                                booking_in_progress = True
                                success = await attempt_booking(target, details, cfg, live_page, slot_handle)
                                live_page = None  # attempt_booking closes the page
                                booking_in_progress = False
                                if success:
                                    booking_succeeded = True
                                    telegram_send("Bot stopping after successful booking.")
                                    return

                        if live_page:
                            await live_page.close()
                            live_page = None

                        # Telegram slot notifications
                        fp = slots_fingerprint(good)
                        now_ts = time.time()

                        should_notify_change = bool(good) and (fp != last_fingerprint[name])
                        should_notify_reminder = bool(good) and (fp == last_fingerprint[name]) and (
                            (now_ts - last_sent_at[name]) >= cfg.telegram_min_interval_seconds
                        )

                        if should_notify_change or should_notify_reminder:
                            header = (
                                f"[{name}] Slots available (changed):"
                                if should_notify_change
                                else f"[{name}] Slots still available:"
                            )
                            lines = [header]
                            lines += [f"- {s.isoformat(sep=' ', timespec='minutes')}" for s in good[:cfg.telegram_max_items]]
                            if len(good) > cfg.telegram_max_items:
                                lines.append(f"(+{len(good) - cfg.telegram_max_items} more)")
                            telegram_send("\n".join(lines))
                            last_sent_at[name] = now_ts
                            last_fingerprint[name] = fp

                    except Exception as e:
                        err_msg = f"{datetime.now().isoformat(sep=' ', timespec='seconds')} [{name}] error: {e}"
                        print(err_msg)
                        telegram_send(err_msg)

                try:
                    requests.get("https://hc-ping.com/4d8cd572-9f5d-4079-b9e9-4f67c907abf4", timeout=10)
                except Exception:
                    pass

                await asyncio.sleep(cfg.interval_seconds + random.randint(0, cfg.jitter_seconds))

        finally:
            await context.close()
            await browser.close()


def run():
    asyncio.run(main_async())


if __name__ == "__main__":
    run()
