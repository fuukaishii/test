# メルカリ広告 日次データ -> Googleスプシ

## セットアップ
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```
1. Google Cloud でサービスアカウントを作成 → Sheets API を有効化 → JSONキーを `service_account.json` として配置
2. 対象スプシを **サービスアカウントのメールアドレスに「編集者」で共有**
3. 初回ログイン(手動・2FA可): `python mercari_ads_export.py --login`
   (GUIが無いサーバーでは手元PCで実行して `auth/state.json` をサーバーへコピー)
4. (画面文言の調整が必要なとき) `python mercari_ads_export.py --inspect` → logs/ の inspect_*.txt/.png を確認
5. 動作確認: `python mercari_ads_export.py --headed --dry-run` → 問題なければ `python mercari_ads_export.py`

## cron (毎日 8:10 JST)
`crontab -e`
```cron
CRON_TZ=Asia/Tokyo
10 8 * * * cd /path/to/test && /usr/bin/flock -n /tmp/mercari_ads.lock .venv/bin/python mercari_ads_export.py >> logs/cron.log 2>&1
```
- `flock` で二重起動防止、ログは `logs/cron.log`
- 終了コード 2 = ログインセッション切れ → `--login` で再取得
- 失敗通知が必要なら末尾に `|| curl -X POST <Slack Webhook> -d '{"text":"mercari export failed"}'` 等を追加

## 仕様メモ
- アーカイブ行: いずれかのセルが「アーカイブ」と完全一致する行を削除
- 日付: `2026/10/1`・`2026-10-01`・`2026年10月1日` 等を `YYYY/MM/DD` に統一して比較
- 書き込み: シートの当月行を破棄 → 今回の当月行で置換 → 過去月は保持し日付昇順。当月データ0件なら中断(既存を消さない)
- シート列順を優先し、CSVの列は見出し名で整列
- タブ名は `SHEET_TAB`(既定「貼り付け用」)、日付列見出しは `DATE_COLUMN`(既定「集計日」)で環境変数から変更可
