"""导出示例数据集为 CSV，并校验分布与字段完整性。

用法：
    .venv\\Scripts\\python.exe scripts\\export_samples.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import SAMPLES_DIR  # noqa: E402
from app.data import samples as S  # noqa: E402

print("=" * 74)
print("  示例数据集导出与校验")
print("=" * 74)

problems = S.validate()
if problems:
    print("\n数据集自身存在问题：")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)

print("\n[分布校验]")
dist = S.distribution()
for skill, expect in S.EXPECTED_DISTRIBUTION.items():
    got = dist.get(skill, 0)
    print(f"  {skill:<12} 案例 {got} 条（期望 {expect}）  {'OK' if got == expect else '不符'}")
print(f"  {'faq':<12} FAQ {len(S.FAQ)} 条（期望 {S.EXPECTED_FAQ}）")
print(f"  案例合计 {len(S.CASES)} 条 + FAQ {len(S.FAQ)} 条 = {len(S.CASES) + len(S.FAQ)} 条")

SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

cases_path = SAMPLES_DIR / "cases.csv"
with cases_path.open("w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=S.CASES_HEADER)
    w.writeheader()
    w.writerows(S.CASES)

faq_path = SAMPLES_DIR / "faq.csv"
with faq_path.open("w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=S.FAQ_HEADER)
    w.writeheader()
    w.writerows(S.FAQ)

print("\n[导出文件]")
for p in (cases_path, faq_path):
    print(f"  {p}  ({p.stat().st_size} 字节)")

# 回读校验：确保 CSV 可被 KnowledgeService 正确解析
print("\n[回读校验]")
from app.services.knowledge_service import KnowledgeService  # noqa: E402

svc = KnowledgeService()
cases = svc.load_cases(cases_path)
faq = svc.load_faq(faq_path)
assert len(cases) == len(S.CASES), f"案例回读数量不符: {len(cases)}"
assert len(faq) == len(S.FAQ), f"FAQ 回读数量不符: {len(faq)}"
assert all(c["expected_diagnosis"] for c in cases), "存在 expected_diagnosis 为空的案例"
print(f"  案例回读 {len(cases)} 条，FAQ 回读 {len(faq)} 条，断言列非空 ✓")

# 知识条目归一化
items = svc.items()
by_kind: dict[str, int] = {}
for it in items:
    by_kind[it.kind] = by_kind.get(it.kind, 0) + 1
print(f"  归一化知识条目：{by_kind}，合计 {len(items)} 条")

print("\n" + "=" * 74)
print("  数据集校验与导出完成。")
print("=" * 74)
