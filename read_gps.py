import serial
import pynmea2

# point to port where gps is sending data

port = "/dev/ttyAMA0"
ser = serial.Serial(port, baudrate=9600, timeout=1)

print("Listening for clean GPS data.......")

while True:
	try:
# Reading data and decode it
		line = ser.readline().decode('utf-8', errors='replace').strip()
		if line.startswith('$GPRMC') or line.startswith('$GNRMC'):
			msg = pynmea2.parse(line)
			if msg.status == 'A': # A is active
				print(f"Latitude: {msg.latitude:.6f}, Longitude: {msg.longitude:.6f}")
			else:
				print("Waiting for satellite lock...")
	except pynmea2.ParseError:
	#To clean text
		continue
	except KeyboardInterrupt:
		print("\nEXITINGGGG......")
		break

