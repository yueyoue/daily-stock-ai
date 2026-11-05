"""K 线采集（开发文档 §5.2 / §4 模块5，任务 T1.7）。

- 存**不复权 OHLCV + 复权因子 adj_factor**（后复权 = 不复权价 × adj_factor，T5.5）
- 主源 mootdx（通达信）：分页拉日线 + 由除权除息记录推累计后复权因子
- 备源 akshare-新浪（stock_zh_a_daily，含 hfq-factor 直接因子）/ akshare-东财
  （stock_zh_a_hist，因子按 hfq_close/raw_close 推导）；接口登记见 services/source_seed.py
- 保留退市标的由 stock_info 退市标记 + 上市日起采负责（幸存者偏差防线）

口径约定（T1.11 抽查对齐验证）：
- volume 统一为**股**：mootdx vol 与东财"成交量"以手计，×100；新浪 volume 本身为股
- amount 统一为**元**；新浪源无成交额字段，置 0
- adj_factor 为**累计后复权因子**（上市以来 Π 除权除息调整比）

⚠️ 缺陷 D-01 同类加固：价格/量额/复权因子一律走 safe_float，NaN/±Inf 不得进入
stock_kline（MySQL 数值列会拒绝），价格缺失的行直接丢弃（宁缺毋错）。
"""
import logging
from datetime import date

from app.collectors.base import BaseCollector
from app.collectors.util import pick, safe_float, to_date, to_records

logger = logging.getLogger("app.collectors.kline")

_DATE_KEYS = ("date", "日期", "datetime")
_OPEN_KEYS = ("open", "开盘")
_HIGH_KEYS = ("high", "最高")
_LOW_KEYS = ("low", "最低")
_CLOSE_KEYS = ("close", "收盘")
_VOL_KEYS = ("volume", "vol", "成交量")
_AMT_KEYS = ("amount", "成交额", "成交金额")


def normalize_kline_rows(
    raw_rows: list[dict],
    code: str,
    adj_factors: dict[str, float] | None = None,
    volume_scale: float = 1.0,
) -> list[dict]:
    """标准化 K 线记录（按日期升序去重，先到先得）。

    adj_factors: {YYYY-MM-DD: 累计后复权因子}，缺失日期回落 1.0；
    volume_scale: 源单位 → 股 的倍率（手→股 传 100）。
    """
    adj_factors = adj_factors or {}
    seen: set[date] = set()
    out: list[dict] = []
    for row in raw_rows:
        d = to_date(pick(row, _DATE_KEYS)) or to_date(row.get("date"))
        if d is None or d in seen:
            continue
        open_ = safe_float(pick(row, _OPEN_KEYS))
        high = safe_float(pick(row, _HIGH_KEYS))
        low = safe_float(pick(row, _LOW_KEYS))
        close = safe_float(pick(row, _CLOSE_KEYS))
        if None in (open_, high, low, close):
            continue  # 价格字段缺失/非法/NaN → 丢弃该行
        seen.add(d)
        volume = safe_float(pick(row, _VOL_KEYS), 0.0) * volume_scale
        amount = safe_float(pick(row, _AMT_KEYS), 0.0)
        out.append(
            {
                "code": code,
                "date": d,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "adj_factor": safe_float(adj_factors.get(d.isoformat()), 1.0),
            }
        )
    out.sort(key=lambda r: r["date"])
    return out


def xdxr_to_adj_factors(
    xdxr_rows: list[dict],
    bars_rows: list[dict],
    known_closes: dict[str, float] | None = None,
) -> dict[str, float]:
    """由通达信除权除息记录（category=1）推累计后复权因子。

    后复权因子递推（每 10 股口径）：
      每股派息 cash = fenhong/10；每股配股比 = peigu/10；每股送转比 = songzhuangu/10
      除权参考价 refer = (前收盘 - cash + 配股比×配股价) / (1 + 送转比 + 配股比)
      调整比 step = 前收盘 / refer（除权日起因子 × step，保证后复权价连续）
    前收盘取事件前一交易日收盘（bars 优先，known_closes 兜底——增量小窗口场景）；
    算不出前收盘的事件跳过并告警（宁缺毋错）。
    """
    closes: dict[str, float] = {}
    for row in bars_rows:
        d = to_date(pick(row, _DATE_KEYS)) or to_date(row.get("date"))
        c = safe_float(pick(row, _CLOSE_KEYS))
        if d is not None and c is not None:
            closes[d.isoformat()] = c
    closes.update(known_closes or {})

    events: list[tuple[date, float]] = []
    for row in xdxr_rows:
        if int(row.get("category", 0) or 0) != 1:
            continue
        y, m, dd = row.get("year"), row.get("month"), row.get("day")
        if None in (y, m, dd):
            continue
        event_date = to_date(f"{int(y):04d}-{int(m):02d}-{int(dd):02d}")
        fenhong = safe_float(row.get("fenhong"), 0.0)
        peigu = safe_float(row.get("peigu"), 0.0)
        peigujia = safe_float(row.get("peigujia"), 0.0)
        songzhuangu = safe_float(row.get("songzhuangu"), 0.0)
        if event_date is None or (fenhong == 0 and peigu == 0 and songzhuangu == 0):
            continue

        prev_close = None
        prior_days = sorted(d for d in closes if d < event_date.isoformat())
        if prior_days:
            prev_close = closes[prior_days[-1]]
        if prev_close is None or prev_close <= 0:
            logger.warning("除权事件 %s 算不出前收盘，跳过该事件（因子可能偏低）", event_date)
            continue

        cash = fenhong / 10.0
        peigu_ratio = peigu / 10.0
        song_ratio = songzhuangu / 10.0
        refer = (prev_close - cash + peigu_ratio * peigujia) / (1 + song_ratio + peigu_ratio)
        if refer <= 0:
            logger.warning("除权事件 %s 参考价 %s 非法，跳过", event_date, refer)
            continue
        events.append((event_date, prev_close / refer))

    events.sort(key=lambda e: e[0])
    factors: dict[str, float] = {}
    factor = 1.0
    idx = 0
    for day in sorted(closes):
        while idx < len(events) and events[idx][0].isoformat() <= day:
            factor *= events[idx][1]
            idx += 1
        factors[day] = factor
    return factors


