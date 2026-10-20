# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Public composition root shared by native Operator and persistent Sessions."""

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


def ensure_transcript_ledger(ctx, state=None, *, config=None):
    if config is None:
        try:
            from artemis.config import load_agent_config

            config = load_agent_config().memory.transcript
        except Exception:
            from artemis.config.agent import MemoryTranscriptConfig

            config = MemoryTranscriptConfig()
    cfg = config
    from artemis.memory import TranscriptLedger, ensure_step_memory

    ledger = getattr(ctx, "transcript_ledger", None)
    if isinstance(ledger, TranscriptLedger):
        return ledger
    # Anchor the ``T+mm:ss`` clock to the DataEngine session start so the
    # tail offsets agree with the chunk ledger lines, the restored-history
    # block and the video analyzer's action timeline.
    session_start = getattr(ctx.data_engine, "session_start_time", None)
    ledger = TranscriptLedger(
        step_memory=ensure_step_memory(ctx),
        image_scrub_depth=getattr(cfg, "image_scrub_depth", 3),
        pending_grace_steps=getattr(cfg, "pending_grace_steps", 3),
        xml_scrub_depth=getattr(cfg, "xml_scrub_depth", 1),
        image_scrub_depth_relaxed=getattr(cfg, "image_scrub_depth_relaxed", None),
        context_budget_tokens=getattr(cfg, "context_budget_tokens", None),
        start_ratio=getattr(cfg, "start_ratio", None),
        session_start=session_start if isinstance(session_start, (int, float)) else None,
    )
    # L2/L3 chunk compression (M3) rides the same flag; without a
    # DataEngine there are no step records to chunk, so the ledger simply
    # runs scrub-edge-only.
    if ctx.data_engine is not None:
        try:
            from artemis.memory import HistoryChunkManager

            chunking_cfg = None
            try:
                from artemis.config import load_agent_config

                chunking_cfg = load_agent_config().memory.chunking
            except Exception as exc:
                logger.debug(
                    "Chunking config unavailable; using chunker defaults: %s",
                    exc,
                    exc_info=True,
                )
            ledger.attach_chunker(
                HistoryChunkManager(
                    engine=ctx.data_engine,
                    ctx=ctx,
                    chunking_config=chunking_cfg,
                    transcript_config=cfg,
                    goal=getattr(state, "initial_goal", None) if state else None,
                )
            )
        except Exception as e:
            logger.error(f"History chunk manager unavailable: {e}")
    try:
        ctx.transcript_ledger = ledger
    except (AttributeError, TypeError, ValueError):
        pass
    return ledger
