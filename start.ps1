# Start the voice-clone server
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
& "$root\venv\Scripts\python.exe" "$root\backend\app.py"
