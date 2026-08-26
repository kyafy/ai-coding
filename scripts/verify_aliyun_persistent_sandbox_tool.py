from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.tools import (
    ensure_aliyun_code_sandbox,
    kill_aliyun_sandbox,
    run_aliyun_sandbox_code,
    run_aliyun_sandbox_command,
)


def _assert_ok(name: str, result: dict) -> None:
    if not result.get("ok"):
        raise AssertionError(f"{name} failed: {result}")


def main() -> None:
    label = "persistent-mvp"

    sandbox = ensure_aliyun_code_sandbox.invoke(
        {"label": label, "force_new": True, "timeout_seconds": 600}
    )
    _assert_ok("ensure", sandbox)
    sandbox_id = sandbox["sandbox_id"]

    first = run_aliyun_sandbox_code.invoke(
        {
            "label": label,
            "code": "x = 41\nprint('stored', x)",
            "language": "python",
            "timeout": 30,
        }
    )
    _assert_ok("first code run", first)

    second = run_aliyun_sandbox_code.invoke(
        {
            "label": label,
            "code": "print('answer', x + 1)",
            "language": "python",
            "timeout": 30,
        }
    )
    _assert_ok("second code run", second)
    if "answer 42" not in str(second.get("stdout")):
        raise AssertionError(f"persistent Python context was not reused: {second}")
    if second.get("sandbox_id") != sandbox_id:
        raise AssertionError("sandbox_id changed between code runs")

    command = run_aliyun_sandbox_command.invoke(
        {"label": label, "command": "python3 --version", "timeout": 30}
    )
    _assert_ok("command run", command)
    if command.get("sandbox_id") != sandbox_id:
        raise AssertionError("sandbox_id changed for command run")

    killed = kill_aliyun_sandbox.invoke({"label": label})
    _assert_ok("kill", killed)

    print("persistent aliyun sandbox tool verification passed")
    print({"sandbox_id": sandbox_id, "python": command.get("stdout")})


if __name__ == "__main__":
    main()
