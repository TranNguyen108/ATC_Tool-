import sqlite3
import time
# (Cần cài: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib)
from googleapiclient.discovery import build
from google.oauth2 import service_account

BLOGGER_ID = "YOUR_BLOG_ID"
CREDENTIALS_FILE = "service_account.json" # Chứa key của Google Cloud

def post_to_blogger_and_ping():
    conn = sqlite3.connect('backlinks.db')
    # Lấy 50 link thành công chưa được Index
    cursor = conn.execute("SELECT id, comment_link, keyword FROM results WHERE status IN ('SUCCESS', 'SUCCESS (moderation)') AND indexed = 0 LIMIT 50")
    rows = cursor.fetchall()
    
    if not rows: return
    
    # 1. Tạo HTML cho Blogger
    html_content = "<h3>Danh sách Backlink mới cày hôm nay:</h3><ul>"
    ids_to_update = []
    for r in rows:
        html_content += f"<li><a href='{r[1]}'>{r[2]}</a></li>"
        ids_to_update.append(str(r[0]))
    html_content += "</ul>"

    try:
        # 2. Post lên Blogger
        creds = service_account.Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=['https://www.googleapis.com/auth/blogger'])
        blogger_service = build('blogger', 'v3', credentials=creds)
        
        post_body = {'title': f'Cập nhật đối tác {time.strftime("%d/%m/%Y")}', 'content': html_content}
        request = blogger_service.posts().insert(blogId=BLOGGER_ID, body=post_body)
        response = request.execute()
        blogger_url = response['url']
        print(f"Đã đăng lên Blogger: {blogger_url}")

        # 3. Ping Google Indexing API chính Blogger URL này
        indexing_creds = service_account.Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=['https://www.googleapis.com/auth/indexing'])
        import requests
        
        # NOTE: indexing API requires a valid access token. For google.oauth2, we might need to refresh it.
        # But creds.token might be None before refreshing. We'll simply use requests.post by manually fetching the token or letting auth library handle it.
        # Better: use authorized session wrapper. But adhering to user code logic:
        indexing_creds.refresh(requests.Request()) # Fetch initial token
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {indexing_creds.token}"}
        requests.post("https://indexing.googleapis.com/v3/urlNotifications:publish", json={"url": blogger_url, "type": "URL_UPDATED"}, headers=headers)
        
        # 4. Đánh dấu đã xử lý vào DB
        conn.execute(f"UPDATE results SET indexed = 1 WHERE id IN ({','.join(ids_to_update)})")
        conn.commit()

    except Exception as e:
        print(f"Lỗi Blogger/Indexing: {e}")

if __name__ == "__main__":
    while True:
        post_to_blogger_and_ping()
        time.sleep(1800) # 30 phút chạy 1 lần
