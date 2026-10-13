"""Low-cardinality workflow metrics; identifiers stay in structured logs only."""

from aws_lambda_powertools import Metrics
from aws_lambda_powertools.metrics import MetricUnit


def count(name: str, stage: str, value: int = 1) -> None:
    metric = Metrics(namespace="OCRFileWorkflow", service="workflow")
    metric.add_dimension(name="Stage", value=stage)
    metric.add_metric(name=name, unit=MetricUnit.Count, value=value)
    metric.flush_metrics()
