"""Impact-tier invalidation engine (T0–T3) with typed traversal + hash short-circuit."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set

from .model import ArtifactKind, ImpactTier, NodeStatus
from .store import ProvenanceStore


@dataclass
class RerunStep:
    step_node_id: str
    skill: str
    reason: str
    input_versions: Dict[str, str] = field(default_factory=dict)  # logical_id -> version_id
    mode: str = "full"  # full | figures_only | docs_only


@dataclass
class ImpactPlan:
    tier: str
    dirty_nodes: List[str]
    rerun_plan: List[RerunStep]
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "dirty_nodes": self.dirty_nodes,
            "rerun_plan": [asdict(r) for r in self.rerun_plan],
            "notes": self.notes,
        }


def classify_change(
    *,
    kind: Optional[str] = None,
    param_section: Optional[str] = None,
    changed_keys: Optional[List[str]] = None,
    explicit_tier: Optional[ImpactTier | str] = None,
) -> ImpactTier:
    """Classify a change into an impact tier.

    Rules (thin slice):
    - explicit_tier wins
    - kind in {doc} or presentation-only doc edits → T0
    - param_section == presentation → T1
    - param_section == compute → T2
    - kind in {data} or upstream input swap → T3
    """
    if explicit_tier is not None:
        return ImpactTier(explicit_tier) if not isinstance(explicit_tier, ImpactTier) else explicit_tier
    if kind in (ArtifactKind.DOC.value, "prose", "text"):
        return ImpactTier.T0
    if param_section == "presentation":
        return ImpactTier.T1
    if param_section == "compute":
        return ImpactTier.T2
    if kind in (ArtifactKind.DATA.value, "input", "upstream"):
        return ImpactTier.T3
    # figure-only presentation-ish
    if kind == ArtifactKind.FIGURE.value:
        return ImpactTier.T1
    return ImpactTier.T2


class ImpactEngine:
    def __init__(self, store: ProvenanceStore):
        self.store = store

    def plan_from_artifact_change(
        self,
        logical_id: str,
        tier: ImpactTier | str,
        *,
        new_content_hash: Optional[str] = None,
        old_content_hash: Optional[str] = None,
    ) -> ImpactPlan:
        tier = ImpactTier(tier) if not isinstance(tier, ImpactTier) else tier
        dirty: Set[str] = {logical_id}
        rerun: List[RerunStep] = []

        if tier == ImpactTier.T0:
            # Only writing/doc consumers — mark doc-producing steps that read this
            for step_id in self.store.downstream_steps(logical_id):
                skill = step_id.split(":", 1)[-1]
                # For thin slice we don't have a writer stage yet; mark artifact dirty only
                dirty.add(step_id)
            return ImpactPlan(
                tier=tier.value,
                dirty_nodes=sorted(dirty),
                rerun_plan=[],
                notes="T0: prose/doc change — no hard data/code rerun",
            )

        if tier == ImpactTier.T1:
            # Re-render figures from producing steps; data outputs stay valid
            producers = self._producer_steps(logical_id)
            for step_id in producers:
                skill = self._skill_of(step_id)
                dirty.add(step_id)
                for art in self.store.produced_artifacts(step_id):
                    meta_kind = self._artifact_kind(art)
                    if meta_kind == ArtifactKind.FIGURE.value:
                        dirty.add(art)
                rerun.append(
                    RerunStep(
                        step_node_id=step_id,
                        skill=skill,
                        reason="presentation param / figure edit",
                        input_versions=self._latest_input_versions(step_id),
                        mode="figures_only",
                    )
                )
            return ImpactPlan(
                tier=tier.value,
                dirty_nodes=sorted(dirty),
                rerun_plan=rerun,
                notes="T1: figures_only — data outputs not cascaded",
            )

        # T2 / T3: cascade through consumers with optional hash short-circuit
        hash_changed = (
            new_content_hash is not None
            and old_content_hash is not None
            and new_content_hash != old_content_hash
        )
        if tier == ImpactTier.T2 and new_content_hash and old_content_hash and not hash_changed:
            producers = self._producer_steps(logical_id)
            for step_id in producers:
                dirty.add(step_id)
                rerun.append(
                    RerunStep(
                        step_node_id=step_id,
                        skill=self._skill_of(step_id),
                        reason="compute param change but output hash unchanged (short-circuit)",
                        input_versions=self._latest_input_versions(step_id),
                        mode="full",
                    )
                )
            return ImpactPlan(
                tier=tier.value,
                dirty_nodes=sorted(dirty),
                rerun_plan=rerun,
                notes="T2 short-circuit: output hash unchanged — no downstream cascade",
            )

        # Walk consumer steps; for each, mark dirty; after hypothetical rerun,
        # cascade only if data-output hashes would change. At plan time we
        # conservatively mark the subgraph dirty and leave short-circuit to
        # apply_after_rerun().
        frontier = list(self.store.downstream_steps(logical_id))
        seen_steps: Set[str] = set()
        while frontier:
            step_id = frontier.pop(0)
            if step_id in seen_steps:
                continue
            seen_steps.add(step_id)
            dirty.add(step_id)
            for art in self.store.produced_artifacts(step_id):
                dirty.add(art)
                # continue cascade through data artifacts
                if self._artifact_kind(art) == ArtifactKind.DATA.value or tier == ImpactTier.T3:
                    for nxt in self.store.downstream_steps(art):
                        if nxt not in seen_steps:
                            frontier.append(nxt)
            rerun.append(
                RerunStep(
                    step_node_id=step_id,
                    skill=self._skill_of(step_id),
                    reason=f"{tier.value} cascade from {logical_id}",
                    input_versions=self._latest_input_versions(step_id),
                    mode="full",
                )
            )

        return ImpactPlan(
            tier=tier.value,
            dirty_nodes=sorted(dirty),
            rerun_plan=rerun,
            notes=f"{tier.value}: typed cascade; hash short-circuit applied after each rerun",
        )

    def plan_param_change(
        self,
        step_node_id: str,
        section: str,
        changed_keys: Optional[List[str]] = None,
    ) -> ImpactPlan:
        tier = classify_change(param_section=section, changed_keys=changed_keys)
        dirty: Set[str] = {step_node_id}
        produced = self.store.produced_artifacts(step_node_id)

        if tier == ImpactTier.T1:
            for art in produced:
                if self._artifact_kind(art) == ArtifactKind.FIGURE.value:
                    dirty.add(art)
            return ImpactPlan(
                tier=tier.value,
                dirty_nodes=sorted(dirty),
                rerun_plan=[
                    RerunStep(
                        step_node_id=step_node_id,
                        skill=self._skill_of(step_node_id),
                        reason=f"presentation keys {changed_keys or []}",
                        input_versions=self._latest_input_versions(step_node_id),
                        mode="figures_only",
                    )
                ],
                notes="T1 presentation param change",
            )

        # T2: mark step + produced; cascade data consumers only after hash check
        for art in produced:
            dirty.add(art)
        plan = ImpactPlan(
            tier=ImpactTier.T2.value,
            dirty_nodes=sorted(dirty),
            rerun_plan=[
                RerunStep(
                    step_node_id=step_node_id,
                    skill=self._skill_of(step_node_id),
                    reason=f"compute keys {changed_keys or []}",
                    input_versions=self._latest_input_versions(step_node_id),
                    mode="full",
                )
            ],
            notes="T2 compute param — cascade deferred to apply_after_rerun",
        )
        # Also include provisional downstream dirty set for visibility
        for art in produced:
            if self._artifact_kind(art) == ArtifactKind.DATA.value:
                sub = self.plan_from_artifact_change(art, ImpactTier.T2)
                dirty.update(sub.dirty_nodes)
                # Don't duplicate the seed step in rerun; keep downstream only
                for r in sub.rerun_plan:
                    if r.step_node_id != step_node_id:
                        plan.rerun_plan.append(r)
        plan.dirty_nodes = sorted(dirty)
        return plan

    def apply_marks(self, plan: ImpactPlan) -> None:
        for nid in plan.dirty_nodes:
            try:
                self.store.set_node_status(nid, NodeStatus.DIRTY)
            except Exception:
                self.store.upsert_node(
                    nid,
                    "step" if nid.startswith("step:") else "artifact",
                    nid.split(":")[-1],
                    status=NodeStatus.DIRTY,
                )
        self.store.append_event(
            "invalidated",
            {
                "tier": plan.tier,
                "dirty_nodes": plan.dirty_nodes,
                "rerun_plan": [asdict(r) for r in plan.rerun_plan],
                "notes": plan.notes,
            },
        )

    def apply_after_rerun(
        self,
        step_node_id: str,
        *,
        previous_data_hashes: Dict[str, str],
        new_data_hashes: Dict[str, str],
    ) -> ImpactPlan:
        """After a step reruns: if data hashes unchanged, truncate cascade."""
        changed_arts = [
            lid
            for lid, h in new_data_hashes.items()
            if previous_data_hashes.get(lid) != h
        ]
        if not changed_arts:
            # short-circuit
            self.store.set_node_status(step_node_id, NodeStatus.CLEAN)
            for lid in new_data_hashes:
                self.store.set_node_status(lid, NodeStatus.CLEAN)
            return ImpactPlan(
                tier=ImpactTier.T2.value,
                dirty_nodes=[],
                rerun_plan=[],
                notes="Hash short-circuit: no downstream rerun needed",
            )
        dirty: Set[str] = set()
        rerun: List[RerunStep] = []
        for lid in changed_arts:
            sub = self.plan_from_artifact_change(lid, ImpactTier.T3)
            dirty.update(sub.dirty_nodes)
            rerun.extend(sub.rerun_plan)
        plan = ImpactPlan(
            tier=ImpactTier.T3.value,
            dirty_nodes=sorted(dirty),
            rerun_plan=rerun,
            notes=f"Cascade from changed artifacts: {changed_arts}",
        )
        self.apply_marks(plan)
        return plan

    # ---------------------------------------------------------------- helpers
    def _skill_of(self, step_node_id: str) -> str:
        g = self.store.current_graph()
        for n in g.nodes:
            if n["id"] == step_node_id:
                return n.get("skill") or step_node_id.split(":", 1)[-1]
        return step_node_id.split(":", 1)[-1]

    def _artifact_kind(self, logical_id: str) -> str:
        ver = self.store.get_latest_artifact_version(logical_id)
        if ver:
            return ver.get("kind") or ArtifactKind.OTHER.value
        g = self.store.current_graph()
        for n in g.nodes:
            if n["id"] == logical_id:
                meta = n.get("meta") or {}
                if isinstance(meta, str):
                    import json

                    meta = json.loads(meta)
                return meta.get("kind") or ArtifactKind.OTHER.value
        return ArtifactKind.OTHER.value

    def _producer_steps(self, logical_id: str) -> List[str]:
        steps = []
        for e in self.store.neighbors(logical_id, "in"):
            if e["kind"] == "produces" and e["src_id"].startswith("step:"):
                steps.append(e["src_id"])
        return steps

    def _latest_input_versions(self, step_node_id: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for art in self.store.consumed_artifacts(step_node_id):
            ver = self.store.get_latest_artifact_version(art)
            if ver:
                out[art] = ver["id"]
        return out
