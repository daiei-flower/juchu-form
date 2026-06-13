# 受注書入力フォーム — クラウド公開ガイド

責任者（別拠点）がブラウザで受注書を入力できるよう、この `webform` をクラウドに公開します。
**このアプリはNotionの顧客DBにしか書き込みません。** 経営数字（損益・ダッシュボード・元帳）は
あなたのMacのローカルに残り、公開されません。

## 必要なもの
- Notion連携トークン（本体アプリのマスタ設定と同じもの）
- 入力用の共通パスワード（あなたが決める。責任者に共有）
- 無料で使えるクラウド（おすすめ: **Render**）

## 環境変数（クラウドに登録する設定）
| 変数 | 値 | 秘匿 |
|---|---|---|
| `NOTION_TOKEN` | Notion連携トークン（`ntn_…`） | ◎ 秘密 |
| `FORM_PASSWORD` | 入力用パスワード（任意の文字列） | ◎ 秘密 |
| `FORM_USER` | ユーザー名（既定 `staff` のままでOK） | — |

DBのID（部門ごとの受注書／売上明細）は `sources.json` に入っており、秘密ではありません。

## Renderで公開する手順（GitHub経由）
1. この `webform` フォルダを GitHub のリポジトリに置く（私が用意できます）。
2. https://render.com にサインアップ → **New +** → **Blueprint** を選び、そのリポジトリを指定。
   - `render.yaml` を自動認識します（Dockerで起動）。
3. 初回デプロイ時に環境変数を聞かれるので **`NOTION_TOKEN`** と **`FORM_PASSWORD`** を入力。
4. 数分でURL（例 `https://juchu-form.onrender.com`）が発行されます。
5. そのURL＋ユーザー名 `staff`＋パスワードを各責任者に共有。スマホでも入力できます。

> 無料プランは一定時間アクセスがないと休止し、次回アクセスの初回だけ起動に約30秒かかります（その後は快適）。常時すぐ使いたい場合は有料プラン（月数百円〜）に変更できます。

## ローカルで試す（任意）
```
cd webform
NOTION_TOKEN=ntn_xxx FORM_PASSWORD=test ../.venv/bin/python -m uvicorn app:app --port 8790
# ブラウザで http://127.0.0.1:8790/ （ユーザー staff / パスワード test）
```

## 運用の流れ
1. 責任者：このフォームで受注書を入力 → Notionに保存
2. 経営者：本体アプリ（ローカル）の「Notionから取込」で集計・受注書印刷

## メンテナンス
- 部門やDBを増やした場合は、本体アプリで `sources.json` を作り直して再デプロイ：
  ```
  ./.venv/bin/python -c "import json;from app import db,notion_client as nc;c=db.get_connection();print(json.dumps({'departments':{d:{'order_header':nc._src(c,d,'order_header'),'order_line':nc._src(c,d,'order_line')} for d in nc.order_departments(c)}},ensure_ascii=False,indent=2))" > webform/sources.json
  ```
- トークンを再生成したら、Renderの環境変数 `NOTION_TOKEN` も更新。
