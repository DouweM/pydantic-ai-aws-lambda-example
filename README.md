# Durable Pydantic AI agent on AWS Lambda

> Watch the live demo explaining this repo's code at [pydantic.dev/events/make-ai-agents-durable-aws-lambda](https://pydantic.dev/events/make-ai-agents-durable-aws-lambda).

A shop support agent, built with [Pydantic AI](https://pydantic.dev/docs/ai/), that runs on
[AWS Lambda durable functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html)
through the `AWSLambdaDurability` capability from
[Pydantic AI Harness](https://pydantic.dev/docs/ai/harness/aws-lambda/). The demo injects faults to
show what durable execution gives an agent:

- **Every model request and tool call is a durable step.** Its result is checkpointed, so a later
  invocation replays it instead of running it again.
- **Crash and resume without repeating side effects.** If the process dies after the refund, the next
  invocation replays the refund from its checkpoint and continues with the email: one refund, one email.
- **Per-step retries.** When the orders API returns 503, only that step is retried, with backoff, and
  the function is suspended (not running) between attempts.
- **Human approval.** A refund over $100 needs a manager. The agent asks through `RequestInput`, the
  execution suspends on a Lambda callback at no compute cost, and continues where it left off when
  someone runs `demo.py approve` (or `--deny`).

Built with Thomas Gaigher ([@yaythomas](https://github.com/yaythomas), AWS Lambda durable functions team).

## How it works

| File | What it is |
|---|---|
| `app/support_agent.py` | The agent: a Bedrock model, three tools (`lookup_order`, `issue_refund`, `email_customer`), structured `Resolution` output, `AWSLambdaDurability` with per-tool step settings, and `RequestInput` for refund approvals. Also sets up Logfire. |
| `app/handler.py` | The durable handler. Runs the agent once with `run_durable`; an approval waits inside that run. |
| `app/handler_plain.py` | The same agent in an ordinary Lambda handler, to compare against. |
| `app/handler_telemetry.py` | The durable handler with the Durable Execution Telemetry Exporter, for an optional second deployment. |
| `app/shop.py` | Fake backend: two orders, a DynamoDB ledger of refunds, emails and approvals, and fault injection keyed by ticket. |
| `deploy.sh` | Creates the DynamoDB table, IAM role and functions, publishes a version and points the alias `live` at it. Safe to re-run. |
| `demo.py` | Starts scenarios, streams the execution history, answers approvals, prints the ledger. |
| `local_demo.py` | Runs the durable handler on your machine against real Bedrock, with no Lambda function or table. |
| `tests/test_local.py` | Runs the durable handler locally with a scripted model. No AWS calls. |

A ticket goes through these pieces:

1. `demo.py start` invokes the durable function `support-agent:live` with the ticket ID as the durable
   execution name, so starting the same ticket twice returns the same execution.
2. `handler.py` runs the agent with `run_durable`. `AWSLambdaDurability` turns each model request
   (Amazon Bedrock, through `BedrockConverseModel`) and each tool call into a durable step.
   `issue_refund` runs at most once per attempt and is never retried; `lookup_order` is retried on
   `OrdersApiUnavailable` after 2, 4 and 8 seconds.
3. A refund over $100 raises `ApprovalRequired`. `RequestInput` creates a Lambda callback and calls
   `ask_a_manager` in a step, which writes a pending approval with the callback ID to the DynamoDB ledger.
   The execution then suspends for up to 24 hours.
4. `demo.py approve` sends the decision to the callback. Lambda invokes the function again, the run
   replays its checkpoints without calling the model again, and runs the refund (or tells the model it
   was denied).
5. Refunds and emails are written to the DynamoDB ledger, so `demo.py ledger` shows what each ticket
   really did. Traces go to [Logfire](#traces-in-logfire) when a token is configured.

Newer Pydantic AI versions spell `RequestInput(handler)` as `HandleDeferredToolCalls(handler)`, with a
`token` parameter on the handler.

## Prerequisites

- An AWS account and Region where Lambda durable functions are available and you have access to the
  Amazon Bedrock model. The defaults are `us-west-2` and the inference profile
  `global.anthropic.claude-haiku-5-5`; set `AWS_REGION` and `MODEL_ID` to use others.
- Credentials that can create a DynamoDB table, an IAM role and Lambda functions.
- A recent AWS CLI v2 (one that supports `aws lambda create-function --durable-config`) and `zip`.
- Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.13 --seed .venv   # --seed installs pip, which deploy.sh uses to build the package
uv pip install -r requirements.txt
```

## Deploy

```bash
export AWS_REGION=us-west-2
./deploy.sh   # about 2 minutes
```

`deploy.sh` builds an arm64 Python 3.13 package with the pinned dependencies and creates or updates:

- the DynamoDB table `pydantic-durable-demo` (the ledger),
- the IAM role `pydantic-durable-demo-role`, with `AWSLambdaBasicDurableExecutionRolePolicy`,
  `AWSXRayDaemonWriteAccess`, and access to Bedrock and the table,
- `support-agent-plain`, an ordinary function, with Lambda's async retries turned off,
- `support-agent`, the durable function (execution timeout 24 hours, active tracing), with a new
  published version behind the alias `live`. Executions in flight stay on the version they started on.

| Variable | Default | What it does |
|---|---|---|
| `AWS_REGION` | `us-west-2` | Region to deploy to. |
| `MODEL_ID` | `global.anthropic.claude-haiku-5-5` | Bedrock model or inference profile the agent uses. |
| `NAME` | `pydantic-durable-demo` | Name of the table, and prefix of the role. If you change it, set `DEMO_TABLE` to the same value for `demo.py`. |
| `LOGFIRE_TOKEN` | unset | Logfire write token. See [Traces in Logfire](#traces-in-logfire). |
| `LOGFIRE_TOKEN_FILE` | `~/.config/pydantic-demo/logfire-write-token` | File to read the token from when `LOGFIRE_TOKEN` is unset. |
| `PYDANTIC_AI_VERSION` / `HARNESS_VERSION` | `2.68.0` / `0.68.0` | Versions of `pydantic-ai-slim` and `pydantic-ai-harness` to package. |
| `PYDANTIC_AI_REF` | unset | Build Pydantic AI and the harness from this branch or commit of `pydantic/pydantic-ai` instead. |
| `SDK_VERSION` | `3.0.1` | Durable Execution SDK version. Set it to empty (`SDK_VERSION=`) to let the harness pick one, without the OTel plugin. |
| `DURABLE_HANDLER` | `handler.handler` | Handler of the durable function. |
| `FN_SUFFIX` | empty | Suffix for the function names, to deploy a second copy side by side. |
| `PYTHON` | `.venv/bin/python` | Python with pip, used to build the package. |

To deploy a second copy that also writes a per-execution JSON record with the Durable Execution
Telemetry Exporter:

```bash
FN_SUFFIX=-sdk3 DURABLE_HANDLER=handler_telemetry.handler ./deploy.sh
DURABLE_FN=support-agent-sdk3:live .venv/bin/python demo.py start flaky
```

## Run the scenarios

```bash
alias demo="$PWD/.venv/bin/python $PWD/demo.py"
demo start happy   # the first run also warms up the function
```

| Command | What happens |
|---|---|
| `demo start happy` | Look up the order, refund $79, email the customer, answer. One invocation. |
| `demo plain crash` | The ordinary function dies after the refund and the request is retried: two refunds, one email. |
| `demo start crash` | The same crash on the durable function. The second invocation replays the refund and sends the email: one refund, one email. |
| `demo start flaky` | The orders API returns 503 twice. The step is retried after 2 and 4 seconds, with Lambda not running in between. |
| `demo start approval` | A $249 refund needs a manager. The execution suspends on the callback and `demo` prints the ticket ID. |
| `demo approve TICKET` | Approve the pending refund; the execution resumes and finishes. |
| `demo approve TICKET --deny --note "..."` | Deny it. The note is what the model sees, so it can tell the customer why. |
| `demo watch TICKET` | Stream an execution's history until it ends. |
| `demo ledger TICKET` | The refunds, emails and approvals a ticket produced. |
| `demo list` | Recent durable executions. |

`demo.py` reads `AWS_REGION`, `DURABLE_FN` (default `support-agent:live`), `PLAIN_FN` (default
`support-agent-plain`) and `DEMO_TABLE` (default `pydantic-durable-demo`).

The approval decision is the JSON form of Pydantic AI's
[`DeferredToolResults`](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/), keyed by tool
call ID, so any approver can send it, for example from the AWS CLI:

```bash
aws lambda send-durable-execution-callback-success --callback-id <CALLBACK_ID> \
  --cli-binary-format raw-in-base64-out --result '{"approvals": {"<TOOL_CALL_ID>": true}}'
```

In the execution history, the approval shows up as the steps
`support__external_event.deferred_tool_calls` (the callback) and
`support__external_event.deferred_tool_calls.ask` (the step that ran `ask_a_manager`).

## Run locally, without deploying

`local_demo.py` runs the same durable handler on your machine. The Durable Execution SDK's in-process
test runner stands in for the Lambda durable backend (checkpoints, retries, suspension, callbacks), the
ledger is kept in memory, and the model calls go to Amazon Bedrock. You need AWS credentials with
Bedrock access, or `AWS_BEARER_TOKEN_BEDROCK`.

```bash
.venv/bin/python local_demo.py happy
.venv/bin/python local_demo.py flaky
.venv/bin/python local_demo.py approval --wait 20          # a "manager" approves after 20 seconds
.venv/bin/python local_demo.py approval --wait 20 --deny
```

Set `LOGFIRE_TOKEN` to send the traces to Logfire.

## Tests

`tests/test_local.py` runs the durable handler with the SDK's in-process test runner, a scripted
`FunctionModel` and an in-memory shop, so it makes no AWS or model calls. It checks that:

- every model request and tool call is a durable step, and the refund is issued once,
- a flaky `lookup_order` is retried by its step until the third attempt succeeds,
- a $249 refund records a pending approval and refunds nothing while the execution waits, then
  refunds once when approved and not at all when denied.

```bash
.venv/bin/python -m pytest tests -q
```

## Traces in Logfire

1. Create a project in [Logfire](https://logfire.pydantic.dev/docs/) and a write token for it
   (project settings, **Write tokens**).
2. Deploy with the token:

   ```bash
   LOGFIRE_TOKEN=<your-write-token> ./deploy.sh
   ```

   Or save it in `~/.config/pydantic-demo/logfire-write-token`, which `deploy.sh` reads on every run.
   The token is stored in the function's environment variables, so use a token that can only write
   telemetry. Without a token, nothing is sent.
3. Run a scenario and open your project at `https://logfire.pydantic.dev/<your-org>/<your-project>`.

You'll see:

- **The invocation view.** `aws-durable-execution-sdk-python-otel` adds a span per invocation with the
  steps it ran, so a crash, a retry or an approval shows up as separate invocations in one trace.
- **Pydantic AI spans** for the agent run, model requests and tool calls, with token usage and cost
  (`logfire.instrument_pydantic_ai()`).
- **AWS API calls** such as the DynamoDB writes and the Bedrock HTTP call, from the botocore
  instrumentation. Its Bedrock extension is turned off, because it would record each model request
  and its token usage a second time.
- **Spans inside the tool bodies**, such as `payments: refund ...`, and a log for each approval request,
  from `logfire.span(...)` and `logfire.info(...)` in `app/support_agent.py`.

The OTel plugin needs the OpenTelemetry SDK tracer provider, while Logfire hands out a proxy, so
`app/handler.py` passes it `logfire.DEFAULT_LOGFIRE_INSTANCE.config.get_tracer_provider().provider`.

Replayed steps emit no spans or token and cost metrics (since Pydantic AI 2.67.0), so each model
request is counted once, even when an execution runs across several invocations.

## Clean up

```bash
for fn in support-agent support-agent-plain; do   # add the -sdk3 copies if you deployed them
  aws lambda delete-function --function-name $fn
  aws logs delete-log-group --log-group-name /aws/lambda/$fn
done
aws dynamodb delete-table --table-name pydantic-durable-demo
aws iam delete-role-policy --role-name pydantic-durable-demo-role --policy-name app
aws iam detach-role-policy --role-name pydantic-durable-demo-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicDurableExecutionRolePolicy
aws iam detach-role-policy --role-name pydantic-durable-demo-role \
  --policy-arn arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess
aws iam delete-role --role-name pydantic-durable-demo-role
```

## Learn more

- [Durable execution in Pydantic AI](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/)
- [AWS Lambda durable functions in Pydantic AI Harness](https://pydantic.dev/docs/ai/harness/aws-lambda/)
- [Deferred tools and approvals](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/)
- [Pydantic AI with Logfire](https://pydantic.dev/docs/ai/integrations/logfire/)
- [AWS Lambda durable functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html)
- [AWS Durable Execution SDK for Python](https://github.com/aws/aws-durable-execution-sdk-python)
