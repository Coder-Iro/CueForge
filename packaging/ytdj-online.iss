#define AppName "YT-DJ"
#ifndef AppVersion
#define AppVersion "0.1.0-alpha1"
#endif
#ifndef DistDir
#define DistDir "..\dist\YT-DJ"
#endif
#ifndef OutputDir
#define OutputDir "..\release"
#endif

[Setup]
AppId={{9BC88C57-9EE1-4D07-9182-5373F61014AB}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=YT-DJ Contributors
DefaultDirName={localappdata}\Programs\YT-DJ
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=YT-DJ-{#AppVersion}-windows-x64-online-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\YT-DJ.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Icons]
Name: "{group}\YT-DJ"; Filename: "{app}\YT-DJ.exe"
Name: "{autodesktop}\YT-DJ"; Filename: "{app}\YT-DJ.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\YT-DJ.exe"; Description: "{cm:LaunchProgram,YT-DJ}"; Flags: nowait postinstall skipifsilent unchecked

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
    'Setup will download ffmpeg, fpcalc, and Deno, then verify each archive before installing.',
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

function DownloadAndExtractDependencies: Boolean;
begin
  Result := True;
  DownloadPage.Clear;
  DownloadPage.Add(
    'https://github.com/denoland/deno/releases/download/v2.8.2/deno-x86_64-pc-windows-msvc.zip',
    'deno-x86_64-pc-windows-msvc-2.8.2.zip',
    '6fe073b11cabeba2f2726d8a3d1592b198aec5f23dab3473d0dc8d5ec7aee1c9'
  );
  DownloadPage.Add(
    'https://github.com/acoustid/chromaprint/releases/download/v1.6.0/chromaprint-fpcalc-1.6.0-windows-x86_64.zip',
    'chromaprint-fpcalc-1.6.0-windows-x86_64.zip',
    '30179d3d0dc4cc92f1a0995c1a2e523fb4867724c2ee6a6ceae474f8e4d6937a'
  );
  DownloadPage.Add(
    'https://github.com/GyanD/codexffmpeg/releases/download/8.1.1/ffmpeg-8.1.1-full_build-shared.zip',
    'ffmpeg-8.1.1-full_build-shared.zip',
    '4296b396bdfd5fbc3dfc75ab4c8703354a56963232d65c4182993543df2d2f45'
  );

  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
    except
      MsgBox('Failed to download external dependencies: ' + GetExceptionMessage, mbError, MB_OK);
      Result := False;
    end;
  finally
    DownloadPage.Hide;
  end;

  if not Result then
    exit;

  Result :=
    ExtractDependencyArchive('Deno', 'deno-x86_64-pc-windows-msvc-2.8.2.zip', 'deno') and
    ExtractDependencyArchive('Chromaprint fpcalc', 'chromaprint-fpcalc-1.6.0-windows-x86_64.zip', 'chromaprint') and
    ExtractDependencyArchive('FFmpeg', 'ffmpeg-8.1.1-full_build-shared.zip', 'ffmpeg');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpReady then
    Result := DownloadAndExtractDependencies;
end;
