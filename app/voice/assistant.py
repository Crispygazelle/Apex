"""Voice session: wake word, then one command, then back to silence.

Crash state preempts the session. A rider who is lying in the road cannot be
asked to say "hey apex" first, so cancel works without a wake word while the
SOS is counting down. Every other command still needs the wake word, which is
what keeps wind and radio chatter from logging phantom hazards.

Audio is transcribed and then dropped. The assistant never keeps a PCM buffer
longer than the current utterance, and `on_reading` zeros the payload after
the transcriber returns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app import clock
from app.config import AppConfig
from app.models import SensorReading, SensorSource
from app.pipeline.coordinator import Coordinator
from app.safety.monitor import SafetyMonitor
from app.storage.batch_writer import BatchWriter
from app.voice.commands import PREEMPT_STATES, CommandHandler
from app.voice.intent import IntentName, IntentParser
from app.voice.stt import Transcriber, build_transcriber, is_silence
from app.voice.tts import Speaker, build_speaker
from app.voice.wake_word import WakeWordDetector

logger = logging.getLogger(__name__)


class SessionPhase(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    PREEMPTED = "preempted"


@dataclass
class VoiceStats:
    wakeups: int = 0
    commands: int = 0
    unknown: int = 0
    preempted: int = 0
    cancelled: int = 0
    last_transcript: str = ""
    last_reply: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass
class _Utterance:
    started: float = 0.0
    last_voice: float = 0.0
    chunks: list[bytes] = field(default_factory=list)

    def append(self, pcm: bytes, now: float) -> None:
        self.chunks.append(pcm)
        self.last_voice = now

    def take(self) -> bytes:
        blob = b"".join(self.chunks)
        self.chunks.clear()
        return blob


class VoiceAssistant:
    """Owns the listen → parse → speak cycle."""

    def __init__(
        self,
        config: AppConfig,
        coordinator: Coordinator,
        *,
        safety: SafetyMonitor | None = None,
        batch_writer: BatchWriter | None = None,
        transcriber: Transcriber | None = None,
        speaker: Speaker | None = None,
    ) -> None:
        self.config = config
        self.coordinator = coordinator
        self.safety = safety
        voice = config.voice

        self.wake = WakeWordDetector(voice.wake_words)
        self.parser = IntentParser()
        self.commands = CommandHandler(
            coordinator, safety=safety, batch_writer=batch_writer
        )
        self.transcriber = transcriber or build_transcriber(
            voice.vosk_model_path, sample_rate=config.sensors.mic.sample_rate
        )
        self.speaker = speaker or build_speaker(
            voice.piper_model_path,
            output_device=voice.tts_output_device,
            speak=voice.speak_responses,
        )

        self.stats = VoiceStats()
        self.phase = SessionPhase.IDLE
        self._utterance = _Utterance()
        self._announced_preempt = False
        self._sample_rate = config.sensors.mic.sample_rate

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if not self.config.voice.enabled:
            logger.info("Voice layer disabled by config")
            return
        self.coordinator.subscribe_reading(SensorSource.MIC, self.on_reading)
        self.coordinator.subscribe_sample(self.on_sample)
        logger.info(
            "Voice armed: wake %s, timeout %.1fs",
            ", ".join(self.wake.phrases) or "(none)",
            self.config.voice.command_timeout_s,
        )

    async def stop(self) -> None:
        self._utterance.chunks.clear()
        self.transcriber.reset()

    # --- live audio -------------------------------------------------------

    async def on_sample(self, sample: Any) -> None:
        """Watch system state so a crash can seize the session mid-utterance."""
        if sample.system_state in PREEMPT_STATES:
            await self._enter_preempt()
        elif self.phase is SessionPhase.PREEMPTED:
            self.phase = SessionPhase.IDLE
            self._announced_preempt = False

    async def on_reading(self, reading: SensorReading) -> None:
        pcm = reading.payload.get("pcm")
        if not isinstance(pcm, bytes):
            return
        rate = int(reading.payload.get("sample_rate", self._sample_rate))
        # Copy the bytes we need, then drop the payload so nothing downstream
        # can write the raw audio to disk.
        chunk = bytes(pcm)
        reading.payload["pcm"] = b""
        try:
            await self._ingest(chunk, rate)
        finally:
            chunk = b""

    async def _ingest(self, pcm: bytes, sample_rate: int) -> None:
        now = clock.monotonic()
        silent = is_silence(pcm)

        if self.phase is SessionPhase.IDLE:
            if silent:
                return
            # Short rolling window: enough for "hey apex" plus a command.
            self._utterance.append(pcm, now)
            if self._utterance.started == 0.0:
                self._utterance.started = now
            if now - self._utterance.started < 0.6:
                return
            await self._flush_idle(sample_rate)
            return

        if self.phase is SessionPhase.PREEMPTED:
            if silent:
                if (
                    self._utterance.chunks
                    and now - self._utterance.last_voice >= self.config.voice.endpoint_silence_s
                ):
                    await self._finish_utterance(sample_rate, require_wake=False)
                return
            if self._utterance.started == 0.0:
                self._utterance.started = now
            self._utterance.append(pcm, now)
            return

        # LISTENING: collect until silence or timeout.
        if not silent:
            if self._utterance.started == 0.0:
                self._utterance.started = now
            self._utterance.append(pcm, now)
            return

        if not self._utterance.chunks:
            return
        quiet_long_enough = (
            now - self._utterance.last_voice >= self.config.voice.endpoint_silence_s
        )
        timed_out = now - self._utterance.started >= self.config.voice.command_timeout_s
        if quiet_long_enough or timed_out:
            await self._finish_utterance(sample_rate, require_wake=False)

    async def _flush_idle(self, sample_rate: int) -> None:
        blob = self._utterance.take()
        self._utterance = _Utterance()
        text = self._transcribe(blob, sample_rate)
        if not text:
            return
        match = self.wake.match(text)
        if not match.heard:
            return
        self.stats.wakeups += 1
        if match.remainder:
            await self._dispatch(match.remainder)
            return
        self.phase = SessionPhase.LISTENING
        self._utterance = _Utterance()
        logger.info("Wake word %r; listening for a command", match.phrase)

    # --- text path (tests, and a finished utterance) ----------------------

    async def hear(self, text: str) -> str:
        """Treat `text` as a finished transcript. Used by tests and demos."""
        if self.phase is SessionPhase.PREEMPTED or self.commands.preempted:
            return await self._dispatch(text, require_wake=False)
        match = self.wake.match(text)
        if match.heard:
            self.stats.wakeups += 1
            if not match.remainder:
                self.phase = SessionPhase.LISTENING
                return ""
            # Remainder is already past the wake phrase; do not require it again.
            return await self._dispatch(match.remainder, require_wake=False)
        # Already listening from a previous wake with no remainder.
        if self.phase is SessionPhase.LISTENING:
            return await self._dispatch(text, require_wake=False)
        reply = "Say the wake word first."
        self.stats.last_transcript = text
        self.stats.last_reply = reply
        return reply

    async def _finish_utterance(self, sample_rate: int, *, require_wake: bool) -> None:
        blob = self._utterance.take()
        self._utterance = _Utterance()
        text = self._transcribe(blob, sample_rate)
        self.phase = (
            SessionPhase.PREEMPTED if self.commands.preempted else SessionPhase.IDLE
        )
        if not text:
            return
        await self._dispatch(text, require_wake=require_wake)

    def _transcribe(self, pcm: bytes, sample_rate: int) -> str:
        try:
            return self.transcriber.transcribe(pcm, sample_rate).strip()
        finally:
            self.transcriber.reset()

    async def _dispatch(self, text: str, *, require_wake: bool = True) -> str:
        if require_wake:
            match = self.wake.match(text)
            if match.heard:
                text = match.remainder
            elif self.phase is not SessionPhase.LISTENING:
                return ""

        intent = self.parser.parse(text)
        self.stats.last_transcript = text
        self.stats.commands += 1
        if intent.name is IntentName.UNKNOWN:
            self.stats.unknown += 1
        if intent.name is IntentName.CANCEL:
            self.stats.cancelled += 1
        if self.commands.preempted and intent.name not in {
            IntentName.CANCEL,
            IntentName.SOS,
        }:
            self.stats.preempted += 1

        reply = await self.commands.handle(intent)
        self.stats.last_reply = reply
        self.phase = (
            SessionPhase.PREEMPTED if self.commands.preempted else SessionPhase.IDLE
        )
        await self.speaker.speak(reply)
        return reply

    async def _enter_preempt(self) -> None:
        if self.phase is not SessionPhase.PREEMPTED:
            self._utterance = _Utterance()
            self.transcriber.reset()
            self.phase = SessionPhase.PREEMPTED
        if self._announced_preempt:
            return
        self._announced_preempt = True
        prompt = self.commands._preempt_prompt()
        self.stats.last_reply = prompt
        await self.speaker.speak(prompt)
