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
import threading
from config_service import get_log_endpoint, get_auth_header


def get_client() -> docker.DockerClient:
    """Create a Docker client using environment config.
    Works against local Docker. If running in a container, mount /var/run/docker.sock.
    """
    try:
        return docker.from_env()
    except Exception as e:
        raise RuntimeError(f"Failed to connect to Docker daemon: {e}")


def _append_error_log(msg: str) -> None:
    try:
        p = os.path.join(os.path.dirname(__file__), "log.txt")
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        with open(p, "a") as f:
            f.write(f"[{now}] {msg}\n")
    except Exception:
        pass


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


def prune_images() -> Dict[str, Any]:
    client = get_client()
    return client.images.prune()


def prune_system() -> Dict[str, Any]:
    client = get_client()
    return {
        "containers": client.containers.prune(),
        "images": client.images.prune(),
        "networks": client.networks.prune(),
        # volumes prune requires low-level API
    }


def container_logs(id_or_name: str, tail: int = 100, timestamps: bool = False) -> Dict[str, Any]:
    client = get_client()
    c = client.containers.get(id_or_name)
    raw = c.logs(tail=tail, timestamps=timestamps)
    text = raw.decode("utf-8", errors="ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
    lines = text.splitlines()
    return {"id": c.id, "name": c.name, "logs": text, "lines": lines, "timestamps": timestamps}


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
    cpu_quota = None
    cpu_period = None
    if nano_cpus is not None:
        try:
            cpu_period = 100000
            cpu_quota = int((int(nano_cpus) / 1000000000) * cpu_period)
        except Exception:
            cpu_quota = None
            cpu_period = None
    kwargs = {}
    if mem_limit is not None:
        kwargs["mem_limit"] = mem_limit
    if cpu_shares is not None:
        kwargs["cpu_shares"] = cpu_shares
    if cpuset_cpus is not None:
        kwargs["cpuset_cpus"] = cpuset_cpus
    if cpuset_mems is not None:
        kwargs["cpuset_mems"] = cpuset_mems
    if cpu_quota is not None and cpu_period is not None:
        kwargs["cpu_quota"] = cpu_quota
        kwargs["cpu_period"] = cpu_period
    c.update(**kwargs)
    return {"status": "ok", "id": c.id}

def recreate_with_resources(id_or_name: str, mem_limit: Optional[str] = None, nano_cpus: Optional[int] = None, cpu_shares: Optional[int] = None, cpuset_cpus: Optional[str] = None, cpuset_mems: Optional[str] = None, memswap_limit: Optional[str] = None) -> dict:
    client = get_client()
    container = client.containers.get(id_or_name)
    env = _extract_env_from_container(container)
    cfg = container.attrs.get("Config", {}) or {}
    cmd_list = cfg.get("Cmd")
    command = None
    if isinstance(cmd_list, list) and cmd_list:
        if len(cmd_list) == 1:
            command = cmd_list[0]
        else:
            command = " ".join(cmd_list)
    elif isinstance(cmd_list, str) and cmd_list.strip():
        command = cmd_list
    image = (container.image.tags[0] if container.image.tags else container.image.id)
    name = container.name
    network_mode = (container.attrs.get("HostConfig", {}) or {}).get("NetworkMode")
    host_config = container.attrs.get("HostConfig", {}) or {}
    port_bindings = host_config.get("PortBindings", {}) or {}
    ports = {}
    for container_port, host_bindings in port_bindings.items():
        if host_bindings and len(host_bindings) > 0:
            host_port = host_bindings[0].get("HostPort")
            if host_port:
                ports[container_port] = int(host_port)
    volumes = _extract_volumes_from_container(container)
    try:
        container.stop()
    except Exception:
        pass
    try:
        container.remove(force=True)
    except Exception:
        pass
    run_res = run_container_extended(
        image=image,
        name=name,
        command=command,
        env=env,
        volumes=volumes,
        network=network_mode,
        ports=ports if ports else None,
        mem_limit=mem_limit,
        nano_cpus=nano_cpus,
        cpu_shares=cpu_shares,
        cpuset_cpus=cpuset_cpus,
        cpuset_mems=cpuset_mems,
        memswap_limit=memswap_limit,
        detach=True,
    )
    return run_res


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
    labels: Optional[Dict[str, str]] = None,
) -> dict:
    client = get_client()
    try:
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
            labels=labels,
        )
        return {"id": container.id, "name": name or container.name, "status": container.status}
    except docker.errors.APIError as e:
        _append_error_log(f"docker run error {e}")
        msg = str(e)
        code = getattr(e, "status_code", None)
        conflict = bool(code == 409 or ("Conflict" in msg and "already in use" in msg))
        if conflict and name:
            try:
                existing = client.containers.get(name)
                try:
                    existing.remove(force=True)
                    _append_error_log(f"removed existing container id={existing.id} name={name}")
                except Exception as re:
                    _append_error_log(f"remove existing container failed name={name} error={re}")
                try:
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
                        labels=labels,
                    )
                    return {"id": container.id, "name": name or container.name, "status": container.status}
                except docker.errors.APIError as e2:
                    _append_error_log(f"docker run retry error {e2}")
                    suffix = str(int(time.time()))
                    new_name = f"{name}-{suffix}"
                    container = client.containers.run(
                        image,
                        command,
                        name=new_name,
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
                        labels=labels,
                    )
                    return {"id": container.id, "name": new_name, "status": container.status}
            except Exception as ge:
                _append_error_log(f"conflict handling failed name={name} error={ge}")
        raise


