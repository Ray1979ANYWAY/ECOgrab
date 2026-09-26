@echo off
rem eazyVid 启动器：用 pythonw 运行，不显示黑色控制台
where pythonw >nul 2>&1
if %errorlevel%==0 (
  start "" pythonw "%~dp0eazyvid.py"
) else (
  start "" "D:\Program Files\Python\Python310\pythonw.exe" "%~dp0eazyvid.py"
)
