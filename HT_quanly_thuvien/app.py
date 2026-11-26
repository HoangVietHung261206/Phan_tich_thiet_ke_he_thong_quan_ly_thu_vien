import streamlit as st
import pandas as pd
import hashlib
import os
import shutil
from datetime import date, timedelta
from db import DB_PATH, get_conn, init_db, validate_backup_db
from services import (
    AuthService, ReaderService, CatalogService, LoanService,
    ReturnService, RenewService, ReserveService, PaymentService,
    ConfigService, NotificationService, AuditService
)

st.set_page_config(page_title="Hệ thống Quản lý Thư viện", layout="wide")
init_db()

if "user" not in st.session_state:
    st.session_state.user = None

def set_flash(level: str, msg: str):
    st.session_state.flash = (level, msg)

def show_flash():
    if "flash" in st.session_state and st.session_state.flash:
        lvl, msg = st.session_state.flash
        getattr(st, lvl)(msg)
        st.session_state.flash = None

def render_mock_qr_svg(ref_code: str, amount: int) -> str:
    h = hashlib.sha256(f"{ref_code}-{amount}".encode()).digest()
    cells = []
    for r in range(15):
        for c in range(15):
            in_finder = (r < 4 and c < 4) or (r < 4 and c >= 11) or (r >= 11 and c < 4)
            bit = (h[(r * 15 + c) % len(h)] >> ((r + c) % 8)) & 1
            if in_finder or bit:
                cells.append(f'<rect x="{c*10+10}" y="{r*10+10}" width="10" height="10" fill="#111"/>')
    return f'<svg width="170" height="170" xmlns="http://www.w3.org/2000/svg"><rect width="170" height="170" fill="#fff" stroke="#ccc"/>{"".join(cells)}</svg>'

def refresh_user():
    if st.session_state.user:
        conn = get_conn()
        u = conn.execute("SELECT * FROM users WHERE id = ?", (st.session_state.user["id"],)).fetchone()
        conn.close()
        st.session_state.user = dict(u) if u else None

if not st.session_state.user:
    st.title("📚 Hệ thống Quản lý Thư viện (Demo)")
    st.info("Tài khoản mẫu (MK: `123456`): `admin` | `thuthu` | `bandoc1` (60đ) | `bandoc2` (full 3 cuốn) | `bandoc3` (98đ) | `bandoc4` (101đ, 5 cuốn) | `bandoc5` (2 phiếu phạt)")
    with st.form("login_form"):
        username = st.text_input("Tên đăng nhập")
        password = st.text_input("Mật khẩu", type="password")
        if st.form_submit_button("Đăng nhập"):
            conn = get_conn()
            u = AuthService.login(conn, username, password)
            conn.close()
            if u:
                st.session_state.user = u
                st.rerun()
            else:
                st.error("Sai tên đăng nhập hoặc mật khẩu!")
    st.stop()

conn = get_conn()
ReserveService.process_expired_holds(conn)
NotificationService.generate_due_alerts(conn)
refresh_user()
user = st.session_state.user
max_pts = ConfigService.get_max_points(conn)

st.sidebar.title(f"👤 {user['full_name']}")
st.sidebar.caption(f"Mã: **{user['reader_code']}** | Vai trò: **{user['role'].upper()}**")
if st.sidebar.button("Đăng xuất"):
    st.session_state.user = None
    st.rerun()

if user["role"] == "reader":
    menu = st.sidebar.radio("Chức năng", ["Tra cứu & Đặt trước", "Hồ sơ, Gia hạn & Công nợ"])
elif user["role"] == "librarian":
    menu = st.sidebar.radio("Chức năng", ["Mượn sách tại quầy", "Trả sách & Lập phạt", "Gia hạn tại quầy", "Đối soát thanh toán QR", "Tra cứu & Đặt trước", "Quản lý bạn đọc"])
else:
    menu = st.sidebar.radio("Chức năng", ["Thống kê & Báo cáo", "Quản lý đầu sách & bản sách", "Cấu hình tham số & Bảng hạng", "Quản lý bạn đọc", "Quản lý tài khoản nội bộ", "Đối soát thanh toán QR", "Nhật ký hệ thống", "Sao lưu & Khôi phục DB"])

show_flash()

