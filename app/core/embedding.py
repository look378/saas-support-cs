"""向量化供给（Embedding Provider）。

**为什么本项目自带向量化实现（决策 ADR-001）**
--------------------------------------------------
所选大模型供应商**不提供 embeddings 端点**（其官方接口清单只有对话、补全、
模型列表、余额、文件五类）。若改用第三方在线向量化服务，会引入第二个供应商
与第二套密钥；若退化为纯关键词检索，又无法满足向量检索的要求。

因此本项目在 ``app/core/embedding.py`` 内置一个**确定性本地向量化**实现：

- 字符 n-gram（覆盖中文）+ 词 n-gram（覆盖英文/标识符）混合特征
- 有符号哈希投射到固定维度，子线性词频加权，L2 归一化
- 完全确定性：同样文本永远得到同样向量，可复现、可测试
- 零密钥、零下载、零外网依赖，单次向量化 < 1ms

向量维度由配置 ``EMBEDDING_DIM`` 显式声明，**任何代码位置不得写死维度**。
若需更强的语义泛化，可通过 ``EMBEDDING_PROVIDER=st`` 切换到本机句向量模型
（要求模型文件已存在于本机，不联网下载）。
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable, Sequence

from app.core.config import settings
from app.core.logging_config import get_logger

log = get_logger(__name__)

# 中文字符 / 英文单词 切分
_CJK = r"\u4e00-\u9fff"
_TOKEN_RE = re.compile(rf"[{_CJK}]|[a-zA-Z0-9_]+|[\u3040-\u30ff]")


class EmbeddingProvider(ABC):
    """向量化接口。切换实现不得影响调用方。"""

    @property
    @abstractmethod
    def dim(self) -> int:
        """向量维度。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """实现标识，写入日志与集合元数据用于追溯。"""

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化（索引构建）。"""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """单条查询向量化。"""


# ---------------------------------------------------------------------------
# 实现一：确定性本地哈希向量化（默认）
# ---------------------------------------------------------------------------
class LocalHashingEmbedding(EmbeddingProvider):
    """确定性本地向量化。

    特征构造：
    1. 提取 CJK 单字、英文/数字词元
    2. 生成 1~3 阶字符 n-gram（对中文短句有效）与相邻词元二元组
    3. 每个特征经 BLAKE2b 哈希取有符号索引，累加到固定维度
    4. 子线性词频压缩 ``1 + ln(tf)`` 后 L2 归一化

    该方案对"同一故障的多轮改写问法"具备良好召回，且不存在模型加载与
    外网依赖，适合本项目的案例库/FAQ 规模（数十至数千条）。
    """

    def __init__(self, dim: int | None = None, ngram_min: int = 1, ngram_max: int = 3) -> None:
        self._dim = int(dim or settings.embedding_dim)
        self._ngram_min = ngram_min
        self._ngram_max = ngram_max

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def name(self) -> str:
        return f"local-hashing-v1(dim={self._dim},ngram={self._ngram_min}-{self._ngram_max})"

    # -- 特征提取 --
    @staticmethod
    def _normalize_text(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip().lower())

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return _TOKEN_RE.findall(text)

    def _features(self, text: str) -> Counter[str]:
        text = self._normalize_text(text)
        feats: Counter[str] = Counter()
        if not text:
            return feats

        tokens = self._tokens(text)
        # 1) 词元本身（加权更高，保证实体词的作用）
        for tok in tokens:
            feats[f"w:{tok}"] += 3

        # 2) 字符 n-gram（在去空格的紧凑串上生成，覆盖中文短语）
        compact = text.replace(" ", "")
        for n in range(self._ngram_min, self._ngram_max + 1):
            if len(compact) < n:
                continue
            for i in range(len(compact) - n + 1):
                feats[f"c{n}:{compact[i:i + n]}"] += 1

        # 3) 相邻词元二元组（保留词序信息，如"token 过期"）
        for a, b in zip(tokens, tokens[1:]):
            feats[f"b:{a}_{b}"] += 2

        return feats

    def _index_of(self, feature: str) -> tuple[int, float]:
        """特征 → (维度下标, 符号)。符号让哈希冲突相互抵消而非单向累加。"""
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        idx = value % self._dim
        sign = 1.0 if (value >> 63) & 1 else -1.0
        return idx, sign

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        feats = self._features(text)
        if not feats:
            return vec
        for feature, tf in feats.items():
            idx, sign = self._index_of(feature)
            # 子线性词频：抑制高频特征的主导
            vec[idx] += sign * (1.0 + math.log(tf))
        # L2 归一化 → 余弦相似度等价于点积
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            inv = 1.0 / norm
            vec = [v * inv for v in vec]
        return vec

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)


# ---------------------------------------------------------------------------
# 实现二：本机句向量模型（可选升级，不联网下载）
# ---------------------------------------------------------------------------
class SentenceTransformerEmbedding(EmbeddingProvider):
    """基于本机句向量模型的实现。

    仅当 ``EMBEDDING_PROVIDER=st`` 且模型文件已在本机时可用。
    **不会自动下载模型**：路径不存在即报错，避免演示环境不可复现。
    """

    def __init__(self, model_path: str | None = None) -> None:
        path = model_path or settings.embedding_model_path
        if not path:
            raise RuntimeError(
                "EMBEDDING_PROVIDER=st 时必须配置 EMBEDDING_MODEL_PATH 指向本机模型目录"
            )
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:  # pragma: no cover - 依赖可选
            raise RuntimeError(
                "未安装 sentence-transformers，无法使用 st 向量化实现；"
                "请改用 EMBEDDING_PROVIDER=local"
            ) from exc
        self._model = SentenceTransformer(path)
        self._dim = int(self._model.get_sentence_embedding_dimension())

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def name(self) -> str:
        return f"sentence-transformer(dim={self._dim})"

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        arr = self._model.encode(list(texts), normalize_embeddings=True)
        return [list(map(float, row)) for row in arr]

    def embed_query(self, text: str) -> list[float]:
        arr = self._model.encode([text], normalize_embeddings=True)
        return list(map(float, arr[0]))


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
_provider: EmbeddingProvider | None = None


def get_embedding_provider(refresh: bool = False) -> EmbeddingProvider:
    """获取向量化实现单例。"""
    global _provider
    if _provider is None or refresh:
        if settings.embedding_provider == "st":
            _provider = SentenceTransformerEmbedding()
        else:
            _provider = LocalHashingEmbedding(dim=settings.embedding_dim)
        log.info(
            "embedding.provider.ready",
            provider=_provider.name,
            dim=_provider.dim,
        )
    return _provider


def cosine_similarity(a: Iterable[float], b: Iterable[float]) -> float:
    """余弦相似度（两侧均已归一化时即点积）。"""
    va, vb = list(a), list(b)
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
