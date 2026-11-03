"""T1.6 验收：股票基础信息采集（标准化合并/降级容忍/upsert/退市标记/非空率）。

akshare 不在 CI 依赖内 → 用假 akshare 模块桩测采集器（模块本身延迟导入 akshare）。
真实数据验收（入库 ≥5000 只、行业/概念非空率 >95%）由部署后采集流水线跑出统计（stats）。
"""
import sys
import types

from app.collectors.stock_info import (
    AkshareStockInfoCollector,
    market_of,
    normalize_stock_rows,
)
from app.models.base import SessionLocal, init_db
from app.models.market import StockInfo
from app.models.system import DataSourceConfig
from app.services.source_seed import INITIAL_SOURCES, seed_data_sources
from app.services.source_registry import call_source, create_source
from app.services.stock_info import upsert_stock_info


def setup_function():
    init_db()


def _clean(db):
    db.query(StockInfo).delete()
    db.query(DataSourceConfig).delete()
    db.commit()


class FakeDF:
    """假 DataFrame：采集器只用 to_dict("records")，避免 CI 依赖 pandas。"""

    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):  # noqa: ARG002 固定 records
        return [dict(r) for r in self._rows]


def make_fake_akshare(
    base_rows=None,
    industry_boards=None,
    concept_boards=None,
    fail_concept=False,
    fail_base=False,
):
    """构造假 akshare 模块。industry_boards/concept_boards: {板块名: [成分代码]}。"""
    stub = types.ModuleType("akshare")
    base_rows = base_rows if base_rows is not None else [
        {"code": "600000", "name": "浦发银行"},
        {"code": "300750", "name": "宁德时代"},
    ]

    def stock_info_a_code_name():
        if fail_base:
            raise RuntimeError("模拟基础列表接口失效")
        return FakeDF(base_rows)

    def stock_board_industry_name_em():
        return FakeDF([{"板块名称": b} for b in (industry_boards or {})])

    def stock_board_industry_cons_em(symbol):
        return FakeDF([{"代码": c} for c in industry_boards[symbol]])

    def stock_board_concept_name_em():
        return FakeDF([{"板块名称": b} for b in (concept_boards or {})])

    def stock_board_concept_cons_em(symbol):
        if fail_concept:
            raise RuntimeError("模拟概念板块接口 403")
        return FakeDF([{"代码": c} for c in concept_boards[symbol]])

    stub.stock_info_a_code_name = stock_info_a_code_name
    stub.stock_board_industry_name_em = stock_board_industry_name_em
    stub.stock_board_industry_cons_em = stock_board_industry_cons_em
    stub.stock_board_concept_name_em = stock_board_concept_name_em
    stub.stock_board_concept_cons_em = stock_board_concept_cons_em
    return stub


# ---------- 标准化 ----------

def test_market_of_by_code_prefix():
    assert market_of("600000") == "主板"
    assert market_of("688001") == "科创板"
    assert market_of("300750") == "创业板"
    assert market_of("830799") == "北交所"


def test_normalize_merges_tags_and_dedups():
    base = [
        {"code": "600000", "name": "浦发银行"},
        {"code": "600000", "name": "重复行"},  # 去重：先到先得
        {"code": "300750", "name": "宁德时代"},
        {"name": "无代码行"},  # 丢弃
    ]
    rows = normalize_stock_rows(
        base,
        industry_map={"600000": "银行"},
        concept_map={"600000": ["含H股", "跨境支付"], "300750": ["锂电池"]},
    )
    assert [r["code"] for r in rows] == ["600000", "300750"]
    assert rows[0] == {
        "code": "600000", "name": "浦发银行", "industry": "银行",
        "concepts": ["含H股", "跨境支付"], "market": "主板",
    }
    assert rows[1]["industry"] == ""  # 无行业标签 → 空（入库统计进非空率）
    assert rows[1]["concepts"] == ["锂电池"]


# ---------- 采集器（假 akshare 桩）----------

def test_fetch_full_merges_industry_and_concepts(monkeypatch):
    stub = make_fake_akshare(
        industry_boards={"银行": ["600000"]},
        concept_boards={"锂电池": ["300750"], "含H股": ["600000"]},
    )
    monkeypatch.setitem(sys.modules, "akshare", stub)
    rows = AkshareStockInfoCollector().fetch()
    by_code = {r["code"]: r for r in rows}
    assert by_code["600000"]["industry"] == "银行"
    assert by_code["600000"]["concepts"] == ["含H股"]
    assert by_code["300750"]["concepts"] == ["锂电池"]


def test_fetch_without_concepts_skips_concept_fetch(monkeypatch):
    stub = make_fake_akshare(
        industry_boards={"银行": ["600000"]},
        concept_boards={"锂电池": ["300750"]},
    )
    monkeypatch.setitem(sys.modules, "akshare", stub)
    rows = AkshareStockInfoCollector().fetch(include_concepts=False)
    assert all(r["concepts"] == [] for r in rows)
    assert rows[0]["industry"] == "银行" or rows[1]["industry"] == "银行"


def test_fetch_degrades_when_concept_board_fails(monkeypatch):
    stub = make_fake_akshare(
        industry_boards={"银行": ["600000"]},
        concept_boards={"锂电池": ["300750"]},
        fail_concept=True,
    )
    monkeypatch.setitem(sys.modules, "akshare", stub)
    rows = AkshareStockInfoCollector().fetch()  # 不抛：概念局部失败容忍
    assert len(rows) == 2
    assert all(r["concepts"] == [] for r in rows)
    assert {r["industry"] for r in rows} == {"银行", ""}  # 行业链路仍生效


