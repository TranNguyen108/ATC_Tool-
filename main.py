"""
main.py — Entry point cho Comment Bot.

Cài đặt:
    pip install -r requirements.txt
    playwright install chromium

Chạy:
    python main.py
"""

import logging
import tkinter as tk
from gui_app import GuiApp


def _setup_logging() -> None:
    """Cau hinh logger 'atc': DEBUG+ ra file atc_debug.log, WARNING+ ra console."""
    atc = logging.getLogger("atc")
    atc.setLevel(logging.DEBUG)
    atc.propagate = False  # khong lan len root logger

    fmt = logging.Formatter(
        "%(asctime)s [%(threadName)s] %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    # File handler — ghi tat ca DEBUG tro len (xem sau de debug)
    fh = logging.FileHandler("atc_debug.log", encoding="utf-8", mode="w")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    atc.addHandler(fh)

    # Console handler — chi WARNING tro len (giam noise)
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)
    atc.addHandler(ch)


def main() -> None:
    _setup_logging()
    root = tk.Tk()

    # Icon (bỏ qua nếu không có file icon)
    try:
        root.iconbitmap("icon.ico")
    except Exception:
        pass

    app = GuiApp(root)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    main()
