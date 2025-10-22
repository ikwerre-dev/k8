from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, Dict
import os
import threading

import docker_service as ds
import system_service as sys
import nginx_service as ns
import task_service as ts

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

class IdRequest(BaseModel):
    id_or_name: str
    force: bool = False

class ExecRequest(BaseModel):
    id_or_name: str
    cmd: str

class AuthRequest(BaseModel):
    username: str
    password: str
    registry: Optional[str] = "https://index.docker.io/v1/"

class TagRequest(BaseModel):
    source: str
    repo: str
    tag: Optional[str] = None

class PushRequest(BaseModel):
    repo_tag: str

class PullRequest(BaseModel):
    name: str
    tag: Optional[str] = None

class SaveImageRequest(BaseModel):
    repo_tag: str
    tar_path: str

class LoadImageRequest(BaseModel):
    tar_path: str

class VolumeCreateRequest(BaseModel):
    name: str
    driver: Optional[str] = "local"
    labels: Optional[Dict[str, str]] = None

class VolumeRemoveRequest(BaseModel):
    name: str
    force: bool = False

class NetworkCreateRequest(BaseModel):
    name: str
    driver: Optional[str] = "bridge"

class NetworkRemoveRequest(BaseModel):
    id_or_name: str

class NetworkConnectRequest(BaseModel):
    network: str
    container: str

class KillRequest(BaseModel):
    id_or_name: str
    signal: Optional[str] = "SIGKILL"

class UpdateResourcesRequest(BaseModel):
    id_or_name: str
    mem_limit: Optional[str] = None
    nano_cpus: Optional[int] = None
    cpu_shares: Optional[int] = None
    pids_limit: Optional[int] = None
    cpuset_cpus: Optional[str] = None
    cpuset_mems: Optional[str] = None
    memswap_limit: Optional[str] = None

class RunExtendedRequest(BaseModel):
    image: str
    name: Optional[str] = None
    command: Optional[str] = None
    ports: Optional[Dict[str, int]] = None
    env: Optional[Dict[str, str]] = None
    volumes: Optional[Dict[str, dict]] = None
    network: Optional[str] = None
    mem_limit: Optional[str] = None
    nano_cpus: Optional[int] = None
    cpu_shares: Optional[int] = None
    pids_limit: Optional[int] = None
    detach: bool = True
    log_driver: Optional[str] = None
    log_options: Optional[Dict[str, str]] = None
    cpuset_cpus: Optional[str] = None
    cpuset_mems: Optional[str] = None
    memswap_limit: Optional[str] = None
    storage_opt: Optional[Dict[str, str]] = None

class FsListRequest(BaseModel):
    id_or_name: str
    path: str

class FsReadRequest(BaseModel):
    id_or_name: str
    path: str

class FsWriteRequest(BaseModel):
    id_or_name: str
    path: str
    content: str
    base64: bool = False
    mode: int = 0o644

class FsMkdirRequest(BaseModel):
    id_or_name: str
    path: str

class FsDeleteRequest(BaseModel):
    id_or_name: str
    path: str

class BlueGreenRequest(BaseModel):
    id_or_name: str
    image_repo: str
    tag: str
    wait_seconds: int = 15
    app_id: Optional[str] = None

class NginxCreateRequest(BaseModel):
    domain: str
    port: int

class NginxDeleteRequest(BaseModel):
    domain: str

class HtmlSitePipelineRequest(BaseModel):
    repo_url: Optional[str] = "https://github.com/ikwerre-dev/html-site"
    dockerhub_repo: str
    tag: Optional[str] = "latest"
    host_port: Optional[int] = 8080
    dockerhub_username: Optional[str] = None
    dockerhub_password: Optional[str] = None
    task_id: Optional[str] = None

@app.get("/")
def root():
    return {"Hello": f"From: {os.environ.get('ENV', 'DEFAULT_ENV')}", "service": "Docker Manager API", "docs": "/docs"}

@app.post("/docker/build")
def docker_build(req: BuildRequest):
    try:
        return ds.build_image(path=req.path, tag=req.tag)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/run")
