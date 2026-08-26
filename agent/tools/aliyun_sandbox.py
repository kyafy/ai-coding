from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.tools import tool

from agent.core.events import record_event
from agent.core.graph import get_store
from agent.env_utils import get_env, require_env
from agent.tools.gitee_api import mask_token
from agent.tools.runtime_context import get_runtime_thread_id

logger = logging.getLogger("agent.run.aliyun_sandbox")

DEFAULT_SANDBOX_LABEL = "default"
MANUAL_THREAD_ID = "__manual__"
MAX_OUTPUT_CHARS = 12000
MAX_EVENT_OUTPUT_CHARS = 1200
FORBIDDEN_ENV_KEY_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PRIVATE")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _default_template() -> str:
    return get_env("ALIYUN_SANDBOX_TEMPLATE_ID", "code-interpreter-v1").strip()


def _default_region() -> str:
    domain = require_env("E2B_DOMAIN").strip()
    return domain.split(".")[0] if "." in domain else domain


def _sandbox_timeout_seconds(value: int | None = None) -> int:
    default = int(get_env("ALIYUN_SANDBOX_TIMEOUT_SECONDS", "21600"))
    return max(60, min(int(value or default), 86400))


def _command_timeout_seconds(value: int | None = None) -> int:
    return max(1, min(int(value or 30), 300))


def _thread_id() -> str:
    return get_runtime_thread_id() or MANUAL_THREAD_ID


def _clean_label(label: str | None) -> str:
    value = " ".join((label or DEFAULT_SANDBOX_LABEL).split())
    return value[:80] or DEFAULT_SANDBOX_LABEL


def _record_id(thread_id: str, label: str) -> str:
    return f"aliyun-sandbox:{thread_id}:{label}"


