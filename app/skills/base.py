"""技能基类与公共能力。

每个技能独立一个 ``ToolClient`` 实例，用于把该技能产生的工具调用记录
单独收集、落库（供链路回放与指标统计），互不混淆。
"""

from __future__ import annotations

import re
from typing import Any

from app.core.logging_config import get_logger
from app.tools.client import ToolClient, ToolError, get_tool_client

log = get_logger(__name__)


class BaseSkill:
    """技能基类。"""

    code: str = ""
    name: str = ""
    description: str = ""
    # 必填实体（缺失则触发澄清追问）
    required_entities: tuple[str, ...] = ()

    def __init__(self, client: ToolClient | None = None) -> None:
        # 每个技能单独持有客户端，便于按技能归集调用记录
        self.client = client or ToolClient(base_url=get_tool_client().base_url)

    # ------------------------------------------------------------------
    def extract(self, message: str, entities: dict[str, Any] | None = None) -> dict[str, Any]:
        """从消息中抽取实体。基类只做通用抽取，子类可覆盖补强。"""
        found = dict(entities or {})
        for pattern, key in self.entity_patterns():
            m = re.search(pattern, message, re.IGNORECASE)
            if m and not found.get(key):
                found[key] = m.group(1)
        return found

    def entity_patterns(self) -> list[tuple[str, str]]:
        """返回 (正则, 实体名) 列表。"""
        return []

    def missing_entities(self, entities: dict[str, Any]) -> list[str]:
        """返回缺失的必填实体名。"""
        return [k for k in self.required_entities if not entities.get(k)]

    def clarify_question(self, missing: list[str]) -> str:
        """生成澄清问题。子类应覆盖以给出更自然的问法。"""
        labels = {
            "workspace_id": "要排查的工作区",
            "channel": "集成渠道（如 Slack / 钉钉 / 飞书）",
            "doc_id": "打不开的文档",
            "user_id": "遇到问题的账号",
            "account_id": "订阅账号",
        }
        parts = [labels.get(m, m) for m in missing]
        return "为了定位问题，请提供：" + "、".join(parts) + "。"

    # ------------------------------------------------------------------
    def safe(self, fn, *args: Any, **kwargs: Any) -> tuple[bool, Any]:
        """安全调用工具：把 ``ToolError`` 转成 (False, 错误信息)。"""
        try:
            return True, fn(*args, **kwargs)
        except ToolError as exc:
            log.warning(
                "skill.tool_failed",
                skill=self.code,
                tool=getattr(fn, "__name__", "unknown"),
                kind=exc.kind,
                code=exc.code,
                error=str(exc)[:200],
            )
            return False, exc

    def call_tool(self, fn, *args: Any, **kwargs: Any) -> Any:
        """调用工具并把 ``self.client`` 注入进去。"""
        kwargs.setdefault("client", self.client)
        return fn(*args, **kwargs)
