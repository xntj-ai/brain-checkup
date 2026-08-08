#!/usr/bin/env python3
"""压缩打捞审计（全库版）—— 从 git 历史里找回被压掉且从未下沉的判据。

两条损失路径，都查：
  A 索引删行：MEMORY.md / _index-*.md / MEMORY-archive.md 历史上被删掉的行，
              今天在全库还找不找得到
  B 整档消失：memory/*.md 曾存在、被删/改名，且当前磁盘上没有同名文件

判据（非对称举证）：
  「还在」= 一次命中即可（证有易）·「丢了」= 所有词元零命中（证无难）
  ⇒ 本脚本只产出**候选**，不改任何文件；候选必须再跑 salvage-verify.py 逐条复核，
    复核零命中之后还要人读一眼目标专题文件才能定案。
    实测：粗筛 32 条 → 复核后零命中 20 条 → 读文件后真丢失只有 2 条。

⚠ 不靠 commit message 认「压缩」——早期轮次的信息里未必带这个词，
   且 CC 项目目录名以 `-` 开头，一律用 Python，不用 shell 循环。

用法：python3 salvage-audit.py [--base ~/.claude] [--only <项目名片段>] [--min-lines 3]
"""
import subprocess, re, os, glob, json, argparse, collections

CJK = r'[一-鿿]'
IDX_PATTERNS = ('MEMORY.md', 'MEMORY-archive.md', '_index-')

# 🔴 归档区常在记忆库**之外**，判「文件没了」前必须连它一起查 —— 否则「搬去归档」会被
# 全报成「已删除」（实测 50 个里 40 个是这么来的假阳性）。
# **默认为空 = 只查记忆库内部**；你的归档在别处就用 --archive-root 指进来（可重复），
# 或直接改这里。这是本 skill 唯一一处需要按你的目录结构配置的地方。
ARCHIVE_ROOTS = []


def git(base, *args):
    r = subprocess.run(['git', '-C', base] + list(args),
                       capture_output=True, text=True)
    return r.stdout


def build_corpus(base):
    """当前全库正文 —— 「今天还找不找得到」的唯一判据面。"""
    parts = []
    for pat in (os.path.join(base, 'projects', '*', 'memory', '**', '*.md'),
                os.path.join(base, 'rules', '*.md'),
                os.path.join(base, 'skills', '*', 'SKILL.md'),
                os.path.join(base, 'skills', '*', 'references', '*.md')):
        for f in glob.glob(pat, recursive=True):
            try:
                parts.append(open(f, encoding='utf-8', errors='replace').read())
            except OSError:
                pass
    return '\n'.join(parts)


def tokens(line):
    """判别性词元。太短的中文（≤3 字）判别力不够，「服务器」这种通用词会假命中。"""
    t = set()
    t |= set(re.findall(r'`([^`]{3,60})`', line))
    for run in re.findall(CJK + r'{4,}', line):
        t.add(run)
        if len(run) > 8:                    # 长串再切 6-gram，防整句改写被误判成丢失
            t |= {run[i:i + 6] for i in range(0, len(run) - 5, 3)}
    t |= set(re.findall(r'\b[A-Za-z_][A-Za-z0-9_.-]{4,40}\b', line))
    t |= set(re.findall(r'\b\d[\d.,]*\s*(?:ms|s|h|KB|MB|GB|%|条|台|个|次)\b', line))
    return {x.strip() for x in t if len(x.strip()) >= 4}


