#!/usr/bin/env python3
"""reach-rate.py — 「危险操作前触达率」基线（方案 v1 第 5 节主指标）。只读。

为什么必须**现在**量：M1 触发注入刚上线。不先拿到上线前的基线，
之后无论触达率是高是低都说明不了任何事 —— 「没有对照的前后对比」不是证据。

口径（可判定、可复现，不掺推断）：
  对白名单里的每个档，取它的触发词（逐字来自「又踩」原文）；
  扫历史会话，找出 **prompt 里出现过该触发词** 的会话；
  再看那些会话里，该专题档**当次是否被 Read / Grep / Bash 碰过**。
  触达率 = 碰过的会话数 / 命中触发词的会话数。

  这是「该被想起来的时候，有没有被想起来」的一个下界代理：
  prompt 提到了那个词，说明话题确实相关；档没被打开，就是一次没触达。

已知偏差（写出来，不藏着）：
  · 只看 prompt 里的触发词，漏掉「模型自己决定去动那个东西、用户没提」的场景 ⇒ **低估分母**；
  · 档被 Read 过不等于判据被用上 ⇒ **高估分子**。
  两个偏差方向相反，绝对值别当真；**它的用途是同口径前后对比**，不是绝对水平。

用法：
  python3 reach-rate.py                          # 用默认白名单
  python3 reach-rate.py --days 90 --json out.json
"""

import argparse
import collections
import glob
import json
import os
import re
import time

BASE = os.path.expanduser("~/.claude/projects")
WHITELIST = os.path.expanduser("~/.claude/memory-triggers.json")


def session_prompts_and_touches(path, bucket):
    """一个会话里的 (所有 user prompt 文本, 被碰过的 memory 档名集合)。"""
    prompts, touched = [], set()
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"user"' not in line and "tool_use" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                msg = obj.get("message") or {}
                if obj.get("type") == "user" and isinstance(msg.get("content"), str):
                    prompts.append(msg["content"])
                content = msg.get("content") if isinstance(msg.get("content"), list) else []
                for item in content:
                    if item.get("type") == "text" and obj.get("type") == "user":
                        prompts.append(item.get("text") or "")
                    if item.get("type") != "tool_use":
                        continue
                    inp = item.get("input") or {}
                    blob = " ".join(str(inp.get(k) or "") for k in
                                    ("file_path", "command", "pattern", "path", "glob"))
                    for m in re.findall(r"([\w.\-]+\.md)", blob):
                        touched.add(m)
    except Exception:
        pass
    return prompts, touched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--whitelist", default=WHITELIST)
    ap.add_argument("--json", help="把逐条结果写成 JSON（给上线后同口径复测做对照）")
    args = ap.parse_args()

    with open(args.whitelist, encoding="utf-8") as f:
        entries = json.load(f)["entries"]

    cut = time.time() - args.days * 86400
    by_bucket = collections.defaultdict(list)
    for e in entries:
        by_bucket[e["bucket"]].append(e)

    rows = []
    for bucket, es in by_bucket.items():
        for path in glob.glob(f"{BASE}/{bucket}/*.jsonl"):
            if os.path.getmtime(path) < cut:
                continue
            prompts, touched = session_prompts_and_touches(path, bucket)
            blob = "\n".join(prompts).lower()
            if not blob:
                continue
            for e in es:
                hit = next((t for t in e["triggers"] if t.lower() in blob), None)
                if not hit:
                    continue
                rows.append({
                    "bucket": bucket, "file": e["file"], "trigger": hit,
                    "session": os.path.basename(path),
                    "reached": e["file"] in touched,
                })

    agg = collections.defaultdict(lambda: {"hit": 0, "reached": 0})
    for r in rows:
        a = agg[r["file"]]
        a["hit"] += 1
        a["reached"] += 1 if r["reached"] else 0

    print(f"# 危险操作前触达率 · 基线（近 {args.days} 天，M1 上线前口径）\n")
    if not rows:
        print("窗口内没有任何会话的 prompt 命中白名单触发词 —— 基线样本为 0。")
        print("这本身是个结论：这批触发词在最近的真实对话里没出现过，")
        print("要么词选窄了，要么那些话题最近没碰。**别把它读成「触达率 100%」**。")
    else:
        tot_hit = sum(a["hit"] for a in agg.values())
        tot_reach = sum(a["reached"] for a in agg.values())
        print(f"**总计：{tot_reach}/{tot_hit} = {tot_reach/tot_hit*100:.0f}%**"
              f"（命中触发词的会话里，该档当次被打开过的比例）\n")
        print("| 专题档 | 命中触发词的会话 | 其中读过该档 | 触达率 |")
        print("|---|---|---|---|")
        for f, a in sorted(agg.items(), key=lambda kv: -kv[1]["hit"]):
            print(f"| `{f}` | {a['hit']} | {a['reached']} | {a['reached']/a['hit']*100:.0f}% |")

    print("\n> 口径下界代理：只看 prompt 里的触发词（**低估分母**），"
          "档被读过不等于判据被用上（**高估分子**）。"
          "\n> 两个偏差方向相反 ⇒ 绝对值别当真，**只用于 M1 上线后同口径复测的对照**。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"days": args.days, "measured_at": time.strftime("%Y-%m-%d %H:%M"),
                       "rows": rows, "agg": dict(agg)}, f, ensure_ascii=False, indent=2)
        print(f"\n逐条结果 → {os.path.abspath(args.json)}")


if __name__ == "__main__":
    main()
