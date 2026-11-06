#define AppName "Offline Archiver"
#define AppVersion "3.0.0"
#define AppPublisher "Offline Archiver"
#define AppExeName "OfflineArchiver.exe"

[Setup]
AppId={{D76A49E7-6E7A-49A1-A43D-7C35E4E6A7B9}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\Offline Archiver
DefaultGroupName={#AppName}
OutputDir=..\dist
OutputBaseFilename=OfflineArchiver-Setup-x64
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#AppExeName}

[Files]
Source: "..\dist\OfflineArchiver\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Offline Archiver"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\Offline Archiver"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch Offline Archiver"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
