"""Voice assistant: commands, privacy, and crash preemption."""

from __future__ import annotations

from app.config import AppConfig
from app.models import (
    FusionMode,
    MotionState,
    RideMetrics,
    RideSample,
    SensorReading,
    SensorSource,
    SystemState,
)
from app.pipeline.coordinator import Coordinator
from app.safety.monitor import SafetyMonitor
from app.safety.sos import SosState
from app.safety.status_led import RecordingLedBackend
from app.voice.assistant import SessionPhase, VoiceAssistant
from app.voice.stt import FakeTranscriber, is_silence, pcm_rms
from app.voice.tts import RecordingSpeaker
from tests.test_sos import EVENT, StubNotifier


def seed(coordinator: Coordinator, *, speed_kmh: float = 48.0) -> RideSample:
    sample = RideSample(
        timestamp=1_800_000_000.0,
        ride_id=coordinator.ride_id,
        state=MotionState(
            timestamp=1_800_000_000.0,
            latitude=12.9716,
            longitude=77.5946,
            heading_deg=42.0,
            speed_mps=speed_kmh / 3.6,
            mode=FusionMode.FUSED,
        ),
        metrics=RideMetrics(speed_kmh=speed_kmh, g_force=1.1, distance_m=1850.0),
        system_state=SystemState.RIDING,
        gps_valid=True,
        satellites=10,
    )
    coordinator.buffer.append(sample)
    return sample


def make_assistant(
    config: AppConfig,
    *,
    safety: SafetyMonitor | None = None,
    speaker: RecordingSpeaker | None = None,
) -> tuple[VoiceAssistant, Coordinator, RecordingSpeaker]:
    config.voice.enabled = True
    coordinator = Coordinator(config, ride_id="ride-test", include_mic=False)
    seed(coordinator)
    spoken = speaker or RecordingSpeaker()
    assistant = VoiceAssistant(
        config,
        coordinator,
        safety=safety,
        transcriber=FakeTranscriber(),
        speaker=spoken,
    )
    return assistant, coordinator, spoken


async def test_speed_requires_the_wake_word(config: AppConfig) -> None:
    assistant, _, spoken = make_assistant(config)
    reply = await assistant.hear("what's my speed")
    assert reply == "Say the wake word first."
    assert spoken.spoken == []


async def test_speed_after_wake_word(config: AppConfig) -> None:
    assistant, _, spoken = make_assistant(config)
    reply = await assistant.hear("hey apex what's my speed")
    assert "48" in reply
    assert spoken.spoken[-1] == reply


async def test_wake_then_command_as_two_turns(config: AppConfig) -> None:
    assistant, _, _ = make_assistant(config)
    assert await assistant.hear("hey apex") == ""
    assert assistant.phase is SessionPhase.LISTENING
    reply = await assistant.hear("status report")
    assert "48" in reply
    assert "1.1" in reply
    assert assistant.phase is SessionPhase.IDLE


async def test_destination_remaining_is_spoken(config: AppConfig) -> None:
    assistant, coordinator, _ = make_assistant(config)
    latest = coordinator.latest
    coordinator.buffer.append(
        RideSample(
            timestamp=latest.timestamp + 1,
            ride_id=latest.ride_id,
            state=latest.state,
            metrics=RideMetrics(
                speed_kmh=48.0,
                g_force=1.1,
                distance_m=1850.0,
                remaining_m=2300.0,
                destination_latitude=21.0946,
                destination_longitude=79.0497,
            ),
            system_state=latest.system_state,
            gps_valid=True,
            satellites=10,
        )
    )
    reply = await assistant.hear("hey apex how far am I to the destination")
    assert "2.3" in reply
    assert "destination" in reply


async def test_log_a_pothole_here_marks_the_hazard(config: AppConfig) -> None:
    assistant, _, _ = make_assistant(config)
    reply = await assistant.hear("hey apex log a pothole here")
    assert "pothole" in reply.lower()
    assert assistant.commands.hazards[0].hazard_type == "pothole"


async def test_hazard_is_gps_tagged(config: AppConfig) -> None:
    assistant, coordinator, _ = make_assistant(config)
    reply = await assistant.hear("apex log hazard pothole")
    assert "pothole" in reply
    assert len(assistant.commands.hazards) == 1
    hazard = assistant.commands.hazards[0]
    assert hazard.hazard_type == "pothole"
    assert hazard.latitude == coordinator.latest.state.latitude
    assert hazard.ride_id == "ride-test"


