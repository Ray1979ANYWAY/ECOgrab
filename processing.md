# eazyVid 项目工作记录（processing.md）

> **2026-09-26 改名**：项目由 **ECOgrab** 正式更名为 **eazyVid**（本地文件夹 `D:\Documents\eazyVid`、GitHub 仓库 `Ray1979ANYWAY/eazyVid`、主程序 `eazyvid.py`、日志 `eazyvid.log`、嗅探 profile `.eazyvid_profile`、cookie 临时前缀 `eazyvid_ck_*`/`eazyvid_cookies_*`、下载临时目录 `.eazyvid_*`）。
> 改名原因：ECOgrab 无法从名字看出与视频下载/压缩相关；easyVDO 撞车泰语视频教育站（easyvdo.com）+ 大陆集团 VDO 汽车品牌；标准拼写 easyVid 撞车活跃 AI 视频平台（easyvid.app）与 EasyVid Video Converter；最终选定 **eazyVid**——eazy→easy 联想、vid→video 联想 100%，非标准拼写恰好避开全部撞名。**本日志此条之前的条目保留"ECOgrab"原名，如实反映当时历史。**

> 记录从项目建立到当前的全部思路过程与工作过程，供后续迭代回溯。

---

## 项目概述

**目标**：一个本地视频下载+压缩 GUI 工具。解决两个核心痛点：
1. 很多网站把视频封装起来，yt-dlp 直接探测不到，传统做法要用户去 F12 Network 手动找真实地址——体验差；
2. 下载的视频太大，需要无感衔接压缩。

**核心设计原则（用户明确偏好）**：
- 不让普通用户去 F12 手动找地址——程序打开时就能自动嗅探；
- 不装浏览器扩展（"又像 fewtype 一样，本地一个、Chrome 一个"，两套常驻，否决）；
- 不让用户重新登录（独立浏览器窗口没有登录态，太麻烦）；
- 本地程序做所有重活（探测/下载/压缩/管理），浏览器侧只保留最小入口。

**架构状态：未定型**（截至 2026-09-25）。下载/压缩 GUI 已可用；用户还在用 LosslessCut 做视频后期（剪切/合并/封面），不断发现新工具需求。整体架构（单一工具箱 GUI vs 保留独立小工具集）待用户想清楚后确定——新需求先记入本文件，不急于设计进现有 GUI。

