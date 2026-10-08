"""向量库封装（本机持久化）。

- 只用本机目录持久化，不依赖任何外部向量服务。
- 向量化由 ``app/core/embedding.py`` 提供，集合维度来自配置，**不写死**。
- 集合的向量化实现标识写入元数据，实现变更时能自动识别并提示重建，
  避免"换了向量化方式却复用旧索引"导致的检索失真。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import RAG_DIR, settings
from app.core.embedding import get_embedding_provider
from app.core.logging_config import get_logger, timed_step

log = get_logger(__name__)


@dataclass
class VectorHit:
    """一条检索命中。"""

    id: str
    document: str
    metadata: dict[str, Any] = field(default_factory=dict)
    distance: float = 0.0
    score: float = 0.0


class _ProviderEmbeddingFunction:
    """把项目的向量化实现适配到向量库客户端接口。

    客户端在**写入**时调用 ``__call__``，在**查询**时调用 ``embed_query``，
    两个方法都必须实现，否则查询阶段会报缺方法。
    """

    def __init__(self) -> None:
        self._provider = get_embedding_provider()

    # 写入路径
    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        return self._provider.embed_documents(list(input))

    # 查询路径。注意：协议要求返回 Embeddings（列表的列表），
    # 即使只查一条也必须包一层，否则客户端会把 float 当成向量序列。
    def embed_query(self, input: str | Sequence[str]) -> list[list[float]]:
        if isinstance(input, str):
            return [self._provider.embed_query(input)]
        return self._provider.embed_documents(list(input))

    # 兼容部分版本按 name() 生成集合指纹
    @staticmethod
    def name() -> str:
        return get_embedding_provider().name


class VectorStore:
    """本机持久化向量集合。

    索引自愈：集合元数据里记录"索引签名"（向量化实现 + 维度 + 距离度量）。
    若签名与当前配置不一致（换了向量化实现、改了维度、改了度量），
    自动丢弃旧集合并重建，避免"新向量写进旧索引"造成的检索失真。
    距离度量必须用 ``hnsw:space`` 键写入，写错键名会被客户端忽略而静默退回
    默认度量（本次 Spike 实测踩到，已固化为约束 C-5）。
    """

    INDEX_SCHEMA_VERSION = "1"

    def __init__(
        self,
        collection: str | None = None,
        persist_dir: Path | str | None = None,
        *,
        reset: bool = False,
    ) -> None:
        import chromadb

        self.collection_name = collection or settings.chroma_collection
        self.persist_dir = Path(persist_dir) if persist_dir else RAG_DIR / "chroma"
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self.persist_dir))
        self._provider = get_embedding_provider()
        self._embed_fn = _ProviderEmbeddingFunction()

        self._expected_meta = {
            "embedding": self._provider.name,
            # 元数据值必须是标量，维度统一以字符串记录
            "dim": str(self._provider.dim),
            # 键名必须是 hnsw:space，否则客户端忽略并退回默认度量
            "hnsw:space": "cosine",
            "schema_version": self.INDEX_SCHEMA_VERSION,
        }

        if reset:
            self._drop_quietly()

        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            embedding_function=self._embed_fn,
            metadata=dict(self._expected_meta),
        )
        self._self_heal_if_schema_changed()

    # ------------------------------------------------------------------
    def _drop_quietly(self) -> None:
        try:
            self._client.delete_collection(self.collection_name)
        except Exception:  # noqa: BLE001, S110 - 集合不存在即无需删除
            pass

    def _self_heal_if_schema_changed(self) -> None:
        """索引签名不一致时重建集合。"""
        meta = dict(self._collection.metadata or {})
        if not meta:
            return
        mismatched = {
            k: (meta.get(k), v) for k, v in self._expected_meta.items() if meta.get(k) != v
        }
        if not mismatched:
            return
        log.warning(
            "vector.index.rebuild",
            collection=self.collection_name,
            reason="索引签名与当前配置不一致",
            mismatched={k: {"recorded": r, "expected": e} for k, (r, e) in mismatched.items()},
        )
        self._drop_quietly()
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            embedding_function=self._embed_fn,
            metadata=dict(self._expected_meta),
        )

    @property
    def count(self) -> int:
        return int(self._collection.count())

    @property
    def distance_space(self) -> str:
        """当前集合实际使用的距离度量。"""
        return str((self._collection.metadata or {}).get("hnsw:space", "l2"))

    # ------------------------------------------------------------------
    def upsert(
        self,
        ids: Sequence[str],
        documents: Sequence[str],
        metadatas: Sequence[dict[str, Any]] | None = None,
    ) -> int:
        """写入或覆盖。返回写入条数。"""
        ids = list(ids)
        documents = list(documents)
        if not ids:
            return 0
        if len(ids) != len(documents):
            raise ValueError("ids 与 documents 长度必须一致")
        metas = list(metadatas) if metadatas else [{} for _ in ids]
        # 向量库只接受标量元数据，统一清洗
        clean_metas = [{k: _scalar(v) for k, v in m.items()} for m in metas]

        with timed_step("vector.upsert", collection=self.collection_name, count=len(ids)):
            self._collection.upsert(ids=ids, documents=documents, metadatas=clean_metas)
        return len(ids)

    def search(
        self,
        query: str,
        top_k: int | None = None,
        *,
        min_score: float | None = None,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        """相似检索。``min_score`` 不满足时返回空列表（供降级矩阵判定空检索）。"""
        k = top_k or settings.rag_top_k
        threshold = settings.rag_min_score if min_score is None else min_score
        if self.count == 0:
            return []

        with timed_step("vector.search", collection=self.collection_name, top_k=k):
            res = self._collection.query(
                query_texts=[query],
                n_results=min(k, self.count),
                where=where or None,
                include=["documents", "metadatas", "distances"],
            )

        # 距离→相似度换算必须与集合实际度量一致，否则分数无意义
        space = self.distance_space
        if space != "cosine":  # pragma: no cover - 自愈逻辑已保证，防御性分支
            log.warning("vector.space.unexpected", collection=self.collection_name, space=space)

        hits: list[VectorHit] = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, doc_id in enumerate(ids):
            distance = float(dists[i]) if i < len(dists) else 2.0
            score = _distance_to_score(distance, space)
            if score < threshold:
                continue
            hits.append(
                VectorHit(
                    id=str(doc_id),
                    document=docs[i] if i < len(docs) else "",
                    metadata=dict(metas[i] or {}) if i < len(metas) else {},
                    distance=distance,
                    score=score,
                )
            )
        return hits

    def delete(self, ids: Sequence[str]) -> None:
        self._collection.delete(ids=list(ids))

    def drop(self) -> None:
        """删除整个集合（重建索引时使用）。"""
        self._client.delete_collection(self.collection_name)
        log.warning("vector.collection.dropped", collection=self.collection_name)


def _distance_to_score(distance: float, space: str) -> float:
    """距离转相似度。

    - cosine：距离 ∈ [0,2]，相似度 = 1 - 距离
    - l2：对已归一化向量，平方距离 = 2 - 2cos，故 cos = 1 - 距离/2
    """
    if space == "cosine":
        return 1.0 - distance
    if space == "l2":
        return max(-1.0, min(1.0, 1.0 - distance / 2.0))
    if space == "ip":
        return -distance
    return 1.0 - distance


def _scalar(value: Any) -> Any:
    """把元数据值清洗为向量库可存储的标量。"""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
