import subprocess
import sys
import re

def get_formats(url):
    result = subprocess.run(
        ["yt-dlp.exe", "--ffmpeg-location", "ffmpeg.exe", "-F", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        return None, result.stderr
    return result.stdout, None

def parse_formats(raw):
    lines = raw.splitlines()
    formats = []
    in_table = False
    for line in lines:
        # 跳过表头
        if re.match(r'^ID\s+EXT\s+', line):
            in_table = True
            continue
        if not in_table:
            continue
        if line.strip() == "" or line.startswith("-"):
            continue
        # 解析每行
        parts = line.split()
        if len(parts) < 2:
            continue
        fmt_id = parts[0]
        ext = parts[1] if len(parts) > 1 else ""
        
        # 分辨率
        res = ""
        for p in parts:
            if re.match(r'^\d+x\d+$', p):
                res = p
                break
            if re.match(r'^\d+(p|k)$', p):
                res = p
                break
        
        # 编解码器兼容性
        line_lower = line.lower()
        if "avc1" in line_lower or "h264" in line_lower:
            compat = "✓"
        elif "av1" in line_lower or "vp9" in line_lower or "vp08" in line_lower or "vp09" in line_lower:
            compat = "⚠"
        else:
            compat = " "

        # 大小
        size = ""
        m = re.search(r'~?\s*([\d.]+(?:MiB|GiB|KiB|MB|GB|KB))', line)
        if m:
            size = m.group(1).replace("~", "").strip()

        # 码率
        bitrate = ""
        m2 = re.search(r'(\d+)k\s+m3u8', line)
        if m2:
            bitrate = m2.group(1) + "k"

        formats.append({
            "id": fmt_id,
            "ext": ext,
            "res": res,
            "compat": compat,
            "size": size,
            "bitrate": bitrate,
            "raw": line.strip()
        })
    return formats

def print_formats(formats):
    print()
    print(f"  {'#':<4} {'格式ID':<14} {'分辨率':<12} {'大小':<12} {'兼容'}")
    print("  " + "-"*56)
    for i, f in enumerate(formats, 1):
        res = f['res'] or f['bitrate'] or ""
        size = f['size'] or ""
        print(f"  {i:<4} {f['id']:<14} {res:<12} {size:<12} {f['compat']}")
    print()
    print("  ✓ = H.264，兼容所有播放器")
    print("  ⚠ = AV1/VP9，部分播放器不支持")
    print()

def download(url, fmt_arg):
    cmd = [
        "yt-dlp.exe", "--ffmpeg-location", "ffmpeg.exe",
        "-f", fmt_arg,
        "--merge-output-format", "mp4",
        "-o", "%(title)s.%(ext)s",
        url
    ]
    subprocess.run(cmd)

def main():
    url = input("粘贴视频链接（输入 q 退出）：").strip()
    if url.lower() == "q":
        return

    print("\n正在获取格式列表，请稍候...\n")
    raw, err = get_formats(url)
    if err or raw is None:
        print("[错误] 获取格式失败，请检查链接。")
        input("按回车返回...")
        return

    formats = parse_formats(raw)
    if not formats:
        print("[错误] 未能解析格式列表。")
        print(raw)
        input("按回车返回...")
        return

    print_formats(formats)

    print("=" * 58)
    print("  输入序号下载，或直接输入格式ID（如 hls-3025-0）")
    print("  直接回车 = 自动选最佳 H.264")
    print("  合并下载示例：137+140")
    print("=" * 58)
    choice = input("\n请输入选择：").strip()

    if choice == "":
        fmt_arg = "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=avc1]+bestaudio/best[vcodec^=avc1]"
    elif choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(formats):
            fmt_arg = formats[idx]["id"]
            if formats[idx]["compat"] == "⚠":
                print(f"\n  ⚠ 警告：{fmt_arg} 使用 AV1/VP9 编码，部分播放器可能无法播放。")
                confirm = input("  继续下载？(y/n): ").strip().lower()
                if confirm != "y":
                    print("已取消。")
                    input("按回车返回...")
                    return
        else:
            print("[错误] 序号超出范围。")
            input("按回车返回...")
            return
    else:
        fmt_arg = choice

    print(f"\n正在下载：{fmt_arg}\n")
    download(url, fmt_arg)
    print()
    input("下载完成，按回车继续...")

if __name__ == "__main__":
    while True:
        try:
            main()
        except KeyboardInterrupt:
            print("\n退出。")
            break
