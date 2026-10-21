"""Exact declared identities, bounded correction, and unchanged evidence judgments."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage

from artemis.agents.checker.checker import (
    CheckReport, CheckVerdict, _declared_report_model, _normalize_report, _structured_report, verdicts_allow_release,
)


TEXT = "[contract:continue_calc] 在同一子Session观察屏幕后直接将显示值乘3并按等号，屏幕实际显示600；未清空或重输旧值/算式。"
RECORD = "[contract:record_values] 依据本Role原始执行证据记下首次结果和当前结果，并更新A的Role持久notes。"


def item(text=TEXT, kind="verify"):
    return SimpleNamespace(text=text, kind=kind, when="at_end")


def verdict(text=TEXT, kind="verify", status="passed"):
    return dict(item_text=text, kind=kind, status=status, evidence="Recorded screenshot and steps.")


def provider(*responses):
    structured = MagicMock()
    structured.ainvoke = AsyncMock(side_effect=list(responses))
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm, structured


def test_schema_enumerates_only_complete_declared_texts():
    model = _declared_report_model([item(), item(RECORD), item(TEXT, "assert")])
    schema = model.model_json_schema()
    values = schema["$defs"]["DeclaredCheckVerdict"]["properties"]["item_text"]["enum"]
    assert values == [TEXT, RECORD]
    assert "(at_end) " + TEXT not in values


@pytest.mark.asyncio
@pytest.mark.parametrize("container", [dict, CheckReport])
@pytest.mark.parametrize("status", ["passed", "failed", "inconclusive"])
async def test_valid_report_first_turn_preserves_judgment_without_retry(container, status):
    response = container(verdicts=[verdict(status=status)])
    llm, structured = provider(response)
    report = await _structured_report(llm, [HumanMessage(content="Judge evidence")], [item()])
    assert report.verdicts[0].status == status
    assert structured.ainvoke.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "(at_end) " + TEXT,
    TEXT.replace("旧值/算式", "旧值/旧算式"),
    "(at_end) " + TEXT.replace("旧值/算式", "旧值/旧算式"),
    TEXT.replace("continue_calc", "wrong_id"),
    TEXT + " ",
    TEXT[:-1] + "!",
    TEXT.split("] ", 1)[1],
])
async def test_wrong_identity_requires_new_exact_response_before_acceptance(bad):
    first = CheckReport(verdicts=[verdict(text=bad)])
    llm, structured = provider(first, dict(verdicts=[verdict()]))
    report = await _structured_report(llm, [HumanMessage(content="Judge evidence")], [item()])
    assert report.verdicts[0].item_text == TEXT
    assert report.verdicts[0].status == "passed"
    assert first.verdicts[0].item_text == bad
    assert structured.ainvoke.await_count == 2
    feedback = structured.ainvoke.call_args.args[0][-1].content
    assert "only correction attempt" in feedback
    assert "No judgment is preferred" in feedback
    assert TEXT in feedback


@pytest.mark.asyncio
async def test_real_run2_paraphrase_and_display_prefix_cannot_pass_without_correction():
    # Exact observed run2 strings: first item changed 旧值/算式 to 旧值/旧算式.
    raw = dict(verdicts=[
        verdict("(at_end) " + TEXT.replace("旧值/算式", "旧值/旧算式")),
        verdict("(at_end) " + RECORD),
    ])
    llm, structured = provider(raw, raw)
    report = await _structured_report(llm, [HumanMessage(content="Actual final screenshot")], [item(), item(RECORD)])
    normalized = _normalize_report(report, [item(), item(RECORD)])
    assert [(v.item_text, v.status) for v in normalized.verdicts] == [(TEXT, "inconclusive"), (RECORD, "inconclusive")]
    assert structured.ainvoke.await_count == 2
    assert raw["verdicts"][0]["item_text"].startswith("(at_end) ")


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [
    dict(verdicts=[]),
    dict(verdicts=[verdict(kind="assert")]),
    dict(verdicts=[verdict(), verdict()]),
    dict(verdicts=[verdict(), verdict(status="failed")]),
])
async def test_coverage_wrong_kind_and_duplicates_are_rejected(first):
    llm, structured = provider(first, dict(verdicts=[verdict(status="inconclusive")]))
    report = await _structured_report(llm, [HumanMessage(content="Judge evidence")], [item()])
    assert structured.ainvoke.await_count == 2
    # A duplicate consistent passed judgment is frozen: changing it fails closed.
    assert not report.verdicts or report.verdicts[0].status in ("failed", "inconclusive")


@pytest.mark.asyncio
async def test_same_text_different_kind_requires_both_exact_pairs():
    items = [item(), item(TEXT, "assert")]
    llm, structured = provider(dict(verdicts=[verdict(), verdict(kind="assert", status="failed")]))
    report = await _structured_report(llm, [HumanMessage(content="Judge evidence")], items)
    assert [(v.kind, v.status) for v in report.verdicts] == [("verify", "passed"), ("assert", "failed")]
    assert structured.ainvoke.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", ["passed", "failed", "inconclusive"])
async def test_identity_retry_cannot_change_existing_valid_judgment(initial):
    first = dict(verdicts=[verdict(status=initial)])  # RECORD is missing.
    changed = "failed" if initial == "passed" else "passed"
    second = dict(verdicts=[verdict(status=changed), verdict(RECORD)])
    llm, structured = provider(first, second)
    report = await _structured_report(llm, [HumanMessage(content="Judge evidence")], [item(), item(RECORD)])
    if initial == "failed":
        assert report.verdicts[0].status == "failed"
        assert not verdicts_allow_release(_normalize_report(report, [item(), item(RECORD)]))
    else:
        assert report.verdicts == []
    assert structured.ainvoke.await_count == 2


@pytest.mark.asyncio
async def test_conflicting_duplicate_cannot_be_corrected_to_passed():
    llm, structured = provider(
        dict(verdicts=[verdict(), verdict(status="failed")]),
        dict(verdicts=[verdict()]),
    )
    report = await _structured_report(llm, [HumanMessage(content="Judge evidence")], [item()])
    assert report.verdicts[0].status == "failed"
    assert not verdicts_allow_release(_normalize_report(report, [item()]))
    assert structured.ainvoke.await_count == 2


@pytest.mark.asyncio
async def test_identity_correction_cannot_erase_existing_failure_evidence():
    first = dict(verdicts=[verdict(status="failed")])
    second = dict(verdicts=[dict(verdict(status="failed"), evidence=" "), verdict(RECORD)])
    llm, structured = provider(first, second)
    report = await _structured_report(llm, [HumanMessage(content="Judge evidence")], [item(), item(RECORD)])
    normalized = _normalize_report(report, [item(), item(RECORD)])
    assert normalized.verdicts[0].status == "failed"
    assert normalized.verdicts[0].evidence == "Recorded screenshot and steps."
    assert not verdicts_allow_release(normalized)
    assert structured.ainvoke.await_count == 2


@pytest.mark.asyncio
async def test_output_parser_error_allows_one_correction_without_echoing_raw_output():
    llm, structured = provider(OutputParserException("PRIVATE_PROVIDER_OUTPUT"), dict(verdicts=[verdict()]))
    report = await _structured_report(llm, [HumanMessage(content="Judge evidence")], [item()])
    assert report.verdicts[0].status == "passed"
    assert "PRIVATE_PROVIDER_OUTPUT" not in structured.ainvoke.call_args.args[0][-1].content
    assert structured.ainvoke.await_count == 2


@pytest.mark.asyncio
async def test_include_raw_parser_error_is_data_and_preserves_other_valid_failed_item():
    raw = AIMessage(content="", tool_calls=[dict(
        name="DeclaredCheckReport", id="report", args=dict(verdicts=[
            verdict(status="failed"), verdict("(at_end) " + RECORD),
        ]),
    )])
    envelope = dict(raw=raw, parsed=None, parsing_error=OutputParserException("PRIVATE_PROVIDER_OUTPUT"))
    llm, structured = provider(envelope, dict(verdicts=[verdict(), verdict(RECORD)]))
    report = await _structured_report(llm, [HumanMessage(content="Judge evidence")], [item(), item(RECORD)])
    assert llm.with_structured_output.call_args.kwargs == {"include_raw": True}
    assert structured.ainvoke.await_count == 2
    assert report.verdicts[0].status == "failed"
    assert report.verdicts[0].evidence == "Recorded screenshot and steps."
    assert not verdicts_allow_release(_normalize_report(report, [item(), item(RECORD)]))
    assert "PRIVATE_PROVIDER_OUTPUT" not in structured.ainvoke.call_args.args[0][-1].content


@pytest.mark.asyncio
async def test_include_raw_valid_first_response_has_no_correction_round():
    payload = dict(verdicts=[verdict(status="failed")])
    llm, structured = provider(dict(raw=AIMessage(content=""), parsed=CheckReport(**payload), parsing_error=None))
    report = await _structured_report(llm, [HumanMessage(content="Judge evidence")], [item()])
    assert report.verdicts[0].status == "failed"
    assert structured.ainvoke.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [TimeoutError("timeout"), asyncio.CancelledError(), RuntimeError("provider unavailable")])
async def test_cancellation_timeout_and_provider_failure_never_retry(exception):
    llm, structured = provider(exception)
    with pytest.raises(type(exception)):
        await _structured_report(llm, [HumanMessage(content="Judge evidence")], [item()])
    assert structured.ainvoke.await_count == 1


@pytest.mark.asyncio
async def test_empty_declarations_reject_invented_verdicts():
    llm, structured = provider(dict(verdicts=[verdict()]), dict(verdicts=[]))
    report = await _structured_report(llm, [HumanMessage(content="Judge evidence")], [])
    assert report.verdicts == []
    assert structured.ainvoke.await_count == 2
