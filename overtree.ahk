#NoEnv
#SingleInstance Force
SetBatchLines, -1


Run, overtree_overlay.exe,, Hide

SetTimer, CheckProcess, 1000

SetTimer, WatchExplorer, 150

global prevPath := ""
return

CheckProcess:
    Process, Exist, overtree_overlay.exe
    if (!ErrorLevel)
        ExitApp
return

WatchExplorer:

    IniRead, followMode, %A_AppData%\overtree.ini, General, FollowMode, False
    if (followMode != "True")
        return


    if WinActive("ahk_class CabinetWClass") {
        WinGet, hwnd, ID, A
        currentPath := GetExplorerPath(hwnd)
        if (currentPath != "" && currentPath != prevPath) {
            prevPath := currentPath
            SendPathToPipe(currentPath)
        }
    }
return

#IfWinActive ahk_class CabinetWClass
^+t::
    WinGet, hwnd, ID, A
    currentPath := GetExplorerPath(hwnd)
    if (currentPath != "") {
        prevPath := currentPath
        SendPathToPipe(currentPath)
    }
return
#IfWinActive

GetExplorerPath(hwnd) {
    try {
        for window in ComObjCreate("Shell.Application").Windows {
            if (window.hwnd == hwnd) {
                for item in window.Document.SelectedItems {
                    if item.IsFolder
                        return item.Path
                }
                return window.Document.Folder.Self.Path
            }
        }
    }
    return ""
}

SendPathToPipe(path) {
    pipe := DllCall("CreateFile", "Str", "\\.\pipe\overtree_pipe", "UInt", 0x40000000, "UInt", 0, "Ptr", 0, "UInt", 3, "UInt", 0, "Ptr", 0)
    if (pipe != -1) {
        size := StrPut(path, "UTF-8")
        VarSetCapacity(utf8_buffer, size, 0)
        StrPut(path, &utf8_buffer, "UTF-8")
        
        DllCall("WriteFile", "Ptr", pipe, "Ptr", &utf8_buffer, "UInt", size - 1, "UInt*", bytesWritten, "Ptr", 0)
        DllCall("CloseHandle", "Ptr", pipe)
    }
}
