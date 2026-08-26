from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from agent.env_utils import get_env
from agent.tools.gitee_api import mask_token

logger = logging.getLogger("agent.run.observability")

_LANGFUSE_CLIENT: Any | None = None


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def langfuse_enabled() -> bool:
    """判断是否启用 Langfuse。

    观测能力必须是课程项目的可选增强：未配置或配置不完整时，Agent 仍应照常运行。
    """

    if not _truthy(get_env("LX_AICODING_LANGFUSE_ENABLED", "false")):
        return False
    return bool(get_env("LANGFUSE_PUBLIC_KEY").strip() and get_env("LANGFUSE_SECRET_KEY").strip())


def _configure_langfuse_environment() -> None:
    """兼容 Langfuse 新旧环境变量命名。

    Langfuse v4 文档使用 `LANGFUSE_BASE_URL`，LangChain 集成文档和部分旧版本
    仍常见 `LANGFUSE_HOST`。这里允许用户填任意一个，并同步成两个变量。
    """

    public_key = get_env("LANGFUSE_PUBLIC_KEY").strip()
    secret_key = get_env("LANGFUSE_SECRET_KEY").strip()
    base_url = get_env("LANGFUSE_BASE_URL").strip() or get_env("LANGFUSE_HOST").strip()

    if public_key:
        os.environ["LANGFUSE_PUBLIC_KEY"] = public_key
    if secret_key:
        os.environ["LANGFUSE_SECRET_KEY"] = secret_key
    if base_url:
        os.environ["LANGFUSE_BASE_URL"] = base_url
        os.environ["LANGFUSE_HOST"] = base_url


def _get_langfuse_client() -> Any | None:
    global _LANGFUSE_CLIENT

    if not langfuse_enabled():
        return None
    if _LANGFUSE_CLIENT is not None:
        return _LANGFUSE_CLIENT

    try:
        _configure_langfuse_environment()
        from langfuse import get_client

        _LANGFUSE_CLIENT = get_client()
        return _LANGFUSE_CLIENT
    except Exception as exc:
        logger.warning("Langfuse 初始化失败，已跳过观测上报：%s", mask_token(str(exc)))
        return None


def _metadata_value(value: str | None) -> str | None:
    """Langfuse v4 propagated metadata 值建议保持在 200 字符以内。"""

    if value is None:
        return None
    text = mask_token(str(value)).strip()
    if not text:
        return None
    return text[:200]


def _run_metadata(
    *,
    thread_id: str,
    run_id: str | None,
    task_kind: str | None,
    repo_url: str | None,
) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for key, value in {
        "thread_id": thread_id,
        "run_id": run_id,
        "task_kind": task_kind,
        "repo_url": repo_url,
        "app": "lx-aicoding",
    }.items():
        normalized = _metadata_value(value)
        if normalized is not None:
            metadata[key] = normalized
    return metadata


@contextmanager
def langfuse_agent_run(
    *,
    thread_id: str,
    run_id: str | None = None,
    task_kind: str | None = None,
    repo_url: str | None = None,
) -> Iterator[list[Any]]:
    """创建一次 Agent 运行的 Langfuse callback 上下文。

    yield 出来的 callbacks 可直接放入 LangChain/LangGraph config。
    """

    client = _get_langfuse_client()
    if client is None:
        yield []
        return

    try:
        from langfuse import propagate_attributes
        from langfuse.langchain import CallbackHandler

        metadata = _run_metadata(
            thread_id=thread_id,
            run_id=run_id,
            task_kind=task_kind,
            repo_url=repo_url,
        )
        tags = ["lx-aicoding"]
        if task_kind:
            tags.append(task_kind)

        with propagate_attributes(
            session_id=_metadata_value(thread_id),
            trace_name="lx-aicoding-agent-run",
            metadata=metadata,
            tags=tags,
        ):
            yield [CallbackHandler()]
    except Exception as exc:
        logger.warning("Langfuse callback 创建失败，已跳过观测上报：%s", mask_token(str(exc)))
        yield []
    finally:
        try:
            client.flush()
        except Exception as exc:
            logger.warning("Langfuse flush 失败：%s", mask_token(str(exc)))
