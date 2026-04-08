#!/usr/bin/env python3
"""
auto_cut.py - 未カット動画素材の自動前処理

無音区間・フィラーワード・言い直しを検出してカットし、
クリーンなクリップに分割する。

使い方:
  python3 auto_cut.py --input /path/to/raw.mp4 [--output /path/to/clips/] [--silence-only] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    import whisperx
    WHISPERX_AVAILABLE = True
except ImportError:
    WHISPERX_AVAILABLE = False


@dataclass
class Config:
    silence_threshold_db: float = -45.0
    silence_min_duration_sec: float = 0.5
    filler_words: list[str] = field(
        default_factory=lambda: ["えーと", "えー", "あの", "あのー", "まあ", "なんか", "うーん", "そのー"]
    )
    detect_rephrase: bool = True
    padding_before_sec: float = 0.15
    padding_after_sec: float = 0.10
    min_clip_duration_sec: float = 2.0

    @classmethod
    def from_json(cls, path: Path) -> "Config":
        if not path.exists():
            print(f"  ⚠️ 設定ファイル未検出: {path} → デフォルト値を使用")
            return cls()
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Region:
    start: float
    end: float
    kind: str  # "silence" | "filler" | "rephrase"
    text: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Clip:
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


# ===== Step 1: 無音区間検出 =====

def detect_silence(input_path: Path, config: Config) -> list[Region]:
    print("[Step 1] 無音区間検出中...")
    cmd = [
        "ffmpeg", "-i", str(input_path),
        "-af", f"silencedetect=noise={config.silence_threshold_db}dB:d={config.silence_min_duration_sec}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    regions: list[Region] = []
    starts: list[float] = []
    for line in result.stderr.splitlines():
        m_start = re.search(r"silence_start: ([\d.]+)", line)
        m_end = re.search(r"silence_end: ([\d.]+)", line)
        if m_start:
            starts.append(float(m_start.group(1)))
        if m_end and starts:
            s = starts.pop(0)
            e = float(m_end.group(1))
            regions.append(Region(start=s, end=e, kind="silence"))

    print(f"  無音区間: {len(regions)} 個")
    return regions


# ===== Step 2: フィラー・言い直し検出 =====

def transcribe(input_path: Path) -> list[dict]:
    if WHISPERX_AVAILABLE:
        try:
            print("  🔊 WhisperX で文字起こし中...")
            import torch
            device = "cpu"
            model = whisperx.load_model("large-v3", device, compute_type="int8", language="ja")
            audio = whisperx.load_audio(str(input_path))
            result = model.transcribe(audio, batch_size=4, language="ja")
            del model
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
            return result.get("segments", [])
        except Exception as e:
            print(f"  ⚠️ WhisperX 失敗: {e} → whisper-cli にフォールバック")

    print("  🔊 whisper-cli で文字起こし中...")
    with tempfile.TemporaryDirectory() as tmpdir:
        wav_path = str(Path(tmpdir) / "audio.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(input_path), "-ar", "16000", "-ac", "1", wav_path],
            check=True, capture_output=True,
        )
        srt_prefix = str(Path(tmpdir) / "output")
        subprocess.run(
            ["whisper-cli", "-m", str(Path.home() / "ggml-large-v3.bin"),
             "-f", wav_path, "-l", "ja", "--output-srt", "-of", srt_prefix, "--max-len", "50"],
            check=True,
        )
        srt_path = Path(tmpdir) / "output.srt"
        if not srt_path.exists():
            return []
        # SRTをセグメント形式に変換
        content = srt_path.read_text(encoding="utf-8")
        segments = []
        for block in content.split("\n\n"):
            lines = block.strip().splitlines()
            if len(lines) < 3:
                continue
            m = re.match(r"(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})", lines[1])
            if not m:
                continue
            def _tc(t):
                h, mi, rest = t.split(":")
                s, ms = rest.split(",")
                return int(h)*3600 + int(mi)*60 + int(s) + int(ms)/1000
            segments.append({
                "start": _tc(m.group(1)),
                "end": _tc(m.group(2)),
                "text": " ".join(lines[2:]).strip(),
            })
        return segments


CHUNK_SEC = 300  # 5分チャンク

def detect_filler_and_rephrase(segments: list[dict], config: Config) -> tuple[list[Region], list[Region]]:
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  ⚠️ GEMINI_API_KEY 未設定 → フィラー検出スキップ")
        return [], []

    client = genai.Client(api_key=api_key, http_options={"timeout": 300000})
    total_end = segments[-1]["end"] if segments else 0
    all_filler: list[Region] = []
    all_rephrase: list[Region] = []

    chunk_start = 0.0
    while chunk_start < total_end:
        chunk_end = chunk_start + CHUNK_SEC
        chunk_segs = [s for s in segments if s["start"] >= chunk_start and s["start"] < chunk_end]
        if chunk_segs:
            filler_list = "、".join(config.filler_words)
            seg_text = "\n".join(f"[{s['start']:.3f}s-{s['end']:.3f}s] {s.get('text', '')}" for s in chunk_segs)
            rephrase_inst = "- 言い直し: 前の発言を取り消す表現（例:「いや、そうじゃなくて」）" if config.detect_rephrase else "- rephrase_regionsは空配列で返すこと"

            prompt = f"""以下は動画の文字起こしです。フィラー区間と言い直し区間をJSONで返してください。

