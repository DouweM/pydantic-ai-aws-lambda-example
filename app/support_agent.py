"""The support agent: an ordinary Pydantic AI agent with three tools and a structured result."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

import boto3
import logfire
import shop
from aws_durable_execution_sdk_python.config import Duration, JitterStrategy, StepSemantics
from aws_durable_execution_sdk_python.retries import RetryPresets, RetryStrategyConfig, create_retry_strategy
from botocore.config import Config
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.instrumentation.botocore.extensions import _BOTOCORE_EXTENSIONS
from pydantic import BaseModel
from pydantic_ai import Agent, ApprovalRequired, DeferredToolRequests, ModelRetry, RunContext
from pydantic_ai.capabilities import RequestInput
from pydantic_ai.models import Model
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.providers.bedrock import BedrockProvider
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness.aws_lambda import AWSLambdaDurability

APPROVAL_LIMIT = 100.00

# Traces every model request and tool call. Sends to Logfire only when LOGFIRE_TOKEN is set. The
# service name is the function's, so the plain and durable functions show up separately.
logfire.configure(
    service_name=os.environ.get('AWS_LAMBDA_FUNCTION_NAME', 'support-agent'),
    send_to_logfire='if-token-present',
    console=False,
)
logfire.instrument_pydantic_ai()

# Also trace AWS API calls (DynamoDB writes, the Bedrock HTTP call). botocore's Bedrock extension would
# record each model request a second time, with its own `chat` span and token usage, so leave it out:
# Pydantic AI already records those, and the Bedrock call still gets a plain `BedrockRuntime.Converse` span.
_BOTOCORE_EXTENSIONS.pop('bedrock-runtime', None)
BotocoreInstrumentor().instrument()


@dataclass
class Ticket:
    """The agent's dependencies: which ticket it works on, and which faults to inject."""

    ticket_id: str
    faults: frozenset[str] = field(default_factory=frozenset)


class Resolution(BaseModel):
    decision: Literal['refunded', 'denied']
    refund_amount: float
    customer_reply: str


tools = FunctionToolset[Ticket](id='shop')

# The orders API has short outages: retry it quickly, and only for that error.
ORDERS_API_RETRY = create_retry_strategy(
    RetryStrategyConfig(
        max_attempts=4,
        initial_delay=Duration.from_seconds(2),
        backoff_rate=2,
        jitter_strategy=JitterStrategy.NONE,  # Predictable 2s, 4s, 8s for the demo.
        retryable_error_types=[shop.OrdersApiUnavailable],
    )
)


@tools.tool(metadata={'aws_lambda': {'retry_strategy': ORDERS_API_RETRY}})
def lookup_order(ctx: RunContext[Ticket], order_id: str) -> dict[str, Any]:
    """Look up an order by its id."""
    with logfire.span('orders API: GET /orders/{order_id}', order_id=order_id):
        if 'flaky_orders' in ctx.deps.faults:
            shop.fail_first(ctx.deps.ticket_id, 'orders_api', times=2)
        order = shop.ORDERS.get(order_id)
    if order is None:
        raise ModelRetry(f'There is no order {order_id!r}.')
    return order


# Money moves here: never re-run this body after an interruption, and never retry it.
@tools.tool(
    metadata={
        'aws_lambda': {
            'step_semantics': StepSemantics.AT_MOST_ONCE_PER_RETRY,
            'retry_strategy': RetryPresets.none(),
        }
    }
)
def issue_refund(ctx: RunContext[Ticket], order_id: str, amount: float, reason: str) -> str:
    """Refund the customer for an order."""
    if amount > APPROVAL_LIMIT and not ctx.tool_call_approved:
        raise ApprovalRequired(metadata={'why': f'refunds above ${APPROVAL_LIMIT:.0f} need a manager'})
    # The tool call id comes from the checkpointed model response, so it is stable across replays.
    with logfire.span('payments: refund ${amount} on {order_id}', amount=amount, order_id=order_id):
        return shop.record_refund(
            ctx.deps.ticket_id, idempotency_key=ctx.tool_call_id, order_id=order_id, amount=amount, reason=reason
        )


@tools.tool
def email_customer(ctx: RunContext[Ticket], to: str, body: str) -> str:
    """Email the customer. Never call this in the same turn as issue_refund."""
    if 'crash_before_email' in ctx.deps.faults:
        shop.crash_once(ctx.deps.ticket_id, 'email')
    with logfire.span('email: send to {to}', to=to):
        return shop.send_email(ctx.deps.ticket_id, to, body)


INSTRUCTIONS = (
    'You are a support agent for an online shop. Resolve the ticket in this order:\n'
    '1. Look up the order.\n'
    '2. Policy: an item delivered in the last 30 days that arrived damaged or faulty gets a full refund '
    'of its price. Otherwise, deny the refund politely.\n'
    '3. If the policy allows it, call issue_refund exactly once, on its own, and wait for its result.\n'
    '4. Only after that, email the customer with the outcome.\n'
    '5. Return the resolution. Today is 2026-11-12.'
)


def ask_a_manager(ctx: RunContext[Ticket], requests: DeferredToolRequests, callback_id: str) -> None:
    """Tell a manager that a refund needs approval. A real app would post to Slack or send an email.

    Runs once, in a durable step, with the ID of the Lambda callback the execution then suspends on.
    Returning `None` means "a person will answer": `demo.py approve` finds the pending approval in the
    ledger and sends the decision to the callback.
    """
    for call in requests.approvals:
        args = call.args_as_dict()
        why = requests.metadata.get(call.tool_call_id, {}).get('why', '')
        # Keyed on the tool call, so a retried step writes the same row again.
        shop.record_approval_request(
            ctx.deps.ticket_id,
            tool_call_id=call.tool_call_id,
            callback_id=callback_id,
            order_id=args['order_id'],
            amount=args['amount'],
            why=why,
        )
        logfire.info(
            'Approval needed: refund ${amount} on {order_id}',
            ticket_id=ctx.deps.ticket_id,
            amount=args['amount'],
            order_id=args['order_id'],
            tool_call_id=call.tool_call_id,
            callback_id=callback_id,
            why=why,
        )


def build_agent(model: Model) -> Agent[Ticket, Resolution]:
    return Agent(
        model,
        name='support',
        deps_type=Ticket,
        output_type=Resolution,
        toolsets=[tools],
        instructions=INSTRUCTIONS,
        capabilities=[
            AWSLambdaDurability(),
            # A refund above the limit waits for a manager inside the run: in a durable handler, on a
            # Lambda callback, with the execution suspended and no Lambda running until the answer arrives.
            RequestInput(ask_a_manager, timeout=timedelta(hours=24)),
        ],
    )


# One retry owner: durable steps retry model requests, so the Bedrock client makes one attempt.
bedrock = boto3.client('bedrock-runtime', config=Config(retries={'max_attempts': 0}, read_timeout=120))
agent = build_agent(
    BedrockConverseModel(
        os.environ.get('MODEL_ID', 'global.anthropic.claude-haiku-5-5'),
        provider=BedrockProvider(bedrock_client=bedrock),
    )
)
