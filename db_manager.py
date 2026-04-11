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
        # ── Migrate: thêm cột mới nếu chưa có ──
        for col, definition in [
            ("indexed", "INTEGER DEFAULT 0"),
            ("group_name", "TEXT DEFAULT ''"),
            ("comment_used", "TEXT DEFAULT ''"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE results ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass  # Cột đã tồn tại
        # queue: thêm cột priority
        try:
            self.conn.execute("ALTER TABLE queue ADD COLUMN priority INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
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
        """Thuật toán hoàn hảo cho Domain Cooldown, ưu tiên priority cao"""
        now = time.time()
        # Lấy 1 URL chưa chạy, mà domain của nó chưa bị hit trong vòng 'cooldown' giây
        cursor = self.conn.execute('''
            SELECT q.id, q.url, q.domain FROM queue q
            LEFT JOIN domain_stats d ON q.domain = d.domain
            WHERE q.status = 'PENDING' 
            AND (d.last_hit IS NULL OR (? - d.last_hit) > ?)
            ORDER BY q.priority DESC
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

    def add_result(self, keyword, url, comment_link, status, group_name="", comment_used=""):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO results (keyword, url, comment_link, status, group_name, comment_used)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (keyword, url, comment_link, status, group_name, comment_used))
        self.conn.commit()

    def get_success_count(self, keyword=None, group_name=None):
        cursor = self.conn.cursor()
        conditions = ["(status = 'SUCCESS' OR status = 'MODERATION')"]
        params = []
        if keyword and keyword != "Tat ca":
            conditions.append("keyword = ?")
            params.append(keyword)
        if group_name and group_name != "Tat ca":
            conditions.append("group_name = ?")
            params.append(group_name)
        cursor.execute(f"SELECT COUNT(*) FROM results WHERE {' AND '.join(conditions)}", params)
        return cursor.fetchone()[0]

    def get_fail_count(self, keyword=None, group_name=None):
        cursor = self.conn.cursor()
        conditions = ["status != 'SUCCESS' AND status != 'MODERATION'"]
        params = []
        if keyword and keyword != "Tat ca":
            conditions.append("keyword = ?")
            params.append(keyword)
        if group_name and group_name != "Tat ca":
            conditions.append("group_name = ?")
            params.append(group_name)
        cursor.execute(f"SELECT COUNT(*) FROM results WHERE {' AND '.join(conditions)}", params)
        return cursor.fetchone()[0]

    def get_success_results(self, keyword=None, group_name=None):
        cursor = self.conn.cursor()
        conditions = ["(status = 'SUCCESS' OR status = 'MODERATION')"]
        params = []
        if keyword and keyword != "Tat ca":
            conditions.append("keyword = ?")
            params.append(keyword)
        if group_name and group_name != "Tat ca":
            conditions.append("group_name = ?")
            params.append(group_name)
        cursor.execute(f"SELECT keyword, url, comment_link, status FROM results WHERE {' AND '.join(conditions)}", params)
        return cursor.fetchall()
    
    def get_fail_results(self, keyword=None, limit=500, group_name=None):
        cursor = self.conn.cursor()
        conditions = ["status != 'SUCCESS' AND status != 'MODERATION'"]
        params = []
        if keyword and keyword != "Tat ca":
            conditions.append("keyword = ?")
            params.append(keyword)
        if group_name and group_name != "Tat ca":
            conditions.append("group_name = ?")
            params.append(group_name)
        params.append(limit)
        cursor.execute(f"SELECT keyword, url, status, comment_link FROM results WHERE {' AND '.join(conditions)} LIMIT ?", params)
        return cursor.fetchall()

    def clear_results(self):
        self.conn.execute("DELETE FROM results")
        self.conn.commit()

    # ── Multi-Group helpers ───────────────────────────────────────

    def get_group_names(self):
        """Trả về danh sách group_name duy nhất trong results."""
        cursor = self.conn.execute("SELECT DISTINCT group_name FROM results WHERE group_name != '' ORDER BY group_name")
        return [row[0] for row in cursor.fetchall()]

    def get_group_success_urls(self, group_name):
        """Trả về danh sách URL thành công của 1 group (dùng để ưu tiên cho group sau)."""
        cursor = self.conn.execute(
            "SELECT DISTINCT url FROM results WHERE group_name = ? AND (status = 'SUCCESS' OR status = 'MODERATION')",
            (group_name,))
        return [row[0] for row in cursor.fetchall()]

    def reset_queue_with_priority(self, priority_urls=None):
        """Reset toàn bộ queue về PENDING, đánh priority cho các URL ưu tiên."""
        self.conn.execute("UPDATE queue SET status = 'PENDING', priority = 0")
        if priority_urls:
            placeholders = ",".join("?" for _ in priority_urls)
            self.conn.execute(f"UPDATE queue SET priority = 1 WHERE url IN ({placeholders})", priority_urls)
        self.conn.commit()

    def clear_domain_stats(self):
        """Xóa toàn bộ domain cooldown (dùng khi chuyển group)."""
        self.conn.execute("DELETE FROM domain_stats")
        self.conn.commit()
