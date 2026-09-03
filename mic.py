import pyaudio
import wave

# Audio configuration parameters
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000  
#16kHz is optimal for offline STT models like Vosk
CHUNK = 1024
RECORD_SECONDS = 5
OUTPUT_FILENAME = "helmet_command.wav"

audio = pyaudio.PyAudio()

# Start recording
stream = audio.open(format=FORMAT,
                    channels=CHANNELS,
                    rate=RATE,
                    input=True,
                    frames_per_buffer=CHUNK)

print("Listening for rider command...")
frames = []

for i in range(0, int(RATE / CHUNK * RECORD_SECONDS)):
    data = stream.read(CHUNK)
    frames.append(data)

print("Recording complete. Processing...")

# Stop and close the stream
stream.stop_stream()
stream.close()
audio.terminate()

# Save the recorded data as a WAV file
with wave.open(OUTPUT_FILENAME, 'wb') as wf:
    wf.setnchannels(CHANNELS)
    wf.setsampwidth(audio.get_sample_size(FORMAT))
    wf.setframerate(RATE)
    wf.writeframes(b''.join(frames))
