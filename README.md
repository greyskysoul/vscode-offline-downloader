# VS Code 离线安装包下载器

一个基于 Python 标准库和 PyYAML 的命令行工具，用于下载 VS Code 离线安装所需的安装包、远程开发组件和插件。

## 功能

- 下载 VS Code 安装包，支持最新稳定版或指定版本：
  - Windows：`win32-x64-user`、`win32-arm64-user`、`win32-x64`、`win32-arm64`（`.exe`）
  - Linux：`deb`、`rpm`、`tgz`，支持 x64、arm64、armhf
  - macOS：`darwin-arm64`、`darwin-universal`（`.zip`）
- 下载远程开发组件：
  - Linux x64 / arm64 Server
  - Linux x64 / arm64 CLI，可选 Alpine（musl）或 glibc 版本
- 按 `download_config.yaml` 下载插件：
  - 通用插件和平台专用插件
  - 指定版本或自动选择最新版本
  - 语言包自动匹配 VS Code 版本
- 支持断点续传、SHA256 校验、失败重试和下载进度显示
- 可选 aria2 多连接下载、wget 或内置下载器，自动选择可用方案
- 扫描已有离线包目录并导出配置，只下载有更新的安装包和插件
- 下载前展示完整清单并确认

## 环境要求

- Python 3.8+
- PyYAML
- 可选：aria2p（Python 3.11+）和 aria2c，用于多连接加速
- 可选：wget

```bash
python -m pip install -r requirements.txt
```

使用 aria2 加速时还需要安装 `aria2c`：

```bash
pkg install aria2        # Termux
sudo apt install aria2   # Debian/Ubuntu
```

## 快速开始

下载配置中的全部内容（安装包 + 远程 Server/CLI + 插件）：

```bash
python vscode_offline_downloader.py all
```

开始前会列出所有待下载项目并询问：

```text
确认开始下载？[y/N]:
```

输入 `y` 或 `yes` 开始下载；输入 `n` 或直接回车取消。整个流程只确认一次。

只查看下载计划，不写入文件：

```bash
python vscode_offline_downloader.py all --dry-run
```

只下载某一部分：

```bash
# 只下载安装包
python vscode_offline_downloader.py installers

# 只下载远程 Server + CLI
python vscode_offline_downloader.py remote

# 只下载插件
python vscode_offline_downloader.py extensions
```

指定配置文件：

```bash
python vscode_offline_downloader.py all \
  -c example/download_config.yaml \
  -C example
```

- `-c/--config` 指定配置文件，相对路径基于当前目录。
- 不指定 `-c` 时，在当前目录查找 `download_config.yaml`。
- 当前目录没有配置文件且未指定 `-c` 时，`all` 只下载交互式选择的安装包。
- 没有配置文件时，`extensions` 无法运行；`remote` 仍可下载最新稳定版。

非交互式环境或脚本调用时，用 `--targets` 指定平台，并加 `--yes` 跳过确认：

```bash
# 指定平台
python vscode_offline_downloader.py installers \
  --targets win32-x64-user,linux-deb-x64,linux-deb-arm64 --yes

# 全部平台和格式
python vscode_offline_downloader.py installers --targets all --yes

# 跳过安装包，只下载远程组件和插件
python vscode_offline_downloader.py all --targets none --yes

# 使用 aria2 多连接下载安装包和远程组件
python vscode_offline_downloader.py all --download-method aria2 --yes
```

`--targets` 会覆盖配置文件中的 `vscode.targets`。平台专用插件的架构会跟随
`--targets`；`--targets none` 时回退到配置中的 `vscode.targets`。

## 导出配置（增量更新）

在已有离线包的目录中运行：

```bash
python vscode_offline_downloader.py export
```

脚本会递归扫描当前目录，识别 VS Code 安装包和 `.vsix` 插件，并把扫描到的版本写入
`download_config.yaml` 的 `current_version` 字段。把生成的文件放到另一台电脑上运行：

```bash
python vscode_offline_downloader.py all -c download_config.yaml
```

下载器会查询最新版本，只下载比 `current_version` 更新的安装包和插件；已经是最新的
项目会跳过。

插件条目里的 `path` 是相对扫描目录的。另一台电脑上保持相同的目录结构，或用 `-C`
指定离线包根目录即可。

