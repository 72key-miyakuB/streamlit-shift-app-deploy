import streamlit as st
import pandas as pd
import datetime as dt
import calendar
from pathlib import Path
import jpholiday
import os
import shutil
import html
from streamlit_gsheets import GSheetsConnection

# =========================
# 設定値
# =========================
APP_TITLE = "The Sake Council Tokyo シフト管理"
ADMIN_PASSWORD = "TSCT2026"  # パスワードをここで定義

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# 曜日別の必要人数定数（エラー防止のためここで定義）
WEEKDAY_REQUIRED_STAFF = 5
WEEKEND_REQUIRED_STAFF = 6
REQUIRED_EMPLOYEES = 2

SHIFT_FILE = DATA_DIR / "shifts.csv"
REQUEST_FILE = DATA_DIR / "shift_requests.csv"
TIMECARD_FILE = DATA_DIR / "timecards.csv"
MESSAGE_FILE = DATA_DIR / "messages.csv"
STAFF_FILE = "staff_master.csv"

DEFAULT_OPEN_TIME = "17:00"
DEFAULT_CLOSE_TIME = "24:00"

# 列定義（これがないとロードエラーになります）
STAFF_COLUMNS_BASE = ["staff_id", "name", "role", "hourly_wage", "desired_shifts_per_week", "desired_monthly_income"]
STAFF_EXTRA_COLUMNS = ["position", "dayoff1", "dayoff2", "desired_shifts_per_month", "transport_daily"]
STAFF_COLUMNS = STAFF_COLUMNS_BASE + STAFF_EXTRA_COLUMNS

# =========================
# 【修正】3. GSheets連携
# =========================
try:
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception:
    conn = None

def load_csv(path: Path, columns: list) -> pd.DataFrame:
    sheet_name = path.stem
    try:
        # ttl=0 でキャッシュを無効化して常に最新を取得
        df = conn.read(worksheet=sheet_name, ttl=0)
        if df is None or df.empty:
            raise ValueError("Sheet is empty")
    except Exception:
        if path.exists():
            df = pd.read_csv(path)
        else:
            df = pd.DataFrame(columns=columns)
            
    # 指定したカラムが不足していたら追加
    for c in columns:
        if c not in df.columns:
            df[c] = None
    return df[columns]

def save_csv(df: pd.DataFrame, path: Path):
    sheet_name = path.stem
    df.to_csv(path, index=False, encoding="utf-8-sig")
    if conn:
        try:
            conn.update(worksheet=sheet_name, data=df)
            st.toast(f"クラウド({sheet_name})同期完了")
        except Exception as e:
            st.error(f"クラウド保存失敗: {e}")

# =========================================================
# 【修正】4. スタッフマスタの初期化
# =========================================================
def load_staff_master() -> pd.DataFrame:
    df = load_csv(Path(STAFF_FILE), STAFF_COLUMNS)
    if df.empty:
        STAFF_MASTER = [("S001", "宮首（店長）", "社員", 1500, 5, 280000, "店長", None, None, 22, 0)]
        df = pd.DataFrame(STAFF_MASTER, columns=STAFF_COLUMNS)
        save_csv(df, Path(STAFF_FILE))
    return df

STAFF_DF = load_staff_master()


def get_staff_name(staff_id: str) -> str:
    """
    スタッフIDから名前を取得する。
    マスタに存在しない場合は「ID（削除済み）」と表示してエラーを防ぐ。
    """
    rows = STAFF_DF[STAFF_DF["staff_id"] == staff_id]
    if len(rows) == 0:
        return f"{staff_id}（削除済み）"
    name = rows.iloc[0].get("name")
    return str(name) if pd.notna(name) else f"{staff_id}（削除済み）"


def get_active_staff_ids() -> set[str]:
    """現在有効なスタッフIDのセットを返す"""
    return set(STAFF_DF["staff_id"].astype(str).tolist())


def ensure_request_ids(requests_df: pd.DataFrame) -> pd.DataFrame:
    """
    シフト希望/NGデータに一意な request_id を振る。
    既存CSVに request_id がない or NaN の場合もここで埋める。
    """
    if len(requests_df) == 0:
        return requests_df

    # 数値に変換（NaN を許容）
    ids = pd.to_numeric(requests_df["request_id"], errors="coerce")
    max_id = ids.max()
    if pd.isna(max_id):
        max_id = 0

    next_id = int(max_id) + 1
    mask = ids.isna()
    num_new = mask.sum()

    if num_new > 0:
        new_ids = list(range(next_id, next_id + num_new))
        requests_df.loc[mask, "request_id"] = new_ids

    requests_df["request_id"] = requests_df["request_id"].astype(int)
    return requests_df

# =========================
# 共通ユーティリティ
# =========================

def get_staff_by_name(name: str) -> pd.Series:
    return STAFF_DF[STAFF_DF["name"] == name].iloc[0]


def get_staff_label(row) -> str:
    return f"{row['name']} ({row['role']})"


def date_range_for_month(year: int, month: int):
    """指定年月の全日付リスト"""
    first_day = dt.date(year, month, 1)
    _, last_day = calendar.monthrange(year, month)
    return [first_day + dt.timedelta(days=i) for i in range(last_day)]


def is_weekend(date_obj: dt.date) -> bool:
    # 金(4), 土(5), 日(6) を週末扱い
    return date_obj.weekday() >= 4


def format_time_str(time_obj: dt.time | None) -> str:
    if pd.isna(time_obj) or time_obj is None:
        return ""
    return time_obj.strftime("%H:%M")


# =========================
# シフト関連
# =========================
def build_month_calendar_html(
    year: int,
    month: int,
    day_contents: dict,
    shortage_info: dict,
    holiday_info: dict,
    requests_info: dict,
) -> str:
    """
    day_contents:  { date_obj: [ 'りえ 17:00〜24:00', ... ] }   # 確定シフト
    shortage_info: { date_obj: 不足人数(int) }
    holiday_info:  { date_obj: '成人の日', ... }
    requests_info: { date_obj: {'希望': [name...], 'NG': [name...] } }
    """
    cal = calendar.Calendar(firstweekday=6)  # 日曜始まり
    weeks = cal.monthdatescalendar(year, month)

    html = """
    <style>
    table.shift-cal {
        border-collapse: collapse;
        width: 100%;
        margin: 0 auto;
        table-layout: fixed;
        font-size: 1.1rem;
    }

    table.shift-cal th, table.shift-cal td {
        border: 1px solid #444;
        vertical-align: top;
        padding: 6px;
        word-wrap: break-word;
        color: #e0e0e0;
    }

    /* 曜日ヘッダー */
    table.shift-cal th {
        background-color: #222;
        text-align: center;
    }

    /* 平日デフォルト */
    table.shift-cal td {
        background-color: #111;
    }

    /* 土日(週末)デフォルト */
    table.shift-cal td.weekend {
        background-color: #1a1a1a !important;
    }

    /* 祝日：ほんのり紫がかったトーンで区別 */
    table.shift-cal td.holiday {
        background-color: #181423 !important;
    }

    /* 週末かつ祝日の場合は少し明るめ */
    table.shift-cal td.weekend.holiday {
        background-color: #221a33 !important;
    }

    /* 前月・翌月 */
    table.shift-cal td.outside {
        background-color: #2b2b2b !important;
        color: #888 !important;
    }

    /* 人数不足の日：背景色はそのまま、枠だけ赤く */
    table.shift-cal td.shortage {
        border-color: #ff6666 !important;
        box-shadow: 0 0 0 2px #ff6666 inset;
    }

    /* マスの高さ */
    table.shift-cal .cell-inner {
        min-height: 110px;
        display: block;
    }

    /* 日付番号 */
    table.shift-cal .day-number {
        font-weight: bold;
        margin-bottom: 4px;
        display: block;
        color: #fff;
    }

    /* 人数不足ラベル（残り枠） */
    table.shift-cal .shortage-label {
        display: block;
        font-size: 0.7rem;
        color: #ff9090;
        margin-bottom: 2px;
    }

    /* 祝日ラベル */
    table.shift-cal .holiday-label {
        display: block;
        font-size: 0.7rem;
        color: #ffdd88;
        margin-bottom: 2px;
    }

    /* 希望シフトラベル（青系） */
    table.shift-cal .request-hope {
        display: block;
        font-size: 0.75rem;
        color: #70b7ff;
        margin-bottom: 1px;
    }

    /* NGラベル（赤系） */
    table.shift-cal .request-ng {
        display: block;
        font-size: 0.75rem;
        color: #ff8080;
        margin-bottom: 1px;
    }

    /* 確定シフトのテキスト（少し小さめ） */
    table.shift-cal .shift-text {
        display: block;
        font-size: 0.8rem;
    }

    /* 1人分の行 共通スタイル */
    table.shift-cal .cal-line {
        display: block;
        margin-bottom: 2px;
        padding: 1px 2px;
        border-radius: 3px;
        line-height: 1.2;
    }

    /* 調理場担当 */
    table.shift-cal .pos-kitchen {
        background-color: rgba(255, 140, 0, 0.25);
    }

    /* ホール担当 */
    table.shift-cal .pos-hall {
        background-color: rgba(30, 144, 255, 0.25);
    }

    /* オールラウンド */
    table.shift-cal .pos-allround {
        background-color: rgba(50, 205, 50, 0.25);
    }

    /* ログイン中スタッフを強調 */
    table.shift-cal .self-staff {
        color: #ffd700;
        font-weight: bold;
    }

    </style>
    <table class="shift-cal">
    <tr>
        <th>日</th><th>月</th><th>火</th><th>水</th><th>木</th><th>金</th><th>土</th>
    </tr>
    """
    for week in weeks:
        html += "<tr>"
        for day in week:
            classes = []
            if day.month != month:
                classes.append("outside")

            # 週末
            if day.weekday() >= 5:
                classes.append("weekend")

            # 祝日
            holiday_name = holiday_info.get(day)
            is_holiday = holiday_name is not None
            if is_holiday:
                classes.append("holiday")

            # 人数不足
            shortage_count = shortage_info.get(day, 0)
            is_shortage = (shortage_count > 0) and (day.month == month)
            if is_shortage:
                classes.append("shortage")

            # 希望/NG情報
            req = requests_info.get(day, {})
            hope_list = req.get("希望", [])
            ng_list = req.get("NG", [])

            # 確定シフト
            key = day
            contents = day_contents.get(key, [])
            # contents はすでに HTML (<div class="cal-line ...">...</div>) の想定
            shift_html = "".join(contents)

            class_attr = f' class="{" ".join(classes)}"' if classes else ""
            html += f'<td{class_attr}>'
            html += '<div class="cell-inner">'
            html += f'<span class="day-number">{day.day}</span>'

            # 祝日ラベル
            if is_holiday:
                html += f'<span class="holiday-label">{holiday_name}</span>'

            # 残り枠ラベル
            if is_shortage:
                html += f'<span class="shortage-label">残り{shortage_count}枠</span>'

            # 希望シフト（青）
            if hope_list:
                names = ", ".join(hope_list)
                html += f"<span class='request-hope'>希望: {names}</span>"

            # NG（赤）
            if ng_list:
                names = ", ".join(ng_list)
                html += f"<span class='request-ng'>NG: {names}</span>"

            # 確定シフト
            if shift_html:
                html += shift_html

            html += '</div>'
            html += "</td>"
        html += "</tr>"
    html += "</table>"

    return html


