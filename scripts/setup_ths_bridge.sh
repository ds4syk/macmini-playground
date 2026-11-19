#!/usr/bin/env bash
# 为同花顺网页模拟盘桥接创建一个带 playwright 的独立虚拟环境（不影响平台本身的解释器）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${THS_VENV:-$ROOT/.venv-ths}"

python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet playwright

echo "已创建 $VENV"
echo "使用："
echo "  $VENV/bin/python -m paper_trader.ths_web status"
echo "  $VENV/bin/python -m unittest tests.test_ths_web -v"
