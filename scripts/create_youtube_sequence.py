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
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

# ===== 定数 =====

DEFAULT_MODEL = str(Path.home() / "ggml-large-v3.bin")
TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "base_sequence.xml"

MAX_CHARS = 21           # SRT 1セグメント最大文字数 (WhisperX精度向上により19→21)
MIN_DUR_MS = 700         # SRT 最短表示時間 (ms) (WhisperX精度向上により1000→700)
INSERT_MIN_DURATION_MS = 3000  # インサート画像の最短表示時間 (ms)
ZOOM_WINDOW_MS = 1200    # key_point 周辺のスケールアップ時間幅 (ms)

PRICE_PER_IMAGE = 0.101  # $0.101/枚 (gemini-3.1-flash-image / 2K / 標準ティア / 2026-08-31時点の公式料金表)
MAX_IMAGE_COST = float(os.environ.get("GEMINI_MAX_IMAGE_COST_USD", "1.0"))

SIZE_PRESETS = {
    "fhd": (1920, 1080),          # 横 (YouTube)
    "vertical": (1080, 1920),     # 縦 (TikTok / Shorts / リール)
    "square": (1080, 1080),
    "4k": (3840, 2160),
    "vertical-4k": (2160, 3840),
}
SEQ_WIDTH, SEQ_HEIGHT = SIZE_PRESETS["fhd"]  # main() で --size に合わせて上書きする
VERTICAL_MAX_CHARS = 14  # 縦・正方形のときの字幕1枚あたりの最大文字数

CAPTION_MODEL = "gemini-3.6-flash"  # 字幕テキスト修正に使うモデル
CAPTION_BATCH = 120                 # 字幕修正を Gemini に送る1回あたりのセグメント数

TERMS_FILENAME = "terms.txt"   # --clips フォルダにあれば自動で読み込む用語リスト
WHISPER_PROMPT_MAX_CHARS = 100  # Whisper に渡す用語の合計文字数の上限 (プロンプトは約224トークンまで)

# ===== データクラス =====

@dataclass
class Segment:
    index: int
    start_ms: int
    end_ms: int
    text: str
    # text と1文字ずつ対応する (start_ms, end_ms)。
    # WhisperX の alignment 結果がある時だけ入る。None の時は文字数から時刻を推定する
    char_ms: Optional[List[Tuple[int, int]]] = None


@dataclass
class ClipInfo:
    path: Path
    duration_ms: int   # タイムライン上の長さ (映像のフレーム数から計算)
    fps: float
    start_ms: int      # タイムライン上での累積開始時刻 (ms)
    has_audio: bool = True
    width: int = 0     # 表示上の幅 (スマホ縦撮りの回転を反映済み)
    height: int = 0


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


def _is_drop_frame(ntsc_val: str, timebase: Optional[int] = None) -> bool:
    """ドロップフレーム表記は 29.97 / 59.94 だけ (23.976 などは NDF)"""
    return ntsc_val == "TRUE" and (timebase is None or timebase in (30, 60))


def _timecode_string(ntsc_val: str, timebase: Optional[int] = None) -> str:
    return "00:00:00;00" if _is_drop_frame(ntsc_val, timebase) else "00:00:00:00"


def _rate_of(fps: float) -> Tuple[int, str]:
    """fps → (timebase, ntsc)。23.976→(24,TRUE)、29.97→(30,TRUE)、25→(25,FALSE)"""
    tb = int(round(fps))
    return tb, ("TRUE" if abs(fps - tb) > 0.01 else "FALSE")


def parse_fps(text: str) -> float:
    """'29.97' '30000/1001' '24' などを fps に。23.976/29.97/59.94 は正確な NTSC 値にする"""
    if "/" in text:
        n, d = text.split("/")
        return float(n) / float(d)
    v = float(text)
    for tb in (24, 30, 48, 60, 120):
        if abs(v - tb / 1.001) < 0.01:
            return tb / 1.001
    return v


def parse_size(text: str) -> Tuple[int, int]:
    """'vertical' などのプリセット名か '1080x1350' を (幅, 高さ) に"""
    t = text.strip().lower()
    if t in SIZE_PRESETS:
        return SIZE_PRESETS[t]
    m = re.fullmatch(r"(\d+)\s*[x×]\s*(\d+)", t)
    if not m:
        raise ValueError(f"--size は {', '.join(SIZE_PRESETS)} か 幅x高さ（例: 1080x1350）で指定してください: {text}")
    w, h = int(m.group(1)), int(m.group(2))
    if w % 2 or h % 2:
        raise ValueError(f"--size の幅・高さは偶数にしてください: {text}")
    return w, h


def _detect_image_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    return ".bin"


# ===== Step 1: クリップ情報取得・音声連結・文字起こし =====

def get_clip_durations(clips: List[Path]) -> List[ClipInfo]:
    """各クリップの長さ・fps を取得し、タイムライン上の開始位置を計算する。

    長さは「映像のフレーム数」で決める。Premiere のタイムライン上の長さは映像で決まるため。
    コンテナの長さ (format.duration) は音声の方が長いクリップで映像より長くなり、
    クリップ数が増えるほど XML の配置と文字起こしの時刻がずれる原因になる。
    """
    results = []
    cumulative_ms = 0.0
    for clip in clips:
        out = subprocess.check_output(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", str(clip)],
            text=True,
        )
        data = json.loads(out)
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        fmt_dur = float(data.get("format", {}).get("duration", 0) or 0)

        fps = 30.0
        frames = None
        if video:
            num, den = video.get("r_frame_rate", "30/1").split("/")
            if float(den) > 0 and float(num) > 0:
                fps = float(num) / float(den)
            if video.get("duration"):
                frames = int(round(float(video["duration"]) * fps))
            elif video.get("nb_frames", "").isdigit():
                frames = int(video["nb_frames"])
        if not frames or frames <= 0:
            frames = int(round(fmt_dur * fps))

        width = height = 0
        if video:
            width, height = int(video.get("width") or 0), int(video.get("height") or 0)
            rotation = 0
            for sd in video.get("side_data_list", []) or []:
                if "rotation" in sd:
                    rotation = int(float(sd["rotation"]))
            if not rotation and str(video.get("tags", {}).get("rotate", "")).lstrip("-").isdigit():
                rotation = int(video["tags"]["rotate"])
            if abs(rotation) % 180 == 90:
                width, height = height, width

        video_ms = frames * 1000.0 / fps
        start_ms = int(round(cumulative_ms))
        cumulative_ms += video_ms
        end_ms = int(round(cumulative_ms))

        results.append(ClipInfo(
            path=clip, duration_ms=end_ms - start_ms, fps=fps, start_ms=start_ms,
            has_audio=audio is not None, width=width, height=height,
        ))
    return results


def report_clip_timing(clips_info: List[ClipInfo]) -> List[str]:
    """クリップの長さの扱いを表示用の行で返す"""
    if not clips_info:
        return []
    lines = [f"クリップの長さは映像のフレーム数で計算し、文字起こし用の音声も各クリップを映像の長さに揃えて連結しています（{len(clips_info)}本）"]
    no_audio = [c for c in clips_info if not c.has_audio]
    if no_audio:
        lines.append(f"音声のないクリップ: {len(no_audio)}本（無音として扱います）")
    return lines


def _concat_audio(clips_info: List[ClipInfo], out_wav: str) -> None:
    """ffmpeg で全クリップを音声連結 → WAV (16kHz mono)

    各クリップの音声を、タイムライン上の長さ (映像の長さ) にぴったり揃えてから連結する。
    揃えずに連結すると、音声が映像より長いクリップ (AAC など) の差が積み重なり、
    後半ほど字幕・効果音・インサートの時刻が XML の映像とずれる。
    """
    inputs = []
    filters = []
    for k, c in enumerate(clips_info):
        d = f"{c.duration_ms / 1000:.6f}"
        if c.has_audio:
            inputs += ["-i", str(c.path)]
        else:
            inputs += ["-f", "lavfi", "-t", d, "-i", "anullsrc=r=16000:cl=mono"]
        filters.append(
            f"[{k}:a:0]aformat=sample_rates=16000:channel_layouts=mono,"
            f"asetpts=PTS-STARTPTS,atrim=duration={d},apad=whole_dur={d},"
            f"asetpts=PTS-STARTPTS[a{k}]"
        )
    n = len(clips_info)
    labels = "".join(f"[a{k}]" for k in range(n))
    filter_complex = ";".join(filters) + f";{labels}concat=n={n}:v=0:a=1[aout]"
    subprocess.run(
        ["ffmpeg", "-y"] + inputs +
        ["-filter_complex", filter_complex, "-map", "[aout]",
         "-ar", "16000", "-ac", "1", out_wav],
        check=True, capture_output=True,
    )

    # 連結後の長さがタイムラインと一致しているか確認
    try:
        got = float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", out_wav], text=True).strip())
        expected = (clips_info[-1].start_ms + clips_info[-1].duration_ms) / 1000
        if abs(got - expected) * 1000 > 1000.0 / clips_info[0].fps:
            print(f"  ⚠️ 連結音声の長さ {got:.3f}s がタイムライン {expected:.3f}s と1フレーム以上ずれています")
    except Exception:
        pass


