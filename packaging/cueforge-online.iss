#define AppName "CueForge"
#ifndef AppVersion
#define AppVersion "0.1.0-alpha1"
#endif
#ifndef DistDir
#define DistDir "..\dist\CueForge"
#endif
#ifndef OutputDir
#define OutputDir "..\release"
#endif
#ifndef DependencyInclude
#define DependencyInclude "..\build\dependencies.windows-x64.iss"
#endif

[Setup]
AppId={{9BC88C57-9EE1-4D07-9182-5373F61014AB}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=CueForge Contributors
DefaultDirName={localappdata}\Programs\CueForge
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=CueForge-{#AppVersion}-windows-x64-online-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\CueForge.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Icons]
Name: "{group}\CueForge"; Filename: "{app}\CueForge.exe"
Name: "{autodesktop}\CueForge"; Filename: "{app}\CueForge.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CueForge.exe"; Description: "{cm:LaunchProgram,CueForge}"; Flags: nowait postinstall skipifsilent unchecked

[UninstallDelete]
Type: filesandordirs; Name: "{app}\bin"

[Code]
var
  DownloadPage: TDownloadWizardPage;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  Result := True;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(
    'Download external dependencies',
    'Setup will download ffmpeg and Deno, then verify each archive before installing.',
    @OnDownloadProgress
  );
end;

function PowerShellPath: String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  if not FileExists(Result) then
    Result := 'powershell.exe';
end;

function PsSingleQuote(Value: String): String;
begin
  StringChangeEx(Value, '''', '''''', True);
  Result := '''' + Value + '''';
end;

function ExtractDependencyArchive(const Name, ArchiveName, InstallSubdir: String): Boolean;
var
  ArchivePath: String;
  Destination: String;
  Command: String;
  Params: String;
  ResultCode: Integer;
begin
  ArchivePath := ExpandConstant('{tmp}\') + ArchiveName;
  Destination := ExpandConstant('{app}\bin\') + InstallSubdir;
  WizardForm.StatusLabel.Caption := 'Installing ' + Name + '...';

  Command :=
    '$ErrorActionPreference = ''Stop''; ' +
    'if (Test-Path -LiteralPath ' + PsSingleQuote(Destination) + ') { Remove-Item -LiteralPath ' + PsSingleQuote(Destination) + ' -Recurse -Force }; ' +
    'New-Item -ItemType Directory -Path ' + PsSingleQuote(Destination) + ' -Force | Out-Null; ' +
    'Expand-Archive -LiteralPath ' + PsSingleQuote(ArchivePath) + ' -DestinationPath ' + PsSingleQuote(Destination) + ' -Force';
  Params := '-NoProfile -ExecutionPolicy Bypass -Command "' + Command + '"';

  Result := Exec(PowerShellPath, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if (not Result) or (ResultCode <> 0) then
  begin
    MsgBox('Failed to extract ' + Name + '. PowerShell exit code: ' + IntToStr(ResultCode), mbError, MB_OK);
    Result := False;
  end;
end;

#include DependencyInclude

function DownloadAndExtractDependencies: Boolean;
begin
  Result := True;
  DownloadPage.Clear;
  AddDependencyDownloads;

  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
    except
      MsgBox('Failed to download external dependencies (ffmpeg and Deno): ' + GetExceptionMessage, mbError, MB_OK);
      Result := False;
    end;
  finally
    DownloadPage.Hide;
  end;

  if not Result then
    exit;

  Result := ExtractAllDependencies;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpReady then
    Result := DownloadAndExtractDependencies;
end;
