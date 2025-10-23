from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, Dict
import os
import threading
import json
import shutil
import paramiko
import posixpath

import docker_service as ds
import system_service as sys
import nginx_service as ns
import task_service as ts
import db_service as db
 
app = FastAPI(title="Docker Manager API")

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

class LocalRunRequest(BaseModel):
    lz4_path: str  # local relative path to .lz4 (or .tar.lz4)
    task_id: str  # task/build id used for logging dir
    app_id: Optional[str] = None  # app identifier for container naming
    ports: Optional[Dict[str, int]] = None
    env: Optional[Dict[str, str]] = None
    command: Optional[str] = None
    name: Optional[str] = None
    cpu: Optional[float] = None
    cpuset: Optional[str] = None
    memory: Optional[str] = None
    # Optional persistent storage to attach at run
    volume_name: Optional[str] = None
    mount_path: Optional[str] = None
    mode: Optional[str] = "rw"

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

@app.post("/docker/localrun")
def docker_localrun(req: LocalRunRequest):
    try:
        return ds.local_run_from_lz4(
            lz4_path_rel=req.lz4_path,
            task_id=req.task_id,
            app_id=req.app_id,
            ports=req.ports,
            env=req.env,
            command=req.command,
            name=req.name,
            cpu=req.cpu,
            cpuset=req.cpuset,
            memory=req.memory,
            volume_name=req.volume_name,
            mount_path=req.mount_path,
            mode=req.mode or "rw",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class NginxSignDomainRequest(BaseModel):
    domain: str
    conf_dir: str
    task_id: str
    app_id: Optional[str] = None
    old_container: Optional[str] = None
    old_task_id: Optional[str] = None
    new_container: Optional[str] = None
    port: Optional[int] = None

@app.post("/nginx/sign-domain")
def nginx_sign_domain(req: NginxSignDomainRequest):
    try:
        builds_dir = os.path.join("/pxxl/upload", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
        try:
            with open(summary_path, "r") as f:
                summary_obj = json.load(f)
        except Exception:
            summary_obj = {}
        # Infer app_id if missing
        if not req.app_id:
            builds_dir = os.path.join("/pxxl/upload", req.task_id)
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
        # Determine upstream internal port from summary localrun.internal_port_key or default 80
        internal_port = req.port
        if not internal_port:
            try:
                with open(summary_path, "r") as f:
                    summary_obj = json.load(f)
                localrun = (summary_obj.get("localrun", {}) or {})
                ipk = localrun.get("internal_port_key") or "80/tcp"
                try:
                    internal_port = int(str(ipk).split("/", 1)[0])
                except Exception:
                    internal_port = 80
            except Exception:
                internal_port = 80
        # Write app_id.conf targeting container name on internal network
        site_res = ns.create_or_update_site_in_dir(req.app_id, req.domain, internal_port, req.conf_dir, upstream_host=container_name)
        reload_res = ns.reload_nginx()
        # Stop old container if provided, then remove it
        old_stop = None
        old_remove = None
        if req.old_container:
            try:
                old_stop = ds.stop_container(req.old_container)
            except Exception as e:
                old_stop = {"error": str(e)}
            try:
                old_remove = ds.remove_container(req.old_container, force=True)
            except Exception as e:
                old_remove = {"error": str(e)}
        elif req.old_task_id:
            try:
                old_builds_dir = os.path.join("/pxxl/upload", req.old_task_id)
                old_summary_path = os.path.join(old_builds_dir, "build.info.json")
                if os.path.exists(old_summary_path):
                    with open(old_summary_path, "r") as f:
                        old_summary = json.load(f)
                    old_cid = old_summary.get("container_name") or old_summary.get("container_id")
                    if not old_cid:
                        old_up = (old_summary.get("upstream", {}) or {})
                        old_cid = old_up.get("upstream_host")
                    if old_cid:
                        try:
                            old_stop = ds.stop_container(old_cid)
                        except Exception as e:
                            old_stop = {"error": str(e)}
                        try:
                            old_remove = ds.remove_container(old_cid, force=True)
                        except Exception as e:
                            old_remove = {"error": str(e)}
            except Exception as e:
                old_stop = old_stop or {"error": str(e)}
                old_remove = old_remove or {"error": str(e)}
        # Update build.info.json: stage -> upstream, status -> completed
        try:
            with open(summary_path, "r") as f:
                summary_obj = json.load(f)
        except Exception:
            summary_obj = {}
        upstream_meta = {
            "domain": req.domain,
            "conf_path": site_res.get("path"),
            "port": internal_port,
            "upstream_host": container_name,
            "nginx_reload": reload_res,
        }
        try:
            summary_obj.update({
                "stage": "upstream",
                "status": "completed",
                "upstream": upstream_meta,
            })
            # Persist application record with container_id (resolve if missing)
            container_id = summary_obj.get("container_id")
            if not container_id:
                try:
                    details = ds.inspect_container_details(container_name)
                    container_id = details.get("id")
                except Exception:
                    container_id = None
            try:
                db.upsert_application(req.app_id, container_id)
            except Exception:
                pass
            with open(summary_path, "w") as f:
                json.dump(summary_obj, f, indent=2)
        except Exception:
            pass
        # Prune dangling images (images only)
        images_prune = None
        try:
            images_prune = ds.prune_images()
        except Exception as e:
            images_prune = {"error": str(e)}
        return {
            "stage": "upstream",
            "status": "completed",
            "domain": req.domain,
            "app_id": req.app_id,
            "conf_path": site_res.get("path"),
            "port": internal_port,
            "upstream_host": container_name,
            "nginx_reload": reload_res,
            "old_stop": old_stop,
            "old_remove": old_remove,
            "images_prune": images_prune,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
        builds_dir = os.path.join("/pxxl/upload", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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
    tag: Optional[str] = None
    app_id: Optional[str] = None
    # SFTP deployment parameters
    sftp_host: Optional[str] = None
    sftp_username: Optional[str] = None
    sftp_password: Optional[str] = None
    sftp_port: Optional[int] = 22

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
                    emit=_emit,
                    app_id=req.app_id,
                    sftp_host=req.sftp_host,
                    sftp_username=req.sftp_username,
                    sftp_password=req.sftp_password,
                    sftp_port=req.sftp_port,
                )
                set_completed(task_id, result)
            except Exception as e:
                set_error(task_id, str(e))

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

class TaskRunStreamRequest(BaseModel):
    cmd: str
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    task_id: Optional[str] = None

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
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
def tasks_logs(task_id: str, tail: int = 200, server: Optional[str] = None):
    try:
        import json
        from task_registry import get
        
        # Get task registry info
        t = get(task_id)
        status = t.get("status") if isinstance(t, dict) else "unknown"
        
        # Resolve base directory based on server param
        base_dir = None
        if server and str(server).lower() == "build":
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "builds"))
        elif server and str(server).lower() == "runtime":
            base_dir = "/app/upload"
        else:
            # Prefer runtime upload locations; fallback to builds if none exist
            for candidate in ["/app/upload", "/upload/pxxl", "/pxxl/upload", "/uploads"]:
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
            
            # Load build.log (raw build output)
            build_log_path = os.path.join(builds_dir, "build.log")
            if os.path.exists(build_log_path):
                try:
                    with open(build_log_path, "r") as f:
                        build_lines = f.readlines()
                        build_info["build_logs"]["raw"] = [line.rstrip() for line in build_lines[-tail:]]
                        build_info["build_logs"]["raw_total_lines"] = len(build_lines)
                except Exception:
                    pass
            
            # Load build.jsonl (structured build events)
            structured_path = os.path.join(builds_dir, "build.jsonl")
            if os.path.exists(structured_path):
                try:
                    entries = []
                    with open(structured_path, "r") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entries.append(json.loads(line))
                            except Exception:
                                entries.append({"text": line})
                    build_info["build_logs"]["structured"] = entries[-tail:]
                    build_info["build_logs"]["structured_total_lines"] = len(entries)
                except Exception:
                    pass
            
            # Load events.log
            events_log_path = os.path.join(builds_dir, "events.log")
            if os.path.exists(events_log_path):
                try:
                    with open(events_log_path, "r") as f:
                        event_lines = f.readlines()
                        build_info["build_logs"]["events"] = [line.rstrip() for line in event_lines[-tail:]]
                        build_info["build_logs"]["events_total_lines"] = len(event_lines)
                except Exception:
                    pass
            
            # Load error.log
            error_log_path = os.path.join(builds_dir, "error.log")
            if os.path.exists(error_log_path):
                try:
                    with open(error_log_path, "r") as f:
                        error_lines = f.readlines()
                        build_info["build_logs"]["error"] = [line.rstrip() for line in error_lines[-tail:]]
                        build_info["build_logs"]["error_total_lines"] = len(error_lines)
                except Exception:
                    pass
            
            # Load build.info.json (summary) with fallbacks across known runtime dirs
            summary_obj = None
            summary_candidates = [
                os.path.join(builds_dir, "build.info.json"),
                os.path.join(builds_dir, "summary.json"),
            ]
            # If server=runtime or summary not found, probe other runtime locations
            if server and str(server).lower() == "runtime":
                for alt in ["/app/upload", "/upload/pxxl", "/pxxl/upload"]:
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
                # If server=runtime and stage is 'uploading', present it as 'updated'
                try:
                    if server and str(server).lower() == "runtime":
                        if (summary_obj.get("stage") or "") == "uploading":
                            summary_obj["stage"] = "updated"
                except Exception:
                    pass
                build_info["build_metadata"]["summary"] = summary_obj
                inferred = summary_obj.get("status") or summary_obj.get("stage")
                if not build_info.get("status") or build_info.get("status") == "unknown":
                    build_info["status"] = inferred or "unknown"
            
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
        
        return build_info
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tasks/status/{task_id}")
def tasks_status(task_id: str):
    try:
        from task_registry import get
        return get(task_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.middleware("http")
async def log_requests(request: Request, call_next):
    print(f"[HTTP] {request.method} {request.url.path}")
    response = await call_next(request)
    print(f"[HTTP] {response.status_code} {request.method} {request.url.path}")
    return response


class ContainerControlRequest(BaseModel):
    task_id: str

@app.post("/docker/container/start")
def docker_container_start(req: ContainerControlRequest):
    try:
        builds_dir = os.path.join("/pxxl/upload", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
        if not os.path.exists(summary_path):
            raise HTTPException(status_code=404, detail="build.info.json not found for task_id")
        with open(summary_path, "r") as f:
            summary_obj = json.load(f)
        cid = summary_obj.get("container_name") or summary_obj.get("container_id")
        if not cid:
            raise HTTPException(status_code=404, detail="container_id not found in build.info.json")
        res = ds.start_container(cid)
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
        builds_dir = os.path.join("/pxxl/upload", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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
        stop_res = None
        start_res = None
        try:
            stop_res = ds.stop_container(cid)
        except Exception as e:
            stop_res = {"error": str(e)}
        try:
            start_res = ds.start_container(cid)
        except Exception as e:
            start_res = {"error": str(e)}
        return {"task_id": req.task_id, "stop": stop_res, "start": start_res}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/stop")
def docker_container_stop(req: ContainerControlRequest):
    try:
        builds_dir = os.path.join("/pxxl/upload", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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

@app.post("/docker/container/status")
def docker_container_status(req: ContainerControlRequest):
    try:
        builds_dir = os.path.join("/pxxl/upload", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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
        builds_dir = os.path.join("/pxxl/upload", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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

@app.post("/metrics/query")
def metrics_query(req: MetricsQuery):
    try:
        data = db.get_metrics(req.app_id, limit=req.limit or 200)
        return {"app_id": req.app_id, "metrics": data}
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
        builds_dir = os.path.join("/pxxl/upload", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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
                ssh.connect(hostname=sftp_host, port=int(sftp_port), username=req.sftp_username, password=req.sftp_password, timeout=30)
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
    name: str
    driver: Optional[str] = "local"
    labels: Optional[Dict[str, str]] = None

@app.post("/docker/volume/create")
def docker_volume_create(req: VolumeCreateRequest):
    try:
        return ds.create_volume(req.name, driver=req.driver or "local", labels=req.labels)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/volume/list")
def docker_volume_list():
    try:
        return {"volumes": ds.list_volumes()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class VolumeRemoveRequest(BaseModel):
    name: str
    force: Optional[bool] = False

class VolumeClearRequest(BaseModel):
    name: str

@app.post("/docker/volume/remove")
def docker_volume_remove(req: VolumeRemoveRequest):
    try:
        return ds.remove_volume(req.name, force=bool(req.force))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/volume/clear")
def docker_volume_clear(req: VolumeClearRequest):
    """
    Clear a Docker volume's contents without stopping containers.
    Uses a short-lived helper container to mount and empty the volume.
    """
    try:
        result = ds.clear_volume_contents(req.name)
        if result.get("status") == "not_found":
            raise HTTPException(status_code=404, detail=result)
        elif result.get("status") in ("clear_failed", "error"):
            raise HTTPException(status_code=500, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/volume/delete")
def docker_volume_delete(req: VolumeRemoveRequest):
    """
    Delete a Docker volume with proper checking for attached containers.
    
    This endpoint provides better error handling and information about
    volumes that are attached to running containers.
    """
    try:
        result = ds.delete_volume_with_attachment_check(req.name, force=bool(req.force))
        
        # Return appropriate HTTP status based on the result
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail=result)
        elif result["status"] == "attached_to_running_containers":
            # Return 409 Conflict when volume is attached to running containers
            raise HTTPException(status_code=409, detail=result)
        elif result["status"] in ["delete_failed", "force_delete_failed", "error"]:
            raise HTTPException(status_code=500, detail=result)
        else:
            # Return 200 for successful deletions (including force deletions with warnings)
            return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class ContainerFsRequest(BaseModel):
    task_id: str
    path: Optional[str] = "/"

class ContainerVolumeAddRequest(BaseModel):
    task_id: str 
    volume_name: str
    mount_path: str
    mode: Optional[str] = "rw"
    limit_mb: Optional[int] = None

@app.post("/docker/container/volume/add")
def docker_container_volume_add(req: ContainerVolumeAddRequest):
    try:
        builds_dir = os.path.join("/pxxl/upload", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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
        res = ds.recreate_with_added_volume(cid, req.volume_name, req.mount_path, req.mode or "rw")
        # Optionally set a logical size limit (provided in MB)
        saved_limit = None
        usage_status = None
        if req.limit_mb is not None:
            try:
                limit_kb = int(req.limit_mb) * 1024
                saved_limit = ds.set_volume_limit(req.task_id, req.volume_name, req.mount_path, limit_kb)
                new_cid = res.get("id") or cid
                usage_status = ds.check_volume_limit_status(req.task_id, new_cid, req.volume_name)
            except Exception as e:
                saved_limit = {"error": str(e)}
        try:
            summary_obj["container_id"] = res.get("id") or summary_obj.get("container_id")
            vols = summary_obj.get("volumes") or []
            vol_entry = {"name": req.volume_name, "mount": req.mount_path, "mode": req.mode or "rw"}
            if req.limit_mb is not None:
                vol_entry["limit_mb"] = int(req.limit_mb)
            vols.append(vol_entry)
            summary_obj["volumes"] = vols
            with open(summary_path, "w") as f:
                json.dump(summary_obj, f, indent=2)
        except Exception:
            pass
        resp = {"task_id": req.task_id, **res}
        if saved_limit is not None:
            resp["limit"] = saved_limit
        if usage_status is not None:
            resp["usage"] = usage_status
        return resp
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Update ls endpoint to return structured entries with sizes
@app.post("/docker/container/ls")
def docker_container_ls(req: ContainerFsRequest):
    try:
        builds_dir = os.path.join("/pxxl/upload", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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
        res = ds.ls_detailed_in_container(cid, req.path or "/", include_sizes=True)
        return {"task_id": req.task_id, **res}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/du")
def docker_container_du(req: ContainerFsRequest):
    try:
        builds_dir = os.path.join("/pxxl/upload", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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
        res = ds.du_in_container(cid, req.path or "/")
        return {"task_id": req.task_id, **res}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/usage")
def docker_container_usage(req: ContainerControlRequest):
    try:
        builds_dir = os.path.join("/pxxl/upload", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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
        info = ds.inspect_container_details(cid)
        return {"task_id": req.task_id, "id": info.get("id"), "name": info.get("name"), "size_rw": info.get("size_rw"), "size_root_fs": info.get("size_root_fs"), "mounts": info.get("mounts"), "state": info.get("state"), "computed": info.get("computed")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Persistent volume size limit APIs ---
class VolumeLimitSetRequest(BaseModel):
    task_id: str
    volume_name: str
    mount_path: str
    limit_kb: int

@app.post("/docker/container/volume/limit/set")
def docker_volume_limit_set(req: VolumeLimitSetRequest):
    try:
        builds_dir = os.path.join(os.path.dirname(__file__), "builds", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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
        saved = ds.set_volume_limit(req.task_id, req.volume_name, req.mount_path, req.limit_kb)
        status = ds.check_volume_limit_status(req.task_id, cid, req.volume_name)
        return {"status": "ok", "saved": saved, "usage": status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class VolumeLimitUpdateRequest(BaseModel):
    task_id: str
    volume_name: str
    limit_kb: int
    mount_path: Optional[str] = None

@app.post("/docker/container/volume/limit/update")
def docker_volume_limit_update(req: VolumeLimitUpdateRequest):
    try:
        builds_dir = os.path.join(os.path.dirname(__file__), "builds", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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
        saved = ds.update_volume_limit(req.task_id, req.volume_name, req.limit_kb, req.mount_path)
        status = ds.check_volume_limit_status(req.task_id, cid, req.volume_name)
        return {"status": "ok", "saved": saved, "usage": status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class VolumeRemoveRequest(BaseModel):
    task_id: str
    volume_name: str

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
        builds_dir = os.path.join(os.path.dirname(__file__), "builds", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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
        builds_dir = os.path.join(os.path.dirname(__file__), "builds", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
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
        builds_dir = os.path.join(os.path.dirname(__file__), "builds", req.task_id)
        summary_path = os.path.join(builds_dir, "build.info.json")
        if not os.path.exists(summary_path):
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
            network="nginx-network",
        )
        if isinstance(res, dict) and res.get("status") == "error":
            raise HTTPException(status_code=400, detail=res)
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
        return res
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        if "No such" in msg or "not found" in msg:
            raise HTTPException(status_code=404, detail={"status": "not_found", "name": req.name, "error": msg})
        raise HTTPException(status_code=500, detail=str(e))


class BuildCleanupRequest(BaseModel):
    task_id: str

@app.post("/docker/build/cleanup")
def docker_build_cleanup(req: BuildCleanupRequest):
    try:
        # Cleanup build artifacts on build server after successful transfer
        builds_dir = os.path.join(os.path.dirname(__file__), "builds", req.task_id)
        if not os.path.exists(builds_dir):
            return {"task_id": req.task_id, "cleaned": False, "message": "build directory not found"}
        try:
            shutil.rmtree(builds_dir)
            return {"task_id": req.task_id, "cleaned": True}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))