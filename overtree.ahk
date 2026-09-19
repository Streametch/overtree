#NoEnv
#SingleInstance Force
SetBatchLines, -1

; При старте AHK запускаем автономный скрытый .exe файл
Run, overtree_overlay.exe,, Hide

; Запускаем таймер проверки процесса выключения
SetTimer, CheckProcess, 1000

; Безотказный нативный таймер слежения за Проводником (каждые 150 мс)
SetTimer, WatchExplorer, 150

global prevPath := "" ; Переменная для запоминания прошлого пути
return

CheckProcess:
    Process, Exist, overtree_overlay.exe
    if (!ErrorLevel)
        ExitApp
return

; Функция нативного отслеживания переходов по папкам
WatchExplorer:
    ; Проверяем, активен ли режим Follow в overtree.ini
    IniRead, followMode, %A_AppData%\overtree.ini, General, FollowMode, False
    if (followMode != "True")
        return

    ; Проверяем, что сейчас активно именно окно Проводника
    if WinActive("ahk_class CabinetWClass") {
        WinGet, hwnd, ID, A
        currentPath := GetExplorerPath(hwnd)
        
        ; Если путь изменился (пользователь перешел в другую папку)
        if (currentPath != "" && currentPath != prevPath) {
            prevPath := currentPath
            ; Мгновенно шлем новый путь сонару по каналу!
            SendPathToPipe(currentPath)
        }
    }
return

; ХОТКЕЙ ВЫЗОВА ОКНА ВРУЧНУЮ (Ctrl + Shift + T)
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

; Чистая функция извлечения пути папки из активного окна Проводника
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

; Функция отправки пути в системный именованный канал Windows в формате UTF-8
SendPathToPipe(path) {
    pipe := DllCall("CreateFile", "Str", "\\.\pipe\overtree_pipe", "UInt", 0x40000000, "UInt", 0, "Ptr", 0, "UInt", 3, "UInt", 0, "Ptr", 0)
    if (pipe != -1) {
        ; Вычисляем размер буфера для UTF-8 строки
        size := StrPut(path, "UTF-8")
        VarSetCapacity(utf8_buffer, size, 0)
        StrPut(path, &utf8_buffer, "UTF-8")
        
        ; Отправляем UTF-8 буфер в пайп
        DllCall("WriteFile", "Ptr", pipe, "Ptr", &utf8_buffer, "UInt", size - 1, "UInt*", bytesWritten, "Ptr", 0)
        DllCall("CloseHandle", "Ptr", pipe)
    }
}