def _truncate(value: Any, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = mask_token(str(value or ""))
    return f"{text[:limit]}..." if len(text) > limit else text


def _parse_envs(envs_json: str | None) -> dict[str, str]:
    if not envs_json or not envs_json.strip():
        return {}
    try:
        parsed = json.loads(envs_json)
    except ValueError as exc:
        raise ValueError("envs_json 必须是 JSON 对象字符串") from exc
    if not isinstance(parsed, dict):
        raise ValueError("envs_json 必须是 JSON 对象字符串")
    envs: dict[str, str] = {}
    for key, value in parsed.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        upper_key = key_text.upper()
        if any(part in upper_key for part in FORBIDDEN_ENV_KEY_PARTS):
            raise ValueError(f"envs_json 禁止传入敏感环境变量：{key_text}")
        envs[key_text] = str(value)
    return envs


def _api_params() -> dict[str, str]:
    return {
        "api_key": require_env("E2B_API_KEY"),
        "api_url": require_env("E2B_API_URL"),
        "domain": require_env("E2B_DOMAIN"),
    }


def _command_result_to_dict(result: Any) -> dict[str, Any]:
    return {
        "exit_code": getattr(result, "exit_code", None),
        "stdout": _truncate(getattr(result, "stdout", "")),
        "stderr": _truncate(getattr(result, "stderr", "")),
        "error": _truncate(getattr(result, "error", "")),
    }


def _execution_to_dict(execution: Any) -> dict[str, Any]:
    error = getattr(execution, "error", None)
    results = []
    for result in getattr(execution, "results", []) or []:
        item: dict[str, Any] = {}
        for field in ("text", "markdown", "html", "json", "data"):
            value = getattr(result, field, None)
            if value is not None:
                item[field] = value
        formats = getattr(result, "formats", None)
        if callable(formats):
            item["formats"] = list(formats())
        results.append(item)
    return {
        "ok": error is None,
        "stdout": _truncate("\n".join(getattr(getattr(execution, "logs", None), "stdout", []) or [])),
        "stderr": _truncate("\n".join(getattr(getattr(execution, "logs", None), "stderr", []) or [])),
        "results": results,
        "error": None
        if error is None
        else {
            "name": _truncate(getattr(error, "name", "")),
            "value": _truncate(getattr(error, "value", "")),
            "traceback": _truncate(getattr(error, "traceback", "")),
        },
        "execution_count": getattr(execution, "execution_count", None),
    }


def _event_detail(payload: dict[str, Any]) -> str:
    safe_payload = dict(payload)
    for key in ("stdout", "stderr", "error"):
        if key in safe_payload:
            safe_payload[key] = _truncate(safe_payload[key], MAX_EVENT_OUTPUT_CHARS)
    return json.dumps(safe_payload, ensure_ascii=False)


def _connect_or_create_sandbox(
    *,
    label: str,
    template_id: str | None = None,
    timeout_seconds: int | None = None,
    envs: dict[str, str] | None = None,
    force_new: bool = False,
):
    from e2b_code_interpreter import Sandbox

    thread_id = _thread_id()
    label = _clean_label(label)
    store = get_store()
    api_params = _api_params()
    timeout = _sandbox_timeout_seconds(timeout_seconds)
    template = (template_id or _default_template()).strip()
    region = _default_region()
    record_id = _record_id(thread_id, label)

    if not force_new:
        existing = store.get_agent_sandbox(thread_id, label)
        if existing is not None:
            try:
                sandbox = Sandbox.connect(str(existing["sandbox_id"]), timeout=timeout, **api_params)
                store.update_agent_sandbox_status(str(existing["id"]), "running", touch_used=True)
                return sandbox, existing, False
            except Exception as exc:  # noqa: BLE001 - stale sandbox should be replaced
                error = mask_token(str(exc))
                logger.info("已有阿里云沙箱不可复用，准备创建新沙箱：%s", error)
                store.update_agent_sandbox_status(
                    str(existing["id"]),
                    "failed",
                    metadata={"last_error": error},
                    touch_used=True,
                )

    sandbox = Sandbox.create(
        template=template,
        timeout=timeout,
        metadata={"thread_id": thread_id, "label": label, "owner": "LX-AICODING"},
        envs=envs or {},
        **api_params,
    )
    expires_at = (_utc_now() + timedelta(seconds=timeout)).isoformat()
    record = store.upsert_agent_sandbox(
        sandbox_record_id=record_id,
        thread_id=thread_id,
        label=label,
        sandbox_id=sandbox.sandbox_id,
        template=template,
        region=region,
        status="running",
        metadata={"created_by": "agent_tool"},
        expires_at=expires_at,
    )
    return sandbox, record, True


@tool("ensure_aliyun_code_sandbox", parse_docstring=True)
def ensure_aliyun_code_sandbox(
    label: str = DEFAULT_SANDBOX_LABEL,
    template_id: str | None = None,
    timeout_seconds: int | None = None,
    force_new: bool = False,
    envs_json: str | None = None,
) -> dict[str, Any]:
    """获取或创建当前任务可复用的阿里云代码执行沙箱。

    Args:
        label: 沙箱标签，同一 thread 下默认使用 default。
        template_id: 可选模板名称或 ID，默认读取 ALIYUN_SANDBOX_TEMPLATE_ID。
        timeout_seconds: 沙箱保活时间，范围 60 到 86400 秒。
        force_new: 是否强制创建新沙箱并替换旧记录。
        envs_json: 可选非敏感环境变量 JSON 对象字符串。

    Returns:
        包含 ok、sandbox_id、label、template、region、created 的沙箱信息。
    """

    clean_label = _clean_label(label)
    thread_id = _thread_id()
    if thread_id != MANUAL_THREAD_ID:
        record_event(
            thread_id,
            f"aliyun-sandbox:ensure:{clean_label}",
            "准备阿里云代码沙箱",
            kind="execute",
            status="in_progress",
            detail=_event_detail({"label": clean_label, "force_new": force_new}),
        )
    try:
        sandbox, record, created = _connect_or_create_sandbox(
            label=clean_label,
            template_id=template_id,
            timeout_seconds=timeout_seconds,
            envs=_parse_envs(envs_json),
            force_new=force_new,
        )
        output = {
            "ok": True,
            "sandbox_id": sandbox.sandbox_id,
            "label": clean_label,
            "template": record.get("template"),
            "region": record.get("region"),
            "status": "running",
            "created": created,
            "expires_at": record.get("expires_at"),
        }
    except Exception as exc:  # noqa: BLE001 - 工具层返回可恢复错误
        output = {"ok": False, "label": clean_label, "error": mask_token(str(exc))}
    if thread_id != MANUAL_THREAD_ID:
        record_event(
            thread_id,
            f"aliyun-sandbox:ensure:{clean_label}",
            "准备阿里云代码沙箱",
            kind="execute",
            status="completed" if output.get("ok") else "error",
            detail=_event_detail(output),
        )
    return output


@tool("run_aliyun_sandbox_command", parse_docstring=True)
def run_aliyun_sandbox_command(
    command: str,
    timeout: int = 30,
    label: str = DEFAULT_SANDBOX_LABEL,
    template_id: str | None = None,
    sandbox_timeout_seconds: int | None = None,
    envs_json: str | None = None,
) -> dict[str, Any]:
    """在当前任务的持久阿里云沙箱中执行 shell 命令。

    Args:
        command: 要在沙箱内执行的 shell 命令。
        timeout: 命令超时时间，单位秒，范围 1 到 300。
        label: 沙箱标签，同一 thread 下默认使用 default。
        template_id: 可选模板名称或 ID，默认读取 ALIYUN_SANDBOX_TEMPLATE_ID。
        sandbox_timeout_seconds: 创建或连接沙箱时的保活时间。
        envs_json: 首次创建沙箱时注入的非敏感环境变量 JSON 对象字符串。

    Returns:
        包含 ok、sandbox_id、exit_code、stdout、stderr、error 的执行结果。
    """

    normalized_command = " ".join((command or "").split())
    if not normalized_command:
        return {"ok": False, "error": "command 不能为空"}
    clean_label = _clean_label(label)
    thread_id = _thread_id()
    if thread_id != MANUAL_THREAD_ID:
        record_event(
            thread_id,
            f"aliyun-sandbox:cmd:{clean_label}:{normalized_command[:60]}",
            "在阿里云沙箱执行命令",
            kind="execute",
            status="in_progress",
            detail=_event_detail({"label": clean_label, "command": normalized_command}),
        )
    try:
        sandbox, _record, created = _connect_or_create_sandbox(
            label=clean_label,
            template_id=template_id,
            timeout_seconds=sandbox_timeout_seconds,
            envs=_parse_envs(envs_json),
        )
        result = _command_result_to_dict(
            sandbox.commands.run(normalized_command, timeout=_command_timeout_seconds(timeout))
        )
        output = {
            "ok": result.get("exit_code") == 0,
            "sandbox_id": sandbox.sandbox_id,
            "label": clean_label,
            "created": created,
            **result,
        }
    except Exception as exc:  # noqa: BLE001 - 工具层返回可恢复错误
        output = {
            "ok": False,
            "sandbox_id": None,
            "label": clean_label,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error": mask_token(str(exc)),
        }
    if thread_id != MANUAL_THREAD_ID:
        record_event(
            thread_id,
            f"aliyun-sandbox:cmd:{clean_label}:{normalized_command[:60]}",
            "在阿里云沙箱执行命令",
            kind="execute",
            status="completed" if output.get("ok") else "error",
            detail=_event_detail(output),
        )
    return output


@tool("run_aliyun_sandbox_code", parse_docstring=True)
def run_aliyun_sandbox_code(
    code: str,
    language: str = "python",
    timeout: int = 30,
    label: str = DEFAULT_SANDBOX_LABEL,
    template_id: str | None = None,
    sandbox_timeout_seconds: int | None = None,
    envs_json: str | None = None,
) -> dict[str, Any]:
    """在当前任务的持久阿里云代码解释器上下文中执行代码。

    Args:
        code: 要执行的代码片段。
        language: 代码语言，默认 python；也可使用 javascript、typescript、bash 等。
        timeout: 代码执行超时时间，单位秒，范围 1 到 300。
        label: 沙箱标签，同一 thread 下默认使用 default。
        template_id: 可选模板名称或 ID，默认读取 ALIYUN_SANDBOX_TEMPLATE_ID。
        sandbox_timeout_seconds: 创建或连接沙箱时的保活时间。
        envs_json: 首次创建沙箱时注入的非敏感环境变量 JSON 对象字符串。

    Returns:
        包含 ok、sandbox_id、stdout、stderr、results、error 的执行结果。
    """

    if not code or not code.strip():
        return {"ok": False, "error": "code 不能为空"}
    clean_label = _clean_label(label)
    safe_language = " ".join((language or "python").split()) or "python"
    thread_id = _thread_id()
    if thread_id != MANUAL_THREAD_ID:
        record_event(
            thread_id,
            f"aliyun-sandbox:code:{clean_label}",
            "在阿里云沙箱执行代码",
            kind="execute",
            status="in_progress",
            detail=_event_detail({"label": clean_label, "language": safe_language, "chars": len(code)}),
        )
    try:
        sandbox, _record, created = _connect_or_create_sandbox(
            label=clean_label,
            template_id=template_id,
            timeout_seconds=sandbox_timeout_seconds,
            envs=_parse_envs(envs_json),
        )
        result = _execution_to_dict(
            sandbox.run_code(code, language=safe_language, timeout=_command_timeout_seconds(timeout))
        )
        output = {
            "sandbox_id": sandbox.sandbox_id,
            "label": clean_label,
            "language": safe_language,
            "created": created,
            **result,
        }
    except Exception as exc:  # noqa: BLE001 - 工具层返回可恢复错误
        output = {
            "ok": False,
            "sandbox_id": None,
            "label": clean_label,
            "language": safe_language,
            "stdout": "",
            "stderr": "",
            "results": [],
            "error": mask_token(str(exc)),
        }
    if thread_id != MANUAL_THREAD_ID:
        record_event(
            thread_id,
            f"aliyun-sandbox:code:{clean_label}",
            "在阿里云沙箱执行代码",
            kind="execute",
            status="completed" if output.get("ok") else "error",
            detail=_event_detail(output),
        )
    return output


@tool("kill_aliyun_sandbox", parse_docstring=True)
def kill_aliyun_sandbox(label: str = DEFAULT_SANDBOX_LABEL) -> dict[str, Any]:
    """销毁当前任务绑定的阿里云沙箱。

    Args:
        label: 要销毁的沙箱标签，同一 thread 下默认使用 default。

    Returns:
        包含 ok、sandbox_id、killed、label 的销毁结果。
    """

    from e2b_code_interpreter import Sandbox

    clean_label = _clean_label(label)
    thread_id = _thread_id()
    record = get_store().get_agent_sandbox(thread_id, clean_label)
    if record is None:
        return {"ok": True, "label": clean_label, "sandbox_id": None, "killed": False}
    sandbox_id = str(record["sandbox_id"])
    try:
        killed = Sandbox.kill(sandbox_id, **_api_params())
        get_store().update_agent_sandbox_status(str(record["id"]), "killed", touch_used=True)
        output = {"ok": True, "label": clean_label, "sandbox_id": sandbox_id, "killed": bool(killed)}
    except Exception as exc:  # noqa: BLE001 - 工具层返回可恢复错误
        output = {
            "ok": False,
            "label": clean_label,
            "sandbox_id": sandbox_id,
            "killed": False,
            "error": mask_token(str(exc)),
        }
    if thread_id != MANUAL_THREAD_ID:
        record_event(
            thread_id,
            f"aliyun-sandbox:kill:{clean_label}",
            "销毁阿里云沙箱",
            kind="execute",
            status="completed" if output.get("ok") else "error",
            detail=_event_detail(output),
        )
    return output
