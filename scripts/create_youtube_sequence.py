#!/usr/bin/env python3
"""
YouTube用シーケンスを生成するスクリプト

MP4クリップフォルダから以下を生成する:
  output/captions.srt      - テロップ用 (Premiere で別途 import)
  output/sequence.xml      - FCP7 XML (Premiere に import)
  output/sfx_manifest.json - 効果音マニフェスト (参照用)
  output/inserts/*.png     - インサート画像 (Step 4 成功時のみ)

使い方:
  python3 create_youtube_sequence.py --clips /path/to/clips/ [--sfx-dir /path/to/sfx/] [--model ~/ggml-large-v3.bin]
"""

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

# ===== 定数 =====

DEFAULT_MODEL = str(Path.home() / "ggml-large-v3.bin")
TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "base_sequence.xml"

MAX_CHARS = 21           # SRT 1セグメント最大文字数 (WhisperX精度向上により19→21)
MIN_DUR_MS = 700         # SRT 最短表示時間 (ms) (WhisperX精度向上により1000→700)
INSERT_MIN_DURATION_MS = 3000  # インサート画像の最短表示時間 (ms)
ZOOM_WINDOW_MS = 1200    # key_point 周辺のスケールアップ時間幅 (ms)

PRICE_PER_IMAGE = 0.101  # $0.101/枚 (要確認: gemini-3.1-flash-image-preview / 2K)
MAX_IMAGE_COST = float(os.environ.get("GEMINI_MAX_IMAGE_COST_USD", "1.0"))

# ===== データクラス =====

@dataclass
class Segment:
    index: int
    start_ms: int
    end_ms: int
    text: str


@dataclass
class ClipInfo:
    path: Path
    duration_ms: int
    fps: float
    start_ms: int  # タイムライン上での累積開始時刻 (ms)


# ===== Gemini 分析スキーマ =====

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "key_points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "at_ms": {"type": "integer", "minimum": 0},
                    "reason": {"type": "string"},
                },
                "required": ["at_ms", "reason"],
            },
        },
        "sfx_events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "at_ms": {"type": "integer", "minimum": 0},
                    "sfx_id": {"type": "string"},
                    "pattern_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["at_ms", "sfx_id", "reason"],
            },
        },
        "insert_events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_ms": {"type": "integer", "minimum": 0},
                    "end_ms": {"type": "integer", "minimum": 0},
                    "prompt_en": {"type": "string"},
                },
                "required": ["start_ms", "end_ms", "prompt_en"],
            },
        },
        "highlight": {
            "type": "object",
            "properties": {
                "at_ms": {"type": "integer", "minimum": 0},
                "duration_ms": {"type": "integer"},
                "reason": {"type": "string"},
            },
            "required": ["at_ms", "duration_ms", "reason"],
        },
        "thumbnail_ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["title", "description"],
            },
        },
        "transition_events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "at_ms": {"type": "integer", "minimum": 0},
                    "type": {"type": "string"},
                    "duration_ms": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["at_ms", "type", "duration_ms", "reason"],
            },
        },
    },
    "required": ["key_points", "sfx_events", "insert_events", "highlight", "thumbnail_ideas", "transition_events"],
}


# ===== ユーティリティ =====

def _natural_sort_key(name: str):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", name)]


def _ms_to_frames(ms: int, fps: float) -> int:
    return int(round(ms / 1000.0 * fps))


def _format_ts(ms: int) -> str:
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_ts(ts: str) -> int:
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def _pathurl(path: Path) -> str:
    import unicodedata
    p = unicodedata.normalize("NFC", str(path.resolve()))
    return "file://localhost" + quote(p, safe="/")


def _timecode_string(ntsc_val: str) -> str:
    return "00:00:00;00" if ntsc_val == "TRUE" else "00:00:00:00"


def _detect_image_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    return ".bin"


# ===== Step 1: クリップ情報取得・音声連結・文字起こし =====

def get_clip_durations(clips: List[Path]) -> List[ClipInfo]:
    """各 MP4 の長さ (ms) と fps を取得し、累積 start_ms を計算する"""
    results = []
    cumulative = 0
    for clip in clips:
        out = subprocess.check_output(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", str(clip)],
            text=True,
        )
        data = json.loads(out)
        dur_ms = int(round(float(data["format"]["duration"]) * 1000))
        fps = 30.0
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                r_fps = stream.get("r_frame_rate", "30/1")
                num, den = r_fps.split("/")
                fps = float(num) / float(den)
                break
        results.append(ClipInfo(path=clip, duration_ms=dur_ms, fps=fps, start_ms=cumulative))
        cumulative += dur_ms
    return results


def _concat_audio(clips_info: List[ClipInfo], out_wav: str) -> None:
    """ffmpeg で全クリップを音声連結 → WAV (16kHz mono)"""
    inputs = []
    for c in clips_info:
        inputs += ["-i", str(c.path)]
    n = len(clips_info)
    concat_filter = f"concat=n={n}:v=0:a=1[aout]"
    subprocess.run(
        ["ffmpeg", "-y"] + inputs +
        ["-filter_complex", concat_filter, "-map", "[aout]",
         "-ar", "16000", "-ac", "1", out_wav],
        check=True, capture_output=True,
    )


GEMINI_CHUNK_SEC = 300  # 5分ごとにチャンク分割（課金済み65Kトークン対応）


def _transcribe_gemini(wav_path: str, max_chars: int = MAX_CHARS) -> List[Segment]:
    """Gemini API で高精度文字起こし → Segment[]
    戦略: 一括処理を試み、カバレッジ70%未満ならチャンク分割にフォールバック"""
    from google import genai
    from google.genai import types
    import time as _time

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY / GOOGLE_API_KEY が未設定")

    client = genai.Client(api_key=api_key, http_options={"timeout": 600000})

    # 音声の総尺を取得
    out = subprocess.check_output(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", wav_path],
        text=True,
    )
    total_sec = float(json.loads(out)["format"]["duration"])
    total_ms = int(total_sec * 1000)

    # Phase 1: 一括処理を試行（最大2回）
    for attempt in range(2):
        print(f"  🤖 Gemini: 音声 {total_sec:.0f}秒 → 一括処理 (試行 {attempt + 1}/2)")
        try:
            segments = _transcribe_gemini_single(client, types, wav_path, 0, total_ms, max_chars)
            if segments:
                last_end = max(s.end_ms for s in segments)
                coverage = last_end / total_ms
                print(f"     カバレッジ: {coverage:.0%} ({len(segments)} セグメント)")
                if coverage >= 0.7:
                    print(f"  ✅ 一括処理成功")
                    return segments
                print(f"  ⚠️ カバレッジ不足 → ", end="")
        except Exception as e:
            print(f"  ⚠️ 失敗: {e} → ", end="")
        if attempt == 0:
            print("リトライ...")
            _time.sleep(3)
        else:
            print("チャンク分割にフォールバック")

    # Phase 2: チャンク分割
    chunk_sec = GEMINI_CHUNK_SEC
    chunk_count = max(1, int(total_sec / chunk_sec) + (1 if total_sec % chunk_sec > 30 else 0))
    print(f"  🤖 Gemini: {chunk_count}チャンクに分割 ({chunk_sec}秒ごと)")

    all_segments = []
    for i in range(chunk_count):
        start_sec = i * chunk_sec
        end_sec = min((i + 1) * chunk_sec, total_sec)
        chunk_dur_ms = int((end_sec - start_sec) * 1000)
        offset_ms = int(start_sec * 1000)
        print(f"\n  📎 チャンク {i+1}/{chunk_count}: {start_sec:.0f}s 〜 {end_sec:.0f}s")

        # ffmpeg でチャンク切り出し
        chunk_path = wav_path + f".chunk{i}.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_path, "-ss", str(start_sec),
             "-t", str(end_sec - start_sec), "-acodec", "pcm_s16le",
             "-ar", "16000", "-ac", "1", chunk_path],
            capture_output=True, check=True,
        )

        try:
            chunk_segs = _transcribe_gemini_single(
                client, types, chunk_path, offset_ms, chunk_dur_ms, max_chars)
            all_segments.extend(chunk_segs)
        except Exception as e:
            print(f"  ❌ チャンク {i+1} 失敗: {e}")
            raise
        finally:
            Path(chunk_path).unlink(missing_ok=True)

    # インデックス振り直し・ソート
    all_segments.sort(key=lambda s: s.start_ms)
    for i, seg in enumerate(all_segments):
        seg.index = i + 1

    print(f"\n  ✅ Gemini 文字起こし完了（全チャンク統合）: {len(all_segments)} セグメント")
    return all_segments


