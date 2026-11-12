"""The same agent in an ordinary Lambda handler. Nothing is checkpointed, so a retry starts over."""

from __future__ import annotations

from typing import Any

import logfire
from support_agent import Ticket, agent


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    ticket = Ticket(ticket_id=event['ticket_id'], faults=frozenset(event.get('faults', [])))
    prompt = f'Ticket {ticket.ticket_id} about order {event["order_id"]}: {event["message"]}'
    try:
        return agent.run_sync(prompt, deps=ticket).output.model_dump()
    finally:
        logfire.force_flush()