GEMINI_CHUNK_SEC = 300  # 5分ごとにチャンク分割（課金済み65Kトークン対応）


def _transcribe_gemini(wav_path: str, max_chars: Optional[int] = None) -> List[Segment]:
    """Gemini API で高精度文字起こし → Segment[]
    戦略: 一括処理を試み、カバレッジ70%未満ならチャンク分割にフォールバック"""
    max_chars = max_chars or MAX_CHARS
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
                               chunk_dur_ms: int, max_chars: Optional[int] = None) -> List[Segment]:
    """単一チャンク/一括をGeminiで文字起こし（リトライ付き・タイムスタンプバリデーション）"""
    max_chars = max_chars or MAX_CHARS
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
            models = ["gemini-3.6-flash", "gemini-2.5-flash"]
            for model in models:
                try:
                    resp = client.models.generate_content(
                        model=model,
                        contents=[
                            types.Part.from_uri(file_uri=uploaded.uri, mime_type=uploaded.mime_type),
                            prompt,
                        ],
                    )
                    break
                except Exception as e:
                    if "503" not in str(e) or model == models[-1]:
                        raise  # 503以外のエラーはフォールバックしない (外側のリトライへ)
                    print(f"     ⚠️ 失敗 ({model}): {e} → {models[-1]} で再試行")
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


def _is_ja_space(text: str, i: int) -> bool:
    """日本語の文字どうしの間にある半角スペースか"""
    return (text[i] == " " and 0 < i < len(text) - 1
            and ord(text[i - 1]) > 0x7F and ord(text[i + 1]) > 0x7F)


def _clean_caption_text(text: str, times: Optional[List[Tuple[int, int]]] = None):
    """句読点「。」「、」と、日本語どうしの間の半角スペースを除く。
    times (1文字ごとの時刻) を渡すと、同じ位置の時刻も一緒に除いて返す"""
    keep = [i for i, ch in enumerate(text) if ch not in "。、" and not _is_ja_space(text, i)]
    new_text = "".join(text[i] for i in keep)
    new_times = [times[i] for i in keep] if times is not None else None
    return new_text, new_times


def _fill_times(known: List[Optional[Tuple[int, int]]], seg_start: int, seg_end: int) -> List[Tuple[int, int]]:
    """時刻がわからない文字 (None) を、前後のわかっている文字の時刻から均等に埋める"""
    n = len(known)
    out: List[Tuple[int, int]] = []
    i = 0
    prev_end = seg_start
    while i < n:
        if known[i] is not None:
            s, e = known[i]
            s = max(s, prev_end) if out else s
            e = max(e, s)
            out.append((s, e))
            prev_end = e
            i += 1
            continue
        j = i
        while j < n and known[j] is None:
            j += 1
        right = known[j][0] if j < n else seg_end
        right = max(right, prev_end)
        k = j - i
        for t in range(k):
            a = prev_end + (right - prev_end) * t // k
            b = prev_end + (right - prev_end) * (t + 1) // k
            out.append((a, b))
        prev_end = right
        i = j
    return out


def _uniform_times(n: int, start: int, end: int) -> List[Tuple[int, int]]:
    """文字数で均等に時刻を割り振る (単語時刻がない時の推定)"""
    return _fill_times([None] * n, start, end)


