#!/usr/bin/env python3
"""sink-body.py — 把主索引行里的**判据正文**逐字下沉到专题档（playbook A 档）。默认干跑。

索引行的实际结构：
    - 🔴 **[标题](专题档.md)（什么时候必读）**：判据正文 · 判据 · 判据
                                              ^^^^^^^^^^^^^^^^^^^^^^
冒号前是**钩子**（目录页的职责：让人知道该点开哪个档）——留下；
冒号后是**判据正文**（专题档的职责：告诉人具体怎么做）——搬走。

🔴 这是「搬家」不是「压缩」（用户 2026-08-13 定的红线）：
  正文**逐字**追加到专题档，一个字不改、不概括、不精简。
  信息零损失，可达性也不变 —— 那个档在索引里本来就有链。

三条不动的：
  · 没有链接的行（无处可搬）
  · 冒号后不足 40 字符的（短危险提示，P2 ② 允许留在目录页）
  · 链接指向别的桶或文件不存在的

🔴 不是每个桶都该下沉 —— 先看干跑的收益，再决定做不做：
  newera-video 实测收益只有 22%，因为它的索引行**本来就是关键词表**而不是「钩子+论证」。
  盲测：激进版退步 1 题、保守版 1 题、最保守版 3 题（而且退步那行整行未动 ——
  别的行变短后改变了模型在候选之间的相对权重）。⇒ 那个桶只做去日志化，不下沉。
  经验判据：**干跑收益 <30% 的桶别做**，风险大于收益。

用法：
  python3 sink-body.py <MEMORY.md>                    # 干跑，看会搬什么
  python3 sink-body.py <MEMORY.md> --apply
"""

import argparse
import os
import re
import shutil
import sys
import time

# 两种写法都要认，否则只认冒号会把破折号那种桶当成「没什么可整理的」：
#   - **[标题](档.md)（场景）**：正文      ← cruna-ai / cc-service
#   - [标题](档.md) — 正文                 ← newera-video / wecom-twin-camp
# 🔴 冒号式的 `.*?` 必须限长：不限的话它会在破折号式的行上一路回溯，
#    找到行尾某个 `**：` 才停 —— livetovideo 那行 1791 字符，hook 被算成 1476，
#    于是整行被当成「钩子本身就超 200」跳过，而它恰恰是最该处理的。
ROW_RE = re.compile(r"^(\s*-\s.{0,120}?\*\*)[：:](.+)$"
                    r"|^(\s*-\s\[[^\]]+\]\([^)]+\)[^—–]{0,40}?)\s[—–]\s(.+)$")
LINK_RE = re.compile(r"\]\(([^)]+\.md)\)")
MIN_BODY = 40
# 🔴 正文里的反引号标识符是**检索锚点**不是论证（P2 ①：命令名、标识符、报错子串逐字保留）。
#    第一版把它们跟正文一起搬走了，盲测当场抓到：`wecom-qiweapi-relogin` 那行的
#    `checkLogin` / `manualLogin` 被搬走后，模型再也认不出该点开哪个档（稳定命中→错）。
#    ⇒ 搬正文，但把锚点留在索引行。
CODE_RE = re.compile(r"`[^`\n]{2,28}`")
# 闸四会拦「新增行里含 hash/日期/状态标」。下沉让整行内容变了 ⇒ 那些
# delog 时守恒失败而保留下来的 token 会让这一行被当成新增日志行拦下。
# 它们该留（删了可达性只剩 git），所以走闸四自己的豁免出口，而不是绕闸。
# 🔴 判据从闸导入，不自己写第三份 —— 同一个坑已经踩了三次
#    （index-reconcile 写过「必须共用一把尺」，delog 和这里还是各写了一份，
#     都要求反引号包裹，于是 `HEAD 009ee98` 这种裸 hash 漏判、整桶被闸拒）。
def _load_gate():
    import importlib.machinery
    import importlib.util
    hp = os.path.expanduser("~/.claude/hooks/guard-memory-landing.py")
    spec = importlib.util.spec_from_file_location(
        "gate", hp, loader=importlib.machinery.SourceFileLoader("gate", hp))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.HASH_RE, mod.DATE_RE, mod.FLAG_RE, mod.SCRUB_RE


