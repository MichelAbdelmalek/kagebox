"""The ``chapter-understand`` Prefect flow: panels of a ready chapter -> one SceneJSON per panel.

The local vision model describes what is DRAWN in each panel. Every panel crop is sent with its
text regions flat-filled (``pipeline/understand/request.py``, fail closed: a mask error fails that
panel and the model is never called with an unmasked image) and with NO OCR text. Model output is
untrusted: it is validated, cited to the panel by code (``pipeline/understand/contract.py``) and
never logged. Errors and review reasons carry class names and field names only.

State lives in the ``stage_runs`` row ``(chapter, 'understand')``: ``pending`` -> ``running`` ->
``done`` | ``failed``, or back to ``pending`` after a trial. The owner token is the Prefect
flow-run id (as in ``ingest_flow``); a run writes (heartbeat, scenes, review items, outcome) only
while it still owns the running row, so a run that was taken over writes nothing. A background
thread beats every ``HEARTBEAT_INTERVAL_S`` while a panel is being generated, because one panel
can take longer than the stale window (``STALE_INGEST_S``).

Gates, in order, before anything is claimed: a valid trial size, the chapter exists and is
``ready`` (never while an ingest runs: ingest refuses while this stage is alive too), and the
rights record allows rendering (local processing: no cloud gate). Rights are re-checked before
each model call and again inside every writing transaction. Each panel is one transaction: lock
the stage row, re-check ownership, rights and that the chapter is still ``ready`` with this
panel, upsert the scene, replace the panel's understand review items, beat.

Cache: a panel is skipped when its scene has the same ``params_hash`` and input hashes
(``pipeline/understand/request.py``). The model and the bubble detector load once per run, the
model only at the first uncached panel.

Trial: ``limit_panels`` (1..``MAX_TRIAL_PANELS``) processes only the first panels in order and ends
the stage ``pending`` (never ``done``) with the measured seconds per panel and a projection for the
rest; its scenes are normal cached scenes. A full run stops cleanly at the chapter time budget
(``max_minutes_per_chapter``); stored scenes remain and a re-run resumes from the cache.
"""

# No `from __future__ import annotations`: Prefect builds a Pydantic model from the flow
# signature at run time and needs real (not string) annotations.

import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Self, cast

from prefect import Flow, flow
from prefect._internal.concurrency.cancellation import CancelledError as ScopeCancelled
from prefect.client.schemas.objects import FlowRun, State
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import CursorResult, Engine, delete, select, update
from sqlalchemy.orm import Session

from orchestration import ingest_flow
from orchestration.flows import StageResult, db_rights_loader
from pipeline.common.dbmodels import (
    BubbleRow,
    ChapterRow,
    JobRow,
    PageRow,
    PanelRow,
    ReviewItemRow,
    RightsRow,
    SceneRow,
    StageRunRow,
    claim_stage,
)
from pipeline.common.ids import new_id
from pipeline.common.schemas import (
    BBox,
    Bubble,
    Panel,
    Provenance,
    RightsDenied,
    RightsRecord,
    SceneJSON,
    require_render_allowed,
)
from pipeline.common.storage import ObjectStore
from pipeline.ingest.process import safe_errors
from pipeline.providers.base import (
    ModelLoadFailed,
    ModelProvider,
    ModelSpec,
    StructuredOutputFailed,
)
from pipeline.providers.registry import model_for_stage
from pipeline.understand.contract import REVIEW_KINDS, PanelReading, to_scene
from pipeline.understand.mask import Detector
from pipeline.understand.request import (
    MaskFailed,
    PreparedPanel,
    params_hash,
    prepare_panel,
    scene_params,
)
from pipeline.understand.settings import UnderstandSettings, load_settings, prompt_text

log = logging.getLogger(__name__)

