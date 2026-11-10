# youtube-sequence-generator

カット済みMP4フォルダを渡すと、Premiere Pro用のシーケンスXML・字幕SRT・インサート画像・効果音配置を自動生成するスクリプト一式です。

## 生成されるファイル

```
output/
├── captions.srt          # テロップ用字幕（Premiere ProにImport）
├── captions_asr.srt      # 修正前の字幕（音声認識そのまま。比較・差し戻し用）
├── caption_review.md     # 字幕の確認リスト（現状 → 修正 → 理由）
├── caption_review.json   # 修正箇所のマーカー（place_markers.jsx で読み込む）
├── sequence.xml          # FCP7 XML（Premiere ProにImport）
├── audio_report.md       # 声・効果音・BGMの音量の設定結果
├── bgm_ducked.wav        # 会話中に自動で下げたBGM（--bgm 指定時。A3に配置）
├── sfx_manifest.json     # 効果音タイミング（参照用）
├── segments.json         # 文字起こし結果（--skip-whisper で再利用）
├── analysis_debug.json   # Geminiの分析結果（--reuse-analysis・place_markers.jsx・add_transitions.jsx で使用）
├── report.txt            # ハイライト区間・サムネイル案
└── inserts/
    ├── insert_001.png
    ├── insert_002.png
    └── ...
```

## 必要なもの

### ツール

```bash
# Homebrew でインストール
brew install whisper-cpp ffmpeg

# Whisper モデルをダウンロード（初回のみ）
curl -L https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin \
  -o ~/ggml-large-v3.bin
```

### Python パッケージ

```bash
pip install google-genai lxml whisperx
```

必要に応じて追加:

```bash
pip install janome                # 字幕を文節で分ける精度が上がる（なくても動作）
pip install opencv-python numpy   # auto_cut.py の動き検出（なくても動作。--no-motion と同じ扱い）
```

> WhisperX が標準の文字起こし経路です。未インストールや失敗時は自動で whisper-cli にフォールバックします。`--fast` を付けると最初から whisper-cli を使います。

### Gemini API キー（任意）

`~/.zshrc` に追加してください。APIキーがなくてもWhisper文字起こし→SRT生成だけで動作します。

```bash
export GEMINI_API_KEY="自分のAPIキーを入力"
```

