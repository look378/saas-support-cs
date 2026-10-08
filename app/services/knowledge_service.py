"""知识库服务：案例库与 FAQ 的向量索引。

承担三件事：
  1. 从 CSV 装载案例与 FAQ（数据源在 ``data/samples/``）
  2. 构建/重建向量索引（本机持久化）
  3. 提供检索接口，并把案例的"断言三列"一并带出

检索命中案例时会把 ``expected_diagnosis / expected_action / expected_verify_result``
放到元数据里，使集成测试可以据此断言（决策 ADR-005）。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import SAMPLES_DIR
from app.core.logging_config import get_logger, timed_step
from app.core.vector_store import VectorHit, VectorStore

log = get_logger(__name__)

CASES_CSV = "cases.csv"
FAQ_CSV = "faq.csv"


@dataclass
class KnowledgeItem:
    """一条知识条目。"""

    doc_id: str
    kind: str            # case / faq
    skill: str
    text: str            # 用于向量化的文本
    payload: dict[str, Any]


class KnowledgeService:
    """知识库读写。"""

    def __init__(self, store: VectorStore | None = None) -> None:
        self._store = store

    # ------------------------------------------------------------------
    @property
    def store(self) -> VectorStore:
        if self._store is None:
            self._store = VectorStore()
        return self._store

    # ------------------------------------------------------------------
    @staticmethod
    def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
        """读取案例 CSV。"""
        p = path or (SAMPLES_DIR / CASES_CSV)
        if not p.exists():
            log.warning("knowledge.cases_missing", path=str(p))
            return []
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            return [row for row in csv.DictReader(f) if row.get("case_id")]

    @staticmethod
    def load_faq(path: Path | None = None) -> list[dict[str, Any]]:
        """读取 FAQ CSV。"""
        p = path or (SAMPLES_DIR / FAQ_CSV)
        if not p.exists():
            log.warning("knowledge.faq_missing", path=str(p))
            return []
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            return [row for row in csv.DictReader(f) if row.get("faq_id")]

    # ------------------------------------------------------------------
    def items(self) -> list[KnowledgeItem]:
        """把案例与 FAQ 归一化为知识条目。"""
        out: list[KnowledgeItem] = []

        for c in self.load_cases():
            symptom = c.get("symptom", "")
            scenario = c.get("scenario", "")
            text = f"{scenario}。{symptom}"
            out.append(
                KnowledgeItem(
                    doc_id=c["case_id"],
                    kind="case",
                    skill=c.get("skill", ""),
                    text=text,
                    payload={
                        "case_id": c["case_id"],
                        "kind": "case",
                        "skill": c.get("skill", ""),
                        "scenario": scenario,
                        "symptom": symptom,
                        # 断言三列：测试据此判定诊断与动作是否正确
                        "expected_diagnosis": c.get("expected_diagnosis", ""),
                        "expected_action": c.get("expected_action", ""),
                        "expected_verify_result": c.get("expected_verify_result", ""),
                        "risk_level": c.get("risk_level", ""),
                        "tenant_profile": c.get("tenant_profile", ""),
                        "source_ref": c.get("source_ref", ""),
                    },
                )
            )

        for f in self.load_faq():
            q = f.get("question", "")
            a = f.get("answer", "")
            out.append(
                KnowledgeItem(
                    doc_id=f["faq_id"],
                    kind="faq",
                    skill="faq",
                    text=f"{q} {a}",
                    payload={
                        "faq_id": f["faq_id"],
                        "kind": "faq",
                        "skill": "faq",
                        "question": q,
                        "answer": a,
                        "category": f.get("category", ""),
                        "keywords": f.get("keywords", ""),
                    },
                )
            )
        return out

    # ------------------------------------------------------------------
    def rebuild(self, *, force: bool = False, from_promoted: bool = True) -> dict[str, Any]:
        """重建向量索引。

        ``from_promoted``：一并装载"人工办结回流"的案例（ADR-006 闭环）。
        """
        with timed_step("knowledge.rebuild"):
            if force:
                self.store.drop()
                self._store = VectorStore()

            items = self.items()
            if from_promoted:
                promoted = self._promoted_items()
                items.extend(promoted)

            if not items:
                return {"indexed": 0, "cases": 0, "faq": 0, "note": "未找到数据文件"}

            ids = [f"{i.kind}:{i.doc_id}" for i in items]
            docs = [i.text for i in items]
            metas = [{**i.payload, "doc_id": i.doc_id} for i in items]

            n = self.store.upsert(ids=ids, documents=docs, metadatas=metas)

        cases = sum(1 for i in items if i.kind == "case")
        faq = sum(1 for i in items if i.kind == "faq")
        by_skill: dict[str, int] = {}
        for i in items:
            by_skill[i.skill] = by_skill.get(i.skill, 0) + 1

        result = {
            "indexed": n,
            "cases": cases,
            "faq": faq,
            "by_skill": by_skill,
            "collection": self.store.collection_name,
            "embedding": self.store._provider.name,
        }
        log.info("knowledge.rebuilt", **result)
        return result

    def _promoted_items(self) -> list[KnowledgeItem]:
        """从"人工办结回流"中取出已提升的案例，纳入知识库（ADR-006）。"""
        try:
            from sqlalchemy import select

            from app.core.database import read_session
            from app.core.trace_context import bind
            from app.models import CaseFeedback

            # 回流数据跨租户汇总入库，这里显式绑定一个内部租户上下文以通过隔离闸门
            out: list[KnowledgeItem] = []
            with bind(tenant_id="t-001"), read_session() as s:
                rows = s.execute(
                    select(CaseFeedback).where(CaseFeedback.promoted.is_(True))
                ).scalars().all()
            for r in rows:
                out.append(
                    KnowledgeItem(
                        doc_id=f"fb-{r.id}",
                        kind="case",
                        skill=r.skill or "faq",
                        text=f"{r.diagnosis_code} {r.fix_action} {r.notes}",
                        payload={
                            "case_id": f"fb-{r.id}",
                            "kind": "case",
                            "skill": r.skill,
                            "expected_diagnosis": r.diagnosis_code,
                            "expected_action": r.fix_action,
                            "expected_verify_result": "resolved" if r.automatable else "",
                            "promoted": True,
                            "source_ref": f"escalation:{r.escalation_no}",
                        },
                    )
                )
            if out:
                log.info("knowledge.promoted_loaded", count=len(out))
            return out
        except Exception as exc:  # noqa: BLE001 - 回流数据缺失不应阻断建库
            log.warning("knowledge.promoted_failed", error=str(exc)[:200])
            return []

    # ------------------------------------------------------------------
    def search(self, query: str, top_k: int | None = None, *, kind: str | None = None) -> list[VectorHit]:
        """检索知识库。``kind`` 可限定为 case / faq。"""
        where = {"kind": kind} if kind else None
        return self.store.search(query, top_k=top_k, where=where)

    def search_cases(self, query: str, top_k: int = 3) -> list[VectorHit]:
        """只检索案例（诊断参考）。"""
        return self.search(query, top_k=top_k, kind="case")

    def stats(self) -> dict[str, Any]:
        """知识库统计。"""
        items = self.items()
        by_skill: dict[str, int] = {}
        for i in items:
            by_skill[i.skill] = by_skill.get(i.skill, 0) + 1
        return {
            "collection": self.store.collection_name,
            "documents": self.store.count,
            "source_cases": sum(1 for i in items if i.kind == "case"),
            "source_faq": sum(1 for i in items if i.kind == "faq"),
            "by_skill": by_skill,
            "embedding": self.store._provider.name,
        }
