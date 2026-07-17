; Inno Setup script for voice-polish-desktop.
; Build with:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\voice-polish-desktop.iss
;
; Expects dist\voice-polish-desktop.exe to already exist (run PyInstaller
; first — see README's rebuild command sequence).

#define MyAppName "voice-polish-desktop"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Jaydev Ahire"
#define MyAppExeName "voice-polish-desktop.exe"

[Setup]
; A fixed, unique AppId — do NOT change between releases, or Windows will
; treat upgrades as a separate, parallel install instead of an in-place
; update.
AppId={{23FE2C9C-85B2-450D-B1A2-C329EC6EB08A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=..\installer_output
OutputBaseFilename=voice-polish-desktop-setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Installing to Program Files requires admin; the per-user "start with
; Windows" registry key below is written to HKCU regardless, so it works
; the same way whichever account runs the app afterward.
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startupicon"; Description: "Start {#MyAppName} automatically when Windows starts"; GroupDescription: "Additional options:"; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Registry]
; Optional "start with Windows" — only written if the task above was
; checked at install time; uninsdeletevalue removes it on uninstall
; regardless (harmless no-op if it was never written).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The app's own config.json (hotkey, backend URL, app-auth token) lives
; here — a "proper" uninstall removes it too, per the explicit requirement
; that uninstalling doesn't leave the auth token sitting on disk.
Type: filesandordirs; Name: "{userappdata}\voice-polish-desktop"