def local_run_from_lz4(
    lz4_path_rel: str,
    task_id: str,
    app_id: Optional[str] = None,
    ports: Optional[Dict[str, int]] = None,
    env: Optional[Dict[str, str]] = None,
    command: Optional[str] = None,
    name: Optional[str] = None,
    cpu: Optional[float] = None,
    cpuset: Optional[str] = None,
    memory: Optional[str] = None,
    emit: Optional[Callable[[Dict[str, Any]], None]] = None,
    volume_name: Optional[str] = None,
    mount_path: Optional[str] = None,
    mode: Optional[str] = "rw",
    labels: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Decompress a .tar.lz4, load docker image, and run container.
    Logs into builds/{task_id} and uses stage transitions: building -> decompiling -> running.
    Keeps summary status as 'building'.
    """
    client = get_client()
    base_logs_dir = os.path.abspath(os.path.join("/app/upload"))
    task_logs_dir = os.path.join(base_logs_dir, task_id)
    os.makedirs(task_logs_dir, exist_ok=True)
    build_log_path = os.path.join(task_logs_dir, "build.log") 
    build_structured_path = os.path.join(task_logs_dir, "build.jsonl")
    events_log_path = os.path.join(task_logs_dir, "events.log")
    error_log_path = os.path.join(task_logs_dir, "error.log")
    summary_path = os.path.join(task_logs_dir, "build.info.json")
    # File to store raw container stdout/stderr captured during local run
    container_logs_path = os.path.join(task_logs_dir, "container.logs.txt")
    # Prefer app_id from build.info.json over payload 
    try:
        with open(summary_path, "r") as f:
            _summary_obj = json.load(f)
        if not app_id:
            app_id = _summary_obj.get("app_id")
        if not app_id:
            _tag = _summary_obj.get("tag")
            if isinstance(_tag, str) and ":" in _tag:
                app_id = _tag.split(":", 1)[0]
    except Exception:
        pass

    def _append_line(path: Optional[str], line: str) -> None:
        if not path:
            return
        try:
            msg = line.rstrip("\n")
            # Prepend timestamp if missing
            if not re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", msg):
                now = time.strftime('%Y-%m-%d %H:%M:%S')
                msg = f"[{now}] {msg}"
            with open(path, "a") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def _append_json(path: Optional[str], obj: Dict[str, Any]) -> None:
        if not path:
            return
        try:
            # Ensure timestamp exists in structured logs
            obj.setdefault("ts", _ts())
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
    lz4_abs = lz4_path_rel if os.path.isabs(lz4_path_rel) else os.path.abspath(os.path.join(os.getcwd(), lz4_path_rel))
    if not os.path.exists(lz4_abs):
        msg = f"lz4 file not found: {lz4_abs}"
        _append_line(error_log_path, msg)
        _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "localrun_error", "error": msg})
        if emit:
            emit({"task": "docker_localrun", "task_id": task_id, "stage": "decompiling", "status": "error", "error": msg})
        raise FileNotFoundError(msg)

    # Place a copy under /app/upload/{task_id} so recorded path reflects runtime location
    basename_src = os.path.basename(lz4_abs)
    dest_lz4 = os.path.join(task_logs_dir, basename_src)
    try:
        if lz4_abs != dest_lz4:
            shutil.copyfile(lz4_abs, dest_lz4)
    except Exception as e:
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] copy warning {e}")
    lz4_for_decomp = dest_lz4 if os.path.exists(dest_lz4) else lz4_abs
    basename_for_ops = os.path.basename(lz4_for_decomp)
    compressionapthname = f"{task_id}+{basename_for_ops}"

    # Stage: decompiling
    _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl decompile: {basename_for_ops}")
    _append_line(build_log_path, f"[INFO ] pxxl decompile: {basename_for_ops}")
    _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "decompiling_start", "stage": "decompiling", "status": "starting", "lz4_path": lz4_for_decomp, "compressionapthname": compressionapthname, "command": "pxxl launch decompile"})
    if emit:
        emit({"task": "docker_localrun", "task_id": task_id, "stage": "decompiling", "status": "starting", "lz4": lz4_for_decomp, "compressionapthname": compressionapthname})

    if basename_for_ops.endswith('.gz'):
        tar_path = os.path.join(task_logs_dir, basename_for_ops[:-3])
        if not tar_path.endswith('.tar'):
            tar_path += '.tar'
        try:
            # Decompress gzip to target tar_path
            with open(tar_path, 'wb') as out:
                res = subprocess.run(["gzip", "-d", "-c", lz4_for_decomp], stdout=out, stderr=subprocess.PIPE, text=False, timeout=300)
            if res.returncode != 0:
                raise RuntimeError(f"gzip decompression failed: {res.stderr.decode('utf-8', errors='ignore')}")
            _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl decompile: completed")
            _append_line(build_log_path, f"[INFO ] pxxl decompile: completed")
            _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "decompiling_completed", "stage": "decompiling", "status": "completed", "tar_path": tar_path, "command": "pxxl launch decompile"})
            if emit:
                emit({"task": "docker_localrun", "task_id": task_id, "stage": "decompiling", "status": "completed", "output": tar_path})
        except Exception as e:
            msg = f"decompression error: {e}"
            _append_line(error_log_path, msg)
            _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
            _append_line(build_log_path, f"[ERROR] {msg}")
            _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "decompiling_error", "stage": "decompiling", "status": "error", "error": str(e), "command": "pxxl launch decompile"})
            if emit:
                emit({"task": "docker_localrun", "task_id": task_id, "stage": "decompiling", "status": "error", "error": str(e)})
            raise
    else:
        tar_path = os.path.join(task_logs_dir, basename_for_ops.replace('.lz4', ''))
        if not tar_path.endswith('.tar'):
            tar_path += '.tar'
        try:
            # Try lz4 decompression
            res = subprocess.run(["lz4", "-d", "-f", lz4_for_decomp, tar_path], capture_output=True, text=True, timeout=300)
            if res.returncode != 0:
                raise RuntimeError(f"lz4 decompression failed: {res.stderr}")
            _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl decompile: completed")
            _append_line(build_log_path, f"[INFO ] pxxl decompile: completed")
            _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "decompiling_completed", "stage": "decompiling", "status": "completed", "tar_path": tar_path, "command": "pxxl launch decompile"})
            if emit:
                emit({"task": "docker_localrun", "task_id": task_id, "stage": "decompiling", "status": "completed", "output": tar_path})
        except Exception as e:
            msg = f"decompression error: {e}"
            _append_line(error_log_path, msg)
            _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
            _append_line(build_log_path, f"[ERROR] {msg}")
            _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "decompiling_error", "stage": "decompiling", "status": "error", "error": str(e), "command": "pxxl launch decompile"})
            if emit:
                emit({"task": "docker_localrun", "task_id": task_id, "stage": "decompiling", "status": "error", "error": str(e)})
            raise

    # Load image from tar with force reload to avoid Docker caching
    _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] loading image from tar")
    _append_line(build_log_path, f"[INFO ] loading image from tar path={tar_path}")
    _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "image_load_start", "tar_path": tar_path})
    
    # First, inspect the tar to get the image tag/name that will be loaded
    temp_image_tags = []
    try:
        with tarfile.open(tar_path, 'r') as tar:
            # Look for manifest.json to get image tags
            try:
                manifest_member = tar.getmember('manifest.json')
                manifest_data = tar.extractfile(manifest_member).read()
                manifest = json.loads(manifest_data.decode('utf-8'))
                for entry in manifest:
                    if 'RepoTags' in entry and entry['RepoTags']:
                        temp_image_tags.extend(entry['RepoTags'])
            except Exception:
                pass
    except Exception:
        pass
    
    # Remove existing images with the same tags to force fresh loading
    removed_cached_images = []
    for tag in temp_image_tags:
        try:
            existing_img = client.images.get(tag)
            client.images.remove(existing_img.id, force=True)
            removed_cached_images.append(tag)
            _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] removed cached image tag={tag}")
            _append_line(build_log_path, f"[INFO ] removed cached image tag={tag}")
        except Exception:
            # Image doesn't exist or couldn't be removed, which is fine
            pass
    
    if removed_cached_images:
        _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "cached_images_removed", "tags": removed_cached_images})
    
    # Now load the fresh image from tar
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
    _append_line(build_log_path, f"[INFO ] image loaded id={image_id or ''} tag={image_tag or ''}")
    _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "image_loaded", "image_id": image_id, "tag": image_tag, "force_reloaded": len(removed_cached_images) > 0})

    # Cleanup: delete compressed artifact (.lz4 or .gz) copied from SFTP; keep JSONs and Dockerfile
    try:
        removed = []
        if os.path.exists(dest_lz4) and (dest_lz4.endswith('.lz4') or dest_lz4.endswith('.gz')):
            os.remove(dest_lz4)
            removed.append(dest_lz4)
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] cleanup completed removed={removed}")
        _append_line(build_log_path, f"[INFO ] cleanup completed removed={removed}")
        _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "cleanup_compressed_files", "removed": removed})
    except Exception as ce:
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] cleanup warning {ce}")
        _append_line(build_log_path, f"[WARN ] cleanup warning {ce}")
        _append_json(build_structured_path, {"ts": _ts(), "level": "warn", "event": "cleanup_warning", "error": str(ce)})

    # Stage: running
    if emit:
        emit({"task": "docker_localrun", "task_id": task_id, "stage": "running", "status": "starting", "image_id": image_id, "tag": image_tag, "ts": _ts()})
    # Initialize container_name before any logging to avoid UnboundLocalError
    try:
        container_name = f"{app_id}-{task_id}" if app_id else (name or None)
    except Exception:
        container_name = name or None
    _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl run: starting container")
    _append_line(build_log_path, f"[INFO ] pxxl run: starting container image={(image_id or (image_tag or ''))} name={container_name} network=traefik-network")
    if command:
        try:
            _append_line(build_log_path, f"[INFO ] run command: {command}")
            _append_line(events_log_path, f"pxxl run command: {command}")
            _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "run_command", "command": command})
        except Exception:
            pass
    _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "run_start", "stage": "running", "status": "starting", "command": "pxxl launch run"})

    # Map cpu to nano_cpus if provided
    nano_cpus = None
    if cpu is not None:
        try:
            nano_cpus = int(float(cpu) * 1_000_000_000)
        except Exception:
            nano_cpus = None

    # container_name already initialized above for logging
    # Determine internal port key from requested ports; default to 80/tcp
    internal_port_key = None
    if ports and isinstance(ports, dict) and ports:
        try:
            internal_port_key = next(iter(ports.keys()))
        except Exception:
            internal_port_key = None
    internal_port_key = internal_port_key or "80/tcp"

    try:
        # Ensure traefik-network exists
        network_name = "traefik-network"
        try:
            existing = [n.get("name") for n in list_networks()]
            if network_name not in existing:
                create_network(network_name)
        except Exception:
            pass
        # Prepare optional persistent volume
        volumes_arg = None
        if volume_name and mount_path:
            try:
                client.volumes.get(volume_name)
            except Exception:
                try:
                    client.volumes.create(name=volume_name)
                except Exception:
                    pass
            volumes_arg = {volume_name: {"bind": mount_path, "mode": (mode or "rw")}}

        run_res = run_container_extended(
            image=image_id or (image_tag or ''),
            name=container_name,
            command=command,
            ports=None,  # No external port bindings - use Traefik labels for routing
            env=env,
            volumes=volumes_arg,
            mem_limit=memory,
            nano_cpus=nano_cpus,
            cpuset_cpus=cpuset,
            detach=True,
            network=network_name,
            labels=labels,
        )
        if emit:
            emit({"task": "docker_localrun", "task_id": task_id, "stage": "running", "status": "completed", "container_id": run_res.get("id"), "container_name": run_res.get("name"), "ts": _ts()})
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl run: completed")
        _append_line(build_log_path, f"[INFO ] pxxl run: completed")
        _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "run_completed", "stage": "running", "status": "completed", "command": "pxxl launch run"})
    except Exception as e:
        msg = f"run error: {e}"
        _append_line(error_log_path, msg)
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
        _append_line(build_log_path, f"[ERROR] {msg}")
        _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "run_error", "error": str(e)})
        if emit:
            emit({"task": "docker_localrun", "task_id": task_id, "stage": "running", "status": "error", "error": str(e), "ts": _ts()})
        raise

    # Immediately inspect container state and ports
    details = {}
    state = {}
    running_now = False
    inspected_host_port: Optional[int] = None
    try:
        cid = run_res.get("id")
        if cid:
            # Give a brief moment for container to transition
            time.sleep(0.5)
            details = client.api.inspect_container(cid) or {}
            state = details.get("State") or {}
            running_now = bool(state.get("Running"))
            # Extract internal port number from internal_port_key (e.g., "80/tcp" -> 80)
            try:
                internal_port_num = int(internal_port_key.split('/')[0]) if internal_port_key else 80
                inspected_host_port = internal_port_num  # Use internal port since no external binding
            except Exception:
                inspected_host_port = 80  # Default to port 80 for Traefik routing

            # Capture initial container logs regardless of running state
            try:
                c_obj = client.containers.get(cid)
                logs_tail = c_obj.logs(tail=200)
                logs_text = logs_tail.decode("utf-8", errors="ignore") if isinstance(logs_tail, (bytes, bytearray)) else str(logs_tail)
                if logs_text:
                    _append_line(build_log_path, f"[INFO ] container initial logs:\n{logs_text}")
                    _append_line(events_log_path, f"pxxl run: container initial logs:\n{logs_text}")
                    _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "container_logs_initial", "lines": logs_text.splitlines()[-200:]})
                    try:
                        with open(container_logs_path, "a") as f:
                            f.write(logs_text + "\n")
                    except Exception:
                        pass
            except Exception:
                pass
            # If container exited quickly, try to capture last logs for debugging
            if not running_now:
                try:
                    c_obj = client.containers.get(cid)
                    logs_tail = c_obj.logs(tail=50)
                    logs_text = logs_tail.decode("utf-8", errors="ignore") if isinstance(logs_tail, (bytes, bytearray)) else str(logs_tail)
                    _append_line(build_log_path, f"[WARN ] container not running; recent logs:\n{logs_text}")
                    _append_json(build_structured_path, {"ts": _ts(), "level": "warn", "event": "container_logs_tail", "lines": logs_text.splitlines()[-50:]})
                except Exception:
                    pass
    except Exception:
        details = {}
        state = {}
        running_now = False

    # Update summary but keep status as 'building'
    try:
        if os.path.exists(summary_path):
            with open(summary_path, "r") as f:
                summary = json.load(f)
        else:
            summary = {}
        # Healthcheck planning
        start_ts = time.time()
        end_ts = start_ts + 15
        summary.update({
            "status": "running",
            "stage": "running",
            "container_id": run_res.get("id"),
            "container_name": run_res.get("name"),
            "host_port": inspected_host_port,
            "healthcheck": False,
            "healthchecksstatus": "pending",
            "isChecking": True,
            "healthcheck_time": 15,
            "start_check_time": time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(start_ts)),
            "end_check_time": time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(end_ts)),
            "localrun": {
                "lz4_path": lz4_for_decomp,
                "compressionapthname": compressionapthname,
                "tar_path": tar_path,
                "image_id": image_id,
                "image_tag": image_tag,
                "container": run_res,
                "resources": {
                    "cpu": cpu,
                    "cpuset": cpuset,
                    "memory": memory,
                },
                "app_id": app_id,
                "generated_name": container_name,
                "ports_requested": ports,
                "env": env,
                "command": command,
                "internal_port_key": internal_port_key,
                "network": "traefik-network",
                "labels": labels,
                "inspect": {
                    "running": running_now,
                    "state": state,
                    "host_port": inspected_host_port,
                }
            }
        })
        _write_json(summary_path, summary)
    except Exception:
        pass

    # Spawn background healthcheck after 15 seconds
    def _do_healthcheck():
        try:
            # Sleep until end time
            time.sleep(15)
            try:
                details = inspect_container_details(run_res.get("id"))
                state = details.get("state") or {}
                running = bool(state.get("Running"))
                status_str = state.get("Status") or ("running" if running else "exited")
            except Exception:
                running = False
                status_str = "error"
            try:
                with open(summary_path, "r") as f:
                    s = json.load(f)
            except Exception:
                s = {}
            s.update({
                "healthcheck": running,
                "healthchecksstatus": status_str,
                "isChecking": False,
                "status": "completed" if running else "error",
            })
            _write_json(summary_path, s)
            _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl healthcheck: completed running={running} status={status_str}")
            _append_line(build_log_path, f"[INFO ] pxxl healthcheck: completed running={running} status={status_str}")
            _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "healthcheck_completed", "stage": "running", "status": "healthcheck", "running": running, "healthchecksstatus": status_str, "command": "pxxl launch healthcheck"})
            if emit:
                emit({"task": "docker_localrun", "task_id": task_id, "stage": "running", "status": "healthcheck", "healthcheck": running, "healthchecksstatus": status_str})
        except Exception as e:
            _append_line(build_log_path, f"[ERROR] pxxl healthcheck: error {e}")
            _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "healthcheck_error", "stage": "running", "status": "error", "error": str(e), "command": "pxxl launch healthcheck"})
            try:
                with open(summary_path, "r") as f:
                    s = json.load(f)
            except Exception:
                s = {}
            s.update({
                "healthcheck": False,
                "healthchecksstatus": "error",
                "isChecking": False,
                "status": "failed",
            })
            _write_json(summary_path, s)
    threading.Thread(target=_do_healthcheck, daemon=True).start()

    return {
        "status": "ok",
        "task_id": task_id,
        "image_id": image_id,
        "tag": image_tag,
        "container": run_res,
        "container_id": run_res.get("id"),
        "container_name": run_res.get("name"),
        "app_id": app_id,
        "generated_name": container_name,
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


def ls_in_container(id_or_name: str, path: str = "/") -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    res = c.exec_run(f"ls -la {path}")
    try:
        output = res.output.decode("utf-8", errors="ignore")
        exit_code = res.exit_code
    except Exception:
        exit_code = res[0] if isinstance(res, tuple) else None
        output = res[1].decode("utf-8", errors="ignore") if isinstance(res, tuple) else ""
    return {"path": path, "exit_code": exit_code, "output": output}


def du_in_container(id_or_name: str, path: str = "/") -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    # Suppress permission errors and force success exit code
    res = c.exec_run(f"sh -c 'du -sk {path} 2>/dev/null || true'")
    try:
        output = res.output.decode("utf-8", errors="ignore")
        exit_code = res.exit_code
    except Exception:
        exit_code = res[0] if isinstance(res, tuple) else None
        output = res[1].decode("utf-8", errors="ignore") if isinstance(res, tuple) else ""
    size_kb = None
    try:
        lines = [ln for ln in output.strip().splitlines() if ln.strip()]
        for ln in reversed(lines):
            parts = ln.split()
            if parts:
                try:
                    size_kb = int(parts[0])
                    break
                except Exception:
                    continue
    except Exception:
        size_kb = None
    return {"path": path, "exit_code": exit_code, "size_kb": size_kb, "output": output}


def ls_detailed_in_container(id_or_name: str, path: str = "/", include_sizes: bool = True) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    # List entries (excluding . and ..)
    res = c.exec_run(f"sh -c 'ls -A1 {path} 2>/dev/null || true'")
    try:
        names_output = res.output.decode("utf-8", errors="ignore")
    except Exception:
        names_output = res[1].decode("utf-8", errors="ignore") if isinstance(res, tuple) else ""
    names = [n for n in names_output.splitlines() if n.strip()]
    entries = []
    for name in names:
        full_path = f"{path.rstrip('/')}/{name}"
        # stat provides type, size, owner/group, perms, mtime
        sres = c.exec_run(f"sh -c 'stat -c \"%n|%F|%s|%U|%G|%a|%y\" {full_path} 2>/dev/null || true'")
        try:
            sout = sres.output.decode("utf-8", errors="ignore").strip()
        except Exception:
            sout = sres[1].decode("utf-8", errors="ignore").strip() if isinstance(sres, tuple) else ""
        parts = sout.split("|") if sout else []
        entry = {
            "name": name,
            "path": full_path,
            "type": parts[1] if len(parts) > 1 else None,
            "size_bytes": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None,
            "owner": parts[3] if len(parts) > 3 else None,
            "group": parts[4] if len(parts) > 4 else None,
            "perms": parts[5] if len(parts) > 5 else None,
            "mtime": parts[6] if len(parts) > 6 else None,
        }
        if include_sizes:
            dres = c.exec_run(f"sh -c 'du -sk {full_path} 2>/dev/null || true'")
            try:
                dout = dres.output.decode("utf-8", errors="ignore")
            except Exception:
                dout = dres[1].decode("utf-8", errors="ignore") if isinstance(dres, tuple) else ""
            try:
                dline = [ln for ln in dout.splitlines() if ln.strip()][-1]
                entry["size_kb"] = int(dline.split()[0])
            except Exception:
                entry["size_kb"] = None
        entries.append(entry)
    return {"path": path, "entries": entries, "count": len(entries)}


def inspect_container_details(id_or_name: str) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    # Some docker SDK versions do not support 'size' kwarg
    info = client.api.inspect_container(c.id)
    mounts = info.get("Mounts") or []
    # Compute root fs size via du as fallback
    computed_root = None
    try:
        du_res = du_in_container(c.id, "/")
        if isinstance(du_res, dict):
            computed_root = du_res.get("size_kb")
    except Exception:
        computed_root = None
    computed_paths = []
    try:
        for m in mounts:
            dest = m.get("Destination")
            if not dest:
                continue
            try:
                du_p = du_in_container(c.id, dest)
                computed_paths.append({"path": dest, "size_kb": du_p.get("size_kb")})
            except Exception:
                computed_paths.append({"path": dest, "size_kb": None})
    except Exception:
        pass
    return {
        "id": c.id,
        "name": c.name,
        "image": c.image.tags,
        "state": info.get("State"),
        "created": info.get("Created"),
        "size_rw": info.get("SizeRw"),
        "size_root_fs": info.get("SizeRootFs"),
        "mounts": mounts,
        "config": info.get("Config"),
        "network_settings": info.get("NetworkSettings"),
        "computed": {"root_size_kb": computed_root, "paths": computed_paths},
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


def check_volume_attached_to_running_containers(volume_name: str) -> Dict[str, Any]:
    """Check if a volume is currently attached to any running containers."""
    client = get_client()
    attached_containers = []
    
    try:
        # Get all running containers
        running_containers = client.containers.list(filters={"status": "running"})
        
        for container in running_containers:
            volumes = _extract_volumes_from_container(container)
            if volume_name in volumes:
                attached_containers.append({
                    "id": container.id,
                    "name": container.name,
                    "mount_path": volumes[volume_name].get("bind"),
                    "mode": volumes[volume_name].get("mode")
                })
    except Exception as e:
        return {
            "volume_name": volume_name,
            "attached": False,
            "containers": [],
            "error": str(e)
        }
    
    return {
        "volume_name": volume_name,
        "attached": len(attached_containers) > 0,
        "containers": attached_containers,
        "count": len(attached_containers)
    }


def detach_volume_only(id_or_name: str, volume_name: str) -> Dict[str, Any]:
    """
    Attempt to detach a volume from a container without recreating the container.
    
    Note: Docker API does not support detaching volumes from running containers.
    This function provides information about the limitation and alternative approaches.
    """
    client = get_client()
    
    try:
        container = client.containers.get(id_or_name)
        container_running = container.status == "running"
        
        # Check if the volume is actually attached
        volumes = _extract_volumes_from_container(container)
        volume_attached = volume_name in volumes
        
        if not volume_attached:
            return {
                "status": "not_attached",
                "message": f"Volume '{volume_name}' is not attached to container '{id_or_name}'",
                "container_running": container_running,
                "volume_name": volume_name,
                "container_id": container.id,
                "container_name": container.name
            }
        
        # Docker limitation explanation
        if container_running:
            return {
                "status": "docker_limitation",
                "message": "Docker API does not support detaching volumes from running containers without stopping them",
                "explanation": "To detach a volume, the container must be stopped, the volume configuration modified, and the container restarted",
                "alternatives": [
                    "Use the existing /docker/container/volume/remove endpoint to recreate the container without the volume",
                    "Stop the container manually, then use Docker CLI to modify volume mounts",
                    "Create a new container without the volume and migrate data if needed"
                ],
                "container_running": True,
                "volume_name": volume_name,
                "container_id": container.id,
                "container_name": container.name,
                "mount_info": volumes[volume_name]
            }
        else:
            # Container is stopped, we could theoretically modify it, but Docker still doesn't support this directly
            return {
                "status": "container_stopped",
                "message": "Container is stopped. Volume detachment would require container recreation",
                "explanation": "Even with stopped containers, Docker requires recreation to modify volume mounts",
                "recommendation": "Use the /docker/container/volume/remove endpoint to recreate without the volume",
                "container_running": False,
                "volume_name": volume_name,
                "container_id": container.id,
                "container_name": container.name,
                "mount_info": volumes[volume_name]
            }
            
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to check container: {str(e)}",
            "volume_name": volume_name,
            "container_id_or_name": id_or_name
        }


def delete_volume_with_attachment_check(volume_name: str, force: bool = False) -> Dict[str, Any]:
    """
    Delete a Docker volume with proper checking for attached containers.
    
    This function checks if the volume is attached to any running containers
    and provides detailed information about the deletion process.
    """
    client = get_client()
    
    try:
        # First, check if the volume exists
        try:
            volume = client.volumes.get(volume_name)
        except Exception:
            return {
                "status": "not_found",
                "message": f"Volume '{volume_name}' does not exist",
                "volume_name": volume_name
            }
        
        # Check if volume is attached to any running containers
        attachment_info = check_volume_attached_to_running_containers(volume_name)
        
        if attachment_info["attached"]:
            if not force:
                return {
                    "status": "attached_to_running_containers",
                    "message": f"Volume '{volume_name}' is attached to {attachment_info['count']} running container(s)",
                    "volume_name": volume_name,
                    "attached_containers": attachment_info["containers"],
                    "recommendation": "Stop the containers first, detach the volume, or use force=true to attempt forced deletion",
                    "force_warning": "Using force=true may cause data loss and container instability"
                }
            else:
                # Force deletion - warn about potential issues
                try:
                    volume.remove(force=True)
                    return {
                        "status": "force_deleted",
                        "message": f"Volume '{volume_name}' was forcefully deleted despite being attached to running containers",
                        "volume_name": volume_name,
                        "warning": "This may have caused data loss or container instability",
                        "previously_attached_containers": attachment_info["containers"]
                    }
                except Exception as e:
                    return {
                        "status": "force_delete_failed",
                        "message": f"Failed to force delete volume '{volume_name}': {str(e)}",
                        "volume_name": volume_name,
                        "attached_containers": attachment_info["containers"]
                    }
        else:
            # Volume is not attached to running containers, safe to delete
            try:
                volume.remove(force=force)
                return {
                    "status": "deleted",
                    "message": f"Volume '{volume_name}' was successfully deleted",
                    "volume_name": volume_name
                }
            except Exception as e:
                return {
                    "status": "delete_failed",
                    "message": f"Failed to delete volume '{volume_name}': {str(e)}",
                    "volume_name": volume_name
                }
                
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error during volume deletion check: {str(e)}",
            "volume_name": volume_name
        }


def clear_volume_contents(volume_name: str) -> Dict[str, Any]:
    """
    Clear all files from a named Docker volume without stopping containers.

    This uses a short-lived helper container to mount the volume at /mnt and
    remove all contents inside it, including hidden files. Containers that are
    currently using the volume remain running; however, applications may error
    if they depend on the files that are being removed.
    """
    client = get_client()

    # Ensure volume exists
    try:
        volume = client.volumes.get(volume_name)
    except Exception:
        return {
            "status": "not_found",
            "message": f"Volume '{volume_name}' does not exist",
            "volume_name": volume_name,
        }

    # Optional: provide info about running containers using this volume
    attachment_info = {}
    try:
        attachment_info = check_volume_attached_to_running_containers(volume_name)
    except Exception as e:
        attachment_info = {"error": str(e)}

    # Use an ephemeral Alpine container to clear the volume contents
    # The find-based approach reliably handles hidden files and nested dirs.
    try:
        cmd = "sh -c 'find /mnt -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'"
        client.containers.run(
            image="alpine:3.19",
            command=cmd,
            remove=True,
            volumes={volume_name: {"bind": "/mnt", "mode": "rw"}},
            tty=False,
        )

        # Verify emptiness by counting remaining entries
        verify_cmd = "sh -c 'ls -A /mnt | wc -l'"
        out = client.containers.run(
            image="alpine:3.19",
            command=verify_cmd,
            remove=True,
            volumes={volume_name: {"bind": "/mnt", "mode": "rw"}},
            tty=False,
        )
        try:
            remaining_count = int((out.decode("utf-8") if isinstance(out, (bytes, bytearray)) else str(out)).strip())
        except Exception:
            remaining_count = None

        return {
            "status": "cleared",
            "message": f"Volume '{volume_name}' contents cleared",
            "volume_name": volume_name,
            "remaining_count": remaining_count,
            "attachments": attachment_info,
        }
    except Exception as e:
        return {
            "status": "clear_failed",
            "message": f"Failed to clear volume '{volume_name}': {str(e)}",
            "volume_name": volume_name,
            "attachments": attachment_info,
        }


def recreate_with_added_volume(id_or_name: str, volume_name: str, mount_path: str, mode: str = "rw") -> dict:
    client = get_client()
    container = client.containers.get(id_or_name)
    # Ensure volume exists
    try:
        client.volumes.get(volume_name)
    except Exception:
        client.volumes.create(name=volume_name)
    # Extract existing mounts and env
    volumes = _extract_volumes_from_container(container)
    volumes[volume_name] = {"bind": mount_path, "mode": mode}
    env = _extract_env_from_container(container)
    cfg = container.attrs.get("Config", {}) or {}
    
    # Better command extraction - preserve original command structure
    cmd_list = cfg.get("Cmd")
    command = None
    if isinstance(cmd_list, list) and cmd_list:
        # If it's a single command, pass it as string; if multiple parts, join them
        if len(cmd_list) == 1:
            command = cmd_list[0]
        else:
            command = " ".join(cmd_list)
    elif isinstance(cmd_list, str) and cmd_list.strip():
        command = cmd_list
    # If no explicit command, let Docker use the image's default CMD/ENTRYPOINT
    
    image = (container.image.tags[0] if container.image.tags else container.image.id)
    name = container.name
    network_mode = (container.attrs.get("HostConfig", {}) or {}).get("NetworkMode")
    
    # Extract additional container configuration to preserve behavior
    host_config = container.attrs.get("HostConfig", {}) or {}
    port_bindings = host_config.get("PortBindings", {}) or {}
    
    # Convert port bindings to the format expected by run_container_extended
    ports = {}
    for container_port, host_bindings in port_bindings.items():
        if host_bindings and len(host_bindings) > 0:
            host_port = host_bindings[0].get("HostPort")
            if host_port:
                ports[container_port] = int(host_port)
    
    # Stop and remove old container
    try:
        container.stop()
    except Exception:
        pass
    try:
        container.remove(force=True)
    except Exception:
        pass
    
    # Run new container with same name and added volume
    run_res = run_container_extended(
        image=image,
        name=name,
        command=command,
        env=env,
        volumes=volumes,
        network=network_mode,
        ports=ports if ports else None,
        detach=True,
    )
    
    # Verify the container is running
    new_container_id = run_res.get("id")
    container_running = False
    container_state = {}
    
    if new_container_id:
        try:
            # Give the container a moment to start
            time.sleep(1)
            new_container = client.containers.get(new_container_id)
            new_container.reload()
            container_state = client.api.inspect_container(new_container_id).get("State", {})
            container_running = bool(container_state.get("Running"))
            
            # If not running, try to get logs for debugging
            if not container_running:
                try:
                    logs = new_container.logs(tail=50).decode("utf-8", errors="ignore")
                    container_state["logs"] = logs
                except Exception:
                    pass
        except Exception as e:
            container_state["error"] = str(e)
    
    return {
        "status": "recreated", 
        "id": run_res.get("id"), 
        "name": name, 
        "volumes": volumes,
        "running": container_running,
        "container_state": container_state,
        "command_used": command,
        "ports_preserved": ports
    }

# Persistent volume size limit helpers (logical limits stored per task)

def _volume_limits_path(task_id: str) -> str:
    base_dir = os.path.join(os.path.dirname(__file__), "builds", task_id)
    try:
        os.makedirs(base_dir, exist_ok=True)
    except Exception:
        pass
    return os.path.join(base_dir, "volume_limits.json")


def load_volume_limits(task_id: str) -> Dict:
    path = _volume_limits_path(task_id)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return {"limits": {}}
    return {"limits": {}}


def save_volume_limits(task_id: str, data: Dict) -> None:
    path = _volume_limits_path(task_id)
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def set_volume_limit(task_id: str, volume_name: str, mount_path: str, limit_kb: int) -> Dict:
    data = load_volume_limits(task_id)
    if "limits" not in data or not isinstance(data["limits"], dict):
        data["limits"] = {}
    data["limits"][volume_name] = {"mount_path": mount_path, "limit_kb": int(limit_kb)}
    save_volume_limits(task_id, data)
    return {"task_id": task_id, "volume_name": volume_name, "mount_path": mount_path, "limit_kb": int(limit_kb)}


def update_volume_limit(task_id: str, volume_name: str, limit_kb: int, mount_path: Optional[str] = None) -> Dict:
    data = load_volume_limits(task_id)
    entry = data.get("limits", {}).get(volume_name) or {}
    if mount_path is None:
        mount_path = entry.get("mount_path", "")
    data.setdefault("limits", {})[volume_name] = {"mount_path": mount_path, "limit_kb": int(limit_kb)}
    save_volume_limits(task_id, data)
    return {"task_id": task_id, "volume_name": volume_name, "mount_path": mount_path, "limit_kb": int(limit_kb)}


def remove_volume_limit(task_id: str, volume_name: str) -> Dict:
    data = load_volume_limits(task_id)
    if isinstance(data.get("limits"), dict) and volume_name in data["limits"]:
        del data["limits"][volume_name]
        save_volume_limits(task_id, data)
        removed = True
    else:
        removed = False
    return {"task_id": task_id, "volume_name": volume_name, "removed": removed}


def check_volume_limit_status(task_id: str, id_or_name: str, volume_name: str) -> Dict:
    data = load_volume_limits(task_id)
    entry = data.get("limits", {}).get(volume_name)
    if not entry:
        return {"task_id": task_id, "volume_name": volume_name, "status": "no_limit", "usage_kb": None, "limit_kb": None}

    mount_path = entry.get("mount_path")
    limit_kb = int(entry.get("limit_kb", 0))

    # Inspect container state; avoid exec if container not running
    client = get_client()
    state = {}
    running = False
    try:
        c = client.containers.get(id_or_name)
        state = client.api.inspect_container(c.id).get("State") or {}
        running = bool(state.get("Running"))
    except Exception:
        state = {}
        running = False

    # If the container was just recreated, it may need a moment to run
    if not running:
        for _ in range(3):
            try:
                c = client.containers.get(id_or_name)
                state = client.api.inspect_container(c.id).get("State") or {}
                running = bool(state.get("Running"))
            except Exception:
                running = False
            if running:
                break
            time.sleep(0.5)

    du_res = None
    if running:
        du_res = du_in_container(id_or_name, mount_path)
    else:
        du_res = {"path": mount_path, "exit_code": None, "size_kb": None, "output": ""}

    # Normalize usage value
    size_val = None
    exit_code = None
    if isinstance(du_res, dict):
        size_val = du_res.get("size_kb")
        exit_code = du_res.get("exit_code")
    else:
        try:
            size_val = int(du_res)
        except Exception:
            size_val = None

    if not running:
        status = "container_not_running"
    elif size_val is None:
        status = "error" if (exit_code is not None and exit_code != 0) else "unknown"
    else:
        status = "under" if limit_kb > 0 and size_val <= limit_kb else ("over" if limit_kb > 0 else "no_limit")

    return {
        "task_id": task_id,
        "volume_name": volume_name,
        "mount_path": mount_path,
        "usage_kb": du_res,
        "limit_kb": limit_kb,
        "status": status,
        "container_state": state,
    }


def recreate_without_volume(id_or_name: str, volume_name: str) -> dict:
    client = get_client()
    container = client.containers.get(id_or_name)
    volumes = _extract_volumes_from_container(container)
    # Remove the volume by name if present
    if volume_name in volumes:
        del volumes[volume_name]
    env = _extract_env_from_container(container)
    cfg = container.attrs.get("Config", {}) or {}
    
    # Better command extraction - preserve original command structure
    cmd_list = cfg.get("Cmd")
    command = None
    if isinstance(cmd_list, list) and cmd_list:
        # If it's a single command, pass it as string; if multiple parts, join them
        if len(cmd_list) == 1:
            command = cmd_list[0]
        else:
            command = " ".join(cmd_list)
    elif isinstance(cmd_list, str) and cmd_list.strip():
        command = cmd_list
    # If no explicit command, let Docker use the image's default CMD/ENTRYPOINT
    
    image = (container.image.tags[0] if container.image.tags else container.image.id)
    name = container.name
    network_mode = (container.attrs.get("HostConfig", {}) or {}).get("NetworkMode")
    
    # Extract additional container configuration to preserve behavior
    host_config = container.attrs.get("HostConfig", {}) or {}
    ports_config = cfg.get("ExposedPorts", {}) or {}
    port_bindings = host_config.get("PortBindings", {}) or {}
    
    # Convert port bindings to the format expected by run_container_extended
    ports = {}
    for container_port, host_bindings in port_bindings.items():
        if host_bindings and len(host_bindings) > 0:
            host_port = host_bindings[0].get("HostPort")
            if host_port:
                ports[container_port] = int(host_port)
    
    try:
        container.stop()
    except Exception:
        pass
    try:
        container.remove(force=True)
    except Exception:
        pass
    
    run_res = run_container_extended(
        image=image,
        name=name,
        command=command,
        env=env,
        volumes=volumes,
        network=network_mode,
        ports=ports if ports else None,
        detach=True,
    )
    
    # Verify the container is running
    new_container_id = run_res.get("id")
    container_running = False
    container_state = {}
    
    if new_container_id:
        try:
            # Give the container a moment to start
            time.sleep(1)
            new_container = client.containers.get(new_container_id)
            new_container.reload()
            container_state = client.api.inspect_container(new_container_id).get("State", {})
            container_running = bool(container_state.get("Running"))
            
            # If not running, try to get logs for debugging
            if not container_running:
                try:
                    logs = new_container.logs(tail=50).decode("utf-8", errors="ignore")
                    container_state["logs"] = logs
                except Exception:
                    pass
        except Exception as e:
            container_state["error"] = str(e)
    
    return {
        "status": "recreated", 
        "id": run_res.get("id"), 
        "name": name, 
        "volumes": volumes,
        "running": container_running,
        "container_state": container_state,
        "command_used": command,
        "ports_preserved": ports
    }


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
    Deprecated: SFTP transfer retained for backward compatibility. Use HTTP upload.
    """
    def _ts() -> str:
        return time.strftime('%Y-%m-%dT%H:%M:%S')
    
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
                "ts": _ts(),
            })
    
    try:
        _emit_log("Starting SFTP transfer to production server")
        
        # Create SSH client
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Connect to SFTP server
        _emit_log(f"Connecting to SFTP server {sftp_host}:{sftp_port}")
        _conn_kwargs = {
            "hostname": sftp_host,
            "port": int(sftp_port or 22),
            "username": sftp_username,
            "timeout": 30,
        }
        if sftp_password:
            _conn_kwargs["password"] = sftp_password
        else:
            _emit_log("SFTP using key/agent/no-password auth")
        ssh.connect(**_conn_kwargs)
        
        _emit_log("SFTP SSH connection established")
        
        
        # Create SFTP client
        sftp = ssh.open_sftp()
        _emit_log("SFTP client opened")
        
        # Create remote directory path
        
        remote_base_dir = "/upload"
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
        
        # Transfer all files and directories recursively.     
        # Pre-count files to upload for visibility
        planned_file_count = 0
        planned_total_size = 0
        try:
            for root, dirs, files in os.walk(build_dir):
                for file in files:
                    planned_file_count += 1
                    fp = os.path.join(root, file)
                    try:
                        planned_total_size += os.path.getsize(fp)
                    except Exception:
                        pass
        except Exception:
            pass
        _emit_log(f"Transferring build directory from {build_dir} to {remote_dir}")
        _emit_log(f"Upload starting: {planned_file_count} files, {planned_total_size} bytes")
        
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
        _emit_log("Closing SFTP connection")
        
        # Close connections
        sftp.close()
        ssh.close()
        
        return {
            "status": "success",
            "remote_path": remote_dir,
            "files_transferred": file_count,
            "total_size_bytes": total_size,
        }
        
    except Exception as e:
        error_msg = f"SFTP transfer failed: {str(e)}"
        _emit_log(error_msg, "error")
        return {
            "status": "error",
            "error": error_msg,
        }

