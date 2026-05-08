from google.adk.evaluation.custom_metric_evaluator import _CustomMetricEvaluator
from google.adk.evaluation.eval_case import ConversationScenario, Invocation, get_all_tool_calls
from google.adk.evaluation.eval_metrics import (
    EvalMetric,
    EvalStatus,
    Interval,
    MetricInfo,
    MetricValueInfo,
)
from google.adk.evaluation.evaluator import EvaluationResult, PerInvocationResult
from google.adk.evaluation.metric_evaluator_registry import DEFAULT_METRIC_EVALUATOR_REGISTRY

TOOL_TRAJECTORY_NAME_MATCH = "tool_trajectory_name_match"


def register_custom_metrics() -> None:
    """Register this module's custom metrics with ADK's default registry.

    ADK's ``EvalConfig.custom_metrics`` populates ``EvalMetric.custom_function_path``
    but does not register the metric in ``DEFAULT_METRIC_EVALUATOR_REGISTRY``.
    Without this call, ``get_evaluator`` raises ``NotFoundError`` and the eval
    silently reports ``NOT_EVALUATED`` (the exception is swallowed in
    ``LocalEvalService._evaluate_metric_for_eval_case``).
    """
    DEFAULT_METRIC_EVALUATOR_REGISTRY.register_evaluator(
        metric_info=MetricInfo(
            metric_name=TOOL_TRAJECTORY_NAME_MATCH,
            description=(
                "Tool-name trajectory match. Args ignored — useful when an "
                "AgentTool injects an LLM-generated `request` arg."
            ),
            metric_value_info=MetricValueInfo(interval=Interval(min_value=0.0, max_value=1.0)),
        ),
        evaluator=_CustomMetricEvaluator,
    )


def check_tool_trajectory_ignore_request_arg(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None,
    conversation_scenario: ConversationScenario | None,
) -> EvaluationResult:
    if not expected_invocations:
        return EvaluationResult(overall_score=0.0, overall_eval_status=EvalStatus.NOT_EVALUATED)

    if len(actual_invocations) != len(expected_invocations):
        return EvaluationResult(overall_score=0.0, overall_eval_status=EvalStatus.FAILED)

    per_invocation_results = []

    for actual, expected in zip(actual_invocations, expected_invocations, strict=True):
        # `get_all_tool_calls` handles both the JSON-loaded `IntermediateData`
        # shape (expected) and the runtime `InvocationEvents` shape (actual).
        actual_tools = get_all_tool_calls(actual.intermediate_data)
        expected_tools = get_all_tool_calls(expected.intermediate_data)

        if len(actual_tools) != len(expected_tools):
            score = 0.0
        else:
            # Args intentionally unchecked — the AgentTool layer injects an
            # LLM-generated `request` arg whose text is non-deterministic.
            score = 1.0
            for act_tool, exp_tool in zip(actual_tools, expected_tools, strict=True):
                if act_tool.name != exp_tool.name:
                    score = 0.0
                    break

        eval_status = EvalStatus.PASSED if score == 1.0 else EvalStatus.FAILED
        per_invocation_results.append(
            PerInvocationResult(
                actual_invocation=actual,
                expected_invocation=expected,
                score=score,
                eval_status=eval_status,
            )
        )

    average_score = sum(r.score for r in per_invocation_results) / len(per_invocation_results)
    threshold = eval_metric.criterion.threshold if eval_metric.criterion else 1.0
    overall_eval_status = EvalStatus.PASSED if average_score >= threshold else EvalStatus.FAILED

    return EvaluationResult(
        overall_score=average_score,
        overall_eval_status=overall_eval_status,
        per_invocation_results=per_invocation_results,
    )
