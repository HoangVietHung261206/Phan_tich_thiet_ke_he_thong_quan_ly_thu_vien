import sqlite3
import hashlib
import hmac
import os
from datetime import date, datetime, timedelta

DB_PATH = "library.db"
REQUIRED_TABLES = {
    "users", "membership_tiers", "books", "book_copies", "loans",
    "reservations", "fines", "payments", "payment_fines",
    "reputation_history", "notifications", "system_params", "audit_logs"
}

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def validate_backup_db(file_path: str):
    """Kiểm tra tính hợp lệ và đủ bảng của file SQLite trước khi khôi phục (NFR07)."""
    try:
        test_conn = sqlite3.connect(file_path)
        check = test_conn.execute("PRAGMA integrity_check;").fetchone()
        if not check or check[0] != "ok":
            test_conn.close()
            return False, "File cơ sở dữ liệu bị lỗi cấu trúc (integrity_check thất bại)."
        tables = {r[0] for r in test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        test_conn.close()
        missing = REQUIRED_TABLES - tables
        if missing:
            return False, f"File khôi phục thiếu các bảng bắt buộc: {', '.join(sorted(missing))}."
        return True, "Hợp lệ."
    except Exception as e:
        return False, f"File không phải định dạng SQLite hợp lệ: {e}"

def hash_password(password: str, salt: str = None):
    if not salt:
        salt = os.urandom(16).hex()
    pw_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return pw_hash, salt

def verify_password(password: str, pw_hash: str, salt: str) -> bool:
    check_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(check_hash, pw_hash)

def get_max_points(conn) -> int:
    row = conn.execute("SELECT MAX(max_points) FROM membership_tiers").fetchone()
    return int(row[0]) if row and row[0] is not None else 300

def get_tier_and_limit(conn, points: int):
    row = conn.execute(
        "SELECT tier_name, borrow_limit FROM membership_tiers WHERE ? BETWEEN min_points AND max_points LIMIT 1",
        (points,)
    ).fetchone()
    if row:
        return row["tier_name"], int(row["borrow_limit"])
    fallback = conn.execute("SELECT tier_name, borrow_limit FROM membership_tiers ORDER BY min_points ASC LIMIT 1").fetchone()
    return (fallback["tier_name"], int(fallback["borrow_limit"])) if fallback else ("Đồng", 3)

def get_params(conn):
    rows = conn.execute("SELECT param_key, param_value FROM system_params").fetchall()
    return {r["param_key"]: int(r["param_value"]) for r in rows}

def log_audit(conn, actor_id, action, target, old_val="", new_val=""):
    conn.execute(
        "INSERT INTO audit_logs (actor_id, action, target, old_value, new_value, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (actor_id, action, target, str(old_val), str(new_val), datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reader_code TEXT UNIQUE,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        full_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('reader','librarian','admin')),
        reputation_points INTEGER DEFAULT 30,
        is_locked INTEGER DEFAULT 0,
        lock_reason TEXT DEFAULT '',
        card_expiry TEXT DEFAULT '2028-12-31'
    );
    CREATE TABLE IF NOT EXISTS membership_tiers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tier_name TEXT UNIQUE NOT NULL,
        min_points INTEGER NOT NULL,
        max_points INTEGER NOT NULL,
        borrow_limit INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS books (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        isbn TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        author TEXT NOT NULL,
        category TEXT NOT NULL,
        publish_year INTEGER
    );
    CREATE TABLE IF NOT EXISTS book_copies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL REFERENCES books(id),
        copy_code TEXT UNIQUE NOT NULL,
        cover_price INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'Sẵn có'
            CHECK(status IN ('Sẵn có','Đang mượn','Giữ chỗ','Hỏng','Mất','Ngừng lưu thông'))
    );
    CREATE TABLE IF NOT EXISTS loans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reader_id INTEGER NOT NULL REFERENCES users(id),
        copy_id INTEGER NOT NULL REFERENCES book_copies(id),
        librarian_id INTEGER NOT NULL REFERENCES users(id),
        borrow_date TEXT NOT NULL,
        due_date TEXT NOT NULL,
        return_date TEXT,
        renew_count INTEGER DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'Đang mượn'
            CHECK(status IN ('Đang mượn','Đã trả','Đã trả - có vi phạm','Mất'))
    );
    CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reader_id INTEGER NOT NULL REFERENCES users(id),
        book_id INTEGER NOT NULL REFERENCES books(id),
        copy_id INTEGER REFERENCES book_copies(id),
        created_at TEXT NOT NULL,
        hold_until TEXT,
        status TEXT NOT NULL DEFAULT 'Đang chờ'
            CHECK(status IN ('Đang chờ','Giữ chỗ','Đã nhận','Đã hủy','Hết hạn'))
    );
    CREATE TABLE IF NOT EXISTS fines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        loan_id INTEGER NOT NULL REFERENCES loans(id),
        reader_id INTEGER NOT NULL REFERENCES users(id),
        reason TEXT NOT NULL CHECK(reason IN ('Trễ hạn','Hỏng','Mất')),
        amount INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'Chưa thanh toán'
            CHECK(status IN ('Chưa thanh toán','Đã thanh toán','Hủy do lập sai')),
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref_code TEXT UNIQUE NOT NULL,
        receipt_code TEXT UNIQUE,
        reader_id INTEGER NOT NULL REFERENCES users(id),
        total_amount INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'Khởi tạo'
            CHECK(status IN ('Khởi tạo','Thành công','Thất bại','Hết hạn','Cần đối soát')),
        paid_at TEXT
    );
    CREATE TABLE IF NOT EXISTS payment_fines (
        payment_id INTEGER NOT NULL REFERENCES payments(id),
        fine_id INTEGER NOT NULL REFERENCES fines(id),
        PRIMARY KEY (payment_id, fine_id)
    );
    CREATE TABLE IF NOT EXISTS reputation_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reader_id INTEGER NOT NULL REFERENCES users(id),
        loan_id INTEGER REFERENCES loans(id),
        old_points INTEGER NOT NULL,
        new_points INTEGER NOT NULL,
        old_tier TEXT NOT NULL,
        new_tier TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reader_id INTEGER NOT NULL REFERENCES users(id),
        type TEXT NOT NULL CHECK(type IN ('Sắp đến hạn','Quá hạn','Giữ chỗ','Phiếu phạt','Hệ thống')),
        ref_id INTEGER,
        message TEXT NOT NULL,
        is_read INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS system_params (
        param_key TEXT PRIMARY KEY,
        param_value TEXT NOT NULL,
        description TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_id INTEGER REFERENCES users(id),
        action TEXT NOT NULL,
        target TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        created_at TEXT NOT NULL
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_br13_active_loan
        ON loans(copy_id) WHERE status = 'Đang mượn';
    CREATE UNIQUE INDEX IF NOT EXISTS idx_br16_active_reservation
        ON reservations(reader_id, book_id) WHERE status IN ('Đang chờ', 'Giữ chỗ');
    CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_notif_ref
        ON notifications(reader_id, type, ref_id) WHERE ref_id IS NOT NULL;
    """)

    if cur.execute("SELECT COUNT(*) FROM membership_tiers").fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO membership_tiers (tier_name, min_points, max_points, borrow_limit) VALUES (?, ?, ?, ?)",
            [("Đồng", 0, 49, 3), ("Bạc", 50, 99, 4), ("Vàng", 100, 179, 6), ("Kim cương", 180, 300, 8)]
        )

    default_params = [
        ("P1", "14", "Thời hạn mượn (ngày)"),
        ("P2_MAX", "1", "Số lần gia hạn tối đa"),
        ("P2_DAYS", "7", "Số ngày cộng thêm khi gia hạn"),
        ("P3", "2000", "Phí trễ hạn (đồng/ngày/bản)"),
        ("P4", "50", "Phạt hỏng (% giá bìa)"),
        ("P5_FEE", "10000", "Phí xử lý khi mất sách (đồng)"),
        ("P6", "30", "Điểm khởi tạo"),
        ("P7", "5", "Điểm cộng trả đúng hạn nguyên vẹn"),
        ("P8_BASE", "5", "Điểm trừ cơ bản khi trễ"),
        ("P8_PER_DAY", "2", "Điểm trừ thêm mỗi ngày trễ"),
        ("P8_MAX", "30", "Tổng điểm trừ tối đa do trễ"),
        ("P9", "20", "Điểm trừ khi hỏng sách"),
        ("P10", "50", "Điểm trừ khi mất sách"),
        ("P11_DAYS", "2", "Số ngày giữ chỗ đặt trước"),
        ("P11_MAX", "3", "Số lượt đặt trước hiệu lực tối đa"),
    ]
    for k, v, d in default_params:
        cur.execute("INSERT OR IGNORE INTO system_params VALUES (?, ?, ?)", (k, v, d))

    if cur.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        users = [
            ("AD001", "admin", "123456", "Nguyễn Quản Trị", "admin", 300, 0, "", "2030-12-31"),
            ("TT001", "thuthu", "123456", "Trần Thủ Thư", "librarian", 300, 0, "", "2030-12-31"),
            ("BD001", "bandoc1", "123456", "Hoàng Việt Hùng", "reader", 60, 0, "", "2028-12-31"),
            ("BD002", "bandoc2", "123456", "Phạm Đình Khiết (Full hạn mức)", "reader", 30, 0, "", "2028-12-31"),
            ("BD003", "bandoc3", "123456", "Lê Văn An (98đ - Test TC10)", "reader", 98, 0, "", "2028-12-31"),
            ("BD004", "bandoc4", "123456", "Nguyễn Thị Bình (101đ, 5 cuốn - Test TC11)", "reader", 101, 0, "", "2028-12-31"),
            ("BD005", "bandoc5", "123456", "Trần Văn Cường (2 phiếu phạt - TC08)", "reader", 40, 1, "Có phiếu phạt chưa thanh toán", "2028-12-31"),
        ]
        for code, u, pw, name, role, pts, locked, reason, exp in users:
            h, s = hash_password(pw)
            cur.execute(
                "INSERT INTO users (reader_code, username, password_hash, salt, full_name, role, reputation_points, is_locked, lock_reason, card_expiry) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (code, u, h, s, name, role, pts, locked, reason, exp)
            )

        books = [
            ("978-604-01", "Phân tích và Thiết kế Hệ thống Thông tin", "Nguyễn Văn H", "CNTT", 2021),
            ("978-604-02", "Cơ sở dữ liệu SQL & NoSQL", "Trần Thị B", "CNTT", 2023),
            ("978-604-03", "Lập trình Python cho Khoa học Dữ liệu", "Lê Minh C", "AI/DS", 2024),
            ("978-604-04", "Nhập môn Học máy (Machine Learning)", "Vũ Đức D", "AI/DS", 2023),
            ("978-604-05", "Cấu trúc dữ liệu và Giải thuật với C++", "Phạm Quang E", "CNTT", 2022),
            ("978-604-06", "Clean Code - Mã nguồn sạch", "Robert C. Martin", "CNTT", 2020),
            ("978-604-07", "Software Requirements (3rd Edition)", "Karl Wiegers", "CNTT", 2013),
            ("978-604-08", "Quản trị Học căn bản", "Nguyễn Thị F", "Kinh tế", 2021),
            ("978-604-09", "Quản trị Logistics và Chuỗi cung ứng", "Trần Văn G", "Kinh tế", 2022),
            ("978-604-10", "Pháp luật Đại cương", "Bộ Giáo dục", "Luật", 2020),
            ("978-604-11", "Toán Rời Rạc ứng dụng trong Tin học", "Đỗ Văn H", "Toán học", 2019),
            ("978-604-12", "Đắc Nhân Tâm", "Dale Carnegie", "Kỹ năng", 2018),
            ("978-604-13", "Nhà Giả Kim", "Paulo Coelho", "Văn học", 2019),
            ("978-604-14", "Tư duy Nhanh và Chậm", "Daniel Kahneman", "Tâm lý", 2021),
            ("978-604-15", "Sapiens: Lược sử Loài người", "Yuval Noah Harari", "Lịch sử", 2022),
            ("978-604-16", "Deep Learning - Học sâu cơ bản", "Ian Goodfellow", "AI/DS", 2022),
            ("978-604-17", "Mạng Máy Tính và Internet", "James F. Kurose", "CNTT", 2021),
            ("978-604-18", "Hệ Điều Hành (Operating Systems)", "Abraham Silberschatz", "CNTT", 2020),
            ("978-604-19", "Thiết kế Giao diện UI/UX hiện đại", "Don Norman", "Thiết kế", 2023),
            ("978-604-20", "Kinh tế Chính trị Mác - Lênin", "Bộ Giáo dục", "Chính trị", 2021),
            ("978-604-21", "Xác suất Thống kê cho Kỹ sư", "Nguyễn Cao K", "Toán học", 2020),
            ("978-604-22", "Trực quan hóa Dữ liệu với Power BI", "Lê Hoàng M", "AI/DS", 2024),
            ("978-604-23", "Lập trình Hướng đối tượng OOP", "Trần Minh N", "CNTT", 2021),
            ("978-604-24", "An toàn và Bảo mật Thông tin", "William Stallings", "CNTT", 2022),
            ("978-604-25", "Kỹ năng Thuyết trình Chuyên nghiệp", "Carmine Gallo", "Kỹ năng", 2019),
            ("978-604-26", "Tuổi Trẻ Đáng Giá Bao Nhiêu", "Rosie Nguyễn", "Kỹ năng", 2018),
            ("978-604-27", "Cây Cam Ngọt Của Tôi", "José Mauro", "Văn học", 2020),
            ("978-604-28", "Số Đỏ", "Vũ Trọng Phụng", "Văn học", 2017),
            ("978-604-29", "Kinh tế học Vi mô", "N. Gregory Mankiw", "Kinh tế", 2021),
            ("978-604-30", "Khởi nghiệp Tinh gọn (The Lean Startup)", "Eric Ries", "Kinh tế", 2020),
            ("978-604-31", "Kiến trúc Máy tính", "David A. Patterson", "CNTT", 2021),
            ("978-604-32", "Kỹ nghệ Phần mềm (Software Engineering)", "Ian Sommerville", "CNTT", 2020),
            ("978-604-33", "Design Patterns - Các mẫu thiết kế", "Erich Gamma", "CNTT", 2019),
            ("978-604-34", "Lập trình Web với HTML, CSS và JavaScript", "Jon Duckett", "CNTT", 2022),
            ("978-604-35", "Thực hành Docker và Kubernetes", "Nigel Poulton", "CNTT", 2023),
            ("978-604-36", "Git và GitHub cho Lập trình viên", "Scott Chacon", "CNTT", 2021),
            ("978-604-37", "Kiểm thử Phần mềm thực chiến", "Rex Black", "CNTT", 2022),
            ("978-604-38", "Hệ thống Phân tán (Distributed Systems)", "Martin Kleppmann", "CNTT", 2023),
            ("978-604-39", "Xử lý Ngôn ngữ Tự nhiên (NLP)", "Dan Jurafsky", "AI/DS", 2023),
            ("978-604-40", "Thị giác Máy tính (Computer Vision)", "Richard Szeliski", "AI/DS", 2022),
            ("978-604-41", "Khai phá Dữ liệu (Data Mining)", "Jiawei Han", "AI/DS", 2021),
            ("978-604-42", "Học Tăng cường (Reinforcement Learning)", "Richard S. Sutton", "AI/DS", 2020),
            ("978-604-43", "Phân tích Dữ liệu với Pandas và NumPy", "Wes McKinney", "AI/DS", 2023),
            ("978-604-44", "Trí tuệ Nhân tạo: Cách tiếp cận hiện đại", "Stuart Russell", "AI/DS", 2022),
            ("978-604-45", "Kỹ thuật Dữ liệu (Data Engineering)", "Joe Reis", "AI/DS", 2024),
            ("978-604-46", "Đại số Tuyến tính ứng dụng", "Gilbert Strang", "Toán học", 2020),
            ("978-604-47", "Giải tích 1 và 2 cho Kỹ thuật", "James Stewart", "Toán học", 2019),
            ("978-604-48", "Lý thuyết Đồ thị và Ứng dụng", "Nguyễn Đức Nghĩa", "Toán học", 2021),
            ("978-604-49", "Tối ưu hóa Tuyến tính", "Trần Vũ Thiệu", "Toán học", 2018),
            ("978-604-50", "Địa lý Vận tải Việt Nam", "Nguyễn Văn K", "Giao thông", 2021),
            ("978-604-51", "Khai thác Cảng và Vận tải Biển", "Lê Văn L", "Giao thông", 2022),
            ("978-604-52", "Quản trị Chuỗi cung ứng Toàn cầu", "Sunil Chopra", "Kinh tế", 2023),
            ("978-604-53", "Kinh tế học Vĩ mô", "N. Gregory Mankiw", "Kinh tế", 2021),
            ("978-604-54", "Nguyên lý Kế toán", "Nguyễn Thị M", "Kinh tế", 2020),
            ("978-604-55", "Tài chính Doanh nghiệp căn bản", "Stephen Ross", "Kinh tế", 2022),
            ("978-604-56", "Marketing Căn bản", "Philip Kotler", "Kinh tế", 2021),
            ("978-604-57", "Quản trị Dự án Công nghệ Thông tin", "Kathy Schwalbe", "Kinh tế", 2023),
            ("978-604-58", "Quản trị Doanh nghiệp CNTT", "Phạm Văn P", "Kinh tế", 2022),
            ("978-604-59", "Thương mại Điện tử hiện đại", "Kenneth Laudon", "Kinh tế", 2023),
            ("978-604-60", "Luật Thương mại và Giao dịch Điện tử", "Bộ Tư pháp", "Luật", 2022),
            ("978-604-61", "Triết học Mác - Lênin", "Bộ Giáo dục", "Chính trị", 2021),
            ("978-604-62", "Chủ nghĩa Xã hội Khoa học", "Bộ Giáo dục", "Chính trị", 2021),
            ("978-604-63", "Tư tưởng Hồ Chí Minh", "Bộ Giáo dục", "Chính trị", 2021),
            ("978-604-64", "Lịch sử Đảng Cộng sản Việt Nam", "Bộ Giáo dục", "Chính trị", 2021),
            ("978-604-65", "Lịch sử Văn minh Thế giới", "Will Durant", "Lịch sử", 2019),
            ("978-604-66", "Đại Việt Sử Ký Toàn Thư (Trích tuyển)", "Ngô Sĩ Liên", "Lịch sử", 2018),
            ("978-604-67", "Thói Quen Nguyên Tử (Atomic Habits)", "James Clear", "Kỹ năng", 2020),
            ("978-604-68", "7 Thói Quen Hiệu Quả", "Stephen R. Covey", "Kỹ năng", 2019),
            ("978-604-69", "Làm Chủ Tư Duy Phản Biện", "Albert Rutherford", "Kỹ năng", 2022),
            ("978-604-70", "Quản lý Thời gian Hiệu quả", "Brian Tracy", "Kỹ năng", 2020),
            ("978-604-71", "Tâm lý học Đám đông", "Gustave Le Bon", "Tâm lý", 2019),
            ("978-604-72", "Trí tuệ Cảm xúc (EQ)", "Daniel Goleman", "Tâm lý", 2021),
            ("978-604-73", "Dế Mèn Phiêu Lưu Ký", "Tô Hoài", "Văn học", 2018),
            ("978-604-74", "Tắt Đèn", "Ngô Tất Tố", "Văn học", 2017),
            ("978-604-75", "Chí Phèo và Tuyển tập Truyện ngắn", "Nam Cao", "Văn học", 2018),
            ("978-604-76", "Mắt Biếc", "Nguyễn Nhật Ánh", "Văn học", 2019),
            ("978-604-77", "Cho Tôi Xin Một Vé Đi Tuổi Thơ", "Nguyễn Nhật Ánh", "Văn học", 2018),
            ("978-604-78", "Hoàng Tử Bé", "Antoine de Saint-Exupéry", "Văn học", 2020),
            ("978-604-79", "Ông Già Và Biển Cả", "Ernest Hemingway", "Văn học", 2019),
            ("978-604-80", "Lược Sử Thời Gian", "Stephen Hawking", "Khoa học", 2020),
        ]
        for idx, b in enumerate(books, start=1):
            cur.execute("INSERT INTO books (isbn, title, author, category, publish_year) VALUES (?, ?, ?, ?, ?)", b)
            cover_price = 70000 + (idx % 6) * 25000
            for c_num in (1, 2):
                cur.execute(
                    "INSERT INTO book_copies (book_id, copy_code, cover_price, status) VALUES (?, ?, ?, 'Sẵn có')",
                    (idx, f"BK{idx:02d}-{c_num:02d}", cover_price)
                )

        today = date.today()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 1) bandoc2 (id=4, Đồng) mượn đủ 3 cuốn (copy 1, 2, 3) -> sách #1 hết bản sẵn có
        for copy_id in (1, 2, 3):
            cur.execute("INSERT INTO loans (reader_id, copy_id, librarian_id, borrow_date, due_date, status) VALUES (4, ?, 2, ?, ?, 'Đang mượn')",
                        (copy_id, (today - timedelta(days=3)).isoformat(), (today + timedelta(days=11)).isoformat()))
            cur.execute("UPDATE book_copies SET status='Đang mượn' WHERE id=?", (copy_id,))

        # 2) bandoc1 (id=3) đặt trước sách #1
        cur.execute("INSERT INTO reservations (reader_id, book_id, created_at, status) VALUES (3, 1, ?, 'Đang chờ')", (now_str,))

        # 3) bandoc3 (id=5, 98đ) đang mượn copy 4 (test TC10)
        cur.execute("INSERT INTO loans (reader_id, copy_id, librarian_id, borrow_date, due_date, status) VALUES (5, 4, 2, ?, ?, 'Đang mượn')",
                    ((today - timedelta(days=2)).isoformat(), (today + timedelta(days=12)).isoformat()))
        cur.execute("UPDATE book_copies SET status='Đang mượn' WHERE id=4")

        # 4) bandoc4 (id=6, 101đ) đang mượn 5 cuốn (copy 6..10) để test TC11
        for copy_id in (6, 7, 8, 9, 10):
            cur.execute("INSERT INTO loans (reader_id, copy_id, librarian_id, borrow_date, due_date, status) VALUES (6, ?, 2, ?, ?, 'Đang mượn')",
                        (copy_id, (today - timedelta(days=4)).isoformat(), (today + timedelta(days=10)).isoformat()))
            cur.execute("UPDATE book_copies SET status='Đang mượn' WHERE id=?", (copy_id,))

        # 5) bandoc5 (id=7) có 2 phiếu phạt chưa thanh toán (test TC08/TC09)
        cur.execute("INSERT INTO loans (reader_id, copy_id, librarian_id, borrow_date, due_date, return_date, status) VALUES (7, 5, 2, ?, ?, ?, 'Đã trả - có vi phạm')",
                    ((today - timedelta(days=20)).isoformat(), (today - timedelta(days=6)).isoformat(), (today - timedelta(days=1)).isoformat()))
        loan_fine_id = cur.lastrowid
        cur.execute("INSERT INTO fines (loan_id, reader_id, reason, amount, status, created_at) VALUES (?, 7, 'Trễ hạn', 10000, 'Chưa thanh toán', ?)", (loan_fine_id, now_str))
        cur.execute("INSERT INTO fines (loan_id, reader_id, reason, amount, status, created_at) VALUES (?, 7, 'Hỏng', 50000, 'Chưa thanh toán', ?)", (loan_fine_id, now_str))

    conn.commit()
    conn.close()