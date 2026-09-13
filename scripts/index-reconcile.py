#!/usr/bin/env python3
"""index-reconcile.py — 记忆索引「只读对账」（方案 v1 第 0 步 0c / M4，2026-09-13）

四方交叉审查一致否决「脚本重生成索引」：CC 的 auto memory 会高频写 MEMORY.md
（cruna-ai 实测 23 天 238 次、单日最多 48），后台脚本随时重写必然与在跑的会话撞车。
所以本脚本**永不写 MEMORY.md**，只出差异报告，改不改、怎么改由人定。

报出五样东西：
  1. 死链      —— 索引里链到的档不存在（gc 的死链数要先抽验：链接文本被截断、
                  `[[route:*]]` 通配写法，2026-09-13 那轮 4 条里 3 条是误报）
  2. name 异常 —— 带 frontmatter 的档缺 `name:` 或为空（**不**按「name 等于文件名」判：
                  库里的惯例是人可读标题，那样判 9 条里 7 条假阳性）
  3. 未收录    —— 目录里有档，索引里没有任何一行链到它（孤儿）
  4. 日志化存量 —— 索引行里含 commit hash / 日期戳 / ✅🆕 的清单，
                  **判据从 guard-memory-landing.py 直接导入**，保证闸与报告是同一把尺
  5. 又踩计数  —— 专题档里「又踩」出现次数，第 1 步台账的输入

用法：
  python3 index-reconcile.py                       # 默认扫 ~/.claude/projects/*/memory
  python3 index-reconcile.py <memory 目录> [...]    # 指定桶
  python3 index-reconcile.py --out report.md
"""

import argparse
import glob
import importlib.machinery
import importlib.util
import os
import re
import sys

HOME = os.path.expanduser("~")
HOOK_PATH = os.path.join(HOME, ".claude", "hooks", "guard-memory-landing.py")

LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.M)
AGAIN_RE = re.compile(r"又踩")


