from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import Optional, Dict, List, Any
import os
import time
import threading
import json
import shutil
import paramiko
import posixpath
import subprocess

import docker_service as ds
import system_service as sys
import nginx_service as ns
import task_service as ts
import db_service as db
 
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        db.init_db()
        conn = db.get_conn()
        cur = conn.execute("PRAGMA quick_check")
        row = cur.fetchone()
        status = row[0] if row else None
        if status != "ok":
            raise RuntimeError("database integrity check failed")
        conn.execute("SELECT COUNT(*) FROM applications")
        conn.execute("SELECT COUNT(*) FROM metrics")
        try:
            _tmp_sweeper_stop.clear()
        except Exception:
            pass
        sweeper_thread = threading.Thread(target=_tmp_sweeper_worker, daemon=True)
        sweeper_thread.start()
        try:
            _tmp_root_sweeper_stop.clear()
        except Exception:
            pass
        root_sweeper_thread = threading.Thread(target=_tmp_root_sweeper_worker, daemon=True)
        root_sweeper_thread.start()
        try:
            app.state.tmp_sweeper_thread = sweeper_thread
            app.state.tmp_root_sweeper_thread = root_sweeper_thread
        except Exception:
            pass
        yield
        try:
            _tmp_sweeper_stop.set()
            sweeper_thread.join(timeout=2.0)
        except Exception:
            pass
        try:
            _tmp_root_sweeper_stop.set()
            root_sweeper_thread.join(timeout=2.0)
        except Exception:
            pass
    except Exception as e:
        raise RuntimeError(f"database startup failed: {e}")

app = FastAPI(title="Docker Manager API", lifespan=lifespan)


