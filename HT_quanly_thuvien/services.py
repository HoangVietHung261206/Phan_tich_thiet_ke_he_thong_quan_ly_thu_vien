import uuid
import sqlite3
import pandas as pd
from datetime import date, datetime, timedelta
from db import (
    verify_password, hash_password, get_tier_and_limit,
    get_max_points, get_params, log_audit
)

FINE_LOCK_TAG = "Có phiếu phạt chưa thanh toán"
VALID_CONDITIONS = ("Nguyên vẹn", "Hỏng", "Mất")
MAX_DEMO_FUTURE_DAYS = 365

def _append_lock_reason(existing: str, tag: str) -> str:
    parts = [p.strip() for p in (existing or "").split("|") if p.strip()]
    if tag not in parts:
        parts.append(tag)
    return " | ".join(parts)

def _remove_lock_reason(existing: str, tag: str) -> str:
    parts = [p.strip() for p in (existing or "").split("|") if p.strip() and p.strip() != tag]
    return " | ".join(parts)

class AuditService:
    @staticmethod
    def log_audit(conn, actor_id, action, target, old_val="", new_val=""):
        log_audit(conn, actor_id, action, target, old_val, new_val)

    @staticmethod
    def get_logs(conn):
        return pd.read_sql_query("SELECT * FROM audit_logs ORDER BY id DESC", conn)

class ConfigService:
    @staticmethod
    def get_params(conn):
        return get_params(conn)

    @staticmethod
    def get_tier_and_limit(conn, points: int):
        return get_tier_and_limit(conn, points)

    @staticmethod
    def get_max_points(conn):
        return get_max_points(conn)

    @staticmethod
    def update_param(conn, p_key: str, new_val: int, actor_id: int):
        try:
            if p_key == "P4" and not (0 <= new_val <= 100):
                return False, "Tham số P4 (% giá bìa) phải nằm trong khoảng 0–100."
            if p_key in ("P1", "P2_MAX", "P2_DAYS", "P11_DAYS", "P11_MAX") and new_val < 1:
                return False, f"Tham số {p_key} phải lớn hơn hoặc bằng 1."
            old_val = conn.execute("SELECT param_value FROM system_params WHERE param_key=?", (p_key,)).fetchone()[0]
            conn.execute("UPDATE system_params SET param_value=? WHERE param_key=?", (str(int(new_val)), p_key))
            AuditService.log_audit(conn, actor_id, "SUA_THAM_SO", p_key, old_val, str(int(new_val)))
            conn.commit()
            return True, f"Đã cập nhật {p_key}: {old_val} → {int(new_val)}."
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi cập nhật tham số: {e}"

    @staticmethod
    def update_tier(conn, tier_id: int, min_p: int, max_p: int, borrow_limit: int, actor_id: int):
        try:
            if min_p > max_p:
                return False, "Điểm tối thiểu không được lớn hơn điểm tối đa."
            tiers = [dict(r) for r in conn.execute("SELECT * FROM membership_tiers").fetchall()]
            target = None
            for t in tiers:
                if t["id"] == tier_id:
                    target = t
                    t["min_points"], t["max_points"], t["borrow_limit"] = min_p, max_p, borrow_limit
            tiers.sort(key=lambda x: x["min_points"])
            if tiers[0]["min_points"] != 0:
                return False, "Bảng hạng phải bắt đầu từ 0 điểm (FR15)."
            for i in range(len(tiers) - 1):
                if tiers[i]["max_points"] + 1 != tiers[i + 1]["min_points"]:
                    return False, f"Bảng hạng bị chồng lấn hoặc hở khoảng giữa '{tiers[i]['tier_name']}' và '{tiers[i+1]['tier_name']}' (FR15)."
            conn.execute(
                "UPDATE membership_tiers SET min_points=?, max_points=?, borrow_limit=? WHERE id=?",
                (min_p, max_p, borrow_limit, tier_id)
            )
            AuditService.log_audit(conn, actor_id, "SUA_BANG_HANG", target["tier_name"], "", f"{min_p}-{max_p}:{borrow_limit}")
            conn.commit()
            return True, f"Đã cập nhật hạng **{target['tier_name']}** ({min_p}–{max_p} điểm, hạn mức {borrow_limit} cuốn)."
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi cập nhật bảng hạng: {e}"

class AuthService:
    @staticmethod
    def login(conn, username: str, password: str):
        u = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if u and verify_password(password, u["password_hash"], u["salt"]):
            return dict(u)
        return None

