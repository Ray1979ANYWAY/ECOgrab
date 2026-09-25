# ECOgrab

Local video downloader & compressor — URL probe, deep sniffing, parallel download pool, auto compress queue.

## Run

- Requires **ffmpeg** ([official download](https://ffmpeg.org/download.html)) — not bundled, exceeds GitHub size limit. Put `ffmpeg.exe` & `ffprobe.exe` in this folder.
- Requires **deno** (JS runtime for YouTube extraction, [official download](https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip)) — also not bundled. Put `deno.exe` in this folder (or add it to PATH).
- yt-dlp.exe is bundled.
- Double-click `ecograb.bat` to start.

> YouTube notes: if your IP is flagged (datacenter / shared IP), yt-dlp may return "Sign in to confirm you're not a bot". Fix: log in to YouTube once inside the ECOgrab sniffer window — the login is remembered in `.chrome_profile/` and used automatically.

_README WIP._