def transfer_build_to_http(build_dir: str, task_id: str, upload_url: str, emit: Optional[Callable[[Dict[str, Any]], None]] = None, app_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Compress the folder contents to a tar.lz4 and POST to {upload_url}/upload
    with multipart form fields: foldername=task_id, file=@<tar.lz4>.
    """
    def _ts() -> str:
        return time.strftime('%Y-%m-%dT%H:%M:%S')

    def _emit_log(message: str, level: str = "info"):
        if emit:
            emit({
                "task": "http_upload",
                "task_id": task_id,
                "stage": "uploading",
                "status": "progress",
                "message": message,
                "level": level,
                "app_id": app_id,
                "ts": _ts(),
            })

    try:
        import tarfile
        import tempfile
        import requests
        import shutil

        _emit_log("Starting HTTP upload to upload server")

        # Create tar of folder contents (exclude the root folder name)
        tmp_dir = tempfile.mkdtemp(prefix=f"pxxl-{task_id}-")
        tar_path = os.path.join(tmp_dir, f"{task_id}.tar")
        with tarfile.open(tar_path, mode="w") as tf:
            for item in os.listdir(build_dir):
                full_path = os.path.join(build_dir, item)
                # Add each top-level item under its own name
                tf.add(full_path, arcname=item)

        # Compress to .tar.lz4
        lz4_path = f"{tar_path}.lz4"
        try:
            result = subprocess.run(["lz4", "-z", "-f", tar_path, lz4_path], capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise Exception(result.stderr or "lz4 compression failed")
        except Exception as lz4_error:
            # Fallback to gzip if lz4 is unavailable
            import gzip
            lz4_path = f"{tar_path}.gz"
            with open(tar_path, "rb") as f_in:
                with gzip.open(lz4_path, "wb") as f_out:
                    f_out.writelines(f_in)
            _emit_log(f"lz4 unavailable/failed, used gzip: {lz4_error}")

        # Move final archive into build_dir with a stable name
        target_name = "upload.tar.lz4" if lz4_path.endswith(".lz4") else "upload.tar.gz"
        target_archive_path = os.path.join(build_dir, target_name)
        try:
            # Ensure we don't overwrite an existing file silently
            if os.path.exists(target_archive_path):
                try:
                    os.remove(target_archive_path)
                except Exception:
                    pass
            shutil.move(lz4_path, target_archive_path)
        except Exception:
            # If move fails, fallback to using the temp path
            target_archive_path = lz4_path

        size_bytes = os.path.getsize(target_archive_path)

        # Normalize upload endpoint
        final_url = (upload_url or "").rstrip("/") + "/upload"

        # Prepare replicable curl command for visibility
        curl_cmd = (
            f"curl -X POST {final_url} "
            f"-F \"foldername={task_id}\" "
            f"-F \"file=@{target_archive_path}\""
        )

        _emit_log(f"pxxl transfer: upload url: {final_url}")
        _emit_log(f"pxxl transfer: curl: {curl_cmd}")
        # Also print to console for IDE visibility
        print(f"pxxl transfer: upload url {final_url}")
        print(f"pxxl transfer: curl {curl_cmd}")
        _emit_log(f"Uploading archive ({size_bytes} bytes) to {final_url}")

        with open(target_archive_path, "rb") as f:
            files = {"file": (os.path.basename(lz4_path), f)}
            data = {"foldername": task_id}
            resp = requests.post(final_url, files=files, data=data, timeout=60)

        try:
            payload = resp.json()
        except Exception:
            payload = {"status": "error", "error": f"Non-JSON response: {resp.text[:200]}"}

        if resp.status_code == 200 and str(payload.get("status")).lower() == "success":
            _emit_log("HTTP upload completed successfully")
            return {
                "status": "success",
                "foldername": payload.get("folder") or task_id,
                "files_transferred": 1,
                "total_size_bytes": size_bytes,
                "final_url": final_url,
                "archive_path": target_archive_path,
                "curl": curl_cmd,
            }
        else:
            err = payload.get("error") or f"HTTP {resp.status_code}"
            _emit_log(f"HTTP upload failed: {err}", level="error")
            return {
                "status": "error",
                "error": err,
                "final_url": final_url,
                "archive_path": target_archive_path,
                "curl": curl_cmd,
            }
    except Exception as e:
        msg = f"HTTP upload encountered an exception: {str(e)}"
        _emit_log(msg, level="error")
        return {"status": "error", "error": msg}

def transfer_build_to_rsync_ssh(build_dir: str, task_id: str, ssh_target: str, emit: Optional[Callable[[Dict[str, Any]], None]] = None, app_id: Optional[str] = None) -> Dict[str, Any]:
    def _ts() -> str:
        return time.strftime('%Y-%m-%dT%H:%M:%S')

    def _emit_log(message: str, level: str = "info"):
        if emit:
            emit({
                "task": "ssh_upload",
                "task_id": task_id,
                "stage": "uploading",
                "status": "progress",
                "message": message,
                "level": level,
                "app_id": app_id,
                "ts": _ts(),
            })

    try:
        import tarfile
        import tempfile
        import shutil
        import shutil as _shutil
        import shlex
        import os

        _emit_log("Starting SSH rsync upload to remote server")

        tmp_dir = tempfile.mkdtemp(prefix=f"pxxl-{task_id}-")
        tar_path = os.path.join(tmp_dir, f"{task_id}.tar")
        with tarfile.open(tar_path, mode="w") as tf:
            for item in os.listdir(build_dir):
                full_path = os.path.join(build_dir, item)
                tf.add(full_path, arcname=item)

        lz4_path = f"{tar_path}.lz4"
        try:
            result = subprocess.run(["lz4", "-z", "-f", tar_path, lz4_path], capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise Exception(result.stderr or "lz4 compression failed")
        except Exception as lz4_error:
            import gzip
            lz4_path = f"{tar_path}.gz"
            with open(tar_path, "rb") as f_in:
                with gzip.open(lz4_path, "wb") as f_out:
                    f_out.writelines(f_in)
            _emit_log(f"lz4 unavailable/failed, used gzip: {lz4_error}")

        target_name = "upload.tar.lz4" if lz4_path.endswith(".lz4") else "upload.tar.gz"
        target_archive_path = os.path.join(build_dir, target_name)
        try:
            if os.path.exists(target_archive_path):
                try:
                    os.remove(target_archive_path)
                except Exception:
                    pass
            shutil.move(lz4_path, target_archive_path)
        except Exception:
            target_archive_path = lz4_path

        size_bytes = os.path.getsize(target_archive_path)

        remote_dir = f"/pxxl/uploads/{task_id}"
        identity_path = "/root/.ssh/id_ed25519"
        ssh_opts_list = []
        try:
            if os.path.exists(identity_path):
                ssh_opts_list.append(f"-i {shlex.quote(identity_path)}")
                ssh_opts_list.append("-o IdentitiesOnly=yes")
        except Exception:
            pass
        ssh_opts_list.append("-o StrictHostKeyChecking=no")
        ssh_opts = " ".join(ssh_opts_list)
        mkdir_cmd = f"ssh {ssh_opts} {ssh_target} 'mkdir -p {shlex.quote(remote_dir)}'"
        rsync_cmd = f"rsync -avz -e 'ssh {ssh_opts}' {shlex.quote(target_archive_path)} {ssh_target}:'{remote_dir}/'"

        _emit_log(f"pxxl transfer: upload url: {ssh_target}:{remote_dir}")
        _emit_log(f"pxxl transfer: rsync: {rsync_cmd}")
        print(f"pxxl transfer: upload url {ssh_target}:{remote_dir}")
        print(f"pxxl transfer: rsync {rsync_cmd}")
        _emit_log(f"Uploading archive ({size_bytes} bytes) to {ssh_target}:{remote_dir}")

        mk = subprocess.run(mkdir_cmd, shell=True, capture_output=True, text=True, timeout=60)
        if mk.returncode != 0:
            err = (mk.stderr or "") + ("\n" + mk.stdout if mk.stdout else "")
            err = err.strip() or "mkdir failed"
            _emit_log(f"SSH mkdir failed: {err}", level="error")
            return {
                "status": "error",
                "error": err,
                "final_url": f"{ssh_target}:{remote_dir}",
                "archive_path": target_archive_path,
                "rsync": rsync_cmd,
            }

        rsync_bin = _shutil.which("rsync")
        if not rsync_bin:
            scp_cmd = f"scp {ssh_opts} {shlex.quote(target_archive_path)} {ssh_target}:'{remote_dir}/'"
            _emit_log("rsync not found; attempting scp fallback")
            _emit_log(f"pxxl transfer: scp: {scp_cmd}")
            print(f"pxxl transfer: scp {scp_cmd}")
            scp = subprocess.run(scp_cmd, shell=True, capture_output=True, text=True, timeout=300)
            if scp.returncode == 0:
                _emit_log("SSH scp upload completed successfully")
                return {
                    "status": "success",
                    "foldername": task_id,
                    "files_transferred": 1,
                    "total_size_bytes": size_bytes,
                    "final_url": f"{ssh_target}:{remote_dir}",
                    "archive_path": target_archive_path,
                    "scp": scp_cmd,
                }
            else:
                err = (scp.stderr or "") + ("\n" + scp.stdout if scp.stdout else "")
                err = err.strip() or "scp failed"
                _emit_log(f"SSH scp upload failed: {err}", level="error")
                return {
                    "status": "error",
                    "error": err,
                    "final_url": f"{ssh_target}:{remote_dir}",
                    "archive_path": target_archive_path,
                    "scp": scp_cmd,
                }

        rs = subprocess.run(rsync_cmd, shell=True, capture_output=True, text=True, timeout=300)
        if rs.returncode == 0:
            _emit_log("SSH rsync upload completed successfully")
            return {
                "status": "success",
                "foldername": task_id,
                "files_transferred": 1,
                "total_size_bytes": size_bytes,
                "final_url": f"{ssh_target}:{remote_dir}",
                "archive_path": target_archive_path,
                "rsync": rsync_cmd,
            }
        else:
            err = (rs.stderr or "") + ("\n" + rs.stdout if rs.stdout else "")
            err = err.strip() or "rsync failed"
            _emit_log(f"SSH rsync upload failed: {err}", level="error")
            return {
                "status": "error",
                "error": err,
                "final_url": f"{ssh_target}:{remote_dir}",
                "archive_path": target_archive_path,
                "rsync": rsync_cmd,
            }
    except Exception as e:
        msg = f"SSH rsync upload encountered an exception: {str(e)}"
        _emit_log(msg, level="error")
        return {"status": "error", "error": msg}
def stream_build_image(context_path: str, tag: Optional[str] = None, dockerfile: Optional[str] = None, build_args: Optional[Dict[str, str]] = None, task_id: Optional[str] = None, override_log_endpoint: Optional[str] = None, dockerfile_content: Optional[str] = None, dockerfile_name: Optional[str] = None, cleanup: Optional[bool] = True, nocache: Optional[bool] = True, emit: Optional[Callable[[Dict[str, Any]], None]] = None, app_id: Optional[str] = None, upload_url: Optional[str] = None) -> dict:
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
            msg = line.rstrip("\n")
            if not re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", msg):
                now = time.strftime('%Y-%m-%d %H:%M:%S')
                msg = f"[{now}] {msg}"
            with open(path, "a") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def _append_json(path: Optional[str], obj: Dict[str, Any]) -> None:
        if not path:
            return
        try:
            obj.setdefault("ts", _ts())
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
                    "ts": _ts(),
                })
            except Exception:
                pass
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] build starting dockerfile={df_arg or 'Dockerfile'} inline={bool(dockerfile_content)} app_id={app_id or ''}")
        _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] build starting dockerfile={df_arg or 'Dockerfile'} inline={bool(dockerfile_content)} app_id={app_id or ''}")
        _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "build_start", "tag": tag, "dockerfile": df_arg or "Dockerfile", "inline": bool(dockerfile_content), "app_id": app_id, "build_args": build_args or {}, "nocache": bool(nocache)})
        # Log build invocation details
        _append_json(build_structured_path, {
            "ts": _ts(), "level": "info", "event": "invocation",
            "context_path": context_path, "dockerfile": df_arg or "Dockerfile",
            "tag": tag, "build_args": build_args or {}, "app_id": app_id,
            "nocache": bool(nocache),
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
            nocache=bool(nocache),
        )
        step_count = 0
        build_failed = False
        build_failure_msg: Optional[str] = None
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
                elif "error" in chunk:
                    raw_text = chunk.get("error")
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

            # Detect unrecoverable build failures ONLY on Docker API error chunks,
            # and continue streaming to capture all remaining logs
            try:
                if entry_type == "error":
                    build_failed = True
                    build_failure_msg = raw_text
                    _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "build_failed_detected", "message": raw_text})
                    if emit:
                        try:
                            emit({
                                "task": "docker_build",
                                "task_id": task_id,
                                "stage": "building",
                                "status": "failed",
                                "error": raw_text,
                                "app_id": app_id,
                            })
                        except Exception:
                            pass
                    # Do not break; allow remaining chunks to flush so raw logs are complete
            except Exception:
                pass

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
                        "stage": "building",
                        "status": "stream",
                        "chunk": chunk,
                        "app_id": app_id,
                        "ts": _ts(),
                    })
                except Exception:
                    pass
        # Close last step if open
        if current_step:
            current_step["end"] = time.time()
            current_step["duration_sec"] = round(current_step["end"] - current_step["start"], 3)
            _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "step_end", **{k: v for k, v in current_step.items() if k != "start"}})

        # If build failed, do not proceed to next stages
        if build_failed:
            total_dur = round(time.time() - build_started, 3)
            _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] build failed error {build_failure_msg} app_id={app_id or ''}")
            _append_line(error_log_path, build_failure_msg or "build failed")
            _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "build_failed", "message": build_failure_msg})
            # Write failure summary immediately for consumers
            try:
                if summary_path:
                    _write_json(summary_path, {
                        "status": "failed",
                        "stage": "building",
                        "error": build_failure_msg,
                        "tag": tag,
                        "dockerfile_used": df_arg or "Dockerfile",
                        "inline": bool(dockerfile_content),
                        "nocache": bool(nocache),
                        "duration_sec": total_dur,
                        "steps_detected": len(steps),
                        "app_id": app_id,
                        "build_args": build_args or {},
                    })
            except Exception:
                pass
            # Raise to exit and prevent export/upload progression
            raise Exception(build_failure_msg or "Docker build failed")
        # Emit completed event
        if emit:
            try:
                emit({
                    "task": "docker_build",
                    "task_id": task_id,
                    "stage": "building",
                    "status": "completed",
                    "image_id": image_id,
                    "tag": tag,
                    "app_id": app_id,
                    "ts": _ts(),
                })
            except Exception:
                pass
        total_dur = round(time.time() - build_started, 3)
        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl build: project image built successfully duration={total_dur}s")
        _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl build: project image built successfully duration={total_dur}s")
        _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "build_completed", "stage": "building", "status": "completed", "duration_sec": total_dur, "command": "pxxl launch build"})
        # Export and compress the built image
        compressed_image_path = None
        if task_logs_dir and image_id:
            try:
                import subprocess
                import gzip
                
                # Export the Docker image to a tar file
                export_start = time.time()
                tar_path = os.path.join(task_logs_dir, "build.tar")
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
                compressed_path = os.path.join(task_logs_dir, "build.tar.gz")
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

        # Write initial summary before SFTP transfer so runtime can read it
        try:
            if task_logs_dir:
                summary_data = {
                    "status": "building",
                    "stage": "uploading",
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
                _write_json(summary_path, summary_data)
        except Exception:
            pass

        upload_result = None
        if upload_url and task_logs_dir:
            try:
                _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: artifact transfer started")
                _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: artifact transfer started")
                _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "transfer_started", "stage": "uploading", "status": "starting", "command": "pxxl launch transfer"})
                if emit:
                    try:
                        emit({
                            "task": "docker_build",
                            "task_id": task_id,
                            "stage": "uploading",
                            "status": "starting",
                            "app_id": app_id,
                            "ts": _ts(),
                        })
                    except Exception:
                        pass

                if upload_url and ('@' in str(upload_url)) and (not str(upload_url).lower().startswith('http')):
                    upload_result = transfer_build_to_rsync_ssh(
                        build_dir=task_logs_dir,
                        task_id=task_id,
                        ssh_target=str(upload_url),
                        emit=emit,
                        app_id=app_id,
                    )
                else:
                    upload_result = transfer_build_to_http(
                        build_dir=task_logs_dir,
                        task_id=task_id,
                        upload_url=upload_url,
                        emit=emit,
                        app_id=app_id,
                    ) 
                
                # remove accidental debug prints
                
                # Log upload url and curl used for the transfer
                try:
                    if upload_result and upload_result.get("final_url"):
                        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: upload url {upload_result.get('final_url')}")
                        _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: upload url {upload_result.get('final_url')}")
                        _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "transfer_details", "stage": "uploading", "status": "progress", "upload_url": upload_result.get("final_url"), "archive_path": upload_result.get("archive_path")})
                    if upload_result and upload_result.get("curl"):
                        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: curl {upload_result.get('curl')}")
                        _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: curl {upload_result.get('curl')}")
                        _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "transfer_curl", "stage": "uploading", "status": "progress", "curl": upload_result.get("curl")})
                    if upload_result and upload_result.get("rsync"):
                        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: rsync {upload_result.get('rsync')}")
                        _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: rsync {upload_result.get('rsync')}")
                        _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "transfer_rsync", "stage": "uploading", "status": "progress", "rsync": upload_result.get("rsync")})
                except Exception:
                    pass

                if upload_result["status"] == "success":
                    _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: artifact transfer completed successfully")
                    _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: artifact transfer completed successfully files={upload_result['files_transferred']} size_bytes={upload_result['total_size_bytes']}")
                    _append_json(build_structured_path, {"ts": _ts(), "level": "info", "event": "transfer_completed", "stage": "uploading", "status": "completed", "files_transferred": upload_result["files_transferred"], "total_size_bytes": upload_result["total_size_bytes"], "command": "pxxl launch transfer"})
                    if emit:
                        try:
                            emit({
                                "task": "docker_build",
                                "task_id": task_id,
                                "stage": "uploading",
                                "status": "completed",
                                "remote_path": upload_result.get("remote_path"),
                                "app_id": app_id,
                                "ts": _ts(),
                            })
                        except Exception:
                            pass
                else:
                    _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: artifact transfer failed")
                    _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: artifact transfer failed")
                    _append_line(error_log_path, "pxxl transfer: error during artifact transfer")
                    try:
                        _append_line(error_log_path, str(upload_result.get("error") or ""))
                        _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] transfer error detail: {str(upload_result.get('error') or '')}")
                        _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] transfer error detail: {str(upload_result.get('error') or '')}")
                    except Exception:
                        pass
                    _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "transfer_error", "stage": "uploading", "status": "error", "command": "pxxl launch transfer", "error": upload_result.get("error")})
                    if emit:
                        try:
                            emit({
                                "task": "docker_build",
                                "task_id": task_id,
                                "stage": "uploading",
                                "status": "error",
                                "error": upload_result.get("error"),
                                "app_id": app_id,
                                "ts": _ts(),
                            })
                        except Exception:
                            pass
                
            except Exception as upload_error:
                _append_line(events_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: artifact transfer encountered an exception")
                _append_line(build_log_path, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pxxl transfer: artifact transfer encountered an exception")
                _append_line(error_log_path, "pxxl transfer: exception during artifact transfer")
                _append_json(build_structured_path, {"ts": _ts(), "level": "error", "event": "transfer_exception", "stage": "uploading", "status": "error", "command": "pxxl launch transfer"})
                upload_result = {"status": "error", "error": str(upload_error)}

        # Write a summary JSON for convenience
        try:
            if task_logs_dir:
                summary_data = {
                    "status": "running",
                    "stage": "uploading",
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
                # Include HTTP upload outcome in summary under legacy key for compatibility
                if upload_result:
                    summary_data["sftp_deployment"] = upload_result
                    try:
                        if upload_result.get("status") == "error":
                            summary_data["status"] = "error"
                            summary_data["stage"] = "uploading"
                    except Exception:
                        pass
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
            # Surface upload outcome (success/error) in the response for debugging
            "sftp_deployment": upload_result,
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
                    "stage": "building",
                    "status": "failed",
                    "error": msg,
                    "app_id": app_id,
                    "ts": _ts(),
                })
            except Exception:
                pass
        # Write failure summary
        try:
            if summary_path:
                _write_json(summary_path, {
                    "status": "failed",
                    "stage": "building",
                    "error": msg,
                    "tag": tag,
                    "dockerfile_used": df_arg or "Dockerfile",
                    "inline": bool(dockerfile_content),
                    "nocache": bool(nocache),
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


def create_database_container(
    db_type: str,
    tag: Optional[str] = "latest",
    container_name: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    root_password: Optional[str] = None,
    db_name: Optional[str] = None,
    host_port: Optional[int] = None,
    network: Optional[str] = "traefik-network",
) -> Dict[str, Any]:
    """Create a database container and return details.

    Supported types: postgres, mysql, mongodb, redis
    - Ensures network exists and runs the container using run_container_extended
    - Supports optional host_port mapping; if not provided, Docker auto-assigns and we inspect
    - Returns container id/name, image, ports, and computed connection URLs
    """
    client = get_client()

    db_in = (db_type or "").strip().lower()
    db = "postgres" if db_in == "postgres" else ("mysql" if db_in == "mysql" else ("mongodb" if db_in in ("mongodb", "mongo") else ("redis" if db_in == "redis" else None)))
    if not db:
        return {"status": "error", "message": f"Unsupported db_type: {db_type}"}

    # Ensure network exists
    try:
        existing = [n.get("name") for n in list_networks()]
        if network and network not in existing:
            create_network(network)
    except Exception:
        pass

    # Build image/env/command/port key
    env: Dict[str, str] = {}
    command: Optional[str] = None
    if db == "postgres":
        image = f"postgres:{tag or 'latest'}"
        container_port_key = "5432/tcp"
        if not username or not password:
            return {"status": "error", "message": "POSTGRES requires username and password"}
        env["POSTGRES_USER"] = username
        env["POSTGRES_PASSWORD"] = password
        if db_name:
            env["POSTGRES_DB"] = db_name
    elif db == "mysql":
        image = f"mysql:{tag or 'latest'}"
        container_port_key = "3306/tcp"
        if not root_password:
            return {"status": "error", "message": "MYSQL requires root_password"}
        env["MYSQL_ROOT_PASSWORD"] = root_password
        if db_name:
            env["MYSQL_DATABASE"] = db_name
        if username and password:
            env["MYSQL_USER"] = username
            env["MYSQL_PASSWORD"] = password
    elif db == "mongodb":
        image = f"mongo:{tag or 'latest'}"
        container_port_key = "27017/tcp"
        # For MongoDB, set root user via env
        if not username or not root_password:
            return {"status": "error", "message": "MONGODB requires username and root_password"}
        env["MONGO_INITDB_ROOT_USERNAME"] = username
        env["MONGO_INITDB_ROOT_PASSWORD"] = root_password
        if db_name:
            env["MONGO_INITDB_DATABASE"] = db_name
    else:  # redis
        image = f"redis:{tag or 'latest'}"
        container_port_key = "6379/tcp"
        # Redis doesn't support password via env in official image; use command override
        if password:
            command = f"redis-server --requirepass {password}"

    # Ports mapping
    ports = {container_port_key: int(host_port)} if host_port else None

    # Generate a name if not provided
    if not container_name:
        rnd = str(random.randint(1000, 9999))
        container_name = f"db-{db}-{rnd}"

    # Run container
    run_res = run_container_extended(
        image=image,
        name=container_name,
        env=env,
        ports=ports,
        network=network,
        command=command,
        detach=True,
    )

    cid = run_res.get("id")
    details = {}
    host_port_val: Optional[int] = None
    try:
        details = client.api.inspect_container(cid)
        ports_map = ((details.get("NetworkSettings") or {}).get("Ports") or {})
        bindings = ports_map.get(container_port_key) or []
        if bindings:
            try:
                host_port_val = int(bindings[0].get("HostPort"))
            except Exception:
                host_port_val = None
    except Exception:
        details = {}

    # Compute URLs
    host = "localhost"
    internal_host = container_name
    if db == "postgres":
        final_db_name = db_name or (username if username else None)
        user_for_url = username or "postgres"
        pass_for_url = password or ""
        external_url = f"postgresql://{user_for_url}:{pass_for_url}@{host}:{host_port_val or (host_port or 5432)}/{final_db_name or ''}".rstrip("/")
        internal_url = f"postgresql://{user_for_url}:{pass_for_url}@{internal_host}:5432/{final_db_name or ''}".rstrip("/")
        urls = {"external_url": external_url, "internal_url": internal_url}
    elif db == "mysql":
        user_for_url = username or "root"
        pass_for_url = (password if username else root_password) or ""
        external_url = f"mysql://{user_for_url}:{pass_for_url}@{host}:{host_port_val or (host_port or 3306)}/{(db_name or '')}".rstrip("/")
        internal_url = f"mysql://{user_for_url}:{pass_for_url}@{internal_host}:3306/{(db_name or '')}".rstrip("/")
        root_url = None
        if username and password and root_password:
            root_url = f"mysql://root:{root_password}@{host}:{host_port_val or (host_port or 3306)}/{(db_name or '')}".rstrip("/")
        urls = {"external_url": external_url, "internal_url": internal_url}
        if root_url:
            urls["root_external_url"] = root_url
    elif db == "mongodb":
        # Root user is created in 'admin' authSource by init
        auth_qs = "?authSource=admin"
        db_path = f"/{db_name}" if db_name else "/"
        external_url = f"mongodb://{username}:{root_password}@{host}:{host_port_val or (host_port or 27017)}{db_path}{auth_qs}".rstrip("/")
        internal_url = f"mongodb://{username}:{root_password}@{internal_host}:27017{db_path}{auth_qs}".rstrip("/")
        urls = {"external_url": external_url, "internal_url": internal_url}
    else:  # redis
        port_val = host_port_val or (host_port or 6379)
        if password:
            external_url = f"redis://:{password}@{host}:{port_val}/0"
            internal_url = f"redis://:{password}@{internal_host}:6379/0"
        else:
            external_url = f"redis://{host}:{port_val}/0"
            internal_url = f"redis://{internal_host}:6379/0"
        urls = {"external_url": external_url, "internal_url": internal_url}

    # Mask sensitive env values in response copy
    env_safe = {}
    for k, v in env.items():
        if "PASSWORD" in k.upper():
            env_safe[k] = "***"
        else:
            env_safe[k] = v

    result = {
        "status": "created",
        "type": db,
        "image": image,
        "id": cid,
        "name": container_name,
        "ports": {container_port_key: host_port_val or host_port},
        "env": env_safe,
        "urls": urls,
        "network": network,
        "inspect": {
            "state": (details.get("State") if isinstance(details, dict) else None),
            "network_settings": (details.get("NetworkSettings") if isinstance(details, dict) else None),
        },
    }

    return result


def start_container(id_or_name: str) -> dict:
    client = get_client()
    c = client.containers.get(id_or_name)
    c.start()
    return {"status": "ok", "id": c.id, "name": c.name}


def start_exec_pty(id_or_name: str, cmd: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None) -> Dict[str, Any]:
    client = get_client()
    command = cmd or ["/bin/sh"]
    exec_id = client.api.exec_create(container=id_or_name, cmd=command, tty=True, stdin=True, environment=env, workdir=cwd)
    sock = client.api.exec_start(exec_id, detach=False, tty=True, stream=False, socket=True)
    return {"exec_id": exec_id, "socket": sock}


def resize_exec(exec_id: str, width: int, height: int) -> Dict[str, Any]:
    client = get_client()
    client.api.exec_resize(exec_id, height=height, width=width)
    return {"exec_id": exec_id, "width": width, "height": height}