def docker_run(req: RunRequest):
    try:
        return ds.run_container(
            image=req.image,
            name=req.name,
            command=req.command,
            ports=req.ports,
            env=req.env,
            detach=req.detach,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/containers")
def docker_containers(all: bool = False):
    try:
        return ds.list_containers(all=all)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/images")
def docker_images():
    try:
        return ds.list_images()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/stop")
def docker_stop(req: IdRequest):
    try:
        return ds.stop_container(req.id_or_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/rm")
def docker_rm(req: IdRequest):
    try:
        return ds.remove_container(req.id_or_name, force=req.force)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/rmi")
def docker_rmi(req: IdRequest):
    try:
        return ds.remove_image(req.id_or_name, force=req.force)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/prune")
def docker_prune():
    try:
        return ds.prune_system()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/logs/{id_or_name}")
def docker_logs(id_or_name: str, tail: int = 100):
    try:
        return ds.container_logs(id_or_name, tail=tail)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/exec")
def docker_exec(req: ExecRequest):
    try:
        return ds.exec_in_container(req.id_or_name, req.cmd)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/login")
def docker_login(req: AuthRequest):
    try:
        return ds.login(req.username, req.password, req.registry)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/tag")
def docker_tag(req: TagRequest):
    try:
        return ds.tag_image(req.source, req.repo, req.tag)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/push")
def docker_push(req: PushRequest):
    try:
        return ds.push_image(req.repo_tag)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/pull")
def docker_pull(req: PullRequest):
    try:
        return ds.pull_image(req.name, req.tag)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/save")
def docker_save(req: SaveImageRequest):
    try:
        return ds.save_image(req.repo_tag, req.tar_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/load")
def docker_load(req: LoadImageRequest):
    try:
        return ds.load_image(req.tar_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/volume/create")
def volume_create(req: VolumeCreateRequest):
    try:
        return ds.create_volume(req.name, driver=req.driver, labels=req.labels)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/volume/list")
def volume_list():
    try:
        return ds.list_volumes()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/volume/remove")
def volume_remove(req: VolumeRemoveRequest):
    try:
        return ds.remove_volume(req.name, force=req.force)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/network/create")
def network_create(req: NetworkCreateRequest):
    try:
        return ds.create_network(req.name, driver=req.driver)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/network/list")
def network_list():
    try:
        return ds.list_networks()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/network/remove")
def network_remove(req: NetworkRemoveRequest):
    try:
        return ds.remove_network(req.id_or_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/network/connect")
def network_connect(req: NetworkConnectRequest):
    try:
        return ds.connect_network(req.network, req.container)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/network/disconnect")
def network_disconnect(req: NetworkConnectRequest):
    try:
        return ds.disconnect_network(req.network, req.container)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/restart")
def container_restart(req: IdRequest):
    try:
        return ds.restart_container(req.id_or_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/pause")
def container_pause(req: IdRequest):
    try:
        return ds.pause_container(req.id_or_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/unpause")
def container_unpause(req: IdRequest):
    try:
        return ds.unpause_container(req.id_or_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/kill")
def container_kill(req: KillRequest):
    try:
        return ds.kill_container(req.id_or_name, signal=req.signal)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/update")
def container_update(req: UpdateResourcesRequest):
    try:
        return ds.update_container_resources(
            req.id_or_name,
            mem_limit=req.mem_limit,
            nano_cpus=req.nano_cpus,
            cpu_shares=req.cpu_shares,
            pids_limit=req.pids_limit,
            cpuset_cpus=req.cpuset_cpus,
            cpuset_mems=req.cpuset_mems,
            memswap_limit=req.memswap_limit,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/container/stats/{id_or_name}")
def container_stats(id_or_name: str):
    try:
        return ds.get_container_stats(id_or_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/container/top/{id_or_name}")
def container_top(id_or_name: str):
    try:
        return ds.top_processes(id_or_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/run-extended")
def docker_run_extended(req: RunExtendedRequest):
    try:
        return ds.run_container_extended(
            image=req.image,
            name=req.name,
            command=req.command,
            ports=req.ports,
            env=req.env,
            volumes=req.volumes,
            network=req.network,
            mem_limit=req.mem_limit,
            nano_cpus=req.nano_cpus,
            cpu_shares=req.cpu_shares,
            pids_limit=req.pids_limit,
            detach=req.detach,
            log_driver=req.log_driver,
            log_options=req.log_options,
            cpuset_cpus=req.cpuset_cpus,
            cpuset_mems=req.cpuset_mems,
            memswap_limit=req.memswap_limit,
            storage_opt=req.storage_opt,
        )
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
        return sys.top_processes(sort_by=sort, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/fs/list")
def container_fs_list(req: FsListRequest):
    try:
        return ds.list_path_in_container(req.id_or_name, req.path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/fs/read")
def container_fs_read(req: FsReadRequest):
    try:
        return ds.read_file_in_container(req.id_or_name, req.path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/fs/write")
def container_fs_write(req: FsWriteRequest):
    try:
        data = req.content
        if req.base64:
            import base64
            content_bytes = base64.b64decode(data)
        else:
            content_bytes = data.encode("utf-8")
        return ds.write_file_in_container(req.id_or_name, req.path, content_bytes, mode=req.mode)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/fs/mkdir")
def container_fs_mkdir(req: FsMkdirRequest):
    try:
        return ds.mkdir_in_container(req.id_or_name, req.path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/docker/container/fs/delete")
def container_fs_delete(req: FsDeleteRequest):
    try:
        return ds.delete_in_container(req.id_or_name, req.path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/docker/container/inspect/{id_or_name}")
def container_inspect(id_or_name: str):
    try:
        return ds.inspect_container_details(id_or_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/deploy/blue-green")
def deploy_blue_green(req: BlueGreenRequest):
    try:
        return ds.blue_green_deploy(
            base_id_or_name=req.id_or_name,
            image_repo=req.image_repo,
            tag=req.tag,
            wait_seconds=req.wait_seconds,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/nginx/site/create")
def nginx_site_create(req: NginxCreateRequest):
    try:
        return ns.create_site(req.domain, req.port)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/nginx/site/delete")
def nginx_site_delete(req: NginxDeleteRequest):
    try:
        return ns.delete_site(req.domain)
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


@app.post("/pipeline/html-site")
def pipeline_html_site(req: HtmlSitePipelineRequest):
    try:
        from pipeline_service import run_html_site_pipeline

        # Print each pipeline stage to the terminal for immediate feedback
        def _emit(ev):
            try:
                stage = ev.get("stage")
                status = ev.get("status")
                err = ev.get("error")
                extra = []
                if "exit_code" in ev:          
                    extra.append(f"exit_code={ev['exit_code']}")
                if "container_id" in ev:
                    extra.append(f"container_id={ev['container_id']}")
                if "status_code" in ev:
                    extra.append(f"status_code={ev['status_code']}")
                if err:
                    extra.append(f"error={err}")
                suffix = (" " + " ".join(extra)) if extra else ""
                print(f"[PIPELINE] {stage or ''} {status or ''}{suffix}")
            except Exception:
                pass

        return run_html_site_pipeline(
            repo_url=req.repo_url,
            dockerhub_repo=req.dockerhub_repo,
            tag=req.tag or "latest",
            host_port=req.host_port or 8080,
            dockerhub_username=req.dockerhub_username,
            dockerhub_password=req.dockerhub_password,
            task_id=req.task_id,
            emit=_emit,
        ) 
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/pipeline/html-site/start")
def pipeline_html_site_start(req: HtmlSitePipelineRequest):
    try:
        from task_registry import create_task, emit_event, set_completed, set_error
        from pipeline_service import run_html_site_pipeline

        task_id = create_task("html_site_pipeline")

        def _emit(ev):
            ev["task_id"] = task_id
            emit_event(task_id, ev)

        def _runner():
            try:
                result = run_html_site_pipeline(
                    repo_url=req.repo_url or "https://github.com/ikwerre-dev/html-site",
                    dockerhub_repo=req.dockerhub_repo,
                    tag=req.tag or "latest",
                    host_port=req.host_port or 8080,
                    dockerhub_username=req.dockerhub_username,
                    dockerhub_password=req.dockerhub_password,
                    task_id=task_id,
                    emit=_emit,
                )
                set_completed(task_id, result)
            except Exception as e:
                set_error(task_id, str(e))

        t = threading.Thread(target=_runner, daemon=True)
        t.start()

        return {"task_id": task_id, "status": "started"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tasks/logs/{task_id}")
def tasks_logs(task_id: str, tail: int = 200):
    try:
        from task_registry import get
        t = get(task_id)
        events = t.get("events", []) if isinstance(t, dict) else []
        lines = []
        for ev in events:
            chunk = ev.get("chunk")
            if chunk is not None:
                if isinstance(chunk, dict):
                    line = chunk.get("stream") or chunk.get("status") or str(chunk)
                else:
                    line = str(chunk)
                lines.append(line)
        status = t.get("status") if isinstance(t, dict) else "unknown"
        return {"task_id": task_id, "status": status, "lines": lines[-tail:]}
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