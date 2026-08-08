#!/usr/bin/env python3
"""对 salvage 候选逐条定向复核 —— 粗筛的命中率是线索，这一步才是判据。

对每条候选取「最有判别力的短语」（最长中文串 / 最长反引号代码 / 特有数字），
在当前全库逐文件精确搜，打印命中文件名。零命中的才是真丢失。
"""
import json, re, os, glob

CLAUDE = os.path.expanduser('~/.claude')
CJK = r'[一-鿿]'

files = {}
for pat in (os.path.join(CLAUDE, 'projects', '*', 'memory', '**', '*.md'),
            os.path.join(CLAUDE, 'rules', '*.md'),
            os.path.join(CLAUDE, 'skills', '*', 'SKILL.md')):
    for f in glob.glob(pat, recursive=True):
        try:
            files[os.path.relpath(f, CLAUDE)] = open(f, encoding='utf-8', errors='replace').read()
        except OSError:
            pass

cands = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    'salvage-candidates.json'), encoding='utf-8'))

def probes(line):
    """判别力从高到低：反引号代码 > 最长中文串 > 罕见数字组合"""
    p = []
    p += sorted(re.findall(r'`([^`]{4,40})`', line), key=len, reverse=True)[:2]
    runs = sorted(re.findall(CJK + r'{5,}', line), key=len, reverse=True)
    p += runs[:3]
    p += re.findall(r'\b\d[\d.]*\s*(?:Mbps|ms|条|台|倍)\b', line)[:2]
    return [x for x in p if len(x) >= 4][:5]

seen = set()
print(f'全库 {len(files)} 个文件\n')
lost, alive = [], []
for c in cands:
    key = c['line'].strip()[:60]
    if key in seen:
        continue
    seen.add(key)
    ps = probes(c['line'])
    if not ps:
        continue
    hits = {}
    for p in ps:
        for name, txt in files.items():
            if p in txt:
                hits.setdefault(p, []).append(name)
    if hits:
        alive.append((c, hits))
    else:
        lost.append((c, ps))

print(f'=== 🔴 零命中（真丢失候选）{len(lost)} 条 ===\n')
for c, ps in lost:
    print(f"[第{c['round']}轮] {c['line'].strip()[:170]}")
    print(f"    探针: {ps}\n")

print(f'=== ✅ 仍在库里（粗筛误报）{len(alive)} 条 ===\n')
for c, hits in alive:
    where = sorted({n for v in hits.values() for n in v})[:3]
    print(f"[第{c['round']}轮] {c['line'].strip()[:90]}")
    print(f"    命中: {list(hits)[:2]} → {where}\n")
