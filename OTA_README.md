# OTA Upload Documentation

To flash your ESP32 over WiFi using PlatformIO, follow these steps:

1. **Find the ESP32 IP Address:**
   - Check your Render server logs (the terminal on the main page).
   - When the ESP32 boots, it sends its local IP address to the `/boot` endpoint, which is logged in the terminal.

2. **Update `platformio.ini`:**
   - Locate the `[env:esp32dev_ota]` section in your `platformio.ini` file.
   - Replace `192.168.1.XXX` with the actual IP address you found in the logs.

3. **Flash via OTA:**
   - In the PlatformIO sidebar, expand the project tasks.
   - Select the `esp32dev_ota` environment.
   - Run the `Upload` task.

4. **Switching back to Serial:**
   - To flash via USB, simply select the `esp32dev` environment in the PlatformIO tasks and run the `Upload` task.
