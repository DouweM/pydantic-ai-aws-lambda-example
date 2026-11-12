"""The same durable handler, plus the Durable Execution Telemetry Exporter.

The exporter writes one JSON record per execution (status, timing, every step and its
attempts) to the function's own log group. It needs Durable Execution SDK 3. Deploy it as a
side-by-side copy with `SDK_VERSION=3.0.1 FN_SUFFIX=-sdk3 DURABLE_HANDLER=handler_telemetry.handler`.
"""

from __future__ import annotations

from typing import Any

import logfire
from aws_durable_execution_sdk_python import DurableContext, durable_execution
from aws_durable_execution_sdk_python_telemetry_exporter import (
    DurableExecutionTelemetryConfig,
    durable_execution_telemetry,
)
from handler import resolve_ticket


@durable_execution(plugins=[durable_execution_telemetry(DurableExecutionTelemetryConfig())])
def handler(event: dict[str, Any], context: DurableContext) -> dict[str, Any]:
    try:
        return resolve_ticket(event, context)
    finally:
        logfire.force_flush()