class MootdxKlineCollector(BaseCollector):
    """mootdx（通达信）日线主源：分页拉不复权 K 线 + xdxr 推复权因子。"""

    data_type = "kline"
    PAGE = 800  # TDX 单次上限

    def fetch(self, **params) -> list[dict]:
        from mootdx.quotes import Quotes  # 延迟导入：数据栈依赖只在镜像内安装

        code = str(params.get("code", "")).strip()
        if not code:
            raise ValueError("kline 采集缺少 code 参数")
        start_date = to_date(params.get("start_date"))
        end_date = to_date(params.get("end_date")) or date.today()

        client = Quotes.factory(market="std", quiet=True)
        try:
            bars = self._fetch_bars(client, code, start_date, end_date)
            xdxr_rows = to_records(client.xdxr(symbol=code))
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001 连接关闭失败不影响数据
                pass

        factors = xdxr_to_adj_factors(
            xdxr_rows, bars, known_closes=params.get("known_closes")
        )
        return normalize_kline_rows(bars, code, adj_factors=factors, volume_scale=100.0)

    def _fetch_bars(self, client, code: str, start_date: date | None, end_date: date) -> list[dict]:
        """从最新往回分页（start 为距最新偏移），翻到早于 start_date 或翻完为止。"""
        rows: list[dict] = []
        start_idx = 0
        for _ in range(60):  # 60×800=48000 根，远超 20 年日线
            page = to_records(client.bars(symbol=code, frequency=9, start=start_idx, offset=self.PAGE))
            if not page:
                break
            rows.extend(page)
            page_dates = [
                d for d in (to_date(pick(r, _DATE_KEYS)) or to_date(r.get("date")) for r in page) if d
            ]
            oldest = min(page_dates) if page_dates else None
            if len(page) < self.PAGE:
                break  # 翻到底了
            if start_date and oldest and oldest < start_date:
                break
            start_idx += self.PAGE
        return rows


class AkshareSinaKlineCollector(BaseCollector):
    """akshare-新浪（stock_zh_a_daily）备源：不复权 K 线 + hfq-factor 直接因子。"""

    data_type = "kline"

    def fetch(self, **params) -> list[dict]:
        import akshare as ak

        code = str(params.get("code", "")).strip()
        if not code:
            raise ValueError("kline 采集缺少 code 参数")
        start_date = to_date(params.get("start_date"))
        end_date = to_date(params.get("end_date")) or date.today()
        symbol = self._sina_symbol(code)

        raw = to_records(
            ak.stock_zh_a_daily(
                symbol=symbol,
                start_date=start_date.strftime("%Y%m%d") if start_date else "19900101",
                end_date=end_date.strftime("%Y%m%d"),
                adjust="",
            )
        )
        factor_rows = to_records(ak.stock_zh_a_daily(symbol=symbol, adjust="hfq-factor"))
        factors = {}
        for r in factor_rows:
            d = to_date(r.get("date"))
            f = safe_float(r.get("hfq_factor"))
            if d is not None and f is not None:
                factors[d.isoformat()] = f
        return normalize_kline_rows(raw, code, adj_factors=factors, volume_scale=1.0)

    @staticmethod
    def _sina_symbol(code: str) -> str:
        if code.startswith("6"):
            return f"sh{code}"
        if code.startswith(("0", "3")):
            return f"sz{code}"
        return f"bj{code}"  # 北交所；新浪源不支持时由采集链降级备源


class AkshareEastsKlineCollector(BaseCollector):
    """akshare-东财（stock_zh_a_hist）备源：不复权 K 线 + hfq/raw 推导复权因子。"""

    data_type = "kline"

    def fetch(self, **params) -> list[dict]:
        import akshare as ak

        code = str(params.get("code", "")).strip()
        if not code:
            raise ValueError("kline 采集缺少 code 参数")
        start_date = to_date(params.get("start_date"))
        end_date = to_date(params.get("end_date")) or date.today()
        kw = dict(
            symbol=code,
            period="daily",
            start_date=start_date.strftime("%Y%m%d") if start_date else "19900101",
            end_date=end_date.strftime("%Y%m%d"),
        )
        raw = to_records(ak.stock_zh_a_hist(adjust="", **kw))
        hfq = to_records(ak.stock_zh_a_hist(adjust="hfq", **kw))

        factors: dict[str, float] = {}
        raw_close = {}
        for r in raw:
            d = to_date(pick(r, _DATE_KEYS))
            if d is not None:
                raw_close[d.isoformat()] = safe_float(pick(r, _CLOSE_KEYS), 0.0)
        for r in hfq:
            d = to_date(pick(r, _DATE_KEYS))
            if d is None:
                continue
            base = raw_close.get(d.isoformat(), 0)
            close = safe_float(pick(r, _CLOSE_KEYS))
            if base and base > 0 and close is not None:
                factors[d.isoformat()] = close / base
        return normalize_kline_rows(raw, code, adj_factors=factors, volume_scale=100.0)