def _char_ms_from_words(text: str, words: List[Dict], seg_start: int, seg_end: int) -> Optional[List[Tuple[int, int]]]:
    """WhisperX の words (日本語は1文字ずつ) から、text の各文字の時刻を作る。
    alignment できなかった文字 (数字・記号など) は前後から埋める。1文字も取れなければ None"""
    src_chars: List[str] = []
    src_times: List[Optional[Tuple[int, int]]] = []
    for w in words:
        word = str(w.get("word", ""))
        if not word:
            continue
        if "start" in w and "end" in w and w["start"] is not None and w["end"] is not None:
            ws, we = int(float(w["start"]) * 1000), int(float(w["end"]) * 1000)
            for t in _uniform_times(len(word), ws, max(we, ws)):
                src_times.append(t)
        else:
            src_times.extend([None] * len(word))
        src_chars.extend(word)
    if not any(t is not None for t in src_times):
        return None
    known: List[Optional[Tuple[int, int]]] = [None] * len(text)
    sm = difflib.SequenceMatcher(None, "".join(src_chars), text, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                known[j1 + k] = src_times[i1 + k]
    if not any(t is not None for t in known):
        return None
    return _fill_times(known, seg_start, seg_end)


def _map_times(src_text: str, src_times: List[Tuple[int, int]], dst_text: str,
               seg_start: int, seg_end: int) -> List[Tuple[int, int]]:
    """修正前の文字の時刻を、修正後のテキストに引き継ぐ。
    同じ文字はそのまま、置き換えた部分は元の区間の時間を均等に分ける"""
    known: List[Optional[Tuple[int, int]]] = [None] * len(dst_text)
    sm = difflib.SequenceMatcher(None, src_text, dst_text, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                known[j1 + k] = src_times[i1 + k]
        elif tag == "replace" and i2 > i1:
            s, e = src_times[i1][0], src_times[i2 - 1][1]
            for k, t in enumerate(_uniform_times(j2 - j1, s, e)):
                known[j1 + k] = t
    return _fill_times(known, seg_start, seg_end)


def _split_text(text: str, max_chars: Optional[int] = None) -> List[str]:
    """max_chars 以内になるよう文節の区切りで分ける。返す配列をつなげると元の text になる"""
    max_chars = max_chars or MAX_CHARS
    if len(text) <= max_chars:
        return [text] if text else []

    split_points = _find_split_points(text, max_chars)
    if not split_points:
        # 分割候補がない場合は文字数で強制分割
        return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]

    chunks: List[str] = []
    prev = 0
    for sp in split_points:
        if sp - prev > max_chars and prev < sp:
            # この区間が長すぎる場合は強制分割
            portion = text[prev:sp]
            while len(portion) > max_chars:
                chunks.append(portion[:max_chars])
                portion = portion[max_chars:]
            if portion:
                chunks.append(portion)
            prev = sp
        elif sp - prev > 0:
            chunks.append(text[prev:sp])
            prev = sp
    if prev < len(text):
        remaining = text[prev:]
        if chunks and len(chunks[-1]) + len(remaining) <= max_chars:
            chunks[-1] += remaining
        else:
            while len(remaining) > max_chars:
                chunks.append(remaining[:max_chars])
                remaining = remaining[max_chars:]
            if remaining:
                chunks.append(remaining)

    # 短すぎるチャンクを隣と結合
    merged = [chunks[0]] if chunks else []
    for c in chunks[1:]:
        if len(merged[-1]) + len(c) <= max_chars:
            merged[-1] += c
        else:
            merged.append(c)
    return merged


def _segments_from_chunks(chunks: List[str], times: List[Tuple[int, int]],
                          seg_start: int, seg_end: int) -> List[Segment]:
    """チャンクごとの表示時刻を、文字の時刻から決める。
    同じ発話の中では、次の字幕が出るまで前の字幕を表示し続ける (ちらつき防止)"""
    starts: List[int] = []
    pos = 0
    for c in chunks:
        starts.append(times[pos][0] if pos < len(times) else seg_end)
        pos += len(c)
    if starts:
        starts[0] = min(seg_start, starts[0])

    out: List[Segment] = []
    for k, c in enumerate(chunks):
        s = max(starts[k], out[-1].start_ms if out else seg_start)
        e = starts[k + 1] if k + 1 < len(chunks) else max(seg_end, s)
        if out and e <= s:
            # 表示時間が取れない場合は前の字幕にまとめる
            out[-1].text += c
            continue
        out.append(Segment(index=0, start_ms=s, end_ms=max(e, s), text=c))
    if out:
        out[-1].end_ms = max(out[-1].end_ms, seg_end)
    return out


def _seg_times(seg: Segment) -> Tuple[List[Tuple[int, int]], bool]:
    """(1文字ごとの時刻, 推定かどうか) を返す"""
    if seg.char_ms and len(seg.char_ms) == len(seg.text):
        return list(seg.char_ms), False
    return _uniform_times(len(seg.text), seg.start_ms, seg.end_ms), True


def _local_split_segments(segments: List[Segment], max_chars: Optional[int] = None) -> List[Segment]:
    """Gemini不要のローカル分割。文節の区切りで分け、表示時刻は文字の時刻から決める。
    WhisperX の単語時刻がない時は、従来どおり文字数の比率で配分する"""
    max_chars = max_chars or MAX_CHARS
    result: List[Segment] = []
    for seg in segments:
        times, _ = _seg_times(seg)
        text, times = _clean_caption_text(seg.text, times)
        if not text:
            continue
        result.extend(_segments_from_chunks(_split_text(text, max_chars), times, seg.start_ms, seg.end_ms))
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


def load_terms(path: Path) -> List[str]:
    """用語リストを読む。1行1語 (正しい表記)。空行と # で始まる行は無視し、重複は1つにまとめる"""
    terms: List[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        term = line.strip()
        if term and not term.startswith("#") and term not in terms:
            terms.append(term)
    return terms


def find_terms_file(terms_arg: Optional[str], clips_dir: Path) -> Tuple[Optional[Path], bool]:
    """使う用語リストのパスを決める。戻り値: (パス or None, 自動で見つけたか)
    --terms の指定がなければ、--clips フォルダの terms.txt を使う"""
    if terms_arg:
        return Path(terms_arg).expanduser(), False
    auto = clips_dir / TERMS_FILENAME
    if auto.is_file():
        return auto, True
    return None, False


def whisper_prompt_from_terms(terms: List[str]) -> Tuple[str, int]:
    """Whisper に渡すプロンプトを作る。長すぎると先頭が切り捨てられるため、
    合計 WHISPER_PROMPT_MAX_CHARS 文字までの語だけを使う。戻り値: (プロンプト, 使った語数)"""
    picked: List[str] = []
    for term in terms:
        if len("、".join(picked + [term])) > WHISPER_PROMPT_MAX_CHARS:
            break
        picked.append(term)
    return "、".join(picked), len(picked)


def _transcribe_whisperx(wav_path: str, prompt: Optional[str] = None) -> List[Segment]:
    """WhisperX で文字起こし + forced alignment → Segment[]
    prompt: 用語リストから作ったプロンプト (initial_prompt として渡す)"""
    import whisperx
    import torch

    device = "cpu"  # MPS は whisperx の一部で非対応のため CPU で安定動作
    compute_type = "int8"  # M2 Air のメモリ節約

    print("  🔊 WhisperX: モデルロード中...")
    model = None
    if prompt:
        try:
            model = whisperx.load_model("large-v3", device, compute_type=compute_type, language="ja",
                                        asr_options={"initial_prompt": prompt})
            print("  🔊 WhisperX: 用語リストをプロンプトとして指定")
        except TypeError as e:
            # 古い whisperx は asr_options / initial_prompt に対応していないことがある
            print(f"  ⚠️ この whisperx は用語リストの指定に対応していません ({e}) → 指定なしで続行")
    if model is None:
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

    # Segment[] に変換 (alignment できた文字の時刻を char_ms として保持する)
    segments = []
    aligned = 0
    for i, seg in enumerate(result.get("segments", result if isinstance(result, list) else [])):
        start_ms = int(seg.get("start", 0) * 1000)
        end_ms = int(seg.get("end", 0) * 1000)
        text = seg.get("text", "").strip()
        if text:
            char_ms = _char_ms_from_words(text, seg.get("words") or [], start_ms, end_ms)
            if char_ms:
                aligned += 1
            segments.append(Segment(index=i + 1, start_ms=start_ms, end_ms=end_ms,
                                    text=text, char_ms=char_ms))
    print(f"  🔊 WhisperX: 文字ごとの時刻あり {aligned}/{len(segments)} セグメント")

    # メモリ解放
    del model
    if torch.backends.mps.is_available():
        torch.mps.empty_cache()

    return segments


def _transcribe_whisper_cli(wav_path: str, model_path: str, prompt: Optional[str] = None) -> List[Segment]:
    """whisper-cli フォールバック (prompt: 用語リストから作ったプロンプト)"""
    with tempfile.TemporaryDirectory() as tmpdir:
        srt_prefix = str(Path(tmpdir) / "output")
        cmd = ["whisper-cli", "-m", model_path, "-f", wav_path,
               "-l", "ja", "--output-srt", "-of", srt_prefix, "--max-len", "40"]
        if prompt:
            cmd += ["--prompt", prompt]
        subprocess.run(cmd, check=True)
        srt_path = Path(tmpdir) / "output.srt"
        if not srt_path.exists():
            raise RuntimeError("whisper-cli が SRT を生成しなかった")
        return _parse_srt(srt_path)


def concat_and_transcribe(clips_info: List[ClipInfo], model_path: str,
                          fast: bool = False, experimental_gemini: bool = False,
                          prompt: Optional[str] = None) -> List[Segment]:
    """ffmpeg で音声連結 → WhisperX(デフォルト) / whisper-cli(--fast) / Gemini(実験的) → Segment[]
    prompt: 用語リストから作ったプロンプト (WhisperX / whisper-cli に渡す)"""
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
                    segments = _transcribe_whisperx(wav_path, prompt)
                    if segments:
                        segments = _local_split_segments(segments, MAX_CHARS)
                        print(f"  ✅ WhisperX + ローカル分割完了: {len(segments)} セグメント")
                        return segments
                except Exception as e2:
                    print(f"  ⚠️ WhisperX も失敗: {e2} → whisper-cli へ")
                segments = _transcribe_whisper_cli(wav_path, model_path, prompt)
                segments = _local_split_segments(segments, MAX_CHARS)
                print(f"  ✅ whisper-cli + ローカル分割完了: {len(segments)} セグメント")
                return segments

        if fast:
            # --fast: whisper-cli で高速処理
            return _transcribe_whisper_cli(wav_path, model_path, prompt)

        # デフォルト: WhisperX（精度優先）→ 失敗時 whisper-cli フォールバック
        try:
            segments = _transcribe_whisperx(wav_path, prompt)
            if segments:
                print(f"  ✅ WhisperX 完了: {len(segments)} セグメント")
                return segments
            print("  ⚠️ WhisperX が空結果 → whisper-cli にフォールバック")
        except Exception as e:
            print(f"  ⚠️ WhisperX 失敗: {e} → whisper-cli にフォールバック")

        return _transcribe_whisper_cli(wav_path, model_path, prompt)


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
                    model="gemini-3.6-flash",
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

    for model in ["gemini-3.6-flash", "gemini-2.5-flash"]:
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

    for model in ["gemini-3.6-flash", "gemini-2.5-flash"]:
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

def _segments_to_srt(segments: List[Segment]) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_format_ts(seg.start_ms)} --> {_format_ts(seg.end_ms)}")
        lines.append(seg.text)
        lines.append("")
    return "\n".join(lines)


def build_srt(segments: List[Segment], audio_dur_ms: int, skip_refine: bool = False) -> str:
    """captionモード SRT を生成する (後方互換用。新しい処理は build_captions)"""
    return build_captions(segments, audio_dur_ms, skip_refine=skip_refine)["srt"]


def build_captions(segments: List[Segment], audio_dur_ms: int,
                   skip_refine: bool = False, fix_mode: str = "apply",
                   terms: Optional[List[str]] = None) -> Dict:
    """字幕を作る。

    fix_mode:
      apply   … Gemini の修正を字幕に反映し、修正した箇所をすべて一覧に残す (既定)
      propose … 字幕は音声認識のまま。Gemini の修正案を一覧にだけ出す
      off     … Gemini を使わない (ローカル分割のみ)

    terms: 用語リスト (正しい表記)。Gemini の修正で、明らかな聞き間違いをこの表記に直す

    戻り値: {"srt": 最終SRT, "asr_srt": 修正前SRT, "review": 確認リスト}
    """
    prepared = []  # (元のセグメント, 整形済みテキスト, 文字ごとの時刻, 時刻が推定か)
    for seg in segments:
        times, estimated = _seg_times(seg)
        text, times = _clean_caption_text(seg.text, times)
        if text:
            prepared.append((seg, text, times, estimated))

    # 修正前 (音声認識そのまま) の字幕
    asr_segs: List[Segment] = []
    asr_by_index: Dict[int, List[Segment]] = {}
    for seg, text, times, _ in prepared:
        parts = _segments_from_chunks(_split_text(text), times, seg.start_ms, seg.end_ms)
        asr_by_index[seg.index] = parts
        asr_segs.extend(parts)

    review: Dict = {
        "fix_mode": fix_mode,
        "items": [],
        "estimated_timing": sum(1 for p in prepared if p[3]),
        "total_segments": len(prepared),
        "failed_batches": 0,
        "note": "",
    }
    if terms:
        review["terms_count"] = len(terms)
        review["terms_in_fix"] = False

    corrections: Dict[int, Dict] = {}
    if skip_refine:
        review["note"] = "Gemini で文字起こし済みのため、字幕の修正処理は行っていません"
    elif fix_mode == "off":
        review["note"] = "--caption-fix off のため、字幕の修正処理は行っていません"
    elif not os.environ.get("GEMINI_API_KEY"):
        print("  ⚠️ GEMINI_API_KEY 未設定 → ローカル分割で処理")
        review["note"] = "GEMINI_API_KEY 未設定のため、字幕の修正処理は行っていません"
    else:
        corrections, failed = _gemini_correct_captions([(p[0].index, p[1]) for p in prepared], terms)
        review["failed_batches"] = failed
        if terms:
            review["terms_in_fix"] = True

    final: List[Segment] = []
    for seg, text, times, estimated in prepared:
        corr = corrections.get(seg.index)
        if corr is None:
            final.extend(asr_by_index[seg.index])
            continue

        lines = [ln for ln in (_clean_caption_text(l)[0] for l in corr["lines"]) if ln]
        corrected = "".join(lines)
        resplit = any(len(l) > MAX_CHARS for l in lines)
        if resplit:
            lines = [c for l in lines for c in _split_text(l)]

        changed = corrected != text
        at_ms = asr_by_index[seg.index][0].start_ms
        if changed:
            review["items"].append({
                "at_ms": at_ms,
                "before": text,
                "after": corrected,
                "diff": _describe_diff(text, corrected),
                "reasons": corr.get("reasons", []),
                # 全文削除の提案は字幕に反映しない (発話が丸ごと消えるのを防ぐ)
                "applied": fix_mode == "apply" and bool(lines),
                "timing_estimated": estimated,
            })

        if fix_mode != "apply" or not lines:
            # propose: 字幕は元のまま / 全文削除の提案も字幕からは消さない
            final.extend(asr_by_index[seg.index])
            continue

        new_times = _map_times(text, times, corrected, seg.start_ms, seg.end_ms) if changed else times
        final.extend(_segments_from_chunks(lines, new_times, seg.start_ms, seg.end_ms))

    final = _validate_srt_timestamps(_merge_short_segments(final), audio_dur_ms)
    asr_segs = _validate_srt_timestamps(_merge_short_segments(asr_segs), audio_dur_ms)
    return {"srt": _segments_to_srt(final), "asr_srt": _segments_to_srt(asr_segs), "review": review}


def _describe_diff(before: str, after: str) -> str:
    """変更点を「A → B」の形で短く書く"""
    parts = []
    sm = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace":
            parts.append(f"「{before[i1:i2]}」→「{after[j1:j2]}」")
        elif tag == "delete":
            parts.append(f"「{before[i1:i2]}」を削除")
        elif tag == "insert":
            parts.append(f"「{after[j1:j2]}」を追加")
    return "、".join(parts)


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


def _gemini_correct_captions(items: List[Tuple[int, str]],
                             terms: Optional[List[str]] = None) -> Tuple[Dict[int, Dict], int]:
    """Gemini に音声認識の誤りを直してもらい、字幕1枚ずつに分けた行と修正理由を受け取る。
    terms (用語リスト) があれば、その表記をこの案件の正しい表記として渡す。

    時刻は Gemini に任せない (時刻は WhisperX の文字ごとの時刻から決める)。
    戻り値: ({index: {"lines": [...], "reasons": [...]}}, 失敗したバッチ数)
    """
    import time as _time
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"timeout": 300000})
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "index": {"type": "integer"},
                "lines": {"type": "array", "items": {"type": "string"}},
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "before": {"type": "string"},
                            "after": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["before", "after", "reason"],
                    },
                },
            },
            "required": ["index", "lines", "changes"],
        },
    }

    terms_block = ""
    if terms:
        terms_block = (
            "\nこの案件の用語の正しい表記 (固有名詞・専門用語):\n"
            + "\n".join(f"- {t}" for t in terms)
            + "\n音声認識のテキストが、明らかにこれらの用語の聞き間違いである場合は、この表記に直す。"
            "聞き間違いかどうか自信がない場合は直さない。言い回し・口調・方言を直さないルール (2) はそのまま守り、"
            "直した箇所は必ず changes に書く\n"
        )

    result: Dict[int, Dict] = {}
    failed = 0
    batches = [items[i:i + CAPTION_BATCH] for i in range(0, len(items), CAPTION_BATCH)]
    for b, batch in enumerate(batches, 1):
        valid = {idx for idx, _ in batch}
        prompt = f"""以下は音声認識 (WhisperX) の結果です。字幕に使うため、各 index のテキストを整えて JSON 配列だけを返してください。

ルール:
1. 音声認識の誤り (同音異義語の取り違え・固有名詞の誤り・明らかな誤字脱字) だけを直す
2. 話し言葉の言い回し・口調・方言・言いよどみは直さない。話した言葉をそのまま残す
3. 自信がない箇所は直さず、元のまま残す
4. 句読点「。」「、」と半角スペースは入れない
5. lines は字幕1枚ずつの配列。1枚{MAX_CHARS}文字以内。lines をつなげると修正後の全文になるようにする
6. 分ける位置は文節の区切り (「〜は」「〜が」「〜で」「〜を」など助詞の直後) を優先し、単語の途中や「ん」の前では分けない
7. 直した箇所は changes に before / after / reason (短い日本語) で書く。直していなければ空配列
8. 入力のすべての index を1回ずつ返す
{terms_block}
入力:
{json.dumps([{"index": idx, "text": t} for idx, t in batch], ensure_ascii=False, indent=2)}
"""
        ok = False
        for attempt in range(3):
            try:
                print(f"  🤖 Gemini: 字幕テキスト修正中... (バッチ {b}/{len(batches)}・試行 {attempt + 1}/3)")
                resp = client.models.generate_content(
                    model=CAPTION_MODEL,
                    contents=prompt,
                    config={"response_mime_type": "application/json", "response_schema": schema},
                )
                for r in json.loads(resp.text):
                    idx = int(r.get("index", -1))
                    if idx in valid and idx not in result:
                        result[idx] = {
                            "lines": [str(x) for x in r.get("lines", [])],
                            "reasons": [
                                f"「{c.get('before', '')}」→「{c.get('after', '')}」: {c.get('reason', '')}"
                                for c in r.get("changes", [])
                            ],
                        }
                ok = True
                break
            except Exception as e:
                wait = 2 ** attempt
                print(f"  ⚠️ 字幕修正 失敗 (試行 {attempt + 1}/3): {e}")
                if attempt < 2:
                    _time.sleep(wait)
        if not ok:
            failed += 1
            print(f"  ⚠️ バッチ {b} は修正なし (音声認識のまま) で続行")
    return result, failed