def _transcribe_gemini_single(client, types, wav_path: str, offset_ms: int,
                               chunk_dur_ms: int, max_chars: int = MAX_CHARS) -> List[Segment]:
    """単一チャンク/一括をGeminiで文字起こし（リトライ付き・タイムスタンプバリデーション）"""
    import time

    uploaded = client.files.upload(file=wav_path)

    prompt = f"""あなたは日本語の音声文字起こし専門家です。
添付された音声ファイルを**最初から最後まで省略なく**SRT字幕形式で文字起こししてください。

ルール:
1. 1セグメントは最大{max_chars}文字以内
2. 分割は文節の区切り（助詞「は」「が」「で」「を」「に」の直後、句読点、感嘆符）で行う
3. 助詞や単語の途中で分割しない
4. 話者が変わった場合、新しいセグメントを開始する
5. 相槌・笑い声・フィラー（えー、あのー）も忠実に起こす
6. 固有名詞・店名・地名は正確に表記する（グアム、IHOP、Tギャラリア等）
7. タイムスタンプは音声に忠実に（±100ms以内）。00:00:00,000から開始すること
8. 無音区間にはセグメントを作成しない
9. 音声の最後まで必ず文字起こしすること。途中で省略しない
10. 出力はSRT形式のみ。説明や注釈は不要
11. テキストに半角スペースを入れない。日本語は詰めて書く（例: ×「めっちゃ アクティブ」→ ○「めっちゃアクティブ」）
12. テキストに句読点「。」「、」は入れない

出力例:
1
00:00:01,200 --> 00:00:03,500
みんなさぁ

2
00:00:03,500 --> 00:00:05,800
めっちゃアクティブじゃない？
"""

    max_retries = 2
    for attempt in range(max_retries):
        try:
            print(f"     文字起こし中... (試行 {attempt + 1}/{max_retries})")
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Part.from_uri(file_uri=uploaded.uri, mime_type=uploaded.mime_type),
                    prompt,
                ],
            )
            srt_text = resp.text.strip()
            segments = _parse_srt_text(srt_text)
            if not segments:
                if attempt < max_retries - 1:
                    print("     ⚠️ 空結果 → リトライ...")
                    time.sleep(3)
                    continue
                raise RuntimeError("空結果")

            # タイムスタンプバリデーション: チャンク尺を超えるタイムスタンプをクランプ
            # （Geminiが時:分:秒の桁を間違えた場合の対策）
            margin_ms = chunk_dur_ms + 10000  # 10秒のマージン
            clamped = 0
            for seg in segments:
                if seg.start_ms > margin_ms:
                    seg.start_ms = min(seg.start_ms, chunk_dur_ms)
                    clamped += 1
                if seg.end_ms > margin_ms:
                    seg.end_ms = min(seg.end_ms, chunk_dur_ms)
                    clamped += 1
                if seg.end_ms < seg.start_ms:
                    seg.end_ms = seg.start_ms + 1000
            if clamped > 0:
                print(f"     ⚠️ タイムスタンプ補正: {clamped}件をクランプ")

            # オフセット加算
            for seg in segments:
                seg.start_ms += offset_ms
                seg.end_ms += offset_ms

            # 文字数チェック
            over = [s for s in segments if len(s.text) > max_chars]
            if over:
                print(f"     ⚠️ {max_chars}文字超: {len(over)}件 → ローカル分割")
                segments = _local_split_segments(segments, max_chars)
            print(f"     ✅ {len(segments)} セグメント")
            return segments
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"     ⚠️ 失敗: {e} → リトライ...")
                time.sleep(5)
            else:
                raise

    raise RuntimeError("Gemini 文字起こし: 全試行失敗")


def _parse_srt_text(srt_text: str) -> List[Segment]:
    """SRT形式テキストをSegment[]にパース"""
    # markdown fence を除去
    srt_text = re.sub(r"^```[a-z]*\n?", "", srt_text.strip())
    srt_text = re.sub(r"\n?```$", "", srt_text.strip())

    blocks = [b.strip() for b in srt_text.split("\n\n") if b.strip()]
    segs = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        try:
            idx = int(lines[0].strip())
        except ValueError:
            continue
        ts_match = re.match(r"(\d{2}:\d{2}:\d{2}[,.:]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.:]\d{3})", lines[1])
        if not ts_match:
            continue
        start_str = ts_match.group(1).replace(".", ",")
        end_str = ts_match.group(2).replace(".", ",")
        text = "".join(lines[2:]).strip()
        # 半角スペース除去（日本語テキスト間のスペース）
        text = re.sub(r'(?<=[^\x00-\x7F]) (?=[^\x00-\x7F])', '', text)
        # 句読点除去
        text = text.replace('。', '').replace('、', '')
        if text:
            segs.append(Segment(
                index=idx,
                start_ms=_parse_ts(start_str),
                end_ms=_parse_ts(end_str),
                text=text,
            ))
    return segs


def _local_split_segments(segments: List[Segment], max_chars: int = MAX_CHARS) -> List[Segment]:
    """Gemini不要のローカル文字数分割フォールバック。文節区切りで分割する"""
    result = []
    for seg in segments:
        # 句読点・半角スペース除去
        text = seg.text.replace('。', '').replace('、', '')
        text = re.sub(r'(?<=[^\x00-\x7F]) (?=[^\x00-\x7F])', '', text)
        seg = Segment(index=seg.index, start_ms=seg.start_ms, end_ms=seg.end_ms, text=text)

        if len(seg.text) <= max_chars:
            result.append(seg)
            continue

        # 分割候補位置を取得
        split_points = _find_split_points(seg.text, max_chars)

        if not split_points:
            # 分割候補がない場合は文字数で強制分割
            chunks = []
            text = seg.text
            while len(text) > max_chars:
                chunks.append(text[:max_chars])
                text = text[max_chars:]
            if text:
                chunks.append(text)
        else:
            # 分割候補を使って max_chars 以内にまとめる
            chunks = []
            prev = 0
            for sp in split_points:
                if sp - prev > max_chars and prev < sp:
                    # この区間が長すぎる場合、前のチャンクを確定してから強制分割
                    portion = seg.text[prev:sp]
                    while len(portion) > max_chars:
                        chunks.append(portion[:max_chars])
                        portion = portion[max_chars:]
                    if portion:
                        chunks.append(portion)
                    prev = sp
                elif sp - prev > 0:
                    chunks.append(seg.text[prev:sp])
                    prev = sp
            # 残り
            if prev < len(seg.text):
                remaining = seg.text[prev:]
                if chunks and len(chunks[-1]) + len(remaining) <= max_chars:
                    chunks[-1] += remaining
                else:
                    chunks.append(remaining)

            # チャンクを再結合（短すぎるチャンクを隣接と結合）
            merged_chunks = [chunks[0]] if chunks else []
            for c in chunks[1:]:
                if len(merged_chunks[-1]) + len(c) <= max_chars:
                    merged_chunks[-1] += c
                else:
                    merged_chunks.append(c)
            chunks = merged_chunks

        # 時間を文字数比率で配分
        total_chars = sum(len(c) for c in chunks)
        dur_ms = seg.end_ms - seg.start_ms
        current_ms = seg.start_ms
        for chunk in chunks:
            chunk_dur = int(dur_ms * len(chunk) / total_chars) if total_chars > 0 else dur_ms // len(chunks)
            result.append(Segment(
                index=0,
                start_ms=current_ms,
                end_ms=current_ms + chunk_dur,
                text=chunk,
            ))
            current_ms += chunk_dur

    # インデックス振り直し
    for i, seg in enumerate(result):
        seg.index = i + 1
    return result


# ---- janome形態素解析（オプション） ----
try:
    from janome.tokenizer import Tokenizer as _JanomeTokenizer
    _JANOME = _JanomeTokenizer()
    _HAS_JANOME = True
except ImportError:
    _HAS_JANOME = False


def _find_split_points(text: str, max_chars: int) -> List[int]:
    """文節区切りの分割候補位置を返す"""
    if _HAS_JANOME:
        return _find_split_points_janome(text, max_chars)
    return _find_split_points_regex(text, max_chars)


def _find_split_points_janome(text: str, max_chars: int) -> List[int]:
    """janome形態素解析で文節境界を検出"""
    tokens = list(_JANOME.tokenize(text))
    points = []
    pos = 0
    for i, token in enumerate(tokens):
        pos += len(token.surface)
        if pos >= len(text):
            break
        part = token.part_of_speech.split(',')[0]
        # 助詞・助動詞の直後を分割候補
        if part in ('助詞', '助動詞'):
            # 「ん」禁則: 次のトークンが「ん」で始まる場合はスキップ
            if i + 1 < len(tokens) and tokens[i + 1].surface.startswith('ん'):
                continue
            points.append(pos)
    return points


def _find_split_points_regex(text: str, max_chars: int) -> List[int]:
    """正規表現ベースの文節区切り検出（janome未インストール時のフォールバック）"""
    # 助詞パターン（長いものから順にマッチ）
    particles = r'(?:から|まで|より|ので|けど|って|ため|ながら|たら|ほど|ば|は|が|で|を|に|の|と|へ|も|や)'
    # 助詞の直後で、かつ次が「ん」でない位置を分割候補にする
    pattern = re.compile(particles + r'(?!ん)')
    points = []
    for m in pattern.finditer(text):
        end_pos = m.end()
        if 0 < end_pos < len(text):
            points.append(end_pos)
    return points


def _merge_short_segments(segments: List[Segment]) -> List[Segment]:
    """MIN_DUR_MS未満の短すぎるセグメントを隣接セグメントと結合"""
    if not segments:
        return segments

    merged = [copy.copy(segments[0])]
    for seg in segments[1:]:
        dur = seg.end_ms - seg.start_ms
        prev = merged[-1]
        prev_dur = prev.end_ms - prev.start_ms
        # 短すぎる場合は前のセグメントに結合（結合後もMAX_CHARS以下の場合のみ）
        if dur < MIN_DUR_MS and len(prev.text) + len(seg.text) <= MAX_CHARS:
            prev.end_ms = seg.end_ms
            prev.text += seg.text
        # 前のセグメントが短すぎて、今のセグメントに結合できる場合
        elif prev_dur < MIN_DUR_MS and len(prev.text) + len(seg.text) <= MAX_CHARS:
            prev.end_ms = seg.end_ms
            prev.text += seg.text
        else:
            merged.append(copy.copy(seg))

    # インデックス振り直し
    for i, seg in enumerate(merged):
        seg.index = i + 1
    return merged


def _transcribe_whisperx(wav_path: str) -> List[Segment]:
    """WhisperX で文字起こし + forced alignment → Segment[]"""
    import whisperx
    import torch

    device = "cpu"  # MPS は whisperx の一部で非対応のため CPU で安定動作
    compute_type = "int8"  # M2 Air のメモリ節約

    print("  🔊 WhisperX: モデルロード中...")
    model = whisperx.load_model("large-v3", device, compute_type=compute_type, language="ja")

    print("  🔊 WhisperX: 文字起こし中...")
    audio = whisperx.load_audio(wav_path)
    result = model.transcribe(audio, batch_size=4, language="ja")

    # forced alignment でワードレベルタイムスタンプ取得
    print("  🔊 WhisperX: アライメント中...")
    try:
        align_model, metadata = whisperx.load_align_model(language_code="ja", device=device)
        result = whisperx.align(result["segments"], align_model, metadata, audio, device)
    except Exception as e:
        print(f"  ⚠️ WhisperX alignment 失敗（セグメントレベルで続行）: {e}")

    # Segment[] に変換
    segments = []
    for i, seg in enumerate(result.get("segments", result if isinstance(result, list) else [])):
        start_ms = int(seg.get("start", 0) * 1000)
        end_ms = int(seg.get("end", 0) * 1000)
        text = seg.get("text", "").strip()
        if text:
            segments.append(Segment(index=i + 1, start_ms=start_ms, end_ms=end_ms, text=text))

    # メモリ解放
    del model
    if torch.backends.mps.is_available():
        torch.mps.empty_cache()

    return segments


