; VenTapes personal learning build (not a public release)
; Build with: iscc installer.iss

#define MyAppName "VenTapes"
#define MyAppVersion "0.0.0"
#define MyAppPublisher "realvenerable"
#define MyAppURL "https://github.com/realvenerable/VenTapes"
#define MyAppExeName "VenTapes.exe"
#define MyAppId "io.github.realvenerable.VenTapes"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputBaseFilename=VenTapesSetup
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
SetupIconFile=ventapes.ico
UninstallDisplayIcon={app}\VenTapes.exe
WizardStyle=modern
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Main executable and helpers
Source: "{#SourcePath}\VenTapes.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourcePath}\windows\VenTapesBridge.exe"; DestDir: "{app}\windows"; Flags: ignoreversion
Source: "{#SourcePath}\windows\VenTapesLogin.exe"; DestDir: "{app}\windows"; Flags: ignoreversion
Source: "{#SourcePath}\windows\ventapes.ico"; DestDir: "{app}\windows"; Flags: ignoreversion
Source: "{#SourcePath}\windows\fonts.conf"; DestDir: "{app}\windows"; Flags: ignoreversion
Source: "{#SourcePath}\windows\rustypipe-botguard.exe"; DestDir: "{app}\windows"; Flags: ignoreversion

; Debug launcher
Source: "{#SourcePath}\ventapes-debug.bat"; DestDir: "{app}"; Flags: ignoreversion

; App source
Source: "{#SourcePath}\src\*"; DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs

; Assets
Source: "{#SourcePath}\assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs

; Fonts
Source: "{#SourcePath}\fonts\*"; DestDir: "{app}\fonts"; Flags: ignoreversion skipifsourcedoesntexist

; MSYS2 Runtime
Source: "{#SourcePath}\runtime\bin\*"; DestDir: "{app}\runtime\bin"; Flags: ignoreversion
Source: "{#SourcePath}\runtime\lib\*"; DestDir: "{app}\runtime\lib"; Flags: ignoreversion recursesubdirs
Source: "{#SourcePath}\runtime\share\*"; DestDir: "{app}\runtime\share"; Flags: ignoreversion recursesubdirs
Source: "{#SourcePath}\runtime\ssl\*"; DestDir: "{app}\runtime\ssl"; Flags: ignoreversion recursesubdirs

; GPL and provenance
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\NOTICE.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CREDITS.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\vendor\rustypipe-botguard\LICENSE"; DestDir: "{app}\licenses"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\windows\ventapes.ico"; AppUserModelID: "{#MyAppId}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\windows\ventapes.ico"; AppUserModelID: "{#MyAppId}"; Tasks: desktopicon
Name: "{group}\{#MyAppName} (Debug)"; Filename: "{app}\ventapes-debug.bat"; IconFilename: "{app}\windows\ventapes.ico"
Name: "{group}\Login Helper"; Filename: "{app}\windows\VenTapesLogin.exe"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Registry]
; Register AppUserModelID for proper taskbar/SMTC identification
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\{#MyAppId}"; ValueType: string; ValueName: "DisplayName"; ValueData: "{#MyAppName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\{#MyAppId}"; ValueType: string; ValueName: "IconUri"; ValueData: "{app}\windows\ventapes.ico"; Flags: uninsdeletekey

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\ventapes"
