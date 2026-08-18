from gpiozero import LED
from time import sleep

red_led = LED(17)
yellow_led = LED(27)
green_led = LED(22)

print("Traffic light  sequence starting... Press ctrl + C to STOP")
try:
	while True:
		red_led.on()
		sleep(3)
		red_led.off()

		green_led.on()
		sleep(3)
		green_led.off

		yellow_led.on()
		sleep(1)
		yellow_led.off
except KeyboardInterrupt:
	print("\nSequence stopped.  Hardware removed.")
