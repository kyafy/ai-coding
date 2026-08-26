from __future__ import annotations

import json
import sys
from pathlib import Path

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.env_utils import get_env, require_env
from agent.tools.gitee_api import mask_token


def main() -> None:
    api_key = require_env("E2B_API_KEY")
    api_url = require_env("E2B_API_URL").rstrip("/")
    template = get_env("ALIYUN_SANDBOX_TEMPLATE_ID", "code-interpreter-v1")
    body = {
        "templateID": template,
        "envVars": {"LX_AICODING_MVP": "true"},
        "timeout": 300,
    }
    print("request:")
    print(
        json.dumps(
            {
                "method": "POST",
                "url": f"{api_url}/sandboxes",
                "headers": {"X-API-KEY": "***"},
                "json": body,
                "has_api_key": bool(api_key),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    response = requests.post(
        f"{api_url}/sandboxes",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    try:
        parsed = response.json()
    except ValueError:
        parsed = response.text
    print("response:")
    print(
        json.dumps(
            {
                "status_code": response.status_code,
                "headers": {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() in {"content-type", "date", "server", "x-request-id"}
                },
                "body": mask_token(json.dumps(parsed, ensure_ascii=False) if not isinstance(parsed, str) else parsed),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if response.status_code >= 300:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
