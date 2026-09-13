#!/usr/bin/env python3
"""again-ledger.py — 「又踩」台账（记忆管理方案 v1 第 1 步）。只读。

为什么要这张台账：
  cruna-ai 16 处、cc-service 9 处「又踩一次」——**判据在库里写着，事到临头没被读到**。
  这是整个方案的主指标（危险操作前触达率）的分母来源：不知道哪些判据反复失手，
  就无从判断替代触达机制有没有用。

  ⚠ 它**不是**「又踩 ⇒ 自动加一个 hook」的输入。那条元规则已被议会暂停，
  改成「又踩 ⇒ 登记台账，季度评估升级」。台账只登记，不处置。

触发词候选怎么取（议会一致：**只用又踩原文子串，禁自动编**）：
  取那一行里**反引号包裹的标识符**（`assets/` `String.raw` `pnpm typecheck` `-F`）。
  它们逐字来自原文，且恰好是人会在 prompt 里说出口的词。
  行里没有反引号的，候选留空 —— **宁可空着让人填，也不自动造词**。

用法：
  python3 again-ledger.py                    # markdown 台账到 stdout
  python3 again-ledger.py --json out.json    # 同时出 JSON（给 M1 白名单用）
  python3 again-ledger.py --bucket=-home-ccuser-projects-cruna-ai
"""

import argparse
import glob
import json
import os
import re

HOME = os.path.expanduser("~")
AGAIN_RE = re.compile(r"又踩")
CODE_RE = re.compile(r"`([^`\n]{2,40})`")
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
TRIGGERS_RE = re.compile(r"^triggers:\s*(.*)$", re.M)

# 这些反引号内容是通用词，当触发词会把整个库都勾出来 ⇒ 排除。
# 判据：本身不指向任何一条具体判据，命中率高到没有信息量。
TOO_GENERIC = {
    "true", "false", "null", "0", "1", "-", "=", "cat", "ls", "cd", "echo",
    "git", "python3", "bash", "sh", "rm", "cp", "mv", "curl", "grep",
}

# 三条排除规则。注意方向：**删候选是安全的，添候选才是编造** ——
# 所以宁可把可疑的删掉留空让人填，也不放进去污染触发词表。
NOISE_RE = re.compile(
    r"^[0-9a-f]{7,40}$"            # commit hash：是日志不是人会说的词
    r"|[\[\]{}\"\u201c\u201d,]"   # JSON / 数组片段：数据样例不是触发词
    r"|^[-/.]+$"                   # 纯符号
)
MIN_TRIGGER_LEN = 4                # `dk` `-F` 这类太短 ⇒ 当触发词噪音压过信号
MAX_TRIGGER_LEN = 20               # 超过这个长度的不会在 prompt 里逐字出现 ⇒ 永不触发的死条目
# markdown 表格片段 / 带强调标记 / 整条 shell 命令：都是原文没错，但不是**人会说出口的词**。
# 留着它们不会误触发（根本匹配不上），只会让白名单看起来有内容而实际是空的 —— 那更危险。
UNSPEAKABLE_RE = re.compile(r"[|*\n]|<<|\$\(|&&|\|\|")


