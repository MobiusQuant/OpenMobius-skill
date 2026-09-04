#!/usr/bin/env python3
"""kb-doctor：一次性环境体检。

跑一次，定位 kb-qna / kb-analyze-chart / kb-annotate-chart 三个 Skill 运行常见问题：
- Python 虚拟环境 + 包
- nomic 嵌入模型本地缓存
- 知识库向量索引
- CJK 字体（中文 label 渲染必需）
- 当前 Skill 目录与 SKILL.md manifest

每项失败都附修复命令。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional, Sequence


SKILL_DIR = Path(__file__).resolve().parent.parent
INDEX_MANIFEST_FILE = "index_manifest.json"
INDEX_MANIFEST_VERSION = 2
INDEX_SCHEMA_VERSION = 3
LEGACY_COLLECTION = "knowledge_base"
V2_COLLECTIONS = ("school_knowledge_v2", "source_evidence_v2")
NATIVE_EMBEDDING_STRATEGY = "native_document"
INHERITED_EMBEDDING_STRATEGY = "inherited_parent_card"
NATIVE_INPUT_VERSION = "search-document-v2-maxseq512"
INHERITED_INPUT_VERSION = "inherited-parent-card-v1"
NATIVE_MAX_SEQ_LENGTH = 512
COLLECTION_LAYERS = {
    LEGACY_COLLECTION: "legacy",
    "school_knowledge_v2": "school",
    "source_evidence_v2": "evidence",
}
SKILL_NAME_MAX_CHARS = 64
SKILL_DESCRIPTION_MAX_CHARS = 1024
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NOMIC_MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"
NOMIC_MODEL_REVISION = "e9b6763023c676ca8431644204f50c2b100d9aab"
CANONICAL_EMBEDDING_INPUT_PROFILE = {
    "version": "search-document-v1-maxseq8192",
    "provider": "local",
    "task": "search_document",
    "max_seq_length": 8192,
    "model_revision": NOMIC_MODEL_REVISION,
}


def huggingface_hub_cache(
    *,
    environ: Optional[dict[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    """Match huggingface_hub's HF_HUB_CACHE > HF_HOME > home precedence."""
    env = os.environ if environ is None else environ
    configured_hub = env.get("HF_HUB_CACHE", "").strip()
    if configured_hub:
        return Path(configured_hub).expanduser()
    configured_home = env.get("HF_HOME", "").strip()
    hf_home = (
        Path(configured_home).expanduser()
        if configured_home
        else (Path.home() if home is None else Path(home)) / ".cache" / "huggingface"
    )
    return hf_home / "hub"

# 颜色（不支持 TTY 时降级为空字符串）
_USE_COLOR = sys.stdout.isatty()
GREEN = "\033[32m" if _USE_COLOR else ""
RED = "\033[31m" if _USE_COLOR else ""
YELLOW = "\033[33m" if _USE_COLOR else ""
DIM = "\033[2m" if _USE_COLOR else ""
RESET = "\033[0m" if _USE_COLOR else ""


def _ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def _fail(msg: str, fix: Optional[str] = None) -> None:
    print(f"  {RED}✗{RESET} {msg}")
    if fix:
        for line in fix.strip().splitlines():
            print(f"    {YELLOW}↳{RESET} {line}")


def _warn(msg: str, fix: Optional[str] = None) -> None:
    print(f"  {YELLOW}⚠{RESET} {msg}")
    if fix:
        for line in fix.strip().splitlines():
            print(f"    {DIM}↳{RESET} {line}")


def _section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


class SkillManifestError(ValueError):
    """Raised when the portable portion of SKILL.md frontmatter is invalid."""


def _strip_yaml_comment(value: str) -> str:
    """Strip a YAML comment without treating a quoted ``#`` as a comment."""
    quote: Optional[str] = None
    escaped = False
    index = 0
    while index < len(value):
        char = value[index]
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif quote == "'":
            if char == quote:
                # YAML escapes a single quote by doubling it.
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
        index += 1
    return value.rstrip()


