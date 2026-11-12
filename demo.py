#!/usr/bin/env python3
"""Drive the live demo.

demo.py start  happy|crash|flaky|approval   start a durable execution and watch it
demo.py watch  TICKET                        print an execution's history as it happens
demo.py approve TICKET [--deny [--note ..]]  answer a pending approval callback
demo.py ledger TICKET                        refunds, emails and approvals the ticket produced
demo.py plain  crash                         the same crash on an ordinary Lambda function, run twice
demo.py list                                 recent durable executions
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

REGION = os.environ.get('AWS_REGION', 'us-west-2')
DURABLE_FN = os.environ.get('DURABLE_FN', 'support-agent:live')
PLAIN_FN = os.environ.get('PLAIN_FN', 'support-agent-plain')
TABLE = os.environ.get('DEMO_TABLE', 'pydantic-durable-demo')

lam = boto3.client('lambda', region_name=REGION)
table = boto3.resource('dynamodb', region_name=REGION).Table(TABLE)

BOLD, DIM, RED, GREEN, YELLOW, CYAN, RESET = (
    '\033[1m',
    '\033[2m',
    '\033[31m',
    '\033[32m',
    '\033[33m',
    '\033[36m',
    '\033[0m',
)

SCENARIOS: dict[str, dict[str, Any]] = {
    'happy': {'order_id': 'A-1001', 'message': 'My headphones arrived with a cracked headband. Can I get a refund?'},
    'crash': {
        'order_id': 'A-1001',
        'message': 'My headphones arrived with a cracked headband. Can I get a refund?',
        'faults': ['crash_before_email'],
    },
    'flaky': {
        'order_id': 'A-1001',
        'message': 'My headphones arrived with a cracked headband. Can I get a refund?',
        'faults': ['flaky_orders'],
    },
    'approval': {
        'order_id': 'A-2002',
        'message': 'The espresso machine I got leaks water everywhere. I want a refund.',
    },
}


def new_ticket_id(scenario: str) -> str:
    return f'T-{scenario}-{uuid.uuid4().hex[:6]}'


def execution_arn(ticket_id: str) -> str:
    function_name = DURABLE_FN.split(':')[0]
    for _ in range(20):
        found = lam.list_durable_executions_by_function(FunctionName=function_name, DurableExecutionName=ticket_id)
        if found['DurableExecutions']:
            return found['DurableExecutions'][0]['DurableExecutionArn']
        time.sleep(0.5)
    sys.exit(f'No durable execution named {ticket_id}')


def history(arn: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {'DurableExecutionArn': arn, 'IncludeExecutionData': True}
    while True:
        page = lam.get_durable_execution_history(**kwargs)
        events += page['Events']
        if not page.get('NextMarker'):
            return events
        kwargs['Marker'] = page['NextMarker']


def _payload(details: dict[str, Any] | None, key: str) -> Any:
    return ((details or {}).get(key) or {}).get('Payload')


def describe(event: dict[str, Any], invocation: int) -> str:
    kind, name = event['EventType'], event.get('Name') or ''
    stamp = event['EventTimestamp'].astimezone().strftime('%H:%M:%S')
    line = f'{DIM}{stamp}{RESET}  {kind:<20} {name}'
    if kind == 'StepFailed':
        details = event['StepFailedDetails']
        error = _payload(details, 'Error') or {}
        retry = details.get('RetryDetails') or {}
        delay = retry.get('NextAttemptDelaySeconds')
        line = f'{RED}{line}{RESET}  {error.get("ErrorType")}: {str(error.get("ErrorMessage"))[:70]}'
        if delay:
            line += f'{YELLOW}  -> attempt {retry.get("CurrentAttempt", 0) + 1} in {delay}s, Lambda not running{RESET}'
    elif kind == 'StepSucceeded':
        attempt = (event['StepSucceededDetails'].get('RetryDetails') or {}).get('CurrentAttempt')
        line = f'{GREEN}{line}{RESET}' + (f'  (attempt {attempt})' if attempt and attempt > 1 else '')
    elif kind == 'CallbackStarted':
        callback_id = event['CallbackStartedDetails']['CallbackId']
        line = f'{YELLOW}{line}{RESET}  callback {callback_id[:16]}...  waiting for a human, Lambda not running'
    elif kind == 'CallbackSucceeded':
        line = f'{GREEN}{line}{RESET}  {event["CallbackSucceededDetails"]["Result"].get("Payload")}'
    elif kind == 'InvocationCompleted':
        details = event['InvocationCompletedDetails']
        seconds = (details['EndTimestamp'] - details['StartTimestamp']).total_seconds()
        error = _payload(details, 'Error')
        verdict = f'{RED}ended: {error.get("ErrorType")}{RESET}' if error else 'ended'
        line = f'{CYAN}{stamp}  -- invocation {invocation} {verdict} {CYAN}after {seconds:.1f}s{RESET}'
    elif kind.startswith('Execution'):
        line = f'{BOLD}{line}{RESET}'
    return line


TERMINAL = {'ExecutionSucceeded', 'ExecutionFailed', 'ExecutionTimedOut', 'ExecutionStopped'}


def watch(ticket_id: str, *, stop_on_callback: bool = True) -> None:
    arn = execution_arn(ticket_id)
    print(f'{DIM}{arn}{RESET}')
    seen, invocation, started = 0, 1, time.monotonic()
    while True:
        events = history(arn)
        for event in events[seen:]:
            print(describe(event, invocation))
            if event['EventType'] == 'InvocationCompleted':
                invocation += 1
            if event['EventType'] in TERMINAL:
                result = lam.get_durable_execution(DurableExecutionArn=arn)
                print(f'\n{BOLD}status{RESET} {result["Status"]} after {time.monotonic() - started:.0f}s')
                if result.get('Result'):
                    print(json.dumps(json.loads(result['Result']), indent=2))
                if result.get('Error'):
                    print(json.dumps(result['Error'], indent=2, default=str))
                return
        pending = _pending_callbacks(events)
        if stop_on_callback and pending and events[-1]['EventType'] == 'InvocationCompleted':
            for approval in pending_approvals(ticket_id):
                print(f'{YELLOW}{_describe_approval(approval)}{RESET}')
            print(f'\n{YELLOW}Suspended until a manager answers. Run: ./demo.py approve {ticket_id}{RESET}')
            return
        seen = len(events)
        time.sleep(1)


def _pending_callbacks(events: list[dict[str, Any]]) -> list[str]:
    started = {
        e['Id']: e['CallbackStartedDetails']['CallbackId'] for e in events if e['EventType'] == 'CallbackStarted'
    }
    finished = {
        e['Id'] for e in events if e['EventType'].startswith('Callback') and e['EventType'] != 'CallbackStarted'
    }
    return [callback_id for op_id, callback_id in started.items() if op_id not in finished]


def start(scenario: str, *, watch_it: bool = True) -> None:
    ticket_id = new_ticket_id(scenario)
    event = {'ticket_id': ticket_id, **SCENARIOS[scenario]}
    print(f'{BOLD}ticket {ticket_id}{RESET}  {json.dumps(SCENARIOS[scenario])}')
    lam.invoke(
        FunctionName=DURABLE_FN,
        InvocationType='Event',
        DurableExecutionName=ticket_id,  # Same name and payload again = the same execution, not a second one.
        Payload=json.dumps(event).encode(),
    )
    if watch_it:
        watch(ticket_id)


def pending_approvals(ticket_id: str) -> list[dict[str, Any]]:
    """The approvals the agent's `ask_a_manager` recorded for a ticket and nobody has answered yet."""
    key = Key('pk').eq(f'TICKET#{ticket_id}') & Key('sk').begins_with('APPROVAL#')
    return [item for item in table.query(KeyConditionExpression=key)['Items'] if item['status'] == 'pending']


