import sqlite3
import time
from urllib.parse import urlparse

class DBManager:
    def __init__(self, db_path="backlinks.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self):
        # Bảng chứa URL chờ chạy
        self.conn.execute('''CREATE TABLE IF NOT EXISTS queue
                             (id INTEGER PRIMARY KEY, url TEXT UNIQUE, domain TEXT, status TEXT DEFAULT 'PENDING')''')
        # Bảng chứa lịch sử Domain để check Cooldown
        self.conn.execute('''CREATE TABLE IF NOT EXISTS domain_stats
                             (domain TEXT PRIMARY KEY, last_hit REAL)''')
        # Bảng chứa kết quả (Thay cho success_data và CSV)
        self.conn.execute('''CREATE TABLE IF NOT EXISTS results
                             (id INTEGER PRIMARY KEY, keyword TEXT, url TEXT, comment_link TEXT, status TEXT, indexed INTEGER DEFAULT 0)''')
        try:
            self.conn.execute("ALTER TABLE results ADD COLUMN indexed INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass # Cột đã tồn tại
        self.conn.commit()

        # Reset các URL bị kẹt (Tắt tool đột ngột) về lại PENDING
        self.conn.execute("UPDATE queue SET status = 'PENDING' WHERE status = 'PROCESSING'")
        self.conn.commit()

    def add_urls(self, urls: list[str]):
        # Bóc tách domain và insert
        cursor = self.conn.cursor()
        for u in urls:
            try:
                dom = urlparse(u).netloc.lower()
                cursor.execute("INSERT OR IGNORE INTO queue (url, domain) VALUES (?, ?)", (u, dom))
            except: pass
        self.conn.commit()

    def get_next_job(self, cooldown_seconds=300):
        """Thuật toán hoàn hảo cho Domain Cooldown"""
        now = time.time()
        # Lấy 1 URL chưa chạy, mà domain của nó chưa bị hit trong vòng 'cooldown' giây
        cursor = self.conn.execute(f'''
            SELECT q.id, q.url, q.domain FROM queue q
            LEFT JOIN domain_stats d ON q.domain = d.domain
            WHERE q.status = 'PENDING' 
            AND (d.last_hit IS NULL OR (? - d.last_hit) > ?)
            LIMIT 1
        ''', (now, cooldown_seconds))
        row = cursor.fetchone()
        
        if row:
            job_id, url, domain = row
            # Đánh dấu đang chạy và cập nhật last_hit
            self.conn.execute("UPDATE queue SET status = 'PROCESSING' WHERE id = ?", (job_id,))
            self.conn.execute("INSERT OR REPLACE INTO domain_stats (domain, last_hit) VALUES (?, ?)", (domain, now))
            self.conn.commit()
            return job_id, url
        return None, None

    def add_result(self, keyword, url, comment_link, status):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO results (keyword, url, comment_link, status)
            VALUES (?, ?, ?, ?)
        ''', (keyword, url, comment_link, status))
        self.conn.commit()

    def get_success_count(self, keyword=None):
        cursor = self.conn.cursor()
        if keyword and keyword != "Tat ca":
            cursor.execute("SELECT COUNT(*) FROM results WHERE (status = 'SUCCESS' OR status = 'MODERATION') AND keyword = ?", (keyword,))
        else:
            cursor.execute("SELECT COUNT(*) FROM results WHERE status = 'SUCCESS' OR status = 'MODERATION'")
        return cursor.fetchone()[0]

    def get_fail_count(self, keyword=None):
        cursor = self.conn.cursor()
        if keyword and keyword != "Tat ca":
            cursor.execute("SELECT COUNT(*) FROM results WHERE status != 'SUCCESS' AND status != 'MODERATION' AND keyword = ?", (keyword,))
        else:
            cursor.execute("SELECT COUNT(*) FROM results WHERE status != 'SUCCESS' AND status != 'MODERATION'")
        return cursor.fetchone()[0]

    def get_success_results(self, keyword=None):
        cursor = self.conn.cursor()
        if keyword and keyword != "Tat ca":
            cursor.execute("SELECT keyword, url, comment_link, status FROM results WHERE (status = 'SUCCESS' OR status = 'MODERATION') AND keyword = ?", (keyword,))
        else:
            cursor.execute("SELECT keyword, url, comment_link, status FROM results WHERE status = 'SUCCESS' OR status = 'MODERATION'")
        return cursor.fetchall()
    
    def get_fail_results(self, keyword=None, limit=500):
        cursor = self.conn.cursor()
        if keyword and keyword != "Tat ca":
            cursor.execute("SELECT keyword, url, status, comment_link FROM results WHERE status != 'SUCCESS' AND status != 'MODERATION' AND keyword = ? LIMIT ?", (keyword, limit))
        else:
            cursor.execute("SELECT keyword, url, status, comment_link FROM results WHERE status != 'SUCCESS' AND status != 'MODERATION' LIMIT ?", (limit,))
        return cursor.fetchall()

    def clear_results(self):
        self.conn.execute("DELETE FROM results")
        self.conn.commit()
