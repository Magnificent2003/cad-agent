# CAD 格式转换组件

这个目录负责安装和启动 DWG/DXF 格式转换程序。项目中的实体查询、修改和预览由 `backend/app/cad/` 实现；转换器不负责理解用户指令，也不是 CAD MCP 服务。

```text
cad_converter/
├── install.sh      # 我们维护的安装脚本
├── run.sh          # 我们维护的转换器入口，可修改环境和启动方式
├── tests/          # 不联网的脚本验证
└── runtime/        # 本机生成，不纳入 Git
    └── squashfs-root/
        └── usr/bin/ODAFileConverter
```

当前转换程序是 [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter) 27.1 的 Linux x86_64 发行版。这里没有 ODA 的开源源码；我们可以修改安装脚本、启动脚本和后端适配逻辑，不能把第三方二进制当作本项目可自行维护的开源实现。第三方程序的使用遵循其许可。

## 安装和使用

在项目根目录运行：

```bash
./cad_converter/install.sh
```

安装包缓存在 `.cache/downloads/ODAFileConverter.AppImage`，程序解包到本目录的 `runtime/`。下载和复用缓存时都会与脚本中固定的 SHA-256 比较；该值来自本项目已验证的安装包记录，不是 ODA 提供的独立签名。如果已存在可执行的安装结果，重复执行会直接成功，不需要缓存包和网络。

在 `.env` 中配置：

```dotenv
ODA_EXECUTABLE=./cad_converter/run.sh
ODA_USE_XVFB=false
```

入口从任意工作目录启动都能找到组件自身的程序路径。后端负责向入口传递输入/输出目录和转换参数，负责超时处理，也负责在 `ODA_USE_XVFB=true` 时调用 `xvfb-run`。不要把该开关当作 `run.sh` 自己解析的参数。

当前发行版使用 Qt xcb 插件，需要图形显示环境。WSL2 可使用 WSLg；无桌面的 Linux 服务器需安装 `xvfb xauth` 并设置 `ODA_USE_XVFB=true`。安装成功只表示程序已解包，真实转换仍需通过项目的 CAD 验证脚本确认。

复制项目到新主机后，需要再次执行安装脚本；仅安装 Python 包不能恢复转换器。删除下载缓存不影响已经安装的程序，删除 `runtime/` 后则需要重新安装。

## 修改与验证

修改启动环境时编辑 `run.sh`；调整发行版时同时核对 `install.sh` 中的版本链接和预期哈希，不要只打印哈希就认定完成验证。不要直接在 `runtime/` 中开发项目逻辑，因为它会被重新安装替换，也不会随 Git 同步。

无需网络和真实 ODA 程序的验证：

```bash
python3 -m unittest discover -s cad_converter/tests -v
```

这些检查验证路径解析、参数传递以及已有安装无需下载缓存即可复用。真实 DWG 转换验证使用项目根目录的 `scripts/verify_cad.py`。
