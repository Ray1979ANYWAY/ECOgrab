# -*- coding: utf-8 -*-
"""
ECOgrab v2 —— 视频下载压缩工具（图形界面版）

定稿交互：
  1. 粘贴 URL → 探测格式；成功列出格式（悬停出下载按钮）
  2. 探测失败 → 提示窗 → 确认后自动开播放窗口（CDP 嗅探）→ 播放捕获 → tooltip 回主窗
  3. 下载池：并行下载、暂停/恢复、每行压缩下拉；完成变灰
  4. 压缩队列：下载完自动衔接（空闲即压/忙则排队）
"""
import subprocess
import sys
import os
import json
import re
import queue
import threading
import time
import shutil
import tempfile
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
YTDLP = os.path.join(SCRIPT_DIR, "yt-dlp.exe")
FFMPEG = os.path.join(SCRIPT_DIR, "ffmpeg.exe")
FFPROBE = os.path.join(SCRIPT_DIR, "ffprobe.exe")
# GUI 用 pythonw 运行（无控制台），子进程若不指定此标志会在桌面弹黑色命令窗口
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CHROME_PORT = 9222
CAPTURE_PORT = 8899  # 书签通道备用端口
CHROME_PROFILE = os.path.join(SCRIPT_DIR, ".chrome_profile")
MAX_CONCURRENT = 3          # 并行下载数（吃满带宽）
SNIFF_TIMEOUT = 60          # 嗅探无动作超时（秒）

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

# YouTube DASH 流 itag → (分辨率/类型标签, 编码标签)
ITAG_LABELS = {
    137: ("1080p H.264", "H.264"), 136: ("720p H.264", "H.264"), 135: ("480p H.264", "H.264"),
    134: ("360p H.264", "H.264"), 133: ("240p H.264", "H.264"), 160: ("144p H.264", "H.264"),
    248: ("1080p VP9", "VP9"), 247: ("720p VP9", "VP9"), 244: ("480p VP9", "VP9"),
    243: ("360p VP9", "VP9"), 242: ("240p VP9", "VP9"), 278: ("144p VP9", "VP9"),
    299: ("1080p60 VP9", "VP9"), 298: ("720p60 VP9", "VP9"),
    399: ("1080p AV1", "AV1"), 398: ("1080p AV1", "AV1"), 397: ("480p AV1", "AV1"),
    396: ("360p AV1", "AV1"), 395: ("240p AV1", "AV1"), 394: ("144p AV1", "AV1"),
    140: ("音频 m4a 128k", "m4a"), 251: ("音频 opus 160k", "opus"),
    139: ("音频 m4a 48k", "m4a"), 258: ("音频 m4a", "m4a"), 599: ("音频", "?"),
    18: ("360p 合并", "H.264"), 22: ("720p 合并", "H.264"), 37: ("1080p 合并", "H.264"),
}


