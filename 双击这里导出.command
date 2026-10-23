#!/bin/bash
# 双击本文件即可运行 iMessage 导出器（交互式引导，跟着提示操作）。
# 它只会在本机读取你的聊天记录并导出到桌面，不联网、不修改原始数据。

cd "$(dirname "$0")" || exit 1
clear
python3 imessage_export.py interactive