class ReaderService:
    @staticmethod
    def create_reader(conn, r_code: str, u_name: str, f_name: str, exp_dt: date, actor_id: int):
        try:
            params = ConfigService.get_params(conn)
            h, s = hash_password("123456")
            conn.execute(
                "INSERT INTO users (reader_code, username, password_hash, salt, full_name, role, reputation_points, card_expiry) VALUES (?, ?, ?, ?, ?, 'reader', ?, ?)",
                (r_code, u_name, h, s, f_name, params["P6"], exp_dt.isoformat())
            )
            AuditService.log_audit(conn, actor_id, "TAO_BAN_DOC", r_code, "", u_name)
            conn.commit()
            return True, f"Đã tạo bạn đọc {r_code}!"
        except sqlite3.IntegrityError:
            conn.rollback()
            return False, "Mã bạn đọc hoặc tên đăng nhập đã tồn tại!"
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi tạo bạn đọc: {e}"

    @staticmethod
    def create_staff(conn, s_code: str, s_user: str, s_name: str, s_role: str, actor_id: int):
        try:
            if s_role not in ("librarian", "admin"):
                return False, "Vai trò nội bộ không hợp lệ."
            max_pts = ConfigService.get_max_points(conn)
            h, s = hash_password("123456")
            conn.execute(
                "INSERT INTO users (reader_code, username, password_hash, salt, full_name, role, reputation_points, card_expiry) VALUES (?, ?, ?, ?, ?, ?, ?, '2035-12-31')",
                (s_code, s_user, h, s, s_name, s_role, max_pts)
            )
            AuditService.log_audit(conn, actor_id, "TAO_NV", s_user, "", s_role)
            conn.commit()
            return True, f"Đã tạo tài khoản {s_role}: **{s_user}**!"
        except sqlite3.IntegrityError:
            conn.rollback()
            return False, "Mã nhân viên hoặc tên đăng nhập đã tồn tại!"
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi tạo tài khoản nội bộ: {e}"

    @staticmethod
    def update_reader(conn, reader_id: int, new_name: str, new_exp: date, actor_id: int):
        try:
            old = conn.execute("SELECT full_name FROM users WHERE id=?", (reader_id,)).fetchone()
            conn.execute("UPDATE users SET full_name=?, card_expiry=? WHERE id=?", (new_name, new_exp.isoformat(), reader_id))
            AuditService.log_audit(conn, actor_id, "SUA_HO_SO", f"User #{reader_id}", old["full_name"], new_name)
            conn.commit()
            return True, f"Đã cập nhật hồ sơ bạn đọc #{reader_id}!"
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi cập nhật hồ sơ: {e}"

    @staticmethod
    def get_profile_and_debt(conn, reader_id: int):
        u = dict(conn.execute("SELECT * FROM users WHERE id=?", (reader_id,)).fetchone())
        tier, limit = ConfigService.get_tier_and_limit(conn, u["reputation_points"])
        total_debt = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM fines WHERE reader_id=? AND status='Chưa thanh toán'", (reader_id,)
        ).fetchone()[0]
        return {"user": u, "tier": tier, "limit": limit, "total_debt": int(total_debt)}

    @staticmethod
    def lock_account(conn, reader_id: int, reason: str, actor_id: int):
        try:
            old_r = conn.execute("SELECT lock_reason FROM users WHERE id=?", (reader_id,)).fetchone()[0]
            merged = _append_lock_reason(old_r, reason)
            conn.execute("UPDATE users SET is_locked=1, lock_reason=? WHERE id=?", (merged, reader_id))
            AuditService.log_audit(conn, actor_id, "KHOA_TAI_KHOAN", f"User #{reader_id}", old_r, merged)
            conn.commit()
            return True, f"Đã khóa tài khoản #{reader_id}."
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi khóa tài khoản: {e}"

    @staticmethod
    def unlock_account(conn, reader_id: int, actor_id: int):
        try:
            unpaid_cnt = conn.execute("SELECT COUNT(*) FROM fines WHERE reader_id=? AND status='Chưa thanh toán'", (reader_id,)).fetchone()[0]
            if unpaid_cnt > 0:
                return False, "Không thể mở khóa: Bạn đọc vẫn còn phiếu phạt chưa thanh toán (BR2.7)."
            conn.execute("UPDATE users SET is_locked=0, lock_reason='' WHERE id=?", (reader_id,))
            AuditService.log_audit(conn, actor_id, "MO_KHOA", f"User #{reader_id}", "1", "0")
            conn.commit()
            return True, f"Đã mở khóa tài khoản #{reader_id}."
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi mở khóa tài khoản: {e}"

