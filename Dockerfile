FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# 注意：容器内不带 Playwright，"浏览器登录自动获取 Cookie"功能请在宿主机使用；
# 容器场景下直接把 Cookie 粘贴进页面即可。

EXPOSE 8765

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