def load_gate_patterns():
    """从闸里借正则 —— 两份正则必然漂移，报告与闸必须共用一把尺。

    闸四还没上线时（跑的是旧版 hook）退回内置副本，并在报告里注明，
    不要让「量具缺席」表现成「一条都没命中」。
    """
    for path in (HOOK_PATH + ".new", HOOK_PATH):
        if not os.path.exists(path):
            continue
        try:
            # 显式给 loader：`.new` 后缀 spec_from_file_location 认不出是源码，loader 会是 None
            spec = importlib.util.spec_from_file_location(
                "gate", path, loader=importlib.machinery.SourceFileLoader("gate", path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if all(hasattr(mod, a) for a in ("HASH_RE", "DATE_RE", "FLAG_RE", "SCRUB_RE")):
                return mod.HASH_RE, mod.DATE_RE, mod.FLAG_RE, mod.SCRUB_RE, os.path.basename(path)
        except Exception:
            continue
    return (re.compile(r"(?<![0-9A-Za-z])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![0-9A-Za-z])"),
            re.compile(r"(?<![0-9-])(?:\d{4}-\d{2}-\d{2}|\d{2}-\d{2})(?![0-9-])"),
            re.compile(r"[✅🆕]"),
            re.compile(r"\]\([^)]*\)|<!--.*?-->"),
            "内置副本（闸里没读到，判据可能已漂移）")


HASH_RE, DATE_RE, FLAG_RE, SCRUB_RE, GATE_SRC = load_gate_patterns()


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def audit_bucket(mem_dir):
    index_path = os.path.join(mem_dir, "MEMORY.md")
    if not os.path.exists(index_path):
        return None
    text = read(index_path)
    lines = text.split("\n")

    md_files = {os.path.basename(p) for p in glob.glob(os.path.join(mem_dir, "*.md"))}
    md_files.discard("MEMORY.md")

    dead, mismatched, linked = [], [], set()
    for i, line in enumerate(lines, 1):
        for label, target in LINK_RE.findall(line):
            t = target.split("#")[0].strip()
            if not t or t.startswith(("http://", "https://", "mailto:")):
                continue
            if "*" in t:                      # `route:*` 这类通配写法不是链接，别当死链
                continue
            base = os.path.basename(t)
            resolved = os.path.normpath(os.path.join(mem_dir, t))
            if os.path.exists(resolved):
                if os.path.dirname(t) in ("", "."):
                    linked.add(base)
                # 🔴 只报「缺 name / name 为空」。曾按「name 必须等于文件名」判，
                #    9 条里 7 条是假阳性 —— 库里 name 的实际惯例是人可读标题
                #    （auth-strategy.md → 「CC 认证策略」），不是文件名 slug。
                #    照那个判据报下去，天天误报的检查等于没有检查。
                if base.endswith(".md") and not base.startswith("_index-"):
                    body = read(resolved)
                    declared = NAME_RE.search(body)
                    if body.startswith("---") and (not declared or not declared.group(1).strip().strip('"\'')):
                        mismatched.append((i, base, "name 缺失或为空"))
            else:
                dead.append((i, label[:40], t))

    # 🔴 二级索引也在收录档：只数主索引的链接，会把 cc-service 的 336 个档里 187 个报成孤儿，
    #    而那一轮逐条核出来的真孤儿只有 1 个。量具少看一层 = 在「没找到」那一侧失灵。
    for idx in glob.glob(os.path.join(mem_dir, "_index-*.md")):
        for _label, target in LINK_RE.findall(read(idx)):
            t = target.split("#")[0].strip()
            if t and "*" not in t and not t.startswith(("http", "mailto:")) \
                    and os.path.dirname(t) in ("", "."):
                linked.add(os.path.basename(t))

    orphans = sorted(md_files - linked - {f for f in md_files if f.startswith("_index-")})

    logged = []
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        probe = SCRUB_RE.sub("", line)
        hits = []
        if HASH_RE.search(probe):
            hits.append("hash")
        if DATE_RE.search(probe):
            hits.append("日期")
        if FLAG_RE.search(probe):
            hits.append("✅🆕")
        if hits:
            # 删掉这三类片段后能省多少 —— **收益下界**：真正的去日志化还要移走
            # 论证、条件、历史与接口路径，省得比这多。锚点逐字不动，所以只量这三类。
            stripped = FLAG_RE.sub("", DATE_RE.sub("", HASH_RE.sub("", line)))
            logged.append((i, "/".join(hits), len(line), line.strip()[:60], len(line) - len(stripped)))

    again = 0
    for p in glob.glob(os.path.join(mem_dir, "*.md")):
        again += len(AGAIN_RE.findall(read(p)))

    return {
        "dir": mem_dir,
        "chars": len(text),
        "lines": len([l for l in lines if l.strip()]),
        "files": len(md_files),
        "dead": dead,
        "mismatched": mismatched,
        "orphans": orphans,
        "logged": logged,
        "again": again,
    }


def render(results):
    out = [
        "# 记忆索引只读对账报告",
        "",
        f"判据来源：`{GATE_SRC}`（与落点闸共用同一把尺）",
        "> 本脚本只读。死链与孤儿是**线索不是结论** —— 按 audit-verification，",
        "> 报「不存在」之前必须逐条抽验（上一轮 4 条死链里 3 条是误报）。",
        "",
        "| 桶 | 索引字符 | 非空行 | 专题档 | 死链 | name 异常 | 未收录 | 日志化行 | 三类片段可省 | 又踩 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        bucket = os.path.basename(os.path.dirname(r["dir"]))
        out.append(
            f"| {bucket} | {r['chars']} | {r['lines']} | {r['files']} | {len(r['dead'])} | "
            f"{len(r['mismatched'])} | {len(r['orphans'])} | "
            f"{len(r['logged'])}（{len(r['logged'])/max(r['lines'],1):.0%}） | "
            f"-{sum(x[4] for x in r['logged'])}（{sum(x[4] for x in r['logged'])/max(r['chars'],1):.1%}） | {r['again']} |"
        )

    for r in results:
        bucket = os.path.basename(os.path.dirname(r["dir"]))
        detail = []
        if r["dead"]:
            detail += ["", "**疑似死链（逐条抽验后再下结论）**"] + \
                      [f"- L{i} `{t}` — 链接文本「{lab}」" for i, lab, t in r["dead"]]
        if r["mismatched"]:
            detail += ["", "**frontmatter `name` 缺失或为空**"] + \
                      [f"- L{i} `{b}` ：{d}" for i, b, d in r["mismatched"]]
        if r["orphans"]:
            detail += ["", f"**索引未收录的档（{len(r['orphans'])} 个）**", "",
                       "  " + " · ".join(f"`{o}`" for o in r["orphans"])]
        if r["logged"]:
            detail += ["", f"**日志化存量：{len(r['logged'])} 行（第 2 步去日志化的工作量）**", "",
                       "| 行 | 命中 | 字符 | 可省 | 开头 |", "|---|---|---|---|---|"] + \
                      [f"| {i} | {h} | {n} | -{d} | {s} |" for i, h, n, s, d in r["logged"][:40]]
            if len(r["logged"]) > 40:
                detail.append(f"| … | | | 另有 {len(r['logged'])-40} 行 |")
        if detail:
            out += ["", f"## {bucket}", f"`{r['dir']}`"] + detail
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="*")
    ap.add_argument("--out")
    args = ap.parse_args()

    dirs = args.dirs or sorted(glob.glob(os.path.join(HOME, ".claude", "projects", "*", "memory")))
    results = [r for r in (audit_bucket(d) for d in dirs) if r]
    if not results:
        print("没找到任何带 MEMORY.md 的记忆目录", file=sys.stderr)
        sys.exit(1)

    report = render(results)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"报告已写入 {os.path.abspath(args.out)}（{len(results)} 个桶）")
    else:
        print(report)


if __name__ == "__main__":
    main()
