# recdyud

地上波デジタルチューナー **DY-UD200** を [mirakc](https://github.com/mirakc/mirakc) から使うための、
[recpt1](https://github.com/stz2012/recpt1) 互換のチューナーコマンドです。

- 指定チャンネルを受信し、フルセグの TS を出力します。
- チューナー内蔵のカードスロットに挿した B-CAS カードでスクランブルを解除します（mirakc 側の `decode-filter` は不要です）。
- アンテナ調整用に、ロック状態・信号レベル・TEI・CC エラーをリアルタイム表示する診断コマンド `recdyud-diag` があります。
- `recdyud` を組み込んだ mirakc のカスタムイメージを用意しているので、[KonomiTV](https://github.com/tsukumijima/KonomiTV) などのクライアントから利用できます。

## 必要なもの

- Linux (x86_64 / aarch64)
- [uv](https://docs.astral.sh/uv/)（CPython 3.14 は uv が取得します）
- C/C++ コンパイラと CMake 3.20 以上（ネイティブ部分のビルド用）
- Docker（mirakc イメージを使う場合）

C/C++ ライブラリはシステムのものを使わず、git submodule でベンダリングしたソースからビルドします。

| submodule | 用途 | ライセンス |
| --- | --- | --- |
| `vendor/libaribb25` ([nanamitm/libaribb25](https://github.com/nanamitm/libaribb25)) | ARIB STD-B25 デスクランブラ (MULTI2) | Apache-2.0 |
| `vendor/libusb-cmake` ([libusb/libusb-cmake](https://github.com/libusb/libusb-cmake)) | PyUSB のバックエンド (libusb-1.0) | LGPL-2.1 |

## セットアップ

```sh
git clone --recursive <this repository> recdyud
cd recdyud
# clone 時に --recursive を付けなかった場合
git submodule update --init --recursive

uv sync            # 依存関係の取得とネイティブライブラリのビルド
```

一般ユーザーでチューナーにアクセスできるよう、udev ルールをインストールします（`video` グループとログイン中のユーザーに権限を付与し、
TS の欠けの原因になる USB のオートサスペンドを止めます）。

```sh
sudo install -m 644 udev/60-dy-ud200.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger --action=add --subsystem-match=usb
```

オートサスペンドの設定はデバイスの追加時にだけ適用されるので、`--action=add` を付けてください（またはチューナーを挿し直してください）。

`bin/recdyud` と `bin/recdyud-diag` は `uv run` を呼び出すラッパースクリプトです。
`PATH` の通ったディレクトリにシンボリックリンクを置くと、どこからでも実行できます。

```sh
ln -s "$PWD/bin/recdyud" "$PWD/bin/recdyud-diag" ~/.local/bin/
```

## 使い方

### recdyud

```
recdyud [options] CHANNEL RECTIME DESTFILE
```

| 引数 | 説明 |
| --- | --- |
| `CHANNEL` | `13`〜`62`（UHF）、`C13`〜`C63`（CATV パススルー）、`1`〜`12`（VHF）、または `473143kHz` のような周波数 |
| `RECTIME` | 録画秒数。`-` なら終了されるまで |
| `DESTFILE` | 出力ファイル。`-` なら標準出力 |

```sh
# 27ch を 30 秒録画
recdyud 27 30 nhk.ts

# 標準出力へ流し続ける（mirakc と同じ使い方）
recdyud 27 - - | ffplay -
```

主なオプション:

| オプション | 説明 |
| --- | --- |
| `--b25` / `--no-b25` | B-CAS によるスクランブル解除の有無（既定は解除する。`--b25` は recpt1 互換のため受け付けます） |
| `--round N`, `--strip`, `--EMM` | recpt1 / libaribb25 と同じ意味 |
| `--b25-timeout SEC` | この秒数たってもスクランブル解除が始まらない（受信不良などで PAT・PMT・ECM が届かない）場合、スクランブルされたまま出力する（既定 5 秒、0 で libaribb25 の既定どおり 16〜32 MiB 待つ） |
| `--device DEV` | 使うチューナー。`auto`（空いている最初のもの、既定）、番号 `0`、`BUS:ADDR`、`/dev/bus/usb/BBB/AAA`、ポートパス `5-1.2`。環境変数 `RECDYUD_DEVICE` でも指定可 |
| `--lock-timeout SEC` | この秒数以内にロックしなければ終了（既定 10 秒、終了コード 2） |
| `--keep-null` | ヌルパケット (PID 0x1FFF) を残す。DY-UD200 は多重フレームのヌル TSP ごと約 30 Mbps で出力するため、既定では BonDriver_dyud と同じく捨てます（残すとサイズが約 2 倍になります） |
| `-v` / `-q` | ログを詳細に / 警告以上のみに |

ログは標準エラー出力に出ます。B-CAS カードを初期化できない場合やデスクランブラでエラーが起きた場合は、
recpt1 と同様にスクランブルされたままの TS を出力し続けます。

### recdyud-diag

```sh
recdyud-diag list                 # 接続されているチューナーと使用状況
recdyud-diag info                 # ファームウェア、シリアル番号、B-CAS カードの状態
recdyud-diag monitor 27           # アンテナ調整用のリアルタイム表示（Ctrl+C で終了）
recdyud-diag scan --mirakc        # チャンネルスキャンと mirakc 用 channels の出力
```

`monitor` の表示例（受信状態が不十分な例）:

```
11:38:03  lock   9  signal   8.688   30.21 Mbps  TEI  97.97% (11231, total 40995)  CC    0 (total 0)  scrambled   0.0%  [NG: full-seg layer lost]
# TSID 0x7e91 ＮＨＫＥテレ青森 / remote key 2: 22536 NHKEテレ1青森, 22537 NHKEテレ2青森, 22538 NHKEテレ3青森, 22920 NHK携帯2
```

- `lock`: チューナーのロック状態値。8 を超えるとロック（BonDriver_dyud と同じ判定）
- `signal`: チューナーが返す信号レベル（BonDriver_dyud の `GetSignalLevel()` と同じ値）。単位は公開されていないので、
  向きを変えたときの相対的な比較に使ってください（C/N [dB] に近い値のようで、開発時の環境では 9〜14 程度のときワンセグ層しか復調できませんでした）
- `TEI`: ヌルパケットを除いたパケットのうち、誤り訂正できなかった (transport_error_indicator) パケットの割合と数
- `CC`: 連続性カウンタのエラー数
- 末尾の判定: `OK` / `NG: no lock` / `NG: full-seg layer lost`（TEI が 90% 以上。ワンセグ層だけ受信できている状態）/ `NG: TEI` / `NG: CC`
- `--json` を付けると 1 行 1 JSON で出力します

アンテナ調整では、`signal` が大きく、`TEI` と `CC` が 0 のまま安定する（判定が `OK` になる）向きを探してください。
フルセグ層が受信できないと PAT・PMT・ECM も届かないため、`recdyud` はスクランブル解除を開始できず、
`--b25-timeout`（既定 5 秒）の経過後にスクランブルされたままの出力に切り替わります。

`scan` は `--channels 13-62,C13-C63` のように範囲を指定できます。`--all` でロックしなかったチャンネルも表示し、
`--json` で詳細な結果を出力します。SDT/NIT はワンセグ層でも届くので、受信品質が悪くても放送局名は表示されます。
その場合は `TEI` の割合を確認してください。

## mirakc イメージ

`docker/` に mirakc の公式イメージ (`mirakc/mirakc:debian`) をベースにしたイメージと compose ファイルがあります。

```sh
git submodule update --init --recursive
# docker/config.yml の channels を地域に合わせて編集してから
docker compose -f docker/compose.yaml up -d --build
```

受信できるチャンネルは、コンテナ内でスキャンして `docker/config.yml` の `channels` に書きます
（mirakc がチューナーを使っていない時に実行してください）。

```sh
docker compose -f docker/compose.yaml run --rm --entrypoint recdyud-diag mirakc scan --mirakc
```

チューナーの設定は次のとおりです。DY-UD200 を複数使う場合は台数分のエントリーを書きます。

```yaml
tuners:
  - name: dy-ud200
    types: [GR]
    command: recdyud {{{channel}}} - -
```

コンテナは `/dev/bus/usb` をマウントし、USB デバイス（メジャー番号 189）へのアクセスを許可しているので、チューナーを挿し直しても再起動は不要です。

### KonomiTV から使う

KonomiTV の設定で、バックエンドに **mirakc** を選び、Mirakurun (mirakc) の URL に `http://<ホスト>:40772/` を指定します。

## 仕組み

```
DY-UD200 ──USB──> PyUSB (vendored libusb)
  EP 0x02/0x84: 制御コマンド (AES 暗号化 + CRC32)  ─┐
  EP 0x86     : TS                                  │
                                                     ▼
  TS ─> パケット同期 ─> libaribb25 (MULTI2) ─> stdout
                           │ ECM
                           ▼
          B-CAS カード（チューナー内蔵スロット。制御コマンドで T=1 ブロックを送る）
```

- USB プロトコルと初期化手順は、Windows 用ドライバ BonDriver_dyud のソースを参考にしています
  （制御コマンドは AES-ECB で暗号化され、CRC-32 が付きます）。
- libaribb25 の B-CAS 実装は PC/SC 前提なので、PC/SC に依存する `b_cas_card.c` はビルドせず、
  `native/dyud_b25.c` の小さなシムで `B_CAS_CARD` インターフェースを実装し、ECM 処理を Python 側（`recdyud.bcas`）に委ねています。
- MULTI2 の復号は libaribb25 の C++ 実装（SIMD 対応）をそのまま使います。Python 側の処理はチャンクごとの NumPy 演算（同期確認・統計）だけなので、フルセグでも CPU 負荷は小さいです。
- mirakc はチューナーコマンドを SIGKILL で止めるため、起動時に前回のプロセスが残した応答や TS を読み捨ててから初期化します。
- 元のドライバにあるファームウェア書き換え機能は、危険なので実装していません。

TS の解析（TEI・CC・PSI/SI）は NumPy と小さな自前パーサで行っています。
[TSDuck](https://github.com/tsduck/tsduck) は必要な機能に対してソースからのビルドが重いため、組み込んでいません。
詳しく解析したい場合は、出力を `tsp` や `tsanalyze` に渡してください（例: `recdyud --no-b25 27 10 - | tsanalyze`）。

## 開発

```sh
uv run pytest
```

テストでは、合成したスクランブル TS（Python で実装した MULTI2 暗号化）を libaribb25 で復号し、元に戻ることを確認しています。

ネイティブ部分だけをビルドする場合:

```sh
cmake -S . -B build && cmake --build build && cmake --install build --prefix build/install
RECDYUD_NATIVE_DIR=build/install/recdyud/_native uv run recdyud --help
```

## ライセンス

recdyud は MIT ライセンスです（[LICENSE](LICENSE)）。
submodule のライブラリにはそれぞれのライセンスが適用されます（libaribb25: Apache-2.0、libusb: LGPL-2.1）。