**项目位置**：`D:\Documents\ECOgrab\`（用户切换的独立项目目录，含 yt-dlp/ffmpeg/ffprobe 工具集）

---

## 阶段一：需求澄清与方案确认

- **用户需求（原始）**：基于现有 download.bat 做 UI——粘贴 URL → 能探测则数字/鼠标选格式分辨率 → 探测不到则提示去网页刷新嗅探真实地址 → 点击下载后可选择是否排期压缩。
- **用户关键升级**：不要用户专业地 F12 找地址——"它刷新的时候，我们直接能够在程序打开的情况下嗅探到地址"。
- **方案调研结论**：
  - 系统代理抓包：HTTPS 必须装自签 CA 证书，有安全顾虑，否决；
  - CDP（Chrome DevTools Protocol）监听浏览器网络：独立调试窗口 + websocket-client，无需证书/代理，选定；
  - 用户确认完整流程（4 步）：
    1. 粘贴 URL → 探测成功列格式点选下载 / 失败提示打开嗅探窗口；
    2. 嗅探窗口正常播放 → 程序实时列捕获的视频流；
    3. 点选地址下载，下载开始可见体积，可直接勾选压缩、选压缩方式；
    4. 未勾选压缩 → 下载完弹窗询问；已勾选 → 下载完直接走压缩。

## 阶段二：首次实现（v1）

**环境确认**：Python310（pip 26.2.1）、tkinter OK、websocket-client 1.9.0 已装、ECOgrab 目录工具齐全。

**实现 `ecograb.py`（tkinter 桌面 GUI，单文件）**：
- 探测模块：`yt-dlp -J` → 解析 formats（分辨率/编码/大小），纯视频流自动 `id+bestaudio` 合并（避免无声视频）；
- 嗅探模块（CDP）：自动启动独立 Chrome（`--remote-debugging-port=9222` + 独立 profile）→ 新建标签页 → `Network.enable` 监听 → 按扩展名 + content-type 过滤视频流（m3u8/mp4/webm/mpd 等，排除 .ts 分片防刷屏）；
- 下载模块：yt-dlp `-f` 指定格式 + `--merge-output-format mp4`，实时解析进度与体积；
- 压缩模块：内嵌 ffmpeg（4 模式：x265 CRF24 默认 / CRF20 高画质 / CRF27 小体积 / NVENC 硬件加速），`-progress pipe:1` 解析百分比，音频 copy，输出 `_压缩.mp4`；
- 下载完自动衔接压缩（勾选）或弹窗询问（未勾选）。

**测试（全部通过）**：
- 逻辑单测：格式解析、音轨自动合并、视频流识别过滤；
- CDP 嗅探链路集成测试：本地起假 m3u8 视频站 → 嗅探窗口成功捕获地址；
- GUI 冒烟：窗口构建、元素齐全。

**交付**：`ecograb.py` + `ecograb.bat`（pythonw 无控制台启动）。

## 阶段三：嗅探体验迭代——"不要重新登录"

**用户反馈**：嗅探会打开新的 Chrome，用户已登录的网站（视频站/YouTube）在新窗口要重新登录，太麻烦；希望在自己浏览器里 F5 就捕获。

**排查过程**：
- 方案 A（重启用户浏览器带调试端口）：Chrome 153 起禁止"默认登录资料开调试端口"（安全收紧），**不可行**；
- 方案 B（浏览器扩展）：已实现（manifest v3 + background.js），但**用户否决**——"不能再装扩展了，又像 fewtype 一样，本地一个、Chrome 一个"；
- 方案 C（书签小工具 bookmarklet）：**选定**。零扩展、零权限、无常驻，收藏栏一个按钮，视频页点一下 → 播放/F5 → 捕获。

**书签方案实现**：
- 本地接收服务：`127.0.0.1:8899/capture`（POST 视频地址，带 CORS 头）；
- `安装书签.html`：拖拽按钮到收藏栏即可（不用复制代码）；
- 书签原理：注入脚本轮询 `performance.getEntriesByType('resource')` 60 秒，匹配视频扩展名 → POST 到本地程序；
- 下载增加「用浏览器Cookie下载(Chrome)」选项（`--cookies-from-browser chrome`）：嗅探窗口没登录也能用登录态下载；
- 保留「开始嗅探」作兜底。

**测试（通过）**：模拟书签 POST → 捕获通道 PASS；GUI 冒烟 OK。

## 阶段四：真实场景实测与问题修复

**用户实测 Reddit 页面未捕获** → 用 CDP 实测该页面：
- 结论：**未登录的独立 profile 打开 Reddit 直接被反爬拦截**（"You've been blocked by network security"），只加载空壳页面，无视频请求——CDP 测试环境无法复现用户已登录页面；
- 判断书签未捕获的候选原因：视频未播放（Reddit 懒加载）/ YouTube 嵌入（googlevideo 地址无扩展名）/ 书签未装好。

**书签升级**：
- 支持 googlevideo/videoplayback（YouTube 无扩展名视频流）；
- 监听结束 alert 汇报"捕获到 N 个视频地址"（0 则提示确认已播放/刷新）。

**窗口问题修复**：
- 用户报告"点探测格式跳出新窗口"——实为 pythonw 无窗口运行时，yt-dlp/ffmpeg 子进程被 Windows 开了黑色命令行窗口；
- 修复：所有子进程调用（探测/下载/压缩/ffprobe）加 `CREATE_NO_WINDOW`，全程无感。

**用户截图确认**：嗅探窗口显示 Reddit blocked 页——确认嗅探窗口方案对反爬站点天然受限，书签（用户已登录浏览器）是正确主路径。

---

## 当前状态与待办

**文件清单**：
- `ecograb.py` — 主程序（GUI + 探测 + CDP 嗅探 + 下载 + 压缩 + 8899 接收服务）
- `ecograb.bat` — 启动器（pythonw 无控制台）
- `安装书签.html` — 书签安装页（拖到收藏栏）
- `download.py` / `compress.py` / `yt-dlp.exe` / `ffmpeg.exe` / `ffprobe.exe` — 原有工具集

**用户待实测**（工具本身已就绪）：
1. ECOgrab 直接「探测格式」这个 Reddit URL（yt-dlp 有 Reddit 解析器，可能直接成功，无需嗅探）；
2. 若探测失败：日常 Chrome（已登录）→ 先播放视频 → 点「ECOgrab 捕获」书签 → 回程序捕获列表下载。

**已知边界（如实记录）**：
- 书签只认带 `.m3u8/.mp4/.webm/.mpd` 等特征或 googlevideo 的视频地址；
- 不带任何特征的动态流地址（极少）书签抓不到，需 CDP 兜底；
- CDP 独立窗口对反爬站点（如 Reddit）会被拦截，仅作最后兜底；
- 压缩依赖本地 ffmpeg，浏览器侧不可能替代（这是保持"纯本地"架构的根本原因）。

## 阶段五：视频后期工具需求（架构未定，仅记录）

**背景**：用户在用 LosslessCut 做剪切/合并/封面等后期操作，遇到问题来求解。**架构状态：未定型**——整体方案未定，以下已验证方案先记录，暂不设计进现有 GUI。

**已验证方案（可复用）**：

1. **无损合并（混合帧率源）**
   - 场景：多个同源/同模式压缩文件合并，真实帧率可能是 59.94 与 60fps 混杂，容器时基不同（1/11988 vs 1/15360）。LosslessCut 报"轨道不匹配"；强制 `-r 60 -fps_mode cfr` 重编码则音画漂移（59.94→60 加速 0.1%，长视频累积 0.4s+）。
   - 根因：① 强制统一帧率重编码破坏 59.94 段时间轴；② mp4 直 concat copy 对不同时基换算错乱（实测视频流被拉长 57.8s、音画差 57.8s）。
   - 正确流程（无损）：
     a. 每段清洗去封面流、转 mkv：`ffmpeg -i in.mp4 -map 0:v:0 -map 0:a:0 -c copy out.mkv`
     b. list.txt 必须无 BOM（PowerShell `Set-Content -Encoding UTF8` 会带 BOM → ffmpeg 报 unknown keyword；用 `[IO.File]::WriteAllText` + UTF8Encoding($false) 或 ASCII）
     c. `ffmpeg -f concat -safe 0 -i list.txt -c copy merged.mp4`
   - 实测：858.8s 合并成功，视频 858.77s / 音频 858.83s 同步，帧数 51462 全保留。

2. **MP4 设置封面（JPEG）**
   - 场景：LosslessCut 导出帧（JPEG 49KB）映射封面失败（合并产物上）。
   - 根因：MP4 封面规范只认 JPEG（mjpeg 附加流），须标记 `attached_pic`；LosslessCut 写封面时重封装整个 mp4，对混合时基/特殊帧率标记文件处理失败。
   - 正确命令（无损）：`ffmpeg -i 视频.mp4 -i 封面.jpg -map 0 -map 1 -c copy -c:v:1 mjpeg -disposition:v:1 attached_pic 输出.mp4`
   - 实测：attached_pic=1 写入成功，音视频流不变（127.8MB）。

**待定**：是否将"无损合并 + 设置封面"整合进 ECOgrab（图形界面点选），等用户确认整体架构后决定。

## 阶段六：下载页交互与 UI 定稿（架构未定，待实现）

**核心体验目标（用户反复强调）**：用户不需要分辨"探测 vs 嗅探"、不需要 F12、不需要书签、不需要点多余按钮。

**探测失败自动嗅探流程（定稿）**：
1. 粘贴 URL → 点「探测」；
2. 成功 → 格式列表；
3. 失败 → 弹提示窗"视频文件藏得比较深，需要你在我们的窗口再点击一次播放"；
4. 用户确认 → 提示窗变为播放窗口（程序自己的 Chrome，CDP 监听，自动打开该 URL）；
5. 需要登录的网站用户自行登录（profile 持久化，以后免登录）；很多网站无需登录；
6. 用户点击播放 → 嗅探到视频文件 → 弹 tooltip"回到主窗口下载"；
7. 一分钟无动作 → 提示再点一次探测。

**下载池（定稿）**：
- 上半区格式列表：显示分辨率/格式，保留 AV1/VP9 ⚠ 兼容性提示；**鼠标悬停行 → 该行出现「下载」按钮**；
- 下载池每任务一行：文件名/进度/**压缩下拉菜单**（不压缩/x265默认/x265高画质/x265小体积/NVENC）；
- 多任务**并行下载**吃满带宽；可**暂停/恢复**（yt-dlp -c 断点续传）以实现优先级调整；
- 下载完成行**变灰**（下载环节结束），压缩交给压缩页面：选过模式的自动入压缩队列（空闲立即压/忙则排队）；选"不压缩"的可去压缩页手动转；
- 日志保留作 debug，做成**抽屉式**（可折叠，如 fewtype 风格），最终版去掉。

**待确认细节**：压缩页面 UI 逻辑（用户晚点定）；格式列表是否合并音视频自动处理（沿用 v1 逻辑）。

### 阶段六实现（v2，2026-09-26）

**已完成重构 `ecograb.py` → v2**（v1 备份为 `ecograb_v1.py`）：
- **自动嗅探**：探测失败 → 弹提示窗"视频文件藏得比较深，需要你在我们的窗口再点击一次播放" → 确认后启动程序自己的 Chrome（CDP）并自动打开该 URL → 60 秒无捕获自动提示"再点一次探测"；
- **捕获通知**：嗅探到视频 → 格式列表区顶部追加"嗅探·类型"行 + 3.5 秒 tooltip"已捕获视频文件，请回到主窗口点「下载」"；
- **格式列表**：探测结果按 分辨率/格式ID/编码/大小/来源 列展示，保留 ⚠（AV1/VP9）提示；**鼠标悬停行 → 行尾浮出「⬇下载」按钮**（tkinter 浮动控件实现）；
- **下载池**：每任务一行（名称/进度条/百分比/状态/暂停/继续/压缩下拉）；**并行 3 个吃满带宽**（Semaphore 调度）；暂停=终止 yt-dlp 进程（-c 保留 .part），恢复=重跑续传；每任务独立临时目录 `.ecograb_<id>` 隔离并发文件，完成后移出；**完成变灰**（控件全 disabled）；
- **压缩队列**：下载完成且选了压缩模式 → 自动入队（单 worker 串行：空闲立即压/忙排队），行内状态显示"压缩中 x%"；
- **日志抽屉**：可折叠（▸/▾），保留作 debug；
- 保留：`用浏览器Cookie下载` 全局勾选、8899 书签通道（备用）。

**测试（全部通过）**：
- 语法 py_compile OK；核心逻辑单测 5 组 PASS（格式解析/嗅探过滤/下载池并发-暂停让位-恢复/压缩队列串行排队/命令构造）；
- GUI 冒烟 6 组 PASS（UI 构建/失败提示窗/捕获入列表+tooltip/行组件状态流转/压缩下拉/日志抽屉）；
- 待用户实测：真实 URL 探测、真实嗅探、多任务并行下载、暂停/恢复续传、下载完自动压缩衔接。

**测试脚本**：`test_v2.py`（核心逻辑）、`smoke_v2.py`（GUI 冒烟），保留可回归。

## 关键经验备忘

- **pythonw + subprocess**：子进程必须 `CREATE_NO_WINDOW`，否则弹命令行窗口；
- **Chrome 新版**：默认 profile 不能开调试端口，独立 profile 又无登录态且易被反爬拦截——所以"嗅探用户已登录页面"只能靠页面内脚本（书签）转交地址；
- **bookmarklet 局限**：`performance` 只记录已加载资源、只按 URL 判断（拿不到响应头），YouTube 等无扩展名流需特征匹配；
- **架构结论**：压缩/探测/下载全是本地强项、浏览器侧做不到，故"本地程序 + 浏览器最小入口"是唯一平衡点；纯扩展不可行（沙箱无法调本地 exe）。

### 阶段七：YouTube 探测修复（2026-09-26）
**现象**：真实 YouTube 视频探测失败；嗅探窗口登录后播放 60 秒无捕获；download.bat 同样失败。
**诊断（逐层排除）**：
1. yt-dlp 报 "No supported JavaScript runtime" → YouTube 新版提取需要 JS runtime（deno）
2. 显式 --js-runtimes deno 后警告消失，但仍 "This video is unavailable"
3. 换 tv/android_vr 客户端 → 依旧 "Sign in to confirm you're not a bot" → **节点 IP 被 YouTube 风控**（数据中心/共享 IP 未登录必拦）
4. `--cookies-from-browser chrome` 报 "Could not copy Chrome cookie database"（issue 7271：Chrome 运行中锁库 + 新版加密）
**修复**：
1. 下载 deno 2.9.7 → `D:\Documents\ECOgrab\deno.exe`（.gitignore 排除，不推送）
2. 全局配置 `%APPDATA%\yt-dlp\config`：`--js-runtimes deno:D:/Documents/ECOgrab/deno.exe`（**注意：config 文件里反斜杠会被 yt-dlp 吃掉，必须用正斜杠**）→ 所有 yt-dlp 调用自动生效
3. **cookie 通道**：ECOgrab 嗅探浏览器 `.chrome_profile` 登录 YouTube 后，`--cookies-from-browser chrome:<profile路径>` 可绕过 bot 风控（实测 ytsearch1 完整列出 144p-1080p）
4. 代码集成：`ecograb.py` 新增 `cookie_args()`（探测/下载自动带）；`download.py` 同步
5. `is_video_request` 补 googlevideo/videoplayback 特征兜底（YouTube 无扩展名流）
**验证**：download.py 探测 ytsearch1:hello 成功（全格式列表）；test_v2.py 5 组全 PASS（断言同步更新）
**遗留**：IP 被风控时未登录仍可能拦截 → 换干净节点（日/新/住宅 IP）；嗅探窗口登录态需保持；用户主 Chrome 的 cookie 通道（7271）未解，靠 .chrome_profile 绕开


## 阶段八：下载/性能/稳定性调试（2026-09-26 下午）

### 坑 1：删除任务后 yt-dlp 还在下载（孤儿进程）
**现象**：删除下载中任务后流速仍有 1~2MB/s；"暂停后过一会儿失败"。
**根因**：yt-dlp.exe 是 pyinstaller onefile 双进程架构（bootloader 父进程 + 真正下载的子进程）。`terminate()` 只杀父进程，子进程变孤儿继续下载；`.part` 被占用导致 rmtree 也失败。
**修复**：`_kill_proc_tree()` 用 `taskkill /PID /T /F` 杀整个进程树（删除/暂停都走它），杀完 wait 回收再删 tmpdir。实测系统里曾有 2 个孤儿 yt-dlp 进程，已清。
**洞见**：Windows 上 terminate() 对打包型 exe 不可靠，杀进程一律杀树。

### 坑 2：GUI 卡顿（下载后随机卡、删了还占 CPU）——两次误判
**误判 A**：怀疑下载完自动压缩（ffmpeg）吃 CPU——**用户实测没选压缩、CPU<40%，排除**。
**误判 B**：怀疑 cookie CDP 同步阻塞——只解决"点下载那一瞬间"，下载后仍卡。
**真凶（多因素叠加）**：嗅探 Chrome（普通优先级）一直开着 + yt-dlp 子进程普通优先级抢 UI + 孤儿进程残留。
**为什么脚本不卡**：download.py 无 GUI 主线程（没有"界面卡"概念）、无嗅探 Chrome；yt-dlp 下载本身是网络/IO 型，CPU 很低。GUI 卡 = 界面线程被普通优先级子进程抢占 + 多余 Chrome 进程。
**修复**：所有子进程统一 `BELOW_NORMAL_PRIORITY_CLASS`（0x4000）：嗅探 Chrome、yt-dlp 下载、探测/-J、预探测、HLS 解析、HEAD、压缩 ffmpeg。
**洞见**：GUI 应用里凡是有可能长期运行的子进程（下载/解码/压缩）一律降优先级；用户侧再配合下载目录加 Defender 排除 + 用完关嗅探窗。

### 坑 3：下载进度从 50% 起跳
**根因**：临时目录 `.ecograb_{task_id}` 的 task_id 是进程内递增序号，**重启后从 1 重置** → 新任务复用旧残留目录（上次中断的 .part 500MB）→ yt-dlp `-c` 续传 → 从 50% 开始。
**修复**：tmpdir 加进程号 `.ecograb_{task_id}_{pid}`；启动时清理超过 10 分钟的 `.ecograb_*` 残留（排除当前 pid）。

### 坑 4：清晰度遍历"碰运气"（mat6tube）
**现象**：有时 4 档全抓到，有时丢档。
**根因**：`setCurrentQuality` 首档（240，低→高顺序第一个）调用时机太早（播放器刚就绪）抛异常，而 `idx+1` 在 try 之前 → 档位被永久跳过（3/4 次丢 240）。
**修复**：失败重试同档最多 2 次，成功才推进。实测 13:28 稳定 240→360→480→720 全抓。
**洞见**：JW Player `getQualityLevels()` 返回顺序 = 高→低；`setCurrentQuality(0)`（最高档）因 JW 视其为"当前档位"不触发重新拉流（UI 标记 720 active，实际 CDP 拉 480 = 带宽自适应）——**切换必须低→高**，最高档最后请求必拉流。

### 坑 5：cookie CDP 同步阻塞主线程
**现象**：点下载卡一阵、最小化恢复黑屏（主线程无法重绘）。
**根因**：`cookie_args()` 的 CDP `Network.getAllCookies` 同步等 Chrome 响应 1~2s，在 DownloadTask.start（主线程）调用。
**修复**：`_COOKIE_CACHE` 缓存 120s——探测/预探测的后台线程已取过，下载启动直接命中。
**洞见**：一切可能阻塞主线程的外部调用（CDP/网络）要么后台化，要么缓存。

### 坑 6：tooltip 位置
主窗口 tooltip 对"回主窗口下载"的提醒无意义 → 改为 CDP `Runtime.evaluate` 注入播放页 DOM（右上角浮动条，6 秒自动消失），注入失败才兜底主窗口。

### 坑 7：测试断言鲁棒性
嗅探 Chrome 正开着锁 `.chrome_profile` 时，测试环境的 cookie 副本复制会失败 → cookie 断言改为容忍 CDP 或副本任一路径。

### 其它
- 提示窗口（"视频文件藏得比较深"）定位到主窗口正中间。
- GitHub 推送与网络：AI reality 节点挂 → git push `schannel: failed to receive handshake, SSL/TLS connection failed`（exit 128）；本地 commit 全部安全，节点恢复后补推。


### 验证（用户实测 2026-09-26 下午）
- 统一 BELOW_NORMAL + taskkill 杀进程树后：**界面不再卡顿**（"对，没那么卡了"）——坑 1/坑 2 闭环。
- 累积 commit（进程树修复、全子进程降优先级、processing.md 阶段八）在节点恢复后已推送 GitHub（eb1c693..8709e89）。
- 遗留观察：节点不稳定期 git push 会 SSL/RPC 中断，本地 commit 安全，恢复后补推即可。