def render_month_calendar_with_shifts(
    year: int,
    month: int,
    shifts_df: pd.DataFrame,
    title: str = "",
    current_staff_id: str | None = None,
):
    """
    year, month と シフトDataFrame から、マス目カレンダーHTMLを描画する簡易版。
    shifts_df: columns = ["date", "staff_id", "start_time", "end_time", "source"] を想定
    """

    # --- 日ごとの HTML 行を準備 ---
    day_to_lines: dict[int, list[str]] = {}

    for _, row in shifts_df.iterrows():
        try:
            d = dt.datetime.strptime(str(row["date"]), "%Y-%m-%d").date()
        except Exception:
            continue

        day = d.day
        sid = str(row["staff_id"])

        staff_row = STAFF_DF[STAFF_DF["staff_id"] == sid]
        if not staff_row.empty:
            s = staff_row.iloc[0]
            name = str(s.get("name", sid))
            position = str(s.get("position") or "")
        else:
            name = sid
            position = ""

        start = str(row.get("start_time") or "")
        end = str(row.get("end_time") or "")
        time_part = f"{start}〜{end}" if (start or end) else ""

        # --- ポジション別の背景色 ---
        bg_color = ""
        if position == "調理場担当":
            bg_color = "background-color: rgba(255, 140, 0, 0.25);"   # オレンジ系
        elif position == "ホール担当":
            bg_color = "background-color: rgba(30, 144, 255, 0.25);"  # 青系
        elif position == "オールラウンド":
            bg_color = "background-color: rgba(50, 205, 50, 0.25);"   # 緑系
        elif position == "店長" or position == "料理長" or position == "社員":
            bg_color = "background-color: rgba(255, 255, 255, 0.12);" # 社員うっすら

        # --- ログイン中スタッフの強調（文字色＋太字） ---
        highlight = ""
        if current_staff_id is not None and sid == str(current_staff_id):
            highlight = "color: #ffd700; font-weight: 700;"  # ゴールド

        style = (bg_color + " " + highlight).strip()

        line_html = f'<div class="cal-line" style="{style}">{html.escape(name)} {html.escape(time_part)}</div>'

        day_to_lines.setdefault(day, []).append(line_html)

    # --- カレンダーの枠組み ---
    first_day = dt.date(year, month, 1)
    start_weekday = first_day.weekday()  # 月曜=0, 日曜=6
    _, num_days = calendar.monthrange(year, month)

    html_code = """
    <style>
    .cal-wrapper {
        width: 100%;
        overflow-x: auto;
    }
    .cal-table {
        border-collapse: collapse;
        width: 100%;
        table-layout: fixed;
    }
    .cal-table th, .cal-table td {
        border: 1px solid #555;
        vertical-align: top;
        padding: 4px;
        font-size: 11px;
    }
    .cal-table th {
        text-align: center;
        background-color: #222;
    }
    .cal-daynum {
        font-weight: bold;
        margin-bottom: 2px;
    }
    .cal-cell {
        height: 110px;
    }
    .cal-line {
        margin-bottom: 2px;
        padding: 1px 2px;
        border-radius: 2px;
        overflow: hidden;
        white-space: nowrap;
        text-overflow: ellipsis;
    }
    </style>
    <div class="cal-wrapper">
      <table class="cal-table">
        <tr>
          <th>日</th><th>月</th><th>火</th><th>水</th><th>木</th><th>金</th><th>土</th>
        </tr>
    """

    # 日曜スタートに合わせる
    first_col = (start_weekday + 1) % 7

    html_code += "<tr>"


def get_default_year_month_for_ui() -> tuple[int, int]:
    """
    UIの初期表示用に「年・月」を返す。
    - 月前半（1〜19日）：当月
    - 月後半（20日〜）：翌月
    """
    today = dt.date.today()

    # しきい値（ここを変えれば「15日以降なら…」などに調整可能）
    THRESHOLD_DAY = 9

    if today.day >= THRESHOLD_DAY:
        # 翌月に進める
        if today.month == 12:
            default_year = today.year + 1
            default_month = 1
        else:
            default_year = today.year
            default_month = today.month + 1
    else:
        # 当月のまま
        default_year = today.year
        default_month = today.month

    return default_year, default_month


def page_shift_calendar(current_staff):
    st.header("📅 シフト確認カレンダー")

# --- 【修正箇所】必要なカラムをすべて指定して読み込む ---
    # シフト本体（給与計算済みのデータも含むためカラムを追加）
    shift_cols = ["date", "staff_id", "start_time", "end_time", "source", "hours", "late_hours", "pay"]
    shifts_df = load_csv(SHIFT_FILE, shift_cols)

    # シフト希望/NGデータ（一意なIDを含めて読み込む）
    req_cols = ["request_id", "date", "staff_id", "request_type", "start_time", "end_time", "note"]
    requests_df = load_csv(REQUEST_FILE, req_cols)
    # --------------------------------------------------

    # 月選択
    today = dt.date.today()
    EDIT_LOCK_DAYS = 7
    edit_lock_until = today + dt.timedelta(days=EDIT_LOCK_DAYS)

    # ✅ 月後半になったら翌月を初期表示にする
    default_year, default_month = get_default_year_month_for_ui()

    col1, col2 = st.columns(2)
    with col1:
        year = st.number_input(
            "年",
            min_value=2024,
            max_value=2100,
            value=default_year,
        )
    with col2:
        month = st.number_input(
            "月",
            min_value=1,
            max_value=12,
            value=default_month,
        )

    year = int(year)
    month = int(month)

    # 選択ユーザーの役割
    is_employee = current_staff["role"] == "社員"
    
    # 対象月のシフトだけに絞る
    month_prefix = f"{year:04d}-{month:02d}-"
    month_shifts = shifts_df[shifts_df["date"].str.startswith(month_prefix)]
    # 対象月の希望/NGだけに絞る
    month_requests = requests_df[requests_df["date"].str.startswith(month_prefix)]

    # 社員は全員分、バイトは自分の分だけ
    if not is_employee:
        month_requests = month_requests[month_requests["staff_id"] == current_staff["staff_id"]]

    # ----- 一覧テーブル用データ & 不足人数 & 祝日情報 -----
    target_dates = date_range_for_month(year, month)
    rows = []
    shortage_info: dict[dt.date, int] = {}
    holiday_info: dict[dt.date, str] = {}

    # 在籍中スタッフだけを人数カウントに使う
    active_ids = get_active_staff_ids()

    for d in target_dates:
        day_str = d.strftime("%Y-%m-%d")
        day_of_week = "日月火水木金土"[d.weekday()]
        is_weekend_flag = is_weekend(d)
        required_staff = WEEKEND_REQUIRED_STAFF if is_weekend_flag else WEEKDAY_REQUIRED_STAFF

        # 祝日判定
        hname = get_jp_holiday_name(d)
        if hname:
            holiday_info[d] = hname

        day_shifts = month_shifts[month_shifts["date"] == day_str]

        # 🔴 削除済みスタッフは人数カウントから除外
        active_day_shifts = day_shifts[day_shifts["staff_id"].astype(str).isin(active_ids)]
        current_count = len(active_day_shifts)

        # 残り枠（足りていれば0、オーバーでも0にしておく）
        remaining_slots = max(required_staff - current_count, 0)

        # 人数不足なら記録
        if current_count < required_staff:
            shortage_info[d] = required_staff - current_count

        # 一覧表示には削除済みも含めて表示する（名前は get_staff_name で安全に）
        shift_summaries = []
        for _, row in day_shifts.iterrows():
            name = get_staff_name(str(row["staff_id"]))
            start = row["start_time"] or ""
            end = row["end_time"] or ""
            time_part = f" {start}〜{end}" if start or end else ""
            shift_summaries.append(f"{name}{time_part}")

        rows.append(
            {
                "日付": day_str,
                "曜日": day_of_week,
                "区分": "週末" if is_weekend_flag else "平日",
                "必要人数": required_staff,
                "確定シフト人数": current_count,
                "残り枠": remaining_slots,
                "シフト一覧": "\n".join(shift_summaries),
            }
        )

