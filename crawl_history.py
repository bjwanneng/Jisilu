# -*- coding: utf-8 -*-
"""逐只抓取集思录 ETF 近50个交易日历史数据（detail_hists 接口）。

- 指数ETF(1396) + 黄金ETF(14)；货币ETF 逐日历史意义不大，跳过
- 每次请求间隔 0.35s，预计 ~10 分钟
- 结果逐行追加到 etf_history.jsonl，可断点续传（已抓的 fund_id 跳过）
"""
import json
import sys
import time

sys.path.insert(0, ".")
from jisilu import get_session, BASE

DELAY = 0.35
OUT = "etf_history.jsonl"


def load_done():
    done = set()
    try:
        with open(OUT, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["fund_id"])
                except (ValueError, KeyError):
                    pass
    except OSError:
        pass
    return done


def main():
    fund_ids = []
    for src in ("etf_full.json", "gold_full.json"):
        d = json.load(open(src, encoding="utf-8"))
        fund_ids += [r["cell"]["fund_id"] for r in d["rows"]]
    print(f"待抓取池: {len(fund_ids)} 只")

    done = load_done()
    todo = [f for f in fund_ids if f not in done]
    print(f"已完成 {len(done)}，剩余 {len(todo)}")

    s = get_session()
    t0 = time.time()
    ok = fail = 0
    with open(OUT, "a", encoding="utf-8") as out:
        for i, fid in enumerate(todo, 1):
            try:
                r = s.post(f"{BASE}/data/etf/detail_hists/",
                           data={"is_search": 1, "fund_id": fid, "rp": 50, "page": 1},
                           timeout=30)
                rows = r.json().get("rows", [])
                rec = {"fund_id": fid,
                       "history": [row["cell"] for row in rows]}
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                ok += 1
            except Exception as e:
                fail += 1
                print(f"[{fid}] 失败: {e}", file=sys.stderr)
            if i % 50 == 0:
                rate = i / (time.time() - t0)
                print(f"进度 {i}/{len(todo)} ok={ok} fail={fail} "
                      f"({rate:.1f} 只/秒, 预计剩余 {(len(todo)-i)/rate/60:.1f} 分钟)", flush=True)
            time.sleep(DELAY)
    print(f"完成: ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