def _transcribe_whisper_cli(wav_path: str, model_path: str) -> List[Segment]:
    """whisper-cli フォールバック"""
    with tempfile.TemporaryDirectory() as tmpdir:
        srt_prefix = str(Path(tmpdir) / "output")
        subprocess.run(
            ["whisper-cli", "-m", model_path, "-f", wav_path,
             "-l", "ja", "--output-srt", "-of", srt_prefix, "--max-len", "40"],
            check=True,
        )
        srt_path = Path(tmpdir) / "output.srt"
        if not srt_path.exists():
            raise RuntimeError("whisper-cli が SRT を生成しなかった")
        return _parse_srt(srt_path)


def concat_and_transcribe(clips_info: List[ClipInfo], model_path: str,
                          fast: bool = False, experimental_gemini: bool = False) -> List[Segment]:
    """ffmpeg で音声連結 → WhisperX(デフォルト) / whisper-cli(--fast) / Gemini(実験的) → Segment[]"""
    with tempfile.TemporaryDirectory() as tmpdir:
        wav_path = str(Path(tmpdir) / "concat.wav")
        _concat_audio(clips_info, wav_path)

        if experimental_gemini:
            # Gemini API で文字起こし → 失敗時は自動フォールバック
            try:
                segments = _transcribe_gemini(wav_path)
                return segments
            except Exception as e:
                print(f"\n  ❌ Gemini 文字起こし失敗: {e}")
                print("  → WhisperX に自動フォールバック")
                try:
                    segments = _transcribe_whisperx(wav_path)
                    if segments:
                        segments = _local_split_segments(segments, MAX_CHARS)
                        print(f"  ✅ WhisperX + ローカル分割完了: {len(segments)} セグメント")
                        return segments
                except Exception as e2:
                    print(f"  ⚠️ WhisperX も失敗: {e2} → whisper-cli へ")
                segments = _transcribe_whisper_cli(wav_path, model_path)
                segments = _local_split_segments(segments, MAX_CHARS)
                print(f"  ✅ whisper-cli + ローカル分割完了: {len(segments)} セグメント")
                return segments

        if fast:
            # --fast: whisper-cli で高速処理
            return _transcribe_whisper_cli(wav_path, model_path)

        # デフォルト: WhisperX（精度優先）→ 失敗時 whisper-cli フォールバック
        try:
            segments = _transcribe_whisperx(wav_path)
            if segments:
                print(f"  ✅ WhisperX 完了: {len(segments)} セグメント")
                return segments
            print("  ⚠️ WhisperX が空結果 → whisper-cli にフォールバック")
        except Exception as e:
            print(f"  ⚠️ WhisperX 失敗: {e} → whisper-cli にフォールバック")

        return _transcribe_whisper_cli(wav_path, model_path)


def _parse_srt(srt_path: Path) -> List[Segment]:
    content = srt_path.read_text(encoding="utf-8")
    blocks = [b.strip() for b in content.split("\n\n") if b.strip()]
    segs = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        idx = int(lines[0])
        start_str, end_str = lines[1].split(" --> ")
        text = " ".join(lines[2:])
        segs.append(Segment(
            index=idx,
            start_ms=_parse_ts(start_str.strip()),
            end_ms=_parse_ts(end_str.strip()),
            text=text.strip(),
        ))
    return segs


# ===== Step 2: sfx_manifest 生成 + Gemini 分析 =====