# 一覧表示のデータフレームを表示する際、一意のキーを持たせる
    st.subheader("一覧表示")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, key=f"df_list_{year}_{month}")

    # ----- 月間カレンダー（マス表示） -----
    st.markdown("---")
    st.subheader("カレンダー表示（名前＋出勤退勤予定時間）")

    # 各日ごとの「名前＋時間」のリストを作成（HTMLで色分け）
    day_contents: dict[dt.date, list[str]] = {}

    # ログイン中スタッフID
    current_staff_id = str(current_staff["staff_id"])

    for _, row in month_shifts.iterrows():
        date_obj = dt.datetime.strptime(row["date"], "%Y-%m-%d").date()

        sid = str(row["staff_id"])
        name = get_staff_name(sid)

        # ポジション取得（なければ空文字）
        staff_row = STAFF_DF[STAFF_DF["staff_id"] == row["staff_id"]]
        position = ""
        if not staff_row.empty:
            position = str(staff_row.iloc[0].get("position") or "")

        start = row["start_time"] or ""
        end = row["end_time"] or ""
        time_part = f" {start}〜{end}" if start or end else ""

        # --- CSSクラス決定 ---
        css_classes = ["cal-line"]   # 基本クラス

        # ポジション別
        if "調理" in position:
            css_classes.append("pos-kitchen")
        elif "ホール" in position:
            css_classes.append("pos-hall")
        elif "オール" in position:
            css_classes.append("pos-allround")

        # ログイン中スタッフなら強調
        if sid == current_staff_id:
            css_classes.append("self-staff")

        class_str = " ".join(css_classes)

        # 1行分のHTML
        line_html = f'<div class="{class_str}">{name}{time_part}</div>'

        day_contents.setdefault(date_obj, []).append(line_html)


    # 各日ごとの「希望 / NG」情報を作成
    requests_info: dict[dt.date, dict] = {}

    for _, row in month_requests.iterrows():
        date_obj = dt.datetime.strptime(row["date"], "%Y-%m-%d").date()
        staff_row = get_staff_name(str(row["staff_id"]))
        name = get_staff_name(str(row["staff_id"]))
        rtype = row["request_type"]  # '希望' or 'NG'

        if date_obj not in requests_info:
            requests_info[date_obj] = {"希望": [], "NG": []}

        if rtype == "希望":
            requests_info[date_obj]["希望"].append(name)
        elif rtype == "NG":
            requests_info[date_obj]["NG"].append(name)

    html = build_month_calendar_html(
        year,
        month,
        day_contents,
        shortage_info,
        holiday_info,
        requests_info,
    )
    st.markdown(html, unsafe_allow_html=True)

    # =======================================
    # 今月のスタッフ別シフト集計
    # =======================================
    st.markdown("---")
    st.subheader("今月のスタッフ別シフト集計")

    # month_shifts: すでに「対象月のシフト」だけに絞った DataFrame がある前提
    if month_shifts.empty:
        st.info("この月にはまだシフトが登録されていません。")
    else:
        # スタッフ名・区分を付与
        ms = month_shifts.merge(STAFF_DF, on="staff_id", how="left")

        # 文字列の時刻を「分」に変換するヘルパー
        def _time_str_to_minutes(s: str) -> int:
            if not s or pd.isna(s):
                return None
            s = str(s).strip()
            # 24:00 対応
            if s in ("24:00", "24"):
                return 24 * 60
            try:
                hh, mm = s.split(":")
                return int(hh) * 60 + int(mm)
            except Exception:
                return None

        def _calc_hours(row) -> float:
            start = row.get("start_time") or DEFAULT_OPEN_TIME
            end = row.get("end_time") or DEFAULT_CLOSE_TIME

            sm = _time_str_to_minutes(start)
            em = _time_str_to_minutes(end)

            if sm is None or em is None:
                return 0.0

            # 日付またぎ対応（例: 17:00〜24:00, 20:00〜02:00 など）
            if em <= sm:
                em += 24 * 60

            diff_min = em - sm
            return diff_min / 60.0

        ms["work_hours"] = ms.apply(_calc_hours, axis=1)

        # スタッフ別集計
        summary = (
            ms.groupby(["staff_id", "name", "role"], as_index=False)
              .agg(
                  出勤回数=("date", "count"),
                  勤務時間合計_時間=("work_hours", "sum"),
              )
        )

        # ---- desired_shifts_per_month を merge（上限値を付与） ----
        summary = summary.merge(
            STAFF_DF[["staff_id", "desired_shifts_per_month"]],
            on="staff_id",
            how="left"
        )

        summary["desired_shifts_per_month"] = (
            summary["desired_shifts_per_month"].fillna(0).astype(int)
        )

        # ---- 比較指標を追加 ----
        summary["残り可能回数"] = summary["desired_shifts_per_month"] - summary["出勤回数"]

        summary["稼働率(%)"] = summary.apply(
            lambda r: (r["出勤回数"] / r["desired_shifts_per_month"] * 100)
            if r["desired_shifts_per_month"] > 0 else 0,
            axis=1
        ).round(1)

        # ---- 列を見やすく並べ替え ----
        summary = summary[
            [
                "staff_id",
                "name",
                "role",
                "出勤回数",
                "desired_shifts_per_month",
                "残り可能回数",
                "稼働率(%)",
                "勤務時間合計_時間",
            ]
        ]

        # 全体合計の行を追加
        total_row = {
            "staff_id": "合計",
            "name": "",
            "role": "",
            "出勤回数": int(summary["出勤回数"].sum()),
            "勤務時間合計_時間": float(summary["勤務時間合計_時間"].sum()),
        }
        summary_with_total = pd.concat(
            [summary, pd.DataFrame([total_row])],
            ignore_index=True,
        )

        # 小数1桁くらいに丸める（見やすさ用）
        summary_with_total["勤務時間合計_時間"] = summary_with_total["勤務時間合計_時間"].round(1)

        st.dataframe(
            summary_with_total,
            use_container_width=True,
            height=400,
        )

    # ----- シフトを追加フォーム（簡易版） -----
    st.markdown("---")
    st.subheader("シフトを追加（簡易版）")

    date_to_add = st.date_input(
        "日付を選択（追加）",
        key="add_date",
        value=today,
        min_value=today - dt.timedelta(days=365),
    )

    # 社員は全員分、バイトは自分だけ
    if is_employee:
        staff_for_form = STAFF_DF
    else:
        staff_for_form = STAFF_DF[STAFF_DF["staff_id"] == current_staff["staff_id"]]

    staff_label_to_id = {
        get_staff_label(row): row["staff_id"]
        for _, row in staff_for_form.iterrows()
    }

    staff_choice_label = st.selectbox("スタッフ（追加）", list(staff_label_to_id.keys()))
    staff_id_selected = staff_label_to_id[staff_choice_label]

    col3, col4 = st.columns(2)
    with col3:
        start_time_str = st.text_input(
            "開始時間 (HH:MM)", value=DEFAULT_OPEN_TIME, key="add_start"
        )
    with col4:
        end_time_str = st.text_input(
            "終了時間 (HH:MM)", value=DEFAULT_CLOSE_TIME, key="add_end"
        )

    if st.button("シフトを追加"):
        new_row = {
            "date": date_to_add.strftime("%Y-%m-%d"),
            "staff_id": staff_id_selected,
            "start_time": start_time_str,
            "end_time": end_time_str,
            "source": "manual",
        }
        shifts_df = pd.concat([shifts_df, pd.DataFrame([new_row])], ignore_index=True)
        save_csv(shifts_df, SHIFT_FILE)
        st.success("シフトを追加しました。")
        st.rerun()

    # ----- 既存シフトを編集 / 削除 -----
    st.markdown("---")
    st.subheader("既存シフトを編集 / 削除")

    edit_date = st.date_input(
        "日付を選択（編集）",
        key="edit_date",
        value=today,
        min_value=today - dt.timedelta(days=365),
    )
    edit_date_str = edit_date.strftime("%Y-%m-%d")

    # 編集対象のシフトを絞り込み
    if is_employee:
        # 社員は制限なし
        editable_shifts = shifts_df[shifts_df["date"] == edit_date_str]
    else:
        # アルバイト：自分のシフト かつ edit_lock_until より後の日付だけ
        lock_str = edit_lock_until.strftime("%Y-%m-%d")
        editable_shifts = shifts_df[
            (shifts_df["date"] == edit_date_str)
            & (shifts_df["staff_id"] == current_staff["staff_id"])
            & (shifts_df["date"] > lock_str)
        ]

    if editable_shifts.empty:
        st.info("この日には編集できるシフトがありません。")
    else:
        # シフト選択プルダウン（内部的には DataFrame の index を使う）
        options = editable_shifts.index.tolist()

        def format_shift_option(idx: int) -> str:
            row = editable_shifts.loc[idx]
            name = get_staff_name(str(row["staff_id"]))
            return f"{name} {row['date']} {row['start_time']}〜{row['end_time']}"

        selected_idx = st.selectbox(
            "編集するシフトを選択",
            options,
            format_func=format_shift_option,
            key="edit_shift_select",
        )

        # 選択されたシフトの現在値を取得
        selected_row = editable_shifts.loc[selected_idx]
        current_date_obj = dt.datetime.strptime(selected_row["date"], "%Y-%m-%d").date()

        # 日付変更時の最小日付（非社員は edit_lock_until 以降のみ）
        min_editable_date = today - dt.timedelta(days=365)
        if not is_employee and edit_lock_until > min_editable_date:
            min_editable_date = edit_lock_until

        edit_target_date = st.date_input(
            "日付（変更可）",
            key="edit_target_date",
            value=current_date_obj,
            min_value=min_editable_date,
        )

        col_e1, col_e2 = st.columns(2)
        with col_e1:
            new_start = st.text_input(
                "開始時間 (HH:MM)",
                value=selected_row["start_time"] or "",
                key="edit_start",
            )
        with col_e2:
            new_end = st.text_input(
                "終了時間 (HH:MM)",
                value=selected_row["end_time"] or "",
                key="edit_end",
            )

        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            if st.button("この内容で更新"):
                shifts_df.loc[selected_idx, "date"] = edit_target_date.strftime("%Y-%m-%d")
                shifts_df.loc[selected_idx, "start_time"] = new_start
                shifts_df.loc[selected_idx, "end_time"] = new_end
                save_csv(shifts_df, SHIFT_FILE)
                st.success("シフトを更新しました。")
                st.rerun()

        with col_btn2:
            if st.button("このシフトを削除"):
                shifts_df = shifts_df.drop(index=selected_idx)
                save_csv(shifts_df, SHIFT_FILE)
                st.success("シフトを削除しました。")
                st.rerun()

    # =============================
    # 🔥 管理者専用：この月のシフトをリセット（ページ一番下）
    # =============================
    if is_employee:
        st.markdown("---")
        st.subheader("⚠️ 管理者専用：この月のシフトをリセット")

        st.caption(
            "このボタンを押すと、選択中の年月に登録されているシフトを **すべて削除** します。\n"
            "シフト希望・NG情報（requests.csv）は削除されません。"
        )

        if st.button("🚨 この月のシフトを全て削除する（取り消し不可）", key="reset_month_bottom"):
            month_prefix_for_reset = f"{year:04d}-{month:02d}-"

            # この月のシフトだけ除外して残す
            remaining = shifts_df[~shifts_df["date"].str.startswith(month_prefix_for_reset)]

            save_csv(remaining, SHIFT_FILE)

            st.success(f"{year}年{month}月のシフトを全て削除しました。（希望/NGは残しています）")
            st.rerun()

