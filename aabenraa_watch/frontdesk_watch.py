import asyncio
import json
import os
import requests
import random
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import List, Set, Dict
import hashlib
import time

from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


BASE = "https://reservation.frontdesksuite.com"

BOOKING_TYPES = [
    {
        "name": "Own witnesses",
        "url": BASE + "/aabenraavielse/vielse/ReserveTime/StartReservation"
                      "?pageId=b373305a-e1ef-4f58-8e27-fbfbf65b417a"
                      "&buttonId=e7d25fd6-807f-45db-882c-79114e239c89"
                      "&culture=en&uiCulture=en",
    },
    {
        "name": "No witnesses",
        "url": BASE + "/aabenraavielse/vielse/ReserveTime/StartReservation"
                      "?pageId=b373305a-e1ef-4f58-8e27-fbfbf65b417a"
                      "&buttonId=25803dc3-fdee-4af6-bc51-2c62de114ceb"
                      "&culture=en&uiCulture=en",
    },
]


def telegram_send(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def slots_fingerprint(slots: List[datetime]) -> str:
    payload = "|".join(s.isoformat(timespec="minutes") for s in slots)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Config:
    interval_seconds: int = 60
    jitter_seconds: int = 15

    cutoff_year: int = 2026
    cutoff_month: int = 11
    cutoff_day: int = 30

    seen_file: str = "seen_slots.json"
    headless: bool = True

    telegram_min_interval_seconds: int = 30 * 60
    telegram_max_items: int = 10


def cutoff_date(cfg: Config) -> date:
    return date(cfg.cutoff_year, cfg.cutoff_month, cfg.cutoff_day)


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


async def get_slots_for_url(url: str, headless: bool) -> List[datetime]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        try:
            await page.wait_for_selector("div.date.one-queue", timeout=20000)
        except PlaywrightTimeoutError:
            html = await page.content()
            await context.close()
            await browser.close()
            return parse_times_from_html(html)

        html = await page.content()
        slots = parse_times_from_html(html)

        await context.close()
        await browser.close()
        return slots


async def main_async():
    cfg = Config()
    seen = load_seen(cfg.seen_file)

    # per-booking-type state for change detection
    last_fingerprint: Dict[str, str] = {bt["name"]: "" for bt in BOOKING_TYPES}
    last_sent_at: Dict[str, float] = {bt["name"]: 0.0 for bt in BOOKING_TYPES}

    print(f"Cutoff: on or before {cutoff_date(cfg).isoformat()}")
    print(f"Checking every {cfg.interval_seconds}s (+ up to {cfg.jitter_seconds}s jitter).")
    print(f"Watching {len(BOOKING_TYPES)} booking type(s): {[bt['name'] for bt in BOOKING_TYPES]}")

    while True:
        for bt in BOOKING_TYPES:
            name = bt["name"]
            try:
                slots = await get_slots_for_url(bt["url"], cfg.headless)
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

                fp = slots_fingerprint(good)
                now_ts = time.time()

                should_notify_change = bool(good) and (fp != last_fingerprint[name])
                should_notify_reminder = bool(good) and (fp == last_fingerprint[name]) and (
                    (now_ts - last_sent_at[name]) >= cfg.telegram_min_interval_seconds
                )

                if should_notify_change or should_notify_reminder:
                    header = f"[{name}] Slots available (changed):" if should_notify_change else f"[{name}] Slots still available:"
                    lines = [header]
                    lines += [f"- {s.isoformat(sep=' ', timespec='minutes')}" for s in good[:cfg.telegram_max_items]]
                    if len(good) > cfg.telegram_max_items:
                        lines.append(f"(+{len(good) - cfg.telegram_max_items} more)")
                    telegram_send("\n".join(lines))
                    last_sent_at[name] = now_ts
                    last_fingerprint[name] = fp

            except Exception as e:
                print(f"{datetime.now().isoformat(sep=' ', timespec='seconds')} [{name}] error: {e}")

        await asyncio.sleep(cfg.interval_seconds + random.randint(0, cfg.jitter_seconds))


def run():
    asyncio.run(main_async())


if __name__ == "__main__":
    run()
