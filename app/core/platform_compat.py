"""平台兼容层（Windows 专用修正）。

集中处理在 Windows 上必须显式调整、否则会在运行期才暴露的底层差异。
放在一处，避免每个入口各写一遍而漏掉某个（漏掉的后果通常是"异步路径偶发失败"）。

当前包含：

1. **异步事件循环策略**
   Windows 上 asyncio 默认使用 ``ProactorEventLoop``，而 psycopg 的异步连接不能
   在该循环上运行，会抛::

       Psycopg cannot use the 'ProactorEventLoop' to run in async mode.
       Please use a compatible event loop, for instance by setting
       'asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())'

   本项目的状态持久化（异步 checkpointer）走 psycopg 异步连接，因此必须在
   任何异步代码运行**之前**切换为 ``WindowsSelectorEventLoopPolicy``。

2. **启动方式同样关键**
   用 uvicorn 命令行启动时，uvicorn 会在加载应用后自行配置事件循环策略，
   可能覆盖本模块的设置，于是异步数据库调用全部失败
   （表现为"接口 500、诊断一律降级为转人工"），
   而在 pytest 进程内却完全正常——极难定位（本项目实测踩到）。

   因此推荐用 **``python -m app.main``** 启动：该入口先调用 ``apply()``
   再调用 ``uvicorn.run()``，顺序有保证。启动自检会打印实际事件循环类型，
   可直接确认是否为选择器循环。

导入本模块即完成设置（幂等）。
"""

from __future__ import annotations

import asyncio
import sys

_applied: list[str] = []


def apply() -> list[str]:
    """应用平台修正，返回已应用的项（幂等）。"""
    if sys.platform != "win32":
        return []

    if "event_loop_policy" not in _applied:
        policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        if policy is not None:
            asyncio.set_event_loop_policy(policy())
            _applied.append("event_loop_policy")

    return list(_applied)


def describe() -> str:
    """返回当前异步事件循环策略描述，便于写入自检日志。"""
    if sys.platform != "win32":
        return "非 Windows 平台，使用默认策略"
    try:
        policy = asyncio.get_event_loop_policy()
        return f"{type(policy).__name__}"
    except Exception as exc:  # noqa: BLE001
        return f"未知({exc})"


def is_compatible_loop(loop_name: str) -> bool:
    """判断给定事件循环是否满足异步数据库驱动的要求。

    选择器循环（Selector / _WindowsSelector）兼容；Proactor 不兼容。
    非 Windows 平台一律视为兼容。
    """
    if sys.platform != "win32":
        return True
    return "selector" in loop_name.lower()


apply()