def test_fetch_raises_when_base_list_fails(monkeypatch):
    stub = make_fake_akshare(fail_base=True)
    monkeypatch.setitem(sys.modules, "akshare", stub)
    try:
        AkshareStockInfoCollector().fetch()
    except RuntimeError as e:
        assert "基础列表" in str(e) or "失效" in str(e)
    else:
        raise AssertionError("基础列表失败必须抛异常（交采集链降级到备源）")


# ---------- 入库服务 ----------

def test_upsert_insert_update_and_empty_tag_guard():
    db = SessionLocal()
    try:
        _clean(db)
        stats = upsert_stock_info(db, [
            {"code": "600000", "name": "浦发银行", "industry": "银行",
             "concepts": ["含H股"], "market": "主板"},
            {"code": "600000", "name": "浦发银行", "industry": "银行",
             "concepts": ["含H股"], "market": "主板"},  # 同批次去重
        ], full_refresh=False)
        assert stats["inserted"] == 1 and stats["updated"] == 0
        assert stats["industry_rate"] == 1.0 and stats["concept_rate"] == 1.0

        # 空标签更新不覆盖已有标签（轻量备源保护）
        upsert_stock_info(db, [
            {"code": "600000", "name": "浦发银行", "industry": "",
             "concepts": [], "market": "主板"},
        ], full_refresh=False)
        row = db.get(StockInfo, "600000")
        assert row.industry == "银行" and row.concepts == ["含H股"]

        # 带标签更新生效
        upsert_stock_info(db, [
            {"code": "600000", "name": "浦发银行", "industry": "银行Ⅱ",
             "concepts": ["含H股", "跨境支付"], "market": "主板"},
        ], full_refresh=False)
        db.refresh(row)
        assert row.industry == "银行Ⅱ" and row.concepts == ["含H股", "跨境支付"]
    finally:
        db.close()


def test_upsert_full_refresh_marks_missing_delisted():
    db = SessionLocal()
    try:
        _clean(db)
        first = [
            {"code": f"{600000 + i:06d}", "name": f"股{i}", "industry": "行业A",
             "concepts": ["概念A"], "market": "主板"}
            for i in range(3100)  # ≥ MIN_FULL_ROWS 护栏
        ]
        upsert_stock_info(db, first)
        # 第二轮全量：600000/600001 退市消失，603100 新股上市
        second = first[2:] + [
            {"code": "603100", "name": "新股", "industry": "行业B",
             "concepts": [], "market": "主板"},
        ]
        stats = upsert_stock_info(db, second)
        assert stats["delisted_marked"] == 2 and not stats["delisted_skipped"]
        assert db.get(StockInfo, "600000").is_delisted is True
        assert db.get(StockInfo, "603100").is_delisted is False

        # 标的恢复出现 → is_delisted 回 False
        upsert_stock_info(db, first)
        assert db.get(StockInfo, "600000").is_delisted is False
    finally:
        db.close()


def test_upsert_guard_skips_delisted_on_small_sample():
    db = SessionLocal()
    try:
        _clean(db)
        upsert_stock_info(db, [
            {"code": "600000", "name": "浦发银行", "industry": "银行",
             "concepts": [], "market": "主板"},
        ], full_refresh=False)
        stats = upsert_stock_info(db, [
            {"code": "300750", "name": "宁德时代", "industry": "电池",
             "concepts": [], "market": "创业板"},
        ])  # 样本 1 条 < 护栏 3000：不标记退市
        assert stats["delisted_skipped"] is True and stats["delisted_marked"] == 0
        assert db.get(StockInfo, "600000").is_delisted is False

        # full_refresh=False 增量模式同样不动退市标记
        stats = upsert_stock_info(db, [], full_refresh=False)
        assert stats["delisted_marked"] == 0
    finally:
        db.close()


# ---------- 初始登记 ----------

def test_call_source_merges_params_template():
    """接口行为差异由配置模板控制（T1.6：主源带概念/备源轻量），调用方参数优先。"""
    db = SessionLocal()
    try:
        _clean(db)
        src = create_source(
            db, data_type="stock_info", name="模板源",
            impl_ref="tests.fake_sources:FakeEchoCollector",
            params_template={"include_concepts": False, "scope": "template"},
            priority=1,
        )
        records = call_source(db, src, code="000001", scope="caller")
        assert records[0]["params"] == {
            "include_concepts": False, "code": "000001", "scope": "caller",
        }
    finally:
        db.close()


def test_seed_data_sources_idempotent():
    db = SessionLocal()
    try:
        _clean(db)
        assert seed_data_sources(db) == len(INITIAL_SOURCES)
        assert seed_data_sources(db) == 0  # 幂等：二次补种不重复
        rows = db.query(DataSourceConfig).filter(
            DataSourceConfig.data_type == "stock_info"
        ).order_by(DataSourceConfig.priority.asc()).all()
        stock_sources = [s for s in INITIAL_SOURCES if s["data_type"] == "stock_info"]
        assert [r.name for r in rows] == [
            s["name"] for s in sorted(stock_sources, key=lambda s: s["priority"])
        ]
        assert rows[0].impl_ref == "app.collectors.stock_info:AkshareStockInfoCollector"
    finally:
        db.close()
