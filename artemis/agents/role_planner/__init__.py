"""Case initialization: identify fixed device participants before allocation."""

import json
import time

from langchain_core.messages import HumanMessage, SystemMessage

from artemis.runtime.multi_device.models import RolePlan
from artemis.services.llm import get_llm, invoke_llm_with_timeout_message, with_fallback
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


async def plan_roles(ctx, request) -> RolePlan:
    started = time.monotonic()
    logger.info(f"[multi-device] role_plan.start source={'supplied' if request.role_plan else 'model'} binding_count={len(request.role_bindings)} feature_count={len(request.feature_catalog)}")
    if request.role_plan is not None:
        plan = request.role_plan
    else:
        # Reuse the configured Planner model; no new provider/settings requirement.
        llm = get_llm(ctx=ctx, name="planner").with_structured_output(RolePlan)
        fallback = get_llm(ctx=ctx, name="planner", use_fallback=True).with_structured_output(
            RolePlan
        )
        messages = [
            SystemMessage(
                content=(
                    "Identify the distinct Android device participants needed by this test case. "
                    "Return one role instance per device participant, with a fixed identity and "
                    "its exact case actor. Reuse the supplied binding IDs when they describe these "
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
        result = await with_fallback(
            main_call=lambda: invoke_llm_with_timeout_message(llm.ainvoke(messages)),
            fallback_call=lambda: invoke_llm_with_timeout_message(fallback.ainvoke(messages)),
        )
        plan = result if isinstance(result, RolePlan) else RolePlan.model_validate(result)
    if plan.unresolved:
        logger.warning(f"[multi-device] role_plan.unresolved count={len(plan.unresolved)}")
        raise ValueError("Role planning needs input: " + "; ".join(plan.unresolved))
    ids = {r.id for r in plan.roles}
    if request.role_bindings and ids != set(request.role_bindings):
        logger.warning(f"[multi-device] role_plan.binding_mismatch planned={sorted(ids)} supplied={sorted(request.role_bindings)}")
        raise ValueError(f"Required role bindings {sorted(ids)} differ from supplied bindings")
    for role in plan.roles:
        for feature, value in (role.feature_requirements or {}).items():
            if feature not in request.feature_catalog:
                raise ValueError(f"Unknown feature {feature!r} for role {role.id}")
            if not request.role_bindings:
                raise ValueError(f"Feature {feature!r} needs a verified device fixture for role {role.id}")
            prepared = request.role_bindings[role.id].prepared_features
            if feature not in prepared or prepared[feature] != value:
                raise ValueError(f"Feature {feature!r} is not prepared for role {role.id}")
    logger.info(f"[multi-device] role_plan.complete roles={sorted(ids)} elapsed_ms={int((time.monotonic()-started)*1000)}")
    return plan
