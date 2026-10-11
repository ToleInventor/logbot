import time
import requests

URLS = [
    "https://backend-y4ud.onrender.com/",
    "https://auto-what.onrender.com/"
]

# Set the interval in seconds (2 minutes = 120 seconds)
INTERVAL = 120

print("Starting URL pinger. Press Ctrl+C to stop.")

while True:
    for url in URLS:
        try:
            # Send a GET request with a timeout to avoid hanging indefinitely
            response = requests.get(url, timeout=10)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Pinged {url} -> Status Code: {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Failed to ping {url} -> Error: {e}")
            
    # Wait for 2 minutes before the next round
    time.sleep(INTERVAL)
