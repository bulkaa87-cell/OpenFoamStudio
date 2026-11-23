; Inno Setup script of the Windows installer of OpenFOAM Studio.
;
;   ISCC.exe /DAppVersion=0.2.7 /DSrcDir=<dist>\OpenFOAM Studio /DOutDir=<out> ^
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
  #define AppVersion "0.2.7"
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
en.FoamTask=Prepare OpenFOAM in WSL (also WSL and Ubuntu if they are missing; about 1-2 GB)
uk.FoamTask=Підготувати OpenFOAM у WSL (і WSL з Ubuntu, якщо їх немає; близько 1-2 ГБ)
en.FoamGroup=Calculations on this computer:
uk.FoamGroup=Розрахунки на цьому компʼютері:
en.VCTitle=Microsoft Visual C++ Runtime
uk.VCTitle=Середовище Microsoft Visual C++
en.VCDesc=The program needs it. The installer downloads it from Microsoft (about 25 MB) and installs it (Windows asks for administrator rights).
uk.VCDesc=Програмі воно потрібне. Інсталятор завантажить його з сайту Microsoft (близько 25 МБ) і встановить (Windows попросить права адміністратора).
en.VCFailed=The Microsoft Visual C++ Runtime was not installed (%1). The program may not start. Install it by hand:%n%n%2
uk.VCFailed=Середовище Microsoft Visual C++ не встановлено (%1). Програма може не запуститися. Встановіть його вручну:%n%n%2
en.AskResults=Also delete the results of the simulations?%n%nThe folders cases, projects, session and meshes in:%n%1%nand ~/openfoam-studio in WSL.%n%nThe settings stay. Servers are not changed.
uk.AskResults=Видалити також результати симуляцій?%n%nТеки cases, projects, session і meshes у:%n%1%nі ~/openfoam-studio у WSL.%n%nНалаштування лишаються. Сервери не змінюються.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "serverwizard"; Description: "{cm:ServerTask}"; GroupDescription: "{cm:ServerGroup}"; Flags: unchecked
Name: "prepareopenfoam"; Description: "{cm:FoamTask}"; GroupDescription: "{cm:FoamGroup}"

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
; With the task «Prepare OpenFOAM» the program starts at the end of the
; install in any case (it opens the window of fs_setupfoam). Then the box
; «Launch» is not shown: one window, not two. runasoriginaluser: WSL
; belongs to the user, not to the administrator of an install for all users.
Filename: "{app}\{#AppExe}"; Parameters: "{code:LaunchArgs}"; Tasks: prepareopenfoam; Flags: nowait skipifsilent runasoriginaluser
Filename: "{app}\{#AppExe}"; Parameters: "{code:LaunchArgs}"; Description: "{cm:LaunchProgram,{#AppName}}"; Tasks: not prepareopenfoam; Flags: nowait postinstall skipifsilent

; The update from the program (update.py of the program): /RELAUNCH=1
; starts the program again after a silent install. runasoriginaluser: not
; as the administrator of an install for all users.
Filename: "{app}\{#AppExe}"; Check: RelaunchWanted; Flags: nowait runasoriginaluser

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

{ ---- An installed version is removed before the new one goes in. ----
  Without this, files that the new version does not have stay in the
  program folder, and an install "for all users" and one "for this user"
  stay side by side. The old uninstaller runs silent: then it does not
  remove the backend and does not ask about the results (see below). }
const
  UninstKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{3B7E4C1A-9D52-4E8B-A6F3-0C2D7E91B845}_is1';

procedure RemoveOld(RootKey: Integer);
var
  S: String;
  Code, Tries: Integer;
begin
  if not RegQueryStringValue(RootKey, UninstKey, 'UninstallString', S) then
    Exit;
  S := RemoveQuotes(S);
  if not FileExists(S) then
    Exit;
  Log('Removing the installed version: ' + S);
  if not ShellExec('', S, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '',
                   SW_HIDE, ewWaitUntilTerminated, Code) then
    Log('The uninstaller did not start: ' + SysErrorMessage(Code));
  Tries := 0;
  while FileExists(S) and (Tries < 75) do
  begin
    Sleep(200);
    Tries := Tries + 1;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    RemoveOld(HKCU);
    if IsWin64 then
    begin
      RemoveOld(HKLM64);
      RemoveOld(HKLM32);
    end
    else
      RemoveOld(HKLM);
  end;
end;