フィラーワード: {filler_list}
{rephrase_inst}
start_ms/end_msはミリ秒単位。

文字起こし:
{seg_text}"""

            schema = {
                "type": "object",
                "properties": {
                    "filler_regions": {"type": "array", "items": {"type": "object", "properties": {"start_ms": {"type": "integer"}, "end_ms": {"type": "integer"}, "text": {"type": "string"}}, "required": ["start_ms", "end_ms", "text"]}},
                    "rephrase_regions": {"type": "array", "items": {"type": "object", "properties": {"start_ms": {"type": "integer"}, "end_ms": {"type": "integer"}, "text": {"type": "string"}}, "required": ["start_ms", "end_ms", "text"]}},
                },
                "required": ["filler_regions", "rephrase_regions"],
            }

            try:
                print(f"  🤖 Gemini: フィラー検出中 ({chunk_start:.0f}s-{chunk_end:.0f}s)...")
                resp = client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=prompt,
                    config={"response_mime_type": "application/json", "response_schema": schema},
                )
                data = json.loads(resp.text)
                for r in data.get("filler_regions", []):
                    all_filler.append(Region(start=r["start_ms"]/1000, end=r["end_ms"]/1000, kind="filler", text=r.get("text", "")))
                for r in data.get("rephrase_regions", []):
                    all_rephrase.append(Region(start=r["start_ms"]/1000, end=r["end_ms"]/1000, kind="rephrase", text=r.get("text", "")))
            except Exception as e:
                print(f"  ⚠️ Gemini フィラー検出失敗: {e}")
        chunk_start = chunk_end

    print(f"  フィラー: {len(all_filler)} 個、言い直し: {len(all_rephrase)} 個")
    return all_filler, all_rephrase


# ===== Step 3: カット区間統合 & クリップ分割 =====

def merge_regions(regions: list[Region], config: Config) -> list[Region]:
    if not regions:
        return []
    padded = [Region(start=max(0, r.start - config.padding_before_sec), end=r.end + config.padding_after_sec, kind=r.kind, text=r.text) for r in regions]
    padded.sort(key=lambda r: r.start)
    merged = [padded[0]]
    for cur in padded[1:]:
        prev = merged[-1]
        if cur.start <= prev.end:
            merged[-1] = Region(start=prev.start, end=max(prev.end, cur.end), kind=prev.kind, text=prev.text)
        else:
            merged.append(cur)
    return merged


def compute_clips(cut_regions: list[Region], total_duration: float, config: Config) -> list[Clip]:
    clips: list[Clip] = []
    cursor = 0.0
    for region in cut_regions:
        if region.start > cursor:
            clips.append(Clip(index=len(clips)+1, start=cursor, end=region.start))
        cursor = max(cursor, region.end)
    if cursor < total_duration:
        clips.append(Clip(index=len(clips)+1, start=cursor, end=total_duration))

    # 短いクリップを統合
    changed = True
    while changed:
        changed = False
        new_clips: list[Clip] = []
        i = 0
        while i < len(clips):
            clip = clips[i]
            if clip.duration < config.min_clip_duration_sec:
                if new_clips:
                    prev = new_clips.pop()
                    new_clips.append(Clip(index=prev.index, start=prev.start, end=max(prev.end, clip.end)))
                    changed = True
                elif i + 1 < len(clips):
                    new_clips.append(Clip(index=clip.index, start=clip.start, end=clips[i+1].end))
                    i += 1
                    changed = True
                else:
                    new_clips.append(clip)
            else:
                new_clips.append(clip)
            i += 1
        clips = new_clips

    for i, c in enumerate(clips):
        c.index = i + 1
    return clips


def get_video_duration(input_path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(input_path)], text=True)
    return float(json.loads(out)["format"]["duration"])


# ===== Step 4: クリップ書き出し =====

def export_clips(input_path: Path, output_dir: Path, clips: list[Clip], dry_run: bool) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for clip in clips:
        out = output_dir / f"clip_{clip.index:03d}.mp4"
        paths.append(out)
        if dry_run:
            print(f"  [dry-run] {out.name}: {clip.start:.2f}s - {clip.end:.2f}s ({clip.duration:.2f}s)")
            continue
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(input_path), "-ss", f"{clip.start:.6f}", "-to", f"{clip.end:.6f}", "-c", "copy", str(out)],
            check=True, capture_output=True,
        )
        print(f"  ✅ {out.name}: {clip.start:.2f}s - {clip.end:.2f}s ({clip.duration:.2f}s)")
    return paths


# ===== Step 5: レポート =====

def save_report(output_dir: Path, input_path: Path, total_dur: float, silence: list[Region], filler: list[Region], rephrase: list[Region], clips: list[Clip], dry_run: bool) -> Path:
    report = {
        "input": str(input_path),
        "dry_run": dry_run,
        "source_duration_sec": round(total_dur, 3),
        "total_cut_sec": round(sum(r.duration for r in silence + filler + rephrase), 3),
        "output_clip_count": len(clips),
        "breakdown": {
            "silence": {"count": len(silence), "total_sec": round(sum(r.duration for r in silence), 3)},
            "filler": {"count": len(filler), "total_sec": round(sum(r.duration for r in filler), 3)},
            "rephrase": {"count": len(rephrase), "total_sec": round(sum(r.duration for r in rephrase), 3)},
        },
        "clips": [{"index": c.index, "start_sec": round(c.start, 3), "end_sec": round(c.end, 3), "duration_sec": round(c.duration, 3)} for c in clips],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "auto_cut_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  📊 レポート保存: {path}")
    return path


# ===== メイン =====

def main():
    parser = argparse.ArgumentParser(description="未カット動画素材の自動前処理")
    parser.add_argument("--input", required=True, help="入力MP4ファイルパス")
    parser.add_argument("--output", default=None, help="出力先フォルダ（デフォルト: 入力ファイルの隣にclips/）")
    parser.add_argument("--config", default=str(Path(__file__).parent.parent / "configs" / "default.json"), help="設定JSONファイルパス")
    parser.add_argument("--silence-only", action="store_true", help="無音検出のみ（フィラー/言い直し検出をスキップ）")
    parser.add_argument("--dry-run", action="store_true", help="ファイル書き出しなし、結果のみプレビュー")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        print(f"エラー: 入力ファイルが見つからない: {input_path}")
        sys.exit(1)

    output_dir = Path(args.output).resolve() if args.output else input_path.parent / "clips"
    config = Config.from_json(Path(args.config))

    print(f"📂 入力: {input_path.name}")
    total_dur = get_video_duration(input_path)
    print(f"  総尺: {total_dur:.1f}秒 ({total_dur/60:.1f}分)")

    silence = detect_silence(input_path, config)

    filler: list[Region] = []
    rephrase: list[Region] = []
    if not args.silence_only:
        print("\n[Step 2] フィラー・言い直し検出中...")
        try:
            segments = transcribe(input_path)
            if segments:
                filler, rephrase = detect_filler_and_rephrase(segments, config)
        except Exception as e:
            print(f"  ⚠️ フィラー検出失敗: {e}")
    else:
        print("\n[Step 2] --silence-only: スキップ")

    print("\n[Step 3] カット区間統合・クリップ算出...")
    merged = merge_regions(silence + filler + rephrase, config)
    print(f"  統合後カット区間: {len(merged)} 個")
    clips = compute_clips(merged, total_dur, config)
    print(f"  出力クリップ数: {len(clips)}")

    print(f"\n[Step 4] クリップ書き出し → {output_dir}")
    export_clips(input_path, output_dir, clips, args.dry_run)

    print("\n[Step 5] レポート生成...")
    save_report(output_dir, input_path, total_dur, silence, filler, rephrase, clips, args.dry_run)

    cut_sec = sum(r.duration for r in silence + filler + rephrase)
    print(f"\n✅ 完了! カット: {cut_sec:.1f}秒 ({cut_sec/total_dur*100:.1f}%) → {len(clips)} クリップ")


if __name__ == "__main__":
    main()