FLOW_NAME = "chapter-understand"
STAGE = "understand"
MAX_TRIAL_PANELS = 50
# A slow panel (several attempts of up to max_time_s) must never look dead: the heartbeat beats.
HEARTBEAT_INTERVAL_S = 60.0
# Prefect ends a run that exceeds this. It must stay above the chapter budget
# (max_minutes_per_chapter: 720 = 12 h), which ends a healthy run cleanly and resumably first;
# this only bounds a hung one.
FLOW_TIMEOUT_S = 13 * 3600

GENERIC_ERROR = "understand failed"
INTERRUPTED = "understand was interrupted"
TIMED_OUT = "understand timed out"
NOT_STARTED = "understand could not start"
ALREADY_RUNNING = "understand already running"
MODEL_NOT_LOADED = "the vision model could not be loaded"
NOT_READY = "chapter is not ready"
CHAPTER_CHANGED = "the chapter changed during understanding"
BUDGET = "understand stopped at its time budget; run it again to continue"
BAD_TRIAL = f"the trial size must be between 1 and {MAX_TRIAL_PANELS} panels"


class UnderstandAlreadyRunning(RuntimeError):
    """Another run holds the stage. The message is safe to show to the user."""


class UnderstandRefused(RuntimeError):
    """The run cannot start or must stop. The message is fixed and safe to show to the user."""


class UnderstandSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    panels: int  # panels considered (a trial: only the first limit_panels)
    scenes: int  # panels that have a scene after this run (new + cached)
    cached: int  # of those, reused from an earlier run
    failed: int  # panels recorded as scene_failed
    flagged: int  # review items created by this run
    trial: bool
    seconds_per_panel: float | None  # measured on model-called panels only
    projected_total_s: float | None  # trial: load + seconds_per_panel * uncached remaining
    dropped_figures: int = 0  # figures the model listed without a description


@dataclass(frozen=True)
class UnderstandDeps:
    engine: Engine
    store: ObjectStore
    settings: UnderstandSettings
    provider_factory: Callable[[ModelSpec], ModelProvider]
    detector_factory: Callable[[], Detector]


def _local_provider(settings: UnderstandSettings) -> Callable[[ModelSpec], ModelProvider]:
    def make(spec: ModelSpec) -> ModelProvider:
        from pipeline.providers.local_vlm import LocalVlmProvider  # deferred: imports torch

        if spec.provider != "local":
            raise ValueError("understand runs on the local provider only")
        return LocalVlmProvider(settings)

    return make


def build_deps() -> UnderstandDeps:
    """Production wiring (tests monkeypatch this)."""
    from pipeline.common.database import make_engine  # deferred: needs settings at run time
    from pipeline.common.settings import get_settings
    from pipeline.common.storage import make_store
    from pipeline.ingest.bubbles import BubbleDetector

    settings = load_settings()
    app = get_settings()
    return UnderstandDeps(
        engine=make_engine(app),
        store=make_store(app),
        settings=settings,
        provider_factory=_local_provider(settings),
        detector_factory=BubbleDetector,
    )


def build_engine() -> Engine:
    """Database only, for the state hooks (tests monkeypatch this)."""
    return ingest_flow.build_engine()


def project_total_s(load_s: float, seconds_per_panel: float, uncached_panels: int) -> float:
    """Projected seconds for the panels still to describe: one model load plus the rest."""
    return load_s + seconds_per_panel * uncached_panels


def _monotonic() -> float:
    return time.monotonic()  # a function so tests can move the clock


# ---------------------------------------------------------------------------------------------
# stage row: claim, heartbeat, owner-only writes


@dataclass(frozen=True)
class _Run:
    chapter_id: str
    project_id: str
    run_id: str  # this run's owner token


def _owns(row: StageRunRow | None, run_id: str) -> bool:
    return row is not None and row.status == "running" and row.run_id == run_id


def _rights_in(s: Session, project_id: str) -> None:
    raw = s.scalar(select(RightsRow.record).where(RightsRow.project_id == project_id))
    rec = None if raw is None else RightsRecord.model_validate_json(json.dumps(raw))
    require_render_allowed(rec, datetime.now(UTC))


