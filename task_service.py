import subprocess
import threading
from typing import Dict, Optional
import requests
from config_service import get_log_endpoint, get_auth_header


def run_task_stream(cmd: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, task_id: Optional[str] = None) -> Dict[str, str]:
    endpoint = get_log_endpoint()
    headers = get_auth_header()

    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd or None,
        env=env or None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    def streamer():
        if not endpoint:
            return
        try:
            for line in proc.stdout:  # type: ignore
                payload = {
                    "task": "generic_task",
                    "task_id": task_id,
                    "cmd": cmd,
                    "line": line.rstrip("\n"),
                }
                try:
                    requests.post(endpoint, json=payload, headers=headers, timeout=3)
                except Exception:
                    pass
        except Exception:
            pass

    t = threading.Thread(target=streamer, daemon=True)
    t.start()
    exit_code = proc.wait()

    if endpoint:
        payload = {
            "task": "generic_task",
            "task_id": task_id,
            "cmd": cmd,
            "status": "completed",
            "exit_code": exit_code,
        }
        try:
            requests.post(endpoint, json=payload, headers=headers, timeout=3)
        except Exception:
            pass

    return {"status": "completed", "exit_code": str(exit_code)}