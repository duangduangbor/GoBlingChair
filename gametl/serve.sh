#!/bin/bash
# gametl Ollama 服务启动脚本（确保环境变量正确注入）
export OLLAMA_MODELS="D:/ollama-models"
export OLLAMA_HOST="127.0.0.1:11434"
export OLLAMA_KEEP_ALIVE="30m"
exec "C:/Users/h/Tools/ollama/ollama.exe" serve
