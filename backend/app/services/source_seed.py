"""数据源接口初始登记（开发文档 §2.4.3 接口注册清单）。

- 应用启动时幂等补种 data_source_config（已存在的 (数据类型, 名称) 不覆盖，
  设置页的用户修改永远优先）
- 采集器实现按"模块:类"登记，业务代码不 import 具体数据源（§2.4 硬约束 4）
- 后续任务接入新数据类型时，在 INITIAL_SOURCES 追加一行即可
"""
import logging

from sqlalchemy.orm import Session

from app.models.system import DataSourceConfig
from app.services.source_registry import create_source

logger = logging.getLogger("app.source_seed")

# §2.4.3 初始登记（启用中；QPS 按清单建议值）
INITIAL_SOURCES: list[dict] = [
    {
        "data_type": "stock_info",
        "name": "akshare-东财行业概念",
        "impl_kind": "py_func",
        "impl_ref": "app.collectors.stock_info:AkshareStockInfoCollector",
        "params_template": {"include_industry": True, "include_concepts": True},
        "role": "主源",
        "priority": 1,
        "qps_limit": 0.5,
        "timeout_sec": 600,
    },
    {
        "data_type": "stock_info",
        "name": "akshare-东财列表(轻量)",
        "impl_kind": "py_func",
        "impl_ref": "app.collectors.stock_info:AkshareStockInfoCollector",
        "params_template": {"include_industry": True, "include_concepts": False},
        "role": "备用",
        "priority": 2,
        "qps_limit": 1.0,
        "timeout_sec": 300,
    },
    # ---- 日线 K 线（§2.4.3：主 2 / 备 1；mootdx 主源 + akshare 双备源）----
    {
        "data_type": "kline",
        "name": "mootdx-通达信日线",
        "impl_kind": "py_func",
        "impl_ref": "app.collectors.kline:MootdxKlineCollector",
        "params_template": {},
        "role": "主源",
        "priority": 1,
        "qps_limit": 2.0,
        "timeout_sec": 60,
    },
    {
        "data_type": "kline",
        "name": "akshare-新浪日线(含复权因子)",
        "impl_kind": "py_func",
        "impl_ref": "app.collectors.kline:AkshareSinaKlineCollector",
        "params_template": {},
        "role": "备用",
        "priority": 2,
        "qps_limit": 1.0,
        "timeout_sec": 60,
    },
    {
        "data_type": "kline",
        "name": "akshare-东财日线(hfq推导因子)",
        "impl_kind": "py_func",
        "impl_ref": "app.collectors.kline:AkshareEastsKlineCollector",
        "params_template": {},
        "role": "备用",
        "priority": 3,
        "qps_limit": 1.0,
        "timeout_sec": 60,
    },
    # ---- 财务数据（§2.4.3：akshare-东财主 / 新浪备，QPS 1）----
    {
        "data_type": "finance",
        "name": "akshare-东财三大报表",
        "impl_kind": "py_func",
        "impl_ref": "app.collectors.finance:AkshareEastsFinanceCollector",
        "params_template": {},
        "role": "主源",
        "priority": 1,
        "qps_limit": 1.0,
        "timeout_sec": 60,
    },
    {
        "data_type": "finance",
        "name": "akshare-新浪三大报表",
        "impl_kind": "py_func",
        "impl_ref": "app.collectors.finance:AkshareSinaFinanceCollector",
        "params_template": {},
        "role": "备用",
        "priority": 2,
        "qps_limit": 1.0,
        "timeout_sec": 60,
    },
    # ---- 资金流向（§2.4.3：东财口径 QPS≤1；不采北向净流入 §10.7）----
    {
        "data_type": "moneyflow",
        "name": "akshare-东财大盘资金流",
        "impl_kind": "py_func",
        "impl_ref": "app.collectors.moneyflow:AkshareEastsMarketFundFlowCollector",
        "params_template": {},
        "role": "主源",
        "priority": 1,
        "qps_limit": 1.0,
        "timeout_sec": 60,
    },
    {
        "data_type": "moneyflow",
        "name": "akshare-东财个股排行加总(兜底)",
        "impl_kind": "py_func",
        "impl_ref": "app.collectors.moneyflow:AkshareEastsFundFlowRankCollector",
        "params_template": {},
        "role": "备用",
        "priority": 2,
        "qps_limit": 1.0,
        "timeout_sec": 60,
    },
    # ---- 涨停/情绪统计（§2.4.3：东财涨跌停主 / 计算兜底备）----
    {
        "data_type": "limit_updown",
        "name": "akshare-东财涨跌停池",
        "impl_kind": "py_func",
        "impl_ref": "app.collectors.limit:AkshareEastsLimitPoolCollector",
        "params_template": {},
        "role": "主源",
        "priority": 1,
        "qps_limit": 1.0,
        "timeout_sec": 60,
    },
    {
        "data_type": "limit_updown",
        "name": "快照涨跌幅近似(计算兜底)",
        "impl_kind": "py_func",
        "impl_ref": "app.collectors.limit:AkshareSpotLimitCollector",
        "params_template": {},
        "role": "备用",
        "priority": 2,
        "qps_limit": 1.0,
        "timeout_sec": 60,
    },
    # ---- 大盘汇总（涨跌家数/情绪原始分项）----
    {
        "data_type": "market_summary",
        "name": "akshare-东财快照统计",
        "impl_kind": "py_func",
        "impl_ref": "app.collectors.market:AkshareEastsSpotSummaryCollector",
        "params_template": {},
        "role": "主源",
        "priority": 1,
        "qps_limit": 1.0,
        "timeout_sec": 60,
    },
    {
        "data_type": "market_summary",
        "name": "akshare-乐咕市场活跃度",
        "impl_kind": "py_func",
        "impl_ref": "app.collectors.market:AkshareLeguActivityCollector",
        "params_template": {},
        "role": "备用",
        "priority": 2,
        "qps_limit": 1.0,
        "timeout_sec": 60,
    },
]


def seed_data_sources(db: Session) -> int:
    """补种初始接口配置，返回本次新增条数（幂等）。"""
    inserted = 0
    for spec in INITIAL_SOURCES:
        exists = (
            db.query(DataSourceConfig)
            .filter(
                DataSourceConfig.data_type == spec["data_type"],
                DataSourceConfig.name == spec["name"],
            )
            .first()
        )
        if exists is not None:
            continue
        create_source(db, **spec)
        inserted += 1
    if inserted:
        logger.info("数据源初始登记补种 %d 条", inserted)
    return inserted