# Helper to resolve build.info.json across candidate directories
def _resolve_summary_path(task_id: str):
    candidates = ["/app/upload", "/upload/pxxl", "/uploads"]
    fallback = os.path.abspath(os.path.join(os.path.dirname(__file__), "builds"))
    search_dirs = [p for p in candidates if os.path.isdir(p)]
    if fallback not in search_dirs:
        search_dirs.append(fallback)
    for base_dir in search_dirs:
        builds_dir = os.path.join(base_dir, task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
        if os.path.exists(summary_path):
            return summary_path, builds_dir
    base_dir = search_dirs[0] if search_dirs else fallback
    builds_dir = os.path.join(base_dir, task_id)
    summary_path = os.path.join(builds_dir, "build.info.json")
    return summary_path, builds_dir

_tmp_sweeper_stop = threading.Event()
_tmp_root_sweeper_stop = threading.Event()

def _tmp_sweeper_worker():
    try:
        while not _tmp_sweeper_stop.is_set():
            try:
                _sweep_tmp_docker_builds(20)
            except Exception:
                pass
            for _ in range(60):
                if _tmp_sweeper_stop.is_set():
                    break
                time.sleep(1)
    except Exception:
        pass

def _sweep_tmp_docker_builds(max_age_minutes: int = 20):
    try:
        base = "/tmp/docker-builds"
        now = time.time()
        max_age = float(max_age_minutes or 20) * 60.0
        if not os.path.isdir(base):
            return
        for name in os.listdir(base):
            p = os.path.join(base, name)
            try:
                if not os.path.isdir(p):
                    continue
                try:
                    from task_registry import get as _get_task
                    t = _get_task(name)
                    if isinstance(t, dict) and str(t.get("status")).lower() == "running":
                        continue
                except Exception:
                    pass
                m = os.path.getmtime(p)
                c = os.path.getctime(p)
                age = now - max(m, c)
                if age > max_age:
                    shutil.rmtree(p)
            except Exception:
                pass
    except Exception:
        pass


def _tmp_root_sweeper_worker():
    try:
        while not _tmp_root_sweeper_stop.is_set():
            try:
                _sweep_tmp_root_build_dirs(15)
            except Exception:
                pass
            for _ in range(30):
                if _tmp_root_sweeper_stop.is_set():
                    break
                time.sleep(1)
    except Exception:
        pass


def _sweep_tmp_root_build_dirs(max_age_minutes: int = 15):
    try:
        base = "/tmp"
        now = time.time()
        max_age = float(max_age_minutes or 15) * 60.0
        if not os.path.isdir(base):
            return
        for name in os.listdir(base):
            try:
                if not isinstance(name, str):
                    continue
                if not name.startswith("pxxl-docker_build-"):
                    continue
                p = os.path.join(base, name)
                if not os.path.isdir(p):
                    continue
                try:
                    for root, _, files in os.walk(p):
                        for fn in files:
                            try:
                                if not isinstance(fn, str):
                                    continue
                                if not fn.endswith(".tar"):
                                    continue
                                fp = os.path.join(root, fn)
                                try:
                                    m = os.path.getmtime(fp)
                                    c_file = os.path.getctime(fp)
                                    age_file = now - max(float(m), float(c_file))
                                except Exception:
                                    continue
                                if age_file > max_age:
                                    try:
                                        os.remove(fp)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                except Exception:
                    pass
                c = os.path.getctime(p)
                age = now - float(c)
                if age > max_age:
                    shutil.rmtree(p)
            except Exception:
                pass
    except Exception:
        pass

def _append_error_log(msg: str):
    try:
        p = os.path.join(os.path.dirname(__file__), "log.txt")
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        with open(p, "a") as f:
            f.write(f"[{now}] {msg}\n")
    except Exception:
        pass

class BuildRequest(BaseModel):
    path: str = "."
    tag: Optional[str] = None

class RunRequest(BaseModel):
    image: str
    name: Optional[str] = None
    command: Optional[str] = None
    ports: Optional[Dict[str, int]] = None  # e.g., {"8000/tcp": 8000}
    env: Optional[Dict[str, str]] = None
    detach: bool = True
    cpu: Optional[float] = None  # CPUs to allocate (e.g., 0.5, 1.0)
    cpuset: Optional[str] = None  # CPU set string, e.g., "0-2"
    memory: Optional[str] = None  # Mem limit, e.g., "512m" or "1g"
    app_id: Optional[str] = None  # app identifier for container naming
    task_id: Optional[str] = None  # task identifier for container naming

class VolumeItem(BaseModel):
    name: str
    mount_path: str
    mode: Optional[str] = "rw"
    limit_mb: Optional[int] = None

class LocalRunRequest(BaseModel):
    lz4_path: str  # local relative path to .lz4 (or .tar.lz4)
    task_id: str  # task/build id used for logging dir
    app_id: Optional[str] = None  # app identifier for container naming
    ports: Optional[Dict[str, int]] = None
    env: Optional[Dict[str, str]] = None
    labels: Optional[Dict[str, str]] = None
    command: Optional[str] = None
    name: Optional[str] = None
    cpu: Optional[float] = None
    cpuset: Optional[str] = None
    memory: Optional[str] = None
    # Optional persistent storage to attach at run
    volume_name: Optional[str] = None
    mount_path: Optional[str] = None
    mode: Optional[str] = "rw"
    volumes: Optional[List[VolumeItem]] = None

# --- Network management --- 
class NetworkCreateRequest(BaseModel):
    name: str
    driver: Optional[str] = "bridge"

@app.post("/docker/network/create")
def docker_network_create(req: NetworkCreateRequest):
    try:
        return ds.create_network(name=req.name, driver=req.driver or "bridge")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class ContainerResourceUpdateRequest(BaseModel):
    task_id: Optional[str] = None
    container_id_or_name: Optional[str] = None
    memory: Optional[str] = None  # e.g., "512m", "1g"
    cpu: Optional[float] = None  # CPUs to allocate (e.g., 0.5, 1.0)
    cpu_shares: Optional[int] = None  # CPU shares (relative weight)
    pids_limit: Optional[int] = None  # Process limit
    cpuset_cpus: Optional[str] = None  # CPU set string, e.g., "0-2"
    cpuset_mems: Optional[str] = None  # Memory nodes, e.g., "0-1"
    memswap_limit: Optional[str] = None  # Memory + swap limit
    recreate_on_conflict: Optional[bool] = True

@app.post("/docker/container/update-resources")
def docker_container_update_resources(req: ContainerResourceUpdateRequest):
    try:
        # Determine container ID/name
        container_id = req.container_id_or_name
        
        if not container_id and req.task_id:
            # Get container from task_id
            summary_path, builds_dir = _resolve_summary_path(req.task_id)
            if not os.path.exists(summary_path):
                raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
            with open(summary_path, "r") as f:
                summary_obj = json.load(f)
            container_id = summary_obj.get("container_name") or summary_obj.get("container_id")
            if not container_id:
                upstream = (summary_obj.get("upstream", {}) or {})
                container_id = upstream.get("upstream_host")
        
        if not container_id:
            raise HTTPException(status_code=400, detail="container_id_or_name or task_id with valid container required")
        
        # Convert CPU to nano_cpus if provided
        nano_cpus = None
        if req.cpu is not None:
            nano_cpus = int(float(req.cpu) * 1_000_000_000)
        
        # Update container resources with conflict handling
        try:
            result = ds.update_container_resources(
                id_or_name=container_id,
                mem_limit=req.memory,
                nano_cpus=nano_cpus,
                cpu_shares=req.cpu_shares,
                pids_limit=req.pids_limit,
                cpuset_cpus=req.cpuset_cpus,
                cpuset_mems=req.cpuset_mems,
                memswap_limit=req.memswap_limit
            )
            status = "updated"
            new_container_id = result.get("id")
            new_container_name = None
        except Exception as e:
            msg = str(e)
            conflict = ("Conflicting options" in msg and "NanoCPUs" in msg) or ("409" in msg and "NanoCPUs" in msg)
            if conflict and req.recreate_on_conflict:
                run_res = ds.recreate_with_resources(
                    id_or_name=container_id,
                    mem_limit=req.memory,
                    nano_cpus=nano_cpus,
                    cpu_shares=req.cpu_shares,
                    cpuset_cpus=req.cpuset_cpus,
                    cpuset_mems=req.cpuset_mems,
                    memswap_limit=req.memswap_limit,
                )
                status = "recreated"
                new_container_id = run_res.get("id")
                new_container_name = run_res.get("name")
            else:
                raise
        
        # Log the update if task_id is provided
        if req.task_id:
            try:
                summary_path, builds_dir = _resolve_summary_path(req.task_id)
                
                # Log to events.log
                events_log_path = os.path.join(builds_dir, "events.log")
                with open(events_log_path, "a") as f:
                    f.write(f"Container resource {status}: {container_id}\n")
                    if req.memory:
                        f.write(f"  Memory limit: {req.memory}\n")
                    if req.cpu:
                        f.write(f"  CPU limit: {req.cpu}\n")
                    if req.cpu_shares:
                        f.write(f"  CPU shares: {req.cpu_shares}\n")
                    if req.cpuset_cpus:
                        f.write(f"  CPU set: {req.cpuset_cpus}\n")
                
                # Update summary with new resource limits
                if os.path.exists(summary_path):
                    with open(summary_path, "r") as f:
                        summary_obj = json.load(f)
                    
                    localrun = summary_obj.get("localrun", {}) or {}
                    resources = localrun.get("resources", {}) or {}
                    
                    if req.memory:
                        resources["memory"] = req.memory
                    if req.cpu:
                        resources["cpu"] = req.cpu
                    if req.cpu_shares:
                        resources["cpu_shares"] = req.cpu_shares
                    if req.cpuset_cpus:
                        resources["cpuset"] = req.cpuset_cpus
                    if new_container_id:
                        summary_obj["container_id"] = new_container_id
                    if new_container_name:
                        summary_obj["container_name"] = new_container_name
                    
                    localrun["resources"] = resources
                    summary_obj["localrun"] = localrun
                    
                    with open(summary_path, "w") as f:
                        json.dump(summary_obj, f, indent=2)
            except Exception:
                pass  # Don't fail the update if logging fails
        
        return {
            "status": status,
            "container_id": new_container_id,
            "task_id": req.task_id,
            "resources": {
                "memory": req.memory,
                "cpu": req.cpu,
                "cpu_shares": req.cpu_shares,
                "cpuset_cpus": req.cpuset_cpus,
                "cpuset_mems": req.cpuset_mems,
                "pids_limit": req.pids_limit,
                "memswap_limit": req.memswap_limit
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) 

@app.post("/docker/localrun")
def docker_localrun(req: LocalRunRequest):
    try:
        res = ds.local_run_from_lz4(
            lz4_path_rel=req.lz4_path,
            task_id=req.task_id,
            app_id=req.app_id,
            ports=req.ports,
            env=req.env,
            labels=req.labels,
            command=req.command,
            name=req.name,
            cpu=req.cpu,
            cpuset=req.cpuset,
            memory=req.memory,
            volume_name=req.volume_name,
            mount_path=req.mount_path,
            mode=req.mode or "rw",
        )
        try:
            aid = req.app_id or (res.get("app_id") if isinstance(res, dict) else None) or (res.get("container_name") if isinstance(res, dict) else None) or req.name
            cid = (res.get("container_id") if isinstance(res, dict) else None) or ((res.get("container") or {}).get("id") if isinstance(res, dict) else None)
            if aid:
                db.upsert_application(aid, cid)
                if cid:
                    try:
                        stats = ds.get_container_stats(cid)
                        cpu = _cpu_percent(stats)
                        ram = _mem_usage(stats)
                        db.add_metric(aid, cpu=cpu, ram=ram)
                    except Exception:
                        pass
        except Exception:
            pass
        return res
    except Exception as e:
        try:
            _append_error_log(f"docker_localrun error task_id={req.task_id} detail={e}")
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=str(e))

class NginxSignDomainRequest(BaseModel):
    task_id: str
    app_id: Optional[str] = None
    old_container: Optional[str] = None
    old_task_id: Optional[str] = None
    new_container: Optional[str] = None
    port: Optional[int] = None

@app.post("/nginx/sign-domain")
def nginx_sign_domain(req: NginxSignDomainRequest):
    try:
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        try:
            with open(summary_path, "r") as f:
                summary_obj = json.load(f)
        except Exception:
            summary_obj = {}
        os.makedirs(builds_dir, exist_ok=True)
        import time
        events_log_path = os.path.join(builds_dir, "events.log")
        build_log_path = os.path.join(builds_dir, "build.log")
        build_structured_path = os.path.join(builds_dir, "build.jsonl")
        sign_error = False
        def _append_line(path: str, msg: str):
            try:
                now = time.strftime('%Y-%m-%d %H:%M:%S')
                with open(path, "a") as f:
                    f.write(f"[{now}] {msg}\n")
            except Exception:
                pass
        def _append_build(path: str, msg: str, level: str = "info"):
            try:
                now = time.strftime('%Y-%m-%d %H:%M:%S')
                prefix = "[ERROR]" if str(level).lower() == "error" else "[INFO ]"
                with open(path, "a") as f:
                    f.write(f"[{now}] {prefix} {msg}\n")
            except Exception:
                pass
        def _append_json(path: str, obj: dict):
            try:
                with open(path, "a") as f:
                    f.write(json.dumps(obj) + "\n")
            except Exception:
                pass
        def _write_json(path: str, obj: dict):
            try:
                with open(path, "w") as f:
                    json.dump(obj, f, indent=2)
            except Exception:
                pass
        def _ts() -> str:
            return time.strftime('%Y-%m-%dT%H:%M:%S')
        _append_line(
            events_log_path,
            f"Uploading stage completed: task_id={req.task_id} app_id={req.app_id or 'unknown'}. Artifact uploaded successfully; preparing to start signing stage."
        )
        _append_build(
            build_log_path,
            f"Uploading stage completed. Next stage: signing. task_id={req.task_id} app_id={req.app_id or 'unknown'}"
        )
        _append_json(
            build_structured_path,
            {
                "ts": _ts(),
                "level": "info",
                "event": "upload_completed",
                "stage": "uploading",
                "status": "completed",
                "next_stage": "signing",
                "task_id": req.task_id,
                "app_id": req.app_id,
                "command": "pxxl launch upload",
                "brand": "pxxl",
            }
        )
        # Infer app_id if missing
        if not req.app_id:
            build_jsonl_path = os.path.join(builds_dir, "build.jsonl")
            if os.path.exists(build_jsonl_path):
                try:
                    with open(build_jsonl_path, "r") as f:
                        lines = f.readlines()
                    for line in reversed(lines[-500:]):
                        try:
                            ev = json.loads(line.strip())
                        except Exception:
                            continue
                        aid = ev.get("app_id")
                        if aid:
                            req.app_id = aid
                            break
                except Exception:
                    pass
            if not req.app_id and summary_obj and isinstance(summary_obj.get("tag"), str):
                tag_val = summary_obj.get("tag")
                if ":" in tag_val:
                    req.app_id = tag_val.split(":", 1)[0]
        if not req.app_id:
            raise HTTPException(status_code=400, detail=f"app_id not found; provide app_id or ensure {summary_path} contains app_id")
        # Derive container name if missing (use app_id-task_id for internal network name)
        container_name = req.new_container or None
        if not container_name and req.app_id and req.task_id:
            container_name = f"{req.app_id}-{req.task_id}"

        # Only update status and logs; no Nginx or Certbot actions
        try:
            summary_obj.update({"status": "running", "stage": "signing"})
            _write_json(summary_path, summary_obj)
        except Exception:
            pass
        # Include domain if available in summary
        domain = summary_obj.get("domain") or summary_obj.get("nginx_domain")
        _append_line(
            events_log_path,
            (
                f"pxxl signing: starting for app_id={req.app_id} task_id={req.task_id}. "
                f"domain={domain or 'not set'} target_container={container_name or 'n/a'}"
            )
        )
        _append_build(
            build_log_path,
            f"pxxl signing: started. domain={domain or 'not set'} target_container={container_name or 'n/a'}"
        )
        _append_json(
            build_structured_path,
            {
                "ts": _ts(),
                "level": "info",
                "event": "signing_started",
                "stage": "signing",
                "status": "in_progress",
                "task_id": req.task_id,
                "app_id": req.app_id,
                "container": container_name,
                "port": req.port,
                "old_container": req.old_container,
                "domain": domain,
                "command": "pxxl launch sign-domain",
                "brand": "pxxl",
                "note": "Status-only signing; sanitized logs without sensitive endpoints.",
            }
        )

        _append_line(
            events_log_path,
            f"pxxl signing: completed for app_id={req.app_id} task_id={req.task_id}. domain={domain or 'not set'}"
        )
        _append_build(
            build_log_path,
            f"pxxl signing: completed successfully. active_container={container_name or 'n/a'}"
        )
        _append_json(
            build_structured_path,
            {
                "ts": _ts(),
                "level": "info",
                "event": "signing_completed",
                "stage": "signing",
                "status": "completed",
                "task_id": req.task_id,
                "app_id": req.app_id,
                "container": container_name,
                "domain": domain,
                "command": "pxxl launch sign-domain",
                "brand": "pxxl",
            }
        )
        _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "signing_completed"})
        try:
            summary_obj.update({"status": "completed", "stage": "signed"})
            _write_json(summary_path, summary_obj)
        except Exception:
            pass

        # Stop old container if provided, then remove it
        old_stop = None
        old_remove = None
        if req.old_container:
            try:
                _append_line(events_log_path, f"stopping old container {req.old_container}")
                _append_build(build_log_path, f"stopping old container {req.old_container}")
                old_stop = ds.stop_container(req.old_container)
                try:
                    st = (old_stop or {})
                    if st.get("status") == "not_found":
                        _append_line(events_log_path, f"skip stop: container not found {req.old_container}")
                        _append_build(build_log_path, f"skip stop: container not found {req.old_container}")
                    elif st.get("status") == "error":
                        _append_line(events_log_path, f"stop warning: {st.get('error')}")
                        _append_build(build_log_path, f"stop warning: {st.get('error')}", level="error")
                    else:
                        _append_line(events_log_path, f"stopped old container {req.old_container}")
                        _append_build(build_log_path, f"stopped old container {req.old_container}")
                except Exception:
                    pass
            except Exception as e:
                old_stop = {"error": str(e)}
                _append_line(events_log_path, f"stop old container error: {e}")
                _append_build(build_log_path, f"stop old container error: {e}", level="error")
            try:
                _append_line(events_log_path, f"removing old container {req.old_container}")
                _append_build(build_log_path, f"removing old container {req.old_container}")
                old_remove = ds.remove_container(req.old_container, force=True)
                try:
                    if isinstance(old_remove, dict) and old_remove.get("status") == "removal_in_progress":
                        _append_line(events_log_path, f"removal in progress for {req.old_container}")
                        _append_build(build_log_path, f"removal in progress for {req.old_container}")
                    elif isinstance(old_remove, dict) and old_remove.get("removed"):
                        _append_line(events_log_path, f"removed old container {req.old_container}")
                        _append_build(build_log_path, f"removed old container {req.old_container}")
                    else:
                        _append_line(events_log_path, f"remove old container returned: {old_remove}")
                        _append_build(build_log_path, f"remove old container returned: {old_remove}")
                except Exception:
                    pass
            except Exception as e:
                old_remove = {"error": str(e)}
                _append_line(events_log_path, f"remove old container error: {e}")
                _append_build(build_log_path, f"remove old container error: {e}", level="error")
        elif req.old_task_id:
            try:
                old_summary_path, old_builds_dir = _resolve_summary_path(req.old_task_id)
                if os.path.exists(old_summary_path):
                    with open(old_summary_path, "r") as f:
                        old_summary = json.load(f)
                    old_cid = old_summary.get("container_name") or old_summary.get("container_id")
                    if not old_cid:
                        old_up = (old_summary.get("upstream", {}) or {})
                        old_cid = old_up.get("upstream_host")
                    if old_cid:
                        try:
                            _append_line(events_log_path, f"stopping old container {old_cid}")
                            _append_build(build_log_path, f"stopping old container {old_cid}")
                            old_stop = ds.stop_container(old_cid)
                            try:
                                st = (old_stop or {})
                                if st.get("status") == "not_found":
                                    _append_line(events_log_path, f"skip stop: container not found {old_cid}")
                                    _append_build(build_log_path, f"skip stop: container not found {old_cid}")
                                elif st.get("status") == "error":
                                    _append_line(events_log_path, f"stop warning: {st.get('error')}")
                                    _append_build(build_log_path, f"stop warning: {st.get('error')}", level="error")
                                else:
                                    _append_line(events_log_path, f"stopped old container {old_cid}")
                                    _append_build(build_log_path, f"stopped old container {old_cid}")
                            except Exception:
                                pass
                        except Exception as e:
                            old_stop = {"error": str(e)}
                            _append_line(events_log_path, f"stop old container error: {e}")
                            _append_build(build_log_path, f"stop old container error: {e}", level="error")
                        try:
                            _append_line(events_log_path, f"removing old container {old_cid}")
                            _append_build(build_log_path, f"removing old container {old_cid}")
                            old_remove = ds.remove_container(old_cid, force=True)
                            try:
                                if isinstance(old_remove, dict) and old_remove.get("status") == "removal_in_progress":
                                    _append_line(events_log_path, f"removal in progress for {old_cid}")
                                    _append_build(build_log_path, f"removal in progress for {old_cid}")
                                elif isinstance(old_remove, dict) and old_remove.get("removed"):
                                    _append_line(events_log_path, f"removed old container {old_cid}")
                                    _append_build(build_log_path, f"removed old container {old_cid}")
                                else:
                                    _append_line(events_log_path, f"remove old container returned: {old_remove}")
                                    _append_build(build_log_path, f"remove old container returned: {old_remove}")
                            except Exception:
                                pass
                        except Exception as e:
                            old_remove = {"error": str(e)}
                            _append_line(events_log_path, f"remove old container error: {e}")
                            _append_build(build_log_path, f"remove old container error: {e}", level="error")
            except Exception as e:
                old_stop = old_stop or {"error": str(e)}
                old_remove = old_remove or {"error": str(e)}
        images_prune = None
        try:
            images_prune = ds.prune_images()
        except Exception:
            pass
        # After signing-related actions, finalize status: remain running until here
        try:
            summary_obj.update({"status": "completed", "stage": "signed"})
            with open(summary_path, "w") as f:
                json.dump(summary_obj, f, indent=2)
        except Exception:
            pass

        # Cleanup: delete build.tar in the upload/task_id directory after signing
        try:
            build_tar_path = os.path.join(builds_dir, "build.tar")
            if os.path.exists(build_tar_path):
                os.remove(build_tar_path)
                _append_line(events_log_path, f"pxxl signing: cleanup removed build.tar at {build_tar_path}")
                _append_build(build_log_path, f"cleanup: removed build.tar")
                _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "cleanup_build_tar", "removed": build_tar_path})
            else:
                _append_line(events_log_path, f"pxxl signing: cleanup skipped, build.tar not found")
                _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "cleanup_build_tar_skipped"})
        except Exception as ce:
            _append_line(events_log_path, f"pxxl signing: cleanup warning {ce}")
            _append_build(build_log_path, f"cleanup warning: {ce}", level="error")
            _append_json(build_structured_path, {"ts": _ts(), "level": "warn", "event": "cleanup_warning", "error": str(ce)})
        return {
            "stage": "signing_domain",
            "status": "completed",
            "upstream_host": container_name,
            "old_stop": old_stop,
            "old_remove": old_remove,
            "images_prune": images_prune,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

 

@app.get("/terminal/test")
def terminal_test(id: str, cmd: Optional[str] = "/bin/bash"):
    html = """
<!doctype html>
<html>
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
<style>
html,body{height:100%;margin:0;padding:0;background:#111;color:#ddd;font-family:system-ui}
#term{height:80vh;overflow:auto;white-space:pre;outline:none;border:1px solid #333;padding:8px;font-family:monospace;cursor:text}
#term:focus{border-color:#555}
#bar{display:flex;gap:8px;align-items:center;padding:8px;border-top:1px solid #333}
#bar input{flex:1;background:#111;color:#ddd;border:1px solid #333;padding:6px}
#bar button{background:#222;color:#ddd;border:1px solid #333;padding:6px 10px;cursor:pointer}
#status{padding:6px 8px;color:#aaa;font-size:12px}
</style>
</head>
<body>
<div id=\"status\">WS: connecting...</div>
<div id=\"term\" tabindex=\"0\" contenteditable=\"true\"></div>
<div id=\"bar\">
  <input id=\"cmdInput\" type=\"text\" placeholder=\"Type a command (fallback)\" />
  <button id=\"sendBtn\">Send</button>
  <button id=\"enterBtn\">Enter</button>
  <button id=\"clearBtn\">Clear Output</button>
  <button id=\"focusBtn\">Focus Terminal</button>
</div>
<script>
const params = new URLSearchParams(window.location.search);
const id = params.get('id') || '';
const cmd = params.get('cmd') || '/bin/bash';
const proto = location.protocol === 'https:' ? 'wss' : 'ws';
const url = proto + '://' + location.host + '/terminal/' + id + '?cmd=' + encodeURIComponent(cmd);
let ws = null;
const termEl = document.getElementById('term');
const statusEl = document.getElementById('status');
const cmdInput = document.getElementById('cmdInput');
const sendBtn = document.getElementById('sendBtn');
const enterBtn = document.getElementById('enterBtn');
const clearBtn = document.getElementById('clearBtn');
const focusBtn = document.getElementById('focusBtn');
function appendText(t){ termEl.textContent += t; termEl.scrollTop = termEl.scrollHeight; }
termEl.addEventListener('click', function(){ termEl.focus(); });
termEl.focus();
let execId = null;
function connect(){
  ws = new WebSocket(url);
  ws.binaryType = 'arraybuffer';
  ws.onopen = function(){ statusEl.textContent = 'WS: connected'; termEl.focus(); cmdInput.focus(); };
  ws.onmessage = function(ev){
    const d = ev.data;
    if (typeof d === 'string' && d.startsWith('{')) {
      try { const obj = JSON.parse(d); if (obj.exec_id) execId = obj.exec_id; } catch(e) {}
      return;
    }
    appendText(typeof d === 'string' ? d : new TextDecoder().decode(d));
  };
  ws.onclose = function(){ statusEl.textContent = 'WS: disconnected (reconnecting...)'; setTimeout(connect, 1000); };
}
connect();
termEl.addEventListener('beforeinput', function(ev){
  const t = ev.inputType;
  if(t==='insertParagraph' || t==='insertLineBreak'){ ev.preventDefault(); appendText('\\n'); if(ws && ws.readyState===1){ ws.send('\\n'); } return; }
  if(ev.data){ ev.preventDefault(); appendText(ev.data); if(ws && ws.readyState===1){ ws.send(ev.data); } }
});
termEl.addEventListener('keydown', function(ev){
  if(ev.key==='Enter'){ ev.preventDefault(); appendText('\\n'); if(ws && ws.readyState===1){ ws.send('\\n'); } return; }
  if(!ws || ws.readyState!==1) return;
  if(ev.key==='Tab'){ ws.send('\\\\t'); ev.preventDefault(); return; }
  if(ev.key==='Backspace'){ termEl.textContent = termEl.textContent.slice(0, -1); ws.send('\\\\u007f'); ev.preventDefault(); return; }
  if(ev.key==='ArrowUp'){ ws.send('\\\\u001b[A'); ev.preventDefault(); return; }
  if(ev.key==='ArrowDown'){ ws.send('\\\\u001b[B'); ev.preventDefault(); return; }
  if(ev.key==='ArrowLeft'){ ws.send('\\\\u001b[D'); ev.preventDefault(); return; }
  if(ev.key==='ArrowRight'){ ws.send('\\\\u001b[C'); ev.preventDefault(); return; }
  if(ev.key.length===1){ appendText(ev.key); ws.send(ev.key); ev.preventDefault(); }
});
window.addEventListener('resize', function(){ if (execId) fetch('/terminal/resize', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ exec_id: execId, width: 120, height: 30 }) }); });
sendBtn.addEventListener('click', function(){ if(ws && ws.readyState===1){ ws.send(cmdInput.value + '\\n'); cmdInput.value=''; } });
cmdInput.addEventListener('keydown', function(ev){ if(ev.key==='Enter'){ ev.preventDefault(); if(ws && ws.readyState===1){ ws.send(cmdInput.value + '\\n'); cmdInput.value=''; } } });
enterBtn.addEventListener('click', function(){ if(ws && ws.readyState===1){ ws.send('\\n'); } });
clearBtn.addEventListener('click', function(){ termEl.textContent = ''; });
focusBtn.addEventListener('click', function(){ termEl.focus(); });
</script>
</body>
</html>
"""
    return HTMLResponse(content=html)

@app.get("/terminal/xterm")
def terminal_xterm(id: str, cmd: Optional[str] = "/bin/bash"):
    html = """
<!doctype html>
<html>
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
<link rel=\"stylesheet\" href=\"https://cdn.jsdelivr.net/npm/xterm@4.5.0/css/xterm.css\" />
<style>
html,body{height:100%;margin:0;padding:0;background:#111;color:#ddd;font-family:system-ui}
#xterm{height:90vh;border:1px solid #333;margin:8px;outline:none;cursor:text}
</style>
</head>
<body>
<div id=\"xterm\" tabindex=\"0\"></div>
<script src=\"https://cdn.jsdelivr.net/npm/xterm@4.5.0/lib/xterm.js\"></script>
<script src=\"https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.3.0/lib/xterm-addon-fit.js\"></script>
<script>
var params = new URLSearchParams(window.location.search);
var id = params.get('id') || '';
var cmd = params.get('cmd') || '/bin/bash';
var proto = location.protocol === 'https:' ? 'wss' : 'ws';
var url = proto + '://' + location.host + '/terminal/' + id + '?cmd=' + encodeURIComponent(cmd);
var container = document.getElementById('xterm');
var term = new window.Terminal({ cursorBlink: true });
var fit = new window.FitAddon.FitAddon();
term.loadAddon(fit);
term.open(container);
fit.fit();
var ws = null;
var execId = null;
function connect(){
  ws = new WebSocket(url);
  ws.binaryType = 'arraybuffer';
  ws.onopen = function(){ term.focus(); try{ ws.send('\r'); }catch(e){} };
  ws.onmessage = function(ev){
    var d = ev.data;
    if (typeof d === 'string' && d.indexOf('{') === 0) {
      try { var obj = JSON.parse(d); if (obj.exec_id) execId = obj.exec_id; } catch(e) {}
    } else {
      term.write(typeof d === 'string' ? d : new TextDecoder().decode(d));
    }
  };
  ws.onclose = function(){ setTimeout(connect, 1000); };
}
connect();
term.onData(function(data){ if(ws && ws.readyState===1){ ws.send(data); } });
container.addEventListener('click', function(){ term.focus(); });
document.addEventListener('visibilitychange', function(){ if(!document.hidden){ term.focus(); } });
setTimeout(function(){ term.focus(); }, 0);
window.addEventListener('resize', function(){
  fit.fit();
  if (execId) fetch('/terminal/resize', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ exec_id: execId, width: term.cols, height: term.rows }) });
});
</script>
</body>
</html>
"""
    return HTMLResponse(content=html)

# --- Update app and container port ---
class UpdatePortRequest(BaseModel):
    task_id: str
    new_port: int
    conf_dir: str
    domain: str

@app.post("/app/update-port")
def app_update_port(req: UpdatePortRequest):
    try:
        # Load build summary
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        if not os.path.exists(summary_path):
            raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        with open(summary_path, "r") as f:
            summary_obj = json.load(f)
        localrun = summary_obj.get("localrun", {}) or {}
        app_id = localrun.get("app_id") or summary_obj.get("app_id")
        current_id = summary_obj.get("container_name") or summary_obj.get("container_id")
        name = summary_obj.get("container_name")
        internal_port_key = localrun.get("internal_port_key") or "80/tcp"
        image = localrun.get("image_tag") or localrun.get("image_id") 
        env = localrun.get("env")
        resources = localrun.get("resources", {}) or {}
        cpu = resources.get("cpu")
        cpuset = resources.get("cpuset")
        memory = resources.get("memory")
        # Stop and remove old container
        stop_result = None
        rm_result = None
        try:
            if current_id:
                stop_result = ds.stop_container(current_id)
        except Exception as e:
            stop_result = {"error": str(e)}
        try:
            if current_id:
                rm_result = ds.remove_container(current_id, force=True)
        except Exception as e:
            rm_result = {"error": str(e)}
        # Re-run container with new port mapping
        ports = {internal_port_key: req.new_port}
        run_res = ds.run_container_extended(
            image=image,
            name=name,
            ports=ports,
            env=env,
            mem_limit=memory,
            nano_cpus=int(float(cpu) * 1_000_000_000) if isinstance(cpu, (int, float, str)) and str(cpu) else None,
            cpuset_cpus=cpuset,
            detach=True,
        )
        # Update summary with new port and container info
        try:
            summary_obj.update({
                "container_id": run_res.get("id"),
                "container_name": run_res.get("name"),
                "host_port": req.new_port,
                "localrun": {
                    **localrun,
                    "ports_requested": ports,
                }
            })
            with open(summary_path, "w") as f:
                json.dump(summary_obj, f, indent=2)
        except Exception:
            pass
        # Update nginx config to point to new port
        site_res = ns.create_or_update_site_in_dir(app_id, req.domain, req.new_port, req.conf_dir)
        reload_res = ns.reload_nginx()
        return {
            "stage": "update_port",
            "status": "completed",
            "task_id": req.task_id,
            "app_id": app_id,
            "domain": req.domain,
            "conf_path": site_res.get("path"),
            "new_port": req.new_port,
            "container": run_res,
            "stop": stop_result,
            "remove": rm_result,
            "nginx_reload": reload_res,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class BuildStartRequest(BaseModel):
    dockerfile_content: Optional[str] = None
    dockerfile: Optional[str] = None
    dockerfile_name: Optional[str] = None
    build_args: Optional[Dict[str, str]] = None
    cleanup: Optional[bool] = True
    nocache: Optional[bool] = True
    tag: Optional[str] = None
    app_id: Optional[str] = None
    # HTTP upload deployment
    upload_url: str

@app.post("/docker/build/start")
def docker_build_start(req: BuildStartRequest):
    try:
        from task_registry import create_task, emit_event, set_completed, set_error
        task_id = create_task("docker_build")

        def _emit(ev):
            ev["task_id"] = task_id
            if req.app_id:
                ev["app_id"] = req.app_id
            emit_event(task_id, ev)
        # Use a temp context so the inline Dockerfile writes in isolation
        tmp_dir = f"/tmp/docker-builds/{task_id}"
        try:
            os.makedirs(tmp_dir, exist_ok=True)
        except Exception:
            pass
        try:
            _sweep_tmp_docker_builds(20)
        except Exception:
            pass
        tag = req.tag or (f"{req.app_id}:latest" if req.app_id else None)

        def _runner():
            try:
                result = ds.stream_build_image(
                    context_path=tmp_dir,
                    tag=tag,
                    dockerfile=req.dockerfile,
                    build_args=req.build_args,
                    task_id=task_id,
                    override_log_endpoint=None,
                    dockerfile_content=req.dockerfile_content,
                    dockerfile_name=req.dockerfile_name,
                    cleanup=req.cleanup,
                    nocache=req.nocache,
                    emit=_emit,
                    app_id=req.app_id,
                    upload_url=req.upload_url,
                )
                set_completed(task_id, result)
            except Exception as e:
                set_error(task_id, str(e))
            finally:
                try:
                    if bool(req.cleanup):
                        shutil.rmtree(tmp_dir)
                except Exception:
                    pass

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        return {"task_id": task_id, "status": "started"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class BuildStreamRequest(BaseModel):
    context_path: str
    tag: Optional[str] = None
    dockerfile: Optional[str] = None
    build_args: Optional[Dict[str, str]] = None
    task_id: Optional[str] = None
    dockerfile_content: Optional[str] = None
    dockerfile_name: Optional[str] = None
    cleanup: Optional[bool] = True
    nocache: Optional[bool] = True

class TaskRunStreamRequest(BaseModel):
    cmd: str
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    task_id: Optional[str] = None

class ContainerLsRequest(BaseModel):
    container: str
    path: Optional[str] = "/"

class ContainerDuRequest(BaseModel):
    container: str
    path: Optional[str] = "/"

class ContainerLsDetailedRequest(BaseModel):
    container: str
    path: Optional[str] = "/"
    include_sizes: Optional[bool] = True

class ContainerInspectRequest(BaseModel):
    container: str

@app.post("/docker/build-stream")
def docker_build_stream(req: BuildStreamRequest):
    try:
        return ds.stream_build_image(
            context_path=req.context_path,
            tag=req.tag,
            dockerfile=req.dockerfile,
            build_args=req.build_args,
            task_id=req.task_id,
            override_log_endpoint=None,
            dockerfile_content=req.dockerfile_content,
            dockerfile_name=req.dockerfile_name,
            cleanup=req.cleanup,
            nocache=req.nocache,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/ls")
def docker_container_ls(req: ContainerLsRequest):
    try:
        res = ds.list_or_read_in_container(req.container, req.path or "/", include_sizes=True, max_bytes=200000)
        return res
    except Exception as e:
        msg = str(e)
        if "No such container" in msg or "not found" in msg.lower():
            raise HTTPException(status_code=404, detail="container not found")
        raise HTTPException(status_code=500, detail=msg)

@app.post("/docker/container/du")
def docker_container_du(req: ContainerDuRequest):
    try:
        res = ds.du_in_container(req.container, req.path or "/")
        return res
    except Exception as e:
        msg = str(e)
        if "No such container" in msg or "not found" in msg.lower():
            raise HTTPException(status_code=404, detail="container not found")
        raise HTTPException(status_code=500, detail=msg)

@app.post("/docker/container/ls-detailed")
def docker_container_ls_detailed(req: ContainerLsDetailedRequest):
    try:
        res = ds.ls_detailed_in_container(req.container, req.path or "/", include_sizes=bool(req.include_sizes))
        return res
    except Exception as e:
        msg = str(e)
        if "No such container" in msg or "not found" in msg.lower():
            raise HTTPException(status_code=404, detail="container not found")
        raise HTTPException(status_code=500, detail=msg)

@app.post("/docker/container/inspect")
def docker_container_inspect(req: ContainerInspectRequest):
    try:
        res = ds.inspect_container_details(req.container)
        return res
    except Exception as e:
        msg = str(e)
        if "No such container" in msg or "not found" in msg.lower():
            raise HTTPException(status_code=404, detail="container not found")
        raise HTTPException(status_code=500, detail=msg)

@app.post("/tasks/run-stream")
def tasks_run_stream(req: TaskRunStreamRequest):
    try:
        return ts.run_task_stream(cmd=req.cmd, cwd=req.cwd, env=req.env, task_id=req.task_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class HtmlSitePipelineRequest(BaseModel):
    repo_url: Optional[str] = "https://github.com/ikwerre-dev/html-site"
    dockerhub_repo: str
    tag: Optional[str] = "latest"
    host_port: Optional[int] = 8080
    dockerhub_username: Optional[str] = None
    dockerhub_password: Optional[str] = None
    task_id: Optional[str] = None

@app.post("/pipeline/html-site")
def pipeline_html_site(req: HtmlSitePipelineRequest):
    try:
        from pipeline_service import run_html_site_pipeline
        return run_html_site_pipeline(
            repo_url=req.repo_url or "https://github.com/ikwerre-dev/html-site",
            dockerhub_repo=req.dockerhub_repo,
            tag=req.tag or "latest",
            host_port=req.host_port or 8080,
            dockerhub_username=req.dockerhub_username,
            dockerhub_password=req.dockerhub_password,
            task_id=req.task_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tasks/logs/{task_id}")
def tasks_logs(task_id: str, tail: int = 20000, server: Optional[str] = None):
    try:
        import json
        from task_registry import get, set_error
        import db_service as db
        import time
        
        # Get task registry info
        t = get(task_id)
        status = t.get("status") if isinstance(t, dict) else "unknown"
        created_at = t.get("created_at") if isinstance(t, dict) else None
        now_ts = time.time()
        age_sec = (now_ts - float(created_at)) if created_at else 0.0
        
        # Resolve base directory based on server param
        base_dir = None
        if server and str(server).lower() == "build":
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "builds"))
        elif server and str(server).lower() == "runtime":
            base_dir = "/app/upload"
        else:
            # Prefer runtime upload locations; fallback to builds if none exist
            for candidate in ["/app/upload", "/upload/pxxl", "/app/upload", "/uploads"]:
                if os.path.isdir(candidate):
                    base_dir = candidate
                    break
            if not base_dir:
                base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "builds"))
        
        builds_dir = os.path.join(base_dir, task_id)
        build_info = {
            "task_id": task_id,
            "status": status,
            "build_logs": {},
            "build_metadata": {},
            "files_available": []
        }
        
        if os.path.exists(builds_dir):
            # List all available files
            try:
                build_info["files_available"] = [f for f in os.listdir(builds_dir) if os.path.isfile(os.path.join(builds_dir, f))]
            except Exception:
                pass
            
            # Load build.log (raw build output) with fallbacks to local builds directory
            build_log_candidates = [
                os.path.join(builds_dir, "build.log"),
            ]
            # If we are looking at runtime, also try local builds folder as a fallback
            if server and str(server).lower() == "runtime":
                build_log_candidates.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "builds", task_id, "build.log")))
            for blp in build_log_candidates:
                if os.path.exists(blp):
                    try:
                        with open(blp, "r") as f:
                            build_lines = f.readlines()
                            if tail is None or int(tail) <= 0:
                                slice_lines = build_lines
                            else:
                                slice_lines = build_lines[-int(tail):]
                            build_info["build_logs"]["raw"] = [line.rstrip() for line in slice_lines]
                            build_info["build_logs"]["raw_total_lines"] = len(build_lines)
                        break
                    except Exception:
                        continue
            
            # Load build.jsonl (structured build events) with fallbacks
            structured_candidates = [
                os.path.join(builds_dir, "build.jsonl"),
            ]
            if server and str(server).lower() == "runtime":
                structured_candidates.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "builds", task_id, "build.jsonl")))
            for sp in structured_candidates:
                if os.path.exists(sp):
                    try:
                        entries = []
                        with open(sp, "r") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    entries.append(json.loads(line))
                                except Exception:
                                    entries.append({"text": line})
                        if tail is None or int(tail) <= 0:
                            slice_entries = entries
                        else:
                            slice_entries = entries[-int(tail):]
                        build_info["build_logs"]["structured"] = slice_entries
                        build_info["build_logs"]["structured_total_lines"] = len(entries)
                        break
                    except Exception:
                        continue
            
            # Load events.log with fallbacks
            events_candidates = [
                os.path.join(builds_dir, "events.log"),
            ]
            if server and str(server).lower() == "runtime":
                events_candidates.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "builds", task_id, "events.log")))
            for ep in events_candidates:
                if os.path.exists(ep):
                    try:
                        with open(ep, "r") as f:
                            event_lines = f.readlines()
                            if tail is None or int(tail) <= 0:
                                slice_events = event_lines
                            else:
                                slice_events = event_lines[-int(tail):]
                            build_info["build_logs"]["events"] = [line.rstrip() for line in slice_events]
                            build_info["build_logs"]["events_total_lines"] = len(event_lines)
                        break
                    except Exception:
                        continue
            
            # Load error.log with fallbacks
            error_candidates = [
                os.path.join(builds_dir, "error.log"),
            ]
            if server and str(server).lower() == "runtime":
                error_candidates.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "builds", task_id, "error.log")))
            for elp in error_candidates:
                if os.path.exists(elp):
                    try:
                        with open(elp, "r") as f:
                            error_lines = f.readlines()
                            if tail is None or int(tail) <= 0:
                                slice_error = error_lines
                            else:
                                slice_error = error_lines[-int(tail):]
                            build_info["build_logs"]["error"] = [line.rstrip() for line in slice_error]
                            build_info["build_logs"]["error_total_lines"] = len(error_lines)
                        break
                    except Exception:
                        continue
            
            # Load build.info.json (summary) with fallbacks across known runtime dirs
            summary_obj = None
            summary_candidates = [
                os.path.join(builds_dir, "build.info.json"),
                os.path.join(builds_dir, "summary.json"),
            ]
            # If server=runtime or summary not found, probe other runtime locations
            if server and str(server).lower() == "runtime":
                for alt in ["/app/upload", "/upload/pxxl", "/app/upload"]:
                    summary_candidates.append(os.path.join(alt, task_id, "build.info.json"))
                    summary_candidates.append(os.path.join(alt, task_id, "summary.json"))
            # Try reading first available summary
            for sp in summary_candidates:
                if os.path.exists(sp):
                    try:
                        with open(sp, "r") as f:
                            summary_obj = json.load(f)
                        break
                    except Exception:
                        continue
            if summary_obj:
                # If server=runtime and stage is 'uploading', present it as 'uploaded'
                try:
                    if server and str(server).lower() == "runtime":
                        if (summary_obj.get("stage") or "") == "uploading":
                            summary_obj["stage"] = "uploaded"
                except Exception:
                    pass
                build_info["build_metadata"]["summary"] = summary_obj
                inferred = summary_obj.get("status") or summary_obj.get("stage")
                # Always reflect summary status precisely; task registry is secondary
                try:
                    build_info["status"] = inferred or build_info.get("status") or "unknown"
                except Exception:
                    build_info["status"] = inferred or build_info.get("status") or "unknown"
                # Surface stage and SFTP deployment details at top-level for easy consumption
                try:
                    build_info["stage"] = summary_obj.get("stage")
                    sftp_info = summary_obj.get("sftp_deployment")
                    if sftp_info:
                        build_info["sftp"] = sftp_info
                    # Do not surface sensitive SFTP params in API response
                except Exception:
                    pass
                # Upsert project record
                try:
                    app_id_val = summary_obj.get("app_id")
                    container_id_val = summary_obj.get("container_id") or summary_obj.get("container_name")
                    if app_id_val:
                        db.upsert_application(str(app_id_val), container_id_val)
                except Exception:
                    pass
            else:
                # Attempt to derive app_id from task events and upsert
                try:
                    evs = (t.get("events") or []) if isinstance(t, dict) else []
                    app_id_val = None
                    for ev in evs:
                        if isinstance(ev, dict) and ev.get("app_id"):
                            app_id_val = ev.get("app_id")
                            break
                    if app_id_val:
                        db.upsert_application(str(app_id_val), None)
                except Exception:
                    pass

            # Enforce build state and auto-fail when not active, with startup grace
            try:
                still_building = False
                summary_status = (summary_obj or {}).get("status") if summary_obj else None
                summary_stage = (summary_obj or {}).get("stage") if summary_obj else None
                if str(status).lower() == "running":
                    still_building = True
                if summary_status in ("running", "building"):
                    still_building = True
                if summary_stage in ("building", "uploading"):
                    still_building = True
                tmp_dir = os.path.join("/tmp/docker-builds", task_id)
                if os.path.isdir(tmp_dir):
                    still_building = True
                # Grace window: do not fail within first 60s of task creation
                if (not still_building) and (age_sec > 60.0):
                    try:
                        set_error(task_id, "build not active; deployment failed")
                        build_info["status"] = "failed"
                        build_info["failed_reason"] = "build not active"
                    except Exception:
                        pass
            except Exception:
                pass
            
            # Load parsed Dockerfile metadata
            parsed_dockerfile_path = os.path.join(builds_dir, "dockerfile.parsed.json")
            if os.path.exists(parsed_dockerfile_path):
                try:
                    with open(parsed_dockerfile_path, "r") as f:
                        build_info["build_metadata"]["dockerfile_parsed"] = json.load(f)
                except Exception:
                    pass
        else:
            build_info["error"] = f"logs directory not found: {builds_dir}"
            try:
                # Check alternate summary paths and tmp builder indicator
                summary_path, _bd = _resolve_summary_path(task_id)
                tmp_dir = os.path.join("/tmp/docker-builds", task_id)
                summary_exists = os.path.exists(summary_path)
                tmp_exists = os.path.isdir(tmp_dir)
                # Only fail if clearly inactive and past grace window
                if (str(status).lower() != "running") and (not summary_exists) and (not tmp_exists) and (age_sec > 60.0):
                    set_error(task_id, "logs directory not found")
                    build_info["status"] = "failed"
                    build_info["failed_reason"] = "logs directory not found"
                else:
                    # Reflect starting state if within grace
                    if str(status).lower() == "running" or tmp_exists or (age_sec <= 60.0):
                        build_info["status"] = build_info.get("status") or "running"
                        build_info["stage"] = build_info.get("stage") or "building"
            except Exception:
                pass
        
        return build_info
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tasks/status/{task_id}")
def tasks_status(task_id: str):
    try:
        from task_registry import get, set_error
        import db_service as db
        import json
        import time
        t = get(task_id)
        summary_path, builds_dir = _resolve_summary_path(task_id)
        summary_obj = None
        try:
            if os.path.exists(summary_path):
                with open(summary_path, "r") as f:
                    summary_obj = json.load(f)
        except Exception:
            summary_obj = None
        try:
            if summary_obj and summary_obj.get("app_id"):
                db.upsert_application(str(summary_obj.get("app_id")), summary_obj.get("container_id") or summary_obj.get("container_name"))
            else:
                evs = (t.get("events") or []) if isinstance(t, dict) else []
                app_id_val = None
                for ev in evs:
                    if isinstance(ev, dict) and ev.get("app_id"):
                        app_id_val = ev.get("app_id")
                        break
                if app_id_val:
                    db.upsert_application(str(app_id_val), None)
        except Exception:
            pass
        try:
            still_building = False
            status_val = t.get("status") if isinstance(t, dict) else None
            created_at = t.get("created_at") if isinstance(t, dict) else None
            age_sec = (time.time() - float(created_at)) if created_at else 0.0
            if str(status_val).lower() == "running":
                still_building = True
            if summary_obj:
                if summary_obj.get("status") in ("running", "building"):
                    still_building = True
                if summary_obj.get("stage") in ("building", "uploading"):
                    still_building = True
            if os.path.isdir(os.path.join("/tmp/docker-builds", task_id)):
                still_building = True
            if (not still_building) and (age_sec > 60.0):
                try:
                    set_error(task_id, "build not active; deployment failed")
                except Exception:
                    pass
        except Exception:
            pass
        return t
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.middleware("http")
async def log_requests(request: Request, call_next):
    print(f"[HTTP] {request.method} {request.url.path}")
    response = await call_next(request)
    print(f"[HTTP] {response.status_code} {request.method} {request.url.path}")
    return response

