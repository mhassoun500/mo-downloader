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
OutputBaseFilename=MO-Downloader-{#MyAppVersion}-Setup
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
#ifdef WebView2Setup
; Microsoft's WebView2 runtime draws the app window; installed only if this PC doesn't have it yet
Source: "{#WebView2Setup}"; DestDir: "{tmp}"; DestName: "MicrosoftEdgeWebview2Setup.exe"; Flags: deleteafterinstall; Check: NeedsWebView2
#endif

[InstallDelete]
; old engine files from a previous version
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\MO Downloader"; Filename: "{app}\MO Downloader.exe"
Name: "{autodesktop}\MO Downloader"; Filename: "{app}\MO Downloader.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Classes\mo-downloader"; ValueType: string; ValueName: ""; ValueData: "URL:MO Downloader"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\mo-downloader"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\mo-downloader\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\MO Downloader.exe"",0"
Root: HKCU; Subkey: "Software\Classes\mo-downloader\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\MO Downloader.exe"" ""%1"""

[Run]
#ifdef WebView2Setup
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing Microsoft WebView2…"; Flags: waituntilterminated; Check: NeedsWebView2
#endif
Filename: "{app}\MO Downloader.exe"; Description: "{cm:LaunchProgram,MO Downloader}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM ""MO Downloader.exe"""; Flags: runhidden; RunOnceId: "StopApp"

[UninstallDelete]
Type: files; Name: "{userstartup}\MO Downloader.lnk"

[Code]
function HasWebView2(Root: Integer; Key: String): Boolean;
var v: String;
begin
  Result := RegQueryStringValue(Root, Key, 'pv', v) and (v <> '') and (v <> '0.0.0.0');
end;

function NeedsWebView2: Boolean;
var k: String;
begin
  k := 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := not (HasWebView2(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}')
                 or HasWebView2(HKLM, k) or HasWebView2(HKCU, k));
end;
