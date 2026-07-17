#!/usr/bin/env python3
"""One-shot Jellyfin Live TV setup for an STVT tuner.

Drives a fresh Jellyfin server's API to: complete the first-run wizard (create
an admin user), add the STVT HDHomeRun tuner (stvt_hdhr.py), add its XMLTV
guide, and refresh the guide. Idempotent-ish: run it once on a brand-new
Jellyfin install. Config via environment variables:

  JF_URL      Jellyfin base URL         (default http://localhost:8096)
  JF_USER     admin username to create  (default admin)
  JF_PASS     admin password to set     (default changeme — OVERRIDE THIS)
  STVT_TUNER  tuner base URL            (default http://raspberrypi.local:5004)

Example:
  JF_PASS='my-password' STVT_TUNER='http://192.168.1.100:5004' python3 jf_setup.py

Jellyfin 10.11 rejects a blank password. To reset a Jellyfin to first-run:
  sudo systemctl stop jellyfin
  sudo rm -rf /var/lib/jellyfin/data /var/lib/jellyfin/root /etc/jellyfin/system.xml
  sudo systemctl start jellyfin
"""
import json, os, time, urllib.request, urllib.error

BASE = os.environ.get("JF_URL", "http://localhost:8096").rstrip("/")
TUNER = os.environ.get("STVT_TUNER", "http://raspberrypi.local:5004").rstrip("/")
ADMIN = os.environ.get("JF_USER", "admin")
PASSWORD = os.environ.get("JF_PASS", "changeme")
AUTH_HDR = ('MediaBrowser Client="stvt", Device="stvt-setup", '
            'DeviceId="stvt-setup", Version="1.0.0"')


def call(method, path, body=None, token=None, params=""):
    req = urllib.request.Request(BASE + path + params,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Emby-Authorization", AUTH_HDR)
    if token:
        req.add_header("Authorization", f'MediaBrowser Token="{token}"')
        req.add_header("X-Emby-Token", token)
    try:
        r = urllib.request.urlopen(req, timeout=25)
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return None, str(e)


def main():
    print(f"Jellyfin: {BASE}   tuner: {TUNER}   admin: {ADMIN}")
    call("POST", "/Startup/Configuration",
         {"UICulture": "en-US", "MetadataCountryCode": "US",
          "PreferredMetadataLanguage": "en"})
    call("GET", "/Startup/User")
    s, r = call("POST", "/Startup/User", {"Name": ADMIN, "Password": PASSWORD})
    print(f"create admin: {s} {r if s not in (200, 204) else ''}")
    call("POST", "/Startup/Complete")

    s, r = call("POST", "/Users/AuthenticateByName",
                {"Username": ADMIN, "Pw": PASSWORD})
    if s != 200 or not isinstance(r, dict):
        print(f"AUTH FAILED: {s} {r} — is this a fresh Jellyfin? (see reset note)")
        return
    token = r["AccessToken"]

    s, r = call("POST", "/LiveTv/TunerHosts",
                {"Type": "hdhomerun", "Url": TUNER}, token=token)
    print(f"add tuner: {s} "
          f"{'id=' + r.get('Id', '?') if isinstance(r, dict) else r}")

    s, r = call("POST", "/LiveTv/ListingProviders",
                {"Type": "xmltv", "Path": TUNER + "/guide.xml",
                 "EnableAllTuners": True},
                token=token, params="?validateListings=false")
    print(f"add guide: {s} id={r.get('Id') if isinstance(r, dict) else r}")

    time.sleep(2)
    _, tasks = call("GET", "/ScheduledTasks", token=token)
    tid = next((t.get("Id") for t in (tasks if isinstance(tasks, list) else [])
                if "guide" in (t.get("Key", "") + t.get("Name", "")).lower()), None)
    if tid:
        call("POST", f"/ScheduledTasks/Running/{tid}", token=token)
    time.sleep(3)
    _, ch = call("GET", "/LiveTv/Channels", token=token)
    n = ch.get("TotalRecordCount") if isinstance(ch, dict) else "?"
    print(f"Live TV channels: {n}")
    print(f"\nDone. Log in at {BASE} as '{ADMIN}' and change the password.")


if __name__ == "__main__":
    main()