def build_sfx_manifest(sfx_dir: Path) -> Dict:
    """sfx_dir をスキャンして sfx_manifest.json 用データを生成する。
    サブフォルダ（カテゴリ名）をサポート: カテゴリ/ファイル名 形式で id を管理。
    フラット構造（サブフォルダなし）は従来通り動作する。
    """
    from google import genai

    supported = {".mp3", ".wav", ".aiff", ".m4a"}

    # (category_or_None, Path) のリストを収集
    file_pairs: List[tuple] = []
    for item in sorted(sfx_dir.iterdir(), key=lambda x: _natural_sort_key(x.name)):
        if item.is_dir():
            for f in sorted(item.iterdir(), key=lambda x: _natural_sort_key(x.name)):
                if f.suffix.lower() in supported:
                    file_pairs.append((item.name, f))
        elif item.suffix.lower() in supported:
            file_pairs.append((None, item))

    if not file_pairs:
        return {"sfx": []}

    sfx_entries = []
    for category, f in file_pairs:
        out = subprocess.check_output(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(f)],
            text=True,
        )
        dur_ms = int(round(float(json.loads(out)["format"]["duration"]) * 1000))
        sfx_id = f"{category}/{f.stem}" if category else f.stem
        rel_path = f"{category}/{f.name}" if category else f.name
        sfx_entries.append({
            "id": sfx_id,
            "relative_path": rel_path,
            "duration_ms": dur_ms,
            "gain_db": 0.0,
            "tags": [],
        })

    # Gemini で tags を自動推定（キャッシュあり）
    ids = [e["id"] for e in sfx_entries]
    cache_path = sfx_dir / ".sfx_tags_cache.json"
    tags_map: Dict[str, list] = {}

    # キャッシュ確認: IDリストが一致すれば再利用
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if set(cache.get("ids", [])) == set(ids):
                tags_map = cache.get("tags", {})
                print(f"  📦 sfx タグキャッシュ使用 ({len(ids)} 件)")
        except Exception:
            pass

    if not tags_map:
        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key:
            try:
                client = genai.Client(api_key=api_key, http_options={"timeout": 300000})
                prompt = (
                    "以下の効果音ID（カテゴリ/ファイル名 形式）について、各ファイルが表す音の特徴を英語タグ（配列）で推定してください。\n"
                    "タグ例: impact, emphasis, sparkle, happy, positive, negative, surprise, drop, accent, light\n"
                    'JSON形式で返してください: {"tags": {"id": ["タグ", ...]}}\n\n'
                    f"効果音ID: {json.dumps(ids, ensure_ascii=False)}"
                )
                print(f"  🤖 Gemini: sfx タグ推定中 ({len(ids)} 件)...")
                resp = client.models.generate_content(
                    model="gemini-3-flash-preview",
                    contents=prompt,
                    config={"response_mime_type": "application/json"},
                )
                tags_map = json.loads(resp.text).get("tags", {})
                # キャッシュを保存
                cache_path.write_text(
                    json.dumps({"ids": ids, "tags": tags_map}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  💾 sfx タグキャッシュ保存: {cache_path}")
            except Exception as e:
                print(f"  ⚠️ sfx tags 推定失敗: {e}")
        else:
            print("  ⚠️ GEMINI_API_KEY 未設定 - sfx tags は空配列")

    for entry in sfx_entries:
        entry["tags"] = tags_map.get(entry["id"], [])

    return {"sfx": sfx_entries}


def _supplement_sfx(client, sfx_events, sfx_summary, segs_data, total_ms, sfx_min):
    """sfx_events が sfx_min 未満のとき、40秒以上空いている区間に補填リクエストを送る"""
    GAP_MS = 40000

    sorted_events = sorted(sfx_events, key=lambda e: e["at_ms"])
    existing_ms = [e["at_ms"] for e in sorted_events]

    # 40秒以上空いている区間を特定
    checkpoints = [0] + existing_ms + [total_ms]
    gaps = []
    for i in range(len(checkpoints) - 1):
        gap_start = checkpoints[i]
        gap_end = checkpoints[i + 1]
        if gap_end - gap_start > GAP_MS:
            gaps.append({"start_ms": gap_start, "end_ms": gap_end})

    if not gaps:
        return sfx_events

    needed = sfx_min - len(sfx_events)
    print(f"  ⚠️ sfx_events {len(sfx_events)}件 < 最低{sfx_min}件 → 補填リクエスト ({len(gaps)}区間, 不足{needed}件)")

    # 空き区間に含まれるセグメントのみ渡す
    gap_segs = [
        s for s in segs_data
        if any(g["start_ms"] <= s["start_ms"] < g["end_ms"] for g in gaps)
    ]

    supplement_prompt = f"""以下のYouTube動画の文字起こしから、効果音を追加すべき場面を選んでください。

効果音ライブラリ (sfx_id と tags):
{json.dumps(sfx_summary, ensure_ascii=False, indent=2)}

効果音が40秒以上空いている区間:
{json.dumps(gaps, ensure_ascii=False, indent=2)}

該当区間の文字起こし:
{json.dumps(gap_segs, ensure_ascii=False, indent=2)}

既存の効果音配置 (at_ms のみ):
{json.dumps(existing_ms)}

指示:
- 上記の「空き区間」に効果音を追加してください
- 最低{needed}件以上追加すること（各区間に少なくとも1件）
- sfx_id は sfx_summary の id から選択する
- at_ms はグローバルタイムライン上の絶対 ms 値
- 既存の配置と重複しないこと（既存の at_ms から 2000ms 以上離すこと）
"""

    supp_schema = {
        "type": "object",
        "properties": {
            "sfx_events": ANALYSIS_SCHEMA["properties"]["sfx_events"]
        },
        "required": ["sfx_events"],
    }

    for model in ["gemini-3-flash-preview", "gemini-2.5-flash"]:
        try:
            print(f"  🤖 Gemini: sfx補填中... ({model})")
            resp = client.models.generate_content(
                model=model,
                contents=supplement_prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": supp_schema,
                },
            )
            supplement = json.loads(resp.text).get("sfx_events", [])
            merged = sorted(sfx_events + supplement, key=lambda e: e["at_ms"])
            print(f"  ✅ sfx補填完了: {len(sfx_events)}件 → {len(merged)}件")
            return merged
        except Exception as e:
            print(f"  ⚠️ sfx補填失敗 ({model}): {e}")
            if "503" not in str(e):
                break

    return sfx_events


def _validate_sfx_intervals(sfx_events: List[Dict], total_ms: int) -> List[Dict]:
    """SFXイベントのポストプロセス検証: 40秒超ギャップ警告 + 2秒以内近接統合"""
    if not sfx_events:
        return sfx_events

    sorted_events = sorted(sfx_events, key=lambda e: e["at_ms"])

    # 2秒以内の近接イベント統合（後のイベントを削除）
    merged = [sorted_events[0]]
    for ev in sorted_events[1:]:
        if ev["at_ms"] - merged[-1]["at_ms"] < 2000:
            print(f"  ⚠️ sfx近接統合: {merged[-1]['at_ms']}ms と {ev['at_ms']}ms（差 {ev['at_ms'] - merged[-1]['at_ms']}ms）→ 後者を削除")
        else:
            merged.append(ev)

    # 40秒超ギャップ警告
    checkpoints = [0] + [e["at_ms"] for e in merged] + [total_ms]
    gap_count = 0
    for i in range(len(checkpoints) - 1):
        gap = checkpoints[i + 1] - checkpoints[i]
        if gap > 40000:
            gap_count += 1
            print(f"  ⚠️ sfxギャップ: {checkpoints[i]}ms〜{checkpoints[i+1]}ms（{gap // 1000}秒間 効果音なし）")
    if gap_count:
        print(f"  📊 40秒超ギャップ: {gap_count}箇所")

    if len(merged) < len(sorted_events):
        print(f"  📊 sfx近接統合: {len(sorted_events)}件 → {len(merged)}件")

    return merged


def analyze_with_gemini(
    segments: List[Segment],
    clips_info: List[ClipInfo],
    sfx_manifest: Dict,
    insert_target: str = "",
) -> Dict:
    """全コンテンツ分析を Gemini に 1 リクエストで送信する"""
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  ⚠️ GEMINI_API_KEY 未設定 - 分析スキップ")
        return {"key_points": [], "sfx_events": [], "insert_events": []}

    client = genai.Client(api_key=api_key, http_options={"timeout": 300000})
    has_sfx = len(sfx_manifest.get("sfx", [])) > 0
    sfx_summary = [{"id": s["id"], "tags": s["tags"]} for s in sfx_manifest.get("sfx", [])]

    segs_data = [
        {"index": s.index, "start_ms": s.start_ms, "end_ms": s.end_ms, "text": s.text}
        for s in segments
    ]
    clips_data = [
        {"clip": c.path.name, "start_ms": c.start_ms, "duration_ms": c.duration_ms}
        for c in clips_info
    ]

    # 動画総尺に比例した件数を動的に計算
    total_sec = sum(c.duration_ms for c in clips_info) / 1000
    sfx_min = max(10, int(total_sec / 40))              # 40秒に1件（最低件数）
    sfx_max = sfx_min + max(20, int(sfx_min * 0.5))    # 列挙ポイント等で複数配置できる余裕分を追加
    insert_max = max(3, min(10, int(total_sec / 300)))  # 5分に1件、最大10件
    kp_min = max(5, int(total_sec / 20))               # 20秒に1件（最低件数）
    kp_max = kp_min + max(10, int(kp_min * 0.3))       # 余裕分を追加
    transition_max = max(2, min(8, int(total_sec / 120)))  # 2分に1件、最大8件

    # minItems/maxItems は Gemini API スキーマ非対応のため削除し、件数指定はプロンプトのみで行う
    schema = copy.deepcopy(ANALYSIS_SCHEMA)

    insert_target_instruction = (
        f"ターゲット層: {insert_target}。このターゲット向けの画像スタイルを優先すること（ただし動画内容との関連性が最優先）。"
        if insert_target
        else ""
    )

    sfx_instruction = (
        f"効果音ライブラリ (sfx_id と tags):\n{json.dumps(sfx_summary, ensure_ascii=False, indent=2)}"
        if has_sfx
        else "効果音ライブラリ: なし（sfx_events は必ず空配列 [] で返すこと）"
    )

    prompt = f"""以下はYouTube動画の文字起こしセグメント（JSON）です。グローバルタイムライン上の at_ms（ミリ秒）を基準に分析してください。

クリップ情報:
{json.dumps(clips_data, ensure_ascii=False, indent=2)}

文字起こし:
{json.dumps(segs_data, ensure_ascii=False, indent=2)}

{sfx_instruction}

分析してください:
1. key_points: ズームアップ演出を入れる場面 (笑い・驚き・強調・感情的な瞬間など映像にメリハリが出る全ての場面)。**最低{kp_min}件**（20秒に1件が基本ルール）、最大{kp_max}件。20秒以上ズームが空く区間を作ってはならない
2. sfx_events: {"効果音を挿入すべき場面 (sfx_id で選択)。**最低 {sfx_min} 件**（40秒に1件が絶対ルール）、最大 {sfx_max} 件。40秒以上効果音が空く区間を作ってはならない" if has_sfx else "（sfx_events は空配列 []）"}
3. insert_events: 画像インサートを挿入すべき場面。**最大{insert_max}件以内**に厳選すること。{insert_target_instruction}各インサートには start_ms（表示開始ms）・end_ms（表示終了ms、start_msより**最低3000ms以上**後）・prompt_en（画像生成用英語プロンプト）を返すこと。表示時間はシーンの内容・テンポに合わせて自然な長さにすること。prompt_en には必ず「Japanese style, set in Japan, featuring Japanese people」等の日本に関連する要素を含めること（外国人・英語テキスト・西洋的なビジュアルは使わない）
4. highlight: ショート動画・切り抜き用として最も盛り上がる15秒前後の区間を1件。at_ms（開始ms）とduration_ms（10000〜20000msの範囲）、reason（選んだ理由・**日本語**で記述）を返すこと
5. thumbnail_ideas: サムネイル画像の案を3件。title（視聴者の興味を引く煽りタイトル文・日本語）とdescription（画像の具体的な構図・人物・テキストの説明・日本語）を返すこと
6. transition_events: シーン転換・話題変更時に適用するトランジション。**最大{transition_max}件**に厳選すること。
   - type: "cross_dissolve"（柔らかいシーン転換・話題移行）, "dip_to_black"（大きな場面転換・冒頭/エンディング前後）, "dip_to_white"（強い感情・特別な瞬間）のいずれか
   - at_ms: トランジションを配置するカットポイント（クリップ境界）のms値。クリップ情報の start_ms + duration_ms がクリップ境界
   - duration_ms: トランジション長さ（500-2000ms。通常は1000ms。短いカットでは500ms、大きな場面転換では1500-2000ms）
   - 全てのカット境界にトランジションを入れるのではなく、**話題の変化・場面の切り替わり**のみに限定すること
   - 冒頭の挨拶後・エンディング前は dip_to_black が適切
   - 通常の話題移行は cross_dissolve が基本

指示:
- at_ms はグローバルタイムライン上の絶対 ms 値
- clip_index は返さない（コード側で算出する）
- sfx_events の sfx_id は sfx_summary の id から選択する
- sfx_events の配置ルール:
  1. 挨拶・自己紹介・動画冒頭の「こんにちは」等の挨拶シーンには効果音を付けない
  2. **大事なポイント・要点を言い始める冒頭のタイミング**に強調系（強調/和風カテゴリ）を配置する。1つ目・2つ目・3つ目のような列挙時は各々の冒頭に効果音を付与し、同じ sfx_id を使う
  3. 列挙・説明が続く区間では40秒以内に複数の効果音を入れてよい（密度制限なし）
  4. 40秒以上効果音が空いている区間には必ずアクセントカテゴリ（アクセント/）を挿入して埋める
  5. 笑い・ボケ・ポジティブな場面にはそれぞれに合ったカテゴリを使う
- sfx_events の pattern_id: 「1つ目・2つ目」「まず・次に・最後に」などの列挙・並列パターンには必ず同じ sfx_id を使い、同じ pattern_id を設定すること（例: "enumeration"）。役割が同じ場面は統一する
- pattern_id は任意フィールド。パターンに当てはまらない場面は省略してよい
"""

    for model in ["gemini-3-flash-preview", "gemini-2.5-flash"]:
        try:
            print(f"  🤖 Gemini: コンテンツ分析中... ({model})")
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": schema,
                },
            )
            result = json.loads(resp.text)
            if not has_sfx:
                result["sfx_events"] = []  # LLM が空配列を守らない場合に備えて強制上書き
            elif len(result.get("sfx_events", [])) < sfx_min:
                result["sfx_events"] = _supplement_sfx(
                    client, result["sfx_events"], sfx_summary, segs_data,
                    int(total_sec * 1000), sfx_min,
                )
            return result
        except Exception as e:
            print(f"  ⚠️ Gemini 分析失敗 ({model}): {e}")
            if "503" not in str(e):
                break  # 503以外のエラーはフォールバックしない
    return {"key_points": [], "sfx_events": [], "insert_events": []}


# ===== Step 3: SRT 生成 (captionモード統一: 元タイムスタンプ尊重) =====

def build_srt(segments: List[Segment], audio_dur_ms: int, skip_refine: bool = False) -> str:
    """captionモード SRT を生成する。元のタイムスタンプを尊重し、無音区間にはテロップを表示しない"""
    if skip_refine:
        # Gemini文字起こし済み → refine スキップ
        print("  ℹ️ Gemini文字起こし済み → refine スキップ")
        segments = _merge_short_segments(_local_split_segments(segments))
    else:
        # WhisperX / whisper-cli → Geminiテキスト修正（失敗時はローカル分割）
        segments = _refine_segments(segments)

    # タイムスタンプバリデーション（昇順保証・重複除去）
    segments = _validate_srt_timestamps(segments, audio_dur_ms)

    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_format_ts(seg.start_ms)} --> {_format_ts(seg.end_ms)}")
        lines.append(seg.text)
        lines.append("")
    return "\n".join(lines)


