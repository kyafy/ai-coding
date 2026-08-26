from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.env_utils import get_env, require_env
from agent.tools.gitee_api import mask_token


def _result_to_dict(result: Any) -> dict[str, Any]:
    """Convert SDK command results into a compact, stable debug payload."""

    return {
        "exit_code": getattr(result, "exit_code", None),
        "stdout": str(getattr(result, "stdout", "") or "").strip(),
        "stderr": mask_token(str(getattr(result, "stderr", "") or "").strip()),
        "error": mask_token(str(getattr(result, "error", "") or "").strip()),
    }


def main() -> None:
    """MVP verification for Aliyun AgentSandbox E2B-compatible API.

    This script creates one sandbox from the configured template, runs a tiny
    Python command, prints sanitized input/output, and always attempts cleanup.
    """

    api_key = require_env("E2B_API_KEY")
    api_url = require_env("E2B_API_URL")
    domain = require_env("E2B_DOMAIN")
    template = get_env("ALIYUN_SANDBOX_TEMPLATE_ID", "code-interpreter-v1")

    try:
        from e2b_code_interpreter import Sandbox
    except ImportError as exc:
        raise RuntimeError("缺少 e2b-code-interpreter 依赖，请先运行：uv sync") from exc

    sandbox = None
    command = "python3 -c \"import json, platform; print(json.dumps({'ok': True, 'python': platform.python_version()}))\""
    payload = {
        "template": template,
        "api_url": api_url,
        "domain": domain,
        "command": command,
        "has_api_key": bool(api_key),
    }
    print("request:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    try:
        try:
            sandbox = Sandbox.create(
                template=template,
                api_key=api_key,
                api_url=api_url,
                domain=domain,
                envs={"LX_AICODING_MVP": "true"},
            )
            result = sandbox.commands.run(command, timeout=30)
            output = {"ok": True, **_result_to_dict(result)}
        finally:
            if sandbox is not None:
                sandbox.kill()
    except Exception as exc:  # noqa: BLE001 - MVP 脚本需要直接展示远端接口错误
        output = {
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error": mask_token(str(exc)),
        }

    print("response:")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if not output["ok"] or output["exit_code"] not in {0, None}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
