"""DailyStockAI 后端入口（T0.1 空骨架，T0.2 起完善）。"""
from fastapi import FastAPI

app = FastAPI(title="DailyStockAI", version="0.1.0")


@app.get("/")
def root():
    return {"name": "DailyStockAI", "status": "skeleton"}