{ ---- Uninstall: the backend and, on request, the results. ----
  The program removes only what it installed itself (fs_setupfoam,
  installed-backend.json). A silent uninstall (also the one of an update)
  removes nothing of this and asks nothing. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Params, Docs: String;
  Code: Integer;
begin
  if (CurUninstallStep <> usUninstall) or UninstallSilent then
    Exit;
  Docs := ExpandConstant('{userdocs}\{#AppName}');
  Params := '--remove-backend';
  if SuppressibleMsgBox(FmtMessage(CustomMessage('AskResults'), [Docs]),
                        mbConfirmation, MB_YESNO or MB_DEFBUTTON2,
                        IDNO) = IDYES then
    Params := Params + ' --remove-results';
  if not Exec(ExpandConstant('{app}\{#AppExe}'), Params, '', SW_SHOW,
              ewWaitUntilTerminated, Code) then
    Log('The backend was not removed: ' + SysErrorMessage(Code));
end;

function LaunchArgs(Param: String): String;
begin
  Result := '';
  if WizardIsTaskSelected('serverwizard') then
    Result := '--server-wizard';
  { The program prepares OpenFOAM (fs_setupfoam) when it starts with this
    flag. }
  if WizardIsTaskSelected('prepareopenfoam') then
    Result := Trim(Result + ' --prepare-openfoam');
end;

{ ---- The update from the program (update.py of the program). ----
  The program starts this installer with /SILENT /WAITPID=<its process>
  /RELAUNCH=1 and then closes. The installer waits until that process has
  ended (at most 5 min): only then can the old version go. After the
  install, [Run] starts the program again. }
function OpenProcess(Access: Cardinal; Inherit: Boolean; Pid: Cardinal): Cardinal;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(Handle: Cardinal; Ms: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: Cardinal): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function InitializeSetup(): Boolean;
var
  Pid, Handle: Cardinal;
begin
  Result := True;
  Pid := StrToIntDef(ExpandConstant('{param:WAITPID|0}'), 0);
  if Pid = 0 then
    Exit;
  Handle := OpenProcess($00100000 { SYNCHRONIZE }, False, Pid);
  if Handle = 0 then
    Exit;
  Log('Waiting for the program to close (process ' + IntToStr(Pid) + ')');
  WaitForSingleObject(Handle, 300000);
  CloseHandle(Handle);
end;

function RelaunchWanted(): Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

{ ---- Dependency: the Microsoft Visual C++ Runtime (2015-2022, x64). ----
  Python, VTK and numpy of the program link to the system copy of
  vcruntime140.dll / msvcp140.dll (only PySide6 has its own). A fresh
  Windows can be without it, or have a copy older than 14.40 (the newer
  modules then crash). If it is missing or old, the installer downloads
  vc_redist.x64.exe from Microsoft with a progress bar and installs it. }
const
  VCUrl = 'https://aka.ms/vs/17/release/vc_redist.x64.exe';
  VCKey = 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';

var
  DownloadPage: TDownloadWizardPage;

function VCRuntimeNeeded(): Boolean;
var
  Installed, Major, Minor: Cardinal;
begin
  Result := True;
  if not RegQueryDWordValue(HKLM64, VCKey, 'Installed', Installed) then
    Exit;
  if (Installed <> 1)
     or not RegQueryDWordValue(HKLM64, VCKey, 'Major', Major)
     or not RegQueryDWordValue(HKLM64, VCKey, 'Minor', Minor) then
    Exit;
  Result := (Major < 14) or ((Major = 14) and (Minor < 40));
  Log(Format('Visual C++ Runtime %d.%d, needed: %d', [Major, Minor, Ord(Result)]));
end;

function InstallVCRuntime(const Exe: String): String;
var
  Code: Integer;
begin
  Result := '';
  { 'runas': the runtime goes into System32, also for an install of this
    user only. /passive: its own progress bar, no questions. 3010 = done,
    restart later. 1638 = a newer version is there. }
  if not ShellExec('runas', Exe, '/install /passive /norestart', '',
                   SW_SHOW, ewWaitUntilTerminated, Code) then
    Result := SysErrorMessage(Code)
  else if (Code <> 0) and (Code <> 3010) and (Code <> 1638) then
    Result := 'code ' + IntToStr(Code);
end;

procedure InitializeWizard();
begin
  DownloadPage := CreateDownloadPage(CustomMessage('VCTitle'),
                                     CustomMessage('VCDesc'), nil);
end;

procedure VCFailed(const Why: String);
begin
  Log('Visual C++ Runtime: ' + Why);
  SuppressibleMsgBox(FmtMessage(CustomMessage('VCFailed'), [Why, VCUrl]),
                     mbError, MB_OK, IDOK);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Why: String;
begin
  Result := True;
  if (CurPageID <> wpReady) or WizardSilent() or not VCRuntimeNeeded() then
    Exit;
  DownloadPage.Clear;
  DownloadPage.Add(VCUrl, 'vc_redist.x64.exe', '');
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
      Why := InstallVCRuntime(ExpandConstant('{tmp}\vc_redist.x64.exe'));
      if Why <> '' then
        VCFailed(Why);
    except
      if not DownloadPage.AbortedByUser then
        VCFailed(GetExceptionMessage);
    end;
  finally
    DownloadPage.Hide;
  end;
end;

{ A silent install (the update from the program) shows no pages: the same
  check, the download without the page. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Why: String;
begin
  Result := '';
  if not WizardSilent() or not VCRuntimeNeeded() then
    Exit;
  try
    DownloadTemporaryFile(VCUrl, 'vc_redist.x64.exe', '', nil);
    Why := InstallVCRuntime(ExpandConstant('{tmp}\vc_redist.x64.exe'));
    if Why <> '' then
      Log('Visual C++ Runtime: ' + Why);
  except
    Log('Visual C++ Runtime: ' + GetExceptionMessage);
  end;
end;
