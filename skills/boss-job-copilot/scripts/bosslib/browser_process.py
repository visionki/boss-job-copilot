"""Windows Chrome ownership checks for the dedicated profile, without database access."""
import json
import os
import re
import subprocess
from pathlib import Path

from .local import Stopped


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE: observe exit only.
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not evidence of exit.
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def powershell(command, **environment):
    command = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); " + command
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                            env={**os.environ, **environment}, capture_output=True, text=True,
                            encoding="utf-8", timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise Stopped("browser_process_check_failed")
    return json.loads(result.stdout.strip() or "[]")


def chrome_processes(profile):
    """Return PID + creation time, so a recycled PID is never treated as an owned child."""
    if os.name != "nt":
        return []
    rows = powershell('''@(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
        Select-Object ProcessId,CommandLine,@{Name='Created';Expression={$_.CreationDate.ToUniversalTime().ToString('o')}}) |
        ConvertTo-Json -Compress''')
    if isinstance(rows, dict):
        rows = [rows]
    result = []
    for row in rows:
        match = re.search(r'--user-data-dir=(.+?)(?="?\s+--|"?$)', row.get("CommandLine") or "")
        if match and Path(match.group(1).strip('"')).resolve() == Path(profile).resolve():
            result.append({"pid": row["ProcessId"], "created": row["Created"]})
    return result


def kill_owned_chrome(profile, owned):
    # Recheck both the exact profile and process creation time immediately before termination.
    known = {(row["pid"], row["created"]) for row in owned}
    remaining = [row for row in chrome_processes(profile) if (row["pid"], row["created"]) in known]
    if remaining:
        powershell('''$owned = @($env:BOSS_COPILOT_OWNED_CHROME | ConvertFrom-Json)
            Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | ForEach-Object {
                $process = $_
                $created = $process.CreationDate.ToUniversalTime().ToString('o')
                if ($owned | Where-Object { $_.pid -eq $process.ProcessId -and $_.created -eq $created }) {
                    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
                }
            }''', BOSS_COPILOT_OWNED_CHROME=json.dumps(remaining))
    return len(remaining)
