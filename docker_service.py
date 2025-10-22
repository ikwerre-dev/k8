import docker
from docker.types import LogConfig
from typing import Dict, Optional, List, Any, Callable
import io
import os
import tarfile
import base64
import random
import time
import requests
import json
import re
import shutil
import subprocess
import gzip
import paramiko
import posixpath
from config_service import get_log_endpoint, get_auth_header


def get_client() -> docker.DockerClient:
    """Create a Docker client using environment config.
    Works against local Docker. If running in a container, mount /var/run/docker.sock.
    """
    try:
        return docker.from_env()
    except Exception as e:
        raise RuntimeError(f"Failed to connect to Docker daemon: {e}")


def login(username: str, password: str, registry: str = "https://index.docker.io/v1/") -> dict:
    client = get_client()
    res = client.login(username=username, password=password, registry=registry)
    return {"status": "ok", "detail": res}


def build_image(path: str = ".", tag: Optional[str] = None) -> Dict[str, Any]:
    client = get_client()
    image, logs = client.images.build(path=path, tag=tag, rm=True)
    output = "".join(line.get('stream', '') for line in logs)
    return {"image_id": image.id, "tag": tag, "logs": output}


def tag_image(source: str, repo: str, tag: Optional[str] = None) -> dict:
    client = get_client()
    image = client.images.get(source)
    image.tag(repo, tag=tag)
    return {"status": "ok", "source": source, "target": f"{repo}:{tag or 'latest'}"}


def run_container(
    image: str,
    name: Optional[str] = None,
    command: Optional[str] = None,
    ports: Optional[Dict[str, int]] = None,
    env: Optional[Dict[str, str]] = None,
    detach: bool = True,
) -> Dict[str, Any]:
    client = get_client()
    container = client.containers.run(
        image,
        command,
        name=name,
        ports=ports,
        environment=env,
        detach=detach,
    )
    return {"id": container.id, "name": name or container.name, "status": container.status}


def list_containers(all: bool = False) -> List[Dict[str, Any]]:
    client = get_client()
    out = []
    for c in client.containers.list(all=all):
        out.append({
            "id": c.id,
            "name": c.name,
            "image": c.image.tags,
            "status": c.status,
        })
    return out


def stop_container(id_or_name: str) -> Dict[str, Any]:
    client = get_client()
    c = client.containers.get(id_or_name)
    c.stop()
    return {"id": c.id, "name": c.name, "status": c.status}


def remove_container(id_or_name: str, force: bool = False) -> Dict[str, Any]:
    client = get_client()
    c = client.containers.get(id_or_name)
    c.remove(force=force)
    return {"removed": True, "id": c.id, "name": c.name}


def list_images() -> List[Dict[str, Any]]:
    client = get_client()
    imgs = []
    for img in client.images.list():
        imgs.append({"id": img.id, "tags": img.tags})
    return imgs


def remove_image(tag_or_id: str, force: bool = False) -> Dict[str, Any]:
    client = get_client()
    client.images.remove(tag_or_id, force=force)
    return {"removed": True, "image": tag_or_id}


def push_image(repo_tag: str) -> dict:
    client = get_client()
    logs = client.images.push(repo_tag, stream=True, decode=True)
    output = "\n".join(str(l) for l in logs)
    return {"status": "ok", "pushed": repo_tag, "logs": output}


def pull_image(name: str, tag: Optional[str] = None) -> dict:
    client = get_client()
    img = client.images.pull(name, tag=tag)
    return {"status": "ok", "id": img.id, "tags": img.tags}


def list_images() -> List[Dict[str, Any]]:
    client = get_client()
    imgs = []
    for img in client.images.list():
        imgs.append({"id": img.id, "tags": img.tags})
    return imgs


def remove_image(tag_or_id: str, force: bool = False) -> Dict[str, Any]:
    client = get_client()
    client.images.remove(tag_or_id, force=force)
    return {"removed": True, "image": tag_or_id}


def prune_system() -> Dict[str, Any]:
    client = get_client()
    return {
        "containers": client.containers.prune(),
        "images": client.images.prune(),
        "networks": client.networks.prune(),
        # volumes prune requires low-level API
    }


def container_logs(id_or_name: str, tail: int = 100) -> Dict[str, Any]:
    client = get_client()
    c = client.containers.get(id_or_name)
    logs = c.logs(tail=tail).decode("utf-8", errors="ignore")
    return {"id": c.id, "name": c.name, "logs": logs}


def exec_in_container(id_or_name: str, cmd: str) -> Dict[str, Any]:
    client = get_client()
    c = client.containers.get(id_or_name)
    res = c.exec_run(cmd)
    output = res.output.decode("utf-8", errors="ignore") if isinstance(res.output, (bytes, bytearray)) else str(res.output)
    return {"exit_code": res.exit_code, "output": output}


def save_image(repo_tag: str, tar_path: str) -> dict:
    client = get_client()
    img = client.images.get(repo_tag)
    with open(tar_path, "wb") as f:
        for chunk in img.save(named=True):
            f.write(chunk)
    return {"status": "ok", "saved_to": tar_path}


def load_image(tar_path: str) -> dict:
    client = get_client()
    with open(tar_path, "rb") as f:
        data = f.read()
    imgs = client.images.load(data)
    return {"status": "ok", "images": [i.id for i in imgs]}


def create_volume(name: str, driver: str = "local", labels: Optional[Dict[str, str]] = None) -> dict:
    client = get_client()
    v = client.volumes.create(name=name, driver=driver, labels=labels)
    return {"status": "ok", "name": v.name}


def list_volumes() -> list:
    client = get_client()
    return [v.name for v in client.volumes.list()]


def remove_volume(name: str, force: bool = False) -> dict:
    client = get_client()
    v = client.volumes.get(name)
    v.remove(force=force)
    return {"status": "ok", "removed": name}


