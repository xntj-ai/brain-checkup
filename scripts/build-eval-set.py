#!/usr/bin/env python3
"""build-eval-set.py — 从历史会话里挖「一跳命中」的真题与标准答案。只读。

方案 v1 第 5 节要的是「一跳高信心命中」不退步，**题目来自历史真题**，不是我编的。
真题与标准答案都在 jsonl 里现成躺着：

  用户问了一句话  →  CC 随后打开了某个专题档  →  这就是一组 (题目, 正确答案)

标准答案是**当时那个会话实际 Read 的档**，不是我判断「应该读哪个」——
后者会把我的偏见变成评分标准，那样测出来的只是「索引像不像我想的样子」。

配对判据（宁缺毋滥，三条都要满足）：
  · 用户 prompt 之后、下一条用户 prompt 之前，有 Read/Grep 碰过本桶的专题档；
  · 只取**第一个**被碰的专题档 —— 「一跳」的定义就是第一跳；
  · prompt 长度 12–400 字符：太短没信息、太长通常是粘贴的日志而不是提问。

用法：
  python3 build-eval-set.py --bucket=-home-ccuser-projects-cruna-ai --n 50 --out eval.json
"""

import argparse
import glob
import json
import os
import random
import re
import time

BASE = os.path.expanduser("~/.claude/projects")


def extract(path, bucket):
    """把一个会话切成 (prompt, 第一个被打开的专题档) 配对。"""
    pairs = []
    pending = None            # 当前等待「第一跳」的 prompt
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
                content = msg.get("content")

                if obj.get("type") == "user":
                    text = content if isinstance(content, str) else " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
                    text = (text or "").strip()
                    # 工具结果也以 user 身份回来，那不是提问
                    if isinstance(content, list) and any(
                            isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                        continue
                    if text.startswith("<") or "system-reminder" in text[:200]:
                        continue
                    # has_anchor：prompt 里有没有**可检索的实体**（英文标识符 / 反引号 / 路径）。
                    # 客观可判定，不掺我对「这题算不算好题」的判断。
                    # 用途是分层报告：「还有没做完的吗」这类泛问题没有唯一正确档，
                    # 标准答案是偶然的 —— 但 A/B 用同一批题，这种噪音共模、会在前后对比里抵消，
                    # 所以**不删题**（删就是把我的偏见变成评分标准），只标记出来分开看。
                    has_anchor = bool(re.search(r"`[^`]+`|[A-Za-z][A-Za-z0-9_.\-]{3,}|/\w+/", text))
                    pending = {"prompt": text, "answer": None, "has_anchor": has_anchor}
                    if 12 <= len(text) <= 400:
                        pairs.append(pending)
                    else:
                        pending = None
                    continue

                if pending is None or pending["answer"] is not None:
                    continue
                for item in (content if isinstance(content, list) else []):
                    if item.get("type") != "tool_use":
                        continue
                    inp = item.get("input") or {}
                    blob = " ".join(str(inp.get(k) or "") for k in
                                    ("file_path", "command", "pattern", "path", "glob"))
                    if f"{bucket}/memory/" not in blob:
                        continue
                    # 🔴 必须绑定**本桶**路径再取档名：blob 是多个字段拼起来的，
                    #    只匹配 `/memory/xxx.md` 会把命令里提到的**跨桶**档名当成本桶答案
                    #    （实测 cc-service 的题库里混进了 cruna-* 的档，那些题必然答不对）。
                    m = re.search(re.escape(bucket) + r"/memory/([\w.\-]+\.md)", blob)
                    if m and m.group(1) != "MEMORY.md" and not m.group(1).startswith("_index-"):
                        pending["answer"] = m.group(1)
                        break
    except Exception:
        pass
    return [p for p in pairs if p["answer"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True, help="值以 - 开头必须写 --bucket=-home-xxx")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=20260913, help="固定随机种子 ⇒ 留出集可重复跑")
    args = ap.parse_args()

    cut = time.time() - args.days * 86400
    allp = []
    for path in sorted(glob.glob(f"{BASE}/{args.bucket}/*.jsonl")):
        if os.path.getmtime(path) < cut:
            continue
        for p in extract(path, args.bucket):
            p["session"] = os.path.basename(path)
            allp.append(p)

    # 🔴 剔除「标准答案档如今已不存在」的题：历史上读过、后来被删或改名，
    #    索引里自然也不会有它 ⇒ 这题无解，留着只会当共模噪音压低分数。
    #    这是客观事实（文件在不在）不是主观判断，可以放心删。
    mem_dir = os.path.join(BASE, args.bucket, "memory")
    before_n = len(allp)
    allp = [p_ for p_ in allp if os.path.exists(os.path.join(mem_dir, p_["answer"]))]
    gone = before_n - len(allp)

    # 去重：同一句话问过多次只留一条，否则高频问法会主导分数
    seen, uniq = set(), []
    for p in allp:
        k = p["prompt"][:80]
        if k not in seen:
            seen.add(k)
            uniq.append(p)

    random.Random(args.seed).shuffle(uniq)
    picked = uniq[:args.n]

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"bucket": args.bucket, "built_at": time.strftime("%Y-%m-%d %H:%M"),
                   "seed": args.seed, "total_found": len(uniq), "items": picked},
                  f, ensure_ascii=False, indent=2)

    print(f"共挖到 {len(allp)+gone} 组配对（剔除 {gone} 组答案档已不存在的），"
          f"去重后 {len(uniq)} 组，取 {len(picked)} 题 → {os.path.abspath(args.out)}")
    if picked:
        dist = {}
        for p in picked:
            dist[p["answer"]] = dist.get(p["answer"], 0) + 1
        print("\n答案分布（前 10）：")
        for f_, n in sorted(dist.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {n:3}  {f_}")
        print(f"\n样例题：{picked[0]['prompt'][:90]}…")
        print(f"标准答案：{picked[0]['answer']}")


if __name__ == "__main__":
    main()
