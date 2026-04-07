"""
comment_gen.py — Sinh comment blog tu dong bang Groq API.

- Key rotation khi gap rate limit (429)
- Batch processing (max 10 comment/call)
- Post-processing: strip numbering, fix keyword case
- .env management cho API keys
"""

from __future__ import annotations

import re
import time
import threading
from pathlib import Path
from dataclasses import dataclass, field

from groq import Groq

# ── Constants ──────────────────────────────────────────────────────────────────

BATCH_SIZE = 10
MAX_RETRY = 5
RETRY_WAIT = 10  # seconds
ENV_FILE = Path(".env")

# ── Prompt templates ───────────────────────────────────────────────────────────

SYSTEM_VI = (
    'Bạn là trợ lý tạo comment blog tự nhiên cho trang web game trò chơi trực tuyến.\n'
    'BỐI CẢNH: Trang web cung cấp các trò chơi trực tuyến, cá cược thể thao, game bài, '
    'xổ số, casino online và nhiều hình thức giải trí số khác. '
    'Khi keyword liên quan đến một chủ đề cụ thể (ví dụ: bóng đá, thể thao, game bài...), '
    'hãy viết comment giới thiệu trang web như một nền tảng chơi/xem/theo dõi chủ đề đó '
    '(chơi cá cược bóng đá, xem kết quả trực tiếp, chơi game bài trực tuyến...). '
    'Nếu keyword chung chung, hãy giới thiệu trang web game giải trí trực tuyến tổng hợp.\n'
    'QUY TẮC CỨNG — KHÔNG ĐƯỢC VI PHẠM:\n'
    '1. Keyword "{keyword}" PHẢI xuất hiện trong mỗi comment, viết Y CHANG "{keyword}" '
    '— không viết hoa, không capitalize, không thay đổi bất kỳ ký tự nào dù ở đầu câu.\n'
    '2. Mỗi comment là một đoạn văn giới thiệu trang web game trực tuyến liên quan đến "{keyword}", đúng 60 từ tiếng Việt.\n'
    '3. Viết bằng tiếng Việt, giọng tự nhiên như người dùng thật chia sẻ trải nghiệm.\n'
    '4. Tuyệt đối KHÔNG thêm số thứ tự, KHÔNG thêm dấu ngoặc kép bao quanh comment, KHÔNG dùng markdown hay bất kỳ thẻ HTML (kể cả <a>) nào.\n'
    '5. Mỗi comment nằm trên ĐÚNG 1 dòng. Giữa các comment là 1 dòng trống.\n'
    '6. Đếm lại trước khi trả lời: mỗi comment phải đúng 60 từ.\n'
    '7. Mỗi comment phải KHÁC NHAU về nội dung và cách diễn đạt.'
)

USER_VI = (
    'Tạo {batch_size} comment, mỗi cái đúng 60 từ, giới thiệu trang web game trò chơi '
    'trực tuyến liên quan đến "{keyword}". Viết như người dùng thật chia sẻ, tự nhiên. '
    'Mỗi comment 1 dòng, cách nhau bởi dòng trống.'
)

SYSTEM_EN = (
    'You are a natural blog comment writing assistant for an online gaming website.\n'
    'CONTEXT: The website offers online games, sports betting, card games, lottery, '
    'online casino and other digital entertainment. '
    'When the keyword relates to a specific topic (e.g. football, sports, card games...), '
    'write comments introducing the site as a platform for playing/watching/following that topic '
    '(football betting, live scores, online card games...). '
    'If the keyword is generic, introduce it as a comprehensive online gaming entertainment site.\n'
    'HARD RULES — DO NOT VIOLATE:\n'
    '1. The keyword "{keyword}" MUST appear in every comment, written EXACTLY as "{keyword}" '
    '— do NOT capitalize, do NOT change any character even at the start of a sentence.\n'
    '2. Each comment is a paragraph introducing an online gaming site related to "{keyword}", exactly 60 English words.\n'
    '3. Write ENTIRELY in English, natural tone like a real user sharing experience.\n'
    '4. Absolutely do NOT add numbering, do NOT use quotes, do NOT use markdown or any HTML tags (including <a>).\n'
    '5. Each comment on EXACTLY 1 line. Separate comments with 1 blank line.\n'
    '6. Re-count before answering: each comment must be exactly 60 words.\n'
    '7. Each comment must be DIFFERENT in content and phrasing.'
)

USER_EN = (
    'Generate {batch_size} comments, each exactly 60 words, introducing an online gaming '
    'website related to "{keyword}". Write like a real user sharing experience, natural tone. '
    'One comment per line, separated by blank lines.'
)

# ── Regex for stripping numbering ──────────────────────────────────────────────

_NUMBERING_RE = re.compile(r'^\d+[\.\)\-\:]\s*')


# ── Data types ─────────────────────────────────────────────────────────────────

@dataclass
class KeywordTask:
    keyword: str
    count: int = 10
    lang: str = "vi"


@dataclass
class GenProgress:
    """Sent from worker thread to UI."""
    keyword: str = ""
    comments: list[str] = field(default_factory=list)  # new comments this batch
    done: int = 0       # total done so far (all keywords)
    total: int = 0      # grand total
    error: str = ""
    finished: bool = False


# ── .env helpers ───────────────────────────────────────────────────────────────

def load_keys_from_env() -> list[str]:
    """Read GROQ_API_KEY, GROQ_API_KEY_2, ... from .env file."""
    if not ENV_FILE.exists():
        return []
    keys: list[str] = []
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith("GROQ_API_KEY"):
            _, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            if val:
                keys.append(val)
    return keys


