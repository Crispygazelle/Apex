"""Phase 4 against a real Vosk model, not a scripted FakeTranscriber.

These tests synthesize speech with macOS `say`, run it through the small
English model, and assert the wake word, intent, and command path still
work when the transcript is produced by the recogniser a helmet will use.

They skip when the model or the synthesizer is missing, so the rest of the
suite stays green on a machine that has not run scripts/fetch_models.sh.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from app.config import AppConfig
from app.voice.intent import IntentName, IntentParser
from app.voice.stt import VoskTranscriber, build_transcriber, is_silence
from app.voice.tts import RecordingSpeaker
from app.voice.wake_word import WakeWordDetector
from tests.test_voice import make_assistant

VOSK_MODEL = Path("models/vosk-model-small-en-us-0.15")
PIPER_MODEL = Path("models/en_US-amy-low.onnx")


def _have_say() -> bool:
    return shutil.which("say") is not None and shutil.which("afconvert") is not None


needs_vosk = pytest.mark.skipif(
    not VOSK_MODEL.exists(), reason="run scripts/fetch_models.sh"
)
needs_say = pytest.mark.skipif(not _have_say(), reason="macOS say/afconvert required")
needs_piper = pytest.mark.skipif(
    not PIPER_MODEL.exists(), reason="run scripts/fetch_models.sh"
)


@pytest.fixture(scope="module")
def vosk() -> VoskTranscriber:
    if not VOSK_MODEL.exists():
        pytest.skip("run scripts/fetch_models.sh")
    return VoskTranscriber(VOSK_MODEL)


def synthesize(text: str, dest: Path) -> bytes:
    """16 kHz 16-bit mono PCM of `text`, via the system voice."""
    aiff = dest.with_suffix(".aiff")
    wav = dest.with_suffix(".wav")
    subprocess.run(
        ["say", "-o", str(aiff), "--rate=160", text],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", str(aiff), str(wav)],
        check=True,
        capture_output=True,
    )
    with wave.open(str(wav), "rb") as handle:
        assert handle.getframerate() == 16_000
        assert handle.getnchannels() == 1
        pcm = handle.readframes(handle.getnframes())
    if is_silence(pcm):
        pytest.skip("speech synthesis produced silence; run this test unsandboxed")
    return pcm


@pytest.fixture(scope="module")
def spoken(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    if not _have_say():
        pytest.skip("macOS say/afconvert required")
    folder = tmp_path_factory.mktemp("spoken")
    phrases = {
        "speed": "hey apex what is my speed",
        "hazard": "hey apex log hazard pothole",
        "cancel": "cancel",
        "status": "hey apex status report",
    }
    return {name: synthesize(text, folder / name) for name, text in phrases.items()}


@needs_vosk
def test_build_transcriber_loads_the_real_model() -> None:
    transcriber = build_transcriber(str(VOSK_MODEL))
    assert isinstance(transcriber, VoskTranscriber)


@needs_vosk
@needs_say
def test_vosk_hears_speed_and_the_wake_word(
    vosk: VoskTranscriber, spoken: dict[str, bytes]
) -> None:
    text = vosk.transcribe(spoken["speed"], 16_000)
    match = WakeWordDetector(["apex", "hey apex"]).match(text)
    intent = IntentParser().parse(match.remainder)

    assert "apex" in text
    assert "speed" in text
    assert match.heard
    assert intent.name is IntentName.SPEED


@needs_vosk
@needs_say
def test_vosk_hears_a_pothole(vosk: VoskTranscriber, spoken: dict[str, bytes]) -> None:
    text = vosk.transcribe(spoken["hazard"], 16_000)
    match = WakeWordDetector(["apex", "hey apex"]).match(text)
    intent = IntentParser().parse(match.remainder)

    assert match.heard
    assert intent.name is IntentName.HAZARD
    assert intent.hazard_type == "pothole"


@needs_vosk
@needs_say
def test_vosk_hears_cancel_without_a_wake_word(
    vosk: VoskTranscriber, spoken: dict[str, bytes]
) -> None:
    text = vosk.transcribe(spoken["cancel"], 16_000)
    assert "cancel" in text
    assert IntentParser().parse(text).name is IntentName.CANCEL


@needs_vosk
def test_vosk_silence_is_empty(vosk: VoskTranscriber) -> None:
    silence = b"\x00\x00" * 16_000  # one second of zeros
    assert is_silence(silence)
    assert vosk.transcribe(silence, 16_000) == ""


@needs_vosk
@needs_say
def test_vosk_does_not_retain_the_pcm_buffer(
    vosk: VoskTranscriber, spoken: dict[str, bytes]
) -> None:
    pcm = spoken["speed"]
    vosk.transcribe(pcm, 16_000)
    leaked = [value for value in vars(vosk).values() if value is pcm]
    assert leaked == [], "the transcriber must not keep a reference to the buffer"


@needs_vosk
@needs_say
async def test_real_transcript_drives_the_assistant(
    config: AppConfig, vosk: VoskTranscriber, spoken: dict[str, bytes]
) -> None:
    """The words Vosk actually produced must be enough to answer a speed query."""
    assistant, _, speaker = make_assistant(config, speaker=RecordingSpeaker())
    text = vosk.transcribe(spoken["speed"], 16_000)
    reply = await assistant.hear(text)
    assert "48" in reply
    assert speaker.spoken[-1] == reply


@needs_vosk
@needs_piper
def test_piper_speech_is_understood_by_vosk(vosk: VoskTranscriber, tmp_path: Path) -> None:
    """The two models this helmet ships must understand each other.

    If Piper says 'hey apex what is my speed' and Vosk hears something else,
    the rider never gets an answer no matter how good each piece looks alone.
    """
    from piper import PiperVoice

    voice = PiperVoice.load(str(PIPER_MODEL))
    wav_path = tmp_path / "piper.wav"
    with wave.open(str(wav_path), "wb") as handle:
        voice.synthesize_wav("hey apex what is my speed", handle)

    with wave.open(str(wav_path), "rb") as handle:
        pcm = handle.readframes(handle.getnframes())
        rate = handle.getframerate()

    text = vosk.transcribe(pcm, rate)
    match = WakeWordDetector(["apex", "hey apex"]).match(text)
    intent = IntentParser().parse(match.remainder)

    assert match.heard, f"wake word missing in {text!r}"
    assert intent.name is IntentName.SPEED, f"intent {intent} from {text!r}"