if menu == "Tra cứu & Đặt trước":
    st.header("🔍 Tra cứu danh mục & bản sách")
    kw = st.text_input("Tìm theo tên sách, tác giả, ISBN hoặc mã bản sách (VD: BK01-01)")
    df, copies_df = CatalogService.search_books(conn, kw)
    st.dataframe(df, width="stretch")
    st.subheader("📑 Chi tiết từng bản sách (FR03)")
    st.dataframe(copies_df, width="stretch")

    if user["role"] == "reader":
        st.subheader("📌 Đặt trước sách (chỉ khi hết bản sẵn có - BR1.5)")
        unavailable = df[df["san_co"] == 0]
        if unavailable.empty:
            st.info("Hiện tất cả đầu sách tìm thấy đều có bản sẵn có tại quầy.")
        else:
            book_opt = {f"{r['title']} ({r['isbn']})": int(r["id"]) for _, r in unavailable.iterrows()}
            chosen = st.selectbox("Chọn đầu sách đặt trước", list(book_opt.keys()))
            if st.button("Xác nhận đặt trước"):
                ok, msg = ReserveService.reserve_book(conn, user["id"], book_opt[chosen])
                set_flash("success" if ok else "error", msg)
                st.rerun()

elif menu == "Hồ sơ, Gia hạn & Công nợ":
    st.header("🪪 Hồ sơ bạn đọc")
    prof = ReaderService.get_profile_and_debt(conn, user["id"])
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Điểm uy tín", f"{user['reputation_points']} / {max_pts}")
    c2.metric("Hạng thành viên", prof["tier"])
    c3.metric("Hạn mức mượn", f"{prof['limit']} cuốn")
    c4.metric("Tổng nợ (FR04)", f"{prof['total_debt']:,} đ")
    c5.metric("Trạng thái", "🔴 Đang khóa" if user["is_locked"] else "🟢 Hoạt động")
    st.caption(f"Mã bạn đọc: **{user['reader_code']}** | Hạn thẻ: **{user['card_expiry']}** | Lý do khóa: {user['lock_reason'] or 'Không'}")

    st.subheader("🔔 Thông báo nghiệp vụ")
    notifs = pd.read_sql_query("SELECT id, type AS loai, message AS noi_dung, is_read AS da_doc, created_at AS thoi_gian FROM notifications WHERE reader_id=? ORDER BY id DESC LIMIT 15", conn, params=(user["id"],))
    if not notifs.empty:
        st.dataframe(notifs, width="stretch")
        if st.button("Đánh dấu tất cả đã đọc"):
            ok, msg = NotificationService.mark_all_read(conn, user["id"])
            set_flash("success" if ok else "error", msg)
            st.rerun()
    else:
        st.info("Chưa có thông báo mới.")

    with st.expander("📈 Lịch sử biến động điểm uy tín & hạng"):
        hist = pd.read_sql_query("SELECT created_at, old_points, new_points, old_tier, new_tier, reason FROM reputation_history WHERE reader_id=? ORDER BY id DESC", conn, params=(user["id"],))
        if not hist.empty:
            st.dataframe(hist, width="stretch")
        else:
            st.info("Chưa có lịch sử biến động điểm.")

    st.subheader("📖 Sách đang mượn & Gia hạn")
    loans = pd.read_sql_query("SELECT l.id, c.copy_code, b.title, l.borrow_date, l.due_date, l.renew_count FROM loans l JOIN book_copies c ON l.copy_id=c.id JOIN books b ON c.book_id=b.id WHERE l.reader_id=? AND l.status='Đang mượn'", conn, params=(user["id"],))
    st.dataframe(loans, width="stretch")
    if not loans.empty:
        loan_id = st.selectbox("Chọn mã phiếu mượn để gia hạn", loans["id"].tolist())
        if st.button("Gia hạn sách"):
            ok, msg = RenewService.renew_loan(conn, int(loan_id), user["id"], user["role"])
            set_flash("success" if ok else "error", msg)
            st.rerun()

    st.subheader("📌 Lượt đặt trước của tôi")
    my_res = pd.read_sql_query("SELECT r.id, b.title, r.status, r.created_at, r.hold_until FROM reservations r JOIN books b ON r.book_id=b.id WHERE r.reader_id=? AND r.status IN ('Đang chờ','Giữ chỗ')", conn, params=(user["id"],))
    st.dataframe(my_res, width="stretch")
    if not my_res.empty:
        res_id = st.selectbox("Chọn mã đặt trước muốn hủy", my_res["id"].tolist())
        if st.button("Hủy đặt trước"):
            ok, msg = ReserveService.cancel_reservation(conn, int(res_id), user["id"])
            set_flash("success" if ok else "error", msg)
            st.rerun()

    st.subheader("💳 Phiếu phạt & Thanh toán QR giả lập")
    fines = pd.read_sql_query("SELECT id, loan_id, reason, amount, status, created_at FROM fines WHERE reader_id=?", conn, params=(user["id"],))
    st.dataframe(fines, width="stretch")

    pending_pay = conn.execute("SELECT * FROM payments WHERE reader_id=? AND status='Khởi tạo' ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    unpaid = fines[fines["status"] == "Chưa thanh toán"]
    if not unpaid.empty and not pending_pay:
        selected_fines = st.multiselect("Chọn mã phiếu phạt cần thanh toán", unpaid["id"].tolist(), default=unpaid["id"].tolist())
        if selected_fines:
            total = int(unpaid[unpaid["id"].isin(selected_fines)]["amount"].sum())
            if st.button(f"Tạo yêu cầu QR ({total:,} VNĐ)"):
                ok, res = PaymentService.init_payment(conn, user["id"], selected_fines)
                if not ok:
                    set_flash("error", res)
                st.rerun()

    if pending_pay:
        col_qr, col_act = st.columns([1, 3])
        with col_qr:
            st.markdown(render_mock_qr_svg(pending_pay["ref_code"], pending_pay["total_amount"]), unsafe_allow_html=True)
        with col_act:
            st.warning(f"Mã tham chiếu: **{pending_pay['ref_code']}** | Số tiền: **{pending_pay['total_amount']:,} VNĐ**")
            c1, c2, c3, c4 = st.columns(4)
            if c1.button("✅ Thành công"):
                ok, msg = PaymentService.process_callback(conn, pending_pay["ref_code"], pending_pay["total_amount"], "Thành công", user["id"])
                set_flash("success" if ok else "error", msg)
                st.rerun()
            if c2.button("❌ Thất bại"):
                _, msg = PaymentService.process_callback(conn, pending_pay["ref_code"], pending_pay["total_amount"], "Thất bại", user["id"])
                set_flash("error", msg)
                st.rerun()
            if c3.button("⌛ Hết hạn QR"):
                _, msg = PaymentService.process_callback(conn, pending_pay["ref_code"], pending_pay["total_amount"], "Hết hạn", user["id"])
                set_flash("warning", msg)
                st.rerun()
            if c4.button("⚠️ Sai số tiền"):
                _, msg = PaymentService.process_callback(conn, pending_pay["ref_code"], pending_pay["total_amount"] - 1000, "Thành công", user["id"])
                set_flash("warning", msg)
                st.rerun()

    pays_df = PaymentService.get_receipts(conn, user["id"])
    if not pays_df.empty:
        st.caption("Lịch sử giao dịch thanh toán & Biên lai:")
        st.dataframe(pays_df, width="stretch")
        paid_refs = pays_df[pays_df["status"] == "Thành công"]["ref_code"].tolist()
        if paid_refs:
            dup_ref = st.selectbox("Chọn mã giao dịch thành công để test Callback trùng (TC12)", paid_refs)
            if st.button("🔁 Giả lập gửi trùng Callback"):
                _, msg = PaymentService.process_callback(conn, dup_ref, 0, "Thành công", user["id"])
                set_flash("info", msg)
                st.rerun()

elif menu == "Mượn sách tại quầy":
    st.header("📥 Lập giao dịch mượn sách")
    readers = conn.execute("SELECT id, reader_code, full_name, reputation_points FROM users WHERE role='reader'").fetchall()
    copies = conn.execute("SELECT c.id, c.copy_code, b.title, c.status FROM book_copies c JOIN books b ON c.book_id=b.id WHERE c.status IN ('Sẵn có','Giữ chỗ')").fetchall()
    r_map = {f"{r['reader_code']} - {r['full_name']} ({ConfigService.get_tier_and_limit(conn, r['reputation_points'])[0]})": r["id"] for r in readers}
    c_map = {f"{c['copy_code']} - {c['title']} [{c['status']}]": c["id"] for c in copies}
    if not c_map:
        st.warning("Không có bản sách nào sẵn có để mượn.")
    else:
        sel_r_id = r_map[st.selectbox("Chọn bạn đọc", list(r_map.keys()))]
        sel_c_id = c_map[st.selectbox("Chọn bản sách", list(c_map.keys()))]
        if st.button("Xác nhận cho mượn"):
            ok, msg = LoanService.borrow_book(conn, sel_r_id, sel_c_id, user["id"])
            set_flash("success" if ok else "error", msg)
            st.rerun()

elif menu == "Trả sách & Lập phạt":
    st.header("📤 Ghi nhận trả sách & Xử lý vi phạm")
    active_loans = pd.read_sql_query("SELECT l.id, u.reader_code, u.full_name, c.copy_code, b.title, l.borrow_date, l.due_date FROM loans l JOIN users u ON l.reader_id=u.id JOIN book_copies c ON l.copy_id=c.id JOIN books b ON c.book_id=b.id WHERE l.status='Đang mượn'", conn)
    st.dataframe(active_loans, width="stretch")
    if not active_loans.empty:
        lid = st.selectbox("Chọn mã phiếu mượn", active_loans["id"].tolist())
        condition = st.radio("Tình trạng sách khi trả", ["Nguyên vẹn", "Hỏng", "Mất"], horizontal=True)
        return_dt = st.date_input("Ngày trả thực tế (tối đa +365 ngày cho chế độ demo)", value=date.today())
        if st.button("Xác nhận trả sách"):
            ok, msg = ReturnService.return_book(conn, int(lid), condition, return_dt, user["id"])
            set_flash("success" if ok else "error", msg)
            st.rerun()

elif menu == "Gia hạn tại quầy":
    st.header("⏳ Hỗ trợ gia hạn sách tại quầy")
    active_loans = pd.read_sql_query("SELECT l.id, u.reader_code, u.full_name, c.copy_code, b.title, l.due_date, l.renew_count FROM loans l JOIN users u ON l.reader_id=u.id JOIN book_copies c ON l.copy_id=c.id JOIN books b ON c.book_id=b.id WHERE l.status='Đang mượn'", conn)
    st.dataframe(active_loans, width="stretch")
    if not active_loans.empty:
        lid = st.selectbox("Chọn mã phiếu mượn cần gia hạn", active_loans["id"].tolist())
        if st.button("Xác nhận gia hạn"):
            ok, msg = RenewService.renew_loan(conn, int(lid), user["id"], user["role"])
            set_flash("success" if ok else "error", msg)
            st.rerun()

elif menu == "Đối soát thanh toán QR":
    st.header("🧾 Đối soát giao dịch QR")
    rec_df = pd.read_sql_query("SELECT p.ref_code, u.reader_code, u.full_name, p.total_amount, p.status FROM payments p JOIN users u ON p.reader_id=u.id WHERE p.status='Cần đối soát'", conn)
    if rec_df.empty:
        st.info("Không có giao dịch nào cần đối soát.")
    else:
        st.dataframe(rec_df, width="stretch")
        ref = st.selectbox("Chọn mã giao dịch cần đối soát", rec_df["ref_code"].tolist())
        c1, c2 = st.columns(2)
        if c1.button("✅ Duyệt tất toán & Phát hành biên lai"):
            ok, msg = PaymentService.reconcile_payment(conn, ref, True, user["id"])
            set_flash("success" if ok else "error", msg)
            st.rerun()
        if c2.button("❌ Từ chối & Giữ công nợ"):
            ok, msg = PaymentService.reconcile_payment(conn, ref, False, user["id"])
            set_flash("warning" if ok else "error", msg)
            st.rerun()

elif menu == "Thống kê & Báo cáo":
    st.header("📊 Thống kê hoạt động thư viện")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tổng đầu sách", conn.execute("SELECT COUNT(*) FROM books").fetchone()[0])
    c2.metric("Đang cho mượn", conn.execute("SELECT COUNT(*) FROM loans WHERE status='Đang mượn'").fetchone()[0])
    c3.metric("Phiếu phạt chưa thu", conn.execute("SELECT COUNT(*) FROM fines WHERE status='Chưa thanh toán'").fetchone()[0])
    c4.metric("Tiền phạt đã thu", f"{conn.execute('SELECT COALESCE(SUM(total_amount),0) FROM payments WHERE status=\'Thành công\'').fetchone()[0]:,} đ")
    st.bar_chart(pd.read_sql_query("SELECT status, COUNT(*) as so_luong FROM book_copies GROUP BY status", conn).set_index("status"))

elif menu == "Quản lý bạn đọc":
    st.header("👥 Quản lý hồ sơ bạn đọc")
    readers_df = pd.read_sql_query("SELECT id, reader_code, username, full_name, reputation_points, is_locked, lock_reason, card_expiry FROM users WHERE role='reader'", conn)
    readers_df["tier"] = readers_df["reputation_points"].apply(lambda p: ConfigService.get_tier_and_limit(conn, int(p))[0])
    st.dataframe(readers_df, width="stretch")

    t1, t2, t3 = st.tabs(["➕ Thêm bạn đọc", "✏️ Sửa hồ sơ", "🔒 Khóa / Mở khóa"])
    with t1:
        with st.form("add_reader"):
            c1, c2, c3, c4 = st.columns(4)
            r_code, u_name, f_name = c1.text_input("Mã bạn đọc (VD: BD006)"), c2.text_input("Tên đăng nhập"), c3.text_input("Họ và tên")
            exp_dt = c4.date_input("Ngày hết hạn thẻ", value=date.today() + timedelta(days=365))
            if st.form_submit_button("Tạo tài khoản (MK: 123456)"):
                ok, msg = ReaderService.create_reader(conn, r_code, u_name, f_name, exp_dt, user["id"])
                set_flash("success" if ok else "error", msg)
                st.rerun()
    with t2:
        edit_id = st.selectbox("Chọn ID bạn đọc cần sửa", readers_df["id"].tolist(), key="edit_r")
        cur_r = readers_df[readers_df["id"] == edit_id].iloc[0]
        with st.form("edit_reader_form"):
            new_name = st.text_input("Họ và tên mới", value=cur_r["full_name"])
            new_exp = st.date_input("Hạn thẻ mới", value=date.fromisoformat(cur_r["card_expiry"]))
            if st.form_submit_button("Lưu thay đổi"):
                ok, msg = ReaderService.update_reader(conn, int(edit_id), new_name, new_exp, user["id"])
                set_flash("success" if ok else "error", msg)
                st.rerun()
    with t3:
        rid = st.selectbox("Chọn ID bạn đọc", readers_df["id"].tolist(), key="lock_r")
        reason = st.text_input("Lý do khóa (nếu khóa)", value="Vi phạm nội quy thư viện")
        col1, col2 = st.columns(2)
        if col1.button("Khóa tài khoản"):
            ok, msg = ReaderService.lock_account(conn, int(rid), reason, user["id"])
            set_flash("warning" if ok else "error", msg)
            st.rerun()
        if col2.button("Mở khóa tài khoản"):
            ok, msg = ReaderService.unlock_account(conn, int(rid), user["id"])
            set_flash("success" if ok else "error", msg)
            st.rerun()

elif menu == "Quản lý tài khoản nội bộ":
    st.header("🛡️ Quản lý tài khoản Thủ thư & Quản trị viên")
    st.dataframe(pd.read_sql_query("SELECT id, reader_code AS ma_nv, username, full_name, role FROM users WHERE role IN ('librarian','admin')", conn), width="stretch")
    with st.form("add_staff"):
        c1, c2, c3, c4 = st.columns(4)
        s_code, s_user, s_name, s_role = c1.text_input("Mã NV"), c2.text_input("Tên đăng nhập"), c3.text_input("Họ tên"), c4.selectbox("Vai trò", ["librarian", "admin"])
        if st.form_submit_button("Tạo tài khoản nội bộ (MK: 123456)"):
            ok, msg = ReaderService.create_staff(conn, s_code, s_user, s_name, s_role, user["id"])
            set_flash("success" if ok else "error", msg)
            st.rerun()

elif menu == "Quản lý đầu sách & bản sách":
    st.header("📚 Quản lý đầu sách & bản sách")
    t1, t2, t3 = st.tabs(["Thêm đầu sách mới", "Thêm bản sao", "Cập nhật trạng thái bản sách"])
    with t1:
        with st.form("add_book"):
            c1, c2, c3, c4 = st.columns(4)
            isbn, title, author, price = c1.text_input("ISBN"), c2.text_input("Tên sách"), c3.text_input("Tác giả"), c4.number_input("Giá bìa", min_value=10000, value=100000, step=10000)
            copy_code = st.text_input("Mã bản sách đầu tiên (VD: BK81-01)")
            if st.form_submit_button("Thêm đầu sách"):
                ok, msg = CatalogService.add_book(conn, isbn, title, author, int(price), copy_code, user["id"])
                set_flash("success" if ok else "error", msg)
                st.rerun()
    with t2:
        books_list = conn.execute("SELECT id, title, isbn FROM books").fetchall()
        b_map = {f"{b['title']} ({b['isbn']})": b["id"] for b in books_list}
        with st.form("add_copy"):
            sel_b = b_map[st.selectbox("Chọn đầu sách", list(b_map.keys()))]
            new_code = st.text_input("Mã bản sách mới (VD: BK01-03)")
            new_price = st.number_input("Giá bìa", min_value=10000, value=100000, step=10000)
            if st.form_submit_button("Thêm bản sao"):
                ok, msg = CatalogService.add_copy(conn, sel_b, new_code, int(new_price), user["id"])
                set_flash("success" if ok else "error", msg)
                st.rerun()
    with t3:
        copies_df = pd.read_sql_query("SELECT c.id, c.copy_code, b.title, c.cover_price, c.status FROM book_copies c JOIN books b ON c.book_id=b.id", conn)
        st.dataframe(copies_df, width="stretch")
        cid = st.selectbox("Chọn ID bản sách", copies_df["id"].tolist())
        new_st = st.selectbox("Trạng thái mới", ["Sẵn có", "Ngừng lưu thông", "Hỏng", "Mất"])
        if st.button("Cập nhật trạng thái"):
            ok, msg = CatalogService.update_copy_status(conn, int(cid), new_st, user["id"])
            set_flash("success" if ok else "error", msg)
            st.rerun()

elif menu == "Cấu hình tham số & Bảng hạng":
    st.header("⚙️ Cấu hình tham số & Bảng hạng thành viên")
    t1, t2 = st.tabs(["Tham số P1–P11", "Bảng hạng thành viên"])
    with t1:
        df_p = pd.read_sql_query("SELECT * FROM system_params", conn)
        st.dataframe(df_p, width="stretch")
        p_key = st.selectbox("Chọn tham số cần sửa", df_p["param_key"].tolist())
        new_val = st.number_input("Giá trị mới (số nguyên)", min_value=0, step=1)
        if st.button("Cập nhật tham số"):
            ok, msg = ConfigService.update_param(conn, p_key, int(new_val), user["id"])
            set_flash("success" if ok else "error", msg)
            st.rerun()
    with t2:
        df_t = pd.read_sql_query("SELECT * FROM membership_tiers ORDER BY min_points ASC", conn)
        st.dataframe(df_t, width="stretch")
        t_id = st.selectbox("Chọn ID hạng cần sửa", df_t["id"].tolist())
        row_t = df_t[df_t["id"] == t_id].iloc[0]
        c1, c2, c3 = st.columns(3)
        min_p = c1.number_input("Điểm tối thiểu", value=int(row_t["min_points"]), min_value=0)
        max_p = c2.number_input("Điểm tối đa", value=int(row_t["max_points"]), min_value=0)
        b_lim = c3.number_input("Hạn mức mượn (cuốn)", value=int(row_t["borrow_limit"]), min_value=1, max_value=50)
        if st.button("Cập nhật hạng"):
            ok, msg = ConfigService.update_tier(conn, int(t_id), int(min_p), int(max_p), int(b_lim), user["id"])
            set_flash("success" if ok else "error", msg)
            st.rerun()

elif menu == "Nhật ký hệ thống":
    st.header("📝 Nhật ký hệ thống (Audit Logs)")
    st.dataframe(AuditService.get_logs(conn), width="stretch")

elif menu == "Sao lưu & Khôi phục DB":
    st.header("💾 Sao lưu & Khôi phục Cơ sở dữ liệu (NFR07)")
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "rb") as f:
            st.download_button("⬇️ Tải bản sao lưu (.db)", f, file_name=f"library_backup_{date.today().isoformat()}.db")
    uploaded = st.file_uploader("⬆️ Chọn file .db để khôi phục", type=["db"])
    if uploaded and st.button("Khôi phục ngay"):
        tmp_path = "tmp_restore_check.db"
        with open(tmp_path, "wb") as f:
            f.write(uploaded.getbuffer())
        ok, msg = validate_backup_db(tmp_path)
        if not ok:
            os.remove(tmp_path)
            set_flash("error", f"Từ chối khôi phục: {msg}")
            st.rerun()
        else:
            conn.close()
            shutil.move(tmp_path, DB_PATH)
            set_flash("success", "Khôi phục cơ sở dữ liệu thành công!")
            st.rerun()

conn.close()