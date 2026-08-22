# Review Writer CN Expert Kit

这是给 QoderWork CN 的 Expert Kit。插件根目录必须保持为 ZIP 根目录中的：

```text
.qoder-plugin/plugin.json
qoderwork.md
skills/review-writer/SKILL.md
```

在产品包根目录构建：

```bash
python scripts/build_qoderwork_plugin_zip.py
```

把生成的 `build/review-writer-cn.qoder-plugin.zip` 上传到 QoderWork CN 的
`Extensions → Expert Kits`。官方插件规范见：

- https://docs.qoder.cn/qoder-plugins
- https://docs.qoder.cn/qoderwork/product-overview/what-is-qoderwork-cn
