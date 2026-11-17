"""同花顺网页模拟盘（moni.10jqka.com.cn）的浏览器操作层。

工具不保存账号密码，也不启动自己的浏览器：人工启动 Chrome 并登录同花顺模拟盘，
本模块通过 Chrome DevTools Protocol（CDP，默认 127.0.0.1:9222）挂接该浏览器，
读取资金/持仓，并按市价委托下单。

页面结构与选择器参考了 MIT 许可的开源项目
https://github.com/trading-a/ai_ths_moni_trading_mcp（Copyright (c) 2026 jsiq@qq.com），
按本平台的 CLI 用法重写；选择器集中在 SELECTORS，页面改版时只改这里。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # playwright 只在真正操作浏览器时才需要，单元测试可以没有它
    from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright
except ImportError:  # pragma: no cover - 取决于运行环境
    Page = Any  # type: ignore[assignment,misc]
    PlaywrightTimeoutError = Exception  # type: ignore[assignment,misc]
    async_playwright = None  # type: ignore[assignment]

CDP_ENDPOINT = os.environ.get("THS_CDP", "http://127.0.0.1:9222")
PLATFORM_URL = "https://moni.10jqka.com.cn/"
PLATFORM_HOST = "moni.10jqka.com.cn"
LOGIN_HOST = "upass.10jqka.com.cn"
TRADE_PAGE_TITLE = "同花顺虚拟交易所"

SELECTORS = {
    # 页面识别
    "page_title": TRADE_PAGE_TITLE,
    # 平台页
    "platform_enter_zone": "a.xzjrjyq",
    "platform_zone_dialog": "#jyqnr",
    "platform_zone_link": "#jyqnr a.xzjyqjr[name='thsbs']",
    "logged_in": "#topUser",
    "logged_in_text": "你好",
    "header_logged_in": "#header_logined",
    "header_username": "#J_username",
    # 登录页二维码
    "login_qr_toggle": "#to_qrcode_login",
    "login_qr_panel": "#qrcode_pannel",
    "login_qr_image": "#qrcode_pannel .code-box img",
    "login_qr_refresh": "#qrcode_pannel .again-refresh",
    "login_qr_invalid": "#qrcode_pannel .invalid-box",
    "login_qr_success": "#qrcode_pannel .success-box",
    # 左侧菜单
    "menu_query": "#nav4",
    "menu_account": "#azijin",
    "menu_deals": "#acjtoday",
    "menu_orders": "#awttoday",
    "menu_cancel": ".nav3",
    "menu_sjwt": "#nav5",
    "menu_sjwt_buy": "#asjwtbuy",
    "menu_sjwt_sell": "#asjwtsell",
    # 资金与持仓
    "account_panel": "#zijindiv",
    "account_fields": {
        "可用余额": "#kyye",
        "资金余额": "#zjye",
        "冻结余额": "#djje",
        "可取金额": "#kqje",
        "总资产": "#zzc",
        "证券市值": "#gpsz",
    },
    "table_positions": "table[name='qryChicang']",
    "table_orders": "table[name='qryDelegated']",
    "table_deals": "table[name='qryChengjiao']",
    "table_cancel": "table[name='qryChedan']",
    # 市价委托
    "buy_panel": "#sjwtbuydiv",
    "sell_panel": "#sjwtselldiv",
    "input_code": "input[name='stockcode']",
    "input_qty": "input[name='amount']",
    "type_select": "select[name='sjwt_type']",
    "type_text": "剩余撤销",
    "submit_buy": "#sjwtbuySubmit",
    "submit_sell": "#sjwtsellSubmit",
    "cmd_buy": "cmd_wt_mairu",
    "cmd_sell": "cmd_wt_maichu",
    "autocomplete": "{panel} #autocomplete_stockcode",
    "gddm": "select[name='gddm']",
    "suggest_url": "https://news.10jqka.com.cn/public/index_keyboard_{code}_stock,venuefund_5_jsonp.html",
    # 确认/结果弹窗
    "prompt": "#promptWinContainer",
    "prompt_ok": "#ymPrompt_btn_0",
    "prompt_cancel": "#ymPrompt_btn_1",
    # 撤单
    "cancel_checkbox": "table[name='qryChedan'] input[name='{order_id}']",
    "cancel_all": "#allCheDanBtn",
}

# 下单/查询各步骤的等待时间（毫秒）
TIMINGS = {
    "menu": 400,
    "panel": 900,
    "suggest_wait": 2200,
    "suggest_timeout": 5000,
    "quote_timeout": 9000,
    "dialog_timeout": 9000,
    "reload_timeout": 12000,
    "js_settle": 3000,
    "zone_popup": 9000,
    "zone_new_tab": 20000,
}


class ThsError(RuntimeError):
    """同花顺页面操作失败（可安全展示给用户的中文原因）。"""


class BrowserUnavailable(ThsError):
    """连不上 Chrome 调试端口。"""


class NotLoggedIn(ThsError):
    """浏览器已连上，但同花顺模拟盘没有登录。"""


class TradingPageMissing(ThsError):
    """找不到交易页（未进入交易区）。"""


_playwright = None
_browser = None
_lock = None


async def connect():
    """连接 Chrome 调试端口；复用同一个连接。"""
    global _playwright, _browser
    if async_playwright is None:
        raise BrowserUnavailable(
            "未安装 playwright。请先运行 scripts/setup_ths_bridge.sh，"
            "再用该虚拟环境的 python 执行本工具。"
        )
    if _browser is not None and _browser.is_connected():
        return _browser
    import asyncio

    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        await disconnect()
        _playwright = await async_playwright().start()
        try:
            _browser = await _playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
        except Exception as exc:
            await disconnect()
            raise BrowserUnavailable(
                "未检测到浏览器调试端口（%s）。请先运行 scripts/start_ths_chrome.sh "
                "打开 Chrome，并在其中登录同花顺模拟盘。" % CDP_ENDPOINT
            ) from exc
        return _browser


async def disconnect() -> None:
    """断开 CDP 连接（不关闭用户的浏览器）。"""
    global _playwright, _browser
    _browser = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
    _playwright = None


def _pages(browser) -> List[Page]:
    pages: List[Page] = []
    for context in browser.contexts:
        pages.extend(context.pages)
    return pages


async def _title(page: Page) -> str:
    try:
        return await page.title()
    except Exception:
        return ""


async def find_trading_page() -> Optional[Page]:
    browser = await connect()
    for page in _pages(browser):
        if await _title(page) == TRADE_PAGE_TITLE:
            return page
    return None


async def find_platform_page() -> Optional[Page]:
    browser = await connect()
    for page in _pages(browser):
        try:
            if PLATFORM_HOST in page.url:
                return page
        except Exception:
            continue
    return None


async def is_logged_in(page: Page) -> bool:
    """平台首页用 #header_logined/#J_username，交易页用 #topUser（两套页面结构）。"""
    header = page.locator(SELECTORS["header_logged_in"])
    if await header.count():
        try:
            username = (await page.locator(SELECTORS["header_username"]).first.inner_text()).strip()
        except Exception:
            username = ""
        return bool(username) or await header.first.is_visible()
    indicator = page.locator(SELECTORS["logged_in"])
    if await indicator.count() == 0:
        return False
    try:
        return SELECTORS["logged_in_text"] in (await indicator.first.inner_text())
    except Exception:
        return False