# =========================
# シフト希望・NG入力
# =========================
def page_shift_request(current_staff):
    st.header("✋ シフト希望・NG日入力")

    # --- データ読み込み（ID付き） ---
    requests_df = load_csv(
        REQUEST_FILE,
        ["request_id", "date", "staff_id", "request_type", "start_time", "end_time", "note"],
    )
    requests_df = ensure_request_ids(requests_df)

    today = dt.date.today()

    # ================================
    # 💡 デフォルト対象日：
    #   「他ページのデフォ月」のさらに +1 ヶ月 の 1 日
    # ================================
    base_year, base_month = get_default_year_month_for_ui()

    # base_year/base_month をさらに +1ヶ月
    if base_month == 12:
        target_year = base_year + 1
        target_month = 1
    else:
        target_year = base_year
        target_month = base_month + 1

    default_date = dt.date(target_year, target_month, 1)

    # ---------------- 新規登録 ----------------
    st.subheader("新規登録")
    date_selected = st.date_input(
        "対象日",
        value=default_date,
        min_value=today - dt.timedelta(days=365),  # ここは必要に応じて future only にしてもOK
    )

    request_type = st.radio(
        "区分", ["希望シフト", "NG日（入れない）"], horizontal=True
    )

    start_time_str = None
    end_time_str = None
    if request_type == "希望シフト":
        col1, col2 = st.columns(2)
        with col1:
            start_time_str = st.text_input("希望開始時間 (HH:MM)", value=DEFAULT_OPEN_TIME)
        with col2:
            end_time_str = st.text_input("希望終了時間 (HH:MM)", value=DEFAULT_CLOSE_TIME)

    note = st.text_area("メモ（任意）", "")

    if st.button("希望/NGを登録"):
        # 次の ID を採番
        if len(requests_df) == 0:
            next_id = 1
        else:
            next_id = int(requests_df["request_id"].max()) + 1

        new_row = {
            "request_id": next_id,
            "date": date_selected.strftime("%Y-%m-%d"),
            "staff_id": current_staff["staff_id"],
            "request_type": "希望" if request_type == "希望シフト" else "NG",
            "start_time": start_time_str,
            "end_time": end_time_str,
            "note": note,
        }
        requests_df = pd.concat(
            [requests_df, pd.DataFrame([new_row])],
            ignore_index=True,
        )
        save_csv(requests_df, REQUEST_FILE)
        st.success("登録しました。")

    # ---------------- 一覧 & 取り消し ----------------
    st.markdown("---")
    st.subheader("自分の登録済み希望・NG一覧")

    my_requests = requests_df[requests_df["staff_id"] == current_staff["staff_id"]]
    if len(my_requests) == 0:
        st.info("まだ登録がありません。")
        return

    my_requests = my_requests.sort_values(["date", "request_id"])
    st.dataframe(
        my_requests[["request_id", "date", "request_type", "start_time", "end_time", "note"]],
        use_container_width=True,
    )

    st.markdown("### 登録済みの希望/NGを取り消す")

    target_id = st.selectbox(
        "取り消したい行（ID）を選択してください",
        options=my_requests["request_id"].tolist(),
        format_func=lambda x: f"ID {x}",
    )

    target_row = my_requests[my_requests["request_id"] == target_id].iloc[0]
    st.write(
        f"**ID {target_id}**: {target_row['date']} / "
        f"{target_row['request_type']} / "
        f"{(target_row['start_time'] or '')}〜{(target_row['end_time'] or '')} "
        f"{' / ' + target_row['note'] if isinstance(target_row['note'], str) and target_row['note'] else ''}"
    )

    if st.button("この希望/NGを取り消す（削除）"):
        new_df = requests_df[
            ~(
                (requests_df["request_id"] == target_id)
                & (requests_df["staff_id"] == current_staff["staff_id"])
            )
        ]
        save_csv(new_df, REQUEST_FILE)
        st.success(f"ID {target_id} の希望/NGを取り消しました。")
        st.rerun()