_H, _D, _F, SCRUB_RE = _load_gate()
LOG_RE = re.compile("|".join(f"(?:{r.pattern})" for r in (_H, _D, _F)))
EXEMPT = "<!--闸四豁免:delog 时守恒失败保留的日志 token，专题档里查不到-->"
MAX_ANCHORS = 6
# 没有反引号锚点时的兜底：留正文第一个语义段（逐字，不改写）
SEG_RE = re.compile(r"[·；;]")
FALLBACK_MAX = 60          # 第一段太长就截到这里
MIN_GAIN_RATIO = 0.30      # 留下来的相比原文省不到三成 ⇒ 这行不值得搬
LINE_LIMIT = 200
SECTION = "## 从主索引下沉的判据"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("index")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-body", type=int, default=MIN_BODY)
    args = ap.parse_args()

    idx = os.path.abspath(args.index)
    mem_dir = os.path.dirname(idx)
    lines = open(idx, encoding="utf-8").read().split("\n")

    out, moves, skipped = [], [], []
    for i, line in enumerate(lines, 1):
        m = ROW_RE.match(line)
        if not m:
            out.append(line)
            continue
        # 冒号式落在 1/2 组，破折号式落在 3/4 组
        hook, body = (m.group(1), m.group(2)) if m.group(1) is not None else (m.group(3), m.group(4))
        if body is None or len(body) < args.min_body:
            out.append(line)
            continue
        sep = "：" if m.group(1) is not None else " — "
        body = body.strip()

        # 🔴 留下的钩子里必须还有链接。有些行的链接在冒号**后面**
        #    （`- 🔴 **命名**：… [详见](brand-cctv-rename.md)`），搬走正文＝把链接一起搬走，
        #    那一行就成了指不到任何档的孤条 —— 钩子断了比索引长更糟。
        if not LINK_RE.search(hook):
            skipped.append((i, "链接在冒号后，搬走会断链", line[:60]))
            out.append(line)
            continue

        if len(hook) > LINE_LIMIT:
            # 原行就是存量超长行，留下的 hook 仍超 200 ⇒ 闸一会当「新增超长行」拦下。
            # 它区分不了「新加的长行」和「变短了的存量行」，不为这几行松闸。
            skipped.append((i, f"钩子本身 {len(hook)} 字符仍超 200", line[:60]))
            out.append(line)
            continue

        targets = LINK_RE.findall(hook)
        local = [t for t in targets
                 if os.path.dirname(t) in ("", ".")
                 and os.path.exists(os.path.join(mem_dir, os.path.basename(t)))]
        if not local:
            skipped.append((i, "无本桶链接，正文无处可搬", line[:60]))
            out.append(line)
            continue

        # 在 200 字符预算内**从正文开头按语义段尽量装**，装不下的才下沉。
        # 不再「有反引号就只留反引号、没有就只留第一段」那种二选一 ——
        # 盲测抓到两种死法：被 `；` 切掉后续钩子、以及因为有一个反引号就把中文全丢了。
        segs = [x.strip() for x in SEG_RE.split(body) if x.strip()]

        def pack(budget):
            """从正文开头按语义段往里装，返回 (装进去的段, 装不下的段)。"""
            got, used = [], 0
            for seg in segs:
                add = len(seg) + (3 if got else 0)      # ' · '
                if used + add > budget:
                    break
                got.append(seg)
                used += add
            return got, segs[len(got):]

        # 🔴 豁免注释只在真需要时才占预算。无条件扣掉 47 字符，会让绝大多数
        #    根本不需要豁免的行白白少留一半内容（asr 那行只剩 77 字符就是这么来的）。
        base = LINE_LIMIT - len(hook) - len(sep)
        kept_segs, dropped = pack(base)
        if not kept_segs:
            # 🔴 连第一段都装不下 ⇒ **截到预算内**，不是跳过。
            #    这类行正是最该处理的：clip-pipeline 那行 1791 字符，一行占索引 10%。
            if base > 30:
                kept_segs = [body[:base - 1] + "…"]
                dropped = segs
            else:
                skipped.append((i, f"钩子已占 {len(hook)} 字符，没有预算留正文", line[:60]))
                out.append(line)
                continue

        if not dropped:
            skipped.append((i, "整条正文都装得下，不用搬", line[:60]))
            out.append(line)
            continue

        new_line = hook + sep + " · ".join(kept_segs)
        if LOG_RE.search(SCRUB_RE.sub("", new_line)) and "闸四豁免" not in new_line:
            # 真的要豁免 ⇒ 这才把它的长度从预算里扣掉，重装一遍
            kept_segs, dropped = pack(base - len(EXEMPT))
            if not kept_segs and base - len(EXEMPT) > 30:
                kept_segs = [body[:base - len(EXEMPT) - 1] + "…"]
                dropped = segs
            new_line = hook + sep + " · ".join(kept_segs) + EXEMPT

        body = " · ".join(dropped)

        if len(new_line) > LINE_LIMIT:
            skipped.append((i, f"成行后 {len(new_line)} 字符仍超 200", line[:60]))
            out.append(line)
            continue

        moves.append({"line": i, "target": os.path.basename(local[0]),
                      "body": body, "hook": new_line, "anchors": len(kept_segs)})
        out.append(new_line)

    before = sum(len(l) for l in lines)
    after = sum(len(l) for l in out)
    print(f"可下沉 {len(moves)} 行 · 跳过 {len(skipped)} 行")
    print(f"字符 {before} → {after}（-{before-after}，{(before-after)/before*100:.0f}%）"
          f"  水位 {before/25000:.0%} → {after/25000:.0%}")
    print(f"🔴 行数 {len(lines)} 不变 = {len(lines)/200:.0%} ⇒ 新水位 max = "
          f"{max(after/25000, len(lines)/200):.0%}（行数成了新瓶颈，要再降得靠合并条目）")

    print(f"\n前 3 条示例：")
    for mv in moves[:3]:
        print(f"  L{mv['line']} → {mv['target']}")
        print(f"    留: {mv['hook'][:88]}")
        print(f"    搬: {mv['body'][:88]}…")

    if skipped:
        print(f"\n跳过的 {len(skipped)} 行：")
        for i, why, ctx in skipped[:8]:
            print(f"  L{i} {why} ｜ {ctx}…")

    if not args.apply:
        print("\n（干跑。确认后加 --apply）")
        return

    # ── 先过闸，再写 ──────────────────────────────────────────────
    import json
    import subprocess
    hook_path = os.path.expanduser("~/.claude/hooks/guard-memory-landing.py")
    gated = os.path.basename(idx) == "MEMORY.md" and f"{os.sep}memory{os.sep}" in idx
    if gated and os.path.exists(hook_path):
        r = subprocess.run(["python3", hook_path],
                           input=json.dumps({"tool_name": "Write",
                                             "tool_input": {"file_path": idx,
                                                            "content": "\n".join(out)}}),
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("\n🔴 闸自检没过，未写入：\n" + (r.stderr or r.stdout)[:900])
            sys.exit(1)
        print("\n✅ 闸自检通过")
    else:
        print(f"\n⚠ 自检跳过：闸只管 */memory/MEMORY.md（这次是 {idx}）")

    # 正文逐字追加到各专题档
    stamp = time.strftime("%Y-%m-%d")
    by_target = {}
    for mv in moves:
        by_target.setdefault(mv["target"], []).append(mv)

    dup = 0
    for target, mvs in by_target.items():
        p = os.path.join(mem_dir, target)
        cur = open(p, encoding="utf-8", errors="replace").read()
        # 已经逐字在档里的就不重复追加（子串精确匹配 ⇒ 只会漏判不会误判，安全的方向）
        fresh = [mv for mv in mvs if mv["body"] not in cur]
        dup += len(mvs) - len(fresh)
        if not fresh:
            continue
        mvs = fresh
        shutil.copy2(p, p + ".pre-sink")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"\n\n{SECTION}（{stamp}）\n\n"
                    "> 以下每条都是从主索引那一行**逐字**搬下来的，一个字没改。\n"
                    "> 搬的原因：目录页负责「该点开哪个档」，判据正文归这里。\n\n")
            for mv in mvs:
                f.write(f"- {mv['body']}\n")

    shutil.copy2(idx, idx + ".pre-sink")
    with open(idx, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"已写入：主索引 + {len(by_target)} 个专题档（各自 .pre-sink 备份）"
          + (f"；其中 {dup} 条正文在专题档里已逐字存在，未重复追加" if dup else ""))


if __name__ == "__main__":
    main()