def _describe_approval(approval: dict[str, Any]) -> str:
    return (
        f'approval needed: refund ${approval["amount"]} on {approval["order_id"]} ({approval["why"]}), '
        f'tool call {approval["tool_call_id"]}, callback {approval["callback_id"][:16]}...'
    )


def approve(ticket_id: str, *, deny: bool, note: str | None) -> None:
    waiting = set(_pending_callbacks(history(execution_arn(ticket_id))))
    approvals = [a for a in pending_approvals(ticket_id) if a['callback_id'] in waiting]
    if not approvals:
        sys.exit('Nothing is waiting for approval.')
    approver = os.environ.get('USER', 'manager')
    # The answer is `DeferredToolResults` as JSON, keyed by tool call ID: `true` approves the call, and a
    # denial carries the message the model sees. One callback covers every call the model made in that turn.
    answer: bool | dict[str, str] = {'kind': 'tool-denied', 'message': note or 'A manager declined.'} if deny else True
    for callback_id in sorted({a['callback_id'] for a in approvals}):
        calls = [a['tool_call_id'] for a in approvals if a['callback_id'] == callback_id]
        decision = {
            'approvals': {tool_call_id: answer for tool_call_id in calls},
            'answered_by': {tool_call_id: approver for tool_call_id in calls},
        }
        lam.send_durable_execution_callback_success(CallbackId=callback_id, Result=json.dumps(decision).encode())
        print(f'{GREEN}sent {json.dumps(decision)}{RESET}')
        for tool_call_id in calls:
            table.update_item(
                Key={'pk': f'TICKET#{ticket_id}', 'sk': f'APPROVAL#{tool_call_id}'},
                UpdateExpression='SET #status = :status, answered_by = :approver',
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues={':status': 'denied' if deny else 'approved', ':approver': approver},
            )
    watch(ticket_id)