async def ensure_trading_page() -> Page:
    """返回已登录的交易页；缺失或会话失效时自动从平台页重进交易区。"""
    page = await find_trading_page()
    if page is not None:
        try:
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_function(
                "() => { const el = document.querySelector('%s');"
                "return el && /\\d/.test(el.innerText || ''); }" % SELECTORS["logged_in"],
                timeout=TIMINGS["reload_timeout"],
            )
            await page.wait_for_timeout(TIMINGS["js_settle"])
            return page
        except Exception:
            try:
                await page.close()
            except Exception:
                pass
    platform = await open_platform_page()
    if not await is_logged_in(platform):
        raise NotLoggedIn(
            "同花顺模拟盘未登录。请在 Chrome 里登录（可用同花顺 App 扫码），"
            "然后重试；也可以用 `python -m paper_trader.ths_web login` 生成的二维码登录。"
        )
    await enter_trading_zone(platform)
    page = await find_trading_page()
    if page is None:
        raise TradingPageMissing("已点击进入交易区，但没有出现交易页，请人工检查浏览器。")
    await page.wait_for_timeout(TIMINGS["js_settle"])
    return page


async def _trade_url_from_profile(platform: Page) -> Optional[str]:
    """从「我的模拟炒股」主页取交易区链接（账号相关，带 token）。"""
    profile_href = None
    link = platform.locator("a.user_mncg")
    if await link.count():
        profile_href = await link.first.get_attribute("href")
    if not profile_href:
        return None
    probe = await platform.context.new_page()
    try:
        await probe.goto(profile_href, wait_until="domcontentloaded")
        await probe.wait_for_timeout(1500)
        trade = probe.locator("a.xzjyqjr")
        if await trade.count() == 0:
            return None
        return await trade.first.get_attribute("href")
    finally:
        try:
            await probe.close()
        except Exception:
            pass


