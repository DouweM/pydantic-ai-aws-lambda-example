"""A fake shop backend: orders, a refund ledger, an email outbox, and fault injection.

Refunds and emails are written to DynamoDB, so after a crash you can count how many
refunds and emails a ticket really produced.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

TABLE = boto3.resource('dynamodb').Table(os.environ.get('DEMO_TABLE', 'pydantic-durable-demo'))

ORDERS: dict[str, dict[str, Any]] = {
    'A-1001': {
        'order_id': 'A-1001',
        'item': 'Noise-cancelling headphones',
        'price': 79.00,
        'delivered': '2026-11-02',
        'customer_email': 'ana@example.com',
    },
    'A-2002': {
        'order_id': 'A-2002',
        'item': 'Espresso machine',
        'price': 249.00,
        'delivered': '2026-11-01',
        'customer_email': 'ben@example.com',
    },
}


class OrdersApiUnavailable(Exception):
    """The orders API returned a 503. Retrying later succeeds."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


def record_refund(ticket_id: str, *, idempotency_key: str, order_id: str, amount: float, reason: str) -> str:
    """Write one refund to the ledger. A repeated idempotency key does not refund twice."""
    refund_id = f'R-{uuid.uuid4().hex[:8]}'
    try:
        TABLE.put_item(
            Item={
                'pk': f'TICKET#{ticket_id}',
                'sk': f'REFUND#{idempotency_key}',
                'refund_id': refund_id,
                'order_id': order_id,
                'amount': Decimal(str(amount)),
                'reason': reason,
                'at': _now(),
            },
            ConditionExpression='attribute_not_exists(pk)',
        )
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        existing = TABLE.get_item(Key={'pk': f'TICKET#{ticket_id}', 'sk': f'REFUND#{idempotency_key}'})['Item']
        return f'refund {existing["refund_id"]} already issued for ${existing["amount"]}'
    print(f'[shop] REFUND {refund_id}: ${amount:.2f} on {order_id} for ticket {ticket_id}', flush=True)
    return f'refund {refund_id} issued for ${amount:.2f}'


def send_email(ticket_id: str, to: str, body: str) -> str:
    TABLE.put_item(
        Item={'pk': f'TICKET#{ticket_id}', 'sk': f'EMAIL#{_now()}#{uuid.uuid4().hex[:4]}', 'to': to, 'body': body}
    )
    print(f'[shop] EMAIL to {to} for ticket {ticket_id}', flush=True)
    return f'email sent to {to}'


def record_approval_request(
    ticket_id: str, *, tool_call_id: str, callback_id: str, order_id: str, amount: float, why: str
) -> None:
    """Write a pending approval: what to approve, and the Lambda callback that carries the answer."""
    TABLE.put_item(
        Item={
            'pk': f'TICKET#{ticket_id}',
            'sk': f'APPROVAL#{tool_call_id}',
            'tool_call_id': tool_call_id,
            'callback_id': callback_id,
            'order_id': order_id,
            'amount': Decimal(str(amount)),
            'why': why,
            'status': 'pending',
            'at': _now(),
        }
    )
    print(f'[shop] APPROVAL needed: ${amount:.2f} on {order_id} for ticket {ticket_id} ({why})', flush=True)


def ledger(ticket_id: str) -> list[dict[str, Any]]:
    """Every refund, email and approval written for a ticket."""
    response = TABLE.query(KeyConditionExpression=Key('pk').eq(f'TICKET#{ticket_id}'))
    return [item for item in response['Items'] if not item['sk'].startswith('FAULT#')]


# Fault injection. Each fault is keyed by ticket, so every demo run starts clean.


def fail_first(ticket_id: str, fault: str, times: int) -> None:
    """Raise `OrdersApiUnavailable` on the first `times` calls for this ticket."""
    response = TABLE.update_item(
        Key={'pk': f'TICKET#{ticket_id}', 'sk': f'FAULT#{fault}'},
        UpdateExpression='ADD calls :one',
        ExpressionAttributeValues={':one': 1},
        ReturnValues='UPDATED_NEW',
    )
    calls = int(response['Attributes']['calls'])
    if calls <= times:
        print(f'[shop] orders API 503 (call {calls} of {times} failing calls)', flush=True)
        raise OrdersApiUnavailable(f'orders API returned 503 Service Unavailable (call {calls})')


def crash_once(ticket_id: str, fault: str) -> None:
    """Kill the whole process the first time this runs for a ticket, like an OOM or a lost host."""
    try:
        TABLE.put_item(
            Item={'pk': f'TICKET#{ticket_id}', 'sk': f'FAULT#{fault}', 'at': _now()},
            ConditionExpression='attribute_not_exists(pk)',
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return  # Already crashed once for this ticket.
        raise
    print(f'[shop] CRASH: the process dies now (fault {fault!r}, ticket {ticket_id})', flush=True)
    sys.stdout.flush()
    os._exit(1)
