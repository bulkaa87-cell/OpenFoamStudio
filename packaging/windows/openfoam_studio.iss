; Inno Setup script of the Windows installer of OpenFOAM Studio.
;
;   ISCC.exe /DAppVersion=0.2.1 /DSrcDir=<dist>\OpenFOAM Studio /DOutDir=<out> ^
;            packaging\windows\openfoam_studio.iss
;
; Input: the PyInstaller folder (packaging\pyinstaller\openfoam_studio.spec).
; Output: <out>\OpenFOAM-Studio-<version>-windows-x64-setup.exe.
; packaging\windows\build.ps1 does all steps.
;
; - For this user only, without administrator rights (the default); the
;   person can select "all users" in the first dialog.
; - Start menu entry, desktop icon (a task), uninstaller.
; - The uninstaller removes only the program folder. The settings
;   (%APPDATA%\OpenFOAM Studio) and the data (Documents\OpenFOAM Studio:
;   cases, projects, session, meshes) stay.
; - The task "serverwizard" starts the program with --server-wizard at the
;   end (Tailscale + SSH server setup).
; - The icon: the logo of T33 (icons\openfoam-studio.ico, the PNG sizes for
;   the small wizard image). The shortcuts carry the Windows app id of the
;   program (fs_icons.OFS_APP_ID): one taskbar button for the window and the
;   pinned shortcut.
; - A shortcut "OpenFOAM Studio" that is already on the desktop or in the
;   Start menu and opens ANOTHER program (for example the source copy) stays
;   as it is. The installer then makes its shortcut with the name
;   "OpenFOAM Studio (installed)", and the uninstaller removes only that one.

#ifndef AppVersion
  #define AppVersion "0.2.1"
#endif
#ifndef SrcDir
  #define SrcDir "..\..\build\dist\OpenFOAM Studio"
#endif
#ifndef OutDir
  #define OutDir "..\..\build\installer"
#endif
#define AppName "OpenFOAM Studio"
#define AppExe "OpenFOAM Studio.exe"
#define IconFile "..\..\icons\openfoam-studio.ico"
#define SmallImage "..\..\icons\openfoam-studio-64.png"
#define AppUserModelId "OpenFOAMStudio.OpenFOAMStudio"

[Setup]
AppId={{3B7E4C1A-9D52-4E8B-A6F3-0C2D7E91B845}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=OpenFOAM Studio contributors
AppPublisherURL=https://github.com/bulkaa87-cell/OpenFoamStudio
AppSupportURL=https://github.com/bulkaa87-cell/OpenFoamStudio/issues
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutDir}
OutputBaseFilename=OpenFOAM-Studio-{#AppVersion}-windows-x64-setup
#ifexist IconFile
SetupIconFile={#IconFile}
#endif
#ifexist SmallImage
WizardSmallImageFile={#SmallImage}
#endif
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "uk"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[CustomMessages]
en.ServerTask=Set up a calculation server after the install (Tailscale, SSH)
uk.ServerTask=Налаштувати сервер для розрахунків після встановлення (Tailscale, SSH)
en.ServerGroup=Server:
uk.ServerGroup=Сервер:
en.InstalledCopy=installed
uk.InstalledCopy=встановлена

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "serverwizard"; Description: "{cm:ServerTask}"; GroupDescription: "{cm:ServerGroup}"; Flags: unchecked

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Each shortcut has two names: the plain name, or (when a shortcut of
; another program has the plain name) the name with "(installed)".
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelId}"; Check: PlainName('{autoprograms}')
Name: "{autoprograms}\{#AppName} ({cm:InstalledCopy})"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelId}"; Check: not PlainName('{autoprograms}')
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelId}"; Tasks: desktopicon; Check: PlainName('{autodesktop}')
Name: "{autodesktop}\{#AppName} ({cm:InstalledCopy})"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelId}"; Tasks: desktopicon; Check: not PlainName('{autodesktop}')

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "{code:LaunchArgs}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
{ The target of a .lnk file ('' when it cannot be read). }
function ShortcutTarget(const Lnk: String): String;
var
  Shell, Link: Variant;
begin
  Result := '';
  try
    Shell := CreateOleObject('WScript.Shell');
    Link := Shell.CreateShortcut(Lnk);
    Result := Link.TargetPath;
  except
    Result := '';
  end;
end;

{ True: the plain shortcut name in Folder is free, or it already opens this
  program. False: it belongs to another program (keep it). }
function PlainName(Folder: String): Boolean;
var
  Lnk, Target: String;
begin
  Lnk := ExpandConstant(Folder) + '\{#AppName}.lnk';
  Result := True;
  if FileExists(Lnk) then
  begin
    Target := ShortcutTarget(Lnk);
    Result := CompareText(Target, ExpandConstant('{app}\{#AppExe}')) = 0;
    if not Result then
      Log('Keep the shortcut of another program: ' + Lnk + ' -> ' + Target);
  end;
end;

function LaunchArgs(Param: String): String;
begin
  if WizardIsTaskSelected('serverwizard') then
    Result := '--server-wizard'
  else
    Result := '';
end;