def create_network(name: str, driver: str = "bridge") -> dict:
    client = get_client()
    n = client.networks.create(name=name, driver=driver)
    return {"status": "ok", "id": n.id, "name": name}


def list_networks() -> list:
    client = get_client()
    return [{"id": n.id, "name": n.name} for n in client.networks.list()]


def remove_network(id_or_name: str) -> dict:
    client = get_client()
    n = client.networks.get(id_or_name)
    n.remove()
    return {"status": "ok", "removed": id_or_name}


def connect_network(network: str, container: str) -> dict:
    client = get_client()
    n = client.networks.get(network)
    c = client.containers.get(container)
    n.connect(c)
    return {"status": "ok"}


def disconnect_network(network: str, container: str) -> dict:
    client = get_client()
    n = client.networks.get(network)
    c = client.containers.get(container)
    n.disconnect(c)
    return {"status": "ok"}


def get_container_stats(id_or_name: str) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    stats = c.stats(stream=False)
    return stats


def top_processes(id_or_name: str) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    return c.top()


def restart_container(id_or_name: str) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    c.restart()
    return {"status": "ok", "id": c.id}


def pause_container(id_or_name: str) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    c.pause()
    return {"status": "ok"}


def unpause_container(id_or_name: str) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    c.unpause()
    return {"status": "ok"}


def kill_container(id_or_name: str, signal: str = "SIGKILL") -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    c.kill(signal=signal)
    return {"status": "ok"}


def update_container_resources(id_or_name: str, mem_limit: Optional[str] = None, nano_cpus: Optional[int] = None, cpu_shares: Optional[int] = None, pids_limit: Optional[int] = None, cpuset_cpus: Optional[str] = None, cpuset_mems: Optional[str] = None, memswap_limit: Optional[str] = None) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    c.update(mem_limit=mem_limit, nano_cpus=nano_cpus, cpu_shares=cpu_shares, pids_limit=pids_limit, cpuset_cpus=cpuset_cpus, cpuset_mems=cpuset_mems, memswap_limit=memswap_limit)
    return {"status": "ok", "id": c.id}


def run_container_extended(
    image: str,
    name: Optional[str] = None,
    command: Optional[str] = None,
    ports: Optional[Dict[str, int]] = None,
    env: Optional[Dict[str, str]] = None,
    volumes: Optional[Dict[str, dict]] = None,
    network: Optional[str] = None,
    mem_limit: Optional[str] = None,
    nano_cpus: Optional[int] = None,
    cpu_shares: Optional[int] = None,
    pids_limit: Optional[int] = None,
    detach: bool = True,
    log_driver: Optional[str] = None,
    log_options: Optional[Dict[str, str]] = None,
    cpuset_cpus: Optional[str] = None,
    cpuset_mems: Optional[str] = None,
    memswap_limit: Optional[str] = None,
    storage_opt: Optional[Dict[str, str]] = None,
) -> dict:
    client = get_client()
    container = client.containers.run(
        image,
        command,
        name=name,
        ports=ports,
        environment=env,
        volumes=volumes,
        network=network,
        mem_limit=mem_limit,
        nano_cpus=nano_cpus,
        cpu_shares=cpu_shares,
        pids_limit=pids_limit,
        cpuset_cpus=cpuset_cpus,
        cpuset_mems=cpuset_mems,
        memswap_limit=memswap_limit,
        storage_opt=storage_opt,
        detach=detach,
        log_config=LogConfig(type=log_driver, config=log_options) if log_driver else None,
    )
    return {"id": container.id, "name": name or container.name, "status": container.status}


