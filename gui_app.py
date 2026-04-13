"""
gui_app.py — Comment Bot UI
Tab 1: Profile cards + URL list + Start/Stop
Tab 2: Thanh cong  — loc theo profile
Tab 3: That bai    — loc theo profile
Tab 4: Loc Link    — strip/dedup/shuffle URLs
Tab 5: Gen Comment — sinh comment bang Groq AI
Tab 6: Log         — xem log real-time de debug
"""

from __future__ import annotations


# ── Logging handler: forward Python log → Tab 6 text widget ──────────────────

import logging as _logging


class _GuiLogHandler(_logging.Handler):
    """Thread-safe logging handler: emit() gui record ve callback trong main thread."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback  # callable(level_str: str, text: str)

    def emit(self, record: _logging.LogRecord) -> None:
        try:
            text = self.format(record)
            self._callback(record.levelname, text)
        except Exception:
            self.handleError(record)

import csv
import json
import queue
import random
import re
import logging
import threading
import time
import webbrowser
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse
from tkinter import filedialog, messagebox
import tkinter as tk
import tkinter.ttk as ttk

import worker as wk
import comment_gen as cg
import db_manager as dbm

# ── Palette ────────────────────────────────────────────────────────────────────
BG         = "#1e1e2e"
FG         = "#cdd6f4"
ENTRY_BG   = "#313244"
BTN_START  = "#a6e3a1"
BTN_STOP   = "#f38ba8"
BTN_FG     = "#1e1e2e"
SUCCESS_CLR = "#a6e3a1"
MOD_CLR    = "#fab387"
FAIL_CLR   = "#f38ba8"
TREE_BG    = "#181825"
TREE_SEL   = "#45475a"
HEADING_FG = "#89b4fa"


class GuiApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Comment Bot")
        self.root.configure(bg=BG)
        self.root.minsize(1000, 680)

        self._result_queue: queue.Queue        = queue.Queue()
        self._session_thread: threading.Thread | None = None
        self._stop_event_holder: list          = []
        self._total_jobs  = 0
        self._done_jobs   = 0
        self._scheduler: wk.JobScheduler | None = None

        self._key_rows: list[dict] = []
        self._urls_list: list[str] = []

        # Multi-group data
        self._groups: list[dict] = []          # [{"name": str, "profiles": list[dict]}]
        self._current_group_view: int = 0

        self._state_file = Path("gui_state.json")

        self.db = dbm.DBManager()

        self._build_style()
        self._build_ui()
        self._load_state()
        self._poll_results()

        # ── Gui log handler (wires Python logging → Tab 6 text widget) ──
        self._attach_log_handler()

        # Luu state khi dong cua so
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Style ──────────────────────────────────────────────────────────────────

    def _build_style(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG,
                    fieldbackground=ENTRY_BG, font=("Segoe UI", 10))
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=ENTRY_BG, foreground=FG,
                    padding=[14, 6], font=("Segoe UI", 10, "bold"))
        s.map("TNotebook.Tab",
              background=[("selected", "#313244")],
              foreground=[("selected", HEADING_FG)])
        s.configure("TFrame",      background=BG)
        s.configure("TLabel",      background=BG, foreground=FG)
        s.configure("TLabelframe", background=BG)
        s.configure("TLabelframe.Label", background=BG, foreground=HEADING_FG,
                    font=("Segoe UI", 9, "bold"))
        s.configure("TEntry",      fieldbackground=ENTRY_BG,
                    foreground=FG, insertcolor=FG)
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.configure("TSpinbox",    fieldbackground=ENTRY_BG, foreground=FG)
        s.configure("TCombobox",   fieldbackground=ENTRY_BG, foreground=FG,
                    selectbackground=TREE_SEL)
        s.configure("Horizontal.TProgressbar",
                    troughcolor=ENTRY_BG, background=HEADING_FG, thickness=8)
        s.configure("Treeview", background=TREE_BG, foreground=FG,
                    fieldbackground=TREE_BG, rowheight=24,
                    font=("Segoe UI", 9))
        s.configure("Treeview.Heading", background=ENTRY_BG,
                    foreground=HEADING_FG, font=("Segoe UI", 9, "bold"))
        s.map("Treeview", background=[("selected", TREE_SEL)])

    # ── UI skeleton ────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        self._notebook = nb

        self._tab1 = ttk.Frame(nb)
        self._tab2 = ttk.Frame(nb)
        self._tab3 = ttk.Frame(nb)
        self._tab4 = ttk.Frame(nb)
        self._tab5 = ttk.Frame(nb)
        self._tab6 = ttk.Frame(nb)
        nb.add(self._tab1, text="Cau hinh & Chay")
        nb.add(self._tab2, text="Thanh cong")
        nb.add(self._tab3, text="That bai")
        nb.add(self._tab4, text="Loc Link")
        nb.add(self._tab5, text="Gen Comment")
        nb.add(self._tab6, text="Log")

        self._build_tab1()
        self._build_tab2()
        self._build_tab3()
        self._build_tab4()
        self._build_tab5()
        self._build_tab6()

    # ═══════════════════════════════════════════════════════════════
    # TAB 1
    # ═══════════════════════════════════════════════════════════════

    def _build_tab1(self) -> None:
        self._tab1.columnconfigure(0, weight=6)
        self._tab1.columnconfigure(1, weight=4)
        self._tab1.rowconfigure(0, weight=1)

        # LEFT — scrollable profile cards
        left = ttk.Frame(self._tab1)
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)

        ttk.Label(left, text="Profiles",
                  foreground=HEADING_FG,
                  font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 4))

        # ── Group bar ────────────────────────────────────────────
        grp_bar = ttk.Frame(left)
        grp_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 4))

        self._group_combo = ttk.Combobox(
            grp_bar, state="readonly", width=20)
        self._group_combo.pack(side="left", padx=(0, 6))
        self._group_combo.bind("<<ComboboxSelected>>", self._on_group_selected)

        self._group_name_entry = ttk.Entry(grp_bar, width=15)
        self._group_name_entry.pack(side="left", padx=(0, 2))
        tk.Button(
            grp_bar, text="Doi ten", font=("Segoe UI", 8),
            bg=ENTRY_BG, fg=FG, relief="flat", padx=6, cursor="hand2",
            command=self._rename_group,
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            grp_bar, text="+ Them Group", font=("Segoe UI", 8),
            bg=ENTRY_BG, fg=HEADING_FG, relief="flat", padx=6, cursor="hand2",
            command=self._add_new_group,
        ).pack(side="left", padx=(0, 4))

        tk.Button(
            grp_bar, text="Xoa Group", font=("Segoe UI", 8),
            bg=BTN_STOP, fg=BTN_FG, relief="flat", padx=6, cursor="hand2",
            command=self._delete_group,
        ).pack(side="left")

        canvas = tk.Canvas(left, bg=BG, highlightthickness=0)
        vsb    = ttk.Scrollbar(left, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=2, column=0, sticky="nsew")
        vsb.grid(   row=2, column=1, sticky="ns")

        self._cards_frame = ttk.Frame(canvas)
        _win = canvas.create_window((0, 0), window=self._cards_frame, anchor="nw")

        def _fit(e):
            canvas.itemconfig(_win, width=e.width)
        canvas.bind("<Configure>", _fit)

        def _scroll_update(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        self._cards_frame.bind("<Configure>", _scroll_update)

        def _wheel(e):
            canvas.yview_scroll(int(-1*(e.delta/120)), "units")
        canvas.bind("<MouseWheel>", _wheel)
        self._cards_frame.bind("<MouseWheel>", _wheel)

        tk.Button(
            left, text="+ Them Profile",
            font=("Segoe UI", 9), bg=ENTRY_BG, fg=HEADING_FG,
            relief="flat", padx=10, pady=4, cursor="hand2",
            command=self._add_profile_card,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # RIGHT — URL list + options + controls
        right = ttk.Frame(self._tab1)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)

        opt = ttk.Frame(right)
        opt.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self._headless_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Headless",
                        variable=self._headless_var).pack(side="left")
        ttk.Label(opt, text="Workers:").pack(side="left", padx=(16, 4))
        self._worker_var = tk.IntVar(value=3)
        self._worker_spin = ttk.Spinbox(opt, from_=1, to=8, width=4,
                                        textvariable=self._worker_var)
        self._worker_spin.pack(side="left")

        self._bypass_name_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Bypass name filter",
                        variable=self._bypass_name_var).pack(side="left", padx=(16, 0))

        # ── Proxy frame ──
        proxy_fr = ttk.LabelFrame(right, text=" Proxy ", padding=6)
        proxy_fr.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        proxy_fr.columnconfigure(0, weight=1)

        _proxy_top = ttk.Frame(proxy_fr)
        _proxy_top.pack(fill="x")
        ttk.Label(_proxy_top, text="Moi dong 1 proxy (ip:port:user:password):",
                  font=("Segoe UI", 8)).pack(side="left")
        ttk.Label(_proxy_top, text="Rotate moi:", font=("Segoe UI", 8)).pack(side="left", padx=(12, 2))
        self._proxy_rotate_var = tk.IntVar(value=250)
        ttk.Spinbox(_proxy_top, from_=50, to=5000, increment=50, width=6,
                    textvariable=self._proxy_rotate_var).pack(side="left")
        ttk.Label(_proxy_top, text="success", font=("Segoe UI", 8)).pack(side="left", padx=(2, 0))

        self._proxy_text = tk.Text(proxy_fr, height=4, bg=ENTRY_BG, fg=HEADING_FG,
                                   font=("Consolas", 9), insertbackground=HEADING_FG,
                                   relief="flat", bd=1)
        self._proxy_text.pack(fill="x", pady=(4, 4))

        _proxy_btns = ttk.Frame(proxy_fr)
        _proxy_btns.pack(fill="x")
        self._btn_test_proxy = tk.Button(
            _proxy_btns, text="Test Proxies",
            font=("Segoe UI", 8, "bold"), bg=ENTRY_BG, fg=HEADING_FG,
            relief="flat", padx=8, pady=2, cursor="hand2",
            command=self._test_proxies,
        )
        self._btn_test_proxy.pack(side="left")
        self._proxy_status_lbl = ttk.Label(_proxy_btns, text="", font=("Segoe UI", 8))
        self._proxy_status_lbl.pack(side="left", padx=(8, 0))

        # URL label + live count
        url_hdr = ttk.Frame(right)
        url_hdr.grid(row=2, column=0, sticky="ew", pady=(0, 2))
        ttk.Label(
            url_hdr, text="Danh sach URL (moi dong 1 URL):",
            foreground=HEADING_FG, font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        self._lbl_url_count = ttk.Label(
            url_hdr, text="0 URL",
            foreground=MOD_CLR, font=("Segoe UI", 9, "bold"))
        self._lbl_url_count.pack(side="right")

        url_fr = ttk.Frame(right)
        url_fr.grid(row=3, column=0, sticky="nsew")
        url_fr.rowconfigure(0, weight=1)
        url_fr.columnconfigure(0, weight=1)

        self._btn_import_urls = tk.Button(
            url_fr, text="Import tu file .txt",
            font=("Segoe UI", 10, "bold"), bg=ENTRY_BG, fg=HEADING_FG,
            relief="flat", padx=14, pady=8, cursor="hand2",
            command=self._import_urls_from_file
        )
        self._btn_import_urls.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=(4, 8))

        cb = ttk.Frame(right)
        cb.grid(row=4, column=0, sticky="ew", pady=(8, 0))

        self._btn_start = tk.Button(
            cb, text="Bat dau",
            font=("Segoe UI", 10, "bold"),
            bg=BTN_START, fg=BTN_FG,
            relief="flat", padx=14, pady=6,
            cursor="hand2", command=self._on_start,
        )
        self._btn_start.pack(side="left")

        self._btn_stop = tk.Button(
            cb, text="Dung",
            font=("Segoe UI", 10, "bold"),
            bg=BTN_STOP, fg=BTN_FG,
            relief="flat", padx=14, pady=6,
            cursor="hand2", command=self._on_stop, state="disabled",
        )
        self._btn_stop.pack(side="left", padx=(8, 0))

        self._btn_reset_all = tk.Button(
            cb, text="XOÁ TẤT CẢ",
            font=("Segoe UI", 10, "bold"),
            bg="#fab387", fg=BTN_FG,
            relief="flat", padx=14, pady=6,
            cursor="hand2", command=self._reset_all_url_data,
        )
        self._btn_reset_all.pack(side="left", padx=(16, 0))

        self._progress_lbl = ttk.Label(cb, text="")
        self._progress_lbl.pack(side="left", padx=(14, 0))

        self._progressbar = ttk.Progressbar(
            cb, mode="determinate",
            style="Horizontal.TProgressbar",
        )
        self._progressbar.pack(side="right", fill="x", expand=True, padx=(8, 0))
        ttk.Label(cb, text="Tien do:").pack(side="right")

    def _import_urls_from_file(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            title="Chon file chua URLs"
        )
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                self._urls_list = [l.strip() for l in content.splitlines() if l.strip()]
                self._lbl_url_count.config(text=f"{len(self._urls_list)} URL")
                messagebox.showinfo("Import thanh cong", f"Da nhap {len(self._urls_list)} URLs tu file.")
            except Exception as e:
                messagebox.showerror("Loi", f"Loi khi doc file: {e}")

    def _test_proxies(self) -> None:
        """Test proxy concurrent — hien thi popup ket qua chi tiet (IP, latency, loi)."""
        import httpx, time as _time, concurrent.futures

        raw = self._proxy_text.get("1.0", "end-1c").strip()
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        if not lines:
            self._proxy_status_lbl.config(text="Khong co proxy nao de test")
            return

        self._btn_test_proxy.config(state="disabled")
        self._proxy_status_lbl.config(text=f"Dang test {len(lines)} proxies...")

        # ── Popup ket qua ──────────────────────────────────────────────────
        win = tk.Toplevel(self.root)
        win.title("Ket qua kiem tra Proxy")
        win.configure(bg=BG)
        win.geometry("720x480")
        win.resizable(True, True)

        hdr = ttk.Label(win, text=f"Test {len(lines)} proxies (concurrent 10 luong)...",
                        font=("Segoe UI", 9, "bold"), foreground=HEADING_FG)
        hdr.pack(anchor="w", padx=10, pady=(8, 4))

        txt_fr = ttk.Frame(win)
        txt_fr.pack(fill="both", expand=True, padx=10, pady=4)
        sb = ttk.Scrollbar(txt_fr)
        sb.pack(side="right", fill="y")
        out = tk.Text(txt_fr, bg=ENTRY_BG, fg=HEADING_FG, font=("Consolas", 9),
                      yscrollcommand=sb.set, relief="flat", state="disabled")
        out.pack(fill="both", expand=True)
        sb.config(command=out.yview)
        out.tag_config("ok",   foreground="#a6e3a1")
        out.tag_config("fail", foreground="#f38ba8")
        out.tag_config("warn", foreground="#fab387")
        out.tag_config("hdr",  foreground="#cba6f7", font=("Consolas", 9, "bold"))

        btn_fr = ttk.Frame(win)
        btn_fr.pack(fill="x", padx=10, pady=(4, 8))
        failed_proxies: list[str] = []

        def _append(text: str, tag: str = "") -> None:
            out.config(state="normal")
            out.insert("end", text, tag)
            out.see("end")
            out.config(state="disabled")

        def _remove_failed() -> None:
            good = [l for l in lines if l not in failed_proxies]
            self._proxy_text.delete("1.0", "end")
            self._proxy_text.insert("1.0", "\n".join(good))
            _append(f"\n→ Da xoa {len(failed_proxies)} proxy loi, con lai {len(good)}\n", "warn")

        btn_remove = tk.Button(btn_fr, text="Xoa proxy loi khoi danh sach",
                               font=("Segoe UI", 8, "bold"), bg=BTN_STOP, fg=BTN_FG,
                               relief="flat", padx=8, pady=2, cursor="hand2",
                               command=_remove_failed, state="disabled")
        btn_remove.pack(side="left")
        sum_lbl = ttk.Label(btn_fr, text="", font=("Segoe UI", 8, "bold"))
        sum_lbl.pack(side="left", padx=(12, 0))

        # ── Worker test 1 proxy ────────────────────────────────────────────
        _TEST_URL = "https://api.ipify.org?format=json"
        _TIMEOUT  = 12  # giay

        def _check_one(line: str) -> dict:
            parsed = wk._parse_proxy(line)
            if not parsed:
                return {"line": line, "ok": False, "reason": "Format proxy sai"}
            proxies_cfg = {"http://": parsed["server"], "https://": parsed["server"]}
            auth = None
            if parsed.get("username"):
                auth = httpx.BasicAuth(parsed["username"], parsed.get("password", ""))
            t0 = _time.monotonic()
            try:
                with httpx.Client(proxies=proxies_cfg, auth=auth,
                                  timeout=_TIMEOUT, verify=False,
                                  follow_redirects=True) as client:
                    resp = client.get(_TEST_URL)
                elapsed_ms = int((_time.monotonic() - t0) * 1000)
                if resp.status_code == 200:
                    ip = resp.json().get("ip", "?")
                    return {"line": line, "ok": True, "ip": ip, "ms": elapsed_ms}
                else:
                    return {"line": line, "ok": False, "reason": f"HTTP {resp.status_code}",
                            "ms": int((_time.monotonic() - t0) * 1000)}
            except httpx.ProxyError as e:
                return {"line": line, "ok": False,
                        "reason": f"Proxy loi: {str(e)[:60]}"}
            except httpx.TimeoutException:
                return {"line": line, "ok": False, "reason": f"Timeout {_TIMEOUT}s"}
            except Exception as e:
                return {"line": line, "ok": False, "reason": str(e)[:80]}

        # ── Chay trong background thread ──────────────────────────────────
        def _run():
            n_ok = n_fail = 0
            seen_ips: dict[str, int] = {}  # ip → count (phat hien IP trung)

            _append(f"{'SỐ':>4}  {'PROXY':<30}  {'TRẠNG THÁI'}\n", "hdr")
            _append(f"{'─'*70}\n", "hdr")

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                futs = {pool.submit(_check_one, l): (i + 1, l) for i, l in enumerate(lines)}
                for fut in concurrent.futures.as_completed(futs):
                    idx, raw_line = futs[fut]
                    try:
                        res = fut.result()
                    except Exception as e:
                        res = {"line": raw_line, "ok": False, "reason": str(e)}

                    short = raw_line[:28] + ".." if len(raw_line) > 30 else raw_line
                    if res["ok"]:
                        n_ok += 1
                        ip = res["ip"]
                        ms = res["ms"]
                        dup_warn = ""
                        seen_ips[ip] = seen_ips.get(ip, 0) + 1
                        if seen_ips[ip] > 1:
                            dup_warn = f"  ⚠ IP trung ({seen_ips[ip]} proxy cung IP)"
                        row = f"{idx:>4}  {short:<30}  ✓ OK  {ms:>5}ms  IP={ip}{dup_warn}\n"
                        tag = "warn" if dup_warn else "ok"
                        win.after(0, lambda r=row, t=tag: _append(r, t))
                    else:
                        n_fail += 1
                        failed_proxies.append(raw_line)
                        reason = res.get("reason", "?")
                        row = f"{idx:>4}  {short:<30}  ✗ FAIL  {reason}\n"
                        win.after(0, lambda r=row: _append(r, "fail"))

                    total_done = n_ok + n_fail
                    win.after(0, lambda d=total_done: hdr.config(
                        text=f"Da test {d}/{len(lines)}..."))

            # Tong ket
            dup_ip_count = sum(1 for c in seen_ips.values() if c > 1)
            sum_text = (f"Tong ket: ✓ {n_ok} OK  |  ✗ {n_fail} Fail  |"
                        f"  {dup_ip_count} IP bi trung")
            win.after(0, lambda: _append(f"\n{'─'*70}\n", "hdr"))
            win.after(0, lambda: _append(sum_text + "\n", "ok" if n_fail == 0 else "warn"))
            win.after(0, lambda: hdr.config(text=sum_text))
            win.after(0, lambda: sum_lbl.config(text=sum_text))
            win.after(0, lambda: btn_remove.config(
                state="normal" if failed_proxies else "disabled"))
            self.root.after(0, lambda: self._proxy_status_lbl.config(
                text=f"OK: {n_ok} | Fail: {n_fail} / {len(lines)}"))
            self.root.after(0, lambda: self._btn_test_proxy.config(state="normal"))

        threading.Thread(target=_run, daemon=True).start()

    # ── Profile card ───────────────────────────────────────────────────────────

    def _add_profile_card(self, name="", email="", host="", target=5, comments: list[str] | None = None) -> None:
        card = ttk.LabelFrame(self._cards_frame, text=" Profile ", padding=8)
        card.pack(fill="x", padx=4, pady=(0, 8))
        card.columnconfigure(1, weight=1)

        name_var   = tk.StringVar(value=name)
        email_var  = tk.StringVar(value=email)
        host_var   = tk.StringVar(value=host)
        target_var = tk.IntVar(value=target)

        p = {"padx": 4, "pady": 3}
        ttk.Label(card, text="Name / Key:").grid(row=0, column=0, sticky="e", **p)
        ttk.Entry(card, textvariable=name_var).grid(row=0, column=1, sticky="ew", **p)
        ttk.Label(card, text="Email:").grid(row=1, column=0, sticky="e", **p)
        ttk.Entry(card, textvariable=email_var).grid(row=1, column=1, sticky="ew", **p)
        ttk.Label(card, text="Website:").grid(row=2, column=0, sticky="e", **p)
        ttk.Entry(card, textvariable=host_var).grid(row=2, column=1, sticky="ew", **p)

        sf = ttk.Frame(card)
        sf.grid(row=3, column=0, columnspan=3, sticky="ew", padx=4, pady=4)
        ttk.Label(sf, text="Target:").pack(side="left")
        ttk.Spinbox(sf, from_=1, to=9999, width=6,
                    textvariable=target_var).pack(side="left", padx=(4, 16))
        prog_lbl = ttk.Label(sf, text="0/0",
                             foreground=SUCCESS_CLR,
                             font=("Segoe UI", 9, "bold"))
        prog_lbl.pack(side="left", padx=(0, 12))
        pool_lbl = ttk.Label(sf, text="0 CMT", foreground=MOD_CLR)
        pool_lbl.pack(side="left")

        ttk.Label(
            card,
            text="Comments (1 dong = 1 CMT | tai su dung khi URL fail):",
            foreground=HEADING_FG, font=("Segoe UI", 8),
        ).grid(row=4, column=0, columnspan=3, sticky="w", padx=4, pady=(4, 0))

        txt_cmt = tk.Text(
            card, height=5, wrap="none",
            bg=ENTRY_BG, fg=FG, insertbackground=FG,
            font=("Consolas", 8), relief="flat",
            selectbackground=TREE_SEL,
        )
        txt_cmt.grid(row=5, column=0, columnspan=3, sticky="ew",
                     padx=4, pady=(2, 4))
        
        txt_cmt._count_job = None

        def _count(t=txt_cmt, pl=pool_lbl):
            try:
                n = len([ln for ln in t.get("1.0", "end-1c").splitlines() if ln.strip()])
                pl.config(text=f"{n} CMT")
            except Exception:
                pass

        def _modified(e, t=txt_cmt):
            t.edit_modified(False)
            if t._count_job is not None:
                self.root.after_cancel(t._count_job)
            t._count_job = self.root.after(300, _count)
            
        txt_cmt.bind("<<Modified>>", _modified)

        # Pre-fill comments if provided
        if comments:
            txt_cmt.insert("1.0", "\n".join(comments))
            pool_lbl.config(text=f"{len(comments)} CMT")

        rd = {
            "frame": card, "name_var": name_var, "email_var": email_var,
            "host_var": host_var, "target_var": target_var,
            "prog_lbl": prog_lbl, "pool_lbl": pool_lbl, "txt_cmt": txt_cmt,
        }
        self._key_rows.append(rd)

        tk.Button(
            card, text="Xoa",
            font=("Segoe UI", 8), bg=BTN_STOP, fg=BTN_FG,
            relief="flat", padx=6, pady=1, cursor="hand2",
            command=lambda r=rd: self._remove_profile(r),
        ).grid(row=0, column=2, padx=(6, 0), sticky="e")
        card.columnconfigure(2, minsize=50)

    def _remove_profile(self, rd: dict) -> None:
        if rd in self._key_rows:
            rd["frame"].destroy()
            self._key_rows.remove(rd)

    # ── Group management ───────────────────────────────────────────────────────

    def _collect_current_profiles(self) -> list[dict]:
        """Thu thap profiles tu cac card dang hien thi."""
        profiles = []
        for r in self._key_rows:
            comments = [
                ln for ln in r["txt_cmt"].get("1.0", "end-1c").splitlines()
                if ln.strip()
            ]
            try:
                target = int(r["target_var"].get())
            except Exception:
                target = 5
            profiles.append({
                "name":    r["name_var"].get(),
                "email":   r["email_var"].get(),
                "host":    r["host_var"].get(),
                "target":  target,
                "comments": comments,
            })
        return profiles

    def _display_group(self, idx: int) -> None:
        """Xoa card cu, hien thi profiles cua group[idx]."""
        for rd in list(self._key_rows):
            rd["frame"].destroy()
        self._key_rows.clear()

        if 0 <= idx < len(self._groups):
            profs = self._groups[idx].get("profiles", [])
            if profs:
                for p in profs:
                    self._add_profile_card(
                        name=p.get("name", ""),
                        email=p.get("email", ""),
                        host=p.get("host", ""),
                        target=p.get("target", 5),
                        comments=p.get("comments", []),
                    )
            else:
                self._add_profile_card()
        else:
            self._add_profile_card()

        self._current_group_view = idx
        self._refresh_group_combo()

    def _switch_group(self, new_idx: int) -> None:
        if new_idx == self._current_group_view:
            return
        if 0 <= self._current_group_view < len(self._groups):
            self._groups[self._current_group_view]["profiles"] = self._collect_current_profiles()
        self._display_group(new_idx)

    def _on_group_selected(self, event=None) -> None:
        idx = self._group_combo.current()
        if idx >= 0:
            self._switch_group(idx)

    def _rename_group(self) -> None:
        new_name = self._group_name_entry.get().strip()
        if not new_name:
            return
        if 0 <= self._current_group_view < len(self._groups):
            self._groups[self._current_group_view]["name"] = new_name
            self._refresh_group_combo()
            self._group_name_entry.delete(0, "end")

    def _add_new_group(self) -> None:
        if 0 <= self._current_group_view < len(self._groups):
            self._groups[self._current_group_view]["profiles"] = self._collect_current_profiles()
        self._groups.append({"name": f"Group {len(self._groups) + 1}", "profiles": []})
        self._display_group(len(self._groups) - 1)

    def _delete_group(self) -> None:
        if len(self._groups) <= 1:
            messagebox.showwarning("Khong the xoa", "Phai co it nhat 1 group.")
            return
        name = self._groups[self._current_group_view]["name"]
        if not messagebox.askyesno("Xoa Group", f"Xoa group '{name}' va tat ca profiles trong do?"):
            return
        del self._groups[self._current_group_view]
        new_idx = min(self._current_group_view, len(self._groups) - 1)
        self._display_group(new_idx)

    def _refresh_group_combo(self) -> None:
        names = [g["name"] for g in self._groups]
        self._group_combo["values"] = names
        if 0 <= self._current_group_view < len(names):
            self._group_combo.current(self._current_group_view)
        # Sync Tab5 target group combobox
        if hasattr(self, "_gc_target_group"):
            cur = self._gc_target_group_var.get()
            self._gc_target_group["values"] = names
            if cur in names:
                self._gc_target_group.current(names.index(cur))
            elif names:
                self._gc_target_group.current(0)

    def _get_keys_targets(self) -> list[tuple[str, int]]:
        out = []
        for r in self._key_rows:
            k = r["name_var"].get().strip()
            try:
                t = int(r["target_var"].get())
            except Exception:
                t = 5
            if k:
                out.append((k, max(1, t)))
        return out

    def _update_profile_progress(self, key: str, success: int, target: int) -> None:
        for r in self._key_rows:
            ck = f"{r['name_var'].get().strip()}::{r['host_var'].get().strip()}"
            if ck == key:
                try:
                    original_target = int(r["target_var"].get())
                except Exception:
                    original_target = target
                nm_part, host_part = key.split("::", 1)
                total_success = self.db.get_success_count(nm_part, website=host_part)
                r["prog_lbl"].config(text=f"{total_success}/{original_target}")
                break

    # ═══════════════════════════════════════════════════════════════
    # TAB 2 — Thanh cong
    # ═══════════════════════════════════════════════════════════════

    def _build_tab2(self) -> None:
        self._tab2.columnconfigure(0, weight=1)
        self._tab2.rowconfigure(1, weight=1)

        # ── top bar ──────────────────────────────────────────────────
        top = ttk.Frame(self._tab2)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 4))

        ttk.Label(top, text="Group:").pack(side="left")
        self._s_group_var = tk.StringVar(value="Tat ca")
        self._s_group_cb = ttk.Combobox(
            top, textvariable=self._s_group_var,
            values=["Tat ca"], state="readonly", width=18,
        )
        self._s_group_cb.pack(side="left", padx=(6, 12))
        self._s_group_cb.bind("<<ComboboxSelected>>", self._on_s_group_changed)

        ttk.Label(top, text="Profile:").pack(side="left")
        self._s_profile_var = tk.StringVar(value="Tat ca")
        self._s_cb = ttk.Combobox(
            top, textvariable=self._s_profile_var,
            values=["Tat ca"], state="readonly", width=22,
        )
        self._s_cb.pack(side="left", padx=(6, 16))
        self._s_cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_success())

        self._lbl_s_count = ttk.Label(
            top, text="0 URL thanh cong",
            foreground=SUCCESS_CLR, font=("Segoe UI", 16, "bold"))
        self._lbl_s_count.pack(side="left", pady=20)

        # ── bottom bar ───────────────────────────────────────────────
        bb = ttk.Frame(self._tab2)
        bb.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 6))
        tk.Button(bb, text="Xuat toan bo ket qua ra file Excel/CSV", font=("Segoe UI", 10, "bold"),
                  bg=ENTRY_BG, fg=HEADING_FG, relief="flat", padx=10, pady=6,
                  cursor="hand2",
                  command=self._export_success_csv2).pack(side="right")
        tk.Button(bb, text="Xoa ket qua", font=("Segoe UI", 10, "bold"),
                  bg=BTN_STOP, fg=BTN_FG, relief="flat", padx=10, pady=6,
                  cursor="hand2",
                  command=lambda: (
                      self._clear_success_data()
                      if messagebox.askyesno("Xoa?", "Xoa toan bo ket qua tu SQLite?")
                      else None
                  )).pack(side="right", padx=(0, 8))

    # ═══════════════════════════════════════════════════════════════
    # TAB 3 — That bai
    # ═══════════════════════════════════════════════════════════════

    def _build_tab3(self) -> None:
        self._tab3.columnconfigure(0, weight=1)
        self._tab3.rowconfigure(1, weight=1)

        top = ttk.Frame(self._tab3)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 2))

        ttk.Label(top, text="Group:").pack(side="left")
        self._f_group_var = tk.StringVar(value="Tat ca")
        self._f_group_cb = ttk.Combobox(
            top, textvariable=self._f_group_var,
            values=["Tat ca"], state="readonly", width=18,
        )
        self._f_group_cb.pack(side="left", padx=(6, 12))
        self._f_group_cb.bind("<<ComboboxSelected>>", self._on_f_group_changed)

        ttk.Label(top, text="Profile:").pack(side="left")
        self._f_profile_var = tk.StringVar(value="Tat ca")
        self._f_cb = ttk.Combobox(
            top, textvariable=self._f_profile_var,
            values=["Tat ca"], state="readonly", width=22,
        )
        self._f_cb.pack(side="left", padx=(6, 16))
        self._f_cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_fail())

        self._lbl_f_count = ttk.Label(top, text="0 URL that bai")
        self._lbl_f_count.pack(side="left")
        ttk.Label(top, text="  Chuot phai -> Retry",
                  foreground=HEADING_FG, font=("Segoe UI", 9)).pack(side="left")

        cols_f = ("profile", "url", "reason", "comment_used")
        self._tree_f = ttk.Treeview(
            self._tab3, columns=cols_f, show="headings", selectmode="browse")
        for col, text, w in [
            ("profile",      "Profile",      110),
            ("url",          "URL",          240),
            ("reason",       "Ly do",        200),
            ("comment_used", "Comment dung", 300),
        ]:
            self._tree_f.heading(col, text=text)
            self._tree_f.column(col, width=w, minwidth=60, stretch=True)
        self._tree_f.tag_configure("fail",    foreground=FAIL_CLR)
        self._tree_f.tag_configure("review",  foreground=MOD_CLR)
        self._tree_f.tag_configure("captcha", foreground="#cba6f7")
        self._tree_f.tag_configure("noform",  foreground="#89dceb")

        f_vsb = ttk.Scrollbar(self._tab3, orient="vertical",  command=self._tree_f.yview)
        f_hsb = ttk.Scrollbar(self._tab3, orient="horizontal", command=self._tree_f.xview)
        self._tree_f.configure(yscrollcommand=f_vsb.set, xscrollcommand=f_hsb.set)
        self._tree_f.grid( row=1, column=0, sticky="nsew", padx=(8, 0), pady=2)
        f_vsb.grid(        row=1, column=1, sticky="ns")
        f_hsb.grid(        row=2, column=0, sticky="ew",  padx=(8, 0))

        self._fail_menu = tk.Menu(self.root, tearoff=0, bg=ENTRY_BG, fg=FG)
        self._fail_menu.add_command(label="Retry URL nay", command=self._retry_fail)
        self._tree_f.bind("<Button-3>", self._show_fail_menu)

        bb = ttk.Frame(self._tab3)
        bb.grid(row=3, column=0, columnspan=2, sticky="ew", padx=8, pady=(4, 6))
        tk.Button(bb, text="Export CSV", font=("Segoe UI", 9),
                  bg=ENTRY_BG, fg=FG, relief="flat", padx=10, pady=3,
                  cursor="hand2",
                  command=lambda: self._export_tree(
                      self._tree_f,
                      ["profile", "url", "ly_do", "comment_dung"])
                  ).pack(side="right")

    # ── Combo sync & refresh ───────────────────────────────────────────────────

    def _update_profile_combos(self) -> None:
        # Group combos
        group_names = ["Tat ca"] + self.db.get_group_names()
        if hasattr(self, "_s_group_cb"):
            self._s_group_cb["values"] = group_names
            if self._s_group_var.get() not in group_names:
                self._s_group_var.set("Tat ca")
        if hasattr(self, "_f_group_cb"):
            self._f_group_cb["values"] = group_names
            if self._f_group_var.get() not in group_names:
                self._f_group_var.set("Tat ca")

        # Profile combos — filter by selected group
        s_grp = self._s_group_var.get() if hasattr(self, "_s_group_var") else "Tat ca"
        f_grp = self._f_group_var.get() if hasattr(self, "_f_group_var") else "Tat ca"

        def _get_profile_names(grp_filter):
            if grp_filter and grp_filter != "Tat ca":
                seed = [
                    p.get("name", "").strip()
                    for g in self._groups if g["name"] == grp_filter
                    for p in g.get("profiles", [])
                    if p.get("name", "").strip()
                ]
            else:
                seed = [
                    p.get("name", "").strip()
                    for g in self._groups
                    for p in g.get("profiles", [])
                    if p.get("name", "").strip()
                ]
            names = ["Tat ca"] + seed
            cursor = self.db.conn.cursor()
            if grp_filter and grp_filter != "Tat ca":
                cursor.execute("SELECT DISTINCT keyword FROM results WHERE group_name = ?", (grp_filter,))
            else:
                cursor.execute("SELECT DISTINCT keyword FROM results")
            for row in cursor.fetchall():
                k = row[0]
                if k and k not in names:
                    names.append(k)
            return names

        s_names = _get_profile_names(s_grp)
        f_names = _get_profile_names(f_grp)

        self._s_cb["values"] = s_names
        self._f_cb["values"] = f_names
        if self._s_profile_var.get() not in s_names:
            self._s_profile_var.set("Tat ca")
        if self._f_profile_var.get() not in f_names:
            self._f_profile_var.set("Tat ca")

    def _on_s_group_changed(self, event=None) -> None:
        self._s_profile_var.set("Tat ca")
        self._update_profile_combos()
        self._refresh_success()

    def _on_f_group_changed(self, event=None) -> None:
        self._f_profile_var.set("Tat ca")
        self._update_profile_combos()
        self._refresh_fail()

    def _refresh_success(self) -> None:
        sel = self._s_profile_var.get()
        grp = self._s_group_var.get() if hasattr(self, "_s_group_var") else None
        count = self.db.get_success_count(sel, group_name=grp)
        self._lbl_s_count.config(text=f"{count} URL thanh cong")

    def _refresh_fail(self) -> None:
        sel = self._f_profile_var.get()
        grp = self._f_group_var.get() if hasattr(self, "_f_group_var") else None
        for iid in self._tree_f.get_children():
            self._tree_f.delete(iid)
        data = self.db.get_fail_results(sel, group_name=grp)
        for (k, url, reason, comment_used) in data:
            tag = ("fail"    if "FORM_ERROR" in reason else
                   "captcha" if "CAPTCHA"    in reason else
                   "noform"  if "FORM_NOT"   in reason else "review")
            self._tree_f.insert("", "end", values=(k, url, reason, comment_used), tags=(tag,))
        
        count = self.db.get_fail_count(sel, group_name=grp)
        self._lbl_f_count.config(text=f"{count} URL that bai")

    # ═══════════════════════════════════════════════════════════════
    # Session control
    # ═══════════════════════════════════════════════════════════════

    def _on_start(self) -> None:
        # Thu thap profiles hien tai vao group dang xem
        if 0 <= self._current_group_view < len(self._groups):
            self._groups[self._current_group_view]["profiles"] = self._collect_current_profiles()

        urls = self._urls_list.copy()
        if not urls:
            messagebox.showwarning("Thieu URL", "Nhap it nhat 1 URL.")
            return

        # Xay dung group queue: chi nhung group co it nhat 1 profile co name
        self._group_queue = []
        for g in self._groups:
            has_profile = any(
                p.get("name", "").strip() for p in g.get("profiles", [])
            )
            if has_profile:
                self._group_queue.append(g)

        if not self._group_queue:
            messagebox.showwarning("Thieu Profile",
                                   "Them it nhat 1 Profile co Name/Key.")
            return

        # Validate tat ca groups truoc khi chay
        for g in self._group_queue:
            gname = g["name"]
            for p in g.get("profiles", []):
                nm = p.get("name", "").strip()
                if nm and not p.get("email", "").strip():
                    messagebox.showwarning(
                        "Thieu Email",
                        f"Group '{gname}', profile '{nm}' chua co Email.")
                    return
                if nm and not [ln for ln in p.get("comments", []) if ln.strip()]:
                    messagebox.showwarning(
                        "Chua co Comments",
                        f"Group '{gname}', profile '{nm}' chua co comment.")
                    return

        self._urls_for_session = urls
        self._running_group_idx = 0
        self._running_group_name = self._group_queue[0]["name"]
        self._user_stopped = False

        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")

        self._update_profile_combos()
        self._refresh_success()
        self._refresh_fail()

        self._start_group(0)

    def _start_group(self, idx: int) -> None:
        """Khoi chay 1 group cu the (logic tach tu _on_start cu)."""
        group = self._group_queue[idx]
        group_name = group["name"]
        profs = group.get("profiles", [])

        # Tao profiles dict + pools dict + keys_targets
        kts: list[tuple[str, int]] = []
        pools: dict[str, list[str]] = {}
        profiles: dict[str, wk.SessionProfile] = {}

        for p in profs:
            nm = p.get("name", "").strip()
            if not nm:
                continue
            host = p.get("host", "").strip()
            ck = f"{nm}::{host}"  # composite key — phân biệt profile cùng tên khác website
            try:
                t = int(p.get("target", 5))
            except Exception:
                t = 5
            kts.append((ck, max(1, t)))
            pools[ck] = [ln.strip() for ln in p.get("comments", []) if ln.strip()]
            profiles[ck] = wk.SessionProfile(
                name=nm,
                email=p.get("email", "").strip(),
                host=host,
            )

        if not kts:
            self._advance_to_next_group()
            return

        # Short comment warning (khong block, chi log)
        short = [f"  - {k}: co {len(pools.get(k, []))} / can {t}"
                 for k, t in kts if 0 < len(pools.get(k, [])) < t]
        if short:
            print(f"[gui] Group '{group_name}' — comment it hon target:\n" + "\n".join(short))

        urls = self._urls_for_session.copy()

        # ── Resume trong CUNG group: chi loai bo URL ma CHINH group nay da thanh cong ──
        # Group khac la session doc lap — khong ke ket qua cua group truoc
        done_urls: set[str] = set()
        cursor = self.db.conn.cursor()
        cursor.execute(
            "SELECT url FROM results WHERE (status = 'SUCCESS' OR status = 'MODERATION') AND group_name = ?",
            (group_name,)
        )
        for row in cursor.fetchall():
            done_urls.add(row[0])

        urls = [u for u in urls if u not in done_urls]

        # Tru so da thanh cong tu target — chi chay phan con lai
        adjusted_kts: list[tuple[str, int]] = []
        existing_success: dict[str, int] = {}
        for k, t in kts:
            nm_part, host_part = k.split("::", 1)
            already = self.db.get_success_count(nm_part, group_name=group_name, website=host_part)
            existing_success[k] = already
            remaining = max(0, t - already)
            adjusted_kts.append((k, remaining))

        total_remaining = sum(t for _, t in adjusted_kts)
        if total_remaining == 0:
            print(f"[gui] Group '{group_name}' da du target — skip")
            self._advance_to_next_group()
            return
        if not urls:
            print(f"[gui] Group '{group_name}' — het URL — skip")
            self._advance_to_next_group()
            return

        self._scheduler = wk.JobScheduler(
            urls=urls, keys_targets=adjusted_kts,
            comment_pools={k: list(v) for k, v in pools.items()},
            profiles=profiles,
        )

        self._total_jobs = total_remaining
        self._done_jobs  = 0
        self._progressbar["maximum"] = self._total_jobs
        self._progressbar["value"]   = 0
        grp_label = f"[{idx+1}/{len(self._group_queue)}] {group_name}"
        self._progress_lbl.config(text=f"{grp_label} — 0 / {self._total_jobs}")
        self._grp_label = grp_label

        # Parse proxy list tu UI
        _proxy_raw = self._proxy_text.get("1.0", "end-1c").strip()
        _proxy_lines = [l.strip() for l in _proxy_raw.splitlines() if l.strip()] if _proxy_raw else None
        _proxy_rotate = self._proxy_rotate_var.get()

        self._stop_event_holder.clear()

        def _thread_target():
            try:
                wk.run_session(
                    self._scheduler, self._result_queue,
                    max_workers=max(1, min(8, int(self._worker_spin.get() or 3))),
                    headless=self._headless_var.get(),
                    stop_event_holder=self._stop_event_holder,
                    bypass_name=self._bypass_name_var.get(),
                    group_name=group_name,
                    proxies=_proxy_lines,
                    proxy_rotate_every=_proxy_rotate,
                )
            except Exception as exc:
                import traceback
                err = traceback.format_exc()
                self._result_queue.put(wk.Result(
                    status=wk.STATUS_DONE,
                    error_detail=f"THREAD CRASH: {exc}",
                ))
                self.root.after(0, lambda: messagebox.showerror(
                    "Loi nghiem trong",
                    f"Session thread bi crash:\n\n{err}"
                ))

        self._session_thread = threading.Thread(
            target=_thread_target,
            daemon=True,
        )
        self._session_thread.start()

    def _on_stop(self) -> None:
        self._user_stopped = True
        self._group_queue = []          # Khong auto-continue
        for ev, loop in self._stop_event_holder:
            try:
                loop.call_soon_threadsafe(ev.set)
            except Exception:
                pass
        self._btn_stop.config(state="disabled")
        self._progress_lbl.config(
            text=self._progress_lbl.cget("text") + "  [dang dung...]")

    def _advance_to_next_group(self) -> None:
        """Chuyen sang group tiep theo hoac ket thuc."""
        group_queue = getattr(self, "_group_queue", [])
        idx = getattr(self, "_running_group_idx", 0)
        prev_name = getattr(self, "_running_group_name", "")

        # ── Cleanup thread + scheduler cu truoc khi chay group moi ──
        old_thread = self._session_thread
        if old_thread is not None and old_thread.is_alive():
            old_thread.join(timeout=15)
        self._session_thread = None

        old_sched = self._scheduler
        if old_sched is not None:
            try:
                old_sched.db.close()
            except Exception:
                pass
        self._scheduler = None

        import gc
        gc.collect()

        next_idx = idx + 1
        if next_idx < len(group_queue):
            # Co group tiep theo
            # Uu tien URL thanh cong tu group truoc
            if prev_name:
                success_urls = self.db.get_group_success_urls(prev_name)
                self.db.reset_queue_with_priority(success_urls)
                self.db.clear_domain_stats()

            self._running_group_idx = next_idx
            self._running_group_name = group_queue[next_idx]["name"]
            self._progress_lbl.config(
                text=f"Cho 30s → Group tiep theo: {group_queue[next_idx]['name']}...")
            self.root.after(30000, lambda: self._start_group(next_idx))
        else:
            # Het groups
            self._btn_start.config(state="normal")
            self._btn_stop.config(state="disabled")
            n = len(group_queue)
            self._progress_lbl.config(text=f"Hoan tat {n} group(s)")
            self._group_queue = []

    def _auto_export_group_csv(self, group_name: str) -> None:
        """Tu dong export CSV ket qua thanh cong cua 1 group."""
        import os
        folder = r"C:\Users\trand\Documents\BLCM"
        try:
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"{group_name}.csv")
            rows = self.db.get_success_results(group_name=group_name)
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["profile", "url_goc", "link_thanh_cong", "status", "comment_used", "website"])
                w.writerows(rows)
            print(f"[CSV] Exported {len(rows)} rows → {path}")
        except Exception as exc:
            print(f"[CSV] Export error for group '{group_name}': {exc}")

    # ═══════════════════════════════════════════════════════════════
    # Result polling & handling
    # ═══════════════════════════════════════════════════════════════

    def _poll_results(self) -> None:
        # Gioi han 15 result/tick de tranh block main thread qua lau
        count = 0
        try:
            while count < 15:
                r: wk.Result = self._result_queue.get_nowait()
                self._handle_result(r)
                count += 1
        except queue.Empty:
            pass
        finally:
            # Neu con nhieu result trong queue, poll nhanh hon
            interval = 50 if count >= 15 else 150
            self.root.after(interval, self._poll_results)

    def _handle_result(self, r: wk.Result) -> None:
        if r.status == wk.STATUS_PROGRESS:
            self._done_jobs += 1
            self._progressbar["value"] = self._done_jobs
            grp = getattr(self, "_grp_label", "")
            self._progress_lbl.config(text=f"{grp} — {self._done_jobs} / {self._total_jobs}")
            if self._scheduler and r.key:
                sm  = self._scheduler.get_success()
                tgt = self._scheduler.get_target(r.key)
                self._update_profile_progress(r.key, sm.get(r.key, 0), tgt)
            return

        if r.status == wk.STATUS_WATCHDOG:
            msg = r.error_detail or "Watchdog: khoi dong lai workers..."
            self._progress_lbl.config(text=f"[WD] {msg}")
            return

        if r.status == wk.STATUS_DONE:
            gname = getattr(self, "_running_group_name", "")
            if gname:
                self._auto_export_group_csv(gname)
            self._advance_to_next_group()
            return

        key = r.key or "?"
        db_key = key.split("::")[0] if "::" in key else key  # strip composite — lưu tên gốc vào DB

        if r.status in (wk.STATUS_SUCCESS, wk.STATUS_MODERATION):
            self.db.add_result(db_key, r.url, r.comment_link or "", r.status,
                               group_name=r.group_name, comment_used=r.comment_used,
                               website=r.host)
            self._refresh_success()
            return

        reason = r.status
        if r.error_detail:
            reason += f" - {r.error_detail}"

        self.db.add_result(db_key, r.url, r.comment_used or "", reason,
                           group_name=r.group_name, comment_used=r.comment_used,
                           website=r.host)
        self._refresh_fail()

    # ═══════════════════════════════════════════════════════════════
    # Action helpers
    # ═══════════════════════════════════════════════════════════════

    def _export_success_csv2(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            title="Luu ket qua thanh cong",
        )
        if not path:
            return
        sel = self._s_profile_var.get()
        grp = self._s_group_var.get() if hasattr(self, "_s_group_var") else None
        rows = self.db.get_success_results(sel, group_name=grp)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["profile", "url_goc", "link_thanh_cong", "status", "comment_used", "website"])
            w.writerows(rows)
        messagebox.showinfo("Export", f"Da luu {len(rows)} dong.")

    def _show_fail_menu(self, event) -> None:
        item = self._tree_f.identify_row(event.y)
        if item:
            self._tree_f.selection_set(item)
            self._fail_menu.post(event.x_root, event.y_root)

    def _retry_fail(self) -> None:
        item = self._tree_f.selection()
        if not item:
            return
        vals = self._tree_f.item(item[0], "values")
        url  = vals[1] if vals else ""
        if url:
            self._urls_list.append(url)
            self._lbl_url_count.config(text=f"{len(self._urls_list)} URL")
            self._notebook.select(0)
            messagebox.showinfo("Retry", "Da them URL vao cuoi danh sach.")

    def _build_tab4(self) -> None:
        """Tab 4 — Loc Link: xoa #comment-ID va/hoac loc trung lap."""
        self._tab4.columnconfigure(0, weight=1)
        self._tab4.columnconfigure(1, weight=0)
        self._tab4.columnconfigure(2, weight=1)
        self._tab4.rowconfigure(0, weight=1)

        # ── Input panel ──────────────────────────────────────────────
        in_fr = ttk.LabelFrame(self._tab4, text=" Input — Dan link vao day ", padding=6)
        in_fr.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        in_fr.rowconfigure(0, weight=1)
        in_fr.columnconfigure(0, weight=1)

        self._txt_filter_in = tk.Text(
            in_fr, wrap="none",
            bg=ENTRY_BG, fg=FG, insertbackground=FG,
            font=("Consolas", 9), relief="flat",
            selectbackground=TREE_SEL,
        )
        in_vsb = ttk.Scrollbar(in_fr, command=self._txt_filter_in.yview)
        in_hsb = ttk.Scrollbar(in_fr, orient="horizontal", command=self._txt_filter_in.xview)
        self._txt_filter_in.configure(yscrollcommand=in_vsb.set, xscrollcommand=in_hsb.set)
        self._txt_filter_in.grid(row=0, column=0, sticky="nsew")
        in_vsb.grid(row=0, column=1, sticky="ns")
        in_hsb.grid(row=1, column=0, sticky="ew")
        
        self._txt_filter_in._count_job = None

        def _update_in_count():
            try:
                n = len([l for l in self._txt_filter_in.get("1.0","end-1c").splitlines() if l.strip()])
                lbl_in_count.config(text=f"{n} dong")
            except Exception:
                pass

        def _in_modified(e):
            self._txt_filter_in.edit_modified(False)
            if self._txt_filter_in._count_job is not None:
                self.root.after_cancel(self._txt_filter_in._count_job)
            self._txt_filter_in._count_job = self.root.after(300, _update_in_count)
            
        self._txt_filter_in.bind("<<Modified>>", _in_modified)

        lbl_in_count = ttk.Label(in_fr, text="0 dong", foreground=MOD_CLR)
        lbl_in_count.grid(row=2, column=0, sticky="w", pady=(4, 0))

        # ── Button column ────────────────────────────────────────────
        btn_col = ttk.Frame(self._tab4)
        btn_col.grid(row=0, column=1, padx=8, pady=8)

        _btn_cfg = dict(
            font=("Segoe UI", 9, "bold"), relief="flat",
            padx=14, pady=8, cursor="hand2", width=18,
        )

        self._lbl_filter_stat = ttk.Label(
            btn_col, text="", wraplength=160,
            font=("Segoe UI", 8), foreground=SUCCESS_CLR)

        tk.Button(btn_col, text="Xoa #comment-ID",
                  bg=HEADING_FG, fg=BTN_FG,
                  command=self._filter_strip_comment_id,
                  **_btn_cfg).pack(pady=(0, 8))

        tk.Button(btn_col, text="Xoa trung lap",
                  bg=MOD_CLR, fg=BTN_FG,
                  command=self._filter_dedup,
                  **_btn_cfg).pack(pady=(0, 8))

        tk.Button(btn_col, text="Ca hai (strip + dedup)",
                  bg=SUCCESS_CLR, fg=BTN_FG,
                  command=self._filter_both,
                  **_btn_cfg).pack(pady=(0, 16))

        tk.Button(btn_col, text="Xoa Output",
                  bg=ENTRY_BG, fg=FG,
                  command=lambda: (
                      self._txt_filter_out.config(state="normal"),
                      self._txt_filter_out.delete("1.0", "end"),
                      self._txt_filter_out.config(state="disabled"),
                      self._lbl_filter_stat.config(text=""),
                  ),
                  **_btn_cfg).pack(pady=(0, 8))

        tk.Button(btn_col, text="Copy Output",
                  bg=ENTRY_BG, fg=HEADING_FG,
                  command=self._filter_copy_output,
                  **_btn_cfg).pack(pady=(0, 16))

        tk.Button(btn_col, text="Xao tron (Random)",
                  bg="#8E44AD", fg=BTN_FG,
                  command=self._filter_shuffle,
                  **_btn_cfg).pack(pady=(0, 8))

        # Chuyển output về Tab 1 URL
        tk.Button(btn_col, text="Dung lam URL chay",
                  bg=BTN_STOP, fg=BTN_FG,
                  command=self._filter_send_to_tab1,
                  **_btn_cfg).pack()

        self._lbl_filter_stat.pack(pady=(10, 0))

        # ── Domain tools ──────────────────────────────────────────────
        dom_fr = ttk.LabelFrame(btn_col, text=" Theo domain ", padding=6)
        dom_fr.pack(fill="x", pady=(12, 0))

        tk.Button(dom_fr, text="Thong ke domain",
                  bg=HEADING_FG, fg=BTN_FG,
                  command=self._filter_domain_stats,
                  **_btn_cfg).pack(pady=(0, 8))

        limit_row = ttk.Frame(dom_fr)
        limit_row.pack(fill="x", pady=(0, 8))
        ttk.Label(limit_row, text="Max/domain:").pack(side="left")
        self._domain_limit_var = tk.IntVar(value=10)
        ttk.Spinbox(limit_row, from_=1, to=999, width=4,
                    textvariable=self._domain_limit_var).pack(side="left", padx=(4, 8))
        tk.Button(limit_row, text="Gioi han",
                  font=("Segoe UI", 8, "bold"), relief="flat",
                  bg=MOD_CLR, fg=BTN_FG,
                  padx=8, pady=4, cursor="hand2",
                  command=self._filter_domain_limit).pack(side="left")

        rm_row = ttk.Frame(dom_fr)
        rm_row.pack(fill="x")
        self._ent_domain_rm = tk.Entry(
            rm_row, bg=ENTRY_BG, fg=FG, insertbackground=FG,
            font=("Consolas", 8), relief="flat",
        )
        self._ent_domain_rm.pack(side="left", fill="x", expand=True, padx=(0, 4))
        tk.Button(rm_row, text="Xoa domain",
                  font=("Segoe UI", 8, "bold"), relief="flat",
                  bg=BTN_STOP, fg=BTN_FG,
                  padx=8, pady=4, cursor="hand2",
                  command=self._filter_domain_remove).pack(side="left")

        # ── Output panel ─────────────────────────────────────────────
        out_fr = ttk.LabelFrame(self._tab4, text=" Output — Ket qua sau loc ", padding=6)
        out_fr.grid(row=0, column=2, sticky="nsew", padx=(4, 8), pady=8)
        out_fr.rowconfigure(0, weight=1)
        out_fr.columnconfigure(0, weight=1)

        self._txt_filter_out = tk.Text(
            out_fr, wrap="none", state="disabled",
            bg=TREE_BG, fg=SUCCESS_CLR,
            selectbackground=TREE_SEL, selectforeground=FG,
            font=("Consolas", 9), relief="flat", cursor="xterm",
        )
        out_vsb = ttk.Scrollbar(out_fr, command=self._txt_filter_out.yview)
        out_hsb = ttk.Scrollbar(out_fr, orient="horizontal", command=self._txt_filter_out.xview)
        self._txt_filter_out.configure(yscrollcommand=out_vsb.set, xscrollcommand=out_hsb.set)
        self._txt_filter_out.grid(row=0, column=0, sticky="nsew")
        out_vsb.grid(row=0, column=1, sticky="ns")
        out_hsb.grid(row=1, column=0, sticky="ew")

        self._lbl_out_count = ttk.Label(out_fr, text="0 dong", foreground=SUCCESS_CLR)
        self._lbl_out_count.grid(row=2, column=0, sticky="w", pady=(4, 0))

    # ── Tab 4 filter helpers ───────────────────────────────────────────────────

    _COMMENT_FRAG_RE = re.compile(r"#comment-\d+$", re.IGNORECASE)

    def _filter_get_input_lines(self) -> list[str]:
        return [l.strip() for l in
                self._txt_filter_in.get("1.0", "end-1c").splitlines() if l.strip()]

    def _filter_write_output(self, lines: list[str], msg: str) -> None:
        self._txt_filter_out.config(state="normal")
        self._txt_filter_out.delete("1.0", "end")
        self._txt_filter_out.insert("end", "\n".join(lines))
        self._txt_filter_out.config(state="disabled")
        self._lbl_out_count.config(text=f"{len(lines)} dong")
        self._lbl_filter_stat.config(text=msg)

    def _filter_strip_comment_id(self) -> None:
        lines = self._filter_get_input_lines()
        result = [self._COMMENT_FRAG_RE.sub("", l).rstrip("/") + "/"
                  if "#comment-" in l.lower() else l
                  for l in lines]
        removed = sum(1 for a, b in zip(lines, result) if a != b)
        self._filter_write_output(result, f"Da xoa #{removed} comment-ID fragment.")

    def _filter_dedup(self) -> None:
        lines = self._filter_get_input_lines()
        seen: set[str] = set()
        result = []
        for l in lines:
            if l not in seen:
                seen.add(l)
                result.append(l)
        removed = len(lines) - len(result)
        self._filter_write_output(result, f"Da xoa {removed} dong trung lap.")

    def _filter_both(self) -> None:
        lines = self._filter_get_input_lines()
        # Strip fragments first
        stripped = [self._COMMENT_FRAG_RE.sub("", l).rstrip("/") + "/"
                    if "#comment-" in l.lower() else l
                    for l in lines]
        # Then dedup preserving order
        seen: set[str] = set()
        result = []
        for l in stripped:
            if l not in seen:
                seen.add(l)
                result.append(l)
        frag_rm  = sum(1 for a, b in zip(lines, stripped) if a != b)
        dup_rm   = len(stripped) - len(result)
        self._filter_write_output(
            result,
            f"Da xoa {frag_rm} fragment + {dup_rm} trung lap.\nCon lai: {len(result)} URL.")

    def _filter_shuffle(self) -> None:
        """Xáo trộn ngẫu nhiên thứ tự các link từ Input → Output."""
        lines = self._filter_get_input_lines()
        if not lines:
            self._lbl_filter_stat.config(text="Input trong!")
            return
        random.shuffle(lines)
        self._filter_write_output(lines, f"Da xao tron {len(lines)} link.")

    def _filter_copy_output(self) -> None:
        content = self._txt_filter_out.get("1.0", "end-1c").strip()
        if content:
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            n = len([l for l in content.splitlines() if l.strip()])
            messagebox.showinfo("Copied", f"Da copy {n} URL.")

    def _filter_send_to_tab1(self) -> None:
        content = self._txt_filter_out.get("1.0", "end-1c").strip()
        if not content:
            messagebox.showwarning("Trong", "Output chua co du lieu.")
            return
        
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        self._urls_list = lines.copy()
        self._lbl_url_count.config(text=f"{len(self._urls_list)} URL")

        self._notebook.select(0)
        messagebox.showinfo("Da chuyen", f"Da dua {len(self._urls_list)} URL sang Tab 1.")

    # ── Domain helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _extract_domain(url: str) -> str:
        try:
            parsed = urlparse(url.strip())
            host = parsed.netloc or parsed.path.split("/")[0]
            return host.lower().strip()
        except Exception:
            return ""

    def _filter_domain_stats(self) -> None:
        lines = self._filter_get_input_lines()
        if not lines:
            self._lbl_filter_stat.config(text="Input trong!")
            return
        cnt: Counter[str] = Counter()
        for l in lines:
            d = self._extract_domain(l)
            if d:
                cnt[d] += 1
        result = [f"{domain}  \u2014  {n} link" for domain, n in cnt.most_common()]
        self._filter_write_output(
            result,
            f"{len(cnt)} domain  |  {sum(cnt.values())} link tong cong")

    def _filter_domain_limit(self) -> None:
        lines = self._filter_get_input_lines()
        if not lines:
            self._lbl_filter_stat.config(text="Input trong!")
            return
        try:
            n = max(1, int(self._domain_limit_var.get()))
        except Exception:
            n = 10
        domain_count: dict[str, int] = {}
        result: list[str] = []
        for l in lines:
            d = self._extract_domain(l)
            cur = domain_count.get(d, 0)
            if cur < n:
                result.append(l)
                domain_count[d] = cur + 1
        removed = len(lines) - len(result)
        self._filter_write_output(
            result,
            f"Da xoa {removed} link vuot gioi han ({n}/domain). Con lai: {len(result)} link.")

    def _filter_domain_remove(self) -> None:
        raw = self._ent_domain_rm.get().strip()
        if not raw:
            messagebox.showwarning("Thieu", "Nhap URL hoac domain can xoa.")
            return
        target = self._extract_domain(raw)
        if not target:
            messagebox.showwarning("Thieu", "Nhap URL hoac domain can xoa.")
            return
        lines = self._filter_get_input_lines()
        result = [l for l in lines if self._extract_domain(l) != target]
        removed = len(lines) - len(result)
        self._filter_write_output(
            result,
            f"Da xoa {removed} link cua domain '{target}'. Con lai: {len(result)} link.")

    # ── Common helpers ─────────────────────────────────────────────────────────

    def _export_tree(self, tree: ttk.Treeview, headers: list) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            title="Luu ket qua",
        )
        if not path:
            return
        rows = [tree.item(iid, "values") for iid in tree.get_children()]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(rows)
        messagebox.showinfo("Export", f"Da luu {len(rows)} dong.")

    # ── Persist state ───────────────────────────────────────────────────────────

    def _gather_state(self) -> dict:
        """Thu thap toan bo trang thai hien tai de luu."""
        # Luu profiles hien tai vao group dang xem
        if 0 <= self._current_group_view < len(self._groups):
            self._groups[self._current_group_view]["profiles"] = self._collect_current_profiles()

        urls = "\n".join(self._urls_list)
        proxy_text = self._proxy_text.get("1.0", "end-1c").strip()
        return {
            "groups": self._groups,
            "urls": urls,
            "headless": self._headless_var.get(),
            "workers":  self._worker_var.get(),
            "bypass_name": self._bypass_name_var.get(),
            "proxies": proxy_text,
            "proxy_rotate_every": self._proxy_rotate_var.get(),
        }

    def _save_state(self) -> None:
        """Luu trang thai vao gui_state.json."""
        try:
            state = self._gather_state()
            # Khong indent de giam dung luong (89K URLs → 7MB vs 12MB)
            data = json.dumps(state, ensure_ascii=False)
            self._state_file.write_text(data, encoding="utf-8")
        except Exception as exc:
            print(f"[gui] Khong the luu state: {exc}")

    def _auto_save(self) -> None:
        """Tu dong luu state moi 120 giay (giam tan suat de tranh freeze UI)."""
        try:
            self._save_state()
        except Exception:
            pass
        self.root.after(120_000, self._auto_save) 

    def _load_state(self) -> None:
        """Tai trang thai tu gui_state.json neu ton tai."""
        if not self._state_file.exists():
            self._groups = [{"name": "Default", "profiles": []}]
            self._display_group(0)
            return
        try:
            raw = self._state_file.read_text(encoding="utf-8")
            state = json.loads(raw)
        except Exception as exc:
            print(f"[gui] Khong the doc state: {exc}")
            self._groups = [{"name": "Default", "profiles": []}]
            self._display_group(0)
            return

        try:
            # Backward compatible: "groups" moi hoac "profiles" cu
            if "groups" in state:
                self._groups = state["groups"]
            elif "profiles" in state and state["profiles"]:
                self._groups = [{"name": "Default", "profiles": state["profiles"]}]
            else:
                self._groups = [{"name": "Default", "profiles": []}]

            if not self._groups:
                self._groups = [{"name": "Default", "profiles": []}]

            self._display_group(0)

            urls = state.get("urls", "")
            if urls:
                self._urls_list = [u.strip() for u in urls.splitlines() if u.strip()]
                self._lbl_url_count.config(text=f"{len(self._urls_list)} URL")

            self._headless_var.set(state.get("headless", False))
            self._bypass_name_var.set(state.get("bypass_name", False))
            try:
                self._worker_var.set(int(state.get("workers", 3)))
            except Exception:
                pass

            # Restore proxy config
            proxy_text = state.get("proxies", "")
            if proxy_text:
                self._proxy_text.delete("1.0", "end")
                self._proxy_text.insert("1.0", proxy_text)
            try:
                self._proxy_rotate_var.set(int(state.get("proxy_rotate_every", 250)))
            except Exception:
                pass

            total_profiles = sum(len(g.get("profiles", [])) for g in self._groups)
            print(f"[gui] Da tai state: {len(self._groups)} groups, "
                  f"{total_profiles} profiles, {len(urls)} chars URLs")
        except Exception as exc:
            print(f"[gui] Loi khi tai state: {exc}")
            if not self._groups:
                self._groups = [{"name": "Default", "profiles": []}]
            if not self._key_rows:
                self._display_group(0)

    def _clear_success_data(self) -> None:
        """Xoa toan bo ket qua (in-memory + sqlite)."""
        self.db.clear_results()
        self._refresh_success()
        self._refresh_fail()

    def _reset_all_url_data(self) -> None:
        """Xoá toàn bộ results + reset queue → PENDING. Giữ nguyên config group/comment."""
        if not messagebox.askyesno(
            "XOÁ TẤT CẢ?",
            "Hành động này sẽ:\n"
            "  • Xoá toàn bộ kết quả (SUCCESS, FAILED, MODERATION)\n"
            "  • Reset tất cả URL trong queue về PENDING\n"
            "  • Xoá domain cooldown\n\n"
            "Cấu hình group, comment, proxy KHÔNG bị xoá.\n"
            "Tool sẽ chạy lại từ đầu với danh sách URL hiện tại.\n\n"
            "Tiếp tục?",
        ):
            return
        self.db.reset_all_url_data()
        self._refresh_success()
        self._refresh_fail()
        self._progressbar["value"] = 0
        self._progress_lbl.config(text="")
        messagebox.showinfo("Đã reset", "Đã xoá toàn bộ kết quả và reset URL về PENDING.\nBấm Bắt đầu để chạy lại từ đầu.")

    # ═══════════════════════════════════════════════════════════════
    # TAB 5 — Gen Comment (Groq AI)
    # ═══════════════════════════════════════════════════════════════

    def _build_tab5(self) -> None:
        self._tab5.columnconfigure(0, weight=1)
        self._tab5.rowconfigure(3, weight=1)  # output area expands

        # ── Gen comment state ────────────────────────────────────
        self._gc_comments: dict[str, list[str]] = {}
        self._gc_stop = threading.Event()
        self._gc_thread: threading.Thread | None = None

        # ── Top: API keys + keywords side by side ────────────────
        top = ttk.Frame(self._tab5)
        top.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 4))
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)
        top.rowconfigure(0, weight=1)

        # -- API Keys panel --
        key_fr = ttk.LabelFrame(top, text=" API Keys (moi dong 1 key) ", padding=6)
        key_fr.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        key_fr.rowconfigure(0, weight=1)
        key_fr.columnconfigure(0, weight=1)

        self._txt_gc_keys = tk.Text(
            key_fr, height=5, wrap="none",
            bg=ENTRY_BG, fg=FG, insertbackground=FG,
            font=("Consolas", 9), relief="flat",
            selectbackground=TREE_SEL,
        )
        key_vsb = ttk.Scrollbar(key_fr, command=self._txt_gc_keys.yview)
        self._txt_gc_keys.configure(yscrollcommand=key_vsb.set)
        self._txt_gc_keys.grid(row=0, column=0, sticky="nsew")
        key_vsb.grid(row=0, column=1, sticky="ns")

        # Auto-save keys on edit
        self._gc_key_save_job = None
        def _keys_modified(e):
            self._txt_gc_keys.edit_modified(False)
            if self._gc_key_save_job is not None:
                self.root.after_cancel(self._gc_key_save_job)
            self._gc_key_save_job = self.root.after(800, self._gc_save_keys)
        self._txt_gc_keys.bind("<<Modified>>", _keys_modified)

        # Load existing keys
        existing = cg.load_keys_from_env()
        if existing:
            self._txt_gc_keys.insert("1.0", "\n".join(existing))
            self._txt_gc_keys.edit_modified(False)

        # -- Keywords panel --
        kw_fr = ttk.LabelFrame(
            top,
            text=" Keywords (keyword-so_luong-ngon_ngu, VD: dich vu seo-20-vi) ",
            padding=6,
        )
        kw_fr.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        kw_fr.rowconfigure(0, weight=1)
        kw_fr.columnconfigure(0, weight=1)

        self._txt_gc_kw = tk.Text(
            kw_fr, height=5, wrap="none",
            bg=ENTRY_BG, fg=FG, insertbackground=FG,
            font=("Consolas", 9), relief="flat",
            selectbackground=TREE_SEL,
        )
        kw_vsb = ttk.Scrollbar(kw_fr, command=self._txt_gc_kw.yview)
        self._txt_gc_kw.configure(yscrollcommand=kw_vsb.set)
        self._txt_gc_kw.grid(row=0, column=0, sticky="nsew")
        kw_vsb.grid(row=0, column=1, sticky="ns")

        # ── Control bar ──────────────────────────────────────────
        ctrl = ttk.Frame(self._tab5)
        ctrl.grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 4))

        self._btn_gc_start = tk.Button(
            ctrl, text="Tao Comment",
            font=("Segoe UI", 10, "bold"),
            bg=BTN_START, fg=BTN_FG,
            relief="flat", padx=14, pady=6,
            cursor="hand2", command=self._gc_on_start,
        )
        self._btn_gc_start.pack(side="left")

        self._btn_gc_stop = tk.Button(
            ctrl, text="Dung",
            font=("Segoe UI", 10, "bold"),
            bg=BTN_STOP, fg=BTN_FG,
            relief="flat", padx=14, pady=6,
            cursor="hand2", command=self._gc_on_stop, state="disabled",
        )
        self._btn_gc_stop.pack(side="left", padx=(8, 0))

        tk.Button(
            ctrl, text="Copy All",
            font=("Segoe UI", 9, "bold"),
            bg=ENTRY_BG, fg=HEADING_FG,
            relief="flat", padx=14, pady=6,
            cursor="hand2", command=self._gc_copy_all,
        ).pack(side="left", padx=(8, 0))

        tk.Button(
            ctrl, text="Xoa tat ca",
            font=("Segoe UI", 9, "bold"),
            bg=ENTRY_BG, fg=FAIL_CLR,
            relief="flat", padx=14, pady=6,
            cursor="hand2", command=self._gc_clear_all,
        ).pack(side="left", padx=(8, 0))

        self._gc_target_group_var = tk.StringVar()
        self._gc_target_group = ttk.Combobox(
            ctrl, textvariable=self._gc_target_group_var,
            values=[g["name"] for g in self._groups],
            state="readonly", width=16,
        )
        self._gc_target_group.pack(side="left", padx=(8, 0))
        if self._groups:
            self._gc_target_group.current(0)

        tk.Button(
            ctrl, text="Them vao GR",
            font=("Segoe UI", 9, "bold"),
            bg=MOD_CLR, fg=BTN_FG,
            relief="flat", padx=14, pady=6,
            cursor="hand2", command=self._gc_send_to_group,
        ).pack(side="left", padx=(4, 0))

        # Email + Website for Tab 1 profiles
        ttk.Label(ctrl, text="Email:").pack(side="left", padx=(16, 2))
        self._gc_email_var = tk.StringVar()
        tk.Entry(
            ctrl, textvariable=self._gc_email_var,
            bg=ENTRY_BG, fg=FG, insertbackground=FG,
            font=("Consolas", 9), relief="flat", width=22,
        ).pack(side="left")

        ttk.Label(ctrl, text="Web:").pack(side="left", padx=(8, 2))
        self._gc_host_var = tk.StringVar()
        tk.Entry(
            ctrl, textvariable=self._gc_host_var,
            bg=ENTRY_BG, fg=FG, insertbackground=FG,
            font=("Consolas", 9), relief="flat", width=22,
        ).pack(side="left")

        # ── Progress row ─────────────────────────────────────────
        ctrl2 = ttk.Frame(self._tab5)
        ctrl2.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 4))

        # Filter
        ttk.Label(ctrl2, text="Filter:").pack(side="left", padx=(0, 4))
        self._gc_filter_var = tk.StringVar(value="Tat ca")
        self._gc_filter_cb = ttk.Combobox(
            ctrl2, textvariable=self._gc_filter_var,
            values=["Tat ca"], state="readonly", width=28,
        )
        self._gc_filter_cb.pack(side="left")
        self._gc_filter_cb.bind("<<ComboboxSelected>>", lambda e: self._gc_refresh_output())

        # Progress
        self._gc_progress_lbl = ttk.Label(ctrl2, text="")
        self._gc_progress_lbl.pack(side="right", padx=(8, 0))

        self._gc_progressbar = ttk.Progressbar(
            ctrl2, mode="determinate",
            style="Horizontal.TProgressbar",
        )
        self._gc_progressbar.pack(side="right", fill="x", expand=True, padx=(8, 0))

        # Counter label
        self._gc_counter_lbl = ttk.Label(
            ctrl2, text="0 comment",
            foreground=MOD_CLR, font=("Segoe UI", 9, "bold"),
        )
        self._gc_counter_lbl.pack(side="right", padx=(8, 0))

        # ── Output area ──────────────────────────────────────────
        out_fr = ttk.LabelFrame(self._tab5, text=" Comments ", padding=6)
        out_fr.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 8))
        out_fr.rowconfigure(0, weight=1)
        out_fr.columnconfigure(0, weight=1)

        self._txt_gc_out = tk.Text(
            out_fr, wrap="word", state="disabled",
            bg=TREE_BG, fg=SUCCESS_CLR,
            selectbackground=TREE_SEL, selectforeground=FG,
            font=("Consolas", 9), relief="flat", cursor="xterm",
        )
        out_vsb = ttk.Scrollbar(out_fr, command=self._txt_gc_out.yview)
        self._txt_gc_out.configure(yscrollcommand=out_vsb.set)
        self._txt_gc_out.grid(row=0, column=0, sticky="nsew")
        out_vsb.grid(row=0, column=1, sticky="ns")

    # ── Tab 6 helpers ──────────────────────────────────────────────────────────

    def _gc_save_keys(self) -> None:
        """Auto-save API keys to .env."""
        raw = self._txt_gc_keys.get("1.0", "end-1c")
        keys = [l.strip() for l in raw.splitlines() if l.strip()]
        cg.save_keys_to_env(keys)

    def _gc_get_keys(self) -> list[str]:
        raw = self._txt_gc_keys.get("1.0", "end-1c")
        return [l.strip() for l in raw.splitlines() if l.strip()]

    def _gc_update_counter(self) -> None:
        total = sum(len(v) for v in self._gc_comments.values())
        parts = " | ".join(f"{k}: {len(v)}" for k, v in self._gc_comments.items() if v)
        text = f"{total} comment"
        if parts:
            text += f" ({parts})"
        self._gc_counter_lbl.config(text=text)

    def _gc_update_filter_combo(self) -> None:
        values = ["Tat ca"] + [k for k in self._gc_comments if self._gc_comments[k]]
        self._gc_filter_cb["values"] = values
        if self._gc_filter_var.get() not in values:
            self._gc_filter_var.set("Tat ca")

    def _gc_refresh_output(self) -> None:
        """Rebuild the output text from stored comments, filtered."""
        sel = self._gc_filter_var.get()
        self._txt_gc_out.config(state="normal")
        self._txt_gc_out.delete("1.0", "end")

        if sel == "Tat ca":
            for kw, cmts in self._gc_comments.items():
                for c in cmts:
                    self._txt_gc_out.insert("end", c + "\n")
        else:
            for c in self._gc_comments.get(sel, []):
                self._txt_gc_out.insert("end", c + "\n")

        self._txt_gc_out.config(state="disabled")
        self._txt_gc_out.see("end")

    def _gc_on_start(self) -> None:
        keys = self._gc_get_keys()
        if not keys:
            messagebox.showwarning("Thieu Key", "Nhap it nhat 1 API key.")
            return

        raw_kw = self._txt_gc_kw.get("1.0", "end-1c")
        tasks = cg.parse_keywords(raw_kw)
        if not tasks:
            messagebox.showwarning("Thieu Keyword", "Nhap it nhat 1 keyword.")
            return

        # Reset
        self._gc_comments.clear()
        for t in tasks:
            self._gc_comments[t.keyword] = []
        self._gc_update_filter_combo()
        self._txt_gc_out.config(state="normal")
        self._txt_gc_out.delete("1.0", "end")
        self._txt_gc_out.config(state="disabled")

        grand_total = sum(t.count for t in tasks)
        self._gc_progressbar["maximum"] = grand_total
        self._gc_progressbar["value"] = 0
        self._gc_progress_lbl.config(text=f"0 / {grand_total}")
        self._gc_update_counter()

        self._btn_gc_start.config(state="disabled")
        self._btn_gc_stop.config(state="normal")
        self._gc_stop.clear()

        def _callback(prog: cg.GenProgress):
            # Schedule UI update on main thread
            self.root.after(0, lambda p=prog: self._gc_handle_progress(p))

        self._gc_thread = threading.Thread(
            target=cg.generate_comments,
            args=(keys, tasks, _callback, self._gc_stop),
            daemon=True,
        )
        self._gc_thread.start()

    def _gc_handle_progress(self, prog: cg.GenProgress) -> None:
        if prog.error:
            self._gc_progress_lbl.config(text=prog.error)

        if prog.comments and prog.keyword:
            if prog.keyword not in self._gc_comments:
                self._gc_comments[prog.keyword] = []
            self._gc_comments[prog.keyword].extend(prog.comments)

            # Live append to output (if filter matches)
            sel = self._gc_filter_var.get()
            if sel in ("Tat ca", prog.keyword):
                self._txt_gc_out.config(state="normal")
                for c in prog.comments:
                    self._txt_gc_out.insert("end", c + "\n")
                self._txt_gc_out.config(state="disabled")
                self._txt_gc_out.see("end")

            self._gc_update_counter()
            self._gc_update_filter_combo()

        self._gc_progressbar["value"] = prog.done
        self._gc_progress_lbl.config(
            text=f"{prog.done} / {prog.total}" + (" [Xong]" if prog.finished else ""),
        )

        if prog.finished:
            self._btn_gc_start.config(state="normal")
            self._btn_gc_stop.config(state="disabled")

    def _gc_on_stop(self) -> None:
        self._gc_stop.set()
        self._btn_gc_stop.config(state="disabled")
        self._gc_progress_lbl.config(
            text=self._gc_progress_lbl.cget("text") + "  [dang dung...]")

    def _gc_copy_all(self) -> None:
        content = self._txt_gc_out.get("1.0", "end-1c").strip()
        if not content:
            messagebox.showwarning("Trong", "Chua co comment nao.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        n = len([l for l in content.splitlines() if l.strip()])
        messagebox.showinfo("Copied", f"Da copy {n} comment.")

    def _gc_clear_all(self) -> None:
        self._gc_comments.clear()
        self._txt_gc_out.config(state="normal")
        self._txt_gc_out.delete("1.0", "end")
        self._txt_gc_out.config(state="disabled")
        self._gc_update_counter()
        self._gc_update_filter_combo()
        self._gc_progressbar["value"] = 0
        self._gc_progress_lbl.config(text="")

    def _gc_send_to_group(self) -> None:
        """Them profiles vao group duoc chon (khong xoa profiles cu)."""
        if not self._gc_comments or not any(self._gc_comments.values()):
            messagebox.showwarning("Trong", "Chua co comment nao de them.")
            return

        email = self._gc_email_var.get().strip()
        host = self._gc_host_var.get().strip()
        if not email:
            messagebox.showwarning("Thieu Email", "Nhap email truoc khi them.")
            return

        target_idx = self._gc_target_group.current()
        if target_idx < 0 or target_idx >= len(self._groups):
            messagebox.showwarning("Group", "Chon group truoc khi them.")
            return

        # Save current Tab1 view into its group first
        if 0 <= self._current_group_view < len(self._groups):
            self._groups[self._current_group_view]["profiles"] = self._collect_current_profiles()

        group_name = self._groups[target_idx]["name"]
        count = 0
        for keyword, cmts in self._gc_comments.items():
            if not cmts:
                continue
            prof = {
                "name": keyword,
                "email": email,
                "host": host,
                "target": len(cmts),
                "comments": cmts,
            }
            self._groups[target_idx].setdefault("profiles", []).append(prof)
            count += 1

        # If viewing the target group, refresh cards
        if target_idx == self._current_group_view:
            self._display_group(target_idx)

        self._update_profile_combos()
        total_cmts = sum(len(v) for v in self._gc_comments.values())
        messagebox.showinfo(
            "Da them",
            f"Da them {count} profile ({total_cmts} comment) vao {group_name}.")

    # ═══════════════════════════════════════════════════════════════
    # TAB 6 — Log viewer
    # ═══════════════════════════════════════════════════════════════

    def _build_tab6(self) -> None:
        self._tab6.columnconfigure(0, weight=1)
        self._tab6.rowconfigure(1, weight=1)

        # ── Toolbar ──
        bar = ttk.Frame(self._tab6)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))

        ttk.Label(bar, text="Level:", font=("Segoe UI", 9)).pack(side="left")
        self._log_level_var = tk.StringVar(value="INFO")
        level_cb = ttk.Combobox(
            bar, textvariable=self._log_level_var,
            values=["DEBUG", "INFO", "WARNING", "ERROR"],
            state="readonly", width=9,
        )
        level_cb.pack(side="left", padx=(4, 16))
        level_cb.bind("<<ComboboxSelected>>", lambda _: self._apply_log_level())

        self._log_autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            bar, text="Auto-scroll",
            variable=self._log_autoscroll_var,
        ).pack(side="left", padx=(0, 16))

        self._log_filter_var = tk.StringVar()
        ttk.Label(bar, text="Filter:", font=("Segoe UI", 9)).pack(side="left")
        filter_entry = ttk.Entry(bar, textvariable=self._log_filter_var, width=20)
        filter_entry.pack(side="left", padx=(4, 16))
        filter_entry.bind("<Return>", lambda _: self._log_apply_filter())
        ttk.Button(bar, text="Apply", command=self._log_apply_filter).pack(side="left", padx=(0, 16))

        tk.Button(
            bar, text="Clear",
            font=("Segoe UI", 8, "bold"), bg=ENTRY_BG, fg=HEADING_FG,
            relief="flat", padx=8, pady=2, cursor="hand2",
            command=self._log_clear,
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            bar, text="Save to file",
            font=("Segoe UI", 8, "bold"), bg=ENTRY_BG, fg=HEADING_FG,
            relief="flat", padx=8, pady=2, cursor="hand2",
            command=self._log_save,
        ).pack(side="left")

        self._log_count_lbl = ttk.Label(bar, text="0 dong", font=("Segoe UI", 8))
        self._log_count_lbl.pack(side="right", padx=(0, 8))

        # ── Text area ──
        txt_fr = ttk.Frame(self._tab6)
        txt_fr.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        txt_fr.rowconfigure(0, weight=1)
        txt_fr.columnconfigure(0, weight=1)

        vsb = ttk.Scrollbar(txt_fr, orient="vertical")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(txt_fr, orient="horizontal")
        hsb.grid(row=1, column=0, sticky="ew")

        self._log_text = tk.Text(
            txt_fr,
            bg=ENTRY_BG, fg=HEADING_FG,
            font=("Consolas", 9),
            wrap="none",
            state="disabled",
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
        )
        self._log_text.grid(row=0, column=0, sticky="nsew")
        vsb.config(command=self._log_text.yview)
        hsb.config(command=self._log_text.xview)

        # Color tags per level
        self._log_text.tag_config("DEBUG",   foreground="#585b70")  # muted grey
        self._log_text.tag_config("INFO",    foreground="#89b4fa")  # blue
        self._log_text.tag_config("WARNING", foreground="#fab387")  # orange
        self._log_text.tag_config("ERROR",   foreground="#f38ba8")  # red
        self._log_text.tag_config("CRITICAL",foreground="#f38ba8", font=("Consolas", 9, "bold"))

        self._log_lines: list[tuple[str, str]] = []  # (level, text)
        self._log_line_count = 0
        self._LOG_MAX_LINES = 5000  # giu toi da de tranh an RAM

    def _attach_log_handler(self) -> None:
        """Gan GuiLogHandler vao logger 'atc' — forward tat ca log len Tab 6."""
        handler = _GuiLogHandler(self._append_log_line)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [W%(thread)d] %(levelname)-8s %(message)s",
            datefmt="%H:%M:%S",
        ))
        handler.setLevel(logging.DEBUG)
        atc_log = logging.getLogger("atc")
        atc_log.setLevel(logging.DEBUG)
        atc_log.addHandler(handler)
        self._gui_log_handler = handler

    def _apply_log_level(self) -> None:
        level_str = self._log_level_var.get()
        level = getattr(logging, level_str, logging.INFO)
        if hasattr(self, "_gui_log_handler"):
            self._gui_log_handler.setLevel(level)

    def _append_log_line(self, level: str, text: str) -> None:
        """Goi tu handler (background thread) — schedule ve main thread."""
        self.root.after(0, self._write_log_line, level, text)

    def _write_log_line(self, level: str, text: str) -> None:
        """Them 1 dong vao log text widget (main thread)."""
        # Trim khi qua gioi han
        if self._log_line_count >= self._LOG_MAX_LINES:
            self._log_text.config(state="normal")
            self._log_text.delete("1.0", "501.0")  # xoa 500 dong dau
            self._log_line_count -= 500
            self._log_lines = self._log_lines[500:]
            self._log_text.config(state="disabled")

        self._log_lines.append((level, text))
        self._log_line_count += 1

        # Kiem tra filter
        flt = self._log_filter_var.get().strip().lower()
        if flt and flt not in text.lower():
            return

        self._log_text.config(state="normal")
        self._log_text.insert("end", text + "\n", level)
        self._log_text.config(state="disabled")

        if self._log_autoscroll_var.get():
            self._log_text.see("end")

        self._log_count_lbl.config(text=f"{self._log_line_count} dong")

    def _log_apply_filter(self) -> None:
        """Re-render toan bo log voi filter hien tai."""
        flt = self._log_filter_var.get().strip().lower()
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        for level, text in self._log_lines:
            if flt and flt not in text.lower():
                continue
            self._log_text.insert("end", text + "\n", level)
        self._log_text.config(state="disabled")
        if self._log_autoscroll_var.get():
            self._log_text.see("end")

    def _log_clear(self) -> None:
        self._log_lines.clear()
        self._log_line_count = 0
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")
        self._log_count_lbl.config(text="0 dong")

    def _log_save(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
            title="Luu log ra file",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for _, text in self._log_lines:
                    f.write(text + "\n")
        except Exception as exc:
            messagebox.showerror("Loi", f"Khong luu duoc: {exc}")

    # ── Common helpers ─────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        """Luu state roi dong app."""
        self._save_state()
        self.root.destroy()

    def run(self) -> None:
        # Bat dau auto-save sau 120s
        self.root.after(120_000, self._auto_save)
        self.root.mainloop()