# =========================
# 自動シフト提案（既存シフトを尊重して「不足分だけ」埋める版）
# 仕様：
#  1. 全日、社員2人になるように優先的に割り振る
#  2. まだ月上限に余裕があれば、
#     金曜 → 土曜 → 日曜 → （平日の）祝日の順で社員3人目を入れる
#  3. その後、平日5人 / 週末6人 を満たすように
#     アルバイトを含めて不足分だけ埋める
# =========================
def auto_assign_shifts_for_month(
    year: int,
    month: int,
    requests_df: pd.DataFrame,
    existing_shifts_df: pd.DataFrame,
):
    dates = date_range_for_month(year, month)

    # ---- 希望 / NG を日付ごとにまとめる ----
    req_by_date: dict[str, dict] = {}
    for _, r in requests_df.iterrows():
        d = str(r["date"])
        if d not in req_by_date:
            req_by_date[d] = {"希望": set(), "NG": set()}
        if r["request_type"] == "希望":
            req_by_date[d]["希望"].add(str(r["staff_id"]))
        elif r["request_type"] == "NG":
            req_by_date[d]["NG"].add(str(r["staff_id"]))

    # ---- 社員 / アルバイトリスト ----
    employees = [str(s) for s in STAFF_DF[STAFF_DF["role"] == "社員"]["staff_id"].tolist()]
    parttimers = [str(s) for s in STAFF_DF[STAFF_DF["role"] == "アルバイト"]["staff_id"].tolist()]

    # ---- ポジション情報 ----
    position_map = {
        str(row["staff_id"]): str(row.get("position") or "")
        for _, row in STAFF_DF.iterrows()
    }

    MANAGER_ID = "S001"  # 宮首さん
    CHEF_ID = "S002"     # 山田(料理長)
    EMP_SATO_ID = "S003" # 佐藤(社員)

    def is_kitchen_capable_for_day(sid: str, sato_is_off: bool) -> bool:
        """そのスタッフが『この日』キッチン要員として数えて良いか"""
        # 佐藤はキッチンには入れない
        if sid == EMP_SATO_ID:
            return False

        # 佐藤が休みの日は店長をキッチンカウントから外す
        if sato_is_off and sid == MANAGER_ID:
            return False

        pos = position_map.get(sid, "")
        if pos in ("料理長", "調理場担当", "オールラウンド"):
            return True
        if sid == MANAGER_ID:
            # 店長は基本的にはキッチンも可能
            return True
        return False

    def kitchen_priority_rank(sid: str) -> int:
        """キッチン要員の優先度（数字が小さいほど優先）"""
        pos = position_map.get(sid, "")
        if sid == CHEF_ID:              # 料理長
            return 0
        if pos == "調理場担当":          # バイト調理
            return 1
        if pos == "オールラウンド":      # バイトオールラウンド
            return 2
        if sid == MANAGER_ID:           # 店長
            return 3
        return 9                         # それ以外は低優先

    def counts_for_hall(sid: str) -> bool:
        """ホール人数としてカウントして良いか"""
        pos = position_map.get(sid, "")
        # 調理場専任バイトはホール人数には含めない
        if pos == "調理場担当":
            return False
        return True

    # ---- 社員の固定休 {staff_id -> {曜日indexセット}} ----
    employee_dayoff_map: dict[str, set[int]] = {}
    for _, row in STAFF_DF.iterrows():
        sid = str(row["staff_id"])
        if row["role"] != "社員":
            continue
        offs = set()
        for col in ["dayoff1", "dayoff2"]:
            if col in row and not pd.isna(row[col]):
                try:
                    w = int(row[col])
                    if 0 <= w <= 6:
                        offs.add(w)
                except Exception:
                    pass
        employee_dayoff_map[sid] = offs

    def _to_int_safe(x, default=0):
        """NaN や空文字でも落ちない int 変換ヘルパー"""
        try:
            if pd.isna(x):
                return default
            return int(x)
        except Exception:
            return default

    # ---- 月あたり最大シフト回数 ----
    if "desired_shifts_per_month" in STAFF_DF.columns:
        # 新方式：月上限をそのまま使う（例: 23 回）
        max_shifts_per_person = {
            str(row["staff_id"]): _to_int_safe(row["desired_shifts_per_month"], 0)
            for _, row in STAFF_DF.iterrows()
        }
    elif "desired_shifts_per_week" in STAFF_DF.columns:
        # 旧方式との後方互換：週希望回数 × 4 を月上限とみなす
        max_shifts_per_person = {
            str(row["staff_id"]): _to_int_safe(row["desired_shifts_per_week"], 0) * 4
            for _, row in STAFF_DF.iterrows()
        }
    else:
        # どちらの列も無い場合のデフォルト（とりあえず 12 回 / 月）
        max_shifts_per_person = {
            str(row["staff_id"]): 12
            for _, row in STAFF_DF.iterrows()
        }

    # ---- 既存シフトぶんを人数カウントに反映 ----
    assigned_count = {sid: 0 for sid in max_shifts_per_person.keys()}
    for _, row in existing_shifts_df.iterrows():
        sid = str(row["staff_id"])
        if sid in assigned_count:
            assigned_count[sid] += 1

    # ---- ヘルパー：ある日の社員候補を作る ----
    def make_employee_candidates(
        d: dt.date,
        assigned_today: list[str],
        hope_set: set[str],
        ng_set: set[str],
    ) -> list[dict]:
        weekday_idx = d.weekday()
        cand = []
        for sid in employees:
            # NG
            if sid in ng_set:
                continue
            # 当日すでに入っている
            if sid in assigned_today:
                continue
            # 月上限チェック
            max_cap = max_shifts_per_person.get(sid, 0)
            used = assigned_count.get(sid, 0)
            remaining = max_cap - used
            if remaining <= 0:
                continue
            # 固定休
            offs = employee_dayoff_map.get(sid, set())
            if weekday_idx in offs:
                continue

            cand.append(
                {
                    "staff_id": sid,
                    "is_hope": sid in hope_set,   # 希望日
                    "assigned_count": used,       # すでに何回入ってるか
                    "remaining_cap": remaining,   # あと何回入れるか（均等化の核）
                }
            )

        # --- 均等化ソート ---
        cand.sort(
            key=lambda c: (
                not c["is_hope"],      # 希望者を最優先
                -c["remaining_cap"],   # 残り枠の多い人を優先（均等化）
                c["assigned_count"],   # 現在の担当回数が少ない人
                c["staff_id"],         # タイブレーク
            )
        )
        return cand

    # ---- ヘルパー：ある日を「社員 target_emp 人」に近づける ----
    new_shift_rows: list[dict] = []

    def assign_employees_for_day(d: dt.date, target_emp: int):
        nonlocal new_shift_rows

        day_str = d.strftime("%Y-%m-%d")
        # その日の既存シフト（社員 / バイト混在）
        existing_today = existing_shifts_df[existing_shifts_df["date"] == day_str]
        assigned_today = existing_today["staff_id"].astype(str).tolist()

        # すでに追加した自動案も考慮
        for r in new_shift_rows:
            if r["date"] == day_str:
                assigned_today.append(str(r["staff_id"]))

        # その日に既に入っている社員数
        current_emp = sum(1 for sid in assigned_today if sid in employees)
        need = max(target_emp - current_emp, 0)
        if need <= 0:
            return  # もう十分入っている

        day_info = req_by_date.get(day_str, {"希望": set(), "NG": set()})
        hope_set = day_info["希望"]
        ng_set = day_info["NG"]

        for _ in range(need):
            cand = make_employee_candidates(d, assigned_today, hope_set, ng_set)
            if not cand:
                break  # 入れられる社員がもういない

            chosen = cand[0]
            sid = chosen["staff_id"]

            assigned_today.append(sid)
            assigned_count[sid] = assigned_count.get(sid, 0) + 1

            new_shift_rows.append(
                {
                    "date": day_str,
                    "staff_id": sid,
                    "start_time": DEFAULT_OPEN_TIME,
                    "end_time": DEFAULT_CLOSE_TIME,
                    "source": "auto",
                }
            )

    # =========================
    # Phase 1: まず全日を「社員2人」に近づける
    # =========================
    for d in dates:
        assign_employees_for_day(d, target_emp=2)

    # =========================
    # Phase 2: 金→土→日→祝（平日）に社員3人目を入れる
    # =========================
    fridays = [d for d in dates if d.weekday() == 4]
    saturdays = [d for d in dates if d.weekday() == 5]
    sundays = [d for d in dates if d.weekday() == 6]
    # 祝日は、平日（月〜木）の祝日のみ対象にして重複を避ける
    holiday_weekdays = [
        d
        for d in dates
        if get_jp_holiday_name(d) and d.weekday() <= 3
    ]

    for d in fridays + saturdays + sundays + holiday_weekdays:
        assign_employees_for_day(d, target_emp=3)

    # =========================
    # Phase 3: 残りの「人数不足分」を社員＋アルバイトで埋める
    # =========================
    # ここからは、既存シフト + new_shift_rows を起点に、
    # 平日5人 / 週末6人 を満たすように不足分だけ追加する。
    combined = pd.concat(
        [
            existing_shifts_df,
            pd.DataFrame(new_shift_rows),
        ],
        ignore_index=True,
    )

    # パート含めた上限カウントを更新
    # （社員分はすでに増えているのでそのまま、バイト分も足しておく）
    for _, row in combined.iterrows():
        sid = str(row["staff_id"])
        if sid in assigned_count:
            # 既に社員分は加算済みだが、二重にはならないように注意。
            # ここでは「existing_shifts_df」ぶんだけ追加したいが、
            # new_shift_rows はすでに上で加算しているのでスキップしてもよい。
            pass

    # バイトも含めた候補生成
    def make_any_candidates(
        d: dt.date,
        assigned_today: list[str],
        hope_set: set[str],
        ng_set: set[str],
        mode: str,
        sato_is_off: bool,
    ) -> list[dict]:
        """
        mode:
          - 'kitchen': キッチン不足分を優先して埋める
          - 'hall'   : ホール不足分を優先して埋める
          - 'any'    : 通常（これまで通り）
        """
        weekday_idx = d.weekday()
        cand = []
        for sid in employees + parttimers:
            # NG
            if sid in ng_set:
                continue
            # すでに当日入っている
            if sid in assigned_today:
                continue
            # 月上限
            if assigned_count.get(sid, 0) >= max_shifts_per_person.get(sid, 0):
                continue
            # 固定休（社員のみ）
            if sid in employees:
                offs = employee_dayoff_map.get(sid, set())
                if weekday_idx in offs:
                    continue

            pos = position_map.get(sid, "")

            # --- mode別フィルタ ---
            if mode == "kitchen":
                if not is_kitchen_capable_for_day(sid, sato_is_off):
                    continue
            elif mode == "hall":
                if not counts_for_hall(sid):
                    continue
            else:
                pass  # any の時は特に制限なし

            cand.append(
                {
                    "staff_id": sid,
                    "is_employee": sid in employees,
                    "is_hope": sid in hope_set,
                    "assigned_count": assigned_count.get(sid, 0),
                    "pos": pos,
                }
            )

        # --- ソート ---
        if mode == "kitchen":
            # 希望日 → キッチン優先度 → 回数少ない → ID
            cand.sort(
                key=lambda c: (
                    not c["is_hope"],
                    kitchen_priority_rank(c["staff_id"]),
                    c["assigned_count"],
                    c["staff_id"],
                )
            )
        elif mode == "hall":
            # 希望日 → バイト優先 → 回数少ない → ID
            cand.sort(
                key=lambda c: (
                    not c["is_hope"],
                    c["is_employee"],      # ★ ここを変更（バイト優先）
                    c["assigned_count"],
                    c["staff_id"],
                )
            )
        else:  # any
            # 希望日 → バイト優先 → 回数少ない → ID
            cand.sort(
                key=lambda c: (
                    not c["is_hope"],
                    c["is_employee"],      # ★ ここも同じく変更
                    c["assigned_count"],
                    c["staff_id"],
                )
            )


        return cand

    # 日毎に不足分だけ埋める
    for d in dates:
        day_str = d.strftime("%Y-%m-%d")
        weekend_flag = is_weekend(d)
        required_staff = WEEKEND_REQUIRED_STAFF if weekend_flag else WEEKDAY_REQUIRED_STAFF

        day_shifts = combined[combined["date"] == day_str]
        # combined の中に new_shift_rows も既に含まれているので、これだけでOK
        assigned_today = day_shifts["staff_id"].astype(str).tolist()

        current_count = len(assigned_today)
        remaining = max(required_staff - current_count, 0)
        if remaining <= 0:
            continue

        day_info = req_by_date.get(day_str, {"希望": set(), "NG": set()})
        hope_set = day_info["希望"]
        ng_set = day_info["NG"]

        # 佐藤がその日に入っているか
        sato_is_off = (EMP_SATO_ID not in assigned_today)

        for _ in range(remaining):
            # その時点でのキッチン / ホール人数を計算
            kitchen_count = sum(
                1
                for sid in assigned_today
                if is_kitchen_capable_for_day(sid, sato_is_off=False)  # 宮首も含めた純粋なキッチン能力
            )
            hall_count = sum(
                1
                for sid in assigned_today
                if counts_for_hall(sid)
            )

            need_kitchen = max(2 - kitchen_count, 0)
            need_hall = max(3 - hall_count, 0)

            if need_kitchen > 0:
                mode = "kitchen"
            elif need_hall > 0:
                mode = "hall"
            else:
                mode = "any"

            cand = make_any_candidates(d, assigned_today, hope_set, ng_set, mode, sato_is_off)
            if not cand:
                break  # 本当に誰も入れない

            chosen = cand[0]
            sid = chosen["staff_id"]
            assigned_today.append(sid)
            assigned_count[sid] = assigned_count.get(sid, 0) + 1
            new_shift_rows.append(
                {
                    "date": day_str,
                    "staff_id": sid,
                    "start_time": DEFAULT_OPEN_TIME,
                    "end_time": DEFAULT_CLOSE_TIME,
                    "source": "auto",
                }
            )

    # 最終的に「新しく追加されたぶんだけ」を返す
    return pd.DataFrame(new_shift_rows)


