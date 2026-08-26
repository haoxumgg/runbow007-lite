#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$project_root/compose.web.yaml"
secrets_file="${RUNBOW007_SECRETS_FILE:-/etc/runbow007/secrets.env}"
web_port="${RUNBOW007_WEB_PORT:-18080}"

if ((EUID != 0)); then
  echo "请使用 sudo 执行本脚本" >&2
  exit 2
fi

if [[ ! "$web_port" =~ ^[0-9]+$ ]] || ((web_port < 1024 || web_port > 65535)); then
  echo "RUNBOW007_WEB_PORT 必须是 1024-65535 之间的端口" >&2
  exit 2
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "未找到 Docker Compose 插件，请先确认 docker compose version 可用" >&2
  exit 2
fi

cd "$project_root"

if [[ ! -f config.yaml ]]; then
  cp config.example.yaml config.yaml
  echo "已创建 $project_root/config.yaml"
fi

if [[ ! -f "$secrets_file" ]]; then
  install -d -m 0750 "$(dirname "$secrets_file")"
  install -m 0600 deploy/secrets.env.example "$secrets_file"
  echo "已创建 $secrets_file"
  echo "请填写飞书配置和网页账号密码，然后重新执行本脚本" >&2
  exit 2
fi

required_keys=(
  RUNBOW007_FEISHU_APP_ID
  RUNBOW007_FEISHU_APP_SECRET
  RUNBOW007_FEISHU_CHAT_ID
  RUNBOW007_WEB_USERNAME
  RUNBOW007_WEB_PASSWORD
)
missing_keys=()
for key in "${required_keys[@]}"; do
  value="$(grep -E "^${key}=" "$secrets_file" | tail -n 1 || true)"
  value="${value#*=}"
  value="${value%$'\r'}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ ${#value} -ge 2 ]] \
    && { [[ "$value" == \"*\" ]] || [[ "$value" == \'*\' ]]; }; then
    value="${value:1:${#value}-2}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
  fi
  if [[ -z "$value" ]]; then
    missing_keys+=("$key")
  fi
done
if ((${#missing_keys[@]})); then
  echo "$secrets_file 缺少以下必填项：" >&2
  printf '  %s\n' "${missing_keys[@]}" >&2
  exit 2
fi
chmod 0600 "$secrets_file"

runtime_dirs=(
  "$project_root/data"
  "$project_root/downloads"
  "$project_root/logs"
)
for runtime_dir in "${runtime_dirs[@]}"; do
  if [[ "$runtime_dir" != "$project_root/"* ]] || [[ -L "$runtime_dir" ]]; then
    echo "拒绝修改项目目录之外或符号链接指向的运行目录: $runtime_dir" >&2
    exit 2
  fi
  install -d -m 0700 -o 10001 -g 10001 "$runtime_dir"
  chown -R -P 10001:10001 "$runtime_dir"
  chmod -R u=rwX,go= "$runtime_dir"
done

export RUNBOW007_SECRETS_FILE="$secrets_file"
export RUNBOW007_WEB_PORT="$web_port"

# 先完成新镜像构建，避免构建失败时提前中断当前服务。
docker compose -f "$compose_file" build web

# 使用 Compose 自己解析 env_file，再在容器内确认必填值存在；只输出缺失字段名。
docker compose -f "$compose_file" run --rm --no-deps --entrypoint python web \
  -c 'import os, sys; keys=("RUNBOW007_FEISHU_APP_ID", "RUNBOW007_FEISHU_APP_SECRET", "RUNBOW007_FEISHU_CHAT_ID", "RUNBOW007_WEB_USERNAME", "RUNBOW007_WEB_PASSWORD"); missing=[key for key in keys if not os.getenv(key, "").strip()]; print("容器缺少必填项: " + ", ".join(missing), file=sys.stderr) if missing else None; raise SystemExit(bool(missing))'

# 兼容此前用 docker run 创建的同名旧容器；只替换本项目的 Web 容器。
if docker container inspect runbow007-web >/dev/null 2>&1; then
  legacy_config="$(
    docker inspect --format \
      '{{range .Mounts}}{{if eq .Destination "/app/config.yaml"}}{{.Source}}{{end}}{{end}}' \
      runbow007-web
  )"
  if [[ "$legacy_config" != "$project_root/config.yaml" ]]; then
    echo "发现同名容器 runbow007-web，但它不属于 $project_root；已停止部署且未删除该容器" >&2
    exit 2
  fi
  docker rm -f runbow007-web >/dev/null
fi

docker compose -f "$compose_file" up -d --no-build --force-recreate web

for _ in {1..30}; do
  container_id="$(docker compose -f "$compose_file" ps -q web)"
  health_status=""
  if [[ -n "$container_id" ]]; then
    health_status="$(
      docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
        "$container_id" 2>/dev/null || true
    )"
  fi
  if [[ "$health_status" == "healthy" ]] \
    && curl -fsS "http://127.0.0.1:${web_port}/healthz" >/dev/null; then
    echo
    echo "runbow007 Web 已启动"
    echo "本机健康检查: http://127.0.0.1:${web_port}/healthz"
    echo "公网访问地址: http://<服务器公网IP>:${web_port}/"
    exit 0
  fi
  sleep 1
done

echo "服务未在 30 秒内通过健康检查，最近日志如下：" >&2
docker compose -f "$compose_file" logs --tail 100 web >&2
exit 1