```bash
# 指定扫描目录
python vscode_offline_downloader.py export -C /path/to/offline-packages

# 指定输出文件
python vscode_offline_downloader.py export -o config/update.yaml

# 覆盖已存在的输出文件
python vscode_offline_downloader.py export --force
```

扫描规则：

- Windows：`VSCodeUserSetup-*.exe`、`VSCodeSetup-*.exe`
- Linux：`code_*.deb`、`code-*.rpm`、`code-stable-*.tar.gz`
- macOS：`VSCode-darwin-*.zip`
- 插件：读取 `.vsix` 内的 `extension.vsixmanifest`，识别 `publisher.name`、版本和目标平台

如果同一架构或同一插件存在多个版本，脚本会使用最高版本并打印提示。
`current_version` 只用于判断是否有更新，不会把下载限制在旧版本；需要固定版本时继续使用
`version` 字段。

## 命令说明

```text
python vscode_offline_downloader.py [all|installers|remote|extensions|export] [选项]
```

| 选项 | 说明 |
| --- | --- |
| `-c, --config download_config.yaml` | 配置文件路径；不指定时在当前目录查找默认文件 |
| `-o, --output download_config.yaml` | `export` 输出的配置文件 |
| `--targets <列表>` | 逗号分隔的安装包平台；`all` 全选，`none` 不下载安装包 |
| `--download-method aria2` | 下载方式：`aria2`、`wget` 或 `none`；不指定时自动选择 |
| `--cli-flavor alpine` | 远程 CLI 类型，默认 `alpine`；可选 `linux` |
| `--no-remote` | `all` 模式下不下载远程 Server/CLI |
| `-y, --yes` | 跳过下载前的 y/N 确认 |
| `-C, --base-dir .` | 下载根目录；`export` 时作为扫描目录 |
| `--force` | 覆盖已存在的文件；`export` 时覆盖输出配置 |
| `--dry-run` | 只显示计划，不实际下载 |
| `--retries 3` | 下载失败重试次数 |
| `--timeout 60` | 网络超时时间（秒） |

说明：

- 安装包和远程 Server/CLI 默认使用最新稳定版，也可以在 `vscode.version` 中指定版本。
- 安装包、远程组件和语言包使用同一个 VS Code 版本。
- 默认的 Alpine CLI 为静态链接，兼容 glibc 和 musl 发行版；目标环境需要 glibc 版时使用 `--cli-flavor linux`。
- 下载地址由 VS Code 更新接口返回，使用官方 PRSS CDN，无需手动配置镜像。

### 下载方式

安装包和远程 Server/CLI 支持三种下载方式：

| 方式 | 说明 |
| --- | --- |
| `aria2` | 使用 aria2p + aria2c，多连接分段下载，默认 8 连接，速度最快 |
| `wget` | 调用系统 `wget`，单连接下载 |
| `none` | 使用内置 urllib，单连接下载 |

不指定 `--download-method` 时，按 `aria2` → `wget` → 内置的顺序自动选择可用方案。

```bash
# 强制使用 aria2
python vscode_offline_downloader.py all --download-method aria2

# 使用 wget
python vscode_offline_downloader.py installers --download-method wget

# 使用内置下载器
python vscode_offline_downloader.py installers --download-method none
```

`aria2` 需要同时安装 `aria2p` 和 `aria2c`。VSIX 插件固定使用内置下载器，
因为 Marketplace 返回 gzip 压缩内容，需要解压后再保存。

## 配置文件格式

`download_config.yaml` 包含 `vscode` 和 `extensions` 两个顶层字段。

```yaml
vscode:
  version:        # 可选；留空或省略表示最新稳定版，例如 '1.128.1'
  current_version: '1.143.1'  # 可选；当前已有版本，只下载更新的版本
  targets:
    - platform: win32-x64
      package: user
    - platform: linux-x64
      package: deb
    - platform: linux-arm64
      package: deb

extensions:
  - name: alefragnani.Bookmarks
    path: ext
    version: '14.1.1'

  - name: ms-vscode.cpptools
    path: ext/ccpp
    target: linux-x64
    current_version: '1.35.5'  # 可选；已安装版本

  - name: some.publisher.extension
    path: ext
    version:        # 留空表示最新版本
```

