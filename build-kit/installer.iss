; Inno Setup script for MO Downloader (compiled by build.py)
#ifndef MyAppVersion
  #define MyAppVersion "2.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build-tmp\dist\MO Downloader"
#endif
#ifndef IconFile
  #define IconFile "..\app\mo_downloader.ico"
#endif

[Setup]
AppId={{8F3C2A51-6D1E-4C7B-9A20-4B1D7E5F0A11}
AppName=MO Downloader
AppVersion={#MyAppVersion}
AppVerName=MO Downloader {#MyAppVersion}
AppPublisher=MO
DefaultDirName={localappdata}\Programs\MO Downloader
DefaultGroupName=MO Downloader
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=MO-Downloader-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\MO Downloader.exe
UninstallDisplayName=MO Downloader
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; old engine files from a previous version
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\MO Downloader"; Filename: "{app}\MO Downloader.exe"
Name: "{autodesktop}\MO Downloader"; Filename: "{app}\MO Downloader.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MO Downloader.exe"; Description: "{cm:LaunchProgram,MO Downloader}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM ""MO Downloader.exe"""; Flags: runhidden; RunOnceId: "StopApp"

[UninstallDelete]
Type: files; Name: "{userstartup}\MO Downloader.lnk"
