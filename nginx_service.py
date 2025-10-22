import os
import platform
from typing import Dict

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


def generate_config(domain: str, port: int) -> str:
    domain = _sanitize_domain(domain)
    upstream = _upstream_host()

    # Note: limit_req_zone must be declared in http context; many setups include site files inside http, so we include it here.
    # SSL certificate/key directives are omitted; ensure your global config or cert automation provides them.
    return f"""
limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;

server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    server_name {domain};

    # ssl_certificate /etc/ssl/certs/{domain}.crt;
    # ssl_certificate_key /etc/ssl/private/{domain}.key;

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


def create_site(domain: str, port: int) -> Dict[str, str]:
    os.makedirs(SITES_DIR, exist_ok=True)
    domain = _sanitize_domain(domain)
    path = os.path.join(SITES_DIR, f"{domain}.conf")
    conf = generate_config(domain, port)
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