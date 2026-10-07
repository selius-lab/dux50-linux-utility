# dux50 — ELECOM M-DUX50 / M-DUX30 設定ツール for Linux

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)

ELECOM M-DUX50 (M-DUX50BK) MMO ゲーミングマウスの本体設定
（ボタン割当・DPI・マクロ・プロファイル等）を、Linux から**読み書き**できるように
した CLI ツールです。対象 VID:PID は `056e:00e7`〜`00ea`（M-DUX50 は `056e:00e9`）。


## 現状（実機 056e:00e9 で検証済み）

| 機能 | 状態 |
|---|---|
| デバイス検出 / 情報取得 | ✅ 動作 |
| 設定メモリ読み出し (61B/回, 4KB を約0.5秒) | ✅ 実機検証済み |
| バックアップ (ファイル保存) | ✅ 実機検証済み |
| **任意アドレスへの書き込み** | ✅ **実機検証済み** (1B/4B/8B で確認) |
| **復元 (4096B 全体の書き戻し → 読み戻し検証)** | ✅ **実機検証済み** |
| 構造ダンプ / マクロ名抽出 | ✅ 動作 |
| **ボタン割当の読み書き** | ✅ **実機検証済み** (G1→'z' で確認) |
| **DPI の読み書き (5段) + 段切替** | ✅ **実機検証済み** (速度変化を確認) |
| **レポートレート (125/250/500/1000Hz)** | ✅ **実機検証済み** (入力レートを実測) |
| その他の個別設定 (0x0d/0x0f のビットフィールド) | 🚧 未マッピング（backup/restore で保存・復元は可能） |

プロトコル（Feature 長 64B + usage page 0xFF18）:

```
読み出し: report = {01, 00, 00, ADDR_LO, ADDR_HI}       -> resp[2..62] に 61B
書き込み: report = {01, 03, ADDR_LO, ADDR_HI, LEN, data…}
適用:     report = {01, 04, 00, 00, 00, 00, 00, 00}
DPI適用:  report = {01, 07, 01, X, Y}   # 現在の段に即時反映
DPI段切替: report = {01, 07, 00, STAGE}
report[63] = XOR(report[1..62])
```

詳細は `docs/PROTOCOL.md` を参照してください。

### 書き込みができる条件

**新品・未設定の個体では、書き込みが無視される可能性があります。**

当初は書き込みコマンドを送っても一切反映されませんでした。原因はコマンドではなく
**デバイスの状態**でした。デバイス側で一度設定変更（＝`{01,04,…}` を含む
セッション）が行われると、その後は書き込みが受け付けられるようになります。

マウスを挿し直したあとに `dux50.py write` で確認してください。

## ファイル

| ファイル | 内容 |
|---|---|
| `dux50.py` | 本体 CLI ツール（設定の読み書き） |
| `webui.py` | ブラウザから操作できる Web UI |
| `docs/PROTOCOL.md` | プロトコル（バイト列）仕様 |
| `tools/measure-rate.py` | マウスの入力レートを実測する（レポートレート検証用） |
| `tests/test_protocol.py` | ハード不要のユニットテスト |
| `LICENSE` | MIT ライセンス |

## 使い方

このデバイスは hidraw 経由だと Feature レポートが正しく転送されない
（カーネル 6.12 で確認）ため、usbfs を使う関係で **root 権限**が必要です。
実行中はベンダーインターフェースから `usbhid` を一時的に外し、終了時に自動で
再バインドします（ポインタ操作には影響しません）。

