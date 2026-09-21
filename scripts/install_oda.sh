#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
mkdir -p .cache/downloads .tools/oda
if ! test -x .tools/oda/squashfs-root/usr/bin/ODAFileConverter; then
  curl -fL --retry 3 'https://www.opendesign.com/guestfiles/get?filename=ODAFileConverter_QT6_lnxX64_8.3dll_27.1.AppImage' -o .cache/downloads/ODAFileConverter.AppImage
  chmod +x .cache/downloads/ODAFileConverter.AppImage
  (cd .tools/oda && ../../.cache/downloads/ODAFileConverter.AppImage --appimage-extract >/dev/null)
fi
cat > .tools/oda/ODAFileConverter <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
oda_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/squashfs-root" && pwd)"
export LD_LIBRARY_PATH="$oda_root/usr/lib:$oda_root/usr/bin${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export QT_PLUGIN_PATH="$oda_root/usr/plugins"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
exec "$oda_root/usr/bin/ODAFileConverter" "$@"
WRAPPER
chmod +x .tools/oda/ODAFileConverter
sha256sum .cache/downloads/ODAFileConverter.AppImage
if test -z "${DISPLAY:-}" && ! command -v xvfb-run >/dev/null; then
  echo 'ODA 已安装。此系统无显示服务，请安装 xvfb xauth 并设置 ODA_USE_XVFB=true。'
else
  echo 'ODA 已安装到 .tools/oda；运行 scripts/verify_cad.py 验证实际图纸。'
fi