async def enter_trading_zone(platform: Page) -> None:
    """进入交易区：优先用「我的模拟炒股」主页里的交易区链接，直接新开标签页。

    首页的「进入交易区」按钮在没有模拟账户时是隐藏的，而且点击依赖页面脚本；
    个人主页里的链接是带 usrid/token 的直链，直接打开更稳定。
    """
    platform = platform or await find_platform_page()
    if platform is None:
        raise TradingPageMissing("找不到同花顺模拟炒股平台页。")
    await platform.bring_to_front()
    trade_url = await _trade_url_from_profile(platform)
    if not trade_url:
        # 兜底：首页「进入交易区」→ 选择交易区弹层 → 同花顺交易区
        try:
            await platform.locator(SELECTORS["platform_enter_zone"]).first.click()
            await platform.wait_for_selector(
                SELECTORS["platform_zone_dialog"], state="visible", timeout=TIMINGS["zone_popup"]
            )
            zone_link = platform.locator(SELECTORS["platform_zone_link"]).first
            trade_url = await zone_link.get_attribute("href")
        except PlaywrightTimeoutError as exc:
            raise TradingPageMissing(
                "进入交易区失败（页面结构可能已改版）。请人工在浏览器里打开交易页后重试。"
            ) from exc
    if not trade_url:
        raise TradingPageMissing("没有找到交易区链接，请人工打开同花顺交易页。")
    page = await platform.context.new_page()
    await page.goto(trade_url, wait_until="domcontentloaded")
    steps = max(1, TIMINGS["zone_new_tab"] // 500)
    for _ in range(steps):
        if await _title(page) == TRADE_PAGE_TITLE:
            await page.bring_to_front()
            return
        await page.wait_for_timeout(500)
    raise TradingPageMissing("打开交易区后 20 秒内没有出现「同花顺虚拟交易所」页面。")


async def open_platform_page() -> Page:
    """打开（或复用）平台首页；不在首页时先回首页，登录轮询依赖首页的 #topUser。"""
    platform = await find_platform_page()
    if platform is None:
        browser = await connect()
        platform = browser.contexts[0].pages[0]
    await platform.bring_to_front()
    if platform.url.rstrip("/") != PLATFORM_URL.rstrip("/"):
        await platform.goto(PLATFORM_URL, wait_until="domcontentloaded")
    await platform.wait_for_timeout(1200)
    return platform


async def login_with_qr(qr_out: Path, timeout: int = 1800, refresh_every: int = 60) -> Path:
    """打开登录页的扫码面板，把二维码写成图片，等人工扫码。

    登录页开在独立标签页，平台首页保持打开用于轮询登录状态。二维码失效时自动
    点刷新；每 ``refresh_every`` 秒重写一次图片，方便外部把图片转发给人。
    """
    import asyncio
    import time

    platform = await open_platform_page()
    if await is_logged_in(platform):
        return qr_out
    login_page = await platform.context.new_page()
    try:
        await login_page.goto("https://%s/login" % LOGIN_HOST, wait_until="domcontentloaded")
        await login_page.wait_for_timeout(2000)
        await login_page.bring_to_front()
        await login_page.locator(SELECTORS["login_qr_toggle"]).first.click()
        await login_page.wait_for_selector(
            SELECTORS["login_qr_image"], state="visible", timeout=10000
        )
    except Exception as exc:
        raise ThsError("打开同花顺登录页失败：%s" % exc) from exc

    qr_out = Path(qr_out)
    qr_out.parent.mkdir(parents=True, exist_ok=True)
    last_shot = 0.0
    last_poll = 0.0
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        now = time.monotonic()
        if now - last_poll >= 4:
            last_poll = now
            try:
                await platform.reload(wait_until="domcontentloaded")
                if await is_logged_in(platform):
                    print("登录成功。", flush=True)
                    try:
                        await login_page.close()
                    except Exception:
                        pass
                    return qr_out
            except Exception:
                pass
        try:
            if await login_page.locator(SELECTORS["login_qr_invalid"]).is_visible():
                await login_page.locator(SELECTORS["login_qr_refresh"]).first.click()
                await login_page.wait_for_timeout(1500)
                last_shot = 0.0
        except Exception:
            pass
        try:
            if await login_page.locator(SELECTORS["login_qr_success"]).is_visible():
                print("已扫码，等待手机端确认…", flush=True)
        except Exception:
            pass
        now = time.monotonic()
        if now - last_shot >= refresh_every:
            try:
                panel = login_page.locator(SELECTORS["login_qr_panel"])
                await panel.screenshot(path=str(qr_out))
                print("二维码已写入 %s" % qr_out, flush=True)
            except Exception as exc:
                print("二维码截图失败：%s" % exc, file=sys.stderr, flush=True)
            last_shot = now
        await asyncio.sleep(2)
    raise NotLoggedIn("等待扫码登录超时（%d 秒）。请重新运行 login 生成新的二维码。" % timeout)


async def _show_submenu(page: Page, parent: str, child: str) -> None:
    if not await page.locator(child).first.is_visible():
        await page.locator(parent).first.click()
        await page.wait_for_timeout(TIMINGS["menu"])
    await page.locator(child).first.click()


async def goto_account(page: Page) -> None:
    await _show_submenu(page, SELECTORS["menu_query"], SELECTORS["menu_account"])
    await page.wait_for_timeout(TIMINGS["panel"])


async def goto_orders(page: Page) -> None:
    await _show_submenu(page, SELECTORS["menu_query"], SELECTORS["menu_orders"])
    await page.wait_for_timeout(TIMINGS["panel"])


async def goto_deals(page: Page) -> None:
    await _show_submenu(page, SELECTORS["menu_query"], SELECTORS["menu_deals"])
    await page.wait_for_timeout(TIMINGS["panel"])


async def read_account(page: Page) -> Dict[str, str]:
    """读取资金概览（字段名 → 原始文本）；资金面板未打开时先打开。"""
    if await page.locator("#kyye").count() == 0:
        await goto_account(page)
    result: Dict[str, str] = {}
    for name, selector in SELECTORS["account_fields"].items():
        locator = page.locator(selector)
        if await locator.count() == 0:
            continue
        try:
            result[name] = (await locator.first.inner_text()).strip()
        except Exception:
            continue
    return result


async def table_records(page: Page, table_selector: str) -> List[Dict[str, str]]:
    """按表头把表格转成记录列表。

    同花顺的表格表头有时在 ``thead``，有时是 ``tbody`` 第一行的 ``th``（空表则是
    ``<tr><th>无数据</th></tr>``）；空单元格保留位置，避免错列。
    """
    table = page.locator(table_selector).first
    if await table.count() == 0:
        return []
    headers: List[str] = []
    for th in await table.locator("thead th").all():
        headers.append((await th.inner_text()).strip())
    records: List[Dict[str, str]] = []
    for tr in await table.locator("tbody tr").all():
        th_cells = [(await th.inner_text()).strip() for th in await tr.locator("th").all()]
        if th_cells:
            if not headers:
                headers = th_cells
            continue
        cells = [(await td.inner_text()).strip() for td in await tr.locator("td").all()]
        if not any(cells) or "无数据" in "".join(cells):
            continue
        if not headers:
            continue
        records.append({header: cells[i] if i < len(cells) else "" for i, header in enumerate(headers) if header})
    return records


async def read_positions(page: Page) -> List[Dict[str, str]]:
    await goto_account(page)
    return await table_records(page, SELECTORS["table_positions"])


async def read_orders(page: Page) -> List[Dict[str, str]]:
    await goto_orders(page)
    return await table_records(page, SELECTORS["table_orders"])


async def read_deals(page: Page) -> List[Dict[str, str]]:
    await goto_deals(page)
    return await table_records(page, SELECTORS["table_deals"])


async def _wait_dialog(page: Page, timeout: Optional[int] = None) -> None:
    await page.wait_for_function(
        "() => { const el = document.querySelector('%s'); if (!el) return false;"
        "return getComputedStyle(el).display !== 'none' && el.offsetParent !== null; }"
        % SELECTORS["prompt"],
        timeout=timeout or TIMINGS["dialog_timeout"],
        polling=200,
    )


async def dialog_text(page: Page) -> str:
    return await page.evaluate(
        "() => { const el = document.querySelector('%s'); if (!el) return '';"
        "if (getComputedStyle(el).display === 'none' || el.offsetParent === null) return '';"
        "return (el.innerText || '').trim(); }" % SELECTORS["prompt"]
    )


async def _close_dialog(page: Page) -> None:
    await page.evaluate(
        """() => {
        const win = document.querySelector('%s');
        if (!win) return;
        const btn = win.querySelector('#ymPrompt_btn_0') || win.querySelector('input[type=button]');
        if (btn) btn.click();
        }""" % SELECTORS["prompt"]
    )


def _parse_suggestions(data) -> Optional[List[Dict[str, str]]]:
    if not data or not data[0]:
        return None
    items = []
    for entry in data[0]:
        parts = str(entry).split("||", 1)
        if len(parts) != 2:
            continue
        fields = parts[1].split(" ")
        if len(fields) < 2:
            continue
        items.append({"code": fields[0], "name": " ".join(fields[1:-1]) if len(fields) > 2 else fields[1]})
    return items or None


async def _read_mkcode(page: Page, panel: str) -> str:
    return await page.evaluate(
        "() => { const s = document.querySelector('%s select[name=gddm]');"
        "return s ? s.value : ''; }" % panel
    )


async def _load_and_click_stock(page: Page, panel: str, cmd: str, code: str) -> Optional[str]:
    """通过页面自己的联想接口加载行情，返回股票名称；联想不可用返回 None。"""
    url = SELECTORS["suggest_url"].format(code=code)
    data = await page.evaluate(
        """(url) => new Promise((resolve) => {
        window.__ths_sug = null;
        window.jsonp = (d) => { window.__ths_sug = d; };
        const s = document.createElement('script');
        s.src = url;
        s.onerror = () => resolve(null);
        document.head.appendChild(s);
        setTimeout(() => resolve(window.__ths_sug), %d);
        })""" % TIMINGS["suggest_timeout"],
        url,
    )
    items = _parse_suggestions(data)
    if items is not None:
        target = next((item for item in items if item["code"] == code), None)
        if target is None:
            return ""  # 明确无此代码
        mkcode = await _read_mkcode(page, panel)
        panel_id = panel.lstrip("#")
        await page.evaluate(
            """(args) => {
            const box = document.querySelector(args.panel + ' #autocomplete_stockcode');
            if (!box || !window.showStockInfo2) return false;
            box.innerHTML = '';
            const dl = document.createElement('dl');
            dl.style.cssText = 'margin:0;cursor:pointer;';
            args.items.forEach(it => {
                const dd = document.createElement('dd');
                dd.textContent = it.code + ' ' + it.name;
                dd.style.cssText = 'padding:2px 4px;';
                dd.dataset.code = it.code;
                dd.addEventListener('click', () => {
                    window.showStockInfo2(args.cmd, dd.dataset.code, args.mkcode, args.panelId);
                });
                dl.appendChild(dd);
            });
            box.appendChild(dl);
            box.style.display = 'block';
            return true;
            }""",
            {"panel": panel, "panelId": panel_id, "cmd": cmd, "mkcode": mkcode, "items": items},
        )
        try:
            await page.locator(
                "%s #autocomplete_stockcode dd[data-code='%s']" % (panel, code)
            ).first.click(timeout=TIMINGS["quote_timeout"])
            return target["name"]
        except PlaywrightTimeoutError:
            pass
    mkcode = await _read_mkcode(page, panel)
    await page.evaluate(
        "(args) => { if (window.showStockInfo2)"
        " window.showStockInfo2(args.cmd, args.code, args.mkcode, args.panelId); }",
        {"cmd": cmd, "code": code, "mkcode": mkcode, "panelId": panel.lstrip("#")},
    )
    return None


async def _quote_loaded(page: Page, panel: str, expected: Optional[str], before: str) -> bool:
    expected_js = json.dumps(expected, ensure_ascii=False) if expected else "null"
    before_js = json.dumps(before, ensure_ascii=False)
    script = (
        "() => { const n = document.querySelector('%s #stname');"
        " const p = document.querySelector('%s #zxjw');"
        " const nv = n ? n.textContent.trim() : ''; const pv = p ? p.textContent.trim() : '-';"
        " const priceOk = pv !== '-' && !isNaN(parseFloat(pv));"
        " const exp = %s; const prev = %s;"
        " if (exp) return nv === exp && priceOk;"
        " return nv !== '' && nv !== prev && priceOk; }" % (panel, panel, expected_js, before_js)
    )
    try:
        await page.wait_for_function(script, timeout=TIMINGS["quote_timeout"], polling=200)
        return True
    except PlaywrightTimeoutError:
        return False


async def place_market_order(
    page: Page, side: str, code: str, shares: int, dry_run: bool = False
) -> Dict[str, Any]:
    """市价委托（最优五档即时成交剩余撤销）。

    ``dry_run`` 只走到确认弹窗并点「取消」，不会真正提交委托；用于下单前自检。
    """
    try:
        if side == "buy":
            await _show_submenu(page, SELECTORS["menu_sjwt"], SELECTORS["menu_sjwt_buy"])
            panel, submit, cmd = SELECTORS["buy_panel"], SELECTORS["submit_buy"], SELECTORS["cmd_buy"]
        elif side == "sell":
            await _show_submenu(page, SELECTORS["menu_sjwt"], SELECTORS["menu_sjwt_sell"])
            panel, submit, cmd = SELECTORS["sell_panel"], SELECTORS["submit_sell"], SELECTORS["cmd_sell"]
        else:
            raise ThsError("方向必须是 buy 或 sell")
        await page.wait_for_timeout(TIMINGS["panel"])
        code_input = page.locator("%s %s" % (panel, SELECTORS["input_code"])).first
        await code_input.fill("")
        await code_input.fill(code)
        await page.wait_for_timeout(TIMINGS["suggest_wait"])
        loaded = False
        for attempt in (1, 2):
            before = await page.evaluate(
                "() => { const el = document.querySelector('%s #stname');"
                "return el ? el.textContent.trim() : ''; }" % panel
            )
            expected = await _load_and_click_stock(page, panel, cmd, code)
            if expected == "":
                raise ThsError("未找到股票代码 %s 的行情，未提交委托。" % code)
            if await _quote_loaded(page, panel, expected, before):
                loaded = True
                break
            if attempt == 1:
                await code_input.fill("")
                await code_input.fill(code)
                await page.wait_for_timeout(TIMINGS["suggest_wait"])
        if not loaded:
            raise ThsError("股票 %s 的行情没有加载出来，未提交委托。" % code)
        type_select = page.locator("%s %s" % (panel, SELECTORS["type_select"]))
        option = type_select.locator("option", has_text=SELECTORS["type_text"]).first
        await option.wait_for(state="attached", timeout=TIMINGS["quote_timeout"])
        await type_select.select_option(index=await option.evaluate("el => el.index"))
        await page.locator("%s %s" % (panel, SELECTORS["input_qty"])).first.fill(str(shares))
        await page.locator("%s %s" % (panel, submit)).first.click()
        await _wait_dialog(page)
        text = await dialog_text(page)
        if dry_run:
            cancel = page.locator(SELECTORS["prompt_cancel"])
            if await cancel.count() == 0:
                raise ThsError("确认弹窗没有取消按钮，自检中止（未提交）。")
            await cancel.first.click()
            await page.wait_for_timeout(600)
            return {"dry_run": True, "confirm_text": text}
        await page.locator(SELECTORS["prompt_ok"]).first.click()
        await _wait_dialog(page)
        text = await dialog_text(page)
        await _close_dialog(page)
        match = re.search(r"合同编号[：:]\s*(\d+)", text)
        return {"dry_run": False, "result": text, "order_id": match.group(1) if match else ""}
    except PlaywrightTimeoutError as exc:
        raise ThsError("下单操作超时，请人工检查页面状态。") from exc
    except ThsError:
        raise
    except Exception as exc:  # pragma: no cover - 页面/网络异常
        raise ThsError("下单失败：%s" % exc) from exc


async def cancel_order(page: Page, order_id: str) -> Dict[str, Any]:
    """按委托号撤单。"""
    try:
        await page.locator(SELECTORS["menu_cancel"]).first.click()
        await page.wait_for_timeout(TIMINGS["panel"])
        checkbox = page.locator(SELECTORS["cancel_checkbox"].format(order_id=order_id))
        if await checkbox.count() == 0:
            raise ThsError("委托 %s 不存在或已成交，无法撤单。" % order_id)
        await checkbox.first.check()
        await page.locator(SELECTORS["cancel_all"]).first.click()
        await _wait_dialog(page)
        text = await dialog_text(page)
        await page.locator(SELECTORS["prompt_ok"]).first.click()
        await page.wait_for_timeout(800)
        return {"result": text, "order_id": order_id}
    except PlaywrightTimeoutError as exc:
        raise ThsError("撤单操作超时，请人工检查页面状态。") from exc
    except ThsError:
        raise
    except Exception as exc:  # pragma: no cover
        raise ThsError("撤单失败：%s" % exc) from exc
