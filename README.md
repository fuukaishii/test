# メルカリ広告 日次データ -> Googleスプシ

## セットアップ
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```
1. Google認証(自分のGoogleアカウントでOAuth。組織ポリシーでサービスアカウントキーが作れない場合はこちら)
   1. Google Cloud Console でプロジェクトを選び、「APIとサービス」→「ライブラリ」で **Google Sheets API** を有効化
   2. 「APIとサービス」→「OAuth同意画面」(Google Auth Platform): ユーザーの種類は組織内なら **内部**(トークンが失効しない)。外部の場合は公開ステータスを「本番環境」にしないと **7日でトークンが切れる**
   3. 「認証情報」→「認証情報を作成」→「OAuthクライアントID」→ アプリの種類 **デスクトップアプリ** → JSONをダウンロードし `oauth_client.json` として本フォルダに置く
   4. `python mercari_ads_export.py --google-login` → ブラウザでログインして許可(対象スプシの編集権限を持つアカウントで)。`auth/google_token.json` が作られる
   - (サービスアカウントキーが使える環境なら、`service_account.json` を置いてスプシをそのメールに共有すればそちらが優先される)
2. 対象スプシに、ログインするGoogleアカウントの編集権限があることを確認
3. 初回ログイン(手動・2FA可): `python mercari_ads_export.py --login`
   (GUIが無いサーバーでは手元PCで実行して `auth/state.json` をサーバーへコピー)
4. (画面文言の調整が必要なとき) `python mercari_ads_export.py --inspect` → logs/ の inspect_*.txt/.png を確認
5. 動作確認: `python mercari_ads_export.py --headed --dry-run`(キャンペーン数+1個のダウンロードファイルが作られ、数分かかる) → 問題なければ `python mercari_ads_export.py`

## cron (毎日 8:10 JST)
`crontab -e`
```cron
CRON_TZ=Asia/Tokyo
10 8 * * * cd /path/to/test && /usr/bin/flock -n /tmp/mercari_ads.lock .venv/bin/python mercari_ads_export.py >> logs/cron.log 2>&1
```
- `flock` で二重起動防止、ログは `logs/cron.log`
- 終了コード 2 = ログインセッション切れ → `--login` で再取得
- 終了コード 3 = Google認証が未実施(`--google-login`)、4 = Google認証の失効(再度 `--google-login`)
- 失敗通知が必要なら末尾に `|| curl -X POST <Slack Webhook> -d '{"text":"mercari export failed"}'` 等を追加

## Windows(タスクスケジューラ)で毎日実行
`run_daily.bat` を使う。コマンドプロンプトで(本フォルダで):
```
schtasks /create /tn "MercariAdsExport" /tr "\"%CD%\run_daily.bat\"" /sc daily /st 08:10
```
- 実行ログは `logs\task.log`(末尾の `exit code: 0` が成功。2=メルカリ再ログイン、3/4=Google再認証)
- 試運転: `schtasks /run /tn "MercariAdsExport"`、削除: `schtasks /delete /tn "MercariAdsExport" /f`
- PCの電源が入りログオン中であること。取り逃し対策はタスクスケジューラGUIの「設定」で「スケジュールされた時刻にタスクを開始できなかった場合、すぐにタスクを実行する」をON

## 仕様メモ
- アーカイブ行: いずれかのセルが「アーカイブ」と完全一致する行を削除
- 取得単位: シートが広告グループ単位のため、キャンペーン一覧CSVで配信中(有効)キャンペーンを特定 → 各キャンペーンの広告グループ画面から日別CSVを取得して結合(広告グループ自体の有効/無効は問わず、アーカイブ行のみ削除)。1件でも失敗したら書き込まず中断
- 配信中の判定: CSVの有効/無効列(自動検出、`STATUS_COLUMN` で指定可)が「有効」の行だけ残す。画面側の絞り込みはしない
- 日付: `2026/10/1`・`2026-10-01`・`2026年10月1日` 等を `YYYY/MM/DD` に統一して比較
- 書き込み: シートの当月行を破棄 → 今回の当月行で置換 → 過去月は保持し日付昇順。当月データ0件なら中断(既存を消さない)
- シート列順を優先し、CSVの列は見出し名で整列
- タブ名は `SHEET_TAB`(既定「貼り付け用（タブ名変更不可）」)、日付列見出しは `DATE_COLUMN`(既定「集計日」)で環境変数から変更可
