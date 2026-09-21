# SVG、字体与天正兼容修复

核验日期：2026-09-21。本次验证在本机进行，没有向模型发送真实图纸。

## 修复内容

- 所有图纸默认 SVG，包含旧元数据曾建议 PNG 的大图；保留手动 PNG 和 SVG 失败回退。
- 随代码分发 Noto Sans CJK SC Regular、Noto Serif CJK SC Regular 和 OFL 许可证。实际检查字符覆盖，避免“文件存在但缺少字形”仍显示方框。
- 原默认 Droid Sans Fallback 在本机的字体文件中缺少拉丁字母、数字及部分符号；改用同时覆盖中文和这些字符的回退字体。
- 使用 ezdxf 的 MIF / DXF Unicode 解码器处理旧式文字，查询同时返回可读 `text` 与原始 `raw_text`。渲染覆盖 TEXT、MTEXT 内联字体、块属性和可读取的代理文字。
- 自定义字体目录实际注册到 ezdxf 字体管理器；不再只设置搜索目录。
- 预览缓存包含渲染器版本、回退字体、DPI 和项目/自定义字体文件信息。旧版本预览按需重新生成，不新增图纸版本，不改写原 DWG 或版本 DXF；浏览器重新校验缓存。

## 真实总平面图

对象为用户已经上传的 `白桥一号园（二期）总图20260414_t9 - 副本 - 副本.dwg` 对应 r0001。

| 核对项 | 结果 |
| --- | --- |
| 旧编码解码 | 530 处文字 |
| 本次渲染缺失字符报告 | 0 个；不代表无法展开的代理对象已全面识别 |
| 完整 SVG | 24,759,522 字节，约 23.6 MiB |
| 完整 PNG | 457,497 字节 |
| 独立渲染耗时 | 约 22.4 秒，本机单次观察 |
| 源文件保护 | 渲染前后版本 DXF SHA-256 一致；实际 API 刷新另核对原 DWG 与 DXF |
| 局部人工检查 | 中文“塘”“土”“路”及高程数字已显示为字形 |

本地证据保存在 `data/validation/font-fix-total/`、`data/validation/font-fix-crop/`，不提交真实图纸预览。

图纸类表包含 23 个 `TCH_` 类，包括 `TCH_TEXT`、`TCH_MTEXT`、`TCH_DIMENSION2`。全 entity database 的依赖检查列出 290 个代理或特殊对象，其中 287 个带代理图形；这个口径包括块中的对象，不等于之前报告的 30 个顶层代理实体。它能说明图纸包含天正数据，不能证明具体为天正 9。

代理图形存在也不保证每种绘图指令都受支持。系统没有安装天正对象解释器，无法恢复未保存的代理图形或完整编辑专有对象；中心区域的遮罩、外参和专业对象显示仍需结合原图检查。字体替换可能改变字宽与换行。需要更完整的对象显示时，请在天正环境中导出兼容普通 CAD 的副本进行对照。

## 自动化与部署

- Python：43 项通过，包括字符覆盖、自定义字体注册、MIF / Unicode 查询、MTEXT / 块文字、原件哈希保护、旧预览重建及字体配置变更后缓存更新。
- 浏览器：5 项通过，新增旧 PNG 建议下仍默认 SVG 的回归检查；既有历史版本、上传、布局、PNG 回退和消息流程通过。
- TypeScript / Vite 构建及 Prettier 检查通过。
- 真实样图目录迁入 `samples/private/白桥室外施工图纸/`，20 个文件哈希一致；该目录不提交 Git。

原始依据：[ezdxf 字体配置](https://ezdxf.readthedocs.io/en/stable/tools/fonts.html)、[代理实体与虚拟图形](https://ezdxf.readthedocs.io/en/stable/dxfentities/acad_proxy_entity.html)、[Noto CJK 上游](https://github.com/notofonts/noto-cjk)。
