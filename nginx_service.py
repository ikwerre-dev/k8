import os
import platform
import subprocess
from typing import Dict, Optional

SITES_DIR = "/pxxl/sites"


def _sanitize_domain(domain: str) -> str:
    d = domain.strip()
    d = d.replace("http://", "").replace("https://", "")
    return d.strip("/")


def _upstream_host() -> str:
    # Prefer Docker Desktop host on macOS; fallback to localhost
    if platform.system().lower() == "darwin":
        return "host.docker.internal"
    return "127.0.0.1"


def generate_config(domain: str, port: int, upstream_host: str = None, ssl: Optional[bool] = None) -> str:
    domain = _sanitize_domain(domain)
    upstream = upstream_host or _upstream_host()

    acme_location = f"""
    location ^~ /.well-known/acme-challenge/ {{
        root /usr/share/nginx/html;
        default_type text/plain;
        try_files $uri =404;
    }}
    """

    # Note: limit_req_zone must be declared in http context; many setups include site files inside http, so we include it here.
    if ssl is None:
        # Legacy: HTTP redirects to HTTPS; HTTPS proxies with WS headers
        return f"""
limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;

server {{
    listen 80;
    server_name {domain};
    {acme_location}
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    server_name {domain};

    ssl_certificate /etc/ssl/certs/{domain}.crt;
    ssl_certificate_key /etc/ssl/private/{domain}.key;

    location / {{
        limit_req zone=api_limit burst=20 nodelay;
        proxy_pass http://{upstream}:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Accept-Encoding "";

        sub_filter 'http://{domain}' 'https://{domain}';
        sub_filter_once off;
    }}
}}
"""
    elif ssl is True:
        # Both HTTP and HTTPS proxy, include WS headers in both
        return f"""
limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;

server {{
    listen 80;
    server_name {domain};
    {acme_location}
    location / {{
        limit_req zone=api_limit burst=20 nodelay;
        proxy_pass http://{upstream}:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Accept-Encoding "";
    }}
}}

server {{
    listen 443 ssl;
    server_name {domain};

    ssl_certificate /etc/ssl/certs/{domain}.crt;
    ssl_certificate_key /etc/ssl/private/{domain}.key;

    location / {{
        limit_req zone=api_limit burst=20 nodelay;
        proxy_pass http://{upstream}:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Accept-Encoding "";

        sub_filter 'http://{domain}' 'https://{domain}';
        sub_filter_once off;
    }}
}}
"""
    else:
        # ssl is False: only HTTP proxy with WS headers
        return f"""
limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;

server {{
    listen 80;
    server_name {domain};
    {acme_location}
    location / {{
        limit_req zone=api_limit burst=20 nodelay;
        proxy_pass http://{upstream}:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Accept-Encoding "";
    }}
}}
"""


def create_site(domain: str, port: int, upstream_host: str = None) -> Dict[str, str]:
    os.makedirs(SITES_DIR, exist_ok=True)
    domain = _sanitize_domain(domain)
    path = os.path.join(SITES_DIR, f"{domain}.conf")
    conf = generate_config(domain, port, upstream_host=upstream_host, ssl=None)
    with open(path, "w") as f:
        f.write(conf)
    return {"status": "created", "path": path, "domain": domain}


def delete_site(domain: str) -> Dict[str, str]:
    domain = _sanitize_domain(domain)
    path = os.path.join(SITES_DIR, f"{domain}.conf")
    if os.path.exists(path):
        os.remove(path)
        return {"status": "deleted", "path": path, "domain": domain}
    return {"status": "not_found", "path": path, "domain": domain}


def create_or_update_site_in_dir(app_id: str, domain: str, port: int, conf_dir: str, upstream_host: str = None, ssl: Optional[bool] = None) -> Dict[str, str]:
    os.makedirs(conf_dir, exist_ok=True)
    domain = _sanitize_domain(domain)
    filename = f"{app_id}.conf"
    path = os.path.join(conf_dir, filename)
    conf = generate_config(domain, port, upstream_host=upstream_host, ssl=ssl)
    status = "updated" if os.path.exists(path) else "created"
    with open(path, "w") as f:
        f.write(conf)
    return {"status": status, "path": path, "domain": domain, "app_id": app_id, "port": str(port)}


def reload_nginx() -> Dict[str, str]:
    try:
        res = subprocess.run(["nginx", "-s", "reload"], capture_output=True, text=True)
        if res.returncode == 0:
            return {"status": "reloaded"}
        return {"status": "error", "returncode": str(res.returncode), "stderr": res.stderr}
    except Exception as e:
        return {"status": "not_available", "error": str(e)}