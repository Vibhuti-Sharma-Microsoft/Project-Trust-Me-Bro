from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from scoring_service.models import ClaimSupport, GateVote
from test_report import _case, _raw_case, _render, _running_server, _scored_step

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    channel = os.environ.get("SCORING_SERVICE_BROWSER_CHANNEL")
    if not channel:
        pytest.skip("Set SCORING_SERVICE_BROWSER_CHANNEL=msedge (local) or chromium (CI); no browser is downloaded automatically.")
    if channel not in {"msedge", "chromium"}:
        pytest.fail("SCORING_SERVICE_BROWSER_CHANNEL must be msedge or chromium.")
    api = pytest.importorskip("playwright.sync_api", reason="Install the optional browser extra to run browser tests.")
    with api.sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(channel=channel, headless=True)
        except api.Error as error:
            if "Executable doesn't exist" in str(error) or "is not found" in str(error):
                pytest.skip(f"Requested browser {channel!r} is not installed; no automatic download: {error}")
            raise
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def report_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, browser: Any) -> Iterator[tuple[Any, str, Path]]:
    first = _raw_case("BROWSER_SENSITIVE_RAW_SENTINEL " * 2000)
    first.steps[0].freshness_reason = "No referenced documents: neutral convention, not verified freshness."
    first.steps[0].support = [ClaimSupport(claim_id="c", verdict="PARTIAL", rationale="Some material claims lack corroboration.")]
    first.steps[0].votes = {"gpt": 1, "claude": 1, "gemini": 1}
    first.judges.extend(first.judges[0].model_copy(update={"role": role}) for role in ("claude", "gemini"))
    first.documents = []
    second = _case(
        case_id="second", incident_id="incident-987", synthetic=True, score=50,
        steps=[_scored_step(values=(0.5, 0.5, 0.5, 0.5), freshness_reason="Document is within the partial freshness window.")],
        response_text="\n  Exact evaluated response\r\nwith whitespace  \r",
        response_raw="\r\n<p>Original &amp; response</p>\r",
    )
    gate = _case(
        case_id="gate", incident_id="incident-gate", status="GATE_FAILED", score=0, steps=[],
        gate_votes={"gpt": GateVote(decision="FAIL", rationale="Required investigation absent.")},
    )
    unavailable = _case(case_id="unscorable", incident_id="incident-unknown", status="UNSCORABLE", score=None, steps=[])
    attack = '</template><img src=x onerror="window.reportInjected=true"><script>window.reportInjected=true</script>'
    malicious = _raw_case("BROWSER_SENSITIVE_RAW_SENTINEL")
    malicious.case_id = "markup"
    malicious.incident_id = "<img src=x onerror=alert(1)>"
    malicious.synthetic = True
    malicious.judges[0].output["rationale"] = attack
    archived = _case(case_id="archived", incident_id="incident-archived", score=12.34, synthetic=True)
    error = _case(case_id="error", incident_id="incident-error", status="JUDGE_ERROR", score=100, synthetic=True)
    index, _ = _render(tmp_path, first, second, gate, unavailable, malicious, archived, error,
                      selected_real_cases=3, runtime_log_file="scoring-service.log")
    for name in ("results.json", "scoring-service.log"):
        (index.parent / name).write_text("BROWSER_PRIVATE_FILE_SENTINEL", encoding="utf-8")
    with _running_server(index.parent, monkeypatch) as address:
        url = f"http://{address[0]}:{address[1]}"
        context = browser.new_context(viewport={"width": 1040, "height": 900})
        errors: list[str] = []
        requests: list[str] = []
        try:
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: errors.append(message.text) if "Content Security Policy" in message.text else None)
            page.on("request", lambda request: requests.append(request.url))
            response = page.goto(url, wait_until="load")
            assert response is not None and response.status == 200
            yield page, url, index
            assert not errors, errors
            assert all(request.startswith(url + "/") for request in requests), requests
        finally:
            context.close()


