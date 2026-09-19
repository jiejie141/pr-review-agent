# pr-review-agent 容器镜像。
#
# 这个项目**运行时零第三方依赖**（全 stdlib），所以镜像可以做得非常小：
# 直接在 python:slim 上放源码即可，不需要 pip install 任何东西。
# 这是"零依赖设计"带来的一个具体收益，值得在 Dockerfile 里显式体现。
FROM python:3.12-slim

# 不写 .pyc、不缓冲 stdout（日志要能实时看到）、pip 不留缓存
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# 先只拷依赖声明。本项目没有运行时依赖，这一步几乎瞬间完成；
# 保留这个分层是为了以后万一加了依赖，缓存机制仍然生效。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt || true

# 再拷源码
COPY src/ ./src/
COPY main.py ./
COPY examples/ ./examples/

# 非 root 运行。容器里的进程不该有 root 权限 —— 尤其这个服务会读取
# 用户提交的 diff 并可能回写 PR，权限越小越好。
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

# CI 里默认走离线替身：不联网、不烧 token、结果可复现
ENV MOCK=true \
    OFFLINE=true \
    DRY_RUN=true

ENTRYPOINT ["python", "main.py"]
CMD ["--help"]
