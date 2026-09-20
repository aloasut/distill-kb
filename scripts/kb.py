#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb.py — 本地知识库（~/Documents/KnowledgeBase）维护与检索 CLI。

布局:
  KB_ROOT/                源资料，用户按目录管理（只读，绝不修改）
  KB_ROOT/.kb/notes/*.md  逐篇蒸馏笔记（镜像源目录结构，路径 = <源相对路径>.md）
  KB_ROOT/.kb/state.json  增量状态（sha256 / 状态 / 时间戳），只经本脚本读写

子命令:
  scan                       增量扫描（新增/变更/删除），更新 state.json
  todo [--cap N]             列出待蒸馏文件（按加入时间升序）
  mark REL done|failed|pending [--title T] [--reason R]
  notepath REL               打印源文件对应的笔记绝对路径
  search QUERY [--top N]     BM25 检索：笔记优先，未蒸馏的文本类原文兜底
  tags                       蒸馏笔记的 标签/实体/主题 聚合索引
  notes [--tag T] [--entity E] [--topic P]   按标签/实体/主题列出笔记（汇总用）
  conflicts                  全库冲突标注对 + 已失效笔记清单（汇总前必查）
  status                     状态总览
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

HOME = Path.home()
KB_ROOT = Path(os.environ.get("KB_ROOT", str(HOME / "Documents" / "KnowledgeBase")))
KB_DIR = KB_ROOT / ".kb"
NOTES_DIR = KB_DIR / "notes"
STATE_FILE = KB_DIR / "state.json"

TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".rst", ".org", ".tex", ".csv", ".tsv",
    ".json", ".yaml", ".yml", ".html", ".htm", ".xml",
    ".py", ".js", ".ts", ".sh", ".sql", ".log",
}
IGNORE_DIR_NAMES = {".kb", ".git", "node_modules", ".obsidian", ".trash", "__pycache__"}
IGNORE_FILE_SUFFIXES = (".icloud", ".tmp", ".part", ".crdownload")
IGNORE_FILE_PREFIXES = ("~$", ".~")
RAW_READ_LIMIT = 300_000

CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
WORD_RE = re.compile(r"[a-z0-9][a-z0-9_+#-]{1,}")


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def ensure_dirs() -> None:
    KB_ROOT.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass  # 损坏则重置
    return {"version": 1, "created": now(), "last_scan": None, "files": {}}


def save_state(s: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def iter_sources():
    """yield (Path, 相对Path)，跳过隐藏目录/.kb/临时文件/iCloud占位。"""
    if not KB_ROOT.exists():
        return
    for p in sorted(KB_ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(KB_ROOT)
        if any(seg.startswith(".") or seg in IGNORE_DIR_NAMES for seg in rel.parts[:-1]):
            continue
        name = p.name
        if name.startswith(".") or name.startswith(IGNORE_FILE_PREFIXES):
            continue
        if name.endswith(IGNORE_FILE_SUFFIXES):
            continue
        yield p, rel


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def note_rel_for(rel_str: str) -> str:
    return "notes/" + rel_str + ".md"


def note_path_for(rel_str: str) -> Path:
    return NOTES_DIR / (rel_str + ".md")


def read_text(p: Path, limit: int = RAW_READ_LIMIT) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        m = re.search(r"\n---\s*\n", text[3:])
        if m:
            return text[3 + m.end():]
    return text


def parse_note_frontmatter(np: Path) -> dict:
    """解析蒸馏笔记头部 YAML 子集（单行 key: value 或 key: [a, b]）。"""
    meta = {}
    text = read_text(np, limit=8000)
    if not text.startswith("---"):
        return meta
    end = text.find("\n---", 3)
    if end < 0:
        return meta
    for line in text[3:end].splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if not k or not v:
            continue
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        else:
            meta[k] = v.strip("'\"")
    return meta


def collect_note_meta() -> list:
    """扫描全部蒸馏笔记，返回 frontmatter + src/note/title 的列表。"""
    out = []
    if NOTES_DIR.exists():
        for np in sorted(NOTES_DIR.rglob("*.md")):
            rel_note = np.relative_to(KB_DIR).as_posix()
            src = rel_note[len("notes/"):-len(".md")]
            meta = parse_note_frontmatter(np)
            body = read_text(np)
            m = re.search(r"^#\s*蒸馏笔记[:：]\s*(.+)$", body, re.M)
            if m:
                meta.setdefault("title", m.group(1).strip())
            meta["src"] = src
            meta["note"] = rel_note
            out.append(meta)
    return out


def tokenize(text: str) -> list:
    text = text.lower()
    toks = [t for t in (w.strip("-._") for w in WORD_RE.findall(text)) if len(t) >= 2]
    cjk = CJK_RE.findall(text)
    toks += [a + b for a, b in zip(cjk, cjk[1:])]
    if not toks and cjk:
        toks = cjk  # 单字兜底
    return toks


def make_snippet(text: str, qtokens: list, width: int = 200) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    low = flat.lower()
    positions = []
    for t in set(qtokens):
        i = low.find(t)
        n = 0
        while i >= 0 and n < 200:
            positions.append(i)
            n += 1
            i = low.find(t, i + 1)
    if not positions:
        return flat[:width] + ("…" if len(flat) > width else "")
    best_pos, best_score = positions[0], -1
    for p0 in positions:
        s = sum(1 for p in positions if abs(p - p0) <= width // 2)
        if s > best_score:
            best_score, best_pos = s, p0
    start = max(0, best_pos - width // 3)
    end = min(len(flat), start + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return prefix + flat[start:end] + suffix


# ---------------- 子命令 ----------------

def cmd_scan(args) -> None:
    ensure_dirs()
    st = load_state()
    files = st["files"]
    events = []
    seen = set()
    for p, rel in iter_sources():
        key = str(rel)
        seen.add(key)
        try:
            s = p.stat()
        except FileNotFoundError:
            continue
        try:
            h = sha256_of(p)
        except OSError as e:
            events.append(("读取失败", key, str(e)))
            continue
        old = files.get(key)
        if old is None:
            files[key] = {
                "sha256": h, "size": s.st_size, "mtime": int(s.st_mtime),
                "added": now(), "status": "pending",
                "note": note_rel_for(key), "title": None,
                "distilled_at": None, "reason": None,
            }
            events.append(("新增", key, fmt_size(s.st_size)))
        elif old.get("sha256") != h:
            old.update({"sha256": h, "size": s.st_size, "mtime": int(s.st_mtime),
                        "status": "pending", "distilled_at": None, "reason": None})
            events.append(("变更·需重新蒸馏", key, fmt_size(s.st_size)))
    for key in [k for k in files if k not in seen]:
        note_rel = files[key].get("note")
        files.pop(key)
        events.append(("删除", key, ""))
        if note_rel:
            np = KB_DIR / note_rel
            if np.exists():
                np.unlink()
                events.append(("删除笔记", ".kb/" + note_rel, ""))
    st["last_scan"] = now()
    save_state(st)
    done = sum(1 for v in files.values() if v["status"] == "done")
    failed = sum(1 for v in files.values() if v["status"] == "failed")
    pending = len(files) - done - failed
    print(f"扫描完成 {st['last_scan']}")
    for e in events:
        print(f"  [{e[0]}] {e[1]} {e[2]}".rstrip())
    if not events:
        print("  （无变化）")
    print(f"合计: 源文件 {len(files)} | 已蒸馏 {done} | 待处理 {pending} | 失败 {failed}")


def cmd_todo(args) -> None:
    st = load_state()
    items = [(k, v) for k, v in st["files"].items() if v["status"] != "done"]
    items.sort(key=lambda kv: kv[1].get("added", ""))
    if args.cap and args.cap > 0:
        items = items[: args.cap]
    if not items:
        print("待处理: 0")
        return
    print(f"待处理 {len(items)} 篇:")
    for k, v in items:
        extra = ""
        if v.get("reason"):
            extra = f", 上次失败: {v['reason']}"
        print(f"  - {k}  ({fmt_size(v.get('size', 0))}, 加入 {v.get('added', '?')}{extra})")


def cmd_mark(args) -> None:
    st = load_state()
    entry = st["files"].get(args.rel)
    if entry is None:
        print(f"错误: 源文件不在库中: {args.rel}（先运行 scan）")
        sys.exit(1)
    entry["status"] = args.state
    entry["distilled_at"] = now() if args.state == "done" else None
    if args.title:
        entry["title"] = args.title
    entry["reason"] = args.reason
    save_state(st)
    line = f"已标记 {args.state}: {args.rel}"
    if args.title:
        line += f" — {args.title}"
    if args.reason:
        line += f"（原因: {args.reason}）"
    print(line)


def cmd_notepath(args) -> None:
    p = note_path_for(args.rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    print(str(p))


def build_corpus(st: dict) -> list:
    docs, seen = [], set()
    if NOTES_DIR.exists():
        for np in sorted(NOTES_DIR.rglob("*.md")):
            rel_note = np.relative_to(KB_DIR).as_posix()
            src = rel_note[len("notes/"):-len(".md")]
            seen.add(src)
            docs.append({"label": "笔记", "key": src, "note": rel_note,
                         "text": strip_frontmatter(read_text(np))})
    for p, rel in iter_sources():
        key = str(rel)
        if key in seen:
            continue
        if p.suffix.lower() not in TEXT_EXTS:
            continue
        docs.append({"label": "原文·未蒸馏", "key": key, "note": None,
                     "text": read_text(p)})
    return docs


def cmd_search(args) -> None:
    ensure_dirs()
    st = load_state()
    docs = build_corpus(st)
    if not docs:
        print("知识库为空（先放入资料并 scan/蒸馏）")
        return
    q = tokenize(args.query)
    if not q:
        print("查询词无法分词")
        return
    qset = set(q)
    lists = [tokenize(d["text"]) for d in docs]
    n_docs = len(docs)
    avgdl = (sum(len(x) for x in lists) / n_docs) or 1.0
    df = {}
    for toks in lists:
        for t in set(toks):
            if t in qset:
                df[t] = df.get(t, 0) + 1
    k1, b = 1.5, 0.75
    scored = []
    for d, toks in zip(docs, lists):
        dl = len(toks) or 1
        tf = {}
        for t in toks:
            if t in qset:
                tf[t] = tf.get(t, 0) + 1
        if not tf:
            continue
        score = 0.0
        for t, f in tf.items():
            idf = math.log(1 + (n_docs - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avgdl))
        scored.append((score, d))
    if not scored:
        print(f'检索 "{args.query}" — 无命中（共 {n_docs} 篇可检索）')
        return
    scored.sort(key=lambda x: -x[0])
    hits = scored[: max(1, args.top)]
    print(f'检索 "{args.query}" — 命中 {len(hits)}/{n_docs} 篇:')
    for i, (score, d) in enumerate(hits, 1):
        loc = d["note"] or d["key"]
        print(f"\n{i}. [{d['label']}] {loc}  score={score:.2f}")
        print(f"   {make_snippet(d['text'], q)}")


def cmd_tags(args) -> None:
    metas = collect_note_meta()
    if not metas:
        print("尚无蒸馏笔记")
        return
    print(f"蒸馏笔记 {len(metas)} 篇 — 标签/实体/主题索引:")

    def pair_items(field):
        c = {}
        for m in metas:
            for t in (m.get(field) or []):
                c[t] = c.get(t, 0) + 1
        return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))

    c_topic = {}
    for m in metas:
        t = m.get("topic")
        if t:
            c_topic[t] = c_topic.get(t, 0) + 1
    for name, items in (
        ("主题", sorted(c_topic.items(), key=lambda kv: (-kv[1], kv[0]))),
        ("标签", pair_items("tags")),
        ("实体", pair_items("entities")),
    ):
        if items:
            print(f"  {name}: " + "、".join(f"{k}×{v}" for k, v in items))
    no_meta = [m["src"] for m in metas if not (m.get("tags") or m.get("topic"))]
    if no_meta:
        print(f"  ⚠ 缺少 v2 结构化字段的旧笔记 {len(no_meta)} 篇: {no_meta[:5]}")


def cmd_notes(args) -> None:
    metas = collect_note_meta()

    def match(m):
        if args.tag and args.tag not in (m.get("tags") or []):
            return False
        if args.entity and args.entity not in (m.get("entities") or []):
            return False
        if args.topic and m.get("topic") != args.topic:
            return False
        return True

    hits = [m for m in metas if match(m)]
    hits.sort(key=lambda m: str(m.get("date") or ""))
    if not hits:
        print("无匹配笔记（用 tags 查看可用标签/实体/主题）")
        return
    print(f"匹配笔记 {len(hits)} 篇（按资料日期升序）:")
    for m in hits:
        flag = ""
        if m.get("superseded_by"):
            flag = f"  ⚠已失效→被 {m['superseded_by']} 推翻"
        elif m.get("conflicts_with"):
            flag = "  ⚡与其他笔记说法冲突"
        print(f"  [{m.get('date', '?')}] [{m.get('topic', '-')}/{m.get('source_type', '-')}] "
              f"{m.get('title') or m['src']}{flag}")
        print(f"      源: {m['src']}")
        print(f"      标签: {', '.join(m.get('tags') or [])}")


def cmd_conflicts(args) -> None:
    metas = collect_note_meta()
    by_src = {m["src"]: m for m in metas}
    superseded = [(m["src"], m["superseded_by"]) for m in metas if m.get("superseded_by")]
    pairs, seen = [], set()
    for m in metas:
        for other in (m.get("conflicts_with") or []):
            key = tuple(sorted((m["src"], other)))
            if key not in seen:
                seen.add(key)
                pairs.append((m["src"], other))
    if not pairs and not superseded:
        print("无冲突标注、无失效笔记")
        return
    if superseded:
        print(f"已失效笔记 {len(superseded)} 条（现行结论勿引用）:")
        for src, by in superseded:
            t = by_src.get(src, {}).get("title", src)
            print(f"  [已失效] {src} → 被 {by} 推翻")
    if pairs:
        print(f"冲突标注 {len(pairs)} 对（汇总时并列展示，不裁决）:")
        for a, b in pairs:
            ta = by_src.get(a, {}).get("title", a)
            tb = by_src.get(b, {}).get("title", b)
            print(f"  [冲突] {a}  ↔  {b}")


def cmd_status(args) -> None:
    st = load_state()
    files = st["files"]
    done = [k for k, v in files.items() if v["status"] == "done"]
    pending = [k for k, v in files.items() if v["status"] == "pending"]
    failed = [k for k, v in files.items() if v["status"] == "failed"]
    notes = list(NOTES_DIR.rglob("*.md")) if NOTES_DIR.exists() else []
    print(f"知识库: {KB_ROOT}")
    print(f"上次扫描: {st.get('last_scan')}")
    print(f"源文件 {len(files)}: 已蒸馏 {len(done)} / 待处理 {len(pending)} / 失败 {len(failed)}")
    if failed:
        print("失败清单:")
        for k in failed[:10]:
            print(f"  [失败] {k} — {files[k].get('reason', '')}")
    if pending:
        print("待处理(前10):")
        for k in pending[:10]:
            print(f"  - {k} ({fmt_size(files[k].get('size', 0))})")
        if len(pending) > 10:
            print(f"  …共 {len(pending)} 篇")
    metas = collect_note_meta()
    n_conf = sum(1 for m in metas if m.get("conflicts_with"))
    n_sup = sum(1 for m in metas if m.get("superseded_by"))
    if n_conf or n_sup:
        print(f"知识一致性: 冲突标注 {n_conf} 篇 / 已失效 {n_sup} 篇（kb.py conflicts 查看明细）")
    print(f"笔记 {len(notes)} 篇")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="本地知识库维护与检索")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="增量扫描")
    t = sub.add_parser("todo", help="待蒸馏清单")
    t.add_argument("--cap", type=int, default=0)
    m = sub.add_parser("mark", help="标记蒸馏结果")
    m.add_argument("rel")
    m.add_argument("state", choices=["done", "failed", "pending"])
    m.add_argument("--title", default=None)
    m.add_argument("--reason", default=None)
    n = sub.add_parser("notepath", help="笔记路径")
    n.add_argument("rel")
    s = sub.add_parser("search", help="BM25 检索")
    s.add_argument("query")
    s.add_argument("--top", type=int, default=8)
    sub.add_parser("tags", help="标签/实体/主题索引")
    n2 = sub.add_parser("notes", help="按标签/实体/主题列笔记（汇总用）")
    n2.add_argument("--tag", default=None)
    n2.add_argument("--entity", default=None)
    n2.add_argument("--topic", default=None)
    sub.add_parser("conflicts", help="冲突标注对 + 已失效笔记清单")
    sub.add_parser("status", help="状态总览")
    args = ap.parse_args(argv)
    {"scan": cmd_scan, "todo": cmd_todo, "mark": cmd_mark,
     "notepath": cmd_notepath, "search": cmd_search, "status": cmd_status,
     "tags": cmd_tags, "notes": cmd_notes, "conflicts": cmd_conflicts}[args.cmd](args)


if __name__ == "__main__":
    main()
