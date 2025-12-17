#!/bin/bash
set -e

# 获取脚本所在目录（infra/）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 获取 .env 文件路径（上一级的上一级）
ENV_FILE="$(cd "$SCRIPT_DIR/../.." && pwd)/.env"

# 加载 .env 文件中的变量（只加载 KEY=VALUE 格式的行）
if [ -f "$ENV_FILE" ]; then
    echo "Loading environment variables from $ENV_FILE"
    # 使用 export 和 grep 安全地加载变量，只处理 KEY=VALUE 格式
    while IFS='=' read -r key value; do
        # 跳过注释和空行
        [[ "$key" =~ ^#.*$ ]] && continue
        [[ -z "$key" ]] && continue
        # 移除 key 和 value 两边的空格
        key=$(echo "$key" | xargs)
        value=$(echo "$value" | xargs)
        # 导出变量
        export "$key=$value"
    done < <(grep -E "^[A-Z_]+=.*" "$ENV_FILE")
else
    echo "Warning: .env file not found at $ENV_FILE"
fi

echo "Starting infra services..."
echo "Compose file: $SCRIPT_DIR/docker-compose.yml"
echo "MONGO_USERNAME: ${MONGO_USERNAME:-admin (default)}"
echo "MONGO_PASSWORD: ${MONGO_PASSWORD:+*** (set)}"

cd "$SCRIPT_DIR"
docker compose up -d

echo ""
echo "Infra services started successfully!"
echo "MongoDB: infra_mongodb (127.0.0.1:27017)"
echo "Redis: infra_redis (127.0.0.1:6379)"
echo "Network: infra_network"
