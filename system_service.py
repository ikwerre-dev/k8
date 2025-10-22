import platform
import socket
import time
import os
from typing import List, Dict, Any

import psutil


def _safe_get_loadavg() -> Dict[str, Any]:
    try:
        if hasattr(os, "getloadavg"):
            la1, la5, la15 = os.getloadavg()
            return {"loadavg": {"1m": la1, "5m": la5, "15m": la15}}
    except Exception:
        pass
    return {"loadavg": None}


def get_system_info() -> Dict[str, Any]:
    """Basic server info: hostname, platform, uptime, CPU cores, memory size."""
    boot_time = psutil.boot_time()
    uptime_seconds = time.time() - boot_time
    mem = psutil.virtual_memory()
    cpu_count_logical = psutil.cpu_count(logical=True) or 0
    cpu_count_physical = psutil.cpu_count(logical=False) or cpu_count_logical
    info = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "uptime_seconds": int(uptime_seconds),
        "cpu": {"logical": cpu_count_logical, "physical": cpu_count_physical},
        "memory": {"total": mem.total, "available": mem.available},
    }
    info.update(_safe_get_loadavg())
    return info


def get_resource_usage() -> Dict[str, Any]:
    """Snapshot of CPU%, memory usage, swap, disk, and network IO."""
    cpu_percent = psutil.cpu_percent(interval=0.2)
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk_root = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    return {
        "cpu_percent": cpu_percent,
        "memory": {
            "total": vm.total,
            "used": vm.used,
            "available": vm.available,
            "percent": vm.percent,
        },
        "swap": {
            "total": swap.total,
            "used": swap.used,
            "free": swap.free,
            "percent": swap.percent,
        },
        "disk_root": {
            "total": disk_root.total,
            "used": disk_root.used,
            "free": disk_root.free,
            "percent": disk_root.percent,
        },
        "net_io": {
            "bytes_sent": net.bytes_sent,
            "bytes_recv": net.bytes_recv,
            "packets_sent": net.packets_sent,
            "packets_recv": net.packets_recv,
        },
    }


def _proc_snapshot(p: psutil.Process) -> Dict[str, Any]:
    try:
        with p.oneshot():
            name = p.name()
            pid = p.pid
            username = None
            try:
                username = p.username()
            except Exception:
                pass
            # short sampling to get a non-zero value
            cpu = p.cpu_percent(interval=0.1)
            mem_percent = p.memory_percent()
            mem_info = p.memory_info()
            cmdline = []
            try:
                cmdline = p.cmdline()
            except Exception:
                pass
            return {
                "pid": pid,
                "name": name,
                "username": username,
                "cpu_percent": round(cpu, 2),
                "memory_percent": round(mem_percent, 2),
                "rss": mem_info.rss,
                "cmdline": cmdline,
            }
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return {}


def top_processes(sort_by: str = "cpu", limit: int = 10) -> List[Dict[str, Any]]:
    """Return top processes by CPU or memory usage."""
    sort_key = "cpu_percent" if sort_by.lower() == "cpu" else "memory_percent"
    snapshots: List[Dict[str, Any]] = []
    for p in psutil.process_iter(attrs=["pid", "name"]):
        snap = _proc_snapshot(p)
        if snap:
            snapshots.append(snap)
    snapshots.sort(key=lambda x: x.get(sort_key, 0.0), reverse=True)
    return snapshots[:limit]