def local_run_from_lz4(
    lz4_path_rel: str,
    task_id: str,
    ports: Optional[Dict[str, int]] = None,
    env: Optional[Dict[str, str]] = None,
    command: Optional[str] = None,
    name: Optional[str] = None,
    cpu: Optional[float] = None,
    cpuset: Optional[str] = None,
    memory: Optional[str] = None,
    emit: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Decompress a .tar.lz4, load docker image, and run container.
    Logs into builds/{task_id} and uses stage transitions: building -> decompiling -> running.
    Keeps summary status as 'building'.
    """
    client = get_client()
    base_logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "builds"))
    task_logs_dir = os.path.join(base_logs_dir, task_id)
    os.makedirs(task_logs_dir, exist_ok=True)
    build_log_path = os.path.join(task_logs_dir, "build.log")
    build_structured_path = os.path.join(task_logs_dir, "build.jsonl")
    events_log_path = os.path.join(task_logs_dir, "events.log")
    error_log_path = os.path.join(task_logs_dir, "error.log")
    summary_path = os.path.join(task_logs_dir, "build.info.json")

    def _append_line(path: Optional[str], line: str) -> None:
        if not path:
            return
        try:
            with open(path, "a") as f:
                f.write(line.rstrip("\n") + "\n")
        except Exception:
            pass

    def _append_json(path: Optional[str], obj: Dict[str, Any]) -> None:
        if not path:
            return
        try:
            with open(path, "a") as f:
                f.write(json.dumps(obj) + "\n")
        except Exception:
            pass

    def _write_json(path: Optional[str], obj: Dict[str, Any]) -> None:
        if not path:
            return
        try:
            with open(path, "w") as f:
                json.dump(obj, f, indent=2)
        except Exception:
            pass

    def _ts() -> str:
        return time.strftime('%Y-%m-%dT%H:%M:%S')

    # Resolve paths
    lz4_abs = os.path.abspath(os.path.join(os.getcwd(), lz4_path_rel))
    if not os.path.exists(lz4_abs):
        msg = f"lz4 file not found: {lz4_abs}"
        _append_line(error_log_path, msg)
        _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "localrun_error", "error": msg})
        if emit:
            emit({"task": "docker_localrun", "task_id": task_id, "stage": "decompiling", "status": "error", "error": msg})
        raise FileNotFoundError(msg)

    # Stage: decompiling
    _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] decompiling {os.path.basename(lz4_abs)}")
    _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "decompiling_start", "lz4_path": lz4_abs})
    if emit:
        emit({"task": "docker_localrun", "task_id": task_id, "stage": "decompiling", "status": "starting", "lz4": lz4_abs})

    tar_path = os.path.join(task_logs_dir, os.path.basename(lz4_abs).replace('.lz4', ''))
    if not tar_path.endswith('.tar'):
        tar_path += '.tar'

    try:
        # Try lz4 decompression
        res = subprocess.run(["lz4", "-d", "-f", lz4_abs, tar_path], capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            raise RuntimeError(f"lz4 decompression failed: {res.stderr}")
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] decompiling completed output={tar_path}")
        _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "decompiling_completed", "tar_path": tar_path})
        if emit:
            emit({"task": "docker_localrun", "task_id": task_id, "stage": "decompiling", "status": "completed", "output": tar_path})
    except Exception as e:
        msg = f"decompression error: {e}"
        _append_line(error_log_path, msg)
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
        _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "decompiling_error", "error": str(e)})
        if emit:
            emit({"task": "docker_localrun", "task_id": task_id, "stage": "decompiling", "status": "error", "error": str(e)})
        raise

    # Load image from tar
    _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] loading image from tar")
    _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "image_load_start", "tar_path": tar_path})
    with open(tar_path, "rb") as f:
        img_list = client.images.load(f.read())
    image_id = None
    image_tag = None
    try:
        if img_list:
            image_id = img_list[0].id
            tags = img_list[0].tags
            image_tag = tags[0] if tags else None
    except Exception:
        pass
    _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] image loaded id={image_id or ''} tag={image_tag or ''}")
    _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "image_loaded", "image_id": image_id, "tag": image_tag})

    # Stage: running
    if emit:
        emit({"task": "docker_localrun", "task_id": task_id, "stage": "running", "status": "starting", "image_id": image_id, "tag": image_tag})
    _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] running container")
    _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "run_start", "image_id": image_id, "tag": image_tag})

    # Map cpu to nano_cpus if provided
    nano_cpus = None
    if cpu is not None:
        try:
            nano_cpus = int(float(cpu) * 1_000_000_000)
        except Exception:
            nano_cpus = None

    try:
        run_res = run_container_extended(
            image=image_id or (image_tag or ''),
            name=name,
            command=command,
            ports=ports,
            env=env,
            mem_limit=memory,
            nano_cpus=nano_cpus,
            cpuset_cpus=cpuset,
            detach=True,
        )
        if emit:
            emit({"task": "docker_localrun", "task_id": task_id, "stage": "running", "status": "completed", "container_id": run_res.get("id")})
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] run completed container_id={run_res.get('id')}")
        _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "run_completed", "container": run_res})
    except Exception as e:
        msg = f"run error: {e}"
        _append_line(error_log_path, msg)
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
        _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "run_error", "error": str(e)})
        if emit:
            emit({"task": "docker_localrun", "task_id": task_id, "stage": "running", "status": "error", "error": str(e)})
        raise

    # Update summary but keep status as 'building'
    try:
        if os.path.exists(summary_path):
            with open(summary_path, "r") as f:
                summary = json.load(f)
        else:
            summary = {}
        summary.update({
            "status": "building",
            "stage": "running",
            "localrun": {
                "lz4_path": lz4_abs,
                "tar_path": tar_path,
                "image_id": image_id,
                "image_tag": image_tag,
                "container": run_res,
                "resources": {
                    "cpu": cpu,
                    "cpuset": cpuset,
                    "memory": memory,
                }
            }
        })
        _write_json(summary_path, summary)
    except Exception:
        pass

    return {
        "status": "ok",
        "task_id": task_id,
        "image_id": image_id,
        "tag": image_tag,
        "container": run_res,
    }


def list_path_in_container(id_or_name: str, path: str) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    stream, stat = c.get_archive(path)
    data = b"".join(chunk for chunk in stream)
    tf = tarfile.open(fileobj=io.BytesIO(data))
    entries = []
    for ti in tf.getmembers():
        entries.append({
            "name": ti.name,
            "size": ti.size,
            "type": "dir" if ti.isdir() else "file",
            "mode": ti.mode,
        })
    return {"path": path, "entries": entries, "stat": stat}


def read_file_in_container(id_or_name: str, path: str) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    stream, stat = c.get_archive(path)
    data = b"".join(chunk for chunk in stream)
    tf = tarfile.open(fileobj=io.BytesIO(data))
    content_bytes = b""
    for ti in tf.getmembers():
        if ti.isfile():
            f = tf.extractfile(ti)
            if f:
                content_bytes = f.read()
                break
    text = None
    try:
        text = content_bytes.decode("utf-8")
    except Exception:
        text = None
    b64 = base64.b64encode(content_bytes).decode("ascii")
    return {"path": path, "size": len(content_bytes), "text": text, "base64": b64, "stat": stat}


def write_file_in_container(id_or_name: str, path: str, content: bytes, mode: int = 0o644) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    dest_dir = os.path.dirname(path) or "/"
    filename = os.path.basename(path)
    file_obj = io.BytesIO(content)
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as tf:
        ti = tarfile.TarInfo(name=filename)
        ti.size = len(content)
        ti.mode = mode
        tf.addfile(ti, fileobj=file_obj)
    tar_bytes.seek(0)
    ok = c.put_archive(dest_dir, tar_bytes.read())
    return {"path": path, "written": bool(ok), "bytes": len(content)}


def mkdir_in_container(id_or_name: str, path: str) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    res = c.exec_run(f"mkdir -p {path}")
    return {"path": path, "exit_code": res.exit_code}


def delete_in_container(id_or_name: str, path: str) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    res = c.exec_run(f"rm -rf {path}")
    return {"path": path, "exit_code": res.exit_code}


def inspect_container_details(id_or_name: str) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    info = client.api.inspect_container(c.id, size=True)
    return {
        "id": c.id,
        "name": c.name,
        "image": c.image.tags,
        "state": info.get("State"),
        "created": info.get("Created"),
        "size_rw": info.get("SizeRw"),
        "size_root_fs": info.get("SizeRootFs"),
        "mounts": info.get("Mounts"),
        "config": info.get("Config"),
        "network_settings": info.get("NetworkSettings"),
    }

# Blue-green deploy helpers and orchestration

def _extract_volumes_from_container(container) -> Dict[str, dict]:
    mounts = container.attrs.get("Mounts", [])
    volumes: Dict[str, dict] = {}
    for m in mounts:
        dest = m.get("Destination")
        mode = "rw" if m.get("RW", True) else "ro"
        t = m.get("Type")
        if t == "bind":
            src = m.get("Source")
            if src and dest:
                volumes[src] = {"bind": dest, "mode": mode}
        elif t == "volume":
            name = m.get("Name")
            if name and dest:
                volumes[name] = {"bind": dest, "mode": mode}
    return volumes


def _extract_env_from_container(container) -> Dict[str, str]:
    env_list = (container.attrs.get("Config", {}) or {}).get("Env", []) or []
    env: Dict[str, str] = {}
    for e in env_list:
        if isinstance(e, str) and "=" in e:
            k, v = e.split("=", 1)
            env[k] = v
    return env


def blue_green_deploy(base_id_or_name: str, image_repo: str, tag: str, wait_seconds: int = 15) -> dict:
    client = get_client()
    old = client.containers.get(base_id_or_name)
    base_name = old.name
    suffix = "".join(random.choices("0123456789", k=5))
    new_name = f"{base_name}-{suffix}"

    # Reuse volumes and environment from the old container
    volumes = _extract_volumes_from_container(old)
    env = _extract_env_from_container(old)

    # Prefer same primary network; avoid host port conflicts by not binding ports
    networks = list(((old.attrs.get("NetworkSettings", {}) or {}).get("Networks", {}) or {}).keys())
    network = networks[0] if networks else None

    full_image = f"{image_repo}:{tag}" if tag else image_repo
    new = client.containers.run(
        full_image,
        name=new_name,
        environment=env or None,
        volumes=volumes or None,
        network=network,
        detach=True,
    )

    time.sleep(max(0, int(wait_seconds)))
    new.reload()
    state = client.api.inspect_container(new.id).get("State", {})
    running = bool(state.get("Running")) and new.status == "running"

    if not running:
        logs = ""
        try:
            out = new.logs(tail=100)
            logs = out.decode("utf-8", errors="ignore") if isinstance(out, (bytes, bytearray)) else str(out)
        except Exception:
            pass
        return {
            "status": "stopped",
            "new_container": {"id": new.id, "name": new.name, "suffix": suffix},
            "reason": {
                "exit_code": state.get("ExitCode"),
                "error": state.get("Error"),
                "oom_killed": state.get("OOMKilled"),
                "state": state,
            },
            "logs": logs,
        }

    # Dummy proxy update step to "pick" the new container
    time.sleep(1)

    # Stop old container after proxy points to new one
    try:
        old.stop()
        old_stopped = True
    except Exception as e:
        old_stopped = False
        stop_error = str(e)
    else:
        stop_error = None

    return {
        "status": "promoted",
        "old_stopped": old_stopped,
        "stop_error": stop_error,
        "base_name": base_name,
        "new_container": {"id": new.id, "name": new_name, "suffix": suffix},
        "network": network,
        "volumes_reused": bool(volumes),
        "wait_seconds": wait_seconds,
    }


def transfer_build_to_sftp(build_dir: str, task_id: str, sftp_host: str, sftp_username: str, sftp_password: Optional[str] = None, sftp_port: int = 22, emit: Optional[Callable[[Dict[str, Any]], None]] = None, app_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Transfer the entire build directory to SFTP server at /upload/pxxl/applications/{task_id}
    """
    def _emit_log(message: str, level: str = "info"):
        if emit:
            emit({
                "task": "sftp_transfer",
                "task_id": task_id,
                "stage": "deployment",
                "status": "progress",
                "message": message,
                "level": level,
                "app_id": app_id,
            })
    
    try:
        _emit_log("Starting SFTP transfer to production server")
        
        # Create SSH client
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Connect to SFTP server
        _emit_log(f"Connecting to SFTP server {sftp_host}:{sftp_port}")
        ssh.connect(
            hostname=sftp_host,
            port=sftp_port,
            username=sftp_username,
            password=sftp_password,
            timeout=30
        )
        
        # Create SFTP client
        sftp = ssh.open_sftp()
        
        # Create remote directory path
        remote_base_dir = "/upload/pxxl/applications"
        remote_dir = f"{remote_base_dir}/{task_id}"
        
        _emit_log(f"Ensuring remote directory exists: {remote_dir}")
        
        # Robust mkdir -p implementation for SFTP (create parents step-by-step)
        def ensure_remote_dirs(path: str):
            parts = [p for p in path.split('/') if p]
            current = '/' if path.startswith('/') else ''
            for part in parts:
                next_path = posixpath.join(current, part) if current not in ('', '/') else ('/' + part if current == '/' else part)
                try:
                    sftp.stat(next_path)
                except IOError:
                    try:
                        sftp.mkdir(next_path)
                    except IOError:
                        pass
                current = next_path
        
        # Ensure base and target directory exist
        ensure_remote_dirs(remote_base_dir)
        ensure_remote_dirs(remote_dir)
        
        # Transfer all files and directories recursively
        _emit_log(f"Transferring build directory from {build_dir} to {remote_dir}")
        
        def upload_recursive(local_path: str, remote_path: str):
            for item in os.listdir(local_path):
                local_item = os.path.join(local_path, item)
                remote_item = f"{remote_path}/{item}"
                
                if os.path.isfile(local_item):
                    _emit_log(f"Uploading file: {item}")
                    sftp.put(local_item, remote_item)
                elif os.path.isdir(local_item):
                    _emit_log(f"Ensuring remote subdirectory exists: {remote_item}")
                    try:
                        # Create parent directories step-by-step
                        parts = [p for p in remote_item.split('/') if p]
                        current = '/' if remote_item.startswith('/') else ''
                        for part in parts:
                            next_path = posixpath.join(current, part) if current not in ('', '/') else ('/' + part if current == '/' else part)
                            try:
                                sftp.stat(next_path)
                            except IOError:
                                try:
                                    sftp.mkdir(next_path)
                                except IOError:
                                    pass
                            current = next_path
                    except Exception:
                        pass
                    upload_recursive(local_item, remote_item)
        
        upload_recursive(build_dir, remote_dir)
        
        # Get transfer statistics
        total_size = 0
        file_count = 0
        for root, dirs, files in os.walk(build_dir):
            for file in files:
                file_path = os.path.join(root, file)
                total_size += os.path.getsize(file_path)
                file_count += 1
        
        _emit_log(f"Transfer completed successfully. Files: {file_count}, Total size: {total_size} bytes")
        
        # Close connections
        sftp.close()
        ssh.close()
        
        return {
            "status": "success",
            "remote_path": remote_dir,
            "files_transferred": file_count,
            "total_size_bytes": total_size,
            "sftp_host": sftp_host,
            "sftp_port": sftp_port
        }
        
    except Exception as e:
        error_msg = f"SFTP transfer failed: {str(e)}"
        _emit_log(error_msg, "error")
        return {
            "status": "error",
            "error": error_msg,
            "sftp_host": sftp_host,
            "sftp_port": sftp_port
        }

def stream_build_image(context_path: str, tag: Optional[str] = None, dockerfile: Optional[str] = None, build_args: Optional[Dict[str, str]] = None, task_id: Optional[str] = None, override_log_endpoint: Optional[str] = None, dockerfile_content: Optional[str] = None, dockerfile_name: Optional[str] = None, cleanup: Optional[bool] = True, emit: Optional[Callable[[Dict[str, Any]], None]] = None, app_id: Optional[str] = None, sftp_host: Optional[str] = None, sftp_username: Optional[str] = None, sftp_password: Optional[str] = None, sftp_port: Optional[int] = 22) -> dict:
    client = get_client()
    endpoint = override_log_endpoint or get_log_endpoint()
    headers = get_auth_header()
    logs_collected: List[Dict[str, Any]] = []

    # Prepare on-disk logging paths per task_id
    base_logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "builds"))
    task_logs_dir = None
    build_log_path = None
    build_structured_path = None
    events_log_path = None
    error_log_path = None
    parsed_dockerfile_path = None
    summary_path = None
    if task_id:
        os.makedirs(base_logs_dir, exist_ok=True)
        task_logs_dir = os.path.join(base_logs_dir, task_id)
        os.makedirs(task_logs_dir, exist_ok=True)
        build_log_path = os.path.join(task_logs_dir, "build.log")
        build_structured_path = os.path.join(task_logs_dir, "build.jsonl")
        events_log_path = os.path.join(task_logs_dir, "events.log")
        error_log_path = os.path.join(task_logs_dir, "error.log")
        parsed_dockerfile_path = os.path.join(task_logs_dir, "dockerfile.parsed.json")
        summary_path = os.path.join(task_logs_dir, "build.info.json")
        try:
            # Initialize build.log so it always exists
            now = time.strftime('%Y-%m-%d %H:%M:%S')
            with open(build_log_path, "a") as f:
                f.write(f"[{now}] build initialized tag={tag or ''} context={context_path} app_id={app_id or ''}\n")
            with open(events_log_path, "a") as f:
                f.write(f"[{now}] build starting tag={tag or ''} context={context_path} app_id={app_id or ''}\n")
        except Exception:
            pass

    def _append_line(path: Optional[str], line: str) -> None:
        if not path:
            return
        try:
            with open(path, "a") as f:
                f.write(line.rstrip("\n") + "\n")
        except Exception:
            pass

    def _append_json(path: Optional[str], obj: Dict[str, Any]) -> None:
        if not path:
            return
        try:
            with open(path, "a") as f:
                f.write(json.dumps(obj) + "\n")
        except Exception:
            pass

    def _write_json(path: Optional[str], obj: Dict[str, Any]) -> None:
        if not path:
            return
        try:
            with open(path, "w") as f:
                json.dump(obj, f, indent=2)
        except Exception:
            pass

    def _ts() -> str:
        return time.strftime('%Y-%m-%dT%H:%M:%S')

    build_started = time.time()
    current_step: Optional[Dict[str, Any]] = None
    steps: List[Dict[str, Any]] = []

    # Regex patterns to classify build output
    step_re = re.compile(r"^Step\s+(\d+)\/(\d+)\s*:\s*(.+)$")
    layer_re = re.compile(r"^\s*--->\s+([a-f0-9]+)")
    using_cache_re = re.compile(r"^\s*--->\s+Using cache")
    running_in_re = re.compile(r"^\s*Running in\s+([a-f0-9]+)")
    success_built_re = re.compile(r"^\s*Successfully built\s+([a-f0-9]+)")
    success_tagged_re = re.compile(r"^\s*Successfully tagged\s+(.+)")
    created_path = None
    df_arg = dockerfile
    used_dockerfile_content: Optional[str] = None
    image_id: Optional[str] = None
    try:
        # If Dockerfile content is provided, write it into context
        if dockerfile_content:
            name = dockerfile_name or "Dockerfile"
            created_path = os.path.join(context_path, name)
            os.makedirs(context_path, exist_ok=True)
            with open(created_path, "w") as f:
                f.write(dockerfile_content)
            used_dockerfile_content = dockerfile_content
            # Log context and Dockerfile creation
            _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] context prepared path={context_path} dockerfile_name={name}")
            _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] context prepared path={context_path} dockerfile_name={name}")
            _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "context_prepared", "context_path": context_path, "dockerfile_name": name})
            # Copy into task log folder for auditability
            try:
                if task_logs_dir:
                    with open(os.path.join(task_logs_dir, "Dockerfile.used"), "w") as df_out:
                        df_out.write(dockerfile_content)
            except Exception:
                pass
            # For non-standard name, pass dockerfile arg
            df_arg = None if name == "Dockerfile" else name
        else:
            # If a dockerfile path is provided, copy its content for audit
            try:
                df_path = os.path.join(context_path, df_arg or "Dockerfile")
                if os.path.exists(df_path):
                    with open(df_path, "r") as f:
                        used_dockerfile_content = f.read()
                    if task_logs_dir:
                        with open(os.path.join(task_logs_dir, "Dockerfile.used"), "w") as df_out:
                            df_out.write(used_dockerfile_content)
            except Exception:
                pass

        # Parse Dockerfile to capture ops for logs
        if used_dockerfile_content:
            try:
                lines = used_dockerfile_content.splitlines()
                for i, ln in enumerate(lines, start=1):
                    ln_stripped = ln.strip()
                    if not ln_stripped or ln_stripped.startswith('#'):
                        continue
                    op = ln_stripped.split()[0].upper()
                    args = ln_stripped[len(op):].strip() if len(ln_stripped) > len(op) else ""
                    steps.append({"line": i, "op": op, "instruction": ln_stripped, "args": args})
                _write_json(parsed_dockerfile_path, {"ops": steps})
            except Exception:
                pass

        # Emit starting event
        if emit:
            try:
                emit({
                    "task": "docker_build",
                    "task_id": task_id,
                    "stage": "build",
                    "status": "starting",
                    "tag": tag,
                    "context_path": context_path,
                    "dockerfile": df_arg or "Dockerfile",
                    "inline": bool(dockerfile_content),
                    "app_id": app_id,
                    "build_args": build_args or {},
                })
            except Exception:
                pass
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] build starting dockerfile={df_arg or 'Dockerfile'} inline={bool(dockerfile_content)} app_id={app_id or ''}")
        _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] build starting dockerfile={df_arg or 'Dockerfile'} inline={bool(dockerfile_content)} app_id={app_id or ''}")
        _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "build_start", "tag": tag, "dockerfile": df_arg or "Dockerfile", "inline": bool(dockerfile_content), "app_id": app_id, "build_args": build_args or {}})
        # Log build invocation details
        _append_json(build_structured_path, {
            "ts": _ts(), "level": "info", "event": "invocation",
            "context_path": context_path, "dockerfile": df_arg or "Dockerfile",
            "tag": tag, "build_args": build_args or {}, "app_id": app_id,
        })

        # Use low-level API to stream logs in real-time
        logs = client.api.build(
            path=context_path,
            tag=tag,
            dockerfile=df_arg,
            buildargs=build_args or {},
            rm=True,
            forcerm=True,
            decode=True,
        )
        step_count = 0
        for chunk in logs:
            logs_collected.append(chunk)
            # Derive raw text and type
            raw_text = None
            entry_type = "stdout"
            if isinstance(chunk, dict):
                if "stream" in chunk:
                    raw_text = chunk.get("stream")
                    entry_type = "stdout"
                elif "status" in chunk:
                    raw_text = chunk.get("status")
                    entry_type = "stdout"
                elif "errorDetail" in chunk:
                    raw_text = chunk.get("errorDetail", {}).get("message") or chunk.get("error") or str(chunk)
                    entry_type = "error"
                else:
                    raw_text = str(chunk)
            if not raw_text:
                raw_text = str(chunk)
            # Write raw to build.log
            prefix = "[ERROR]" if entry_type == "error" else "[INFO ]"
            _append_line(build_log_path, f"{prefix} {raw_text}")
            # Track output size per step
            try:
                if current_step:
                    current_step["output_bytes"] = current_step.get("output_bytes", 0) + len(raw_text.encode("utf-8", errors="ignore"))
            except Exception:
                pass

            # Structured log with level and step context
            level = "error" if entry_type == "error" else "info"
            entry: Dict[str, Any] = {
                "ts": _ts(),
                "level": level,
                "type": "output",
                "subtype": entry_type,
                "event": "chunk",
                "raw": raw_text,
            }
            if current_step:
                entry["step_index"] = current_step.get("step_index")
            if isinstance(chunk, dict):
                entry["keys"] = list(chunk.keys())

            # Parse step starts and associate commands
            m = step_re.match(raw_text)
            if m:
                # Close previous step if open
                if current_step:
                    current_step["end"] = time.time()
                    current_step["duration_sec"] = round(current_step["end"] - current_step["start"], 3)
                    _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "step_end", **{k: v for k, v in current_step.items() if k != "start"}})
                # Start new step
                instr = m.group(3)
                op = instr.split()[0] if instr else ""
                cmd = instr[len(op):].strip() if instr and op else ""
                step_count += 1
                current_step = {
                    "step_index": int(m.group(1)),
                    "step_total": int(m.group(2)),
                    "instruction": instr,
                    "op": op,
                    "command": cmd,
                    "start": time.time(),
                }
                _append_json(build_structured_path, {
                    "ts": _ts(),
                    "level": "info",
                    "event": "step_start",
                    "type": "command",
                    "step_index": current_step["step_index"],
                    "step_total": current_step["step_total"],
                    "instruction": current_step["instruction"],
                    "op": current_step["op"],
                    "command": current_step["command"],
                })
            else:
                # Detect layer and cache events
                if using_cache_re.match(raw_text):
                    if current_step:
                        current_step["used_cache"] = True
                    _append_json(build_structured_path, {
                        "ts": _ts(), "level": "info", "event": "layer", "action": "using_cache",
                        "step_index": current_step.get("step_index") if current_step else None,
                    })
                m_layer = layer_re.match(raw_text)
                if m_layer:
                    lid = m_layer.group(1)
                    if current_step:
                        current_step["layer_id"] = lid
                    _append_json(build_structured_path, {
                        "ts": _ts(), "level": "info", "event": "layer", "action": "layer_id",
                        "layer_id": lid,
                        "step_index": current_step.get("step_index") if current_step else None,
                    })
                m_run = running_in_re.match(raw_text)
                if m_run:
                    cid = m_run.group(1)
                    if current_step:
                        current_step["container_id"] = cid
                    _append_json(build_structured_path, {
                        "ts": _ts(), "level": "info", "event": "container", "action": "running_in",
                        "container_id": cid,
                        "step_index": current_step.get("step_index") if current_step else None,
                    })
                # Log BuildKit progress as structured entries
                if isinstance(chunk, dict) and ("id" in chunk or "progressDetail" in chunk or "progress" in chunk):
                    _append_json(build_structured_path, {
                        "ts": _ts(), "level": "info", "event": "buildkit",
                        "id": chunk.get("id"), "status": chunk.get("status"),
                        "progress": chunk.get("progress"), "progressDetail": chunk.get("progressDetail"),
                        "step_index": current_step.get("step_index") if current_step else None,
                    })
                # Detect successful build and tag lines (BuildKit classic output)
                m_built = success_built_re.match(raw_text)
                if m_built:
                    image_id = m_built.group(1)
                    _append_json(build_structured_path, {
                        "ts": _ts(), "level": "info", "event": "built", "image_id": image_id,
                    })
                m_tagged = success_tagged_re.match(raw_text)
                if m_tagged:
                    _append_json(build_structured_path, {
                        "ts": _ts(), "level": "info", "event": "tagged", "tag": m_tagged.group(1),
                    })
                # Emit chunk as structured output
                _append_json(build_structured_path, entry)

            # Forward to external and emit callback
            if endpoint:
                payload = {
                    "task": "docker_build",
                    "task_id": task_id,
                    "tag": tag,
                    "context_path": context_path,
                    "dockerfile": df_arg or "Dockerfile",
                    "inline": bool(dockerfile_content),
                    "chunk": chunk,
                    "app_id": app_id,
                }
                try:
                    requests.post(endpoint, json=payload, headers=headers, timeout=3)
                except Exception:
                    pass
            if emit:
                try:
                    emit({
                        "task": "docker_build",
                        "task_id": task_id,
                        "stage": "build",
                        "status": "stream",
                        "chunk": chunk,
                        "app_id": app_id,
                    })
                except Exception:
                    pass
        # Close last step if open
        if current_step:
            current_step["end"] = time.time()
            current_step["duration_sec"] = round(current_step["end"] - current_step["start"], 3)
            _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "step_end", **{k: v for k, v in current_step.items() if k != "start"}})
        # Emit completed event
        if emit:
            try:
                emit({
                    "task": "docker_build",
                    "task_id": task_id,
                    "stage": "build",
                    "status": "completed",
                    "image_id": image_id,
                    "tag": tag,
                    "app_id": app_id,
                })
            except Exception:
                pass
        total_dur = round(time.time() - build_started, 3)
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] build completed image_id={image_id or ''} duration={total_dur}s app_id={app_id or ''}")
        _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] build completed image_id={image_id or ''} duration={total_dur}s app_id={app_id or ''}")
        _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "build_completed", "image_id": image_id, "tag": tag, "duration_sec": total_dur, "app_id": app_id})
        # Export and compress the built image
        compressed_image_path = None
        if task_logs_dir and image_id:
            try:
                import subprocess
                import gzip
                
                # Export the Docker image to a tar file
                export_start = time.time()
                tar_path = os.path.join(task_logs_dir, f"{tag.replace(':', '_').replace('/', '_')}.tar")
                _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] exporting image {image_id} to {tar_path}")
                _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "image_export_start", "image_id": image_id, "tar_path": tar_path})
                
                # Use docker save to export the image
                with open(tar_path, "wb") as f:
                    image = client.images.get(image_id)
                    for chunk in image.save():
                        f.write(chunk)
                
                export_dur = round(time.time() - export_start, 3)
                tar_size = os.path.getsize(tar_path)
                _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] image exported size={tar_size} bytes duration={export_dur}s")
                _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "image_exported", "tar_path": tar_path, "size_bytes": tar_size, "duration_sec": export_dur})
                
                # Compress with lz4 (fallback to gzip if lz4 not available)
                compress_start = time.time()
                compressed_path = tar_path + ".gz"
                _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] compressing {tar_path} with lz4/gzip")
                _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "compression_start", "input_path": tar_path, "output_path": compressed_path})
                
                # Try lz4 first, fallback to gzip
                compression_method = "gzip"
                try:
                    # Try lz4 compression
                    result = subprocess.run(["lz4", "-z", "-f", tar_path, compressed_path.replace('.gz', '.lz4')], 
                                          capture_output=True, text=True, timeout=300)
                    if result.returncode == 0:
                        compressed_path = compressed_path.replace('.gz', '.lz4')
                        compression_method = "lz4"
                    else:
                        raise Exception(f"lz4 failed: {result.stderr}")
                except Exception as lz4_error:
                    # Fallback to gzip compression
                    _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] lz4 failed, using gzip: {lz4_error}")
                    with open(tar_path, "rb") as f_in:
                        with gzip.open(compressed_path, "wb") as f_out:
                            f_out.writelines(f_in)
                
                compress_dur = round(time.time() - compress_start, 3)
                compressed_size = os.path.getsize(compressed_path)
                compression_ratio = round((1 - compressed_size / tar_size) * 100, 1) if tar_size > 0 else 0
                
                _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] compression completed method={compression_method} size={compressed_size} bytes ratio={compression_ratio}% duration={compress_dur}s")
                _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "compression_completed", 
                                                   "method": compression_method, "compressed_path": compressed_path, 
                                                   "compressed_size_bytes": compressed_size, "original_size_bytes": tar_size,
                                                   "compression_ratio_percent": compression_ratio, "duration_sec": compress_dur})
                
                # Remove the uncompressed tar file to save space
                try:
                    os.remove(tar_path)
                    _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] removed uncompressed tar file {tar_path}")
                except Exception:
                    pass
                
                compressed_image_path = compressed_path
                
            except Exception as export_error:
                _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] image export/compression failed: {export_error}")
                _append_line(error_log_path, f"Image export/compression error: {export_error}")
                _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "export_compression_error", "error": str(export_error)})

        # SFTP Transfer to production server if SFTP parameters are provided
        sftp_result = None
        if sftp_host and sftp_username and task_logs_dir:
            try:
                _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] starting SFTP transfer to production server")
                _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "sftp_transfer_start", "sftp_host": sftp_host, "sftp_port": sftp_port})
                
                sftp_result = transfer_build_to_sftp(
                    build_dir=task_logs_dir,
                    task_id=task_id,
                    sftp_host=sftp_host,
                    sftp_username=sftp_username,
                    sftp_password=sftp_password,
                    sftp_port=sftp_port or 22,
                    emit=emit,
                    app_id=app_id
                )
                
                if sftp_result["status"] == "success":
                    _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SFTP transfer completed successfully to {sftp_result['remote_path']}")
                    _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "sftp_transfer_completed", 
                                                       "remote_path": sftp_result["remote_path"], 
                                                       "files_transferred": sftp_result["files_transferred"],
                                                       "total_size_bytes": sftp_result["total_size_bytes"]})
                else:
                    _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SFTP transfer failed: {sftp_result['error']}")
                    _append_line(error_log_path, f"SFTP transfer error: {sftp_result['error']}")
                    _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "sftp_transfer_error", "error": sftp_result["error"]})
                    
            except Exception as sftp_error:
                error_msg = f"SFTP transfer exception: {str(sftp_error)}"
                _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {error_msg}")
                _append_line(error_log_path, error_msg)
                _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "sftp_transfer_exception", "error": error_msg})
                sftp_result = {"status": "error", "error": error_msg}

        # Write a summary JSON for convenience
        try:
            if task_logs_dir:
                summary_data = {
                    "status": "ok",
                    "image_id": image_id,
                    "tag": tag,
                    "dockerfile_used": df_arg or "Dockerfile",
                    "inline": bool(dockerfile_content),
                    "duration_sec": total_dur,
                    "steps_detected": len(steps),
                    "app_id": app_id,
                    "build_args": build_args or {},
                }
                if compressed_image_path:
                    summary_data["compressed_image_path"] = compressed_image_path
                    summary_data["compressed_image_size_bytes"] = os.path.getsize(compressed_image_path) if os.path.exists(compressed_image_path) else 0
                if sftp_result:
                    summary_data["sftp_deployment"] = sftp_result
                _write_json(summary_path, summary_data)
        except Exception:
            pass
        return {
            "status": "ok",
            "image_id": image_id,
            "tag": tag,
            "dockerfile_used": df_arg or "Dockerfile",
            "inline": bool(dockerfile_content),
            "logs": logs_collected[-10:],
        }
    except Exception as e:
        # Append error logs
        msg = str(e)
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] build error {msg} app_id={app_id or ''}")
        _append_line(error_log_path, msg)
        _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] build error {msg} app_id={app_id or ''}")
        _append_json(build_structured_path, {"ts": _ts(), "level": "error", "type": "error", "event": "build_error", "message": msg, "app_id": app_id})
        if endpoint:
            payload = {
                "task": "docker_build",
                "task_id": task_id,
                "tag": tag,
                "context_path": context_path,
                "dockerfile": df_arg or "Dockerfile",
                "inline": bool(dockerfile_content),
                "error": msg,
                "app_id": app_id,
            }
            try:
                requests.post(endpoint, json=payload, headers=headers, timeout=3)
            except Exception:
                pass
        if emit:
            try:
                emit({
                    "task": "docker_build",
                    "task_id": task_id,
                    "stage": "build",
                    "status": "error",
                    "error": msg,
                    "app_id": app_id,
                })
            except Exception:
                pass
        # Write failure summary
        try:
            if summary_path:
                _write_json(summary_path, {
                    "status": "error",
                    "error": msg,
                    "tag": tag,
                    "dockerfile_used": df_arg or "Dockerfile",
                    "inline": bool(dockerfile_content),
                    "duration_sec": round(time.time() - build_started, 3),
                    "steps_detected": len(steps),
                    "app_id": app_id,
                    "build_args": build_args or {},
                })
        except Exception:
            pass
        raise
    finally:
        if created_path and cleanup:
            try:
                os.remove(created_path)
            except Exception:
                pass