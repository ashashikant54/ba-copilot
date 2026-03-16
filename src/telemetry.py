# telemetry.py
# CoAnalytica — OpenTelemetry Instrumentation Setup
#
# ═══════════════════════════════════════════════════════════════
# WHAT THIS FILE DOES
# ═══════════════════════════════════════════════════════════════
#
# Sets up OpenTelemetry for CoAnalytica with:
#
#  1. TracerProvider — creates and manages traces
#     A trace is one end-to-end request (e.g. one agent run).
#     It contains many spans — one per tool call or node.
#
#  2. Azure Monitor Exporter — sends traces to App Insights
#     Falls back to ConsoleSpanExporter if connection string
#     is not set (local development mode).
#
#  3. Helper functions:
#     - llm_span()      context manager for every GPT call
#     - agent_span()    context manager for agent-level spans
#     - tool_span()     context manager for tool-level spans
#
# ═══════════════════════════════════════════════════════════════
# HOW OPENTELEMETRY WORKS — THE 3 CONCEPTS
# ═══════════════════════════════════════════════════════════════
#
# TRACES
#   A trace represents one complete operation — e.g. one call to
#   validate_requirements(). It has a unique trace_id.
#
# SPANS
#   A trace is made up of spans. Each span represents one unit of
#   work — one node, one tool call, one LLM call.
#   Spans have:
#     - name        "babok_check_node"
#     - start/end   timing
#     - attributes  key-value metadata (session_id, model, tokens...)
#     - events      point-in-time occurrences ("threshold_met")
#     - status      OK or ERROR
#
# CONTEXT PROPAGATION
#   Spans are nested — a child span's parent is the span that was
#   active when the child was created. This is how you get the
#   tree structure in App Insights:
#
#   validate_requirements [trace]
#     ├── kb_search_node [span]
#     ├── meeting_crossref_node [span]
#     │     └── llm_call/gpt-4o-mini [span]  ← child of meeting_crossref
#     ├── babok_check_node iter=1 [span]
#     │     └── llm_call/gpt-4o-mini [span]
#     ├── reflection_node iter=1 [span]
#     │     └── llm_call/gpt-4o-mini [span]
#     └── babok_check_node iter=2 [span]
#           └── llm_call/gpt-4o-mini [span]
#
# ═══════════════════════════════════════════════════════════════
# GENAI SEMANTIC CONVENTIONS
# ═══════════════════════════════════════════════════════════════
#
# The OpenTelemetry GenAI working group has standardised attribute
# names for LLM calls. Using these means your traces are compatible
# with any OTel-aware tool (Grafana, Datadog, LangSmith, etc.)
# without any translation layer.
#
# Key attributes used in this file:
#   gen_ai.system              = "openai"
#   gen_ai.request.model       = "gpt-4o-mini"
#   gen_ai.request.temperature = 0.1
#   gen_ai.request.max_tokens  = 2000
#   gen_ai.usage.input_tokens  = 847
#   gen_ai.usage.output_tokens = 312
#
# CoAnalytica custom attributes (prefixed coanalytica.*):
#   coanalytica.session_id
#   coanalytica.agent.name
#   coanalytica.agent.iteration
#   coanalytica.agent.quality_score
#   coanalytica.tool.name
#   coanalytica.prompt.version
#   coanalytica.cost.usd
 
import os
import sys
import logging
from contextlib import contextmanager
from typing import Optional, Generator
 
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.trace import Status, StatusCode, Span
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as GenAI
 
logger = logging.getLogger(__name__)
 
# ── Service resource ───────────────────────────────────────────
# Resource attributes appear on every span — identifies the service
# that generated the trace in App Insights.
_RESOURCE = Resource.create({
    SERVICE_NAME:    "coanalytica",
    SERVICE_VERSION: "1.0.0",
    "deployment.environment": os.getenv("ENVIRONMENT", "development"),
})
 
