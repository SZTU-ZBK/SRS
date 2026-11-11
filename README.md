# SRS 视频链路管理

Windows 电脑端程序：Python / PySide6 管理界面 + 独立 SRS RTMP 服务进程 + FFmpeg 发布与解码。板端负责检测及自身重连。

## 在这台电脑运行

Conda 环境名称为 `srs-service`，在本机位于 `C:\Users\30683\miniconda3\envs\srs-service`。SRS 位于项目的 `tools/srs`。在项目目录执行：

```powershell
conda activate srs-service
python main.py
```

也可以执行 `start.ps1`。此脚本查找 Conda，并通过 `conda run --name srs-service` 启动，不依赖终端是否提前激活环境。

在另一台 Windows x64 电脑部署时：

```powershell
conda env create --file environment.yml
conda activate srs-service
python scripts/install_srs.py
python main.py
```

`environment.yml` 固定 Python 3.12、PySide6 6.8.3，FFmpeg 使用 conda-forge 的 7/8 系列。PySide6 固定为经过本机验证的版本，避免新版 Qt 的 DLL 兼容问题。FFmpeg 需包含 `libx264` 编码器。

SRS 固定使用官方 Windows 原生发行包 **v8.0-d3**（8.0 开发系列）；版本、下载地址、SHA256 记录在 `tools/srs/release.json`，发行资产有官方摘要时会验证。也可在界面指定其他兼容的 `srs.exe`。不需要 Docker 或 WSL。参考：[SRS Windows 文档](https://www.ossrs.net/lts/en-us/docs/v8/doc/windows)、[固定版本发行页](https://github.com/ossrs/srs/releases/tag/v8.0-d3)。

## 操作顺序

1. 选择电脑供板端访问的局域网 IPv4 地址，端口默认 `1935`。程序始终绑定 `0.0.0.0`；此处填写的 IP 用于生成板端地址。多网卡机器请核对 IP。
2. 点击“启动服务”，等待服务状态显示运行中。两个预览会开始等待对应的发布者。
3. 选择视频，设置推流分辨率、帧率和转码码率，点击“开始推流”。
4. 原始窗口显示画面后，再手动启动板端程序。
5. 板端发布 `/live/alertgateway` 后，检测结果窗口自动显示视频。检测框应由板端画入视频。
6. “停止推流”仅停止电脑输入发布，服务保持运行。再次“开始推流”从视频文件开头恢复，两路预览自动重连。
7. “停止服务”停止本程序的发布、预览和服务。关闭窗口也会异步清理这些进程，清理完成后退出。

程序不会修改板端文件、启动板端程序或读取板端日志。板端“解码器打开成功”需要在板端终端核实。板端无法连接时，检查网络连通性以及 Windows 防火墙是否允许入站 TCP 1935；程序不自动修改防火墙。

## 分辨率、帧率和发布模式

- 分辨率默认“原始分辨率”。预置包含 `640x360`、`1280x720`、`1920x1080`、`2560x1440`、`3840x2160`；也能输入自定义 `宽x高`。宽高应为 16～8192 的偶数。
- 帧率默认 **30 fps**，可配置 1～120 fps，支持 `29.97` 等小数；勾选“保持源帧率”可取消帧率转换。
- 指定尺寸时保持画面比例并补黑边，输出准确匹配设置的宽高。
- 兼容的 H.264 / yuv420p 视频，在尺寸及帧率满足设置、无旋转元数据时直接转发。否则使用 FFmpeg / libx264 转为 H.264 / yuv420p。
- 转码码率默认 18 Mbps，可以调整；直接转发保持源视频码率。这里是编码目标码率，不保证每秒数据量恒定。
- 使用实时读取、无限循环，忽略音频。循环衔接和传输时间戳交给 FFmpeg 处理。
- 转码速度显示在输入状态中。持续低于 `1x` 表示电脑无法实时完成当前转码设置，可以降低尺寸/帧率，或预先准备满足设置的 H.264 文件以直接转发。4K 转码是否实时取决于电脑性能。
- 改变尺寸、帧率前先停止推流；下次开始时使用新设置。

## 预览与重连

两个窗口各有独立的 FFmpeg 解码进程和 Python 读取线程，帧缓冲容量均为 **1**。界面通过定时器取最新图像，慢帧被替换，不通过无限图像事件队列传输。

为控制显示开销，预览图像适配到最大 `960x540`，显示上限 **15 fps**。状态栏展示源视频尺寸和实际显示帧率；这两个值与推流配置分开。两个窗口不要求逐帧同步。

连接失败、发布中断和长时间无图像都会触发重连。失败后等待 2～5 秒再试，10 秒无有效图像会重建解码进程。流恢复到画面出现还受连接握手、探测和下一个关键帧时间影响。服务不会因为发布者或播放器退出而停止。

本机内部发布及预览使用 `127.0.0.1`，板端使用界面中的局域网地址；两个地址访问同一个 SRS 服务。

## 板端配置

假设界面选择的电脑 IP 为 `10.34.39.100`，端口 `1935`：

```text
板端输入：rtmp://10.34.39.100:1935/live/testsrc2
板端输出：rtmp://10.34.39.100:1935/live/alertgateway
```

在板端 `/home/alientek/AlertGateway/config/config_4k_18mbps.json` 中仅修改下面两个字段，保留原配置的其他内容（这是字段片段）：

```json
"source": {
  "url": "rtmp://10.34.39.100:1935/live/testsrc2"
},
"stream": {
  "rtmp_url": "rtmp://10.34.39.100:1935/live/alertgateway"
}
```

## 日志与验收

界面日志最多保留 2000 行，后台日志队列也限制长度。完整运行日志保存于 `runtime/logs/manager.log`，单文件约 5 MB，轮换保留 3 个备份。服务的实际配置保存于 `runtime/srs.conf`；上次的界面配置保存在 `runtime/settings.json`。

只管理当前实例通过 `subprocess.Popen` 创建的进程对象；不按名称批量结束 `ffmpeg.exe` 或 `srs.exe`，不接管已占用的服务端口。正常停止和关闭时回收进程；强制杀死 Python 或系统断电不属于正常关闭流程。

人工验收按以下顺序：

1. 电脑发布视频，原始窗口有画面，尺寸和推流配置相符。
2. 启动板端，板端日志显示解码器打开成功。
3. 板端输出后，结果窗口有画面。
4. 使用包含目标的视频，结果视频出现检测框。
5. 停止输入，再恢复输入，确认服务存活、板端自身重连、两路窗口恢复。

无需板端的自动检查：

```powershell
conda activate srs-service
python -m unittest discover -s tests -v
python scripts/smoke_test.py --file .\test.mp4
python scripts/gui_smoke_test.py
```

集成检查默认使用临时端口，也可指定 `--port 1935`。它启动真实 SRS、FFmpeg 发布者、两路独立预览，并用 FFmpeg 转发模拟板端输出，验证尺寸、断线恢复、服务重启和进程清理；不会验证板端解码器、模型或检测框。检查前请确保相应端口未被其他程序占用。

`gui_smoke_test.py` 使用项目根目录中的第一个 MP4，在隐藏窗口中验证实际按钮、图像显示、显示帧率及播放中关闭。测试配置位于临时目录，不覆盖日常界面设置；截图保存在 `runtime/gui-preview.png`。

## 文件结构

```text
environment.yml          Conda 环境定义
main.py                  桌面程序入口
start.ps1                本项目环境启动脚本
srs_manager/media.py     参数校验、FFmpeg 命令、PPM 帧解析、SRS 配置
srs_manager/backend.py   服务/发布/预览线程、状态、日志、重连
srs_manager/processes.py 自建进程登记与回收
srs_manager/gui.py       PySide6 界面
scripts/install_srs.py   官方 Windows SRS 下载和解包
scripts/smoke_test.py    真实进程集成检查（模拟板端）
tests/test_media.py      配置、转码决策、图像边界及缓冲检查
tools/srs/               本地 SRS 二进制，不纳入源码
runtime/                 本地日志和运行配置，不纳入源码
```

Python 环境位于 Miniconda 的 `envs/srs-service`，不随项目源码存储。