def _parse_yaml_string(value: str, field: str) -> str:
    """Parse the small YAML string subset needed by portable skill manifests."""
    value = _strip_yaml_comment(value).strip()
    if not value:
        raise SkillManifestError(f"frontmatter field {field!r} must not be empty")
    if value[0] in "|>":
        raise SkillManifestError(
            f"frontmatter field {field!r} must be a one-line string",
        )
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SkillManifestError(
                f"frontmatter field {field!r} has an invalid quoted string",
            ) from exc
        if not isinstance(parsed, str):
            raise SkillManifestError(f"frontmatter field {field!r} must be a string")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise SkillManifestError(
                f"frontmatter field {field!r} has an invalid quoted string",
            )
        inner = value[1:-1]
        # Any quote left after removing doubled pairs was not escaped in YAML.
        if "'" in inner.replace("''", ""):
            raise SkillManifestError(
                f"frontmatter field {field!r} has an invalid quoted string",
            )
        return inner.replace("''", "'")
    lowered = value.lower()
    if lowered in {"null", "~", "true", "false"} or re.fullmatch(
        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?",
        value,
        flags=re.IGNORECASE,
    ):
        raise SkillManifestError(f"frontmatter field {field!r} must be a string")
    return value


def load_required_skill_frontmatter(skill_md: Path) -> dict[str, str]:
    """Read only the required, portable top-level fields from SKILL.md."""
    try:
        lines = skill_md.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SkillManifestError(f"cannot read {skill_md}: {exc}") from exc
    if not lines or lines[0].strip() != "---":
        raise SkillManifestError("SKILL.md must start with YAML frontmatter (---)")

    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing_index is None:
        raise SkillManifestError("SKILL.md frontmatter has no closing ---")

    required = {"name", "description"}
    found: dict[str, str] = {}
    for line in lines[1:closing_index]:
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not match or match.group(1) not in required:
            continue
        field, raw_value = match.groups()
        if field in found:
            raise SkillManifestError(f"duplicate frontmatter field {field!r}")
        found[field] = _parse_yaml_string(raw_value, field)

    missing = sorted(required - found.keys())
    if missing:
        raise SkillManifestError(
            "missing required frontmatter field(s): " + ", ".join(missing),
        )
    return found


def _is_branded_source_checkout(skill_dir: Path, skill_name: str) -> bool:
    """Allow only the repository's case-styled dirname during development."""
    return (
        skill_dir.name != skill_name
        and skill_dir.name.casefold() == skill_name.casefold()
        and (skill_dir / ".git").exists()
    )


# ============================================================================
# Checks
# ============================================================================

def check_env_python() -> bool:
    _section("Python 虚拟环境")
    # 同时认 .venv（标准）/ .env（旧）
    for sub in (".venv", ".env"):
        env_py = SKILL_DIR / sub / "bin" / "python"
        if env_py.is_file():
            _ok(f"虚拟环境 python 存在: {env_py}")
            return True
    _fail(
        f"找不到 .venv/bin/python in {SKILL_DIR}",
        f"cd {SKILL_DIR}\n"
        f"bash install.sh        # 一键安装 (推荐)\n"
        f"# 或手动:\n"
        f"python3 -m venv .venv && .venv/bin/pip install -r requirements.txt",
    )
    return False


def check_python_packages() -> bool:
    _section("Python 包")
    packages = [
        ("chromadb",                "pip install chromadb",                          True),
        ("sentence_transformers",   "pip install sentence-transformers",             True),
        ("PIL",                     "pip install Pillow",                            True),
        ("numpy",                   "pip install numpy",                             True),
        ("playwright",              "pip install playwright && playwright install chromium", True),
        ("openai",                  "pip install openai  (可选：远程 embedding 时需要)", False),
    ]
    all_ok = True
    for name, install_cmd, required in packages:
        try:
            mod = importlib.import_module(name)
            version = getattr(mod, "__version__", "?")
            _ok(f"{name} {version}")
        except ImportError:
            if required:
                _fail(f"{name} 未安装", install_cmd)
                all_ok = False
            else:
                _warn(f"{name} 未安装（可选）", install_cmd)
    return all_ok


