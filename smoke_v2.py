# -*- coding: utf-8 -*-
"""eazyVid v2 GUI 冒烟测试：构建窗口、弹窗、捕获提示、下载池行"""
import sys, os
sys.path.insert(0, r'D:\Documents\eazyVid')
import tkinter as tk
import eazyvid as E

root = tk.Tk()
app = E.App(root)
root.update()

# 关键控件存在
for name in ("url_var", "fmt_tree", "pool", "cqueue", "_dl_btn", "_log_btn"):
    assert getattr(app, name) is not None, name
assert app._pool_inner is not None and app._pool_canvas is not None
print("1. UI 构建 OK", flush=True)

# 探测失败 → 弹提示窗
app._probe_failed(("http://x/video", "测试错误"))
root.update()
tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
assert tops, "未弹出提示窗"
assert "藏得比较深" in "".join(w.cget("text") or "" for w in tops[0].winfo_children() if isinstance(w, tk.Label))
print("2. 探测失败提示窗 OK", flush=True)

# 嗅探捕获 → 格式列表追加 + tooltip
app._on_capture_url("http://cdn/video.m3u8?token=1")
root.update()
assert len(app.captured) == 1, app.captured
items = app.fmt_tree.get_children()
assert items, "捕获行未加入格式列表"
assert "嗅探" in app.fmt_tree.item(items[0], "values")[4], app.fmt_tree.item(items[0], "values")
print("3. 嗅探捕获入列表 + tooltip OK", flush=True)

# 下载池行组件（不触发真实下载）
t = E.DownloadTask("http://x/v", "18", r'D:\Documents\eazyVid', None, "测试任务", app.pool, 99)
row = E.TaskRow(app._pool_inner, t, app)
t.ui = row
row.refresh()
root.update()
assert t.ui is not None
assert "排队" in row.state_lbl.cget("text")
# 模拟下载中进度
t.state = "downloading"; t.progress = 42.5; t.size_str = "10.0MiB"; t.total_known = True
row.refresh(); root.update()
assert "42%" in row.pct.cget("text")
assert str(row.pause_btn.cget("state")) == "normal"
# 模拟暂停
t.state = "paused"; row.refresh(); root.update()
assert str(row.resume_btn.cget("state")) == "normal"
# 模拟完成变灰
t.state = "done"; t.progress = 100; row.refresh(); root.update()
assert str(row.pause_btn.cget("state")) == "disabled"
assert str(row.mode_cb.cget("state")) == "disabled"
print("4. 下载池行 排队/进度/暂停/完成变灰 OK", flush=True)

# 压缩下拉菜单值
vals = row.mode_cb.cget("values")
assert vals[0] == "不压缩" and "x265 默认(推荐)" in vals and "NVENC 硬件加速" in vals, vals
print("5. 压缩下拉菜单 OK", flush=True)

# 日志抽屉
app._toggle_log(); root.update()
assert app.log_visible is True
assert app.log_text.winfo_manager() == "pack"
app._toggle_log(); root.update()
assert app.log_visible is False
print("6. 日志抽屉 OK", flush=True)

root.after(200, root.destroy)
root.mainloop()
print("=== GUI SMOKE PASSED ===", flush=True)
