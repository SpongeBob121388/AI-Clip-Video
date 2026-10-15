"""端到端剪辑功能测试 — 生成模拟口播视频并运行完整剪辑流程。

流程：生成测试视频 → 媒体探测 → 静音检测 → LLM决策 → EDL 生成 → 导出
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import imageio_ffmpeg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
TEST_DIR = PROJECT_ROOT / "data" / "e2e_test"
TEST_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(f"[E2E] {msg}", flush=True)


def generate_test_video(output_path: Path) -> None:
    """生成模拟口播视频：3段语音 + 2段静音，总约30秒。"""
    log(f"生成测试视频 → {output_path}")

    segments = [
        (440, 8),
        (0, 4),
        (523, 8),
        (0, 4),
        (659, 6),
    ]

    audio_parts = []
    for i, (freq, dur) in enumerate(segments):
        if freq > 0:
            audio_parts.append(f"sine=frequency={freq}:duration={dur}")
        else:
            audio_parts.append(f"anullsrc=duration={dur}")

    concat_filter = "".join(
        f"[{i+1}:a]" for i in range(len(audio_parts))
    ) + f"concat=n={len(audio_parts)}:v=0:a=1[aout]"

    input_args = []
    for part in audio_parts:
        input_args.extend(["-f", "lavfi", "-i", part])

    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=1280x720:d=30:r=30",
        *input_args,
        "-filter_complex", concat_filter,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        log(f"FFmpeg stderr: {result.stderr[-500:]}")
        raise RuntimeError(f"FFmpeg failed: returncode={result.returncode}")

    log(f"测试视频生成完成，大小: {output_path.stat().st_size / 1024:.1f} KB")


def get_ffprobe() -> str:
    """获取 ffprobe 路径，若不存在则返回 ffmpeg 路径。"""
    ffprobe = FFMPEG.replace("ffmpeg", "ffprobe")
    if Path(ffprobe).exists():
        return ffprobe
    ffprobe2 = str(Path(FFMPEG).parent / "ffprobe.exe")
    if Path(ffprobe2).exists():
        return ffprobe2
    return FFMPEG


def probe_media(video_path: Path) -> dict:
    """使用 ffmpeg/ffprobe 探测媒体信息。"""
    log("步骤1: 媒体探测")

    ffprobe = get_ffprobe()

    if "ffprobe" in ffprobe:
        cmd = [
            ffprobe, "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(video_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return json.loads(result.stdout)

    cmd = [FFMPEG, "-i", str(video_path), "-hide_banner"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    stderr = result.stderr

    duration = 0.0
    for line in stderr.split("\n"):
        if "Duration" in line:
            parts = line.split("Duration:")[1].strip().split(",")[0].strip()
            h, m, s = parts.split(":")
            duration = int(h) * 3600 + int(m) * 60 + float(s)

    log(f"  时长: {duration:.1f}s")
    return {"format": {"duration": str(duration), "format_name": "mp4"}}


def detect_silence(video_path: Path) -> list[dict]:
    """使用 FFmpeg silencedetect 检测静音段。"""
    log("步骤2: 静音检测 (FFmpeg silencedetect)")

    cmd = [
        FFMPEG, "-y",
        "-i", str(video_path),
        "-af", "silencedetect=noise=-30dB:d=2.0",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    silences = []
    for line in result.stderr.split("\n"):
        if "silence_start" in line:
            start = float(line.split("silence_start:")[1].strip())
            silences.append({"start": start, "end": None})
        elif "silence_end" in line and silences:
            end_part = line.split("silence_end:")[1].strip()
            end = float(end_part.split()[0])
            silences[-1]["end"] = end

    silences = [s for s in silences if s["end"] is not None]
    for s in silences:
        log(f"  静音段: {s['start']:.1f}s - {s['end']:.1f}s (时长 {s['end']-s['start']:.1f}s)")

    return silences


def run_llm_decision(silences: list[dict]) -> list[dict]:
    """使用 LLM 决策服务对静音候选做剪辑决策。

    有 API Key 走 OpenAI LLM，无则自动降级为规则策略。
    """
    log("步骤3: LLM 剪辑决策")

    from app.models.enums import SuggestionType

    from app.providers.credential.win_credential_store import WinCredentialStore
    from app.providers.llm.openai_adapter import OpenAILLMAdapter
    from app.providers.llm.protocol import CandidateContext
    from app.services.llm_service import LLMDecisionService

    candidates = []
    for i, s in enumerate(silences):
        candidates.append(CandidateContext(
            id=f"silence-{i+1}",
            prev_text=f"第{i+1}段语音结束" if i > 0 else "视频开头",
            next_text=f"第{i+2}段语音开始" if i < len(silences) - 1 else "视频结尾",
            duration_us=int((s["end"] - s["start"]) * 1_000_000),
            type=SuggestionType.SILENCE,
        ))

    cred_store = WinCredentialStore()
    base_url = cred_store.get_secret("llm_base_url") or "https://api.deepseek.com/v1"
    llm_adapter = OpenAILLMAdapter(cred_store, base_url=base_url, model_name="deepseek-chat")
    llm_service = LLMDecisionService(llm_adapter)

    api_key_configured = llm_adapter.is_available()
    if api_key_configured:
        log("  LLM API Key 已配置，使用 OpenAI LLM 决策")
    else:
        log("  LLM API Key 未配置，将自动降级为规则策略")

    decisions, used_fallback = asyncio.run(llm_service.get_decisions(candidates))

    if used_fallback:
        log("  ⚠ 使用了降级规则策略（LLM 不可用或调用失败）")
    else:
        log("  ✓ LLM 决策成功")

    result = []
    for cand, dec in zip(candidates, decisions, strict=False):
        s = silences[int(cand.id.split("-")[1]) - 1]
        result.append({
            "id": cand.id,
            "start": s["start"],
            "end": s["end"],
            "decision": dec.decision.value,
            "reason": dec.reason,
            "source": dec.decision_source.value,
        })
        log(f"  {cand.id}: {dec.decision.value} — {dec.reason}")

    return result


def build_edl_from_decisions(
    decisions: list[dict], total_duration: float
) -> dict:
    """根据 LLM 决策结果生成 EDL。"""
    log("步骤4: 生成 EDL（基于 LLM 决策）")

    delete_segments = sorted(
        [d for d in decisions if d["decision"] == "delete"],
        key=lambda d: d["start"],
    )

    output_intervals = []
    prev_end = 0.0
    for d in delete_segments:
        if d["start"] > prev_end:
            output_intervals.append({
                "source_start_us": int(prev_end * 1_000_000),
                "source_end_us": int(d["start"] * 1_000_000),
            })
        prev_end = max(prev_end, d["end"])
    if prev_end < total_duration:
        output_intervals.append({
            "source_start_us": int(prev_end * 1_000_000),
            "source_end_us": int(total_duration * 1_000_000),
        })

    total_kept = sum(iv["source_end_us"] - iv["source_start_us"] for iv in output_intervals) / 1_000_000
    log(f"  保留区间: {len(output_intervals)} 段，总时长 {total_kept:.1f}s")
    log(f"  删除区间: {len(delete_segments)} 段")

    return {"delete_segments": delete_segments, "output_intervals": output_intervals}


def export_clipped_video(
    source: Path, output: Path, intervals: list[dict]
) -> None:
    """根据保留区间导出剪辑后视频。"""
    log(f"步骤5: 导出剪辑视频 → {output}")

    if len(intervals) == 1:
        iv = intervals[0]
        start = iv["source_start_us"] / 1_000_000
        end = iv["source_end_us"] / 1_000_000
        cmd = [
            FFMPEG, "-y",
            "-ss", str(start),
            "-to", str(end),
            "-i", str(source),
            "-c", "copy",
            str(output),
        ]
    else:
        concat_file = TEST_DIR / "concat_list.txt"
        temp_files = []
        for i, iv in enumerate(intervals):
            start = iv["source_start_us"] / 1_000_000
            end = iv["source_end_us"] / 1_000_000
            temp = TEST_DIR / f"segment_{i}.mp4"
            temp_files.append(temp)
            subprocess.run([
                FFMPEG, "-y",
                "-ss", str(start),
                "-to", str(end),
                "-i", str(source),
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-c:a", "aac",
                str(temp),
            ], capture_output=True, timeout=30)

        with open(concat_file, "w", encoding="utf-8") as f:
            for temp in temp_files:
                f.write(f"file '{temp.name}'\n")

        cmd = [
            FFMPEG, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(output),
        ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        log(f"  FFmpeg stderr: {result.stderr[-300:]}")
        raise RuntimeError(f"导出失败: returncode={result.returncode}")

    output_size = output.stat().st_size / 1024
    log(f"  导出完成，大小: {output_size:.1f} KB")


def verify_output(source: Path, clipped: Path) -> None:
    """验证输出视频。"""
    log("步骤6: 验证输出视频")

    ffprobe = get_ffprobe()

    for label, path in [("源视频", source), ("剪辑后", clipped)]:
        if "ffprobe" in ffprobe:
            cmd = [
                ffprobe, "-v", "quiet",
                "-print_format", "json",
                "-show_format", str(path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            info = json.loads(result.stdout)
            duration = float(info["format"]["duration"])
        else:
            cmd = [FFMPEG, "-i", str(path), "-hide_banner"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            duration = 0.0
            for line in result.stderr.split("\n"):
                if "Duration" in line:
                    parts = line.split("Duration:")[1].strip().split(",")[0].strip()
                    h, m, s = parts.split(":")
                    duration = int(h) * 3600 + int(m) * 60 + float(s)
        size = path.stat().st_size / 1024
        log(f"  {label}: 时长 {duration:.1f}s, 大小 {size:.1f} KB")


def main() -> None:
    log("=== 端到端剪辑功能测试 ===\n")

    source_video = TEST_DIR / "source_talking.mp4"
    clipped_video = TEST_DIR / "clipped_output.mp4"

    log("--- 生成测试素材 ---")
    generate_test_video(source_video)

    log("\n--- 运行剪辑流程 ---")
    start_time = time.perf_counter()

    media_info = probe_media(source_video)
    total_duration = float(media_info["format"]["duration"])

    silences = detect_silence(source_video)
    if not silences:
        log("未检测到静音段，调整阈值重试...")
        cmd = [
            FFMPEG, "-y",
            "-i", str(source_video),
            "-af", "silencedetect=noise=-20dB:d=1.5",
            "-f", "null", "-",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for line in result.stderr.split("\n"):
            if "silence_start" in line:
                start = float(line.split("silence_start:")[1].strip())
                silences.append({"start": start, "end": None})
            elif "silence_end" in line and silences:
                end_part = line.split("silence_end:")[1].strip()
                end = float(end_part.split()[0])
                silences[-1]["end"] = end
        silences = [s for s in silences if s["end"] is not None]
        log(f"  重新检测到 {len(silences)} 个静音段")

    decisions = run_llm_decision(silences)
    edl = build_edl_from_decisions(decisions, total_duration)
    export_clipped_video(source_video, clipped_video, edl["output_intervals"])
    verify_output(source_video, clipped_video)

    elapsed = time.perf_counter() - start_time
    log(f"\n=== 测试完成，总耗时 {elapsed:.1f}s ===")
    log(f"源视频: {source_video}")
    log(f"剪辑后: {clipped_video}")


if __name__ == "__main__":
    main()