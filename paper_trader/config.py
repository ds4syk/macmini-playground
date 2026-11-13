"""Static configuration and secret loading.

Secrets live outside the repository in ~/.config/paper-trader/.env.
"""
from __future__ import annotations

import os
import pathlib
from typing import Dict, List

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = pathlib.Path(os.environ.get("PAPER_DATA_DIR", ROOT / "data"))
WEB_DIR = ROOT / "web"
DIST_DIR = ROOT / "dist"
DB_PATH = DATA_DIR / "trader.db"
CONFIG_PATH = pathlib.Path.home() / ".config" / "paper-trader" / ".env"

# Realistic A-share universe: liquid large/mid caps across industries.
# 100-share lots mean the expensive names are only reachable by larger accounts.
UNIVERSE: List[Dict[str, str]] = [
    {"code": "sh600519", "symbol": "600519", "name": "贵州茅台", "sector": "白酒"},
    {"code": "sz000858", "symbol": "000858", "name": "五粮液", "sector": "白酒"},
    {"code": "sh600036", "symbol": "600036", "name": "招商银行", "sector": "银行"},
    {"code": "sh601318", "symbol": "601318", "name": "中国平安", "sector": "保险"},
    {"code": "sz300750", "symbol": "300750", "name": "宁德时代", "sector": "动力电池"},
    {"code": "sz002594", "symbol": "002594", "name": "比亚迪", "sector": "汽车"},
    {"code": "sz000333", "symbol": "000333", "name": "美的集团", "sector": "家电"},
    {"code": "sh600276", "symbol": "600276", "name": "恒瑞医药", "sector": "创新药"},
    {"code": "sz300760", "symbol": "300760", "name": "迈瑞医疗", "sector": "医疗器械"},
    {"code": "sz002415", "symbol": "002415", "name": "海康威视", "sector": "安防"},
    {"code": "sh688981", "symbol": "688981", "name": "中芯国际", "sector": "半导体"},
    {"code": "sz002371", "symbol": "002371", "name": "北方华创", "sector": "半导体设备"},
    {"code": "sz300059", "symbol": "300059", "name": "东方财富", "sector": "券商"},
    {"code": "sh600030", "symbol": "600030", "name": "中信证券", "sector": "券商"},
    {"code": "sh601899", "symbol": "601899", "name": "紫金矿业", "sector": "有色金属"},
    {"code": "sh600900", "symbol": "600900", "name": "长江电力", "sector": "电力"},
    {"code": "sh601088", "symbol": "601088", "name": "中国神华", "sector": "煤炭"},
    {"code": "sh600309", "symbol": "600309", "name": "万华化学", "sector": "化工"},
    {"code": "sz002475", "symbol": "002475", "name": "立讯精密", "sector": "消费电子"},
    {"code": "sz000725", "symbol": "000725", "name": "京东方A", "sector": "面板"},
    {"code": "sh600031", "symbol": "600031", "name": "三一重工", "sector": "工程机械"},
    {"code": "sh600887", "symbol": "600887", "name": "伊利股份", "sector": "乳制品"},
    {"code": "sh601888", "symbol": "601888", "name": "中国中免", "sector": "免税"},
    {"code": "sh603259", "symbol": "603259", "name": "药明康德", "sector": "CXO"},
    {"code": "sz300274", "symbol": "300274", "name": "阳光电源", "sector": "光伏"},
    {"code": "sh601012", "symbol": "601012", "name": "隆基绿能", "sector": "光伏"},
    {"code": "sz000651", "symbol": "000651", "name": "格力电器", "sector": "家电"},
    {"code": "sh601668", "symbol": "601668", "name": "中国建筑", "sector": "建筑"},
    {"code": "sh601857", "symbol": "601857", "name": "中国石油", "sector": "石油"},
    {"code": "sh600941", "symbol": "600941", "name": "中国移动", "sector": "通信运营"},
]

INDICES: List[Dict[str, str]] = [
    {"code": "sh000001", "name": "上证指数"},
    {"code": "sz399001", "name": "深证成指"},
    {"code": "sz399006", "name": "创业板指"},
    {"code": "sh000300", "name": "沪深300"},
]

# Every model manages the same four capital buckets; the capital effect is
# directly comparable because target weights are capital independent.
MODELS: List[Dict[str, str]] = [
    {
        "id": "deepseek-flash",
        "name": "DeepSeek V4.1 Flash",
        "short": "DS Flash",
        "provider": "deepseek",
        "model": "deepseek-flash",
        "color": "#d92b2b",
        "note": "速度优先，采样温度 0.4",
    },
    {
        "id": "deepseek-v4-pro",
        "name": "DeepSeek V4 Pro",
        "short": "DS V4 Pro",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "color": "#7b2ff2",
        "max_tokens": "3200",
        "note": "更强推理，采样温度 0.4",
    },
    {
        "id": "glm-5.3",
        "name": "GLM-5.3",
        "short": "GLM-5.3",
        "provider": "zai",
        "model": "glm-5.3",
        "color": "#1f6feb",
        "note": "智谱旗舰，采样温度 0.4",
    },
    {
        "id": "glm-5.3-flash",
        "name": "GLM-5.3 Flash",
        "short": "GLM Flash",
        "provider": "zai",
        "model": "glm-5.3-flash",
        "color": "#0a9b5a",
        "note": "低延迟版本，采样温度 0.4",
    },
    {
        "id": "glm-5.2",
        "name": "GLM-5.2",
        "short": "GLM-5.2",
        "provider": "zai",
        "model": "glm-5.2",
        "color": "#f08c00",
        "note": "上一代旗舰，采样温度 0.4",
    },
]

CAPITALS: List[int] = [10000, 30000, 50000, 100000]
CAPITAL_LABELS = {10000: "1万", 30000: "3万", 50000: "5万", 100000: "10万"}

# Replay window. Decisions use data up to the close of each day and execute at
# the next session's open, so no look-ahead is possible.
START_DATE = os.environ.get("PAPER_START_DATE", "2026-09-01")

# A-share trading costs (2026 retail defaults).
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_DUTY_RATE = 0.0005
TRANSFER_FEE_RATE = 0.00001
LOT_SIZE = 100
MAX_POSITIONS = 8
MAX_WEIGHT = 0.25
PRICE_LIMIT = 0.098

# HTTP models.
TIMEOUT_SECONDS = 90
MAX_RETRIES = 1


def load_env() -> None:
    """Load the private env file without overriding real environment values."""
    if not CONFIG_PATH.exists():
        return
    for raw in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def api_settings() -> Dict[str, Dict[str, str]]:
    load_env()
    return {
        "deepseek": {
            "base": os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com"),
            "key": os.environ.get("DEEPSEEK_API_KEY", ""),
        },
        "zai": {
            "base": os.environ.get(
                "ZAI_BASE", "https://open.bigmodel.cn/api/coding/paas/v4"
            ),
            "key": os.environ.get("ZAI_API_KEY", "")
            or os.environ.get("ZAI_CODING_CN_API_KEY", ""),
        },
        "sensenova": {
            "base": os.environ.get("SENSENOVA_BASE", "https://token.sensenova.cn/v1"),
            "key": os.environ.get("SENSENOVA_API_KEY", ""),
        },
    }


def model_by_id(model_id: str) -> Dict[str, str]:
    for model in MODELS:
        if model["id"] == model_id:
            return model
    raise KeyError(model_id)


def universe_by_symbol() -> Dict[str, Dict[str, str]]:
    return {item["symbol"]: item for item in UNIVERSE}


def portfolio_id(model_id: str, capital: int) -> str:
    return "%s__%d" % (model_id, capital)
