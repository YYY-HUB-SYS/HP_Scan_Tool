; HP Scan Tool — Inno Setup 安装脚本
;
; ★ 形态：**per-user 安装**（不需要管理员权限）—— 办公环境很多同事没有管理员权限，
;   装到 {localappdata}\Programs\HP Scan Tool，卸载也在用户目录，无需提权。
; ★ 输入是 **onedir 目录发布**（`python build_exe.py --onedir`）⇒
;   安装后是**真实文件**，**完全不解压 %TEMP%**（onefile 便携版每次启动要往
;   %TEMP% 解 138.5 MiB，崩溃还会残留）。
;
; ★ 编码：本文件是 **UTF-8（无 BOM）**。
;   · Inno Setup **6.3 起**可自动识别「无 BOM 的 UTF-8」.iss ⇒ 中文正确；
;     6.3 以下会把无 BOM 文件按系统 ANSI/GBK 解析 ⇒ 中文乱码。
;   · 下面用**版本闸门**兜底：真有人用 <6.3 编译时，在**编译期**直接报错，
;     而不是静默产出乱码向导。
;   （KEEPER 的 windows_keeper.iss 里同一个坑与同一个闸门，本项目照抄。）
#if VER < EncodeVer(6, 3, 0)
  #error "本 .iss 为 UTF-8（无 BOM）编码，需 Inno Setup 6.3 或更高版本编译。若必须用旧版，请先给 hp_scan_tool.iss 添加 UTF-8 BOM，否则中文向导文案会乱码。"
#endif

#define MyAppName "HP Scan Tool"
#define MyAppNameCN "HP Scan Tool 惠普集成扫描工具"
#define MyAppVersion "4.0.0.0"
#define MyAppPublisher "HP Scan Tool Project"
#define MyAppExeName "HP_Scan_Tool.exe"
; ★ AppId：**两个**右花括号是 Inno 的转义写法（`{{` = 字面 `{`），
;   收尾必须是**单个** `}` ⇒ 真实键名 = {GUID}_is1。
;   （KEEPER 曾写成 `{{GUID}}`（多一个右花括号）⇒ 键名变成 {GUID}}_is1，
;     凡是手读写该键的代码全部对不上 —— 本项目没有手写卸载键的代码，
;     但仍按规范写法，避免以后有人加「检测已安装」逻辑时踩坑。）
#define MyAppId "{7C3E9A21-4D58-4F6B-9C10-2A6E5D8F1B47}"

