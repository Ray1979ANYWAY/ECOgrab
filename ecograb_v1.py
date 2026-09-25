# -*- coding: utf-8 -*-
"""
ECOgrab —— 视频下载压缩工具（图形界面版）

流程：
  1. 粘贴视频页 URL → 探测格式（yt-dlp -J）
       ├─ 成功 → 格式列表点选 → 下载
       └─ 失败 → 打开嗅探窗口（CDP 监听浏览器网络，自动捕获真实视频地址）
  2. 捕获到的视频流 → 点选 → 下载
  3. 下载前可勾选"下载完自动压缩"并选压缩模式；未勾选则下载完弹窗询问
"""
import subprocess
import sys
import os
import json
import re
import queue
import threading
import time
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
YTDLP = os.path.join(SCRIPT_DIR, "yt-dlp.exe")
FFMPEG = os.path.join(SCRIPT_DIR, "ffmpeg.exe")
FFPROBE = os.path.join(SCRIPT_DIR, "ffprobe.exe")
# GUI 用 pythonw 运行（无控制台），子进程若不指定此标志会在桌面弹黑色命令窗口
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CHROME_PORT = 9222
CAPTURE_PORT = 8899  # 浏览器扩展 POST 视频地址到此端口
CHROME_PROFILE = os.path.join(SCRIPT_DIR, ".chrome_profile")

# ---------- 压缩模式（与 compress.py 保持一致） ----------
COMPRESS_MODES = {
    "x265 默认(推荐)": {"codec": "libx265", "params": ["-crf", "24", "-preset", "medium"], "desc": "画质基本不变，体积约减半"},
    "x265 高画质":     {"codec": "libx265", "params": ["-crf", "20", "-preset", "medium"], "desc": "最接近原片，体积减小较少"},
    "x265 小体积":     {"codec": "libx265", "params": ["-crf", "27", "-preset", "medium"], "desc": "体积最小，画质略有损失"},
    "NVENC 硬件加速":   {"codec": "hevc_nvenc", "params": [], "desc": "用显卡编码，速度快10倍，体积减小较少"},
}

def fmt_size(n):
    if not n:
        return "未知"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "未知"
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"

def fmt_time(sec):
    if not sec:
        return "?"
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return "?"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