def cookie_args():
    """YouTube 风控绕过：复制 .chrome_profile 的 cookie 库到临时副本再读。
    直接读运行中的 Chrome profile 会被锁库（yt-dlp #7271）导致下载立即失败；
    复制副本则无论嗅探窗口是否打开都能正常读取。Chrome 写锁是瞬时的，重试几次。"""
    src_cookies = os.path.join(CHROME_PROFILE, "Default", "Network", "Cookies")
    if not os.path.exists(src_cookies):
        return []
    tmp = os.path.join(tempfile.gettempdir(), "ecograb_ck")
    for _ in range(4):
        try:
            if os.path.exists(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(os.path.join(tmp, "Default", "Network"), exist_ok=True)
            ls = os.path.join(CHROME_PROFILE, "Local State")
            if os.path.exists(ls):
                shutil.copy2(ls, os.path.join(tmp, "Local State"))
            shutil.copy2(src_cookies, os.path.join(tmp, "Default", "Network", "Cookies"))
            j = src_cookies + "-journal"
            if os.path.exists(j):
                shutil.copy2(j, os.path.join(tmp, "Default", "Network", "Cookies-journal"))
            return ["--cookies-from-browser", f"chrome:{tmp}"]
        except Exception:
            time.sleep(0.5)
    return []

# ---------- 探测模块：yt-dlp -J ----------
def probe_url(url, timeout=90):
    """返回 (info_dict, error)"""
    try:
        r = subprocess.run(
            [YTDLP, "--ffmpeg-location", FFMPEG, "--no-playlist"] + cookie_args() + ["-J", url],
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
    out = []
    for f in info.get("formats", []):
        vid = f.get("vcodec") or "none"
        aud = f.get("acodec") or "none"
        if vid == "none" and aud == "none":
            continue
        height = f.get("height") or 0
        if height:
            res = f"{f.get('width') or 0}x{height}"
        else:
            res = f.get("format_note") or f.get("resolution") or ""
        size = f.get("filesize") or f.get("filesize_approx")
        out.append({
            "id": f.get("format_id", ""),
            "ext": f.get("ext", ""),
            "res": str(res),
            "vcodec": vid,
            "acodec": aud,
            "size": size,
            "note": f.get("format_note", ""),
        })
    out.sort(key=lambda x: x["vcodec"] == "none")
    return out

def format_size(b):
    if not b:
        return "未知大小"
    b = float(b)
    u = "B"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or u == "TB":
            break
        b /= 1024
    return f"{b:.1f}{u}" if u != "B" else f"{int(b)}B"

def fmt_arg_for(fmt):
    vid = fmt.get("vcodec") or "none"
    aud = fmt.get("acodec") or "none"
    if vid != "none" and aud != "none":
        return fmt["id"]
    if vid != "none":
        return f"{fmt['id']}+bestaudio/best"
    return fmt["id"]

# ---------- 嗅探（CDP） ----------
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

def find_browser():
    for c in CHROME_CANDIDATES:
        if os.path.isfile(c):
            return c
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

def is_video_request(url, headers):
    if not url or url.startswith("data:"):
        return False
    # YouTube 无扩展名流（googlevideo/videoplayback）特征兜底
    if "googlevideo.com" in url or "/videoplayback" in url:
        return True
    ct = (headers.get("content-type") or "").lower()
    if ct.startswith("video/") or ct in ("application/vnd.apple.mpegurl", "application/x-mpegurl",
                                         "application/dash+xml", "application/vnd.ms-sstr+xml"):
        return True
    if "octet-stream" in ct and re.search(r"\.(mp4|webm|flv)(\?|$)", url):
        return True
    return bool(re.search(r"\.(m3u8|mp4|webm|flv|mov|m4s|mpd|f4m)(\?|$)", url))

class Sniffer:
    """CDP 嗅探：启动调试浏览器 → 监听 Network → 识别视频流。
    event_cb(kind, payload): kind in ("captured", url) / ("timeout", None)"""
    def __init__(self, log_cb, event_cb=None):
        self.log_cb = log_cb
        self.event_cb = event_cb
        self.proc = None
        self.ws = None
        self.thread = None
        self.running = False
        self.seen = set()
        self.start_url = "about:blank"
        self._captured = False
        self._timeout_fired = False
        self._auto_clicked = False

    def start(self, url=""):
        browser = find_browser()
        if not browser:
            return "找不到 Chrome/Edge，请手动安装浏览器"
        os.makedirs(CHROME_PROFILE, exist_ok=True)
        cmd = [browser,
               f"--remote-debugging-port={CHROME_PORT}",
               f"--user-data-dir={CHROME_PROFILE}",
               "--no-first-run", "--no-default-browser-check",
               "--remote-allow-origins=*",
               "--disable-extensions",
               "--disable-sync",
               "--disable-features=ExtensionsToolbarMenu,Translate,ReadingList,BookmarkBar",
               f"--app={url or 'about:blank'}"]
        try:
            self.proc = subprocess.Popen(cmd)
        except OSError as e:
            return f"启动浏览器失败: {e}"
        self.start_url = url or "about:blank"
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        # 超时计时：60 秒无捕获 → 提示用户再点一次探测
        threading.Thread(target=self._timeout_watch, daemon=True).start()
        return None

    def _timeout_watch(self):
        time.sleep(SNIFF_TIMEOUT)
        if self.running and not self._captured and not self._timeout_fired:
            self._timeout_fired = True
            if self.event_cb:
                self.event_cb("timeout", None)
            self.stop()

    def _run(self):
        ws_url = None
        for _ in range(40):
            if not self.running:
                return
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{CHROME_PORT}/json")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    tabs = json.loads(resp.read().decode("utf-8", "replace"))
                target = None
                for t in tabs:
                    if t.get("type") != "page":
                        continue
                    u = t.get("url", "")
                    if self.start_url and self.start_url != "about:blank" and u.startswith(self.start_url):
                        target = t
                        break
                if target is None and (not self.start_url or self.start_url == "about:blank"):
                    for t in tabs:
                        if t.get("type") == "page":
                            target = t
                            break
                if target and target.get("webSocketDebuggerUrl"):
                    ws_url = target["webSocketDebuggerUrl"]
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
            self.ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
            self.log_cb("嗅探：已连接，请在播放窗口打开/刷新视频页并点击播放")
            while self.running:
                try:
                    msg = json.loads(self.ws.recv())
                except Exception:
                    if self.running:
                        continue
                    break
                method = msg.get("method", "")
                params = msg.get("params", {})
                if method == "Runtime.consoleAPICalled":
                    try:
                        args_ = params.get("args", [])
                        text = " ".join((a.get("value") if isinstance(a.get("value"), str) else str(a.get("value", ""))) for a in args_)
                        if text:
                            self.log_cb("嗅探JS: " + text)
                    except Exception:
                        pass
                    continue
                url = None
                headers = {}
                if method == "Network.requestWillBeSent":
                    req_ = params.get("request", {})
                    url = req_.get("url")
                    headers = req_.get("headers", {})
                elif method == "Network.responseReceived":
                    resp = params.get("response", {})
                    url = resp.get("url")
                    headers = resp.get("headers", {})
                if url and is_video_request(url, headers):
                    if url in self.seen:
                        continue
                    self.seen.add(url)
                    self._captured = True
                    self.log_cb("嗅探：捕获 " + url)
                    if self.event_cb:
                        self.event_cb("captured", url)
                    if not self._auto_clicked:
                        self._auto_clicked = True
                        threading.Thread(target=self._auto_quality, daemon=True).start()
        except Exception as e:
            self.log_cb(f"嗅探：连接中断 {e}")
        finally:
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass

    def _auto_quality(self):
        """通用清晰度遍历（不依赖站点结构）：
        状态机 —— 0 找档位直显 / 找设置·清晰度按钮打开菜单 → 1 菜单开（找 Quality 子菜单或档位）→ 2 逐个点档位。
        每轮固定等待 5 秒（点档位时新流 2~5 秒出现），最多 8 轮（40 秒封顶）。"""
        time.sleep(1.5)
        if not self.running or not self.ws:
            return
        self.log_cb("嗅探：自动遍历清晰度档位（通用播放器识别，最多约 40 秒）")
        for _ in range(8):
            if not self.running or not self.ws:
                return
            before = len(self.seen)
            js = r"""(() => {
  const isQ = /^(自动|流畅|标清|高清|超清|蓝光|\d{2,4}\s?p|2k|4k)$/i;
  const isMenu = /(清晰度|画质|quality|质量|设置|settings|gear|畫質)/i;
  const qOpts = () => [...document.querySelectorAll('li,div,span,button,a')].filter(e => {
    const t = (e.textContent || '').trim();
    return isQ.test(t) && e.children.length <= 1 && e.offsetParent !== null;
  });
  const st = window.__eq_state || 0;
  if (st === 2) {
    const opts = qOpts();
    if (opts.length) {
      const idx = window.__eq_opt_idx || 0;
      const chosen = opts[Math.min(idx, opts.length - 1)];
      window.__eq_opt_idx = idx + 1;
      if (chosen) { chosen.click(); console.log('EQ: click opt ' + (chosen.textContent || '').trim()); }
      return 'ok';
    }
    console.log('EQ: done no opts'); window.__eq_state = 9; return 'done';
  }
  const opts0 = qOpts();
  if (opts0.length) { window.__eq_state = 2; console.log('EQ: opts visible directly'); return 'ok'; }
  const v = document.querySelector('video');
  const root = v ? (v.closest('.jw-media') || v.parentElement || document) : document;
  const btn = [...root.querySelectorAll('button,div,span,a')].find(e => {
    const t = (e.textContent || '').trim();
    const a = ((e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || '') + ' ' + (e.className || ''));
    const okText = isMenu.test(t) && t.length <= 8 && e.children.length <= 2 && e.offsetParent !== null;
    const okAttr = /(settings|设置|quality|画质|清晰|gear|hd)/i.test(a) && e.offsetParent !== null;
    return (okText || okAttr) && !/jw-nextup|jw-title/i.test(a);
  });
  if (btn) {
    btn.click();
    if (st === 0) window.__eq_state = 1;
    console.log('EQ: menu btn ' + ((btn.textContent || '').trim() || btn.getAttribute('aria-label') || ''));
    return 'ok';
  }
  if (st === 1) {
    const sub = [...document.querySelectorAll('.jw-menu-item, li, div, span, button, a')].find(e => {
      const t = (e.textContent || '').trim();
      const a = (e.getAttribute('aria-label') || '') + ' ' + (e.className || '');
      return (/(quality|画质|清晰度|质量|解析度)/i.test(t) && t.length <= 10 && e.offsetParent !== null) ||
             (/jw-menu-item/.test(a) && /(quality|画质|清晰)/i.test(a));
    });
    if (sub) { sub.click(); console.log('EQ: sub ' + (sub.textContent || '').trim()); return 'ok'; }
    console.log('EQ: done no sub'); window.__eq_state = 9; return 'done';
  }
  console.log('EQ: done no menu btn'); window.__eq_state = 9; return 'done';
})()"""
            try:
                self.ws.send(json.dumps({"id": 70 + _, "method": "Runtime.evaluate",
                                         "params": {"expression": js, "returnByValue": True}}))
            except Exception:
                return
            time.sleep(5)
            if not self.running or not self.ws:
                return
            if len(self.seen) > before:
                self.log_cb(f"嗅探：第 {_+1} 轮有新流，已捕获 {len(self.seen)} 个流")
        self.log_cb("嗅探：清晰度遍历结束（可手动切换清晰度继续捕获）")

    def _probe_capture(self, url, iid):
        """捕获流后台探测（并行）：URL 猜分辨率立即显示 → HEAD 拿大小 → -J 后台补精确。
        三路并行，列表先出 ~清晰度，随后补大小，最后补精确宽高；每行都有信息。"""
        m = re.search(r"(?:^|[/_.-])(\d{3,4})p(?=[/_.-]|$)", url, re.I)
        h = int(m.group(1)) if m else 0
        if h:
            self.q.put(("cap_info", url, iid, None, f"~{h}p"))
        threading.Thread(target=self._probe_j, args=(url, iid, h), daemon=True).start()
        size = self._head_size(url)
        if size:
            self.q.put(("cap_info", url, iid, size, f"~{h}p" if h else ""))
            self.log(f"探测 {url[-60:]}：HEAD 大小 {format_size(size)}")
        else:
            self.log(f"探测 {url[-60:]}：HEAD 失败（无大小），等 yt-dlp -J 兜底")

    def _probe_j(self, url, iid, h):
        """yt-dlp -J 后台补精确分辨率/大小（不覆盖 HEAD 已拿到的大小）"""
        try:
            args = [YTDLP, "--ffmpeg-location", FFMPEG, "--no-playlist", "-J", "--no-warnings"]
            args += cookie_args()
            ref = self._sniff_referer()
            if ref and "googlevideo.com" not in url:
                args += ["--referer", ref]
            args.append(url)
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60, creationflags=NO_WINDOW)
            if r.returncode == 0:
                info = json.loads(r.stdout)
                size2 = info.get("filesize") or info.get("filesize_approx")
                h2 = info.get("height") or 0
                w2 = info.get("width") or 0
                if size2 or h2:
                    old = self.cap_meta.get(url) or {}
                    self.q.put(("cap_info", url, iid, size2 or old.get("size"),
                                (f"{w2}x{h2}" if h2 else (f"~{h}p" if h else ""))))
                    self.log(f"探测 {url[-60:]}：-J 精确 {w2}x{h2} / {format_size(size2) if size2 else '无大小'}")
            else:
                self.log(f"探测 {url[-60:]}：-J 失败 rc={r.returncode}（{((r.stderr or '').strip().splitlines() or [''])[-1][:120]}）")
        except Exception as e:
            self.log(f"探测 {url[-60:]}：-J 异常 {e}")

    def _head_size(self, url):
        """HEAD 拿 Content-Length；被拦则 GET Range: bytes=0-0 从 Content-Range 取总大小"""
        try:
            import urllib.request
            ref = self._sniff_referer() or ""
            for method, headers, is_range in (
                ("HEAD", {"User-Agent": "Mozilla/5.0", "Referer": ref}, False),
                ("GET", {"User-Agent": "Mozilla/5.0", "Referer": ref, "Range": "bytes=0-0"}, True),
            ):
                try:
                    req = urllib.request.Request(url, method=method, headers=headers)
                    with urllib.request.urlopen(req, timeout=10) as r:
                        if is_range:
                            cr = r.headers.get("Content-Range") or ""
                            m = re.search(r"/\s*(\d+)\s*$", cr)
                            if m:
                                return int(m.group(1))
                        else:
                            cl = r.headers.get("Content-Length")
                            if cl:
                                return int(cl)
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _probe_hls(self, url, ph):
        try:
            args = [YTDLP, "--ffmpeg-location", FFMPEG, "--no-playlist", "-J", "--no-warnings"]
            args += cookie_args()
            ref = self._sniff_referer()
            if ref and "googlevideo.com" not in url:
                args += ["--referer", ref]
            args.append(url)
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=90, creationflags=NO_WINDOW)
            if r.returncode != 0:
                self.q.put(("hls_fail", ph, (r.stderr or r.stdout or "")[-300:]))
                return
            info = json.loads(r.stdout)
            self.q.put(("hls_formats", url, ph, extract_formats(info)))
        except Exception as e:
            self.q.put(("hls_fail", ph, str(e)))

    def _show_hls_formats(self, url, ph, fmts):
        try:
            self.fmt_tree.delete(ph)
        except Exception:
            pass
        if not fmts:
            self.log("HLS 流未解析出清晰度，将按默认最高清晰度下载")
            return
        self.hls_formats.append(fmts)
        self.hls_urls.append(url)
        base = len(self.hls_formats) - 1
        for i, f in enumerate(fmts, 1):
            v, a = f["vcodec"], f["acodec"]
            if v != "none" and a != "none":
                codec = f"{v.split('.')[0]}+{a.split('.')[0]}"
            elif v != "none":
                codec = v.split(".")[0] + "（自动合音轨）"
            else:
                codec = a.split(".")[0] + "（纯音频）"
            warn = "⚠" if ("av1" in v or "vp9" in v or "vp08" in v or "vp09" in v) else "✓"
            self.fmt_tree.insert("", 0, iid=f"hls_{base}_{i}", values=(
                f["res"] or "", f["id"], f"{codec} {warn}", fmt_size(f["size"]), "嗅探"))
        self.log(f"HLS 嗅探展开 {len(fmts)} 个清晰度，可直接点选")

    def _sniff_referer(self):
        try:
            u = self.sniffer.start_url
            return u if u and u != "about:blank" else None
        except Exception:
            return None

    def _show_tooltip(self, msg):
        try:
            tip = tk.Toplevel(self.root)
            tip.overrideredirect(True)
            x = self.root.winfo_x() + 60
            y = self.root.winfo_y() + 60
            tip.geometry(f"+{x}+{y}")
            tk.Label(tip, text=msg, bg="#fff8dc", fg="#333",
                     font=("Microsoft YaHei", 11), padx=14, pady=8).pack()
            tip.after(3500, tip.destroy)
        except Exception:
            pass

    # ---------- 格式列表交互 ----------
    def _show_formats(self, fmts):
        self.formats = fmts
        self.fmt_tree.delete(*self.fmt_tree.get_children())
        for i, f in enumerate(fmts, 1):
            v, a = f["vcodec"], f["acodec"]
            if v != "none" and a != "none":
                codec = f"{v.split('.')[0]}+{a.split('.')[0]}"
            elif v != "none":
                codec = v.split(".")[0] + "（自动合音轨）"
            else:
                codec = a.split(".")[0] + "（纯音频）"
            warn = "⚠" if ("av1" in v or "vp9" in v or "vp08" in v or "vp09" in v) else "✓"
            self.fmt_tree.insert("", "end", iid=str(i), values=(
                f["res"] or "", f["id"], f"{codec} {warn}", fmt_size(f["size"]), "探测"))
        self.log(f"探测成功：{len(fmts)} 个格式")

    def _mouse_on_dl_btn(self, e):
        """鼠标当前是否落在下载按钮上（防止事件冒泡把按钮点走/藏掉）"""
        try:
            return self._dl_btn.winfo_containing(e.x_root, e.y_root) is self._dl_btn
        except Exception:
            return False

    def _on_tree_click(self, e):
        if not self._mouse_on_dl_btn(e):
            self._hide_dl_btn()

    def _on_tree_double(self, e):
        row = self.fmt_tree.identify_row(e.y)
        if row:
            self._download_fmt_row(row)

    def _on_tree_motion(self, e):
        if self._mouse_on_dl_btn(e):
            return
        row = self.fmt_tree.identify_row(e.y)
        if not row:
            self._hide_dl_btn()
            self._hover_row = None
            return
        if row == self._hover_row:
            return
        self._hover_row = row
        bbox = self.fmt_tree.bbox(row)
        if bbox:
            _, y, _, h = bbox
            cb = self.fmt_tree.bbox(row, "codec")
            bw = 82
            if cb:
                x = cb[0] + cb[2] - bw
            else:
                x = max(0, bbox[2] - bw)
            self._dl_btn.place(x=x, y=y, width=bw, height=max(18, h))
            self._dl_btn.config(command=lambda r=row: self._download_fmt_row(r))

    def _hide_dl_btn(self):
        self._dl_btn.place_forget()

    def _download_fmt_row(self, row):
        self._hide_dl_btn()
        if not row:
            return
        try:
            idx = int(row) - 1
            fmt = self.formats[idx]
        except (ValueError, IndexError):
            if row.startswith("hls_"):
                try:
                    _, bi, i = row.split("_")
                    fmts = self.hls_formats[int(bi)]
                    fmt = fmts[int(i) - 1]
                    url = self.hls_urls[int(bi)]
                except Exception:
                    return
                fsz = fmt.get("size") or fmt.get("filesize") or fmt.get("filesize_approx")
                if not self._ensure_new_download(url, fmt_arg_for(fmt)):
                    return
                self.pool.add(url, fmt_arg_for(fmt), self.dl_dir_var.get(), None,
                              f"{fmt['res'] or fmt['id']} · {fmt['id']}", True,
                              referer=self._sniff_referer(), size=fsz)
                self.log(f"加入下载池：{fmt['res'] or fmt['id']}（HLS 格式 {fmt['id']}）")
                return
            url = self._capture_url_for_row(row)
            if not url:
                return
            vals = self.fmt_tree.item(row).get("values") or []
            is_audio = bool(vals and "音频" in str(vals[0]))
            name = (str(vals[0]) + " · 捕获") if vals and vals[0] else f"捕获流 {len(self.captured)}"
            meta = self.cap_meta.get(url) or {}
            if not self._ensure_new_download(url, None):
                return
            self.pool.add(url, None, self.dl_dir_var.get(), None,
                          name, True, referer=self._sniff_referer(),
                          size=meta.get("size"))
            if not is_audio:
                self.log("提示：该流是纯视频流（YouTube 分片视频通常无声音），如需声音请再下载对应音频流后合并")
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "URL 为空")
            return
        info = getattr(self, "current_info", None) or {}
        t = (info.get("title") or "").strip() or f"{fmt['res'] or fmt['id']} · {fmt['id']}"
        ext = fmt.get("ext") or "mp4"
        fsz = fmt.get("size") or fmt.get("filesize") or fmt.get("filesize_approx")
        if not self._ensure_new_download(url, fmt_arg_for(fmt)):
            return
        self.pool.add(url, fmt_arg_for(fmt), self.dl_dir_var.get(), None,
                      f"{t}.{ext}", True,
                      referer=url if not url.startswith("about:") else None,
                      size=fsz)
        self.log(f"加入下载池：{fmt['res'] or fmt['id']}（格式 {fmt['id']}）")

    def _capture_url_for_row(self, row):
        try:
            i = self.fmt_tree.index(row)
        except Exception:
            return None
        if 0 <= i < len(self.captured):
            return self.captured[len(self.captured) - 1 - i][0]
        return None

    # ---------- 下载池 UI ----------
    def _ui_event(self, event):
        kind, payload = event
        try:
            self._ui_event_impl(event)
        except Exception as e:
            self.log(f"界面刷新错误[{kind}]：{e}")

    def _ui_event_impl(self, event):
        kind, payload = event
        if kind == "added":
            task = payload
            row = TaskRow(self._pool_inner, task, self)
            task.ui = row
            row.refresh()
        elif kind == "progress":
            if payload.ui:
                payload.ui.refresh()
        elif kind == "paused":
            if payload.ui:
                payload.ui.refresh()
        elif kind == "done":
            task = payload
            if task.ui:
                task.ui.refresh()
            if task.state == "done" and task.out_path and task.mode:
                self.cqueue.add(task.out_path, task.mode, task.ui)
                self.log(f"下载完成，加入压缩队列：{os.path.basename(task.out_path)}（{task.mode}）")
            elif task.state == "done":
                self.log(f"下载完成：{os.path.basename(task.out_path)}（未压缩，可到压缩页手动转）")

    # ---------- 服务 ----------
    def _start_capture_server(self):
        CaptureHandler.APP = self
        try:
            httpd = HTTPServer(("127.0.0.1", CAPTURE_PORT), CaptureHandler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self.log(f"本地接收服务已启动（端口 {CAPTURE_PORT}）")
        except OSError as e:
            self.log(f"本地接收服务启动失败：{e}")

def main():
    root = tk.Tk()
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
