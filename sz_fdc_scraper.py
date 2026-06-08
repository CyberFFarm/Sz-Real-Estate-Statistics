#!/usr/bin/env python3
"""深圳房地产信息平台 - 自动抓取脚本"""

import json, os, subprocess
from datetime import datetime

COOKIE = ('cookssotoken=eyJpc3N1Y2Nlc3MiOiJ0cnVlIiwiZmFpbHJlc29uIjoiIiwiYWNjb3VudCI6InhpYTQwNjc4OTU2NTE3MDE5Mzg3OTkiLCJ0b2tlbiI6IjI3NDBmMDkyYmEzNzRjOGFiYWZiODJlZDFhYzk4MTkzIn0=.Eg4DFhERDQ==; WSESSIONID-SZFDC-COMMON=SRSfyr4sRsnT47gEfh4cPp5bcyZu6GvvUfHnwO4-RDKOgYeqE1JP!-752690671; BIGipServerPool-fdc-SC-9001=2248500234.13091.0000; BIGipServerPool-fdc-SC-9004=2248500234.13859.0000; szfdc-session-id=721a8d3c-dc29-44c8-a86f-ed8e283b0acc; ssotoken="eyJpc3N1Y2Nlc3MiOiJ0cnVlIiwiZmFpbHJlc29uIjoiIiwiYWNjb3VudCI6InhpYTQwNjc4OTU2NTE3MDE5Mzg3OTkiLCJ0b2tlbiI6IjI3NDBmMDkyYmEzNzRjOGFiYWZiODJlZDFhYzk4MTkzIn0=.Eg4DFhERDQ=="; BIGipServerPool-fdc-SC-9002=2231723018.13347.0000')
BASE = "https://fdc.zjj.sz.gov.cn/szfdccommon"
ZONE = "%E5%85%A8%E5%B8%82"
OUT = os.path.dirname(os.path.abspath(__file__))
CURL_ARGS = ["curl", "-s", "--max-time", "15", "-b", COOKIE,
    "-H", "Accept: application/json, text/plain, */*",
    "-H", "Referer: https://fdc.zjj.sz.gov.cn/szfdccommon/",
    "-H", "User-Agent: Mozilla/5.0"]

def get(api_path):
    url = f"{BASE}/{api_path}?zone={ZONE}" if "?" not in api_path else f"{BASE}/{api_path}"
    r = subprocess.run([*CURL_ARGS, url], capture_output=True, text=True, timeout=20)
    return json.loads(r.stdout)

def cat_key(item, *keys):
    for k in keys:
        if item.get(k):
            return item[k]
    return ""

# Data fetchers - each returns list of {"cat": str, "units": int, "area": float, "date": str}
def presale_daily():
    d = get("ysfcjxxnew/ysfcjgs1")
    return [{"cat": cat_key(r, "reportcatalog"), "units": r["rgts"], "area": r["rgarea"],
             "date": d["data"]["xmlDateDay"]} for r in d["data"]["list"]]

def current_sale_daily():
    d = get("ysfcjxxnew/ysfcjgs2")
    return [{"cat": cat_key(r, "useAge"), "units": r["dealCount"], "area": r["dealArea"],
             "date": d["data"]["xmlDateDay"]} for r in d["data"]["list"]]

def new_approval_daily():
    d = get("ysfcjxxnew/ysfcjgs3")
    return [{"cat": cat_key(r, "areaRange"), "units": r["dealCount"], "area": r["dealArea"],
             "date": d["data"]["xmlDateDay"]} for r in d["data"]["list"]]

def presale_monthly():
    d = get("ysfcjxxnew/ysfcjgs2ForMonth")
    return [{"cat": cat_key(r, "useAge"), "units": r["dealCount"], "area": r["dealArea"],
             "date": d["data"]["xmlDateMonth"]} for r in d["data"]["list"]]

def new_approval_monthly():
    d = get("ysfcjxxnew/ysfcjgs3ForMonth")
    return [{"cat": cat_key(r, "areaRange"), "units": r["dealCount"], "area": r["dealArea"],
             "date": d["data"]["xmlDateMonth"]} for r in d["data"]["list"]]

def new_house_price_index():
    return get("ysfcjxx/marketInfoShow/getHousePriceIndex?type=1")["data"]["listData"]

def second_house_price_index():
    return get("ysfcjxx/marketInfoShow/getHousePriceIndex?type=2")["data"]["listData"]

def second_daily():
    d = get("esfCjxxNew/esfcjgsDay")
    return [{"cat": cat_key(r, "usage"), "units": r["contractCount"], "area": r["buildingArea"],
             "date": d["data"]["xmlDateDay"]} for r in d["data"]["list"]]