# ---------- 探测模块：yt-dlp -J ----------
def probe_url(url, timeout=90):
    """返回 (info_dict, error)"""
    try:
        r = subprocess.run(
            [YTDLP, "--ffmpeg-location", FFMPEG, "--no-playlist", "-J", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return None, "探测超时（90秒），可能是网络慢或需要代理"
    if r.returncode != 0:
        return None, (r.stderr or r.stdout or "").strip()[-800:]
    try:
        return json.loads(r.stdout), None
    except Exception as e:
        return None, f"解析失败: {e}"

def extract_formats(info):
    """从 yt-dlp -J 结果提取格式列表，返回 list[dict]"""
    out = []
    for f in info.get("formats", []):
        vid = f.get("vcodec") or "none"
        aud = f.get("acodec") or "none"
        if vid == "none" and aud == "none":
            continue
        height = f.get("height") or 0
        res = f"{f.get('width') or 0}x{height}" if height else (f.get("format_note") or "")
        if not res:
            res = f.get("resolution") or ""
        size = f.get("filesize") or f.get("filesize_approx")
        out.append({
            "id": f.get("format_id", ""),
            "ext": f.get("ext", ""),
            "res": str(res),
            "vcodec": vid,
            "acodec": aud,
            "tbr": f.get("tbr") or f.get("vbr") or "",
            "size": size,
            "note": f.get("format_note", ""),
        })
    # 视频格式优先显示；有高度排前面
    out.sort(key=lambda x: (x["vcodec"] == "none", -(x.get("res") or "0").count("x") and 0 or 0), reverse=False)
    return out

# ---------- 嗅探模块：CDP 监听浏览器网络 ----------
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

VIDEO_HINTS = [
    r"\.m3u8($|\?)", r"\.mp4($|\?)", r"\.webm($|\?)", r"\.flv($|\?)",
    r"\.mov($|\?)", r"\.m4s($|\?)", r"\.mpd($|\?)", r"\.f4m($|\?)",
]

def is_video_request(url, headers):
    if not url or url.startswith("data:"):
        return False
    ct = (headers.get("content-type") or "").lower()
    if ct.startswith("video/") or ct in ("application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml", "application/vnd.ms-sstr+xml"):
        return True
    if "octet-stream" in ct and re.search(r"\.(mp4|webm|flv)(\?|$)", url):
        return True
    return bool(re.search(r"\.(m3u8|mp4|webm|flv|mov|m4s|mpd|f4m)(\?|$)", url))

def find_browser():
    for c in CHROME_CANDIDATES:
        if os.path.isfile(c):
            return c
    # 注册表兜底
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"):
                try:
                    k = winreg.OpenKey(root, sub)
                    v, _ = winreg.QueryValueEx(k, None)
                    if v and os.path.isfile(v):
                        return v
                except OSError:
                    continue
    except Exception:
        pass
    return None

class Sniffer:
    """CDP 嗅探：启动调试浏览器 → 监听 Network 请求 → 识别视频流"""
    def __init__(self, log_cb):
        self.log_cb = log_cb
        self.proc = None
        self.ws = None
        self.thread = None
        self.running = False
        self.seen = set()

    def start(self, url=""):
        browser = find_browser()
        if not browser:
            return "找不到 Chrome/Edge，请手动安装浏览器"
        os.makedirs(CHROME_PROFILE, exist_ok=True)
        cmd = [browser,
               f"--remote-debugging-port={CHROME_PORT}",
               f"--user-data-dir={CHROME_PROFILE}",
               "--no-first-run", "--no-default-browser-check",
               "--remote-allow-origins=*"]
        try:
            self.proc = subprocess.Popen(cmd)
        except OSError as e:
            return f"启动浏览器失败: {e}"
        self.start_url = url or "about:blank"
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return None

    def _run(self):
        ws_url = None
        # 等调试端口就绪（最多 20 秒），然后新建一个嗅探专用标签页
        for _ in range(40):
            if not self.running:
                return
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{CHROME_PORT}/json/new?{urllib.parse.quote(self.start_url, safe='')}",
                    data=b"", method="PUT")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    t = json.loads(resp.read().decode("utf-8", "replace"))
                    if t.get("webSocketDebuggerUrl"):
                        ws_url = t["webSocketDebuggerUrl"]
                        break
            except Exception:
                pass
            time.sleep(0.5)
        if not ws_url:
            self.log_cb("嗅探：未能连接浏览器调试端口")
            return
        try:
            import websocket
            self.ws = websocket.create_connection(ws_url, timeout=15)
            self.ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
            self.log_cb("嗅探：已连接，请在嗅探窗口打开视频页并播放")
            while self.running:
                try:
                    msg = json.loads(self.ws.recv())
                except Exception:
                    if self.running:
                        continue
                    break
                method = msg.get("method", "")
                params = msg.get("params", {})
                url = None
                headers = {}
                if method == "Network.requestWillBeSent":
                    req = params.get("request", {})
                    url = req.get("url")
                    headers = req.get("headers", {})
                elif method == "Network.responseReceived":
                    resp = params.get("response", {})
                    url = resp.get("url")
                    headers = resp.get("headers", {})
                if url and is_video_request(url, headers):
                    if url in self.seen:
                        continue
                    self.seen.add(url)
                    ctype = headers.get("content-type", "")
                    size = headers.get("content-length", "")
                    kind = "m3u8" if (".m3u8" in url) else (
                        "mpd" if ".mpd" in url else "mp4" if ".mp4" in url else (
                        "webm" if ".webm" in url else "ts" if ".ts" in url else "流"))
                    self.log_cb(f"嗅探：捕获 {kind} {fmt_size(size) if size else '未知大小'}")
                    self.log_cb(url)
        except Exception as e:
            self.log_cb(f"嗅探：连接中断 {e}")
        finally:
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass

    def stop(self):
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        if self.proc:
            # 不杀浏览器进程，只断开监听；用户可手动关闭嗅探窗口
            pass

class CaptureServer:
    """接收浏览器扩展发来的视频地址（127.0.0.1:8899/capture）"""
    app = None

    def __init__(self, app):
        self.app = app

class CaptureHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/capture":
            try:
                ln = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(ln).decode("utf-8", "replace")
                data = json.loads(body)
                url = data.get("url")
                if url and CaptureServer.app:
                    CaptureServer.app.q.put(("capture", url))
            except Exception:
                pass
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # 静音，避免刷日志

# ---------- 下载模块 ----------
def build_dl_cmd(url, fmt_arg, out_dir, use_cookie=False):
    cmd = [YTDLP, "--ffmpeg-location", FFMPEG, "--no-playlist",
           "-f", fmt_arg, "--merge-output-format", "mp4",
           "-o", os.path.join(out_dir, "%(title)s.%(ext)s"),
           "--no-progress"]
    if use_cookie:
        cmd += ["--cookies-from-browser", "chrome"]
    cmd.append(url)
    return cmd

def fmt_arg_for(fmt):
    """根据格式的音频/视频情况构造 -f 参数，确保有声有画"""
    vid = fmt.get("vcodec") or "none"
    aud = fmt.get("acodec") or "none"
    if vid != "none" and aud != "none":
        return fmt["id"]
    if vid != "none":
        return f"{fmt['id']}+bestaudio/best"
    return fmt["id"]

# ---------- 压缩模块 ----------
def get_duration(path):
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "json", path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30,
                           creationflags=NO_WINDOW)
        d = json.loads(r.stdout).get("format", {}).get("duration")
        return float(d) if d else None
    except Exception:
        return None

