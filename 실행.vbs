' Double-click to open the trading GUI without a console window
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Environment("Process")("UV_LINK_MODE") = "copy"
sh.Run "uv run pythonw gui.py", 0, False