def _validate_srt_timestamps(segments: List[Segment], audio_dur_ms: int) -> List[Segment]:
    """SRTタイムスタンプのバリデーション: 昇順保証・尺超え防止・最小表示時間保証"""
    if not segments:
        return segments
    result = []
    for seg in sorted(segments, key=lambda s: s.start_ms):
        # 尺を超えるセグメントをクランプ
        seg.start_ms = min(seg.start_ms, audio_dur_ms)
        seg.end_ms = min(seg.end_ms, audio_dur_ms)
        # start >= end の不正セグメントをスキップ
        if seg.end_ms <= seg.start_ms:
            continue
        # 前のセグメントと重複する場合は前のendを調整
        if result and seg.start_ms < result[-1].end_ms:
            result[-1].end_ms = seg.start_ms
            if result[-1].end_ms <= result[-1].start_ms:
                result.pop()
        result.append(seg)
    # インデックス振り直し
    for i, seg in enumerate(result):
        seg.index = i + 1
    return result


def _refine_segments(segments: List[Segment]) -> List[Segment]:
    """Gemini Structured Outputs でテキストを修正・分割 → 失敗時はローカル分割フォールバック"""
    import time as _time
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  ⚠️ GEMINI_API_KEY 未設定 → ローカル分割で処理")
        return _merge_short_segments(_local_split_segments(segments))

    client = genai.Client(api_key=api_key, http_options={"timeout": 300000})
    seg_list = [
        {"index": s.index, "start_ms": s.start_ms, "end_ms": s.end_ms, "text": s.text}
        for s in segments
    ]
    prompt = f"""以下はWhisperXが生成した音声認識結果（JSON）です。
タイムスタンプはforced alignmentで単語レベルに正確です（±50ms程度）。
以下のルールに従って修正し、JSON配列のみを返してください。

ルール:
1. start_ms/end_ms は**変更しない**（タイムスタンプの正確性を維持）
2. 誤字脱字・不自然な表現を修正する
3. 句読点「。」「、」は入れない。半角スペースも入れない
4. 1セグメントが{MAX_CHARS}文字を超える場合、文字数比率で時間を比例配分して分割する
5. 分割位置は**文節の区切り**を最優先する（「〜は」「〜が」「〜で」「〜を」などの助詞の直後）
6. 助詞の途中や単語の途中で分割しない
7. 「ん」の前で分割しない（例: 「なんぼ」→「な」+「んぼ」は不可）

入力:
{json.dumps(seg_list, ensure_ascii=False, indent=2)}
"""
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "start_ms": {"type": "integer"},
                "end_ms": {"type": "integer"},
            },
            "required": ["text", "start_ms", "end_ms"],
        },
    }

    # リトライ（指数バックオフ: 1s, 2s, 4s）
    for attempt in range(3):
        try:
            print(f"  🤖 Gemini: テキスト修正中... (試行 {attempt + 1}/3)")
            resp = client.models.generate_content(
                model="gemini-3-flash-preview",
                contents=prompt,
                config={"response_mime_type": "application/json", "response_schema": schema},
            )
            raw = json.loads(resp.text)
            result = [
                Segment(index=i + 1, start_ms=int(r["start_ms"]),
                        end_ms=int(r["end_ms"]), text=r["text"])
                for i, r in enumerate(raw)
            ]
            if result:
                return _merge_short_segments(result)
        except Exception as e:
            wait = 2 ** attempt
            print(f"  ⚠️ Gemini SRT refine 失敗 (試行 {attempt + 1}/3): {e}")
            if attempt < 2:
                print(f"     {wait}秒後にリトライ...")
                _time.sleep(wait)

    print("  ⚠️ Gemini 全試行失敗 → ローカル分割にフォールバック")
    return _merge_short_segments(_local_split_segments(segments))


# ===== Step 4: インサート画像生成 =====

def estimate_image_cost(n: int) -> float:
    return n * PRICE_PER_IMAGE


def generate_insert_images(
    insert_list: List[Dict], out_dir: Path
) -> List[Optional[Path]]:
    """Gemini で画像生成 → PNG に正規化 → sips で 1920×1080 にリサイズ"""
    from google import genai

    out_dir.mkdir(parents=True, exist_ok=True)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  ⚠️ GEMINI_API_KEY 未設定 - 画像生成スキップ")
        return [None] * len(insert_list)

    n = len(insert_list)
    cost = estimate_image_cost(n)
    print(f"  📸 {n}枚 × ${PRICE_PER_IMAGE:.3f} = 推定 ${cost:.3f} USD (要確認)")
    if cost > MAX_IMAGE_COST:
        print(f"  ⛔ 推定コスト ${cost:.3f} が上限 ${MAX_IMAGE_COST} を超えるため中断")
        print(f"     環境変数 GEMINI_MAX_IMAGE_COST_USD で上限を変更できます")
        return [None] * n

    client = genai.Client(api_key=api_key, http_options={"timeout": 300000})
    results: List[Optional[Path]] = []

    for i, event in enumerate(insert_list):
        out_path = out_dir / f"insert_{i + 1:03d}.png"
        last_err = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    print(f"    🔄 {out_path.name} リトライ ({attempt}/2)...")
                from google.genai import types as genai_types
                jp_suffix = ". Japanese style, set in Japan, featuring Japanese people and Japanese elements. No Western or foreign people, no English text."
                resp = client.models.generate_content(
                    model="gemini-3.1-flash-image-preview",
                    contents=event["prompt_en"] + jp_suffix,
                    config=genai_types.GenerateContentConfig(
                        response_modalities=["IMAGE", "TEXT"],
                    ),
                )
                img_bytes = resp.candidates[0].content.parts[0].inline_data.data
                raw_ext = _detect_image_ext(img_bytes)
                raw_path = out_dir / f"insert_{i + 1:03d}{raw_ext}"
                raw_path.write_bytes(img_bytes)
                if raw_ext != ".png":
                    print(f"    ℹ️ {out_path.name}: 実体は {raw_ext} のため PNG に変換")
                if out_path.exists() and raw_path != out_path:
                    out_path.unlink()

                if raw_ext == ".png":
                    final_input = raw_path
                else:
                    subprocess.run(
                        ["sips", "-s", "format", "png", str(raw_path), "--out", str(out_path)],
                        check=True, capture_output=True,
                    )
                    final_input = out_path

                if final_input != out_path:
                    out_path.write_bytes(final_input.read_bytes())

                # sips で 1920×1080 にリサイズ (-z 高さ 幅)
                subprocess.run(
                    ["sips", "-z", "1080", "1920", str(out_path)],
                    check=True, capture_output=True,
                )
                if raw_path != out_path and raw_path.exists():
                    raw_path.unlink()
                print(f"    ✅ {out_path.name}")
                results.append(out_path)
                last_err = None
                break
            except Exception as e:
                last_err = e
        if last_err is not None:
            print(f"    ⚠️ {out_path.name} 生成失敗 (3回試行): {last_err}")
            results.append(None)

    return results


# ===== Step 5: FCP7 XML 生成 =====

def ms_to_clip_index(at_ms: int, clips_info: List[ClipInfo]) -> int:
    """グローバル at_ms からクリップインデックスを導出する"""
    for i, c in enumerate(clips_info):
        if c.start_ms <= at_ms < c.start_ms + c.duration_ms:
            return i
    return len(clips_info) - 1


