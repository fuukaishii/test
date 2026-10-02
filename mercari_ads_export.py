#!/usr/bin/env python3
"""メルカリ広告管理画面: 配信中キャンペーンの当月日次CSV -> Googleスプシ(当月分のみ上書き)

使い方:
  python mercari_ads_export.py --login   # 初回のみ。ブラウザが開くので手動ログイン(2FA可)。セッションを保存
  python mercari_ads_export.py           # 通常実行(headless)。cron用
  python mercari_ads_export.py --headed  # 画面を見ながら実行(セレクタ調整用)
  python mercari_ads_export.py --csv x.csv  # DLを飛ばして既存CSVでシート更新のみ試す
"""
import argparse
import csv
import io
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
CAMPAIGNS_URL = (
    "https://ads-manager.partner.mercari.com/biz-accounts/yCaeSbVBUdToVHT2jXTDE4"
    "/ad-accounts/KDdQNN7QndqffnZywfMPec/campaigns"
)
SPREADSHEET_ID = "1Y-H26QoUVAvRWYNfB8UUcfWSCmVIuu8sKbzn2Sasz90"
SHEET_TAB = os.getenv("SHEET_TAB", "貼り付け用")  # タブ名(変更不可とのことなので固定)
# Google認証: OAuth(自分のGoogleアカウント)を標準とし、service_account.json があればそちらを優先
OAUTH_CLIENT_FILE = Path(os.getenv("OAUTH_CLIENT_FILE", BASE / "oauth_client.json"))  # OAuthクライアントID(デスクトップ)のJSON
OAUTH_TOKEN_FILE = BASE / "auth" / "google_token.json"  # 初回ログインで自動作成(refresh token入り)
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SERVICE_ACCOUNT_FILE = Path(os.getenv("SERVICE_ACCOUNT_FILE", BASE / "service_account.json"))
STATE_FILE = BASE / "auth" / "state.json"  # ログインセッション(storage_state)
DOWNLOAD_DIR = BASE / "downloads"
TZ = ZoneInfo("Asia/Tokyo")

DATE_COLUMN = os.getenv("DATE_COLUMN", "集計日")  # CSV/シートの日付列見出し。無ければ自動検出
ARCHIVE_WORD = "アーカイブ"
STATUS_COLUMN = os.getenv("STATUS_COLUMN", "")  # 有効/無効の列見出し。空なら自動検出
STATUS_ACTIVE = "有効"  # この値の行だけを配信中として残す

# ---- 画面操作の文言 (実画面に合わせて調整してください) ----
UI_PERIOD_BUTTON = r"\d{4}\/\d{2}\/\d{2}\s*-\s*\d{4}\/\d{2}\/\d{2}"  # 期間ボタン(表示が「2026/09/26 - 2026/10/02」)
UI_DAILY_LABEL = "日別"                # DLダイアログの集計単位(期間合算/日別)
UI_DOWNLOAD = r"^ダウンロード$"        # 一覧右上のDLボタン(ダイアログを開く)
UI_CREATE = "ダウンロードファイル作成"  # ダイアログの作成ボタン
UI_FILELIST = "ダウンロードファイル一覧"  # サイドバーのリンク(作成済みファイルの一覧)
UI_FILE_DL = r"ダウンロード"            # 一覧ページ内の各ファイルのDLボタン(推測。要確認)
FILE_WAIT_SEC = 180                    # ファイル生成待ちの上限

log = logging.getLogger("mercari")


class SessionExpired(Exception):
    pass


# ============================ 1. ログイン/ダウンロード ============================
def save_login_session() -> None:
    from playwright.sync_api import sync_playwright

    STATE_FILE.parent.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(CAMPAIGNS_URL)
        input("ブラウザでログインし、キャンペーン一覧が表示されたらここで Enter を押してください > ")
        ctx.storage_state(path=str(STATE_FILE))
        STATE_FILE.chmod(0o600)
        browser.close()
    log.info("セッション保存: %s", STATE_FILE)