async def test_hazard_without_a_fix_is_refused(config: AppConfig) -> None:
    assistant, coordinator, _ = make_assistant(config)
    latest = coordinator.latest
    coordinator.buffer.append(
        RideSample(
            timestamp=latest.timestamp + 1,
            ride_id=latest.ride_id,
            state=latest.state,
            metrics=latest.metrics,
            gps_valid=False,
        )
    )
    reply = await assistant.hear("apex log hazard gravel")
    assert "No GPS" in reply
    assert assistant.commands.hazards == []


async def test_unknown_phrase_is_honest(config: AppConfig) -> None:
    assistant, _, _ = make_assistant(config)
    reply = await assistant.hear("apex play music")
    assert "didn't catch" in reply
    assert assistant.stats.unknown == 1


async def test_voice_cannot_fire_an_sos_on_its_own(config: AppConfig) -> None:
    """A garbled 'emergency' from wind must not page anyone."""
    config.safety.sos_countdown_s = 30.0
    coordinator = Coordinator(config, ride_id="ride-test")
    safety = SafetyMonitor(
        config, coordinator, notifier=StubNotifier(), led_backend=RecordingLedBackend()
    )
    assistant, _, _ = make_assistant(config, safety=safety)
    reply = await assistant.hear("apex emergency")
    assert safety.sos.state is SosState.IDLE
    assert "will not send" in reply


async def test_crash_preempts_voice_and_cancel_needs_no_wake_word(
    config: AppConfig,
) -> None:
    config.safety.sos_countdown_s = 30.0
    coordinator = Coordinator(config, ride_id="ride-test")
    seed(coordinator)
    notifier = StubNotifier()
    safety = SafetyMonitor(
        config, coordinator, notifier=notifier, led_backend=RecordingLedBackend()
    )
    spoken = RecordingSpeaker()
    assistant = VoiceAssistant(
        config,
        coordinator,
        safety=safety,
        transcriber=FakeTranscriber(),
        speaker=spoken,
    )

    safety.sos.arm(EVENT)
    coordinator.processor.state_override = SystemState.SOS_ACTIVE
    await assistant._enter_preempt()

    assert any("Crash detected" in line for line in spoken.spoken)

    # No wake word: the rider may not be able to say one.
    reply = await assistant.hear("I'm okay")
    assert "cancelled" in reply.lower()
    await safety.sos.wait()
    assert notifier.calls == []
    assert assistant.stats.cancelled == 1


async def test_ordinary_commands_are_blocked_during_sos(config: AppConfig) -> None:
    config.safety.sos_countdown_s = 30.0
    coordinator = Coordinator(config, ride_id="ride-test")
    seed(coordinator)
    safety = SafetyMonitor(
        config, coordinator, notifier=StubNotifier(), led_backend=RecordingLedBackend()
    )
    assistant = VoiceAssistant(
        config,
        coordinator,
        safety=safety,
        transcriber=FakeTranscriber(),
        speaker=RecordingSpeaker(),
    )
    safety.sos.arm(EVENT)
    coordinator.processor.state_override = SystemState.SOS_ACTIVE

    reply = await assistant.hear("what's my speed")
    assert "cancel" in reply.lower()
    assert assistant.stats.preempted == 1
    safety.cancel_sos()
    await safety.sos.wait()


async def test_raw_pcm_is_dropped_after_transcription(config: AppConfig) -> None:
    assistant, _, _ = make_assistant(config)
    loud = (b"\x00\x40") * 256
    reading = SensorReading(
        timestamp=1.0,
        source=SensorSource.MIC,
        payload={"pcm": loud, "sample_rate": 16_000, "frames": 256},
    )
    await assistant.on_reading(reading)
    assert reading.payload["pcm"] == b"", "raw audio must not survive the handler"


def test_silence_is_detected_and_speech_is_not() -> None:
    assert is_silence(b"\x00\x00" * 64)
    loud = b"\x00\x7f" * 64
    assert pcm_rms(loud) > 200
    assert not is_silence(loud)


async def test_missing_models_do_not_prevent_startup() -> None:
    """A laptop without Vosk or Piper must still import and construct speakers."""
    from app.voice.stt import NullTranscriber, build_transcriber
    from app.voice.tts import RecordingSpeaker, build_speaker

    transcriber = build_transcriber("models/does-not-exist")
    speaker = build_speaker("models/does-not-exist.onnx")
    assert isinstance(transcriber, NullTranscriber)
    assert isinstance(speaker, RecordingSpeaker)
    assert transcriber.transcribe(b"\x00\x10" * 8, 16_000) == ""
    await speaker.speak("hello")
    assert speaker.spoken == ["hello"]
