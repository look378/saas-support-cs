# =====================================================================
# 智汇云客服 · 镜像定义（**交付物**）
#
# 说明：本机开发与验收不使用 Docker（见 README §2）。
#       本文件用于交付后的容器化部署。
#
# 构建：docker build -f deploy/Dockerfile -t saaskefu:latest .
# =====================================================================
FROM python:3.11-slim

# 时区与中文环境：日志与时间戳统一按 Asia/Shanghai
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUTF8=1

# 向量库（Chroma）在部分依赖上需要构建工具；slim 镜像缺少 gcc 时预编译包通常足够，
# 这里保留 build-essential 以防个别包需要本地编译，装完即删以控制体积。
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- 依赖层（利用缓存：先装依赖再拷贝源码）----
# 使用锁定文件保证可复现构建
COPY requirements.lock.txt requirements.in ./
RUN python -m pip install --upgrade pip \
 && python -m pip install -r requirements.lock.txt

# ---- 源码 ----
COPY app ./app
COPY scripts ./scripts
COPY data/samples ./data/samples
COPY docs ./docs
COPY pytest.ini ./
COPY README.md ./

# 运行期目录（向量库与日志通过卷持久化）
RUN mkdir -p data/rag logs data/run

# 非 root 运行：容器内不必要也不应该用 root
RUN useradd --create-home --shell /bin/bash appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8011 8010 8502

# 默认启动 API 网关；compose 中按服务覆盖 command
CMD ["python", "-m", "app.main"]
