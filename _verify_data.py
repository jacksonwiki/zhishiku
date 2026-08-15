"""快速验证真实数据获取。"""
import sys
sys.path.insert(0, "/Users/yang/wise/zhishiku")

from app.rag.stock_data import (
    get_stock_quote,
    get_stock_kline,
    get_money_flow,
    get_stock_news,
    normalize_code,
)

print("=== normalize_code ===")
print(f"  000001 -> {normalize_code('000001')}")
print(f"  sh600519 -> {normalize_code('sh600519')}")
print(f"  sz000001 -> {normalize_code('sz000001')}")

print("\n=== 行情 (000001 平安银行) ===")
q = get_stock_quote("000001")
if "error" in q:
    print(f"  FAIL: {q['error']}")
else:
    print(f"  OK: {q['name']} 价格={q['price']} 涨跌={q['change']} ({q['change_pct']}%)")
    print(f"  最高={q['high']} 最低={q['low']} 今开={q['open']} 昨收={q['prev_close']}")
    print(f"  成交量={q['volume']:.0f} 成交额={q['amount']:.0f}")

print("\n=== 行情 (600519 贵州茅台) ===")
q2 = get_stock_quote("600519")
if "error" in q2:
    print(f"  FAIL: {q2['error']}")
else:
    print(f"  OK: {q2['name']} 价格={q2['price']} 涨跌={q2['change_pct']}%")

print("\n=== K线 (000001, 日K, 5条) ===")
k = get_stock_kline("000001", "daily", 5)
if "error" in k:
    print(f"  FAIL: {k['error']}")
else:
    print(f"  OK: {len(k['kline'])} 条数据")
    for kl in k["kline"]:
        print(f"    {kl[0]}: 开{kl[1]} 收{kl[2]} 低{kl[3]} 高{kl[4]}")
    print(f"  MA5: {k['ma']['ma5']}")

print("\n=== 资金流向 (000001, 5日) ===")
m = get_money_flow("000001", 5)
if "error" in m:
    print(f"  FAIL: {m['error']}")
else:
    print(f"  OK: {len(m['dates'])} 条数据")
    for i, d in enumerate(m["dates"]):
        print(f"    {d}: 主力={m['main_flow'][i]:.0f} 超大={m['super_large'][i]:.0f} 大单={m['large'][i]:.0f}")

print("\n=== 新闻 (000001) ===")
n = get_stock_news("000001", "平安银行")
if "error" in n:
    print(f"  FAIL: {n['error']}")
else:
    print(f"  OK: {len(n['news'])} 条新闻")
    for i, news in enumerate(n["news"][:3], 1):
        print(f"    [{i}] {news['title']}")

print("\n全部验证完成！")