; AliceAI.iss - Inno Setup script for the UNSIGNED Windows x64 installer (M5).
;
; Wraps the PyInstaller one-dir (AliceAI.exe + _internal) into a per-user
; Start-menu installer. Driven by build_exe.ps1 -Inno, which passes the source
; dir / icon / output path as /D defines:
;
;   iscc /DAppSourceDir=...\backend\dist\AliceAI ^
;        /DAppIcon=...\assets\icons\AliceAI.ico ^
;        /DOutputDir=...\dist /DOutputBase=AliceAI-windows-x64-setup AliceAI.iss
;
; UNSIGNED per V (no EV cert). Per-user install (PrivilegesRequired=lowest) so no
; UAC/admin prompt - matches the "no terminal, no admin" non-technical-user contract. The app's
; own data + models live under the user profile via ALICE_AI_DATA_DIR, NOT here.

#ifndef AppSourceDir
  #error Pass /DAppSourceDir=<PyInstaller one-dir> (e.g. backend\dist\AliceAI)
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif
#ifndef OutputBase
  #define OutputBase "AliceAI-windows-x64-setup"
#endif

#define MyAppName "Alice"
#define MyAppExeName "AliceAI.exe"
#define MyAppPublisher "Alice Protocol"
#define MyAppVersion "0.1.0"
#define MyAppId "{{A11CE0A1-0000-4A11-CE00-A11CEA1AAAAA}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Per-user install - no admin/UAC (the non-technical-user path). Installs to %LOCALAPPDATA%.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\AliceAI
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; UNSIGNED: no SignTool directive. The output installer is itself unsigned.
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBase}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
#ifdef AppIcon
SetupIconFile={#AppIcon}
UninstallDisplayIcon={app}\{#MyAppExeName}
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
; Simplified Chinese (primary per the brand). The .isl ships with Inno Setup;
; guard it at COMPILE time so a minimal Inno install without the unofficial
; Chinese translation still compiles (the Languages\ entry is added only if the
; file exists for the compiling ISCC).
#if FileExists(CompilerPath + "\Languages\ChineseSimplified.isl")
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
  GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The ENTIRE PyInstaller one-dir (the .exe + _internal/ with all native libs:
; llama.cpp DLLs, the bundled odysseus/static, the vendored alice_acp source).
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; \
  Flags: nowait postinstall skipifsilent