def build_fcp7_xml(
    clips_info: List[ClipInfo],
    key_points: List[Dict],
    inserts: List[Dict],
    sfx_events: List[Dict],
    sfx_manifest: Dict,
    sfx_dir: Optional[Path],
    insert_imgs: List[Optional[Path]],
) -> str:
    """テンプレート差し替え方式で FCP7 XML を生成する"""
    from lxml import etree

    tree = etree.parse(str(TEMPLATE_PATH))
    root = tree.getroot()
    seq = root.find("sequence")
    media = seq.find("media")
    video_elem = media.find("video")
    audio_elem = media.find("audio")

    video_tracks = video_elem.findall("track")
    audio_tracks = audio_elem.findall("track")
    v1_track = video_tracks[0]
    # V2: 調整レイヤー (動的追加 — テンプレートに含めるとPremiere Proインポート後にずれるため)
    v2_track = etree.SubElement(video_elem, "track")
    etree.SubElement(v2_track, "enabled").text = "TRUE"
    etree.SubElement(v2_track, "locked").text = "FALSE"
    a1_track, a2_track = audio_tracks[0], audio_tracks[1]

    # fps / timebase / ntsc (全クリップ共通: 最初のクリップから取得)
    fps = clips_info[0].fps if clips_info else 30.0
    timebase = int(round(fps))
    ntsc = "TRUE" if abs(fps - round(fps)) > 0.01 else "FALSE"

    # シーケンス・タイムコード・フォーマットの rate をクリップfpsで上書き
    displayformat = "DF" if ntsc == "TRUE" else "NDF"
    for elem in [
        seq.find("rate"),
        seq.find("timecode/rate"),
        seq.find("media/video/format/samplecharacteristics/rate"),
    ]:
        if elem is not None:
            elem.find("timebase").text = str(timebase)
            elem.find("ntsc").text = ntsc
    df_elem = seq.find("timecode/displayformat")
    if df_elem is not None:
        df_elem.text = displayformat
    tc_string_elem = seq.find("timecode/string")
    if tc_string_elem is not None:
        tc_string_elem.text = _timecode_string(ntsc)

    # key_points をクリップごとのローカル時刻へ変換
    key_points_local: Dict[int, List[int]] = {}
    for kp in key_points:
        clip_idx = ms_to_clip_index(kp["at_ms"], clips_info)
        clip = clips_info[clip_idx]
        local_ms = max(0, min(clip.duration_ms, kp["at_ms"] - clip.start_ms))
        key_points_local.setdefault(clip_idx, []).append(local_ms)

    # file id 管理 (同一ファイルは 2 回目以降 id 参照のみ)
    seen_files: Dict[str, str] = {}  # 絶対パス -> file_id

    # clipitem の連番カウンタ
    ci_seq = [0]

    def _next_id(prefix: str) -> str:
        ci_seq[0] += 1
        return f"{prefix}-{ci_seq[0]}"

    def _rate_elem(tb: int, ntsc_val: str):
        r = etree.Element("rate")
        etree.SubElement(r, "timebase").text = str(tb)
        etree.SubElement(r, "ntsc").text = ntsc_val
        return r

    def _timecode_elem(tb: int, ntsc_val: str):
        tc = etree.Element("timecode")
        tc.append(_rate_elem(tb, ntsc_val))
        etree.SubElement(tc, "string").text = _timecode_string(ntsc_val)
        etree.SubElement(tc, "frame").text = "0"
        etree.SubElement(tc, "displayformat").text = "DF" if ntsc_val == "TRUE" else "NDF"
        return tc

    # ZOOM_WINDOW_MS 動的計算: 平均カット長に応じて調整
    if clips_info:
        avg_dur_ms = sum(c.duration_ms for c in clips_info) / len(clips_info)
        if avg_dur_ms < 3000:
            zoom_window = 800
        elif avg_dur_ms > 8000:
            zoom_window = 1500
        else:
            zoom_window = ZOOM_WINDOW_MS  # デフォルト 1200
    else:
        zoom_window = ZOOM_WINDOW_MS

    def _get_zoom_intervals(clip_idx: int, clip: ClipInfo):
        """key_points に基づくズーム区間 [(start_ms, end_ms), ...] を返す"""
        points = sorted(key_points_local.get(clip_idx, []))
        if not points:
            return []
        half = zoom_window // 2
        intervals = []
        for point_ms in points:
            start = max(0, point_ms - half)
            end = min(clip.duration_ms, point_ms + half)
            if end > start:
                intervals.append((start, end))
        if not intervals:
            return []
        merged = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return [(s, e) for s, e in merged]

    def _make_video_clipitem(
        ci_id: str, clip: ClipInfo,
        tl_start_ms: int, tl_end_ms: int,
        src_in_ms: int, src_out_ms: int,
        file_id: str, link_audio_id: str,
        link_clipindex: int,
    ):
        ci = etree.Element("clipitem")
        ci.set("id", ci_id)
        etree.SubElement(ci, "name").text = clip.path.name
        etree.SubElement(ci, "enabled").text = "TRUE"
        etree.SubElement(ci, "duration").text = str(_ms_to_frames(src_out_ms - src_in_ms, fps))
        ci.append(_rate_elem(timebase, ntsc))
        etree.SubElement(ci, "start").text = str(_ms_to_frames(tl_start_ms, fps))
        etree.SubElement(ci, "end").text = str(_ms_to_frames(tl_end_ms, fps))
        etree.SubElement(ci, "in").text = str(_ms_to_frames(src_in_ms, fps))
        etree.SubElement(ci, "out").text = str(_ms_to_frames(src_out_ms, fps))

        p_str = str(clip.path.resolve())
        if p_str not in seen_files:
            # 初回のみフル定義
            seen_files[p_str] = file_id
            f_elem = etree.SubElement(ci, "file")
            f_elem.set("id", file_id)
            etree.SubElement(f_elem, "name").text = clip.path.name
            etree.SubElement(f_elem, "pathurl").text = _pathurl(clip.path)
            f_elem.append(_rate_elem(timebase, ntsc))
            etree.SubElement(f_elem, "duration").text = str(_ms_to_frames(clip.duration_ms, fps))
            m = etree.SubElement(f_elem, "media")
            etree.SubElement(m, "video")
            aud = etree.SubElement(m, "audio")
            etree.SubElement(aud, "channelcount").text = "2"
        else:
            # 2回目以降は id 参照のみ（空タグ）
            actual_fid = seen_files[p_str]
            f_elem = etree.SubElement(ci, "file")
            f_elem.set("id", actual_fid)

        lnk = etree.SubElement(ci, "link")
        etree.SubElement(lnk, "linkclipref").text = link_audio_id
        etree.SubElement(lnk, "mediatype").text = "audio"
        etree.SubElement(lnk, "trackindex").text = "1"
        etree.SubElement(lnk, "clipindex").text = str(link_clipindex)

        return ci

    def _make_audio_clipitem(
        ci_id: str, clip: ClipInfo,
        tl_start_ms: int, tl_end_ms: int,
        src_in_ms: int, src_out_ms: int,
        link_video_id: str, link_clipindex: int,
    ):
        ci = etree.Element("clipitem")
        ci.set("id", ci_id)
        etree.SubElement(ci, "name").text = clip.path.name
        etree.SubElement(ci, "enabled").text = "TRUE"
        etree.SubElement(ci, "duration").text = str(_ms_to_frames(src_out_ms - src_in_ms, fps))
        ci.append(_rate_elem(timebase, ntsc))
        etree.SubElement(ci, "start").text = str(_ms_to_frames(tl_start_ms, fps))
        etree.SubElement(ci, "end").text = str(_ms_to_frames(tl_end_ms, fps))
        etree.SubElement(ci, "in").text = str(_ms_to_frames(src_in_ms, fps))
        etree.SubElement(ci, "out").text = str(_ms_to_frames(src_out_ms, fps))

        # V1 で登録済みの file_id を参照
        p_str = str(clip.path.resolve())
        fid = seen_files.get(p_str, f"file-unknown-{ci_id}")
        f_elem = etree.SubElement(ci, "file")
        f_elem.set("id", fid)

        lnk = etree.SubElement(ci, "link")
        etree.SubElement(lnk, "linkclipref").text = link_video_id
        etree.SubElement(lnk, "mediatype").text = "video"
        etree.SubElement(lnk, "trackindex").text = "1"
        etree.SubElement(lnk, "clipindex").text = str(link_clipindex)

        src = etree.SubElement(ci, "sourcetrack")
        etree.SubElement(src, "mediatype").text = "audio"
        etree.SubElement(src, "trackindex").text = "1"

        return ci

    # ---- V1 / A1: カット済みクリップを配置（分割なし）----
    tl_cursor = 0
    for i, clip in enumerate(clips_info):
        fid = f"file-{i + 1}"
        v_id = _next_id("v")
        a_id = _next_id("a")
        clipindex = len(v1_track.findall("clipitem")) + 1

        v_ci = _make_video_clipitem(
            ci_id=v_id, clip=clip,
            tl_start_ms=tl_cursor, tl_end_ms=tl_cursor + clip.duration_ms,
            src_in_ms=0, src_out_ms=clip.duration_ms,
            file_id=fid, link_audio_id=a_id,
            link_clipindex=clipindex,
        )
        v1_track.append(v_ci)

        a_ci = _make_audio_clipitem(
            ci_id=a_id, clip=clip,
            tl_start_ms=tl_cursor, tl_end_ms=tl_cursor + clip.duration_ms,
            src_in_ms=0, src_out_ms=clip.duration_ms,
            link_video_id=v_id, link_clipindex=clipindex,
        )
        a1_track.append(a_ci)
        tl_cursor += clip.duration_ms

    # ---- V2: ズーム区間の調整レイヤーを配置 ----
    # Premiere Pro FCP7 XML の調整レイヤーは generatoritem ではなく clipitem +
    # mediaSource=Slug で表現される (Premiere Pro v26 実機エクスポートで確認済み)
    SLUG_RATE_TB   = 24       # Slug の内部 timebase (常に 24fps/non-drop)
    SLUG_DURATION  = 1036800  # Slug の合計 duration (12時間 @ 24fps)
    SLUG_IN_OFFSET = 86400    # Slug の in オフセット (1時間 @ 24fps)

    tl_fps = timebase / 1.001 if ntsc == "TRUE" else float(timebase)

    tl_cursor_v2 = 0
    for i, clip in enumerate(clips_info):
        for zoom_start_ms, zoom_end_ms in _get_zoom_intervals(i, clip):
            zoom_tl_start = tl_cursor_v2 + zoom_start_ms
            zoom_tl_end   = tl_cursor_v2 + zoom_end_ms
            dur_ms     = zoom_end_ms - zoom_start_ms
            dur_frames = _ms_to_frames(dur_ms, fps)

            # Slug rate (24fps/non-drop) でのソース in/out を計算
            slug_src_dur = int(round(dur_frames * SLUG_RATE_TB / tl_fps))
            slug_in  = SLUG_IN_OFFSET
            slug_out = SLUG_IN_OFFSET + slug_src_dur

            ci_id = _next_id("adj")
            mc_id = _next_id("adjmc")
            fid   = _next_id("adjfile")

            ci = etree.Element("clipitem")
            ci.set("id", ci_id)
            etree.SubElement(ci, "masterclipid").text = mc_id
            etree.SubElement(ci, "name").text = "調整レイヤー"
            etree.SubElement(ci, "enabled").text = "TRUE"
            etree.SubElement(ci, "duration").text = str(SLUG_DURATION)
            # clipitem の rate は Slug 固有 (24fps/non-drop)
            r = etree.SubElement(ci, "rate")
            etree.SubElement(r, "timebase").text = str(SLUG_RATE_TB)
            etree.SubElement(r, "ntsc").text = "FALSE"
            etree.SubElement(ci, "start").text = str(_ms_to_frames(zoom_tl_start, fps))
            etree.SubElement(ci, "end").text   = str(_ms_to_frames(zoom_tl_end,   fps))
            etree.SubElement(ci, "in").text    = str(slug_in)
            etree.SubElement(ci, "out").text   = str(slug_out)
            etree.SubElement(ci, "alphatype").text       = "none"
            etree.SubElement(ci, "pixelaspectratio").text = "square"
            etree.SubElement(ci, "anamorphic").text      = "FALSE"

            # file: mediaSource=Slug で調整レイヤーとして認識される
            f_elem = etree.SubElement(ci, "file")
            f_elem.set("id", fid)
            etree.SubElement(f_elem, "name").text        = "ブラック"
            etree.SubElement(f_elem, "mediaSource").text = "Slug"
            f_r = etree.SubElement(f_elem, "rate")
            etree.SubElement(f_r, "timebase").text = str(timebase)
            etree.SubElement(f_r, "ntsc").text     = ntsc
            tc = etree.SubElement(f_elem, "timecode")
            tc_r = etree.SubElement(tc, "rate")
            etree.SubElement(tc_r, "timebase").text = str(timebase)
            etree.SubElement(tc_r, "ntsc").text     = ntsc
            etree.SubElement(tc, "string").text        = "00;00;00;00" if ntsc == "TRUE" else "00:00:00:00"
            etree.SubElement(tc, "frame").text         = "0"
            etree.SubElement(tc, "displayformat").text = "DF" if ntsc == "TRUE" else "NDF"
            fm  = etree.SubElement(f_elem, "media")
            fv  = etree.SubElement(fm, "video")
            fsc = etree.SubElement(fv, "samplecharacteristics")
            fsc_r = etree.SubElement(fsc, "rate")
            etree.SubElement(fsc_r, "timebase").text = str(timebase)
            etree.SubElement(fsc_r, "ntsc").text     = ntsc
            etree.SubElement(fsc, "width").text            = "1920"
            etree.SubElement(fsc, "height").text           = "1080"
            etree.SubElement(fsc, "anamorphic").text       = "FALSE"
            etree.SubElement(fsc, "pixelaspectratio").text = "square"
            etree.SubElement(fsc, "fielddominance").text   = "none"

            v2_track.append(ci)
        tl_cursor_v2 += clip.duration_ms

    # ---- V3: インサート画像を配置 ----
    v3_track = etree.SubElement(video_elem, "track")
    etree.SubElement(v3_track, "enabled").text = "TRUE"
    etree.SubElement(v3_track, "locked").text = "FALSE"
    for i, (event, img_path) in enumerate(zip(inserts, insert_imgs)):
        if img_path is None:
            continue
        # start_ms/end_ms 形式を優先、古い at_ms 形式にもフォールバック
        at_ms = event.get("start_ms", event.get("at_ms", 0))
        end_ms = event.get("end_ms", at_ms + INSERT_MIN_DURATION_MS)
        dur_ms = max(end_ms - at_ms, INSERT_MIN_DURATION_MS)
        ci_id = _next_id("ins")
        fid = f"ins-file-{i + 1}"

        ci = etree.Element("clipitem")
        ci.set("id", ci_id)
        etree.SubElement(ci, "masterclipid").text = f"masterclip-{ci_id}"
        etree.SubElement(ci, "name").text = img_path.name
        etree.SubElement(ci, "enabled").text = "TRUE"
        etree.SubElement(ci, "duration").text = str(_ms_to_frames(dur_ms, fps))
        ci.append(_rate_elem(timebase, ntsc))
        etree.SubElement(ci, "start").text = str(_ms_to_frames(at_ms, fps))
        etree.SubElement(ci, "end").text = str(_ms_to_frames(at_ms + dur_ms, fps))
        etree.SubElement(ci, "in").text = "0"
        etree.SubElement(ci, "out").text = str(_ms_to_frames(dur_ms, fps))
        etree.SubElement(ci, "stillframe").text = "TRUE"
        etree.SubElement(ci, "alphatype").text = "none"
        etree.SubElement(ci, "pixelaspectratio").text = "square"
        etree.SubElement(ci, "anamorphic").text = "FALSE"

        f_elem = etree.SubElement(ci, "file")
        f_elem.set("id", fid)
        etree.SubElement(f_elem, "name").text = img_path.name
        etree.SubElement(f_elem, "pathurl").text = _pathurl(img_path)
        f_elem.append(_rate_elem(timebase, ntsc))
        etree.SubElement(f_elem, "duration").text = str(_ms_to_frames(dur_ms, fps))
        etree.SubElement(f_elem, "width").text = "1920"
        etree.SubElement(f_elem, "height").text = "1080"
        f_elem.append(_timecode_elem(timebase, ntsc))
        m = etree.SubElement(f_elem, "media")
        v = etree.SubElement(m, "video")
        sc = etree.SubElement(v, "samplecharacteristics")
        sc.append(_rate_elem(timebase, ntsc))
        etree.SubElement(sc, "width").text = "1920"
        etree.SubElement(sc, "height").text = "1080"
        etree.SubElement(sc, "anamorphic").text = "FALSE"
        etree.SubElement(sc, "pixelaspectratio").text = "square"
        etree.SubElement(sc, "fielddominance").text = "none"
        etree.SubElement(v, "stillframe").text = "TRUE"

        v3_track.append(ci)

    # ---- A2: 効果音を配置 ----
    sfx_map = {s["id"]: s for s in sfx_manifest.get("sfx", [])}
    for i, event in enumerate(sfx_events):
        sfx_entry = sfx_map.get(event["sfx_id"])
        if sfx_entry is None or sfx_dir is None:
            continue
        sfx_path = sfx_dir / sfx_entry["relative_path"]
        if not sfx_path.exists():
            print(f"  ⚠️ sfx ファイルが見つからない: {sfx_path}")
            continue

        at_ms = event["at_ms"]
        dur_ms = sfx_entry["duration_ms"]
        ci_id = _next_id("sfx")
        fid = f"sfx-file-{i + 1}"

        ci = etree.Element("clipitem")
        ci.set("id", ci_id)
        etree.SubElement(ci, "name").text = sfx_path.name
        etree.SubElement(ci, "enabled").text = "TRUE"
        etree.SubElement(ci, "duration").text = str(_ms_to_frames(dur_ms, fps))
        ci.append(_rate_elem(timebase, ntsc))
        etree.SubElement(ci, "start").text = str(_ms_to_frames(at_ms, fps))
        etree.SubElement(ci, "end").text = str(_ms_to_frames(at_ms + dur_ms, fps))
        etree.SubElement(ci, "in").text = "0"
        etree.SubElement(ci, "out").text = str(_ms_to_frames(dur_ms, fps))

        f_elem = etree.SubElement(ci, "file")
        f_elem.set("id", fid)
        etree.SubElement(f_elem, "name").text = sfx_path.name
        etree.SubElement(f_elem, "pathurl").text = _pathurl(sfx_path)
        f_elem.append(_rate_elem(timebase, ntsc))
        etree.SubElement(f_elem, "duration").text = str(_ms_to_frames(dur_ms, fps))
        m = etree.SubElement(f_elem, "media")
        aud = etree.SubElement(m, "audio")
        etree.SubElement(aud, "channelcount").text = "2"

        src = etree.SubElement(ci, "sourcetrack")
        etree.SubElement(src, "mediatype").text = "audio"
        etree.SubElement(src, "trackindex").text = "1"

        a2_track.append(ci)

    # sequence.duration を更新
    seq.find("duration").text = str(_ms_to_frames(tl_cursor, fps))


    xml_bytes = etree.tostring(root, pretty_print=True, encoding="UTF-8")
    xml_str = xml_bytes.decode()
    # Premiere Pro が要求する DOCTYPE 宣言と標準的なダブルクォート形式に整える
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + xml_str


