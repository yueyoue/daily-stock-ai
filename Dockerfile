# ============================================================
# DailyStockAI 单镜像（开发文档 §8.1）：
#   阶段1 构建前端产物
#   阶段2 运行层 = Python 后端（托管前端）+ Node.js≥18（pywencai 依赖）+ TA-Lib C 库
# 构建：GitHub Actions（.github/workflows/build-image.yml）
# ============================================================

# ---- 阶段1：前端构建 ----
FROM node:20-bookworm-slim AS frontend
WORKDIR /fe
COPY frontend/package.json ./
RUN npm install
COPY frontend/ .
RUN npm run build

# ---- 阶段2：运行层 ----
FROM python:3.11-slim-bookworm

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FRONTEND_DIST=frontend/dist

# 系统依赖：TA-Lib C 库编译 + Node.js（pywencai 需要 ≥18）+ curl（健康检查）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential wget curl ca-certificates gnupg \
    && wget -q http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz \
    && tar -xzf ta-lib-0.4.0-src.tar.gz \
    && cd ta-lib \
    && ./configure --prefix=/usr >/dev/null \
    && make >/dev/null \
    && make install >/dev/null \
    && cd .. && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && ldconfig

WORKDIR /app

# Python 依赖：核心 + 数据/LLM 重依赖（TA-Lib wrapper 在 C 库之上安装）
COPY backend/requirements.txt backend/requirements-data.txt ./
RUN pip install -r requirements.txt -r requirements-data.txt

# 应用代码 + 前端产物
COPY backend/ /app/
COPY --from=frontend /fe/dist /app/frontend/dist

# 非 root 运行（开发文档 §8.4）
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