def _start(engine: Engine, chapter_id: str, run_id: str) -> _Run:
    """Gates, then take the stage with one guarded UPDATE. Refused: nothing is touched."""
    now = datetime.now(UTC)
    with Session(engine) as s, s.begin():
        row = s.get(ChapterRow, chapter_id)
        if row is None:
            raise LookupError("chapter not found")
        if row.status != "ready":  # also: an ingest is running on it
            raise UnderstandRefused(NOT_READY)
        _rights_in(s, row.project_id)
        if not claim_stage(s, chapter_id, STAGE, run_id, now):
            raise UnderstandAlreadyRunning(ALREADY_RUNNING)
        return _Run(chapter_id, row.project_id, run_id)


def _beat(engine: Engine, run: _Run) -> None:
    """Heartbeat while this run owns the stage; a run that lost it stops here."""
    with Session(engine) as s, s.begin():
        res = cast(
            "CursorResult[Any]",
            s.execute(
                update(StageRunRow)
                .where(
                    StageRunRow.chapter_id == run.chapter_id,
                    StageRunRow.stage == STAGE,
                    StageRunRow.status == "running",
                    StageRunRow.run_id == run.run_id,
                )
                .values(heartbeat_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            ),
        )
        if res.rowcount != 1:
            raise UnderstandAlreadyRunning(ALREADY_RUNNING)


class _Heartbeat:
    """Beats from a background thread every ``interval`` s until stopped (owner-only update).

    A beat that finds the stage lost, or fails, ends the thread and sets ``lost``; the run checks
    it when the slow call returns and stops there. Nothing but a class name is logged.
    """

    def __init__(self, engine: Engine, run: _Run, interval: float) -> None:
        self._engine, self._run, self._interval = engine, run, interval
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="understand-heartbeat", daemon=True)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                _beat(self._engine, self._run)
            except UnderstandAlreadyRunning:
                self.lost.set()
                return
            except Exception as exc:  # a failing beat must not kill the generation
                log.warning("heartbeat failed: %s", type(exc).__name__)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def check(self) -> None:
        if self.lost.is_set():
            raise UnderstandAlreadyRunning(ALREADY_RUNNING)


@contextlib.contextmanager
def _beating(engine: Engine, run: _Run) -> Iterator[_Heartbeat]:
    with _Heartbeat(engine, run, HEARTBEAT_INTERVAL_S) as hb:
        yield hb
    hb.check()


# Stored in ``counts`` when the chapter budget stopped a full run: its scenes are cached, so the
# API lets the user confirm a full run again without a new trial (claim_stage resets counts, so
# any later run replaces the marker).
RESUMABLE_COUNTS: dict[str, Any] = {"resumable": True}


def _fail(
    engine: Engine,
    chapter_id: str,
    message: str,
    run_id: str,
    counts: dict[str, Any] | None = None,
) -> None:
    """Mark the stage failed, only while ``run_id`` still owns it."""
    with Session(engine) as s, s.begin():
        row = s.get(StageRunRow, (chapter_id, STAGE), with_for_update=True)
        if row is not None and _owns(row, run_id):
            row.status, row.error, row.finished_at = "failed", message, datetime.now(UTC)
            if counts is not None:
                row.counts = counts


def _safe_fail(
    engine: Engine,
    chapter_id: str,
    message: str,
    run_id: str,
    counts: dict[str, Any] | None = None,
) -> None:
    """``_fail`` that never raises, so the caller re-raises the ORIGINAL error."""
    try:
        _fail(engine, chapter_id, message, run_id, counts)
    except Exception:
        log.exception("could not mark understand of chapter %s failed", chapter_id)


