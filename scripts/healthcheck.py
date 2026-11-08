"""Ask the app whether it is up. This is what the container health check runs."""
import os
import urllib.error
import urllib.request

PORT = os.environ.get("PORT", "8000")
URL = f"http://127.0.0.1:{PORT}/healthz"

try:
    with urllib.request.urlopen(URL, timeout=5) as response:
        body = response.read().decode("utf-8", "replace")
        if response.status == 200:
            print("ok")
            raise SystemExit(0)
        print(f"{URL} -> {response.status} {body.strip()}")
except urllib.error.URLError as error:
    print(f"{URL} -> {error}")
    raise SystemExit(1)

raise SystemExit(1)
