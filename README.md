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
├── sfx_manifest.json     # 効果音タイミング（参照用）
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
│   ├── clip_alignment.jsx             # Premiere用：テロップ間ギャップ自動修正
│   └── legacy/
│       ├── convert_v2_to_adjustment.jsx
│       └── set_v2_scale.jsx
├── configs/
│   ├── default.json                   # auto_cut.py デフォルト設定
│   └── a_channel.json                 # Aチャンネル用設定
└── templates/
    └── base_sequence.xml              # XMLテンプレート（スクリプトが自動参照）
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
| `--clips` | カット済みMP4フォルダのパス（必須） |
| `--sfx-dir` | 効果音フォルダのパス（省略時はA2トラックなし） |
| `--model` | whisper.cppモデルのパス（デフォルト: `~/ggml-large-v3.bin`） |
| `--output` | 出力先フォルダ（デフォルト: `--clips/output/`） |
| `--insert-target` | インサート画像の生成指示（Gemini向けプロンプト） |
| `--skip-whisper` | 既存の `segments.json` を再利用してWhisperをスキップ |
| `--caption-fix` | 字幕の修正方法。`apply`=Geminiの修正を反映して一覧に残す（デフォルト） / `propose`=字幕は音声認識のまま、修正案だけ一覧に出す / `off`=修正しない |
| `--reuse-analysis` | 既存の `analysis_debug.json` を再利用して画像・XMLのみ再生成 |
| `--no-insert` | インサート画像の生成をスキップ |
| `--only sfx,srt,...` | 指定ステップのみ実行（sfx/zoom/insert/srt） |
| `--skip sfx,srt,...` | 指定ステップをスキップ |

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

### 字幕の表示タイミング

WhisperXで文字起こしした場合、1文字ごとの時刻（forced alignment）を使って字幕の分割位置の時刻を決めます。早口や間がある発話でも、字幕が話し始めに合わせて出ます。

whisper-cli（`--fast`）やGemini文字起こしでは文字ごとの時刻がないため、従来どおり文字数の比率で推定します（`caption_review.md` に表示されます）。

> 以前の `segments.json`（文字ごとの時刻なし）を `--skip-whisper` で再利用した場合も、文字数からの推定になります。精度を上げたい場合は一度通常実行してください。

### クリップの長さと時刻のずれ

クリップの長さは映像のフレーム数で計算し、文字起こし用の音声も各クリップを映像の長さに揃えてから連結します。

以前はクリップの音声をそのまま連結していたため、AAC音声のクリップでは1本あたり約10msずつ音声が長くなり、クリップ数が多いほど後半の字幕・効果音・インサートが映像より遅れていました（テスト用の99クリップ・約4分20秒で約1.1秒）。音声のないクリップが混ざっていても無音として扱います。

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

### 未カット素材の自動カット（新機能）

```bash
# ドライラン（ファイル書き出しなし、結果プレビュー）
python3 scripts/auto_cut.py --input /path/to/raw.mp4 --dry-run

# 本実行（clips/ にクリップ分割して出力）
python3 scripts/auto_cut.py --input /path/to/raw.mp4

# 無音検出のみ（フィラー/言い直し検出をスキップ）
python3 scripts/auto_cut.py --input /path/to/raw.mp4 --silence-only
```

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
