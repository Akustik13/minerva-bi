; «Пошта Minerva» — додаткові кроки інсталятора NSIS (electron-builder, nsis.include).
; minerva-mail:// — посилання «🔗 Підключити програму» зі сторінки Minerva відкриває програму вже з адресою й кодом.

!macro customInstall
  WriteRegStr HKCU "Software\Classes\minerva-mail" "" "URL:Minerva Mail"
  WriteRegStr HKCU "Software\Classes\minerva-mail" "URL Protocol" ""
  WriteRegStr HKCU "Software\Classes\minerva-mail\DefaultIcon" "" "$INSTDIR\MinervaMail.exe,0"
  WriteRegStr HKCU "Software\Classes\minerva-mail\shell\open\command" "" '"$INSTDIR\MinervaMail.exe" "%1"'
!macroend

!macro customUnInstall
  ${IfNot} ${isUpdated}
    DeleteRegKey HKCU "Software\Classes\minerva-mail"
    DeleteRegKey HKCU "Software\Classes\MinervaMail.mailto"
    DeleteRegKey HKCU "Software\Clients\Mail\Minerva Mail"
    DeleteRegValue HKCU "Software\RegisteredApplications" "Minerva Mail"
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "com.minerva.mail"
  ${EndIf}
!macroend