def _finish(engine: Engine, run: _Run, status: str, counts: dict[str, Any]) -> None:
    with Session(engine) as s, s.begin():
        row = s.get(StageRunRow, (run.chapter_id, STAGE), with_for_update=True)
        if row is None or not _owns(row, run.run_id):
            raise UnderstandAlreadyRunning(ALREADY_RUNNING)
        row.status, row.error, row.counts = status, None, counts
        row.finished_at = datetime.now(UTC)


# ---------------------------------------------------------------------------------------------
# panels, scenes, review items


@dataclass(frozen=True)
class _PanelWork:
    id: str
    panel: Panel
    bubbles: list[Bubble]
    page_boxes: list[BBox]  # every stored region box of the panel's page (page coordinates)


def _load_panels(engine: Engine, chapter_id: str) -> list[_PanelWork]:
    """Panels in page then reading order, with their bubbles and their page's stored boxes."""
    with Session(engine) as s:
        rows = list(
            s.execute(
                select(PanelRow, PageRow.id)
                .join(PageRow, PanelRow.page_id == PageRow.id)
                .where(PanelRow.chapter_id == chapter_id)
                .order_by(PageRow.index, PanelRow.reading_order)
            )
        )
        by_panel: dict[str, list[Bubble]] = {}
        for brow in s.scalars(select(BubbleRow).where(BubbleRow.chapter_id == chapter_id)):
            bubble = Bubble.model_validate_json(json.dumps({**brow.artifact, "id": brow.id}))
            by_panel.setdefault(brow.panel_id, []).append(bubble)
        panels = [
            (Panel.model_validate_json(json.dumps(p.artifact)), page_id) for p, page_id in rows
        ]
    boxes_by_page: dict[str, list[BBox]] = {}
    for panel, page_id in panels:
        boxes_by_page.setdefault(page_id, []).extend(b.bbox for b in by_panel.get(panel.id, []))
    return [
        _PanelWork(p.id, p, by_panel.get(p.id, []), boxes_by_page[page_id]) for p, page_id in panels
    ]


def _cached(engine: Engine, panel_id: str, ph: str, ih: list[str]) -> bool:
    """True when the panel's stored scene was made with these params from these inputs."""
    with Session(engine) as s:
        row = s.scalar(select(SceneRow).where(SceneRow.panel_id == panel_id))
        if row is None or row.params_hash != ph:
            return False
        try:
            scene = SceneJSON.model_validate_json(json.dumps(row.artifact))
        except ValidationError:
            return False  # unreadable: describe it again
        return scene.provenance.input_hashes == ih


def _uncached_by_params(engine: Engine, panel_ids: list[str], ph: str) -> int:
    """Estimate for a trial: panels with no scene made with these params (inputs not rechecked)."""
    if not panel_ids:
        return 0
    with Session(engine) as s:
        have = set(
            s.scalars(
                select(SceneRow.panel_id).where(
                    SceneRow.panel_id.in_(panel_ids), SceneRow.params_hash == ph
                )
            )
        )
    return len(panel_ids) - len(have)


def _provenance(spec: ModelSpec, ph: str, ih: list[str]) -> Provenance:
    return Provenance(
        stage="understand",
        tool=spec.model,
        tool_version=spec.revision,
        model_revision=spec.revision,
        license=spec.license,
        source_url=spec.source_url,
        provider="local",
        params_hash=ph,
        input_hashes=ih,
        created_at=datetime.now(UTC),
    )


def _owned_stage(s: Session, run: _Run) -> StageRunRow:
    """The locked stage row, if this run still owns it (else the new owner writes)."""
    row = s.get(StageRunRow, (run.chapter_id, STAGE), with_for_update=True)
    if row is None or not _owns(row, run.run_id):
        raise UnderstandAlreadyRunning(ALREADY_RUNNING)
    return row


