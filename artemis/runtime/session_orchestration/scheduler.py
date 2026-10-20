"""Deterministic dependency/contract gates; models cannot override task history."""

from datetime import UTC, datetime


class Scheduler:
    def __init__(self, workflow):
        self.roles = {r.role_id for r in workflow.roles}
        self.tasks = {}
        self.results = {}
        self.running = set()
        self.executed = set()
        self.recoveries = {}
        self.invalidated = set()
        self.revision = 0
        self.expected_cancellations = {
            (item.role_id, item.display_name) for item in workflow.expected_cancellations
        }
        self.authorized_cancellations = set()
        self.result_attempts = {}
        self.apply(workflow.tasks, 0)

    def apply(self, tasks, base_revision, recoveries=()):
        if base_revision != self.revision:
            raise ValueError("Stale plan revision")
        updated = dict(self.tasks)
        for task in tasks:
            if task.role_id not in self.roles:
                raise ValueError("Unknown role")
            if "cancelled" in task.expected_terminal_statuses and (
                task.role_id, task.display_name
            ) not in self.expected_cancellations:
                raise ValueError("Expected cancellation requires workflow authorization")
            if (
                task.task_id in self.running
                or task.task_id in self.results
                or task.task_id in self.executed
            ):
                if task == self.tasks[task.task_id]:
                    continue
                raise ValueError("Cannot revise executed task")
            updated[task.task_id] = task
        edges = {}
        cancelled_bindings = [
            (task.role_id, task.display_name) for task in updated.values()
            if "cancelled" in task.expected_terminal_statuses
        ]
        if len(cancelled_bindings) != len(set(cancelled_bindings)):
            raise ValueError("Expected cancellation task binding must be unique")
        for task in updated.values():
            parents = {d.task_id for d in task.depends_on}
            parents |= {
                v["from_task"]
                for v in task.spec.inputs.values()
                if isinstance(v, dict) and "from_task" in v
            }
            if not parents <= updated.keys():
                raise ValueError("Unknown task dependency")
            for binding in task.spec.inputs.values():
                if isinstance(binding, dict) and "from_task" in binding:
                    if set(binding) - {"from_task", "key", "max_age_seconds"}:
                        raise ValueError("Unsupported input binding")
                    source = updated[binding["from_task"]]
                    if binding.get("key") not in source.spec.outputs_schema.get("properties", {}):
                        raise ValueError("Binding must reference a declared output")
                    age = binding.get("max_age_seconds")
                    if age is not None and (not isinstance(age, (int, float)) or age <= 0):
                        raise ValueError("Input freshness must be positive seconds")
            edges[task.task_id] = parents
        seen, visiting = set(), set()

        def visit(key):
            if key in visiting:
                raise ValueError("Cyclic workflow")
            if key in seen:
                return
            visiting.add(key)
            for parent in edges[key]:
                visit(parent)
            visiting.remove(key)
            seen.add(key)

        for key in edges:
            visit(key)
        recovery_map = dict(self.recoveries)
        for recovery in recoveries:
            failed = self.results.get(recovery.failed_task_id)
            if failed is None or (
                failed.status == "succeeded" and recovery.failed_task_id not in self.invalidated
            ):
                raise ValueError("Recovery must identify a terminal unsuccessful task")
            if recovery.failed_task_id in recovery.replacement_task_ids:
                raise ValueError("Recovery cannot replace itself")
            for key in recovery.replacement_task_ids:
                if key not in updated:
                    raise ValueError("Unknown recovery task")
                original = self.tasks[recovery.failed_task_id]
                replacement = updated[key]
                required = {(c.id, c.description) for c in original.spec.completion if c.required}
                provided = {
                    (c.id, c.description) for c in replacement.spec.completion if c.required
                }
                if (
                    replacement.role_id != original.role_id
                    or not required <= provided
                    or replacement.spec.outputs_schema != original.spec.outputs_schema
                ):
                    raise ValueError(
                        "Recovery must preserve the original role and acceptance contract"
                    )
                if (
                    failed.side_effect_state == "uncertain"
                    or recovery.failed_task_id in self.invalidated
                ) and updated[key].spec.effect_policy != "read_only":
                    raise ValueError("Uncertain effects require a read-only reconciliation task")
            recovery_map[recovery.failed_task_id] = recovery.replacement_task_ids

        # Recovery edges must also be acyclic.
        def recoverable(key, trail):
            if key in trail:
                raise ValueError("Cyclic recovery")
            for child in recovery_map.get(key, []):
                recoverable(child, trail | {key})

        for key in recovery_map:
            recoverable(key, set())
        self.tasks = updated
        self.recoveries = recovery_map
        self.revision += 1

    def ready(self, task):
        if task.task_id in self.running or task.task_id in self.results:
            return False
        conditions = []
        for dep in task.depends_on:
            value = self.results.get(dep.task_id)
            valid = (
                dep.task_id not in self.invalidated
                and value is not None
                and value.status in dep.statuses
            )
            if valid and dep.check_id:
                valid = any(c.id == dep.check_id and c.verdict == dep.verdict for c in value.checks)
            if valid and dep.output_key:
                valid = value.outputs.get(dep.output_key) == dep.equals
            if valid and dep.max_age_seconds is not None:
                valid = (
                    datetime.now(UTC) - datetime.fromisoformat(value.observed_at)
                ).total_seconds() <= dep.max_age_seconds
            conditions.append(valid)
        if conditions and not (
            all(conditions) if task.dependency_mode == "all" else any(conditions)
        ):
            return False
        return all(
            v["from_task"] not in self.invalidated
            and v["from_task"] in self.results
            and self.results[v["from_task"]].status == "succeeded"
            for v in task.spec.inputs.values()
            if isinstance(v, dict) and "from_task" in v
        )

    def invalidate(self, task_ids):
        """Invalidate executed descendants without rewriting historical results."""
        affected = set(task_ids)
        while True:
            before = len(affected)
            for key, task in self.tasks.items():
                if key not in self.executed and key not in self.results:
                    continue
                sources = {dep.task_id for dep in task.depends_on}
                sources.update(
                    v["from_task"]
                    for v in task.spec.inputs.values()
                    if isinstance(v, dict) and "from_task" in v
                )
                if sources & affected:
                    affected.add(key)
            if len(affected) == before:
                break
        self.invalidated.update(affected)
        return affected

    @property
    def expected_cancelled_tasks(self):
        return {
            key for key, result in self.results.items()
            if key not in self.invalidated
            and result.status == "cancelled"
            and "cancelled" in self.tasks[key].expected_terminal_statuses
            and (key, self.result_attempts.get(key)) in self.authorized_cancellations
        }

    def record_result(self, task_id, attempt_id, result):
        self.results[task_id] = result
        self.result_attempts[task_id] = attempt_id

    @property
    def completed(self):
        def succeeded(key):
            if key in self.recoveries:
                return all(succeeded(child) for child in self.recoveries[key])
            return (
                key not in self.invalidated
                and key in self.results
                and (
                    (self.results[key].status == "succeeded"
                     and "succeeded" in self.tasks[key].expected_terminal_statuses)
                    or key in self.expected_cancelled_tasks
                )
            )

        return bool(self.tasks) and all(succeeded(key) for key in self.tasks)
