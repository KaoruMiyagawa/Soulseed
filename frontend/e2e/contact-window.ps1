param([int]$ProcessId, [ValidateSet('Minimize','Restore','Close')][string]$Command)
$ErrorActionPreference='Stop'
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class ContactWindow {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr window, int command);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr window);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr window, uint message, IntPtr w, IntPtr l);
    public delegate bool EnumProc(IntPtr window, IntPtr unused);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc callback, IntPtr unused);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr window, System.Text.StringBuilder title, int count);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    public static IntPtr Find(uint pid) {
        IntPtr found = IntPtr.Zero;
        EnumWindows((window, unused) => {
            uint owner;
            GetWindowThreadProcessId(window, out owner);
            if (owner != pid) return true;
            var title = new System.Text.StringBuilder(256);
            GetWindowText(window, title, title.Capacity);
            if (title.ToString() != "Soulseed") return true;
            found = window;
            return false;
        }, IntPtr.Zero);
        return found;
    }
}
'@
$window=[ContactWindow]::Find($ProcessId)
if ($window -eq [IntPtr]::Zero) { throw 'Missing Soulseed window' }
if ($Command -eq 'Close') {
    if (-not [ContactWindow]::PostMessage($window,0x10,[IntPtr]::Zero,[IntPtr]::Zero)) { throw 'Cannot send WM_CLOSE' }
    if (-not (Get-Process -Id $ProcessId).WaitForExit(10000)) { throw 'Desktop did not exit' }
    return
}
$minimize=$Command -eq 'Minimize'
[void][ContactWindow]::ShowWindow($window, $(if ($minimize) { 6 } else { 9 }))
if ([ContactWindow]::IsIconic($window) -ne $minimize) { throw 'Unexpected window state' }
