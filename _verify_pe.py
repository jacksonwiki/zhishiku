"""验证 PE/PB/市值修复。"""
import sys
sys.path.insert(0, "/Users/yang/wise/zhishiku")

from app.rag.stock_data import get_stock_quote

print("=== 000001 平安银行 ===")
q = get_stock_quote("000001")
if "error" in q:
    print(f"  FAIL: {q['error']}")
else:
    print(f"  名称: {q['name']}")
    print(f"  价格: {q['price']} 元")
    print(f"  涨跌: {q['change']} ({q['change_pct']}%)")
    print(f"  PE: {q['pe']}  PB: {q['pb']}")
    print(f"  总市值: {q['total_mv']}  流通市值: {q['circ_mv']}")

print("\n=== 600519 贵州茅台 ===")
q2 = get_stock_quote("600519")
if "error" in q2:
    print(f"  FAIL: {q2['error']}")
else:
    print(f"  名称: {q2['name']}")
    print(f"  价格: {q2['price']} 元")
    print(f"  PE: {q2['pe']}  PB: {q2['pb']}")
    print(f"  总市值: {q2['total_mv']}  流通市值: {q2['circ_mv']}")

print("\n=== 300750 宁德时代 ===")
q3 = get_stock_quote("300750")
if "error" in q3:
    print(f"  FAIL: {q3['error']}")
else:
    print(f"  名称: {q3['name']}")
    print(f"  价格: {q3['price']} 元")
    print(f"  PE: {q3['pe']}  PB: {q3['pb']}")
    print(f"  总市值: {q3['total_mv']}  流通市值: {q3['circ_mv']}")