### vscode 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `version` | 否 | 固定 VS Code 版本，如 `1.128.1`；设置后不会再检查更新 |
| `current_version` | 否 | 当前已有版本；只下载比它更新的版本 |
| `targets` | 否 | 安装包列表；省略时使用内置默认值，`targets: []` 表示不下载安装包 |

`targets` 中每一项包含：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `platform` | 是 | 平台和架构：`win32-x64`、`win32-arm64`、`linux-x64`、`linux-arm64`、`linux-armhf`、`darwin-arm64`、`darwin-universal` |
| `package` | 否 | 安装包类型：Windows 用 `user`/`system`，Linux 用 `deb`/`rpm`/`tgz`，macOS 用 `zip`；省略时 Windows 默认 `user`、Linux 默认 `deb`、macOS 默认 `zip` |
| `current_version` | 否 | 该架构当前已有版本；会覆盖全局 `vscode.current_version` |

也可以直接写平台 ID：

```yaml
vscode:
  version: '1.128.1'
  targets:
    - win32-x64-user
    - linux-deb-x64
    - linux-deb-arm64
```

`targets` 决定下载哪些安装包，也决定平台专用插件的架构。省略时使用内置默认值
（`win32-x64-user`、`linux-deb-x64`、`linux-deb-arm64`）。

### extensions 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | Marketplace 插件 ID，格式为 `publisher.extension` |
| `path` | 否 | 保存目录，相对于运行目录或 `--base-dir`；默认当前目录 |
| `version` | 否 | 固定插件版本；设置后不会再检查更新 |
| `current_version` | 否 | 当前已有版本；只下载比它更新的版本 |
| `target` | 否 | 平台专用版本，如 `win32-x64`、`linux-x64`、`linux-arm64`、`alpine-x64` |

`version` 用于固定版本，`current_version` 用于增量更新。两者同时存在时，`version`
优先，不再检查更新。

平台专用插件可以写 `target` 标记。实际下载的架构会跟随 `vscode.targets`，而不是固定使用
这里的 `target`：

- `vscode.targets` 选择 `linux-arm64`，就只下载 `linux-arm64` 插件变体；
- 选择多个架构时，每个架构各下载一个变体；
- 配置里有对应架构的条目时沿用其版本，否则使用该架构的最新版本；
- 带 `current_version` 的显式架构会保留，即使不在 `vscode.targets` 中。

如果插件没有写 `target`，但 Marketplace 上只提供平台专用版本（或平台版本明显新于
通用版本），脚本会自动按 `vscode.targets` 展开。例如 `ms-vscode.cpptools`
不需要手动写多个 `target`，选择三个 VS Code 平台就会下载三个架构的 VSIX。

如果写了 `target` 但 `version` 留空，脚本会自动选择该平台的最新版本。

### 语言包

语言包（`MS-CEINTL.vscode-language-pack-*`）不接受 `version` 参数，配置后也会被忽略。
语言包始终按 VS Code 版本自动选择：

1. 优先使用与 VS Code 主次版本完全一致的系列。例如 VS Code 为 `1.128.1`，选择 `1.128.*` 系列的最新构建。
2. 如果没有完全一致的系列，使用小于目标版本的最近一个系列，并打印提示。
3. 如果没有可用的系列，则跳过该语言包，其他插件继续下载。

Marketplace 上的语言包可能暂时落后于 VS Code 版本，这是上游发布节奏导致的。

## 输出目录

下载完成后，目录结构大致如下：

```text
.
├── VSCodeUserSetup-x64-<版本>.exe
├── code_<版本>-<时间戳>_amd64.deb
├── code_<版本>-<时间戳>_arm64.deb
├── code-<版本>-<时间戳>.x86_64.rpm
├── code-stable-x64-<时间戳>.tar.gz
├── download_config.yaml
├── 远程开发/
│   ├── ms-vscode-remote.remote-ssh-<版本>.vsix
│   ├── ...
│   └── server+cli/
│       ├── vscode-server-linux-x64.tar.gz
│       ├── vscode-server-linux-arm64.tar.gz
│       ├── vscode_cli_alpine_x64_cli.tar.gz
│       └── vscode_cli_alpine_arm64_cli.tar.gz
└── ext/
    ├── alefragnani.Bookmarks-<版本>.vsix
    ├── ccpp/
    │   ├── ms-vscode.cpptools-<版本>@linux-x64.vsix
    │   └── ...
    └── ...
```

