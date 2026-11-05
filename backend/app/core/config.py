"""应用配置：统一从 .env / 环境变量读取（开发文档 §8.4：Key 只经环境变量注入）。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 基础
    app_name: str = "DailyStockAI"
    timezone: str = "Asia/Shanghai"
    cors_origins: list[str] = ["http://localhost:5173"]

    # 数据库（开发文档 §8.2：容器内 MySQL，不映射宿主端口）
    database_url: str = "mysql+pymysql://dsa:dsa@mysql:3306/daily_stock_ai?charset=utf8mb4"

    # 鉴权（开发文档 §3.4）
    secret_key: str = "dev-secret-change-me"
    session_max_age_days: int = 7
    login_max_fails: int = 5
    login_lock_minutes: int = 10
    admin_username: str = "admin"
    admin_initial_password: str = ""  # 留空则首次启动生成随机密码并打印到日志

    # LLM（开发文档 §5.4：仅环境变量，前端只显示尾号）
    llm_api_key: str = ""
    llm_model: str = "qwen-plus"

    # 定时任务（开发文档 §4.9，时区固定 Asia/Shanghai）
    # scheduler_enabled=False 可整体关闭进程内调度器（测试/手工补跑场景）
    scheduler_enabled: bool = True
    report_am_cron: str = "0 8 * * mon-fri"       # 盘前早报（交易日 08:00）
    report_pm_cron: str = "0 17 * * mon-fri"      # 盘后复盘（交易日 17:00）
    collect_am_cron: str = "30 7 * * *"       # 隔夜资讯（每日 07:30，非交易日仍抓）
    collect_pm_cron: str = "30 15 * * mon-fri"    # 收盘采集（交易日 15:30）
    collect_am_enabled: bool = True
    collect_pm_enabled: bool = True
    report_am_enabled: bool = True
    report_pm_enabled: bool = True

    # 失败告警 webhook（T7.6，开发文档 §1.4）：留空则失败只在日志与 task_log 留痕；
    # 填 URL 则任务重试用尽后 POST JSON 告警（3 秒超时，best-effort，不影响任务本身）
    alert_webhook: str = ""

    # 指标计算（开发文档 §5.4：TA-Lib 主引擎 / pandas 降级）
    indicator_engine: str = "talib"  # talib | pandas

    # 指标缓存（开发文档 §5.3/§5.4：模式 B，按交易日一版滚动保留）
    indicator_cache_dir: str = "data/indicator_cache"
    indicator_cache_keep_days: int = 3

    # 技术指标入库开关（开发文档 §5.4：模式 A 写库每日增量；默认关=模式 B 缓存）
    tech_indicator_store: bool = False

    # 选股扫描预算（开发文档 §4 模块4：全市场扫描 ≤20s）
    selector_scan_budget_s: float = 20.0

    # 回测任务队列（T5.8：长周期回测走后台队列，HTTP 立即返回不超时）
    backtest_max_concurrent: int = 2          # 同时在跑的回测数（超出则在队列中等待）
    backtest_queue_timeout_s: float = 1800.0  # 排队等待上限（秒），超时置 failed 留因

    # 前端产物目录（单镜像由后端托管，开发文档 §8.1）
    frontend_dist: str = "frontend/dist"


@lru_cache
def get_settings() -> Settings:
    return Settings()