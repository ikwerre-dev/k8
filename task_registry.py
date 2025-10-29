import threading
import time
import uuid
import os
import hashlib
import random
from typing import Dict, Any, Optional

_lock = threading.Lock()
_tasks: Dict[str, Dict[str, Any]] = {}


def create_task(task_type: str) -> str:
    # Generate timestamp component (microsecond precision)
    timestamp = int(time.time() * 1000000)  # microseconds since epoch
    
    # Generate machine/process identifier
    machine_id = hashlib.md5(f"{os.getpid()}-{os.uname().nodename}".encode()).hexdigest()[:8]
    
    # Generate extended UUID component (full 32 characters)
    uuid_component = uuid.uuid4().hex
    
    # Add additional entropy with random component
    entropy = random.randint(10000, 99999)
    
    # Create longer, more unique task ID
    # Format: {task_type}-{timestamp}-{machine_id}-{uuid}-{entropy}
    task_id = f"{task_type}-{timestamp}-{machine_id}-{uuid_component}-{entropy}"
    
    with _lock:
        _tasks[task_id] = {
            "task_type": task_type,
            "status": "running",
            "events": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    return task_id


def emit_event(task_id: str, event: Dict[str, Any]) -> None:
    event.setdefault("ts", time.time())
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        t["events"].append(event)
        t["updated_at"] = time.time()
    # Print a concise event line to terminal
    try:
        stage = event.get("stage")
        status = event.get("status")
        err = event.get("error")
        extra = []
        if "exit_code" in event:
            extra.append(f"exit_code={event['exit_code']}")
        if "container_id" in event:
            extra.append(f"container_id={event['container_id']}")
        if "status_code" in event:
            extra.append(f"status_code={event['status_code']}")
        if err:
            extra.append(f"error={err}")
        suffix = (" " + " ".join(extra)) if extra else ""
        print(f"[TASK] {task_id} {stage or ''} {status or ''}{suffix}")
    except Exception:
        pass


def set_completed(task_id: str, result: Optional[Dict[str, Any]] = None) -> None:
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        t["status"] = "completed"
        if result is not None:
            t["result"] = result
        t["updated_at"] = time.time()


def set_error(task_id: str, error: str) -> None:
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        t["status"] = "error"
        t["error"] = error
        t["updated_at"] = time.time()


def get(task_id: str) -> Dict[str, Any]:
    with _lock:
        return _tasks.get(task_id, {"error": "not_found"})