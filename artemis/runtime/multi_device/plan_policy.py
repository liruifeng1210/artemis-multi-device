"""Deterministic constraints; Markdown remains the authoritative plan."""

import re

from artemis.utils.plan_grammar import parse_plan

ROLE_TAG_RE = re.compile(r"\[role:([a-z][a-z0-9_]*)\]")


def role_of(item) -> str | None:
    match = ROLE_TAG_RE.match(item.text)
    return match.group(1) if match else None


def role_instructions(role_plan) -> str:
    return (
        "\n# Multi-device role contract\n"
        f"Participants (fixed identities, not action names):\n{role_plan.model_dump_json(indent=2)}\n"
        "Every top-level milestone MUST start with exactly one [role:ID] tag after its checkbox. "
        "All device operations and observations in that milestone belong to that participant. "
        "Split cross-participant goals DURING planning. A reply by B remains role B. "
        "Role boundaries take priority over the usual 2-5 milestone guidance. "
        "Children and verify/assert lines inherit their parent role; do not add role tags there. "
        "When adding subtasks, preserve the ENTIRE top-level milestone line verbatim, including "
        "any trailing sentences. Insert child lines only after its final newline; a substring "
        "replacement must not move the remaining milestone text into a child. "
        "Keep the existing checkbox, Loop and check grammar. Preserve tags when editing. "
        "Do not mark another role's milestones complete. After changing the active top-level "
        "milestone, finish this turn and wait for a fresh device observation. "
        "Execute top-level milestones in their written order, including consecutive milestones "
        "for the same role. Do not perform a later milestone's work while the current one is open. "
        "For bounded cross-role repetition expand distinct top-level milestones; do not hide "
        "role switches inside a Loop. Use shared notes for run-specific references and "
        "necessary non-sensitive confirmed facts; keep runtime-bound values in bindings.\n"
    )


class RoleBindingValidator:
    def __init__(self, role_plan):
        self.role_ids = {r.id for r in role_plan.roles}

    def validate(self, content: str, before: str = "", acting_role: str | None = None):
        plan = parse_plan(content)
        if not plan.top_level:
            raise ValueError("The task plan must contain at least one role-bound milestone")
        keys = set()
        for item in plan.items:
            tags = ROLE_TAG_RE.findall(item.text)
            if item.is_top_level:
                role = role_of(item)
                if len(tags) != 1 or item.text.count("[role:") != 1 or role not in self.role_ids:
                    raise ValueError(
                        f"Milestone must start with one declared [role:ID]: {item.text}"
                    )
                if item.key in keys:
                    raise ValueError("Duplicate milestone text: add a distinguishing description")
                keys.add(item.key)
            elif tags or "[role:" in item.text:
                raise ValueError("Subtasks inherit the milestone's role; remove their role tag")
        active_owners = set()
        for item in plan.items:
            if not item.is_active:
                continue
            owner = item if item.is_top_level else plan.parent_of(item)
            if owner is None:
                raise ValueError("An active subtask needs a role-bound milestone")
            active_owners.add(owner.key)
        if len(active_owners) > 1:
            raise ValueError("Only one milestone may be active")
        first_open = next((item for item in plan.top_level if not item.is_done), None)
        if active_owners and first_open is None:
            raise ValueError("A completed milestone cannot contain an active subtask")
        if active_owners and active_owners != {first_open.key}:
            raise ValueError("A later milestone cannot be active before earlier milestones finish")
        # A caller cannot report execution performed by a different device.
        if acting_role and before:
            old = parse_plan(before)
            old_keys = {item.key: item for item in old.top_level}
            target = next((item for item in old.top_level if not item.is_done), None)
            if target and target.status == "!":
                raise ValueError(f"Blocked milestone requires repair: {target.text}")
            if target and role_of(target) != acting_role:
                raise ValueError("Only the current milestone's role may change the plan")
            for item in plan.top_level:
                previous = old_keys.get(item.key)
                newly_done = item.is_done and (previous is None or not previous.is_done)
                if newly_done and role_of(item) != acting_role:
                    raise ValueError("Cannot complete another role's milestone")
                if newly_done and (target is None or item.key != target.key):
                    raise ValueError("Only the current milestone can be completed in this turn")
            if target and target.key not in {item.key for item in plan.top_level}:
                raise ValueError(
                    "Keep the current milestone's identity and role until it is complete. "
                    "You changed its full text (possibly by inserting children before a trailing "
                    "sentence). Preserve this ENTIRE top-level text and add children after it: "
                    + target.text
                )
            for item in plan.top_level:
                if not item.is_done or role_of(item) == acting_role:
                    continue
                if not any(p.key == item.key and p.is_done for p in old.top_level):
                    raise ValueError("Cannot complete another role's milestone")
            # Preserve completed identities and the active role before checking
            # that every original top-level identity remains in order.
            for previous in old.top_level:
                if previous.is_done and any(
                    item.key == previous.key and not item.is_done for item in plan.top_level
                ):
                    raise ValueError(
                        "Only checkpoint or final review may reopen a completed milestone"
                    )
                if previous.is_done and not any(p.key == previous.key for p in plan.top_level):
                    raise ValueError("Completed milestones cannot be deleted or rebound")
                if previous.is_active:
                    description = ROLE_TAG_RE.sub("", previous.text, count=1).strip()
                    for item in plan.top_level:
                        if ROLE_TAG_RE.sub("", item.text, count=1).strip() == description:
                            if role_of(item) != role_of(previous):
                                raise ValueError(
                                    "The active milestone cannot be rebound to another role"
                                )
            # An operator may add later milestones, but cannot erase, rename or
            # move an existing predecessor out of the execution path. Planning
            # and checkpoint/final repair use their separate trusted phases.
            if [item.key for item in plan.top_level[: len(old.top_level)]] != [
                item.key for item in old.top_level
            ]:
                raise ValueError(
                    "Keep every existing milestone's full text and written order; "
                    "add children beneath it or append new milestones after the existing plan"
                )
        return plan

    def target(self, content):
        snapshot = self.validate(content)
        for item in snapshot.top_level:
            if item.is_done:
                continue
            if item.status == "!":
                raise ValueError(f"Blocked milestone requires repair: {item.text}")
            return item
        return None
