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


_SNIFFER = None
_cookie_lock = threading.Lock()


def cookie_args():
    """优先 CDP 取 cookie（嗅探 Chrome 运行时，Network.getAllCookies 无文件锁问题，
    且拿到的是嗅探窗口的登录态）；兜底复制 .chrome_profile 副本。"""
    if _SNIFFER is not None:
        with _cookie_lock:
            try:
                cf = _SNIFFER.cookie_file()
                if cf:
                    return ["--cookies", cf]
            except Exception:
                pass
    src_cookies = os.path.join(CHROME_PROFILE, "Default", "Network", "Cookies")
    if not os.path.exists(src_cookies):
        return []
    tmp = os.path.join(tempfile.gettempdir(), "ecograb_ck_" + str(os.getpid()))
    for _ in range(4):
        try:
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
        self._base_path = None
        self._cookie_wait = None

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

    def _vid_path(self, url):
        """视频流的'主路径'：去掉文件名后的目录路径，用于区分同一视频的不同清晰度 vs 页面跳转/推荐流"""
        try:
            path = urllib.parse.urlparse(url).path
            parts = [p for p in path.split("/") if p]
            if parts:
                parts = parts[:-1]
            return "/".join(parts)
        except Exception:
            return None

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
                if msg.get("id") == 900:
                    if self._cookie_wait:
                        ev, _ = self._cookie_wait
                        self._cookie_wait = (ev, msg.get("result", {}))
                        ev.set()
                    continue
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
                    vp = self._vid_path(url)
                    if self._base_path is None:
                        self._base_path = vp
                    elif vp and vp != self._base_path:
                        self.log_cb(f"嗅探：检测到页面跳转/推荐视频流（与当前视频不同），已忽略：{url}")
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
        每轮固定等待 3 秒（点档位/API 切换时新流 1~3 秒出现，捕获是事件驱动不丢流），最多 8 轮（约 25 秒封顶）。"""
        time.sleep(1.5)
        if not self.running or not self.ws:
            return
        self.log_cb("嗅探：自动遍历清晰度档位（通用播放器识别，最多约 40 秒）")
        for _ in range(8):
            if not self.running or not self.ws:
                return
            before = len(self.seen)
            js = r"""(() => {
  const isQ = /^(自动|流畅|标清|高清|超清|蓝光|\d{2,4}\s?p?|2k|4k)$/i;
  const v = document.querySelector('video');
  if (v) {
    for (const ev of ['mousemove','pointermove','mouseover','mousedown']) {
      try { v.dispatchEvent(new MouseEvent(ev, {bubbles: true, clientX: 200, clientY: 150, view: window})); } catch(e) {}
    }
  }
  let root = v ? (v.closest('.jwplayer') || v.closest('.jw-media') || v.parentElement.parentElement || v.parentElement || document) : document;
  if (window.__eq_api !== 'jw') {
    try {
      if (typeof window.jwplayer === 'function') {
        const inst = jwplayer();
        if (inst && inst.getQualityLevels) {
          const lv = inst.getQualityLevels();
          if (lv && lv.length) {
            window.__eq_api = 'jw';
            window.__eq_api_levels = lv.map(l => (l && (l.label || l.name)) || '');
            console.log('EQ api: jw ' + JSON.stringify(window.__eq_api_levels));
          }
        }
      }
    } catch(e) {}
  }
  if (window.__eq_api === 'jw') {
    const levels = window.__eq_api_levels || [];
    const total = levels.length;
    const step = window.__eq_api_idx || 0;
    if (step < total) {
      const target = total - 1 - step;
      window.__eq_api_idx = step + 1;
      try { jwplayer().setCurrentQuality(target); console.log('EQ api click: ' + levels[target]); }
      catch(e) { console.log('EQ api err'); }
    } else { window.__eq_state = 9; console.log('EQ api done'); }
    return 'ok';
  }
  const st = window.__eq_state || 0;
  if (window.__eq_dump === undefined) {
    window.__eq_dump = 1;
    try {
      const gears = [...root.querySelectorAll('[class*=settings], [aria-label*="settings" i]')];
      console.log('EQ gears: ' + JSON.stringify(gears.slice(0, 10).map(e => ({
        tag: e.tagName, aria: e.getAttribute('aria-label')||'', cls: (e.className||'').toString().slice(0, 60),
        kids: e.children.length, vis: e.offsetParent!==null
      }))));
    } catch(e) { console.log('EQ gears err'); }
  }
  const qOpts = () => [...root.querySelectorAll('li,div,span,button,a')].filter(e => {
    const t = (e.textContent || '').trim();
    return isQ.test(t) && e.children.length <= 1 && e.offsetParent !== null;
  });
  const settingsBtn = [...root.querySelectorAll('button,div,span,a')].find(e => {
    const t = (e.textContent || '').trim();
    const cls = (e.className || '').toString();
    const ariaTitle = ((e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || '')).trim();
    if (/menu|topbar|submenu|close/i.test(ariaTitle + ' ' + cls)) return false;
    const okCls = /jw-icon-settings/.test(cls);
    const okAttr = /(^|\s)(settings|设置|齿轮)(\s|$)/i.test(ariaTitle);
    const okText = /^(settings|设置|齿轮)$/i.test(t);
    return (okCls || okAttr || okText) && e.children.length <= 6;
  });
  const findSub = (scope) => {
    const topBar = [...scope.querySelectorAll('.jw-settings-topbar [class*=jw-icon], .jw-settings-topbar-buttons [class*=jw-icon], .jw-settings-topbar [class*=settings]')].find(e => {
      const t = (e.textContent || '').trim();
      const aria = (e.getAttribute('aria-label') || '').trim();
      return ((/(quality|画质|清晰度|解析度)/i.test(t) && t.length <= 12) || /^(quality|画质|清晰度|解析度)$/i.test(aria)) && e.offsetParent !== null;
    });
    if (topBar) return topBar;
    return [...scope.querySelectorAll('.jw-menu-item, .jw-settings-content-item, li, div, span, button, a')].find(e => {
      const t = (e.textContent || '').trim();
      const a = (e.getAttribute('aria-label') || '') + ' ' + (e.className || '');
      return (/(quality|画质|清晰度|质量|解析度)/i.test(t) && t.length <= 10 && e.offsetParent !== null) ||
             (/jw-menu-item|jw-settings-content-item/.test(a) && /(quality|画质|清晰)/i.test(a)) ||
             (/^(quality|画质|清晰度)$/i.test((e.getAttribute('aria-label')||'').trim()) && e.offsetParent !== null);
    });
  };
  if (st === 2) {
    const opts = qOpts();
    if (opts.length) {
      const idx = window.__eq_opt_idx || 0;
      const chosen = opts[Math.min(idx, opts.length - 1)];
      window.__eq_opt_idx = idx + 1;
      if (chosen) { chosen.click(); console.log('EQ: click opt ' + (chosen.textContent || '').trim()); }
      return 'ok';
    }
    const sub = findSub(root);
    if (sub) { sub.click(); console.log('EQ: sub again'); return 'ok'; }
    console.log('EQ: done no opts'); window.__eq_state = 9; return 'done';
  }
  if (st === 1) {
    const menuEl = root.querySelector('.jw-settings-menu, [class*=settings-menu]');
    const open = menuEl && menuEl.offsetParent !== null;
    if (!open) {
      if (settingsBtn) { settingsBtn.click(); console.log('EQ: reopen settings'); return 'ok'; }
      console.log('EQ: done no settings'); window.__eq_state = 9; return 'done';
    }
    const sub = findSub(root);
    if (sub) { sub.click(); window.__eq_state = 2; console.log('EQ: sub ' + (sub.textContent || '').trim()); return 'ok'; }
    const opts = qOpts();
    if (opts.length) { window.__eq_state = 2; console.log('EQ: opts visible'); return 'ok'; }
    console.log('EQ: menu open no opts yet'); return 'ok';
  }
  const sub = findSub(root);
  if (sub) { sub.click(); window.__eq_state = 2; console.log('EQ: sub ' + (sub.textContent || '').trim()); return 'ok'; }
  const opts = qOpts();
  if (opts.length) { window.__eq_state = 2; console.log('EQ: opts visible'); return 'ok'; }
  if (settingsBtn) { settingsBtn.click(); window.__eq_state = 1; console.log('EQ: settings btn ' + (settingsBtn.getAttribute('aria-label') || (settingsBtn.textContent||'').trim() || 'gear')); return 'ok'; }
  console.log('EQ: done no settings'); window.__eq_state = 9; return 'done';
})()"""
            try:
                self.ws.send(json.dumps({"id": 70 + _, "method": "Runtime.evaluate",
                                         "params": {"expression": js, "returnByValue": True}}))
            except Exception:
                return
            time.sleep(3)
            if not self.running or not self.ws:
                return
            if len(self.seen) > before:
                self.log_cb(f"嗅探：第 {_+1} 轮有新流，已捕获 {len(self.seen)} 个流")
        self.log_cb("嗅探：清晰度遍历结束（可手动切换清晰度继续捕获）")

    def cookie_file(self):
        """CDP Network.getAllCookies → 写 NetScape 格式 cookie 文件（无文件锁问题）。
        返回文件路径；失败返回 None。"""
        if not self.ws or not self.running:
            return None
        try:
            ev = threading.Event()
            self._cookie_wait = (ev, None)
            self.ws.send(json.dumps({"id": 900, "method": "Network.getAllCookies"}))
            ev.wait(4)
            res = self._cookie_wait[1]
            if not res:
                return None
            cookies = res.get("cookies", [])
            if not cookies:
                return None
            path = os.path.join(tempfile.gettempdir(), f"ecograb_cookies_{os.getpid()}.txt")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("# Netscape HTTP Cookie File\n")
                f.write("# This file was generated by ECOgrab. Do not edit.\n\n")
                for c in cookies:
                    domain = c.get("domain", "")
                    inc = "TRUE" if domain.startswith(".") else "FALSE"
                    try:
                        expiry = int(c.get("expires", 0))
                    except Exception:
                        expiry = 0
                    sec = "TRUE" if c.get("secure") else "FALSE"
                    name = c.get("name", "")
                    value = c.get("value", "")
                    pp = c.get("path", "/") or "/"
                    f.write(f"{domain}\t{inc}\t{pp}\t{sec}\t{expiry}\t{name}\t{value}\n")
            return path
        except Exception:
            return None

    def stop(self):
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

# ---------- 本地接收（书签通道，保留备用） ----------
class CaptureHandler(BaseHTTPRequestHandler):
    APP = None
    def do_POST(self):
        try:
            if self.path == "/capture":
                ln = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(ln).decode("utf-8", "replace")
                data = json.loads(body)
                url = data.get("url")
                if url and self.APP:
                    self.APP.on_capture_url(url)
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

# ---------- 下载池 ----------
def build_dl_cmd(url, fmt_arg, out_dir, task_id, use_cookie=False, referer=None):
    tmpdir = os.path.join(out_dir, f".ecograb_{task_id}")
    os.makedirs(tmpdir, exist_ok=True)
    cmd = [YTDLP, "--ffmpeg-location", FFMPEG, "--no-playlist", "--newline",
           "--progress-delta", "0.5"]
    if fmt_arg:
        cmd += ["-f", fmt_arg]
    if "googlevideo.com" in url or "/videoplayback" in url:
        cmd += ["--referer", "https://www.youtube.com/"]
    elif referer:
        cmd += ["--referer", referer]
    cmd += ["--merge-output-format", "mp4",
            "-o", os.path.join(tmpdir, "%(title)s.%(ext)s"),
            "-c"]
    if use_cookie:
        cmd += cookie_args()
    cmd.append(url)
    return cmd, tmpdir

class DownloadTask:
    """单个下载任务；state: waiting/downloading/paused/done/failed"""
    def __init__(self, url, fmt_arg, out_dir, mode, title, pool, task_id, use_cookie=False, referer=None, size=None):
        self.url = url
        self.fmt_arg = fmt_arg
        self.out_dir = out_dir
        self.mode = mode
        self.title = title
        self.pool = pool
        self.task_id = task_id
        self.use_cookie = use_cookie
        self.referer = referer
        self.state = "waiting"
        self.progress = 0.0
        self.size_str = ""
        self.total_known = bool(size)
        self.dl_bytes = 0
        self._total_bytes = int(size) if size else 0
        self.proc = None
        self.out_path = None
        self.tmpdir = None
        self.ui = None   # 任务行组件

    def start(self):
        cmd, self.tmpdir = build_dl_cmd(self.url, self.fmt_arg, self.out_dir, self.task_id,
                                         self.use_cookie, self.referer)
        self.state = "downloading"
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace",
                                     creationflags=NO_WINDOW)
        if not self.fmt_arg:
            threading.Thread(target=self._preprobe, daemon=True).start()
        threading.Thread(target=self._run, daemon=True).start()
        if self.ui:
            self.ui.refresh()

    def _preprobe(self):
        """嗅探流直链：下载前用 yt-dlp -J 拿总大小和真实标题（解决摆锤和文件名）"""
        try:
            args = [YTDLP, "--ffmpeg-location", FFMPEG, "--no-playlist", "-J", "--no-warnings"]
            args += cookie_args()
            if self.referer and "googlevideo.com" not in self.url:
                args += ["--referer", self.referer]
            args.append(self.url)
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=25, creationflags=NO_WINDOW)
            if r.returncode != 0:
                return
            info = json.loads(r.stdout)
            size = info.get("filesize") or info.get("filesize_approx")
            if size:
                self._total_bytes = int(size)
                self.total_known = True
                if self.ui:
                    self.pool.on_task_update(self)
            title = info.get("title")
            if title:
                ext = info.get("ext") or "mp4"
                self.title = f"{title}.{ext}"
                if self.ui:
                    self.pool.on_task_update(self)
        except Exception:
            pass

    def _run(self):
        _unit = {"KiB": 1024, "MiB": 1048576, "GiB": 1073741824}

        # 兜底：嗅探流直链 yt-dlp 可能不输出进度行，先 HEAD 拿总大小（仅流 URL，referer 用真实页面）
        if self.url and not self.url.startswith("file://") and self.referer and self.referer != self.url:
            try:
                import urllib.request
                req = urllib.request.Request(self.url, method="HEAD",
                                             headers={"User-Agent": "Mozilla/5.0",
                                                      "Referer": self.referer})
                with urllib.request.urlopen(req, timeout=10) as r:
                    cl = r.headers.get("Content-Length")
                    if cl:
                        self._total_bytes = int(cl)
                        self.total_known = True
            except Exception:
                pass

        def poll_part():
            while self.state == "downloading":
                if self.tmpdir and os.path.isdir(self.tmpdir):
                    got = 0
                    name = None
                    npart = []
                    try:
                        for f in os.listdir(self.tmpdir):
                            fp = os.path.join(self.tmpdir, f)
                            if not os.path.isfile(fp):
                                continue
                            if f.endswith(".part"):
                                got += os.path.getsize(fp)
                            elif not f.endswith((".ytdl", ".json")):
                                npart.append(os.path.getsize(fp))
                            if not name or len(f) > len(name):
                                name = f
                    except OSError:
                        pass
                    if npart:
                        got += max(npart)
                    changed = False
                    if name:
                        n = name[:-5] if name.endswith(".part") else name
                        n = re.sub(r"\.f\d+(?=\.)", "", n)
                        if n and n != self.title:
                            self.title = n
                            changed = True
                    if got != self.dl_bytes:
                        self.dl_bytes = got
                        changed = True
                    if self._total_bytes > 0:
                        self.total_known = True
                        pct = min(99.9, got / self._total_bytes * 100)
                        if pct > self.progress:
                            self.progress = pct
                            changed = True
                    if changed and self.ui:
                        self.pool.on_task_update(self)
                time.sleep(0.5)

        threading.Thread(target=poll_part, daemon=True).start()
        for line in self.proc.stdout:
            line = line.strip()
            m = re.search(r"\[download\]\s+([\d.]+)% of ~?([\d.]+(?:MiB|GiB|KiB))", line)
            if m:
                self.progress = float(m.group(1))
                self.size_str = m.group(2)
                mm = re.match(r"([\d.]+)(MiB|GiB|KiB)", m.group(2))
                if mm:
                    self._total_bytes = int(float(mm.group(1)) * _unit[mm.group(2)])
                    self.total_known = True
                if self.ui:
                    self.pool.on_task_update(self)
            elif line.startswith("ERROR"):
                self.pool.log(f"下载错误：{line}")
        self.proc.wait()
        if self.state == "paused":
            self.pool.log("已暂停，断点保留，可点「继续」续传")
            return
        ok = self.proc.returncode == 0
        out = None
        if self.tmpdir and os.path.isdir(self.tmpdir):
            for f in os.listdir(self.tmpdir):
                p = os.path.join(self.tmpdir, f)
                if os.path.isfile(p) and f.lower().endswith((".mp4", ".mkv", ".webm")):
                    out = p
                    break
        if ok and out:
            dest = os.path.join(self.out_dir, os.path.basename(out))
            base, ext = os.path.splitext(dest)
            i = 1
            while os.path.exists(dest):
                dest = f"{base}_{i}{ext}"
                i += 1
            try:
                shutil.move(out, dest)
            except OSError as e:
                dest = out
                self.pool.log(f"移动文件失败：{e}")
            try:
                shutil.rmtree(self.tmpdir, ignore_errors=True)
            except Exception:
                pass
            self.out_path = dest
            self.state = "done"
        else:
            self.state = "failed"
            if not ok:
                self.pool.log(f"下载失败（退出码 {self.proc.returncode}）：{self.url}")
            else:
                self.pool.log(f"下载进程正常退出但未找到成品文件，请查看下方日志；URL：{self.url}")
        self.progress = 100.0 if ok else self.progress
        self.pool.on_task_done(self)

    def pause(self):
        if self.state == "downloading" and self.proc:
            self.state = "paused"
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.pool.on_task_paused(self)

    def resume(self):
        if self.state == "paused":
            self.state = "waiting"
            threading.Thread(target=self.pool._schedule, args=(self,), daemon=True).start()

    def retry(self):
        if self.state != "failed":
            return
        if self.tmpdir and os.path.isdir(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        self.progress = 0.0
        self.size_str = ""
        self.out_path = None
        self.state = "waiting"
        if self.ui:
            self.pool.on_task_update(self)
        threading.Thread(target=self.pool._schedule, args=(self,), daemon=True).start()

class DownloadPool:
    def __init__(self, on_update, log_cb, max_concurrent=MAX_CONCURRENT):
        self.sem = threading.Semaphore(max_concurrent)
        self.tasks = []
        self.task_seq = 0
        self.on_update = on_update
        self.log = log_cb

    def add(self, url, fmt_arg, out_dir, mode, title, use_cookie=False, referer=None, size=None):
        self.task_seq += 1
        t = DownloadTask(url, fmt_arg, out_dir, mode, title, self, self.task_seq,
                         use_cookie, referer, size=size)
        self.tasks.append(t)
        self.on_update(("added", t))
        threading.Thread(target=self._schedule, args=(t,), daemon=True).start()
        return t

    def _schedule(self, task):
        if task.state == "paused":
            return
        self.sem.acquire()
        if task.state != "waiting":
            self.sem.release()
            return
        task.start()

    def on_task_update(self, task):
        self.on_update(("progress", task))

    def remove(self, task):
        if task in self.tasks:
            self.tasks.remove(task)
        if task.proc and task.proc.poll() is None:
            try:
                task.proc.terminate()
            except Exception:
                pass
        if task.state != "done":
            if task.tmpdir and os.path.isdir(task.tmpdir):
                shutil.rmtree(task.tmpdir, ignore_errors=True)
        ui = task.ui
        task.ui = None
        if ui:
            try:
                ui.frame.destroy()
            except Exception:
                pass

    def on_task_done(self, task):
        self.sem.release()
        self.on_update(("done", task))

    def on_task_paused(self, task):
        self.sem.release()  # 让出并发名额，其他任务可继续
        self.on_update(("paused", task))

# ---------- 压缩队列 ----------
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

def compress_video(in_path, mode_name, log_cb):
    out_path = os.path.splitext(in_path)[0] + "_压缩.mp4"
    mode = COMPRESS_MODES[mode_name]
    if mode["codec"] == "hevc_nvenc":
        vargs = ["-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq",
                 "-rc", "vbr", "-b:v", "2500k", "-maxrate", "3250k",
                 "-bufsize", "5000k", "-cq", "27", "-spatial-aq", "1"]
    else:
        vargs = ["-c:v", mode["codec"]] + list(mode["params"])
    cmd = [FFMPEG, "-hide_banner", "-y", "-i", in_path,
           "-map", "0:v:0", "-map", "0:a?", *vargs, "-c:a", "copy",
           "-progress", "pipe:1", "-movflags", "+faststart", out_path]
    total = get_duration(in_path)
    log_cb(f"压缩：{os.path.basename(in_path)} → {os.path.basename(out_path)}（{mode_name}）")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=NO_WINDOW)
    t0 = time.time()
    for raw in proc.stdout:
        line = raw.strip()
        if line.startswith("out_time_us="):
            try:
                cur = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                continue
            if total:
                log_cb(f"压缩进度：{min(cur/total*100, 100):.1f}% ({fmt_time(cur)}/{fmt_time(total)})")
    proc.wait()
    if proc.returncode != 0:
        log_cb("压缩失败")
        return False, None
    log_cb(f"压缩完成，耗时 {fmt_time(time.time()-t0)}")
    return True, out_path

class CompressQueue:
    def __init__(self, log_cb, on_event=None):
        self.q = queue.Queue()
        self.log = log_cb
        self.on_event = on_event
        self.busy = False
        threading.Thread(target=self._worker, daemon=True).start()

    def add(self, path, mode, ui_ref=None):
        self.q.put((path, mode, ui_ref))
        if self.on_event:
            self.on_event(("queued", None))

    def _worker(self):
        while True:
            path, mode, ui_ref = self.q.get()
            self.busy = True
            if ui_ref:
                ui_ref.show_compress(0.0)
            ok, out = compress_video(path, mode, self.log)
            self.busy = False
            if ui_ref:
                ui_ref.show_compress(None)
            if self.on_event:
                self.on_event(("compressed", (path, out, ok)))

# ---------- GUI ----------
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

class TaskRow:
    """下载池中的一行任务"""
    def __init__(self, parent, task, app):
        self.task = task
        self.app = app
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill="x", padx=4, pady=2)
        self.name = ttk.Label(self.frame, text=task.title, width=38, anchor="w")
        self.name.pack(side="left")
        self.bar = ttk.Progressbar(self.frame, length=130, maximum=100)
        self.bar.pack(side="left", padx=4)
        self.pct = ttk.Label(self.frame, text="0%", width=14)
        self.pct.pack(side="left")
        self.state_lbl = ttk.Label(self.frame, text="排队", width=12)
        self.state_lbl.pack(side="left")
        self.pause_btn = ttk.Button(self.frame, text="暂停", width=5, command=self._on_pause)
        self.pause_btn.pack(side="left", padx=2)
        self.resume_btn = ttk.Button(self.frame, text="继续", width=5, command=self._on_resume)
        self.resume_btn.pack(side="left", padx=2)
        self.resume_btn.config(state="disabled")
        self.mode_cb = ttk.Combobox(self.frame, state="readonly", width=15,
                                    values=["不压缩"] + list(COMPRESS_MODES.keys()))
        self.mode_cb.current(0 if not task.mode else list(COMPRESS_MODES.keys()).index(task.mode) + 1)
        self.mode_cb.pack(side="left", padx=2)
        self.mode_cb.bind("<<ComboboxSelected>>", self._on_mode)
        self._bar_mode = "determinate"
        self.del_btn = ttk.Button(self.frame, text="删除", width=5, command=self._on_delete)
        self.del_btn.pack(side="left", padx=2)

    def _on_pause(self):
        self.task.pause()
    def _on_resume(self):
        if self.task.state == "failed":
            self.task.retry()
        else:
            self.task.resume()
    def _on_delete(self):
        st = self.task.state
        if st in ("downloading", "paused"):
            if not messagebox.askyesno("删除任务",
                    "该任务仍在下载/暂停中，删除会中断并丢弃未完成的文件。确定删除？",
                    parent=self.app.root):
                return
        elif st == "done" and self.task.out_path and os.path.exists(self.task.out_path):
            if messagebox.askyesno("删除任务",
                    f"是否同时删除已下载的文件？\n\n{os.path.basename(self.task.out_path)}\n\n是 = 连文件一起删除\n否 = 仅从列表移除",
                    parent=self.app.root):
                try:
                    os.remove(self.task.out_path)
                    self.app.log(f"已删除文件：{os.path.basename(self.task.out_path)}")
                except OSError as e:
                    self.app.log(f"删除文件失败：{e}")
        self.task.pool.remove(self.task)
        self.app.log(f"已删除任务：{self.task.title}")
    def _on_mode(self, _e=None):
        v = self.mode_cb.get()
        self.task.mode = None if v == "不压缩" else v

    def refresh(self):
        s = self.task.state
        self.name["text"] = self.task.title
        mode = "indeterminate" if (s == "downloading" and not self.task.total_known) else "determinate"
        if mode != self._bar_mode:
            self._bar_mode = mode
            self.bar.config(mode=mode)
            if mode == "indeterminate":
                self.bar.start(12)
            else:
                self.bar.stop()
        if mode == "determinate":
            self.bar["value"] = self.task.progress
        if s == "downloading" and not self.task.total_known:
            mb = self.task.dl_bytes / 1048576
            self.pct["text"] = f"已下 {mb:.0f}MB" if mb >= 1 else f"已下 {int(self.task.dl_bytes)}B"
        elif self.task.state == "downloading" and self.task.size_str:
            self.pct["text"] = f"{self.task.progress:.0f}%（共 {self.task.size_str}）"
        else:
            self.pct["text"] = f"{self.task.progress:.0f}%"
        if s == "waiting":
            self.state_lbl["text"] = "排队"
            self.pause_btn.config(state="disabled")
            self.resume_btn.config(state="disabled", text="继续")
        elif s == "downloading":
            self.state_lbl["text"] = "下载中"
            self.pause_btn.config(state="normal")
            self.resume_btn.config(state="disabled", text="继续")
        elif s == "paused":
            self.state_lbl["text"] = "已暂停"
            self.pause_btn.config(state="disabled")
            self.resume_btn.config(state="normal", text="继续")
        elif s == "done":
            self.state_lbl["text"] = "完成"
            self.pause_btn.config(state="disabled")
            self.resume_btn.config(state="disabled", text="继续")
            self._grey(True)
        elif s == "failed":
            self.state_lbl["text"] = "失败"
            self.pause_btn.config(state="disabled")
            self.resume_btn.config(state="normal", text="重新下载")
            self._grey(True)
            self.resume_btn.config(state="normal")

    def show_compress(self, pct):
        if pct is None:
            self.state_lbl["text"] = "完成"
            return
        self.state_lbl["text"] = f"压缩中 {pct:.0f}%"

    def _grey(self, grey):
        st = "disabled" if grey else "normal"
        for w in (self.name, self.bar, self.pct, self.state_lbl, self.pause_btn, self.resume_btn, self.mode_cb):
            try:
                w.config(state=st)
            except Exception:
                pass

class App:
    def __init__(self, root):
        self.root = root
        root.title("ECOgrab 视频下载压缩工具")
        root.geometry("980x820")
        self.q = queue.Queue()
        self.sniffer = None
        self.current_info = None
        self.formats = []
        self.captured = []
        self.cap_meta = {}
        self.hls_formats = []
        self.hls_urls = []
        self._hover_row = None
        self._dl_btn = None
        self.log_visible = False
        self.pool = DownloadPool(lambda ev: self.q.put(("ui", ev)), self.log)
        self.cqueue = CompressQueue(self.log, self._ui_event)
        self._build_ui()
        self._start_capture_server()
        root.after(100, self._poll_queue)

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="视频页 URL:").pack(side="left")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(top, textvariable=self.url_var, width=64)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=4)
        self._make_rightclick_menu(self.url_entry)
        ttk.Button(top, text="探测格式", command=self.on_probe).pack(side="left", padx=2)

        # 下载目录
        drow = ttk.Frame(self.root)
        drow.pack(fill="x", **pad)
        ttk.Label(drow, text="下载到:").pack(side="left")
        self.dl_dir_var = tk.StringVar(value=SCRIPT_DIR)
        ttk.Entry(drow, textvariable=self.dl_dir_var, width=64).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(drow, text="浏览…", command=self._pick_dir).pack(side="left", padx=2)

        # 格式列表（探测/嗅探共用）
        frm = ttk.LabelFrame(self.root, text="格式列表（鼠标移到行上点「下载」；⚠=AV1/VP9 部分播放器不支持）")
        frm.pack(fill="both", expand=True, **pad)
        cols = ("res", "fid", "codec", "size", "src")
        self.fmt_tree = ttk.Treeview(frm, columns=cols, show="headings", height=9)
        for k, (t, w) in {"res": ("分辨率", 120), "fid": ("格式ID", 90), "codec": ("编码", 160),
                          "size": ("大小", 90), "src": ("来源", 60)}.items():
            self.fmt_tree.heading(k, text=t)
            self.fmt_tree.column(k, width=w, anchor="w")
        self.fmt_tree.pack(fill="both", expand=True)
        self.fmt_tree.bind("<Motion>", self._on_tree_motion)
        self.fmt_tree.bind("<Leave>", lambda e: self._hide_dl_btn())
        self.fmt_tree.bind("<Button-1>", self._on_tree_click)
        self.fmt_tree.bind("<Double-1>", self._on_tree_double)
        self._dl_btn = ttk.Button(self.fmt_tree, text="⬇ 下载", width=8)
        self._dl_btn.place_forget()

        # 下载池
        pf = ttk.LabelFrame(self.root, text="下载池（并行下载，可暂停/恢复；每行可选压缩模式）")
        pf.pack(fill="both", expand=True, **pad)
        self._pool_canvas = tk.Canvas(pf, height=190)
        sb = ttk.Scrollbar(pf, orient="vertical", command=self._pool_canvas.yview)
        self._pool_inner = ttk.Frame(self._pool_canvas)
        self._pool_win = self._pool_canvas.create_window((0, 0), window=self._pool_inner, anchor="nw")
        self._pool_inner.bind("<Configure>", lambda e: self._pool_canvas.configure(scrollregion=self._pool_canvas.bbox("all")))
        self._pool_canvas.bind("<Configure>", lambda e: self._pool_canvas.itemconfigure(self._pool_win, width=e.width))
        self._pool_canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # 日志抽屉
        self._log_btn = ttk.Button(self.root, text="▸ 日志（调试）", command=self._toggle_log)
        self._log_btn.pack(anchor="w", padx=8)
        self.log_text = tk.Text(self.root, height=8, state="disabled", wrap="word")
        self.logfile = os.path.join(SCRIPT_DIR, "ecograb.log")
        try:
            with open(self.logfile, "a", encoding="utf-8") as _lf:
                _lf.write(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        except Exception:
            pass
        self.log("就绪：粘贴视频地址 → 解析；失败会引导你播放视频页。")

    def _make_rightclick_menu(self, widget):
        """右键直接粘贴（tkinter 默认没有右键粘贴）"""
        def paste(e):
            try:
                if widget.clipboard_get():
                    widget.delete(0, "end")
                    widget.event_generate("<<Paste>>")
            except Exception:
                pass
        widget.bind("<Button-3>", paste)

    def _pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.dl_dir_var.get() or SCRIPT_DIR)
        if d:
            self.dl_dir_var.set(d)

    def _ensure_new_download(self, url, fmt_arg):
        """同 URL 同格式已在池中 → 提示；已下载完成 → 询问是否重新下载"""
        for t in self.pool.tasks:
            if t.url == url and t.fmt_arg == fmt_arg:
                if t.state in ("downloading", "paused", "waiting"):
                    messagebox.showinfo("已在下载池",
                        "该视频（此清晰度/格式）已在下载池中（下载中/排队/暂停），无需重复添加。",
                        parent=self.root)
                    return False
                if t.state == "done":
                    return messagebox.askyesno("已下载过",
                        "该视频（此清晰度/格式）此前已下载完成。\n是否重新下载？",
                        parent=self.root)
        return True

    # ---------- 日志与队列 ----------
    def log(self, msg):
        self.q.put(("log", msg))

    def _show_log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        try:
            lines = int(self.log_text.index("end-1c").split(".")[0])
            if lines > 500:
                self.log_text.delete("1.0", f"{lines - 300}.0")
        except Exception:
            pass
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        try:
            with open(self.logfile, "a", encoding="utf-8") as _lf:
                _lf.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    def _toggle_log(self):
        if self.log_visible:
            self.log_text.pack_forget()
            self._log_btn.config(text="▸ 日志（调试）")
            self.log_visible = False
        else:
            self.log_text.pack(fill="both", expand=True, padx=8, pady=4)
            self._log_btn.config(text="▾ 日志（调试）")
            self.log_visible = True

    def _poll_queue(self):
        n = 0
        try:
            while n < 200:
                item = self.q.get_nowait()
                n += 1
                kind = item[0]
                if kind == "log":
                    self._show_log(item[1])
                elif kind == "formats":
                    self._show_formats(item[1])
                elif kind == "probe_fail":
                    self._probe_failed(item[1])
                elif kind == "capture":
                    self._on_capture_url(item[1])
                elif kind == "hls_formats":
                    _, url, ph, fmts = item
                    self._show_hls_formats(url, ph, fmts)
                elif kind == "hls_fail":
                    _, ph, err = item
                    try:
                        self.fmt_tree.delete(ph)
                    except Exception:
                        pass
                    self.log(f"HLS 清晰度解析失败：{err[-200:]}")
                elif kind == "cap_info":
                    _, url, iid, size, res = item
                    try:
                        self.cap_meta[url] = {"size": size, "res": res}
                        vals = list(self.fmt_tree.item(iid).get("values") or [])
                        if vals:
                            if res:
                                vals[0] = res
                            vals[3] = format_size(size) if size else "未知大小"
                            self.fmt_tree.item(iid, values=vals)
                    except Exception:
                        pass
                elif kind == "sniff_timeout":
                    messagebox.showinfo("未捕获到视频",
                        "这一分钟内没有嗅探到视频文件。\n请确认已在播放窗口打开视频页并点击播放，然后重新点「探测格式」。")
                elif kind == "ui":
                    self._ui_event(item[1])
                elif kind in ("added", "progress", "paused"):
                    self._ui_event((kind, item[1]))
                elif kind == "done":
                    self._ui_event(("done", item[1]))
                elif kind == "compressed":
                    path, out, ok = item[1]
                    self.log(f"压缩{'成功' if ok else '失败'}：{os.path.basename(out or path)}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    # ---------- 探测与嗅探 ----------
    def on_probe(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请先粘贴视频页 URL")
            return
        # 新地址：清空上一轮列表与捕获，避免新旧结果混在一起
        self.formats = []
        self.captured = []
        self.current_info = None
        self._hover_row = None
        self._hide_dl_btn()
        self.fmt_tree.delete(*self.fmt_tree.get_children())
        self.log(f"正在探测：{url}")
        threading.Thread(target=self._probe_worker, args=(url,), daemon=True).start()

    def _probe_worker(self, url):
        info, err = probe_url(url)
        if info:
            fmts = extract_formats(info)
            if fmts:
                self.q.put(("formats", fmts))
                return
        self.q.put(("probe_fail", (url, err or "未找到格式")))

    def _probe_failed(self, payload):
        url, err = payload
        self.log(f"探测失败：{err}")
        win = tk.Toplevel(self.root)
        win.title("需要你播放一次")
        win.geometry("480x210")
        win.transient(self.root)
        tk.Label(win, text="视频文件藏得比较深，需要你在我们的窗口\n再点击一次播放。",
                 font=("Microsoft YaHei", 13), pady=12).pack()
        tk.Label(win, text="需要登录的网站请先在播放窗口登录一次（登录状态会保留，以后免登录）。",
                 fg="#666").pack()
        btns = tk.Frame(win)
        btns.pack(pady=10)
        tk.Button(btns, text="打开播放窗口", width=14,
                  command=lambda: self._start_auto_sniff(url, win)).pack(side="left", padx=6)
        tk.Button(btns, text="取消", width=10, command=win.destroy).pack(side="left", padx=6)

    def _start_auto_sniff(self, url, win):
        try:
            win.destroy()
        except Exception:
            pass
        self.log(f"嗅探：打开播放窗口 {url}")
        global _SNIFFER
        self.sniffer = Sniffer(self.log, self._on_sniff_event)
        _SNIFFER = self.sniffer
        err = self.sniffer.start(url)
        if err:
            messagebox.showerror("错误", err)

    def _on_sniff_event(self, kind, payload):
        if kind == "captured":
            self.q.put(("capture", payload))
        elif kind == "timeout":
            self.q.put(("sniff_timeout",))

    def _on_capture_url(self, url):
        if any(u == url for u, in self.captured):
            return
        self.captured.append((url,))
        try:
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(url).query)
        except Exception:
            q = {}
        mime = q.get("mime", [""])[0]
        itag = q.get("itag", [""])[0]
        itag_label = ITAG_LABELS.get(int(itag)) if itag.isdigit() else None
        if itag_label:
            res_label, codec_label = itag_label
        elif mime.startswith("audio/"):
            res_label, codec_label = "音频流", (mime.split("/")[1] or "音频")
        elif mime.startswith("video/"):
            res_label, codec_label = "视频流", (mime.split("/")[1] or "视频")
        elif ".m3u8" in url:
            res_label, codec_label = "视频流(HLS)", "HLS"
            self._hls_seq = getattr(self, "_hls_seq", 0) + 1
            ph = f"hls_ph_{self._hls_seq}"
            self.fmt_tree.insert("", 0, iid=ph, values=(res_label, "捕获", codec_label, "解析中…", "嗅探"))
            threading.Thread(target=self._probe_hls, args=(url, ph), daemon=True).start()
            return
        elif ".mpd" in url:
            res_label, codec_label = "视频流(DASH)", "DASH"
        elif ".mp4" in url:
            res_label, codec_label = "视频流", "mp4"
        elif "video/webm" in mime:
            res_label, codec_label = "视频流", "webm"
        else:
            res_label, codec_label = "视频流?", "未知"
        iid = self.fmt_tree.insert("", 0, values=(
            res_label, "捕获", codec_label, "解析中…", "嗅探"))
        self._show_tooltip("已捕获视频文件，请回到主窗口点「下载」")
        if ".m3u8" not in url and ".mpd" not in url and "音频" not in res_label:
            threading.Thread(target=self._probe_capture, args=(url, iid), daemon=True).start()
        if "音频" in res_label:
            self.log(f"嗅探捕获音频流：{url}")
        else:
            self.log(f"嗅探捕获视频流 {res_label}：{url}")

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
