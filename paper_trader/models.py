"""LLM provider clients and strict decision parsing."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from . import config, net  # noqa: F401


class ModelError(RuntimeError):
    pass


# The 智谱 coding endpoint tolerates only a small number of concurrent
# generations; serializing it avoids multi-minute queue stalls.
_ZAISEM = threading.Semaphore(1)


def _post_json(url: str, key: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_model(model: Dict[str, str], system: str, user: str) -> Dict:
    settings = config.api_settings()
    provider = model["provider"]
    conf = settings.get(provider) or {}
    base = conf.get("base", "").rstrip("/")
    key = conf.get("key", "")
    if not base or not key:
        raise ModelError("missing credentials for %s" % provider)

    payload = {
        "model": model["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(model.get("temperature", 0.4)),
        "max_tokens": int(model.get("max_tokens", 1600)),
        "stream": False,
    }
    if provider == "deepseek":
        payload["reasoning_effort"] = "low"
        payload["thinking"] = {"type": "disabled"}
    if provider == "zai":
        payload["thinking"] = {"type": "disabled"}

    last_error: Optional[Exception] = None
    started = time.time()
    for attempt in range(config.MAX_RETRIES + 1):
        try:
            if provider == "zai":
                with _ZAISEM:
                    data = _post_json(base + "/chat/completions", key, payload, config.TIMEOUT_SECONDS)
            else:
                data = _post_json(base + "/chat/completions", key, payload, config.TIMEOUT_SECONDS)
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
            usage = data.get("usage") or {}
            return {
                "content": content,
                "latency_ms": int((time.time() - started) * 1000),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "finish_reason": choice.get("finish_reason"),
            }
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            last_error = ModelError("HTTP %s: %s" % (exc.code, detail))
            if exc.code in (400, 401, 403):
                break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(1.2 * (attempt + 1))
    raise ModelError(str(last_error))


def extract_json(text: str) -> dict:
    if not text:
        raise ValueError("empty completion")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("no JSON object in completion")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(cleaned[start:])
    if not isinstance(obj, dict):
        raise ValueError("JSON root is not an object")
    return obj


def _symbol(raw: str) -> str:
    raw = str(raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    return digits[-6:].zfill(6) if digits else ""


def normalize_targets(raw_targets, universe_symbols: set) -> Tuple[List[dict], List[str]]:
    """Clamp model output to the platform's risk constraints."""
    problems: List[str] = []
    by_symbol: Dict[str, dict] = {}
    if not isinstance(raw_targets, list):
        return [], ["targets is not a list"]
    for item in raw_targets:
        if not isinstance(item, dict):
            continue
        symbol = _symbol(item.get("symbol") or item.get("code") or "")
        if symbol not in universe_symbols:
            problems.append("unknown symbol %s" % symbol)
            continue
        try:
            weight = float(item.get("weight") or 0)
        except (TypeError, ValueError):
            problems.append("bad weight for %s" % symbol)
            continue
        weight = max(0.0, min(weight, config.MAX_WEIGHT))
        if weight < 0.005:
            continue
        by_symbol[symbol] = {
            "symbol": symbol,
            "weight": round(weight, 4),
            "reason": str(item.get("reason") or "")[:180],
        }
    targets = sorted(by_symbol.values(), key=lambda x: x["weight"], reverse=True)
    if len(targets) > config.MAX_POSITIONS:
        problems.append("trimmed to %d positions" % config.MAX_POSITIONS)
        targets = targets[: config.MAX_POSITIONS]
    total = sum(t["weight"] for t in targets)
    if total > 1.0:
        problems.append("weights sum %.2f > 1, scaled" % total)
        for item in targets:
            item["weight"] = round(item["weight"] / total, 4)
    return targets, problems


def generate_decision(model: Dict[str, str], system: str, user: str) -> Dict:
    """Call the model with one corrective retry, returning raw + parsed data."""
    attempts = []
    for attempt in range(2):
        prompt = user
        if attempt == 1:
            prompt = user + "\n\n上一次输出不是合法 JSON。请只输出一个 JSON 对象，不要 Markdown 代码块，不要解释。"
        response = call_model(model, system, prompt)
        attempts.append(response)
        try:
            parsed = extract_json(response["content"])
            if "targets" not in parsed:
                raise ValueError("missing targets key")
            targets, problems = normalize_targets(
                parsed.get("targets") or [], {u["symbol"] for u in config.UNIVERSE}
            )
            return {
                "targets": targets,
                "market_view": str(parsed.get("market_view") or "")[:400],
                "confidence": float(parsed.get("confidence") or 0),
                "problems": problems,
                "response": response,
            }
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise ModelError("invalid decision JSON: %s" % last)
