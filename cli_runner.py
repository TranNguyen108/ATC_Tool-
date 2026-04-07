"""
cli_runner.py — Chay headless tren server/Colab, khong can GUI Tkinter.
Usage: python cli_runner.py --config config.json --urls urls.txt
"""
from __future__ import annotations

import argparse
import json
import queue
import threading
import time
from pathlib import Path

from worker import (
    JobScheduler, SessionProfile, run_session,
    STATUS_SUCCESS, STATUS_MODERATION, STATUS_DONE,
    STATUS_PROGRESS, STATUS_NO_FORM, STATUS_REVIEW,
    STATUS_CAPTCHA, STATUS_CAPTCHA_FAILED, STATUS_FORM_ERROR,
)

# ── ANSI colors (hoat dong tren Linux/Colab) ──
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

STATUS_COLOR = {
    STATUS_SUCCESS:        GREEN,
    STATUS_MODERATION:     CYAN,
    STATUS_NO_FORM:        YELLOW,
    STATUS_REVIEW:         YELLOW,
    STATUS_FORM_ERROR:     RED,
    STATUS_CAPTCHA:        RED,
    STATUS_CAPTCHA_FAILED: RED,
}


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _result_listener(
    result_queue: queue.Queue,
    scheduler: JobScheduler,
    success_log: list,
    fail_log: list,
) -> None:
    """Chay trong thread rieng, lang nghe ket qua va in ra terminal."""
    total_target = sum(t for _, t in scheduler._keys)
    total_done   = 0

    while True:
        result = result_queue.get()

        if result.status == STATUS_DONE:
            _log(f"{BOLD}=== SESSION DONE ==={RESET}")
            _log(f"Thanh cong: {GREEN}{total_done}{RESET} / {total_target}")
            _log(f"That bai  : {RED}{len(fail_log)}{RESET}")
            break

        if result.status == STATUS_PROGRESS:
            success_map = scheduler.get_success()
            parts = [f"{k}:{v}/{scheduler.get_target(k)}" for k, v in success_map.items()]
            print(f"\r  Progress: {' | '.join(parts)}   ", end="", flush=True)
            continue

        color = STATUS_COLOR.get(result.status, RESET)
        _log(
            f"{color}[{result.status}]{RESET} "
            f"key={CYAN}{result.key}{RESET} "
            f"url={result.url[:60]}"
            + (f"\n           link={result.comment_link}" if result.status in (STATUS_SUCCESS, STATUS_MODERATION) else "")
            + (f"\n           err ={result.error_detail}"  if result.error_detail else "")
        )

        if result.status in (STATUS_SUCCESS, STATUS_MODERATION):
            total_done += 1
            success_log.append(result)
        else:
            fail_log.append(result)


def _save_results(success_log: list, fail_log: list, out_dir: Path) -> None:
    out_dir.mkdir(exist_ok=True)

    # Success CSV
    sc = out_dir / "success.csv"
    with sc.open("w", encoding="utf-8") as f:
        f.write("key,url,comment_link,comment_used,strategy\n")
        for r in success_log:
            def esc(s): return '"' + s.replace('"', '""') + '"'
            f.write(f"{esc(r.key)},{esc(r.url)},{esc(r.comment_link)},{esc(r.comment_used)},{esc(r.strategy)}\n")

    # Fail CSV
    fc = out_dir / "fail.csv"
    with fc.open("w", encoding="utf-8") as f:
        f.write("key,url,status,error_detail\n")
        for r in fail_log:
            def esc(s): return '"' + s.replace('"', '""') + '"'
            f.write(f"{esc(r.key)},{esc(r.url)},{esc(r.status)},{esc(r.error_detail)}\n")

    _log(f"Da luu: {sc}")
    _log(f"Da luu: {fc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Comment Bot CLI")
    parser.add_argument("--config", default="config.json", help="File config JSON")
    parser.add_argument("--urls",   default="urls.txt",    help="File danh sach URL")
    parser.add_argument("--workers", type=int, default=3,  help="So worker song song")
    parser.add_argument("--out",    default="output",      help="Thu muc luu ket qua")
    args = parser.parse_args()

    # ── Load config ──
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"{RED}Khong tim thay {cfg_path}{RESET}")
        return

    with cfg_path.open(encoding="utf-8") as f:
        cfg = json.load(f)

    # ── Load URLs ──
    url_path = Path(args.urls)
    if not url_path.exists():
        print(f"{RED}Khong tim thay {url_path}{RESET}")
        return

    urls = [u.strip() for u in url_path.read_text(encoding="utf-8").splitlines() if u.strip()]
    _log(f"Loaded {len(urls)} URLs")

    # ── Build scheduler ──
    profiles: dict[str, SessionProfile] = {}
    keys_targets: list[tuple[str, int]]  = []
    comment_pools: dict[str, list[str]]  = {}

    for p in cfg["profiles"]:
        key = p["name"]
        keys_targets.append((key, p["target"]))
        comment_pools[key] = p["comments"]
        profiles[key] = SessionProfile(
            name=p["name"],
            email=p["email"],
            host=p["host"],
        )
        _log(f"Profile: {CYAN}{key}{RESET} | target={p['target']} | {len(p['comments'])} comments")

    scheduler = JobScheduler(urls, keys_targets, comment_pools, profiles)

    # ── Chay ──
    result_queue: queue.Queue = queue.Queue()
    success_log: list = []
    fail_log:    list = []

    listener = threading.Thread(
        target=_result_listener,
        args=(result_queue, scheduler, success_log, fail_log),
        daemon=True,
    )
    listener.start()

    _log(f"Bat dau voi {args.workers} workers (headless=True)...")
    run_session(
        scheduler=scheduler,
        result_queue=result_queue,
        max_workers=args.workers,
        headless=True,          # Luon headless tren server
    )

    listener.join(timeout=30)
    _save_results(success_log, fail_log, Path(args.out))


if __name__ == "__main__":
    main()