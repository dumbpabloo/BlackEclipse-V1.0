; Black Eclipse - per-user installer.
;
; Why per-user: the application resolves `python/harness` and its `.black-eclipse`
; runtime locks from `process.cwd()` (see docs/desktop-app-plan.md section 6).
; That means the install directory MUST be writable by the user who runs it.
; Program Files is not, so a per-user install is a correctness requirement, not
; a convenience.
;
; User data lives in %LOCALAPPDATA%\BlackEclipse\data (BE_DATA_DIR, set by the
; bootstrap) and is deliberately NOT removed on uninstall.
;
; Branding: the icon and the two bitmaps under assets/ are generated from
; public/branding by `node scripts/make-installer-assets.js` and committed.
; MUI_ICON is what Explorer draws for this setup .exe; without it NSIS ships
; its stock "NSIS globe" icon.

Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "WinMessages.nsh"

!ifndef APP_VERSION
  !define APP_VERSION "0.0.0"
!endif
!ifndef SRCDIR
  !define SRCDIR "..\..\dist\BlackEclipse"
!endif
!ifndef OUTFILE
  !define OUTFILE "..\..\dist\BlackEclipse-Setup.exe"
!endif
!ifndef PACKSDIR
  !define PACKSDIR "..\..\dist\packs"
!endif

Name "Black Eclipse"
Caption "Black Eclipse Setup"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\BlackEclipse"
InstallDirRegKey HKCU "Software\BlackEclipse" "InstallDir"
RequestExecutionLevel user
BrandingText "Black Eclipse Setup  |  ${APP_VERSION}"

VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "Black Eclipse"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "FileDescription" "Black Eclipse Setup"
VIAddVersionKey "CompanyName" "Black Eclipse"
VIAddVersionKey "LegalCopyright" "MIT License"

SetCompressor /SOLID lzma
SetCompressorDictSize 64

; --- branding ----------------------------------------------------------------
; The same mark the installed app and the boot screen use, so setup reads as
; Black Eclipse rather than a generic Windows wizard.
!define MUI_ICON "assets\icon.ico"
!define MUI_UNICON "assets\icon.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "assets\welcome.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "assets\welcome.bmp"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_BITMAP "assets\header.bmp"
!define MUI_HEADERIMAGE_UNBITMAP "assets\header.bmp"
!define MUI_HEADERIMAGE_RIGHT
!define MUI_BGCOLOR "FFFFFF"
!define MUI_TEXTCOLOR "0B0B0E"
!define MUI_ABORTWARNING

!define MUI_WELCOMEPAGE_TITLE "Black Eclipse ${APP_VERSION}"
!define MUI_WELCOMEPAGE_TEXT "Local-first market research and trading workspace. Everything runs on this machine: no account, no sign-in, no telemetry.$\r$\n$\r$\nInstalls for the current Windows user in %LOCALAPPDATA%\Programs\BlackEclipse. No administrator rights, no Node.js and no Python are required.$\r$\n$\r$\nYour data lives in %LOCALAPPDATA%\BlackEclipse\data and stays there if you uninstall.$\r$\n$\r$\nThis build is not code-signed yet. If Windows shows a blue SmartScreen prompt, choose More info, then Run anyway. SHA256SUMS.txt on the release page lets you verify the download."

!define MUI_INSTFILESPAGE_FINISHHEADER_TEXT "Black Eclipse is installed"
!define MUI_INSTFILESPAGE_FINISHHEADER_SUBTEXT "Finishing up."

!define MUI_FINISHPAGE_NOREBOOTSUPPORT
!define MUI_FINISHPAGE_TITLE "Ready to go"
!define MUI_FINISHPAGE_TEXT "Black Eclipse is installed for this Windows user.$\r$\n$\r$\nThe first launch takes a moment while the local agent runtime starts. Open it from the Start menu, the desktop shortcut, or the checkbox below."
!define MUI_FINISHPAGE_RUN "$INSTDIR\Black Eclipse.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Open Black Eclipse now"

