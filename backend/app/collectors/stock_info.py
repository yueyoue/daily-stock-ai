"""股票基础信息采集（开发文档 §3.1 / §4 模块1，任务 T1.6）。

数据范围：全市场个股代码/名称 + 行业 + 概念标签（377+ 概念板块），日更。
初始主源：akshare-东财行业概念（数据源登记见 services/source_seed.py）。

设计要点：
- akshare 延迟到 fetch 内导入（CI 不装数据栈依赖，模块可被安全 import）；
- 基础列表必须成功（失败抛异常交采集链降级）；行业/概念板块局部失败**降级容忍**，
  保住已有标签继续出数（板块接口 403/改版是常态，见 AI 代理提示）；
- 标准化输出走 normalize_stock_rows 纯函数（可脱离 pandas 单测）。
"""
import logging

from app.collectors.base import BaseCollector

logger = logging.getLogger("app.collectors.stock_info")

# 板块成分列名兼容（东财接口列名偶有变动，多别名兜底）
_CODE_KEYS = ("代码", "code", "证券代码")
_BOARD_KEYS = ("板块名称", "板块", "name")


def _pick(row: dict, keys: tuple) -> str:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _records(df) -> list[dict]:
    """DataFrame → list[dict]（不依赖 pandas 类型，测试可注入假 DataFrame）。"""
    return [dict(r) for r in df.to_dict("records")]


def market_of(code: str) -> str:
    """按代码前缀推断上市板（§6 stock_info.market：主板/创业板/科创板/北交所）。"""
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301", "302")):
        return "创业板"
    if code.startswith(("4", "8", "92")):
        return "北交所"
    return "主板"


def normalize_stock_rows(
    base_rows: list[dict],
    industry_map: dict[str, str] | None = None,
    concept_map: dict[str, list[str]] | None = None,
) -> list[dict]:
    """合并基础列表 + 行业 + 概念 → 标准化 stock_info 记录（按代码去重，先到先得）。

    记录字段：code/name/industry/concepts/market（is_delisted 由入库服务维护）。
    """
    industry_map = industry_map or {}
    concept_map = concept_map or {}
    seen: set[str] = set()
    out: list[dict] = []
    for row in base_rows:
        code = _pick(row, _CODE_KEYS) or str(row.get("code", "")).strip()
        if not code:
            continue
        name = str(row.get("name") or _pick(row, ("名称", "证券简称"))).strip()
        if code in seen:
            continue
        seen.add(code)
        out.append(
            {
                "code": code,
                "name": name,
                "industry": industry_map.get(code, ""),
                "concepts": sorted(set(concept_map.get(code, []))),
                "market": market_of(code),
            }
        )
    return out


class AkshareStockInfoCollector(BaseCollector):
    """akshare-东财：全市场股票列表 + 行业 + 概念标签。

    params_template 可配：
    - include_industry: 是否采集行业板块（默认 True）
    - include_concepts: 是否采集概念板块（默认 True，377+ 板块耗时较长，可关）
    """

    data_type = "stock_info"

    def fetch(self, **params) -> list[dict]:
        import akshare as ak  # 延迟导入：数据栈依赖只在镜像内安装

        include_industry = bool(params.get("include_industry", True))
        include_concepts = bool(params.get("include_concepts", True))

        base_rows = self._fetch_base(ak)  # 主体数据，失败必须抛（交采集链降级）
        industry_map: dict[str, str] = {}
        concept_map: dict[str, list[str]] = {}
        if include_industry:
            industry_map = self._tolerant(self._fetch_industry_map, ak, "行业板块")
        if include_concepts:
            concept_map = self._tolerant(self._fetch_concept_map, ak, "概念板块")
        return normalize_stock_rows(base_rows, industry_map, concept_map)

    @staticmethod
    def _tolerant(fn, ak, label: str):
        """板块类采集局部失败容忍：告警并降级为空标签，不拖垮整体采集。"""
        try:
            return fn(ak)
        except Exception as e:  # noqa: BLE001 采集侧异常类型不可枚举
            logger.warning("%s采集失败，降级为无该类标签继续出数：%s", label, e)
            return {}

    @staticmethod
    def _fetch_base(ak) -> list[dict]:
        """全市场代码+名称（含沪深北交所）。"""
        df = ak.stock_info_a_code_name()
        rows = _records(df)
        if not rows:
            raise RuntimeError("stock_info_a_code_name 返回空")
        return rows

    @staticmethod
    def _fetch_industry_map(ak) -> dict[str, str]:
        """行业板块成分 → {code: 行业名}（东财行业分类，约 90 个板块）。"""
        boards = _records(ak.stock_board_industry_name_em())
        mapping: dict[str, str] = {}
        for b in boards:
            board = _pick(b, _BOARD_KEYS)
            if not board:
                continue
            for row in _records(ak.stock_board_industry_cons_em(symbol=board)):
                code = _pick(row, _CODE_KEYS)
                if code and code not in mapping:
                    mapping[code] = board
        return mapping

    @staticmethod
    def _fetch_concept_map(ak) -> dict[str, list[str]]:
        """概念板块成分 → {code: [概念名, ...]}（东财概念分类，377+ 个板块）。"""
        boards = _records(ak.stock_board_concept_name_em())
        mapping: dict[str, list[str]] = {}
        for b in boards:
            board = _pick(b, _BOARD_KEYS)
            if not board:
                continue
            for row in _records(ak.stock_board_concept_cons_em(symbol=board)):
                code = _pick(row, _CODE_KEYS)
                if code:
                    mapping.setdefault(code, []).append(board)
        return mapping
