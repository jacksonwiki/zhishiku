"""调试东方财富 API 返回字段。"""
import sys, json
sys.path.insert(0, "/Users/yang/wise/zhishiku")

import httpx

client = httpx.Client(timeout=15.0)

# 测试 000001
code = "000001"
secid = f"0.{code}"  # 深市

url = "https://push2.eastmoney.com/api/qt/stock/get"
params = {
    "secid": secid,
    "fields": "f43,f44,f45,f46,f47,f48,f50,f51,f52,f55,f57,f58,f60,"
              "f116,f117,f162,f167,f168,f169,f170,f171",
    "ut": "fa5fd1943c7b386f172d6893dbbd1",
}

try:
    r = client.get(url, params=params)
    data = r.json()
    print("=== 东方财富原始响应 (000001) ===")
    print(json.dumps(data, indent=2, ensure_ascii=False))
except Exception as e:
    print(f"FAIL: {e}")

# 也测试新浪
print("\n=== 新浪原始响应 (000001) ===")
r2 = client.get(
    "https://hq.sinajs.cn/list=sz000001",
    headers={"Referer": "https://finance.sina.com.cn"},
)
print(r2.text[:500])

# 测试东方财富 fflow
print("\n=== 东方财富资金流向 (000001) ===")
try:
    r3 = client.get(
        "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
        params={
            "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "klt": "101",
            "lmt": "5",
        },
    )
    print(json.dumps(r3.json(), indent=2, ensure_ascii=False)[:1000])
except Exception as e:
    print(f"FAIL: {e}")

# 测试东方财富日线 K 线
print("\n=== 东方财富 K线 (000001) ===")
try:
    r4 = client.get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params={
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": "101",
            "fqt": "1",
            "end": "20500101",
            "lmt": "5",
        },
    )
    d4 = r4.json()
    if d4.get("data"):
        print(f"  名称: {d4['data'].get('name')}")
        print(f"  代码: {d4['data'].get('code')}")
        klines = d4["data"].get("klines", [])
        for k in klines:
            print(f"  {k}")
    else:
        print(f"  无数据: {json.dumps(d4, indent=2)[:500]}")
except Exception as e:
    print(f"FAIL: {e}")