class CatalogService:
    @staticmethod
    def search_books(conn, keyword: str):
        like = f"%{keyword}%"
        books_df = pd.read_sql_query("""
            SELECT b.id, b.isbn, b.title, b.author, b.category,
                   COUNT(DISTINCT c.id) AS tong_ban,
                   SUM(CASE WHEN c.status = 'Sẵn có' THEN 1 ELSE 0 END) AS san_co
            FROM books b LEFT JOIN book_copies c ON b.id = c.book_id
            WHERE b.title LIKE ? OR b.author LIKE ? OR b.isbn LIKE ? OR c.copy_code LIKE ?
            GROUP BY b.id
        """, conn, params=(like, like, like, like))
        copies_df = pd.read_sql_query("""
            SELECT c.copy_code AS ma_ban, b.title AS ten_sach, b.isbn, c.cover_price AS gia_bia, c.status AS trang_thai
            FROM book_copies c JOIN books b ON c.book_id=b.id
            WHERE b.title LIKE ? OR b.author LIKE ? OR b.isbn LIKE ? OR c.copy_code LIKE ?
        """, conn, params=(like, like, like, like))
        return books_df, copies_df

    @staticmethod
    def add_book(conn, isbn: str, title: str, author: str, price: int, copy_code: str, actor_id: int):
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO books (isbn, title, author, category, publish_year) VALUES (?, ?, ?, 'Chung', 2026)", (isbn, title, author))
            cur.execute("INSERT INTO book_copies (book_id, copy_code, cover_price, status) VALUES (?, ?, ?, 'Sẵn có')", (cur.lastrowid, copy_code, int(price)))
            AuditService.log_audit(conn, actor_id, "THEM_DAU_SACH", isbn, "", copy_code)
            conn.commit()
            return True, "Đã thêm đầu sách & bản sách!"
        except sqlite3.IntegrityError:
            conn.rollback()
            return False, "ISBN hoặc Mã bản sách đã tồn tại!"
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi thêm sách: {e}"

    @staticmethod
    def add_copy(conn, book_id: int, copy_code: str, price: int, actor_id: int):
        try:
            conn.execute("INSERT INTO book_copies (book_id, copy_code, cover_price, status) VALUES (?, ?, ?, 'Sẵn có')", (book_id, copy_code, int(price)))
            AuditService.log_audit(conn, actor_id, "THEM_BAN_SAO", copy_code, "", f"Book #{book_id}")
            conn.commit()
            return True, f"Đã thêm bản sao {copy_code}!"
        except sqlite3.IntegrityError:
            conn.rollback()
            return False, "Mã bản sách đã tồn tại!"
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi thêm bản sao: {e}"

    @staticmethod
    def update_copy_status(conn, copy_id: int, new_status: str, actor_id: int):
        try:
            c = conn.execute("SELECT copy_code, status FROM book_copies WHERE id=?", (copy_id,)).fetchone()
            if not c:
                return False, "Không tìm thấy bản sách."
            if c["status"] in ("Đang mượn", "Giữ chỗ"):
                return False, f"Từ chối: Bản sách đang ở trạng thái '{c['status']}', không thể đổi thủ công (BR1.3/BR4.2)."
            conn.execute("UPDATE book_copies SET status=? WHERE id=?", (new_status, copy_id))
            AuditService.log_audit(conn, actor_id, "CAP_NHAT_BAN_SACH", c["copy_code"], c["status"], new_status)
            conn.commit()
            return True, f"Đã cập nhật bản sách {c['copy_code']}: {c['status']} → {new_status}."
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi cập nhật bản sách: {e}"