def _replace_reviews(
    s: Session, run: _Run, panel_id: str, reasons: Sequence[tuple[str, str]]
) -> None:
    s.execute(
        delete(ReviewItemRow).where(
            ReviewItemRow.chapter_id == run.chapter_id,
            ReviewItemRow.artifact_id == panel_id,
            ReviewItemRow.kind.in_(REVIEW_KINDS),
        )
    )
    for kind, reason in reasons:
        s.add(
            ReviewItemRow(
                id=new_id("rev"),
                project_id=run.project_id,
                chapter_id=run.chapter_id,
                artifact_id=panel_id,
                kind=kind,
                reason=reason,
                status="open",
            )
        )


def _store_scene(
    engine: Engine,
    run: _Run,
    panel_id: str,
    scene: SceneJSON,
    ph: str,
    reasons: Sequence[tuple[str, str]],
) -> None:
    """One transaction: owner, rights, chapter and panel checks; scene and review items; beat."""
    artifact = scene.model_dump(mode="json", exclude={"unknown_fields"})
    SceneJSON.model_validate_json(json.dumps(artifact))  # re-validated at write
    with Session(engine) as s, s.begin():
        stage = _owned_stage(s, run)
        chapter = s.get(ChapterRow, run.chapter_id, with_for_update=True)
        if chapter is None or chapter.status != "ready":
            raise UnderstandRefused(CHAPTER_CHANGED)
        _rights_in(s, run.project_id)
        panel = s.get(PanelRow, panel_id)
        if panel is None or panel.chapter_id != run.chapter_id:  # the chapter id is never input
            raise UnderstandRefused(CHAPTER_CHANGED)
        existing = s.scalar(select(SceneRow).where(SceneRow.panel_id == panel_id))
        if existing is None:
            s.add(
                SceneRow(
                    id=scene.id,
                    panel_id=panel_id,
                    chapter_id=panel.chapter_id,
                    params_hash=ph,
                    artifact=artifact,
                )
            )
        else:  # the scene id was taken from this row when the scene was built
            existing.params_hash, existing.artifact = ph, artifact
            existing.created_at = datetime.now(UTC)
        _replace_reviews(s, run, panel_id, reasons)
        stage.heartbeat_at = datetime.now(UTC)


def _scene_id(engine: Engine, panel_id: str) -> str:
    """The panel's existing scene id (a re-run replaces the scene in place), else a new one."""
    with Session(engine) as s:
        existing = s.scalar(select(SceneRow.id).where(SceneRow.panel_id == panel_id))
    return existing or new_id("scene")


def _store_failure(engine: Engine, run: _Run, panel_id: str, reason: str) -> None:
    """A panel without a scene: its review item, in its own transaction, owner only."""
    with Session(engine) as s, s.begin():
        stage = _owned_stage(s, run)
        _replace_reviews(s, run, panel_id, [("scene_failed", reason)])
        stage.heartbeat_at = datetime.now(UTC)


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, MaskFailed):
        return f"the text mask could not be built ({exc.cause}); the panel was not described"
    if isinstance(exc, StructuredOutputFailed):
        return (
            f"the vision model gave no valid answer after {exc.attempts} attempts "
            f"(last error: {exc.last_error})"
        )
    return f"the scene could not be built ({type(exc).__name__})"


# ---------------------------------------------------------------------------------------------
# the run


@dataclass
class _Tally:
    cached: int = 0
    new: int = 0
    failed: int = 0
    flagged: int = 0
    dropped_figures: int = 0
    model_panels: int = 0
    model_seconds: float = 0.0
    load_s: float = 0.0


def _check_rights(engine: Engine, project_id: str) -> None:
    require_render_allowed(db_rights_loader(engine)(project_id), datetime.now(UTC))