def write_caption_review(out_dir: Path, review: Dict) -> Tuple[Path, Path]:
    """字幕の確認リスト (Markdown) と、place_markers.jsx 用のマーカー JSON を書き出す"""
    items = sorted(review["items"], key=lambda x: x["at_ms"])
    applied = review["fix_mode"] == "apply"

    md = ["# 字幕の確認リスト", ""]
    if review.get("note"):
        md += [f"- {review['note']}"]
    md += [
        f"- 字幕の修正: {'反映済み (captions.srt)。修正前は captions_asr.srt' if applied else '未反映。下の修正案を見て手で直してください'}",
        f"- 修正{'した' if applied else '案のある'}箇所: {len(items)}件",
    ]
    if review["failed_batches"]:
        md.append(f"- ⚠️ Gemini の修正に失敗したバッチ: {review['failed_batches']}件 (その部分は音声認識のまま)")
    if review["total_segments"] and review["estimated_timing"]:
        if review["estimated_timing"] == review["total_segments"]:
            md.append("- ⚠️ 単語ごとの時刻がないため、表示タイミングは文字数から推定しています (whisper-cli / Gemini 文字起こし)")
        else:
            md.append(f"- 表示タイミングを文字数から推定した発話: {review['estimated_timing']}/{review['total_segments']}件")
    if review.get("terms_count"):
        src = f"（{review['terms_source']}）" if review.get("terms_source") else ""
        md.append(f"- 用語リスト{src}: {review['terms_count']}語"
                  + (" を字幕の修正に使用" if review.get("terms_in_fix")
                     else "（字幕の修正は行っていないため、Gemini には渡していません）"))
    for line in review.get("clip_timing", []):
        md.append(f"- {line}")
    md.append("")

    if items:
        md += ["| 位置 | 現状 (音声認識) | 修正" + ("後" if applied else "案") + " | 変更点 | 理由 |",
               "|---|---|---|---|---|"]
        for it in items:
            reason = " / ".join(it["reasons"]) if it["reasons"] else "(理由の記載なし)"
            cells = [_format_ts(it["at_ms"])[:8], it["before"], it["after"], it["diff"], reason]
            md.append("| " + " | ".join(c.replace("|", "｜") for c in cells) + " |")
    else:
        md.append("修正箇所はありません。")
    md += ["", "固有名詞・専門用語・聞き取りにくい箇所は、修正の有無にかかわらず通しで確認してください。", ""]

    md_path = out_dir / "caption_review.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    markers = [{
        "at_ms": it["at_ms"],
        "name": "字幕:修正済み" if it["applied"] else "字幕:修正案",
        "comment": f"{it['before']} → {it['after']}"
                   + (f"（{' / '.join(it['reasons'])}）" if it["reasons"] else ""),
    } for it in items]
    json_path = out_dir / "caption_review.json"
    json_path.write_text(json.dumps({"caption_review": markers}, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path


# ===== Step 4: インサート画像生成 =====

def estimate_image_cost(n: int) -> float:
    return n * PRICE_PER_IMAGE


def generate_insert_images(
    insert_list: List[Dict], out_dir: Path
) -> List[Optional[Path]]:
    """Gemini で画像生成 → ffmpeg でシーケンスのサイズ (SEQ_WIDTH×SEQ_HEIGHT) の PNG にする。
    縦横比が違う場合は、引き伸ばさずに画面を覆うまで拡大して中央を切り抜く"""
    from google import genai

    out_dir.mkdir(parents=True, exist_ok=True)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  ⚠️ GEMINI_API_KEY 未設定 - 画像生成スキップ")
        return [None] * len(insert_list)

    n = len(insert_list)
    cost = estimate_image_cost(n)
    print(f"  📸 {n}枚 × ${PRICE_PER_IMAGE:.3f} = 推定 ${cost:.3f} USD (2K標準ティア換算)")
    if cost > MAX_IMAGE_COST:
        print(f"  ⛔ 推定コスト ${cost:.3f} が上限 ${MAX_IMAGE_COST} を超えるため中断")
        print(f"     環境変数 GEMINI_MAX_IMAGE_COST_USD で上限を変更できます")
        return [None] * n

    client = genai.Client(api_key=api_key, http_options={"timeout": 300000})
    results: List[Optional[Path]] = []
    aspect = _aspect_label(SEQ_WIDTH, SEQ_HEIGHT)
    orient = ("vertical" if SEQ_HEIGHT > SEQ_WIDTH else "square" if SEQ_HEIGHT == SEQ_WIDTH else "horizontal")
    use_aspect_config = True  # 縦横比の指定が使えない環境では、プロンプトでの指示だけにする

    for i, event in enumerate(insert_list):
        out_path = out_dir / f"insert_{i + 1:03d}.png"
        last_err = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    print(f"    🔄 {out_path.name} リトライ ({attempt}/2)...")
                from google.genai import types as genai_types
                jp_suffix = ". Japanese style, set in Japan, featuring Japanese people and Japanese elements. No Western or foreign people, no English text."
                jp_suffix += f" {orient.capitalize()} {aspect} composition."
                cfg = {"response_modalities": ["IMAGE", "TEXT"]}
                if use_aspect_config and aspect and hasattr(genai_types, "ImageConfig"):
                    cfg["image_config"] = genai_types.ImageConfig(aspect_ratio=aspect)
                try:
                    resp = client.models.generate_content(
                        model="gemini-3.1-flash-image",
                        contents=event["prompt_en"] + jp_suffix,
                        config=genai_types.GenerateContentConfig(**cfg),
                    )
                except Exception:
                    if "image_config" in cfg:
                        use_aspect_config = False  # 次の試行からは縦横比の指定なし
                    raise
                img_bytes = resp.candidates[0].content.parts[0].inline_data.data
                raw_ext = _detect_image_ext(img_bytes)
                raw_path = out_dir / f"insert_{i + 1:03d}{raw_ext}"
                raw_path.write_bytes(img_bytes)
                if raw_ext != ".png":
                    print(f"    ℹ️ {out_path.name}: 実体は {raw_ext} のため PNG に変換")
                if out_path.exists() and raw_path != out_path:
                    out_path.unlink()

                _fit_image(raw_path, out_path, SEQ_WIDTH, SEQ_HEIGHT)
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


def _aspect_label(w: int, h: int) -> Optional[str]:
    """Gemini の画像生成で指定できる縦横比のうち、一番近いもの"""
    choices = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4, "4:5": 4 / 5, "5:4": 5 / 4}
    r = w / h
    return min(choices, key=lambda k: abs(choices[k] - r))


def _fit_image(src: Path, dst: Path, w: int, h: int) -> None:
    """画像を w×h の PNG にする。縦横比が違う場合は、画面を覆うまで拡大して中央を切り抜く (引き伸ばさない)"""
    tmp = dst.with_name(dst.stem + "_fit.png")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
         "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
         "-frames:v", "1", str(tmp)],
        check=True, capture_output=True,
    )
    tmp.replace(dst)