def page_auto_scheduler(current_staff):
    st.header("🤖 自動シフト提案（不足分を自動で埋める版）")

    if current_staff["role"] != "社員":
        st.warning("このページは社員のみ利用できます。")
        return

    st.info(
        "NG日を避けつつ、希望日を優先しながら\n"
        "・平日5人 / 週末6人\n"
        "・社員2人以上\n"
        "・各スタッフの週希望回数×4 を上限\n"
        "を満たすように、【不足分だけ】自動でシフトを埋めます。"
    )

    today = dt.date.today()
    default_year, default_month = get_default_year_month_for_ui()

    col1, col2 = st.columns(2)
    with col1:
        year = st.number_input("対象年", min_value=2024, max_value=2100, value=default_year)
    with col2:
        month = st.number_input("対象月", min_value=1, max_value=12, value=default_month)

    year = int(year)
    month = int(month)
    month_str_prefix = f"{year:04d}-{month:02d}-"

    # --------- データ読み込み ---------
    requests_df = load_csv(
        REQUEST_FILE,
        ["date", "staff_id", "request_type", "start_time", "end_time", "note"],
    )
    shifts_df = load_csv(
        SHIFT_FILE,
        ["date", "staff_id", "start_time", "end_time", "source"],
    )

    # 在籍スタッフID（文字列に揃える）
    active_ids = [str(sid) for sid in get_active_staff_ids()]

    # 対象月の既存シフト（★削除済みスタッフは除外）
    existing_month_shifts = shifts_df[
        (shifts_df["date"].str.startswith(month_str_prefix))
        & (shifts_df["staff_id"].astype(str).isin(active_ids))
    ]

    # --------- 対象月の既存シフト表示 ---------
    st.subheader("対象月の既存シフト")
    if existing_month_shifts.empty:
        st.info("この月にはまだ確定シフトがありません。")
    else:
        merged_exist = existing_month_shifts.merge(STAFF_DF, on="staff_id", how="left")
        merged_exist = merged_exist[
            ["date", "name", "role", "start_time", "end_time", "source"]
        ].sort_values(["date", "role", "name"])
        st.dataframe(merged_exist, use_container_width=True, height=250)

    # --------- 不足日チェック（本当に足りているかの確認） ---------
    shortage_days: list[str] = []
    employee_ids = (
        STAFF_DF[STAFF_DF["role"] == "社員"]["staff_id"]
        .astype(str)
        .tolist()
    )

    for d in date_range_for_month(year, month):
        day_str = d.strftime("%Y-%m-%d")
        weekend_flag = is_weekend(d)
        required_staff = WEEKEND_REQUIRED_STAFF if weekend_flag else WEEKDAY_REQUIRED_STAFF

        day_shifts = existing_month_shifts[existing_month_shifts["date"] == day_str]
        current_count = len(day_shifts)

        current_emp_count = len(
            day_shifts[day_shifts["staff_id"].astype(str).isin(employee_ids)]
        )

        if current_count < required_staff or current_emp_count < REQUIRED_EMPLOYEES:
            shortage_days.append(day_str)

    # ---------------- ボタンで自動案生成 ----------------
    if st.button("この条件で【不足分だけ】自動シフト案を生成"):
        month_requests = requests_df[requests_df["date"].str.startswith(month_str_prefix)]

        auto_df = auto_assign_shifts_for_month(
            int(year),
            int(month),
            month_requests,
            existing_month_shifts,
        )

        st.session_state["auto_shift_proposal_v2"] = auto_df

        if auto_df.empty:
            if shortage_days:
                # ★ 足りない日があるのに誰も入れられなかったケース
                st.warning(
                    "不足している日がありますが、希望回数/NG/固定休の条件のため、"
                    "自動で追加できるスタッフが見つかりませんでした。\n\n"
                    "・各スタッフの『週あたり希望シフト回数』\n"
                    "・社員の固定休\n"
                    "・シフト希望NG\n"
                    "を一度見直してみてください。"
                )
            else:
                # 本当に足りている
                st.success("この月はすでに必要人数・社員数を満たしているため、追加すべきシフトはありません。")
        else:
            st.success("自動シフト案を生成しました。下で内容を確認できます。")

    auto_df = st.session_state.get("auto_shift_proposal_v2")

    st.subheader("自動生成されたシフト案（テーブル表示）")
    if auto_df is None:
        st.info("まだ自動シフト案は生成されていません。")
        return

    if auto_df.empty:
        # メッセージはボタン押下時に出しているので、ここでは早期リターンだけ
        return

    # --- テーブルプレビュー ---
    merged = auto_df.merge(STAFF_DF, on="staff_id", how="left")
    merged = merged[["date", "name", "role", "start_time", "end_time", "source"]]
    merged = merged.sort_values(["date", "role", "name"])
    st.dataframe(merged, use_container_width=True, height=300)

    st.caption("※ まだ保存されていません。「不足分を既存シフトに追加して保存」を押すと確定します。")

    # --- マス目カレンダーでプレビュー（自動案を反映した状態） ---
    st.markdown("---")
    st.subheader("自動シフト提案を反映したカレンダープレビュー")

    # 既存シフト + 自動案を合算
    combined = pd.concat(
        [
            existing_month_shifts,
            auto_df[["date", "staff_id", "start_time", "end_time", "source"]],
        ],
        ignore_index=True,
    )

    #ここは、シフトカレンダーで使っている関数を流用すると楽です
    #current_staff_id = str(current_staff["staff_id"])
    #render_month_calendar_with_shifts(
    #    year,
    #    month,
    #    combined,
    #    title="",
    #    current_staff_id=current_staff_id,
    #)


    # 対象月の日付一覧
    all_dates = date_range_for_month(year, month)

    # 不足人数 & 祝日情報
    shortage_info: dict[dt.date, int] = {}
    holiday_info: dict[dt.date, str] = {}

    active_ids = get_active_staff_ids()

    for d in all_dates:
        day_str = d.strftime("%Y-%m-%d")
        is_weekend_flag = is_weekend(d)
        required_staff = WEEKEND_REQUIRED_STAFF if is_weekend_flag else WEEKDAY_REQUIRED_STAFF

        # 祝日名
        hname = get_jp_holiday_name(d)
        if hname:
            holiday_info[d] = hname

        # その日のシフト（既存＋自動案）
        day_shifts = combined[combined["date"] == day_str]

        # 🔴 削除済みスタッフは人数カウントから除外
        active_day_shifts = day_shifts[day_shifts["staff_id"].astype(str).isin(active_ids)]
        current_count = len(active_day_shifts)

        remaining = max(required_staff - current_count, 0)
        if remaining > 0:
            shortage_info[d] = remaining

    # カレンダーに表示する「名前＋時間」の文字列
    day_contents: dict[dt.date, list[str]] = {}
    for _, row in combined.iterrows():
        date_obj = dt.datetime.strptime(row["date"], "%Y-%m-%d").date()
        name = get_staff_name(str(row["staff_id"]))  # 削除済み対応
        start = row["start_time"] or ""
        end = row["end_time"] or ""
        time_part = f" {start}〜{end}" if start or end else ""
        text = f"{name}{time_part}"
        day_contents.setdefault(date_obj, []).append(text)

    # このプレビューでは希望/NGは使わない
    requests_info: dict[dt.date, dict] = {}

    html = build_month_calendar_html(
        year,
        month,
        day_contents,
        shortage_info,
        holiday_info,
        requests_info,
    )
    st.markdown(html, unsafe_allow_html=True)

    # --- 保存ボタン ---
    st.markdown("---")
    if st.button("不足分を既存シフトに追加して保存"):
        if auto_df.empty:
            st.info("追加するシフトがありません。")
            return

        # 最新のシフトファイルを読み直してから結合する
        shifts_df_latest = load_csv(
            SHIFT_FILE,
            ["date", "staff_id", "start_time", "end_time", "source"],
        )

        # 既存 + 自動案を結合
        merged = pd.concat(
            [shifts_df_latest, auto_df[["date", "staff_id", "start_time", "end_time", "source"]]],
            ignore_index=True,
        )

        # (date, staff_id) が重複している行は、最後のものだけ残す
        merged = merged.drop_duplicates(subset=["date", "staff_id"], keep="last")

        # 保存
        save_csv(merged, SHIFT_FILE)

        # 提案はクリアしておく（次に開いたとき二重に見えないように）
        st.session_state.pop("auto_shift_proposal_v2", None)

        st.success("自動シフト案をシフトファイルに反映しました。")
        st.rerun()