@app.middleware("http")
async def api_secret_auth(request: Request, call_next):
    secret = os.environ.get("API_SECRET")
    if secret:
        incoming = request.headers.get("X-API-SECRET")
        if incoming != secret:
            raise HTTPException(status_code=401, detail="unauthorized")
    return await call_next(request)

@app.websocket("/terminal/{id_or_name}")
async def terminal_ws(id_or_name: str, websocket: WebSocket):
    await websocket.accept()
    cmd_q = websocket.query_params.get("cmd")
    cwd_q = websocket.query_params.get("cwd")
    try:
        res = ds.start_exec_pty(id_or_name=id_or_name, cmd=(cmd_q.split() if cmd_q else None), env=None, cwd=cwd_q)
        sock = res["socket"]
        exec_id = res["exec_id"]
        await websocket.send_json({"exec_id": exec_id})
        try:
            sock.sendall(b"\n")
        except Exception:
            pass
    except Exception as e:
        try:
            await websocket.send_text(str(e))
        except Exception:
            pass
        return
    import asyncio
    async def ws_to_docker():
        try:
            while True:
                m = await websocket.receive()
                if m.get("type") == "websocket.receive":
                    if m.get("text") is not None:
                        sock.sendall(m["text"].encode("utf-8"))
                    elif m.get("bytes") is not None:
                        sock.sendall(m["bytes"])
                elif m.get("type") == "websocket.disconnect":
                    break
        except Exception:
            pass
    async def docker_to_ws():
        try:
            while True:
                data = await asyncio.to_thread(sock.recv, 4096)
                if not data:
                    break
                try:
                    await websocket.send_bytes(data)
                except Exception:
                    try:
                        await websocket.send_text(data.decode("utf-8", errors="ignore"))
                    except Exception:
                        break
        except Exception:
            pass
    try:
        await asyncio.gather(ws_to_docker(), docker_to_ws())
    finally:
        try:
            sock.close()
        except Exception:
            pass
        