def ledger(ticket_id: str) -> None:
    items = table.query(KeyConditionExpression=Key('pk').eq(f'TICKET#{ticket_id}'))['Items']
    refunds = [i for i in items if i['sk'].startswith('REFUND#')]
    emails = [i for i in items if i['sk'].startswith('EMAIL#')]
    approvals = [i for i in items if i['sk'].startswith('APPROVAL#')]
    print(f'{BOLD}ticket {ticket_id}: {len(refunds)} refund(s), {len(emails)} email(s){RESET}')
    for approval in approvals:
        answered = f' by {approval["answered_by"]}' if approval.get('answered_by') else ''
        print(f'  approval {approval["status"]}{answered}: refund ${approval["amount"]} on {approval["order_id"]}')
    for refund in refunds:
        print(f'  refund {refund["refund_id"]}  ${refund["amount"]}  at {refund["at"]}  key {refund["sk"][7:30]}')
    for email in emails:
        print(f'  email to {email["to"]}: {email["body"][:90]}')


def plain(scenario: str) -> None:
    """Run the crash on an ordinary function, then retry it like a client or a queue would."""
    ticket_id = new_ticket_id(f'plain-{scenario}')
    event = {'ticket_id': ticket_id, **SCENARIOS[scenario]}
    print(f'{BOLD}ticket {ticket_id}{RESET} on {PLAIN_FN} (no durable execution)')
    for attempt in (1, 2):
        began = time.monotonic()
        response = lam.invoke(FunctionName=PLAIN_FN, Payload=json.dumps(event).encode())
        body = json.loads(response['Payload'].read() or b'null')
        outcome = f'{RED}{response.get("FunctionError")}: {body}{RESET}' if response.get('FunctionError') else body
        print(f'attempt {attempt} ({time.monotonic() - began:.1f}s): {outcome}')
        if not response.get('FunctionError'):
            break
        print(f'{DIM}retrying the whole request, as a client, a queue, or an async invoke would...{RESET}')
    ledger(ticket_id)


def list_executions() -> None:
    page = lam.list_durable_executions_by_function(
        FunctionName=DURABLE_FN.split(':')[0], ReverseOrder=True, MaxItems=10
    )
    for execution in page['DurableExecutions']:
        when = execution['StartTimestamp'].astimezone().strftime('%H:%M:%S')
        print(f'{when}  {execution["Status"]:<10} {execution["DurableExecutionName"]}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('start').add_argument('scenario', choices=SCENARIOS)
    sub.add_parser('watch').add_argument('ticket')
    approve_parser = sub.add_parser('approve')
    approve_parser.add_argument('ticket')
    approve_parser.add_argument('--deny', action='store_true')
    approve_parser.add_argument('--note')
    sub.add_parser('ledger').add_argument('ticket')
    sub.add_parser('plain').add_argument('scenario', choices=['crash'])
    sub.add_parser('list')
    args = parser.parse_args()

    if args.command == 'start':
        start(args.scenario)
    elif args.command == 'watch':
        watch(args.ticket, stop_on_callback=False)
    elif args.command == 'approve':
        approve(args.ticket, deny=args.deny, note=args.note)
    elif args.command == 'ledger':
        ledger(args.ticket)
    elif args.command == 'plain':
        plain(args.scenario)
    elif args.command == 'list':
        list_executions()


if __name__ == '__main__':
    main()
