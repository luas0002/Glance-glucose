"""
Libre 3 live glucose desktop widget (Windows/Mac/Linux).
Shows your current glucose reading in a small always-on-top window.
Refreshes every 60 seconds and is pulled from LibreLinkUp cloud.

SETUP (one-time):
  1. In the Libre 3 app on your phone: Menu -> Connected Apps -> LibreLinkUp
     -> invite a SECOND email address you own (not your Libre account email).
  2. Install the LibreLinkUp app on your phone, log in with that second
     email, and ACCEPT the invitation. You can uninstall the app afterwards.
  3. Create a file named ".env" in this same folder containing:
         LLU_EMAIL=your-linkup-email@example.com
         LLU_PASSWORD=your-password
  4. pip install requests python-dotenv
  5. python glucose_widget.py

NOTES:
  - Not for treatment decisions.
  - Uses the unofficial LibreLinkUp API (same one FLwatch/xDrip use).
    If Abbott bumps the required client version you may get "HTTP 403". Raise API_VERSION.
  - The widget window is draggable with the mouse. Right-click or press Esc to quit.
"""

import hashlib
import os
import threading
import time
import tkinter as tk
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

# Configuration
LLU_EMAIL = os.environ["LLU_EMAIL"]       
LLU_PASSWORD = os.environ["LLU_PASSWORD"]  
REGION = "eu"
API_VERSION = "4.16.0"
UNIT = "mmol" #Change to mg/dl if that's what you prefer.
LOW_LIMIT = 3.9          # (mmol/L)
HIGH_LIMIT = 10.0        # (mmol/L)
REFRESH_SECONDS = 60


BASE_URL = f"https://api-{REGION}.libreview.io"

HEADERS = {
    "accept-encoding": "gzip",
    "cache-control": "no-cache",
    "connection": "Keep-Alive",
    "content-type": "application/json",
    "product": "llu.android",
    "version": API_VERSION,
}

TREND_ARROWS = {1: "\u2193", 2: "\u2198", 3: "\u2192", 4: "\u2197", 5: "\u2191"}


class LibreLinkUpClient:
    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.token = None
        self.account_id_hash = None
        self.patient_id = None
        self.base_url = BASE_URL

    def _headers(self):
        h = dict(HEADERS)
        if self.token:
            h["authorization"] = f"Bearer {self.token}"
        if self.account_id_hash:
            h["account-id"] = self.account_id_hash
        return h

    def login(self):
        r = requests.post(
            f"{self.base_url}/llu/auth/login",
            headers=self._headers(),
            json={"email": self.email, "password": self.password},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data", {})

        # Redirects may occur to your correct regional server.
        if data.get("redirect"):
            region = data["region"]
            self.base_url = f"https://api-{region}.libreview.io"
            return self.login()

        auth = data.get("authTicket", {})
        self.token = auth.get("token")
        user_id = data.get("user", {}).get("id", "")
        self.account_id_hash = hashlib.sha256(user_id.encode()).hexdigest()
        if not self.token:
            raise RuntimeError(f"Login failed: {r.text[:200]}")

    def get_patient_id(self):
        r = requests.get(
            f"{self.base_url}/llu/connections",
            headers=self._headers(),
            timeout=15,
        )
        r.raise_for_status()
        connections = r.json().get("data", [])
        if not connections:
            raise RuntimeError(
                "No connections found - did you accept the LibreLinkUp invite?"
            )
        self.patient_id = connections[0]["patientId"]

    def latest_reading(self):
        """Returns (value, trend_arrow, timestamp_str). Value in configured UNIT."""
        if not self.token:
            self.login()
        if not self.patient_id:
            self.get_patient_id()

        r = requests.get(
            f"{self.base_url}/llu/connections/{self.patient_id}/graph",
            headers=self._headers(),
            timeout=15,
        )
        if r.status_code in (401, 403):
            self.token = None
            self.login()
            r = requests.get(
                f"{self.base_url}/llu/connections/{self.patient_id}/graph",
                headers=self._headers(),
                timeout=15,
            )
        r.raise_for_status()

        gm = r.json()["data"]["connection"]["glucoseMeasurement"]
        mgdl = gm["ValueInMgPerDl"]
        value = round(mgdl / 18.016, 1) if UNIT == "mmol" else mgdl
        arrow = TREND_ARROWS.get(gm.get("TrendArrow"), "?")
        ts = gm.get("Timestamp", "")
        return value, arrow, ts


class GlucoseWidget:
    def __init__(self):
        self.client = LibreLinkUpClient(LLU_EMAIL, LLU_PASSWORD)
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#1e1e1e")
        self.root.geometry("+40+40")

        self.value_label = tk.Label(
            self.root, text="--", font=("Segoe UI", 28, "bold"),
            fg="white", bg="#1e1e1e", padx=14, pady=2,
        )
        self.value_label.pack()
        self.sub_label = tk.Label(
            self.root, text="connecting...", font=("Segoe UI", 9),
            fg="#aaaaaa", bg="#1e1e1e", pady=2,
        )
        self.sub_label.pack()


        for w in (self.root, self.value_label, self.sub_label):
            w.bind("<Button-1>", self._start_drag)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<Button-3>", lambda e: self.root.destroy())
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _start_drag(self, event):
        self._dx, self._dy = event.x, event.y

    def _drag(self, event):
        x = self.root.winfo_x() + event.x - self._dx
        y = self.root.winfo_y() + event.y - self._dy
        self.root.geometry(f"+{x}+{y}")

    def _color(self, value):
        low = LOW_LIMIT if UNIT == "mmol" else LOW_LIMIT * 18.016
        high = HIGH_LIMIT if UNIT == "mmol" else HIGH_LIMIT * 18.016
        if value < low:
            return "#ff4444" 
        if value > high:
            return "#ffaa00"     
        return "#44dd77"         

    def _poll_loop(self):
        while True:
            try:
                value, arrow, ts = self.client.latest_reading()
                unit_txt = "mmol/L" if UNIT == "mmol" else "mg/dL"
                self.value_label.config(
                    text=f"{value} {arrow}", fg=self._color(value)
                )
                self.sub_label.config(
                    text=f"{unit_txt} \u00b7 {datetime.now():%H:%M}"
                )
            except Exception as e:
                self.sub_label.config(text=f"error: {str(e)[:40]}")
            time.sleep(REFRESH_SECONDS)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    GlucoseWidget().run()