# ===== Step 5: FCP7 XML 生成 =====

def ms_to_clip_index(at_ms: int, clips_info: List[ClipInfo]) -> int:
    """グローバル at_ms からクリップインデックスを導出する"""
    for i, c in enumerate(clips_info):
        if c.start_ms <= at_ms < c.start_ms + c.duration_ms:
            return i
    return len(clips_info) - 1


# ===== 音量・BGM =====

VOICE_PEAK_DB = -7.0      # 声のピーク目安 (-6〜-9dB)
SFX_PEAK_DB = -16.5       # 効果音のピーク目安 (-15〜-18dB)
BGM_PEAK_DB = -24.5       # 会話がない区間のBGMピーク目安 (-24〜-30dB)
BGM_DUCK_PEAK_DB = -28.5  # 会話中のBGMピーク目安
MAX_CLIP_GAIN_DB = 6.0    # Premiere のクリップ音量の上限 (+6dB)
SILENT_DB = -60.0         # これより小さいピークは無音として扱う


def _measure_peak_db(path: Path, start_ms: Optional[int] = None, dur_ms: Optional[int] = None) -> Optional[float]:
    """ffmpeg の volumedetect でピーク (dBFS) を測る。音声がなければ None"""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "info"]
    if start_ms is not None:
        cmd += ["-ss", f"{start_ms / 1000:.3f}"]
    if dur_ms is not None:
        cmd += ["-t", f"{dur_ms / 1000:.3f}"]
    cmd += ["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", r.stderr)
    if not m:
        return None
    return -120.0 if m.group(1) == "-inf" else float(m.group(1))


def _db_to_linear(db: float) -> float:
    return 10 ** (db / 20)


def plan_audio_levels(clips_info: List[ClipInfo], sfx_paths: Dict[str, Path],
                      voice_db: float, sfx_db: float) -> Dict:
    """声 (A1) と効果音 (A2) のピークを測り、目安に合わせるゲインを決める。
    Premiere のクリップ音量は +6dB までなので、それ以上必要なものは一覧に残す"""
    plan = {"voice": {}, "sfx": {}, "voice_short": [], "sfx_short": [], "voice_silent": [],
            "voice_after": [], "sfx_after": []}
    for i, c in enumerate(clips_info):
        if not c.has_audio:
            continue
        peak = _measure_peak_db(c.path)
        if peak is None or peak < SILENT_DB:
            plan["voice_silent"].append(c.path.name)
            continue
        want = voice_db - peak
        gain = min(want, MAX_CLIP_GAIN_DB)
        plan["voice"][i] = round(gain, 2)
        plan["voice_after"].append(peak + gain)
        if want > MAX_CLIP_GAIN_DB + 0.05:
            plan["voice_short"].append({"clip": c.path.name, "peak_db": round(peak, 1),
                                        "need_db": round(want, 1)})
    for sid, path in sfx_paths.items():
        peak = _measure_peak_db(path)
        if peak is None or peak < SILENT_DB:
            continue
        want = sfx_db - peak
        gain = min(want, MAX_CLIP_GAIN_DB)
        plan["sfx"][sid] = round(gain, 2)
        plan["sfx_after"].append(peak + gain)
        if want > MAX_CLIP_GAIN_DB + 0.05:
            plan["sfx_short"].append({"clip": path.name, "peak_db": round(peak, 1), "need_db": round(want, 1)})
    return plan


def _speech_intervals(segments: List[Segment], total_ms: int, pad_before: int = 200,
                      pad_after: int = 300, join_gap: int = 1500) -> List[Tuple[int, int]]:
    """会話がある区間。前後に少し余裕を持たせ、短い間は1つにまとめる (BGMが細かく上下しないように)"""
    spans = sorted((max(0, s.start_ms - pad_before), min(total_ms, s.end_ms + pad_after))
                   for s in segments if s.end_ms > s.start_ms)
    merged: List[List[int]] = []
    for a, b in spans:
        if merged and a - merged[-1][1] <= join_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def render_bgm(bgm_path: Path, out_wav: Path, total_ms: int, speech: List[Tuple[int, int]],
               bgm_db: float, duck_db: float, ramp_ms: int = 400,
               fade_in_ms: int = 1000, fade_out_ms: int = 3000) -> Dict:
    """BGM をループして全尺に敷き、会話中は自動で下げた WAV (48kHz/16bit/ステレオ) を書き出す。
    下げ方は Premiere のキーフレームではなく、音そのものに焼き込む (XML で確実に再現させるため)"""
    import wave
    import numpy as np

    src_peak = _measure_peak_db(bgm_path)
    if src_peak is None or src_peak < SILENT_DB:
        raise RuntimeError(f"BGM の音が取得できません: {bgm_path}")
    g_open, g_duck = bgm_db - src_peak, duck_db - src_peak

    # 音量の折れ線 (時刻 ms, dB)。会話の前後 ramp_ms で滑らかに上げ下げする。
    # 会話区間は join_gap (> 2 * ramp_ms) 以上離れているので、上げ下げの区間は重ならない
    pts: List[Tuple[float, float]] = []
    for a, b in speech:
        pts += [(a - ramp_ms, g_open), (a, g_duck), (b, g_duck), (b + ramp_ms, g_open)]
    if not pts:
        pts = [(0.0, g_open), (float(total_ms), g_open)]
    tx = np.array([p[0] for p in pts]) / 1000.0
    ty = np.array([p[1] for p in pts])

    sr, ch = 48000, 2
    total_samples = int(round(total_ms * sr / 1000))
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-stream_loop", "-1", "-i", str(bgm_path),
         "-t", f"{total_ms / 1000:.6f}", "-f", "s16le", "-ac", str(ch), "-ar", str(sr), "-"],
        stdout=subprocess.PIPE,
    )
    chunk = sr * 10  # 10秒ずつ処理
    written = 0
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        while written < total_samples:
            raw = proc.stdout.read(min(chunk, total_samples - written) * ch * 2)
            if not raw:
                break
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32).reshape(-1, ch)
            t = (written + np.arange(len(x))) / sr
            gain = 10 ** (np.interp(t, tx, ty) / 20)
            gain *= np.clip(t * 1000 / fade_in_ms, 0, 1) if fade_in_ms else 1
            gain *= np.clip((total_ms / 1000 - t) * 1000 / fade_out_ms, 0, 1) if fade_out_ms else 1
            y = np.clip(x * gain[:, None], -32768, 32767).astype(np.int16)
            w.writeframes(y.tobytes())
            written += len(x)
        if written < total_samples:
            w.writeframes(np.zeros((total_samples - written, ch), dtype=np.int16).tobytes())
    proc.stdout.close()
    proc.wait()

    # 書き出した音を実測 (フェード部分を除いた、会話中/会話なしの最大ピーク)
    def _peak_over(spans):
        peaks = [_measure_peak_db(out_wav, a, b - a) for a, b in spans if b - a >= 500]
        peaks = [p for p in peaks if p is not None]
        return round(max(peaks), 1) if peaks else None
    body = (fade_in_ms + ramp_ms, total_ms - fade_out_ms - ramp_ms)
    duck_spans = [(max(a + ramp_ms, body[0]), min(b - ramp_ms, body[1])) for a, b in speech]
    open_spans, cur = [], body[0]
    for a, b in speech:
        if a - ramp_ms > cur:
            open_spans.append((cur, min(a - ramp_ms, body[1])))
        cur = max(cur, b + ramp_ms)
    if cur < body[1]:
        open_spans.append((cur, body[1]))
    return {
        "source_peak_db": round(src_peak, 1),
        "measured_open_db": _peak_over(open_spans),
        "measured_duck_db": _peak_over(duck_spans),
        "speech_spans": len(speech),
    }