def test_selecting_one_incident_updates_title_score_and_tiles(report_page: tuple[Any, str, Path]) -> None:
    page, _, _ = report_page
    host = page.locator("#scorecard-host")
    assert host.locator(".scorecard:visible").count() == 1
    assert host.locator('[data-score="total"]').inner_text() == "90.00"
    assert host.locator('[data-contribution="faithfulness"]').inner_text() == "35.00 pt"
    assert host.get_by_role("heading", name="Dimension contributions").is_visible()
    assert host.locator(".consistency, .status-note").count() == 0
    assert "before display rounding" not in host.inner_text()
    assert "not an averaged tri-score" not in host.inner_text()
    assert page.locator("#corpus-note").inner_text() == "Includes synthetic data. 3/10 real cases."
    page.get_by_label("Select incident").select_option("case-1")
    assert host.locator(".scorecard:visible").count() == 1
    assert host.locator("#incident-title").inner_text() == "Incident incident-987"
    assert page.title() == "Scoring Service | Incident incident-987"
    assert host.locator('[data-score="total"]').inner_text() == "50.00"
    assert host.locator('[data-contribution="faithfulness"]').inner_text() == "17.50 pt"
    assert "Synthetic" in host.locator(".eyebrow").first.text_content()
    assert "incident-123" not in host.inner_text()
    assert host.locator(".evaluated-response").text_content() == "\n  Exact evaluated response\r\nwith whitespace  \r"
    assert host.locator(".original-response").text_content() == "\r\n<p>Original &amp; response</p>\r"
    assert page.locator("#selection-status").inner_text() == "Incident incident-987: SCORED, score 50.00."


@pytest.mark.parametrize(
    ("dimension", "name", "calculation", "reason"),
    [
        ("faithfulness", "Faithfulness", "35 x 1 / 1", "Observed signals support the response."),
        ("coverage", "Coverage", "35 x 1 / 1", "Some material claims lack corroboration."),
        ("source_trust", "Source trust", "20 x 0.5 / 1", "No explanation recorded."),
        ("freshness", "Freshness", "10 x 1 / 1", "neutral no-document convention, not verified freshness"),
    ],
)
def test_dimension_click_shows_calculation_definition_and_reason(
    report_page: tuple[Any, str, Path], dimension: str, name: str, calculation: str, reason: str,
) -> None:
    page, _, _ = report_page
    tile = page.locator(f'#scorecard-host [data-dimension="{dimension}"]')
    tile.click()
    dialog = page.get_by_role("dialog", name=f"{name} contribution")
    assert dialog.is_visible()
    assert dialog.evaluate("element => element.matches(':modal')")
    assert dialog.locator(".definition").inner_text()
    assert calculation in dialog.locator(".calculation").inner_text()
    assert dialog.locator(".distribution").inner_text() == "1 contributing step."
    assert dialog.locator(".reasons li").count() <= 2
    for unwanted in ("0 at", "0 unavailable", "0 excluded", "0 structural", "disagree on 0"):
        assert unwanted not in dialog.inner_text()
    if dimension == "faithfulness":
        assert dialog.locator(".reasons li").count() == 1
        assert dialog.inner_text().count(reason) == 1
        assert "Each step uses the median model faithfulness vote." in dialog.inner_text()
    assert reason in dialog.inner_text()
    assert tile.get_attribute("aria-expanded") == "true"
    page.get_by_role("button", name="Close dimension details").click()
    assert not dialog.is_visible()
    assert tile.evaluate("element => element === document.activeElement")
    assert tile.get_attribute("aria-expanded") == "false"


def test_keyboard_open_escape_close_and_focus_return(report_page: tuple[Any, str, Path]) -> None:
    page, _, _ = report_page
    tile = page.locator('#scorecard-host [data-dimension="faithfulness"]')
    tile.focus()
    page.keyboard.press("Enter")
    assert page.get_by_role("dialog").is_visible()
    assert page.evaluate("document.activeElement.id") == "close-dimension"
    page.keyboard.press("Escape")
    assert not page.locator("#dimension-dialog").is_visible()
    assert tile.evaluate("element => element === document.activeElement")
    assert page.locator("#dimension-content").inner_text() == ""


def test_switch_resets_dialog_and_next_detail_uses_new_incident(report_page: tuple[Any, str, Path]) -> None:
    page, _, _ = report_page
    page.locator('#scorecard-host [data-dimension="faithfulness"]').click()
    # A programmatic selection change must reset the modal too, not retain the old incident's detail.
    page.get_by_label("Select incident").evaluate(
        "select => { select.value = 'case-1'; select.dispatchEvent(new Event('change', {bubbles: true})); }"
    )
    assert not page.locator("#dimension-dialog").is_visible()
    assert page.locator("#dimension-content").inner_text() == ""
    assert page.evaluate("document.activeElement.id") == "incident-select"
    page.locator('#scorecard-host [data-dimension="faithfulness"]').click()
    dialog = page.get_by_role("dialog")
    assert dialog.locator("#dialog-incident").text_content() == "Incident incident-987"
    assert dialog.locator(".calculation").inner_text() == "35 x 0.5 / 1 included steps = 17.50 contribution points."
    assert "Observed signals support" not in dialog.inner_text()
    page.keyboard.press("Escape")


