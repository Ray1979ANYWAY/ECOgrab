# -*- coding: utf-8 -*-
"""ECOgrab v2 核心逻辑测试（不启动 GUI、不真实下载）"""
import sys, os, time, threading
sys.path.insert(0, r'D:\Documents\ECOgrab')
import ecograb as E

# 1. extract_formats / fmt_arg_for
info = {"formats": [
    {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "width": 1920, "height": 1080, "filesize": 1000000},
    {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "width": 0, "height": 0, "filesize": 500000},
    {"format_id": "303", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 1920, "height": 1080},
    {"format_id": "18",  "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "width": 640, "height": 360},
]}
fmts = E.extract_formats(info)
assert len(fmts) == 4, len(fmts)
assert fmts[-1]["vcodec"] == "none"           # 纯音频排最后
assert E.fmt_arg_for(fmts[0]) == "137+bestaudio/best"   # 纯视频自动合音轨
assert E.fmt_arg_for(fmts[2]) == "18"                    # 音视频一体
assert E.fmt_arg_for(fmts[3]) == "140"                   # 纯音频直接用 id
print("1. extract_formats / fmt_arg_for OK")

# 2. is_video_request
assert E.is_video_request("http://x/v.m3u8?token=1", {})
assert E.is_video_request("http://x/a.mp4", {})
assert not E.is_video_request("http://x/a.ts?seg=1", {})     # .ts 分片排除
assert E.is_video_request("http://x/seg.mp4?x=1", {"content-type": "video/mp4"})
assert E.is_video_request("http://x/video?token=1", {"content-type": "application/vnd.apple.mpegurl"})
assert not E.is_video_request("http://x/page.html", {})
print("2. is_video_request OK")

# 3. 下载池：并发限制 + 暂停让位 + 恢复（模拟 add() 的线程包装调度）
events = []
pool = E.DownloadPool(lambda e: events.append(e), print, max_concurrent=2)

def sched(task):
    threading.Thread(target=pool._schedule, args=(task,), daemon=True).start()

class FakeTask:
    def __init__(self, i):
        self.id = i; self.state = "waiting"; self.started = 0
    def start(self):
        self.started += 1; self.state = "downloading"

t1, t2, t3 = FakeTask(1), FakeTask(2), FakeTask(3)
sched(t1); sched(t2); sched(t3)
time.sleep(0.3)
assert t1.state == "downloading" and t2.state == "downloading", (t1.state, t2.state)
assert t3.state == "waiting", t3.state                     # 并发2，第3个排队
# 暂停 t1 → 让出席位 → t3 应开始
t1.state = "paused"
pool.on_task_paused(t1)
time.sleep(0.3)
assert t3.state == "downloading", t3.state
# 完成 t2 → 恢复 t1
t2.state = "done"; pool.on_task_done(t2)
t1.state = "waiting"; sched(t1)
time.sleep(0.3)
assert t1.state == "downloading", t1.state
print("3. 下载池 并发限制/暂停让位/恢复 OK", flush=True)

# 4. 压缩队列：空闲立即压 + 忙时排队
# mock compress_video：睡 0.2s 表示正在压
orig = E.compress_video
def fake_compress(path, mode, log):
    time.sleep(0.2)
    return True, path + "_out.mp4"
E.compress_video = fake_compress
cq = E.CompressQueue(print, lambda e: None)
t0 = time.time()
cq.add("a.mp4", "x265 默认(推荐)")
cq.add("b.mp4", "x265 默认(推荐)")
time.sleep(0.55)
elapsed = time.time() - t0
# 串行队列：两个 0.2s 任务 ≈ 0.4s+（若并行则 ≈0.2s）
assert elapsed >= 0.4, elapsed
assert cq.q.empty()
print(f"4. 压缩队列 串行排队 OK（两个任务耗时 {elapsed:.2f}s）")

# 5. build_dl_cmd：临时目录隔离 + 续传 -c
cmd, tmpdir = E.build_dl_cmd("http://x/v", "18", r"D:\tmp_out", 7, use_cookie=True)
assert "--cookies-from-browser" in cmd and "ecograb_ck" in " ".join(cmd)
assert ".ecograb_7_" in " ".join(cmd) and ".ecograb_7_" in tmpdir
assert cmd[-1] == "http://x/v" and "-c" in cmd
os.rmdir(tmpdir)
print("5. build_dl_cmd（临时目录 + cookie + 续传）OK")

print("=== ALL CORE TESTS PASSED ===")