; The welcome page's stock button says "Next"; this setup is one decision, so
; name the action plainly. (The progress and finish pages keep their native
; buttons - that is what makes a Windows installer predictable.)
!define MUI_PAGE_CUSTOMFUNCTION_SHOW WelcomePageShow
Function WelcomePageShow
  SendMessage $mui.Button.Next ${WM_SETTEXT} 0 "STR:Install"
FunctionEnd

!insertmacro MUI_PAGE_WELCOME
; No directory page: the location is fixed per user. Silent installs still honor
; the standard `/D=` override.
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

; Uninstaller: replace the stock confirmation box with the same branded welcome
; screen, so removal explains what survives (the user's data) before it starts.
!define MUI_WELCOMEPAGE_TITLE "Remove Black Eclipse"
!define MUI_WELCOMEPAGE_TEXT "This removes Black Eclipse for the current Windows user.$\r$\n$\r$\nYour database, journals and settings are not touched. They stay in %LOCALAPPDATA%\BlackEclipse\data, so reinstalling brings your workspace back."
!define MUI_PAGE_CUSTOMFUNCTION_SHOW "un.UnWelcomePageShow"
Function un.UnWelcomePageShow
  SendMessage $mui.Button.Next ${WM_SETTEXT} 0 "STR:Remove"
FunctionEnd

!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; ── Progress reporting ────────────────────────────────────────────────────────
; NSIS installs file by file, and the stock instfiles page prints one
; `Extract: <path>` line per file in the status strip. The payload is ~100k
; files (Next server, node_modules, the vendored Python tree), so that reads as
; a frozen installer printing paths forever — the single most common "is it
; stuck?" report. Keep the per-file list in the DETAILS pane (where it is useful
; for support) and report coarse phases in the status strip instead. This
; changes what is displayed, not how long the work takes.
!macro BeginPhase TEXT
  DetailPrint "${TEXT}"
  SetDetailsPrint listonly
!macroend
!macro EndPhase TEXT
  SetDetailsPrint both
  DetailPrint "${TEXT}"
!macroend

Section "Black Eclipse" SecMain
  SetOutPath "$INSTDIR"
  ; Replace the payload atomically enough: remove the old application tree first
  ; so removed files from a previous version cannot linger. NEVER touch the data
  ; directory, which is a sibling of $INSTDIR, not inside it. Removing the old
  ; tree is also file-by-file, so it gets a phase of its own instead of a silent
  ; pause before the bar appears to move.
  !insertmacro BeginPhase "Removing the previous version..."
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\runtime"
  !insertmacro EndPhase "Previous version removed"

  !insertmacro BeginPhase "Installing application files (this can take a few minutes)..."
  File /r "${SRCDIR}\*.*"
  !insertmacro EndPhase "Application files installed"

  WriteRegStr HKCU "Software\BlackEclipse" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\BlackEclipse" "Version" "${APP_VERSION}"

  ; Add/Remove Programs entry (per-user hive).
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlackEclipse" "DisplayName" "Black Eclipse"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlackEclipse" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlackEclipse" "Publisher" "BlackEclipse"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlackEclipse" "DisplayIcon" "$INSTDIR\Black Eclipse.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlackEclipse" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlackEclipse" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlackEclipse" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlackEclipse" "NoRepair" 1

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlackEclipse" "EstimatedSize" "$0"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; The app is a WebView2 host: without the runtime it opens a message box and
  ; exits, which reads as "the installer is broken". Check before finishing and
  ; install the Evergreen runtime when it is missing.
  Call EnsureWebView2

!ifdef FULL
  ; Offline edition: include whichever optional packs were built for this
  ; platform. The list is generated at build time (a pack such as winsdk has no
  ; Linux wheel), so NSIS does not hard-code it.
  SetOutPath "$INSTDIR\app\scripts\vendor\python\Lib\site-packages"
  !insertmacro BeginPhase "Installing the offline feature packs..."
  !include "${PACKSINCLUDE}"
  !insertmacro EndPhase "Offline feature packs installed"
