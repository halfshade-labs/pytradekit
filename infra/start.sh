#!/bin/bash
set -e

# 获取脚本所在目录（infra/）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 获取 .env 文件路径（上一级的上一级）
ENV_FILE="$(cd "$SCRIPT_DIR/../.." && pwd)/.env"

# 检查 .env 文件是否存在
if [ ! -f "$ENV_FILE" ]; then
    echo "Error: .env file not found at $ENV_FILE"
    echo "Please create .env file with MONGO_USERNAME and MONGO_PASSWORD"
    exit 1
fi

# 加载 .env 文件中的变量（只加载 KEY=VALUE 格式的行）
echo "Loading environment variables from $ENV_FILE"
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

# 检查必需的 MongoDB 环境变量
if [ -z "$MONGO_USERNAME" ]; then
    echo "Error: MONGO_USERNAME is not set in .env file"
    exit 1
fi

if [ -z "$MONGO_PASSWORD" ]; then
    echo "Error: MONGO_PASSWORD is not set in .env file"
    exit 1
fi

echo "Starting infra services..."
echo "Compose file: $SCRIPT_DIR/docker-compose.yml"
echo "MONGO_USERNAME: $MONGO_USERNAME"
echo "MONGO_PASSWORD: *** (set)"

cd "$SCRIPT_DIR"
docker compose up -d

echo ""
echo "Infra services started successfully!"
echo "MongoDB: infra_mongodb (127.0.0.1:27017)"
echo "Redis: infra_redis (127.0.0.1:6379)"
echo "Network: infra_network"