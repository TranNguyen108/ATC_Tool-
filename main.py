"""
main.py — Entry point cho Comment Bot.

Cài đặt:
    pip install -r requirements.txt
    playwright install chromium

Chạy:
    python main.py
"""

import tkinter as tk
from gui_app import GuiApp


def main() -> None:
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
        app._save_state()
        try:
            root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    main()
