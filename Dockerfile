# 使用官方 Python 3.11 镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 复制当前目录的文件到容器的 /app 目录
COPY . /app

# 升级 pip 并安装依赖
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# 设置环境变量（可以通过 docker-compose 文件传入）
ENV TELEGRAM_BOT_TOKEN=""

# 指定容器启动时的默认命令
CMD ["python", "bot.py"]