def check_embedding_model() -> bool:
    _section("Embedding 模型 (nomic-embed-text-v1.5)")
    hf_cache = huggingface_hub_cache()
    model_dir = hf_cache / "models--nomic-ai--nomic-embed-text-v1.5"
    snap = model_dir / "snapshots" / NOMIC_MODEL_REVISION
    if snap.is_dir():
        weight_files = list(snap.glob("*.safetensors")) + list(
            snap.glob("pytorch_model.bin")
        )
        if weight_files:
            size_mb = sum(f.stat().st_size for f in weight_files) / 1024 / 1024
            _ok(f"已下载 pinned revision ({size_mb:.0f} MB): {snap}")
            return True
        _warn(
            f"固定版本目录存在但权重文件缺失: {snap}",
            "重新运行 installer 的 embedding model 预热步骤",
        )
        return False
    _fail(
        f"nomic-embed-text-v1.5 固定版本 {NOMIC_MODEL_REVISION[:12]} 未下载"
        "（首次会下载约 547 MB / 522 MiB 权重）",
        "首次跑 build_index.py 或 kb_retrieve.py 会自动下载\n"
        f"或手动预热: {sys.executable} -c \"from sentence_transformers import "
        "SentenceTransformer; "
        f"SentenceTransformer('{NOMIC_MODEL_ID}', revision='{NOMIC_MODEL_REVISION}', "
        "trust_remote_code=False)\"",
    )
    return False


def load_index_manifest(index_dir: Path) -> tuple[str, Optional[dict], Optional[str]]:
    """Return ``(state, manifest, error)`` without hiding invalid manifests."""
    path = index_dir / INDEX_MANIFEST_FILE
    if not path.exists():
        return "missing", None, None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "invalid", None, str(exc)
    if not isinstance(manifest, dict):
        return "invalid", None, "manifest root must be an object"
    return "valid", manifest, None