# Module-level tracer — set up once on first import
_tracer: Optional[trace.Tracer] = None
_initialized = False
 
 
def setup_telemetry() -> trace.Tracer:
    """
    Initialise OpenTelemetry with Azure Monitor exporter.
 
    Called once at application startup (in main.py lifespan).
 
    Export behaviour:
      APPLICATIONINSIGHTS_CONNECTION_STRING set → Azure Monitor
      Not set → ConsoleSpanExporter (local dev, prints to stdout)
 
    Returns the global tracer for CoAnalytica.
    """
    global _tracer, _initialized
    if _initialized:
        return _tracer
 
    provider = TracerProvider(resource=_RESOURCE)
 
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
 
    if connection_string:
        # ── Production: Azure Monitor exporter ────────────────
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
            azure_exporter = AzureMonitorTraceExporter(
                connection_string=connection_string
            )
            provider.add_span_processor(
                BatchSpanProcessor(azure_exporter)
            )
            logger.info("✅ OTel → Azure Monitor (App Insights)")
            print("✅ Telemetry: Azure Monitor exporter active")
        except Exception as e:
            logger.warning(f"Azure Monitor exporter failed: {e} — falling back to console")
            _add_console_exporter(provider)
    else:
        # ── Development: Console exporter ─────────────────────
        # Prints span summaries to stdout — useful for local debugging
        _add_console_exporter(provider)
        print("ℹ️  Telemetry: Console exporter active (set APPLICATIONINSIGHTS_CONNECTION_STRING for Azure Monitor)")
 
    # Register as the global provider
    trace.set_tracer_provider(provider)
 
    _tracer = trace.get_tracer(
        "coanalytica.agents",
        schema_url="https://opentelemetry.io/schemas/1.26.0"
    )
    _initialized = True
    return _tracer
 
 
def _add_console_exporter(provider: TracerProvider) -> None:
    """Add a readable console exporter for local development."""
    # SimpleSpanProcessor exports immediately (no batching)
    # Good for dev — you see spans as they complete
    provider.add_span_processor(
        SimpleSpanProcessor(ConsoleSpanExporter())
    )
 
 
def get_tracer() -> trace.Tracer:
    """Get the global tracer. Initialises with console exporter if not set up yet."""
    global _tracer
    if _tracer is None:
        return setup_telemetry()
    return _tracer
 
 
# ══════════════════════════════════════════════════════════════
# CONTEXT MANAGERS — Use these in every node and LLM call
# ══════════════════════════════════════════════════════════════
 
@contextmanager
def agent_span(
    agent_name: str,
    session_id: str,
) -> Generator[Span, None, None]:
    """
    Context manager for agent-level spans.
    This is the ROOT span for an entire agent run.
    All node spans and LLM call spans are children of this.
 
    Usage:
        with agent_span("requirements_validation", session_id) as span:
            # run all tools
            span.set_attribute("coanalytica.agent.quality_score", 82)
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(
        f"agent/{agent_name}",
        kind=trace.SpanKind.INTERNAL,
    ) as span:
        span.set_attribute("coanalytica.session_id",  session_id)
        span.set_attribute("coanalytica.agent.name",  agent_name)
        span.add_event("agent.started", {"session_id": session_id})
        try:
            yield span
            span.set_status(Status(StatusCode.OK))
            span.add_event("agent.completed")
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)
            raise
 
 
@contextmanager
def tool_span(
    tool_name:  str,
    session_id: str,
    iteration:  int = 0,
    **extra_attrs,
) -> Generator[Span, None, None]:
    """
    Context manager for tool/node-level spans.
    These are children of the agent span they're nested inside.
 
    Usage:
        with tool_span("kb_search", session_id) as span:
            result = _tool_kb_search(...)
            span.set_attribute("coanalytica.kb.chunks_found", len(result))
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(
        f"tool/{tool_name}",
        kind=trace.SpanKind.INTERNAL,
    ) as span:
        span.set_attribute("coanalytica.session_id", session_id)
        span.set_attribute("coanalytica.tool.name",  tool_name)
        if iteration:
            span.set_attribute("coanalytica.agent.iteration", iteration)
        for k, v in extra_attrs.items():
            span.set_attribute(f"coanalytica.{k}", v)
        try:
            yield span
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)
            raise
 
 