def compress_video(in_path, mode_name, log_cb, cancel_event=None):
    """压缩视频，log_cb(str) 输出日志，返回 (success, out_path)"""
    out_path = os.path.splitext(in_path)[0] + "_压缩.mp4"
    mode = COMPRESS_MODES[mode_name]
    vargs = ["-c:v", mode["codec"]] + list(mode["params"])
    if mode["codec"] == "hevc_nvenc":
        vargs = ["-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq",
                 "-rc", "vbr", "-b:v", "2500k", "-maxrate", "3250k",
                 "-bufsize", "5000k", "-cq", "27", "-spatial-aq", "1"]
    cmd = [FFMPEG, "-hide_banner", "-y", "-i", in_path,
           "-map", "0:v:0", "-map", "0:a?", *vargs,
           "-c:a", "copy",
           "-progress", "pipe:1", "-movflags", "+faststart", out_path]
    total = get_duration(in_path)
    log_cb(f"压缩：{os.path.basename(in_path)} → {os.path.basename(out_path)}（{mode_name}）")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=NO_WINDOW)
    errs = []
    t0 = time.time()
    try:
        for raw in proc.stdout:
            line = raw.strip()
            if line.startswith("out_time_us="):
                try:
                    cur = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                if total:
                    pct = min(cur / total * 100, 100.0)
                    log_cb(f"压缩进度：{pct:.1f}%  ({fmt_time(cur)}/{fmt_time(total)})")
                else:
                    log_cb(f"压缩进度：已处理 {fmt_time(cur)}")
    finally:
        proc.wait()
    if proc.returncode != 0:
        log_cb("压缩失败，查看 ffmpeg 输出")
        return False, None
    log_cb(f"压缩完成，耗时 {fmt_time(time.time() - t0)}")
    return True, out_path

class CaptureHandler(BaseHTTPRequestHandler):
    """接收浏览器书签工具 POST 的视频地址（无需扩展/证书）"""
    APP = None

    def do_POST(self):
        try:
            if self.path == "/capture":
                ln = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(ln).decode("utf-8", "replace")
                data = json.loads(body)
                url = data.get("url")
                if url and self.APP:
                    self.APP.q.put(("capture", url))
        except Exception:
            pass
        try:
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"ok")
        except Exception:
            pass

    def log_message(self, *a):
        pass