# =========================
# タイムカード（給与計算・休憩・深夜手当対応版）
# =========================
def page_timecard(current_staff):
    st.header("⏱ タイムカード")

    # 必要なカラムを指定して読み込み
    tc_cols = ["date", "staff_id", "clock_in", "clock_out", "hours", "late_hours", "pay"]
    timecards_df = load_csv(TIMECARD_FILE, tc_cols)

    today = dt.date.today().strftime("%Y-%m-%d")
    now = dt.datetime.now()
    sid = str(current_staff["staff_id"])

    existing_today = timecards_df[(timecards_df["date"] == today) & (timecards_df["staff_id"] == sid)]

    st.subheader("本日の打刻")
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("出勤", use_container_width=True) and existing_today.empty:
            new_row = pd.DataFrame([{
                "date": today, "staff_id": sid, "clock_in": now.strftime("%H:%M:%S"),
                "clock_out": None, "hours": 0.0, "late_hours": 0.0, "pay": 0
            }])
            save_csv(pd.concat([timecards_df, new_row], ignore_index=True), TIMECARD_FILE)
            st.rerun()

    with col2:
        if st.button("退勤", use_container_width=True) and not existing_today.empty:
            idx = existing_today.index[0]
            if pd.isna(timecards_df.loc[idx, "clock_out"]):
                # 計算ロジック
                fmt = "%H:%M:%S"
                start_t = dt.datetime.strptime(timecards_df.loc[idx, "clock_in"], fmt)
                end_t = now
                diff_h = (end_t - start_t.replace(year=end_t.year, month=end_t.month, day=end_t.day)).total_seconds() / 3600
                if diff_h < 0: diff_h += 24
                
                # 休憩・深夜計算
                break_h = 1.0 if diff_h > 8 else (0.75 if diff_h > 6 else 0.0)
                net_h = max(0, diff_h - break_h)
                limit_22 = start_t.replace(hour=22, minute=0, second=0, year=end_t.year, month=end_t.month, day=end_t.day)
                late_h = max(0, (end_t - max(start_t.replace(year=end_t.year, month=end_t.month, day=end_t.day), limit_22)).total_seconds() / 3600)
                
                # 給与確定
                wage = int(current_staff["hourly_wage"])
                total_pay = int((net_h * wage) + (late_h * wage * 0.25) + int(current_staff.get("transport_daily", 0) or 0))
                
                timecards_df.loc[idx, ["clock_out", "hours", "late_hours", "pay"]] = [now.strftime("%H:%M:%S"), round(net_h, 2), round(late_h, 2), total_pay]
                save_csv(timecards_df, TIMECARD_FILE)
                st.success(f"退勤完了: {total_pay}円"); st.rerun()

    st.markdown("---")
    my_records = timecards_df[timecards_df["staff_id"] == sid].sort_values("date", ascending=False)
    if not my_records.empty:
        st.metric("今月の総支給額（概算）", f"{int(my_records['pay'].sum()):,} 円")
        st.dataframe(my_records, use_container_width=True)

# =========================
# 連絡ボード（簡易チャット）
# =========================
def page_message_board(current_staff):
    st.header("💬 連絡ボード（社内連絡・シフト調整用）")

    messages_df = load_csv(
        MESSAGE_FILE,
        ["timestamp", "from_staff_id", "to_staff_id", "category", "message"],
    )

    st.subheader("新規メッセージ投稿")

    category = st.selectbox(
        "カテゴリ", ["全体連絡", "シフト交代相談", "その他"]
    )

    # 宛先
    to_options = ["全員に送信"] + [
        get_staff_label(row) for _, row in STAFF_DF.iterrows()
    ]
    to_choice = st.selectbox("宛先", to_options)
    to_staff_id = None
    if to_choice != "全員に送信":
        # ラベルから staff_id を逆引き
        for _, row in STAFF_DF.iterrows():
            if get_staff_label(row) == to_choice:
                to_staff_id = row["staff_id"]
                break

    msg = st.text_area("メッセージ内容", "")

    if st.button("送信"):
        if msg.strip() == "":
            st.warning("メッセージ内容を入力してください。")
        else:
            new_row = {
                "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                "from_staff_id": current_staff["staff_id"],
                "to_staff_id": to_staff_id,
                "category": category,
                "message": msg,
            }
            messages_df = pd.concat(
                [messages_df, pd.DataFrame([new_row])],
                ignore_index=True,
            )
            save_csv(messages_df, MESSAGE_FILE)
            st.success("メッセージを送信しました。")

    st.markdown("---")
    st.subheader("メッセージ一覧")

    # 自分に関係あるメッセージだけ表示する仕様にしても良いが、
    # ひとまず全件表示（後でフィルタ追加）
    merged = messages_df.merge(
        STAFF_DF[["staff_id", "name"]].rename(
            columns={"staff_id": "from_staff_id", "name": "from_name"}
        ),
        on="from_staff_id",
        how="left",
    )
    merged = merged.merge(
        STAFF_DF[["staff_id", "name"]].rename(
            columns={"staff_id": "to_staff_id", "name": "to_name"}
        ),
        on="to_staff_id",
        how="left",
    )

    merged["to_name"] = merged["to_name"].fillna("全員")
    merged = merged.sort_values("timestamp", ascending=False)

    show_cols = [
        "timestamp",
        "category",
        "from_name",
        "to_name",
        "message",
    ]
    st.dataframe(merged[show_cols], use_container_width=True, height=300)


# =========================
# 管理者設定（ダミー・今後拡張用）
# =========================
def generate_new_staff_id(df: pd.DataFrame, role: str) -> str:
    prefix = "S" if role == "社員" else "A"
    existing = [
        int(s[1:])
        for s in df["staff_id"].tolist()
        if isinstance(s, str) and s.startswith(prefix) and s[1:].isdigit()
    ]
    next_num = max(existing) + 1 if existing else 1
    return f"{prefix}{next_num:03d}"