def write_audio_report(out_dir: Path, plan: Optional[Dict], bgm: Optional[Dict],
                       targets: Dict[str, float]) -> Path:
    """音量の設定結果を audio_report.md に書き出す"""
    md = ["# 音量の設定結果", "",
          "XML の音量 (Audio Levels) は、各クリップのピークを ffmpeg で測って計算した値です。",
          "Premiere で読み込んだあと、メーターで一度確認してください。", ""]
    md += ["| 種類 | 目安 | 設定後のピーク |", "|---|---|---|"]
    def _rng(vals):
        return f"{min(vals):.1f}〜{max(vals):.1f}dB（{len(vals)}本）" if vals else "—"
    if plan:
        md.append(f"| 声 (A1) | {targets['voice']:.1f}dB | {_rng(plan['voice_after'])} |")
        md.append(f"| 効果音 (A2) | {targets['sfx']:.1f}dB | {_rng(plan['sfx_after'])} |")
    if bgm:
        md.append(f"| BGM・会話なし (A3) | {targets['bgm']:.1f}dB | {bgm['measured_open_db']}dB（書き出した音を実測） |")
        md.append(f"| BGM・会話中 (A3) | {targets['bgm_duck']:.1f}dB | {bgm['measured_duck_db']}dB（書き出した音を実測） |")
    md.append("")
    short = (plan or {}).get("voice_short", []) + (plan or {}).get("sfx_short", [])
    if short:
        md += ["## +6dB では目安に届かないクリップ", "",
               "Premiere のクリップ音量は +6dB が上限です。下のクリップは +6dB にしてありますが、まだ小さいままです。",
               "オーディオゲインで上げるか、音声を補正したファイルに差し替えてください。", "",
               "| クリップ | 元のピーク | 必要なゲイン |", "|---|---|---|"]
        md += [f"| {v['clip']} | {v['peak_db']}dB | +{v['need_db']}dB |" for v in short]
        md.append("")
    if plan and plan["voice_silent"]:
        md += ["## 音が入っていないクリップ（音量は変更していません）", ""]
        md += [f"- {n}" for n in plan["voice_silent"]]
        md.append("")
    if bgm:
        md += ["## BGM", "",
               f"- `bgm_ducked.wav` を A3 に配置。曲をループして全尺に敷き、会話中（{bgm['speech_spans']}区間）は自動で下げています",
               "- 下げ方は音に焼き込んでいるため、Premiere 上のキーフレームはありません。BGM を変える場合は `--bgm` を変えて再実行してください",
               "- 曲のつなぎ目（ループ位置）は自然になっていない場合があります。聴いて確認してください", ""]
    path = out_dir / "audio_report.md"
    path.write_text("\n".join(md), encoding="utf-8")
    return path


