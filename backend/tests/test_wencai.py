"""T6.4 验收（CI 可测部分）：Node 校验、cookie 失效提示、HTML 改版识别、降级链切换。

真机验收：断掉 pywencai（错误 cookie）→ 系统正常运行、自动降级、告警产生（演练脚本）。
"""
import sys
import types

from app.collectors.research import AkshareReportCollector, normalize_report
from app.collectors.wencai import PywencaiCollector, _require_node
from app.models.base import SessionLocal, init_db
from app.models.system import DataSourceConfig, DataSourceLog
from app.services.collect_chain import collect_with_fallback
from app.services.source_registry import create_source


class FakeDf:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        return self._rows


def _install_fake_pywencai(monkeypatch, get_result):
    fake = types.ModuleType("pywencai")
    fake.get = lambda **kw: get_result
    monkeypatch.setitem(sys.modules, "pywencai", fake)


def _clean(db):
    db.query(DataSourceConfig).delete()
    db.query(DataSourceLog).delete()
    db.commit()


def test_require_node_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    try:
        _require_node()
        raise AssertionError("应抛出 Node.js 缺失错误")
    except RuntimeError as e:
        assert "Node.js" in str(e)


def test_wencai_cookie_invalid_returns_none(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node")
    _install_fake_pywencai(monkeypatch, None)
    try:
        PywencaiCollector().fetch(query="非st")
        raise AssertionError("应抛出 cookie 失效错误")
    except RuntimeError as e:
        assert "cookie" in str(e)


def test_wencai_html_response(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node")
    _install_fake_pywencai(monkeypatch, "<html>被拦截</html>")
    try:
        PywencaiCollector().fetch(query="非st")
        raise AssertionError("应抛出改版/拦截错误")
    except RuntimeError as e:
        assert "HTML" in str(e)


def test_wencai_success_normalizes(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node")
    _install_fake_pywencai(monkeypatch, FakeDf([{"代码": "000001", "名称": "平安银行"}]))
    records = PywencaiCollector().fetch(query="非st", cookie="ck")
    assert records == [{"代码": "000001", "名称": "平安银行"}]


def test_normalize_report():
    rec = normalize_report({"报告名称": "AI投研深度", "机构": "某券商", "评级": "买入"})
    assert rec == {"title": "AI投研深度", "org": "某券商", "rating": "买入",
                   "published_at": "", "code": ""}
    assert normalize_report({"报告名称": "  "}) is None


def test_chain_degrades_to_backup(monkeypatch):
    """人为断掉 pywencai（返回空）→ 采集链自动降级备源并留告警日志（验收口径）。"""
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node")
        _install_fake_pywencai(monkeypatch, None)  # 坏 cookie

        create_source(db, data_type="research_report", name="问财研报",
                      impl_ref="app.collectors.wencai:PywencaiReportCollector",
                      role="主源", priority=1, params_template={"query": "研报", "cookie": "bad"})
        create_source(db, data_type="research_report", name="东财研报",
                      impl_ref="tests.fake_sources:FakeOkCollector",
                      role="备用", priority=2)

        records = collect_with_fallback(db, "research_report", query="研报")
        assert records == [{"code": "000001", "date": "2026-11-03"}]  # 备源接管

        logs = db.query(DataSourceLog).order_by(DataSourceLog.id.asc()).all()
        assert [l.status for l in logs] == ["fail", "ok"]  # 主源失败留痕（告警依据）
        assert "cookie" in logs[0].message
    finally:
        _clean(db)
        db.close()
