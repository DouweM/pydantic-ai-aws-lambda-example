"""The durable handler: the agent's work survives crashes, retries and long waits."""

from __future__ import annotations

from typing import Any

import logfire
from aws_durable_execution_sdk_python import DurableContext, durable_execution
from pydantic_ai_harness.aws_lambda import run_durable
from support_agent import Ticket, agent

try:  # The OTel plugin needs Durable Execution SDK 3; deploy.sh installs it only for SDK 3 builds.
    from aws_durable_execution_sdk_python_otel import InvocationOtelPluginFactory, OtelPluginConfig
except ImportError:
    durable = durable_execution
else:
    # Invocation view: each Invocation span contains the steps it ran and the agent's spans. Logfire
    # hands out a proxy tracer provider, and the plugin needs the OpenTelemetry SDK provider behind it.
    tracer_provider = logfire.DEFAULT_LOGFIRE_INSTANCE.config.get_tracer_provider().provider
    otel_view = InvocationOtelPluginFactory(OtelPluginConfig(tracer_provider=tracer_provider))
    durable = durable_execution(plugins=[otel_view])


@durable
def handler(event: dict[str, Any], context: DurableContext) -> dict[str, Any]:
    try:
        return resolve_ticket(event, context)
    finally:
        # Lambda freezes the sandbox when an invocation ends or suspends, so send traces now.
        logfire.force_flush()


def resolve_ticket(event: dict[str, Any], context: DurableContext) -> dict[str, Any]:
    ticket = Ticket(ticket_id=event['ticket_id'], faults=frozenset(event.get('faults', [])))
    prompt = f'Ticket {ticket.ticket_id} about order {event["order_id"]}: {event["message"]}'
    context.logger.info('Ticket received', extra={'ticket_id': ticket.ticket_id, 'faults': sorted(ticket.faults)})

    # One agent run, start to finish. A refund above the limit waits for a manager inside it
    # (`RequestInput` in support_agent.py): the execution suspends on a Lambda callback, Lambda is
    # not running while it waits, and the run continues where it left off when the answer arrives.
    result = run_durable(lambda: agent.run(prompt, deps=ticket), context=context)
    return result.output.model_dump()