# ---------- GUI ----------
class App:
    def __init__(self, root):
        self.root = root
        root.title("ECOgrab 视频下载压缩工具")
        root.geometry("900x760")
        self.q = queue.Queue()
        self.sniffer = Sniffer(self.log)
        self.current_info = None
        self.formats = []
        self.captured = []

        self._build_ui()
        self._start_capture_server()
        self.root.after(100, self._poll_queue)

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        # 顶部：URL + 按钮
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="视频页 URL:").pack(side="left")
        self.url_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.url_var, width=58).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="探测格式", command=self.on_probe).pack(side="left", padx=2)
        ttk.Button(top, text="开始嗅探", command=self.on_sniff_start).pack(side="left", padx=2)
        ttk.Button(top, text="停止嗅探", command=self.on_sniff_stop).pack(side="left", padx=2)
        ttk.Label(self.root, text="提示：探测不到时，先点收藏栏的『ECOgrab 捕获』书签，再在视频页播放/刷新即可自动捕获（不用重新登录）；『开始嗅探』只作为兜底（需重新登录）",
                  foreground="#666").pack(fill="x", padx=8)

        # 格式列表
        frm = ttk.LabelFrame(self.root, text="探测到的格式（鼠标点选或看序号）")
        frm.pack(fill="both", expand=True, **pad)
        cols = ("idx", "fid", "res", "codec", "size", "ext")
        self.fmt_tree = ttk.Treeview(frm, columns=cols, show="headings", height=9)
        heads = {"idx": ("#", 40), "fid": ("格式ID", 90), "res": ("分辨率", 110),
                 "codec": ("编码", 160), "size": ("大小", 90), "ext": ("扩展名", 70)}
        for k, (t, w) in heads.items():
            self.fmt_tree.heading(k, text=t)
            self.fmt_tree.column(k, width=w, anchor="w")
        self.fmt_tree.pack(fill="both", expand=True)
        self.fmt_tree.bind("<Double-1>", lambda e: self.on_download_fmt())

        # 捕获列表
        cap = ttk.LabelFrame(self.root, text="嗅探捕获的视频流（在嗅探窗口播放视频后自动出现）")
        cap.pack(fill="both", expand=True, **pad)
        ccols = ("idx", "kind", "size", "url")
        self.cap_tree = ttk.Treeview(cap, columns=ccols, show="headings", height=5)
        for k, (t, w) in {"idx": ("#", 40), "kind": ("类型", 70), "size": ("大小", 90), "url": ("地址", 500)}.items():
            self.cap_tree.heading(k, text=t)
            self.cap_tree.column(k, width=w, anchor="w")
        self.cap_tree.pack(fill="both", expand=True)
        self.cap_tree.bind("<Double-1>", lambda e: self.on_download_cap())

        # 下载选项
        opt = ttk.Frame(self.root)
        opt.pack(fill="x", **pad)
        ttk.Label(opt, text="下载到:").pack(side="left")
        self.dir_var = tk.StringVar(value=SCRIPT_DIR)
        ttk.Entry(opt, textvariable=self.dir_var, width=30).pack(side="left", padx=4)
        ttk.Button(opt, text="浏览", command=self.on_pick_dir).pack(side="left")
        self.auto_compress = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="下载完自动压缩", variable=self.auto_compress).pack(side="left", padx=12)
        self.use_cookie = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="用浏览器Cookie下载(Chrome)", variable=self.use_cookie).pack(side="left", padx=12)
        ttk.Label(opt, text="压缩模式:").pack(side="left")
        self.mode_var = tk.StringVar(value="x265 默认(推荐)")
        ttk.Combobox(opt, textvariable=self.mode_var, values=list(COMPRESS_MODES.keys()),
                     state="readonly", width=16).pack(side="left")

        # 下载按钮
        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="下载选中的格式", command=self.on_download_fmt).pack(side="left", padx=4)
        ttk.Button(btns, text="下载捕获的视频流", command=self.on_download_cap).pack(side="left", padx=4)
        ttk.Button(btns, text="直接下载此 URL（直链/探测失败兜底）", command=self.on_download_url).pack(side="left", padx=4)

        # 日志
        logf = ttk.LabelFrame(self.root, text="日志")
        logf.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(logf, height=11, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log("就绪：粘贴 URL 探测格式；探测失败 → 点视频页收藏栏『ECOgrab 捕获』书签，播放/刷新即自动捕获地址。")

    def _start_capture_server(self):
        """启动本地接收服务：浏览器书签把捕获到的视频地址 POST 到这里"""
        CaptureHandler.APP = self
        try:
            httpd = HTTPServer(("127.0.0.1", CAPTURE_PORT), CaptureHandler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self.log(f"本地接收服务已启动（端口 {CAPTURE_PORT}）——书签捕获通道就绪")
        except OSError as e:
            self.log(f"本地接收服务启动失败（端口 {CAPTURE_PORT} 可能被占用）：{e}")

    # ---------- 日志 ----------
    def log(self, msg):
        self.q.put(("log", msg))

    def _show_log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ---------- 队列轮询 ----------
    def _poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind, payload = item[0], item[1]
                if kind == "log":
                    self._show_log(payload)
                elif kind == "formats":
                    self._show_formats(payload)
                elif kind == "probe_err":
                    self._show_log("探测失败：" + payload)
                    messagebox.showinfo("探测失败",
                        "yt-dlp 未探测到可下载格式。\n\n"
                        "请点击『开始嗅探』，在弹出的浏览器窗口打开该视频页并播放，\n"
                        "程序会自动捕获真实视频地址，然后点『下载捕获的视频流』。")
                elif kind == "capture":
                    self._add_capture(payload)
                elif kind == "dl_start":
                    self._show_log("开始下载：格式 " + payload)
                elif kind == "dl_done":
                    self._show_log("下载完成：" + payload)
                    if self.auto_compress.get():
                        self._show_log("已勾选自动压缩，开始压缩...")
                        threading.Thread(target=self._do_compress, args=(payload,), daemon=True).start()
                    else:
                        if messagebox.askyesno("下载完成", f"文件：{os.path.basename(payload)}\n\n是否立即压缩？"):
                            threading.Thread(target=self._do_compress, args=(payload,), daemon=True).start()
                elif kind == "done":
                    self._show_log(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    # ---------- 探测 ----------
    def on_probe(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请先粘贴视频页 URL")
            return
        self.log(f"正在探测：{url}")
        threading.Thread(target=self._probe_worker, args=(url,), daemon=True).start()

    def _probe_worker(self, url):
        info, err = probe_url(url)
        if err:
            self.q.put(("probe_err", err))
            return
        fmts = extract_formats(info)
        if not fmts:
            self.q.put(("probe_err", "未找到任何视频格式"))
            return
        self.q.put(("formats", (info, fmts)))

    def _show_formats(self, payload):
        self.current_info, self.formats = payload
        self.fmt_tree.delete(*self.fmt_tree.get_children())
        for i, f in enumerate(self.formats, 1):
            codec = ""
            v = f["vcodec"]; a = f["acodec"]
            if v != "none" and a != "none":
                codec = f"{v.split('.')[0]}+{a.split('.')[0]}"
            elif v != "none":
                codec = v.split(".")[0] + "（无音轨，自动合并）"
            else:
                codec = a.split(".")[0] + "（纯音频）"
            tbr = f"{int(f['tbr'])}k" if f.get("tbr") else ""
            self.fmt_tree.insert("", "end", values=(
                i, f["id"], f["res"] or tbr, codec, fmt_size(f["size"]), f["ext"]))
        self.log(f"探测成功：{len(self.formats)} 个格式，双击或点『下载选中的格式』")

    # ---------- 嗅探 ----------
    def on_sniff_start(self):
        url = self.url_var.get().strip()
        err = self.sniffer.start(url)
        if err:
            messagebox.showerror("错误", err)
        else:
            self.log(f"嗅探：正在启动浏览器窗口{('，并打开 ' + url) if url else ''}...")

    def on_sniff_stop(self):
        self.sniffer.stop()
        self.log("嗅探：已停止监听")

    def _add_capture(self, url):
        # 判定类型
        kind = "m3u8" if ".m3u8" in url else "mpd" if ".mpd" in url else "mp4" if ".mp4" in url else "流"
        if any(u == url for u, in self.captured):
            return
        self.captured.append((url,))
        self.cap_tree.insert("", "end", values=(len(self.captured), kind, "未知", url[:80]))

    # ---------- 下载 ----------
    def on_download_fmt(self):
        sel = self.fmt_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在格式列表中点选一行")
            return
        idx = int(self.fmt_tree.item(sel[0], "values")[0]) - 1
        fmt = self.formats[idx]
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "URL 为空")
            return
        fmt_arg = fmt_arg_for(fmt)
        out_dir = self.dir_var.get().strip() or SCRIPT_DIR
        use_cookie = self.use_cookie.get()
        self.log(f"选择格式：{fmt['id']}（{fmt['res']}）→ 下载参数 -f {fmt_arg}")
        threading.Thread(target=self._download_worker, args=(url, fmt_arg, out_dir, use_cookie), daemon=True).start()

    def on_download_cap(self):
        sel = self.cap_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在捕获列表中点选一行")
            return
        idx = int(self.cap_tree.item(sel[0], "values")[0]) - 1
        url = self.captured[idx][0]
        out_dir = self.dir_var.get().strip() or SCRIPT_DIR
        use_cookie = self.use_cookie.get()
        threading.Thread(target=self._download_worker, args=(url, "best/bestvideo+bestaudio", out_dir, use_cookie), daemon=True).start()

    def on_download_url(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "URL 为空")
            return
        out_dir = self.dir_var.get().strip() or SCRIPT_DIR
        use_cookie = self.use_cookie.get()
        threading.Thread(target=self._download_worker, args=(url, "best/bestvideo+bestaudio", out_dir, use_cookie), daemon=True).start()

    def _download_worker(self, url, fmt_arg, out_dir, use_cookie=False):
        cmd = build_dl_cmd(url, fmt_arg, out_dir, use_cookie)
        self.q.put(("dl_start", fmt_arg))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=NO_WINDOW)
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                m = re.search(r"\[download\]\s+([\d.]+)% of ~?([\d.]+(?:MiB|GiB|KiB))", line)
                if m:
                    self.q.put(("log", f"下载：{m.group(1)}% （{m.group(2)}）"))
                elif line.startswith("ERROR"):
                    self.q.put(("log", "下载错误：" + line))
            proc.wait()
        except Exception as e:
            self.q.put(("log", f"下载异常：{e}"))
            return
        # 找到输出文件（title 可能含特殊字符，用最新改动的视频文件推断）
        newest = None
        try:
            for f in os.listdir(out_dir):
                p = os.path.join(out_dir, f)
                if os.path.isfile(p) and f.lower().endswith((".mp4", ".mkv", ".webm")):
                    mt = os.path.getmtime(p)
                    if newest is None or mt > newest[1]:
                        newest = (p, mt)
        except Exception:
            newest = None
        if newest:
            self.q.put(("dl_done", newest[0]))
        else:
            self.q.put(("log", "下载结束，但未找到输出文件（可能下载失败）"))

    # ---------- 压缩 ----------
    def _do_compress(self, path):
        mode = self.mode_var.get()
        ok, out = compress_video(path, mode, lambda m: self.q.put(("log", m)))
        if ok:
            self.q.put(("done", f"压缩完成：{os.path.basename(out)}"))
        else:
            self.q.put(("done", "压缩失败（详见上方日志）"))

    # ---------- 工具 ----------
    def on_pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or SCRIPT_DIR)
        if d:
            self.dir_var.set(d)

def main():
    root = tk.Tk()
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
