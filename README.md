# Docker Manager API

This API manages Docker images and containers, streams logs for long-running tasks, and includes a real-time HTML-site pipeline.

## Quick Start
- Run locally: `uvicorn main:app --reload --port 8002`
- Docs: `http://localhost:8002/docs`

## Images
- `POST /docker/build` – `{ "path": ".", "tag": "myimage:latest" }`
- `POST /docker/build-stream` – Stream build logs from Docker API
  - Body:
    - `context_path` (string): Path to build context directory.
    - `tag` (string, optional): Image tag.
    - `dockerfile` (string, optional): Dockerfile path relative to `context_path`.
    - `dockerfile_content` (string, optional): Inline Dockerfile content to write before build.
    - `dockerfile_name` (string, optional): Filename to use when writing inline content (default `Dockerfile`).
    - `cleanup` (bool, optional): Remove the written Dockerfile after build (default true).
    - `build_args` (object, optional): Build args.
    - `task_id` (string, optional): Correlation ID.
  - Behavior:
    - If `dockerfile_content` is provided, it is written to `context_path` as `dockerfile_name` (default `Dockerfile`), then used for the build. Non-default names are passed to the Docker API via `dockerfile`.
    - Emits metadata in stream payloads: `dockerfile` used and whether `inline` content was used.
    - Collects and returns the tail of logs for convenience.
- `POST /docker/build/start` – Start a build asynchronously and stream logs to task registry
  - Body:
    - `dockerfile_content` (string, optional): Inline Dockerfile.
    - `dockerfile` (string, optional): Dockerfile path relative to temp context.
    - `dockerfile_name` (string, optional): Filename for inline content.
    - `build_args` (object, optional): Build args.
    - `cleanup` (bool, optional): Cleanup written Dockerfile (default true).
    - `tag` (string, optional): Image tag; defaults to `<app_id>:latest` if provided.
    - `app_id` (string, optional): Used to annotate events and default tag.
  - Response: `{ "task_id": "docker_build-xxxxxxxxxx", "status": "started" }`
  - Retrieve logs: `GET /tasks/logs/{task_id}`
- `POST /docker/tag` – `{ "source": "myimage:latest", "repo": "user/myimage", "tag": "latest" }`
- `POST /docker/login` – `{ "username": "user", "password": "pass", "registry": "https://index.docker.io/v1/" }`
- `POST /docker/push` – `{ "repo_tag": "user/myimage:latest" }`
- `POST /docker/pull` – `{ "name": "nginx", "tag": "latest" }`
- `POST /docker/save` – `{ "repo_tag": "user/myimage:latest", "tar_path": "./myimage.tar" }`
- `POST /docker/load` – `{ "tar_path": "./myimage.tar" }`
- `GET /docker/images` – List images
- `POST /docker/rmi` – Remove image `{ "id_or_name": "user/myimage:latest", "force": true }`

## Containers
- `POST /docker/run` – Simple run
- `POST /docker/run-extended` – Run with resource limits and logging
  - Example:
    ```json
    {
      "image": "nginx:latest",
      "name": "web",
      "ports": {"80/tcp": 8080},
      "mem_limit": "512m",
      "nano_cpus": 1000000000,
      "cpu_shares": 1024,
      "pids_limit": 128,
      "log_driver": "json-file",
      "log_options": {"max-size": "10m", "max-file": "3"}
    }
    ```
- `GET /docker/containers?all=true` – List containers
- `POST /docker/stop` – `{ "id_or_name": "web" }`
- `POST /docker/rm` – `{ "id_or_name": "web", "force": true }`
- `GET /docker/logs/{id_or_name}?tail=100` – Container logs
- `POST /docker/exec` – `{ "id_or_name": "web", "cmd": "ls -la" }`

## Filesystem in Container
- `POST /docker/container/fs/list` – `{ "id_or_name": "web", "path": "/var/log" }`
- `POST /docker/container/fs/read` – `{ "id_or_name": "web", "path": "/etc/nginx/nginx.conf" }`
- `POST /docker/container/fs/write` – Write file content (optionally base64)
- `POST /docker/container/fs/mkdir` – Create directory
- `POST /docker/container/fs/delete` – Delete path (dangerous)

## System
- `GET /system/info` – Host info
- `GET /system/usage` – CPU/Mem/Disk utilization
- `GET /system/top?sort=cpu&limit=10` – Top processes by `cpu` or `memory`.

## Task Runner
- `POST /tasks/run-stream` – Run a generic command and stream stdout lines
  - Body: `{ "cmd": "npm run build", "cwd": "/app", "task_id": "task-456" }`
  - Behavior: Spawns process, POSTs each line to `log_stream_endpoint` with `task=generic_task`, and posts completion with `exit_code`.
- `GET /tasks/status/{task_id}` – Retrieve current status, stage events, and result
- `GET /tasks/logs/{task_id}` – Retrieve tail of build log lines by `task_id`

## Real-time Pipeline Runner
- `POST /pipeline/html-site/start` – Start the HTML site pipeline asynchronously
  - Returns `{ "task_id": "html_site_pipeline-xxxxxxxxxx", "status": "started" }`
  - Body:
    - `repo_url` (string, optional): Defaults to `https://github.com/ikwerre-dev/html-site`
    - `dockerhub_repo` (string): e.g., `youruser/html-site`
    - `tag` (string, optional): Defaults to `latest`
    - `host_port` (int, optional): Defaults to `8080`
    - `dockerhub_username` / `dockerhub_password` (optional): overrides env
- `GET /tasks/status/{task_id}` – Retrieve current status, stage events, and result
  - Response example:
    - `{ "task_type": "html_site_pipeline", "status": "running|completed|error", "events": [ { "stage": "clone", "status": "starting" }, ... ], "result": { ... }, "error": "..." }`
- Behavior:
  - Emits stage events for `clone`, `build`, `login`, `push`, `pull`, `run`, and `probe`.
  - If `config.json` provides `log_stream_endpoint`, events are also posted externally for unified streaming.
  - The synchronous endpoint `POST /pipeline/html-site` continues to perform an immediate run and returns a summary upon completion.