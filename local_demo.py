"""Run the durable handler locally against real Bedrock, with every layer traced to Logfire.

No Lambda function or DynamoDB table is needed: the AWS Durable Execution SDK's in-process
runner plays the Lambda durable backend (checkpoints, retries, suspension, callbacks), and the
shop's ledger is kept in memory. The model calls are real Bedrock calls.

Traces show four layers, nested:

- each invocation, with the steps it ran (the SDK's OpenTelemetry plugin in its invocation view, added in `app/handler.py`),
- the agent run, model requests and tool calls (Pydantic AI instrumentation),
- the Bedrock HTTP calls (botocore instrumentation, set up in `app/support_agent.py`),
- the work inside each tool body (`logfire.span` in `app/support_agent.py`).

Usage (needs SDK 3, the SDK's testing and OTel packages, and Bedrock credentials):

    LOGFIRE_TOKEN=... python local_demo.py happy
    LOGFIRE_TOKEN=... python local_demo.py flaky
    LOGFIRE_TOKEN=... python local_demo.py approval --wait 20
    LOGFIRE_TOKEN=... python local_demo.py approval --wait 20 --deny
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-west-2')
os.environ.setdefault('LOGFIRE_ENVIRONMENT', 'local-demo')
sys.path.insert(0, str(Path(__file__).parent / 'app'))

import shop  # noqa: E402

# An in-memory ledger instead of DynamoDB, so the demo runs without AWS resources beyond Bedrock.
_ledger: dict[str, list[dict[str, Any]]] = {}
_fault_calls: dict[tuple[str, str], int] = {}


def _record_refund(ticket_id: str, *, idempotency_key: str, order_id: str, amount: float, reason: str) -> str:
    entries = _ledger.setdefault(ticket_id, [])
    if any(e.get('idempotency_key') == idempotency_key for e in entries):
        return f'refund already issued for ${amount:.2f}'
    entries.append({'kind': 'refund', 'idempotency_key': idempotency_key, 'order_id': order_id, 'amount': amount})
    time.sleep(0.3)  # A payments call takes a moment.
    return f'refund R-{uuid.uuid4().hex[:8]} issued for ${amount:.2f}'


def _send_email(ticket_id: str, to: str, body: str) -> str:
    _ledger.setdefault(ticket_id, []).append({'kind': 'email', 'to': to})
    time.sleep(0.2)
    return f'email sent to {to}'


def _record_approval_request(
    ticket_id: str, *, tool_call_id: str, callback_id: str, order_id: str, amount: float, why: str
) -> None:
    entries = _ledger.setdefault(ticket_id, [])
    if not any(e.get('tool_call_id') == tool_call_id for e in entries):
        entries.append(
            {
                'kind': 'approval',
                'status': 'pending',
                'tool_call_id': tool_call_id,
                'callback_id': callback_id,
                'order_id': order_id,
                'amount': amount,
                'why': why,
            }
        )
    print(f'[shop] APPROVAL needed: ${amount:.2f} on {order_id} for ticket {ticket_id} ({why})', flush=True)


def _pending_approvals(ticket_id: str) -> list[dict[str, Any]]:
    return [e for e in _ledger.get(ticket_id, []) if e['kind'] == 'approval' and e['status'] == 'pending']


def _fail_first(ticket_id: str, fault: str, times: int) -> None:
    calls = _fault_calls[ticket_id, fault] = _fault_calls.get((ticket_id, fault), 0) + 1
    time.sleep(0.1)
    if calls <= times:
        raise shop.OrdersApiUnavailable(f'orders API returned 503 Service Unavailable (call {calls})')


shop.record_refund = _record_refund
shop.send_email = _send_email
shop.record_approval_request = _record_approval_request
shop.fail_first = _fail_first

import logfire  # noqa: E402
from aws_durable_execution_sdk_python_testing.runner import DurableFunctionTestRunner  # noqa: E402

# The deployed handler, unchanged: it already adds the SDK's OTel plugin when SDK 3 is installed.
from handler import handler  # noqa: E402

SCENARIOS = {
    'happy': ('A-1001', [], 'My headphones arrived with a cracked headband.'),
    'flaky': ('A-1001', ['flaky_orders'], 'My headphones arrived with a cracked headband.'),
    'approval': ('A-2002', [], 'The espresso machine arrived with a broken water tank.'),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('scenario', choices=SCENARIOS)
    parser.add_argument('--wait', type=float, default=20, help='seconds the manager takes to answer (approval only)')
    parser.add_argument('--deny', action='store_true', help='deny the refund instead of approving it (approval only)')
    args = parser.parse_args()

    order_id, faults, message = SCENARIOS[args.scenario]
    ticket_id = f'T-{args.scenario}-{uuid.uuid4().hex[:6]}'
    event = json.dumps({'ticket_id': ticket_id, 'order_id': order_id, 'message': message, 'faults': faults})
    print(f'ticket {ticket_id}')

    with DurableFunctionTestRunner(handler=handler) as runner:
        arn = runner.run_async(input=event, execution_timeout=600)
        if args.scenario == 'approval':
            runner.wait_for_callback(arn, timeout=300)
            # The agent's `ask_a_manager` records the approval in a step just after the callback is created.
            deadline = time.monotonic() + 60
            while not (pending := _pending_approvals(ticket_id)):
                if time.monotonic() > deadline:
                    sys.exit(f'No approval was recorded. ledger: {_ledger.get(ticket_id, [])}')
                time.sleep(0.2)
            print(f'waiting for a manager ({args.wait:g}s); nothing runs meanwhile. ledger: {_ledger.get(ticket_id, [])}')
            time.sleep(args.wait)
            # The answer is `DeferredToolResults` as JSON, keyed by tool call ID, as `demo.py approve` sends it.
            answer = {'kind': 'tool-denied', 'message': 'Ask the customer for a photo first.'} if args.deny else True
            decision = {
                'approvals': {a['tool_call_id']: answer for a in pending},
                'answered_by': {a['tool_call_id']: 'local-manager' for a in pending},
            }
            for approval in pending:
                approval['status'] = 'denied' if args.deny else 'approved'
            print(f'sending {json.dumps(decision)}')
            runner.send_callback_success(pending[0]['callback_id'], json.dumps(decision).encode())
        result = runner.wait_for_result(arn, timeout=600)

    print(f'status: {result.status.name}')
    print(f'result: {result.result}')
    print(f'operations: {[op.name for op in result.operations]}')
    print(f'ledger: {_ledger.get(ticket_id, [])}')
    logfire.force_flush()


if __name__ == '__main__':
    main()
