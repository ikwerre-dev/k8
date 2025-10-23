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

## Local Run
- `POST /docker/localrun` – Decompress a local `.lz4` Docker image tarball, load it, and run
  - Body:
    - `lz4_path` (string): Local relative path to the `.lz4` file (e.g., `./dist/app.tar.lz4`).
    - `task_id` (string): Task/build ID; local run writes logs under `/upload/pxxl/{task_id}` when available, otherwise falls back to `./builds/{task_id}`.
    - `app_id` (string, optional): App identifier for container naming. If provided, container name becomes `{app_id}-{task_id}`.
    - `ports` (object, optional): Port bindings, e.g., `{ "8000/tcp": 8000 }`.
    - `env` (object, optional): Env variables map.
    - `command` (string, optional): Container command.
    - `name` (string, optional): Container name (overrides auto-generated name).
    - `cpu` (number, optional): CPUs to allocate (e.g., `0.5`, `1`, `2`).
    - `cpuset` (string, optional): CPU set (e.g., `"0-2"`).
    - `memory` (string, optional): Memory limit (e.g., `"512m"`, `"1g"`).
  - Behavior:
    - Writes logs to `/upload/pxxl/{task_id}` (or `/pxxl/upload/{task_id}`) when present; otherwise falls back to `./builds/{task_id}` including `events.log`, `error.log`, and `build.jsonl`.
    - Uses stages: `decompiling` (decompress), then `running` (start container).
    - Updates `build.info.json` with `status: "building"`, current `stage`, `container_id`, and `container_name`.
    - Container naming: Uses `{app_id}-{task_id}` format when `app_id` is provided and `name` is not specified.

## Stage Model
- Build flow now tracks `stage` across operations:
  - `building` – Docker build and image export/compression.
  - `uploading` – SFTP transfer of build artifacts to remote.
  - `decompiling` – Local decompression of `.lz4` image tarball.
  - `running` – Container run lifecycle.
- After SFTP upload, the build summary keeps `status: "building"` (not completed) and sets `stage: "uploading"`.

## Run
- `POST /docker/run` – Run a Docker container with resource limits
  - Body:
    - `image` (string): Docker image name/tag.
    - `name` (string, optional): Container name (overrides auto-generated name).
    - `command` (string, optional): Container command.
    - `ports` (object, optional): Port bindings, e.g., `{ "8000/tcp": 8000 }`.
    - `env` (object, optional): Environment variables map.
    - `detach` (boolean, optional): Run in detached mode (default: `true`).
    - `cpu` (number, optional): CPUs to allocate (e.g., `0.5`, `1`, `2`).
    - `cpuset` (string, optional): CPU set (e.g., `"0-2"`).
    - `memory` (string, optional): Memory limit (e.g., `"512m"`, `"1g"`).
    - `app_id` (string, optional): App identifier for container naming.
    - `task_id` (string, optional): Task identifier for container naming.
  - Container naming: Uses `{app_id}-{task_id}` format when both are provided and `name` is not specified.
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

### Logs Retrieval
- `GET /tasks/logs/{task_id}?server=build&tail=200`
  - Reads build logs from the build server path: `./builds/{task_id}`.
- `GET /tasks/logs/{task_id}?server=runtime&tail=200`
  - Reads runtime logs from `/app/upload/{task_id}`.
- `GET /tasks/logs/{task_id}?tail=200`
  - Reads runtime logs from `/app/upload/{task_id}` when available.
  - Fallbacks: `/upload/pxxl/{task_id}`, `/pxxl/upload/{task_id}`, or `/uploads/{task_id}` if present; otherwise falls back to `./builds/{task_id}`.
  - Returns aggregated `build.log` (raw), `build.jsonl` (structured), `events.log`, `error.log`, plus metadata from `build.info.json` and `dockerfile.parsed.json`.

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
- `GET /tasks/logs/{task_id}` – Retrieve build metadata and healthcheck info by `task_id`
  - Response fields:
    - `status` – build status (`building` | `successful` | `failed` | `error`)
    - `healthcheck` – boolean, true if container is running at check time
    - `healthchecksstatus` – string container status at check time
    - `isChecking` – boolean, true while health check is pending
    - `healthcheck_time` – number of seconds for the scheduled check (default 15)
    - `start_check_time` – ISO timestamp when the health check window starts
    - `end_check_time` – ISO timestamp when the health check will run

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

## Nginx Sites
- `POST /nginx/site/create` – `{ "domain": "example.com", "port": 8080 }`
- `POST /nginx/site/delete` – `{ "domain": "example.com" }`
- `POST /nginx/sign-domain` – Write `appid.conf`, reload Nginx, stop old container
  - Body:
    - `domain` (string): Domain to serve
    - `app_id` (string, optional): Used as config filename `appid.conf`; auto-filled if `task_id` is provided
    - `conf_dir` (string): Directory to write the `.conf` file
    - `new_container` (string, optional): ID or name of the new container; auto-filled if `task_id` is provided
    - `old_container` (string, optional): ID or name of the old container to stop
    - `port` (int, optional): Upstream port; auto-detected from `new_container` or auto-filled from `build.info.json`
    - `task_id` (string, optional): If provided, reads `builds/{task_id}/build.info.json` to auto-fill `app_id`, `new_container`, and `port`
  - Behavior:
    - Generates an Nginx config that proxies `domain` to the new container host port
    - Writes to `{conf_dir}/{app_id}.conf` (creates or updates if exists)
    - Reloads Nginx without stopping (`nginx -s reload`)
    - Stops `old_container` if provided
- `POST /app/update-port` – Update both Docker container port and Nginx config using saved build metadata
  - Body:
    - `task_id` (string): Task/build ID to locate `build.info.json`
    - `new_port` (int): New host port to bind and proxy
    - `conf_dir` (string): Directory where `{app_id}.conf` resides
    - `domain` (string): Domain to serve
  - Behavior:
    - Stops and removes the existing container recorded in `build.info.json`
    - Re-runs the container with the same name and environment, binding `new_port`
    - Updates `build.info.json` (`host_port`, `ports_requested`)
    - Updates `{conf_dir}/{app_id}.conf` to proxy to `new_port` and reloads Nginx
  - Returns `{ stage: "update_port", status: "completed", new_port, container, nginx_reload }`
- Returns `{ stage: "signing_domain", status: "completed", conf_path, port, nginx_reload, old_stop }`