def _understand(
    run: _Run, deps: UnderstandDeps, limit_panels: int | None
) -> tuple[UnderstandSummary, dict[str, Any], str]:
    s = deps.settings
    spec = model_for_stage(STAGE)
    instructions, prompt_sha = prompt_text(s)
    ph = params_hash(scene_params(s, spec, prompt_sha))
    everything = _load_panels(deps.engine, run.chapter_id)
    selected = everything if limit_panels is None else everything[:limit_panels]
    started = _monotonic()
    tally = _Tally()
    detector: Detector | None = None
    provider: ModelProvider | None = None
    _beat(deps.engine, run)
    last_beat = started
    for work in selected:
        now = _monotonic()
        if now - started > s.max_minutes_per_chapter * 60:
            raise UnderstandRefused(BUDGET)
        if now - last_beat >= HEARTBEAT_INTERVAL_S:  # cached panels never reach a model call
            _beat(deps.engine, run)
            last_beat = now
        panel_started = _monotonic()
        if detector is None:
            detector = deps.detector_factory()  # once per run; a failure here fails the run
        # Fail closed: any error up to the masked image fails this panel and calls no model.
        try:
            crop = deps.store.get(work.panel.crop_ref)
            prepared = prepare_panel(
                crop, work.panel.bbox, work.bubbles, work.page_boxes, detector, s
            )
        except Exception as exc:
            _record_failed(deps, run, tally, work.id, exc)
            continue
        if _cached(deps.engine, work.id, ph, prepared.input_hashes):
            tally.cached += 1
            continue
        _check_rights(deps.engine, run.project_id)
        load_s = 0.0
        if provider is None:
            provider, load_s = _load(deps, run, spec)
            tally.load_s += load_s
        try:
            with _beating(deps.engine, run):
                reading = provider.vlm_structured(
                    spec, instructions, "", [prepared.png], PanelReading
                )
        except (UnderstandAlreadyRunning, UnderstandRefused, ModelLoadFailed):
            raise  # run-level conditions (all RuntimeErrors): they stop the run, not this panel
        except (StructuredOutputFailed, ValueError, RuntimeError) as exc:
            # No valid answer, an image the model refused (ValueError) or an out-of-memory error
            # (RuntimeError): this panel only; the review item carries the class name, not the text.
            _record_failed(deps, run, tally, work.id, exc)
            continue
        _record_scene(deps, run, tally, work, reading, spec, ph, prepared)
        tally.model_panels += 1
        tally.model_seconds += max(0.0, _monotonic() - panel_started - load_s)
    return _outcome(deps, run, tally, selected, everything, limit_panels, ph)


def _load(deps: UnderstandDeps, run: _Run, spec: ModelSpec) -> tuple[ModelProvider, float]:
    """Build the provider and load the model, timed; heartbeat before and after."""
    _beat(deps.engine, run)
    started = _monotonic()
    with _beating(deps.engine, run):
        provider = deps.provider_factory(spec)
        loader = getattr(provider, "load", None)
        if callable(loader):
            loader(spec)
    elapsed = _monotonic() - started
    _beat(deps.engine, run)
    return provider, elapsed


def _record_failed(
    deps: UnderstandDeps, run: _Run, tally: _Tally, panel_id: str, exc: Exception
) -> None:
    log.warning("panel %s not described: %s", panel_id, type(exc).__name__)
    _store_failure(deps.engine, run, panel_id, _failure_reason(exc))
    tally.failed += 1
    tally.flagged += 1


def _record_scene(
    deps: UnderstandDeps,
    run: _Run,
    tally: _Tally,
    work: _PanelWork,
    reading: PanelReading,
    spec: ModelSpec,
    ph: str,
    prepared: PreparedPanel,
) -> None:
    prov = _provenance(spec, ph, prepared.input_hashes)
    try:
        scene, issues = to_scene(
            reading, scene_id=_scene_id(deps.engine, work.id), panel_id=work.id, provenance=prov
        )
    except ValidationError as exc:  # a model answer the schema refuses: this panel only
        _record_failed(deps, run, tally, work.id, exc)
        return
    reasons = [
        (i.kind, f"{i.field}: the claim cites the panel's text as evidence and was dropped")
        for i in issues
    ]
    _store_scene(deps.engine, run, work.id, scene, ph, reasons)
    tally.new += 1
    tally.flagged += len(reasons)
    tally.dropped_figures += len(reading.figures) - len(scene.figures)


