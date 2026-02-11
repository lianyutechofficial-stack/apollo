# Gateway Server

Kiro API 网关服务器端。负责：
- 管理 Kiro token 池（管理员添加的凭证）
- 用户 API key 管理（usertoken 生成与验证）
- 请求转发到 Kiro API（OpenAI 兼容协议）
- 轮询负载均衡多个 token

## 部署

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env
python main.py
```