def dump_page(page, tag: str) -> Path:
    """画面調整用: スクショ・HTML・クリック可能要素の文言を logs/ に保存"""
    stem = BASE / "logs" / f"{tag}_{datetime.now(TZ):%Y%m%d_%H%M%S}"
    stem.parent.mkdir(exist_ok=True)
    page.screenshot(path=f"{stem}.png", full_page=True)
    Path(f"{stem}.html").write_text(page.content(), encoding="utf-8")
    items = page.evaluate(
        """() => [...document.querySelectorAll(
            'button,[role=button],[role=tab],[role=option],[role=menuitem],[role=combobox],a,label,input,select')]
          .filter(e => e.offsetParent !== null)
          .map(e => [e.tagName, e.getAttribute('role') || '', (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 60)])
          .map(x => x.join(' | '))"""
    )
    Path(f"{stem}.txt").write_text(f"URL: {page.url}\n" + "\n".join(items), encoding="utf-8")
    return Path(f"{stem}.txt")


def inspect_page() -> None:
    """保存済みセッションで開き、手動操作しながら任意の場面で画面要素を保存する(セレクタ調整用)"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(storage_state=str(STATE_FILE), accept_downloads=True, locale="ja-JP")
        page = ctx.new_page()
        page.on("download", lambda d: print(f"  [ダウンロード発生] {d.suggested_filename}"))
        page.goto(CAMPAIGNS_URL, wait_until="networkidle")
        n = 0
        while True:
            n += 1
            path = dump_page(page, f"inspect{n:02d}")
            print(f"\n=== 保存({n}): {path} ===")
            print(path.read_text(encoding="utf-8"))
            ans = input(
                "\nブラウザで次の操作を1つ行い、"
                "Enterで再保存 / 終了は q + Enter > "
            )
            if ans.strip().lower() == "q":
                break
        browser.close()


def set_period_this_month(page, today: date) -> None:
    """期間ピッカーで「当月1日 - 今日」をカレンダーのクリックで指定する。

    ピッカーは2か月分(前月|当月)のカレンダーを出す。日付ボタンの列を1に戻る所で区切り、最後のブロックを当月とみなす。
    """
    page.get_by_role("button", name=re.compile(UI_PERIOD_BUTTON)).first.click()
    days = page.locator("button").filter(has_text=re.compile(r"^\d{1,2}$"))  # 名前(aria-label)ではなく表示テキストで判定
    days.first.wait_for()
    nums = [int(t.strip()) for t in days.all_inner_texts()]
    blocks: list[list[int]] = []
    prev = 0
    for i, n in enumerate(nums):
        if n <= prev or not blocks:
            blocks.append([])
        blocks[-1].append(i)
        prev = n
    cur = blocks[-1]
    if len(cur) < today.day:
        raise RuntimeError(f"カレンダー構造が想定と違います: blocks={[len(b) for b in blocks]}")
    days.nth(cur[0]).click()
    days.nth(cur[today.day - 1]).click()
    want = f"{today:%Y/%m/01} - {today:%Y/%m/%d}"
    page.get_by_role("button", name=want).wait_for(timeout=10_000)  # 反映されたか確認


def create_and_fetch_file(page, dest: Path) -> None:
    """DLダイアログで日別を選んでファイル作成 -> ダウンロードファイル一覧から取得"""
    page.get_by_role("button", name=re.compile(UI_DOWNLOAD)).first.click()
    page.get_by_text(UI_DAILY_LABEL, exact=True).click()
    page.get_by_role("button", name=UI_CREATE).click()
    page.get_by_role("button", name=UI_CREATE).wait_for(state="hidden", timeout=30_000)

    page.get_by_role("link", name=UI_FILELIST).first.click()
    page.wait_for_load_state("networkidle")
    log.info("一覧ページ: %s", dump_page(page, "filelist"))
    deadline = time.time() + FILE_WAIT_SEC
    while time.time() < deadline:
        btn = page.get_by_role("button", name=re.compile(UI_FILE_DL))
        if btn.count():
            with page.expect_download(timeout=30_000) as dl:
                btn.first.click()
            dl.value.save_as(dest)
            return
        time.sleep(10)  # 生成待ち
        page.reload(wait_until="networkidle")
    raise TimeoutError(f"{FILE_WAIT_SEC}秒待ってもダウンロード対象が見つかりません")


def download_csv(headless: bool) -> Path:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    if not STATE_FILE.exists():
        raise SessionExpired("auth/state.json がありません。--login を先に実行してください")
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    today = datetime.now(TZ).date()
    dest = DOWNLOAD_DIR / f"campaigns_daily_{today:%Y%m}.csv"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(storage_state=str(STATE_FILE), accept_downloads=True, locale="ja-JP")
        page = ctx.new_page()
        try:
            page.goto(CAMPAIGNS_URL, wait_until="networkidle")
            if "/campaigns" not in page.url:  # ログイン画面等へ飛ばされた
                raise SessionExpired(f"セッション切れ: {page.url}")

            set_period_this_month(page, today)
            page.wait_for_load_state("networkidle")
            create_and_fetch_file(page, dest)
            # セッション更新(有効期限延長)を反映
            ctx.storage_state(path=str(STATE_FILE))
        except (PWTimeout, Exception) as e:
            if isinstance(e, SessionExpired):
                raise
            shot = dump_page(page, "error")
            raise RuntimeError(f"画面操作に失敗。画面要素一覧(.txt/.png/.html): {shot} / セレクタ文言(UI_*)を調整してください: {e}") from e
        finally:
            browser.close()
    log.info("CSV保存: %s", dest)
    return dest


# ============================ 2. CSV読込・アーカイブ行削除 ============================
def read_csv(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("CSVの文字コードを判定できません")
    return [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]


def drop_archive_rows(header: list[str], rows: list[list[str]]) -> list[list[str]]:
    """いずれかのセル値が「アーカイブ」(前後空白除く完全一致)の行を削除。

    部分一致にするとキャンペーン名に「アーカイブ」を含む行まで消えるため完全一致にしている。
    """
    kept = [r for r in rows if not any(c.strip() == ARCHIVE_WORD for c in r)]
    log.info("アーカイブ行削除: %d -> %d 行", len(rows), len(kept))
    return kept


def keep_active_rows(header: list[str], rows: list[list[str]]) -> list[list[str]]:
    """有効/無効の列で判定し、値が「有効」の行だけ残す(=配信中キャンペーン)。"""
    if STATUS_COLUMN:
        if STATUS_COLUMN not in header:
            raise ValueError(f"STATUS_COLUMN={STATUS_COLUMN!r} がCSVの見出しにありません: {header}")
        col = header.index(STATUS_COLUMN)
    else:
        cands = [
            i for i in range(len(header))
            if any(i < len(r) and r[i].strip() in (STATUS_ACTIVE, "無効") for r in rows)
        ]
        if not cands:
            raise ValueError(f"有効/無効の列を検出できません。STATUS_COLUMN を指定してください: {header}")
        hinted = [i for i in cands if re.search("ステータス|状態|有効|配信", header[i])]
        col = (hinted or cands)[0]
    log.info("有効/無効の判定列: %r", header[col])
    kept = [r for r in rows if col < len(r) and r[col].strip() == STATUS_ACTIVE]
    log.info("有効行のみ残す: %d -> %d 行", len(rows), len(kept))
    return kept


# ============================ 3. 日付の統一 ============================
_DATE_RES = [
    re.compile(r"^(\d{4})[/\-.年](\d{1,2})[/\-.月](\d{1,2})日?$"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
]


def parse_date(v: str, default_year: int | None = None) -> date | None:
    """2026/10/1, 2026-10-01, 2026年10月1日, 20261001 (+ 時刻付き) を date に。年なし(10/1)は default_year 補完。"""
    s = (v or "").strip().replace("　", " ").split(" ")[0].split("T")[0]
    for rx in _DATE_RES:
        m = rx.match(s)
        if m:
            try:
                return date(*map(int, m.groups()))
            except ValueError:
                return None
    m = re.match(r"^(\d{1,2})[/月](\d{1,2})日?$", s)
    if m and default_year:
        try:
            return date(default_year, int(m[1]), int(m[2]))
        except ValueError:
            return None
    return None


def fmt_date(d: date) -> str:
    return f"{d:%Y/%m/%d}"  # シート上の統一形式


def find_date_col(header: list[str], rows: list[list[str]], year: int) -> int:
    if DATE_COLUMN in header:
        return header.index(DATE_COLUMN)
    best, best_n = -1, 0
    for i in range(len(header)):
        n = sum(1 for r in rows[:50] if i < len(r) and parse_date(r[i], year))
        if n > best_n:
            best, best_n = i, n
    if best < 0:
        raise ValueError(f"日付列を特定できません。DATE_COLUMN を指定してください: {header}")
    return best


# ============================ 4. 当月分のみ上書きマージ ============================
def merge_current_month(
    sheet_header: list[str],
    sheet_rows: list[list[str]],
    new_header: list[str],
    new_rows: list[list[str]],
    today: date,
) -> tuple[list[str], list[list[str]]]:
    """既存(シート)の当月行を捨て、新データの当月行で置換。過去月は保持し日付昇順で返す。"""
    ym = (today.year, today.month)
    header = sheet_header or new_header
    # 新データをシートの列順へ見出し名で整列
    idx = {h: i for i, h in enumerate(new_header)}
    missing = [h for h in header if h not in idx]
    if missing:
        log.warning("CSVに無い列は空欄で出力: %s", missing)
    extra = [h for h in new_header if h not in header]
    if extra:
        log.warning("シートに無いCSV列は無視: %s", extra)

    def align(r: list[str]) -> list[str]:
        return [r[idx[h]] if h in idx and idx[h] < len(r) else "" for h in header]

    dcol = find_date_col(header, sheet_rows or [align(r) for r in new_rows], today.year)

    def normalize(rows: list[list[str]], what: str) -> list[tuple[date | None, list[str]]]:
        out = []
        for r in rows:
            r = (r + [""] * len(header))[: len(header)]
            d = parse_date(r[dcol], today.year)
            if d:
                r[dcol] = fmt_date(d)
            else:
                log.warning("%s: 日付を解釈できない行 %r", what, r[dcol])
            out.append((d, r))
        return out

    old = normalize(sheet_rows, "既存")
    new = normalize([align(r) for r in new_rows], "新規")
    new_cur = [(d, r) for d, r in new if d and (d.year, d.month) == ym]
    if not new_cur:
        raise ValueError("当月の日付を持つ新規データが0件のため中断(既存当月分を消さないため)")
    old_keep = [(d, r) for d, r in old if not (d and (d.year, d.month) == ym)]
    dated = sorted(
        [(d, r) for d, r in old_keep + new_cur if d], key=lambda x: x[0]
    )  # sortは安定: 同日は 既存→新規 の順を維持
    undated = [r for d, r in old_keep if not d]  # 日付不明の既存行は末尾に残す(消さない)
    return header, [r for _, r in dated] + undated


# ============================ 5. Google Sheets ============================
def google_login() -> None:
    """初回のみ: ブラウザで自分のGoogleアカウントにログインして許可し、トークンを保存"""
    import gspread

    if not OAUTH_CLIENT_FILE.exists():
        raise SystemExit(f"OAuthクライアントのJSONがありません: {OAUTH_CLIENT_FILE} (README参照)")
    OAUTH_TOKEN_FILE.parent.mkdir(exist_ok=True)
    OAUTH_TOKEN_FILE.unlink(missing_ok=True)  # 再ログイン時は作り直す
    gspread.oauth(
        scopes=SHEETS_SCOPES,
        credentials_filename=str(OAUTH_CLIENT_FILE),
        authorized_user_filename=str(OAUTH_TOKEN_FILE),
    )
    OAUTH_TOKEN_FILE.chmod(0o600)
    log.info("Google認証を保存: %s", OAUTH_TOKEN_FILE)


def has_google_auth() -> bool:
    return SERVICE_ACCOUNT_FILE.exists() or OAUTH_TOKEN_FILE.exists()


def get_gspread_client():
    import gspread

    if SERVICE_ACCOUNT_FILE.exists():
        return gspread.service_account(filename=str(SERVICE_ACCOUNT_FILE))
    return gspread.oauth(
        scopes=SHEETS_SCOPES,
        credentials_filename=str(OAUTH_CLIENT_FILE),
        authorized_user_filename=str(OAUTH_TOKEN_FILE),
    )


def write_to_sheet(new_header: list[str], new_rows: list[list[str]], today: date) -> None:
    import gspread

    gc = get_gspread_client()
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_TAB)
    values = ws.get_all_values()  # 表示値(文字列)で取得 -> parse_dateで統一
    sheet_header = values[0] if values else []
    sheet_rows = [r for r in values[1:] if any(c.strip() for c in r)]
    header, merged = merge_current_month(sheet_header, sheet_rows, new_header, new_rows, today)

    out = [header] + merged
    # 先にクリアせず、update後に余った末尾行だけ消す(途中失敗でデータを失いにくくする)
    ws.update(range_name="A1", values=out, value_input_option="USER_ENTERED")
    if len(values) > len(out):
        ws.batch_clear([f"A{len(out) + 1}:ZZ{len(values)}"])
    log.info("シート更新: %d 行(ヘッダ除く)", len(merged))


# ============================ main ============================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="手動ログインしてセッション保存")
    ap.add_argument("--inspect", action="store_true", help="画面要素を保存(セレクタ調整用)")
    ap.add_argument("--google-login", action="store_true", help="Googleアカウントで認証(初回のみ)")
    ap.add_argument("--headed", action="store_true", help="ブラウザを表示して実行")
    ap.add_argument("--csv", type=Path, help="DLせずこのCSVを使う")
    ap.add_argument("--dry-run", action="store_true", help="シートへ書き込まない")
    args = ap.parse_args()
    (BASE / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.login:
        save_login_session()
        return 0
    if args.google_login:
        google_login()
        return 0
    if args.inspect:
        inspect_page()
        return 0
    if not args.dry_run and not has_google_auth():
        log.error("Google認証がありません。`python mercari_ads_export.py --google-login` を先に実行してください")
        return 3
    try:
        path = args.csv or download_csv(headless=not args.headed)
    except SessionExpired as e:
        log.error("%s -> `python mercari_ads_export.py --login` で再ログインしてください", e)
        return 2

    rows = read_csv(path)
    if len(rows) < 2:
        log.error("CSVにデータ行がありません")
        return 1
    header = rows[0]
    body = keep_active_rows(header, drop_archive_rows(header, rows[1:]))
    today = datetime.now(TZ).date()
    if args.dry_run:
        h, m = merge_current_month(header, [], header, body, today)
        log.info("dry-run: %d 行 / 先頭: %s", len(m), m[:2])
        return 0
    try:
        write_to_sheet(header, body, today)
    except Exception as e:
        from google.auth.exceptions import RefreshError

        if isinstance(e, RefreshError):
            log.error("Google認証の期限切れ/失効: %s -> `--google-login` で再認証してください", e)
            return 4
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
