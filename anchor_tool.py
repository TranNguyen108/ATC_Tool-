"""
anchor_tool.py — Công cụ chuyển đổi Anchor Text độc lập.
Chạy: python anchor_tool.py
Build: pyinstaller --onefile --windowed --name AnchorTool --clean anchor_tool.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import re


class AnchorToolApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Anchor Text Converter")
        self.root.geometry("900x650")
        self.root.minsize(700, 500)
        self.root.configure(bg="#2b2b2b")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#2b2b2b")
        style.configure("TLabel", background="#2b2b2b", foreground="#ffffff", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10, "bold"))
        style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"), foreground="#00d4ff", background="#2b2b2b")

        self._build_ui()

    # ──────────────────────── UI ────────────────────────
    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # ── Header ──
        ttk.Label(main, text="🔗 Anchor Text Converter", style="Header.TLabel").pack(pady=(0, 10))

        # ── Key + URL row ──
        top = ttk.Frame(main)
        top.pack(fill="x", pady=(0, 8))

        ttk.Label(top, text="Key:").pack(side="left", padx=(0, 4))
        self._ent_key = tk.Entry(top, font=("Consolas", 11), bg="#1e1e1e", fg="#ffffff",
                                  insertbackground="#ffffff", width=20)
        self._ent_key.pack(side="left", padx=(0, 12))

        ttk.Label(top, text="URL:").pack(side="left", padx=(0, 4))
        self._ent_url = tk.Entry(top, font=("Consolas", 11), bg="#1e1e1e", fg="#ffffff",
                                  insertbackground="#ffffff")
        self._ent_url.pack(side="left", fill="x", expand=True, padx=(0, 12))

        # Case checkbox
        self._case_var = tk.BooleanVar(value=False)
        self._chk_case = tk.Checkbutton(top, text="Phân biệt hoa/thường",
                                         variable=self._case_var,
                                         bg="#2b2b2b", fg="#ffffff",
                                         selectcolor="#1e1e1e",
                                         activebackground="#2b2b2b",
                                         activeforeground="#ffffff",
                                         font=("Segoe UI", 9))
        self._chk_case.pack(side="left")

        # ── Option: chỉ thay thế lần đầu ──
        opt_row = ttk.Frame(main)
        opt_row.pack(fill="x", pady=(0, 8))

        self._first_only_var = tk.BooleanVar(value=True)
        tk.Checkbutton(opt_row, text="Chỉ thay thế lần xuất hiện đầu tiên trong mỗi comment",
                       variable=self._first_only_var,
                       bg="#2b2b2b", fg="#ffffff",
                       selectcolor="#1e1e1e",
                       activebackground="#2b2b2b",
                       activeforeground="#ffffff",
                       font=("Segoe UI", 9)).pack(side="left")

        # ── Input / Output panels ──
        panels = ttk.Frame(main)
        panels.pack(fill="both", expand=True)
        panels.columnconfigure(0, weight=1)
        panels.columnconfigure(1, weight=0)
        panels.columnconfigure(2, weight=1)
        panels.rowconfigure(0, weight=1)

        # ── INPUT ──
        left = ttk.LabelFrame(panels, text="  Input — Paste comments (mỗi dòng 1 CMT)  ")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        self._txt_in = tk.Text(left, wrap="word", font=("Consolas", 10),
                                bg="#1e1e1e", fg="#e0e0e0",
                                insertbackground="#ffffff",
                                selectbackground="#264f78")
        sb_in = ttk.Scrollbar(left, orient="vertical", command=self._txt_in.yview)
        self._txt_in.configure(yscrollcommand=sb_in.set)
        self._txt_in.pack(side="left", fill="both", expand=True)
        sb_in.pack(side="right", fill="y")

        self._lbl_in_count = ttk.Label(left, text="0 dòng")
        self._lbl_in_count.pack(side="bottom", anchor="e", padx=4)
        self._txt_in.bind("<<Modified>>", self._update_in_count)

        # ── BUTTONS (giữa) ──
        mid = ttk.Frame(panels)
        mid.grid(row=0, column=1, padx=8)

        buttons = [
            ("▶ Chuyển đổi", "#27ae60", self._convert),
            ("🔄 Đổi ngược\n(HTML → text)", "#2980b9", self._reverse),
            ("📋 Copy Output", "#8e44ad", self._copy_output),
            ("🗑 Xóa Input", "#c0392b", self._clear_input),
            ("🗑 Xóa Output", "#7f8c8d", self._clear_output),
            ("📥 Input ← Output", "#e67e22", self._output_to_input),
        ]
        for text, color, cmd in buttons:
            btn = tk.Button(mid, text=text, command=cmd,
                            bg=color, fg="white",
                            font=("Segoe UI", 9, "bold"),
                            width=18, height=2,
                            relief="flat", cursor="hand2")
            btn.pack(pady=3)

        # ── OUTPUT ──
        right = ttk.LabelFrame(panels, text="  Output — Kết quả  ")
        right.grid(row=0, column=2, sticky="nsew", padx=(4, 0))

        self._txt_out = tk.Text(right, wrap="word", font=("Consolas", 10),
                                 bg="#1e1e1e", fg="#e0e0e0",
                                 insertbackground="#ffffff",
                                 selectbackground="#264f78")
        sb_out = ttk.Scrollbar(right, orient="vertical", command=self._txt_out.yview)
        self._txt_out.configure(yscrollcommand=sb_out.set)
        self._txt_out.pack(side="left", fill="both", expand=True)
        sb_out.pack(side="right", fill="y")

        self._lbl_out_count = ttk.Label(right, text="0 dòng")
        self._lbl_out_count.pack(side="bottom", anchor="e", padx=4)

        # ── Status bar ──
        self._lbl_status = ttk.Label(main, text="Sẵn sàng", foreground="#888888")
        self._lbl_status.pack(fill="x", pady=(8, 0))

    # ──────────────────────── LOGIC ────────────────────────
    def _get_key_url(self) -> tuple[str, str] | None:
        key = self._ent_key.get().strip()
        url = self._ent_url.get().strip()
        if not key:
            messagebox.showwarning("Thiếu Key", "Nhập keyword vào ô Key.")
            return None
        if not url:
            messagebox.showwarning("Thiếu URL", "Nhập URL vào ô URL.")
            return None
        return key, url

    def _convert(self) -> None:
        """Key trong comment → <a href="url">key</a>"""
        pair = self._get_key_url()
        if not pair:
            return
        key, url = pair

        raw = self._txt_in.get("1.0", "end").rstrip("\n")
        if not raw.strip():
            messagebox.showinfo("Trống", "Paste comments vào ô Input.")
            return

        lines = raw.split("\n")
        anchor = f'<a href="{url}">{key}</a>'

        case_sensitive = self._case_var.get()
        first_only = self._first_only_var.get()
        count = 0
        results = []

        for line in lines:
            if not line.strip():
                results.append(line)
                continue

            if case_sensitive:
                if key in line:
                    if first_only:
                        new_line = line.replace(key, anchor, 1)
                    else:
                        new_line = line.replace(key, anchor)
                    count += 1
                else:
                    new_line = line
            else:
                # Case-insensitive replace giữ nguyên anchor tag dùng key gốc
                pattern = re.compile(re.escape(key), re.IGNORECASE)
                if pattern.search(line):
                    if first_only:
                        new_line = pattern.sub(anchor, line, count=1)
                    else:
                        new_line = pattern.sub(anchor, line)
                    count += 1
                else:
                    new_line = line

            results.append(new_line)

        self._txt_out.delete("1.0", "end")
        self._txt_out.insert("1.0", "\n".join(results))
        self._update_out_count()
        self._lbl_status.configure(
            text=f"✅ Đã chuyển đổi {count}/{len([l for l in lines if l.strip()])} comment chứa \"{key}\"",
            foreground="#27ae60"
        )

    def _reverse(self) -> None:
        """<a href="...">key</a> → key (bỏ HTML tag)"""
        raw = self._txt_in.get("1.0", "end").rstrip("\n")
        if not raw.strip():
            messagebox.showinfo("Trống", "Paste comments vào ô Input.")
            return

        # Regex: <a ...>text</a> → text
        pattern = re.compile(r'<a\s+[^>]*>(.*?)</a>', re.IGNORECASE)
        lines = raw.split("\n")
        count = 0
        results = []

        for line in lines:
            new_line, n = pattern.subn(r'\1', line)
            if n > 0:
                count += 1
            results.append(new_line)

        self._txt_out.delete("1.0", "end")
        self._txt_out.insert("1.0", "\n".join(results))
        self._update_out_count()
        self._lbl_status.configure(
            text=f"✅ Đã gỡ anchor tag khỏi {count} comment",
            foreground="#2980b9"
        )

    def _copy_output(self) -> None:
        text = self._txt_out.get("1.0", "end").strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._lbl_status.configure(text="📋 Đã copy output!", foreground="#8e44ad")

    def _clear_input(self) -> None:
        self._txt_in.delete("1.0", "end")
        self._update_in_count()

    def _clear_output(self) -> None:
        self._txt_out.delete("1.0", "end")
        self._update_out_count()

    def _output_to_input(self) -> None:
        """Chuyển output → input để chain xử lý"""
        text = self._txt_out.get("1.0", "end").strip()
        if not text:
            return
        self._txt_in.delete("1.0", "end")
        self._txt_in.insert("1.0", text)
        self._txt_out.delete("1.0", "end")
        self._update_in_count()
        self._update_out_count()
        self._lbl_status.configure(text="📥 Đã chuyển Output → Input", foreground="#e67e22")

    # ──────────────────────── Helpers ────────────────────────
    def _update_in_count(self, _event=None) -> None:
        self._txt_in.edit_modified(False)
        raw = self._txt_in.get("1.0", "end").strip()
        n = len([l for l in raw.split("\n") if l.strip()]) if raw else 0
        self._lbl_in_count.configure(text=f"{n} dòng")

    def _update_out_count(self) -> None:
        raw = self._txt_out.get("1.0", "end").strip()
        n = len([l for l in raw.split("\n") if l.strip()]) if raw else 0
        self._lbl_out_count.configure(text=f"{n} dòng")

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    root = tk.Tk()
    try:
        root.iconbitmap("icon.ico")
    except Exception:
        pass
    app = AnchorToolApp(root)
    app.run()


if __name__ == "__main__":
    main()