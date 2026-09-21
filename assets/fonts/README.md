# 看图字体

`noto/` 随项目分发两款开源简体中文字体：Noto Sans CJK SC Regular（黑体风格）和 Noto Serif CJK SC Regular（宋体风格）。同时覆盖常用中文、拉丁字母、数字和工程符号。默认回退使用前者；原图可用且包含所需字符的字体优先。

文件提取自 Ubuntu `fonts-noto-cjk` 的 Regular TTC 中的 SC 字体面，未改变字形。上游：[Noto CJK](https://github.com/notofonts/noto-cjk)。版权与 SIL Open Font License 1.1 见 [noto/OFL.txt](noto/OFL.txt)。不包含从 Windows 或商业 CAD 软件复制的字体。

你拥有使用权的原图字体（TTF / OTF / TTC / SHX）可以放到 `assets/fonts/local/`；此目录自动扫描且不提交 Git。也可在 `.env` 的 `CAD_FONT_DIRS` 中填写其他目录（Linux 下用冒号分隔，相对路径从项目根目录解析）。更换字体后重启后端，再重新打开图纸，预览会按新的字体缓存标识重新生成。

回退字体只能改善可读性，不能保证与原 SHX / Bigfont 的字宽、换行和专业符号完全一致。天正对象能否显示还取决于图纸是否保存了代理图形。