def read(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def index_links(mem_dir):
    """主索引 + 二级索引里被链到的文件名 —— 判「有没有导航触达」。"""
    linked = set()
    for p in [os.path.join(mem_dir, "MEMORY.md")] + glob.glob(os.path.join(mem_dir, "_index-*.md")):
        for _lab, t in LINK_RE.findall(read(p)):
            t = t.split("#")[0].strip()
            if t and "*" not in t and os.path.dirname(t) in ("", "."):
                linked.add(os.path.basename(t))
    return linked


def scan_bucket(mem_dir):
    bucket = os.path.basename(os.path.dirname(mem_dir))
    linked = index_links(mem_dir)
    rows = []
    for p in sorted(glob.glob(os.path.join(mem_dir, "*.md"))):
        base = os.path.basename(p)
        if base == "MEMORY.md" or base.startswith("_index-"):
            continue
        body = read(p)
        if not AGAIN_RE.search(body):
            continue
        has_triggers = bool(TRIGGERS_RE.search(body))
        for i, line in enumerate(body.split("\n"), 1):
            if not AGAIN_RE.search(line):
                continue
            cands = [c.strip() for c in CODE_RE.findall(line)]
            cands = [c for c in cands
                     if c.lower() not in TOO_GENERIC
                     and len(c) >= MIN_TRIGGER_LEN
                     and len(c) <= MAX_TRIGGER_LEN
                     and not NOISE_RE.search(c)
                     and not UNSPEAKABLE_RE.search(c)]
            seen, uniq = set(), []
            for c in cands:
                if c not in seen:
                    seen.add(c)
                    uniq.append(c)
            rows.append({
                "bucket": bucket,
                "file": base,
                "line": i,
                "text": line.strip(),
                "trigger_candidates": uniq[:4],
                "in_index": base in linked,
                "has_triggers_frontmatter": has_triggers,
            })
    return rows


def reach(r):
    """现有触达机制 —— 只报**可观测的**，不推断。"""
    m = []
    if r["in_index"]:
        m.append("索引有链")
    if r["has_triggers_frontmatter"]:
        m.append("有 triggers:")
    return " + ".join(m) if m else "🔴 无"


def _headline(rows, by_file):
    """台账里最值钱的一行：**有导航却照样踩**的比例。

    它是方案主指标的直接证据 —— 「索引有链」是现有的触达机制，
    如果又踩的档几乎全都有链，那说明缺的不是链接，是**加载时机**。
    """
    linked = [r for r in rows if r["in_index"]]
    naked = [r for r in rows if not r["in_index"]]
    trig = [r for r in rows if r["has_triggers_frontmatter"]]
    pct = len(linked) / len(rows) * 100 if rows else 0
    return (
        f"**{len(linked)}/{len(rows)}（{pct:.0f}%）的又踩发生在「索引里有链」的档上**"
        f"，完全没被收录的只有 {len(naked)} 处，带 `triggers:` 的 {len(trig)} 处。\n"
        "> ⇒ 缺的不是链接，是**加载时机**：判据躺在被链到的档里，事到临头没人去读它。\n"
        "> 这正是 M1 触发注入要解决的那一段，也是「危险操作前触达率」这个主指标的由来。"
    )


def render(rows):
    by_file = {}
    for r in rows:
        by_file.setdefault((r["bucket"], r["file"]), []).append(r)

    out = [
        "# 「又踩」台账",
        "",
        f"共 {len(rows)} 处，分布在 {len(by_file)} 个专题档。",
        "",
        _headline(rows, by_file),
        "",
        "> 台账**只登记不处置**：「又踩 ⇒ 自动加 hook」那条元规则已被议会暂停，",
        "> 改成「又踩 ⇒ 登记，季度评估升级」。触发词候选逐字取自原文的反引号内容，",
        "> **没有自动造词**；候选为空的那几条要人来填，别让脚本编。",
        "",
        "| 桶 | 专题档 | 次数 | 现有触达 | 触发词候选（原文子串） |",
        "|---|---|---|---|---|",
    ]
    for (bucket, f), rs in sorted(by_file.items(), key=lambda kv: -len(kv[1])):
        cands = []
        for r in rs:
            for c in r["trigger_candidates"]:
                if c not in cands:
                    cands.append(c)
        short = bucket.replace("-home-ccuser-projects-", "").replace("-home-ccuser-", "")
        out.append(f"| {short} | `{f}` | {len(rs)} | {reach(rs[0])} | "
                   + (" · ".join(f"`{c}`" for c in cands[:5]) or "—（待人工填）") + " |")

    out += ["", "## 逐条原文", ""]
    for (bucket, f), rs in sorted(by_file.items()):
        short = bucket.replace("-home-ccuser-projects-", "").replace("-home-ccuser-", "")
        out.append(f"### {short} · `{f}`")
        for r in rs:
            out.append(f"- **L{r['line']}** {r['text'][:220]}")
        out.append("")
    return "\n".join(out)


def write_whitelist(rows, path, buckets):
    """派生 M1 白名单。三条约束都在这里兑现：

    · **白名单起步**：只收指定桶里、**有又踩记录**的档；
    · **禁自动编**：触发词逐字来自原文反引号，`note` 留空给人填，脚本不写概括；
    · **合并同档**：一个档一条，多次又踩的触发词并起来（注入按档去重，不按次数）。

    已存在的白名单**不覆盖**：人手填过的 note 与增删过的触发词比重跑一次更值钱。
    """
    if os.path.exists(path):
        print(f"⚠ {path} 已存在，不覆盖（人工编辑优先）。要重建请先改名。")
        return

    by_file = {}
    for r in rows:
        if r["bucket"] not in buckets or not r["trigger_candidates"]:
            continue
        key = (r["bucket"], r["file"])
        e = by_file.setdefault(key, {"bucket": r["bucket"], "file": r["file"],
                                     "triggers": [], "hits": 0, "note": ""})
        e["hits"] += 1
        for c in r["trigger_candidates"]:
            if c not in e["triggers"]:
                e["triggers"].append(c)

    data = {
        "_note": ("M1 触发注入白名单。触发词逐字取自专题档里「又踩」那一行的反引号内容，"
                  "不是自动概括的。note 留空＝还没人填，注入时只给路径。"
                  "改这个文件不需要重启会话；删掉某条即停止为它注入。"),
        "_limits": {"max_injections_per_session": 3, "max_chars_per_session": 1200},
        "entries": sorted(by_file.values(), key=lambda e: (-e["hits"], e["file"])),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"白名单 → {os.path.abspath(path)}（{len(data['entries'])} 条）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", help="只扫一个桶；值以 - 开头必须写 --bucket=-home-xxx")
    ap.add_argument("--json", help="同时把台账写成 JSON（M1 白名单的输入）")
    ap.add_argument("--whitelist", help="派生 M1 触发注入白名单（只收有原文候选的条目）")
    ap.add_argument("--whitelist-buckets", default="-home-ccuser-projects-cruna-ai,-home-ccuser-projects-cc-service",
                    help="白名单只收这些桶，逗号分隔（方案第 1 步：白名单起步，不泛化）")
    args = ap.parse_args()

    pattern = os.path.join(HOME, ".claude", "projects",
                           args.bucket if args.bucket else "*", "memory")
    rows = []
    for d in sorted(glob.glob(pattern)):
        rows.extend(scan_bucket(d))

    if args.whitelist:
        write_whitelist(rows, args.whitelist, args.whitelist_buckets.split(","))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f"JSON 台账 → {os.path.abspath(args.json)}（{len(rows)} 条）")
    print(render(rows))


if __name__ == "__main__":
    main()