def _outcome(
    deps: UnderstandDeps,
    run: _Run,
    t: _Tally,
    selected: list[_PanelWork],
    everything: list[_PanelWork],
    limit_panels: int | None,
    ph: str,
) -> tuple[UnderstandSummary, dict[str, Any], str]:
    trial = limit_panels is not None
    spp = t.model_seconds / t.model_panels if t.model_panels else None
    projected: float | None = None
    counts: dict[str, Any] = {
        "trial": trial,
        "panels": len(selected),
        "scenes": t.cached + t.new,
        "cached": t.cached,
        "failed": t.failed,
        "flagged": t.flagged,
        "dropped_figures": t.dropped_figures,
    }
    if trial:
        remaining = _uncached_by_params(
            deps.engine, [w.id for w in everything[len(selected) :]], ph
        )
        if spp is not None:
            projected = project_total_s(t.load_s, spp, remaining)
        counts |= {
            "limit_panels": limit_panels,
            "measured_panels": t.model_panels,
            "load_s": round(t.load_s, 3),
            "seconds_per_panel": None if spp is None else round(spp, 3),
            "uncached_remaining": remaining,
            "projected_total_s": None if projected is None else round(projected, 1),
            "over_budget": projected is not None
            and projected > deps.settings.max_minutes_per_chapter * 60,
        }
    summary = UnderstandSummary(
        panels=len(selected),
        scenes=t.cached + t.new,
        cached=t.cached,
        failed=t.failed,
        flagged=t.flagged,
        trial=trial,
        seconds_per_panel=spp,
        projected_total_s=projected,
        dropped_figures=t.dropped_figures,
    )
    return summary, counts, "pending" if trial else "done"


def run_understand(
    chapter_id: str,
    deps: UnderstandDeps,
    run_id: str | None = None,
    limit_panels: int | None = None,
) -> UnderstandSummary:
    if limit_panels is not None and not 1 <= limit_panels <= MAX_TRIAL_PANELS:
        raise UnderstandRefused(BAD_TRIAL)  # before claiming anything
    run = _start(deps.engine, chapter_id, run_id or ingest_flow.current_run_id())
    set_job = ingest_flow._safe_set_job
    set_job(deps.engine, run.run_id, "running")
    try:
        summary, counts, status = _understand(run, deps, limit_panels)
        _finish(deps.engine, run, status, counts)
    except UnderstandAlreadyRunning:
        log.warning("chapter %s understand was taken over; nothing written", chapter_id)
        set_job(deps.engine, run.run_id, "failed")
        raise
    except (RightsDenied, UnderstandRefused, ModelLoadFailed) as exc:  # fixed, user-safe messages
        log.warning("chapter %s understand stopped: %s", chapter_id, exc)
        budget = isinstance(exc, UnderstandRefused) and str(exc) == BUDGET
        _safe_fail(
            deps.engine, chapter_id, str(exc), run.run_id, RESUMABLE_COUNTS if budget else None
        )
        set_job(deps.engine, run.run_id, "failed")
        raise
    except ValidationError as exc:  # never log or re-raise it with its (untrusted) input
        errors = safe_errors(exc)
        log.error("chapter %s understand failed: invalid artifact: %s", chapter_id, errors)
        _safe_fail(deps.engine, chapter_id, GENERIC_ERROR, run.run_id)
        set_job(deps.engine, run.run_id, "failed")
        raise RuntimeError(f"invalid artifact: {errors}") from None
    except Exception as exc:
        log.error("chapter %s understand failed: %s", chapter_id, type(exc).__name__)
        _safe_fail(deps.engine, chapter_id, GENERIC_ERROR, run.run_id)
        set_job(deps.engine, run.run_id, "failed")
        raise
    except ScopeCancelled:  # the flow's timeout_seconds expired (Prefect's cancel scope)
        log.error("chapter %s understand timed out after %ss", chapter_id, FLOW_TIMEOUT_S)
        _safe_fail(deps.engine, chapter_id, TIMED_OUT, run.run_id)
        set_job(deps.engine, run.run_id, "failed")
        raise
    except BaseException:  # cancellation, termination signal, KeyboardInterrupt
        log.warning("chapter %s understand interrupted", chapter_id)
        _safe_fail(deps.engine, chapter_id, INTERRUPTED, run.run_id)
        raise
    set_job(deps.engine, run.run_id, "completed")
    log.info("chapter %s understand %s: %s", chapter_id, status, summary)
    return summary


