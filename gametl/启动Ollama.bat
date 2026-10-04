@echo off
rem ========================================
rem  gametl 一键启动：启动 Ollama 服务
rem ========================================
chcp 65001 >nul
set "OLLAMA_MODELS=D:\ollama-models"
set "OLLAMA_HOST=127.0.0.1:11434"

echo [gametl] 启动 Ollama 服务...
echo   模型目录: %OLLAMA_MODELS%
echo   监听地址: %OLLAMA_HOST%
echo.
echo 保持此窗口开启，翻译任务需要它持续运行。
echo 关闭窗口即停止服务。
echo.

"C:\Users\h\Tools\ollama\ollama.exe" serve
