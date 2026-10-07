"""受控证据校验用例；不计入五类真实供应商案例。"""
from __future__ import annotations

import copy
from uuid import uuid4

import pytest

from apps.m5_acceptance_probe import ProbeError, verify_case_evidence


RUN = {"run_id": "a" * 32, "database": "m5_acceptance_" + "a" * 32}


@pytest.mark.parametrize("case", ["C1-submit", "C2-pend_message", "C3-remember", "C4-add_evidence", "C5-report_task_progress"])
def test_expected_only_or_mock_receipts_are_never_real_acceptance(case):
    with pytest.raises(ProbeError):
        verify_case_evidence(case, {"expected": {"tool_name": case, "ok": True}, "source": "mock"}, RUN)


def remember_evidence():
    origin, operation, memory = str(uuid4()), str(uuid4()), str(uuid4())
    arguments = '{"content":"澄湾工作坊每周六上午九点开始。","type":"semantic"}'
    receipt = {"contract_version": "m5-tool-v1", "tool_name": "remember", "call_id": "call-real-provider",
               "operation_id": operation, "ok": True, "state": "accepted", "data": {}, "error": None}
    done = dict(receipt) | {"state": "done", "data": {"memory_id": memory, "write_effect": "created"}}
    action = {"command_id": operation, "actual_call_id": receipt["call_id"], "tool_name": "remember",
              "state": "done", "origin_session_id": origin, "arguments": {"content": "澄湾工作坊每周六上午九点开始。", "type": "semantic"},
              "result": done, "authorization": {"supplier_response_id": "response-real-provider", "response_id": "response-real-provider", "input_turn_id": str(uuid4())}}
    before = {"memories": [], "tasks": [], "turns": []}
    after = {"actions": [action], "memories": [{"id": memory, "type": "semantic", "content": "澄湾工作坊每周六上午九点开始。", "metadata": {}}], "tasks": [], "turns": [],
             "events": [{"event_type": "voice_input_committed", "input_turn_id": action["authorization"]["input_turn_id"]}]}
    return {"run_id": RUN["run_id"], "database": RUN["database"], "origin_session_id": origin,
            "source": "real", "cloud": {"source": "real", "request": {"type": "response.create"},
            "call": {"type": "response.function_call_arguments.done", "name": "remember", "call_id": receipt["call_id"], "response_id": "response-real-provider", "arguments": arguments}},
            "receipt": receipt, "before": before, "after": after}


def test_remember_requires_real_call_and_db_change_and_acceptance_done_boundary():
    # 此结构只测试验证器正向分支，不声称测试数据是真云事实。
    evidence = remember_evidence()
    assert verify_case_evidence("C3-remember", evidence, RUN)["passed"] is True


def test_lightweight_db_business_body_uses_its_bound_action_identity():
    evidence = remember_evidence()
    done = evidence["after"]["actions"][0]["result"]
    for key in ("contract_version", "call_id", "tool_name"):
        done.pop(key)
    assert verify_case_evidence("C3-remember", evidence, RUN)["passed"] is True


@pytest.mark.parametrize("broken", ["no_call", "fixture_call", "no_memory", "wrong_run", "not_done", "premature_done", "agent_loop", "wrong_content"])
def test_rejects_incomplete_or_unrelated_remember_facts(broken):
    evidence = copy.deepcopy(remember_evidence())
    if broken == "no_call": evidence["cloud"].pop("call")
    if broken == "fixture_call": evidence["cloud"]["call"]["call_id"] = "fixture-remember"
    if broken == "no_memory": evidence["after"]["memories"] = []
    if broken == "wrong_run": evidence["run_id"] = "b" * 32
    if broken == "not_done": evidence["after"]["actions"][0]["state"] = "accepted"
    if broken == "premature_done": evidence["receipt"] = evidence["after"]["actions"][0]["result"]
    if broken == "agent_loop": evidence["after"]["turns"] = [{"id": str(uuid4())}]
    if broken == "wrong_content": evidence["after"]["memories"][0]["content"] = "另一个事实"
    with pytest.raises(ProbeError): verify_case_evidence("C3-remember", evidence, RUN)


def test_report_without_actual_call_and_original_origin_receipt_is_rejected():
    evidence = {"run_id": RUN["run_id"], "database": RUN["database"], "source": "real", "report": {"outcome": "delivered"}, "cloud": {"source": "real"}}
    with pytest.raises(ProbeError): verify_case_evidence("C5-report_task_progress", evidence, RUN)
