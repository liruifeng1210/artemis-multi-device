"""Case initialization: identify fixed device participants before allocation."""

import asyncio
import json
import time

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from artemis.runtime.multi_device.models import RolePlan
from artemis.services.llm import get_llm, invoke_llm_with_timeout_message, with_fallback
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


def _catalog_values(entry):
    """Return an explicit value enumeration, if the catalog defines one.

    Plain strings and mappings without ``allowed_values``/``values`` are
    descriptions used by existing callers, not implicit enumerations.
    """
    if isinstance(entry, list):
        return entry
    if isinstance(entry, dict):
        for key in ("allowed_values", "values"):
            values = entry.get(key)
            if isinstance(values, list):
                return values
    return None


async def plan_roles(ctx, request) -> RolePlan:
    started = time.monotonic()
    logger.info(
        f"[multi-device] role_plan.start source=model binding_count={len(request.role_bindings)} feature_count={len(request.feature_catalog)}"
    )
    # Reuse the configured Planner and its fallback. The SDK's case timeout also
    # surrounds this stage; both attempts share this one planning deadline.
    primary_model = get_llm(ctx=ctx, name="planner")
    fallback_model = get_llm(ctx=ctx, name="planner", use_fallback=True)
    configured_budget = getattr(getattr(primary_model, "endpoint", None), "timeout_seconds", None)
    stage_budget = min(
        request.timeout_seconds,
        configured_budget
        if isinstance(configured_budget, (int, float)) and configured_budget > 0
        else 180.0,
    )
    llm = primary_model.with_structured_output(RolePlan, include_raw=True)
    fallback = fallback_model.with_structured_output(RolePlan, include_raw=True)
    messages = [
        SystemMessage(
            content=(
                "Identify the distinct Android device participants needed by this test case. "
                "Return one role instance per device participant, with a fixed identity and "
                "its case actor. Explain actor identity evidence in descriptions or assumptions. "
                "Pronouns and participant counts are valid actor evidence; do not require literal "
                "name substrings. Reuse the supplied binding IDs when they describe these "
                "participants. Do not merge actors to fit available devices. A recipient "
                "replying remains the same actor. Only select feature IDs/values from the "
                "catalog; null means not analyzed. Report material ambiguity in unresolved. "
                "Use account_ref as a binding identity constraint. If participants cannot be "
                "unambiguously mapped to supplied nonempty binding IDs/account references, report unresolved; do not guess. "
                "When no bindings are supplied, derive role IDs and the number of devices solely "
                "from the case. Missing bindings are expected: physical allocation happens AFTER "
                "this plan. An ordinary single-device case needs one role; never default to two. "
                "Do not operate devices or produce milestones."
            )
        ),
        HumanMessage(
            content=json.dumps(
                {
                    "case": request.case,
                    "binding_ids": list(request.role_bindings),
                    "binding_identities": {
                        role: spec.account_ref for role, spec in request.role_bindings.items()
                    },
                    "app_profile": request.app_profile.model_dump()
                    if request.app_profile
                    else None,
                    "feature_catalog": request.feature_catalog,
                },
                ensure_ascii=False,
            )
        ),
    ]
    async with asyncio.timeout(stage_budget):
        for attempt in range(2):
            try:
                result = await with_fallback(
                    main_call=lambda: invoke_llm_with_timeout_message(llm.ainvoke(messages)),
                    fallback_call=lambda: invoke_llm_with_timeout_message(
                        fallback.ainvoke(messages)
                    ),
                )
                if isinstance(result, dict) and "parsing_error" in result:
                    if result["parsing_error"] is not None:
                        raise OutputParserException(str(result["parsing_error"]))
                    result = result.get("parsed")
                plan = result if isinstance(result, RolePlan) else RolePlan.model_validate(result)
            except (ValidationError, OutputParserException) as exc:
                error = "RolePlan schema is invalid: " + str(exc)
            else:
                if plan.unresolved:
                    logger.warning(
                        f"[multi-device] role_plan.unresolved count={len(plan.unresolved)}"
                    )
                    raise ValueError("Role planning needs input: " + "; ".join(plan.unresolved))
                error = _contract_error(plan, request)
                if error is None:
                    break
            if attempt:
                raise ValueError(error)
            logger.warning(f"[multi-device] role_plan.repair reason={error}")
            messages.append(
                HumanMessage(
                    content=(
                        "The previous RolePlan violated this required contract: "
                        + error
                        + ". Return a corrected RolePlan without changing the case actors. "
                        "If the actor mapping is ambiguous, report it in unresolved."
                    )
                )
            )
    ids = {r.id for r in plan.roles}
    logger.info(
        f"[multi-device] role_plan.complete roles={sorted(ids)} elapsed_ms={int((time.monotonic() - started) * 1000)}"
    )
    return plan


def _contract_error(plan: RolePlan, request) -> str | None:
    ids = {r.id for r in plan.roles}
    if request.role_bindings and ids != set(request.role_bindings):
        logger.warning(
            f"[multi-device] role_plan.binding_mismatch planned={sorted(ids)} supplied={sorted(request.role_bindings)}"
        )
        return f"Required role bindings {sorted(ids)} differ from supplied bindings"
    for role in plan.roles:
        for feature, value in (role.feature_requirements or {}).items():
            if feature not in request.feature_catalog:
                return f"Unknown feature {feature!r} for role {role.id}"
            allowed = _catalog_values(request.feature_catalog[feature])
            if allowed is not None and not any(
                type(candidate) is type(value) and candidate == value for candidate in allowed
            ):
                return f"Unsupported value {value!r} for feature {feature!r} on role {role.id}"
    return None


def validate_role_preparation(plan, request):
    """Validate each role's declared device preparation before case actions."""
    for role in plan.roles:
        requirements = role.feature_requirements or {}
        if not requirements:
            continue
        device = request.role_bindings.get(role.id)
        prepared = device.prepared_features if device is not None else {}
        pending = {
            key: value
            for key, value in requirements.items()
            if key not in prepared or prepared[key] != value
        }
        if pending:
            logger.warning(
                f"[multi-device] preparation.rejected role_id={role.id} unmet_requirements={len(pending)}"
            )
            raise ValueError(f"Feature requirements are not prepared for role {role.id}")
        logger.info(
            f"[multi-device] preparation.verified role_id={role.id} requirements={len(requirements)}"
        )
