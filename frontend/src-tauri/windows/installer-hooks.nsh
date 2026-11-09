; Tauri NSIS installer hooks (bundle.windows.nsis.installerHooks in tauri.conf.json).
; The default installer only offers a desktop shortcut as a checkbox on its last page, which is
; easy to miss, so always create one. The uninstaller already removes it.
!macro NSIS_HOOK_POSTINSTALL
  CreateShortcut "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
!macroend