def audit_index_deletions(base, corpus, only, min_lines):
    """A 路径：索引历史删行。"""
    cands = []
    tracked = git(base, 'ls-files', 'projects/').splitlines()
    idx_files = sorted({p for p in tracked
                        if '/memory/' in p and any(k in os.path.basename(p) for k in IDX_PATTERNS)})
    if only:
        idx_files = [p for p in idx_files if only in p]
    for path in idx_files:
        shas = git(base, 'log', '--follow', '--format=%H', '--', path).split()
        for sha in shas:
            old = git(base, 'show', f'{sha}^:{path}')
            new = git(base, 'show', f'{sha}:{path}')
            if not old or not new:
                continue
            new_lines = set(new.split('\n'))
            removed = [l for l in old.split('\n')
                       if l.strip() and l not in new_lines and not l.lstrip().startswith('#')]
            if len(removed) < min_lines:      # 零星改字不算压缩事件
                continue
            for line in removed:
                tk = tokens(line)
                if not tk:
                    continue
                hit = sum(1 for x in tk if x in corpus)
                if hit / len(tk) < 0.34:
                    cands.append({'kind': 'index-line', 'path': path, 'sha': sha[:7],
                                  'hit': hit, 'tot': len(tk), 'line': line})
    return cands


def audit_deleted_files(base, only):
    """B 路径：整个专题文件消失且当前磁盘无同名文件。"""
    out = git(base, 'log', '--diff-filter=D', '--name-only', '--format=%H', '--', 'projects/')
    deleted, sha = collections.OrderedDict(), None
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if re.fullmatch(r'[0-9a-f]{40}', ln):
            sha = ln
        elif '/memory/' in ln and ln.endswith('.md'):
            deleted.setdefault(ln, sha)
    # 当前所有还能找到的文件名（换目录/改名也算还在）。
    # 🔴 归档区**可能在 `~/.claude` 之外**（例：`~/projects/<某项目>/_archive/`）：
    # 只扫 `.claude` 内部会把「搬去归档」全报成「已删除」—— 实测 50 个里 40 个是这么来的假阳性，
    # 正是 D 节那张表里的「换了位置」形态。用 --archive-root 把你的归档目录指进来。
    alive = set()
    for root in ARCHIVE_ROOTS + [os.path.join(base, 'projects')]:
        root = os.path.expanduser(root)
        if os.path.isdir(root):
            alive |= {os.path.basename(f)
                      for f in glob.glob(os.path.join(root, '**', '*.md'), recursive=True)}
    rows = []
    for path, sha in deleted.items():
        if only and only not in path:
            continue
        if os.path.basename(path) not in alive and not os.path.exists(os.path.join(base, path)):
            rows.append({'kind': 'deleted-file', 'path': path, 'sha': (sha or '')[:7]})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default=os.path.expanduser('~/.claude'))
    ap.add_argument('--only', default=None,
                    help='只处理路径含该片段的项目。⚠ 值以 - 开头必须写成 --only=-home-xxx')
    ap.add_argument('--min-lines', type=int, default=3)
    ap.add_argument('--archive-root', action='append', default=[],
                    help='归档目录（可重复）。归档常在记忆库之外，不指进来会把「搬去归档」误报成「已删除」')
    ap.add_argument('--out', default='salvage-candidates.json')
    a = ap.parse_args()
    base = os.path.expanduser(a.base)

    corpus = build_corpus(base)
    print(f'语料：当前全库 {len(corpus)/1024/1024:.1f} MB\n')

    print('=== A 索引删行 ===')
    ci = audit_index_deletions(base, corpus, a.only, a.min_lines)
    by_proj = collections.Counter(c['path'].split('/')[1] for c in ci)
    for p, n in by_proj.most_common():
        print(f'  {n:4d} 条候选  {p}')
    print(f'  小计 {len(ci)} 条\n')

    print('=== B 整档消失（当前全库无同名文件）===')
    globals()['ARCHIVE_ROOTS'] = ARCHIVE_ROOTS + list(a.archive_root)
    cf = audit_deleted_files(base, a.only)
    for r in cf:
        print(f"  {r['sha']}  {r['path']}")
    print(f'  小计 {len(cf)} 个\n')

    json.dump(ci + cf, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'候选共 {len(ci)+len(cf)} 条 → {a.out}')
    print('⚠ 候选不是结论：必须跑 salvage-verify.py 逐条复核，再人读目标文件定案。')


if __name__ == '__main__':
    main()
