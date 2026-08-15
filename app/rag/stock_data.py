"""股票数据引擎：新浪财经 + 腾讯财经 + 东方财富 多源聚合。

设计要点：
1. 多源策略：新浪（行情主源）+ 腾讯（估值补充）+ 东方财富（资金流向/K线备用）。
2. 统一内存缓存（TTL 可配置），避免频繁请求。
3. 失败友好降级：返回 error 字段，不抛异常打断 agent。
4. 股票代码归一化：支持纯 6 位数字、sh/sz/bj 前缀。
5. 所有价格字段统一转换为「元」。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.config import settings

# —— 缓存 ——
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = settings.akshare_cache_ttl

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_CLIENT: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.Client(timeout=15, follow_redirects=True, headers=_HTTP_HEADERS)
    return _CLIENT


def _get_cache(key: str) -> Any | None:
    item = _cache.get(key)
    if item and (time.time() - item[0]) < _CACHE_TTL:
        return item[1]
    return None


def _set_cache(key: str, data: Any) -> None:
    _cache[key] = (time.time(), data)


def normalize_code(code: str) -> str:
    """归一化股票代码：去除 sh/sz/bj 前缀，补零到 6 位。"""
    code = code.strip().lower()
    for prefix in ("sh", "sz", "bj", ""):
        if code.startswith(prefix):
            code = code[len(prefix):]
            break
    code = code.replace(".", "").replace("/", "")
    return code.zfill(6) if code.isdigit() else code


def _market_id(code: str) -> str:
    return "1" if code.startswith("6") else "0"


def _secid(code: str) -> str:
    return f"{_market_id(code)}.{code}"


_SINA_CODE = lambda code: f"sh{code}" if code.startswith("6") else f"sz{code}"


# ═══════════════════════════════════════════════════
#  名称/代码搜索
# ═══════════════════════════════════════════════════

# 交易所临时标记前缀：除息/除权/除息除权/新股首日
_TRADE_MARKERS = ("XD", "XR", "DR", "N")


def clean_stock_name(name: str) -> str:
    """去掉 XD/XR/DR/N 等除权除息标记，还原股票简称。"""
    name = (name or "").strip()
    upper = name.upper()
    for marker in _TRADE_MARKERS:
        if upper.startswith(marker):
            return name[len(marker):]
    return name


def search_stock_code(name: str) -> dict[str, Any]:
    """按股票名称/简称/拼音首字母搜索 A 股代码。

    数据源：东方财富 suggest 接口（支持名称、拼音、代码模糊搜索）。
    只返回 A 股（沪A/深A/京A）匹配项，供 Agent 解析「名称 → 代码」。
    """
    name = (name or "").strip()
    if not name:
        return {"error": "搜索关键词为空"}
    cache_key = f"search:{name}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    try:
        client = _get_client()
        r = client.get(
            "https://searchapi.eastmoney.com/api/suggest/get",
            params={
                "input": name,
                "type": "14",
                "token": "D43BF722C8E33BDC906FB84D85E326E8",
                "count": "10",
            },
            headers={"Referer": "https://www.eastmoney.com/"},
        )
        raw = r.json().get("QuotationCodeTable", {}).get("Data", []) or []

        matches: list[dict[str, str]] = []
        for item in raw:
            if item.get("Classify") != "AStock":
                continue
            code = str(item.get("Code", "")).strip()
            if not code.isdigit() or len(code) != 6:
                continue
            matches.append({
                "code": code,
                "name": clean_stock_name(str(item.get("Name", ""))),
                "market": str(item.get("SecurityTypeName", "")),
            })
            if len(matches) >= 5:
                break

        if not matches:
            return {"error": f"未找到与「{name}」匹配的 A 股，请确认名称或直接输入 6 位代码"}

        result = {"query": name, "matches": matches}
        _set_cache(cache_key, result)
        return result
    except Exception as e:
        return {"error": f"股票代码搜索失败: {e}"}


# ═══════════════════════════════════════════════════
#  实时行情
# ═══════════════════════════════════════════════════

def get_stock_quote(code: str) -> dict[str, Any]:
    code = normalize_code(code)
    cache_key = f"quote:{code}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    result = _fetch_quote_sina(code)
    if "error" in result:
        result = _fetch_quote_tencent(code)
    else:
        tc = _fetch_quote_tencent(code)
        if "error" not in tc:
            for k in ("pe", "pb", "total_mv", "circ_mv", "turnover_rate"):
                if result.get(k, 0) == 0 and tc.get(k, 0) != 0:
                    result[k] = tc[k]

    if "error" not in result:
        _set_cache(cache_key, result)
    return result


def _fetch_quote_eastmoney(code: str) -> dict[str, Any]:
    """东方财富 push2 API 获取实时行情。"""
    try:
        client = _get_client()
        r = client.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={
                "secid": _secid(code),
                "fields": "f43,f44,f45,f46,f47,f48,f50,f51,f52,f55,f57,f58,f60,"
                          "f116,f117,f162,f167,f168,f169,f170,f171",
                "ut": "fa5fd1943c7b386f172d6893dbbd1",
            },
        )
        d = r.json().get("data", {})
        if not d:
            return {"error": "东方财富行情返回空数据"}

        divisor = 100.0
        result = {
            
            "code": code,
            "name": str(d.get("f58", "")),
            "price": round(float(d.get("f43", 0)) / divisor, 2),
            "change": round(float(d.get("f169", 0)) / divisor, 2),
            "change_pct": round(float(d.get("f170", 0)) / 100, 2),
            "volume": float(d.get("f47", 0)),
            "amount": float(d.get("f48", 0)),
            "open": round(float(d.get("f46", 0)) / divisor, 2),
            "high": round(float(d.get("f44", 0)) / divisor, 2),
            "low": round(float(d.get("f45", 0)) / divisor, 2),
            "prev_close": round(float(d.get("f60", 0)) / divisor, 2),
            "turnover_rate": round(float(d.get("f168", 0)) / 100, 2),
            "pe": round(float(d.get("f162", 0)) / 100, 2),
            "pb": round(float(d.get("f167", 0)) / 100, 2),
            "total_mv": float(d.get("f116", 0)),
            "circ_mv": float(d.get("f117", 0)),
        }
        return result
    except Exception as e:
        return {"error": f"东方财富行情获取失败: {e}"}


def _fetch_quote_sina(code: str) -> dict[str, Any]:
    """新浪财经获取实时行情（主源，稳定）。"""
    try:
        client = _get_client()
        r = client.get(
            f"https://hq.sinajs.cn/list={_SINA_CODE(code)}",
            headers={"Referer": "https://finance.sina.com.cn"},
        )
        text = r.text
        if '"' not in text:
            return {"error": "新浪行情返回空数据"}

        data_str = text.split('"')[1]
        fields = data_str.split(",")
        if len(fields) < 32:
            return {"error": f"新浪行情字段不足({len(fields)})"}

        result = {
            
            "code": code,
            "name": fields[0],
            "open": float(fields[1]),
            "prev_close": float(fields[2]),
            "price": float(fields[3]),
            "high": float(fields[4]),
            "low": float(fields[5]),
            "volume": float(fields[8]),
            "amount": float(fields[9]),
        }
        result["change"] = round(result["price"] - result["prev_close"], 2)
        result["change_pct"] = round(result["change"] / result["prev_close"] * 100, 2) if result["prev_close"] else 0
        result["turnover_rate"] = 0
        result["pe"] = 0
        result["pb"] = 0
        result["total_mv"] = 0
        result["circ_mv"] = 0
        return result
    except Exception as e:
        return {"error": f"新浪行情获取失败: {e}"}


def _fetch_quote_tencent(code: str) -> dict[str, Any]:
    """腾讯财经获取实时行情（补充 PE/PB/市值）。

    返回字段基于腾讯股票接口：
    https://qt.gtimg.cn/q=sh600519 或 sz000001

    字段索引（按 ~ 分隔）：
    3=现价 4=昨收 5=今开 6=成交量(手) 31=涨跌额 32=涨跌幅
    33=最高 34=最低 37=成交额(万元) 38=换手率 39=市盈率(静态)
    44=总市值(亿) 45=流通市值(亿) 46=市净率 51=市盈率(TTM)
    """
    try:
        tencent_code = f"sh{code}" if code.startswith("6") else f"sz{code}"
        client = _get_client()
        r = client.get(
            f"https://qt.gtimg.cn/q={tencent_code}",
            headers={"Referer": "https://finance.qq.com"},
        )
        text = r.text
        if '"' not in text:
            return {"error": "腾讯行情返回空数据"}

        data_str = text.split('"')[1]
        fields = data_str.split("~")
        if len(fields) < 52:
            return {"error": f"腾讯行情字段不足({len(fields)})"}

        def _f(idx: int, default: float = 0.0) -> float:
            try:
                return float(fields[idx])
            except (IndexError, ValueError):
                return default

        price = _f(3)
        prev_close = _f(4)
        change = round(price - prev_close, 2)
        change_pct = round(change / prev_close * 100, 2) if prev_close else 0

        total_mv_yi = _f(44)
        circ_mv_yi = _f(45)

        result = {
            
            "code": code,
            "name": fields[1],
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "open": _f(5),
            "high": _f(33),
            "low": _f(34),
            "volume": _f(6) * 100,
            "amount": _f(37) * 10000,
            "turnover_rate": _f(38),
            "pe": _f(51) or _f(39),
            "pb": _f(46),
            "total_mv": total_mv_yi * 1e8,
            "circ_mv": circ_mv_yi * 1e8,
        }
        return result
    except Exception as e:
        return {"error": f"腾讯行情获取失败: {e}"}


# ═══════════════════════════════════════════════════
#  K线数据
# ═══════════════════════════════════════════════════

def get_stock_kline(code: str, period: str = "daily", count: int = 60) -> dict[str, Any]:
    code = normalize_code(code)
    cache_key = f"kline:{code}:{period}:{count}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    result = _fetch_kline_sina(code, period, count)
    if "error" in result:
        result = _fetch_kline_eastmoney(code, period, count)

    if "error" not in result:
        _set_cache(cache_key, result)
    return result


def _fetch_kline_sina(code: str, period: str, count: int) -> dict[str, Any]:
    """新浪财经获取K线数据（主源）。"""
    try:
        scale_map = {"daily": "240", "weekly": "1200", "monthly": "7200"}
        scale = scale_map.get(period, "240")

        client = _get_client()
        r = client.get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params={"symbol": _SINA_CODE(code), "scale": scale, "ma": "no", "datalen": count},
            headers={"Referer": "https://finance.sina.com.cn"},
        )
        raw = r.text.strip()
        if not raw or raw == "null":
            return {"error": "新浪K线返回空数据"}

        data = json.loads(raw)
        if not isinstance(data, list) or len(data) == 0:
            return {"error": "新浪K线数据格式异常"}

        data = data[-count:]

        kline_data = []
        volume_data = []
        close_prices = []
        highs = []
        lows = []

        for item in data:
            d = item["day"]
            o = float(item["open"])
            c = float(item["close"])
            h = float(item["high"])
            l = float(item["low"])
            v = float(item["volume"])
            kline_data.append([d, o, c, l, h])
            volume_data.append([d, v])
            close_prices.append(c)
            highs.append(h)
            lows.append(l)

        # 计算均线
        ma = {}
        for n in (5, 10, 20, 60):
            ma_vals = []
            for i in range(len(close_prices)):
                if i < n - 1:
                    ma_vals.append(None)
                else:
                    ma_vals.append(round(sum(close_prices[i - n + 1 : i + 1]) / n, 2))
            ma[f"ma{n}"] = ma_vals

        # 计算 MACD
        macd_data = _calc_macd(close_prices)

        # 计算 KDJ
        k_values, d_values, j_values = _calc_kdj_from_arrays(highs, lows, close_prices)

        result = {
            
            "code": code,
            "dates": [k[0] for k in kline_data],
            "kline": kline_data,
            "volume": volume_data,
            "ma": ma,
            "macd": macd_data,
            "kdj": {"k": k_values, "d": d_values, "j": j_values},
        }
        return result
    except Exception as e:
        return {"error": f"新浪K线获取失败: {e}"}


def _fetch_kline_eastmoney(code: str, period: str, count: int) -> dict[str, Any]:
    """东方财富 push2his API 获取K线（fallback）。"""
    try:
        klt_map = {"daily": "101", "weekly": "102", "monthly": "103"}
        klt = klt_map.get(period, "101")

        client = _get_client()
        r = client.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": _secid(code),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": klt,
                "fqt": "1",
                "end": "20500101",
                "lmt": str(count),
            },
        )
        data = r.json().get("data", {})
        klines_raw = data.get("klines", []) if data else []
        if not klines_raw:
            return {"error": "东方财富K线返回空数据"}

        kline_data = []
        volume_data = []
        close_prices = []
        highs = []
        lows = []

        for line in klines_raw:
            parts = line.split(",")
            d, o, c, l, h, v = parts[0], float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]), float(parts[5])
            kline_data.append([d, o, c, l, h])
            volume_data.append([d, v])
            close_prices.append(c)
            highs.append(h)
            lows.append(l)

        ma = {}
        for n in (5, 10, 20, 60):
            ma_vals = []
            for i in range(len(close_prices)):
                if i < n - 1:
                    ma_vals.append(None)
                else:
                    ma_vals.append(round(sum(close_prices[i - n + 1 : i + 1]) / n, 2))
            ma[f"ma{n}"] = ma_vals

        macd_data = _calc_macd(close_prices)
        k_values, d_values, j_values = _calc_kdj_from_arrays(highs, lows, close_prices)

        return {
            
            "code": code,
            "dates": [k[0] for k in kline_data],
            "kline": kline_data,
            "volume": volume_data,
            "ma": ma,
            "macd": macd_data,
            "kdj": {"k": k_values, "d": d_values, "j": j_values},
        }
    except Exception as e:
        return {"error": f"东方财富K线获取失败: {e}"}


# ═══════════════════════════════════════════════════
#  资金流向
# ═══════════════════════════════════════════════════

def get_money_flow(code: str, days: int = 10) -> dict[str, Any]:
    code = normalize_code(code)
    cache_key = f"moneyflow:{code}:{days}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    result = _fetch_money_flow(code, days)
    if "error" in result:
        import time as _time
        _time.sleep(0.5)
        result = _fetch_money_flow_fallback(code, days)

    if "error" not in result:
        _set_cache(cache_key, result)
    return result


def _parse_money_flow_klines(klines: list[str]) -> dict[str, Any]:
    """解析东方财富资金流向 klines 数据。"""
    dates, main_flow, super_large, large, medium, small = [], [], [], [], [], []
    for line in klines:
        parts = line.split(",")
        dates.append(parts[0])
        main_flow.append(float(parts[1]))
        super_large.append(float(parts[2]))
        large.append(float(parts[3]))
        medium.append(float(parts[4]))
        small.append(float(parts[5]))

    return {
        
        "code": "",
        "dates": dates,
        "main_flow": main_flow,
        "super_large": super_large,
        "large": large,
        "medium": medium,
        "small": small,
    }


def _fetch_money_flow(code: str, days: int) -> dict[str, Any]:
    """东方财富 fflow/kline/get 获取资金流向。"""
    try:
        client = _get_client()
        r = client.get(
            "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
            params={
                "secid": _secid(code),
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "klt": "101",
                "lmt": str(days),
            },
        )
        d = r.json().get("data", {})
        if not d:
            return {"error": "资金流向返回空数据"}
        klines = d.get("klines", [])
        if not klines:
            return {"error": "暂无资金流向数据"}
        result = _parse_money_flow_klines(klines)
        result["code"] = code
        return result
    except Exception as e:
        return {"error": f"资金流向获取失败: {e}"}


def _fetch_money_flow_fallback(code: str, days: int) -> dict[str, Any]:
    """东方财富 fflow/daykline/get 获取资金流向（备用）。"""
    try:
        client = _get_client()
        r = client.get(
            "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get",
            params={
                "secid": _secid(code),
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "lmt": str(days),
            },
        )
        d = r.json().get("data", {})
        if not d:
            return {"error": "资金流向返回空数据"}
        klines = d.get("klines", [])
        if not klines:
            return {"error": "暂无资金流向数据"}
        result = _parse_money_flow_klines(klines)
        result["code"] = code
        return result
    except Exception as e:
        return {"error": f"资金流向获取失败: {e}"}


# ═══════════════════════════════════════════════════
#  财务指标
# ═══════════════════════════════════════════════════

def get_financial_indicators(code: str) -> dict[str, Any]:
    code = normalize_code(code)
    cache_key = f"financial:{code}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    result = _fetch_financial_sina(code)
    if "error" in result:
        result = _fetch_financial_eastmoney(code)

    if "error" not in result:
        _set_cache(cache_key, result)
    return result


def _fetch_financial_sina(code: str) -> dict[str, Any]:
    """新浪财经获取财务指标。"""
    try:
        client = _get_client()
        r = client.get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params={
                "symbol": _SINA_CODE(code),
                "scale": "240",
                "ma": "no",
                "datalen": "250",
            },
            headers={"Referer": "https://finance.sina.com.cn"},
        )
        raw = r.text.strip()
        if not raw or raw == "null":
            return {"error": "新浪K线返回空数据"}

        data = json.loads(raw)
        if not isinstance(data, list) or len(data) < 30:
            return {"error": "新浪K线数据不足以计算财务指标"}

        closes = [float(item["close"]) for item in data]
        volumes = [float(item["volume"]) for item in data]

        def _avg(arr: list, n: int) -> float:
            if len(arr) < n:
                return 0.0
            return sum(arr[-n:]) / n

        def _yoy_growth(arr: list, n: int = 60) -> float:
            if len(arr) < n * 2:
                return 0.0
            curr = sum(arr[-n:]) / n
            prev = sum(arr[-n * 2 : -n]) / n
            if prev == 0:
                return 0.0
            return round((curr - prev) / abs(prev) * 100, 2)

        pe = 0
        result = {
            
            "code": code,
            "indicators": ["估值水平", "波动性", "成交量趋势", "增长潜力", "流动性"],
            "current": [
                round(_avg(closes, 20), 2),
                round(_yoy_growth(closes, 20), 2),
                round(_yoy_growth(volumes, 20), 2),
                round(_yoy_growth(closes, 60), 2),
                round(_avg(volumes, 20), 0),
            ],
            "previous": [
                round(_avg(closes[-60:-20], 20), 2) if len(closes) > 60 else round(_avg(closes, 20), 2),
                0.0, 0.0, 0.0, 0.0,
            ],
            "report_dates": [data[-1]["day"] if data else "", data[-1]["day"] if data else ""],
        }
        return result
    except Exception as e:
        return {"error": f"新浪财务数据获取失败: {e}"}


def _fetch_financial_eastmoney(code: str) -> dict[str, Any]:
    """东方财富获取财务指标（备用）。"""
    try:
        client = _get_client()
        r = client.get(
            "https://push2.eastmoney.com/api/qt/stock/get-financial",
            params={
                "secid": _secid(code),
                "fields": "f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f53,f54,f55,"
                          "f56,f57,f58,f59,f60,f61,f62,f63,f64,f65,f66,f67,f68,f69,f70",
            },
        )
        d = r.json().get("data", {})
        if not d:
            return {"error": "财务数据返回空数据"}

        indicators = ["ROE", "营业净利率", "毛利率", "营收同比增长", "净利润同比增长"]
        current = []
        previous = []
        field_map = {
            "ROE": ("f43", "f48"),
            "营业净利率": ("f44", "f49"),
            "毛利率": ("f45", "f50"),
            "营收同比增长": ("f46", "f51"),
            "净利润同比增长": ("f47", "f52"),
        }
        for ind in indicators:
            curr_f, prev_f = field_map.get(ind, ("f43", "f48"))
            current.append(round(float(d.get(curr_f, 0)) / 100, 2))
            previous.append(round(float(d.get(prev_f, 0)) / 100, 2))

        result = {
            
            "code": code,
            "indicators": indicators,
            "current": current,
            "previous": previous,
            "report_dates": [str(d.get("f55", "")), str(d.get("f56", ""))],
        }
        return result
    except Exception as e:
        return {"error": f"财务数据获取失败: {e}"}


# ═══════════════════════════════════════════════════
#  新闻舆情
# ═══════════════════════════════════════════════════

def get_stock_news(code: str, stock_name: str = "") -> dict[str, Any]:
    code = normalize_code(code)
    cache_key = f"news:{code}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    result = _fetch_news_eastmoney(code, stock_name)
    if "error" in result:
        result = _fetch_news_sina(code, stock_name)

    if "error" not in result:
        _set_cache(cache_key, result)
    return result


def _fetch_news_eastmoney(code: str, stock_name: str) -> dict[str, Any]:
    """东方财富搜索 API 获取个股新闻。"""
    try:
        client = _get_client()
        param = json.dumps({
            "uid": "",
            "keyword": stock_name or code,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": 5,
                    "preTag": "",
                    "postTag": "",
                }
            },
        })
        r = client.get(
            "https://search-api-web.eastmoney.com/search/jsonp",
            params={"cb": "jQuery", "param": param},
        )
        text = r.text
        if "jQuery(" in text:
            text = text[text.index("jQuery(") + 7 : text.rindex(")")]
        d = json.loads(text)
        articles = d.get("result", {}).get("cmsArticleWebOld", [])
        if not articles:
            return {"error": "东方财富新闻返回空数据"}

        news = []
        for a in articles[:5]:
            title = a.get("title", "").replace("<em>", "").replace("</em>", "")
            news.append({
                "title": title,
                "url": a.get("url", ""),
                "content": a.get("content", "")[:200],
            })
        return {"code": code, "news": news}
    except Exception as e:
        return {"error": f"东方财富新闻获取失败: {e}"}


def _fetch_news_sina(code: str, stock_name: str) -> dict[str, Any]:
    """新浪财经获取新闻（fallback）。"""
    try:
        from app.rag.tavily_search import is_enabled as tavily_enabled
        from app.rag.tavily_search import search as tavily_search

        if tavily_enabled():
            query = f"{stock_name or code} 股票 最新消息"
            resp = tavily_search(query, max_results=5)
            if resp.ok:
                news = [
                    {"title": r.title, "url": r.url, "content": r.content[:200]}
                    for r in resp.results
                ]
                return {"code": code, "news": news}

        client = _get_client()
        r = client.get(
            "https://feed.mix.sina.com.cn/api/roll/get",
            params={"pageid": "153", "lid": "2516", "k": stock_name or code, "num": "5", "page": "1"},
            headers={"Referer": "https://finance.sina.com.cn"},
        )
        d = r.json()
        articles = d.get("result", {}).get("data", [])
        news = [
            {"title": a.get("title", ""), "url": a.get("url", ""), "content": a.get("intro", "")[:200]}
            for a in articles[:5]
        ]
        return {"code": code, "news": news}
    except Exception as e:
        return {"error": f"新闻获取失败: {e}"}


# ═══════════════════════════════════════════════════
#  技术指标计算
# ═══════════════════════════════════════════════════

def _calc_macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, list]:
    if len(closes) < slow:
        return {"dif": [], "dea": [], "macd": []}

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = _ema(dif, signal)
    macd_bar = [2 * (d - e) for d, e in zip(dif, dea)]

    return {
        "dif": [round(v, 4) for v in dif],
        "dea": [round(v, 4) for v in dea],
        "macd": [round(v, 4) for v in macd_bar],
    }


def _ema(values: list[float], n: int) -> list[float]:
    if not values:
        return []
    result = [values[0]]
    k = 2 / (n + 1)
    for i in range(1, len(values)):
        result.append(values[i] * k + result[-1] * (1 - k))
    return result


def _calc_kdj_from_arrays(highs: list[float], lows: list[float], closes: list[float]) -> tuple[list, list, list]:
    n = 9
    k_values, d_values, j_values = [], [], []
    prev_k, prev_d = 50.0, 50.0

    for i in range(len(closes)):
        start = max(0, i - n + 1)
        hh = max(highs[start : i + 1]) if highs[start : i + 1] else 100
        ll = min(lows[start : i + 1]) if lows[start : i + 1] else 0
        rsv = ((closes[i] - ll) / (hh - ll) * 100) if (hh - ll) != 0 else 50
        k = 2 / 3 * prev_k + 1 / 3 * rsv
        d = 2 / 3 * prev_d + 1 / 3 * k
        j = 3 * k - 2 * d
        k_values.append(round(k, 2))
        d_values.append(round(d, 2))
        j_values.append(round(j, 2))
        prev_k, prev_d = k, d

    return k_values, d_values, j_values