deb/rpm/tgz 文件名中的时间戳由微软服务器决定，脚本会自动解析。

## 断点续传与校验

- 安装包、Server、CLI 下载时使用 `.part` 临时文件，支持 HTTP Range 断点续传；aria2 方式还会保留 `.part.aria2` 控制文件，网络中断后重新运行会从上次位置继续。
- 下载完成后才会重命名为正式文件名。
- 使用最新稳定版时，脚本会通过微软更新接口校验 SHA256；指定 `vscode.version` 时接口不提供校验值，脚本会检查文件是否完整下载。
- Marketplace 的 VSIX 响应通常是 gzip 压缩的，脚本会自动解压后再保存；VSIX 固定使用内置下载器。
- 已存在的文件默认跳过；需要重新下载时加 `--force`。

## 下载进度

下载过程中显示三行信息：

```text
正在下载: VSCodeUserSetup-x64-1.143.1.exe
[========================------]  75.0%
速度 12.3 MB/s   已下载 90.0 MB/120.0 MB   已用 00:30   ETA 00:10
```

- 第一行：当前文件。
- 第二行：进度条和百分比，窄屏时自动缩短。
- 第三行：速度、已下载/总大小、已用时间和 ETA，按终端宽度压缩。
- 速度为最近 1 秒的滑动平均值，每 0.5 秒刷新一次；启动后的最初约 1 秒显示 `--`，避免缓存突发导致数值虚高。
- 非交互式终端只输出最终结果，不刷新进度。
- VSIX 使用 gzip 压缩响应，解压后的总大小未知，因此进度条显示 `?`，ETA 显示 `--:--`。

## 离线安装插件

下载完成后，在目标机器上执行：

```bash
code --install-extension ext/alefragnani.Bookmarks-14.1.1.vsix
code --install-extension ext/ccpp/ms-vscode.cpptools-1.33.4@linux-x64.vsix
```

Windows 下把 `/` 换成 `\`。

## 常见问题

**提示缺少 PyYAML**

```bash
python -m pip install pyyaml
```

**当前目录没有 download_config.yaml**

`all` 会只下载交互式选择的安装包。需要下载插件时，请准备配置文件，或用 `-c` 指定路径。

**export 提示输出文件已存在**

```bash
python vscode_offline_downloader.py export --force
# 或
python vscode_offline_downloader.py export -o other.yaml
```

**export 没有识别到安装包或插件**

确认文件是常见的 VS Code 安装包命名，并且 `.vsix` 内包含 `extension.vsixmanifest`。
可以用 `-C` 指定实际目录：

```bash
python vscode_offline_downloader.py export -C /path/to/packages
```

**current_version 和 version 有什么区别**

`version` 表示固定下载这个版本；`current_version` 表示当前已有版本，只下载更新的版本。
`export` 生成的是 `current_version`。

**插件下载返回 404**

先检查 `version` 是否存在；如果是平台专用插件，确认配置里有 `target`。也可以把 `version` 留空，让脚本自动选择该平台的最新版本。

**语言包报错：没有找到与 VS Code 匹配的版本**

说明 Marketplace 还没有发布对应 VS Code 系列的语言包。可以把语言包的 `version` 留空，让脚本自动匹配；如果仍然没有，请等待上游发布。

**远程 Server 和客户端版本不一致**

脚本在同一次运行中解析安装包和远程 Server/CLI，正常情况下版本一致。微软分批发布时可能出现短暂差异，重新运行即可。

**下载速度慢**

脚本使用官方 PRSS CDN，该 CDN 在中国大陆也有节点。如果速度仍然不理想，可以：

- 使用 aria2 多连接下载：`--download-method aria2`。单连接通常会被限速，多连接可以明显提速。
- 检查本地网络或代理设置。
- 使用 HTTP 代理时，在运行脚本的终端里设置 `HTTPS_PROXY` 和 `HTTP_PROXY`。`urllib` 不支持 SOCKS 代理，请使用代理软件提供的 HTTP 端口。
- 避开网络高峰时段，或稍后重试。
