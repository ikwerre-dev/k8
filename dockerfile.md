docker build --no-cache -t k8-app .

docker stop k8-container
docker rm k8-container
 
docker run -d \
  -p 8000:8000 \
  -v /pxxl/upload:/app/upload \
  -v /pxxl/builds:/app/builds \
  -v /pxxl/db:/app/db \
  -v /pxxl/proxy:/pxxl/proxy \
  -v /etc/nginx:/etc/nginx \
  -v /usr/sbin/nginx:/usr/sbin/nginx \
  -v /var/run/nginx.pid:/var/run/nginx.pid \
  -v /etc/ssl/certs:/etc/ssl/certs \
  -v /etc/ssl/private:/etc/ssl/private \
  -v /var/lib/nginx:/var/lib/nginx \
  -v /var/log/nginx:/var/log/nginx \ 
  -v /var/run/docker.sock:/var/run/docker.sock \
  --name k8-container \
  k8-app


build server:


docker build --no-cache -t k8-app .
docker stop k8-container
docker rm k8-container

docker run -d \
  -p 8000:8000 \
  -v /pxxl/upload:/app/upload \
  -v /pxxl/builds:/app/builds \
  -v /pxxl/db:/app/db \
  -v /root/.ssh/id_ed25519_build:/root/.ssh/id_ed25519:ro \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --name k8-container \
  k8-app



  runtime server:
    docker run -d \
  --name nginx-proxy \
  --network traefik-network \
  -p 80:80 -p 443:443 \
  -v /pxxl/proxy/conf:/etc/nginx/conf.d \
  -v /pxxl/proxy/html:/usr/share/nginx/html \
  nginx