def _is_sha256(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _chroma_collection_names(client) -> set[str]:
    """Normalize Chroma's old/new list_collections return shapes."""
    names: set[str] = set()
    for collection in client.list_collections():
        name = collection if isinstance(collection, str) else getattr(collection, "name", None)
        if isinstance(name, str):
            names.add(name)
    return names


def _close_chroma_client(client) -> None:
    """Best-effort release of Chroma's SQLite handles before returning."""
    system = getattr(client, "_system", None)
    stop = getattr(system, "stop", None)
    if callable(stop):
        try:
            stop()
        except Exception:  # noqa: BLE001
            pass


def check_kb_index() -> bool:
    _section("知识库向量索引")
    index_dir = SKILL_DIR / "knowledge_base" / "_index"
    index_path = index_dir / "chroma.sqlite3"
    if not index_path.is_file():
        _fail(
            f"索引不存在: {index_path.relative_to(SKILL_DIR)}",
            f"cd {SKILL_DIR}\n"
            f".venv/bin/python scripts/build_index.py",
        )
        return False

    size_mb = index_path.stat().st_size / 1024 / 1024
    manifest_state, manifest, manifest_error = load_index_manifest(index_dir)
    if manifest_state == "invalid":
        _fail(
            f"索引 manifest 损坏: {manifest_error}",
            f"cd {SKILL_DIR}\n"
            f".venv/bin/python scripts/build_index.py --upgrade",
        )
        return False

    client = None

    def finish(result: bool) -> bool:
        if client is not None:
            _close_chroma_client(client)
        return result

    try:
        import chromadb  # noqa: PLC0415

        client = chromadb.PersistentClient(path=str(index_dir))
        names = _chroma_collection_names(client)

        if LEGACY_COLLECTION not in names:
            _fail(
                f"索引缺少兼容 collection: {LEGACY_COLLECTION}",
                f"cd {SKILL_DIR}\n"
                f".venv/bin/python scripts/build_index.py --force",
            )
            return finish(False)
    except ImportError as exc:
        # Package availability is reported by the separate package check.
        _warn(f"索引文件存在，但无法读取 collection: {exc}")
        return finish(True)
    except Exception as exc:  # noqa: BLE001
        _fail(
            f"索引文件无法打开: {exc}",
            f"cd {SKILL_DIR}\n"
            f".venv/bin/python scripts/build_index.py --force",
        )
        return finish(False)

    if manifest_state == "missing":
        _ok(f"旧版索引可用 ({size_mb:.1f} MB): {index_path.relative_to(SKILL_DIR)}")
        _warn(
            "未找到 index_manifest.json；v1 检索仍可用",
            f"cd {SKILL_DIR}\n"
            f".venv/bin/python scripts/build_index.py --upgrade",
        )
        return finish(True)

    assert manifest is not None
    if manifest.get("manifest_version") != INDEX_MANIFEST_VERSION:
        _fail(
            f"不支持的 manifest_version: {manifest.get('manifest_version')!r}",
            f"cd {SKILL_DIR}\n"
            f".venv/bin/python scripts/build_index.py --upgrade",
        )
        return finish(False)
    if manifest.get("index_schema_version") != INDEX_SCHEMA_VERSION:
        _fail(
            f"不支持的 index_schema_version: "
            f"{manifest.get('index_schema_version')!r}",
            f"cd {SKILL_DIR}\n"
            f".venv/bin/python scripts/build_index.py --upgrade",
        )
        return finish(False)
    for field in ("v2_input_fingerprint", "canonical_input_fingerprint"):
        if not _is_sha256(manifest.get(field)):
            _fail(
                f"索引 manifest 的 {field} 无效",
                f"cd {SKILL_DIR}\n"
                f".venv/bin/python scripts/build_index.py --upgrade",
            )
            return finish(False)

    strategies = manifest.get("embedding_strategy")
    models = manifest.get("embedding_models")
    revisions = manifest.get("embedding_revisions")
    dimensions = manifest.get("embedding_dimensions")
    if not all(
        isinstance(value, dict)
        for value in (strategies, models, revisions, dimensions)
    ):
        _fail("索引 manifest 缺少 embedding strategy/model/revision/dimension 映射")
        return finish(False)
    if strategies.get(LEGACY_COLLECTION) != "bundled_card_embeddings":
        _fail("legacy collection 的 embedding strategy 无效")
        return finish(False)
    legacy_model = manifest.get("embedding_model")
    legacy_revision = manifest.get("embedding_model_revision")
    legacy_dimension = manifest.get("embedding_dimension")
    if (
        not isinstance(legacy_model, str)
        or not legacy_model
        or isinstance(legacy_dimension, bool)
        or not isinstance(legacy_dimension, int)
        or legacy_dimension <= 0
        or models.get(LEGACY_COLLECTION) != legacy_model
        or LEGACY_COLLECTION not in revisions
        or revisions.get(LEGACY_COLLECTION) != legacy_revision
        or dimensions.get(LEGACY_COLLECTION) != legacy_dimension
    ):
        _fail("legacy collection 的 embedding model/dimension 无效")
        return finish(False)
    if legacy_model == NOMIC_MODEL_ID and legacy_revision != NOMIC_MODEL_REVISION:
        _fail("legacy collection 的 Nomic model revision 缺失或已过期")
        return finish(False)
    canonical_profile = manifest.get("canonical_embedding_input_profile")
    if legacy_model == NOMIC_MODEL_ID:
        if canonical_profile != CANONICAL_EMBEDDING_INPUT_PROFILE:
            _fail("canonical embedding input profile 缺失或已过期")
            return finish(False)
    elif canonical_profile is not None:
        _fail("非 Nomic legacy collection 不应声明 Nomic canonical profile")
        return finish(False)

    v2_strategy = strategies.get(V2_COLLECTIONS[0])
    v2_model = models.get(V2_COLLECTIONS[0])
    v2_revision = revisions.get(V2_COLLECTIONS[0])
    v2_dimension = dimensions.get(V2_COLLECTIONS[0])
    if v2_strategy not in {
        NATIVE_EMBEDDING_STRATEGY,
        INHERITED_EMBEDDING_STRATEGY,
    }:
        _fail(f"v2 embedding strategy 无效: {v2_strategy!r}")
        return finish(False)
    if (
        not isinstance(v2_model, str)
        or not v2_model
        or isinstance(v2_dimension, bool)
        or not isinstance(v2_dimension, int)
        or v2_dimension <= 0
        or any(strategies.get(name) != v2_strategy for name in V2_COLLECTIONS)
        or any(models.get(name) != v2_model for name in V2_COLLECTIONS)
        or any(name not in revisions for name in V2_COLLECTIONS)
        or any(revisions.get(name) != v2_revision for name in V2_COLLECTIONS)
        or any(dimensions.get(name) != v2_dimension for name in V2_COLLECTIONS)
    ):
        _fail("school/evidence collection 的 embedding 配置不一致或无效")
        return finish(False)
    if v2_strategy == INHERITED_EMBEDDING_STRATEGY and (
        v2_model != legacy_model
        or v2_revision != legacy_revision
        or v2_dimension != legacy_dimension
    ):
        _fail("继承模式的 v2 embedding 必须与 legacy model/dimension 一致")
        return finish(False)

    input_profile = manifest.get("v2_embedding_input_profile")
    if not isinstance(input_profile, dict) or input_profile.get("strategy") != v2_strategy:
        _fail("索引 manifest 的 v2 embedding input profile 缺失或策略不一致")
        return finish(False)
    if v2_strategy == NATIVE_EMBEDDING_STRATEGY:
        provider = input_profile.get("provider")
        expected_task = "search_document" if provider == "local" else "document"
        expected_max_length = NATIVE_MAX_SEQ_LENGTH if provider == "local" else None
        if (
            input_profile.get("version") != NATIVE_INPUT_VERSION
            or provider not in {"local", "openai"}
            or input_profile.get("task") != expected_task
            or input_profile.get("max_seq_length") != expected_max_length
            or (
                provider == "local"
                and (
                    v2_model != NOMIC_MODEL_ID
                    or v2_revision != NOMIC_MODEL_REVISION
                    or input_profile.get("model_revision")
                    != NOMIC_MODEL_REVISION
                )
            )
            or (
                provider == "openai"
                and (
                    v2_revision is not None
                    or "model_revision" in input_profile
                )
            )
        ):
            _fail("native v2 embedding input profile 无效或已过期")
            return finish(False)
    elif input_profile != {
        "version": INHERITED_INPUT_VERSION,
        "strategy": INHERITED_EMBEDDING_STRATEGY,
        "provider": "parent_card",
        "task": "inherited_parent_vector",
        "max_seq_length": None,
    }:
        _fail("继承模式的 v2 embedding input profile 无效")
        return finish(False)

    declared = manifest.get("collections")
    if not isinstance(declared, dict):
        _fail("索引 manifest 没有 collections 对象")
        return finish(False)

    for name, layer in COLLECTION_LAYERS.items():
        details = declared.get(name)
        if not isinstance(details, dict):
            _fail(f"索引 manifest 缺少 collection 定义: {name}")
            return finish(False)
        count = details.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _fail(f"collection {name} 的 manifest count 无效: {count!r}")
            return finish(False)
        expected_created = name == LEGACY_COLLECTION or count > 0
        if details.get("created") is not expected_created:
            _fail(f"collection {name} 的 created/count 不一致")
            return finish(False)
        if details.get("schema_version") != INDEX_SCHEMA_VERSION:
            _fail(f"collection {name} 的 manifest schema_version 无效")
            return finish(False)
        if details.get("layer") != layer:
            _fail(f"collection {name} 的 manifest layer 无效")
            return finish(False)

        if expected_created and name not in names:
            _fail(
                f"manifest 声明 collection {name}，但 ChromaDB 中不存在",
                f"cd {SKILL_DIR}\n"
                f".venv/bin/python scripts/build_index.py --force",
            )
            return finish(False)
        if not expected_created and name in names:
            _fail(f"collection {name} 未声明创建，但 ChromaDB 中存在")
            return finish(False)
        if expected_created:
            collection = client.get_collection(name)
            actual = collection.count()
            if actual != count:
                _fail(f"collection {name} 计数不匹配: {actual} != {count}")
                return finish(False)
            metadata = collection.metadata or {}
            if metadata.get("kb_schema_version") != INDEX_SCHEMA_VERSION:
                _fail(f"collection {name} 的 Chroma schema_version 无效")
                return finish(False)
            if metadata.get("layer") != layer:
                _fail(f"collection {name} 的 Chroma layer 无效")
                return finish(False)

    present_v2 = [name for name in V2_COLLECTIONS if name in names]
    _ok(
        f"索引可用 ({size_mb:.1f} MB, schema="
        f"{manifest.get('index_schema_version', '?')}): "
        f"{index_path.relative_to(SKILL_DIR)}"
    )
    if present_v2:
        counts = [f"{name}={client.get_collection(name).count()}" for name in present_v2]
        _ok("v2 collections: " + ", ".join(counts))
        if v2_strategy == INHERITED_EMBEDDING_STRATEGY:
            _warn("v2 collections 正在使用仅供应急/测试的父卡向量继承模式")
    else:
        _warn("v2 records 尚未可用；legacy knowledge_base 仍正常工作")
    return finish(True)


def check_playwright_chromium() -> bool:
    _section("Playwright Chromium (图表渲染)")
    import os as _os  # noqa: PLC0415
    import sys as _sys  # noqa: PLC0415
    if _sys.platform == "win32":
        default = Path(_os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"
    elif _sys.platform == "darwin":
        default = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        default = Path.home() / ".cache" / "ms-playwright"
    cache = Path(_os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or default)
    if not cache.is_dir():
        _fail(
            f"{cache} 不存在",
            ".venv/bin/playwright install chromium  (~280MB)",
        )
        return False
    candidates = list(cache.glob("chromium-*")) + list(cache.glob("chromium_headless_shell-*"))
    if not candidates:
        _fail(
            "Chromium 未在缓存中找到",
            ".venv/bin/playwright install chromium  (~280MB)",
        )
        return False
    sizes = [(c.name, sum(f.stat().st_size for f in c.rglob("*") if f.is_file())) for c in candidates]
    total = sum(s for _, s in sizes) / 1024 / 1024
    _ok(f"Chromium 已安装 ({len(candidates)} bundle, {total:.0f} MB total)")
    return True


def check_cjk_fonts() -> bool:
    _section("CJK 字体 (中文标注图必需)")
    # 与 kb_draw_annotation.py 保持一致
    cjk_fonts = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
        "C:\\Windows\\Fonts\\msyh.ttc",
        "C:\\Windows\\Fonts\\simhei.ttf",
    ]
    found = [p for p in cjk_fonts if Path(p).is_file()]
    if found:
        _ok(f"已安装 {len(found)} 个 CJK 字体:")
        for p in found:
            print(f"      - {p}")
        return True
    if sys.platform == "linux":
        fix = (
            "Debian/Ubuntu: sudo apt install fonts-noto-cjk\n"
            "Fedora/RHEL:   sudo dnf install google-noto-cjk-fonts\n"
            "Arch:          sudo pacman -S noto-fonts-cjk"
        )
    elif sys.platform == "darwin":
        fix = "macOS 通常自带 PingFang.ttc — 请检查 /System/Library/Fonts/"
    elif sys.platform == "win32":
        fix = "Windows 通常自带 msyh.ttc — 请检查 C:\\Windows\\Fonts\\"
    else:
        fix = "请安装任意 Noto Sans CJK / 文泉驿 / PingFang / SimHei 字体"
    _fail("未找到 CJK 字体（中文 label 会渲染为方块 口口口）", fix)
    return False


def check_skill_install(
    platform: Optional[str] = None,
    expected_dir: Optional[Path] = None,
) -> bool:
    """Validate this skill copy without assuming any platform's home layout."""
    title = "Skill manifest"
    if platform:
        title += f" ({platform})"
    _section(title)

    skill_md = SKILL_DIR / "SKILL.md"
    if not skill_md.is_file():
        _fail(f"SKILL.md not found: {skill_md}")
        return False

    try:
        manifest = load_required_skill_frontmatter(skill_md)
    except SkillManifestError as exc:
        _fail(f"Invalid SKILL.md frontmatter: {exc}")
        return False

    name = manifest["name"]
    description = manifest["description"].strip()
    all_ok = True

    if len(name) > SKILL_NAME_MAX_CHARS or not SKILL_NAME_RE.fullmatch(name):
        _fail(
            f"Invalid skill slug {name!r}",
            "Use 1-64 lowercase letters, digits, and single hyphens; "
            "do not start or end with a hyphen.",
        )
        all_ok = False
    else:
        _ok(f"manifest name: {name}")

    if not description:
        _fail("frontmatter description must not be blank")
        all_ok = False
    elif len(description) > SKILL_DESCRIPTION_MAX_CHARS:
        _fail(
            "frontmatter description is too long "
            f"({len(description)} > {SKILL_DESCRIPTION_MAX_CHARS} characters)",
        )
        all_ok = False
    else:
        _ok(f"manifest description: {len(description)} characters")

    if expected_dir is not None:
        expected = Path(expected_dir).expanduser().resolve()
        actual = SKILL_DIR.resolve()
        if actual != expected:
            _fail(f"Skill directory mismatch: expected {expected}, got {actual}")
            all_ok = False
        else:
            _ok(f"expected install directory: {expected}")

    if SKILL_DIR.name == name:
        _ok(f"directory name matches manifest slug: {name}")
    elif _is_branded_source_checkout(SKILL_DIR, name):
        _ok(
            "source checkout uses branded directory casing "
            f"({SKILL_DIR.name}); installed copies use {name}",
        )
    else:
        _fail(
            f"Skill directory {SKILL_DIR.name!r} does not match manifest name {name!r}",
            f"Install this skill into a directory named {name}",
        )
        all_ok = False

    return all_ok


def check_mobius_api() -> bool:
    """Mobius Quant API connectivity check (public endpoint, no auth)."""
    _section("Mobius Quant API (for live market data and chart generation)")
    import os as _os  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    base = _os.environ.get("MOBIUS_API_BASE", "https://api.mobiusquant.ai")

    req = urllib.request.Request(
        f"{base}/api/health",
        headers={
            "User-Agent": "MobiusSkills-Doctor/0.1",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json as _json  # noqa: PLC0415
            data = _json.loads(resp.read().decode("utf-8"))
            status = data.get("status", "unknown")
            totals = data.get("totals", {})
            _ok(
                f"API reachable: {base} (status={status}, "
                f"venues={totals.get('venues', '?')}, "
                f"streams={totals.get('streams', '?')})",
            )
    except urllib.error.HTTPError as e:
        _fail(
            f"API HTTP {e.code}: {base}",
            "Check network or override MOBIUS_API_BASE",
        )
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        _fail(
            f"API unreachable: {base} ({e.__class__.__name__}: {e})",
            "Ignore if you don't need live market data; otherwise check network / proxy",
        )
        return False
    return True


# ============================================================================
# Main
# ============================================================================

def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose an OpenMobius skill copy")
    parser.add_argument(
        "--platform",
        help="platform label for diagnostics (does not select a home directory)",
    )
    parser.add_argument(
        "--expected-dir",
        type=Path,
        help="expected directory of the skill copy being diagnosed",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    print(f"{DIM}{'=' * 64}{RESET}")
    print(f"  OpenMobius-skill 环境体检 (kb-doctor)")
    print(f"{DIM}{'=' * 64}{RESET}")
    print(f"  Skill: {SKILL_DIR}")
    print(f"  Python: {sys.version.split()[0]} ({sys.executable})")
    print(f"  Platform: {sys.platform}")

    results = {
        "Python 虚拟环境":      check_env_python(),
        "Python 包":           check_python_packages(),
        "Embedding 模型":      check_embedding_model(),
        "知识库索引":           check_kb_index(),
        "Playwright Chromium": check_playwright_chromium(),
        "CJK 字体":            check_cjk_fonts(),
        "Skill 安装":          check_skill_install(args.platform, args.expected_dir),
        "Mobius API":         check_mobius_api(),
    }

    # 关键项（决定 exit code）— Mobius API 是可选
    critical = {k: v for k, v in results.items() if k != "Mobius API"}

    print(f"\n{DIM}{'=' * 64}{RESET}")
    print("  Summary")
    print(f"{DIM}{'=' * 64}{RESET}")
    passed = sum(1 for v in critical.values() if v)
    total = len(critical)
    for name, ok_ in results.items():
        mark = f"{GREEN}✓{RESET}" if ok_ else f"{RED}✗{RESET}"
        print(f"  {mark} {name}")
    print()
    if passed == total:
        print(f"{GREEN}✓ All critical checks passed ({passed}/{total}){RESET}")
        return 0
    print(f"{YELLOW}⚠ {passed}/{total} critical checks passed — see ↳ fixes above{RESET}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
