# -*- coding: utf-8 -*-
"""
视频压缩脚本 —— 利用本目录下的 ffmpeg.exe

特点：
  - 保持原分辨率、原帧率（不缩放、不降帧），只重新编码压缩
  - 输出为 <原名>_压缩.mp4，绝不覆盖原文件
  - 支持单个文件、拖入多个文件、或直接压缩本文件夹内全部视频
"""
import subprocess
import sys
import os
import json
import shlex
import time
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(SCRIPT_DIR, "ffmpeg.exe")
FFPROBE = os.path.join(SCRIPT_DIR, "ffprobe.exe")

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".ts", ".m2ts", ".flv", ".webm", ".avi", ".m4v", ".wmv"}
OUT_SUFFIX = "_压缩.mp4"

MODES = {
    "1": {
        "name": "默认（推荐）",
        "codec": "libx265",
        "params": ["-crf", "24", "-preset", "medium"],
        "desc": "H.265 软件编码，画质基本不变，体积约减半",
    },
    "2": {
        "name": "高画质",
        "codec": "libx265",
        "params": ["-crf", "20", "-preset", "medium"],
        "desc": "画质最接近原片，体积减小较少",
    },
    "3": {
        "name": "小体积",
        "codec": "libx265",
        "params": ["-crf", "27", "-preset", "medium"],
        "desc": "体积最小，画质略有损失",
    },
    "4": {
        "name": "兼容（H.264）",
        "codec": "libx264",
        "params": ["-crf", "20", "-preset", "medium"],
        "desc": "所有播放器/设备都能放，体积比 H.265 大一些",
    },
    "5": {
        "name": "硬件加速（NVENC）",
        "codec": "hevc_nvenc",
        "params": [],
        "desc": "用显卡 NVENC 编码，速度快数倍；体积约减小 2~3 成，画质略低于软件模式",
    },
}


