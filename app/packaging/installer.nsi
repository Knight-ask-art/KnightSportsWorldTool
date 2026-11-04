; 好想跑 Windows 安装包（NSIS 3.x）
;
; 本文件必须保存为 **UTF-8 with BOM**。NSIS 只有在看到 BOM 时才按 UTF-8
; 解析脚本；没有 BOM 会把「好想跑」当成 ANSI，装出来的路径全是乱码。
; 改完请确认文件头是 EF BB BF。
;
; 设计约束（不要改）：
;   1. 按用户安装，装到 %LOCALAPPDATA%\Programs\好想跑，不请求管理员权限。
;      程序把自己的 data\ 写在 exe 旁边，装到 Program Files 会写不进去。
;   2. 升级时先删掉除 data\ 以外的所有文件，再解包。data\ 里有登录状态、
;      提交回执、设备指纹，任何时候都不能碰。
;   3. 静默安装（/S）必须可用：自动更新就是靠这个跑的。
;
; 构建：packaging\build.ps1 调用，只传 VERSION / OUTFILE 两个 ASCII 宏，
; 其余路径写死成本文件里的相对路径，避免中文参数在命令行上被转码。

Unicode true

!include "MUI2.nsh"

!ifndef VERSION
  !define VERSION "0.1.8"
!endif
!ifndef SRCDIR
  !define SRCDIR "..\build_stage\好想跑"
!endif
!ifndef OUTFILE
  !define OUTFILE "haoxiangpao_v${VERSION}_win64_setup.exe"
!endif

!define APPNAME "好想跑"
!define EXENAME "好想跑.exe"
!define PUBLISHER "Haoxiangpao"
!define REGKEY "Software\好想跑"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\好想跑"

Name "${APPNAME} ${VERSION}"
BrandingText "${APPNAME} · 本地工作台"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${APPNAME}"
InstallDirRegKey HKCU "${REGKEY}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "FileDescription" "${APPNAME} 安装程序"
VIAddVersionKey "FileVersion" "${VERSION}.0"
VIAddVersionKey "ProductVersion" "${VERSION}.0"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "LegalCopyright" "${PUBLISHER}"

!define MUI_ABORTWARNING
!define MUI_ICON "assets\logo.ico"
!define MUI_UNICON "assets\logo.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${EXENAME}"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 ${APPNAME}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Section "Install" SecInstall
  SetShellVarContext current
  SetOutPath "$INSTDIR"

  ; ---- 升级：清掉旧程序文件，只留 data\ ------------------------------
  ; 逐项判断，不用 RMDir /r $INSTDIR：那会连 data\ 一起删掉。
  FindFirst $0 $1 "$INSTDIR\*.*"
hxp_inst_loop:
  StrCmp $1 "" hxp_inst_done
  StrCmp $1 "." hxp_inst_next
  StrCmp $1 ".." hxp_inst_next
  StrCmp $1 "data" hxp_inst_next
  IfFileExists "$INSTDIR\$1\*.*" 0 hxp_inst_file
  RMDir /r "$INSTDIR\$1"
  Goto hxp_inst_next
hxp_inst_file:
  Delete "$INSTDIR\$1"
hxp_inst_next:
  FindNext $0 $1
  Goto hxp_inst_loop
hxp_inst_done:
  FindClose $0

  ; ---- 程序本体 ------------------------------------------------------
  File /r /x data "${SRCDIR}\*.*"

  ; ---- 快捷方式 ------------------------------------------------------
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\${EXENAME}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\卸载 ${APPNAME}.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\${EXENAME}"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; ---- 卸载信息（控制面板 / 应用和功能）-----------------------------
  WriteRegStr HKCU "${REGKEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${REGKEY}" "Version" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\${EXENAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTKEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext current

  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"

  ; ---- 同样的规则：删程序，留 data\ ----------------------------------
  FindFirst $0 $1 "$INSTDIR\*.*"
hxp_un_loop:
  StrCmp $1 "" hxp_un_done
  StrCmp $1 "." hxp_un_next
  StrCmp $1 ".." hxp_un_next
  StrCmp $1 "data" hxp_un_next
  IfFileExists "$INSTDIR\$1\*.*" 0 hxp_un_file
  RMDir /r "$INSTDIR\$1"
  Goto hxp_un_next
hxp_un_file:
  Delete "$INSTDIR\$1"
hxp_un_next:
  FindNext $0 $1
  Goto hxp_un_loop
hxp_un_done:
  FindClose $0

  ; 静默卸载（自动更新失败后的兜底、脚本调用）一律保留 data\。
  IfSilent hxp_un_finish
  IfFileExists "$INSTDIR\data\*.*" 0 hxp_un_finish
  MessageBox MB_YESNO|MB_DEFBUTTON2 \
    "是否一并删除 data 文件夹？$\r$\n$\r$\n里面有登录状态、提交回执和设备指纹。选择「否」会保留它，重装后可以继续用。" \
    IDNO hxp_un_finish
  RMDir /r "$INSTDIR\data"
hxp_un_finish:
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${UNINSTKEY}"
  DeleteRegKey HKCU "${REGKEY}"
SectionEnd

Function un.onInit
  ; 卸载器必须用真实安装目录。InstallDirRegKey 通常已经处理，这里显式读一次，
  ; 避免用户改过目录后卸载器仍在默认路径上操作（那会留下整个程序目录）。
  ReadRegStr $0 HKCU "${REGKEY}" "InstallDir"
  StrCmp $0 "" hxp_un_init_done
  StrCpy $INSTDIR $0
hxp_un_init_done:
FunctionEnd

Function .onInit
  ; 已经装过就沿用原来的目录（InstallDirRegKey 已处理），这里只做提示。
  ReadRegStr $0 HKCU "${UNINSTKEY}" "DisplayVersion"
  StrCmp $0 "" hxp_init_done
  IfSilent hxp_init_done
  MessageBox MB_OK|MB_ICONINFORMATION \
    "检测到已安装 v$0，本次将原地升级。$\r$\n$\r$\ndata 文件夹（登录状态、提交回执）会保留。" \
    /SD IDOK
hxp_init_done:
FunctionEnd