Gemini APIキーは [Google AI Studio](https://aistudio.google.com) で取得できます。

## ファイル構成

```
youtube-sequence-generator/
├── scripts/
│   ├── create_youtube_sequence.py     # メインスクリプト（WhisperX対応）
│   ├── auto_cut.py                    # 未カット素材の自動前処理
│   ├── apply_zoom.jsx                 # Premiere用：ズーム一括適用（V2→調整レイヤー+スケール115%）
│   ├── add_transitions.jsx            # Premiere用：analysis_debug.json の場面転換にトランジションを適用
│   ├── place_markers.jsx              # Premiere用：JSONからシーケンスマーカーを配置
│   ├── export_markers.jsx             # Premiere用：シーケンスマーカーをJSONに書き出し
│   ├── delete_markers.jsx             # Premiere用：シーケンスマーカーを一括削除
│   ├── clip_alignment.jsx             # Premiere用：テロップ間ギャップ自動修正
│   └── legacy/
│       ├── convert_v2_to_adjustment.jsx
│       └── set_v2_scale.jsx
├── configs/
│   ├── default.json                   # auto_cut.py デフォルト設定
│   └── a_channel.json                 # Aチャンネル用設定（--config で指定）
├── templates/
│   └── base_sequence.xml              # XMLテンプレート（スクリプトが自動参照）
└── LICENSE                            # MIT License
```

> **重要**: `scripts/` と `templates/` の位置関係を変えないでください。スクリプトが相対パスで `templates/base_sequence.xml` を参照しています。

## 使い方

### 基本実行

```bash
python3 scripts/create_youtube_sequence.py \
  --clips /path/to/clips/
```

### 効果音・インサート画像あり（フル機能）

```bash
GEMINI_MAX_IMAGE_COST_USD=2.0 python3 scripts/create_youtube_sequence.py \
  --clips /path/to/clips/ \
  --sfx-dir /path/to/sfx/ \
  --insert-target "日本人女性20〜40代向け"
```

### オプション一覧

| オプション | 説明 |
|---|---|
| `--clips` | カット済みMP4/MOV/MXFフォルダのパス（必須） |
| `--output` | 出力先フォルダ（デフォルト: `--clips/output/`） |
| `--sfx-dir` | 効果音フォルダのパス（省略時はA2トラックなし） |
| `--insert-target` | インサート画像の生成指示（Gemini向けプロンプト） |
| `--no-insert` | インサート画像の生成をスキップ |
| `--size` | シーケンスのサイズ。`fhd`（1920×1080・デフォルト） / `vertical`（1080×1920） / `square`（1080×1080） / `4k` / `vertical-4k` / `幅x高さ`（例: `1080x1350`） |
| `--fps` | シーケンスのfps（例: `23.976` / `29.97` / `30` / `59.94`）。省略時は最初のクリップのfps |
| `--fit` | 素材とシーケンスのサイズが違う時の拡大方法。`fill`=画面いっぱい（はみ出しは切れる・デフォルト） / `fit`=全体が入る / `none`=100%のまま |
| `--max-chars` | 字幕1枚の最大文字数（デフォルト: 横21 / 縦・正方形14） |
| `--bgm` | BGMファイル。ループして全尺に敷き、会話中は自動で下げた `bgm_ducked.wav` をA3に配置 |
| `--voice-db` / `--sfx-db` | 声・効果音のピーク目安（デフォルト -7.0 / -16.5 dB） |
| `--bgm-db` / `--bgm-duck-db` | BGMのピーク目安。会話なし / 会話中（デフォルト -24.5 / -28.5 dB） |
| `--no-levels` | 声・効果音の音量をXMLに入れない（元の音量のまま） |
| `--terms` | 用語リスト（1行1語・UTF-8）。音声認識と字幕の修正に使う。省略時は `--clips` フォルダの `terms.txt` があれば自動で使う（下の「用語リスト」参照） |
| `--caption-fix` | 字幕の修正方法。`apply`=Geminiの修正を反映して一覧に残す（デフォルト） / `propose`=字幕は音声認識のまま、修正案だけ一覧に出す / `off`=修正しない |
| `--fast` | WhisperXを使わず、最初から whisper-cli で文字起こし（速いが字幕タイミングは文字数からの推定） |
| `--model` | whisper.cppモデルのパス（デフォルト: `~/ggml-large-v3.bin`。WhisperXのフォールバック時にも使用） |
| `--experimental-gemini` | [実験的] Gemini APIで文字起こし（5分以下の短尺推奨。長尺は不安定） |
| `--skip-whisper` | 既存の `segments.json` を再利用して文字起こしをスキップ |
| `--reuse-analysis` | 既存の `analysis_debug.json` を再利用して画像・XMLのみ再生成（字幕は作り直さない） |
| `--only sfx,srt,...` | 指定ステップのみ実行（sfx/zoom/insert/srt） |
| `--skip sfx,srt,...` | 指定ステップをスキップ |

> `--use-whisperx`（WhisperXがデフォルトになったため不要）と `--use-gemini-transcribe`（`--experimental-gemini` に変更）は後方互換のために残している非推奨オプションです。

### 再実行フラグの使い分け

| 状況 | 使うフラグ |
|---|---|
| 初回・新しい動画 | なし |
| 効果音ルールや分析プロンプトを変えて再テスト | `--skip-whisper` |
| 画像プロンプトは変えずに画像だけ再生成 | `--reuse-analysis` |

### 字幕の修正と確認リスト

Geminiが直すのは、音声認識の誤り（同音異義語の取り違え・固有名詞・明らかな誤字脱字）だけです。話し言葉の言い回しや方言は直しません。

直した箇所はすべて `caption_review.md` に「現状 → 修正 → 理由」の形で残ります。修正前の字幕は `captions_asr.srt` に残るので、戻したい箇所はそこから差し戻せます。

- 修正を字幕に入れず、案だけ見て自分で直したい場合は `--caption-fix propose`
- `caption_review.json` を `place_markers.jsx` で読み込むと、修正箇所にシーケンスマーカー（白）が付きます

### 用語リスト（固有名詞・専門用語）

商品名・専門用語・人名など、音声認識が毎回間違える言葉は、正しい表記を用語リストに書いておくと直りやすくなります。

```text
# 1行に1語。空行と # で始まる行は無視されます
御影石
墓じまい
```

- `--terms /path/to/terms.txt` で指定します。指定しなくても、`--clips` フォルダに `terms.txt` があれば自動で読み込みます
- WhisperX / whisper-cli にはプロンプトとして渡します。長すぎると効かなくなるため、渡すのは先頭から合計100文字までです（よく間違える語を上に書いてください）
- 字幕の修正（Gemini）には全部の語を渡します。明らかな聞き間違いだけをこの表記に直し、直した箇所は `caption_review.md` に残ります
- `caption_review.md` の先頭に、使った用語リストの語数が表示されます

### 字幕の表示タイミング

WhisperXで文字起こしした場合、1文字ごとの時刻（forced alignment）を使って字幕の分割位置の時刻を決めます。早口や間がある発話でも、字幕が話し始めに合わせて出ます。

whisper-cli（`--fast`）やGemini文字起こしでは文字ごとの時刻がないため、従来どおり文字数の比率で推定します（`caption_review.md` に表示されます）。

> 以前の `segments.json`（文字ごとの時刻なし）を `--skip-whisper` で再利用した場合も、文字数からの推定になります。精度を上げたい場合は一度通常実行してください。

### クリップの長さと時刻のずれ

クリップの長さは映像のフレーム数で計算し、文字起こし用の音声も各クリップを映像の長さに揃えてから連結します。

以前はクリップの音声をそのまま連結していたため、AAC音声のクリップでは1本あたり約10msずつ音声が長くなり、クリップ数が多いほど後半の字幕・効果音・インサートが映像より遅れていました（テスト用の99クリップ・約4分20秒で約1.1秒）。音声のないクリップが混ざっていても無音として扱います。

### 音量とBGM

各クリップと効果音のピークを ffmpeg で測り、目安に合う音量（Audio Levels）を XML に入れます。

| 種類 | 目安（デフォルト） | 入れ方 |
|---|---|---|
| 声（A1） | ピーク -7.0dB（-6〜-9dB） | クリップごとの音量 |
| 効果音（A2） | ピーク -16.5dB（-15〜-18dB） | 効果音ファイルごとの音量 |
| BGM（A3） | 会話なし -24.5dB / 会話中 -28.5dB | `bgm_ducked.wav` に焼き込み |

- Premiere のクリップ音量は +6dB が上限です。それでも小さいクリップは `audio_report.md` に一覧で出ます。オーディオゲインで上げるか、補正した音声に差し替えてください
- BGM は曲をループして全尺に敷き、`segments.json` の会話区間で自動的に下げます。下げ方は音そのものに焼き込むため、Premiere 上にキーフレームはできません
- 曲のつなぎ目（ループ位置）が自然かどうかは、聴いて確認してください

```bash
python3 scripts/create_youtube_sequence.py --clips /path/to/clips --sfx-dir /path/to/sfx --bgm /path/to/bgm.mp3
```

### 縦動画・解像度・fps

```bash
# TikTok / Shorts / リール向け（1080×1920）
python3 scripts/create_youtube_sequence.py --clips /path/to/clips --size vertical

# 4K素材を 23.976fps の FHD シーケンスに
python3 scripts/create_youtube_sequence.py --clips /path/to/clips --fps 23.976
```

- 素材とシーケンスのサイズが違う場合（横の素材を縦シーケンスに置く、4K素材をFHDに置くなど）は、画面いっぱいになるスケールを XML に入れます（`--fit` で変更可）。スマホの縦撮り素材は回転情報を読んで縦として扱います
- 縦・正方形では、字幕1枚の文字数を14文字にします（`--max-chars` で変更可）
- インサート画像はシーケンスの縦横比で生成し、引き伸ばさずに中央を切り抜いてサイズを合わせます
- シーケンスと素材の fps が違う場合、フレームレートの変換は Premiere 側で行われます

### コスト管理

インサート画像の生成にはGemini APIを使います（gemini-3.1-flash-image の2K画像・標準ティアで画像出力1枚あたり約$0.101、2026-08-31時点の公式料金表）。環境変数で上限を設定してください。

```bash
# デフォルトは $1.0
GEMINI_MAX_IMAGE_COST_USD=2.0 python3 scripts/create_youtube_sequence.py ...
```

## Premiere Pro での手順

### sequence.xml をインポート後

1. `File > Import` で `sequence.xml` を読み込む
2. `captions.srt` を Import してキャプショントラックに変換
3. `caption_review.md` を見ながら字幕を確認する（`place_markers.jsx` で `caption_review.json` を読み込むと、修正箇所にマーカーが付きます）

### ズームエフェクトの適用（ワンクリック）

`apply_zoom.jsx` で3ステップを一括実行します。

1. Premiere Pro で YouTube Sequence を開く
2. `apply_zoom.jsx` を Loader Script Panel で実行
3. QE API 非対応の場合はダイアログの指示に従ってトランスフォームを手動追加 → 再実行

> 旧手順の `convert_v2_to_adjustment.jsx` + `set_v2_scale.jsx` は `scripts/legacy/` に移動済み

### その他のPremiere用スクリプト

どれも Loader Script Panel（または ExtendScript Toolkit）で、YouTube Sequence をアクティブにして実行します。

| スクリプト | 用途 |
|---|---|
| `place_markers.jsx` | `analysis_debug.json`（ズーム・効果音・インサート・ハイライト）や `caption_review.json`（字幕の修正箇所）を選ぶと、該当位置にマーカーを配置 |
| `add_transitions.jsx` | `analysis_debug.json` の場面転換の位置で、V1のクリップ境界にトランジションを適用 |
| `clip_alignment.jsx` | テロップクリップ間の数フレームの隙間を詰める |
| `export_markers.jsx` / `delete_markers.jsx` | マーカーのJSON書き出し / 一括削除 |

### 未カット素材の自動カット

```bash
# ドライラン（ファイル書き出しなし、結果プレビュー）
python3 scripts/auto_cut.py --input /path/to/raw.mp4 --dry-run

# 本実行（clips/ にクリップ分割して出力）
python3 scripts/auto_cut.py --input /path/to/raw.mp4

# 無音検出のみ（フィラー/言い直し検出をスキップ）
python3 scripts/auto_cut.py --input /path/to/raw.mp4 --silence-only

# チャンネル用の設定を使う / 動き検出をスキップして速くする
python3 scripts/auto_cut.py --input /path/to/raw.mp4 --config configs/a_channel.json --no-motion
```

| オプション | 説明 |
|---|---|
| `--input` | 入力動画ファイル（必須） |
| `--output` | 出力先フォルダ（デフォルト: 入力ファイルの隣の `clips/`） |
| `--config` | 設定JSON（デフォルト: `configs/default.json`）。無音の基準・フィラーワード・前後の余白など |
| `--silence-only` | 無音検出のみ（フィラー/言い直し検出をスキップ） |
| `--no-motion` | 動き検出をスキップ（高速化） |
| `--dry-run` | ファイルを書き出さず、結果だけ表示 |

> フィラーワードは自動でカットされます。「ちょっと」「やっぱり」のように意味のある言葉としても使う語を入れると、話の内容まで削れることがあるので、`--dry-run` で確認してから本実行してください。

## 効果音フォルダの推奨構成

サブフォルダでカテゴリ分けすると精度が上がります。

```
効果音_normalized/
├── 強調/
│   └── pop_01.mp3
├── ネガティブ/
│   └── down_01.mp3
└── アクセント/
    └── click_01.mp3
```

4種類以内に絞るとGeminiの選定精度が安定します。

## 動作確認環境

- macOS
- Premiere Pro v26.0.0 (Build 72)
- Python 3.13
- whisper.cpp（`whisper-cli` コマンド）
- FFmpeg

## ライセンス

[MIT License](LICENSE)