def page_admin_settings(current_staff):
    global STAFF_DF

    st.header("⚙️ 管理者設定")

    if current_staff["role"] != "社員":
        st.warning("管理者設定は社員のみ利用できます。")
        return

    staff_df = STAFF_DF.copy()

    # 必要な列がなければデフォルト値で埋める
    defaults = {
        "desired_shifts_per_week": 0,   # 旧カラム（互換用）
        "desired_shifts_per_month": 0,  # 新カラム（本命）
        "desired_monthly_income": 0,
        "position": "",
        "dayoff1": pd.NA,
        "dayoff2": pd.NA,
    }

    for col, val in defaults.items():
        if col not in staff_df.columns:
            staff_df[col] = val

    weekday_labels = ["月", "火", "水", "木", "金", "土", "日"]
    weekday_label_to_value = {
        "月": 0,
        "火": 1,
        "水": 2,
        "木": 3,
        "金": 4,
        "土": 5,
        "日": 6,
    }

    role_options = ["社員", "アルバイト"]
    position_options = {
        "社員": ["店長", "料理長", "社員"],
        "アルバイト": ["調理場担当", "ホール担当", "オールラウンド"],
    }

    st.subheader("現在のスタッフ一覧")

    # 表示する列を動的に決める（旧CSVとの互換用）
    display_cols = [
        "staff_id",
        "name",
        "role",
        "position",
        "hourly_wage",
    ]

    # まずは月ベースの希望回数を優先
    if "desired_shifts_per_month" in staff_df.columns:
        display_cols.append("desired_shifts_per_month")
    elif "desired_shifts_per_week" in staff_df.columns:
        # 古いCSVの場合は週ベースを一応表示
        display_cols.append("desired_shifts_per_week")

    # 月希望収入
    if "desired_monthly_income" in staff_df.columns:
        display_cols.append("desired_monthly_income")

    st.dataframe(
        staff_df[display_cols],
        use_container_width=True,
        height=250,
    )

    # ---------------- スタッフ情報の編集 ----------------
    st.markdown("---")
    st.subheader("スタッフ情報の編集（名前・区分・ポジションなど）")

    for idx, row in staff_df.iterrows():
        staff_id = row["staff_id"]
        with st.expander(f"{row['staff_id']} : {row['name']}（{row['role']}）", expanded=False):
            # 名前
            name = st.text_input(
                "名前",
                value=row["name"],
                key=f"name_{staff_id}",
            )

            # 区分（社員 / アルバイト）
            cur_role = row["role"] if row["role"] in role_options else "アルバイト"
            role = st.selectbox(
                "区分",
                role_options,
                index=role_options.index(cur_role),
                key=f"role_{staff_id}",
            )

            # ポジション（役職・担当）
            pos_list = position_options[role]
            cur_pos = row.get("position")
            if not isinstance(cur_pos, str) or cur_pos not in pos_list:
                # デフォルトは最後（例: 社員→「社員」、アルバイト→「オールラウンド」）
                cur_pos = pos_list[-1]
            position = st.selectbox(
                "ポジション",
                pos_list,
                index=pos_list.index(cur_pos),
                key=f"pos_{staff_id}",
            )

            # 時給
            hourly = int(row.get("hourly_wage") or 0)
            hourly = st.number_input(
                "時給",
                min_value=0,
                max_value=10000,
                step=50,
                value=hourly,
                key=f"wage_{staff_id}",
            )

            # 月あたり最大シフト回数（新仕様）
            # まずは desired_shifts_per_month を優先し、
            # 空なら desired_shifts_per_week × 4 で初期値を作る
            raw_month = row.get("desired_shifts_per_month")

            def _to_int_safe(x, default=0):
                try:
                    if pd.isna(x) or x == "":
                        return default
                    return int(x)
                except Exception:
                    return default

            # 1. まずは desired_shifts_per_month を優先
            month_cap_default = _to_int_safe(raw_month, default=-1)

            if month_cap_default < 0:
                # 2. 無い場合は desired_shifts_per_week から推測
                raw_week = row.get("desired_shifts_per_week")
                week_val = _to_int_safe(raw_week, default=0)

                if week_val > 7:
                    # 旧UIで「月20回」を週カラムに入れていたケースを救済
                    month_cap_default = week_val
                else:
                    month_cap_default = week_val * 4

            # 3. number_input の制約に合わせてクリップ（0〜31）
            if month_cap_default < 0:
                month_cap_default = 0
            if month_cap_default > 31:
                month_cap_default = 31

            month_cap = st.number_input(
                "月あたり最大シフト回数",
                min_value=0,
                max_value=31,
                step=1,
                value=month_cap_default,
                key=f"monthcap_{staff_id}",
            )

            # 月希望収入（目安）
            raw_income = row.get("desired_monthly_income", 0)

            # NA や空文字でも落ちないように安全に変換
            if pd.isna(raw_income) or raw_income == "":
                income_default = 0
            else:
                try:
                    income_default = int(raw_income)
                except Exception:
                    income_default = 0

            income = st.number_input(
                "月希望収入（目安）",
                min_value=0,
                max_value=1_000_000,
                step=10_000,
                value=income_default,
                key=f"income_{staff_id}",
            )

            # 固定休（社員のみ）
            if role == "社員":
                # dayoff1
                v1 = row.get("dayoff1")
                if pd.isna(v1):
                    default_label1 = "（設定なし）"
                else:
                    try:
                        default_label1 = weekday_labels[int(v1)]
                    except Exception:
                        default_label1 = "（設定なし）"

                d1_label = st.selectbox(
                    "固定休1",
                    ["（設定なし）"] + weekday_labels,
                    index=(["（設定なし）"] + weekday_labels).index(default_label1),
                    key=f"dayoff1_{staff_id}",
                )

                # dayoff2
                v2 = row.get("dayoff2")
                if pd.isna(v2):
                    default_label2 = "（設定なし）"
                else:
                    try:
                        default_label2 = weekday_labels[int(v2)]
                    except Exception:
                        default_label2 = "（設定なし）"

                d2_label = st.selectbox(
                    "固定休2",
                    ["（設定なし）"] + weekday_labels,
                    index=(["（設定なし）"] + weekday_labels).index(default_label2),
                    key=f"dayoff2_{staff_id}",
                )
            else:
                d1_label = "（設定なし）"
                d2_label = "（設定なし）"

            # 一旦セッションに保存
            st.session_state[f"cfg_staff_{staff_id}"] = {
                "name": name,
                "role": role,
                "position": position,
                "hourly_wage": hourly,
                "month_cap": month_cap,   # ←ここ
                "income": income,
                "dayoff1_label": d1_label,
                "dayoff2_label": d2_label,
            }

    if st.button("スタッフ編集内容を保存"):
        for idx, row in staff_df.iterrows():
            staff_id = row["staff_id"]
            cfg = st.session_state.get(f"cfg_staff_{staff_id}")
            if not cfg:
                continue

            staff_df.at[idx, "name"] = cfg["name"]
            staff_df.at[idx, "role"] = cfg["role"]
            staff_df.at[idx, "position"] = cfg["position"]
            staff_df.at[idx, "hourly_wage"] = cfg["hourly_wage"]

            # 月あたり最大シフト回数を本命として保存
            month_cap = int(cfg["month_cap"])
            staff_df.at[idx, "desired_shifts_per_month"] = month_cap

            # 互換用に「週あたり目安」も入れておきたい場合（任意）
            week_cap = max(month_cap // 4, 0)
            staff_df.at[idx, "desired_shifts_per_week"] = week_cap

            staff_df.at[idx, "desired_monthly_income"] = int(cfg["income"])

            if cfg["role"] == "社員":
                l1 = cfg["dayoff1_label"]
                l2 = cfg["dayoff2_label"]
                staff_df.at[idx, "dayoff1"] = (
                    weekday_label_to_value[l1] if l1 in weekday_label_to_value else pd.NA
                )
                staff_df.at[idx, "dayoff2"] = (
                    weekday_label_to_value[l2] if l2 in weekday_label_to_value else pd.NA
                )
            else:
                staff_df.at[idx, "dayoff1"] = pd.NA
                staff_df.at[idx, "dayoff2"] = pd.NA

        staff_df.to_csv(STAFF_FILE, index=False, encoding="utf-8-sig")
        STAFF_DF = staff_df
        st.success("スタッフ情報を保存しました。")

    # ---------------- 新規スタッフ追加 ----------------
    st.markdown("---")
    st.subheader("スタッフを新規追加")

    with st.expander("新しいスタッフを追加する", expanded=False):
        new_name = st.text_input("名前", key="new_staff_name")

        new_role = st.selectbox(
            "区分",
            role_options,
            key="new_staff_role",
        )

        pos_list_new = position_options[new_role]
        new_pos = st.selectbox(
            "ポジション",
            pos_list_new,
            key="new_staff_pos",
        )

        new_hourly = st.number_input(
            "時給",
            min_value=0,
            max_value=10000,
            step=50,
            value=1300,
            key="new_staff_hourly",
        )

        new_month_cap = st.number_input(
            "月あたり最大シフト回数",
            min_value=0,
            max_value=31,
            step=1,
            value=12,
            key="new_staff_month_cap",
        )


        new_income = st.number_input(
            "月希望収入（目安）",
            min_value=0,
            max_value=1_000_000,
            step=10_000,
            value=80_000,
            key="new_staff_income",
        )
        if st.button("この内容でスタッフを追加"):
            if not new_name:
                st.warning("名前を入力してください。")
            else:
                new_id = generate_new_staff_id(staff_df, new_role)
                week_cap = max(int(new_month_cap) // 4, 0)

                new_row = {
                    "staff_id": new_id,
                    "name": new_name,
                    "role": new_role,
                    "position": new_pos,
                    "hourly_wage": int(new_hourly),
                    "desired_shifts_per_month": int(new_month_cap),
                    "desired_shifts_per_week": week_cap,   # 互換用
                    "desired_monthly_income": int(new_income),
                    "dayoff1": pd.NA,
                    "dayoff2": pd.NA,
                }

                staff_df = pd.concat(
                    [staff_df, pd.DataFrame([new_row])],
                    ignore_index=True,
                )
                staff_df.to_csv(STAFF_FILE, index=False, encoding="utf-8-sig")
                STAFF_DF = staff_df
                st.success(f"スタッフ {new_name}（{new_id}）を追加しました。")
                st.rerun()
    st.markdown("---")
    st.subheader("削除済みスタッフのシフトをクリーンアップ（上級者向け）")

    year_clean = st.number_input("対象年", min_value=2024, max_value=2100, value=dt.date.today().year)
    month_clean = st.number_input("対象月", min_value=1, max_value=12, value=dt.date.today().month)

    if st.button("この年月の削除済みスタッフのシフトを削除する"):
        active_ids = get_active_staff_ids()
        ym_prefix = f"{int(year_clean):04d}-{int(month_clean):02d}-"

        shifts_df = load_csv(
            SHIFT_FILE,
            ["date", "staff_id", "start_time", "end_time", "source"],
        )

        before = len(shifts_df)
        mask_target_month = shifts_df["date"].str.startswith(ym_prefix)
        mask_deleted_staff = ~shifts_df["staff_id"].astype(str).isin(active_ids)
        shifts_df = shifts_df[~(mask_target_month & mask_deleted_staff)]
        after = len(shifts_df)

        save_csv(shifts_df, SHIFT_FILE)

        st.success(f"{before - after} 件のシフトを削除しました。")
        st.info("※過去の履歴からも削除されます。必要であれば実行前にバックアップを取ってください。")


    # ---------------- スタッフ削除 ----------------
    st.markdown("---")
    st.subheader("スタッフ削除")

    # 過去のシフトは残したいので、ここでは master から消すだけにする
    choices = staff_df["staff_id"].tolist()
    if not choices:
        st.info("削除できるスタッフがいません。")
        return

    def format_staff_label(sid: str) -> str:
        row = staff_df[staff_df["staff_id"] == sid].iloc[0]
        return f"{sid} : {row['name']}（{row['role']}）"

    delete_ids = st.multiselect(
        "削除するスタッフを選択（※過去のシフトには名前が表示されなくなります）",
        options=choices,
        format_func=format_staff_label,
        key="delete_staff_ids",
    )

    if st.button("選択したスタッフを削除"):
        if not delete_ids:
            st.info("削除するスタッフが選択されていません。")
        else:
            staff_df = staff_df[~staff_df["staff_id"].isin(delete_ids)]
            staff_df.to_csv(STAFF_FILE, index=False, encoding="utf-8-sig")
            STAFF_DF = staff_df
            st.success(f"{len(delete_ids)} 名のスタッフを削除しました。")
            st.rerun()

    # ---------------- データバックアップ ----------------
    st.markdown("---")
    st.subheader("データバックアップ")

    st.caption(
        "現在のスタッフマスタ / シフト / 希望・NG をまとめてバックアップします。\n"
        "backups/ 以下に日時入りフォルダが作成されます。"
    )

    if st.button("CSVをまとめてバックアップする"):
        dest_dir, copied = backup_all_data()
        if copied:
            st.success(f"バックアップ完了: {dest_dir}")
            st.write("作成されたファイル:")
            for p in copied:
                st.code(p, language="bash")
        else:
            st.warning("バックアップ対象のCSVファイルが見つかりませんでした。")


# =========================
# 【修正】メイン制御（重複を除去した決定版）
# =========================
def main():
    # 1. 簡易パスワード認証
    if "authenticated" not in st.session_state:
        st.title("🔐 " + APP_TITLE)
        pw = st.text_input("Password", type="password")
        if st.button("Login"):
            if pw == ADMIN_PASSWORD:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Wrong password")
        return

    # 2. ログイン後のサイドバー共通表示
    st.sidebar.title("🍷 TSCTメニュー")
    
    # ログアウトボタン
    if st.sidebar.button("🔓 ログアウト"):
        del st.session_state.authenticated
        st.rerun()

    # スタッフ選択
    staff_name = st.sidebar.selectbox("スタッフ選択", STAFF_DF["name"].tolist())
    current_staff = get_staff_by_name(staff_name)
    st.sidebar.write(f"ログイン中: {current_staff['role']}")

    # 3. ページ切り替えメニュー
    page = st.sidebar.radio(
        "機能を選択", 
        ("シフトカレンダー", "シフト希望入力", "自動シフト提案", "タイムカード", "連絡ボード", "管理者設定")
    )

    # 4. 各ページ関数の呼び出し
    if page == "シフトカレンダー":
        page_shift_calendar(current_staff)
    elif page == "シフト希望入力":
        page_shift_request(current_staff)
    elif page == "自動シフト提案":
        page_auto_scheduler(current_staff)
    elif page == "タイムカード":
        page_timecard(current_staff)
    elif page == "連絡ボード":
        page_message_board(current_staff)
    elif page == "管理者設定":
        page_admin_settings(current_staff)

if __name__ == "__main__":
    main()