def fmt_size(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def fmt_time(sec):
    if not sec:
        return "?"
    sec = int(float(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def probe(path):
    cmd = [FFPROBE, "-hide_banner", "-v", "error",
           "-show_entries", "stream=codec_type,codec_name,width,height,pix_fmt,bit_rate",
           "-show_entries", "format=duration,size",
           "-of", "json", path]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return None, r.stderr.strip()
    try:
        return json.loads(r.stdout), None
    except Exception as e:
        return None, str(e)


def video_stream(info):
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return None


def first_audio(info):
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            return s
    return None


def audio_args(info):
    a = first_audio(info)
    if not a:
        return [], "无音轨"
    codec = a.get("codec_name", "")
    try:
        br = int(a.get("bit_rate") or 0)
    except (TypeError, ValueError):
        br = 0
    # 已是低码率常见音频格式：原样复制，音频零损失
    if codec in ("aac", "mp3", "ac3", "eac3") and 0 < br <= 192000:
        return ["-c:a", "copy"], "音频原样复制"
    return ["-c:a", "aac", "-b:a", "192k"], "音频转 AAC 192kbps"


def is_high_bitdepth(vs):
    pf = vs.get("pix_fmt", "") or ""
    return any(x in pf for x in ("10le", "12le", "p010", "p012"))


def build_cmd(in_path, out_path, vargs, aargs):
    return [FFMPEG, "-hide_banner", "-y", "-i", in_path,
            "-map", "0:v:0", "-map", "0:a?",
            *vargs, *aargs,
            "-progress", "pipe:1",
            "-movflags", "+faststart",
            out_path]


BAR_WIDTH = 30


def draw_progress(cur, total, elapsed):
    """绘制单行进度条，返回该行字符数（用于结束清行）"""
    if total and total > 0:
        pct = min(cur / total * 100, 100.0)
        filled = int(pct / 100 * BAR_WIDTH)
        bar = "█" * filled + "░" * (BAR_WIDTH - filled)
        line = f"  [{bar}] {pct:5.1f}%  {fmt_time(cur)} / {fmt_time(total)}  用时 {fmt_time(elapsed)}"
    else:
        line = f"  正在压缩：已处理 {fmt_time(cur)}  用时 {fmt_time(elapsed)}"
    sys.stdout.write("\r" + line)
    sys.stdout.flush()
    return len(line)


def run_ffmpeg_with_progress(cmd, total_dur):
    """运行 ffmpeg，解析 -progress 输出并显示可视化百分比进度条，返回返回码"""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"  [错误] 无法启动 ffmpeg：{e}")
        return 1

    # 后台收集 stderr 尾部，失败时用于诊断
    err_lines = []

    def read_stderr():
        for line in proc.stderr:
            err_lines.append(line.rstrip("\n"))
            if len(err_lines) > 30:
                err_lines.pop(0)

    t_err = threading.Thread(target=read_stderr, daemon=True)
    t_err.start()

    t0 = time.time()
    last_draw = 0.0
    last_len = 0
    try:
        for raw in proc.stdout:
            line = raw.strip()
            if line.startswith("out_time_us="):
                try:
                    cur = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                now = time.time()
                if now - last_draw >= 0.1:  # 每 0.1 秒刷新一次
                    last_len = draw_progress(cur, total_dur, now - t0)
                    last_draw = now
    finally:
        proc.wait()

    # 清掉进度条残留
    if last_len:
        sys.stdout.write("\r" + " " * last_len + "\r")
        sys.stdout.flush()
    t_err.join(timeout=2)

    if proc.returncode != 0 and err_lines:
        print("  [ffmpeg 错误输出]")
        for ln in err_lines[-15:]:
            print("    " + ln)
    return proc.returncode


def encode_file(in_path, out_path, mode_key):
    info, err = probe(in_path)
    if err or info is None:
        print(f"  [错误] 无法读取文件信息：{err}")
        return False

    vs = video_stream(info)
    if not vs:
        print("  [跳过] 未找到视频流")
        return False

    w, h = vs.get("width"), vs.get("height")
    vcodec = vs.get("codec_name", "?")
    dur = info.get("format", {}).get("duration")
    try:
        dur_f = float(dur) if dur else None
    except (TypeError, ValueError):
        dur_f = None
    mode = MODES[mode_key]

    print(f"  源视频：{w}x{h}  {vcodec}  时长 {fmt_time(dur)}")
    print(f"  编码器：{mode['name']}（{mode['desc']}）")
    print(f"  输出保持 {w}x{h} 分辨率，不缩放")

    vargs = ["-c:v", mode["codec"]] + list(mode["params"])
    aargs, anote = audio_args(info)
    print(f"  音频：{anote}")

    # 硬件模式：按源视频码率的 50% 设目标码率，保证有实际压缩效果
    if mode["codec"] == "hevc_nvenc":
        try:
            src_br = int(vs.get("bit_rate") or 0)
        except (TypeError, ValueError):
            src_br = 0
        target = max(int(src_br * 0.5 / 1000), 600) if src_br > 0 else 2500
        print(f"  目标码率约 {target}kbps（源视频 {src_br // 1000}kbps 的 50%）")
        vargs = ["-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq",
                 "-rc", "vbr", "-b:v", f"{target}k",
                 "-maxrate", f"{int(target * 1.3)}k", "-bufsize", f"{int(target * 2)}k",
                 "-cq", "27", "-spatial-aq", "1"]

    if is_high_bitdepth(vs):
        if mode["codec"] == "libx265":
            vargs += ["-pix_fmt", "yuv420p10le"]
            print("  检测到 10bit 色深，按 10bit 编码")
        else:
            print("  ⚠ 源视频为 10bit，该模式不支持，自动改用软件 x265")
            vargs = ["-c:v", "libx265", "-crf", "24", "-preset", "medium", "-pix_fmt", "yuv420p10le"]

    cmd = build_cmd(in_path, out_path, vargs, aargs)
    print("  开始压缩...")
    t0 = time.time()
    rc = run_ffmpeg_with_progress(cmd, dur_f)

    # 硬件编码失败时自动降级为软件 x265 重试
    if rc != 0 and mode["codec"] == "hevc_nvenc":
        print("  ⚠ 硬件编码失败，自动改用软件 x265 重试...")
        vargs = ["-c:v", "libx265", "-crf", "24", "-preset", "medium"]
        cmd = build_cmd(in_path, out_path, vargs, aargs)
        rc = run_ffmpeg_with_progress(cmd, dur_f)

    dt = time.time() - t0
    if rc != 0:
        print(f"  [错误] 压缩失败（耗时 {fmt_time(dt)}），已跳过该文件")
        return False

    # 验证输出：分辨率是否保持
    ow = oh = None
    oinfo, _ = probe(out_path)
    if oinfo:
        ovs = video_stream(oinfo)
        if ovs:
            ow, oh = ovs.get("width"), ovs.get("height")
    try:
        in_size = float(info["format"].get("size") or 0)
    except (TypeError, ValueError):
        in_size = 0
    out_size = os.path.getsize(out_path)
    ok_res = (ow == w and oh == h)
    ratio = (1 - out_size / in_size) * 100 if in_size else 0
    res_mark = f"，分辨率 {w}x{h} ✓" if ok_res else "，⚠ 分辨率与源不一致！"
    print(f"  完成（耗时 {fmt_time(dt)}）：{fmt_size(in_size)} → {fmt_size(out_size)}，减小 {ratio:.1f}%{res_mark}")
    return True


def collect_files(args):
    files, dirs = [], []
    for a in args:
        a = (a or "").strip().strip('"')
        if not a:
            continue
        if os.path.isdir(a):
            dirs.append(a)
        elif os.path.isfile(a):
            files.append(a)
    if not args:
        dirs.append(SCRIPT_DIR)  # 直接回车 = 压缩脚本所在文件夹
    for d in dirs:
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if (os.path.isfile(p) and os.path.splitext(name)[1].lower() in VIDEO_EXTS
                    and not name.lower().endswith(OUT_SUFFIX)):
                files.append(p)
    seen, out = set(), []
    for f in files:  # 去重且保持顺序
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def safe_input(prompt=""):
    """输入封装：stdin 被关闭（如管道调用）时返回空串而不是崩溃"""
    try:
        return input(prompt)
    except EOFError:
        return ""


def main():
    print()
    print("=" * 56)
    print("  视频压缩工具（保持分辨率，不覆盖原文件）")
    print("=" * 56)

    args = sys.argv[1:]
    if args:
        print("  已收到拖入的文件/文件夹")
        files = collect_files(args)
    else:
        raw = safe_input("  把视频文件拖入窗口，或输入路径（可多个，用空格隔开）\n"
                         "  直接回车 = 压缩本文件夹内全部视频\n> ").strip()
        if raw:
            try:
                parts = shlex.split(raw.replace("，", " "), posix=False)
            except ValueError:
                parts = raw.split()
            files = collect_files(parts)
        else:
            files = collect_files([])

    if not files:
        print("  没有找到可压缩的视频文件。")
        safe_input("按回车退出...")
        return

    print()
    print("  请选择压缩模式：")
    for k, m in MODES.items():
        print(f"    {k}. {m['name']} —— {m['desc']}")
    choice = safe_input("  输入序号（默认 1）：").strip() or "1"
    if choice not in MODES:
        print("  无效选择，已使用默认模式。")
        choice = "1"

    total_in = total_out = 0.0
    done = fail = skip = 0
    print()
    print(f"  共 {len(files)} 个文件，开始处理...")
    for i, f in enumerate(files, 1):
        print(f"\n[{i}/{len(files)}] {os.path.basename(f)}")
        out = os.path.splitext(f)[0] + OUT_SUFFIX
        if os.path.exists(out):
            print(f"  [跳过] 输出已存在：{os.path.basename(out)}（如需重新压缩请先删除它）")
            skip += 1
            continue
        if encode_file(f, out, choice):
            done += 1
            total_in += os.path.getsize(f)
            total_out += os.path.getsize(out)
        else:
            fail += 1

    print()
    print("=" * 56)
    print(f"  完成 {done} 个，跳过 {skip} 个，失败 {fail} 个")
    if total_in:
        print(f"  总大小：{fmt_size(total_in)} → {fmt_size(total_out)}（减小 {(1 - total_out / total_in) * 100:.1f}%）")
    print("=" * 56)
    safe_input("全部处理完毕，按回车退出...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。")
