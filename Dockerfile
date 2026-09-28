FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# 容器内不含 Playwright："浏览器登录获取 Cookie"请在宿主机使用，
# 或直接把 Cookie 粘贴进页面。数据持久化依赖 volume 挂载 /app/data。

EXPOSE 8765

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
