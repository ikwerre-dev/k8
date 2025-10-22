import os
import subprocess
import time
import uuid
from typing import Optional, Dict, Any, Callable

import requests

from config_service import get_log_endpoint, get_auth_header
import docker_service as ds


def _post(endpoint: Optional[str], payload: Dict[str, Any]) -> None:
    if not endpoint:
        return
    headers = get_auth_header()
    try:
        requests.post(endpoint, json=payload, headers=headers, timeout=5)
    except Exception:
        pass


def run_html_site_pipeline(
    repo_url: str,
    dockerhub_repo: str,
    tag: str = "latest",
    host_port: int = 8080,
    dockerhub_username: Optional[str] = None,
    dockerhub_password: Optional[str] = None,
    task_id: Optional[str] = None,
    emit: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Clone a static HTML repo, build an nginx image, push to Docker Hub,
    pull it, and run it locally. Stream stage updates to the configured log endpoint.
    Optionally emit events to an in-process listener (task registry).
    """
    endpoint = get_log_endpoint()
    stages: Dict[str, Any] = {}
    run_result: Dict[str, Any] = {}

    def _emit(ev: Dict[str, Any]) -> None:
        if emit:
            try:
                emit(ev)
            except Exception:
                pass

    # Resolve credentials
    username = dockerhub_username or os.environ.get("DOCKERHUB_USERNAME")
    password = dockerhub_password or os.environ.get("DOCKERHUB_PASSWORD")

    # Workspace
    ts = int(time.time())
    workdir = os.path.join("/tmp", f"html-site-{ts}-{uuid.uuid4().hex[:6]}")
    os.makedirs(workdir, exist_ok=True)
    repo_dir = os.path.join(workdir, "repo")

    # Stage: clone
    payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "clone", "status": "starting", "repo_url": repo_url}
    _post(endpoint, payload)
    _emit(payload)
    try:
        res = subprocess.run(["git", "clone", "--depth=1", repo_url, repo_dir], capture_output=True, text=True, check=False)
        stages["clone"] = {"exit_code": res.returncode, "stdout": res.stdout, "stderr": res.stderr}
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "clone", "status": "completed", "exit_code": res.returncode}
        _post(endpoint, payload)
        _emit(payload)
        if res.returncode != 0:
            raise RuntimeError(f"git clone failed: {res.stderr}")
    except Exception as e:
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "clone", "status": "error", "error": str(e)}
        _post(endpoint, payload)
        _emit(payload)
        raise

    # Prepare Dockerfile (nginx serving static content)
    dockerfile_content = (
        "FROM nginx:alpine\n"
        "COPY . /usr/share/nginx/html\n"
        "EXPOSE 80\n"
        "CMD [\"nginx\", \"-g\", \"daemon off;\"]\n"
    )

    repo_tag = f"{dockerhub_repo}:{tag}"

    # Stage: build
    payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "build", "status": "starting", "repo_tag": repo_tag}
    _post(endpoint, payload)
    _emit(payload)
    try:
        build_res = ds.stream_build_image(
            context_path=repo_dir,
            tag=repo_tag,
            dockerfile_content=dockerfile_content,
            dockerfile_name="Dockerfile",
            cleanup=True,
            task_id=task_id,
        )
        stages["build"] = build_res
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "build", "status": "completed", "image_id": build_res.get("image_id")}
        _post(endpoint, payload)
        _emit(payload)
    except Exception as e:
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "build", "status": "error", "error": str(e)}
        _post(endpoint, payload)
        _emit(payload)
        raise

    # Stage: login
    payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "login", "status": "starting", "registry": "dockerhub"}
    _post(endpoint, payload)
    _emit(payload)
    if not username or not password:
        err = "Docker Hub credentials are required (env DOCKERHUB_USERNAME/DOCKERHUB_PASSWORD or request fields)."
        stages["login"] = {"status": "error", "error": err}
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "login", "status": "error", "error": err}
        _post(endpoint, payload)
        _emit(payload)
        raise RuntimeError(err)
    try:
        login_res = ds.login(username, password, registry="https://index.docker.io/v1/")
        stages["login"] = login_res
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "login", "status": "completed"}
        _post(endpoint, payload)
        _emit(payload)
    except Exception as e:
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "login", "status": "error", "error": str(e)}
        _post(endpoint, payload)
        _emit(payload)
        raise

    # Stage: push
    payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "push", "status": "starting", "repo_tag": repo_tag}
    _post(endpoint, payload)
    _emit(payload)
    try:
        push_res = ds.push_image(repo_tag)
        stages["push"] = push_res
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "push", "status": "completed"}
        _post(endpoint, payload)
        _emit(payload)
    except Exception as e:
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "push", "status": "error", "error": str(e)}
        _post(endpoint, payload)
        _emit(payload)
        raise

    # Stage: pull
    payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "pull", "status": "starting", "name": dockerhub_repo, "tag": tag}
    _post(endpoint, payload)
    _emit(payload)
    try:
        pull_res = ds.pull_image(dockerhub_repo, tag)
        stages["pull"] = pull_res
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "pull", "status": "completed"}
        _post(endpoint, payload)
        _emit(payload)
    except Exception as e:
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "pull", "status": "error", "error": str(e)}
        _post(endpoint, payload)
        _emit(payload)
        raise

    # Stage: run
    payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "run", "status": "starting", "host_port": host_port}
    _post(endpoint, payload)
    _emit(payload)
    try:
        run_res = ds.run_container(
            image=repo_tag,
            name=f"html-site-{uuid.uuid4().hex[:6]}",
            command=None,
            ports={"80/tcp": host_port},
            env=None,
            detach=True,
        )
        stages["run"] = run_res
        run_result = run_res
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "run", "status": "completed", "container_id": run_res.get("id")}
        _post(endpoint, payload)
        _emit(payload)
    except Exception as e:
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "run", "status": "error", "error": str(e)}
        _post(endpoint, payload)
        _emit(payload)
        raise

    # Stage: probe HTTP
    url = f"http://localhost:{host_port}/"
    payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "probe", "status": "starting", "url": url}
    _post(endpoint, payload)
    _emit(payload)
    probe = {}
    try:
        r = requests.get(url, timeout=5)
        probe = {"status_code": r.status_code, "length": len(r.text), "ok": r.ok}
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "probe", "status": "completed", "status_code": r.status_code}
        _post(endpoint, payload)
        _emit(payload)
    except Exception as e:
        probe = {"error": str(e)}
        payload = {"task": "html_site_pipeline", "task_id": task_id, "stage": "probe", "status": "error", "error": str(e)}
        _post(endpoint, payload)
        _emit(payload)

    return {
        "status": "ok",
        "repo_url": repo_url,
        "repo_tag": repo_tag,
        "host_port": host_port,
        "run": run_result,
        "stages": stages,
        "probe": probe,
        "url": url,
    }