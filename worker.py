"""
worker.py — Playwright async workers xu ly tung URL.

Kien truc:
- run_session() chay trong thread rieng (via asyncio.run)
- JobScheduler quan ly (url, comment, key) theo thu tu key uu tien
- Moi worker la 1 coroutine chay vong lap lay job tu Scheduler
- Comment that bai duoc tra ve pool de tai su dung
- Ket qua gui ve Tkinter qua thread-safe queue.Queue
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import queue
import time
import threading
from collections import deque
from urllib.parse import urlparse, quote
from dataclasses import dataclass, field
from typing import Optional
import httpx

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# TargetClosedError co tu Playwright >= 1.35; fallback de tuong thich nga phien ban cu
try:
    from playwright.async_api import TargetClosedError as _TargetClosedError
except ImportError:
    _TargetClosedError = Exception  # type: ignore

try:
    from playwright_stealth import stealth_async
    _HAS_STEALTH = True
except ImportError:
    _HAS_STEALTH = False

import human_sim
import form_detect

# ── Logging ─────────────────────────────────────
log = logging.getLogger("atc")
log.addHandler(logging.NullHandler())


# ─────────────────────────────────────────────
# Result dataclass — gửi về GUI
# ─────────────────────────────────────────────

STATUS_SUCCESS        = "SUCCESS"
STATUS_MODERATION     = "SUCCESS (moderation)"
STATUS_FORM_ERROR     = "FORM_ERROR"
STATUS_CAPTCHA        = "CAPTCHA"          # đang chờ user giải
STATUS_CAPTCHA_FAILED = "CAPTCHA_FAILED"
STATUS_NO_FORM        = "FORM_NOT_FOUND"
STATUS_REVIEW         = "NEEDS_REVIEW"
STATUS_PROGRESS       = "__PROGRESS__"     # internal: cập nhật progress bar
STATUS_WATCHDOG       = "__WATCHDOG__"     # internal: watchdog restart
STATUS_DONE           = "__DONE__"         # internal: session kết thúc

async def _ping_indexnow(url: str, keyword: str):
    """Gửi tín hiệu ping URL lên mạng lưới IndexNow (Bing/Yandex)."""
    try:
        encoded_url = quote(url, safe='')
        # Ping toi api.indexnow.org de bao cho mang luoi biet co comment moi (call bot)
        ping_url = f"https://api.indexnow.org/indexnow?url={encoded_url}&key=atctool_seobot_999"
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(ping_url)
            log.info(f"[{keyword}] PING IndexNow {resp.status_code}: {url}")
    except Exception as e:
        log.debug(f"[{keyword}] IndexNow ping error: {e}")


@dataclass
class Result:
    url: str = ""
    status: str = STATUS_REVIEW
    comment_used: str = ""
    comment_link: str = ""          # URL day du bao gom #comment-ID
    error_detail: str = ""
    strategy: str = ""              # layer nao detect duoc form
    key: str = ""                   # keyword dang chay (de GUI hien thi per-key)
    group_name: str = ""            # ten group (multi-group)
    # Chi dung khi status == STATUS_CAPTCHA:
    captcha_event: Optional[asyncio.Event] = field(default=None, repr=False)
    captcha_loop: Optional[asyncio.AbstractEventLoop] = field(default=None, repr=False)


@dataclass
class SessionProfile:
    name: str = ""   # Ten tac gia dien vao form comment (khac voi keyword)
    email: str = ""
    host: str = ""      # website/URL field


async def _call_groq_realtime(prompt: str, sys_prompt: str = "") -> str:
    import random
    import asyncio
    from groq import AsyncGroq
    try:
        import comment_gen
        keys = comment_gen.load_keys_from_env()
        if not keys: return ""
        
        # Thử tối đa 3 lần với 3 key ngẫu nhiên
        for attempt in range(3):
            try:
                client = AsyncGroq(api_key=random.choice(keys))
                msgs = []
                if sys_prompt:
                    msgs.append({"role": "system", "content": sys_prompt})
                msgs.append({"role": "user", "content": prompt})
                chat = await client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=msgs,

                    temperature=0.8,
                    max_tokens=250,
                )
                return chat.choices[0].message.content.strip()
            except Exception as e:
                err_str = str(e).lower()
                if "429" in err_str or "rate limit" in err_str:
                    await asyncio.sleep(1) # Nghỉ 1 giây rồi xoay key khác thử lại
                    continue
                else:
                    break # Lỗi khác (mạng, API đổi) thì bỏ qua
        return ""
    except Exception as exc:
        log.debug(f"[GROQ Realtime] Error: {exc}")
        return ""

# ─────────────────────────────────────────────
# JobScheduler — thread-safe, quan ly job theo key
# ─────────────────────────────────────────────

class JobScheduler:
    """
    Quan ly danh sach (url, comment, key) theo thu tu key.
    - next_job(): lay job tiep theo, tu dong chuyen key khi du target
    - record_success(key): ghi nhan thanh cong, comment duoc tieu thu
    - return_comment(key, comment): tra comment lai pool (URL fail — tai su dung)
    - is_done(): tat ca key da du target hoac het URL
    """

    def __init__(
        self,
        urls: list[str],
        keys_targets: list[tuple[str, int]],
        comment_pools: dict[str, list[str]],
        profiles: "dict[str, SessionProfile] | None" = None,
    ) -> None:
        from db_manager import DBManager
        self._lock = threading.Lock()
        self.db = DBManager()
        self.db.add_urls(urls)
        
        self._keys: list[tuple[str, int]] = list(keys_targets)
        self._key_idx: int = 0
        self._success: dict[str, int] = {k: 0 for k, _ in keys_targets}
        self._pools: dict[str, deque[str]] = {
            k: deque(v) for k, v in comment_pools.items()
        }
        # Backup goc de recycle khi pool het
        self._pools_backup: dict[str, list[str]] = {
            k: list(v) for k, v in comment_pools.items()
        }
        self._profiles: dict[str, SessionProfile] = profiles or {}
        self._target_map: dict[str, int] = {k: t for k, t in keys_targets}
        # in-flight: URL da dispatch nhung chua hoan thanh (dung cho watchdog recovery)
        self._inflight: dict[str, tuple[str, str]] = {}  # url -> (comment, key)
        # dem so lan watchdog phuc hoi moi URL — qua gioi han → FAILED vinh vien
        self._url_recover_count: dict[str, int] = {}

    # --- public -------------------------------------------------------

    def next_job(self) -> tuple[str, str, str] | None:
        """Tra ve (url, comment, key) hoac None chi khi THUC SU khong con gi de lam hoac dang sleep."""
        with self._lock:
            # Check if all targets met
            all_targets_met = True
            for key, target in self._keys:
                if self._success[key] < target:
                    all_targets_met = False
                    break
            if all_targets_met:
                return None

            valid_key = None
            valid_comment = None
            tried = 0
            total_keys = len(self._keys)
            start_idx = self._key_idx

            while tried < total_keys:
                idx = (start_idx + tried) % total_keys
                key, target = self._keys[idx]

                if self._success[key] >= target:
                    if idx == self._key_idx:
                        self._key_idx = (self._key_idx + 1) % total_keys
                    tried += 1
                    continue

                pool = self._pools.get(key)
                if pool and len(pool) > 0:
                    valid_comment = pool.popleft()
                else:
                    # Pool het → recycle random tu pool goc
                    backup = self._pools_backup.get(key, [])
                    if backup:
                        valid_comment = random.choice(backup)
                    else:
                        valid_comment = "Bài viết rất hay và chi tiết, cảm ơn bạn đã chia sẻ!"
                    
                valid_key = key
                self._key_idx = (idx + 1) % total_keys
                break

            if not valid_key:
                return None

            job_id, valid_url = self.db.get_next_job(cooldown_seconds=300)
            if valid_url:
                self._inflight[valid_url] = (valid_comment, valid_key)
                return (valid_url, valid_comment, valid_key)
            else:
                self._pools[valid_key].appendleft(valid_comment)
                return None
    def record_success(self, key: str, _comment: str) -> None:
        """Comment da dung thanh cong — khong tra lai pool."""
        with self._lock:
            self._success[key] += 1

    def finalize_job(self, url: str) -> None:
        """Xoa URL khoi danh sach in-flight sau khi hoan thanh (thanh cong hoac that bai)."""
        with self._lock:
            self._inflight.pop(url, None)

    def recover_inflight(self) -> tuple[int, list[tuple[str, str, str]]]:
        """Phuc hoi URL dang in-flight ve hang doi (goi sau watchdog).
        URL da bi phuc hoi qua _MAX_URL_RECOVERIES lan se bi danh dau FAILED vinh vien.
        Tra ve (so_url_phuc_hoi, danh_sach_url_chet).
        danh_sach_url_chet: list[(url, comment, key)]"""
        with self._lock:
            recovered = 0
            dead: list[tuple[str, str, str]] = []
            for url, (comment, key) in self._inflight.items():
                count = self._url_recover_count.get(url, 0) + 1
                self._url_recover_count[url] = count
                if count > _MAX_URL_RECOVERIES:
                    # URL nay that su hong — bo qua, khong tra lai queue
                    dead.append((url, comment, key))
                    log.warning(
                        f"[WD] URL DEAD sau {count} lan treo, bo qua vinh vien: {url}"
                    )
                else:
                    # Con trong gioi han — tra lai de thu tiep
                    self.db.conn.execute("UPDATE queue SET status = 'PENDING' WHERE url = ?", (url,))
                    self.db.conn.commit()
                    if key in self._pools:
                        self._pools[key].appendleft(comment)
                    recovered += 1
                    log.info(
                        f"[WD] Phuc hoi URL (lan {count}/{_MAX_URL_RECOVERIES}): {url}"
                    )
            self._inflight.clear()
            return recovered, dead

    def return_comment(self, key: str, comment: str) -> None:
        """URL fail — tra comment ve dau pool de tai su dung."""
        with self._lock:
            if key in self._pools:
                self._pools[key].appendleft(comment)

    def add_comments(self, key: str, comments: list[str]) -> None:
        """Bo sung comment vao pool (goi khi pool can them)."""
        with self._lock:
            if key not in self._pools:
                self._pools[key] = deque()
            self._pools[key].extend(comments)

    def get_success(self) -> dict[str, int]:
        with self._lock:
            return dict(self._success)

    def get_target(self, key: str) -> int:  # O(1)
        return self._target_map.get(key, 0)

    def is_done(self) -> bool:
        with self._lock:
            cursor = self.db.conn.execute("SELECT COUNT(*) FROM queue WHERE status = 'PENDING'")
            pending_db = cursor.fetchone()[0]
            # Chi tra ve True khi ko con url trong queue VA ko con url in-flight
            if pending_db == 0 and len(self._inflight) == 0:
                return True
                
            all_targets_met = True
            for key, target in self._keys:
                if self._success.get(key, 0) < target:
                    all_targets_met = False
                    if pending_db > 0:
                        return False
            
            if all_targets_met and len(self._inflight) == 0:
                return True
                
            return False

    def done_count(self) -> int:
        """Tong so URL da xu ly xong (success + fail). Dung de watchdog theo doi tien do."""
        with self._lock:
            return sum(self._success.values())

    def pending_count(self) -> int:
        """So URL con trong queue + in-flight."""
        with self._lock:
            cursor = self.db.conn.execute("SELECT COUNT(*) FROM queue WHERE status = 'PENDING'")
            return cursor.fetchone()[0] + len(self._inflight)

    def status_summary(self) -> str:
        """Tra ve chuoi tom tat trang thai hien tai."""
        with self._lock:
            parts = []
            for key, target in self._keys:
                s = self._success.get(key, 0)
                pool_sz = len(self._pools.get(key, []))
                parts.append(f"{key}: {s}/{target} (pool={pool_sz})")
            inf = len(self._inflight)
            cursor = self.db.conn.execute("SELECT COUNT(*) FROM queue WHERE status = 'PENDING'")
            q = cursor.fetchone()[0]
            return f"URLs: queue={q} inflight={inf} | " + " | ".join(parts)

    def get_profile(self, key: str) -> "SessionProfile":
        """Tra ve SessionProfile cho key, fallback ve profile rong."""
        return self._profiles.get(key, SessionProfile())


# ─────────────────────────────────────────────
# Worker coroutine
# ─────────────────────────────────────────────

_COMMENT_ID_RE = re.compile(r"#comment-\d+", re.IGNORECASE)

_SUCCESS_TEXTS = [
    "awaiting moderation",
    "awaiting your approval",
    "thank you for your comment",
    "comment submitted",
    "comment is awaiting",
    "your comment has been",
    "comment saved",
]

_ERROR_TEXTS = [
    "duplicate comment",
    "you are posting comments too quickly",
    "please fill the required fields",
    "please fill in",
    "something went wrong",
    "comment could not be posted",
    "you have already said that",
]


async def _get_page_text(page: Page) -> str:
    """Lay toan bo visible text 1 lan — dung chung cho success & error check."""
    try:
        return (await page.inner_text("body", timeout=3000)).lower()
    except Exception:
        return ""


def _text_contains(text: str, keywords: list[str]) -> bool:
    """Kiem tra text co chua bat ky keyword nao trong list."""
    return any(kw in text for kw in keywords)


# ─────────────────────────────────────────────
# Name obfuscation — bypass keyword filter
# ─────────────────────────────────────────────

_HOMOGLYPHS = {
    'a': '\u0430', 'c': '\u0441', 'e': '\u0435', 'o': '\u043e',
    'p': '\u0440', 'x': '\u0445', 'y': '\u0443', 'i': '\u0456',
    's': '\u0455',
    'A': '\u0410', 'B': '\u0412', 'C': '\u0421', 'E': '\u0415',
    'H': '\u041d', 'K': '\u041a', 'M': '\u041c', 'O': '\u041e',
    'P': '\u0420', 'T': '\u0422', 'X': '\u0425',
}


def _obfuscate_name(name: str) -> str:
    """Thay 1-2 ky tu bang homoglyph Unicode trong giong het.
    Bypass bo loc text chan keyword (vd: 'kubet88') ma giu nguyen hien thi."""
    # Khong obfuscate neu name co ve nhu 1 URL (de tranh WordPress stripping)
    if "http" in name.lower() or "://" in name or ".com" in name.lower() or ".dev" in name.lower() or ".net" in name.lower():
        return name
        
    replaceable = [(i, _HOMOGLYPHS[ch]) for i, ch in enumerate(name) if ch in _HOMOGLYPHS]
    if not replaceable:
        if len(name) > 1:
            pos = random.randint(1, len(name) - 1)
            return name[:pos] + '\u200b' + name[pos:]
        return name
    n_replace = min(len(replaceable), random.randint(1, 2))
    chosen = random.sample(replaceable, n_replace)
    chars = list(name)
    for idx, repl in chosen:
        chars[idx] = repl
    return ''.join(chars)


# ─────────────────────────────────────────────
# Rating handler
# ─────────────────────────────────────────────

async def _handle_rating(page: Page, form_result) -> None:
    """Xu ly rating (stars, select, radio) neu form yeu cau."""
    if form_result.rating_type == "none" or form_result.f_rating is None:
        return
    try:
        if form_result.rating_type == "stars_select":
            best_val = await form_result.f_rating.evaluate("""el => {
                for (let i = el.options.length - 1; i >= 0; i--) {
                    if (el.options[i].value && el.options[i].value !== '0' && el.options[i].value !== '')
                        return el.options[i].value;
                }
                return null;
            }""")
            if best_val:
                await form_result.f_rating.select_option(value=best_val, timeout=3000)

        elif form_result.rating_type == "stars_radio":
            await form_result.f_rating.click(timeout=3000)

        elif form_result.rating_type == "stars_click":
            await form_result.f_rating.click(timeout=3000)
            await asyncio.sleep(0.3)
            # WooCommerce stars: click lan 2 neu can confirm
            try:
                active = page.locator(".comment-form-rating .stars a.active, p.stars a.active").first
                if not await active.is_visible(timeout=500):
                    await form_result.f_rating.click(timeout=2000)
            except Exception:
                pass

        elif form_result.rating_type == "select_text":
            best_val = await form_result.f_rating.evaluate("""el => {
                const opts = Array.from(el.options);
                const priority = ['xuất sắc','tuyệt vời','excellent','rất tốt','very good',
                                   'great','best','tốt','good','5','4'];
                for (const p of priority) {
                    const m = opts.find(o => o.text.toLowerCase().includes(p));
                    if (m) return m.value;
                }
                for (let i = opts.length - 1; i >= 0; i--) {
                    if (opts[i].value && opts[i].value !== '' && opts[i].value !== '0')
                        return opts[i].value;
                }
                return null;
            }""")
            if best_val:
                await form_result.f_rating.select_option(value=best_val, timeout=3000)

        log.info(f"Rating handled: type={form_result.rating_type}")
        await asyncio.sleep(0.3)
    except Exception as exc:
        log.warning(f"Rating handle failed: {exc}")


# ─────────────────────────────────────────────
# Cookie / Consent Popup Handler
# ─────────────────────────────────────────────

# Regex CHỈ khớp nút đồng ý cookie/consent thực sự.
# KHÔNG gồm: continue, close, dismiss, no thanks
# → tránh click nhầm vào link điều hướng / "Continue Reading" trong blog.
_COOKIE_BTN_RE = re.compile(
    r"\b(accept(\s+all)?|agree|i\s+agree|consent|allow(\s+all)?|got\s+it|okay?)\b",
    re.IGNORECASE,
)

# Selector trực tiếp cho các CMP framework phổ biến.
# Ưu tiên ID cố định → không bao giờ nhầm sang element khác.
_COOKIE_DIRECT_SELS = [
    # Google Funding Choices (fc-consent-root) — thường render sau 1-2s
    ".fc-consent-root .fc-cta-consent",
    "[class*='fc-cta-consent']",
    ".fc-primary-button",
    "[class*='fc-button'][class*='consent' i]",
    # Borlabs Cookie
    "#CookieBoxSaveButton",
    ".borlabs-cookie-btn-accept",
    "[class*='BorlabsCookie'] button[class*='accept' i]",
    "[id*='borlabs'] button",
    "[class*='borlabs'] button",
    # OneTrust
    "#onetrust-accept-btn-handler",
    ".onetrust-accept-btn-handler",
    # CookieBot
    "#CybotCookiebotDialogBodyButtonAccept",
    # Quantcast / Didomi
    "[data-gdpr-expression='acceptAll']",
    "#didomi-notice-agree-button",
    # Generic consent wrappers
    "[id*='cookie'] button[class*='accept' i]",
    "[id*='consent'] button[class*='accept' i]",
    "[class*='cookie-banner'] button",
    "[class*='cookie-notice'] button",
    "[class*='gdpr'] button",
]


async def handle_cookie_popup(page: Page) -> bool:
    """
    Dismiss cookie/consent popup nếu có.
    Tối ưu: dùng 1 lần JS evaluate để phát hiện có popup không,
    nếu không có → return ngay (< 50ms thay vì duyệt 14+ selectors).
    """
    if page.is_closed():
        return False

    # ── Fast-check bằng JS: có bất kỳ cookie/consent element nào không? ──
    # 1 lần evaluate nhanh hơn 14 lần is_visible() qua CDP
    try:
        has_any = await page.evaluate("""() => {
            const sels = [
                '.fc-consent-root', '.fc-primary-button', '.fc-cta-consent',
                '#BorlabsCookieBox', '.borlabs-cookie', '#CookieBoxSaveButton',
                '#onetrust-consent-sdk', '#onetrust-accept-btn-handler',
                '#CybotCookiebotDialog',
                '#didomi-notice', '[data-gdpr-expression]',
                '[class*="cookie-banner"]', '[class*="cookie-notice"]',
                '[class*="gdpr"]', '[id*="cookie"]', '[id*="consent"]'
            ];
            return sels.some(s => document.querySelector(s) !== null);
        }""")
        if not has_any:
            return False  # Không có cookie popup — return ngay
    except Exception:
        return False

    # ── Lớp 1: Selector framework cụ thể (chỉ chạy nếu fast-check dương tính) ──
    for sel in _COOKIE_DIRECT_SELS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                await loc.click(timeout=1500)
                await asyncio.sleep(0.4)
                return True
        except Exception:
            pass

    # ── Lớp 2: ARIA role=button + text regex (chỉ <button>) ──
    try:
        btn = page.get_by_role("button", name=_COOKIE_BTN_RE).first
        if await btn.is_visible():
            await btn.click(timeout=1500)
            await asyncio.sleep(0.3)
            return True
    except Exception:
        pass

    try:
        loc = page.locator("button").filter(has_text=_COOKIE_BTN_RE).first
        if await loc.is_visible():
            await loc.click(timeout=1500)
            await asyncio.sleep(0.3)
            return True
    except Exception:
        pass

    # ── Lớp 3: Trong iframe CMP (chỉ nếu main frame không tìm thấy) ──
    for frame in page.frames[1:4]:  # chỉ 3 frame đầu
        try:
            if frame.is_detached():
                continue
            for sel in _COOKIE_DIRECT_SELS[:6]:  # chỉ 6 selector CMP chính
                try:
                    loc = frame.locator(sel).first
                    if await loc.is_visible():
                        await loc.click(timeout=1500)
                        await asyncio.sleep(0.4)
                        return True
                except Exception:
                    pass
        except Exception:
            pass

    return False


# Timeout cho toàn bộ 1 URL (giây). Nếu quá thì force-kill.
_URL_TIMEOUT = 60
_MAX_URL_RECOVERIES = 3   # URL bi watchdog phuc hoi qua so lan nay → FAILED han

# Track context dang active theo worker ID — de force-close khi timeout/cancel
_active_contexts: dict[int, BrowserContext] = {}


async def _force_close_context(wid: int, label: str = "") -> None:
    """Force-close context cua worker wid neu con active. Goi tu worker_loop exception handlers."""
    ctx = _active_contexts.pop(wid, None)
    if ctx is None:
        return
    try:
        for p in ctx.pages:
            try:
                await p.unroute("**/*")
            except Exception:
                pass
            try:
                await asyncio.wait_for(p.close(), timeout=2)
            except Exception:
                pass
        await asyncio.wait_for(ctx.close(), timeout=5)
        log.info(f"[W{wid}] Force-closed zombie context ({label})")
    except Exception:
        log.warning(f"[W{wid}] Zombie context close failed ({label})")


_USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36", "Windows"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36", "Windows"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36", "Windows"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edg/131.0.0.0 Safari/537.36", "Windows"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36", "macOS"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "macOS")
]

_UA_VERSION_RE = re.compile(r'(?:Chrome|Edg)/([\d]+)')

def _get_random_ua_headers() -> tuple[str, dict[str, str]]:
    ua, platform = random.choice(_USER_AGENTS)
    
    # Extract version dynamically from UA string
    m = _UA_VERSION_RE.search(ua)
    ver = m.group(1) if m else "134"
    
    if "Edg/" in ua:
        sec_ch_ua = f'"Microsoft Edge";v="{ver}", "Not:A-Brand";v="8", "Chromium";v="{ver}"'
    else:
        sec_ch_ua = f'"Google Chrome";v="{ver}", "Not:A-Brand";v="8", "Chromium";v="{ver}"'
        
    headers = {
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua": sec_ch_ua,
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": f'"{platform}"',
    }
    return ua, headers


async def _process_url(
    url: str,
    comment: str,
    key: str,
    profile: SessionProfile,
    browser: Browser,
    result_queue: queue.Queue,
    loop: asyncio.AbstractEventLoop,
    headless: bool,
    stop_event: Optional[asyncio.Event] = None,
    bypass_name: bool = False,
    worker_id: int = 0,
    group_name: str = "",
) -> str:
    """
    Xu ly 1 URL. Tra ve final_status (STATUS_*) de caller cap nhat scheduler.
    Moi ket qua trung gian va ket qua cuoi deu duoc put vao result_queue.
    """
    t0 = time.monotonic()
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None  # khoi tao truoc try — tranh UnboundLocalError trong finally
    try:
        ua, extra_headers = _get_random_ua_headers()
        context = await browser.new_context(
            user_agent=ua,
            extra_http_headers=extra_headers,
            viewport={"width": random.randint(1280, 1440), "height": random.randint(768, 900)},
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
        )
        _active_contexts[worker_id] = context
        page = await context.new_page()

        # ── Global timeout cho mọi Playwright call trên page này ──
        page.set_default_timeout(10000)       # 10s max cho bất kỳ action nào
        page.set_default_navigation_timeout(25000)  # 25s max cho navigation (site chậm cần thời gian)

        # ── Block resources nang (images, fonts, trackers) native glob ──
        async def _abort_route(route):
            try:
                await route.abort()
            except Exception:
                pass

        # Chi file media, font va tracker. Khong block JS (kieu nhu React) va khong block CSS 
        await context.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico,mp4,webm,ogg,woff,woff2,ttf,otf}", _abort_route)
        await context.route("**/*google-analytics.com*", _abort_route)
        await context.route("**/*googletagmanager.com*", _abort_route)
        await context.route("**/*facebook.net*", _abort_route)
        await context.route("**/*doubleclick.net*", _abort_route)
        await context.route("**/*analytics*", _abort_route)
        await context.route("**/*tracker*", _abort_route)
        await context.route("**/*adservice*", _abort_route)

        if _HAS_STEALTH:
            await stealth_async(page)

        # ── 1. LOAD PAGE (20s domcontentloaded, 10s commit fallback) ──────
        # Site WordPress chậm có thể cần 15-25s — tăng timeout để không bỏ lỡ URL tốt
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            log.info(f"[{key}] LOAD OK {url} — {time.monotonic()-t0:.1f}s")
        except Exception:
            # Fallback: chỉ cần server trả header — đủ để detect form
            try:
                await page.goto(url, wait_until="commit", timeout=10000)
                log.info(f"[{key}] LOAD FALLBACK {url} — {time.monotonic()-t0:.1f}s")
            except Exception as e_load:
                log.warning(f"[{key}] LOAD FAIL {url} — {e_load}")
                result_queue.put(Result(
                    url=url, status=STATUS_NO_FORM, key=key,
                    comment_used=comment, comment_link=url,
                    error_detail="Trang khong phan hoi (timeout 30s)",
                    group_name=group_name,
                ))
                return STATUS_NO_FORM

        # ── Check stop event giữa các bước ──
        if stop_event and stop_event.is_set():
            return STATUS_REVIEW

        # Cào Title hoặc H1
        article_title = ""
        try:
            article_title = await page.locator("h1").first.inner_text(timeout=2000)
        except:
            try:
                article_title = await page.title()
            except: pass

        # Nếu cào được tiêu đề, gọi Groq AI sinh comment Realtime
        if article_title and len(article_title) > 5:
            def is_vietnamese(text: str) -> bool:
                vn_chars = "áàảãạăắằẳẵặâấầẩẫậéèẻẽẹêếềểễệíìỉĩịóòỏõọôốồổỗộơớờởỡợúùủũụưứừửữựýỳỷỹỵđ"
                text_lower = text.lower()
                return any(c in text_lower for c in vn_chars) or "nhà cái" in text_lower or "cá cược" in text_lower

            # Cấu hình WEBTITLE (nếu có)
            webtitle = ""
            try:
                import comment_gen
                if comment_gen.ENV_FILE.exists():
                    env_text = comment_gen.ENV_FILE.read_text(encoding="utf-8")
                    for line in env_text.splitlines():
                        if line.startswith("WEBTITLE="):
                            webtitle = line.partition("=")[2].strip()
            except Exception:
                pass
            
            domain_hint = webtitle if webtitle else "giải trí, cá cược, trò chơi trực tuyến"
            is_vn = is_vietnamese(key)
            
            if is_vn:
                sys_prompt = "Bạn là người đọc blog để lại bình luận tự nhiên."
                usr_prompt = (
                    f"Tôi đang đọc bài blog: '{article_title}'. Viết 1 bình luận ngắn (dưới 40 từ) bằng tiếng Việt:\n"
                    f"- Khen 1 điểm của bài, sau đó chuyển ý (dẫn dắt) để giới thiệu khéo léo về một dịch vụ có từ khóa tập trung là '{key}' (lĩnh vực chung: {domain_hint}).\n"
                    f"QUY TẮC CỨNG:\n"
                    f"1. KHÔNG được viết trực tiếp từ khóa '{key}' hay bất kỳ link nào vào bình luận vì sẽ bị trùng lặp. Hãy dùng từ nối như 'nền tảng này', 'website bên dưới', 'dịch vụ này'...\n"
                    f"2. BẮT BUỘC kết thúc câu trả lời bằng đúng chuỗi văn bản [MY_LINK] (đây là nơi hệ thống sẽ tự động cắm link)."
                )
            else:
                sys_prompt = "You are a natural blog reader leaving a comment."
                usr_prompt = (
                    f"I am reading the blog post: '{article_title}'. Write a short comment (under 40 words) in English:\n"
                    f"- Praise a specific point from the post, then seamlessly transition to recommending a service with the main keyword '{key}' (overall domain/niche: {domain_hint}).\n"
                    f"HARD RULES:\n"
                    f"1. Do NOT write the keyword '{key}' or any URLs directly in your text to avoid repetition. Refer to it indirectly like 'this platform', 'this website', 'the service below'...\n"
                    f"2. You MUST end your text with the exact placeholder [MY_LINK] (the system will inject the specific link there)."
                )

            dynamic_comment = await _call_groq_realtime(usr_prompt, sys_prompt)
            if dynamic_comment:
                # Thay thế placeholder [MY_LINK] bằng chính keyword/anchor mong muốn
                if "[MY_LINK]" in dynamic_comment:
                    if profile.host:
                        comment = dynamic_comment.replace("[MY_LINK]", f"<a href=\"{profile.host}\">{key}</a>")
                    else:
                        comment = dynamic_comment.replace("[MY_LINK]", key)
                else:
                    # Fallback (AI quên in MY_LINK)
                    if profile.host:
                        comment = f"{dynamic_comment} <a href=\"{profile.host}\">{key}</a>"
                    else:
                        comment = f"{dynamic_comment} {key}"

        # ── 2. SCROLL + SETTLE — trigger lazy-load trước khi tìm form ──
        try:
            await page.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'})")
            await asyncio.sleep(0.8)  # chờ IntersectionObserver / lazy JS render
        except Exception:
            pass

        # ── 2.5. Cookie popup (chạy sớm, tránh overlay chặn click) ──────
        if not page.is_closed():
            try:
                await asyncio.wait_for(handle_cookie_popup(page), timeout=5)
            except Exception:
                pass

        # ── 3. QUICK CHECK: có dấu hiệu comment area? ───────────
        # Kiểm tra rộng: form+textarea HOẶC #respond/#commentform trong DOM
        # (kể cả hidden — vì có thể cần click reveal button)
        form_hint = 0  # 0=nothing, 1=has section/id, 2=has visible form+textarea
        try:
            form_hint = await page.evaluate("""() => {
                // Ưu tiên: form có textarea + không display:none
                const fs = document.querySelectorAll('form');
                for (const f of fs) {
                    if (f.querySelector('textarea')) {
                        const st = getComputedStyle(f);
                        if (st.display !== 'none' && st.visibility !== 'hidden')
                            return 2;
                    }
                }
                // Có section comment nhưng form có thể ẩn (cần click reveal)
                if (document.querySelector('#respond, #commentform, #comments, .comment-respond, [id*="comment"], .comments-area, #reply-title, .review-form, #review_form_wrapper, .woocommerce-Reviews, [class*="review"]'))
                    return 1;
                // Có textarea bất kỳ (ngoài form)
                if (document.querySelector('textarea[name*="comment" i], textarea[id*="comment" i], textarea[name*="message" i], textarea[name*="content" i], textarea[placeholder*="comment" i]'))
                    return 1;
                return 0;
            }""")
        except Exception:
            form_hint = 0

        if form_hint == 0:
            # Smart scroll: 4-stop jump (30%/60%/90%/100%) instead of viewport-by-viewport
            try:
                total_h = await page.evaluate("() => document.body.scrollHeight") or 5000
                for pct in (0.3, 0.6, 0.9, 1.0):
                    target = int(total_h * pct)
                    await page.evaluate(f"window.scrollTo({{top: {target}, behavior: 'instant'}})")
                    await asyncio.sleep(0.15)
                    # Check if page grew (infinite scroll / lazy load)
                    new_h = await page.evaluate("() => document.body.scrollHeight")
                    if new_h > total_h:
                        total_h = new_h
                await page.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'})")
                await asyncio.sleep(0.8)
                form_hint = await page.evaluate("""() => {
                    const fs = document.querySelectorAll('form');
                    for (const f of fs) {
                        if (f.querySelector('textarea')) return 2;
                    }
                    if (document.querySelector('#respond, #commentform, #comments, .comment-respond, [id*="comment"], .review-form, #review_form_wrapper, .woocommerce-Reviews'))
                        return 1;
                    if (document.querySelector('textarea'))
                        return 1;
                    return 0;
                }""")
            except Exception:
                pass
            if form_hint == 0:
                log.info(f"[{key}] NO_FORM (quick check) {url} — {time.monotonic()-t0:.1f}s")
                result_queue.put(Result(
                    url=url, status=STATUS_NO_FORM, key=key,
                    comment_used=comment, comment_link=url,
                    error_detail="Trang khong co form comment",
                    group_name=group_name,
                ))
                return STATUS_NO_FORM
        log.debug(f"[{key}] quick check form_hint={form_hint} — {time.monotonic()-t0:.1f}s")

        # ── 3.5. Click reveal button nếu form ẩn (WordPress pattern) ──
        if form_hint == 1:  # có section nhưng form chưa visible
            revealed = False
            _REVEAL_SELS = [
                "a[href='#respond']",
                "a[href*='#comment']",
                "a[href*='#review']",
                ".comment-reply-link",
                "#reply-title a",
                "a:has-text('Leave a Reply')",
                "a:has-text('Leave a Comment')",
                "a:has-text('Add a Comment')",
                "a:has-text('Write a Comment')",
                "a:has-text('Bình luận')",
                "a:has-text('Viết bình luận')",
                "a:has-text('Để lại bình luận')",
                "a:has-text('Đánh giá')",
                "a:has-text('Viết đánh giá')",
                "button:has-text('Leave a Reply')",
                "button:has-text('Leave a Comment')",
                "button:has-text('Bình luận')",
                "button:has-text('Viết đánh giá')",
                ".show-comment-form",
                "[data-action='show-comment-form']",
                ".review-link",
            ]
            for sel in _REVEAL_SELS:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=500):
                        await loc.click(timeout=3000)
                        await asyncio.sleep(1.0)  # chờ form xuất hiện
                        revealed = True
                        log.info(f"[{key}] REVEAL clicked: {sel}")
                        break
                except Exception:
                    pass
            # Nếu không click được reveal, scroll xuống #respond
            if not revealed:
                try:
                    await page.evaluate("""
                        const el = document.querySelector('#respond, #commentform, .comment-respond');
                        if (el) el.scrollIntoView({behavior: 'instant', block: 'center'});
                    """)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

        # ── Check stop event giữa các bước ──
        if stop_event and stop_event.is_set():
            return STATUS_REVIEW

        # ── 4. FIND FORM (2 lần, cap 15s total) ─────────────
        form_result = None
        form_t0 = time.monotonic()
        for attempt in range(2):
            if page.is_closed():
                break
            if time.monotonic() - form_t0 > 15:
                log.warning(f"[{key}] FORM DETECT timeout cap 15s — {url}")
                break
            try:
                # Scroll xuống cuối trang trước mỗi lần detect
                await page.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'})")
                await asyncio.sleep(0.3)
                fr = await asyncio.wait_for(
                    form_detect.find_comment_form(page), timeout=8
                )
                if fr.found:
                    form_result = fr
                    break
            except asyncio.TimeoutError:
                log.warning(f"[{key}] form_detect timeout attempt={attempt} — {url}")
                break
            except Exception:
                pass
            if attempt == 0:
                await asyncio.sleep(0.5)

        if form_result is None or not form_result.found:
            log.info(f"[{key}] NO_FORM (detect) {url} — {time.monotonic()-t0:.1f}s")
            result_queue.put(Result(
                url=url, status=STATUS_NO_FORM, key=key,
                comment_used=comment, comment_link=url,
                error_detail="Khong tim thay comment form",
                group_name=group_name,
            ))
            return STATUS_NO_FORM
        log.info(f"[{key}] FORM found strategy={form_result.strategy} — {time.monotonic()-t0:.1f}s")

        # ── 4. Kiểm tra captcha → skip ngay (GUI chưa hỗ trợ giải) ──
        if form_result.has_captcha:
            log.info(f"[{key}] CAPTCHA detected, skip {url}")
            result_queue.put(Result(
                url=url, status=STATUS_CAPTCHA_FAILED, key=key,
                comment_used=comment, comment_link=url,
                error_detail="Captcha detected — skip",
                group_name=group_name,
            ))
            return STATUS_CAPTCHA_FAILED

        # ── Check stop event giữa các bước ──
        if stop_event and stop_event.is_set():
            return STATUS_REVIEW

        # ── 4b. Akismet signals: mouse/focus on form + reading pause ──
        if form_result.form:
            await human_sim.emit_form_signals(page, form_result.form)
        await asyncio.sleep(random.uniform(0.3, 0.5))

        # ── 5. FILL + SUBMIT (flat — no nested class/closure) ──

        if page.is_closed():
            result_queue.put(Result(
                url=url, status=STATUS_REVIEW, key=key,
                comment_used=comment, comment_link=url,
                error_detail="Page closed truoc khi dien form",
                group_name=group_name,
            ))
            return STATUS_REVIEW

        # Fill name/email/url — retry với JS fallback nếu fill() fail
        async def _robust_fill(field_loc, value, field_name):
            """Fill field với retry: fast_fill → JS set value. Return True nếu thành công."""
            if not field_loc or not value:
                return True  # không cần fill
            try:
                await human_sim.fast_fill(field_loc, value, page=page)
                return True
            except Exception:
                pass
            # JS fallback: set value trực tiếp qua evaluate
            try:
                await field_loc.evaluate(
                    "(el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }",
                    value,
                )
                log.info(f"[{key}] {field_name} filled via JS fallback")
                return True
            except Exception as exc:
                log.warning(f"[{key}] {field_name} fill FAILED: {exc}")
                return False

        # ── Rating (nếu form yêu cầu) ──
        await _handle_rating(page, form_result)

        # ── Progressive field reveal ──
        # Một số trang chỉ hiện field tiếp theo khi click field trước
        if form_result.f_comment:
            try:
                await form_result.f_comment.click(timeout=3000)
                await asyncio.sleep(0.5)
            except Exception:
                pass
            # Re-detect fields sau khi click comment
            if form_result.f_name is None or form_result.f_email is None:
                try:
                    _re_fields = await form_detect._extract_fields(page, form_result.form)
                    if form_result.f_name is None and _re_fields["name"]:
                        form_result.f_name = _re_fields["name"]
                    if form_result.f_email is None and _re_fields["email"]:
                        form_result.f_email = _re_fields["email"]
                    if form_result.f_url is None and _re_fields["url"]:
                        form_result.f_url = _re_fields["url"]
                except Exception:
                    pass

        # Name obfuscation (bypass keyword filter)
        name_to_fill = profile.name
        if bypass_name and profile.name:
            name_to_fill = _obfuscate_name(profile.name)
            log.debug(f"[{key}] Name obfuscated: {profile.name!r} -> {name_to_fill!r}")

        await _robust_fill(form_result.f_name, name_to_fill, "name")

        # Re-detect sau khi fill name (progressive reveal)
        if form_result.f_email is None and profile.email:
            await asyncio.sleep(0.3)
            try:
                _re_fields = await form_detect._extract_fields(page, form_result.form)
                if _re_fields["email"]:
                    form_result.f_email = _re_fields["email"]
                if form_result.f_url is None and _re_fields["url"]:
                    form_result.f_url = _re_fields["url"]
            except Exception:
                pass

        email_ok = await _robust_fill(form_result.f_email, profile.email, "email")

        # Re-detect URL sau khi fill email (progressive reveal)
        if form_result.f_url is None and profile.host:
            await asyncio.sleep(0.3)
            try:
                _re_fields = await form_detect._extract_fields(page, form_result.form)
                if _re_fields["url"]:
                    form_result.f_url = _re_fields["url"]
            except Exception:
                pass

        await _robust_fill(form_result.f_url, profile.host, "website")

        # Email là bắt buộc trên hầu hết WordPress — nếu fail thì skip sớm
        if not email_ok and profile.email:
            log.warning(f"[{key}] SKIP {url} — email fill failed")
            result_queue.put(Result(
                url=url, status=STATUS_FORM_ERROR, key=key,
                comment_used=comment, comment_link=url,
                error_detail="Khong dien duoc email — skip URL",
                group_name=group_name,
            ))
            return STATUS_FORM_ERROR

        # Fill comment (BẮT BUỘC)
        if form_result.f_comment is None:
            result_queue.put(Result(
                url=url, status=STATUS_NO_FORM, key=key,
                comment_used=comment, comment_link=url,
                error_detail="Khong tim thay textarea comment",
                group_name=group_name,
            ))
            return STATUS_NO_FORM

        try:
            await human_sim.type_text(form_result.f_comment, comment, page=page)
        except Exception as exc:
            result_queue.put(Result(
                url=url, status=STATUS_FORM_ERROR, key=key,
                comment_used=comment, comment_link=url,
                error_detail=f"Fill comment loi: {exc}",
                group_name=group_name,
            ))
            return STATUS_FORM_ERROR

        # Verify comment đã ghi vào textarea
        try:
            val = await form_result.f_comment.input_value(timeout=2000)
            if not val or len(val.strip()) < 3:
                result_queue.put(Result(
                    url=url, status=STATUS_FORM_ERROR, key=key,
                    comment_used=comment, comment_link=url,
                    error_detail=f"Comment khong ghi vao textarea (len={len(val) if val else 0})",
                    group_name=group_name,
                ))
                return STATUS_FORM_ERROR
        except Exception:
            pass  # Không verify được — vẫn submit

        # Submit
        if form_result.f_submit is None:
            result_queue.put(Result(
                url=url, status=STATUS_NO_FORM, key=key,
                comment_used=comment, comment_link=url,
                error_detail="Khong tim thay nut submit",
                group_name=group_name,
            ))
            return STATUS_NO_FORM

        if page.is_closed():
            result_queue.put(Result(
                url=url, status=STATUS_REVIEW, key=key,
                comment_used=comment, comment_link=url,
                error_detail="Page closed truoc khi submit",
                group_name=group_name,
            ))
            return STATUS_REVIEW

        try:
            await human_sim.human_click(form_result.f_submit, no_wait_after=True)
            log.info(f"[{key}] SUBMIT clicked — {time.monotonic()-t0:.1f}s")
        except Exception as exc:
            exc_str = str(exc).lower()
            # Overlay (cookie/dialog) chặn click → thử dismiss lại rồi JS click
            if "intercept" in exc_str or "pointer" in exc_str or "cover" in exc_str:
                log.warning(f"[{key}] SUBMIT blocked by overlay, retry dismiss — {url}")
                try:
                    await handle_cookie_popup(page)
                    await asyncio.sleep(0.5)
                    await form_result.f_submit.dispatch_event("click")
                    log.info(f"[{key}] SUBMIT JS-click (overlay fallback) — {time.monotonic()-t0:.1f}s")
                except Exception as exc2:
                    log.warning(f"[{key}] RESULT REVIEW (SUBMIT overlay fail) {url} — {exc2}")
                    result_queue.put(Result(
                        url=url, status=STATUS_REVIEW, key=key,
                        comment_used=comment, comment_link=url,
                        error_detail=f"Click submit bi overlay chan: {exc2}",
                        group_name=group_name,
                    ))
                    return STATUS_REVIEW
            else:
                log.warning(f"[{key}] RESULT REVIEW (SUBMIT fail) {url} — {exc}")
                result_queue.put(Result(
                    url=url, status=STATUS_REVIEW, key=key,
                    comment_used=comment, comment_link=url,
                    error_detail=f"Click submit loi: {exc}",
                    group_name=group_name,
                ))
                return STATUS_REVIEW

        # ── 6. Cho phan hoi ──────────────────────────

        final_status = STATUS_REVIEW
        comment_link = url
        url_before = page.url

        try:
            # Chờ URL thay đổi thành dạng #comment-ID (tối đa 15s)
            await page.wait_for_url(
                re.compile(r"#comment-\d+", re.IGNORECASE),
                timeout=15000,
            )
            # Đợi page load xong hoàn toàn rồi mới lấy URL
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            comment_link = page.url
            final_status = STATUS_SUCCESS
        except Exception:
            # wait_for_url timeout → chờ page settle rồi kiểm tra
            # networkidle tốt hơn sleep(1) cứng: đợi đúng lúc page xong AJAX
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass  # Không đạt networkidle — kiểm tra ngay
            try:
                current_url = page.url
            except Exception:
                # Page da bi dong (stop_event hoac crash)
                result_queue.put(Result(
                    url=url, status=STATUS_REVIEW, key=key,
                    comment_used=comment, comment_link=url,
                    error_detail="Page closed before response could be read",
                    group_name=group_name,
                ))
                return STATUS_REVIEW

            if _COMMENT_ID_RE.search(current_url):
                comment_link = current_url
                final_status = STATUS_SUCCESS
            else:
                # Fetch body text 1 lan — dung chung cho ca success & error check
                body_text = await _get_page_text(page)
                if current_url != url_before and current_url != url:
                    comment_link = current_url
                    if _text_contains(body_text, _SUCCESS_TEXTS):
                        final_status = STATUS_MODERATION
                    else:
                        final_status = STATUS_REVIEW
                elif _text_contains(body_text, _SUCCESS_TEXTS):
                    final_status = STATUS_MODERATION
                elif _text_contains(body_text, _ERROR_TEXTS):
                    final_status = STATUS_FORM_ERROR
                else:
                    final_status = STATUS_REVIEW

        # ── 9. Captcha fail after solve ──────────────
        if form_result.has_captcha and final_status != STATUS_SUCCESS:
            final_status = STATUS_CAPTCHA_FAILED

        # ── 10. Ping IndexNow (neu comment success/mod) ──
        if final_status in (STATUS_SUCCESS, STATUS_MODERATION):
            asyncio.create_task(_ping_indexnow(comment_link, key))

        log.info(f"[{key}] RESULT {final_status} {url} — {time.monotonic()-t0:.1f}s total")
        result_queue.put(Result(
            url=url,
            status=final_status,
            comment_used=comment,
            comment_link=comment_link,
            strategy=form_result.strategy,
            key=key,
            group_name=group_name,
        ))
        return final_status

    except _TargetClosedError:
        log.warning(f"[{key}] RESULT REVIEW (TargetClosedError) {url} — {time.monotonic()-t0:.1f}s")
        result_queue.put(Result(
            url=url, status=STATUS_REVIEW, key=key,
            comment_used=comment, comment_link=url,
            error_detail="TargetClosedError: browser/context da bi dong",
            group_name=group_name,
        ))
        return STATUS_REVIEW
    except Exception as exc:
        log.error(f"[{key}] RESULT REVIEW (UNHANDLED) {url} — {exc} — {time.monotonic()-t0:.1f}s", exc_info=True)
        result_queue.put(Result(
            url=url, status=STATUS_REVIEW, key=key,
            comment_used=comment, comment_link=url,
            error_detail=str(exc),
            group_name=group_name,
        ))
        return STATUS_REVIEW
    finally:
        _active_contexts.pop(worker_id, None)
        # ── Aggressive cleanup: free renderer memory ASAP ──
        if page:
            try:
                await page.unroute("**/*")
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    page.goto("about:blank", wait_until="commit"), timeout=2
                )
            except Exception:
                pass
            try:
                await asyncio.wait_for(page.close(), timeout=3)
            except Exception:
                pass
            page = None
        if context:
            try:
                await asyncio.wait_for(context.close(), timeout=5)
            except Exception:
                log.warning(f"[{key}] context.close() timeout — force skip")
            context = None


# ─────────────────────────────────────────────
# Session runner — chay trong background thread
# ─────────────────────────────────────────────

async def _run_async(
    scheduler: "JobScheduler",
    result_queue: queue.Queue,
    max_workers: int,
    headless: bool,
    stop_event: asyncio.Event,
    bypass_name: bool = False,
    group_name: str = "",
) -> None:
    """Coroutine chinh: khoi browser, chay worker loops song song.
    Co watchdog: neu 5 phut khong co URL nao hoan thanh → huy worker,
    phuc hoi URL dang treo, khoi dong lai worker moi (khong reset tu dau).
    """
    loop = asyncio.get_event_loop()

    WATCHDOG_TIMEOUT = 180   # 3 phut khong co ket qua → restart (voi _URL_TIMEOUT=60s, 3 URL fail lien tiep = signal)
    WATCHDOG_CHECK   = 20    # kiem tra moi 20 giay

    async with async_playwright() as pw:
        # Vong lap ngoai cung: dam bao browser duoc khoi tao lai hoan toan sau moi lan restart lon
        MAX_OUTER_RESTARTS = 100
        outer_restarts = 0
        _RESTART_EVERY = 100  # Giam xuong de giai phong RAM som hon (mac dinh 200)

        while not stop_event.is_set():
            if scheduler.is_done():
                break

            log.info(f"[SESSION] Launching new browser process (Restart #{outer_restarts})")
            browser: Browser = await pw.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-translate",
                    "--metrics-recording-only",
                    "--js-flags=--max-old-space-size=256",
                    "--disable-features=TranslateUI",
                    "--disable-ipc-flooding-protection",
                    "--memory-pressure-off",
                ],
            )
            
            try:
                # ── Worker loop logic starts here ──
                _urls_since_restart = [0]
                _restart_lock = asyncio.Lock()
                while not stop_event.is_set():
                    if scheduler.is_done():
                        break

                    # Browser crash → dừng ngay
                    if not browser.is_connected():
                        log.error("[SESSION] Browser process died — stopping inner loop")
                        break

                    last_result_time: list[float] = [time.monotonic()]
                    watchdog_triggered = asyncio.Event()

                    # ── Worker loop ─────────────────────────────────────────
                    async def worker_loop(wid: int, _lrt=last_result_time, _wt=watchdog_triggered) -> None:
                        empty_retries = 0
                        MAX_EMPTY_RETRIES = 25
                        while not stop_event.is_set() and not _wt.is_set():
                            job = scheduler.next_job()
                            if job is None:
                                if scheduler.is_done(): break
                                empty_retries += 1
                                if empty_retries > MAX_EMPTY_RETRIES: break
                                await asyncio.sleep(10)
                                continue

                            empty_retries = 0
                            url, comment, key = job
                            profile = scheduler.get_profile(key)
                            try:
                                final_status = await asyncio.wait_for(
                                    _process_url(url, comment, key, profile, browser, result_queue, loop, headless, stop_event=stop_event, bypass_name=bypass_name, worker_id=wid, group_name=group_name),
                                    timeout=_URL_TIMEOUT,
                                )
                            except (asyncio.TimeoutError, Exception) as exc:
                                label = "timeout" if isinstance(exc, asyncio.TimeoutError) else "unhandled"
                                await _force_close_context(wid, label)
                                result_queue.put(Result(url=url, status=STATUS_REVIEW, key=key, comment_used=comment, error_detail=f"Worker error: {exc}", group_name=group_name))
                                scheduler.return_comment(key, comment)
                                scheduler.finalize_job(url)
                                _lrt[0] = time.monotonic()
                                continue

                            if final_status in (STATUS_SUCCESS, STATUS_MODERATION):
                                scheduler.record_success(key, comment)
                            else:
                                scheduler.return_comment(key, comment)
                            scheduler.finalize_job(url)
                            _lrt[0] = time.monotonic()
                            result_queue.put(Result(status=STATUS_PROGRESS, key=key, group_name=group_name))

                            async with _restart_lock:
                                _urls_since_restart[0] += 1
                                if _urls_since_restart[0] >= _RESTART_EVERY:
                                    log.info(f"[W{wid}] Da xu ly {_RESTART_EVERY} URLs — trigger restart browser")
                                    _wt.set()
                                    break

                    async def watchdog_loop(_lrt=last_result_time, _wt=watchdog_triggered) -> None:
                        while not stop_event.is_set() and not _wt.is_set():
                            await asyncio.sleep(WATCHDOG_CHECK)
                            if time.monotonic() - _lrt[0] >= WATCHDOG_TIMEOUT:
                                _wt.set()
                                break

                    tasks = [asyncio.create_task(worker_loop(i)) for i in range(max_workers)]
                    wd_task = asyncio.create_task(watchdog_loop())

                    try:
                        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=28800)
                    except Exception:
                        pass
                    
                    if not wd_task.done(): wd_task.cancel()
                    
                    # Cleanup after tasks/watchdog
                    for t in tasks:
                        if not t.done(): t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

                    if watchdog_triggered.is_set() and not stop_event.is_set():
                        recovered, dead = scheduler.recover_inflight()
                        _urls_since_restart[0] = 0
                        await asyncio.sleep(2)
                        break # Exit inner loop -> close browser -> start over

                    if scheduler.is_done():
                        break
                
                import gc
                gc.collect()

            finally:
                log.info("[SESSION] Closing browser process for restart/cleanup")
                try:
                    await asyncio.wait_for(browser.close(), timeout=10)
                except Exception:
                    pass
                _active_contexts.clear()
                import gc
                gc.collect()

            outer_restarts += 1
            if outer_restarts >= MAX_OUTER_RESTARTS:
                log.error(f"[SESSION] Qua {MAX_OUTER_RESTARTS} lan restart — force stop")
                break

        log.info(f"[SESSION] Done. Final Status: {scheduler.status_summary()}")

    result_queue.put(Result(status=STATUS_DONE, group_name=group_name))


def run_session(
    scheduler: "JobScheduler",
    result_queue: queue.Queue,
    max_workers: int = 3,
    headless: bool = False,
    stop_event_holder: list | None = None,
    bypass_name: bool = False,
    group_name: str = "",
) -> None:
    """
    Entry point cho background thread.
    scheduler: JobScheduler chua tat ca (url, comment, key) triplets va per-key profiles.
    stop_event_holder: list de GUI co the set stop event.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Suppress "Future exception was never retrieved" cho TargetClosedError
    # phat sinh tu orphaned route callbacks sau khi context/page da dong.
    def _loop_exc_handler(loop, context):
        exc = context.get("exception")
        if isinstance(exc, _TargetClosedError):
            return  # im lang — day chi la cleanup race condition
        msg = context.get("message", "")
        if "TargetClosedError" in msg or "Target page" in msg:
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(_loop_exc_handler)

    stop_event = asyncio.Event()
    if stop_event_holder is not None:
        stop_event_holder.append((stop_event, loop))

    try:
        loop.run_until_complete(
            _run_async(scheduler, result_queue, max_workers, headless, stop_event, bypass_name, group_name)
        )
    finally:
        loop.close()
