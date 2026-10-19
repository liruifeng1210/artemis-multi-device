"""Role-aware check identity and evidence routing for the native Checker pipeline."""

from collections import Counter

from artemis.graph.checkpoints import TestSummary, resolve_item_status
from artemis.utils.plan_grammar import missing_check_items


class RoleCheckPolicy:
    def __init__(self, runtime):
        self.runtime = runtime

    @staticmethod
    def dropped_checks(before, after):
        missing = missing_check_items(before, after)
        old_top, new_top = before.top_level, after.top_level
        new_keys = {item.key for item in new_top}
        moved_parents = {}
        for index, old_item in enumerate(old_top):
            if old_item.key in new_keys or index >= len(new_top):
                continue
            new_item = new_top[index]
            if old_item.role_id == new_item.role_id:
                moved_parents[old_item.key] = new_item.key
        still_declared = Counter((ci.kind, ci.text, ci.parent_key) for ci in after.check_items)
        dropped = []
        for ci in missing:
            parent = moved_parents.get(ci.parent_key, ci.parent_key)
            signature = (ci.kind, ci.text, parent)
            if still_declared.get(signature, 0) > 0:
                still_declared[signature] -= 1
            else:
                dropped.append(ci)
        return dropped

    @staticmethod
    def verdict_key(check_items, verdict):
        key = getattr(verdict, "check_key", None)
        if key and any(ci.key == key for ci in check_items):
            return str(key)
        matches = [
            ci.key for ci in check_items
            if ci.kind == getattr(verdict, "kind", None)
            and ci.text == getattr(verdict, "item_text", None)
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def retired(ctx):
        retired = getattr(ctx, "guidance_retired_check_keys", None)
        return retired if isinstance(retired, set) else set()

    def retire(self, ctx, items):
        from artemis.graph.checkpoints import _clear_finding, _finding_items, _finding_registry, sync_finding_lines

        incoming = {str(item.key) for item in items}
        if not incoming:
            return
        retired = self.retired(ctx) | incoming
        ctx.guidance_retired_check_keys = retired
        registry = _finding_registry(ctx)
        produced_by = _finding_items(ctx)
        stale = [cid for cid in list(registry) if produced_by.get(cid) and produced_by[cid] <= retired]
        for cid in stale:
            _clear_finding(ctx, cid)
        if stale:
            sync_finding_lines(ctx)

    def reinstate(self, ctx, items):
        retired = self.retired(ctx)
        if retired:
            ctx.guidance_retired_check_keys = retired - {str(item.key) for item in items}

    def active_verdicts(self, ctx, checkpoint, verdicts):
        retired = self.retired(ctx)
        return [v for v in verdicts if self.verdict_key(checkpoint.check_items, v) not in retired]

    def finding_keys(self, check_items, verdicts):
        return {
            key for v in verdicts
            if (key := self.verdict_key(check_items, v)) is not None
        }

    def normalize_report(self, report, check_items):
        from artemis.agents.checker.checker import CheckReport, CheckVerdict

        normalized = []
        for ci in check_items:
            candidates = [
                v for v in report.verdicts
                if v.check_key == ci.key and v.kind == ci.kind and v.item_text == ci.text
            ]
            if not candidates and sum(c.kind == ci.kind and c.text == ci.text for c in check_items) == 1:
                candidates = [
                    v for v in report.verdicts
                    if not v.check_key and v.kind == ci.kind and v.item_text == ci.text
                ]
            if candidates:
                v = candidates[-1].model_copy(update={"check_key": ci.key, "role_id": ci.role_id})
                if v.status == "failed" and not v.evidence.strip():
                    v = v.model_copy(update={
                        "status": "inconclusive",
                        "evidence": "failed verdict lacked concrete evidence",
                    })
            else:
                v = CheckVerdict(
                    item_text=ci.text,
                    kind=ci.kind,
                    status="inconclusive",
                    evidence="No unambiguous verdict produced for this role's check",
                    check_key=ci.key,
                    role_id=ci.role_id,
                )
            normalized.append(v)
        return CheckReport(verdicts=normalized, unmet_subgoals=list(report.unmet_subgoals))

    async def run_checkpoint(self, pending, callback, *, step_id=None):
        return await self.runtime.run_checkpoint(
            pending.role_id,
            callback,
            step_id=step_id,
            subgoal_hash=pending.checkpoint_id,
        )

    async def final_observations(self):
        return await self.runtime.final_observations()

    def mark_final(self, status):
        self.runtime.final_review_status = status

    def annotate_context(self, ctx, checkpoint):
        return self.runtime.contexts.get(checkpoint.role_id, ctx)

    def reopen(self, subgoal_key):
        return self.runtime.reopen_milestone(subgoal_key)

    def write_system_plan(self, path, before, after):
        from artemis.runtime.execution_scope import ExecutionScope

        return self.runtime.commit_plan(path, before, after, ExecutionScope(phase="system"))

    def test_summary(self, check_items, records, retired=None):
        summary = TestSummary()
        declared = {ci.key: ci for ci in check_items}
        grouped = {}
        for row in records:
            key = row.get("check_key")
            if not key:
                matches = [
                    ci.key for ci in check_items
                    if ci.kind == row.get("kind")
                    and ci.text == row.get("item_text")
                    and ci.role_id == row.get("role_id")
                    and (not ci.parent_key or row.get("checkpoint_id") in (ci.parent_key, "final"))
                ]
                key = matches[0] if len(matches) == 1 else None
            if key:
                grouped.setdefault(key, []).append(row)
        for key in set(declared) | set(grouped):
            rows = grouped.get(key, [])
            ci = declared.get(key)
            kind = ci.kind if ci else rows[-1].get("kind")
            text = ci.text if ci else rows[-1].get("item_text")
            role = ci.role_id if ci else rows[-1].get("role_id")
            status = resolve_item_status(kind, rows)
            if ci is None and key in (retired or set()):
                summary.retired += 1
                summary.retired_items.append(
                    {"kind": kind, "item_text": text, "role_id": role, "last_status": status}
                )
            elif status == "passed":
                summary.passed += 1
            elif status == "failed":
                summary.failed += 1
                failures = [r for r in rows if r.get("status") == "failed"]
                summary.failed_items.append({
                    "kind": kind,
                    "item_text": text,
                    "role_id": role,
                    "evidence": failures[-1].get("evidence", "") if failures else "",
                })
            elif status == "inconclusive":
                summary.inconclusive += 1
            else:
                summary.unchecked += 1
        return summary
