from __future__ import annotations

from pca.services import reports
from pca.services.audits import list_user_audits
from pca.services.report_pdf import render_comparison_pdf, render_report_pdf
from tests.test_audit_engine import FakeScan, _claim, _run, _setup, keyring, settings  # noqa: F401


async def _completed(sm, settings, keyring, tg_id: int, pending: int) -> tuple[int, int]:
    user_id, job_id = await _setup(sm, settings, keyring, tg_id, pending=pending)
    assert await _claim(sm, settings) == job_id
    await _run(sm, settings, keyring, FakeScan(pending), job_id)
    return user_id, job_id


async def test_report_renders_text_pdf_and_csv(sessionmaker, settings, keyring):
    user_id, job_id = await _completed(sessionmaker, settings, keyring, 9301, 35)
    async with sessionmaker() as s:
        view = await reports.load_report(s, user_id, job_id)
        assert view is not None and view.scanned == 35

        text = reports.render_text(view)
        assert "35" in text and "Chan" in text

        pdf = render_report_pdf(view)
        assert pdf.startswith(b"%PDF")

        csv_bytes = await reports.export_csv(s, view)
        assert csv_bytes is not None
        body = csv_bytes.decode("utf-8-sig")
        assert len(body.strip().splitlines()) == 36, "header plus one row per requester"
        columns = set(body.splitlines()[0].lower().split(","))
        assert not columns & {"user_id", "id", "username", "name", "first_name", "last_name", "phone"}
        assert "777" not in body, "connected account id is never exported"


async def test_report_is_owner_scoped(sessionmaker, settings, keyring):
    _, job_id = await _completed(sessionmaker, settings, keyring, 9302, 10)
    other_id, _ = await _setup(sessionmaker, settings, keyring, 9303, pending=5)
    async with sessionmaker() as s:
        assert await reports.load_report(s, other_id, job_id) is None


async def test_history_lists_own_audits(sessionmaker, settings, keyring):
    user_id, job_id = await _completed(sessionmaker, settings, keyring, 9304, 10)
    async with sessionmaker() as s:
        result = await list_user_audits(s, user_id)
        ids = [a.job_id for a in (result.items if hasattr(result, "items") else result)]
        assert job_id in ids


async def test_comparison_text_and_pdf(sessionmaker, settings, keyring):
    ua, a = await _completed(sessionmaker, settings, keyring, 9305, 20)
    ub, b = await _completed(sessionmaker, settings, keyring, 9306, 40)
    async with sessionmaker() as s:
        va = await reports.load_report(s, ua, a)
        vb = await reports.load_report(s, ub, b)
    cmp = reports.build_comparison(va, vb)
    assert cmp.rows and cmp.conclusion
    assert "40" in reports.render_comparison_text(cmp)
    assert render_comparison_pdf(cmp).startswith(b"%PDF")
