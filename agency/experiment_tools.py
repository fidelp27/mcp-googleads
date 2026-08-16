"""Google Ads Drafts & Experiments tools.

v25 favors `Experiment` + `ExperimentArm` over the older `CampaignDraft`
flow. These tools cover the operator loop that actually matters day-to-
day: see running experiments, compare control vs treatment metrics,
create SETUP experiments, schedule them and end them. Graduation and
promotion (post-experiment merge into the base campaign) are left as a
follow-up because they require per-campaign budget mappings and don't
fit into a single mutation.
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from .api import format_json, mutate, request, search
from .config import (
    client_display_name,
    ensure_customer_allowed,
    ensure_tool_allowed,
    get_settings,
)
from .governance import (
    audited_tool,
    create_approval,
    record_audit,
    update_approval,
    validate_approval,
)


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

_EXPERIMENT_STATUSES = frozenset({"SETUP", "INITIATED", "RUNNING", "GRADUATED", "HALTED", "PROMOTED"})


def _positive_int(value, field: str) -> int:
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return int(text)


def _experiment_resource(customer_id: str, experiment_id: str) -> str:
    return f"customers/{customer_id}/experiments/{_positive_int(experiment_id, 'experiment_id')}"


def _campaign_resource(customer_id: str, campaign_id: str) -> str:
    return f"customers/{customer_id}/campaigns/{_positive_int(campaign_id, 'campaign_id')}"


def _iso_date(value: Optional[str], field: str) -> Optional[str]:
    if not value:
        return None
    import re
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        raise ValueError(f"{field} must be YYYY-MM-DD (got {value!r})")
    return value.strip()


def register_experiment_tools(mcp) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def list_experiments(
        customer_id: str = Field(description="Google Ads customer ID"),
        status: Optional[str] = Field(
            default=None,
            description="Optional filter: SETUP/INITIATED/RUNNING/GRADUATED/HALTED/PROMOTED",
        ),
    ) -> str:
        """List experiments in the account with status, dates and promotion state."""
        ensure_tool_allowed("list_experiments")
        cid = ensure_customer_allowed(customer_id)
        filters: list[str] = []
        if status:
            clean = status.strip().upper()
            if clean not in _EXPERIMENT_STATUSES:
                raise ValueError(
                    f"status must be one of {sorted(_EXPERIMENT_STATUSES)}"
                )
            filters.append(f"experiment.status = '{clean}'")
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = search(
            cid,
            f"""
            SELECT experiment.resource_name, experiment.experiment_id,
                   experiment.name, experiment.description, experiment.suffix,
                   experiment.type, experiment.status, experiment.promote_status,
                   experiment.start_date, experiment.end_date,
                   experiment.sync_enabled, experiment.long_running_operation
            FROM experiment
            {where}
            ORDER BY experiment.start_date DESC
            """,
        )
        return format_json({
            "customer_id": cid,
            "tool": "list_experiments",
            "fresh": True,
            "count": len(rows),
            "experiments": [row.get("experiment", {}) for row in rows],
        })

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_experiment(
        customer_id: str = Field(description="Google Ads customer ID"),
        experiment_id: str = Field(description="Experiment ID"),
    ) -> str:
        """Return one experiment plus its arms (control + treatment) and their campaigns."""
        ensure_tool_allowed("get_experiment")
        cid = ensure_customer_allowed(customer_id)
        resource = _experiment_resource(cid, experiment_id)

        experiments = search(
            cid,
            f"""
            SELECT experiment.resource_name, experiment.experiment_id,
                   experiment.name, experiment.description, experiment.suffix,
                   experiment.type, experiment.status, experiment.promote_status,
                   experiment.start_date, experiment.end_date,
                   experiment.sync_enabled, experiment.goals,
                   experiment.long_running_operation
            FROM experiment
            WHERE experiment.resource_name = '{resource}'
            """,
        )
        if not experiments:
            return format_json({
                "customer_id": cid,
                "tool": "get_experiment",
                "fresh": True,
                "error": f"experiment {experiment_id} not found",
            })

        arms = search(
            cid,
            f"""
            SELECT experiment_arm.resource_name, experiment_arm.name,
                   experiment_arm.control, experiment_arm.traffic_split,
                   experiment_arm.experiment,
                   experiment_arm.campaigns, experiment_arm.in_design_campaigns
            FROM experiment_arm
            WHERE experiment_arm.experiment = '{resource}'
            """,
        )
        return format_json({
            "customer_id": cid,
            "tool": "get_experiment",
            "fresh": True,
            "experiment": experiments[0].get("experiment", {}),
            "arms": [row.get("experimentArm", {}) for row in arms],
        })

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_experiment_results(
        customer_id: str = Field(description="Google Ads customer ID"),
        experiment_id: str = Field(description="Experiment ID"),
        days: int = Field(default=30, description="Lookback window 1-365"),
    ) -> str:
        """Compare control-arm vs treatment-arm campaign metrics for the experiment."""
        ensure_tool_allowed("get_experiment_results")
        cid = ensure_customer_allowed(customer_id)
        resource = _experiment_resource(cid, experiment_id)

        arms = search(
            cid,
            f"""
            SELECT experiment_arm.resource_name, experiment_arm.name,
                   experiment_arm.control, experiment_arm.traffic_split,
                   experiment_arm.campaigns
            FROM experiment_arm
            WHERE experiment_arm.experiment = '{resource}'
            """,
        )
        if not arms:
            return format_json({
                "customer_id": cid,
                "tool": "get_experiment_results",
                "fresh": True,
                "error": f"experiment {experiment_id} has no arms or does not exist",
            })

        arm_reports: list[dict[str, Any]] = []
        for row in arms:
            arm = row.get("experimentArm", {}) or {}
            campaigns = arm.get("campaigns") or []
            metrics = _metrics_for_campaigns(cid, campaigns, days)
            arm_reports.append(
                {
                    "name": arm.get("name"),
                    "control": bool(arm.get("control")),
                    "traffic_split": int(arm.get("trafficSplit") or 0),
                    "campaigns": campaigns,
                    "metrics": metrics,
                }
            )

        comparison = _compare_arms(arm_reports)
        return format_json({
            "customer_id": cid,
            "tool": "get_experiment_results",
            "fresh": True,
            "period_days": days,
            "arms": arm_reports,
            "comparison": comparison,
        })

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_experiment(
        customer_id: str = Field(description="Google Ads customer ID"),
        name: str = Field(description="Experiment name (unique in the account)"),
        base_campaign_id: str = Field(
            description="Existing campaign to use as the control base"
        ),
        suffix: str = Field(
            description="Suffix appended to experiment campaign names (max 34 chars)"
        ),
        traffic_split_percent: int = Field(
            description="Percent of eligible traffic given to the treatment arm (1-100)",
            ge=1,
            le=100,
        ),
        description: Optional[str] = Field(default=None),
        start_date: Optional[str] = Field(
            default=None, description="Optional YYYY-MM-DD (defaults to now on schedule)"
        ),
        end_date: Optional[str] = Field(
            default=None, description="Optional YYYY-MM-DD"
        ),
        dry_run: bool = Field(default=True),
        approval_id: Optional[str] = Field(default=None),
        confirmation_text: Optional[str] = Field(default=None),
    ) -> str:
        """Create a SETUP experiment plus a control arm pointing at base_campaign_id.

        The treatment arm and its cloned campaigns must be created separately
        via the Google Ads UI or an ExperimentArm creation call the operator
        supplies. This tool intentionally scopes creation to the primitives
        that produce a syntactically valid SETUP state; scheduling still
        requires arm coverage on both control and treatment.
        """
        ensure_tool_allowed("create_experiment", write=True)
        cid = ensure_customer_allowed(customer_id)
        if len(suffix.strip()) > 34:
            raise ValueError("suffix must be at most 34 characters")
        base_resource = _campaign_resource(cid, base_campaign_id)
        experiment_payload: dict[str, Any] = {
            "name": name.strip(),
            "suffix": suffix.strip(),
            "type": "SEARCH_CUSTOM",
            "status": "SETUP",
        }
        if description:
            experiment_payload["description"] = description.strip()
        if start_date := _iso_date(start_date, "start_date"):
            experiment_payload["startDate"] = start_date
        if end_date := _iso_date(end_date, "end_date"):
            experiment_payload["endDate"] = end_date

        experiment_op = [{"create": experiment_payload}]
        payload = {
            "operation": "create_experiment",
            "name": name.strip(),
            "base_campaign_id": str(base_campaign_id),
            "suffix": suffix.strip(),
            "traffic_split_percent": int(traffic_split_percent),
        }
        return _dispatch_experiment_write(
            tool_name="create_experiment",
            customer_id=cid,
            payload=payload,
            expected_confirmation=name.strip(),
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
            executor=lambda: _execute_create_experiment(
                cid,
                experiment_op,
                base_resource,
                int(traffic_split_percent),
                name.strip(),
            ),
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def schedule_experiment(
        customer_id: str = Field(description="Google Ads customer ID"),
        experiment_id: str = Field(description="Experiment ID (must be in SETUP)"),
        validate_only_from_google: bool = Field(
            default=False,
            description="Ask Google to validate feasibility without starting the LRO",
        ),
        dry_run: bool = Field(default=True),
        approval_id: Optional[str] = Field(default=None),
        confirmation_text: Optional[str] = Field(default=None),
    ) -> str:
        """Schedule a SETUP experiment (starts the long-running operation)."""
        ensure_tool_allowed("schedule_experiment", write=True)
        cid = ensure_customer_allowed(customer_id)
        resource = _experiment_resource(cid, experiment_id)
        payload = {
            "operation": "schedule_experiment",
            "experiment_id": str(experiment_id),
            "validate_only": bool(validate_only_from_google),
        }
        return _dispatch_experiment_write(
            tool_name="schedule_experiment",
            customer_id=cid,
            payload=payload,
            expected_confirmation=f"SCHEDULE {experiment_id}",
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
            executor=lambda: request(
                "POST",
                f"{resource}:scheduleExperiment",
                customer_id=cid,
                body={"validateOnly": bool(validate_only_from_google)},
            ),
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    @audited_tool
    def end_experiment(
        customer_id: str = Field(description="Google Ads customer ID"),
        experiment_id: str = Field(description="Experiment ID"),
        dry_run: bool = Field(default=True),
        approval_id: Optional[str] = Field(default=None),
        confirmation_text: Optional[str] = Field(default=None),
    ) -> str:
        """End a RUNNING experiment. Treatment traffic returns to control."""
        ensure_tool_allowed("end_experiment", write=True)
        cid = ensure_customer_allowed(customer_id)
        resource = _experiment_resource(cid, experiment_id)
        payload = {"operation": "end_experiment", "experiment_id": str(experiment_id)}
        return _dispatch_experiment_write(
            tool_name="end_experiment",
            customer_id=cid,
            payload=payload,
            expected_confirmation=f"END {experiment_id}",
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
            executor=lambda: request(
                "POST",
                f"{resource}:endExperiment",
                customer_id=cid,
                body={},
            ),
        )


# ---------- internals ----------


def _metrics_for_campaigns(customer_id: str, campaigns: list[str], days: int) -> dict:
    if not campaigns:
        return {
            "impressions": 0,
            "clicks": 0,
            "cost_micros": 0,
            "conversions": 0.0,
            "conversions_value": 0.0,
        }
    if not 1 <= days <= 365:
        raise ValueError("days must be between 1 and 365")
    resource_names = ",".join(f"'{r}'" for r in campaigns)
    from datetime import date, timedelta
    end = date.today()
    start = end - timedelta(days=days - 1)
    rows = search(
        customer_id,
        f"""
        SELECT campaign.resource_name,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value
        FROM campaign
        WHERE campaign.resource_name IN ({resource_names})
          AND segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'
        """,
    )
    totals = {
        "impressions": 0,
        "clicks": 0,
        "cost_micros": 0,
        "conversions": 0.0,
        "conversions_value": 0.0,
    }
    for row in rows:
        metrics = row.get("metrics", {}) or {}
        totals["impressions"] += int(metrics.get("impressions") or 0)
        totals["clicks"] += int(metrics.get("clicks") or 0)
        totals["cost_micros"] += int(metrics.get("costMicros") or 0)
        totals["conversions"] += float(metrics.get("conversions") or 0)
        totals["conversions_value"] += float(metrics.get("conversionsValue") or 0)
    return totals


def _compare_arms(arm_reports: list[dict]) -> dict:
    """Pair control vs treatment and compute deltas."""
    control = next((a for a in arm_reports if a["control"]), None)
    treatments = [a for a in arm_reports if not a["control"]]
    if not control or not treatments:
        return {
            "warning": (
                "Need both a control arm and at least one treatment arm to compute deltas."
            )
        }

    def _rate(numerator: float, denominator: float) -> float | None:
        return round(numerator / denominator, 6) if denominator else None

    def _delta_pct(current: float, base: float) -> float | None:
        if base == 0:
            return None
        return round((current - base) / base * 100, 2)

    control_metrics = control["metrics"]
    control_ctr = _rate(control_metrics["clicks"], control_metrics["impressions"])
    control_cvr = _rate(control_metrics["conversions"], control_metrics["clicks"])
    control_cpa = _rate(control_metrics["cost_micros"] / 1_000_000, control_metrics["conversions"])

    comparisons: list[dict[str, Any]] = []
    for treatment in treatments:
        tm = treatment["metrics"]
        t_ctr = _rate(tm["clicks"], tm["impressions"])
        t_cvr = _rate(tm["conversions"], tm["clicks"])
        t_cpa = _rate(tm["cost_micros"] / 1_000_000, tm["conversions"])
        comparisons.append(
            {
                "treatment_arm": treatment["name"],
                "traffic_split": treatment["traffic_split"],
                "control_metrics": control_metrics,
                "treatment_metrics": tm,
                "rates": {
                    "control_ctr": control_ctr,
                    "treatment_ctr": t_ctr,
                    "control_conversion_rate": control_cvr,
                    "treatment_conversion_rate": t_cvr,
                    "control_cpa": control_cpa,
                    "treatment_cpa": t_cpa,
                },
                "deltas_pct": {
                    "clicks": _delta_pct(tm["clicks"], control_metrics["clicks"]),
                    "conversions": _delta_pct(
                        tm["conversions"], control_metrics["conversions"]
                    ),
                    "cost_micros": _delta_pct(
                        tm["cost_micros"], control_metrics["cost_micros"]
                    ),
                    "cpa": _delta_pct(t_cpa, control_cpa) if control_cpa and t_cpa else None,
                },
            }
        )
    return {"control_arm": control["name"], "treatments": comparisons}


def _dispatch_experiment_write(
    *,
    tool_name: str,
    customer_id: str,
    payload: dict[str, Any],
    expected_confirmation: str,
    dry_run: bool,
    approval_id: Optional[str],
    confirmation_text: Optional[str],
    executor,
) -> str:
    settings = get_settings()
    if not dry_run and settings.read_only_mode:
        raise PermissionError("READ_ONLY_MODE=true blocks experiment mutations")

    if dry_run:
        approval = create_approval(
            tool_name,
            customer_id,
            payload,
            expected_confirmation=expected_confirmation,
        )
        record_audit(
            tool_name,
            customer_id,
            payload,
            "validated",
            {"note": "dry-run does not touch the experiment API"},
            dry_run=True,
            operation_id=approval["approval_id"],
        )
        return format_json(
            {
                "status": "validated",
                "executed": False,
                "approval_id": approval["approval_id"],
                "expires_at": approval["expires_at"],
                "customer_id": customer_id,
                "client_name": client_display_name(customer_id),
                "tool": tool_name,
                "expected_confirmation": expected_confirmation,
                "preview": payload,
                "next_step": (
                    f"Call {tool_name} again with the same arguments, dry_run=false, "
                    f"this approval_id and confirmation_text='{expected_confirmation}'."
                ),
            }
        )

    if not approval_id:
        raise PermissionError("approval_id is required when dry_run=false")
    approval = validate_approval(
        approval_id,
        tool_name,
        customer_id,
        payload,
        confirmation_text,
    )
    try:
        result = executor()
        audit_id = record_audit(
            tool_name,
            customer_id,
            payload,
            "executed",
            result,
            dry_run=False,
            operation_id=approval_id,
        )
        update_approval(approval_id, status="executed", audit_id=audit_id)
        return format_json(
            {
                "status": "executed",
                "executed": True,
                "approval_id": approval_id,
                "audit_id": audit_id,
                "customer_id": customer_id,
                "client_name": client_display_name(customer_id),
                "result": result,
            }
        )
    except Exception as error:
        record_audit(
            tool_name,
            customer_id,
            payload,
            "error",
            str(error),
            dry_run=False,
            operation_id=approval_id,
        )
        raise


def _execute_create_experiment(
    customer_id: str,
    experiment_ops: list[dict],
    base_campaign_resource: str,
    traffic_split_percent: int,
    experiment_name: str,
) -> dict:
    """Create the Experiment then a control arm pointing at base_campaign."""
    experiment_response = mutate(
        customer_id, "experiments", experiment_ops, validate_only=False
    )
    experiment_resource = (
        experiment_response.get("results", [{}])[0].get("resourceName")
    )
    if not experiment_resource:
        raise RuntimeError(
            f"experiments:mutate did not return a resourceName; got {experiment_response}"
        )
    arm_payload = {
        "create": {
            "name": f"{experiment_name} · control",
            "experiment": experiment_resource,
            "control": True,
            "trafficSplit": 100 - traffic_split_percent,
            "campaigns": [base_campaign_resource],
        }
    }
    arm_response = mutate(
        customer_id, "experimentArms", [arm_payload], validate_only=False
    )
    return {
        "experiment_resource_name": experiment_resource,
        "experiment_response": experiment_response,
        "control_arm_response": arm_response,
        "next_steps": [
            "Create the treatment arm with create_experiment_arm (UI or API).",
            "Call schedule_experiment(experiment_id) to start the LRO.",
        ],
    }
