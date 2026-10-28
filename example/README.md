# 最小示例

下载 Windows x64 安装包、中文语言包和 Python 插件：

```bash
python vscode_offline_downloader.py all \
  -c example/download_config.yaml \
  -C example \
  --no-remote
```

`--no-remote` 跳过远程 Server/CLI。非交互式环境请加 `--yes`。

`vscode.version` 留空表示最新稳定版，也可以填写 `'1.128.1'` 这样的版本号。
语言包会自动匹配 VS Code 版本，不需要配置 `version`。