def build_fcp7_xml(
    clips_info: List[ClipInfo],
    key_points: List[Dict],
    inserts: List[Dict],
    sfx_events: List[Dict],
    sfx_manifest: Dict,
    sfx_dir: Optional[Path],
    insert_imgs: List[Optional[Path]],
    audio_plan: Optional[Dict] = None,
    bgm_wav: Optional[Path] = None,
    seq_fps: Optional[float] = None,
    fit: str = "fill",
) -> str:
    """テンプレート差し替え方式で FCP7 XML を生成する

    audio_plan: plan_audio_levels() の結果。A1/A2 に音量 (Audio Levels) を入れる
    bgm_wav:    render_bgm() で書き出した BGM。A3 に全尺で配置する
    seq_fps:    シーケンスの fps (省略時は最初のクリップの fps)
    fit:        素材とシーケンスのサイズが違う時の拡大方法。fill=画面いっぱい / fit=全体が入る / none=100%のまま
    """
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

    # fps / timebase / ntsc (シーケンス全体: --fps か、最初のクリップの fps)
    fps = seq_fps or (clips_info[0].fps if clips_info else 30.0)
    timebase, ntsc = _rate_of(fps)

    # シーケンスのサイズ (--size)
    seq_sc = seq.find("media/video/format/samplecharacteristics")
    seq_sc.find("width").text = str(SEQ_WIDTH)
    seq_sc.find("height").text = str(SEQ_HEIGHT)

    # シーケンス・タイムコード・フォーマットの rate を上書き
    displayformat = "DF" if _is_drop_frame(ntsc, timebase) else "NDF"
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
        tc_string_elem.text = _timecode_string(ntsc, timebase)

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
        etree.SubElement(tc, "string").text = _timecode_string(ntsc_val, tb)
        etree.SubElement(tc, "frame").text = "0"
        etree.SubElement(tc, "displayformat").text = "DF" if _is_drop_frame(ntsc_val, tb) else "NDF"
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
        _append_range(ci, tl_start_ms, tl_end_ms, src_in_ms)

        p_str = str(clip.path.resolve())
        if p_str not in seen_files:
            # 初回のみフル定義 (file の rate・duration は素材そのもののfps)
            seen_files[p_str] = file_id
            f_elem = etree.SubElement(ci, "file")
            f_elem.set("id", file_id)
            etree.SubElement(f_elem, "name").text = clip.path.name
            etree.SubElement(f_elem, "pathurl").text = _pathurl(clip.path)
            f_elem.append(_rate_elem(*_rate_of(clip.fps)))
            etree.SubElement(f_elem, "duration").text = str(_ms_to_frames(clip.duration_ms, clip.fps))
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

    def _append_range(ci, tl_start_ms: int, tl_end_ms: int, src_in_ms: int):
        """start/end/in/out。out は in + (end − start) にして、タイムライン上の長さと必ず一致させる"""
        start_f = _ms_to_frames(tl_start_ms, fps)
        end_f = _ms_to_frames(tl_end_ms, fps)
        in_f = _ms_to_frames(src_in_ms, fps)
        etree.SubElement(ci, "start").text = str(start_f)
        etree.SubElement(ci, "end").text = str(end_f)
        etree.SubElement(ci, "in").text = str(in_f)
        etree.SubElement(ci, "out").text = str(in_f + end_f - start_f)

    def _scale_for(clip: ClipInfo) -> Optional[float]:
        """素材とシーケンスのサイズが違う時の拡大率 (%)。同じなら None"""
        if fit == "none" or not clip.width or not clip.height:
            return None
        sx, sy = SEQ_WIDTH / clip.width, SEQ_HEIGHT / clip.height
        scale = (max(sx, sy) if fit == "fill" else min(sx, sy)) * 100
        return None if abs(scale - 100) < 0.01 else round(scale, 3)

    def _motion_filter(scale_pct: float):
        """Basic Motion (スケール)。Premiere ではモーションの「スケール」になる"""
        flt = etree.Element("filter")
        eff = etree.SubElement(flt, "effect")
        etree.SubElement(eff, "name").text = "Basic Motion"
        etree.SubElement(eff, "effectid").text = "basic"
        etree.SubElement(eff, "effectcategory").text = "motion"
        etree.SubElement(eff, "effecttype").text = "motion"
        etree.SubElement(eff, "mediatype").text = "video"
        prm = etree.SubElement(eff, "parameter")
        etree.SubElement(prm, "parameterid").text = "scale"
        etree.SubElement(prm, "name").text = "Scale"
        etree.SubElement(prm, "valuemin").text = "0"
        etree.SubElement(prm, "valuemax").text = "1000"
        etree.SubElement(prm, "value").text = f"{scale_pct:g}"
        return flt

    def _level_filter(gain_db: float):
        """クリップ音量 (Audio Levels)。value はリニア値 (1.0 = 0dB)"""
        flt = etree.Element("filter")
        eff = etree.SubElement(flt, "effect")
        etree.SubElement(eff, "name").text = "Audio Levels"
        etree.SubElement(eff, "effectid").text = "audiolevels"
        etree.SubElement(eff, "effectcategory").text = "audiolevels"
        etree.SubElement(eff, "effecttype").text = "audiolevels"
        etree.SubElement(eff, "mediatype").text = "audio"
        prm = etree.SubElement(eff, "parameter")
        etree.SubElement(prm, "parameterid").text = "level"
        etree.SubElement(prm, "name").text = "Level"
        etree.SubElement(prm, "valuemin").text = "0"
        etree.SubElement(prm, "valuemax").text = "3.98109"
        etree.SubElement(prm, "value").text = f"{_db_to_linear(gain_db):.5f}"
        return flt

    voice_gain = (audio_plan or {}).get("voice", {})
    sfx_gain = (audio_plan or {}).get("sfx", {})

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
        _append_range(ci, tl_start_ms, tl_end_ms, src_in_ms)

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

    scaled_clips: List[Tuple[str, int, int, float]] = []

    # ---- V1 / A1: カット済みクリップを配置（分割なし）----
    # 開始位置は get_clip_durations が映像のフレーム数から計算した start_ms を使う
    # (ms の端数を足し続けると、クリップ数が多い時に1フレームの隙間・重なりが出るため)
    tl_cursor = 0
    for i, clip in enumerate(clips_info):
        tl_cursor = clip.start_ms
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
        scale = _scale_for(clip)
        if scale is not None:
            v_ci.append(_motion_filter(scale))
            scaled_clips.append((clip.path.name, clip.width, clip.height, scale))
        v1_track.append(v_ci)

        a_ci = _make_audio_clipitem(
            ci_id=a_id, clip=clip,
            tl_start_ms=tl_cursor, tl_end_ms=tl_cursor + clip.duration_ms,
            src_in_ms=0, src_out_ms=clip.duration_ms,
            link_video_id=v_id, link_clipindex=clipindex,
        )
        if i in voice_gain:
            a_ci.append(_level_filter(voice_gain[i]))
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
        tl_cursor_v2 = clip.start_ms
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
            etree.SubElement(fsc, "width").text            = str(SEQ_WIDTH)
            etree.SubElement(fsc, "height").text           = str(SEQ_HEIGHT)
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
        etree.SubElement(f_elem, "width").text = str(SEQ_WIDTH)
        etree.SubElement(f_elem, "height").text = str(SEQ_HEIGHT)
        f_elem.append(_timecode_elem(timebase, ntsc))
        m = etree.SubElement(f_elem, "media")
        v = etree.SubElement(m, "video")
        sc = etree.SubElement(v, "samplecharacteristics")
        sc.append(_rate_elem(timebase, ntsc))
        etree.SubElement(sc, "width").text = str(SEQ_WIDTH)
        etree.SubElement(sc, "height").text = str(SEQ_HEIGHT)
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
        if event["sfx_id"] in sfx_gain:
            ci.append(_level_filter(sfx_gain[event["sfx_id"]]))

        a2_track.append(ci)

    # ---- A3: BGM (会話中に下げた音を書き出したもの) を全尺で配置 ----
    if bgm_wav is not None:
        a3_track = etree.SubElement(audio_elem, "track")
        etree.SubElement(a3_track, "enabled").text = "TRUE"
        etree.SubElement(a3_track, "locked").text = "FALSE"
        frames = _ms_to_frames(tl_cursor, fps)
        ci = etree.SubElement(a3_track, "clipitem")
        ci.set("id", _next_id("bgm"))
        etree.SubElement(ci, "name").text = bgm_wav.name
        etree.SubElement(ci, "enabled").text = "TRUE"
        etree.SubElement(ci, "duration").text = str(frames)
        ci.append(_rate_elem(timebase, ntsc))
        etree.SubElement(ci, "start").text = "0"
        etree.SubElement(ci, "end").text = str(frames)
        etree.SubElement(ci, "in").text = "0"
        etree.SubElement(ci, "out").text = str(frames)
        f_elem = etree.SubElement(ci, "file")
        f_elem.set("id", "bgm-file-1")
        etree.SubElement(f_elem, "name").text = bgm_wav.name
        etree.SubElement(f_elem, "pathurl").text = _pathurl(bgm_wav)
        f_elem.append(_rate_elem(timebase, ntsc))
        etree.SubElement(f_elem, "duration").text = str(frames)
        m = etree.SubElement(f_elem, "media")
        aud = etree.SubElement(m, "audio")
        etree.SubElement(aud, "channelcount").text = "2"
        src = etree.SubElement(ci, "sourcetrack")
        etree.SubElement(src, "mediatype").text = "audio"
        etree.SubElement(src, "trackindex").text = "1"

    # sequence.duration を更新
    seq.find("duration").text = str(_ms_to_frames(tl_cursor, fps))

    if scaled_clips:
        sizes = sorted({f"{w}×{h}→{sc:g}%" for _, w, h, sc in scaled_clips})
        print(f"  📐 シーケンス {SEQ_WIDTH}×{SEQ_HEIGHT} とサイズが違う素材 {len(scaled_clips)}本にスケールを設定"
              f"（{fit}）: {', '.join(sizes)}")


    xml_bytes = etree.tostring(root, pretty_print=True, encoding="UTF-8")
    xml_str = xml_bytes.decode()
    # Premiere Pro が要求する DOCTYPE 宣言と標準的なダブルクォート形式に整える
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + xml_str


# ===== メイン =====