# ---------------------------------------------------------------------------------------------
# Prefect state hooks


def _fail_unstarted(engine: Engine, chapter_id: str, run_id: str) -> None:
    """A run that ended before claiming the stage fails it, if the stage still waits for exactly
    this run ('pending', and the chapter's latest understand job is this run)."""
    with Session(engine) as s, s.begin():
        row = s.get(StageRunRow, (chapter_id, STAGE), with_for_update=True)
        if row is None or row.status != "pending":
            return
        latest = s.scalar(
            select(JobRow.flow_run_id)
            .where(JobRow.chapter_id == chapter_id, JobRow.stage == STAGE)
            .order_by(JobRow.created_at.desc(), JobRow.id.desc())
            .limit(1)
        )
        if latest == run_id:
            row.status, row.error, row.finished_at = "failed", NOT_STARTED, datetime.now(UTC)


def _after_run(flow_run: FlowRun, job_status: str, message: str) -> None:
    """Record how a run ended: its job, and its stage if the run left it unfinished."""
    chapter_id = flow_run.parameters.get("chapter_id")
    run_id = str(flow_run.id)
    try:
        engine = build_engine()
    except Exception:
        log.exception("could not record the end of run %s", run_id)
        return
    steps: list[tuple[str, Callable[[], None]]] = [
        ("job", lambda: ingest_flow._set_job(engine, run_id, job_status)),
    ]
    if isinstance(chapter_id, str):
        steps += [
            ("owned stage", lambda: _fail(engine, chapter_id, message, run_id)),
            ("pending stage", lambda: _fail_unstarted(engine, chapter_id, run_id)),
        ]
    try:
        for what, step in steps:
            try:
                step()
            except Exception:
                log.exception("could not update the %s after run %s ended", what, run_id)
    finally:
        engine.dispose()


def on_cancelled(flow: Flow[Any, Any], flow_run: FlowRun, state: State[Any] | None) -> None:
    _after_run(flow_run, "cancelled", INTERRUPTED)


def on_crashed(flow: Flow[Any, Any], flow_run: FlowRun, state: State[Any] | None) -> None:
    _after_run(flow_run, "failed", INTERRUPTED)


def on_failed(flow: Flow[Any, Any], flow_run: FlowRun, state: State[Any] | None) -> None:
    """Any failure, including one before the stage was claimed. ``run_understand`` has usually
    recorded the precise message already; this only fills gaps."""
    _after_run(flow_run, "failed", GENERIC_ERROR)


@flow(
    name=FLOW_NAME,
    log_prints=False,
    validate_parameters=True,
    on_cancellation=[on_cancelled],
    on_crashed=[on_crashed],
    on_failure=[on_failed],
    timeout_seconds=FLOW_TIMEOUT_S,
)
def understand_flow(chapter_id: str, limit_panels: int | None = None) -> UnderstandSummary:
    """Deployment entry point. Parameters must stay JSON-serializable."""
    return run_understand(chapter_id, build_deps(), limit_panels=limit_panels)


def understand_stage(chapter_id: str) -> StageResult:
    """The chapter pipeline's understand stage: a full run."""
    summary = understand_flow(chapter_id)
    counts = {k: str(v) for k, v in summary.model_dump().items()}
    return {"stage": STAGE, "chapter_id": chapter_id, "status": "done", **counts}
