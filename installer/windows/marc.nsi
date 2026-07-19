; Instalador de MARC para Windows (rama installer/nsis-browser).
; Sin ventana propia -- arranca el backend Python empaquetado y abre el
; navegador default (ver launcher.py).

Unicode true

!define VERSION "1.0.0"
!define PRODUCT_NAME "MARC"
!define PUBLISHER "Antonio Baeza"

!include "MUI2.nsh"
!include "WinMessages.nsh"
!include "StrFunc.nsh"
${StrStr}
${UnStrStr}

Name "${PRODUCT_NAME}"
OutFile "..\..\dist\MARC-Setup.exe"
InstallDir "$LOCALAPPDATA\MARC"
InstallDirRegKey HKCU "Software\MARC" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma

; --- branding del asistente ---
!define MUI_ICON "assets\installer.ico"
!define MUI_UNICON "assets\installer.ico"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_BITMAP "assets\header.bmp"
!define MUI_WELCOMEFINISHPAGE_BITMAP "assets\welcome.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "assets\welcome.bmp"
!define MUI_ABORTWARNING

; --- paginas del instalador ---
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "LICENSE.txt"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\python\pythonw.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS '$\"$INSTDIR\launcher.py$\"'
!define MUI_FINISHPAGE_RUN_TEXT "Abrir MARC ahora"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Spanish"

; ============================================================
; Instalacion
; ============================================================

Section "MARC (nucleo)" SEC_CORE
  SectionIn RO

  SetOutPath "$INSTDIR\python"
  File /r /x "__pycache__" "..\..\runtime\windows-x86_64\python\*.*"

  SetOutPath "$INSTDIR\webapp"
  File /r /x "__pycache__" /x "config.json" /x "mkdocs.pid" /x "page_index.json" "..\..\webapp\*.*"

  SetOutPath "$INSTDIR\hooks"
  File /r /x "__pycache__" "..\..\hooks\*.*"

  SetOutPath "$INSTDIR\branding"
  File /r "..\..\branding\*.*"

  SetOutPath "$INSTDIR\vendor"
  File /r "..\..\vendor\*.*"

  SetOutPath "$INSTDIR\theme_overrides"
  File /r "..\..\theme_overrides\*.*"

  SetOutPath "$INSTDIR"
  File "..\..\mkdocs.yml"
  File "..\..\VERSION"
  File "launcher.py"
  File "assets\installer.ico"

  WriteRegStr HKCU "Software\MARC" "InstallDir" "$INSTDIR"

  CreateDirectory "$SMPROGRAMS\MARC"
  CreateShortcut "$SMPROGRAMS\MARC\MARC.lnk" "$INSTDIR\python\pythonw.exe" '"$INSTDIR\launcher.py"' "$INSTDIR\installer.ico"
  CreateShortcut "$SMPROGRAMS\MARC\Desinstalar MARC.lnk" "$INSTDIR\uninstall.exe"

  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; Registro para que aparezca correctamente en "Aplicaciones instaladas"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MARC" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MARC" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MARC" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MARC" "DisplayIcon" "$INSTDIR\installer.ico"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MARC" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MARC" "DisplayVersion" "${VERSION}"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MARC" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MARC" "NoRepair" 1
SectionEnd

Section "Icono en el escritorio" SEC_DESKTOP
  CreateShortcut "$DESKTOP\MARC.lnk" "$INSTDIR\python\pythonw.exe" '"$INSTDIR\launcher.py"' "$INSTDIR\installer.ico"
SectionEnd

Section "Agregar MARC al PATH" SEC_PATH
  ReadRegStr $0 HKCU "Environment" "Path"
  ${StrStr} $1 "$0;" "$INSTDIR;"
  StrCmp $1 "" 0 PathDone
    StrCmp $0 "" 0 +3
      WriteRegExpandStr HKCU "Environment" "Path" "$INSTDIR"
      Goto PathBroadcast
    WriteRegExpandStr HKCU "Environment" "Path" "$0;$INSTDIR"
  PathBroadcast:
    SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=5000
  PathDone:
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_CORE} "El programa en si -- Python autocontenido, MkDocs y MARC. Obligatorio."
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_DESKTOP} "Crea un icono de MARC en el escritorio."
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_PATH} "Permite escribir 'marc' desde una terminal para abrir la app -- util a futuro (ej. lanzar MARC desde scripts)."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ============================================================
; Desinstalacion
; ============================================================

Section "Uninstall"
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\webapp"
  RMDir /r "$INSTDIR\hooks"
  RMDir /r "$INSTDIR\branding"
  RMDir /r "$INSTDIR\vendor"
  RMDir /r "$INSTDIR\theme_overrides"
  Delete "$INSTDIR\mkdocs.yml"
  Delete "$INSTDIR\VERSION"
  Delete "$INSTDIR\launcher.py"
  Delete "$INSTDIR\installer.ico"
  Delete "$INSTDIR\_runtime_mkdocs.yml"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"

  Delete "$SMPROGRAMS\MARC\MARC.lnk"
  Delete "$SMPROGRAMS\MARC\Desinstalar MARC.lnk"
  RMDir "$SMPROGRAMS\MARC"
  Delete "$DESKTOP\MARC.lnk"

  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MARC"
  DeleteRegKey HKCU "Software\MARC"

  ; Quitar $INSTDIR del PATH, en cualquiera de las 4 posiciones posibles
  ; (unico valor, al inicio, al final, o en medio) -- sin tocar el resto.
  ReadRegStr $0 HKCU "Environment" "Path"
  StrCmp $0 "$INSTDIR" 0 +2
    DeleteRegValue HKCU "Environment" "Path"
    Goto PathRemoved

  ${UnStrStr} $1 "$0" "$INSTDIR;"
  StrCmp $1 "$0" AtStart
  ${UnStrStr} $1 "$0" ";$INSTDIR"
  StrCmp $1 "" NotFound
    StrLen $2 "$0"
    StrLen $3 $1
    IntOp $4 $2 - $3
    StrCpy $5 "$0" $4          ; parte antes de ";$INSTDIR"
    StrLen $6 ";$INSTDIR"
    IntOp $7 $3 - $6
    IntCmp $7 0 +2 +2 0
      StrCpy $8 $1 "" $6        ; lo que sigue despues (si habia mas repos en el PATH)
    StrCpy $0 "$5$8"
    WriteRegExpandStr HKCU "Environment" "Path" "$0"
    Goto PathRemoved
  AtStart:
    StrLen $6 "$INSTDIR;"
    StrCpy $0 "$0" "" $6
    WriteRegExpandStr HKCU "Environment" "Path" "$0"
    Goto PathRemoved
  NotFound:
  PathRemoved:
  SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=5000
SectionEnd
