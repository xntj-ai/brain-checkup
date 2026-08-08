# brain-checkup

**Not brain-wash — brain-checkup.** A Claude Code skill that gives your AI assistant's long-term memory a physical, then walks you through fixing what it finds.

`v1.0.0` · [中文说明](README.zh.md)

Brainwashing overwrites what you believe. A checkup hands you the findings and lets *you* decide. That distinction is the whole design: raw notes are an **evidence layer that is never rewritten**, the default action is **annotate, not act**, and every judgment call goes back to you.

---

## Why this exists

Long-lived AI memory rots in ways you don't notice:

- **The index silently truncates.** Past a size limit the tail is simply not loaded. Nothing errors. You just find your assistant "forgetting" things it definitely knew.
- **Compression eats the evidence.** Round after round of "tidying up" rewrites the *previous* summary rather than the source. Deviations compound. There is published work showing repeated self-summarization drives utility *below* the no-memory baseline.
- **Facts go stale without a sound.** Ports, paths, versions, service names — all written down once, never re-checked, and confidently wrong later.
- **Your checkers lie to you.** This is the big one. See below.

## The finding that mattered most

On the first full-library run against a real 39-library, 1000+ file memory store, the diagnostics were wrong **six times out of seven**:

| Report claimed | Reality | Why the tool was wrong |
|---|---|---|
| 66 orphan notes | **10** | Only counted markdown links and wikilinks; the library mostly used backticked filenames and bare words |
| 38 stale entries | **0** | `last_verified` was a batch-stamped field nobody ever updated — it measured the stamping date, not staleness |
| 7 facts mismatched | **0** | The probes ran Windows paths on a Linux host. The probe syntax had no field for *which machine to check on* |
| 50 files deleted | **8** | The archive lives *outside* the memory root and wasn't scanned; 40 were relocations |
| 5 cross-library refs "not dead links" | **all 5 were dead** | The only *under*-report: after a whole library was archived away, every relative path into it broke — and the checker waved them through |
| 19 oversized-index warnings | **1** | 18 were "line exceeds 200 chars" — and blind testing showed dense lines are the *best-performing* pattern |

**A check that cries wolf daily is not a check.** Acting on those numbers would have meant hours of work on problems that did not exist — and, in the one under-report, missing five real dead links.

So the first rule of this skill is: **suspect the instrument before you suspect the memory.**

## What it actually does

Five conversational steps, with a stop for you between each:

1. **Learn how you work** *(never skipped)* — before judging anything stale, establish *which machine* it describes. Most "expired" memory is just describing a different box.
2. **Checkup** — read-only scan, after taking a snapshot you can roll back to
3. **Report in plain language** — findings sorted into safe / reversible / needs-your-call
4. **Fix one thing at a time**, narrating as it goes
5. **Blind-test the result** — the only step that answers "did this actually help?"

Depth is chosen up front: *checkup only* / *light* / *deep* / *targeted*. Default is checkup — diagnose first, then decide.

### The blind test caught a regression

Four uninformed evaluators, eight questions drawn from real past incidents, index-only (no article bodies). Before vs. after tidying:

| | Hits | High-confidence, one hop |
|---|---|---|
| Before | 8/8 | **8** |
| After (first attempt) | 8/8 | **7** ← regression |
| After (fixed) | 8/8 | **8** — with a 3.4 KB smaller index |

The regression: a pointer line had compressed the user-facing *symptom* ("the cover photos all look like the same stranger") into a technical token (`FACE_BLOCK`). People search by symptom. Without the blind test, that would have shipped as an improvement.

## Install

```bash
git clone https://github.com/xntj-ai/brain-checkup ~/.claude/skills/brain-checkup
```

Then ask Claude Code: **"给我的记忆做个体检"** / *"run a checkup on my memory"*.

Scripts run standalone too:

```bash
python3 scripts/memory-gc.py      --base ~/.claude/projects      # structural checkup (read-only)
python3 scripts/verify-facts.py   --base ~/.claude/projects      # do the written-down facts still hold
python3 scripts/salvage-audit.py  --archive-root ~/path/to/archive  # what did past compressions lose
python3 scripts/salvage-verify.py                                # verify salvage candidates one by one
```

Everything is **read-only**. No script ever rewrites a memory file.

## ⚠️ Built for Claude Code

This skill is written against Claude Code's memory system and leans on its specific shape: the `~/.claude/projects/<bucket>/memory/` layout, bucket names derived from cwd (which start with `-` and break a lot of tooling), the ~25600-byte silent-truncation limit, `tier`/`status`/`last_verified` frontmatter, `<!-- vrfy: -->` probes, and the `SessionStart` hook protocol.

**Using another agent (Cursor, Codex, your own)? Fork it.** You'll need to swap the directory layout, the size limit, the frontmatter field names, and the hook contract.

**The methodology transfers as-is** and is the more valuable half: asymmetric burden of proof · suspect the instrument first · attribute the environment before declaring staleness · never tidy the output of tidying · archiving ≠ removing from search · blind-test your results.

## License

MIT © [xntj-ai](https://github.com/xntj-ai)