!endif

  ; Start Menu
  CreateDirectory "$SMPROGRAMS\Black Eclipse"
  CreateShortCut "$SMPROGRAMS\Black Eclipse\Black Eclipse.lnk" "$INSTDIR\Black Eclipse.exe" "" "$INSTDIR\Black Eclipse.exe" 0
  CreateShortCut "$SMPROGRAMS\Black Eclipse\Uninstall Black Eclipse.lnk" "$INSTDIR\Uninstall.exe"

  ; Desktop shortcut, but never overwrite one the user already arranged.
  IfFileExists "$DESKTOP\Black Eclipse.lnk" +2 0
  CreateShortCut "$DESKTOP\Black Eclipse.lnk" "$INSTDIR\Black Eclipse.exe" "" "$INSTDIR\Black Eclipse.exe" 0
SectionEnd

; --- WebView2 -----------------------------------------------------------------
; The runtime registers its version under one of three roots (per-machine 32/64
; and per-user). Any of them means it is available to this user.
!macro WV2_READ out
  ReadRegStr ${out} HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${If} ${out} == ""
    ReadRegStr ${out} HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${EndIf}
  ${If} ${out} == ""
    ReadRegStr ${out} HKCU "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${EndIf}
!macroend

Function EnsureWebView2
  !insertmacro WV2_READ $0
  ${If} $0 != ""
    DetailPrint "WebView2 runtime already present ($0)"
    Return
  ${EndIf}

  DetailPrint "WebView2 runtime not found - installing the Evergreen runtime..."

!ifdef WEBVIEW2_BOOTSTRAPPER
  InitPluginsDir
  File "/oname=$PLUGINSDIR\MicrosoftEdgeWebview2Setup.exe" "${WEBVIEW2_BOOTSTRAPPER}"
  ExecWait '"$PLUGINSDIR\MicrosoftEdgeWebview2Setup.exe" /silent /install' $1
  DetailPrint "WebView2 bootstrapper finished with exit code $1"
!else
  DetailPrint "No WebView2 bootstrapper was bundled in this build."
!endif

  !insertmacro WV2_READ $0
  ${If} $0 != ""
    DetailPrint "WebView2 runtime is now available ($0)"
    Return
  ${EndIf}

  ; Still missing: explain precisely and let the user decide. Silently finishing
  ; would leave an app that cannot open, which is worse than a clear message.
  MessageBox MB_YESNO|MB_ICONEXCLAMATION \
    "Black Eclipse needs the Microsoft Edge WebView2 Runtime, and it could not be installed automatically.$\r$\n$\r$\nThis usually means there is no internet connection right now, or the installer was blocked by policy.$\r$\n$\r$\nInstall it manually from:$\r$\nhttps://developer.microsoft.com/microsoft-edge/webview2/$\r$\n$\r$\nBlack Eclipse is installed already. Without the runtime it will not open.$\r$\n$\r$\nContinue anyway?" \
    IDYES wv2_continue
  Abort "Installation paused: install the WebView2 Runtime, then start Black Eclipse again."
  wv2_continue:
  DetailPrint "Continuing without WebView2 - the app will ask for it on launch."
FunctionEnd

Section "Uninstall"  ; Stop a running instance so files are not locked.
  nsExec::Exec 'taskkill /IM "Black Eclipse.exe" /F'

  ; Same reason as install: the removal is file by file (~100k), so report a
  ; phase instead of streaming every deleted path through the status strip.
  !insertmacro BeginPhase "Removing application files..."

  Delete "$SMPROGRAMS\Black Eclipse\Black Eclipse.lnk"
  Delete "$SMPROGRAMS\Black Eclipse\Uninstall Black Eclipse.lnk"
  RMDir "$SMPROGRAMS\Black Eclipse"
  Delete "$DESKTOP\Black Eclipse.lnk"

  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlackEclipse"
  DeleteRegKey HKCU "Software\BlackEclipse"

  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\runtime"
  Delete "$INSTDIR\Black Eclipse.exe"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  !insertmacro EndPhase "Application files removed"

  ; Deliberately NOT deleted: "$LOCALAPPDATA\BlackEclipse\data"
  ; Reinstalling keeps the user's database, journals and settings.
SectionEnd
