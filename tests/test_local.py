"""Run the durable handler locally, without AWS, using the SDK's in-process test runner.

The model is scripted with `FunctionModel`, and the shop backend is replaced by in-memory fakes,
so these tests make no network calls.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-west-2')
sys.path.insert(0, str(Path(__file__).parents[1] / 'app'))

import handler as handler_module  # noqa: E402
import shop  # noqa: E402
from aws_durable_execution_sdk_python.execution import InvocationStatus  # noqa: E402
from aws_durable_execution_sdk_python_testing.runner import DurableFunctionTestRunner  # noqa: E402
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402
from support_agent import build_agent  # noqa: E402

handler = handler_module.handler


def scripted_model(order_id: str, amount: float) -> FunctionModel:
    """A model that looks up the order, refunds it, emails the customer, then answers."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:  # noqa: ARG001
        done = {
            part.tool_name
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        }
        if 'lookup_order' not in done:
            return ModelResponse(parts=[ToolCallPart('lookup_order', {'order_id': order_id})])
        if 'issue_refund' not in done:
            args = {'order_id': order_id, 'amount': amount, 'reason': 'arrived damaged'}
            return ModelResponse(parts=[ToolCallPart('issue_refund', args)])
        if 'email_customer' not in done:
            return ModelResponse(parts=[ToolCallPart('email_customer', {'to': 'ana@example.com', 'body': 'Refunded.'})])
        answer = {'decision': 'refunded', 'refund_amount': amount, 'customer_reply': 'Refunded.'}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, answer)])

    return FunctionModel(respond)


@pytest.fixture
def fake_shop(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {'refunds': {}, 'emails': [], 'calls': {}, 'approvals': {}}

    def record_refund(ticket_id: str, *, idempotency_key: str, order_id: str, amount: float, reason: str) -> str:
        state['refunds'].setdefault(idempotency_key, amount)
        return f'refund issued for ${amount:.2f}'

    def send_email(ticket_id: str, to: str, body: str) -> str:
        state['emails'].append(to)
        return f'email sent to {to}'

    def record_approval_request(
        ticket_id: str, *, tool_call_id: str, callback_id: str, order_id: str, amount: float, why: str
    ) -> None:
        state['approvals'][tool_call_id] = {'callback_id': callback_id, 'order_id': order_id, 'amount': amount}

    def fail_first(ticket_id: str, fault: str, times: int) -> None:
        state['calls'][fault] = state['calls'].get(fault, 0) + 1
        if state['calls'][fault] <= times:
            raise shop.OrdersApiUnavailable('orders API returned 503')

    monkeypatch.setattr(shop, 'record_refund', record_refund)
    monkeypatch.setattr(shop, 'send_email', send_email)
    monkeypatch.setattr(shop, 'record_approval_request', record_approval_request)
    monkeypatch.setattr(shop, 'fail_first', fail_first)
    return state


def ticket(order_id: str, *faults: str) -> str:
    event = {'ticket_id': 'T-local', 'order_id': order_id, 'message': 'It arrived broken.', 'faults': list(faults)}
    return json.dumps(event)


def step_names(result: Any) -> list[str]:
    return [op.name for op in result.operations]


def test_every_model_request_and_tool_call_is_a_step(
    fake_shop: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler_module, 'agent', build_agent(scripted_model('A-1001', 79.0)))
    with DurableFunctionTestRunner(handler=handler) as runner:
        result = runner.run(input=ticket('A-1001'), execution_timeout=60)

    assert result.status is InvocationStatus.SUCCEEDED
    assert json.loads(result.result)['decision'] == 'refunded'
    names = step_names(result)
    assert 'support__function_toolset__shop.call_tool:issue_refund' in names
    assert sum(name.startswith('support__model.request') for name in names) == 4
    assert len(fake_shop['refunds']) == 1


def test_a_flaky_tool_is_retried_by_the_step(fake_shop: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_module, 'agent', build_agent(scripted_model('A-1001', 79.0)))
    with DurableFunctionTestRunner(handler=handler) as runner:
        result = runner.run(input=ticket('A-1001', 'flaky_orders'), execution_timeout=120)

    assert result.status is InvocationStatus.SUCCEEDED
    assert fake_shop['calls']['orders_api'] == 3  # Two 503s, then success.
    lookup = result.get_step('support__function_toolset__shop.call_tool:lookup_order')
    assert lookup.attempt == 3


@pytest.mark.parametrize('approved', [True, False])
def test_a_large_refund_waits_for_a_manager(
    fake_shop: dict[str, Any], monkeypatch: pytest.MonkeyPatch, approved: bool
) -> None:
    monkeypatch.setattr(handler_module, 'agent', build_agent(scripted_model('A-2002', 249.0)))
    with DurableFunctionTestRunner(handler=handler) as runner:
        arn = runner.run_async(input=ticket('A-2002'), execution_timeout=120)
        callback_id = runner.wait_for_callback(arn, timeout=60)
        # `ask_a_manager` records the approval in a step just after the callback is created.
        deadline = time.monotonic() + 30
        while not fake_shop['approvals'] and time.monotonic() < deadline:
            time.sleep(0.1)
        [(tool_call_id, approval)] = fake_shop['approvals'].items()
        assert approval == {'callback_id': callback_id, 'order_id': 'A-2002', 'amount': 249.0}
        assert fake_shop['refunds'] == {}  # Nothing is refunded while the execution waits.

        answer = True if approved else {'kind': 'tool-denied', 'message': 'Too expensive.'}
        decision = {'approvals': {tool_call_id: answer}, 'answered_by': {tool_call_id: 'manager'}}
        runner.send_callback_success(callback_id, json.dumps(decision).encode())
        result = runner.wait_for_result(arn, timeout=60)

    assert result.status is InvocationStatus.SUCCEEDED
    assert len(fake_shop['refunds']) == (1 if approved else 0)
    names = step_names(result)
    assert 'support__external_event.deferred_tool_calls' in names
    assert 'support__external_event.deferred_tool_calls.ask' in names