def second_monthly():
    d = get("esfCjxxNew/esfcjgsMonth")
    return [{"cat": cat_key(r, "usage"), "units": r["contractCount"], "area": r["buildingArea"],
             "date": d["data"]["xmlDateMonth"]} for r in d["data"]["list"]]

def main():
    ts = datetime.now()
    today = ts.strftime("%Y%m%d")
    all_data = {"fetch_time": ts.strftime("%Y-%m-%d %H:%M:%S")}

    tasks = {
        "一手房 预售成交 当日": presale_daily,
        "一手房 现售成交 当日": current_sale_daily,
        "一手房 新批准预售 当日": new_approval_daily,
        "一手房 预售成交 当月": presale_monthly,
        "一手房 新批准预售 当月": new_approval_monthly,
        "一手房 价格指数(13月)": new_house_price_index,
        "二手房 价格指数(13月)": second_house_price_index,
        "二手房 成交 当日": second_daily,
        "二手房 成交 当月": second_monthly,
    }

    i = 0
    for label, fn in tasks.items():
        i += 1
        try:
            print(f"[{i}/{len(tasks)}] {label}...", end=" ")
            data = fn()
            all_data[label] = data
            if isinstance(data, list) and data and isinstance(data[0], dict):
                last = data[-1]
                print(f"=> {last.get('cat',last.get('year',''))} {last.get('units','') or last.get('price_total','')}")
            elif isinstance(data, list) and data and 'price_increase_rate' in data[0]:
                print(f"=> {data[0].get('year','')}/{data[0].get('month','')} 指数{data[0].get('price_total','')} 环比{data[0].get('price_increase_rate','')}")
        except Exception as e:
            print(f"FAIL: {e}")

    # Save JSON
    jp = os.path.join(OUT, f"sz_fdc_{today}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    print(f"\nJSON: {jp}")

    # Accumulate daily history
    hist_path = os.path.join(OUT, "data", "daily_history.json")
    daily_hist = {"new": {}, "old": {}, "updated": ts.strftime("%Y-%m-%d %H:%M:%S")}
    if os.path.exists(hist_path):
        with open(hist_path, "r", encoding="utf-8") as f:
            daily_hist = json.load(f)

    today_iso = ts.strftime("%Y-%m-%d")
    daily_hist.setdefault("new", {})
    daily_hist.setdefault("old", {})

    # Build new house daily structure from scraper results
    presale_items = all_data.get("一手房 预售成交 当日", [])
    current_items = all_data.get("一手房 现售成交 当日", [])
    approval_items = all_data.get("一手房 新批准预售 当日", [])
    new_daily = {}
    if presale_items:
        new_daily["预售成交"] = {r["cat"]: {"units": r["units"], "area": r["area"]} for r in presale_items}
    if current_items:
        new_daily["现售成交"] = {r["cat"]: {"units": r["units"], "area": r["area"]} for r in current_items}
    if approval_items:
        new_daily["新批准预售"] = {r["cat"]: {"units": r["units"], "area": r["area"]} for r in approval_items}
    if new_daily:
        daily_hist["new"][today_iso] = new_daily

    # Build second-hand daily structure
    second_items = all_data.get("二手房 成交 当日", [])
    if second_items:
        daily_hist["old"][today_iso] = {r["cat"]: {"units": r["units"], "area": r["area"]} for r in second_items}

    daily_hist["updated"] = ts.strftime("%Y-%m-%d %H:%M:%S")
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(daily_hist, f, ensure_ascii=False, indent=2)
    print(f"Daily history updated: {hist_path} ({len(daily_hist['new'])} new / {len(daily_hist['old'])} old days)")

    # Save CSV
    cp = os.path.join(OUT, f"sz_fdc_{today}.csv")
    with open(cp, "w", encoding="utf-8") as f:
        f.write("类别,周期,日期,房产类型,套数,面积㎡\n")
        for label, data in all_data.items():
            if not isinstance(data, list) or not data or "units" not in data[0]:
                continue
            period = "当日" if "当日" in label else "当月"
            for r in data:
                f.write(f"{label},{period},{r.get('date','')},{r['cat']},{r.get('units','')},{r.get('area','')}\n")
    print(f"CSV: {cp}")

    print("\n" + "=" * 60)
    print("  深圳房地产数据抓取结果 (2026年6月6日)")
    print("=" * 60)
    for label in ["一手房 预售成交 当日", "一手房 现售成交 当日",
                  "一手房 预售成交 当月", "二手房 成交 当日", "二手房 成交 当月"]:
        data = all_data.get(label)
        if data and isinstance(data, list) and data:
            total = data[-1]
            print(f"  {label}: {total['cat']}: {total['units']}套 / {total['area']}㎡")
    print("=" * 60)

if __name__ == "__main__":
    main()
