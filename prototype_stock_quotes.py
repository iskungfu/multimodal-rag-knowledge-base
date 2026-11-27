import requests, json, time, random

SYMBOLS = [
    ("1.600519", "贵州茅台"),
    ("0.300750", "宁德时代"),
    ("1.000001", "上证指数"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}
FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f168,f169,f170"

def fetch(secid, name, retries=3, base_delay=5):
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields={FIELDS}"
    for attempt in range(retries):
        try:
            t0 = time.time()
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.encoding = "gbk"
            dt = round((time.time() - t0) * 1000)
            d = (r.json().get("data") or {})
            if not d and attempt < retries - 1:
                time.sleep(base_delay * (2 ** attempt) + random.random())
                continue
            return {
                "secid": secid,
                "name": d.get("f58"),
                "price": round(d.get("f43", 0) / 100, 2),
                "change_pct": round(d.get("f170", 0) / 10, 2),
                "volume": d.get("f47"),
                "turnover": round(d.get("f48", 0) / 1e8, 2),
                "pe": round(d.get("f169", 0) / 10, 2),
                "turnover_rate": round(d.get("f168", 0) / 100, 2),
                "high": round(d.get("f44", 0) / 100, 2),
                "low": round(d.get("f45", 0) / 100, 2),
                "open": round(d.get("f46", 0) / 100, 2),
                "pre_close": round(d.get("f60", 0) / 100, 2),
                "latency_ms": dt,
            }
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(base_delay * (2 ** attempt) + random.random())
            else:
                raise

if __name__ == "__main__":
    for i, (secid, name) in enumerate(SYMBOLS):
        if i > 0:
            print(f"等待 8 秒避免限流...")
            time.sleep(8)
        print(f"\n=== {name} ({secid}) ===")
        try:
            result = fetch(secid, name)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"FAILED: {type(e).__name__}: {e}")
