on run argv
    if (count of argv) < 1 then error "Не передан путь к JSX."
    set scriptFile to POSIX file (item 1 of argv)
    tell application id "com.adobe.AfterEffects.application"
        activate
        DoScriptFile scriptFile
    end tell
end run
