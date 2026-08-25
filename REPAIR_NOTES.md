# 修复记录（进行中）

- 新增 `authorization.py`：授权白名单保存在 `APITELEGRAMCHAT_DATA_DIR`，采用 JSON schema、原子替换与 `0600` 文件权限。
- `config.py`：移除 webhook 查询参数拼接；引入请求头密钥配置；白名单兼容接口改接授权存储；关键整数环境变量改为有边界的安全解析；不再在 import 时篡改全局环境变量。
- `app.py`：启动时加载白名单；健康检查不再泄露运行详情；Webhook 改为 Telegram `secret_token` 请求头验证；白名单写入失败时回滚内存变更；普通命令改为精确匹配；更新去重将改接 TTL 状态接口。
- `state.py`：正在迁移为带访问时间与 TTL 的进程级临时状态，后续将补充回归测试。
