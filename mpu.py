from mpu6050 import mpu6050
import time

# Create a new MPU6050 object using its default I2C address (0x68)
sensor = mpu6050(0x68)

print("MPU6050 Desk Test Initialized")
print("Reading Telemetry Data...")
print("-" * 50)

while True:
    # Read the accelerometer and gyroscope data
    accel_data = sensor.get_accel_data()
    gyro_data = sensor.get_gyro_data()

    # Convert accelerometer data from m/s^2 to G-force
    accel_x_g = accel_data['x'] / 9.81
    accel_y_g = accel_data['y'] / 9.81
    accel_z_g = accel_data['z'] / 9.81

    # Format and print the data
    print(f"Accel (g):    X={accel_x_g:5.2f} | Y={accel_y_g:5.2f} | Z={accel_z_g:5.2f}")
    print(f"Gyro (deg/s): X={gyro_data['x']:5.2f} | Y={gyro_data['y']:5.2f} | Z={gyro_data['z']:5.2f}")
    print("-" * 50)
    
    # Pause for 1 second before fetching the next reading
    time.sleep(1)