def test_gate_failed_and_unscorable_are_distinct(report_page: tuple[Any, str, Path]) -> None:
    page, _, _ = report_page
    page.get_by_label("Select incident").select_option("case-2")
    host = page.locator("#scorecard-host")
    assert host.locator('[data-score="total"]').inner_text() == "0.00"
    assert host.locator("[data-status]").inner_text() == "GATE_FAILED"
    assert host.locator("[data-contribution]").all_inner_texts() == ["Not computed"] * 4
    host.locator('[data-dimension="coverage"]').click()
    assert "no dimension contributions were computed" in page.get_by_role("dialog").inner_text()
    assert "Required investigation absent" in page.get_by_role("dialog").inner_text()
    page.keyboard.press("Escape")
    page.get_by_label("Select incident").select_option("case-3")
    assert host.locator('[data-score="total"]').inner_text() == "Unavailable"
    assert host.locator("[data-status]").inner_text() == "UNSCORABLE"
    assert host.locator("[data-contribution]").all_inner_texts() == ["Not computed"] * 4
    assert host.locator('[data-score="total"]').inner_text() != "0.00"


def test_archived_mismatch_requires_review_without_invented_points(report_page: tuple[Any, str, Path]) -> None:
    page, _, _ = report_page
    page.get_by_label("Select incident").select_option("case-5")
    host = page.locator("#scorecard-host")
    assert host.locator('[data-score="total"]').inner_text() == "12.34"
    assert host.locator("[data-contribution]").all_inner_texts() == ["Unavailable"] * 4
    assert "Review required" in host.locator('[data-consistency="mismatch"]').inner_text()
    host.locator('[data-dimension="faithfulness"]').click()
    dialog = page.get_by_role("dialog")
    assert "Review required" in dialog.locator(".calculation").inner_text()
    assert "35 x" not in dialog.inner_text()
    assert "Recorded step notes for review" in dialog.inner_text()
    page.keyboard.press("Escape")
    page.get_by_label("Select incident").select_option("case-0")
    assert host.locator('[data-contribution="faithfulness"]').inner_text() == "35.00 pt"


def test_error_dimensions_are_explicitly_not_computed(report_page: tuple[Any, str, Path]) -> None:
    page, _, _ = report_page
    page.get_by_label("Select incident").select_option("case-6")
    host = page.locator("#scorecard-host")
    assert host.locator("[data-status]").inner_text() == "JUDGE_ERROR"
    assert host.locator('[data-score="total"]').inner_text() == "Unavailable"
    assert host.locator("[data-contribution]").all_inner_texts() == ["Not computed"] * 4
    host.locator('[data-dimension="coverage"]').click()
    assert "Final dimension contributions were not computed for JUDGE_ERROR" in page.get_by_role("dialog").inner_text()
    page.keyboard.press("Escape")


def test_detailed_payloads_remain_inert_and_backing_files_are_not_served(report_page: tuple[Any, str, Path]) -> None:
    page, url, index = report_page
    assert "BROWSER_SENSITIVE_RAW_SENTINEL" in index.read_text(encoding="utf-8")
    assert page.locator("#scorecard-host .evaluated-response").text_content() == "BROWSER_SENSITIVE_RAW_SENTINEL " * 2000
    assert page.locator("script[type='application/json']").count() == 0
    assert page.locator("#scorecard-host table").count() > 0
    assert page.locator("script").count() == 1
    assert page.locator("script").get_attribute("src") == "report.js"
    for name in ("results.json", "scoring-service.log"):
        response = page.request.get(f"{url}/{name}")
        assert response.status == 404
        assert "BROWSER_PRIVATE_FILE_SENTINEL" not in response.text()


def test_untrusted_templates_and_explanations_never_become_markup(report_page: tuple[Any, str, Path]) -> None:
    page, _, _ = report_page
    page.get_by_label("Select incident").select_option("case-4")
    assert page.locator("#incident-title").text_content() == "Incident <img src=x onerror=alert(1)>"
    page.locator('#scorecard-host [data-dimension="faithfulness"]').click()
    assert "</template><img" in page.get_by_role("dialog").inner_text()
    assert page.locator("img, [onerror]").count() == 0
    assert page.locator("script").count() == 1
    assert page.evaluate("window.reportInjected === undefined")
    page.keyboard.press("Escape")


def test_mobile_card_and_dialog_fit_viewport(report_page: tuple[Any, str, Path]) -> None:
    page, _, _ = report_page
    page.set_viewport_size({"width": 375, "height": 812})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator("#scorecard-host .scorecard:visible").count() == 1
    page.locator('#scorecard-host [data-dimension="freshness"]').click()
    box = page.get_by_role("dialog").bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= 375
    page.keyboard.press("Escape")