```sh
sudo python3 dux50.py info                 # デバイス情報と設定名一覧
sudo python3 dux50.py dump                 # 設定メモリの構造をダンプ表示
sudo python3 dux50.py backup               # 設定を丸ごと保存 (4096B)
sudo python3 dux50.py restore FILE --yes   # 保存した設定を書き戻す（要 backup）
sudo python3 dux50.py verify FILE          # 現在の設定とファイルを比較
sudo python3 dux50.py read 0x40 0x20       # 任意アドレスを読み出し
sudo python3 dux50.py write 0x110 deadbeef --yes   # 任意バイトを書き込み
sudo python3 dux50.py buttons              # ボタン割当を一覧
sudo python3 dux50.py button 3 --key z --yes        # G1 を 'z' キーに
sudo python3 dux50.py dpi                  # 5段の DPI を一覧
sudo python3 dux50.py set-dpi 0 1600 --yes # stage 0 を 1600dpi に（即反映）
sudo python3 dux50.py use-dpi 1            # 段1 を選択（保存値に切り替え）
sudo python3 dux50.py report-rate          # レポートレートを表示
sudo python3 dux50.py set-report-rate 500 --yes   # 500Hz に（即反映）
sudo python3 tools/measure-rate.py 10      # 入力レートを実測（マウスを動かす）
python3 -m unittest discover -s tests      # ユニットテスト
```

`restore` / `write` は実行前に自動でバックアップを取り、書き込み後は必ず読み戻して
検証します。反映されなければエラー終了します。

## Web UI

ブラウザから操作したい場合は `webui.py` を使います。CLI と同じ操作
（情報表示・ボタン割当・DPI・レポートレート・バックアップ/復元・生アドレス読み書き）を
GUI で行えます。標準ライブラリのみで動作します。

```sh
sudo python3 webui.py                        # http://127.0.0.1:8765/ を開く
sudo python3 webui.py --port 9000 --no-browser
```

- デバイスアクセスに root 権限が必要なため、`sudo` で起動してください。
- `127.0.0.1` にのみバインドするので、外部からはアクセスできません。
- 書き込みの前には `backups/` に自動でバックアップを保存します。

## テスト

プロトコルのフレーミング（読み書きのチャンク長・16bit アドレス指定）や DPI 換算、
レポートレート値などを、**ハードウェア不要のユニットテスト**で検証しています。
push 時に GitHub Actions でも実行されます（`.github/workflows/tests.yml`）。

```sh
python3 -m unittest discover -s tests
```

## 設定メモリの構造

- `0x0000–0x0fff` が設定本体。同内容が `0x2000, 0x4000, … 0xe000` に 8 個複製
- `0x0020–0x025f`: 5 個のプロファイルレコード (0x80B 間隔)
- `0x0040, 0x0058, 0x0070, 0x0088`: 24B 間隔の設定レコード（4B ヘッダ + 12B 表×2）
- `0x03–0x0c`: DPI 5段 (X,Y の2バイト×5)。**dpi = 値 × 50**（例 `0x18`=1200dpi）
- `0x02a0`: 値テーブル（差分符号化） / `0x0400+`: マクロ（UTF-16 名 + バイトコード）

DPI は設定バイトの書き込みに加えて、実機では次の2コマンドで即時反映します:

- `{01 07 00 段}` … 段を切り替え（**保存値が読み込まれる**ので速度が変わる／`use-dpi`）
- `{01 07 01 X Y}` … **現在の段**に値を上書き（`set-dpi` が自動送信）

詳細は `docs/PROTOCOL.md` を参照。

## ボタン一覧

`buttons` / `button` が使うインデックスと実機ボタンの対応（実機で確認済み）:

| idx | ボタン | idx | ボタン |
|---|---|---|---|
| 0 | 左ボタン | 8 | チルト左 |
| 1 | 右ボタン | 9 | G2（プロファイル切替） |
| 2 | ホイールクリック | 10 | G6 |
| 3 | G1 | 11 | G7 |
| 4 | G3 | 12 | G8 |
| 5 | G4 | 13 | G9 |
| 6 | G5 | 14 | G10 |
| 7 | チルト右 | 15 | G11 |

## 注意

- ポインタ・クリック・スクロールなど通常動作は標準 HID なので Linux でも
  そのまま動きます。このツールが扱うのは **本体に保存された設定**
  （ボタン割当・DPI・マクロ・プロファイル等）です。
- 書き込み前に必ず `backup` を取ってください。`restore` で元に戻せます。

## ライセンス

MIT License — [`LICENSE`](LICENSE) を参照してください。