class NotificationService:
    @staticmethod
    def notify(conn, reader_id: int, n_type: str, message: str, ref_id: int = None):
        conn.execute(
            "INSERT OR IGNORE INTO notifications (reader_id, type, ref_id, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (reader_id, n_type, ref_id, message, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )

    @staticmethod
    def generate_due_alerts(conn):
        today = date.today()
        soon_str, today_str = (today + timedelta(days=2)).isoformat(), today.isoformat()
        loans = conn.execute("""
            SELECT l.id, l.reader_id, l.due_date, b.title
            FROM loans l JOIN book_copies c ON l.copy_id=c.id JOIN books b ON c.book_id=b.id
            WHERE l.status='Đang mượn'
        """).fetchall()
        for l in loans:
            if l["due_date"] < today_str:
                NotificationService.notify(conn, l["reader_id"], "Quá hạn", f"Sách '{l['title']}' đã quá hạn trả ({l['due_date']}).", ref_id=l["id"])
            elif l["due_date"] <= soon_str:
                NotificationService.notify(conn, l["reader_id"], "Sắp đến hạn", f"Sách '{l['title']}' sắp đến hạn trả vào ngày {l['due_date']}.", ref_id=l["id"])
        conn.commit()

    @staticmethod
    def mark_all_read(conn, reader_id: int):
        try:
            conn.execute("UPDATE notifications SET is_read=1 WHERE reader_id=?", (reader_id,))
            conn.commit()
            return True, "Đã đánh dấu tất cả thông báo là đã đọc."
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi cập nhật thông báo: {e}"

class FineService:
    @staticmethod
    def create_fines(conn, loan_id: int, reader_id: int, late_days: int, condition: str, cover_price: int, book_title: str = "", actor_id: int = None):
        """UC08: Kiểm tra giá bìa & tình trạng hợp lệ, lập từng khoản phạt riêng, khóa TK, gửi thông báo & ghi audit."""
        if condition not in VALID_CONDITIONS or cover_price is None or int(cover_price) <= 0:
            return False, "Thiếu giá bìa hoặc tình trạng sách không hợp lệ (Ngoại lệ UC08). Chuyển Thủ thư/Quản trị viên xử lý."
        params = ConfigService.get_params(conn)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fines_created = []
        if late_days > 0:
            amt = min(late_days * params["P3"], int(cover_price))
            conn.execute("INSERT INTO fines (loan_id, reader_id, reason, amount, status, created_at) VALUES (?, ?, 'Trễ hạn', ?, 'Chưa thanh toán', ?)", (loan_id, reader_id, amt, now_str))
            fines_created.append(("Trễ hạn", amt))
        if condition == "Hỏng":
            amt = int(int(cover_price) * params["P4"] / 100)
            conn.execute("INSERT INTO fines (loan_id, reader_id, reason, amount, status, created_at) VALUES (?, ?, 'Hỏng', ?, 'Chưa thanh toán', ?)", (loan_id, reader_id, amt, now_str))
            fines_created.append(("Hỏng", amt))
        elif condition == "Mất":
            amt = int(cover_price) + params["P5_FEE"]
            conn.execute("INSERT INTO fines (loan_id, reader_id, reason, amount, status, created_at) VALUES (?, ?, 'Mất', ?, 'Chưa thanh toán', ?)", (loan_id, reader_id, amt, now_str))
            fines_created.append(("Mất", amt))

        if fines_created:
            u_lock = conn.execute("SELECT lock_reason FROM users WHERE id=?", (reader_id,)).fetchone()
            new_reason = _append_lock_reason(u_lock["lock_reason"], FINE_LOCK_TAG)
            conn.execute("UPDATE users SET is_locked=1, lock_reason=? WHERE id=?", (new_reason, reader_id))
            NotificationService.notify(conn, reader_id, "Phiếu phạt", f"Phát sinh {len(fines_created)} phiếu phạt khi trả sách '{book_title}'. Tài khoản tạm khóa.", ref_id=loan_id)
            AuditService.log_audit(conn, actor_id, "LAP_PHIEU_PHAT", f"Loan #{loan_id}", "", str(fines_created))
        return True, fines_created

class ReputationService:
    @staticmethod
    def calc_delta(conn, late_days: int, condition: str) -> int:
        params = ConfigService.get_params(conn)
        delta = 0
        if late_days > 0:
            delta -= min(params["P8_BASE"] + params["P8_PER_DAY"] * late_days, params["P8_MAX"])
        if condition == "Hỏng":
            delta -= params["P9"]
        elif condition == "Mất":
            delta -= params["P10"]
        if late_days == 0 and condition == "Nguyên vẹn":
            delta = params["P7"]
        return delta

    @staticmethod
    def update_points(conn, reader_id: int, loan_id: int, delta: int, reason: str):
        max_pts = ConfigService.get_max_points(conn)
        old_pts = conn.execute("SELECT reputation_points FROM users WHERE id=?", (reader_id,)).fetchone()[0]
        old_tier, _ = ConfigService.get_tier_and_limit(conn, old_pts)
        new_pts = max(0, min(max_pts, old_pts + delta))
        new_tier, _ = ConfigService.get_tier_and_limit(conn, new_pts)
        conn.execute("UPDATE users SET reputation_points=? WHERE id=?", (new_pts, reader_id))
        conn.execute("""
            INSERT INTO reputation_history (reader_id, loan_id, old_points, new_points, old_tier, new_tier, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (reader_id, loan_id, old_pts, new_pts, old_tier, new_tier, reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        return old_pts, new_pts, old_tier, new_tier

class ReserveService:
    @staticmethod
    def assign_copy_to_next(conn, book_id: int, copy_id: int, title: str):
        params = ConfigService.get_params(conn)
        next_res = conn.execute("SELECT id, reader_id FROM reservations WHERE book_id=? AND status='Đang chờ' ORDER BY created_at ASC LIMIT 1", (book_id,)).fetchone()
        if next_res:
            hold_until = (date.today() + timedelta(days=params["P11_DAYS"])).isoformat()
            conn.execute("UPDATE reservations SET status='Giữ chỗ', copy_id=?, hold_until=? WHERE id=?", (copy_id, hold_until, next_res["id"]))
            conn.execute("UPDATE book_copies SET status='Giữ chỗ' WHERE id=?", (copy_id,))
            NotificationService.notify(conn, next_res["reader_id"], "Giữ chỗ", f"Sách '{title}' đã có sẵn và giữ chỗ cho bạn đến hết {hold_until}.", ref_id=next_res["id"])
        else:
            conn.execute("UPDATE book_copies SET status='Sẵn có' WHERE id=?", (copy_id,))

    @staticmethod
    def process_expired_holds(conn):
        today_str = date.today().isoformat()
        expired = conn.execute("""
            SELECT r.id, r.reader_id, r.book_id, r.copy_id, b.title
            FROM reservations r JOIN books b ON r.book_id=b.id
            WHERE r.status='Giữ chỗ' AND r.hold_until < ?
        """, (today_str,)).fetchall()
        for r in expired:
            conn.execute("UPDATE reservations SET status='Hết hạn' WHERE id=?", (r["id"],))
            NotificationService.notify(conn, r["reader_id"], "Hệ thống", f"Lượt giữ chỗ sách '{r['title']}' đã hết hạn và chuyển cho người tiếp theo.")
            if r["copy_id"]:
                ReserveService.assign_copy_to_next(conn, r["book_id"], r["copy_id"], r["title"])
        if expired:
            conn.commit()

    @staticmethod
    def reserve_book(conn, reader_id: int, book_id: int):
        try:
            params = ConfigService.get_params(conn)
            u = conn.execute("SELECT * FROM users WHERE id=?", (reader_id,)).fetchone()
            if not u:
                return False, "Không tìm thấy bạn đọc."
            if conn.execute("SELECT COUNT(*) FROM book_copies WHERE book_id=? AND status='Sẵn có'", (book_id,)).fetchone()[0] > 0:
                return False, "Đầu sách vẫn còn bản 'Sẵn có' tại quầy, không thể đặt trước (BR1.5)."
            active_cnt = conn.execute("SELECT COUNT(*) FROM reservations WHERE reader_id=? AND status IN ('Đang chờ','Giữ chỗ')", (reader_id,)).fetchone()[0]
            if date.today() > date.fromisoformat(u["card_expiry"]):
                return False, "Thẻ bạn đọc đã hết hạn (BR-05)."
            if u["is_locked"]:
                return False, "Tài khoản đang bị khóa, không thể đặt trước (BR2.5)."
            if active_cnt >= params["P11_MAX"]:
                return False, f"Đã đạt giới hạn tối đa {params['P11_MAX']} lượt đặt trước (P11)."
            conn.execute("INSERT INTO reservations (reader_id, book_id, created_at, status) VALUES (?, ?, ?, 'Đang chờ')", (reader_id, book_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            pos = conn.execute("SELECT COUNT(*) FROM reservations WHERE book_id=? AND status='Đang chờ'", (book_id,)).fetchone()[0]
            conn.commit()
            return True, f"Đặt trước thành công! Vị trí hàng đợi: **#{pos}**."
        except sqlite3.IntegrityError:
            conn.rollback()
            return False, "Bạn đã có lượt đặt trước đang hiệu lực cho đầu sách này (BR1.6)."
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi đặt trước: {e}"

    @staticmethod
    def cancel_reservation(conn, res_id: int, reader_id: int):
        try:
            row = conn.execute("SELECT r.id, r.book_id, r.copy_id, b.title FROM reservations r JOIN books b ON r.book_id=b.id WHERE r.id=? AND r.reader_id=? AND r.status IN ('Đang chờ','Giữ chỗ')", (res_id, reader_id)).fetchone()
            if not row:
                return False, "Không tìm thấy lượt đặt trước hợp lệ."
            conn.execute("UPDATE reservations SET status='Đã hủy' WHERE id=?", (res_id,))
            if row["copy_id"]:
                ReserveService.assign_copy_to_next(conn, row["book_id"], row["copy_id"], row["title"])
            conn.commit()
            return True, "Đã hủy lượt đặt trước!"
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi hủy đặt trước: {e}"

class LoanService:
    @staticmethod
    def borrow_book(conn, reader_id: int, copy_id: int, librarian_id: int):
        try:
            params = ConfigService.get_params(conn)
            r = conn.execute("SELECT * FROM users WHERE id=?", (reader_id,)).fetchone()
            c = conn.execute("SELECT c.*, b.title FROM book_copies c JOIN books b ON c.book_id=b.id WHERE c.id=?", (copy_id,)).fetchone()
            if not r or not c:
                return False, "Dữ liệu bạn đọc hoặc bản sách không tồn tại."
            tier, limit = ConfigService.get_tier_and_limit(conn, r["reputation_points"])
            borrowing = conn.execute("SELECT COUNT(*) FROM loans WHERE reader_id=? AND status='Đang mượn'", (reader_id,)).fetchone()[0]
            res_hold = conn.execute("SELECT id, reader_id FROM reservations WHERE copy_id=? AND status='Giữ chỗ'", (copy_id,)).fetchone() if c["status"] == "Giữ chỗ" else None

            if date.today() > date.fromisoformat(r["card_expiry"]):
                return False, "Từ chối: Thẻ bạn đọc đã hết hạn (BR-05)."
            if r["is_locked"]:
                return False, "Từ chối: Tài khoản bạn đọc đang bị khóa (BR2.5)."
            if borrowing >= limit:
                return False, f"Từ chối: Đã đạt hạn mức {limit} cuốn cho hạng {tier} (BR1.1)."
            if c["status"] not in ("Sẵn có", "Giữ chỗ"):
                return False, "Từ chối: Bản sách không ở trạng thái sẵn có (BR1.3)."
            if c["status"] == "Giữ chỗ" and (not res_hold or res_hold["reader_id"] != reader_id):
                return False, "Từ chối: Bản sách đang được giữ chỗ cho bạn đọc khác (BR1.3)."

            today = date.today()
            due = today + timedelta(days=params["P1"])
            conn.execute("INSERT INTO loans (reader_id, copy_id, librarian_id, borrow_date, due_date, status) VALUES (?, ?, ?, ?, ?, 'Đang mượn')", (reader_id, copy_id, librarian_id, today.isoformat(), due.isoformat()))
            conn.execute("UPDATE book_copies SET status='Đang mượn' WHERE id=?", (copy_id,))
            if res_hold:
                conn.execute("UPDATE reservations SET status='Đã nhận' WHERE id=?", (res_hold["id"],))
            NotificationService.notify(conn, reader_id, "Hệ thống", f"Mượn thành công sách '{c['title']}' ({c['copy_code']}). Hạn trả: {due.isoformat()}.")
            AuditService.log_audit(conn, librarian_id, "MUON_SACH", c["copy_code"], c["status"], f"Đang mượn bởi {r['username']}")
            conn.commit()
            return True, f"Cho mượn thành công sách **{c['copy_code']}**! Hạn trả: **{due.isoformat()}**."
        except sqlite3.IntegrityError:
            conn.rollback()
            return False, "Từ chối: Bản sách đang thuộc một phiếu mượn khác (BR1.3)."
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi lập phiếu mượn (đã rollback): {e}"

class RenewService:
    @staticmethod
    def renew_loan(conn, loan_id: int, actor_id: int, actor_role: str = "librarian"):
        try:
            params = ConfigService.get_params(conn)
            row = conn.execute("""
                SELECT l.id, l.reader_id, l.due_date, l.renew_count, c.book_id, b.title, u.is_locked, u.card_expiry
                FROM loans l JOIN book_copies c ON l.copy_id=c.id JOIN books b ON c.book_id=b.id JOIN users u ON l.reader_id=u.id
                WHERE l.id=? AND l.status='Đang mượn'
            """, (loan_id,)).fetchone()
            if not row:
                return False, "Không tìm thấy phiếu mượn đang hiệu lực."
            if actor_role == "reader" and int(actor_id) != int(row["reader_id"]):
                return False, "Từ chối: Bạn không có quyền gia hạn phiếu mượn của người khác (NFR02)."
            has_res = conn.execute("SELECT COUNT(*) FROM reservations WHERE book_id=? AND status='Đang chờ'", (row["book_id"],)).fetchone()[0]
            if date.today() > date.fromisoformat(row["card_expiry"]):
                return False, "Thẻ bạn đọc đã hết hạn (BR-05)."
            if row["is_locked"]:
                return False, "Tài khoản đang bị khóa (BR1.4)."
            if date.today() > date.fromisoformat(row["due_date"]):
                return False, "Sách đã quá hạn, không thể gia hạn (BR1.4)."
            if row["renew_count"] >= params["P2_MAX"]:
                return False, f"Đã gia hạn tối đa {params['P2_MAX']} lần (P2)."
            if has_res > 0:
                return False, "Đầu sách đang có người đặt trước, không thể gia hạn (BR1.4)."

            new_due = (date.fromisoformat(row["due_date"]) + timedelta(days=params["P2_DAYS"])).isoformat()
            conn.execute("UPDATE loans SET due_date=?, renew_count=renew_count+1 WHERE id=?", (new_due, loan_id))
            NotificationService.notify(conn, row["reader_id"], "Hệ thống", f"Gia hạn thành công sách '{row['title']}'. Hạn trả mới: {new_due}.")
            AuditService.log_audit(conn, actor_id, "GIA_HAN", f"Loan #{loan_id}", row["due_date"], new_due)
            conn.commit()
            return True, f"Gia hạn thành công! Hạn trả mới: **{new_due}**."
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi gia hạn (đã rollback): {e}"

class ReturnService:
    @staticmethod
    def return_book(conn, loan_id: int, condition: str, return_dt: date, librarian_id: int):
        try:
            if condition not in VALID_CONDITIONS:
                return False, "Tình trạng sách không hợp lệ (chỉ chấp nhận Nguyên vẹn, Hỏng, Mất)."
            row = conn.execute("""
                SELECT l.id, l.reader_id, l.copy_id, l.borrow_date, l.due_date, c.cover_price, c.book_id, b.title
                FROM loans l JOIN book_copies c ON l.copy_id=c.id JOIN books b ON c.book_id=b.id
                WHERE l.id=? AND l.status='Đang mượn'
            """, (loan_id,)).fetchone()
            if not row:
                return False, "Không tìm thấy phiếu mượn đang hiệu lực (A4)."
            if return_dt < date.fromisoformat(row["borrow_date"]):
                return False, f"Ngày trả ({return_dt.isoformat()}) không được nhỏ hơn ngày mượn ({row['borrow_date']})."
            if return_dt > date.today() + timedelta(days=MAX_DEMO_FUTURE_DAYS):
                return False, f"Ngày trả không được vượt quá {MAX_DEMO_FUTURE_DAYS} ngày so với hiện tại (giới hạn chế độ demo)."

            late_days = max(0, (return_dt - date.fromisoformat(row["due_date"])).days)
            has_violation = late_days > 0 or condition in ("Hỏng", "Mất")
            fines_created = []
            if has_violation:
                ok_fine, fine_res = FineService.create_fines(
                    conn, loan_id, row["reader_id"], late_days, condition, row["cover_price"], row["title"], librarian_id
                )
                if not ok_fine:
                    conn.rollback()
                    return False, fine_res
                fines_created = fine_res

            loan_status = "Mất" if condition == "Mất" else ("Đã trả - có vi phạm" if fines_created else "Đã trả")
            conn.execute("UPDATE loans SET return_date=?, status=? WHERE id=?", (return_dt.isoformat(), loan_status, loan_id))

            if condition == "Nguyên vẹn":
                ReserveService.assign_copy_to_next(conn, row["book_id"], row["copy_id"], row["title"])
            else:
                conn.execute("UPDATE book_copies SET status=? WHERE id=?", (condition, row["copy_id"]))

            delta = ReputationService.calc_delta(conn, late_days, condition)
            reason_str = ", ".join([r for r, _ in fines_created]) if fines_created else "Trả đúng hạn, nguyên vẹn"
            old_pts, new_pts, old_tier, new_tier = ReputationService.update_points(conn, row["reader_id"], loan_id, delta, reason_str)

            AuditService.log_audit(conn, librarian_id, "TRA_SACH", f"Loan #{loan_id}", f"Pts:{old_pts} ({old_tier})", f"Pts:{new_pts} ({new_tier}), Fines:{len(fines_created)}")
            conn.commit()
            return True, f"Trả sách thành công! Điểm uy tín: **{old_pts} → {new_pts} ({new_tier})**. Số phiếu phạt phát sinh: **{len(fines_created)}**."
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi trả sách (đã rollback - NFR06): {e}"

class PaymentService:
    @staticmethod
    def get_receipts(conn, reader_id: int):
        return pd.read_sql_query(
            "SELECT ref_code, receipt_code, total_amount, status, paid_at FROM payments WHERE reader_id=? ORDER BY id DESC",
            conn, params=(reader_id,)
        )

    @staticmethod
    def init_payment(conn, reader_id: int, fine_ids: list):
        try:
            if not fine_ids:
                return False, "Vui lòng chọn ít nhất một phiếu phạt."
            placeholders = ",".join("?" * len(fine_ids))
            fids = [int(x) for x in fine_ids]
            dup = conn.execute(f"SELECT pf.fine_id FROM payment_fines pf JOIN payments p ON pf.payment_id=p.id WHERE p.status='Khởi tạo' AND pf.fine_id IN ({placeholders})", fids).fetchone()
            if dup:
                return False, f"Phiếu phạt #{dup['fine_id']} đang nằm trong một giao dịch QR chờ xử lý."
            rows = conn.execute(f"SELECT id, amount FROM fines WHERE reader_id=? AND status='Chưa thanh toán' AND id IN ({placeholders})", [reader_id] + fids).fetchall()
            if not rows:
                return False, "Không có phiếu phạt hợp lệ để thanh toán."
            total = sum(int(r["amount"]) for r in rows)
            ref_code = f"QR-{uuid.uuid4().hex[:8].upper()}"
            cur = conn.cursor()
            cur.execute("INSERT INTO payments (ref_code, reader_id, total_amount, status) VALUES (?, ?, ?, 'Khởi tạo')", (ref_code, reader_id, total))
            pid = cur.lastrowid
            for r in rows:
                cur.execute("INSERT INTO payment_fines (payment_id, fine_id) VALUES (?, ?)", (pid, r["id"]))
            conn.commit()
            return True, ref_code
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi khởi tạo QR: {e}"

    @staticmethod
    def _settle_payment(conn, pay, actor_id: int):
        receipt = f"BL-{uuid.uuid4().hex[:8].upper()}"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE payments SET status='Thành công', receipt_code=?, paid_at=? WHERE id=?", (receipt, now_str, pay["id"]))
        conn.execute("UPDATE fines SET status='Đã thanh toán' WHERE id IN (SELECT fine_id FROM payment_fines WHERE payment_id=?) AND status='Chưa thanh toán'", (pay["id"],))
        rem = conn.execute("SELECT COUNT(*) FROM fines WHERE reader_id=? AND status='Chưa thanh toán'", (pay["reader_id"],)).fetchone()[0]
        unlocked_msg = ""
        if rem == 0:
            u = conn.execute("SELECT lock_reason FROM users WHERE id=?", (pay["reader_id"],)).fetchone()
            remaining_reason = _remove_lock_reason(u["lock_reason"], FINE_LOCK_TAG)
            if not remaining_reason:
                conn.execute("UPDATE users SET is_locked=0, lock_reason='' WHERE id=?", (pay["reader_id"],))
                unlocked_msg = " Tài khoản đã được mở khóa tự động!"
            else:
                conn.execute("UPDATE users SET lock_reason=? WHERE id=?", (remaining_reason, pay["reader_id"]))
                unlocked_msg = f" Đã hết nợ nhưng tài khoản vẫn khóa do: {remaining_reason}."
        NotificationService.notify(conn, pay["reader_id"], "Hệ thống", f"Thanh toán thành công {pay['total_amount']:,} VNĐ (Biên lai: {receipt}).")
        AuditService.log_audit(conn, actor_id, "THANH_TOAN_QR", pay["ref_code"], "Chưa thanh toán", f"Đã thanh toán ({receipt})")
        return receipt, unlocked_msg

    @staticmethod
    def process_callback(conn, ref_code: str, received_amount: int, result: str, actor_id: int):
        try:
            pay = conn.execute("SELECT * FROM payments WHERE ref_code=?", (ref_code,)).fetchone()
            if not pay:
                return False, "Không tìm thấy mã tham chiếu giao dịch."
            if pay["status"] == "Thành công":
                return True, f"Callback trùng (TC12): Giao dịch {ref_code} đã tất toán (Biên lai {pay['receipt_code']}). Không nhân đôi biên lai!"
            if pay["status"] != "Khởi tạo":
                return False, f"Giao dịch đang ở trạng thái '{pay['status']}', không thể xử lý lại."
            if result in ("Thất bại", "Hết hạn"):
                conn.execute("UPDATE payments SET status=? WHERE id=?", (result, pay["id"]))
                conn.commit()
                return False, f"Giao dịch {result}. Công nợ được giữ nguyên (BR2.6)."
            if int(received_amount) != int(pay["total_amount"]):
                conn.execute("UPDATE payments SET status='Cần đối soát' WHERE id=?", (pay["id"],))
                AuditService.log_audit(conn, actor_id, "DOI_SOAT_QR", ref_code, f"Expected:{pay['total_amount']}", f"Received:{received_amount}")
                conn.commit()
                return False, "Sai lệch số tiền! Giao dịch chuyển sang 'Cần đối soát', công nợ giữ nguyên."
            receipt, unlocked_msg = PaymentService._settle_payment(conn, pay, actor_id)
            conn.commit()
            return True, f"Thanh toán thành công! Mã biên lai: **{receipt}**.{unlocked_msg}"
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi callback (đã rollback): {e}"

    @staticmethod
    def reconcile_payment(conn, ref_code: str, approve: bool, actor_id: int):
        try:
            pay = conn.execute("SELECT * FROM payments WHERE ref_code=? AND status='Cần đối soát'", (ref_code,)).fetchone()
            if not pay:
                return False, "Không tìm thấy giao dịch đang chờ đối soát."
            if approve:
                receipt, unlocked_msg = PaymentService._settle_payment(conn, pay, actor_id)
                conn.commit()
                return True, f"Đã duyệt đối soát! Biên lai: **{receipt}**.{unlocked_msg}"
            conn.execute("UPDATE payments SET status='Thất bại' WHERE id=?", (pay["id"],))
            AuditService.log_audit(conn, actor_id, "TU_CHOI_DOI_SOAT", ref_code, "Cần đối soát", "Thất bại")
            conn.commit()
            return True, f"Đã từ chối giao dịch {ref_code}, giữ nguyên công nợ bạn đọc."
        except Exception as e:
            conn.rollback()
            return False, f"Lỗi đối soát (đã rollback): {e}"