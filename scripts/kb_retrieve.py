#!/usr/bin/env python3
"""知识库检索工具：query → top-K 相关 cards 的完整内容。

这是个**纯检索工具**，不调 LLM 生成回答。
设计目的：被 Skill（Claude Code / Codex / Hermes / OpenClaw）调用，
让平台 LLM 拿到检索结果后自己综合答案。

Usage:
    python scripts/kb_retrieve.py "什么是 Fair Value Gap" --layer school --schools ICT SMC
    python scripts/kb_retrieve.py "FVG 怎么入场" --layer school --schools ICT SMC --top-k 5
    python scripts/kb_retrieve.py "ICT killzone" --kb "materials/Education - ICT/knowledge_base"
    python scripts/kb_retrieve.py "BTC reversal" --layer school --schools ICT SMC --type case
    python scripts/kb_retrieve.py "Order Block" --layer school --school ICT
    python scripts/kb_retrieve.py "market structure" --layer school --schools ICT SMC
    python scripts/kb_retrieve.py "Wyckoff spring" --layer school --schools Wyckoff
    python scripts/kb_retrieve.py "FVG" --layer school --schools ICT SMC
    python scripts/kb_retrieve.py "FVG" --layer evidence --sources Teach-Wuyuan
    python scripts/kb_retrieve.py --layer school --list-schools
    python scripts/kb_retrieve.py "..." --format json   # JSON 输出便于程序解析
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


THIS_DIR = Path(__file__).resolve().parent        # skills/OpenMobius-skill/scripts/
SKILL_DIR = THIS_DIR.parent                       # skills/OpenMobius-skill/
sys.path.insert(0, str(THIS_DIR))                 # 让 _lib 包可见

from _lib.build_lock import (  # noqa: E402
    BuildLockUnavailable,
    knowledge_base_read_session,
)
from _lib.retriever import (  # noqa: E402
    LAYER_COLLECTIONS,
    SEARCH_MODES,
    ReadOnlySearchModeError,
    RetrievalScopeError,
    Retriever,
    assert_readable_generation,
    build_where_filter,
    normalize_filter_values,
    resolve_search_mode,
)


log = logging.getLogger("kb_retrieve")


# 默认知识库（如果用户不指定 --kb）
DEFAULT_KB = SKILL_DIR / "knowledge_base"


def get_embedder(provider: str):
    """Load embedding dependencies only when an actual query will run."""
    from _lib.embedder import get_embedder as factory  # noqa: PLC0415

    return factory(provider)


def _projected_school_counts(kb_dir: Path, layer: str) -> dict[str, int]:
    """Derive v2 School counts without opening Chroma.

    Older index manifests do not persist per-School counts. Rebuilding the
    deterministic projections is still read-only and lets capability
    discovery work when an agent mounts installed Skill resources read-only.
    """
    from _lib.knowledge_v2 import build_v2_records  # noqa: PLC0415

    result = build_v2_records(kb_dir)
    records = (
        result.school_records if layer == "school" else result.evidence_records
    )
    counts: dict[str, int] = {}
    for record in records:
        school = (record.get("metadata") or {}).get("school")
        if not isinstance(school, str) or not school.strip():
            continue
        school = school.strip()
        counts[school] = counts.get(school, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[0].casefold()))


def _validate_school_counts(raw_counts, *, collection: str) -> dict[str, int]:
    """Validate a manifest School-count map before presenting it as indexed."""
    if not isinstance(raw_counts, dict):
        raise RetrievalScopeError(
            f"索引 manifest 的 {collection} School 计数格式无效"
        )
    counts: dict[str, int] = {}
    for raw_name, raw_count in raw_counts.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise RetrievalScopeError(
                f"索引 manifest 的 {collection} 包含空 School 名称"
            )
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < 1
        ):
            raise RetrievalScopeError(
                f"索引 manifest 的 {collection} School 计数无效: "
                f"{raw_name}={raw_count!r}"
            )
        counts[raw_name.strip()] = raw_count
    return dict(sorted(counts.items(), key=lambda item: item[0].casefold()))


def _enrich_school_counts(kb_dir: Path, counts: dict[str, int]) -> list[dict]:
    """Attach declared aliases and capability fields to actual index counts."""
    try:
        payload = json.loads((kb_dir / "schools.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        payload = {}
    registry = {
        entry["name"].strip(): entry
        for entry in (payload.get("schools") or [])
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and entry["name"].strip()
    }
    inventory: list[dict] = []
    for name, count in counts.items():
        entry = registry.get(name, {})
        item = {
            "name": name,
            "count": count,
            "available_in_layer": True,
            "aliases": entry.get("aliases") or [],
        }
        for field in (
            "id",
            "kind",
            "availability",
            "knowledge_qna",
            "native_market_analyzer",
        ):
            if field in entry:
                item[field] = entry[field]
        inventory.append(item)
    return inventory


def read_only_school_inventory(kb_dir: Path, layer: str) -> list[dict] | None:
    """Read a collection-backed School inventory without opening Chroma.

    New manifests carry exact per-School counts. For an older v2 manifest,
    derive the same deterministic projection counts and require their total to
    match the installed collection count. ``None`` preserves the legacy
    Chroma path when no usable manifest exists (including canonical indexes).
    """
    if layer not in {"school", "evidence"}:
        return None
    manifest_path = kb_dir / "_index" / "index_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise RetrievalScopeError(
            f"无法读取索引 manifest: {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise RetrievalScopeError(f"索引 manifest 格式无效: {manifest_path}")

    collection_name = LAYER_COLLECTIONS[layer]
    details = (manifest.get("collections") or {}).get(collection_name)
    if not isinstance(details, dict):
        raise RetrievalScopeError(
            f"索引 manifest 未声明 collection '{collection_name}'"
        )
    if details.get("created") is not True:
        raise RetrievalScopeError(
            f"索引 collection '{collection_name}' 尚未创建"
        )
    expected_count = details.get("count")
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 1
    ):
        raise RetrievalScopeError(
            f"索引 manifest 的 {collection_name} 总计数无效: "
            f"{expected_count!r}"
        )

    metadata_counts = details.get("metadata_value_counts")
    if metadata_counts is None:
        try:
            raw_counts = _projected_school_counts(kb_dir, layer)
        except (OSError, ValueError, RuntimeError) as exc:
            raise RetrievalScopeError(
                f"无法从只读知识投影计算 School 清单: {exc}"
            ) from exc
    elif isinstance(metadata_counts, dict):
        raw_counts = metadata_counts.get("school")
    else:
        raw_counts = None
    counts = _validate_school_counts(raw_counts, collection=collection_name)
    actual_count = sum(counts.values())
    if actual_count != expected_count:
        raise RetrievalScopeError(
            f"索引 manifest 的 {collection_name} School 计数不一致: "
            f"{actual_count} != {expected_count}"
        )
    return _enrich_school_counts(kb_dir, counts)


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser separately so scope semantics are testable."""
    p = argparse.ArgumentParser(
        description="检索知识库 top-K 卡片（不调 LLM，留给平台 LLM 综合）",
    )
    p.add_argument(
        "query", nargs="?",
        help="自然语言查询（--list-schools/--explain-scope 时可省略）",
    )
    p.add_argument(
        "-k", "--top-k", type=int, default=5,
        help="返回 top-K 条（默认 5）",
    )
    p.add_argument(
        "--kb", default=None,
        help=f"知识库目录（默认: {DEFAULT_KB.relative_to(SKILL_DIR)}）",
    )
    p.add_argument(
        "--embedder", default="local",
        choices=["local", "openai"],
        help="embedding provider（默认 local nomic-embed）",
    )
    p.add_argument(
        "--search-mode", default="auto", choices=SEARCH_MODES,
        help=(
            "检索模式：auto（canonical=semantic，school/evidence=hybrid）/ "
            "hybrid / semantic / lexical"
        ),
    )
    p.add_argument(
        "--max-per-canonical", type=int, default=None, metavar="N",
        help=(
            "同一 canonical parent 最多返回 N 条；school/evidence 默认 2，"
            "0 表示不限制"
        ),
    )
    p.add_argument(
        "--type", default=None,
        choices=["concept", "case"],
        help="只检索某类型卡片",
    )
    p.add_argument(
        "--school", action="append", default=[],
        help="只检索某流派；保留的单值参数，也可重复使用",
    )
    p.add_argument(
        "--schools", nargs="+", action="append", default=[],
        metavar="SCHOOL",
        help=(
            "检索一个或多个流派（OR），如 --schools ICT SMC；"
            "带空格的名称需加引号"
        ),
    )
    p.add_argument(
        "--all-schools", action="store_true",
        help="显式检索所有流派（不能与 --school/--schools 同用）",
    )
    p.add_argument(
        "--exclude-schools", nargs="+", action="append", default=[],
        metavar="SCHOOL",
        help="硬排除一个或多个 School（OR 排除）",
    )
    p.add_argument(
        "--sources", nargs="+", action="append", default=[],
        metavar="SOURCE",
        help="按来源硬过滤；仅 --layer evidence 支持",
    )
    p.add_argument(
        "--layer", default="canonical",
        choices=list(LAYER_COLLECTIONS),
        help=(
            "检索层：canonical（CLI 向后兼容默认，仅用于融合卡探索）/ "
            "school（Skill 默认路由）/ evidence；"
            "school/evidence 需要 v2 索引"
        ),
    )
    p.add_argument(
        "--list-schools", action="store_true",
        help="列出选定 layer 中实际存在的 School 及记录数后退出",
    )
    p.add_argument(
        "--explain-scope", action="store_true",
        help="校验并输出有效硬过滤范围，不执行向量检索",
    )
    p.add_argument(
        "--format", default="markdown",
        choices=["markdown", "json", "compact"],
        help="输出格式：markdown（默认，LLM 友好）/ json / compact（一行一张）",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def resolve_school_scope(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser | None = None,
) -> list[str]:
    """Resolve legacy and multi-value CLI options into one OR school scope."""
    raw_schools = list(args.school or [])
    for group in args.schools or []:
        raw_schools.extend(group)
    schools = normalize_filter_values(raw_schools)

    if raw_schools and not schools:
        message = "--school/--schools 至少需要一个非空的流派名称"
        if parser is not None:
            parser.error(message)
        raise ValueError(message)

    if args.all_schools and schools:
        message = "--all-schools 不能与 --school/--schools 同时使用"
        if parser is not None:
            parser.error(message)
        raise ValueError(message)
    return [] if args.all_schools else schools


def resolve_grouped_scope(
    groups: list[list[str]],
    option_name: str,
    parser: argparse.ArgumentParser | None = None,
) -> list[str] | None:
    """Flatten a repeatable nargs selector, preserving absent vs empty."""
    if not groups:
        return None
    raw_values = [value for group in groups for value in group]
    values = normalize_filter_values(raw_values)
    if raw_values and not values:
        message = f"{option_name} 至少需要一个非空值"
        if parser is not None:
            parser.error(message)
        raise ValueError(message)
    return values


def make_scope(
    schools: list[str],
    card_type: str | None,
    *,
    layer: str = "canonical",
    sources: list[str] | None = None,
    excluded_schools: list[str] | None = None,
) -> dict:
    """Return the effective, serializable retrieval scope."""
    excluded_schools = excluded_schools or []
    scope = {
        "schools": schools,
        "all_schools": not bool(schools or excluded_schools),
        "type": card_type or "all",
    }
    # Preserve the established canonical JSON scope shape unless a v2-only
    # selector is used. New fields are additive for new functionality.
    if layer != "canonical":
        scope["layer"] = layer
        scope["collection"] = LAYER_COLLECTIONS[layer]
    if sources:
        scope["sources"] = sources
    if excluded_schools:
        scope["excluded_schools"] = excluded_schools
    return scope


def format_scope(scope: dict) -> str:
    """Format a scope compactly for human-readable output."""
    schools = ",".join(scope["schools"]) if scope["schools"] else "all"
    parts = []
    if scope.get("layer"):
        parts.append(f"layer={scope['layer']}")
    parts.append(f"schools={schools}")
    if scope.get("excluded_schools"):
        parts.append("exclude_schools=" + ",".join(scope["excluded_schools"]))
    if scope.get("sources"):
        parts.append("sources=" + ",".join(scope["sources"]))
    parts.append(f"type={scope['type']}")
    return ";".join(parts)


def explain_scope_payload(
    scope: dict,
    where: dict | None,
    *,
    search_mode: str = "auto",
    max_per_canonical: int | None = None,
) -> dict:
    """Return a complete, machine-readable scope plan."""
    return {
        "layer": scope.get("layer", "canonical"),
        "collection": scope.get("collection", LAYER_COLLECTIONS["canonical"]),
        "schools": scope["schools"],
        "all_schools": scope["all_schools"],
        "excluded_schools": scope.get("excluded_schools", []),
        "sources": scope.get("sources", []),
        "type": scope["type"],
        "hard_filter": where,
        "exact_term_alias_boost": True,
        "search_mode": search_mode,
        "max_per_canonical": max_per_canonical,
    }


def print_scope_explanation(payload: dict, output_format: str) -> None:
    """Print --explain-scope without contaminating normal result formats."""
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif output_format == "compact":
        hard_filter = json.dumps(
            payload["hard_filter"], ensure_ascii=False, separators=(",", ":")
        )
        print(
            f"layer={payload['layer']} collection={payload['collection']} "
            f"filter={hard_filter} search_mode={payload['search_mode']} "
            f"exact_term_alias_boost=true"
        )
    else:
        print("# 检索范围解释\n")
        print(f"- **Layer**: {payload['layer']}")
        print(f"- **Collection**: {payload['collection']}")
        print(
            "- **Hard filter**: `"
            + json.dumps(payload["hard_filter"], ensure_ascii=False)
            + "`"
        )
        print(f"- **Search mode**: {payload['search_mode']}")
        print(
            "- **Max per canonical**: "
            + (
                str(payload["max_per_canonical"])
                if payload["max_per_canonical"] is not None
                else "unlimited"
            )
        )
        print("- **Exact canonical/alias boost**: enabled")


def print_school_inventory(
    layer: str,
    inventory: list[dict],
    output_format: str,
) -> None:
    """Print the collection-backed School inventory."""
    if output_format == "json":
        payload = {
            "layer": layer,
            "collection": LAYER_COLLECTIONS[layer],
            "schools": inventory,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif output_format == "compact":
        for item in inventory:
            kind = item.get("kind") or "-"
            print(f"{item['name']}\t{item['count']}\t{kind}")
    else:
        print(f"# School inventory (layer={layer})\n")
        print("| School | Records | Kind | Aliases |")
        print("|---|---:|---|---|")
        for item in inventory:
            aliases = ", ".join(item.get("aliases") or []) or "-"
            print(
                f"| {item['name']} | {item['count']} | "
                f"{item.get('kind') or '-'} | {aliases} |"
            )


def format_card_text(card_full: dict, retrieved_doc: str, header: str) -> str:
    """把检索结果格式化成 LLM 可读的 markdown 块。"""
    out = [f"## {header}"]

    if card_full.get("canonical_term"):
        out.append(f"**Term**: {card_full['canonical_term']}")
        if card_full.get("aliases"):
            out.append(f"**Aliases**: {', '.join(card_full['aliases'])}")
        if card_full.get("school"):
            out.append(f"**School**: {card_full['school']}")
        out.append("")
        if card_full.get("definition"):
            out.append(f"**Definition**: {card_full['definition']}")
        if card_full.get("identification_rules"):
            out.append("\n**Identification rules**:")
            for r in card_full["identification_rules"]:
                out.append(f"- {r}")
        if card_full.get("trading_implication"):
            out.append(f"\n**Trading implication**: {card_full['trading_implication']}")
        if card_full.get("common_mistakes"):
            out.append("\n**Common mistakes**:")
            for m in card_full["common_mistakes"]:
                out.append(f"- {m}")
        related = card_full.get("related_concepts") or []
        if related:
            rel_strs = [r.get("term") if isinstance(r, dict) else r for r in related]
            rel_strs = [s for s in rel_strs if s]
            if rel_strs:
                out.append(f"\n**Related**: {', '.join(rel_strs)}")
        stats = card_full.get("stats") or {}
        if stats.get("source_count"):
            out.append(f"\n_合并自 {stats['source_count']} 个视频源_")
    elif card_full.get("title"):
        # case 卡
        out.append(f"**Title**: {card_full['title']}")
        if card_full.get("school"):
            out.append(f"**School**: {card_full['school']}")
        meta = []
        if card_full.get("asset"):
            meta.append(f"asset={card_full['asset']}")
        if card_full.get("timeframe"):
            meta.append(f"timeframe={card_full['timeframe']}")
        if meta:
            out.append(f"**Market**: {', '.join(meta)}")
        out.append("")
        if card_full.get("market_context"):
            out.append(f"**Context**: {card_full['market_context']}")
        if card_full.get("key_observation"):
            out.append(f"**Observation**: {card_full['key_observation']}")
        if card_full.get("analysis_steps"):
            out.append("\n**Analysis steps**:")
            for s in card_full["analysis_steps"]:
                out.append(f"- {s}")
        if card_full.get("lessons"):
            out.append(f"\n**Lessons**: {card_full['lessons']}")
        related = card_full.get("illustrates_concepts") or []
        if related:
            rel_strs = [r if isinstance(r, str) else r.get("term", "") for r in related]
            rel_strs = [s for s in rel_strs if s]
            if rel_strs:
                out.append(f"\n**Illustrates concepts**: {', '.join(rel_strs)}")
        if card_full.get("primary_image"):
            out.append(f"\n**Primary image**: {card_full['primary_image']}")
    else:
        # 兜底：直接用 embed 时的文本
        out.append(retrieved_doc)

    return "\n".join(out)


def main(
    argv: list[str] | None = None,
    *,
    _lock_already_held: bool = False,
) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    schools = resolve_school_scope(args, p)
    sources = resolve_grouped_scope(args.sources, "--sources", p)
    excluded_schools = resolve_grouped_scope(
        args.exclude_schools, "--exclude-schools", p
    )

    if not args.list_schools and not args.explain_scope:
        if not args.query or not args.query.strip():
            p.error("query 不能为空")
        if args.top_k < 1:
            p.error("--top-k 必须大于 0")
    if args.max_per_canonical is not None and args.max_per_canonical < 0:
        p.error("--max-per-canonical 不能小于 0")
    if args.list_schools and (
        schools
        or args.all_schools
        or sources is not None
        or excluded_schools is not None
        or args.type
    ):
        p.error(
            "--list-schools 只接受 --layer/--kb/--format，"
            "不能与 School/source/type selector 同用"
        )
    if sources is not None and args.layer != "evidence":
        p.error(
            "--sources 只支持 --layer evidence；canonical/school 层"
            "无法对聚合 source_names 执行精确硬过滤"
        )

    logging.basicConfig(
        level=logging.WARNING if not args.verbose else logging.DEBUG,
        format="%(levelname)-5s %(message)s",
    )

    kb_dir = Path(args.kb) if args.kb else DEFAULT_KB
    if not kb_dir.is_dir():
        log.error("知识库目录不存在: %s", kb_dir)
        return 1
    if not _lock_already_held:
        try:
            with knowledge_base_read_session(kb_dir):
                # Keep one generation lease through query, full-card reads and
                # output serialization; inner Retriever locks are re-entrant.
                return main(argv, _lock_already_held=True)
        except BuildLockUnavailable as exc:
            log.error("知识库正在被另一进程读取或更新，拒绝混合代际检索: %s", exc)
            return 1

    try:
        # Guard every read path, including the manifest-only --list-schools
        # fast path, after acquiring the generation lease so there is no
        # check/use race with a builder.
        assert_readable_generation(kb_dir)
    except RuntimeError as exc:
        log.error(str(exc))
        return 1

    if args.list_schools:
        try:
            inventory = read_only_school_inventory(kb_dir, args.layer)
        except RetrievalScopeError as e:
            log.error(str(e))
            return 2
        if inventory is not None:
            print_school_inventory(args.layer, inventory, args.format)
            return 0

    effective_search_mode = resolve_search_mode(args.search_mode, args.layer)
    if args.max_per_canonical is None:
        effective_max_per_canonical = 2 if args.layer != "canonical" else None
    elif args.max_per_canonical == 0:
        effective_max_per_canonical = None
    else:
        effective_max_per_canonical = args.max_per_canonical

    try:
        if args.layer == "canonical":
            # Keep compatibility with callers/mocks implementing the original
            # two-argument Retriever constructor.
            retriever = Retriever(kb_dir, None)
        else:
            retriever = Retriever(kb_dir, None, layer=args.layer)
    except FileNotFoundError as e:
        index_path = kb_dir / "_index"
        index_is_completely_absent = (
            not index_path.exists() and not index_path.is_symlink()
        )
        if args.search_mode == "lexical" and index_is_completely_absent:
            try:
                retriever = Retriever.from_source(kb_dir, layer=args.layer)
            except Exception as source_error:  # noqa: BLE001 - CLI boundary
                log.error(
                    "bundled corpus lexical fallback 初始化失败: %s",
                    source_error,
                )
                return 1
        else:
            log.error(str(e))
            if index_is_completely_absent and args.search_mode != "lexical":
                log.error(
                    "无索引模式不会把 auto/semantic/hybrid 静默降级；"
                    "仅显式 --search-mode lexical 可从 bundled knowledge corpus "
                    "执行 BM25 + exact alias（无向量语义排序）。"
                )
            return 1
    except RuntimeError as e:
        log.error(str(e))
        return 1

    if not args.list_schools and getattr(retriever, "read_only_fallback", False):
        try:
            effective_search_mode = retriever.available_search_mode(
                args.search_mode
            )
        except ReadOnlySearchModeError as e:
            log.error(str(e))
            return 1
        if effective_search_mode == "lexical":
            log.warning(
                "只读索引：search-mode=%s 已解析为 lexical；保留 BM25、"
                "exact term/alias 和硬过滤，但不包含向量语义排序。",
                args.search_mode,
            )

    if args.list_schools:
        try:
            if hasattr(retriever, "school_inventory"):
                inventory = retriever.school_inventory()
            else:
                inventory = [
                    {
                        "name": name,
                        "count": count,
                        "available_in_layer": True,
                        "aliases": [],
                    }
                    for name, count in retriever.list_schools().items()
                ]
            print_school_inventory(args.layer, inventory, args.format)
        except RetrievalScopeError as e:
            log.error(str(e))
            return 2
        return 0

    try:
        if hasattr(retriever, "resolve_scope"):
            resolved = retriever.resolve_scope(
                filter_schools=schools or (None if not (args.school or args.schools) else []),
                filter_sources=sources,
                exclude_schools=excluded_schools,
                filter_type=args.type,
            )
        else:
            # Backward-compatible adapter path for embedders/tests wrapping the
            # original Retriever API.
            resolved = {
                "schools": schools,
                "sources": sources or [],
                "excluded_schools": excluded_schools or [],
                "type": args.type,
                "where": build_where_filter(
                    filter_schools=schools,
                    filter_sources=sources,
                    exclude_schools=excluded_schools,
                    filter_type=args.type,
                ),
            }
    except RetrievalScopeError as e:
        log.error(str(e))
        return 2

    schools = resolved["schools"]
    sources = resolved["sources"]
    excluded_schools = resolved["excluded_schools"]
    scope = make_scope(
        schools,
        args.type,
        layer=args.layer,
        sources=sources,
        excluded_schools=excluded_schools,
    )

    if args.explain_scope:
        print_scope_explanation(
            explain_scope_payload(
                scope,
                resolved["where"],
                search_mode=effective_search_mode,
                max_per_canonical=effective_max_per_canonical,
            ),
            args.format,
        )
        return 0

    try:
        if effective_search_mode != "lexical":
            retriever.embedder = get_embedder(args.embedder)
        cards = retriever.search(
            query=args.query.strip(),
            top_k=args.top_k,
            filter_type=args.type,
            filter_schools=schools or None,
            filter_sources=sources or None,
            exclude_schools=excluded_schools or None,
            search_mode=args.search_mode,
            max_per_canonical=args.max_per_canonical,
        )
    except Exception as e:  # noqa: BLE001 - CLI boundary: report, do not traceback
        if args.verbose:
            log.exception("检索执行失败: %s", e)
        else:
            log.error("检索执行失败: %s", e)
        return 1

    if not cards:
        if args.format == "json":
            print("[]")
        elif args.format == "compact":
            # Preserve the original machine-friendly compact sentinel.
            print("(no results)")
        else:
            print(f"(no results; scope: {format_scope(scope)})")
        return 0

    if args.format == "json":
        out = []
        for c in cards:
            item = {
                "card_id": c.card_id,
                "type": c.card_type,
                "term": c.term,
                "school": c.school,
                "distance": (
                    round(c.distance, 4) if c.distance is not None else None
                ),
                "file_path": c.file_path,
                "scope": scope,
                "retrieval": {
                    "search_mode": effective_search_mode,
                    "match_kind": c.match_kind,
                    "fusion_score": round(c.fusion_score, 8),
                    "lexical_score": round(c.lexical_score, 8),
                    "semantic_rank": c.semantic_rank,
                    "lexical_rank": c.lexical_rank,
                },
            }
            if args.layer == "canonical":
                item["card"] = retriever.get_full_card(c)
            else:
                item["record"] = {
                    "match_kind": c.match_kind,
                    "fusion_score": round(c.fusion_score, 8),
                    "lexical_score": round(c.lexical_score, 8),
                    "semantic_rank": c.semantic_rank,
                    "lexical_rank": c.lexical_rank,
                    "metadata": c.metadata,
                    "document": c.document,
                }
            out.append(item)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.format == "compact":
        for i, c in enumerate(cards, 1):
            match = f" match={c.match_kind:<8}" if args.layer != "canonical" else ""
            scores = ""
            if c.fusion_score > 0:
                scores += f" rrf={c.fusion_score:.5f}"
            if c.lexical_score > 0:
                scores += f" bm25={c.lexical_score:.5f}"
            distance = (
                f"{c.distance:.3f}" if c.distance is not None else "n/a"
            )
            print(
                f"[{i}] {c.term:<40}"
                f" type={c.card_type:<7} school={c.school or '-':<10}"
                f"{match}"
                f"{scores}"
                f" distance={distance}"
                f" → {c.file_path}"
            )
        return 0

    # markdown
    print(
        f"# 检索结果：'{args.query}'"
        f"（top {len(cards)}；scope: {format_scope(scope)}）\n"
    )
    for i, c in enumerate(cards, 1):
        # v2 documents are already School/source-scoped projections. Loading
        # their parent canonical card here would re-introduce fused rules and
        # silently defeat the requested isolation boundary.
        if args.layer == "canonical":
            full = retriever.get_full_card(c) or {}
        else:
            full = {}
        distance = f"{c.distance:.3f}" if c.distance is not None else "n/a"
        header = (
            f"[{i}] {c.term}"
            f"  _(type={c.card_type}, school={c.school or '-'}, "
            f"distance={distance}"
            + (f", match={c.match_kind}" if args.layer != "canonical" else "")
            + (
                f", rrf={c.fusion_score:.5f}"
                if c.fusion_score > 0
                else ""
            )
            + (
                f", bm25={c.lexical_score:.5f}"
                if c.lexical_score > 0
                else ""
            )
            + ")_"
        )
        print(format_card_text(full, c.document, header))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