def main():
    global SEQ_WIDTH, SEQ_HEIGHT, MAX_CHARS
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
    parser.add_argument("--terms", default=None,
                        help=f"用語リスト（1行1語・UTF-8）。音声認識と字幕の修正に使う。省略時は --clips フォルダの {TERMS_FILENAME} があれば使う")
    parser.add_argument("--caption-fix", choices=["apply", "propose", "off"], default="apply",
                        help="字幕の修正: apply=反映して一覧に残す(既定) / propose=字幕は元のまま修正案だけ一覧に出す / off=修正しない")
    parser.add_argument("--size", default="fhd",
                        help="シーケンスのサイズ: fhd(1920x1080・デフォルト) / vertical(1080x1920) / square / 4k / vertical-4k / 幅x高さ")
    parser.add_argument("--fps", default=None,
                        help="シーケンスのfps（例: 23.976 / 29.97 / 30 / 59.94）。省略時は最初のクリップのfps")
    parser.add_argument("--fit", choices=["fill", "fit", "none"], default="fill",
                        help="素材とシーケンスのサイズが違う時: fill=画面いっぱい(はみ出しは切れる・デフォルト) / fit=全体が入る / none=100%%のまま")
    parser.add_argument("--max-chars", type=int, default=None,
                        help=f"字幕1枚の最大文字数（デフォルト: 横 {MAX_CHARS} / 縦・正方形 {VERTICAL_MAX_CHARS}）")
    parser.add_argument("--bgm", default=None,
                        help="BGMファイル。ループして全尺に敷き、会話中は自動で下げた bgm_ducked.wav を A3 に置く")
    parser.add_argument("--voice-db", type=float, default=VOICE_PEAK_DB,
                        help=f"声のピーク目安 dB（デフォルト {VOICE_PEAK_DB}。目安 -6〜-9）")
    parser.add_argument("--sfx-db", type=float, default=SFX_PEAK_DB,
                        help=f"効果音のピーク目安 dB（デフォルト {SFX_PEAK_DB}。目安 -15〜-18）")
    parser.add_argument("--bgm-db", type=float, default=BGM_PEAK_DB,
                        help=f"会話がない区間のBGMピーク dB（デフォルト {BGM_PEAK_DB}）")
    parser.add_argument("--bgm-duck-db", type=float, default=BGM_DUCK_PEAK_DB,
                        help=f"会話中のBGMピーク dB（デフォルト {BGM_DUCK_PEAK_DB}）")
    parser.add_argument("--no-levels", action="store_true",
                        help="声・効果音の音量をXMLに入れない（元の音量のまま）")
    parser.add_argument("--only", default=None,
                        help="指定ステップのみ実行（カンマ区切り: sfx,zoom,insert,srt）")
    parser.add_argument("--skip", default=None,
                        help="指定ステップをスキップ（カンマ区切り: sfx,zoom,insert,srt）")
    args = parser.parse_args()

    # シーケンスのサイズ・fps・字幕の文字数
    try:
        SEQ_WIDTH, SEQ_HEIGHT = parse_size(args.size)
        seq_fps = parse_fps(args.fps) if args.fps else None
    except ValueError as e:
        parser.error(str(e))
    if seq_fps is not None and not 1 <= seq_fps <= 240:
        parser.error(f"--fps の値が不正です: {args.fps}")
    MAX_CHARS = args.max_chars or (VERTICAL_MAX_CHARS if SEQ_HEIGHT >= SEQ_WIDTH else MAX_CHARS)

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

    # 用語リスト (--terms / --clips フォルダの terms.txt)
    terms: List[str] = []
    terms_path, terms_auto = find_terms_file(args.terms, clips_dir)
    if terms_path is not None:
        if not terms_path.is_file():
            print(f"エラー: --terms が見つからない: {terms_path}")
            sys.exit(1)
        terms = load_terms(terms_path)
        how = "を自動で読み込みました" if terms_auto else ""
        print(f"📖 用語リスト: {terms_path.name}{how}（{len(terms)}語）")
        if not terms:
            print("  ⚠️ 用語リストに有効な行がありません（空行と # の行は無視されます）")
    whisper_prompt = None
    if terms:
        whisper_prompt, n_prompt = whisper_prompt_from_terms(terms)
        if n_prompt < len(terms):
            print(f"  ⚠️ 音声認識に渡せるのは先頭 {n_prompt}語まで（合計{WHISPER_PROMPT_MAX_CHARS}文字まで）。"
                  f"残り {len(terms) - n_prompt}語は字幕の修正 (Gemini) だけで使います")

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
    total_dur_ms = clips_info[-1].start_ms + clips_info[-1].duration_ms
    print(f"  合計尺: {total_dur_ms / 1000:.1f} 秒")
    clip_timing_lines = report_clip_timing(clips_info)
    for line in clip_timing_lines:
        print(f"  ⏱ {line}")
    out_fps = seq_fps or clips_info[0].fps
    print(f"  シーケンス: {SEQ_WIDTH}×{SEQ_HEIGHT} / {out_fps:.3f}fps / 字幕は1枚{MAX_CHARS}文字まで")
    other_fps = sorted({round(c.fps, 3) for c in clips_info if abs(c.fps - out_fps) > 0.01})
    if other_fps:
        print(f"  ℹ️ シーケンスと違うfpsの素材: {', '.join(f'{f:g}fps' for f in other_fps)}"
              f"（{sum(1 for c in clips_info if abs(c.fps - out_fps) > 0.01)}本）。Premiere 側でフレームレートが変換されます")

    # 後方互換: --use-gemini-transcribe → --experimental-gemini
    use_gemini = args.experimental_gemini or args.use_gemini_transcribe
    srt_path = out_dir / "captions.srt"

    if args.reuse_analysis:
        print("  --reuse-analysis: Whisper スキップ")
        segments = []
    elif args.skip_whisper:
        seg_json = out_dir / "segments.json"
        if not seg_json.exists():
            print(f"エラー: --skip-whisper が指定されましたが {seg_json} が見つかりません（先に通常実行してください）")
            sys.exit(1)
        raw = json.loads(seg_json.read_text(encoding="utf-8"))
        segments = [Segment(index=s["index"], start_ms=s["start_ms"], end_ms=s["end_ms"], text=s["text"],
                            char_ms=[tuple(t) for t in s["char_ms"]] if s.get("char_ms") else None)
                    for s in raw]
        print(f"  --skip-whisper: segments.json を再利用 ({len(segments)} セグメント)")
        if segments and not any(s.char_ms for s in segments):
            print("  ℹ️ segments.json に文字ごとの時刻がありません（旧形式）。表示タイミングは文字数から推定します")
    else:
        model_path = args.model
        if not Path(model_path).exists():
            print(f"エラー: whisper モデルが見つからない: {model_path}")
            print(f"  ダウンロード: curl -L https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin -o {model_path}")
            sys.exit(1)
        if args.use_whisperx:
            print("  ℹ️ --use-whisperx は非推奨（デフォルトがWhisperXになりました）")
        if args.use_gemini_transcribe:
            print("  ℹ️ --use-gemini-transcribe は非推奨。--experimental-gemini を使用してください")
        segments = concat_and_transcribe(clips_info, model_path,
                                         fast=args.fast,
                                         experimental_gemini=use_gemini,
                                         prompt=whisper_prompt or None)
        print(f"  whisper セグメント数: {len(segments)}")
        seg_json = out_dir / "segments.json"
        seg_json.write_text(
            json.dumps([{"index": s.index, "start_ms": s.start_ms, "end_ms": s.end_ms, "text": s.text,
                         **({"char_ms": [list(t) for t in s.char_ms]} if s.char_ms else {})}
                        for s in segments],
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
        captions = build_captions(segments, total_dur_ms, skip_refine=use_gemini, fix_mode=args.caption_fix,
                                  terms=terms or None)
        srt_path.write_text(captions["srt"], encoding="utf-8")
        print(f"  ✅ captions.srt 保存: {srt_path}")
        asr_path = out_dir / "captions_asr.srt"
        asr_path.write_text(captions["asr_srt"], encoding="utf-8")
        print(f"  ✅ captions_asr.srt 保存（修正前の字幕）: {asr_path}")
        captions["review"]["clip_timing"] = clip_timing_lines
        if terms:
            captions["review"]["terms_source"] = terms_path.name
        md_path, marker_path = write_caption_review(out_dir, captions["review"])
        n_fix = len(captions["review"]["items"])
        print(f"  ✅ caption_review.md 保存（修正{'した' if args.caption_fix == 'apply' else '案のある'}箇所 {n_fix}件）: {md_path}")
        print(f"  ✅ caption_review.json 保存（place_markers.jsx 用）: {marker_path}")

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

    # ---- 音量・BGM ----
    audio_plan = None
    bgm_wav = None
    bgm_result = None
    if not args.no_levels:
        print("\n[音量] 声・効果音のピークを測定中...")
        used_sfx: Dict[str, Path] = {}
        if sfx_dir:
            sfx_map = {s["id"]: s for s in sfx_manifest.get("sfx", [])}
            for ev in sfx_events:
                ent = sfx_map.get(ev.get("sfx_id"))
                if ent and (sfx_dir / ent["relative_path"]).exists():
                    used_sfx[ev["sfx_id"]] = sfx_dir / ent["relative_path"]
        audio_plan = plan_audio_levels(clips_info, used_sfx, args.voice_db, args.sfx_db)
        print(f"  声: {len(audio_plan['voice'])}本 / 効果音: {len(audio_plan['sfx'])}種類 に音量を設定")
        if audio_plan["voice_short"]:
            print(f"  ⚠️ +6dB では目安に届かないクリップ: {len(audio_plan['voice_short'])}本（audio_report.md 参照）")
    if args.bgm:
        bgm_src = Path(args.bgm).expanduser()
        if not bgm_src.exists():
            print(f"  ⚠️ BGMファイルが見つかりません: {bgm_src} - BGMなしで続行")
        else:
            speech_segs = segments
            if not speech_segs and (out_dir / "segments.json").exists():
                speech_segs = [Segment(index=s["index"], start_ms=s["start_ms"], end_ms=s["end_ms"], text=s["text"])
                               for s in json.loads((out_dir / "segments.json").read_text(encoding="utf-8"))]
            if not speech_segs:
                print("  ⚠️ 会話の位置がわからないため、BGMは全体を会話中の音量にします")
                speech = [(0, total_dur_ms)]
            else:
                speech = _speech_intervals(speech_segs, total_dur_ms)
            print(f"\n[BGM] {bgm_src.name} をループして書き出し中（会話中 {len(speech)}区間は自動で下げる）...")
            try:
                bgm_wav = out_dir / "bgm_ducked.wav"
                bgm_result = render_bgm(bgm_src, bgm_wav, total_dur_ms, speech, args.bgm_db, args.bgm_duck_db)
                print(f"  ✅ bgm_ducked.wav 保存（実測: 会話なし {bgm_result['measured_open_db']}dB / "
                      f"会話中 {bgm_result['measured_duck_db']}dB）")
            except ImportError:
                print("  ⚠️ BGMの書き出しには numpy が必要です（pip install numpy）- BGMなしで続行")
                bgm_wav = None
            except Exception as e:
                print(f"  ⚠️ BGMの書き出しに失敗: {e} - BGMなしで続行")
                bgm_wav = None
    if audio_plan or bgm_result:
        rp = write_audio_report(out_dir, audio_plan, bgm_result, {
            "voice": args.voice_db, "sfx": args.sfx_db, "bgm": args.bgm_db, "bgm_duck": args.bgm_duck_db})
        print(f"  ✅ audio_report.md 保存: {rp}")

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
            audio_plan=audio_plan,
            bgm_wav=bgm_wav,
            seq_fps=seq_fps,
            fit=args.fit,
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
    print("  - caption_review.md   : 字幕の修正箇所と理由（現状 → 修正 → 理由）")
    print("  - caption_review.json : place_markers.jsx で読み込むと、修正箇所にマーカーが付く")
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
