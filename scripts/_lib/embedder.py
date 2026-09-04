"""Embedding utilities — text → vector.

Default: local `nomic-embed-text-v1.5` (Apache 2.0, multilingual,
~547 MB / 522 MiB weight file).
Users need no API key. An optional OpenAI-compatible provider is also
available; see `get_embedder('openai')`.
"""

from __future__ import annotations

import logging
import os
import warnings
from typing import Optional

# 抑制 HuggingFace Hub 联网检查 + 警告噪音。
# 模型一次下载后存在本地 ~/.cache/huggingface/，无需每次启动联网检查更新。
# setdefault 让用户能通过环境变量覆盖（如果真的想联网更新）。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# 抑制各种库的 INFO/WARNING 噪音（保留 ERROR）
for _noisy in (
    "transformers", "sentence_transformers",
    "huggingface_hub", "urllib3", "filelock",
):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

# 抑制 Pydantic / sentence-transformers 的 FutureWarning
warnings.filterwarnings("ignore", category=FutureWarning, module="sentence_transformers")
warnings.filterwarnings("ignore", category=FutureWarning, module="rag.embedder")

import numpy as np


log = logging.getLogger(__name__)


class LocalNomicEmbedder:
    """本地 nomic-embed-text-v1.5。

    特点：
    - 约 547 MB / 522 MiB 权重，~4GB 内存
    - 支持中英文跨语言（实测）
    - 需要给文本加前缀 "search_query: " / "search_document: " 才能正确工作
    """

    DEFAULT_MODEL = "nomic-ai/nomic-embed-text-v1.5"
    # Pin the exact model snapshot. With transformers >=5.5 the text model has
    # a built-in implementation, so loading explicitly disables remote code.
    DEFAULT_REVISION = "e9b6763023c676ca8431644204f50c2b100d9aab"

    def __init__(self, model_name: Optional[str] = None) -> None:
        # 显式压低 transformers logger
        try:
            import transformers.utils.logging as _tlog  # noqa: PLC0415
            _tlog.set_verbosity_error()
        except Exception:  # noqa: BLE001
            pass

        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        name = model_name or self.DEFAULT_MODEL
        self.model_name = name
        self.model_revision = (
            self.DEFAULT_REVISION if name == self.DEFAULT_MODEL else None
        )
        identity = (
            f"{name}@{self.model_revision[:12]}"
            if self.model_revision
            else name
        )
        log.info("加载 embedding model: %s（首次使用会下载约 547 MB / 522 MiB 权重）", identity)
        # 加载模型时全局压制 <ERROR 级别日志 + 重定向 stderr
        # ("<All keys matched successfully>" 等无害噪音不应打到用户屏幕)
        import contextlib  # noqa: PLC0415
        logging.disable(logging.ERROR - 1)  # 抑制所有 < ERROR 级别
        try:
            with open(os.devnull, "w") as _devnull, contextlib.redirect_stderr(_devnull):
                kwargs = {"trust_remote_code": False}
                if self.model_revision:
                    kwargs["revision"] = self.model_revision
                self.model = SentenceTransformer(name, **kwargs)
        finally:
            logging.disable(logging.NOTSET)  # 恢复
        # Pillow / sentence-transformers 新版改名；优先用新方法，旧版本回退
        if hasattr(self.model, "get_embedding_dimension"):
            self.dim = self.model.get_embedding_dimension()
        else:
            self.dim = self.model.get_sentence_embedding_dimension()
        log.info("embedding dim = %d", self.dim)

    def embed_documents(
        self,
        texts: list[str],
        batch_size: int = 32,
        show_progress_bar: bool = True,
    ) -> np.ndarray:
        """对入库的文档批量 embed。"""
        prefixed = [f"search_document: {t}" for t in texts]
        return self.model.encode(
            prefixed,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=show_progress_bar,
        )

    def embed_query(self, query: str) -> np.ndarray:
        """对查询单次 embed。"""
        prefixed = f"search_query: {query}"
        vec = self.model.encode(
            [prefixed],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vec[0]


class OpenAIEmbedder:
    """OpenAI text-embedding-3-small (alternative embedding provider).

    Requires OPENAI_API_KEY environment variable.
    """

    DEFAULT_MODEL = "text-embedding-3-small"

    def __init__(
        self,
        model_name: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        from openai import OpenAI  # noqa: PLC0415
        self.model_name = model_name or self.DEFAULT_MODEL
        self.model_revision = None  # provider-managed service model identity
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key or os.environ.get("OPENAI_API_KEY"),
        )
        self.dim = 1536  # text-embedding-3-small 默认 1536

    def embed_documents(
        self,
        texts: list[str],
        batch_size: int = 100,
        show_progress_bar: bool = True,
    ) -> np.ndarray:
        """OpenAI API 支持批量，单次最多 ~2048 个，分批跑。"""
        del show_progress_bar  # Common document-embedder API; no local bar here.
        all_vecs = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            resp = self.client.embeddings.create(input=batch, model=self.model_name)
            for item in resp.data:
                all_vecs.append(item.embedding)
        return np.array(all_vecs, dtype=np.float32)

    def embed_query(self, query: str) -> np.ndarray:
        resp = self.client.embeddings.create(input=[query], model=self.model_name)
        return np.array(resp.data[0].embedding, dtype=np.float32)


def get_embedder(provider: str = "local", **kwargs) -> "LocalNomicEmbedder | OpenAIEmbedder":
    """工厂：按 provider 字符串选 embedder。

    provider:
      - "local" / "nomic"：本地 nomic-embed-text-v1.5（默认，开源友好）
      - "openai"：OpenAI text-embedding-3-small（需要 OPENAI_API_KEY）
    """
    p = (provider or "local").lower()
    if p in ("local", "nomic", "bge"):
        return LocalNomicEmbedder(model_name=kwargs.get("model_name"))
    if p == "openai":
        return OpenAIEmbedder(
            model_name=kwargs.get("model_name"),
            base_url=kwargs.get("base_url"),
            api_key=kwargs.get("api_key"),
        )
    raise ValueError(f"未知 embedder provider: {provider!r}（可选: local / openai）")