class TerminalResizeRequest(BaseModel):
    exec_id: str
    width: int
    height: int

@app.post("/terminal/resize")
def terminal_resize(req: TerminalResizeRequest):
    try:
        return ds.resize_exec(req.exec_id, req.width, req.height)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ContainerControlRequest(BaseModel):
    task_id: str

@app.post("/docker/container/start")
def docker_container_start(req: ContainerControlRequest):
    try:
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        if not os.path.exists(summary_path):
            raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        with open(summary_path, "r") as f:
            summary_obj = json.load(f)
        cid = summary_obj.get("container_name") or summary_obj.get("container_id")
        if not cid:
            raise HTTPException(status_code=404, detail="container_id not found in build.info.json")
        res = ds.start_container(cid)
        # persist container id/name in case engine reassigns or aliases
        summary_obj["container_id"] = res.get("id") or summary_obj.get("container_id")
        summary_obj["container_name"] = res.get("name") or summary_obj.get("container_name")
        # update status watch metadata
        summary_obj.update({
            "stage": "container_start",
            "status": "completed",
            "status_watch": {"enabled": True, "interval_sec": 15}
        })
        with open(summary_path, "w") as f:
            json.dump(summary_obj, f, indent=2)
        return {"task_id": req.task_id, "container": res}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/restart")
