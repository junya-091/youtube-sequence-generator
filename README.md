# youtube-sequence-generator

カット済みMP4フォルダを渡すと、Premiere Pro用のシーケンスXML・字幕SRT・インサート画像・効果音配置を自動生成するスクリプト一式です。

## 生成されるファイル

```
output/
├── captions.srt          # テロップ用字幕（Premiere ProにImport）
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

> WhisperX はオプションです。インストールされていない場合は自動で whisper-cli にフォールバックします。

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

### コスト管理

インサート画像の生成にはGemini APIを使います（1枚あたり約$0.10）。環境変数で上限を設定してください。

```bash
# デフォルトは $1.0
GEMINI_MAX_IMAGE_COST_USD=2.0 python3 scripts/create_youtube_sequence.py ...
```

## Premiere Pro での手順

### sequence.xml をインポート後

1. `File > Import` で `sequence.xml` を読み込む
2. `captions.srt` を Import してキャプショントラックに変換

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