# ===== メイン =====

def main():
    parser = argparse.ArgumentParser(description="YouTube用シーケンスを生成する")
    parser.add_argument("--clips", required=True, help="カット済みMP4フォルダのパス")
    parser.add_argument("--sfx-dir", default=None, help="効果音フォルダのパス（省略時 A2 なし）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="whisper.cpp モデルのパス")
    parser.add_argument("--output", default=None, help="出力先フォルダ（デフォルト: --clips/output/）")
    parser.add_argument("--reuse-analysis", action="store_true",
                        help="既存の analysis_debug.json を再利用してStep1(Whisper)/Step3(SRT)をスキップ。Step4/5のみ再実行")
    parser.add_argument("--skip-whisper", action="store_true",
                        help="既存の segments.json を再利用してWhisperをスキップ。Step2(Gemini分析)/Step3/Step4/5を再実行")
    parser.add_argument("--insert-target", default="",
                        help="インサート画像のターゲット指定（例: '日本人女性20-40代、リアルな3D女性画像多め'）")
    parser.add_argument("--no-insert", action="store_true",
                        help="インサート画像の生成をスキップする")
    parser.add_argument("--fast", action="store_true",
                        help="whisper-cliで高速文字起こし（デフォルト: WhisperX精度優先）")
    parser.add_argument("--experimental-gemini", action="store_true",
                        help="[実験的] Gemini APIで文字起こし（短尺5分以下推奨・長尺は不安定）")
    # 後方互換（非推奨・非表示）
    parser.add_argument("--use-whisperx", action="store_true",
                        help="[非推奨] デフォルトがWhisperXになったため不要。無視されます")
    parser.add_argument("--use-gemini-transcribe", action="store_true",
                        help="[非推奨] --experimental-gemini を使用してください")
    parser.add_argument("--only", default=None,
                        help="指定ステップのみ実行（カンマ区切り: sfx,zoom,insert,srt）")
    parser.add_argument("--skip", default=None,
                        help="指定ステップをスキップ（カンマ区切り: sfx,zoom,insert,srt）")
    args = parser.parse_args()

    # --only / --skip の解決
    valid_steps = {"sfx", "zoom", "insert", "srt"}
    enabled_steps = set(valid_steps)
    if args.only:
        enabled_steps = {s.strip() for s in args.only.split(",") if s.strip() in valid_steps}
        print(f"📋 --only 指定: {', '.join(sorted(enabled_steps))}")
    if args.skip:
        skip_steps = {s.strip() for s in args.skip.split(",") if s.strip() in valid_steps}
        enabled_steps -= skip_steps
        print(f"📋 --skip 指定: {', '.join(sorted(skip_steps))}")

    clips_dir = Path(args.clips)
    if not clips_dir.is_dir():
        print(f"エラー: --clips が見つからない: {clips_dir}")
        sys.exit(1)

    mp4_files = sorted(
        [f for f in clips_dir.iterdir() if f.suffix.lower() in {".mp4", ".mov", ".mxf"}],
        key=lambda f: _natural_sort_key(f.name),
    )
    if not mp4_files:
        print("エラー: --clips フォルダに MP4/MOV/MXF が見つからない")
        sys.exit(1)

    print(f"📂 クリップ数: {len(mp4_files)}")
    for f in mp4_files:
        print(f"   - {f.name}")

    out_dir = Path(args.output) if args.output else clips_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    inserts_dir = out_dir / "inserts"
    sfx_dir = Path(args.sfx_dir) if args.sfx_dir else None
    if sfx_dir and not sfx_dir.is_dir():
        print(f"エラー: --sfx-dir が見つからない: {sfx_dir}")
        sys.exit(1)

    # ---- Step 1 ----
    print("\n[Step 1] クリップ情報取得・文字起こし中...")
    clips_info = get_clip_durations(mp4_files)
    total_dur_ms = sum(c.duration_ms for c in clips_info)
    print(f"  合計尺: {total_dur_ms / 1000:.1f} 秒")

    if args.reuse_analysis:
        print("  --reuse-analysis: Whisper スキップ")
        segments = []
    elif args.skip_whisper:
        seg_json = out_dir / "segments.json"
        if not seg_json.exists():
            print(f"エラー: --skip-whisper が指定されましたが {seg_json} が見つかりません（先に通常実行してください）")
            sys.exit(1)
        raw = json.loads(seg_json.read_text(encoding="utf-8"))
        segments = [Segment(index=s["index"], start_ms=s["start_ms"], end_ms=s["end_ms"], text=s["text"]) for s in raw]
        print(f"  --skip-whisper: segments.json を再利用 ({len(segments)} セグメント)")
    else:
        model_path = args.model
        if not Path(model_path).exists():
            print(f"エラー: whisper モデルが見つからない: {model_path}")
            print(f"  ダウンロード: curl -L https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin -o {model_path}")
            sys.exit(1)
        # 後方互換: --use-gemini-transcribe → --experimental-gemini
        use_gemini = args.experimental_gemini or args.use_gemini_transcribe
        if args.use_whisperx:
            print("  ℹ️ --use-whisperx は非推奨（デフォルトがWhisperXになりました）")
        if args.use_gemini_transcribe:
            print("  ℹ️ --use-gemini-transcribe は非推奨。--experimental-gemini を使用してください")
        segments = concat_and_transcribe(clips_info, model_path,
                                         fast=args.fast,
                                         experimental_gemini=use_gemini)
        print(f"  whisper セグメント数: {len(segments)}")
        seg_json = out_dir / "segments.json"
        seg_json.write_text(
            json.dumps([{"index": s.index, "start_ms": s.start_ms, "end_ms": s.end_ms, "text": s.text} for s in segments],
                       ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  segments.json 保存: {seg_json}")

    # ---- Step 2 ----
    print("\n[Step 2] コンテンツ分析中...")
    if sfx_dir:
        sfx_manifest = build_sfx_manifest(sfx_dir)
        print(f"  sfx 数: {len(sfx_manifest['sfx'])}")
        if not sfx_manifest["sfx"]:
            print("  ⚠️ 対応する効果音ファイルが 0 件です。A2 トラックは空になります")
            print("     対応拡張子: .mp3 .wav .aiff .m4a")
    else:
        sfx_manifest = {"sfx": []}
        print("  --sfx-dir 未指定: A2 トラックなし")

    sfx_manifest_path = out_dir / "sfx_manifest.json"
    sfx_manifest_path.write_text(
        json.dumps(sfx_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  sfx_manifest.json 保存: {sfx_manifest_path}")

    if args.reuse_analysis:
        debug_path = out_dir / "analysis_debug.json"
        if not debug_path.exists():
            print(f"エラー: --reuse-analysis が指定されましたが {debug_path} が見つかりません")
            sys.exit(1)
        analysis = json.loads(debug_path.read_text(encoding="utf-8"))
        print(f"  --reuse-analysis: 既存の analysis_debug.json を再利用 ({debug_path})")
    else:
        analysis = analyze_with_gemini(segments, clips_info, sfx_manifest, args.insert_target)
        debug_path = out_dir / "analysis_debug.json"
        debug_path.write_text(
            json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  analysis_debug.json 保存: {debug_path}")

    key_points = analysis.get("key_points", [])
    sfx_events = analysis.get("sfx_events", []) if "sfx" in enabled_steps else []
    if not sfx_dir:
        sfx_events = []
    insert_events = analysis.get("insert_events", []) if "insert" in enabled_steps else []
    transition_events = analysis.get("transition_events", [])
    print(f"  key_points: {len(key_points)}, sfx_events: {len(sfx_events)}, insert_events: {len(insert_events)}, transitions: {len(transition_events)}")
    if sfx_dir and sfx_manifest["sfx"] and not sfx_events:
        print("  ⚠️ sfx ライブラリはあるが、分析結果の sfx_events は 0 件です")
    if sfx_events:
        sfx_events = _validate_sfx_intervals(sfx_events, total_dur_ms)

    # ---- Step 3: SRT (Step 2 の成否に依存しない) ----
    if "srt" not in enabled_steps:
        print("\n[Step 3] --only/--skip により SRT 生成スキップ")
    elif args.reuse_analysis:
        print("\n[Step 3] --reuse-analysis: SRT 生成スキップ")
    else:
        print("\n[Step 3] SRT 生成中...")
        srt_content = build_srt(segments, total_dur_ms, skip_refine=use_gemini)
        srt_path = out_dir / "captions.srt"
        srt_path.write_text(srt_content, encoding="utf-8")
        print(f"  ✅ captions.srt 保存: {srt_path}")

    # ---- インサート画像バリデーション ----
    if insert_events:
        validated = []
        seen_ranges = []
        for ev in insert_events:
            dur = ev.get("end_ms", 0) - ev.get("start_ms", 0)
            # 表示時間を3s-15sにクランプ
            if dur < 3000:
                ev["end_ms"] = ev["start_ms"] + 3000
                print(f"  ⚠️ インサート表示時間短すぎ → 3sに延長 (start_ms={ev['start_ms']})")
            elif dur > 15000:
                ev["end_ms"] = ev["start_ms"] + 15000
                print(f"  ⚠️ インサート表示時間長すぎ → 15sにカット (start_ms={ev['start_ms']})")
            # 重複排除（既存の範囲と50%以上重なる場合はスキップ）
            overlap = False
            for sr in seen_ranges:
                o_start = max(ev["start_ms"], sr[0])
                o_end = min(ev["end_ms"], sr[1])
                if o_end > o_start:
                    overlap_ratio = (o_end - o_start) / (ev["end_ms"] - ev["start_ms"])
                    if overlap_ratio > 0.5:
                        overlap = True
                        print(f"  ⚠️ インサート重複排除: {ev['start_ms']}ms〜{ev['end_ms']}ms")
                        break
            if not overlap:
                validated.append(ev)
                seen_ranges.append((ev["start_ms"], ev["end_ms"]))
        if len(validated) < len(insert_events):
            print(f"  📊 インサート検証: {len(insert_events)}件 → {len(validated)}件")
        insert_events = validated

    # ---- Step 4: 画像生成 ----
    if args.no_insert:
        print("\n[Step 4] --no-insert 指定 - 画像生成スキップ")
        insert_events = []
    insert_imgs: List[Optional[Path]] = [None] * len(insert_events)
    if insert_events:
        print(f"\n[Step 4] インサート画像生成中 ({len(insert_events)} 枚)...")
        try:
            insert_imgs = generate_insert_images(insert_events, inserts_dir)
        except Exception as e:
            print(f"  ⚠️ Step 4 全体失敗: {e} - インサートなしで続行")
    else:
        print("\n[Step 4] insert_events なし - 画像生成スキップ")

    # ---- Step 5: FCP7 XML ----
    print("\n[Step 5] FCP7 XML 生成中...")
    try:
        xml_content = build_fcp7_xml(
            clips_info=clips_info,
            key_points=key_points,
            inserts=insert_events,
            sfx_events=sfx_events,
            sfx_manifest=sfx_manifest,
            sfx_dir=sfx_dir,
            insert_imgs=insert_imgs,
        )
        xml_path = out_dir / "sequence.xml"
        xml_path.write_text(xml_content, encoding="utf-8")
        print(f"  ✅ sequence.xml 保存: {xml_path}")
    except Exception as e:
        print(f"  ⚠️ XML 生成失敗: {e}")
        import traceback
        traceback.print_exc()

    # ---- Step 6: レポート出力 ----
    highlight = analysis.get("highlight")
    thumbnail_ideas = analysis.get("thumbnail_ideas", [])
    if highlight or thumbnail_ideas:
        def _fmt(ms):
            s = int(ms / 1000)
            h, r = divmod(s, 3600)
            m, s = divmod(r, 60)
            return f"{h:02d}:{m:02d}:{s:02d}"

        lines = []
        if highlight:
            start = highlight.get("at_ms", 0)
            dur = highlight.get("duration_ms", 15000)
            lines.append("## ハイライト区間（切り抜き・ショート用）")
            lines.append(f"  {_fmt(start)} 〜 {_fmt(start + dur)}（{dur // 1000}秒）")
            lines.append(f"  理由: {highlight.get('reason', '')}")
            lines.append("")
        if thumbnail_ideas:
            lines.append("## サムネイル案")
            for i, idea in enumerate(thumbnail_ideas, 1):
                lines.append(f"  {i}. 【{idea.get('title', '')}】")
                lines.append(f"     {idea.get('description', '')}")
            lines.append("")
        report_text = "\n".join(lines)
        report_path = out_dir / "report.txt"
        report_path.write_text(report_text, encoding="utf-8")
        print(f"\n📋 レポート:\n{report_text}")
        print(f"  report.txt 保存: {report_path}")

    # ===== 処理完了サマリー =====
    import time as _summary_time
    engine = "Gemini" if use_gemini else ("whisper-cli (--fast)" if args.fast else "WhisperX")
    print(f"\n{'='*50}")
    print(f"✅ 完了!")
    print(f"  文字起こしエンジン : {engine}")
    print(f"  セグメント数       : {len(segments)}")
    print(f"  合計尺             : {total_dur_ms / 1000:.1f}秒")
    print(f"  出力先             : {out_dir}")
    print(f"{'='*50}")
    print("  - captions.srt  : Premiere Pro で Import → caption track 化（captionモード）")
    print("  - sequence.xml  : Premiere Pro で Import")
    print("  - sfx_manifest.json")
    if any(p is not None for p in insert_imgs):
        print("  - inserts/*.png")

    # テロップ装飾: 作業リスト自動生成
    classify_script = Path("~/.claude/skills/テロップ装飾/scripts/classify_telop.py").expanduser()
    if classify_script.exists() and srt_path.exists():
        print("\n🎨 テロップ装飾: 作業リストを生成中...")
        result = subprocess.run(
            ["python3", str(classify_script), "--srt", str(srt_path)],
            capture_output=False,
        )
        if result.returncode == 0:
            print(f"  ✅ 作業リスト.md 保存: {out_dir / '作業リスト.md'}")
        else:
            print("  ⚠️ 作業リスト生成に失敗しました（テロップ装飾はスキップ）")


if __name__ == "__main__":
    main()