[Setup]
AppId={{#MyAppId}
AppName={#MyAppNameCN}
AppVersion={#MyAppVersion}
AppVerName={#MyAppNameCN} {#MyAppVersion}
AppPublisher={#MyAppPublisher}

; ★ per-user 安装：默认装到当前用户目录，**不需要管理员**。
;   办公环境很多同事没有管理员权限 ⇒ 免提权安装是硬需求。
;   同时也避免「装到 Program Files 后普通权限写不了数据目录」的经典问题。
DefaultDirName={localappdata}\Programs\HP Scan Tool
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
; ★ 用到 per-user 区域（HKCU / {userappdata} / {localappdata}）⇒ 必须显式声明，
;   否则 ISCC 每次构建都刷同一条警告。
UsedUserAreasWarning=no

; ★ 升级覆盖安装时，旧实例可能还在跑 ⇒ Inno 默认弹「文件占用」对话框，
;   用户误点就会安装失败。静默强退。
CloseApplications=force

; 输出
OutputDir=..\build\release
OutputBaseFilename=HP_Scan_Tool_Setup_v{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
; ★ Inno 7 会报 "Architecture identifier x64 is deprecated" ⇒ 用官方建议的
;   x64compatible（语义：可在 x64 及兼容环境安装）。
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\resources\icons\app.ico
; ★ 控制面板「应用和功能」里的显示项（不写就没有卸载入口的图标/名称）
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppNameCN}

; 版本信息（★ VersionInfoVersion 必须写：不写时 Setup 壳的**文件版本**是空串、
;   版本元组 0.0.0.0 —— ProductVersion 正常，肉眼极容易漏）
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppNameCN}
VersionInfoCopyright=Copyright (c) 2026 HP Scan Tool Project
VersionInfoProductName={#MyAppNameCN}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}

; 界面
WizardStyle=modern
SetupLogging=yes
DisableProgramGroupPage=yes

[Languages]
; ★ 用**相对 .iss 的路径**（仓库自带 Languages\ChineseSimplified.isl）：
;   写成系统绝对路径，换机或 CI（装的是 Inno 6）必然报 "Could not read" 导致编译失败。
Name: "chinesesimplified"; MessagesFile: "Languages\ChineseSimplified.isl"

[Tasks]
; ★ 这里**故意不设任何 task**。
;   原因（实测 2026-10-22）：桌面图标若挂在 task 上（`Tasks: desktopicon`），
;   那么**覆盖升级时按本次任务选择重建图标** ⇒ 用户上一次装过（图标在）、
;   这一次没勾（默认不勾）⇒ **桌面图标被删掉** ⇒ 用户以为装坏了。
;   ⇒ 桌面快捷方式**总是创建**（行为可预期，升级不掉）。
;   （`[Tasks]` 段保留是 Inno 的格式要求；空的 [Tasks] 段不会显示任务页。）

[Files]
; ★ 源目录 = onedir 发布产物（`python build_exe.py --onedir` 的输出）
Source: "..\build\onedir\HP_Scan_Tool\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 开始菜单
Name: "{group}\{#MyAppNameCN}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppNameCN}}"; Filename: "{uninstallexe}"
; 桌面快捷方式：★ 显式指定高清图标（从 EXE 资源取图标容易糊）
;   ★ 图标在 `_internal\resources\icons\`（PyInstaller 6.x 把数据文件放在 _internal 下）
Name: "{autodesktop}\{#MyAppNameCN}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\_internal\resources\icons\app.ico"

[Run]
; 安装完成后启动（★ [Run] 是 Inno 官方的「安装后动作」位置，静默安装同样执行）
Filename: "{app}\{#MyAppExeName}"; Flags: nowait

[Code]
// ── 数据目录位置（卸载时可选清理）────────────────────────────────────────
// ★ 用户数据**默认保留**（配置 / 曝光预设 / 扫描历史 / 会话 / 缓存都是用户的）。
//   卸载时**问一句**要不要一起删 ⇒ 删了不可恢复 ⇒ 必须让用户自己决定。
// ★ 路径口径与代码一致：
//   · 配置/预设/历史/会话 ⇒ %APPDATA%\HP_Scan_Tool\      （config_io.config_dir）
//   · 扫描缓存            ⇒ %USERPROFILE%\Documents\HP_Scans\ （cache_manager
//                            用的是 os.path.expanduser("~") + 字面 "Documents"
//                            ⇒ **不是**可能被 OneDrive 重定向的「我的文档」）
//   · 调试日志            ⇒ %USERPROFILE%\Desktop\hp_scan_debug.log
procedure UserDataDirs(Del: Boolean);
var
  UserProfile: String;
begin
  UserProfile := GetEnv('USERPROFILE');

  if Del then
  begin
    DelTree(ExpandConstant('{userappdata}\HP_Scan_Tool'), True, True, True);
    // ★★ 只删**缓存子目录** `.cache` ——
    //   `Documents\HP_Scans\` 的**根目录就是产品的默认输出目录**
    //   （scan_coordinator.DEFAULT_OUT_DIR = expanduser("~")/Documents/HP_Scans）
    //   ⇒ 用户的**扫描成品**就放在那儿 ⇒ 整目录删会**毁掉用户扫出来的文件**。
    //   只删 `.cache`（中间产物），成品一律保留。
    DelTree(UserProfile + '\Documents\HP_Scans\.cache', True, True, True);
    DelTree(UserProfile + '\Desktop\hp_scan_debug.log', True, True, True);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Reply: Integer;
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    // ★★★ 卸载前**必须先停掉正在运行的程序**（实测 2026-10-22）
    //   安装器末尾的 [Run] 会启动应用 ⇒ 用户直接从控制面板卸载时它还在跑
    //   ⇒ 它占着 DLL ⇒ 卸载「成功」了但**目录删不掉**
    //   （实测残留 63 个文件 / 149 MiB，winerror=5「拒绝访问」/ 32「正在使用」）。
    //   ★ /IM 后面**不能加引号**（实测带引号杀不掉）。
    Exec('taskkill', '/F /IM {#MyAppExeName} /T', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
    Sleep(2000);

    // ★ 必须用 **SuppressibleMsgBox** 而不是 MsgBox：
    //   普通 MsgBox 在**静默模式**下不显示、直接返回 **IDYES**
    //   ⇒ 静默卸载会把用户数据删掉（不可恢复）。
    //   SuppressibleMsgBox 可以指定**默认值** ⇒ 静默模式返回 IDNO（保留数据）。
    Reply := SuppressibleMsgBox(
      '是否同时删除本程序的用户数据？' + #13#10 + #13#10 +
      '· 配置、曝光预设、扫描历史、会话记录：%APPDATA%\HP_Scan_Tool' + #13#10 +
      '· 扫描缓存（中间产物）：%USERPROFILE%\Documents\HP_Scans\.cache' + #13#10 +
      '· 调试日志：%USERPROFILE%\Desktop\hp_scan_debug.log' + #13#10 + #13#10 +
      '★ 你扫描出来的文件（%USERPROFILE%\Documents\HP_Scans 下的成品）**永远不会被删**。' + #13#10 +
      '选择「否」将保留这些数据（以后重装可继续用）。' + #13#10 +
      '★ 选择「是」后不可恢复。',
      mbConfirmation, MB_YESNO, IDNO);
    if Reply = IDYES then
      UserDataDirs(True)
    else
      UserDataDirs(False);
  end;

  // 卸载收尾：把**空的应用目录**也删掉。
  // ★ 为什么需要：卸载时若还有文件被占用（刚杀掉的实例句柄还没释放），
  //   Inno 会删掉文件但**保留目录** ⇒ 用户的「程序」里留一个空文件夹。
  //   实测：文件全删（剩 0 个）但 `{app}` 空目录还在。
  //   RemoveDir 只删**空**目录 ⇒ 非空时静默失败（不报错，不影响卸载结果）。
  if CurUninstallStep = usPostUninstall then
    RemoveDir(ExpandConstant('{app}'));
end;

// ── 升级时的兜底强退 ────────────────────────────────────────────────────
// ★ CloseApplications=force 只覆盖「Inno 自己检测到的占用」；
//   形态特殊（子进程/服务）时检测不到 ⇒ 这里再兜一层 taskkill。
//   （KEEPER 的同一个坑与同一个兜底。）
// ★ 必须是 `function ...: Boolean`（写成 procedure ⇒ ISCC 报
//   "Invalid prototype for 'InitializeSetup'"），且**必须给 Result 赋值**
//   —— 不赋值 = 返回 False = 安装直接中止。
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  // ★ /IM 后面**不能加引号**！实测（2026-10-22）：`taskkill /F /IM "HP_Scan_Tool.exe" /T`
  //   **杀不掉**运行中的实例（taskkill 的 /IM 按字面匹配图像名，带引号就匹配不上）
  //   ⇒ 覆盖安装时旧实例还占着文件 ⇒ 安装装不上、卸载也删不干净（文件被占用）。
  //   不带引号的 `/F /IM HP_Scan_Tool.exe /T` 实测 rc=0、进程立刻消失。
  Exec('taskkill', '/F /IM {#MyAppExeName} /T', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Result := True;
end;
