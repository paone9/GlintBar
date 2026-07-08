' Launches glintbar with no console window. Double-click to run.
' Uses pythonw from PATH (works on any machine with Python installed).
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
sh.Run "pythonw """ & here & "\monitor.py""", 0, False
