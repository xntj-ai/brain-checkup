#!/usr/bin/env python3
"""split-to-l2.py — 按档名前缀把主索引的一批条目**逐字**迁进二级索引。默认干跑。

用在「主索引是一大片平铺条目、其中一类占了大半行数」的桶上。
典型：newera-video 的 166 行里 112 条是 `feedback-*`（踩坑教训），
只有 32 条是业务功能 —— 那 112 行占 74% 的行数，而盲测里它们对应的题命中 0/8。

🔴 这是**搬家不是压缩**（用户 2026-08-13 的红线）：
  条目一字不改地搬进二级索引，主索引留一行指针。信息零损失，只是从常驻层挪到调页层。
  **迁走之前必须有数据**：那一类条目在盲测里的命中率。命中率不低还迁，就是拿检索换数字。

指针行的钩子从**档名词根**里取（客观，来自文件名，不是我概括的），
按出现频次取前几个，人可以再手改。
"""

import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys

LINK_RE = re.compile(r"\]\(([\w.\-]+\.md)\)")
STOP = {"feedback", "the", "and", "for", "with", "into", "from", "not", "cant",
        "must", "only", "its", "was", "are", "you", "your", "md"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("index")
    ap.add_argument("--prefix", required=True, help="要迁走的档名前缀，如 feedback-")
    ap.add_argument("--out-name", help="二级索引文件名（默认 _index-<前缀去横线>.md）")
    ap.add_argument("--title", default="", help="二级索引的标题")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    idx = os.path.abspath(args.index)
    mem = os.path.dirname(idx)
    lines = open(idx, encoding="utf-8").read().split("\n")
    l2name = args.out_name or f"_index-{args.prefix.strip('-')}.md"
    l2path = os.path.join(mem, l2name)

    keep, moved = [], []
    for line in lines:
        m = LINK_RE.search(line)
        if m and line.lstrip().startswith("- ") and m.group(1).startswith(args.prefix):
            moved.append(line)
        else:
            keep.append(line)

    if not moved:
        print(f"没有匹配 {args.prefix}* 的条目")
        return

    # 指针行的钩子：取**被读最多的那几条的标题**，逐字。
    # 不从档名取词根 —— 那样得到的是 two/needs/same/one 这种没信息量的词；
    # 也不由我概括「这批是讲什么的」—— 那就是编造。
    # 「哪几条最常用」从会话记录里数，是客观事实。
    TITLE_RE = re.compile(r"\[([^\]]+)\]\(")
    touched = collections.Counter()
    try:
        import glob
        import time
        cut = time.time() - 90 * 86400
        bucket = os.path.basename(os.path.dirname(mem))
        for jp in glob.glob(os.path.expanduser(f"~/.claude/projects/{bucket}/*.jsonl")):
            if os.path.getmtime(jp) < cut:
                continue
            for ln in open(jp, encoding="utf-8", errors="ignore"):
                if "/memory/" not in ln:
                    continue
                for f_ in re.findall(r"/memory/([\w.\-]+\.md)", ln):
                    touched[f_] += 1
    except Exception:
        pass

    ranked = sorted(moved, key=lambda l: -touched.get(LINK_RE.search(l).group(1), 0))
    picked = []
    for line in ranked:
        t = TITLE_RE.search(line)
        if t and len(t.group(1)) <= 22:
            picked.append(t.group(1))
        if len(picked) >= 6:
            break
    hooks = " · ".join(picked) if picked else "见二级索引"

    pointer = (f"- 🔴 **{args.title or args.prefix.strip('-')} {len(moved)} 条**"
               f" → [{l2name}]({l2name})（{hooks}）")
    # 插在原来那批条目的第一条的位置，保持上下文不跳
    first = next(i for i, l in enumerate(lines) if l in moved)
    insert_at = sum(1 for l in lines[:first] if l not in moved)
    keep.insert(insert_at, pointer)

    before_c, after_c = sum(len(l) for l in lines), sum(len(l) for l in keep)
    print(f"迁走 {len(moved)} 条 → {l2name}")
    print(f"主索引 行 {len(lines)} → {len(keep)}（{len(lines)/200:.0%} → {len(keep)/200:.0%}）")
    print(f"       字符 {before_c} → {after_c}（{before_c/25000:.0%} → {after_c/25000:.0%}）")
    print(f"       水位 {max(before_c/25000, len(lines)/200):.0%} → "
          f"{max(after_c/25000, len(keep)/200):.0%}")
    print(f"\n指针行（{len(pointer)} 字符）：\n  {pointer}")

    if not args.apply:
        print("\n（干跑。确认后加 --apply）")
        return

    hook = os.path.expanduser("~/.claude/hooks/guard-memory-landing.py")
    if os.path.exists(hook):
        r = subprocess.run(["python3", hook],
                           input=json.dumps({"tool_name": "Write",
                                             "tool_input": {"file_path": idx,
                                                            "content": "\n".join(keep)}}),
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("\n🔴 闸自检没过，未写入：\n" + (r.stderr or r.stdout)[:800])
            sys.exit(1)
        print("\n✅ 闸自检通过")

    with open(l2path, "w", encoding="utf-8") as f:
        f.write("---\ntier: L2\nstatus: active\n"
                f"name: {l2name[:-3]}\n"
                f"description: {args.title or args.prefix.strip('-')}条目索引 —— "
                f"{len(moved)} 条，从主索引逐字迁出，一个字没改\n"
                "metadata:\n  node_type: memory\n  type: reference\n---\n\n"
                f"# {args.title or args.prefix.strip('-')}（{len(moved)} 条）\n\n"
                "> 从主索引**逐字**迁出，不是压缩。迁的理由：这批条目占了主索引大半行数，\n"
                "> 而盲测里它们的一跳命中率是 0 —— 常驻在目录页里没有产生检索价值。\n"
                "> 主索引留了一行指针。**这里是导航层，不是判据堆场**：新判据写专题档的定案节。\n\n"
                + "\n".join(moved) + "\n")

    shutil.copy2(idx, idx + ".pre-split")
    with open(idx, "w", encoding="utf-8") as f:
        f.write("\n".join(keep))
    print(f"已写入：{l2path}（备份 {idx}.pre-split）")


if __name__ == "__main__":
    main()