def docker_container_restart(req: ContainerControlRequest):
    try:
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        if not os.path.exists(summary_path):
            raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        with open(summary_path, "r") as f:
            summary_obj = json.load(f)
        cid = summary_obj.get("container_name") or summary_obj.get("container_id")
        if not cid:
            upstream = (summary_obj.get("upstream", {}) or {})
            cid = upstream.get("upstream_host")
        if not cid:
            raise HTTPException(status_code=404, detail="container_id not found in build.info.json")
        # Use native Docker restart to avoid manual stop/start sequencing
        restart_res = None
        try:
            restart_res = ds.restart_container(cid)
        except Exception as e:
            restart_res = {"error": str(e)}
        # Inspect state after restart attempt
        details = ds.inspect_container_details(cid)
        state = details.get("state") or {}
        running = bool(state.get("Running"))
        # persist container id/name and stage in summary
        summary_obj["container_id"] = details.get("id") or summary_obj.get("container_id")
        summary_obj["container_name"] = details.get("name") or summary_obj.get("container_name")
        summary_obj.update({
            "stage": "container_restart",
            "status": "completed"
        })
        with open(summary_path, "w") as f:
            json.dump(summary_obj, f, indent=2)
        return {
            "task_id": req.task_id,
            "restart": restart_res,
            "container": {
                "id": details.get("id"),
                "name": details.get("name"),
                "running": running,
                "state": state,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/stop")
def docker_container_stop(req: ContainerControlRequest):
    try:
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        if not os.path.exists(summary_path):
            raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        with open(summary_path, "r") as f:
            summary_obj = json.load(f)
        cid = summary_obj.get("container_name") or summary_obj.get("container_id")
        if not cid:
            upstream = (summary_obj.get("upstream", {}) or {})
            cid = upstream.get("upstream_host")
        if not cid:
            raise HTTPException(status_code=404, detail="container_id not found in build.info.json")
        res = ds.stop_container(cid)
        # persist container id/name even on stop for consistency
        summary_obj["container_id"] = res.get("id") or summary_obj.get("container_id")
        summary_obj["container_name"] = res.get("name") or summary_obj.get("container_name")
        summary_obj.update({
            "stage": "container_stop",
            "status": "completed"
        })
        with open(summary_path, "w") as f:
            json.dump(summary_obj, f, indent=2)
        return {"task_id": req.task_id, "container": res}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/status")
def docker_container_status(req: ContainerControlRequest):
    try:
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        if not os.path.exists(summary_path):
            raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        with open(summary_path, "r") as f:
            summary_obj = json.load(f)
        cid = summary_obj.get("container_name") or summary_obj.get("container_id")
        if not cid:
            upstream = (summary_obj.get("upstream", {}) or {})
            cid = upstream.get("upstream_host")
        if not cid:
            raise HTTPException(status_code=404, detail="container_id not found in build.info.json")
        details = ds.inspect_container_details(cid)
        state = details.get("state") or {}
        running = bool(state.get("Running"))
        return {
            "task_id": req.task_id,
            "container": {
                "id": details.get("id"),
                "name": details.get("name"),
                "running": running,
                "state": state,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/container/status/{idorname}")
def docker_container_status_get(idorname: str):
    try:
        details = ds.inspect_container_details(idorname)
        state = details.get("state") or {}
        running = bool(state.get("Running"))
        return {
            "container": {
                "id": details.get("id"),
                "name": details.get("name"),
                "running": running,
                "state": state,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/container/logs-by-task/{task_id}")
def docker_container_logs_by_task(task_id: str, tail: int = 2000, timestamps: Optional[bool] = False):
    try:
        summary_path, builds_dir = _resolve_summary_path(task_id)
        if not os.path.exists(summary_path):
            try:
                res = ds.container_logs(task_id, tail=int(tail or 20000), timestamps=bool(timestamps))
                return {
                    "task_id": task_id,
                    "container_id": res.get("id"),
                    "container_name": res.get("name"),
                    "tail": int(tail or 20000),
                    "timestamps": bool(timestamps),
                    "logs": res.get("logs"),
                    "lines": res.get("lines"),
                }
            except Exception:
                raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        with open(summary_path, "r") as f:
            summary_obj = json.load(f)
        cid = summary_obj.get("container_name") or summary_obj.get("container_id")
        if not cid:
            upstream = (summary_obj.get("upstream", {}) or {})
            cid = upstream.get("upstream_host")
        if not cid:
            raise HTTPException(status_code=404, detail="container_id not found in build.info.json")
        res = ds.container_logs(cid, tail=int(tail or 200), timestamps=bool(timestamps))
        return {
            "task_id": task_id,
            "container_id": res.get("id"),
            "container_name": res.get("name"),
            "tail": int(tail or 200),
            "timestamps": bool(timestamps),
            "logs": res.get("logs"),
            "lines": res.get("lines"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/container/logs/{id_or_name}")
def docker_container_logs(id_or_name: str, tail: int = 2000, timestamps: Optional[bool] = False):
    try:
        res = ds.container_logs(id_or_name, tail=int(tail or 200), timestamps=bool(timestamps))
        return {
            "container_id": res.get("id"),
            "container_name": res.get("name"),
            "tail": int(tail or 200),
            "timestamps": bool(timestamps),
            "logs": res.get("logs"),
            "lines": res.get("lines"),
        }
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        if "No such container" in msg or "not found" in msg.lower():
            raise HTTPException(status_code=404, detail="container not found")
        raise HTTPException(status_code=500, detail=msg)

@app.post("/docker/container/stop")
def docker_container_stop(req: ContainerControlRequest):
    try:
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        if not os.path.exists(summary_path):
            raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        with open(summary_path, "r") as f:
            summary_obj = json.load(f)
        cid = summary_obj.get("container_name") or summary_obj.get("container_id")
        if not cid:
            upstream = (summary_obj.get("upstream", {}) or {})
            cid = upstream.get("upstream_host")
        if not cid:
            raise HTTPException(status_code=404, detail="container_id not found in build.info.json")
        res = ds.stop_container(cid)
        # persist container id/name even on stop for consistency
        summary_obj["container_id"] = res.get("id") or summary_obj.get("container_id")
        summary_obj["container_name"] = res.get("name") or summary_obj.get("container_name")
        summary_obj.update({
            "stage": "container_stop",
            "status": "completed"
        })
        with open(summary_path, "w") as f:
            json.dump(summary_obj, f, indent=2)
        return {"task_id": req.task_id, "container": res}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/status")
def docker_container_status(req: ContainerControlRequest):
    try:
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        if not os.path.exists(summary_path):
            raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        with open(summary_path, "r") as f:
            summary_obj = json.load(f)
        cid = summary_obj.get("container_name") or summary_obj.get("container_id")
        if not cid:
            upstream = (summary_obj.get("upstream", {}) or {})
            cid = upstream.get("upstream_host")
        if not cid:
            raise HTTPException(status_code=404, detail="container_id not found in build.info.json")
        details = ds.inspect_container_details(cid)
        state = details.get("state") or {}
        running = bool(state.get("Running"))
        return {
            "task_id": req.task_id,
            "container": {
                "id": details.get("id"),
                "name": details.get("name"),
                "running": running,
                "state": state,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/stop")
def docker_container_stop(req: ContainerControlRequest):
    try:
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        if not os.path.exists(summary_path):
            raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        with open(summary_path, "r") as f:
            summary_obj = json.load(f)
        cid = summary_obj.get("container_name") or summary_obj.get("container_id")
        if not cid:
            upstream = (summary_obj.get("upstream", {}) or {})
            cid = upstream.get("upstream_host")
        if not cid:
            raise HTTPException(status_code=404, detail="container_id not found in build.info.json")
        res = ds.stop_container(cid)
        # persist container id/name even on stop for consistency
        summary_obj["container_id"] = res.get("id") or summary_obj.get("container_id")
        summary_obj["container_name"] = res.get("name") or summary_obj.get("container_name")
        summary_obj.update({
            "stage": "container_stop",
            "status": "completed"
        })
        with open(summary_path, "w") as f:
            json.dump(summary_obj, f, indent=2)
        return {"task_id": req.task_id, "container": res}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Initialize DB and metrics watcher at startup
try:
    db.init_db()
except Exception:
    pass

_metrics_stop_event = threading.Event()

def _cpu_percent(stats: dict) -> float:
    try:
        cpu = stats.get("cpu_stats", {})
        precpu = stats.get("precpu_stats", {})
        cpu_delta = float(cpu.get("cpu_usage", {}).get("total_usage", 0)) - float(precpu.get("cpu_usage", {}).get("total_usage", 0))
        system_delta = float(cpu.get("system_cpu_usage", 0)) - float(precpu.get("system_cpu_usage", 0))
        cores = max(1, int(cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage", []) or [0])))
        if system_delta > 0 and cpu_delta > 0:
            return round((cpu_delta / system_delta) * cores * 100.0, 2)
        return 0.0
    except Exception:
        return 0.0

def _mem_usage(stats: dict) -> int:
    try:
        mem = stats.get("memory_stats", {})
        return int(mem.get("usage") or 0)
    except Exception:
        return 0

def _metrics_watcher_loop():
    while not _metrics_stop_event.is_set():
        try:
            apps = db.list_applications()
            for app in apps:
                cid = app.get("container_id")
                aid = app.get("app_id")
                if not cid:
                    continue
                try:
                    s = ds.get_container_stats(cid)
                    cpu = _cpu_percent(s)
                    ram = _mem_usage(s)
                    db.add_metric(aid, cpu=cpu, ram=ram)
                except Exception:
                    # ignore stats errors
                    pass
        except Exception:
            pass
        _metrics_stop_event.wait(15)

try:
    threading.Thread(target=_metrics_watcher_loop, daemon=True).start()
except Exception:
    pass


class MetricsQuery(BaseModel):
    app_id: str
    limit: Optional[int] = 200
    type: Optional[str] = "raw"

@app.post("/metrics/query")
def metrics_query(req: MetricsQuery):
    try:
        if req.type == "summary":
            data = db.get_metrics_summary(req.app_id, days=req.limit or 7)
        else:
            data = db.get_metrics(req.app_id, limit=req.limit or 200)

        container_stats = None
        app_info = db.get_application_by_id(req.app_id)
        if app_info and app_info.get("container_id"):
            try:
                details = ds.inspect_container_details(app_info["container_id"])
                container_stats = details.get("state")
            except Exception:
                container_stats = {"Status": "not_found", "Running": False}

        return {
            "app_id": req.app_id,
            "metrics": data,
            "container_stats": container_stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/system/info")
def system_info():
    try:
        return sys.get_system_info()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/system/usage")
def system_usage():
    try:
        return sys.get_resource_usage()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/system/top")
def system_top(sort: str = "cpu", limit: int = 10):
    try:
        return {
            "sort": sort,
            "limit": limit,
            "processes": sys.top_processes(sort_by=sort, limit=limit),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class BuildDeleteRequest(BaseModel):
    task_id: str
    sftp_username: Optional[str] = None
    sftp_password: Optional[str] = None

@app.post("/build/delete")
def build_delete(req: BuildDeleteRequest):
    try:
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        if not os.path.exists(summary_path):
            raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        with open(summary_path, "r") as f:
            summary_obj = json.load(f)
        # Resolve container id/name
        cid = summary_obj.get("container_name") or summary_obj.get("container_id")
        upstream = (summary_obj.get("upstream", {}) or {})
        if not cid:
            cid = upstream.get("upstream_host")
        stop_res = None
        rm_res = None
        if cid:
            try:
                stop_res = ds.stop_container(cid)
            except Exception as e:
                stop_res = {"error": str(e)}
            try:
                rm_res = ds.remove_container(cid, force=True)
            except Exception as e:
                rm_res = {"error": str(e)}
        # Resolve image tag or id
        image_id = (summary_obj.get("localrun", {}) or {}).get("image_id") or summary_obj.get("image_id")
        image_tag = (summary_obj.get("localrun", {}) or {}).get("image_tag") or summary_obj.get("tag")
        img_ref = image_tag or image_id
        rmi_res = None
        if img_ref:
            try:
                rmi_res = ds.remove_image(img_ref, force=True)
            except Exception as e:
                rmi_res = {"error": str(e)}
        # Delete remote SFTP build directory if info is present and credentials provided
        sftp_res = {"status": "skipped"}
        sftp_info = summary_obj.get("sftp_deployment") or {}
        remote_path = sftp_info.get("remote_path")
        sftp_host = sftp_info.get("sftp_host")
        sftp_port = sftp_info.get("sftp_port") or 22
        
        if remote_path and sftp_host and req.sftp_username:
            ssh = None
            sftp = None
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                _conn_kwargs = {
                    "hostname": sftp_host,
                    "port": int(sftp_port),
                    "username": req.sftp_username,
                    "timeout": 30,
                }
                if req.sftp_password:
                    _conn_kwargs["password"] = req.sftp_password
                ssh.connect(**_conn_kwargs)
                sftp = ssh.open_sftp()
                def _rmtree(path: str):
                    try:
                        for entry in sftp.listdir_attr(path):
                            child = posixpath.join(path, entry.filename)
                            # Best-effort directory detection: try stat and rmdir; if fails, remove as file
                            try:
                                sftp.listdir(child)
                                _rmtree(child)
                                sftp.rmdir(child)
                            except IOError:
                                try:
                                    sftp.remove(child)
                                except Exception:
                                    pass
                        sftp.rmdir(path)
                    except IOError:
                        return
                _rmtree(remote_path)
                sftp.close()
                ssh.close()
                sftp_res = {"status": "success", "remote_path": remote_path}
            except Exception as e:
                sftp_res = {"status": "error", "error": str(e), "remote_path": remote_path}
            finally:
                try:
                    if sftp:
                        sftp.close()
                except Exception:
                    pass
                try:
                    if ssh:
                        ssh.close()
                except Exception:
                    pass
        # Delete local build directory
        local_delete = None
        try:
            if os.path.exists(builds_dir):
                shutil.rmtree(builds_dir)
                local_delete = {"deleted": True, "path": builds_dir}
            else:
                local_delete = {"deleted": False, "path": builds_dir}
        except Exception as e:
            local_delete = {"error": str(e), "path": builds_dir}
        return {
            "task_id": req.task_id,
            "stop": stop_res,
            "remove": rm_res,
            "rmi": rmi_res,
            "sftp_delete": sftp_res,
            "local_delete": local_delete,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class VolumeCreateRequest(BaseModel):
    task_id: str
    volume_name: str
    mount_path: str
    mode: str
    limit_mb: Optional[int] = None
    driver: Optional[str] = "local"
    labels: Optional[Dict[str, str]] = None

@app.get("/docker/container/list")
def docker_container_list(all: Optional[bool] = False):
    try:
        return {"containers": ds.list_containers(all=bool(all))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/volume/create")
def docker_volume_create(req: VolumeCreateRequest):
    try:
        # Create the volume
        vol_res = ds.create_volume(req.volume_name, driver=req.driver or "local", labels=req.labels)
        
        # Set limit if provided
        limit_res = None
        if req.limit_mb is not None:
             limit_kb = int(req.limit_mb) * 1024
             limit_res = ds.set_volume_limit(req.task_id, req.volume_name, req.mount_path, limit_kb)
         
        # Resolve container from task_id
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        cid = None
        if os.path.exists(summary_path):
            with open(summary_path, "r") as f:
                summary_obj = json.load(f)
            cid = summary_obj.get("container_name") or summary_obj.get("container_id")
            if not cid:
                upstream = (summary_obj.get("upstream", {}) or {})
                cid = upstream.get("upstream_host")
        
        if not cid:
            # Try to infer from app_id if task_id didn't yield a container
            pass

        if cid:
             # Attach volume by recreating container
             recreate_res = ds.recreate_with_added_volume(cid, req.volume_name, req.mount_path, req.mode)
             return {
                 "status": "ok", 
                 "volume": vol_res, 
                 "limit": limit_res, 
                 "container": recreate_res
             }
        else:
             return {
                 "status": "ok", 
                 "volume": vol_res, 
                 "limit": limit_res, 
                 "message": "Container not found for task_id, volume created but not attached"
             }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/volume/list")
def docker_volume_list():
    try:
        return {"volumes": ds.list_volumes()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/docker/volume/inspect/{volume_name}")
def docker_volume_inspect(volume_name: str):
    try:
        res = ds.inspect_volume(volume_name)
        if isinstance(res, dict) and res.get("status") == "not_found":
            raise HTTPException(status_code=404, detail=res)
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class VolumeEmptyRequest(BaseModel):
    volume_name: str
    force: Optional[bool] = False


@app.post("/docker/volume/empty")
def docker_volume_empty(req: VolumeEmptyRequest):
    try:
        res = ds.clear_volume_contents(req.volume_name, force=bool(req.force))
        if isinstance(res, dict) and res.get("status") == "not_found":
            raise HTTPException(status_code=404, detail=res)
        if isinstance(res, dict) and res.get("status") == "attached_to_running_containers":
            raise HTTPException(status_code=409, detail=res)
        if isinstance(res, dict) and res.get("status") in ("clear_failed", "error"):
            raise HTTPException(status_code=500, detail=res)
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class VolumeRemoveRequest(BaseModel):
    task_id: str
    volume_name: str
    force: Optional[bool] = False

class VolumeDetachRequest(BaseModel):
    task_id: str
    volume_name: str

@app.post("/docker/container/volume/detach")
def docker_volume_detach(req: VolumeDetachRequest):
    """
    Attempt to detach a volume from a container without recreating the container.
    
    Note: This endpoint explains Docker's limitations regarding volume detachment
    and provides information about alternative approaches.
    """
    try:
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        if not os.path.exists(summary_path):
            raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        with open(summary_path, "r") as f:
            summary_obj = json.load(f)
        cid = summary_obj.get("container_name") or summary_obj.get("container_id")
        name = summary_obj.get("container_name") or cid
        if not cid:
            upstream = (summary_obj.get("upstream", {}) or {})
            cid = upstream.get("upstream_host")
            name = name or cid
        if not cid:
            raise HTTPException(status_code=404, detail="container_id not found in build.info.json")
        
        # Attempt to detach volume (will explain limitations)
        result = ds.detach_volume_only(cid, req.volume_name)
        
        # Return appropriate HTTP status based on the result
        if result["status"] == "docker_limitation":
            # Return 409 Conflict to indicate the operation cannot be performed due to Docker limitations
            raise HTTPException(status_code=409, detail=result)
        elif result["status"] == "not_attached":
            # Return 404 if volume is not attached
            raise HTTPException(status_code=404, detail=result)
        elif result["status"] == "error":
            # Return 500 for other errors
            raise HTTPException(status_code=500, detail=result)
        else:
            # Return 200 for informational responses
            return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/volume/remove")
def docker_volume_remove(req: VolumeRemoveRequest):
    try:
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        if not os.path.exists(summary_path):
            raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        with open(summary_path, "r") as f:
            summary_obj = json.load(f)
        cid = summary_obj.get("container_name") or summary_obj.get("container_id")
        name = summary_obj.get("container_name") or cid
        if not cid:
            upstream = (summary_obj.get("upstream", {}) or {})
            cid = upstream.get("upstream_host")
            name = name or cid
        if not cid:
            raise HTTPException(status_code=404, detail="container_id not found in build.info.json")
        # Recreate without the specified volume and clear any stored limit
        recreate_res = ds.recreate_without_volume(cid, req.volume_name)
        limit_res = ds.remove_volume_limit(req.task_id, req.volume_name)
        return {"status": "ok", "container": recreate_res, "limit": limit_res}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class VolumeRemoveAndDeleteRequest(BaseModel):
    task_id: str
    volume_name: str
    force: Optional[bool] = False

@app.post("/docker/container/volume/remove-and-delete")
def docker_volume_remove_and_delete(req: VolumeRemoveAndDeleteRequest):
    """
    Remove and delete a volume. This endpoint now uses improved logic that
    explains Docker's limitations regarding volume detachment from running containers.
    """
    try:
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        if not summary_path or not os.path.exists(summary_path):
            raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        
        result = {"status": "ok"}
        
        # Step 1: Clear any stored logical limit for the volume
        try:
            result["limit"] = ds.remove_volume_limit(req.task_id, req.volume_name)
        except Exception as e:
            result["limit"] = {"error": str(e)}
        
        # Step 2: Use improved volume deletion logic
        try:
            volume_result = ds.delete_volume_with_attachment_check(req.volume_name, force=bool(req.force))
            result["volume_delete"] = volume_result
            
            # If volume is attached to running containers and force is not used, 
            # return appropriate HTTP status
            if volume_result["status"] == "attached_to_running_containers":
                raise HTTPException(status_code=409, detail=result)
            elif volume_result["status"] in ["delete_failed", "force_delete_failed", "error"]:
                raise HTTPException(status_code=500, detail=result)
                
        except HTTPException:
            raise
        except Exception as e:
            result["volume_delete"] = {"error": str(e)}
            raise HTTPException(status_code=500, detail=result)

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class DatabaseCreateRequest(BaseModel):
    type: str  # "postgres", "mysql", "mongodb", or "redis"
    tag: Optional[str] = "latest"
    container_name: Optional[str] = None
    host_port: Optional[int] = None
    username: Optional[str] = None      # postgres user, mysql app user, or mongodb root user
    password: Optional[str] = None      # postgres user password, mysql app user password, or redis password
    root_password: Optional[str] = None # mysql root password or mongodb root password
    db_name: Optional[str] = None       # database name (mongo: MONGO_INITDB_DATABASE)

@app.post("/database/create")
def database_create(req: DatabaseCreateRequest):
    try:
        res = ds.create_database_container(
            db_type=req.type,
            tag=req.tag or "latest",
            container_name=req.container_name,
            username=req.username,
            password=req.password,
            root_password=req.root_password,
            db_name=req.db_name,
            host_port=req.host_port,
            network="traefik-network",
        )
        if isinstance(res, dict) and res.get("status") == "error":
            raise HTTPException(status_code=400, detail=res)
        try:
            name_val = (res.get("name") if isinstance(res, dict) else (req.container_name or ""))
            id_val = (res.get("id") if isinstance(res, dict) else None)
            if name_val:
                db.upsert_application(name_val, id_val)
        except Exception:
            pass
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ContainerNameRequest(BaseModel):
    name: str

@app.post("/docker/container/stop-by-name")
def docker_container_stop_by_name(req: ContainerNameRequest):
    try:
        res = ds.stop_container(req.name)
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class DatabaseDeleteRequest(BaseModel):
    name: str
    force: Optional[bool] = True
    stop_first: Optional[bool] = True

@app.post("/database/delete")
def database_delete(req: DatabaseDeleteRequest):
    try:
        stop_res = None
        if req.stop_first:
            try:
                stop_res = ds.stop_container(req.name)
            except Exception as e:
                stop_res = {"error": str(e)}
        rm_res = ds.remove_container(req.name, force=bool(req.force))
        return {"status": "ok", "stop": stop_res, "remove": rm_res}
    except HTTPException:
        raise
    except Exception as e:
        # Return 404 for missing container if identifiable
        msg = str(e)
        if "No such" in msg or "not found" in msg:
            raise HTTPException(status_code=404, detail={"status": "not_found", "name": req.name, "error": msg})
        raise HTTPException(status_code=500, detail=str(e))


class DatabaseStatusRequest(BaseModel):
    name: str

@app.post("/database/status")
def database_status(req: DatabaseStatusRequest):
    try:
        details = ds.inspect_container_details(req.name)
        state = details.get("state") or {}
        running = bool(state.get("Running"))
        return {"name": details.get("name"), "id": details.get("id"), "running": running, "state": state}
    except Exception as e:
        msg = str(e)
        if "No such" in msg or "not found" in msg:
            raise HTTPException(status_code=404, detail={"status": "not_found", "name": req.name, "error": msg})
        raise HTTPException(status_code=500, detail=str(e))


class DatabaseDetailsRequest(BaseModel):
    name: str

@app.post("/database/details")
def database_details(req: DatabaseDetailsRequest):
    try:
        info = ds.inspect_container_details(req.name)
        return info
    except Exception as e:
        msg = str(e)
        if "No such" in msg or "not found" in msg:
            raise HTTPException(status_code=404, detail={"status": "not_found", "name": req.name, "error": msg})
        raise HTTPException(status_code=500, detail=str(e))


class DatabaseLogsRequest(BaseModel):
    name: str
    tail: Optional[int] = 200

@app.post("/database/logs")
def database_logs(req: DatabaseLogsRequest):
    try:
        res = ds.container_logs(req.name, tail=int(req.tail or 200), timestamps=True)
        text = res.get("logs") or ""
        lines = res.get("lines") or text.splitlines()
        entries = []
        for line in lines:
            ts = None
            msg = line
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[0].startswith("20") and ("T" in parts[0]):
                ts = parts[0]
                msg = parts[1]
            entries.append({"timestamp": ts, "logs": msg})
        return {"entries": entries}
    except Exception as e:
        msg = str(e)
        if "No such" in msg or "not found" in msg:
            raise HTTPException(status_code=404, detail={"status": "not_found", "name": req.name, "error": msg})
        raise HTTPException(status_code=500, detail=str(e))


class DatabaseLogsJsonRequest(BaseModel):
    name: str 
    tail: Optional[int] = 200

@app.post("/database/logs-json")
def database_logs_json(req: DatabaseLogsJsonRequest):
    try:
        res = ds.container_logs(req.name, tail=int(req.tail or 200), timestamps=True)
        text = res.get("logs") or ""
        lines = res.get("lines") or text.splitlines()
        entries = []
        for line in lines:
            ts = None
            msg = line
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[0].startswith("20") and ("T" in parts[0]):
                ts = parts[0]
                msg = parts[1]
            try:
                obj = json.loads(msg)
                entries.append({"timestamp": ts, "logs": obj})
            except Exception:
                entries.append({"timestamp": ts, "logs": msg})
        return {"entries": entries}
    except Exception as e:
        msg = str(e)
        if "No such" in msg or "not found" in msg:
            raise HTTPException(status_code=404, detail={"status": "not_found", "name": req.name, "error": msg})
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/docker/container/start-by-name")
def docker_container_start_by_name(req: ContainerNameRequest):
    try:
        res = ds.start_container(req.name)
        try:
            db.upsert_application(req.name, (res.get("id") if isinstance(res, dict) else None))
        except Exception:
            pass
        return res
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        if "No such" in msg or "not found" in msg:
            raise HTTPException(status_code=404, detail={"status": "not_found", "name": req.name, "error": msg})
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/docker/container/restart-by-name")
def docker_container_restart_by_name(req: ContainerNameRequest):
    try:
        res = ds.restart_container(req.name)
        try:
            db.upsert_application(req.name, (res.get("id") if isinstance(res, dict) else None))
        except Exception:
            pass
        return res
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        if "No such" in msg or "not found" in msg:
            raise HTTPException(status_code=404, detail={"status": "not_found", "name": req.name, "error": msg})
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/container/inspect/{id_or_name}")
def docker_container_inspect(id_or_name: str):
    try:
        details = ds.inspect_container_details(id_or_name)
        return {"container": details}
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        if "No such" in msg or "not found" in msg:
            raise HTTPException(status_code=404, detail={"status": "not_found", "id_or_name": id_or_name, "error": msg})
        raise HTTPException(status_code=500, detail=str(e))


class BuildCleanupRequest(BaseModel):
    task_id: str

@app.post("/docker/build/cleanup")
def docker_build_cleanup(req: BuildCleanupRequest):
    try:
        builds_dir = os.path.join(os.path.dirname(__file__), "builds", req.task_id)
        tmp_dir = os.path.join("/tmp/docker-builds", req.task_id)
        cleaned = False
        cleaned_tmp = False
        try:
            if os.path.exists(builds_dir):
                shutil.rmtree(builds_dir)
                cleaned = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        try:
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir)
                cleaned_tmp = True
        except Exception:
            pass
        try:
            _sweep_tmp_docker_builds(20)
        except Exception:
            pass
        return {"task_id": req.task_id, "cleaned": cleaned, "tmp_cleaned": cleaned_tmp}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class TaskCleanupRequest(BaseModel):
    task_id: str
    force: Optional[bool] = True
    prune_images: Optional[bool] = True
    remove_image: Optional[bool] = True

@app.post("/docker/task/cleanup")
def docker_task_cleanup(req: TaskCleanupRequest):
    try:
        # Resolve runtime paths and summary info
        summary_path, builds_dir = _resolve_summary_path(req.task_id)
        try:
            with open(summary_path, "r") as f:
                summary_obj = json.load(f)
        except Exception:
            summary_obj = {}

        import time
        os.makedirs(builds_dir, exist_ok=True)
        events_log_path = os.path.join(builds_dir, "events.log")
        build_log_path = os.path.join(builds_dir, "build.log")
        build_structured_path = os.path.join(builds_dir, "build.jsonl")

        def _append_line(path: str, msg: str):
            try:
                now = time.strftime('%Y-%m-%d %H:%M:%S')
                with open(path, "a") as f:
                    f.write(f"[{now}] {msg}\n")
            except Exception:
                pass

        def _append_build(path: str, msg: str, level: str = "info"):
            try:
                now = time.strftime('%Y-%m-%d %H:%M:%S')
                prefix = "[ERROR]" if str(level).lower() == "error" else "[INFO ]"
                with open(path, "a") as f:
                    f.write(f"[{now}] {prefix} {msg}\n")
            except Exception:
                pass

        def _append_json(path: str, obj: dict):
            try:
                with open(path, "a") as f:
                    f.write(json.dumps(obj) + "\n")
            except Exception:
                pass

        def _ts():
            return time.strftime('%Y-%m-%d %H:%M:%S')

        # Identify container and image from summary
        cid = summary_obj.get("container_name") or summary_obj.get("container_id")
        image_tag = (summary_obj.get("localrun", {}) or {}).get("image_tag") or summary_obj.get("tag")

        result = {"task_id": req.task_id, "container": {}, "image": {}, "volumes": [], "runtime_cleanup": {}, "prune": {}}

        # Inspect container mounts to gather attached volume names (before removal)
        attached_volume_names = []
        if cid:
            try:
                details = ds.inspect_container_details(cid)
                mounts = details.get("mounts") or []
                for m in mounts:
                    if (m.get("Type") == "volume") and m.get("Name"):
                        attached_volume_names.append(m.get("Name"))
            except Exception:
                attached_volume_names = []

        # Remove container
        if cid:
            try:
                _append_line(events_log_path, f"cleanup: removing container {cid}")
                _append_build(build_log_path, f"cleanup: removing container {cid}")
                _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "cleanup_container_remove", "task_id": req.task_id, "container": cid})
                cres = ds.remove_container(cid, force=bool(req.force))
                result["container"] = {"removed": True, "id": cres.get("id"), "name": cres.get("name")}
            except Exception as e:
                msg = str(e)
                result["container"] = {"removed": False, "error": msg}
                _append_line(events_log_path, f"cleanup: remove container failed: {msg}")
                _append_build(build_log_path, f"cleanup: remove container failed: {msg}", level="error")
                _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "cleanup_container_error", "task_id": req.task_id, "error": msg})
        else:
            result["container"] = {"removed": False, "message": "no container recorded in summary"}

        # Delete attached named volumes (after container removal)
        for vname in attached_volume_names:
            try:
                _append_line(events_log_path, f"cleanup: deleting attached volume {vname}")
                _append_build(build_log_path, f"cleanup: deleting attached volume {vname}")
                vres = ds.delete_volume_with_attachment_check(vname, force=bool(req.force))
                result["volumes"].append({"volume": vname, "status": vres.get("status"), "message": vres.get("message")})
                _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "cleanup_volume_delete", "task_id": req.task_id, "volume": vname, "result": vres})
            except Exception as e:
                msg = str(e)
                result["volumes"].append({"volume": vname, "status": "error", "error": msg})
                _append_line(events_log_path, f"cleanup: delete volume {vname} failed: {msg}")
                _append_build(build_log_path, f"cleanup: delete volume {vname} failed: {msg}", level="error")
                _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "cleanup_volume_error", "task_id": req.task_id, "volume": vname, "error": msg})

        # Remove image (optional)
        if req.remove_image and image_tag:
            try:
                _append_line(events_log_path, f"cleanup: removing image {image_tag}")
                _append_build(build_log_path, f"cleanup: removing image {image_tag}")
                _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "cleanup_image_remove", "task_id": req.task_id, "image": image_tag})
                ires = ds.remove_image(image_tag, force=bool(req.force))
                result["image"] = {"removed": True, "image": ires.get("image")}
            except Exception as e:
                msg = str(e)
                result["image"] = {"removed": False, "error": msg, "image": image_tag}
                _append_line(events_log_path, f"cleanup: remove image failed: {msg}")
                _append_build(build_log_path, f"cleanup: remove image failed: {msg}", level="error")
                _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "cleanup_image_error", "task_id": req.task_id, "error": msg})
        else:
            if req.remove_image:
                result["image"] = {"removed": False, "message": "no image tag recorded in summary"}
            else:
                result["image"] = {"removed": False, "message": "image removal skipped"}

        # Prune unused images (optional)
        if req.prune_images:
            try:
                _append_line(events_log_path, "cleanup: pruning unused images")
                _append_build(build_log_path, "cleanup: pruning unused images")
                pres = ds.prune_images()
                result["prune"] = {"status": "ok", "summary": pres}
                _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "cleanup_prune_images", "task_id": req.task_id, "prune": pres})
            except Exception as e:
                msg = str(e)
                result["prune"] = {"status": "error", "error": msg}
                _append_line(events_log_path, f"cleanup: prune images failed: {msg}")
                _append_build(build_log_path, f"cleanup: prune images failed: {msg}", level="error")
                _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "cleanup_prune_images_error", "task_id": req.task_id, "error": msg})

        # Runtime directory cleanup
        try:
            if os.path.isdir(builds_dir):
                _append_line(events_log_path, f"cleanup: deleting runtime dir {builds_dir}")
                _append_build(build_log_path, f"cleanup: deleting runtime dir {builds_dir}")
                shutil.rmtree(builds_dir)
                result["runtime_cleanup"] = {"deleted": True, "path": builds_dir}
                _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "cleanup_runtime_deleted", "task_id": req.task_id, "path": builds_dir})
            else:
                result["runtime_cleanup"] = {"deleted": False, "message": "runtime dir not found", "path": builds_dir}
                _append_line(events_log_path, f"cleanup: runtime dir not found {builds_dir}")
                _append_build(build_log_path, f"cleanup: runtime dir not found {builds_dir}")
        except Exception as e:
            msg = str(e)
            result["runtime_cleanup"] = {"deleted": False, "error": msg, "path": builds_dir}
            _append_line(events_log_path, f"cleanup: delete runtime dir failed: {msg}")
            _append_build(build_log_path, f"cleanup: delete runtime dir failed: {msg}", level="error")
            _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "cleanup_runtime_error", "task_id": req.task_id, "error": msg})

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