def save_keys_to_env(keys: list[str]) -> None:
    """Write keys to .env, preserving non-GROQ lines."""
    other_lines: list[str] = []
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip().upper()
            if stripped.startswith("GROQ_API_KEY"):
                continue
            other_lines.append(line)

    new_lines = list(other_lines)
    for i, key in enumerate(keys):
        if not key.strip():
            continue
        suffix = "" if i == 0 else f"_{i + 1}"
        new_lines.append(f"GROQ_API_KEY{suffix}={key.strip()}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ── Keyword parser ─────────────────────────────────────────────────────────────

def parse_keywords(text: str) -> list[KeywordTask]:
    """Parse keyword input lines, format: keyword-count-lang."""
    tasks: list[KeywordTask] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        parts = line.rsplit("-", 1)
        lang = "vi"
        rest = line

        # Check if last part is a language code
        if len(parts) == 2 and parts[1].strip().lower() in ("vi", "en"):
            lang = parts[1].strip().lower()
            rest = parts[0].strip()

        # Now check if last part of rest is a number (count)
        parts2 = rest.rsplit("-", 1)
        count = 10
        keyword = rest

        if len(parts2) == 2 and parts2[1].strip().isdigit():
            count = int(parts2[1].strip())
            keyword = parts2[0].strip()

        if keyword:
            tasks.append(KeywordTask(keyword=keyword, count=count, lang=lang))

    return tasks


# ── Post-processing ────────────────────────────────────────────────────────────

def process_response(raw: str, keyword: str) -> list[str]:
    """Clean up API response into list of comments."""
    comments: list[str] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Strip surrounding quotes
        if (line.startswith('"') and line.endswith('"')) or \
           (line.startswith("'") and line.endswith("'")):
            line = line[1:-1].strip()
        # Remove numbering
        line = _NUMBERING_RE.sub("", line).strip()
        if not line:
            continue
        # Fix keyword case
        line = re.sub(re.escape(keyword), keyword, line, flags=re.IGNORECASE)
        comments.append(line)
    return comments


# ── API call with key rotation ─────────────────────────────────────────────────

def _call_groq(
    keys: list[str],
    key_index: int,
    system_prompt: str,
    user_prompt: str,
    batch_size: int,
    stop_event: threading.Event,
) -> tuple[str, int]:
    """
    Call Groq API with key rotation on rate limit.
    Returns (response_text, updated_key_index).
    Raises RuntimeError if all retries exhausted.
    """
    retry_count = 0
    start_index = key_index

    while retry_count < MAX_RETRY:
        if stop_event.is_set():
            raise InterruptedError("Stopped by user")

        try:
            client = Groq(api_key=keys[key_index])
            chat = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.9,
                max_tokens=batch_size * 250,
            )
            return chat.choices[0].message.content.strip(), key_index

        except Exception as exc:
            err_str = str(exc).lower()
            is_rate_limit = "429" in err_str or "rate" in err_str

            if not is_rate_limit:
                raise

            # Rate limit — rotate key
            key_index = (key_index + 1) % len(keys)
            retry_count += 1

            if retry_count >= MAX_RETRY:
                raise RuntimeError(
                    f"Da thu {MAX_RETRY} lan, tat ca key deu bi rate limit."
                )

            # If we've cycled through all keys, wait before retrying
            if key_index == start_index:
                for _ in range(RETRY_WAIT):
                    if stop_event.is_set():
                        raise InterruptedError("Stopped by user")
                    time.sleep(1)

    raise RuntimeError("Retry exhausted")


# ── Main generation function (runs in thread) ─────────────────────────────────

def generate_comments(
    keys: list[str],
    tasks: list[KeywordTask],
    callback,
    stop_event: threading.Event,
) -> None:
    """
    Generate comments for all keyword tasks.
    callback(GenProgress) is called from the worker thread — caller must
    schedule UI updates via root.after().
    """
    if not keys:
        callback(GenProgress(error="Chua nhap API key.", finished=True))
        return
    if not tasks:
        callback(GenProgress(error="Chua nhap keyword.", finished=True))
        return

    grand_total = sum(t.count for t in tasks)
    grand_done = 0
    key_index = 0

    for task in tasks:
        if stop_event.is_set():
            break

        remaining = task.count
        while remaining > 0:
            if stop_event.is_set():
                break

            batch_size = min(BATCH_SIZE, remaining)

            # Build prompts
            if task.lang == "en":
                sys_p = SYSTEM_EN.format(keyword=task.keyword)
                usr_p = USER_EN.format(batch_size=batch_size, keyword=task.keyword)
            else:
                sys_p = SYSTEM_VI.format(keyword=task.keyword)
                usr_p = USER_VI.format(batch_size=batch_size, keyword=task.keyword)

            try:
                raw, key_index = _call_groq(
                    keys, key_index, sys_p, usr_p, batch_size, stop_event,
                )
            except InterruptedError:
                break
            except Exception as exc:
                callback(GenProgress(
                    keyword=task.keyword,
                    error=f"[{task.keyword}] {exc}",
                    done=grand_done,
                    total=grand_total,
                ))
                break

            comments = process_response(raw, task.keyword)
            # Only take what we need
            comments = comments[:remaining]

            grand_done += len(comments)
            remaining -= len(comments)

            callback(GenProgress(
                keyword=task.keyword,
                comments=comments,
                done=grand_done,
                total=grand_total,
            ))

    callback(GenProgress(done=grand_done, total=grand_total, finished=True))
