# USB 3.0 Required for Live Operation

The RSPdx delivers ~64 MB/s at 8 MS/s sample rate (the project's
default). USB 2.0 caps sustained throughput at ~50 MB/s, causing
18-25% sample loss visible as "OsO" markers in tv_live logs.

Verify the SDR's USB link with:

    lsusb -t | grep -B5 -i sdrplay

Look for the bus speed:

    480M   -> USB 2.0  -> WILL drop samples, broken live operation
    5000M  -> USB 3.0  -> works
    10000M -> USB 3.1 Gen 2 -> works

If on USB 2.0, either:

 - Move the SDR cable to a USB 3.x port (look for blue plastic
   inside the receptacle), or
 - Use a USB 3.0 powered hub. USB 2.0 hubs do NOT work even when
   plugged into a USB 3.0 port — the hub itself caps the link.

File replay against a captured I/Q file works regardless of USB
speed because the I/Q is already on disk. Only LIVE operation
needs USB 3.0.
