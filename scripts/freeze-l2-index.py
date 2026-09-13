#!/usr/bin/env python3
"""给二级索引加冻结声明（方案 v1 第 0 步 0e）。默认干跑，--apply 才写。

幂等：已有声明的跳过。只加注释，一个字的存量内容都不改 ——
L2 的处置方式是「冻结快照 → 迁移映射 → 分批逐字回流」，此刻任何内容改动都不合法。
"""
import glob
import io
import os
import sys

BANNER = """> 🔒 **冻结中**（记忆管理方案 v1 · L2）：**新判据别再往这里写**，落点是专题档的「定案结论」节。
> 导航条目（新增一条链接行）照常加。存量逐字不动，将来分批回流，不做二次压缩。
> `PreToolUse` 闸五只出声不阻断：往已有行续判据会被点名，新增整行只提醒一句。
"""

APPLY = "--apply" in sys.argv
roots = sorted(glob.glob(os.path.expanduser("~/.claude/projects/*/memory/_index-*.md")))

# 只动方案点名的两桶。其余桶（wecom-twin-camp / newera-video …）还没做过体检，
# 对它们宣布冻结＝替没诊断过的桶做决定；闸五对所有 _index-*.md 都只出声，行为不受影响。
IN_SCOPE = ("-home-ccuser-projects-cruna-ai", "-home-ccuser-projects-cc-service")

for p in roots:
    if p.endswith(".bak") or ".bak-" in p:
        continue
    # 🔴 按**桶名整段**比，不是子串：`cc-service` 用 `in` 会把独立的
    #    `cc-service-wecom` 桶一起吞掉（本脚本初版就误写了那个桶，已 git restore 回退）。
    bucket = p.split(os.sep + "projects" + os.sep)[1].split(os.sep)[0]
    if bucket not in IN_SCOPE:
        print(f"  ⏭  不在方案范围，跳过  {bucket}/{os.path.basename(p)}")
        continue
    src = io.open(p, encoding="utf-8").read()
    if "冻结中" in src:
        print(f"  ⚪ 已有声明，跳过  {os.path.basename(p)}")
        continue

    lines = src.split("\n")
    # 找 frontmatter 结束位置
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    # frontmatter 之后的第一个 `# ` 标题之后插；没有标题就紧跟 frontmatter
    ins = start
    for i in range(start, min(start + 8, len(lines))):
        if lines[i].startswith("# "):
            ins = i + 1
            break

    ctx = lines[max(0, ins - 1)][:60] if ins else "(文件开头)"
    print(f"  {'✍️' if APPLY else '👀'} {os.path.basename(p):38} 插在第 {ins+1} 行之前，其上一行：{ctx}")

    if APPLY:
        new = lines[:ins] + ["", BANNER.rstrip("\n")] + lines[ins:]
        io.open(p, "w", encoding="utf-8").write("\n".join(new))

if not APPLY:
    print("\n（干跑。确认插入点无误后加 --apply）")
