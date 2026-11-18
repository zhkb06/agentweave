FROM python:3.12-slim

# 纯标准库应用：无需 pip install；镜像即源码 + Python 运行时
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    AGENTWEAVE_LOG_LEVEL=INFO

WORKDIR /app
COPY . .

# 运行数据与日志目录（建议挂载持久卷）
RUN mkdir -p /app/data/runs /app/data/logs

EXPOSE 8765

# 生产容器不自动打开浏览器；鉴权与限流通过 config/platform.json 或环境变量配置
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8765"]