@contextmanager
def llm_span(
    operation:     str,
    model:         str,
    temperature:   float,
    max_tokens:    int,
    prompt_version: str = "",
    session_id:    str = "",
    iteration:     int = 0,
) -> Generator[Span, None, None]:
    """
    Context manager for every LLM call.
    Applies GenAI semantic conventions — industry standard attributes
    that make traces compatible with any OTel-aware observability tool.
 
    Usage:
        with llm_span("babok_check", "gpt-4o-mini", 0.1, 3000,
                      prompt_version="1.0.0",
                      session_id=session_id,
                      iteration=iteration) as span:
            response = client.chat.completions.create(...)
            # Record actual token usage after the call
            record_llm_usage(span, response.usage)
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(
        f"llm/{operation}",
        kind=trace.SpanKind.CLIENT,  # CLIENT = outbound call
    ) as span:
        # ── GenAI Semantic Conventions ─────────────────────────
        span.set_attribute(GenAI.GEN_AI_SYSTEM,              "openai")
        span.set_attribute(GenAI.GEN_AI_REQUEST_MODEL,        model)
        span.set_attribute("gen_ai.request.temperature",      temperature)
        span.set_attribute("gen_ai.request.max_tokens",       max_tokens)
 
        # ── CoAnalytica custom attributes ──────────────────────
        span.set_attribute("coanalytica.session_id",          session_id)
        span.set_attribute("coanalytica.llm.operation",       operation)
        if prompt_version:
            span.set_attribute("coanalytica.prompt.version",  prompt_version)
        if iteration:
            span.set_attribute("coanalytica.agent.iteration", iteration)
 
        try:
            yield span
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)
            raise
 
 
def record_llm_usage(span: Span, usage, cost_usd: float = 0.0) -> None:
    """
    Record actual token usage and cost on an llm_span after the call completes.
 
    Call this immediately after client.chat.completions.create():
 
        with llm_span(...) as span:
            response = client.chat.completions.create(...)
            record_llm_usage(span, response.usage, cost)
    """
    if usage:
        span.set_attribute(GenAI.GEN_AI_USAGE_INPUT_TOKENS,  usage.prompt_tokens or 0)
        span.set_attribute(GenAI.GEN_AI_USAGE_OUTPUT_TOKENS, usage.completion_tokens or 0)
    if cost_usd:
        span.set_attribute("coanalytica.cost.usd", cost_usd)
 
 
def record_quality_score(span: Span, score: int, threshold: int, passed: bool) -> None:
    """
    Record agent quality score on a span and emit a quality event.
    Used in both F7 and F8 agents.
    """
    span.set_attribute("coanalytica.agent.quality_score", score)
    span.set_attribute("coanalytica.agent.threshold",     threshold)
    span.set_attribute("coanalytica.agent.passed",        passed)
    span.add_event(
        "quality_score_computed",
        {
            "score":     score,
            "threshold": threshold,
            "passed":    passed,
        }
    )
 
 
def record_reflection_triggered(span: Span, iteration: int, score: int) -> None:
    """Emit an event when the reflection loop is triggered."""
    span.add_event(
        "reflection_triggered",
        {
            "iteration": iteration,
            "score":     score,
            "reason":    f"score {score} below threshold",
        }
    )
 
 
def record_agent_coordination(span: Span, f7_score: int, new_threshold: int) -> None:
    """
    Record multi-agent coordination event.
    Called in F8 when it reads F7 score and adjusts threshold.
    This span event makes the coordination visible in App Insights.
    """
    span.add_event(
        "multi_agent_coordination",
        {
            "f7_quality_score":    f7_score,
            "adjusted_threshold":  new_threshold,
            "reason": "F8 threshold raised because F7 score was below 70"
                      if f7_score < 70 else "F8 threshold unchanged",
        }
    )
